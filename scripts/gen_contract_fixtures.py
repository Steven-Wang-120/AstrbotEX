"""Generate the B00 golden contract fixtures.

The fixtures are the shared artefact that proves AstrBotEX and A.E.B parse the
same payload into the same decision. Every case below carries its own accept or
reject reason so a reviewer can check the contract test against the spec.

Run from the AstrBotEX repository root:

    python scripts/gen_contract_fixtures.py

Writes: tests/fixtures/decision_contracts/golden.json

Non-finite test values are represented by explicit fixture-only markers. The
runner injects actual non-finite Python floats after strict JSON decoding;
no wire JSON extension is used in the golden file.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "decision_contracts" / "golden.json"

GOOD_SESSION = "ex-boot-a"

# B00 §2.2 verbatim example.
GOOD_GOAL_SUBMIT = {
    "schema_version": 1,
    "request_id": "req-17",
    "ex_session": GOOD_SESSION,
    "task_id": "task-4",
    "step_id": "step-2",
    "goal_id": "goal-9",
    "expected_revision": 8,
    "goal_text_en": "Pick the selected cup using the arm. Keep the base stopped.",
    "allowed_actions": ["new_arm.pick_selected.v1", "new_yolo.detect.start.v1"],
    "parameters": {
        "new_arm.pick_selected.v1": {
            "target": {
                "observation_id": "obs-40",
                "stream_id": "front",
                "frame_id": 40,
                "object_id": "track-8",
                "selected": True,
            }
        }
    },
    "completion": {"required_success_actions": ["new_arm.pick_selected.v1"]},
    "lease_ms": 10000,
}

# B00 §2.4 verbatim example.
GOOD_ACTION_MANIFEST_V2 = {
    "action_api_version": 2,
    "observation_sources": {"front_clearance": {"topic": "front_sensor.clearance", "max_age_ms": 200, "required_fields": ["distance_m", "valid"]}},
    "actions": [
        {
            "action_id": "new_base.forward.v1",
            "description": (
                "Move forward using the bounded local controller. "
                "Stop when its lease expires."
            ),
            "schema": {
                "type": "object",
                "properties": {"profile": {"type": "string", "enum": ["slow"]}},
                "required": ["profile"],
                "additionalProperties": False,
            },
            "resources": ["base_motion"],
            "operations": ["start", "cancel"],
            "requires_runtime_state": ["running"],
            "requires_observations": ["front_clearance"],
            "max_duration_ms": 1000,
            "cancel_timeout_ms": 300,
            "danger": "high",
        }
    ],
    "observation_guide": "output_description.md",
}

# B00 §2.3 verbatim example.
GOOD_COMMAND = {
    "schema_version": 1,
    "command_id": "cmd-21",
    "ex_session": GOOD_SESSION,
    "goal_id": "goal-9",
    "goal_revision": 9,
    "decision_id": "dec-12",
    "owner": "new_arm",
    "plugin_generation": 3,
    "action_id": "new_arm.pick_selected.v1",
    "operation": "start",
    "params": {
        "target": {"observation_id": "obs-40", "object_id": "track-8", "selected": True}
    },
    "lease_ms": 1000,
}

# A minimal v2 manifest used by command admission cases.
ARM_MANIFEST = {
    "action_api_version": 2,
    "observation_sources": {"front_clearance": {"topic": "front_sensor.clearance", "max_age_ms": 200, "required_fields": ["distance_m", "valid"]}},
    "actions": [
        {
            "action_id": "new_arm.pick_selected.v1",
            "description": "Pick the operator-selected target with the arm.",
            "schema": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "object",
                        "properties": {
                            "observation_id": {"type": "string"},
                            "object_id": {"type": "string"},
                            "selected": {"type": "boolean"},
                            "depth_m": {"type": "number", "minimum": 0, "maximum": 2},
                        },
                        "required": ["observation_id", "object_id", "selected"],
                        "additionalProperties": False,
                    }
                },
                "required": ["target"],
                "additionalProperties": False,
            },
            "resources": ["arm_motion"],
            "operations": ["start", "cancel"],
            "requires_runtime_state": ["running"],
            "requires_observations": ["front_clearance"],
            "max_duration_ms": 5000,
            "cancel_timeout_ms": 300,
            "danger": "high",
        },
        {
            "action_id": "new_arm.report_pose.v1",
            "description": "Report the current arm pose. Not cancelable.",
            "schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "resources": [],
            "operations": ["start"],
            "danger": "low",
        },
    ],
}

# A legacy v1 manifest: command delivery is a TopicBus topic.
LEGACY_MANIFEST = {
    "id": "legacy_demo",
    "actions": [
        {"action_id": "legacy_demo.move.v1", "topic": "legacy_demo.move.v1", "description": "Legacy move."}
    ],
}


def case(case_id, kind, purpose, expect, **extra):
    """Build one fixture case. ``expect`` is the acceptance decision + reason."""
    entry = {"id": case_id, "kind": kind, "purpose": purpose, "expect": expect}
    entry.update(extra)
    return entry


def goal_submit_cases():
    valid = json.loads(json.dumps(GOOD_GOAL_SUBMIT))
    empty = json.loads(json.dumps(GOOD_GOAL_SUBMIT))
    empty["goal_text_en"] = ""
    blank = json.loads(json.dumps(GOOD_GOAL_SUBMIT))
    blank["goal_text_en"] = "   \t  "
    long_text = json.loads(json.dumps(GOOD_GOAL_SUBMIT))
    long_text["goal_text_en"] = "x" * 5000
    # A proper noun with non-ASCII characters must NOT be rejected: the English
    # requirement is a skill/eval concern, not an ASCII gate.
    proper_noun = json.loads(json.dumps(GOOD_GOAL_SUBMIT))
    proper_noun["goal_text_en"] = "Place the café sign beside Björk's crate."
    cjk = json.loads(json.dumps(GOOD_GOAL_SUBMIT))
    cjk["goal_text_en"] = "拿起选中的杯子"
    missing_schema = json.loads(json.dumps(GOOD_GOAL_SUBMIT))
    del missing_schema["schema_version"]
    bad_schema = json.loads(json.dumps(GOOD_GOAL_SUBMIT))
    bad_schema["schema_version"] = 2
    negative_lease = json.loads(json.dumps(GOOD_GOAL_SUBMIT))
    negative_lease["lease_ms"] = -1
    zero_lease = json.loads(json.dumps(GOOD_GOAL_SUBMIT))
    zero_lease["lease_ms"] = 0
    completion_leak = json.loads(json.dumps(GOOD_GOAL_SUBMIT))
    completion_leak["completion"] = {"required_success_actions": ["new_arm.not_allowed.v1"]}

    return [
        case("S-GOAL-OK", "goal_submit", "B00 §2.2 example round-trips", {"outcome": "accept"},
             payload=valid),
        case("S-GOAL-EMPTY", "goal_submit", "empty goal is refused before side effects",
             {"outcome": "reject", "code": "empty_string"}, payload=empty),
        case("S-GOAL-BLANK", "goal_submit", "whitespace-only goal is refused",
             {"outcome": "reject", "code": "empty_string"}, payload=blank),
        case("S-GOAL-LONG", "goal_submit", "goal beyond MAX_GOAL_TEXT_LEN is refused",
             {"outcome": "reject", "code": "text_too_long"}, payload=long_text),
        case("S-GOAL-PROPER-NOUN", "goal_submit",
             "non-ASCII proper nouns are accepted; language is not an ASCII gate",
             {"outcome": "accept"}, payload=proper_noun),
        case("S-GOAL-NON-ENGLISH", "goal_submit",
             "a fully non-English goal parses; only an advisory marker is raised",
             {"outcome": "accept", "marker": "not_english_marker"}, payload=cjk),
        case("S-GOAL-NO-SCHEMA", "goal_submit", "business schema_version is mandatory",
             {"outcome": "reject", "code": "missing_field"}, payload=missing_schema),
        case("S-GOAL-BAD-SCHEMA", "goal_submit", "unknown schema_version is refused",
             {"outcome": "reject", "code": "unsupported_schema_version"}, payload=bad_schema),
        case("S-GOAL-NEG-TTL", "goal_submit", "negative lease is refused",
             {"outcome": "reject", "code": "negative_ttl"}, payload=negative_lease),
        case("S-GOAL-ZERO-LEASE", "goal_submit", "zero lease is refused",
             {"outcome": "reject", "code": "non_positive_lease"}, payload=zero_lease),
        case("S-GOAL-COMPLETION", "goal_submit",
             "completion may only cite actions the goal allowed",
             {"outcome": "reject", "code": "unknown_action"}, payload=completion_leak),
    ]


def goal_lifecycle_cases():
    return [
        case("S-PHASE-NEW", "phase", "a non-replacing goal becomes active",
             {"outcome": "accept", "phase": "active"},
             arguments={"accepted": True, "replacing": False, "old_stopped": False}),
        case("S-PHASE-REPLACE-STOPPED", "phase",
             "a replacing goal is active once the old stop is confirmed",
             {"outcome": "accept", "phase": "active"},
             arguments={"accepted": True, "replacing": True, "old_stopped": True}),
        case("S-PHASE-REPLACE-UNSTOPPED", "phase",
             "a replacing goal is blocked, never silently switched, if the old stop is unconfirmed",
             {"outcome": "accept", "phase": "blocked"},
             arguments={"accepted": True, "replacing": True, "old_stopped": False}),
        case("S-PHASE-REJECTED", "phase", "a refused goal is rejected",
             {"outcome": "accept", "phase": "rejected"},
             arguments={"accepted": False, "replacing": False, "old_stopped": False}),
    ]


def status_cases():
    return [
        case("S-ST-ADMIT", "status", "admitted -> accepted is legal",
             {"outcome": "accept", "result": "accepted"}, current="admitted", incoming="accepted"),
        case("S-ST-RUN", "status", "accepted -> running is legal",
             {"outcome": "accept", "result": "running"}, current="accepted", incoming="running"),
        case("S-ST-DONE", "status", "running -> succeeded is legal",
             {"outcome": "accept", "result": "succeeded"}, current="running", incoming="succeeded"),
        case("S-ST-PROGRESS-IDEMPOTENT", "status", "re-reporting running merges idempotently",
             {"outcome": "accept", "result": "running"}, current="running", incoming="running"),
        case("S-ST-REJECT", "status", "admitted -> rejected is legal",
             {"outcome": "accept", "result": "rejected"}, current="admitted", incoming="rejected"),
        case("S-ST-SKIP", "status", "admitted -> running (skipping acceptance) is refused",
             {"outcome": "reject", "code": "illegal_transition"}, current="admitted", incoming="running"),
        case("S-ST-BACKWARD", "status", "running -> admitted is refused",
             {"outcome": "reject", "code": "illegal_transition"}, current="running", incoming="admitted"),
        case("S-ST-TERMINAL-WINS", "status", "succeeded is terminal and cannot be overwritten",
             {"outcome": "reject", "code": "illegal_transition"}, current="succeeded", incoming="running"),
        case("S-ST-TERMINAL-FAILED", "status", "canceled cannot become failed",
             {"outcome": "reject", "code": "illegal_transition"}, current="canceled", incoming="failed"),
        case("S-ST-TERMINAL-UNKNOWN", "status", "timed_out cannot become unknown",
             {"outcome": "reject", "code": "illegal_transition"}, current="timed_out", incoming="unknown"),
    ]


def cancel_cases():
    return [
        case("S-CANCEL-EVIDENCE", "cancel", "canceled requires positive stop evidence",
             {"outcome": "accept", "result": "canceled"},
             has_stop_evidence=True, timed_out=False),
        case("S-CANCEL-PENDING", "cancel",
             "no evidence and no timeout yet leaves the action non-terminal",
             {"outcome": "accept", "result": None},
             has_stop_evidence=False, timed_out=False),
        case("S-CANCEL-TIMEOUT", "cancel",
             "a stop timeout is reported as timed_out, never faked as canceled",
             {"outcome": "accept", "result": "timed_out"},
             has_stop_evidence=False, timed_out=True),
    ]


def event_cases():
    event = {
        "event_id": "evt-1", "event_seq": 1, "ex_session": GOOD_SESSION,
        "task_id": "task-4", "goal_id": "goal-9", "goal_revision": 9,
        "command_id": "cmd-21", "owner": "new_arm", "status": "canceled",
        "reason_code": "", "details": {"stop_evidence": {"stopped": True, "source": "new_arm", "reference": "cmd-21"}},
    }
    no_evidence = json.loads(json.dumps(event))
    no_evidence["details"] = {}
    false_evidence = json.loads(json.dumps(event))
    false_evidence["details"]["stop_evidence"]["stopped"] = False
    return [
        case("S-EVENT-CANCELED", "action_event", "positive stop evidence in details round-trips",
             {"outcome": "accept"}, payload=event),
        case("S-EVENT-NO-STOP", "action_event", "canceled without evidence is refused",
             {"outcome": "reject", "code": "missing_field"}, payload=no_evidence),
        case("S-EVENT-STOP-FALSE", "action_event", "false stop claim cannot cancel",
             {"outcome": "reject", "code": "enum_violation"}, payload=false_evidence),
    ]


def manifest_cases():
    no_version = {"id": "legacy_demo", "actions": []}
    bad_version = json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2))
    bad_version["action_api_version"] = 1
    owner_mismatch = json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2))
    owner_mismatch["actions"][0]["action_id"] = "other_owner.forward.v1"
    dup = json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2))
    dup["actions"].append(json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2["actions"][0])))
    bad_id = json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2))
    bad_id["actions"][0]["action_id"] = "NotAnActionId"
    no_cancel_timeout = json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2))
    del no_cancel_timeout["actions"][0]["cancel_timeout_ms"]
    cancel_timeout_without_cancel = json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2))
    cancel_timeout_without_cancel["actions"][0]["operations"] = ["start"]
    unsupported_keyword = json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2))
    unsupported_keyword["actions"][0]["schema"]["oneOf"] = [{"type": "object"}]
    bad_runtime_state = json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2))
    bad_runtime_state["actions"][0]["requires_runtime_state"] = ["teleporting"]
    bad_danger = json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2))
    bad_danger["actions"][0]["danger"] = "catastrophic"
    empty_actions = json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2))
    empty_actions["actions"] = []
    bad_operation = json.loads(json.dumps(GOOD_ACTION_MANIFEST_V2))
    bad_operation["actions"][0]["operations"] = ["start", "detonate"]

    return [
        case("S-MF-V2-OK", "action_manifest_v2", "B00 §2.4 example parses",
             {"outcome": "accept"}, owner="new_base", payload=GOOD_ACTION_MANIFEST_V2),
        case("S-MF-LEGACY", "action_manifest_v2",
             "a v1 manifest cannot be parsed as v2",
             {"outcome": "reject", "code": "legacy_isolation"}, owner="", payload=no_version),
        case("S-MF-BAD-VERSION", "action_manifest_v2", "only action_api_version 2 is supported",
             {"outcome": "reject", "code": "unsupported_action_api_version"},
             owner="new_base", payload=bad_version),
        case("S-MF-OWNER", "action_manifest_v2", "action_id must belong to the declaring owner",
             {"outcome": "reject", "code": "owner_mismatch"}, owner="new_base", payload=owner_mismatch),
        case("S-MF-DUP", "action_manifest_v2", "duplicate action_id is refused",
             {"outcome": "reject", "code": "duplicate_action_id"}, owner="new_base", payload=dup),
        case("S-MF-BAD-ID", "action_manifest_v2", "malformed action_id is refused",
             {"outcome": "reject", "code": "invalid_action_id"}, owner="", payload=bad_id),
        case("S-MF-CANCEL-NO-TIMEOUT", "action_manifest_v2",
             "declaring cancel requires a cancel_timeout_ms",
             {"outcome": "reject", "code": "cancel_unsupported"},
             owner="new_base", payload=no_cancel_timeout),
        case("S-MF-TIMEOUT-NO-CANCEL", "action_manifest_v2",
             "cancel_timeout_ms without cancel is refused",
             {"outcome": "reject", "code": "cancel_unsupported"},
             owner="new_base", payload=cancel_timeout_without_cancel),
        case("S-MF-KEYWORD", "action_manifest_v2",
             "an unsupported schema keyword is refused, not silently ignored",
             {"outcome": "reject", "code": "schema_unsupported_keyword"},
             owner="new_base", payload=unsupported_keyword),
        case("S-MF-RUNTIME-STATE", "action_manifest_v2", "unknown runtime state is refused",
             {"outcome": "reject", "code": "enum_violation"},
             owner="new_base", payload=bad_runtime_state),
        case("S-MF-DANGER", "action_manifest_v2", "danger must be a declared level",
             {"outcome": "reject", "code": "enum_violation"}, owner="new_base", payload=bad_danger),
        case("S-MF-EMPTY", "action_manifest_v2", "an action manifest must declare at least one action",
             {"outcome": "reject", "code": "length_violation"}, owner="new_base", payload=empty_actions),
        case("S-MF-OPERATION", "action_manifest_v2", "only declared operations are accepted",
             {"outcome": "reject", "code": "unsupported_operation"},
             owner="new_base", payload=bad_operation),
        case("S-MF-LEGACY-PARSE", "legacy_manifest",
             "a v2 manifest cannot be parsed as a legacy topic manifest",
             {"outcome": "reject", "code": "legacy_isolation"}, payload=GOOD_ACTION_MANIFEST_V2),
        case("S-MF-LEGACY-OK", "legacy_manifest",
             "a v1 topic manifest parses on the legacy path",
             {"outcome": "accept", "actions": 1}, payload=LEGACY_MANIFEST),
    ]


def command_cases():
    unknown_action = json.loads(json.dumps(GOOD_COMMAND))
    unknown_action["action_id"] = "new_arm.teleport.v1"
    owner_mismatch = json.loads(json.dumps(GOOD_COMMAND))
    owner_mismatch["owner"] = "new_yolo"
    no_params = json.loads(json.dumps(GOOD_COMMAND))
    del no_params["params"]
    extra_field = json.loads(json.dumps(GOOD_COMMAND))
    extra_field["params"]["target"]["color"] = "red"
    bad_enum = json.loads(json.dumps(GOOD_COMMAND))
    bad_enum["params"]["target"]["selected"] = "yes"
    range_violation = json.loads(json.dumps(GOOD_COMMAND))
    range_violation["params"]["target"]["depth_m"] = 5.0
    perf_nan = json.loads(json.dumps(GOOD_COMMAND))
    perf_nan["params"]["target"]["depth_m"] = {"__fixture_non_finite__": "nan"}
    perf_inf = json.loads(json.dumps(GOOD_COMMAND))
    perf_inf["params"]["target"]["depth_m"] = {"__fixture_non_finite__": "infinity"}
    neg_lease = json.loads(json.dumps(GOOD_COMMAND))
    neg_lease["lease_ms"] = -5
    zero_lease = json.loads(json.dumps(GOOD_COMMAND))
    zero_lease["lease_ms"] = 0
    no_schema = json.loads(json.dumps(GOOD_COMMAND))
    del no_schema["schema_version"]
    bad_operation = json.loads(json.dumps(GOOD_COMMAND))
    bad_operation["operation"] = "launch"
    cancel_unsupported = json.loads(json.dumps(GOOD_COMMAND))
    cancel_unsupported["action_id"] = "new_arm.report_pose.v1"
    cancel_unsupported["owner"] = "new_arm"
    cancel_unsupported["operation"] = "cancel"
    stale_session = json.loads(json.dumps(GOOD_COMMAND))
    stale_session["ex_session"] = "ex-boot-old"
    bad_revision = json.loads(json.dumps(GOOD_COMMAND))
    bad_revision["goal_revision"] = 3

    return [
        case("S-CMD-OK", "action_command", "B00 §2.3 example is admitted",
             {"outcome": "accept", "action_id": "new_arm.pick_selected.v1"},
             payload=GOOD_COMMAND, manifest=ARM_MANIFEST),
        case("S-CMD-UNKNOWN-ACTION", "action_command", "an action not in the catalog is refused",
             {"outcome": "reject", "code": "unknown_action"},
             payload=unknown_action, manifest=ARM_MANIFEST),
        case("S-CMD-OWNER", "action_command", "owner is bound by prefix, not by payload claim",
             {"outcome": "reject", "code": "owner_mismatch"},
             payload=owner_mismatch, manifest=ARM_MANIFEST),
        case("S-CMD-NO-PARAMS", "action_command", "params is mandatory",
             {"outcome": "reject", "code": "missing_params"},
             payload=no_params, manifest=ARM_MANIFEST),
        case("S-CMD-EXTRA-FIELD", "action_command",
             "additionalProperties:false rejects an undeclared field",
             {"outcome": "reject", "code": "unknown_field"},
             payload=extra_field, manifest=ARM_MANIFEST),
        case("S-CMD-ENUM", "action_command", "a boolean field rejects a string",
             {"outcome": "reject", "code": "invalid_type"},
             payload=bad_enum, manifest=ARM_MANIFEST),
        case("S-CMD-RANGE", "action_command", "a numeric bound is enforced",
             {"outcome": "reject", "code": "range_violation"},
             payload=range_violation, manifest=ARM_MANIFEST),
        case("S-CMD-NAN", "action_command", "NaN is refused at the parse layer",
             {"outcome": "reject", "code": "non_finite_number"},
             payload=perf_nan, manifest=ARM_MANIFEST),
        case("S-CMD-INF", "action_command", "Infinity is refused at the parse layer",
             {"outcome": "reject", "code": "non_finite_number"},
             payload=perf_inf, manifest=ARM_MANIFEST),
        case("S-CMD-NEG-TTL", "action_command", "negative lease is refused",
             {"outcome": "reject", "code": "negative_ttl"},
             payload=neg_lease, manifest=ARM_MANIFEST),
        case("S-CMD-ZERO-LEASE", "action_command", "zero lease is refused",
             {"outcome": "reject", "code": "non_positive_lease"},
             payload=zero_lease, manifest=ARM_MANIFEST),
        case("S-CMD-NO-SCHEMA", "action_command", "business schema_version is mandatory",
             {"outcome": "reject", "code": "missing_field"},
             payload=no_schema, manifest=ARM_MANIFEST),
        case("S-CMD-OPERATION", "action_command", "an undeclared operation is refused",
             {"outcome": "reject", "code": "unsupported_operation"},
             payload=bad_operation, manifest=ARM_MANIFEST),
        case("S-CMD-CANCEL-UNSUPPORTED", "action_command",
             "cancel on an action that never declared it is refused",
             {"outcome": "reject", "code": "cancel_unsupported"},
             payload=cancel_unsupported, manifest=ARM_MANIFEST),
        case("S-CMD-STALE-SESSION", "action_command",
             "a command from a previous EX boot is refused",
             {"outcome": "reject", "code": "stale_ex_session"},
             payload=stale_session, manifest=ARM_MANIFEST, current_ex_session=GOOD_SESSION),
        case("S-CMD-BAD-REVISION", "action_command",
             "a command bound to an old goal revision is refused",
             {"outcome": "reject", "code": "revision_conflict"},
             payload=bad_revision, manifest=ARM_MANIFEST, current_revision=9),
    ]


def idempotency_cases():
    return [
        case("S-IDEM-REPLAY", "idempotency", "the same request_id with the same payload replays",
             {"outcome": "accept", "results": ["new", "replay"]},
             request_id="req-17", payloads=[GOOD_GOAL_SUBMIT, GOOD_GOAL_SUBMIT]),
        case("S-IDEM-CONFLICT", "idempotency",
             "the same request_id with a different payload conflicts",
             {"outcome": "reject", "code": "duplicate_request_id_conflict"},
             request_id="req-17", payloads=[GOOD_GOAL_SUBMIT, GOOD_COMMAND]),
    ]


def resource_cases():
    return [
        case("S-RES-FREE", "resources", "an unheld resource is granted",
             {"outcome": "accept"}, holders={}, requested=["base_motion"], command_id="cmd-21"),
        case("S-RES-SELF", "resources", "a command may re-declare a resource it already holds",
             {"outcome": "accept"}, holders={"base_motion": "cmd-21"},
             requested=["base_motion"], command_id="cmd-21"),
        case("S-RES-CONFLICT", "resources", "a resource held by another live command is refused",
             {"outcome": "reject", "code": "resource_conflict"},
             holders={"base_motion": "cmd-99"}, requested=["base_motion"], command_id="cmd-21"),
    ]


def method_cases():
    return [
        case("S-METHOD-UNKNOWN", "request_parse",
             "an unknown method must be reported unsupported, never fall back to a legacy proposal",
             {"outcome": "reject", "code": "unsupported_method"},
             method="bridge.proposal.submit", payload={}),
        case("S-METHOD-KNOWN", "request_parse", "a known decision method parses",
             {"outcome": "accept", "type": "GoalSubmit"},
             method="decision.goal.submit", payload=GOOD_GOAL_SUBMIT),
    ]


def revision_cases():
    return [
        case("S-REV-MATCH", "revision", "a matching expected_revision advances by one",
             {"outcome": "accept", "result": 9}, expected=8, current=8),
        case("S-REV-MISMATCH", "revision", "a stale expected_revision is refused",
             {"outcome": "reject", "code": "revision_conflict"}, expected=7, current=8),
        case("S-REV-NONE", "revision", "an omitted expected_revision is not a compare step",
             {"outcome": "accept", "result": 9}, expected=None, current=8),
    ]


def snapshot_cases():
    versions = {"ex_session": GOOD_SESSION, "goal_revision": 9, "config_revision": 1,
                "catalog_revision": 2, "environment_generation": 3, "gate_epoch": 4,
                "plugin_generations": {"new_arm": 5}}
    observation = {"schema_version": 1, "observation_id": "obs-40", "source_id": "front",
                   "source_epoch": "camera-boot-a", "seq": 40, "received_monotonic_ns": 2**53 + 1,
                   "age_ms": 12.5, "description_hash": "hash-40", "health": {"status": "ok", "reason_code": ""},
                   "data": {"object_id": "track-8", "selected": True}}
    snapshot = {"schema_version": 1, "snapshot_id": "snap-1", "created_monotonic_ns": 2**53 + 2,
                "versions": versions,
                "goal": {"task_id": "task-4", "goal_id": "goal-9", "goal_text_en": "Pick the selected cup.",
                         "allowed_actions": ["new_arm.pick_selected.v1"],
                         "parameters": {"new_arm.pick_selected.v1": {"target": {"observation_id": "obs-40"}}}},
                "observations": [observation],
                "owners": [{"owner": "new_arm", "plugin_generation": 5, "status": "ready", "candidates": [
                    {"option_id": "opt-pick", "kind": "start", "description": "Pick cup", "eligible": True,
                     "action_id": "new_arm.pick_selected.v1"},
                    {"option_id": "opt-wait", "kind": "wait", "description": "Wait", "eligible": True}]}]}
    decision = {"schema_version": 1, "snapshot_id": "snap-1", "versions": versions,
                "backend": "mock", "model": "policy-1", "elapsed_ms": 12.5,
                "choices": [{"owner": "new_arm", "option_id": "opt-pick", "confidence": 0.75,
                             "probabilities": {"opt-pick": 0.75, "opt-wait": 0.25}}]}
    wrong_age = json.loads(json.dumps(observation)); wrong_age["age_ms"] = -1
    wrong_generation = json.loads(json.dumps(snapshot)); wrong_generation["versions"]["environment_generation"] = "env-3"
    unknown = json.loads(json.dumps(decision)); unknown["choices"][0]["option_id"] = "opt-other"
    stale = json.loads(json.dumps(versions)); stale["gate_epoch"] = 5
    return [
        case("S-OBS-FRACTIONAL-AGE", "snapshot_contract", "fractional age and 64-bit monotonic time parse",
             {"outcome": "accept"}, parser="ObservationEnvelope", payload=observation),
        case("S-OBS-NEG-AGE", "snapshot_contract", "negative age rejected",
             {"outcome": "reject", "code": "range_violation"}, parser="ObservationEnvelope", payload=wrong_age),
        case("S-VERS-INT-ENV", "snapshot_contract", "environment generation is integer",
             {"outcome": "accept"}, parser="VersionSet", payload=versions),
        case("S-SNAP-BOUND-OWNER", "snapshot_contract", "snapshot binds goal candidates and owner generation",
             {"outcome": "accept"}, parser="DecisionSnapshot", payload=snapshot),
        case("S-SNAP-ENV-STRING", "snapshot_contract", "environment generation cannot be a string",
             {"outcome": "reject", "code": "invalid_type"}, parser="DecisionSnapshot", payload=wrong_generation),
        case("S-BACKEND-OPTION", "snapshot_contract", "backend chooses a bound eligible option",
             {"outcome": "accept"}, parser="BackendDecision", payload=decision),
        case("S-BACKEND-SELECT", "snapshot_selection", "selection validates against snapshot and versions",
             {"outcome": "accept", "options": ["opt-pick"]}, snapshot=snapshot, decision=decision, current_versions=versions),
        case("S-BACKEND-UNKNOWN", "snapshot_selection", "unlisted option cannot execute",
             {"outcome": "reject", "code": "unknown_action"}, snapshot=snapshot, decision=unknown, current_versions=versions),
        case("S-BACKEND-STALE", "snapshot_selection", "stale gate invalidates backend choice",
             {"outcome": "reject", "code": "revision_conflict"}, snapshot=snapshot, decision=decision, current_versions=stale),
    ]


def build_fixture():
    cases = []
    cases += goal_submit_cases()
    cases += goal_lifecycle_cases()
    cases += status_cases()
    cases += cancel_cases()
    cases += event_cases()
    cases += manifest_cases()
    cases += command_cases()
    cases += idempotency_cases()
    cases += resource_cases()
    cases += method_cases()
    cases += revision_cases()
    cases += snapshot_cases()
    return {
        "fixture": "b00-decision-contracts",
        "schema_version": 1,
        "protocol": "astrbotex-zmq",
        "protocol_version": 1,
        "note": (
            "Shared golden fixtures for B00. Both AstrBotEX and A.E.B parse this "
            "same file; identical outcomes are the cross-endpoint proof."
        ),
        "cases": cases,
    }


def main() -> int:
    fixture = build_fixture()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(fixture, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT} ({len(fixture['cases'])} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
