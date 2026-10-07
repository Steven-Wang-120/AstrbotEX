"""EX projection validator/fences; real bidirectional AEB acceptance is separate."""
import copy
import threading
import time
import unittest
from unittest.mock import patch
from astrbot_ex.core.connection_manager import ConnectionRecord, _ZmqAdapter
from tests.test_decision_management_http import ManagementHTTPFixture


class DecisionManagementProjectionTests(ManagementHTTPFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.management = self.server.decision_management
        self.connections = self.management.connections
        self.adapter = _ZmqAdapter(self.connections, ConnectionRecord("text", "Fixture", "zmq_client", True,
            {"protocol_profile": "astrbotex", "channel": "text", "endpoint": "tcp://127.0.0.1:1"}))
        with self.connections._lock:
            self.connections._records["text"] = self.adapter.record
            self.connections._adapters["text"] = self.adapter

    def value(self, **changes):
        result = {"schema_version": 1, "ex_session": self.management.store.ex_session,
            "robot_id": "fixture-robot", "task_id": "fixture-task", "generation": 2, "turn_id": "fixture-turn",
            "available": True, "phase": "planning", "title": "Fixture task", "current_goal": None,
            "completed": 0, "total": 1, "can_cancel": False, "updated_at": time.time(), "message": "Current task"}
        result.update(changes)
        return result

    def read(self, value, binary=None):
        with patch.object(self.adapter, "request", return_value=(value, binary)) as request:
            code, view, _ = self.request("/api/v1/ex/decision/view")
        self.assertEqual(code, 200)
        request.assert_called_once_with("task.projection.get",
            {"schema_version": 1, "ex_session": self.management.store.ex_session}, binary=None, timeout_sec=1)
        return view

    def test_fresh_view_does_not_start_hidden_mock_or_probe(self):
        with patch.object(self.management, "_make_backend", side_effect=AssertionError("view probe")):
            view = self.read(self.value(available=False, phase="unavailable"))
        self.assertEqual(set(view), {"schema_version", "ex_session", "revision", "effective_revision", "provider", "connection", "ex", "task", "error"})
        self.assertFalse(view["ex"]["can_start"])
        self.assertFalse(view["task"]["available"])
        self.assertEqual(view["task"]["phase"], "unavailable")
        self.assert_idle()

    def test_actual_producer_phases_and_trusted_idle_strip_private_identity(self):
        for phase in ("planning", "executing", "waiting_input", "lease_lost", "resume_review", "canceling", "completed", "canceled"):
            with self.subTest(phase=phase):
                self.management._projection_identity = None
                view = self.read(self.value(phase=phase))
                self.assertTrue(view["task"]["available"])
                self.assertEqual(view["task"]["phase"], phase)
                self.assertEqual(set(view["task"]), set(self.management._unavailable_task()))
                for key in ("robot_id", "task_id", "generation", "turn_id", "plan", "secret"):
                    self.assertNotIn(key, view["task"])
        self.management._projection_identity = None
        view = self.read(self.value(task_id=None, generation=None, turn_id=None, phase="idle", title="", total=0))
        self.assertTrue(view["task"]["available"])
        self.assertEqual(view["task"]["phase"], "idle")

    def test_invalid_ids_generation_idle_binary_and_extra_fields_fail_closed(self):
        invalid = [{"robot_id": None}, {"robot_id": " "}, {"robot_id": "r" * 257},
            {"task_id": " "}, {"turn_id": "t" * 257}, {"generation": 0}, {"generation": 2**53},
            {"generation": True}, {"phase": "needs_planning"}, {"phase": "running"},
            {"completed": 2}, {"updated_at": float("inf")}, {"current_goal": ""}, {"phase": "idle"}]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertFalse(self.read(self.value(**changes))["task"]["available"])
        idle = self.value(task_id=None, generation=None, turn_id=None, phase="idle", title="", total=0)
        for changes in ({"robot_id": None}, {"title": "hidden task"}, {"generation": 1}, {"turn_id": "turn"}, {"total": 1}):
            self.assertFalse(self.read({**idle, **changes})["task"]["available"])
        self.assertFalse(self.read(self.value(), b"")["task"]["available"])
        self.assertFalse(self.read(self.value(extra="private-plan"))["task"]["available"])
        self.management._projection_identity = None
        self.assertTrue(self.read(self.value(robot_id="r" * 256, generation=2**53 - 1))["task"]["available"])

    def test_generation_time_turn_closure_and_new_turn_fences(self):
        first = self.value(updated_at=time.time() - 5)
        self.assertTrue(self.read(first)["task"]["available"])
        for changes in ({"generation": 1}, {"updated_at": first["updated_at"] - 1}, {"turn_id": "different-turn"}):
            self.assertFalse(self.read({**first, **changes})["task"]["available"])
        closed = {**first, "turn_id": None, "phase": "executing", "updated_at": first["updated_at"] + 1}
        self.assertTrue(self.read(closed)["task"]["available"])
        self.assertFalse(self.read({**closed, "turn_id": "fixture-turn"})["task"]["available"])
        self.assertTrue(self.read({**closed, "generation": 3, "turn_id": "new-turn", "phase": "planning"})["task"]["available"])

    def test_inflight_intent_session_provider_and_same_id_adapter_replacement_fences(self):
        for change in ("intent", "session", "provider", "adapter", "route"):
            with self.subTest(change=change):
                entered, release = threading.Event(), threading.Event()
                value = self.value()
                result = []
                original_session = self.management.store.ex_session
                old_saved = copy.deepcopy(self.management.store.saved)
                def held(*args, **kwargs):
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("projection latch timeout")
                    return value, None
                with patch.object(self.adapter, "request", side_effect=held):
                    thread = threading.Thread(target=lambda: result.append(self.management.view()))
                    thread.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        if change == "session":
                            self.management.store.ex_session = "new-session"
                        elif change == "provider":
                            saved = copy.deepcopy(old_saved)
                            saved["backend"] = "laya"
                            self.assertEqual(self.write("/config", {"config": saved})[0], 200)
                        elif change == "adapter":
                            with self.connections._lock:
                                self.connections._adapters["text"] = _ZmqAdapter(self.connections, self.adapter.record)
                        elif change == "route":
                            with self.connections._lock:
                                self.adapter.record.enabled = False
                        else:
                            # Actual Stop must not wait for projection RPC.
                            code, accepted, _ = self.write("/stop")
                            self.assertEqual(code, 202)
                            self.assertEqual(self.operation(accepted)["state"], "succeeded")
                            self.assertFalse(release.is_set())
                    finally:
                        release.set()
                        thread.join(3)
                        self.management.store.ex_session = original_session
                        with self.connections._lock:
                            self.adapter.record.enabled = True
                            self.connections._adapters["text"] = self.adapter
                    self.assertFalse(thread.is_alive())
                    self.assertFalse(result[0]["task"]["available"])

    def test_saved_probe_is_not_online_proof_after_backend_quarantine(self):
        from tests import test_decision_management_provider_http as provider_fixture
        url, calls = provider_fixture.DecisionManagementProviderHTTPTests.supplier(self, "laya")
        saved = copy.deepcopy(self.get_config()["saved"])
        saved["backend"] = "laya"
        saved["laya"]["service_connection"]["base_url"] = url
        self.assertEqual(self.write("/config", {"config": saved})[0], 200)
        code, accepted, _ = self.write("/test")
        self.assertEqual(code, 202)
        self.assertEqual(self.operation(accepted)["state"], "succeeded")
        self.assertEqual(self.management.view()["connection"]["state"], "verified")
        code, accepted, _ = self.write("/mode", {"mode": "execute"})
        self.assertEqual(code, 202)
        self.assertEqual(self.operation(accepted)["state"], "succeeded")
        # Re-probe installed live backend without a Goal, preserving matching saved identity.
        code, accepted, _ = self.write("/test")
        self.assertEqual(code, 202)
        self.assertEqual(self.operation(accepted)["state"], "succeeded")
        self.assertEqual(self.management.view()["connection"]["state"], "verified")
        backend = self.server.decision_service.backend
        with backend._lock:
            backend._restart_required = True
        view = self.management.view()
        self.assertEqual(view["connection"]["state"], "disconnected")
        self.assertEqual(view["connection"]["code"], "restart_required")
        self.assertEqual(view["ex"]["state"], "failed")
        self.assertFalse(view["ex"]["can_start"])
        self.assertFalse(self.management.status()["probe"]["binding"]["current_config_verified"])
        self.assertEqual(view["error"]["code"], "restart_required")

    def test_view_response_wide_fence_after_projection_returns(self):
        original = self.management._task_projection
        session = self.management.store.ex_session
        def race(**kwargs):
            result = original(**kwargs)
            self.management.store.ex_session = "changed-after-rpc"
            self.management.invalidate("new_session")
            return result
        with patch.object(self.adapter, "request", return_value=(self.value(), None)), \
             patch.object(self.management, "_task_projection", side_effect=race):
            try:
                view = self.management.view()
                self.assertEqual(view["ex_session"], "changed-after-rpc")
                self.assertFalse(view["task"]["available"])
            finally:
                self.management.store.ex_session = session

    def test_older_concurrent_rpc_cannot_overwrite_newer_projection(self):
        entered, release = threading.Event(), threading.Event()
        old, new = self.value(title="old"), self.value(title="new", generation=3)
        results, calls = [], []
        def rpc(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("projection latch timeout")
                return old, None
            return new, None
        with patch.object(self.adapter, "request", side_effect=rpc):
            thread = threading.Thread(target=lambda: results.append(self.management.view()))
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(self.management.view()["task"]["title"], "new")
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(results[0]["task"]["available"])
            self.assertEqual(self.management._projection_identity[4], 3)
