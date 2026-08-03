from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from astrbot_ex.core.providers.astrbot_providers import (
    AstrBotSTTProvider,
    AstrBotTTSProvider,
)


def _json_response(payload: dict[str, object]) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


class AstrBotSTTProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = AstrBotSTTProvider(
            astrbot_base_url="http://127.0.0.1:8766",
            timeout_sec=5.0,
        )

    def test_get_text_returns_text_on_success(self) -> None:
        response = _json_response({"ok": True, "text": "hello"})

        with patch("urllib.request.urlopen", return_value=response):
            result = asyncio.run(
                self.provider.get_text("http://example.com/audio.wav")
            )

        self.assertEqual(result, "hello")

    def test_get_text_raises_on_not_ok(self) -> None:
        response = _json_response({"ok": False, "error": "stt failed"})

        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "stt failed"):
                asyncio.run(
                    self.provider.get_text("http://example.com/audio.wav")
                )

    def test_get_text_raises_on_network_error(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            with self.assertRaisesRegex(OSError, "connection refused"):
                asyncio.run(
                    self.provider.get_text("http://example.com/audio.wav")
                )

    def test_get_text_uploads_local_audio_as_base64(self) -> None:
        response = _json_response({"ok": True, "text": "hello"})
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = Path(tmp) / "recording.wav"
            audio_path.write_bytes(b"RIFF")
            with patch("urllib.request.urlopen", return_value=response) as mock_urlopen:
                result = asyncio.run(self.provider.get_text(str(audio_path)))

        payload = json.loads(mock_urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(result, "hello")
        self.assertEqual(payload["filename"], "recording.wav")
        self.assertEqual(base64.b64decode(payload["audio_base64"]), b"RIFF")


class AstrBotTTSProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = AstrBotTTSProvider(
            astrbot_base_url="http://127.0.0.1:8766",
            timeout_sec=10.0,
        )

    def test_get_audio_returns_local_path_directly(self) -> None:
        local_path = "/tmp/fake_audio.wav"
        response = _json_response({"ok": True, "audio_url": local_path})

        with patch("urllib.request.urlopen", return_value=response):
            result = asyncio.run(self.provider.get_audio("test text"))

        self.assertEqual(result, local_path)

    def test_get_audio_downloads_http_url(self) -> None:
        http_url = "http://example.com/audio.wav"
        response = _json_response({"ok": True, "audio_url": http_url})

        with tempfile.TemporaryDirectory() as tmp:
            with patch("urllib.request.urlopen", return_value=response):
                with patch(
                    "urllib.request.urlretrieve",
                    side_effect=lambda url, dest: Path(dest).write_bytes(b"RIFF"),
                ) as mock_retrieve:
                    with patch(
                        "astrbot_ex.core.providers.astrbot_providers.gettempdir",
                        return_value=tmp,
                    ):
                        result = asyncio.run(self.provider.get_audio("test text"))

            result_path = Path(result)
            self.assertEqual(result_path.parent, Path(tmp))
            self.assertEqual(result_path.read_bytes(), b"RIFF")
            mock_retrieve.assert_called_once_with(http_url, result)

    def test_get_audio_writes_base64_response_to_a_local_file(self) -> None:
        response = _json_response(
            {
                "ok": True,
                "audio_base64": base64.b64encode(b"ID3").decode("ascii"),
                "audio_format": "mp3",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch("urllib.request.urlopen", return_value=response):
                with patch(
                    "astrbot_ex.core.providers.astrbot_providers.gettempdir",
                    return_value=tmp,
                ):
                    result = asyncio.run(self.provider.get_audio("test text"))

            result_path = Path(result)
            self.assertEqual(result_path.suffix, ".mp3")
            self.assertEqual(result_path.read_bytes(), b"ID3")

    def test_get_audio_raises_on_not_ok(self) -> None:
        response = _json_response({"ok": False, "error": "tts failed"})

        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "tts failed"):
                asyncio.run(self.provider.get_audio("test text"))


class BuildServerProviderInjectionTest(unittest.TestCase):
    def test_build_server_no_providers_by_default(self) -> None:
        from astrbot_ex.core.api_server import build_server

        with tempfile.TemporaryDirectory() as tmp:
            environment = {
                "ASTRBOTEX_DATA_DIR": tmp,
                "ASTRBOTEX_STT_ENABLED": "",
                "ASTRBOTEX_TTS_ENABLED": "",
            }
            with patch.dict(os.environ, environment):
                server = build_server("127.0.0.1", 0, 20)
            try:
                self.assertIsNone(server.interaction_core.stt_provider)
                self.assertIsNone(server.interaction_core.tts_provider)
            finally:
                server.server_close()

    def test_build_server_creates_providers_when_enabled(self) -> None:
        from astrbot_ex.core.api_server import build_server

        with tempfile.TemporaryDirectory() as tmp:
            environment = {
                "ASTRBOTEX_DATA_DIR": tmp,
                "ASTRBOTEX_STT_ENABLED": "true",
                "ASTRBOTEX_TTS_ENABLED": "1",
            }
            with patch.dict(os.environ, environment):
                server = build_server("127.0.0.1", 0, 20)
            try:
                self.assertIsInstance(
                    server.interaction_core.stt_provider,
                    AstrBotSTTProvider,
                )
                self.assertIsInstance(
                    server.interaction_core.tts_provider,
                    AstrBotTTSProvider,
                )
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
