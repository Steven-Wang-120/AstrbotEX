"""PCM WAV playback with a silent tail for Bluetooth transport buffers."""
import math
import struct
import wave

DEFAULT_TAIL_SECONDS = 1.0


def play_pcm_chunks(chunks, device, cancel_event, on_first_audio=None, stream_factory=None):
    """Play arriving PCM on one stream, keeping tail padding only at turn end."""
    if stream_factory is None:
        import sounddevice
        stream_factory = sounddevice.RawOutputStream
    stream = None
    pending = b''
    try:
        for chunk in chunks:
            if cancel_event.is_set():
                if stream: stream.abort(ignore_errors=False)
                return False
            pending += chunk
            count = len(pending)//2*2
            if not count:
                continue
            data,pending = pending[:count],pending[count:]
            if stream is None:
                stream = stream_factory(device=device,samplerate=24000,channels=1,dtype='int16',latency='high')
                stream.start()
            samples = struct.unpack('<{}h'.format(len(data)//2),data)
            stream.write(struct.pack('<{}h'.format(len(samples)),*[int(s*.4) for s in samples]))
            if on_first_audio:
                on_first_audio();on_first_audio=None
        if cancel_event.is_set():
            if stream:stream.abort(ignore_errors=False)
            return False
        if pending:
            raise ValueError('PCM stream ended with an incomplete sample')
        if stream is None:
            raise ValueError('PCM stream was empty')
        for _ in range(24):
            if cancel_event.is_set():
                stream.abort(ignore_errors=False);return False
            stream.write(b'\0'*2000)
        stream.stop(ignore_errors=False)
        return True
    finally:
        if stream:stream.close()


def play_wav(path, device, tail_seconds=DEFAULT_TAIL_SECONDS, volume=0.4, stream_factory=None,
             cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        return False
    if not math.isfinite(tail_seconds) or not 0 <= tail_seconds <= 3:
        raise ValueError("播放尾部静音必须在 0～3 秒之间。")
    if not math.isfinite(volume) or not 0 <= volume <= 1:
        raise ValueError("播放音量必须在 0～1 之间。")
    if stream_factory is None:
        import sounddevice
        stream_factory = sounddevice.RawOutputStream
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        rate = audio.getframerate()
        if audio.getsampwidth() != 2 or channels not in (1, 2) or audio.getcomptype() != "NONE":
            raise ValueError("播放需要 16bit PCM 单声道或双声道 WAV。")
        if not audio.getnframes():
            raise ValueError("播放音频为空。")
        stream = stream_factory(device=device, samplerate=rate, channels=channels,
                                dtype="int16", latency="high")
        try:
            stream.start()
            played_frames = 0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    stream.abort(ignore_errors=False)
                    return False
                data = audio.readframes(2048)
                if not data:
                    break
                if len(data) % (channels * 2):
                    raise ValueError("WAV 末尾存在不完整的采样帧。")
                played_frames += len(data) // (channels * 2)
                samples = struct.unpack("<{}h".format(len(data) // 2), data)
                stream.write(struct.pack("<{}h".format(len(samples)),
                                         *[int(sample * volume) for sample in samples]))
            if played_frames != audio.getnframes():
                raise ValueError("WAV 播放数据不完整。")
            # Keep the SAME stream/device running past the final syllable.
            # Sleeping after close cannot flush Bluetooth's downstream buffers.
            remaining = round(rate * tail_seconds)
            silence = b"\0" * (2048 * channels * 2)
            while remaining:
                if cancel_event is not None and cancel_event.is_set():
                    stream.abort(ignore_errors=False)
                    return False
                count = min(2048, remaining)
                stream.write(silence[:count * channels * 2])
                remaining -= count
            # Drain PortAudio too; report a failed drain instead of ignoring it.
            stream.stop(ignore_errors=False)
            return True
        finally:
            stream.close()
