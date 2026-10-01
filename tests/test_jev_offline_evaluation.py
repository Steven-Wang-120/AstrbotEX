from __future__ import annotations

import copy
import importlib.util
import json
import math
import os
import socket
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("b07_offline", ROOT / "scripts/evaluate_jev_offline.py")
offline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(offline)


class OfflineEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scenes, cls.manifest = offline.load_corpus()

    def scene(self, category=None, subtype=None, split=None):
        return copy.deepcopy(next(s for s in self.scenes if
                                  (category is None or s["category"] == category) and
                                  (subtype is None or s["subtype"] == subtype) and
                                  (split is None or s["split"] == split)))

    def test_frozen_exact_counts_splits_valid_snapshots_and_multiple_labels(self):
        self.assertEqual(len(self.scenes), 100)
        self.assertEqual(Counter(s["category"] for s in self.scenes), offline.COUNTS)
        self.assertEqual(Counter(s["split"] for s in self.scenes), {"development": 80, "holdout": 20})
        self.assertEqual(Counter(s["category"] for s in self.scenes if s["split"] == "holdout"), offline.HOLDOUT_COUNTS)
        self.assertEqual(len({s["snapshot_sha256"] for s in self.scenes}), 100)
        ids = {split: {s["scene_id"] for s in self.scenes if s["split"] == split} for split in ("development", "holdout")}
        self.assertFalse(ids["development"] & ids["holdout"])
        self.assertTrue(any(len(s["labels"]["acceptable_joint_choices"]) > 1 for s in self.scenes if s["split"] == "holdout"))
        for scene in self.scenes:
            with self.subTest(scene=scene["scene_id"]):
                for acceptable in scene["labels"]["acceptable_joint_choices"]:
                    score = offline.score_scene(scene, acceptable, 0)
                    self.assertTrue(score["joint_label_match"])
                    self.assertFalse(score["invalid_output"])
                    self.assertEqual(score["bad_choice_count"], 0)

    def test_rule_and_stub_llm_three_repeats_same_snapshots_and_separate_holdout(self):
        before = copy.deepcopy(self.scenes)
        seen = {"rule": [], "stub_llm": []}
        def spy(name):
            def select(snapshot):
                self.assertEqual(set(snapshot), {"schema_version", "snapshot_id", "created_monotonic_ns", "versions", "goal", "observations", "owners"})
                self.assertNotIn("labels", snapshot)
                self.assertNotIn("expected", snapshot)
                self.assertNotIn("split", snapshot)
                self.assertNotIn("category", snapshot)
                seen[name].append(offline.digest(snapshot))
                return offline.SELECTORS[name](snapshot)
            return select
        report = offline.evaluate(self.scenes, self.manifest, selectors={name: spy(name) for name in seen})
        self.assertEqual(seen["rule"], seen["stub_llm"])
        self.assertEqual(len(seen["rule"]), 300)
        self.assertEqual(self.scenes, before)
        for name in seen:
            scores = report["summaries"][name]
            self.assertEqual(scores["development"]["scene_runs"], 240)
            self.assertEqual(scores["holdout"]["scene_runs"], 60)
            self.assertEqual(scores["development"]["unique_scenes"], 80)
            self.assertEqual(scores["holdout"]["unique_scenes"], 20)
            for repeat in range(1, 4):
                run = next(r for r in report["runs"] if r["selector"] == name and r["repeat"] == repeat)
                self.assertEqual(len(run["rows"]), 100)
                self.assertEqual({r["scene_id"] for r in run["rows"]}, {s["scene_id"] for s in self.scenes})
        rule = report["summaries"]["rule"]
        self.assertEqual(rule["development"]["joint_label_match_rate"], 1)
        self.assertEqual(rule["holdout"]["joint_label_match_rate"], 1)
        self.assertEqual(rule["holdout"]["resource_conflict_scene_runs"], 0)
        stub = report["summaries"]["stub_llm"]
        self.assertLess(stub["holdout"]["joint_label_match_rate"], 1)
        self.assertGreater(stub["holdout"]["bad_choice_count"], 0)
        self.assertEqual(stub["by_category"]["normal"]["joint_label_matches"], 120)
        # The naive stub replans BOTH owners on ambiguity, although the bound
        # goal only permits arm clarification with base waiting. Preserve that
        # deliberately wrong selector rather than tuning after seeing holdout.
        self.assertEqual(stub["by_category"]["ambiguous"]["joint_label_matches"], 0)
        self.assertEqual(stub["by_category"]["stale"]["joint_label_matches"], 0)
        self.assertEqual(stub["by_category"]["conflict_cancel"]["resource_conflict_scene_runs"], 30)
        for name in seen:
            runs = [r for r in report["runs"] if r["selector"] == name]
            signatures = [[(r["scene_id"], r["choices"], r["joint_label_match"]) for r in run["rows"]] for run in runs]
            self.assertEqual(signatures[0], signatures[1])
            self.assertEqual(signatures[1], signatures[2])
        self.assertFalse(report["real_jev_measured"])
        self.assertFalse(report["real_llm_measured"])
        self.assertFalse(report["execute_authorized"])
        self.assertIsNone(report["actual_bill"])
        self.assertIsNone(report["token_usage"])
        json.dumps(report, allow_nan=False)

    def test_labels_cannot_change_selector_inputs_or_choices(self):
        scene = self.scene(category="normal", subtype="pick")
        original = offline.evaluate([scene], self.manifest)
        poisoned = copy.deepcopy(scene)
        owners = {o["owner"]: o for o in scene["snapshot"]["owners"]}
        waits = {owner: next(c["option_id"] for c in record["candidates"] if c["kind"] == "wait") for owner, record in owners.items()}
        poisoned["labels"]["acceptable_joint_choices"] = [waits]
        poisoned["labels"]["reasonable_options"] = {owner: [option] for owner, option in waits.items()}
        changed = offline.evaluate([poisoned], self.manifest)
        for a, b in zip(original["runs"], changed["runs"]):
            self.assertEqual(a["rows"][0]["choices"], b["rows"][0]["choices"])
            self.assertTrue(a["rows"][0]["joint_label_match"])
            self.assertFalse(b["rows"][0]["joint_label_match"])

    def test_multi_reasonable_and_joint_resource_conflict_are_distinct(self):
        scene = self.scene(category="conflict_cancel", subtype="shared_resource")
        first, second = scene["labels"]["acceptable_joint_choices"]
        self.assertTrue(offline.score_scene(scene, first, 1)["joint_label_match"])
        self.assertTrue(offline.score_scene(scene, second, 1)["joint_label_match"])
        incompatible = {"arm": first["arm"], "base": second["base"]}
        scored = offline.score_scene(scene, incompatible, 1)
        self.assertEqual(scored["owner_option_matches"], 2)
        self.assertEqual(scored["resource_conflicts"], ["workspace"])
        self.assertFalse(scored["joint_label_match"])
        self.assertEqual(scored["bad_choice_count"], 0)  # individual options legal, joint pair wrong
        conservative = self.scene(category="ambiguous", split="holdout")
        for joint in conservative["labels"]["acceptable_joint_choices"]:
            scored = offline.score_scene(conservative, joint, 0)
            self.assertTrue(scored["joint_label_match"])
            self.assertEqual(scored["conservative_matches"], 1)

    def test_wrong_but_legal_bad_selection_and_invalid_output_scoring(self):
        scene = self.scene(category="stale")
        bad = offline.stub_llm(scene["snapshot"])
        result = offline.score_scene(scene, bad, 0.1)
        self.assertFalse(result["joint_label_match"])
        self.assertFalse(result["invalid_output"])
        self.assertEqual(result["bad_choice_count"], 1)
        self.assertEqual(result["conservative_matches"], 0)
        good = scene["labels"]["acceptable_joint_choices"][0]
        for choices in (None, [], {}, {**good, "evil": "invented"}, {**good, "arm": "unknown"}, {**good, "arm": True}):
            with self.subTest(choices=choices):
                result = offline.score_scene(scene, choices, 0)
                self.assertTrue(result["invalid_output"])
                self.assertFalse(result["joint_label_match"])
        blocked = next(c for o in scene["snapshot"]["owners"] if o["owner"] == "arm" for c in o["candidates"] if not c["eligible"])
        result = offline.score_scene(scene, {**good, "arm": blocked["option_id"]}, 0)
        self.assertTrue(result["invalid_output"])
        self.assertEqual(result["bad_choice_count"], 1)

    def test_holdout_scores_do_not_blend_with_development(self):
        dev = self.scene(category="normal", subtype="pick", split="development")
        hold = self.scene(category="stale", split="holdout")
        report = offline.evaluate([dev, hold], self.manifest, selectors={"stub_llm": offline.stub_llm})
        scores = report["summaries"]["stub_llm"]
        self.assertEqual(scores["development"]["joint_label_match_rate"], 1)
        self.assertEqual(scores["holdout"]["joint_label_match_rate"], 0)
        self.assertEqual(scores["development"]["scene_runs"], 3)
        self.assertEqual(scores["holdout"]["scene_runs"], 3)
        self.assertEqual(scores["holdout"]["conservative_match_rate"], 0)

    def test_no_network_or_secret_reads_and_selector_errors_are_recorded(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
             patch("os.getenv", side_effect=AssertionError("secret reads forbidden")), \
             patch.object(os._Environ, "__getitem__", side_effect=AssertionError("env reads forbidden")):
            scenes, manifest = offline.load_corpus()
            report = offline.evaluate(scenes, manifest)
            self.assertEqual(report["network_requests"], 0)
            self.assertEqual(report["secret_reads"], 0)
        def fail(snapshot):
            raise RuntimeError("private untrusted diagnostic")
        report = offline.evaluate([self.scene(category="normal")], self.manifest, selectors={"fails": fail})
        runs = report["summaries"]["fails"]["by_category"]["normal"]
        self.assertEqual(runs["selector_error_scene_runs"], 3)
        self.assertEqual(runs["invalid_output_scene_runs"], 3)
        self.assertNotIn("private untrusted diagnostic", json.dumps(report))

    def test_latency_nearest_rank_and_empty_denominators_are_explicit(self):
        scene = self.scene(category="normal")
        good = scene["labels"]["acceptable_joint_choices"][0]
        rows = [offline.score_scene(scene, good, value) for value in range(1, 21)]
        measured = offline.summary(rows)
        self.assertEqual(measured["latency_ms"], {"scope": "local_selector_only_not_cloud", "count": 20, "p50": 10, "p95": 19, "max": 20})
        self.assertIsNone(measured["conservative_match_rate"])
        self.assertIsNone(offline.summary([])["joint_label_match_rate"])
        for invalid in (True, -1, math.nan, math.inf):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                offline.score_scene(scene, good, invalid)
        for repeat in (1, 2, 11, True):
            with self.subTest(repeat=repeat), self.assertRaises(ValueError):
                offline.evaluate([], self.manifest, repeat)

    def test_frozen_hash_and_split_tampering_rejected_without_relabeling(self):
        with tempfile.TemporaryDirectory(prefix="b07-offline-") as temporary:
            directory = Path(temporary)
            corpus = (offline.FIXTURE_DIR / "offline-v1.jsonl").read_bytes()
            manifest = (offline.FIXTURE_DIR / "offline-v1.manifest.json").read_bytes()
            (directory / "offline-v1.jsonl").write_bytes(corpus + b" ")
            (directory / "offline-v1.manifest.json").write_bytes(manifest)
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                offline.load_corpus(directory)
            (directory / "offline-v1.jsonl").write_bytes(corpus)
            altered = json.loads(manifest)
            altered["holdout_category_counts"]["normal"] = 0
            (directory / "offline-v1.manifest.json").write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest mismatch"):
                offline.load_corpus(directory)
            altered = json.loads(manifest)
            altered["split_sha256"]["holdout"] = "invented"
            (directory / "offline-v1.manifest.json").write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "split hash mismatch"):
                offline.load_corpus(directory)

    def test_cli_has_no_real_mode_and_refuses_report_overwrite(self):
        with patch("sys.argv", ["evaluate_jev_offline.py", "--real"]), patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                offline.main()
            self.assertEqual(caught.exception.code, 2)
        with tempfile.TemporaryDirectory(prefix="b07-report-") as temporary:
            target = Path(temporary) / "offline-report.json"
            with patch("sys.argv", ["evaluate_jev_offline.py", "--output", str(target)]), patch("builtins.print"):
                offline.main()
                saved = target.read_bytes()
                with self.assertRaises(FileExistsError):
                    offline.main()
                self.assertEqual(target.read_bytes(), saved)
            report = json.loads(saved)
            self.assertEqual(report["repeats"], 3)
            self.assertIn("not-an-llm", report["selector_versions"]["stub_llm"])


if __name__ == "__main__":
    unittest.main()
