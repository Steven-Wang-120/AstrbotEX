"""L1/T1 tests use fixture HTTP only; no model or robot is started."""
import copy
import dataclasses
import json
import threading
import time
import unittest
from astrbot_ex.core.tasks.contracts import action_manifest, validate_skill_parameters, project_catalog
from astrbot_ex.core.tasks.session import TaskSession
from astrbot_ex.core.tasks.laya_client import TaskLayaClient, TaskModelSession
from astrbot_ex.core.tasks.corpus import build_corpus, development_probes, validate_corpus, review_samples
from astrbot_ex.core.decision.backends.laya import LayaConfig, LayaBackendError
from astrbot_ex.core.decision.owned_laya import OwnedLayaError, Deployment
from tests.test_laya_backend import FixtureTransport, reply, response
from tests import test_owned_laya as owned_fixtures

def observation(**changes):
    value={"observation_id":"obs1", "valid":True, "age_ms":0, "max_age_ms":500,
           "at_table":True,"stationary":True,
           "objects":[{"id":"obj-a","color":"red","shape":"cube"},{"id":"obj-b","color":"blue","shape":"cube"}]}
    value.update(changes)
    return value

class MobileTaskTests(unittest.TestCase):
    def make(self, text="把红色方块放到左托盘", **obs):
        s=TaskSession(); context,status=s.feed(text,observation(**obs)); return s,context,status
    def client(self, transport=None, **config):
        client=TaskLayaClient(LayaConfig(enabled=True,**config),transport=transport or FixtureTransport(choices={"task":"C"}))
        client.generation="g1";self.addCleanup(client.close);return client
    def test_projection_keeps_unavailable_owner_and_rejects_schema_drift(self):
        raw={"revision":7,"entries":[{"owner":"mobile_manipulation","manifest":action_manifest().to_dict(),"available":False,"unavailable_reason":"loading"}]}
        projection=project_catalog(raw)
        self.assertFalse(projection["available"]);self.assertEqual(projection["source_catalog_revision"],7)
        raw["entries"][0]["manifest"]["actions"][0]["schema"]["properties"]["position_tolerance_m"]["enum"]=[1.0]
        with self.assertRaisesRegex(ValueError,"schema_mismatch"):project_catalog(raw)

    def test_cli_clarification_keeps_one_session_and_zero_goals(self):
        from astrbot_ex.core.tasks.session import run_cli
        session=TaskSession();turns=iter(['抓取红色方块','左托盘','/quit']);output=[]
        run_cli(session,observation,self.client(),input_fn=lambda prompt:next(turns),output_fn=output.append)
        self.assertEqual([row['state'] for row in output],['clarification_required','ready'])
        self.assertEqual(session.revision,2)
        self.assertEqual(output[-1]['parameters']['place_region_id'],'tray_left')
        self.assertTrue(all(row['goal_submissions']==0 for row in output))

    def test_real_unavailable_catalog_blocks_pre_goal_proposal(self):
        from astrbot_ex.core.tasks.contracts import skill_catalog
        session=TaskSession(catalog=dict(skill_catalog(),available=False,unavailable_reason='disabled'))
        context,status=session.feed('把红色方块放到左托盘',observation())
        selected=self.client().select(context)
        self.assertEqual(status['state'],'skill_unavailable')
        self.assertEqual(session.accept(selected,service_generation='g1')['state'],'skill_unavailable')

    def test_manifest_and_fixed_discrete_bounds(self):
        manifest=action_manifest();self.assertEqual(len(manifest.actions),2)
        params={"target_region_id":"home","position_tolerance_m":.10,"yaw_tolerance_rad":0.17453292519943295,"success_template":"move.arrive_and_stop.v1"}
        validate_skill_parameters('move',params)
        for bad in (.5,float('nan'),.005):
            with self.assertRaises(ValueError):validate_skill_parameters('move',{**params,'position_tolerance_m':bad})
    def test_real_model_shape_maps_to_task_without_goal(self):
        session,context,status=self.make(); selection=self.client().select(context)
        result=session.accept(selection,service_generation='g1')
        self.assertEqual(result['skill'],'fetch');self.assertEqual(result['parameters']['object_id'],'obj-a')
        self.assertFalse(result['execution_enabled']);self.assertEqual(result['goal_submissions'],0)
    def test_missing_destination_followup(self):
        session,context,status=self.make('抓取红色方块')
        self.assertEqual(status['state'],'clarification_required')
        context,status=session.feed('放到左托盘',observation())
        self.assertEqual(status['state'],'ready_for_selection')
        self.assertEqual(session.goal_submissions,0)
    def test_same_color_requires_object_binding(self):
        obs=observation();obs['objects'][1]['color']='red'
        session=TaskSession();_,status=session.feed('把红色方块放到左托盘',obs)
        self.assertEqual(status['state'],'clarification_required')
        _,status=session.feed('选择obj-b',obs)
        self.assertEqual(status['state'],'ready_for_selection')
    def test_unknown_cup_stale_and_unsupported(self):
        for text,obs,state in [('跳舞',{},'unsupported'),('抓红色杯子',{},'unsupported'),('把红色方块放到左托盘',{'valid':False},'wait_observation')]:
            with self.subTest(text=text):self.assertEqual(self.make(text,**obs)[2]['state'],state)
    def test_late_cancel_revision_generation_and_observation(self):
        for change in ('cancel','revision','generation','observation'):
            s,c,_=self.make();sel=self.client().select(c)
            if change=='cancel':s.cancel()
            if change=='revision':s.feed('精确放置',observation())
            with self.assertRaises(ValueError):
                s.accept(sel,service_generation='g2' if change=='generation' else 'g1',current_observation_id='obs2' if change=='observation' else 'obs1')
    def test_wrong_object_rejected_no_repair(self):
        s,c,_=self.make();sel=self.client(FixtureTransport(choices={'task':'D'})).select(c)
        with self.assertRaisesRegex(ValueError,'wrong_object'):s.accept(sel,service_generation='g1')
    def test_done_needs_evidence(self):
        s,c,_=self.make();sel=self.client(FixtureTransport(choices={'task':'H'})).select(c)
        with self.assertRaisesRegex(ValueError,'success_not_proven'):s.accept(sel,service_generation='g1')
    def test_bad_choice_nan_and_truncation_quarantine(self):
        changes=[lambda raw:raw['answers']['task'].update(choice='Z'),lambda raw:raw['answers']['task']['probabilities'].update(A=float('nan')),lambda raw:raw['usage'].update(truncated=True)]
        for mutation in changes:
            c=self.make()[1];client=self.client(FixtureTransport(mutate=mutation))
            with self.assertRaises(LayaBackendError):client.select(c)
            self.assertTrue(client.status()['restart_required'])
    def test_text_does_not_serialize_objects(self):
        s=TaskSession();c,_=s.feed('把红色方块放到左托盘',observation(),mode='text')
        body,_,_=self.client().request_payload(c);raw=json.loads(body)
        self.assertNotIn('observation',json.loads(raw['state']));self.assertNotIn('obj-a',raw['state'])
        self.assertIn('Fetch object to named tray',str(raw['questions']))
    def test_invalid_context_does_not_hold_busy_permit(self):
        client=self.client()
        with self.assertRaises(LayaBackendError):client.select(None)
        self.assertFalse(client.status()["busy"])
        self.assertEqual(client.select(self.make()[1]).option_id,"fetch:obj-a")

    def test_budget_rejected_before_http(self):
        transport=FixtureTransport();client=self.client(transport)
        c=self.make('红'*450+'方块放到左托盘')[1]
        with self.assertRaises(LayaBackendError):client.select(c)
        self.assertEqual(transport.calls,[])
    def test_two_orders_bind_same_contract_and_keep_ambiguity(self):
        results=[]
        for text in ("把红色方块放到左托盘", "在左托盘上放红色方块"):
            session,context,_=self.make(text)
            results.append(session.accept(self.client().select(context),service_generation="g1"))
            obs=observation();obs["objects"][1]["color"]="red"
            self.assertEqual(TaskSession().feed(text,obs)[1]["state"],"clarification_required")
        self.assertEqual(results[0]["contract_hash"],results[1]["contract_hash"])

    def test_equivalent_orders_for_all_supported_colors_shapes_and_trays(self):
        for zh,color in (("红","red"),("蓝","blue"),("绿","green")):
            for shape_zh,shape in (("方块","cube"),("圆柱","cylinder")):
                for tray in ("左托盘","右托盘"):
                    obs=observation()
                    obs["objects"][0].update(color=color,shape=shape)
                    obs["objects"][1].update(color="blue" if color!="blue" else "red",shape=shape)
                    results=[]
                    for phrase in (f"把{zh}色{shape_zh}放到{tray}",f"在{tray}上放{zh}色{shape_zh}"):
                        session=TaskSession();context,status=session.feed(phrase,obs)
                        self.assertEqual(status["state"],"ready_for_selection")
                        results.append(session.accept(self.client().select(context),service_generation="g1")["parameters"])
                    self.assertEqual(results[0],results[1])

    def test_same_color_explicit_id_is_visible_to_model(self):
        obs=observation();obs['objects'][1].update(color='red',shape='cube')
        session=TaskSession();context,status=session.feed('把红色方块obj-b放到左托盘',obs)
        self.assertEqual(status['state'],'ready_for_selection')
        body,mapping,_=self.client().request_payload(context)
        labels=json.loads(body)['questions']['task']['criteria']
        self.assertIn('obj-a',labels['C']);self.assertIn('obj-b',labels['D'])
        selected=self.client(FixtureTransport(choices={'task':'D'})).select(context)
        self.assertEqual(session.accept(selected,service_generation='g1')['parameters']['object_id'],'obj-b')

    def test_v2_detects_actual_semantic_duplicates_despite_new_scene_ids(self):
        groups=build_corpus();report=validate_corpus(groups)
        self.assertEqual(report['unique_semantic_tasks'],120)
        self.assertEqual(report['unique_actual_model_inputs'],230)
        self.assertEqual(report['development_overlap'],0)
        self.assertEqual(set(report['fetch_target_roles']),{'obj-a','obj-b'})
        src=next(g for g in groups if g['category']=='fetch' and g['split']=='train')
        dst=next(g for g in groups if g['category']=='fetch' and g['split']=='test')
        dst['decision_points']=copy.deepcopy(src['decision_points'])
        dst['decision_points'][0]['observation']['observation_id']='new-id-does-not-create-an-independent-task'
        with self.assertRaisesRegex(ValueError,'duplicate_semantic_task_between_groups'):validate_corpus(groups)

    def test_v2_excludes_original_development_semantics(self):
        groups=build_corpus()
        dev=next(g for g in development_probes() if g['category']=='fetch')
        groups[0]['decision_points']=copy.deepcopy(dev['decision_points'])
        with self.assertRaisesRegex(ValueError,'development_semantic_leak'):validate_corpus(groups)

    def test_corpus_count_grouping_and_source(self):
        report=validate_corpus(build_corpus());self.assertEqual(report['decision_points'],120)
        self.assertEqual(len(review_samples()),14);self.assertEqual(len(development_probes()),20)
        groups=build_corpus();groups[-1]['language_family']=groups[0]['language_family']
        with self.assertRaisesRegex(ValueError,'family_leak'):validate_corpus(groups)

class TaskOwnedIntegrationTests(unittest.TestCase):
    setUp = owned_fixtures.OwnedLayaTests.setUp
    tearDown = owned_fixtures.OwnedLayaTests.tearDown
    manager = owned_fixtures.OwnedLayaTests.manager
    def test_task_uses_same_permit_and_does_not_need_ex_backend_switch(self):
        owner=self.manager();owner.start()
        task=TaskModelSession(owner,execution_idle=lambda:True,transport=FixtureTransport(choices={'task':'C'})).attach()
        session=TaskSession();context,_=session.feed('把红色方块放到左托盘',observation())
        self.assertEqual(task.select(context).option_id,'fetch:obj-a')
        with owner.decision_guard(owner.generation,task.client):
            with self.assertRaises(OwnedLayaError):task.select(context)
        old=owner.generation;result=task.recover()
        self.assertNotEqual(old,owner.generation);self.assertFalse(result['replayed'])
    def test_cold_load_budget_is_separate_from_hot_and_stop(self):
        extended=dataclasses.replace(self.deployment,startup_timeout_s=1800)
        self.assertEqual(extended.startup_timeout_s,1800)
        self.assertEqual(extended.terminate_timeout_s,self.deployment.terminate_timeout_s)
        with self.assertRaises(OwnedLayaError):dataclasses.replace(self.deployment,startup_timeout_s=3601)
        self.assertEqual(TaskLayaClient().config.deadline_ms,300)

    def test_recovery_requires_execution_stopped(self):
        owner=self.manager();owner.start()
        task=TaskModelSession(owner,execution_idle=lambda:False).attach()
        with self.assertRaisesRegex(OwnedLayaError,'execution_not_stopped'):task.recover()

class ColdWarmupBoundsTests(unittest.TestCase):
    def test_trusted_cold_budget_does_not_change_hot_limit(self):
        from astrbot_ex.core.decision.owned_laya import Deployment, OwnedLayaError, _ColdWarmupBackend
        from astrbot_ex.core.decision.backends.laya import LayaConfig, LayaBackendError
        from pathlib import Path
        import time
        d=Deployment(Path('/usr/bin/python3'),Path('/tmp'),Path('/tmp'),startup_timeout_s=1800,warmup_deadline_ms=1800000)
        with self.assertRaises(OwnedLayaError):Deployment(d.python,d.cache,d.output,warmup_deadline_ms=3600001)
        with self.assertRaises(LayaBackendError):LayaConfig(deadline_ms=60001)
        client=_ColdWarmupBackend(LayaConfig(enabled=True,allow_live_http=True,deadline_ms=60000),d.warmup_deadline_ms)
        started=time.monotonic();call=client._begin(started)
        self.assertEqual(call.deadline,started+1800)
        client.close()
