from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import time
from typing import Any, Literal


class RuntimeState(str, Enum):
    IDLE = "idle"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    FAULT = "fault"
    FINISHED = "finished"


@dataclass(slots=True)
class Entity:
    id: str
    type: str
    confidence: float = 1.0
    semantic: str | None = None
    position: tuple[float, float] | None = None
    bbox_px: tuple[int, int, int, int] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Zone:
    id: str
    role: str
    polygon: list[tuple[float, float]]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RobotState:
    pose: tuple[float, float, float] | None = None
    battery_voltage: float | None = None
    link_ok: bool = False
    estop: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VisionResult:
    frame_id: int
    timestamp: float
    entities: list[Entity] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorldState:
    timestamp: float = field(default_factory=time)
    entities: list[Entity] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    robot: RobotState = field(default_factory=RobotState)
    task_state: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Goal:
    type: str
    target_entity_id: str | None = None
    target_zone_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MotionIntent:
    vx: float = 0.0
    vy: float = 0.0
    wz: float = 0.0
    duration_ms: int = 100
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActuatorIntent:
    name: str
    value: Any
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Intent:
    motion: MotionIntent | None = None
    actuators: list[ActuatorIntent] = field(default_factory=list)
    note: str = ""


@dataclass(slots=True)
class RuleDecision:
    allowed: bool
    reason: str = ""
    severity: Literal["info", "warning", "error"] = "info"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SkillResult:
    status: Literal["running", "done", "failed"]
    intent: Intent = field(default_factory=Intent)
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RuntimeEvent:
    type: str
    message: str
    timestamp: float = field(default_factory=time)
    data: dict[str, Any] = field(default_factory=dict)
