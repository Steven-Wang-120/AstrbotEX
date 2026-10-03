"""Deterministic protocol tests. These responses are not Laya model results."""
from __future__ import annotations

import copy
import importlib.util
import json
import math
import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.decision.backends import laya
from astrbot_ex.core.decision.models import DecisionSnapshot, validate_backend_selection

REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
FIXTURE = json.loads((Path(__file__).parent / "fixtures/decision/jev/normal.json").read_text())


def snapshot(*, two_owners=False):
    raw = copy.deepcopy(FIXTURE["snapshot"])
    raw["goal"].update(goal_text_en="Pick bound cup.", parameters={"arm.pick.v1": {"target": "cup-1"}})
    raw["observations"][0]["data"] = {"cup": "cup-1", "ready": True}
    for owner in raw["owners"]:
        for option in owner["candidates"]:
            option["description"] = {
                "start": "Pick bound cup.", "wait": "Wait.", "request_replan": "Ask new goal.",
                "keep": "Keep command.", "cancel": "Cancel command.",
            }[option["kind"]]
    if not two_owners:
        raw["owners"] = raw["owners"][:1]
        raw["versions"]["plugin_generations"] = {"arm": 5}
    return DecisionSnapshot.parse(raw)


def health(**changes):
    raw = {"status": "ok", "loaded": ["typed-decisions"],
           "revisions": {"typed-decisions": REVISION}, "device": "cpu", "device_is_preference": False,
           "checkpoint_devices": {"typed-decisions": "cpu"},
           "cpu_fallbacks": {"typed-decisions": {"count": 0, "last_reason": None}}}
    raw.update(changes)
    return raw


def response(questions, *, choices=None):
    answers = {}
    for index, (question, item) in enumerate(questions.items()):
        keys = list(item["criteria"])
        chosen = (choices or {}).get(question, "A")
        answers[question] = {"type": "choice", "choice": chosen,
            "probabilities": {key: float(key == chosen) for key in keys},
            "confidence": 1.0, "answer_confidence": 1.0,
            "action": {"act_probability": 0.5}}
    return {"model": "laya-rl-agent", "answers": answers,
            "usage": {"input_tokens": 80, "output_tokens": 0, "state_tokens": 32,
                      "state_tokens_dropped": 0, "truncated": False, "truncated_questions": []},
            "routing": {"model": "typed-decisions", "repo": "convaiinnovations/laya/typed-decisions",
                        "reason": "explicit model='typed-decisions'", "detection": None, "workflow": None}}


def reply(raw, status=200):
    return laya.HTTPReply(status, json.dumps(raw, allow_nan=True).encode())


class FixtureTransport:
    """Only fixture HTTP shape; never contacts or loads the model."""
    def __init__(self, *, mutate=None, choices=None, post=None, health_value=None):
        self.mutate, self.choices, self.post = mutate, choices, post
        self.health_value = health_value
        self.calls = []
        self.requests = []

    def __call__(self, method, path, body, deadline, cancel, max_bytes):
        self.calls.append((method, path))
        if method == "GET":
            return reply(self.health_value if self.health_value is not None else health())
        request = json.loads(body)
        self.requests.append(request)
        if self.post is not None:
            return self.post(method, path, body, deadline, cancel, max_bytes)
        raw = response(request["questions"], choices=self.choices)
        if self.mutate is not None:
            self.mutate(raw)
        return reply(raw)


class LayaBackendTests(unittest.TestCase):
    def backend(self, transport=None, **kwargs):
        backend = laya.LayaBackend(replace(laya.LayaConfig(enabled=True), **kwargs),
                                   transport=transport or FixtureTransport())
        self.addCleanup(backend.close)
        return backend

    def rejected(self, backend, code=None, value=None):
        with self.assertRaises(laya.LayaBackendError) as caught:
            backend.decide(value if value is not None else snapshot())
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        return caught.exception.code

    def thread_decide(self, backend, value=None):
        results, errors = [], []
        def run():
            try:
                results.append(backend.decide(value if value is not None else snapshot()))
            except laya.LayaBackendError as exc:
                errors.append(exc.code)
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(lambda: thread.join(2))
        return thread, results, errors

    def test_normal_copy_deterministic_mapping_and_identity(self):
        transport = FixtureTransport(choices={"q0": "A", "q1": "C"})
        backend = self.backend(transport)
        value = snapshot(two_owners=True)
        frozen = value.to_dict()
        decision = backend.decide(value)
        self.assertEqual(value.to_dict(), frozen)
        self.assertEqual(decision.backend, "laya")
        self.assertEqual(decision.snapshot_id, value.snapshot_id)
        self.assertEqual(decision.versions.to_dict(), value.versions.to_dict())
        self.assertIn("typed-decisions", decision.model)
        selected = validate_backend_selection(value, decision, value.versions)
        self.assertEqual([item["kind"] for item in selected], ["start", "cancel"])
        request = transport.requests[0]
        self.assertEqual(request["model"], "typed-decisions")
        self.assertEqual(set(request["questions"]), {"q0", "q1"})
        self.assertEqual(set(request["questions"]["q0"]["criteria"]), {"A", "B", "C"})
        self.assertIn("Pick bound cup.", request["questions"]["q0"]["criteria"]["A"])
        self.assertNotIn("arm-blocked", json.dumps(request))
        self.assertIsInstance(request["state"], str)
        self.assertTrue(request["state"].isascii())
        self.assertIn("Pick bound cup.", request["state"])
        self.assertIn("cup-1", request["state"])
        self.assertIn("ready", request["state"])
        self.assertEqual(backend.config.revision, REVISION)
        backend.decide(value)
        self.assertEqual(transport.requests[0], transport.requests[1])

    def test_caller_mutation_and_last_record_are_defensive(self):
        value = snapshot()
        frozen = value.to_dict()
        transport = FixtureTransport()
        original = transport.__call__
        def mutate(method, *args):
            if method == "POST":
                value.goal["parameters"].clear()
                value.owners.clear()
                value.versions.goal_revision = 99
            return original(method, *args)
        backend = self.backend(mutate)
        decision = backend.decide(value)
        self.assertEqual(decision.versions.to_dict(), frozen["versions"])
        self.assertEqual(len(validate_backend_selection(DecisionSnapshot.parse(frozen), decision,
                                                        DecisionSnapshot.parse(frozen).versions)), 1)
        record = backend.last_record
        record["caller_mutation"] = True
        self.assertNotIn("caller_mutation", backend.last_record)
        state = backend.status()
        state["caller_mutation"] = True
        self.assertNotIn("caller_mutation", backend.status())

    def test_missing_extra_owner_unknown_crossowner_and_ineligible_choices(self):
        mutations = [
            lambda x: x["answers"].pop("q0"),
            lambda x: x["answers"].update(q99=x["answers"]["q0"]),
            lambda x: x["answers"]["q0"].update(choice="arm-start"),
            lambda x: x["answers"]["q0"].update(choice="q1:A"),
            lambda x: x["answers"]["q0"].update(choice="D"),
            lambda x: x["answers"]["q0"].update(params={"meters": 999}),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                transport = FixtureTransport(mutate=mutation)
                backend = self.backend(transport)
                self.rejected(backend)
                self.assertEqual(len(transport.requests), 1)

    def test_protocol_identity_routing_and_shape_fail_closed(self):
        mutations = [lambda x: x.update(model="jev-1.13.0"),
            lambda x: x["routing"].update(model="systemone"),
            lambda x: x["routing"].update(repo="other/repo"),
            lambda x: x["routing"].update(workflow="automatic"),
            lambda x: x.update(execution_allowed=True),
            lambda x: x["answers"]["q0"].update(type="text"),
            lambda x: x["answers"]["q0"].pop("answer_confidence"),
            lambda x: x["answers"]["q0"].update(action={"action_id": "arm.pick.v99"}),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.rejected(self.backend(FixtureTransport(mutate=mutation)))

    def test_health_requires_pinned_resident_checkpoint_device_and_no_post(self):
        invalid = [health(revisions={"typed-decisions": "other"}), health(loaded=[]),
                   health(checkpoint_devices={}), health(device_is_preference=True),
                   health(checkpoint_devices={"typed-decisions": "unknown"})]
        for raw in invalid:
            with self.subTest(raw=raw):
                transport = FixtureTransport(health_value=raw)
                self.rejected(self.backend(transport))
                self.assertEqual(transport.requests, [])
        transport = FixtureTransport()
        backend = self.backend(transport)
        probed = backend.probe()
        self.assertIsInstance(probed, dict)
        self.assertEqual(transport.requests, [])
        self.assertTrue(transport.calls)
        self.assertTrue(all(method == "GET" for method, _ in transport.calls))
        self.assertIsNone(backend.last_record)

    def test_distribution_rounded_normalization_preserves_raw_and_frozen_contract(self):
        def rounded(raw):
            raw["answers"]["q0"].update(choice="A", probabilities={"A": 0.3333, "B": 0.3333, "C": 0.3333},
                                           confidence=0.0, answer_confidence=0.3333)
        backend = self.backend(FixtureTransport(mutate=rounded))
        value = snapshot()
        decision = backend.decide(value)
        self.assertAlmostEqual(math.fsum(decision.choices[0]["probabilities"].values()), 1.0, delta=1e-12)
        self.assertEqual(decision.choices[0]["confidence"], 0.3333)
        validate_backend_selection(value, decision, value.versions)
        rendered = json.dumps(backend.last_record, sort_keys=True)
        self.assertIn("0.3333", rendered)
        self.assertIn("normal", rendered.lower())
        for distribution in ({"A": 0.3332, "B": 0.3333, "C": 0.3333},
                             {"A": 0.9, "B": 0.05, "C": 0.0498}):
            def invalid(raw, p=distribution):
                raw["answers"]["q0"].update(probabilities=p, choice=max(p, key=p.get), answer_confidence=max(p.values()))
            self.rejected(self.backend(FixtureTransport(mutate=invalid)))

    def test_probabilities_confidences_and_action_diagnostic_reject_invalid_numbers(self):
        cases = [{"A": True, "B": 0, "C": 0}, {"A": -0.1, "B": 1, "C": 0.1},
                 {"A": 1.1, "B": 0, "C": 0}, {"A": 1}, {"A": 0.9, "B": 0.1, "C": 0, "D": 0},
                 {"A": "0.9", "B": 0.1, "C": 0}, {"A": float("nan"), "B": 0.1, "C": 0}]
        for distribution in cases:
            with self.subTest(distribution=distribution):
                self.rejected(self.backend(FixtureTransport(mutate=lambda x, p=distribution:
                    x["answers"]["q0"].update(probabilities=p))))
        for field in ("confidence", "answer_confidence"):
            for value in (True, -0.1, 1.1, "high", float("inf")):
                with self.subTest(field=field, value=value):
                    self.rejected(self.backend(FixtureTransport(mutate=lambda x, f=field, v=value:
                        x["answers"]["q0"].update({f: v}))))
        self.rejected(self.backend(FixtureTransport(mutate=lambda x:
            x["answers"]["q0"].update(choice="B"))))
        self.rejected(self.backend(FixtureTransport(mutate=lambda x:
            x["answers"]["q0"]["action"].update(act_probability=True))))

    def test_duplicate_json_keys_invalid_utf8_and_nonfinite_exponent(self):
        for body in (b'{"model":"laya-rl-agent","model":"laya-rl-agent"}', b'\xff', b'[[[', b'{"model":NaN}'):
            transport = FixtureTransport(post=lambda *args, b=body: laya.HTTPReply(200, b))
            self.rejected(self.backend(transport))
        def huge(*args):
            request = json.loads(args[2])
            body = json.dumps(response(request["questions"])).replace('"A": 1.0', '"A": 1e999').encode()
            return laya.HTTPReply(200, body)
        self.rejected(self.backend(FixtureTransport(post=huge)))
        def duplicate_owner(*args):
            raw = response(json.loads(args[2])["questions"])
            body = json.dumps(raw).replace('"answers": {',
                '"answers": {"q0": ' + json.dumps(raw["answers"]["q0"]) + ', ', 1).encode()
            return laya.HTTPReply(200, body)
        self.rejected(self.backend(FixtureTransport(post=duplicate_owner)), "invalid_response_json")
        def duplicate_probability(*args):
            raw = response(json.loads(args[2])["questions"])
            body = json.dumps(raw).replace('"A": 1.0', '"A": 1.0, "A": 1.0', 1).encode()
            return laya.HTTPReply(200, body)
        self.rejected(self.backend(FixtureTransport(post=duplicate_probability)), "invalid_response_json")

    def test_input_request_response_and_model_token_budgets_never_silently_truncate(self):
        limits = [("max_owners", 1, snapshot(two_owners=True)), ("max_candidates_per_owner", 2, snapshot()),
                  ("max_request_bytes", 100, snapshot()), ("max_response_bytes", 100, snapshot())]
        for field, limit, value in limits:
            with self.subTest(field=field):
                self.rejected(self.backend(**{field: limit}), value=value)
        oversized = FixtureTransport(post=lambda *args: laya.HTTPReply(200, b"x" * 1001))
        self.rejected(self.backend(oversized, max_response_bytes=1000))
        self.assertEqual(len(oversized.requests), 1)
        value = snapshot()
        value.goal["goal_text_en"] = "x" * 2000
        transport = FixtureTransport()
        self.rejected(self.backend(transport), value=value)
        self.assertEqual(transport.requests, [])
        for mutation in (lambda x: x["usage"].update(truncated=True),
                         lambda x: x["usage"].update(state_tokens_dropped=1),
                         lambda x: x["usage"].update(truncated_questions=["q0"]),
                         lambda x: x["usage"].update(options={"q0": {"A": ["A", "B"]}}),
                         lambda x: x["usage"].update(input_tokens=True)):
            with self.subTest(mutation=mutation):
                self.rejected(self.backend(FixtureTransport(mutate=mutation)))

    def test_default_import_constructor_has_no_network_model_or_execution(self):
        before = set(sys.modules)
        threads = set(threading.enumerate())
        name = "astrbot_ex.core.decision.backends._laya_import_probe"
        spec = importlib.util.spec_from_file_location(name, laya.__file__)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            with patch("http.client.HTTPConnection", side_effect=AssertionError("no HTTP on construction")):
                spec.loader.exec_module(module)
                backend = module.LayaBackend()
            self.assertFalse(backend.execution_allowed)
            with self.assertRaises(module.LayaBackendError) as caught:
                backend.decide(snapshot())
            self.assertEqual(caught.exception.code, "disabled")
            backend.close()
        finally:
            sys.modules.pop(name, None)
        self.assertEqual(set(threading.enumerate()), threads)
        self.assertNotIn("torch", set(sys.modules) - before)
        self.assertNotIn("transformers", set(sys.modules) - before)
        backend = self.backend()
        self.assertFalse(backend.execution_allowed)
        backend = laya.LayaBackend(laya.LayaConfig(enabled=True), transport=FixtureTransport(), allow_test_execution=True)
        self.addCleanup(backend.close)
        self.assertTrue(backend.execution_allowed)

    def test_live_http_requires_explicit_opt_in_and_fixed_model_alias(self):
        self.rejected(laya.LayaBackend(laya.LayaConfig(enabled=True)), "live_http_not_authorized")
        for model in ("latest", "router", "jev-1.13.0"):
            with self.subTest(model=model):
                with self.assertRaises(laya.LayaBackendError):
                    laya.LayaBackend(replace(laya.LayaConfig(), model=model))

    def test_single_live_rejects_second_call_without_queue(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(2)
            return reply(response(json.loads(args[2])["questions"]))
        transport = FixtureTransport(post=blocked)
        backend = self.backend(transport)
        thread, results, errors = self.thread_decide(backend)
        try:
            self.assertTrue(entered.wait(1))
            self.rejected(backend, "busy")
            self.assertEqual(len(transport.requests), 1)
            release.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
        finally:
            release.set()

    def test_sent_timeout_is_latched_restart_required_and_probe_cannot_restore(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(2)
            finished.set()
            return reply(response(json.loads(args[2])["questions"]))
        transport = FixtureTransport(post=blocked)
        backend = self.backend(transport, deadline_ms=40)
        try:
            self.rejected(backend, "deadline_exceeded")
            self.assertTrue(entered.is_set())
            self.assertTrue(backend.status()["restart_required"])
            self.rejected(backend, "restart_required")
            backend.probe()
            self.assertTrue(backend.status()["restart_required"])
            release.set()
            self.assertTrue(finished.wait(1))
            self.rejected(backend, "restart_required")
            self.assertEqual(len(transport.requests), 1)
        finally:
            release.set()

    def test_sent_cancel_and_close_reject_late_results_without_claiming_remote_stop(self):
        for operation, code in (("cancel", "canceled"), ("close", "closed")):
            with self.subTest(operation=operation):
                entered, release, finished = threading.Event(), threading.Event(), threading.Event()
                def blocked(*args):
                    entered.set()
                    release.wait(2)
                    finished.set()
                    return reply(response(json.loads(args[2])["questions"]))
                transport = FixtureTransport(post=blocked)
                backend = self.backend(transport)
                thread, results, errors = self.thread_decide(backend)
                try:
                    self.assertTrue(entered.wait(1))
                    start = time.monotonic()
                    getattr(backend, operation)()
                    thread.join(0.5)
                    self.assertFalse(thread.is_alive())
                    self.assertLess(time.monotonic() - start, 0.5)
                    self.assertEqual(errors, [code])
                    self.assertEqual(results, [])
                    self.assertTrue(backend.status()["restart_required"])
                    self.assertFalse(finished.is_set())
                    release.set()
                    self.assertTrue(finished.wait(1))
                    self.assertEqual(results, [])
                    self.assertTrue(backend.status()["restart_required"])
                    backend.close()
                finally:
                    release.set()

    def test_presend_cancel_never_sends_and_does_not_require_restart(self):
        entered, release = threading.Event(), threading.Event()
        base = FixtureTransport()
        def preflight(method, *args):
            if method == "GET":
                entered.set()
                release.wait(2)
            return base(method, *args)
        backend = self.backend(preflight)
        thread, results, errors = self.thread_decide(backend)
        try:
            self.assertTrue(entered.wait(1))
            backend.cancel()
            thread.join(0.5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["canceled"])
            self.assertEqual(results, [])
            self.assertFalse(backend.status()["restart_required"])
            release.set()
            self.assertEqual(base.requests, [])
        finally:
            release.set()

    def test_presend_timeout_never_sends_and_can_recover_after_worker_finishes(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        base = FixtureTransport()
        def preflight(method, *args):
            if method == "GET" and not release.is_set():
                entered.set()
                release.wait(2)
                finished.set()
            return base(method, *args)
        backend = self.backend(preflight, deadline_ms=40)
        try:
            self.rejected(backend, "deadline_exceeded")
            self.assertTrue(entered.is_set())
            self.assertFalse(backend.status()["restart_required"])
            self.assertEqual(base.requests, [])
            release.set()
            self.assertTrue(finished.wait(1))
            end = time.monotonic() + 1
            idle = threading.Event()
            while backend.status()["busy"] and time.monotonic() < end:
                idle.wait(0.003)
            self.assertFalse(backend.status()["busy"])
            self.assertEqual(base.requests, [])
            self.assertEqual(backend.decide(snapshot()).choices[0]["option_id"], "arm-start")
            self.assertEqual(len(base.requests), 1)
        finally:
            release.set()

    def test_post_transport_uncertainty_requires_restart_without_retry(self):
        def uncertain(*args):
            raise OSError("request may have reached the server")
        transport = FixtureTransport(post=uncertain)
        backend = self.backend(transport)
        self.rejected(backend, "transport_failure")
        self.assertTrue(backend.status()["restart_required"])
        self.rejected(backend, "restart_required")
        self.assertEqual(len(transport.requests), 1)

    def test_explicit_admission_rejection_does_not_retry_or_fake_success(self):
        transport = FixtureTransport(post=lambda *args: reply({"detail": "server busy"}, status=503))
        backend = self.backend(transport)
        self.rejected(backend)
        self.assertEqual(len(transport.requests), 1)


if __name__ == "__main__":
    unittest.main()
