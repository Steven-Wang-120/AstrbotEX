"""Plugin-facing report API. Only the trusted composition layer may bind it.

The facade starts closed; construction/on-load code cannot report until binding.
Bind once, before on_load, with a dispatcher callback and immutable OwnerBinding.
Revocation prevents subsequent calls; an already submitted Future remains owned by
its callback. During stopping, keep the old facade bound so stop confirmation can
arrive; the dispatcher enforces generation, command ownership and gate policy.
Revocation is for completed unload, never a substitute for safe-stop confirmation.
In-process plugins are trusted Python code, not a security sandbox.
"""

from __future__ import annotations

import copy
import threading
from concurrent.futures import Future
from typing import Any, Callable

from .ledger import OwnerBinding, StopEvidence
from .models import LEGAL_TRANSITIONS, MAX_ID_LEN, MAX_SEQUENCE, measure_json_budget

ReportCallback = Callable[[str, OwnerBinding, str], Future[Any]]


class PluginActionAPI:
    __slots__ = ("__binding", "__callback", "__lock", "__bound", "__revoked")

    def __init__(self) -> None:
        self.__binding: OwnerBinding | None = None
        self.__callback: Callable[..., Future[Any]] | None = None
        self.__lock = threading.Lock()
        self.__bound = False
        self.__revoked = False

    def _bind(self, binding: OwnerBinding, callback: Callable[..., Future[Any]]) -> None:
        """Internal registration hook; never expose this to plugin declarations."""
        if (not isinstance(binding, OwnerBinding) or type(binding.owner) is not str
                or not binding.owner or len(binding.owner) > MAX_ID_LEN
                or type(binding.generation) is not int
                or not 0 <= binding.generation <= MAX_SEQUENCE or not callable(callback)):
            raise ValueError("invalid trusted action binding")
        with self.__lock:
            if self.__bound or self.__revoked:
                raise RuntimeError("action facade cannot be rebound")
            self.__binding = OwnerBinding(binding.owner, binding.generation)
            self.__callback = callback
            self.__bound = True

    def _revoke(self) -> None:
        """Close new submissions only after the old generation is safely stopped."""
        with self.__lock:
            self.__revoked = True
            self.__callback = None

    def report(self, command_id: str, status: str, *, reason_code: str = "",
               details: dict[str, Any] | None = None,
               stop_evidence: StopEvidence | None = None) -> Future[Any]:
        if type(command_id) is not str or not command_id.strip() or len(command_id) > MAX_ID_LEN:
            raise ValueError("invalid command_id")
        if type(status) is not str or status not in LEGAL_TRANSITIONS:
            raise ValueError("invalid status")
        if type(reason_code) is not str or len(reason_code) > MAX_ID_LEN:
            raise ValueError("invalid reason_code")
        payload = {} if details is None else details
        if not isinstance(payload, dict) or measure_json_budget(payload) is not None:
            raise ValueError("invalid report details")
        if "stop_evidence" in payload:
            raise ValueError("stop_evidence is reserved")
        if stop_evidence is not None and not isinstance(stop_evidence, StopEvidence):
            raise ValueError("stop_evidence must be structured")
        if status == "canceled" and (stop_evidence is None or
                stop_evidence.command_id != command_id or stop_evidence.stopped is not True or
                any(type(v) is not str or not v.strip() or len(v) > MAX_ID_LEN
                    for v in (stop_evidence.source, stop_evidence.reference))):
            raise ValueError("positive structured stop evidence required")
        with self.__lock:
            if self.__callback is None or self.__binding is None or self.__revoked:
                raise RuntimeError("action facade is unbound or revoked")
            callback, binding = self.__callback, self.__binding
        # Dispatcher validates generation and command before delegating to the ledger.
        future = callback(command_id, binding, status,
                          reason_code=reason_code, details=copy.deepcopy(payload),
                          stop_evidence=stop_evidence)
        if not isinstance(future, Future):
            raise TypeError("dispatcher report callback must return Future")
        return future
