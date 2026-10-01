"""Pure B02 capability snapshots; lifecycle wiring belongs to composition."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from threading import RLock
from typing import Any, Iterable

from astrbot_ex.core.actions.models import (
    ActionManifestV2, MAX_ID_LEN, MAX_SEQUENCE, measure_json_budget,
    parse_action_manifest,
)


@dataclass(frozen=True, slots=True)
class CapabilityInput:
    """One owner captured at a single input revision by the integration layer."""

    owner: str
    generation: int
    manifest: ActionManifestV2
    config: dict[str, Any]
    guide: dict[str, str]
    enabled: bool
    version: str
    state: str = "ready"
    directory_version: str = ""
    directory_status: str = ""


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    revision: int
    entries: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"revision": self.revision, "entries": copy.deepcopy(self.entries)}

    def executable(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(entry) for entry in self.entries if entry["available"])


class CapabilityCatalog:
    """Caller captures input records together; refresh is atomic and semantic.

    The catalog does no registry reads or file I/O. A missing/unavailable guide is
    visible but excludes its owner from executable(), as does a disabled owner.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._revision = 0
        self._entries: tuple[dict[str, Any], ...] = ()
        self._canonical = "[]"

    def snapshot(self) -> CapabilitySnapshot:
        with self._lock:
            return CapabilitySnapshot(self._revision, copy.deepcopy(self._entries))

    def refresh(self, records: Iterable[CapabilityInput]) -> CapabilitySnapshot:
        items = list(records)
        if len(items) > 256:
            raise ValueError("too many catalog owners")
        seen_owners: set[str] = set()
        seen_actions: set[str] = set()
        entries: list[dict[str, Any]] = []
        for record in items:
            if not isinstance(record, CapabilityInput):
                raise ValueError("catalog requires CapabilityInput records")
            if (type(record.owner) is not str or not record.owner or len(record.owner) > MAX_ID_LEN
                    or record.owner in seen_owners or type(record.generation) is not int
                    or not 0 <= record.generation <= MAX_SEQUENCE
                    or type(record.enabled) is not bool or type(record.version) is not str
                    or not record.version.strip() or len(record.version) > MAX_ID_LEN
                    or type(record.state) is not str
                    or record.state not in {"ready", "loading", "starting", "stopping", "blocked", "unloaded"}
                    or type(record.directory_version) is not str
                    or len(record.directory_version) > MAX_ID_LEN
                    or type(record.directory_status) is not str
                    or record.directory_status not in {"", "version_changed", "manifest_changed", "config_changed", "directory_unavailable"}):
                raise ValueError("invalid or duplicate owner/generation/enabled/version")
            seen_owners.add(record.owner)
            if not isinstance(record.manifest, ActionManifestV2):
                raise ValueError("catalog requires v2 action manifest")
            manifest = parse_action_manifest(record.manifest.to_dict(), owner=record.owner)
            if manifest.id and manifest.id != record.owner:
                raise ValueError("manifest owner mismatch")
            if not isinstance(record.config, dict) or measure_json_budget(record.config) is not None:
                raise ValueError("invalid or excessive config")
            guide = record.guide
            if (not isinstance(guide, dict) or set(guide) != {"status", "reason", "text", "content_hash"}
                    or any(type(value) is not str for value in guide.values())
                    or guide["status"] not in {"available", "unavailable", "rejected"}
                    or len(guide["text"].encode("utf-8")) > 8192
                    or measure_json_budget(guide) is not None):
                raise ValueError("invalid guide")
            if guide["status"] == "available":
                if (not guide["content_hash"] or
                        guide["content_hash"] != hashlib.sha256(guide["text"].encode("utf-8")).hexdigest()):
                    raise ValueError("guide hash does not match content")
            elif guide["text"] or guide["content_hash"]:
                raise ValueError("unavailable guide must not contain content")
            for action in manifest.actions:
                if action.action_id in seen_actions:
                    raise ValueError("duplicate catalog action_id")
                seen_actions.add(action.action_id)
            directory_status = (record.directory_status or
                                ("version_changed" if record.directory_version and
                                 record.directory_version != record.version else ""))
            available = (record.enabled and record.state == "ready" and
                         not directory_status and guide["status"] == "available")
            entries.append({"owner": record.owner, "generation": record.generation,
                            "version": record.version, "directory_version": record.directory_version,
                            "directory_status": directory_status, "state": record.state,
                            "manifest": manifest.to_dict(), "config": copy.deepcopy(record.config),
                            "guide": copy.deepcopy(guide), "enabled": record.enabled,
                            "available": available,
                            "unavailable_reason": ("disabled" if not record.enabled else
                                                   record.state if record.state != "ready" else
                                                   directory_status or (guide["status"] if not available else ""))})
        entries.sort(key=lambda entry: entry["owner"])
        canonical = json.dumps(entries, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self._lock:
            if canonical != self._canonical:
                if self._revision == MAX_SEQUENCE:
                    raise OverflowError("catalog revision exhausted")
                self._revision += 1
                self._canonical = canonical
                self._entries = tuple(entries)
            return CapabilitySnapshot(self._revision, copy.deepcopy(self._entries))
