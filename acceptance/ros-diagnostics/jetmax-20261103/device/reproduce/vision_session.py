#!/usr/bin/env python3
"""Run the deployed camera and YOLO through the EX service until Ctrl+C."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request

BASE=Path('/home/jetmax/astrex')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration',type=float,default=0,help='seconds; 0 runs until Ctrl+C')
    parser.add_argument('--fps',type=float,default=1)
    args=parser.parse_args()
    token=Path('/home/jetmax/ros-diag-acceptance-20261103/data/secrets/admin.token').read_text().strip()
    def api(path,body=None):
        req=urllib.request.Request('http://127.0.0.1:18765/api/v1/ex/'+path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=15) as response:return json.load(response)
    status=api('status')
    if status['runtime_state']!='idle':raise SystemExit('EX must be idle before starting this session.')
    others=[p['id'] for p in status['plugins'] if p['enabled'] and p['id']!='yolo']
    if others:raise SystemExit('This vision session requires other plugins disabled: '+', '.join(others))
    api('plugins/yolo/enable',{})
    api('environments/select',{'mode':'ros2'})
    deadline=time.monotonic()+30
    while True:
        environment=api('environments')
        state=environment['environment']
        if state['active_mode']=='ros2' and state['phase']=='idle':break
        if time.monotonic()>deadline:raise SystemExit('ROS environment did not become ready: '+str(state))
        time.sleep(.1)
    running=True
    def stop(*_):
        nonlocal running
        running=False
    signal.signal(signal.SIGINT,stop)
    signal.signal(signal.SIGTERM,stop)
    child=None
    started=False
    try:
        api('runtime/start',{})
        started=True
        env=dict(os.environ,ROS_DOMAIN_ID=str(environment['effective_config']['domain_id']))
        child=subprocess.Popen([str(BASE/'bin/run-camera'),'--fps',str(args.fps),
            '--duration',str(args.duration)],env=env,start_new_session=True)
        print('Camera + YOLO running. Ctrl+C stops this vision session.',flush=True)
        while running and child.poll() is None:
            time.sleep(.2)
        if child.poll() not in (None,0):raise SystemExit('Camera exited with code '+str(child.returncode))
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid,signal.SIGTERM)
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGKILL)
                child.wait(timeout=5)
        if started:api('runtime/stop',{'reason':'vision session finished'})
        print('Vision session stopped.',flush=True)


if __name__=='__main__':main()
