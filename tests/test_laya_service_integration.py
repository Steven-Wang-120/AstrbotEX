"""Formal EX/Actor/Ledger tests with synthetic HTTP replies, not model claims."""
from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from astrbot_ex.core.actions.ledger import OwnerBinding
from astrbot_ex.core.decision.backends import MockBackend
from astrbot_ex.core.decision.backends import laya, jev
from tests import test_decision_service as service_fixture
from tests.test_decision_service import wait_for
from tests.test_laya_backend import FixtureTransport, health, reply, response


class LayaServiceIntegrationTests(unittest.TestCase):
    # Reuse setup helpers without inheriting/re-running the old test class.
    setUp = service_fixture.DecisionServiceTests.setUp
    tearDown = service_fixture.DecisionServiceTests.tearDown
    create = service_fixture.DecisionServiceTests.create
    submit = service_fixture.DecisionServiceTests.submit

    def install(self, transport=None, *, execute=True, **config):
        from dataclasses import replace
        service = self.create(backend=MockBackend(kind="wait"), decision_mode="disabled")
        self.assertTrue(wait_for(lambda: service.goals.phase == "idle" and
                                 not service.status()["backend_live"] and
                                 not service.status()["backend_applying"]))
        config_value = replace(laya.LayaConfig(enabled=True), **config)
        backend = laya.LayaBackend(config_value, transport=transport or self.start_transport(),
                                   allow_test_execution=execute)
        previous = service.backend
        result = service.replace_backend("laya", lambda: backend)
        self.assertTrue(result["applied"])
        self.assertIs(service.backend, backend)
        self.assertTrue(previous.closed.is_set())
        self.assertTrue(wait_for(lambda: service.goals.phase == "idle" and
                                 service.status()["stop"]["state"] == "proven"))
        return service, backend

    def start_transport(self, *, post=None):
        test = self
        class StartTransport(FixtureTransport):
            def __call__(self, method, path, body, deadline, cancel, max_bytes):
                if method == "GET":
                    return super().__call__(method, path, body, deadline, cancel, max_bytes)
                self.calls.append((method, path))
                request = json.loads(body)
                self.requests.append(request)
                if post is not None:
                    return post(method, path, body, deadline, cancel, max_bytes)
                question = request["questions"]["q0"]
                # The fixture has three real candidates: wait/replan/start.
                # A controlled protocol reply picks the distinct start, not first.
                criteria = question["criteria"]
                if len(criteria) == 3:
                    test.assertIn("Start", criteria["C"])
                    test.assertIn("Move safely", request["state"])
                    choice = "C"
                else:
                    choice = "A"  # Legal wait after an action has already started.
                return reply(response(request["questions"], choices={"q0": choice}))
        return StartTransport()

    def authorize(self, service, *, mode="execute", n=1, **extra):
        service.set_mode(mode)
        self.assertTrue(wait_for(lambda: service.goals.phase == "idle" and
                                 service.status()["stop"]["state"] == "proven"))
        self.submit(n, **extra)

    def test_formal_call_choice_reaches_actor_and_committed_success_ledger(self):
        transport = self.start_transport()
        service, backend = self.install(transport)
        self.authorize(service, completion={"required_success_actions": ["arm.move.v1"]})
        self.assertTrue(self.plugins["arm"].started.wait(1))
        command = self.plugins["arm"].commands[0]
        self.assertEqual(command.action_id, "arm.move.v1")
        self.assertEqual(command.params, {"meters": 1})
        self.assertEqual(command.goal_id, "goal-1")
        self.assertEqual(command.goal_revision, 1)
        self.assertEqual(command.owner, "arm")
        self.assertTrue(wait_for(lambda: self.ledger.get(command.command_id).result(1).status == "accepted"))
        accepted = self.ledger.get(command.command_id).result(1)
        self.assertEqual(accepted.command_id, command.command_id)
        self.assertEqual(json.loads(accepted.canonical_command)["command"]["decision_id"], command.decision_id)
        self.dispatcher.report(command.command_id, OwnerBinding("arm", 1), "running").result(1)
        self.dispatcher.report(command.command_id, OwnerBinding("arm", 1), "succeeded").result(1)
        done = self.ledger.get(command.command_id).result(1)
        self.assertEqual(done.status, "succeeded")
        self.assertGreater(done.event_seq, accepted.event_seq)
        self.assertEqual(len(self.plugins["arm"].commands), 1)
        self.assertTrue(wait_for(lambda: service.goals.phase == "awaiting_llm"))
        self.assertEqual(service.status()["backend"]["name"], "laya")
        self.assertTrue(transport.requests)
        self.assertEqual(transport.requests[0]["model"], "typed-decisions")
        self.assertIsNotNone(backend.last_record)

    def test_shadow_records_model_choice_without_actor_or_ledger_side_effect(self):
        transport = self.start_transport()
        service, _ = self.install(transport, execute=False)
        self.authorize(service, mode="shadow")
        self.assertTrue(wait_for(lambda: any(d["outcome"] == "shadow" for d in service.status()["decisions"])))
        decision = next(d for d in service.status()["decisions"] if d["outcome"] == "shadow")
        self.assertTrue(any(c["kind"] == "start" for c in decision["choices"]))
        self.assertEqual(self.plugins["arm"].commands, [])
        self.assertEqual(self.ledger.list_commands().result(1), ())
        self.assertTrue(transport.requests)

    def test_stop_receipt_matches_proven_operation_and_real_cancellation_proof(self):
        service, _ = self.install()
        self.authorize(service)
        self.assertTrue(self.plugins["arm"].started.wait(1))
        command = self.plugins["arm"].commands[0]
        self.assertTrue(wait_for(lambda: self.ledger.get(command.command_id).result(1).status == "accepted"))
        receipt = service.request_stop("laya_fixture_stop")
        def proven():
            operation = service.status()["stop"]
            return operation["operation_id"] == receipt["operation_id"] and operation["state"] == "proven"
        self.assertTrue(wait_for(proven))
        proof = self.ledger.stop_proof(command.command_id, OwnerBinding("arm", 1)).result(1)
        self.assertIsNotNone(proof)
        self.assertTrue(proof.stopped)
        self.assertEqual(self.ledger.get(command.command_id).result(1).status, "canceled")
        self.assertEqual(self.plugins["arm"].cancels, [command.command_id])
        self.assertFalse(service.status()["gate_open"])

    def test_goal_and_configuration_change_drop_real_adapter_late_choice(self):
        for change in ("goal", "configuration"):
            with self.subTest(change=change):
                # Each iteration owns a fresh EX fixture and cleans it fully.
                if self.service is not None:
                    self.tearDown()
                    self.setUp()
                entered, release = threading.Event(), threading.Event()
                post_count = []
                def late(*args):
                    post_count.append(True)
                    if len(post_count) == 1:
                        entered.set()
                        release.wait(2)
                    request = json.loads(args[2])
                    return reply(response(request["questions"], choices={"q0": "C" if len(post_count) == 1 else "A"}))
                service, backend = self.install(self.start_transport(post=late))
                self.authorize(service)
                try:
                    self.assertTrue(entered.wait(1))
                    old_id = service.snapshot()["snapshot_id"]
                    if change == "goal":
                        self.submit(2)
                    else:
                        service.configuration_changed()
                    release.set()
                    self.assertTrue(wait_for(lambda: any(d["snapshot_id"] == old_id and d["outcome"] == "discarded"
                                                         for d in service.status()["decisions"])))
                    rejected = next(d for d in service.status()["decisions"]
                                    if d["snapshot_id"] == old_id and d["outcome"] == "discarded")
                    self.assertEqual(rejected["reason_code"],
                                     "goal_revision_changed" if change == "goal" else "config_revision_changed")
                    self.assertEqual(self.plugins["arm"].commands, [])
                    self.assertEqual(self.ledger.list_commands().result(1), ())
                    receipt = service.request_stop("after_stale_result_observed")
                    self.assertTrue(wait_for(lambda: service.status()["stop"]["operation_id"] == receipt["operation_id"]
                                             and service.status()["stop"]["state"] == "proven"))
                    self.assertIsNotNone(backend.last_record)
                finally:
                    release.set()

    def test_sent_adapter_timeout_is_unavailable_and_dispatches_zero_actions(self):
        entered, release = threading.Event(), threading.Event()
        def hung(*args):
            entered.set()
            release.wait(2)
            return reply(response(json.loads(args[2])["questions"], choices={"q0": "C"}))
        transport = self.start_transport(post=hung)
        service, backend = self.install(transport, deadline_ms=40)
        self.authorize(service)
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(wait_for(lambda: backend.status()["restart_required"]))
            self.assertTrue(wait_for(lambda: any(d["outcome"] == "discarded" for d in service.status()["decisions"])))
            self.assertEqual(self.plugins["arm"].commands, [])
            self.assertEqual(self.ledger.list_commands().result(1), ())
            self.assertEqual(len(transport.requests), 1)
            receipt = service.request_stop("timeout_stop")
            self.assertTrue(wait_for(lambda: service.status()["stop"]["operation_id"] == receipt["operation_id"]
                                     and service.status()["stop"]["state"] == "proven"))
            release.set()
            self.assertTrue(backend.status()["restart_required"])
            self.assertEqual(self.plugins["arm"].commands, [])
        finally:
            release.set()

    def test_service_unavailable_health_cannot_dispatch_or_submit_inference(self):
        transport = FixtureTransport(health_value=health(loaded=[]))
        service, _ = self.install(transport)
        self.authorize(service)
        self.assertTrue(wait_for(lambda: any(d["outcome"] == "discarded" for d in service.status()["decisions"])))
        self.assertEqual(transport.requests, [])
        self.assertEqual(self.plugins["arm"].commands, [])
        self.assertEqual(self.ledger.list_commands().result(1), ())

    def test_default_laya_execute_is_rejected_and_jev_capability_unchanged(self):
        service, backend = self.install(execute=False)
        self.assertFalse(backend.execution_allowed)
        with self.assertRaisesRegex(RuntimeError, "backend_execute_not_allowed"):
            service.set_mode("execute")
        self.assertEqual(service.status()["mode"], "disabled")
        self.assertEqual(self.plugins["arm"].commands, [])
        cloud = jev.JevBackend()
        try:
            self.assertFalse(cloud.execution_allowed)
        finally:
            cloud.close()


if __name__ == "__main__":
    unittest.main()
