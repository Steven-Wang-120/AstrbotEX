from __future__ import annotations

import copy
import json
import math
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class _Invocation:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: Future[Any] | None = None
    coalesce_key: str | None = None


@dataclass(slots=True)
class _ActionInvocation:
    args: tuple[Any, ...]
    future: Future[Any]
    size: int
    queued_at: float


class ActionMailboxFull(RuntimeError):
    """An action or cancel could not fit in its bounded queue."""


class ActionGuardRejected(RuntimeError):
    """The dispatcher's execution-time start guard refused a command."""


_STOP = object()


_MAX_ACTION_QUEUE_COUNT = 10_000
_MAX_ACTION_QUEUE_BYTES = 64 * 1024 * 1024
_MAX_ACTION_CALLBACK_BUDGET_MS = 60_000.0
_MAX_CANCEL_TEXT_LEN = 256


def _require_positive_int(value: Any, name: str, maximum: int) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be a positive int <= {maximum}")
    return value


def _require_finite_positive_number(value: Any, name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number <= {maximum}")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > maximum:
        raise ValueError(f"{name} must be a finite positive number <= {maximum}")
    return number


def _require_bounded_text(value: Any, name: str, maximum: int, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        empty_note = " and non-empty" if not allow_empty else ""
        raise ValueError(f"{name} must be a string of at most {maximum} characters{empty_note}")
    return value


class PluginActor:
    """Serializes all calls to one plugin on one managed worker thread."""

    def __init__(
        self,
        plugin: Any,
        *,
        call_timeout: float = 2.0,
        action_max_count: int = 64,
        action_max_bytes: int = 1_048_576,
        cancel_max_count: int = 16,
        cancel_max_bytes: int = 65_536,
        action_callback_budget_ms: float = 20.0,
    ) -> None:
        action_max_count = _require_positive_int(action_max_count, "action_max_count", _MAX_ACTION_QUEUE_COUNT)
        cancel_max_count = _require_positive_int(cancel_max_count, "cancel_max_count", _MAX_ACTION_QUEUE_COUNT)
        action_max_bytes = _require_positive_int(action_max_bytes, "action_max_bytes", _MAX_ACTION_QUEUE_BYTES)
        cancel_max_bytes = _require_positive_int(cancel_max_bytes, "cancel_max_bytes", _MAX_ACTION_QUEUE_BYTES)
        action_callback_budget_ms = _require_finite_positive_number(
            action_callback_budget_ms, "action_callback_budget_ms", _MAX_ACTION_CALLBACK_BUDGET_MS
        )
        self.plugin = plugin
        self.call_timeout = call_timeout
        plugin_id = getattr(plugin, "id", plugin.__class__.__name__)
        self.thread_name = f"astrbotex-plugin-{plugin_id}"
        self._mailbox: queue.Queue[_Invocation | object] = queue.Queue()
        self._pending_keys: set[str] = set()
        self._pending_lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._runtime_active = False
        self._enabled = False
        self.last_error: str | None = None
        self._action_condition = threading.Condition()
        self._starts: deque[_ActionInvocation] = deque()
        self._cancels: deque[_ActionInvocation] = deque()
        self._start_bytes = 0
        self._cancel_bytes = 0
        self._action_max_count = action_max_count
        self._action_max_bytes = action_max_bytes
        self._cancel_max_count = cancel_max_count
        self._cancel_max_bytes = cancel_max_bytes
        self._action_callback_budget_ms = action_callback_budget_ms
        self._action_guard: Callable[[Any], bool] | None = None
        self._lifecycle_ready = True
        self._action_draining = False
        self._stop_draining = False
        self._stop_token = None
        self._closing = False
        self._last_action_duration_ms: float | None = None
        self._last_action_fault: str | None = None
        self._action_budget_exceeded = 0

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def has_method(self, method: str) -> bool:
        return callable(getattr(self.plugin, method, None))

    def start(self) -> None:
        with self._action_condition:
            if self._closing:
                raise RuntimeError(f"plugin actor is closed: {self.thread_name}")
            if self.alive:
                return
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name=self.thread_name, daemon=True)
            self._thread.start()
        if not self._ready.wait(timeout=self.call_timeout):
            raise TimeoutError(f"plugin actor did not start: {self.thread_name}")

    def call(self, method: str, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        if threading.current_thread() is self._thread:
            if self._closing:
                raise RuntimeError(f"plugin actor is closing: {self.thread_name}")
            return self._invoke(method, args, kwargs)
        with self._action_condition:
            if self._closing or not self.alive:
                raise RuntimeError(f"plugin actor is not running: {self.thread_name}")
            future: Future[Any] = Future()
            self._mailbox.put(_Invocation(method=method, args=args, kwargs=kwargs, future=future))
            self._action_condition.notify()
        return future.result(timeout=self.call_timeout if timeout is None else timeout)

    def cast(
        self,
        method: str,
        *args: Any,
        coalesce_key: str | None = None,
        **kwargs: Any,
    ) -> bool:
        with self._action_condition:
            if self._closing or not self.alive:
                return False
            if coalesce_key:
                with self._pending_lock:
                    if coalesce_key in self._pending_keys:
                        return False
                    self._pending_keys.add(coalesce_key)
            self._mailbox.put(
                _Invocation(method=method, args=args, kwargs=kwargs, coalesce_key=coalesce_key)
            )
            self._action_condition.notify()
            return True

    def set_action_guard(self, guard: Callable[[Any], bool] | None) -> None:
        """Install a trusted, fast in-memory guard; None closes start admission."""
        if guard is not None and not callable(guard):
            raise TypeError("action guard must be callable")
        with self._action_condition:
            self._action_guard = guard

    def set_lifecycle_ready(self, ready: bool) -> None:
        """Atomically open or close lifecycle admission without replacing the dispatcher guard."""
        if type(ready) is not bool:
            raise TypeError("ready must be a bool")
        with self._action_condition:
            self._lifecycle_ready = ready
            self._action_condition.notify_all()

    def lifecycle_ready(self) -> bool:
        with self._action_condition:
            return self._lifecycle_ready

    def set_stop_token(self, token: str | None) -> None:
        with self._action_condition:
            self._stop_token = token
            self._action_condition.notify_all()

    def stop_token(self) -> str | None:
        with self._action_condition:
            return self._stop_token

    def set_action_draining(self, draining: bool) -> None:
        """Keep stop-progress worker steps alive after ordinary runtime ticks stop."""
        if type(draining) is not bool:
            raise TypeError("draining must be a bool")
        with self._action_condition:
            self._action_draining = draining
            self._action_condition.notify()

    def set_stop_draining(self, draining: bool) -> None:
        """Keep worker steps running until a stop callback and proof complete."""
        if type(draining) is not bool:
            raise TypeError("draining must be a bool")
        with self._action_condition:
            self._stop_draining = draining
            self._action_condition.notify()

    def submit_action(self, command: Any) -> Future[Any]:
        """Queue a frozen command for on_action_command; never select a method from payload."""
        frozen = copy.deepcopy(command)
        payload = frozen.to_dict() if callable(getattr(frozen, "to_dict", None)) else frozen
        size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        return self._enqueue_action((frozen,), size, cancel=False)

    def submit_action_cancel(self, command_id: str, reason: str) -> Future[Any]:
        """Queue a distinct high-priority on_action_cancel invocation."""
        _require_bounded_text(command_id, "command_id", _MAX_CANCEL_TEXT_LEN, allow_empty=False)
        _require_bounded_text(reason, "reason", _MAX_CANCEL_TEXT_LEN)
        size = len(json.dumps([command_id, reason], ensure_ascii=False).encode("utf-8"))
        return self._enqueue_action((command_id, reason), size, cancel=True)

    def _enqueue_action(self, args: tuple[Any, ...], size: int, *, cancel: bool) -> Future[Any]:
        future: Future[Any] = Future()
        with self._action_condition:
            if self._closing or not self.alive:
                raise RuntimeError(f"plugin actor is not running: {self.thread_name}")
            mailbox = self._cancels if cancel else self._starts
            used = self._cancel_bytes if cancel else self._start_bytes
            count_limit = self._cancel_max_count if cancel else self._action_max_count
            byte_limit = self._cancel_max_bytes if cancel else self._action_max_bytes
            if len(mailbox) >= count_limit or used + size > byte_limit:
                raise ActionMailboxFull(f"{'cancel' if cancel else 'start'} mailbox busy: {self.thread_name}")
            item = _ActionInvocation(args=args, future=future, size=size, queued_at=time.monotonic())
            mailbox.append(item)
            if cancel:
                self._cancel_bytes += size
            else:
                self._start_bytes += size
            self._action_condition.notify()
        future.add_done_callback(lambda done: self._refund_canceled(item, cancel=cancel) if done.cancelled() else None)
        return future

    def _refund_canceled(self, item: _ActionInvocation, *, cancel: bool) -> None:
        with self._action_condition:
            mailbox = self._cancels if cancel else self._starts
            try:
                mailbox.remove(item)
            except ValueError:
                return
            if cancel:
                self._cancel_bytes -= item.size
            else:
                self._start_bytes -= item.size

    def action_mailbox_stats(self) -> dict[str, Any]:
        """Return cached queue metrics without waiting for a plugin callback."""
        with self._action_condition:
            now = time.monotonic()
            return {
                "start_count": len(self._starts),
                "start_bytes": self._start_bytes,
                "cancel_count": len(self._cancels),
                "cancel_bytes": self._cancel_bytes,
                "oldest_start_ms": (now - self._starts[0].queued_at) * 1000 if self._starts else None,
                "oldest_cancel_ms": (now - self._cancels[0].queued_at) * 1000 if self._cancels else None,
                "last_action_duration_ms": self._last_action_duration_ms,
                "last_action_fault": self._last_action_fault,
                "action_budget_exceeded": self._action_budget_exceeded,
                "draining": self._action_draining,
                "closing": self._closing,
            }

    def stop(self, timeout: float = 2.0) -> None:
        with self._action_condition:
            thread = self._thread
            if thread is None:
                return
            self._closing = True
            pending = self._detach_pending_locked()
            self._action_condition.notify_all()
        self._resolve_pending(pending)
        if threading.current_thread() is not thread:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise TimeoutError(f"plugin actor did not stop: {self.thread_name}")
        # Keep the thread reference: alive remains truthful on a timed-out stop.

    def _run(self) -> None:
        self._ready.set()
        try:
            while True:
                invocation = self._next_invocation()
                if invocation is _STOP:
                    break
                if isinstance(invocation, _ActionInvocation):
                    self._execute_action(invocation)
                elif isinstance(invocation, _Invocation):
                    self._execute(invocation)
                elif not self._closing and self._worker_step_enabled():
                    try:
                        self._invoke("on_worker_step", (), {})
                    except Exception as exc:
                        self.last_error = str(exc)
                        self._runtime_active = False
                        self._emit_worker_error_safely(exc)
        finally:
            with self._action_condition:
                self._closing = True
                pending = self._detach_pending_locked()
                self._action_condition.notify_all()
            self._resolve_pending(pending)

    def _worker_step_enabled(self) -> bool:
        return (self._stop_draining or self._action_draining or (self._runtime_active and self._enabled)) and self.has_method("on_worker_step")

    def _next_invocation(self) -> _Invocation | _ActionInvocation | object | None:
        with self._action_condition:
            while True:
                if self._closing:
                    return _STOP
                if self._cancels:
                    item = self._cancels.popleft()
                    self._cancel_bytes -= item.size
                    return item
                if self._starts:
                    item = self._starts.popleft()
                    self._start_bytes -= item.size
                    return item
                try:
                    return self._mailbox.get_nowait()
                except queue.Empty:
                    if self._worker_step_enabled():
                        return None
                    self._action_condition.wait(timeout=0.1)

    def _execute_action(self, invocation: _ActionInvocation) -> None:
        future = invocation.future
        if not future.set_running_or_notify_cancel():
            return
        started = time.perf_counter()
        try:
            if len(invocation.args) == 1:
                with self._action_condition:
                    guard = self._action_guard
                    lifecycle_ready = self._lifecycle_ready
                    closing = self._closing
                if closing or not lifecycle_ready or guard is None or guard(invocation.args[0]) is not True:
                    raise ActionGuardRejected(f"action start refused at execution: {self.thread_name}")
                with self._action_condition:
                    if self._closing or not self._lifecycle_ready:
                        raise ActionGuardRejected(f"action start refused during lifecycle transition: {self.thread_name}")
                method = "on_action_command"
            else:
                with self._action_condition:
                    if self._closing:
                        raise RuntimeError(f"plugin actor stopped: {self.thread_name}")
                method = "on_action_cancel"
            if not self.has_method(method):
                raise RuntimeError(f"plugin actor missing {method}: {self.thread_name}")
            result = self._invoke(method, invocation.args, {})
        except Exception as exc:
            self._record_action_timing(started, fault=str(exc))
            if not isinstance(exc, ActionGuardRejected):
                self.last_error = str(exc)
            future.set_exception(exc)
        else:
            self._record_action_timing(started)
            future.set_result(result)

    def _record_action_timing(self, started: float, *, fault: str | None = None) -> None:
        duration = (time.perf_counter() - started) * 1000
        with self._action_condition:
            self._last_action_duration_ms = duration
            if fault is not None:
                self._last_action_fault = fault
            if duration > self._action_callback_budget_ms:
                self._action_budget_exceeded += 1
                if fault is None:
                    self._last_action_fault = f"action callback exceeded {self._action_callback_budget_ms:g}ms: {duration:.2f}ms"

    def _execute(self, invocation: _Invocation) -> None:
        if invocation.future is not None and not invocation.future.set_running_or_notify_cancel():
            if invocation.coalesce_key:
                with self._pending_lock:
                    self._pending_keys.discard(invocation.coalesce_key)
            return
        try:
            result = self._invoke(invocation.method, invocation.args, invocation.kwargs)
        except Exception as exc:
            self.last_error = str(exc)
            if invocation.future is not None:
                invocation.future.set_exception(exc)
            self._emit_worker_error_safely(exc)
        else:
            if invocation.future is not None:
                invocation.future.set_result(result)
        finally:
            if invocation.coalesce_key:
                with self._pending_lock:
                    self._pending_keys.discard(invocation.coalesce_key)

    def _invoke(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        callback = getattr(self.plugin, method, None)
        operation_token = kwargs.pop('_astrbotex_environment_token', None)
        ros = getattr(self.plugin, '_astrbotex_ros', None)
        if ros is not None and method == "on_action_cancel":
            operation_token = self.stop_token() or operation_token
        if method == "on_runtime_stop":
            self._runtime_active = False
        if ros is not None and method in {"on_action_cancel", "on_environment_deactivating"}:
            ros._hook_local.token = operation_token
        try:
            result = callback(*args, **kwargs) if callable(callback) else None
        finally:
            if ros is not None and method in {"on_action_cancel", "on_environment_deactivating"}:
                ros._hook_local.token = None
        if method == "on_enable":
            self._enabled = True
        elif method == "on_disable":
            self._enabled = False
            self._runtime_active = False
        elif method == "on_runtime_start":
            self._runtime_active = True
            self.last_error = None
        return result

    def _emit_worker_error_safely(self, exc: Exception) -> None:
        try:
            self._emit_worker_error(exc)
        except Exception as diagnostic_error:
            self.last_error = f"{exc}; diagnostic emit failed: {diagnostic_error}"

    def _emit_worker_error(self, exc: Exception) -> None:
        context = getattr(self.plugin, "context", None)
        event_bus = getattr(context, "event_bus", None)
        if event_bus is not None:
            event_bus.emit(
                "plugin_fault",
                "plugin worker failed",
                plugin=getattr(self.plugin, "id", self.plugin.__class__.__name__),
                error=str(exc),
            )

    def _detach_pending_locked(self) -> list[_ActionInvocation | _Invocation]:
        pending: list[_ActionInvocation | _Invocation] = list(self._cancels) + list(self._starts)
        self._cancels.clear()
        self._starts.clear()
        self._start_bytes = 0
        self._cancel_bytes = 0
        while True:
            try:
                item = self._mailbox.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, _Invocation):
                pending.append(item)
        return pending

    def _resolve_pending(self, pending: list[_ActionInvocation | _Invocation]) -> None:
        for item in pending:
            if item.future is not None and item.future.set_running_or_notify_cancel():
                item.future.set_exception(RuntimeError(f"plugin actor stopped: {self.thread_name}"))
            if isinstance(item, _Invocation) and item.coalesce_key:
                with self._pending_lock:
                    self._pending_keys.discard(item.coalesce_key)
