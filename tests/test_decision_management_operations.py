"""Event-controlled B08 races. No real model, weights, ROS, or robot commands."""
from __future__ import annotations

import copy
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.decision.config import ManagementError
from astrbot_ex.core.decision.management import ManagementSettings
from astrbot_ex.core.decision.owned_laya import Deployment, OwnedLayaService
from tests.test_decision_management_http import ManagementHTTPFixture
from tests.test_owned_laya import FakeBackend, FakeProcess


class DecisionManagementOperationsTests(ManagementHTTPFixture, unittest.TestCase):
    """Reuse the real loopback/API/service fixture and inject only the child boundary."""

    def setUp(self):
        self.deployment_temp = tempfile.TemporaryDirectory()
        deployment_root = Path(self.deployment_temp.name)
        (deployment_root / "cache").mkdir()
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        deployment = Deployment(Path(sys.executable), deployment_root / "cache", deployment_root / "logs",
                                port=port, device="cpu", state_path=deployment_root / "execution/laya/service-state.json")
        self.load_entered, self.load_release = threading.Event(), threading.Event()
        self.processes, self.release_events = [], [self.load_release]
        self.completed, self.done_ids = threading.Condition(), set()

        def process_factory(*args, **kwargs):
            child = FakeProcess()
            self.processes.append(child)
            return child

        def warmup(backend):
            self.load_entered.set()
            if not self.load_release.wait(3):
                raise RuntimeError("test_load_latch_not_released")
            return {"fixture": True}

        def owned_factory(value):
            self.owned = OwnedLayaService(value, process_factory=process_factory,
                                         probe_factory=FakeBackend, warmup=warmup)
            return self.owned

        settings = ManagementSettings(laya_deployment=deployment, laya_service_factory=owned_factory,
                                      operation_timeout_s=3)
        with patch("tests.test_decision_management_http.ManagementSettings", return_value=settings):
            super().setUp()
        self.management = self.server.decision_management
        config = self.get_config()
        saved = copy.deepcopy(config["saved"])
        saved["backend"] = "laya"
        saved["laya"]["service_connection"]["mode"] = "owned"
        saved["laya"]["deployment"] = {"launcher": "subprocess", "python": str(deployment.python),
                                       "cache": str(deployment.cache), "device": deployment.device}
        self.assertEqual(self.write("/config", {"config": saved}, version=config)[0], 200)
        self.owned = self.management.laya
        original = self.management._run_operation

        def observed(operation, work):
            try:
                return original(operation, work)
            finally:
                with self.completed:
                    self.done_ids.add(operation["operation_id"])
                    self.completed.notify_all()

        self.operation_observer = patch.object(self.management, "_run_operation", side_effect=observed)
        self.operation_observer.start()

    def tearDown(self):
        for release in self.release_events:
            release.set()
        try:
            super().tearDown()
        finally:
            self.operation_observer.stop()
            self.deployment_temp.cleanup()

    def finished(self, accepted):
        operation_id = accepted["operation_id"]
        with self.completed:
            self.assertTrue(self.completed.wait_for(lambda: operation_id in self.done_ids, timeout=3),
                            "management operation did not finish")
        return self.management.operation(operation_id)

    def save_laya(self):
        before = self.get_config()
        config = copy.deepcopy(before["saved"])
        config["backend"] = "laya"
        config["laya"].update(enabled=True, allow_live_http=True)
        status, saved, _ = self.write("/config", {"config": config}, version=before)
        self.assertEqual(status, 200)
        return self.get_config()

    def test_active_operation_limit_is_enforced_without_dropping_existing_operations(self):
        release, entered = threading.Event(), threading.Event()
        self.release_events.append(release)

        def held(operation):
            entered.set()
            self.assertTrue(release.wait(3))
            return {"fixture": True}

        accepted = [self.management._submit("test", held, supersede=False) for _ in range(8)]
        self.assertTrue(entered.wait(2))
        with self.assertRaises(ManagementError) as caught:
            self.management._submit("test", held, supersede=False)
        self.assertEqual(caught.exception.code, "operation_capacity")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(sum(self.management.operation(item["operation_id"])["state"] in
                             {"pending", "running"} for item in accepted), 8)
        release.set()
        self.assertTrue(all(self.finished(item)["state"] == "succeeded" for item in accepted))

    def test_completed_retention_keeps_128_without_pruning_an_active_operation(self):
        release, entered = threading.Event(), threading.Event()
        self.release_events.append(release)

        def held(operation):
            entered.set()
            self.assertTrue(release.wait(3))
            return {}

        active = self.management._submit("test", held, supersede=False)
        self.assertTrue(entered.wait(2))
        first_completed = None
        for _ in range(129):
            accepted = self.management._submit("test", lambda operation: {}, supersede=False)
            if first_completed is None:
                first_completed = accepted
            self.assertEqual(self.finished(accepted)["state"], "succeeded")
        self.assertEqual(self.management.operation(active["operation_id"])["state"], "running")
        self.assertEqual(self.management.status()["operations"]["succeeded"], 128)
        with self.assertRaises(ManagementError) as caught:
            self.management.operation(first_completed["operation_id"])
        self.assertEqual(caught.exception.code, "operation_not_found")
        release.set()
        self.assertEqual(self.finished(active)["state"], "succeeded")

    def test_repeated_start_reuses_id_and_stop_revokes_before_load_latch_releases(self):
        status, first, _ = self.write("/service/start")
        self.assertEqual(status, 202)
        self.assertTrue(self.load_entered.wait(2))
        status, second, _ = self.write("/service/start")
        self.assertEqual(status, 202)
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.processes), 1)
        self.assertIsNone(self.processes[0].poll())

        status, stopped, _ = self.write("/stop")
        self.assertEqual(status, 202)
        decision = self.server.decision_service.status()
        self.assertFalse(decision["gate_open"])
        self.assertEqual(decision["mode"], "disabled")
        self.assertFalse(self.load_release.is_set())
        self.assertIsNone(self.processes[0].poll())  # Stop did not wait for model loading.
        self.load_release.set()
        self.assertEqual(self.finished(first)["state"], "superseded")
        self.assertEqual(self.finished(stopped)["state"], "succeeded")
        self.assertIsNotNone(self.processes[0].poll())
        self.assertTrue(self.owned.history[-1]["exit_confirmed_monotonic_ns"])
        self.assertIsNone(self.server.decision_service.goals.active)

    def test_config_save_supersedes_cold_recover_and_cleans_only_its_candidate(self):
        before = self.save_laya()
        self.owned.start(warmup=False)
        old_generation = self.owned.generation
        self.owned.quarantine(old_generation, "deadline_exceeded")
        with patch.object(self.server.decision_service, "replace_backend",
                          wraps=self.server.decision_service.replace_backend) as replacement:
            status, recover, _ = self.write("/service/recover", version=before)
            self.assertEqual(status, 202)
            self.assertTrue(self.load_entered.wait(2))
            self.assertEqual(len(self.processes), 2)
            self.assertIsNotNone(self.processes[0].poll())
            self.assertIsNone(self.processes[1].poll())
            changed = copy.deepcopy(before["saved"])
            changed["laya"]["deadline_ms"] -= 1
            status, saved, _ = self.write("/config", {"config": changed}, version=before)
            self.assertEqual(status, 200)
            self.assertEqual(saved["revision"], before["revision"] + 1)
            self.assertFalse(self.load_release.is_set())  # Save did not queue behind cold loading.
            self.load_release.set()
            result = self.finished(recover)
            self.assertEqual(result["state"], "superseded")
            replacement.assert_not_called()
        self.assertIsNotNone(self.processes[1].poll())
        current = self.get_config()
        self.assertEqual(current["saved"], changed)
        self.assertEqual(current["effective_revision"], before["effective_revision"])
        self.assertEqual(self.server.decision_service.status()["mode"], "disabled")
        self.assertIsNone(self.server.decision_service.goals.active)
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())

    def test_new_stop_atomically_rejects_old_mode_at_trusted_commit_boundary(self):
        self.save_laya()
        self.load_release.set()
        self.assertEqual(self.finished(self.write("/service/start")[1])["state"], "succeeded")
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        original = self.server.decision_service.management_set_mode

        def parked(mode, **context):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(mode, **context)

        with patch.object(self.server.decision_service, "management_set_mode", side_effect=parked), \
                patch.object(self.server.decision_service, "set_mode", wraps=self.server.decision_service.set_mode) as set_mode:
            status, old_mode, _ = self.write("/mode", {"mode": "shadow"})
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(2))
            status, stopped, _ = self.write("/stop")
            self.assertEqual(status, 202)
            receipt = self.server.decision_service.status()["stop"]
            self.assertFalse(self.server.decision_service.status()["gate_open"])
            release.set()
            old_result = self.finished(old_mode)
            self.assertEqual(old_result["state"], "superseded")
            self.assertEqual(old_result["error_code"], "management_context_changed")
            self.assertEqual(self.finished(stopped)["state"], "succeeded")
            set_mode.assert_not_called()
        self.assertEqual(self.server.decision_service.status()["mode"], "disabled")
        self.assertEqual(self.server.decision_service.status()["stop"]["operation_id"], receipt["operation_id"])

    def test_invalidation_between_work_and_result_commit_cannot_report_success(self):
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        original = self.management._assert_current
        checks = 0

        def parked(operation):
            nonlocal checks
            result = original(operation)
            if operation["kind"] == "commit_race":
                checks += 1
                if checks == 2:  # Work completed; its outside-lock check just observed the old intent.
                    entered.set()
                    self.assertTrue(release.wait(3))
            return result

        with patch.object(self.management, "_assert_current", side_effect=parked):
            old = self.management._submit("commit_race", lambda operation: {"late": True}, supersede=False)
            self.assertTrue(entered.wait(2))
            status, stopped, _ = self.write("/stop")
            self.assertEqual(status, 202)
            release.set()
            result = self.finished(old)
            self.assertEqual(result["state"], "superseded")
            self.assertEqual(result["error_code"], "operation_superseded")
            self.assertIsNone(result["result"])
            self.assertEqual(self.finished(stopped)["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
