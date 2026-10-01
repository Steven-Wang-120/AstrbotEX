from __future__ import annotations

import hashlib
import unittest

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


if __name__ == "__main__":
    unittest.main()
