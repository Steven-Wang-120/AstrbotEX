"""Hardware-free Lobot packet codec and injected, bounded transaction client.

No serial/GPIO module is imported. Transport methods MUST honor their timeout;
this module cannot interrupt an arbitrary blocking driver. Stop writes use only
the short write lock, independent of a waiting read transaction. The wire has no
transaction identifier: delayed identical-ID/command replies cannot be perfectly
correlated; drain-before-request and transaction serialization reduce that risk.
"""
from dataclasses import dataclass
from collections import deque
import math
import struct
import threading
import time
from typing import Protocol
from .validation import bounded, finite, integer

MOVE_TIME_WRITE = 1
MOVE_STOP = 12
ID_READ = 14
VIN_LIMIT_READ = 23
POSITION_READ = 28
BROADCAST_ID = 254


class ProtocolError(ValueError):
    pass


class ServoTimeout(TimeoutError):
    pass


class ServoCancelled(RuntimeError):
    pass


def _servo_id(servo_id, broadcast=False):
    return integer(servo_id, 1, BROADCAST_ID if broadcast else 253, "servo_id")


@dataclass(frozen=True)
class ServoFrame:
    servo_id: int
    command: int
    payload: bytes = b""


def encode_frame(servo_id, command, payload=b""):
    _servo_id(servo_id, broadcast=True)
    integer(command, 0, 255, "command")
    if not isinstance(payload, bytes) or len(payload) > 64:
        raise ValueError("payload must be bytes, at most 64 bytes")
    body = bytes((servo_id,len(payload)+3,command))+payload
    return b"\x55\x55"+body+bytes(((~sum(body)) & 255,))


def decode_frame(packet):
    if not isinstance(packet, bytes) or len(packet) < 6 or packet[:2] != b"\x55\x55":
        raise ProtocolError("invalid or short header")
    length = packet[3]
    if not 3 <= length <= 67 or len(packet) != length+3:
        raise ProtocolError("invalid frame length")
    if not 1 <= packet[2] <= BROADCAST_ID:
        raise ProtocolError("invalid reply ID")
    if (sum(packet[2:]) & 255) != 255:
        raise ProtocolError("checksum mismatch")
    return ServoFrame(packet[2],packet[4],packet[5:-1])


class FrameParser:
    """Incremental framing, bounded memory, checksum validation and resync."""
    def __init__(self):
        self.buffer = bytearray()
        self.rejected_frames = 0

    def feed(self, data):
        if not isinstance(data, bytes):
            raise ValueError("parser input must be bytes")
        self.buffer.extend(data)
        frames = []
        while self.buffer:
            index = self.buffer.find(b"\x55\x55")
            if index < 0:
                self.buffer[:] = b"\x55" if self.buffer[-1:] == b"\x55" else b""
                break
            if index:
                del self.buffer[:index]
            if len(self.buffer) < 4:
                break
            length = self.buffer[3]
            if not 3 <= length <= 67:
                del self.buffer[0]
                self.rejected_frames += 1
                continue
            total = length+3
            if len(self.buffer) < total:
                # A truncated frame must not hide a complete, checksum-valid
                # reply later in the stream. A raw header alone is insufficient.
                later = self.buffer.find(b"\x55\x55",2)
                recovered = False
                while later >= 0 and len(self.buffer)-later >= 6:
                    end = later+self.buffer[later+3]+3
                    if end <= len(self.buffer):
                        try:
                            decode_frame(bytes(self.buffer[later:end]))
                        except ProtocolError:
                            pass
                        else:
                            del self.buffer[:later]
                            self.rejected_frames += 1
                            recovered = True
                            break
                    later = self.buffer.find(b"\x55\x55",later+1)
                if recovered:
                    continue
                break
            try:
                frame = decode_frame(bytes(self.buffer[:total]))
            except ProtocolError:
                del self.buffer[0]
                self.rejected_frames += 1
                continue
            frames.append(frame)
            del self.buffer[:total]
        return tuple(frames)
