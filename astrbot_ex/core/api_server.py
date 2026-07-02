from __future__ import annotations

import argparse
import cgi
import json
import os
import queue
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from astrbot_ex.core.models import RuntimeEvent, RuntimeState
from astrbot_ex.core.local_plugins import LocalPluginManager
from astrbot_ex.core.runtime import AstrBotEXRuntime
from astrbot_ex.core.runtime_demo import build_demo_runtime
from astrbot_ex.core.serialization import to_jsonable
from astrbot_ex.core.vision_sources import VisionSourceManager


class RuntimeController:
    def __init__(self, runtime: AstrBotEXRuntime, tick_hz: float = 5.0) -> None:
        self.runtime = runtime
        self.tick_hz = tick_hz
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            self.runtime.start()
            if self.runtime.state != RuntimeState.RUNNING:
                return
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._tick_loop, name="astrbotex-tick", daemon=True)
            self._thread.start()

    def stop(self, reason: str = "stopped by api") -> None:
        with self._lock:
            self._stop_event.set()
            self.runtime.stop(reason)

    def status(self) -> dict[str, Any]:
        with self._lock:
            active_skill = self.runtime.active_skill
            robot = self.runtime.world.robot
            return {
                "runtime_state": self.runtime.state.value,
                "tick_hz": self.tick_hz,
                "active_skill": active_skill.plugin.id if active_skill else None,
                "active_goal": active_skill.goal if active_skill else None,
                "world": {
                    "timestamp": self.runtime.world.timestamp,
                    "entities": self.runtime.world.entities,
                    "zones": self.runtime.world.zones,
                    "robot": robot,
                },
                "plugins": [
                    {
                        "id": getattr(slot.plugin, "id", slot.plugin.__class__.__name__),
                        "name": getattr(slot.plugin, "name", slot.plugin.__class__.__name__),
                        "kind": slot.kind,
                        "enabled": slot.enabled,
                    }
                    for slot in self.runtime.registry.list()
                ],
                "recent_events": self.runtime.event_bus.recent(20),
            }

    def _tick_loop(self) -> None:
        interval = 1.0 / self.tick_hz if self.tick_hz > 0 else 0.2
        while not self._stop_event.is_set():
            with self._lock:
                if self.runtime.state == RuntimeState.RUNNING:
                    self.runtime.tick()
            time.sleep(interval)


class EventStream:
    def __init__(self, controller: RuntimeController) -> None:
        self.controller = controller
        self.queue: queue.Queue[RuntimeEvent] = queue.Queue(maxsize=200)
        self.unsubscribe = controller.runtime.event_bus.subscribe(self._on_event)

    def close(self) -> None:
        self.unsubscribe()

    def recent(self) -> list[RuntimeEvent]:
        return self.controller.runtime.event_bus.recent(50)

    def get(self, timeout: float) -> RuntimeEvent | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _on_event(self, event: RuntimeEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(event)


class AstrBotEXRequestHandler(BaseHTTPRequestHandler):
    server_version = "AstrBotEXAPI/0.1"

    def do_GET(self) -> None:
        if self._try_send_static():
            return
        path = self._path()
        if path == "/api/status" or path == "/api/v1/ex/status":
            self._send_json(self.controller.status())
            return
        if path == "/api/events" or path == "/api/v1/ex/events":
            self._send_events()
            return
        if path in {"/api/vision/sources", "/api/v1/ex/vision/sources"}:
            self._send_json(
                {
                    "active_source": self.server.vision_sources.active_source_id,
                    "sources": self.server.vision_sources.list_sources(),
                }
            )
            return
        if path in {"/api/vision/active-source", "/api/v1/ex/vision/active-source"}:
            self._send_json({"active_source": self.server.vision_sources.active()})
            return
        if path in {"/api/vision/latest", "/api/v1/ex/vision/latest"}:
            self._send_json(self.server.vision_sources.latest())
            return
        if path in {"/api/plugins", "/api/v1/ex/plugins"}:
            plugins = self.server.local_plugins.list_plugins()
            grouped = {category: [] for category in ("vision", "control", "decision", "special")}
            for plugin in plugins:
                grouped.setdefault(plugin.get("category", "special"), []).append(plugin)
            self._send_json({"plugins": plugins, "groups": grouped})
            return
        plugin_id = self._match_plugin_id(path)
        if plugin_id:
            try:
                self._send_json({"plugin": self.server.local_plugins.get_plugin(plugin_id)})
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        cover_id = self._match_plugin_cover(path)
        if cover_id:
            self._send_plugin_cover(cover_id)
            return
        if path == "/healthz":
            self._send_json({"ok": True})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = self._path()
        if path == "/api/runtime/start" or path == "/api/v1/ex/runtime/start":
            self.controller.start()
            self._send_json({"ok": True, "state": self.controller.runtime.state.value})
            return
        if path == "/api/runtime/stop" or path == "/api/v1/ex/runtime/stop":
            payload = self._read_json()
            reason = str(payload.get("reason", "stopped by api"))
            self.controller.stop(reason)
            self._send_json({"ok": True, "state": self.controller.runtime.state.value})
            return
        if path in {"/api/vision/sources", "/api/v1/ex/vision/sources"}:
            payload = self._read_json()
            try:
                source = self.server.vision_sources.upsert(payload)
            except TypeError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "source": source})
            return
        if path in {"/api/vision/active-source", "/api/v1/ex/vision/active-source"}:
            payload = self._read_json()
            source_id = str(payload.get("id", ""))
            try:
                source = self.server.vision_sources.set_active(source_id)
            except KeyError:
                self._send_json({"ok": False, "error": f"unknown source: {source_id}"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "active_source": source})
            return
        source_id = self._match_source_action(path, "test")
        if source_id:
            try:
                result = self.server.vision_sources.test(source_id)
            except KeyError:
                self._send_json({"ok": False, "error": f"unknown source: {source_id}"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json(result)
            return
        if path in {"/api/plugins/upload", "/api/v1/ex/plugins/upload"}:
            self._handle_plugin_upload()
            return
        plugin_id = self._match_plugin_action(path, "enable")
        if plugin_id:
            try:
                plugin = self.server.local_plugins.set_enabled(plugin_id, True)
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "plugin": plugin})
            return
        plugin_id = self._match_plugin_action(path, "disable")
        if plugin_id:
            try:
                plugin = self.server.local_plugins.get_plugin(plugin_id)
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            if any(cap in plugin.get("provides", []) for cap in ("motion_bridge", "transport", "protocol_codec", "skill_plugin")):
                self.controller.stop(f"plugin disabled: {plugin_id}")
            try:
                plugin = self.server.local_plugins.set_enabled(plugin_id, False)
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "plugin": plugin})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        path = self._path()
        source_id = self._match_source_id(path)
        if source_id:
            payload = self._read_json()
            payload["id"] = source_id
            try:
                source = self.server.vision_sources.upsert(payload)
            except TypeError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "source": source})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        path = self._path()
        source_id = self._match_source_id(path)
        if source_id:
            try:
                self.server.vision_sources.delete(source_id)
            except KeyError:
                self._send_json({"ok": False, "error": f"unknown source: {source_id}"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    @property
    def controller(self) -> RuntimeController:
        return self.server.controller

    def _path(self) -> str:
        return urlparse(self.path).path

    @staticmethod
    def _match_source_id(path: str) -> str | None:
        for prefix in ("/api/vision/sources/", "/api/v1/ex/vision/sources/"):
            if path.startswith(prefix) and not path.endswith("/test"):
                return unquote(path.removeprefix(prefix).strip("/"))
        return None

    @staticmethod
    def _match_source_action(path: str, action: str) -> str | None:
        suffix = f"/{action}"
        for prefix in ("/api/vision/sources/", "/api/v1/ex/vision/sources/"):
            if path.startswith(prefix) and path.endswith(suffix):
                return unquote(path.removeprefix(prefix).removesuffix(suffix).strip("/"))
        return None

    @staticmethod
    def _match_plugin_id(path: str) -> str | None:
        for prefix in ("/api/plugins/", "/api/v1/ex/plugins/"):
            if path.startswith(prefix) and not path.endswith("/enable") and not path.endswith("/disable") and not path.endswith("/cover"):
                tail = path.removeprefix(prefix).strip("/")
                if tail and "/" not in tail:
                    return unquote(tail)
        return None

    @staticmethod
    def _match_plugin_action(path: str, action: str) -> str | None:
        suffix = f"/{action}"
        for prefix in ("/api/plugins/", "/api/v1/ex/plugins/"):
            if path.startswith(prefix) and path.endswith(suffix):
                return unquote(path.removeprefix(prefix).removesuffix(suffix).strip("/"))
        return None

    @staticmethod
    def _match_plugin_cover(path: str) -> str | None:
        suffix = "/cover"
        for prefix in ("/api/plugins/", "/api/v1/ex/plugins/"):
            if path.startswith(prefix) and path.endswith(suffix):
                return unquote(path.removeprefix(prefix).removesuffix(suffix).strip("/"))
        return None

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(to_jsonable(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _handle_plugin_upload(self) -> None:
        ctype, _ = cgi.parse_header(self.headers.get("Content-Type", ""))
        if ctype != "multipart/form-data":
            self._send_json({"ok": False, "error": "multipart/form-data required"}, HTTPStatus.BAD_REQUEST)
            return
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )
        upload = form["file"] if "file" in form else None
        if upload is None or not getattr(upload, "filename", ""):
            self._send_json({"ok": False, "error": "missing plugin zip file"}, HTTPStatus.BAD_REQUEST)
            return
        filename = str(upload.filename)
        if not filename.lower().endswith(".zip"):
            self._send_json({"ok": False, "error": "only .zip plugin packages are supported"}, HTTPStatus.BAD_REQUEST)
            return
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            shutil_source = upload.file
            while True:
                chunk = shutil_source.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
            temp_path = Path(tmp.name)
        try:
            category = form.getfirst("category")
            plugin = self.server.local_plugins.install_zip(temp_path, category=category)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        self._send_json({"ok": True, "plugin": plugin})

    def _send_plugin_cover(self, plugin_id: str) -> None:
        try:
            plugin = self.server.local_plugins.get_plugin(plugin_id)
            record = self.server.local_plugins.records[plugin["id"]]
            cover = record.manifest.cover
            if not cover:
                self._send_json({"ok": False, "error": "cover not configured"}, HTTPStatus.NOT_FOUND)
                return
            target = (record.root / cover).resolve()
            if record.root.resolve() not in target.parents and target != record.root.resolve():
                self._send_json({"ok": False, "error": "invalid cover path"}, HTTPStatus.BAD_REQUEST)
                return
            body = target.read_bytes()
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        content_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_types.get(target.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _try_send_static(self) -> bool:
        path = self.path.split("?", 1)[0]
        if path == "/":
            relative = "index.html"
        elif path.startswith("/dashboard/"):
            relative = path.removeprefix("/dashboard/")
        elif path in {"/index.html", "/styles.css", "/app.js"}:
            relative = path.lstrip("/")
        else:
            return False

        static_root = self.server.static_root
        target = (static_root / relative).resolve()
        if static_root not in target.parents and target != static_root:
            self._send_json({"error": "invalid static path"}, HTTPStatus.BAD_REQUEST)
            return True
        if not target.is_file():
            self._send_json({"error": "static file not found"}, HTTPStatus.NOT_FOUND)
            return True

        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_types.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        return True

    def _send_events(self) -> None:
        stream = EventStream(self.controller)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            for event in stream.recent():
                self._write_sse("event", event)
            while True:
                event = stream.get(timeout=10.0)
                if event is None:
                    self._write_raw(": keepalive\n\n")
                    continue
                self._write_sse("event", event)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            stream.close()

    def _write_sse(self, event_name: str, payload: Any) -> None:
        data = json.dumps(to_jsonable(payload), ensure_ascii=False)
        self._write_raw(f"event: {event_name}\ndata: {data}\n\n")

    def _write_raw(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()


class AstrBotEXHTTPServer(ThreadingHTTPServer):
    controller: RuntimeController
    static_root: Path
    vision_sources: VisionSourceManager
    local_plugins: LocalPluginManager


def build_server(host: str, port: int, tick_hz: float) -> AstrBotEXHTTPServer:
    runtime = build_demo_runtime()
    controller = RuntimeController(runtime=runtime, tick_hz=tick_hz)
    server = AstrBotEXHTTPServer((host, port), AstrBotEXRequestHandler)
    server.controller = controller
    project_root = Path(__file__).resolve().parents[2]
    server.static_root = (project_root / "dashboard").resolve()
    server.vision_sources = VisionSourceManager(project_root / "profiles" / "default" / "vision_sources.json")
    server.local_plugins = LocalPluginManager(
        plugins_root=project_root / "plugins",
        state_path=project_root / "profiles" / "default" / "plugins_state.json",
        registry=runtime.registry,
        event_bus=runtime.event_bus,
    )
    server.local_plugins.discover()
    server.local_plugins.load_enabled()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AstrBotEX local API server.")
    parser.add_argument("--host", default=os.environ.get("ASTRBOTEX_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ASTRBOTEX_PORT", "8765")))
    parser.add_argument("--tick-hz", type=float, default=float(os.environ.get("ASTRBOTEX_TICK_HZ", "5")))
    args = parser.parse_args()

    server = build_server(args.host, args.port, args.tick_hz)
    print(f"AstrBotEX API listening on http://{args.host}:{args.port}")
    print(f"Dashboard: http://{args.host}:{args.port}/")
    print("Core endpoints: /api/status, /api/events, /api/runtime/start, /api/runtime/stop")
    print("Vision endpoints: /api/v1/ex/vision/sources, /api/v1/ex/vision/latest")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping AstrBotEX API server...")
    finally:
        server.controller.stop("api server shutdown")
        server.server_close()


if __name__ == "__main__":
    main()
