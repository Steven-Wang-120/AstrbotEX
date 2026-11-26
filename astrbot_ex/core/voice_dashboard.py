"""Host-independent dashboard contract for optional continuous voice plugins."""
from __future__ import annotations

import copy
import threading


FIELDS = frozenset((
    'provider_id input_device output_device decision_mode decision_type decision_url '
    'decision_model decision_token_env decision_api_key decision_timeout decision_source '
    'decision_topic decision_threshold forward_threshold quiet_threshold decision_json_mode '
    'decision_stage'
).split())


class VoiceDashboard:
    def __init__(self, manager, controller):
        self.manager, self.controller = manager, controller
        self.lock = threading.RLock()
        self.query_lock = threading.Lock()

    def providers(self):
        return [{'id': item.manifest.id, 'name': item.manifest.name,
                 'enabled': item.enabled, 'loaded': item.loaded}
                for item in list(self.manager.records.values())
                if item.manifest.voice_dashboard]

    def record(self, plugin_id=None):
        providers = self.providers()
        if plugin_id is None and len(providers) == 1:
            plugin_id = providers[0]['id']
        record = self.manager.records.get(plugin_id)
        if record is None or not record.manifest.voice_dashboard:
            raise ValueError('请选择已安装并声明 voice_dashboard 的语音插件')
        return record

    def _slot(self, record, method):
        slot = self.manager.registry.get(record.manifest.id)
        if not record.enabled or slot is None or not slot.enabled:
            raise ValueError('请先在插件页启用所选语音插件')
        if not slot.has_method(method):
            raise ValueError('语音插件未实现接口：' + method)
        return slot

    def _secrets(self, record):
        config = self.manager.get_plugin(record.manifest.id)['config']
        return tuple(value for key, value in config.items()
                     if isinstance(value, str) and value and any(word in key.lower()
                     for word in ('api_key', 'password', 'secret', 'access_token')))

    @staticmethod
    def _redact(value, secrets=()):
        if isinstance(value, dict):
            return {key: ('[redacted]' if any(word in key.lower() for word in
                    ('api_key', 'password', 'secret', 'authorization', 'access_token'))
                    else VoiceDashboard._redact(item, secrets))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [VoiceDashboard._redact(item, secrets) for item in value]
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, '[redacted]')
        return value

    def status(self, plugin_id=None):
        with self.lock:
            providers = self.providers()
            common = {'ok': True, 'providers': providers,
                      'runtime_state': self.controller.runtime.state.value}
            if plugin_id is not None and plugin_id not in {item['id'] for item in providers}:
                plugin_id = None
            if plugin_id is None and len(providers) != 1:
                return {**common, 'installed': False, 'config': {}, 'status': {}, 'events': []}
            record = self.record(plugin_id)
            config = self.manager.get_plugin(record.manifest.id)['config']
            slot = self.manager.registry.get(record.manifest.id)
            data = (self._slot(record, 'get_voice_status').call('get_voice_status', timeout=3)
                    if record.enabled and slot is not None else
                    {'state': record.status, 'last_error': record.error})
            if not isinstance(data, dict):
                raise ValueError('语音插件状态必须为 JSON 对象')
            data = copy.deepcopy(data)
            status = {key: data[key] for key in ('state', 'streaming_enabled', 'worker_running',
                      'runtime_active', 'pending_decisions', 'last_error') if key in data}
            status.setdefault('state', 'disabled' if not record.enabled else 'stopped')
            public_config = {key: value for key, value in config.items()
                             if key in FIELDS and key != 'decision_api_key'}
            models = {}
            supplied_models = data.get('models', {})
            if not isinstance(supplied_models, dict):
                raise ValueError('语音插件 models 必须为 JSON 对象')
            for kind in ('stt', 'decision', 'chat', 'tts'):
                item = supplied_models.get(kind, {})
                if isinstance(item, dict):
                    models[kind] = {key: item[key] for key in ('model', 'configured',
                                    'last_check_ok', 'checked_at') if key in item}
            profile = data.get('decision_profile', {})
            if not isinstance(profile, dict):
                profile = {}
            events = data.get('events', [])
            if not isinstance(events, list):
                raise ValueError('语音插件 events 必须为数组')
            result = {**common, 'plugin_id': record.manifest.id, 'installed': True,
                      'enabled': record.enabled, 'loaded': record.loaded,
                      'config': public_config, 'status': status, 'events': events[-80:],
                      'models': models, 'decision_profile': {
                          'type': profile.get('type', config.get('decision_type', '')),
                          'model': profile.get('model', config.get('decision_model', ''))}}
            result = self._redact(result, self._secrets(record))
            result['config']['decision_api_key_configured'] = bool(config.get('decision_api_key'))
            return result

    def configure(self, config, plugin_id=None):
        if not isinstance(config, dict) or set(config) - FIELDS:
            raise ValueError('无效的语音配置字段')
        for name in ('input_device', 'output_device'):
            value = config.get(name)
            if value is None or type(value) is int or isinstance(value, str):
                continue
            if (not isinstance(value, dict) or set(value) - {'id', 'name', 'host_api'}
                    or not isinstance(value.get('id'), str) or not value['id']
                    or any(not isinstance(item, str) for item in value.values())):
                raise ValueError('设备选择必须来自设备列表')
        with self.lock:
            record = self.record(plugin_id)
            status = self.status(record.manifest.id)['status']
            if status.get('worker_running') or status.get('streaming_enabled'):
                raise ValueError('请先停止连续对话再保存配置')
            self.manager.update_config(record.manifest.id, {**config, 'streaming_enabled': False})
            return self.status(record.manifest.id)

    def streaming(self, enabled, plugin_id=None):
        if type(enabled) is not bool:
            raise ValueError('enabled 必须是布尔值')
        with self.lock:
            record = self.record(plugin_id)
            if enabled and self.controller.runtime.state.value != 'running':
                raise ValueError('请先在总览启动 EX 运行时')
            self._slot(record, 'set_streaming').call('set_streaming', enabled, timeout=5)
            return self.status(record.manifest.id)

    def devices(self, payload, plugin_id=None):
        if not self.query_lock.acquire(blocking=False):
            raise ValueError('上一项设备查询尚未结束')
        try:
            record = self.record(plugin_id)
            result = self._slot(record, 'query_audio_devices').call(
                'query_audio_devices', dict(payload), timeout=10)
            if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                raise ValueError('语音插件返回无效的设备查询结果')
            return self._redact(result, self._secrets(record))
        finally:
            self.query_lock.release()
