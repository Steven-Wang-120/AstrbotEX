"""Task choice over the existing owned Laya service, without an EX Goal.

The pinned B07 client supplies HTTP, identity, deadline, cancellation and sticky
quarantine. Only task request/response mapping differs from EX decisions.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import time
from dataclasses import replace
from astrbot_ex.core.decision.backends.laya import LayaBackend, LayaBackendError, LayaConfig, _json_bytes
from astrbot_ex.core.decision.owned_laya import OwnedLayaError
from .contracts import canonical, digest, skill_catalog
from .session import TaskContext, TaskSelection

WEIGHT_HASH = "4fa56de72383a9d3efa9cfa78955733c81b9fc8067a587ca4beb82c78107a24e"

class TaskLayaClient(LayaBackend):
    """Never registered as an EX backend and never grants execution permission."""
    def __init__(self, config=None, *, transport=None):
        super().__init__(config or LayaConfig(enabled=True, deadline_ms=300), transport=transport)
        self.generation = ""

    def request_payload(self, context):
        if not isinstance(context, TaskContext):
            raise LayaBackendError("invalid_snapshot")
        if digest({"text":context.text,"observation":context.observation,"options":context.options,"mode":context.mode}) != context.input_hash:
            raise LayaBackendError("invalid_snapshot")
        compact = {"text": context.text, "skills": {action["action_id"].split(".")[1]: action["description"] for action in skill_catalog()["actions"]}}
        if context.mode == "structured":
            obs = context.observation
            compact["observation"] = {k: copy.deepcopy(obs[k]) for k in
                ("objects", "at_table", "stationary", "valid", "age_ms", "max_age_ms", "task_success_proven") if k in obs}
        # Identifiers are local trace fields, not labels or model hints.
        meanings, mapping = {}, {}
        for i, option in enumerate(context.options):
            letter = chr(65 + i)
            label = option["label"].replace("Ask for missing details", "Ask details").replace("Wait for observations", "Wait").replace("Unsupported request", "Unsupported").replace("Task already complete", "Done").replace("Move to table dock", "Move table").replace("Move to home", "Move home")
            meanings[letter], mapping[letter] = label, option["option_id"]
        state = canonical(compact).replace("[MASK]", "\\u005bMASK]")
        question = {"type": "choice", "instructions": "Choose next task option.", "criteria": meanings}
        # The fixed tokenizer is byte-level BPE. UTF-8 bytes are an upper bound.
        head = len(("choice question: " + question["instructions"]).encode()) + sum(len((" " + k + ": " + v).encode()) + 1 for k, v in meanings.items()) + 4
        total = head + len(state.encode())
        if head > self.config.head_max_len or total > self.config.max_len:
            raise LayaBackendError("token_budget_exceeded")
        payload = {"model": self.config.model, "state": state, "questions": {"task": question},
                   "max_len": self.config.max_len, "head_max_len": self.config.head_max_len}
        body = _json_bytes(payload)
        if len(body) > self.config.max_request_bytes:
            raise LayaBackendError("request_too_large")
        return body, mapping, {"utf8_token_upper_bound": total, "head_upper_bound": head, "truncated": False}

    def _decode(self, raw, context, mapping, elapsed):
        if not isinstance(raw, dict) or set(raw) != {"model", "answers", "usage", "routing"} or raw["model"] != "laya-rl-agent":
            raise LayaBackendError("response_shape")
        expected = {"model": self.config.model, "repo": "convaiinnovations/laya/typed-decisions",
                    "reason": "explicit model='typed-decisions'", "detection": None, "workflow": None}
        if raw["routing"] != expected:
            raise LayaBackendError("routing_mismatch")
        usage = raw["usage"]
        keys = {"input_tokens", "output_tokens", "state_tokens", "state_tokens_dropped", "truncated", "truncated_questions"}
        if not isinstance(usage, dict) or not keys <= set(usage) or set(usage) - keys - {"options"}:
            raise LayaBackendError("invalid_usage")
        if any(type(usage[k]) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens", "state_tokens", "state_tokens_dropped")) or usage["output_tokens"] != 0 or type(usage["truncated"]) is not bool or not isinstance(usage["truncated_questions"], list):
            raise LayaBackendError("invalid_usage")
        if "options" in usage and not isinstance(usage["options"], dict):
            raise LayaBackendError("invalid_usage")
        if usage["truncated"] or usage["state_tokens_dropped"] or usage["truncated_questions"] or usage.get("options"):
            raise LayaBackendError("input_truncated")
        if not isinstance(raw["answers"], dict) or set(raw["answers"]) != {"task"}:
            raise LayaBackendError("answer_shape")
        answer = raw["answers"]["task"]
        if not isinstance(answer, dict) or set(answer) != {"type", "choice", "probabilities", "confidence", "answer_confidence", "action"} or answer["type"] != "choice":
            raise LayaBackendError("answer_shape")
        choice, probs = answer["choice"], answer["probabilities"]
        if not isinstance(choice, str) or choice not in mapping:
            raise LayaBackendError("unknown_option")
        finite_prob = lambda x: type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 1
        if not isinstance(probs, dict) or set(probs) != set(mapping) or not all(finite_prob(v) and abs(v-round(v, 4)) <= 1e-12 for v in probs.values()):
            raise LayaBackendError("invalid_probabilities")
        total = math.fsum(probs.values())
        if total <= 0 or abs(total-1) > len(probs)*.00005+1e-12:
            raise LayaBackendError("invalid_probabilities")
        if not all(finite_prob(answer[k]) for k in ("confidence", "answer_confidence")):
            raise LayaBackendError("invalid_confidence")
        if probs[choice] != max(probs.values()) or abs(answer["answer_confidence"]-max(probs.values())) > 1e-12:
            raise LayaBackendError("choice_not_argmax")
        if not isinstance(answer["action"], dict) or set(answer["action"]) != {"act_probability"} or not finite_prob(answer["action"]["act_probability"]):
            raise LayaBackendError("answer_shape")
        selected = TaskSelection(context.request_id, context.task_session, context.task_revision,
            context.catalog_revision, context.option_set_id, mapping[choice], self.config.model, self.config.revision,
            WEIGHT_HASH, self.generation, elapsed, context.input_hash, {mapping[k]: v/total for k, v in probs.items()})
        return selected, {"raw_sum": total, "rounding_bound": len(probs)*.00005}

    def select(self, context):
        if not isinstance(context, TaskContext):
            raise LayaBackendError("invalid_snapshot")
        started = time.monotonic_ns()
        call = self._begin(started/1e9)
        call.record = {"started_monotonic_ns": started, "request_id": context.request_id,
                       "task_revision": context.task_revision, "post_attempted": False, "phase": "prepare"}
        code = None
        try:
            body, mapping, budgets = self.request_payload(context)
            self._record_update(call, request_body=body.decode("ascii"), input_sha256=hashlib.sha256(body).hexdigest(),
                                option_mapping=mapping, budgets=budgets, service_generation=self.generation)
            return self._run_worker(call, lambda: self._infer(call, context, body, mapping))
        except LayaBackendError as exc:
            code = exc.code
            raise
        except Exception:
            code = "invalid_snapshot"
            raise LayaBackendError(code) from None
        finally:
            with self._lock:
                if code is not None and call.attempted and not call.known_rejection:
                    self._restart_required = True
                call.record.update(completed_monotonic_ns=time.monotonic_ns(), error_code=code, restart_required=self._restart_required)
                self._record, self._last_error = copy.deepcopy(call.record), code
                if self._active is call:
                    self._active = None

class TaskModelSession:
    """Trusted in-process composition around the same manager used by B08.

    execution_idle must verify EX actions and the physical stop boundary.
    This object never changes EX backend/mode and never submits a Goal.
    """
    def __init__(self, owner, *, execution_idle, transport=None):
        if not callable(execution_idle):
            raise ValueError("execution_idle_callback_required")
        self.owner, self.execution_idle, self.transport = owner, execution_idle, transport
        self.client = None

    @property
    def generation(self):
        return self.owner.generation

    def attach(self):
        generation = self.owner.generation
        self.owner.guard(generation)
        if self.client is not None:
            self.client.close()
        self.client = TaskLayaClient(LayaConfig(enabled=True, allow_live_http=True, port=self.owner.deployment.port, deadline_ms=300), transport=self.transport)
        self.client.generation = generation
        return self

    def start(self):
        if not self.execution_idle():
            raise OwnedLayaError("execution_not_stopped")
        self.owner.start()
        return self.attach()

    def select(self, context):
        if self.client is None:
            raise OwnedLayaError("service_not_ready")
        with self.owner.decision_guard(self.client.generation, self.client):
            return self.client.select(context)

    def cancel(self):
        if self.client is not None:
            self.client.cancel()

    def recover(self):
        if not self.execution_idle():
            raise OwnedLayaError("execution_not_stopped")
        self.cancel()
        old_generation = self.owner.generation
        exited = self.owner.stop(expected_generation=old_generation)
        if not self.owner.requests_idle():
            raise OwnedLayaError("old_requests_pending")
        self.owner.start(recovery=True)
        self.attach()
        return {"old_exit": exited, "new_generation": self.generation, "replayed": False}
