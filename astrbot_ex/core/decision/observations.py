"""Bounded, receive-stamped original TopicBus JSON. No business normalization."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from ..actions.models import MAX_SEQUENCE, measure_json_budget
from .models import ObservationEnvelope


@dataclass(frozen=True)
class _Frame:
    envelope_json: str
    initial_age_ms: float


@dataclass(frozen=True)
class _Epoch:
    current: str
    last_seq: int
    retired: frozenset[str]


class ObservationStore:
    def __init__(self, topic_bus=None, *, clock_ns: Callable[[], int] = time.monotonic_ns,
                 wall_clock: Callable[[], float] = time.time, max_bytes: int = 65536,
                 max_sources: int = 128, max_epochs: int = 64,
                 on_update: Callable[[], None] = lambda: None) -> None:
        self.bus = topic_bus
        self._clock, self._wall = clock_ns, wall_clock
        self.max_bytes, self.max_sources, self.max_epochs = max_bytes, max_sources, max_epochs
        self._on_update = on_update
        self._lock = threading.RLock()
        self._specs: dict[str, dict] = {}
        self._frames: dict[str, _Frame] = {}
        self._epochs: dict[str, _Epoch] = {}
        self._diagnostics: dict[str, str] = {}
        self._subscriptions: dict[str, Callable] = {}
        self._closed = False

    def configure(self, entries) -> None:
        specs = {}
        for entry in entries:
            for source, spec in entry["manifest"].get("observation_sources", {}).items():
                value = {**copy.deepcopy(spec), "description_hash": hashlib.sha256(json.dumps(
                    [spec, entry["guide"]["content_hash"], entry["generation"]], sort_keys=True).encode()).hexdigest(),
                         "generation": entry["generation"], "owner": entry["owner"],
                         "enabled": entry["enabled"] and entry["available"]}
                if source in specs and specs[source] != value:
                    raise ValueError(f"ambiguous observation source ID: {source}")
                specs[source] = value
        with self._lock:
            if len(set(specs) | set(self._specs) | set(self._epochs)) > self.max_sources:
                raise ValueError("observation source capacity exceeded")
            if self._closed:
                return
            for source in set(self._specs) | set(specs):
                if self._specs.get(source) == specs.get(source):
                    # Preserve subscription identity across semantic no-op refreshes.
                    specs[source] = self._specs[source]
                    continue
                unsubscribe = self._subscriptions.pop(source, None)
                if unsubscribe:
                    unsubscribe()
                self._frames.pop(source, None)
                # Keep retired epochs for a source through reconfiguration. A
                # generation change gives the TopicBus fallback a distinct epoch.
                self._diagnostics.pop(source, None)
                if source in specs and specs[source]["enabled"] and self.bus is not None:
                    self._subscriptions[source] = self.bus.subscribe(
                        specs[source]["topic"], lambda msg, sid=source, bound=specs[source]:
                        self._receive(sid, msg, bound))
            self._specs = specs

    def _receive(self, source: str, message, bound_spec: dict) -> None:
        with self._lock:
            spec = self._specs.get(source)
            if self._closed or spec is not bound_spec or not spec["enabled"]:
                return
            if message.source != spec["owner"] and message.source != spec["topic"].split(".", 1)[0]:
                self._diagnostics[source] = "source_mismatch"
                return
            epoch = (message.payload.get("source_epoch") if isinstance(message.payload, dict) else None)
            # No invented business wrapper. Plugins may carry an explicit boot
            # epoch; otherwise only a trusted plugin-generation change resets seq.
            epoch = epoch if epoch is not None else f'{spec["owner"]}:{spec["generation"]}'
        try:
            self.ingest(source, message.payload, source_epoch=epoch, seq=message.seq,
                        source_timestamp=message.timestamp, _bound_spec=bound_spec)
        except (ValueError, TypeError):
            with self._lock:
                if not self._closed and self._specs.get(source) is bound_spec:
                    self._diagnostics[source] = "invalid_payload"

    def ingest(self, source: str, data: dict, *, source_epoch: str, seq: int,
               source_timestamp: float | None, description_hash: str | None = None,
               _bound_spec: dict | None = None) -> bool:
        budget = measure_json_budget(data)
        if not isinstance(data, dict) or budget is not None:
            raise ValueError(f"invalid JSON: {budget}")
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self.max_bytes:
            raise ValueError("observation byte budget exceeded")
        if type(source_epoch) is not str or not source_epoch.strip() or len(source_epoch) > 256:
            raise ValueError("invalid source epoch")
        if type(seq) is not int or not 0 <= seq <= MAX_SEQUENCE:
            raise ValueError("missing/invalid source sequence")
        now = self._clock()
        reason = ""
        age = 0.0
        if source_timestamp is None:
            reason = "missing_source_time"
        elif type(source_timestamp) not in (int, float) or not math.isfinite(source_timestamp):
            reason = "invalid_source_time"
        else:
            age = (self._wall() - source_timestamp) * 1000
            if age < 0:
                reason, age = "future_source_time", 0.0
        with self._lock:
            # TopicBus captures callbacks before invoking them outside its lock.
            # Recheck after JSON validation so retired subscriptions cannot commit.
            if _bound_spec is not None and (self._closed or self._specs.get(source) is not _bound_spec):
                return False
            if self._closed or source not in self._specs:
                raise ValueError("source is not declared")
            spec = self._specs[source]
            if not spec["enabled"]:
                self._diagnostics[source] = "source_disabled"
                return False
            epoch = self._epochs.get(source, _Epoch(source_epoch, -1, frozenset()))
            current, retired = epoch.current, epoch.retired
            if source_epoch in retired:
                self._diagnostics[source] = "old_source_epoch"
                return False
            if source_epoch == current and seq <= epoch.last_seq:
                self._diagnostics[source] = "old_frame"
                return False
            restarted = source_epoch != current
            if restarted:
                if len(retired) >= self.max_epochs:
                    self._diagnostics[source] = "source_epoch_capacity"
                    self._frames.pop(source, None)
                    return False
                retired = retired | {current}
            if description_hash is not None and description_hash != spec["description_hash"]:
                reason = "description_hash_changed"
            if any(field not in data for field in spec.get("required_fields", [])):
                reason = "missing_required_fields"
            if age > spec["max_age_ms"] and not reason:
                reason = "source_time_expired"
            envelope = ObservationEnvelope.parse({
                "schema_version": 1, "observation_id": uuid.uuid4().hex, "source_id": source,
                "source_epoch": source_epoch, "seq": seq, "received_monotonic_ns": now,
                "age_ms": max(0, age), "description_hash": spec["description_hash"],
                "health": {"status": "error" if reason else "ok", "reason_code": reason},
                "data": json.loads(encoded),
            })
            self._frames[source] = _Frame(json.dumps(envelope.to_dict(), ensure_ascii=False), max(0, age))
            self._epochs[source] = _Epoch(source_epoch, seq, frozenset(retired))
            self._diagnostics[source] = reason or ("source_restarted" if restarted else "")
        self._on_update()
        return True

    def snapshot(self, sources) -> list[dict]:
        now = self._clock()
        result = []
        with self._lock:
            for source in sorted(set(sources)):
                frame = self._frames.get(source)
                spec = self._specs.get(source)
                if frame is None or spec is None:
                    continue
                item = json.loads(frame.envelope_json)
                elapsed = now - item["received_monotonic_ns"]
                item["age_ms"] = frame.initial_age_ms + max(0, elapsed) / 1_000_000
                if elapsed < 0:
                    item["health"] = {"status": "error", "reason_code": "future_receive_time"}
                elif item["health"]["status"] == "ok" and item["age_ms"] > spec["max_age_ms"]:
                    item["health"] = {"status": "stale", "reason_code": "observation_expired"}
                result.append(item)
        return result

    def request_observations(self, observations: list[dict]) -> list[dict]:
        """Age this request's data; never substitute a newer frame's payload/TTL."""
        now = self._clock()
        result = []
        with self._lock:
            for old in observations:
                spec = self._specs.get(old["source_id"])
                if self._closed or spec is None or not spec["enabled"]:
                    raise RuntimeError("request_observation_source_missing")
                if spec["description_hash"] != old["description_hash"]:
                    raise RuntimeError("request_observation_description_changed")
                if old["health"]["status"] != "ok":
                    raise RuntimeError(old["health"]["reason_code"])
                elapsed = now - old["received_monotonic_ns"]
                if elapsed < 0:
                    raise RuntimeError("request_observation_future_receive_time")
                # Envelope age may already include time before snapshot assembly;
                # retaining it plus receive elapsed is intentionally conservative.
                age = old["age_ms"] + elapsed / 1_000_000
                if age > spec["max_age_ms"]:
                    raise RuntimeError("request_observation_expired")
                item = {**old, "age_ms": age}
                result.append(item)
        return result

    def relevant(self, action: dict, parameters: dict, *, dangerous: bool = False) -> tuple[list[dict], str]:
        required = action.get("requires_observations", [])
        observations = self.snapshot(required)
        if len(observations) != len(required):
            return observations, "observation_missing"
        for item in observations:
            if item["health"]["status"] != "ok":
                return observations, item["health"]["reason_code"]
        if "target" in parameters and "target_ref" in parameters:
            return observations, "target_binding_ambiguous"
        canonical = "target_ref" in parameters
        field = "target_ref" if canonical else "target"
        if field in parameters:
            target = parameters[field]
            if not isinstance(target, dict):
                return observations, "target_binding_invalid"
            mandatory = ("observation_id", "source_epoch", "stream_id", "track_session", "object_id")
            if canonical and any(type(target.get(k)) is not str or not target[k].strip() for k in mandatory):
                return observations, "target_binding_incomplete"
            matched = [o for o in observations if o["observation_id"] == target.get("observation_id")]
            if len(matched) != 1:
                return observations, "target_observation_changed"
            item = matched[0]
            data = item["data"]
            if canonical:
                for key, actual in (("source_epoch", item["source_epoch"]), ("stream_id", data.get("stream_id"))):
                    if actual != target[key]:
                        return observations, f"target_{key}_changed"
                objects = data.get("objects")
                if not isinstance(objects, list):
                    return observations, "target_object_missing"
                objects = [obj for obj in objects if isinstance(obj, dict) and obj.get("object_id") == target["object_id"]]
                if not objects:
                    return observations, "target_object_missing"
                if len(objects) != 1:
                    return observations, "target_object_ambiguous"
                if objects[0].get("track_session") != target["track_session"]:
                    return observations, "target_track_session_changed"
                for key, actual in (("frame_id", data.get("frame_id")), ("observation_seq", item["seq"])):
                    if key in target and key in parameters and target[key] != parameters[key]:
                        return observations, "target_binding_ambiguous"
                    expected = target.get(key, parameters.get(key))
                    if key in target or key in parameters:
                        if type(expected) is not type(actual) or expected != actual:
                            return observations, f"target_{key}_changed"
                if "observation_seq" in data and (type(data["observation_seq"]) is not int or data["observation_seq"] != item["seq"]):
                    return observations, "target_observation_seq_changed"
            else:
                for key in ("source_epoch", "session_id", "camera_session", "tracking_session", "frame_id", "object_id"):
                    actual = item["source_epoch"] if key == "source_epoch" else data.get(key)
                    if key in target and (actual is None or actual != target[key]):
                        return observations, f"target_{key}_changed"
                if dangerous and ("object_id" not in target or "frame_id" not in target or
                                  not any(k in target for k in ("source_epoch", "session_id", "camera_session", "tracking_session"))):
                    return observations, "target_binding_incomplete"
        elif dangerous and required:
            return observations, "target_binding_required"
        return observations, ""

    def status(self) -> dict:
        with self._lock:
            return {"sources": {sid: {"topic": spec["topic"],
                                      "reason_code": self._diagnostics.get(sid, "observation_missing"),
                                      "has_frame": sid in self._frames}
                                for sid, spec in self._specs.items()}, "closed": self._closed}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for unsubscribe in self._subscriptions.values():
                unsubscribe()
            self._subscriptions.clear()
            self._frames.clear()
