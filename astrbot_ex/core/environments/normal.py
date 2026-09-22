from __future__ import annotations

from typing import Any


class NormalEnvironmentAdapter:
    mode = "normal"

    def start(self) -> None:
        return None

    def close(self, reason: str = "environment closed") -> None:
        del reason

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "available": True,
            "active": True,
            "health": "ok",
            "label": "普通环境",
            "reason": None,
        }

    def graph(self) -> dict[str, Any]:
        return {
            "nodes": [],
            "topics": [],
            "refreshed_at": None,
            "source": "normal",
        }

    def endpoints(self) -> dict[str, Any]:
        return {
            "received": [],
            "sent": [],
            "refreshed_at": None,
        }
