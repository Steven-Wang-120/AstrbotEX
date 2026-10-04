"""M1 navigation through formal EX Goals and independent PhysX evidence.

Each process runs one seeded development layout. M1 requires three distinct
layout runs. These do not count toward the final ten frozen physical scenarios.
The evaluation subscriber cannot issue wheel commands or reposition the robot.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import threading
import time


def yaw_of(quaternion):
    x, y, z, w = quaternion
    return math.atan2(2 * (w*z + x*y), 1 - 2 * (y*y + z*z))


def angle_difference(a, b):
    return math.atan2(math.sin(a-b), math.cos(a-b))



def verify_simulation_configuration(path, declared, profile_path, profile_hash, robot_asset, observation):
    """Bind the live simulator identity to its actual launch configuration."""
    path = Path(path).resolve()
    content = path.read_bytes()
    actual = json.loads(content)
    seed = declared['navigation']['layout_seed']
    canonical = {key:value for key,value in actual.items()
                 if key not in {'config_hash','pid','started_wall_ns'}}
    calculated = hashlib.sha256(json.dumps(canonical, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    if actual.get('config_hash') != calculated:
        raise ValueError('SIMULATION_CONFIGURATION_HASH_INVALID')
    if actual.get('mode') != 'control' or actual.get('robot', {}).get('mobile') is not True:
        raise ValueError('SIMULATION_NOT_MOBILE_CONTROL')
    if (actual.get('sensor_profile_id') != 'RGB_LIDAR_2D'
        or actual.get('sensor_rendering', True) is not True or not actual.get('lidar')):
        raise ValueError('M1_REQUIRES_NATIVE_2D_LIDAR')
    if (actual.get('scene_seed') != seed
        or actual.get('scene', {}).get('navigation_obstacles', {}).get('seed') != seed):
        raise ValueError('ACTUAL_SIMULATION_LAYOUT_SEED_MISMATCH')
    for key in ('scene','robot','execution'):
        if actual.get(key) != declared.get(key, {}):
            raise ValueError('ACTUAL_SIMULATION_CONFIGURATION_MISMATCH:'+key)
    for key,value in declared.get('sensors', {}).items():
        if actual.get('sensors', {}).get(key) != value:
            raise ValueError('ACTUAL_SIMULATION_SENSOR_MISMATCH:'+key)
    actual_profile = actual.get('execution', {}).get('profile_path')
    if not actual_profile or Path(actual_profile).resolve() != Path(profile_path).resolve():
        raise ValueError('ACTUAL_SIMULATION_PROFILE_PATH_MISMATCH')
    if not robot_asset or actual.get('robot_usd') != robot_asset:
        raise ValueError('ACTUAL_SIMULATION_ROBOT_ASSET_MISMATCH')
    if (type(actual.get('pid')) is not int or actual['pid'] <= 0
        or observation.get('simulator_pid') != actual['pid']
        or observation.get('effective_config_hash') != calculated
        or observation.get('profile_hash') != profile_hash
        or not observation.get('sim_session')):
        raise ValueError('LIVE_SIMULATOR_CONFIGURATION_IDENTITY_MISMATCH')
    observed = {key:observation[key] for key in
        ('sim_session','simulator_pid','effective_config_hash','profile_hash')}
    return {'simulation_config_path':str(path),
        'simulation_config_sha256':hashlib.sha256(content).hexdigest(),
        'effective_config_hash':calculated,'actual_layout_seed':seed,
        'simulator_pid':actual['pid'],'sim_session':observation['sim_session'],
        'profile_path':str(Path(profile_path).resolve()),'profile_hash':profile_hash,
        'robot_asset':robot_asset, 'declared_configuration':declared,
        'observed_truth_identity':observed,
        'binding_source':'live_ROS_evaluation_truth_matched_to_simulator_launch_file'}


def recheck_configuration_evidence(evidence):
    """Detect changed launch files or mismatched labels during final aggregation."""
    checked = verify_simulation_configuration(
        evidence['simulation_config_path'], evidence['declared_configuration'],
        evidence['profile_path'], evidence['profile_hash'], evidence['robot_asset'],
        evidence['observed_truth_identity'])
    if checked != evidence:
        raise ValueError('SIMULATION_CONFIGURATION_EVIDENCE_CHANGED')
    return checked


class NavigationEvidence:
    def __init__(self, profile, regions, output, *, hold_seconds=1., simulator_binding=None):
        self.lock = threading.RLock()
        self.profile = profile
        self.simulator_binding = simulator_binding
        self.regions = regions
        self.output = Path(output)
        self.hold_seconds = hold_seconds
        self.previous = None
        self.last_metrics = {}
        self.hold_since = None
        self.evaluation_since = None
        self.session = None
        self.command_id = None
        self.target_region_id = None
        self.parameters = None
        self.sample_count = 0
        self.hold_gap_resets = 0
        self.errors = set()
        self.contacts = set()
        self.stream = (self.output/'navigation_samples.jsonl').open('w')
        self.reference = str(self.output/'navigation_physical.json')

    def bind(self, parameters):
        self.parameters = dict(parameters)
        self.target_region_id = parameters['target_region_id']

    def sample(self, state):
        with self.lock:
            return self._sample(state)

    def _sample(self, state):
        if self.parameters is None:
            raise ValueError('NAVIGATION_PARAMETERS_NOT_BOUND')
        if self.simulator_binding is not None:
            binding=self.simulator_binding
            if (state.get('simulator_pid') != binding['simulator_pid']
                or state.get('effective_config_hash') != binding['effective_config_hash']
                or state.get('sim_session') != binding['sim_session']
                or state.get('profile_hash') != binding['profile_hash']):
                raise ValueError('LIVE_SIMULATOR_CONFIGURATION_IDENTITY_CHANGED')
        if self.session is None:
            self.session = state['sim_session']
        if state['sim_session'] != self.session:
            self.errors.add('SIMULATION_SESSION_CHANGED')
        stamp = state['source_stamp']
        previous = self.previous
        if previous and state['seq'] == previous['seq']:
            return self.last_metrics
        if previous and stamp-previous['source_stamp'] > .2:
            self.hold_since = None
            self.hold_gap_resets += 1
        if previous and (state['seq'] < previous['seq'] or stamp <= previous['source_stamp']):
            self.errors.add('SIMULATION_FEEDBACK_REVERSED')
        if state.get('pose_source') != 'PhysX_articulation_link_transforms':
            self.errors.add('BASE_TRUTH_SOURCE_UNVERIFIED')
        base = state['base_pose']
        if base is None or base.get('frame_id') != 'world':
            raise ValueError('PHYSICAL_BASE_POSE_MISSING')
        x, y = base['position'][:2]
        yaw = yaw_of(base['orientation'])
        goal_x, goal_y, goal_yaw = self.regions[self.target_region_id]
        position_error = math.hypot(x-goal_x, y-goal_y)
        yaw_error = abs(angle_difference(yaw, goal_yaw))
        wheels = self.profile['base']['wheel_joints']
        expected_wheels = wheels['left'] + wheels['right']
        velocities = state.get('wheel_joint_velocities', {})
        if any(name not in velocities for name in expected_wheels):
            raise ValueError('PHYSICAL_WHEEL_VELOCITY_MISSING')
        values = [float(velocities[name]) for name in expected_wheels]
        if not all(math.isfinite(v) for v in values):
            raise ValueError('NONFINITE_WHEEL_FEEDBACK')
        radius = self.profile['base']['wheel_radius']
        separation = self.profile['base']['wheel_separation']
        left = sum(velocities[n] for n in wheels['left'])/len(wheels['left'])
        right = sum(velocities[n] for n in wheels['right'])/len(wheels['right'])
        wheel_surface_speed = max(abs(v)*radius for v in values)
        wheel_angular_speed = abs(radius*(right-left)/separation)
        linear_speed = angular_speed = None
        if previous and stamp > previous['source_stamp']:
            old = previous['base_pose']
            delta = stamp - previous['source_stamp']
            linear_speed = math.hypot(x-old['position'][0], y-old['position'][1])/delta
            angular_speed = abs(angle_difference(yaw, yaw_of(old['orientation'])))/delta
        trial = state.get('trial') or {}
        for pair in trial.get('unexpected_contacts', []):
            self.contacts.add(tuple(pair))
        if trial.get('protective_failure'):
            self.errors.add('PROTECTIVE_FAILURE')
        finite_pose = all(math.isfinite(v) for v in [x,y,yaw,position_error,yaw_error])
        stopped = (linear_speed is not None and linear_speed <= .02 and angular_speed <= .05
                   and wheel_surface_speed <= .02 and wheel_angular_speed <= .05)
        arrived = (finite_pose and position_error <= self.parameters['position_tolerance_m']
                   and yaw_error <= self.parameters['yaw_tolerance_rad'])
        clear = not self.contacts and not self.errors
        if arrived and stopped and clear:
            if self.hold_since is None:
                self.hold_since = stamp
        else:
            self.hold_since = None
        hold = 0. if self.hold_since is None else stamp-self.hold_since
        self.last_metrics = {
            'source_stamp':stamp, 'seq':state['seq'], 'base_pose':base,
            'position_error_m':position_error, 'yaw_error_rad':yaw_error,
            'physical_linear_speed_m_s':linear_speed, 'physical_angular_speed_rad_s':angular_speed,
            'max_wheel_surface_speed_m_s':wheel_surface_speed,
            'wheel_angular_speed_rad_s':wheel_angular_speed,
            'wheel_joint_velocities':velocities, 'arrived':arrived, 'stopped':stopped,
            'clear':clear, 'continuous_hold_sim_seconds':hold,
            'physical_success':bool(arrived and stopped and clear and hold >= self.hold_seconds),
            'guard_active':state['guard_active'],
        }
        self.previous = state
        self.sample_count += 1
        self.stream.write(json.dumps(self.last_metrics, allow_nan=False)+'\n')
        return self.last_metrics

    def summary(self):
        return {
            'scope':'M1_DEVELOPMENT_NAVIGATION_ONLY',
            'input_source':'ORACLE_EVALUATION_ONLY',
            'pose_source':'PhysX_articulation_link_transforms',
            'motion_source':'Nav2_ROS2_guarded_wheel_drives',
            'sim_session':self.session, 'command_id':self.command_id,
            'target_region_id':self.target_region_id,
            'target_pose_xyyaw':self.regions[self.target_region_id],
            'parameters':self.parameters, 'samples':self.sample_count,
            'hold_gap_resets':self.hold_gap_resets, 'maximum_hold_sample_gap_sim_seconds':.2,
            'required_hold_sim_seconds':self.hold_seconds,
            'unexpected_contacts':[list(pair) for pair in sorted(self.contacts)],
            'errors':sorted(self.errors), 'evidence_reference':self.reference,
            **self.last_metrics,
        }

    def evaluate(self, command, stage_results, observation):
        with self.lock:
            return self._evaluate(command, stage_results, observation)

    def _evaluate(self, command, stage_results, observation):
        self.command_id = command['command_id']
        self.sample(observation)
        complete_stages = bool(stage_results) and all(row.get('status') == 'succeeded' for row in stage_results)
        if self.evaluation_since is None:
            self.evaluation_since = observation['source_stamp']
        success = bool(complete_stages and self.last_metrics['physical_success'])
        # Once Nav2 has finished, allow a short real physical settle window.
        # Incorrect arrival does not become success merely because Nav2 succeeded.
        terminal = (success or not complete_stages or bool(self.contacts or self.errors)
                    or observation['source_stamp']-self.evaluation_since >= 3.)
        summary = self.summary()
        Path(self.reference).write_text(json.dumps(summary, indent=2, allow_nan=False)+'\n')
        return {
            'physical_complete':terminal, 'success':success,
            'evidence_reference':self.reference,
            'details':{'independent_navigation_evaluation':summary},
        }

    def close(self):
        self.stream.close()
        result = self.summary()
        Path(self.reference).write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
        return result


def run_navigation(profile_path, scene_config, output, reader, *, simulation_config_path):
    """Run one to three move Goals in one layout; caller owns ROS and TruthReader."""
    from mobile_ex_runtime import MobileEXRuntime
    from astrex_mobile_manipulation.core import RobotProfile
    profile_path, output = Path(profile_path), Path(output)
    profile = json.loads(profile_path.read_text())
    config = json.loads(Path(scene_config).read_text()) if not isinstance(scene_config, dict) else scene_config
    navigation = config['navigation']
    if 'base' not in profile:
        raise ValueError('NAVIGATION_REQUIRES_MOBILE_PROFILE')
    if not config.get('robot', {}).get('mobile'):
        raise ValueError('NAVIGATION_REQUIRES_MOBILE_SCENE')
    profile_hash = RobotProfile.from_dict(profile).profile_hash
    observation = reader.observation(120.)
    if observation['profile_hash'] != profile_hash:
        raise ValueError('SIMULATION_PROFILE_MISMATCH')
    configuration_evidence = verify_simulation_configuration(
        simulation_config_path, config, profile_path, profile_hash,
        profile['robot_source']['isaac_asset'], observation)
    regions = navigation['regions']
    cases = navigation['cases']
    if not 1 <= len(cases) <= 3:
        raise ValueError('M1_REQUIRES_ONE_TO_THREE_ROUTES_PER_LAYOUT')
    if config['scene']['navigation_obstacles']['seed'] != navigation['layout_seed']:
        raise ValueError('NAVIGATION_LAYOUT_SEED_MISMATCH')
    output.mkdir(parents=True, exist_ok=True)
    (output/'navigation_config.json').write_text(json.dumps({
        'profile_path':str(profile_path), 'profile_hash':profile_hash, 'scene_config':config,
        'initial_physical_base_pose':observation['base_pose'],
        'configuration_evidence':configuration_evidence,
        'scope':'ONE_DEVELOPMENT_LAYOUT_NOT_FULL_M1_ACCEPTANCE',
        'planner_inputs':'known boundary + actual 2D LiDAR + wheel odometry; no truth obstacle feed',
    }, indent=2)+'\n')
    results = []
    for case in cases:
        trial_id = case['trial_id']
        case_dir = output/trial_id
        case_dir.mkdir(exist_ok=False)
        reader.settle()
        params = {
            'target_region_id':case['target_region_id'],
            'position_tolerance_m':navigation['position_tolerance_m'],
            'yaw_tolerance_rad':navigation['yaw_tolerance_rad'],
            'success_template':'move.arrive_and_stop.v1',
        }
        evidence = NavigationEvidence(profile, regions, case_dir,
            hold_seconds=navigation['stop_hold_sim_seconds'], simulator_binding=configuration_evidence)
        evidence.bind(params)
        row = {'case':case, 'goal':None, 'runtime':None, 'physical':None,
               'simulator_trial':None, 'errors':[], 'succeeded':False}
        runtime = None
        begun = False
        status = {}
        try:
            reader.request('begin', trial_id=trial_id, task_kind='MOVE',
                           scene_seed=navigation['layout_seed'])
            begun = True
            runtime = MobileEXRuntime(case_dir, profile_hash=profile_hash, profile=profile,
                regions=regions, physical_evaluator=evidence.evaluate,
                observation_provider=reader.observation)
            runtime.start()
            row['goal'] = runtime.submit_move(params)
            deadline = time.monotonic()+navigation['wall_deadline_seconds']
            while time.monotonic() < deadline:
                evidence.sample(reader.observation(2.))
                runtime.pump()
                status = runtime.status()
                if status.get('terminal'):
                    break
                threading.Event().wait(.02)
            else:
                runtime.cancel('M1_NAVIGATION_WALL_DEADLINE')
                stop_deadline = time.monotonic()+10.
                while time.monotonic() < stop_deadline:
                    runtime.pump()
                    status = runtime.status()
                    if status.get('terminal'):
                        break
                    threading.Event().wait(.02)
            row['runtime'] = status
            row['succeeded'] = bool(status.get('succeeded') and evidence.last_metrics.get('physical_success'))
        except Exception as exc:
            row['errors'].append(type(exc).__name__+':'+str(exc))
        finally:
            if runtime is not None:
                try:
                    runtime.close()
                except Exception as exc:
                    row['errors'].append('close:'+str(exc))
            try:
                row['physical'] = evidence.close()
            except Exception as exc:
                row['errors'].append('physical_close:'+str(exc))
            if begun:
                try:
                    row['simulator_trial'] = reader.request('end', trial_id=trial_id)
                    final_trial = row['simulator_trial']
                    if final_trial.get('unexpected_contacts') or final_trial.get('protective_failure'):
                        row['errors'].append('FINAL_PHYSICAL_CONTACT_OR_PROTECTIVE_FAILURE')
                except Exception as exc:
                    row['errors'].append('physical_end:'+str(exc))
            row['succeeded'] = bool(row['succeeded'] and not row['errors'])
            (case_dir/'result.json').write_text(json.dumps(row, indent=2, allow_nan=False)+'\n')
            results.append(row)
        # A failed or unresolved run requires diagnosis, not silent scene reset.
        if not row['succeeded']:
            break
    summary = {
        'scope':'M1_NAVIGATION_ONLY_ONE_SEEDED_DEVELOPMENT_LAYOUT',
        'layout_seed':navigation['layout_seed'], 'independent_layouts':1,
        'sim_session':observation['sim_session'], 'm1_acceptance_complete':False,
        'configuration_evidence':configuration_evidence,
        'fetch_executed':False, 'final_frozen_scenario_count':0,
        'configured_cases':len(cases), 'started_cases':len(results),
        'successes':sum(row['succeeded'] for row in results),
        'all_passed':len(results)==len(cases) and all(row['succeeded'] for row in results),
        'results':results,
    }
    (output/'navigation_summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False)+'\n')
    return summary


def summarize_navigation_layouts(summary_paths, required_seeds=(7301, 7302, 7303)):
    """Aggregate existing evidence only. Never run or repeat a scene here."""
    rows = [json.loads(Path(p).read_text()) if not isinstance(p, dict) else p for p in summary_paths]
    seeds = [row.get('layout_seed') for row in rows]
    sessions = [row.get('sim_session') for row in rows]
    errors = []
    if len(required_seeds) != 3 or len(set(required_seeds)) != 3:
        errors.append('M1_REQUIRES_THREE_FROZEN_LAYOUT_SEEDS')
    if (len(rows) != len(required_seeds) or any(type(seed) is not int for seed in seeds)
        or sorted(seeds) != sorted(required_seeds)):
        errors.append('REQUIRES_EACH_FROZEN_DEVELOPMENT_LAYOUT_ONCE')
    if any(not value for value in sessions) or len(set(sessions)) != len(rows):
        errors.append('REQUIRES_DISTINCT_SIMULATION_SESSIONS')
    for row in rows:
        try:
            binding = recheck_configuration_evidence(row['configuration_evidence'])
            if binding['actual_layout_seed'] != row.get('layout_seed') or binding['sim_session'] != row.get('sim_session'):
                raise ValueError('SUMMARY_LAYOUT_IDENTITY_MISMATCH')
        except (KeyError, ValueError, TypeError, OSError) as exc:
            errors.append('LAYOUT_CONFIGURATION_EVIDENCE:'+str(exc))
        if (row.get('scope') != 'M1_NAVIGATION_ONLY_ONE_SEEDED_DEVELOPMENT_LAYOUT'
            or row.get('independent_layouts') != 1 or not row.get('all_passed')
            or not 1 <= row.get('started_cases', 0) <= 3
            or row.get('successes') != row.get('started_cases')):
            errors.append('LAYOUT_RUN_NOT_PASSED:'+str(row.get('layout_seed')))
    return {'scope':'M1_THREE_DISTINCT_SEEDED_DEVELOPMENT_LAYOUTS',
            'required_seeds':list(required_seeds), 'observed_seeds':seeds,
            'independent_layouts':len(set(seeds)), 'sim_sessions':sessions,
            'all_passed':not errors, 'm1_acceptance_complete':not errors,
            'errors':errors, 'fetch_executed':False, 'final_frozen_scenario_count':0,
            'run_summaries':[str(p) for p in summary_paths if not isinstance(p, dict)]}
