from __future__ import annotations

from typing import Protocol

from astrbot_ex.core.models import FusedScene, ScanResult, VisionResult


class FusionProvider(Protocol):
    def fuse(self, vision: VisionResult, scan: ScanResult | None) -> FusedScene: ...
