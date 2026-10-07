"""C04 software-path evidence: real Action/Actor/Ledger/Journal/Controller, no model IO."""
from __future__ import annotations

import copy
import threading
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.actions.ledger import OwnerBinding, StopEvidence
from astrbot_ex.core.contracts import Feedback
from astrbot_ex.core.decision.backends import MockBackend
from astrbot_ex.core.decision.feedback_journal import FeedbackJournal
from tests.test_decision_service import wait_for
from tests.wiring_fixture import WiringFixture


class ScoredBackend(MockBackend):
    """Inject the frozen provider contract until reviewed provider dependencies land."""
    min_confidence = 0.6

    def __init__(self, kind="start", confidence=0.2):
        super().__init__(kind=kind)
        self.confidence = confidence
        self.original = None
        self.snapshot = None

    def decide(self, snapshot):
        decision = super().decide(snapshot)
        raw = decision.to_dict()
        for choice in raw["choices"]:
            choice["confidence"] = self.confidence
            owner = next(o for o in snapshot.owners if o["owner"] == choice["owner"])
            choice["probabilities"] = {c["option_id"]: int(c["option_id"] == choice["option_id"])
                for c in owner["candidates"] if c["eligible"]}
        self.original = copy.deepcopy(raw)
        self.snapshot = snapshot
        from astrbot_ex.core.decision.models import BackendDecision
        return BackendDecision.parse(raw)


class CapabilitiesControllerTests(WiringFixture):
    def test_configured_route_pre_admission_readonly_exact_summary(self):
        self.execute()
        before = (self.s.goals.status(), copy.deepcopy(self.c._bindings),
            copy.deepcopy(self.c._watermarks), self.c.journal.snapshot())
        result = self.request("decision.capabilities.get", {"schema_version": 1})
        self.assertEqual(set(result), {"schema_version", "ex_session", "revision", "catalog_revision",
            "control_mode", "execution", "actions"})
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["revision"], 0)
        self.assertEqual(result["ex_session"], self.s.goals.ex_session)
        self.assertEqual(result["control_mode"], "decision")
        self.assertEqual(result["execution"], {"mode": "execute", "execution_allowed": True,
            "runtime_state": "running"})
        self.assertEqual(result["actions"][0]["owner"], "arm")
        self.assertEqual(result["actions"][0]["plugin_generation"], 1)
        self.assertEqual(result["actions"][0]["action_id"], "arm.move.v1")
        self.assertEqual((self.s.goals.status(), self.c._bindings, self.c._watermarks,
            self.c.journal.snapshot()), before)
        for kwargs in ({"conn": "foreign"}, {"feature": "audio"}, {"binary": b""}):
            self.assertFalse(self.request("decision.capabilities.get", {"schema_version": 1}, **kwargs)["ok"])
        # Reading capabilities grants no turn/admission authority.
        self.assertFalse(self.submit()["ok"])

    def test_legacy_bridge_context_and_late_proposal_stay_rejected(self):
        self.execute()
        bridge = self.server.bridge
        self.assertEqual(bridge.build_context(), {"ok": False,
            "error": "legacy proposal context unavailable in decision mode", "affordances": []})
        self.assertEqual(bridge.handle_proposal({"context_id": "old-context", "commands": [
            {"action_id": "arm.move.v1", "params": {"meters": 1}}]}),
            {"ok": False, "error": "legacy proposals disabled in decision mode"})
        self.assertEqual(self.owners["arm"].commands, [])

    def test_capabilities_filters_unavailable_catalog_and_bounds_output(self):
        self.execute()
        entry = self.s.catalog.snapshot().entries[0]
        from astrbot_ex.core.actions.models import parse_action_manifest
        from astrbot_ex.core.decision.catalog import CapabilityInput
        self.s.catalog.refresh([CapabilityInput(entry["owner"], 2,
            parse_action_manifest(entry["manifest"]), {}, entry["guide"], False, "1")])
        result = self.request("decision.capabilities.get", {"schema_version": 1})
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["catalog_revision"], self.s.catalog.snapshot().revision)
        from astrbot_ex.core.decision import controller
        with patch.object(controller, "measure_json_budget", return_value="json_size_limit"):
            self.assertFalse(self.request("decision.capabilities.get", {"schema_version": 1})["ok"])


class DecisionReplanningTests(WiringFixture):
    def terminal(self):
        self.assertTrue(wait_for(lambda: self.c.journal.snapshot()["feedback"] is not None), self.diagnostics())
        return Feedback.parse(self.c.journal.snapshot()["feedback"]).to_dict()

    def assert_retired(self, fact, reason):
        self.assertEqual((fact["status"], fact["reason_code"]), ("failed", reason))
        self.assertEqual((fact["goal_id"], fact["goal_revision"]), ("goal-1", 1))
        proof = fact["details"]["terminal_evidence"]
        self.assertTrue(proof["verified"])
        self.assertEqual((proof["goal_id"], proof["goal_revision"]), ("goal-1", 1))
        self.assertGreaterEqual(proof["stop_proof_epoch"], proof["dispatcher_epoch"])
        self.assertTrue(fact["details"]["stop_evidence"]["stopped"])
        state = self.c.state()
        self.assertIsNone(state["active_goal_id"])
        self.assertIsNone(state["pending_goal_id"])
        self.assertEqual(state["execution"]["unresolved"], [])
        self.assertTrue(state["execution"]["stop_proven"])
        self.assertEqual(self.s.goals.phase, "idle")
        self.assertEqual(self.c._verified_completions, {})

    def test_low_confidence_start_retired_no_dispatch_durable_and_outbound(self):
        self.execute()
        backend = self.s.backend = ScoredBackend()
        self.bind()
        received = []
        def ack(connection, channel, method, fact, **kwargs):
            self.assertEqual((connection, channel, method), ("trusted", "text", "decision.feedback"))
            self.assertFalse(self.s._lock._is_owned())
            self.assertFalse(self.c._lock._is_owned())
            received.append(copy.deepcopy(fact))
            return {"ok": True, "ex_session": fact["ex_session"], "acked_event_seq": fact["event_seq"]}, None
        with patch.object(self.m, "request_connection", side_effect=ack), \
                patch.object(self.server.action_service, "start", wraps=self.server.action_service.start) as start:
            self.assertTrue(self.submit()["ok"])
            fact = self.terminal()
            self.assert_retired(fact, "low_confidence")
            start.assert_not_called()
            self.assertTrue(wait_for(lambda: fact in received))
        self.assertEqual(self.owners["arm"].commands, [])
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())
        self.assertEqual(fact["details"]["terminal_evidence"]["commands"], [])
        self.assertNotIn("failure_evidence", fact["details"])
        self.assertEqual(fact["details"]["decision_rejection"]["choices"], backend.original["choices"])
        self.assertFalse(fact["details"]["decision_rejection"]["truncated"])
        stored = FeedbackJournal(Path(self.tmp.name) / "execution" / "feedback.sqlite3", self.s.goals.ex_session)
        try:
            self.assertEqual(stored.snapshot()["feedback"], fact)
        finally:
            stored.close()
        # An old exact reply cannot acquire authorization after retirement.
        from astrbot_ex.core.decision.models import BackendDecision
        with self.assertRaises(RuntimeError):
            self.s._apply(backend.snapshot, BackendDecision.parse(backend.original))
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [fact])

    def test_model_wait_at_threshold_remains_active_and_never_replans(self):
        self.execute()
        self.s.backend = ScoredBackend(kind="wait", confidence=0.6)
        self.bind()
        self.assertTrue(self.submit()["ok"])
        self.assertTrue(wait_for(lambda: any(d["outcome"] == "no_dispatch" for d in self.s.status()["decisions"])))
        self.assertEqual(self.s.goals.phase, "active")
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.assertEqual(self.owners["arm"].commands, [])

    def test_request_replan_no_dispatch_uses_same_retirement_fact(self):
        self.execute()
        self.s.backend = ScoredBackend(kind="request_replan", confidence=1)
        self.bind()
        self.assertTrue(self.submit()["ok"])
        fact = self.terminal()
        self.assert_retired(fact, "backend_requested_replan")
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())
        self.assertEqual(self.owners["arm"].commands, [])

    def active(self, stop_proof=True):
        self.execute(stop_proof=stop_proof)
        self.bind()
        self.assertTrue(self.submit()["ok"])
        self.assertTrue(self.owners["arm"].started.wait(2))
        cid = self.owners["arm"].commands[0].command_id
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(cid).result(1).status == "accepted"))
        return cid

    def test_replan_running_command_requires_actual_actor_cancel_proof(self):
        cid = self.active()
        self.s.backend.kind = "request_replan"
        self.s.tick()
        fact = self.terminal()
        self.assert_retired(fact, "backend_requested_replan")
        commands = fact["details"]["terminal_evidence"]["commands"]
        self.assertEqual(commands[0]["command_id"], cid)
        self.assertEqual(commands[0]["stop_evidence"], {"stopped": True, "source": "arm", "reference": cid})
        self.assertEqual(self.server.action_ledger.get(cid).result(1).status, "canceled")
        self.assertEqual(self.owners["arm"].cancels, [cid])
        self.assertEqual(len(self.owners["arm"].commands), 1)

    def test_replan_without_physical_proof_withholds_feedback_even_after_late_proof(self):
        cid = self.active(stop_proof=False)
        self.server.action_service.stop_timeout = 0.1
        self.s.backend.kind = "request_replan"
        self.s.tick()
        self.assertTrue(wait_for(lambda: self.s.goals.phase == "blocked"))
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.assertIsNotNone(self.s.goals.active)
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(cid).result(1).status in {"unknown", "timed_out"}))
        self.server.action_dispatcher.reconcile_stop(cid, OwnerBinding("arm", 1),
            StopEvidence(cid, True, "arm", "late-stop")).result(1)
        self.s._poll_rows()
        self.s.tick()
        self.assertEqual(self.s.goals.phase, "blocked")
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])

    def test_low_confidence_framework_stop_not_proven_withholds_retirement(self):
        self.execute()
        self.s.backend = ScoredBackend()
        self.bind()
        with patch.object(self.server.action_service, "stop_actions", return_value=False):
            self.assertTrue(self.submit()["ok"])
            self.assertTrue(wait_for(lambda: self.s.goals.phase == "blocked"))
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.assertIsNotNone(self.s.goals.active or self.s.goals.pending_replace)
        self.assertEqual(self.owners["arm"].commands, [])

    def test_completed_owner_prefix_not_replayed_before_suffix_replan(self):
        self.execute(("arm", "camera"))
        self.bind()
        self.assertTrue(self.submit(owners=("arm", "camera"))["ok"])
        self.assertTrue(self.owners["arm"].started.wait(2))
        self.assertTrue(self.owners["camera"].started.wait(2))
        arm = self.succeed("arm")
        camera = self.owners["camera"].commands[0].command_id
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(camera).result(1).status == "accepted"))
        self.s._poll_rows()
        snapshot = self.s.build_snapshot()
        arm_options = next(o for o in snapshot.owners if o["owner"] == "arm")["candidates"]
        self.assertFalse(any(c["kind"] == "start" for c in arm_options))
        self.s.backend.kind = "request_replan"
        self.s.tick()
        fact = self.terminal()
        self.assert_retired(fact, "backend_requested_replan")
        rows = {r.command_id: r for r in self.server.action_ledger.list_commands().result(1)}
        self.assertEqual(rows[arm].status, "succeeded")
        self.assertEqual(rows[camera].status, "canceled")
        self.assertEqual(len(self.owners["arm"].commands), 1)
        self.assertEqual(len(self.owners["camera"].commands), 1)
        self.assertEqual(self.owners["arm"].cancels, [])
        self.assertEqual(self.owners["camera"].cancels, [camera])

    def test_unknown_command_never_becomes_replan_feedback_or_success(self):
        cid = self.active(stop_proof=False)
        self.server.action_dispatcher.report(cid, OwnerBinding("arm", 1), "unknown").result(1)
        self.s.backend.kind = "request_replan"
        self.s.tick()
        self.assertTrue(wait_for(lambda: self.s.goals.phase == "blocked"))
        self.assertIsNotNone(self.s.goals.active)
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.assertFalse(self.c.state()["execution"]["stop_proven"])
        self.assertEqual(len(self.owners["arm"].commands), 1)

    def test_low_confidence_shadow_only_records_original_choice(self):
        self.execute()
        self.s.set_mode("shadow")
        self.assertTrue(wait_for(lambda: self.s.goals.phase == "idle"))
        self.s.backend = ScoredBackend()
        self.bind()
        self.assertTrue(self.submit()["ok"])
        self.assertTrue(wait_for(lambda: any(d["outcome"] == "shadow" for d in self.s.status()["decisions"])))
        self.assertEqual(self.s.goals.phase, "active")
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.assertEqual(self.owners["arm"].commands, [])
        self.assertFalse(self.s.status()["gate_open"])

    def test_large_original_reply_is_preserved_locally_and_feedback_is_bounded(self):
        owners = tuple("owner" + str(index) for index in range(24))
        self.execute(owners)
        backend = self.s.backend = ScoredBackend()
        self.bind()
        self.assertTrue(self.submit(owners=owners)["ok"])
        fact = self.terminal()
        self.assert_retired(fact, "low_confidence")
        details = fact["details"]["decision_rejection"]
        self.assertTrue(details["truncated"])
        self.assertEqual(details["choice_count"], 24)
        import json
        self.assertLessEqual(len(json.dumps(details, ensure_ascii=False).encode("utf-8")), 3500)
        original = next(d["decision"] for d in self.s.status()["decisions"]
            if d["outcome"] == "decision_rejected")
        self.assertEqual(original, backend.original)
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())
        self.assertTrue(all(not owner.commands for owner in self.owners.values()))

    def test_terminal_dispatcher_epoch_conflict_after_draft_withholds_fact(self):
        self.execute()
        self.s.backend = ScoredBackend()
        self.bind()
        entered, release = threading.Event(), threading.Event()
        original = self.c.journal.prepare_completion
        def prepared(*args, **kwargs):
            token = original(*args, **kwargs)
            entered.set()
            release.wait(2)
            return token
        try:
            with patch.object(self.c.journal, "prepare_completion", side_effect=prepared):
                self.assertTrue(self.submit()["ok"])
                self.assertTrue(entered.wait(1))
                self.server.action_service.revoke_decision()
                self.assertTrue(self.server.action_service.await_stop_proof("new dispatcher epoch"))
                release.set()
                self.assertTrue(wait_for(lambda: not self.s.status()["backend_applying"]))
        finally:
            release.set()
        self.assertIsNotNone(self.s.goals.active)
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])

    def assert_rejection_fence(self, change):
        from dataclasses import replace
        from tests.test_goal_manager import goal_payload
        self.execute()
        self.s.backend = MockBackend(kind="wait")
        self.bind()
        self.assertTrue(self.submit()["ok"])
        self.assertTrue(wait_for(lambda: any(d["outcome"] == "no_dispatch"
            for d in self.s.status()["decisions"])))
        with self.s._condition:
            snapshot = self.s.build_snapshot()
            decision = ScoredBackend().decide(snapshot)
            self.s.backend = ScoredBackend()
            validate = self.s._validate_response
            calls, state = [], []
            def race(*args):
                calls.append(True)
                if len(calls) == 2:
                    self.assertTrue(self.s.goals._lock._is_owned())
                    self.assertTrue(self.s.actions.dispatcher._lock._is_owned())
                selected = validate(*args)
                if len(calls) == 1:
                    if change == "cancel":
                        self.s.goals.cancel({"schema_version": 1, "request_id": "race-cancel",
                            "ex_session": self.s.goals.ex_session, "goal_id": "goal-1",
                            "goal_revision": 1, "reason_code": "goal_canceled"})
                    elif change in {"pending_replace", "new_goal"}:
                        self.s.goals.submit(goal_payload(self.s.goals, 2, expected_revision=1))
                        if change == "new_goal":
                            self.assertTrue(self.s.goals.resolve_stop(self.s.goals.gate_epoch, True))
                    elif change == "expired":
                        self.s.goals.active = replace(self.s.goals.active, expires_ns=0)
                    else:
                        self.s.actions.revoke_decision()
                    state.append((self.s.goals.status(), self.s.goals._terminal_stop,
                        self.s.actions.dispatcher._epoch))
                return selected
            with patch.object(self.s, "_validate_response", side_effect=race):
                with self.assertRaises(RuntimeError):
                    self.s._apply(snapshot, decision)
            self.assertEqual(len(calls), 2)
            self.assertEqual((self.s.goals.status(), self.s.goals._terminal_stop,
                self.s.actions.dispatcher._epoch), state[0])
            self.assertIsNone(self.s._decision_rejection)
            self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
            self.assertEqual(self.owners["arm"].commands, [])
            self.s.backend = MockBackend(kind="wait")

    def test_rejection_fence_preserves_previous_cancel(self):
        self.assert_rejection_fence("cancel")

    def test_rejection_fence_preserves_pending_replacement(self):
        self.assert_rejection_fence("pending_replace")

    def test_rejection_fence_cannot_retire_new_active_goal(self):
        self.assert_rejection_fence("new_goal")

    def test_rejection_fence_rejects_expired_goal(self):
        self.assert_rejection_fence("expired")

    def test_rejection_fence_rejects_changed_dispatcher_epoch(self):
        self.assert_rejection_fence("dispatcher_epoch")

    def test_rejection_draft_superseded_goal_cannot_clear_replacement(self):
        self.execute()
        self.s.backend = ScoredBackend()
        self.bind()
        entered, release = threading.Event(), threading.Event()
        original = self.c.journal.prepare_completion
        def prepared(*args, **kwargs):
            token = original(*args, **kwargs)
            entered.set()
            release.wait(2)
            return token
        try:
            with patch.object(self.c.journal, "prepare_completion", side_effect=prepared):
                self.assertTrue(self.submit()["ok"])
                self.assertTrue(entered.wait(1))
                self.s.backend = MockBackend(kind="wait")
                self.bind(2, turn_id="replacement")
                self.assertTrue(self.submit(2)["ok"])
                release.set()
                self.assertTrue(wait_for(lambda: self.s.goals.active is not None
                    and self.s.goals.active.payload()["goal_id"] == "goal-2"))
        finally:
            release.set()
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.assertEqual(self.owners["arm"].commands, [])
