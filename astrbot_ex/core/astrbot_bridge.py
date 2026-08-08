from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.local_plugins import LocalPluginManager
from astrbot_ex.core.models import RuntimeState
from astrbot_ex.core.serialization import to_jsonable
from astrbot_ex.core.topic_bus import TopicBus


_MAX_SUMMARY_OBSTACLES = 5


@dataclass(slots=True)
class BridgeAction:
    action_id: str
    owner: str
    description: str = ""
    command_topic: str | None = None
    schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    requires_blocks: list[str] = field(default_factory=list)
    requires_runtime_state: list[str] = field(default_factory=list)
    danger: str = "low"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "owner": self.owner,
            "description": self.description,
            "command_topic": self.command_topic,
            "schema": self.schema,
            "requires_blocks": self.requires_blocks,
            "requires_runtime_state": self.requires_runtime_state,
            "danger": self.danger,
        }


class AstrBotBridge:
    """Builds grounded AstrBot context and validates command proposals."""

    def __init__(
        self,
        *,
        controller: Any,
        local_plugins: LocalPluginManager,
        event_bus: EventBus,
        topic_bus: TopicBus,
        context_ttl_sec: float = 15.0,
        block_ttl_ms: int = 1000,
    ) -> None:
        self.controller = controller
        self.local_plugins = local_plugins
        self.event_bus = event_bus
        self.topic_bus = topic_bus
        self.context_ttl_sec = context_ttl_sec
        self.block_ttl_ms = block_ttl_ms
        self._contexts: dict[str, dict[str, Any]] = {}

    def build_context(self) -> dict[str, Any]:
        now = time.time()
        status = self.controller.status()
        actions = [action.to_dict() for action in self.list_actions()]
        blocks = [
            self._runtime_block(status, now),
            self._world_block(status, now),
            self._events_block(status, now),
            self._perception_block(status, now),
            *self._plugin_blocks(now),
        ]
        context_id = self._context_id(now, blocks)
        context = {
            "context_id": context_id,
            "created_at": now,
            "ttl_sec": self.context_ttl_sec,
            "blocks": blocks,
            "affordances": actions,
            "proposal_schema": self.proposal_schema(),
            "rules": [
                "Only submit action_id values listed in affordances.",
                "Do not submit motor speeds, CAN frames, raw wheel commands, or plugin method names.",
                "AstrBotEX may reject stale, unsafe, or unauthorized proposals.",
            ],
        }
        self._contexts[context_id] = context
        self._drop_old_contexts(now)
        return context

    def list_actions(self) -> list[BridgeAction]:
        actions = [
            BridgeAction(
                action_id="runtime.start.v1",
                owner="runtime",
                description="Start AstrBotEX runtime.",
                requires_runtime_state=[RuntimeState.IDLE.value, RuntimeState.PAUSED.value],
            ),
            BridgeAction(
                action_id="runtime.stop.v1",
                owner="runtime",
                description="Stop AstrBotEX runtime safely.",
                schema={
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                },
            ),
        ]
        for record in self.local_plugins.records.values():
            if not record.enabled:
                continue
            for action in getattr(record.manifest, "actions", []):
                actions.append(
                    BridgeAction(
                        action_id=action.action_id,
                        owner=record.manifest.id,
                        description=action.description,
                        command_topic=action.topic,
                        schema=action.schema,
                        requires_blocks=action.requires_blocks,
                        requires_runtime_state=action.requires_runtime_state,
                        danger=action.danger,
                    )
                )
        return actions

    def handle_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(proposal, dict):
            return {"ok": False, "error": "proposal must be an object"}
        commands = proposal.get("commands")
        if not isinstance(commands, list) or not commands:
            return {"ok": False, "error": "proposal.commands must be a non-empty array"}

        context_id = str(proposal.get("context_id", "")).strip()
        context = self._contexts.get(context_id)
        if context is None:
            return {"ok": False, "error": f"unknown or expired context_id: {context_id}"}
        if time.time() - float(context["created_at"]) > self.context_ttl_sec:
            return {"ok": False, "error": f"stale context_id: {context_id}"}

        actions = {action.action_id: action for action in self.list_actions()}
        prepared: list[tuple[dict[str, Any], BridgeAction]] = []
        for index, command in enumerate(commands):
            if not isinstance(command, dict):
                return {"ok": False, "error": f"commands[{index}] must be an object"}
            action_id = str(command.get("action_id", "")).strip()
            action = actions.get(action_id)
            if action is None:
                return {"ok": False, "error": f"unsupported action_id: {action_id}"}
            owner = command.get("owner")
            if owner is not None and str(owner) != action.owner:
                return {"ok": False, "error": f"owner mismatch for {action_id}"}
            runtime_state = self.controller.runtime.state.value
            if action.requires_runtime_state and runtime_state not in action.requires_runtime_state:
                return {
                    "ok": False,
                    "error": f"{action_id} requires runtime state {action.requires_runtime_state}, got {runtime_state}",
                }
            params = command.get("params", {})
            if not isinstance(params, dict):
                return {"ok": False, "error": f"{action_id}.params must be an object"}
            validation_error = self._validate_value("params", params, action.schema)
            if validation_error:
                return {"ok": False, "error": validation_error}
            block_error = self._validate_block_refs(command, action, context)
            if block_error:
                return {"ok": False, "error": block_error}
            prepared.append((command, action))

        accepted = [self._execute(command, action, context_id) for command, action in prepared]
        self.event_bus.emit("bridge", "AstrBot proposal accepted", context_id=context_id, commands=len(accepted))
        return {"ok": True, "context_id": context_id, "accepted": accepted}

    @staticmethod
    def proposal_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["context_id", "commands"],
            "properties": {
                "context_id": {"type": "string"},
                "commands": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["action_id", "params", "reason"],
                        "properties": {
                            "action_id": {"type": "string"},
                            "owner": {"type": "string"},
                            "uses_blocks": {"type": "array", "items": {"type": "object"}},
                            "params": {"type": "object"},
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
        }

    def _execute(self, command: dict[str, Any], action: BridgeAction, context_id: str) -> dict[str, Any]:
        params = dict(command.get("params", {}) or {})
        reason = str(command.get("reason", "AstrBot proposal")).strip() or "AstrBot proposal"
        if action.action_id == "runtime.start.v1":
            self.controller.start()
            return {"action_id": action.action_id, "owner": action.owner, "status": self.controller.runtime.state.value}
        if action.action_id == "runtime.stop.v1":
            self.controller.stop(str(params.get("reason", reason)))
            return {"action_id": action.action_id, "owner": action.owner, "status": self.controller.runtime.state.value}
        if not action.command_topic:
            raise ValueError(f"action has no command topic: {action.action_id}")

        payload = {
            "context_id": context_id,
            "action_id": action.action_id,
            "owner": action.owner,
            "params": params,
            "reason": reason,
            "uses_blocks": command.get("uses_blocks", []),
        }
        self.topic_bus.publish_payload(
            action.command_topic,
            timestamp=time.time(),
            source="astrbot_bridge",
            payload=payload,
        )
        return {
            "action_id": action.action_id,
            "owner": action.owner,
            "command_topic": action.command_topic,
            "status": "published",
        }

    def _runtime_block(self, status: dict[str, Any], now: float) -> dict[str, Any]:
        return self._core_block(
            block_id="runtime.status.v1",
            schema="runtime_status.v1",
            now=now,
            payload={
                "runtime_state": status.get("runtime_state"),
                "tick_hz": status.get("tick_hz"),
                "active_skill": status.get("active_skill"),
                "active_goal": status.get("active_goal"),
            },
        )

    def _world_block(self, status: dict[str, Any], now: float) -> dict[str, Any]:
        world = status.get("world", {})
        return self._core_block(
            block_id="world.snapshot.v1",
            schema="world_snapshot.v1",
            now=now,
            payload={
                "timestamp": world.get("timestamp"),
                "robot": world.get("robot"),
                "entities": world.get("entities", []),
                "zones": world.get("zones", []),
            },
        )

    def _perception_block(self, status: dict[str, Any], now: float) -> dict[str, Any]:
        world = status.get("world", {})
        targets = [_target_payload(entity) for entity in world.get("entities", []) or []]
        raw_obstacles = to_jsonable(world.get("obstacles", []))
        obstacles = raw_obstacles if isinstance(raw_obstacles, list) else []
        notes = _perception_notes(world.get("task_state"))
        if not notes:
            runtime_world = getattr(getattr(self.controller, "runtime", None), "world", None)
            notes = _perception_notes(getattr(runtime_world, "task_state", {}))
        degraded = bool(world.get("perception_degraded", False))
        return self._core_block(
            block_id="perception.scene.v1",
            schema="perception_scene.v1",
            now=now,
            payload={
                "timestamp": world.get("timestamp"),
                "degraded": degraded,
                "summary": build_scene_summary(targets, obstacles, degraded, notes),
                "targets": targets,
                "obstacles": obstacles,
                "notes": notes,
            },
        )

    def _events_block(self, status: dict[str, Any], now: float) -> dict[str, Any]:
        return self._core_block(
            block_id="events.recent.v1",
            schema="events_recent.v1",
            now=now,
            payload={"events": status.get("recent_events", [])[-10:]},
        )

    def _core_block(self, *, block_id: str, schema: str, now: float, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "block_id": block_id,
            "contract_id": self._contract_id("core", block_id, schema),
            "source_plugin": "core",
            "topic": block_id,
            "schema": schema,
            "seq": int(now * 1000),
            "timestamp": now,
            "ttl_ms": int(self.context_ttl_sec * 1000),
            "fresh": True,
            "payload": to_jsonable(payload),
        }

    def _plugin_blocks(self, now: float) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for publisher in self.local_plugins.list_publishers():
            plugin_id = str(publisher.get("plugin_id", ""))
            for topic_decl in publisher.get("topics", []):
                topic = str(topic_decl.get("topic", ""))
                schema = str(topic_decl.get("schema", "message") or "message")
                message = self.topic_bus.get_latest(topic)
                ttl_ms = message.ttl_ms if message and message.ttl_ms is not None else self.block_ttl_ms
                timestamp = message.timestamp if message else None
                blocks.append(
                    {
                        "block_id": self._block_id(topic, schema),
                        "contract_id": self._contract_id(plugin_id, topic, schema),
                        "source_plugin": plugin_id,
                        "topic": topic,
                        "schema": schema,
                        "label": topic_decl.get("label", topic),
                        "seq": message.seq if message else None,
                        "timestamp": timestamp,
                        "ttl_ms": ttl_ms,
                        "fresh": bool(timestamp and now - timestamp <= ttl_ms / 1000.0),
                        "payload": to_jsonable(message.payload) if message else None,
                    }
                )
        return blocks

    def _validate_block_refs(
        self,
        command: dict[str, Any],
        action: BridgeAction,
        context: dict[str, Any],
    ) -> str | None:
        context_blocks = {
            block["block_id"]: block
            for block in context.get("blocks", [])
            if isinstance(block, dict) and block.get("block_id")
        }
        context_blocks.update(
            {
                block["contract_id"]: block
                for block in context.get("blocks", [])
                if isinstance(block, dict) and block.get("contract_id")
            }
        )
        used = command.get("uses_blocks", [])
        if not isinstance(used, list):
            return f"{action.action_id}.uses_blocks must be an array"
        used_refs = {
            str(item.get("block_id") or item.get("contract_id") or "")
            for item in used
            if isinstance(item, dict)
        }
        for required in action.requires_blocks:
            if required not in used_refs:
                return f"{action.action_id} requires block reference: {required}"
        for item in used:
            if not isinstance(item, dict):
                return f"{action.action_id}.uses_blocks entries must be objects"
            ref = str(item.get("block_id") or item.get("contract_id") or "")
            block = context_blocks.get(ref)
            if block is None:
                return f"unknown block reference: {ref}"
            if not block.get("fresh"):
                return f"stale block reference: {ref}"
            if "seq" in item and item["seq"] != block.get("seq"):
                return f"block seq mismatch for {ref}"
        return None

    def _validate_value(self, path: str, value: Any, schema: dict[str, Any]) -> str | None:
        expected = schema.get("type")
        if expected == "object":
            if not isinstance(value, dict):
                return f"{path} must be an object"
            for key in schema.get("required", []):
                if key not in value:
                    return f"{path}.{key} is required"
            for key, child_schema in schema.get("properties", {}).items():
                if key in value:
                    error = self._validate_value(f"{path}.{key}", value[key], child_schema)
                    if error:
                        return error
        elif expected == "array":
            if not isinstance(value, list):
                return f"{path} must be an array"
            item_schema = schema.get("items", {})
            for index, item in enumerate(value):
                error = self._validate_value(f"{path}[{index}]", item, item_schema)
                if error:
                    return error
        elif expected == "string" and not isinstance(value, str):
            return f"{path} must be a string"
        elif expected == "boolean" and not isinstance(value, bool):
            return f"{path} must be a boolean"
        elif expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            return f"{path} must be an integer"
        elif expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            return f"{path} must be a number"
        if "enum" in schema and value not in schema["enum"]:
            return f"{path} must be one of {schema['enum']}"
        return None

    def _drop_old_contexts(self, now: float) -> None:
        stale = [
            context_id
            for context_id, context in self._contexts.items()
            if now - float(context["created_at"]) > self.context_ttl_sec
        ]
        for context_id in stale:
            self._contexts.pop(context_id, None)

    def _context_id(self, now: float, blocks: list[dict[str, Any]]) -> str:
        seed = "|".join(f"{block['contract_id']}:{block.get('seq')}" for block in blocks)
        digest = hashlib.sha1(f"{now}:{seed}".encode("utf-8")).hexdigest()[:16]
        return f"ctx_{digest}"

    @staticmethod
    def _block_id(topic: str, schema: str) -> str:
        suffix = schema if schema.endswith(".v1") else f"{schema}.v1"
        return topic if topic.endswith(".v1") else f"{topic}.{suffix}"

    @staticmethod
    def _contract_id(source: str, topic: str, schema: str) -> str:
        return hashlib.sha1(f"{source}|{topic}|{schema}".encode("utf-8")).hexdigest()[:12]


def build_scene_summary(
    targets: list[dict[str, Any]],
    obstacles: list[dict[str, Any]],
    degraded: bool,
    notes: list[str],
) -> str:
    parts: list[str] = []
    if degraded:
        detail = "; ".join(str(note) for note in notes if str(note))
        parts.append(f"[DEGRADED: {detail}]" if detail else "[DEGRADED]")

    ordered_targets = sorted(
        targets,
        key=lambda target: (
            _sort_float(target.get("bearing_deg")),
            str(target.get("id", "")),
            str(target.get("type", "")),
        ),
    )
    if ordered_targets:
        parts.append("Targets: " + "; ".join(_target_summary(target) for target in ordered_targets) + ".")
    else:
        parts.append("No targets detected.")

    ordered_obstacles = sorted(
        obstacles,
        key=lambda obstacle: (
            _sort_float(obstacle.get("range_m")),
            _sort_float(obstacle.get("bearing_deg")),
            str(obstacle.get("id", "")),
        ),
    )
    if ordered_obstacles:
        visible = ordered_obstacles[:_MAX_SUMMARY_OBSTACLES]
        obstacle_texts = [_obstacle_summary(obstacle) for obstacle in visible]
        remaining = len(ordered_obstacles) - len(visible)
        if remaining > 0:
            obstacle_texts.append(f"and {remaining} more obstacles")
        parts.append("Obstacles: " + "; ".join(obstacle_texts) + ".")
    else:
        parts.append("No obstacles detected.")

    return " ".join(parts)


def _target_payload(entity: Any) -> dict[str, Any]:
    return {
        "id": _field(entity, "id"),
        "type": _field(entity, "type"),
        "semantic": _field(entity, "semantic"),
        "bearing_deg": _field(entity, "bearing_deg"),
        "range_m": _field(entity, "range_m"),
        "range_quality": _field(entity, "range_quality"),
        "confidence": _field(entity, "confidence"),
    }


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _perception_notes(task_state: Any) -> list[str]:
    if not isinstance(task_state, dict):
        return []
    notes = task_state.get("perception_notes", [])
    if not isinstance(notes, list):
        return []
    return [str(note) for note in notes if str(note)]


def _target_summary(target: dict[str, Any]) -> str:
    label = _label(target, "target")
    bearing = _float_or_none(target.get("bearing_deg"))
    direction = _direction(bearing)
    bearing_text = "bearing unknown" if bearing is None else f"{direction} ({_format_number(bearing)} deg)"
    range_m = _float_or_none(target.get("range_m"))
    if range_m is None:
        return f"{label} {bearing_text}, range unknown"
    return f"{label} {bearing_text} at {_range_text(range_m, target.get('range_quality'))}"


def _obstacle_summary(obstacle: dict[str, Any]) -> str:
    label = str(obstacle.get("id") or "obstacle")
    bearing = _float_or_none(obstacle.get("bearing_deg"))
    direction = _direction(bearing)
    bearing_text = "bearing unknown" if bearing is None else f"{direction} ({_format_number(bearing)} deg)"
    range_m = _float_or_none(obstacle.get("range_m"))
    range_text = "range unknown" if range_m is None else f"at {_format_number(range_m)}m"
    attributed_to = obstacle.get("attributed_to")
    attribution_text = "" if attributed_to is None else f" (identified entity {attributed_to})"
    return f"{label} {bearing_text} {range_text}{attribution_text}"


def _label(item: dict[str, Any], fallback: str) -> str:
    for key in ("type", "semantic", "id"):
        value = item.get(key)
        if value:
            return str(value)
    return fallback


def _direction(bearing_deg: float | None) -> str:
    if bearing_deg is None:
        return "unknown"
    abs_bearing = abs(bearing_deg)
    if abs_bearing <= 10.0:
        return "ahead"
    side = "left" if bearing_deg < 0.0 else "right"
    if abs_bearing <= 30.0:
        return f"slightly {side}"
    if abs_bearing <= 75.0:
        return side
    return f"far {side}"


def _range_text(range_m: float, range_quality: Any) -> str:
    quality = _float_or_none(range_quality)
    value = f"{_format_number(range_m)}m"
    if quality is not None and quality < 0.5:
        return f"~{value} (low confidence)"
    return value


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _sort_float(value: Any) -> float:
    number = _float_or_none(value)
    return number if number is not None else float("inf")


def _format_number(value: float) -> str:
    return f"{value:.1f}"
