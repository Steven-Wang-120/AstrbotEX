"""Evidence tests: a Nav2 result is not physical navigation success."""
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts/lib'))
sys.path.insert(0, str(ROOT/'sim'))
from mobile_navigation_checks import (NavigationEvidence, summarize_navigation_layouts,
                                      verify_simulation_configuration)
from mobile_manipulation.spec import obstacle_layout

PROFILE = {'base':{'wheel_joints':{'left':['l1','l2'],'right':['r1','r2']},
                   'wheel_radius':.12,'wheel_separation':.64}}
PARAMS = {'target_region_id':'home','position_tolerance_m':.1,
          'yaw_tolerance_rad':math.radians(10),'success_template':'move.arrive_and_stop.v1'}


def state(seq, stamp, x=0., wheel=0., contacts=()):
    return {'sim_session':'sim_1','seq':seq,'source_stamp':stamp,
        'pose_source':'PhysX_articulation_link_transforms',
        'base_pose':{'position':[x,0,.23],'orientation':[0,0,0,1],'frame_id':'world'},
        'wheel_joint_velocities':{n:wheel for n in ('l1','l2','r1','r2')},
        'trial':{'unexpected_contacts':list(contacts),'protective_failure':False},
        'guard_active':False}


class NavigationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.evidence=NavigationEvidence(PROFILE,{'home':[0,0,0]},self.tmp.name)
        self.evidence.bind(PARAMS)

    def tearDown(self):
        self.evidence.close()
        self.tmp.cleanup()

    def test_nav2_success_outside_goal_fails(self):
        self.evidence.sample(state(1,0,x=1.))
        pending=self.evidence.evaluate({'command_id':'cmd'},[{'status':'succeeded'}],state(2,.1,x=1.))
        self.assertFalse(pending['physical_complete'])
        result=self.evidence.evaluate({'command_id':'cmd'},[{'status':'succeeded'}],state(3,3.2,x=1.))
        self.assertTrue(result['physical_complete'])
        self.assertFalse(result['success'])

    def test_success_requires_continuous_physical_stop(self):
        for seq,stamp in enumerate((i/10 for i in range(13)),1):
            self.evidence.sample(state(seq,stamp))
        result=self.evidence.evaluate({'command_id':'cmd'},[{'status':'succeeded'}],state(13,1.2))
        self.assertTrue(result['success'])
        # A single resumed wheel motion invalidates the current continuous window.
        self.evidence.sample(state(14,1.3,wheel=1.))
        self.assertFalse(self.evidence.last_metrics['physical_success'])
        self.assertEqual(self.evidence.last_metrics['continuous_hold_sim_seconds'],0.)

    def test_zero_wheel_feedback_does_not_hide_physical_drift(self):
        self.evidence.sample(state(1,0,x=0.))
        self.evidence.sample(state(2,.1,x=.01))
        self.assertFalse(self.evidence.last_metrics['stopped'])
        self.assertAlmostEqual(self.evidence.last_metrics['physical_linear_speed_m_s'],.1)

    def test_collision_is_retained_after_contact_disappears(self):
        self.evidence.sample(state(1,0,contacts=[['robot/base','obstacle']]))
        for seq,stamp in enumerate((.1,.7,1.2),2):
            self.evidence.sample(state(seq,stamp))
        self.assertFalse(self.evidence.last_metrics['physical_success'])
        self.assertEqual(len(self.evidence.contacts),1)

    def test_missing_interval_does_not_count_as_continuous_stop(self):
        self.evidence.sample(state(1,0))
        self.evidence.sample(state(2,.1))
        self.evidence.sample(state(3,1.2))
        self.assertFalse(self.evidence.last_metrics['physical_success'])
        self.assertEqual(self.evidence.hold_gap_resets,1)

    def test_missing_wheel_feedback_is_rejected(self):
        value=state(1,0)
        del value['wheel_joint_velocities']['r2']
        with self.assertRaisesRegex(ValueError,'WHEEL_VELOCITY_MISSING'):
            self.evidence.sample(value)


class NavigationLayoutTests(unittest.TestCase):
    def test_optional_random_layout_is_deterministic_and_does_not_change_p1(self):
        baseline=obstacle_layout()
        config={'navigation_obstacles':{'seed':7301,'xy_jitter_m':.08}}
        a=obstacle_layout(config)
        self.assertEqual(a,obstacle_layout(config))
        self.assertNotEqual(a,baseline)
        self.assertEqual(obstacle_layout(),baseline)
        self.assertEqual(baseline[0]['position'],[-.8,-.3,.25])


def write_actual_config(path, actual):
    value=copy.deepcopy(actual)
    canonical={k:v for k,v in value.items() if k not in
               {'config_hash','pid','started_wall_ns'}}
    value['config_hash']=hashlib.sha256(json.dumps(
        canonical,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    path.write_text(json.dumps(value)+'\n')
    return value


class NavigationConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'simulation_config.json'
        self.declared={
            'navigation':{'layout_seed':7301},
            'robot':{'mobile':True,'sensor_parent_prim':'/World/Robot/base_link'},
            'scene':{'navigation_obstacles':{'seed':7301,'xy_jitter_m':.08,
                'additional':[{'position':[-.65,-1.25,.25],'dimensions':[.2,.2,.5]}]}},
            'execution':{'profile_path':str(Path(self.tmp.name)/'profile.json'),'evaluation':{}},
            'sensors':{'calibration_id':'mobile_rig_v1'}}
        self.actual={'mode':'control','scene_seed':7301,
            'sensor_profile_id':'RGB_LIDAR_2D','sensor_rendering':True,
            'lidar':{'configuration':'Example_Rotary_2D'},
            'robot_usd':'/assets/vendor.usd','pid':12345,'started_wall_ns':11,
            **{key:copy.deepcopy(self.declared[key]) for key in
               ('robot','scene','execution','sensors')}}
        self.actual=write_actual_config(self.path,self.actual)
        self.observation={'sim_session':'sim_7301','simulator_pid':12345,
            'effective_config_hash':self.actual['config_hash'],'profile_hash':'profilehash'}

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, declared=None, observation=None):
        return verify_simulation_configuration(self.path,declared or self.declared,
            self.declared['execution']['profile_path'],'profilehash','/assets/vendor.usd',
            observation or self.observation)

    def test_live_configuration_is_bound_to_pid_hash_session_and_profile(self):
        value=self.verify()
        self.assertEqual(value['actual_layout_seed'],7301)
        self.assertEqual(value['simulator_pid'],12345)
        self.assertEqual(value['sim_session'],'sim_7301')
        self.assertEqual(value['simulation_config_sha256'],
                         hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_wrong_declared_seed_and_changed_obstacle_geometry_are_rejected(self):
        changed=copy.deepcopy(self.declared)
        changed['navigation']['layout_seed']=7302
        with self.assertRaisesRegex(ValueError,'LAYOUT_SEED_MISMATCH'):
            self.verify(declared=changed)
        changed=copy.deepcopy(self.declared)
        changed['scene']['navigation_obstacles']['additional'][0]['dimensions'][0]=.5
        with self.assertRaisesRegex(ValueError,'CONFIGURATION_MISMATCH:scene'):
            self.verify(declared=changed)

    def test_other_process_or_late_previous_simulator_identity_is_rejected(self):
        for key,value in [('simulator_pid',12346),('effective_config_hash','old_hash'),
                          ('profile_hash','other_robot')]:
            with self.subTest(key=key):
                changed={**self.observation,key:value}
                with self.assertRaisesRegex(ValueError,'IDENTITY_MISMATCH'):
                    self.verify(observation=changed)

    def test_disabled_sensor_rendering_is_not_navigation_sensor_evidence(self):
        self.actual['sensor_rendering']=False
        self.actual=write_actual_config(self.path,self.actual)
        observation={**self.observation,'effective_config_hash':self.actual['config_hash']}
        with self.assertRaisesRegex(ValueError,'REQUIRES_NATIVE_2D_LIDAR'):
            self.verify(observation=observation)


class NavigationAggregationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, seed, session):
        path=Path(self.tmp.name)/session/'simulation_config.json'
        path.parent.mkdir(exist_ok=True)
        profile=str(Path(self.tmp.name)/'profile.json')
        declared={'navigation':{'layout_seed':seed},'robot':{'mobile':True},
            'scene':{'navigation_obstacles':{'seed':seed,'xy_jitter_m':.08,
                'additional':[{'position':[-.65,-1.25,.25],'dimensions':[.2,.2,.5]}]}},
            'execution':{'profile_path':profile,'evaluation':{}},'sensors':{}}
        actual={'mode':'control','scene_seed':seed,'sensor_profile_id':'RGB_LIDAR_2D',
            'sensor_rendering':True,'lidar':{'configuration':'Example_Rotary_2D'},
            'robot_usd':'/assets/vendor.usd','pid':10000+seed,'started_wall_ns':11,
            **{k:copy.deepcopy(declared[k]) for k in ('robot','scene','execution','sensors')}}
        actual=write_actual_config(path,actual)
        observation={'sim_session':session,'simulator_pid':actual['pid'],
            'effective_config_hash':actual['config_hash'],'profile_hash':'profilehash'}
        evidence=verify_simulation_configuration(
            path,declared,profile,'profilehash','/assets/vendor.usd',observation)
        return {'scope':'M1_NAVIGATION_ONLY_ONE_SEEDED_DEVELOPMENT_LAYOUT',
                'layout_seed':seed,'sim_session':session,'independent_layouts':1,
                'started_cases':1,'successes':1,'all_passed':True,
                'configuration_evidence':evidence}

    def test_three_distinct_layouts_and_sessions_pass(self):
        rows=[self.row(seed,'session_'+str(seed)) for seed in (7301,7302,7303)]
        self.assertTrue(summarize_navigation_layouts(rows)['m1_acceptance_complete'])

    def test_three_routes_in_one_layout_do_not_pass_stage(self):
        rows=[self.row(7301,'one_session') for _ in range(3)]
        result=summarize_navigation_layouts(rows)
        self.assertFalse(result['all_passed'])
        self.assertEqual(result['independent_layouts'],1)

    def test_missing_or_failed_layout_does_not_pass_stage(self):
        rows=[self.row(seed,'session_'+str(seed)) for seed in (7301,7302)]
        self.assertFalse(summarize_navigation_layouts(rows)['all_passed'])
        rows.append(self.row(7303,'session_7303'))
        rows[-1]['all_passed']=False
        self.assertFalse(summarize_navigation_layouts(rows)['all_passed'])

    def test_three_declared_seeds_cannot_relabel_one_real_layout(self):
        rows=[self.row(seed,'session_'+str(seed)) for seed in (7301,7302,7303)]
        rows[1]['configuration_evidence']=copy.deepcopy(rows[0]['configuration_evidence'])
        result=summarize_navigation_layouts(rows)
        self.assertFalse(result['all_passed'])
        self.assertTrue(any('SUMMARY_LAYOUT_IDENTITY_MISMATCH' in e for e in result['errors']))

    def test_launch_file_mutation_after_capture_is_detected(self):
        rows=[self.row(seed,'session_'+str(seed)) for seed in (7301,7302,7303)]
        path=Path(rows[0]['configuration_evidence']['simulation_config_path'])
        path.write_text(path.read_text()+' ')
        result=summarize_navigation_layouts(rows)
        self.assertFalse(result['all_passed'])
        self.assertTrue(any('EVIDENCE_CHANGED' in e for e in result['errors']))


if __name__=='__main__':
    unittest.main()
