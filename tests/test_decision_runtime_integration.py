import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import json
import urllib.request
from astrbot_ex.core.api_server import AstrBotEXHTTPServer, AstrBotEXRequestHandler, RuntimeController, build_server
from astrbot_ex.core.models import RuntimeState, WorldState
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.runtime import AstrBotEXRuntime
from tests import test_decision_service as service_tests
from tests.test_decision_service import wait_for
from tests.test_goal_manager import goal_payload
from astrbot_ex.core.decision.backends import MockBackend


class JevServiceCompositionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = service_tests.DecisionServiceTests()
        self.fixture.setUp()
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.requests = []
        self.kind = "start"

    def tearDown(self):
        self.release.set()
        self.fixture.tearDown()
        if getattr(self, "backend", None) and self.backend._worker:
            self.backend._worker.join(1)
            self.assertFalse(self.backend._worker.is_alive())

    def transport(self, body, headers, deadline, cancel, limit):
        from astrbot_ex.core.decision.backends.jev import HTTPReply
        request = json.loads(body)
        self.requests.append(request)
        self.entered.set()
        self.release.wait(2)
        answers = {}
        for owner in request["state"]["snapshot"]["owners"]:
            options = [c for c in owner["candidates"] if c["eligible"]]
            selected = next((c for c in options if c["kind"] == self.kind),
                            next(c for c in options if c["kind"] == "wait"))
            answers[owner["owner"]] = {"type": "choice", "choice": selected["option_id"], "confidence": 1,
                "probabilities": {c["option_id"]: int(c is selected) for c in options}}
        return HTTPReply(200, json.dumps({"model": request["model"], "answers": answers,
            "usage": {"input_tokens": 1, "output_tokens": 1}}).encode())

    def create(self):
        from astrbot_ex.core.decision.backends.jev import JevBackend, JevConfig
        self.backend = JevBackend(JevConfig(mode="shadow", min_interval_ms=0),
                                  transport=self.transport, secret_provider=lambda: "offline-composition-only")
        return self.fixture.create(backend=self.backend, decision_mode="shadow")

    def assert_no_effects(self, service):
        self.assertEqual(self.fixture.plugins["arm"].commands, [])
        self.assertEqual(self.fixture.plugins["arm"].cancels, [])
        self.assertTrue(self.fixture.actors["arm"].alive)
        self.assertFalse(service.status()["gate_open"])

    def test_jev_execute_rejected_for_disabled_and_shadow_configuration(self):
        from dataclasses import replace
        service = self.create()
        for mode in ("shadow", "disabled"):
            service.reconfigure_backend(replace(self.backend.config, mode=mode))
            before = service.config_revision
            with self.assertRaisesRegex(RuntimeError, "backend_execute_not_allowed"):
                service.set_mode("execute")
            self.assertEqual(service.mode, "shadow")
            self.assertEqual(service.config_revision, before)
        with self.assertRaises(AttributeError):
            self.backend.execution_allowed = True
        self.assertEqual(self.requests, [])
        self.assert_no_effects(service)

    def test_jev_shadow_start_selection_does_not_execute_enable_or_renew(self):
        service = self.create()
        with patch.object(self.fixture.actions, "start", wraps=self.fixture.actions.start) as starts, \
                patch.object(self.fixture.actions, "cancel", wraps=self.fixture.actions.cancel) as cancels, \
                patch.object(service, "renew_goal", wraps=service.renew_goal) as renews, \
                patch.object(self.fixture.actors["arm"], "set_lifecycle_ready", wraps=self.fixture.actors["arm"].set_lifecycle_ready) as enabled:
            self.fixture.submit()
            self.assertTrue(wait_for(lambda: any(d["outcome"] == "shadow" for d in service.status()["decisions"])))
            selection = next(d for d in service.status()["decisions"] if d["outcome"] == "shadow")
            self.assertEqual(selection["choices"][0]["kind"], "start")
            starts.assert_not_called()
            cancels.assert_not_called()
            renews.assert_not_called()
            enabled.assert_not_called()
            self.assertEqual(self.fixture.ledger.list_commands().result(1), ())
        self.assert_no_effects(service)
        self.assertEqual(self.backend.last_record.reject_code, None)

    def test_jev_shadow_cancel_choice_has_zero_cancel_and_lease_side_effect(self):
        from astrbot_ex.core.actions.models import ActionCommand
        from astrbot_ex.core.actions.ledger import OwnerBinding, StopEvidence
        service = self.create()
        self.kind = "wait"
        self.fixture.submit()
        self.assertTrue(wait_for(lambda: service.goals.phase == "active"))
        command = ActionCommand.parse({"schema_version": 1, "command_id": "shadow-existing", "ex_session": service.goals.ex_session,
            "goal_id": "goal-1", "goal_revision": 1, "decision_id": "fixture-only", "owner": "arm", "plugin_generation": 1,
            "action_id": "arm.move.v1", "operation": "start", "params": {"meters": 1}, "lease_ms": 1000})
        binding = OwnerBinding("arm", 1)
        self.fixture.ledger.admit(command, (), binding, task_id="task").result(1)
        self.fixture.ledger.report(command.command_id, binding, "accepted").result(1)
        before = service.goals.active.expires_ns
        try:
            self.kind = "cancel"
            with patch.object(self.fixture.actions, "cancel", wraps=self.fixture.actions.cancel) as cancel, \
                    patch.object(service, "renew_goal", wraps=service.renew_goal) as renew:
                service.tick()
                self.assertTrue(wait_for(lambda: any(d["outcome"] == "shadow" and d["choices"][0]["kind"] == "cancel"
                                                      for d in service.status()["decisions"])))
                cancel.assert_not_called()
                renew.assert_not_called()
            self.assertEqual(service.goals.active.expires_ns, before)
            self.assertEqual(self.fixture.ledger.get(command.command_id).result(1).status, "accepted")
            self.assert_no_effects(service)
        finally:
            # This synthetic Ledger-only row never reached an Actor. Reconcile
            # requires uncertain terminal ownership (ledger.py:461-463).
            self.fixture.ledger.report(command.command_id, binding, "unknown").result(1)
            self.fixture.ledger.reconcile_stop(command.command_id, binding,
                StopEvidence(command.command_id, True, "fixture", command.command_id)).result(1)

    def test_jev_late_goal_result_is_discarded_by_real_service(self):
        self.release.clear()
        service = self.create()
        self.fixture.submit()
        self.assertTrue(self.entered.wait(1))
        old_id = self.requests[0]["state"]["snapshot"]["snapshot_id"]
        self.fixture.submit(2)
        self.release.set()
        self.assertTrue(wait_for(lambda: any(d["snapshot_id"] == old_id and d["reason_code"] == "goal_revision_changed"
                                              for d in service.status()["decisions"])))
        self.assertFalse(any(d["snapshot_id"] == old_id and d["outcome"] == "shadow" for d in service.status()["decisions"]))
        self.assertEqual(self.fixture.ledger.list_commands().result(1), ())
        self.assert_no_effects(service)

    def test_jev_shadow_gate_still_blocks_admission_if_mode_field_is_misused(self):
        service = self.create()
        applying, resume = threading.Event(), threading.Event()
        original = service._apply
        def parked(snapshot, decision):
            applying.set()
            resume.wait(2)
            return original(snapshot, decision)
        try:
            with patch.object(service, "_apply", side_effect=parked):
                self.fixture.submit()
                self.assertTrue(applying.wait(1))
                service.mode = "execute"  # trusted-code misuse must not bypass the final capability check
                resume.set()
                self.assertTrue(wait_for(lambda: any(d["reason_code"] == "backend_execute_not_allowed"
                                                      for d in service.status()["decisions"])))
            self.assertEqual(self.fixture.ledger.list_commands().result(1), ())
            self.assert_no_effects(service)
        finally:
            resume.set()

    def test_jev_config_change_during_transport_updates_ex_revision_and_no_dispatch(self):
        from dataclasses import replace
        self.release.clear()
        service = self.create()
        self.fixture.submit()
        self.assertTrue(self.entered.wait(1))
        before = service.config_revision
        old_id = self.requests[0]["state"]["snapshot"]["snapshot_id"]
        service.reconfigure_backend(replace(self.backend.config, model="jev-1.14.0"))
        self.assertEqual(service.config_revision, before + 1)
        self.assertEqual(self.backend.config.model, "jev-1.14.0")
        self.release.set()
        self.assertTrue(wait_for(lambda: any(d["snapshot_id"] == old_id and d["outcome"] == "discarded"
                                              for d in service.status()["decisions"])))
        self.assertFalse(any(d["snapshot_id"] == old_id and d["outcome"] == "shadow" for d in service.status()["decisions"]))
        self.assertEqual(self.fixture.ledger.list_commands().result(1), ())
        self.assert_no_effects(service)

    def test_jev_returned_result_config_change_uses_ex_revision_not_only_backend_epoch(self):
        from dataclasses import replace
        service = self.create()
        applying, resume = threading.Event(), threading.Event()
        original = service._apply
        def parked(snapshot, decision):
            applying.set()
            resume.wait(2)
            return original(snapshot, decision)
        try:
            with patch.object(service, "_apply", side_effect=parked):
                self.fixture.submit()
                self.assertTrue(applying.wait(1))
                old = self.requests[0]["state"]["snapshot"]
                self.assertEqual(self.backend.last_record.reject_code, None)
                before = service.config_revision
                service.reconfigure_backend(replace(self.backend.config, min_confidence=0.7))
                self.assertEqual(service.config_revision, before + 1)
                self.assertEqual(service._versions(service.catalog.snapshot()).config_revision, old["versions"]["config_revision"] + 1)
                resume.set()
                self.assertTrue(wait_for(lambda: any(d["snapshot_id"] == old["snapshot_id"] and d["reason_code"] == "config_revision_changed"
                                                      for d in service.status()["decisions"])))
            self.assertEqual(self.fixture.ledger.list_commands().result(1), ())
            self.assert_no_effects(service)
        finally:
            resume.set()


class RuntimeDecisionIntegrationTests(unittest.TestCase):
    def test_G09_no_goal_no_motion_interaction_and_perception_continue(self):
        interaction = Mock()
        perception = Mock()
        perception.update.return_value = WorldState()
        runtime = AstrBotEXRuntime(PluginRegistry(), interaction_core=interaction, perception=perception)
        runtime.state = RuntimeState.RUNNING
        runtime.tick()
        perception.update.assert_called_once()
        interaction.tick.assert_called_once()
        self.assertIsNone(runtime.active_skill)

    def test_build_server_disabled_default_status_cached_and_close_reclaims(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
                "ASTRBOTEX_DATA_DIR": root, "ASTRBOTEX_STT_ENABLED": "", "ASTRBOTEX_TTS_ENABLED": ""}):
            server = build_server("127.0.0.1", 0, 20)
            try:
                self.assertEqual(server.decision_service.mode, "disabled")
                self.assertIs(server.controller.runtime.decision_service, server.decision_service)
                self.assertIs(server.environment_manager.decision_service, server.decision_service)
                with patch.object(server.action_service, "status", side_effect=AssertionError("SQLite status waited")):
                    state = server.controller.status()
                self.assertFalse(state["actions"]["gate_open"])
                self.assertEqual(state["control_mode"], "legacy")
            finally:
                server.server_close()
            self.assertFalse(server.decision_service._control.is_alive())
            self.assertFalse(server.decision_service._backend_worker.is_alive())

    def test_G04_controller_status_and_stop_do_not_wait_latched_backend(self):
        fixture = service_tests.DecisionServiceTests()
        fixture.setUp()
        try:
            backend = MockBackend(kind="start", block=True)
            service = fixture.create(backend=backend)
            runtime = AstrBotEXRuntime(PluginRegistry(), action_service=fixture.actions, decision_service=service)
            runtime.state = RuntimeState.RUNNING
            controller = RuntimeController(runtime)
            service.submit_goal(goal_payload(service.goals))
            self.assertTrue(backend.entered.wait(1))
            begin = time.monotonic()
            self.assertTrue(controller.status()["actions"]["backend_live"])
            controller.stop("latched backend stop")
            self.assertLess(time.monotonic() - begin, 0.1)
            self.assertFalse(service.status()["gate_open"])
            self.assertFalse(backend.release.is_set())
        finally:
            fixture.tearDown()

    def test_G04_real_http_status_available_while_backend_is_latched(self):
        fixture = service_tests.DecisionServiceTests()
        fixture.setUp()
        server = None
        try:
            backend = MockBackend(block=True)
            service = fixture.create(backend=backend)
            runtime = AstrBotEXRuntime(PluginRegistry(), action_service=fixture.actions, decision_service=service)
            runtime.state = RuntimeState.RUNNING
            server = AstrBotEXHTTPServer(("127.0.0.1", 0), AstrBotEXRequestHandler)
            server.controller = RuntimeController(runtime)
            serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            serving.start()
            service.submit_goal(goal_payload(service.goals))
            self.assertTrue(backend.entered.wait(1))
            begin = time.perf_counter_ns()
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/v1/ex/status", timeout=1) as response:
                state = json.load(response)
            self.assertLess((time.perf_counter_ns() - begin) / 1e9, 0.1)
            self.assertTrue(state["actions"]["backend_live"])
            self.assertFalse(backend.release.is_set())
        finally:
            if server:
                server.shutdown()
                serving.join(1)
                server.server_close()
            fixture.tearDown()

    def test_G07_environment_switch_hook_closes_gate_before_transition(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"ASTRBOTEX_DATA_DIR": root}):
            server = build_server("127.0.0.1", 0, 20)
            try:
                observed = []
                with patch.object(server.decision_service, "request_stop", side_effect=lambda why: observed.append(why)), \
                        patch.object(server.environment_manager, "_transition", return_value=None):
                    server.environment_manager.select("ros2")
                    server.environment_manager._worker.join(1)
                self.assertIn("environment_switch", observed)
                self.assertFalse(server.decision_service.status()["gate_open"])
            finally:
                server.server_close()

    def test_review_close_failure_still_reclaims_action_workers_and_socket(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"ASTRBOTEX_DATA_DIR": root}):
            server = build_server("127.0.0.1", 0, 20)
            original = server.decision_service.close
            try:
                with patch.object(server.decision_service, "close", side_effect=RuntimeError("injected decision close")):
                    with self.assertRaisesRegex(RuntimeError, "injected decision close"):
                        server.server_close()
                self.assertTrue(server.action_ledger.health.closed)
                self.assertFalse(server.action_dispatcher._worker.is_alive())
                self.assertEqual(server.socket.fileno(), -1)
            finally:
                original()
                server.action_service.close()

    def test_review_noncooperative_backend_close_reports_timeout_and_reclaims_action_socket(self):
        class Noncooperative(MockBackend):
            def close(self):
                pass
        fixture = service_tests.DecisionServiceTests()
        fixture.setUp()
        backend = Noncooperative(block=True)
        server = None
        try:
            service = fixture.create(backend=backend)
            runtime = AstrBotEXRuntime(PluginRegistry(), action_service=fixture.actions, decision_service=service)
            runtime.state = RuntimeState.RUNNING
            server = AstrBotEXHTTPServer(("127.0.0.1", 0), AstrBotEXRequestHandler)
            server.controller = RuntimeController(runtime)
            server.decision_service, server.action_service = service, fixture.actions
            service.submit_goal(goal_payload(service.goals))
            self.assertTrue(backend.entered.wait(1))
            original = service.close
            with patch.object(service, "close", side_effect=lambda: original(timeout=0.03)):
                with self.assertRaisesRegex(TimeoutError, "did not cooperate"):
                    server.server_close()
            self.assertTrue(fixture.ledger.health.closed)
            self.assertFalse(fixture.dispatcher._worker.is_alive())
            self.assertEqual(server.socket.fileno(), -1)
            self.assertFalse(service._control.is_alive())
            self.assertTrue(service.observations.status()["closed"])
        finally:
            backend.release.set()
            if fixture.service:
                fixture.service._backend_worker.join(1)
            if server:
                server.socket.close()
            fixture.tearDown()

    def test_review_multiple_close_errors_are_reported_after_cleanup(self):
        server = AstrBotEXHTTPServer(("127.0.0.1", 0), AstrBotEXRequestHandler)
        server.connections = Mock(close=Mock(side_effect=ValueError("connection close failed")))
        server.decision_service = Mock(close=Mock(side_effect=RuntimeError("decision close failed")))
        server.action_service = Mock(close=Mock(side_effect=OSError("action close failed")))
        try:
            from astrbot_ex.core.decision.service import ShutdownErrors
            with patch("builtins.ExceptionGroup", create=True, side_effect=AssertionError("3.11-only builtin used")), \
                    self.assertRaises(ShutdownErrors) as error:
                server.server_close()
            self.assertEqual({str(exc) for exc in error.exception.exceptions},
                {"connection close failed", "decision close failed", "action close failed"})
            server.action_service.close.assert_called_once()
            self.assertEqual(server.socket.fileno(), -1)
        finally:
            server.socket.close()

    def test_python310_socket_close_error_does_not_drop_other_shutdown_errors(self):
        from http.server import ThreadingHTTPServer
        from astrbot_ex.core.decision.service import ShutdownErrors
        server = AstrBotEXHTTPServer(("127.0.0.1", 0), AstrBotEXRequestHandler)
        decision_error, socket_error = ValueError("decision failed"), OSError("socket cleanup failed")
        server.decision_service = Mock(close=Mock(side_effect=decision_error))
        server.action_service = Mock()
        original = ThreadingHTTPServer.server_close
        def close_socket(instance):
            original(instance)
            raise socket_error
        try:
            with patch.object(ThreadingHTTPServer, "server_close", close_socket), \
                    patch("builtins.ExceptionGroup", create=True, side_effect=AssertionError("3.11-only builtin used")), \
                    self.assertRaises(ShutdownErrors) as error:
                server.server_close()
            self.assertEqual(error.exception.exceptions, (decision_error, socket_error))
            server.action_service.close.assert_called_once()
            self.assertEqual(server.socket.fileno(), -1)
        finally:
            server.socket.close()

    def test_G08_pause_then_runtime_start_needs_fresh_goal(self):
        fixture = service_tests.DecisionServiceTests()
        fixture.setUp()
        try:
            service = fixture.create(backend=MockBackend(kind="wait"))
            runtime = AstrBotEXRuntime(PluginRegistry(), action_service=fixture.actions, decision_service=service)
            runtime.state = RuntimeState.RUNNING
            service.submit_goal(goal_payload(service.goals))
            self.assertTrue(wait_for(lambda: service.goals.phase == "active"))
            runtime.pause()
            self.assertFalse(service.status()["gate_open"])
            self.assertTrue(wait_for(lambda: service.goals.phase == "idle"))
            runtime.start()
            self.assertFalse(service.status()["gate_open"])
            self.assertIsNone(service.goals.active)
        finally:
            fixture.tearDown()
