"""ROS declarations and bindings; importing this module never imports ROS."""
from __future__ import annotations
import copy
import math
import re

TYPE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*/msg/[A-Za-z][A-Za-z0-9_]*\Z")
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
PRESETS = {
    "sensor_data": {"reliability": "best_effort", "durability": "volatile", "history": "keep_last", "depth": 5},
    "reliable_volatile": {"reliability": "reliable", "durability": "volatile", "history": "keep_last", "depth": 10},
}

def integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value

def topic_name(value):
    if not isinstance(value, str) or not value or value.endswith("/") or "//" in value:
        raise ValueError("ROS topic must be a nonempty topic name")
    if any(not TOKEN.fullmatch(part) or "__" in part for part in value.lstrip("/").split("/")):
        raise ValueError(f"invalid ROS topic: {value}")
    return value

def qos_config(raw=None, preset="reliable_volatile"):
    if raw is None: raw = {}
    if not isinstance(raw, dict): raise ValueError("qos must be an object")
    unknown = set(raw) - {"preset", "depth", "reliability", "durability", "history"}
    if unknown: raise ValueError(f"unsupported QoS fields: {sorted(unknown)}")
    preset = raw.get("preset", preset)
    if preset not in PRESETS: raise ValueError(f"unknown QoS preset: {preset}")
    result = {"preset": preset, **PRESETS[preset], **raw}
    integer(result["depth"], "qos.depth", 1, 1024)
    for key, choices in (("reliability", ("reliable", "best_effort")), ("durability", ("volatile", "transient_local")), ("history", ("keep_last",))):
        if result[key] not in choices: raise ValueError(f"invalid qos.{key}: {result[key]}")
    return result

def parse_ports(raw):
    if raw is None: raw = {}
    if not isinstance(raw, dict): raise ValueError("ros2 declaration must be an object")
    if raw.get("schema_version", 1) != 1: raise ValueError("unsupported ros2 schema_version")
    ports = raw.get("ports", [])
    if not isinstance(ports, list): raise ValueError("ros2.ports must be an array")
    result, seen = [], set()
    for item in ports:
        if not isinstance(item, dict): raise ValueError("ROS port must be an object")
        port = copy.deepcopy(item)
        name = port.get("id", "")
        if not isinstance(name, str) or not TOKEN.fullmatch(name) or name in seen:
            raise ValueError(f"invalid or duplicate ROS port id: {name}")
        seen.add(name)
        if port.get("direction") not in ("subscribe", "publish"): raise ValueError(f"{name}: invalid direction")
        types = port.get("message_types")
        if not isinstance(types, list) or not types or any(not isinstance(t, str) or not TYPE_NAME.fullmatch(t) for t in types):
            raise ValueError(f"{name}: message_types must list package/msg/Type")
        port["default_topic"] = topic_name(port.get("default_topic", "/" + name))
        port.setdefault("execution_lane", "sensor" if port["direction"] == "subscribe" else "general")
        if port["execution_lane"] not in ("sensor", "control", "general"): raise ValueError(f"{name}: invalid lane")
        gate = port.setdefault("requires_runtime_running", port["execution_lane"] == "control")
        if not isinstance(gate, bool): raise ValueError(f"{name}: invalid runtime gate")
        port.setdefault("qos_preset", "sensor_data" if port["direction"] == "subscribe" else "reliable_volatile")
        qos_config(preset=port["qos_preset"])
        queue = port.setdefault("queue", {})
        if not isinstance(queue, dict): raise ValueError(f"{name}: queue must be an object")
        queue.setdefault("capacity", 1)
        queue.setdefault("overflow", "keep_latest" if port["direction"] == "subscribe" else "reject_new")
        queue.setdefault("max_age_ms", 200 if gate else 0)
        queue.setdefault("max_message_bytes", 8 * 1024 * 1024)
        queue.setdefault("max_bytes", 16 * 1024 * 1024)
        integer(queue["capacity"], f"{name}.queue.capacity", 1, 1024)
        integer(queue["max_age_ms"], f"{name}.queue.max_age_ms", 0, 3600000)
        integer(queue["max_message_bytes"], f"{name}.queue.max_message_bytes", 1, 1024 ** 3)
        integer(queue["max_bytes"], f"{name}.queue.max_bytes", 1, 1024 ** 3)
        if queue["max_message_bytes"] > queue["max_bytes"]:
            raise ValueError(f"{name}: max_message_bytes exceeds queue byte budget")
        if gate and queue["max_age_ms"] == 0: raise ValueError(f"{name}: control outputs require a finite max_age_ms")
        if queue["overflow"] not in ("keep_latest", "reject_new"): raise ValueError(f"{name}: invalid overflow policy")
        result.append(port)
    return result

def normalize_bindings(ports, raw=None):
    if raw is None: raw = {}
    if not isinstance(raw, dict): raise ValueError("ROS bindings must be an object")
    unknown = set(raw) - {p["id"] for p in ports}
    if unknown: raise ValueError(f"undeclared ROS ports: {sorted(unknown)}")
    result = {}
    for port in ports:
        value = raw.get(port["id"], {})
        if not isinstance(value, dict): raise ValueError("ROS binding must be an object")
        if set(value) - {"enabled", "topic", "message_type", "qos"}: raise ValueError("unknown ROS binding field")
        enabled = value.get("enabled", port["direction"] == "subscribe")
        if not isinstance(enabled, bool): raise ValueError("binding.enabled must be boolean")
        msg_type = value.get("message_type", port["message_types"][0])
        if msg_type not in port["message_types"]: raise ValueError(f'{port["id"]}: unsupported message_type')
        qos = qos_config(value.get("qos"), port["qos_preset"])
        if port["requires_runtime_running"] and qos["durability"] != "volatile":
            raise ValueError("control outputs require volatile durability")
        result[port["id"]] = {"enabled": enabled, "topic": topic_name(value.get("topic", port["default_topic"])), "message_type": msg_type, "qos": qos}
    return result

def environment_config(raw, base=None):
    if not isinstance(raw, dict): raise ValueError("ros2 config must be an object")
    defaults = {"domain_id": 0, "namespace": "/astrbotex", "node_name": "environment", "discovery_interval_sec": 1.0, "statistics_interval_sec": 1.0, "include_hidden_topics": False}
    if set(raw) - set(defaults): raise ValueError("unsupported environment config field")
    result = {**defaults, **(base or {}), **raw}
    integer(result["domain_id"], "domain_id", 0, 232)
    namespace = result["namespace"]
    if namespace != "/":
        topic_name(namespace)
        if not namespace.startswith("/"): raise ValueError("namespace must be absolute")
    if not isinstance(result["node_name"], str) or not TOKEN.fullmatch(result["node_name"]): raise ValueError("invalid node_name")
    for key in ("discovery_interval_sec", "statistics_interval_sec"):
        val = result[key]
        if isinstance(val, bool) or not isinstance(val, (float, int)) or not math.isfinite(val) or not 0.2 <= val <= 60:
            raise ValueError(f"{key} must be in [0.2, 60]")
    if not isinstance(result["include_hidden_topics"], bool): raise ValueError("include_hidden_topics must be boolean")
    return result
