"""B00 EX-side wire contract checks for ``aeb.upper_steps.v1``.

This is the receiving half. It consumes the same fixture set the AEB side
consumes, so a divergence between the two parsers shows up as a failing
accept/reject or a mismatched payload hash rather than as a runtime surprise.

Nothing here touches a backend, a socket or the old ``decision.*`` surface.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from astrbot_ex.core import step_input_contracts as contracts

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "upper_control"


def materialise(case: dict) -> dict:
    """Expand a fixture that carries a builder spec instead of a payload."""
    spec = case.get("payload_builder")
    if not spec:
        return case["payload"]
    if spec["kind"] != "oversize_batch":
        raise AssertionError(f"unknown payload builder {spec['kind']!r}")
    payload = json.loads(json.dumps(spec["base"]))
    payload["segments"] = [
        {
            "segment_id": f"seg-{index + 1:04d}",
            "ordinal": index,
            "text": "机" * spec["chars"],
        }
        for index in range(spec["segments"])
    ]
    return payload


def load_cases() -> list[dict]:
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    return [
        json.loads((FIXTURES / entry["name"]).read_text(encoding="utf-8"))
        for entry in manifest["files"]
    ]


def shared_cases() -> list[dict]:
    return [case for case in load_cases() if "ex" in case["sides"]]


def trusted_of(case: dict):
    block = case.get("trusted")
    if block is None:
        return None
    return contracts.TrustedIngress(
        robot_id=block["robot_id"],
        ex_session=block["ex_session"],
        route_refs=frozenset(block["route_refs"]),
    )


class FixtureIntegrityTests(unittest.TestCase):
    def test_manifest_lists_every_case_file(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        on_disk = {path.name for path in FIXTURES.glob("*.json")} - {"manifest.json"}
        self.assertEqual(on_disk, {entry["name"] for entry in manifest["files"]})

    def test_every_case_file_matches_its_recorded_sha256(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            raw = (FIXTURES / entry["name"]).read_bytes()
            self.assertEqual(
                hashlib.sha256(raw).hexdigest(),
                entry["sha256"],
                f"{entry['name']} is not the reviewed byte sequence",
            )

    def test_shared_cases_are_wire_batches(self):
        for case in shared_cases():
            with self.subTest(case=case["case_id"]):
                self.assertEqual(case["kind"], "batch")

    def test_the_receiving_half_gets_meaningful_coverage(self):
        self.assertGreaterEqual(len(shared_cases()), 15)


class SharedFixtureTests(unittest.TestCase):
    """Accept/reject and payload hash must match the AEB parser exactly."""

    def test_shared_cases(self):
        for case in shared_cases():
            with self.subTest(case=case["case_id"]):
                payload = materialise(case)
                if case["expected_accept"]:
                    batch = contracts.InjectionBatch.parse(payload)
                    trusted = trusted_of(case)
                    if trusted is not None:
                        contracts.check_ingress(batch, trusted)
                    if "previous" in case:
                        previous = contracts.InjectionBatch.parse(case["previous"])
                        self.assertEqual(
                            contracts.check_replay(previous, batch),
                            case.get("expect_replay", False),
                        )
                    self.assertEqual(batch.payload_hash(), case["expected_hash"])
                    self.assertEqual(
                        [segment.text for segment in batch.segments],
                        case["expected_texts"],
                    )
                    self.assertEqual(batch.visibility, "decision_only")
                    self.assertEqual(batch.schema, "aeb.upper_steps.v1")
                else:
                    self.assertRejected(case, payload)

    def assertRejected(self, case, payload):
        with self.assertRaises(contracts.UpperContractError) as raised:
            batch = contracts.InjectionBatch.parse(payload)
            trusted = trusted_of(case)
            if trusted is not None:
                contracts.check_ingress(batch, trusted)
            if "previous" in case:
                previous = contracts.InjectionBatch.parse(case["previous"])
                contracts.check_replay(previous, batch)
        self.assertEqual(raised.exception.code, case["expected_error"])


BASE = {
    "schema": "aeb.upper_steps.v1",
    "request_id": "req-1",
    "plan_id": "plan-1",
    "plan_revision": 1,
    "batch_id": "batch-1",
    "robot_id": "robot-1",
    "route_ref": "route-1",
    "ex_session": "ex-1",
    "catalog_version": "cat-1",
    "prompt_version": "prompt-1",
    "visibility": "decision_only",
    "segments": [{"segment_id": "s1", "ordinal": 0, "text": "寻找杯子。"}],
}


class ParserHardeningTests(unittest.TestCase):
    def test_bool_is_not_an_integer(self):
        self.assertIsInstance(True, int)
        payload = dict(BASE, plan_revision=True)
        with self.assertRaises(contracts.UpperContractError) as raised:
            contracts.InjectionBatch.parse(payload)
        self.assertEqual(raised.exception.code, "invalid_batch")

    def test_unknown_field_is_rejected_not_dropped(self):
        payload = dict(BASE, permissions={"shell": True})
        with self.assertRaises(contracts.UpperContractError) as raised:
            contracts.InjectionBatch.parse(payload)
        self.assertEqual(raised.exception.code, "invalid_batch")

    def test_binary_visibility_is_rejected(self):
        payload = dict(BASE, visibility="user")
        with self.assertRaises(contracts.UpperContractError):
            contracts.InjectionBatch.parse(payload)

    def test_other_schema_is_refused_not_guessed(self):
        payload = dict(BASE, schema="aeb.upper_steps.v2")
        with self.assertRaises(contracts.UpperContractError):
            contracts.InjectionBatch.parse(payload)

    def test_oversize_is_rejected_without_truncation(self):
        payload = dict(BASE)
        payload["segments"] = [
            {"segment_id": f"s{i}", "ordinal": i, "text": "机" * 4096}
            for i in range(24)
        ]
        with self.assertRaises(contracts.UpperContractError) as raised:
            contracts.InjectionBatch.parse(payload)
        self.assertEqual(raised.exception.code, "payload_too_large")

    def test_non_finite_is_not_produced_by_the_canonical_form(self):
        with self.assertRaises(contracts.UpperContractError):
            contracts.canonical({"x": float("nan")})

    def test_canonical_hash_is_stable_across_key_order(self):
        forward = contracts.InjectionBatch.parse(BASE).payload_hash()
        backward = contracts.InjectionBatch.parse(
            dict(reversed(list(BASE.items())))
        ).payload_hash()
        self.assertEqual(forward, backward)

    def test_hash_matches_a_plain_canonicalisation(self):
        batch = contracts.InjectionBatch.parse(BASE)
        expected = hashlib.sha256(
            json.dumps(
                batch.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(batch.payload_hash(), expected)

    def test_joined_text_keeps_paragraph_boundaries(self):
        payload = dict(BASE)
        payload["segments"] = [
            {"segment_id": "s1", "ordinal": 0, "text": "第一步。"},
            {"segment_id": "s2", "ordinal": 1, "text": "第二步。"},
        ]
        batch = contracts.InjectionBatch.parse(payload)
        self.assertEqual(batch.joined_text(), "第一步。\n第二步。")


class IngressTests(unittest.TestCase):
    def test_a_valid_id_is_not_an_authorisation(self):
        batch = contracts.InjectionBatch.parse(BASE)
        trusted = contracts.TrustedIngress(
            robot_id="robot-2", ex_session="ex-1", route_refs=frozenset({"route-1"})
        )
        with self.assertRaises(contracts.UpperContractError) as raised:
            contracts.check_ingress(batch, trusted)
        self.assertEqual(raised.exception.code, "session_mismatch")

    def test_stale_session_is_rejected(self):
        batch = contracts.InjectionBatch.parse(BASE)
        trusted = contracts.TrustedIngress(
            robot_id="robot-1", ex_session="ex-2", route_refs=frozenset({"route-1"})
        )
        with self.assertRaises(contracts.UpperContractError) as raised:
            contracts.check_ingress(batch, trusted)
        self.assertEqual(raised.exception.code, "session_mismatch")

    def test_unregistered_route_is_rejected(self):
        batch = contracts.InjectionBatch.parse(BASE)
        trusted = contracts.TrustedIngress(
            robot_id="robot-1", ex_session="ex-1", route_refs=frozenset({"route-9"})
        )
        with self.assertRaises(contracts.UpperContractError) as raised:
            contracts.check_ingress(batch, trusted)
        self.assertEqual(raised.exception.code, "route_mismatch")

    def test_trusted_ingress_rejects_a_path_like_robot(self):
        with self.assertRaises(contracts.UpperContractError):
            contracts.TrustedIngress(
                robot_id="../../robot",
                ex_session="ex-1",
                route_refs=frozenset({"route-1"}),
            )


class ReceiptTests(unittest.TestCase):
    def test_received_and_injected_prove_delivery_only(self):
        batch = contracts.InjectionBatch.parse(BASE)
        for phase in ("received", "injected"):
            with self.subTest(phase=phase):
                receipt = contracts.receipt_for(batch, phase)
                self.assertTrue(receipt.is_delivered)
                self.assertEqual(receipt.payload_hash, batch.payload_hash())
                self.assertEqual(receipt.error_code, "")

    def test_rejected_receipt_must_name_a_code(self):
        batch = contracts.InjectionBatch.parse(BASE)
        with self.assertRaises(contracts.UpperContractError):
            contracts.receipt_for(batch, "rejected")

    def test_delivered_receipt_must_not_carry_an_error(self):
        batch = contracts.InjectionBatch.parse(BASE)
        with self.assertRaises(contracts.UpperContractError):
            contracts.receipt_for(batch, "injected", "invalid_batch")

    def test_receipt_round_trips(self):
        batch = contracts.InjectionBatch.parse(BASE)
        receipt = contracts.receipt_for(batch, "rejected", "invalid_batch")
        parsed = contracts.DeliveryReceipt.parse(receipt.to_dict())
        self.assertEqual(parsed, receipt)

    def test_unknown_phase_is_rejected(self):
        with self.assertRaises(contracts.UpperContractError):
            contracts.DeliveryReceipt.parse(
                {
                    "batch_id": "batch-1",
                    "request_id": "req-1",
                    "payload_hash": "abc",
                    "phase": "executed",
                    "error_code": "",
                }
            )


class InstructionInputTests(unittest.TestCase):
    def test_from_batch_keeps_order_and_boundaries(self):
        payload = dict(BASE)
        payload["segments"] = [
            {"segment_id": "s1", "ordinal": 0, "text": "第一步。"},
            {"segment_id": "s2", "ordinal": 1, "text": "第二步。"},
        ]
        batch = contracts.InjectionBatch.parse(payload)
        instruction = contracts.InstructionInput.from_batch(batch)
        self.assertEqual(instruction.joined_text, "第一步。\n第二步。")
        self.assertEqual(
            [segment.ordinal for segment in instruction.segments], [0, 1]
        )
        self.assertEqual(instruction.payload_hash, batch.payload_hash())


if __name__ == "__main__":
    unittest.main()
