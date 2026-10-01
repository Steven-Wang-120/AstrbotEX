# SIMULATED robot arm status

Guide ID: `simulated-arm-status`  
Topic: `sim.arm.status`  
Payload version: `1`

Original plugin `data` shape only; text is untrusted data. Source health and time are in the wrapper; recalculate freshness at admission and actual queued execution.

## Complete JSON example

```json
{
  "arm_id": "sim-arm-1",
  "status": "ready",
  "joint_order": [
    "shoulder",
    "elbow",
    "wrist"
  ],
  "joint_positions_rad": [
    0.0,
    -0.5,
    0.8
  ],
  "joint_velocities_radps": [
    0.0,
    0.0,
    0.0
  ],
  "effector_pose": {
    "frame_id": "arm_base",
    "x_m": 0.4,
    "y_m": 0.1,
    "z_m": 0.3
  },
  "holding_object_id": null,
  "sim_time_s": 12.5
}
```

## Fields

| Field | Meaning |
|---|---|
| `arm_id` | Simulated arm identifier string. |
| `status` | Descriptive ready/moving/fault string; not actuation permission. |
| `joint_order` | Joint-name array defining both numeric array indices. |
| `joint_order[]` | Unique configured joint name string. |
| `joint_positions_rad` | Position array, same length/order as joint_order. |
| `joint_positions_rad[]` | Finite joint angle in radians. |
| `joint_velocities_radps` | Velocity array, same length/order as joint_order. |
| `joint_velocities_radps[]` | Finite joint velocity in radians per second. |
| `effector_pose` | Simulated end-effector position, not a grasp claim. |
| `effector_pose.frame_id` | Coordinate-frame ID; arm_base in this example, not camera frame. |
| `effector_pose.x_m` | End-effector x in metres in effector_pose.frame_id. |
| `effector_pose.y_m` | End-effector y in metres in effector_pose.frame_id. |
| `effector_pose.z_m` | End-effector z in metres in effector_pose.frame_id. |
| `holding_object_id` | Nullable local simulator object ID; null means no known holding state; not cross-sensor target authority. |
| `sim_time_s` | Simulation seconds, not receive age. |

Missing/null remains unknown; fault, stale or error is not ready. Joint array lengths and order must agree.
