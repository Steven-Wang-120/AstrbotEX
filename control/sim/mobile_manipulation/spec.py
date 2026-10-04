"""Dependency-free sensor and scene configuration for the Isaac entry point."""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

PROFILES = {
    "RGB_LIDAR_2D": {"asset": "Example_Rotary_2D", "type": "laser_scan", "topic": "/astrex/mm/scan"},
    "RGB_LIDAR_3D": {"asset": "Example_Rotary", "type": "point_cloud", "topic": "/astrex/mm/points"},
}

@dataclass(frozen=True)
class SensorSpec:
    width: int = 640
    height: int = 480
    render_hz: int = 60
    camera_hz: int = 15
    lidar_hz: int = 10
    lidar_min_range: float | None = None
    lidar_max_range: float | None = None
    camera_position: tuple = (1.25, -1.25, 1.6)
    camera_target: tuple = (0.55, 0.0, 0.76)
    lidar_position: tuple = (0.0, -0.8, 0.3)
    table_position: tuple = (0.60, 0.0, 0.375)
    table_dimensions: tuple = (1.0, 0.8, 0.75)
    frame_id: str = "world"
    camera_frame: str = "mm_camera_optical"
    lidar_frame: str = "mm_lidar"
    calibration_id: str = "fixed_rig_v1"

    def validate(self):
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Camera resolution must be positive")
        for rate in (self.camera_hz, self.lidar_hz):
            if rate <= 0 or self.render_hz % rate:
                raise ValueError("Sensor rates must divide the render rate")
        return self

def config_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def load_spec(path=None):
    raw = json.loads(Path(path).read_text()) if path else {}
    spec = SensorSpec(**raw.get("sensors", {})).validate()
    return spec, raw

def effective_config(spec, profile, seed, mode):
    if profile not in PROFILES:
        raise ValueError("Unsupported sensor profile")
    value = {"mode": mode, "scene_seed": seed, "sensor_profile_id": profile,
             "sensors": asdict(spec), "lidar": {**PROFILES[profile],
                 "scan_rate_hz": spec.lidar_hz, "tick_rate_hz": spec.render_hz,
                 "publication": "complete_scan"},
             "input_source": "RAW_SENSOR", "perception_backend": None}
    value["config_hash"] = config_hash(value)
    return value

def obstacle_layout(scene_config=None):
    """Resolve fixed P1 geometry or an explicitly seeded M1 development layout."""
    import math
    import random
    config = (scene_config or {}).get("navigation_obstacles")
    defaults = [
        {"position": [-0.8, -0.3, 0.25], "dimensions": [0.35, 0.35, 0.5]},
        {"position": [1.3, -0.65, 0.30], "dimensions": [0.3, 0.3, 0.6]},
        {"position": [0.7, 1.0, 0.25], "dimensions": [0.4, 0.3, 0.5]},
    ]
    if config is None:
        return defaults
    seed = config["seed"]
    jitter = config.get("xy_jitter_m", 0.0)
    if type(seed) is not int or seed < 0 or not math.isfinite(jitter) or jitter < 0:
        raise ValueError("Navigation obstacle seed/jitter are invalid")
    rng = random.Random(seed)
    result = []
    for row in defaults + config.get("additional", []):
        position, dimensions = list(row["position"]), list(row["dimensions"])
        if len(position) != 3 or len(dimensions) != 3 or not all(math.isfinite(x) for x in position + dimensions) or min(dimensions) <= 0:
            raise ValueError("Obstacle position/dimensions must be finite 3-vectors")
        position[0] += rng.uniform(-jitter, jitter)
        position[1] += rng.uniform(-jitter, jitter)
        result.append({"position": position, "dimensions": dimensions})
    return result
