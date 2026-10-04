"""Synchronous trial helper over the same bounded gateway protocol.

This does not bypass the gateway, create a second executor for an EX plugin,
claim physical success, or publish a robot actuator command.
"""
import time
import uuid
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from std_msgs.msg import String
from .core import SCHEMA, RobotProfile, encode, decode, envelope, identity

class GatewayClient(Node):
    def __init__(self,profile_path,session=None):
        super().__init__('mm_trial_client_'+uuid.uuid4().hex[:8])
        self.profile=RobotProfile.load(profile_path)
        self.session=session or 'trial_'+uuid.uuid4().hex
        self.epoch=0; self.current=None; self.ack=None; self.result=None; self.status={}; self.feedback=[]
        qos=QoSProfile(depth=10,reliability=ReliabilityPolicy.RELIABLE,durability=DurabilityPolicy.VOLATILE)
        self.command_publishers={name:self.create_publisher(String,'/astrex/mobile_mvp/'+name,qos) for name in ('command','lease','cancel')}
        for name in ('ack','result','feedback','status'):
            self.create_subscription(String,'/astrex/mobile_mvp/'+name,lambda msg,n=name:self.receive(n,msg),qos)

    def receive(self,name,msg):
        value=decode(msg.data)
        if name=='status': self.status=value; return
        if self.current is None or identity(value)!=identity(self.current):
            return
        if name=='feedback': self.feedback.append(value)
        else: setattr(self,name,value)

    def wait_ready(self,timeout=120.):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            rclpy.spin_once(self,timeout_sec=.05)
            age=self.status.get('raw_state_age')
            if (self.command_publishers['command'].get_subscription_count()>0 and self.status.get('ready_for_command',False) and
                isinstance(age,(int,float)) and age<=self.profile.state_age): return self.status
        raise TimeoutError('Gateway did not obtain fresh Isaac feedback')

    def execute(self,op,payload,command_id=None,timeout=60.,renew=True,cancel_after=None,cancel_when=None,lease_until=None):
        self.epoch+=1
        now=time.monotonic()
        command={'schema':SCHEMA,'command_id':command_id or uuid.uuid4().hex,'ex_session':self.session,
            'goal_revision':1,'execution_epoch':self.epoch,'deadline_monotonic':now+timeout,
            'robot_config_hash':self.profile.profile_hash,'op':op,'payload':payload}
        self.current=command; self.ack=None; self.result=None; self.feedback=[]
        self.command_publishers['command'].publish(String(data=encode(command)))
        last_lease=now; seq=0; canceled=False; ack_deadline=now+1.
        while time.monotonic()<command['deadline_monotonic']+6.:
            current=time.monotonic()
            if renew and (lease_until is None or lease_until(self)) and current-last_lease>=.1:
                seq+=1; last_lease=current
                self.command_publishers['lease'].publish(String(data=encode(envelope(command,seq=seq))))
            if not canceled and ((cancel_after is not None and current-now>=cancel_after) or (cancel_when is not None and cancel_when(self)) or current>=command['deadline_monotonic']):
                canceled=True
                self.command_publishers['cancel'].publish(String(data=encode(envelope(command,reason='TRIAL_CANCEL'))))
            rclpy.spin_once(self,timeout_sec=.01)
            if self.result is not None: return {'ack':self.ack,'result':self.result,'feedback':self.feedback}
            if self.ack is not None and self.ack.get('status')=='rejected':
                return {'ack':self.ack,'result':None,'feedback':self.feedback}
            if self.ack is None and current>=ack_deadline and not canceled:
                canceled=True
                self.command_publishers['cancel'].publish(String(data=encode(envelope(command,reason='ACK_TIMEOUT'))))
        return {'ack':self.ack,'result':None,'feedback':self.feedback,'error':'TERMINAL_OR_STOP_UNCONFIRMED'}
