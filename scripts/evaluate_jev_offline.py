"""Synthetic offline scoring only: no network, keys, model SDK or execution API."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astrbot_ex.core.decision.models import DecisionSnapshot

DATASET_VERSION = "b07-offline-v1"
SEED = 20260930
FROZEN_CORPUS_SHA256 = "fba8d87b04399fd644cddd96cad1ac08181baf695de5d2a03fe17db811ad9936"
FIXTURE_DIR = ROOT / "tests/fixtures/decision/jev"
COUNTS = {"normal": 40, "ambiguous": 20, "stale": 10, "conflict_cancel": 15, "missing_failure": 15}
HOLDOUT_COUNTS = {"normal": 8, "ambiguous": 4, "stale": 2, "conflict_cancel": 3, "missing_failure": 3}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("non-finite JSON")
    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)


def load_corpus(directory=FIXTURE_DIR):
    content = (directory / "offline-v1.jsonl").read_bytes()
    if len(content) > 2097152 or hashlib.sha256(content).hexdigest() != FROZEN_CORPUS_SHA256:
        raise ValueError("frozen corpus hash mismatch or size overflow")
    manifest = strict_json((directory / "offline-v1.manifest.json").read_text(encoding="utf-8"))
    if (manifest["dataset_version"] != DATASET_VERSION or manifest["seed"] != SEED or
        manifest["corpus_sha256"] != FROZEN_CORPUS_SHA256 or manifest["scene_count"] != 100 or
        manifest["category_counts"] != COUNTS or manifest["holdout_category_counts"] != HOLDOUT_COUNTS or
        manifest["holdout_count"] != 20 or manifest["holdout_frozen_before_evaluation"] is not True):
        raise ValueError("frozen manifest mismatch")
    scenes = [strict_json(line) for line in content.decode("utf-8").splitlines()]
    if len(scenes) != 100 or len({s["scene_id"] for s in scenes}) != 100:
        raise ValueError("scene count or uniqueness mismatch")
    if dict(Counter(s["category"] for s in scenes)) != COUNTS:
        raise ValueError("category count mismatch")
    if dict(Counter(s["category"] for s in scenes if s["split"] == "holdout")) != HOLDOUT_COUNTS:
        raise ValueError("holdout category count mismatch")
    if manifest["scene_sha256"] != {s["scene_id"]: digest(s) for s in scenes}:
        raise ValueError("scene hash mismatch")
    for split in ("development", "holdout"):
        if manifest["split_sha256"][split] != digest([s for s in scenes if s["split"] == split]):
            raise ValueError("split hash mismatch")
    for scene in scenes:
        if scene["split"] not in ("development", "holdout") or digest(scene["snapshot"]) != scene["snapshot_sha256"]:
            raise ValueError("snapshot hash or split mismatch")
        snap = DecisionSnapshot.parse(scene["snapshot"]).to_dict()
        owners = {o["owner"]: o for o in snap["owners"]}
        labels = scene["labels"]
        if any(set(labels[key]) != set(owners) for key in ("reasonable_options", "bad_options")):
            raise ValueError("label owners mismatch")
        if not set(labels["conservative_owners"]) <= set(owners):
            raise ValueError("unknown conservative owner")
        if not labels["acceptable_joint_choices"]:
            raise ValueError("missing joint labels")
        for owner, record in owners.items():
            eligible = {c["option_id"] for c in record["candidates"] if c["eligible"]}
            reasonable, bad = labels["reasonable_options"][owner], labels["bad_options"][owner]
            if not reasonable or not set(reasonable) <= eligible or not set(bad) <= eligible or set(reasonable) & set(bad):
                raise ValueError("invalid option labels")
        for joint in labels["acceptable_joint_choices"]:
            if set(joint) != set(owners) or any(joint[o] not in labels["reasonable_options"][o] for o in owners):
                raise ValueError("invalid joint labels")
            if resource_conflicts(snap, joint):
                raise ValueError("acceptable label has resource conflict")
    return scenes, manifest


def facts(snapshot):
    return snapshot["observations"][0]["data"]["offline_facts"]


def eligible_by_kind(owner):
    return {c["kind"]: c for c in sorted(owner["candidates"], key=lambda c: c["option_id"]) if c["eligible"]}


def rule(snapshot):
    """Hand-authored deterministic synthetic baseline, not a production gate."""
    context = facts(snapshot)
    used = set(context["occupied_resources"])
    selected = {}
    for owner in sorted(snapshot["owners"], key=lambda o: o["owner"]):
        options = eligible_by_kind(owner)
        wait = options.get("wait") or options["request_replan"]
        choice = wait
        if owner["owner"] == "arm":
            wanted = context["desired_operation"]
            if context["cancel_requested"] and "cancel" in options:
                choice = options["cancel"]
            elif (not context["ambiguous"] and context["freshness_verified"] and context["binding_verified"] and
                  owner["status"] != "fault"):
                candidate = options.get(wanted)
                if candidate is not None:
                    action = candidate.get("action_id")
                    parameters_present = wanted != "start" or bool(snapshot["goal"]["parameters"].get(action))
                    required = set(context["action_resources"].get(action, [])) if wanted in ("start", "keep", "resume") else set()
                    if parameters_present and not required & used:
                        choice = candidate
                        used.update(required)
        selected[owner["owner"]] = choice["option_id"]
    return selected


def stub_llm(snapshot):
    """Deliberately naive offline script. NO LLM/provider/model call occurs.

    Ignores binding/freshness/resources and fails on several authored cases to
    exercise scoring. It never receives category, split, rationale or answers.
    """
    context = facts(snapshot)
    selected = {}
    for owner in snapshot["owners"]:
        options = eligible_by_kind(owner)
        if context["ambiguous"]:
            chosen = options.get("request_replan") or options["wait"]
        elif owner["owner"] == "arm":
            desired = "keep" if context["cancel_requested"] else context["desired_operation"]
            chosen = options.get(desired) or options["wait"]
        elif context["action_resources"]["base.move.v1"] == ["workspace"]:
            chosen = options.get("start") or options["wait"]
        else:
            chosen = options["wait"]
        selected[owner["owner"]] = chosen["option_id"]
    return selected


SELECTORS = {"rule": rule, "stub_llm": stub_llm}
SELECTOR_VERSIONS = {"rule": "offline-rule-v1", "stub_llm": "offline-script-stub-v1-not-an-llm"}


def resource_conflicts(snapshot, choices):
    """Offline oracle for simulated occupancy, not B04 resource arbitration."""
    context = facts(snapshot)
    claimed = {resource: ["preoccupied"] for resource in context["occupied_resources"]}
    for owner in snapshot["owners"]:
        selected = choices.get(owner["owner"])
        candidate = next((c for c in owner["candidates"] if c["option_id"] == selected), None)
        if candidate is None or candidate["kind"] not in ("start", "keep", "resume"):
            continue
        for resource in context["action_resources"].get(candidate.get("action_id"), []):
            claimed.setdefault(resource, []).append(owner["owner"])
    return sorted(resource for resource, claimants in claimed.items() if len(claimants) > 1)


def score_scene(scene, choices, elapsed_ms, selector_error=None):
    if type(elapsed_ms) not in (int, float) or not math.isfinite(elapsed_ms) or elapsed_ms < 0:
        raise ValueError("invalid latency")
    snap, labels = scene["snapshot"], scene["labels"]
    owners = {o["owner"]: o for o in snap["owners"]}
    output = choices if isinstance(choices, dict) else {}
    invalid = selector_error is not None or not isinstance(choices, dict) or set(output) != set(owners)
    option_matches = 0
    bad_choices = 0
    conservative_matches = 0
    for owner, record in owners.items():
        option = output.get(owner)
        candidate = next((c for c in record["candidates"] if c["option_id"] == option), None) if isinstance(option, str) else None
        legal = candidate is not None and candidate["eligible"]
        invalid = invalid or not legal
        bad_choices += int(not legal or option in labels["bad_options"][owner])
        option_matches += int(legal and option in labels["reasonable_options"][owner])
        if owner in labels["conservative_owners"]:
            conservative_matches += int(legal and candidate["kind"] in ("wait", "request_replan") and option in labels["reasonable_options"][owner])
    conflicts = resource_conflicts(snap, output)
    joint_match = not invalid and output in labels["acceptable_joint_choices"] and not conflicts
    return {
        "scene_id": scene["scene_id"], "split": scene["split"], "category": scene["category"],
        "choices": copy.deepcopy(output) if all(isinstance(k, str) and isinstance(v, str) for k, v in output.items()) else {},
        "joint_label_match": joint_match, "owner_option_matches": option_matches, "owner_count": len(owners),
        "conservative_matches": conservative_matches, "conservative_opportunities": len(labels["conservative_owners"]),
        "bad_choice_count": bad_choices, "invalid_output": invalid, "selector_error": selector_error,
        "resource_conflicts": conflicts, "elapsed_ms": elapsed_ms,
    }


def percentile(values, percent):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)] if ordered else None


def summary(rows):
    def ratio(num, den):
        return num / den if den else None
    matches = sum(int(r["joint_label_match"]) for r in rows)
    options = sum(r["owner_count"] for r in rows)
    option_matches = sum(r["owner_option_matches"] for r in rows)
    conservative = sum(r["conservative_opportunities"] for r in rows)
    conservative_matches = sum(r["conservative_matches"] for r in rows)
    times = [r["elapsed_ms"] for r in rows]
    return {"scene_runs": len(rows), "unique_scenes": len({r["scene_id"] for r in rows}),
            "joint_label_matches": matches, "joint_label_match_rate": ratio(matches, len(rows)),
            "owner_option_matches": option_matches, "owner_choices": options,
            "owner_option_match_rate": ratio(option_matches, options),
            "conservative_matches": conservative_matches, "conservative_opportunities": conservative,
            "conservative_match_rate": ratio(conservative_matches, conservative),
            "bad_choice_count": sum(r["bad_choice_count"] for r in rows),
            "bad_selection_scene_runs": sum(int(r["bad_choice_count"] > 0) for r in rows),
            "invalid_output_scene_runs": sum(int(r["invalid_output"]) for r in rows),
            "resource_conflict_scene_runs": sum(int(bool(r["resource_conflicts"])) for r in rows),
            "selector_error_scene_runs": sum(int(r["selector_error"] is not None) for r in rows),
            "latency_ms": {"scope": "local_selector_only_not_cloud", "count": len(times),
                           "p50": percentile(times, 50), "p95": percentile(times, 95), "max": max(times) if times else None}}


def evaluate(scenes, manifest, repeats=3, selectors=None):
    if type(repeats) is not int or not 3 <= repeats <= 10:
        raise ValueError("repeats must be 3..10")
    selector_map = SELECTORS if selectors is None else selectors
    runs = []
    for name, selector in selector_map.items():
        for repeat in range(1, repeats + 1):
            rows = []
            for scene in scenes:
                # Isolated parser-backed copy; never give selector the scene,
                # category, labels, split or manifest. No expected-answer input.
                selector_input = DecisionSnapshot.parse(scene["snapshot"]).to_dict()
                started = time.perf_counter_ns()
                error = None
                try:
                    choices = selector(selector_input)
                except Exception:
                    choices = None
                    error = "selector_failed"
                elapsed = (time.perf_counter_ns() - started) / 1000000
                rows.append(score_scene(scene, choices, elapsed, error))
            runs.append({"selector": name, "repeat": repeat, "rows": rows})
    groups = {}
    for name in selector_map:
        rows = [row for run in runs if run["selector"] == name for row in run["rows"]]
        groups[name] = {split: summary([r for r in rows if r["split"] == split]) for split in ("development", "holdout")}
        groups[name]["by_category"] = {category: summary([r for r in rows if r["category"] == category]) for category in COUNTS}
        groups[name]["by_split_and_category"] = {
            split: {category: summary([r for r in rows if r["split"] == split and r["category"] == category]) for category in COUNTS}
            for split in ("development", "holdout")
        }
        groups[name]["by_repeat_and_split"] = {
            str(run["repeat"]): {split: summary([r for r in run["rows"] if r["split"] == split]) for split in ("development", "holdout")}
            for run in runs if run["selector"] == name
        }
    return {"report_schema_version": 1, "dataset_version": manifest["dataset_version"],
            "corpus_sha256": manifest["corpus_sha256"], "split_sha256": manifest["split_sha256"],
            "seed": manifest["seed"], "repeats": repeats, "network_requests": 0,
            "secret_reads": 0, "real_jev_measured": False, "real_llm_measured": False,
            "execute_authorized": False, "actual_bill": None, "token_usage": None,
            "scope": "synthetic_offline_scoring_not_model_quality_or_execution_safety",
            "selector_versions": {name: SELECTOR_VERSIONS.get(name, "test_injected_selector") for name in selector_map},
            "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "summaries": groups, "runs": runs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    scenes, manifest = load_corpus()
    report = evaluate(scenes, manifest, options.repeats)
    text = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if options.output is None:
        print(text, end="")
    else:
        # Never overwrite an earlier benchmark report.
        with options.output.open("x", encoding="utf-8") as stream:
            stream.write(text)
        print(json.dumps({"output": str(options.output), "corpus_sha256": report["corpus_sha256"],
                          "network_requests": 0, "repeats": options.repeats,
                          "summaries": report["summaries"]}, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
