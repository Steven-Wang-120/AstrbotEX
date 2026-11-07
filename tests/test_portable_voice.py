from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
import wave

import pytest

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.interaction_core import InteractionCore
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.providers import mimo_client
from astrbot_ex.core.providers.mimo_providers import MiMoSTTProvider, MiMoTTSProvider
from astrbot_ex.core.topic_bus import TopicBus
from astrbot_ex.core.voice import ConversationGate, ConversationMemory, DecisionClient, GateError
from astrbot_ex.core.voice.decision import endpoint


def pcm_wav() -> bytes:
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 320)
    return data.getvalue()


@pytest.fixture
def service():
    state = SimpleNamespace(reply={}, status=200, content_type="application/json", requests=[])

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state.requests.append((self.path, json.loads(body), self.headers.get("Authorization")))
            reply = state.reply if isinstance(state.reply, bytes) else json.dumps(state.reply).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", state.content_type)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    state.url = "http://127.0.0.1:" + str(server.server_port)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_imports_need_no_audio_backend_or_platform_credential_store():
    code = """
import importlib.abc
import sys
class BlockDevices(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'sounddevice', 'vosk', 'ctypes', 'win32crypt'}:
            raise AssertionError('Device dependency: ' + fullname)
sys.meta_path.insert(0, BlockDevices())
from astrbot_ex.core.voice import DecisionClient, ConversationGate, ConversationMemory
from astrbot_ex.core.providers.mimo_providers import MiMoSTTProvider, MiMoTTSProvider
"""
    subprocess.run([sys.executable, "-X", "utf8", "-c", code], check=True, capture_output=True)


@pytest.mark.parametrize("protocol", ["systemone", "laya", "cloudflare", "http_json", "openai"])
def test_decision_protocols_use_only_http(service, protocol):
    scores = {"addressed": .95, "forward": .9, "quiet": .02}
    typed = {"answers": {key: {"type": "noul", "noul": value} for key, value in scores.items()}}
    if protocol == "cloudflare":
        service.reply = {"success": True, "result": typed}
    elif protocol == "http_json":
        service.reply = {"scores": scores}
    elif protocol == "openai":
        service.reply = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(scores)}}]}
    else:
        service.reply = typed
    config = {"type": protocol, "url": service.url, "api_key": "test-key", "model": "test-model"}
    assert DecisionClient(config).evaluate({"latest_utterance": "hello"}) == scores
    path, body, authorization = service.requests[-1]
    assert authorization == "Bearer test-key"
    assert path == ("/v1/systemone" if protocol in ("systemone", "laya") else
                    "/v1/chat/completions" if protocol == "openai" else "/")
    if protocol == "laya":
        assert body["max_len"] == 8192
    if protocol == "openai":
        assert body["stream"] is False
        assert body["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("reply", [
    {"scores": {"addressed": True, "forward": .9, "quiet": 0}},
    {"scores": {"addressed": 1.01, "forward": .9, "quiet": 0}},
    {"scores": {"addressed": float("nan"), "forward": .9, "quiet": 0}},
    {"scores": {"addressed": .9, "quiet": 0}},
    {"allowed": True},
    {"addressed_to_assistant": "true", "should_reply": True, "should_stop": False},
    {"addressed": .9, "forward": .9, "quiet": 0, "should_reply": True},
    {"request_id": "old-request", "addressed_to_assistant": True, "should_reply": True, "should_stop": False},
    {"abstention": True, "scores": {"addressed": 1, "forward": 1, "quiet": 0}},
    {"truncated": True, "scores": {"addressed": 1, "forward": 1, "quiet": 0}},
])
def test_invalid_or_stale_decision_never_approves(service, reply):
    service.reply = reply
    client = DecisionClient({"type": "http_json", "url": service.url})
    with pytest.raises(GateError):
        client.evaluate({"latest_utterance": "hello"})


def test_canceled_decision_does_not_call_service(service):
    stop = threading.Event()
    stop.set()
    with pytest.raises(GateError, match="cancel"):
        DecisionClient({"type": "http_json", "url": service.url}).evaluate({}, stop)
    assert service.requests == []


def test_service_errors_do_not_expose_response_body(service):
    service.status = 401
    service.reply = {"error": "private service error body"}
    with pytest.raises(GateError) as caught:
        DecisionClient({"url": service.url}).evaluate({})
    assert "401" in str(caught.value)
    assert "private service" not in str(caught.value)


def test_device_credentials_are_resolved_by_the_caller():
    with pytest.raises(GateError, match="adapter"):
        DecisionClient({"url": "https://example.test", "credentials_file": "machine-bound.json"})
    with patch.dict(os.environ, {"DECISION_API_KEY": "env-key"}):
        assert DecisionClient({"url": "https://example.test"}).token() == "env-key"
        assert DecisionClient({"url": "https://example.test", "api_key": "new-key"}).token() == "new-key"


@pytest.mark.parametrize("url", ["file:///private/key", "http://example.test", "https://user:pass@example.test", "https://example.test/#key"])
def test_unsafe_decision_endpoints_are_rejected(url):
    with pytest.raises(GateError):
        endpoint(url, "http_json")


def test_conversation_gate_handles_background_silence_and_resume():
    clock = [100.]
    client = SimpleNamespace(evaluate=lambda *args, **kwargs: {"addressed": .9, "forward": .9, "quiet": 0})
    gate = ConversationGate(client, threshold=.4, clock=lambda: clock[0])
    assert not gate.local_quiet("别说话")
    result = gate.evaluate("你好")
    assert result["allowed"]
    assert gate.local_quiet("别说话")
    quiet = gate.evaluate("别说话")
    assert quiet["quiet"] and not quiet["allowed"]
    assert gate.state("test")["quiet_requested"]
    assert gate.evaluate("继续解释一下")["allowed"]
    assert not gate.state("test")["quiet_requested"]


def test_gate_rejects_probabilities_from_an_invalid_adapter():
    client = SimpleNamespace(evaluate=lambda *args, **kwargs: {"addressed": True, "forward": 1, "quiet": 0})
    with pytest.raises(GateError):
        ConversationGate(client).evaluate("hello")


def test_background_memory_has_bounded_size_age_and_version():
    now = [100.]
    memory = ConversationMemory(max_items=2, max_age=10, clock=lambda: now[0])
    for text in ["one", "two", "three"]:
        memory.remember(text)
    context, version = memory.snapshot()
    assert context == ["two", "three"]
    memory.remember("four")
    memory.acknowledge(version)
    assert memory.context() == ["four"]
    now[0] += 11
    assert memory.context() == []


def test_mimo_stt_and_tts_fit_existing_interaction_core(service, tmp_path):
    settings = {"api_base": service.url + "/v1", "api_key": "test-key"}
    core = InteractionCore(registry=PluginRegistry(), topic_bus=TopicBus(), event_bus=EventBus(),
                           stt_provider=MiMoSTTProvider(settings), tts_provider=MiMoTTSProvider(settings))
    wav = pcm_wav()
    recording = tmp_path / "recording.wav"
    recording.write_bytes(wav)
    service.reply = {"choices": [{"message": {"content": "你好"}}]}
    assert core.transcribe_audio(str(recording)) == {"ok": True, "text": "你好"}
    path, payload, auth = service.requests[-1]
    assert path == "/v1/chat/completions" and auth == "Bearer test-key"
    assert payload["model"] == "mimo-v2.5-asr"
    assert base64.b64decode(payload["messages"][0]["content"][0]["input_audio"]["data"].split(",", 1)[1]) == wav
    service.reply = {"choices": [{"message": {"audio": {"data": base64.b64encode(wav).decode()}}}]}
    result = core.synthesize_text("你好")
    assert result["ok"]
    output = Path(result["audio_url"])
    try:
        assert output.read_bytes() == wav
        assert core.topic_bus.get_latest("interaction_core.audio.play").payload["delete_after_play"] is True
    finally:
        output.unlink(missing_ok=True)


def test_invalid_input_audio_is_rejected_before_network(service, tmp_path):
    path = tmp_path / "invalid.wav"
    path.write_bytes(b"not audio")
    provider = MiMoSTTProvider({"api_base": service.url, "api_key": "test-key"})
    with pytest.raises(mimo_client.MiMoError):
        asyncio.run(provider.get_text(str(path)))
    assert service.requests == []


def test_tts_failure_cleans_temporary_file(service):
    service.reply = {"choices": [{"message": {"audio": {"data": "invalid base64"}}}]}
    with tempfile.TemporaryDirectory() as temp:
        with patch("tempfile.tempdir", temp):
            with pytest.raises(mimo_client.MiMoError):
                asyncio.run(MiMoTTSProvider({"api_base": service.url, "api_key": "test-key"}).get_audio("hello"))
        assert list(Path(temp).iterdir()) == []


def test_pcm_stream_is_reusable_without_playback(service):
    service.content_type = "text/event-stream"
    event = {"choices": [{"delta": {"audio": {"data": base64.b64encode(b"\x01\x00\x02\x00").decode()}}, "finish_reason": "stop"}]}
    service.reply = ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()
    provider = MiMoTTSProvider({"api_base": service.url, "api_key": "test-key"})
    assert b"".join(provider.iter_pcm("hello")) == b"\x01\x00\x02\x00"
    assert service.requests[-1][1]["audio"]["format"] == "pcm16"
    stop = threading.Event()
    stop.set()
    assert list(provider.iter_pcm("hello", cancel_event=stop)) == []
    assert len(service.requests) == 1


@pytest.mark.parametrize("reply", [
    b'data: [DONE]\n\n',
    b'data: {"choices":[{"delta":{"audio":{"data":"AQ=="}},"finish_reason":"stop"}]}\n\n',
    b'data: {"choices":[{"delta":{"audio":{"data":"AQI="}}}]}\n\n',
])
def test_empty_odd_or_unfinished_pcm_stream_is_rejected(service, reply):
    service.content_type = "text/event-stream"
    service.reply = reply
    provider = MiMoTTSProvider({"api_base": service.url, "api_key": "test-key"})
    with pytest.raises(mimo_client.MiMoError):
        list(provider.iter_pcm("hello"))


def test_sentence_buffer_keeps_partial_text_until_a_boundary():
    from astrbot_ex.core.voice.text import SentenceBuffer, spoken_text
    buffer = SentenceBuffer()
    assert buffer.feed("This is") == []
    assert buffer.feed(" a sentence!Next") == ["This is a sentence!"]
    assert buffer.finish() == ["Next"]
    assert buffer.finish() == []
    assert spoken_text("**hello** [world](https://example.test) `again`") == "hello world again"
