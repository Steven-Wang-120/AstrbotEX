from pathlib import Path
import threading
import time
import math
import pytest
import yaml
from jetmax_joint_nodes.backends import FakeBackend
from jetmax_joint_nodes.session import SharedSession


def config():
    return yaml.safe_load((Path(__file__).parents[1] / 'config/fake.yaml').read_text())


def until(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.005)
    assert predicate(), 'Condition did not become true before deadline'


@pytest.fixture
def session():
    bus = FakeBackend()
    s = SharedSession(bus, config(), control_enabled=True)
    s.start()
    until(lambda: all(s.snapshot(n)['feedback_valid'] for n in ('arm_base', 'arm_shoulder', 'arm_elbow')))
    yield s
    assert s.close()


def test_start_readonly_and_no_startup_motion():
    bus = FakeBackend()
    s = SharedSession(bus, config())
    s.start()
    try:
        until(lambda: s.snapshot('arm_base')['feedback_valid'])
        with pytest.raises(ValueError, match='disabled'):
            s.reserve('arm_base', 510, 100)
        assert s.stop('arm_base')[0] is False
        assert bus.events == [] and bus.open_count == 1
    finally:
        s.close()


def test_parallel_goals_and_same_joint_busy(session):
    a = session.reserve('arm_shoulder', 550, 500)
    b = session.reserve('arm_elbow', 650, 500)
    session.activate(a)
    session.activate(b)
    with pytest.raises(ValueError, match='busy'):
        session.reserve('arm_shoulder', 510, 100)
    until(lambda: a.written and b.written)
    assert not a.done.is_set() and not b.done.is_set()
    assert a.done.wait(2) and b.done.wait(2)
    assert a.outcome == b.outcome == 'target_observed'
    assert session.backend.open_count == 1


def test_cancel_only_one_joint(session):
    a = session.reserve('arm_shoulder', 600, 700)
    b = session.reserve('arm_elbow', 660, 500)
    session.activate(a)
    session.activate(b)
    until(lambda: a.written and b.written)
    assert session.cancel(a)
    assert a.done.wait(1) and b.done.wait(2)
    assert a.outcome == 'cancelled' and b.outcome == 'target_observed'
    assert ('stop', 2) in session.backend.events and ('stop', 3) not in session.backend.events
    assert not a.final_state['physical_stop_confirmed']
    assert session.cancel(a) is False


def test_cancel_pending_never_sends_old_goal(session):
    op = session.reserve('arm_base', 550, 100)
    assert session.cancel(op)
    session.activate(op)
    time.sleep(.05)
    assert op.outcome == 'cancelled'
    assert not any(e[0] == 'move' for e in session.backend.events)


@pytest.mark.parametrize('target,duration', [(901, 100), (True, 100), (510, 0), (510, 30001), (510.5, 100)])
def test_reject_invalid_goals(session, target, duration):
    with pytest.raises(ValueError):
        session.reserve('arm_base', target, duration)
    assert not session.backend.events


def test_missing_config_rejects_motion_but_allows_reading():
    cfg = config()
    cfg['joints']['arm_base']['tolerance_raw'] = None
    s = SharedSession(FakeBackend(), cfg, control_enabled=True)
    s.start()
    try:
        until(lambda: s.snapshot('arm_base')['feedback_valid'])
        with pytest.raises(ValueError, match='pending'):
            s.reserve('arm_base', 550, 100)
    finally:
        s.close()


def test_gripper_result_is_only_command_evidence(session):
    op = session.reserve('gripper', 75.0)
    session.activate(op)
    assert op.done.wait(1) and op.outcome == 'command_written'
    state = op.final_state
    assert state['last_written_deg'] == 75 and state['command_written']
    assert not state['feedback_valid'] and not state['measured_angle_valid']
    assert math.isnan(state['measured_angle_deg'])
    assert session.stop('gripper')[0]
    assert session.snapshot('gripper')['stop_state'] == 'physical_stop_unsupported'
    assert session.backend.events == [('pwm', 2, 75)]


def test_feedback_loss_stops_affected_joint(session):
    op = session.reserve('arm_base', 650, 1000)
    session.activate(op)
    until(lambda: op.written)
    session.backend.silent.add(1)
    assert op.done.wait(1)
    assert op.outcome == 'feedback_lost'
    assert ('stop', 1) in session.backend.events
    assert not session.snapshot('arm_base')['feedback_valid']


def test_precommand_or_sparse_feedback_cannot_complete(session):
    # Target equals the pre-command sample: it still needs a post-write window.
    op = session.reserve('arm_base', 500, 100)
    session.activate(op)
    until(lambda: op.written)
    assert not op.done.is_set()
    session.backend.silent.add(1)
    assert op.done.wait(1) and op.outcome != 'target_observed'


def test_moving_but_stopped_packet_never_claims_physical_stop(session):
    session.backend.stop = lambda sid, timeout: session.backend.events.append(('stop', sid))
    op = session.reserve('arm_base', 600, 1500)
    session.activate(op)
    until(lambda: op.written)
    session.cancel(op)
    assert op.done.wait(1)
    before = session.backend._position(1)
    time.sleep(.08)
    assert session.backend._position(1) > before
    assert not session.snapshot('arm_base')['physical_stop_confirmed']


def test_goal_timeout_stops_and_does_not_retry(session):
    session.backend.move = lambda sid, target, duration, timeout: session.backend.events.append(('move', sid, target, duration))
    op = session.reserve('arm_base', 550, 10)
    session.activate(op)
    assert op.done.wait(2) and op.outcome == 'timeout'
    assert [e[0] for e in session.backend.events] == ['move', 'stop']


def test_blocked_write_latches_fault_and_cannot_replay():
    bus = FakeBackend()
    entered, release = threading.Event(), threading.Event()
    def blocked(target, timeout):
        bus.events.append(('pwm_attempt', target))
        entered.set()
        release.wait(2)
    bus.pwm = blocked
    s = SharedSession(bus, config(), control_enabled=True)
    s.start()
    try:
        op = s.reserve('gripper', 75.0)
        s.activate(op)
        assert entered.wait(1)
        assert op.done.wait(.5) and op.outcome == 'io_error'
        with pytest.raises(ValueError):
            s.reserve('gripper', 60.0)
        assert len(bus.events) == 1
    finally:
        release.set()
        assert s.close()


def test_reopen_has_no_command_history():
    bus = FakeBackend()
    first = SharedSession(bus, config(), control_enabled=True)
    first.start()
    op = first.reserve('gripper', 75.0)
    first.activate(op)
    assert op.done.wait(1)
    first.close()
    bus.events.clear()
    second = SharedSession(bus, config(), control_enabled=True)
    second.start()
    try:
        time.sleep(.1)
        assert bus.events == [] and second.snapshot('gripper')['goal_id'] == ''
    finally:
        second.close()


def test_fake_profile_cannot_be_used_on_real():
    from jetmax_joint_nodes.backends import RealBackend
    with pytest.raises(ValueError, match='profile_kind'):
        SharedSession(RealBackend(config()), config(), control_enabled=True)


def test_one_thread_owns_all_io_and_stop_has_priority(session):
    bus = session.backend
    ids = set()
    original_read, original_move, original_stop = bus.read, bus.move, bus.stop
    def observed_read(*args):
        ids.add(threading.get_ident())
        return original_read(*args)
    def observed_move(*args):
        ids.add(threading.get_ident())
        return original_move(*args)
    def observed_stop(*args):
        ids.add(threading.get_ident())
        return original_stop(*args)
    bus.read, bus.move, bus.stop = observed_read, observed_move, observed_stop
    first = session.reserve('arm_base', 600, 500)
    session.activate(first)
    until(lambda: first.written)
    # Hold the scheduler lock until both queues are populated.
    with session.lock:
        second = session.reserve('arm_elbow', 650, 100)
        session.activate(second)
        session.cancel(first)
    assert first.done.wait(1) and second.done.wait(2)
    events = [(e[0], e[1]) for e in bus.events]
    assert events.index(('stop', 1)) < events.index(('move', 3))
    assert len(ids) == 1


def test_shutdown_stops_active_goal_and_fresh_start_never_replays(session):
    op = session.reserve('arm_base', 700, 1000)
    session.activate(op)
    until(lambda: op.written)
    assert session.close()
    assert op.done.is_set() and op.outcome == 'shutdown'
    assert ('stop', 1) in session.backend.events


def test_cancel_gripper_while_write_inflight_never_releases(session):
    entered, release = threading.Event(), threading.Event()
    original = session.backend.pwm
    def delayed(*args):
        entered.set()
        release.wait(.04)
        original(*args)
    session.backend.pwm = delayed
    op = session.reserve('gripper', 75.0)
    session.activate(op)
    assert entered.wait(1)
    assert session.cancel(op)
    with pytest.raises(ValueError, match='busy'):
        session.reserve('gripper', 60.0)
    release.set()
    assert op.done.wait(1) and op.outcome == 'cancelled'
    assert session.backend.events == [('pwm', 2, 75)]
    assert op.final_state['stop_state'] == 'physical_stop_unsupported'
