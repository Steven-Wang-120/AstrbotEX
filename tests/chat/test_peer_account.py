"""Same mailbox protocol for astrC humans and an EX-hosted OneBot identity."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from astrbot_ex.core.chat.peer_account import PeerAccount, PeerRelayClient, RelayError
from astrbot_ex.core.chat.peer_identity import PeerIdentity
from astrbot_ex.core.chat.peer_relay import Mailbox, create_peer_app
from astrbot_ex.core.chat.repository import ChatRepository
from astrbot_ex.core.chat.service import ChatService
from astrbot_ex.core.chat.onebot11.actions import ActionDispatcher

RELAY = 'http://127.0.0.1:8781'


def setup(tmp_path, auto_accept=True):
    store = Mailbox(tmp_path/'relay.sqlite3')
    app = create_peer_app(store, allow_loopback=True)
    repo = ChatRepository(tmp_path/'chat.sqlite3')
    service = ChatService(repo)
    bot = int(service.create_user('bot', 'synthetic-bot-password', 'EX 测试 bot', 'bot')['user_id'])
    def client():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), follow_redirects=False)
    account = PeerAccount(service, bot, tmp_path, RELAY, 'EX 测试 bot', auto_accept=auto_accept, allow_loopback=True, client=client())
    service.peer_account = account
    alice = PeerIdentity.load_or_create(tmp_path/'alice.json', '真人甲')
    human = PeerRelayClient(alice, RELAY, allow_loopback=True, client=client())
    return store, repo, service, account, human, client


def test_signed_friend_request_permissions_and_resolution(tmp_path):
    async def run():
        store, repo, service, account, human, client = setup(tmp_path)
        try:
            await account.sync_once()
            request = human.identity.friend_request(RELAY, account.identity.id)
            response = await human.request(RELAY, '/friend-requests', 'POST', request)
            assert response['state'] == 'pending'
            assert await human.request(RELAY, '/friend-requests', 'POST', request) == response
            # A signed request is bound to its receiver and comment.
            changed = {**request, 'comment': 'tampered'}
            with pytest.raises(RelayError) as exc:
                await human.request(RELAY, '/friend-requests', 'POST', changed)
            assert exc.value.status == 400
            other = PeerRelayClient(PeerIdentity.load_or_create(tmp_path/'other.json', '第三者'), RELAY, allow_loopback=True, client=client())
            try:
                with pytest.raises(RelayError) as exc:
                    await other.request(RELAY, '/friend-requests/'+request['id']+'/resolve', 'POST', {'sender': human.identity.id, 'approve': True})
                assert exc.value.status == 404
            finally:
                await other.close()
            await account.sync_once()
            assert account.online, account.last_error
            assert account.contacts()[0]['state'] == 'accepted'
            assert (await human.request(RELAY, '/friend-requests', 'POST', request))['state'] == 'approved'
            # Replay with different, correctly signed contents is an ID conflict.
            changed['signature'] = human.identity.sign(__import__('astrbot_ex.core.chat.peer_relay', fromlist=['canonical']).canonical({k:v for k,v in changed.items() if k!='signature'}))
            with pytest.raises(RelayError) as exc:
                await human.request(RELAY, '/friend-requests', 'POST', changed)
            assert exc.value.status == 409
        finally:
            await account.client.close(); await human.close(); repo.close(); store.close()
    asyncio.run(run())


def test_peer_to_onebot_and_back_fixed_outbox_and_restart(tmp_path):
    async def run():
        store, repo, service, account, human, client = setup(tmp_path)
        try:
            await account.sync_once()
            await human.allow(account.identity.id)
            await human.request(RELAY, '/friend-requests', 'POST', human.identity.friend_request(RELAY, account.identity.id))
            await account.sync_once()
            incoming = human.identity.seal(account.identity.card(RELAY), '/sid')
            await human.request(RELAY, '/envelopes', 'POST', incoming)
            original_request = account.client.request
            async def lose_ack(origin, path, method='GET', data=None):
                if path == '/ack':
                    raise RelayError(503, 'synthetic_lost_ack')
                return await original_request(origin, path, method, data)
            account.client.request = lose_ack
            await account.sync_once()
            assert not account.online
            uid = account.contacts()[0]['user_id']
            conversation = service.direct(account.bot_id, str(uid))
            first = service.messages(account.bot_id, conversation['conversation_id'])
            assert len(first) == 1
            old_id = account.identity.id
            await account.client.close()
            account = PeerAccount(service, account.bot_id, tmp_path, RELAY, 'EX 测试 bot', auto_accept=True, allow_loopback=True, client=client())
            service.peer_account = account
            assert account.identity.id == old_id
            await account.sync_once()
            assert len(service.messages(account.bot_id, conversation['conversation_id'])) == 1
            actions = ActionDispatcher(service, account.bot_id)
            events = [actions.event(e) for e in service.sync(account.bot_id)['events']]
            message = next(e for e in events if e and e['post_type']=='message')
            assert message['message_type']=='private' and message['user_id']==uid and message['raw_message']=='/sid'
            result = actions.dispatch({'action':'send_private_msg','params':{'user_id':uid,'message':[
                {'type':'reply','data':{'id':str(message['message_id'])}}, {'type':'text','data':{'text':'你好，来自 EX 的真实协议回复'}}]},'echo':{'opaque':5}})
            assert result['status']=='ok' and result['echo']=={'opaque':5}
            original_request = account.client.request
            packets = []
            async def lose_send_response(origin, path, method='GET', data=None):
                result = await original_request(origin, path, method, data)
                if path == '/envelopes' and method == 'POST':
                    packets.append(json.dumps(data, sort_keys=True))
                    if len(packets)==1:
                        raise RelayError(503, 'synthetic_lost_send_response')
                return result
            account.client.request = lose_send_response
            await account.sync_once()
            await account.sync_once()
            assert len(packets)==2 and packets[0]==packets[1]
            inbox = await human.request(RELAY, '/envelopes')
            assert len(inbox['items'])==1
            assert human.identity.open(account.identity.card(RELAY), inbox['items'][0]) == '你好，来自 EX 的真实协议回复'
            assert actions.dispatch({'action':'delete_msg','params':{'message_id':result['data']['message_id']}})['status']=='failed'
        finally:
            await account.client.close(); await human.close(); repo.close(); store.close()
    asyncio.run(run())


def test_manual_onebot_friend_request_approval_and_rejection(tmp_path):
    async def run():
        store, repo, service, account, human, client = setup(tmp_path, auto_accept=False)
        try:
            await account.sync_once()
            request = human.identity.friend_request(RELAY, account.identity.id, '想加 bot 为好友')
            await human.request(RELAY, '/friend-requests', 'POST', request)
            await account.sync_once()
            actions = ActionDispatcher(service, account.bot_id)
            event = next(v for e in service.sync(account.bot_id)['events'] if (v:=actions.event(e)) and v['post_type']=='request')
            assert event['request_type']=='friend' and event['comment']=='想加 bot 为好友'
            rejected_send = actions.dispatch({'action':'send_private_msg','params':{'user_id':event['user_id'],'message':'pending'}})
            assert rejected_send['retcode']==1403
            for approve in [False, True]:
                # First request is rejected; a new signed request may later be accepted.
                if approve:
                    request = human.identity.friend_request(RELAY, account.identity.id)
                    await human.request(RELAY, '/friend-requests', 'POST', request)
                    await account.sync_once()
                    event = [v for e in service.sync(account.bot_id)['events'] if (v:=actions.event(e)) and v['post_type']=='request'][-1]
                result = actions.dispatch({'action':'set_friend_add_request','params':{'flag':event['flag'],'approve':str(approve).lower()},'echo':'e'})
                assert result['status']=='ok' and result['echo']=='e'
                await account.sync_once()
                assert (await human.request(RELAY, '/friend-requests', 'POST', request))['state'] == ('approved' if approve else 'rejected')
            assert len(actions.dispatch({'action':'get_friend_list'})['data'])==1
        finally:
            await account.client.close(); await human.close(); repo.close(); store.close()
    asyncio.run(run())


def test_ex_can_initiate_a_friend_request_and_retain_fixed_request_after_reopen(tmp_path):
    async def run():
        store, repo, service, account, human, client = setup(tmp_path)
        try:
            await human.authenticate(RELAY)
            account.add_contact(human.identity.contact_code(RELAY))
            await account.sync_once()
            request = (await human.request(RELAY, '/friend-requests'))['items'][0]
            assert request['card']['id'] == account.identity.id
            assert request['recipient'] == human.identity.id
            uid = account.contacts()[0]['user_id']
            actions = ActionDispatcher(service, account.bot_id)
            result = actions.dispatch({'action':'send_private_msg','params':{'user_id':uid,'message':'主动问候'}})
            assert result['status']=='ok'
            await account.sync_once()
            assert not account.online  # The human has not yet approved incoming messages.
            with repo.transaction() as db:
                fixed = db.execute('SELECT packet FROM peer_outbox').fetchone()[0]
            await account.client.close()
            account = PeerAccount(service, account.bot_id, tmp_path, RELAY, 'EX 测试 bot', allow_loopback=True, client=client())
            service.peer_account = account
            await human.request(RELAY, '/friend-requests/'+request['id']+'/resolve', 'POST', {'sender':account.identity.id,'approve':True})
            await account.sync_once()
            packet = (await human.request(RELAY, '/envelopes'))['items'][0]
            from astrbot_ex.core.chat.peer_relay import canonical
            assert canonical(packet).decode()==fixed
            assert human.identity.open(account.identity.card(RELAY),packet)=='主动问候'
        finally:
            await account.client.close(); await human.close(); repo.close(); store.close()
    asyncio.run(run())


def test_identity_rejects_noncanonical_headers_and_oversized_ciphertext(tmp_path):
    from astrbot_ex.core.chat.peer_identity import b64
    from astrbot_ex.core.chat.peer_relay import canonical
    sender = PeerIdentity.load_or_create(tmp_path/'sender.json', 'sender')
    receiver = PeerIdentity.load_or_create(tmp_path/'receiver.json', 'receiver')
    card = sender.card(RELAY)
    packet = sender.seal(receiver.card(RELAY), 'hello')
    assert receiver.open(card, packet) == 'hello'
    for changes in ({'id': 'not-a-uuid'}, {'v': True}, {'created_at': ''},
                    {'ciphertext': b64(b'x' * 16385)}):
        invalid = {**packet, **changes}
        invalid['signature'] = sender.sign(canonical({k:v for k,v in invalid.items() if k != 'signature'}))
        with pytest.raises(ValueError):
            receiver.open(card, invalid)
