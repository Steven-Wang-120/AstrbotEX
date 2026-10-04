"""One explicit B08 management loop; real Laya, isolated test Actor, no robot."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import threading
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.actions.ledger import OwnerBinding
from astrbot_ex.core.actions.models import ActionStatus, parse_action_manifest
from astrbot_ex.core.decision.catalog import CapabilityInput
from astrbot_ex.core.decision.management import ManagementSettings, ManagedLayaBackend
from astrbot_ex.core.decision.owned_laya import Deployment, fixed_warmup_snapshot
from astrbot_ex.core.decision.backends.laya import LayaBackend, LayaConfig, _http_transport
from astrbot_ex.core.plugin_actor import PluginActor
from tests.test_decision_service import ActionOwner
from tests.test_goal_manager import make_catalog, goal_payload


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n')


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def until(predicate, reason, timeout=5):
    deadline = time.monotonic() + timeout
    event = threading.Event()
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        event.wait(.005)
    raise RuntimeError(reason)


class HTTP:
    def __init__(self, base, token, evidence):
        self.base, self.token, self.evidence = base, token, evidence
        self.opener = build_opener(ProxyHandler({}))

    def request(self, suffix, data=None, *, auth=True):
        path = suffix if suffix.startswith('/api/') else '/api/v1/ex/decision' + suffix
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer ' + self.token
        body = None if data is None else json.dumps(data).encode()
        started = time.monotonic_ns()
        request = Request(self.base + path, data=body, headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            payload = json.loads(response.read())
            operation = payload.get('operation', {})
            if not '/operations/' in path or operation.get('state') not in ('pending', 'running'):
                self.evidence.append({'path': path, 'method': request.get_method(), 'status': response.status,
                    'started_ns': started, 'returned_ns': time.monotonic_ns(), 'response': payload})
            return response.status, payload

    def get(self, suffix):
        code, value = self.request(suffix)
        require(code == 200, 'management_get_failed_' + str(code))
        return value

    def post(self, suffix, data=None, *, allow_error=False):
        config = self.get('/config')
        code, value = self.request(suffix, {'ex_session': config['ex_session'],
            'expected_revision': config['revision'], **(data or {})})
        if not allow_error:
            require(code in (200, 202), 'management_post_failed_' + suffix + '_' + str(code))
        return code, value

    def operation(self, accepted, *, timeout=180, expected='succeeded'):
        oid = accepted['operation_id']
        operation = until(lambda: self._terminal(oid), 'management_operation_deadline_' + oid, timeout)
        if expected is not None:
            require(operation['state'] == expected,
                'management_operation_' + operation['kind'] + '_' + operation['state'] + '_' + str(operation['error_code']))
        return operation

    def _terminal(self, oid):
        value = self.get('/operations/' + oid)['operation']
        if value['state'] in ('pending', 'running'):
            threading.Event().wait(.1)
        return value if value['state'] not in ('pending', 'running') else None

    def run(self, suffix, data=None, **kwargs):
        code, value = self.post(suffix, data)
        require(code == 202, 'operation_not_accepted_' + suffix)
        return self.operation(value, **kwargs)


class PostFault:
    """Trusted test injection into the default transport, after actual socket write."""
    def __init__(self):
        self.armed = False
        self.lock = threading.Lock()
        self.events = []
        self.triggered = threading.Event()

    def factory(self, manager, generation):
        def transport(method, path, body, deadline, cancel, max_bytes):
            def written():
                with self.lock:
                    if not self.armed:
                        return
                    self.armed = False
                    owned = manager.owned_process_handle(expected_generation=generation)
                    require(owned.poll() is None, 'fault_owned_process_already_exited')
                    record = {'generation': generation, 'owned_pid': owned.pid,
                        'post_written_monotonic_ns': time.monotonic_ns(),
                        'request_sha256': hashlib.sha256(body).hexdigest(),
                        'fault': 'SIGSTOP via saved owned Popen handle', 'method': method, 'path': path}
                    owned.send_signal(signal.SIGSTOP)
                    self.events.append(record)
                    self.triggered.set()
            return _http_transport(manager.deployment.port, method, path, body, deadline, cancel,
                                   max_bytes, on_written=written if method == 'POST' else None)
        return transport


def compose_actor(server):
    fixture = make_catalog()
    entry = fixture.snapshot().entries[0]
    server.capability_catalog.refresh([CapabilityInput('arm', 1, parse_action_manifest(entry['manifest'], owner='arm'), {},
        {'status': 'available', 'reason': '', 'text': 'Use safety checks',
         'content_hash': hashlib.sha256(b'Use safety checks').hexdigest()}, True, '1')])
    owner = ActionOwner('arm', server.action_dispatcher)
    accepted = {}
    original = owner.on_action_command
    def command(command):
        result = original(command)
        accepted[command.command_id] = time.monotonic_ns()
        server.action_dispatcher.report(command.command_id, OwnerBinding('arm', 1), ActionStatus.RUNNING,
                                        details={'test_actor_only': True})
        return result
    owner.on_action_command = command
    actor = PluginActor(owner)
    actor.start()
    server.action_dispatcher.register_owner(OwnerBinding('arm', 1), actor, entry['manifest'])
    server.action_service.control_mode = 'decision'
    # Explicit trusted test composition. No runtime.start or robot plugins are involved.
    server.action_service.update_versions(runtime_state='running')
    until(lambda: server.decision_service._last_catalog_revision == server.capability_catalog.snapshot().revision,
          'composition_catalog_not_ready')
    return owner, actor, accepted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8769)
    parser.add_argument('--python', type=Path, default=Path(os.environ.get('ASTRBOTEX_LAYA_PYTHON', str(Path(__file__).resolve().parents[1] / 'runtime/laya/.venv/bin/python'))))
    parser.add_argument('--cache', type=Path, default=Path('/data/shared/AstrEX_project_data/models/pretrained/laya/hub'))
    parser.add_argument('--device', default='cuda')
    args = parser.parse_args()
    require(not args.output.exists(), 'verification_output_must_be_fresh')
    args.output.mkdir(mode=0o700, parents=True)
    root = args.output / 'isolated-instance'
    root.mkdir(mode=0o700)
    fault = PostFault()
    result = {'scope': 'real management HTTP and isolated test Actor; no ROS or robot',
        'B07_known_quality': {'replan_correct': 0, 'trials': 8, 're_evaluated': False},
        'http': [], 'checks': [], 'cases': [], 'faults': fault.events, 'pass': False}
    server = thread = actor = None
    deployment = Deployment(args.python, args.cache, args.output / 'owned-laya', port=args.port,
        device=args.device, state_path=root / 'execution/laya/service-state.json',
        terminate_timeout_s=2, kill_timeout_s=2)
    try:
        settings = ManagementSettings(laya_deployment=deployment, allow_test_execution=True,
            test_isolation=True, laya_transport_factory=fault.factory)
        with patch.dict(os.environ, {'ASTRBOTEX_DATA_DIR': str(root), 'ASTRBOTEX_STT_ENABLED': '', 'ASTRBOTEX_TTS_ENABLED': ''}):
            server = build_server('127.0.0.1', 0, 20, management_settings=settings)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        token = server.decision_management.credential_path.read_text().strip()
        http = HTTP('http://127.0.0.1:' + str(server.server_address[1]), token, result['http'])
        result['base'] = http.base
        owner, actor, accepted = compose_actor(server)
        require(http.request('/status', auth=False)[0] == 401, 'missing_credential_not_rejected')
        config = http.get('/config')['saved']
        config.update(backend='laya')
        config['laya'].update(enabled=True, allow_live_http=True)
        http.post('/config', {'config': config})
        require(server.decision_management.laya.status()['state'] == 'stopped', 'save_started_model')
        result['checks'].append('save did not start process, runtime, mode or Goal')
        result['start'] = http.run('/service/start')
        require(server.decision_service.mode == 'disabled', 'start_opened_execution')
        result['probe'] = http.run('/test')
        require(result['probe']['result']['inference_called'] is False, 'test_created_inference')
        result['shadow_mode'] = http.run('/mode', {'mode': 'shadow'})
        service = server.decision_service
        service.submit_goal(goal_payload(service.goals, 1, parameters={'arm.move.v1': {'meters': 1}}))
        until(lambda: any(d['outcome'] == 'shadow' for d in service.status()['decisions']), 'shadow_decision_missing')
        result['shadow_stop'] = http.run('/stop')
        require(not owner.commands and not server.action_ledger.list_commands().result(1), 'shadow_dispatched_action')
        result['cases'].append({'kind': 'shadow', 'actions': http.get('/actions'), 'decisions': http.get('/decisions')})
        result['checks'].append('real model shadow choice did not dispatch or write Ledger action')

        def execute(n, label):
            mode = http.run('/mode', {'mode': 'execute'})
            owner.started.clear()
            before = len(owner.commands)
            submitted = time.monotonic_ns()
            service.submit_goal(goal_payload(service.goals, n, parameters={'arm.move.v1': {'meters': 1}},
                completion={'required_success_actions': ['arm.move.v1']}))
            require(owner.started.wait(5), 'real_model_did_not_dispatch_' + label)
            command = owner.commands[before]
            until(lambda: server.action_ledger.get(command.command_id).result(1).status == ActionStatus.RUNNING,
                  "actor_running_feedback_not_committed_" + label)
            row = server.action_dispatcher.report(command.command_id, OwnerBinding('arm', 1),
                ActionStatus.SUCCEEDED, details={'test_actor_only': True}).result(1)
            require(row.status == ActionStatus.SUCCEEDED, 'actor_success_not_committed')
            until(lambda: any(r['snapshot_id'] == command.decision_id and command.command_id in r['command_ids']
                for r in http.get('/decisions')['items']), 'trace_ledger_command_not_joined')
            return {'kind': label, 'mode': mode, 'command': command.to_dict(), 'ledger': asdict(row),
                'submitted_ns': submitted, 'actor_callback_accepted_ns': accepted[command.command_id],
                'actions': http.get('/actions'), 'decisions': http.get('/decisions'), 'stop': http.run('/stop')}

        result['cases'].append(execute(2, 'real_execute_before_fault'))
        result['checks'].append('real validated Laya selection reached Actor and durable succeeded Ledger')
        http.run('/mode', {'mode': 'execute'})
        before_fault = len(owner.commands)
        old_generation = server.decision_management.laya.generation
        old_process = server.decision_management.laya.owned_process_handle(expected_generation=old_generation)
        fault.armed = True
        service.submit_goal(goal_payload(service.goals, 3, parameters={'arm.move.v1': {'meters': 1}}))
        require(fault.triggered.wait(5), 'owned_post_written_fault_not_triggered')
        until(lambda: http.get('/status')['service']['restart_required'], 'post_timeout_not_quarantined')
        require(len(owner.commands) == before_fault, 'post_timeout_dispatched_action')
        result['fault_stop'] = http.run('/stop')
        require(service.mode == 'disabled', 'fault_stop_not_disabled')
        current = http.get('/config')['saved']
        http.post('/config', {'config': current})
        code, blocked = http.post('/mode', {'mode': 'execute'}, allow_error=True)
        require(code == 409 and blocked.get('code') == 'restart_required', 'saved_config_bypassed_generation_quarantine')
        fresh = LayaBackend(LayaConfig(**current['laya']), allow_test_execution=True)
        try:
            guarded = ManagedLayaBackend(fresh, server.decision_management.laya, old_generation)
            try:
                guarded.decide(fixed_warmup_snapshot())
            except Exception as exc:
                require(getattr(exc, 'code', None) == 'restart_required', 'new_adapter_wrong_generation_rejection')
            else:
                raise RuntimeError('new_adapter_bypassed_generation_quarantine')
            require(fresh.last_record is None, 'new_adapter_submitted_request_in_quarantine')
        finally:
            fresh.close()
        result['checks'].append('HTTP config and a new trusted backend instance cannot clear service-generation quarantine')
        result['post_fault_status'] = http.get('/status')
        result['recover'] = http.run('/service/recover')
        require(old_process.poll() is not None, 'recover_did_not_confirm_old_owned_exit')
        require(server.decision_management.laya.generation != old_generation, 'recover_reused_old_generation')
        require(service.mode == 'disabled' and service.goals.active is None and service.goals.pending_replace is None,
                'recover_restored_goal_or_execution')
        require(len(owner.commands) == before_fault, 'recover_replayed_old_goal')
        result['checks'].append('recover proved Actor stop and old process exit, installed fresh generation disabled, no old replay')
        result['cases'].append(execute(4, 'real_execute_after_fresh_authorization'))
        result['service_stop'] = http.run('/service/stop')
        result['final_status'] = http.get('/status')
        result['final_decisions'] = http.get('/decisions')
        result['final_actions'] = http.get('/actions')
        result['owned_history'] = server.decision_management.laya.history
        require(len(owner.commands) == 2, 'unexpected_extra_test_actor_commands')
        require(result['final_status']['service']['state'] == 'stopped', 'final_owned_service_not_stopped')
        require(not result['final_status']['decision']['gate_open'], 'final_gate_open')
        result['pass'] = True
    except Exception as exc:
        result['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        raise
    finally:
        try:
            if server is not None:
                result['before_cleanup'] = server.decision_management.status()
                server.shutdown()
                if thread is not None:
                    thread.join(3)
                server.server_close()
                result['after_cleanup_service'] = server.decision_management.laya.status()
        finally:
            if actor is not None:
                actor.stop(2)
            write(args.output / 'result.json', result)
    print(json.dumps({'pass': result['pass'], 'checks': len(result['checks']), 'test_actor_commands': 2,
                      'output': str(args.output)}))


if __name__ == '__main__':
    main()
