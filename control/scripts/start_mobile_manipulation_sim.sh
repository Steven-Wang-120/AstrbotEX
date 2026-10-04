#!/usr/bin/env bash
# Keep Isaac's Python and bundled Jazzy separate from system ROS.
set -euo pipefail
script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
source "$script_root/scripts/lib/isaac_common.sh"
sim_site="$ASTREX_CONDA_ROOT/envs/$ASTREX_ISAAC_CONDA_ENV/lib/python3.11/site-packages/isaacsim"
export ROS_DISTRO="$ASTREX_ROS_DISTRO" ROS_DOMAIN_ID="$ASTREX_ROS_DOMAIN_ID"
export RMW_IMPLEMENTATION="$ASTREX_RMW_IMPLEMENTATION"
export LD_LIBRARY_PATH="$sim_site/exts/isaacsim.ros2.bridge/jazzy/lib"
exec python -B "$ASTREX_ROOT/sim/scripts/run_mobile_manipulation.py" "$@"
