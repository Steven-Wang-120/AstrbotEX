#!/usr/bin/env bash

export ASTREX_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
export ASTREX_DATA_ROOT=/data/shared/AstrEX_project_data

source /opt/ros/jazzy/setup.bash

if [ -f "$ASTREX_ROOT/ros2_ws/install/setup.bash" ]; then
    source "$ASTREX_ROOT/ros2_ws/install/setup.bash"
fi
