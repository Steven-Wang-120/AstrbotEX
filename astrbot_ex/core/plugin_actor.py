from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class _Invocation:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: Future[Any] | None = None
    coalesce_key: str | None = None


_STOP = object()


class PluginActor:
    """Serializes all calls to one plugin on one managed worker thread."""

    def __init__(self, plugin: Any, *, call_timeout: float = 2.0) -> None:
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

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def has_method(self, method: str) -> bool:
        return callable(getattr(self.plugin, method, None))

    def start(self) -> None:
        if self.alive:
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name=self.thread_name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=self.call_timeout):
            raise TimeoutError(f"plugin actor did not start: {self.thread_name}")

    def call(self, method: str, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        if threading.current_thread() is self._thread:
            return self._invoke(method, args, kwargs)
        if not self.alive:
            raise RuntimeError(f"plugin actor is not running: {self.thread_name}")
        future: Future[Any] = Future()
        self._mailbox.put(_Invocation(method=method, args=args, kwargs=kwargs, future=future))
        return future.result(timeout=self.call_timeout if timeout is None else timeout)

    def cast(
        self,
        method: str,
        *args: Any,
        coalesce_key: str | None = None,
        **kwargs: Any,
    ) -> bool:
        if not self.alive:
            return False
        if coalesce_key:
            with self._pending_lock:
                if coalesce_key in self._pending_keys:
                    return False
                self._pending_keys.add(coalesce_key)
        self._mailbox.put(
            _Invocation(
                method=method,
                args=args,
                kwargs=kwargs,
                coalesce_key=coalesce_key,
            )
        )
        return True

    def stop(self, timeout: float = 2.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._mailbox.put(_STOP)
        if threading.current_thread() is not thread:
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise TimeoutError(f"plugin actor did not stop: {self.thread_name}")
        self._thread = None

    def _run(self) -> None:
        self._ready.set()
        while True:
            invocation = self._next_invocation()
            if invocation is _STOP:
                break
            if isinstance(invocation, _Invocation):
                self._execute(invocation)
                continue
            if self._runtime_active and self._enabled and self.has_method("on_worker_step"):
                try:
                    self._invoke("on_worker_step", (), {})
                except Exception as exc:
                    self.last_error = str(exc)
                    self._runtime_active = False
                    self._emit_worker_error(exc)

        self._fail_pending(RuntimeError(f"plugin actor stopped: {self.thread_name}"))

    def _next_invocation(self) -> _Invocation | object | None:
        try:
            if self._runtime_active and self._enabled and self.has_method("on_worker_step"):
                return self._mailbox.get_nowait()
            return self._mailbox.get(timeout=0.1)
        except queue.Empty:
            return None

    def _execute(self, invocation: _Invocation) -> None:
        try:
            result = self._invoke(invocation.method, invocation.args, invocation.kwargs)
        except Exception as exc:
            self.last_error = str(exc)
            self._emit_worker_error(exc)
            if invocation.future is not None:
                invocation.future.set_exception(exc)
        else:
            if invocation.future is not None:
                invocation.future.set_result(result)
        finally:
            if invocation.coalesce_key:
                with self._pending_lock:
                    self._pending_keys.discard(invocation.coalesce_key)

    def _invoke(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        callback = getattr(self.plugin, method, None)
        if method == "on_runtime_stop":
            self._runtime_active = False
        result = callback(*args, **kwargs) if callable(callback) else None
        if method == "on_enable":
            self._enabled = True
        elif method == "on_disable":
            self._enabled = False
            self._runtime_active = False
        elif method == "on_runtime_start":
            self._runtime_active = True
            self.last_error = None
        return result

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

    def _fail_pending(self, exc: Exception) -> None:
        while True:
            try:
                item = self._mailbox.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, _Invocation):
                if item.future is not None:
                    item.future.set_exception(exc)
                if item.coalesce_key:
                    with self._pending_lock:
                        self._pending_keys.discard(item.coalesce_key)
