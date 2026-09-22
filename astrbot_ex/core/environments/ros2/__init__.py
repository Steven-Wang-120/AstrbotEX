"""Optional ROS 2 environment implementation.

This package must remain importable when ROS 2 is not installed. The adapter
imports rclpy only after the ROS 2 environment is explicitly selected.
"""

from astrbot_ex.core.environments.ros2.adapter import Ros2EnvironmentAdapter

__all__ = ["Ros2EnvironmentAdapter"]
