from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from astrbot_ex.core.actions.ledger import OwnerBinding
from astrbot_ex.core.decision.backends.structured import StructuredBackend
from astrbot_ex.core.step_input import StepInputError, parse_injection_batch
from tests import test_decision_service as fixtures
from tests.test_upper_control_e2e import upper_batch


class PrimitiveDispatchTests(unittest.TestCase):
    setUp = fixtures.DecisionServiceTests.setUp
    tearDown = fixtures.DecisionServiceTests.tearDown
    create = fixtures.DecisionServiceTests.create

    def make(self, backend=None):
        service = self.create(backend=backend or StructuredBackend())
        self.provider = Mock(catalog={"actions": ["arm.move.v1"]}, result_scope="joint_control",
                             failure_prefix="command_", status_fields={})
        self.provider.execution_params.side_effect = lambda b: b["params"]
        def bind(action):
            if set(action) != {"joint", "action_id", "params"} or action["action_id"] != "arm.move.v1":
                raise ValueError("invalid_action")
            return {"state": "ready", **action}
        self.provider.bind_structured.side_effect = bind
        service.set_primitive_owner(SimpleNamespace(create_primitive_provider=lambda *_: self.provider), None)
        self.batch = upper_batch(["Move arm"])
        self.batch["ex_session"] = service.goals.ex_session
        self.action = {"joint": "arm", "action_id": "arm.move.v1", "params": {"meters": 3}}
        return service

    def deliver(self, structured):
        if structured:
            self.batch["segments"][0]["structured_action"] = self.action
        return self.service.submit_instruction(parse_injection_batch(self.batch))

    def assert_completed(self):
        self.assertTrue(fixtures.wait_for(lambda: len(self.plugins["arm"].commands) == 1), self.service.status())
        cmd = self.plugins["arm"].commands[0].to_dict()
        self.assertEqual(cmd["params"], self.action["params"])
        cid = cmd["command_id"]
        self.assertTrue(fixtures.wait_for(lambda: self.ledger.get(cid).result(1).status == "accepted"))
        self.dispatcher.report(cid, OwnerBinding("arm", 1), "succeeded").result(1)
        self.assertTrue(fixtures.wait_for(lambda: self.service.primitives.phase == "completed"), self.service.status())
        step = self.service.primitives.status()["steps"][0]
        self.assertEqual(step["commands"][0]["status"], "succeeded")
        self.assertLessEqual(step["binding_completed_ns"], step["action_submit_ns"])
        self.assertLessEqual(step["action_submit_ns"], step["completed_ns"])
        self.assertEqual(len(self.plugins["arm"].commands), 1)

    def test_structured_input_executed_once_without_model_or_camera(self):
        backend = StructuredBackend()
        backend.decide = Mock(side_effect=AssertionError("unexpected model poll"))
        self.make(backend)
        self.assertEqual(self.deliver(True)["phase"], "injected")
        self.assert_completed()
        self.provider.context.assert_not_called()
        self.provider.structured_context.assert_not_called()
        backend.decide.assert_not_called()

    def test_stop_can_cancel_while_provider_resolves_a_fresh_target(self):
        self.make()
        entered, resume = threading.Event(), threading.Event()
        def execution(binding):
            entered.set()
            if not resume.wait(3):
                raise TimeoutError("test stop blocked by provider lock")
            return binding["params"]
        self.provider.execution_params.side_effect = execution
        self.deliver(True)
        self.assertTrue(entered.wait(2))
        self.service.request_stop("operator")
        resume.set()
        self.assertTrue(fixtures.wait_for(lambda: self.service.primitives.phase == "canceled"))
        self.assertEqual(self.plugins["arm"].commands, [])

    def test_model_bound_once_and_never_polled_for_owner_choice(self):
        backend = Mock(require_image=True, execution_allowed=True, primitive_only=True, structured_only=False,
                       min_confidence=None)
        backend.supports_instruction_input.return_value = True
        self.make(backend)
        backend.bind_primitive.return_value = {"state": "ready", **self.action}
        self.provider.structured_context.return_value = ([], {"available": True}, b"frame")
        self.assertEqual(self.deliver(False)["phase"], "injected")
        self.assert_completed()
        backend.bind_primitive.assert_called_once()
        self.provider.structured_context.assert_called_once_with(require_image=True)
        backend.decide.assert_not_called()
        backend.consume_instruction.assert_not_called()

    def test_invalid_structured_action_does_not_reach_owner(self):
        self.make()
        self.action["unexpected"] = 42
        self.assertEqual(self.deliver(True)["phase"], "rejected")
        self.assertEqual(self.service.primitives.status()["phase"], "rejected")
        self.assertEqual(self.plugins["arm"].commands, [])

    def test_structured_action_participates_in_receipt_digest_and_is_frozen(self):
        raw = upper_batch(["Move arm"])
        plain = parse_injection_batch(raw)
        raw["segments"][0]["structured_action"] = {"joint": "arm", "action_id": "arm.move.v1", "params": {"meters": 3}}
        with_action = parse_injection_batch(raw)
        original_hash = with_action.payload_hash
        raw["segments"][0]["structured_action"]["params"]["meters"] = 9
        self.assertEqual(with_action.payload_hash, original_hash)
        self.assertNotEqual(plain.payload_hash, original_hash)
        self.assertNotEqual(parse_injection_batch(raw).payload_hash, original_hash)
        raw["segments"][0]["structured_action"] = None
        with self.assertRaises(StepInputError):
            parse_injection_batch(raw)
