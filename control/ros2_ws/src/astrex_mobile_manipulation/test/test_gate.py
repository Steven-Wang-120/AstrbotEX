import json
import math
import unittest
from astrex_mobile_manipulation.core import (RobotProfile,StateCache,FinalGateCore,
    CommandLedger,Rejected,SCHEMA,decode,identity,support_contact_updates,base_stop_measurement,execution_timing_status,encode)


def profile():
    return RobotProfile.from_dict({'arm_joint_names':['a','b'],'gripper_joint_names':['g'],
        'joint_limits':{n:{'lower':-2,'upper':2,'velocity':1,'acceleration':2,'effort':4} for n in ('a','b','g')}})

def command(i='one',epoch=1):
    return {'schema':SCHEMA,'command_id':i,'ex_session':'ex','goal_revision':1,
        'execution_epoch':epoch,'deadline_monotonic':20,'op':'arm','payload':{}}

class Gates(unittest.TestCase):
    def setUp(self):
        self.p=profile(); self.q={'a':0.,'b':0.,'g':.2}; self.dq=dict.fromkeys(self.q,0.)

    def test_uninitialized_gate_status_is_valid_json_without_changing_deadline(self):
        gate=FinalGateCore(self.p)
        status=json.loads(encode({'schema':SCHEMA,**execution_timing_status(gate,1.)}))
        self.assertIsNone(status['lease_until_monotonic'])
        self.assertIsNone(status['last_target_received_monotonic'])
        self.assertEqual(gate.lease_until,-math.inf)
        lease={**command(),'execution_session':'gw','seq':0,'robot_config_hash':self.p.profile_hash}
        gate.grant(lease,1.)
        status=json.loads(encode({'schema':SCHEMA,**execution_timing_status(gate,1.1)}))
        self.assertEqual(status['lease_until_monotonic'],gate.lease_until)

    def test_duplicate_cannot_reexecute_or_change_payload(self):
        ledger=CommandLedger(); c=command()
        record,fresh=ledger.admit(c,1); self.assertTrue(fresh)
        ledger.finish({'status':'succeeded'})
        again,fresh=ledger.admit(c,2); self.assertFalse(fresh)
        self.assertEqual(again['result']['status'],'succeeded'); self.assertIsNone(ledger.active)
        with self.assertRaisesRegex(Rejected,'CONFLICT'): ledger.admit({**c,'payload':{'x':1}},2)
        with self.assertRaisesRegex(Rejected,'OLD_EPOCH'): ledger.admit(command('new',1),2)

    def test_cached_stamp_does_not_refresh_state(self):
        state=StateCache(self.p); names=list(self.q)
        state.update(names,[0,0,.2],[0,0,0],1.,1.)
        self.assertFalse(state.update(names,[0,0,.2],[0,0,0],1.,1.19))
        with self.assertRaisesRegex(Rejected,'STALE'): state.require_fresh(1.21)
        with self.assertRaisesRegex(Rejected,'REVERSED'): state.update(names,[0,0,.2],[0,0,0],.5,1.3)
        with self.assertRaisesRegex(Rejected,'RESET'): state.require_fresh(1.31)

    def test_stop_window_resets_on_feedback_gap(self):
        state=StateCache(self.p)
        for i in range(7): state.update(list(self.q),[0,0,.2],[0,0,0],i/10,i/10)
        self.assertTrue(state.stopped(.6))
        state.update(list(self.q),[0,0,.2],[0,0,0],1.,1.)
        self.assertFalse(state.stopped(1.))

    def test_lease_expiry_stops_even_if_old_jtc_targets_continue(self):
        gate=FinalGateCore(self.p); c=command()
        lease={**c,'execution_session':'gw','seq':0,'robot_config_hash':self.p.profile_hash}
        gate.tick(1.,0.,self.q,self.dq); gate.grant(lease,1.)
        target={**c,'execution_session':'gw','seq':1,'source_monotonic':1.1,'names':['a'],'positions':[.4]}
        gate.receive(target,1.1)
        self.assertEqual(gate.tick(1.2,.2,self.q,self.dq)['a'],.4)
        self.assertEqual(gate.tick(1.51,.3,self.q,self.dq)['a'],0.)
        self.assertFalse(gate.active)
        with self.assertRaises(Rejected): gate.receive({**target,'seq':2,'source_monotonic':1.52},1.52)
        with self.assertRaisesRegex(Rejected,'CLOSED'): gate.grant({**lease,'seq':1},1.53)

    def test_lease_and_command_replays_cannot_refresh_validity(self):
        gate=FinalGateCore(self.p); c=command()
        lease={**c,'execution_session':'gw','seq':0,'robot_config_hash':self.p.profile_hash}
        gate.grant(lease,1.)
        with self.assertRaisesRegex(Rejected,'REPLAY'): gate.grant(lease,1.2)
        target={**c,'execution_session':'gw','seq':1,'source_monotonic':1.1,'names':['a'],'positions':[.2]}
        gate.receive(target,1.1)
        with self.assertRaisesRegex(Rejected,'REPLAY'): gate.receive(target,1.15)
        with self.assertRaisesRegex(Rejected,'STALE'): gate.receive({**target,'seq':2},1.4)
        with self.assertRaisesRegex(Rejected,'LIMIT'): gate.receive({**target,'seq':2,'source_monotonic':1.2,'positions':[3.]},1.2)

    def test_new_command_cannot_relabel_old_raw_target(self):
        gate=FinalGateCore(self.p); c=command()
        lease={**c,'execution_session':'gw','seq':0,'robot_config_hash':self.p.profile_hash}
        gate.grant(lease,1.); gate.stop('CANCEL',self.q)
        gate.grant({**lease,**command('two',2),'seq':0},1.1)
        with self.assertRaisesRegex(Rejected,'IDENTITY'):
            gate.receive({**c,'execution_session':'gw','seq':1,'source_monotonic':1.2,'names':['a'],'positions':[.5]},1.2)
        self.assertEqual(gate.targets,{})

    def test_sim_reset_latches_closed(self):
        gate=FinalGateCore(self.p); c=command()
        gate.tick(1.,5.,self.q,self.dq)
        gate.grant({**c,'execution_session':'gw','seq':0,'robot_config_hash':self.p.profile_hash},1.)
        gate.tick(1.1,0.,self.q,self.dq)
        self.assertFalse(gate.active)
        with self.assertRaisesRegex(Rejected,'RESET'):
            gate.grant({**command('next',2),'execution_session':'gw','seq':0,'robot_config_hash':self.p.profile_hash},1.2)

    def test_unparameterized_or_wrong_start_trajectory_rejected(self):
        state=StateCache(self.p); state.update(list(self.q),[0,0,.2],[0,0,0],1.,1.)
        point={'positions':[0,0],'velocities':[0,0],'accelerations':[0,0],'time_from_start':0.}
        traj={'joint_names':['a','b'],'points':[point,{**point,'positions':[.1,.1],'time_from_start':1.}]}
        self.assertTrue(state.validate_trajectory(traj,1.1))
        for bad in ({**point,'velocities':[]},{**point,'positions':[1,0]},{**point,'accelerations':[float('nan'),0]}):
            with self.assertRaises(Rejected): state.validate_trajectory({**traj,'points':[bad,traj['points'][1]]},1.1)

    def test_gripper_clamp_survives_arm_hold_but_explicit_open_releases(self):
        gate=FinalGateCore(self.p); gate.tick(1.,1.,self.q,self.dq)
        lease={**command(),'execution_session':'gw','seq':0,'robot_config_hash':self.p.profile_hash,'motion_kind':'gripper'}
        gate.grant(lease,1.)
        gate.receive({**lease,'seq':1,'source_monotonic':1.1,'names':['g'],'positions':[.5]},1.1)
        gate.stop('CONTROLLER_DONE',self.q)
        self.assertEqual(gate.hold['g'],.5)
        gate.grant({**lease,**command('arm',2),'motion_kind':'arm','seq':0},1.2)
        gate.stop('CANCEL',self.q)
        self.assertEqual(gate.hold['g'],.5)
        gate.grant({**lease,**command('open',3),'seq':0},1.3)
        gate.receive({**lease,**command('open',3),'seq':1,'source_monotonic':1.4,'names':['g'],'positions':[0.]},1.4)
        gate.stop('CANCEL',self.q)
        self.assertEqual(gate.hold['g'],.2)

    def test_base_uses_same_lease_and_rejects_arm_authorization(self):
        self.p.raw['base']={'max_linear_velocity':.15,'max_angular_velocity':.4}
        gate=FinalGateCore(self.p); gate.tick(1.,1.,self.q,self.dq)
        lease={**command(),'execution_session':'gw','seq':0,'robot_config_hash':self.p.profile_hash,'motion_kind':'move'}
        gate.grant(lease,1.)
        gate.receive_base({**lease,'seq':1,'source_monotonic':1.1,'linear':.1,'angular':.2},1.1)
        self.assertEqual(gate.base_twist,(.1,.2))
        with self.assertRaises(Rejected): gate.receive_base({**lease,'seq':2,'source_monotonic':1.2,'linear':.3,'angular':0.},1.2)
        gate.tick(1.6,1.6,self.q,self.dq)
        self.assertEqual(gate.base_twist,(0.,0.))
        with self.assertRaises(Rejected): gate.receive_base({**lease,'seq':3,'source_monotonic':1.7,'linear':.1,'angular':0.},1.7)

    def test_base_stop_requires_every_wheel_not_averages(self):
        # Each side averages to zero, but its wheels physically turn.
        measurement=base_stop_measurement({'lf':.2,'lr':-.2,'rf':.2,'rr':-.2},.12,[0.,0.,0.],[0.,0.,0.])
        self.assertFalse(measurement['stationary'])
        self.assertAlmostEqual(measurement['max_wheel_surface_speed_m_s'],.024)
        self.assertEqual(len(measurement['wheel_surface_speeds_m_s']),4)

    def test_base_stop_rejects_sliding_and_full_vector_rotation(self):
        wheels=dict.fromkeys(('lf','lr','rf','rr'),0.)
        for linear,angular in [([.021,0.,0.],[0.,0.,0.]),([.015,.015,0.],[0.,0.,0.]),
                               ([0.,0.,0.],[.051,0.,0.]),([0.,0.,0.],[.04,.04,0.])]:
            with self.subTest(linear=linear,angular=angular):
                self.assertFalse(base_stop_measurement(wheels,.12,linear,angular)['stationary'])
        stopped=base_stop_measurement(wheels,.12,[.01,0.,0.],[0.,0.,.03])
        self.assertTrue(stopped['stationary'])
        self.assertEqual(stopped['velocity_source'],'physx_articulation_root')

    def test_base_stop_missing_or_invalid_physics_feedback_cannot_pass(self):
        for wheels,linear,angular in [({},[0.,0.,0.],[0.,0.,0.]),
            ({'lf':0.},None,[0.,0.,0.]),({'lf':0.},[0.,0.,0.],[float('nan'),0.,0.]),
            ({'lf':float('inf')},[0.,0.,0.],[0.,0.,0.])]:
            with self.subTest(wheels=wheels,linear=linear,angular=angular),self.assertRaises(Rejected):
                base_stop_measurement(wheels,.12,linear,angular)

    def test_controller_stream_hang_stops_despite_valid_renewals(self):
        gate=FinalGateCore(self.p); gate.tick(1.,1.,self.q,self.dq)
        lease={**command(),'execution_session':'gw','seq':0,'robot_config_hash':self.p.profile_hash}
        gate.grant(lease,1.)
        gate.receive({**lease,'seq':1,'source_monotonic':1.1,'names':['a'],'positions':[.1]},1.1)
        gate.grant({**lease,'seq':1},1.3)
        gate.tick(1.31,1.31,self.q,self.dq)
        self.assertFalse(gate.active)
        self.assertEqual(gate.reason,'COMMAND_STREAM_STALE')

    def test_effort_cap_cannot_exceed_profile(self):
        gate=FinalGateCore(self.p)
        lease={**command(),'execution_session':'gw','seq':0,'robot_config_hash':self.p.profile_hash,'effort_limits':{'g':2.}}
        gate.grant(lease,1.)
        self.assertEqual(gate.effort_limits['g'],2.)
        with self.assertRaisesRegex(Rejected,'EFFORT_LIMIT'): gate.grant({**lease,'seq':1,'effort_limits':{'g':5.}},1.1)
        self.assertEqual(gate.effort_limits['g'],2.)

    def test_support_contacts_are_whitelisted_and_reset_each_phase(self):
        self.p.raw['allowed_support_contacts']=[['red_cube','table'],['red_cube','place_region']]
        lift={'support_contacts':[{'object_id':'red_cube','support_id':'table','allowed':True}]}
        self.assertEqual(support_contact_updates(lift,self.p),[('red_cube','table',True),('red_cube','place_region',False)])
        self.assertEqual(support_contact_updates({},self.p),[('red_cube','table',False),('red_cube','place_region',False)])
        for row in ({'object_id':'a','support_id':'table','allowed':True},
                    {'object_id':'red_cube','support_id':'wall','allowed':True},
                    {'object_id':'red_cube','support_id':'table','allowed':1}):
            with self.assertRaises(Rejected):support_contact_updates({'support_contacts':[row]},self.p)

    def test_gripper_physics_velocity_cannot_exceed_profile(self):
        raw=dict(self.p.raw)
        self.assertEqual(RobotProfile.from_dict({**raw,'gripper_velocity':.3}).raw['gripper_velocity'],.3)
        for value in (0.,1.01,float('nan'),True):
            with self.assertRaises(Rejected):RobotProfile.from_dict({**raw,'gripper_velocity':value})

    def test_wire_size_and_nonfinite(self):
        with self.assertRaises(Rejected): decode('{"schema":"astrex.mm.v1","v":NaN}')
        with self.assertRaises(Rejected): decode(' '*65537)

if __name__=='__main__': unittest.main()
