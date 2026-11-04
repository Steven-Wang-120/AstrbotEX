"""Incremental 16kHz PCM capture, local previews and utterance boundaries."""
from collections import deque
import json
import math
import queue
import struct
import threading
import time

RATE = 16000
FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2


def rms(pcm):
    if len(pcm) % 2:
        raise ValueError("PCM frame must contain complete int16 samples")
    if not pcm:
        return 0
    values = struct.unpack("<{}h".format(len(pcm) // 2), pcm)
    return math.sqrt(sum(value * value for value in values) / len(values))


class Segmenter:
    """Energy-based boundaries with pre-roll, minimum speech and a hard cap."""
    def __init__(self, threshold=180, silence_seconds=0.9, max_seconds=30):
        self.threshold = threshold
        self.silence_frames = round(silence_seconds * 50)
        self.max_frames = round(max_seconds * 50)
        self.pre_roll = deque(maxlen=15)
        self.active = False
        self.chunks = []
        self.consecutive_voice = 0
        self.voice_frames = 0
        self.quiet_frames = 0
        self.last_reason = ""

    def push(self, pcm):
        if len(pcm) != FRAME_BYTES:
            raise ValueError("Each frame must be 20ms of 16kHz mono PCM16")
        speaking = rms(pcm) >= self.threshold
        if not self.active:
            self.pre_roll.append(pcm)
            self.consecutive_voice = self.consecutive_voice + 1 if speaking else 0
            if self.consecutive_voice >= 2:
                self.active = True
                self.chunks = list(self.pre_roll)
                self.voice_frames = self.consecutive_voice
            return None
        self.chunks.append(pcm)
        self.voice_frames += int(speaking)
        self.quiet_frames = 0 if speaking else self.quiet_frames + 1
        capped = len(self.chunks) >= self.max_frames
        if not capped and self.quiet_frames < self.silence_frames:
            return None
        result = b"".join(self.chunks) if self.voice_frames >= 8 else b""
        self.last_reason = "limit" if capped else "silence"
        self.active = False
        self.chunks = []
        self.pre_roll.clear()
        self.consecutive_voice = self.voice_frames = self.quiet_frames = 0
        return result


def listen_once(device, model, stop_event, notify, preview, threshold=180, on_utterance=None):
    """Return (PCM, local transcript) after closing the microphone, or None."""
    import sounddevice as sd
    from vosk import KaldiRecognizer
    recognizer = KaldiRecognizer(model, RATE)
    segmenter = Segmenter(threshold=threshold)
    frames = queue.Queue(maxsize=100)
    failed = threading.Event()
    discontinuity = threading.Event()
    finished = threading.Event()
    committed = []
    previous_preview = ""
    last_frame = time.monotonic()
    tick = 0

    def callback(data, count, timing, status):
        if stop_event.is_set():
            raise sd.CallbackStop
        if count != FRAME_SAMPLES or (status and not getattr(status, 'input_overflow', False)):
            failed.set()
            raise sd.CallbackAbort
        if getattr(status, 'input_overflow', False):
            discontinuity.set()
        try:
            frames.put_nowait(bytes(data))
        except queue.Full:
            # A brief CPU stall must not permanently close the microphone.
            # The main loop discards the damaged utterance and resumes cleanly.
            discontinuity.set()

    with sd.RawInputStream(device=device, samplerate=RATE, channels=1, dtype="int16",
                           blocksize=FRAME_SAMPLES, callback=callback,
                           finished_callback=finished.set):
        notify("正在听，请说话；停顿约0.9秒后自动提交。")
        while not stop_event.is_set():
            if discontinuity.is_set():
                discontinuity.clear()
                while True:
                    try:
                        frames.get_nowait()
                    except queue.Empty:
                        break
                recognizer = KaldiRecognizer(model, RATE)
                segmenter = Segmenter(threshold=threshold)
                committed = []
                previous_preview = ''
                preview('')
                notify('收音短暂丢帧，已自动恢复；刚才未完成的一句请重说。')
                last_frame = time.monotonic()
            if failed.is_set():
                raise RuntimeError("麦克风音频流丢帧，请重新开始或换电脑麦克风。")
            try:
                pcm = frames.get(timeout=0.1)
            except queue.Empty:
                if finished.is_set() or time.monotonic() - last_frame > 3:
                    raise RuntimeError("麦克风音频流已中断，请检查所选录音设备或连接。")
                continue
            last_frame = time.monotonic()
            packet = segmenter.push(pcm)
            if recognizer.AcceptWaveform(pcm):
                text = json.loads(recognizer.Result()).get("text", "")
                if segmenter.active or packet is not None:
                    committed.append(text)
            tick += 1
            if tick % 8 == 0:
                text = " ".join(committed + [json.loads(recognizer.PartialResult()).get("partial", "")]).strip()
                if text != previous_preview:
                    preview(text)
                    previous_preview = text
            if packet is not None:
                local_text = " ".join(committed + [json.loads(recognizer.FinalResult()).get("text", "")]).strip()
                if packet and (local_text or on_utterance is not None):
                    if segmenter.last_reason == "limit":
                        notify("单句已达到30秒上限，提交当前这段语音。")
                    if on_utterance is None:
                        return packet, local_text
                    # Callback only enqueues work. Keep this same microphone open.
                    on_utterance(packet, local_text)
                else:
                    notify("没有识别到清晰语音，继续听。")
                recognizer = KaldiRecognizer(model, RATE)
                committed = []
                previous_preview = ""
    return None
