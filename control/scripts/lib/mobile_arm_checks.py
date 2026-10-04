"""A1/A2/R protection checks for the shared trial runner (no standalone runner).

Call only after the caller has started the actual Isaac scene, controllers,
MoveIt and gateway. Truth is evaluator-only; target scene geometry is the
explicit ORACLE test input. Controller success alone never passes a motion.
"""
from __future__ import annotations
from collections import deque
import json
import math
from pathlib import Path
import time
import uuid
import rclpy
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import String
from moveit_msgs.srv import GetPositionFK, GetPlanningScene
from moveit_msgs.msg import PlanningSceneComponents
from astrex_mobile_manipulation.client import GatewayClient
from astrex_mobile_manipulation.moveit_adapter import MoveItAdapter
from astrex_mobile_manipulation.core import encode, IDENTITY, StateCache
from astrex_mobile_manipulation.test_faults import (FAULT_SCHEMA,load_fault_config,write_fault_request,OwnedGateway)


class _EndFaultObservation(Exception):
    """The owned gateway is absent; no controller terminal is fabricated."""



def angular_error(a,b):
    if len(a)!=4 or len(b)!=4 or not all(math.isfinite(v) for v in [*a,*b]):
        raise ValueError('Finite xyzw quaternion pair required')
    norm_a=math.sqrt(sum(v*v for v in a)); norm_b=math.sqrt(sum(v*v for v in b))
    if min(norm_a,norm_b)<1e-12: raise ValueError('Zero quaternion is invalid')
    dot=abs(sum(x*y for x,y in zip(a,b)))/(norm_a*norm_b)
    return 2*math.acos(max(-1.,min(1.,dot)))

def _pose_message(pose):
    return {'position':[pose.position.x,pose.position.y,pose.position.z],
            'orientation':[pose.orientation.x,pose.orientation.y,pose.orientation.z,pose.orientation.w]}

class ArmChecks:
    def __init__(self,profile_path,evidence_dir,scene,truth_topic='/astrex/mm/evaluation/truth',fault_config=None):
        if not isinstance(scene,dict): raise ValueError('Explicit ORACLE scene specification is required')
        self.profile_path=profile_path; self.fault_config_path=fault_config
        self.client=GatewayClient(profile_path)
        self.profile=self.client.profile; self.scene=scene
        self.directory=Path(evidence_dir); self.directory.mkdir(parents=True,exist_ok=True)
        self.run_id='arm_checks_'+uuid.uuid4().hex
        self.states=deque(maxlen=20000); self.truth=deque(maxlen=10000)
        self.guarded=deque(maxlen=20000); self.checks=[]; self.case_id=None
        self.raw_commands=deque(maxlen=20000); self.broadcast_states=deque(maxlen=20000)
        self.final_states=deque(maxlen=20000); self.final_leases=deque(maxlen=20000)
        self.gateway_events=deque(maxlen=20000)
        self.fk=self.client.create_client(GetPositionFK,self.profile.moveit_namespace.rstrip('/')+'/compute_fk')
        qos=QoSProfile(depth=50,reliability=ReliabilityPolicy.RELIABLE,durability=DurabilityPolicy.VOLATILE)
        self.client.create_subscription(JointState,'/astrex/mm/joint_states_raw',self.on_state,qos)
        self.client.create_subscription(String,truth_topic,self.on_truth,qos)
        self.client.create_subscription(String,'/astrex/mm/guarded_joint_command',self.on_guarded,qos)
        self.client.create_subscription(String,'/astrex/mm/final_status',lambda msg:self.capture_json(msg,self.final_states),qos)
        if fault_config:
            self.client.create_subscription(JointState,'/astrex/mm/joint_command_raw',
                lambda msg:self.capture_joint(msg,self.raw_commands,'/astrex/mm/joint_command_raw'),qos)
            for topic in ('/joint_states','/astrex/mm/joint_states'):
                self.client.create_subscription(JointState,topic,
                    lambda msg,t=topic:self.capture_joint(msg,self.broadcast_states,t),qos)
            self.client.create_subscription(String,'/astrex/mm/final_lease',lambda msg:self.capture_json(msg,self.final_leases),qos)
            for channel in ('status','result','feedback'):
                self.client.create_subscription(String,'/astrex/mobile_mvp/'+channel,
                    lambda msg,c=channel:self.capture_json(msg,self.gateway_events,channel=c),qos)

    def capture_json(self,msg,buffer,**fields):
        buffer.append({'received':time.monotonic(),**json.loads(msg.data),**fields})

    def capture_joint(self,msg,buffer,topic):
        buffer.append({'received':time.monotonic(),'topic':topic,
            'stamp':msg.header.stamp.sec+msg.header.stamp.nanosec/1e9,
            'names':list(msg.name),'q':list(msg.position),'dq':list(msg.velocity)})

    def on_state(self,msg):
        self.states.append({'received':time.monotonic(),'stamp':msg.header.stamp.sec+msg.header.stamp.nanosec/1e9,
            'names':list(msg.name),'q':list(msg.position),'dq':list(msg.velocity)})

    def on_truth(self,msg):
        value=json.loads(msg.data)
        if not isinstance(value,dict): return
        self.truth.append({'received':time.monotonic(),**value})

    def on_guarded(self,msg):
        value=json.loads(msg.data)
        self.guarded.append({'received':time.monotonic(),**value})

    def pump(self,seconds):
        end=time.monotonic()+seconds
        while time.monotonic()<end: rclpy.spin_once(self.client,timeout_sec=.01)

    def wait_samples(self,timeout=30.):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.pump(.05)
            if (self.states and self.truth and time.monotonic()-self.states[-1]['received']<.2 and
                time.monotonic()-self.truth[-1]['received']<.2): return
        raise RuntimeError('Fresh raw Isaac state and evaluator truth are required')

    def positions(self,state=None):
        state=state or self.states[-1]
        return dict(zip(state['names'],state['q']))

    def joint_targets(self,values):
        return dict(zip(self.profile.arm_joint_names,values))

    def record(self,name,passed,**evidence):
        row={'check_id':name,'status':'PASS' if passed else 'FAIL',**evidence}
        self.checks.append(row)
        self.save(); return row

    def save(self):
        result={'schema':'astrex.mm.arm-checks.v1','run_id':self.run_id,
            'robot_config_hash':self.profile.profile_hash,'checks':self.checks,
            'all_passed':bool(self.checks) and all(c['status']=='PASS' for c in self.checks),
            'scope':'A1 model/state, A2 planning, R execution protection; not physical grasp/placement',
            'input_source':'ORACLE','scene':self.scene}
        target=self.directory/'arm_checks_result.json'
        tmp=target.with_suffix('.tmp'); tmp.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n'); tmp.replace(target)
        return result

    def execute(self,check_id,op,payload,**options):
        self.client.wait_ready(120.)
        self.wait_samples()
        self.case_id=check_id; before=self.positions(); started=time.monotonic()
        try:
            outcome=self.client.execute(op,payload,command_id=self.run_id+'/'+check_id,**options)
        except _EndFaultObservation:
            outcome={'ack':self.client.ack,'result':self.client.result,'feedback':self.client.feedback,
                     'error':'OWNED_GATEWAY_CRASH_OBSERVATION_ENDED_WITHOUT_TERMINAL'}
        self.pump(.1)
        trace=[s for s in self.states if s['received']>=started]
        frames=[s for s in self.truth if s['received']>=started]
        commands=[c for c in self.guarded if c.get('command_id')==self.client.current['command_id']]
        evidence={'command':self.client.current,'outcome':outcome,'start_positions':before,
            'raw_states':trace,'truth_frames':frames,'guarded_count':len(commands),
            'started_monotonic':started,'ended_monotonic':time.monotonic()}
        if self.fault_config_path:
            for name in ('raw_commands','broadcast_states','final_states','final_leases','gateway_events'):
                evidence[name]=[row for row in getattr(self,name) if row['received']>=started]
        (self.directory/(check_id+'.json')).write_text(json.dumps(evidence,indent=2,allow_nan=False)+'\n')
        return outcome,evidence

    def max_motion(self,evidence,names=None):
        names=names or self.profile.arm_joint_names
        before=evidence['start_positions']
        return max([0.,*[abs(dict(zip(s['names'],s['q']))[n]-before[n])
            for s in evidence['raw_states'] for n in names]])

    def controller_ok(self,outcome):
        result=outcome.get('result') or {}
        return result.get('status')=='succeeded' and result.get('stop_evidence',{}).get('stopped') is True

    def fk_pair(self):
        self.wait_samples()
        truth=self.truth[-1]; stamp=truth['source_stamp']
        state=min(self.states,key=lambda row:abs(row['stamp']-stamp))
        if abs(state['stamp']-stamp)>.05: raise RuntimeError('No time-aligned raw q and evaluator TCP pair')
        tcp=truth['tcp_pose']
        if tcp.get('frame_id')!='world': raise RuntimeError('Evaluator TCP frame must be explicitly world')
        request=GetPositionFK.Request(); request.header.frame_id='world'
        request.fk_link_names=[self.profile.tcp_frame]
        request.robot_state.joint_state.name=state['names']; request.robot_state.joint_state.position=state['q']
        if not self.fk.wait_for_service(timeout_sec=15.): raise RuntimeError('MoveIt compute_fk service is unavailable after discovery wait')
        future=self.fk.call_async(request); end=time.monotonic()+10.
        while not future.done() and time.monotonic()<end: self.pump(.01)
        if not future.done(): raise TimeoutError('MoveIt FK did not return')
        response=future.result()
        if response.error_code.val!=1 or len(response.pose_stamped)!=1: raise RuntimeError('MoveIt FK failed')
        actual=_pose_message(response.pose_stamped[0].pose)
        pos_error=math.dist(actual['position'],tcp['position'])
        angle=angular_error(actual['orientation'],tcp['orientation'])
        return {'source_stamp':stamp,'state_stamp':state['stamp'],'stamp_delta':abs(state['stamp']-stamp),
            'sim_session':truth['sim_session'],'truth_seq':truth['seq'],'q':self.positions(state),
            'moveit_fk':actual,'isaac_tcp':tcp,'position_error_m':pos_error,'orientation_error_rad':angle,
            'pass':pos_error<=.005 and angle<=math.radians(5.)}

    def model_and_gripper(self):
        home=list(self.profile.raw['initial_joint_positions'][:len(self.profile.arm_joint_names)])
        if len(home)!=6: raise ValueError('This MVP check requires the selected six arm joints')
        poses=[list(home),list(home),list(home)]
        poses[0][0]+=.12
        poses[1][0]-=.12; poses[1][1]+=.05; poses[1][2]-=.05; poses[1][3]+=.05
        for number,target in enumerate(poses,1):
            name=f'A1_pose_{number}'
            outcome,evidence=self.execute(name,'arm',{'joint_targets':self.joint_targets(target),'scene':self.scene},timeout=45.)
            measured=self.positions(); error=max(abs(measured[n]-q) for n,q in self.joint_targets(target).items())
            physical_motion=self.max_motion(evidence)
            fk=self.fk_pair() if self.controller_ok(outcome) else None
            self.record(name,self.controller_ok(outcome) and error<=.02 and physical_motion>=.03 and fk is not None and fk['pass'],
                target_q=target,actual_q=measured,max_joint_error_rad=error,max_motion_rad=physical_motion,
                fk_tcp=fk,evidence_file=name+'.json')
            if self.checks[-1]['status']!='PASS': return False
        gripper=self.profile.gripper_joint_names[0]; limits=self.profile.joint_limits[gripper]
        # Empty-gripper check; contact grasp is the separate A3/E0 runner.
        for label,target in [('close',min(.70,limits.upper)),('open',limits.lower)]:
            name='A1_gripper_'+label
            outcome,evidence=self.execute(name,'gripper',{'position':target,'max_effort':limits.effort},timeout=60.)
            actual=self.positions()[gripper]
            motion=self.max_motion(evidence,[gripper])
            self.record(name,self.controller_ok(outcome) and abs(actual-target)<=.03 and motion>=.05,
                target_rad=target,actual_rad=actual,max_motion_rad=motion,evidence_file=name+'.json')
            if self.checks[-1]['status']!='PASS': return False
        return True

    def planning(self):
        home=list(self.profile.raw['initial_joint_positions'][:6]); target=list(home); target[0]+=.08
        normal={'joint_targets':self.joint_targets(target),'scene':self.scene,'plan_only':True}
        unreachable={'target_pose':{'position':[10.,10.,10.],'orientation':[0.,0.,0.,1.]},'scene':self.scene,'plan_only':True}
        probe={'id':'planning_collision_probe','shape':'box','dimensions':[2.,2.,2.],
               'position':[0.,0.,.3],'orientation':[0.,0.,0.,1.]}
        obstructed={**normal,'scene':{**self.scene,'objects':[*self.scene.get('objects',[]),probe]}}
        try:
            for name,payload,expected in [('A2_reachable',normal,True),('A2_unreachable',unreachable,False),('A2_collision',obstructed,False)]:
                outcome,evidence=self.execute(name,'arm',payload,timeout=20.)
                result=outcome.get('result') or {}; motion=self.max_motion(evidence)
                valid=(self.controller_ok(outcome) if expected else result.get('status')=='failed' and
                    ('MOVEIT_ERROR:' in result.get('reason','') or 'CARTESIAN_INCOMPLETE:' in result.get('reason','')))
                self.record(name,valid and result.get('ros_goal_uuid') is None and evidence['guarded_count']==0 and motion<=.01,
                    expected_plan_success=expected,max_motion_rad=motion,guarded_count=evidence['guarded_count'],
                    input_source='SYNTHETIC_PLANNING_COLLISION_PROBE' if name=='A2_collision' else 'ORACLE',
                    result=result,evidence_file=name+'.json')
        finally:
            self.remove_planning_probe()
        return all(c['status']=='PASS' for c in self.checks if c['check_id'].startswith('A2_'))

    def remove_planning_probe(self):
        """Always remove the synthetic probe, even in an A2-only invocation."""
        adapter=MoveItAdapter(self.client,self.profile)
        started=time.monotonic(); end=started+15.
        row={'object_id':'planning_collision_probe','scene_ack':False,'removed_verified':False}
        try:
            while time.monotonic()<end:
                self.pump(.05)
                if (not self.client.status.get('moveit_inflight',True) and
                    self.client.status.get('phase')=='idle' and
                    adapter.apply.service_is_ready() and adapter.get_scene.service_is_ready()): break
            else: raise TimeoutError('Scene cleanup requires idle gateway and no pending plan')
            outcome={}
            adapter.update_scene({'objects':[{'id':'planning_collision_probe','remove':True}]},
                lambda value,error:outcome.update(result=value,error=error))
            while not outcome and time.monotonic()<end: self.pump(.01)
            if not outcome: raise TimeoutError('Scene cleanup acknowledgement did not return')
            if outcome['error']: raise RuntimeError(outcome['error'])
            row.update(outcome['result'])
            request=GetPlanningScene.Request()
            request.components.components=PlanningSceneComponents.WORLD_OBJECT_NAMES
            future=adapter.get_scene.call_async(request)
            while not future.done() and time.monotonic()<end: self.pump(.01)
            if not future.done(): raise TimeoutError('Scene cleanup readback did not return')
            names=[obj.id for obj in future.result().scene.world.collision_objects]
            row.update(remaining_object_ids=names,removed_verified='planning_collision_probe' not in names)
        except Exception as exc:
            row['error']=f'{type(exc).__name__}: {exc}'
        finally:
            row['elapsed_wall_seconds']=time.monotonic()-started
            self.record('A2_probe_cleanup',row['scene_ack'] and row['removed_verified'],**row)
            for service in (adapter.planner,adapter.cartesian,adapter.apply,adapter.get_scene):
                self.client.destroy_client(service)
        return row

    def protection(self):
        grip=self.profile.gripper_joint_names[0]
        outcome,evidence=self.execute('R_effort_rejected','gripper',
            {'position':.3,'max_effort':self.profile.joint_limits[grip].effort*1.25},timeout=10.)
        result=outcome.get('result') or {}
        self.record('R_effort_rejected',result.get('status')=='failed' and
            result.get('reason')=='GRIPPER_EFFORT_LIMIT' and result.get('ros_goal_uuid') is None and evidence['guarded_count']==0,
            result=result,guarded_count=evidence['guarded_count'],evidence_file='R_effort_rejected.json')
        home=list(self.profile.raw['initial_joint_positions'][:6])
        for name,drop_lease in [('R_cancel_during_motion',False),('R_lease_loss',True)]:
            before=self.positions(); target=list(home)
            target[0]=min(self.profile.joint_limits[self.profile.arm_joint_names[0]].upper,before[self.profile.arm_joint_names[0]]+.35)
            moved={'seen':False}
            def motion_seen(_):
                if self.states:
                    moved['seen']=moved['seen'] or max(abs(self.positions()[n]-before[n]) for n in self.profile.arm_joint_names)>.025
                return moved['seen']
            options={'lease_until':lambda client:not motion_seen(client)} if drop_lease else {'cancel_when':motion_seen}
            outcome,evidence=self.execute(name,'arm',{'joint_targets':self.joint_targets(target),'scene':self.scene},timeout=30.,**options)
            result=outcome.get('result') or {}; stop=result.get('stop_evidence') or {}
            expected=('LEASE_EXPIRED' in result.get('reason','')) if drop_lease else result.get('status')=='canceled'
            self.record(name,moved['seen'] and self.max_motion(evidence)>.025 and expected and stop.get('stopped') is True,
                motion_observed=moved['seen'],max_motion_rad=self.max_motion(evidence),result=result,evidence_file=name+'.json')
            if self.checks[-1]['status']!='PASS': return False
            # Replaying the exact canceled/expired envelope must only replay
            # its terminal receipt, never submit a new Action or target stream.
            old=dict(self.client.current); old_uuid=result.get('ros_goal_uuid')
            replay_start=time.monotonic()
            self.client.ack=None; self.client.result=None
            self.client.command_publishers['command'].publish(String(data=encode(old)))
            self.pump(.8)
            replay=self.client.result or {}
            new_commands=[r for r in self.guarded if r['received']>=replay_start]
            self.record(name+'_old_replay',replay.get('ros_goal_uuid')==old_uuid and replay.get('status')==result.get('status') and not new_commands,
                replay_result=replay,new_guarded_commands=len(new_commands),original_command_id=old['command_id'])
        return all(c['status']=='PASS' for c in self.checks if c['check_id'].startswith('R_'))

    def controllers_active(self):
        from controller_manager_msgs.srv import ListControllers
        service=self.client.create_client(ListControllers,'/astrex/mm/controller_manager/list_controllers')
        try:
            if not service.wait_for_service(timeout_sec=15.): raise RuntimeError('CONTROLLER_LIST_UNAVAILABLE')
            future=service.call_async(ListControllers.Request()); end=time.monotonic()+10.
            while not future.done() and time.monotonic()<end: self.pump(.01)
            if not future.done(): raise TimeoutError('CONTROLLER_LIST_TIMEOUT')
            states={c.name:c.state for c in future.result().controller}
            if any(states.get(name)!='active' for name in ('joint_state_broadcaster','arm_controller','gripper_controller')):
                raise RuntimeError('REQUIRED_CONTROLLERS_NOT_ACTIVE:'+str(states))
            return states
        finally: self.client.destroy_client(service)

    def write_pause(self,settings,seconds=1.2):
        now=time.monotonic(); latest=self.final_states[-1]
        fault=latest.get('local_test_fault',{})
        if (not fault.get('enabled') or fault.get('config_hash')!=settings['config_hash'] or
                latest.get('robot_config_hash')!=self.profile.profile_hash or
                any(latest.get(k)!=self.client.current[k] for k in IDENTITY)):
            raise RuntimeError('LOCAL_FAULT_NOT_BOUND_TO_THIS_SIMULATION_AND_COMMAND')
        request={'schema':FAULT_SCHEMA,'test_id':settings['test_id'],'seq':fault['seq']+1,
                 'isaac_session':latest['isaac_session'],'op':'raw_feedback_pause',
                 'until_monotonic':now+seconds,**{k:self.client.current[k] for k in IDENTITY}}
        write_fault_request(settings,request)
        return request

    def stop_window(self,states,after):
        cache=StateCache(self.profile)
        for state in states:
            if state['received']<after: continue
            try:
                cache.update(state['names'],state['q'],state['dq'],state['stamp'],state['received'])
                if cache.stopped(state['received']):
                    return {'stopped':True,'window_seconds':self.profile.stop_window,
                            'proven_monotonic':state['received'],'raw_source_stamp':state['stamp'],
                            'q':cache.positions,'dq':cache.velocities}
            except ValueError:
                return None
        return None

    def independent_faults(self):
        """R07/R08: explicit local injection; the normal R section stays unchanged."""
        if not self.fault_config_path: raise ValueError('R_FAULTS_REQUIRES_EXPLICIT_LOCAL_CONFIG')
        if self.profile.raw.get('base'): raise ValueError('R_FAULTS_REQUIRES_FIXED_BASE_ARM_PROFILE')
        settings=load_fault_config(self.fault_config_path,self.profile.profile_hash)
        self.wait_samples()
        fault=self.final_states[-1].get('local_test_fault',{}) if self.final_states else {}
        if not fault.get('enabled') or fault.get('config_hash')!=settings['config_hash']:
            raise RuntimeError('ISAAC_LOCAL_FAULT_CONFIG_NOT_ARMED')
        # ROS discovery precedes process ownership. Never kill an existing gateway.
        self.pump(2.)
        if self.client.get_publishers_info_by_topic('/astrex/mobile_mvp/status') or self.client.command_publishers['command'].get_subscription_count():
            raise RuntimeError('R_FAULTS_REQUIRES_NO_PREEXISTING_GATEWAY')
        owned=OwnedGateway(self.profile_path,self.directory/'fault_gateway')
        try:
            owned.start(); self.client.status={}; self.client.wait_ready(120.)
            if len(self.client.get_publishers_info_by_topic('/astrex/mobile_mvp/status'))!=1:
                raise RuntimeError('R_FAULTS_REQUIRES_EXACTLY_ONE_OWNED_GATEWAY')
            controllers_before=self.controllers_active()
            before=self.positions(); target=list(self.profile.raw['initial_joint_positions'][:6])
            target[0]=min(self.profile.joint_limits[self.profile.arm_joint_names[0]].upper,before[self.profile.arm_joint_names[0]]+.35)
            injected={}
            def pause_after_motion(_):
                if not injected and max(abs(self.positions()[n]-before[n]) for n in self.profile.arm_joint_names)>.025:
                    injected.update(self.write_pause(settings))
                return True
            outcome,evidence=self.execute('R07_raw_feedback_loss','arm',
                {'joint_targets':self.joint_targets(target),'scene':self.scene},timeout=45.,lease_until=pause_after_motion)
            result=outcome.get('result') or {}
            paused=[s for s in evidence['final_states'] if s.get('local_test_fault',{}).get('raw_feedback_paused')]
            interval=(paused[0]['local_test_fault']['applied_monotonic'],injected['until_monotonic']) if paused and injected else None
            broadcast=[s for s in evidence['broadcast_states'] if interval and interval[0]+.05<s['received']<interval[1]-.05]
            broadcast_topics={topic:len({s['stamp'] for s in broadcast if s['topic']==topic}) for topic in {s['topic'] for s in broadcast}}
            premature=[s for s in evidence['gateway_events'] if s['channel']=='result' and interval and interval[0]<=s['received']<interval[1]]
            raw_gap=max([0.,*[b['received']-a['received'] for a,b in zip(evidence['raw_states'],evidence['raw_states'][1:])]])
            physical_during_pause=[s for s in evidence['truth_frames'] if interval and interval[0]<=s['received']<interval[1]]
            controllers_after=self.controllers_active()
            passed=(bool(interval) and self.max_motion(evidence)>.025 and raw_gap>self.profile.state_age and
                    max(broadcast_topics.values(),default=0)>=2 and len(physical_during_pause)>=2 and not premature and
                    result.get('status')=='failed' and result.get('reason')=='RAW_STATE_STALE' and
                    result.get('stop_evidence',{}).get('stopped') is True and
                    self.stop_window(evidence['raw_states'],interval[1]) is not None)
            self.record('R07_raw_feedback_loss',passed,request=injected,raw_gap_seconds=raw_gap,
                broadcast_unique_stamps=broadcast_topics,physical_samples_during_pause=len(physical_during_pause),
                premature_results=len(premature),controllers_before=controllers_before,controllers_after=controllers_after,
                result=result,evidence_file='R07_raw_feedback_loss.json')
            if not passed: return False
            # Source publication has recovered; use a fresh normal Action before
            # crashing the same child. No test has a direct actuator publisher.
            self.client.wait_ready(120.); before=self.positions()
            target=list(self.profile.raw['initial_joint_positions'][:6])
            target[0]=max(self.profile.joint_limits[self.profile.arm_joint_names[0]].lower,before[self.profile.arm_joint_names[0]]-.35)
            crash={}
            def crash_after_motion(_):
                now=time.monotonic()
                if not crash and max(abs(self.positions()[n]-before[n]) for n in self.profile.arm_joint_names)>.025:
                    final=self.final_states[-1]
                    session=self.client.status.get('execution_session')
                    if (not final.get('active') or final.get('execution_session')!=session or
                            any(final.get(k)!=self.client.current[k] for k in IDENTITY)):
                        return True
                    if len(self.client.get_publishers_info_by_topic('/astrex/mobile_mvp/status'))!=1:
                        raise RuntimeError('GATEWAY_OWNERSHIP_GRAPH_CHANGED')
                    crash.update(monotonic=now,execution_session=session,final_before=final)
                    owned.crash(self.client.current['command_id'],session)
                if crash and now-crash['monotonic']>=2.:
                    raise _EndFaultObservation()
                return True
            outcome,evidence=self.execute('R08_owned_gateway_crash','arm',
                {'joint_targets':self.joint_targets(target),'scene':self.scene},timeout=45.,lease_until=crash_after_motion)
            if not crash:
                self.record('R08_owned_gateway_crash',False,reason='NO_CONFIRMED_MOTION_OR_BOUND_GATEWAY',evidence_file='R08_owned_gateway_crash.json')
                return False
            crash['returncode']=owned.confirm_crashed()
            stopped=[s for s in evidence['final_states'] if s['received']>=crash['monotonic'] and not s.get('active',True) and
                     s.get('execution_session')==crash['execution_session'] and
                     all(s.get(k)==self.client.current[k] for k in IDENTITY)]
            last_lease=max((s for s in evidence['final_leases'] if s.get('execution_session')==crash['execution_session']),
                           key=lambda s:s['received'],default=None)
            lease_deadline=last_lease.get('deadline_monotonic') if last_lease else None
            after_lease=[s for s in stopped if lease_deadline is not None and s['sample_monotonic']>=lease_deadline]
            continuing=[s for s in evidence['raw_commands'] if s['received']>crash['monotonic']+.05]
            physical_stop=self.stop_window(evidence['raw_states'],stopped[0]['received']) if stopped else None
            controllers_after=self.controllers_active()
            passed=(self.max_motion(evidence)>.025 and bool(stopped) and stopped[0].get('reason') in ('LEASE_EXPIRED','COMMAND_STALE') and
                    bool(after_lease) and len(continuing)>=2 and physical_stop is not None and
                    outcome.get('result') is None and crash['returncode']==-9)
            self.record('R08_owned_gateway_crash',passed,crash=crash,last_final_lease=last_lease,
                first_closed=stopped[0] if stopped else None,closed_samples_after_lease=len(after_lease),
                raw_jtc_commands_after_crash=len(continuing),physical_stop=physical_stop,
                controllers_after=controllers_after,evidence_file='R08_owned_gateway_crash.json',
                semantics='Independent final stop; COMMAND_STALE may precede the unchanged lease deadline.')
            if not passed: return False
            # A new gateway begins idle. Old JTC output must not be repackaged
            # as a new authorized epoch. Do not submit any replacement command.
            restart=time.monotonic(); owned.start(); self.client.status={}
            self.client.wait_ready(120.); self.pump(1.)
            recent=[s for s in self.final_states if s['received']>=restart]
            raw=[s for s in self.raw_commands if s['received']>=restart]
            forwarded=[s for s in self.guarded if s['received']>=restart]
            fresh_session=self.client.status.get('execution_session')
            restored=(fresh_session and fresh_session!=crash['execution_session'] and len(raw)>=2 and recent and
                      all(not s.get('active',True) for s in recent) and not forwarded and
                      self.stop_window(list(self.states),restart) is not None)
            (self.directory/'R08_restart_idle.json').write_text(json.dumps({'new_gateway_session':fresh_session,
                'old_gateway_session':crash['execution_session'],'final_states':recent,'raw_commands':raw,
                'guarded_commands':forwarded,'raw_states':[s for s in self.states if s['received']>=restart]},indent=2,allow_nan=False)+'\n')
            self.record('R08_restart_idle_old_cache',bool(restored),new_gateway_session=fresh_session,
                        raw_jtc_commands=len(raw),guarded_commands=len(forwarded),evidence_file='R08_restart_idle.json')
            return bool(restored)
        finally:
            # The source pause is bounded, including when the runner fails.
            # Normal cleanup never SIGKILLs any process and never targets a PID
            # supplied by the user or discovered from a generic process name.
            owned.close()

    def close(self):
        self.save(); self.client.destroy_node()


def run_arm_checks(profile_path,evidence_dir,scene,sections=('A1','A2','R'),truth_topic='/astrex/mm/evaluation/truth',fault_config=None):
    """Return a report; caller owns the ROS context and all simulator processes.

    Initial GUI review and stop-demo approval remain with the shared runner.
    Callers may select preparation-only A1/A2 and postpone R's stop demonstration.
    """
    if ('R_FAULTS' in sections) and (tuple(sections)!=('R_FAULTS',) or not fault_config):
        raise ValueError('R_FAULTS_MUST_RUN_ALONE_WITH_EXPLICIT_CONFIG')
    checker=ArmChecks(profile_path,evidence_dir,scene,truth_topic,fault_config)
    try:
        if tuple(sections)==('R_FAULTS',):
            checker.independent_faults()
            return checker.save()
        checker.client.wait_ready(120.); checker.wait_samples()
        for section,operation in [('A1',checker.model_and_gripper),('A2',checker.planning),('R',checker.protection)]:
            if section in sections and not operation(): break
        return checker.save()
    except Exception as exc:
        checker.record('infrastructure',False,error=f'{type(exc).__name__}: {exc}')
        return checker.save()
    finally: checker.close()
