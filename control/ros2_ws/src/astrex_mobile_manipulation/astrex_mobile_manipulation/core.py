"""ROS-free protocol, feedback and execution gate shared with Isaac.

All deadlines and receive ages use this host's monotonic clock. ROS stamps
remain simulation time and are never subtracted from a wall clock.
"""
from __future__ import annotations
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "astrex.mm.v1"
MAX_BYTES = 65536
IDENTITY = ("command_id", "ex_session", "goal_revision", "execution_epoch")

class Rejected(ValueError):
    pass

def finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Rejected(f"INVALID_NUMBER:{label}")
    return float(value)

def base_stop_measurement(wheel_rates: dict, radius: float, linear: list, angular: list) -> dict:
    """Measure a physical stop without signed wheel averaging or commands.

    Root velocities come from the articulation physics API. All wheel surface
    speeds and full 3-D root velocity norms must meet the existing thresholds.
    """
    radius=finite(radius,'wheel_radius')
    if radius<=0 or not isinstance(wheel_rates,dict) or not wheel_rates:
        raise Rejected('BASE_STOP_WHEELS')
    if (not isinstance(linear,(list,tuple)) or len(linear)!=3 or
        not isinstance(angular,(list,tuple)) or len(angular)!=3):
        raise Rejected('BASE_STOP_ROOT_VELOCITY_SHAPE')
    surface={name:abs(finite(rate,'wheel_rate')*radius) for name,rate in wheel_rates.items()}
    linear=[finite(v,'base_linear_velocity') for v in linear]
    angular=[finite(v,'base_angular_velocity') for v in angular]
    max_wheel=finite(max(surface.values()),'wheel_surface_speed')
    linear_norm=finite(math.hypot(*linear),'base_linear_speed')
    angular_norm=finite(math.hypot(*angular),'base_angular_speed')
    return {'stationary':max_wheel<=.02 and linear_norm<=.02 and angular_norm<=.05,
        'wheel_surface_speeds_m_s':surface,'max_wheel_surface_speed_m_s':max_wheel,
        'root_linear_velocity_m_s':linear,'root_angular_velocity_rad_s':angular,
        'root_linear_speed_m_s':linear_norm,'root_angular_speed_rad_s':angular_norm,
        'velocity_source':'physx_articulation_root'}

def execution_timing_status(gate, now: float) -> dict:
    """Expose unset times as null without changing internal guard deadlines."""
    return {'sample_monotonic': finite(now, 'sample_monotonic'),
            'lease_until_monotonic': gate.lease_until if math.isfinite(gate.lease_until) else None,
            'last_target_received_monotonic': gate.last_target_received}

def encode(value: dict) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    if len(text.encode()) > MAX_BYTES:
        raise Rejected("MESSAGE_TOO_LARGE")
    return text

def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

def decode(text: str) -> dict:
    if len(text.encode()) > MAX_BYTES:
        raise Rejected("MESSAGE_TOO_LARGE")
    try:
        value = json.loads(text, parse_constant=lambda v: (_ for _ in ()).throw(Rejected("NONFINITE_JSON")))
    except (ValueError, TypeError) as exc:
        raise Rejected("INVALID_JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise Rejected("INVALID_SCHEMA")
    return value

def identity(value: dict) -> tuple:
    for key in ("command_id", "ex_session"):
        if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 160:
            raise Rejected(f"INVALID_ID:{key}")
    for key in ("goal_revision", "execution_epoch"):
        if type(value.get(key)) is not int or value[key] < 0:
            raise Rejected(f"INVALID_REVISION:{key}")
    return tuple(value[k] for k in IDENTITY)

def envelope(value: dict, **fields: Any) -> dict:
    return {"schema": SCHEMA, **{k: value[k] for k in IDENTITY}, **fields}


def support_contact_updates(specification: dict, profile) -> list[tuple[str, str, bool]]:
    """Fail closed for phase-specific object/support pairs from the profile.

    Return every configured pair, including false defaults. Thus an omitted
    relation cannot retain an earlier lift/place permission in MoveIt's ACM.
    """
    permitted = profile.raw.get("allowed_support_contacts", [])
    if not isinstance(permitted, list):
        raise Rejected("SUPPORT_PROFILE_SHAPE")
    updates = {}
    for pair in permitted:
        if (not isinstance(pair, list) or len(pair) != 2 or
            any(not isinstance(n, str) or not n for n in pair) or pair[0] == pair[1]):
            raise Rejected("SUPPORT_PROFILE_PAIR")
        if tuple(pair) in updates:
            raise Rejected("SUPPORT_PROFILE_DUPLICATE")
        updates[tuple(pair)] = False
    requested = specification.get("support_contacts", [])
    if not isinstance(requested, list) or len(requested) > len(updates):
        raise Rejected("SUPPORT_CONTACT_BUDGET")
    seen = set()
    for row in requested:
        if not isinstance(row, dict):
            raise Rejected("SUPPORT_CONTACT_SHAPE")
        pair = (row.get("object_id"), row.get("support_id"))
        if any(not isinstance(n, str) for n in pair) or pair not in updates:
            raise Rejected("UNAUTHORIZED_SUPPORT_CONTACT")
        if pair in seen:
            raise Rejected("SUPPORT_CONTACT_DUPLICATE")
        if type(row.get("allowed")) is not bool:
            raise Rejected("SUPPORT_ALLOWED_TYPE")
        seen.add(pair)
        updates[pair] = row["allowed"]
    return [(a, b, allowed) for (a, b), allowed in updates.items()]

@dataclass(frozen=True)
class JointLimit:
    lower: float
    upper: float
    velocity: float
    acceleration: float
    effort: float

@dataclass
class RobotProfile:
    arm_joint_names: list[str]
    gripper_joint_names: list[str]
    joint_limits: dict[str, JointLimit]
    group_name: str = "xarm6"
    base_frame: str = "link_base"
    tcp_frame: str = "link_tcp"
    arm_action: str = "/astrex/mm/arm_controller/follow_joint_trajectory"
    gripper_action: str = "/astrex/mm/gripper_controller/gripper_cmd"
    moveit_namespace: str = "/astrex/mm"
    velocity_scale: float = .15
    acceleration_scale: float = .15
    state_age: float = .2
    lease_seconds: float = .5
    stop_window: float = .5
    stop_velocity: float = .02
    start_tolerance: float = .02
    planner_budget: float = 2.0
    profile_hash: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "RobotProfile":
        arm = list(data["arm_joint_names"])
        gripper = list(data["gripper_joint_names"])
        if not arm or not gripper or len(set(arm + gripper)) != len(arm + gripper):
            raise Rejected("INVALID_JOINT_NAMES")
        limits = {}
        for name in arm + gripper:
            row = data["joint_limits"][name]
            lim = JointLimit(*(finite(row[key], f"{name}.{key}") for key in ("lower", "upper", "velocity", "acceleration", "effort")))
            if lim.lower >= lim.upper or min(lim.velocity, lim.acceleration, lim.effort) <= 0:
                raise Rejected("INVALID_LIMITS:" + name)
            limits[name] = lim
        options = {key: data[key] for key in cls.__dataclass_fields__ if key in data and key not in ("arm_joint_names", "gripper_joint_names", "joint_limits", "profile_hash", "raw")}
        profile = cls(arm, gripper, limits, **options)
        for key in ("state_age", "lease_seconds", "stop_window", "stop_velocity", "start_tolerance", "planner_budget"):
            if finite(getattr(profile, key), key) <= 0:
                raise Rejected("INVALID_PROFILE:" + key)
        for key in ("velocity_scale", "acceleration_scale"):
            if not 0 < finite(getattr(profile, key), key) <= 1:
                raise Rejected("INVALID_PROFILE:" + key)
        if "gripper_velocity" in data:
            velocity = finite(data["gripper_velocity"], "gripper_velocity")
            if not 0 < velocity <= min(limits[name].velocity for name in gripper):
                raise Rejected("GRIPPER_VELOCITY_LIMIT")
        profile.profile_hash = digest(data)
        profile.raw = data
        return profile

    @classmethod
    def load(cls, path: str) -> "RobotProfile":
        return cls.from_dict(json.loads(Path(path).read_text()))

    @property
    def names(self) -> list[str]:
        return self.arm_joint_names + self.gripper_joint_names

class StateCache:
    def __init__(self, profile: RobotProfile):
        self.profile = profile
        self.positions: dict[str, float] = {}
        self.velocities: dict[str, float] = {}
        self.received = -math.inf
        self.stamp = -math.inf
        self.sequence = 0
        self.reset_detected = False
        self.still_since: float | None = None

    def update(self, names, positions, velocities, stamp: float, now: float) -> bool:
        if len(names) != len(positions) or len(names) != len(velocities) or len(set(names)) != len(names):
            raise Rejected("INVALID_RAW_STATE_SHAPE")
        values = dict(zip(names, positions)); rates = dict(zip(names, velocities))
        if any(name not in values for name in self.profile.names):
            raise Rejected("MISSING_RAW_JOINT")
        q = {name: finite(values[name], name) for name in self.profile.names}
        dq = {name: finite(rates[name], name) for name in self.profile.names}
        finite(stamp, "source_stamp")
        if stamp < self.stamp:
            self.reset_detected = True
            self.received = -math.inf
            self.still_since = None
            raise Rejected("SIM_TIME_REVERSED")
        if stamp == self.stamp:
            return False  # Cached timestamps must not refresh validity.
        gap = now - self.received
        self.positions, self.velocities = q, dq
        self.received, self.stamp = now, stamp
        self.sequence += 1
        stationary = all(abs(dq[n]) <= self.profile.stop_velocity for n in self.profile.names)
        if not stationary:
            self.still_since = None
        elif self.still_since is None or gap > self.profile.state_age:
            self.still_since = now
        return True

    def require_fresh(self, now: float):
        if self.reset_detected:
            raise Rejected("SIM_RESET_REQUIRES_NEW_GATEWAY")
        if now - self.received > self.profile.state_age:
            raise Rejected("RAW_STATE_STALE")
        return self.positions

    def stopped(self, now: float) -> bool:
        return not self.reset_detected and now - self.received <= self.profile.state_age and self.still_since is not None and now - self.still_since >= self.profile.stop_window

    def check_position(self, name: str, value: float):
        value = finite(value, name)
        lim = self.profile.joint_limits[name]
        if not lim.lower - 1e-6 <= value <= lim.upper + 1e-6:
            raise Rejected("JOINT_LIMIT:" + name)

    def validate_trajectory(self, trajectory: dict, now: float):
        q = self.require_fresh(now)
        names = trajectory.get("joint_names", [])
        if len(names) != len(set(names)) or set(names) != set(self.profile.arm_joint_names):
            raise Rejected("TRAJECTORY_JOINTS")
        points = trajectory.get("points", [])
        if not 2 <= len(points) <= 10000:
            raise Rejected("TRAJECTORY_POINTS")
        previous = -1.0
        for i, point in enumerate(points):
            t = finite(point["time_from_start"], "time_from_start")
            if t < 0 or t <= previous:
                raise Rejected("TRAJECTORY_TIME")
            previous = t
            for field_name in ("positions", "velocities", "accelerations"):
                vals = point.get(field_name, [])
                if len(vals) != len(names):
                    raise Rejected("TRAJECTORY_DIMENSION:" + field_name)
                for name, value in zip(names, vals):
                    value = finite(value, name)
                    lim = self.profile.joint_limits[name]
                    if field_name == "positions":
                        self.check_position(name, value)
                        if i == 0 and abs(value - q[name]) > self.profile.start_tolerance:
                            raise Rejected("TRAJECTORY_START:" + name)
                    elif abs(value) > getattr(lim, "velocity" if field_name == "velocities" else "acceleration") * 1.001:
                        raise Rejected("TRAJECTORY_" + field_name.upper() + ":" + name)
        return digest(trajectory)

class CommandLedger:
    """Bounded session ledger; a full ledger refuses rather than evicts IDs."""
    def __init__(self, capacity=4096):
        self.records: dict[tuple, dict] = {}
        self.active: tuple | None = None
        self.epochs: dict[str, int] = {}
        self.capacity = capacity

    def admit(self, command: dict, now: float) -> tuple[dict, bool]:
        key = identity(command)
        hashed = digest(command)
        if key in self.records:
            record = self.records[key]
            if hashed != record["hash"]:
                raise Rejected("COMMAND_ID_CONFLICT")
            return record, False
        if len(self.records) >= self.capacity:
            raise Rejected("LEDGER_FULL")
        if self.active is not None:
            raise Rejected("BUSY")
        deadline = finite(command.get("deadline_monotonic"), "deadline_monotonic")
        if deadline <= now:
            raise Rejected("COMMAND_EXPIRED")
        session, epoch = command["ex_session"], command["execution_epoch"]
        if epoch <= self.epochs.get(session, -1):
            raise Rejected("OLD_EPOCH")
        self.epochs[session] = epoch
        record = {"hash": hashed, "command": command, "ack": None, "result": None, "lease_seq": -1, "lease_until": now + .5}
        self.records[key] = record
        self.active = key
        return record, True

    def renew(self, message: dict, now: float, duration: float):
        key = identity(message)
        if key != self.active:
            raise Rejected("LEASE_IDENTITY")
        seq = message.get("seq")
        if type(seq) is not int or seq <= self.records[key]["lease_seq"]:
            raise Rejected("LEASE_SEQUENCE")
        self.records[key]["lease_seq"] = seq
        self.records[key]["lease_until"] = min(now + duration, self.records[key]["command"]["deadline_monotonic"])

    def current(self):
        return self.records.get(self.active)

    def finish(self, result: dict):
        if self.active is not None:
            self.records[self.active]["result"] = result
        self.active = None

class FinalGateCore:
    """Final in-simulator lease gate; stopping never calls a model or planner."""
    def __init__(self, profile: RobotProfile):
        self.profile = profile
        self.key: tuple | None = None
        self.closed_keys: set[tuple] = set()
        self.gateway_session: str | None = None
        self.lease_seq = -1
        self.command_seq = -1
        self.last_target_received = None
        self.lease_until = -math.inf
        self.targets: dict[str, float] = {}
        self.gripper_hold: dict[str, float] = {}
        self.local_velocities: dict[str, float] = {}
        self.hold: dict[str, float] = {}
        self.active = False
        self.reason = "INITIAL_HOLD"
        self.tracking_error_since = None
        self.last_sim_time = -math.inf
        self.last_source_stamp = -math.inf
        self.reset_latched = False
        self.motion_kind = None
        self.base_twist = (0., 0.)
        self.stop_count = 0
        self.effort_limits = {n: profile.joint_limits[n].effort for n in profile.names}

    def grant(self, message: dict, now: float):
        key = identity(message)
        session = message.get("execution_session")
        seq = message.get("seq")
        if not isinstance(session, str) or not session or type(seq) is not int or seq < 0:
            raise Rejected("FINAL_LEASE_INVALID")
        if message.get("robot_config_hash") != self.profile.profile_hash:
            raise Rejected("FINAL_PROFILE_MISMATCH")
        if self.reset_latched:
            raise Rejected("FINAL_SIM_RESET")
        if key in self.closed_keys:
            raise Rejected("FINAL_CLOSED_COMMAND")
        if key != self.key or session != self.gateway_session:
            if self.active:
                raise Rejected("FINAL_BUSY")
            self.key, self.gateway_session = key, session
            self.lease_seq = -1
            self.command_seq = -1
            self.last_target_received = None
            self.last_source_stamp = -math.inf
            self.targets = {}
            self.base_twist = (0., 0.)
            self.motion_kind = message.get("motion_kind", "arm")
        if seq <= self.lease_seq:
            raise Rejected("FINAL_LEASE_REPLAY")
        for name, value in message.get("effort_limits", {}).items():
            if name not in self.profile.joint_limits:
                raise Rejected("FINAL_EFFORT_JOINT")
            value = finite(value, "effort_limit")
            if not 0 < value <= self.profile.joint_limits[name].effort:
                raise Rejected("FINAL_EFFORT_LIMIT")
            self.effort_limits[name] = value
        self.lease_seq = seq
        self.lease_until = min(now + self.profile.lease_seconds, finite(message.get("deadline_monotonic"), "deadline"))
        if self.lease_until <= now:
            raise Rejected("FINAL_EXPIRED")
        self.active, self.reason = True, "AUTHORIZED"

    def receive(self, message: dict, now: float):
        if not self.active or now >= self.lease_until:
            raise Rejected("FINAL_NO_LEASE")
        if identity(message) != self.key or message.get("execution_session") != self.gateway_session:
            raise Rejected("FINAL_IDENTITY")
        seq = message.get("seq")
        if type(seq) is not int or seq <= self.command_seq:
            raise Rejected("FINAL_COMMAND_REPLAY")
        age = now - finite(message.get("source_monotonic"), "source_monotonic")
        if age < 0 or age > self.profile.state_age:
            raise Rejected("FINAL_COMMAND_STALE")
        names = message.get("names", []); values = message.get("positions", [])
        if not names or len(names) != len(values) or len(set(names)) != len(names):
            raise Rejected("FINAL_COMMAND_SHAPE")
        allowed = self.profile.arm_joint_names if self.motion_kind == "arm" else (self.profile.gripper_joint_names if self.motion_kind == "gripper" else [])
        if not set(names).issubset(set(allowed)):
            raise Rejected("FINAL_WRONG_MOTION_GROUP")
        targets = {}
        for name, value in zip(names, values):
            if name not in self.profile.joint_limits:
                raise Rejected("FINAL_UNKNOWN_JOINT")
            value = finite(value, name)
            lim = self.profile.joint_limits[name]
            if not lim.lower <= value <= lim.upper:
                raise Rejected("FINAL_JOINT_LIMIT:" + name)
            targets[name] = value
        self.command_seq = seq
        self.last_target_received = now
        for name, value in targets.items():
            if name in self.profile.gripper_joint_names and value < self.gripper_hold.get(name, -math.inf):
                self.gripper_hold.pop(name, None)
        self.targets.update(targets)

    def receive_base(self, message: dict, now: float):
        if self.motion_kind != "move" or not self.active or now >= self.lease_until:
            raise Rejected("BASE_NOT_AUTHORIZED")
        if identity(message) != self.key or message.get("execution_session") != self.gateway_session:
            raise Rejected("BASE_IDENTITY")
        seq = message.get("seq")
        if type(seq) is not int or seq <= self.command_seq:
            raise Rejected("BASE_REPLAY")
        age = now - finite(message.get("source_monotonic"), "source_monotonic")
        if age < 0 or age > self.profile.state_age:
            raise Rejected("BASE_STALE")
        base = self.profile.raw.get("base")
        if not base: raise Rejected("BASE_NOT_CONFIGURED")
        linear = finite(message.get("linear"), "linear")
        angular = finite(message.get("angular"), "angular")
        if abs(linear) > min(.15, base.get("max_linear_velocity", .15)) or abs(angular) > base.get("max_angular_velocity", .5):
            raise Rejected("BASE_VELOCITY_LIMIT")
        self.command_seq = seq
        self.last_target_received = now
        self.base_twist = (linear, angular)

    def stop(self, reason: str, positions: dict[str, float]):
        if self.key is not None:
            self.closed_keys.add(self.key)
        if self.motion_kind == "gripper":
            for name in self.profile.gripper_joint_names:
                target = self.targets.get(name)
                if (target is not None and name in positions and target > positions[name] and
                    abs(self.local_velocities.get(name, math.inf)) <= self.profile.stop_velocity):
                    self.gripper_hold[name] = target
        if self.active or not self.hold:
            self.hold = {n: float(positions[n]) if n in positions and math.isfinite(positions[n]) else self.hold.get(n, 0.) for n in self.profile.names}
            self.hold.update(self.gripper_hold)
            self.stop_count += 1
        self.active = False
        self.targets = {}
        self.base_twist = (0., 0.)
        self.reason = reason
        self.tracking_error_since = None

    def tick(self, now: float, sim_time: float, positions: dict[str, float], velocities: dict[str, float]):
        self.local_velocities = dict(velocities)
        if sim_time < self.last_sim_time:
            self.reset_latched = True
            self.stop("SIM_TIME_REVERSED", positions)
        self.last_sim_time = sim_time
        if any(n not in positions or not math.isfinite(positions[n]) or n not in velocities or not math.isfinite(velocities[n]) for n in self.profile.names):
            self.stop("INVALID_LOCAL_STATE", positions)
        elif self.active and now >= self.lease_until:
            self.stop("LEASE_EXPIRED", positions)
        elif self.active and self.last_target_received is not None and now - self.last_target_received > self.profile.state_age:
            self.stop("COMMAND_STREAM_STALE", positions)
        elif self.active and any(not self.profile.joint_limits[n].lower - .001 <= positions[n] <= self.profile.joint_limits[n].upper + .001 for n in self.profile.names):
            self.stop("LOCAL_JOINT_LIMIT", positions)
        elif self.active and any(abs(velocities[n]) > self.profile.joint_limits[n].velocity * 1.1 for n in self.profile.names):
            self.stop("LOCAL_VELOCITY_LIMIT", positions)
        if self.active:
            tracking_limit = self.profile.raw.get("tracking_error_limit", .35)
            excess = any(name in self.targets and abs(self.targets[name] - positions[name]) > tracking_limit for name in self.profile.arm_joint_names)
            if excess:
                if self.tracking_error_since is None: self.tracking_error_since = now
                elif now - self.tracking_error_since >= self.profile.raw.get("tracking_error_timeout", .3):
                    self.stop("ARM_TRACKING_ERROR", positions)
            else: self.tracking_error_since = None
        if not self.hold:
            self.hold = {n: positions[n] for n in self.profile.names if n in positions}
        return {**self.hold, **self.targets} if self.active else dict(self.hold)
