"""Real management HTTP must protect identity setup and persist the same account."""
import json
import os
import threading
from unittest.mock import patch

import httpx

from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.chat.lifecycle import ChatLifecycle


def test_management_setup_auth_persistence_and_invalid_contact(tmp_path):
    with patch.dict(os.environ, {'ASTRBOTEX_DATA_DIR': str(tmp_path), 'ASTREX_CHAT_ENABLED':'1',
            'ASTREX_CHAT_HOST':'127.0.0.1','ASTREX_CHAT_PORT':'0'}, clear=False):
        server=build_server('127.0.0.1',0,20)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        base=f'http://127.0.0.1:{server.server_port}/api/v1/ex/chat/account'
        headers={'Authorization':'Bearer '+server.decision_management.credential_path.read_text().strip()}
        with httpx.Client(trust_env=False,timeout=10) as client:
            assert client.get(base).status_code==401
            assert client.post(base,json={'name':'bot','relay':'https://relay.example','auto_accept':False}).status_code==401
            assert client.get(base,headers=headers).json()['enabled'] is False
            result=client.post(base,headers=headers,json={'name':'我的 bot','relay':'https://relay.example','auto_accept':False})
            assert result.status_code==200,result.text
            identity=result.json()['card']['id']
            assert result.json()['contact_code'].startswith('astrc:contact:')
            assert not result.json()['auto_accept'] and not result.json()['onebot_connected']
            result=client.post(base+'/contacts',headers=headers,json={'code':'invalid-contact'})
            assert result.status_code==400
            assert client.post(base,headers=headers,json={'name':'改名 bot','relay':'https://relay.example','auto_accept':True}).json()['card']['id']==identity
            assert client.post(base,headers=headers,json={'name':'bot','relay':'https://other.example','auto_accept':False}).status_code==409
            saved=json.loads(server.chat_lifecycle.peer_config.read_text())
            assert set(saved)=={'bot_id','name','relay','auto_accept'}
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    reopened=ChatLifecycle(tmp_path,port=0)
    try:
        assert reopened.peer_status()['card']['id']==identity
        assert reopened.peer_status()['card']['name']=='改名 bot'
        assert reopened.peer_status()['auto_accept'] is True
    finally:
        reopened.close()
