from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path, PurePosixPath
from typing import Any


SNAPSHOT_FORMAT = "astrbotex-instance-snapshot"
SNAPSHOT_FORMAT_VERSION = 1
SNAPSHOT_ROOTS = ("profiles", "plugins")
_EXCLUDED_PARTS = {".git", ".agents", "__pycache__", "backups"}
_EXCLUDED_SUFFIXES = {".log", ".pyc", ".pyo", ".tmp"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COPY_CHUNK_SIZE = 1024 * 1024


class SnapshotError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotLimits:
    max_upload_bytes: int = 256 * 1024 * 1024
    max_files: int = 10_000
    max_file_bytes: int = 128 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    max_compression_ratio: float = 200.0
    max_manifest_bytes: int = 2 * 1024 * 1024


@dataclass(slots=True)
class _PreparedSnapshot:
    work_dir: Path
    data_dir: Path
    manifest: dict[str, Any]


def current_astrbotex_version() -> str:
    try:
        return package_version("astrbotex")
    except PackageNotFoundError:
        return "0.1.0"


def _version_family(value: str) -> tuple[int, int]:
    match = re.match(r"^v?(\d+)\.(\d+)(?:\.|$)", value.strip())
    if not match:
        raise SnapshotError(f"invalid AstrBotEX version: {value!r}")
    return int(match.group(1)), int(match.group(2))


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


class SnapshotExporter:
    def __init__(self, data_root: Path, *, application_version: str) -> None:
        self.data_root = data_root.resolve()
        self.application_version = application_version

    def export(self, output_dir: Path) -> tuple[Path, dict[str, Any]]:
        output_dir.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc)
        filename = (
            f"astrbotex_snapshot_{created_at.strftime('%Y%m%d_%H%M%S')}_"
            f"{uuid.uuid4().hex[:8]}.zip"
        )
        destination = output_dir / filename
        handle = tempfile.NamedTemporaryFile(
            dir=output_dir,
            prefix=".snapshot-",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        handle.close()

        files: list[dict[str, Any]] = []
        total_bytes = 0
        try:
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for source, archive_path in self._iter_files():
                    digest, size = self._write_file(archive, source, archive_path)
                    files.append(
                        {
                            "path": archive_path,
                            "size": size,
                            "sha256": digest,
                        }
                    )
                    total_bytes += size

                manifest = {
                    "format": SNAPSHOT_FORMAT,
                    "format_version": SNAPSHOT_FORMAT_VERSION,
                    "astrbotex_version": self.application_version,
                    "created_at": created_at.isoformat(),
                    "roots": list(SNAPSHOT_ROOTS),
                    "files": files,
                    "statistics": {
                        "file_count": len(files),
                        "total_bytes": total_bytes,
                    },
                }
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                )
            temp_path.replace(destination)
            return destination, manifest
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def _iter_files(self) -> Iterator[tuple[Path, str]]:
        for root_name in SNAPSHOT_ROOTS:
            root = self.data_root / root_name
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                raise SnapshotError(f"snapshot root must be a directory: {root_name}")
            for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
                relative = path.relative_to(root)
                if any(part in _EXCLUDED_PARTS for part in relative.parts):
                    continue
                if path.is_symlink():
                    continue
                if not path.is_file() or path.suffix.lower() in _EXCLUDED_SUFFIXES:
                    continue
                archive_path = (PurePosixPath("data") / root_name / PurePosixPath(*relative.parts)).as_posix()
                yield path, archive_path

    @staticmethod
    def _write_file(
        archive: zipfile.ZipFile,
        source: Path,
        archive_path: str,
    ) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as src, archive.open(archive_path, "w", force_zip64=True) as dst:
            while chunk := src.read(_COPY_CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
                dst.write(chunk)
        return digest.hexdigest(), size


class SnapshotImporter:
    def __init__(
        self,
        data_root: Path,
        *,
        application_version: str,
        limits: SnapshotLimits,
    ) -> None:
        self.data_root = data_root.resolve()
        self.application_version = application_version
        self.limits = limits
        self.backup_dir = self.data_root / "backups"

    def restore(
        self,
        zip_path: Path,
        *,
        before_replace: Callable[[], None] | None = None,
        after_replace: Callable[[], None] | None = None,
        after_rollback: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        prepared = self._prepare(zip_path)
        try:
            self._commit(
                prepared,
                before_replace=before_replace,
                after_replace=after_replace,
                after_rollback=after_rollback,
            )
            statistics = prepared.manifest["statistics"]
            return {
                "format": prepared.manifest["format"],
                "format_version": prepared.manifest["format_version"],
                "astrbotex_version": prepared.manifest["astrbotex_version"],
                "created_at": prepared.manifest["created_at"],
                "restored_roots": list(SNAPSHOT_ROOTS),
                "restored_files": statistics["file_count"],
                "restored_bytes": statistics["total_bytes"],
            }
        finally:
            _remove_path(prepared.work_dir)

    def _prepare(self, zip_path: Path) -> _PreparedSnapshot:
        if not zip_path.is_file():
            raise SnapshotError("snapshot ZIP does not exist")
        if zip_path.stat().st_size > self.limits.max_upload_bytes:
            raise SnapshotError("snapshot ZIP exceeds the 256 MiB upload limit")

        work_dir = self.backup_dir / f".staging-{uuid.uuid4().hex}"
        data_dir = work_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=False)
        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                infos = self._validate_archive_entries(archive)
                manifest = self._read_manifest(archive, infos)
                expected = self._validate_manifest(manifest, infos)
                for root_name in SNAPSHOT_ROOTS:
                    (data_dir / root_name).mkdir(parents=True, exist_ok=True)
                self._extract_verified(archive, expected, data_dir)
                self._validate_json_files(data_dir)
            return _PreparedSnapshot(work_dir=work_dir, data_dir=data_dir, manifest=manifest)
        except zipfile.BadZipFile as exc:
            _remove_path(work_dir)
            raise SnapshotError("invalid snapshot ZIP") from exc
        except Exception:
            _remove_path(work_dir)
            raise

    def _validate_archive_entries(
        self,
        archive: zipfile.ZipFile,
    ) -> dict[str, zipfile.ZipInfo]:
        infos: dict[str, zipfile.ZipInfo] = {}
        file_count = 0
        total_bytes = 0
        for info in archive.infolist():
            name = self._validate_archive_name(info.filename, is_dir=info.is_dir())
            if name in infos:
                raise SnapshotError(f"duplicate ZIP entry: {name}")
            infos[name] = info
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise SnapshotError(f"ZIP symbolic links are not allowed: {name}")
            if info.flag_bits & 0x1:
                raise SnapshotError(f"encrypted ZIP entries are not supported: {name}")
            if info.is_dir():
                if not self._is_snapshot_directory_path(name):
                    raise SnapshotError(f"ZIP directory is outside the snapshot roots: {name}")
                continue
            if name != "manifest.json" and not self._is_snapshot_data_path(name):
                raise SnapshotError(f"ZIP entry is outside the snapshot roots: {name}")
            if info.file_size > self.limits.max_file_bytes and name != "manifest.json":
                raise SnapshotError(f"snapshot file exceeds the per-file limit: {name}")
            if name == "manifest.json" and info.file_size > self.limits.max_manifest_bytes:
                raise SnapshotError("manifest.json is too large")
            if info.file_size:
                ratio = info.file_size / max(info.compress_size, 1)
                if ratio > self.limits.max_compression_ratio:
                    raise SnapshotError(f"ZIP compression ratio is unsafe: {name}")
            if name != "manifest.json":
                file_count += 1
                total_bytes += info.file_size
                if file_count > self.limits.max_files:
                    raise SnapshotError("snapshot contains too many files")
                if total_bytes > self.limits.max_total_bytes:
                    raise SnapshotError("snapshot exceeds the total extracted-size limit")
        if "manifest.json" not in infos or infos["manifest.json"].is_dir():
            raise SnapshotError("snapshot is missing manifest.json")
        return infos

    @staticmethod
    def _validate_archive_name(name: str, *, is_dir: bool) -> str:
        if not name or "\x00" in name or "\\" in name or name.startswith("/"):
            raise SnapshotError(f"unsafe ZIP path: {name!r}")
        normalized = name[:-1] if is_dir and name.endswith("/") else name
        parts = normalized.split("/")
        if not normalized or any(part in {"", ".", ".."} for part in parts):
            raise SnapshotError(f"unsafe ZIP path: {name!r}")
        if ":" in parts[0] or PurePosixPath(normalized).is_absolute():
            raise SnapshotError(f"unsafe ZIP path: {name!r}")
        return normalized

    @staticmethod
    def _is_snapshot_data_path(path: str) -> bool:
        parts = PurePosixPath(path).parts
        return len(parts) >= 3 and parts[0] == "data" and parts[1] in SNAPSHOT_ROOTS

    @staticmethod
    def _is_snapshot_directory_path(path: str) -> bool:
        parts = PurePosixPath(path).parts
        if parts == ("data",):
            return True
        return len(parts) >= 2 and parts[0] == "data" and parts[1] in SNAPSHOT_ROOTS

    @staticmethod
    def _read_manifest(
        archive: zipfile.ZipFile,
        infos: dict[str, zipfile.ZipInfo],
    ) -> dict[str, Any]:
        try:
            value = json.loads(archive.read(infos["manifest.json"]).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotError("manifest.json is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise SnapshotError("manifest.json must contain an object")
        return value

    def _validate_manifest(
        self,
        manifest: dict[str, Any],
        infos: dict[str, zipfile.ZipInfo],
    ) -> dict[str, dict[str, Any]]:
        if manifest.get("format") != SNAPSHOT_FORMAT:
            raise SnapshotError("ZIP is not an AstrBotEX instance snapshot")
        if manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION:
            raise SnapshotError("snapshot format version is not supported")
        snapshot_version = manifest.get("astrbotex_version")
        if not isinstance(snapshot_version, str):
            raise SnapshotError("snapshot is missing astrbotex_version")
        if _version_family(snapshot_version) != _version_family(self.application_version):
            raise SnapshotError(
                "snapshot AstrBotEX version is incompatible: "
                f"{snapshot_version} != {self.application_version}"
            )
        if set(manifest.get("roots", [])) != set(SNAPSHOT_ROOTS):
            raise SnapshotError("snapshot roots do not match the supported instance roots")
        if not isinstance(manifest.get("created_at"), str):
            raise SnapshotError("snapshot is missing created_at")

        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise SnapshotError("snapshot manifest files must be a list")
        expected: dict[str, dict[str, Any]] = {}
        total_bytes = 0
        for entry in entries:
            if not isinstance(entry, dict):
                raise SnapshotError("snapshot manifest contains an invalid file entry")
            path = entry.get("path")
            size = entry.get("size")
            digest = entry.get("sha256")
            if not isinstance(path, str) or not self._is_snapshot_data_path(path):
                raise SnapshotError(f"snapshot manifest contains an unsafe path: {path!r}")
            self._validate_archive_name(path, is_dir=False)
            if path in expected:
                raise SnapshotError(f"snapshot manifest contains a duplicate path: {path}")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise SnapshotError(f"snapshot manifest contains an invalid size: {path}")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise SnapshotError(f"snapshot manifest contains an invalid SHA-256: {path}")
            info = infos.get(path)
            if info is None or info.is_dir():
                raise SnapshotError(f"snapshot file is missing from ZIP: {path}")
            if info.file_size != size:
                raise SnapshotError(f"snapshot file size does not match manifest: {path}")
            expected[path] = entry
            total_bytes += size

        actual_paths = {
            name
            for name, info in infos.items()
            if name != "manifest.json" and not info.is_dir()
        }
        if actual_paths != set(expected):
            raise SnapshotError("ZIP file set does not match manifest.json")
        statistics = manifest.get("statistics")
        if not isinstance(statistics, dict):
            raise SnapshotError("snapshot manifest is missing statistics")
        if statistics.get("file_count") != len(expected) or statistics.get("total_bytes") != total_bytes:
            raise SnapshotError("snapshot statistics do not match the file manifest")
        return expected

    def _extract_verified(
        self,
        archive: zipfile.ZipFile,
        expected: dict[str, dict[str, Any]],
        data_dir: Path,
    ) -> None:
        base = data_dir.resolve()
        for archive_path, metadata in expected.items():
            relative = PurePosixPath(archive_path).parts[1:]
            target = data_dir.joinpath(*relative)
            resolved = target.resolve(strict=False)
            if not resolved.is_relative_to(base):
                raise SnapshotError(f"snapshot path escapes staging directory: {archive_path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with archive.open(archive_path, "r") as src, target.open("xb") as dst:
                while chunk := src.read(_COPY_CHUNK_SIZE):
                    size += len(chunk)
                    if size > metadata["size"] or size > self.limits.max_file_bytes:
                        raise SnapshotError(f"snapshot file expanded beyond its declared size: {archive_path}")
                    digest.update(chunk)
                    dst.write(chunk)
            if size != metadata["size"] or digest.hexdigest() != metadata["sha256"]:
                raise SnapshotError(f"snapshot SHA-256 verification failed: {archive_path}")

    @staticmethod
    def _validate_json_files(data_dir: Path) -> None:
        for path in data_dir.rglob("*.json"):
            relative = path.relative_to(data_dir)
            if relative.parts[0] != "profiles" and path.name not in {
                "plugin.json",
                "config.json",
                "config.schema.json",
            }:
                continue
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SnapshotError(f"snapshot contains invalid JSON: {relative.as_posix()}") from exc

    def _commit(
        self,
        prepared: _PreparedSnapshot,
        *,
        before_replace: Callable[[], None] | None,
        after_replace: Callable[[], None] | None,
        after_rollback: Callable[[], None] | None,
    ) -> None:
        rollback_dir = self.backup_dir / f".rollback-{uuid.uuid4().hex}"
        rollback_dir.mkdir(parents=True, exist_ok=False)
        old_roots: set[str] = set()
        installed_roots: set[str] = set()
        lifecycle_started = False
        try:
            if before_replace is not None:
                lifecycle_started = True
                before_replace()
            for root_name in SNAPSHOT_ROOTS:
                target = self.data_root / root_name
                previous = rollback_dir / root_name
                staged = prepared.data_dir / root_name
                if target.exists() or target.is_symlink():
                    target.replace(previous)
                    old_roots.add(root_name)
                staged.replace(target)
                installed_roots.add(root_name)
            if after_replace is not None:
                after_replace()
        except Exception as exc:
            rollback_errors: list[str] = []
            for root_name in reversed(SNAPSHOT_ROOTS):
                target = self.data_root / root_name
                previous = rollback_dir / root_name
                try:
                    if root_name in installed_roots:
                        _remove_path(target)
                    if root_name in old_roots and previous.exists():
                        previous.replace(target)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{root_name}: {rollback_exc}")
            if lifecycle_started and after_rollback is not None:
                try:
                    after_rollback()
                except Exception as rollback_exc:
                    rollback_errors.append(f"runtime reload: {rollback_exc}")
            detail = f"; rollback errors: {', '.join(rollback_errors)}" if rollback_errors else ""
            raise SnapshotError(f"snapshot restore failed and was rolled back: {exc}{detail}") from exc
        finally:
            _remove_path(rollback_dir)


class SnapshotService:
    def __init__(
        self,
        data_root: Path,
        *,
        application_version: str | None = None,
        limits: SnapshotLimits | None = None,
        before_restore: Callable[[], None] | None = None,
        after_restore: Callable[[], None] | None = None,
        after_rollback: Callable[[], None] | None = None,
    ) -> None:
        self.data_root = data_root.resolve()
        self.application_version = application_version or current_astrbotex_version()
        self.limits = limits or SnapshotLimits()
        self.backup_dir = self.data_root / "backups"
        self.before_restore = before_restore
        self.after_restore = after_restore
        self.after_rollback = after_rollback
        self._lock = threading.RLock()

    def create(self) -> dict[str, Any]:
        with self._lock:
            path, manifest = SnapshotExporter(
                self.data_root,
                application_version=self.application_version,
            ).export(self.backup_dir)
            return {
                "filename": path.name,
                "size": path.stat().st_size,
                "created_at": manifest["created_at"],
                "file_count": manifest["statistics"]["file_count"],
                "total_bytes": manifest["statistics"]["total_bytes"],
                "download_url": f"/api/v1/ex/backups/{path.name}",
            }

    def download_path(self, filename: str) -> Path:
        if not filename or filename != Path(filename).name or not filename.lower().endswith(".zip"):
            raise SnapshotError("invalid snapshot filename")
        if any(value in filename for value in ("/", "\\", "..", "\x00")):
            raise SnapshotError("invalid snapshot filename")
        path = self.backup_dir / filename
        if not path.is_file():
            raise SnapshotError("snapshot file not found")
        return path

    def restore_upload(self, filename: str, content: bytes) -> dict[str, Any]:
        if not filename.lower().endswith(".zip"):
            raise SnapshotError("only .zip snapshots are supported")
        if not content:
            raise SnapshotError("uploaded snapshot is empty")
        if len(content) > self.limits.max_upload_bytes:
            raise SnapshotError("snapshot ZIP exceeds the 256 MiB upload limit")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        upload_path = self.backup_dir / f".upload-{uuid.uuid4().hex}.zip"
        with self._lock:
            try:
                upload_path.write_bytes(content)
                result = SnapshotImporter(
                    self.data_root,
                    application_version=self.application_version,
                    limits=self.limits,
                ).restore(
                    upload_path,
                    before_replace=self.before_restore,
                    after_replace=self.after_restore,
                    after_rollback=self.after_rollback,
                )
                result["uploaded_filename"] = Path(filename).name
                return result
            finally:
                upload_path.unlink(missing_ok=True)
