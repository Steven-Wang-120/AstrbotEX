"""B09 integration tests: the real request body and the real control socket.

Two scopes are covered here, and a delivery claim needs both:

* ``backend_request_capture`` — a real ``JevBackend`` builds and serialises the
  request, so we can read ``model`` and ``state.instruction_input`` off the wire
  bytes instead of trusting a mock's list.
* ``real_zmq_loopback`` — a real ROUTER/DEALER pair carries ``upper.steps.inject``
  from a fake upper controller into the ingress.

Nothing here proves execution.  An ``injected`` receipt only says the text
crossed the configured backend input boundary.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import Future
from pathlib import Path

from astrbot_ex.core.actions.dispatcher import ActionDispatcher
from astrbot_ex.core.actions.ledger import ActionLedger, OwnerBinding, StopEvidence
from astrbot_ex.core.actions.models import ActionStatus
from astrbot_ex.core.actions.service import ActionService
from astrbot_ex.core.connection_manager import ConnectionManager, zmq
from astrbot_ex.core.decision.backends import MockBackend, jev
from astrbot_ex.core.decision.instruction_input import (
    DecisionInstructionInput,
    InstructionInputError,
)
from astrbot_ex.core.decision.service import DecisionService
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.plugin_actor import PluginActor
from astrbot_ex.core.step_input import (
    METHOD_CAPABILITIES,
    METHOD_INJECT,
    SCHEMA,
    StepInputIngress,
    StepInputLog,
    parse_injection_batch,
)
from astrbot_ex.core.topic_bus import TopicBus
from tests.test_goal_manager import goal_payload, make_catalog

TEXTS = ["寻找红色杯子。", "接近并对齐同一个目标。", "确认抓取位姿。"]
SESSION = "current-ex-session"
ROBOT = "trusted-config-robot"
ROUTE = "framework-route"


def payload(**overrides):
    value = {
        "schema": SCHEMA,
        "request_id": "framework-request",
        "plan_id": "framework-plan",
        "plan_revision": 1,
        "batch_id": "framework-batch",
        "robot_id": ROBOT,
        "route_ref": ROUTE,
        "ex_session": SESSION,
        "catalog_version": "snapshot-hash",
        "prompt_version": "snapshot-hash",
        "visibility": "decision_only",
        "segments": [
            {"segment_id": f"framework-s{index}", "ordinal": index, "text": text}
            for index, text in enumerate(TEXTS)
        ],
    }
    value.update(overrides)
    return value


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def free_tcp_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def instruction_ack(model: str, choice: str = "accept", *, usage=None) -> bytes:
    body = {
        "model": model,
        "answers": {jev.INSTRUCTION_ACK_OWNER: {"type": "choice", "choice": choice}},
        "usage": usage or {"input_tokens": 11, "output_tokens": 2},
    }
    return json.dumps(body).encode("utf-8")


# --------------------------------------------------------------------------
# scope: backend_request_capture
# --------------------------------------------------------------------------


class BackendRequestCaptureTest(unittest.TestCase):
    """A real JevBackend serialises the batch; we read the captured bytes."""

    def setUp(self) -> None:
        self.captured: list[bytes] = []
        self.replies: list[bytes] = []

    def backend(self, *, model: str | None = None, choice: str = "accept"):
        # The production backend pins one model; "keeps the configured value"
        # therefore means exactly PINNED_MODEL and nothing else.
        model = model or jev.PINNED_MODEL

        def transport(body, headers, deadline, cancel, limit):
            self.captured.append(body)
            return jev.HTTPReply(200, self.replies.pop(0) if self.replies else instruction_ack(model, choice))
        config = jev.JevConfig(mode="shadow", min_interval_ms=0, model=model)
        backend = jev.JevBackend(config, transport=transport,
                                 secret_provider=lambda: "test-only-not-a-live-key")
        self.addCleanup(backend.close)
        return backend, config

    def test_three_segments_reach_the_model_verbatim(self) -> None:
        backend, config = self.backend()
        batch = parse_injection_batch(payload())
        result = backend.consume_instruction(batch)

        self.assertTrue(result["accepted"])
        self.assertEqual(result["model"], config.model)
        self.assertEqual(len(self.captured), 1)
        request = json.loads(self.captured[0])
        self.assertEqual(request["model"], config.model)
        self.assertEqual(set(request), {"model", "state", "questions"})
        # The batch crosses as one immutable, ordered block.
        self.assertEqual(request["state"]["instruction_input"], batch.to_wire())
        self.assertEqual([s["text"] for s in request["state"]["instruction_input"]["segments"]], TEXTS)
        self.assertEqual([s["ordinal"] for s in request["state"]["instruction_input"]["segments"]], [0, 1, 2])
        self.assertEqual(request["state"]["instruction_input"]["segments"][1]["text"], "接近并对齐同一个目标。")
        # No Goal and no DecisionSnapshot are invented for an input receipt.
        self.assertNotIn("snapshot", request["state"])
        self.assertNotIn("goal", request["state"])
        self.assertEqual(list(request["questions"]), [jev.INSTRUCTION_ACK_OWNER])
        self.assertEqual(set(request["questions"][jev.INSTRUCTION_ACK_OWNER]["criteria"]), {"accept", "reject"})
        self.assertEqual(backend.last_instruction_record.model, config.model)
        self.assertEqual(backend.last_instruction_record.attempts, 1)
        self.assertIsNone(backend.last_instruction_record.reject_code)

    def test_single_segment_is_passed_through_unchanged(self) -> None:
        backend, config = self.backend()
        batch = parse_injection_batch(payload(segments=[{"segment_id": "s0", "ordinal": 0, "text": "只发一段。"}]))
        backend.consume_instruction(batch)
        request = json.loads(self.captured[0])
        self.assertEqual(request["state"]["instruction_input"]["segments"],
                         [{"segment_id": "s0", "ordinal": 0, "text": "只发一段。"}])
        self.assertEqual(request["model"], config.model)

    def test_model_is_never_substituted(self) -> None:
        backend, config = self.backend()
        backend.consume_instruction(parse_injection_batch(payload()))
        self.assertEqual(json.loads(self.captured[0])["model"], jev.PINNED_MODEL)
        self.assertEqual(backend.last_instruction_record.model, jev.PINNED_MODEL)

    def test_declining_model_ack_is_not_a_delivery(self) -> None:
        backend, _ = self.backend(choice="reject")
        result = backend.consume_instruction(parse_injection_batch(payload()))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["detail"], "declined")

    def test_reply_model_mismatch_is_rejected(self) -> None:
        backend, _ = self.backend()
        self.replies.append(instruction_ack("some-other-model"))
        with self.assertRaises(jev.JevBackendError) as caught:
            backend.consume_instruction(parse_injection_batch(payload()))
        self.assertEqual(caught.exception.code, "model_mismatch")
        self.assertEqual(backend.last_instruction_record.reject_code, "model_mismatch")

    def test_malformed_batch_never_reaches_transport(self) -> None:
        backend, _ = self.backend()

        class Broken:
            batch_id = "b"
            payload_hash = "h"

            def to_wire(self):
                return {"schema": "wrong", "visibility": "decision_only"}

        with self.assertRaises(jev.JevBackendError) as caught:
            backend.consume_instruction(Broken())
        self.assertEqual(caught.exception.code, "invalid_batch")
        self.assertEqual(self.captured, [])

    def test_record_never_leaks_secret_or_text(self) -> None:
        backend, _ = self.backend()
        backend.consume_instruction(parse_injection_batch(payload()))
        rendered = repr(backend.last_instruction_record)
        self.assertNotIn("test-only", rendered)
        for text in TEXTS:
            self.assertNotIn(text, rendered)


# --------------------------------------------------------------------------
# service-level: no active Goal, no dispatcher, no fabricated Goal
# --------------------------------------------------------------------------


class Owner:
    def __init__(self, owner, dispatcher):
        self.id = owner
        self.dispatcher = dispatcher
        self.commands = []
        self.cancels = []

    def on_action_command(self, command):
        self.commands.append(command)
        return "accepted"

    def on_action_cancel(self, command_id, reason):
        self.cancels.append(command_id)
        self.dispatcher.report(command_id, OwnerBinding(self.id, 1), ActionStatus.CANCELED,
                               stop_evidence=StopEvidence(command_id, True, self.id, command_id))
        return "requested"


class ServiceInstructionInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = ActionLedger(Path(self.tmp.name) / "actions.sqlite")
        self.dispatcher = ActionDispatcher(self.ledger)
        self.catalog = make_catalog(("arm",))
        self.actors = {}
        for entry in self.catalog.snapshot().entries:
            actor = PluginActor(Owner(entry["owner"], self.dispatcher))
            actor.start()
            self.actors[entry["owner"]] = actor
            self.dispatcher.register_owner(OwnerBinding(entry["owner"], 1), actor, entry["manifest"])
        self.actions = ActionService(self.ledger, self.dispatcher, self.catalog, stop_timeout=0.12)
        self.actions.control_mode = "decision"
        self.actions.update_versions(runtime_state="running")

    def _service(self, backend=None, mode="execute"):
        backend = backend or MockBackend(kind="wait")
        service = DecisionService(self.actions, backend=backend, max_hz=50)
        self.addCleanup(service.close)
        service.set_mode(mode)
        wait_until(lambda: service.goals.phase == "idle")
        return service, backend

    def tearDown(self) -> None:
        self.dispatcher.close()
        for actor in self.actors.values():
            actor.stop(2)
        self.ledger.close()

    def test_idle_service_accepts_input_without_creating_a_goal(self) -> None:
        service, backend = self._service()
        self.assertIsNone(service.goals.active)
        self.assertEqual(service.instruction_ready(), (True, ""))

        outcome = service.submit_instruction(parse_injection_batch(payload()), timeout_sec=2.0)
        self.assertEqual(outcome["phase"], "injected")
        self.assertEqual(len(backend.instructions), 1)
        self.assertEqual([s.text for s in backend.instructions[0].segments], TEXTS)
        # The batch must not become authority: no Goal, no command, gate closed.
        self.assertIsNone(service.goals.active)
        self.assertEqual(backend.calls, 0)
        self.assertFalse(self.dispatcher._gate)
        self.assertEqual(self.actors["arm"].plugin.commands, [])
        self.assertEqual(service._instruction_rounds, 1)

    def test_active_goal_is_never_preempted(self) -> None:
        service, backend = self._service()
        submitted = service.submit_goal(goal_payload(service.goals, 1, ("arm",)))
        self.assertTrue(submitted.get("ok"), submitted)
        wait_until(lambda: service.goals.active is not None)
        with self.assertRaises(InstructionInputError) as caught:
            service.submit_instruction(parse_injection_batch(payload()), timeout_sec=1.0)
        self.assertEqual(caught.exception.code, "input_busy")
        self.assertEqual(list(backend.instructions), [])

    def test_backend_without_capability_reports_not_ready(self) -> None:
        class NoInstructionBackend(MockBackend):
            consume_instruction = None

        service, _ = self._service(backend=NoInstructionBackend(kind="wait"))
        self.assertEqual(service.instruction_ready(), (False, "backend_unsupported"))
        adapter = DecisionInstructionInput(service)
        self.assertEqual(adapter.supports(), (False, "backend_unsupported"))
        with self.assertRaises(InstructionInputError) as caught:
            adapter.accept(parse_injection_batch(payload()))
        self.assertEqual(caught.exception.code, "backend_unsupported")

    def test_unknown_backend_failure_is_not_reported_as_injected(self) -> None:
        service, backend = self._service(backend=MockBackend(kind="wait", fail=True))
        outcome = service.submit_instruction(parse_injection_batch(payload()), timeout_sec=2.0)
        self.assertEqual(outcome["phase"], "unknown")
        self.assertEqual(len(backend.instructions), 1)

    def test_concurrent_submit_is_refused_while_one_round_is_pending(self) -> None:
        service, backend = self._service(backend=MockBackend(kind="wait", block_instruction=True))
        first = service.submit_instruction(parse_injection_batch(payload()), timeout_sec=0.05)
        self.assertEqual(first["phase"], "received")
        self.assertTrue(backend.instruction_entered.wait(2))
        with self.assertRaises(InstructionInputError) as caught:
            service.submit_instruction(
                parse_injection_batch(payload(batch_id="framework-batch-2")), timeout_sec=0.5)
        self.assertEqual(caught.exception.code, "input_busy")
        self.assertEqual(len(backend.instructions), 1)

    def test_close_resolves_a_pending_round_as_unknown(self) -> None:
        service, backend = self._service(backend=MockBackend(kind="wait", block_instruction=True))
        seen = {}

        def submit():
            seen["outcome"] = service.submit_instruction(
                parse_injection_batch(payload()), timeout_sec=0.05)

        thread = threading.Thread(target=submit, daemon=True)
        thread.start()
        self.assertTrue(backend.instruction_entered.wait(2))
        thread.join(2)
        self.assertEqual(seen["outcome"]["phase"], "received")
        # close() settles the queued round as unknown rather than dropping it.
        service.close()
        self.assertEqual(service._last_instruction["phase"], "unknown")


# --------------------------------------------------------------------------
# scope: real_zmq_loopback
# --------------------------------------------------------------------------


@unittest.skipIf(zmq is None, "pyzmq is not installed")
class ControlLoopbackTest(unittest.TestCase):
    """A fake upper controller drives a real control DEALER."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.ledger = ActionLedger(root / "actions.sqlite")
        self.dispatcher = ActionDispatcher(self.ledger)
        self.catalog = make_catalog(("arm",))
        self.actors = {}
        for entry in self.catalog.snapshot().entries:
            actor = PluginActor(Owner(entry["owner"], self.dispatcher))
            actor.start()
            self.actors[entry["owner"]] = actor
            self.dispatcher.register_owner(OwnerBinding(entry["owner"], 1), actor, entry["manifest"])
        self.actions = ActionService(self.ledger, self.dispatcher, self.catalog, stop_timeout=0.12)
        self.actions.control_mode = "decision"
        self.actions.update_versions(runtime_state="running")
        self.service = DecisionService(self.actions, backend=MockBackend(kind="wait"), max_hz=50)
        self.service.set_mode("execute")
        wait_until(lambda: self.service.goals.phase == "idle")

        self.port = free_tcp_port()
        self.context = zmq.Context.instance()
        self.router = self.context.socket(zmq.ROUTER)
        self.router.setsockopt(zmq.LINGER, 0)
        self.router.bind(f"tcp://127.0.0.1:{self.port}")

        self.business_calls: list[tuple] = []
        self.manager = ConnectionManager(root / "connections.json",
                                         event_bus=EventBus(), topic_bus=TopicBus())
        self.manager.set_business_handler(
            lambda *args: (self.business_calls.append(args), {"ok": False, "error": "must not be used"})[1])
        self.manager.create({
            "id": "control", "name": "Upper control", "type": "zmq_client", "enabled": False,
            "config": {
                "endpoint": f"tcp://127.0.0.1:{self.port}", "identity": "test-control",
                "protocol_profile": "astrbotex", "channel": "control",
                "upper_robot_id": ROBOT, "upper_route_refs": ROUTE,
                # Deliberately stale: the runtime provider must win.
                "upper_ex_session": "stale-recorded-session",
            },
        })
        self.ingress = StepInputIngress(
            log=StepInputLog(root / "execution" / "upper_step_input.jsonl"),
            adapter=DecisionInstructionInput(self.service),
            trusted_context=self.manager.control_trusted_context,
            request_budget_sec=2.0,
            worker_budget_sec=5.0,
        )
        self.ingress.start()
        self.manager.set_control_session_provider(lambda: SESSION)
        self.manager.set_step_input_handler(self.ingress.handle)

    def tearDown(self) -> None:
        self.manager.close()
        self.ingress.close()
        self.router.setsockopt(zmq.LINGER, 0)
        self.router.close()
        self.service.close()
        self.dispatcher.close()
        for actor in self.actors.values():
            actor.stop(2)
        self.ledger.close()

    # -- raw ROUTER helpers ------------------------------------------------

    def _start(self) -> bytes:
        self.manager.start("control")
        wait_until(lambda: self.manager.get("control")["runtime"]["state"] == "running")
        # The DEALER announces itself with system.hello; that frame carries the
        # peer identity we must answer to.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not self.router.poll(50):
                continue
            frames = self.router.recv_multipart()
            envelope = json.loads(frames[1].decode("utf-8"))
            if envelope.get("method") == "system.hello":
                return frames[0]
        raise AssertionError("control DEALER never sent hello")

    def _call(self, peer: bytes, method: str, body, *, binary: bytes | None = None,
              channel: str = "control", timeout: float = 5.0):
        request_id = uuid.uuid4().hex
        envelope = {
            "protocol": "astrbotex-zmq", "version": 1, "channel": channel,
            "kind": "request", "id": request_id, "method": method,
            "timestamp": time.time(), "payload": body,
        }
        frames = [peer, json.dumps(envelope).encode("utf-8")]
        if binary is not None:
            frames.append(binary)
        self.router.send_multipart(frames)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.router.poll(50):
                continue
            reply = self.router.recv_multipart()
            answer = json.loads(reply[1].decode("utf-8"))
            if answer.get("kind") == "response" and answer.get("reply_to") == request_id:
                return answer.get("payload"), reply[2:]
        raise AssertionError(f"no response for {method}")

    # -- tests -------------------------------------------------------------

    def test_capabilities_over_the_real_socket(self) -> None:
        peer = self._start()
        body, _ = self._call(peer, METHOD_CAPABILITIES, {})
        self.assertTrue(body["ok"])
        self.assertEqual(body["schema"], SCHEMA)
        self.assertTrue(body["input_ready"])
        self.assertEqual(body["ex_session"], SESSION)
        self.assertEqual(body["methods"], [METHOD_CAPABILITIES, METHOD_INJECT])

    def test_three_segments_arrive_verbatim_in_order(self) -> None:
        peer = self._start()
        body, binary = self._call(peer, METHOD_INJECT, payload())
        self.assertEqual(binary, [])
        self.assertTrue(body["ok"])
        self.assertEqual(body["receipt"]["phase"], "injected")
        self.assertEqual(body["receipt"]["batch_id"], "framework-batch")
        self.assertEqual(body["receipt"]["segment_count"], 3)
        delivered = self.service.backend.instructions[0]
        self.assertEqual([s.text for s in delivered.segments], TEXTS)
        self.assertEqual([s.ordinal for s in delivered.segments], [0, 1, 2])
        self.assertEqual(body["receipt"]["payload_hash"], delivered.payload_hash)
        # No interaction pipeline, no execution.
        self.assertEqual(self.business_calls, [])
        self.assertIsNone(self.service.goals.active)

    def test_duplicate_is_not_consumed_twice(self) -> None:
        peer = self._start()
        first, _ = self._call(peer, METHOD_INJECT, payload())
        second, _ = self._call(peer, METHOD_INJECT, payload())
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["receipt"]["payload_hash"], second["receipt"]["payload_hash"])
        self.assertEqual(len(self.service.backend.instructions), 1)

    def test_same_id_different_content_conflicts(self) -> None:
        peer = self._start()
        self._call(peer, METHOD_INJECT, payload())
        changed = payload(segments=[{"segment_id": "s0", "ordinal": 0, "text": "换了内容。"}])
        body, _ = self._call(peer, METHOD_INJECT, changed)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "id_conflict")
        self.assertEqual(len(self.service.backend.instructions), 1)

    def test_unknown_field_binary_and_identity_mismatches_are_rejected(self) -> None:
        peer = self._start()
        cases = [
            ("unknown field", payload(public_text="hi"), None, "invalid_batch"),
            ("binary frame", payload(batch_id="b-bin"), b"\x00\x01", "invalid_batch"),
            ("wrong robot", payload(batch_id="b-robot", robot_id="intruder"), None, "session_mismatch"),
            ("stale session", payload(batch_id="b-session", ex_session="stale-recorded-session"), None, "session_mismatch"),
            ("wrong route", payload(batch_id="b-route", route_ref="other-route"), None, "route_mismatch"),
        ]
        for label, body, binary, expected in cases:
            with self.subTest(label=label):
                reply, _ = self._call(peer, METHOD_INJECT, body, binary=binary)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"]["code"], expected)
        self.assertEqual(list(self.service.backend.instructions), [])

    def test_unknown_method_on_control_is_rejected_without_fallback(self) -> None:
        peer = self._start()
        body, _ = self._call(peer, "upper.steps.cancel", {})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "unsupported_method")
        body, _ = self._call(peer, "interaction.message", {"text": "hi"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "unsupported_method")
        self.assertEqual(self.business_calls, [])

    def test_upper_methods_are_refused_off_the_control_channel(self) -> None:
        result, _ = self.manager._handle_business_request("text", METHOD_INJECT, payload(),
                                                          None, connection_id="control")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "route_mismatch")
        self.assertEqual(self.business_calls, [])
        self.assertEqual(list(self.service.backend.instructions), [])

    def test_restart_does_not_reinject_a_consumed_batch(self) -> None:
        peer = self._start()
        self._call(peer, METHOD_INJECT, payload())
        log = StepInputLog(Path(self.tmp.name) / "execution" / "upper_step_input.jsonl")
        self.assertEqual(log.get("framework-batch").phase, "injected")
        # A fresh process sees the terminal phase and never replays it.
        self.assertEqual(log.get("framework-batch").reason, "accepted")
        self.assertEqual(len(self.service.backend.instructions), 1)


if __name__ == "__main__":
    unittest.main()
