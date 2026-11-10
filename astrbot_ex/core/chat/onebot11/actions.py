"""Action registry independent of the WebSocket transport."""
from __future__ import annotations

from datetime import datetime
import logging
from uuid import uuid4

from .. import VERSION
from ..models import ChatError
from ..service import ChatService, entity_id

logger = logging.getLogger(__name__)


class ActionDispatcher:
    def __init__(self, service: ChatService, bot_id: int):
        self.service, self.bot_id = service, bot_id
        if service.user(bot_id)["kind"] != "bot":
            raise ValueError("OneBot self_id must belong to a bot account")
        self.actions = {
            "get_login_info": lambda p: {"user_id": bot_id, "nickname": service.user(bot_id)["display_name"]},
            "get_version_info": lambda p: {"app_name": "astrEX", "app_version": VERSION, "protocol_version": "11"},
            "get_status": lambda p: {"online": True, "good": True},
            "can_send_image": lambda p: {"yes": False},
            "can_send_record": lambda p: {"yes": False},
            "get_group_list": lambda p: [self._group(c) for c in service.conversations(bot_id) if c["kind"] == "group"],
            "get_group_info": lambda p: self._group(self._find_group(p)),
            "get_group_member_list": lambda p: [self._member(self._find_group(p), m) for m in self._find_group(p)["members"]],
            "get_group_member_info": self._member_info,
            "get_friend_list": self._friends,
            "send_group_msg": self._send_group,
            "send_private_msg": self._send_private,
            "send_msg": self._send,
            "get_msg": self._get_message,
            "delete_msg": self._delete,
            "_ex_get_capabilities": lambda p: {"actions": sorted(self.actions), "segments": ["text"], "product": "astrEX"},
        }

    def dispatch(self, request) -> dict:
        response = {"status": "failed", "retcode": 1400, "data": None}
        if isinstance(request, dict) and "echo" in request:
            response["echo"] = request["echo"]
        try:
            if not isinstance(request, dict) or not isinstance(request.get("action"), str) or not isinstance(request.get("params", {}), dict):
                raise ChatError("invalid_action", "Invalid action envelope")
            handler = self.actions.get(request["action"])
            if handler is None:
                response.update(retcode=1404, message="Unsupported action")
                return response
            data = handler(request.get("params", {}))
            response.update(status="ok", retcode=0, data=data)
        except ChatError as exc:
            response.update(retcode={401: 1401, 403: 1403, 404: 1404, 409: 1409}.get(exc.status, 1400), message=exc.message)
        except (KeyError, TypeError, ValueError):
            response.update(message="Invalid action parameters")
        except Exception:
            logger.exception("OneBot action failed")
            response.update(retcode=1500, message="Chat service failed")
        return response

    def _find_group(self, params: dict) -> dict:
        gid = str(entity_id(params["group_id"]))
        group = next((c for c in self.service.conversations(self.bot_id) if c["group_id"] == gid), None)
        if group is None:
            raise ChatError("forbidden", "Bot cannot access this group", 403)
        return group

    @staticmethod
    def _group(group: dict) -> dict:
        return {"group_id": int(group["group_id"]), "group_name": group["title"], "member_count": len(group["members"]), "max_member_count": 100}

    @staticmethod
    def _member(group: dict, person: dict) -> dict:
        return {"group_id": int(group["group_id"]), "user_id": int(person["user_id"]), "nickname": person["display_name"],
                "card": "", "sex": "unknown", "age": 0, "area": "", "join_time": 0, "last_sent_time": 0,
                "level": "", "role": person["role"], "unfriendly": False, "title": "", "title_expire_time": 0, "card_changeable": False}

    def _member_info(self, params: dict) -> dict:
        group = self._find_group(params)
        uid = str(entity_id(params["user_id"]))
        person = next((m for m in group["members"] if m["user_id"] == uid), None)
        if person is None:
            raise ChatError("not_found", "Group member not found", 404)
        return self._member(group, person)

    def _friends(self, params: dict) -> list:
        # Skeleton semantics: accounts with an established direct conversation.
        ids = {m["user_id"] for c in self.service.conversations(self.bot_id) if c["kind"] == "direct" for m in c["members"] if int(m["user_id"]) != self.bot_id}
        return [{"user_id": int(uid), "nickname": self.service.user(int(uid))["display_name"], "remark": ""} for uid in sorted(ids)]

    @staticmethod
    def _segments(params: dict) -> list:
        message = params["message"]
        if isinstance(message, str):
            if "[CQ:" in message and not params.get("auto_escape", False):
                raise ChatError("unsupported_segment", "CQ-code parsing is not implemented yet")
            return [{"type": "text", "data": {"text": message}}]
        if not isinstance(message, list):
            raise ChatError("invalid_message", "Message must be text or an array")
        return message

    def _send_group(self, params: dict) -> dict:
        group = self._find_group(params)
        result = self.service.send(self.bot_id, group["conversation_id"], str(uuid4()), self._segments(params))
        return {"message_id": int(result["message_id"])}

    def _send_private(self, params: dict) -> dict:
        conversation = self.service.direct(self.bot_id, str(entity_id(params["user_id"])))
        result = self.service.send(self.bot_id, conversation["conversation_id"], str(uuid4()), self._segments(params))
        return {"message_id": int(result["message_id"])}

    def _send(self, params: dict) -> dict:
        kind = params.get("message_type", "group" if "group_id" in params else "private")
        if kind not in {"group", "private"}:
            raise ChatError("invalid_message_type", "Invalid message type")
        return self._send_group(params) if kind == "group" else self._send_private(params)

    def _get_message(self, params: dict) -> dict:
        message = self.service.get_message(self.bot_id, str(entity_id(params["message_id"])))
        conversation = self.service.conversation(self.bot_id, message["conversation_id"])
        sender = self.service.user(int(message["sender_id"]))
        return {"time": int(datetime.fromisoformat(message["created_at"].replace("Z", "+00:00")).timestamp()),
                "message_type": conversation["kind"] if conversation["kind"] == "group" else "private",
                "message_id": int(message["message_id"]), "real_id": int(message["message_id"]),
                "sender": {"user_id": int(message["sender_id"]), "nickname": sender["display_name"]},
                "message": message["segments"]}

    def _delete(self, params: dict):
        self.service.recall(self.bot_id, str(entity_id(params["message_id"])))
        return None

    def event(self, native: dict) -> dict | None:
        if native["type"] != "message.created":
            return None
        message = native["payload"]["message"]
        if int(message["sender_id"]) == self.bot_id:
            return None
        conversation = self.service.conversation(self.bot_id, message["conversation_id"])
        sender = self.service.user(int(message["sender_id"]))
        result = {"time": int(datetime.fromisoformat(message["created_at"].replace("Z", "+00:00")).timestamp()),
                  "self_id": self.bot_id, "post_type": "message", "message_id": int(message["message_id"]),
                  "user_id": int(message["sender_id"]), "message": message["segments"],
                  "raw_message": "".join(s["data"]["text"] for s in message["segments"]), "font": 0}
        if conversation["kind"] == "group":
            person = next(m for m in conversation["members"] if m["user_id"] == message["sender_id"])
            result.update(message_type="group", sub_type="normal", group_id=int(conversation["group_id"]), anonymous=None,
                          sender={k: v for k, v in self._member(conversation, person).items() if k not in {"group_id", "unfriendly", "card_changeable", "join_time", "last_sent_time", "title_expire_time"}})
        else:
            result.update(message_type="private", sub_type="friend", sender={"user_id": int(message["sender_id"]), "nickname": sender["display_name"], "sex": "unknown", "age": 0})
        return result
