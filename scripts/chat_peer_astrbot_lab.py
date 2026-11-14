"""Real astrC -> mailbox -> EX identity -> OneBot -> AstrBot -> encrypted reply."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Installed AstrBot image; never pulled')
    parser.add_argument('--flutter', required=True)
    parser.add_argument('--astrc-root', required=True, type=Path)
    parser.add_argument('--relay', help='Deployed HTTPS mailbox; otherwise starts an isolated loopback mailbox')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--media', action='store_true', help='Install a disposable synthetic probe and verify actual AstrBot media downloads/replies')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from astrbot_ex.core.chat.lifecycle import ChatLifecycle
    from astrbot_ex.core.chat.repository import ChatRepository
    from astrbot_ex.core.chat.service import ChatService
    def docker(*command):
        result = subprocess.run(['docker', *command], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
        if result.returncode:
            raise RuntimeError('Docker ' + command[0] + ' failed: ' + result.stderr[-1200:])
        return result.stdout.strip()
    name = 'astrc-peer-lab-' + uuid4().hex[:10]
    listener = relay_process = relay_log = None
    container_created = False
    with tempfile.TemporaryDirectory(prefix='astrc-peer-agent-') as directory:
        work = Path(directory)
        try:
            relay = args.relay
            if not relay:
                port = free_port()
                relay = f'http://127.0.0.1:{port}'
                relay_log = (work/'relay.log').open('w', encoding='utf-8')
                relay_process = subprocess.Popen([sys.executable,'-m','astrbot_ex.core.chat.peer_relay', '--data-root', str(work/'relay'),
                    '--port',str(port),'--allow-loopback'],cwd=root,stdout=relay_log,stderr=subprocess.STDOUT)
            with httpx.Client(timeout=5, trust_env=False) as client:
                deadline = time.monotonic()+20
                while True:
                    try:
                        health=client.get(relay+'/api/v1/peer/health'); health.raise_for_status()
                        assert health.json().get('friend_requests') is True
                        break
                    except (httpx.HTTPError, AssertionError):
                        if time.monotonic()>deadline: raise
                        time.sleep(.1)
            token = secrets.token_urlsafe(32)
            bot_root = work/'astrbot'
            (bot_root/'data').mkdir(parents=True)
            if args.media:
                shutil.copytree(root/'tests/chat/fixtures/media_probe', bot_root/'data/plugins/astrc_media_probe')
            config = {'platform':[{'id':'astrc_peer_acceptance','type':'aiocqhttp','enable':True,'ws_reverse_host':'0.0.0.0','ws_reverse_port':6199,'ws_reverse_token':token}],
                'platform_settings':{'enable_id_white_list':False,'reply_with_mention':True,'reply_with_quote':True},
                'dashboard':{'enable':False},'provider':[],'provider_sources':[],'provider_settings':{'enable':False},'disable_metrics':True,'t2i':False,'log_level':'INFO' if args.media else 'WARNING'}
            (bot_root/'data/cmd_config.json').write_text(json.dumps(config),encoding='utf-8')
            host_port=free_port()
            bootstrap='import asyncio; import main; main.check_env(); broker=main.LogBroker(); main.LogManager.set_queue_handler(main.logger,broker); loader=main.InitialLoader(main.db_helper,broker); asyncio.run(loader.start())'
            docker('run','--detach','--pull=never','--name',name,'--publish',f'127.0.0.1:{host_port}:6199','--env','ASTRBOT_ROOT=/lab',
                '--mount',f'type=bind,source={bot_root},target=/lab','--entrypoint','python',args.image,'-c',bootstrap)
            container_created=True
            repo=ChatRepository(work/'ex/chat/chat.sqlite3')
            service=ChatService(repo)
            bot=int(service.create_user('bot',secrets.token_urlsafe(32),'EX 联测 bot','bot')['user_id'])
            repo.close()
            # The local relay exception is explicit and exists only in this isolated test.
            listener=ChatLifecycle(work/'ex',host='0.0.0.0' if args.media else '127.0.0.1',port=0,bot_id=bot,reverse_url=f'ws://127.0.0.1:{host_port}/ws',bot_token=token)
            from astrbot_ex.core.chat.peer_account import PeerAccount
            account=PeerAccount(listener.service,bot,work/'ex',relay,'EX 联测 bot',auto_accept=True,allow_loopback=not args.relay)
            listener.peer_account=account
            listener.service.peer_account=account
            listener.start()
            if args.media:
                os.environ['ASTREX_CHAT_MEDIA_BASE_URL'] = f'http://host.docker.internal:{listener.port}'
            deadline=time.monotonic()+60
            while not (listener.connector.connected and account.online):
                if time.monotonic()>deadline:
                    raise RuntimeError('EX peer/OneBot readiness timed out: '+account.last_error)
                time.sleep(.25)
            public_card=work/'bot-card.json'
            public_card.write_text(json.dumps({'contact_code':account.identity.contact_code(relay)}),encoding='utf-8')
            env=dict(os.environ,ASTRC_PEER_BOT_CARD_FILE=str(public_card))
            subprocess.run([args.flutter,'test','--no-pub','test/peer_bot_roundtrip_test.dart',
                *(['test/peer_bot_media_roundtrip_test.dart'] if args.media else []),
                '--dart-define=ASTRC_PEER_TEST_RELAY_A='+relay,'--reporter=expanded'],cwd=args.astrc_root,env=env,check=True,timeout=240)
            report={'schema_version':1,'status':'passed','tested_at':datetime.now(timezone.utc).isoformat(),
                'transport':'public HTTPS mailbox' if args.relay else 'real isolated loopback mailbox',
                'astrbot_image':args.image,'protocol':'astrc-mailbox/1 + OneBot11',
                'scenarios':['astrC human creates identity','adds signed EX bot card in normal peer UI flow','signed friend request auto accepted by EX',
                    'encrypted human message through mailbox','EX decrypts and emits OneBot private event','real AstrBot /sid command',
                    'OneBot reply encrypted by EX and decrypted by Dart','human restart retains identity/contact/history','second real bot reply after restart'],
                'limitations':['Android UI and background not verified','No model provider or third-party plugin configured',
                    'Synthetic probe uses base64 file action; ordinary file:// plugin output unsupported' if args.media else 'Peer private text only tested in this run'],
                'source_sha256':{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in ['astrbot_ex/core/chat/peer_account.py','astrbot_ex/core/chat/peer_identity.py','astrbot_ex/core/chat/peer_relay.py']}}
            if args.media:
                report['scenarios'] += ['Dart encrypted PNG to actual AstrBot Image component', 'AstrBot original HTTP media fetch and base64 Image reply verified by identical digest',
                    'Dart binary attachment to actual AstrBot File.get_file', 'AstrBot base64 file action and identical Dart reply',
                    'Animated GIF through actual AstrBot and back without byte changes']
                report['limitations'].append('AstrBot normal static-image preprocessing may convert PNG to JPEG; probe fetches original event URL')
            if args.output:
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps({'status':'passed','scenarios':len(report['scenarios']),'transport':report['transport']}))
        except Exception:
            if container_created:
                result = subprocess.run(['docker', 'logs', '--tail', '120', name], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
                print(result.stdout + result.stderr, file=sys.stderr)
            raise
        finally:
            if listener: listener.close()
            if container_created: docker('rm','--force',name)
            if relay_process:
                relay_process.terminate()
                try: relay_process.wait(timeout=10)
                except subprocess.TimeoutExpired: relay_process.kill(); relay_process.wait()
            if relay_log: relay_log.close()


if __name__=='__main__': main()
