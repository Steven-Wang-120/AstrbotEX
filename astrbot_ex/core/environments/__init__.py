"""External communication environment adapters for AstrBotEX."""

from astrbot_ex.core.environments.manager import EnvironmentManager
from astrbot_ex.core.environments.models import (
    EnvironmentBusyError,
    EnvironmentRevisionConflict,
    EnvironmentSnapshot,
)

__all__ = [
    "EnvironmentBusyError",
    "EnvironmentManager",
    "EnvironmentRevisionConflict",
    "EnvironmentSnapshot",
]
