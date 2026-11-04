import argparse
from pathlib import Path
import zipfile

FILES = ['plugin.json', 'config.json', 'config.schema.json', 'main.py', 'worker.py',
         'README.md', 'VALIDATION.md', 'DECISION_API.md', 'decision-api.schema.json',
         'build_package.py', 'voice.gate.example.json', 'AUDIO_DEVICE_API.md', 'audio-devices.schema.json']
LIB = ['voice_terminal.py', 'streaming_input.py', 'windows_test.py', 'audio_playback.py',
       'astrbot_chat.py', 'mimo_client.py', 'windows_speak.ps1', 'conversation_memory.py',
       'duplex_input.py', 'realtime_pipeline.py', 'decision_credentials.py', 'decision_client.py', 'voice_gate.py', 'voice_session.py', 'audio_devices.py']


def build(output):
    root = Path(__file__).resolve().parent
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in FILES + ['lib/' + name for name in LIB]:
            archive.write(root / name, root.name + '/' + name)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    print(build(parser.parse_args().output))
