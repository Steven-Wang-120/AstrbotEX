from __future__ import annotations
from ..contracts import TYPE_NAME
from pathlib import Path
from xml.etree import ElementTree

def load_message(message_type):
    if not isinstance(message_type, str) or not TYPE_NAME.fullmatch(message_type):
        raise ValueError("message_type must be package/msg/Type")
    from rosidl_runtime_py.utilities import get_message
    from rclpy.type_support import check_for_type_support
    cls = get_message(message_type)
    check_for_type_support(cls)
    return cls

def check_message(message_type):
    if not isinstance(message_type, str) or not TYPE_NAME.fullmatch(message_type):
        raise ValueError("message_type must be package/msg/Type")
    result = {"message_type": message_type, "package": message_type.split("/")[0], "available": False,
              "python_import": False, "typesupport": False, "restart_required": False}
    try:
        from rosidl_runtime_py.utilities import get_message
        cls = get_message(message_type)
        result["python_import"] = True
        from rclpy.type_support import check_for_type_support
        check_for_type_support(cls)
        result.update(available=True, typesupport=True, code="ready", message=None)
    except Exception as exc:
        result.update(code="missing_interface" if not result["python_import"] else "typesupport_error",
                      message=str(exc), missing_dependency=getattr(exc, "name", None), restart_required=True)
    return result

def installed_interfaces():
    try:
        from rosidl_runtime_py import get_message_interfaces
        from ament_index_python.packages import get_package_prefix
        result = []
        for pkg, names in sorted(get_message_interfaces().items()):
            prefix = get_package_prefix(pkg)
            try:
                version = ElementTree.parse(Path(prefix) / 'share' / pkg / 'package.xml').findtext('version')
            except (OSError, ElementTree.ParseError):
                version = None
            result.append({'package': pkg, 'prefix': prefix, 'version': version,
                           'message_types': [pkg + '/' + name for name in names]})
        return result
    except Exception:
        return []
