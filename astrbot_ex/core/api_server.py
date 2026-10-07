from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import shutil
import tempfile
import threading
import time
import urllib.request
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from astrbot_ex.core.astrbot_bridge import AstrBotBridge
from astrbot_ex.core.actions.dispatcher import ActionDispatcher
from astrbot_ex.core.actions.ledger import ActionLedger
from astrbot_ex.core.actions.service import ActionService
from astrbot_ex.core.actions.storage import prepare_action_ledger
from astrbot_ex.core.decision.catalog import CapabilityCatalog
from astrbot_ex.core.decision.service import DecisionService, ShutdownErrors
from astrbot_ex.core.decision.management import DecisionManagement, ManagementSettings
from astrbot_ex.core.decision.config import ManagementError
from astrbot_ex.core.decision.controller import DecisionController
from astrbot_ex.core.decision.public_delivery import TaskPublicDelivery
from astrbot_ex.core.backup import SnapshotError, SnapshotService
from astrbot_ex.core.connection_manager import ConnectionManager
from astrbot_ex.core.environments import (
    EnvironmentBusyError,
    EnvironmentManager,
    EnvironmentRevisionConflict,
)
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.interaction_core import InteractionCore
from astrbot_ex.core.local_plugins import LocalPluginManager
from astrbot_ex.core.models import RuntimeEvent, RuntimeState
from astrbot_ex.core.perception_config import load_perception_config
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.providers.astrbot_providers import AstrBotSTTProvider, AstrBotTTSProvider
from astrbot_ex.core.providers.interaction_provider import STTProvider, TTSProvider
from astrbot_ex.core.runtime import AstrBotEXRuntime
from astrbot_ex.core.scene_fusion import SceneFusion
from astrbot_ex.core.serialization import to_jsonable
from astrbot_ex.core.topic_bus import TopicBus
from astrbot_ex.core.vision_sources import VisionSourceManager


class RuntimeController:
    def __init__(self, runtime: AstrBotEXRuntime, tick_hz: float = 20.0) -> None:
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
        self.runtime.request_stop()
        self._stop_event.set()
        service = self.runtime.action_service
        if service is not None:
            service.stop_actions(reason)
        self.runtime.stop(reason)

    def pause(self) -> None:
        self.runtime.request_stop()
        if self.runtime.action_service is not None:
            self.runtime.action_service.stop_actions("runtime paused")
        with self._lock:
            self.runtime.pause()

    def fail(self, reason: str) -> None:
        self.runtime.request_stop()
        if self.runtime.action_service is not None:
            self.runtime.action_service.stop_actions(reason)
        self.runtime.fail(reason)
        self._stop_event.set()

    def change_mode(self, mode: str) -> None:
        if mode not in ("legacy", "decision"):
            raise ValueError("control_mode must be legacy or decision")
        service = self.runtime.action_service
        if service is None:
            raise RuntimeError("action service unavailable")
        if mode == service.control_mode:
            return
        service.revoke()
        self.stop("control mode change")
        service.change_mode(mode)

    def status(self) -> dict[str, Any]:
        service = self.runtime.action_service
        decision = self.runtime.decision_service
        actions = decision.status() if decision is not None else service.status() if service is not None else {
            "control_mode": "legacy", "gate_open": False, "blocked": False,
            "unresolved": [], "error": None,
        }
        with self._lock:
            active_skill = self.runtime.active_skill
            robot = self.runtime.world.robot
            return {
                "runtime_state": self.runtime.state.value,
                "control_mode": actions["control_mode"],
                "actions": actions,
                "tick_hz": self.tick_hz,
                "active_skill": active_skill.plugin.id if active_skill else None,
                "active_goal": active_skill.goal if active_skill else None,
                "world": {
                    "timestamp": self.runtime.world.timestamp,
                    "entities": self.runtime.world.entities,
                    "zones": self.runtime.world.zones,
                    "robot": robot,
                    "obstacles": self.runtime.world.obstacles,
                    "perception_degraded": self.runtime.world.perception_degraded,
                },
                "plugins": [
                    {
                        "id": slot.id,
                        "name": slot.name,
                        "kind": slot.kind,
                        "enabled": slot.enabled,
                        "thread": {
                            "name": slot.actor.thread_name,
                            "alive": slot.actor.alive,
                            "last_error": slot.actor.last_error,
                        },
                    }
                    for slot in self.runtime.registry.list()
                ],
                "recent_events": self.runtime.event_bus.recent(20),
            }

    def _tick_loop(self) -> None:
        interval = 1.0 / self.tick_hz if self.tick_hz > 0 else 0.2
        while not self._stop_event.is_set():
            faulted = False
            with self._lock:
                if self.runtime.state == RuntimeState.RUNNING:
                    try:
                        self.runtime.tick()
                    except Exception as exc:
                        self.runtime.fail(f"runtime tick failed: {exc}")
                        self._stop_event.set()
                        faulted = True
                    if self.runtime.state == RuntimeState.FAULT:
                        faulted = True
                        self._stop_event.set()
            if faulted and self.runtime.action_service is not None:
                self.runtime.action_service.stop_actions("runtime tick fault")
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
        if not self._authorize_http():
            return
        if self._try_send_static():
            return
        if self.server.decision_management.handle_http(self):
            return
        path = self._path()
        backup_filename = self._match_backup_filename(path)
        if backup_filename is not None:
            self._send_backup_download(backup_filename)
            return
        if path == "/api/status" or path == "/api/v1/ex/status":
            status = self.controller.status()
            environment_manager = getattr(self.server, "environment_manager", None)
            if environment_manager is not None:
                status["environment"] = environment_manager.status()
            self._send_json(status)
            return
        if path == "/api/events" or path == "/api/v1/ex/events":
            self._send_events()
            return
        if self._environment_api(path, "GET"):
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
            grouped = {
                category: []
                for category in (
                    "vision",
                    "perception",
                    "control",
                    "decision",
                    "special",
                    "interaction",
                )
            }
            for plugin in plugins:
                grouped.setdefault(plugin.get("category", "special"), []).append(plugin)
            self._send_json({"plugins": plugins, "groups": grouped})
            return
        if path in {"/api/pubsub/publishers", "/api/v1/ex/pubsub/publishers"}:
            self._send_json({"publishers": self.server.local_plugins.list_publishers()})
            return
        if path in {"/api/connections/types", "/api/v1/ex/connections/types"}:
            self._send_json({"types": self.server.connections.list_types()})
            return
        if path in {"/api/connections", "/api/v1/ex/connections"}:
            self._send_json({"connections": self.server.connections.list()})
            return
        if path in {"/api/connections/business", "/api/v1/ex/connections/business"}:
            probes: dict[str, Any] = {}
            for feature, method in (
                ("text", "transport.status"),
                ("audio", "providers.status"),
                ("vision", "vision.status"),
            ):
                try:
                    payload, _ = self.server.connections.request_feature(
                        feature, method, timeout_sec=3.0
                    )
                    probes[feature] = {"ok": True, "response": payload}
                except Exception as exc:
                    probes[feature] = {"ok": False, "error": str(exc)}
            self._send_json(
                {
                    "business_connections": self.server.connections.business_status(),
                    "probes": probes,
                }
            )
            return
        connection_id = self._match_connection_id(path)
        if connection_id:
            try:
                self._send_json({"connection": self.server.connections.get(connection_id)})
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        if path in {"/api/bridge/context", "/api/v1/ex/bridge/context", "/api/v1/ex/llm/context"}:
            self._send_json(self.server.bridge.build_context())
            return
        if path in {"/api/bridge/actions", "/api/v1/ex/bridge/actions", "/api/v1/ex/llm/actions"}:
            self._send_json({"actions": [action.to_dict() for action in self.server.bridge.list_actions()]})
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
        dashboard_id = self._match_plugin_dashboard(path)
        if dashboard_id:
            self._send_plugin_dashboard(dashboard_id)
            return
        if path in {"/api/v1/ex/interaction/status"}:
            self._send_json(self._interaction_status())
            return
        if path == "/healthz":
            self._send_json({"ok": True})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _environment_api(self, path: str, method: str) -> bool:
        manager = getattr(self.server, "environment_manager", None)
        if manager is None:
            return False
        prefix = "/api/v1/ex/environments"
        plugin_prefix = "/api/v1/ex/plugins/"
        plugin_ros = path.startswith(plugin_prefix) and path.endswith("/ros2")
        if not (path.startswith(prefix) or path in {"/api/environments", "/api/environments/select"} or plugin_ros):
            return False
        try:
            status = HTTPStatus.OK
            if method == "GET":
                if plugin_ros:
                    result = {"ok": True, "ros2": self.server.local_plugins.get_ros2(unquote(path[len(plugin_prefix):-5])),
                              "environment": manager.snapshot()}
                elif path in {prefix, "/api/environments", prefix + "/ros2/status"}:
                    result = manager.status()
                elif path == prefix + "/ros2/graph":
                    result = manager.graph()
                elif path == prefix + "/ros2/endpoints":
                    result = manager.endpoints()
                elif path == prefix + "/ros2/interfaces":
                    result = manager.interfaces()
                elif path.startswith(prefix + "/operations/"):
                    operation = manager.get_operation(unquote(path.rsplit("/", 1)[-1]))
                    if operation is None: raise KeyError("unknown environment operation")
                    result = {"ok": True, "operation": operation}
                else:
                    return False
            else:
                payload = self._read_json()
                if not isinstance(payload, dict): raise ValueError("request body must be an object")
                if plugin_ros:
                    manager._check_revision(expected_session=payload.get("expected_session"))
                    result = self.server.local_plugins.update_ros2(unquote(path[len(plugin_prefix):-5]),
                                                                   payload.get("bindings"), payload.get("expected_revision"))
                elif path in {prefix + "/select", "/api/environments/select"}:
                    result = manager.select(payload.get("mode"), expected_revision=payload.get("expected_revision"),
                                            expected_session=payload.get("expected_session"))
                    if result.get("accepted"): status = HTTPStatus.ACCEPTED
                elif path == prefix + "/ros2/config":
                    config = payload.get("config", {k: v for k, v in payload.items() if k not in ("expected_revision", "expected_session")})
                    result = manager.configure_ros2(config, expected_revision=payload.get("expected_revision"),
                                                    expected_session=payload.get("expected_session"))
                elif path == prefix + "/ros2/discovery/refresh":
                    result, status = manager.refresh(), HTTPStatus.ACCEPTED
                elif path == prefix + "/ros2/interfaces/check":
                    result = manager.check_interface(payload.get("message_type"))
                else:
                    return False
            self._send_json(result, status)
        except (EnvironmentRevisionConflict, EnvironmentBusyError) as exc:
            code = "revision_conflict" if isinstance(exc, EnvironmentRevisionConflict) else "environment_busy"
            self._send_json({"ok": False, "code": code, "message": str(exc), "error": str(exc), "details": manager.snapshot()}, HTTPStatus.CONFLICT)
        except KeyError as exc:
            self._send_json({"ok": False, "code": "not_found", "message": str(exc), "error": str(exc), "details": {}}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"ok": False, "code": "invalid_environment_request", "message": str(exc), "error": str(exc), "details": {}}, HTTPStatus.BAD_REQUEST)
        return True

    def do_POST(self) -> None:
        if not self._authorize_http():
            return
        if self.server.decision_management.handle_http(self):
            return
        self.server.decision_management.invalidate("legacy_write")
        path = self._path()
        if path == "/api/v1/ex/backups":
            self._create_backup()
            return
        if path == "/api/v1/ex/backups/upload":
            self._handle_backup_upload()
            return
        if self._environment_api(path, "POST"):
            return
        if path == "/api/v1/ex/runtime/control-mode":
            try:
                self.controller.change_mode(self._read_json().get("control_mode"))
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            except RuntimeError as exc:
                self._send_json({"ok": False, "error": str(exc), "actions": self.controller.status()["actions"]},
                                HTTPStatus.CONFLICT)
                return
            self._send_json({"ok": True, "control_mode": self.controller.runtime.action_service.control_mode})
            return
        if path == "/api/runtime/start" or path == "/api/v1/ex/runtime/start":
            try:
                self.controller.start()
            except Exception as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "state": self.controller.runtime.state.value},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._send_json({"ok": True, "state": self.controller.runtime.state.value})
            return
        if path == "/api/runtime/stop" or path == "/api/v1/ex/runtime/stop":
            payload = self._read_json()
            reason = str(payload.get("reason", "stopped by api"))
            self.controller.stop(reason)
            actions = self.controller.status()["actions"]
            self._send_json({"ok": not actions["blocked"], "state": self.controller.runtime.state.value,
                             "actions": actions}, HTTPStatus.CONFLICT if actions["blocked"] else HTTPStatus.OK)
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
        if path in {"/api/v1/ex/bridge/action/start", "/api/v1/ex/actions/start"}:
            result = self.server.bridge.direct_action_start(self._read_json())
            self._send_json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return
        if path in {"/api/v1/ex/bridge/action/query", "/api/v1/ex/actions/query"}:
            payload = self._read_json()
            result = self.server.bridge.direct_action_query(str(payload.get("command_id", "")))
            self._send_json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return
        if path in {"/api/v1/ex/bridge/action/cancel", "/api/v1/ex/actions/cancel"}:
            payload = self._read_json()
            self._send_json({"ok": False, "error": "cancel requires trusted owner binding"}, HTTPStatus.BAD_REQUEST)
            return
        if path in {"/api/bridge/proposal", "/api/v1/ex/bridge/proposal", "/api/v1/ex/llm/proposal"}:
            result = self.server.bridge.handle_proposal(self._read_json())
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
            self._send_json(result, status)
            return
        if path == "/api/v1/ex/interaction/message":
            payload = self._read_json()
            text = str(payload.get("text", "")).strip()
            if not text:
                self._send_json(
                    {"ok": False, "error": "text is required"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            result = self.server.interaction_core.send_text(
                text,
                source=str(payload.get("source", "api")),
            )
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_GATEWAY
            self._send_json(result, status)
            return
        if path == "/api/v1/ex/interaction/stt":
            payload = self._read_json()
            audio_url = self.server.interaction_core._resolve_audio_url(payload)
            if audio_url is None:
                self._send_json(
                    {"ok": False, "error": "audio_url or audio data is required"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            result = self.server.interaction_core.transcribe_audio(
                audio_url,
                source="api",
                delete_after_read=str(payload.get("audio_url", "")).startswith("data:"),
            )
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_GATEWAY
            self._send_json(result, status)
            return
        if path == "/api/v1/ex/interaction/tts":
            payload = self._read_json()
            text = str(payload.get("text", "")).strip()
            if not text:
                self._send_json(
                    {"ok": False, "error": "text is required"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            result = self.server.interaction_core.synthesize_text(text, source="api")
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_GATEWAY
            self._send_json(result, status)
            return
        if path == "/api/v1/ex/interaction/reply":
            payload = self._read_json()
            self.server.interaction_core.handle_astrbot_reply(payload)
            self._send_json({"ok": True})
            return
        if path in {"/api/vision/publish", "/api/v1/ex/vision/publish"}:
            payload = self._read_json()
            binary = None
            encoded = payload.pop("data_base64", None)
            if encoded is not None:
                try:
                    binary = base64.b64decode(str(encoded), validate=True)
                except ValueError:
                    self._send_json({"ok": False, "error": "data_base64 must be valid base64"}, HTTPStatus.BAD_REQUEST)
                    return
            try:
                result, _ = self.server.connections.request_feature(
                    "vision", "vision.publish", payload, binary=binary, timeout_sec=10.0
                )
            except (RuntimeError, TimeoutError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)
                return
            self._send_json(result)
            return
        if path in {"/api/connections", "/api/v1/ex/connections"}:
            try:
                connection = self.server.connections.create(self._read_json())
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "connection": connection}, HTTPStatus.CREATED)
            return
        connection_action = self._match_connection_action(path)
        if connection_action:
            connection_id, action = connection_action
            try:
                if action == "start":
                    connection = self.server.connections.start(connection_id)
                    self._send_json({"ok": True, "connection": connection})
                elif action == "stop":
                    connection = self.server.connections.stop(connection_id)
                    self._send_json({"ok": True, "connection": connection})
                else:
                    payload = self._read_json()
                    result = self.server.connections.send(
                        connection_id,
                        payload.get("data", ""),
                        peer=str(payload.get("peer") or "") or None,
                    )
                    self._send_json(result)
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (RuntimeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        plugin_id = self._match_plugin_action(path, "enable")
        if plugin_id:
            try:
                plugin = self.server.local_plugins.set_enabled(plugin_id, True)
            except (KeyError, ValueError) as exc:
                status = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            self.server.interaction_core.refresh_mic_subscriptions()
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
            except (KeyError, ValueError) as exc:
                status = HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            self.server.interaction_core.refresh_mic_subscriptions()
            self._send_json({"ok": True, "plugin": plugin})
            return
        plugin_id = self._match_plugin_action(path, "config")
        if plugin_id:
            payload = self._read_json()
            try:
                plugin = self.server.local_plugins.update_config(plugin_id, payload)
            except (KeyError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self.server.interaction_core.refresh_mic_subscriptions()
            self._send_json({"ok": True, "plugin": plugin})
            return
        plugin_id = self._match_plugin_action(path, "pubsub")
        if plugin_id:
            payload = self._read_json()
            try:
                plugin = self.server.local_plugins.update_pubsub(plugin_id, payload)
            except (KeyError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "plugin": plugin})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        if not self._authorize_http():
            return
        if self.server.decision_management.handle_http(self):
            return
        self.server.decision_management.invalidate("legacy_write")
        path = self._path()
        connection_id = self._match_connection_id(path)
        if connection_id:
            try:
                connection = self.server.connections.update(connection_id, self._read_json())
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "connection": connection})
            return
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
        if not self._authorize_http():
            return
        if self.server.decision_management.handle_http(self):
            return
        self.server.decision_management.invalidate("legacy_write")
        path = self._path()
        connection_id = self._match_connection_id(path)
        if connection_id:
            try:
                self.server.connections.delete(connection_id)
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "connection_id": connection_id})
            return
        source_id = self._match_source_id(path)
        if source_id:
            try:
                self.server.vision_sources.delete(source_id)
            except KeyError:
                self._send_json({"ok": False, "error": f"unknown source: {source_id}"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True})
            return
        plugin_id = self._match_plugin_id(path)
        if plugin_id:
            try:
                plugin = self.server.local_plugins.get_plugin(plugin_id)
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            if any(cap in plugin.get("provides", []) for cap in ("motion_bridge", "transport", "protocol_codec", "skill_plugin", "vision_provider")):
                self.controller.stop(f"plugin uninstalled: {plugin_id}")
            try:
                self.server.local_plugins.uninstall(plugin_id)
            except (KeyError, OSError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "plugin_id": plugin_id})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _authorize_http(self) -> bool:
        management = getattr(self.server, "decision_management", None)
        if management is None:
            self._send_json({"ok": False, "code": "management_unavailable",
                             "message": "management access is unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return False
        return management.authorize(self)

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
    def _match_backup_filename(path: str) -> str | None:
        prefix = "/api/v1/ex/backups/"
        if not path.startswith(prefix):
            return None
        filename = unquote(path.removeprefix(prefix).strip("/"))
        return filename if filename else None

    @staticmethod
    def _match_connection_id(path: str) -> str | None:
        for prefix in ("/api/connections/", "/api/v1/ex/connections/"):
            if path.startswith(prefix):
                tail = path.removeprefix(prefix).strip("/")
                if tail and "/" not in tail and tail != "types":
                    return unquote(tail)
        return None

    @staticmethod
    def _match_connection_action(path: str) -> tuple[str, str] | None:
        for action in ("start", "stop", "send"):
            suffix = f"/{action}"
            for prefix in ("/api/connections/", "/api/v1/ex/connections/"):
                if path.startswith(prefix) and path.endswith(suffix):
                    connection_id = path.removeprefix(prefix).removesuffix(suffix).strip("/")
                    if connection_id and "/" not in connection_id:
                        return unquote(connection_id), action
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

    @staticmethod
    def _match_plugin_dashboard(path: str) -> str | None:
        suffix = "/dashboard"
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

    def _create_backup(self) -> None:
        try:
            self._send_json({"ok": True, "backup": self.server.snapshot_service.create()})
        except SnapshotError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _send_backup_download(self, filename: str) -> None:
        try:
            path = self.server.snapshot_service.download_path(filename)
        except SnapshotError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, length=1024 * 1024)

    def _handle_backup_upload(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            self._send_json({"ok": False, "error": "missing request body"}, HTTPStatus.BAD_REQUEST)
            return
        if content_length > self.server.snapshot_service.limits.max_upload_bytes + 1024 * 1024:
            self._send_json(
                {"ok": False, "error": "uploaded request exceeds the size limit"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        if not self.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
            self._send_json({"ok": False, "error": "multipart/form-data required"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            form = self._parse_multipart_form_data()
            upload = form.get("file")
            if not upload or not upload.get("filename"):
                raise SnapshotError("missing snapshot ZIP file")
            result = self.server.snapshot_service.restore_upload(
                str(upload["filename"]),
                bytes(upload.get("content") or b""),
            )
            result["ok"] = True
            result["runtime_state"] = self.controller.runtime.state.value
            result["restart_recommended"] = True
            self._send_json(result)
        except SnapshotError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        management = getattr(self.server, "decision_management", None)
        if management is not None:
            payload = management.sanitize(to_jsonable(payload))
        body = json.dumps(to_jsonable(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _interaction_status(self) -> dict[str, Any]:
        ic = self.server.interaction_core
        connection_manager = getattr(self.server, "connections", None)
        business = connection_manager.business_status() if connection_manager is not None else {
            feature: {"connection_id": None, "configured": False, "ready": False, "error": None}
            for feature in ("text", "audio", "vision")
        }
        if business["audio"]["ready"]:
            try:
                provider_status, _ = connection_manager.request_feature(
                    "audio", "providers.status", timeout_sec=2.0
                )
                astrbot_reachable = True
                astrbot_error = None
            except Exception as exc:
                provider_status = {}
                astrbot_reachable = False
                astrbot_error = str(exc)
            slots = self.server.controller.runtime.registry.list()
            mic_plugins = [slot.plugin.id for slot in slots if slot.kind == "mic" and slot.enabled]
            speaker_plugins = [slot.plugin.id for slot in slots if slot.kind == "speaker" and slot.enabled]
            status = {
                "transport": "zeromq",
                "business_connections": business,
                "astrbot_base_url": ic.astrbot_base_url,
                "astrbot_reachable": astrbot_reachable,
                "astrbot_error": astrbot_error,
                "stt_provider": provider_status.get("stt"),
                "tts_provider": provider_status.get("tts"),
                "stt_proxy": type(ic.stt_provider).__name__ if ic.stt_provider is not None else None,
                "tts_proxy": type(ic.tts_provider).__name__ if ic.tts_provider is not None else None,
                "stt_ready": bool(astrbot_reachable and provider_status.get("stt") and ic.stt_provider),
                "tts_ready": bool(astrbot_reachable and provider_status.get("tts") and ic.tts_provider),
                "mic_plugins": mic_plugins,
                "speaker_plugins": speaker_plugins,
            }
            status.update(ic.status_snapshot())
            return status
        astrbot_reachable = False
        provider_status: dict[str, Any] = {}
        astrbot_error: str | None = None
        try:
            url = ic.astrbot_base_url.rstrip("/") + "/api/v1/ex/interaction/providers"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                astrbot_reachable = resp.status == 200
                if astrbot_reachable:
                    parsed = json.loads(resp.read().decode("utf-8"))
                    if isinstance(parsed, dict):
                        provider_status = parsed
        except Exception as exc:
            astrbot_error = str(exc)

        slots = self.server.controller.runtime.registry.list()
        mic_plugins = [slot.plugin.id for slot in slots if slot.kind == "mic" and slot.enabled]
        speaker_plugins = [slot.plugin.id for slot in slots if slot.kind == "speaker" and slot.enabled]
        status = {
            "transport": "http-compat",
            "business_connections": business,
            "astrbot_base_url": ic.astrbot_base_url,
            "astrbot_reachable": astrbot_reachable,
            "astrbot_error": astrbot_error,
            "stt_provider": provider_status.get("stt"),
            "tts_provider": provider_status.get("tts"),
            "stt_proxy": type(ic.stt_provider).__name__ if ic.stt_provider is not None else None,
            "tts_proxy": type(ic.tts_provider).__name__ if ic.tts_provider is not None else None,
            "stt_ready": bool(astrbot_reachable and provider_status.get("stt") and ic.stt_provider),
            "tts_ready": bool(astrbot_reachable and provider_status.get("tts") and ic.tts_provider),
            "mic_plugins": mic_plugins,
            "speaker_plugins": speaker_plugins,
        }
        status.update(ic.status_snapshot())
        return status

    def _handle_plugin_upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self._send_json({"ok": False, "error": "multipart/form-data required"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            form = self._parse_multipart_form_data()
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        upload = form.get("file")
        if not upload or not upload.get("filename"):
            self._send_json({"ok": False, "error": "missing plugin zip file"}, HTTPStatus.BAD_REQUEST)
            return
        filename = str(upload["filename"])
        if not filename.lower().endswith(".zip"):
            self._send_json({"ok": False, "error": "only .zip plugin packages are supported"}, HTTPStatus.BAD_REQUEST)
            return
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            tmp.write(upload["content"])
            temp_path = Path(tmp.name)
        try:
            category = form.get("category", {}).get("text")
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

    def _send_plugin_dashboard(self, plugin_id: str) -> None:
        try:
            plugin = self.server.local_plugins.get_plugin(plugin_id)
            record = self.server.local_plugins.records[plugin["id"]]
            dashboard = record.manifest.dashboard
            if not dashboard:
                self._send_json({"ok": False, "error": "dashboard not configured"}, HTTPStatus.NOT_FOUND)
                return
            target = (record.root / dashboard).resolve()
            if record.root.resolve() not in target.parents and target != record.root.resolve():
                self._send_json({"ok": False, "error": "invalid dashboard path"}, HTTPStatus.BAD_REQUEST)
                return
            body = target.read_bytes()
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
        elif path in {"/index.html", "/styles.css", "/app.js", "/environments.js"}:
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
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
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

    def _parse_multipart_form_data(self) -> dict[str, dict[str, Any]]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("missing request body")

        raw_body = self.rfile.read(length)
        header_block = f"Content-Type: {self.headers.get('Content-Type', '')}\r\nMIME-Version: 1.0\r\n\r\n"
        message = BytesParser(policy=email_policy).parsebytes(header_block.encode("utf-8") + raw_body)
        if not message.is_multipart():
            raise ValueError("invalid multipart/form-data body")

        fields: dict[str, dict[str, Any]] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            entry: dict[str, Any] = {"content": payload}
            filename = part.get_filename()
            if filename:
                entry["filename"] = filename
            else:
                entry["text"] = payload.decode("utf-8").strip()
            fields[name] = entry
        return fields

    def _write_sse(self, event_name: str, payload: Any) -> None:
        management = getattr(self.server, "decision_management", None)
        if management is not None:
            payload = management.sse_payload(payload)
        data = json.dumps(to_jsonable(payload), ensure_ascii=False)
        self._write_raw(f"event: {event_name}\ndata: {data}\n\n")

    def _write_raw(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()


class RuntimeCapabilityCatalog(CapabilityCatalog):
    def __init__(self) -> None:
        super().__init__()
        self.on_change = None

    def refresh(self, records):
        before = self.snapshot().revision
        snapshot = super().refresh(records)
        if snapshot.revision != before and self.on_change is not None:
            self.on_change()
        return snapshot


class AstrBotEXHTTPServer(ThreadingHTTPServer):
    controller: RuntimeController
    static_root: Path
    vision_sources: VisionSourceManager
    local_plugins: LocalPluginManager
    bridge: AstrBotBridge
    interaction_core: InteractionCore
    connections: ConnectionManager
    snapshot_service: SnapshotService
    environment_manager: EnvironmentManager
    action_service: ActionService
    decision_management: DecisionManagement

    def server_close(self) -> None:
        errors = []
        if hasattr(self, "decision_management"):
            try:
                self.decision_management.close()
            except Exception as exc:
                errors.append(exc)
        try:
            if hasattr(self, "controller"):
                self.controller.stop("api server shutdown")
            if hasattr(self, "environment_manager"):
                self.environment_manager.close("api server shutdown")
        except Exception as exc:
            # Keep the old adapter and action reporting alive when stop is unproven.
            super().server_close()
            if errors:
                raise ShutdownErrors("API server shutdown failed", [*errors, exc]) from None
            raise
        for name in ("task_public_delivery", "decision_controller"):
            try:
                if hasattr(self, name):
                    getattr(self, name).close()
            except Exception as exc:
                errors.append(exc)
        try:
            try:
                if hasattr(self, "connections"):
                    self.connections.close()
            except Exception as exc:
                errors.append(exc)
            finally:
                try:
                    if hasattr(self, "decision_service"):
                        self.decision_service.close()
                except Exception as exc:
                    errors.append(exc)
                finally:
                    try:
                        if hasattr(self, "action_service"):
                            self.action_service.close()
                    except Exception as exc:
                        errors.append(exc)
        finally:
            try:
                super().server_close()
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ShutdownErrors("API server shutdown failed", errors)


def build_server(host: str, port: int, tick_hz: float, *,
                 management_settings: ManagementSettings | None = None) -> AstrBotEXHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("management HTTP requires a loopback binding")
    project_root = Path(__file__).resolve().parents[2]
    data_dir = os.environ.get("ASTRBOTEX_DATA_DIR")
    data_root = Path(data_dir).resolve() if data_dir else project_root
    event_bus = EventBus()
    topic_bus = TopicBus()
    ledger = ActionLedger(prepare_action_ledger(data_root))
    try:
        dispatcher = ActionDispatcher(ledger)
    except BaseException:
        ledger.close()
        raise
    catalog = RuntimeCapabilityCatalog()
    action_service = ActionService(ledger, dispatcher, catalog)
    catalog.on_change = action_service.update_versions
    environment_manager = EnvironmentManager(
        data_root=data_root,
        event_bus=event_bus,
        topic_bus=topic_bus,
    )
    environment_manager.action_service = action_service
    decision_service = DecisionService(action_service, topic_bus=topic_bus, environment=environment_manager)
    environment_manager.decision_service = decision_service
    try:
        decision_management = DecisionManagement(decision_service, data_root, event_bus=event_bus,
                                                settings=management_settings)
    except BaseException:
        try:
            decision_service.close()
        finally:
            action_service.close()
        raise
    fusion = None
    try:
        perception_config = load_perception_config(data_root / "profiles" / "default" / "perception.json")
    except ValueError as exc:
        event_bus.emit(
            "perception",
            "perception config invalid, fusion disabled",
            severity="error",
            error=str(exc),
        )
    else:
        fusion = SceneFusion(perception_config)
    runtime = AstrBotEXRuntime(
        registry=PluginRegistry(),
        event_bus=event_bus,
        topic_bus=topic_bus,
        fusion=fusion,
        action_service=action_service,
        decision_service=decision_service,
    )
    def prove_decision_owner_stop(slot, reason):
        decision_service.request_stop(reason)
        return action_service.prove_owner_stop(slot, reason)

    runtime.registry.set_action_lifecycle_guard(prove_decision_owner_stop)
    _astrbot_base_url = os.environ.get("ASTRBOT_BASE_URL", "http://127.0.0.1:8766")
    _timeout_sec = float(os.environ.get("ASTRBOTEX_TIMEOUT_SEC", "5.0"))
    _capture_tail_sec = float(os.environ.get("ASTRBOTEX_CAPTURE_TAIL_SEC", "0.8"))
    _capture_fallback_sec = float(
        os.environ.get("ASTRBOTEX_CAPTURE_FALLBACK_SEC", "15.0")
    )
    connections = ConnectionManager(
        data_root / "profiles" / "default" / "connections.json",
        event_bus=runtime.event_bus,
        topic_bus=runtime.topic_bus,
    )

    _stt_provider: STTProvider | None = None
    _tts_provider: TTSProvider | None = None
    if os.environ.get("ASTRBOTEX_STT_ENABLED", "").lower() in ("true", "1"):
        _stt_provider = AstrBotSTTProvider(
            astrbot_base_url=_astrbot_base_url,
            timeout_sec=_timeout_sec,
            event_bus=event_bus,
            business_connections=connections,
        )
    if os.environ.get("ASTRBOTEX_TTS_ENABLED", "").lower() in ("true", "1"):
        _tts_provider = AstrBotTTSProvider(
            astrbot_base_url=_astrbot_base_url,
            timeout_sec=float(os.environ.get("ASTRBOTEX_TTS_TIMEOUT_SEC", "30.0")),
            event_bus=event_bus,
            business_connections=connections,
        )

    interaction_core = InteractionCore(
        registry=runtime.registry,
        topic_bus=runtime.topic_bus,
        event_bus=runtime.event_bus,
        astrbot_base_url=_astrbot_base_url,
        session_id=os.environ.get("ASTRBOTEX_SESSION_ID", "astrbotex_default"),
        timeout_sec=_timeout_sec,
        stt_provider=_stt_provider,
        tts_provider=_tts_provider,
        business_connections=connections,
        capture_tail_sec=_capture_tail_sec,
        capture_fallback_playback_sec=_capture_fallback_sec,
    )
    environment_manager.runtime_running = lambda: runtime.state.value == 'running'
    runtime.interaction_core = interaction_core
    controller = RuntimeController(runtime=runtime, tick_hz=tick_hz)
    server = AstrBotEXHTTPServer((host, port), AstrBotEXRequestHandler)
    server.controller = controller
    decision_management.attach_runtime(controller, connections)
    server.action_service = action_service
    server.decision_service = decision_service
    server.decision_management = decision_management
    server.action_dispatcher = dispatcher
    server.action_ledger = ledger
    server.capability_catalog = catalog
    server.interaction_core = interaction_core
    server.environment_manager = environment_manager
    server.static_root = (project_root / "dashboard").resolve()
    server.vision_sources = VisionSourceManager(data_root / "profiles" / "default" / "vision_sources.json")
    server.local_plugins = LocalPluginManager(
        plugins_root=data_root / "plugins",
        state_path=data_root / "profiles" / "default" / "plugins_state.json",
        registry=runtime.registry,
        event_bus=runtime.event_bus,
        topic_bus=runtime.topic_bus,
        environment_manager=environment_manager,
        action_dispatcher=dispatcher,
        capability_catalog=catalog,
    )
    server.local_plugins.discover()
    server.local_plugins.load_enabled()
    action_service.update_versions()
    server.bridge = AstrBotBridge(
        controller=controller,
        local_plugins=server.local_plugins,
        event_bus=runtime.event_bus,
        topic_bus=runtime.topic_bus,
    )
    server.connections = connections

    def reload_instance_data() -> None:
        """Reload all in-memory state that is persisted by an instance snapshot."""
        perception_config = load_perception_config(data_root / "profiles" / "default" / "perception.json")
        runtime.fusion = SceneFusion(perception_config)
        runtime.perception_core.fusion = runtime.fusion
        server.vision_sources.load()
        server.connections.reload()
        environment_manager.reload()
        server.local_plugins.discover()
        server.local_plugins.load_enabled()
        action_service.update_versions()
        interaction_core.refresh_mic_subscriptions()
        environment_manager.restore_selected_mode()
        decision_management.after_restore()

    def before_snapshot_restore() -> None:
        try:
            decision_management.before_restore()
        except ManagementError as exc:
            # A superseded stop receipt cannot prove this restore safe either.
            if exc.code not in {"stop_not_proven", "stop_operation_superseded"}:
                raise
            raise SnapshotError(action_service.status()["error"] or "stop proof pending") from exc
        controller.stop("instance snapshot restore")
        if not action_service.stop_actions("instance snapshot restore"):
            raise SnapshotError(action_service.status()["error"] or "action stop not proven")
        unload_errors: list[str] = []
        for slot in runtime.registry.list():
            try:
                runtime.registry.unregister(slot.id)
            except Exception as exc:
                unload_errors.append(f"{slot.id}: {exc}")
        environment_manager.reset_after_restore()
        connections.close()
        if unload_errors:
            raise RuntimeError(f"plugin unload failed: {'; '.join(unload_errors)}")

    server.snapshot_service = SnapshotService(
        data_root,
        before_restore=before_snapshot_restore,
        after_restore=reload_instance_data,
        after_rollback=reload_instance_data,
    )
    def handle_zmq_business(
        feature: str,
        method: str,
        payload: dict[str, Any],
        binary: bytes | None,
    ) -> tuple[dict[str, Any], bytes | None]:
        del binary
        if feature == "text" and method == "interaction.reply":
            interaction_core.handle_astrbot_reply(payload)
            return {"ok": True}, None
        if feature == "text" and method == "bridge.context.get":
            return server.bridge.build_context(), None
        if feature == "text" and method == "bridge.proposal.submit":
            return server.bridge.handle_proposal(payload), None
        if feature == "text" and method == "runtime.status":
            status = controller.status()
            status.update(
                {
                    "ok": True,
                    "text_peers": int(connections.business_status()["text"]["ready"]),
                    "vision_streams": 0,
                }
            )
            return status, None
        return {"ok": False, "error": f"unsupported {feature} method: {method}"}, None

    connections.set_business_handler(handle_zmq_business)
    try:
        server.decision_controller = DecisionController(decision_service, connections, interaction_core, data_root)
        server.task_public_delivery = TaskPublicDelivery(server.decision_controller, interaction_core)
        connections.set_decision_handler(server.decision_controller.handle,
            public_validator=server.decision_controller.validate_public,
            public_handler=server.task_public_delivery.enqueue)
        connections.set_task_turn_handler(server.decision_controller.turn)
        interaction_core.task_authority_invalidator = server.decision_controller.invalidate_local
    except BaseException:
        server.server_close()
        raise
    server.connections.start_enabled()
    selected_mode = environment_manager.status()["config"]["selected_mode"]
    if selected_mode != "normal":
        environment_manager.select(selected_mode)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AstrBotEX local API server.")
    parser.add_argument("--host", default=os.environ.get("ASTRBOTEX_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ASTRBOTEX_PORT", "8765")))
    parser.add_argument("--tick-hz", type=float, default=float(os.environ.get("ASTRBOTEX_TICK_HZ", "20")))
    args = parser.parse_args()

    server = build_server(args.host, args.port, args.tick_hz)
    print(f"Management credential file: {server.decision_management.credential_path}")
    print(f"AstrBotEX API listening on http://{args.host}:{args.port}")
    print(f"Dashboard: http://{args.host}:{args.port}/")
    print("Core endpoints: /api/status, /api/events, /api/runtime/start, /api/runtime/stop")
    print("Vision endpoints: /api/v1/ex/vision/sources, /api/v1/ex/vision/latest")
    print("Bridge endpoints: /api/v1/ex/bridge/context, /api/v1/ex/bridge/proposal")
    print("Interaction endpoints: /api/v1/ex/interaction/reply")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping AstrBotEX API server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
