"""Synthetic-only B08 management loop; real HTTP/runtime and software Actor, no model."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from astrbot_ex.core.decision.backends.laya import LayaBackend
from astrbot_ex.core.plugin_actor import PluginActor
from tests.test_decision_service import ActionOwner
from tests.test_goal_manager import make_catalog, goal_payload
from tests.test_laya_backend import health, response


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


class SyntheticOwned:
    """Popen-shaped loopback fixture; no subprocess, weights, signals or remote service."""
    def __init__(self, port):
        self.port, self.children, self.calls = port, [], []
        self.armed = False
        self.events = []
        self.triggered = threading.Event()

    def launch(self, command, **kwargs):
        fixture = self
        marker = kwargs['env']['LAYA_API_KEY']
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def send(self, value):
                body = json.dumps(value).encode()
                self.send_response(200 if self.headers.get('Authorization') == 'Bearer ' + marker else 401)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            def do_GET(self):
                self.send(health())
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.calls.append(request)
                if fixture.armed:
                    fixture.armed = False
                    fixture.events.append({'fault': 'synthetic loopback response stall after POST read',
                        'request_sha256': hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()})
                    fixture.triggered.set()
                    child.release.wait(3)
                choices = {name: next((key for key, criterion in question['criteria'].items()
                    if criterion.startswith('Start')), next(iter(question['criteria'])))
                    for name, question in request['questions'].items()}
                self.send(response(request['questions'], choices=choices))
        supplier = ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        supplier.daemon_threads = True
        thread = threading.Thread(target=supplier.serve_forever, kwargs={'poll_interval': .01})
        class Child:
            pid = 98000 + len(fixture.children)
            code = None
            release = threading.Event()
            def poll(self):
                return self.code
            def terminate(self):
                if self.code is None:
                    self.release.set()
                    supplier.shutdown()
                    supplier.server_close()
                    thread.join(2)
                    self.code = -15
            kill = terminate
            def wait(self, timeout=None):
                return self.code
        child = Child()
        self.children.append(child)
        thread.start()
        return child

    def close(self):
        for child in self.children:
            child.terminate()


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
    until(lambda: server.decision_service._last_catalog_revision == server.capability_catalog.snapshot().revision,
          'composition_catalog_not_ready')
    return owner, actor, accepted


def service_idle(server):
    return server.decision_service.mode == 'disabled' and server.decision_service.goals.active is None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0, help='Synthetic-only loopback port (0 selects a free port)')
    args = parser.parse_args()
    require(not args.output.exists(), 'verification_output_must_be_fresh')
    args.output.mkdir(mode=0o700, parents=True)
    isolated = tempfile.TemporaryDirectory(prefix='c06-verifier-')
    root = Path(isolated.name)
    cache = root / 'cache'
    cache.mkdir()
    port = args.port
    if not port:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
    fault = SyntheticOwned(port)
    launch_patch = patch('astrbot_ex.core.decision.owned_laya.subprocess.Popen', side_effect=fault.launch)
    launch_patch.start()
    result = {'scope': 'synthetic owned HTTP, real RuntimeController, isolated software Actor; no model/ROS/robot',
        'http': [], 'checks': [], 'cases': [], 'faults': fault.events, 'pass': False}
    server = thread = actor = None
    deployment = Deployment(Path(sys.executable), cache, root / 'owned-laya', port=port,
        device='cpu', state_path=root / 'execution/laya/service-state.json',
        terminate_timeout_s=2, kill_timeout_s=2)
    try:
        settings = ManagementSettings(laya_deployment=deployment)
        with patch.dict(os.environ, {'ASTRBOTEX_DATA_DIR': str(root), 'ASTRBOTEX_STT_ENABLED': '', 'ASTRBOTEX_TTS_ENABLED': ''}):
            server = build_server('127.0.0.1', 0, 20, management_settings=settings)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        thread.start()
        token = server.decision_management.credential_path.read_text().strip()
        http = HTTP('http://127.0.0.1:' + str(server.server_address[1]), token, result['http'])
        result['base'] = http.base
        owner, actor, accepted = compose_actor(server)
        require(http.request('/status', auth=False)[0] == 401, 'missing_credential_not_rejected')
        original = http.get('/config')
        config = original['saved']
        config.update(backend='laya')
        config['laya'].update(enabled=True, allow_live_http=True, deadline_ms=300)
        config['laya']['service_connection'].update(mode='owned', auth_mode='bearer')
        config['laya']['deployment'] = {'launcher': 'subprocess', 'python': str(deployment.python),
            'cache': str(deployment.cache), 'device': deployment.device}
        http.post('/config', {'config': config})
        http.post('/secret', {'provider': 'laya', 'action': 'set', 'value': 'C06-synthetic-verifier-only'})
        require(server.decision_management.laya.status()['state'] == 'stopped', 'save_started_model')
        require(server.controller.runtime.state.value == 'idle' and service_idle(server), 'save_started_runtime')
        before = http.get('/config')
        code, stale = http.request('/mode', {'mode': 'execute', 'ex_session': original['ex_session'],
            'expected_revision': original['revision']})
        require(code == 409 and stale['code'] == 'revision_conflict', 'stale_cas_not_rejected')
        require(http.get('/config') == before, 'stale_cas_changed_config')
        result['checks'].append('schema2 config, secret/CAS and save-only lifecycle preserved')
        result['checks'].append('save did not start process, runtime, mode or Goal')
        result['start'] = http.run('/service/start')
        require(server.decision_service.mode == 'disabled', 'start_opened_execution')
        result['probe'] = http.run('/test')
        require(result['probe']['result']['inference_called'] is True and
                result['probe']['result']['inference_ok'] is True, 'fixed_wait_probe_not_exercised')
        require(result['probe']['result']['binding']['current_config_verified'], 'saved_probe_not_bound')
        require(server.controller.runtime.state.value == 'idle' and service_idle(server), 'probe_activated_runtime')
        require(not owner.commands and not server.action_ledger.list_commands().result(1), 'probe_dispatched_actor')
        result['checks'].append('builtin fixed-wait HTTP inference without Goal/Action/runtime activation')
        result['shadow_runtime'] = http.run('/mode', {'mode': 'execute'})
        http.run('/mode', {'mode': 'disabled'})
        result['shadow_restart'] = http.run('/service/start')
        server.controller.start()
        require(server.controller.runtime.state.value == 'running', 'shadow_runtime_start_failed')
        result['shadow_mode'] = http.run('/mode', {'mode': 'shadow'})
        service = server.decision_service
        service.submit_goal(goal_payload(service.goals, 1, parameters={'arm.move.v1': {'meters': 1}}))
        until(lambda: any(d['outcome'] == 'shadow' for d in service.status()['decisions']), 'shadow_decision_missing')
        result['shadow_stop'] = http.run('/stop')
        require(not owner.commands and not server.action_ledger.list_commands().result(1), 'shadow_dispatched_action')
        result['cases'].append({'kind': 'shadow', 'actions': http.get('/actions'), 'decisions': http.get('/decisions')})
        result['checks'].append('synthetic shadow choice did not dispatch or write Ledger action')

        def execute(n, label):
            mode = http.run('/mode', {'mode': 'execute'})
            require(server.controller.runtime.state.value == 'running' and
                    service.status()['control_mode'] == 'decision', 'execute_runtime_not_running')
            require(mode['result']['new_goal_required'] and service.goals.active is None, 'activation_replayed_goal')
            require(server.decision_management.laya.generation == service.backend.generation,
                    'installed_backend_generation_stale')
            owner.started.clear()
            before = len(owner.commands)
            submitted = time.monotonic_ns()
            service.submit_goal(goal_payload(service.goals, n, parameters={'arm.move.v1': {'meters': 1}},
                completion={'required_success_actions': ['arm.move.v1']}))
            require(owner.started.wait(5), 'synthetic_model_did_not_dispatch_' + label)
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

        result['cases'].append(execute(2, 'synthetic_execute_before_fault'))
        result['checks'].append('synthetic selection reached Actor and durable succeeded Ledger')
        http.run('/mode', {'mode': 'execute'})
        before_fault = len(owner.commands)
        old_generation = server.decision_management.laya.generation
        old_process = server.decision_management.laya.owned_process_handle(expected_generation=old_generation)
        fault.armed = True
        service.submit_goal(goal_payload(service.goals, 3, parameters={'arm.move.v1': {'meters': 1}}))
        require(fault.triggered.wait(5), 'owned_post_written_fault_not_triggered')
        until(lambda: http.get('/status')['service']['restart_required'], 'post_timeout_not_quarantined')
        require(len(owner.commands) == before_fault, 'post_timeout_dispatched_action')
        view = http.get('/view')
        require(view['ex']['state'] == 'failed' and not view['ex']['can_start'] and
                view['connection']['state'] == 'disconnected', 'quarantine_view_not_truthful')
        result['fault_stop'] = http.run('/stop')
        require(service.mode == 'disabled', 'fault_stop_not_disabled')
        current = http.get('/config')['saved']
        http.post('/config', {'config': current})
        code, blocked = http.post('/mode', {'mode': 'execute'}, allow_error=True)
        require(code == 409 and blocked.get('code') == 'restart_required', 'saved_config_bypassed_generation_quarantine')
        fresh = LayaBackend(server.decision_management.store.backend_config('laya', current),
                            secret_provider=lambda: 'C06-synthetic-verifier-only')
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
        result['cases'].append(execute(4, 'synthetic_execute_after_fresh_authorization'))
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
            try:
                if actor is not None:
                    actor.stop(2)
                fault.close()
            finally:
                launch_patch.stop()
                isolated.cleanup()
                write(args.output / 'result.json', result)
    print(json.dumps({'pass': result['pass'], 'checks': len(result['checks']), 'test_actor_commands': 2,
                      'output': str(args.output)}))


if __name__ == '__main__':
    main()
