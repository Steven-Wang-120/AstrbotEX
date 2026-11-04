"""Dashboard adapter for the optional isolated voice plugin."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

VOICE_ID = 'astrbotex_voice_jev'
FIELDS = frozenset(('python_executable vosk_model_path speech_config_path chat_config_path '
                    'provider_id input_device output_device gate_config_path decision_mode '
                    'decision_type decision_url decision_model decision_token_env decision_api_key '
                    'decision_timeout decision_source decision_topic decision_threshold '
                    'forward_threshold quiet_threshold decision_json_mode decision_stage').split())


def model_profiles(config, root):
    """Return explicit public model identities; never expose provider credentials."""
    def read(path):
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}
    speech = read(config.get('speech_config_path'))
    chat = read(config.get('chat_config_path'))
    gate = read(config.get('gate_config_path'))
    models = {}
    for kind in ('stt', 'tts'):
        settings = speech.get(kind, {})
        settings = settings if isinstance(settings, dict) else {}
        models[kind] = {'model': settings.get('model', ''), 'configured': bool(settings.get('backend'))}
    models['chat'] = {'model': config.get('provider_id') or chat.get('provider_id', ''),
                      'configured': bool(chat)}
    direct = bool(config.get('decision_url'))
    models['decision'] = {'model': config.get('decision_model', '') if direct else gate.get('model', ''),
                          'configured': direct or bool(gate.get('url'))}
    report = read(root / 'models-check.local.json')
    checks = report.get('models', {})
    if isinstance(checks, dict):
        for kind, item in models.items():
            checked = checks.get(kind, {})
            if isinstance(checked, dict) and type(checked.get('ok')) is bool:
                item['last_check_ok'] = checked['ok']
                item['checked_at'] = report.get('checked_at')
    return models


class VoiceDashboard:
    def __init__(self, manager, controller):
        self.manager, self.controller = manager, controller
        self.lock = threading.RLock()
        self.query_lock = threading.Lock()

    def record(self):
        record = self.manager.records.get(VOICE_ID)
        if record is None:
            raise ValueError('未安装语音插件 astrbotex_voice_jev')
        return record

    def status(self):
        with self.lock:
            record = self.manager.records.get(VOICE_ID)
            if record is None:
                return {'ok': True, 'installed': False, 'config': {}, 'status': {}, 'events': []}
            config = self.manager.get_plugin(VOICE_ID)['config']
            slot = self.manager.registry.get(VOICE_ID)
            status = slot.call('get_status') if slot else {'state': 'disabled', 'streaming_enabled': False}
            events = status.pop('events', [])
            profile = {k: config.get('decision_' + k, '') for k in ('type', 'url', 'model')}
            if config.get('gate_config_path') and not config.get('decision_url'):
                try:
                    saved = json.loads(Path(config['gate_config_path']).read_text(encoding='utf-8-sig'))
                    profile = {k: saved.get(k, '') for k in ('type', 'url', 'model')}
                except (OSError, ValueError):
                    profile = {'type': '本地配置读取失败'}
            return {'ok': True, 'installed': True, 'enabled': record.enabled, 'loaded': record.loaded,
                    'runtime_state': self.controller.runtime.state.value, 'status': status,
                    'config': {k: v for k, v in config.items() if k in FIELDS}, 'events': events,
                    'models': model_profiles(config, record.root),
                    'decision_profile': {k: profile.get(k, '') for k in ('type', 'model')}}

    def configure(self, config):
        if not isinstance(config, dict) or set(config) - FIELDS:
            raise ValueError('无效的语音配置字段')
        for name in ('input_device', 'output_device'):
            if name not in config:
                continue
            value = config[name]
            if value is None or type(value) is int or isinstance(value, str):
                continue
            if (not isinstance(value, dict) or set(value) - {'id', 'name', 'host_api'}
                    or not isinstance(value.get('id'), str) or not value['id'].startswith('pa:')
                    or any(not isinstance(v, str) for v in value.values())):
                raise ValueError('设备选择必须来自设备列表')
        with self.lock:
            status = self.status()['status']
            if status.get('worker_running') or status.get('streaming_enabled'):
                raise ValueError('请先停止连续对话再保存配置')
            # Force capture off across plugin reloads and future runtime starts.
            self.manager.update_config(VOICE_ID, {**config, 'streaming_enabled': False})
            return self.status()

    def streaming(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled 必须是布尔值')
        with self.lock:
            self.record()
            if enabled:
                if self.controller.runtime.state.value != 'running':
                    raise ValueError('请先在总览启动 EX 运行时')
                if not self.record().enabled:
                    self.manager.set_enabled(VOICE_ID, True)
            slot = self.manager.registry.get(VOICE_ID)
            if slot:
                slot.call('set_streaming', enabled)
            return self.status()

    def devices(self, payload):
        if not self.query_lock.acquire(blocking=False):
            raise ValueError('上一项设备查询尚未结束')
        try:
            record = self.record()
            config = self.manager.get_plugin(VOICE_ID)['config']
            request = dict(payload)
            if request.get('action') == 'validate':
                for name in ('input_device', 'output_device'):
                    request.setdefault(name, config.get(name))
            result = subprocess.run(
                [config.get('python_executable') or sys.executable, '-X', 'utf8',
                 str(record.root / 'lib' / 'audio_devices.py')],
                input=json.dumps(request), capture_output=True, text=True, encoding='utf-8',
                timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode or len(result.stdout) > 524288:
                raise ValueError('设备查询失败，请检查音频 Python 环境和系统驱动')
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            raise ValueError('音频设备查询超时') from None
        finally:
            self.query_lock.release()
