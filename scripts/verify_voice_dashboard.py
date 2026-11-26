"""Exercise the actual dashboard and HTTP API with synthetic voice plugins."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from playwright.sync_api import sync_playwright, expect
    from astrbot_ex.core.api_server import build_server
    from astrbot_ex.core.voice_dashboard import VoiceDashboard
    from tests.test_voice_dashboard import PLUGIN

    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory(prefix='voice-dashboard-') as temp:
        with patch.dict(os.environ, {'ASTRBOTEX_DATA_DIR': temp}):
            server = build_server('127.0.0.1', 0, 20)
        controller = SimpleNamespace(runtime=SimpleNamespace(state=SimpleNamespace(value='idle')))
        server.voice_dashboard = VoiceDashboard(server.local_plugins, controller)
        manager = server.local_plugins
        token = server.decision_management.store.secrets.credential_path.read_text().strip()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def install(plugin_id):
            root = manager.plugins_root / 'special' / plugin_id
            root.mkdir(parents=True)
            (root / 'plugin.json').write_text(json.dumps({'id': plugin_id, 'name': plugin_id,
                'version': '1', 'entry': 'main.py', 'provides': ['trace_plugin'],
                'voice_dashboard': True}), encoding='utf-8')
            (root / 'main.py').write_text(PLUGIN, encoding='utf-8')
            (root / 'config.json').write_text(json.dumps({'decision_api_key': 'synthetic-test-secret',
                'decision_threshold': 0.4, 'streaming_enabled': False}), encoding='utf-8')
            manager.discover()
            manager.set_enabled(plugin_id, True)
            return manager.records[plugin_id].plugin

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1050})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))

                def authenticate():
                    page.locator('#managementCredential').fill(token)
                    page.locator('#managementCredentialApply').click()
                    expect(page.locator('#managementCredentialStatus')).to_contain_text('凭据已应用')
                    page.evaluate('refreshLiveVoice()')

                page.goto(f'http://127.0.0.1:{server.server_port}/#/voice')
                authenticate()
                expect(page.locator('#voiceLiveState')).to_contain_text('请选择外部语音插件')
                expect(page.locator('#voiceLiveStart')).to_be_disabled()
                checks.append('missing plugin is actionable; no capture starts')
                plugin = install('synthetic_voice_a')
                stamp = time.time() - 5
                plugin.snapshot['events'] = [
                    {'id': 1, 'timestamp': stamp, 'type': 'transcript', 'payload': {'role': 'user', 'text': '你好，测试语音', 'partial': False}},
                    {'id': 2, 'timestamp': stamp + 1, 'type': 'partial', 'payload': {'text': '碎片'}},
                    {'id': 3, 'timestamp': stamp + 2, 'type': 'reply', 'payload': {'text': '收到，这是一条测试回复。', 'partial': False}}]
                page.evaluate('refreshLiveVoice()')
                expect(page.locator('#voiceLivePlugin')).to_have_value('synthetic_voice_a')
                expect(page.locator('.voice-bubble')).to_have_count(2)
                assert 'synthetic-test-secret' not in page.content()
                expect(page.locator('#voiceLiveKey')).to_have_value('')
                page.locator('#voiceLiveThreshold').fill('0.65')
                page.locator('#voiceLiveSave').click()
                expect(page.locator('#voiceLiveResult')).to_contain_text('设置已保存')
                config = manager.get_plugin('synthetic_voice_a')['config']
                assert config['decision_threshold'] == 0.65
                assert config['decision_api_key'] == 'synthetic-test-secret'
                plugin = manager.records['synthetic_voice_a'].plugin
                checks.append('partial events filtered; blank key preserved; credentials absent from DOM')
                page.locator('#voiceChatClear').click()
                page.evaluate('refreshLiveVoice()')
                expect(page.locator('.voice-bubble')).to_have_count(0)
                page.reload()
                authenticate()
                expect(page.locator('.voice-bubble')).to_have_count(0)
                plugin.snapshot['events'].append({'id': 4, 'timestamp': time.time(), 'type': 'reply',
                    'payload': {'text': '清空后的新回复', 'partial': False}})
                page.evaluate('refreshLiveVoice()')
                expect(page.locator('.voice-bubble')).to_have_count(1)
                page.evaluate('refreshLiveVoice()')
                expect(page.locator('.voice-bubble')).to_have_count(1)
                checks.append('clear survives polling and reload; new events remain deduplicated')
                page.locator('.voice-advanced summary').click()
                page.locator('#voiceLiveRefresh').click()
                expect(page.locator('#voiceDeviceResult')).to_contain_text('已刷新 1 个端点')
                page.locator('#voiceLiveCheck').click()
                expect(page.locator('#voiceDeviceResult')).to_contain_text('格式检查通过')
                controller.runtime.state.value = 'running'
                page.evaluate('refreshLiveVoice()')
                page.locator('#voiceLiveStart').click()
                expect(page.locator('#voiceLiveActionResult')).to_contain_text('已开启')
                expect(page.locator('#voiceLiveSave')).to_be_disabled()
                page.locator('#voiceLiveStop').click()
                expect(page.locator('#voiceLiveActionResult')).to_contain_text('已停止')
                checks.append('device delegation and explicit start/stop work; active config is locked')
                second = install('synthetic_voice_b')
                second.snapshot['events'] = [{'id': 1, 'timestamp': time.time(), 'type': 'reply',
                    'payload': {'text': '第二套设备的回复', 'partial': False}}]
                page.evaluate('refreshLiveVoice()')
                expect(page.locator('#voiceLivePlugin option')).to_have_count(3)
                page.locator('#voiceLivePlugin').select_option('synthetic_voice_b')
                expect(page.locator('.voice-bubble')).to_have_count(1)
                expect(page.locator('.voice-bubble')).to_have_text('第二套设备的回复')
                checks.append('provider selection isolates configuration and transcript history')
                page.screenshot(path=str(output / 'voice-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(output / 'voice-mobile.png'), full_page=True)
                assert not errors, errors
                checks.append('desktop and 390px mobile render without overflow or JavaScript errors')
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
    (output / 'result.json').write_text(json.dumps({'ok': True, 'checks': checks},
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'ok': True, 'checks': len(checks), 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
