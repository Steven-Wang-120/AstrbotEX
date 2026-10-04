"""Pure regression checks for trial identity and physical success evidence."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/lib'))
from mobile_trial_evidence import (TruthIdentity,matches_trial,update_place_window,
    fetch_physical_decision,write_decision_evidence,finalize_trial,trial_run_summary,truth_sample_due)

BINDING={'sim_session':'sim-a','profile_hash':'profile-a','after_seq':4,
    'trial_id':'trial_1','scene_seed':101,'task_kind':'PLACE'}
PARAMS={'placement_tolerance_m':.02}


def truth(seq=5,**overrides):
    value={'schema':'astrex.mm.truth.v1','sim_session':'sim-a','profile_hash':'profile-a',
        'seq':seq,'source_stamp':seq/10,'published_monotonic':9.95,
        'trial':{k:BINDING[k] for k in ('trial_id','scene_seed','task_kind')}}
    value.update(overrides)
    return value


def physical(**overrides):
    value={'sim_session':'sim-a','config_hash':'profile-a','seq':9,
        'trial_id':'trial_1','scene_seed':101,'task_kind':'PLACE','grasp_success':True,
        'channel_success':True,'current_place_valid':True,'current_place_hold_seconds':1.1,
        'within_region':True,'placement_xy_error_m':.01,'physical_success':True,
        'unexpected_contacts':[],'protective_failure':False,'drop_detected':False}
    value.update(overrides)
    return value


def row(**overrides):
    value={'case':{k:BINDING[k] for k in ('trial_id','scene_seed','task_kind')},
        'runtime':{'terminal':True,'succeeded':True},'physical':physical(),
        'errors':[],'trial_begun':True}
    value.update(overrides)
    return value


class TruthPublicationScheduleTests(unittest.TestCase):
    def test_slow_simulation_can_sample_new_physics_before_sim_period(self):
        self.assertTrue(truth_sample_due(1.01,10.06,1.,10.))
        self.assertFalse(truth_sample_due(1.01,10.01,1.,10.))

    def test_fast_simulation_keeps_original_sim_period(self):
        self.assertTrue(truth_sample_due(1.06,10.01,1.,10.))
        self.assertTrue(truth_sample_due(0.,10.,-float('inf'),-float('inf')))

    def test_pause_duplicate_or_reversed_stamp_cannot_make_heartbeat(self):
        self.assertFalse(truth_sample_due(1.,100.,1.,10.))
        self.assertFalse(truth_sample_due(.99,100.,1.,10.))

    def test_invalid_current_time_cannot_request_snapshot(self):
        for sim,wall in [(float('nan'),10.),(1.,float('inf'))]:
            with self.assertRaisesRegex(ValueError,'INVALID_TRUTH_SAMPLE_TIME'):
                truth_sample_due(sim,wall,0.,0.)


class TrialIdentityTests(unittest.TestCase):
    def test_old_sequence_cannot_replace_truth(self):
        reader=TruthIdentity('profile-a')
        accepted=reader.accept(truth(),10.)
        self.assertEqual(accepted['seq'],5)
        self.assertIsNone(reader.accept(truth(4),10.))
        self.assertIsNone(reader.accept(truth(5),10.))
        self.assertEqual(reader.seq,5)
        self.assertEqual(reader.accept(truth(6),10.)['seq'],6)

    def test_session_change_requires_new_reader_and_latches_error(self):
        reader=TruthIdentity('profile-a');reader.accept(truth(),10.)
        with self.assertRaisesRegex(RuntimeError,'SIM_SESSION_CHANGED_REBUILD_READER'):
            reader.accept(truth(6,sim_session='sim-b'),10.)
        with self.assertRaisesRegex(RuntimeError,'SIM_SESSION_CHANGED_REBUILD_READER'):
            reader.accept(truth(7),10.)

    def test_profile_and_clock_regression_rejected(self):
        with self.assertRaisesRegex(RuntimeError,'TRUTH_PROFILE_MISMATCH'):
            TruthIdentity('profile-a').accept(truth(profile_hash='profile-b'),10.)
        reader=TruthIdentity('profile-a');reader.accept(truth(),10.)
        with self.assertRaisesRegex(RuntimeError,'TRUTH_TIME_REGRESSED'):
            reader.accept(truth(6,source_stamp=.1),10.)

    def test_queued_stale_frame_does_not_lock_session(self):
        reader=TruthIdentity('profile-a')
        self.assertIsNone(reader.accept(truth(published_monotonic=9.),10.))
        self.assertIsNone(reader.session)

    def test_old_trial_session_seed_profile_and_begin_sequence_rejected(self):
        self.assertTrue(matches_trial(truth(),BINDING))
        cases=[truth(4),truth(sim_session='sim-b'),truth(profile_hash='profile-b')]
        for key,value in [('trial_id','old_trial'),('scene_seed',102),('task_kind','G')]:
            changed=truth();changed['trial'][key]=value;cases.append(changed)
        for candidate in cases:
            with self.subTest(candidate=candidate):self.assertFalse(matches_trial(candidate,BINDING))


class ContinuousPlacementTests(unittest.TestCase):
    def test_previous_hold_cannot_pass_after_motion_interrupts_stability(self):
        value=physical(place_stable_since=None,place_hold_seconds=0.)
        update_place_window(value,1.,True);update_place_window(value,2.1,True)
        self.assertEqual(fetch_physical_decision(PARAMS,value)[:2],(True,True))
        update_place_window(value,2.2,False)
        self.assertGreater(value['place_hold_seconds'],1.)
        self.assertEqual(value['current_place_hold_seconds'],0.)
        update_place_window(value,2.3,True)
        self.assertEqual(fetch_physical_decision(PARAMS,value)[:2],(False,False))
        update_place_window(value,3.4,True)
        self.assertEqual(fetch_physical_decision(PARAMS,value)[:2],(True,True))

    def test_recoverable_unsettled_waits_but_collision_fails(self):
        self.assertEqual(fetch_physical_decision(PARAMS,physical(current_place_valid=False))[:2],(False,False))
        self.assertEqual(fetch_physical_decision(PARAMS,physical(unexpected_contacts=['arm/table']))[:2],(True,False))


class ImmutableDecisionEvidenceTests(unittest.TestCase):
    def test_real_file_exists_before_evaluator_can_return_success(self):
        command={'command_id':'cmd','ex_session':'ex-session','goal_revision':2,'params':PARAMS}
        evaluation={'physical_complete':True,'success':True}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(write_decision_evidence(directory,command,truth(),evaluation))
            record=json.loads(path.read_text())
            self.assertEqual(record['observation']['seq'],5)
            self.assertEqual(record['observation']['trial']['trial_id'],'trial_1')
            self.assertEqual(record['command_id'],'cmd')
            self.assertEqual(str(path),write_decision_evidence(directory,command,truth(),evaluation))
            path.write_text('corrupt')
            with self.assertRaisesRegex(RuntimeError,'PHYSICAL_EVIDENCE_CONFLICT'):
                write_decision_evidence(directory,command,truth(),evaluation)


class FinalAccountingTests(unittest.TestCase):
    def test_end_or_close_failure_never_counts_verified_success_or_exit_zero(self):
        for failure in ('physical_end:timeout','close:stop_timeout'):
            value=finalize_trial(row(errors=[failure]),BINDING,PARAMS)
            self.assertTrue(value['ledger_succeeded'])
            self.assertFalse(value['verified_fetch_success'])
            self.assertFalse(value['research_success'])
            summary,code=trial_run_summary('fetch',[value],[value['case']])
            self.assertEqual(code,1)
            self.assertFalse(summary['all_required_passed'])

    def test_final_result_requires_current_identity_and_new_sequence(self):
        for fields in ({'sim_session':'old'},{'trial_id':'old'},{'scene_seed':102},
                       {'config_hash':'old'},{'seq':4}):
            with self.subTest(fields=fields):
                value=finalize_trial(row(physical=physical(**fields)),BINDING,PARAMS)
                self.assertFalse(value['record_complete'])
                self.assertFalse(value['verified_fetch_success'])

    def test_ledger_success_requires_final_physical_success(self):
        value=finalize_trial(row(physical=physical(current_place_hold_seconds=.1)),BINDING,PARAMS)
        self.assertTrue(value['record_complete'])
        self.assertTrue(value['ledger_succeeded'])
        self.assertFalse(value['verified_fetch_success'])
        self.assertEqual(trial_run_summary('fetch',[value],[value['case']])[1],1)
        value=finalize_trial(row(),BINDING,PARAMS)
        self.assertEqual(trial_run_summary('fetch',[value],[value['case']])[1],0)

    def test_e0_fifteen_records_and_one_research_success_per_kind_suffice(self):
        cases=[];rows=[]
        for kind in ('G','PLACE','C'):
            for index in range(5):
                case={'trial_id':kind+'_'+str(index),'task_kind':kind,'scene_seed':101+index}
                cases.append(case)
                rows.append({'case':case,'trial_begun':True,'record_complete':True,
                    'research_success':index==0,'ledger_succeeded':False,'verified_fetch_success':False})
        summary,code=trial_run_summary('e0',rows,cases)
        self.assertEqual(code,0)
        self.assertEqual(summary['started_trials'],15)
        self.assertEqual(summary['record_rate'],1.)
        self.assertEqual(summary['physical_successes'],3)
        self.assertEqual(summary['verified_fetch_successes'],0)
        self.assertEqual(trial_run_summary('e0',rows[:-1],cases)[1],1)
        rows[0]['research_success']=False
        self.assertEqual(trial_run_summary('e0',rows,cases)[1],1)


    def test_e0_partial_or_unpaired_matrix_cannot_claim_complete(self):
        cases=[{'trial_id':kind+'_'+str(index),'task_kind':kind,'scene_seed':101+index}
               for kind in ('G','PLACE','C') for index in range(5)]
        variants=[cases[::5],copy.deepcopy(cases),copy.deepcopy(cases),copy.deepcopy(cases)]
        variants[1][-1]['scene_seed']=999
        variants[2][-1]['scene_seed']=103
        variants[3][-1]['trial_id']=variants[3][0]['trial_id']
        for candidate in variants:
            rows=[{'case':case,'trial_begun':True,'record_complete':True,'research_success':True,
                   'ledger_succeeded':True,'verified_fetch_success':True} for case in candidate]
            with self.subTest(cases=candidate):
                summary,code=trial_run_summary('e0',rows,candidate)
                self.assertEqual(code,1)
                self.assertFalse(summary['complete_paired_matrix'])
                self.assertFalse(summary['all_required_passed'])


if __name__=='__main__':unittest.main()
