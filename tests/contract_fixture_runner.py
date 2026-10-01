"""Execute B00 golden contract fixtures against a contract module.

The runner is deliberately import-agnostic: it is fed a contract module (the
AstrBotEX implementation, or the A.E.B mirror) and a parsed fixture, and returns
a list of ``(case_id, ok, detail)`` results. Both repositories run this same
logic, so "identical outcome" is a property of the data, not of two hand-written
test suites that could drift.

It never touches the network, a runtime, or a hardware SDK.
"""

from __future__ import annotations

import json
import math
from typing import Any

FIXTURE_NAME = "b00-decision-contracts"


def _reject_non_json_number(value: str) -> None:
    raise ValueError(f"non-JSON number: {value}")


class CaseResult:
    __slots__ = ("case_id", "ok", "detail")

    def __init__(self, case_id: str, ok: bool, detail: str = "") -> None:
        self.case_id = case_id
        self.ok = ok
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        mark = "ok" if self.ok else "FAIL"
        return f"<{mark} {self.case_id}{': ' + self.detail if self.detail else ''}>"


def load_fixture(path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_non_json_number)
    if data.get("fixture") != FIXTURE_NAME:
        raise ValueError(f"unexpected fixture: {data.get('fixture')!r}")
    return data


def _decode(payload: Any) -> Any:
    """Inject only explicit test markers after strict fixture JSON decoding."""
    if isinstance(payload, dict):
        if set(payload) == {"__fixture_non_finite__"}:
            marker = payload["__fixture_non_finite__"]
            if marker == "nan":
                return math.nan
            if marker == "infinity":
                return math.inf
            if marker == "-infinity":
                return -math.inf
            raise ValueError(f"unknown fixture marker: {marker!r}")
        return {key: _decode(item) for key, item in payload.items()}
    if isinstance(payload, list):
        return [_decode(item) for item in payload]
    return payload


def _expect_reject(call, expect_code: str) -> tuple[bool, str]:
    try:
        call()
    except _CONTRACTS.ContractError as exc:  # noqa: F821 - bound in run_fixtures
        if exc.error.code != expect_code:
            return False, f"expected {expect_code}, got {exc.error.code}"
        return True, ""
    except Exception as exc:  # noqa: BLE001 - a non-contract error is a hard failure
        return False, f"unexpected {type(exc).__name__}: {exc}"
    return False, "accepted but should have been rejected"


# The contract module under test is bound here by run_fixtures, so the case
# functions read one module-level name instead of threading it through.
_CONTRACTS: Any = None


def _run_goal_submit(case) -> tuple[bool, str]:
    c = _CONTRACTS
    expect = case["expect"]
    payload = _decode(case["payload"])
    if expect["outcome"] == "accept":
        try:
            goal = c.GoalSubmit.parse(payload)
        except c.ContractError as exc:
            return False, f"unexpectedly rejected: {exc.error.code}"
        marker = goal.english_marker()
        if "marker" in expect:
            if marker is None or marker.code != expect["marker"]:
                return False, f"expected advisory marker {expect['marker']}, got {marker}"
        elif marker is not None:
            return False, f"unexpected marker on an English goal: {marker.code}"
        return True, ""
    return _expect_reject(lambda: c.GoalSubmit.parse(payload), expect["code"])


def _run_phase(case) -> tuple[bool, str]:
    c = _CONTRACTS
    expect = case["expect"]
    phase = c.decide_phase(**case["arguments"])
    if phase != expect["phase"]:
        return False, f"expected {expect['phase']}, got {phase}"
    if c.phase_enters_decision(phase) != (phase == "active"):
        return False, "only active may enter decision"
    return True, ""


def _run_status(case) -> tuple[bool, str]:
    c = _CONTRACTS
    expect = case["expect"]
    current, incoming = case["current"], case["incoming"]
    legal = c.can_transition(current, incoming)
    if expect["outcome"] == "accept":
        if not legal:
            return False, f"expected legal {current}->{incoming}"
        result = c.apply_status(current, incoming)
        if result != expect["result"]:
            return False, f"expected {expect['result']}, got {result}"
        return True, ""
    if legal:
        return False, f"expected {current}->{incoming} to be illegal"
    return _expect_reject(lambda: c.apply_status(current, incoming), expect["code"])


def _run_cancel(case) -> tuple[bool, str]:
    c = _CONTRACTS
    result = c.cancel_outcome(
        has_stop_evidence=case["has_stop_evidence"], timed_out=case["timed_out"]
    )
    if result != case["expect"]["result"]:
        return False, f"expected {case['expect']['result']}, got {result}"
    return True, ""


def _run_action_event(case) -> tuple[bool, str]:
    c = _CONTRACTS
    payload = _decode(case["payload"])
    if case["expect"]["outcome"] == "accept":
        try:
            event = c.ActionEvent.parse(payload)
        except c.ContractError as exc:
            return False, f"unexpectedly rejected: {exc.error.code}"
        if event.to_dict() != payload:
            return False, "event did not round-trip"
        return True, ""
    return _expect_reject(lambda: c.ActionEvent.parse(payload), case["expect"]["code"])


def _run_manifest_v2(case) -> tuple[bool, str]:
    c = _CONTRACTS
    expect = case["expect"]
    owner = case.get("owner", "")
    if expect["outcome"] == "accept":
        try:
            manifest = c.parse_action_manifest(case["payload"], owner=owner)
        except c.ContractError as exc:
            return False, f"unexpectedly rejected: {exc.error.code}"
        if not manifest.actions:
            return False, "no actions parsed"
        return True, ""
    return _expect_reject(
        lambda: c.parse_action_manifest(case["payload"], owner=owner), expect["code"]
    )


def _run_legacy_manifest(case) -> tuple[bool, str]:
    c = _CONTRACTS
    expect = case["expect"]
    if expect["outcome"] == "accept":
        try:
            actions = c.parse_legacy_action_manifest(case["payload"])
        except c.ContractError as exc:
            return False, f"unexpectedly rejected: {exc.error.code}"
        if len(actions) != expect["actions"]:
            return False, f"expected {expect['actions']} actions, got {len(actions)}"
        return True, ""
    return _expect_reject(
        lambda: c.parse_legacy_action_manifest(case["payload"]), expect["code"]
    )


def _run_command(case) -> tuple[bool, str]:
    c = _CONTRACTS
    expect = case["expect"]
    payload = _decode(case["payload"])

    def admit():
        # A rejected parse must happen before any manifest lookup or admission
        # side effect, which is why parse is inside this closure.
        command = c.ActionCommand.parse(payload)
        manifest = c.parse_action_manifest(case["manifest"], owner="new_arm")
        return c.validate_command_against_manifest(
            command,
            manifest,
            current_ex_session=case.get("current_ex_session", "ex-boot-a"),
            current_revision=case.get("current_revision", 9),
            runtime_state="running",
            trusted_owner="new_arm",
        )

    if expect["outcome"] == "accept":
        try:
            declaration = admit()
        except c.ContractError as exc:
            return False, f"unexpectedly rejected: {exc.error.code}"
        if declaration.action_id != expect["action_id"]:
            return False, f"wrong action: {declaration.action_id}"
        return True, ""
    return _expect_reject(admit, expect["code"])


def _run_idempotency(case) -> tuple[bool, str]:
    c = _CONTRACTS
    expect = case["expect"]
    registry = c.IdempotencyRegistry()
    request_id = case["request_id"]
    results: list[str] = []
    for index, payload in enumerate(case["payloads"]):
        try:
            results.append(registry.resolve(request_id, payload))
        except c.ContractError as exc:
            if expect["outcome"] != "reject":
                return False, f"unexpectedly rejected: {exc.error.code}"
            if exc.error.code != expect["code"]:
                return False, f"expected {expect['code']}, got {exc.error.code}"
            return True, ""
        if expect["outcome"] == "reject" and index == len(case["payloads"]) - 1:
            return False, "expected a conflict but the payload was accepted"
    if results != expect["results"]:
        return False, f"expected {expect['results']}, got {results}"
    return True, ""


def _run_resources(case) -> tuple[bool, str]:
    c = _CONTRACTS
    expect = case["expect"]
    try:
        c.check_resources(case["holders"], case["requested"], case["command_id"])
    except c.ContractError as exc:
        if expect["outcome"] != "reject":
            return False, f"unexpectedly rejected: {exc.error.code}"
        if exc.error.code != expect["code"]:
            return False, f"expected {expect['code']}, got {exc.error.code}"
        return True, ""
    if expect["outcome"] == "reject":
        return False, "expected a resource conflict"
    return True, ""


def _run_request_parse(case) -> tuple[bool, str]:
    c = _CONTRACTS
    expect = case["expect"]
    if expect["outcome"] == "accept":
        try:
            parsed = c.parse_request(case["method"], case["payload"])
        except c.ContractError as exc:
            return False, f"unexpectedly rejected: {exc.error.code}"
        if type(parsed).__name__ != expect["type"]:
            return False, f"expected {expect['type']}, got {type(parsed).__name__}"
        return True, ""
    return _expect_reject(
        lambda: c.parse_request(case["method"], case["payload"]), expect["code"]
    )


def _run_revision(case) -> tuple[bool, str]:
    c = _CONTRACTS
    expect = case["expect"]
    try:
        result = c.check_revision(case["expected"], case["current"])
    except c.ContractError as exc:
        if expect["outcome"] != "reject":
            return False, f"unexpectedly rejected: {exc.error.code}"
        if exc.error.code != expect["code"]:
            return False, f"expected {expect['code']}, got {exc.error.code}"
        return True, ""
    if expect["outcome"] == "reject":
        return False, "expected a revision conflict"
    if result != expect["result"]:
        return False, f"expected {expect['result']}, got {result}"
    return True, ""


def _run_snapshot_contract(case) -> tuple[bool, str]:
    c = _CONTRACTS
    parser = getattr(c, case["parser"])
    if case["expect"]["outcome"] == "reject":
        return _expect_reject(lambda: parser.parse(_decode(case["payload"])), case["expect"]["code"])
    try:
        parsed = parser.parse(_decode(case["payload"]))
        parser.parse(parsed.to_dict())
    except c.ContractError as exc:
        return False, f"unexpectedly rejected: {exc.error.code}"
    return True, ""


def _run_snapshot_selection(case) -> tuple[bool, str]:
    c = _CONTRACTS
    def check():
        return c.validate_backend_selection(c.DecisionSnapshot.parse(case["snapshot"]),
                                            c.BackendDecision.parse(case["decision"]),
                                            c.VersionSet.parse(case["current_versions"]))
    if case["expect"]["outcome"] == "reject":
        return _expect_reject(check, case["expect"]["code"])
    try:
        selected = check()
    except c.ContractError as exc:
        return False, f"unexpectedly rejected: {exc.error.code}"
    return ([item["option_id"] for item in selected] == case["expect"]["options"], "selected different option")


_RUNNERS = {
    "goal_submit": _run_goal_submit,
    "phase": _run_phase,
    "status": _run_status,
    "cancel": _run_cancel,
    "action_event": _run_action_event,
    "action_manifest_v2": _run_manifest_v2,
    "legacy_manifest": _run_legacy_manifest,
    "action_command": _run_command,
    "idempotency": _run_idempotency,
    "resources": _run_resources,
    "request_parse": _run_request_parse,
    "revision": _run_revision,
    "snapshot_contract": _run_snapshot_contract,
    "snapshot_selection": _run_snapshot_selection,
}


def run_fixtures(contracts, fixture: dict[str, Any]) -> list[CaseResult]:
    """Run every fixture case against ``contracts``; return per-case results."""
    global _CONTRACTS
    previous, _CONTRACTS = _CONTRACTS, contracts
    try:
        results: list[CaseResult] = []
        for case in fixture["cases"]:
            runner = _RUNNERS.get(case["kind"])
            if runner is None:
                results.append(CaseResult(case["id"], False, f"unknown kind {case['kind']!r}"))
                continue
            try:
                ok, detail = runner(case)
            except Exception as exc:  # noqa: BLE001 - a crash fails the case, not the run
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append(CaseResult(case["id"], ok, detail))
        return results
    finally:
        _CONTRACTS = previous


def outcome_summary(contracts, fixture: dict[str, Any]) -> dict[str, str]:
    """Case id -> ``accept``/``reject:<detail>`` verdict, to compare endpoints."""
    return {
        case["id"]: ("accept" if result.ok else f"reject:{result.detail}")
        for case, result in zip(fixture["cases"], run_fixtures(contracts, fixture))
    }
