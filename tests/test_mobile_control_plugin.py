"""Communication fixtures for the real plugin; these are not robot evidence."""
import importlib.util
import json
import threading
import time
import unittest
from collections import deque
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from astrbot_ex.core.tasks.contracts import action_manifest
from astrbot_ex.core.actions.models import ActionCommand
from astrbot_ex.core.environments.contracts import parse_ports

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('mobile_plugin_test',ROOT/'plugins/control/mobile_manipulation/main.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class Publisher:
    def __init__(self):self.sent=[];self.stop_sent=[]
    def new_message(self):return SimpleNamespace(data='')
    def publish(self,message):self.sent.append(json.loads(message.data));return SimpleNamespace(accepted=True)
    def publish_stop(self,message):self.stop_sent.append(json.loads(message.data));return SimpleNamespace(accepted=True)
class Inbox:
    def __init__(self):self.queue=deque()
    def push(self,value):self.queue.append(SimpleNamespace(message=SimpleNamespace(data=json.dumps(value))))
    def get_nowait(self):return self.queue.popleft() if self.queue else None
class Ports:
    def __init__(self):self.p={n:Publisher() for n in ('command','lease','cancel')};self.s={n:Inbox() for n in ('ack','feedback','result','status')}
    def publisher(self,n):return self.p[n]
    def subscribe(self,n):return self.s[n]
class Reports:
    def __init__(self):self.rows=[]
    def report(self,command_id,status,**kw):
        self.rows.append((command_id,status,kw));future=Future();future.set_result(True);return future

def command(skill='move'):
    import math
    params={'target_region_id':'home','position_tolerance_m':.1,'yaw_tolerance_rad':math.radians(10),'success_template':'move.arrive_and_stop.v1'} if skill=='move' else {'object_id':'obj-a','place_region_id':'tray_left','placement_tolerance_m':.020,'orientation_mode':'free','yaw_tolerance_rad':math.radians(5),'success_template':'fetch.pick_place_stable.v1'}
    return {'schema_version':1,'command_id':'macro1','ex_session':'session1','goal_id':'goal1','goal_revision':1,'decision_id':'decision1','owner':'mobile_manipulation','plugin_generation':1,'action_id':'mobile_manipulation.'+skill+'.v1','operation':'start','params':params,'lease_ms':30000}

class MobilePluginTests(unittest.TestCase):
    def make(self,*,compiler=None,evaluator=None,observer=None):
        ports,reports=Ports(),Reports();plugin=module.Plugin(SimpleNamespace(ros=ports,actions=reports,config={}))
        plugin.configure_execution('a'*64,{'home':[0.,0.,0.],'table_dock':[1.,0.,0.]},compile_fetch=compiler,
            physical_evaluator=evaluator or (lambda *a:{'physical_complete':False}),observation_provider=observer or (lambda:{}))
        ports.s['status'].push({'schema':'astrex.mm.v1','phase':'idle','raw_state_age':0.,'robot_config_hash':'a'*64})
        plugin.on_worker_step();self.addCleanup(lambda:(setattr(plugin,'job_closed',True),plugin.jobs.put_nowait(None)));return plugin,ports,reports
    def finish(self,plugin,ports,status='succeeded'):
        stage=ports.p['command'].sent[-1]
        value={k:stage[k] for k in module.IDENTITY}
        value.update(schema='astrex.mm.v1',status=status,scope='controller_stage',physical_complete=False,
            stop_evidence={'command_id':stage['command_id'],'stopped':True,'source':'isaac_raw_joint_feedback','reference':'fixture-stop.json'})
        ports.s['result'].push(value);plugin.on_worker_step();return value
    def test_manifest_matches_single_schema_source(self):
        manifest=json.loads((ROOT/'plugins/control/mobile_manipulation/plugin.json').read_text())
        self.assertEqual(manifest['actions'],action_manifest().to_dict()['actions'])
        self.assertEqual(len(parse_ports(manifest['ros2'])),7)
        self.assertFalse(manifest['enabled_default'])
    def test_callback_queues_move_and_worker_sends_real_protocol(self):
        plugin,ports,reports=self.make()
        self.assertEqual(plugin.on_action_command(ActionCommand.parse(command())),'accepted');self.assertEqual(ports.p['command'].sent,[])
        plugin.on_worker_step();stage=ports.p['command'].sent[0]
        self.assertEqual(stage['op'],'move');self.assertEqual(stage['command_id'],'macro1/0')
        self.assertEqual(stage['payload']['target_pose']['position'],[0.,0.,0.])
        self.assertEqual(reports.rows[0][1],'running')
    def test_fetch_without_recipe_is_explicitly_unavailable(self):
        plugin,ports,_=self.make();self.assertFalse(plugin.status()['fetch_available'])
        self.assertEqual(plugin.on_action_command(command('fetch'))['status'],'rejected')
        self.assertEqual(ports.p['command'].sent,[])
    def test_controller_success_waits_for_physical_stability(self):
        state={'ready':False}
        def evaluate(*args):return {'physical_complete':state['ready'],'success':True,'evidence_reference':'fixture-physical.json'}
        plugin,ports,reports=self.make(evaluator=evaluate)
        plugin.on_action_command(command());plugin.on_worker_step();self.finish(plugin,ports)
        self.assertNotIn('succeeded',[r[1] for r in reports.rows]);self.assertIsNotNone(plugin.active)
        state['ready']=True;plugin.active['last_physical_check']=0.
        deadline=time.monotonic()+1.
        while plugin.active is not None and time.monotonic()<deadline:
            plugin.on_worker_step();time.sleep(.002)
        self.assertEqual(reports.rows[-1][1],'succeeded');self.assertTrue(reports.rows[-1][2]['details']['physical_complete'])
    def test_external_cancel_uses_normal_port_and_preserves_child_stop(self):
        plugin,ports,reports=self.make();plugin.on_action_command(command());plugin.on_worker_step()
        plugin.on_action_cancel('macro1','user_cancel')
        self.assertEqual(len(ports.p['cancel'].sent),1);self.assertEqual(ports.p['cancel'].stop_sent,[])
        self.finish(plugin,ports,'canceled');record=reports.rows[-1]
        self.assertEqual(record[1],'canceled');self.assertEqual(record[2]['stop_evidence'].command_id,'macro1')
        self.assertEqual(record[2]['details']['last_stage_stop']['command_id'],'macro1/0')
    def test_internal_deadline_reports_failed_not_fake_external_cancel(self):
        plugin,ports,reports=self.make();plugin.on_action_command(command());plugin.on_worker_step()
        plugin.active['deadline']=0.;plugin.on_worker_step();self.finish(plugin,ports,'failed')
        self.assertEqual(reports.rows[-1][1],'failed')
    def test_cancel_before_publish_fences_all_child_commands(self):
        plugin,ports,reports=self.make();plugin.on_action_command(command());plugin.on_action_cancel('macro1','cancel')
        plugin.on_worker_step();self.assertEqual(ports.p['command'].sent,[])
        self.assertEqual(reports.rows[-1][2]['stop_evidence'].source,'plugin_no_command_published')
    def test_cancel_does_not_wait_for_grounder(self):
        entered,release=threading.Event(),threading.Event()
        def compiler(*args):entered.set();release.wait(2);return [{'name':'arm','op':'arm','payload':{'joint_targets':{}}}]
        plugin,ports,reports=self.make(compiler=compiler);plugin.on_action_command(command('fetch'))
        # The SAME Actor thread must finish its worker step before cancel is queued.
        began=time.monotonic();plugin.on_worker_step()
        self.assertLess(time.monotonic()-began,.2)
        self.assertTrue(entered.wait(1))
        try:
            plugin.on_action_cancel('macro1','cancel');plugin.on_worker_step()
            self.assertEqual(reports.rows[-1][1],'canceled')
            self.assertFalse(plugin.status()['fetch_available'])
        finally:release.set();plugin.job['done'].wait(1)
        plugin.on_worker_step()  # Late recipe is discarded after terminal cancellation.
        self.assertEqual(ports.p['command'].sent,[])
    def test_simulation_settle_uses_source_time_and_allows_cancel(self):
        clock={'source_stamp':10.}
        recipe=lambda *a:[{'op':'arm','payload':{'joint_targets':{}},'settle_sim_seconds':1.1},
                          {'op':'gripper','payload':{'position':0.}}]
        plugin,ports,reports=self.make(compiler=recipe,observer=lambda:dict(clock,received_monotonic=time.monotonic()))
        plugin.on_action_command(command('fetch'))
        end=time.monotonic()+1
        while not ports.p['command'].sent and time.monotonic()<end:plugin.on_worker_step();time.sleep(.001)
        self.finish(plugin,ports)
        for _ in range(5):plugin.on_worker_step();time.sleep(.002)
        self.assertEqual(len(ports.p['command'].sent),1)
        clock['source_stamp']=10.5
        for _ in range(5):plugin.on_worker_step();time.sleep(.002)
        self.assertEqual(len(ports.p['command'].sent),1)
        plugin.on_action_cancel('macro1','cancel_during_settle');plugin.on_worker_step()
        self.assertEqual(reports.rows[-1][1],'canceled')
        self.assertEqual(len(ports.p['command'].sent),1)

    def test_late_stop_survives_immutable_ex_timeout(self):
        plugin,ports,reports=self.make();plugin.on_action_command(command());plugin.on_worker_step()
        def immutable(*a,**kw):
            future=Future();future.set_exception(ValueError('timed_out is immutable'));return future
        reports.report=immutable
        plugin.on_action_cancel('macro1','EX_timeout');self.finish(plugin,ports,'canceled')
        self.assertIsNotNone(plugin.active)
        proof=plugin.pending_stop_proof('macro1')
        self.assertEqual(proof['last_stage_stop']['reference'],'fixture-stop.json')
        self.assertIsNone(plugin.pending_stop_proof('different-macro'))
        plugin.acknowledge_reconciled_stop('macro1','timed_out')
        self.assertIsNone(plugin.active)
        self.assertEqual(plugin.history[-1]['status'],'timed_out')

    def test_wrong_stage_identity_is_ignored(self):
        plugin,ports,reports=self.make();plugin.on_action_command(command());plugin.on_worker_step()
        stage=ports.p['command'].sent[-1];wrong={k:stage[k] for k in module.IDENTITY};wrong.update(schema='astrex.mm.v1',command_id='older')
        ports.s['result'].push(wrong);plugin.on_worker_step();self.assertIsNotNone(plugin.active['stage'])
