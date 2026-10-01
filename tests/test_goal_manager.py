from __future__ import annotations

import hashlib
import threading
import unittest

from astrbot_ex.core.actions.models import ContractError, parse_action_manifest
from astrbot_ex.core.decision.catalog import CapabilityCatalog, CapabilityInput
from astrbot_ex.core.decision.goal_manager import GoalManager


def make_catalog(owners=("arm",), *, resource=None, observe=False):
    catalog = CapabilityCatalog()
    records = []
    for owner in owners:
        manifest = {"id": owner, "action_api_version": 2, "actions": [{
            "action_id": f"{owner}.move.v1", "description": "Move safely",
            "schema": {"type": "object", "properties": {"meters": {"type": "integer", "minimum": 0}},
                       "required": ["meters"], "additionalProperties": False},
            "resources": [resource] if resource else [], "operations": ["start", "cancel"],
            "cancel_timeout_ms": 80, "max_duration_ms": 1000,
            "requires_observations": [f"{owner}_pose"] if observe else []}],
            "observation_sources": {f"{owner}_pose": {"topic": "sensor.pose", "max_age_ms": 80,
                                                       "required_fields": ["position"]}} if observe else {}}
        guide = {"status": "available", "reason": "", "text": "Use safety checks",
                 "content_hash": hashlib.sha256(b"Use safety checks").hexdigest()}
        records.append(CapabilityInput(owner, 1, parse_action_manifest(manifest, owner=owner), {}, guide, True, "1"))
    catalog.refresh(records)
    return catalog


def goal_payload(manager, n=1, owners=("arm",), **extra):
    actions = [f"{owner}.move.v1" for owner in owners]
    return {"schema_version": 1, "request_id": f"req-{n}", "ex_session": manager.ex_session,
            "task_id": "task", "step_id": "step", "goal_id": f"goal-{n}",
            "goal_text_en": "Move safely.", "allowed_actions": actions,
            "parameters": {aid: {"meters": n} for aid in actions}, "lease_ms": 10000, **extra}


class GoalManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000_000
        self.revoked = []
        self.goals = GoalManager(make_catalog(), revoke=lambda: self.revoked.append(True), clock_ns=lambda: self.now)

    def activate(self, n=1):
        self.goals.submit(goal_payload(self.goals, n))
        self.goals.resolve_stop(self.goals.gate_epoch, True)

    def test_G01_competing_cas_is_atomic(self):
        barrier = threading.Barrier(3)
        results = []
        def submit(n):
            barrier.wait()
            try:
                results.append(self.goals.submit(goal_payload(self.goals, n, expected_revision=0)))
            except ContractError as exc:
                results.append(exc.code)
        workers = [threading.Thread(target=submit, args=(n,)) for n in (1, 2)]
        for worker in workers: worker.start()
        barrier.wait()
        for worker in workers: worker.join(1)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn("revision_conflict", results)
        self.assertEqual(self.goals.revision, 1)

    def test_validation_does_not_mix_revision_parameters(self):
        self.activate()
        old = self.goals.active
        invalid = goal_payload(self.goals, 2)
        invalid["parameters"]["arm.move.v1"]["meters"] = -1
        with self.assertRaises(ContractError): self.goals.submit(invalid)
        self.assertIs(self.goals.active, old)
        self.assertEqual(self.goals.revision, 1)
        self.assertEqual(len(self.revoked), 1)

    def test_idempotency_conflict_and_capacity_never_forget_requests(self):
        payload = goal_payload(self.goals)
        response = self.goals.submit(payload)
        self.assertEqual(self.goals.submit(payload), response)
        payload["parameters"]["arm.move.v1"]["meters"] = 7
        with self.assertRaises(ContractError) as exc: self.goals.submit(payload)
        self.assertEqual(exc.exception.code, "duplicate_request_id_conflict")
        self.goals._request_limit = 1
        with self.assertRaises(ContractError) as exc: self.goals.submit(goal_payload(self.goals, 2))
        self.assertEqual(exc.exception.code, "request_capacity")
        self.assertEqual(self.goals.revision, 1)

    def test_G02_one_pending_and_superseded_stop_cannot_activate(self):
        self.activate()
        for n in range(2, 102):
            self.goals.submit(goal_payload(self.goals, n))
            self.assertFalse(self.goals.resolve_stop(self.goals.gate_epoch - 1, True))
        self.assertEqual(self.goals.pending_replace.payload()["goal_id"], "goal-101")
        self.assertEqual(self.goals.active.payload()["goal_id"], "goal-1")
        self.assertTrue(self.goals.resolve_stop(self.goals.gate_epoch, True))
        self.assertEqual(self.goals.active.payload()["parameters"]["arm.move.v1"]["meters"], 101)

    def test_G03_unknown_stop_blocked_until_review_and_new_submit(self):
        self.activate()
        self.goals.submit(goal_payload(self.goals, 2))
        epoch = self.goals.gate_epoch
        self.assertFalse(self.goals.resolve_stop(epoch, False))
        self.assertEqual(self.goals.phase, "blocked")
        self.assertFalse(self.goals.resolve_stop(epoch, True))
        self.goals.submit(goal_payload(self.goals, 3))
        self.assertEqual(self.goals.phase, "blocked")
        self.goals.reviewed(self.goals.gate_epoch)
        self.assertIsNone(self.goals.active)
        self.assertIsNone(self.goals.pending_replace)
        self.assertEqual(self.goals.phase, "idle")

    def test_lease_renew_replay_cannot_extend_or_restore_revoked_goal(self):
        self.activate()
        request = {"schema_version": 1, "request_id": "renew", "ex_session": self.goals.ex_session,
                   "goal_id": "goal-1", "goal_revision": 1, "lease_ms": 10}
        self.goals.renew(request)
        expiry = self.goals.active.expires_ns
        self.now += 5_000_000
        self.goals.renew(request)
        self.assertEqual(expiry, self.goals.active.expires_ns)
        self.now += 10_000_000
        self.assertTrue(self.goals.expire())
        request["request_id"] = "renew-new"
        with self.assertRaises(ContractError): self.goals.renew(request)
