from __future__ import annotations

import unittest
from concurrent.futures import Future

from astrbot_ex.core.actions.ledger import OwnerBinding, StopEvidence
from astrbot_ex.core.actions.plugin_api import PluginActionAPI


class PluginActionAPITest(unittest.TestCase):
    def test_closed_bind_once_and_revoke(self):
        api = PluginActionAPI()
        with self.assertRaises(RuntimeError):
            api.report("cmd", "running")
        calls = []

        def callback(command_id, binding, status, **kwargs):
            calls.append((command_id, binding, status, kwargs))
            future = Future()
            future.set_result(status)
            return future

        api._bind(OwnerBinding("owner", 3), callback)
        with self.assertRaises(RuntimeError):
            api._bind(OwnerBinding("other", 4), callback)
        details = {"nested": [1]}
        self.assertEqual(api.report("cmd", "running", details=details).result(), "running")
        details["nested"].append(2)
        self.assertEqual(calls[0][1], OwnerBinding("owner", 3))
        self.assertEqual(calls[0][3]["details"], {"nested": [1]})
        with self.assertRaises(TypeError):
            api.report("cmd", "running", owner="other")
        with self.assertRaises(TypeError):
            api.report("cmd", "running", generation=4)
        with self.assertRaisesRegex(RuntimeError, "cannot be rebound"):
            api._bind(OwnerBinding("owner", 4), callback)
        self.assertEqual(len(calls), 1)
        api._revoke()
        with self.assertRaises(RuntimeError):
            api.report("cmd", "canceled", stop_evidence=StopEvidence("cmd", True, "sim", "ref"))

    def test_dispatcher_callback_rejects_stale_generation(self):
        current_generation = 4

        def dispatcher(command_id, binding, status, **kwargs):
            if binding != OwnerBinding("owner", current_generation):
                raise RuntimeError("stale generation")
            future = Future()
            future.set_result((command_id, status))
            return future

        retained = PluginActionAPI()
        retained._bind(OwnerBinding("owner", 3), dispatcher)
        with self.assertRaisesRegex(RuntimeError, "stale generation"):
            retained.report("cmd", "running")
        fresh = PluginActionAPI()
        fresh._bind(OwnerBinding("owner", 4), dispatcher)
        self.assertEqual(fresh.report("cmd", "running").result(), ("cmd", "running"))

    def test_evidence_and_payload_cannot_be_shortcuts(self):
        api = PluginActionAPI()
        calls = []
        api._bind(OwnerBinding("owner", 7), lambda *args, **kwargs: calls.append(args))
        for evidence in (True, "stopped", StopEvidence("cmd", False, "sim", "ref"),
                         StopEvidence("other", True, "sim", "ref")):
            with self.assertRaises(ValueError):
                api.report("cmd", "canceled", stop_evidence=evidence)
        with self.assertRaises(ValueError):
            api.report("cmd", "running", details={"stop_evidence": True})
        with self.assertRaisesRegex(TypeError, "must return Future"):
            api.report("cmd", "running")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
