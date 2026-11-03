from pathlib import Path
import importlib.util,json,os,subprocess,sys,time
w=Path('/home/jetmax/ros-diag-acceptance-20261103')
spec=importlib.util.spec_from_file_location('v',w/'source/scripts/verify_ros2_diagnostics.py')
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
live=v.Api('http://127.0.0.1:8765',Path('/home/jetmax/astrex/data/secrets/admin.token').read_text().strip())
api=v.Api('http://127.0.0.1:18765',(w/'data/secrets/admin.token').read_text().strip())
baseline=json.loads((w/'live-state-before.json').read_text())
record={'stage':'native-entry-final','reason':'reset interrupted-run fixture to disabled and site Domain 0','started_at':v.utc_now()}
def save():(w/'evidence/entry-final-state.json').write_text(json.dumps(record,indent=2))
save()
try:
 status=live.get('status')
 assert status['runtime_state']=='idle' and status['actions']['gate_open'] is False
 for p in live.get('plugins')['plugins']:
  if p['enabled']:live.post('plugins/'+p['id']+'/disable',{})
 v._select_mode(live,'normal')
 api.post('runtime/stop',{})
 v._select_mode(api,'normal')
 api.post('plugins/diag_image_probe/disable',{})
 api.post('plugins/yolo/disable',{})
 api.post('environments/ros2/config',{'config':{'domain_id':0}})
 env=dict(os.environ,RMW_IMPLEMENTATION='rmw_cyclonedds_cpp',ROS_LOCALHOST_ONLY='1',OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
 # The verifier's entry scenario itself imports no rclpy; only the existing entry starts ROS.
 argv=['/home/jetmax/astrex/astrbotex/.venv/bin/python',str(w/'source/scripts/verify_ros2_diagnostics.py'),'--url','http://127.0.0.1:18765','--token-file',str(w/'data/secrets/admin.token'),'--scenario','entry','--domain','0','--duration','30','--repeat','2','--entry',str(w/'run-yolo'),'--output',str(w/'evidence/entry-final')]
 with (w/'evidence/entry-final.log').open('wb') as log:
  p=subprocess.Popen(argv,cwd=w/'source',env=env,stdout=log,stderr=subprocess.STDOUT)
  deadline=time.monotonic()+240
  while p.poll() is None:
   if time.monotonic()>deadline:
    p.send_signal(__import__('signal').SIGINT);p.wait(timeout=25);break
   graph=api.get('environments/ros2/graph');graph=graph.get('graph',graph)
   topics=[t for t in graph.get('topics',[]) if t['name']=='/camera/color/image_raw/compressed']
   with (w/'evidence/entry-final-topology.jsonl').open('a') as f:
    f.write(json.dumps({'at':v.utc_now(),'topics':topics,'endpoints':api.get('environments/ros2/endpoints'),'diagnostics':api.get('environments/diagnostics')})+'\n')
   time.sleep(.5)
 record['exit_code']=p.returncode
except BaseException as exc:
 record['error']=type(exc).__name__+': '+str(exc)
finally:
 try:
  v._select_mode(live,'normal')
  live.post('environments/ros2/config',{'config':baseline['environment']['config']['ros2']})
  for p in baseline['plugins']:live.post('plugins/'+p['id']+('/enable' if p['enabled'] else '/disable'),{})
  v._select_mode(live,baseline['environment']['environment']['active_mode'])
  record['live_restored']=live.get('status')['runtime_state']=='idle' and live.get('environments')['environment']['active_mode']==baseline['environment']['environment']['active_mode']
 except Exception as exc:record['restore_error']=type(exc).__name__
 record['stage']='ui-ready';record['finished_at']=v.utc_now();save()
 print(json.dumps(record),flush=True)
