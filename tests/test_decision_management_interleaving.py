"""Event-controlled management intent and generation isolation, without weights."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.decision.backends.laya import LayaBackend, LayaConfig, LayaBackendError
from astrbot_ex.core.decision.catalog import CapabilityInput
from astrbot_ex.core.decision.management import ManagementSettings, ManagedLayaBackend
from astrbot_ex.core.decision.owned_laya import Deployment, OwnedLayaService, fixed_warmup_snapshot
from astrbot_ex.core.actions.ledger import OwnerBinding
from astrbot_ex.core.actions.models import parse_action_manifest
from astrbot_ex.core.plugin_actor import PluginActor
from tests import test_laya_backend as laya_fixture
from tests import test_goal_manager as goal_fixture
from tests import test_decision_service as decision_fixture
import test_decision_management_http as http_fixture


class ProcessFixture:
    next_pid = 91000
    def __init__(self):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.returncode = None
    def poll(self):
        return self.returncode
    def terminate(self):
        self.returncode = -15
    def kill(self):
        self.returncode = -9
    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired('owned-fixture', timeout)
        return self.returncode


class ProbeFixture:
    def __init__(self, config=None):
        self.config = config
        self.cancelled = threading.Event()
    def probe(self):
        return {'ok': True, 'health': laya_fixture.health()}
    def status(self):
        return {'busy': False, 'restart_required': False}
    def cancel(self):
        self.cancelled.set()
    def close(self):
        self.cancelled.set()


class DecisionManagementInterleavingTests(http_fixture.ManagementHTTPFixture, unittest.TestCase):
    def setUp(self):
        import tempfile
        from urllib.request import build_opener, ProxyHandler
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.processes, self.actors, self.post_calls = [], [], []
        self.warmup_blocked = False
        self.warmup_entered = threading.Event()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        deployment = Deployment(Path(sys.executable), self.root / 'cache', self.root / 'logs',
            port=port, device='cpu', state_path=self.root / 'execution/laya/service-state.json',
            terminate_timeout_s=.2, kill_timeout_s=.2)
        def processes(*args, **kwargs):
            process = ProcessFixture()
            self.processes.append(process)
            return process
        def warmup(backend):
            if self.warmup_blocked:
                self.warmup_entered.set()
                self.assertTrue(backend.cancelled.wait(2), 'latched warmup not interrupted')
            return {'fixture': True}
        def factory(deployment):
            return OwnedLayaService(deployment, process_factory=processes,
                probe_factory=ProbeFixture, warmup=warmup)
        def transport(manager, generation):
            def request(method, path, body, deadline, cancel, max_bytes):
                if method == 'GET':
                    return laya_fixture.reply(laya_fixture.health())
                self.post_calls.append({'generation': generation, 'body': body})
                raise LayaBackendError('deadline_exceeded')
            return request
        settings = ManagementSettings(laya_deployment=deployment, allow_test_execution=True,
            test_isolation=True, laya_service_factory=factory, laya_transport_factory=transport)
        with patch.dict(os.environ, {'ASTRBOTEX_DATA_DIR': str(self.root),
                'ASTRBOTEX_STT_ENABLED': '', 'ASTRBOTEX_TTS_ENABLED': ''}):
            self.server = build_server('127.0.0.1', 0, 20, management_settings=settings)
        self.token = self.server.decision_management.credential_path.read_text().strip()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            for actor in self.actors:
                actor.stop(2)

    def begin(self, suffix, data=None):
        code, accepted, _ = self.write(suffix, data)
        self.assertEqual(code, 202)
        return accepted

    def laya_config(self):
        config = self.get_config()
        saved = copy.deepcopy(config['saved'])
        saved['backend'] = 'laya'
        saved['laya'].update(enabled=True, allow_live_http=True)
        self.assertEqual(self.write('/config', {'config': saved}, version=config)[0], 200)

    def actor(self):
        original = goal_fixture.make_catalog().snapshot().entries[0]
        self.server.capability_catalog.refresh([CapabilityInput('arm', 1, parse_action_manifest(original['manifest'], owner='arm'), {},
            {'status': 'available', 'reason': '', 'text': 'Use safety checks',
             'content_hash': hashlib.sha256(b'Use safety checks').hexdigest()}, True, '1')])
        owner = decision_fixture.ActionOwner('arm', self.server.action_dispatcher)
        actor = PluginActor(owner)
        actor.start()
        self.actors.append(actor)
        self.server.action_dispatcher.register_owner(OwnerBinding('arm', 1), actor, original['manifest'])
        self.server.action_service.control_mode = 'decision'
        self.server.action_service.update_versions(runtime_state='running')
        self.assertTrue(decision_fixture.wait_for(lambda:
            self.server.decision_service._last_catalog_revision == self.server.capability_catalog.snapshot().revision))
        return owner

    def test_duplicate_start_reuses_intent_and_stop_revokes_latched_warmup(self):
        self.warmup_blocked = True
        first = self.begin('/service/start')
        self.assertTrue(self.warmup_entered.wait(2))
        second = self.begin('/service/start')
        self.assertEqual(first['operation_id'], second['operation_id'])
        stopped = self.begin('/stop')
        self.assertFalse(self.server.decision_service.status()['gate_open'])
        self.assertEqual(self.operation(stopped)['state'], 'succeeded')
        self.assertEqual(self.operation(first)['state'], 'superseded')
        self.assertEqual(len(self.processes), 1)
        self.assertIsNotNone(self.processes[0].poll())
        self.assertEqual(self.server.decision_management.laya.status()['state'], 'stopped')
        self.assert_idle()

    def test_new_stop_wins_over_backend_apply_latched_before_trusted_replacement(self):
        entered, release = threading.Event(), threading.Event()
        original = self.server.decision_service.replace_backend
        def parked(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return original(*args, **kwargs)
        try:
            with patch.object(self.server.decision_service, 'replace_backend', side_effect=parked):
                old = self.begin('/mode', {'mode': 'shadow'})
                self.assertTrue(entered.wait(2))
                stop = self.begin('/stop')
                self.assertEqual(self.server.decision_service.mode, 'disabled')
                self.assertFalse(self.server.decision_service.status()['gate_open'])
                release.set()
                self.assertEqual(self.operation(stop)['state'], 'succeeded')
                self.assertEqual(self.operation(old)['state'], 'superseded')
        finally:
            release.set()
        self.assert_idle()

    def test_post_timeout_quarantine_crosses_instances_and_http_recovery_requires_new_goal(self):
        owner = self.actor()
        self.laya_config()
        self.assertEqual(self.operation(self.begin('/service/start'))['state'], 'succeeded')
        self.assertEqual(self.operation(self.begin('/mode', {'mode': 'execute'}))['state'], 'succeeded')
        manager = self.server.decision_management.laya
        old_generation = manager.generation
        service = self.server.decision_service
        service.submit_goal(goal_fixture.goal_payload(service.goals, 1))
        self.assertTrue(decision_fixture.wait_for(lambda: manager.status()['restart_required']))
        self.assertEqual(len(self.post_calls), 1)
        self.assertEqual(owner.commands, [])
        fresh = LayaBackend(LayaConfig(enabled=True), transport=lambda *args: self.fail('quarantine emitted HTTP'))
        try:
            guarded = ManagedLayaBackend(fresh, manager, old_generation)
            with self.assertRaises(Exception) as error:
                guarded.decide(fixed_warmup_snapshot())
            self.assertEqual(getattr(error.exception, 'code', None), 'restart_required')
            self.assertIsNone(fresh.last_record)
        finally:
            fresh.close()
        self.assertEqual(self.operation(self.begin('/stop'))['state'], 'succeeded')
        code, value, _ = self.write('/mode', {'mode': 'execute'})
        self.assertEqual(code, 409)
        self.assertEqual(value['code'], 'restart_required')
        recovered = self.operation(self.begin('/service/recover'))
        self.assertEqual(recovered['state'], 'succeeded')
        self.assertTrue(recovered['result']['old_process_exit']['exit_confirmed'])
        self.assertIsNotNone(self.processes[0].poll())
        self.assertNotEqual(old_generation, manager.generation)
        self.assertEqual(service.mode, 'disabled')
        self.assertIsNone(service.goals.active)
        self.assertIsNone(service.goals.pending_replace)
        self.assertEqual(len(self.post_calls), 1)
        self.assertEqual(owner.commands, [])
        self.assertEqual(self.server.action_ledger.list_commands().result(1), ())
        self.assertTrue(recovered['result']['new_goal_required'])
        self.assertFalse(recovered['result']['replayed'])


if __name__ == '__main__':
    unittest.main()
