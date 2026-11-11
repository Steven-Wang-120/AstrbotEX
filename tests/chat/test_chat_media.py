from io import BytesIO
import json
from uuid import uuid4

from PIL import Image, PngImagePlugin
import pytest

from astrbot_ex.core.chat.media import MAX_IMAGE_BYTES
from astrbot_ex.core.chat.models import ChatError
from astrbot_ex.core.chat.onebot11.actions import ActionDispatcher
from astrbot_ex.core.chat.repository import ChatRepository
from astrbot_ex.core.chat.service import ChatService
from test_chat_skeleton import auth, rig


def picture(color='red', format='PNG', size=(12, 8), metadata=True):
    output = BytesIO()
    image = Image.new('RGB', size, color)
    info = PngImagePlugin.PngInfo()
    info.add_text('Comment', 'private-metadata-must-disappear')
    image.save(output, format=format, **({'pnginfo': info} if format == 'PNG' and metadata else {}))
    return output.getvalue()


def upload(client, headers, cid, content=None, key=None):
    return client.post('/api/v1/chat/media', params={'conversation_id': cid, 'client_media_id': key or str(uuid4())},
                       content=content if content is not None else picture(), headers={**headers, 'Content-Type': 'application/octet-stream'})


def send_image(client, headers, cid, mid, key=None):
    return client.post(f'/api/v1/chat/conversations/{cid}/messages', headers=headers, json={
        'client_message_id': key or str(uuid4()), 'segments': [{'type': 'image', 'data': {'media_id': mid}}],
    })


def test_native_image_upload_is_normalized_scoped_and_visible_only_after_send(rig):
    service, ids, client = rig
    alice, bob = auth(client), auth(client, 'bob')
    cid = ids['conversation_id']
    response = upload(client, alice, cid)
    assert response.status_code == 201
    media = response.json()
    assert media['width'] == 12 and media['height'] == 8 and media['content_type'] == 'image/png'
    assert 'content' not in media and 'owner_id' not in media
    path = f"/api/v1/chat/media/{media['media_id']}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=bob).status_code == 403
    own = client.get(path, headers=alice)
    assert own.status_code == 200 and b'private-metadata' not in own.content
    assert own.headers['cache-control'] == 'private, no-store'
    assert own.headers['x-content-type-options'] == 'nosniff'
    with Image.open(BytesIO(own.content)) as image:
        assert image.size == (12, 8) and 'Comment' not in image.info
    assert send_image(client, bob, cid, media['media_id']).status_code == 400
    sent = send_image(client, alice, cid, media['media_id'])
    assert sent.status_code == 201 and sent.json()['segments'][0]['type'] == 'image'
    assert client.get(path, headers=bob).content == own.content
    service.create_user('outsider', 'astrc-demo', 'Outsider')
    outsider = auth(client, 'outsider')
    assert client.get(path, headers=outsider).status_code == 403
    assert upload(client, outsider, cid).status_code == 403
    private = service.direct(int(ids['alice']), ids['bob'])
    assert send_image(client, alice, private['conversation_id'], media['media_id']).status_code == 400
    bot = ActionDispatcher(service, int(ids['bot']))
    assert bot.dispatch({'action': 'can_send_image'})['data'] == {'yes': False}
    event = next(e for e in service.sync(int(ids['bot']))['events'] if e['type'] == 'message.created' and e['payload']['message']['message_id'] == sent.json()['message_id'])
    assert bot.event(event)['message'] == [{'type': 'text', 'data': {'text': '[Image]'}}]


def test_image_upload_and_send_retries_are_idempotent_and_recall_revokes_download(rig):
    _, ids, client = rig
    alice, bob = auth(client), auth(client, 'bob')
    cid, upload_key, send_key = ids['conversation_id'], str(uuid4()), str(uuid4())
    first = upload(client, alice, cid, key=upload_key)
    assert upload(client, alice, cid, key=upload_key).json() == first.json()
    assert upload(client, alice, cid, content=picture('blue'), key=upload_key).status_code == 409
    mid = first.json()['media_id']
    sent = send_image(client, alice, cid, mid, send_key)
    assert send_image(client, alice, cid, mid, send_key).json() == sent.json()
    assert client.post(f"/api/v1/chat/messages/{sent.json()['message_id']}/recall", headers=alice).status_code == 200
    assert client.get(f'/api/v1/chat/media/{mid}', headers=bob).status_code == 403
    assert client.get(f'/api/v1/chat/media/{mid}', headers=alice).status_code == 200
    assert send_image(client, alice, cid, mid, send_key).json()['segments'] == []


@pytest.mark.parametrize('content', [b'', b'not an image', b'<svg xmlns="http://www.w3.org/2000/svg"/>',
                                    b'x' * (MAX_IMAGE_BYTES + 1), picture(size=(8193, 1)), picture()[:-12]])
def test_bad_uploads_do_not_create_media_or_messages(rig, content):
    service, ids, client = rig
    response = upload(client, auth(client), ids['conversation_id'], content=content)
    assert response.status_code in {400, 413}
    with service.repository.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM media').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1


def test_chunked_body_limit_is_checked_without_content_length(rig):
    service, ids, client = rig
    response = client.post('/api/v1/chat/media', headers=auth(client),
        params={'conversation_id': ids['conversation_id'], 'client_media_id': str(uuid4())},
        content=iter([b'x' * (1024 * 1024)] * 9))
    assert response.status_code == 413
    with service.repository.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM media').fetchone()[0] == 0


@pytest.mark.parametrize('format', ['JPEG', 'WEBP'])
def test_supported_formats_are_decoded_and_reencoded(rig, format):
    _, ids, client = rig
    headers = auth(client)
    response = upload(client, headers, ids['conversation_id'], content=picture(format=format))
    assert response.status_code == 201
    data = client.get('/api/v1/chat/media/' + response.json()['media_id'], headers=headers)
    assert data.headers['content-type'] == 'image/jpeg'
    with Image.open(BytesIO(data.content)) as image:
        assert image.format == 'JPEG' and image.size == (12, 8)


def test_animated_and_excessive_pixel_images_are_rejected(rig):
    _, ids, client = rig
    output = BytesIO()
    Image.new('RGB', (2, 2), 'red').save(output, format='PNG', save_all=True, append_images=[Image.new('RGB', (2, 2), 'blue')], duration=100, loop=0)
    assert upload(client, auth(client), ids['conversation_id'], content=output.getvalue()).status_code == 400
    assert upload(client, auth(client), ids['conversation_id'], content=picture(size=(4001, 4000))).status_code == 413


def test_account_quota_rejects_upload_without_writing(rig, monkeypatch):
    service, ids, client = rig
    monkeypatch.setattr('astrbot_ex.core.chat.media.MAX_ACCOUNT_BYTES', 1)
    assert upload(client, auth(client), ids['conversation_id']).status_code == 413
    with service.repository.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM media').fetchone()[0] == 0


def test_media_and_visibility_survive_reopen_and_v1_database_migration(tmp_path):
    path = tmp_path / 'v1.sqlite3'
    import sqlite3
    db = sqlite3.connect(path)
    schema = __import__('pathlib').Path(__file__).parents[2] / 'astrbot_ex/core/chat/migrations/001_initial.sql'
    db.executescript(schema.read_text(encoding='utf-8'))
    db.close()
    repo = ChatRepository(path)
    service = ChatService(repo)
    ids = service.seed_demo()
    key = str(uuid4())
    stored = service.media.upload(int(ids['alice']), ids['conversation_id'], key, picture())
    service.send(int(ids['alice']), ids['conversation_id'], str(uuid4()), [{'type': 'image', 'data': {'media_id': stored['media_id']}}])
    repo.close()
    repo = ChatRepository(path)
    try:
        service = ChatService(repo)
        assert service.media.upload(int(ids['alice']), ids['conversation_id'], key, picture()) == stored
        assert service.media.download(int(ids['bob']), stored['media_id'])[1] == 'image/png'
        assert repo.connection.execute('PRAGMA user_version').fetchone()[0] == 2
    finally:
        repo.close()


def test_jpeg_orientation_is_applied_and_exif_is_removed(rig):
    _, ids, client = rig
    source = Image.new('RGB', (12, 8), 'green')
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = 'private-caption'
    output = BytesIO()
    source.save(output, format='JPEG', exif=exif)
    headers = auth(client)
    response = upload(client, headers, ids['conversation_id'], content=output.getvalue())
    assert response.status_code == 201
    assert (response.json()['width'], response.json()['height']) == (8, 12)
    fetched = client.get('/api/v1/chat/media/' + response.json()['media_id'], headers=headers)
    with Image.open(BytesIO(fetched.content)) as image:
        assert image.size == (8, 12) and not image.getexif()
    assert b'private-caption' not in fetched.content
