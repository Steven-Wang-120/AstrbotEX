"""EX actor adapter. Audio/cloud work runs only in an isolated process."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque

ID = "astrbotex_voice_jev"


class Plugin:
    def __init__(self, context):
        self.context = context
        self.config = dict(context.config or {})
        self.root = Path(__file__).resolve().parent
        self.process = None
        self.running = False
        self.streaming = bool(self.config.get("streaming_enabled", False))
        self.events = queue.Queue(maxsize=64)
        self.pending = {}
        self.epoch = ""
        self.status = {"state": "stopped", "last_error": None}
        self.audio = {'state': 'idle', 'inventory': None, 'validation': None, 'selected': None}
        self.device_events = queue.Queue(maxsize=2)
        self.device_task = None
        self.recent_events = deque(maxlen=80)
        self.event_sequence = 0

    def on_load(self):
        self.control = self.context.subscribe(ID + ".control", max_messages=8)
        self.decisions = self.context.subscribe(self.config.get("decision_topic", "voice.decision"), max_messages=32)

    def publish(self, kind, payload):
        self.event_sequence += 1
        self.recent_events.append({'id': self.event_sequence, 'type': kind,
                                   'payload': payload, 'timestamp': time.time()})
        self.context.topic_bus.publish_payload(ID + "." + kind, timestamp=time.time(), source=ID, payload=payload)

    def set_streaming(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be boolean')
        if enabled and not self.running:
            raise ValueError('请先启动 EX 运行时')
        if enabled and self.process and self.process.poll() is None:
            return self.get_status()
        self.streaming = enabled
        if enabled:
            try:
                self.status['last_error'] = None
                self.start_worker()
            except Exception as exc:
                self.streaming = False
                self.stop_worker()
                self.status.update(state='error', last_error=str(exc)[:2000])
                raise
        else:
            self.stop_worker()
        return self.get_status()

    def on_runtime_start(self):
        self.running = True
        if self.streaming:
            self.start_worker()

    def stop_worker(self):
        self.epoch = ""
        self.pending.clear()
        proc, self.process = self.process, None
        if proc:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.6)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=0.6)
            for handle in (proc.stdin, proc.stdout):
                if handle:
                    handle.close()
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        self.status["state"] = "stopped"
        self.audio['selected'] = None

    def query_audio_devices(self, payload):
        """Backend integration hook; executes in the configured audio host Python.

        It lists/checks settings only, never records, plays or installs drivers.
        A frontend backend can call this method or use the declared Topic actions.
        """
        try:
            if isinstance(payload, dict) and payload.get('action') == 'validate':
                payload = dict(payload)
                payload.setdefault('input_device', self.config.get('input_device'))
                payload.setdefault('output_device', self.config.get('output_device'))
            body = json.dumps(payload, ensure_ascii=False)
            if len(body.encode('utf-8')) > 16384:
                raise ValueError('request too large')
            result = subprocess.run(
                [self.config.get('python_executable') or sys.executable, '-X', 'utf8', str(self.root/'lib/audio_devices.py')],
                input=body, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                timeout=8, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode or len(result.stdout.encode('utf-8')) > 524288:
                raise ValueError('invalid worker response')
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or type(data.get('ok')) is not bool:
                raise ValueError('invalid worker response')
            return data
        except subprocess.TimeoutExpired:
            return {'ok': False, 'code': 'device_query_timeout', 'message': '设备查询超时，请检查音频主机和系统驱动。'}
        except (OSError, ValueError, TypeError):
            return {'ok': False, 'code': 'device_query_failed', 'message': '设备查询失败，请检查音频Python环境与PortAudio依赖。'}

    def request_audio_devices(self, payload):
        if (self.device_task and self.device_task.is_alive()) or not self.device_events.empty():
            return False
        self.audio['state'] = 'querying'
        def run():
            result = self.query_audio_devices(payload)
            self.device_events.put_nowait((payload['action'], result))
        self.device_task = threading.Thread(target=run, daemon=True)
        self.device_task.start()
        return True

    def start_worker(self):
        self.stop_worker()
        self.epoch = uuid.uuid4().hex
        command = [self.config.get("python_executable") or sys.executable, "-X", "utf8", "-u", str(self.root / "worker.py")]
        error_file = self.root / "worker-error.local.log"
        with error_file.open("ab") as errors:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=errors, text=True, encoding="utf-8",
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        proc, epoch = self.process, self.epoch
        proc.stdin.write(json.dumps({"op": "start", "epoch": epoch,
                                     "config": self.config}) + "\n")
        proc.stdin.flush()

        def reader():
            try:
                while True:
                    line = proc.stdout.readline(16385)
                    if not line:
                        break
                    if len(line) > 16384:
                        raise ValueError("worker event exceeds 16 KiB")
                    item = json.loads(line)
                    self.events.put_nowait((epoch, item))
            except (ValueError, queue.Full):
                # Overflow fails closed, never silently approves a candidate.
                if proc.poll() is None:
                    proc.terminate()
            except (OSError, AttributeError):
                pass
        threading.Thread(target=reader, daemon=True).start()
        self.status["state"] = "starting"

    def accept_decision(self, payload, source):
        request_id = payload.get("request_id")
        pending = self.pending.get(request_id)
        if (not pending or source != self.config.get("decision_source", "decision") or
                payload.get("epoch") != self.epoch):
            return False
        self.pending.pop(request_id)
        fields = ('addressed_to_assistant', 'should_reply', 'should_stop')
        valid = time.monotonic() < pending and all(type(payload.get(key)) is bool for key in fields)
        result = {key: payload[key] if valid else False for key in fields}
        allowed = result['addressed_to_assistant'] and result['should_reply'] and not result['should_stop']
        if self.process and self.process.poll() is None:
            self.process.stdin.write(json.dumps({"op": "decision", "request_id": request_id,
                                                 "epoch": self.epoch, **result}) + "\n")
            self.process.stdin.flush()
        self.publish("gate_result", {"request_id": request_id, "allowed": allowed})
        return allowed

    def on_worker_step(self):
        message = self.control.get_nowait()
        if message:
            payload = message.payload
            if 'action_id' in payload and isinstance(payload.get('params'), dict):
                payload = {'action': payload['action_id'], **payload['params']}
            if payload.get("action") == "set_streaming" and type(payload.get("enabled")) is bool:
                try:
                    self.set_streaming(payload['enabled'])
                except (OSError, ValueError) as exc:
                    self.publish('error', {'message': str(exc)[:2000]})
            elif payload.get('action') in ('refresh_audio_devices', 'check_audio_devices'):
                action = 'list' if payload['action'] == 'refresh_audio_devices' else 'validate'
                request = {'action': action,
                    'input_device': payload.get('input_device', self.config.get('input_device')),
                    'output_device': payload.get('output_device', self.config.get('output_device'))}
                if 'request_id' in payload:
                    request['request_id'] = payload['request_id']
                if not self.request_audio_devices(request):
                    self.publish('audio_devices', {'operation': action, 'ok': False, 'code': 'device_query_busy',
                                 'request_id': payload.get('request_id'), 'message': '上一项设备查询尚未完成，请稍后重试。'})
        try:
            action, result = self.device_events.get_nowait()
            self.audio['state'] = 'ready' if result.get('ok') else 'error'
            self.audio['inventory' if action == 'list' else 'validation'] = result
            self.publish('audio_devices', {'operation': action, **result})
        except queue.Empty:
            pass
        for _ in range(16):
            message = self.decisions.get_nowait()
            if message is None:
                break
            self.accept_decision(message.payload, message.source)
        for _ in range(16):
            try:
                epoch, item = self.events.get_nowait()
            except queue.Empty:
                break
            if epoch != self.epoch or not self.running:
                continue
            kind = item.get("type")
            if kind == "candidate":
                self.pending.clear()  # One active utterance only.
                self.pending[item["request_id"]] = time.monotonic() + float(self.config.get("decision_timeout", 10))
                self.publish("candidate", {**item, "epoch": epoch, "ttl_ms": int(float(self.config.get("decision_timeout", 10))*1000)})
            elif kind == 'devices_selected':
                self.audio['selected'] = item
                self.publish('audio_devices', {'operation': 'selected', **item})
            elif kind in ("status", "partial", "reply", "error", "gate_result"):
                self.publish(kind, item)
                if kind == "status":
                    self.status["state"] = item.get("state")
                elif kind == "error":
                    self.status["last_error"] = item.get("message")
        now = time.monotonic()
        self.pending = {key: deadline for key, deadline in self.pending.items() if deadline > now}
        if self.process and self.process.poll() is not None:
            code = self.process.returncode
            self.streaming = False
            self.stop_worker()
            self.status['state'] = 'error' if code else 'stopped'
            if code and not self.status.get('last_error'):
                self.status['last_error'] = '语音进程异常退出，请检查音频设备和运行环境。'
                self.publish('error', {'message': self.status['last_error']})
        time.sleep(0.01)

    def on_runtime_stop(self, reason="runtime stopped"):
        self.running = False
        self.streaming = False
        self.stop_worker()

    def on_disable(self):
        self.on_runtime_stop()

    def on_unload(self):
        self.on_runtime_stop()
        self.control.close()
        self.decisions.close()

    def get_status(self):
        return {**self.status, "streaming_enabled": self.streaming, "runtime_active": self.running,
                "worker_running": bool(self.process and self.process.poll() is None),
                "pending_decisions": len(self.pending), 'audio': dict(self.audio),
                'events': list(self.recent_events)}
