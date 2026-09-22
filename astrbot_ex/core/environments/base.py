from __future__ import annotations

from typing import Any, Protocol


class EnvironmentAdapter(Protocol):
    mode: str

    def start(self) -> None:
        ...

    def close(self, reason: str = "environment closed") -> None:
        ...

    def status(self) -> dict[str, Any]:
        ...

    def graph(self) -> dict[str, Any]:
        ...

    def endpoints(self) -> dict[str, Any]:
        ...
