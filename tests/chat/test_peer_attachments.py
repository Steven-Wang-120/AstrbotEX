"""Rich bodies traverse relay handlers and actual OneBot/domain code."""
import asyncio
import base64
from io import BytesIO
import json

import httpx
from PIL import Image
import pytest

from astrbot_ex.core.chat.native_api import create_app
from astrbot_ex.core.chat.onebot11.actions import ActionDispatcher
from astrbot_ex.core.chat.peer_account import PeerAccount, PeerRelayClient
from astrbot_ex.core.chat.peer_content import attachment, validate_content
from astrbot_ex.core.chat.peer_identity import PeerIdentity
from astrbot_ex.core.chat.peer_relay import Mailbox, create_peer_app
from astrbot_ex.core.chat.repository import ChatRepository
from astrbot_ex.core.chat.service import ChatService

RELAY = 'http://127.0.0.1:8781'


def picture(animated=False):
    output = BytesIO()
    image = Image.new('RGB', (24, 16), '#438bdd')
    if animated:
        image.save(output, format='GIF', save_all=True, append_images=[Image.new('RGB', (24, 16), '#ffbb77')], duration=100, loop=0)
    else:
        image.save(output, format='PNG')
    return output.getvalue()


def test_content_rejects_tampering_paths_mime_and_combined_size(tmp_path):
    good = attachment('sticker', 'synthetic.gif', 'image/gif', picture(True))
    sender = PeerIdentity.load_or_create(tmp_path/'sender.json', 'sender')
    receiver = PeerIdentity.load_or_create(tmp_path/'receiver.json', 'receiver')
    body = {'text': 'emoji 😀🎉', 'attachments': [good]}
    packet = sender.seal_content(receiver.card(RELAY), body)
    assert receiver.open_content(sender.card(RELAY), packet) == body
    assert 'synthetic.gif' not in json.dumps(packet) and good['data'] not in json.dumps(packet)
    for changes in ({'name':'../escape.gif'}, {'sha256':'0'*64}, {'mime':'image/png'}, {'size':1}, {'kind':'html'}):
        with pytest.raises(ValueError):
            validate_content({'text':'', 'attachments':[{**good, **changes}]})
    big = attachment('file', 'large.bin', 'application/octet-stream', b'x' * (5*1024*1024))
    with pytest.raises(ValueError):
        validate_content({'text':'', 'attachments':[big, big]})


def test_peer_images_files_animated_stickers_onebot_replies_and_ack(tmp_path):
    async def run():
        store = Mailbox(tmp_path/'relay.sqlite3')
        app = create_peer_app(store, allow_loopback=True)
        repo = ChatRepository(tmp_path/'chat.sqlite3')
        service = ChatService(repo)
        bot = int(service.create_user('bot', 'synthetic-password', 'bot', 'bot')['user_id'])
        def client():
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        account = PeerAccount(service, bot, tmp_path, RELAY, 'bot', auto_accept=True, allow_loopback=True, client=client())
        service.peer_account = account
        human = PeerRelayClient(PeerIdentity.load_or_create(tmp_path/'human.json', 'human'), RELAY, allow_loopback=True, client=client())
        actions = ActionDispatcher(service, bot)
        try:
            await account.sync_once()
            await human.allow(account.identity.id)
            await human.request(RELAY, '/friend-requests', 'POST', human.identity.friend_request(RELAY, account.identity.id))
            await account.sync_once()
            uid = account.contacts()[0]['user_id']
            values = [attachment('image','test.png','image/png',picture()),
                      attachment('file','notes.txt','application/octet-stream',b'Synthetic private file\x00\xff'),
                      attachment('sticker','wave.gif','image/gif',picture(True))]
            packets = []
            for value in values:
                packet = human.identity.seal_content(account.identity.card(RELAY), {'text':'', 'attachments':[value]})
                packets.append(packet)
                await human.request(RELAY, '/envelopes', 'POST', packet)
            assert 'notes.txt' not in ''.join(r[0] for r in store.db.execute('SELECT packet FROM envelopes'))
            await account.sync_once()
            assert account.online, account.last_error
            events = [actions.event(e) for e in service.sync(bot)['events'] if e['type']=='message.created']
            assert [e['message'][0]['type'] for e in events] == ['image','file','image']
            assert events[-1]['message'][0]['data']['sub_type']=='1'
            history = account.history(human.identity.id)
            assert [m['created_at'] for m in history['items']]==[p['created_at'] for p in packets]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service))) as api:
                for event, original in zip(events, values):
                    url=event['message'][0]['data']['url']
                    response=await api.get(url)
                    assert response.status_code==200 and response.content==base64.b64decode(original['data'])
                    assert (await api.get(url[:-64] + '0' * 64)).status_code==403
            for packet in packets:
                await human.request(RELAY,'/envelopes','POST',packet)
            await account.sync_once()
            assert len(account.history(human.identity.id)['items'])==3
            for value in values:
                kind = 'file' if value['kind']=='file' else 'image'
                result=actions.dispatch({'action':'send_private_msg','params':{'user_id':uid,'message':[{'type':kind,'data':{'file':'base64://'+value['data'],'name':value['name'],'sub_type':'1' if value['kind']=='sticker' else '0'}}]}})
                assert result['status']=='ok',result
            await account.sync_once()
            assert account.online, account.last_error
            received=(await human.request(RELAY,'/envelopes'))['items']
            assert len(received)==3
            for packet, value in zip(received, values):
                decoded=human.identity.open_content(account.identity.card(RELAY),packet)
                assert decoded['attachments'][0]['sha256']==value['sha256']
                assert decoded['attachments'][0]['kind']==value['kind']
            account.favorite_sticker(values[-1]); account.favorite_sticker(values[-1])
            assert len(account.stickers()['items'])==1
            account.remove_sticker(values[-1]['sha256'])
            assert account.stickers()['items']==[]
        finally:
            await account.client.close(); await human.close(); repo.close(); store.close()
    asyncio.run(run())


def test_large_encrypted_file_and_mailbox_restart(tmp_path):
    from fastapi.testclient import TestClient
    sender = PeerIdentity.load_or_create(tmp_path/'sender.json','sender')
    receiver = PeerIdentity.load_or_create(tmp_path/'receiver.json','receiver')
    def login(client, person):
        card=person.card(RELAY)
        challenge=client.post('/api/v1/peer/challenge',json={'id':person.id}).json()
        proof=f"astrc-relay-auth-v1\n{challenge['challenge_id']}\n{challenge['challenge']}\n{person.id}".encode()
        result=client.post('/api/v1/peer/session',json={'card':card,'challenge_id':challenge['challenge_id'],'signature':person.sign(proof)})
        assert result.status_code==200
        return {'Authorization':'Bearer '+result.json()['access_token']}
    value=attachment('file','synthetic.bin','application/octet-stream',b'01234567'*(512*1024))
    packet=sender.seal_content(receiver.card(RELAY),{'text':'large', 'attachments':[value]})
    path=tmp_path/'mailbox.sqlite3'
    with TestClient(create_peer_app(Mailbox(path),allow_loopback=True)) as client:
        a,b=login(client,sender),login(client,receiver)
        client.put('/api/v1/peer/allowed/'+sender.id,headers=b)
        assert client.get('/api/v1/peer/health').json()['media'] is True
        assert client.post('/api/v1/peer/envelopes',json=packet,headers=a).status_code==201
    with TestClient(create_peer_app(Mailbox(path),allow_loopback=True)) as client:
        b=login(client,receiver)
        received=client.get('/api/v1/peer/envelopes',headers=b).json()['items'][0]
        assert receiver.open_content(sender.card(RELAY),received)['attachments'][0]==value
        assert client.post('/api/v1/peer/ack',json={'items':[{'sender':sender.id,'id':packet['id']}]},headers=b).status_code==200
        assert client.get('/api/v1/peer/envelopes',headers=b).json()['items']==[]
