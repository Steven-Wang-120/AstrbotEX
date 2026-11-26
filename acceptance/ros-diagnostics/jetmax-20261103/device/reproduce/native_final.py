from pathlib import Path
import datetime,hashlib,json,os,platform,re,subprocess,time
w=Path('/home/jetmax/ros-diag-acceptance-20261103');out=w/'evidence'
record={'stage':'wait-for-entry','python':platform.python_version(),'platform':platform.platform(),'temp_directory':'/dev/shm','started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'base_commit':'86ce3916e9dd93dd8d85e435e138eb64c2d13be1'}
def save():(out/'native-unit-final.json').write_text(json.dumps(record,indent=2))
save()
try:
 deadline=time.monotonic()+240
 while time.monotonic()<deadline:
  p=out/'entry-final-state.json'
  if p.exists() and json.loads(p.read_text()).get('stage')=='ui-ready':break
  time.sleep(.5)
 else:raise RuntimeError('entry cleanup did not complete')
 # Browser checks have completed; stop the private EX after the entry cleanup.
 (w/'ui-done').write_text('browser PASS; entry cleanup complete\n')
 record['stage']='native-unit-final'
 record['source_sha256']={name:hashlib.sha256((w/'source'/name).read_bytes()).hexdigest() for name in ['scripts/verify_ros2_diagnostics.py','tests/test_action_dispatcher.py','tests/test_ros2_diagnostics_verifier.py']}
 save()
 env=dict(os.environ,TMPDIR='/dev/shm',OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
 # Source the same ROS and interface overlays as the deployed EX process.
 command=['bash','-c','source /opt/ros/humble/setup.bash; source /home/jetmax/astrex/runtime/ex-interfaces/install/local_setup.bash; exec /home/jetmax/astrex/astrbotex/.venv/bin/python -m unittest discover -s tests -p "test_*.py"']
 start=time.monotonic()
 with (out/'native-unit-final.log').open('wb') as log:
  p=subprocess.run(command,cwd=w/'source',env=env,stdout=log,stderr=subprocess.STDOUT,timeout=700)
 text=(out/'native-unit-final.log').read_text(errors='replace')
 m=re.search(r'Ran (\d+) tests? in',text)
 record.update(exit_code=p.returncode,elapsed_sec=round(time.monotonic()-start,3),tests=int(m.group(1)) if m else None,result='PASS' if p.returncode==0 else 'FAIL',log_tail=text[-1800:])
except BaseException as exc:record.update(result='FAIL',error=type(exc).__name__+': '+str(exc))
record['stage']='finished';record['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
print(json.dumps(record),flush=True)
