from __future__ import annotations

import importlib.util
import json
import math
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
from astrbot_ex.core.topic_bus import TopicBus, TopicInbox


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

PLUGIN_CATEGORIES = ("vision", "perception", "control", "decision", "special")

DEFAULT_CATEGORY_BY_CAPABILITY = {
    "vision_provider": "vision",
    "motion_bridge": "control",
    "transport": "control",
    "protocol_codec": "control",
    "telemetry_provider": "perception",
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
class TopicDeclaration:
    topic: str
    label: str = ""
    schema: str = ""


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
    dashboard: str | None = None
    publishes: list[TopicDeclaration] = field(default_factory=list)
    subscribes: list[TopicDeclaration] = field(default_factory=list)


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
        topic_bus: TopicBus,
    ) -> None:
        self.plugin_id = plugin_id
        self.plugin_root = plugin_root
        self.config = config
        self.event_bus = event_bus
        self.topic_bus = topic_bus

    def subscribe(self, topic: str, *, max_messages: int = 1) -> TopicInbox:
        return self.topic_bus.subscribe_inbox(topic, max_messages=max_messages)


class LocalPluginManager:
    def __init__(
        self,
        *,
        plugins_root: Path,
        state_path: Path,
        registry: PluginRegistry,
        event_bus: EventBus,
        topic_bus: TopicBus,
    ) -> None:
        self.plugins_root = plugins_root
        self.state_path = state_path
        self.registry = registry
        self.event_bus = event_bus
        self.topic_bus = topic_bus
        self.records: dict[str, LocalPluginRecord] = {}
        self.plugins_root.mkdir(parents=True, exist_ok=True)
        for category in PLUGIN_CATEGORIES:
            (self.plugins_root / category).mkdir(parents=True, exist_ok=True)

    def discover(self) -> None:
        self.records.clear()
        state = self._load_state()
        for child in sorted(self.plugins_root.iterdir()):
            if not child.is_dir():
                continue
            if child.name in PLUGIN_CATEGORIES:
                for nested in sorted(child.iterdir()):
                    if not nested.is_dir():
                        continue
                    self._discover_plugin_dir(nested, child.name, state)
                continue
            self._discover_plugin_dir(child, None, state)

    def load_enabled(self) -> None:
        for record in list(self.records.values()):
            if record.enabled:
                self._load_record(record)

    def list_plugins(self) -> list[dict[str, Any]]:
        return [self._serialize(record) for record in self.records.values()]

    def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        return self._serialize(self._record(plugin_id), include_schema=True)

    def update_config(self, plugin_id: str, config: dict[str, Any]) -> dict[str, Any]:
        record = self._record(plugin_id)
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        merged_config = self._load_plugin_config(record)
        merged_config.update(config)
        self._validate_config(record, merged_config)
        self._write_plugin_config(record, merged_config)
        if record.loaded:
            self.registry.unregister(record.manifest.id)
            record.loaded = False
            record.plugin = None
            record.module_name = None
            record.status = "installed"
        if record.enabled:
            self._load_record(record)
        self.event_bus.emit("plugin", "plugin config updated", plugin=plugin_id)
        return self._serialize(record, include_schema=True)

    def update_pubsub(self, plugin_id: str, pubsub: dict[str, Any]) -> dict[str, Any]:
        record = self._record(plugin_id)
        if not isinstance(pubsub, dict):
            raise ValueError("pubsub must be an object")
        config = self._load_plugin_config(record)
        config["pubsub"] = self._normalize_pubsub(record, pubsub)
        return self.update_config(plugin_id, config)

    def list_publishers(self) -> list[dict[str, Any]]:
        return [self._publisher_payload(record) for record in self.records.values() if record.manifest.publishes]

    def uninstall(self, plugin_id: str) -> None:
        record = self._record(plugin_id)
        if record.loaded:
            self.registry.unregister(record.manifest.id)
        self.records.pop(plugin_id, None)
        shutil.rmtree(record.root)
        self._save_enabled_state()
        self.event_bus.emit("plugin", "plugin uninstalled", plugin=plugin_id)
        self.discover()

    def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        record = self._record(plugin_id)
        if enabled:
            record.enabled = True
            self._load_record(record)
            if record.error:
                record.enabled = False
                self._save_enabled_state()
                raise ValueError(f"failed to enable plugin: {record.error}")
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

    def _validate_config(self, record: LocalPluginRecord, config: dict[str, Any]) -> None:
        schema = record.config_schema or {}

        def validate_value(path: str, value: Any, value_schema: dict[str, Any]) -> None:
            expected = value_schema.get("type")
            if expected == "object":
                if not isinstance(value, dict):
                    raise ValueError(f"{path} must be an object")
                for key in value_schema.get("required", []):
                    if key not in value:
                        raise ValueError(f"{path}.{key} is required")
                for key, child_schema in value_schema.get("properties", {}).items():
                    if key in value:
                        validate_value(f"{path}.{key}", value[key], child_schema)
            elif expected == "array":
                if not isinstance(value, list):
                    raise ValueError(f"{path} must be an array")
                item_schema = value_schema.get("items", {})
                for index, item in enumerate(value):
                    validate_value(f"{path}[{index}]", item, item_schema)
            elif expected == "boolean":
                if not isinstance(value, bool):
                    raise ValueError(f"{path} must be a boolean")
            elif expected == "integer":
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError(f"{path} must be an integer")
            elif expected == "number":
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                    raise ValueError(f"{path} must be a finite number")
            elif expected == "string" and not isinstance(value, str):
                raise ValueError(f"{path} must be a string")

            if "enum" in value_schema and value not in value_schema["enum"]:
                raise ValueError(f"{path} must be one of {value_schema['enum']}")
            if expected in {"integer", "number"}:
                if "minimum" in value_schema and value < value_schema["minimum"]:
                    raise ValueError(f"{path} must be >= {value_schema['minimum']}")
                if "maximum" in value_schema and value > value_schema["maximum"]:
                    raise ValueError(f"{path} must be <= {value_schema['maximum']}")

        validate_value("config", config, schema)

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
            topic_bus=self.topic_bus,
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
        slot = self.registry.get(manifest.id)
        actor_error = slot.actor.last_error if slot is not None else None
        payload = {
            "id": manifest.id,
            "name": manifest.name,
            "category": record.category,
            "version": manifest.version,
            "description": manifest.description,
            "author": manifest.author,
            "provides": manifest.provides,
            "requires": manifest.requires,
            "publishes": [self._topic_dict(item) for item in manifest.publishes],
            "subscribes": [self._topic_dict(item) for item in manifest.subscribes],
            "pubsub": self._pubsub_payload(record),
            "enabled": record.enabled,
            "loaded": record.loaded,
            "status": "fault" if actor_error else record.status,
            "error": actor_error or record.error,
            "thread": {
                "name": slot.actor.thread_name,
                "alive": slot.actor.alive,
                "last_error": slot.actor.last_error,
            }
            if slot is not None
            else None,
            "cover_url": f"/api/plugins/{manifest.id}/cover" if manifest.cover else None,
            "dashboard_url": f"/api/plugins/{manifest.id}/dashboard" if manifest.dashboard else None,
            "path": str(record.root),
        }
        if include_schema:
            payload["config_schema"] = record.config_schema
            payload["config"] = self._load_plugin_config(record)
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
        if manifest.dashboard and not (root / manifest.dashboard).is_file():
            raise ValueError(f"dashboard not found: {manifest.dashboard}")
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
            dashboard=str(data["dashboard"]).strip() if data.get("dashboard") else None,
            publishes=self._parse_topics(data.get("publishes", [])),
            subscribes=self._parse_topics(data.get("subscribes", [])),
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
        self._validate_topics(manifest.id, manifest.publishes)
        self._validate_topics(manifest.id, manifest.subscribes, require_prefix=False)

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

    def _write_plugin_config(self, record: LocalPluginRecord, config: dict[str, Any]) -> None:
        config_path = record.root / "config.json"
        config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")

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

    def _discover_plugin_dir(self, root: Path, category_hint: str | None, state: dict[str, bool]) -> None:
        try:
            manifest = self._load_manifest(root)
            category = category_hint or self._category_for_manifest(manifest)
            enabled = bool(state.get(manifest.id, manifest.enabled_default))
            self.records[manifest.id] = LocalPluginRecord(
                manifest=manifest,
                root=root,
                category=category,
                enabled=enabled,
                config_schema=self._load_config_schema(root, manifest),
            )
        except Exception as exc:
            fallback_id = root.name
            self.records[fallback_id] = LocalPluginRecord(
                manifest=PluginManifest(
                    id=fallback_id,
                    name=fallback_id,
                    version="0.0.0",
                    entry="main.py",
                    provides=[],
                ),
                root=root,
                category=category_hint or "special",
                enabled=False,
                status="fault",
                error=str(exc),
            )

    def _manifest_dict(self, manifest: PluginManifest) -> dict[str, Any]:
        return {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "author": manifest.author,
            "provides": manifest.provides,
            "requires": manifest.requires,
            "publishes": [self._topic_dict(item) for item in manifest.publishes],
            "subscribes": [self._topic_dict(item) for item in manifest.subscribes],
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

    def _parse_topics(self, raw_items: Any) -> list[TopicDeclaration]:
        items: list[TopicDeclaration] = []
        if not isinstance(raw_items, list):
            return items
        for raw in raw_items:
            if isinstance(raw, str):
                topic = raw.strip()
                if topic:
                    items.append(TopicDeclaration(topic=topic, label=topic, schema=""))
                continue
            if not isinstance(raw, dict):
                continue
            topic = str(raw.get("topic", "")).strip()
            if not topic:
                continue
            label = str(raw.get("label", "")).strip() or topic
            schema = str(raw.get("schema", "")).strip()
            items.append(TopicDeclaration(topic=topic, label=label, schema=schema))
        return items

    def _validate_topics(self, plugin_id: str, items: list[TopicDeclaration], *, require_prefix: bool = True) -> None:
        seen: set[str] = set()
        for item in items:
            if not item.topic:
                raise ValueError("topic must not be empty")
            if item.topic in seen:
                raise ValueError(f"duplicate topic declaration: {item.topic}")
            seen.add(item.topic)
            if require_prefix and not item.topic.startswith(f"{plugin_id}."):
                raise ValueError(f"publish topic must start with '{plugin_id}.': {item.topic}")

    def _topic_dict(self, item: TopicDeclaration) -> dict[str, str]:
        return {
            "topic": item.topic,
            "label": item.label or item.topic,
            "schema": item.schema,
        }

    def _pubsub_payload(self, record: LocalPluginRecord) -> dict[str, Any]:
        config = self._load_plugin_config(record)
        raw = config.get("pubsub", {})
        if not isinstance(raw, dict):
            raw = {}
        normalized = self._normalize_pubsub(record, raw, strict=False)
        return normalized

    def _normalize_pubsub(
        self,
        record: LocalPluginRecord,
        raw: dict[str, Any],
        *,
        strict: bool = True,
    ) -> dict[str, Any]:
        publishes = {item.topic for item in record.manifest.publishes}
        publish_enabled = bool(raw.get("publish_enabled", False))
        enabled_topics: list[str] = []
        for item in raw.get("enabled_topics", []):
            topic = str(item).strip()
            if not topic:
                continue
            if topic not in publishes:
                if strict:
                    raise ValueError(f"unknown publish topic for {record.manifest.id}: {topic}")
                continue
            enabled_topics.append(topic)

        subscriptions: list[dict[str, str]] = []
        for item in raw.get("subscriptions", []):
            if not isinstance(item, dict):
                continue
            plugin_id = str(item.get("plugin_id", "")).strip()
            topic = str(item.get("topic", "")).strip()
            if not plugin_id or not topic:
                continue
            subscriptions.append({"plugin_id": plugin_id, "topic": topic})

        return {
            "publish_enabled": publish_enabled,
            "enabled_topics": enabled_topics,
            "subscriptions": subscriptions,
        }

    def _publisher_payload(self, record: LocalPluginRecord) -> dict[str, Any]:
        pubsub = self._pubsub_payload(record)
        return {
            "plugin_id": record.manifest.id,
            "name": record.manifest.name,
            "category": record.category,
            "enabled": record.enabled,
            "publish_enabled": pubsub.get("publish_enabled", False),
            "topics": [
                {
                    **self._topic_dict(item),
                    "enabled": item.topic in set(pubsub.get("enabled_topics", [])),
                }
                for item in record.manifest.publishes
            ],
        }
