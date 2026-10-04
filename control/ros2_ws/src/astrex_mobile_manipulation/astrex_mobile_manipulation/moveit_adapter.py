"""Asynchronous, planning-only MoveIt adapter. No execution capability."""
from __future__ import annotations
import math
import time
from geometry_msgs.msg import Pose
from moveit_msgs.msg import (AttachedCollisionObject, CollisionObject, Constraints,
    JointConstraint, PositionConstraint, OrientationConstraint, BoundingVolume,
    PlanningScene, AllowedCollisionEntry, PlanningSceneComponents)
from moveit_msgs.srv import GetMotionPlan, GetCartesianPath, ApplyPlanningScene, GetPlanningScene
from shape_msgs.msg import SolidPrimitive
from .core import Rejected, finite, digest, support_contact_updates


def pose_from_dict(row):
    p = row.get('position'); q = row.get('orientation', [0.,0.,0.,1.])
    if len(p or []) != 3 or len(q) != 4:
        raise Rejected('POSE_SHAPE')
    p = [finite(v,'position') for v in p]; q = [finite(v,'quaternion') for v in q]
    if abs(sum(v*v for v in q)-1) > 1e-3:
        raise Rejected('QUATERNION_NORM')
    pose = Pose(); pose.position.x,pose.position.y,pose.position.z=p
    pose.orientation.x,pose.orientation.y,pose.orientation.z,pose.orientation.w=q
    return pose


def set_allowed_contact(matrix, target, links, allowed):
    for name in [target, *links]:
        if name not in matrix.entry_names:
            for entry in matrix.entry_values: entry.enabled.append(False)
            matrix.entry_names.append(name)
            entry=AllowedCollisionEntry(); entry.enabled=[False]*len(matrix.entry_names)
            matrix.entry_values.append(entry)
    target_index=matrix.entry_names.index(target)
    for link in links:
        link_index=matrix.entry_names.index(link)
        matrix.entry_values[target_index].enabled[link_index]=allowed
        matrix.entry_values[link_index].enabled[target_index]=allowed


def trajectory_dict(trajectory):
    return {'joint_names':list(trajectory.joint_names), 'points':[
        {'positions':list(p.positions),'velocities':list(p.velocities),
         'accelerations':list(p.accelerations),
         'time_from_start':p.time_from_start.sec+p.time_from_start.nanosec/1e9}
        for p in trajectory.points]}

class MoveItAdapter:
    def __init__(self, node, profile):
        self.node, self.profile = node, profile
        prefix = profile.moveit_namespace.rstrip('/')
        self.planner = node.create_client(GetMotionPlan,prefix+'/plan_kinematic_path')
        self.cartesian = node.create_client(GetCartesianPath,prefix+'/compute_cartesian_path')
        self.apply = node.create_client(ApplyPlanningScene,prefix+'/apply_planning_scene')
        self.get_scene = node.create_client(GetPlanningScene,prefix+'/get_planning_scene')
        self.inflight = False
        self.scene_revision = 0
        self.scene_hash = ''
        self.active_request = None

    def _object(self,row):
        obj=CollisionObject(); obj.header.frame_id=self.profile.base_frame
        obj.id=row['id']
        if not isinstance(obj.id,str) or not 1<=len(obj.id)<=128:
            raise Rejected('OBJECT_ID')
        if row.get('remove',False):
            obj.operation=CollisionObject.REMOVE; return obj
        obj.operation=CollisionObject.ADD
        kind=row.get('shape','box'); dims=row.get('dimensions',[])
        expected={'box':3,'sphere':1,'cylinder':2}
        if kind not in expected or len(dims)!=expected[kind]: raise Rejected('GEOMETRY_SHAPE')
        primitive=SolidPrimitive(); primitive.type={'box':1,'sphere':2,'cylinder':3}[kind]
        primitive.dimensions=[finite(v,'dimension') for v in dims]
        if any(v<=0 for v in primitive.dimensions): raise Rejected('GEOMETRY_DIMENSION')
        obj.primitives=[primitive]; obj.primitive_poses=[pose_from_dict(row)]
        return obj

    def _scene(self,payload):
        specification=payload.get('scene',{})
        if not isinstance(specification,dict): raise Rejected('SCENE_SHAPE')
        scene=PlanningScene(); scene.is_diff=True; scene.robot_state.is_diff=True
        objects=specification.get('objects',[]); attachments=specification.get('attached',[])
        if len(objects)>128 or len(attachments)>1: raise Rejected('SCENE_BUDGET')
        scene.world.collision_objects=[self._object(row) for row in objects]
        for row in attachments:
            attached=AttachedCollisionObject()
            attached.link_name=self.profile.tcp_frame
            attached.object=self._object(row)
            if not row.get('remove',False):
                attached.object.header.frame_id=row.get('frame_id',self.profile.tcp_frame)
                if attached.object.header.frame_id != self.profile.tcp_frame:
                    raise Rejected('ATTACHED_FRAME')
            allowed=self.profile.raw.get('gripper_touch_links',[])
            requested=row.get('touch_links',allowed)
            if not set(requested).issubset(set(allowed)): raise Rejected('UNAUTHORIZED_TOUCH_LINK')
            attached.touch_links=list(requested)
            scene.robot_state.attached_collision_objects.append(attached)
        return scene,specification

    def _apply_scene(self,specification,done):
        """Apply one validated diff and report its acknowledged scene revision."""
        scene,specification=self._scene({'scene':specification})
        support_updates=support_contact_updates(specification,self.profile)
        request=GetPlanningScene.Request()
        request.components.components=PlanningSceneComponents.ALLOWED_COLLISION_MATRIX
        def got_scene(f):
            try:
                scene.allowed_collision_matrix=f.result().scene.allowed_collision_matrix
                matrix=scene.allowed_collision_matrix
                for pair in specification.get('allow_contacts',[]):
                    target=pair['object_id']; links=pair['links']; allowed=pair.get('allowed',True)
                    if type(allowed) is not bool: raise Rejected('CONTACT_ALLOWED_TYPE')
                    if not set(links).issubset(set(self.profile.raw.get('gripper_touch_links',[]))):
                        raise Rejected('UNAUTHORIZED_CONTACT')
                    set_allowed_contact(matrix,target,links,allowed)
                for target,support,allowed in support_updates:
                    set_allowed_contact(matrix,target,[support],allowed)
                req=ApplyPlanningScene.Request(); req.scene=scene
                self.apply.call_async(req).add_done_callback(applied)
            except Exception as exc: done(None,str(exc))
        def applied(f):
            try:
                if not f.result().success: raise Rejected('SCENE_APPLY_FAILED')
                self.scene_revision+=1; self.scene_hash=digest(specification)
                done({'scene_ack':True,'scene_revision':self.scene_revision,'scene_hash':self.scene_hash},None)
            except Exception as exc: done(None,str(exc))
        self.get_scene.call_async(request).add_done_callback(got_scene)

    def update_scene(self,specification,done):
        """Scene-only maintenance, including removal of a diagnostic object.

        Uses the same validation and acknowledged mutation as planning. This
        method cannot generate a trajectory or submit a controller Action.
        The caller serializes it against other adapter instances.
        """
        if self.inflight: raise Rejected('PLANNER_BUSY')
        if not all(c.service_is_ready() for c in (self.apply,self.get_scene)):
            raise Rejected('MOVEIT_UNAVAILABLE')
        self.inflight=True
        def finished(result,error):
            self.inflight=False; done(result,error)
        try: self._apply_scene(specification,finished)
        except Exception:
            self.inflight=False; raise

    def plan(self,payload,state,done):
        if self.inflight: raise Rejected('PLANNER_BUSY')
        required=[self.apply,self.get_scene,self.cartesian if payload.get('cartesian',False) else self.planner]
        if not all(client.service_is_ready() for client in required): raise Rejected('MOVEIT_UNAVAILABLE')
        self.inflight=True; started=time.monotonic()
        token=object(); self.active_request=token
        self._q=dict(state.positions); self._stamp=state.stamp
        def applied(scene_ack,error):
            try:
                if error: raise Rejected(error)
                if time.monotonic()-started > self.profile.planner_budget+1:
                    raise Rejected('SCENE_HANDOFF_TIMEOUT')
                if payload.get('cartesian',False):
                    req=GetCartesianPath.Request()
                    req.header.frame_id=self.profile.base_frame
                    req.start_state=self._start_state()
                    req.group_name=self.profile.group_name; req.link_name=self.profile.tcp_frame
                    req.waypoints=[pose_from_dict(payload['target_pose'])]
                    req.max_step=.005; req.jump_threshold=2.; req.revolute_jump_threshold=.35
                    req.avoid_collisions=True
                    req.max_velocity_scaling_factor=self.profile.velocity_scale
                    req.max_acceleration_scaling_factor=self.profile.acceleration_scale
                    self.cartesian.call_async(req).add_done_callback(planned)
                else:
                    req=GetMotionPlan.Request(); motion=req.motion_plan_request
                    motion.group_name=self.profile.group_name; motion.pipeline_id='ompl'
                    motion.planner_id='RRTConnectkConfigDefault'
                    motion.allowed_planning_time=self.profile.planner_budget
                    motion.num_planning_attempts=1; motion.start_state=self._start_state()
                    motion.max_velocity_scaling_factor=self.profile.velocity_scale
                    motion.max_acceleration_scaling_factor=self.profile.acceleration_scale
                    constraint=Constraints()
                    if 'joint_targets' in payload:
                        targets=payload['joint_targets']
                        if set(targets)!=set(self.profile.arm_joint_names): raise Rejected('TARGET_JOINTS')
                        for name in self.profile.arm_joint_names:
                            value=finite(targets[name],name); limits=self.profile.joint_limits[name]
                            if not limits.lower<=value<=limits.upper: raise Rejected('TARGET_JOINT_LIMIT')
                            c=JointConstraint(); c.joint_name=name; c.position=value
                            c.tolerance_above=.001; c.tolerance_below=.001; c.weight=1.
                            constraint.joint_constraints.append(c)
                    else:
                        pose=pose_from_dict(payload['target_pose'])
                        pc=PositionConstraint(); pc.header.frame_id=self.profile.base_frame
                        pc.link_name=self.profile.tcp_frame; pc.weight=1.
                        shape=SolidPrimitive(); shape.type=SolidPrimitive.SPHERE; shape.dimensions=[.005]
                        pc.constraint_region=BoundingVolume(); pc.constraint_region.primitives=[shape]
                        pc.constraint_region.primitive_poses=[pose]
                        oc=OrientationConstraint(); oc.header.frame_id=self.profile.base_frame
                        oc.link_name=self.profile.tcp_frame; oc.orientation=pose.orientation; oc.weight=1.
                        oc.absolute_x_axis_tolerance=math.radians(5)
                        oc.absolute_y_axis_tolerance=math.radians(5)
                        oc.absolute_z_axis_tolerance=math.radians(5)
                        constraint.position_constraints=[pc]; constraint.orientation_constraints=[oc]
                    motion.goal_constraints=[constraint]
                    self.planner.call_async(req).add_done_callback(planned)
            except Exception as exc: finish(None,str(exc))
        def planned(f):
            try:
                response=f.result()
                if payload.get('cartesian',False):
                    if response.error_code.val!=1 or response.fraction<1.-1e-9:
                        raise Rejected(f'CARTESIAN_INCOMPLETE:{response.error_code.val}:{response.fraction}')
                    trajectory=response.solution.joint_trajectory; planning_time=None
                else:
                    motion=response.motion_plan_response
                    if motion.error_code.val!=1: raise Rejected(f'MOVEIT_ERROR:{motion.error_code.val}')
                    trajectory=motion.trajectory.joint_trajectory; planning_time=motion.planning_time
                if time.monotonic()-started > self.profile.planner_budget+2:
                    raise Rejected('PLANNING_WALL_TIMEOUT')
                finish({'trajectory':trajectory,'summary':trajectory_dict(trajectory),
                    'scene_revision':self.scene_revision,'scene_hash':self.scene_hash,
                    'planning_time':planning_time,'total_wall_time':time.monotonic()-started,
                    'source_state_stamp':self._stamp},None)
            except Exception as exc: finish(None,str(exc))
        def finish(result,error):
            if self.active_request is not token: return
            self.inflight=False; self.active_request=None; done(result,error)
        try: self._apply_scene(payload.get('scene',{}),applied)
        except Exception as exc: finish(None,str(exc))

    def _start_state(self):
        from moveit_msgs.msg import RobotState
        state=RobotState(); state.is_diff=True
        state.joint_state.name=list(self._q)
        state.joint_state.position=[self._q[n] for n in state.joint_state.name]
        sec=int(self._stamp); state.joint_state.header.stamp.sec=sec
        state.joint_state.header.stamp.nanosec=int((self._stamp-sec)*1e9)
        return state
