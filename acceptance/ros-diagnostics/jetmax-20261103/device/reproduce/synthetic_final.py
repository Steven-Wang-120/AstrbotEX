from pathlib import Path
import hashlib,importlib.util,json,os,signal,subprocess,sys,time
w=Path('/home/jetmax/ros-diag-acceptance-20261103');out=w/'evidence'
spec=importlib.util.spec_from_file_location('v',w/'source/scripts/verify_ros2_diagnostics.py')
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
record={'stage':'start','started_at':v.utc_now(),'checks':{},'source_sha256':hashlib.sha256((w/'source/scripts/verify_ros2_diagnostics.py').read_bytes()).hexdigest()}
server=None
def save():(out/'synthetic-final-suite.json').write_text(json.dumps(record,indent=2))
try:
 env=dict(os.environ,ASTRBOTEX_DATA_DIR=str(w/'data'),RMW_IMPLEMENTATION='rmw_cyclonedds_cpp',ROS_LOCALHOST_ONLY='1')
 with (out/'ex-server-final.log').open('wb') as log:
  server=subprocess.Popen([sys.executable,'-u','-m','astrbot_ex.core.api_server','--host','127.0.0.1','--port','18765'],cwd=w/'source',env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 record['ex_pid']=server.pid;save()
 api=v.Api('http://127.0.0.1:18765',(w/'data/secrets/admin.token').read_text().strip())
 for _ in range(100):
  try:api.get('status');break
  except v.AcceptanceError:time.sleep(.2)
 else:raise RuntimeError('private EX did not become ready')
 api.post('runtime/stop',{});v._select_mode(api,'normal')
 for plugin in api.get('plugins')['plugins']:api.post('plugins/'+plugin['id']+'/disable',{})
 api.post('environments/ros2/config',{'config':{'domain_id':0}})
 for name,domain,extra,expected_subscribers in [('synthetic-final',73,[],1),('synthetic-observer-final',74,['--with-observer'],2)]:
  record['stage']=name;save()
  cmd=[sys.executable,str(w/'source/scripts/verify_ros2_diagnostics.py'),'--url','http://127.0.0.1:18765','--token-file',str(w/'data/secrets/admin.token'),'--scenario','synthetic','--domain',str(domain),'--repeat','2','--duration','30','--output',str(out/name)]+extra
  counts=[]
  with (out/(name+'.log')).open('wb') as log:
   p=subprocess.Popen(cmd,cwd=w/'source',env=env,stdout=log,stderr=subprocess.STDOUT)
   deadline=time.monotonic()+200
   while p.poll() is None:
    if time.monotonic()>deadline:raise RuntimeError('native synthetic timed out')
    graph=api.get('environments/ros2/graph');graph=graph.get('graph',graph)
    endpoints=api.get('environments/ros2/endpoints')
    endpoint=v.find_port(endpoints,'image',plugin_id='diag_image_probe')
    wanted=endpoint.get('topic')
    topics=[t for t in graph.get('topics',[]) if t['name']==wanted]
    if topics:
     t=topics[0]
     if t['publishers']:counts.append({'publisher_count':len(t['publishers']),'subscriber_count':len(t['subscriptions'])})
    with (out/(name+'-topology.jsonl')).open('a') as f:f.write(json.dumps({'at':v.utc_now(),'ex_pid':server.pid,'topics':topics,'endpoint':endpoint,'diagnostics':api.get('environments/diagnostics')})+'\n')
    time.sleep(.5)
  report=json.loads((out/name/'report.json').read_text())
  record['checks'][name]=p.returncode==0 and report['result']=='PASS'
  record['checks'][name+'-topology']=bool(counts) and any(c['subscriber_count']==expected_subscribers and c['publisher_count']==1 for c in counts) and all(c['subscriber_count']<=expected_subscribers for c in counts)
  record[name+'-counts']=counts;save()
  print(name,report['result'],'topology',record['checks'][name+'-topology'],flush=True)
except BaseException as exc:record['error']=type(exc).__name__+': '+str(exc)
finally:
 if server is not None and server.poll() is None:
  try:server.send_signal(signal.SIGINT);server.wait(timeout=25);record['checks']['private_ex_stopped']=server.returncode==0
  except Exception:
   os.killpg(server.pid,signal.SIGTERM);record['checks']['private_ex_stopped']=False
 record['stage']='finished';record['finished_at']=v.utc_now();record['result']='PASS' if not record.get('error') and all(record['checks'].values()) else 'FAIL';save()
 print(json.dumps({'result':record['result'],'checks':record['checks'],'error':record.get('error')}),flush=True)
