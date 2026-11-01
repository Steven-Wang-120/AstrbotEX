import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import pytest
import yaml
from jetmax_joint_nodes.acceptance import summarize, hold_metrics


def readings(drift=0):
    return [dict(receipt_ns=1_000_000_000+i*100_000_000, position_raw=500+drift*i,
                 query_duration_ms=8, local_s=i/10) for i in range(31)]


def test_baseline_and_hold_reject_drift_invalid_and_sparse_feedback():
    assert summarize(readings(), 3)['passed']
    assert hold_metrics(readings())['passed']
    assert not summarize(readings(), 3, invalid=1)['passed']
    assert not summarize(readings(1), 3)['passed']
    assert not hold_metrics(readings(1))['passed']
    sparse = readings()[::4]
    assert not summarize(sparse, 3)['passed']


@pytest.mark.parametrize('case', ['reach', 'stop_wait', 'timeout', 'timeout2', 'gripper'])
def test_installed_acceptance_client_fake_only(tmp_path, monkeypatch, case):
    monkeypatch.setenv('ROS_DOMAIN_ID', '174')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    monkeypatch.setenv('ROS_LOG_DIR', str(tmp_path/'ros-logs'))
    cfg = yaml.safe_load((Path(__file__).parents[1]/'config/fake.yaml').read_text())
    for name, p0 in [('arm_base', 500), ('arm_shoulder', 500), ('arm_elbow', 600)]:
        cfg['joints'][name] = dict(limits=[p0,p0+40], tolerance_raw=5, settled_ms=400)
    cfg['goal_timeout_cap_s'] = {'timeout': 1.0, 'timeout2': 2.0}.get(case)
    case = 'timeout' if case == 'timeout2' else case
    config_path = tmp_path/'config.yaml'
    config_path.write_text(yaml.safe_dump(cfg))
    ros2 = shutil.which('ros2')
    assert ros2
    log_path = tmp_path/'nodes.log'
    with log_path.open('w') as log:
        process = subprocess.Popen([ros2, 'launch', 'jetmax_joint_nodes', 'joints.launch.py',
                                    'backend:=fake', 'enable_control:=true', 'config:='+str(config_path)],
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic()+10
            while 'four nodes, one bus owner' not in log_path.read_text() and time.monotonic() < deadline:
                assert process.poll() is None, log_path.read_text()
                time.sleep(.05)
            result = subprocess.run([ros2, 'run', 'jetmax_joint_nodes', 'joint_acceptance',
                                     '--case', case, '--joint', 'arm_base', '--config', str(config_path),
                                     '--expected-backend', 'fake', '--fake-no-observer',
                                     '--output-dir', str(tmp_path/'evidence')],
                                    capture_output=True, text=True, timeout=40)
            assert result.returncode == 0, result.stdout+result.stderr
            events = [json.loads(line) for line in (tmp_path/'evidence/events.jsonl').read_text().splitlines()]
            assert events[-1]['event'] == 'case_end' and events[-1]['passed']
            names = [e['event'] for e in events]
            assert names.index('goal_request') < names.index('operator_observation')
            if case == 'timeout':
                results = [e for e in events if e['event'] == 'action_result']
                assert len(results) == 1 and results[0]['result']['outcome'] == 'timeout'
                assert results[0]['result']['final_state']['stop_state'] == 'stop_written_unconfirmed'
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
