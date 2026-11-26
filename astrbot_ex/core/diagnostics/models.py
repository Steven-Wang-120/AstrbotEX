"""JSON-only diagnostic values and stable identity helpers."""
import json

DATA_HEALTH = frozenset(("not_monitored", "disabled", "paused", "unavailable", "warming_up",
                         "no_data", "receiving", "stale", "unknown"))
SEVERITIES = frozenset(("info", "warning", "error", "critical"))
SCOPE_FIELDS = ("session_id", "environment_generation", "endpoint_id", "owner_generation",
                "binding_generation", "monitoring_epoch")
CODES = frozenset(("ROS_RX_NO_PEER", "ROS_RX_NO_DATA_WITH_PEER", "ROS_RX_STALE",
                   "ROS_QOS_INCOMPATIBLE", "ROS_INTERFACE_MISSING", "ROS_ENDPOINT_ERROR",
                   "ROS_DIAGNOSTICS_PARTIAL", "ROS_LOOPBACK_MULTICAST_DISABLED",
                   "ROS_RMW_MISMATCH", "ROS_DEPENDENCY_MISSING", "ROS_OVERLAY_PATH_MISSING",
                   "ROS_DIAGNOSTIC_RECOVERED"))

def normalize_scope(value):
    return {key: value[key] for key in (*SCOPE_FIELDS, "plugin_id", "port_id", "topic")
            if key in value and isinstance(value[key], (str, int)) and not isinstance(value[key], bool)}

def scope_complete(scope, *, port=False):
    required = SCOPE_FIELDS if port else SCOPE_FIELDS[:2]
    return all(key in scope and scope[key] is not None and scope[key] != "" for key in required)

def scope_key(scope):
    return json.dumps([scope.get(key) for key in SCOPE_FIELDS], ensure_ascii=False, separators=(",", ":"))

def issue_key(scope, code):
    return scope_key(scope) + "|" + str(code)
