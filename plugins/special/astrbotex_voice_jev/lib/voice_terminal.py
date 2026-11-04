#!/usr/bin/env python3
"""Standalone Python 3.6+ Nano STT/TTS terminal; no robotics imports."""
import argparse
import base64
import importlib.util
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

RATE = 16000
MAX_AUDIO = 25 * 1024 * 1024
MAX_JSON = 36 * 1024 * 1024


class VoiceError(RuntimeError):
    pass


def command(args, timeout=30):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
    except FileNotFoundError:
        raise VoiceError("缺少命令 {}，请先运行安装脚本。".format(args[0]))
    except subprocess.TimeoutExpired:
        raise VoiceError("命令 {} 超时。".format(args[0]))
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise VoiceError("{} 失败：{}".format(args[0], detail[-1500:]))
    return result.stdout


def devices(kind):
    rows = command(["pactl", "list", "short", kind]).decode("utf-8").splitlines()
    return [line.split()[1] for line in rows if len(line.split()) >= 2]


def resolve_device(kind, requested):
    names = devices(kind)
    if kind == "sources":
        # A speaker monitor is playback loopback, never a microphone.
        names = [name for name in names if not name.endswith(".monitor")]
    if requested:
        if requested not in names:
            raise VoiceError("设备不存在或不能用于录音：{}。先运行 devices。".format(requested))
        return requested
    if not names:
        raise VoiceError("没有可用的{}。蓝牙 A2DP 通常只有输出；麦克风需 HSP/HFP 或独立输入。"
                         .format("录音输入" if kind == "sources" else "播放输出"))
    if len(names) > 1:
        raise VoiceError("发现多个{}，请在配置中明确填写 {}：\n{}".format(
            kind, "source" if kind == "sources" else "sink", "\n".join(names)))
    return names[0]


def record_wav(path, source, seconds):
    if not math.isfinite(seconds) or not 0.2 <= seconds <= 60:
        raise VoiceError("录音时长必须在 0.2～60 秒之间。")
    target = int(seconds * RATE) * 2
    audio = bytearray()
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(["parec", "--raw", "--format=s16le", "--rate=16000",
                                     "--channels=1", "--device=" + source],
                                    stdout=subprocess.PIPE, stderr=errors)
        except FileNotFoundError:
            raise VoiceError("缺少 parec，请安装 pulseaudio-utils。")
        try:
            # Deadline includes startup: a disconnected headset must not hang forever.
            deadline = time.monotonic() + seconds + 5
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while len(audio) < target:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise VoiceError("录音超时，输入设备可能已断开。")
                    if not selector.select(min(remaining, 0.25)):
                        if proc.poll() is not None:
                            break
                        continue
                    chunk = os.read(proc.stdout.fileno(), min(8192, target - len(audio)))
                    if not chunk:
                        break
                    audio.extend(chunk)
            if len(audio) < target:
                errors.seek(0)
                raise VoiceError("录音提前结束：" + errors.read(1500).decode("utf-8", "replace"))
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stdout.close()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(bytes(audio))


def check_wav(path):
    if Path(path).stat().st_size > MAX_AUDIO:
        raise VoiceError("音频超过 25 MiB。")
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                audio.getcomptype()) != (1, 2, RATE, "NONE"):
            raise VoiceError("STT 输入需要 16kHz、16bit、单声道 PCM WAV。")
        if audio.getnframes() == 0:
            raise VoiceError("录音为空。")


def http_post(settings, payload):
    url = settings.get("url", "")
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise VoiceError("HTTP 后端需要完整的 http/https url。")
    headers = {"Content-Type": "application/json"}
    token_env = settings.get("token_env", "")
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            raise VoiceError("未设置环境变量：" + token_env)
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read(MAX_JSON + 1)
    except urllib.error.HTTPError as exc:
        raise VoiceError("语音服务返回 HTTP {}，请检查服务配置。".format(exc.code))
    except (urllib.error.URLError, OSError):
        raise VoiceError("语音服务连接失败或超时，请检查服务地址与网络。")
    if len(raw) > MAX_JSON:
        raise VoiceError("语音服务响应过大。")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise VoiceError("语音服务未返回有效 JSON。")
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise VoiceError("语音服务报告失败，请检查服务端 STT/TTS 配置。")
    return result


class Speech:
    def __init__(self, config, root):
        self.config = config
        self.root = root
        self.model = None

    def transcribe(self, path):
        check_wav(path)
        settings = self.config["stt"]
        if settings["backend"] == "mimo":
            import mimo_client
            try:
                return mimo_client.transcribe(settings, self.root, path)
            except mimo_client.MiMoError as exc:
                raise VoiceError(str(exc)) from None
        if settings["backend"] == "http":
            result = http_post(settings, {
                "audio_base64": base64.b64encode(Path(path).read_bytes()).decode("ascii"),
                "filename": "recording.wav"})
            text = result.get("text", "")
            if not isinstance(text, str):
                raise VoiceError("STT 返回的 text 必须是字符串。")
            return text.strip()
        if settings["backend"] != "vosk":
            raise VoiceError("STT backend 只支持 vosk、mimo 或 http。")
        try:
            from vosk import Model, KaldiRecognizer
        except ImportError:
            raise VoiceError("未安装 Vosk，请运行安装脚本并激活 .venv。")
        model_path = self.root / settings.get("model_path", "models/vosk-model-small-cn-0.22")
        if not (model_path / "am").is_dir() or not (model_path / "conf").is_dir():
            raise VoiceError("缺少 Vosk 模型：{}。请下载并解压中文模型。".format(model_path))
        if self.model is None:
            print("正在加载中文识别模型……", flush=True)
            self.model = Model(str(model_path))
        recognizer = KaldiRecognizer(self.model, RATE)
        parts = []
        with wave.open(str(path), "rb") as audio:
            while True:
                data = audio.readframes(4000)
                if not data:
                    break
                if recognizer.AcceptWaveform(data):
                    parts.append(json.loads(recognizer.Result()).get("text", ""))
        parts.append(json.loads(recognizer.FinalResult()).get("text", ""))
        return " ".join(part for part in parts if part).strip()

    def synthesize(self, text, path):
        if not text.strip():
            raise VoiceError("合成文字不能为空。")
        settings = self.config["tts"]
        if settings["backend"] == "mimo":
            import mimo_client
            try:
                mimo_client.synthesize(settings, self.root, text, path)
            except mimo_client.MiMoError as exc:
                raise VoiceError(str(exc)) from None
            return
        if settings["backend"] == "espeak":
            binary = shutil.which("espeak-ng") or shutil.which("espeak")
            if not binary:
                raise VoiceError("未安装 espeak-ng/espeak。")
            # --stdin avoids interpreting text beginning with '-' as an option.
            with tempfile.TemporaryFile() as errors:
                proc = subprocess.run([binary, "-v", settings.get("voice", "zh"),
                                       "-s", str(settings.get("speed", 150)),
                                       "-w", str(path), "--stdin"],
                                      input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                                      stderr=errors, timeout=60)
                if proc.returncode:
                    raise VoiceError("基础 TTS 合成失败，请检查 espeak 的中文语音支持。")
            return
        if settings["backend"] != "http":
            raise VoiceError("TTS backend 只支持 espeak、mimo 或 http。")
        result = http_post(settings, {"text": text})
        encoded = result.get("audio_base64")
        if encoded:
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                raise VoiceError("TTS 返回的 base64 音频无效。")
        else:
            url = result.get("audio_url", "")
            if not isinstance(url, str) or urllib.parse.urlparse(url).scheme not in ("http", "https"):
                raise VoiceError("远程 TTS 必须返回 audio_base64 或可下载的 HTTP 音频；服务端文件路径不能在 Nano 上播放。")
            try:
                with urllib.request.urlopen(url, timeout=60) as response:
                    data = response.read(MAX_AUDIO + 1)
            except (urllib.error.URLError, OSError):
                raise VoiceError("TTS 音频下载失败。")
        if not data or len(data) > MAX_AUDIO:
            raise VoiceError("TTS 音频为空或超过 25 MiB。")
        Path(path).write_bytes(data)


def play(path, sink, volume):
    # Normalize WAV/MP3/etc. to WAV; PulseAudio's paplay doesn't decode MP3.
    with tempfile.TemporaryDirectory(prefix="nano-play-") as temp:
        wav = str(Path(temp) / "play.wav")
        command(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(path), wav], 60)
        command(["paplay", "--device=" + sink,
                 "--volume=" + str(round(volume * 65536 / 100)), wav], 120)


def turn(speech, source, sink, seconds, volume, directory, chat=None):
    recording = Path(directory) / "input.wav"
    reply = Path(directory) / "reply.audio"
    print("正在录音（{} 秒），请说话……".format(seconds), flush=True)
    record_wav(recording, source, seconds)
    print("正在识别……", flush=True)
    text = speech.transcribe(recording)
    print("STT：" + (text or "[未识别到文字]"), flush=True)
    if not text:
        return
    if chat is not None:
        print("正在请求 AstrBot 生成回答……", flush=True)
        text = chat.ask(text)
        print("回答：" + text, flush=True)
    # Without a chat client, preserve the independent STT/TTS debug round trip.
    speech.synthesize(text, reply)
    print("正在复述……", flush=True)
    play(reply, sink, volume)
    # Capture is closed before synthesis/playback, preventing feedback recording.


def tone(path):
    samples = [int(5000 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(RATE)]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(struct.pack("<{}h".format(len(samples)), *samples))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Nano STT/TTS 终端：蓝牙/USB 音频均可")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--source", help="完整 PulseAudio 输入设备名称")
    parser.add_argument("--sink", help="完整 PulseAudio 输出设备名称")
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("devices", help="列出音频输入、输出和蓝牙控制器")
    sub.add_parser("doctor", help="检查依赖与模型（不修改系统）")
    sub.add_parser("tone", help="播放一秒测试音，不需要麦克风或模型")
    stt = sub.add_parser("stt", help="语音识别，可使用已有 WAV 或现场录音")
    stt.add_argument("--file", help="16kHz/16bit/单声道 PCM WAV")
    tts = sub.add_parser("tts", help="文字合成后播放，不需要麦克风或 STT 模型")
    tts.add_argument("text")
    run = sub.add_parser("run", help="录音 → STT → TTS 复述；按回车开始一轮")
    run.add_argument("--once", action="store_true")
    run.add_argument("--chat-config", help="AstrBot 连接 JSON；提供后识别文字交给对话模型回答")
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 0
    if args.action == "devices":
        for kind in ("sinks", "sources"):
            print(kind + ":\n" + "\n".join(devices(kind)))
        if shutil.which("bluetoothctl"):
            print(command(["bluetoothctl", "list"]).decode("utf-8"))
        return 0
    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        raise VoiceError("请先复制 config.example.json 为 config.json。")
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    root = config_path.parent
    if args.action == "doctor":
        healthy = True
        for kind in ("stt", "tts"):
            if config[kind]["backend"] == "mimo":
                import mimo_client
                try:
                    mimo_client.api_key(config[kind], root)
                    mimo_client.api_url(config[kind].get("api_base", mimo_client.DEFAULT_BASE))
                    mimo_client.timeout_seconds(config[kind].get("timeout"))
                    print("MiMo {} 本地配置: OK（未请求云端）".format(kind))
                except mimo_client.MiMoError as exc:
                    print(str(exc))
                    healthy = False
        for name in ("pactl", "parec", "paplay", "ffmpeg"):
            present = bool(shutil.which(name))
            healthy = healthy and present
            print("{}: {}".format(name, "OK" if present else "缺失"))
        if config["stt"]["backend"] == "vosk":
            model = root / config["stt"]["model_path"]
            present = importlib.util.find_spec("vosk") is not None and (model / "am").is_dir() and (model / "conf").is_dir()
            healthy = healthy and present
            print("Vosk + 中文模型: " + ("OK" if present else "缺失"))
        if config["tts"]["backend"] == "espeak":
            present = bool(shutil.which("espeak-ng") or shutil.which("espeak"))
            healthy = healthy and present
            print("基础 TTS: " + ("OK" if present else "缺失"))
        try:
            print(command(["pactl", "info"]).decode("utf-8"))
        except VoiceError as exc:
            print(str(exc))
            healthy = False
        print("doctor 只检查本地依赖，服务端连通性、中文识别效果和实物收音需实际测试。")
        return 0 if healthy else 1
    seconds = float(config.get("record_seconds", 5))
    volume = float(config.get("volume_percent", 40))
    if not math.isfinite(volume) or not 0 <= volume <= 100:
        raise VoiceError("volume_percent 必须在 0～100 之间。")
    speech = Speech(config, root)
    with tempfile.TemporaryDirectory(prefix="nano-voice-") as temp:
        if args.action == "stt":
            recording = args.file or str(Path(temp) / "input.wav")
            if not args.file:
                source = resolve_device("sources", args.source or config.get("source", ""))
                print("开始录音……", flush=True)
                record_wav(recording, source, seconds)
            print(speech.transcribe(recording))
            return 0
        sink = resolve_device("sinks", args.sink or config.get("sink", ""))
        if args.action == "tone":
            output = Path(temp) / "tone.wav"
            tone(output)
            play(output, sink, volume)
        elif args.action == "tts":
            output = Path(temp) / "reply.audio"
            speech.synthesize(args.text, output)
            play(output, sink, volume)
        elif args.action == "run":
            source = resolve_device("sources", args.source or config.get("source", ""))
            chat = None
            if args.chat_config:
                from astrbot_chat import AstrBotChat
                chat = AstrBotChat.from_file(args.chat_config)
            while True:
                if not args.once and input("回车开始录音；q + 回车退出：").strip().lower() == "q":
                    break
                turn(speech, source, sink, seconds, volume, temp, chat=chat)
                if args.once:
                    break
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n已停止。")
        sys.exit(0)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, wave.Error,
            subprocess.TimeoutExpired) as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        sys.exit(1)
