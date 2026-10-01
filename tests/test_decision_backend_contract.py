from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path

from astrbot_ex.core.actions.models import ContractError
from astrbot_ex.core.decision.backends.jev import HTTPReply, JevBackend, JevBackendError, JevConfig
from astrbot_ex.core.decision.backends.registry import BACKEND_FACTORIES, backend_names, create_backend
from astrbot_ex.core.decision.models import BackendDecision, DecisionSnapshot, VersionSet, validate_backend_selection

FIXTURE = json.loads((Path(__file__).parent / "fixtures/decision/jev/normal.json").read_text(encoding="utf-8"))


class BackendContractTests(unittest.TestCase):
    def test_static_registry_no_dynamic_import_or_plugin_lookup(self):
        from astrbot_ex.core.decision.backends.mock import MockBackend
        self.assertEqual(backend_names(), ("mock", "jev"))
        self.assertIs(BACKEND_FACTORIES["mock"], MockBackend)
        mock = create_backend("mock")
        self.assertTrue(mock.execution_allowed)
        mock.close()
        self.assertIs(BACKEND_FACTORIES["jev"], JevBackend)
        with self.assertRaises(TypeError):
            BACKEND_FACTORIES["evil"] = JevBackend
        for name in ("evil.module:Backend", "__import__('os')", "../plugin", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                create_backend(name)
        backend = create_backend("jev")
        self.assertFalse(backend.execution_allowed)
        with self.assertRaises(AttributeError):
            backend.execution_allowed = True
        self.assertEqual(backend.config.mode, "disabled")
        self.assertFalse(backend.config.allow_live_http)
        with self.assertRaises(JevBackendError) as caught:
            backend.decide(DecisionSnapshot.parse(FIXTURE["snapshot"]))
        self.assertEqual(caught.exception.code, "disabled")
        backend.close()

    def test_synchronous_duck_interface_and_no_execution_side_effects(self):
        requests = []
        def transport(body, *args):
            requests.append(json.loads(body))
            return HTTPReply(200, json.dumps(FIXTURE["response"]).encode())
        backend = create_backend("jev", config=JevConfig(mode="shadow"), transport=transport,
                                 secret_provider=lambda: "offline-test-only")
        original = DecisionSnapshot.parse(FIXTURE["snapshot"])
        before = original.to_dict()
        decision = backend.decide(original)
        self.assertIsInstance(decision, BackendDecision)
        self.assertEqual(original.to_dict(), before)
        selected = validate_backend_selection(original, decision, original.versions)
        self.assertEqual(selected[0]["action_id"], "arm.pick.v1")
        self.assertNotIn("params", decision.to_dict()["choices"][0])
        self.assertEqual(len(requests), 1)
        self.assertFalse(hasattr(backend, "dispatcher"))
        self.assertFalse(hasattr(backend, "plugin_manager"))
        # Shadow only returns/records data. No executor, publisher or lease owner
        # is accepted by the constructor. Integration must keep that separation.
        with self.assertRaises(TypeError):
            create_backend("jev", dispatcher=lambda *a: None)
        backend.close()

    def test_every_trusted_version_changed_must_fail_before_external_dispatch(self):
        backend = JevBackend(JevConfig(mode="shadow"), transport=lambda *a: HTTPReply(200, json.dumps(FIXTURE["response"]).encode()),
                             secret_provider=lambda: "offline-test-only")
        original = DecisionSnapshot.parse(FIXTURE["snapshot"])
        decision = backend.decide(original)
        for field in ("ex_session", "goal_revision", "config_revision", "catalog_revision",
                      "environment_generation", "gate_epoch", "plugin_generations"):
            with self.subTest(field=field):
                wire = original.versions.to_dict()
                if field == "ex_session":
                    wire[field] = "replacement-session"
                elif field == "plugin_generations":
                    wire[field]["arm"] += 1
                else:
                    wire[field] += 1
                with self.assertRaises(ContractError):
                    validate_backend_selection(original, decision, VersionSet.parse(wire))
        replaced = copy.deepcopy(original)
        replaced.snapshot_id = "replacement-snapshot"
        with self.assertRaises(ContractError):
            validate_backend_selection(replaced, decision, original.versions)
        backend.close()

    def test_reconfigure_immutable_config_cannot_mutate_without_epoch(self):
        backend = JevBackend()
        with self.assertRaises(AttributeError):
            backend.config.model = "jev-1.14.0"
        with self.assertRaises(AttributeError):
            backend.config = JevConfig()
        backend.reconfigure(replace(backend.config, model="jev-1.14.0"))
        self.assertEqual(backend.config.model, "jev-1.14.0")
        self.assertEqual(backend._epoch, 1)
        backend.close()


if __name__ == "__main__":
    unittest.main()
