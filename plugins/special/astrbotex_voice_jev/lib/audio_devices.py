"""System audio inventory, explicit selection and format checks for any frontend.

No driver installation, microphone capture or test playback is performed here.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import uuid


class AudioDeviceError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def get_backend(backend=None):
    if backend is not None:
        return backend
    try:
        import sounddevice
        return sounddevice
    except (ImportError, OSError):
        raise AudioDeviceError('backend_unavailable', '音频主机缺少 sounddevice/PortAudio 或系统音频库。') from None


def refresh_backend(backend=None, busy=False):
    """Rescan the pinned PortAudio backend only while this app owns no streams.

    sounddevice 0.5.3 exposes no public hotplug rescan. Other clients can obtain
    a fresh snapshot by invoking this module in a fresh worker process instead.
    """
    if busy:
        raise AudioDeviceError('requires_stop', '请先停止聆听和播放，再刷新或切换音频设备。')
    backend = get_backend(backend)
    terminate, initialize = getattr(backend, '_terminate', None), getattr(backend, '_initialize', None)
    if not callable(terminate) or not callable(initialize):
        raise AudioDeviceError('restart_required', '当前音频后端不支持重新扫描，请重启音频程序后再选择新设备。')
    try:
        terminate()
        initialize()
    except Exception:
        raise AudioDeviceError('backend_refresh_failed', '音频后端重新扫描失败，请重启程序并检查系统设备。') from None
    return inventory(backend)


def inventory(backend=None):
    backend = get_backend(backend)
    try:
        apis = list(backend.query_hostapis())
        devices = list(backend.query_devices())
        defaults = list(backend.default.device)
    except Exception:
        raise AudioDeviceError('enumeration_failed', '无法读取音频设备；请检查系统音频服务、权限及容器设备映射。') from None
    if len(devices) > 256:
        raise AudioDeviceError('inventory_too_large', '音频设备数量超过256，请在系统中整理音频后端。')
    result = []
    for index, raw in enumerate(devices):
        if not raw.get('max_input_channels') and not raw.get('max_output_channels'):
            continue
        name = str(raw['name'])
        api = str(apis[int(raw['hostapi'])]['name'])
        key = json.dumps([api, name], ensure_ascii=False).encode('utf-8')
        identifier = 'pa:'+hashlib.sha256(key).hexdigest()[:24]
        result.append({'id': identifier, 'index': index, 'name': name[:256], 'host_api': api[:80],
                       'input_channels': int(raw['max_input_channels']),
                       'output_channels': int(raw['max_output_channels']),
                       'default_sample_rate': float(raw['default_samplerate']),
                       'default_input': index == defaults[0], 'default_output': index == defaults[1],
                       'identity': 'name_and_host_api', 'persistent': True, 'selectable': True})
    counts = Counter(item['id'] for item in result)
    for item in result:
        if counts[item['id']] > 1:
            item['id'] += ':index-'+str(item['index'])
            item['persistent'] = False
            item['selectable'] = False
        item['label'] = '{} [{}]{}'.format(item['name'], item['host_api'],
                                           '（同名无法可靠区分，请在系统中改名）' if not item['persistent'] else '')
    return {'schema_version': 'audio-devices.v1', 'inventory_id': uuid.uuid4().hex,
            'scope': 'audio_worker_host', 'backend': 'sounddevice/PortAudio',
            'driver_installation': 'operating_system', 'devices': result,
            'notes': ['设备ID根据名称和音频后端生成，不是硬件序列号；同名同后端设备需在系统中区分名称后明确选择。',
                      '列表缺少设备不能单独证明缺驱动，也可能是未连接、权限、配对、音频服务或容器映射问题。']}


def resolve(selector, direction, snapshot):
    if direction not in ('input', 'output'):
        raise AudioDeviceError('invalid_direction', '音频方向必须是input或output。')
    devices = snapshot['devices']
    metadata = selector if isinstance(selector, dict) else None
    if metadata is not None:
        selector = metadata.get('id')
        if not isinstance(selector, str) or not selector.startswith('pa:'):
            raise AudioDeviceError('invalid_selector', '设备对象必须包含id。')
    if selector is None or selector == 'default':
        matches = [d for d in devices if d['default_'+direction]]
    elif isinstance(selector, bool):
        raise AudioDeviceError('invalid_selector', '设备选择不能是布尔值。')
    elif isinstance(selector, int):
        matches = [d for d in devices if d['index'] == selector]
    elif isinstance(selector, str):
        if selector.startswith('pa:'):
            matches = [d for d in devices if d['id'] == selector]
        else:
            exact = [d for d in devices if d['name'].lower() == selector.lower()]
            matches = exact or [d for d in devices if selector.lower() in (d['name']+' '+d['host_api']).lower()]
    else:
        raise AudioDeviceError('invalid_selector', '设备选择必须为设备id、名称、旧索引、设备对象或null。')
    matches = [d for d in matches if d[direction+'_channels'] > 0]
    if not matches:
        raise AudioDeviceError('device_not_found', '所选{}设备不可见或方向不支持；请连接设备后刷新，不会自动改用其他设备。'.format(direction))
    if len(matches) != 1:
        raise AudioDeviceError('ambiguous_device', '存在多个同名设备，请从最新列表明确选择后端和设备。')
    item = matches[0]
    if not item['selectable'] and selector not in (None, 'default'):
        raise AudioDeviceError('ambiguous_device', '同名同后端设备无法可靠区分；请在系统中改名，或显式使用系统默认设备。')
    if metadata and ((metadata.get('name') and metadata['name'] != item['name']) or
                     (metadata.get('host_api') and metadata['host_api'] != item['host_api'])):
        raise AudioDeviceError('device_identity_changed', '设备身份已变化，请刷新后重新选择。')
    return dict(item)


def descriptor(item):
    if not item['persistent']:
        raise AudioDeviceError('ambiguous_persistence', '同名同后端设备需在系统中区分名称后再选择和保存。')
    return {key: item[key] for key in ('id', 'name', 'host_api')}


def validate_selection(input_device=None, output_device=None, backend=None, snapshot=None):
    backend = get_backend(backend)
    # A frontend snapshot is display data, never authority for the current
    # numeric index. Rebind its identity against the live backend before use.
    previous = snapshot
    snapshot = inventory(backend)
    results = {}
    for direction, selector, rate in (('input', input_device, 16000), ('output', output_device, 24000)):
        try:
            if previous is not None and selector not in (None, 'default'):
                old = resolve(selector, direction, previous)
                selector = descriptor(old)
            item = resolve(selector, direction, snapshot)
            check = backend.check_input_settings if direction == 'input' else backend.check_output_settings
            try:
                check(device=item['index'], samplerate=rate, channels=1, dtype='int16')
            except Exception as exc:
                results[direction] = {'ok': False, 'code': 'settings_rejected', 'device': item,
                                      'message': '系统能看到设备，但无法确认{}Hz单声道PCM16设置：{}'.format(rate, str(exc)[:300]),
                                      'hint': '检查采样率、系统音频转换、设备占用、权限和厂商驱动；此结果不等于缺驱动。'}
            else:
                results[direction] = {'ok': True, 'device': item, 'sample_rate': rate, 'channels': 1, 'dtype': 'int16'}
        except AudioDeviceError as exc:
            results[direction] = {'ok': False, 'code': exc.code, 'message': str(exc)}
    return {'schema_version': 'audio-devices.v1', 'ok': all(r['ok'] for r in results.values()),
            'scope': 'audio_worker_host', 'capture_performed': False, 'playback_performed': False,
            'input': results['input'], 'output': results['output']}


def require_selection(input_device=None, output_device=None, backend=None, snapshot=None):
    result = validate_selection(input_device, output_device, backend, snapshot)
    if not result['ok']:
        failures = [r for r in (result['input'], result['output']) if not r['ok']]
        raise AudioDeviceError(failures[0]['code'], '；'.join(r['message'] for r in failures))
    return result['input']['device']['index'], result['output']['device']['index']


def load_selection(path):
    path = Path(path)
    if not path.exists():
        return {'input_device': None, 'output_device': None}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        return {key: data.get(key) for key in ('input_device', 'output_device')}
    except (OSError, ValueError, AttributeError):
        raise AudioDeviceError('invalid_device_config', '音频设备配置损坏，请重新选择并保存。') from None


def save_selection(path, input_device, output_device, backend=None, snapshot=None):
    snapshot = snapshot or inventory(backend)
    selected = {key+'_device': descriptor(resolve(value, key, snapshot)) if value is not None else None
                for key, value in (('input', input_device), ('output', output_device))}
    path = Path(path)
    temporary = path.with_name(path.stem+'.pending.local.json')
    temporary.write_text(json.dumps(selected, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)
    return selected


def request(payload, backend=None):
    """Frontend-neutral JSON RPC. CLI runs in a fresh audio-host process."""
    if not isinstance(payload, dict):
        raise AudioDeviceError('invalid_request', '设备请求必须是JSON对象。')
    request_id = payload.get('request_id')
    if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 100):
        raise AudioDeviceError('invalid_request', 'request_id必须是1～100字符的字符串。')
    action = payload.get('action', 'list')
    if action == 'list':
        result = {'ok': True, **inventory(backend)}
    elif action == 'validate':
        result = validate_selection(payload.get('input_device'), payload.get('output_device'), backend)
    else:
        raise AudioDeviceError('unsupported_action', '设备接口支持list与validate；保存选择请使用现有配置接口。')
    if request_id is not None:
        result['request_id'] = request_id
    return result


if __name__ == '__main__':
    try:
        raw = sys.stdin.read(16385)
        if len(raw) > 16384:
            raise AudioDeviceError('request_too_large', '设备请求超过大小限制。')
        result = request(json.loads(raw) if raw.strip() else {'action': 'list'})
    except AudioDeviceError as exc:
        result = {'ok': False, 'code': exc.code, 'message': str(exc)}
    except (ValueError, TypeError):
        result = {'ok': False, 'code': 'invalid_request', 'message': '设备请求JSON无效。'}
    print(json.dumps(result, ensure_ascii=False), flush=True)
