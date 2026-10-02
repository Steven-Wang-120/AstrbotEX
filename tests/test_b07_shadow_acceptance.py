"""Additional Jev-only shadow acceptance; no B04 race-test modifications."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from astrbot_ex.core.actions.ledger import OwnerBinding, StopEvidence
from astrbot_ex.core.actions.models import ActionCommand
from astrbot_ex.core.decision.backends.jev import HTTPReply
from tests import test_decision_runtime_integration as composition
from tests.test_decision_service import wait_for


class B07ShadowAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = composition.JevServiceCompositionTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_wait_and_replan_choices_cannot_transition_goal_or_invoke_action(self):
        fixture = self.fixture
        fixture.kind = "wait"
        service = fixture.create()
        fixture.fixture.submit()
        self.assertTrue(wait_for(lambda: any(d["outcome"] == "shadow" for d in service.status()["decisions"])))
        expiry = service.goals.active.expires_ns
        for kind in ("wait", "request_replan"):
            with self.subTest(kind=kind), \
                    patch.object(fixture.fixture.actions, "start", wraps=fixture.fixture.actions.start) as start, \
                    patch.object(fixture.fixture.actions, "cancel", wraps=fixture.fixture.actions.cancel) as cancel, \
                    patch.object(service.goals, "awaiting_llm", wraps=service.goals.awaiting_llm) as replan, \
                    patch.object(service.goals, "renew", wraps=service.goals.renew) as renew, \
                    patch.object(fixture.fixture.actors["arm"], "set_lifecycle_ready") as enable:
                fixture.kind = kind
                service.tick()
                self.assertTrue(wait_for(lambda: any(d["outcome"] == "shadow" and d["choices"][0]["kind"] == kind
                                                      for d in service.status()["decisions"])))
                start.assert_not_called()
                cancel.assert_not_called()
                replan.assert_not_called()
                renew.assert_not_called()
                enable.assert_not_called()
                self.assertEqual(service.goals.phase, "active")
                self.assertEqual(service.goals.active.expires_ns, expiry)
                self.assertEqual(fixture.fixture.ledger.list_commands().result(1), ())
        fixture.assert_no_effects(service)

    def test_existing_keep_choice_never_restarts_or_extends_goal_lease(self):
        fixture = self.fixture
        fixture.kind = "wait"
        service = fixture.create()
        fixture.fixture.submit()
        self.assertTrue(wait_for(lambda: service.goals.phase == "active"))
        command = ActionCommand.parse({"schema_version": 1, "command_id": "b07-shadow-keep", "ex_session": service.goals.ex_session,
            "goal_id": "goal-1", "goal_revision": 1, "decision_id": "fixture-only", "owner": "arm", "plugin_generation": 1,
            "action_id": "arm.move.v1", "operation": "start", "params": {"meters": 1}, "lease_ms": 1000})
        binding = OwnerBinding("arm", 1)
        fixture.fixture.ledger.admit(command, (), binding, task_id="task").result(1)
        fixture.fixture.ledger.report(command.command_id, binding, "accepted").result(1)
        before = fixture.fixture.ledger.get(command.command_id).result(1)
        expiry = service.goals.active.expires_ns
        try:
            fixture.kind = "keep"
            with patch.object(fixture.fixture.actions, "start", wraps=fixture.fixture.actions.start) as start, \
                    patch.object(fixture.fixture.actions, "cancel", wraps=fixture.fixture.actions.cancel) as cancel, \
                    patch.object(service.goals, "renew", wraps=service.goals.renew) as renew:
                service.tick()
                self.assertTrue(wait_for(lambda: any(d["outcome"] == "shadow" and d["choices"][0]["kind"] == "keep"
                                                      for d in service.status()["decisions"])))
                start.assert_not_called()
                cancel.assert_not_called()
                renew.assert_not_called()
            self.assertEqual(fixture.fixture.ledger.get(command.command_id).result(1), before)
            self.assertEqual(service.goals.active.expires_ns, expiry)
            fixture.assert_no_effects(service)
        finally:
            fixture.fixture.ledger.report(command.command_id, binding, "unknown").result(1)
            fixture.fixture.ledger.reconcile_stop(command.command_id, binding,
                StopEvidence(command.command_id, True, "fixture", command.command_id)).result(1)

    def test_malformed_and_injected_responses_have_zero_ledger_or_actor_effects(self):
        fixture = self.fixture
        service = fixture.create()
        with patch.object(fixture.backend, "_transport", return_value=HTTPReply(200,
                json.dumps({"model": "jev-1.13.0", "answers": {"arm": {"choice": "invented.handler", "params": {}}},
                            "usage": {"input_tokens": 1, "output_tokens": 1}}).encode())):
            fixture.fixture.submit()
            self.assertTrue(wait_for(lambda: any(d["outcome"] == "discarded" and d["reason_code"] == "backend_error:JevBackendError"
                                                  for d in service.status()["decisions"])))
        self.assertEqual(fixture.backend.last_record.reject_code, "answer_shape")
        self.assertEqual(fixture.fixture.ledger.list_commands().result(1), ())
        fixture.assert_no_effects(service)


if __name__ == "__main__":
    unittest.main()
