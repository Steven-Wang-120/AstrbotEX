from __future__ import annotations

from typing import Protocol

from astrbot_ex.core.models import ScanResult
from astrbot_ex.interfaces.base import EXPlugin


class ScanProvider(EXPlugin, Protocol):
    def get_scan(self) -> ScanResult: ...
