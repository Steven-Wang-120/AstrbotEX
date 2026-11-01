"""Per-joint goals, one bounded I/O scheduler, and an independent deadline monitor."""
from collections import deque
from dataclasses import dataclass, field
import copy
import math
import threading
import time
import uuid

JOINT_IDS = {'arm_base': 1, 'arm_shoulder': 2, 'arm_elbow': 3, 'gripper': None}
SUCCESS = {'target_observed', 'command_written'}


def number(value, low, high, name, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name}: numeric value required')
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name}: outside [{low}, {high}]')
    if integer and type(value) is not int:
        raise ValueError(f'{name}: integer required')
    return value


def validate_config(config, backend):
    cfg = copy.deepcopy(config)
    if backend not in ('real', 'fake') or cfg.get('profile_kind') != backend:
        raise ValueError('Backend and profile_kind must match; fake limits cannot enable real hardware')
    for key, low, high in [('io_timeout_s', .005, .2), ('poll_period_s', .01, 1),
                           ('feedback_max_age_s', .05, 2), ('goal_margin_s', .1, 10)]:
        number(cfg[key], low, high, key)
    cap = cfg.get('goal_timeout_cap_s')
    if cap is not None:
        number(cap, 0, float('inf'), 'goal_timeout_cap_s')
        if cap <= 0:
            raise ValueError('goal_timeout_cap_s must be positive or null')
    if cfg['feedback_max_age_s'] <= cfg['poll_period_s']:
        raise ValueError('feedback_max_age_s must exceed poll_period_s')
    if set(cfg['joints']) != set(JOINT_IDS):
        raise ValueError('Exactly arm_base, arm_shoulder, arm_elbow and gripper are required')
    for name, entry in cfg['joints'].items():
        limits = entry.get('limits')
        if limits is not None:
            if not isinstance(limits, list) or len(limits) != 2:
                raise ValueError(f'{name}: limits must be [min, max] or null')
            top = 180 if name == 'gripper' else 1000
            for v in limits:
                number(v, 0, top, f'{name}.limits', name != 'gripper')
            if limits[0] >= limits[1]:
                raise ValueError(f'{name}: empty limits')
        if name != 'gripper':
            for key, low, high in [('tolerance_raw', 0, 1000), ('settled_ms', 20, 5000)]:
                if entry.get(key) is not None:
                    number(entry[key], low, high, f'{name}.{key}', True)
            if entry.get('settled_ms') is not None and entry['settled_ms'] < cfg['poll_period_s'] * 1000:
                raise ValueError('Settling requires multiple distinct samples')
    return cfg


@dataclass
class Operation:
    joint: str
    target: float
    duration_ms: int
    generation: int
    deadline: float
    goal_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    attempted: bool = False
    written: bool = False
    sent_at: float = 0.0
    settled_since: float = None
    ending: str = ''
    outcome: str = ''
    message: str = ''
    final_state: dict = None
    done: threading.Event = field(default_factory=threading.Event)


class Joint:
    def __init__(self, name):
        self.name = name
        self.generation = 0
        self.active = None
        self.phase = 'idle'
        self.goal_id = ''
        self.value = None
        self.receipt = 0.0
        self.receipt_wall_ns = 0
        self.query_ms = 0.0
        self.valid = False
        self.target = None
        self.written = False
        self.last_written_deg = float('nan')
        self.stop_state = 'not_requested'
        self.stop_pending = False
        self.error = ''


class SharedSession:
    """The backend is touched only by the I/O thread, after one explicit open.

    A blocked kernel call cannot be revoked by Python. The monitor latches the
    session, completes affected goals as unknown/error and forbids further
    motion; the same worker retains ownership until that call returns.
    """
    def __init__(self, backend, config, *, control_enabled=False):
        self.backend = backend
        self.cfg = validate_config(config, backend.kind)
        self.control_enabled = bool(control_enabled)
        self.joints = {n: Joint(n) for n in JOINT_IDS}
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.commands = deque()
        self.stops = deque()
        self.fault = ''
        self.closing = False
        self.running = False
        self.inflight = None
        self.worker = None
        self.monitor = None

    def start(self):
        with self.lock:
            if self.running or self.closing:
                raise RuntimeError('Session can only be started once')
            try:
                self.backend.open()
            except Exception:
                self.backend.close()
                raise
            self.running = True
            self.worker = threading.Thread(target=self._run, name='jetmax-bus-owner', daemon=True)
            self.monitor = threading.Thread(target=self._monitor, name='jetmax-deadlines', daemon=True)
            self.worker.start()
            self.monitor.start()

    def configured(self, name):
        entry = self.cfg['joints'][name]
        return (entry.get('limits') is not None and
                (name == 'gripper' or all(entry.get(k) is not None for k in ('tolerance_raw', 'settled_ms'))))

    def reserve(self, name, target, duration_ms=0):
        with self.lock:
            joint = self.joints[name]
            if not self.running or self.closing or self.fault:
                raise ValueError(self.fault or 'Session not running')
            if not self.control_enabled:
                raise ValueError('Control disabled; read-only session')
            if not self.configured(name):
                raise ValueError('Motion limits/acceptance configuration pending')
            if joint.active or joint.stop_pending:
                raise ValueError('Joint busy; no queue or replacement')
            lo, hi = self.cfg['joints'][name]['limits']
            number(target, lo, hi, 'target', name != 'gripper')
            if name == 'gripper':
                number(round(target), lo, hi, 'quantized PWM target')
            if name != 'gripper':
                number(duration_ms, 10, 30000, 'duration_ms', True)
                if not self._valid(joint):
                    raise ValueError('No fresh position feedback')
            joint.generation += 1
            timeout = duration_ms / 1000 + self.cfg['goal_margin_s']
            if name != 'gripper':
                timeout += self.cfg['joints'][name]['settled_ms'] / 1000
                cap = self.cfg.get('goal_timeout_cap_s')
                if cap is not None:
                    timeout = min(timeout, cap)
            op = Operation(name, target, duration_ms, joint.generation, time.monotonic() + timeout)
            joint.active = op
            joint.phase = 'reserved'
            joint.goal_id = op.goal_id
            joint.target = target
            joint.written = False
            joint.stop_state = 'not_requested'
            joint.error = ''
            return op

    def activate(self, op, goal_id=None):
        with self.lock:
            joint = self.joints[op.joint]
            if goal_id is not None:
                op.goal_id = goal_id
                joint.goal_id = goal_id
                if op.final_state is not None:
                    op.final_state['goal_id'] = goal_id
            if joint.active is not op or op.done.is_set() or op.ending:
                return
            if joint.phase != 'reserved':
                raise ValueError('Goal already activated')
            joint.phase = 'queued'
            self.commands.append(op)
            self.wake.set()

    def cancel(self, op):
        with self.lock:
            joint = self.joints[op.joint]
            if joint.active is not op or op.done.is_set() or op.ending:
                return False
            self._end(joint, 'cancelled', 'Cancellation accepted; physical stop unconfirmed')
            return True

    def stop(self, name):
        with self.lock:
            joint = self.joints[name]
            if not self.control_enabled or not self.running or self.closing:
                return False, 'Control disabled or session closing'
            if joint.active:
                self._end(joint, 'cancelled', 'Explicit joint stop requested')
            elif not joint.stop_pending:
                joint.generation += 1
                if name == 'gripper':
                    joint.stop_state = 'physical_stop_unsupported'
                else:
                    self._queue_stop(joint)
            return True, 'Follow state.stop_state; no unloading or automatic gripper release'

    def _queue_stop(self, joint):
        if not joint.stop_pending:
            joint.stop_pending = True
            joint.stop_state = 'requested'
            self.stops.append(joint.name)
            self.wake.set()

    def _end(self, joint, outcome, message):
        op = joint.active
        if op is None or op.ending:
            return
        joint.generation += 1
        op.ending = outcome
        op.message = message
        joint.phase = 'cancelling' if outcome == 'cancelled' else 'stopping'
        if joint.name == 'gripper':
            joint.stop_state = 'physical_stop_unsupported' if op.attempted else 'withdrawn_before_write'
            # An in-flight write must finish before this joint can accept another.
            if not op.attempted or self.inflight is None:
                self._finish(joint, op, outcome, message)
        elif op.attempted:
            self._queue_stop(joint)
        else:
            joint.stop_state = 'withdrawn_before_write'
            self._finish(joint, op, outcome, message)

    def _finish(self, joint, op, outcome, message=''):
        if op.done.is_set():
            return
        op.outcome, op.message = outcome, message
        joint.phase = 'idle' if outcome in SUCCESS or outcome == 'cancelled' else 'error'
        if outcome not in SUCCESS and outcome != 'cancelled':
            joint.error = message or outcome
        op.final_state = self.snapshot(joint.name)
        op.done.set()
        if joint.active is op:
            joint.active = None

    def _valid(self, joint):
        return joint.valid and time.monotonic() - joint.receipt <= self.cfg['feedback_max_age_s']

    def snapshot(self, name):
        with self.lock:
            j = self.joints[name]
            arm = name != 'gripper'
            return dict(joint=name, backend=self.backend.kind, phase=j.phase, goal_id=j.goal_id,
                        control_enabled=self.control_enabled and not self.fault and not self.closing,
                        motion_configured=self.configured(name), feedback_valid=self._valid(j),
                        position_raw=j.value if j.value is not None else 0,
                        received_wall_ns=j.receipt_wall_ns,
                        receipt_age_ms=(time.monotonic() - j.receipt) * 1000 if j.receipt else float('nan'),
                        query_duration_ms=j.query_ms, sensor_timestamp_available=False,
                        command_written=j.written, target_raw=int(j.target) if arm and j.target is not None else 0,
                        target_deg=float(j.target) if not arm and j.target is not None else float('nan'),
                        last_written_deg=j.last_written_deg, measured_angle_valid=False,
                        measured_angle_deg=float('nan'), stop_state=j.stop_state,
                        physical_stop_confirmed=False, error=self.fault or j.error)

    def _latch_fault(self, message):
        if self.fault:
            return
        self.fault = message
        for joint in self.joints.values():
            op = joint.active
            if op:
                self._end(joint, 'io_error', message)
                # Do not block a ROS result on a kernel call that may never return.
                self._finish(joint, op, 'io_error', message)

    def _monitor(self):
        while self.running:
            with self.lock:
                now = time.monotonic()
                if self.inflight and now - self.inflight[0] > self.cfg['io_timeout_s'] + .02:
                    self._latch_fault('I/O deadline exceeded; in-flight outcome unknown; no retry')
                for joint in self.joints.values():
                    op = joint.active
                    if not op or op.ending:
                        continue
                    if now >= op.deadline:
                        self._end(joint, 'timeout', 'Goal deadline exceeded')
                    elif op.written and joint.name != 'gripper' and now - joint.receipt > self.cfg['feedback_max_age_s']:
                        self._end(joint, 'feedback_lost', 'No fresh position feedback')
            time.sleep(.005)

    def _run(self):
        due = {n: 0.0 for n in JOINT_IDS if n != 'gripper'}
        while self.running:
            with self.lock:
                task = None
                if self.stops:
                    task = ('stop', self.stops.popleft())
                while task is None and self.commands:
                    op = self.commands.popleft()
                    j = self.joints[op.joint]
                    if not self.fault and not self.closing and j.active is op and not op.ending and op.generation == j.generation:
                        if time.monotonic() >= op.deadline:
                            self._end(j, 'timeout', 'Goal expired before write')
                            continue
                        if op.joint != 'gripper' and not self._valid(j):
                            self._end(j, 'feedback_lost', 'Feedback expired before write')
                            continue
                        op.attempted = True
                        j.phase = 'executing'
                        task = ('move', op)
                if task is None and not self.closing and not self.fault:
                    name = min(due, key=due.get)
                    if due[name] <= time.monotonic():
                        task = ('read', name)
                        due[name] = time.monotonic() + self.cfg['poll_period_s']
                if task:
                    self.inflight = (time.monotonic(), task)
            if task is None:
                self.wake.wait(.005)
                self.wake.clear()
                continue
            self._perform(*task)

    def _perform(self, kind, item):
        started = time.monotonic()
        error, value = None, None
        try:
            if kind == 'read':
                value = self.backend.read(JOINT_IDS[item], self.cfg['io_timeout_s'])
                number(value, -32768, 32767, 'raw reply', True)
            elif kind == 'stop':
                self.backend.stop(JOINT_IDS[item], self.cfg['io_timeout_s'])
            elif item.joint == 'gripper':
                self.backend.pwm(item.target, self.cfg['io_timeout_s'])
            else:
                self.backend.move(JOINT_IDS[item.joint], item.target, item.duration_ms, self.cfg['io_timeout_s'])
            if time.monotonic() - started > self.cfg['io_timeout_s']:
                raise TimeoutError('I/O returned after deadline; outcome uncertain')
        except Exception as exc:
            error = exc
        with self.lock:
            self.inflight = None
            if kind == 'read':
                joint = self.joints[item]
                joint.query_ms = (time.monotonic() - started) * 1000
                op = joint.active
                if error:
                    joint.valid = False
                    joint.error = str(error)
                    if op:
                        op.settled_since = None
                    if not isinstance(error, TimeoutError):
                        self._latch_fault('Read I/O error: ' + str(error))
                    return
                joint.value, joint.valid = value, True
                joint.receipt, joint.receipt_wall_ns = time.monotonic(), time.time_ns()
                joint.error = ''
                if op and op.written and not op.ending and not op.done.is_set() and joint.receipt > op.sent_at:
                    # A reply can beat the monitor thread after the deadline.
                    # Preserve that real feedback, but never finish a late goal.
                    if joint.receipt >= op.deadline:
                        self._end(joint, 'timeout', 'Goal deadline exceeded before target acceptance')
                        return
                    config = self.cfg['joints'][item]
                    if abs(value - op.target) <= config['tolerance_raw']:
                        if op.settled_since is None:
                            op.settled_since = joint.receipt
                        elif joint.receipt - op.settled_since >= config['settled_ms'] / 1000:
                            self._finish(joint, op, 'target_observed', 'Fresh raw feedback within configured target window')
                    else:
                        op.settled_since = None
            elif kind == 'stop':
                joint = self.joints[item]
                joint.stop_pending = False
                joint.stop_state = 'write_failed' if error else 'stop_written_unconfirmed'
                if error:
                    self._latch_fault('STOP write failed: ' + str(error))
                op = joint.active
                if op and op.ending:
                    self._finish(joint, op, 'io_error' if error else op.ending, str(error) if error else op.message)
            else:
                op = item
                joint = self.joints[op.joint]
                if error:
                    self._latch_fault('Motion write outcome uncertain: ' + str(error))
                    return
                op.written = joint.written = True
                op.sent_at = time.monotonic()
                if op.joint == 'gripper':
                    # Board write_word_data accepts integer degrees, not fractional positioning.
                    joint.last_written_deg = float(round(op.target))
                    if not op.done.is_set():
                        self._finish(joint, op, op.ending or 'command_written', op.message or 'PWM2 target written; actual opening unknown')

    def close(self):
        with self.lock:
            self.closing = True
            for j in self.joints.values():
                if j.active:
                    self._end(j, 'shutdown', 'Session shutting down')
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with self.lock:
                if not self.inflight and not self.stops:
                    break
            time.sleep(.005)
        self.running = False
        self.wake.set()
        for thread in (self.worker, self.monitor):
            if thread:
                thread.join(timeout=.2)
        with self.lock:
            for j in self.joints.values():
                if j.active:
                    self._finish(j, j.active, 'shutdown', 'Session ended; physical state unconfirmed')
        if self.worker and self.worker.is_alive():
            return False  # Do not close/reopen handles underneath an in-flight syscall.
        self.backend.close()
        return True
