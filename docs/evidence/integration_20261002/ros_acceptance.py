import os,subprocess,threading,shutil,json
from pathlib import Path
from astrbot_ex.core.api_server import build_server
root=Path('/home/sssxy/Projects/AstrbotEX')
out=Path('/tmp/astrex_integration_20261002/ros-native-02');out.mkdir()
data=out/'instance';data.mkdir()
shutil.copytree(root/'examples/ros2_echo',data/'plugins/control/ros2_echo',ignore=shutil.ignore_patterns('__pycache__'))
os.environ.update(ASTRBOTEX_DATA_DIR=str(data),ASTRBOTEX_STT_ENABLED='',ASTRBOTEX_TTS_ENABLED='')
server=build_server('127.0.0.1',0,20)
thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
try:
 with (out/'roundtrip.log').open('w') as log:
  r=subprocess.run([str(root/'.venv/bin/python'),'scripts/verify_ros2_deployment.py','--url','http://127.0.0.1:'+str(server.server_address[1]),'--token-file',str(server.decision_management.credential_path)],cwd=root,stdout=log,stderr=subprocess.STDOUT,timeout=70)
 (out/'result.json').write_text(json.dumps({'pass':r.returncode==0,'returncode':r.returncode,'transport':'native ROS 2 Jazzy DDS','domain_id':73,'robot_commands':0,'restoration_record':'roundtrip.log'},indent=2))
finally:
 server.shutdown();thread.join(5);server.server_close()
raise SystemExit(r.returncode)
