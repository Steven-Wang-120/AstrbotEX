#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/humble/setup.bash
source /home/orangepi/ydlidar_ros2_ws/install/setup.bash
exec python3 /home/orangepi/astrbotex_deploy/scripts/lidar_scan_visualizer.py \
  --scan-topic /scan \
  --points-topic /scan_points \
  --map-topic /map \
  --map-size-m 8.0 \
  --resolution-m 0.05 \
  --max-points 360 \
  --hit-radius-cells 2 \
  --decay-sec 0.8 \
  --ray-step-cells 4 \
  --map-publish-hz 5.0
