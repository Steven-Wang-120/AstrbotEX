"""AstrBot OpenAPI text conversation, with an isolated, reusable WebChat session."""
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ChatError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AstrBotChat:
    def __init__(self, config):
        self.config = dict(config)
        self.session_id = uuid.uuid4().hex

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        if not path.is_file():
            raise ChatError("缺少 AstrBot 连接配置，请先建立本地聊天连接。")
        return cls(json.loads(path.read_text(encoding="utf-8-sig")))

    def new_session(self):
        self.session_id = uuid.uuid4().hex

    def ask(self, text, on_text=None, cancel_event=None):
        if not isinstance(text, str) or not text.strip():
            raise ChatError("提问不能为空。")
        base = self.config.get("base_url", "http://127.0.0.1:6185").rstrip("/")
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ChatError("AstrBot 地址无效。")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ChatError("远程 AstrBot 请通过 HTTPS 或 SSH 隧道连接。")
        token = os.environ.get(self.config.get("token_env", "ASTRBOT_API_KEY"), "") or self.config.get("api_key", "")
        if not token:
            raise ChatError("缺少 AstrBot OpenAPI Key（需要 chat 权限）。")
        timeout = float(self.config.get("timeout", 90))
        if not math.isfinite(timeout) or not 1 <= timeout <= 180:
            raise ChatError("对话超时必须在 1～180 秒之间。")
        payload = {"username": self.config.get("username", "jetmax_voice"),
                   "session_id": self.session_id, "message": text, "enable_streaming": True}
        if self.config.get("config_id"):
            payload["config_id"] = self.config["config_id"]
        if self.config.get("provider_id"):
            payload["selected_provider"] = self.config["provider_id"]
        request = urllib.request.Request(base + "/api/v1/chat", method="POST",
                                         data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                                                  "Authorization": "Bearer " + token})
        try:
            opener = urllib.request.build_opener(NoRedirect())
            with opener.open(request, timeout=timeout) as response:
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    raise ChatError("AstrBot 没有启动对话，请确认 DSV4 已保存、启用并设为默认模型。")
                return self._read_reply(response, timeout, on_text=on_text, cancel_event=cancel_event)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status in (401, 403):
                raise ChatError("AstrBot 认证失败，请检查访问 Key 及 chat 权限。") from None
            raise ChatError("AstrBot 返回 HTTP {}。".format(status)) from None
        except (urllib.error.URLError, OSError):
            raise ChatError("AstrBot 对话连接失败或超时，请确认服务与模型接口正常。") from None

    def _read_reply(self, response, timeout, on_text=None, cancel_event=None):
        deadline = time.monotonic() + timeout
        consumed = 0
        event_lines = []
        parts = []
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ChatError("对话已取消。")
            if time.monotonic() >= deadline:
                raise ChatError("AstrBot 对话超时。")
            raw = response.readline(65537)
            if not raw:
                raise ChatError("AstrBot 对话连接提前结束，请重试；没有播放未完成的回答。")
            consumed += len(raw)
            if len(raw) > 65536 or consumed > 2 * 1024 * 1024:
                raise ChatError("AstrBot 回复超过大小限制。")
            try:
                line = raw.decode("utf-8").rstrip("\r\n")
            except UnicodeError:
                raise ChatError("AstrBot 事件不是 UTF-8 文本。")
            if line.startswith("data:"):
                event_lines.append(line[5:].lstrip(" "))
            elif not line and event_lines:
                text = "\n".join(event_lines)
                event_lines = []
                if text == "[DONE]":
                    return self._finish(parts)
                try:
                    event = json.loads(text)
                except ValueError:
                    raise ChatError("AstrBot 返回了无效的事件数据。")
                if not isinstance(event, dict):
                    raise ChatError("AstrBot 事件必须是对象。")
                kind = event.get("type")
                if kind == "error":
                    raise ChatError("AstrBot 生成回答失败，请检查 DSV4 配置和服务端错误提示。")
                if kind == "plain" and event.get("chain_type") not in ("reasoning", "tool_call", "tool_call_result", "agent_stats"):
                    value = event.get("data")
                    if isinstance(value, str):
                        parts.append(value)
                        if on_text is not None:
                            on_text(value)
                if kind == "end":
                    return self._finish(parts)

    @staticmethod
    def _finish(parts):
        text = "".join(parts).strip()
        if not text:
            raise ChatError("AstrBot 没有返回可播报的文字回答。")
        return text
