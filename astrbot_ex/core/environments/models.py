from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ENVIRONMENT_MODES = ("normal", "ros2")
ENVIRONMENT_PHASES = ("idle", "starting", "stopping", "failed")
ENVIRONMENT_HEALTH = ("ok", "degraded", "unavailable", "unknown")


class EnvironmentError(RuntimeError):
    """Base class for environment lifecycle errors."""


class EnvironmentBusyError(EnvironmentError):
    """Raised when another environment operation is already running."""


class EnvironmentRevisionConflict(EnvironmentError):
    """Raised when a client writes against an old environment revision."""


@dataclass(slots=True)
class EnvironmentSnapshot:
    schema_version: int
    session_id: str
    revision: int
    desired_mode: str
    active_mode: str
    phase: str
    health: str
    generation: int
    operation_id: str | None
    last_error: dict[str, Any] | None
    topic_bus_available: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "revision": self.revision,
            "desired_mode": self.desired_mode,
            "active_mode": self.active_mode,
            "phase": self.phase,
            "health": self.health,
            "generation": self.generation,
            "operation_id": self.operation_id,
            "last_error": self.last_error,
            "topic_bus_available": self.topic_bus_available,
        }
