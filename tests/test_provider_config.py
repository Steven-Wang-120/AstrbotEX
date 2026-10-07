"""C02/C03 storage/migration regressions; no management server or model."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.decision.config import DecisionConfigStore, ManagementError, SecretStore, atomic_bytes
from astrbot_ex.core.decision import config as config_module
from astrbot_ex.core.decision.backends.laya import LayaConfig


@contextmanager
def directory_fault(directory, *, stage="fsync", rollback=None):
    """Exercise POSIX control flow; only Windows substitutes the directory primitive."""
    directory = Path(directory)
    original_open, original_fsync, original_replace = os.open, os.fsync, os.replace
    original_sync = config_module._sync_directory
    opened, live, fsyncs, replacements = [], set(), [], []
    directory_calls, directory_fsyncs = [], []
    def open_fd(path, flags, *args, **kwargs):
        is_directory = Path(path) == directory
        if is_directory:
            directory_calls.append(True)
            if stage in ("open", "post_open") and len(directory_calls) == (1 if stage == "open" else 2):
                raise OSError("fixture directory open")
            if os.name == "nt":
                path = directory / "directory-fd-fixture"
                flags = os.O_CREAT | os.O_RDWR
        fd = original_open(path, flags, *args, **kwargs)
        opened.append(fd)
        if is_directory:
            live.add(fd)
        return fd
    def fsync(fd):
        fsyncs.append("directory" if fd in live else "file")
        if fd in live:
            directory_fsyncs.append(True)
            if stage == "fsync" and (len(directory_fsyncs) == 2 or (
                    rollback == "sync" and len(directory_fsyncs) > 2)):
                raise OSError("fixture directory fsync")
        if os.name != "nt" or fd not in live:
            original_fsync(fd)
    original_close = os.close
    def close(fd):
        live.discard(fd)
        original_close(fd)
    def replace(source, target):
        if Path(target).parent == directory:
            replacements.append(str(target))
            if rollback == "replace" and len(replacements) == 2:
                raise OSError("fixture rollback replace")
        original_replace(source, target)
    def sync(path):
        if Path(path) == directory or os.name != "nt":
            original_sync(path)
    with patch.object(config_module, "_directory_sync_required", return_value=True), \
         patch.object(config_module, "_sync_directory", side_effect=sync), \
         patch.object(os, "open", side_effect=open_fd), patch.object(os, "close", side_effect=close), \
         patch.object(os, "fsync", side_effect=fsync), patch.object(os, "replace", side_effect=replace):
        yield {"opened": opened, "fsyncs": fsyncs, "replacements": replacements}
    for fd in set(opened):
        try:
            os.fstat(fd)
        except OSError:
            continue
        raise AssertionError("leaked descriptor")
    (directory / "directory-fd-fixture").unlink(missing_ok=True)


class ProviderConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DecisionConfigStore(self.root, "session")

    def test_external_default_mapping_has_no_local_deployment_preflight(self):
        saved = self.store.get()["saved"]
        saved["backend"] = "laya"
        saved["laya"]["service_connection"].update(base_url="https://remote.example.invalid/prefix", auth_mode="bearer")
        with patch("astrbot_ex.core.decision.owned_laya.Deployment.preflight", side_effect=AssertionError("external")):
            self.store.save(saved, 0, "session")
            config = self.store.backend_config("laya")
        self.assertEqual(config.base_url, "https://remote.example.invalid/prefix")
        self.assertEqual(config.auth_mode, "bearer")
        with self.assertRaises(ManagementError) as caught:
            self.store.laya_deployment(output=self.root / "output")
        self.assertEqual(caught.exception.code, "external_service_not_owned")
        self.assertEqual(json.loads(self.store.path.read_text())["schema_version"], 2)

    def test_owned_deployment_mapping_and_missing_explicit_deployment(self):
        saved = self.store.get()["saved"]
        saved["laya"]["service_connection"]["mode"] = "owned"
        self.store.save(saved, 0, "session")
        with self.assertRaises(ManagementError) as caught:
            self.store.laya_deployment(output=self.root / "output")
        self.assertEqual(caught.exception.code, "deployment_required")
        cache = self.root / "cache"
        cache.mkdir()
        saved["laya"]["deployment"] = {"launcher": "subprocess", "python": sys.executable, "cache": str(cache), "device": "cpu"}
        saved["laya"]["service_connection"]["auth_mode"] = "bearer"
        self.store.save(saved, 1, "session")
        deployment = self.store.laya_deployment(output=self.root / "output", state_path=self.root / "state")
        self.assertEqual(deployment.python, Path(sys.executable))
        self.assertEqual(deployment.cache, cache)
        self.assertEqual(deployment.auth_mode, "bearer")
        deployment.preflight()

    def test_invalid_configs_and_mapping_reject_without_write(self):
        changes = [
            lambda x: x["laya"]["service_connection"].update(base_url="http://remote.example.invalid"),
            lambda x: x["jev"]["service_connection"].update(model="jev-latest"),
            lambda x: x["jev"]["service_connection"].update(model="jev-1.14.0"),
            lambda x: x["laya"]["service_connection"].update(model="english"),
            lambda x: x["laya"].update(allow_test_execution=True),
            lambda x: x["jev"].update(allow_test_execution=True),
            lambda x: x["laya"].update(deployment={"python": "relative"}),
            lambda x: x["laya"].update(deadline_ms=True),
            lambda x: x["laya"].update(revision="latest"),
            lambda x: x["jev"].update(min_confidence=float("nan")),
        ]
        before = self.store.get()
        for change in changes:
            with self.subTest(change=change):
                invalid = copy.deepcopy(before["saved"])
                change(invalid)
                with self.assertRaises(ManagementError):
                    self.store.save(invalid, 0, "session")
                with self.assertRaises(ManagementError):
                    self.store.backend_config("laya", invalid)
                self.assertEqual(self.store.get(), before)
                self.assertFalse(self.store.path.exists())

    def test_v1_migrates_preserving_key_and_revision_without_start_or_disk_write(self):
        reference = self.store.secrets.create("v1-jev-fixture-secret")
        laya = asdict(LayaConfig())
        for key in ("base_url", "auth_mode", "execution_enabled"):
            laya.pop(key)
        old = {"backend": "laya", "mock": {"kind": "wait"}, "laya": laya,
               "jev": {"model": "jev-1.13.0", "mode": "shadow", "allow_live_http": False,
                       "deadline_ms": 1500, "min_interval_ms": 500}, "secret_ref": reference}
        self.store.path.parent.mkdir(parents=True)
        content = json.dumps({"schema_version": 1, "revision": 9, "config": old}).encode()
        self.store.path.write_bytes(content)
        with patch("subprocess.Popen", side_effect=AssertionError("migration must not start")), \
             patch("http.client.HTTPConnection", side_effect=AssertionError("migration must not connect")), \
             patch("http.client.HTTPSConnection", side_effect=AssertionError("migration must not connect")):
            restored = DecisionConfigStore(self.root, "new-session")
            self.assertEqual(restored.revision, 9)
            self.assertEqual(restored.saved["laya"]["service_connection"]["mode"], "owned")
            self.assertIsNone(restored.saved["laya"]["deployment"])
            self.assertFalse(restored.saved["laya"]["execution_enabled"])
            self.assertEqual(restored.secrets.read(restored.saved["secret_ref"]), "v1-jev-fixture-secret")
            self.assertIsNone(restored.effective)
            self.assertEqual(restored.path.read_bytes(), content)
            restored.save(restored.get()["saved"], 9, "new-session")
        self.assertEqual(json.loads(restored.path.read_text())["schema_version"], 2)
        self.assertEqual(restored.revision, 10)

    def test_separate_provider_keys_startup_redaction_and_ephemeral_probe_key(self):
        jev = 'Jev-fixture-"quoted"-\\path-中文'
        laya = 'Laya-fixture-"quoted"-\\path-中文'
        self.store.update_secret("set", jev, 0, "session")
        self.store.update_secret("set", laya, 1, "session", provider="laya")
        saved = self.store.saved
        self.assertTrue(saved["secret_ref"].startswith("jev-"))
        self.assertTrue(saved["laya_secret_ref"].startswith("laya-"))
        restored = DecisionConfigStore(self.root, "again")
        # No prior read of either reference: redaction must already protect startup diagnostics.
        for marker in (jev, laya):
            body = json.dumps({"state": json.dumps({"echo": marker}, ensure_ascii=True)})
            safe = restored.secrets.redact({"direct": marker, "body": body})
            self.assertEqual(safe["direct"], "[REDACTED]")
            self.assertEqual(json.loads(json.loads(safe["body"])["state"])["echo"], "[REDACTED]")
        before, files = restored.get(), set(restored.secrets.directory.iterdir())
        restored.secrets.remember("ephemeral-probe-fixture-key")
        self.assertEqual(restored.secrets.redact("ephemeral-probe-fixture-key"), "[REDACTED]")
        self.assertEqual(restored.get(), before)
        self.assertEqual(set(restored.secrets.directory.iterdir()), files)
        self.store.update_secret("clear", None, 2, "session", provider="laya")
        self.assertEqual(self.store.secrets.read(self.store.saved["secret_ref"]), jev)
        self.assertIsNone(self.store.saved["laya_secret_ref"])
        self.assertNotIn(jev, json.dumps(self.store.get()))
        for action, value in (("set", " " * 8), ("set", ""), ("keep", "key"), ("clear", "key")):
            with self.assertRaises(ManagementError):
                self.store.update_secret(action, value, 3, "session", provider="laya")
        invalid = self.store.get()["saved"]
        invalid["laya_secret_ref"] = invalid["secret_ref"]
        with self.assertRaises(ManagementError):
            self.store.validate(invalid, allow_reference=True)

    def test_invalid_saved_schema_and_cas_do_not_publish(self):
        for envelope in ({"schema_version": True, "revision": 0, "config": self.store.saved},
                         {"schema_version": 3, "revision": 0, "config": self.store.saved},
                         {"schema_version": 2, "revision": True, "config": self.store.saved},
                         {"schema_version": 2, "revision": -1, "config": self.store.saved}):
            with self.subTest(envelope=envelope):
                self.store.path.parent.mkdir(parents=True, exist_ok=True)
                self.store.path.write_text(json.dumps(envelope))
                with self.assertRaises(ManagementError) as caught:
                    DecisionConfigStore(self.root, "new")
                self.assertEqual(caught.exception.code, "invalid_saved_config")
        for revision, session in ((True, "session"), (1, "session"), (0, "other")):
            with self.assertRaises(ManagementError):
                self.store.save(self.store.get()["saved"], revision, session)
        self.assertEqual(self.store.revision, 0)

    def test_directory_open_and_second_fsync_failure_restore_saved_cas_and_both_keys(self):
        for operation in ("save", "set", "clear"):
            for stage in ("open", "post_open", "fsync"):
                with self.subTest(operation=operation, stage=stage), tempfile.TemporaryDirectory() as root:
                    store = DecisionConfigStore(root, "session")
                    store.update_secret("set", "old-jev-fixture-key", 0, "session")
                    store.update_secret("set", "old-laya-fixture-key", 1, "session", provider="laya")
                    before, content = store.get(), store.path.read_bytes()
                    initial_files = set(store.secrets.directory.iterdir())
                    with directory_fault(store.path.parent, stage=stage) as trace:
                        with self.assertRaises(ManagementError) as caught:
                            if operation == "save":
                                changed = copy.deepcopy(store.saved)
                                changed["mock"]["kind"] = "request_replan"
                                store.save(changed, 2, "session")
                            else:
                                store.update_secret(operation, "new-laya-fixture-key" if operation == "set" else None,
                                                    2, "session", provider="laya")
                    self.assertEqual(caught.exception.code, "config_write_failed")
                    if stage == "fsync":
                        self.assertEqual(trace["fsyncs"].count("directory"), 3)  # Fence, publish, recovery.
                        self.assertEqual(trace["fsyncs"][-1], "directory")
                    self.assertEqual(store.path.read_bytes(), content)
                    self.assertEqual(store.get(), before)
                    self.assertEqual(set(store.secrets.directory.iterdir()), initial_files)
                    self.assertEqual(list(store.path.parent.glob(".decision-*")), [])
                    restored = DecisionConfigStore(root, "restart")
                    self.assertEqual(restored.revision, before["revision"])
                    self.assertEqual(restored.saved, before["saved"])
                    self.assertEqual(restored.secrets.read(restored.saved["secret_ref"]), "old-jev-fixture-key")
                    self.assertEqual(restored.secrets.read(restored.saved["laya_secret_ref"]), "old-laya-fixture-key")
                    restored.save(restored.get()["saved"], before["revision"], "restart")
                    final = DecisionConfigStore(root, "restart-again")
                    self.assertEqual(final.revision, before["revision"] + 1)
                    self.assertEqual(final.saved, restored.saved)

    def test_rollback_replace_or_sync_failure_is_uncertain_keeps_all_keys_and_blocks_cas(self):
        for rollback in ("replace", "sync"):
            with self.subTest(rollback=rollback), tempfile.TemporaryDirectory() as root:
                store = DecisionConfigStore(root, "session")
                store.update_secret("set", "old-jev-fixture-key", 0, "session")
                store.update_secret("set", "old-laya-fixture-key", 1, "session", provider="laya")
                before = store.get()
                with directory_fault(store.path.parent, rollback=rollback):
                    with self.assertRaises(ManagementError) as caught:
                        store.update_secret("set", "new-laya-fixture-key", 2, "session", provider="laya")
                self.assertEqual(caught.exception.code, "storage_write_uncertain")
                current = json.loads(store.path.read_text())
                self.assertTrue(store.get()["storage_uncertain"])
                self.assertEqual(store.saved, current["config"])
                self.assertEqual(store.revision, current["revision"])
                self.assertEqual(store.revision, 3 if rollback == "replace" else 2)
                values = {path.read_text() for path in store.secrets.directory.glob("*.secret")}
                self.assertEqual(values, {"old-jev-fixture-key", "old-laya-fixture-key", "new-laya-fixture-key"})
                protected = list(store.path.parent.glob(".decision-decision.json-rollback-*"))
                self.assertEqual(len(protected), 1)
                self.assertEqual(json.loads(protected[0].read_text())["config"], before["saved"])
                restored = DecisionConfigStore(root, "restart")
                self.assertTrue(restored.storage_uncertain)
                self.assertEqual(restored.revision, store.revision)
                self.assertEqual(restored.saved, store.saved)
                for reference in (current["config"]["secret_ref"], current["config"]["laya_secret_ref"]):
                    self.assertTrue(restored.secrets.read(reference))
                for instance in (store, restored):
                    for callback in (lambda: instance.save(instance.saved, instance.revision, instance.ex_session),
                                     lambda: instance.update_secret("clear", None, instance.revision, instance.ex_session),
                                     lambda: instance.backend_config("laya"), instance.reload_after_restore):
                        with self.assertRaises(ManagementError) as blocked:
                            callback()
                        self.assertEqual(blocked.exception.code, "storage_write_uncertain")
                self.assertNotIn("new-laya-fixture-key", json.dumps(store.get()))

    def test_first_creation_failed_directory_durability_restores_absence_or_fences_restart(self):
        for operation in ("save", "set"):
            for rollback in (None, "sync", "unlink"):
                with self.subTest(operation=operation, rollback=rollback), tempfile.TemporaryDirectory() as root:
                    store = DecisionConfigStore(root, "session")
                    original_unlink = Path.unlink
                    def unlink(path, *args, **kwargs):
                        if rollback == "unlink" and path == store.path:
                            raise OSError("fixture rollback unlink")
                        return original_unlink(path, *args, **kwargs)
                    with directory_fault(store.path.parent, rollback=rollback), patch.object(Path, "unlink", unlink):
                        with self.assertRaises(ManagementError) as caught:
                            if operation == "save":
                                store.save(store.saved, 0, "session")
                            else:
                                store.update_secret("set", "first-jev-fixture-key", 0, "session")
                    self.assertEqual(caught.exception.code, "storage_write_uncertain" if rollback else "config_write_failed")
                    restored = DecisionConfigStore(root, "restart")
                    self.assertEqual(store.saved, restored.saved)
                    self.assertEqual(store.revision, restored.revision)
                    self.assertEqual(store.storage_uncertain, bool(rollback))
                    self.assertEqual(restored.storage_uncertain, bool(rollback))
                    self.assertEqual(store.path.exists(), rollback == "unlink")
                    self.assertEqual(len(list(store.secrets.directory.glob("*.secret"))),
                                     int(operation == "set" and bool(rollback)))
                    if rollback:
                        with self.assertRaises(ManagementError):
                            restored.mark_effective(restored.revision, restored.saved)
                        with self.assertRaises(ManagementError):
                            restored.backend_config("jev", restored.saved)

    def test_secret_creation_durability_failure_is_classified_and_restart_fenced_when_ambiguous(self):
        for rollback in (None, "sync"):
            with self.subTest(rollback=rollback), tempfile.TemporaryDirectory() as root:
                store = DecisionConfigStore(root, "session")
                before = store.get()
                with directory_fault(store.secrets.directory, rollback=rollback):
                    with self.assertRaises(ManagementError) as caught:
                        store.update_secret("set", "secret-fsync-fixture-key", 0, "session")
                self.assertEqual(caught.exception.code, "storage_write_uncertain" if rollback else "secret_write_failed")
                self.assertEqual(store.saved, before["saved"])
                self.assertEqual(store.revision, 0)
                self.assertFalse(store.path.exists())
                restored = DecisionConfigStore(root, "restart")
                self.assertEqual(restored.saved, store.saved)
                self.assertEqual(restored.storage_uncertain, bool(rollback))
                if rollback:
                    with self.assertRaises(ManagementError):
                        restored.backend_config("jev")

    def test_posix_secret_permissions_and_config_cas_success_consistent_after_restart(self):
        self.store.update_secret("set", "permissions-jev-fixture-key", 0, "session")
        self.store.update_secret("set", "permissions-laya-fixture-key", 1, "session", provider="laya")
        if os.name != "nt":
            self.assertEqual(self.store.secrets.directory.stat().st_mode & 0o777, 0o700)
            for path in (self.store.path, self.store.secrets.credential_path,
                         *self.store.secrets.directory.glob("*.secret")):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        restored = DecisionConfigStore(self.root, "restart")
        self.assertEqual(restored.revision, 2)
        self.assertEqual(restored.saved, self.store.saved)
        self.assertFalse(restored.storage_uncertain)

    def test_secret_reference_collision_cannot_overwrite_an_immutable_key(self):
        self.store.update_secret("set", "immutable-original-fixture-key", 0, "session")
        reference = self.store.saved["secret_ref"]
        before = self.store.get()
        with patch("secrets.token_hex", return_value=reference[4:]):
            with self.assertRaises(ManagementError) as caught:
                self.store.update_secret("set", "immutable-new-fixture-key", 1, "session")
        self.assertEqual(caught.exception.code, "secret_ref_conflict")
        self.assertEqual(self.store.get(), before)
        self.assertEqual(self.store.secrets.read(reference), "immutable-original-fixture-key")

    def test_postcommit_old_key_cleanup_failure_reports_committed_cas_and_restart(self):
        self.store.update_secret("set", "cleanup-old-fixture-key", 0, "session")
        old = self.store.saved["secret_ref"]
        with patch.object(self.store.secrets, "remove", side_effect=OSError("fixture unlink")):
            with self.assertRaises(ManagementError) as caught:
                self.store.update_secret("set", "cleanup-new-fixture-key", 1, "session")
        self.assertEqual(caught.exception.code, "secret_cleanup_failed")
        self.assertEqual(self.store.revision, 2)
        self.assertNotEqual(self.store.saved["secret_ref"], old)
        self.assertEqual(self.store.secrets.read(old), "cleanup-old-fixture-key")
        restored = DecisionConfigStore(self.root, "restart")
        self.assertEqual(restored.saved, self.store.saved)
        self.assertEqual(restored.revision, 2)
        self.assertEqual(restored.secrets.read(restored.saved["secret_ref"]), "cleanup-new-fixture-key")
        with self.assertRaises(ManagementError) as conflict:
            self.store.update_secret("keep", None, 1, "session")
        self.assertEqual(conflict.exception.code, "revision_conflict")

    def test_precommit_new_key_cleanup_failure_is_classified_not_a_hidden_success(self):
        before = self.store.get()
        with patch.object(self.store, "_publish", side_effect=ManagementError("config_write_failed", 500)), \
             patch.object(self.store.secrets, "remove", side_effect=OSError("fixture unlink")):
            with self.assertRaises(ManagementError) as caught:
                self.store.update_secret("set", "cleanup-orphan-fixture-key", 0, "session")
        self.assertEqual(caught.exception.code, "secret_cleanup_failed")
        self.assertEqual(self.store.get(), before)
        restored = DecisionConfigStore(self.root, "restart")
        self.assertEqual(restored.saved, self.store.saved)
        self.assertEqual(restored.revision, 0)
        self.assertEqual(restored.secrets.redact("cleanup-orphan-fixture-key"), "[REDACTED]")

    def test_failed_secret_publish_preserves_both_keys_cas_and_old_config(self):
        self.store.update_secret("set", "old-jev-fixture-key", 0, "session")
        self.store.update_secret("set", "old-laya-fixture-key", 1, "session", provider="laya")
        before, content = self.store.get(), self.store.path.read_bytes()
        with patch.object(self.store, "_publish", side_effect=ManagementError("config_write_failed", 500)):
            with self.assertRaises(ManagementError):
                self.store.update_secret("set", "new-laya-fixture-key", 2, "session", provider="laya")
        self.assertEqual(self.store.get(), before)
        self.assertEqual(self.store.path.read_bytes(), content)
        self.assertEqual(len(list(self.store.secrets.directory.glob("*.secret"))), 2)
        self.assertEqual(self.store.secrets.redact("new-laya-fixture-key"), "[REDACTED]")


class AtomicWriteTests(unittest.TestCase):
    def test_optional_fchmod_and_repeated_save_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config"
            with patch.object(os, "fchmod", None, create=True):
                atomic_bytes(target, b"one")
                atomic_bytes(target, b"two")
            self.assertEqual(target.read_bytes(), b"two")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_cleanup_failure_cannot_mask_rollback_uncertainty_or_remove_its_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DecisionConfigStore(directory, "session")
            store.update_secret("set", "cleanup-rollback-old-key", 0, "session")
            original_unlink = os.unlink
            def unlink(path, *args, **kwargs):
                if (Path(path).parent == store.path.parent and "-rollback-" not in str(path) and
                        Path(path).name.startswith(".decision-")):
                    raise OSError("fixture temp cleanup")
                original_unlink(path, *args, **kwargs)
            with directory_fault(store.path.parent, rollback="replace"), \
                 patch.object(os, "unlink", side_effect=unlink):
                with self.assertRaises(ManagementError) as caught:
                    store.update_secret("set", "cleanup-rollback-new-key", 1, "session")
            self.assertEqual(caught.exception.code, "storage_write_uncertain")
            restored = DecisionConfigStore(directory, "restart")
            self.assertTrue(restored.storage_uncertain)
            self.assertTrue(store.storage_uncertain)
            self.assertEqual({p.read_text() for p in store.secrets.directory.glob("*.secret")},
                             {"cleanup-rollback-old-key", "cleanup-rollback-new-key"})
            self.assertTrue(restored.secrets.read(restored.saved["secret_ref"]))
            with self.assertRaises(ManagementError):
                restored.backend_config("jev")

    def test_temporary_cleanup_failure_after_publish_retains_restart_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "config"
            target.write_bytes(b"old")
            original_unlink = os.unlink
            def unlink(path, *args, **kwargs):
                if "-rollback-" not in str(path) and Path(path).name.startswith(".decision-"):
                    raise OSError("fixture temp cleanup")
                original_unlink(path, *args, **kwargs)
            with patch.object(os, "unlink", side_effect=unlink):
                with self.assertRaises(ManagementError) as caught:
                    atomic_bytes(target, b"new", exclusive=False)
            # Replacement consumes its temp; the injected unlink still fails. No
            # hidden success, even on Windows where directory fsync is unavailable.
            self.assertEqual(caught.exception.code, "storage_write_uncertain")
            self.assertEqual(target.read_bytes(), b"new")
            self.assertTrue(config_module.storage_recovery_pending(target))
            with self.assertRaises(ManagementError):
                atomic_bytes(target, b"must-not-write")

    def test_recovery_marker_cleanup_failure_fences_committed_config_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DecisionConfigStore(directory, "session")
            original_unlink = os.unlink
            def unlink(path, *args, **kwargs):
                if "decision.json-rollback-" in str(path):
                    raise OSError("fixture backup cleanup")
                original_unlink(path, *args, **kwargs)
            with patch.object(os, "unlink", side_effect=unlink):
                with self.assertRaises(ManagementError) as caught:
                    store.save(store.saved, 0, "session")
            self.assertEqual(caught.exception.code, "storage_write_uncertain")
            self.assertTrue(store.storage_uncertain)
            restored = DecisionConfigStore(directory, "restart")
            self.assertTrue(restored.storage_uncertain)
            # First config creation uses an absence marker, not a hardlink.
            self.assertEqual(restored.revision, 1)
            self.assertEqual(restored.saved, store.saved)
            self.assertEqual(restored.revision, store.revision)
            with self.assertRaises(ManagementError):
                restored.backend_config("laya")

    def test_all_precommit_failures_close_descriptor_and_preserve_old_file(self):
        for stage in ("fchmod", "fdopen", "write", "flush", "fsync", "replace"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "config"
                target.write_bytes(b"old")
                captured = []
                original_mkstemp, original_fdopen = tempfile.mkstemp, os.fdopen
                def mkstemp(*args, **kwargs):
                    fd, name = original_mkstemp(*args, **kwargs)
                    captured.append(fd)
                    return fd, name
                class FailingStream:
                    def __init__(self, fd, mode):
                        self.stream = original_fdopen(fd, mode)
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        self.stream.close()
                    def write(self, data):
                        if stage == "write":
                            raise OSError("fixture write failure")
                        return self.stream.write(data)
                    def flush(self):
                        if stage == "flush":
                            raise OSError("fixture flush failure")
                        return self.stream.flush()
                    def fileno(self):
                        return self.stream.fileno()
                name = "os." + stage if stage in ("fchmod", "fdopen", "fsync", "replace") else "os.fdopen"
                kwargs = {"side_effect": OSError("fixture " + stage)} if stage not in ("write", "flush") else {"side_effect": FailingStream}
                with patch("tempfile.mkstemp", side_effect=mkstemp), patch(name, create=True, **kwargs):
                    with self.assertRaises(OSError):
                        atomic_bytes(target, b"new")
                with self.assertRaises(OSError):
                    os.fstat(captured[0])
                self.assertEqual(target.read_bytes(), b"old")
                self.assertEqual(list(target.parent.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
