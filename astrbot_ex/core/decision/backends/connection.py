"""Shared URL and no-action probe contract for SystemOne clients."""
from __future__ import annotations

import http.client
import ipaddress
import re
import select
import ssl
import threading
from contextlib import contextmanager
import time
from urllib.parse import unquote, urlsplit

from ..models import DecisionSnapshot


def service_url(value, *, allow_tailnet_http=False):
    """Validate before secrets are read. Explicit tailnet HTTP uses numeric addresses only."""
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
            if not (allow_tailnet_http and isinstance(address, ipaddress.IPv4Address)
                    and address in ipaddress.IPv4Network("100.64.0.0/10")):
                raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("invalid_service_url") from None
    return parsed


def open_connection(base_url, timeout, *, allow_tailnet_http=False):
    parsed = service_url(base_url, allow_tailnet_http=allow_tailnet_http)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    # Never resolve the HTTP localhost alias through DNS; connect to literal loopback.
    host = "127.0.0.1" if parsed.scheme == "http" and parsed.hostname == "localhost" else parsed.hostname
    # Omit the default port to preserve the normal stdlib verified TLS setup.
    if parsed.port is None:
        return cls(host, timeout=timeout)
    return cls(host, parsed.port, timeout=timeout)


class ConnectionPool:
    """One idle connection per client. Each borrower owns its socket until response EOF."""

    def __init__(self):
        self._lock = threading.Lock()
        self._idle = None
        self._closed = False
        self._opened = 0
        self._released = 0

    def statistics(self):
        """Snapshot of TCP/TLS connections owned by this provider client."""
        with self._lock:
            idle = int(self._idle is not None)
            return {"opened": self._opened, "released": self._released, "idle": idle,
                    "borrowed": self._opened - self._released - idle}

    def _release(self, conn):
        try:
            conn.close()
        finally:
            with self._lock:
                self._released += 1

    @staticmethod
    def _reusable(connection):
        sock = connection.sock
        if sock is None:
            return False
        try:
            if not select.select([sock], [], [], 0)[0]:
                return True
            if isinstance(sock, ssl.SSLSocket):
                # TLS 1.3 session tickets can make a healthy idle socket readable.
                timeout = sock.gettimeout()
                try:
                    sock.setblocking(False)
                    sock.recv(1)
                except ssl.SSLWantReadError:
                    return True
                finally:
                    sock.settimeout(timeout)
            # EOF or unsolicited application data cannot start another response.
            return False
        except (OSError, ValueError):
            return False

    @contextmanager
    def connection(self, base_url, timeout, *, timings=None, allow_tailnet_http=False):
        parsed = service_url(base_url, allow_tailnet_http=allow_tailnet_http)
        key = (parsed.scheme, parsed.hostname, parsed.port)
        with self._lock:
            if self._closed:
                raise RuntimeError("connection_pool_closed")
            idle, self._idle = self._idle, None
        conn = None
        if idle is not None:
            old_key, old_conn = idle
            if key == old_key and self._reusable(old_conn):
                conn = old_conn
            else:
                self._release(old_conn)
        try:
            reused = conn is not None
            counted = reused
            connect_ms = 0.0
            if not reused:
                started = time.monotonic_ns()
                conn = open_connection(base_url, timeout, allow_tailnet_http=allow_tailnet_http)
                conn.connect()
                with self._lock:
                    self._opened += 1
                counted = True
                connect_ms = (time.monotonic_ns() - started) / 1_000_000
            conn.timeout = timeout
            conn.sock.settimeout(timeout)
            if timings is not None:
                timings.update(connection_reused=reused, connect_tls_ms=connect_ms)
            # Disable HTTPConnection's implicit connect when the caller writes a request.
            # Never send the same POST again after an uncertain write or lost reply.
            conn.auto_open = False
            yield conn
            with self._lock:
                if not self._closed and self._idle is None and conn.sock is not None:
                    self._idle = (key, conn)
                    conn = None
        finally:
            if conn is not None:
                if counted:
                    self._release(conn)
                else:
                    conn.close()

    def close(self):
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, None
        if idle is not None:
            self._release(idle[1])


def endpoint(base_url, path, *, allow_tailnet_http=False):
    return service_url(base_url, allow_tailnet_http=allow_tailnet_http).path.rstrip("/") + path


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
