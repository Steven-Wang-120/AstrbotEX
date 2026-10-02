"""Shared EX-only composition fixture; no Host or sibling checkout required."""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import AsyncExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.actions.ledger import OwnerBinding, StopEvidence
from astrbot_ex.core.contracts import DecisionState, EventsReply
from astrbot_ex.core.decision.backends import MockBackend
from astrbot_ex.core.decision.feedback_journal import FeedbackJournal
from astrbot_ex.core.plugin_actor import PluginActor
from tests.test_decision_service import ActionOwner, wait_for
from tests.test_goal_manager import goal_payload, make_catalog

class WiringFixture(unittest.TestCase):
    def setUp(self):
        self.actors, self.owners = [], {}
        self._fixture_closed = False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.dict(os.environ, {"ASTRBOTEX_DATA_DIR": self.tmp.name,
                "ASTRBOTEX_STT_ENABLED": "", "ASTRBOTEX_TTS_ENABLED": ""}):
            self.server = build_server("127.0.0.1", 0, 20)
        self.addCleanup(self._close_fixture)
        self.s = self.server.decision_service
        self.c = self.server.decision_controller
        self.m = self.server.connections
        self.actors, self.owners = [], {}
        self.m.create({"id": "trusted", "name": "offline", "type": "zmq_client", "enabled": False,
            "config": {"protocol_profile": "astrbotex", "channel": "text",
                       "endpoint": "tcp://127.0.0.1:39999", "identity": "trusted"}})
        self.m._records["trusted"].enabled = True

    def tearDown(self):
        self._close_fixture()

    def _close_fixture(self):
        if self._fixture_closed:
            return
        self._fixture_closed = True
        actors = tuple(self.actors)
        try:
            for owner in self.owners.values():
                owner.stop_proof = True
            self.server.decision_service.request_stop("fixture cleanup")
            ledger = self.server.action_ledger
            dispatcher = self.server.action_dispatcher
            for row in ledger.list_commands(limit=500).result(1):
                owner = self.owners.get(row.owner)
                if owner is None or row.status in {"succeeded", "rejected", "canceled"}:
                    continue
                proof = ledger.stop_proof(row.command_id, OwnerBinding(row.owner, row.generation)).result(1)
                if proof is not None and proof.stopped is True and not row.held_resources:
                    continue
                actor = next(actor for actor in actors if actor.plugin is owner)
                # Invoke the actual mock controller's stop through its Actor.
                actor.submit_action_cancel(row.command_id, "fixture cleanup").result(2)
                dispatcher._enqueue(lambda: None, priority=True).result(1)
                proof = ledger.stop_proof(row.command_id, OwnerBinding(row.owner, row.generation)).result(1)
                self.assertIsNotNone(proof, self.diagnostics())
                self.assertTrue(proof.stopped, self.diagnostics())
            self.assertTrue(self.server.action_service.await_stop_proof("fixture cleanup"), self.diagnostics())
            if self.server.decision_service.goals.phase == "blocked":
                self.assertEqual(self.server.decision_service.goals.phase, "blocked")
            self.server.decision_service.review().result(2)  # explicit fixture-only review, never auto-unblock
            self.server.server_close()
        finally:
            for actor in actors:
                actor.stop(2)

    def diagnostics(self):
        service = self.server.decision_service
        dispatcher = self.server.action_dispatcher
        with service._condition, service.goals._lock, dispatcher._lock:
            result = {"goals": service.goals.status(), "mode": service.mode,
                "control_mode": service.actions.control_mode, "gate": dispatcher._gate,
                "dispatcher_epoch": dispatcher._epoch, "dispatcher_blocked": dispatcher.blocked,
                "faults": dispatcher.faults, "stop_proof_epoch": service.actions._stop_proof_epoch,
                "last_error": service._last_error, "stop_error": service._stop_error,
                "stop_attempts": service._stop_attempts, "decisions": list(service._decisions)[-8:],
                "framework_versions": service._last_framework_versions,
                "catalog_revision": service._last_catalog_revision,
                "dispatcher_versions": dispatcher._versions,
                "rows": [{"command_id": row.command_id, "status": row.status,
                          "held_resources": row.held_resources, "event_seq": row.event_seq,
                          "positive_proof": row.command_id in service._proven_uncertain}
                         for row in service._rows],
                "actors": [actor.action_mailbox_stats() for actor in self.actors]}
        return json.dumps(result, default=str, ensure_ascii=False)

    def execute(self, owners=("arm",), stop_proof=True):
        catalog = make_catalog(owners, resource="shared" if len(owners) == 1 else None)
        self.server.capability_catalog.refresh([
            # Refresh using original validated CapabilityInput, not a fake catalog.
            __import__("astrbot_ex.core.decision.catalog", fromlist=["CapabilityInput"]).CapabilityInput(
                entry["owner"], entry["generation"],
                __import__("astrbot_ex.core.actions.models", fromlist=["parse_action_manifest"]).parse_action_manifest(entry["manifest"]),
                {}, entry["guide"], True, "1") for entry in catalog.snapshot().entries])
        for entry in catalog.snapshot().entries:
            owner = ActionOwner(entry["owner"], self.server.action_dispatcher, stop_proof=stop_proof)
            actor = PluginActor(owner)
            actor.start()
            self.actors.append(actor)
            self.owners[owner.id] = owner
            self.server.action_dispatcher.register_owner(OwnerBinding(owner.id, 1), actor, entry["manifest"])
        self.server.action_service.control_mode = "decision"
        self.server.action_service.update_versions(runtime_state="running")
        self.s.backend = MockBackend(kind="start")
        self.s.set_mode("execute")
        self.assertTrue(wait_for(lambda: self.s.goals.phase == "idle"))
        self.assertTrue(wait_for(lambda: self.s._last_catalog_revision == self.server.capability_catalog.snapshot().revision))
        self.assertTrue(wait_for(lambda: self.s._last_framework_versions == (
            self.server.action_service._runtime_state, self.server.action_service._config_revision,
            self.server.action_service._environment_revision, self.server.action_service.control_mode)),
            self.diagnostics())
        # RuntimeCapabilityCatalog on_change includes real config/environment values.
        return self.s

    def request(self, method, payload, conn="trusted", feature="text", binary=None):
        return self.m._handle_business_request(feature, method, payload, binary, connection_id=conn)[0]

    def turn(self, generation=1, turn_id="turn", **extra):
        return {"task_schema_version": 1, "operation": "bind", "ex_session": self.s.goals.ex_session,
            "task_id": "task", "robot_id": "robot", "session_id": "session", "user_id": "user",
            "route_ref": "route", "turn_id": turn_id, "generation": generation,
            "expected_revision": self.s.goals.revision, **extra}

    def bind(self, generation=1, **extra):
        turn = self.turn(generation, **extra)
        self.assertTrue(self.request("interaction.task.turn", turn)["ok"])
        return turn

    def submit(self, n=1, owners=("arm",), **extra):
        return self.request("decision.goal.submit", goal_payload(self.s.goals, n, owners,
            expected_revision=self.s.goals.revision,
            completion={"required_success_actions": [f"{owner}.move.v1" for owner in owners]}, **extra))

    def public(self, turn, message_id="public-1", delivery="text", **extra):
        return {k: v for k, v in {**turn, "visibility": "user", "source": "private_planning",
            "message_id": message_id, "text": "A bounded progress update.", "delivery": delivery,
            "claim": "progress", **extra}.items()
            if k not in {"task_schema_version", "operation", "expected_revision"}}

    def succeed(self, owner="arm"):
        plugin = self.owners[owner]
        self.assertTrue(plugin.started.wait(2))
        cid = plugin.commands[-1].command_id
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(cid).result(1).status == "accepted"))
        binding = OwnerBinding(owner, 1)
        self.server.action_dispatcher.report(cid, binding, "running").result(1)
        self.server.action_dispatcher.report(cid, binding, "succeeded").result(1)
        return cid


