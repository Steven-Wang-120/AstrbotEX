"""Explicit IP Web UI deployment keeps the management security contract."""
import http.client
import unittest
from unittest.mock import patch
from urllib.request import Request
from urllib.error import HTTPError
from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.decision.management import ManagementSettings, management_public_address
from tests.test_decision_management_http import ManagementHTTPFixture

ORIGIN = 'http://100.95.122.29:8765'

class ManagementNetworkConfigurationTests(unittest.TestCase):
    def test_default_and_mismatched_non_loopback_bindings_are_rejected(self):
        for host, port, settings in [('100.95.122.29',8765,None),
                ('0.0.0.0',8765,ManagementSettings(public_origin=ORIGIN)),
                ('100.95.122.30',8765,ManagementSettings(public_origin=ORIGIN)),
                ('100.95.122.29',8766,ManagementSettings(public_origin=ORIGIN))]:
            with self.subTest(host=host,port=port):
                with self.assertRaises(ValueError):build_server(host,port,20,management_settings=settings)

    def test_public_origin_requires_an_exact_concrete_address(self):
        self.assertEqual(management_public_address(ORIGIN),('100.95.122.29',8765))
        for value in ['', 'http://0.0.0.0:8765', 'http://224.0.0.1:8765', 'http://100.95.122.29',
                'http://100.95.122.29:0', 'http://100.95.122.29:8765/', 'https://100.95.122.29:8765',
                ORIGIN+'?token=secret', ORIGIN+'#fragment','http://user:secret@100.95.122.29:8765',
                'http://robot.example:8765',True]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):management_public_address(value)

    def test_matching_binding_reaches_the_listener_without_relaxing_default(self):
        # Exercise the real server construction and cleanup, substituting only
        # the address because this workstation does not own the device IP.
        import tempfile, os
        from astrbot_ex.core.api_server import AstrBotEXHTTPServer
        requested = []
        def local_listener(address, handler):
            requested.append(address)
            return AstrBotEXHTTPServer(('127.0.0.1', 0), handler)
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'ASTRBOTEX_DATA_DIR':root}), \
             patch('astrbot_ex.core.api_server.AstrBotEXHTTPServer',side_effect=local_listener):
            server = build_server('100.95.122.29',8765,20,management_settings=ManagementSettings(public_origin=ORIGIN))
            try:
                self.assertEqual(requested,[('100.95.122.29',8765)])
            finally:
                server.server_close()

class ManagementNetworkHTTPTests(ManagementHTTPFixture,unittest.TestCase):
    def setUp(self):
        with patch('tests.test_decision_management_http.ManagementSettings',
                   return_value=ManagementSettings(public_origin=ORIGIN)):
            super().setUp()
        self.headers={'Host':'100.95.122.29:8765','Origin':ORIGIN}

    def test_trusted_ip_host_supports_authenticated_reads_refresh_and_sse(self):
        code,state,_=self.request('/api/v1/ex/environments/diagnostics',headers=self.headers)
        self.assertEqual(code,200)
        env=state['environment']
        self.assertEqual(self.request('/api/v1/ex/environments/diagnostics/refresh',
            {'expected_session':env['session_id'],'expected_revision':env['revision']},headers=self.headers)[0],202)
        self.server.controller.runtime.event_bus.emit('test','remote visible warning',severity='warning')
        with self.opener.open(Request(self.base+'/api/events',headers={**self.headers,'Authorization':'Bearer '+self.token}),timeout=3) as response:
            self.assertEqual(response.status,200)
            self.assertIn(b'id:',response.readline())
        self.assert_idle()

    def test_missing_and_wrong_credentials_remain_401_on_get_post_sse(self):
        for path,data in [('/api/status',None),('/api/events',None),
                ('/api/v1/ex/environments/diagnostics/refresh',{})]:
            for credential in [False,'Bearer invalid-test-only']:
                self.assertEqual(self.request(path,data,authorization=credential,headers=self.headers)[0],401)
        self.assert_idle()

    def test_other_hosts_ports_and_cross_origin_are_rejected(self):
        for headers in [{'Host':'100.95.122.30:8765'}, {'Host':'100.95.122.29:8766'},
                {'Host':'100.95.122.29:8765.evil'}, {**self.headers,'Origin':'http://attacker.invalid'},
                {**self.headers,'Origin':'null'}]:
            self.assertEqual(self.request('/api/status',headers=headers)[0],403)
        self.assert_idle()

    def test_duplicate_security_headers_are_rejected_with_public_host(self):
        for name,value in [('Host','100.95.122.29:8765'),('Origin',ORIGIN),('Authorization','Bearer '+self.token)]:
            connection=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=3)
            connection.putrequest('GET','/api/status',skip_host=True)
            for key,item in {**self.headers,'Authorization':'Bearer '+self.token}.items():connection.putheader(key,item)
            connection.putheader(name,value);connection.endheaders()
            self.assertEqual(connection.getresponse().status,400);connection.close()
        self.assert_idle()
