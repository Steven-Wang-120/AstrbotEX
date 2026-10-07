"""C09 offline EX/AEB closure; explicit companion checkout, never AEB main.

Set ASTRBOTEX_AEB_TEST_ROOT to the companion plugin directory (or its parent).
Core cases need only Python plus EX dependencies. The two LLM-tool cases also
need the real AstrBot public Message/ToolSet modules, with a synthetic Host.
No socket service, provider, key or physical action is used.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import os
import sys
import threading
import time
import types
import unittest
from concurrent.futures import Executor, Future, TimeoutError as FutureTimeout
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import patch

from astrbot_ex.core.actions.ledger import OwnerBinding
from astrbot_ex.core.decision.feedback_journal import FeedbackJournal
from tests.test_decision_replanning import ScoredBackend
from tests.test_decision_service import wait_for
from tests.wiring_fixture import WiringFixture


PACKAGE = "_c09_aeb_companion"
SOURCE_NAMES = ("__init__.py", "task_contracts.py", "task_models.py", "task_store.py",
                "task_coordinator.py", "planning_tools.py", "skills/robot_task_planning/SKILL.md")


def companion():
    configured = os.environ.get("ASTRBOTEX_AEB_TEST_ROOT")
    if not configured:
        raise unittest.SkipTest("set ASTRBOTEX_AEB_TEST_ROOT to the reviewed companion checkout")
    root = Path(configured).resolve()
    if (root / "astrbot_plugin_astrbotex_interaction").is_dir():
        root /= "astrbot_plugin_astrbotex_interaction"
    if not all((root / name).is_file() for name in SOURCE_NAMES):
        raise RuntimeError("configured AEB companion is incomplete")
    loaded = sys.modules.get(PACKAGE)
    if loaded is not None and list(loaded.__path__) != [str(root)]:
        raise RuntimeError("C09 companion path changed in the same process")
    if loaded is None:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(root)]
        package.__package__ = PACKAGE
        sys.modules[PACKAGE] = package
    return root, {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                  for name in SOURCE_NAMES}


class RequestExecutor(Executor):
    """Fixture-owned daemon workers; no unbounded stdlib executor atexit join."""
    def __init__(self):
        self._queue = Queue()
        self._lock = threading.Lock()
        self._closed = False
        self.threads = [threading.Thread(target=self._run, name=f"c09-aeb-request-{i}",
                       daemon=True) for i in range(2)]
        for thread in self.threads:
            thread.start()

    def submit(self, fn, /, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RuntimeError("C09 request executor closed")
            future = Future()
            self._queue.put((future, fn, args, kwargs))
            return future

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, fn, args, kwargs = item
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._lock:
            if not self._closed:
                self._closed = True
                if cancel_futures:
                    while True:
                        try:
                            future, _, _, _ = self._queue.get_nowait()
                        except Empty:
                            break
                        future.cancel()
                for _ in self.threads:
                    self._queue.put(None)
        if wait:
            self.join(3)

    def join(self, timeout):
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            raise TimeoutError("C09 request executor did not stop")


class BridgeCleanupError(RuntimeError):
    """Python 3.10 aggregate: retain every task/executor/loop cleanup error."""
    def __init__(self, errors):
        self.errors = tuple(errors)
        super().__init__("; ".join(type(error).__name__ + ": " + str(error) for error in errors))


class AsyncBridge:
    """One bounded loop thread; EX feedback never waits on its own event loop."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.executor = RequestExecutor()
        self.ready = threading.Event()
        self._loop_error = None
        self.thread = threading.Thread(target=self._run, name="c09-aeb-loop", daemon=True)
        self.thread.start()
        if not self.ready.wait(2):
            self.close()
            raise TimeoutError("C09 loop did not start")

    def _run(self):
        try:
            asyncio.set_event_loop(self.loop)
            self.ready.set()
            self.loop.run_forever()
        except BaseException as exc:
            self._loop_error = exc
        finally:
            try:
                self.loop.close()
            except BaseException as exc:
                self._loop_error = exc

    def call(self, coroutine, timeout=12):
        if threading.current_thread() is self.thread:
            coroutine.close()
            raise RuntimeError("synchronous bridge call from the AEB loop")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except FutureTimeout:
            future.cancel()
            raise

    def close(self):
        async def finish():
            current = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not current]
            for task in pending:
                task.cancel()
            done, remaining = await asyncio.wait(pending, timeout=3) if pending else (set(), set())
            errors = [task.exception() for task in done if not task.cancelled() and task.exception() is not None]
            if remaining:
                errors.append(TimeoutError("C09 AEB tasks did not cancel"))
            try:
                await asyncio.wait_for(self.loop.shutdown_asyncgens(), 2)
            except BaseException as exc:
                errors.append(exc)
            if errors:
                raise BridgeCleanupError(errors)
        errors = []
        try:
            self.call(finish(), timeout=6)
        except BaseException as exc:
            errors.append(exc)
        # Request waits are already limited to 5s. Stop accepting/cancel queued
        # work, then verify real worker exit; asyncio cancellation alone cannot
        # stop a running synchronous request. No default executor is created.
        try:
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor.join(3)
        except BaseException as exc:
            errors.append(exc)
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(5)
            if self.thread.is_alive():
                raise TimeoutError("C09 loop did not stop")
            if not self.loop.is_closed():
                raise RuntimeError("C09 loop was not closed")
        except BaseException as exc:
            errors.append(exc)
        if self._loop_error is not None:
            errors.append(self._loop_error)
        if errors:
            raise BridgeCleanupError(errors)


class ScriptHost:
    """Only returns tool calls; real AEB validation/execution consumes them."""
    def __init__(self, names, arguments):
        self.names, self.arguments = names, arguments
        self.calls = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) != 1:
            raise AssertionError("unexpected additional LLM round")
        return types.SimpleNamespace(role="assistant", is_chunk=False, completion_text="",
            tools_call_name=self.names, tools_call_args=self.arguments,
            tools_call_ids=[f"offline-call-{i}" for i in range(len(self.names))])


class AEBLoopFixture(WiringFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aeb_root, cls.source_hashes = companion()
        print("C09_AEB_SOURCE_SHA256 " + json.dumps(cls.source_hashes, sort_keys=True), flush=True)

    def setUp(self):
        super().setUp()
        self.bridge = AsyncBridge()
        self.addCleanup(self.bridge.close)
        store_module = importlib.import_module(PACKAGE + ".task_store")
        model_module = importlib.import_module(PACKAGE + ".task_models")
        coordinator_module = importlib.import_module(PACKAGE + ".task_coordinator")
        self.planning = importlib.import_module(PACKAGE + ".planning_tools")
        self.TaskError = model_module.TaskError
        self.store = store_module.TaskStore(str(Path(self.tmp.name) / "aeb-tasks.sqlite3"))
        self.addCleanup(self.store.close)
        self.authority = model_module.TaskAuthority("robot", "session", "user", "route", True)
        self.inbound, self.outbound, self.deliveries, self.errors = [], [], [], []
        self._capture_lock = threading.Lock()
        self._drop_reason = None
        self._dropped = False
        self.coordinator = coordinator_module.TaskCoordinator(self.store, self.aeb_request,
            turn_sync=self.turn_sync, llm_timeout=10)
        self.addCleanup(lambda: self.bridge.call(self.coordinator.close()))
        self.rpc_patch = patch.object(self.m, "request_connection", side_effect=self.ex_feedback)
        self.rpc_patch.start()
        self.addCleanup(self.rpc_patch.stop)
        self.addCleanup(self._assert_source_stable)

    def tearDown(self):
        # Leave the bridge/store alive until real EX workers have been reclaimed.
        self._close_fixture()
        self.assertFalse(self.c._worker.is_alive())
        self.assertFalse(self.s._control.is_alive())
        self.assertFalse(self.s._backend_worker.is_alive())
        self.assertTrue(all(not actor.alive for actor in self.actors))
        self.assertEqual(self.errors, [])

    def _assert_source_stable(self):
        _, hashes = companion()
        self.assertEqual(hashes, self.source_hashes, "companion sources changed during C09 run")

    async def aeb_request(self, robot, route, method, payload):
        self.assertEqual((robot, route), ("robot", "route"))
        with self._capture_lock:
            self.outbound.append((method, copy.deepcopy(payload)))
        # Production Controller/DecisionTransport handles every response. The
        # executor avoids blocking the AEB loop during FULL/WAL journal writes.
        return await asyncio.wait_for(self.bridge.loop.run_in_executor(
            self.bridge.executor, self.request, method, payload), 5)

    async def turn_sync(self, payload):
        result = await self.aeb_request("robot", "route", "interaction.task.turn", payload)
        self.assertTrue(result["ok"], result)
        return result

    def ex_feedback(self, connection, channel, method, fact, **kwargs):
        self.assertEqual((connection, channel, method), ("trusted", "text", "decision.feedback"))
        self.assertFalse(self.s._lock._is_owned())
        self.assertFalse(self.c._lock._is_owned())
        with self._capture_lock:
            self.inbound.append(copy.deepcopy(fact))
        try:
            ack = self.bridge.call(self.coordinator.feedback("robot", "route", copy.deepcopy(fact)),
                                   timeout=kwargs.get("timeout_sec", 0.25))
            current = self.current()
            with self._capture_lock:
                self.deliveries.append((copy.deepcopy(fact), current))
                drop = fact["reason_code"] == self._drop_reason and not self._dropped
                if drop:
                    self._dropped = True
            if drop:
                # Real AEB durable receipt committed, but EX sees ambiguous ACK.
                raise TimeoutError("synthetic lost ACK after real AEB feedback")
            return ack, None
        except TimeoutError:
            raise
        except Exception as exc:
            with self._capture_lock:
                self.errors.append(type(exc).__name__ + ": " + str(exc))
            raise

    def start_task(self, *, backend=None, stop_proof=True):
        self.execute(stop_proof=stop_proof)
        if backend is not None:
            self.s.backend = backend
        context = self.request("decision.context.get", {"schema_version": 1})
        self.task = self.store.create(self.authority, "Offline three-step task.", "host-message",
            ex_session=context["ex_session"], revision=context["revision"])
        self.store.bind_route(self.authority, b"trusted", origin="offline")
        return context

    def current(self):
        return self.store.get(self.task["task_id"])

    @staticmethod
    def steps(ids=("step-0", "step-1", "step-2")):
        return [{"step_id": step, "intent": "Move the software owner.",
                 "completion_condition": "Committed arm action success."} for step in ids]

    @staticmethod
    def goal_args(step, meters):
        return {"step_id": step, "goal_text_en": "Move the software owner.",
            "allowed_actions": ["arm.move.v1"], "parameters": {"arm.move.v1": {"meters": meters}},
            "completion": {"required_success_actions": ["arm.move.v1"]}, "lease_ms": 10000}

    def plan(self, step, meters, *, steps=None, host=False, outcome="waiting_feedback"):
        names, arguments = [], []
        if steps is not None:
            names.append("save_plan")
            arguments.append({"steps": self.steps(steps), "expected_revision": self.current()["plan_revision"]})
        if step is not None:
            names.append("submit_current_goal")
            arguments.append(self.goal_args(step, meters))
        names.append("finish_planning_turn")
        arguments.append({"outcome": outcome})
        scripted = ScriptHost(names, arguments)
        async def planning_turn():
            turn = self.store.begin_turn(self.task["task_id"])
            await self.coordinator.sync_turn(self.task["task_id"], "bind", turn=turn)
            context = await self.aeb_request("robot", "route", "decision.context.get", {"schema_version": 1})
            tools = self.planning.PlanningTools(self.coordinator, None, turn, context)
            if host:
                prompt = json.dumps({"task": self.current(), "decision_context": context})
                await self.planning.run_planning_turn(scripted, "synthetic-offline", tools, prompt,
                                                     timeout_sec=10)
            else:
                for i, (name, args) in enumerate(zip(names, arguments)):
                    result = await tools.execute(name, args, f"offline-call-{i}")
                    self.assertTrue(result["ok"], result)
            await self.coordinator.drain_turn_sync()
        self.bridge.call(planning_turn())
        if host:
            self.assertEqual(len(scripted.calls), 1)
            self.assertFalse(scripted.calls[0]["stream"])
            self.assertEqual({t.name for t in scripted.calls[0]["tools"].tools},
                {"save_plan", "submit_current_goal", "emit_user_message", "finish_planning_turn"})
        return self.current()

    def admitted(self, count):
        self.assertTrue(wait_for(lambda: len(self.owners["arm"].commands) == count), self.diagnostics())
        command = self.owners["arm"].commands[-1]
        self.assertTrue(wait_for(lambda: self.server.action_ledger.get(command.command_id).result(1).status == "accepted"))
        goal = self.current()["current_goal"]
        self.assertIsNotNone(goal)
        self.assertEqual(command.goal_id, goal["payload"]["goal_id"])
        state = self.request("decision.state.get", {"schema_version": 1, "ex_session": self.s.goals.ex_session})
        self.assertEqual(state["active_goal_id"], command.goal_id)
        self.assertIsNone(state["pending_goal_id"])
        return command

    def complete(self, count):
        command = self.admitted(count)
        owner = self.owners["arm"]
        actor = self.actors[0]
        def on_test_complete(plugin, command_id):
            self.assertIs(threading.current_thread(), actor._thread)
            plugin.dispatcher.report(command_id, OwnerBinding(plugin.id, 1), "running").result(1)
            plugin.dispatcher.report(command_id, OwnerBinding(plugin.id, 1), "succeeded").result(1)
        owner.on_test_complete = types.MethodType(on_test_complete, owner)
        actor.call("on_test_complete", command.command_id, timeout=2)
        self.assertTrue(wait_for(lambda: self.current()["current_goal"] is None
            and self.current()["active_step"] == count and self.current()["needs_planning"]), self.diagnostics())
        fact = self.terminal(command.goal_id, "succeeded")
        evidence = fact["details"]["completion_evidence"]
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["goal_id"], command.goal_id)
        self.assertIn("arm.move.v1", evidence["succeeded_actions"])
        self.assertEqual(self.server.action_ledger.get(command.command_id).result(1).status, "succeeded")
        return command, fact

    def terminal(self, goal_id, status="failed", reason=None):
        def find():
            return next((fact for fact in self.c.journal.snapshot()["goal_summaries"]
                if fact["goal_id"] == goal_id and fact["status"] == status
                and (reason is None or fact["reason_code"] == reason)), None)
        self.assertTrue(wait_for(lambda: find() is not None), self.diagnostics())
        fact = find()
        self.assertTrue(wait_for(lambda: any(raw == fact for raw, _ in self.deliveries)), self.diagnostics())
        self.assertTrue(wait_for(lambda: fact not in self.c.journal.pending()), self.diagnostics())
        # Reopening both SQLite files reads actual committed state, not captures.
        journal = FeedbackJournal(Path(self.tmp.name) / "execution" / "feedback.sqlite3", self.s.goals.ex_session)
        try:
            self.assertIn(fact, journal.snapshot()["goal_summaries"])
        finally:
            journal.close()
        connection = __import__("sqlite3").connect(Path(self.tmp.name) / "aeb-tasks.sqlite3")
        try:
            row = connection.execute("SELECT payload FROM receipts WHERE robot_id=? AND ex_session=? AND seq=?",
                ("robot", fact["ex_session"], fact["event_seq"])).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(json.loads(row[0]), fact)
            saved = json.loads(connection.execute("SELECT data FROM tasks WHERE task_id=?",
                (self.task["task_id"],)).fetchone()[0])
            self.assertEqual(saved, self.current())
        finally:
            connection.close()
        self.print_fact(fact)
        return fact

    def print_fact(self, fact):
        print("C09_FACT " + json.dumps({"test": self._testMethodName, "event_seq": fact["event_seq"],
            "status": fact["status"], "reason_code": fact["reason_code"],
            "completion_verified": fact["details"].get("completion_evidence", {}).get("verified"),
            "terminal_verified": fact["details"].get("terminal_evidence", {}).get("verified"),
            "stop_proven": fact["details"].get("stop_evidence", {}).get("stopped"),
            "command_statuses": [c["status"] for c in fact["details"].get("terminal_evidence", {}).get("commands", [])],
            "aeb_status": self.current()["status"], "active_step": self.current()["active_step"],
            "needs_planning": self.current()["needs_planning"]}, sort_keys=True), flush=True)

    def assert_retired(self, fact):
        self.assertTrue(fact["details"]["terminal_evidence"]["verified"])
        self.assertTrue(fact["details"]["stop_evidence"]["stopped"])
        state = self.request("decision.state.get", {"schema_version": 1, "ex_session": fact["ex_session"]})
        self.assertIsNone(state["active_goal_id"])
        self.assertIsNone(state["pending_goal_id"])
        self.assertTrue(state["execution"]["stop_proven"])
        self.assertEqual(state["execution"]["unresolved"], [])
        self.assertIsNone(self.current()["current_goal"])
        self.assertTrue(self.current()["needs_planning"])
        self.assertTrue(any(raw == fact and task["needs_planning"] and task["current_goal"] is None
                            for raw, task in self.deliveries))

    def replay(self, fact):
        before = self.current()
        ack, binary = self.ex_feedback("trusted", "text", "decision.feedback", fact, timeout_sec=2)
        self.assertEqual(ack, {"ok": True, "ex_session": fact["ex_session"], "acked_event_seq": fact["event_seq"]})
        self.assertIsNone(binary)
        self.assertEqual(self.current(), before)


class PurePythonAEBReplanningTests(AEBLoopFixture):
    def test_request_replan_running_actor_stop_proof_and_lost_ack_idempotence(self):
        self.start_task()
        self.plan("step-0", 1, steps=("step-0", "step-1", "step-2"))
        command = self.admitted(1)
        self._drop_reason = "backend_requested_replan"
        self.s.backend = ScoredBackend(kind="request_replan", confidence=1)
        self.s.tick()
        self.assertTrue(wait_for(lambda: self.current()["needs_planning"] and self.current()["current_goal"] is None))
        fact = self.terminal(command.goal_id, reason="backend_requested_replan")
        self.assert_retired(fact)
        self.assertEqual(self.owners["arm"].cancels, [command.command_id])
        self.assertEqual(self.server.action_ledger.get(command.command_id).result(1).status, "canceled")
        proof = fact["details"]["terminal_evidence"]["commands"][0]
        self.assertEqual(proof["command_id"], command.command_id)
        self.assertEqual(proof["stop_evidence"], {"stopped": True, "source": "arm", "reference": command.command_id})
        self.assertTrue(self._dropped)
        self.assertGreaterEqual(sum(raw == fact for raw in self.inbound), 2)
        self.replay(fact)
        self.assertEqual(self.current()["active_step"], 0)
        self.assertEqual(len(self.owners["arm"].commands), 1)
        self.assertEqual(len([m for m, _ in self.outbound if m == "decision.goal.submit"]), 1)

    def test_model_wait_at_threshold_keeps_goal_without_replanning_or_dispatch(self):
        backend = ScoredBackend(kind="wait", confidence=0.6)
        self.start_task(backend=backend)
        task = self.plan("step-0", 1, steps=("step-0", "step-1", "step-2"))
        goal = task["current_goal"]
        self.assertTrue(wait_for(lambda: any(d["outcome"] == "no_dispatch" for d in self.s.status()["decisions"])))
        self.bridge.call(self.coordinator.sync(self.task["task_id"]))
        self.assertEqual(self.current()["current_goal"]["payload"], goal["payload"])
        self.assertFalse(self.current()["needs_planning"])
        self.assertEqual(self.current()["active_step"], 0)
        state = self.request("decision.state.get", {"schema_version": 1, "ex_session": self.s.goals.ex_session})
        self.assertEqual(state["active_goal_id"], goal["payload"]["goal_id"])
        self.assertIsNone(state["pending_goal_id"])
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.assertEqual(self.owners["arm"].commands, [])
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())
        selected = backend.original["choices"][0]
        self.assertEqual(selected["confidence"], 0.6)
        self.assertEqual(next(c["kind"] for o in backend.snapshot.owners for c in o["candidates"]
                              if c["option_id"] == selected["option_id"]), "wait")

    def test_user_cancel_and_old_feedback_cannot_revive_or_advance_task(self):
        self.start_task()
        self.plan("step-0", 1, steps=("step-0", "step-1", "step-2"))
        _, success = self.complete(1)
        self.plan("step-1", 2)
        command = self.admitted(2)
        self.bridge.call(self.coordinator.cancel_task(self.task["task_id"], self.authority))
        self.assertTrue(wait_for(lambda: self.current()["status"] == "canceled" and not self.current()["active"]))
        canceled = self.terminal(command.goal_id, status="canceled", reason="user_cancel")
        self.replay(success)
        self.replay(canceled)
        self.bridge.call(self.coordinator.sync(self.task["task_id"]))
        self.assertFalse(self.current()["needs_planning"])
        self.assertIsNone(self.current()["current_goal"])
        self.assertEqual(self.current()["active_step"], 1)
        self.assertEqual([s["status"] for s in self.current()["steps"]], ["completed", "canceled", "planned"])
        self.assertEqual(self.owners["arm"].cancels, [command.command_id])
        self.assertEqual(len(self.owners["arm"].commands), 2)
        self.assertEqual(len([m for m, _ in self.outbound if m == "decision.goal.submit"]), 2)
        self.assertNotIn(PACKAGE + ".main", sys.modules)

    def test_unknown_action_without_stop_proof_requires_review_and_never_replays(self):
        self.start_task(stop_proof=False)
        self.plan("step-0", 1, steps=("step-0", "step-1", "step-2"))
        command = self.admitted(1)
        self.server.action_dispatcher.report(command.command_id, OwnerBinding("arm", 1), "unknown").result(1)
        self.assertTrue(wait_for(lambda: self.s.goals.phase == "blocked"), self.diagnostics())
        self.assertTrue(wait_for(lambda: any(raw["reason_code"] == "ledger_unknown"
            and raw["goal_id"] == command.goal_id for raw, _ in self.deliveries)), self.diagnostics())
        self.bridge.call(self.coordinator.sync(self.task["task_id"]))
        state = self.request("decision.state.get", {"schema_version": 1, "ex_session": self.s.goals.ex_session})
        self.assertFalse(state["execution"]["stop_proven"])
        self.assertTrue(state["execution"]["blocked"])
        self.assertNotEqual(state["execution"]["unresolved"], [])
        self.assertEqual(self.c.journal.snapshot()["goal_summaries"], [])
        self.assertFalse(self.current()["needs_planning"])
        self.assertIsNotNone(self.current()["current_goal"])
        self.assertEqual(self.current()["active_step"], 0)
        self.assertEqual(len(self.owners["arm"].commands), 1)
        print("C09_UNRESOLVED " + json.dumps({"action_status": self.server.action_ledger.get(command.command_id).result(1).status,
            "ex_internal_phase": state["execution"]["internal_phase"], "ex_blocked": state["execution"]["blocked"],
            "stop_proven": state["execution"]["stop_proven"], "unresolved_count": len(state["execution"]["unresolved"]),
            "goal_summaries": len(self.c.journal.snapshot()["goal_summaries"]),
            "aeb_status": self.current()["status"], "needs_planning": self.current()["needs_planning"],
            "current_goal_retained": self.current()["current_goal"] is not None,
            "dispatch_count": len(self.owners["arm"].commands)}, sort_keys=True), flush=True)
        self.assertEqual(self.current()["status"], "resume_review", "real blocked EX must require AEB review")
        with self.assertRaises(self.TaskError):
            self.plan("step-0", 1)


class HostToolAEBReplanningTests(AEBLoopFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            importlib.import_module("astrbot.core.agent.message")
            importlib.import_module("astrbot.core.agent.tool")
        except ModuleNotFoundError as exc:
            if exc.name != "astrbot":
                raise
            raise unittest.SkipTest("LLM-tool cases require installed Host public modules") from exc

    def test_three_real_successes_advance_only_current_goal_via_llm_tools(self):
        self.start_task()
        goals = []
        for i in range(3):
            self.plan(f"step-{i}", i + 1, steps=("step-0", "step-1", "step-2") if i == 0 else None, host=True)
            current = self.current()
            self.assertEqual(current["active_step"], i)
            self.assertFalse(current["needs_planning"])
            self.assertEqual(current["current_goal"]["payload"]["step_id"], f"step-{i}")
            self.assertEqual(len([m for m, _ in self.outbound if m == "decision.goal.submit"]), i + 1)
            command, _ = self.complete(i + 1)
            goals.append(command.goal_id)
            self.assertEqual([s["status"] for s in self.current()["steps"][:i + 1]], ["completed"] * (i + 1))
        self.plan(None, None, host=True, outcome="completed")
        self.assertEqual(len(set(goals)), 3)
        self.assertEqual(self.current()["status"], "completed")
        self.assertFalse(self.current()["active"])
        self.assertEqual(self.current()["active_step"], 3)
        self.assertEqual(len(self.owners["arm"].commands), 3)
        self.assertIsNone(self.s.goals.active)

    def test_low_confidence_retired_feedback_then_llm_replans_suffix_without_prefix_replay(self):
        self.start_task()
        self.plan("step-0", 1, steps=("step-0", "step-1", "step-2"), host=True)
        prefix_command, prefix_fact = self.complete(1)
        prefix = copy.deepcopy(self.current()["steps"][0])
        backend = self.s.backend = ScoredBackend(kind="start", confidence=0.2)
        backend.release.clear()
        try:
            task = self.plan("step-1", 2, host=True)
            rejected = task["current_goal"]["payload"]["goal_id"]
        finally:
            backend.release.set()
        self.assertTrue(wait_for(lambda: self.current()["needs_planning"] and self.current()["current_goal"] is None))
        fact = self.terminal(rejected, reason="low_confidence")
        self.assert_retired(fact)
        self.assertEqual(fact["details"]["decision_rejection"]["choices"], backend.original["choices"])
        self.assertEqual(backend.original["choices"][0]["confidence"], 0.2)
        self.assertEqual(next(c["kind"] for o in backend.snapshot.owners for c in o["candidates"]
            if c["option_id"] == backend.original["choices"][0]["option_id"]), "start")
        self.assertEqual(fact["details"]["terminal_evidence"]["commands"], [])
        self.assertEqual(len(self.owners["arm"].commands), 1)
        self.assertEqual(len(self.server.action_ledger.list_commands().result(1)), 1)
        self.assertEqual(self.current()["active_step"], 1)
        self.assertEqual(self.current()["steps"][0], prefix)
        self.s.backend = ScoredBackend(kind="start", confidence=1)
        replacement = self.plan("replacement-1", 4, steps=("replacement-1", "replacement-2"), host=True)
        self.assertNotEqual(replacement["current_goal"]["payload"]["goal_id"], rejected)
        self.assertEqual(replacement["steps"][0], prefix)
        self.replay(prefix_fact)
        self.replay(fact)
        self.complete(2)
        self.plan("replacement-2", 5, host=True)
        self.complete(3)
        self.plan(None, None, host=True, outcome="completed")
        commands = self.owners["arm"].commands
        self.assertEqual([c.params["meters"] for c in commands], [1, 4, 5])
        self.assertEqual(sum(c.goal_id == prefix_command.goal_id for c in commands), 1)
        self.assertNotIn(rejected, [c.goal_id for c in commands])
        self.assertEqual([s["step_id"] for s in self.current()["steps"]],
            ["step-0", "replacement-1", "replacement-2"])
        self.assertEqual(self.current()["status"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
