"""Single source for the MVP skill declarations and supported task vocabulary."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from astrbot_ex.core.actions.models import parse_action_manifest, validate_params

OWNER = "mobile_manipulation"
ACTION_IDS = {skill: f"{OWNER}.{skill}.v1" for skill in ("move", "fetch")}
COLORS = {"红": "red", "蓝": "blue", "绿": "green"}
SHAPES = {"方块": "cube", "圆柱": "cylinder"}
REGIONS = {"操作台停车区": "table_dock", "左托盘": "tray_left", "右托盘": "tray_right", "起点区": "home"}
SUCCESS = {"move": "move.arrive_and_stop.v1", "fetch": "fetch.pick_place_stable.v1"}
NAV_TOLERANCES = (.10, .05)
YAW_TOLERANCES = (math.radians(10), math.radians(5))
PLACE_TOLERANCES = (.020, .010)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def _enum(values, kind="string"):
    return {"type": kind, "enum": list(values)}

def action_manifest():
    common = {"resources": ["mobile.base", "mobile.arm", "mobile.gripper"],
              "operations": ["start", "cancel"], "danger": "high",
              "max_duration_ms": 120000, "cancel_timeout_ms": 5000}
    props = {
        "move": {"target_region_id": _enum(("table_dock", "home")),
                 "position_tolerance_m": _enum(NAV_TOLERANCES, "number"),
                 "yaw_tolerance_rad": _enum(YAW_TOLERANCES, "number"),
                 "success_template": _enum((SUCCESS["move"],))},
        "fetch": {"object_id": {"type": "string", "minLength": 1, "maxLength": 128},
                  "place_region_id": _enum(("tray_left", "tray_right")),
                  "placement_tolerance_m": _enum(PLACE_TOLERANCES, "number"),
                  "orientation_mode": _enum(("free", "yaw")),
                  "yaw_tolerance_rad": _enum((math.radians(5),), "number"),
                  "success_template": _enum((SUCCESS["fetch"],))}}
    actions = []
    for skill in ACTION_IDS:
        actions.append({**copy.deepcopy(common), "action_id": ACTION_IDS[skill],
            "description": "Move base to a named region and stop." if skill == "move" else "With base stopped, pick the named object and place it in the named tray.",
            "schema": {"type": "object", "properties": props[skill],
                       "required": list(props[skill]), "additionalProperties": False}})
    return parse_action_manifest({"id": OWNER, "action_api_version": 2, "actions": actions}, owner=OWNER)

def skill_catalog():
    manifest = action_manifest().to_dict()
    return {"revision": digest(manifest), "owner": OWNER, "actions": manifest["actions"],
            "success_templates": dict(SUCCESS), "regions": dict(REGIONS),
            "colors": dict(COLORS), "shapes": dict(SHAPES)}

def validate_skill_parameters(skill, parameters):
    if skill not in ACTION_IDS:
        raise ValueError("unsupported_skill")
    action = action_manifest().by_id(ACTION_IDS[skill])
    errors = validate_params(action.schema, parameters)
    if errors:
        raise ValueError("invalid_skill_parameters: " + str(errors[0]))
    return copy.deepcopy(parameters)


def project_catalog(snapshot):
    """Project the real EX capability snapshot without changing availability.

    The caller passes CapabilitySnapshot or its public dictionary. An unavailable
    plugin remains unavailable; this function grants no execution authority.
    """
    raw = snapshot.to_dict() if hasattr(snapshot, "to_dict") else copy.deepcopy(snapshot)
    entries = [entry for entry in raw["entries"] if entry["owner"] == OWNER]
    if len(entries) != 1:
        raise ValueError("mobile_skill_owner_unavailable")
    entry = entries[0]
    actual = parse_action_manifest(entry["manifest"], owner=OWNER)
    expected = action_manifest()
    for skill, action_id in ACTION_IDS.items():
        action = actual.by_id(action_id)
        if action is None or action.schema != expected.by_id(action_id).schema:
            raise ValueError("skill_schema_mismatch")
    result = skill_catalog()
    result.update(source_catalog_revision=raw["revision"], available=entry["available"],
                  unavailable_reason=entry.get("unavailable_reason", ""),
                  actions=[a.to_dict() for a in actual.actions if a.action_id in ACTION_IDS.values()])
    result["revision"] = digest(result)
    return result
