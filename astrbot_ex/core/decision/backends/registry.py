"""Static trusted backend constructors; no plugin paths or dynamic imports."""
from __future__ import annotations

from types import MappingProxyType

from .jev import JevBackend
from .mock import MockBackend


BACKEND_FACTORIES = MappingProxyType({"mock": MockBackend, "jev": JevBackend})


def backend_names() -> tuple[str, ...]:
    return tuple(BACKEND_FACTORIES)


def create_backend(name: str, **kwargs):
    if not isinstance(name, str) or name not in BACKEND_FACTORIES:
        raise ValueError("unknown trusted decision backend")
    return BACKEND_FACTORIES[name](**kwargs)
