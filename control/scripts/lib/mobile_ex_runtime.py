"""One real EX runtime for the shared Isaac trial runner.

Import from system Jazzy Python (or the existing EX Python with Jazzy sourced).
Only EX's existing native environment adapter owns ROS nodes and executors.
This module creates no A.E.B., HTTP server, model service or other plugin.
"""
from __future__ import annotations
import dataclasses
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
from mobile_paths import resolve_ex_root
EX = resolve_ex_root(REPO)
if str(EX) not in sys.path:sys.path.insert(0,str(EX))

from astrbot_ex.core.actions.dispatcher import ActionDispatcher
from astrbot_ex.core.actions.ledger import ActionLedger, StopEvidence
from astrbot_ex.core.actions.service import ActionService
from astrbot_ex.core.decision.backends.mock import MockBackend
from astrbot_ex.core.decision.catalog import CapabilityCatalog
from astrbot_ex.core.decision.service import DecisionService
from astrbot_ex.core.environments.manager import EnvironmentManager
from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.local_plugins import LocalPluginManager
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.runtime import AstrBotEXRuntime
from astrbot_ex.core.topic_bus import TopicBus
from astrbot_ex.core.tasks.contracts import ACTION_IDS, OWNER, validate_skill_parameters

class MobileEXRuntime:
    def __init__(self, run_dir, *, profile_hash, regions, profile=None, compile_fetch=None,
                 physical_evaluator=None, observation_provider=None, domain_id=63):
        self.run_dir=Path(run_dir)
        self.data_root=self.run_dir/'ex_runtime'
        self.data_root.mkdir(parents=True,exist_ok=False)
        self.events=EventBus();self.topics=TopicBus();self.registry=PluginRegistry()
        self.catalog=CapabilityCatalog()
        self.ledger=ActionLedger(self.data_root/'actions.sqlite3')
        self.dispatcher=ActionDispatcher(self.ledger)
        self.actions=ActionService(self.ledger,self.dispatcher,self.catalog,stop_timeout=8.)
        self.environment=EnvironmentManager(data_root=self.data_root,event_bus=self.events,topic_bus=self.topics)
        self.environment.action_service=self.actions
        self.decision=DecisionService(self.actions,backend=MockBackend(kind='start'),topic_bus=self.topics,
                                      environment=self.environment,io_timeout=5.)
        self.environment.decision_service=self.decision
        self.runtime=AstrBotEXRuntime(self.registry,event_bus=self.events,topic_bus=self.topics,
                                      action_service=self.actions,decision_service=self.decision)
        self.environment.runtime_running=lambda:self.runtime.state.value=='running'
        def owner_stop(slot,reason):
            self.decision.request_stop(reason)
            return self.actions.prove_owner_stop(slot,reason)
        self.registry.set_action_lifecycle_guard(owner_stop)
        source=EX/'plugins/control/mobile_manipulation'
        destination=self.data_root/'plugins/control/mobile_manipulation'
        destination.mkdir(parents=True)
        hashes={}
        for name in ('plugin.json','config.json','guide.md','main.py'):
            content=(source/name).read_bytes();(destination/name).write_bytes(content)
            hashes[name]=hashlib.sha256(content).hexdigest()
        (self.data_root/'plugin-source.json').write_text(json.dumps({'source':str(source),'sha256':hashes},indent=2)+'\n')
        self.plugins=LocalPluginManager(plugins_root=self.data_root/'plugins',state_path=self.data_root/'plugins_state.json',
            registry=self.registry,event_bus=self.events,topic_bus=self.topics,environment_manager=self.environment,
            action_dispatcher=self.dispatcher,capability_catalog=self.catalog)
        self.plugins.discover();self.plugins.set_enabled(OWNER,True)
        self.plugin=self.plugins.records[OWNER].plugin
        self.plugin.configure_execution(profile_hash,regions,profile=profile,compile_fetch=compile_fetch,
                                        physical_evaluator=physical_evaluator,observation_provider=observation_provider)
        self.actions.update_versions()
        self.domain_id=domain_id
        self.last_goal=None
        self.closed=False
        self._started=False
        self._submitted=[]
        self._reconciliations={}

    def start(self, *, wait_gateway=True, timeout=180.):
        if self._started:return self.status()
        self.environment.configure_ros2({'domain_id':self.domain_id,'namespace':'/astrbotex_mobile_mvp','node_name':'mobile_trial_'+uuid.uuid4().hex[:8]})
        operation=self.environment.select('ros2')
        end=time.monotonic()+timeout
        while True:
            state=self.environment.snapshot()
            if state['phase']=='failed':raise RuntimeError('EX ROS environment failed: '+str(state['last_error']))
            if state['active_mode']=='ros2' and state['phase']=='idle':break
            if time.monotonic()>=end:raise TimeoutError('EX ROS environment startup timeout')
            time.sleep(.02)
        self.actions.change_mode('decision')
        self.runtime.start()
        self.decision.set_mode('execute')
        self.decision.review().result(timeout=20.)
        self._started=True
        if wait_gateway:
            while not self.plugin.status()['gateway_fresh']:
                self.pump()
                if time.monotonic()>=end:raise TimeoutError('EX did not receive fresh idle gateway status')
                time.sleep(.02)
        return self.status()

    def _submit(self, skill, params, *, lease_ms=120000, task_id=None):
        if not self._started or self.closed:raise RuntimeError('EX runtime not started')
        if not self.plugin.status()[skill+'_available']:
            raise RuntimeError(skill+'_unavailable')
        bound=validate_skill_parameters(skill,params)
        actions=self.ledger.list_commands().result(5.)
        if any(row.held_resources for row in actions):raise RuntimeError('previous_action_resources_not_released')
        status=self.decision.goals.status()
        if status['phase'] in {'blocked','stopping'}:
            raise RuntimeError('goal_stop_or_review_required')
        goal_id=uuid.uuid4().hex;action_id=ACTION_IDS[skill]
        payload={'schema_version':1,'request_id':uuid.uuid4().hex,'ex_session':status['ex_session'],
            'task_id':task_id or 'trial_'+goal_id,'step_id':skill,'goal_id':goal_id,
            'goal_text_en':('Move to '+bound['target_region_id']+' and stop.') if skill=='move' else
                           ('Pick '+bound['object_id']+' and place in '+bound['place_region_id']+'.'),
            'allowed_actions':[action_id],'parameters':{action_id:bound},
            'completion':{'required_success_actions':[action_id]},'lease_ms':lease_ms,
            'expected_revision':status['revision']}
        result=self.decision.submit_goal(payload)
        self.last_goal=result;self._submitted.append({'request':payload,'receipt':result})
        with (self.data_root/'goals.jsonl').open('a') as log:log.write(json.dumps(self._submitted[-1],ensure_ascii=False)+'\n')
        return result

    def submit_fetch(self, params, **kwargs):return self._submit('fetch',params,**kwargs)
    def submit_move(self, params, **kwargs):return self._submit('move',params,**kwargs)

    def pump(self):
        """Signal existing runtime workers. Never spin a second ROS executor."""
        self.runtime.tick()
        self._reconcile_terminal_stops()
        return self.decision.status()

    def _reconcile_terminal_stops(self):
        # Failed remains failed. Only a committed mapped proof releases its resources.
        binding=self.plugins.records[OWNER].action_binding
        for command_id,future in tuple(self._reconciliations.items()):
            if future.done():
                committed=future.result()
                if not committed.held_resources:
                    self.plugin.acknowledge_reconciled_stop(command_id,committed.status)
        for row in self.ledger.list_commands().result(5.):
            if row.owner!=OWNER or row.status not in {'failed','timed_out','unknown'} or not row.held_resources:
                continue
            if row.command_id in self._reconciliations:
                future=self._reconciliations[row.command_id]
                if future.done():future.result()
                continue
            details=self.plugin.pending_stop_proof(row.command_id) or row.details
            proof=details.get('last_stage_stop')
            stages=details.get('stage_command_ids',[])
            if not isinstance(proof,dict) or proof.get('stopped') is not True:continue
            command_id=proof.get('command_id')
            fenced=(command_id==row.command_id and proof.get('source')=='plugin_no_command_published'
                    and proof.get('reference')=='admission_fence:'+row.command_id and not stages)
            child=(command_id in stages and command_id.startswith(row.command_id+'/')
                   and proof.get('source')=='isaac_raw_joint_feedback')
            if not (fenced or child):continue
            evidence=StopEvidence(row.command_id,True,proof['source'],proof['reference'])
            self._reconciliations[row.command_id]=self.dispatcher.reconcile_stop(row.command_id,binding,evidence)

    def status(self):
        rows=self.ledger.list_commands().result(5.)
        current=[]
        if self.last_goal:
            for row in rows:
                command=json.loads(row.canonical_command)
                if (command.get('goal_id')==self.last_goal['goal_id'] and
                        command.get('goal_revision')==self.last_goal['revision']):
                    current.append(row)
        terminal=(bool(current) and all(row.status in {'succeeded','failed','rejected','canceled','timed_out','unknown'}
                    and not row.held_resources for row in current))
        succeeded=terminal and all(row.status=='succeeded' for row in current)
        return {'terminal':terminal,'succeeded':succeeded,
            'current_command_ids':[row.command_id for row in current],
            'runtime':self.runtime.state.value,'environment':self.environment.snapshot(),
            'decision':self.decision.status(),'plugin':self.plugin.status(),
            'actions':[dataclasses.asdict(row) for row in rows],
            'last_goal':self.last_goal,'source':'FORMAL_EX_GOAL_ACTOR_LEDGER'}

    def cancel(self, reason='trial_cancel'):
        goal=self.decision.goals.status()
        current=goal['pending_goal_id'] or goal['active_goal_id']
        if current:
            return self.decision.cancel_goal({'schema_version':1,'request_id':uuid.uuid4().hex,
                'ex_session':goal['ex_session'],'goal_id':current,'goal_revision':goal['revision'],'reason_code':reason})
        return self.decision.request_stop(reason)

    def close(self):
        if self.closed:return
        errors=[]
        self.cancel('trial_close')
        # Keep native feedback and Actor workers alive until the public stop proof succeeds.
        end=time.monotonic()+12.
        while time.monotonic()<end:
            self.pump()
            if (self.decision.status().get('stop') or {}).get('state')=='proven':break
            time.sleep(.02)
        try:
            (self.data_root/'final-status.json').write_text(json.dumps(self.status(),ensure_ascii=False,indent=2)+'\n')
        except Exception as exc:errors.append('status:'+str(exc))
        for name,callback in (
            ('runtime',lambda:self.runtime.stop('trial_close')),
            ('environment',lambda:self.environment.close('trial_close')),
            ('plugin',lambda:self.registry.unregister(OWNER)),
            ('decision',self.decision.close),('actions',self.actions.close)):
            try:callback()
            except Exception as exc:errors.append(name+':'+str(exc))
        self.closed=True
        (self.data_root/'close.json').write_text(json.dumps({'errors':errors},indent=2)+'\n')
        if errors:raise RuntimeError('; '.join(errors))
