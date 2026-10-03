"""Explicit real Laya validation: fixed weights, loopback service, test Actors only."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

from astrbot_ex.core.actions.models import ActionStatus
from astrbot_ex.core.actions.ledger import OwnerBinding
from astrbot_ex.core.decision.models import DecisionSnapshot, validate_backend_selection
from astrbot_ex.core.decision.backends.laya import LayaBackend, LayaConfig, LayaBackendError
from astrbot_ex.core.decision.backends.registry import create_backend
from astrbot_ex.core.decision.backends import MockBackend
from astrbot_ex.core.decision.service import DecisionService
from astrbot_ex.core.decision.owned_laya import OwnedServer
from astrbot_ex.core.actions.service import ActionService
from astrbot_ex.core.plugin_actor import PluginActor
from tests.test_decision_service import DecisionServiceTests, ActionOwner, wait_for
from tests.test_goal_manager import make_catalog, goal_payload

CODE_REVISION = "6d942c92081fbc139e736bbd9ac0023223c29b7f"
WEIGHT_REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
SCENARIOS = (
    {"id": "move", "goal": "Move safely.", "expected_kinds": ["start"]},
    {"id": "wait", "goal": "Wait without moving until a new instruction arrives.", "expected_kinds": ["wait"]},
    {"id": "replan", "goal": "Ask AEB for new goal parameters before moving.", "expected_kinds": ["request_replan"]},
)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n")


def stats(values):
    if not values:
        return {"n": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    values = sorted(values)
    def percentile(p):
        import math
        return values[max(0, math.ceil(p * len(values)) - 1)]
    return {"n": len(values), "p50_ms": percentile(.5), "p95_ms": percentile(.95), "max_ms": values[-1]}


def snapshot(scene, sample):
    sid = f"real-{scene['id']}-{sample}"
    return DecisionSnapshot.parse({"schema_version": 1, "snapshot_id": sid,
        "created_monotonic_ns": time.monotonic_ns(),
        "versions": {"ex_session": "laya-real-validation", "goal_revision": 1,
                     "config_revision": 1, "catalog_revision": 1,
                     "environment_generation": 1, "gate_epoch": 1,
                     "plugin_generations": {"arm": 1}},
        "goal": {"task_id": "laya-real-task", "goal_id": sid,
                 "goal_text_en": scene["goal"], "allowed_actions": ["arm.move.v1"],
                 "parameters": {"arm.move.v1": {"meters": 1}}},
        "observations": [], "owners": [{"owner": "arm", "plugin_generation": 1,
        "status": "available", "candidates": [
            {"option_id": sid + ":wait", "kind": "wait", "description": "Wait without dispatch or extending an action lease.", "eligible": True},
            {"option_id": sid + ":replan", "kind": "request_replan", "description": "Ask AEB to supply new goal parameters; never choose another goal.", "eligible": True},
            {"option_id": sid + ":start", "kind": "start", "description": "Move safely", "action_id": "arm.move.v1", "eligible": True},
        ]}]})


def run_integration(config, output):
    """Reuse existing Ledger/Actor fixtures with the unmodified production 5 Hz default."""
    fixture = DecisionServiceTests()
    fixture.setUp()
    catalog = make_catalog()
    owner = ActionOwner("arm", fixture.dispatcher)
    actor = PluginActor(owner)
    actor.start()
    fixture.plugins["arm"], fixture.actors["arm"] = owner, actor
    manifest = catalog.snapshot().entries[0]["manifest"]
    fixture.dispatcher.register_owner(OwnerBinding("arm", 1), actor, manifest)
    fixture.actions = ActionService(fixture.ledger, fixture.dispatcher, catalog, stop_timeout=.12)
    fixture.actions.control_mode = "decision"
    fixture.actions.update_versions(runtime_state="running")
    service = fixture.service = DecisionService(fixture.actions, backend=MockBackend())
    backend = create_backend("laya", config=config, allow_test_execution=True)
    accepted_ns, request_records = {}, {}
    original_decide = backend.decide
    def traced_decide(frozen):
        started = time.monotonic_ns()
        decision, error = None, None
        try:
            decision = original_decide(frozen)
            return decision  # The real validated model result is unchanged.
        except LayaBackendError as exc:
            error = exc.code
            raise
        finally:
            last_record = backend.last_record
            request_records[frozen.snapshot_id] = {"snapshot": frozen.to_dict(),
                "decision": decision.to_dict() if decision is not None else None,
                "error_code": error,
                "record": last_record if last_record and last_record["snapshot_id"] == frozen.snapshot_id else None,
                "backend_started_monotonic_ns": started,
                "backend_returned_monotonic_ns": time.monotonic_ns()}
    backend.decide = traced_decide
    original_command = owner.on_action_command
    def command_callback(command):
        result = original_command(command)
        accepted_ns[command.command_id] = time.monotonic_ns()  # Callback accepted receipt, before durable ACK.
        fixture.dispatcher.report(command.command_id, OwnerBinding("arm", 1), ActionStatus.RUNNING,
                                  details={"test_actor_only": True})
        return result
    owner.on_action_command = command_callback
    result = {"dispatch_hz": 5, "test_actor_only": True, "cases": []}
    try:
        if not wait_for(lambda: service._last_catalog_revision == catalog.snapshot().revision):
            raise RuntimeError("composition_catalog_not_ready")
        result["replacement"] = service.replace_backend("laya", lambda: backend)
        if not wait_for(lambda: service.status()["stop"]["state"] == "proven"):
            raise RuntimeError("replacement_stop_not_proven")
        service.set_mode("shadow")
        service.submit_goal(goal_payload(service.goals, 1, goal_text_en=SCENARIOS[0]["goal"]))
        if not wait_for(lambda: any(d["outcome"] == "shadow" for d in service.status()["decisions"]), timeout=3):
            raise RuntimeError("real_shadow_no_valid_decision")
        result["cases"].append({"id": "shadow", "record": backend.last_record,
                                "status": service.status(), "commands": len(owner.commands),
                                "ledger": [asdict(row) for row in fixture.ledger.list_commands().result(1)]})
        if owner.commands or fixture.ledger.list_commands().result(1):
            raise RuntimeError("shadow_had_action_side_effect")
        service.set_mode("execute")
        if not wait_for(lambda: service.status()["stop"]["state"] == "proven"):
            raise RuntimeError("mode_change_stop_not_proven")
        for trial in range(5):
            owner.started.clear()
            before = len(owner.commands)
            submitted = time.monotonic_ns()
            parameters = {"arm.move.v1": {"meters": 1}}
            service.submit_goal(goal_payload(service.goals, trial + 2,
                goal_text_en=SCENARIOS[0]["goal"], parameters=parameters,
                completion={"required_success_actions": ["arm.move.v1"]}))
            if not owner.started.wait(3):
                result["cases"].append({"id": "execute", "trial": trial, "pass": False,
                    "reason": "model_did_not_start", "record": backend.last_record,
                    "status": service.status()})
            else:
                command = owner.commands[before]
                row = fixture.dispatcher.report(command.command_id, OwnerBinding("arm", 1),
                    ActionStatus.SUCCEEDED, details={"test_actor_only": True}).result(1)
                trace = request_records[command.decision_id]
                chosen_snapshot = trace["snapshot"]
                accepted = accepted_ns[command.command_id]
                result["cases"].append({"id": "execute", "trial": trial,
                    "pass": row.status == ActionStatus.SUCCEEDED,
                    "command": command.to_dict(), "ledger": asdict(row), **trace,
                    "goal_submitted_monotonic_ns": submitted,
                    "actor_accepted_monotonic_ns": accepted,
                    "snapshot_to_actor_ms": (accepted - chosen_snapshot["created_monotonic_ns"]) / 1e6,
                    "goal_to_snapshot_ms": (chosen_snapshot["created_monotonic_ns"] - submitted) / 1e6,
                    "snapshot_to_backend_ms": (trace["backend_started_monotonic_ns"] - chosen_snapshot["created_monotonic_ns"]) / 1e6,
                    "backend_to_actor_ms": (accepted - trace["backend_returned_monotonic_ns"]) / 1e6})
            receipt = service.request_stop("real_validation_stop")
            if not wait_for(lambda: service.status()["stop"]["operation_id"] == receipt["operation_id"]
                            and service.status()["stop"]["state"] == "proven"):
                raise RuntimeError("real_validation_stop_not_proven")
            result["cases"][-1]["stop"] = {"receipt": receipt, "final": service.status()["stop"]}
            if backend.status()["restart_required"]:
                raise RuntimeError("owned_service_restart_required_no_automatic_recovery")
        result["snapshot_to_actor_latency"] = stats([c["snapshot_to_actor_ms"] for c in result["cases"] if c.get("pass")])
        result["goal_to_snapshot_latency"] = stats([c["goal_to_snapshot_ms"] for c in result["cases"] if c.get("pass")])
        result["backend_to_actor_latency"] = stats([c["backend_to_actor_ms"] for c in result["cases"] if c.get("pass")])
        result["all_real_decisions"] = list(request_records.values())
        result["final_status"] = service.status()
        return result
    finally:
        result["all_real_decisions"] = list(request_records.values())
        result["backend_status_before_cleanup"] = backend.status()
        dump(output / "actor-integration-partial.json", result)
        fixture.tearDown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-real", action="store_true", help="Explicitly start the owned service and call real weights.")
    parser.add_argument("--laya-python", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--repetitions", type=int, default=8)
    args = parser.parse_args()
    if not args.run_real:
        parser.error("--run-real is required; importing or displaying help never starts a model")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("output must be empty; raw previous results are never overwritten")
    if not 1 <= args.repetitions <= 8:
        parser.error("repetitions must be 1..8")
    args.output.mkdir(parents=True, exist_ok=True)
    dump(args.output / "scenarios.json", SCENARIOS)
    server = OwnedServer(args.laya_python, args.model_cache, args.port, args.output, args.device)
    report = {"source_revision": CODE_REVISION, "weight_revision": WEIGHT_REVISION,
              "scenario_sha256": hashlib.sha256(json.dumps(SCENARIOS, sort_keys=True).encode()).hexdigest(),
              "samples": [], "errors": [], "server_history": server.history,
              "actor_timing_boundary": "test callback returned accepted; durable accepted/running/succeeded verified separately in Ledger"}
    exit_code = 1
    try:
        config = server.start(warmup=False)
        # Cold first inference has its own declared budget; hot/EX calls retain 1500ms.
        cold = LayaBackend(replace(config, deadline_ms=30000))
        first_start = time.monotonic_ns()
        try:
            warmup = cold.decide(snapshot(SCENARIOS[0], "warmup"))
            report["warmup"] = {"elapsed_ms": (time.monotonic_ns() - first_start) / 1e6,
                                 "deadline_ms": 30000, "decision": warmup.to_dict(), "record": cold.last_record}
            report["warmup_complete"] = True
        finally:
            if "warmup" not in report:
                report["warmup"] = {"elapsed_ms": (time.monotonic_ns() - first_start) / 1e6,
                                     "deadline_ms": 30000, "record": cold.last_record,
                                     "backend_status": cold.status()}
            cold.close()
        backend = LayaBackend(config)
        try:
            with (args.output / "calls.jsonl").open("w") as log:
                for repeat in range(args.repetitions):
                    for scene in SCENARIOS:
                        item = {"scene_id": scene["id"], "repeat": repeat, "expected_kinds": scene["expected_kinds"]}
                        frozen = snapshot(scene, repeat)
                        started = time.monotonic_ns()
                        try:
                            decision = backend.decide(frozen)
                            selected = validate_backend_selection(frozen, decision, frozen.versions)
                            item.update(decision=decision.to_dict(), selected=selected,
                                        correct=all(c["kind"] in scene["expected_kinds"] for c in selected))
                        except LayaBackendError as exc:
                            item.update(error_code=exc.code, correct=False)
                        item.update(elapsed_ms=(time.monotonic_ns()-started)/1e6,
                                    snapshot=frozen.to_dict(), record=backend.last_record)
                        report["samples"].append(item)
                        log.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
                        log.flush()
                        if backend.status()["restart_required"]:
                            raise RuntimeError("owned_service_restart_required_no_automatic_recovery")
            report["health_after_samples"] = backend.probe()
        finally:
            backend.close()
        report["latency"] = stats([s["elapsed_ms"] for s in report["samples"] if "error_code" not in s])
        report["http_latency"] = stats([s["record"]["http_elapsed_ms"] for s in report["samples"]
                                      if "error_code" not in s and s["record"]["http_elapsed_ms"] is not None])
        report["server_inference_latency"] = stats([s["record"]["server_inference_ms"] for s in report["samples"]
                                                   if "error_code" not in s and s["record"]["server_inference_ms"] is not None])
        report["selection_correct"] = sum(s.get("correct", False) for s in report["samples"])
        report["failure_count"] = sum("error_code" in s for s in report["samples"])
        report["latency_target_p95_100ms_met"] = (report["latency"]["p95_ms"] is not None and report["latency"]["p95_ms"] <= 100)
        report["selection_quality_pass"] = report["selection_correct"] == len(report["samples"]) and report["failure_count"] == 0
        report["integration"] = run_integration(config, args.output)
        report["actor_success_count"] = sum(c["id"] == "execute" and c.get("pass", False) for c in report["integration"]["cases"])
        report["model_to_actor_pass"] = report["actor_success_count"] > 0
        exit_code = 0 if report["model_to_actor_pass"] and report["failure_count"] == 0 else 1
    except Exception as exc:
        report["errors"].append({"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
    finally:
        try:
            server.stop()
        except Exception as exc:
            report["errors"].append({"type": type(exc).__name__, "message": str(exc)})
            exit_code = 1
        report["server_history"] = server.history
        report["exit_code"] = exit_code
        dump(args.output / "result.json", report)
        print(json.dumps({k: report.get(k) for k in ("latency", "selection_correct", "failure_count", "model_to_actor_pass", "errors", "exit_code")}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
