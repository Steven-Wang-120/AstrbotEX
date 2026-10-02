from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from astrbot_ex.core import contracts
from tests.contract_fixture_runner import load_fixture, run_fixtures

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "decision_contracts" / "golden.json"
)


class GoldenFixtureTests(unittest.TestCase):
    """Every B00 §5 case, driven from the shared golden fixture."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = load_fixture(FIXTURE)

    def test_all_cases_pass(self) -> None:
        results = run_fixtures(contracts, self.fixture)
        failures = [r for r in results if not r.ok]
        detail = "\n".join(f"  {r.case_id}: {r.detail}" for r in failures)
        self.assertEqual(failures, [], f"failing contract cases:\n{detail}")

    def test_case_count(self) -> None:
        # Guards against a fixture edit silently dropping existing coverage.
        self.assertGreaterEqual(len(self.fixture["cases"]), 81)

    def test_fixture_matches_generator(self) -> None:
        from scripts.gen_contract_fixtures import build_fixture
        self.assertEqual(self.fixture, build_fixture())

    def test_fixture_is_json_round_trippable(self) -> None:
        # The golden file is strict JSON; only the runner injects non-finite test values.
        text = json.dumps(self.fixture, allow_nan=False)
        self.assertEqual(json.loads(text, parse_constant=lambda value: self.fail(value))["cases"], self.fixture["cases"])


class RequiredCoverageTests(unittest.TestCase):
    """Assert the B00 §5 checklist is actually present, by id."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ids = {case["id"] for case in load_fixture(FIXTURE)["cases"]}

    def test_every_required_scenario_is_covered(self) -> None:
        required = {
            "empty_goal": "S-GOAL-EMPTY",
            "blank_goal": "S-GOAL-BLANK",
            "overlong_text": "S-GOAL-LONG",
            "non_english_marker": "S-GOAL-NON-ENGLISH",
            "non_ascii_proper_noun_allowed": "S-GOAL-PROPER-NOUN",
            "illegal_owner": "S-CMD-OWNER",
            "unknown_action": "S-CMD-UNKNOWN-ACTION",
            "missing_params": "S-CMD-NO-PARAMS",
            "nan": "S-CMD-NAN",
            "infinity": "S-CMD-INF",
            "negative_ttl": "S-CMD-NEG-TTL",
            "duplicate_id_replay": "S-IDEM-REPLAY",
            "duplicate_id_conflict": "S-IDEM-CONFLICT",
            "old_ex_session": "S-CMD-STALE-SESSION",
            "wrong_revision": "S-CMD-BAD-REVISION",
            "illegal_status_jump": "S-ST-SKIP",
            "terminal_cannot_regress": "S-ST-TERMINAL-WINS",
            "cancel_without_support": "S-CMD-CANCEL-UNSUPPORTED",
            "v2_isolated_from_v1": "S-MF-LEGACY",
            "v1_isolated_from_v2": "S-MF-LEGACY-PARSE",
            "unknown_method_no_fallback": "S-METHOD-UNKNOWN",
            "cancel_requires_evidence": "S-CANCEL-EVIDENCE",
            "canceled_event_positive_proof": "S-EVENT-CANCELED",
            "canceled_event_missing_proof": "S-EVENT-NO-STOP",
            "canceled_event_false_proof": "S-EVENT-STOP-FALSE",
            "cancel_timeout_not_faked": "S-CANCEL-TIMEOUT",
        }
        missing = {name: cid for name, cid in required.items() if cid not in self.ids}
        self.assertEqual(missing, {}, f"missing scenarios: {missing}")


class DocumentedExamplesTests(unittest.TestCase):
    def test_goal_command_and_event_examples_parse(self) -> None:
        document = (Path(__file__).resolve().parents[1] / "docs" / "DECISION-CONTRACT.md").read_text(encoding="utf-8")
        section = document.split("## Goal, Command And Event Examples", 1)[1].split("## B00 Observation And Backend Types", 1)[0]
        samples = re.findall(r"```json\s*\n(.*?)\n```", section, flags=re.DOTALL)
        self.assertEqual(len(samples), 3)
        goal, command, event = (json.loads(sample) for sample in samples)
        self.assertEqual(contracts.GoalSubmit.parse(goal).to_dict(), goal)
        self.assertEqual(contracts.ActionCommand.parse(command).to_dict(), command)
        normalized_event = {**event, "reason_code": ""}
        self.assertEqual(contracts.ActionEvent.parse(event).to_dict(), normalized_event)
        feedback = {
            "schema_version": 1,
            **{key: value for key, value in event.items() if key not in ("event_id", "command_id", "owner")},
        }
        self.assertEqual(contracts.Feedback.parse(feedback).to_dict(), {**feedback, "reason_code": ""})


class NoHardwareImportTests(unittest.TestCase):
    """B00: the contract layer must not pull in runtime, ROS or hardware SDKs."""

    def test_contract_modules_import_only_stdlib(self) -> None:
        forbidden = ("rclpy", "zmq", "astrbot", "can", "serial", "numpy", "PySide6")
        import astrbot_ex.core.actions.models as actions_models
        import astrbot_ex.core.decision.models as decision_models

        for module in (contracts, actions_models, decision_models):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for name in forbidden:
                self.assertNotIn(
                    f"import {name}",
                    source,
                    f"{module.__name__} must not import {name}",
                )

    def test_contract_module_has_no_side_effects(self) -> None:
        # Importing must not start a thread, socket, or event loop.
        import threading

        before = threading.active_count()
        # Reload in a fresh interpreter: reloading the live module replaces
        # ContractError/classes already imported by runtime clients.
        import subprocess
        import sys

        probe = "import threading; from astrbot_ex.core import contracts; import importlib; before = threading.active_count(); importlib.reload(contracts); assert threading.active_count() == before"
        result = subprocess.run([sys.executable, "-B", "-c", probe],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(threading.active_count(), before)


if __name__ == "__main__":
    unittest.main()
