"""Real and explicitly selected test backends. Neither opens devices on import."""
import ctypes
import fcntl
import math
import os
from pathlib import Path
import select
import struct
import time
from types import SimpleNamespace

from .protocol import encode_frame, FrameParser, POSITION_READ, MOVE_TIME_WRITE, MOVE_STOP


class FakeBackend:
    kind = 'fake'

    def __init__(self):
        self.open_count = 0
        self.events = []
        self.positions = {1: 500, 2: 500, 3: 600}
        self.moves = {}
        self.silent = set()
        self.connected = False

    def open(self):
        self.open_count += 1
        self.connected = True

    def close(self):
        self.connected = False

    def _position(self, sid):
        if sid in self.moves:
            start, target, when, duration = self.moves[sid]
            ratio = min(1.0, (time.monotonic() - when) / duration)
            self.positions[sid] = round(start + (target - start) * ratio)
        return self.positions[sid]

    def read(self, sid, timeout):
        if sid in self.silent:
            raise TimeoutError('Injected missing feedback')
        return self._position(sid)

    def move(self, sid, target, duration_ms, timeout):
        self.events.append(('move', sid, target, duration_ms))
        self.moves[sid] = (self._position(sid), target, time.monotonic(), duration_ms / 1000)

    def stop(self, sid, timeout):
        self.events.append(('stop', sid))
        self._position(sid)
        self.moves.pop(sid, None)

    def pwm(self, target, timeout):
        self.events.append(('pwm', 2, round(target)))


class QueryTrace(ctypes.Structure):
    _fields_ = [(k, ctypes.c_uint64) for k in ('tx_on_ns', 'write_done_ns', 'tx_empty_ns', 'rx_on_ns')] + [
        ('bytes_written', ctypes.c_uint32), ('polls', ctypes.c_uint32),
        ('error', ctypes.c_int32), ('restore_error', ctypes.c_int32)]


class RealBackend:
    kind = 'real'

    def __init__(self, config, *, library_path=None):
        self.device = config['serial_device']
        self.library_path = library_path
        self.serial = self.gpio = self.i2c = self.query = None
        self.lock_fd = None

    def open(self):
        import serial
        from .grouped_gpio import GroupedDirectionGPIO
        # This application lock complements TIOCEXCL, including privileged peer processes.
        import hashlib
        key = hashlib.sha256(os.path.realpath(self.device).encode()).hexdigest()[:16]
        self.lock_fd = os.open('/tmp/jetmax-uart-' + key + '.lock',
                               os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.serial = serial.Serial(self.device, 115200, timeout=0, write_timeout=.05, exclusive=True)
        fcntl.ioctl(self.serial.fileno(), 0x540C)  # TIOCEXCL; advisory flock alone is insufficient.
        flags = fcntl.fcntl(self.serial.fileno(), fcntl.F_GETFL)
        fcntl.fcntl(self.serial.fileno(), fcntl.F_SETFL, flags | os.O_NONBLOCK)
        profile = SimpleNamespace(raw={'serial': {'rx_bcm': 17, 'tx_bcm': 27,
            'rx_levels': {'write': 0, 'read': 1}, 'tx_levels': {'write': 1, 'read': 0}}})
        self.gpio = GroupedDirectionGPIO(profile)
        self.gpio.setmode(self.gpio.BCM)
        self.gpio.setup(17, self.gpio.OUT, initial=1)
        self.gpio.setup(27, self.gpio.OUT, initial=0)
        if self.library_path is None:
            from ament_index_python.packages import get_package_prefix
            self.library_path = str(Path(get_package_prefix('jetmax_joint_nodes')) /
                                    'lib/jetmax_joint_nodes/libjetmax_position_query.so')
        self.library = ctypes.CDLL(self.library_path)
        self.query = self.library.jm_write_position_query
        self.query.argtypes = [ctypes.c_int] * 5 + [ctypes.POINTER(QueryTrace)]
        self.query.restype = ctypes.c_int

    def close(self):
        # This is called only after the single I/O worker has exited.
        try:
            if self.i2c is not None:
                self.i2c.close()
        finally:
            try:
                if self.gpio is not None:
                    self.gpio.cleanup([17, 27])
            finally:
                try:
                    if self.serial is not None:
                        self.serial.close()
                finally:
                    if self.lock_fd is not None:
                        os.close(self.lock_fd)
                        self.lock_fd = None
        self.i2c = self.gpio = self.serial = None

    def read(self, sid, timeout):
        if sid not in (1, 2, 3):
            raise ValueError('Unknown servo')
        deadline = time.monotonic() + timeout
        self.serial.reset_input_buffer()
        trace = QueryTrace()
        rc = self.query(self.serial.fileno(), self.gpio._handle, sid, 1,
                        max(100, int((deadline - time.monotonic()) * 1e6)), ctypes.byref(trace))
        if rc:
            if -rc == 110:
                raise TimeoutError('Native position query timed out')
            raise OSError(-rc, 'Native position query failed')
        parser = FrameParser()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.serial.fileno()], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            for frame in parser.feed(self.serial.read(256)):
                # Six-byte query echo has no payload and must never become feedback.
                if frame.servo_id == sid and frame.command == POSITION_READ and len(frame.payload) == 2:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Position reply after deadline')
                    return struct.unpack('<h', frame.payload)[0]
        raise TimeoutError('No matching position reply')

    def _send(self, packet, timeout):
        """Non-response commands: complete TX drain before releasing the pair to RX."""
        deadline = time.monotonic() + timeout
        try:
            self.gpio.output(27, 1)
            self.gpio.output(17, 0)
            pending = memoryview(packet)
            while pending:
                if time.monotonic() >= deadline:
                    raise TimeoutError('UART write deadline')
                try:
                    size = os.write(self.serial.fileno(), pending)
                    pending = pending[size:]
                except BlockingIOError:
                    select.select([], [self.serial.fileno()], [], max(0, deadline - time.monotonic()))
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError('UART physical TX drain deadline')
                queued = struct.unpack('i', fcntl.ioctl(self.serial.fileno(), 0x5411, struct.pack('i', 0)))[0]
                status = struct.unpack('i', fcntl.ioctl(self.serial.fileno(), 0x5459, struct.pack('i', 0)))[0]
                if queued == 0 and status & 1:
                    break
                time.sleep(.00005)
        finally:
            self.gpio.output(17, 1)
            self.gpio.output(27, 0)

    def move(self, sid, target, duration_ms, timeout):
        if sid not in (1, 2, 3) or type(target) is not int or not 0 <= target <= 1000:
            raise ValueError('Invalid raw target')
        if type(duration_ms) is not int or not 10 <= duration_ms <= 30000:
            raise ValueError('Invalid duration')
        self._send(encode_frame(sid, MOVE_TIME_WRITE, struct.pack('<HH', target, duration_ms)), timeout)

    def stop(self, sid, timeout):
        if sid not in (1, 2, 3):
            raise ValueError('Unknown servo')
        self._send(encode_frame(sid, MOVE_STOP), timeout)

    def pwm(self, target, timeout):
        if isinstance(target, bool) or not math.isfinite(target) or not 0 <= target <= 180:
            raise ValueError('Invalid PWM command angle')
        if self.i2c is None:
            from smbus2 import SMBus
            self.i2c = SMBus(1)
        # Kernel ioctl has no Python cancellation. SharedSession bounds observation
        # and latches a fault if this call does not return in time. Never retry it.
        self.i2c.write_word_data(0x36, 22, round(target))
