"""C07 actual HTTP composition. Only loopback suppliers are synthetic.

No decision endpoint interception, real credentials, model/owned processes, or
physical plugins. Temporary data is isolated; stdout is the bounded harness RPC.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The read-only Host venv and user Python share the 3.12 ABI/installed pyzmq.
sys.path.append(str(Path.home() / "AppData/Local/Programs/Python/Python312/Lib/site-packages"))

from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.decision.management import ManagementSettings
from tests.test_laya_backend import health, response
from tests.test_provider_connection import jev_response


def close_owned(outcomes, resource, close):
    try:
        close()
    except BaseException as error:
        outcomes.append({"resource": resource, "ok": False,
                         "error": type(error).__name__ + ": " + str(error)})
    else:
        outcomes.append({"resource": resource, "ok": True})


def join_owned(thread, timeout):
    thread.join(timeout)
    if thread.is_alive():
        raise RuntimeError("owned thread did not stop")


class ActualProjection:
    """Actual AEB handler/Store over ROUTER/DEALER; no projection dict stub."""
    def __init__(self, fixture):
        configured = os.environ.get("ASTRBOTEX_AEB_TEST_ROOT")
        if not configured:
            raise RuntimeError("explicit ASTRBOTEX_AEB_TEST_ROOT required")
        companion = Path(configured).resolve() / "astrbot_plugin_astrbotex_interaction"
        if not (companion / "main.py").is_file():
            raise RuntimeError("invalid explicit AEB companion root")
        # Installed read-only Host modules, same Python ABI; never load formal AEB.
        sys.path.append(str(Path.home() / "AppData/Local/Programs/Python/Python312/Lib/site-packages"))
        sys.path.append(r"D:\Code\AstrBot-latest")
        name = "_c07_actual_aeb"
        spec = importlib.util.spec_from_file_location(name, companion / "__init__.py", submodule_search_locations=[str(companion)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        plugin_module = importlib.import_module(name + ".main")
        transport = importlib.import_module(name + ".zmq_transport")
        models = importlib.import_module(name + ".task_models")
        stores = importlib.import_module(name + ".task_store")
        self.companion = companion
        self.hashes = {str(p.relative_to(companion)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in companion.glob("*.py")}
        self.fixture = fixture
        self.plugin = object.__new__(plugin_module.AstrBotEXInteractionPlugin)
        self.plugin.request_timeout_sec = .8
        self.plugin.task_store = stores.TaskStore(str(fixture.root / "aeb-tasks.sqlite3"))
        self.authority = models.TaskAuthority("c07-synthetic-robot", "c07-synthetic-session", "c07-private-user",
                                              "c07-private-route", True)
        self.peer = b"c07-ex-actual"
        self.plugin.task_store.bind_route(self.authority, self.peer, origin="fixture-private-origin")
        self.plugin.text_channel = transport.ZmqRouterChannel("text", "tcp://127.0.0.1:0", request_timeout_sec=.8)
        self.projection_calls = 0
        self.capability_calls = 0
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.connected = False
        self.task_id = None
        try:
            self.run(self.start())
        except BaseException:
            self.close()
            raise

    def run(self, work):
        return asyncio.run_coroutine_threadsafe(work, self.loop).result(5)

    async def start(self):
        async def projection(peer, envelope, binary):
            self.projection_calls += 1
            return await self.plugin._handle_task_projection(peer, envelope, binary)
        # Instrument real channel requests without replacing handler/result/transport.
        original = self.plugin.text_channel.request
        async def request(method, *args, **kwargs):
            if method == "decision.capabilities.get":
                self.capability_calls += 1
            return await original(method, *args, **kwargs)
        self.plugin.text_channel.request = request
        self.plugin.text_channel.register_handler("task.projection.get", projection)
        await self.plugin.text_channel.start()
        import zmq
        endpoint = self.plugin.text_channel.socket.getsockopt(zmq.LAST_ENDPOINT).decode()
        record = self.fixture.server.connections.create({"id": "c07-projection", "name": "C07 isolated actual text",
            "type": "zmq_client", "enabled": True, "config": {"endpoint": endpoint, "identity": self.peer.decode(),
            "channel": "text", "protocol_profile": "astrbotex"}})
        self.connection_id = record["id"]
        end = self.loop.time() + 3
        while self.peer not in self.plugin.text_channel.online_peers():
            if self.loop.time() >= end:
                raise RuntimeError("actual ZMQ handshake timeout")
            await asyncio.sleep(.02)
        self.connected = True

    def task(self):
        store = self.plugin.task_store
        session = self.fixture.server.decision_management.store.ex_session
        task = store.create(self.authority, "隔离实际TaskStore投影（不执行）", "c07-message-actual", ex_session=session,
                            provider_id="fixture-not-invoked")
        self.task_id = task["task_id"]
        turn = store.begin_turn(self.task_id)
        store.save_plan(turn, [{"step_id": "c07-one", "intent": "Hold for explicit planning",
                               "completion_condition": "Actual result required"}], 0)
        return self.facts()

    def facts(self):
        value = self.plugin.task_store.get(self.task_id) if self.task_id else None
        return {"connected": self.connected, "projection_calls": self.projection_calls,
                "nested_capability_calls": self.capability_calls, "source_hashes": self.hashes,
                "task": {"title": value["text"], "phase": value["status"], "total": len(value["steps"]),
                         "completed": sum(s["status"] == "completed" for s in value["steps"]),
                         "current_goal": value["current_goal"], "generation": value["generation"]} if value else None}

    def disconnect(self):
        self.fixture.server.connections.stop(self.connection_id)
        self.run(self.plugin.text_channel.close())
        self.connected = False
        return self.facts()

    def close(self):
        outcomes = []
        if hasattr(self, "connection_id"):
            close_owned(outcomes, "projection.connection", lambda: self.fixture.server.connections.stop(self.connection_id))
        close_owned(outcomes, "projection.channel", lambda: self.run(self.plugin.text_channel.close()))
        self.connected = False
        close_owned(outcomes, "projection.task_store", self.plugin.task_store.close)
        close_owned(outcomes, "projection.loop_stop", lambda: self.loop.call_soon_threadsafe(self.loop.stop))
        close_owned(outcomes, "projection.thread", lambda: join_owned(self.thread, 3))
        close_owned(outcomes, "projection.loop_close", self.loop.close)

        def verify_sources():
            for name, digest in self.hashes.items():
                if hashlib.sha256((self.companion / name).read_bytes()).hexdigest() != digest:
                    raise RuntimeError("AEB companion sources changed during actual run")

        close_owned(outcomes, "projection.source_hashes", verify_sources)
        return outcomes


class ActualHTTPFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="c07-real-http-")
        self.root = Path(self.temp.name)
        self.suppliers = []
        self.calls = []
        self.inference_status = 200
        self.lock = threading.RLock()
        self.thread = None
        self.server = None
        self.projection = None
        self.source_hashes = self.hashes()
        os.environ["ASTRBOT_ROOT"] = str(self.root / "host-runtime")
        for name in tuple(os.environ):
            if name.startswith("ASTRBOTEX_") and name != "ASTRBOTEX_AEB_TEST_ROOT":
                del os.environ[name]
        os.environ.update(ASTRBOTEX_DATA_DIR=str(self.root), ASTRBOTEX_STT_ENABLED="", ASTRBOTEX_TTS_ENABLED="")

    @staticmethod
    def hashes():
        root = Path(__file__).resolve().parents[1]
        files = ["astrbot_ex/core/api_server.py", "astrbot_ex/core/decision/management.py",
                 "astrbot_ex/core/decision/service.py", "astrbot_ex/core/decision/controller.py",
                 "astrbot_ex/core/decision/config.py", "astrbot_ex/core/actions/dispatcher.py",
                 "astrbot_ex/core/connection_manager.py", "astrbot_ex/core/decision_transport.py",
                 "astrbot_ex/core/decision/backends/jev.py", "astrbot_ex/core/decision/backends/laya.py",
                 "dashboard/app.js", "dashboard/decision.js", "dashboard/index.html", "dashboard/styles.css",
                 "scripts/verify_decision_ui.mjs", "tests/decision_ui_real_fixture.py"]
        return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}

    def supplier(self, provider):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, value, status):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                with fixture.lock:
                    fixture.calls.append({"provider": provider, "method": "GET", "path": self.path,
                                          "status": 200, "has_authorization": bool(self.headers.get("Authorization"))})
                self.send(health(), 200)

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fixture.lock:
                    status = fixture.inference_status
                    fixture.calls.append({"provider": provider, "method": "POST", "path": self.path,
                                          "status": status, "has_authorization": bool(self.headers.get("Authorization")),
                                          "question_count": len(value["questions"]),
                                          "criteria_counts": [len(q["criteria"]) for q in value["questions"].values()]})
                self.send(jev_response(value) if provider == "jev" else response(value["questions"]), status)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.suppliers.append((server, thread))
        return "http://127.0.0.1:" + str(server.server_port) + "/c07-synthetic"

    def start(self):
        urls = {provider: self.supplier(provider) for provider in ("jev", "laya")}
        self.server = build_server("127.0.0.1", 0, 20, management_settings=ManagementSettings())
        self.token = (self.root / "secrets/admin.token").read_text().strip()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        return {"base": "http://127.0.0.1:" + str(self.server.server_port), "token": self.token,
                "urls": urls, "scope": "actual build_server HTTP; synthetic loopback suppliers only",
                "source_hashes": self.source_hashes}

    def facts(self):
        service = self.server.decision_service
        status = service.status()
        path = self.root / "profiles/default/decision.json"
        return {"runtime": self.server.controller.runtime.state.value,
                "mode": status["mode"], "control_mode": status["control_mode"], "gate_open": status["gate_open"],
                "backend_type": type(service.backend).__name__, "execution_capability": getattr(service.backend, "execution_capability", None),
                "execution_allowed": getattr(service.backend, "execution_allowed", False),
                "goal": service.goals.active is not None, "pending_goal": service.goals.pending_replace is not None,
                "actions": len(self.server.action_ledger.list_commands().result(2)),
                "history": len(self.server.decision_management.history.page()["items"]),
                "supplier_calls": copy.deepcopy(self.calls),
                "disk_config_hash": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None}

    def command(self, request):
        kind = request["command"]
        if kind == "projection_start":
            if self.projection is not None:
                raise RuntimeError("projection already initialized")
            self.projection = ActualProjection(self)
            return self.projection.facts()
        if kind == "projection_task":
            return self.projection.task()
        if kind == "projection_facts":
            return self.projection.facts()
        if kind == "projection_disconnect":
            return self.projection.disconnect()
        if kind == "facts":
            return self.facts()
        if kind == "supplier_status":
            self.inference_status = request["status"]
            return {"ok": True}
        if kind == "change_config":
            store = self.server.decision_management.store
            saved = copy.deepcopy(store.saved)
            saved["jev"]["min_interval_ms"] += 1
            store.save(saved, store.revision, store.ex_session)
            return {"revision": store.revision}
        raise ValueError("unknown actual fixture command")

    def close(self):
        outcomes = []
        if self.projection:
            def close_projection():
                outcomes.extend(self.projection.close())
            close_owned(outcomes, "projection", close_projection)
        if self.server:
            # shutdown() waits forever if serve_forever() never started.
            if self.thread and self.thread.is_alive():
                close_owned(outcomes, "http.shutdown", self.server.shutdown)
            if self.thread:
                close_owned(outcomes, "http.thread", lambda: join_owned(self.thread, 3))
            close_owned(outcomes, "http.server_close", self.server.server_close)
        for index, (server, thread) in enumerate(self.suppliers):
            close_owned(outcomes, f"supplier.{index}.shutdown", server.shutdown)
            close_owned(outcomes, f"supplier.{index}.server_close", server.server_close)
            close_owned(outcomes, f"supplier.{index}.thread", lambda: join_owned(thread, 2))

        def verify_sources():
            if self.hashes() != self.source_hashes:
                raise RuntimeError("sources changed during actual browser run")

        close_owned(outcomes, "source_hashes", verify_sources)
        close_owned(outcomes, "temporary_directory", self.temp.cleanup)
        return {"ok": all(item["ok"] for item in outcomes), "resources": outcomes}


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    fixture = ActualHTTPFixture()
    watchdog = threading.Timer(170, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    try:
        print(json.dumps({"ready": fixture.start()}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request["command"] == "quit":
                break
            try:
                result = {"id": request["id"], "result": fixture.command(request)}
            except Exception as error:
                result = {"id": request["id"], "error": type(error).__name__ + ": " + str(error)}
            print(json.dumps(result), flush=True)
    finally:
        cleanup = {"ok": False, "resources": []}
        try:
            cleanup = fixture.close()
        except BaseException as error:
            cleanup["resources"].append({"resource": "fixture.close", "ok": False,
                                         "error": type(error).__name__ + ": " + str(error)})
        finally:
            watchdog.cancel()
        print(json.dumps({"cleanup": cleanup}), flush=True)
        if not cleanup["ok"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
