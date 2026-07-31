from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class CameraConfig:
    hfov_deg: float
    image_width_px: int
    image_height_px: int
    to_lidar_yaw_offset_deg: float = 0.0
    x_to_lidar_angle_sign: float = -1.0


@dataclass(slots=True)
class FusionConfig:
    bearing_tolerance_deg: float
    time_window_ms: float
    range_min_m: float
    range_max_m: float
    range_window_deg: float = 3.0
    range_select_method: str = "min"


@dataclass(slots=True)
class PerceptionConfig:
    camera: CameraConfig
    fusion: FusionConfig


DEFAULT_PERCEPTION_CONFIG: dict[str, object] = {
    "_comment": "相机与雷达的对齐标定参数。角度单位为度，距离单位为米。",
    "camera": {
        "hfov_deg": 90.0,
        "image_width_px": 640,
        "image_height_px": 480,
        "to_lidar_yaw_offset_deg": 0.0,
        "x_to_lidar_angle_sign": -1.0,
    },
    "fusion": {
        "bearing_tolerance_deg": 8.0,
        "time_window_ms": 150,
        "range_min_m": 0.05,
        "range_max_m": 12.0,
        "range_window_deg": 3.0,
        "range_select_method": "min",
    },
}


def load_perception_config(path: Path) -> PerceptionConfig:
    """Load calibration for camera-to-lidar bearing and weak range fusion.

    camera.hfov_deg maps bbox center x to bearing across the full horizontal FOV.
    camera.to_lidar_yaw_offset_deg is added to the camera bearing to compensate
    physical yaw offset from lidar 0 degrees. camera.x_to_lidar_angle_sign must
    be 1.0 or -1.0 and controls whether image x and lidar-positive angles share
    direction. fusion.bearing_tolerance_deg and fusion.time_window_ms bound
    spatial and timestamp matching. fusion.range_min_m/range_max_m bound valid
    ranges. fusion.range_window_deg is the half-width sampled around a bearing,
    and fusion.range_select_method chooses min or median inside that window.
    """
    if not path.exists():
        _write_default(path)
        return _parse_config(DEFAULT_PERCEPTION_CONFIG)

    raw = json.loads(path.read_text(encoding="utf-8"))
    return _parse_config(raw)


def _write_default(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(DEFAULT_PERCEPTION_CONFIG, ensure_ascii=False, indent=2)
    path.write_text(f"{payload}\n", encoding="utf-8")


def _parse_config(raw: object) -> PerceptionConfig:
    root = _object(raw, "config")
    camera = _object(_required(root, "camera", "camera"), "camera")
    fusion = _object(_required(root, "fusion", "fusion"), "fusion")

    camera_config = CameraConfig(
        hfov_deg=_valid_hfov(_number(camera, "hfov_deg", "camera.hfov_deg")),
        image_width_px=_integer(camera, "image_width_px", "camera.image_width_px"),
        image_height_px=_integer(camera, "image_height_px", "camera.image_height_px"),
        to_lidar_yaw_offset_deg=_number(
            camera,
            "to_lidar_yaw_offset_deg",
            "camera.to_lidar_yaw_offset_deg",
            default=0.0,
        ),
        x_to_lidar_angle_sign=_valid_angle_sign(
            _number(
                camera,
                "x_to_lidar_angle_sign",
                "camera.x_to_lidar_angle_sign",
                default=-1.0,
            )
        ),
    )
    fusion_config = FusionConfig(
        bearing_tolerance_deg=_positive(
            _number(fusion, "bearing_tolerance_deg", "fusion.bearing_tolerance_deg"),
            "fusion.bearing_tolerance_deg",
        ),
        time_window_ms=_positive(
            _number(fusion, "time_window_ms", "fusion.time_window_ms"),
            "fusion.time_window_ms",
        ),
        range_min_m=_number(fusion, "range_min_m", "fusion.range_min_m"),
        range_max_m=_number(fusion, "range_max_m", "fusion.range_max_m"),
        range_window_deg=_non_negative(
            _number(fusion, "range_window_deg", "fusion.range_window_deg", default=3.0),
            "fusion.range_window_deg",
        ),
        range_select_method=_valid_range_select_method(
            _string(fusion, "range_select_method", "fusion.range_select_method", default="min")
        ),
    )
    if fusion_config.range_max_m > 0.0 and fusion_config.range_min_m > fusion_config.range_max_m:
        raise ValueError(
            f"invalid field: fusion.range_min_m={fusion_config.range_min_m!r}; "
            f"expected <= fusion.range_max_m={fusion_config.range_max_m!r} "
            "or fusion.range_max_m == 0"
        )
    return PerceptionConfig(camera=camera_config, fusion=fusion_config)


def _object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"invalid field: {path}={value!r}; expected object")
    return value


def _required(container: dict[str, object], key: str, path: str) -> object:
    if key not in container:
        raise ValueError(f"missing field: {path}")
    return container[key]


def _number(container: dict[str, object], key: str, path: str, default: float | None = None) -> float:
    if key not in container:
        if default is None:
            raise ValueError(f"missing field: {path}")
        return default
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"invalid field: {path}={value!r}; expected number")
    return float(value)


def _integer(container: dict[str, object], key: str, path: str) -> int:
    value = _required(container, key, path)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid field: {path}={value!r}; expected integer")
    return value


def _string(container: dict[str, object], key: str, path: str, default: str | None = None) -> str:
    if key not in container:
        if default is None:
            raise ValueError(f"missing field: {path}")
        return default
    value = container[key]
    if not isinstance(value, str):
        raise ValueError(f"invalid field: {path}={value!r}; expected string")
    return value


def _valid_hfov(value: float) -> float:
    if not 0 < value < 180:
        raise ValueError(f"invalid field: camera.hfov_deg={value!r}; expected 0 < value < 180")
    return value


def _positive(value: float, path: str) -> float:
    if value <= 0:
        raise ValueError(f"invalid field: {path}={value!r}; expected value > 0")
    return value


def _non_negative(value: float, path: str) -> float:
    if value < 0:
        raise ValueError(f"invalid field: {path}={value!r}; expected value >= 0")
    return value


def _valid_angle_sign(value: float) -> float:
    if value not in {1.0, -1.0}:
        raise ValueError(
            f"invalid field: camera.x_to_lidar_angle_sign={value!r}; expected one of: -1.0, 1.0"
        )
    return value


def _valid_range_select_method(value: str) -> str:
    if value not in {"min", "median"}:
        raise ValueError(
            f"invalid field: fusion.range_select_method={value!r}; expected one of: min, median"
        )
    return value
