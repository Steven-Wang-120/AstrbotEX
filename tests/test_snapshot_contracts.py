from __future__ import annotations

import copy
import json
import math
import re
import unittest
from pathlib import Path

from astrbot_ex.core import contracts as c
from astrbot_ex.core.decision import models as d


VERSIONS = {
    "ex_session": "ex-1", "goal_revision": 9, "config_revision": 2,
    "catalog_revision": 3, "environment_generation": 4, "gate_epoch": 5,
    "plugin_generations": {"arm": 7, "base": 8},
}
OBSERVATION = {
    "schema_version": 1, "observation_id": "obs-1", "source_id": "front",
    "source_epoch": "camera-boot-2", "seq": 3,
    "received_monotonic_ns": 2**53 + 2, "age_ms": 12.5,
    "description_hash": "hash-1", "health": {"status": "ok", "reason_code": ""},
    "data": {"target": {"depth_m": 0.3, "selected": True}},
}
SNAPSHOT = {
    "schema_version": 1, "snapshot_id": "snap-1", "created_monotonic_ns": 2**53 + 3,
    "versions": VERSIONS,
    "goal": {"task_id": "task-1", "goal_id": "goal-1", "goal_text_en": "Pick the cup.",
             "allowed_actions": ["arm.pick.v1"], "parameters": {"arm.pick.v1": {"target": {"observation_id": "obs-1"}}}},
    "observations": [OBSERVATION],
    "owners": [
        {"owner": "arm", "plugin_generation": 7, "status": "ready", "candidates": [
            {"option_id": "arm-start", "kind": "start", "description": "Pick cup", "eligible": True, "action_id": "arm.pick.v1"},
            {"option_id": "arm-wait", "kind": "wait", "description": "Wait", "eligible": True},
            {"option_id": "arm-blocked", "kind": "start", "description": "Blocked", "eligible": False, "action_id": "arm.pick.v1"},
        ]},
        {"owner": "base", "plugin_generation": 8, "status": "ready", "candidates": [
            {"option_id": "base-wait", "kind": "wait", "description": "Hold base", "eligible": True},
        ]},
    ],
}
DECISION = {
    "schema_version": 1, "snapshot_id": "snap-1", "versions": VERSIONS,
    "backend": "mock", "model": "policy-1", "elapsed_ms": 12.5,
    "choices": [
        {"owner": "arm", "option_id": "arm-start", "confidence": 0.75,
         "probabilities": {"arm-start": 0.75, "arm-wait": 0.25}},
        {"owner": "base", "option_id": "base-wait"},
    ],
}


class SnapshotContractTests(unittest.TestCase):
    def assert_rejected(self, parser, payload, code, path=None):
        with self.assertRaises(c.ContractError) as caught:
            parser(payload)
        self.assertEqual(caught.exception.error.code, code)
        if path is not None:
            self.assertEqual(caught.exception.error.path, path)

    def test_observation_fractional_age_64_bit_time_and_original_data_copy(self):
        wire = copy.deepcopy(OBSERVATION)
        parsed = c.ObservationEnvelope.parse(wire)
        self.assertEqual(parsed.to_dict(), wire)
        wire["data"]["target"]["depth_m"] = 9
        self.assertEqual(parsed.data["target"]["depth_m"], 0.3)
        parsed.to_dict()["data"]["target"]["depth_m"] = 8
        self.assertEqual(parsed.data["target"]["depth_m"], 0.3)
        for value, code in ((True, "invalid_type"), (-1, "range_violation"), (math.inf, "non_finite_number")):
            bad = copy.deepcopy(OBSERVATION); bad["age_ms"] = value
            self.assert_rejected(c.ObservationEnvelope.parse, bad, code)
        for value, code in ((2**63, "range_violation"), (1.0, "invalid_type")):
            bad = copy.deepcopy(OBSERVATION); bad["received_monotonic_ns"] = value
            self.assert_rejected(c.ObservationEnvelope.parse, bad, code)

    def test_versions_integer_generation_and_bounds(self):
        self.assertEqual(c.VersionSet.parse(VERSIONS).to_dict(), VERSIONS)
        for value, code in (("env-4", "invalid_type"), (True, "invalid_type"), (-1, "range_violation")):
            bad = copy.deepcopy(VERSIONS); bad["environment_generation"] = value
            self.assert_rejected(c.VersionSet.parse, bad, code)
        bad = copy.deepcopy(VERSIONS); bad["plugin_generations"]["arm"] = True
        self.assert_rejected(c.VersionSet.parse, bad, "invalid_type")

    def test_snapshot_roundtrip_owners_candidates_and_defaults(self):
        wire = copy.deepcopy(SNAPSHOT)
        parsed = c.DecisionSnapshot.parse(wire)
        self.assertEqual(parsed.to_dict()["owners"][0]["candidates"][0]["reason_code"], "")
        wire["observations"][0]["data"]["target"]["depth_m"] = 99
        self.assertEqual(parsed.observations[0]["data"]["target"]["depth_m"], 0.3)
        parsed.to_dict()["owners"][0]["candidates"].clear()
        self.assertEqual(len(parsed.owners[0]["candidates"]), 3)
        for mutation, code in (
            (lambda x: x["owners"][0].update(plugin_generation=8), "revision_conflict"),
            (lambda x: x["owners"][1].update(owner="arm"), "enum_violation"),
            (lambda x: x["owners"][1]["candidates"][0].update(option_id="arm-start"), "enum_violation"),
            (lambda x: x["owners"][0]["candidates"][0].update(action_id="base.move.v1"), "unknown_action"),
            (lambda x: x["owners"][0]["candidates"][0].update(command_id="cmd-1"), "invalid_type"),
            (lambda x: x["owners"][0]["candidates"][1].update(command_id="cmd-1"), "invalid_type"),
            (lambda x: x["owners"][0]["candidates"][1].update(action_id=None), "invalid_type"),
        ):
            bad = copy.deepcopy(SNAPSHOT); mutation(bad)
            self.assert_rejected(c.DecisionSnapshot.parse, bad, code)
        valid = copy.deepcopy(SNAPSHOT)
        valid["owners"][0]["candidates"].append({"option_id": "arm-cancel", "kind": "cancel", "description": "Stop", "eligible": True, "command_id": "cmd-1"})
        self.assertEqual(c.DecisionSnapshot.parse(valid).owners[0]["candidates"][-1]["command_id"], "cmd-1")

    def test_backend_selection_revalidates_mutated_models_and_duplicate_owners(self):
        snapshot = c.DecisionSnapshot.parse(SNAPSHOT)
        decision = c.BackendDecision.parse(DECISION)
        decision.choices[1] = copy.deepcopy(decision.choices[0])
        self.assert_rejected(lambda value: c.validate_backend_selection(snapshot, value, c.VersionSet.parse(VERSIONS)), decision, "enum_violation", "decision.choices[1].owner")
        direct = d.BackendDecision("snap-1", d.VersionSet.parse(VERSIONS), "mock", "policy", math.nan, [{"owner": "arm", "option_id": "arm-start"}, {"owner": "base", "option_id": "base-wait"}])
        self.assert_rejected(lambda value: c.validate_backend_selection(snapshot, value, c.VersionSet.parse(VERSIONS)), direct, "non_finite_number", "decision.elapsed_ms")
        direct_versions = d.VersionSet("ex-1", 9, 2, 3, 4, 5, {"arm": True, "base": 8})
        self.assert_rejected(lambda value: c.validate_backend_selection(snapshot, c.BackendDecision.parse(DECISION), value), direct_versions, "invalid_type", "current_versions.plugin_generations.arm")
        current = c.VersionSet.parse(VERSIONS)
        decision = c.BackendDecision.parse(DECISION)
        snapshot.owners[0]["candidates"][0]["eligible"] = False
        self.assert_rejected(lambda value: c.validate_backend_selection(value, decision, current), snapshot, "unknown_action", "decision.choices[0].option_id")
        snapshot = c.DecisionSnapshot.parse(SNAPSHOT)
        for field in ("goal_revision", "config_revision", "catalog_revision", "environment_generation", "gate_epoch"):
            with self.subTest(field=field):
                current = c.VersionSet.parse(VERSIONS)
                setattr(current, field, getattr(current, field) + 1)
                self.assert_rejected(lambda value: c.validate_backend_selection(snapshot, decision, value), current, "revision_conflict", "decision.versions")
        decision.versions.plugin_generations["arm"] = False
        self.assert_rejected(lambda value: c.validate_backend_selection(snapshot, value, c.VersionSet.parse(VERSIONS)), decision, "invalid_type", "decision.versions.plugin_generations.arm")
        snapshot.versions.plugin_generations = None
        self.assert_rejected(lambda value: c.validate_backend_selection(value, c.BackendDecision.parse(DECISION), c.VersionSet.parse(VERSIONS)), snapshot, "invalid_type", "snapshot.versions.plugin_generations")
        self.assert_rejected(lambda value: c.validate_backend_selection(snapshot, c.BackendDecision.parse(DECISION), value), None, "invalid_type", "current_versions")

    def test_nested_snapshot_errors_include_indexes_and_versions(self):
        second = copy.deepcopy(SNAPSHOT)
        second["observations"].append(copy.deepcopy(OBSERVATION))
        second["observations"][1]["health"]["status"] = "invalid"
        self.assert_rejected(c.DecisionSnapshot.parse, second, "enum_violation", "observations[1].health.status")
        bad_versions = copy.deepcopy(SNAPSHOT)
        bad_versions["versions"]["plugin_generations"]["arm"] = True
        self.assert_rejected(c.DecisionSnapshot.parse, bad_versions, "invalid_type", "versions.plugin_generations.arm")
        bad_versions = copy.deepcopy(SNAPSHOT)
        bad_versions["versions"]["environment_generation"] = "env-4"
        self.assert_rejected(c.DecisionSnapshot.parse, bad_versions, "invalid_type", "versions.environment_generation")
        missing_versions = copy.deepcopy(SNAPSHOT)
        del missing_versions["versions"]["ex_session"]
        self.assert_rejected(c.DecisionSnapshot.parse, missing_versions, "missing_field", "versions.ex_session")
        invalid_versions = copy.deepcopy(SNAPSHOT)
        invalid_versions["versions"] = []
        self.assert_rejected(c.DecisionSnapshot.parse, invalid_versions, "invalid_type", "versions")
        missing_observation = copy.deepcopy(SNAPSHOT)
        del missing_observation["observations"][0]["observation_id"]
        self.assert_rejected(c.DecisionSnapshot.parse, missing_observation, "missing_field", "observations[0].observation_id")
        invalid_observation = copy.deepcopy(SNAPSHOT)
        invalid_observation["observations"][0] = []
        self.assert_rejected(c.DecisionSnapshot.parse, invalid_observation, "invalid_type", "observations[0]")
        bad_decision_versions = copy.deepcopy(DECISION)
        bad_decision_versions["versions"]["plugin_generations"]["arm"] = True
        self.assert_rejected(c.BackendDecision.parse, bad_decision_versions, "invalid_type", "versions.plugin_generations.arm")
        invalid_decision = copy.deepcopy(DECISION)
        invalid_decision["schema_version"] = 2
        self.assert_rejected(lambda wire: c.validate_backend_selection(c.DecisionSnapshot.parse(SNAPSHOT), c.BackendDecision.parse(wire), c.VersionSet.parse(VERSIONS)), invalid_decision, "unsupported_schema_version", "schema_version")

    def test_generated_and_authored_selection_match(self):
        authored_snapshot = d.DecisionSnapshot.parse(SNAPSHOT)
        authored_decision = d.BackendDecision.parse(DECISION)
        authored_versions = d.VersionSet.parse(VERSIONS)
        generated = c.validate_backend_selection(c.DecisionSnapshot.parse(authored_snapshot.to_dict()), c.BackendDecision.parse(authored_decision.to_dict()), c.VersionSet.parse(authored_versions.to_dict()))
        authored = d.validate_backend_selection(authored_snapshot, authored_decision, authored_versions)
        self.assertEqual(generated, authored)

    def test_backend_decision_selection_and_distribution(self):
        snapshot = c.DecisionSnapshot.parse(SNAPSHOT)
        decision = c.BackendDecision.parse(DECISION)
        self.assertEqual(decision.to_dict(), DECISION)
        selected = c.validate_backend_selection(snapshot, decision, c.VersionSet.parse(VERSIONS))
        self.assertEqual([item["option_id"] for item in selected], ["arm-start", "base-wait"])
        selected[0]["option_id"] = "altered"
        self.assertEqual(snapshot.owners[0]["candidates"][0]["option_id"], "arm-start")
        for mutation, code in (
            (lambda x: x["choices"][0].update(option_id="base-wait"), "unknown_action"),
            (lambda x: x["choices"][0].update(option_id="arm-blocked"), "unknown_action"),
            (lambda x: x["choices"].pop(), "missing_field"),
            (lambda x: x["choices"][0]["probabilities"].update({"base-wait": 0}), "unknown_action"),
            (lambda x: x["choices"][0]["probabilities"].update({"arm-start": 0.7}), "range_violation"),
        ):
            bad = copy.deepcopy(DECISION); mutation(bad)
            self.assert_rejected(lambda wire: c.validate_backend_selection(snapshot, c.BackendDecision.parse(wire), c.VersionSet.parse(VERSIONS)), bad, code)
        for mutation, code in (
            (lambda x: x["choices"].append(copy.deepcopy(x["choices"][0])), "enum_violation"),
            (lambda x: x["choices"][0].update(params={}), "unknown_field"),
            (lambda x: x.update(elapsed_ms=math.inf), "non_finite_number"),
            (lambda x: x["choices"][0].update(confidence=True), "invalid_type"),
        ):
            bad = copy.deepcopy(DECISION); mutation(bad)
            self.assert_rejected(c.BackendDecision.parse, bad, code)
        stale = copy.deepcopy(VERSIONS); stale["environment_generation"] += 1
        self.assert_rejected(lambda value: c.validate_backend_selection(snapshot, decision, c.VersionSet.parse(value)), stale, "revision_conflict")
        bad = copy.deepcopy(DECISION); bad["snapshot_id"] = "other"
        self.assert_rejected(lambda value: c.validate_backend_selection(snapshot, c.BackendDecision.parse(value), c.VersionSet.parse(VERSIONS)), bad, "revision_conflict")

    def test_documented_examples_parse(self):
        document = (Path(__file__).resolve().parents[1] / "docs" / "DECISION-CONTRACT.md").read_text(encoding="utf-8")
        section = document.split("The next four examples are parsed", 1)[1].split("## Planned Runtime Ownership", 1)[0]
        blocks = re.findall(r"```json\s*\n(.*?)\n```", section, flags=re.DOTALL)
        self.assertEqual(len(blocks), 4)
        observation, versions, snapshot, decision = [json.loads(block) for block in blocks]
        self.assertEqual(c.ObservationEnvelope.parse(observation).to_dict(), observation)
        self.assertEqual(c.VersionSet.parse(versions).to_dict(), versions)
        self.assertEqual(c.DecisionSnapshot.parse(snapshot).to_dict()["snapshot_id"], snapshot["snapshot_id"])
        self.assertEqual(c.BackendDecision.parse(decision).to_dict(), decision)
        selected = c.validate_backend_selection(c.DecisionSnapshot.parse(snapshot), c.BackendDecision.parse(decision), c.VersionSet.parse(versions))
        self.assertEqual(selected[0]["option_id"], "opt-pick")

    def test_budget_nonfinite_and_cycle(self):
        for field, parser, payload in (
            ("data", c.ObservationEnvelope.parse, OBSERVATION),
            ("goal", c.DecisionSnapshot.parse, SNAPSHOT),
        ):
            bad = copy.deepcopy(payload); bad[field] = {"blob": "x" * 1_048_576}
            self.assert_rejected(parser, bad, "value_budget_exceeded")
            bad = copy.deepcopy(payload); bad[field] = {"cycle": None}; bad[field]["cycle"] = bad[field]
            self.assert_rejected(parser, bad, "invalid_type")
        bad = copy.deepcopy(DECISION); bad["choices"][0]["confidence"] = math.nan
        self.assert_rejected(c.BackendDecision.parse, bad, "non_finite_number")
        self.assertEqual(c.VersionSet.parse(VERSIONS).to_dict(), d.VersionSet.parse(VERSIONS).to_dict())


if __name__ == "__main__":
    unittest.main()
