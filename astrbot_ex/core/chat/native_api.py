"""Native human-facing API, isolated from EX management endpoints."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from . import PROTOCOL, VERSION
from .models import ChatError, DirectRequest, GroupRequest, LoginRequest, ProfilePatch, RegisterRequest, SendMessageRequest
from .service import ChatService


def create_app(service: ChatService, *, invite_token: str | None = None, connector=None, demo: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(connector.run()) if connector is not None else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="astrEX Chat", version=VERSION, lifespan=lifespan)
    if demo:
        app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                           allow_methods=["GET", "POST", "PATCH"], allow_headers=["Authorization", "Content-Type"])
    app.state.chat_service = service
    prefix = "/api/v1/chat"

    @app.exception_handler(ChatError)
    async def domain_error(request: Request, exc: ChatError):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message, "request_id": str(uuid4())}})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Validation errors can contain password input; expose no submitted body.
        return JSONResponse(status_code=400, content={"error": {"code": "invalid_request", "message": "Invalid request fields", "request_id": str(uuid4())}})

    def token(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise ChatError("unauthorized", "Please sign in", 401)
        return authorization[7:]

    def current_user(access: str = Depends(token)) -> int:
        return service.authenticate(access)

    @app.get(prefix + "/health")
    def health():
        return {"product": "astrEX", "version": VERSION, "protocol": PROTOCOL, "mode": "demo" if demo else "normal",
                "capabilities": ["login", "profile", "conversations", "groups", "text", "mention", "reply", "history", "sync", "events", "recall"],
                "onebot_connected": connector.connected if connector else False}

    @app.post(prefix + "/auth/register", status_code=201)
    def register(body: RegisterRequest):
        if not invite_token:
            raise ChatError("registration_disabled", "Registration is not enabled", 503)
        if not hmac.compare_digest(body.invite_token, invite_token):
            raise ChatError("forbidden", "Invalid invitation", 403)
        service.create_user(body.username, body.password, body.display_name)
        return service.login(body.username, body.password, body.device_id)

    @app.post(prefix + "/auth/login")
    def login(body: LoginRequest):
        return service.login(body.username, body.password, body.device_id)

    @app.post(prefix + "/auth/logout")
    def logout(access: str = Depends(token), user_id: int = Depends(current_user)):
        service.logout(access)
        return {"ok": True}

    @app.post(prefix + "/auth/refresh")
    def refresh(user_id: int = Depends(current_user)):
        raise ChatError("not_implemented", "Token rotation is not implemented in this skeleton", 501)

    @app.get(prefix + "/me")
    def me(user_id: int = Depends(current_user)):
        return service.user(user_id)

    @app.patch(prefix + "/me")
    def patch_profile(body: ProfilePatch, user_id: int = Depends(current_user)):
        return service.update_profile(user_id, body.model_dump(exclude_unset=True))

    @app.get(prefix + "/conversations")
    def conversations(user_id: int = Depends(current_user)):
        return {"items": service.conversations(user_id)}

    @app.post(prefix + "/conversations/direct", status_code=201)
    def direct(body: DirectRequest, user_id: int = Depends(current_user)):
        return service.direct(user_id, body.target_user_id)

    @app.post(prefix + "/groups", status_code=201)
    def create_group(body: GroupRequest, user_id: int = Depends(current_user)):
        return service.create_group(user_id, body.title, body.member_ids)

    @app.get(prefix + "/groups/{group_id}/members")
    def group_members(group_id: int, user_id: int = Depends(current_user)):
        group = next((x for x in service.conversations(user_id) if x["group_id"] == str(group_id)), None)
        if group is None:
            raise ChatError("forbidden", "Group is not accessible", 403)
        return {"items": group["members"]}

    @app.get(prefix + "/conversations/{conversation_id}/messages")
    def messages(conversation_id: str, limit: int = Query(default=50, ge=1, le=100), before: int | None = Query(default=None, ge=1), user_id: int = Depends(current_user)):
        items = service.messages(user_id, conversation_id, limit, before)
        return {"items": items, "next_before": items[0]["message_id"] if len(items) == limit else None}

    @app.post(prefix + "/conversations/{conversation_id}/messages", status_code=201)
    def send(conversation_id: str, body: SendMessageRequest, user_id: int = Depends(current_user)):
        return service.send(user_id, conversation_id, str(body.client_message_id), [segment.model_dump() for segment in body.segments])

    @app.post(prefix + "/messages/{message_id}/recall")
    def recall(message_id: str, user_id: int = Depends(current_user)):
        service.recall(user_id, message_id)
        return {"ok": True}

    @app.get(prefix + "/sync")
    def sync(after: str | None = Query(default=None, max_length=512), limit: int = Query(default=100, ge=1, le=100), user_id: int = Depends(current_user)):
        return service.sync(user_id, after, limit)

    @app.get(prefix + "/posts")
    @app.post(prefix + "/posts")
    def posts(user_id: int = Depends(current_user)):
        raise ChatError("not_implemented", "Posts are planned for the next stage", 501)

    @app.websocket(prefix + "/events")
    async def events(socket: WebSocket):
        authorization = socket.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            await socket.close(code=4401)
            return
        access = authorization[7:]
        after = socket.query_params.get("after")
        try:
            user_id = await run_in_threadpool(service.authenticate, access)
            await run_in_threadpool(service.sync, user_id, after, 1)
        except ChatError as exc:
            await socket.close(code=4401 if exc.status == 401 else 4400)
            return
        await socket.accept()
        try:
            while True:
                await run_in_threadpool(service.authenticate, access)
                batch = await run_in_threadpool(service.sync, user_id, after, 100)
                for event in batch["events"]:
                    await asyncio.wait_for(socket.send_json(event), timeout=5)
                after = batch["next_cursor"]
                # Durable polling avoids a replay/subscribe gap in the skeleton.
                if batch["has_more"]:
                    await asyncio.sleep(0)
                else:
                    try:
                        packet = await asyncio.wait_for(socket.receive(), timeout=0.25)
                    except asyncio.TimeoutError:
                        pass
                    else:
                        if packet["type"] == "websocket.disconnect":
                            return
        except ChatError:
            await socket.close(code=4401)
        except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
            return

    return app
