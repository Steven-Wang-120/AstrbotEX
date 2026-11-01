import ctypes
import struct
import subprocess
from pathlib import Path
import pytest
from jetmax_joint_nodes.backends import RealBackend, QueryTrace
from jetmax_joint_nodes.protocol import FrameParser, encode_frame, POSITION_READ, MOVE_TIME_WRITE, MOVE_STOP


def test_echo_bad_checksum_wrong_id_and_partial_frames():
    parser = FrameParser()
    echo = encode_frame(1, POSITION_READ)
    reply = encode_frame(1, POSITION_READ, struct.pack('<h', 537))
    corrupt = reply[:-1] + bytes([reply[-1] ^ 1])
    frames = []
    for part in [b'noise' + echo[:3], echo[3:] + corrupt, reply[:4], reply[4:]]:
        frames.extend(parser.feed(part))
    replies = [f for f in frames if f.servo_id == 1 and f.command == POSITION_READ and len(f.payload) == 2]
    assert len(replies) == 1 and struct.unpack('<h', replies[0].payload)[0] == 537


def test_directed_motion_and_stop_packets_only():
    backend = RealBackend({'serial_device': '/not/opened'})
    sent = []
    backend._send = lambda packet, timeout: sent.append(packet)
    backend.move(2, 550, 400, .05)
    backend.stop(2, .05)
    frames = [FrameParser().feed(p)[0] for p in sent]
    assert frames[0].servo_id == 2 and frames[0].command == MOVE_TIME_WRITE
    assert struct.unpack('<HH', frames[0].payload) == (550, 400)
    assert frames[1].command == MOVE_STOP and frames[1].payload == b''
    with pytest.raises(ValueError):
        backend.stop(254, .05)
    assert backend.serial is None


def test_compiled_native_query_rejects_invalid_handles_without_io():
    from ament_index_python.packages import get_package_prefix
    library = ctypes.CDLL(str(Path(get_package_prefix('jetmax_joint_nodes')) / 'lib/jetmax_joint_nodes/libjetmax_position_query.so'))
    query = library.jm_write_position_query
    query.argtypes = [ctypes.c_int] * 5 + [ctypes.POINTER(QueryTrace)]
    query.restype = ctypes.c_int
    trace = QueryTrace()
    assert query(-1, -1, 1, 1, 50000, ctypes.byref(trace)) == -22
    assert trace.bytes_written == 0 and trace.tx_on_ns == 0


@pytest.fixture(scope='module')
def native_fake(tmp_path_factory):
    root = Path(__file__).parents[1]
    output = tmp_path_factory.mktemp('native') / 'query.so'
    subprocess.run(['cc', '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror', '-DJETMAX_QUERY_TEST',
                    str(root / 'native/native_query.c'), str(root / 'test/native_query_fake.c'),
                    '-o', str(output)], check=True)
    lib = ctypes.CDLL(str(output))
    lib.jm_write_position_query.argtypes = [ctypes.c_int] * 5 + [ctypes.POINTER(QueryTrace)]
    lib.jm_write_position_query.restype = ctypes.c_int
    return lib


@pytest.mark.parametrize('mode,limit,expected', [(0, 0, 0), (0, 2, 0), (1, 0, -110), (2, 0, -5)])
def test_native_query_echo_partial_write_and_timeout_restore(native_fake, mode, limit, expected):
    lib = native_fake
    lib.jm_test_reset(mode, limit)
    trace = QueryTrace()
    assert lib.jm_write_position_query(10, 11, 3, 1, 50000, ctypes.byref(trace)) == expected
    transitions = lib.jm_test_transitions()
    assert transitions == 2
    assert (lib.jm_test_state(0, 0), lib.jm_test_state(0, 1)) == (1, 1)
    assert (lib.jm_test_state(1, 0), lib.jm_test_state(1, 1)) == (1, 0)
    if mode != 2:
        assert bytes(lib.jm_test_byte(i) for i in range(lib.jm_test_count())) == encode_frame(3, POSITION_READ)
