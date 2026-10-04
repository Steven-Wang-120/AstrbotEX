"""Compose the pinned xArm asset with bounded drives and an optional wheel base.

Construction/reset is allowed before a trial. Runtime motion uses articulation
drives through the shared execution gate, never world-pose assignments.
"""
import json
import math
from pathlib import Path

from pxr import Gf, Sdf, UsdGeom, UsdPhysics, PhysxSchema


def _pose(prim, xyz):
    api = UsdGeom.Xformable(prim)
    api.ClearXformOpOrder()
    api.AddTranslateOp().Set(Gf.Vec3d(*xyz))
    api.AddOrientOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Quatd(1))


def prepare_contact_reporting(stage):
    """Author contact schemas before world.reset creates PhysX tensor views."""
    for prim in stage.Traverse():
        if prim.HasAPI(UsdPhysics.RigidBodyAPI):
            PhysxSchema.PhysxContactReportAPI.Apply(prim).CreateThresholdAttr(0)


def prepare_robot(stage, robot_prim, config):
    profile = json.loads(Path(config['profile_path']).read_text())
    source = profile['robot_source']
    mount = source['arm_mount_world']
    scenes=[p for p in stage.Traverse() if p.IsA(UsdPhysics.Scene)]
    if len(scenes)!=1:
        raise ValueError('Expected one physics scene for the robot')
    scene_api=PhysxSchema.PhysxSceneAPI.Apply(scenes[0])
    scene_api.CreateEnableExternalForcesEveryIterationAttr().Set(
        bool(source.get('enable_external_forces_every_iteration',False)))
    prim = stage.GetPrimAtPath(robot_prim)
    if not prim:
        raise ValueError('Robot USD reference is missing')
    initial_base=profile.get('base',{}).get('initial_pose',[0.,0.,0.]) if config.get('mobile',False) else [0.,0.,0.]
    if initial_base[2] != 0:
        raise ValueError('This initial model composition requires zero yaw; navigation may rotate physically')
    _pose(prim, [mount[0]+initial_base[0],mount[1]+initial_base[1],mount[2]])
    # The vendor gripper is referenced as a standalone articulation. In the
    # combined robot its fixed flange joint belongs to the arm articulation.
    gripper_root = stage.GetPrimAtPath(robot_prim + '/gripper/root_joint')
    if gripper_root.HasAPI(UsdPhysics.ArticulationRootAPI):
        gripper_root.RemoveAPI(UsdPhysics.ArticulationRootAPI)
    root = UsdPhysics.FixedJoint(stage.GetPrimAtPath(robot_prim + '/root_joint'))
    root.CreateLocalPos0Attr(Gf.Vec3f(*mount))
    for i, name in enumerate(profile['arm_joint_names'] + profile['gripper_joint_names']):
        path = robot_prim + ('/gripper/joints/' if name == 'drive_joint' else '/joints/') + name
        joint = stage.GetPrimAtPath(path)
        if not joint:
            raise ValueError('USD joint missing: ' + name)
        lim = profile['joint_limits'][name]
        physics_joint = UsdPhysics.RevoluteJoint(joint)
        physics_joint.CreateLowerLimitAttr(math.degrees(lim['lower']))
        physics_joint.CreateUpperLimitAttr(math.degrees(lim['upper']))
        drive = UsdPhysics.DriveAPI.Apply(joint, 'angular')
        drive.CreateTypeAttr('force')
        drive.CreateMaxForceAttr(lim['effort'])
        stiffness = source['arm_stiffness'][i] if i < 6 else source['gripper_stiffness']
        damping = source['arm_damping'][i] if i < 6 else source['gripper_damping']
        drive.CreateStiffnessAttr(stiffness * math.pi / 180)
        drive.CreateDampingAttr(damping * math.pi / 180)
        q = source['initial_joint_positions'][i]
        drive.CreateTargetPositionAttr(math.degrees(q))
        state = PhysxSchema.JointStateAPI.Apply(joint, 'angular')
        state.CreatePositionAttr(math.degrees(q))
        state.CreateVelocityAttr(0)
    if config.get('mobile', False):
        add_mobile_base(stage, robot_prim, profile)
    effective_root=stage.GetPrimAtPath(robot_prim+('/base_link' if config.get('mobile',False) else '/root_joint'))
    articulation_api=PhysxSchema.PhysxArticulationAPI.Apply(effective_root)
    articulation_api.CreateSleepThresholdAttr(float(source.get('articulation_sleep_threshold',0.005)))
    articulation_api.CreateSolverPositionIterationCountAttr(int(source.get('solver_position_iterations',32)))
    articulation_api.CreateSolverVelocityIterationCountAttr(int(source.get('solver_velocity_iterations',1)))
    # All scene bodies and mobile links now exist, but world.reset has not
    # parsed them into PhysX. Runtime evaluators only subscribe and read back.
    prepare_contact_reporting(stage)


def add_mobile_base(stage, robot_prim, profile):
    base = profile['base']
    height = profile['robot_source']['base']['height']
    mount = profile['robot_source']['arm_mount_world']
    # Child coordinates below use world relative to the asset's mount transform.
    chassis_path = robot_prim + '/base_link'
    chassis = UsdGeom.Xform.Define(stage, chassis_path)
    _pose(chassis.GetPrim(), (0, 0, height - mount[2]))
    geometry = UsdGeom.Cube.Define(stage, chassis_path + '/collision')
    geometry.CreateSizeAttr(1)
    geometry.AddScaleOp().Set(Gf.Vec3f(.55, .56, .18))
    geometry.CreateDisplayColorAttr([Gf.Vec3f(.13, .17, .20)])
    UsdPhysics.CollisionAPI.Apply(geometry.GetPrim())
    UsdPhysics.RigidBodyAPI.Apply(chassis.GetPrim())
    UsdPhysics.MassAPI.Apply(chassis.GetPrim()).CreateMassAttr(profile['robot_source']['base']['mass'])
    root = stage.GetPrimAtPath(robot_prim + '/root_joint')
    root.SetActive(False)
    UsdPhysics.ArticulationRootAPI.Apply(chassis.GetPrim())
    mount_joint = UsdPhysics.FixedJoint.Define(stage, robot_prim + '/base_mount_joint')
    mount_joint.CreateBody0Rel().SetTargets([Sdf.Path(chassis_path)])
    mount_joint.CreateBody1Rel().SetTargets([Sdf.Path(robot_prim + '/world')])
    mount_joint.CreateLocalPos0Attr(Gf.Vec3f(0, 0, mount[2] - height))
    mount_joint.CreateLocalPos1Attr(Gf.Vec3f(0))
    for side, sign in [('left', 1), ('right', -1)]:
        for offset, name in zip((.20, -.20), base['wheel_joints'][side]):
            wheel_path = robot_prim + '/' + name.removesuffix('_joint')
            wheel = UsdGeom.Cylinder.Define(stage, wheel_path)
            wheel.CreateRadiusAttr(base['wheel_radius'])
            wheel.CreateHeightAttr(.06)
            wheel.CreateAxisAttr('Y')
            y = sign * base['wheel_separation'] / 2
            _pose(wheel.GetPrim(), (offset, y, base['wheel_radius'] - mount[2]))
            wheel.CreateDisplayColorAttr([Gf.Vec3f(.06, .06, .06)])
            UsdPhysics.CollisionAPI.Apply(wheel.GetPrim())
            UsdPhysics.RigidBodyAPI.Apply(wheel.GetPrim())
            UsdPhysics.MassAPI.Apply(wheel.GetPrim()).CreateMassAttr(1.0)
            joint = UsdPhysics.RevoluteJoint.Define(stage, robot_prim + '/base_joints/' + name)
            joint.CreateBody0Rel().SetTargets([Sdf.Path(chassis_path)])
            joint.CreateBody1Rel().SetTargets([Sdf.Path(wheel_path)])
            joint.CreateAxisAttr('Y')
            joint.CreateLocalPos0Attr(Gf.Vec3f(offset, y, base['wheel_radius'] - height))
            joint.CreateLocalPos1Attr(Gf.Vec3f(0))
            drive = UsdPhysics.DriveAPI.Apply(joint.GetPrim(), 'angular')
            drive.CreateTypeAttr('force')
            drive.CreateStiffnessAttr(0)
            drive.CreateDampingAttr(20 * math.pi / 180)
            drive.CreateTargetVelocityAttr(0)
            drive.CreateMaxForceAttr(base['wheel_effort_limit'])
    # Track shells are visual only. Wheel contact is the documented equivalent
    # differential model, not a simulation of individual track links.
    for side, sign in [('left', 1), ('right', -1)]:
        shell = UsdGeom.Cube.Define(stage, chassis_path + '/' + side + '_track_shell')
        shell.CreateSizeAttr(1)
        shell.AddTranslateOp().Set(Gf.Vec3d(0, sign * .32, -.02))
        shell.AddScaleOp().Set(Gf.Vec3f(.62, .04, .20))
        shell.CreateDisplayColorAttr([Gf.Vec3f(.08, .09, .1)])
