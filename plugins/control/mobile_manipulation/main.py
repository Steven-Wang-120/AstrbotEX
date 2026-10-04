"""EX macro-action owner using declared ROS ports only.

Controller-stage results require independent physical evaluation. No rclpy,
private executor, model inference or motion planning runs in an Actor callback.
"""
from __future__ import annotations
import copy
import json
import math
import queue
import threading
import time
from astrbot_ex.core.actions.ledger import StopEvidence
from astrbot_ex.core.actions.models import ActionCommand
from astrbot_ex.core.tasks.contracts import ACTION_IDS, validate_skill_parameters

IDENTITY = ('command_id', 'ex_session', 'goal_revision', 'execution_epoch')

class Plugin:
    def __init__(self, context):
        self.context = context
        self.lock = threading.RLock()
        self.publishers = {n:context.ros.publisher(n) for n in ('command','lease','cancel')}
        self.subscribers = {n:context.ros.subscribe(n) for n in ('ack','feedback','result','status')}
        self.active = None
        self.gateway_status = {}
        self.gateway_received = -math.inf
        self.profile = None
        self.profile_hash = ''
        self.regions = {}
        self.compile_fetch = self.physical_evaluator = self.observation_provider = None
        self.epoch = time.monotonic_ns()
        self.last_error = ''
        self.history = []
        self.jobs = queue.Queue(maxsize=1)
        self.job = None
        self.job_closed = False
        self.job_thread = threading.Thread(target=self._job_loop, name='mobile-pure-callbacks', daemon=True)
        self.job_thread.start()

    def configure_execution(self, profile_hash, regions, *, profile=None, compile_fetch=None,
                            physical_evaluator=None, observation_provider=None):
        """Trusted composition only. Call before submitting any formal Goal."""
        with self.lock:
            if self.active is not None:
                raise RuntimeError('cannot_reconfigure_active_action')
            if not isinstance(profile_hash,str) or len(profile_hash)!=64:
                raise ValueError('robot_profile_hash_required')
            self.profile_hash, self.profile = profile_hash, profile
            self.regions = copy.deepcopy(regions)
            self.compile_fetch, self.physical_evaluator = compile_fetch, physical_evaluator
            self.observation_provider = observation_provider

    def status(self):
        age=self.gateway_status.get('raw_state_age')
        fresh = (time.monotonic()-self.gateway_received < 1. and self.gateway_status.get('phase')=='idle'
                 and type(age) in (int,float) and math.isfinite(age) and 0<=age<=getattr(self.profile,'state_age',.2))
        physical = (callable(self.physical_evaluator) and callable(self.observation_provider)
                    and (self.job is None or self.job['done'].is_set()))
        return {'configured':bool(self.profile_hash), 'gateway_fresh':fresh,
                'move_available':bool(self.profile_hash and self.regions and physical and fresh),
                'fetch_available':bool(self.profile_hash and callable(self.compile_fetch) and physical and fresh),
                'active_command_id':self.active['command']['command_id'] if self.active else None,
                'error':self.last_error, 'history':copy.deepcopy(self.history[-16:])}

    def on_action_command(self, raw):
        command = ActionCommand.parse(raw.to_dict() if isinstance(raw,ActionCommand) else raw).to_dict()
        skill = next((s for s,a in ACTION_IDS.items() if a==command['action_id']),None)
        if skill is None or command['operation']!='start':
            return {'status':'rejected','reason':'unsupported_skill_operation'}
        validate_skill_parameters(skill,command['params'])
        with self.lock:
            available = self.status()
            if self.active is not None or not available[skill+'_available']:
                return {'status':'rejected','reason':'busy_or_skill_unavailable','capabilities':available}
            self.active={'command':copy.deepcopy(command),'skill':skill,'received':time.monotonic(),
                'deadline':time.monotonic()+command['lease_ms']/1000.,'cancel':None,'stages':None,
                'index':0,'stage':None,'results':[],'last_lease':0.,'seq':0,'running_reported':False,
                'last_physical_check':0.,'last_stop':None,'settle':None,'external_cancel':False,'terminal_pending':False}
        return 'accepted'

    def pending_stop_proof(self, command_id):
        """Trusted composition read, including late evidence after EX timeout."""
        with self.lock:
            active=self.active
            if (active is None or active['command']['command_id']!=command_id or active['stage'] is not None
                    or not active['last_stop']):return None
            return {'last_stage_stop':copy.deepcopy(active['last_stop']),
                    'stage_command_ids':[row['command_id'] for row in active['results']]}

    def acknowledge_reconciled_stop(self, command_id, status):
        """Call only after Dispatcher commits positive proof and releases resources."""
        with self.lock:
            if status not in {'failed','timed_out','unknown'}:raise ValueError('immutable_terminal_required')
            if self.pending_stop_proof(command_id) is None:return
            self.history.append({'command_id':command_id,'status':status,'reason':'stop_reconciled'})
            self.active=None

    def _envelope(self, stage, **values):
        return {'schema':'astrex.mm.v1', **{k:stage[k] for k in IDENTITY}, **values}

    def _send(self, kind, payload, *, environment_stop=False):
        encoded=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(encoded.encode())>65536:
            raise ValueError('gateway_payload_too_large')
        publisher=self.publishers[kind]
        message=publisher.new_message();message.data=encoded
        result=publisher.publish_stop(message) if environment_stop else publisher.publish(message)
        if not result.accepted:
            raise RuntimeError('ros_publish_'+result.status)

    def _request_stop(self, command_id, reason, *, external=False):
        with self.lock:
            active=self.active
            if active is None or active['command']['command_id']!=command_id:
                return {'status':'not_active'}
            active['cancel']=str(reason)[:128]
            active['external_cancel'] = active['external_cancel'] or external
            stage=copy.deepcopy(active['stage'])
        if stage:
            try:self._send('cancel',self._envelope(stage,reason=active['cancel']))
            except Exception as exc:self.last_error=type(exc).__name__+':'+str(exc)
        return 'requested'

    def on_action_cancel(self, command_id, reason):
        return self._request_stop(command_id,reason,external=True)

    def on_environment_deactivating(self, *args, **kwargs):
        with self.lock:
            if self.active is None:return
            self.active['cancel']='environment_deactivating'
            stage=copy.deepcopy(self.active['stage'])
        if stage:self._send('cancel',self._envelope(stage,reason='environment_deactivating'),environment_stop=True)

    def on_runtime_stop(self, *args, **kwargs):
        if self.active:self._request_stop(self.active['command']['command_id'],'runtime_stop')

    def on_unload(self):
        if self.active is not None:
            raise RuntimeError('active_action_stop_not_proven')
        self.job_closed = True
        try:self.jobs.put_nowait(None)
        except queue.Full:pass
        self.job_thread.join(.2)

    def _job_loop(self):
        # Exactly one bounded pure worker. It cannot publish ROS or report actions.
        while not self.job_closed:
            job=self.jobs.get()
            if job is None:return
            try:job['value']=job['callback']()
            except Exception as exc:job['error']=str(exc)[:256]
            finally:job['done'].set()

    def _start_job(self, active, kind, callback):
        if self.job is not None:return
        self.job={'active':active,'kind':kind,'callback':callback,'done':threading.Event(),'value':None,'error':None}
        self.jobs.put_nowait(self.job)

    def _fence_unpublished(self, active):
        if active['stage'] is None and not active['results']:
            active['last_stop']={'command_id':active['command']['command_id'], 'stopped':True,
                'source':'plugin_no_command_published', 'reference':'admission_fence:'+active['command']['command_id']}

    def _report(self, active, status, reason='', **details):
        proof=active['last_stop']
        stop_evidence=None
        if proof and proof.get('stopped') is True:
            stop_evidence=StopEvidence(active['command']['command_id'],True,proof['source'],proof['reference'])
        if status=='canceled' and stop_evidence is None:
            raise RuntimeError('stop_not_proven')
        result=self.context.actions.report(active['command']['command_id'],status,reason_code=reason[:128],
            details={'parent_command_id':active['command']['command_id'],
                     'stage_command_ids':[r.get('command_id') for r in active['results']],
                     'last_stage_stop':copy.deepcopy(proof), **details},stop_evidence=stop_evidence)
        if status in {'succeeded','failed','canceled'}:
            active['terminal_pending']=True
            def settled(future):
                with self.lock:
                    try:future.result()
                    except Exception as exc:
                        self.last_error='report_not_persisted:'+str(exc)
                        return
                    self.history.append({'command_id':active['command']['command_id'],'status':status,'reason':reason})
                    if self.active is active:self.active=None
            result.add_done_callback(settled)
        return result

    def _build_stages(self, active):
        if active['skill']=='fetch':
            observation=self.observation_provider()
            recipe=self.compile_fetch(active['command']['params'],observation,self.profile)
            if not isinstance(recipe,list) or not recipe or len(recipe)>32:
                raise ValueError('invalid_fetch_recipe')
            stages=copy.deepcopy(recipe)
        else:
            region=self.regions[active['command']['params']['target_region_id']]
            if isinstance(region,(list,tuple)):
                x,y,yaw=region
                pose={'position':[x,y,0.], 'orientation':[0.,0.,math.sin(yaw/2),math.cos(yaw/2)]}
            else:pose=copy.deepcopy(region['target_pose'])
            stages=[{'name':'navigate','op':'move','payload':{'target_pose':pose}}]
        if any(type(stage.get('settle_sim_seconds',0)) not in (int,float) or not 0<=stage.get('settle_sim_seconds',0)<=2 for stage in stages):
            raise ValueError('invalid_sim_settle_window')
        if any(stage.get('op') not in {'arm','gripper','move'} or not isinstance(stage.get('payload'),dict) for stage in stages):
            raise ValueError('invalid_recipe_stage')
        return stages

    def _receive(self, kind, raw):
        if not isinstance(raw,dict) or raw.get('schema')!='astrex.mm.v1':return
        if kind=='status':
            if raw.get('robot_config_hash')==self.profile_hash:
                self.gateway_status=raw;self.gateway_received=time.monotonic()
            return
        active=self.active
        if not active or not active['stage']:return
        stage=active['stage']
        if any(raw.get(k)!=stage[k] for k in IDENTITY):return
        if kind=='ack':
            active['ack']=raw
            if raw.get('status')=='rejected':
                # A negative admission receipt is not a physical success or StopEvidence.
                active['cancel']='gateway_rejected'
                self.last_error='gateway_rejected:'+str(raw.get('reason',''))
            return
        if kind=='feedback':
            active['feedback']=raw
            if raw.get('status')=='blocked':self.last_error='gateway_stop_unproven'
            return
        if kind!='result':return
        if raw.get('scope')!='controller_stage' or raw.get('physical_complete') is not False:
            active['cancel']='invalid_controller_result_scope';return
        proof=raw.get('stop_evidence')
        if not isinstance(proof,dict) or proof.get('command_id')!=stage['command_id'] or proof.get('stopped') is not True or not proof.get('source') or not proof.get('reference'):
            active['cancel']='invalid_gateway_stop_proof';return
        active['last_stop']=copy.deepcopy(proof)
        active['results'].append(copy.deepcopy(raw))
        active['stage']=None
        if active['cancel']:
            self._report(active,'canceled' if active['external_cancel'] else 'failed',active['cancel'],physical_complete=False)
        elif raw.get('status')!='succeeded':
            self._report(active,'failed',str(raw.get('reason','controller_stage_failed')),physical_complete=False)
        else:
            seconds=active['stages'][active['index']].get('settle_sim_seconds',0.)
            if seconds:
                active['settle']={'seconds':seconds,'start':None,'last':None,'last_progress':time.monotonic()}
            active['index']+=1

    def on_worker_step(self):
        # Nonblocking inbox reads only. ROS callbacks and native executor belong to EX.
        for kind,inbox in self.subscribers.items():
            for _ in range(8):
                item=inbox.get_nowait()
                if item is None:break
                try:
                    text=item.message.data
                    if len(text.encode())>65536:continue
                    raw=json.loads(text,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite')))
                    with self.lock:self._receive(kind,raw)
                except Exception as exc:self.last_error=type(exc).__name__+':'+str(exc)
        with self.lock:
            active=self.active
            built=None; evaluated=None; observed=None; callback_error=None
            if self.job is not None and self.job['done'].is_set():
                job=self.job;self.job=None
                if job['active'] is active and active is not None and not active['cancel']:
                    callback_error=job['error']
                    if job['kind']=='build':built=job['value']
                    elif job['kind']=='evaluate':evaluated=job['value']
                    else:observed=job['value']
            if active is None or active['terminal_pending']:return
            now=time.monotonic()
            if callback_error and not active['cancel']:
                self._fence_unpublished(active)
                self._report(active,'failed','grounding_or_evaluation_error',physical_complete=False,error=callback_error)
                return
            if built is not None and not active['cancel']:active['stages']=built
            if now>=active['deadline'] and not active['cancel']:
                self._request_stop(active['command']['command_id'],'macro_deadline')
            if active['cancel']:
                # Never renew a canceled stage. Its local watchdog stops independently.
                if active['stage'] and now-active.get('last_cancel',0)>.1:
                    try:self._send('cancel',self._envelope(active['stage'],reason=active['cancel']))
                    except Exception as exc:self.last_error=str(exc)
                    active['last_cancel']=now
                elif active['stage'] is None and active['last_stop']:
                    self._report(active,'canceled' if active['external_cancel'] else 'failed',active['cancel'],physical_complete=False)
                elif active['stage'] is None and not active['results']:
                    # Under the same send/cancel lock, no child was ever published.
                    # This proves command fencing, not global robot stationarity.
                    self._fence_unpublished(active)
                    self._report(active,'canceled' if active['external_cancel'] else 'failed',active['cancel'],physical_complete=False,no_motion_submitted=True)
                return
            if active['settle'] is not None:
                settle=active['settle']
                if observed is not None:
                    stamp=observed.get('source_stamp')
                    received=observed.get('received_monotonic',now)
                    stale=(type(received) not in (int,float) or not math.isfinite(received)
                           or not 0<=now-received<=getattr(self.profile,'state_age',.2))
                    if type(stamp) not in (int,float) or not math.isfinite(stamp) or stale:
                        self._report(active,'failed','settle_observation_stale',physical_complete=False);return
                    if settle['start'] is None:settle['start']=stamp
                    if settle['last'] is not None and stamp<settle['last']:
                        self._report(active,'failed','simulation_time_regressed',physical_complete=False);return
                    if stamp!=settle['last']:settle['last_progress']=now
                    settle['last']=stamp
                    if stamp-settle['start']>=settle['seconds']:active['settle']=None
                if active['settle'] is not None:
                    if now-settle['last_progress']>max(1.,getattr(self.profile,'state_age',.2)):
                        self._report(active,'failed','settle_observation_not_advancing',physical_complete=False);return
                    self._start_job(active,'observe',self.observation_provider)
                    return
            if active['stages'] is None:
                if active['skill']=='move':
                    try:active['stages']=self._build_stages(active)
                    except Exception as exc:
                        self._fence_unpublished(active)
                        self._report(active,'failed','grounding_error',physical_complete=False,error=str(exc)[:256])
                        return
                else:
                    self._start_job(active,'build',lambda:self._build_stages(active))
                    return
            if not active['running_reported']:
                self._report(active,'running',physical_complete=False)
                active['running_reported']=True
            if active['stage'] is not None:
                if now-active['last_lease']>=.1:
                    active['seq']+=1;active['last_lease']=now
                    try:self._send('lease',self._envelope(active['stage'],seq=active['seq']))
                    except Exception as exc:self._request_stop(active['command']['command_id'],'lease_publish_failed');self.last_error=str(exc)
                if not active.get('ack') and now-active['stage_started']>1.:
                    self._request_stop(active['command']['command_id'],'gateway_ack_timeout')
                return
            if active['index']>=len(active['stages']):
                if evaluated is None:
                    if now-active['last_physical_check']>=.02 and self.job is None:
                        active['last_physical_check']=now
                        command=copy.deepcopy(active['command']);results=copy.deepcopy(active['results'])
                        self._start_job(active,'evaluate',lambda:self.physical_evaluator(command,results,self.observation_provider()))
                    return
                if evaluated.get('physical_complete') is True:
                    if not evaluated.get('evidence_reference'):
                        self._report(active,'failed','physical_evidence_reference_required',physical_complete=False)
                    else:
                        self._report(active,'succeeded' if evaluated.get('success') is True else 'failed',
                            'physical_evaluation',physical_complete=True,evaluation=evaluated)
                return
            descriptor=active['stages'][active['index']]
            self.epoch+=1
            command=active['command']
            stage={'schema':'astrex.mm.v1','command_id':command['command_id']+'/'+str(active['index']),
                   'ex_session':command['ex_session'],'goal_revision':command['goal_revision'],
                   'execution_epoch':self.epoch,'deadline_monotonic':active['deadline'],
                   'robot_config_hash':self.profile_hash,'op':descriptor['op'],'payload':descriptor['payload']}
            active.update(stage=stage,stage_started=now,last_lease=now,seq=0,ack=None)
            try:self._send('command',stage)
            except Exception as exc:
                # No confirmed ROS receipt: retain ownership until watchdog/stop proof.
                self._request_stop(command['command_id'],'command_publish_uncertain');self.last_error=str(exc)
