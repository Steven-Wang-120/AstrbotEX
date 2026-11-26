"""Bounded Chinese task sessions. L1 produces proposals, never submits Goals."""
from __future__ import annotations
import copy
import dataclasses
import math
import re
import time
import uuid
from .contracts import COLORS, SHAPES, REGIONS, SUCCESS, digest, skill_catalog, validate_skill_parameters

STATE_OPTIONS = (
    {"option_id": "clarification", "kind": "clarification_required", "label": "Ask for missing details"},
    {"option_id": "wait", "kind": "wait_observation", "label": "Wait for observations"},
    {"option_id": "unsupported", "kind": "unsupported", "label": "Unsupported request"},
    {"option_id": "done", "kind": "done", "label": "Task already complete"},
)

@dataclasses.dataclass(frozen=True)
class TaskContext:
    task_session: str
    task_revision: int
    request_id: str
    catalog_revision: str
    text: str
    observation: dict
    options: tuple[dict, ...]
    mode: str = "structured"
    option_set_id: str = ""
    input_hash: str = ""

    def __post_init__(self):
        if self.mode not in {"text", "structured"} or not 1 <= len(self.options) <= 8:
            raise ValueError("invalid_task_context")
        ids = [item["option_id"] for item in self.options]
        if len(ids) != len(set(ids)) or not self.text.strip():
            raise ValueError("invalid_task_options")
        obs, options = copy.deepcopy(self.observation), copy.deepcopy(tuple(self.options))
        object.__setattr__(self, "observation", obs)
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "option_set_id", digest(options))
        object.__setattr__(self, "input_hash", digest({"text": self.text, "observation": obs, "options": options, "mode": self.mode}))

    def to_dict(self):
        return dataclasses.asdict(self)

@dataclasses.dataclass(frozen=True)
class TaskSelection:
    request_id: str
    task_session: str
    task_revision: int
    catalog_revision: str
    option_set_id: str
    option_id: str
    model: str
    model_revision: str
    model_hash: str
    service_generation: str
    elapsed_ms: float
    input_hash: str
    probabilities: dict

    def to_dict(self):
        return dataclasses.asdict(self)

class TaskSession:
    def __init__(self, *, session_id=None, catalog=None):
        self.session_id = session_id or uuid.uuid4().hex
        self.revision = 0
        self.catalog = copy.deepcopy(catalog or skill_catalog())
        self.turns = []
        self.canceled = False
        self.last_context = None
        self.goal_submissions = 0  # L1 has no Goal submission dependency.

    def cancel(self):
        self.canceled = True
        self.revision += 1

    def feed(self, text, observation, *, mode="structured"):
        if self.canceled:
            raise ValueError("task_canceled")
        if not isinstance(text, str) or not text.strip() or len(text) > 512:
            raise ValueError("invalid_task_text")
        self.turns.append(text.strip())
        self.revision += 1
        text = "；".join(self.turns)
        obs = copy.deepcopy(observation)
        obs.setdefault("received_monotonic_ns", time.monotonic_ns())
        # Explicit names form a small grammar; unknown wording requires clarification.
        is_fetch = any(word in text for word in ("抓", "夹", "搬", "放"))
        is_move = any(word in text for word in ("移动", "前往", "开到", "去")) and not is_fetch
        colors = [v for k, v in COLORS.items() if k in text]
        shapes = [v for k, v in SHAPES.items() if k in text]
        regions = [v for k, v in REGIONS.items() if k in text]
        places = [v for v in regions if v.startswith("tray_")]
        reason, question = "", ""
        if self.catalog.get("available") is False:
            reason, question = "skill_unavailable", "技能尚未就绪：" + self.catalog.get("unavailable_reason", "unavailable")
        elif any(word in text for word in ("不要", "别", "取消", "不抓", "不放", "停止")):
            reason, question = "clarification_required", "本入口只接受明确的单个动作请求；取消使用/cancel。"
        elif not (is_fetch or is_move) or "杯子" in text:
            reason = "unsupported"
        elif is_fetch and len(places) != 1:
            reason, question = "clarification_required", "放到哪个区域：左托盘还是右托盘？"
        elif is_fetch and (len(colors) != 1 or len(shapes) != 1):
            reason, question = "clarification_required", "说明物体颜色和类别：红、蓝、绿；方块、圆柱。"
        elif is_move and len([r for r in regions if r in {"table_dock", "home"}]) != 1:
            reason, question = "clarification_required", "移动到哪个区域：操作台停车区还是起点区？"
        elif re.search(r"[0-9]+(?:\.[0-9]+)?\s*(?:毫米|厘米|米|度|mm|cm|rad)", text):
            reason, question = "clarification_required", "首版仅支持标准或精确档，选择支持的精度。"
        elif not obs.get("valid", False) or obs.get("age_ms", math.inf) > obs.get("max_age_ms", 500):
            reason = "wait_observation"
        objects = obs.get("objects", [])
        matches = [o for o in objects if o.get("color") in colors and o.get("shape") in shapes]
        if not reason and is_fetch and obs.get("at_table", False) and len(matches) != 1:
            if len(matches) > 1:
                reason, question = "clarification_required", "有多个相同目标，明确对象编号。"
                explicit = [o for o in matches if o["id"] in text]
                if len(explicit) == 1:
                    reason, question, matches = "", "", explicit
            else:
                reason = "wait_observation"
        options = [
            {"option_id": "move:table_dock", "kind": "move", "region_id": "table_dock", "label": "Move to table dock"},
            {"option_id": "move:home", "kind": "move", "region_id": "home", "label": "Move to home"},
        ]
        # All observed objects remain candidates, not only the requested color.
        if len(objects) > 2:
            reason, question = "clarification_required", "目标候选过多，缩小工作区或明确对象。"
        for obj in objects[:2] if mode == "structured" else []:
            options.append({"option_id": "fetch:" + obj["id"], "kind": "fetch", "object_id": obj["id"],
                            "label": "Fetch " + obj["id"] + " " + obj["color"] + " " + obj["shape"]})
        if mode == "text":
            options.append({"option_id": "fetch", "kind": "fetch", "label": "Fetch object to named tray"})
        options.extend(copy.deepcopy(STATE_OPTIONS))
        self.intent = {"kind": "fetch" if is_fetch else "move", "target": matches[0]["id"] if len(matches) == 1 else None,
                       "place": places[0] if len(places) == 1 else None, "regions": regions,
                       "precision": "精确" in text, "reason": reason, "question": question}
        context = TaskContext(self.session_id, self.revision, uuid.uuid4().hex,
                              self.catalog["revision"], text, obs, tuple(options), mode)
        self.last_context = context
        return context, {"state": reason or "ready_for_selection", "question": question, "goal_submissions": 0}

    def accept(self, selection, *, service_generation, current_observation_id=None):
        context = self.last_context
        if context is None or self.canceled or selection.task_revision != self.revision:
            raise ValueError("stale_task_selection")
        if digest({"text":context.text,"observation":context.observation,"options":context.options,"mode":context.mode}) != context.input_hash:
            raise ValueError("context_changed_after_selection")
        checks = ((selection.task_session, context.task_session), (selection.request_id, context.request_id),
                  (selection.catalog_revision, self.catalog["revision"]), (selection.option_set_id, context.option_set_id),
                  (selection.input_hash, context.input_hash), (selection.service_generation, service_generation))
        if any(a != b for a, b in checks):
            raise ValueError("stale_task_selection")
        if current_observation_id is not None and current_observation_id != context.observation.get("observation_id"):
            raise ValueError("stale_observation")
        if self.intent["reason"]:
            return {"state": self.intent["reason"], "question": self.intent["question"], "goal_submissions": 0}
        received = context.observation.get("received_monotonic_ns", time.monotonic_ns())
        age = context.observation.get("age_ms", math.inf) + max(0, time.monotonic_ns()-received)/1e6
        if age > context.observation.get("max_age_ms", 500):
            return {"state": "wait_observation", "goal_submissions": 0}
        option = next((o for o in context.options if o["option_id"] == selection.option_id), None)
        if option is None:
            raise ValueError("unknown_option")
        if option["kind"] == "done":
            if not context.observation.get("task_success_proven", False):
                raise ValueError("success_not_proven")
            return {"state": "done", "goal_submissions": 0}
        if option["kind"] not in {"move", "fetch"}:
            return {"state": option["kind"], "goal_submissions": 0}
        intended = self.intent
        expected = "move" if intended["kind"] == "fetch" and not context.observation.get("at_table") else intended["kind"]
        if option["kind"] != expected:
            raise ValueError("selection_conflicts_with_task")
        if expected == "move":
            region = "table_dock" if intended["kind"] == "fetch" else intended["regions"][0]
            if option["region_id"] != region:
                raise ValueError("wrong_region")
            params = {"target_region_id": region, "position_tolerance_m": .05 if intended["precision"] else .10,
                      "yaw_tolerance_rad": math.radians(5 if intended["precision"] else 10), "success_template": SUCCESS["move"]}
        else:
            if (context.mode == "structured" and option["object_id"] != intended["target"]) or not context.observation.get("stationary"):
                raise ValueError("wrong_object_or_base_moving")
            params = {"object_id": intended["target"], "place_region_id": intended["place"],
                      "placement_tolerance_m": .010 if intended["precision"] else .020,
                      "orientation_mode": "yaw" if "朝向" in context.text else "free", "yaw_tolerance_rad": math.radians(5), "success_template": SUCCESS["fetch"]}
        return {"state": "ready", "skill": expected, "parameters": validate_skill_parameters(expected, params),
                "contract_hash": digest(params), "goal_submissions": 0, "execution_enabled": False}

def run_cli(task_session, observation_provider, selector, *, input_fn=input, output_fn=print):
    """Embed this input channel in the existing EX process; never create a runtime."""
    while True:
        text = input_fn("任务（/cancel 或 /quit）：")
        if text == "/quit":
            return
        if text == "/cancel":
            task_session.cancel()
            selector.cancel()
            return
        context, status = task_session.feed(text, observation_provider())
        if status["state"] != "ready_for_selection":
            output_fn(status)
            continue
        selection = selector.select(context)
        output_fn(task_session.accept(selection, service_generation=selector.generation))
