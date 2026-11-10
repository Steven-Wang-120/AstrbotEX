from uuid import uuid4

import pytest

from astrbot_ex.core.chat.models import ChatError
from astrbot_ex.core.chat.onebot11.actions import ActionDispatcher
from test_chat_skeleton import auth, body, rig


def segment(kind, value):
    key = {"text": "text", "mention": "user_id", "reply": "message_id"}[kind]
    return {"type": kind, "data": {key: value}}


def test_native_mentions_replies_and_idempotent_retry_after_recall(rig):
    service, ids, client = rig
    url = f"/api/v1/chat/conversations/{ids['conversation_id']}/messages"
    alice, bob = auth(client), auth(client, "bob")
    original = client.post(url, headers=alice, json=body("Quoted")).json()
    segments = [segment("reply", original["message_id"]), segment("mention", ids["bot"]), segment("text", " /sid")]
    payload = {"client_message_id": str(uuid4()), "segments": segments}
    response = client.post(url, headers=bob, json=payload)
    assert response.status_code == 201 and response.json()["segments"] == segments
    client.post(f"/api/v1/chat/messages/{original['message_id']}/recall", headers=alice).raise_for_status()
    assert client.post(url, headers=bob, json=payload).json() == response.json()
    payload["client_message_id"] = str(uuid4())
    assert client.post(url, headers=bob, json=payload).json()["error"]["code"] == "invalid_reply"
    assert service.messages(int(ids["alice"]), ids["conversation_id"])[-1]["segments"] == segments


def test_mention_and_reply_permissions_are_transactional(rig):
    service, ids, client = rig
    alice, bob = int(ids["alice"]), int(ids["bob"])
    cid = ids["conversation_id"]
    outsider = service.create_user("outsider", "test-password", "Outsider")
    direct = service.direct(alice, ids["bob"])
    hidden = service.send(alice, direct["conversation_id"], str(uuid4()), [segment("text", "private")])
    before = service.sync(bob)["next_cursor"]
    count = len(service.messages(bob, cid))
    denied = [
        (bob, cid, [segment("mention", "all")], 403),
        (alice, cid, [segment("mention", outsider["user_id"])], 400),
        (bob, cid, [segment("reply", hidden["message_id"]), segment("text", "no")], 400),
        (alice, cid, [segment("reply", "2147483647"), segment("text", "no")], 400),
        (alice, direct["conversation_id"], [segment("mention", ids["bob"])], 400),
        (int(outsider["user_id"]), cid, [segment("mention", ids["bot"])], 403),
    ]
    for uid, room, segments, status in denied:
        with pytest.raises(ChatError) as error:
            service.send(uid, room, str(uuid4()), segments)
        assert error.value.status == status
    assert len(service.messages(bob, cid)) == count
    assert service.sync(bob, before)["events"] == []
    assert service.send(alice, cid, str(uuid4()), [segment("mention", "all")])["segments"] == [segment("mention", "all")]


@pytest.mark.parametrize("segments", [
    [segment("reply", "1")],
    [segment("text", "x"), segment("reply", "1")],
    [segment("reply", "1"), segment("reply", "1"), segment("text", "x")],
    [segment("mention", "01")], [segment("mention", "2147483648")],
    [segment("reply", 1), segment("text", "x")],
    [{"type": "mention", "data": {"user_id": "1", "name": "forged"}}],
    [{"type": "image", "data": {"media_id": str(uuid4())}}],
    [segment("text", "x" * 2001), segment("text", "x" * 2000)],
    [segment("mention", "1")] * 17,
])
def test_invalid_native_segments_do_not_write(rig, segments):
    service, ids, client = rig
    url = f"/api/v1/chat/conversations/{ids['conversation_id']}/messages"
    response = client.post(url, headers=auth(client), json={"client_message_id": str(uuid4()), "segments": segments})
    assert response.status_code == 400
    assert len(service.messages(int(ids["alice"]), ids["conversation_id"])) == 1


def test_onebot_bidirectional_mapping_get_msg_and_visibility(rig):
    service, ids, _ = rig
    dispatcher = ActionDispatcher(service, int(ids["bot"]))
    cid = ids["conversation_id"]
    original = service.messages(int(ids["alice"]), cid)[0]
    segments = [segment("reply", original["message_id"]), segment("mention", ids["bot"]), segment("text", "&[CQ:at,qq=all]")]
    native = service.send(int(ids["alice"]), cid, str(uuid4()), segments)
    event = next(e for e in service.sync(int(ids["bot"]))["events"] if e.get("payload", {}).get("message", {}).get("message_id") == native["message_id"])
    ob = dispatcher.event(event)
    expected = [{"type": "reply", "data": {"id": original["message_id"]}}, {"type": "at", "data": {"qq": ids["bot"]}}, segments[-1]]
    assert ob["message"] == expected
    assert ob["raw_message"] == f"[CQ:reply,id={original['message_id']}][CQ:at,qq={ids['bot']}]&amp;&#91;CQ:at,qq=all&#93;"
    fetched = dispatcher.dispatch({"action": "get_msg", "params": {"message_id": int(native["message_id"])}})
    assert fetched["status"] == "ok"
    assert fetched["data"]["message"] == expected
    assert fetched["data"]["self_id"] == int(ids["bot"])
    assert fetched["data"]["group_id"] == 1 and fetched["data"]["user_id"] == int(ids["alice"])
    result = dispatcher.dispatch({"action": "send_group_msg", "params": {"group_id": 1, "message": [
        {"type": "reply", "data": {"id": int(native["message_id"]) }},
        {"type": "at", "data": {"qq": int(ids["alice"]) }},
        segment("text", "hello"),
    ]}})
    assert result["status"] == "ok"
    assert service.get_message(int(ids["alice"]), str(result["data"]["message_id"]))["segments"] == [segment("reply", native["message_id"]), segment("mention", ids["alice"]), segment("text", "hello")]
    assert dispatcher.dispatch({"action": "get_stranger_info", "params": {"user_id": ids["alice"]}})["data"]["nickname"] == "Alice"
    outsider = service.create_user("hidden", "test-password", "Hidden")
    assert dispatcher.dispatch({"action": "get_stranger_info", "params": {"user_id": outsider["user_id"]}})["retcode"] == 1403
    private = service.direct(int(ids["alice"]), ids["bob"])
    hidden = service.send(int(ids["alice"]), private["conversation_id"], str(uuid4()), [segment("text", "hidden")])
    assert dispatcher.dispatch({"action": "get_msg", "params": {"message_id": hidden["message_id"]}})["retcode"] == 1403
    service.recall(int(ids["alice"]), native["message_id"])
    assert dispatcher.dispatch({"action": "get_msg", "params": {"message_id": native["message_id"]}})["data"]["message"] == []


@pytest.mark.parametrize("message", [
    [{"type": "at", "data": {"qq": "all"}}],
    [{"type": "mention", "data": {"user_id": "1"}}],
    [{"type": "at", "data": {"qq": True}}],
    [{"type": "reply", "data": {"id": "1", "text": "forged"}}],
    "[CQ:at,qq=1]",
])
def test_onebot_rejects_unauthorized_or_unsupported_segments(rig, message):
    service, ids, _ = rig
    result = ActionDispatcher(service, int(ids["bot"])).dispatch({"action": "send_group_msg", "params": {"group_id": 1, "message": message}})
    assert result["status"] == "failed"
    assert len(service.messages(int(ids["alice"]), ids["conversation_id"])) == 1
