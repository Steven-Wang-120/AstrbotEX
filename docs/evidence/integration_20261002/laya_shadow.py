"""Integration acceptance only: one warmup and one shadow, existing B08 helpers."""
import os,json,socket,threading,subprocess,sys
from pathlib import Path
from astrbot_ex.core.api_server import build_server
from astrbot_ex.core.decision.management import ManagementSettings
from astrbot_ex.core.decision.owned_laya import Deployment
from scripts.verify_decision_management import HTTP,compose_actor,until,require
from tests.test_goal_manager import goal_payload

root=Path('/home/sssxy/Projects/AstrbotEX')
out=Path('/tmp/astrex_integration_20261002/laya-shadow-02');out.mkdir()
data=out/'instance';data.mkdir()
with socket.socket() as sock:
 sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
python=root/'runtime/laya/.venv/bin/python'
metadata=subprocess.check_output([str(python),'-c','import json,torch,sys,importlib.metadata as m; print(json.dumps(dict(python=sys.executable,laya=m.version("laya"),torch=torch.__version__,cuda=torch.version.cuda,cuda_available=torch.cuda.is_available(),device=torch.cuda.get_device_name(0))))'],text=True)
result={'scope':'new independent Python; pinned shared weights; health + warmup + one shadow; no robot','environment':json.loads(metadata),'http':[],'pass':False}
os.environ.update(ASTRBOTEX_DATA_DIR=str(data),ASTRBOTEX_STT_ENABLED='',ASTRBOTEX_TTS_ENABLED='')
deployment=Deployment(python,Path('/data/shared/AstrEX_project_data/models/pretrained/laya/hub'),out/'owned-laya',port=port,device='cuda',state_path=data/'execution/laya/service-state.json')
server=build_server('127.0.0.1',0,20,management_settings=ManagementSettings(laya_deployment=deployment))
thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
actor=None
try:
 http=HTTP('http://127.0.0.1:'+str(server.server_address[1]),server.decision_management.credential_path.read_text().strip(),result['http'])
 owner,actor,_=compose_actor(server)
 config=http.get('/config')['saved'];config['backend']='laya';config['laya'].update(enabled=True,allow_live_http=True)
 http.post('/config',{'config':config})
 result['start']=http.run('/service/start')
 result['probe']=http.run('/test')
 require(result['probe']['result']['inference_called'] is False,'probe_inferred')
 result['shadow_mode']=http.run('/mode',{'mode':'shadow'})
 require(server.decision_service.backend.execution_allowed is False,'production_execution_permission_changed')
 service=server.decision_service
 service.submit_goal(goal_payload(service.goals,1,parameters={'arm.move.v1':{'meters':1}}))
 until(lambda:any(d['outcome']=='shadow' for d in service.status()['decisions']),'shadow_missing')
 result['stop']=http.run('/stop')
 result['decisions']=http.get('/decisions')
 result['actor_commands']=len(owner.commands)
 result['ledger_commands']=len(server.action_ledger.list_commands().result(1))
 require(result['actor_commands']==result['ledger_commands']==0,'shadow_executed')
 result['service']=server.decision_management.laya.status()
 result['pass']=True
finally:
 server.shutdown();thread.join(5)
 try:server.server_close()
 finally:
  if actor:actor.stop(2)
 result['cleanup']=server.decision_management.laya.status()
 result['cleanup']['process_exited']=all(h.get('exit_confirmed_monotonic_ns') for h in result['cleanup']['history'])
 result['pass']=result['pass'] and result['cleanup']['process_exited']
 (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({'pass':result['pass'],'actor_commands':result['actor_commands'],'ledger_commands':result['ledger_commands'],'process_exited':result['cleanup']['process_exited']}))
