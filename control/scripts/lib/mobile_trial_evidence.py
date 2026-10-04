"""Pure identity, continuous stability and final trial accounting rules."""
import hashlib
import json
import math
from pathlib import Path


def truth_sample_due(sim_time, wall_time, last_sim_time, last_wall_time):
    """Request a new physics snapshot; never authorize a cached heartbeat.

    The caller runs on a physics callback and must read current PhysX values.
    Either clock can make sampling due, but simulation time must advance.
    This removes sampler delay; it cannot remove a blocked physics/render step.
    """
    if not math.isfinite(sim_time) or not math.isfinite(wall_time):
        raise ValueError('INVALID_TRUTH_SAMPLE_TIME')
    if sim_time<=last_sim_time:
        return False
    return sim_time-last_sim_time>=.05 or wall_time-last_wall_time>=.05


class TruthIdentity:
    def __init__(self, profile_hash=None):
        self.profile_hash=profile_hash
        self.session=None
        self.seq=-1
        self.stamp=-math.inf
        self.error=None

    def accept(self, value, now):
        if self.error:raise RuntimeError(self.error)
        if value.get('schema')!='astrex.mm.truth.v1':return None
        session=value.get('sim_session');seq=value.get('seq');stamp=value.get('source_stamp')
        if not isinstance(session,str) or not session or type(seq) is not int or seq<0:
            raise ValueError('INVALID_TRUTH_IDENTITY')
        if type(stamp) not in (int,float) or not math.isfinite(stamp):raise ValueError('INVALID_TRUTH_TIME')
        if self.session is not None and session!=self.session:
            self.error='SIM_SESSION_CHANGED_REBUILD_READER';raise RuntimeError(self.error)
        if self.profile_hash and value.get('profile_hash')!=self.profile_hash:
            self.error='TRUTH_PROFILE_MISMATCH';raise RuntimeError(self.error)
        if seq<=self.seq:return None
        if stamp<self.stamp:
            self.error='TRUTH_TIME_REGRESSED';raise RuntimeError(self.error)
        published=value.get('published_monotonic')
        if type(published) not in (int,float) or not math.isfinite(published) or not 0<=now-published<=.2:
            return None
        self.session=session;self.seq=seq;self.stamp=stamp
        return {**value,'received_monotonic':now}


def matches_trial(observation,binding):
    trial=observation.get('trial')
    return (isinstance(trial,dict) and observation.get('sim_session')==binding['sim_session']
        and observation.get('profile_hash')==binding['profile_hash']
        and observation.get('seq',-1)>binding['after_seq']
        and all(trial.get(key)==binding[key] for key in ('trial_id','scene_seed','task_kind')))


def update_place_window(trial, sim_time, placed):
    if placed:
        if trial['place_stable_since'] is None:trial['place_stable_since']=sim_time
        current=max(0.,sim_time-trial['place_stable_since'])
        trial['place_hold_seconds']=max(trial['place_hold_seconds'],current)
    else:
        trial['place_stable_since']=None
        current=0.
    trial['current_place_hold_seconds']=current


def fetch_physical_decision(params,trial):
    """Complete fatal failures now; wait for a recoverable settling window."""
    if trial.get('unexpected_contacts') or trial.get('protective_failure') or trial.get('drop_detected'):
        return True,False,'PHYSICAL_FAILURE'
    if not trial.get('grasp_success'):
        return True,False,'GRASP_EVIDENCE_MISSING'
    if trial.get('task_kind')=='C' and not trial.get('channel_success'):
        return True,False,'CHANNEL_EVIDENCE_MISSING'
    if not trial.get('current_place_valid') or trial.get('current_place_hold_seconds',0)<1.:
        return False,False,'WAITING_CONTINUOUS_PLACE_STABILITY'
    success=(trial.get('within_region') is True and
             trial.get('placement_xy_error_m',math.inf)<=params['placement_tolerance_m'])
    return True,bool(success),'PHYSICAL_SUCCESS' if success else 'PLACEMENT_TOLERANCE_FAILED'


def write_decision_evidence(directory, command, observation, evaluation):
    """directory is trusted composition, never a path taken from observations."""
    directory=Path(directory)/'physical_decisions';directory.mkdir(parents=True,exist_ok=True)
    body=json.dumps({'command_id':command['command_id'],'ex_session':command['ex_session'],
        'goal_revision':command['goal_revision'],'parameters':command['params'],
        'observation':observation,'evaluation':evaluation},sort_keys=True,ensure_ascii=False,allow_nan=False,indent=2)+'\n'
    suffix=hashlib.sha256(body.encode()).hexdigest()
    path=directory/('observation_'+str(observation['seq'])+'_'+suffix[:16]+'.json')
    try:
        with path.open('x') as stream:
            stream.write(body)
            stream.flush()
            import os
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_text()!=body:raise RuntimeError('PHYSICAL_EVIDENCE_CONFLICT')
    return str(path)


def finalize_trial(row,binding,params):
    """Keep Ledger facts, per-task research metrics and verified fetch separate."""
    runtime=row.get('runtime') or {};physical=row.get('physical')
    row['ledger_succeeded']=bool(runtime.get('succeeded'))
    identity=bool(binding and isinstance(physical,dict) and
        physical.get('sim_session')==binding['sim_session'] and physical.get('config_hash')==binding['profile_hash'] and
        type(physical.get('seq')) is int and physical['seq']>binding['after_seq'] and
        all(physical.get(k)==binding[k] for k in ('trial_id','scene_seed','task_kind')))
    row['record_complete']=bool(identity and runtime.get('terminal') and not row['errors'])
    complete,success,_=fetch_physical_decision(params,physical) if identity else (False,False,'IDENTITY_MISMATCH')
    row['verified_fetch_success']=bool(row['record_complete'] and row['ledger_succeeded'] and complete and success)
    row['full_fetch_success']=row['verified_fetch_success']
    row['research_success']=bool(row['record_complete'] and physical.get('physical_success'))
    if row['ledger_succeeded'] and not row['verified_fetch_success']:
        row['verification_failure']='LEDGER_SUCCESS_NOT_CONFIRMED_BY_FINAL_PHYSICAL_RESULT'
    return row


def trial_run_summary(mode,rows,cases):
    kinds=('G','PLACE','C')
    by_kind={kind:{'successes':sum(r.get('research_success',False) for r in rows if r['case']['task_kind']==kind),
                  'planned':sum(c['task_kind']==kind for c in cases),
                  'recorded':sum(r.get('record_complete',False) for r in rows if r['case']['task_kind']==kind)} for kind in kinds}
    all_records=(len(rows)==len(cases) and all(r.get('record_complete') for r in rows)
        and sorted(r['case']['trial_id'] for r in rows)==sorted(c['trial_id'] for c in cases))
    paired_seeds={kind:{c['scene_seed'] for c in cases if c['task_kind']==kind} for kind in kinds}
    complete_e0_matrix=(len(cases)==15 and len({c['trial_id'] for c in cases})==15
        and all(by_kind[k]['planned']==5 and len(paired_seeds[k])==5 for k in kinds)
        and paired_seeds['G']==paired_seeds['PLACE']==paired_seeds['C'])
    passed=(complete_e0_matrix and all_records and all(by_kind[k]['successes']>=1 for k in kinds)) if mode=='e0' else (
        all_records and len(rows)==1 and rows[0].get('verified_fetch_success',False))
    summary={'results':rows,'planned_trials':len(cases),'started_trials':sum(r.get('trial_begun',False) for r in rows),
        'complete_records':sum(r.get('record_complete',False) for r in rows),'record_rate':sum(r.get('record_complete',False) for r in rows)/len(cases) if cases else 0.,
        'physical_successes':sum(r.get('research_success',False) for r in rows),
        'formal_fetch_successes':sum(r.get('ledger_succeeded',False) for r in rows),
        'verified_fetch_successes':sum(r.get('verified_fetch_success',False) for r in rows),
        'by_task_kind':by_kind,'all_required_passed':bool(passed)}
    if mode=='e0':summary['complete_paired_matrix']=complete_e0_matrix
    return summary,0 if passed else 1
