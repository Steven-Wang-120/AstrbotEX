"""Read-only ROS 2 environment preflight.

This module answers "is the ROS environment shaped the way the deployment
expects?" without changing anything. It never imports rclpy or rosidl (only
``importlib.util.find_spec``), never starts a process, never opens a socket and
never writes a file. Every filesystem read is bounded; nothing outside the
whitelist in C01 is read, and no file content is returned, only path existence.

The report shape is frozen in the task package (C01 section 4). ``runtime`` and
``interfaces`` are facts; ``checks`` are the machine readable conclusions whose
``id`` values are also frozen, so B00/B04/B06 must not invent their own names.
"""
from __future__ import annotations

import importlib.util
import os
import sys

SCHEMA_VERSION = 1

#: Hard budgets. A probe that would exceed them reports partial instead.
MAX_INTERFACES = 64
MAX_READ_BYTES = 4096
MAX_OVERLAY_PATHS = 64
MAX_TEXT = 128

SYS_CLASS_NET = "/sys/class/net"

IFF_UP = 0x1
IFF_LOOPBACK = 0x8
IFF_MULTICAST = 0x1000

#: Environment variables that may be read. Anything else stays untouched.
ENV_ROS_DISTRO = "ROS_DISTRO"
ENV_RMW = "RMW_IMPLEMENTATION"
ENV_DOMAIN = "ROS_DOMAIN_ID"
ENV_LOCALHOST_ONLY = "ROS_LOCALHOST_ONLY"
OVERLAY_KEYS = ("AMENT_PREFIX_PATH", "COLCON_PREFIX_PATH", "ASTRBOTEX_ROS_OVERLAY")

CHECK_IDS = (
    "rclpy_available",
    "rosidl_available",
    "loopback_multicast",
    "rmw_consistency",
    "rmw_effective",
    "overlay_paths",
    "interface_probe",
    "environment_values",
)

#: Interface names are used to build sysfs paths, so they are validated first.
_INTERFACE_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")

__all__ = [
    "SCHEMA_VERSION",
    "MAX_INTERFACES",
    "MAX_READ_BYTES",
    "MAX_OVERLAY_PATHS",
    "CHECK_IDS",
    "SYS_CLASS_NET",
    "collect_preflight",
    "parse_interface_flags",
]


class _SysfsReader:
    """Default reader: bounded binary reads from sysfs, nothing else."""

    def list_names(self):
        return os.listdir(SYS_CLASS_NET)

    def read_text(self, path):
        with open(path, "rb") as handle:
            return handle.read(MAX_READ_BYTES).decode("ascii", "replace")


def _resolve_reader(interface_reader):
    """Accept the default, an object with list_names/read_text, or a plain callable."""
    if interface_reader is None:
        return _SysfsReader()
    if hasattr(interface_reader, "read_text"):
        return interface_reader

    class _CallableReader:
        def list_names(self):
            return os.listdir(SYS_CLASS_NET)

        def read_text(self, path):
            return interface_reader(path)

    return _CallableReader()


def _platform_id(platform_name):
    value = platform_name if platform_name is not None else sys.platform
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    text = value.strip().lower()
    if text.startswith("linux"):
        return "linux"
    if text.startswith(("win", "msys", "cygwin")):
        return "windows"
    if text.startswith("darwin"):
        return "darwin"
    return text[:32]


def _text(value):
    """Whitelisted environment value: trimmed, bounded, empty means unset."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:MAX_TEXT]


def _domain_id(raw):
    """Return ``(value, invalid)``; unset stays null instead of defaulting to 0."""
    if raw is None:
        return None, False
    if not isinstance(raw, str):
        return None, True
    text = raw.strip()
    if not text:
        return None, False
    if not text.lstrip("+").isdigit() or len(text) > 8:
        return None, True
    value = int(text)
    if not 0 <= value <= 232:
        return None, True
    return value, False


def _localhost_only(raw):
    """Only an explicit 1/0 is trusted; anything else is unknown, not false."""
    if raw is None:
        return None, False
    if not isinstance(raw, str):
        return None, True
    text = raw.strip()
    if not text:
        return None, False
    if text == "1":
        return True, False
    if text == "0":
        return False, False
    return None, True


def _overlay_paths(environ, platform_id):
    """Return ``(entries, truncated)`` for the deployment overlay whitelist."""
    separator = ";" if platform_id == "windows" else ":"
    entries, seen, truncated = [], set(), False
    for key in OVERLAY_KEYS:
        raw = environ.get(key)
        if not isinstance(raw, str):
            continue
        for part in raw.split(separator):
            path = part.strip()
            if not path or path in seen:
                continue
            if len(entries) >= MAX_OVERLAY_PATHS:
                truncated = True
                break
            seen.add(path)
            entries.append({"path": path[:1024], "exists": _exists(path)})
        if truncated:
            break
    return entries, truncated


def _exists(path):
    try:
        return bool(os.path.exists(path))
    except (OSError, ValueError):
        return False


def _spec_available(name):
    """``(available, error)``. A failed lookup is unknown, never a hard failure."""
    try:
        return importlib.util.find_spec(name) is not None, None
    except (ImportError, ValueError, AttributeError) as exc:  # pragma: no cover - platform dependent
        return None, type(exc).__name__


def parse_interface_flags(text):
    """Parse a sysfs flags value (hex ``0x1003`` or decimal) into a dict or None."""
    if not isinstance(text, str):
        return None
    token = text.strip()
    if not token or len(token) > 32:
        return None
    try:
        value = int(token, 0)
    except ValueError:
        return None
    if value < 0:
        return None
    return {
        "up": bool(value & IFF_UP),
        "loopback": bool(value & IFF_LOOPBACK),
        "multicast": bool(value & IFF_MULTICAST),
    }


def _valid_interface_name(name):
    return (
        isinstance(name, str)
        and 0 < len(name) <= 32
        and all(char in _INTERFACE_ALLOWED for char in name)
    )


def _interface(name, reader):
    """One interface entry; unreadable fields stay null and are reported partial."""
    base = {"name": name, "up": None, "loopback": None, "multicast": None}
    flags_text = None
    error = None
    try:
        flags_text = reader.read_text(SYS_CLASS_NET + "/" + name + "/flags")
    except (OSError, ValueError) as exc:
        flags_text = None
        error = type(exc).__name__
    parsed = parse_interface_flags(flags_text)
    if parsed is None:
        # Fall back to operstate only for "up", and never treat "unknown" as down.
        try:
            state = reader.read_text(SYS_CLASS_NET + "/" + name + "/operstate").strip().lower()
        except (OSError, ValueError):
            state = ""
        if state in ("up", "down"):
            base["up"] = state == "up"
        return base, error or "FLAGS_UNPARSABLE"
    base.update(parsed)
    return base, None


def _check(check_id, status, message, evidence=None, remediation=None):
    return {
        "id": check_id,
        "status": status,
        "message": message,
        "evidence": evidence if isinstance(evidence, dict) else {},
        "remediation": remediation,
    }


def collect_preflight(*, environ=None, platform_name=None, interface_reader=None, runtime_rmw=None):
    """Collect one read-only preflight report.

    ``runtime_rmw`` is injected by the caller that can safely observe the active
    ROS process; this function never starts ROS to obtain it.
    """
    env = os.environ if environ is None else environ
    if not hasattr(env, "get"):
        raise TypeError("environ must be a mapping")
    platform_id = _platform_id(platform_name)
    reader = _resolve_reader(interface_reader)
    partial = False

    ros_distro = _text(env.get(ENV_ROS_DISTRO))
    rmw_requested = _text(env.get(ENV_RMW))
    rmw_effective = _text(runtime_rmw)
    domain_id, domain_invalid = _domain_id(env.get(ENV_DOMAIN))
    localhost_only, localhost_invalid = _localhost_only(env.get(ENV_LOCALHOST_ONLY))
    overlay_paths, overlay_truncated = _overlay_paths(env, platform_id)
    rclpy_available, rclpy_error = _spec_available("rclpy")
    rosidl_available, rosidl_error = _spec_available("rosidl_runtime_py")
    if rclpy_error or rosidl_error:
        partial = True
    if overlay_truncated:
        partial = True

    supported = platform_id == "linux"
    interfaces, probe_error, interfaces_truncated = [], None, False
    interface_errors = []
    if supported:
        try:
            names = list(reader.list_names())
        except (OSError, ValueError) as exc:
            names = []
            probe_error = type(exc).__name__
            partial = True
        valid = [name for name in names if _valid_interface_name(name)]
        if len(valid) > MAX_INTERFACES:
            valid = valid[:MAX_INTERFACES]
            interfaces_truncated = True
            partial = True
        for name in sorted(valid):
            entry, read_error = _interface(name, reader)
            interfaces.append(entry)
            if read_error is not None:
                interface_errors.append({"name": name, "error_code": read_error})
                partial = True

    checks = []
    missing_libraries = [
        name for name, available in (("rclpy", rclpy_available), ("rosidl_runtime_py", rosidl_available))
        if available is False
    ]
    dependency_evidence = {
        "available": rclpy_available is True,
        "missing": missing_libraries,
        "platform": platform_id,
    }
    if rclpy_available is True:
        checks.append(_check("rclpy_available", "pass", "rclpy 可导入（仅 find_spec，未初始化 ROS）",
                             {"available": True}))
    elif rclpy_available is False:
        checks.append(_check(
            "rclpy_available", "fail",
            "当前解释器无法定位 rclpy；只有确实要启用 ROS 的环境才应把它当作故障",
            dict(dependency_evidence),
            "在目标镜像内安装 ROS 2 的 Python 运行时（如 apt install ros-humble-rclpy），"
            "或用预装 ROS 的镜像重启 EX；本检查不会自动安装任何软件包。",
        ))
    else:
        checks.append(_check("rclpy_available", "unknown", "rclpy 探测本身失败，依赖状态未知",
                             {"error": rclpy_error}, "检查解释器与 site-packages 权限后重新预检。"))

    if rosidl_available is True:
        checks.append(_check("rosidl_available", "pass", "rosidl_runtime_py 可导入（未初始化 ROS）",
                             {"available": True}))
    elif rosidl_available is False:
        checks.append(_check(
            "rosidl_available", "fail",
            "当前解释器无法定位 rosidl_runtime_py；与 rclpy 缺失属于同一依赖问题",
            dict(dependency_evidence),
            "安装对应发行版的 ros-<distro>-rosidl-runtime-py；与 rclpy 缺失合并处理，不要重复报警。",
        ))
    else:
        checks.append(_check("rosidl_available", "unknown", "rosidl 探测本身失败，依赖状态未知",
                             {"error": rosidl_error}, "检查解释器与 site-packages 权限后重新预检。"))

    loopback = next((item for item in interfaces if item["name"] == "lo"), None)
    loopback_multicast = None if loopback is None else loopback["multicast"]
    multicast_evidence = {"platform": platform_id, "localhost_only": localhost_only,
                          "multicast": loopback_multicast}
    if not supported:
        checks.append(_check("loopback_multicast", "unknown",
                             "当前平台不支持 sysfs 网卡标志探测，未判断回环组播",
                             dict(multicast_evidence, supported=False),
                             "在 Linux 部署机上重新预检；Windows 结果不代表 ROS 通信故障。"))
    elif localhost_only is None:
        checks.append(_check("loopback_multicast", "unknown",
                             "ROS_LOCALHOST_ONLY 未设置或取值不可信，未判断回环组播",
                             multicast_evidence,
                             "显式设置 ROS_LOCALHOST_ONLY=1/0 后再判断；本检查不修改进程环境。"))
    elif loopback is None:
        checks.append(_check("loopback_multicast", "unknown",
                             "未在 /sys/class/net 中找到 lo，无法判断回环组播",
                             multicast_evidence, "确认容器/网络命名空间是否提供 lo。"))
    elif localhost_only and loopback_multicast is False:
        checks.append(_check(
            "loopback_multicast", "warn",
            "仅本机通信已启用，但 lo 的 IFF_MULTICAST 未置位；同机 ROS 发现可能收不到数据",
            multicast_evidence,
            "确认 ip link show lo 的 MULTICAST 标志；由现场决定是否启用（ip link set lo multicast on）。"
            "这是配置建议，不是启动硬阻断，静态单播部署可能仍然正常。",
        ))
    else:
        checks.append(_check("loopback_multicast", "pass",
                             "回环组播标志与 localhost-only 设置一致", multicast_evidence))

    if rmw_requested is not None and rmw_effective is not None:
        if rmw_requested == rmw_effective:
            checks.append(_check("rmw_consistency", "pass", "请求与实际 RMW 一致",
                                 {"requested": rmw_requested, "effective": rmw_effective}))
        else:
            checks.append(_check(
                "rmw_consistency", "warn",
                "请求的 RMW 与当前进程实际使用的 RMW 不一致",
                {"requested": rmw_requested, "effective": rmw_effective},
                "按部署策略重启 EX 使 RMW_IMPLEMENTATION 生效；RMW 只能在进程启动前选择，"
                "不能在运行中热切换。",
            ))
    else:
        checks.append(_check("rmw_consistency", "unknown",
                             "请求值或实际值未知，未比较 RMW",
                             {"requested": rmw_requested, "effective": rmw_effective},
                             "需要活跃 ROS 进程的实际 RMW 才能比较；本检查不猜默认实现。"))

    if rmw_effective is None:
        checks.append(_check("rmw_effective", "unknown", "未取得当前进程实际 RMW 标识",
                             {"effective": None},
                             "由能安全观测活跃 ROS 进程的一方注入实际值；本项只作信息，不单独产生持续告警。"))
    else:
        checks.append(_check("rmw_effective", "pass", "已取得当前进程实际 RMW 标识",
                             {"effective": rmw_effective}))

    missing_overlays = [entry["path"] for entry in overlay_paths if not entry["exists"]]
    overlay_evidence = {"missing_paths": missing_overlays, "configured": bool(overlay_paths),
                        "count": len(overlay_paths), "truncated": overlay_truncated}
    if missing_overlays:
        checks.append(_check(
            "overlay_paths", "warn", "部署 overlay 白名单中存在不存在的路径",
            overlay_evidence,
            "确认容器内挂载与 AMENT_PREFIX_PATH/COLCON_PREFIX_PATH 是否指向真实目录；"
            "本检查不会自动 source 或下载任何 overlay。",
        ))
    elif overlay_truncated:
        checks.append(_check("overlay_paths", "warn", "overlay 路径超过上限已截断，证据不完整",
                             overlay_evidence, "检查是否重复拼接了 prefix 环境变量。"))
    else:
        checks.append(_check("overlay_paths", "pass",
                             "overlay 白名单路径均存在" if overlay_paths else "未配置 overlay 路径",
                             overlay_evidence))

    if not supported:
        checks.append(_check("interface_probe", "unknown",
                             "当前平台不支持只读网卡探针", {"supported": False},
                             "在 Linux 部署机上重新预检；其它平台的结论不代表 ROS 通信状态。"))
    elif probe_error is not None:
        checks.append(_check(
            "interface_probe", "warn", "网卡目录读取失败，预检证据不完整",
            {"supported": True, "error_code": probe_error},
            "确认容器内 /sys/class/net 可读；不要为此提升权限或修改网卡。",
        ))
    elif interface_errors:
        checks.append(_check(
            "interface_probe", "warn", "部分网卡标志读取失败，相关条目按未知处理",
            {"supported": True, "error_code": interface_errors[0]["error_code"],
             "failed_interfaces": [item["name"] for item in interface_errors][:8],
             "failed_count": len(interface_errors)},
            "确认 /sys/class/net/<iface>/flags 可读（容器常缺权限）；不要为此修改网卡或提权。",
        ))
    else:
        checks.append(_check("interface_probe", "pass", "已读取网卡标志",
                             {"supported": True, "interfaces": len(interfaces),
                              "truncated": interfaces_truncated}))

    invalid_keys = []
    if domain_invalid:
        invalid_keys.append("domain_id")
    if localhost_invalid:
        invalid_keys.append("localhost_only")
    if invalid_keys:
        checks.append(_check(
            "environment_values", "warn", "ROS 环境变量取值非法，已按未知处理",
            {"invalid_keys": invalid_keys},
            "把 ROS_DOMAIN_ID 设为 0-232 的整数、ROS_LOCALHOST_ONLY 设为 1 或 0 后重启进程；"
            "本检查不回显原始取值，也不会修改进程环境。",
        ))
    else:
        checks.append(_check("environment_values", "pass", "ROS 环境变量取值可解析",
                             {"invalid_keys": []}))

    return {
        "schema_version": SCHEMA_VERSION,
        "platform": platform_id,
        "supported": supported,
        "checks": checks,
        "runtime": {
            "ros_distro": ros_distro,
            "python_executable": sys.executable,
            "rmw_requested": rmw_requested,
            "rmw_effective": rmw_effective,
            "domain_id": domain_id,
            "localhost_only": localhost_only,
            "overlay_paths": overlay_paths,
            "rclpy_available": rclpy_available,
            "rosidl_available": rosidl_available,
        },
        "interfaces": interfaces,
        "partial": partial,
    }
