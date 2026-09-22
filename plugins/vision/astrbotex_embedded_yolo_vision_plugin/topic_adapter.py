from __future__ import annotations

from typing import Any


class TopicAdapter:
    def __init__(self, context, config: dict[str, Any], emit) -> None:
        self.context = context
        self.config = config
        self.emit = emit
        self.enabled = bool(config.get("publish_topics", False))
        self.prefix = str(config.get("topic_prefix", "astrbotex_embedded_yolo_vision_plugin"))

    def publish(self, packet: dict[str, Any]) -> None:
        if not self.enabled or not hasattr(self.context, "topic_bus"):
            return
        timestamp = float(packet["timestamp"])
        source = str(packet.get("source", self.prefix))
        frame = source
        self.context.topic_bus.publish_payload(f"{self.prefix}.raw_packet", timestamp=timestamp, source=source, frame=frame, payload=packet)
        objects = list(packet.get("objects", []))
        self.context.topic_bus.publish_payload(f"{self.prefix}.detections", timestamp=timestamp, source=source, frame=frame, payload={"frame_id": packet["frame_id"], "source": source, "objects": objects, "metadata": packet.get("metadata", {})})
        current = self._pick_target(objects, packet["frame_id"])
        if current is None:
            current = {"target_valid": False, "target_x": "empty", "frame_id": packet["frame_id"], "source": source}
        self.context.topic_bus.publish_payload(str(self.config.get("vision_target_topic", f"{self.prefix}.current_target")), timestamp=timestamp, source=source, frame=frame, payload=current)

    def _pick_target(self, objects: list[dict[str, Any]], frame_id: int) -> dict[str, Any] | None:
        if not objects:
            return None
        target = max(objects, key=lambda item: float(item.get("confidence", item.get("score", 0.0)) or 0.0))
        return {**target, "frame_id": frame_id}
