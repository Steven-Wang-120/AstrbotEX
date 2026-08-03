from __future__ import annotations

import json
import time
import unittest
from unittest.mock import MagicMock, patch

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.interaction_core import InteractionCore
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.topic_bus import TopicBus, TopicMessage


class FakeMicPlugin:
    id = "test_mic"
    name = "Test Mic"


class FakeSTTProvider:
    async def get_text(self, audio_url: str) -> str:
        return "hello from stt"


class FakeTTSProvider:
    async def get_audio(self, text: str) -> str:
        return "/tmp/fake_audio.wav"


class InteractionCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = PluginRegistry()
        self.topic_bus = TopicBus()
        self.event_bus = EventBus()
        self.core = InteractionCore(
            registry=self.registry,
            topic_bus=self.topic_bus,
            event_bus=self.event_bus,
            astrbot_base_url="http://127.0.0.1:8766",
            session_id="test_session",
        )

    def test_subscribes_to_mic_topics_after_runtime_start(self) -> None:
        mic_plugin = FakeMicPlugin()
        self.registry.register("mic", mic_plugin, enabled=True)

        self.core.on_runtime_start()

        self.topic_bus.publish_payload(
            "test_mic.audio.text",
            timestamp=time.time(),
            source="test_mic",
            payload={"text": "hello"},
        )
        latest = self.topic_bus.get_latest("interaction_core.message.incoming")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.payload["text"], "hello")

        self.core.on_runtime_stop("test")

    def test_handle_mic_text_publishes_incoming_and_calls_send_text(self) -> None:
        with patch.object(self.core, "send_text") as mock_send:
            message = TopicMessage(
                topic="mic.audio.text",
                timestamp=time.time(),
                source="mic_plugin",
                payload={"text": "hello world"},
            )
            self.core._handle_mic_text(message)

            latest = self.topic_bus.get_latest("interaction_core.message.incoming")
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.payload["text"], "hello world")
            mock_send.assert_called_once_with("hello world", source="mic_plugin")

    def test_publish_audio_publishes_to_interaction_core_audio_play(self) -> None:
        self.core.publish_audio("/tmp/test.wav", format="wav")

        latest = self.topic_bus.get_latest("interaction_core.audio.play")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.payload["format"], "wav")
        self.assertEqual(latest.payload["data"], "/tmp/test.wav")
        self.assertFalse(latest.payload["delete_after_play"])

    def test_send_text_posts_correct_json_and_returns_parsed_response(self) -> None:
        expected_response = {"ok": True, "message_id": "msg_123"}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(expected_response).encode("utf-8")
        mock_response.__enter__.return_value = mock_response

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            with patch("urllib.request.Request") as mock_request:
                mock_request_instance = MagicMock()
                mock_request.return_value = mock_request_instance
                result = self.core.send_text("hello", source="test")

                mock_urlopen.assert_called_once()
                self.assertEqual(result, expected_response)

    def test_send_text_emits_event_on_failure(self) -> None:
        delivered = []
        self.event_bus.subscribe(delivered.append)

        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            result = self.core.send_text("hello", source="test")

        self.assertFalse(result["ok"])
        self.assertIn("connection refused", result["error"])

    def test_handle_mic_raw_with_stt_provider(self) -> None:
        self.core.stt_provider = FakeSTTProvider()
        with patch.object(self.core, "send_text") as mock_send:
            message = TopicMessage(
                topic="mic.audio.raw",
                timestamp=time.time(),
                source="mic_plugin",
                payload={"audio_url": "http://example.com/audio.wav"},
            )
            self.core._handle_mic_raw(message)

            mock_send.assert_called_once()
            args = mock_send.call_args
            self.assertEqual(args[0][0], "hello from stt")

    def test_handle_mic_raw_without_stt_provider_emits_warning(self) -> None:
        message = TopicMessage(
            topic="mic.audio.raw",
            timestamp=time.time(),
            source="mic_plugin",
            payload={"audio_url": "http://example.com/audio.wav"},
        )
        self.core._handle_mic_raw(message)

        recent = self.event_bus.recent(10)
        self.assertTrue(
            any(
                "stt provider unavailable" in event.message
                for event in recent
            )
        )

    def test_handle_astrbot_reply_with_tts(self) -> None:
        self.core.tts_provider = FakeTTSProvider()
        with patch.object(self.core, "publish_text") as mock_publish_text:
            with patch.object(self.core, "publish_audio") as mock_publish_audio:
                self.core._handle_astrbot_reply({"text": "hello there"})

                mock_publish_text.assert_called_once_with("hello there")
                mock_publish_audio.assert_called_once_with(
                    "/tmp/fake_audio.wav",
                    delete_after_play=True,
                )

    def test_synthesize_text_returns_audio_and_updates_status(self) -> None:
        self.core.tts_provider = FakeTTSProvider()

        result = self.core.synthesize_text("hello")

        self.assertTrue(result["ok"])
        self.assertEqual(result["audio_url"], "/tmp/fake_audio.wav")
        self.assertIsNotNone(self.core.status_snapshot()["last_tts_audio_at"])

    def test_transcribe_audio_returns_text_and_updates_status(self) -> None:
        self.core.stt_provider = FakeSTTProvider()

        result = self.core.transcribe_audio("http://example.com/audio.wav")

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "hello from stt")
        self.assertIsNotNone(self.core.status_snapshot()["last_stt_at"])

    def test_resolve_audio_url_file_protocol(self) -> None:
        result = self.core._resolve_audio_url({"audio_url": "file:///tmp/test.wav"})
        self.assertEqual(result, "/tmp/test.wav")

    def test_resolve_audio_url_base64(self) -> None:
        import base64
        data = base64.b64encode(b"RIFF").decode("ascii")
        data_uri = f"data:audio/wav;base64,{data}"
        result = self.core._resolve_audio_url({"audio_url": data_uri})
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.endswith(".wav"))

    def test_tick_processes_pending_messages(self) -> None:
        message = TopicMessage(
            topic="mic.audio.text",
            timestamp=time.time(),
            source="mic_plugin",
            payload={"text": "pending message"},
        )
        with self.core._lock:
            self.core._pending_mic_messages.append(message)

        with patch.object(self.core, "send_text") as mock_send:
            self.core.tick()
            mock_send.assert_called_once_with("pending message", source="mic_plugin")


if __name__ == "__main__":
    unittest.main()
