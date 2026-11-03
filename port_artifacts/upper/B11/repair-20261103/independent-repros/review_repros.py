"""Independent merge review. Sources are mounted read-only; all data is temporary."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = ["/aeb-src", "/ex-src"]

from astrbot_plugin_astrbotex_interaction.tests.test_upper_e2e import (
    CONTROL_PEER, EX_SESSION, HostConfig, UpperStackAcceptanceTests,
    plan_action, plan_speech,
)
from astrbot_plugin_astrbotex_interaction.tests.test_upper_composition import (
    RecordingContext, ex_event,
)


class ReviewChecks(UpperStackAcceptanceTests):
    async def test_management_api_with_defaults_off(self):
        await self.plugin.terminate()
        self.plugin.plugin_config["upper_enabled"] = False
        self.plugin.plugin_config["control_enabled"] = False
        self.plugin.upper_settings = self.plugin.upper_settings.from_config(
            self.plugin.plugin_config, environ={}
        )
        context = RecordingContext()
        self.plugin.context = context
        await self.plugin.initialize()
        print("DEFAULT_OFF", json.dumps({
            "registered_api_count": len(context.web_apis),
            "status": self.plugin.upper_status,
        }), flush=True)
        self.assertEqual(len(context.web_apis), 10,
                         "A02 requires all 10 management APIs before enabling control")

    async def test_saved_provider_ids_apply_to_next_input(self):
        observed = []
        classify = self.models.classify
        generate = self.models.generate

        async def capture_classify(text, snapshot, provider_id=""):
            observed.append(["classify", provider_id])
            return await classify(text, snapshot, provider_id)

        async def capture_generate(text, route, snapshot, provider_id=""):
            observed.append(["generate", provider_id])
            return await generate(text, route, snapshot, provider_id)

        self.models.classify = capture_classify
        self.models.generate = capture_generate
        repository = self.components.settings_repository
        saved = repository.save({"routing_provider_id": "new-router",
                                 "generation_provider_id": "new-generator"},
                                repository.load().revision)
        await self.deliver("review-provider-change", plan_speech("public reply"))
        print("NEXT_INPUT_PROVIDERS", json.dumps({
            "saved": saved.to_dict(), "actual_calls": observed,
        }), flush=True)
        self.assertEqual(observed, [["classify", "new-router"],
                                    ["generate", "new-generator"]])

    async def test_voice_uses_configured_control_peer(self):
        await self.attach_peer()
        text = "review-voice-action"
        self.models.script[text] = plan_action(["review-execution-segment"])
        event = ex_event(text, peer=b"independent-text-peer", adapter=self.plugin._adapter)
        event.session_id = EX_SESSION
        await self.plugin.take_over_upper_input(event)
        plans = self.components.store.list_plans()
        detail = self.components.store.get_plan(plans[0]["plan_id"])
        print("VOICE_CONTROL", json.dumps({
            "text_peer": "independent-text-peer", "control_peer": CONTROL_PEER,
            "control_injections": len(self.injects()), "plan": detail,
        }, default=str), flush=True)
        self.assertEqual(len(self.injects()), 1,
                         "the text reply peer must not override the independent control peer")

    async def test_real_aeb_to_real_ex_prefix_delivery(self):
        from tests.test_upper_control_e2e import UpperControlCrossEndTests

        ex_fixture = UpperControlCrossEndTests(
            "test_three_segments_reach_the_real_backend_request_in_order"
        )
        ex_fixture.setUp()
        self.addCleanup(ex_fixture.doCleanups)
        ex_fixture.manager.update("control", {"config": {
            "endpoint": f"tcp://127.0.0.1:{self.control_port}",
            "identity": CONTROL_PEER, "protocol_profile": "astrbotex",
            "channel": "control", "upper_robot_id": "robot-e2e",
            "upper_route_refs": "upper-control", "upper_ex_session": EX_SESSION,
        }})
        ex_fixture.manager.set_control_session_provider(lambda: EX_SESSION)
        ex_fixture.manager.start("control")
        for _ in range(300):
            if self.components.control.peer in self.components.control.online_peers():
                break
            await asyncio.sleep(0.01)
        self.assertIn(self.components.control.peer, self.components.control.online_peers())
        segments = ["review-prefix-0", "review-prefix-1", "review-pending-2"]
        await self.deliver("review-real-cross-end", plan_action(segments, send_count=2))
        plans = self.components.store.list_plans()
        plan = self.components.store.get_plan(plans[0]["plan_id"])
        print("REAL_CROSS_END", json.dumps({
            "backend_requests": len(ex_fixture.captured), "plan": plan,
            "requests": [json.loads(body) for body in ex_fixture.captured],
        }, default=str), flush=True)
        self.assertEqual(len(ex_fixture.captured), 1)
        body = json.loads(ex_fixture.captured[0])
        self.assertEqual([segment["text"] for segment in
                          body["state"]["instruction_input"]["segments"]], segments[:2])
        self.assertEqual(plan["segments"][2]["status"], "pending")


class RunnerChecks(unittest.TestCase):
    def test_failed_required_page_prevents_wiring_pass(self):
        path = Path("/aeb-src/astrbot_plugin_astrbotex_interaction/scripts/verify_upper_control.py")
        spec = importlib.util.spec_from_file_location("review_verify_upper", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as root:
            args = module.build_parser().parse_args([
                "--aeb-root", "/aeb-src", "--ex-root", "/ex-src",
                "--evidence-dir", str(Path(root) / "evidence"), "--scope", "all",
            ])
            runner = module.Runner(args)
            self.addCleanup(shutil.rmtree, runner.temp_root, ignore_errors=True)
            runner.stage("unit_aeb").status = module.PASS
            runner.stage("page_spec", gating=False).status = module.FAIL
            verdict = runner.verdict()
            print("FAILED_PAGE_VERDICT", verdict, flush=True)
            self.assertNotEqual(verdict, "wiring_pass",
                                "A02 page is mandatory in the task package")


if __name__ == "__main__":
    names = ["test_management_api_with_defaults_off",
             "test_saved_provider_ids_apply_to_next_input",
             "test_voice_uses_configured_control_peer",
             "test_real_aeb_to_real_ex_prefix_delivery"]
    suite = unittest.TestSuite([ReviewChecks(name) for name in names])
    suite.addTest(RunnerChecks("test_failed_required_page_prevents_wiring_pass"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    Path("/audit/review-repros-results.json").write_text(json.dumps({
        "tests": result.testsRun,
        "failures": [{"test": str(test), "traceback": trace}
                     for test, trace in result.failures],
        "errors": [{"test": str(test), "traceback": trace}
                   for test, trace in result.errors],
        "passed": result.testsRun - len(result.failures) - len(result.errors),
    }, indent=2), encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() else 1)
