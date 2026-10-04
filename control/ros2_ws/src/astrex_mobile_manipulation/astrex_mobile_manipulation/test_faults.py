"""Opt-in local test controls. This module has no ROS or actuator API.

Only raw-message publication can be paused. Physics sampling and every final
execution guard continue. A pause expires by itself; a request cannot renew it.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
from .core import identity

FAULT_SCHEMA = 'astrex.mm.local-fault-test.v1'
MAX_PAUSE_SECONDS = 2.0


def _private_json(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('FAULT_FILE_MUST_BE_OWNER_ONLY_REGULAR_FILE')
        data = os.read(fd, 8193)
        if len(data) > 8192:
            raise ValueError('FAULT_FILE_TOO_LARGE')
        value = json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('NONFINITE_FAULT_JSON')))
        if not isinstance(value, dict):
            raise ValueError('FAULT_OBJECT_REQUIRED')
        return value
    finally:
        os.close(fd)


def load_fault_config(path, profile_hash, now=None):
    now = time.monotonic() if now is None else now
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('FAULT_CONFIG_REQUIRES_ABSOLUTE_REGULAR_PATH')
    value = _private_json(path)
    if set(value) != {'schema', 'test_id', 'profile_hash', 'control_file', 'expires_monotonic'}:
        raise ValueError('FAULT_CONFIG_FIELDS')
    if value['schema'] != FAULT_SCHEMA or value['profile_hash'] != profile_hash:
        raise ValueError('FAULT_CONFIG_IDENTITY')
    if not isinstance(value['test_id'], str) or not 16 <= len(value['test_id']) <= 80:
        raise ValueError('FAULT_TEST_ID')
    expiry = value['expires_monotonic']
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or not now < expiry <= now + 7200:
        raise ValueError('FAULT_CONFIG_EXPIRED_OR_TOO_LONG')
    control = Path(value['control_file'])
    parent = path.parent.resolve()
    if (not control.is_absolute() or control.parent.resolve() != parent or control == path or
            control.is_symlink() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o022):
        raise ValueError('FAULT_CONTROL_REQUIRES_SAME_PRIVATE_DIRECTORY')
    value['config_hash'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def write_fault_request(settings, request):
    """Atomic owner-only file update; no ROS command or process signal."""
    path = Path(settings['control_file'])
    temporary = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(request, stream, sort_keys=True, allow_nan=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class LocalFaultControl:
    def __init__(self, config_path, profile_hash, now=None):
        self.settings = load_fault_config(config_path, profile_hash, now) if config_path else None
        self.seq = -1
        self.request = None
        self.applied_at = None
        self.suppressed_messages = 0
        self.error = None
        self.last_poll = -math.inf

    def poll(self, now, isaac_session, command_key):
        if self.settings is None:
            return False
        if now >= self.settings['expires_monotonic']:
            self.request = None
            self.error = 'FAULT_CONFIG_EXPIRED'
            return False
        if now - self.last_poll >= .02:
            self.last_poll = now
            try:
                value = _private_json(self.settings['control_file'])
                if value.get('schema') != FAULT_SCHEMA or value.get('test_id') != self.settings['test_id']:
                    raise ValueError('FAULT_REQUEST_IDENTITY')
                if type(value.get('seq')) is not int or value['seq'] < 0:
                    raise ValueError('FAULT_REQUEST_SEQUENCE')
                if value['seq'] > self.seq:
                    fields = {'schema', 'test_id', 'seq', 'isaac_session', 'op', 'until_monotonic',
                              'command_id', 'ex_session', 'goal_revision', 'execution_epoch'}
                    if set(value) != fields or value['isaac_session'] != isaac_session or identity(value) != command_key:
                        raise ValueError('FAULT_REQUEST_SESSION_OR_COMMAND')
                    if value['op'] not in ('raw_feedback_pause', 'clear'):
                        raise ValueError('FAULT_OPERATION_NOT_ALLOWED')
                    until = value['until_monotonic']
                    if type(until) not in (int, float) or not math.isfinite(until) or not now < until <= now + MAX_PAUSE_SECONDS:
                        raise ValueError('FAULT_REQUEST_EXPIRED_OR_TOO_LONG')
                    self.seq = value['seq']
                    self.request = value
                    self.applied_at = now
                    self.error = None
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError) as exc:
                self.error = str(exc)
        return bool(self.request and self.request['op'] == 'raw_feedback_pause' and
                    now < self.request['until_monotonic'] and identity(self.request) == command_key and
                    self.request['isaac_session'] == isaac_session)

    def status(self, paused):
        if self.settings is None:
            return {'enabled': False}
        return {'enabled': True, 'test_id': self.settings['test_id'], 'config_hash': self.settings['config_hash'],
                'seq': self.seq, 'raw_feedback_paused': paused, 'applied_monotonic': self.applied_at,
                'until_monotonic': self.request['until_monotonic'] if self.request else None,
                'suppressed_messages': self.suppressed_messages, 'error': self.error}


class OwnedGateway:
    """Own one gateway child. Never accepts a PID, command, or process group."""
    def __init__(self, profile_path, directory):
        self.profile_path = str(Path(profile_path).resolve())
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.process = None
        self.stream = None
        self.events = []
        self.number = 0

    def _record(self, event, **fields):
        self.events.append({'event': event, 'monotonic': time.monotonic(), **fields})
        (self.directory/'owned_gateway_processes.json').write_text(json.dumps(self.events, indent=2) + '\n')

    def start(self):
        import subprocess
        import sys
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError('OWNED_GATEWAY_ALREADY_RUNNING')
        if self.stream is not None:
            self.stream.close()
        self.number += 1
        evidence = self.directory/('gateway_' + str(self.number))
        evidence.mkdir()
        self.stream = (evidence/'stdout.log').open('wb')
        argv = [sys.executable, '-m', 'astrex_mobile_manipulation.gateway', '--ros-args',
                '-p', 'profile:=' + self.profile_path, '-p', 'evidence_dir:=' + str(evidence),
                '-p', 'use_sim_time:=true']
        self.process = subprocess.Popen(argv, stdout=self.stream, stderr=subprocess.STDOUT, start_new_session=True)
        self._record('start', pid=self.process.pid, argv=argv)
        return self.process.pid

    def crash(self, command_id, execution_session):
        import signal
        if not command_id or not execution_session or self.process is None or self.process.poll() is not None:
            raise RuntimeError('NO_LIVE_OWNED_GATEWAY_FOR_FAULT')
        self._record('intentional_crash', pid=self.process.pid, command_id=command_id,
                     execution_session=execution_session, signal='SIGKILL', scope='owned_child_only')
        self.process.send_signal(signal.SIGKILL)

    def confirm_crashed(self):
        if self.process is None:
            raise RuntimeError('NO_OWNED_GATEWAY')
        code = self.process.wait(timeout=5.)
        self._record('crash_exit', pid=self.process.pid, returncode=code)
        if code != -9:
            raise RuntimeError('GATEWAY_EXIT_WAS_NOT_THE_INJECTED_CRASH')
        return code

    def close(self):
        import signal
        import subprocess
        try:
            if self.process is not None:
                if self.process.poll() is None:
                    self._record('cleanup_signal', pid=self.process.pid, signal='SIGINT')
                    self.process.send_signal(signal.SIGINT)
                    try:
                        code = self.process.wait(timeout=15.)
                    except subprocess.TimeoutExpired:
                        self._record('cleanup_timeout', pid=self.process.pid, forced_kill=False)
                        raise RuntimeError('OWNED_GATEWAY_DID_NOT_EXIT_AFTER_SIGINT')
                else:
                    code = self.process.returncode
                self._record('cleanup_exit', pid=self.process.pid, returncode=code)
        finally:
            if self.stream is not None:
                self.stream.close()
