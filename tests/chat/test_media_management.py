import base64
import os
import threading
from unittest.mock import patch

import httpx

from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.chat.peer_content import attachment
from astrbot_ex.core.chat.peer_identity import PeerIdentity


def test_management_conversation_attachments_favorites_and_auth(tmp_path):
    with patch.dict(os.environ, {'ASTRBOTEX_DATA_DIR':str(tmp_path),'ASTREX_CHAT_ENABLED':'1','ASTREX_CHAT_HOST':'127.0.0.1','ASTREX_CHAT_PORT':'0'}):
        server=build_server('127.0.0.1',0,20)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        base=f'http://127.0.0.1:{server.server_port}/api/v1/ex/chat/account'
        headers={'Authorization':'Bearer '+server.decision_management.credential_path.read_text().strip()}
        with httpx.Client(trust_env=False,timeout=10) as client:
            assert client.post(base,headers=headers,json={'name':'media bot','relay':'https://relay.example','auto_accept':False}).status_code==200
            peer=PeerIdentity.load_or_create(tmp_path/'synthetic-peer.json','synthetic peer')
            server.chat_lifecycle.peer_account.add_contact(peer.contact_code('https://relay.example'))
            assert client.get(base+'/messages/'+peer.id).status_code==401
            body={'kind':'file','name':'synthetic.txt','mime':'application/octet-stream','data':base64.b64encode(b'synthetic private document').decode()}
            prepared=client.post(base+'/attachments/prepare',headers=headers,json=body)
            assert prepared.status_code==200,prepared.text
            request={'peer_id':peer.id,'client_message_id':'11111111-1111-4111-8111-111111111111','content':{'text':'emoji 😀','attachments':[prepared.json()]}}
            assert client.post(base+'/messages',headers=headers,json=request).status_code==200
            assert client.post(base+'/messages',headers=headers,json=request).status_code==200
            history=client.get(base+'/messages/'+peer.id,headers=headers).json()
            assert len(history['items'])==1
            mid=history['items'][0]['segments'][1]['data']['media_id']
            assert history['items'][0]['delivery']=='queued'
            assert client.get(base+'/media/'+mid).status_code==401
            assert client.get(base+'/media/'+mid,headers=headers).json()['data']==body['data']
            from io import BytesIO
            from PIL import Image
            out=BytesIO();Image.new('RGB',(2,2),'blue').save(out,format='PNG')
            sticker=attachment('sticker','saved.png','image/png',out.getvalue())
            assert client.post(base+'/stickers',headers=headers,json={'attachment':sticker}).status_code==200
            assert client.get(base+'/stickers',headers=headers).json()['items']==[sticker]
            assert client.post(base+'/stickers/remove',headers=headers,json={'sha256':sticker['sha256']}).status_code==200
            assert client.get(base+'/stickers',headers=headers).json()['items']==[]
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
