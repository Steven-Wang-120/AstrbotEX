"""Pure interpretation of C01 facts. No ROS imports, clocks, I/O or state."""
import math
from .models import normalize_scope, scope_complete

_DEFAULT_POLICY = {"mode": "off", "startup_grace_sec": 10.0, "stale_after_sec": 5.0}

def _number(value, lo, hi):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and lo <= value <= hi

def _counter(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0

def _candidate(code, scope, message, evidence, remediation, severity="warning", source="ros.port"):
    return {"code": code, "severity": severity, "source": source, "scope": dict(scope),
            "message": message, "evidence": evidence, "remediation": remediation}

def evaluate_port_health(sample, *, now_ns):
    sample = sample if isinstance(sample, dict) else {}
    policy = sample.get("diagnostics_policy")
    policy = dict(policy) if isinstance(policy, dict) else None
    scope = normalize_scope(sample)
    diag = {"policy": policy, "monitoring_active": sample.get("monitoring_active"),
            "monitoring_epoch": sample.get("monitoring_epoch"), "binding_received": sample.get("binding_received"),
            "receive_age_sec": None, "consume_age_sec": None, "monitoring_age_sec": None, "reason_code": None}
    result = {"data_health": "unknown", "diagnostics": diag, "issues": []}
    def finish(health, reason=None):
        result["data_health"] = health
        diag["reason_code"] = reason
        return result
    def issue(code, message, remediation, severity="warning", evidence=None):
        if scope_complete(scope, port=True):
            values = {key: sample.get(key) for key in ("state", "graph_peer_count", "matched_peer_count", "binding_received")}
            values.update({key: diag[key] for key in ("receive_age_sec", "consume_age_sec", "monitoring_age_sec")})
            if evidence: values.update(evidence)
            result["issues"].append(_candidate(code, scope, message, values, remediation, severity))
    if sample.get("direction") == "publish" or (policy and policy.get("mode") == "off"):
        return finish("not_monitored")
    if (sample.get("direction") != "subscribe" or not policy or policy.get("mode") != "periodic"
            or set(policy) - set(_DEFAULT_POLICY)
            or not _number(policy.get("startup_grace_sec"), 0, 300)
            or not _number(policy.get("stale_after_sec"), .2, 300)):
        return finish("unknown", "invalid_diagnostics_policy")
    if sample.get("desired_enabled") is False:
        return finish("disabled")
    if sample.get("monitoring_active") is False:
        return finish("paused")
    if sample.get("desired_enabled") is not True or sample.get("monitoring_active") is not True:
        return finish("unknown", "incomplete_activity")
    if sample.get("resource_created") is False or sample.get("state") in ("missing_interface", "error"):
        missing = sample.get("state") == "missing_interface" or sample.get("reason_code") == "missing_interface"
        code = "ROS_INTERFACE_MISSING" if missing else "ROS_ENDPOINT_ERROR"
        if missing or sample.get("state") == "error":
            issue(code, "ROS 端口原生资源不可用", ["核对消息接口包和端口错误；修复后重新绑定"], "error")
        return finish("unavailable", code)
    if sample.get("resource_created") is not True or not _counter(now_ns):
        return finish("unknown", "incomplete_resource")
    bound, start, received, consumed = (sample.get(key) for key in (
        "bound_monotonic_ns", "monitoring_started_monotonic_ns", "last_receive_monotonic_ns", "last_consume_monotonic_ns"))
    graph, matched = sample.get("graph_peer_count"), sample.get("matched_peer_count")
    if (not _counter(bound) or not _counter(start) or not bound <= start <= now_ns
            or not _counter(sample.get("binding_received")) or not _counter(graph)
            or (matched is not None and not _counter(matched))):
        return finish("unknown", "incomplete_or_inconsistent_evidence")
    for timestamp in (received, consumed):
        if timestamp is not None and (not _counter(timestamp) or not start <= timestamp <= now_ns):
            return finish("unknown", "inconsistent_timestamp")
    if (received is not None and sample["binding_received"] == 0) or (consumed is not None and received is None):
        return finish("unknown", "inconsistent_receive_evidence")
    diag["monitoring_age_sec"] = (now_ns - start) / 1e9
    diag["receive_age_sec"] = None if received is None else (now_ns - received) / 1e9
    diag["consume_age_sec"] = None if consumed is None else (now_ns - consumed) / 1e9
    qos = sample.get("qos_compatibility") or []
    incompatible = [entry for entry in qos[:64] if isinstance(entry, dict) and entry.get("state") in ("incompatible", "incompatible_type")]
    if incompatible:
        issue("ROS_QOS_INCOMPATIBLE", "ROS 图中存在明确不兼容的端点", ["核对消息类型、reliability 和 durability；图中可见不保证兼容"],
              evidence={"incompatible_peer_count": len(incompatible)})
    if received is None:
        if now_ns - start < policy["startup_grace_sec"] * 1e9:
            return finish("warming_up")
        peer = graph > 0 or (matched is not None and matched > 0)
        code = "ROS_RX_NO_DATA_WITH_PEER" if peer else "ROS_RX_NO_PEER"
        message = "图上有发布者，但当前观察窗口没有收到回调" if peer else "当前窗口没有数据，图中也没有可见发布者"
        issue(code, message, ["确认生产者正在发布，核对 Domain、实际 RMW 和 QoS；不要仅凭图推断已收数"])
        return finish("no_data", code)
    if now_ns - received > policy["stale_after_sec"] * 1e9:
        issue("ROS_RX_STALE", "ROS 端口曾收到数据，最新回调现已过期", ["检查生产者是否停流及实际发布频率"])
        return finish("stale", "ROS_RX_STALE")
    return finish("receiving")

def evaluate_preflight(report):
    report = report if isinstance(report, dict) else {}
    scope = normalize_scope(report.get("environment") or {})
    issues = {}
    for check in report.get("checks", [])[:64]:
        if not isinstance(check, dict): continue
        evidence = check.get("evidence") or {}
        if not isinstance(evidence, dict): continue
        cid, code, severity, values = check.get("id"), None, "warning", {}
        if cid in ("rclpy_available", "rosidl_available") and evidence.get("available") is False:
            code, severity = "ROS_DEPENDENCY_MISSING", "error"
            values = {"missing": ["rclpy" if cid == "rclpy_available" else "rosidl_runtime_py"]}
        elif cid == "loopback_multicast" and evidence.get("platform") == "linux" and evidence.get("localhost_only") is True and evidence.get("multicast") is False:
            code = "ROS_LOOPBACK_MULTICAST_DISABLED"
            values = {key: evidence[key] for key in ("platform", "localhost_only", "multicast")}
        elif cid == "rmw_consistency":
            requested, effective = evidence.get("requested"), evidence.get("effective")
            if isinstance(requested, str) and requested and isinstance(effective, str) and effective and requested != effective:
                code, values = "ROS_RMW_MISMATCH", {"requested": requested[:128], "effective": effective[:128]}
        elif cid == "overlay_paths" and evidence.get("missing_paths"):
            code, values = "ROS_OVERLAY_PATH_MISSING", {"missing_paths": [str(x)[:512] for x in evidence["missing_paths"][:64]]}
        elif cid == "interface_probe" and report.get("platform") == "linux" and evidence.get("supported") is True and evidence.get("error_code"):
            code, values = "ROS_DIAGNOSTICS_PARTIAL", {"error_code": str(evidence["error_code"])[:128]}
        elif cid == "environment_values" and evidence.get("invalid_keys"):
            code, values = "ROS_DIAGNOSTICS_PARTIAL", {"invalid_keys": [key for key in evidence["invalid_keys"] if key in ("domain_id", "localhost_only")]}
        if not code: continue
        if code in issues:
            for key, value in values.items():
                if isinstance(value, list): issues[code]["evidence"].setdefault(key, []).extend(value)
                else: issues[code]["evidence"][key] = value
        else:
            remedies = check.get("remediation") or []
            if isinstance(remedies, str): remedies = [remedies]
            issues[code] = _candidate(code, scope, str(check.get("message") or code)[:512], values,
                                      [str(value)[:512] for value in remedies[:8]], severity, "ros.preflight")
    return list(issues.values())
