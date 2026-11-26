from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_ex.core.connection_manager import ConnectionManager


class DecisionTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = ConnectionManager(Path(self.tmp.name) / "connections.json",
            event_bus=SimpleNamespace(emit=lambda *a, **k: None),
            topic_bus=SimpleNamespace(publish_payload=lambda *a, **k: None))
        self.calls = []
        self.public = []
        def decision(connection_id, method, parsed):
            self.calls.append((connection_id, method, parsed))
            return {"ok": True}
        def legacy(feature, method, payload, binary):
            self.public.append(payload)
            return {"ok": True}, None
        def task_public(connection_id, payload):
            self.public.append(payload)
            return {"ok": True}
        self.manager.set_decision_handler(decision,
            public_validator=lambda c, p: c == "trusted" and p["generation"] == 2,
            public_handler=task_public)
        self.manager.set_business_handler(legacy)

    def request(self, method, payload, feature="text", connection="trusted", binary=None):
        return self.manager._handle_business_request(feature, method, payload, binary, connection_id=connection)[0]

    def test_all_frozen_request_methods_dispatch_injected_callable(self):
        common = {"schema_version": 1, "request_id": "req", "ex_session": "ex", "goal_id": "g"}
        requests = {
            "decision.capabilities.get": {"schema_version": 1},
            "decision.context.get": {"schema_version": 1},
            "decision.state.get": {"schema_version": 1},
            "decision.events.get": {"schema_version": 1, "ex_session": "ex", "since_event_seq": 0},
            "decision.goal.submit": {**common, "task_id": "t", "step_id": "s", "goal_text_en": "Wait safely.", "lease_ms": 1000},
            "decision.goal.cancel": {**common, "goal_revision": 1},
            "decision.goal.renew": {**common, "goal_revision": 1, "lease_ms": 1000},
            "decision.feedback": {"schema_version": 1, "ex_session": "ex", "task_id": "t", "goal_id": "g", "goal_revision": 1, "event_seq": 1, "status": "running"},
        }
        for method, payload in requests.items():
            self.assertTrue(self.request(method, payload)["ok"])
        self.assertEqual({c[1] for c in self.calls}, set(requests))
        self.assertEqual({c[0] for c in self.calls}, {"trusted"})
        self.assertEqual(self.public, [])

    def test_unknown_invalid_identity_binary_do_not_fall_back(self):
        for method, payload, feature, conn, binary in [
            ("decision.invented", {}, "text", "trusted", None),
            ("decision.goal.submit", {"schema_version": 1}, "text", "trusted", None),
            ("decision.context.get", {"schema_version": 1}, "audio", "trusted", None),
            ("decision.context.get", {"schema_version": 1}, "text", "", None),
            ("decision.context.get", {"schema_version": 1}, "text", "trusted", b"data"),
        ]:
            self.assertFalse(self.request(method, payload, feature, conn, binary)["ok"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.public, [])

    def test_capabilities_independent_exact_parser_does_not_relax_b00(self):
        from astrbot_ex.core.contracts import ContractError, parse_request
        with self.assertRaises(ContractError):
            parse_request("decision.capabilities.get", {"schema_version": 1})
        for payload in ({}, {"schema_version": True}, {"schema_version": 2},
                        {"schema_version": 1, "ex_session": "ex"}, {"schema_version": 1, "task_id": "t"}):
            self.assertFalse(self.request("decision.capabilities.get", payload)["ok"])
        for feature, conn, binary in (("audio", "trusted", None), ("text", "", None),
                                      ("text", "trusted", b"")):
            self.assertFalse(self.request("decision.capabilities.get", {"schema_version": 1},
                feature, conn, binary)["ok"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.public, [])
        self.assertTrue(self.request("decision.capabilities.get", {"schema_version": 1})["ok"])
        self.assertEqual(self.calls[0], ("trusted", "decision.capabilities.get", {"schema_version": 1}))

    def test_public_task_gate_generation_dedup_and_normal_chat(self):
        payload = {"visibility": "user", "source": "private_planning", "task_id": "t", "turn_id": "turn",
                   "generation": 2, "route_ref": "route", "message_id": "m", "ex_session": "ex",
                   "session_id": "s", "robot_id": "r", "text": "Trying.", "delivery": "text"}
        self.assertTrue(self.request("interaction.reply", payload)["ok"])
        self.assertTrue(self.request("interaction.reply", payload)["duplicate"])
        self.assertFalse(self.request("interaction.reply", {**payload, "generation": 1})["ok"])
        self.assertFalse(self.request("interaction.reply", {"task_id": "t", "text": "private raw"})["ok"])
        self.assertFalse(self.request("interaction.reply", payload, connection="foreign")["ok"])
        self.assertTrue(self.request("interaction.reply", {"text": "normal chat"})["ok"])
        self.assertEqual(len(self.public), 2)
