from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def build(context):
    args = ['--backend', LaunchConfiguration('backend').perform(context)]
    config = LaunchConfiguration('config').perform(context)
    if config:
        args += ['--config', config]
    enabled = LaunchConfiguration('enable_control').perform(context).lower()
    if enabled not in ('true', 'false'):
        raise ValueError('enable_control must be true or false')
    if enabled == 'true':
        args.append('--enable-control')
    # Do not set name: a global __node remap would rename all four nodes alike.
    return [Node(package='jetmax_joint_nodes', executable='joint_nodes', arguments=args, output='screen')]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('backend', default_value='real', choices=['real', 'fake']),
        DeclareLaunchArgument('config', default_value=''),
        DeclareLaunchArgument('enable_control', default_value='false'),
        OpaqueFunction(function=build),
    ])
