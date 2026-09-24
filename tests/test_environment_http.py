import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from astrbot_ex.core.api_server import build_server
from test_environments import FakeAdapter


class EnvironmentHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'ASTRBOTEX_DATA_DIR':self.temp.name,
                                     'ASTRBOTEX_STT_ENABLED':'','ASTRBOTEX_TTS_ENABLED':''}):
            self.server = build_server('127.0.0.1', 0, 20)
        self.manager = self.server.environment_manager
        self.manager._adapter_factory = lambda mode, config: FakeAdapter()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.controller.stop('test shutdown')
        self.manager.close()
        self.server.connections.close()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, suffix='', data=None):
        request = Request(self.url + '/api/v1/ex/environments' + suffix,
                          data=json.dumps(data).encode() if data is not None else None,
                          headers={'Content-Type':'application/json'})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_operation_status_revision_conflicts_and_unknown_operation(self):
        code, original = self.request()
        self.assertEqual(code, 200)
        state = original['environment']
        code, accepted = self.request('/select', {'mode':'ros2','expected_revision':state['revision'],
                                                 'expected_session':state['session_id']})
        self.assertEqual(code, 202)
        self.manager._worker.join(2)
        code, operation = self.request('/operations/' + accepted['operation_id'])
        self.assertEqual(operation['operation']['phase'], 'completed')
        code, conflict = self.request('/select', {'mode':'normal','expected_revision':state['revision']})
        self.assertEqual(code, 409)
        self.assertEqual(conflict['code'], 'revision_conflict')
        self.assertEqual(self.request('/operations/absent')[0], 404)
        self.assertEqual(self.request('/ros2/interfaces/check', {'message_type':'not-a-type'})[0], 400)
        code, graph = self.request('/ros2/graph')
        self.assertEqual(graph['environment']['generation'], self.manager.snapshot()['generation'])

    def test_navigation_and_invalid_config_do_not_change_runtime(self):
        for route in ('/', '/environments.js', '/index.html'):
            with urlopen(self.url + route, timeout=5) as response:
                self.assertEqual(response.status, 200)
        self.assertEqual(self.manager.snapshot()['active_mode'], 'normal')
        self.assertFalse(self.manager.config_path.exists())
        self.assertEqual(self.request('/ros2/config', {'config':{'domain_id':True}})[0], 400)
        self.assertEqual(self.request('/select', {'mode':'unsupported'})[0], 400)
        self.assertFalse(self.manager.config_path.exists())


if __name__ == '__main__':
    unittest.main()
