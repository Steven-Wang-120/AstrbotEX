"""Bounded, in-memory evidence from actual backend and EX admission boundaries."""
from __future__ import annotations

import copy
import json
import queue
import threading
import time
import uuid
from collections import OrderedDict


def display(value, *, limit=65536):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) <= limit:
        return {"value": copy.deepcopy(value), "truncated": False, "original_bytes": len(raw)}
    return {"value": None, "preview": raw[:limit].decode("utf-8", errors="ignore"),
            "truncated": True, "original_bytes": len(raw)}


class RequestHistory:
    def __init__(self, *, notify=None, redact=lambda value: value, capacity=128):
        self.queue = queue.Queue(maxsize=256)
        self._notify, self._redact, self.capacity = notify, redact, capacity
        self._lock = threading.RLock()
        self._records = OrderedDict()
        self._by_snapshot = {}
        self._sequence = 0
        self._lost = 0
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name="decision-history", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._closed.is_set() or not self.queue.empty():
            try:
                event = self.queue.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                self.consume(event)
            except Exception:
                with self._lock:
                    self._lost += 1
            finally:
                self.queue.task_done()

    def consume(self, event):
        snapshot_id = event.get("snapshot_id")
        kind = event.get("kind")
        with self._lock:
            if kind == "prepared":
                self._sequence += 1
                rid = uuid.uuid4().hex
                record = {"request_id": rid, "sequence": self._sequence, "snapshot_id": snapshot_id,
                          "started_monotonic_ns": event["time_ns"], "completed_monotonic_ns": None,
                          "versions": copy.deepcopy(event["snapshot"]["versions"]),
                          "ex_session": event["snapshot"]["versions"]["ex_session"],
                          "backend": event["backend"], "backend_type": event["backend_type"],
                          "service_generation": event.get("service_generation"),
                          "model": None, "model_revision": None,
                          "snapshot": display(self._redact(event["snapshot"]), limit=16384),
                          "actual_request": None, "actual_response": None,
                          "input_sha256": None, "owner_mapping": {},
                          "prepared": True, "post_attempted": False, "post_written_to_socket": None,
                          "response_received": False, "backend_result": None, "backend_error_code": None,
                          "backend_record_missing": False, "ex_outcomes": [], "command_ids": [],
                          "evidence_semantics": "socket write does not prove remote receipt or completion"}
                self._records[rid] = record
                self._by_snapshot[snapshot_id] = rid
                while len(self._records) > self.capacity:
                    old_id, old = self._records.popitem(last=False)
                    if self._by_snapshot.get(old["snapshot_id"]) == old_id:
                        self._by_snapshot.pop(old["snapshot_id"], None)
            else:
                rid = self._by_snapshot.get(snapshot_id)
                if rid is None:
                    self._lost += 1
                    return
                record = self._records[rid]
                if kind in {"backend_progress", "backend_returned"}:
                    trace = event.get("record") or event.get("values") or {}
                    if trace.get("snapshot_id", snapshot_id) != snapshot_id:
                        record["backend_record_missing"] = True
                        trace = {}
                    if kind == "backend_progress" and record["completed_monotonic_ns"] is not None:
                        record["late_backend_progress"] = True
                    self._merge(record, trace)
                    if kind == "backend_returned":
                        record["completed_monotonic_ns"] = event["time_ns"]
                        record["backend_result"] = self._redact(copy.deepcopy(event.get("result")))
                        record["backend_error_code"] = event.get("error_code")
                        record["backend_record_missing"] = record["backend_record_missing"] or event.get("record_missing", False)
                elif kind == "outcome":
                    outcome = {key: copy.deepcopy(event[key]) for key in ("outcome", "reason_code", "details", "time_ns")}
                    record["ex_outcomes"] = (record["ex_outcomes"] + [outcome])[-8:]
                    record["ex_outcome"] = ("discarded" if event["outcome"] == "discarded" else
                                            "rejected" if event["outcome"] in {"blocked", "partial_execution"} else "accepted")
                    record["service_outcome"] = event["outcome"]
                    for cid in event["details"].get("commands", []):
                        if cid not in record["command_ids"]:
                            record["command_ids"].append(cid)
            self._bound_record(record)
            notice = {"request_id": rid, "snapshot_id": snapshot_id, "sequence": record["sequence"], "kind": kind}
        if self._notify:
            self._notify(notice)

    @staticmethod
    def _bound_record(record):
        raw_size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode())
        if raw_size <= 65536:
            return
        record["record_truncated"] = True
        record["original_record_bytes"] = max(raw_size, record.get("original_record_bytes", 0))
        for name in ("backend_result", "owner_mapping", "probability_normalization", "token_budgets"):
            value = record.get(name)
            if value is not None:
                record[name + "_display"] = display(value, limit=1024)
                record[name] = None
        record["ex_outcomes"] = [{"outcome": item["outcome"], "reason_code": item["reason_code"],
                                  "time_ns": item["time_ns"], "details": {},
                                  "details_display": display(item["details"], limit=1024)}
                                 for item in record["ex_outcomes"]]
        # The three large payloads each have a 16-KiB budget. Shrink them further
        # if metadata plus JSON escaping would exceed the whole-record budget.
        if len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()) > 65536:
            for name in ("snapshot", "actual_request", "actual_response"):
                shown = record.get(name)
                if shown is not None:
                    text = json.dumps(shown.get("value"), ensure_ascii=False) if shown.get("value") is not None else shown.get("preview", "")
                    record[name] = {"value": None, "preview": text.encode()[:4096].decode(errors="ignore"),
                                    "truncated": True, "original_bytes": shown["original_bytes"]}

    def _merge(self, record, trace):
        if "request_body" in trace and trace["request_body"] is not None:
            record["actual_request_bytes"] = len(trace["request_body"].encode("ascii"))
            record["actual_request"] = display(self._redact(trace["request_body"]), limit=16384)
        if "raw_response" in trace and trace["raw_response"] is not None:
            record["actual_response"] = display(self._redact(trace["raw_response"]), limit=16384)
        mapping = {"input_sha256": "input_sha256", "owner_mapping": "owner_mapping",
                   "model": "model", "revision": "model_revision", "post_attempted": "post_attempted",
                   "post_written_to_socket": "post_written_to_socket", "probability_normalization": "probability_normalization",
                   "error_code": "backend_error_code", "phase": "backend_phase", "restart_required": "restart_required",
                   "http_elapsed_ms": "http_elapsed_ms", "server_inference_ms": "server_inference_ms",
                   "token_budgets": "token_budgets", "http_status": "http_status"}
        for source, destination in mapping.items():
            if source in trace:
                record[destination] = self._redact(copy.deepcopy(trace[source]))
        if "http_status" in trace or trace.get("raw_response") is not None:
            record["response_received"] = True

    def page(self, *, cursor=0, limit=20, request_id=None):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_history_page")
        with self._lock:
            items = [copy.deepcopy(record) for record in self._records.values()
                     if record["sequence"] > cursor and (request_id is None or record["request_id"] == request_id)]
            page = items[:limit]
            return {"items": page, "next_cursor": page[-1]["sequence"] if page else cursor,
                    "retained": len(self._records), "capacity": self.capacity, "lost_events": self._lost,
                    "display_budget_bytes": 65536}

    def latest(self, *, completed_only=False):
        with self._lock:
            if not self._records:
                return None
            for record in reversed(self._records.values()):
                if not completed_only or record["completed_monotonic_ns"] is not None:
                    return copy.deepcopy(record)
            return None

    def close(self):
        self._closed.set()
        self._thread.join(3)
