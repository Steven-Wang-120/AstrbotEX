#!/usr/bin/env bash
set -e
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
source "$root/scripts/mobile_manipulation_env.sh"
cd "$root/ros2_ws"
exec /usr/bin/colcon build --packages-select astrex_mobile_manipulation astrex_mobile_manipulation_moveit_config --symlink-install --cmake-args -DPython3_EXECUTABLE=/usr/bin/python3 "$@"
