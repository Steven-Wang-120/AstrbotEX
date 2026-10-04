#!/usr/bin/env bash
# Source in a dedicated system-ROS shell; never source in the Isaac process.
ASTREX_MM_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
export ASTREX_MM_ROOT
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER CONDA_SHLVL
unset PYTHONHOME PYTHONPATH LD_LIBRARY_PATH AMENT_PREFIX_PATH COLCON_PREFIX_PATH CMAKE_PREFIX_PATH VIRTUAL_ENV
export PATH="/usr/bin:/bin:$PATH"
source /opt/ros/jazzy/setup.bash
ASTREX_MM_DEPS="$ASTREX_MM_ROOT/runtime/mobile_manipulation_deps/root"
ASTREX_MM_PREFIX="$ASTREX_MM_DEPS/opt/ros/jazzy"
export AMENT_PREFIX_PATH="$ASTREX_MM_PREFIX:${AMENT_PREFIX_PATH:-}"
export CMAKE_PREFIX_PATH="$ASTREX_MM_PREFIX:${CMAKE_PREFIX_PATH:-}"
export LD_LIBRARY_PATH="$ASTREX_MM_PREFIX/lib:$ASTREX_MM_DEPS/usr/lib/x86_64-linux-gnu:$ASTREX_MM_DEPS/usr/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="$ASTREX_MM_PREFIX/lib/python3.12/site-packages:$ASTREX_MM_PREFIX/local/lib/python3.12/dist-packages:${PYTHONPATH:-}"
export PATH="$ASTREX_MM_PREFIX/bin:$PATH"
export ROS_DOMAIN_ID=63 RMW_IMPLEMENTATION=rmw_fastrtps_cpp PYTHONDONTWRITEBYTECODE=1
if [ -f "$ASTREX_MM_ROOT/ros2_ws/install/local_setup.bash" ]; then
    source "$ASTREX_MM_ROOT/ros2_ws/install/local_setup.bash"
fi
