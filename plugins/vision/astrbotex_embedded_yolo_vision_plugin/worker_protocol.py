from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from protocol import decode_worker_line


@dataclass(slots=True)
class WorkerMessage:
    kind: str
    payload: dict[str, Any]


def parse_line(line: str) -> WorkerMessage:
    kind, payload = decode_worker_line(line)
    return WorkerMessage(kind, payload)


def encode_event(kind: str, payload: dict[str, Any] | None = None, **fields: Any) -> str:
    value: dict[str, Any] = {"kind": kind}
    if payload is not None:
        value["payload"] = payload
    value.update(fields)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
