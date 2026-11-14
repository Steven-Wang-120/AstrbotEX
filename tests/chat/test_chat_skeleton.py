from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

from astrbot_ex.core.chat.lifecycle import ChatLifecycle
from astrbot_ex.core.chat.native_api import create_app
from astrbot_ex.core.chat.onebot11.actions import ActionDispatcher
from astrbot_ex.core.chat.onebot11.transport import ReverseConnector
from astrbot_ex.core.chat.repository import ChatRepository
from astrbot_ex.core.chat.service import ChatService


@pytest.fixture
def rig(tmp_path):
    repository = ChatRepository(tmp_path / "chat.sqlite3")
    service = ChatService(repository)
    ids = service.seed_demo()
    with TestClient(create_app(service, demo=True)) as client:
        yield service, ids, client
    repository.close()


def auth(client, username="alice"):
    response = client.post("/api/v1/chat/auth/login", json={"username": username, "password": "astrc-demo", "device_id": "test-device"})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def body(text="Hello", key=None):
    return {"client_message_id": key or str(uuid4()), "segments": [{"type": "text", "data": {"text": text}}]}


def test_login_logout_and_validation_do_not_leak_password(rig):
    _, _, client = rig
    assert client.get("/api/v1/chat/me").status_code == 401
    headers = auth(client)
    assert client.get("/api/v1/chat/me", headers=headers).json()["username"] == "alice"
    assert client.post("/api/v1/chat/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/chat/me", headers=headers).status_code == 401
    secret = "do-not-echo-this-password"
    response = client.post("/api/v1/chat/auth/login", json={"username": "alice", "password": secret})
    assert response.status_code == 400 and secret not in response.text


def test_idempotency_is_atomic_and_content_conflicts_are_rejected(rig):
    _, ids, client = rig
    headers = auth(client)
    url = f"/api/v1/chat/conversations/{ids['conversation_id']}/messages"
    payload = body()
    first = client.post(url, json=payload, headers=headers)
    second = client.post(url, json=payload, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    payload["segments"][0]["data"]["text"] = "Changed"
    assert client.post(url, json=payload, headers=headers).status_code == 409
    assert len(client.get(url, headers=headers).json()["items"]) == 2


def test_conversation_membership_and_own_profile_permissions(rig):
    service, ids, client = rig
    outsider = service.create_user("eve", "astrc-demo", "Eve")
    headers = auth(client, "eve")
    url = f"/api/v1/chat/conversations/{ids['conversation_id']}/messages"
    assert client.get(url, headers=headers).status_code == 403
    assert client.post(url, json=body(), headers=headers).status_code == 403
    assert client.patch("/api/v1/chat/me", json={"user_id": ids["alice"], "display_name": "Hijacked"}, headers=headers).status_code == 400
    response = client.patch("/api/v1/chat/me", json={"display_name": "Eve updated", "bio": "Hello"}, headers=headers)
    assert response.status_code == 200 and response.json()["user_id"] == outsider["user_id"]
    assert service.user(int(ids["alice"]))["display_name"] == "Alice"


def test_cursor_is_account_bound_and_events_are_recoverable(rig):
    _, ids, client = rig
    alice, bob = auth(client), auth(client, "bob")
    before = client.get("/api/v1/chat/sync", headers=alice).json()["next_cursor"]
    assert client.get("/api/v1/chat/sync", params={"after": before}, headers=bob).status_code == 400
    assert client.get("/api/v1/chat/sync", params={"after": "not-a-cursor"}, headers=alice).status_code == 400
    malformed = base64.urlsafe_b64encode("1:0:非ASCII签名".encode()).decode()
    assert client.get("/api/v1/chat/sync", params={"after": malformed}, headers=alice).status_code == 400
    client.post(f"/api/v1/chat/conversations/{ids['conversation_id']}/messages", json=body("After cursor"), headers=alice)
    events = client.get("/api/v1/chat/sync", params={"after": before}, headers=alice).json()["events"]
    assert len(events) == 1 and events[0]["payload"]["message"]["segments"][0]["data"]["text"] == "After cursor"


def test_native_websocket_delivers_persisted_messages(rig):
    _, _, client = rig
    headers = auth(client)
    with client.websocket_connect("/api/v1/chat/events", headers=headers) as socket:
        first = socket.receive_json()
        second = socket.receive_json()
        assert first["type"] == "conversation.updated"
        assert second["type"] == "message.created"
        assert int(second["seq"]) > int(first["seq"])


def test_recall_requires_sender_or_group_admin_and_removes_content(rig):
    _, ids, client = rig
    alice, bob = auth(client), auth(client, "bob")
    url = f"/api/v1/chat/conversations/{ids['conversation_id']}/messages"
    message = client.post(url, json=body(), headers=alice).json()
    recall = f"/api/v1/chat/messages/{message['message_id']}/recall"
    assert client.post(recall, headers=bob).status_code == 403
    assert client.post(recall, headers=alice).status_code == 200
    stored = client.get(url, headers=bob).json()["items"][-1]
    assert stored["recalled"] and stored["segments"] == []


def test_messages_sessions_and_cursors_survive_database_reopen(tmp_path):
    path = tmp_path / "chat.sqlite3"
    repo = ChatRepository(path)
    service = ChatService(repo)
    ids = service.seed_demo()
    session = service.login("alice", "astrc-demo", "restart-test")
    cursor = service.sync(int(ids["alice"]))["next_cursor"]
    repo.close()
    reopened = ChatRepository(path)
    try:
        service = ChatService(reopened)
        assert service.authenticate(session["access_token"]) == int(ids["alice"])
        assert service.sync(int(ids["alice"]), cursor)["events"] == []
        assert len(service.messages(int(ids["alice"]), ids["conversation_id"])) == 1
    finally:
        reopened.close()


def test_onebot_registry_preserves_echo_and_rejects_unsupported_capabilities(rig):
    service, ids, _ = rig
    dispatcher = ActionDispatcher(service, int(ids["bot"]))
    echo = {"trace": [1, "object-echo"]}
    version = dispatcher.dispatch({"action": "get_version_info", "echo": echo})
    assert version["data"]["app_name"] == "astrEX" and version["echo"] == echo
    assert dispatcher.dispatch({"action": "missing", "echo": False})["retcode"] == 1404
    assert dispatcher.dispatch({"action": "can_send_image"})["data"] == {"yes": True}
    sent = dispatcher.dispatch({"action": "send_group_msg", "params": {"group_id": 1, "message": "Bot message"}, "echo": echo})
    assert sent["status"] == "ok" and sent["echo"] == echo
    native = service.sync(int(ids["bot"]))["events"]
    assert any(dispatcher.event(e) for e in native)
    assert not any(dispatcher.event(e) for e in native if e["type"] == "message.created" and e["payload"]["message"]["sender_id"] == ids["bot"])
    forbidden = service.create_group(int(ids["alice"]), "Private group", [ids["bob"]])
    assert dispatcher.dispatch({"action": "send_group_msg", "params": {"group_id": int(forbidden["group_id"]), "message": "No"}})["retcode"] == 1403


def test_reverse_websocket_real_transport_roundtrip(rig):
    service, ids, _ = rig
    async def scenario():
        complete = asyncio.get_running_loop().create_future()
        async def peer(socket):
            try:
                assert socket.request.headers["X-Self-ID"] == ids["bot"]
                assert socket.request.headers["X-Client-Role"] == "Universal"
                assert socket.request.headers["Authorization"] == "Bearer test-token"
                lifecycle = json.loads(await socket.recv())
                assert lifecycle["meta_event_type"] == "lifecycle"
                echo = {"request": 17}
                await socket.send(json.dumps({"action": "send_group_msg", "params": {"group_id": 1, "message": "Reply from fake OneBot peer"}, "echo": echo}))
                saw_message = saw_response = False
                while not (saw_message and saw_response):
                    item = json.loads(await socket.recv())
                    if item.get("post_type") == "message":
                        assert item["user_id"] != int(ids["bot"])
                        saw_message = True
                    if "retcode" in item:
                        assert item["status"] == "ok" and item["echo"] == echo
                        saw_response = True
                complete.set_result(True)
                await socket.wait_closed()
            except Exception as exc:
                if not complete.done(): complete.set_exception(exc)
        async with serve(peer, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            connector = ReverseConnector(service, int(ids["bot"]), f"ws://127.0.0.1:{port}/ws", "test-token")
            task = asyncio.create_task(connector.run())
            try:
                assert await asyncio.wait_for(complete, 8)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError): await task
    asyncio.run(scenario())
    assert service.messages(int(ids["alice"]), ids["conversation_id"])[-1]["segments"][0]["data"]["text"] == "Reply from fake OneBot peer"


def test_listener_starts_and_stops_without_robot_runtime(tmp_path):
    listener = ChatLifecycle(tmp_path, port=0, demo=True)
    try:
        listener.start()
        with httpx.Client(trust_env=False) as client:
            response = client.get(f"http://127.0.0.1:{listener.port}/api/v1/chat/health")
        assert response.status_code == 200 and response.json()["mode"] == "demo"
    finally:
        listener.close()
    assert not listener._thread.is_alive()


def test_unimplemented_features_fail_honestly(rig):
    _, _, client = rig
    headers = auth(client)
    assert client.post("/api/v1/chat/auth/refresh", headers=headers).status_code == 501
    assert client.get("/api/v1/chat/posts", headers=headers).status_code == 501


def test_ex_owns_optional_chat_listener_and_closes_it(tmp_path, monkeypatch):
    from astrbot_ex.core.api_server import build_server
    monkeypatch.setenv("ASTRBOTEX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASTREX_CHAT_ENABLED", "true")
    monkeypatch.setenv("ASTREX_CHAT_DEMO", "true")
    monkeypatch.setenv("ASTREX_CHAT_PORT", "0")
    monkeypatch.delenv("ASTREX_CHAT_ONEBOT_REVERSE_URL", raising=False)
    server = build_server("127.0.0.1", 0, 20)
    listener = server.chat_lifecycle
    try:
        assert server.interaction_core.chat_service is listener.service
        with httpx.Client(trust_env=False) as client:
            assert client.get(f"http://127.0.0.1:{listener.port}/api/v1/chat/health").status_code == 200
    finally:
        server.server_close()
    assert not listener._thread.is_alive()
    assert server.interaction_core.chat_service is None


def test_demo_web_cors_is_limited_and_absent_in_normal_mode(rig):
    service, _, client = rig
    path = "/api/v1/chat/auth/login"
    headers = {"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    assert client.options(path, headers=headers).headers["access-control-allow-origin"] == headers["Origin"]
    headers["Origin"] = "https://untrusted.example"
    assert client.options(path, headers=headers).status_code == 400
    with TestClient(create_app(service)) as normal:
        headers["Origin"] = "http://localhost:5173"
        assert "access-control-allow-origin" not in normal.options(path, headers=headers).headers
