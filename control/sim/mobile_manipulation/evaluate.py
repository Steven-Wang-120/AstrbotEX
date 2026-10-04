"""ORACLE-only physical evidence beside the protected Isaac execution hook.

Truth has its own topic and is never published as perception. Trial setup can
reset a scene only while no command is leased and every arm joint is still.
No reset is permitted during an active trial.
"""
import json
import math
import os
from pathlib import Path
import time
import uuid
import sys

# Shared pure evidence rules; do not import system ROS paths into Isaac.
_EVIDENCE_LIB=Path(__file__).resolve().parents[2]/"scripts/lib"
if str(_EVIDENCE_LIB) not in sys.path:sys.path.insert(0,str(_EVIDENCE_LIB))
from mobile_trial_evidence import update_place_window, truth_sample_due

import numpy as np
from pxr import Gf, PhysicsSchemaTools, Usd, UsdGeom, UsdPhysics, PhysxSchema


def prepare_evaluation_bodies(world):
    """Create wrappers before reset; their constructors can author physics USD."""
    from isaacsim.core.prims import SingleRigidPrim
    if world.scene.get_object('red_cube') is None:
        raise RuntimeError('SCENE_RED_CUBE_NOT_PREPARED')
    if world.scene.get_object('evaluation_blue_cylinder') is None:
        world.scene.add(SingleRigidPrim('/World/BlueCylinder',name='evaluation_blue_cylinder'))


def contact_reporting_readback(stage):
    """Validate pre-reset setup without changing a live physics prim."""
    paths=[]
    for prim in stage.Traverse():
        if not prim.HasAPI(UsdPhysics.RigidBodyAPI):
            continue
        if not prim.HasAPI(PhysxSchema.PhysxContactReportAPI):
            raise RuntimeError('CONTACT_REPORT_NOT_PREPARED_BEFORE_RESET:'+str(prim.GetPath()))
        threshold=PhysxSchema.PhysxContactReportAPI(prim).GetThresholdAttr().Get()
        if threshold is None or float(threshold)!=0.:
            raise RuntimeError('CONTACT_REPORT_THRESHOLD_MISMATCH:'+str(prim.GetPath()))
        paths.append(str(prim.GetPath()))
    return {'source':'pre_reset_schema_readback','threshold':0.,'body_paths':paths,'body_count':len(paths)}


def pose(stage, path, offset=None):
    prim = stage.GetPrimAtPath(path)
    if not prim:
        return None
    matrix = UsdGeom.XformCache().GetLocalToWorldTransform(prim)
    p = matrix.Transform(Gf.Vec3d(*(offset or [0, 0, 0])))
    rotation = matrix.ExtractRotationQuat()
    return {'position': list(p), 'orientation': [*rotation.GetImaginary(), rotation.GetReal()],
            'frame_id': 'world'}


def rotate(q, vector):
    xyz=np.asarray(q[:3],dtype=float)
    v=np.asarray(vector,dtype=float)
    return v+2*np.cross(xyz,np.cross(xyz,v)+q[3]*v)


class EvaluationHook:
    def __init__(self, world, config):
        from astrex_mobile_manipulation.isaac_gate import install
        from omni.physx import get_physx_simulation_interface
        from std_msgs.msg import String
        contact_reporting=contact_reporting_readback(world.stage)
        self.gate = install(world, config)
        self.world, self.node, self.String = world, config['ros_node'], String
        self.robot_path = config.get('robot_prim', '/World/Robot')
        self.output = Path(config['output_dir'])
        (self.output/'robot_profile.json').write_text(json.dumps(self.gate.profile.raw,indent=2)+'\n')
        (self.output/'contact_reporting.json').write_text(json.dumps(contact_reporting,indent=2)+'\n')
        self.spec = config.get('evaluation', {})
        self.effective_config_hash = config.get('effective_config_hash')
        self.session = uuid.uuid4().hex
        self.sleep_threshold=float(self.gate.robot.get_sleep_threshold())
        physics_prim=world.stage.GetPrimAtPath(world.get_physics_context().prim_path)
        self.external_forces_every_iteration=bool(PhysxSchema.PhysxSceneAPI(physics_prim).GetEnableExternalForcesEveryIterationAttr().Get())
        if self.external_forces_every_iteration != self.gate.profile.raw['robot_source'].get('enable_external_forces_every_iteration',False):
            raise RuntimeError('PHYSICS_EXTERNAL_FORCE_SETTING_MISMATCH')
        self.solver_iterations={'position':int(self.gate.robot.get_solver_position_iteration_count()),
                                'velocity':int(self.gate.robot.get_solver_velocity_iteration_count())}
        expected_sleep=self.gate.profile.raw['robot_source'].get('articulation_sleep_threshold',.005)
        if abs(self.sleep_threshold-expected_sleep)>1e-7:
            raise RuntimeError('ARTICULATION_SLEEP_SETTING_MISMATCH')
        self.seq, self.last_publish, self.sim_time = 0, -math.inf, 0.
        self.last_publish_wall=-math.inf
        self.trial = None
        self.last_request = None
        self.requests = {}
        self.contacts = {}
        self.objects = {}
        self.articulation=self.gate.robot._articulation_view
        self.body_names=list(self.articulation.body_names)
        self.local_bounds={}
        cache=UsdGeom.BBoxCache(Usd.TimeCode.Default(),['default','render','proxy'])
        for name in self.body_names:
            path=self.robot_path+('/gripper/' if name in ('xarm_gripper_base_link','left_finger','right_finger',
                'left_outer_knuckle','right_outer_knuckle','left_inner_knuckle','right_inner_knuckle') else '/')+name
            prim=world.stage.GetPrimAtPath(path)
            if not prim:continue
            bounds=cache.ComputeUntransformedBound(prim).ComputeAlignedRange()
            lo,hi=np.asarray(bounds.GetMin()),np.asarray(bounds.GetMax())
            if np.all(lo<=hi) and np.all(np.isfinite(lo)) and np.all(np.isfinite(hi)):
                self.local_bounds[name]=np.asarray([(x,y,z) for x in (lo[0],hi[0]) for y in (lo[1],hi[1]) for z in (lo[2],hi[2])])
        for name, scene_name in [('red_cube','red_cube'), ('blue_cylinder','evaluation_blue_cylinder')]:
            body=world.scene.get_object(scene_name)
            if body is None:
                raise RuntimeError('EVALUATION_BODY_NOT_PREPARED_BEFORE_RESET:'+scene_name)
            self.objects[name]=body
        self.subscription = get_physx_simulation_interface().subscribe_contact_report_events(self.on_contacts)
        self.publisher = self.node.create_publisher(String, '/astrex/mm/evaluation/truth', 10)
        self.acks = self.node.create_publisher(String, '/astrex/mm/evaluation/ack', 10)
        self.request_sub = self.node.create_subscription(String, '/astrex/mm/evaluation/request', self.request, 10)
        self.stream = (self.output/'physical_samples.jsonl').open('a', buffering=65536)
        self.last_flush = time.monotonic()
        # Bounded startup diagnostic: preserve the raw PhysX pre/post readings.
        # It does not alter published state or the stationary threshold.
        from isaacsim.core.simulation_manager import SimulationManager, IsaacEvents
        self.feedback_probe = []
        self.pre_feedback = None
        self.post_callback = SimulationManager.register_callback(
            self.probe_post_feedback, IsaacEvents.POST_PHYSICS_STEP, name='astrex_feedback_probe')

    def on_contacts(self, headers, data):
        for header in headers:
            if not header.num_contact_data:
                continue
            pair = tuple(sorted((str(PhysicsSchemaTools.intToSdfPath(header.actor0)),
                                 str(PhysicsSchemaTools.intToSdfPath(header.actor1)))))
            impulses = [float(np.linalg.norm(data[i].impulse)) for i in
                        range(header.contact_data_offset, header.contact_data_offset+header.num_contact_data)]
            if max(impulses, default=0) < 1e-7:
                continue
            self.contacts[pair] = self.sim_time
            if self.trial is not None:
                if '/World/RedCube' in pair:
                    for side in ('left_finger', 'right_finger'):
                        if any(p.endswith('/'+side) for p in pair):
                            self.trial['finger_contacts'].add(side)
                robot = [p for p in pair if p.startswith(self.robot_path+'/')]
                outside = [p for p in pair if not p.startswith(self.robot_path+'/')]
                allowed_target = (outside == ['/World/RedCube'] and
                                  all(p.endswith(('/left_finger', '/right_finger')) for p in robot))
                allowed_ground = (outside and all('groundPlane' in p or 'GroundPlane' in p for p in outside)
                                  and all('wheel' in p or p.endswith('/base_link') for p in robot))
                if robot and outside and not allowed_target and not allowed_ground:
                    self.trial['unexpected_contacts'].add(pair)

    def request(self, message):
        request_id = None
        try:
            request = json.loads(message.data)
            request_id = request['request_id']
            if request.get('sim_session')!=self.session:raise ValueError('simulation_session_mismatch')
            if not isinstance(request_id, str) or len(request_id) > 128:
                raise ValueError('invalid_request_id')
            if request_id in self.requests:
                result = self.requests[request_id]
            else:
                op = request['op']
                if op == 'reset':
                    result = self.reset(request)
                elif op == 'begin':
                    if self.trial is not None or self.gate.core.active:
                        raise ValueError('trial_or_command_active')
                    kind = request['task_kind']
                    if kind not in ('G', 'PLACE', 'C', 'MOVE', 'DEVELOPMENT'):
                        raise ValueError('unsupported_task_kind')
                    trial_id = request['trial_id']
                    if not isinstance(trial_id, str) or not trial_id.replace('_', '').replace('-', '').isalnum():
                        raise ValueError('invalid_trial_id')
                    self.trial = {'trial_id':trial_id, 'task_kind':kind, 'scene_seed':request['scene_seed'],
                        'started_sim':self.sim_time, 'started_monotonic':time.monotonic(),
                        'initial_guard_stop_count':self.gate.core.stop_count,
                        'initial_object_z':float(self.objects['red_cube'].get_world_pose()[0][2]),
                        'max_lift_m':0., 'lift_stable_since':None, 'place_stable_since':None,
                        'lift_hold_seconds':0., 'place_hold_seconds':0., 'current_place_hold_seconds':0., 'finger_contacts':set(),
                        'unexpected_contacts':set(), 'passed_channel':False, 'channel_entry_seen':False,
                        'channel_departure_seen':False, 'channel_bypass':False,
                        'current_grasped':False,'current_place_valid':False,'release_seen':False,
                        'drop_detected':False,'lost_contact_since':None,
                        'channel_min_aabb_clearance_m':None, 'maximum_joint_velocity':0.,
                        'guard_reasons':set(), 'samples':0}
                    result = {'status':'begun', 'trial_id':trial_id}
                elif op == 'end':
                    if self.trial is None or request.get('trial_id') != self.trial['trial_id']:
                        raise ValueError('trial_mismatch')
                    if self.gate.core.active:
                        raise ValueError('command_still_active')
                    result = self.summary()
                    (self.output/(self.trial['trial_id']+'_physical.json')).write_text(
                        json.dumps(result, indent=2, allow_nan=False)+'\n')
                    self.trial = None
                    self.stream.flush()
                else:
                    raise ValueError('unknown_evaluation_request')
                self.requests[request_id] = result
            ack = {'request_id':request_id, 'ok':True, 'result':result}
        except Exception as exc:
            ack = {'request_id':request_id, 'ok':False, 'error':str(exc)}
        ack.update(sim_session=self.session,seq=self.seq,source_stamp=self.sim_time)
        self.acks.publish(self.String(data=json.dumps(ack, allow_nan=False)))

    def reset(self, request):
        if self.trial is not None or self.gate.core.active:
            raise ValueError('reset_requires_no_trial_and_no_lease')
        velocities = self.gate.robot.get_joint_velocities()
        if max(abs(float(velocities[i])) for i in self.gate.indices) > .05:
            raise ValueError('reset_requires_stationary_arm')
        seed = request['scene_seed']
        if type(seed) is not int or seed < 0:
            raise ValueError('invalid_scene_seed')
        q = np.asarray(self.gate.profile.raw['initial_joint_positions'])
        self.gate.robot.set_joint_positions(q, joint_indices=np.asarray(self.gate.indices))
        self.gate.robot.set_joint_velocities(np.zeros(len(q)), joint_indices=np.asarray(self.gate.indices))
        self.gate.core.hold = dict(zip(self.gate.profile.names, map(float, q)))
        self.gate.core.targets.clear()
        self.gate.core.gripper_hold.clear()
        rng = np.random.default_rng(seed)
        offset = rng.uniform(-.025, .025, 2)
        positions = {'red_cube':[.5+offset[0], -.08+offset[1], .78],
                     'blue_cylinder':[.65, .10, .78]}
        for name, body in self.objects.items():
            body.set_world_pose(np.asarray(positions[name]), np.asarray([1.,0.,0.,0.]))
            body.set_linear_velocity(np.zeros(3))
            body.set_angular_velocity(np.zeros(3))
        self.contacts.clear()
        return {'status':'reset', 'scene_seed':seed, 'setup_only':True,
                'source_stamp':self.sim_time, 'clock_reset':False}

    def snapshot(self):
        # Isaac Lab runs Fabric with updateToUsd=false. Dynamic USD transforms
        # are not truth. Read the PhysX articulation tensor for every body pose.
        transforms=np.asarray(self.articulation._physics_view.get_link_transforms())[0]
        self.body_transforms=dict(zip(self.body_names,transforms))
        base_transform=self.body_transforms['base_link' if self.gate.base else 'world']
        xy_radius=0.
        for name,corners in self.local_bounds.items():
            transform=self.body_transforms[name]
            points=transform[:3]+rotate(transform[3:7],corners)-base_transform[:3]
            xy_radius=max(xy_radius,float(np.linalg.norm(points[:,:2],axis=1).max()))
        def body_pose(name,offset=(0,0,0)):
            transform=self.body_transforms[name]
            return {'position':list(map(float,transform[:3]+rotate(transform[3:7],offset))),
                    'orientation':list(map(float,transform[3:7])),'frame_id':'world'}
        values = {}
        for name, body in self.objects.items():
            p, q = body.get_world_pose()
            values[name] = {'position':list(map(float,p)),
                'orientation':[float(q[1]),float(q[2]),float(q[3]),float(q[0])],
                'linear_velocity':list(map(float,body.get_linear_velocity())),
                'angular_velocity':list(map(float,body.get_angular_velocity()))}
        return {'schema':'astrex.mm.truth.v1', 'input_source':'ORACLE_EVALUATION_ONLY',
            'sim_session':self.session, 'seq':self.seq, 'source_stamp':self.sim_time,
            'published_monotonic':time.monotonic(), 'frame_id':'world',
            'effective_config_hash':self.effective_config_hash, 'simulator_pid':os.getpid(),
            'tcp_pose':body_pose('xarm_gripper_base_link',[0,0,.172]),
            'base_pose':body_pose('base_link' if self.gate.base else 'world'),
            'pose_source':'PhysX_articulation_link_transforms',
            'articulation_sleep_threshold':self.sleep_threshold,
            'solver_iterations':self.solver_iterations,
            'external_forces_every_iteration':self.external_forces_every_iteration,
            'whole_robot_xy_envelope_radius_m':xy_radius,
            'objects':values, 'q':self.gate.q, 'dq':self.gate.dq,
            'wheel_joint_velocities':dict(zip(getattr(self.gate,'wheel_names',[]),
                [float(self.gate.robot.get_joint_velocities()[i]) for i in self.gate.wheel_indices])),
            'place_pose':pose(self.world.stage,'/World/PlaceRegion'),
            'recent_contacts':[list(pair) for pair,t in self.contacts.items() if self.sim_time-t<.06],
            'guard_active':self.gate.core.active, 'guard_reason':self.gate.core.reason,
            'profile_hash':self.gate.profile.profile_hash}

    def update_trial(self, state):
        if self.trial is None:
            return
        trial = self.trial
        obj = state['objects']['red_cube']
        p = np.asarray(obj['position'])
        speed = np.linalg.norm(obj['linear_velocity'])
        lift = p[2]-trial['initial_object_z']
        trial['max_lift_m'] = max(trial['max_lift_m'],float(lift))
        trial['samples'] += 1
        trial['maximum_joint_velocity'] = max(trial['maximum_joint_velocity'],
            max(map(abs,state['dq'].values()), default=0))
        if (self.gate.core.stop_count > trial['initial_guard_stop_count'] and
            self.gate.core.reason not in ('AUTHORIZED','INITIAL_HOLD','ACTION_SUCCEEDED','ACTION_COMPLETE', 'COMPLETED',
                                       'CONTROLLER_COMPLETE_PHYSICAL_EVALUATION_REQUIRED')):
            trial['guard_reasons'].add(self.gate.core.reason)
        recent=state['recent_contacts']
        gripped=all(any('/World/RedCube' in pair and any(p.endswith('/'+side) for p in pair)
                        for pair in recent) for side in ('left_finger','right_finger'))
        trial['current_grasped']=gripped
        if (trial['lift_hold_seconds']>=1. and self.gate.core.motion_kind=='gripper' and
            self.gate.core.active and self.gate.core.targets.get('drive_joint',1.)<.1):
            trial['release_seen']=True
        if trial['lift_hold_seconds']>=1. and not trial['release_seen'] and not gripped:
            if trial['lost_contact_since'] is None:trial['lost_contact_since']=self.sim_time
            if self.sim_time-trial['lost_contact_since']>.1:trial['drop_detected']=True
        else:trial['lost_contact_since']=None
        if lift >= .08 and speed < .025 and gripped:
            if trial['lift_stable_since'] is None:
                trial['lift_stable_since'] = self.sim_time
            trial['lift_hold_seconds'] = max(trial['lift_hold_seconds'],self.sim_time-trial['lift_stable_since'])
        else:
            trial['lift_stable_since'] = None
        place = np.asarray(state['place_pose']['position'])
        xy_error = float(np.linalg.norm(p[:2]-place[:2]))
        trial['placement_xy_error_m'] = xy_error
        half_region = np.asarray(self.spec.get('place_dimensions',[.14,.14,.005]))[:2]/2
        # A rotated 5 cm cube has this exact conservative projection radius.
        qx,qy,qz,qw = obj['orientation']
        rotation = Gf.Matrix3d(Gf.Quatd(qw,Gf.Vec3d(qx,qy,qz)))
        projected = np.sum(np.abs(np.asarray(rotation)), axis=0)*.025
        inside = bool(np.all(np.abs(p[:2]-place[:2])+projected[:2] <= half_region))
        opened = state['q'].get('drive_joint',1.) < .10
        retracted = np.linalg.norm(np.asarray(state['tcp_pose']['position'])-p) > .10
        on_surface = abs(p[2]-(place[2]+.0025+.025)) < .006
        stable = speed < .02 and np.linalg.norm(obj['angular_velocity']) < .1
        placed = inside and opened and retracted and on_surface and stable
        trial['current_place_valid']=placed
        update_place_window(trial,self.sim_time,placed)
        trial['within_region'] = inside
        if trial['task_kind']=='C' and self.spec.get('channel'):
            channel = self.spec['channel']
            if lift >= .06 and gripped:
                inside_x = channel['inner_x_min']+.025 <= p[0] <= channel['inner_x_max']-.025
                if p[1] <= channel['entry_y'] and inside_x:
                    trial['channel_entry_seen'] = True
                if channel['entry_y'] < p[1] < channel['exit_y'] and not inside_x:
                    trial['channel_bypass'] = True
                if p[1] >= channel['exit_y'] and trial['channel_entry_seen'] and inside_x:
                    trial['channel_departure_seen'] = True
                    trial['passed_channel'] = not trial['channel_bypass']
            # World-space collision AABBs give a conservative lower bound;
            # this is explicitly not exact mesh distance.
            cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ['default','render','proxy'])
            walls = [self.world.stage.GetPrimAtPath(path) for path in channel['wall_prims']]
            body_bounds=[]
            for name,corners in self.local_bounds.items():
                transform=self.body_transforms[name]
                points=transform[:3]+rotate(transform[3:7],corners)
                body_bounds.append((points.min(axis=0),points.max(axis=0)))
            object_corners=np.asarray([p+rotate(obj['orientation'],(x,y,z))
                for x in (-.025,.025) for y in (-.025,.025) for z in (-.025,.025)])
            body_bounds.append((object_corners.min(axis=0),object_corners.max(axis=0)))
            lower = math.inf
            for amin,amax in body_bounds:
                for b in walls:
                    bb = cache.ComputeWorldBound(b).ComputeAlignedRange()
                    gap = np.maximum(0, np.maximum(amin-np.asarray(bb.GetMax()),np.asarray(bb.GetMin())-amax))
                    lower = min(lower,float(np.linalg.norm(gap)))
            if math.isfinite(lower):
                old = trial['channel_min_aabb_clearance_m']
                trial['channel_min_aabb_clearance_m'] = lower if old is None else min(old,lower)

    def summary(self):
        if self.trial is None:
            return None
        result = {key:sorted(value) if isinstance(value,set) else value for key,value in self.trial.items()}
        protective_failures = ('LIMIT','STALE','EXPIRED','INVALID','TRACKING_ERROR','RESET','REVERSED')
        violated = any(any(word in reason for word in protective_failures) for reason in result['guard_reasons'])
        clear = not result['unexpected_contacts'] and not violated and not result['drop_detected']
        grasp = result['lift_hold_seconds'] >= 1. and clear
        placed = result['current_place_hold_seconds'] >= 1. and result['current_place_valid'] and clear
        error = result.get('placement_xy_error_m')
        result.update({'input_source':'ORACLE', 'sim_session':self.session, 'seq':self.seq, 'source_stamp':self.sim_time, 'config_hash':self.gate.profile.profile_hash,
            'grasp_success':grasp, 'place_scores':{str(mm):bool(placed and error is not None and error<=mm/1000)
                                                  for mm in (20,10,5)},
            'channel_success':bool(grasp and result['passed_channel'] and clear and
                                   (result['current_grasped'] or placed)),
            'protective_failure':violated,
            'clearance_measure':'conservative_world_AABB_lower_bound_not_exact_mesh_distance',
            'sim_seconds':self.sim_time-result['started_sim'],
            'wall_seconds':time.monotonic()-result['started_monotonic'],
            'evidence_reference':str(self.output/(result['trial_id']+'_physical.json'))})
        result['physical_success'] = ({'G':grasp, 'PLACE':bool(grasp and result['place_scores']['20']),
            'C':result['channel_success']}.get(result['task_kind'],False))
        return result

    def probe_post_feedback(self, dt):
        if len(self.feedback_probe) >= 240 or self.pre_feedback is None:
            return
        self.feedback_probe.append({'sim_time':self.sim_time,'dt':float(dt),
            'pre':self.pre_feedback,
            'post':{'q':self.gate.robot.get_joint_positions().tolist(),
                    'dq':self.gate.robot.get_joint_velocities().tolist()}})
        if len(self.feedback_probe) == 240:
            (self.output/'feedback_pre_post.json').write_text(json.dumps({
                'joint_names':self.gate.all_names, 'samples':self.feedback_probe},indent=2)+'\n')

    def step(self, sim_time, dt):
        self.sim_time = sim_time
        self.gate.step(sim_time,dt)
        if len(self.feedback_probe) < 240:
            self.pre_feedback={'q':self.gate.robot.get_joint_positions().tolist(),
                               'dq':self.gate.robot.get_joint_velocities().tolist()}
        now=time.monotonic()
        if not truth_sample_due(sim_time,now,self.last_publish,self.last_publish_wall):
            return
        self.last_publish=sim_time; self.last_publish_wall=now
        self.seq += 1
        state = self.snapshot()
        self.update_trial(state)
        state['trial'] = self.summary()
        encoded = json.dumps(state,allow_nan=False,separators=(',',':'))
        self.publisher.publish(self.String(data=encoded))
        self.stream.write(encoded+'\n')
        if time.monotonic()-self.last_flush > 2:
            self.last_flush = time.monotonic()
            self.stream.flush()

    def close(self):
        from isaacsim.core.simulation_manager import SimulationManager
        SimulationManager.deregister_callback(self.post_callback)
        self.gate.close()
        self.subscription = None
        self.stream.close()
        self.node.destroy_subscription(self.request_sub)
        self.node.destroy_publisher(self.publisher)
        self.node.destroy_publisher(self.acks)


def install(world, config):
    return EvaluationHook(world, config)
