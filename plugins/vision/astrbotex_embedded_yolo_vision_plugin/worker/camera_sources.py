from __future__ import annotations

import io
import time
import urllib.request
from typing import Any, Iterator


class MjpegReader:
    """Streaming JPEG parser with bounded frames and reconnect backoff."""

    def __init__(self, url: str, *, timeout_sec: float = 5.0, max_frame_bytes: int = 8 * 1024 * 1024, reconnect_sec: float = 2.0) -> None:
        self.url = url
        self.timeout_sec = max(0.1, timeout_sec)
        self.max_frame_bytes = max(1024, max_frame_bytes)
        self.reconnect_sec = max(0.0, reconnect_sec)
        self._response: Any | None = None
        self._buffer = bytearray()
        self._next_connect_at = 0.0

    def close(self) -> None:
        if self._response is not None:
            try:
                self._response.close()
            except Exception:
                pass
        self._response = None
        self._buffer.clear()

    def _connect(self) -> None:
        request = urllib.request.Request(self.url, headers={"Accept": "multipart/x-mixed-replace, image/jpeg", "User-Agent": "astrbotex-embedded-yolo"})
        self._response = urllib.request.urlopen(request, timeout=self.timeout_sec)

    def read_jpeg(self) -> bytes:
        while True:
            if self._response is None:
                if time.monotonic() < self._next_connect_at:
                    time.sleep(min(0.05, self._next_connect_at - time.monotonic()))
                try:
                    self._connect()
                except Exception:
                    self._next_connect_at = time.monotonic() + self.reconnect_sec
                    continue
            start = self._buffer.find(b"\xff\xd8")
            if start >= 0:
                end = self._buffer.find(b"\xff\xd9", start + 2)
                if end >= 0:
                    frame = bytes(self._buffer[start:end + 2])
                    del self._buffer[:end + 2]
                    if len(frame) <= self.max_frame_bytes:
                        return frame
                    del self._buffer[:]
                    continue
            if len(self._buffer) > self.max_frame_bytes:
                marker = self._buffer.rfind(b"\xff\xd8")
                del self._buffer[: marker if marker >= 0 else len(self._buffer)]
            try:
                chunk = self._response.read(65536)
            except Exception:
                self.close()
                self._next_connect_at = time.monotonic() + self.reconnect_sec
                continue
            if not chunk:
                self.close()
                self._next_connect_at = time.monotonic() + self.reconnect_sec
                continue
            self._buffer.extend(chunk)


class FrameSource:
    def __init__(self, mode: str, source: str, **options: Any) -> None:
        self.mode = mode
        self.source = source
        self.options = options
        self._mjpeg = MjpegReader(source, timeout_sec=float(options.get("timeout_sec", 5.0)), max_frame_bytes=int(options.get("max_frame_bytes", 8 * 1024 * 1024)), reconnect_sec=float(options.get("reconnect_sec", 2.0))) if mode == "mjpeg_http" else None
        self._capture: Any | None = None

    def read(self) -> Any:
        if self.mode == "mjpeg_http":
            data = self._mjpeg.read_jpeg()
            try:
                import cv2
                import numpy as np
            except ImportError as exc:
                raise RuntimeError("opencv-python and numpy are required in the worker") from exc
            frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError("MJPEG stream returned an undecodable JPEG")
            return frame
        if self.mode in {"camera_index", "video_file", "rtsp"}:
            try:
                import cv2
            except ImportError as exc:
                raise RuntimeError("opencv-python is required in the worker") from exc
            if self._capture is None:
                value: int | str = int(self.source) if self.mode == "camera_index" and self.source.lstrip("+-").isdigit() else self.source
                self._capture = cv2.VideoCapture(value)
                if not self._capture.isOpened():
                    raise RuntimeError(f"unable to open {self.mode} source: {self.source}")
            ok, frame = self._capture.read()
            if not ok or frame is None:
                raise RuntimeError("frame source returned no frame")
            return frame
        raise ValueError(f"unsupported source mode: {self.mode}")

    def close(self) -> None:
        if self._mjpeg is not None:
            self._mjpeg.close()
        if self._capture is not None:
            self._capture.release()
            self._capture = None
