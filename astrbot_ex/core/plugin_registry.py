from __future__ import annotations

from dataclasses import dataclass, field
from threading import Condition, RLock
from typing import Any, Callable

from astrbot_ex.core.plugin_actor import PluginActor


@dataclass(slots=True)
class PluginSlot:
    kind: str
    plugin: Any
    actor: PluginActor
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    generation: int = 0
    state: str = "loading"
    stop_error: str | None = None
    runtime_started: bool = False
    stop_proven: bool = False
    unloaded: bool = False
    ros_closed: bool = False
    runtime_starting: bool = False
    runtime_start_epoch: int | None = None
    stop_requested: bool = False
    teardown_requested: bool = False

    @property
    def id(self) -> str:
        return str(getattr(self.plugin, "id", self.plugin.__class__.__name__))

    @property
    def name(self) -> str:
        return str(getattr(self.plugin, "name", self.plugin.__class__.__name__))

    @property
    def action_owner(self) -> bool:
        return self.kind == "action" or "action_owner" in self.metadata.get("provides", ())

    def has_method(self, method: str) -> bool:
        return self.actor.has_method(method)

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return self.actor.call(method, *args, **kwargs)

    def cast(self, method: str, *args: Any, coalesce_key: str | None = None, **kwargs: Any) -> bool:
        return self.actor.cast(method, *args, coalesce_key=coalesce_key, **kwargs)


class PluginRegistry:
    _LEGACY_RUNTIME_KINDS = frozenset({"policy", "skill", "motion"})

    def __init__(self) -> None:
        self._slots: dict[str, PluginSlot] = {}
        self._generations: dict[str, int] = {}
        self._busy: set[str] = set()
        self._pending_stop: set[str] = set()
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._runtime_active = False
        self._runtime_stopping = False
        self._runtime_epoch = 0
        self._runtime_mode = "legacy"
        self._action_lifecycle_guard: Callable[[PluginSlot, str], bool] | None = None

    def set_action_lifecycle_guard(self, callback: Callable[[PluginSlot, str], bool] | None) -> None:
        """Install a trusted Dispatcher stop-proof hook; only literal True proves stop.

        The hook is called outside the registry lock after start admission closes and
        draining begins. It must corroborate the ledger/device stop, not merely a
        cancel request or an on_runtime_stop callback return.
        """
        if callback is not None and not callable(callback):
            raise TypeError("action lifecycle guard must be callable")
        with self._lock:
            self._action_lifecycle_guard = callback

    def set_runtime_mode(self, mode: str) -> None:
        if mode not in {"legacy", "decision"}:
            raise ValueError("runtime mode must be legacy or decision")
        with self._condition:
            self._runtime_mode = mode
            if mode == "decision":
                for slot in self._slots.values():
                    if slot.kind in self._LEGACY_RUNTIME_KINDS:
                        slot.actor.set_lifecycle_ready(False)

    def _runtime_slot_allowed(self, slot: PluginSlot) -> bool:
        return self._runtime_mode != "decision" or slot.kind not in self._LEGACY_RUNTIME_KINDS

    def register(
        self,
        kind: str,
        plugin: Any,
        *,
        enabled: bool = True,
        metadata: dict[str, Any] | None = None,
        before_load: Callable[[PluginSlot], None] | None = None,
    ) -> PluginSlot:
        plugin_id = str(getattr(plugin, "id", plugin.__class__.__name__))
        with self._lock:
            if plugin_id in self._slots:
                raise ValueError(f"Plugin already registered: {plugin_id}")
            generation = self._generations.get(plugin_id, 0) + 1
            self._generations[plugin_id] = generation
            actor = PluginActor(plugin)
            actor.set_lifecycle_ready(False)
            slot = PluginSlot(kind, plugin, actor, enabled, dict(metadata or {}), generation)
            self._slots[plugin_id] = slot
            self._busy.add(plugin_id)
        try:
            ros = getattr(plugin, "_astrbotex_ros", None)
            if ros is not None:
                ros.actor = actor
            actor.set_action_guard(None)
            actor.start()
            if before_load is not None:
                before_load(slot)
            slot.call("on_load")
            if enabled:
                slot.call("on_enable")
                with self._lock:
                    runtime_active = self._runtime_active
                    runtime_epoch = self._runtime_epoch
                    cancelled = slot.stop_requested or slot.teardown_requested
                if runtime_active and not cancelled and self._runtime_slot_allowed(slot):
                    self._run_runtime_start(slot, runtime_epoch)
                elif runtime_active and not cancelled:
                    slot.state = "ready"
                    slot.runtime_started = False
                    slot.actor.set_lifecycle_ready(False)
                elif cancelled:
                    self._revoke(slot, "runtime stopped during plugin registration")
            with self._lock:
                runtime_stopped_during_load = slot.state == "stopping" or slot.stop_requested
            if runtime_stopped_during_load and slot.runtime_started:
                with self._lock:
                    externally_stopping = plugin_id in self._pending_stop or slot.teardown_requested
                if not externally_stopping:
                    self._stop(slot, "runtime stopped during plugin registration")
            with self._lock:
                if slot.teardown_requested or plugin_id in self._pending_stop:
                    slot.state = "stopping"
                else:
                    slot.state = "ready"
                    slot.stop_requested = False
                    slot.actor.set_lifecycle_ready(self._runtime_slot_allowed(slot))
            return slot
        except Exception as exc:
            self._blocked(slot, exc)
            try:
                self._teardown(slot, "plugin registration failed")
            except Exception as cleanup_error:
                self._blocked(slot, cleanup_error)
                with self._lock:
                    slot.stop_error = f"registration failed: {exc}; cleanup failed: {cleanup_error}"
                raise cleanup_error from exc
            with self._lock:
                if self._slots.get(plugin_id) is slot:
                    del self._slots[plugin_id]
                self._busy.discard(plugin_id)
                self._pending_stop.discard(plugin_id)
                self._condition.notify_all()
            raise
        finally:
            self._release(slot)

    def _claim(self, plugin_id: str) -> PluginSlot:
        with self._lock:
            slot = self._slots[plugin_id]
            if plugin_id in self._busy or plugin_id in self._pending_stop:
                raise RuntimeError(f"Plugin lifecycle in progress: {plugin_id}")
            self._busy.add(plugin_id)
            return slot

    def _release(self, slot: PluginSlot) -> None:
        with self._condition:
            if self._slots.get(slot.id) is slot:
                self._busy.discard(slot.id)
            self._condition.notify_all()

    def _claim_for_stop(self, plugin_id: str, reason: str, *, teardown: bool = False) -> PluginSlot:
        with self._condition:
            slot = self._slots[plugin_id]
            self._pending_stop.add(plugin_id)
            slot.stop_requested = True
            slot.teardown_requested = slot.teardown_requested or teardown
            slot.state = "stopping"
            slot.stop_error = None
        self._revoke(slot, reason)
        with self._condition:
            while self._slots.get(plugin_id) is slot and plugin_id in self._busy:
                self._condition.wait()
            if self._slots.get(plugin_id) is not slot:
                raise KeyError(f"Plugin instance changed while stopping: {plugin_id} generation {slot.generation}")
            self._pending_stop.discard(plugin_id)
            self._busy.add(plugin_id)
        return slot

    def _revoke(self, slot: PluginSlot, reason: str) -> None:
        """Close admission and keep the actor worker alive while stopping.

        Registry state is changed before taking the actor lock. The actor calls and
        any proof callback therefore never run while the registry lock is held.
        """
        with self._lock:
            if self._slots.get(slot.id) is slot:
                slot.stop_requested = True
                slot.state = "stopping"
        slot.actor.set_lifecycle_ready(False)
        slot.actor.set_stop_draining(True)
        if slot.action_owner:
            slot.actor.set_action_draining(True)

    def _prove_stop(self, slot: PluginSlot, reason: str) -> None:
        if not slot.action_owner:
            return
        with self._lock:
            callback = self._action_lifecycle_guard
        if callback is None or callback(slot, reason) is not True:
            raise RuntimeError(f"action owner stop not proven: {slot.id}")
        slot.stop_proven = True

    def _blocked(self, slot: PluginSlot, exc: Exception) -> None:
        with self._lock:
            if self._slots.get(slot.id) is slot:
                slot.state = "blocked"
                slot.stop_error = str(exc)
                slot.stop_requested = True

    def _stop(self, slot: PluginSlot, reason: str) -> None:
        self._prove_stop(slot, reason)
        if slot.runtime_started:
            slot.call("on_runtime_stop", reason)
            slot.runtime_started = False
        if slot.action_owner:
            slot.actor.set_action_draining(False)
        slot.actor.set_stop_draining(False)

    def _stop_requested_for(self, slot: PluginSlot, epoch: int) -> bool:
        with self._lock:
            return (
                slot.stop_requested
                or slot.teardown_requested
                or epoch != self._runtime_epoch
                or not self._runtime_active
                or self._slots.get(slot.id) is not slot
            )

    def _run_runtime_start(self, slot: PluginSlot, epoch: int) -> bool:
        with self._lock:
            if self._slots.get(slot.id) is not slot:
                return False
            if (
                not slot.enabled
                or slot.stop_requested
                or slot.teardown_requested
                or not self._runtime_active
                or epoch != self._runtime_epoch
            ):
                return False
            slot.runtime_starting = True
            slot.runtime_start_epoch = epoch
            slot.state = "starting"
        try:
            slot.call("on_runtime_start")
        except TimeoutError as exc:
            with self._lock:
                slot.runtime_starting = False
                slot.runtime_start_epoch = None
                slot.runtime_started = True
            self._blocked(slot, exc)
            raise
        except Exception as exc:
            with self._lock:
                slot.runtime_starting = False
                slot.runtime_start_epoch = None
                if slot.action_owner:
                    slot.runtime_started = True
                elif slot.state == "starting":
                    slot.state = "ready"
            if slot.action_owner:
                self._revoke(slot, "runtime start failed")
                self._blocked(slot, exc)
                raise
            return False

        with self._lock:
            slot.runtime_starting = False
            slot.runtime_start_epoch = None
            slot.runtime_started = True
            cancelled = self._stop_requested_for(slot, epoch)
            if not cancelled:
                slot.state = "ready"
                slot.stop_proven = False
                slot.actor.set_lifecycle_ready(self._runtime_slot_allowed(slot))
        if not cancelled:
            return True

        with self._lock:
            externally_stopping = slot.id in self._pending_stop or slot.teardown_requested
        if externally_stopping:
            return False
        try:
            self._stop(slot, "runtime stopped during start")
        except Exception as exc:
            self._blocked(slot, exc)
            raise
        with self._lock:
            if not slot.teardown_requested and slot.id not in self._pending_stop:
                slot.state = "ready"
                slot.stop_requested = False
                slot.stop_error = None
                slot.actor.set_lifecycle_ready(self._runtime_slot_allowed(slot))
        return False

    def _teardown(self, slot: PluginSlot, reason: str) -> None:
        self._revoke(slot, reason)
        self._stop(slot, reason)
        if slot.enabled:
            slot.call("on_disable")
            slot.enabled = False
        if slot.action_owner:
            if not slot.unloaded:
                slot.call("on_unload")
                slot.unloaded = True
            if not slot.ros_closed:
                ros = getattr(slot.plugin, "_astrbotex_ros", None)
                if ros is not None:
                    ros.close()
                slot.ros_closed = True
            slot.actor.stop()
        else:
            try:
                if not slot.unloaded:
                    slot.call("on_unload")
                    slot.unloaded = True
            finally:
                try:
                    if not slot.ros_closed:
                        ros = getattr(slot.plugin, "_astrbotex_ros", None)
                        if ros is not None:
                            ros.close()
                        slot.ros_closed = True
                finally:
                    slot.actor.stop()

    def unregister(self, plugin_id: str) -> None:
        slot = self._claim_for_stop(plugin_id, "plugin unregistered", teardown=True)
        try:
            self._teardown(slot, "plugin unregistered")
            with self._condition:
                if self._slots.get(plugin_id) is slot:
                    del self._slots[plugin_id]
                self._busy.discard(plugin_id)
                self._pending_stop.discard(plugin_id)
                self._condition.notify_all()
        except Exception as exc:
            self._blocked(slot, exc)
            raise
        finally:
            self._release(slot)

    def get_one(self, kind: str) -> Any | None:
        slot = self.get_slot(kind)
        return slot.plugin if slot else None

    def get_slot(self, kind: str) -> PluginSlot | None:
        with self._lock:
            for slot in self._slots.values():
                if slot.kind == kind and slot.enabled and slot.state == "ready":
                    return slot
            return None

    def get(self, plugin_id: str) -> PluginSlot | None:
        with self._lock:
            return self._slots.get(plugin_id)

    def list(self) -> list[PluginSlot]:
        with self._lock:
            return list(self._slots.values())

    def enable(self, plugin_id: str) -> None:
        slot = self._claim(plugin_id)
        try:
            if slot.state == "blocked":
                raise RuntimeError(f"Plugin blocked: {plugin_id}")
            if slot.enabled:
                return
            with self._lock:
                slot.enabled = True
                slot.state = "loading"
                enable_epoch = self._runtime_epoch
                runtime_active = self._runtime_active
            slot.call("on_enable")
            cancelled = self._stop_requested_for(slot, enable_epoch)
            if runtime_active and not cancelled and self._runtime_slot_allowed(slot):
                self._run_runtime_start(slot, enable_epoch)
                cancelled = self._stop_requested_for(slot, enable_epoch)
            elif runtime_active and not cancelled:
                slot.actor.set_lifecycle_ready(False)
                cancelled = False
            if cancelled:
                with self._lock:
                    externally_stopping = slot.id in self._pending_stop or slot.teardown_requested
                if slot.runtime_started and not externally_stopping:
                    self._stop(slot, "plugin enable cancelled by runtime stop")
                with self._lock:
                    if slot.id not in self._pending_stop and not slot.teardown_requested:
                        slot.state = "ready"
                        slot.stop_requested = False
                        slot.stop_error = None
                        slot.actor.set_lifecycle_ready(self._runtime_slot_allowed(slot))
            else:
                with self._lock:
                    if slot.id not in self._pending_stop:
                        slot.stop_proven = False
                        slot.state = "ready"
                        slot.actor.set_lifecycle_ready(self._runtime_slot_allowed(slot))
        except Exception as exc:
            self._blocked(slot, exc)
            raise
        finally:
            self._release(slot)

    def disable(self, plugin_id: str) -> None:
        slot = self._claim_for_stop(plugin_id, "plugin disabled")
        try:
            self._stop(slot, "plugin disabled")
            if slot.enabled:
                slot.call("on_disable")
                slot.enabled = False
            with self._lock:
                slot.state = "ready"
                slot.stop_requested = False
                slot.stop_error = None
                slot.actor.set_lifecycle_ready(False)
        except Exception as exc:
            self._blocked(slot, exc)
            raise
        finally:
            self._release(slot)

    def start_runtime(self) -> None:
        with self._condition:
            if self._runtime_active:
                return
            if self._runtime_stopping:
                raise RuntimeError("Runtime lifecycle stop in progress")
            if any(slot.state == "blocked" and slot.runtime_started for slot in self._slots.values()):
                raise RuntimeError("Runtime lifecycle stop blocked")
            self._runtime_active = True
            self._runtime_epoch += 1
            runtime_epoch = self._runtime_epoch
            slots = [slot for slot in self._slots.values() if slot.enabled and slot.state == "ready"]
        for slot in slots:
            try:
                claimed = self._claim(slot.id)
            except (KeyError, RuntimeError):
                continue
            try:
                with self._lock:
                    if (
                        claimed is not slot
                        or slot.state != "ready"
                        or not slot.enabled
                        or slot.stop_requested
                        or slot.teardown_requested
                        or not self._runtime_active
                        or runtime_epoch != self._runtime_epoch
                    ):
                        continue
                try:
                    if self._runtime_slot_allowed(slot):
                        self._run_runtime_start(slot, runtime_epoch)
                    else:
                        slot.actor.set_lifecycle_ready(False)
                except Exception:
                    # A stop proof failure is retained as blocked for stop_runtime.
                    continue
            finally:
                self._release(slot)

    def stop_runtime(self, reason: str) -> None:
        with self._condition:
            if self._runtime_stopping:
                raise RuntimeError("Runtime lifecycle stop in progress")
            self._runtime_stopping = True
            self._runtime_active = False
            self._runtime_epoch += 1
            slots = [
                slot
                for slot in self._slots.values()
                if slot.enabled or slot.runtime_started or slot.runtime_starting or slot.state == "stopping"
            ]
            reserved: list[PluginSlot] = []
            for slot in slots:
                slot.stop_requested = True
                slot.state = "stopping"
                slot.stop_error = None
                self._pending_stop.add(slot.id)
                if slot.id not in self._busy:
                    self._pending_stop.discard(slot.id)
                    self._busy.add(slot.id)
                    reserved.append(slot)
            self._condition.notify_all()

        # Revoke every admission before the first slow proof or plugin callback.
        for slot in slots:
            self._revoke(slot, reason)

        first_error: Exception | None = None
        for slot in reserved:
            try:
                self._stop(slot, reason)
                with self._lock:
                    if not slot.teardown_requested:
                        slot.state = "ready"
                        slot.stop_requested = False
                        slot.stop_error = None
                        slot.actor.set_lifecycle_ready(self._runtime_slot_allowed(slot))
            except Exception as exc:
                self._blocked(slot, exc)
                first_error = first_error or exc
            finally:
                self._release(slot)

        busy_slots = [slot for slot in slots if all(slot is not item for item in reserved)]
        for slot in busy_slots:
            with self._condition:
                if self._slots.get(slot.id) is not slot or slot.id not in self._pending_stop:
                    continue
                while slot.id in self._busy:
                    self._condition.wait()
                self._pending_stop.discard(slot.id)
                self._busy.add(slot.id)
            try:
                if slot.state == "blocked":
                    first_error = first_error or RuntimeError(slot.stop_error or f"plugin stop blocked: {slot.id}")
                    continue
                self._stop(slot, reason)
                with self._lock:
                    if not slot.teardown_requested:
                        slot.state = "ready"
                        slot.stop_requested = False
                        slot.stop_error = None
                        slot.actor.set_lifecycle_ready(self._runtime_slot_allowed(slot))
            except Exception as exc:
                self._blocked(slot, exc)
                first_error = first_error or exc
            finally:
                self._release(slot)
        with self._condition:
            self._runtime_stopping = False
            self._condition.notify_all()
        if first_error is not None:
            raise first_error
