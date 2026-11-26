"""Local B04/B05 framework boundary; no remote method opens an execution gate."""
from __future__ import annotations

import copy
import threading
from pathlib import Path

from ..contracts import (ContractError, DecisionState, Feedback, GoalSubmitResult,
                         ValidationError, check_ex_session, measure_json_budget,
                         reject, require_id, require_sequence)
from ..actions.models import ContractError as ActionContractError
from .feedback_journal import FeedbackJournal

TURN_FIELDS = frozenset({"task_schema_version", "operation", "ex_session", "task_id", "robot_id",
    "session_id", "user_id", "route_ref", "turn_id", "generation", "expected_revision"})
OWNER_FIELDS = ("ex_session", "task_id", "robot_id", "session_id", "user_id", "route_ref", "turn_id", "generation")
BUSINESS_REJECTIONS = frozenset({"revision_conflict", "stale_ex_session", "unknown_action",
    "cancel_unsupported", "authorization_revoked", "goal_capacity", "request_capacity",
    "duplicate_request_id_conflict", "task_authority_rejected", "decision_disabled"})


class DecisionController:
    def __init__(self, service, connections, interaction, data_root: Path, *, journal=None) -> None:
        self.service, self.connections, self.interaction = service, connections, interaction
        self.journal = journal or FeedbackJournal(data_root / "execution" / "feedback.sqlite3", service.goals.ex_session)
        self._lock = threading.RLock()
        self._bindings = {}
        self._watermarks = {}
        self._robot_id = None
        self._verified_completions = {}
        self._delivery_permits = set()
        self._cancel_receipts = {}
        self._stop = threading.Event()
        self._closed = False
        self._fault = False
        self.service.completion_hook = self._complete
        self._unsubscribe = interaction.topic_bus.subscribe("interaction_core.audio.stop", self._voice_stop)
        self._worker = threading.Thread(target=self._feedback_loop, name="decision-feedback", daemon=True)
        self._worker.start()

    def route(self, connection_id):
        try:
            return not self._closed and self.connections.decision_connection_id() == connection_id
        except RuntimeError:
            return False

    def _journal_fault(self):
        with self._lock:
            if self._fault:
                return
            self._fault = True
            self.invalidate_local()
        with self.service._condition:
            # Auxiliary B05 persistence never acquires physical ownership from a mode flag.
            if self.service.actions.control_mode == "decision":
                self.service.request_stop("feedback_journal_unavailable")

    def _voice_stop(self, message):
        self.invalidate_local()

    def invalidate_local(self):
        with self._lock:
            for robot in self._bindings:
                self._watermarks[robot]["invalidated"] = True
            self._bindings.clear()

    def turn(self, connection_id, feature, raw, binary):
        if feature != "text" or binary is not None or not self.route(connection_id):
            return {"ok": False, "error": "task_turn_route_rejected"}
        try:
            if not isinstance(raw, dict) or set(raw) != TURN_FIELDS or measure_json_budget(raw) is not None:
                raise ValueError("invalid turn fields")
            if type(raw["task_schema_version"]) is not int or raw["task_schema_version"] != 1:
                raise ValueError("invalid version")
            if raw["operation"] not in {"bind", "invalidate"}:
                raise ValueError("invalid operation")
            for key in OWNER_FIELDS[:-2]:
                require_id(raw[key], key)
            if raw["turn_id"] is not None:
                require_id(raw["turn_id"], "turn_id")
            if raw["operation"] == "bind" and raw["turn_id"] is None:
                raise ValueError("bind requires turn")
            require_sequence(raw["generation"], "generation", minimum=1)
            require_sequence(raw["expected_revision"], "expected_revision")
            with self._lock, self.service.goals._lock, self.interaction._lock:
                if self._closed or raw["ex_session"] != self.service.goals.ex_session:
                    raise ValueError("stale session")
                if raw["expected_revision"] != self.service.goals.revision:
                    raise ValueError("stale revision")
                if self._fault:
                    raise ValueError("journal fault requires new session review")
                robot = raw["robot_id"]
                if self._robot_id is not None and robot != self._robot_id:
                    raise ValueError("local robot identity cannot change")
                watermark = self._watermarks.get(robot)
                identity = tuple(raw[k] for k in OWNER_FIELDS if k not in {"generation", "turn_id"})
                if watermark:
                    same_task = raw["task_id"] == watermark["identity"][1]
                    if same_task:
                        if identity != watermark["identity"] or connection_id != watermark["connection_id"]:
                            raise ValueError("task owner identity cannot change")
                        if raw["generation"] < watermark["generation"]:
                            raise ValueError("old generation")
                        if raw["generation"] == watermark["generation"]:
                            if raw["operation"] == "bind" and (watermark["invalidated"]
                                    or raw["turn_id"] != watermark["turn_id"]):
                                raise ValueError("generation cannot resurrect or replace turn")
                    else:
                        with self.service.actions.dispatcher._lock:
                            stopped = (self.service.actions._stop_proof_epoch >=
                                       self.service.actions.dispatcher._epoch)
                        if (raw["generation"] != 1 or raw["operation"] != "bind"
                                or self.service.goals.active is not None
                                or self.service.goals.pending_replace is not None
                                or self.service.goals.phase != "idle" or not stopped):
                            raise ValueError("new task requires proven old stop")
                self._robot_id = robot
                if raw["operation"] == "invalidate":
                    self._bindings.pop(robot, None)
                    self._watermarks[robot] = {"generation": raw["generation"], "identity": identity,
                        "turn_id": raw["turn_id"], "invalidated": True, "connection_id": connection_id}
                    return {"ok": True}
                # EX controls one local robot; do not silently select another robot/owner.
                if self._bindings and robot not in self._bindings:
                    raise ValueError("robot already owned")
                if robot not in self._watermarks and len(self._watermarks) >= 256:
                    raise ValueError("binding capacity")
                old = self._bindings.get(robot)
                if old and raw["generation"] == old["generation"]:
                    if old["voice_generation"] != self.interaction._generation:
                        raise ValueError("voice interrupted")
                self._bindings[robot] = {**copy.deepcopy(raw), "connection_id": connection_id,
                                        "voice_generation": self.interaction._generation}
                self._watermarks[robot] = {"generation": raw["generation"], "identity": identity,
                                          "turn_id": raw["turn_id"], "invalidated": False, "connection_id": connection_id}
                return {"ok": True}
        except Exception:
            return {"ok": False, "error": "task_turn_rejected"}

    def _bound_task(self, connection_id, task_id, revision):
        for binding in self._bindings.values():
            if (binding["connection_id"] == connection_id and binding["task_id"] == task_id
                    and binding["ex_session"] == self.service.goals.ex_session
                    and binding["expected_revision"] == revision
                    and binding["voice_generation"] == self.interaction._generation):
                return binding
        return None

    def _check_admission(self, connection_id, parsed):
        check_ex_session(parsed.ex_session, self.service.goals.ex_session)
        if self._fault:
            reject("task_authority_rejected", "task_id", "journal requires explicit recovery")
        replay = self.service.goals._requests.get(parsed.request_id)
        watermark = self._watermarks.get(self._robot_id)
        if replay:
            if (not watermark or watermark["identity"][1] != parsed.task_id
                    or watermark["connection_id"] != connection_id):
                reject("task_authority_rejected", "task_id", "trusted task owner required")
        else:
            if self._bound_task(connection_id, parsed.task_id, self.service.goals.revision) is None:
                reject("task_authority_rejected", "task_id", "framework turn binding required")
            if parsed.expected_revision != self.service.goals.revision:
                reject("revision_conflict", "expected_revision", "current CAS required")
        if parsed.expected_revision is None:
            reject("revision_conflict", "expected_revision", "CAS required at framework boundary")
        if self.service.mode == "disabled" and not replay:
            reject("decision_disabled", "goal", "decision execution is disabled")

    def handle(self, connection_id, method, parsed):
        if not self.route(connection_id):
            reject("decision_route_rejected", "connection_id", "configured text route required")
        if method == "decision.capabilities.get":
            if (not isinstance(parsed, dict) or set(parsed) != {"schema_version"}
                    or type(parsed["schema_version"]) is not int or parsed["schema_version"] != 1):
                reject("invalid_capabilities_request", "schema_version", "exact v1 request required")
            return self.capabilities()
        if method == "decision.goal.submit":
            payload = parsed.to_dict()
            try:
                with self._lock, self.service._condition, self.service.goals._lock, self.interaction._lock:
                    self._check_admission(connection_id, parsed)
                    _, replay = self.service.goals._replay(parsed.request_id, "submit", payload)
                    revision = replay["revision"] if replay else self.service.goals.revision + 1
                # FULL/WAL write and capacity preflight never hold safety locks.
                try:
                    self.journal.prepare_admission(payload, revision)
                except Exception:
                    self._journal_fault()
                    raise
                with self._lock, self.service._condition, self.service.goals._lock, self.interaction._lock:
                    self._check_admission(connection_id, parsed)
                    result = self.service.submit_goal(payload)
                try:
                    self.journal.confirm_admission(payload, result)
                except Exception:
                    self._journal_fault()
                    raise
                return GoalSubmitResult(**result).to_dict()
            except (ContractError, ActionContractError) as exc:
                if exc.error.code not in BUSINESS_REJECTIONS and not exc.error.path.startswith("parameters"):
                    raise
                return GoalSubmitResult(False, parsed.request_id, parsed.ex_session, parsed.goal_id,
                    self.service.goals.revision, "rejected", ValidationError(exc.error.code, exc.error.path,
                    "Goal authorization rejected.")).to_dict()
        if method in {"decision.goal.cancel", "decision.goal.renew"}:
            with self._lock, self.service._condition, self.service.goals._lock, self.interaction._lock:
                check_ex_session(parsed.ex_session, self.service.goals.ex_session)
                payload = parsed.to_dict()
                watermark = self._watermarks.get(self._robot_id)
                receipt = self._cancel_receipts.get(parsed.request_id) if method == "decision.goal.cancel" else None
                if receipt is not None:
                    owner = receipt["owner"]
                    if (connection_id != owner[0] or not watermark
                            or watermark["connection_id"] != owner[0]
                            or watermark["identity"] != owner[1]
                            or watermark["generation"] != owner[2] or watermark["turn_id"] != owner[3]):
                        reject("task_authority_rejected", "task_id", "original trusted cancel owner required")
                    if payload != receipt["payload"]:
                        reject("duplicate_request_id_conflict", "request_id", "cancel request already bound")
                    return copy.deepcopy(receipt["result"])
                goal = self.service.goals._match(parsed.goal_id, parsed.goal_revision)
                task_id = goal.payload()["task_id"]
                if (not watermark or watermark["identity"][1] != task_id
                        or watermark["connection_id"] != connection_id):
                    reject("task_authority_rejected", "task_id", "trusted task owner required")
                if method == "decision.goal.renew":
                    if self._fault or self._bound_task(connection_id, task_id, parsed.goal_revision) is None:
                        reject("task_authority_rejected", "task_id", "live task binding required")
                    return self.service.renew_goal(parsed.to_dict())
                if len(self._cancel_receipts) >= self.service.goals._request_limit:
                    reject("request_capacity", "request_id", "cancel receipts full; new session required")
                owner = (connection_id, watermark["identity"], watermark["generation"], watermark["turn_id"])
                self.invalidate_local()
                result = self.service.cancel_goal(payload)
                self._cancel_receipts[parsed.request_id] = {"owner": owner,
                    "payload": copy.deepcopy(payload), "result": copy.deepcopy(result)}
                return result
        if method == "decision.feedback":
            # The frozen method is normally outbound. An echo may ACK an existing
            # exact fact; remote status can never create execution/completion facts.
            if not self.journal.ack(parsed.to_dict()):
                reject("feedback_receipt_rejected", "event_seq", "existing durable fact required")
            return {"ok": True, "ex_session": parsed.ex_session, "acked_event_seq": parsed.event_seq}
        session = parsed.ex_session if hasattr(parsed, "ex_session") else parsed.get("ex_session")
        if session is not None:
            check_ex_session(session, self.service.goals.ex_session)
        if method == "decision.events.get":
            return self.journal.events(parsed.since_event_seq)
        if method == "decision.state.get":
            return self.state()
        if method == "decision.context.get":
            return self.context()
        reject("unsupported_method", "method", "unsupported decision method")

    def capabilities(self):
        """Readonly pre-admission manifest summary; never creates a turn or Goal."""
        with self.service._condition, self.service.goals._lock:
            catalog = self.service.catalog.snapshot()
            actions = [{"owner": entry["owner"], "plugin_generation": entry["generation"],
                **copy.deepcopy(action)} for entry in catalog.entries if entry["available"]
                for action in entry["manifest"]["actions"]]
            if len(actions) > 256:
                raise RuntimeError("capabilities_catalog_capacity")
            result = {"schema_version": 1, "ex_session": self.service.goals.ex_session,
                "revision": self.service.goals.revision, "catalog_revision": catalog.revision,
                "control_mode": self.service.actions.control_mode,
                "execution": {"mode": self.service.mode,
                    "execution_allowed": self.service.mode == "execute" and
                        getattr(self.service.backend, "execution_allowed", False) is True,
                    "runtime_state": self.service.actions._runtime_state}, "actions": actions}
        if measure_json_budget(result) is not None:
            raise RuntimeError("capabilities_capacity")
        return result

    def context(self):
        catalog = self.service.catalog.snapshot()
        actions, guides = [], []
        for entry in catalog.entries:
            guides.append({"owner": entry["owner"], "generation": entry["generation"], **entry["guide"]})
            if entry["available"]:
                actions.extend({"owner": entry["owner"], "plugin_generation": entry["generation"],
                    **copy.deepcopy(action)} for action in entry["manifest"]["actions"])
        if len(actions) > 256:
            raise RuntimeError("context_catalog_capacity")
        result = {"schema_version": 1, "ex_session": self.service.goals.ex_session,
            "revision": self.service.goals.revision, "catalog_revision": catalog.revision,
            "actions": actions, "guides": guides, "observations": self.service.observations.snapshot(
                source for entry in catalog.entries for source in entry["manifest"].get("observation_sources", {})),
            "execution": {"mode": self.service.mode,
                          "execution_allowed": self.service.mode == "execute" and
                          getattr(self.service.backend, "execution_allowed", False) is True}}
        if measure_json_budget(result) is not None:
            raise RuntimeError("context_capacity")
        return result

    def state(self):
        status = self.service.status()
        snapshot = self.journal.snapshot()
        # Cached physical proof only; no ledger wait or stop inside safety locks.
        dispatcher = self.service.actions.dispatcher
        with self._lock, self.service._condition, self.service.goals._lock, dispatcher._lock:
            goals = self.service.goals.status()
            gate_open = dispatcher._gate
            dispatcher_epoch = dispatcher._epoch
            stop_proof_epoch = self.service.actions._stop_proof_epoch
            blocked = status["blocked"] or self._fault or dispatcher.blocked or goals["phase"] == "blocked"
            unresolved = [
                {"command_id": row.command_id, "status": row.status,
                 "held_resources": list(row.held_resources)}
                for row in self.service._rows
                if row.held_resources or (row.status not in {"succeeded", "rejected", "canceled"}
                    and not (row.status in {"failed", "unknown", "timed_out"}
                             and row.command_id in self.service._proven_uncertain))]
            stop_proven = (not self._closed and not self.service._closed and not gate_open
                and not blocked and not unresolved and stop_proof_epoch >= dispatcher_epoch >= 0)
        phase = goals["phase"]
        active, pending = goals["active_goal_id"], goals["pending_goal_id"]
        if phase != "active":
            # An old stopping goal with a replacement stays in active pair, but
            # execution explains gate closure; otherwise use legal pending pair.
            if not pending:
                pending, active = active, None
        execution = {"mode": status["mode"], "gate_open": gate_open,
            "blocked": blocked, "internal_phase": phase,
            "reason_code": goals["reason_code"], "unresolved": unresolved,
            "stop_proven": stop_proven, "stop_proof_epoch": stop_proof_epoch,
            "dispatcher_epoch": dispatcher_epoch,
            "new_authorization_required": not active and not pending,
            "feedback": snapshot["feedback"], "goal_summaries": snapshot["goal_summaries"],
            "previous_session_manual_review": snapshot["previous_session_manual_review"]}
        return DecisionState.parse({"schema_version": 1, "ex_session": goals["ex_session"],
            "revision": goals["revision"], "active_goal_id": active,
            "active_phase": "active" if active else None, "pending_goal_id": pending,
            "pending_phase": ("blocked" if phase == "blocked" else "pending_cancel") if pending else None,
            "execution": execution, "event_seq": snapshot["event_seq"]}).to_dict()

    def validate_public(self, connection_id, payload):
        with self._lock, self.service.goals._lock, self.interaction._lock:
            return self._public_token(connection_id, payload) is not None

    def _public_token(self, connection_id, payload):
        if not self.route(connection_id) or self._fault:
            return None
        binding = self._bindings.get(payload.get("robot_id"))
        if not binding or any(payload.get(k) != binding[k] for k in OWNER_FIELDS):
            return None
        if (binding["connection_id"] != connection_id or
                binding["expected_revision"] != self.service.goals.revision or
                binding["voice_generation"] != self.interaction._generation):
            return None
        if payload.get("claim") not in {"question", "progress", "completed"}:
            return None
        if payload["claim"] == "completed":
            if self.service.goals.active or self.service.goals.pending_replace:
                return None
            fact = self._verified_completions.get((payload["task_id"], payload.get("evidence_seq")))
            if not fact or fact["goal_revision"] != binding["expected_revision"]:
                return None
        return (binding["expected_revision"], binding["voice_generation"], self.service.goals.gate_epoch,
                tuple(binding[k] for k in OWNER_FIELDS))

    def public_token(self, connection_id, payload):
        with self._lock, self.service.goals._lock, self.interaction._lock:
            return self._public_token(connection_id, payload)

    def commit_public(self, connection_id, payload, token):
        # Consume authority once; never run subscribers/provider IO under safety locks.
        with self._lock, self.service.goals._lock, self.interaction._lock:
            if token is None or token != self._public_token(connection_id, payload):
                return None
            message_id = payload.get("message_id")
            if not isinstance(message_id, str) or not message_id:
                return None
            permit = (connection_id, message_id)
            if permit in self._delivery_permits or len(self._delivery_permits) >= 10000:
                return None
            self._delivery_permits.add(permit)
            return permit

    def _complete(self, goal, epoch, summary):
        # Durable draft is not a fact or a completion permit.
        try:
            token = self.journal.prepare_completion(goal, epoch, summary, self.service.actions.ledger)
        except Exception:
            self._journal_fault()
            return False
        goals = self.service.goals
        dispatcher = self.service.actions.dispatcher
        with self._lock, self.service._condition, goals._lock, dispatcher._lock:
            terminal = goals._terminal_stop
            revoked = (summary["status"] in {"canceled", "timed_out", "failed"} and terminal is not None
                and terminal[:2] == (goal, epoch) and terminal[2] == summary["status"]
                and terminal[3] == summary["reason_code"] and goals.phase == "stopping")
            completed = (summary["status"] == "succeeded" and goals.phase == "awaiting_llm"
                and goals.reason == "completion_evidence_committed" and goals._clock() < goal.expires_ns)
            evidence = summary["details"].get("terminal_evidence", {})
            if revoked and (evidence.get("verified") is not True
                    or evidence.get("goal_id") != goal.payload()["goal_id"]
                    or evidence.get("goal_revision") != goal.revision
                    or evidence.get("dispatcher_epoch") != dispatcher._epoch
                    or evidence.get("stop_proof_epoch") != self.service.actions._stop_proof_epoch
                    or summary["details"].get("stop_evidence", {}).get("stopped") is not True):
                return False
            if (self._closed or self._fault or self.service._closed or not (revoked or completed)
                    or dispatcher._gate or dispatcher.blocked
                    or self.service.actions._stop_proof_epoch < dispatcher._epoch
                    or not goals.finish_stopped(goal, epoch)):
                return False
        # The successful CAS is the sole permit; publish without safety locks.
        try:
            fact = self.journal.publish_completion(token)
            verified = (fact["status"] == "succeeded"
                and fact["details"].get("completion_evidence", {}).get("verified") is True
                and fact["ex_session"] == goals.ex_session
                and fact["task_id"] == goal.payload()["task_id"]
                and fact["goal_id"] == goal.payload()["goal_id"]
                and fact["goal_revision"] == goal.revision)
        except Exception:
            self._journal_fault()
            return False
        with self._lock, goals._lock:
            if (verified and not self._closed and not self._fault and not self.service._closed
                    and goals.gate_epoch == epoch and goals.revision == goal.revision
                    and goals.active is None and goals.pending_replace is None
                    and goals.phase == "idle"):
                # Never refresh/rebind authority from a delayed completion.
                self._verified_completions[(fact["task_id"], fact["event_seq"])] = copy.deepcopy(fact)
        return True

    def _drain_feedback(self):
        # Each persistence boundary fails closed; network failure retains facts.
        with self._lock:
            if self._closed or self._fault or self._stop.is_set():
                return
        try:
            self.journal.collect(self.service.actions.ledger)
            facts = self.journal.pending()
        except Exception:
            self._journal_fault()
            return
        if self.service.mode == "disabled":
            return
        try:
            connection_id = self.connections.decision_connection_id()
        except Exception:
            return
        for fact in facts:
            with self._lock:
                if self._closed or self._fault or self._stop.is_set():
                    return
            try:
                result, binary = self.connections.request_connection(connection_id, "text",
                    "decision.feedback", fact, timeout_sec=0.25)
                acknowledged = (binary is None and isinstance(result, dict) and result.get("ok") is True
                    and result.get("ex_session") == self.journal.session
                    and type(result.get("acked_event_seq")) is int
                    and result["acked_event_seq"] == fact["event_seq"])
            except Exception:
                return  # Ambiguous network result: no ACK or repeated stop.
            if acknowledged:
                try:
                    if not self.journal.ack(Feedback.parse(fact).to_dict()):
                        raise RuntimeError("journal_feedback_receipt_missing")
                except Exception:
                    self._journal_fault()
                    return

    def _feedback_loop(self):
        while not self._stop.is_set():
            self._drain_feedback()
            self._stop.wait(0.05)

    def close(self, timeout=2):
        if self._closed:
            return
        self._closed = True
        self.invalidate_local()
        self._unsubscribe()
        self._stop.set()
        self._worker.join(timeout)
        if self._worker.is_alive():
            raise TimeoutError("feedback worker did not stop")
        self.service.completion_hook = None
        self.journal.close()
