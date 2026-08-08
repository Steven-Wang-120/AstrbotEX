from __future__ import annotations

import json
import time
import unittest
from unittest.mock import ANY, MagicMock, patch

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.interaction_core import InteractionCore
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.topic_bus import TopicBus, TopicMessage


class FakeMicPlugin:
    id = "test_mic"
    name = "Test Mic"


class LifecycleMicPlugin:
    id = "lifecycle_mic"
    name = "Lifecycle Mic"

    def __init__(self) -> None:
        self.start_count = 0
        self.stop_reasons: list[str] = []

    def on_runtime_start(self) -> None:
        self.start_count += 1

    def on_runtime_stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)


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

    def tearDown(self) -> None:
        self.core.on_runtime_stop("test cleanup")
        self.registry.stop_runtime("test cleanup")
        for slot in self.registry.list():
            self.registry.unregister(slot.id)

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
            mock_send.assert_called_once_with(
                "hello world",
                source="mic_plugin",
                turn_id=ANY,
                generation=0,
            )

    def test_filler_only_is_recorded_as_backchannel_without_llm(self) -> None:
        with patch.object(self.core, "send_text") as mock_send:
            for filler in ("嗯", "嗯嗯", "呃", "额", "唔", "啊啊"):
                with self.subTest(filler=filler):
                    self.core._handle_mic_text(
                        TopicMessage(
                            topic="mic.audio.text",
                            timestamp=time.time(),
                            source="mic_plugin",
                            payload={"text": filler},
                        )
                    )
                    latest = self.topic_bus.get_latest(
                        "interaction_core.message.backchannel"
                    )
                    self.assertIsNotNone(latest)
                    assert latest is not None
                    self.assertEqual(latest.payload["text"], filler)

            mock_send.assert_not_called()
            self.assertIsNone(
                self.topic_bus.get_latest("interaction_core.message.incoming")
            )

    def test_confirmation_ack_emits_internal_event_without_llm(self) -> None:
        with patch.object(self.core, "send_text") as mock_send:
            for acknowledgement in ("嗯", "嗯嗯", "好", "可以", "对"):
                with self.subTest(acknowledgement=acknowledgement):
                    with self.core._lock:
                        self.core._awaiting_confirmation_until = time.monotonic() + 5.0
                    self.core._handle_mic_text(
                        TopicMessage(
                            topic="mic.audio.text",
                            timestamp=time.time(),
                            source="mic_plugin",
                            payload={"text": acknowledgement},
                        )
                    )
                    latest = self.topic_bus.get_latest(
                        "interaction_core.message.confirmation"
                    )
                    self.assertIsNotNone(latest)
                    assert latest is not None
                    self.assertTrue(latest.payload["confirmed"])
                    self.assertEqual(latest.payload["text"], acknowledgement)

            mock_send.assert_not_called()

    def test_confirmation_prompt_sets_pending_state(self) -> None:
        with patch.object(self.core, "publish_text"):
            self.core._handle_astrbot_reply_sync({"text": "是否继续？"})

        self.assertTrue(self.core.status_snapshot()["awaiting_confirmation"])

    def test_filler_with_content_is_forwarded_intact(self) -> None:
        phrases = ("嗯我知道了", "啊，打开灯", "嗯，停下来")
        with patch.object(self.core, "send_text") as mock_send:
            for phrase in phrases:
                self.core._handle_mic_text(
                    TopicMessage(
                        topic="mic.audio.text",
                        timestamp=time.time(),
                        source="mic_plugin",
                        payload={"text": phrase},
                    )
                )

        self.assertEqual(
            [call.args[0] for call in mock_send.call_args_list],
            list(phrases),
        )

    def test_tts_capture_gate_stops_mic_and_resumes_after_deadline(self) -> None:
        mic_plugin = LifecycleMicPlugin()
        self.registry.register("mic", mic_plugin, enabled=True)
        self.registry.start_runtime()
        self.core.on_runtime_start()
        self.assertEqual(mic_plugin.start_count, 1)

        self.core._begin_capture_pause(10.0, "tts_playback")
        deadline = time.monotonic() + 1.0
        while not mic_plugin.stop_reasons and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(mic_plugin.stop_reasons, ["tts_playback"])
        self.assertTrue(self.core.status_snapshot()["capture_paused"])
        suppressed = self.core._mic_message_is_suppressed(
            TopicMessage(
                topic="mic.audio.raw",
                timestamp=time.time(),
                source=mic_plugin.id,
                payload={"utterance_id": "during_tts"},
            )
        )
        self.assertTrue(suppressed)

        with self.core._lock:
            self.core._capture_block_until = time.monotonic() - 0.01
            token = self.core._capture_gate_token
        self.core._resume_capture_if_due(token)
        deadline = time.monotonic() + 1.0
        while mic_plugin.start_count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(mic_plugin.start_count, 2)
        self.assertFalse(self.core.status_snapshot()["capture_paused"])
        self.assertEqual(self.core.capture_tail_sec, 0.5)

    def test_publish_audio_publishes_to_interaction_core_audio_play(self) -> None:
        self.core.publish_audio("/tmp/test.wav", format="wav")

        latest = self.topic_bus.get_latest("interaction_core.audio.play")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.payload["format"], "wav")
        self.assertEqual(latest.payload["data"], "/tmp/test.wav")
        self.assertFalse(latest.payload["delete_after_play"])
        self.assertEqual(latest.payload["duration_sec"], 2.0)
        self.assertTrue(self.core.status_snapshot()["capture_paused"])

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
            mock_send.assert_called_once_with(
                "pending message",
                source="mic_plugin",
                turn_id=ANY,
                generation=0,
            )


if __name__ == "__main__":
    unittest.main()
