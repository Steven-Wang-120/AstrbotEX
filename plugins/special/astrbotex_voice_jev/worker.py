"""Isolated voice worker with a provider-neutral decision contract."""
import contextlib
import json
from pathlib import Path
import queue
import sys
import threading

# Parent-side Popen(encoding=...) does not configure the child's text streams.
# Windows pipe defaults may be GBK, which cannot encode real device names (®).
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8', errors='strict')

sys.path.insert(0, str(Path(__file__).parent / "lib"))
OUT = sys.stdout
OUTPUT_LOCK = threading.Lock()


def emit(kind, **values):
    with OUTPUT_LOCK:
        print(json.dumps({"type": kind, **values}, ensure_ascii=False), file=OUT, flush=True)


def main():
    start = json.loads(sys.stdin.readline())
    config, epoch = start["config"], start["epoch"]
    from audio_devices import AudioDeviceError, inventory, validate_selection
    devices = inventory()
    checked = validate_selection(config.get('input_device'), config.get('output_device'), snapshot=devices)
    if not checked['ok']:
        errors = [r for r in (checked['input'], checked['output']) if not r['ok']]
        raise AudioDeviceError(errors[0]['code'], '；'.join(r['message'] for r in errors))
    capture_device, playback_device = checked['input']['device']['index'], checked['output']['device']['index']
    emit('devices_selected', input_device=checked['input']['device'], output_device=checked['output']['device'])
    from vosk import Model, SetLogLevel
    from streaming_input import listen_once
    from voice_terminal import Speech
    from astrbot_chat import AstrBotChat
    from voice_session import VoiceSession, load_gate
    from decision_client import TopicDecisionClient
    SetLogLevel(-1)
    model = Model(config["vosk_model_path"])
    speech_path = Path(config["speech_config_path"]).resolve()
    speech = Speech(json.loads(speech_path.read_text(encoding="utf-8-sig")), speech_path.parent)
    chat = AstrBotChat.from_file(config["chat_config_path"])
    chat.config["provider_id"] = config.get("provider_id", "deepseek/deepseek-flash")
    decisions = queue.Queue(maxsize=32)
    stop = threading.Event()

    def read_commands():
        try:
            for line in sys.stdin:
                if len(line) > 16384:
                    break
                command = json.loads(line)
                if command.get('op') == 'stop':
                    break
                decisions.put_nowait(command)
        except (ValueError, queue.Full):
            pass
        finally:
            stop.set()
    threading.Thread(target=read_commands, daemon=True).start()

    def progress(message):
        if message.startswith('完整回答：'):
            emit('reply', text=message[5:][:4000], partial=False)
        elif message.startswith('助手片段：'):
            emit('reply', text=message[5:][:4000], partial=True)
        elif message.startswith('背景记忆'):
            emit('gate_result', allowed=False, message=message[:2000])
        elif message.startswith('已确认对我说话'):
            emit('gate_result', allowed=True, message=message[:2000])
        elif message.startswith('本句未提交') or message.startswith('本轮回答未完成'):
            emit('error', message=message[:2000])
        else:
            emit('status', state='speaking' if message.startswith('正在播报') else 'listening', message=message[:2000])

    mode = config.get('decision_mode', 'http')
    settings = {}
    # Direct URL settings are what a future frontend edits. An optional local
    # file can still supply credentials and defaults for existing deployments.
    for src, dest in [('decision_type','type'), ('decision_url','url'),
                      ('decision_model','model'), ('decision_token_env','token_env'),
                      ('decision_api_key','api_key'),
                      ('decision_timeout','timeout'), ('decision_threshold','threshold'),
                      ('forward_threshold','forward_threshold'), ('quiet_threshold','quiet_threshold'), ('decision_json_mode','json_mode')]:
        if config.get(src) not in (None, ''):
            settings[dest] = config[src]
    if config.get('gate_config_path') and not config.get('decision_url'):
        settings = {key: value for key, value in settings.items()
                    if key in ('threshold', 'forward_threshold', 'quiet_threshold', 'timeout')}
    client = None
    if mode == 'topic':
        client = TopicDecisionClient(decisions, lambda **data: emit('candidate', **data), epoch,
                                     timeout=float(config.get('decision_timeout', 10)))
    elif mode != 'http':
        raise ValueError('decision_mode must be http or topic')
    gate = load_gate(config.get('gate_config_path') or None, settings, client=client)
    session = VoiceSession(gate, speech, chat, playback_device, stop, progress,
                           decision_stage=config.get('decision_stage', 'final'))

    def capture():
        try:
            listen_once(capture_device, model, session.lifecycle, progress,
                        lambda text: emit('partial', text=text[:2000]), on_utterance=session.submit)
        except Exception as exc:
            emit('error', message=str(exc)[:2000])
            stop.set()
    listener = threading.Thread(target=capture, daemon=True)
    listener.start()
    try:
        session.run()
    finally:
        session.close()
        listener.join(timeout=4)


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(sys.stderr):
            main()
    except Exception as exc:
        emit("error", message=str(exc)[:2000])
        sys.exit(1)
