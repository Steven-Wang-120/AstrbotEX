from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from astrbot_ex.core.api_server import (
    AstrBotEXRequestHandler,
    AstrBotEXHTTPServer,
    build_server,
)


class InteractionStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        environment = {
            "ASTRBOTEX_DATA_DIR": self.tmp.name,
            "ASTRBOTEX_STT_ENABLED": "",
            "ASTRBOTEX_TTS_ENABLED": "",
        }
        with patch.dict(os.environ, environment):
            self.server: AstrBotEXHTTPServer = build_server("127.0.0.1", 0, 20)

    def tearDown(self) -> None:
        self.server.server_close()
        self.tmp.cleanup()

    def _make_handler(self) -> AstrBotEXRequestHandler:
        handler = AstrBotEXRequestHandler.__new__(AstrBotEXRequestHandler)
        handler.server = self.server
        return handler

    def test_interaction_status_keys_present(self) -> None:
        handler = self._make_handler()

        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            status = handler._interaction_status()

        self.assertIn("astrbot_reachable", status)
        self.assertIn("stt_provider", status)
        self.assertIn("tts_provider", status)
        self.assertIn("mic_plugins", status)
        self.assertIn("speaker_plugins", status)
        self.assertIn("astrbot_base_url", status)

    def test_interaction_status_unreachable_when_offline(self) -> None:
        handler = self._make_handler()

        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            status = handler._interaction_status()

        self.assertFalse(status["astrbot_reachable"])

    def test_interaction_status_no_providers_by_default(self) -> None:
        handler = self._make_handler()

        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            status = handler._interaction_status()

        self.assertIsNone(status["stt_provider"])
        self.assertIsNone(status["tts_provider"])

    def test_interaction_status_empty_plugin_lists(self) -> None:
        handler = self._make_handler()

        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            status = handler._interaction_status()

        self.assertEqual(status["mic_plugins"], [])
        self.assertEqual(status["speaker_plugins"], [])


if __name__ == "__main__":
    unittest.main()
