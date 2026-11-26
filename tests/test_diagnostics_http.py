"""Authenticated diagnostic cache, refresh and real HTTP SSE contract."""
import http.client
import json
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
from astrbot_ex.core.environments.plugin_api import PluginRosFacade
from astrbot_ex.core.models import RuntimeState
from tests.test_decision_management_http import ManagementHTTPFixture
from tests.test_environments import FakeAdapter, Message, finish


class DiagnosticHTTPTests(ManagementHTTPFixture, unittest.TestCase):
    prefix = '/api/v1/ex/environments'

    def stream(self, cursor=None):
        headers = {'Authorization': 'Bearer ' + self.token}
        if cursor is not None: headers['Last-Event-ID'] = cursor
        return self.opener.open(Request(self.base + '/api/events', headers=headers), timeout=3)

    def frame(self, response):
        result, data = {}, []
        for _ in range(100):
            line = response.readline().decode('utf-8').rstrip('\r\n')
            if not line and data:
                result['data'] = json.loads('\n'.join(data))
                return result
            if line.startswith('data:'): data.append(line[5:].lstrip(' '))
            elif line.startswith('event:'): result['event'] = line[6:].strip()
            elif line.startswith('id:'): result['id'] = line[3:].strip()
        self.fail('no complete SSE frame')

    def test_dashboard_assets_are_available_from_the_existing_static_route(self):
        for name, kind in (('logs.js', 'javascript'), ('logs.css', 'css'), ('environment-diagnostics.css', 'css')):
            with self.opener.open(self.base + '/dashboard/' + name, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(kind, response.headers['Content-Type'])

    def test_cache_get_does_not_probe_or_mutate_revision(self):
        manager = self.server.environment_manager
        service = manager._diagnostics
        before = manager.snapshot()
        with service._cycle_lock, patch.object(service, '_collector') as collector, patch.object(service, '_preflight_collector') as probe:
            code, result, _ = self.request(self.prefix + '/diagnostics')
            self.assertEqual(code, 200)
            collector.assert_not_called(); probe.assert_not_called()
        self.assertEqual(manager.snapshot(), before)
        self.assertEqual(result['summary'], 'paused')
        self.assertTrue(result['current'])

    def test_refresh_requires_versions_and_does_not_select_or_start(self):
        manager = self.server.environment_manager
        before = manager.snapshot()
        for payload in ({}, {'expected_session': before['session_id']},
                        {'expected_session': before['session_id'], 'expected_revision': True},
                        {'expected_session': None, 'expected_revision': before['revision']}):
            self.assertEqual(self.request(self.prefix + '/diagnostics/refresh', payload)[0], 400)
        payload = {'expected_session': before['session_id'], 'expected_revision': before['revision']}
        self.assertEqual(self.request(self.prefix + '/diagnostics/refresh', {**payload, 'expected_revision': 0})[0], 409)
        self.assertEqual(self.request(self.prefix + '/diagnostics/refresh', {**payload, 'expected_session': 'old'})[0], 409)
        with patch.object(manager, 'select') as select, patch.object(self.server.controller, 'start') as start:
            code, result, _ = self.request(self.prefix + '/diagnostics/refresh', payload)
            self.assertEqual(code, 202)
            self.assertTrue(result['accepted']); self.assertTrue(result['refresh_id'])
            select.assert_not_called(); start.assert_not_called()
        self.assertEqual(manager.snapshot(), before)
        self.assert_idle()

    def test_all_diagnostic_and_sse_routes_keep_security_guards(self):
        for path, data in ((self.prefix + '/diagnostics', None), (self.prefix + '/diagnostics/refresh', {}), ('/api/events', None)):
            self.assertEqual(self.request(path, data, authorization=False)[0], 401)
            self.assertEqual(self.request(path, data, authorization='Bearer incorrect')[0], 401)
            self.assertEqual(self.request(path, data, headers={'Host': 'attacker.invalid'})[0], 403)
            self.assertEqual(self.request(path, data, headers={'Origin': 'http://attacker.invalid'})[0], 403)

    def test_bad_cursor_is_400_before_sse_headers(self):
        for cursor in ('invalid', 's:0', 's:-1', 's:1x', 's:' + '9' * 129):
            with self.assertRaises(HTTPError) as caught:
                self.stream(cursor)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=3)
        connection.putrequest('GET', '/api/events')
        connection.putheader('Authorization', 'Bearer ' + self.token)
        connection.putheader('Last-Event-ID', 's:1')
        connection.putheader('Last-Event-ID', 's:2')
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 400)
        connection.close()

    def test_replay_keeps_exact_ids_and_cross_stream_reset_is_visible(self):
        bus = self.server.controller.runtime.event_bus
        first = bus.emit('test', 'first warning', severity='warning')
        second = bus.emit('test', 'second warning', severity='warning')
        with self.stream(first.data['log']['event_id']) as response:
            frame = self.frame(response)
            self.assertEqual(frame['id'], second.data['log']['event_id'])
            self.assertEqual(frame['data']['message'], 'second warning')
            self.assertEqual(frame['data']['data']['log']['level'], 'WARNING')
        with self.stream('other-instance:1') as response:
            frame = self.frame(response)
            self.assertEqual(frame['event'], 'log_control')
            self.assertEqual(frame['data']['kind'], 'reset')
            self.assertNotIn('id', frame)

    def test_sse_and_status_preserve_decision_whitelist_and_redaction(self):
        bus = self.server.controller.runtime.event_bus
        before = bus.emit('test', 'cursor', severity='warning')
        notice = bus.emit('decision_changed', 'decision changed', request_id='r', snapshot_id='s', sequence=1, kind='updated',
                          private_payload='marker-must-not-escape', error=self.token)
        with self.stream(before.data['log']['event_id']) as response:
            frame = self.frame(response)
            self.assertEqual(frame['id'], notice.data['log']['event_id'])
            self.assertEqual(set(frame['data']['data']), {'request_id', 'snapshot_id', 'sequence', 'kind'})
            self.assertNotIn(self.token, json.dumps(frame))
        code, status, _ = self.request('/api/status')
        self.assertEqual(code, 200)
        protected = [event for event in status['recent_events'] if event['type'] == 'decision_changed'][0]
        self.assertEqual(protected['event_id'], notice.data['log']['event_id'])
        self.assertEqual(set(protected['data']), {'request_id', 'snapshot_id', 'sequence', 'kind'})
        self.assertNotIn('marker-must-not-escape', json.dumps(status))

    def test_large_unicode_payload_is_bounded_and_does_not_change_bus(self):
        bus = self.server.controller.runtime.event_bus
        before = bus.emit('test', 'cursor', severity='warning')
        raw = '图' * 100000
        event = bus.emit('test', 'warning', severity='warning', detail=raw)
        with self.stream(before.data['log']['event_id']) as response:
            frame = self.frame(response)
            self.assertEqual(frame['id'], event.data['log']['event_id'])
            self.assertLessEqual(len(json.dumps(frame['data'], ensure_ascii=False).encode('utf-8')), 65536)
        self.assertEqual(event.data['detail'], raw)

    def test_closed_diagnostics_returns_503(self):
        self.server.environment_manager.close_diagnostics()
        self.assertEqual(self.request(self.prefix + '/diagnostics')[0], 503)
        self.assertEqual(self.request(self.prefix + '/diagnostics/refresh', {})[0], 503)

    def test_real_port_warning_and_recovery_cross_authenticated_sse(self):
        manager = self.server.environment_manager
        class Adapter(FakeAdapter):
            def attach(self, port, generation):
                super().attach(port, generation)
                if port.native is not None:
                    port.binding_generation += 1
                    port.mark_bound()
                    port.graph_peers = port.matched = 1
        manager._adapter_factory = lambda mode, config: Adapter()
        facade = PluginRosFacade(plugin_id='test_camera', environment_manager=manager, ports=[{
            'id': 'image', 'direction': 'subscribe', 'message_types': ['std_msgs/msg/String'], 'default_topic': '/camera',
            'diagnostics': {'mode': 'periodic', 'startup_grace_sec': 0, 'stale_after_sec': 5}}])
        port = facade.subscribe('image')._port
        finish(manager, 'ros2')
        self.server.controller.runtime.state = RuntimeState.RUNNING
        self.server.controller.runtime.event_bus.emit('runtime_state', 'test running', state='running')
        service = manager._diagnostics
        service._run_cycle()
        issue = next(item for item in service.snapshot()['active_issues'] if item['code'] == 'ROS_RX_NO_DATA_WITH_PEER')
        self.assertEqual(self.request(self.prefix + '/ros2/endpoints')[1]['received'][0]['data_health'], 'no_data')
        event = next(event for event in self.server.controller.runtime.event_bus.recent(1000)
                     if event.type == 'ros_diagnostic' and event.data.get('code') == issue['code'])
        port.receive(Message(), port.generation, port.binding_generation)
        service._run_cycle(); service._run_cycle()
        with self.stream(event.data['log']['event_id']) as response:
            for _ in range(20):
                frame = self.frame(response)
                payload = frame['data']
                if payload.get('type') == 'ros_diagnostic' and payload['data'].get('recovered_code') == issue['code']:
                    self.assertEqual(payload['data']['scope'], issue['scope'])
                    self.assertEqual(payload['data']['log']['level'], 'INFO')
                    break
            else: self.fail('no authenticated recovery event')
