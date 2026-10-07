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
from dataclasses import asdict, fields
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


def _directory_sync_required():
    return os.name != "nt"


def _sync_directory(path):
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _rollback_copy(path, source=None):
    # A same-directory hardlink protects the old inode without rewriting secrets.
    fd, name = tempfile.mkstemp(prefix=".decision-" + path.name + "-rollback-", dir=path.parent)
    os.close(fd)
    if source is not None:
        try:
            os.unlink(name)
            os.link(source, name, follow_symlinks=False)
        except Exception:
            if os.path.exists(name):
                os.unlink(name)
            raise
    return name


def storage_recovery_pending(path):
    path = Path(path)
    return any(path.parent.glob(".decision-" + path.name + "-rollback-*"))


def atomic_bytes(path, raw, *, mode=0o600, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ManagementError("unsafe_storage_path", 500)
    if storage_recovery_pending(path):
        raise ManagementError("storage_write_uncertain", 500)
    if exclusive and path.exists():
        raise FileExistsError()
    fd, name = tempfile.mkstemp(prefix=".decision-", dir=path.parent)
    backup = restore = None
    installed = uncertain = False
    try:
        chmod = getattr(os, "fchmod", None)
        if chmod is not None:
            chmod(fd, mode)
        # Until fdopen succeeds, this scope still owns the raw descriptor.
        output = os.fdopen(fd, "wb")
        fd = None
        with output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        sync = _directory_sync_required()
        existed = path.exists()
        backup = _rollback_copy(path, path if existed else None)
        if sync:
            # A crash after publication must leave a durable recovery fence.
            _sync_directory(path.parent)
        if exclusive:
            os.link(name, path)  # Atomic no-clobber publication of immutable keys.
        else:
            os.replace(name, path)
        installed = True
        if sync:
            try:
                _sync_directory(path.parent)
            except OSError:
                try:
                    if existed:
                        restore = _rollback_copy(path, backup)
                        os.replace(restore, path)
                    else:
                        path.unlink()
                    _sync_directory(path.parent)
                except OSError:
                    # Keep the protected inode/absence marker for storage recovery.
                    uncertain = True
                    raise ManagementError("storage_write_uncertain", 500) from None
                raise
    finally:
        cleanup_failed = False
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                cleanup_failed = True
        for temporary in (name, restore):
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                except OSError:
                    cleanup_failed = True
        # Never let a temporary-cleanup error discard the recovery fence or
        # mask an uncertain publication with an ordinary precommit failure.
        uncertain = uncertain or (installed and cleanup_failed)
        if backup is not None and not uncertain:
            try:
                os.unlink(backup)
            except OSError:
                uncertain = True
        if uncertain:
            raise ManagementError("storage_write_uncertain", 500) from None
        if cleanup_failed:
            raise ManagementError("storage_cleanup_failed", 500) from None


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
            try:
                stream = os.fdopen(descriptor, "w")
                descriptor = None
                with stream:
                    stream.write(secrets.token_urlsafe(32) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        os.chmod(self.credential_path, 0o600)
        self._credential = self.credential_path.read_text(encoding="utf-8").strip()
        if not self._credential.isascii() or len(self._credential) < 32 or len(self._credential) > 256:
            raise ManagementError("invalid_management_credential", 500)
        self._redactions.add(self._credential)
        for path in (*self.directory.glob("jev-*.secret"), *self.directory.glob("laya-*.secret")):
            if path.is_symlink() or not path.is_file():
                raise ManagementError("unsafe_secret_path", 500)
            os.chmod(path, 0o600)
            value = path.read_text(encoding="utf-8")
            if value:
                self._redactions.add(value)

    def authorized(self, header):
        if not isinstance(header, str) or not header.startswith("Bearer "):
            return False
        candidate = header[7:]
        return len(candidate) <= 256 and hmac.compare_digest(candidate.encode("utf-8"), self._credential.encode("ascii"))

    def _path(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(r"(?:jev|laya)-[0-9a-f]{32}", reference):
            raise ManagementError("invalid_secret_ref", 400)
        path = self.directory / (reference + ".secret")
        if path.is_symlink():
            raise ManagementError("unsafe_secret_path", 500)
        return path

    def create(self, value, *, provider="jev"):
        if provider not in {"jev", "laya"}:
            raise ManagementError("invalid_secret_provider", 400)
        self.remember(value)
        with self._lock:
            reference = provider + "-" + secrets.token_hex(16)
            self._redactions.add(value)
            try:
                atomic_bytes(self._path(reference), value.encode(), exclusive=True)
            except FileExistsError:
                raise ManagementError("secret_ref_conflict", 500) from None
            except OSError:
                if storage_recovery_pending(self._path(reference)):
                    raise ManagementError("storage_write_uncertain", 500) from None
                raise ManagementError("secret_write_failed", 500) from None
            return reference

    def remember(self, value):
        """Register a temporary probe key without creating a file or changing CAS."""
        if (not isinstance(value, str) or not 8 <= len(value) <= 4096 or
                not value.strip() or any(c in value for c in "\r\n\0")):
            raise ManagementError("invalid_secret_value", 400)
        with self._lock:
            self._redactions.add(value)
        return value

    def read(self, reference):
        if reference is None:
            return ""
        with self._lock:
            path = self._path(reference)
            if not path.is_file():
                return ""
            value = path.read_text(encoding="utf-8")
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
        self.storage_uncertain = (any(self.path.parent.glob(".decision-decision.json-rollback-*")) or
                                  any(self.secrets.directory.glob(".decision-*-rollback-*")))
        if self.path.exists():
            self._load()

    def defaults(self):
        laya = asdict(LayaConfig(port=self.port))
        for name in ("base_url", "auth_mode", "port", "model"):
            laya.pop(name)
        laya.update(service_connection={"mode": "external", "base_url": f"http://127.0.0.1:{self.port}",
                                       "model": "typed-decisions", "auth_mode": "none"}, deployment=None)
        return {"backend": "mock", "mock": {"kind": "wait"}, "laya": laya,
                "jev": {"service_connection": {"base_url": "https://api.typesafe.ai",
                        "model": JevConfig().model, "auth_mode": "bearer"},
                        "mode": "shadow", "allow_live_http": False, "deadline_ms": 1500,
                        "min_interval_ms": 500, "min_confidence": 0.6},
                "secret_ref": None, "laya_secret_ref": None}

    def backend_config(self, name, config=None):
        """Single validated mapping shared by management/factories/probes."""
        if name not in ("jev", "laya"):
            raise ManagementError("unknown_backend", 400)
        with self._lock:
            self._check_storage()
            config = self.saved if config is None else self.validate(config, allow_reference=True)
            return self._backend_config(name, config)

    def _backend_config(self, name, config):
        raw = copy.deepcopy(config[name])
        connection = raw.pop("service_connection")
        if name == "laya":
            raw.pop("deployment")
            raw.update(base_url=connection["base_url"], auth_mode=connection["auth_mode"],
                       model=connection["model"], port=self.port)
            return LayaConfig(**raw)
        if name == "jev":
            return JevConfig(**raw, base_url=connection["base_url"], model=connection["model"])
        raise ManagementError("unknown_backend", 400)

    def laya_deployment(self, config=None, *, output, state_path=None):
        from .owned_laya import Deployment
        with self._lock:
            self._check_storage()
            config = copy.deepcopy(self.saved) if config is None else self.validate(config, allow_reference=True)
        value = config["laya"]
        if value["service_connection"]["mode"] != "owned":
            raise ManagementError("external_service_not_owned", 400)
        deployment = value["deployment"]
        if deployment is None:
            raise ManagementError("deployment_required", 400)
        return Deployment(python=deployment["python"], cache=deployment["cache"], output=output,
                          state_path=state_path, port=self.port, device=deployment["device"],
                          auth_mode=value["service_connection"]["auth_mode"])

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
        for name in ("jev", "laya"):
            item = value[name]
            if not isinstance(item, dict) or set(item) != set(defaults[name]):
                raise ManagementError("invalid_" + name + "_config", 400)
            connection = item["service_connection"]
            expected = {"base_url", "model", "auth_mode"} | ({"mode"} if name == "laya" else set())
            if not isinstance(connection, dict) or set(connection) != expected:
                raise ManagementError("invalid_service_connection", 400)
            allowed_auth = ("bearer",) if name == "jev" else ("none", "bearer")
            if connection["auth_mode"] not in allowed_auth:
                raise ManagementError("invalid_auth_mode", 400)
            if connection["model"] != defaults[name]["service_connection"]["model"]:
                raise ManagementError("unsupported_model", 400)
            if name == "laya":
                if connection["mode"] not in ("external", "owned"):
                    raise ManagementError("invalid_service_mode", 400)
                deployment = item["deployment"]
                if connection["mode"] == "external" and deployment is not None:
                    raise ManagementError("external_deployment_forbidden", 400)
                if connection["mode"] == "owned":
                    if connection["base_url"] != f"http://127.0.0.1:{self.port}":
                        raise ManagementError("owned_requires_loopback", 400)
                    if deployment is not None:
                        if (not isinstance(deployment, dict) or set(deployment) != {"launcher", "python", "cache", "device"} or
                                deployment["launcher"] != "subprocess" or deployment["device"] not in ("cuda", "cpu") or
                                any(not isinstance(deployment[k], str) or not Path(deployment[k]).is_absolute()
                                    for k in ("python", "cache"))):
                            raise ManagementError("invalid_deployment", 400)
                for field in ("deadline_ms", "max_request_bytes", "max_response_bytes"):
                    if type(item[field]) is not int or item[field] > defaults[name][field]:
                        raise ManagementError("unverified_laya_budget", 400)
            try:
                self._backend_config(name, value)
            except Exception as exc:
                raise ManagementError(getattr(exc, "code", "invalid_" + name + "_config"), 400) from None
        for key, provider in (("secret_ref", "jev"), ("laya_secret_ref", "laya")):
            reference = value[key]
            if reference is not None:
                self.secrets._path(reference)
                if not reference.startswith(provider + "-"):
                    raise ManagementError("invalid_secret_provider", 400)
            if not allow_reference and reference != self.saved[key]:
                raise ManagementError("secret_requires_secret_endpoint", 400)
        return value

    def _check_storage(self):
        self.storage_uncertain = (self.storage_uncertain or storage_recovery_pending(self.path) or
                                  any(self.secrets.directory.glob(".decision-*-rollback-*")))
        if self.storage_uncertain:
            raise ManagementError("storage_write_uncertain", 500)

    def check(self, expected_revision, ex_session):
        self._check_storage()
        if ex_session != self.ex_session:
            raise ManagementError("session_conflict")
        if type(expected_revision) is not int or expected_revision != self.revision:
            raise ManagementError("revision_conflict")

    def _load(self):
        try:
            if self.path.is_symlink():
                raise ValueError()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if set(raw) != {"schema_version", "revision", "config"} or type(raw["schema_version"]) is not int or raw["schema_version"] not in {1, 2}:
                raise ValueError()
            if raw["schema_version"] == 1:
                old = raw["config"]
                legacy_laya = {field.name for field in fields(LayaConfig)} - {"base_url", "auth_mode", "execution_enabled"}
                if (not isinstance(old, dict) or set(old) != {"backend", "mock", "laya", "jev", "secret_ref"} or
                        not isinstance(old["laya"], dict) or set(old["laya"]) != legacy_laya or
                        not isinstance(old["jev"], dict) or set(old["jev"]) != {
                            "model", "mode", "allow_live_http", "deadline_ms", "min_interval_ms"}):
                    raise ValueError()
                migrated = self.defaults()
                migrated.update(backend=old["backend"], mock=old["mock"], secret_ref=old["secret_ref"])
                migrated["laya"].update({k: v for k, v in old["laya"].items() if k not in {"port", "model"}})
                migrated["laya"]["service_connection"].update(mode="owned", model=old["laya"]["model"],
                    base_url=f"http://127.0.0.1:{old['laya']['port']}")
                migrated["jev"].update({k: v for k, v in old["jev"].items() if k != "model"})
                migrated["jev"]["service_connection"]["model"] = old["jev"]["model"]
                raw["config"] = migrated
            if type(raw["revision"]) is not int or raw["revision"] < 0:
                raise ValueError()
            saved = self.validate(raw["config"], allow_reference=True)
            self.revision, self.saved = raw["revision"], saved
        except Exception:
            raise ManagementError("invalid_saved_config", 500) from None

    def _publish(self, value, revision):
        self._check_storage()
        try:
            atomic_json(self.path, {"schema_version": 2, "revision": revision, "config": value})
        except ManagementError as exc:
            if exc.code == "storage_write_uncertain":
                self.storage_uncertain = True
                # Reflect the currently readable envelope, without claiming durability.
                try:
                    if self.path.exists():
                        self._load()
                    else:
                        self.saved, self.revision = self.defaults(), 0
                except ManagementError:
                    pass  # Writes/activation stay fenced; retain last known diagnosis.
            raise
        except Exception:
            if storage_recovery_pending(self.path):
                self.storage_uncertain = True
                raise ManagementError("storage_write_uncertain", 500) from None
            raise ManagementError("config_write_failed", 500) from None
        self.saved, self.revision = value, revision

    def save(self, raw, expected_revision, ex_session):
        with self._lock:
            self.check(expected_revision, ex_session)
            self._publish(self.validate(raw), self.revision + 1)
            return self.get()

    def update_secret(self, action, value, expected_revision, ex_session, *, provider="jev"):
        with self._lock:
            self.check(expected_revision, ex_session)
            if provider not in {"jev", "laya"}:
                raise ManagementError("invalid_secret_provider", 400)
            if not isinstance(action, str) or action not in {"set", "keep", "clear"} or (action != "set" and value is not None):
                raise ManagementError("invalid_secret_action", 400)
            if action == "keep":
                return self.get()
            config = copy.deepcopy(self.saved)
            key = "secret_ref" if provider == "jev" else "laya_secret_ref"
            previous = config[key]
            reference = None
            try:
                reference = self.secrets.create(value, provider=provider) if action == "set" else None
                config[key] = reference
                self._publish(config, self.revision + 1)
            except Exception as exc:
                if isinstance(exc, ManagementError) and exc.code == "storage_write_uncertain":
                    self.storage_uncertain = True
                if not self.storage_uncertain:
                    try:
                        self.secrets.remove(reference)
                    except OSError:
                        raise ManagementError("secret_cleanup_failed", 500) from None
                raise
            # A cleanup error cannot roll back the committed CAS or delete its key.
            try:
                self.secrets.remove(previous)
            except OSError:
                raise ManagementError("secret_cleanup_failed", 500) from None
            return self.get()

    def mark_effective(self, revision, config):
        with self._lock:
            self._check_storage()
            # Installed facts remain true even when a newer stop cancels the operation.
            self.effective_revision, self.effective = revision, copy.deepcopy(config)

    def reload_after_restore(self):
        with self._lock:
            self._check_storage()
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
                    "effective_revision": self.effective_revision, "storage_uncertain": self.storage_uncertain,
                    "saved": copy.deepcopy(self.saved), "effective": copy.deepcopy(self.effective),
                    "secrets": {"jev": {"configured": bool(self.secrets.read(self.saved["secret_ref"]))},
                                "laya": {"configured": bool(self.secrets.read(self.saved["laya_secret_ref"]))}}}
