from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_ex.core.event_bus import EventBus
from astrbot_ex.core.local_plugins import LocalPluginManager
from astrbot_ex.core.plugin_registry import PluginRegistry
from astrbot_ex.core.topic_bus import TopicBus
from astrbot_ex.core.voice_dashboard import VoiceDashboard


PLUGIN = '''class Plugin:
    def __init__(self, context):
        self.streaming = False
        self.snapshot = {"state": "ready", "events": [
            {"id": 1, "timestamp": 1, "type": "reply", "payload": {
                "text": "synthetic-test-secret", "api_key": "hidden"}}],
            "models": {"stt": {"model": "test-asr", "configured": True}}}
    def get_voice_status(self):
        return {**self.snapshot, "streaming_enabled": self.streaming}
    def set_streaming(self, enabled):
        self.streaming = enabled
    def query_audio_devices(self, request):
        return {"ok": True, "action": request["action"], "devices": [
            {"id": "ros:audio-1", "name": "Remote audio", "host_api": "ROS2",
             "input_channels": 1, "output_channels": 1}]}
'''


@pytest.fixture
def voice(tmp_path):
    manager = LocalPluginManager(plugins_root=tmp_path / 'plugins',
        state_path=tmp_path / 'state.json', registry=PluginRegistry(),
        event_bus=EventBus(), topic_bus=TopicBus())
    controller = SimpleNamespace(runtime=SimpleNamespace(state=SimpleNamespace(value='idle')))

    def install(plugin_id='test_voice', enabled=True, declared=True):
        root = manager.plugins_root / 'special' / plugin_id
        root.mkdir(parents=True)
        manifest = {'id': plugin_id, 'name': plugin_id, 'version': '1.0', 'entry': 'main.py',
                    'provides': ['trace_plugin'], 'voice_dashboard': declared}
        (root / 'plugin.json').write_text(json.dumps(manifest), encoding='utf-8')
        (root / 'main.py').write_text(PLUGIN, encoding='utf-8')
        (root / 'config.json').write_text(json.dumps({'decision_api_key': 'synthetic-test-secret',
            'decision_threshold': 0.4, 'streaming_enabled': False,
            'python_executable': '/host/private/python', 'vosk_model_path': '/host/private/model'}), encoding='utf-8')
        manager.discover()
        if enabled:
            manager.set_enabled(plugin_id, True)
        return manager.records[plugin_id]

    dashboard = VoiceDashboard(manager, controller)
    yield dashboard, manager, controller, install
    for slot in manager.registry.list():
        manager.registry.unregister(slot.id)


def test_no_plugin_and_unrelated_plugin_do_not_start_capture(voice):
    dashboard, _, _, install = voice
    assert dashboard.status()['providers'] == []
    install(declared=False)
    assert dashboard.status()['installed'] is False
    with pytest.raises(ValueError):
        dashboard.streaming(True)


def test_manifest_voice_declaration_is_strict_boolean(voice):
    _, manager, _, _ = voice
    for invalid in (1, 'true', {}, None):
        manifest = manager._manifest_from_bytes(json.dumps({'id': 'bad', 'name': 'bad',
            'version': '1', 'entry': 'main.py', 'provides': ['trace_plugin'],
            'voice_dashboard': invalid}).encode())
        with pytest.raises(ValueError, match='voice_dashboard'):
            manager._validate_manifest(manifest)


def test_status_redacts_secrets_and_host_paths_without_mutating_plugin(voice):
    dashboard, manager, _, install = voice
    record = install('robot_voice')
    result = dashboard.status()
    assert result['plugin_id'] == 'robot_voice'
    assert result['models']['stt']['model'] == 'test-asr'
    assert result['config']['decision_api_key_configured'] is True
    assert 'synthetic-test-secret' not in json.dumps(result)
    assert '/host/private' not in json.dumps(result)
    assert 'decision_api_key' not in result['config']
    assert len(record.plugin.snapshot['events']) == 1
    assert manager.get_plugin('robot_voice')['config']['decision_api_key'] == 'synthetic-test-secret'


def test_multiple_plugins_require_selection_and_isolate_configuration(voice):
    dashboard, manager, _, install = voice
    install('robot_voice')
    install('desktop_voice')
    assert len(dashboard.status()['providers']) == 2
    assert dashboard.status()['installed'] is False
    with pytest.raises(ValueError):
        dashboard.configure({'decision_threshold': 0.9})
    dashboard.configure({'decision_threshold': 0.7}, 'desktop_voice')
    assert manager.get_plugin('desktop_voice')['config']['decision_threshold'] == 0.7
    assert manager.get_plugin('robot_voice')['config']['decision_threshold'] == 0.4


def test_partial_config_preserves_key_and_does_not_start_streaming(voice):
    dashboard, manager, _, install = voice
    install()
    result = dashboard.configure({'decision_threshold': 0.6,
        'input_device': {'id': 'ros:audio-1', 'name': 'Remote audio', 'host_api': 'ROS2'}})
    assert result['status']['streaming_enabled'] is False
    assert manager.get_plugin('test_voice')['config']['decision_api_key'] == 'synthetic-test-secret'
    result = dashboard.configure({'decision_api_key': 'replacement-test-key'})
    assert 'replacement-test-key' not in json.dumps(result)
    assert manager.get_plugin('test_voice')['config']['decision_api_key'] == 'replacement-test-key'


@pytest.mark.parametrize('config', [None, [], {'python_executable': 'bad'},
    {'vosk_model_path': 'bad'}, {'input_device': True}, {'output_device': {'id': 'x', 'extra': 'x'}}])
def test_invalid_and_host_specific_configuration_is_rejected(voice, config):
    dashboard, _, _, install = voice
    install()
    with pytest.raises(ValueError):
        dashboard.configure(config)


def test_streaming_requires_runtime_and_blocks_config_until_stopped(voice):
    dashboard, _, controller, install = voice
    install()
    with pytest.raises(ValueError, match='运行时'):
        dashboard.streaming(True)
    controller.runtime.state.value = 'running'
    assert dashboard.streaming(True)['status']['streaming_enabled'] is True
    with pytest.raises(ValueError, match='停止'):
        dashboard.configure({'decision_threshold': 0.8})
    assert dashboard.streaming(False)['status']['streaming_enabled'] is False
    with pytest.raises(ValueError):
        dashboard.streaming('true')


def test_disabled_plugin_can_configure_without_being_enabled(voice):
    dashboard, _, controller, install = voice
    record = install(enabled=False)
    assert dashboard.configure({'decision_threshold': 0.6})['enabled'] is False
    controller.runtime.state.value = 'running'
    with pytest.raises(ValueError, match='启用'):
        dashboard.streaming(True)
    assert record.enabled is False


def test_device_requests_are_delegated_and_concurrent_queries_rejected(voice):
    dashboard, _, _, install = voice
    install()
    result = dashboard.devices({'action': 'list'})
    assert result['devices'][0]['id'] == 'ros:audio-1'
    assert dashboard.devices({'action': 'validate'})['action'] == 'validate'
    with dashboard.query_lock:
        with pytest.raises(ValueError, match='尚未结束'):
            dashboard.devices({'action': 'list'})


def test_missing_and_malformed_plugin_contracts_fail_explicitly(voice):
    dashboard, manager, _, install = voice
    record = install()
    record.plugin.snapshot['events'] = 'invalid'
    with pytest.raises(ValueError, match='events'):
        dashboard.status()
    record.plugin.snapshot['events'] = []
    record.plugin.snapshot['models'] = None
    with pytest.raises(ValueError, match='models'):
        dashboard.status()
    manager.registry.get('test_voice').plugin.query_audio_devices = lambda _: []
    with pytest.raises(ValueError, match='设备查询结果'):
        dashboard.devices({'action': 'list'})


def test_dashboard_has_no_audio_hardware_or_platform_dependencies():
    import astrbot_ex.core.voice_dashboard as module
    tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
    imports = {name.name.split('.')[0] for node in ast.walk(tree)
               if isinstance(node, ast.Import) for name in node.names}
    assert not imports & {'subprocess', 'ctypes', 'sounddevice', 'vosk'}
