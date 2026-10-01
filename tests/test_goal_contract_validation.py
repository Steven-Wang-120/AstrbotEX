from __future__ import annotations

import copy
import math
import unittest

from astrbot_ex.core.actions.models import ActionEvent
from astrbot_ex.core.decision import models as d


GOAL = {
    "schema_version": 1, "request_id": "r", "ex_session": "s", "task_id": "t",
    "step_id": "p", "goal_id": "g", "goal_text_en": "Pick the cup.",
    "allowed_actions": ["arm.pick.v1"], "parameters": {"arm.pick.v1": {"target": {"observation_id": "o"}}},
    "completion": {"required_success_actions": ["arm.pick.v1"]}, "lease_ms": 1000, "expected_revision": 0,
}


def reject(call, code):
    with unittest.TestCase().assertRaises(d.ContractError) as caught:
        call()
    unittest.TestCase().assertEqual(caught.exception.error.code, code)


class GoalValidationTests(unittest.TestCase):
    def test_round_trip_and_input_copy(self):
        payload = copy.deepcopy(GOAL)
        parsed = d.GoalSubmit.parse(payload)
        payload["parameters"]["arm.pick.v1"]["target"]["observation_id"] = "changed"
        self.assertEqual(parsed.parameters["arm.pick.v1"]["target"]["observation_id"], "o")
        self.assertEqual(d.GoalSubmit.parse(parsed.to_dict()).to_dict(), parsed.to_dict())

    def test_schema_version_missing_bool_float(self):
        for value in (True, 1.0, 2, "1"):
            payload = copy.deepcopy(GOAL); payload["schema_version"] = value
            reject(lambda payload=payload: d.GoalSubmit.parse(payload), "unsupported_schema_version")
        payload = copy.deepcopy(GOAL); del payload["schema_version"]
        reject(lambda: d.GoalSubmit.parse(payload), "missing_field")

    def test_unknown_nested_nonfinite_budget_depth_cycle(self):
        payload = copy.deepcopy(GOAL); payload["completion"]["extra"] = True
        reject(lambda: d.GoalSubmit.parse(payload), "unknown_field")
        payload = copy.deepcopy(GOAL); payload["parameters"]["arm.pick.v1"]["n"] = math.inf
        reject(lambda: d.GoalSubmit.parse(payload), "non_finite_number")
        payload = copy.deepcopy(GOAL); payload["parameters"]["arm.pick.v1"] = {"x": "x" * (1_048_577)}
        reject(lambda: d.GoalSubmit.parse(payload), "value_budget_exceeded")
        payload = copy.deepcopy(GOAL); node = {}
        for _ in range(34): node = {"next": node}
        payload["parameters"]["arm.pick.v1"] = node
        reject(lambda: d.GoalSubmit.parse(payload), "schema_depth_exceeded")
        payload = copy.deepcopy(GOAL); payload["parameters"]["arm.pick.v1"]["cycle"] = payload["parameters"]
        reject(lambda: d.GoalSubmit.parse(payload), "invalid_type")

    def test_wire_error_paths_and_budget_codes(self):
        for method, code in (([], "invalid_type"), ("", "empty_string"), ("x" * 257, "text_too_long")):
            with self.subTest(method=method):
                with self.assertRaises(d.ContractError) as caught:
                    d.parse_request(method, GOAL)
                self.assertEqual((caught.exception.error.code, caught.exception.error.path), (code, "method"))
        with self.assertRaises(d.ContractError) as caught:
            d.EventsRequest.parse({"schema_version": 1, "since_event_seq": 0})
        self.assertEqual((caught.exception.error.code, caught.exception.error.path), ("missing_field", "ex_session"))
        for value, code, path in (
            (math.inf, "non_finite_number", "parameters.arm.pick.v1.target.depth_m"),
            ("x" * 1_048_576, "value_budget_exceeded", "goal_submit"),
            (10**4096, "value_budget_exceeded", "goal_submit"),
        ):
            payload = copy.deepcopy(GOAL)
            payload["parameters"]["arm.pick.v1"]["target"]["depth_m"] = value
            with self.subTest(code=code):
                with self.assertRaises(d.ContractError) as caught:
                    d.GoalSubmit.parse(payload)
                self.assertEqual((caught.exception.error.code, caught.exception.error.path), (code, path))
        cycle = {}
        cycle["self"] = cycle
        payload = copy.deepcopy(GOAL)
        payload["parameters"]["arm.pick.v1"]["target"] = cycle
        with self.assertRaises(d.ContractError) as caught:
            d.GoalSubmit.parse(payload)
        self.assertEqual((caught.exception.error.code, caught.exception.error.path), ("invalid_type", "parameters.arm.pick.v1.target.self"))
        nested = {}
        for _ in range(34):
            nested = {"child": nested}
        payload = copy.deepcopy(GOAL)
        payload["parameters"]["arm.pick.v1"] = nested
        with self.assertRaises(d.ContractError) as caught:
            d.GoalSubmit.parse(payload)
        self.assertEqual((caught.exception.error.code, caught.exception.error.path), ("schema_depth_exceeded", "goal_submit"))

    def test_revision_dependency_is_visible(self):
        self.assertEqual(d.check_revision(None, 4), 5)


class DecisionMessageValidationTests(unittest.TestCase):
    def test_all_request_entries_are_strict(self):
        messages = {
            d.METHOD_GOAL_CANCEL: {"schema_version": 1, "request_id": "r", "ex_session": "s", "goal_id": "g", "goal_revision": 0, "reason_code": ""},
            d.METHOD_GOAL_RENEW: {"schema_version": 1, "request_id": "r", "ex_session": "s", "goal_id": "g", "goal_revision": 0, "lease_ms": 1000},
            d.METHOD_STATE_GET: {"schema_version": 1, "ex_session": "s"},
            d.METHOD_EVENTS_GET: {"schema_version": 1, "ex_session": "s", "since_event_seq": 0},
            d.METHOD_FEEDBACK: {"schema_version": 1, "ex_session": "s", "task_id": "t", "goal_id": "g", "goal_revision": 0, "event_seq": 1, "status": "running", "details": {}},
        }
        for method, payload in messages.items():
            parsed = d.parse_request(method, payload)
            self.assertIsNotNone(parsed)
            if hasattr(parsed, "to_dict"):
                self.assertEqual(type(parsed).parse(parsed.to_dict()).to_dict(), parsed.to_dict())
            bad = dict(payload, unknown=True)
            reject(lambda method=method, bad=bad: d.parse_request(method, bad), "unknown_field")
            bad = dict(payload); del bad["schema_version"]
            reject(lambda method=method, bad=bad: d.parse_request(method, bad), "missing_field")
            for version in (True, 1.0):
                bad = dict(payload, schema_version=version)
                reject(lambda method=method, bad=bad: d.parse_request(method, bad), "unsupported_schema_version")

    def test_state_feedback_unknown_method(self):
        state = {"schema_version": 1, "ex_session": "s", "revision": 0, "execution": {}, "event_seq": 0}
        self.assertEqual(d.DecisionState.parse(state).to_dict()["schema_version"], 1)
        reject(lambda: d.DecisionState.parse(dict(state, active_goal_id="g")), "invalid_type")
        feedback = {"schema_version": 1, "ex_session": "s", "task_id": "t", "goal_id": "g", "goal_revision": 0, "event_seq": 1, "status": "bogus", "details": {}}
        reject(lambda: d.Feedback.parse(feedback), "enum_violation")
        feedback_zero = dict(feedback, status="running", event_seq=0)
        reject(lambda: d.Feedback.parse(feedback_zero), "range_violation")
        reject(lambda: d.parse_request("bridge.proposal.submit", {}), "unsupported_method")

    def test_bad_types_and_mutation(self):
        goal = copy.deepcopy(GOAL)
        goal["expected_revision"] = True
        reject(lambda: d.GoalSubmit.parse(goal), "invalid_type")
        goal = copy.deepcopy(GOAL)
        goal["lease_ms"] = -1
        reject(lambda: d.GoalSubmit.parse(goal), "negative_ttl")
        goal = copy.deepcopy(GOAL)
        goal["parameters"]["unexpected.v1"] = {}
        reject(lambda: d.GoalSubmit.parse(goal), "unknown_action")
        state = {"schema_version": 1, "ex_session": "s", "revision": 0, "active_goal_id": "g", "active_phase": "active", "pending_goal_id": "p", "pending_phase": "pending_cancel", "execution": {"target": ["o"]}, "event_seq": 0}
        parsed = d.DecisionState.parse(state)
        self.assertEqual(d.DecisionState.parse(parsed.to_dict()).to_dict(), parsed.to_dict())
        state["execution"]["target"].append("changed")
        self.assertEqual(parsed.execution["target"], ["o"])
        reject(lambda: d.DecisionState.parse(dict(state, revision=-1)), "range_violation")
        reject(lambda: d.DecisionState.parse(dict(state, event_seq=math.nan)), "non_finite_number")

    def test_events_resync_and_reply_round_trip(self):
        event = {"event_id": "e", "event_seq": 2, "ex_session": "s", "task_id": "t", "goal_id": "g", "goal_revision": 0, "command_id": "c", "owner": "arm", "status": "running", "reason_code": "", "details": {}}
        reply = d.events_reply(ex_session="s", buffered=[event], oldest_available_seq=2, since_event_seq=0)
        self.assertTrue(reply.resync_required); self.assertEqual(reply.events, [])
        self.assertEqual(d.EventsReply.parse(reply.to_dict()).to_dict(), reply.to_dict())
        reject(lambda: d.EventsReply.parse(dict(reply.to_dict(), resync_required=1)), "invalid_type")
        old_event = dict(event, event_id="old", event_seq=1)
        reject(
            lambda: d.events_reply(
                ex_session="s",
                buffered=[old_event],
                oldest_available_seq=2,
                latest_event_seq=2,
                since_event_seq=0,
            ),
            "range_violation",
        )
        reject(
            lambda: d.events_reply(
                ex_session="s",
                buffered=[old_event],
                oldest_available_seq=2,
                latest_event_seq=2,
                since_event_seq=1,
            ),
            "range_violation",
        )
    def test_canceled_event_page_and_feedback_require_stop_proof(self):
        event = {"event_id": "e", "event_seq": 4, "ex_session": "s", "task_id": "t", "goal_id": "g", "goal_revision": 0, "command_id": "c", "owner": "arm", "status": "canceled", "details": {}}
        feedback = {"schema_version": 1, "ex_session": "s", "task_id": "t", "goal_id": "g", "goal_revision": 0, "event_seq": 4, "status": "canceled", "details": {}}
        for evidence, code, suffix in (
            (None, "missing_field", "stop_evidence"),
            ({"stopped": False}, "enum_violation", "stop_evidence.stopped"),
            ({"stopped": 1}, "invalid_type", "stop_evidence.stopped"),
            ({"stopped": True, "source": "x" * 257}, "text_too_long", "stop_evidence.source"),
        ):
            details = {} if evidence is None else {"stop_evidence": evidence}
            for parse, path in (
                (lambda: ActionEvent.parse(dict(event, details=details)), f"details.{suffix}"),
                (lambda: d.Feedback.parse(dict(feedback, details=details)), f"details.{suffix}"),
                (lambda: d.events_reply(ex_session="s", buffered=[dict(event, details=details)], oldest_available_seq=4, latest_event_seq=4, since_event_seq=3), f"events[0].details.{suffix}"),
            ):
                with self.subTest(code=code, path=path):
                    with self.assertRaises(d.ContractError) as caught:
                        parse()
                    self.assertEqual((caught.exception.error.code, caught.exception.error.path), (code, path))
        details = {"stop_evidence": {"stopped": True, "source": "arm", "reference": "cmd-1"}}
        self.assertEqual(ActionEvent.parse(dict(event, details=details)).to_dict()["details"], details)
        self.assertEqual(d.Feedback.parse(dict(feedback, details=details)).to_dict()["details"], details)
        page = d.events_reply(ex_session="s", buffered=[dict(event, details=details)], oldest_available_seq=4, latest_event_seq=4, since_event_seq=3)
        self.assertEqual(d.EventsReply.parse(page.to_dict()).to_dict(), page.to_dict())

    def test_event_history_reports_indexed_paths(self):
        event = {"event_id": "e", "event_seq": 4, "ex_session": "s", "task_id": "t", "goal_id": "g", "goal_revision": 0, "command_id": "c", "owner": "arm", "status": "running", "details": {}}
        for buffered, code, path in (
            ([dict(event, event_seq=0)], "range_violation", "events[0].event_seq"),
            ([event, dict(event, event_seq=4)], "range_violation", "events[1].event_seq"),
            ([dict(event, ex_session="other")], "stale_ex_session", "events[0].ex_session"),
            ([dict(event, event_seq=3)], "range_violation", "events[0].event_seq"),
        ):
            with self.subTest(path=path):
                with self.assertRaises(d.ContractError) as caught:
                    d.events_reply(ex_session="s", buffered=buffered, oldest_available_seq=4, latest_event_seq=9, since_event_seq=0)
                self.assertEqual((caught.exception.error.code, caught.exception.error.path), (code, path))

    def test_events_reject_bad_full_buffer_and_strict_page(self):
        reject(lambda: d.events_reply(ex_session="s", buffered=[None], oldest_available_seq=1, since_event_seq=0), "invalid_type")
        event = {"event_id": "e", "event_seq": 2, "ex_session": "s", "task_id": "t", "goal_id": "g", "goal_revision": 0, "command_id": "c", "owner": "arm", "status": "running", "details": {}}
        foreign = dict(event, ex_session="foreign")
        reject(lambda: d.events_reply(ex_session="s", buffered=[foreign], oldest_available_seq=2, since_event_seq=1), "stale_ex_session")
        duplicate = dict(event, event_id="e2")
        reject(lambda: d.events_reply(ex_session="s", buffered=[event, duplicate], oldest_available_seq=2, since_event_seq=1), "range_violation")
        bad = dict(event, event_seq=4)
        reject(lambda: d.events_reply(ex_session="s", buffered=[event, bad], oldest_available_seq=2, since_event_seq=1, latest_event_seq=3), "range_violation")
        reject(lambda: d.EventsReply.parse({"schema_version": 1, "ex_session": "s", "events": [event], "oldest_available_seq": 2, "latest_event_seq": 2, "resync_required": True}), "invalid_type")

    def test_bootstrap_and_empty_trimmed_latest(self):
        for method in (d.METHOD_CONTEXT_GET, d.METHOD_STATE_GET):
            self.assertEqual(d.parse_request(method, {"schema_version": 1}), {"schema_version": 1})
        reject(lambda: d.parse_request([], {}), "invalid_type")
        reject(lambda: d.parse_request({}, {}), "invalid_type")
        reply = d.events_reply(ex_session="s", buffered=[], oldest_available_seq=7, since_event_seq=0, latest_event_seq=9)
        self.assertTrue(reply.resync_required)
        self.assertEqual(reply.latest_event_seq, 9)

    def test_idempotency_validates_before_copy(self):
        payload = {"nested": {"ok": 1}}
        registry = d.DecisionIdempotency()
        self.assertEqual(registry.resolve("r", payload), "new")
        payload["nested"]["ok"] = 2
        self.assertEqual(registry.resolve("r2", {"nested": {"ok": 1}}), "new")
        huge = {"x": "x" * 1_048_577}
        reject(lambda: registry.resolve("huge", huge), "value_budget_exceeded")


if __name__ == "__main__":
    unittest.main()
