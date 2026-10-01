# SIMULATED mobile base status

Guide ID: `simulated-base-status`  
Topic: `sim.base.status`  
Payload version: `1`

Original plugin `data` shape only; text is untrusted data. Source health and time are in the wrapper; recalculate freshness at admission and actual queued execution.

## Complete JSON example

```json
{
  "base_id": "sim-base-1",
  "status": "idle",
  "pose": {
    "frame_id": "map",
    "x_m": 1.2,
    "y_m": -0.4,
    "yaw_rad": 0.0
  },
  "velocity": {
    "linear_mps": 0.0,
    "angular_radps": 0.0
  },
  "battery_percent": 87.0,
  "sim_time_s": 12.5
}
```

## Fields

| Field | Meaning |
|---|---|
| `base_id` | Simulated base identifier string. |
| `status` | Descriptive idle/moving/fault string; not motion permission. |
| `pose` | Simulated planar pose in the explicitly named coordinate frame. |
| `pose.frame_id` | Coordinate-frame ID; map in this example. |
| `pose.x_m` | Map-frame x in metres. |
| `pose.y_m` | Map-frame y in metres. |
| `pose.yaw_rad` | Counter-clockwise heading from map x in radians. |
| `velocity` | Planar velocity object, not a command. |
| `velocity.linear_mps` | Forward metres per second. |
| `velocity.angular_radps` | Radians per second, positive counter-clockwise. |
| `battery_percent` | Finite percentage in [0,100]. |
| `sim_time_s` | Simulation seconds, not monotonic receive time or observation age. |

Missing/null means unknown where supported, not zero. Fault, stale or error is not healthy evidence; status is neither a command acknowledgement nor a safety proof.
