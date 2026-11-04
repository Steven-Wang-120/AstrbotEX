"""B11 EX-side cross-end acceptance for upper-step input.

This is the receiving half of the chain that AEB's ``control`` channel drives.
It is deliberately *not* a unit test of B09: B09 already has its own suite. What
is proved here is the seam between the two ends —

* a real DEALER (the connection manager's control client) talking to a ROUTER
  owned by the test, so the payload is parsed from the same wire format AEB's
  ``ControlChannel`` emits;
* the real ``StepInputIngress`` → ``DecisionInstructionInput`` →
  ``DecisionService`` → **real** ``JevBackend`` stack, with only the transport
  replaced by a capture;
* the captured HTTP body, which is the only honest evidence that the segments
  reached the model input boundary;
* zero calls into ``InteractionCore``, the public task handler, TTS or the
  dispatcher, and no invented ``Goal`` or ``DecisionSnapshot``.

A receipt of ``injected`` means "the text crossed the backend input boundary".
It never means the steps ran.

Everything here is gated on B09. When the module is absent the classes skip with
``upper_dependency_missing: ...`` and the B11 runner reports NOT_RUN, never PASS.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from astrbot_ex.core.actions.dispatcher import ActionDispatcher
from astrbot_ex.core.actions.ledger import ActionLedger, OwnerBinding, StopEvidence
from astrbot_ex.core.actions.models import ActionStatus
from astrbot_ex.core.actions.service import ActionService
from astrbot_ex.core.connection_manager import ConnectionManager, zmq
from astrbot_ex.core.decision.backends import jev
from astrbot_ex.core.decision.service import DecisionService
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.plugin_actor import PluginActor
from astrbot_ex.core.topic_bus import TopicBus
from tests.test_goal_manager import make_catalog

try:  # B09 is a separate task; its absence is a dependency gap, not a failure.
    from astrbot_ex.core.decision.instruction_input import (
        SCHEMA,
        DecisionInstructionInput,
        InstructionBatch,
        StepSegment,
    )
    from astrbot_ex.core.step_input import (
        METHOD_CAPABILITIES,
        METHOD_INJECT,
        StepInputIngress,
        StepInputLog,
        build_trusted_context,
        parse_injection_batch,
    )

    B09_AVAILABLE, B09_REASON = True, ""
except Exception as exc:  # noqa: BLE001 - any import failure means "not installed"
    B09_AVAILABLE = False
    B09_REASON = f"upper_dependency_missing: B09 ({exc})"

RUN_ID = "b11e2e"
PUBLIC_SENTINEL = f"UPPER_PUBLIC_{RUN_ID}"
EXEC_PREFIX = f"UPPER_EXEC_SENTINEL_{RUN_ID}"

SESSION = f"ex-session-{RUN_ID}"
ROBOT = "robot-e2e"
ROUTE = "route-e2e"


def exec_sentinel(ordinal: int) -> str:
    return f"{EXEC_PREFIX}_{ordinal}"


def free_tcp_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_until(predicate, timeout: float = 3.0, label: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"{label} was not met before timeout")


def instruction_ack(choice: str = "accept", *, model: str = jev.PINNED_MODEL,
                    usage: dict | None = None) -> bytes:
    return json.dumps(
        {
            "model": model,
            "answers": {jev.INSTRUCTION_ACK_OWNER: {"type": "choice", "choice": choice}},
            "usage": usage or {"input_tokens": 11, "output_tokens": 2},
        }
    ).encode("utf-8")


def upper_batch(segments: list[str], *, batch_id: str | None = None) -> dict:
    """The exact payload shape AEB's ``ControlChannel`` sends."""

    identity = batch_id or uuid.uuid4().hex
    return {
        "schema": SCHEMA,
        "request_id": f"request-{identity}",
        "plan_id": f"plan-{identity}",
        "plan_revision": 1,
        "batch_id": identity,
        "robot_id": ROBOT,
        "route_ref": ROUTE,
        "ex_session": SESSION,
        "catalog_version": "catalog-hash",
        "prompt_version": "prompt-hash",
        "visibility": "decision_only",
        "segments": [
            {"segment_id": f"s{index}", "ordinal": index, "text": text}
            for index, text in enumerate(segments)
        ],
    }


class _Owner:
    """Minimal plugin owner so the dispatcher has something to refuse to use."""

    def __init__(self, owner: str, dispatcher: ActionDispatcher) -> None:
        self.id = owner
        self.dispatcher = dispatcher
        self.commands: list = []
        self.cancels: list = []

    def on_action_command(self, command):
        self.commands.append(command)
        return "accepted"

    def on_action_cancel(self, command_id, reason):
        self.cancels.append(command_id)
        self.dispatcher.report(
            command_id,
            OwnerBinding(self.id, 1),
            ActionStatus.CANCELED,
            stop_evidence=StopEvidence(command_id, True, self.id, command_id),
        )
        return "requested"


@unittest.skipUnless(B09_AVAILABLE, B09_REASON)
class UpperControlCrossEndTests(unittest.TestCase):
    """One real stack per test: ROUTER ⇄ DEALER ⇄ ingress ⇄ service ⇄ backend."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        # -- decision stack (pre-existing EX modules) ----------------------
        self.ledger = ActionLedger(root / "actions.sqlite")
        self.dispatcher = ActionDispatcher(self.ledger)
        self.catalog = make_catalog(("arm",))
        self.owners: dict[str, _Owner] = {}
        self.actors: dict[str, PluginActor] = {}
        for entry in self.catalog.snapshot().entries:
            owner = _Owner(entry["owner"], self.dispatcher)
            actor = PluginActor(owner)
            actor.start()
            self.owners[entry["owner"]] = owner
            self.actors[entry["owner"]] = actor
            self.dispatcher.register_owner(
                OwnerBinding(entry["owner"], 1), actor, entry["manifest"]
            )
        self.actions = ActionService(
            self.ledger, self.dispatcher, self.catalog, stop_timeout=0.12
        )
        self.actions.control_mode = "decision"
        self.actions.update_versions(runtime_state="running")

        # -- real backend, capture-only transport --------------------------
        self.captured: list[bytes] = []
        self.replies: list[bytes] = []
        self.choice = "accept"
        self.backend = self._make_backend()
        self.service = DecisionService(self.actions, backend=self.backend, max_hz=50)
        self.service.set_mode("shadow")
        wait_until(lambda: self.service.goals.phase == "idle", label="idle service")

        # -- ROUTER the control client dials into --------------------------
        self.port = free_tcp_port()
        self.context = zmq.Context.instance()
        self.router = self.context.socket(zmq.ROUTER)
        self.router.setsockopt(zmq.LINGER, 0)
        self.router.bind(f"tcp://127.0.0.1:{self.port}")

        # -- control connection + ingress ----------------------------------
        self.business_calls: list[tuple] = []
        self.manager = ConnectionManager(
            root / "connections.json", event_bus=EventBus(), topic_bus=TopicBus()
        )
        self.manager.set_business_handler(
            lambda *args: (
                self.business_calls.append(args),
                {"ok": False, "error": {"code": "must_not_be_used"}},
            )[1]
        )
        self.manager.create(
            {
                "id": "control",
                "name": "Upper control",
                "type": "zmq_client",
                "enabled": False,
                "config": {
                    "endpoint": f"tcp://127.0.0.1:{self.port}",
                    "identity": "aeb-control",
                    "protocol_profile": "astrbotex",
                    "channel": "control",
                    "upper_robot_id": ROBOT,
                    "upper_route_refs": ROUTE,
                    # Deliberately stale: the runtime session provider must win.
                    "upper_ex_session": "stale-recorded-session",
                },
            }
        )
        self.log_path = root / "execution" / "upper_step_input.jsonl"
        self.ingress = StepInputIngress(
            log=StepInputLog(self.log_path),
            adapter=DecisionInstructionInput(self.service),
            trusted_context=self.manager.control_trusted_context,
            request_budget_sec=2.0,
            worker_budget_sec=5.0,
        )
        self.ingress.start()
        self.manager.set_control_session_provider(lambda: SESSION)
        self.manager.set_step_input_handler(self.ingress.handle)
        self.addCleanup(self._teardown)

    # -- fixture -----------------------------------------------------------

    def _make_backend(self) -> jev.JevBackend:
        def transport(body, headers, deadline, cancel, limit):
            self.captured.append(body)
            reply = self.replies.pop(0) if self.replies else instruction_ack(self.choice)
            return jev.HTTPReply(200, reply)

        # The production backend pins one model; a different value is refused at
        # construction, which is exactly the guarantee we want to keep.
        config = jev.JevConfig(mode="shadow", min_interval_ms=0)
        backend = jev.JevBackend(
            config,
            transport=transport,
            secret_provider=lambda: "test-only-not-a-live-key",
        )
        self.addCleanup(backend.close)
        return backend

    def _teardown(self) -> None:
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

    def start_control(self) -> bytes:
        """Start the control client and capture the identity it announces."""

        self.manager.start("control")
        wait_until(
            lambda: self.manager.get("control")["runtime"]["state"] == "running",
            label="control client running",
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not self.router.poll(50):
                continue
            frames = self.router.recv_multipart()
            envelope = json.loads(frames[1].decode("utf-8"))
            if envelope.get("method") == "system.hello":
                return frames[0]
        raise AssertionError("the control DEALER never announced itself")

    def call(self, peer: bytes, method: str, body, *, binary: bytes | None = None,
             channel: str = "control", timeout: float = 8.0):
        request_id = uuid.uuid4().hex
        envelope = {
            "protocol": "astrbotex-zmq",
            "version": 1,
            "channel": channel,
            "kind": "request",
            "id": request_id,
            "method": method,
            "timestamp": time.time(),
            "payload": body,
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

    def captured_request(self, index: int = 0) -> dict:
        self.assertGreater(len(self.captured), index, "the backend never received a request")
        return json.loads(self.captured[index])

    def assert_no_execution(self) -> None:
        self.assertEqual(self.business_calls, [], "upper input reached the public handler")
        self.assertIsNone(self.service.goals.active, "upper input invented a Goal")
        self.assertFalse(self.dispatcher._gate, "upper input opened the execution gate")
        for owner in self.owners.values():
            self.assertEqual(owner.commands, [], "upper input issued an action command")

    # -- capabilities ------------------------------------------------------

    def test_capabilities_reports_schema_readiness_and_limits(self) -> None:
        peer = self.start_control()
        body, binary = self.call(peer, METHOD_CAPABILITIES, {})

        self.assertEqual(binary, [])
        self.assertTrue(body["ok"])
        self.assertEqual(body["schema"], SCHEMA)
        self.assertEqual(body["visibility"], "decision_only")
        self.assertEqual(body["methods"], [METHOD_CAPABILITIES, METHOD_INJECT])
        self.assertTrue(body["input_ready"], body.get("input_ready_reason"))
        self.assertEqual(body["ex_session"], SESSION)
        self.assertEqual(body["backend"], "JevBackend")
        self.assertEqual(body["max_segments"], 64)
        self.assertEqual(body["max_bytes"], 256 * 1024)
        self.assertEqual(body["limits"]["max_segment_chars"], 4096)

    # -- the batch really crosses ------------------------------------------

    def test_three_segments_reach_the_real_backend_request_in_order(self) -> None:
        peer = self.start_control()
        segments = [f"第{index}段 {exec_sentinel(index)}。" for index in range(3)]
        payload = upper_batch(segments)

        body, binary = self.call(peer, METHOD_INJECT, payload)

        self.assertEqual(binary, [])
        self.assertTrue(body["ok"], body.get("error"))
        self.assertEqual(body["phase"], "injected")
        self.assertEqual(body["batch_id"], payload["batch_id"])
        self.assertEqual(body["request_id"], payload["request_id"])
        self.assertEqual(body["ex_session"], SESSION)
        self.assertEqual(body["segment_count"], 3)
        self.assertFalse(body["duplicate"])
        self.assertEqual(body["receipt"]["phase"], "injected")

        # Exactly one request, built by the real backend serializer.
        self.assertEqual(len(self.captured), 1)
        request = self.captured_request()
        self.assertEqual(request["model"], jev.PINNED_MODEL)
        self.assertEqual(set(request), {"model", "state", "questions"})

        delivered = request["state"]["instruction_input"]
        self.assertEqual([segment["text"] for segment in delivered["segments"]], segments)
        self.assertEqual([segment["ordinal"] for segment in delivered["segments"]], [0, 1, 2])
        self.assertEqual(
            [segment["segment_id"] for segment in delivered["segments"]], ["s0", "s1", "s2"]
        )
        # The batch travels as one immutable block, byte-identical to the wire form.
        self.assertEqual(delivered, parse_injection_batch(payload).to_wire())
        self.assertEqual(delivered["batch_id"], body["batch_id"])
        self.assertEqual(delivered["catalog_version"], "catalog-hash")
        self.assertEqual(delivered["prompt_version"], "prompt-hash")

        # A receipt question is not a decision: no Goal, no snapshot, no options.
        self.assertNotIn("snapshot", request["state"])
        self.assertNotIn("goal", request["state"])
        self.assertEqual(list(request["questions"]), [jev.INSTRUCTION_ACK_OWNER])
        self.assertEqual(
            set(request["questions"][jev.INSTRUCTION_ACK_OWNER]["criteria"]),
            {"accept", "reject"},
        )

        # Public-facing text never travels toward the model.
        self.assertNotIn(PUBLIC_SENTINEL.encode(), self.captured[0])
        self.assertEqual(self.service._instruction_rounds, 1)
        self.assert_no_execution()

    def test_single_segment_is_not_split_or_rewritten(self) -> None:
        peer = self.start_control()
        payload = upper_batch([f"只发一段 {exec_sentinel(0)}。"])

        body, _ = self.call(peer, METHOD_INJECT, payload)

        self.assertTrue(body["ok"], body.get("error"))
        delivered = self.captured_request()["state"]["instruction_input"]
        self.assertEqual(delivered["segments"], payload["segments"])

    # -- idempotence and conflict ------------------------------------------

    def test_duplicate_batch_is_not_consumed_twice(self) -> None:
        peer = self.start_control()
        payload = upper_batch([f"执行 {exec_sentinel(0)}。"])

        first, _ = self.call(peer, METHOD_INJECT, payload)
        second, _ = self.call(peer, METHOD_INJECT, payload)

        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["payload_hash"], second["payload_hash"])
        self.assertEqual(first["phase"], "injected")
        self.assertEqual(second["phase"], "injected")
        self.assertEqual(len(self.captured), 1, "the model was shown the batch twice")
        self.assertEqual(self.service._instruction_rounds, 1)

    def test_same_batch_id_with_new_content_is_a_conflict(self) -> None:
        peer = self.start_control()
        payload = upper_batch([f"执行 {exec_sentinel(0)}。"])
        self.call(peer, METHOD_INJECT, payload)

        changed = dict(
            payload,
            segments=[{"segment_id": "s0", "ordinal": 0, "text": "改过的文本"}],
        )
        body, _ = self.call(peer, METHOD_INJECT, changed)

        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "id_conflict")
        self.assertEqual(body["error_code"], "id_conflict")
        self.assertEqual(body["phase"], "rejected")
        self.assertEqual(len(self.captured), 1)

    def test_declining_ack_is_never_reported_as_injected(self) -> None:
        peer = self.start_control()
        self.choice = "reject"

        body, _ = self.call(peer, METHOD_INJECT, upper_batch([f"执行 {exec_sentinel(0)}。"]))

        self.assertTrue(body["ok"], "a declined ack is still a well-formed receipt")
        self.assertEqual(body["phase"], "rejected")
        self.assertEqual(body["reason"], "declined")
        self.assertEqual(len(self.captured), 1)
        self.assert_no_execution()

    # -- rejections --------------------------------------------------------

    def test_malformed_and_untrusted_payloads_never_reach_the_model(self) -> None:
        peer = self.start_control()
        cases = (
            ("unknown field", dict(upper_batch(["x"]), public_text=PUBLIC_SENTINEL), None,
             "invalid_batch"),
            ("binary frame", upper_batch(["x"]), b"\x00\x01", "invalid_batch"),
            ("wrong robot", dict(upper_batch(["x"]), robot_id="intruder"), None,
             "session_mismatch"),
            ("stale session", dict(upper_batch(["x"]), ex_session="stale-recorded-session"),
             None, "session_mismatch"),
            ("foreign route", dict(upper_batch(["x"]), route_ref="other-route"), None,
             "route_mismatch"),
            ("wrong schema", dict(upper_batch(["x"]), schema="aeb.upper_steps.v2"), None,
             "invalid_batch"),
            ("not decision-only", dict(upper_batch(["x"]), visibility="public"), None,
             "invalid_batch"),
        )
        for label, body, binary, expected in cases:
            with self.subTest(label=label):
                reply, _ = self.call(peer, METHOD_INJECT, body, binary=binary)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"]["code"], expected)
                self.assertEqual(reply["error_code"], expected)

        self.assertEqual(self.captured, [], "a rejected batch still reached the backend")
        self.assertEqual(self.service._instruction_rounds, 0)

    def test_upper_methods_are_refused_off_the_control_channel(self) -> None:
        result, _ = self.manager._handle_business_request(
            "text", METHOD_INJECT, upper_batch(["x"]), None, connection_id="control"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "route_mismatch")
        self.assertEqual(self.business_calls, [])
        self.assertEqual(self.captured, [])

    def test_unknown_method_on_control_does_not_fall_back(self) -> None:
        peer = self.start_control()

        body, _ = self.call(peer, "upper.steps.cancel", {})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "unsupported_method")

        body, _ = self.call(peer, "interaction.message", {"text": PUBLIC_SENTINEL})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "unsupported_method")
        self.assertEqual(self.business_calls, [])
        self.assertEqual(self.captured, [])

    # -- durability --------------------------------------------------------

    def test_receipt_is_durable_and_replays_without_re_injection(self) -> None:
        peer = self.start_control()
        payload = upper_batch([f"执行 {exec_sentinel(0)}。"])
        self.call(peer, METHOD_INJECT, payload)

        # A fresh log over the same file must answer from the ledger.
        reopened = StepInputLog(self.log_path)
        receipt = reopened.get(payload["batch_id"])
        self.assertIsNotNone(receipt, "the receipt was not persisted")
        self.assertEqual(receipt.phase, "injected")
        self.assertEqual(receipt.segment_ids, ("s0",))

        # Replaying against the reopened ledger must not consume again.
        replay = StepInputIngress(
            log=reopened,
            adapter=DecisionInstructionInput(self.service),
            trusted_context=self.manager.control_trusted_context,
            request_budget_sec=1.0,
            worker_budget_sec=5.0,
        )
        replay.start()
        try:
            body, _ = replay.handle(
                "control", METHOD_INJECT, payload, None, feature="control"
            )
        finally:
            replay.close()
        self.assertTrue(body["duplicate"])
        self.assertEqual(len(self.captured), 1)

    def test_inflight_batch_recovers_as_unknown_after_restart(self) -> None:
        batch_id = f"inflight-{uuid.uuid4().hex}"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "request_id": "request-inflight",
                    "payload_hash": "deadbeef",
                    "phase": "received",
                    "connection_id": "control",
                    "route_ref": ROUTE,
                    "ex_session": SESSION,
                    "robot_id": ROBOT,
                    "segment_ids": ["s0"],
                    "received_at": time.time(),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        recovered = StepInputLog(self.log_path)
        receipt = recovered.get(batch_id)

        self.assertIsNotNone(receipt)
        # Never claim a clean rejection for a batch that may already be in flight.
        self.assertEqual(receipt.phase, "unknown")
        self.assertEqual(receipt.reason, "recovered_after_restart")
        self.assertEqual(self.captured, [])


@unittest.skipUnless(B09_AVAILABLE, B09_REASON)
class BackendTransportCaptureTests(unittest.TestCase):
    """The real HTTP transport, not the injected one, posting to a local capture.

    The B11 loopback stage points the backend at a capture server so the body can
    be inspected. This proves that harness actually observes the real request.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bodies: list[bytes] = []
        bodies = self.bodies

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server contract
                length = int(self.headers.get("Content-Length", "0"))
                bodies.append(self.rfile.read(length))
                payload = instruction_ack()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence the default stderr logging
                return None

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5.0)
        self.addCleanup(self.server.shutdown)

    def test_real_transport_posts_the_batch_to_the_capture(self) -> None:
        port = self.server.server_port
        backend = jev.JevBackend(
            jev.JevConfig(
                base_url=f"http://127.0.0.1:{port}",
                mode="shadow",
                allow_live_http=True,
                min_interval_ms=0,
            ),
            secret_provider=lambda: "test-only-not-a-live-key",
        )
        self.addCleanup(backend.close)

        segments = [f"第{index}段 {exec_sentinel(index)}。" for index in range(3)]
        batch = parse_injection_batch(upper_batch(segments))
        outcome = backend.consume_instruction(batch)

        self.assertTrue(outcome["accepted"], outcome)
        self.assertEqual(len(self.bodies), 1, "the capture saw no request")
        request = json.loads(self.bodies[0])
        self.assertEqual(request["model"], jev.PINNED_MODEL)
        self.assertEqual(request["state"]["instruction_input"], batch.to_wire())
        self.assertEqual(
            [segment["text"] for segment in request["state"]["instruction_input"]["segments"]],
            segments,
        )
        self.assertNotIn("test-only", repr(backend.last_instruction_record))


if __name__ == "__main__":
    unittest.main()
