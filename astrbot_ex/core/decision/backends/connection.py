"""Shared URL and no-action probe contract for SystemOne clients."""
from __future__ import annotations

import http.client
import ipaddress
import re
import time
from urllib.parse import unquote, urlsplit

from ..models import DecisionSnapshot


def service_url(value):
    """Validate before secrets are read. HTTP names are limited to localhost."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(
            ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError("invalid_service_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or "?" in value or "#" in value or
                "\\" in value or (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
        decoded = unquote(parsed.path)
        if (any(ord(c) <= 32 or ord(c) >= 127 for c in decoded) or
                "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")) or
                re.search(r"%2f|%5c", parsed.path, re.I)):
            raise ValueError()
        if parsed.netloc.endswith(":") or "%" in host:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            if len(host) > 253 or not all(re.fullmatch(
                    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                    for label in host.rstrip(".").split(".")):
                raise ValueError()
        if parsed.scheme == "http" and host != "localhost" and (
                address is None or not address.is_loopback):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("invalid_service_url") from None
    return parsed


def open_connection(base_url, timeout):
    parsed = service_url(base_url)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    # Never resolve the HTTP localhost alias through DNS; connect to literal loopback.
    host = "127.0.0.1" if parsed.scheme == "http" and parsed.hostname == "localhost" else parsed.hostname
    # Omit the default port to preserve the normal stdlib verified TLS setup.
    if parsed.port is None:
        return cls(host, timeout=timeout)
    return cls(host, parsed.port, timeout=timeout)


def endpoint(base_url, path):
    return service_url(base_url).path.rstrip("/") + path


def bearer_headers(auth_mode, secret_provider):
    headers = {"Accept": "application/json"}
    if auth_mode == "bearer":
        secret = secret_provider() if secret_provider is not None else None
        if (not isinstance(secret, str) or not 8 <= len(secret) <= 4096 or
                any(ord(c) < 33 or ord(c) > 126 for c in secret)):
            raise ValueError("missing_or_invalid_secret")
        headers["Authorization"] = "Bearer " + secret
    return headers


def probe_snapshot():
    """Synthetic wait only. Constructing a snapshot never submits a Goal."""
    return DecisionSnapshot.parse({
        "schema_version": 1, "snapshot_id": "provider-probe",
        "created_monotonic_ns": time.monotonic_ns(),
        "versions": {"ex_session": "provider-probe", "goal_revision": 0,
                     "config_revision": 0, "catalog_revision": 0,
                     "environment_generation": 0, "gate_epoch": 0,
                     "plugin_generations": {"probe": 1}},
        "goal": {"task_id": "provider-probe", "goal_id": "provider-probe",
                 "goal_text_en": "Wait without taking any action.",
                 "allowed_actions": [], "parameters": {}},
        "observations": [], "owners": [{"owner": "probe", "plugin_generation": 1,
            "status": "available", "candidates": [{"option_id": "probe-wait", "kind": "wait",
                "description": "Wait without dispatch.", "eligible": True}]}],
    })
