"""Injectable B00 decision routing; no hardware or service composition here."""
from __future__ import annotations

import threading
from typing import Any, Callable

from astrbot_ex.core.actions.models import ContractError as ActionContractError
from astrbot_ex.core.contracts import ContractError, parse_request, require_id, require_sequence, require_text


class DecisionTransport:
    def __init__(self, handler: Callable, *, public_validator: Callable | None = None) -> None:
        self.handler = handler
        self.public_validator = public_validator
        self._lock = threading.RLock()
        self._messages: dict[tuple[str, str], dict] = {}

    def handle(self, connection_id: str, feature: str, method: str, payload: dict,
               binary: bytes | None) -> tuple[dict, None]:
        if feature != "text" or binary is not None or not connection_id:
            return {"ok": False, "error": {"code": "decision_route_rejected"}}, None
        try:
            parsed = parse_request(method, payload)
            result = self.handler(connection_id, method, parsed)
            if not isinstance(result, dict):
                raise ValueError("invalid decision handler response")
            return result, None
        except (ContractError, ActionContractError) as exc:
            error = exc.error.to_dict()
            error["message"] = "Decision request rejected."
            return {"ok": False, "error": error}, None
        except Exception:
            return {"ok": False, "error": {"code": "decision_handler_failed"}}, None

    def admit_public(self, connection_id: str, payload: dict, binary: bytes | None) -> dict:
        """Reject task replies unless live framework task/turn/generation validation is wired.

        Claim before legacy publishing/TTS; ambiguous failures never replay the action.
        The same validator must be called AGAIN at asynchronous audio playback commit.
        """
        if not connection_id or binary is not None or self.public_validator is None:
            return {"ok": False, "error": "task_public_gate_unavailable"}
        try:
            if payload.get("visibility") != "user" or payload.get("source") != "private_planning":
                raise ValueError("invalid visibility")
            for key in ("task_id", "turn_id", "route_ref", "message_id", "ex_session", "session_id", "robot_id"):
                require_id(payload.get(key), key)
            require_sequence(payload.get("generation"), "generation", minimum=1)
            require_text(payload.get("text"), "text", max_len=4096)
            if payload.get("delivery") not in {"text", "tts"}:
                raise ValueError("choose one delivery")
            if self.public_validator(connection_id, payload) is not True:
                raise ValueError("stale or unauthorized public message")
            key = (connection_id, payload["message_id"])
            with self._lock:
                old = self._messages.get(key)
                if old is not None:
                    if old != payload:
                        return {"ok": False, "error": "duplicate_message_id_conflict"}
                    return {"ok": True, "duplicate": True}
                if len(self._messages) >= 10000:
                    return {"ok": False, "error": "public_dedup_capacity"}
                import copy
                self._messages[key] = copy.deepcopy(payload)
            return {"ok": True, "duplicate": False}
        except Exception:
            return {"ok": False, "error": "task_public_rejected"}
