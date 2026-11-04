#!/usr/bin/env python3
"""Windows local test UI; reuses the Nano terminal's Vosk STT implementation."""
import argparse
import json
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import wave

import sounddevice as sd
from voice_terminal import Speech, VoiceError, RATE
from astrbot_chat import AstrBotChat
from audio_playback import play_wav

ROOT = Path(__file__).resolve().parent


def sapi(text, output):
    with tempfile.TemporaryDirectory(prefix="voice-text-") as temp:
        text_file = Path(temp) / "text.txt"
        text_file.write_text(text, encoding="utf-8")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(ROOT / "windows_speak.ps1"), "-TextFile", str(text_file),
             "-WaveFile", str(output)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise VoiceError("Windows 中文合成失败：" + result.stderr.decode("utf-8", "replace")[-1200:])


def record(output, device, seconds=5):
    chunks = []
    complete = threading.Event()
    target = int(RATE * seconds)
    received = 0
    problems = []

    def callback(data, frames, timing, status):
        nonlocal received
        if status:
            problems.append(str(status))
        count = min(frames, target - received)
        chunks.append(bytes(data)[:count * 2])
        received += count
        if received >= target:
            raise sd.CallbackStop

    with sd.RawInputStream(device=device, samplerate=RATE, channels=1, dtype="int16",
                           callback=callback, finished_callback=complete.set):
        if not complete.wait(seconds + 8):
            raise VoiceError("录音超时，请检查耳机连接和 Windows 麦克风权限。")
    if received < target or problems:
        raise VoiceError("录音不完整或有丢帧，请重试或切换电脑麦克风。" + "; ".join(problems))
    with wave.open(str(output), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(RATE)
        audio.writeframes(b"".join(chunks))


def playback(path, device):
    play_wav(path, device)


def speech_backend(mode="offline"):
    path = ROOT / "config.example.json"
    if mode == "mimo":
        path = ROOT / "config.mimo.local.json"
        if not path.exists():
            path = ROOT / "config.mimo.example.json"
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    return Speech(config, ROOT)


def device_options():
    apis = sd.query_hostapis()
    entries = []
    for index, device in enumerate(sd.query_devices()):
        api = apis[device["hostapi"]]["name"]
        # MME supports Windows format conversion at our 16kHz recording rate.
        if api != "MME" or "映射器" in device["name"]:
            continue
        entries.append((index, device, "{}: {}".format(index, device["name"])))
    return entries


def gui():
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title("JetMax 语音对话（尾音修复版）")
    root.geometry("860x690")
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="蓝牙耳机语音对话", font=("Microsoft YaHei UI", 17)).pack(anchor="w")
    ttk.Label(frame, text="点按钮后才录音；每次5秒。可切换 MiMo 或本地离线识别与复述。\n"
              "MiMo 模式会将本轮录音和播报文字发送到配置的小米语音服务。").pack(anchor="w", pady=8)
    ttk.Label(frame, text="语音后端：").pack(anchor="w")
    backend = ttk.Combobox(frame, state="readonly", values=["MiMo 云端 STT + TTS", "本地 Vosk + Windows 语音"])
    backend.current(0 if (ROOT / "config.mimo.local.json").exists() else 1)
    backend.pack(fill="x", pady=(3, 8))
    use_chat = tk.BooleanVar(value=(ROOT / "astrbot.chat.local.json").exists())
    chat_option = ttk.Checkbutton(frame, text="接入 AstrBot 对话（使用连接配置中指定的模型）", variable=use_chat)
    chat_option.pack(anchor="w", pady=4)
    entries = device_options()
    inputs = [(i, label) for i, d, label in entries if d["max_input_channels"]]
    outputs = [(i, label) for i, d, label in entries if d["max_output_channels"]]
    ttk.Label(frame, text="录音输入（可选耳机麦克风或电脑麦克风）：").pack(anchor="w")
    source = ttk.Combobox(frame, state="readonly", values=[label for _, label in inputs])
    source.pack(fill="x", pady=(3, 8))
    ttk.Label(frame, text="播放输出：").pack(anchor="w")
    sink = ttk.Combobox(frame, state="readonly", values=[label for _, label in outputs])
    sink.pack(fill="x", pady=(3, 8))
    for combo, choices in ((source, inputs), (sink, outputs)):
        if choices:
            selected = next((n for n, (_, label) in enumerate(choices) if "酷狗" in label), 0)
            combo.current(selected)
    text = tk.StringVar(value="你好，请简单介绍一下你自己。")
    ttk.Entry(frame, textvariable=text).pack(fill="x", pady=8)
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x")
    status = tk.StringVar(value="就绪。先点“测试耳机播报”，确认耳机有声音。")
    ttk.Label(frame, textvariable=status, wraplength=750).pack(anchor="w", pady=12)
    log = tk.Text(frame, height=10, wrap="word", font=("Microsoft YaHei UI", 11))
    log.pack(fill="both", expand=True)
    messages = queue.Queue()
    controls = []
    busy = False
    speech_instances = {}
    chat_client = None

    def new_conversation():
        nonlocal chat_client
        if busy:
            return
        if chat_client:
            chat_client.new_session()
        log.insert("end", "已开始新对话；之前的聊天记录保留在 AstrBot。\n")

    def start(mode):
        nonlocal busy
        if busy:
            return
        if sink.current() < 0 or (mode != "tts" and source.current() < 0):
            status.set("请先连接并选择需要的音频设备。")
            return
        output = outputs[sink.current()][0]
        capture = inputs[source.current()][0] if source.current() >= 0 else None
        words = text.get().strip()
        mode_name = "mimo" if backend.current() == 0 else "offline"
        chat_enabled = use_chat.get() or mode == "chat_text"
        busy = True
        for control in controls:
            control.configure(state="disabled")
        source.configure(state="disabled")
        sink.configure(state="disabled")
        backend.configure(state="disabled")
        chat_option.configure(state="disabled")

        def worker():
            nonlocal chat_client
            try:
                if mode_name not in speech_instances:
                    speech_instances[mode_name] = speech_backend(mode_name)
                speech = speech_instances[mode_name]
                with tempfile.TemporaryDirectory(prefix="jetmax-windows-") as temp:
                    recorded = Path(temp) / "recorded.wav"
                    reply = Path(temp) / "reply.wav"
                    result = words
                    if mode not in ("tts", "chat_text"):
                        messages.put("录音即将开始，请准备说：你好，今天天气很好。")
                        time.sleep(1)
                        messages.put("正在录音5秒，请说话……")
                        record(recorded, capture)
                        if mode == "capture":
                            messages.put("录音完成，正在回放原声（此项不经过 STT/TTS）。")
                            playback(recorded, output)
                            return
                        messages.put("录音结束，正在识别中文……")
                        started = time.perf_counter()
                        result = speech.transcribe(recorded)
                        messages.put("STT 耗时：{:.2f} 秒".format(time.perf_counter() - started))
                        messages.put("识别结果：" + (result or "[没有识别到文字，请检查输入或重试]"))
                    if not result:
                        return
                    if mode in ("roundtrip", "chat_text") and chat_enabled:
                        messages.put("正在请求 AstrBot 对话模型回答……")
                        started = time.perf_counter()
                        if chat_client is None:
                            chat_client = AstrBotChat.from_file(ROOT / "astrbot.chat.local.json")
                        result = chat_client.ask(result)
                        messages.put("对话耗时：{:.2f} 秒".format(time.perf_counter() - started))
                        messages.put("回答：" + result)
                    messages.put("正在合成中文语音……")
                    started = time.perf_counter()
                    if mode_name == "mimo":
                        speech.synthesize(result, reply)
                    else:
                        sapi(result, reply)
                    messages.put("TTS 耗时：{:.2f} 秒".format(time.perf_counter() - started))
                    messages.put("正在通过所选耳机播放……")
                    playback(reply, output)
                    messages.put("本轮完成。请确认实际听到的声音。")
            except Exception as exc:
                messages.put("测试失败：{}".format(exc))
            finally:
                messages.put(None)
        threading.Thread(target=worker, daemon=True).start()

    for title, mode in (("测试耳机播报", "tts"), ("录音并对话 / 复述", "roundtrip"),
                        ("文字提问", "chat_text"), ("回放原声", "capture")):
        button = ttk.Button(buttons, text=title, command=lambda m=mode: start(m))
        button.pack(side="left", padx=(0, 10))
        controls.append(button)
    reset = ttk.Button(buttons, text="新对话", command=new_conversation)
    reset.pack(side="left")
    controls.append(reset)

    def poll():
        nonlocal busy
        while True:
            try:
                item = messages.get_nowait()
            except queue.Empty:
                break
            if item is None:
                busy = False
                for control in controls:
                    control.configure(state="normal")
                source.configure(state="readonly")
                sink.configure(state="readonly")
                backend.configure(state="readonly")
                chat_option.configure(state="normal")
            else:
                status.set(item)
                log.insert("end", item + "\n")
                log.see("end")
                print(item, flush=True)
        root.after(100, poll)

    poll()
    print("GUI_READY", flush=True)
    root.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true", help="SAPI 合成后用 Vosk 识别，不录音不播放")
    parser.add_argument("--play-test", type=int, metavar="DEVICE", help="向指定输出设备播报测试句")
    args = parser.parse_args()
    if args.self_test or args.play_test is not None:
        with tempfile.TemporaryDirectory(prefix="jetmax-check-") as temp:
            wav = Path(temp) / "test.wav"
            sapi("你好，今天天气很好。", wav)
            if args.self_test:
                result = speech_backend().transcribe(wav)
                if not result:
                    raise VoiceError("合成音频识别为空。")
                print("SAPI_TO_VOSK_OK: " + result, flush=True)
            else:
                playback(wav, args.play_test)
                print("PLAYBACK_STREAM_OK (请由用户确认实际听感)", flush=True)
    else:
        gui()


if __name__ == "__main__":
    main()
