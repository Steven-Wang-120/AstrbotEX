#!/usr/bin/env bash
set -eo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ROS_DISTRO="${ROS_DISTRO:-humble}"
source "/opt/ros/${ROS_DISTRO}/setup.bash"
INTERFACE_INSTALL="${ASTRBOTEX_ROS_INTERFACES:-${PROJECT_ROOT}/ros_interfaces/install}"
source "${INTERFACE_INSTALL}/setup.bash"
if [[ -n "${ASTRBOTEX_ROS_OVERLAY:-}" ]]; then
  source "${ASTRBOTEX_ROS_OVERLAY}/setup.bash"
fi
PYTHON_BIN="${ASTRBOTEX_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then PYTHON_BIN=python3; fi
cd "$PROJECT_ROOT"
exec "$PYTHON_BIN" -m astrbot_ex.core.api_server --host "${ASTRBOTEX_HOST:-127.0.0.1}" \
  --port "${ASTRBOTEX_PORT:-8765}" --tick-hz "${ASTRBOTEX_TICK_HZ:-20}"
