"""One bounded public-delivery worker; ambiguous IO is never replayed."""
from __future__ import annotations

import asyncio
import copy
import queue
import threading


class TaskPublicDelivery:
    def __init__(self, controller, interaction, *, capacity=32):
        self.controller, self.interaction = controller, interaction
        self._queue = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="task-public-delivery", daemon=True)
        self._worker.start()

    def enqueue(self, connection_id, payload):
        token = self.controller.public_token(connection_id, payload)
        if token is None or self._stop.is_set():
            return {"ok": False, "error": "task_public_rejected"}
        try:
            self._queue.put_nowait((connection_id, copy.deepcopy(payload), token))
        except queue.Full:
            return {"ok": False, "error": "task_public_queue_full"}
        return {"ok": True, "queued": True}

    def _run(self):
        while not self._stop.is_set():
            try:
                connection_id, payload, token = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if token != self.controller.public_token(connection_id, payload):
                    continue
                if payload["delivery"] == "text":
                    if not self._stop.is_set():
                        permit = self.controller.commit_public(connection_id, payload, token)
                        if permit is not None:
                            self.interaction.publish_text(payload["text"], source="task_public")
                elif self.interaction.tts_provider is not None:
                    audio = asyncio.run(self.interaction.tts_provider.get_audio(payload["text"]))
                    # Recheck AFTER blocked/provider IO, immediately before audio publication.
                    if not self._stop.is_set():
                        permit = self.controller.commit_public(connection_id, payload, token)
                        if permit is not None:
                            self.interaction.publish_audio(audio, delete_after_play=True,
                                metadata={"task_id": payload["task_id"], "turn_id": payload["turn_id"],
                                          "generation": payload["generation"], "message_id": payload["message_id"]})
            except Exception:
                self.interaction.event_bus.emit("interaction", "task public delivery unavailable",
                                                severity="warning", code="task_public_delivery_unknown")
            finally:
                self._queue.task_done()

    def close(self, timeout=2):
        self._stop.set()
        self.controller.invalidate_local()
        self._worker.join(timeout)
        if self._worker.is_alive():
            raise TimeoutError("task public provider did not cooperate with shutdown")
