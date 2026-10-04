import subprocess,json,os
from pathlib import Path
root=Path('/home/sssxy/Projects/AstrbotEX')
out=Path('/tmp/astrex_integration_20261002/legacy-browser-01');out.mkdir()
with (out/'fixture.txt').open('w') as log:
 p=subprocess.Popen([str(root/'.venv/bin/python'),'-m','tests.decision_ui_fixture'],cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,text=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
 try:
  ready=json.loads(p.stdout.readline())['ready']
  result=subprocess.run(['node','scripts/verify_dashboard_management.mjs','--base',ready['base'],'--token-file',ready['token_file'],'--output',str(out)],cwd=root)
 finally:
  p.stdin.write(json.dumps({'id':1,'command':'quit'})+'\n');p.stdin.flush();p.wait(20)
 if result.returncode:raise SystemExit(result.returncode)
