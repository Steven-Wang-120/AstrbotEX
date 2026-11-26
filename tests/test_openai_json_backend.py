import base64
import hashlib
import json
import threading
import time
import unittest
import http.client
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from astrbot_ex.core.decision.backends.openai_json import BindingError, OpenAIJsonBackend, OpenAIJsonConfig
from astrbot_ex.core.decision.config import DecisionConfigStore
from tempfile import TemporaryDirectory

from tests.test_decision_management_http import ManagementHTTPFixture


class Provider:
    catalog = {"actions": [{"joint": "sample", "action_id": "sample.move"}]}
    def structured_schema(self, *, require_vision=False):
        return {"type": "object", "properties": {"joint": {"enum": ["sample"]}}}
    def bind_structured(self, action, *, require_vision=False):
        if action["joint"] != "sample" or type(action["params"].get("target")) is not int:
            raise ValueError("invalid_action")
        if require_vision and "vision_check" not in action:
            raise ValueError("vision_required")
        return dict(state="ready", **action)


class ChatHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *args):
        pass
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append((self.path, self.client_address, payload))
        if hasattr(self.server, "expected_auth"):
            self.server.auth_match = self.headers.get("Authorization") == self.server.expected_auth
        body = self.server.content
        if not isinstance(body, str):
            body = json.dumps(body)
        message = dict(role="assistant", content=body)
        if self.server.refuse:
            message['refusal'] = "no"
        raw = json.dumps(dict(model=payload['model'], choices=[dict(finish_reason="stop", message=message)])).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class OpenAIJsonTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ChatHandler)
        self.server.requests = []
        self.server.refuse = False
        self.action = {"joint":"sample","action_id":"sample.move","params":{"mode":"absolute","target":45}}
        self.server.content = {"action": self.action}
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval":.01})
        self.worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.worker.join, 2)
        self.addCleanup(self.server.shutdown)

    def make(self, **config):
        b = OpenAIJsonBackend(OpenAIJsonConfig(base_url="http://127.0.0.1:%d/v1" % self.server.server_port,
                    model="fixture-vl", mode="execute", allow_live_http=True, **config))
        self.addCleanup(b.close)
        return b

    def test_one_post_json_schema_and_reuse_no_secret_or_photo_for_text(self):
        b = self.make()
        for i in range(2):
            v = b.bind_primitive("目标四十五度", [], {"available":True}, b"extra", provider=Provider())
            self.assertEqual(v["params"]["target"],45)
            self.assertEqual(v["timings"]["connection_reused"], i == 1)
            if i: self.assertEqual(v["timings"]["connect_tls_ms"], 0)
        requests = self.server.requests
        self.assertEqual(len(requests),2)
        self.assertEqual(requests[0][1], requests[1][1])
        self.assertEqual(requests[0][0], '/v1/chat/completions')
        payload=requests[0][2]
        self.assertEqual(payload["response_format"]["type"],"json_schema")
        self.assertEqual(len(payload['messages'][1]['content']),1)

    def test_vague_or_missing_magnitudes_rejected_even_if_model_guesses(self):
        b=self.make(response_format="json_object")
        for text in ("稍微打开一点", "开一点", "open a little", "移到 4.5 度", "移到 450 度", "开到三十度"):
            with self.subTest(text=text):
                result=b.bind_primitive(text, [], {}, provider=Provider())
                self.assertEqual(result["state"],"needs_input")
        self.action["params"].update(duration_ms=45)
        self.assertEqual(b.bind_primitive("移到45度", [], {}, provider=Provider())["state"],"needs_input")
        self.assertEqual(self.server.requests[-1][2]["response_format"],{"type":"json_object"})
        self.server.content={"action": None}
        self.assertEqual(b.bind_primitive("开45度", [], {}, provider=Provider())["state"],"needs_input")

    def test_model_cannot_swap_target_and_duration_or_change_a_written_sign(self):
        backend = self.make()
        provider = Provider()
        provider.bind_structured = lambda action, **_: dict(state="ready", **action)
        instructions = ("底座移到650原始刻度，耗时120毫秒。",
                        "Use 120 milliseconds to move the base to 650 raw.",
                        "耗时一百二十毫秒，底座移到六百五十刻度。")
        action = {"joint": "sample", "action_id": "sample.move",
                  "params": {"mode": "absolute", "target_raw": 650, "duration_ms": 120}}
        for text in instructions:
            with self.subTest(text=text):
                self.server.content = {"action": action}
                self.assertEqual(backend.bind_primitive(text, [], {}, provider=provider)["state"], "ready")
                self.server.content = {"action": dict(action, params={
                    "mode": "absolute", "target_raw": 120, "duration_ms": 650})}
                self.assertEqual(backend.bind_primitive(text, [], {}, provider=provider)["state"], "needs_input")
        for text in ("移动650刻度，另有编号120", "目标650，耗时120毫秒", "移动650度，耗时120毫秒",
                     "移动6.5e2刻度，耗时120毫秒", "移动650刻度或600刻度，耗时120毫秒"):
            with self.subTest(text=text):
                self.server.content = {"action": action}
                self.assertEqual(backend.bind_primitive(text, [], {}, provider=provider)["state"], "needs_input")
        self.server.content = {"action": dict(action, params={"mode":"delta","delta_raw":-8,"duration_ms":120})}
        self.assertEqual(backend.bind_primitive("相对负8刻度，耗时120毫秒", [], {}, provider=provider)["state"], "ready")
        self.assertEqual(backend.bind_primitive("相对正8刻度，耗时120毫秒", [], {}, provider=provider)["state"], "needs_input")

    def test_probe_with_missing_bearer_does_not_claim_an_inference(self):
        binding = self.make(auth_mode="bearer")
        result = binding.probe()
        self.assertEqual(result["error_code"], "missing_or_invalid_secret")
        self.assertFalse(result["network_called"])
        self.assertFalse(result["inference_called"])
        self.assertFalse(result["inference_ok"])
        self.assertEqual(self.server.requests, [])

    def test_invalid_and_refused_model_output(self):
        b=self.make()
        for body in ('{"action":null,"action":null}', '{"action":NaN}', '{"action":null,"extra":1}', '[]',
                     '{"action":{"joint":"bad","params":{"target":45}}}'):
            with self.subTest(body=body):
                self.server.content=body
                with self.assertRaises((BindingError,ValueError)):b.bind_primitive('45',[],{},provider=Provider())
        self.server.content = {"action": dict(self.action, vision_check={"jaw_posture": "closed"})}
        with self.assertRaisesRegex(BindingError, "vision_without_image"):
            b.bind_primitive("45", [], {}, provider=Provider())
        self.server.refuse=True
        with self.assertRaisesRegex(BindingError,'model_refusal'):
            b.bind_primitive('45',[],{},provider=Provider())

    def test_only_the_fresh_selected_raw_jpeg_can_be_sent(self):
        b=self.make(require_image=True)
        jpeg=b'\xff\xd8fixture\xff\xd9'
        meta = dict(available=True, source="raw_camera", observation_id="cam/1", source_epoch="epoch",
                    source_stamp_ns=time.time_ns(), received_monotonic_ns=time.monotonic_ns(), sha256=hashlib.sha256(jpeg).hexdigest())
        meta.update(stream_id="camera", frame_id="rgb_optical")
        obs = [dict(observation_id="cam/1", source_epoch="epoch", health={"status": "ok"},
                    data={"annotated_label": "PRIVATE", "image": dict(meta)})]
        self.action['vision_check']={'some':'observation'}
        for override in [dict(archived=True),dict(received_monotonic_ns=time.monotonic_ns()-2_000_000_000),
                         dict(source_stamp_ns=time.time_ns()-2_000_000_000), dict(sha256='wrong'),dict(observation_id='other'),dict(stream_id='annotated')]:
            with self.assertRaises(BindingError):b.bind_primitive('45',obs,dict(meta,**override),jpeg,provider=Provider())
        self.assertEqual(self.server.requests,[])
        v=b.bind_primitive('45',obs,meta,jpeg,provider=Provider())
        self.assertEqual(v['image_observation_id'],'cam/1')
        messages=self.server.requests[0][2]['messages']
        url=messages[-1]['content'][-1]['image_url']['url']
        self.assertEqual(base64.b64decode(url.split(',')[1]),jpeg)
        self.assertNotIn('PRIVATE',json.dumps(messages))

    def test_siliconflow_preset_sends_exact_json_mode_request_with_separate_key(self):
        with TemporaryDirectory() as tmp:
            store = DecisionConfigStore(tmp, "session")
            preset = store.get()["presets"]["openai_json"]["siliconflow_qwen3_vl_8b"]
            self.assertEqual(preset, {
                "service_connection": {"base_url": "https://api.siliconflow.cn/v1",
                                       "model": "Qwen/Qwen3-VL-8B-Instruct", "auth_mode": "bearer"},
                "response_format": "json_object",
            })
            saved = store.get()["saved"]
            self.assertEqual(saved["backend"], "mock")
            self.assertFalse(saved["openai_json"]["allow_live_http"])
            self.assertIsNone(saved["openai_json_secret_ref"])
            saved["backend"] = "openai_json"
            saved["openai_json"].update(mode="execute", allow_live_http=True)
            store.save(saved, 0, "session")
            store.update_secret("set", "fixture-jev-only", 1, "session", provider="jev")
            ref = lambda: store.saved["openai_json_secret_ref"]
            client = OpenAIJsonBackend(store.backend_config("openai_json"),
                                       secret_provider=lambda: store.secrets.read(ref()))
            self.addCleanup(client.close)
            self.server.expected_auth = "Bearer fixture-siliconflow"
            with patch("astrbot_ex.core.decision.backends.connection.open_connection") as route:
                route.side_effect = lambda *_args, **_kw: http.client.HTTPConnection(
                    "127.0.0.1", self.server.server_port, timeout=2)
                self.assertEqual(client.probe()["error_code"], "missing_or_invalid_secret")
                self.assertEqual(client.transport_statistics()["request_attempts"], 0)
                route.assert_not_called()
                store.update_secret("set", "fixture-siliconflow", 2, "session", provider="openai_json")
                result = client.bind_primitive("45 degrees", [], {}, provider=Provider())
                self.assertEqual(result["state"], "ready")
                self.assertEqual(route.call_args.args[0], "https://api.siliconflow.cn/v1")
                path, _, request = self.server.requests[0]
                self.assertTrue(self.server.auth_match)
                self.assertEqual(path, "/v1/chat/completions")
                self.assertEqual(request["model"], "Qwen/Qwen3-VL-8B-Instruct")
                self.assertEqual(request["response_format"], {"type": "json_object"})
                self.assertIn("JSON Schema", request["messages"][0]["content"])
                for bad in ("not JSON", '{"action":null,"extra":1}'):
                    self.server.content = bad
                    with self.assertRaises(BindingError):
                        client.bind_primitive("45 degrees", [], {}, provider=Provider())
                self.server.refuse = True
                with self.assertRaisesRegex(BindingError, "model_refusal"):
                    client.bind_primitive("45 degrees", [], {}, provider=Provider())
                self.assertEqual(len(self.server.requests), 4)
                self.assertEqual(client.transport_statistics()["request_attempts"], 4)
                self.assertNotIn("fixture-siliconflow", json.dumps(store.get()))
                self.assertNotIn("fixture-jev-only", json.dumps(store.get()))

    def test_config_migrates_old_profile_and_keeps_credentials_separate(self):
        with TemporaryDirectory() as tmp:
            store=DecisionConfigStore(tmp,'session')
            legacy=store.defaults()
            for name in ('openai_json','openai_json_secret_ref'):legacy.pop(name)
            store.path.parent.mkdir(parents=True,exist_ok=True)
            store.path.write_text(json.dumps(dict(schema_version=2,revision=9,config=legacy)))
            store=DecisionConfigStore(tmp,'session')
            self.assertEqual(store.revision,9)
            cfg=store.get()['saved'];cfg['backend']='openai_json'
            cfg['openai_json']['service_connection'].update(base_url='http://100.114.26.5:18187/v1',model='test')
            store.save(cfg,9,'session')
            store.update_secret('set','private-example-key',10,'session',provider='openai_json')
            self.assertIsNone(store.saved['secret_ref'])
            self.assertEqual(store.backend_config('openai_json').model,'test')
            self.assertNotIn('private-example-key',store.path.read_text())
            self.assertNotIn('private-example-key',str(store.get()))
            changed=store.get()['saved']
            changed['openai_json']['service_connection']['base_url']='https://different.example/v1'
            store.save(changed,11,'session')
            self.assertIsNone(store.saved['openai_json_secret_ref'])
            with self.assertRaises(ValueError):OpenAIJsonConfig(base_url='http://public.example/v1')


class JsonManagementTests(ManagementHTTPFixture, unittest.TestCase):
    def test_openai_json_management_probe_real_loopback_without_motion(self):
        model = ThreadingHTTPServer(("127.0.0.1", 0), ChatHandler)
        model.content = {"status":"wait"}
        model.refuse, model.requests = False,[]
        worker = threading.Thread(target=model.serve_forever,kwargs={"poll_interval":.01})
        worker.start()
        self.addCleanup(model.server_close)
        self.addCleanup(worker.join,2)
        self.addCleanup(model.shutdown)
        config=self.get_config()["saved"]
        config["backend"]="openai_json"
        config["openai_json"]["service_connection"].update(
            base_url=f"http://127.0.0.1:{model.server_port}/v1",model="fixture-vl",auth_mode="none")
        code,_,_=self.write("/config",{"config":config})
        self.assertEqual(code,200)
        code,accepted,_=self.write("/test",{"provider":"openai_json"})
        self.assertEqual(code,202)
        operation=self.operation(accepted)
        self.assertEqual(operation["state"],"succeeded")
        self.assertTrue(operation["result"]["binding"]["current_config_verified"])
        self.assertTrue(operation["result"]["inference_called"])
        self.assertEqual(len(model.requests),1)
        self.assert_idle()

    def test_structured_probe_does_not_claim_network_inference(self):
        config=self.get_config()["saved"];config["backend"]="structured"
        self.assertEqual(self.write("/config",{"config":config})[0],200)
        code,accepted,_=self.write("/test",{"provider":"structured"})
        self.assertEqual(code,202)
        op=self.operation(accepted)
        self.assertTrue(op["result"]["ok"])
        self.assertTrue(op["result"]["binding"]["current_config_verified"])
        self.assertFalse(op["result"]["network_called"])
        self.assertFalse(op["result"]["inference_ok"])
        self.assert_idle()
