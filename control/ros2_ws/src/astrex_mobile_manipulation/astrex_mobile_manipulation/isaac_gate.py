"""Isaac hook. Import after SimulationApp and the native ROS bridge are ready.

Uses the simulator's rclpy node and SingleArticulation; never sources a system
ROS overlay into Isaac. All physical writes happen from the simulation thread.
"""
from __future__ import annotations
import time
import uuid
import math
from .core import RobotProfile, FinalGateCore, Rejected, decode, encode, SCHEMA, identity, base_stop_measurement, execution_timing_status
from .test_faults import LocalFaultControl

class IsaacGate:
    def __init__(self,world,config):
        import numpy as np
        from sensor_msgs.msg import JointState
        from std_msgs.msg import String
        from nav_msgs.msg import Odometry
        from geometry_msgs.msg import TransformStamped
        from tf2_msgs.msg import TFMessage
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        self.np=np; self.JointState=JointState; self.String=String
        self.Odometry=Odometry; self.TransformStamped=TransformStamped; self.TFMessage=TFMessage
        self.node=config['ros_node']; self.robot=config['robot']; self.world=world
        self.profile=(RobotProfile.load(config['profile_path']) if 'profile_path' in config
                      else RobotProfile.from_dict(config['profile']))
        self.core=FinalGateCore(self.profile)
        self.test_faults=LocalFaultControl(config.get('local_test_faults'),self.profile.profile_hash)
        self.isaac_session=uuid.uuid4().hex
        names=list(self.robot.dof_names)
        self.indices=[names.index(n) for n in self.profile.names]
        self.all_names=names
        self.applied_effort_limits=None
        # The simulator drive enforces maximum effort in position mode. The
        # message's optional effort field is never used as a substitute.
        self.controller=self.robot.get_articulation_controller()
        initial=self.profile.raw.get('initial_joint_positions')
        if initial is not None:
            if len(initial)!=len(self.indices): raise ValueError('initial_joint_positions dimension')
            for name,value in zip(self.profile.names,initial):
                limit=self.profile.joint_limits[name]
                if not math.isfinite(value) or not limit.lower<=value<=limit.upper:
                    raise ValueError('invalid initial joint: '+name)
            self.robot.set_joint_positions(np.asarray(initial,dtype=float),joint_indices=np.asarray(self.indices))
            self.robot.set_joint_velocities(np.zeros(len(self.indices)),joint_indices=np.asarray(self.indices))
            from isaacsim.core.utils.types import ArticulationAction
            self.robot.apply_action(ArticulationAction(joint_positions=np.asarray(initial,dtype=float),joint_indices=np.asarray(self.indices)))
        if 'arm_stiffness' in self.profile.raw:
            kp=list(self.profile.raw['arm_stiffness'])+[self.profile.raw['gripper_stiffness']]
            kd=list(self.profile.raw['arm_damping'])+[self.profile.raw['gripper_damping']]
            all_kp,all_kd=self.controller.get_gains()
            all_kp=np.asarray(all_kp).copy(); all_kd=np.asarray(all_kd).copy()
            all_kp[self.indices]=kp; all_kd[self.indices]=kd
            self.controller.set_gains(kps=all_kp,kds=all_kd)
            actual_kp,actual_kd=self.controller.get_gains()
            if not np.allclose(np.asarray(actual_kp)[self.indices],kp) or not np.allclose(np.asarray(actual_kd)[self.indices],kd):
                raise RuntimeError('Isaac drive gains readback failed')
        self.controller.set_max_efforts(np.asarray([self.profile.joint_limits[n].effort for n in self.profile.names]),
                                       joint_indices=np.asarray(self.indices))
        # ParallelGripperCommand's position-only hardware does not consume its
        # optional max-velocity field. Enforce actual DOF speed in PhysX.
        self.drive_velocity_limits={n:self.profile.joint_limits[n].velocity for n in self.profile.names}
        for name in self.profile.gripper_joint_names:
            self.drive_velocity_limits[name]=self.profile.raw.get('gripper_velocity',self.profile.joint_limits[name].velocity)
        velocity_values=np.asarray([[self.drive_velocity_limits[n] for n in self.profile.names]],dtype=float)
        view=self.robot._articulation_view
        view.set_max_joint_velocities(velocity_values,joint_indices=np.asarray(self.indices))
        actual_velocity_limits=view.get_joint_max_velocities(joint_indices=np.asarray(self.indices))
        if not np.allclose(np.asarray(actual_velocity_limits),velocity_values,rtol=1e-5,atol=1e-6):
            raise RuntimeError('Isaac DOF velocity limit readback failed')
        qos=QoSProfile(depth=10,reliability=ReliabilityPolicy.RELIABLE,durability=DurabilityPolicy.VOLATILE)
        self.base=self.profile.raw.get('base')
        self.base_since=None; self.base_velocity=(0.,0.); self.base_pose=[0.,0.,0.]
        self.base_stop_measurement=None
        self.wheel_indices=[]; self.odom=None; self.tf=None
        if self.base:
            self.base_pose=list(self.base.get('initial_pose',[0.,0.,0.]))
            self.base_link_height=float(self.base.get('base_link_height',self.profile.raw.get('robot_source',{}).get('base',{}).get('height',0.)))
            self.left_names=list(self.base['wheel_joints']['left']); self.right_names=list(self.base['wheel_joints']['right'])
            self.wheel_names=self.left_names+self.right_names
            self.wheel_indices=[names.index(n) for n in self.wheel_names]
            if not self.left_names or not self.right_names or self.base['wheel_radius']<=0 or self.base['wheel_separation']<=0:
                raise ValueError('invalid differential base geometry')
            self.controller.set_max_efforts(np.full(len(self.wheel_indices),self.base['wheel_effort_limit']),joint_indices=np.asarray(self.wheel_indices))
            wheel_efforts=self.controller.get_max_efforts()
            if not np.allclose(np.asarray(wheel_efforts)[self.wheel_indices],self.base['wheel_effort_limit']):
                raise RuntimeError('wheel drive effort limit readback failed')
            for i in self.wheel_indices: self.controller.switch_dof_control_mode(i,'velocity')
            self.odom=self.node.create_publisher(Odometry,'/astrex/mm/odom',qos)
            self.tf=self.node.create_publisher(TFMessage,'/tf',qos)
        self.raw=self.node.create_publisher(JointState,'/astrex/mm/joint_states_raw',qos)
        self.status=self.node.create_publisher(String,'/astrex/mm/final_status',qos)
        self.subscriptions=[
            self.node.create_subscription(String,'/astrex/mm/final_lease',self.lease,qos),
            self.node.create_subscription(String,'/astrex/mm/guarded_joint_command',self.command,qos),
            self.node.create_subscription(String,'/astrex/mm/guarded_base_command',self.base_command,qos),
            self.node.create_subscription(String,'/astrex/mm/final_stop',self.stop,qos)]
        self.q={}; self.dq={}; self.last_raw=0.; self.last_raw_sim=-math.inf; self.error=None
        self.closed=False; self.shutdown_stop=False

    def lease(self,msg):
        try: self.core.grant(decode(msg.data),time.monotonic())
        except Exception as exc:
            self.error=str(exc)
            # An old or unrelated lease cannot prolong or replace the active one.

    def command(self,msg):
        try:
            value=decode(msg.data)
            if identity(value)!=self.core.key or value.get('execution_session')!=self.core.gateway_session: return
            self.core.receive(value,time.monotonic())
        except Exception as exc:
            self.error=str(exc)
            if str(exc) not in ('FINAL_COMMAND_REPLAY','FINAL_NO_LEASE'):
                self.core.stop('INVALID_COMMAND:'+str(exc),self.q)

    def base_command(self,msg):
        try:
            value=decode(msg.data)
            if identity(value)!=self.core.key or value.get('execution_session')!=self.core.gateway_session: return
            self.core.receive_base(value,time.monotonic())
        except Exception as exc:
            self.error=str(exc)
            if str(exc) not in ('BASE_REPLAY','BASE_NOT_AUTHORIZED'):
                self.core.stop('INVALID_BASE_COMMAND:'+str(exc),self.q)

    def stop(self,msg):
        try:
            value=decode(msg.data); key=identity(value)
            session=value.get('execution_session')
            if not isinstance(session,str) or not session: raise Rejected('STOP_SESSION')
            if self.core.active and (self.core.key!=key or self.core.gateway_session!=session):
                raise Rejected('STOP_IDENTITY')
            if not self.core.active:
                self.core.key=key; self.core.gateway_session=session
            self.core.stop(value.get('reason','STOP'),self.q)
        except Exception as exc: self.error=str(exc)

    def step(self,sim_time,dt):
        if self.closed: return
        from isaacsim.core.utils.types import ArticulationAction
        now=time.monotonic()
        positions=self.robot.get_joint_positions(); velocities=self.robot.get_joint_velocities()
        self.q={name:float(positions[i]) for name,i in zip(self.profile.names,self.indices)}
        self.dq={name:float(velocities[i]) for name,i in zip(self.profile.names,self.indices)}
        targets=self.core.tick(now,sim_time,self.q,self.dq)
        limits=tuple(self.core.effort_limits[n] for n in self.profile.names)
        if limits!=self.applied_effort_limits:
            self.controller.set_max_efforts(self.np.asarray(limits),joint_indices=self.np.asarray(self.indices))
            actual=self.controller.get_max_efforts()
            if actual is None or any(abs(float(actual[i])-limit)>1e-4 for i,limit in zip(self.indices,limits)):
                self.core.stop('DRIVE_LIMIT_NOT_APPLIED',self.q)
                raise RuntimeError('Isaac drive effort limit readback failed')
            self.applied_effort_limits=limits
        if targets:
            self.robot.apply_action(ArticulationAction(
                joint_positions=self.np.asarray([targets[n] for n in self.profile.names]),
                joint_velocities=self.np.zeros(len(self.indices)),joint_indices=self.np.asarray(self.indices)))
        if self.base:
            radius=self.base['wheel_radius']; separation=self.base['wheel_separation']
            left=sum(float(velocities[self.all_names.index(n)]) for n in self.left_names)/len(self.left_names)
            right=sum(float(velocities[self.all_names.index(n)]) for n in self.right_names)/len(self.right_names)
            v=radius*(right+left)/2.; w=radius*(right-left)/separation
            self.base_velocity=(v,w)
            yaw=self.base_pose[2]
            self.base_pose[0]+=v*math.cos(yaw+w*dt/2.)*dt
            self.base_pose[1]+=v*math.sin(yaw+w*dt/2.)*dt
            self.base_pose[2]+=w*dt
            try:
                # SingleArticulation getters read PhysX root velocities. Wheel
                # odometry remains separate and cannot establish a stop alone.
                linear=self.robot.get_linear_velocity()
                angular=self.robot.get_angular_velocity()
                self.base_stop_measurement=base_stop_measurement(
                    {n:float(velocities[i]) for n,i in zip(self.wheel_names,self.wheel_indices)},radius,
                    None if linear is None else [float(v) for v in linear],
                    None if angular is None else [float(v) for v in angular])
                if self.base_stop_measurement['stationary']:
                    if self.base_since is None: self.base_since=now
                else: self.base_since=None
            except Exception as exc:
                self.base_since=None; self.base_stop_measurement=None
                self.error='BASE_STOP_FEEDBACK:'+str(exc)
                self.core.stop(self.error,self.q)
            cv,cw=self.core.base_twist if self.core.active and self.core.motion_kind=='move' else (0.,0.)
            speed_left=(cv-cw*separation/2.)/radius; speed_right=(cv+cw*separation/2.)/radius
            self.robot.apply_action(ArticulationAction(joint_velocities=self.np.asarray(
                [speed_left]*len(self.left_names)+[speed_right]*len(self.right_names)),joint_indices=self.np.asarray(self.wheel_indices)))
        # State is read from articulation physics, not from controller targets.
        raw_paused=self.test_faults.poll(now,self.isaac_session,self.core.key)
        publish_state=sim_time>self.last_raw_sim and now-self.last_raw>=.01
        if publish_state:
            self.last_raw=now; self.last_raw_sim=sim_time; msg=self.JointState()
            sec=int(sim_time); msg.header.stamp.sec=sec
            msg.header.stamp.nanosec=int(round((sim_time-sec)*1e9))
            if msg.header.stamp.nanosec>=1000000000:
                msg.header.stamp.sec+=1; msg.header.stamp.nanosec=0
            msg.name=list(self.all_names)
            msg.position=[float(v) for v in positions]; msg.velocity=[float(v) for v in velocities]
            # Position hardware exports position/velocity only. Applied effort
            # commands are not physical drive-output measurements, so leave
            # JointState.effort empty rather than imply torque was measured.
            if raw_paused:
                self.test_faults.suppressed_messages+=1
            else:
                self.raw.publish(msg)
            if self.base:
                odom=self.Odometry(); odom.header.stamp=msg.header.stamp
                odom.header.frame_id='odom'; odom.child_frame_id='base_link'
                odom.pose.pose.position.x,odom.pose.pose.position.y=self.base_pose[:2]
                odom.pose.pose.position.z=self.base_link_height
                odom.pose.pose.orientation.z=math.sin(self.base_pose[2]/2.)
                odom.pose.pose.orientation.w=math.cos(self.base_pose[2]/2.)
                odom.twist.twist.linear.x,odom.twist.twist.angular.z=self.base_velocity
                odom.pose.covariance[0]=.001; odom.pose.covariance[7]=.001; odom.pose.covariance[35]=.002
                self.odom.publish(odom)
                transform=self.TransformStamped(); transform.header=odom.header; transform.child_frame_id='base_link'
                transform.transform.translation.x,transform.transform.translation.y=self.base_pose[:2]
                transform.transform.translation.z=self.base_link_height
                transform.transform.rotation=odom.pose.pose.orientation
                self.tf.publish(self.TFMessage(transforms=[transform]))
        # Use the same newly read PhysX sample as raw state. A separate 50 ms
        # throttle can skip a fresh frame immediately before a long render.
        if publish_state:
            identity_fields=dict(zip(('command_id','ex_session','goal_revision','execution_epoch'),self.core.key or (None,None,None,None)))
            status={'schema':SCHEMA,**identity_fields,'execution_session':self.core.gateway_session,
                'isaac_session':self.isaac_session,'active':self.core.active,'reason':self.core.reason,
                'robot_config_hash':self.profile.profile_hash,'source_stamp':sim_time,
                **execution_timing_status(self.core,now),
                'local_test_fault':self.test_faults.status(raw_paused),
                'drive_effort_limits':self.core.effort_limits,
                'drive_velocity_limits':self.drive_velocity_limits,
                'effort_measurement_semantics':'drive_output_unmeasured_configuration_limits_read_back',
                'stop_count':self.core.stop_count,'error':self.error,
                'base_velocity':list(self.base_velocity),'base_stationary_seconds':(now-self.base_since if self.base_since is not None else 0.),
                'base_odom_source':'wheel_feedback' if self.base else None,
                'base_stop_measurement':self.base_stop_measurement}
            self.status.publish(self.String(data=encode(status)))

    def close(self):
        self.core.stop('ISAAC_HOOK_CLOSE',self.q)
        self.closed=True
        for subscription in self.subscriptions: self.node.destroy_subscription(subscription)
        self.node.destroy_publisher(self.raw); self.node.destroy_publisher(self.status)
        if self.odom: self.node.destroy_publisher(self.odom)
        if self.tf: self.node.destroy_publisher(self.tf)

def install(world,config):
    return IsaacGate(world,config)
