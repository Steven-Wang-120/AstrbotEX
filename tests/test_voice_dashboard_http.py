from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from astrbot_ex.core.api_server import build_server
from tests.test_voice_dashboard import voice


@pytest.fixture
def http_voice(voice, tmp_path, monkeypatch):
    dashboard, manager, controller, install = voice
    monkeypatch.setenv('ASTRBOTEX_DATA_DIR', str(tmp_path / 'http-data'))
    server = build_server('127.0.0.1', 0, 20)
    server.voice_dashboard = dashboard
    token = server.decision_management.store.secrets.credential_path.read_text().strip()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path='status', body=None, authorized=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if authorized:
            headers['Authorization'] = 'Bearer ' + token
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/v1/ex/voice/{path}',
            data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            response = opener.open(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    yield request, dashboard, manager, controller, install
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_voice_http_requires_management_auth_and_same_origin(http_voice):
    request, *_ = http_voice
    assert request(authorized=False)[0] == 401
    assert request(origin='http://example.invalid')[0] == 403
    code, body = request()
    assert code == 200 and body['providers'] == []


def test_voice_http_config_devices_and_streaming_use_selected_plugin(http_voice):
    request, _, manager, controller, install = http_voice
    install('robot_voice')
    install('desktop_voice')
    code, body = request('status?plugin_id=desktop_voice')
    assert code == 200 and body['plugin_id'] == 'desktop_voice'
    assert 'synthetic-test-secret' not in json.dumps(body)
    assert request('devices?plugin_id=desktop_voice')[1]['devices'][0]['id'] == 'ros:audio-1'
    assert request('devices/check', {'plugin_id': 'robot_voice'})[1]['action'] == 'validate'
    assert request('config', {'plugin_id': 'desktop_voice', 'config': {'decision_threshold': 0.8}})[0] == 200
    assert manager.get_plugin('robot_voice')['config']['decision_threshold'] == 0.4
    assert request('streaming', {'plugin_id': 'desktop_voice', 'enabled': True})[0] == 400
    controller.runtime.state.value = 'running'
    assert request('streaming', {'plugin_id': 'desktop_voice', 'enabled': True})[1]['status']['streaming_enabled']
    assert request('config', {'plugin_id': 'desktop_voice', 'config': {}})[0] == 400
    assert request('streaming', {'plugin_id': 'desktop_voice', 'enabled': False})[0] == 200


@pytest.mark.parametrize('path,body', [('unknown', None), ('config', []),
    ('config', {'plugin_id': [], 'config': {}}), ('streaming', {'enabled': 1}),
    ('config', {'config': {'python_executable': 'arbitrary-command'}})])
def test_voice_http_rejects_invalid_requests_without_server_error(http_voice, path, body):
    request, _, _, _, install = http_voice
    install()
    assert request(path, body)[0] in (400, 404)
