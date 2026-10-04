"""Minimal navigation graph; no Gazebo, SLAM, true obstacle map or actuator path."""
from pathlib import Path
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

def generate_launch_description():
    share=Path(get_package_share_directory('astrex_mobile_manipulation'))
    config=share/'config'
    params=LaunchConfiguration('params_file')
    nodes=[]
    specs=[('nav2_map_server','map_server'),('nav2_planner','planner_server'),
        ('nav2_controller','controller_server'),('nav2_bt_navigator','bt_navigator'),
        ('nav2_collision_monitor','collision_monitor')]
    for package,name in specs:
        extra={'use_sim_time':True}
        if name=='map_server': extra['yaml_filename']=str(config/'boundary_map.yaml')
        if name=='bt_navigator': extra['default_nav_to_pose_bt_xml']=str(config/'navigation_bt.xml')
        nodes.append(Node(package=package,executable=name,name=name,namespace='/astrex/mm',output='screen',
            parameters=[params,extra],remappings=[('cmd_vel','nav_cmd_vel')]))
    nodes.append(Node(package='nav2_lifecycle_manager',executable='lifecycle_manager',
        name='navigation_lifecycle',namespace='/astrex/mm',output='screen',
        parameters=[{'use_sim_time':True,'autostart':True,'bond_timeout':4.,
          'node_names':[name for _,name in specs]}]))
    nodes.append(Node(package='tf2_ros',executable='static_transform_publisher',name='known_map_alignment',
        arguments=['--x','0','--y','0','--z','0','--yaw','0','--pitch','0','--roll','0',
                   '--frame-id','map','--child-frame-id','odom']))
    return LaunchDescription([DeclareLaunchArgument('params_file',default_value=str(config/'nav2.yaml')),*nodes])
