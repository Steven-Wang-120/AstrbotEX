import copy
import threading
import unittest
from unittest.mock import patch

from astrbot_ex.core.decision.observations import ObservationStore
from astrbot_ex.core.topic_bus import TopicBus
from tests.test_goal_manager import make_catalog
from astrbot_ex.core.decision.catalog import CapabilityInput
from astrbot_ex.core.actions.models import parse_action_manifest


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = 1_000_000_000, 100.0
        self.bus = TopicBus()
        self.store = ObservationStore(self.bus, clock_ns=lambda: self.now, wall_clock=lambda: self.wall)
        self.store.configure(make_catalog(observe=True).snapshot().entries)
        self.addCleanup(self.store.close)

    def ingest(self, seq=1, epoch="a", timestamp=100.0, data=None):
        return self.store.ingest("arm_pose", data or {"position": 1}, source_epoch=epoch,
                                 seq=seq, source_timestamp=timestamp)

    def frame(self):
        return self.store.snapshot(["arm_pose"])[0]

    def test_original_json_copy_and_no_cross_topic_atomicity_claim(self):
        data = {"position": {"x": [1, 2]}, "extra": "original"}
        self.ingest(data=data)
        data["position"]["x"].append(3)
        frame = self.frame()
        self.assertEqual(frame["data"]["position"]["x"], [1, 2])
        frame["data"]["position"]["x"].append(4)
        self.assertEqual(self.frame()["data"]["position"]["x"], [1, 2])

    def test_G05_missing_future_expired_time_and_receive_clock_regression(self):
        for seq, stamp, reason in [(1, None, "missing_source_time"), (2, 101., "future_source_time"),
                                  (3, 99., "source_time_expired")]:
            self.ingest(seq, timestamp=stamp)
            self.assertEqual(self.frame()["health"]["reason_code"], reason)
        self.ingest(4)
        self.now += 81_000_000
        self.assertEqual(self.frame()["health"]["status"], "stale")
        self.now -= 82_000_000
        self.assertEqual(self.frame()["health"]["reason_code"], "future_receive_time")

    def test_G05_old_frame_missing_frame_epoch_restart_and_no_rollback(self):
        self.assertEqual(self.store.snapshot(["arm_pose"]), [])
        self.ingest(50)
        original = self.frame()["observation_id"]
        self.assertFalse(self.ingest(49))
        self.assertEqual(self.frame()["observation_id"], original)
        self.assertTrue(self.ingest(1, "b"))
        self.assertEqual(self.store.status()["sources"]["arm_pose"]["reason_code"], "source_restarted")
        self.assertFalse(self.ingest(51, "a"))
        self.assertEqual(self.frame()["source_epoch"], "b")

    def test_topic_bus_keeps_raw_b01_payload_and_generation_epoch(self):
        payload = {"position": 1, "objects": [{"id": "unchanged"}]}
        self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=1, payload=payload)
        self.assertEqual(self.frame()["data"], payload)
        self.assertEqual(self.frame()["source_epoch"], "arm:1")

    def test_byte_budget_and_missing_seq_are_rejected(self):
        with self.assertRaises(ValueError): self.ingest(data={"position": "x" * 70000})
        with self.assertRaises(ValueError): self.ingest(seq=None)
        self.assertEqual(self.store.snapshot(["arm_pose"]), [])

    def test_per_topic_age_and_missing_fields_filter_independently(self):
        catalog = make_catalog(("arm", "base"), observe=True)
        entries = catalog.snapshot().entries
        records = []
        for entry in entries:
            manifest = entry["manifest"]
            if entry["owner"] == "base":
                manifest["observation_sources"]["base_pose"]["max_age_ms"] = 200
            records.append(CapabilityInput(entry["owner"], 1, parse_action_manifest(manifest, owner=entry["owner"]),
                                          {}, entry["guide"], True, "1"))
        catalog.refresh(records)
        self.store.configure(catalog.snapshot().entries)
        self.ingest()
        self.store.ingest("base_pose", {"position": 2}, source_epoch="a", seq=1, source_timestamp=100.)
        self.now += 100_000_000
        observed = {o["source_id"]: o for o in self.store.snapshot(["arm_pose", "base_pose"])}
        self.assertEqual(observed["arm_pose"]["health"]["status"], "stale")
        self.assertEqual(observed["base_pose"]["health"]["status"], "ok")
        self.store.ingest("base_pose", {}, source_epoch="a", seq=2, source_timestamp=100.)
        self.assertEqual(self.store.relevant({"requires_observations": ["base_pose"]}, {})[1], "missing_required_fields")

    def test_epoch_capacity_fail_closed_instead_of_forgetting_old_epoch(self):
        self.store.max_epochs = 1
        self.ingest(epoch="a")
        self.ingest(epoch="b")
        self.assertFalse(self.ingest(epoch="c"))
        self.assertEqual(self.store.snapshot(["arm_pose"]), [])
        self.assertFalse(self.ingest(epoch="a"))

    def test_review_request_observation_requires_original_spec_hash_and_age(self):
        import copy
        self.ingest(timestamp=99.98)
        old = self.frame()
        self.now += 61_000_000
        self.ingest(2)
        with self.assertRaisesRegex(RuntimeError, "request_observation_expired"):
            self.store.request_observations([old])
        latest = self.frame()
        invalid = {**latest, "received_monotonic_ns": self.now + 1}
        with self.assertRaisesRegex(RuntimeError, "request_observation_future_receive_time"):
            self.store.request_observations([invalid])
        entries = list(make_catalog(observe=True).snapshot().entries)
        entries[0] = copy.deepcopy(entries[0])
        entries[0]["guide"]["content_hash"] = "changed"
        self.store.configure(entries)
        with self.assertRaisesRegex(RuntimeError, "request_observation_description_changed"):
            self.store.request_observations([latest])
        self.store.configure([])
        with self.assertRaisesRegex(RuntimeError, "request_observation_source_missing"):
            self.store.request_observations([latest])

    def test_review_sequence_high_water_survives_guide_and_disable_changes(self):
        import copy
        entries = list(make_catalog(observe=True).snapshot().entries)
        self.ingest(50)
        changed = copy.deepcopy(entries)
        changed[0]["guide"]["content_hash"] = "new-guide-hash"
        self.store.configure(changed)
        self.assertEqual(self.store.snapshot(["arm_pose"]), [])
        self.assertFalse(self.ingest(50))
        self.assertFalse(self.ingest(49))
        self.assertTrue(self.ingest(51))
        disabled = copy.deepcopy(changed)
        disabled[0]["enabled"] = False
        self.store.configure(disabled)
        self.assertEqual(self.store.snapshot(["arm_pose"]), [])
        self.store.configure(changed)
        self.assertFalse(self.ingest(51))
        self.assertTrue(self.ingest(1, epoch="new-boot"))
        self.assertFalse(self.ingest(52, epoch="a"))

    def test_review_canonical_target_ref_fixture_and_identity_mismatches(self):
        import copy
        import json
        from pathlib import Path
        payload = json.loads((Path(__file__).parent / "fixtures/decision/p-normal.json").read_text())["payload"]
        self.ingest(42, epoch="epoch-1", data=payload["observation"]["data"] | {"position": 1})
        ref = payload["target_ref"] | {"observation_id": self.frame()["observation_id"]}
        params = {"target_ref": ref, "frame_id": "front-42", "observation_seq": 42}
        action = {"requires_observations": ["arm_pose"]}
        self.assertEqual(self.store.relevant(action, params, dangerous=True)[1], "")
        for key in ("source_epoch", "stream_id", "track_session", "object_id", "observation_id"):
            invalid = copy.deepcopy(params)
            invalid["target_ref"][key] = "wrong"
            self.assertNotEqual(self.store.relevant(action, invalid)[1], "", key)
        for key, value in (("frame_id", "front-41"), ("observation_seq", 41)):
            invalid = copy.deepcopy(params)
            invalid[key] = value
            self.assertNotEqual(self.store.relevant(action, invalid)[1], "", key)
        self.assertEqual(self.store.relevant(action, {**params, "target": ref})[1], "target_binding_ambiguous")
        self.assertEqual(self.store.relevant(action, {"target_ref": {}}, dangerous=True)[1], "target_binding_incomplete")
        self.assertEqual(self.store.relevant(action, {}, dangerous=True)[1], "target_binding_required")
        data = copy.deepcopy(payload["observation"]["data"])
        data["objects"].append(copy.deepcopy(data["objects"][0]))
        self.ingest(43, epoch="epoch-1", data=data | {"position": 1})
        duplicate = {"target_ref": ref | {"observation_id": self.frame()["observation_id"]}}
        self.assertEqual(self.store.relevant(action, duplicate)[1], "target_object_ambiguous")
        data["objects"] = []
        self.ingest(44, epoch="epoch-1", data=data | {"position": 1})
        missing = {"target_ref": ref | {"observation_id": self.frame()["observation_id"]}}
        self.assertEqual(self.store.relevant(action, missing)[1], "target_object_missing")

    def test_unavailable_catalog_entries_never_subscribe_or_accept_frames(self):
        original = list(make_catalog(observe=True).snapshot().entries)
        self.ingest()
        old = self.frame()
        variants = [{"enabled": False}, {"state": "blocked", "available": False},
                    {"state": "starting", "available": False},
                    {"directory_status": "config_changed", "available": False},
                    {"available": False, "unavailable_reason": "unavailable"}]
        for variant in variants:
            with self.subTest(variant=variant):
                entries = copy.deepcopy(original)
                entries[0].update(variant)
                self.store.configure(entries)
                self.assertEqual(self.store.snapshot(["arm_pose"]), [])
                self.assertEqual(self.bus._subscribers.get("sensor.pose", {}), {})
                self.assertFalse(self.ingest(100, epoch="unavailable"))
                self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=100,
                                         payload={"position": "unavailable", "source_epoch": "unavailable"})
                self.assertEqual(self.store.snapshot(["arm_pose"]), [])
                with self.assertRaisesRegex(RuntimeError, "request_observation_source_missing"):
                    self.store.request_observations([old])
        self.store.configure(original)
        self.assertEqual(self.store.snapshot(["arm_pose"]), [])
        self.assertTrue(self.ingest(2))

    def test_retired_topic_callback_cannot_adopt_new_generation_or_epoch(self):
        old_callback = next(iter(self.bus._subscribers["sensor.pose"].values()))
        self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=40,
                                 payload={"position": "old"})
        old_request = self.frame()
        entries = copy.deepcopy(make_catalog(observe=True).snapshot().entries)
        entries[0]["generation"] = 2
        self.store.configure(entries)
        self.assertEqual(self.store.snapshot(["arm_pose"]), [])
        with self.assertRaisesRegex(RuntimeError, "request_observation_description_changed"):
            self.store.request_observations([old_request])
        self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=1,
                                 payload={"position": "new"})
        current = self.frame()
        self.assertEqual(current["source_epoch"], "arm:2")
        self.assertEqual(self.store.status()["sources"]["arm_pose"]["reason_code"], "source_restarted")
        from astrbot_ex.core.topic_bus import TopicMessage
        for data in ({"position": "late"}, {"position": "late", "source_epoch": "late-boot"},
                     {"position": "bad", "source_epoch": None, "not_json": object()}):
            old_callback(TopicMessage("sensor.pose", 100., "sensor", data, seq=999))
            self.assertEqual(self.frame(), current)
            self.assertEqual(self.store.status()["sources"]["arm_pose"]["reason_code"], "source_restarted")
        self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=2,
                                 payload={"position": "current"})
        self.assertEqual(self.frame()["data"], {"position": "current"})

    def test_retired_callback_after_identical_reenable_or_remove_cannot_repopulate_cache(self):
        from astrbot_ex.core.topic_bus import TopicMessage
        entries = make_catalog(observe=True).snapshot().entries
        for removed in ([], [{**entries[0], "enabled": False}]):
            with self.subTest(removed=bool(removed)):
                callback = next(iter(self.bus._subscribers["sensor.pose"].values()))
                self.store.configure(removed)
                self.assertEqual(self.bus._subscribers.get("sensor.pose", {}), {})
                self.store.configure(entries)
                callback(TopicMessage("sensor.pose", 100., "sensor", {"position": "late"}, seq=500))
                self.assertEqual(self.store.snapshot(["arm_pose"]), [])
                self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=1,
                                         payload={"position": "current", "source_epoch": f"boot-{bool(removed)}"})
                self.assertEqual(self.frame()["data"]["position"], "current")

    def test_callback_reconfigured_during_json_validation_cannot_commit(self):
        import astrbot_ex.core.decision.observations as module
        entered, release = threading.Event(), threading.Event()
        original = module.measure_json_budget
        errors = []
        def validate(data):
            if data.get("position") == "delayed":
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test callback was not released")
            return original(data)
        def publish():
            try:
                self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=99,
                                         payload={"position": "delayed", "source_epoch": "late-boot"})
            except Exception as exc:
                errors.append(exc)
        with patch.object(module, "measure_json_budget", side_effect=validate):
            worker = threading.Thread(target=publish)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                entries = copy.deepcopy(make_catalog(observe=True).snapshot().entries)
                entries[0]["generation"] = 2
                self.store.configure(entries)
                self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=1,
                                         payload={"position": "current"})
                current = self.frame()
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.frame(), current)

    def test_configure_noop_close_and_bus_latest_do_not_leak_or_replay(self):
        entries = make_catalog(observe=True).snapshot().entries
        callback = next(iter(self.bus._subscribers["sensor.pose"].values()))
        for _ in range(4):
            self.store.configure(entries)
        self.assertEqual(list(self.bus._subscribers["sensor.pose"].values()), [callback])
        self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=1,
                                 payload={"position": "before-disable"})
        old = self.frame()
        self.store.configure([])
        self.store.configure(entries)
        self.assertIsNotNone(self.bus.get_latest("sensor.pose"))
        self.assertEqual(self.store.snapshot(["arm_pose"]), [])
        self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=2,
                                 payload={"position": "after-enable"})
        self.assertEqual(len(self.bus._subscribers["sensor.pose"]), 1)
        self.store.close()
        self.store.close()
        self.store.configure(entries)
        self.assertEqual(self.bus._subscribers.get("sensor.pose", {}), {})
        self.assertEqual(self.store.snapshot(["arm_pose"]), [])
        with self.assertRaisesRegex(RuntimeError, "request_observation_source_missing"):
            self.store.request_observations([old])

    def test_configure_detaches_source_constraints_from_caller_mutation(self):
        entries = copy.deepcopy(make_catalog(observe=True).snapshot().entries)
        self.store.configure(entries)
        entries[0]["manifest"]["observation_sources"]["arm_pose"]["required_fields"].append("invented")
        self.assertTrue(self.ingest())
        self.assertEqual(self.frame()["health"]["status"], "ok")
        self.assertEqual(self.store.relevant({"requires_observations": ["arm_pose"]}, {})[1], "")

    def test_ambiguous_source_reconfigure_is_atomic_and_keeps_single_subscription(self):
        self.ingest()
        original = self.frame()
        entries = list(copy.deepcopy(make_catalog(observe=True).snapshot().entries))
        entries.append({**copy.deepcopy(entries[0]), "owner": "other"})
        with self.assertRaisesRegex(ValueError, "ambiguous observation source ID"):
            self.store.configure(entries)
        self.assertEqual(self.frame(), original)
        self.assertEqual(len(self.bus._subscribers["sensor.pose"]), 1)

    def test_invalid_callback_reconfigured_during_validation_cannot_change_diagnostics(self):
        import astrbot_ex.core.decision.observations as module
        entered, release = threading.Event(), threading.Event()
        original = module.measure_json_budget
        def validate(data):
            if data.get("position") == "delayed-invalid":
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test callback was not released")
                raise ValueError("synthetic invalid old JSON")
            return original(data)
        with patch.object(module, "measure_json_budget", side_effect=validate):
            worker = threading.Thread(target=lambda: self.bus.publish_payload(
                "sensor.pose", timestamp=100., source="sensor", seq=99,
                payload={"position": "delayed-invalid"}))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.store.configure([])
                self.store.configure(make_catalog(observe=True).snapshot().entries)
                self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=1,
                                         payload={"position": "current"})
                original_frame, original_status = self.frame(), self.store.status()
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.frame(), original_frame)
        self.assertEqual(self.store.status(), original_status)

    def test_topic_source_and_invalid_json_do_not_replace_current_frame(self):
        from astrbot_ex.core.topic_bus import TopicMessage
        self.bus.publish_payload("sensor.pose", timestamp=100., source="sensor", seq=1,
                                 payload={"position": "original", "source_epoch": "boot"})
        original = self.frame()
        for source, data, reason in (("intruder", {"position": "wrong"}, "source_mismatch"),
                                     ("sensor", {"position": float("nan")}, "invalid_payload"),
                                     ("sensor", {"position": "bad", "source_epoch": ""}, "invalid_payload")):
            self.bus.publish("sensor.pose", TopicMessage("sensor.pose", 100., source, data, seq=2))
            self.assertEqual(self.frame(), original)
            self.assertEqual(self.store.status()["sources"]["arm_pose"]["reason_code"], reason)
        self.bus.publish_payload("undeclared.pose", timestamp=100., source="undeclared", seq=100,
                                 payload={"position": "not-declared"})
        self.assertEqual(self.frame(), original)
        with self.assertRaisesRegex(ValueError, "source is not declared"):
            self.store.ingest("not-declared", {}, source_epoch="boot", seq=1, source_timestamp=100.)

    def test_topic_sequence_epoch_and_manifest_ttl_are_not_refreshed_by_replay(self):
        def publish(seq, epoch, stamp=100., position="current"):
            self.bus.publish_payload("sensor.pose", timestamp=stamp, source="sensor", seq=seq,
                                     payload={"position": position, "source_epoch": epoch})
        publish(50, "a")
        original = self.frame()
        publish(50, "a", position="duplicate")
        publish(49, "a", position="older")
        self.assertEqual(self.frame(), original)
        publish(1, "b")
        current = self.frame()
        publish(51, "a", position="retired")
        self.assertEqual(self.frame(), current)
        publish(2, "b", stamp=101.)
        self.assertEqual(self.frame()["health"]["reason_code"], "future_source_time")
        publish(3, "b", stamp=99.)
        self.assertEqual(self.frame()["health"]["reason_code"], "source_time_expired")
        publish(4, "b")
        self.now += 80_000_000
        self.assertEqual(self.frame()["health"]["status"], "ok")
        publish(4, "b", position="replay")
        self.now += 1_000_000
        self.assertEqual(self.frame()["health"]["reason_code"], "observation_expired")

    def test_target_binds_observation_frame_session_object(self):
        self.ingest(data={"position": 1, "frame_id": 4, "object_id": "cup"})
        item = self.frame()
        action = {"requires_observations": ["arm_pose"]}
        params = {"target": {"observation_id": item["observation_id"], "source_epoch": "a",
                             "frame_id": 4, "object_id": "cup"}}
        self.assertEqual(self.store.relevant(action, params, dangerous=True)[1], "")
        params["target"]["object_id"] = "other"
        self.assertEqual(self.store.relevant(action, params, dangerous=True)[1], "target_object_id_changed")
        self.ingest(2)
        self.assertEqual(self.store.relevant(action, params, dangerous=True)[1], "target_observation_changed")
