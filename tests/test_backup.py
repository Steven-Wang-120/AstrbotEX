from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from astrbot_ex.core.backup import SnapshotError, SnapshotService
from astrbot_ex.core.api_server import build_server


class SnapshotServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp_dir.name)
        self.profile_dir = self.data_root / "profiles" / "default"
        self.plugin_dir = self.data_root / "plugins" / "special" / "demo"
        self.profile_dir.mkdir(parents=True)
        self.plugin_dir.mkdir(parents=True)
        self.profile_path = self.profile_dir / "instance.json"
        self.plugin_config_path = self.plugin_dir / "config.json"
        self.profile_path.write_text('{"name": "original"}\n', encoding="utf-8")
        self.plugin_config_path.write_text('{"enabled": true}\n', encoding="utf-8")
        (self.plugin_dir / "plugin.py").write_text("VALUE = 'original'\n", encoding="utf-8")
        self.service = SnapshotService(self.data_root, application_version="0.1.0")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _snapshot_bytes(self) -> tuple[dict[str, object], bytes]:
        result = self.service.create()
        archive_path = self.service.download_path(str(result["filename"]))
        return result, archive_path.read_bytes()

    def test_export_and_restore_replaces_the_instance_roots(self) -> None:
        result, content = self._snapshot_bytes()

        with zipfile.ZipFile(io.BytesIO(content), "r") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["format"], "astrbotex-instance-snapshot")
            self.assertIn("data/profiles/default/instance.json", archive.namelist())
            self.assertIn("data/plugins/special/demo/plugin.py", archive.namelist())
        self.assertEqual(result["file_count"], 3)

        self.profile_path.write_text('{"name": "changed"}\n', encoding="utf-8")
        (self.profile_dir / "new.json").write_text("{}\n", encoding="utf-8")
        self.plugin_config_path.unlink()

        restored = self.service.restore_upload("instance.zip", content)

        self.assertEqual(restored["restored_files"], 3)
        self.assertEqual(json.loads(self.profile_path.read_text(encoding="utf-8"))["name"], "original")
        self.assertFalse((self.profile_dir / "new.json").exists())
        self.assertTrue(self.plugin_config_path.exists())

    def test_restore_rejects_a_tampered_file_before_replacing_data(self) -> None:
        _, content = self._snapshot_bytes()
        original = io.BytesIO(content)
        tampered = io.BytesIO()
        with zipfile.ZipFile(original, "r") as source, zipfile.ZipFile(tampered, "w") as target:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename == "data/profiles/default/instance.json":
                    data = data.replace(b"original", b"tampered")
                target.writestr(info, data)

        self.profile_path.write_text('{"name": "current"}\n', encoding="utf-8")
        with self.assertRaisesRegex(SnapshotError, "size does not match|SHA-256"):
            self.service.restore_upload("tampered.zip", tampered.getvalue())
        self.assertEqual(json.loads(self.profile_path.read_text(encoding="utf-8"))["name"], "current")

    def test_restore_rejects_path_traversal(self) -> None:
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("../escape.txt", "no")
            archive.writestr("manifest.json", "{}")

        with self.assertRaisesRegex(SnapshotError, "unsafe ZIP path"):
            self.service.restore_upload("unsafe.zip", content.getvalue())
        self.assertFalse((self.data_root.parent / "escape.txt").exists())

    def test_restore_rolls_back_when_runtime_reload_fails(self) -> None:
        _, content = self._snapshot_bytes()
        self.profile_path.write_text('{"name": "current"}\n', encoding="utf-8")
        callbacks: list[str] = []

        def fail_reload() -> None:
            callbacks.append("after_restore")
            raise RuntimeError("reload failed")

        service = SnapshotService(
            self.data_root,
            application_version="0.1.0",
            before_restore=lambda: callbacks.append("before_restore"),
            after_restore=fail_reload,
            after_rollback=lambda: callbacks.append("after_rollback"),
        )
        with self.assertRaisesRegex(SnapshotError, "rolled back"):
            service.restore_upload("instance.zip", content)

        self.assertEqual(callbacks, ["before_restore", "after_restore", "after_rollback"])
        self.assertEqual(json.loads(self.profile_path.read_text(encoding="utf-8"))["name"], "current")

    def test_restore_rejects_invalid_json_before_the_lifecycle_starts(self) -> None:
        self.profile_path.write_text("{not-json", encoding="utf-8")
        _, content = self._snapshot_bytes()
        self.profile_path.write_text('{"name": "current"}\n', encoding="utf-8")
        started: list[bool] = []
        service = SnapshotService(
            self.data_root,
            application_version="0.1.0",
            before_restore=lambda: started.append(True),
        )

        with self.assertRaisesRegex(SnapshotError, "invalid JSON"):
            service.restore_upload("invalid-json.zip", content)
        self.assertEqual(started, [])
        self.assertEqual(json.loads(self.profile_path.read_text(encoding="utf-8"))["name"], "current")


class SnapshotHttpApiTest(unittest.TestCase):
    def test_create_download_and_upload_restore_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                "ASTRBOTEX_DATA_DIR": temp_dir,
                "ASTRBOTEX_STT_ENABLED": "",
                "ASTRBOTEX_TTS_ENABLED": "",
            }
            with mock.patch.dict(os.environ, environment):
                server = build_server("127.0.0.1", 0, 20)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            perception_path = Path(temp_dir) / "profiles" / "default" / "perception.json"
            try:
                create_request = urllib.request.Request(
                    f"{base_url}/api/v1/ex/backups",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(create_request, timeout=5) as response:
                    created = json.loads(response.read())
                self.assertTrue(created["ok"])

                with urllib.request.urlopen(f"{base_url}{created['backup']['download_url']}", timeout=5) as response:
                    archive_bytes = response.read()
                    self.assertEqual(response.headers.get_content_type(), "application/zip")
                with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
                    self.assertIn("manifest.json", archive.namelist())

                original_perception = perception_path.read_bytes()
                perception_path.write_text('{"changed": true}\n', encoding="utf-8")
                server.controller.start()
                self.assertEqual(server.controller.runtime.state.value, "running")
                boundary = "----astrbotex-snapshot-test"
                body = (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="file"; filename="snapshot.zip"\r\n'
                    "Content-Type: application/zip\r\n\r\n"
                ).encode() + archive_bytes + f"\r\n--{boundary}--\r\n".encode()
                upload_request = urllib.request.Request(
                    f"{base_url}/api/v1/ex/backups/upload",
                    data=body,
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    method="POST",
                )
                with urllib.request.urlopen(upload_request, timeout=10) as response:
                    restored = json.loads(response.read())

                self.assertTrue(restored["ok"])
                self.assertEqual(restored["runtime_state"], "idle")
                self.assertEqual(perception_path.read_bytes(), original_perception)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.controller.stop("test shutdown")
                server.connections.close()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
