"""B08 configuration CAS and credentials, separate from execution facts."""
from __future__ import annotations

import copy
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path

from .backends.laya import LayaConfig
from .backends.jev import JevConfig


class ManagementError(RuntimeError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def atomic_json(path, value, *, mode=0o600):
    raw = (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode()
    atomic_bytes(path, raw, mode=mode)


def atomic_bytes(path, raw, *, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ManagementError("unsafe_storage_path", 500)
    fd, name = tempfile.mkstemp(prefix=".decision-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class SecretStore:
    def __init__(self, data_root):
        root = Path(data_root).resolve()
        self.directory = root / "secrets"
        if self.directory.is_symlink():
            raise ManagementError("unsafe_secret_directory", 500)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.resolve() != self.directory:
            raise ManagementError("unsafe_secret_directory", 500)
        os.chmod(self.directory, 0o700)
        self.credential_path = self.directory / "admin.token"
        self._lock = threading.RLock()
        self._redactions = set()
        if self.credential_path.is_symlink():
            raise ManagementError("unsafe_secret_path", 500)
        if not self.credential_path.exists():
            descriptor = os.open(self.credential_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_urlsafe(32) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        os.chmod(self.credential_path, 0o600)
        self._credential = self.credential_path.read_text().strip()
        if not self._credential.isascii() or len(self._credential) < 32 or len(self._credential) > 256:
            raise ManagementError("invalid_management_credential", 500)
        self._redactions.add(self._credential)
        for path in self.directory.glob("jev-*.secret"):
            if path.is_symlink() or not path.is_file():
                raise ManagementError("unsafe_secret_path", 500)
            os.chmod(path, 0o600)
            value = path.read_text()
            if value:
                self._redactions.add(value)

    def authorized(self, header):
        if not isinstance(header, str) or not header.startswith("Bearer "):
            return False
        candidate = header[7:]
        return len(candidate) <= 256 and hmac.compare_digest(candidate.encode("utf-8"), self._credential.encode("ascii"))

    def _path(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(r"jev-[0-9a-f]{32}", reference):
            raise ManagementError("invalid_secret_ref", 400)
        path = self.directory / (reference + ".secret")
        if path.is_symlink():
            raise ManagementError("unsafe_secret_path", 500)
        return path

    def create(self, value):
        if not isinstance(value, str) or not 8 <= len(value) <= 8192 or any(c in value for c in "\r\n\0"):
            raise ManagementError("invalid_secret_value", 400)
        with self._lock:
            reference = "jev-" + secrets.token_hex(16)
            self._redactions.add(value)
            atomic_bytes(self._path(reference), value.encode())
            return reference

    def read(self, reference):
        if reference is None:
            return ""
        with self._lock:
            path = self._path(reference)
            if not path.is_file():
                return ""
            value = path.read_text()
            self._redactions.add(value)
            return value

    def remove(self, reference):
        if reference:
            self._path(reference).unlink(missing_ok=True)

    def redact(self, value):
        with self._lock:
            variants = set(self._redactions)
            frontier = set(variants)
            # Actual Laya body contains a JSON state string inside JSON. Protect
            # the legal secret characters through these bounded encoding layers.
            for _ in range(4):
                frontier = {json.dumps(item, ensure_ascii=ascii_only)[1:-1]
                            for item in frontier for ascii_only in (True, False)} - variants
                variants.update(frontier)
            markers = sorted(variants, key=len, reverse=True)
        def walk(item):
            if isinstance(item, str):
                for marker in markers:
                    if marker:
                        item = item.replace(marker, "[REDACTED]")
                return item
            if isinstance(item, dict):
                return {walk(str(key)): walk(val) for key, val in item.items()}
            if isinstance(item, (list, tuple)):
                return [walk(val) for val in item]
            return item
        return walk(value)


class DecisionConfigStore:
    def __init__(self, data_root, ex_session, *, port=8769):
        self.path = Path(data_root).resolve() / "profiles" / "default" / "decision.json"
        self.ex_session, self.port = ex_session, port
        self.secrets = SecretStore(data_root)
        self._lock = threading.RLock()
        self.revision = 0
        self.effective_revision = None
        self.effective = None
        self.saved = self.defaults()
        if self.path.exists():
            self._load()

    def defaults(self):
        return {"backend": "mock", "mock": {"kind": "wait"},
                "laya": asdict(LayaConfig(port=self.port)),
                "jev": {"model": JevConfig().model, "mode": "shadow", "allow_live_http": False,
                        "deadline_ms": 1500, "min_interval_ms": 500}, "secret_ref": None}

    def validate(self, raw, *, allow_reference=False):
        defaults = self.defaults()
        if not isinstance(raw, dict) or set(raw) != set(defaults):
            raise ManagementError("invalid_config", 400)
        value = copy.deepcopy(raw)
        if not isinstance(value["backend"], str) or value["backend"] not in {"mock", "jev", "laya"}:
            raise ManagementError("unknown_backend", 400)
        if not isinstance(value["mock"], dict) or set(value["mock"]) != {"kind"} or not isinstance(value["mock"]["kind"], str) or value["mock"]["kind"] not in {
                "wait", "request_replan", "start", "cancel"}:
            raise ManagementError("invalid_mock_config", 400)
        if not isinstance(value["laya"], dict) or set(value["laya"]) != set(defaults["laya"]):
            raise ManagementError("invalid_laya_config", 400)
        for field in ("model", "revision", "port"):
            if value["laya"][field] != defaults["laya"][field]:
                raise ManagementError("readonly_laya_identity", 400)
        try:
            LayaConfig(**value["laya"])
        except Exception:
            raise ManagementError("invalid_laya_config", 400) from None
        # This management release does not increase the previously verified budgets.
        for field in ("deadline_ms", "max_request_bytes", "max_response_bytes"):
            if value["laya"][field] > defaults["laya"][field]:
                raise ManagementError("unverified_laya_budget", 400)
        if not isinstance(value["jev"], dict) or set(value["jev"]) != set(defaults["jev"]):
            raise ManagementError("invalid_jev_config", 400)
        if value["jev"]["model"] != defaults["jev"]["model"] or value["jev"]["mode"] != "shadow":
            raise ManagementError("readonly_jev_identity", 400)
        try:
            JevConfig(**value["jev"])
        except Exception:
            raise ManagementError("invalid_jev_config", 400) from None
        reference = value["secret_ref"]
        if reference is not None:
            self.secrets._path(reference)
        if not allow_reference and reference != self.saved["secret_ref"]:
            raise ManagementError("secret_requires_secret_endpoint", 400)
        return value

    def check(self, expected_revision, ex_session):
        if ex_session != self.ex_session:
            raise ManagementError("session_conflict")
        if type(expected_revision) is not int or expected_revision != self.revision:
            raise ManagementError("revision_conflict")

    def _load(self):
        try:
            if self.path.is_symlink():
                raise ValueError()
            raw = json.loads(self.path.read_text())
            if set(raw) != {"schema_version", "revision", "config"} or raw["schema_version"] != 1:
                raise ValueError()
            if type(raw["revision"]) is not int or raw["revision"] < 0:
                raise ValueError()
            saved = self.validate(raw["config"], allow_reference=True)
            self.revision, self.saved = raw["revision"], saved
        except Exception:
            raise ManagementError("invalid_saved_config", 500) from None

    def _publish(self, value, revision):
        try:
            atomic_json(self.path, {"schema_version": 1, "revision": revision, "config": value})
        except ManagementError:
            raise
        except Exception:
            raise ManagementError("config_write_failed", 500) from None
        self.saved, self.revision = value, revision

    def save(self, raw, expected_revision, ex_session):
        with self._lock:
            self.check(expected_revision, ex_session)
            self._publish(self.validate(raw), self.revision + 1)
            return self.get()

    def update_secret(self, action, value, expected_revision, ex_session):
        with self._lock:
            self.check(expected_revision, ex_session)
            if not isinstance(action, str) or action not in {"set", "keep", "clear"} or (action != "set" and value is not None):
                raise ManagementError("invalid_secret_action", 400)
            if action == "keep":
                return self.get()
            config = copy.deepcopy(self.saved)
            previous = config["secret_ref"]
            reference = self.secrets.create(value) if action == "set" else None
            config["secret_ref"] = reference
            try:
                self._publish(config, self.revision + 1)
            except Exception:
                self.secrets.remove(reference)
                raise
            # Old immutable secret files stay inaccessible through saved references.
            # A removal error cannot roll back an already committed configuration.
            try:
                self.secrets.remove(previous)
            except OSError:
                pass
            return self.get()

    def mark_effective(self, revision, config):
        with self._lock:
            # Installed facts remain true even when a newer stop cancels the operation.
            self.effective_revision, self.effective = revision, copy.deepcopy(config)

    def reload_after_restore(self):
        with self._lock:
            previous = self.revision
            if self.path.exists():
                self._load()
            else:
                self.saved = self.defaults()
            self._publish(self.saved, max(previous, self.revision) + 1)
            # Saved configuration alone cannot replace the installed backend.
            return self.get()

    def get(self):
        with self._lock:
            return {"ex_session": self.ex_session, "revision": self.revision,
                    "effective_revision": self.effective_revision,
                    "saved": copy.deepcopy(self.saved), "effective": copy.deepcopy(self.effective),
                    "secrets": {"jev": {"configured": bool(self.secrets.read(self.saved["secret_ref"]))}}}
