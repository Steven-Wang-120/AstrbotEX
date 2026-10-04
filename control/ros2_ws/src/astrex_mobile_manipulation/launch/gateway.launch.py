from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('profile'), DeclareLaunchArgument('evidence_dir'),
        Node(package='astrex_mobile_manipulation', executable='mm_gateway', name='mm_gateway',
             output='screen', parameters=[{'profile':LaunchConfiguration('profile'),
             'evidence_dir':LaunchConfiguration('evidence_dir'),'use_sim_time':True}])])
