"""Read-only environment health check for the retained Isaac baseline."""
import argparse
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(os.environ['ASTREX_ROOT'])

def setting(name):
    return os.environ['ASTREX_' + name]

def command(argv, **kwargs):
    return subprocess.run(argv, text=True, capture_output=True, timeout=60, **kwargs)

def health():
    errors = []
    lab = Path(setting('ISAAC_LAB_ROOT'))
    expected_prefix = Path(setting('CONDA_ROOT')) / 'envs' / setting('ISAAC_CONDA_ENV')
    result = {'python': platform.python_version(), 'executable': sys.executable,
              'environment': setting('ISAAC_CONDA_ENV'), 'versions': {}, 'warnings': []}
    if Path(sys.prefix) != expected_prefix:
        errors.append('Python does not belong to the configured environment')
    if '.'.join(platform.python_version_tuple()[:2]) != setting('PYTHON_VERSION'):
        errors.append('Python major/minor mismatch')
    for package, key in [('isaacsim', 'ISAAC_SIM_VERSION'), ('torch', 'TORCH_VERSION'),
                         ('torchvision', 'TORCHVISION_VERSION'), ('torchaudio', 'TORCHAUDIO_VERSION'),
                         ('rsl-rl-lib', 'RSL_RL_VERSION')]:
        try:
            actual = metadata.version(package)
        except metadata.PackageNotFoundError:
            actual = 'MISSING'
        result['versions'][package] = actual
        if actual != setting(key):
            errors.append(f'{package}: expected {setting(key)}, got {actual}')
    for field, args in [('lab_commit', ['rev-parse', 'HEAD']),
                        ('lab_tag_commit', ['rev-parse', setting('ISAAC_LAB_VERSION') + '^{commit}']),
                        ('lab_status', ['status', '--porcelain'])]:
        p = command(['git', '--no-optional-locks', '-C', str(lab), *args])
        result[field] = p.stdout.strip()
        if p.returncode:
            errors.append(f'Cannot inspect {field}: {p.stderr}')
    if result['lab_commit'] != setting('ISAAC_LAB_COMMIT') or result['lab_tag_commit'] != setting('ISAAC_LAB_COMMIT'):
        errors.append('Lab tag/commit mismatch')
    if any(not row.startswith('??') for row in result['lab_status'].splitlines()):
        errors.append('Lab has tracked changes')
    if result['lab_status']:
        result['warnings'].append('Lab untracked files: ' + result['lab_status'])
    # Editable installation must point at the configured source, not another Lab copy.
    try:
        direct = json.loads(metadata.distribution('isaaclab').read_text('direct_url.json') or '{}')
        from urllib.parse import unquote, urlparse
        actual_source = Path(unquote(urlparse(direct.get('url', '')).path))
        if actual_source.resolve() != (lab / 'source/isaaclab').resolve():
            errors.append(f'Unexpected editable Lab source: {direct}')
        result['lab_editable'] = direct
    except Exception as exc:
        errors.append(f'Cannot resolve Lab install: {exc}')
    try:
        import torch
        import torchvision  # noqa: F401
        import torchaudio  # noqa: F401
        result['cuda_available'] = torch.cuda.is_available()
        result['torch_cuda'] = torch.version.cuda
        result['gpu'] = torch.cuda.get_device_name(0) if result['cuda_available'] else None
        if not result['cuda_available'] or setting('GPU_NAME') not in (result['gpu'] or ''):
            errors.append('Configured CUDA GPU unavailable or unexpected GPU')
    except Exception as exc:
        errors.append(f'Core import/CUDA failure: {exc}')
    sim = Path(metadata.distribution('isaacsim').locate_file('isaacsim')) if result['versions']['isaacsim'] != 'MISSING' else Path('/nonexistent')
    bridge = sim / 'exts/isaacsim.ros2.bridge'
    result['bridge'] = str(bridge)
    if not (bridge / 'jazzy/lib').is_dir():
        errors.append('Bundled Jazzy bridge libraries missing')
    if not (lab / setting('ISAAC_EXPERIENCE')).is_file():
        errors.append('Full Lab experience missing')
    if not Path('/opt/ros/' + setting('ROS_DISTRO') + '/setup.bash').is_file():
        errors.append('System ROS missing')
    result['system_python'] = command(['/usr/bin/python3', '--version']).stdout.strip()
    pip = command([sys.executable, '-B', '-m', 'pip', 'check'])
    known = {s for s in (ROOT / 'config/isaac_known_pip_warnings.txt').read_text().splitlines() if s and not s.startswith('#')}
    found = set(pip.stdout.strip().splitlines()) - {'No broken requirements found.'}
    result['known_pip_warnings'] = sorted(found & known)
    result['new_pip_warnings'] = sorted(found - known)
    if found - known or pip.returncode not in (0, 1) or pip.stderr.strip():
        errors.append('New pip diagnostic; no automatic repair: ' + pip.stderr)
    result['errors'] = errors
    result['status'] = 'FAIL' if errors else 'PASS_WITH_KNOWN_WARNINGS'
    return result

def arguments(argv):
    parser = argparse.ArgumentParser(description='AstrEX Isaac environment health check')
    parser.add_argument('profile', choices=('check',))
    parser.add_argument('--allow-legacy', action='store_true',
                        help='Inspect the target baseline without approving legacy use')
    return parser.parse_args(argv)


def main():
    args = arguments(sys.argv[1:])
    caller = os.environ.get('ASTREX_CALLER_CONDA', '')
    legacy = Path(caller).name in ('isaaclab60', 'isaaclab60_6010test')
    if legacy and not args.allow_legacy:
        raise RuntimeError('Legacy caller detected. Deactivate it or use --allow-legacy for diagnosis only.')
    check = health()
    print(json.dumps(check, indent=2, ensure_ascii=False), flush=True)
    if check['errors']:
        return 1
    if legacy:
        print('LEGACY_CALLER_NOT_APPROVED: diagnostics refer only to the configured target environment')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print('BASELINE ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)
