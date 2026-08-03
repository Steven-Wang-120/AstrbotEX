from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

import astrbot_ex.core.api_server as api_server
from astrbot_ex.core.api_server import AstrBotEXRequestHandler, build_server


class FakeProvider:
    pass


class FakePlugin:
    def __init__(self, plugin_id: str) -> None:
        self.id = plugin_id


class FakeSlot:
    def __init__(self, kind: str, plugin_id: str, enabled: bool) -> None:
        self.kind = kind
        self.plugin = FakePlugin(plugin_id)
        self.enabled = enabled


class FakeRegistry:
    def __init__(self, slots: list[FakeSlot]) -> None:
        self._slots = slots

    def list(self) -> list[FakeSlot]:
        return self._slots


class FakeRuntime:
    def __init__(self, registry: FakeRegistry) -> None:
        self.registry = registry


class FakeController:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime


class FakeInteractionCore:
    def __init__(self) -> None:
        self.astrbot_base_url = "http://127.0.0.1:8766"
        self.stt_provider = FakeProvider()
        self.tts_provider = None

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "last_incoming_text_at": None,
            "last_stt_at": None,
            "last_astrbot_reply_at": None,
            "last_tts_audio_at": None,
            "last_error": None,
        }


class FakeServer:
    def __init__(self, slots: list[FakeSlot]) -> None:
        self.interaction_core = FakeInteractionCore()
        self.controller = FakeController(FakeRuntime(FakeRegistry(slots)))


class FakeResponse:
    def __init__(self, status: int, payload: dict[str, Any] | None = None) -> None:
        self.status = status
        self._payload = payload or {"ok": True, "stt": "AstrBotSTT", "tts": None}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def read(self) -> bytes:
        return api_server.json.dumps(self._payload).encode("utf-8")


class ApiServerBuildTest(unittest.TestCase):
    def test_build_server_wires_interaction_core_after_server_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                "ASTRBOTEX_DATA_DIR": temp_dir,
                "ASTRBOTEX_STT_ENABLED": "",
                "ASTRBOTEX_TTS_ENABLED": "",
            }
            with unittest.mock.patch.dict(os.environ, environment):
                server = build_server("127.0.0.1", 0, 20)
            try:
                self.assertIsNotNone(server.interaction_core)
                self.assertIs(server.interaction_core.registry, server.controller.runtime.registry)
                self.assertIs(server.controller.runtime.interaction_core, server.interaction_core)
            finally:
                server.server_close()


class ApiServerInteractionStatusTest(unittest.TestCase):
    def test_interaction_status_reports_voice_chain_state(self) -> None:
        handler = object.__new__(AstrBotEXRequestHandler)
        handler.server = FakeServer(
            [
                FakeSlot("mic", "mic_enabled", True),
                FakeSlot("mic", "mic_disabled", False),
                FakeSlot("speaker", "speaker_enabled", True),
                FakeSlot("vision", "camera", True),
            ]
        )
        calls: list[tuple[str, float, str]] = []
        original_urlopen = api_server.urllib.request.urlopen

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            calls.append((req.full_url, timeout, req.get_method()))
            return FakeResponse(200)

        try:
            api_server.urllib.request.urlopen = fake_urlopen
            status = handler._interaction_status()
        finally:
            api_server.urllib.request.urlopen = original_urlopen

        self.assertEqual(
            calls,
            [("http://127.0.0.1:8766/api/v1/ex/interaction/providers", 2, "GET")],
        )
        self.assertEqual(status["astrbot_base_url"], "http://127.0.0.1:8766")
        self.assertTrue(status["astrbot_reachable"])
        self.assertEqual(status["stt_provider"], "AstrBotSTT")
        self.assertIsNone(status["tts_provider"])
        self.assertEqual(status["stt_proxy"], "FakeProvider")
        self.assertIsNone(status["tts_proxy"])
        self.assertTrue(status["stt_ready"])
        self.assertFalse(status["tts_ready"])
        self.assertEqual(status["mic_plugins"], ["mic_enabled"])
        self.assertEqual(status["speaker_plugins"], ["speaker_enabled"])
        self.assertIsNone(status["last_incoming_text_at"])
        self.assertIsNone(status["last_tts_audio_at"])

    def test_interaction_status_marks_astrbot_unreachable_on_error(self) -> None:
        handler = object.__new__(AstrBotEXRequestHandler)
        handler.server = FakeServer([])
        original_urlopen = api_server.urllib.request.urlopen

        def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
            raise TimeoutError("timed out")

        try:
            api_server.urllib.request.urlopen = fake_urlopen
            status = handler._interaction_status()
        finally:
            api_server.urllib.request.urlopen = original_urlopen

        self.assertFalse(status["astrbot_reachable"])
