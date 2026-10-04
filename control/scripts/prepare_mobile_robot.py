#!/usr/bin/env python3
"""Expand the pinned vendor model and prepare one ROS/Isaac robot profile."""
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import xacro
import yaml

ROOT = Path(__file__).resolve().parents[1]


def prepare(root=ROOT):
    cfg = json.loads((root / 'config/mobile_manipulation/robot_source.json').read_text())
    vendor = root / 'runtime/mobile_manipulation_deps/xarm_ros2'
    revision = subprocess.check_output(['git', '-C', str(vendor), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != cfg['vendor_revision']:
        raise ValueError('Vendor revision differs from the locked robot configuration')
    out = root / 'runtime/mobile_manipulation_deps/model'
    out.mkdir(parents=True, exist_ok=True)
    tree = ET.fromstring(xacro.process_file(str(vendor / 'xarm_description/urdf/xarm_device.urdf.xacro'),
        mappings={'dof': '6', 'add_gripper': 'true',
                  'ros2_control_plugin': 'joint_state_topic_hardware_interface/JointStateTopicSystem'}).toxml())
    # The physical scene uses Isaac drives, not vendor hardware or Gazebo plugins.
    for tag in ('ros2_control', 'gazebo', 'transmission'):
        for item in list(tree.findall(tag)):
            tree.remove(item)
    tree.set('name', 'UF_ROBOT')
    mount = tree.find("joint[@name='world_joint']/origin")
    mount.set('xyz', ' '.join(map(str, cfg['arm_mount_world'])))
    arm = [f'joint{i}' for i in range(1, 7)]
    gripper = ['drive_joint']
    vendor_limits = yaml.safe_load((vendor / 'xarm_moveit_config/config/xarm6/joint_limits.yaml').read_text())['joint_limits']
    limits = {}
    for name in arm + gripper:
        joint = tree.find(f"joint[@name='{name}']")
        row = joint.find('limit').attrib
        dynamic = vendor_limits.get(name, {'max_velocity': .5, 'max_acceleration': 1.0})
        limits[name] = {'lower': float(row['lower']), 'upper': float(row['upper']),
            'velocity': min(float(row['velocity']), dynamic['max_velocity']),
            'acceleration': dynamic['max_acceleration'],
            'effort': min(float(row['effort']), cfg['gripper_effort_nm']) if name in gripper else float(row['effort'])}
    control = ET.SubElement(tree, 'ros2_control', name='IsaacTopicSystem', type='system')
    hardware = ET.SubElement(control, 'hardware')
    ET.SubElement(hardware, 'plugin').text = 'joint_state_topic_hardware_interface/JointStateTopicSystem'
    for name, value in {'joint_commands_topic': '/astrex/mm/joint_command_raw',
                        'joint_states_topic': '/astrex/mm/joint_states_raw',
                        'trigger_joint_command_threshold': '-1', 'sum_wrapped_joint_states': 'false'}.items():
        ET.SubElement(hardware, 'param', name=name).text = value
    for name in arm + gripper:
        joint = ET.SubElement(control, 'joint', name=name)
        ET.SubElement(joint, 'command_interface', name='position')
        for interface in ('position', 'velocity'):
            ET.SubElement(joint, 'state_interface', name=interface)
    ET.indent(tree)
    (out / 'robot.urdf').write_text(ET.tostring(tree, encoding='unicode') + '\n')
    srdf = xacro.process_file(str(vendor / 'xarm_moveit_config/srdf/xarm.srdf.xacro'),
                             mappings={'dof': '6', 'add_gripper': 'true'}).toprettyxml(indent='  ')
    (out / 'robot.srdf').write_text(srdf)
    profile = {'arm_joint_names': arm, 'gripper_joint_names': gripper, 'joint_limits': limits,
        'group_name': 'xarm6', 'base_frame': 'link_base', 'tcp_frame': 'link_tcp',
        'velocity_scale': cfg['velocity_scale'], 'acceleration_scale': cfg['acceleration_scale'],
        'robot_source': cfg, 'gripper_touch_links': ['left_finger', 'right_finger'],
        'allowed_support_contacts': cfg['allowed_support_contacts'],
        'arm_stiffness': cfg['arm_stiffness'], 'arm_damping': cfg['arm_damping'],
        'initial_joint_positions': cfg['initial_joint_positions'],
        'gripper_stiffness': cfg['gripper_stiffness'], 'gripper_damping': cfg['gripper_damping'],
        'gripper_velocity': cfg['gripper_velocity'],
        'model_sha256': hashlib.sha256((out / 'robot.urdf').read_bytes()).hexdigest(),
        'srdf_sha256': hashlib.sha256(srdf.encode()).hexdigest()}
    (out / 'robot_profile.json').write_text(json.dumps(profile, indent=2) + '\n')
    mobile = dict(profile)
    mobile['base'] = {'wheel_joints': {'left': ['left_front_wheel_joint', 'left_rear_wheel_joint'], 'right': ['right_front_wheel_joint', 'right_rear_wheel_joint']}, 'wheel_radius': cfg['base']['wheel_radius'], 'wheel_separation': cfg['base']['wheel_separation'], 'wheel_effort_limit': cfg['base']['wheel_effort'], 'initial_pose': cfg['base'].get('initial_pose', [-1.5, -1.5, 0.0]), 'base_link_height': cfg['base']['height'], 'max_linear_velocity': cfg['base']['max_linear_velocity'], 'max_angular_velocity': cfg['base']['max_angular_velocity']}
    (out / 'mobile_robot_profile.json').write_text(json.dumps(mobile, indent=2) + '\n')
    controllers = {'/**': {'ros__parameters': {'use_sim_time': True}}, '/astrex/mm/controller_manager': {'ros__parameters': {
        'update_rate': 100, 'use_sim_time': True,
        'joint_state_broadcaster': {'type': 'joint_state_broadcaster/JointStateBroadcaster'},
        'arm_controller': {'type': 'joint_trajectory_controller/JointTrajectoryController'},
        'gripper_controller': {'type': 'parallel_gripper_action_controller/GripperActionController'}}},
        '/astrex/mm/arm_controller': {'ros__parameters': {'joints': arm,
            'command_interfaces': ['position'], 'state_interfaces': ['position', 'velocity'],
            'allow_partial_joints_goal': False, 'constraints': {'goal_time': 5.0, 'stopped_velocity_tolerance': .02}}},
        '/astrex/mm/gripper_controller': {'ros__parameters': {'joint': 'drive_joint',
            'state_interfaces': ['position', 'velocity'], 'allow_stalling': True,
            'stall_timeout': 1.0, 'goal_tolerance': .01, 'max_effort': cfg['gripper_effort_nm']}}}
    (out / 'controllers.yaml').write_text(yaml.safe_dump(controllers, sort_keys=False))
    joint_limits = {n: {'has_velocity_limits': True, 'max_velocity': lim['velocity'],
                       'has_acceleration_limits': True, 'max_acceleration': lim['acceleration']}
                    for n, lim in limits.items()}
    moveit = {'robot_description': (out / 'robot.urdf').read_text(), 'robot_description_semantic': srdf,
        'robot_description_kinematics': yaml.safe_load((vendor / 'xarm_moveit_config/config/xarm6/kinematics.yaml').read_text()),
        'robot_description_planning': {'joint_limits': joint_limits},
        'planning_pipelines': ['ompl'], 'default_planning_pipeline': 'ompl',
        'ompl': {'planning_plugins': ['ompl_interface/OMPLPlanner'],
            'request_adapters': ['default_planning_request_adapters/ResolveConstraintFrames',
                'default_planning_request_adapters/ValidateWorkspaceBounds',
                'default_planning_request_adapters/CheckStartStateBounds',
                'default_planning_request_adapters/CheckStartStateCollision'],
            'response_adapters': ['default_planning_response_adapters/AddTimeOptimalParameterization',
                                  'default_planning_response_adapters/ValidateSolution'],
            'planner_configs': {'RRTConnectkConfigDefault': {'type': 'geometric::RRTConnect', 'range': 0.0}},
            'xarm6': {'planner_configs': ['RRTConnectkConfigDefault'],
                      'longest_valid_segment_fraction': .005}},
        'allow_trajectory_execution': False, 'use_sim_time': True,
        'publish_robot_description_semantic': True,
        'planning_scene_monitor_options': {'name': 'planning_scene_monitor', 'robot_description': 'robot_description',
            'joint_state_topic': '/astrex/mm/joint_states_raw',
            'attached_collision_object_topic': '/astrex/mm/attached_collision_object',
            'publish_planning_scene_topic': '/astrex/mm/publish_planning_scene',
            'monitored_planning_scene_topic': '/astrex/mm/monitored_planning_scene',
            'wait_for_initial_state_timeout': 10.0}}
    (out / 'moveit.yaml').write_text(yaml.safe_dump({'/astrex/mm/move_group': {'ros__parameters': moveit}}, sort_keys=False))
    sim_config = {'robot': {'arm_mount_world': cfg['arm_mount_world'], 'mobile': False},
                  'execution': {'profile_path': str(out / 'robot_profile.json')}}
    (out / 'simulation.json').write_text(json.dumps(sim_config, indent=2) + '\n')
    print(json.dumps({'model_dir': str(out), 'vendor_revision': revision,
        'arm_joints': arm, 'controlled_gripper': gripper, 'model_sha256': profile['model_sha256']}, indent=2))
    return out


if __name__ == '__main__':
    prepare()
