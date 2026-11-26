"""B01: read-only ROS environment preflight.

Everything here runs on a fake sysfs reader and a fake environment: no ROS, no
root, no network, no real sysfs. The point of the suite is that unknown values
stay unknown and that the probe never gains a side effect.
"""
from __future__ import annotations

import builtins
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from astrbot_ex.core.environments.ros2 import preflight


class FakeReader:
    """Records every path read; failures are configured per path."""

    def __init__(self, names=("eth0", "lo"), flags=None, operstate=None, errors=None, list_error=None):
        self.names = list(names)
        self.flags = dict(flags or {})
        self.operstate = dict(operstate or {})
        self.errors = dict(errors or {})
        self.list_error = list_error
        self.paths: list[str] = []

    def list_names(self):
        if self.list_error is not None:
            raise self.list_error
        return list(self.names)

    def read_text(self, path):
        self.paths.append(path)
        if path in self.errors:
            raise self.errors[path]
        name = path.split("/")[-2]
        if path.endswith("/flags"):
            if name not in self.flags:
                raise FileNotFoundError(path)
            return self.flags[name]
        if path.endswith("/operstate"):
            return self.operstate.get(name, "unknown")
        raise FileNotFoundError(path)


def checks_by_id(report):
    return {item["id"]: item for item in report["checks"]}


def linux_env(**values):
    env = {"ROS_DISTRO": "humble", "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
           "ROS_DOMAIN_ID": "71", "ROS_LOCALHOST_ONLY": "1"}
    env.update(values)
    return {key: value for key, value in env.items() if value is not None}


class InterfaceFlagTests(unittest.TestCase):
    def test_hexadecimal_and_decimal_flags_are_equivalent(self):
        self.assertEqual(preflight.parse_interface_flags("0x1009"), preflight.parse_interface_flags("4105"))
        self.assertEqual(preflight.parse_interface_flags("0x1009"),
                         {"up": True, "loopback": True, "multicast": True})

    def test_garbage_flags_are_none_not_false(self):
        for value in ("", "  ", "zzz", "0x", None, "9" * 40):
            self.assertIsNone(preflight.parse_interface_flags(value))

    def test_negative_flags_are_rejected(self):
        self.assertIsNone(preflight.parse_interface_flags("-1"))


class LoopbackMulticastTests(unittest.TestCase):
    def test_loopback_without_multicast_warns_when_localhost_only(self):
        reader = FakeReader(flags={"lo": "0x9", "eth0": "0x1003"})
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        check = checks_by_id(report)["loopback_multicast"]
        self.assertEqual(check["status"], "warn")
        self.assertEqual(check["evidence"]["multicast"], False)
        self.assertIn("IFF_MULTICAST", check["message"])
        self.assertTrue(check["remediation"])

    def test_loopback_with_multicast_passes(self):
        reader = FakeReader(flags={"lo": "0x1009", "eth0": "0x1003"})
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        self.assertEqual(checks_by_id(report)["loopback_multicast"]["status"], "pass")

    def test_localhost_only_unset_leaves_multicast_unknown(self):
        reader = FakeReader(flags={"lo": "0x9"})
        report = preflight.collect_preflight(environ=linux_env(ROS_LOCALHOST_ONLY=None),
                                             platform_name="Linux", interface_reader=reader)
        check = checks_by_id(report)["loopback_multicast"]
        self.assertEqual(check["status"], "unknown")
        self.assertIsNone(report["runtime"]["localhost_only"])

    def test_missing_loopback_interface_is_unknown_not_down(self):
        reader = FakeReader(names=("eth0",), flags={"eth0": "0x1003"})
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        self.assertEqual(checks_by_id(report)["loopback_multicast"]["status"], "unknown")

    def test_loopback_operstate_unknown_is_not_treated_as_down(self):
        # flags unreadable, operstate "unknown" for lo: up must stay null, not False
        reader = FakeReader(flags={}, operstate={"lo": "unknown"})
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        entry = next(item for item in report["interfaces"] if item["name"] == "lo")
        self.assertIsNone(entry["up"])
        self.assertIsNone(entry["multicast"])
        self.assertTrue(report["partial"])


class ReaderFailureTests(unittest.TestCase):
    def test_permission_denied_keeps_entry_and_marks_partial(self):
        reader = FakeReader(flags={"lo": "0x1009"},
                            errors={"/sys/class/net/eth0/flags": PermissionError("denied")})
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        entry = next(item for item in report["interfaces"] if item["name"] == "eth0")
        self.assertIsNone(entry["up"])
        self.assertTrue(report["partial"])
        check = checks_by_id(report)["interface_probe"]
        self.assertEqual(check["status"], "warn")
        self.assertEqual(check["evidence"]["error_code"], "PermissionError")

    def test_missing_sys_class_net_marks_partial(self):
        reader = FakeReader(list_error=FileNotFoundError("/sys/class/net"))
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        self.assertEqual(report["interfaces"], [])
        self.assertTrue(report["partial"])
        self.assertEqual(checks_by_id(report)["interface_probe"]["status"], "warn")

    def test_interface_disappearing_between_listing_and_read(self):
        reader = FakeReader(names=("eth0", "lo"), flags={"lo": "0x1009"},
                            errors={"/sys/class/net/eth0/flags": FileNotFoundError("gone")})
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        names = [item["name"] for item in report["interfaces"]]
        self.assertEqual(names, ["eth0", "lo"])
        self.assertTrue(report["partial"])

    def test_interface_names_are_validated_before_use(self):
        reader = FakeReader(names=("lo", "../../etc/passwd", "a" * 40), flags={"lo": "0x1009"})
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        self.assertEqual([item["name"] for item in report["interfaces"]], ["lo"])
        for path in reader.paths:
            self.assertTrue(path.startswith("/sys/class/net/"))
            self.assertNotIn("passwd", path)

    def test_interface_list_is_truncated_with_partial(self):
        names = tuple("if%d" % index for index in range(preflight.MAX_INTERFACES + 5))
        reader = FakeReader(names=names, flags={name: "0x1003" for name in names})
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        self.assertEqual(len(report["interfaces"]), preflight.MAX_INTERFACES)
        self.assertTrue(report["partial"])


class PlatformTests(unittest.TestCase):
    def test_windows_reports_unsupported_without_claiming_a_ros_fault(self):
        reader = FakeReader()
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Windows",
                                             interface_reader=reader)
        self.assertEqual(report["platform"], "windows")
        self.assertFalse(report["supported"])
        self.assertEqual(report["interfaces"], [])
        self.assertEqual(reader.paths, [])
        checks = checks_by_id(report)
        self.assertEqual(checks["interface_probe"]["status"], "unknown")
        self.assertEqual(checks["loopback_multicast"]["status"], "unknown")
        # dependency checks may still be fail, but never "the machine is broken"
        for check_id in ("interface_probe", "loopback_multicast", "environment_values"):
            self.assertNotEqual(checks[check_id]["status"], "fail")

    def test_report_is_json_serializable(self):
        reader = FakeReader(flags={"lo": "0x1009", "eth0": "0x1003"})
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        json.dumps(report, ensure_ascii=False)


class EnvironmentValueTests(unittest.TestCase):
    def test_unset_environment_stays_null(self):
        report = preflight.collect_preflight(environ={}, platform_name="Linux",
                                             interface_reader=FakeReader(flags={"lo": "0x1009"}))
        runtime = report["runtime"]
        self.assertIsNone(runtime["ros_distro"])
        self.assertIsNone(runtime["rmw_requested"])
        self.assertIsNone(runtime["rmw_effective"])
        self.assertIsNone(runtime["domain_id"])
        self.assertIsNone(runtime["localhost_only"])
        self.assertEqual(runtime["overlay_paths"], [])
        self.assertEqual(checks_by_id(report)["environment_values"]["status"], "pass")
        self.assertEqual(checks_by_id(report)["rmw_consistency"]["status"], "unknown")

    def test_invalid_domain_and_localhost_are_reported_without_echoing_values(self):
        env = linux_env(ROS_DOMAIN_ID="999999", ROS_LOCALHOST_ONLY="yes")
        report = preflight.collect_preflight(environ=env, platform_name="Linux",
                                             interface_reader=FakeReader(flags={"lo": "0x1009"}))
        self.assertIsNone(report["runtime"]["domain_id"])
        self.assertIsNone(report["runtime"]["localhost_only"])
        check = checks_by_id(report)["environment_values"]
        self.assertEqual(check["status"], "warn")
        self.assertEqual(sorted(check["evidence"]["invalid_keys"]), ["domain_id", "localhost_only"])
        self.assertNotIn("yes", json.dumps(report, ensure_ascii=False))
        self.assertNotIn("999999", json.dumps(report, ensure_ascii=False))

    def test_domain_bounds(self):
        for raw, expected in (("0", 0), ("232", 232), ("233", None), ("-1", None), ("7.5", None)):
            report = preflight.collect_preflight(environ=linux_env(ROS_DOMAIN_ID=raw),
                                                 platform_name="Linux",
                                                 interface_reader=FakeReader(flags={"lo": "0x1009"}))
            self.assertEqual(report["runtime"]["domain_id"], expected, raw)


class RmwTests(unittest.TestCase):
    def test_matching_rmw_passes(self):
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=FakeReader(flags={"lo": "0x1009"}),
                                             runtime_rmw="rmw_fastrtps_cpp")
        self.assertEqual(checks_by_id(report)["rmw_consistency"]["status"], "pass")
        self.assertEqual(checks_by_id(report)["rmw_effective"]["status"], "pass")

    def test_mismatched_rmw_warns(self):
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=FakeReader(flags={"lo": "0x1009"}),
                                             runtime_rmw="rmw_cyclonedds_cpp")
        check = checks_by_id(report)["rmw_consistency"]
        self.assertEqual(check["status"], "warn")
        self.assertEqual(check["evidence"],
                         {"requested": "rmw_fastrtps_cpp", "effective": "rmw_cyclonedds_cpp"})

    def test_unknown_effective_rmw_never_guesses(self):
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=FakeReader(names=("lo",),
                                                                         flags={"lo": "0x1009"}))
        self.assertEqual(checks_by_id(report)["rmw_consistency"]["status"], "unknown")
        self.assertEqual(checks_by_id(report)["rmw_effective"]["status"], "unknown")
        self.assertIsNone(report["runtime"]["rmw_effective"])
        # an unknown effective RMW alone does not make the report partial
        self.assertFalse(report["partial"])

    def test_unset_requested_rmw_is_not_a_mismatch(self):
        report = preflight.collect_preflight(environ=linux_env(RMW_IMPLEMENTATION=None),
                                             platform_name="Linux",
                                             interface_reader=FakeReader(flags={"lo": "0x1009"}),
                                             runtime_rmw="rmw_cyclonedds_cpp")
        self.assertEqual(checks_by_id(report)["rmw_consistency"]["status"], "unknown")


class OverlayTests(unittest.TestCase):
    # The separator follows the simulated platform, so the fixture uses the host
    # separator only when the host platform is what we pretend to be.
    SEPARATOR = os.pathsep
    PLATFORM = "Windows" if os.name == "nt" else "Linux"

    def test_missing_overlay_path_warns(self):
        with tempfile.TemporaryDirectory() as root:
            present = os.path.join(root, "install")
            os.makedirs(present)
            missing = os.path.join(root, "missing")
            env = linux_env(AMENT_PREFIX_PATH=present + self.SEPARATOR + missing)
            report = preflight.collect_preflight(environ=env, platform_name=self.PLATFORM,
                                                 interface_reader=FakeReader(flags={"lo": "0x1009"}))
        check = checks_by_id(report)["overlay_paths"]
        self.assertEqual(check["status"], "warn")
        self.assertEqual(check["evidence"]["missing_paths"], [missing])
        self.assertEqual([item["exists"] for item in report["runtime"]["overlay_paths"]], [True, False])

    def test_unconfigured_overlay_is_not_a_missing_path(self):
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=FakeReader(flags={"lo": "0x1009"}))
        check = checks_by_id(report)["overlay_paths"]
        self.assertEqual(check["status"], "pass")
        self.assertEqual(check["evidence"]["missing_paths"], [])
        self.assertFalse(check["evidence"]["configured"])

    def test_long_overlay_list_is_truncated_and_partial(self):
        paths = [os.path.join(tempfile.gettempdir(), "p%d" % index)
                 for index in range(preflight.MAX_OVERLAY_PATHS + 6)]
        env = linux_env(COLCON_PREFIX_PATH=self.SEPARATOR.join(paths))
        report = preflight.collect_preflight(environ=env, platform_name=self.PLATFORM,
                                             interface_reader=FakeReader(flags={"lo": "0x1009"}))
        self.assertEqual(len(report["runtime"]["overlay_paths"]), preflight.MAX_OVERLAY_PATHS)
        self.assertTrue(report["partial"])
        check = checks_by_id(report)["overlay_paths"]
        self.assertEqual(check["status"], "warn")
        self.assertTrue(check["evidence"]["truncated"])


class DependencyProbeTests(unittest.TestCase):
    def test_missing_ros_libraries_are_reported_together(self):
        with mock.patch.object(importlib.util, "find_spec", return_value=None):
            report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                                 interface_reader=FakeReader(flags={"lo": "0x1009"}))
        checks = checks_by_id(report)
        self.assertEqual(checks["rclpy_available"]["status"], "fail")
        self.assertEqual(checks["rosidl_available"]["status"], "fail")
        self.assertEqual(checks["rclpy_available"]["evidence"]["missing"], ["rclpy", "rosidl_runtime_py"])

    def test_find_spec_failure_is_unknown_not_failure(self):
        with mock.patch.object(importlib.util, "find_spec", side_effect=ValueError("broken")):
            report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                                 interface_reader=FakeReader(flags={"lo": "0x1009"}))
        checks = checks_by_id(report)
        self.assertEqual(checks["rclpy_available"]["status"], "unknown")
        self.assertEqual(checks["rosidl_available"]["status"], "unknown")
        self.assertTrue(report["partial"])

    def test_preflight_does_not_import_rclpy(self):
        sys.modules.pop("rclpy", None)
        preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                    interface_reader=FakeReader(flags={"lo": "0x1009"}))
        self.assertNotIn("rclpy", sys.modules)


class SideEffectTests(unittest.TestCase):
    def test_no_subprocess_system_or_ros_init(self):
        reader = FakeReader(flags={"lo": "0x1009", "eth0": "0x1003"})
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess")), \
                mock.patch.object(os, "system", side_effect=AssertionError("os.system")):
            report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                                 interface_reader=reader)
        self.assertTrue(report["checks"])

    def test_no_file_is_opened_for_writing(self):
        real_open = builtins.open

        def guarded(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise AssertionError("preflight must not write: %s (%s)" % (file, mode))
            return real_open(file, mode, *args, **kwargs)

        with mock.patch.object(builtins, "open", guarded):
            report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                                 interface_reader=FakeReader(flags={"lo": "0x1009"}))
        self.assertEqual(report["schema_version"], 1)

    def test_reader_sees_only_sysfs_paths(self):
        reader = FakeReader(flags={"lo": "0x1009", "eth0": "0x1003"})
        preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                    interface_reader=reader)
        self.assertTrue(reader.paths)
        for path in reader.paths:
            self.assertTrue(path.startswith("/sys/class/net/"), path)
            lowered = path.lower()
            for secret in ("token", "ssh", "passwd", "shadow", "credential", ".env", "config"):
                self.assertNotIn(secret, lowered)

    def test_report_contains_no_full_environment(self):
        env = linux_env(SECRET_TOKEN="do-not-leak")
        report = preflight.collect_preflight(environ=env, platform_name="Linux",
                                             interface_reader=FakeReader(flags={"lo": "0x1009"}))
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("do-not-leak", text)
        self.assertNotIn("SECRET_TOKEN", text)
        self.assertNotIn("environ", report)

    def test_default_reader_reads_are_bounded(self):
        self.assertEqual(preflight.MAX_READ_BYTES, 4096)
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            handle.write(b"a" * (preflight.MAX_READ_BYTES * 3))
            path = handle.name
        try:
            text = preflight._SysfsReader().read_text(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(text), preflight.MAX_READ_BYTES)

    def test_callable_reader_is_accepted(self):
        def reader(path):
            if path.endswith("/flags"):
                return "0x1009"
            return "up"

        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        self.assertTrue(report["supported"])


class CheckContractTests(unittest.TestCase):
    def test_check_ids_and_statuses_are_frozen(self):
        reader = FakeReader(flags={"lo": "0x1009", "eth0": "0x1003"})
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=reader)
        self.assertEqual([check["id"] for check in report["checks"]], list(preflight.CHECK_IDS))
        for check in report["checks"]:
            self.assertEqual(set(check), {"id", "status", "message", "evidence", "remediation"})
            self.assertIn(check["status"], {"pass", "warn", "fail", "unknown"})
            self.assertIsInstance(check["evidence"], dict)

    def test_report_shape_is_frozen(self):
        report = preflight.collect_preflight(environ=linux_env(), platform_name="Linux",
                                             interface_reader=FakeReader(flags={"lo": "0x1009"}))
        self.assertEqual(set(report), {"schema_version", "platform", "supported", "checks",
                                       "runtime", "interfaces", "partial"})
        self.assertEqual(set(report["runtime"]), {
            "ros_distro", "python_executable", "rmw_requested", "rmw_effective", "domain_id",
            "localhost_only", "overlay_paths", "rclpy_available", "rosidl_available"})
        for entry in report["interfaces"]:
            self.assertEqual(set(entry), {"name", "up", "loopback", "multicast"})


if __name__ == "__main__":
    unittest.main()
