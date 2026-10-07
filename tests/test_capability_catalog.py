from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from unittest.mock import patch

from astrbot_ex.core.actions.models import parse_action_manifest
from astrbot_ex.core.decision.catalog import CapabilityCatalog, CapabilityInput


def manifest(owner="owner"):
    return parse_action_manifest({"id": owner, "action_api_version": 2,
        "provides": ["action_owner"], "actions": [{"action_id": f"{owner}.check.v2",
        "description": "Check", "schema": {"type": "object", "properties": {}},
        "operations": ["start"]}]}, owner=owner)


def record(owner="owner", generation=1, enabled=True, text="hello", version="1.0.0"):
    return CapabilityInput(owner, generation, manifest(owner), {"settings": [1]},
                           {"status": "available", "reason": "", "text": text,
                            "content_hash": hashlib.sha256(text.encode()).hexdigest()}, enabled,
                           version)


class CapabilityCatalogTest(unittest.TestCase):
    def test_atomic_revisions_and_detached_copies(self):
        catalog = CapabilityCatalog()
        original = record()
        first = catalog.refresh([original])
        self.assertEqual(first.revision, 1)
        self.assertEqual(catalog.refresh([original]).revision, 1)
        first.entries[0]["config"]["settings"].append(2)
        first.entries[0]["manifest"]["actions"][0]["description"] = "changed"
        self.assertEqual(catalog.snapshot().entries[0]["config"], {"settings": [1]})
        self.assertEqual(catalog.snapshot().entries[0]["manifest"]["actions"][0]["description"], "Check")
        self.assertEqual(catalog.snapshot().entries[0]["version"], "1.0.0")
        original.config["settings"].append(3)
        self.assertEqual(catalog.refresh([original]).revision, 2)
        self.assertEqual(catalog.refresh([record(generation=2)]).revision, 3)
        self.assertEqual(catalog.refresh([record(generation=2, version="1.0.1")]).revision, 4)
        self.assertEqual(catalog.snapshot().entries[0]["version"], "1.0.1")
        with self.assertRaises(ValueError):
            catalog.refresh([record(), record()])
        self.assertEqual(catalog.snapshot().revision, 4)
        self.assertEqual(catalog.refresh([]).revision, 5)

    def test_unavailable_and_disabled_are_explicit(self):
        catalog = CapabilityCatalog()
        unavailable = record("missing")
        unavailable.guide["status"] = "unavailable"
        unavailable.guide["text"] = ""
        unavailable.guide["content_hash"] = ""
        catalog.refresh([record("off", enabled=False), unavailable])
        self.assertEqual(catalog.snapshot().executable(), ())
        self.assertEqual({e["unavailable_reason"] for e in catalog.snapshot().entries},
                         {"disabled", "unavailable"})
        with self.assertRaises(ValueError):
            catalog.refresh([record("valid"), CapabilityInput("bad", True, manifest("bad"), {},
                {"status": "available"}, True, "1.0.0")])
        self.assertEqual(len(catalog.snapshot().entries), 2)

    def test_version_enabled_and_external_mutation(self):
        catalog = CapabilityCatalog()
        source = record()
        first = catalog.refresh([source])
        self.assertEqual(len(first.executable()), 1)
        first.executable()[0]["guide"]["text"] = "tampered"
        first.to_dict()["entries"][0]["enabled"] = False
        self.assertTrue(catalog.snapshot().entries[0]["enabled"])
        self.assertEqual(catalog.snapshot().entries[0]["guide"]["text"], "hello")
        self.assertEqual(catalog.refresh([record(enabled=False)]).revision, 2)
        self.assertEqual(catalog.snapshot().executable(), ())
        self.assertEqual(catalog.snapshot().entries[0]["unavailable_reason"], "disabled")
        self.assertEqual(catalog.refresh([record(enabled=True, version="2.0.0")]).revision, 3)
        self.assertEqual(catalog.snapshot().entries[0]["version"], "2.0.0")
        for bad in ("", "   ", 2):
            with self.assertRaises(ValueError):
                catalog.refresh([record(version=bad)])
        self.assertEqual(catalog.snapshot().revision, 3)

    def test_ready_state_directory_and_guide_gate_executable_entries(self):
        catalog = CapabilityCatalog()
        source = record()
        for state in ("loading", "starting", "stopping", "blocked", "unloaded"):
            with self.subTest(state=state):
                entry = catalog.refresh([replace(source, state=state)]).entries[0]
                self.assertFalse(entry["available"])
                self.assertEqual(entry["unavailable_reason"], state)
                self.assertEqual(catalog.snapshot().executable(), ())
        for status in ("version_changed", "manifest_changed", "config_changed", "directory_unavailable"):
            with self.subTest(status=status):
                entry = catalog.refresh([replace(source, directory_status=status)]).entries[0]
                self.assertFalse(entry["available"])
                self.assertEqual(entry["unavailable_reason"], status)
        rejected = {"status": "rejected", "reason": "invalid", "text": "", "content_hash": ""}
        self.assertEqual(catalog.refresh([replace(source, guide=rejected)]).executable(), ())
        entry = catalog.refresh([replace(source, generation=2)]).executable()[0]
        self.assertEqual((entry["owner"], entry["generation"]), ("owner", 2))
        with self.assertRaisesRegex(ValueError, "owner_mismatch"):
            catalog.refresh([replace(source, manifest=manifest("other"))])
        self.assertEqual(catalog.snapshot().entries[0]["generation"], 2)

    def test_owner_action_schema_and_resources_remain_distinct(self):
        catalog = CapabilityCatalog()
        records = []
        for owner, field in (("arm", "meters"), ("base", "speed")):
            declared = manifest(owner).to_dict()
            declared["actions"][0]["schema"] = {"type": "object", "properties": {
                field: {"type": "integer", "minimum": 0}}, "required": [field],
                "additionalProperties": False}
            declared["actions"][0]["resources"] = [f"{owner}.motor"]
            records.append(replace(record(owner), manifest=parse_action_manifest(declared, owner=owner)))
        entries = catalog.refresh(records).executable()
        for entry, field in zip(entries, ("meters", "speed")):
            action = entry["manifest"]["actions"][0]
            self.assertEqual(action["action_id"], f'{entry["owner"]}.check.v2')
            self.assertEqual(action["resources"], [f'{entry["owner"]}.motor'])
            self.assertEqual(action["schema"]["required"], [field])
            self.assertFalse(action["schema"]["additionalProperties"])

    def test_duplicate_action_ids_are_globally_rejected_atomically(self):
        catalog = CapabilityCatalog()
        original = catalog.refresh([record()])
        shared = [record("arm"), record("base")]
        for source in shared:
            source.manifest.actions[0].action_id = "shared.check.v2"
            # The SDK already rejects a foreign prefix. Exercise the catalog's
            # global defense independently of that earlier validation boundary.
            with self.assertRaisesRegex(ValueError, "owner_mismatch"):
                parse_action_manifest(source.manifest.to_dict(), owner=source.owner)
        for enabled in (True, False):
            with self.subTest(enabled=enabled), patch(
                    "astrbot_ex.core.decision.catalog.parse_action_manifest",
                    side_effect=lambda data, owner: next(r.manifest for r in shared if r.owner == owner)):
                with self.assertRaisesRegex(ValueError, "duplicate catalog action_id"):
                    catalog.refresh([shared[0], replace(shared[1], enabled=enabled)])
                self.assertEqual(catalog.snapshot(), original)
        duplicate = manifest().to_dict()
        duplicate["actions"].append(duplicate["actions"][0].copy())
        with self.assertRaises(ValueError):
            parse_action_manifest(duplicate, owner="owner")
        # Defense in depth: even a parser-bypassing same-owner manifest is rejected.
        invalid = manifest()
        invalid.actions.append(invalid.actions[0])
        with patch("astrbot_ex.core.decision.catalog.parse_action_manifest", return_value=invalid):
            with self.assertRaisesRegex(ValueError, "duplicate catalog action_id"):
                catalog.refresh([replace(record(), manifest=invalid)])
        self.assertEqual(catalog.snapshot(), original)


if __name__ == "__main__":
    unittest.main()
