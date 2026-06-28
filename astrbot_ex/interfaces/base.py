from __future__ import annotations

from typing import Protocol


class EXPlugin(Protocol):
    id: str
    name: str

    def on_load(self) -> None: ...

    def on_enable(self) -> None: ...

    def on_disable(self) -> None: ...

    def on_unload(self) -> None: ...
