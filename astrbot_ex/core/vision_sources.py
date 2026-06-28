from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class VisionSource:
    id: str
    type: str = "local_api"
    enabled: bool = True
    result_endpoint: str = ""
    preview_url: str = ""
    snapshot_url: str = ""
    timeout_ms: int = 80
    stale_after_ms: int = 300
    min_confidence: float = 0.4
    metadata: dict[str, Any] = field(default_factory=dict)


class VisionSourceManager:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.sources: dict[str, VisionSource] = {}
        self.active_source_id: str | None = None
        self.load()

    def load(self) -> None:
        if not self.config_path.exists():
            self._install_default()
            self.save()
            return

        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.active_source_id = raw.get("active_source")
        self.sources = {}
        for item in raw.get("sources", []):
            source = VisionSource(**item)
            self.sources[source.id] = source
        if self.active_source_id not in self.sources:
            self.active_source_id = next(iter(self.sources), None)

    def save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_source": self.active_source_id,
            "sources": [asdict(source) for source in self.sources.values()],
        }
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_sources(self) -> list[VisionSource]:
        return list(self.sources.values())

    def upsert(self, payload: dict[str, Any]) -> VisionSource:
        source = VisionSource(**payload)
        self.sources[source.id] = source
        if self.active_source_id is None:
            self.active_source_id = source.id
        self.save()
        return source

    def delete(self, source_id: str) -> None:
        if source_id not in self.sources:
            raise KeyError(source_id)
        del self.sources[source_id]
        if self.active_source_id == source_id:
            self.active_source_id = next(iter(self.sources), None)
        self.save()

    def set_active(self, source_id: str) -> VisionSource:
        source = self.get(source_id)
        self.active_source_id = source.id
        self.save()
        return source

    def get(self, source_id: str) -> VisionSource:
        if source_id not in self.sources:
            raise KeyError(source_id)
        return self.sources[source_id]

    def active(self) -> VisionSource | None:
        if self.active_source_id is None:
            return None
        return self.sources.get(self.active_source_id)

    def latest(self, source_id: str | None = None) -> dict[str, Any]:
        source = self.get(source_id) if source_id else self.active()
        if source is None:
            return {"ok": False, "error": "no active vision source"}
        if source.type == "mock":
            return self._mock_latest(source)
        if source.type == "local_api":
            if not source.result_endpoint:
                return {"ok": False, "error": "result_endpoint is required"}
            return self._fetch_json(source.result_endpoint, source.timeout_ms)
        return {"ok": False, "error": f"unsupported vision source type: {source.type}"}

    def test(self, source_id: str) -> dict[str, Any]:
        source = self.get(source_id)
        started = time.perf_counter()
        result = self.latest(source.id)
        latency_ms = round((time.perf_counter() - started) * 1000)
        ok = bool(result.get("ok", True)) and "error" not in result
        return {
            "ok": ok,
            "source_id": source.id,
            "type": source.type,
            "latency_ms": latency_ms,
            "preview_url": source.preview_url,
            "snapshot_url": source.snapshot_url,
            "latest": result,
        }

    def _install_default(self) -> None:
        source = VisionSource(id="mock_vision_source", type="mock", metadata={"note": "built-in development source"})
        self.sources = {source.id: source}
        self.active_source_id = source.id

    @staticmethod
    def _mock_latest(source: VisionSource) -> dict[str, Any]:
        return {
            "ok": True,
            "frame_id": 0,
            "timestamp": time.time(),
            "source": source.id,
            "latency_ms": 0,
            "objects": [],
        }

    @staticmethod
    def _fetch_json(url: str, timeout_ms: int) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=max(timeout_ms / 1000, 0.001)) as response:
                data = json.loads(response.read().decode("utf-8"))
                return data if isinstance(data, dict) else {"ok": False, "error": "response is not an object"}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc)}
