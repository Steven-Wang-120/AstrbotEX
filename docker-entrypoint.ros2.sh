#!/bin/bash
set -e
# Deployment paths; never paths supplied by the HTTP API.
source /opt/ros/${ROS_DISTRO:?ROS_DISTRO must be defined by the image}/setup.bash
source /opt/astrbotex_interfaces/install/setup.bash
if [[ -n "$ASTRBOTEX_ROS_OVERLAY" ]]; then
    if [[ ! -r "$ASTRBOTEX_ROS_OVERLAY/setup.bash" ]]; then
        echo "Configured ROS overlay has no readable setup.bash" >&2
        exit 1
    fi
    source "$ASTRBOTEX_ROS_OVERLAY/setup.bash"
fi
exec /app/docker-entrypoint.sh "$@"
