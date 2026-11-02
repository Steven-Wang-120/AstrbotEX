"""B09 unit tests: strict parsing, the durable ledger, and the ingress.

These tests never touch a real socket.  They prove that a batch is validated,
recorded once, and handed to the input adapter with its text and order intact —
nothing about execution, and nothing about a second delivery attempt.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from astrbot_ex.core.decision.instruction_input import (
    DecisionInstructionInput,
    InstructionBatch,
    InstructionInputError,
    StepSegment,
)
from astrbot_ex.core.step_input import (
    METHOD_CAPABILITIES,
    METHOD_INJECT,
    SCHEMA,
    StepInputError,
    StepInputIngress,
    StepInputLog,
    TrustedControlContext,
    build_trusted_context,
    parse_injection_batch,
)

SEGMENT_TEXTS = ["寻找红色杯子。", "接近并对齐同一个目标。", "确认抓取位姿。"]


def make_payload(**overrides):
    payload = {
        "schema": SCHEMA,
        "request_id": "framework-request",
        "plan_id": "framework-plan",
        "plan_revision": 1,
        "batch_id": "framework-batch",
        "robot_id": "trusted-config-robot",
        "route_ref": "framework-route",
        "ex_session": "current-ex-session",
        "catalog_version": "snapshot-hash",
        "prompt_version": "snapshot-hash",
        "visibility": "decision_only",
        "segments": [
            {"segment_id": f"framework-s{index}", "ordinal": index, "text": text}
            for index, text in enumerate(SEGMENT_TEXTS)
        ],
    }
    payload.update(overrides)
    return payload


class FakeService:
    """Minimal stand-in for DecisionService's instruction entry points."""

    def __init__(self, *, ready: bool = True, reason: str = "", delay: float = 0.0,
                 phase: str = "injected", detail: str = "accepted", error=None,
                 settle_later=None) -> None:
        self.ready, self.reason = ready, reason
        self.delay, self.phase, self.detail = delay, phase, detail
        self.error = error
        self.settle_later = settle_later
        self.calls: list[InstructionBatch] = []
        self._backend_name = "fake"
        self._lock = threading.Lock()

    def instruction_ready(self):
        return self.ready, self.reason

    def submit_instruction(self, batch, *, timeout_sec=1.0, on_settled=None):
        with self._lock:
            self.calls.append(batch)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.settle_later is not None:
            def finish():
                time.sleep(self.settle_later)
                if callable(on_settled):
                    on_settled(self.phase, self.detail)
            threading.Thread(target=finish, daemon=True).start()
            return {"phase": "received", "detail": "queued", "model": "fake-model"}
        if callable(on_settled):
            on_settled(self.phase, self.detail)
        return {"phase": self.phase, "detail": self.detail, "model": "fake-model"}


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


class ParserTest(unittest.TestCase):
    def test_three_segments_keep_order_and_exact_text(self) -> None:
        batch = parse_injection_batch(make_payload())
        self.assertEqual(batch.segment_count, 3)
        self.assertEqual([segment.text for segment in batch.segments], SEGMENT_TEXTS)
        self.assertEqual([segment.ordinal for segment in batch.segments], [0, 1, 2])
        self.assertEqual(batch.segments[1].text, "接近并对齐同一个目标。")

    def test_single_segment_is_allowed(self) -> None:
        payload = make_payload(segments=[{"segment_id": "s0", "ordinal": 0, "text": "只发一段。"}])
        batch = parse_injection_batch(payload)
        self.assertEqual(batch.segment_count, 1)
        self.assertEqual(batch.segments[0].text, "只发一段。")

    def test_unknown_top_level_field_is_rejected(self) -> None:
        with self.assertRaises(StepInputError) as caught:
            parse_injection_batch(make_payload(public_text="说点什么"))
        self.assertEqual(caught.exception.code, "invalid_batch")
        self.assertIn("public_text", caught.exception.detail)

    def test_unknown_segment_field_is_rejected(self) -> None:
        payload = make_payload()
        payload["segments"][0]["params"] = {"speed": 1}
        with self.assertRaises(StepInputError) as caught:
            parse_injection_batch(payload)
        self.assertIn("params", caught.exception.detail)

    def test_missing_required_field_is_rejected(self) -> None:
        payload = make_payload()
        payload.pop("route_ref")
        with self.assertRaises(StepInputError) as caught:
            parse_injection_batch(payload)
        self.assertIn("route_ref", caught.exception.detail)

    def test_schema_and_visibility_are_pinned(self) -> None:
        for key, value in (("schema", "aeb.upper_steps.v2"), ("visibility", "public")):
            with self.subTest(key=key):
                with self.assertRaises(StepInputError):
                    parse_injection_batch(make_payload(**{key: value}))

    def test_plan_revision_must_be_a_real_positive_integer(self) -> None:
        for value in (0, -1, True, 1.0, "1", None):
            with self.subTest(value=value):
                with self.assertRaises(StepInputError):
                    parse_injection_batch(make_payload(plan_revision=value))

    def test_ordinals_must_be_contiguous_from_zero(self) -> None:
        payload = make_payload()
        payload["segments"][1]["ordinal"] = 2
        with self.assertRaises(StepInputError) as caught:
            parse_injection_batch(payload)
        self.assertIn("连续", caught.exception.detail)

    def test_ordinal_rejects_bool(self) -> None:
        payload = make_payload()
        payload["segments"][0]["ordinal"] = False
        with self.assertRaises(StepInputError):
            parse_injection_batch(payload)

    def test_empty_and_oversized_text_are_rejected(self) -> None:
        for text in ("   ", "", "x" * 4097):
            with self.subTest(length=len(text)):
                payload = make_payload(segments=[{"segment_id": "s0", "ordinal": 0, "text": text}])
                with self.assertRaises(StepInputError):
                    parse_injection_batch(payload)

    def test_too_many_segments_are_rejected(self) -> None:
        segments = [{"segment_id": f"s{i}", "ordinal": i, "text": "a"} for i in range(65)]
        with self.assertRaises(StepInputError):
            parse_injection_batch(make_payload(segments=segments))

    def test_ids_reject_whitespace_and_overlong_values(self) -> None:
        for value in ("has space", "a" * 129, "", "tab\there"):
            with self.subTest(value=value[:12]):
                with self.assertRaises(StepInputError):
                    parse_injection_batch(make_payload(batch_id=value))

    def test_non_mapping_payload_is_rejected(self) -> None:
        for value in (None, [], "text", 3):
            with self.subTest(value=value):
                with self.assertRaises(StepInputError):
                    parse_injection_batch(value)

    def test_payload_hash_is_stable_and_content_sensitive(self) -> None:
        first = parse_injection_batch(make_payload())
        second = parse_injection_batch(make_payload())
        changed = parse_injection_batch(
            make_payload(segments=[
                {"segment_id": "framework-s0", "ordinal": 0, "text": "换个说法。"},
            ])
        )
        self.assertEqual(first.payload_hash, second.payload_hash)
        self.assertNotEqual(first.payload_hash, changed.payload_hash)


class ReceiptLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "execution" / "upper_step_input.jsonl"

    def _batch(self, **overrides) -> InstructionBatch:
        return parse_injection_batch(make_payload(**overrides))

    def test_register_is_idempotent_for_identical_content(self) -> None:
        log = StepInputLog(self.path)
        batch = self._batch()
        first, created = log.register(batch, connection_id="c1", ex_session="s1", robot_id="r1")
        self.assertTrue(created)
        self.assertEqual(first.phase, "received")
        second, created_again = log.register(batch, connection_id="c1", ex_session="s1", robot_id="r1")
        self.assertFalse(created_again)
        self.assertIs(second, first)
        self.assertEqual(len(log.entries()), 1)

    def test_same_id_different_content_is_a_conflict(self) -> None:
        log = StepInputLog(self.path)
        log.register(self._batch(), connection_id="c1", ex_session="s1", robot_id="r1")
        other = self._batch(segments=[{"segment_id": "s0", "ordinal": 0, "text": "别的指令。"}])
        with self.assertRaises(StepInputError) as caught:
            log.register(other, connection_id="c1", ex_session="s1", robot_id="r1")
        self.assertEqual(caught.exception.code, "id_conflict")

    def test_same_id_from_another_session_is_a_conflict(self) -> None:
        log = StepInputLog(self.path)
        log.register(self._batch(), connection_id="c1", ex_session="s1", robot_id="r1")
        with self.assertRaises(StepInputError):
            log.register(self._batch(), connection_id="c1", ex_session="s2", robot_id="r1")

    def test_receipts_survive_reopen(self) -> None:
        log = StepInputLog(self.path)
        receipt, _ = log.register(self._batch(), connection_id="c1", ex_session="s1", robot_id="r1")
        log.update(receipt.batch_id, "injected", reason="accepted")
        reopened = StepInputLog(self.path)
        entry = reopened.get(receipt.batch_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.phase, "injected")
        self.assertEqual(entry.payload_hash, receipt.payload_hash)
        self.assertEqual(entry.segment_ids, ("framework-s0", "framework-s1", "framework-s2"))

    def test_restart_turns_in_flight_receipts_into_unknown(self) -> None:
        log = StepInputLog(self.path)
        receipt, _ = log.register(self._batch(), connection_id="c1", ex_session="s1", robot_id="r1")
        self.assertEqual(receipt.phase, "received")
        reopened = StepInputLog(self.path)
        entry = reopened.get(receipt.batch_id)
        self.assertEqual(entry.phase, "unknown")
        self.assertEqual(entry.reason, "recovered_after_restart")
        # Nothing re-injects on recovery: the ledger only records the gap.
        self.assertEqual([item["phase"] for item in reopened.entries()], ["unknown"])

    def test_torn_line_is_ignored_not_replayed(self) -> None:
        log = StepInputLog(self.path)
        receipt, _ = log.register(self._batch(), connection_id="c1", ex_session="s1", robot_id="r1")
        log.update(receipt.batch_id, "injected", reason="accepted")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"batch_id": "torn", "pha')
        reopened = StepInputLog(self.path)
        self.assertEqual(len(reopened.entries()), 1)
        self.assertEqual(reopened.get(receipt.batch_id).phase, "injected")

    def test_unknown_phase_in_file_becomes_unknown(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"batch_id": "b1", "phase": "injected!", "payload_hash": "h"}) + "\n",
            encoding="utf-8",
        )
        log = StepInputLog(self.path)
        self.assertEqual(log.get("b1").phase, "unknown")

    def test_trim_keeps_the_newest_entries(self) -> None:
        log = StepInputLog(self.path, max_entries=2)
        for index in range(4):
            batch = self._batch(batch_id=f"batch-{index}")
            log.register(batch, connection_id="c1", ex_session="s1", robot_id="r1")
        self.assertEqual([item["batch_id"] for item in log.entries()], ["batch-2", "batch-3"])

    def test_public_receipt_never_leaks_payload_text(self) -> None:
        log = StepInputLog(self.path)
        receipt, _ = log.register(self._batch(), connection_id="c1", ex_session="s1", robot_id="r1")
        public = receipt.public()
        self.assertEqual(
            set(public),
            {"batch_id", "request_id", "payload_hash", "phase", "segment_count", "ex_session", "reason"},
        )
        blob = json.dumps(public, ensure_ascii=False)
        for text in SEGMENT_TEXTS:
            self.assertNotIn(text, blob)


class TrustedContextTest(unittest.TestCase):
    def test_route_refs_accept_comma_string_and_list(self) -> None:
        from_string = build_trusted_context(robot_id="r", route_refs="a, b ,c", ex_session="s")
        from_list = build_trusted_context(robot_id="r", route_refs=["a", "b", "c"], ex_session="s")
        self.assertEqual(from_string.route_refs, ("a", "b", "c"))
        self.assertEqual(from_list.route_refs, ("a", "b", "c"))
        self.assertTrue(from_string.ready)

    def test_missing_pieces_are_not_ready(self) -> None:
        self.assertFalse(build_trusted_context(robot_id="", route_refs="a", ex_session="s").ready)
        self.assertFalse(build_trusted_context(robot_id="r", route_refs="", ex_session="s").ready)
        self.assertFalse(build_trusted_context(robot_id="r", route_refs="a", ex_session="").ready)


class IngressTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "upper_step_input.jsonl"
        self.trusted = TrustedControlContext(
            robot_id="trusted-config-robot",
            route_refs=("framework-route",),
            ex_session="current-ex-session",
        )

    def _ingress(self, service=None, *, trusted=None, capacity=8, request_budget=0.6):
        service = service or FakeService()
        log = StepInputLog(self.path)
        ingress = StepInputIngress(
            log=log,
            adapter=DecisionInstructionInput(service),
            trusted_context=lambda connection_id: trusted if trusted is not None else self.trusted,
            queue_capacity=capacity,
            request_budget_sec=request_budget,
            worker_budget_sec=2.0,
        )
        ingress.start()
        self.addCleanup(ingress.close)
        return ingress, log, service

    def test_capabilities_report_schema_methods_and_limits(self) -> None:
        ingress, _, _ = self._ingress()
        result, binary = ingress.handle("c1", METHOD_CAPABILITIES, {})
        self.assertIsNone(binary)
        self.assertTrue(result["ok"])
        self.assertEqual(result["schema"], SCHEMA)
        self.assertEqual(result["methods"], [METHOD_CAPABILITIES, METHOD_INJECT])
        self.assertTrue(result["input_ready"])
        self.assertEqual(result["ex_session"], "current-ex-session")
        self.assertEqual(result["limits"]["max_segments"], 64)
        self.assertEqual(result["limits"]["queue_capacity"], 8)

    def test_capabilities_report_backend_unsupported_without_fallback(self) -> None:
        ingress, _, _ = self._ingress(FakeService(ready=False, reason="backend_unsupported"))
        result, _ = ingress.handle("c1", METHOD_CAPABILITIES, {})
        self.assertFalse(result["input_ready"])
        self.assertEqual(result["input_ready_reason"], "backend_unsupported")
        self.assertNotEqual(result["backend"], "mock")

    def test_capabilities_report_missing_local_identity(self) -> None:
        ingress, _, _ = self._ingress(trusted=build_trusted_context(
            robot_id="", route_refs="r", ex_session="s"))
        result, _ = ingress.handle("c1", METHOD_CAPABILITIES, {})
        self.assertFalse(result["input_ready"])
        self.assertEqual(result["input_ready_reason"], "robot_not_configured")

    def test_wrong_channel_is_rejected(self) -> None:
        ingress, _, _ = self._ingress()
        result, _ = ingress.handle("c1", METHOD_INJECT, make_payload(), feature="text")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "session_mismatch")

    def test_unknown_method_is_rejected(self) -> None:
        ingress, _, _ = self._ingress()
        result, _ = ingress.handle("c1", "upper.steps.cancel", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unsupported_method")

    def test_binary_frame_is_rejected(self) -> None:
        ingress, _, service = self._ingress()
        result, _ = ingress.handle("c1", METHOD_INJECT, make_payload(), b"\x00\x01")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_batch")
        self.assertEqual(service.calls, [])

    def test_robot_mismatch_is_rejected(self) -> None:
        ingress, log, service = self._ingress()
        result, _ = ingress.handle("c1", METHOD_INJECT, make_payload(robot_id="someone-else"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "session_mismatch")
        self.assertEqual(service.calls, [])
        self.assertEqual(log.entries(), [])

    def test_stale_session_is_rejected(self) -> None:
        ingress, _, service = self._ingress()
        result, _ = ingress.handle("c1", METHOD_INJECT, make_payload(ex_session="old-session"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "session_mismatch")
        self.assertEqual(service.calls, [])

    def test_unregistered_route_ref_is_rejected(self) -> None:
        ingress, _, service = self._ingress()
        result, _ = ingress.handle("c1", METHOD_INJECT, make_payload(route_ref="other-route"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "route_mismatch")
        self.assertEqual(service.calls, [])

    def test_missing_trusted_context_is_rejected(self) -> None:
        ingress, _, service = self._ingress(trusted=None)
        ingress._trusted_context = lambda connection_id: None
        result, _ = ingress.handle("c1", METHOD_INJECT, make_payload())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "session_mismatch")
        self.assertEqual(service.calls, [])

    def test_happy_path_reports_injected_and_passes_text_verbatim(self) -> None:
        ingress, log, service = self._ingress()
        result, _ = ingress.handle("c1", METHOD_INJECT, make_payload())
        self.assertTrue(result["ok"])
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["receipt"]["phase"], "injected")
        self.assertEqual(result["receipt"]["segment_count"], 3)
        self.assertEqual(len(service.calls), 1)
        delivered = service.calls[0]
        self.assertEqual([segment.text for segment in delivered.segments], SEGMENT_TEXTS)
        self.assertEqual([segment.ordinal for segment in delivered.segments], [0, 1, 2])
        self.assertEqual(delivered.payload_hash, result["receipt"]["payload_hash"])
        self.assertEqual(log.get("framework-batch").phase, "injected")

    def test_duplicate_is_answered_from_the_ledger_without_second_consumption(self) -> None:
        ingress, _, service = self._ingress()
        first, _ = ingress.handle("c1", METHOD_INJECT, make_payload())
        second, _ = ingress.handle("c1", METHOD_INJECT, make_payload())
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["receipt"]["payload_hash"], second["receipt"]["payload_hash"])
        self.assertEqual(len(service.calls), 1)

    def test_conflicting_reuse_of_batch_id_is_rejected(self) -> None:
        ingress, _, service = self._ingress()
        ingress.handle("c1", METHOD_INJECT, make_payload())
        changed = make_payload(segments=[{"segment_id": "s0", "ordinal": 0, "text": "换了内容。"}])
        result, _ = ingress.handle("c1", METHOD_INJECT, changed)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "id_conflict")
        self.assertEqual(len(service.calls), 1)

    def test_backend_unsupported_rejects_before_recording(self) -> None:
        ingress, log, service = self._ingress(FakeService(ready=False, reason="backend_unsupported"))
        result, _ = ingress.handle("c1", METHOD_INJECT, make_payload())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "backend_unsupported")
        self.assertEqual(service.calls, [])
        self.assertEqual(log.entries(), [])

    def test_busy_backend_is_reported_as_input_busy(self) -> None:
        service = FakeService(error=InstructionInputError("input_busy", "active_goal"))
        ingress, log, _ = self._ingress(service)
        result, _ = ingress.handle("c1", METHOD_INJECT, make_payload())
        # It was registered, so the answer is a receipt, not a transport error.
        self.assertTrue(result["ok"])
        self.assertIn(result["receipt"]["phase"], {"received", "rejected"})
        wait_until(lambda: log.get("framework-batch").phase == "rejected")
        self.assertEqual(log.get("framework-batch").reason, "input_busy")

    def test_full_queue_returns_input_busy(self) -> None:
        service = FakeService(delay=0.5)
        ingress, log, _ = self._ingress(service, capacity=1, request_budget=0.05)
        first, _ = ingress.handle("c1", METHOD_INJECT, make_payload())
        self.assertTrue(first["ok"])
        second_payload = make_payload(batch_id="framework-batch-2",
                                      segments=[{"segment_id": "s0", "ordinal": 0, "text": "第二段。"}])
        ingress.handle("c1", METHOD_INJECT, second_payload)  # fills the single slot
        third_payload = make_payload(batch_id="framework-batch-3",
                                     segments=[{"segment_id": "s0", "ordinal": 0, "text": "第三段。"}])
        third, _ = ingress.handle("c1", METHOD_INJECT, third_payload)
        self.assertFalse(third["ok"])
        self.assertEqual(third["error"]["code"], "input_busy")
        self.assertEqual(log.get("framework-batch-3").phase, "rejected")

    def test_queued_batch_reports_received_then_settles_later(self) -> None:
        service = FakeService(settle_later=0.4, phase="injected", detail="accepted")
        ingress, log, _ = self._ingress(service, request_budget=0.05)
        result, _ = ingress.handle("c1", METHOD_INJECT, make_payload())
        self.assertTrue(result["ok"])
        self.assertEqual(result["receipt"]["phase"], "received")
        wait_until(lambda: log.get("framework-batch").phase == "injected")

    def test_unknown_outcome_is_recorded_as_unknown(self) -> None:
        service = FakeService(error=RuntimeError("boom"))
        ingress, log, _ = self._ingress(service)
        ingress.handle("c1", METHOD_INJECT, make_payload())
        wait_until(lambda: log.get("framework-batch").phase == "unknown")
        self.assertEqual(log.get("framework-batch").reason, "internal_error")

    def test_close_rejects_queued_work_instead_of_consuming_it(self) -> None:
        service = FakeService(delay=0.5)
        ingress, log, _ = self._ingress(service, capacity=4, request_budget=0.05)
        ingress.handle("c1", METHOD_INJECT, make_payload())
        queued = make_payload(batch_id="framework-batch-2",
                              segments=[{"segment_id": "s0", "ordinal": 0, "text": "排队中的指令。"}])
        ingress.handle("c1", METHOD_INJECT, queued)
        ingress.close()
        self.assertEqual(log.get("framework-batch-2").phase, "rejected")
        self.assertEqual(log.get("framework-batch-2").reason, "ingress_stopped")
        self.assertEqual(len(service.calls), 1)

    def test_closed_ingress_refuses_new_batches(self) -> None:
        ingress, _, service = self._ingress()
        ingress.close()
        result, _ = ingress.handle("c1", METHOD_INJECT, make_payload())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "backend_unsupported")
        self.assertEqual(service.calls, [])


class ReplyShapeTest(unittest.TestCase):
    """The upper controller reads receipt fields straight off the reply."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.trusted = TrustedControlContext(
            robot_id="trusted-config-robot",
            route_refs=("framework-route",),
            ex_session="current-ex-session",
        )
        self.ingress = StepInputIngress(
            log=StepInputLog(Path(self._temp.name) / "log.jsonl"),
            adapter=DecisionInstructionInput(FakeService()),
            trusted_context=lambda connection_id: self.trusted,
            request_budget_sec=1.0,
        )
        self.ingress.start()
        self.addCleanup(self.ingress.close)

    def test_success_mirrors_receipt_fields_at_the_top_level(self) -> None:
        result, _ = self.ingress.handle("c1", METHOD_INJECT, make_payload())
        for key in ("batch_id", "request_id", "payload_hash", "phase", "ex_session"):
            self.assertIn(key, result, f"{key} must be readable without unwrapping")
        self.assertEqual(result["batch_id"], "framework-batch")
        self.assertEqual(result["phase"], "injected")
        self.assertEqual(result["ex_session"], "current-ex-session")
        # The nested copy stays available and must agree.
        self.assertEqual(result["receipt"]["payload_hash"], result["payload_hash"])
        self.assertEqual(result["receipt"]["phase"], result["phase"])

    def test_error_carries_a_top_level_error_code(self) -> None:
        result, _ = self.ingress.handle("c1", METHOD_INJECT, make_payload(robot_id="intruder"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "session_mismatch")
        self.assertEqual(result["phase"], "rejected")
        self.assertEqual(result["error"]["code"], result["error_code"])

    def test_capabilities_expose_flat_limit_aliases(self) -> None:
        result, _ = self.ingress.handle("c1", METHOD_CAPABILITIES, {})
        self.assertEqual(result["max_bytes"], 256 * 1024)
        self.assertEqual(result["max_segments"], 64)
        self.assertEqual(result["limits"]["max_payload_bytes"], result["max_bytes"])


if __name__ == "__main__":
    unittest.main()
