"""MiMo STT/TTS protocol ported from the user's AstrBot adapters (Python 3.6+).

No AstrBot imports: the same cloud backend works on a Nano or Windows terminal.
"""
import base64
import io
import json
import math
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import wave

MAX_AUDIO = 25 * 1024 * 1024
MAX_RESPONSE = 36 * 1024 * 1024
DEFAULT_BASE = "https://api.xiaomimimo.com/v1"


class MiMoError(RuntimeError):
    pass


def api_url(base):
    parts = urllib.parse.urlparse(base)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise MiMoError("MiMo api_base 必须是有效的 HTTP/HTTPS 地址，不含账号、查询参数或片段。")
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise MiMoError("远程 MiMo 服务必须使用 HTTPS。")
    base = base.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def timeout_seconds(value):
    try:
        timeout = float(60 if value in (None, "") else value)
    except (TypeError, ValueError):
        raise MiMoError("MiMo timeout 必须是秒数。")
    if not math.isfinite(timeout) or not 1 <= timeout <= 180:
        raise MiMoError("MiMo timeout 必须在 1～180 秒之间。")
    return timeout


def api_key(settings, root):
    value = os.environ.get(settings.get("api_key_env", "MIMO_API_KEY"), "").strip()
    if not value and settings.get("credentials_file"):
        try:
            data = json.loads((Path(root) / settings["credentials_file"]).read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise MiMoError("MiMo 本地凭据必须是 JSON 对象。")
            value = data.get(settings.get("credential_name", "api_key"), "")
        except (OSError, ValueError, TypeError):
            raise MiMoError("无法读取 MiMo 本地凭据文件。")
    if not isinstance(value, str) or not value.strip():
        raise MiMoError("未配置 MiMo 密钥，请设置 MIMO_API_KEY 或导入旧版 provider 配置。")
    return value.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(settings, root, payload):
    key = api_key(settings, root)
    timeout = timeout_seconds(settings.get("timeout", 60))
    url = api_url(settings.get("api_base", DEFAULT_BASE))
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key}, method="POST")
    handlers = [NoRedirect()]
    if settings.get("proxy"):
        handlers.append(urllib.request.ProxyHandler({"http": settings["proxy"], "https": settings["proxy"]}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        hints = {400: "请求参数或模型配置不被接受", 401: "密钥无效", 403: "模型权限不足",
                 404: "模型或接口地址不存在", 429: "请求受限或额度不足"}
        hint = hints.get(exc.code, "语音服务异常，请稍后重试")
        status = exc.code
        exc.close()
        # Never include raw server bodies: they may echo credentials/audio/text.
        raise MiMoError("MiMo HTTP {}：{}。".format(status, hint)) from None
    except (urllib.error.URLError, OSError):
        raise MiMoError("MiMo 连接失败或超时，请检查网络、代理及 timeout。") from None
    if len(raw) > MAX_RESPONSE:
        raise MiMoError("MiMo 响应超过大小限制。")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise MiMoError("MiMo 响应不是有效的 JSON。")
    if not isinstance(data, dict) or data.get("error"):
        raise MiMoError("MiMo 服务返回错误，请检查模型与额度。")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise MiMoError("MiMo 没有返回有效的 choices。")
    if choices[0].get("finish_reason") == "length":
        raise MiMoError("MiMo 输出被截断，请缩短录音或播报文字。")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise MiMoError("MiMo 没有返回有效的 message。")
    return message


def validate_wav(data):
    if not data or len(data) > MAX_AUDIO:
        raise MiMoError("WAV 为空或超过 25 MiB。")
    if not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
        raise MiMoError("音频不是 RIFF/WAVE，请先将 SILK/MP3 等格式转换成 WAV。")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            channels, width, rate, frames = (audio.getnchannels(), audio.getsampwidth(),
                                             audio.getframerate(), audio.getnframes())
            if audio.getcomptype() != "NONE" or channels not in (1, 2) or width != 2 or rate <= 0 or frames <= 0:
                raise MiMoError("WAV 需要非空的 16bit PCM 单声道或双声道音频。")
            if len(audio.readframes(frames)) != frames * width * channels:
                raise MiMoError("WAV 数据不完整。")
    except (wave.Error, EOFError):
        raise MiMoError("WAV 文件头无效或不完整。")


def stt_payload(settings, data):
    validate_wav(data)
    model = settings.get("model", "mimo-v2.5-asr")
    content = [{"type": "input_audio", "input_audio": {
        "data": "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")}}]
    messages = [{"role": "user", "content": content}]
    payload = {"model": model, "messages": messages, "stream": False}
    if "asr" in model.lower():
        language = settings.get("language", "auto")
        if language not in ("auto", "zh", "en"):
            raise MiMoError("MiMo ASR language 只支持 auto/zh/en。")
        payload["asr_options"] = {"language": language}
    else:
        messages.insert(0, {"role": "system", "content":
                        "Transcribe the spoken content exactly and return only the transcription text."})
        content.append({"type": "text", "text": "Please transcribe the audio. Return only the transcription text."})
    return payload


def transcribe(settings, root, path):
    path = Path(path)
    if path.stat().st_size > MAX_AUDIO:
        raise MiMoError("录音超过 25 MiB。")
    message = request(settings, root, stt_payload(settings, path.read_bytes()))
    # Reasoning content is not a transcript; don't fall back to it.
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise MiMoError("MiMo STT 没有返回识别文字，请检查录音是否有声。")
    return text.strip()


def tts_payload(settings, text):
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise MiMoError("MiMo TTS 需要 1～2000 字符的播报文字。")
    model = settings.get("model", "mimo-v2.5-tts")
    if "voiceclone" in model.lower():
        raise MiMoError("当前终端不提供声音克隆，请使用普通 TTS 或 voicedesign 模型。")
    audio_format = settings.get("mimo-tts-format", "wav").lower()
    if audio_format != "wav":
        raise MiMoError("本地与 Nano 共用的 MiMo 后端目前使用 WAV，请将 mimo-tts-format 设为 wav。")
    style = " ".join(str(settings.get(name, "")).strip() for name in
                     ("mimo-tts-style-prompt", "mimo-tts-dialect")).strip()
    prefix = "<style>{}</style>".format("唱歌" if "唱歌" in style else style) if style else ""
    messages = []
    seed = settings.get("mimo-tts-seed-text", "Hello, MiMo, have you had lunch?").strip()
    if seed:
        messages.append({"role": "user", "content": seed})
    messages.append({"role": "assistant", "content": prefix + text})
    audio = {"format": "wav"}
    if "voicedesign" not in model.lower():
        audio["voice"] = settings.get("mimo-tts-voice", "mimo_default")
    return {"model": model, "messages": messages, "audio": audio, "stream": False}


def synthesize(settings, root, text, path):
    message = request(settings, root, tts_payload(settings, text))
    audio = message.get("audio")
    encoded = audio.get("data") if isinstance(audio, dict) else None
    if not isinstance(encoded, str) or not encoded or len(encoded) > ((MAX_AUDIO + 2) // 3) * 4:
        raise MiMoError("MiMo TTS 音频为空或超过大小限制。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise MiMoError("MiMo TTS 返回的 base64 音频无效。")
    validate_wav(data)
    Path(path).write_bytes(data)


def stream_pcm(settings, root, text, cancel_event=None):
    """Yield native 24kHz PCM16LE mono chunks from official MiMo SSE."""
    body = tts_payload(settings, text)
    if body['model'] != 'mimo-v2.5-tts':
        raise MiMoError('Realtime PCM playback requires mimo-v2.5-tts')
    body['stream'] = True
    body['audio']['format'] = 'pcm16'
    key = api_key(settings, root)
    req = urllib.request.Request(api_url(settings.get('api_base', DEFAULT_BASE)),
        method='POST', data=json.dumps(body).encode('utf-8'),
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept':'text/event-stream'})
    handlers = [NoRedirect()]
    if settings.get('proxy'):
        handlers.append(urllib.request.ProxyHandler({'http':settings['proxy'],'https':settings['proxy']}))
    emitted = 0
    finished = False
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout_seconds(settings.get('timeout'))) as response:
            lines = []
            total = 0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return
                raw = response.readline(1048577)
                if not raw:
                    break
                total += len(raw)
                if len(raw)>1048576 or total>MAX_RESPONSE:
                    raise MiMoError('MiMo PCM stream exceeds size limit')
                line = raw.decode('utf-8').rstrip('\r\n')
                if line.startswith('data:'):
                    lines.append(line[5:].lstrip())
                elif not line and lines:
                    event_text = '\n'.join(lines); lines = []
                    if event_text == '[DONE]':
                        finished = True
                        break
                    event = json.loads(event_text)
                    if not isinstance(event, dict) or event.get('error'):
                        raise MiMoError('MiMo PCM stream returned an error')
                    for choice in event.get('choices', []):
                        if choice.get('finish_reason') == 'length':
                            raise MiMoError('MiMo audio was truncated by the service')
                        finished = finished or choice.get('finish_reason') == 'stop'
                        audio = choice.get('delta', {}).get('audio') or {}
                        encoded = audio.get('data')
                        if encoded:
                            data = base64.b64decode(encoded, validate=True)
                            emitted += len(data)
                            if emitted > MAX_AUDIO:
                                raise MiMoError('MiMo audio exceeds size limit')
                            yield data
    except urllib.error.HTTPError as exc:
        status=exc.code; exc.close()
        raise MiMoError('MiMo streaming TTS HTTP {}'.format(status)) from None
    except (urllib.error.URLError, OSError):
        raise MiMoError('MiMo streaming audio connection failed') from None
    except (ValueError, UnicodeError, AttributeError, TypeError):
        raise MiMoError('Invalid MiMo streaming audio response') from None
    if not emitted or not finished:
        raise MiMoError('MiMo audio stream ended before completion')
