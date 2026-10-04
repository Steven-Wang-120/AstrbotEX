"""Bounded EX/ROS gateway. The only arm execution path is plan -> guard -> JTC."""
from __future__ import annotations
import json
import math
import time
import uuid
from pathlib import Path
import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from std_msgs.msg import String
from sensor_msgs.msg import JointState
from geometry_msgs.msg import TwistStamped
try:
    from nav2_msgs.action import NavigateToPose
except ImportError:
    NavigateToPose = None
from control_msgs.action import FollowJointTrajectory, ParallelGripperCommand
from action_msgs.msg import GoalStatus
from .core import (SCHEMA, IDENTITY, Rejected, RobotProfile, StateCache, CommandLedger,
                   decode, encode, envelope, identity, finite)
from .moveit_adapter import MoveItAdapter

class Gateway(Node):
    def __init__(self):
        super().__init__('mm_gateway')
        self.declare_parameter('profile',''); self.declare_parameter('evidence_dir','')
        self.profile=RobotProfile.load(self.get_parameter('profile').value)
        evidence=self.get_parameter('evidence_dir').value
        if not evidence: raise RuntimeError('evidence_dir is required')
        self.evidence=Path(evidence); self.evidence.mkdir(parents=True,exist_ok=True)
        self.log_file=(self.evidence/'gateway.jsonl').open('a',buffering=1)
        self.session=uuid.uuid4().hex
        self.state=StateCache(self.profile); self.ledger=CommandLedger()
        self.moveit=MoveItAdapter(self,self.profile)
        self.arm=ActionClient(self,FollowJointTrajectory,self.profile.arm_action)
        self.gripper=ActionClient(self,ParallelGripperCommand,self.profile.gripper_action)
        self.navigation=ActionClient(self,NavigateToPose,'/astrex/mm/navigate_to_pose') if NavigateToPose is not None else None
        qos=QoSProfile(depth=10,reliability=ReliabilityPolicy.RELIABLE,durability=DurabilityPolicy.VOLATILE)
        self.outputs={kind:self.create_publisher(String,'/astrex/mobile_mvp/'+kind,qos) for kind in ('ack','feedback','result','status')}
        self.base_command=self.create_publisher(String,'/astrex/mm/guarded_base_command',qos)
        self.final_command=self.create_publisher(String,'/astrex/mm/guarded_joint_command',qos)
        self.final_lease=self.create_publisher(String,'/astrex/mm/final_lease',qos)
        self.final_stop=self.create_publisher(String,'/astrex/mm/final_stop',qos)
        self.create_subscription(String,'/astrex/mobile_mvp/command',self.command,qos)
        self.create_subscription(String,'/astrex/mobile_mvp/cancel',self.cancel,qos)
        self.create_subscription(String,'/astrex/mobile_mvp/lease',self.lease,qos)
        self.create_subscription(JointState,'/astrex/mm/joint_states_raw',self.raw_state,qos)
        self.create_subscription(JointState,'/astrex/mm/joint_command_raw',self.raw_command,qos)
        self.create_subscription(TwistStamped,'/astrex/mm/collision_checked_cmd_vel',self.raw_base_command,qos)
        self.create_subscription(String,'/astrex/mm/final_status',self.final_status,qos)
        self.action_handle=None; self.phase='idle'; self.action_started=0.
        self.feedback_since=None; self.feedback_stamp=None; self.last_raw_stamp=-1.; self.action_uuid=None; self.final_seq=0; self.lease_seq=0
        self.last_heartbeat=0.; self.last_feedback=0.; self.last_feedback_emit=0.; self.action_terminal=True; self.stop_requested=None
        self.pending_outcome=None; self.pending_reason=None; self.final_state={}
        self.trajectory_hash=None; self.last_plan=None; self.last_status=0.
        self.timer=self.create_timer(.01,self.tick,clock=Clock(clock_type=ClockType.STEADY_TIME))
        self.log('startup',session=self.session,robot_config_hash=self.profile.profile_hash)

    def log(self,event,**data):
        row={'event':event,'monotonic':time.monotonic(),'execution_session':self.session,**data}
        self.log_file.write(json.dumps(row,ensure_ascii=False,sort_keys=True,allow_nan=False)+'\n')

    def emit(self,kind,command,**data):
        value=envelope(command,execution_session=self.session,**data)
        self.outputs[kind].publish(String(data=encode(value)))
        self.log(kind,**value)
        return value

    def command(self,msg):
        command=None
        try:
            command=decode(msg.data); identity(command)
            if command.get('op') not in ('arm','gripper','move'): raise Rejected('UNKNOWN_OP')
            if not isinstance(command.get('payload'),dict): raise Rejected('PAYLOAD_REQUIRED')
            if command.get('robot_config_hash') != self.profile.profile_hash: raise Rejected('ROBOT_CONFIG_MISMATCH')
            now=time.monotonic()
            # Duplicate results remain queryable even when feedback is stale.
            key=identity(command)
            if key not in self.ledger.records:
                self.state.require_fresh(now)
                if self.phase!='idle': raise Rejected('BUSY_OR_STOP_UNPROVEN')
                if (self.final_state.get('robot_config_hash')!=self.profile.profile_hash or
                    now-self.final_state.get('received',-1e20)>self.profile.state_age):
                    raise Rejected('FINAL_GATE_UNAVAILABLE_OR_CONFIG_MISMATCH')
                if not self.state.stopped(now) or not self.base_stopped(now): raise Rejected('ROBOT_NOT_STATIONARY')
                if self.moveit.inflight: raise Rejected('OLD_PLANNER_REQUEST_PENDING')
            record,fresh=self.ledger.admit(command,now)
            if not fresh:
                for kind in ('ack','result'):
                    if record[kind]: self.outputs[kind].publish(String(data=encode(record[kind])))
                self.log('duplicate',command_id=command['command_id']); return
            record['lease_until']=min(now+self.profile.lease_seconds,command['deadline_monotonic'])
            self.phase='planning' if command['op']=='arm' else 'preparing'
            self.action_handle=None; self.feedback_since=None; self.feedback_stamp=None; self.last_raw_stamp=-1.; self.action_uuid=None
            self.trajectory_hash=None; self.last_plan=None; self.stop_requested=None
            self.final_seq=0; self.lease_seq=0; self.pending_outcome=None; self.action_terminal=True
            record['ack']=self.emit('ack',command,status='accepted',physical_complete=False)
            if command['op']=='arm':
                self.moveit.plan(command['payload'],self.state,lambda plan,error:self.plan_done(key,plan,error))
            elif command['op']=='gripper': self.send_gripper(key,command['payload'])
            else: self.send_navigation(key,command['payload'])
        except Exception as exc:
            if command is not None:
                try:
                    if self.ledger.active==identity(command): self.begin_stop('failed',str(exc))
                    else: self.emit('ack',command,status='rejected',reason=str(exc),physical_complete=False)
                except Exception: self.log('malformed_command',reason=str(exc))
            else: self.log('malformed_command',reason=str(exc))

    def active(self,key):
        return self.ledger.active==key and self.stop_requested is None

    def plan_done(self,key,plan,error):
        if not self.active(key):
            self.log('late_plan_discarded',identity=list(key)); return
        try:
            if error: raise Rejected(error)
            self.trajectory_hash=self.state.validate_trajectory(plan['summary'],time.monotonic())
            self.last_plan={k:v for k,v in plan.items() if k not in ('trajectory','summary')}
            self.last_plan['trajectory_hash']=self.trajectory_hash
            self.log('plan_validated',command_id=key[0],**self.last_plan,trajectory=plan['summary'])
            if self.ledger.current()['command']['payload'].get('plan_only',False):
                self.begin_stop('succeeded','PLAN_VALID_NO_EXECUTION')
                return
            if not self.arm.server_is_ready(): raise Rejected('ARM_ACTION_UNAVAILABLE')
            goal=FollowJointTrajectory.Goal(); goal.trajectory=plan['trajectory']
            # Zero header stamp requests immediate execution; points retain timing.
            goal.trajectory.header.stamp.sec=0; goal.trajectory.header.stamp.nanosec=0
            goal.goal_time_tolerance.sec=2
            self.submit_action(self.arm,goal,key)
        except Exception as exc: self.begin_stop('failed',str(exc))

    def send_gripper(self,key,payload):
        if len(self.profile.gripper_joint_names)!=1: raise Rejected('MVP_REQUIRES_ONE_GRIPPER_DRIVE')
        name=self.profile.gripper_joint_names[0]
        position=finite(payload.get('position'),'gripper.position')
        effort=finite(payload.get('max_effort'),'gripper.max_effort')
        self.state.check_position(name,position)
        if not 0<effort<=self.profile.joint_limits[name].effort: raise Rejected('GRIPPER_EFFORT_LIMIT')
        if not self.gripper.server_is_ready(): raise Rejected('GRIPPER_ACTION_UNAVAILABLE')
        goal=ParallelGripperCommand.Goal(); goal.command.name=[name]
        goal.command.position=[position]; goal.command.effort=[effort]
        goal.command.velocity=[self.profile.raw.get('gripper_velocity',self.profile.joint_limits[name].velocity)]
        self.submit_action(self.gripper,goal,key)

    def submit_action(self,client,goal,key):
        self.phase='awaiting_action'; self.action_started=time.monotonic(); self.action_terminal=False
        future=client.send_goal_async(goal,feedback_callback=lambda msg:self.action_feedback(key,msg))
        future.add_done_callback(lambda f:self.action_accepted(key,f))

    def action_accepted(self,key,future):
        try:
            handle=future.result()
            if not self.active(key):
                if handle.accepted:
                    handle.cancel_goal_async()
                    if self.ledger.active==key:
                        handle.get_result_async().add_done_callback(lambda f:self.action_done(key,f))
                elif self.ledger.active==key: self.action_terminal=True
                self.log('late_action_discarded',identity=list(key)); return
            if not handle.accepted:
                self.action_terminal=True
                raise Rejected('ACTION_REJECTED')
            self.action_handle=handle; self.phase='running'
            self.action_uuid=bytes(handle.goal_id.uuid).hex()
            self.log('action_accepted',command_id=key[0],ros_goal_uuid=self.action_uuid)
            handle.get_result_async().add_done_callback(lambda f:self.action_done(key,f))
        except Exception as exc: self.begin_stop('failed',str(exc))

    def action_feedback(self,key,msg):
        if not self.active(key): return
        if self.feedback_since is None:
            self.feedback_since=time.monotonic()
            header=getattr(msg.feedback,'header',getattr(getattr(msg.feedback,'state',None),'header',None))
            stamped=(header.stamp.sec+header.stamp.nanosec/1e9) if header is not None else 0.
            self.feedback_stamp=max(stamped,self.state.stamp,self.get_clock().now().nanoseconds/1e9)
        self.last_feedback=time.monotonic()
        if self.last_feedback-self.last_feedback_emit<.1: return
        self.last_feedback_emit=self.last_feedback
        command=self.ledger.current()['command']
        self.emit('feedback',command,status='running',ros_goal_uuid=self.action_uuid,
                  state_sequence=self.state.sequence,trajectory_hash=self.trajectory_hash,
                  raw_source_stamp=self.state.stamp,physical_complete=False)

    def action_done(self,key,future):
        if self.ledger.active==key: self.action_terminal=True
        if not self.active(key): return
        try:
            response=future.result()
            ok=response.status==GoalStatus.STATUS_SUCCEEDED
            result=response.result
            if hasattr(result,'error_code'): ok=ok and result.error_code==0
            # Gripper stalled is only a controller observation. The independent
            # evaluator must still prove contact, lift, and placement.
            self.log('controller_result',command_id=key[0],status=response.status,
                     reached_goal=getattr(result,'reached_goal',None),stalled=getattr(result,'stalled',None))
            self.begin_stop('succeeded' if ok else 'failed',
                            'CONTROLLER_COMPLETE_PHYSICAL_EVALUATION_REQUIRED' if ok else 'CONTROLLER_FAILED')
        except Exception as exc: self.begin_stop('failed',str(exc))

    def raw_state(self,msg):
        try:
            stamp=msg.header.stamp.sec+msg.header.stamp.nanosec/1e9
            self.state.update(msg.name,msg.position,msg.velocity,stamp,time.monotonic())
        except Exception as exc:
            self.log('invalid_raw_state',reason=str(exc))
            if self.ledger.active: self.begin_stop('failed',str(exc))

    def raw_command(self,msg):
        if self.phase!='running' or self.feedback_since is None or self.stop_requested is not None: return
        now=time.monotonic()
        if now<=self.feedback_since: return
        raw_stamp=msg.header.stamp.sec+msg.header.stamp.nanosec/1e9
        if raw_stamp<=self.feedback_stamp or raw_stamp<=self.last_raw_stamp: return
        record=self.ledger.current()
        if not record or record['command']['op']=='move': return
        try:
            self.state.require_fresh(now)
            if raw_stamp>self.state.stamp+self.profile.state_age: raise Rejected('RAW_COMMAND_CLOCK_DOMAIN')
            if now>=record['lease_until']: raise Rejected('AUTHORIZATION_EXPIRED')
            if len(msg.name)!=len(msg.position) or len(set(msg.name))!=len(msg.name): raise Rejected('RAW_COMMAND_SHAPE')
            source=dict(zip(msg.name,msg.position))
            names=self.profile.arm_joint_names if record['command']['op']=='arm' else self.profile.gripper_joint_names
            values=[finite(source[name],name) for name in names]
            for name,value in zip(names,values): self.state.check_position(name,value)
            self.final_seq+=1; self.last_raw_stamp=raw_stamp
            payload=envelope(record['command'],execution_session=self.session,seq=self.final_seq,
                source_monotonic=now,source_stamp=msg.header.stamp.sec+msg.header.stamp.nanosec/1e9,
                names=names,positions=values,robot_config_hash=self.profile.profile_hash)
            self.final_command.publish(String(data=encode(payload)))
        except Exception as exc: self.begin_stop('failed',str(exc))

    def base_stopped(self,now):
        if not self.profile.raw.get('base'): return True
        return (now-self.final_state.get('received',-1e20)<=self.profile.state_age and
                self.final_state.get('base_stationary_seconds',0)>=self.profile.stop_window)

    def send_navigation(self,key,payload):
        if self.navigation is None or not self.navigation.server_is_ready():
            raise Rejected('NAVIGATION_UNAVAILABLE')
        if not self.profile.raw.get('base'): raise Rejected('BASE_NOT_CONFIGURED')
        from .moveit_adapter import pose_from_dict
        goal=NavigateToPose.Goal(); goal.pose.header.frame_id='map'
        goal.pose.header.stamp=self.get_clock().now().to_msg()
        goal.pose.pose=pose_from_dict(payload['target_pose'])
        if abs(goal.pose.pose.position.z)>1e-6: raise Rejected('NAVIGATION_REQUIRES_PLANAR_TARGET')
        # No caller-supplied behavior tree or controller plugin is accepted.
        self.submit_action(self.navigation,goal,key)

    def raw_base_command(self,msg):
        record=self.ledger.current()
        if not record or record['command']['op']!='move' or self.phase!='running' or self.feedback_since is None:
            return
        now=time.monotonic(); stamp=msg.header.stamp.sec+msg.header.stamp.nanosec/1e9
        if stamp<=self.feedback_stamp or stamp<=self.last_raw_stamp: return
        try:
            self.state.require_fresh(now)
            if stamp>self.state.stamp+self.profile.state_age: raise Rejected('BASE_COMMAND_CLOCK_DOMAIN')
            if now>=record['lease_until']: raise Rejected('BASE_LEASE_EXPIRED')
            v=finite(msg.twist.linear.x,'base.linear'); w=finite(msg.twist.angular.z,'base.angular')
            if any(abs(vv)>1e-9 for vv in (msg.twist.linear.y,msg.twist.linear.z,msg.twist.angular.x,msg.twist.angular.y)):
                raise Rejected('NON_DIFFERENTIAL_TWIST')
            base=self.profile.raw['base']
            if abs(v)>min(.15,base.get('max_linear_velocity',.15))+1e-6 or abs(w)>base.get('max_angular_velocity',.5)+1e-6:
                raise Rejected('BASE_VELOCITY_LIMIT')
            self.final_seq+=1; self.last_raw_stamp=stamp
            value=envelope(record['command'],execution_session=self.session,seq=self.final_seq,
                source_monotonic=now,source_stamp=stamp,linear=v,angular=w)
            self.base_command.publish(String(data=encode(value)))
        except Exception as exc: self.begin_stop('failed',str(exc))

    def lease(self,msg):
        try: self.ledger.renew(decode(msg.data),time.monotonic(),self.profile.lease_seconds)
        except Exception as exc: self.log('lease_rejected',reason=str(exc))

    def cancel(self,msg):
        try:
            data=decode(msg.data)
            if identity(data)!=self.ledger.active: raise Rejected('CANCEL_IDENTITY')
            self.begin_stop('canceled',data.get('reason','CANCEL_REQUESTED'))
        except Exception as exc: self.log('cancel_rejected',reason=str(exc))

    def final_status(self,msg):
        try:
            value=decode(msg.data)
            self.final_state={**value,'received':time.monotonic()}
            record=self.ledger.current()
            if (record and self.phase=='running' and all(value.get(k)==record['command'][k] for k in IDENTITY)
                and value.get('execution_session')==self.session and not value.get('active',True)
                and value.get('reason') not in ('INITIAL_HOLD','AUTHORIZED')):
                self.begin_stop('failed','FINAL_GATE:'+str(value.get('reason')))
        except Exception as exc: self.log('final_status_invalid',reason=str(exc))

    def begin_stop(self,outcome,reason):
        record=self.ledger.current()
        if not record or self.stop_requested is not None: return
        self.stop_requested=time.monotonic(); self.phase='stopping'
        self.pending_outcome,self.pending_reason=outcome,reason
        self.feedback_since=None
        self.publish_stop(record['command'],reason)
        if self.action_handle is not None: self.action_handle.cancel_goal_async()
        self.log('stop_requested',command_id=record['command']['command_id'],reason=reason,outcome=outcome)

    def publish_stop(self,command,reason):
        value=envelope(command,execution_session=self.session,reason=reason)
        self.final_stop.publish(String(data=encode(value)))

    def tick(self):
        now=time.monotonic(); record=self.ledger.current()
        if record:
            command=record['command']
            if self.stop_requested is None:
                if now>=record['lease_until']: self.begin_stop('failed','LEASE_EXPIRED')
                elif now>=command['deadline_monotonic']: self.begin_stop('failed','COMMAND_DEADLINE')
                elif now-self.state.received>self.profile.state_age: self.begin_stop('failed','RAW_STATE_STALE')
                elif self.phase=='awaiting_action' and now-self.action_started>1.: self.begin_stop('failed','ACTION_ACK_TIMEOUT')
                elif self.phase=='running' and self.feedback_since is not None and now-self.last_feedback>1.: self.begin_stop('failed','ACTION_FEEDBACK_STALE')
                elif self.phase=='running' and now-self.last_heartbeat>=.1:
                    self.lease_seq+=1; self.last_heartbeat=now
                    value=envelope(command,execution_session=self.session,seq=self.lease_seq,
                        deadline_monotonic=min(record['lease_until'],command['deadline_monotonic']),
                        robot_config_hash=self.profile.profile_hash,motion_kind=command['op'],
                        effort_limits=({self.profile.gripper_joint_names[0]:command['payload']['max_effort']} if command['op']=='gripper' else {}))
                    self.final_lease.publish(String(data=encode(value)))
            if self.stop_requested is not None:
                if now-self.last_heartbeat>=.1:
                    self.last_heartbeat=now; self.publish_stop(command,self.pending_reason)
                final_matches=(self.final_state.get('execution_session')==self.session and
                    all(self.final_state.get(k)==command[k] for k in IDENTITY) and
                    not self.final_state.get('active',True) and
                    now-self.final_state.get('received',-1e20)<=self.profile.state_age)
                if (self.action_terminal and final_matches and self.state.stopped(now) and self.base_stopped(now) and
                    self.state.still_since is not None and self.state.still_since>=self.stop_requested):
                    self.finish(True)
                elif (self.action_terminal and final_matches and self.state.stopped(now) and self.base_stopped(now) and now-self.stop_requested>=self.profile.stop_window):
                    # Already stationary when cancellation was requested: fresh
                    # samples throughout the post-stop window still prove hold.
                    self.finish(True)
                elif now-self.stop_requested>5. and self.phase!='blocked':
                    self.phase='blocked'
                    self.emit('feedback',command,status='blocked',reason='STOP_NOT_PROVEN',physical_complete=False)
        if now-self.last_status>=.5:
            self.last_status=now
            status={'schema':SCHEMA,'execution_session':self.session,'phase':self.phase,
                'raw_state_age':None if not math.isfinite(self.state.received) else now-self.state.received,
                'state_sequence':self.state.sequence,'robot_config_hash':self.profile.profile_hash,
                'moveit_inflight':self.moveit.inflight,'action_uuid':self.action_uuid,
                'ready_for_command':(self.phase=='idle' and not self.moveit.inflight and self.state.stopped(now) and self.base_stopped(now)
                    and self.final_state.get('robot_config_hash')==self.profile.profile_hash
                    and now-self.final_state.get('received',-1e20)<=self.profile.state_age)}
            self.outputs['status'].publish(String(data=encode(status)))

    def finish(self,stopped):
        command=self.ledger.current()['command']
        evidence={'schema':SCHEMA,'command_id':command['command_id'],'execution_session':self.session,
            'stop_requested_monotonic':self.stop_requested,'proven_monotonic':time.monotonic(),
            'raw_source_stamp':self.state.stamp,'state_sequence':self.state.sequence,
            'q':self.state.positions,'dq':self.state.velocities,'final_gate':self.final_state,
            'stopped':stopped,'window_seconds':self.profile.stop_window}
        path=self.evidence/('stop_'+uuid.uuid4().hex+'.json')
        path.write_text(json.dumps(evidence,indent=2,allow_nan=False)+'\n')
        result=self.emit('result',command,status=self.pending_outcome,reason=self.pending_reason,
            ros_goal_uuid=self.action_uuid,trajectory_hash=self.trajectory_hash,
            stop_evidence={'command_id':command['command_id'],'stopped':True,
                           'source':'isaac_raw_joint_feedback','reference':str(path)},
            physical_complete=False,scope=('plan_only' if command['payload'].get('plan_only',False) else 'controller_stage'),plan=self.last_plan)
        self.ledger.finish(result); self.phase='idle'; self.action_handle=None
        self.feedback_since=None; self.stop_requested=None

    def close(self):
        if self.ledger.current(): self.publish_stop(self.ledger.current()['command'],'GATEWAY_SHUTDOWN')
        self.log('shutdown'); self.log_file.close()

def main(args=None):
    rclpy.init(args=args); node=None
    try:
        node=Gateway(); rclpy.spin(node)
    except KeyboardInterrupt: pass
    finally:
        if node is not None: node.close(); node.destroy_node()
        if rclpy.ok(): rclpy.shutdown()

if __name__=='__main__': main()
