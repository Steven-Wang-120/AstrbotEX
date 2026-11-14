import base64
import hashlib
import json
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient
import pytest

from astrbot_ex.core.chat.peer_relay import Mailbox, canonical, create_peer_app


def b64(value):
    return base64.b64encode(value).decode()


def identity(name="Synthetic test user"):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    card = {"v": 1, "id": hashlib.sha256(public).hexdigest(), "name": name,
            "sign_key": b64(public), "box_key": b64(X25519PrivateKey.generate().public_key().public_bytes_raw()), "relay": "https://relay.example"}
    card["signature"] = b64(private.sign(canonical(card)))
    return private, card


def authenticate(client, person):
    private, card = person
    challenge = client.post('/api/v1/peer/challenge', json={"id": card["id"]}).json()
    proof = f"astrc-relay-auth-v1\n{challenge['challenge_id']}\n{challenge['challenge']}\n{card['id']}".encode()
    request = {"card": card, "challenge_id": challenge['challenge_id'], "signature": b64(private.sign(proof))}
    response = client.post('/api/v1/peer/session', json=request)
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}, request


def packet(person, recipient):
    private, card = person
    data = {"v": 1, "id": str(uuid4()), "sender": card['id'], "recipient": recipient['id'],
            "created_at": "2026-11-11T00:00:00Z", "ephemeral": b64(bytes(32)), "nonce": b64(bytes(12)),
            "ciphertext": b64(b'opaque-ciphertext'), "mac": b64(bytes(16))}
    data['signature'] = b64(private.sign(canonical(data)))
    return data


@pytest.fixture
def rig(tmp_path):
    clock = [1000.0]
    store = Mailbox(tmp_path/'relay.sqlite3', clock=lambda: clock[0])
    with TestClient(create_peer_app(store)) as client:
        yield store, client, clock


def test_identity_proof_and_single_use_challenge(rig):
    _, client, _ = rig
    person = identity()
    headers, request = authenticate(client, person)
    assert client.post('/api/v1/peer/session', json=request).status_code == 401
    assert client.get('/api/v1/peer/envelopes', headers=headers).status_code == 200
    assert client.get('/api/v1/peer/envelopes').status_code == 401
    assert client.get('/api/v1/peer/envelopes', headers={'Authorization':'Bearer fake'}).status_code == 401


def test_contact_consent_offline_delivery_ack_and_retry(rig):
    store, client, _ = rig
    a, b, outsider = identity('甲'), identity('乙'), identity('丙')
    ah, _ = authenticate(client, a)
    bh, _ = authenticate(client, b)
    ch, _ = authenticate(client, outsider)
    item = packet(a, b[1])
    assert client.post('/api/v1/peer/envelopes', json=item, headers=ah).status_code == 403
    assert client.put('/api/v1/peer/allowed/'+a[1]['id'], headers=bh).status_code == 200
    for _ in range(2):
        assert client.post('/api/v1/peer/envelopes', json=item, headers=ah).status_code == 201
    assert store.db.execute('SELECT COUNT(*) FROM envelopes').fetchone()[0] == 1
    assert client.get('/api/v1/peer/envelopes', headers=ah).json()['items'] == []
    assert client.get('/api/v1/peer/envelopes', headers=ch).json()['items'] == []
    ack = {'items':[{'sender':a[1]['id'],'id':item['id']}]}
    client.post('/api/v1/peer/ack', json=ack, headers=ch)
    assert client.get('/api/v1/peer/envelopes', headers=bh).json()['items'] == [item]
    assert client.post('/api/v1/peer/ack', json=ack, headers=bh).status_code == 200
    assert client.get('/api/v1/peer/envelopes', headers=bh).json()['items'] == []
    assert client.post('/api/v1/peer/envelopes', json=item, headers=ah).status_code == 201
    assert client.get('/api/v1/peer/envelopes', headers=bh).json()['items'] == []
    assert store.db.execute('SELECT packet FROM envelopes').fetchone()[0] == ''


def test_global_pending_quota_releases_on_ack_and_inbox_is_bounded(rig, monkeypatch):
    from astrbot_ex.core.chat import peer_relay

    store, client, clock = rig
    a, b = identity('quota sender'), identity('quota recipient')
    ah, _ = authenticate(client, a)
    bh, _ = authenticate(client, b)
    client.put('/api/v1/peer/allowed/' + a[1]['id'], headers=bh)
    first, second, third = [packet(a, b[1]) for _ in range(3)]
    size = len(canonical(first))
    store.global_quota = size + 10
    assert client.post('/api/v1/peer/envelopes', json=first, headers=ah).status_code == 201
    assert client.post('/api/v1/peer/envelopes', json=second, headers=ah).status_code == 503
    client.post('/api/v1/peer/ack', json={'items':[{'sender':a[1]['id'], 'id':first['id']}]}, headers=bh)
    assert client.post('/api/v1/peer/envelopes', json=second, headers=ah).status_code == 201
    store.global_quota = 512 * 1024 * 1024
    clock[0] += 1  # Distinct arrivals; equal timestamps are ordered by UUID.
    assert client.post('/api/v1/peer/envelopes', json=third, headers=ah).status_code == 201
    monkeypatch.setattr(peer_relay, 'MAX_PACKET_BYTES', size * 2 - 1)
    inbox = client.get('/api/v1/peer/envelopes', headers=bh).json()['items']
    assert inbox == [second]
    client.post('/api/v1/peer/ack', json={'items':[{'sender':a[1]['id'], 'id':second['id']}]}, headers=bh)
    assert client.get('/api/v1/peer/envelopes', headers=bh).json()['items'] == [third]


def test_packet_tampering_impersonation_and_id_conflict(rig):
    _, client, _ = rig
    a, b = identity(), identity()
    ah, _ = authenticate(client, a)
    bh, _ = authenticate(client, b)
    client.put('/api/v1/peer/allowed/'+a[1]['id'], headers=bh)
    original = packet(a, b[1])
    tampered = {**original, 'ciphertext':b64(b'changed')}
    assert client.post('/api/v1/peer/envelopes', json=tampered, headers=ah).status_code == 400
    assert client.post('/api/v1/peer/envelopes', json=original, headers=bh).status_code == 400
    assert client.post('/api/v1/peer/envelopes', json=original, headers=ah).status_code == 201
    tampered['signature'] = b64(a[0].sign(canonical({k:v for k,v in tampered.items() if k!='signature'})))
    assert client.post('/api/v1/peer/envelopes', json=tampered, headers=ah).status_code == 409


def test_expiry_quota_and_rate_limits(rig):
    store, client, clock = rig
    a,b = identity(),identity()
    ah,_ = authenticate(client,a); bh,_=authenticate(client,b)
    client.put('/api/v1/peer/allowed/'+a[1]['id'],headers=bh)
    store.quota=1
    assert client.post('/api/v1/peer/envelopes',json=packet(a,b[1]),headers=ah).status_code==413
    store.quota=100000
    assert client.post('/api/v1/peer/envelopes',json=packet(a,b[1]),headers=ah).status_code==201
    clock[0]+=7*86400+1
    bh,_=authenticate(client,b)
    assert client.get('/api/v1/peer/envelopes',headers=bh).json()['items']==[]
    for _ in range(30):
        client.post('/api/v1/peer/challenge',json={'id':a[1]['id']})
    assert client.post('/api/v1/peer/challenge',json={'id':a[1]['id']}).status_code==429


@pytest.mark.parametrize('value',[{},[],{'card':None},{'card':{'v':1}}, {'id':[]}, {'id':'bad'}])
def test_malformed_public_requests_are_controlled(rig,value):
    _,client,_=rig
    assert client.post('/api/v1/peer/challenge',json=value).status_code==400
    assert client.post('/api/v1/peer/session',json=value).status_code==400


def test_body_bounds_and_bad_card(rig):
    _,client,_=rig
    assert client.post('/api/v1/peer/session',content=b'x'*32769).status_code==413
    private,card=identity()
    card['name']='tampered'
    assert client.post('/api/v1/peer/session',json={'card':card}).status_code==400
    card['relay']=[]
    assert client.post('/api/v1/peer/session',json={'card':card}).status_code==400


def test_mailboxes_and_consent_survive_restart(tmp_path):
    path=tmp_path/'relay.sqlite3'
    a,b=identity(),identity()
    item=packet(a,b[1])
    store=Mailbox(path)
    with TestClient(create_peer_app(store)) as client:
        ah,_=authenticate(client,a);bh,_=authenticate(client,b)
        client.put('/api/v1/peer/allowed/'+a[1]['id'],headers=bh)
        client.post('/api/v1/peer/envelopes',json=item,headers=ah)
    reopened=Mailbox(path)
    with TestClient(create_peer_app(reopened)) as client:
        bh,_=authenticate(client,b)
        assert client.get('/api/v1/peer/envelopes',headers=bh).json()['items']==[item]


def test_existing_contact_remains_idempotent_at_capacity(rig):
    store, client, _ = rig
    owner = identity()
    headers, _ = authenticate(client, owner)
    with store.lock:
        store.db.executemany('INSERT INTO allowed VALUES (?,?)', [(owner[1]['id'], f'{i:064x}') for i in range(1000)])
    assert client.put('/api/v1/peer/allowed/'+f'{0:064x}', headers=headers).status_code == 200
    assert client.put('/api/v1/peer/allowed/'+f'{1001:064x}', headers=headers).status_code == 413
