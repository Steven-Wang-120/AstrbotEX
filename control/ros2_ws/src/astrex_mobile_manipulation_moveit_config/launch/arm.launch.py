"""Start only-planning MoveIt and standard topic controllers; no fake hardware."""
import os
from pathlib import Path
import yaml
from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    root = Path(os.environ['ASTREX_MM_ROOT'])
    model = root / 'runtime/mobile_manipulation_deps/model'
    description = (model / 'robot.urdf').read_text()
    planning = yaml.safe_load((model / 'moveit.yaml').read_text())['/astrex/mm/move_group']['ros__parameters']
    return LaunchDescription([
        Node(package='robot_state_publisher', executable='robot_state_publisher', namespace='/astrex/mm',
             parameters=[{'robot_description': description, 'use_sim_time': True}],
             remappings=[('joint_states', '/astrex/mm/joint_states_raw')], output='screen'),
        Node(package='controller_manager', executable='ros2_control_node', namespace='/astrex/mm',
             parameters=[str(model / 'controllers.yaml')],
             arguments=['--ros-args', '-p', 'use_sim_time:=true'],
             remappings=[('~/robot_description', '/astrex/mm/robot_description'),
                         ('robot_description', '/astrex/mm/robot_description')], output='screen'),
        Node(package='controller_manager', executable='spawner',
             arguments=['joint_state_broadcaster', 'arm_controller', 'gripper_controller',
                        '--controller-manager', '/astrex/mm/controller_manager',
                        '--controller-manager-timeout', '120', '--switch-timeout', '120'], output='screen'),
        Node(package='moveit_ros_move_group', executable='move_group', namespace='/astrex/mm',
             parameters=[planning], remappings=[('joint_states', '/astrex/mm/joint_states_raw')], output='screen'),
    ])
