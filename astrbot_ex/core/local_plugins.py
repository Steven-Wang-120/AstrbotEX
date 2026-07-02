from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.plugin_registry import PluginRegistry


ALLOWED_PLUGIN_TYPES = {
    "motion_bridge",
    "vision_provider",
    "transport",
    "protocol_codec",
    "telemetry_provider",
    "rule_plugin",
    "policy_plugin",
    "skill_plugin",
    "tool_plugin",
    "trace_plugin",
}

PLUGIN_CATEGORIES = ("vision", "control", "decision", "special")

DEFAULT_CATEGORY_BY_CAPABILITY = {
    "vision_provider": "vision",
    "motion_bridge": "control",
    "transport": "control",
    "protocol_codec": "control",
    "telemetry_provider": "control",
    "rule_plugin": "decision",
    "policy_plugin": "decision",
    "skill_plugin": "decision",
    "tool_plugin": "decision",
    "trace_plugin": "special",
}

RUNTIME_KIND_BY_CAPABILITY = {
    "motion_bridge": "motion",
    "vision_provider": "vision",
    "rule_plugin": "rule",
    "policy_plugin": "policy",
    "skill_plugin": "skill",
}


@dataclass(slots=True)
class PluginManifest:
    id: str
    name: str
    version: str
    entry: str
    provides: list[str]
    description: str = ""
    author: str = ""
    requires: list[str] = field(default_factory=list)
    config_schema: str | None = None
    enabled_default: bool = False
    cover: str | None = None


@dataclass(slots=True)
class LocalPluginRecord:
    manifest: PluginManifest
    root: Path
    category: str
    enabled: bool
    loaded: bool = False
    status: str = "installed"
    error: str | None = None
    module_name: str | None = None
    plugin: Any = None
    config_schema: dict[str, Any] | None = None


class PluginContext:
    def __init__(
        self,
        *,
        plugin_id: str,
        plugin_root: Path,
        config: dict[str, Any],
        event_bus: EventBus,
    ) -> None:
        self.plugin_id = plugin_id
        self.plugin_root = plugin_root
        self.config = config
        self.event_bus = event_bus


class LocalPluginManager:
    def __init__(
        self,
        *,
        plugins_root: Path,
        state_path: Path,
        registry: PluginRegistry,
        event_bus: EventBus,
    ) -> None:
        self.plugins_root = plugins_root
        self.state_path = state_path
        self.registry = registry
        self.event_bus = event_bus
        self.records: dict[str, LocalPluginRecord] = {}
        self.plugins_root.mkdir(parents=True, exist_ok=True)
        for category in PLUGIN_CATEGORIES:
            (self.plugins_root / category).mkdir(parents=True, exist_ok=True)

    def discover(self) -> None:
        self.records.clear()
        state = self._load_state()
        for category_root in self._iter_category_roots():
            category = self._category_name_for_root(category_root)
            for child in sorted(category_root.iterdir()):
                if not child.is_dir():
                    continue
                try:
                    manifest = self._load_manifest(child)
                    enabled = bool(state.get(manifest.id, manifest.enabled_default))
                    self.records[manifest.id] = LocalPluginRecord(
                        manifest=manifest,
                        root=child,
                        category=category,
                        enabled=enabled,
                        config_schema=self._load_config_schema(child, manifest),
                    )
                except Exception as exc:
                    fallback_id = child.name
                    self.records[fallback_id] = LocalPluginRecord(
                        manifest=PluginManifest(
                            id=fallback_id,
                            name=fallback_id,
                            version="0.0.0",
                            entry="main.py",
                            provides=[],
                        ),
                        root=child,
                        category=category,
                        enabled=False,
                        status="fault",
                        error=str(exc),
                    )

    def load_enabled(self) -> None:
        for record in list(self.records.values()):
            if record.enabled:
                self._load_record(record)

    def list_plugins(self) -> list[dict[str, Any]]:
        return [self._serialize(record) for record in self.records.values()]

    def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        return self._serialize(self._record(plugin_id), include_schema=True)

    def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        record = self._record(plugin_id)
        if enabled:
            record.enabled = True
            self._load_record(record)
        else:
            if record.loaded:
                self.registry.unregister(record.manifest.id)
                record.loaded = False
                record.status = "disabled"
            record.enabled = False
        self._save_enabled_state()
        self.event_bus.emit(
            "plugin",
            "plugin enabled changed",
            plugin=plugin_id,
            enabled=enabled,
        )
        return self._serialize(record, include_schema=True)

    def install_zip(self, zip_path: Path, *, category: str | None = None) -> dict[str, Any]:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            self._validate_zip_members(members)
            manifest_member = self._find_manifest_member(members)
            if manifest_member is None:
                raise ValueError("plugin.json not found")
            manifest = self._manifest_from_bytes(archive.read(manifest_member))
            base_prefix = manifest_member.filename.removesuffix("plugin.json").strip("/")
            self._validate_manifest(manifest)
            entry_name = f"{base_prefix}/{manifest.entry}".strip("/")
            if entry_name not in {item.filename.strip("/") for item in members}:
                raise ValueError(f"entry file not found: {manifest.entry}")

            plugin_category = self._normalize_category(category) or self._category_for_manifest(manifest)
            target_root = self.plugins_root / plugin_category
            target_root.mkdir(parents=True, exist_ok=True)
            target = target_root / manifest.id
            if target.exists():
                raise ValueError(f"plugin already exists: {manifest.id}")
            temp = target_root / f".upload_{manifest.id}_{int(time.time())}"
            temp.mkdir(parents=True, exist_ok=False)
            try:
                for item in members:
                    if item.is_dir():
                        continue
                    relative = item.filename.strip("/")
                    if base_prefix:
                        if not relative.startswith(f"{base_prefix}/"):
                            continue
                        relative = relative.removeprefix(f"{base_prefix}/")
                    dest = temp / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out)
                self._load_manifest(temp)
                temp.rename(target)
            except Exception:
                shutil.rmtree(temp, ignore_errors=True)
                raise

        self.discover()
        record = self._record(manifest.id)
        if record.enabled:
            self._load_record(record)
        self.event_bus.emit("plugin", "plugin installed", plugin=manifest.id)
        return self._serialize(record, include_schema=True)

    def _load_record(self, record: LocalPluginRecord) -> None:
        try:
            if record.loaded:
                if not record.enabled:
                    self.registry.enable(record.manifest.id)
                record.status = "enabled"
                return
            module = self._import_module(record)
            plugin = self._create_plugin(module, record)
            record.plugin = plugin
            record.module_name = module.__name__
            self.registry.register(
                self._runtime_kind(record.manifest),
                plugin,
                enabled=True,
                metadata=self._manifest_dict(record.manifest),
            )
            record.loaded = True
            record.status = "enabled"
            record.error = None
        except Exception as exc:
            record.loaded = False
            record.status = "fault"
            record.error = str(exc)

    def _import_module(self, record: LocalPluginRecord) -> ModuleType:
        entry = (record.root / record.manifest.entry).resolve()
        if record.root.resolve() not in entry.parents and entry != record.root.resolve():
            raise ValueError("entry path escapes plugin root")
        module_name = f"astrbotex_local_plugin_{record.manifest.id}"
        if module_name in sys.modules:
            del sys.modules[module_name]
        spec = importlib.util.spec_from_file_location(module_name, entry)
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot import plugin entry: {record.manifest.entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _create_plugin(self, module: ModuleType, record: LocalPluginRecord) -> Any:
        config = self._load_plugin_config(record)
        context = PluginContext(
            plugin_id=record.manifest.id,
            plugin_root=record.root,
            config=config,
            event_bus=self.event_bus,
        )
        factory = getattr(module, "create_plugin", None)
        if callable(factory):
            try:
                plugin = factory(context)
            except TypeError:
                plugin = factory()
        else:
            plugin_cls = getattr(module, "Plugin", None) or getattr(module, "Main", None)
            if plugin_cls is None:
                raise ValueError("main.py must expose create_plugin(), Plugin, or Main")
            try:
                plugin = plugin_cls(context)
            except TypeError:
                plugin = plugin_cls()
        plugin.id = record.manifest.id
        plugin.name = record.manifest.name
        return plugin

    def _runtime_kind(self, manifest: PluginManifest) -> str:
        for capability in manifest.provides:
            if capability in RUNTIME_KIND_BY_CAPABILITY:
                return RUNTIME_KIND_BY_CAPABILITY[capability]
        return manifest.provides[0]

    def _serialize(self, record: LocalPluginRecord, *, include_schema: bool = False) -> dict[str, Any]:
        manifest = record.manifest
        payload = {
            "id": manifest.id,
            "name": manifest.name,
            "category": record.category,
            "version": manifest.version,
            "description": manifest.description,
            "author": manifest.author,
            "provides": manifest.provides,
            "requires": manifest.requires,
            "enabled": record.enabled,
            "loaded": record.loaded,
            "status": record.status,
            "error": record.error,
            "cover_url": f"/api/plugins/{manifest.id}/cover" if manifest.cover else None,
            "path": str(record.root),
        }
        if include_schema:
            payload["config_schema"] = record.config_schema
        return payload

    def _load_manifest(self, root: Path) -> PluginManifest:
        manifest_path = root / "plugin.json"
        if not manifest_path.is_file():
            raise ValueError("missing plugin.json")
        manifest = self._manifest_from_bytes(manifest_path.read_bytes())
        self._validate_manifest(manifest)
        entry_path = (root / manifest.entry).resolve()
        if not entry_path.is_file():
            raise ValueError(f"entry file not found: {manifest.entry}")
        if manifest.config_schema and not (root / manifest.config_schema).is_file():
            raise ValueError(f"config_schema not found: {manifest.config_schema}")
        if manifest.cover and not (root / manifest.cover).is_file():
            raise ValueError(f"cover not found: {manifest.cover}")
        return manifest

    def _manifest_from_bytes(self, raw: bytes) -> PluginManifest:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("plugin.json must be an object")
        return PluginManifest(
            id=str(data.get("id", "")).strip(),
            name=str(data.get("name", "")).strip(),
            version=str(data.get("version", "")).strip(),
            entry=str(data.get("entry", "main.py")).strip(),
            provides=[str(item) for item in data.get("provides", [])],
            description=str(data.get("description", "")).strip(),
            author=str(data.get("author", "")).strip(),
            requires=[str(item) for item in data.get("requires", [])],
            config_schema=(
                str(data["config_schema"]).strip()
                if data.get("config_schema")
                else None
            ),
            enabled_default=bool(data.get("enabled_default", False)),
            cover=str(data["cover"]).strip() if data.get("cover") else None,
        )

    def _validate_manifest(self, manifest: PluginManifest) -> None:
        if not manifest.id.replace("_", "").replace("-", "").isalnum():
            raise ValueError("plugin id must only contain letters, numbers, '_' or '-'")
        if not manifest.name:
            raise ValueError("plugin name is required")
        if not manifest.version:
            raise ValueError("plugin version is required")
        if not manifest.entry or manifest.entry.startswith("/") or ".." in Path(manifest.entry).parts:
            raise ValueError("invalid entry path")
        if not manifest.provides:
            raise ValueError("provides must not be empty")
        unknown = [item for item in manifest.provides if item not in ALLOWED_PLUGIN_TYPES]
        if unknown:
            raise ValueError(f"unsupported provides: {', '.join(unknown)}")

    def _load_config_schema(self, root: Path, manifest: PluginManifest) -> dict[str, Any] | None:
        if not manifest.config_schema:
            return None
        return json.loads((root / manifest.config_schema).read_text(encoding="utf-8"))

    def _load_plugin_config(self, record: LocalPluginRecord) -> dict[str, Any]:
        config_path = record.root / "config.json"
        if not config_path.is_file():
            return {}
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def _load_state(self) -> dict[str, bool]:
        if not self.state_path.is_file():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        enabled = data.get("enabled_plugins", {})
        return enabled if isinstance(enabled, dict) else {}

    def _save_enabled_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "enabled_plugins": {
                plugin_id: record.enabled for plugin_id, record in self.records.items()
            }
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _record(self, plugin_id: str) -> LocalPluginRecord:
        try:
            return self.records[plugin_id]
        except KeyError as exc:
            raise KeyError(f"unknown plugin: {plugin_id}") from exc

    def _category_for_manifest(self, manifest: PluginManifest) -> str:
        for capability in manifest.provides:
            category = DEFAULT_CATEGORY_BY_CAPABILITY.get(capability)
            if category:
                return category
        return "special"

    def _normalize_category(self, category: str | None) -> str | None:
        if category is None:
            return None
        value = category.strip().lower()
        if value not in PLUGIN_CATEGORIES:
            raise ValueError(f"unsupported plugin category: {category}")
        return value

    def _iter_category_roots(self) -> list[Path]:
        roots = [self.plugins_root / category for category in PLUGIN_CATEGORIES]
        legacy_children = [child for child in self.plugins_root.iterdir() if child.is_dir() and child.name not in PLUGIN_CATEGORIES]
        if legacy_children:
            roots.append(self.plugins_root)
        return roots

    def _category_name_for_root(self, root: Path) -> str:
        return root.name if root != self.plugins_root else "special"

    def _manifest_dict(self, manifest: PluginManifest) -> dict[str, Any]:
        return {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "author": manifest.author,
            "provides": manifest.provides,
            "requires": manifest.requires,
        }

    def _find_manifest_member(self, members: list[zipfile.ZipInfo]) -> zipfile.ZipInfo | None:
        candidates = [item for item in members if item.filename.strip("/").endswith("plugin.json")]
        root_candidates = [item for item in candidates if item.filename.strip("/") == "plugin.json"]
        if root_candidates:
            return root_candidates[0]
        direct = [item for item in candidates if len(Path(item.filename.strip("/")).parts) == 2]
        return direct[0] if len(direct) == 1 else None

    def _validate_zip_members(self, members: list[zipfile.ZipInfo]) -> None:
        for item in members:
            path = Path(item.filename.strip("/"))
            if item.filename.startswith("/") or ".." in path.parts:
                raise ValueError(f"unsafe zip path: {item.filename}")
