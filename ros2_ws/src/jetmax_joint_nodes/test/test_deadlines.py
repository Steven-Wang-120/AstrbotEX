import math
import time
from pathlib import Path

import pytest
import yaml
from jetmax_joint_nodes.backends import FakeBackend
from jetmax_joint_nodes.session import SharedSession


def make(cap='absent'):
    cfg = yaml.safe_load((Path(__file__).parents[1] / 'config/fake.yaml').read_text())
    if cap != 'absent':
        cfg['goal_timeout_cap_s'] = cap
    session = SharedSession(FakeBackend(), cfg, control_enabled=True)
    # Deterministic scheduler unit: no hardware or background threads.
    session.running = True
    for joint in session.joints.values():
        joint.valid = True
        joint.receipt = time.monotonic()
    return session


@pytest.mark.parametrize('cap', [0, -1, True, float('nan'), float('inf'), '1'])
def test_invalid_cap(cap):
    with pytest.raises(ValueError):
        make(cap)


@pytest.mark.parametrize('cap', ['absent', None, 100.0])
def test_default_or_large_cap_cannot_extend_deadline(cap):
    session = make(cap)
    before = time.monotonic()
    op = session.reserve('arm_base', 540, 4000)
    after = time.monotonic()
    expected = 4 + session.cfg['goal_margin_s'] + session.cfg['joints']['arm_base']['settled_ms'] / 1000
    assert before + expected <= op.deadline <= after + expected


def test_cap_only_affects_arm():
    session = make(.5)
    before = time.monotonic()
    arm = session.reserve('arm_shoulder', 540, 4000)
    gripper = session.reserve('gripper', 75.)
    after = time.monotonic()
    assert before + .5 <= arm.deadline <= after + .5
    assert before + session.cfg['goal_margin_s'] <= gripper.deadline <= after + session.cfg['goal_margin_s']


def test_late_target_reply_beats_monitor_but_cannot_succeed():
    session = make(.5)
    op = session.reserve('arm_base', 540, 4000)
    op.attempted = op.written = True
    op.sent_at = time.monotonic() - 2
    op.deadline = time.monotonic() - .01
    op.settled_since = time.monotonic() - 1
    session.backend.positions[1] = 540
    session._perform('read', 'arm_base')
    assert session.snapshot('arm_base')['position_raw'] == 540
    assert op.ending == 'timeout' and not op.done.is_set()
    assert list(session.stops) == ['arm_base']
    session._perform('stop', session.stops.popleft())
    assert op.done.is_set() and op.outcome == 'timeout'
    assert session.backend.events == [('stop', 1)]


def test_real_scheduler_cap_stops_only_target_and_never_replays():
    session = make(.15)
    session.running = False
    session.start()
    try:
        end = time.monotonic() + 1
        while not session.snapshot('arm_base')['feedback_valid'] and time.monotonic() < end:
            time.sleep(.005)
        op = session.reserve('arm_base', 540, 4000)
        session.activate(op)
        assert op.done.wait(2)
        assert op.written and op.outcome == 'timeout'
        assert session.backend.events == [('move', 1, 540, 4000), ('stop', 1)]
        time.sleep(.2)
        assert len(session.backend.events) == 2
        assert not op.final_state['physical_stop_confirmed']
    finally:
        session.close()
