from __future__ import annotations
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from astrbot_ex.core.environments import plugin_api as plugin_api_module
from astrbot_ex.core.environments.manager import EnvironmentManager
from astrbot_ex.core.environments.plugin_api import PluginRosFacade, RosBindingError, RosUnavailableError
from astrbot_ex.core.environments.models import EnvironmentBusyError, EnvironmentRevisionConflict
from astrbot_ex.core.environments.contracts import parse_ports, normalize_bindings, environment_config
from astrbot_ex.core.environments.ros2.adapter import Ros2EnvironmentAdapter
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.topic_bus import TopicBus
from astrbot_ex.core.local_plugins import LocalPluginManager
from astrbot_ex.core.plugin_registry import PluginRegistry

PORTS = [
    {"id":"input","direction":"subscribe","message_types":["std_msgs/msg/String"],"default_topic":"/input","queue":{"capacity":2,"overflow":"keep_latest"}},
    {"id":"output","direction":"publish","message_types":["std_msgs/msg/String"],"default_topic":"/output","queue":{"capacity":2,"overflow":"reject_new","max_age_ms":50}},
    {"id":"control","direction":"publish","message_types":["std_msgs/msg/String"],"default_topic":"/control","execution_lane":"control"},
]
class Message:
    def __init__(self): self.data = ""

class FakeNative:
    def __init__(self, topic): self.topic_name=topic;self.published=[]
    def publish(self,message): self.published.append(message)

class FakeAdapter:
    def __init__(self): self.ports=[];self.closed=False
    def start(self): pass
    def attach(self,p,generation):
        if not p.binding["enabled"]: return
        p.native=FakeNative(p.binding["topic"]);p.message_type=Message;p.generation=generation;p.state="ready"
        self.ports.append(p)
    def detach(self,p):
        if p in self.ports: self.ports.remove(p)
        p.unavailable()
    def close(self,reason):
        for p in list(self.ports): self.detach(p)
        self.closed=True
    def status(self): return {"health":"ok","active":not self.closed}
    def graph(self): return {"nodes":[],"topics":[]}

def finish(manager,mode):
    result=manager.select(mode)
    worker=manager._worker
    if worker: worker.join(3)
    assert manager.snapshot()["phase"]=="idle", manager.snapshot()
    return result

class ControlledPortClock:
    def __init__(self): self.nanoseconds=time.monotonic_ns()
    def advance_ns(self,nanoseconds): self.nanoseconds+=nanoseconds
    def monotonic_ns(self): return self.nanoseconds
    def monotonic(self): return self.nanoseconds/1_000_000_000


class EnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.bus=TopicBus()
        self.m=EnvironmentManager(data_root=Path(self.temp.name),event_bus=EventBus(),topic_bus=self.bus,
            adapter_factory=lambda mode,config:FakeAdapter())
        self.f=PluginRosFacade(plugin_id="demo",topic_bus=self.bus,environment_manager=self.m,ports=PORTS,
            bindings={"output":{"enabled":True},"control":{"enabled":True}})
    def tearDown(self): self.m.close();self.temp.cleanup()

    def test_normal_is_waiting_and_does_not_turn_topicbus_into_ros(self):
        sub=self.f.subscribe("input");pub=self.f.publisher("output")
        self.bus.publish_payload("/input",timestamp=0,source="internal",payload={"data":"internal"})
        self.assertIsNone(sub.get_nowait())
        self.assertEqual(pub.publish(Message()).status,"unavailable")
        self.assertEqual(sub.status()["state"],"waiting_environment")
        with self.assertRaises(RosUnavailableError): pub.new_message()

    def test_handles_rebind_and_old_generation_is_discarded(self):
        sub=self.f.subscribe("input");pub=self.f.publisher("output")
        self.assertIs(self.f.subscribe("input"),sub)
        for _ in range(20):
            finish(self.m,"ros2")
            generation=sub.status()["environment_generation"]
            sub._port.receive(Message(),generation)
            pub.publish(Message())
            finish(self.m,"normal")
            self.assertIs(self.m.topic_bus,self.bus)
            self.assertIsNone(sub.get_nowait())
            self.assertEqual(pub.status()["queue_depth"],0)
            sub._port.receive(Message(),generation)
            self.assertIsNone(sub.get_nowait())
        finish(self.m,"ros2")
        self.assertTrue(pub.status()["resource_created"])

    def test_publish_is_queued_until_native_call_and_overflow_is_explicit(self):
        p=self.f.publisher("output");finish(self.m,"ros2")
        self.assertEqual(p.publish(Message()).status,"queued")
        self.assertEqual(p.publish(Message()).status,"queued")
        self.assertEqual(p.publish(Message()).status,"rejected_full")
        self.assertEqual(p.status()["tx_published"],0)
        p._port.flush_one()
        self.assertEqual(p.status()["tx_published"],1)
        self.assertEqual(p.publish({}).status,"invalid_type")

    def test_expiry_and_runtime_gate_drop_unsent_control(self):
        p=self.f.publisher("output");c=self.f.publisher("control");finish(self.m,"ros2")
        clock=ControlledPortClock()
        port_time=SimpleNamespace(monotonic_ns=clock.monotonic_ns,monotonic=clock.monotonic,time=time.time)
        with patch.object(plugin_api_module,"time",port_time):
            p.publish(Message());clock.advance_ns(51_000_000);p._port.flush_one()
            self.assertEqual(p.status()["tx_published"],0);self.assertEqual(p.status()["expired"],1)
            self.assertEqual(c.publish(Message()).status,"runtime_inactive")
            self.m.runtime_running=lambda:True
            self.assertEqual(c.publish(Message()).status,"queued")
            with self.assertRaises(EnvironmentBusyError): self.m.select("normal")
            self.m.runtime_running=lambda:False
            c._port.flush_one()
            self.assertEqual(c.status()["tx_published"],0)

    def test_output_queue_age_boundary(self):
        p=self.f.publisher("output");c=self.f.publisher("control");finish(self.m,"ros2")
        clock=ControlledPortClock()
        port_time=SimpleNamespace(monotonic_ns=clock.monotonic_ns,monotonic=clock.monotonic,time=time.time)
        with patch.object(plugin_api_module,"time",port_time):
            self.assertEqual(p.publish(Message()).status,"queued")
            clock.advance_ns(50_000_000)
            p._port.flush_one()
            self.assertEqual(p.status()["tx_published"],1)
            self.assertEqual(p.status()["expired"],0)
            self.assertEqual(p.publish(Message()).status,"queued")
            clock.advance_ns(50_000_001)
            p._port.flush_one()
            self.assertEqual(p.status()["tx_published"],1)
            self.assertEqual(p.status()["expired"],1)
            self.assertEqual(p.status()["queue_depth"],0)
            # The control port uses the default 200 ms age limit.
            self.assertEqual(c._port.declaration["queue"]["max_age_ms"],200)
            original_runtime_running=self.m.runtime_running
            self.m.runtime_running=lambda:True
            try:
                self.assertEqual(c.publish(Message()).status,"queued")
                clock.advance_ns(200_000_000)
                c._port.flush_one()
                self.assertEqual(c.status()["tx_published"],1)
                self.assertEqual(c.status()["expired"],0)
                self.assertEqual(c.publish(Message()).status,"queued")
                clock.advance_ns(200_000_001)
                c._port.flush_one()
                self.assertEqual(c.status()["tx_published"],1)
                self.assertEqual(c.status()["expired"],1)
                self.assertEqual(c.status()["queue_depth"],0)
            finally:
                self.m.runtime_running=original_runtime_running

    def test_subscriptions_are_per_owner_and_bounded(self):
        other=PluginRosFacade(plugin_id="other",environment_manager=self.m,ports=PORTS)
        a=self.f.subscribe("input");b=other.subscribe("input");finish(self.m,"ros2")
        for _ in range(1000): a._port.receive(Message(),a._port.generation)
        self.assertEqual(a.status()["queue_depth"],2)
        self.assertEqual(a.status()["queue_dropped"],998)
        self.assertIsNone(b.get_nowait())
        self.f.close()
        b._port.receive(Message(),b._port.generation)
        self.assertIsNotNone(b.get_nowait())
        self.assertEqual(len(self.m.endpoints()["received"]),1)

    def test_reconfiguration_only_rebuilds_changed_port(self):
        a=self.f.subscribe("input");b=self.f.publisher("output");finish(self.m,"ros2")
        old_b=b._port.native
        config=copy.deepcopy(self.f.bindings_config);config["input"]["topic"]="/changed"
        self.f.reconfigure(config)
        self.assertIs(b._port.native,old_b)
        self.assertEqual(a.status()["topic"],"/changed")
        self.assertEqual(len(self.m.endpoints()["received"]),1)

    def test_failed_start_does_not_persist_or_break_internal_bus(self):
        class Broken(FakeAdapter):
            def start(self): raise RuntimeError("ABI failure")
        self.m._adapter_factory=lambda mode,config:Broken()
        self.m.select("ros2");self.m._worker.join(2)
        self.assertEqual(self.m.snapshot()["active_mode"],"normal")
        self.assertEqual(self.m.snapshot()["phase"],"failed")
        self.assertFalse(self.m.config_path.exists())
        inbox=self.bus.subscribe_inbox("demo.test")
        self.bus.publish_payload("demo.test",timestamp=0,source="test",payload={"ok":True})
        self.assertTrue(inbox.get_nowait().payload["ok"])
        finish(self.m,"normal")

    def test_async_switch_and_revision_conflict(self):
        gate=threading.Event()
        class Slow(FakeAdapter):
            def start(self): gate.wait(2)
        self.m._adapter_factory=lambda mode,config:Slow()
        revision=self.m.snapshot()["revision"]
        response=self.m.select("ros2",expected_revision=revision)
        self.assertTrue(response["accepted"])
        with self.assertRaises(EnvironmentBusyError):self.m.select("normal")
        with self.assertRaises(EnvironmentRevisionConflict):self.m.select("ros2",expected_revision=revision)
        gate.set();self.m._worker.join(2)
        self.assertEqual(self.m.get_operation(response["operation_id"])["phase"],"completed")

    def test_invalid_contract_does_not_create_resource(self):
        with self.assertRaises(RosBindingError):self.f.publisher("input")
        with self.assertRaises(RosBindingError):self.f.subscribe("/undeclared")
        with self.assertRaises(ValueError):parse_ports({"ports":PORTS+[PORTS[0]]})
        with self.assertRaises(ValueError):normalize_bindings(self.f.declarations,{"output":{"message_type":"sensor_msgs/msg/Image"}})
        with self.assertRaises(ValueError):environment_config({"domain_id":True})
        with self.assertRaises(ValueError):environment_config({"discovery_interval_sec":float("nan")})

    def test_restore_closes_old_owners_and_retains_bus_instance(self):
        p=self.f.publisher("output");finish(self.m,"ros2")
        self.m.reset_after_restore()
        self.assertTrue(self.f.closed);self.assertIs(self.m.topic_bus,self.bus)
        self.assertEqual(p.publish(Message()).status,"unavailable")
        self.assertEqual(self.m.endpoints()["sent"],[])

    def test_owner_cleanup_even_if_plugin_unload_raises(self):
        facade=self.f
        class Plugin:
            id="broken"
            _astrbotex_ros=facade
            def on_load(self):self.out=facade.publisher("output")
            def on_unload(self):raise RuntimeError("plugin failure")
        registry=PluginRegistry();registry.register("trace_plugin",Plugin())
        finish(self.m,"ros2")
        with self.assertRaises(RuntimeError):registry.unregister("broken")
        self.assertTrue(facade.closed);self.assertEqual(self.m.endpoints()["sent"],[])

class RosConfigPersistenceTest(unittest.TestCase):
    def test_binding_revision_and_pubsub_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);plugin=root/"plugins/special/test";plugin.mkdir(parents=True)
            (plugin/"plugin.json").write_text(json.dumps({"id":"test","name":"test","version":"1","entry":"main.py",
                "provides":["trace_plugin"],"ros2":{"ports":PORTS}}),encoding="utf-8")
            (plugin/"main.py").write_text("class Plugin:\n    pass\n",encoding="utf-8")
            original={"pubsub":{"publish_enabled":True},"rate":3}
            (plugin/"config.json").write_text(json.dumps(original),encoding="utf-8")
            manager=LocalPluginManager(plugins_root=root/"plugins",state_path=root/"state.json",registry=PluginRegistry(),event_bus=EventBus(),topic_bus=TopicBus())
            manager.discover()
            result=manager.update_ros2("test",{"input":{"topic":"/new"}},0)
            self.assertTrue(result["saved"]);self.assertFalse(result["applied"])
            before=(plugin/"config.json").read_bytes()
            with self.assertRaises(EnvironmentRevisionConflict):manager.update_ros2("test",{},0)
            with self.assertRaises(ValueError):manager.update_ros2("test",{"bad":{}},1)
            self.assertEqual((plugin/"config.json").read_bytes(),before)
            manager.update_config("test",{"rate":5})
            saved=json.loads((plugin/"config.json").read_text())
            self.assertEqual(saved["pubsub"],original["pubsub"])
            self.assertEqual(saved["ros2"]["bindings"]["input"]["topic"],"/new")

if __name__=="__main__":unittest.main()
