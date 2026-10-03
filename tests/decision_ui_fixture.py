"""B09 browser fixture: real B08 HTTP, existing fake Laya child, isolated test Actor.

stdin JSON commands are test orchestration, never exposed as dashboard endpoints.
No model inference, robot plugins, GPU, or new production composition.
"""
from __future__ import annotations

import copy
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.actions.models import ActionStatus
from astrbot_ex.core.actions.ledger import OwnerBinding, StopEvidence
from astrbot_ex.core.decision.management import ManagementSettings
from astrbot_ex.core.decision.owned_laya import Deployment, OwnedLayaService
from scripts.verify_decision_management import compose_actor, until
from tests.test_decision_management_http import ManagementHTTPFixture
from tests.test_goal_manager import goal_payload
from tests.test_laya_backend import health, reply
from tests.test_owned_laya import FakeBackend, FakeProcess


class BrowserFixture(ManagementHTTPFixture, unittest.TestCase):
    def setUp(self):
        self.deployment_temp = tempfile.TemporaryDirectory(prefix='b09-owned-fixture-')
        root = Path(self.deployment_temp.name)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.release = threading.Event()
        self.release.set()
        self.actor = self.owner = None
        self.goal_number = 0

        def warmup(backend):
            if not self.release.wait(30):
                raise RuntimeError('fixture_load_timeout')
            return {'test_fixture': True, 'inference_called': False}

        def factory(deployment):
            return OwnedLayaService(deployment, process_factory=lambda *a, **kw: FakeProcess(),
                                    probe_factory=FakeBackend, warmup=warmup)

        def transport(method, path, *a, **kw):
            if method != 'GET' or path != '/health':
                raise AssertionError('B09 fixture must not perform model inference')
            return reply(health())

        self.settings = ManagementSettings(laya_deployment=Deployment(Path(sys.executable), root/'cache',
            root/'logs', port=port, device='cpu', state_path=root/'execution/laya/service-state.json'),
            laya_service_factory=factory, laya_transport_factory=lambda manager, generation: transport, operation_timeout_s=4)
        with patch('tests.test_decision_management_http.ManagementSettings', return_value=self.settings):
            super().setUp()

    def tearDown(self):
        self.release.set()
        if self.owner:
            for row in self.server.action_ledger.list_commands().result(1):
                if row.status in {ActionStatus.UNKNOWN, ActionStatus.TIMED_OUT, ActionStatus.FAILED}:
                    binding=OwnerBinding(row.owner,row.generation)
                    if self.server.action_ledger.stop_proof(row.command_id,binding).result(1) is None:
                        self.server.action_ledger.reconcile_stop(row.command_id,binding,
                            StopEvidence(row.command_id,True,'arm','fixture-cleanup')).result(1)
        super().tearDown()
        if self.actor:
            self.actor.stop()
        self.deployment_temp.cleanup()

    def run_operation(self, path, payload=None):
        status, value, _ = self.write(path, payload)
        self.assertEqual(status, 202, value)
        result = self.operation(value)
        self.assertEqual(result['state'], 'succeeded', result)
        return result

    def actor_goal(self, finish=True):
        if self.actor is None:
            self.owner, self.actor, _ = compose_actor(self.server)
        self.run_operation('/mode', {'mode': 'execute'})
        self.goal_number += 1
        count = len(self.owner.commands)
        self.owner.started.clear()
        service = self.server.decision_service
        service.submit_goal(goal_payload(service.goals, self.goal_number,
            goal_text_en='TEST FIXTURE: move safely; not a robot task.',
            completion={'required_success_actions': ['arm.move.v1']}))
        self.assertTrue(self.owner.started.wait(3), 'test Actor not called')
        command = self.owner.commands[count]
        until(lambda: self.server.action_ledger.get(command.command_id).result(1).status == ActionStatus.RUNNING,
              'test_actor_running_missing')
        if finish:
            self.server.action_dispatcher.report(command.command_id, OwnerBinding('arm', 1),
                ActionStatus.SUCCEEDED, details={'test_actor_only': True}).result(1)
            self.run_operation('/stop')
        return command.command_id

    def command(self, request):
        kind = request['command']
        management = self.server.decision_management
        if kind == 'info':
            return {'base': self.base, 'token_file': str(self.root/'secrets/admin.token'),
                    'scope': 'real B08 HTTP; fake owned Laya; isolated test Actor only'}
        if kind == 'hold':
            self.release.clear()
        elif kind == 'release':
            self.release.set()
        elif kind == 'quarantine':
            management.laya.quarantine(management.laya.generation, 'deadline_exceeded')
        elif kind == 'change_config':
            version = self.get_config()
            value = copy.deepcopy(version['saved'])
            value['mock']['kind'] = request.get('kind', 'request_replan')
            status, result, _ = self.write('/config', {'config': value}, version=version)
            self.assertEqual(status, 200, result)
            return {'revision': result['revision']}
        elif kind == 'seed_actions':
            version = self.get_config()
            value = copy.deepcopy(version['saved']);value['backend']='mock';value['mock']['kind']='start'
            self.assertEqual(self.write('/config', {'config': value})[0], 200)
            return {'command_ids': [self.actor_goal() for _ in range(request.get('count', 21))]}
        elif kind == 'active_actor':
            cid = self.actor_goal(False)
            self.owner.stop_proof = request.get('stop_proof', True)
            if not self.owner.stop_proof:
                self.server.action_dispatcher.report(cid, OwnerBinding('arm', 1), ActionStatus.FAILED,
                    details={'test_actor_only': True}).result(1)
            return {'command_id': cid}
        elif kind == 'prove_failed':
            cid = self.owner.commands[-1].command_id
            self.server.action_ledger.reconcile_stop(cid, OwnerBinding('arm', 1),
                StopEvidence(cid, True, 'arm', 'fixture-proof')).result(1)
            return {'command_id': cid}
        elif kind == 'seed_history':
            for n in range(22):
                sid = 'TEST-FIXTURE-snapshot-'+str(n)
                now = time.monotonic_ns()
                management.history.consume({'kind':'prepared','snapshot_id':sid,'time_ns':now,
                    'snapshot':{'versions':{'ex_session':management.store.ex_session}, 'fixture':True},
                    'backend':'jev' if n == 21 else 'fixture', 'backend_type':'TEST FIXTURE'})
                management.history.consume({'kind':'backend_returned','snapshot_id':sid,'time_ns':now+2000000,
                    'result':{'choices':[{'kind':'start','test_fixture':True}], 'elapsed_ms':2,
                              'unsafe_text':'<img src=x onerror="window.b09Injected=true">',
                              **({'large':'fixture '*18000} if n == 20 else {})},
                    'record':{'model':'TEST FIXTURE', 'revision':'fixture-v1','post_attempted':True,
                              'post_written_to_socket':None, **({'raw_response':{'fixture':True}} if n != 21 else {})}})
                management.history.consume({'kind':'outcome','snapshot_id':sid,'time_ns':now+3000000,
                    'outcome':'discarded', 'reason_code':'fixture_old_result','details':{'commands':[]}})
            return {'fixture_records':22}
        elif kind == 'notify':
            for _ in range(request.get('count', 1)):
                self.server.controller.runtime.event_bus.emit('decision_changed', 'B09 TEST FIXTURE')
        elif kind == 'restart':
            self.release.set()
            if self.actor:
                self.actor.stop(); self.actor=self.owner=None
            port = self.server.server_address[1]
            self.server.shutdown();self.thread.join(3);self.server.server_close()
            with patch.dict(os.environ, {'ASTRBOTEX_DATA_DIR':str(self.root),
                                         'ASTRBOTEX_STT_ENABLED':'','ASTRBOTEX_TTS_ENABLED':''}):
                self.server=build_server('127.0.0.1',port,20,management_settings=self.settings)
            self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01})
            self.thread.start()
            return {'ex_session': self.server.decision_management.store.ex_session}
        else:
            raise ValueError('unknown fixture command')
        return {'ok': True}


def main():
    fixture=BrowserFixture(); fixture.setUp()
    try:
        print(json.dumps({'ready': fixture.command({'command':'info'})}), flush=True)
        for line in sys.stdin:
            request=json.loads(line)
            if request['command']=='quit':
                break
            try:
                result={'id':request['id'],'result':fixture.command(request)}
            except Exception as exc:
                result={'id':request['id'],'error':str(exc)}
            print(json.dumps(result), flush=True)
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
