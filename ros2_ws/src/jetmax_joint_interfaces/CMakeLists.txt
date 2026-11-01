cmake_minimum_required(VERSION 3.16)
project(jetmax_joint_interfaces)
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(action_msgs REQUIRED)
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/JointState.msg" "srv/StopJoint.srv"
  "action/MoveServoRaw.action" "action/SetGripperAngle.action"
  DEPENDENCIES builtin_interfaces action_msgs)
ament_export_dependencies(rosidl_default_runtime)
ament_package()
