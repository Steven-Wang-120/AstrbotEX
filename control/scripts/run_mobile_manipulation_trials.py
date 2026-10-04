#!/usr/bin/env python3
"""Single first-stage runner: diagnostics, formal fetch Goals and E0 records.

Start the protected Isaac scene and ROS stack separately. This runner never
publishes actuator commands, and never turns a controller receipt into success.
"""
import argparse
import functools
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts/lib'))
from mobile_paths import resolve_ex_root
sys.path.insert(0,str(resolve_ex_root(ROOT)))
import rclpy
from rclpy.node import Node
from rclpy.executors import SingleThreadedExecutor
from std_msgs.msg import String
from mobile_recipes import oracle_scene,fetch_recipe
from mobile_trial_evidence import (TruthIdentity,matches_trial,fetch_physical_decision,write_decision_evidence,finalize_trial,trial_run_summary)


class TruthReader(Node):
    def __init__(self,profile_hash=None):
        super().__init__('mm_trial_evaluator_'+uuid.uuid4().hex[:8])
        self.latest=None
        self.identity=TruthIdentity(profile_hash)
        self.binding=None
        self.reader_error=None
        self.acks={}
        self.pub=self.create_publisher(String,'/astrex/mm/evaluation/request',10)
        self.create_subscription(String,'/astrex/mm/evaluation/truth',self.receive,10)
        self.create_subscription(String,'/astrex/mm/evaluation/ack',self.ack,10)
        self.closed=False
        self.reader_executor=SingleThreadedExecutor()
        self.reader_executor.add_node(self)
        self.thread=threading.Thread(target=self.loop,name='mm_trial_evidence',daemon=True)
        self.thread.start()

    def loop(self):
        while not self.closed and rclpy.ok():
            self.reader_executor.spin_once(timeout_sec=.05)

    def receive(self,msg):
        try:
            value=self.identity.accept(json.loads(msg.data),time.monotonic())
            if value is not None:self.latest=value
        except Exception as exc:self.reader_error=str(exc)

    def ack(self,msg):
        try:
            value=json.loads(msg.data)
            if value.get('sim_session')!=self.identity.session:
                self.reader_error='ACK_SIM_SESSION_MISMATCH';return
            self.acks[value['request_id']]=value
        except Exception as exc:self.reader_error=str(exc)

    def observation(self,timeout=0.):
        end=time.monotonic()+timeout
        while True:
            if self.reader_error or self.identity.error:raise RuntimeError(self.reader_error or self.identity.error)
            value=self.latest
            fresh=value is not None and time.monotonic()-value['received_monotonic']<=.2
            if fresh and (self.binding is None or matches_trial(value,self.binding)):return value
            if time.monotonic()>=end:
                raise RuntimeError('EVALUATION_TRIAL_MISMATCH' if fresh else 'EVALUATION_FEEDBACK_STALE')
            threading.Event().wait(.02)

    def request(self,op,**params):
        if self.reader_error or self.identity.error:raise RuntimeError(self.reader_error or self.identity.error)
        if self.identity.session is None:raise RuntimeError('SIM_SESSION_NOT_OBSERVED')
        request_id=uuid.uuid4().hex
        self.pub.publish(String(data=json.dumps({'request_id':request_id,'op':op,'sim_session':self.identity.session,**params})))
        end=time.monotonic()+10.
        while time.monotonic()<end:
            if self.reader_error:raise RuntimeError(self.reader_error)
            if request_id in self.acks:
                ack=self.acks.pop(request_id)
                if not ack['ok']:raise RuntimeError('EVALUATOR_REJECTED:'+ack['error'])
                if op=='begin':
                    self.binding={key:params[key] for key in ('trial_id','scene_seed','task_kind')}
                    self.binding.update(sim_session=self.identity.session,profile_hash=self.identity.profile_hash,after_seq=ack['seq'])
                    # No Goal is admitted until a newer matching trial frame exists.
                    self.observation(2.)
                elif op=='end':self.binding=None
                return ack['result']
            threading.Event().wait(.02)
        raise TimeoutError('EVALUATOR_REQUEST_TIMEOUT')

    def settle(self,simulation_seconds=1.,timeout=60.):
        end=time.monotonic()+timeout
        started=None
        while time.monotonic()<end:
            value=self.observation(2.)
            still=(not value['guard_active'] and max(map(abs,value['dq'].values()),default=0)<.05 and
                   all(sum(v*v for v in o['linear_velocity'])<.0004 for o in value['objects'].values()))
            if still:
                if started is None:started=value['source_stamp']
                if value['source_stamp']-started>=simulation_seconds:return value
            else:started=None
            threading.Event().wait(.02)
        raise TimeoutError('PHYSICAL_SETTLE_TIMEOUT')

    def close(self):
        self.closed=True
        self.thread.join(2.)
        self.reader_executor.remove_node(self)
        self.reader_executor.shutdown()
        self.destroy_node()


def physical_fetch_evaluator(command,stage_results,observation,*,binding,evidence_dir):
    if not matches_trial(observation,binding):raise RuntimeError('PHYSICAL_TRIAL_IDENTITY_MISMATCH')
    if time.monotonic()-observation['received_monotonic']>.2:raise RuntimeError('PHYSICAL_OBSERVATION_STALE')
    if not stage_results or any(row.get('status')!='succeeded' for row in stage_results):
        complete,success,reason=True,False,'CONTROLLER_STAGE_FAILED'
    else:
        complete,success,reason=fetch_physical_decision(command['params'],observation['trial'])
    evaluation={'physical_complete':complete,'success':success,'details':{
        'reason':reason,'independent_physical_evaluation':observation['trial'],'scope':'full_fetch',
        'evaluation_source':'Isaac_pose_velocity_and_PhysX_contact_events'}}
    if complete:
        evaluation['evidence_reference']=write_decision_evidence(evidence_dir,command,observation,evaluation)
    return evaluation


def default_fetch_params():
    return {'object_id':'red_cube','place_region_id':'tray_left','placement_tolerance_m':.02,
            'orientation_mode':'free','yaw_tolerance_rad':.08726646259971647,
            'success_template':'fetch.pick_place_stable.v1'}


def arguments():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=('observe','arm_checks','fetch','e0','navigation'),required=True)
    p.add_argument('--profile',default=str(ROOT/'runtime/mobile_manipulation_deps/model/robot_profile.json'))
    p.add_argument('--scene-config',default=str(ROOT/'config/mobile_manipulation/arm_development_scene.json'))
    p.add_argument('--output-dir',required=True)
    p.add_argument('--sections',nargs='+',choices=('A1','A2','R','R_FAULTS'),default=['A1','A2','R'])
    p.add_argument('--fault-config',help='Explicit owner-only local R07/R08 test configuration; default disabled')
    p.add_argument('--seed',type=int,default=101)
    p.add_argument('--task-kind',choices=('G','PLACE','C'),default='PLACE')
    p.add_argument('--frozen-spec')
    p.add_argument('--simulation-config',help='Actual Isaac simulation_config.json; required for navigation')
    args=p.parse_args()
    if args.fault_config or 'R_FAULTS' in args.sections:
        if args.mode!='arm_checks' or args.sections!=['R_FAULTS'] or not args.fault_config:
            p.error('Local fault tests require --mode arm_checks --sections R_FAULTS --fault-config FILE')
    if args.mode=='navigation' and not args.simulation_config:
        p.error('--mode navigation requires --simulation-config from the running Isaac instance')
    return args


def main():
    args=arguments()
    output=Path(args.output_dir).resolve()
    output.mkdir(parents=True,exist_ok=True)
    profile=json.loads(Path(args.profile).read_text())
    config=json.loads(Path(args.scene_config).read_text())
    rclpy.init()
    from astrex_mobile_manipulation.core import RobotProfile
    reader=TruthReader(RobotProfile.from_dict(profile).profile_hash)
    try:
        observation=reader.observation(120.)
        if args.mode=='observe':
            (output/'observation.json').write_text(json.dumps(observation,indent=2)+'\n')
            print(json.dumps(observation,indent=2));return 0
        if args.mode=='navigation':
            from mobile_navigation_checks import run_navigation
            result=run_navigation(args.profile,config,output,reader,simulation_config_path=args.simulation_config)
            print(json.dumps({key:value for key,value in result.items() if key!='results'}))
            return 0 if result['all_passed'] else 1
        if args.mode=='arm_checks':
            from mobile_arm_checks import run_arm_checks
            result=run_arm_checks(args.profile,output,oracle_scene(observation,profile,config.get('scene',{})),
                                  sections=tuple(args.sections),fault_config=args.fault_config)
            print(json.dumps(result,indent=2));return 0 if result['all_passed'] else 1
        # Formal EX composition is imported only for macro actions. It uses
        # existing GoalManager/Dispatcher/Ledger and the native plugin ROS port.
        from mobile_ex_runtime import MobileEXRuntime
        if args.mode=='e0':
            if not args.frozen_spec:raise ValueError('E0_REQUIRES_REVIEWED_FROZEN_SPEC')
            frozen=json.loads(Path(args.frozen_spec).read_text())
            if frozen.get('human_reviewed') is not True:raise ValueError('E0_SPEC_NOT_REVIEWED')
            cases=frozen['cases']
        else:
            cases=[{'scene_seed':args.seed,'task_kind':args.task_kind,'trial_id':'dev_'+uuid.uuid4().hex[:12]}]
        results=[]
        for case in cases:
            trial_id=case['trial_id']
            if (output/(trial_id+'.json')).exists():raise ValueError('TRIAL_RESULT_ALREADY_EXISTS:'+trial_id)
            recipe_kind='C' if case['task_kind']=='C' else 'PLACE'
            binding=None
            begun=False
            runtime=None
            row={'case':case,'formal_goal':None,'runtime':None,'physical':None,'errors':[],
                 'scope':'E0_ZERO_ERROR_ORACLE' if args.mode=='e0' else 'DEVELOPMENT',
                 'full_fetch_success':False,'trial_begun':False}
            try:
                reader.settle()
                reader.request('reset',scene_seed=case['scene_seed'])
                reader.settle()
                reader.request('begin',**case)
                begun=True;row['trial_begun']=True
                binding=dict(reader.binding);row['evidence_binding']=binding
                runtime=MobileEXRuntime(output/trial_id,profile_hash=RobotProfile.from_dict(profile).profile_hash,profile=profile,
                    regions={'table_dock':[0.,0.,0.],'home':[-1.,0.,0.]},
                    compile_fetch=functools.partial(fetch_recipe,scene_config=config.get('scene',{}),task_kind=recipe_kind),
                    physical_evaluator=functools.partial(physical_fetch_evaluator,binding=binding,evidence_dir=output/trial_id),observation_provider=reader.observation)
                runtime.start()
                row['formal_goal']=runtime.submit_fetch(default_fetch_params())
                end=time.monotonic()+180.
                while time.monotonic()<end:
                    runtime.pump()
                    status=runtime.status()
                    if status.get('terminal'):break
                    threading.Event().wait(.02)
                else:
                    runtime.cancel('TRIAL_WALL_DEADLINE')
                    stop_end=time.monotonic()+10.
                    while time.monotonic()<stop_end:
                        runtime.pump();status=runtime.status()
                        if status.get('terminal'):break
                        threading.Event().wait(.02)
                row['runtime']=status
                row['ledger_succeeded']=bool(status.get('succeeded'))
            except Exception as exc:
                row['errors'].append(type(exc).__name__+':'+str(exc))
            finally:
                if runtime is not None:
                    try:runtime.close()
                    except Exception as exc:row['errors'].append('close:'+str(exc))
                if begun or (reader.binding and reader.binding.get('trial_id')==trial_id):
                    # A begin ACK counts as started even if the first matching
                    # truth frame timed out before Goal submission.
                    row['trial_begun']=True
                    if binding is None:
                        binding=dict(reader.binding);row['evidence_binding']=binding
                    try:row['physical']=reader.request('end',trial_id=trial_id)
                    except Exception as exc:row['errors'].append('physical_end:'+str(exc))
                finalize_trial(row,binding,default_fetch_params())
                (output/(trial_id+'.json')).write_text(json.dumps(row,indent=2,allow_nan=False)+'\n')
                results.append(row)
            if not row['record_complete']:
                # Do not reset or start another trial with an unresolved stop.
                break
        summary,exit_code=trial_run_summary(args.mode,results,cases)
        (output/'trial_summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
        print(json.dumps({k:v for k,v in summary.items() if k!='results'}))
        return exit_code
    finally:
        reader.close()
        rclpy.shutdown()


if __name__=='__main__':raise SystemExit(main())
