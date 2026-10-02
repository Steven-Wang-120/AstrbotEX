"""Deterministic EX-only public delivery barriers; no Host or hardware."""
from __future__ import annotations

import asyncio
import threading
import time

from tests.wiring_fixture import WiringFixture
from tests.test_decision_service import wait_for


class PublicDeliveryPermitTests(WiringFixture):
    def _blocked_subscriber_safety(self, delivery):
        self.execute()
        self.bind()
        self.assertTrue(self.submit()["ok"])
        self.assertTrue(self.owners["arm"].started.wait(2), self.diagnostics())
        turn = self.bind()
        core = self.server.interaction_core
        entered, release = threading.Event(), threading.Event()
        returned, errors, elapsed = {}, [], {}
        topic = ("interaction_core.message.outgoing" if delivery == "text"
                 else "interaction_core.audio.play")

        def subscriber(message):
            entered.set()
            release.wait(5)

        class OfflineProvider:
            async def get_audio(self, value):
                return "offline-no-device.wav"

        core.tts_provider = OfflineProvider()
        unsubscribe = core.topic_bus.subscribe(topic, subscriber)
        voice_unsubscribe = core.topic_bus.subscribe("test.voice_activity", core._handle_voice_activity)
        threads = []
        try:
            self.assertTrue(self.request("interaction.reply", self.public(turn, delivery=delivery))["ok"])
            self.assertTrue(entered.wait(1), "real synchronous subscriber was not entered")

            def timed(name, operation):
                start = time.perf_counter()
                try:
                    operation()
                except Exception as exc:
                    errors.append(exc)
                finally:
                    elapsed[name] = time.perf_counter() - start
                    returned[name].set()

            def voice():
                if delivery == "text":
                    core.topic_bus.publish_payload("test.voice_activity", timestamp=time.time(),
                        source="offline_mic", payload={"state": "start", "utterance_id": "voice"})
                else:
                    # Capture suppression applies during playback. Exercise the
                    # real voice-stop bus/Controller invalidator independently.
                    core.topic_bus.publish_payload("interaction_core.audio.stop", timestamp=time.time(),
                        source="offline_mic", payload={"reason": "user_speech"})

            for name, operation in [("stop", lambda: self.s.request_stop("subscriber_test")),
                                    ("voice", voice)]:
                returned[name] = threading.Event()
                thread = threading.Thread(target=timed, args=(name, operation), daemon=True)
                threads.append(thread)
                thread.start()
            for name in ("stop", "voice"):
                self.assertTrue(returned[name].wait(0.1), name + " blocked on synchronous subscriber")
                self.assertLess(elapsed[name], 0.1)
            self.assertFalse(release.is_set())
            self.assertEqual(errors, [])
            self.assertFalse(self.server.action_dispatcher._gate)
            self.assertIsNone(self.c.public_token("trusted", self.public(turn)))
            print("blocked-subscriber", delivery, {k: round(v * 1000, 3) for k, v in elapsed.items()})
        finally:
            release.set()
            for thread in threads:
                thread.join(2)
            unsubscribe()
            voice_unsubscribe()
        self.assertTrue(wait_for(lambda: self.server.task_public_delivery._queue.unfinished_tasks == 0))

    def test_text_subscriber_block_does_not_hold_stop_or_voice_locks(self):
        self._blocked_subscriber_safety("text")

    def test_audio_subscriber_block_does_not_hold_stop_or_voice_locks(self):
        self._blocked_subscriber_safety("tts")

    def test_late_provider_old_generation_and_queued_text_have_zero_output(self):
        turn = self.bind()
        core = self.server.interaction_core
        audio, text = [], []
        core.topic_bus.subscribe("interaction_core.audio.play", lambda message: audio.append(message.payload))
        core.topic_bus.subscribe("interaction_core.message.outgoing", lambda message: text.append(message.payload))
        voice_unsubscribe = core.topic_bus.subscribe("test.voice_activity", core._handle_voice_activity)
        entered, release = threading.Event(), threading.Event()

        class BlockedProvider:
            async def get_audio(self, value):
                entered.set()
                await asyncio.to_thread(release.wait, 5)
                return "offline-no-device.wav"

        core.tts_provider = BlockedProvider()
        try:
            self.assertTrue(self.request("interaction.reply", self.public(turn, "late-tts", "tts"))["ok"])
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.request("interaction.reply", self.public(turn, "old-queued-text"))["ok"])
            generation = core._generation
            core.topic_bus.publish_payload("test.voice_activity", timestamp=time.time(),
                source="offline_mic", payload={"state": "start", "utterance_id": "new-voice"})
            self.assertGreater(core._generation, generation)
            self.assertIsNone(self.c.public_token("trusted", self.public(turn)))
            self.bind(2, turn_id="new")
        finally:
            release.set()
            voice_unsubscribe()
        self.assertTrue(wait_for(lambda: self.server.task_public_delivery._queue.unfinished_tasks == 0))
        self.assertEqual(audio, [])
        self.assertEqual(text, [])
        self.assertEqual(self.c._delivery_permits, set())

    def test_commit_returns_one_per_message_id_without_publishing(self):
        turn = self.bind()
        core = self.server.interaction_core
        outputs = []
        core.topic_bus.subscribe("interaction_core.message.outgoing", lambda message: outputs.append(message.payload))
        payload = self.public(turn, "single-permit")
        token = self.c.public_token("trusted", payload)
        self.assertIsNone(self.c.commit_public("trusted", payload, None))
        entered, release = threading.Event(), threading.Event()
        results = []

        def commit():
            entered.set()
            release.wait(1)
            results.append(self.c.commit_public("trusted", payload, token))

        threads = [threading.Thread(target=commit) for _ in range(2)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(1))
        release.set()
        for thread in threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results.count(("trusted", "single-permit")), 1)
        self.assertEqual(results.count(None), 1)
        self.assertEqual(outputs, [])
        newer = self.bind(2, turn_id="new")
        newer_payload = self.public(newer, "single-permit")
        self.assertIsNone(self.c.commit_public("trusted", newer_payload,
            self.c.public_token("trusted", newer_payload)))
