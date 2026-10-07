// C07 stage1: isolated frozen HTTP contract fixture, not real backend acceptance.
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';

const args=Object.fromEntries(process.argv.slice(2).reduce((a,v,i,s)=>{if(v.startsWith('--'))a.push([v.slice(2),s[i+1]]);return a;},[]));
if(!args.output || !args.chrome || !args.python)throw new Error('--output, --chrome and --python required');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const output=path.resolve(args.output);await fs.mkdir(output,{recursive:true});
const serverLog=await fs.open(path.join(output,'fixture.txt'),'w');
const actual=args.mode==='actual';
if(args.mode && !['actual','fixture'].includes(args.mode))throw new Error('--mode must be actual or fixture');
const fixture=spawn(args.python,['-B','-m',actual?'tests.decision_ui_real_fixture':'tests.decision_ui_fixture'],{cwd:root,windowsHide:true,stdio:['pipe','pipe',serverLog.fd],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',...(args['aeb-root']?{ASTRBOTEX_AEB_TEST_ROOT:path.resolve(args['aeb-root'])}:{})}});
const waiters=new Map();let fixtureID=0,ready,failure;
const fixtureState={pid:fixture.pid??null,exit_code:null,signal:null,terminated:false,timed_out:false,forced_kill:false};
const fixtureClosed=new Promise(resolve=>fixture.once('close',(code,signal)=>{Object.assign(fixtureState,{exit_code:code,signal,terminated:true});resolve();}));
createInterface({input:fixture.stdout}).on('line',line=>{
  try {const r=JSON.parse(line);if(r.ready)ready=r.ready;else if(Object.hasOwn(r,'cleanup'))evidence.fixture_cleanup=r.cleanup;else {const p=waiters.get(r.id);if(p){waiters.delete(r.id);r.error?p.reject(new Error(r.error)):p.resolve(r.result);}}}catch{failure='invalid fixture response';}
});
fixture.on('exit',(code,signal)=>{if(code!==0 || signal)failure='fixture exited '+code+(signal?' ('+signal+')':'');});
fixture.on('error',()=>{failure='fixture failed to start';fixtureState.spawn_error=failure;});
fixture.stdin.on('error',()=>{failure='fixture input failed';});
function fixtureCall(command,extra={}) {
  const id=++fixtureID;
  return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{waiters.delete(id);reject(new Error('fixture command timeout'));},15000);
    waiters.set(id,{resolve:r=>{clearTimeout(timer);resolve(r);},reject:e=>{clearTimeout(timer);reject(e);}});
    fixture.stdin.write(JSON.stringify({id,command,...extra})+'\n');});
}
let token='',socket,chrome,chromeState,chromeClosed,profile,profileOwner,chromeLog,sequence=0;
const pending=new Map(),requests=new Map(),dialogChoices=[];
const evidence={scope:actual?'C07 experimental actual build_server HTTP + builtin production transports to loopback synthetic suppliers. No real Key/provider/hardware. C06 not final.':'C07 stage1: isolated frozen decision HTTP contract fixture + real EX neighboring pages. No supplier inference, activation, model process, Goal, Action or physical motion.',checks:[],errors:[],screenshots:[],dialogs:[]};
const delay=ms=>new Promise(r=>setTimeout(r,ms));
const assert=(v,m)=>{if(!v)throw new Error(m);};
function cleanupFailure(message) {evidence.pass=false;evidence.errors.push(message);}
async function waitForTermination(closed,timeout) {
  let timer;
  try {return await Promise.race([closed.then(()=>true),new Promise(resolve=>{timer=setTimeout(()=>resolve(false),timeout);})]);}
  finally {clearTimeout(timer);}
}
async function terminateFixture() {
  try {
    if(!fixtureState.terminated) {
      if(fixture.exitCode===null && fixture.signalCode===null && !fixture.stdin.destroyed && !fixture.stdin.writableEnded)fixture.stdin.end(JSON.stringify({id:++fixtureID,command:'quit'})+'\n');
      if(!await waitForTermination(fixtureClosed,12000)) {
        fixtureState.timed_out=true;cleanupFailure('fixture cleanup/exit timeout');
        fixtureState.forced_kill=true;fixtureState.kill_sent=fixture.kill();
        if(!await waitForTermination(fixtureClosed,3000))cleanupFailure('fixture termination unconfirmed after forced kill');
      }
    }
  } catch(error) {cleanupFailure('fixture termination failed: '+error.message);}
  evidence.fixture_process={...fixtureState};
  if(!fixtureState.terminated || fixtureState.exit_code!==0 || fixtureState.signal || fixtureState.forced_kill || fixtureState.spawn_error)cleanupFailure('fixture did not exit cleanly');
  if(failure)cleanupFailure(failure);
  if(actual && evidence.fixture_cleanup?.ok!==true)cleanupFailure('actual fixture cleanup missing or unsuccessful');
}
async function until(fn,message,timeout=10000) {const end=Date.now()+timeout;while(Date.now()<end){const v=await fn();if(v)return v;await delay(40);}throw new Error(message);}
function call(method,params={}) {const id=++sequence;return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout: '+method));},15000);pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));});}
async function evaluate(expression) {const r=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,timeout:8000});if(r.exceptionDetails)throw new Error('browser evaluation failed');return r.result?.value;}
function listen(e) {
  if(e.id){const p=pending.get(e.id);if(p){pending.delete(e.id);clearTimeout(p.timer);e.error?p.reject(new Error(e.error.message)):p.resolve(e.result);}return;}
  if(e.method==='Runtime.exceptionThrown')evidence.errors.push('browser runtime exception');
  if(e.method==='Page.javascriptDialogOpening') {
    const choice=dialogChoices.shift(),accept=choice?.accept ?? true;
    evidence.dialogs.push({type:e.params.type,accept});
    call('Page.handleJavaScriptDialog',{accept}).then(()=>choice?.resolve(),error=>{evidence.errors.push('dialog handling failed');choice?.reject(error);});
  }
  if(e.method==='Network.requestWillBeSent') {const v=e.params,u=new URL(v.request.url),authorization=Object.entries(v.request.headers).find(([k])=>k.toLowerCase()==='authorization')?.[1];
    requests.set(v.requestId,{path:u.pathname,method:v.request.method,at:Date.now(),credential_in_url:u.href.includes(token),has_authorization:Boolean(authorization),uses_expected_authorization:authorization==='Bearer '+token,status:null});}
  if(e.method==='Network.responseReceived'){const r=requests.get(e.params.requestId);if(r)r.status=e.params.response.status;}
  if(['Network.loadingFinished','Network.loadingFailed'].includes(e.method)){const r=requests.get(e.params.requestId);if(r)r.ended_at=Date.now();}
}
const click=id=>evaluate(`document.getElementById(${JSON.stringify(id)}).click();true`);
const content=id=>evaluate(`document.getElementById(${JSON.stringify(id)}).textContent`);
const set=(id,value)=>evaluate(`(()=>{const n=document.getElementById(${JSON.stringify(id)});n.${typeof value==='boolean'?'checked':'value'}=${JSON.stringify(value)};n.dispatchEvent(new Event('input',{bubbles:true}));return true;})()`);
const writes=()=>[...requests.values()].filter(r=>r.method==='POST');
async function chooseDialog(accept,action) {
  let choice;
  const handled=new Promise((resolve,reject)=>{choice={accept,resolve,reject};dialogChoices.push(choice);});
  const timer=setTimeout(()=>{const i=dialogChoices.indexOf(choice);if(i>=0)dialogChoices.splice(i,1);choice.reject(new Error('expected native confirmation'));},10000);
  try {await Promise.all([handled,action()]);assert(evidence.dialogs.at(-1)?.type==='confirm','expected native confirm dialog');}
  finally {clearTimeout(timer);}
}
async function key(key,code,windowsVirtualKeyCode) {
  await call('Input.dispatchKeyEvent',{type:'keyDown',key,code,windowsVirtualKeyCode});
  await call('Input.dispatchKeyEvent',{type:'keyUp',key,code,windowsVirtualKeyCode});
}
async function pointerClick(id) {
  const point=await evaluate(`(()=>{const n=document.getElementById(${JSON.stringify(id)});n.scrollIntoView({block:'center'});const r=n.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  assert(await evaluate(`(()=>{const n=document.getElementById(${JSON.stringify(id)}),r=n.getBoundingClientRect(),hit=document.elementFromPoint(${point.x},${point.y});return !n.disabled && r.width>0 && r.height>0 && r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight && (hit===n || n.contains(hit));})()`),'button not reachable: '+id);
  await call('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});
  await call('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',clickCount:1});
}
const readConfig=()=>evaluate(`managementFetch('/api/v1/ex/decision/config').then(r=>r.json())`);
async function credential(value) {await set('managementCredential',value);await click('managementCredentialApply');}
async function uiReady() {await until(async()=>(await content('decisionFreshness'))==='当前状态已读取','view not ready');}
async function check(name,work) {evidence.current_check=name;await work();evidence.checks.push({name,pass:true});await fs.writeFile(path.join(output,'progress.json'),JSON.stringify(evidence,null,2));}
async function close() {await click('decisionCancel');await until(()=>evaluate(`!document.getElementById('decisionModal').open`),'modal did not close');}
async function open(provider) {await click('decisionProvider'+provider);await until(()=>evaluate(`document.getElementById('decisionModal').open`),'modal did not open');}
async function save() {await click('decisionSave');await until(()=>evaluate(`!document.getElementById('decisionModal').open`),'save did not close modal');await uiReady();}
async function refresh() {await click('decisionRefresh');await delay(1200);}
async function screenshot(name,width,height) {
  await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
  const banner=actual?'EXPERIMENTAL ACTUAL HTTP · synthetic suppliers · 非最终 C06 / 机器人验收':'TEST FIXTURE · C07 stage1 · 非最终后端 / 机器人验收';
  await evaluate(`(()=>{const b=document.createElement('div');b.id='c07FixtureBanner';b.textContent=${JSON.stringify(banner)};b.style='position:fixed;bottom:0;left:0;right:0;z-index:99999;padding:8px;background:#133d36;color:white;text-align:center;font:12px sans-serif';document.body.append(b);return true;})()`);
  const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});await fs.writeFile(path.join(output,name+'.png'),Buffer.from(shot.data,'base64'));evidence.screenshots.push(name+'.png');await evaluate(`document.getElementById('c07FixtureBanner').remove();true`);
}
async function removeOwnedProfile() {
  try {
    assert(profileOwner && profile===profileOwner.created,'profile creation ownership missing');
    const temp=await fs.realpath(os.tmpdir()),resolved=path.resolve(profile),canonical=await fs.realpath(profile);
    const tempStat=await fs.stat(temp,{bigint:true}),stat=await fs.lstat(profile,{bigint:true});
    assert(temp===profileOwner.temp && tempStat.dev===profileOwner.tempDev && tempStat.ino===profileOwner.tempIno && tempStat.birthtimeNs===profileOwner.tempBirth,'temporary root changed');
    assert(resolved===profileOwner.canonical && canonical===resolved && path.dirname(canonical)===temp && canonical!==path.parse(canonical).root && path.basename(canonical).startsWith('c07-chrome-'),'profile is not the owned canonical direct temp child');
    assert(stat.isDirectory() && !stat.isSymbolicLink() && stat.dev===profileOwner.dev && stat.ino===profileOwner.ino && stat.birthtimeNs===profileOwner.birth,'profile identity changed or is a junction/link');
    await fs.rm(canonical,{recursive:true,force:true,maxRetries:5,retryDelay:200});
    evidence.profile_cleanup={removed:true,canonical,temp,ownership_verified:true};
  } catch(error) {
    evidence.pass=false;evidence.profile_cleanup={removed:false,preserved:profile,reason:String(error.message)};
    evidence.errors.push('profile cleanup refused/failed: '+error.message);
  }
}
async function actualChecks() {
  const facts=async(label)=>{const value=await fixtureCall('facts');assert(!value.goal && !value.pending_goal && value.actions===0 && value.history===0,'actual Goal/Action side effects: '+label);(evidence.actual_facts??=[]).push({label,...value});return value;};
  const raw=expression=>evaluate(`(async()=>{${expression}})()`);
  await check('actual HTTP rejects anonymous read/write; fresh provider selection required',async()=>{
    const codes=await raw(`return Promise.all([fetch('/api/v1/ex/decision/view'),fetch('/api/v1/ex/decision/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})].map(async p=>(await p).status));`);
    assert(codes.every(v=>v===401),'anonymous actual HTTP allowed');
    await credential('invalid-c07-actual');await until(()=>evaluate(`!managementCredential && document.getElementById('managementCredentialStatus').textContent.includes('无效')`),'invalid actual credential allowed');
    await credential(token);await uiReady();assert(await evaluate(`document.getElementById('decisionStart').disabled`),'fresh mock implicitly startable');await facts('fresh');
  });
  for(const provider of ['jev','laya']) {
    await check('actual '+provider+' ephemeral draft probe no-save; key/config save; production start/stop',async()=>{
      await open(provider);await set('decisionAddress',ready.urls[provider]);
      if(provider==='laya')await set('decisionAuth','bearer');
      await set('decisionSecret','C07-actual-'+provider+'-ephemeral');
      const before=await readConfig(),initial=await facts(provider+' before draft');
      await pointerClick('decisionTest');await until(async()=>(await content('decisionTestResult')).includes('不代表已保存'),'actual draft probe failed');
      assert(JSON.stringify(await readConfig())===JSON.stringify(before),'actual draft probe saved config');
      const probe=await facts(provider+' after draft');assert(probe.disk_config_hash===initial.disk_config_hash && probe.runtime==='idle' && probe.mode==='disabled','draft changed runtime/disk');
      assert(probe.supplier_calls.filter(c=>c.provider===provider && c.method==='POST').length===1,'draft inference not once');
      await set('decisionSecret','C07-actual-'+provider+'-saved');await pointerClick('decisionSave');await until(()=>evaluate(`!document.getElementById('decisionModal').open`),'actual save did not close');await uiReady();
      const saved=await readConfig();assert(saved.saved.backend===provider && saved.secrets[provider].configured && saved.saved[provider].service_connection.base_url===ready.urls[provider],'actual config/key not saved');
      const stopped=await facts(provider+' saved');assert(stopped.runtime==='idle' && stopped.mode==='disabled','save started runtime');
      await until(()=>evaluate(`!document.getElementById('decisionStart').disabled`),'actual saved selection not startable');
      await pointerClick('decisionStart');await until(async()=>(await content('decisionEXState'))==='已启用','actual runtime not running',15000);
      const running=await facts(provider+' running');assert(running.runtime==='running' && running.control_mode==='decision' && running.mode==='execute' && running.execution_allowed && running.execution_capability.allowed && running.execution_capability.transport==='live','actual execute/production backend gate false');
      assert(running.backend_type===(provider==='jev'?'JevBackend':'LayaBackend') && running.supplier_calls.length===stopped.supplier_calls.length,'start inferred or wrong backend');
      assert(running.disk_config_hash===stopped.disk_config_hash,'start changed saved flags');
      if(provider==='jev') {
        await open('laya');await set('decisionAddress',ready.urls.laya);await click('decisionSave');
        await until(async()=>(await content('decisionModalMessage')).includes('请先停止 EX'),'actual business 409 mislabeled');
        assert(await evaluate(`document.getElementById('decisionReviewConfig').hidden`),'business 409 forced CAS review');
        await pointerClick('decisionModalStop');await until(async()=>(await content('decisionEXState'))==='已停用','actual modal stop failed');await close();
      } else {await pointerClick('decisionStop');await until(async()=>(await content('decisionEXState'))==='已停用','actual stop failed');}
      const ended=await facts(provider+' stopped');assert(ended.runtime==='idle' && ended.mode==='disabled' && !ended.gate_open,'actual runtime/gate not stopped');
    });
  }
  await check('actual Laya health200 inference401 not success; management auth preserved',async()=>{
    await fixtureCall('supplier_status',{status:401});await open('laya');await click('decisionTest');
    await until(async()=>(await content('decisionTestResult')).includes('认证失败'),'actual inference401 green');
    assert(await evaluate(`Boolean(managementCredential)`),'supplier401 cleared admin');
    const status=await raw(`return (await managementFetch('/api/v1/ex/decision/status')).json();`);
    assert(status.probe.health_ok===true && status.probe.inference_ok===false && status.probe.error_code==='http_401','actual health/inference distinction');
    await close();await fixtureCall('supplier_status',{status:200});await facts('401 probe');
  });
  await check('actual CAS409 preserves draft and requires explicit review',async()=>{
    await open('laya');await set('decisionAddress',ready.urls.laya+'/edited');await fixtureCall('change_config');await click('decisionSave');
    await until(async()=>(await content('decisionModalMessage')).includes('配置已变化'),'actual CAS not shown');
    assert(await evaluate(`document.getElementById('decisionAddress').value===${JSON.stringify(ready.urls.laya+'/edited')} && !document.getElementById('decisionReviewConfig').hidden`),'actual CAS lost draft');await close();
  });
  if(args['aeb-root']) {
    await check('actual AEB projection handler bidirectional ZMQ + nested EX capabilities: scoped idle',async()=>{
      const start=await fixtureCall('projection_start');evidence.aeb_source_hashes=start.source_hashes;
      await refresh();await until(async()=>(await content('decisionTaskStatus'))==='暂无任务','actual scoped idle unavailable',15000);
      const projected=await raw(`return (await managementFetch('/api/v1/ex/decision/view')).json();`);
      assert(projected.task.available && projected.task.phase==='idle' && !projected.task.title,'actual idle projection');
      const p=await fixtureCall('projection_facts');assert(p.connected && p.projection_calls>0 && p.nested_capability_calls>=p.projection_calls,'missing nested actual capabilities');evidence.projection_idle=p;
    });
    await check('actual TaskStore summary on browser, no plan/identity leakage; disconnect unavailable',async()=>{
      const task=await fixtureCall('projection_task');await refresh();
      await until(async()=>(await content('decisionTaskTitle'))===task.task.title,'actual Store title not projected',15000);
      assert((await content('decisionTaskStatus'))==='正在规划' && (await content('decisionTaskProgress')).includes('0 / 1'),'actual task status/progress');
      const projected=await raw(`return (await managementFetch('/api/v1/ex/decision/view')).json();`);
      assert(projected.task.available && projected.task.title===task.task.title && projected.task.phase===task.task.phase && projected.task.total===1 && projected.task.completed===0 && projected.task.current_goal===null,'actual Store projection differs');
      const publicText=JSON.stringify(projected)+await evaluate(`document.getElementById('page-decision').textContent`);
      for(const marker of ['c07-private-user','c07-private-route','fixture-private-origin','Hold for explicit planning','Actual result required'])assert(!publicText.includes(marker),'projection leaked private identity/plan');
      evidence.projection_task=await fixtureCall('projection_facts');await facts('projection task without Goal');
      await screenshot('02-actual-zmq-store-task',1440,1000);await fixtureCall('projection_disconnect');await refresh();
      await until(async()=>(await content('decisionTaskStatus'))==='任务暂不可确认','disconnect retained live task',15000);
      assert((await content('decisionTaskTitle'))==='','disconnect retained task title');
      evidence.projection_disconnected=await fixtureCall('projection_facts');await facts('projection disconnected');
    });
  }
  await check('actual secrets absent DOM URL/storage and reload forgets admin',async()=>{
    await open('laya');assert(await evaluate(`document.getElementById('decisionSecret').value===''`),'actual key returned to input');await close();
    const stored=await evaluate(`JSON.stringify({local:Object.values(localStorage),session:Object.values(sessionStorage),html:document.documentElement.outerHTML})`);
    for(const marker of [token,...['jev','laya'].flatMap(p=>['ephemeral','saved'].map(k=>'C07-actual-'+p+'-'+k))])assert(!stored.includes(marker),'actual secret exposed');
    assert(![...requests.values()].some(r=>r.credential_in_url),'actual credential URL');
    await screenshot('01-actual-http-stopped',1440,1000);
    const before=writes().length;await call('Page.reload',{ignoreCache:true});await until(()=>evaluate(`document.readyState==='complete' && Boolean(window.DecisionPage) && !managementCredential`),'actual reload auth retained');await delay(800);assert(writes().length===before,'actual reload writes');
    evidence.side_effects=await facts('end');
  });
}
try {
  await until(()=>{if(failure)throw new Error(failure);return ready;},'fixture startup',20000);token=ready.token;
  if(actual)evidence.source_hashes=ready.source_hashes;
  const temp=await fs.realpath(os.tmpdir()),tempStat=await fs.stat(temp,{bigint:true});
  profile=await fs.mkdtemp(path.join(temp,'c07-chrome-'));
  const canonical=await fs.realpath(profile),stat=await fs.lstat(profile,{bigint:true});
  assert(canonical===path.resolve(profile) && path.dirname(canonical)===temp && stat.isDirectory() && !stat.isSymbolicLink(),'created profile is not a canonical temporary directory');
  profileOwner=Object.freeze({created:profile,canonical,temp,dev:stat.dev,ino:stat.ino,birth:stat.birthtimeNs,tempDev:tempStat.dev,tempIno:tempStat.ino,tempBirth:tempStat.birthtimeNs});
  chromeLog=await fs.open(path.join(output,'chrome.txt'),'w');
  chrome=spawn(args.chrome,['--headless=new','--disable-gpu','--disable-background-networking','--no-first-run','--no-default-browser-check','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:['ignore',chromeLog.fd,chromeLog.fd]});
  chromeState={pid:chrome.pid??null,terminated:false};
  chromeClosed=new Promise(resolve=>chrome.once('close',(code,signal)=>{Object.assign(chromeState,{exit_code:code,signal,terminated:true});resolve();}));
  chrome.on('error',()=>{failure='chrome failed to start';});
  const info=await until(async()=>{try{return (await fs.readFile(path.join(profile,'DevToolsActivePort'),'utf8')).trim().split('\n');}catch{return null;}},'CDP startup');
  const targets=await (await fetch('http://127.0.0.1:'+info[0]+'/json/list')).json();socket=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
  socket.addEventListener('message',e=>listen(JSON.parse(e.data)));await new Promise((r,j)=>{socket.addEventListener('open',r,{once:true});socket.addEventListener('error',j,{once:true});});
  await call('Page.enable');await call('Runtime.enable');await call('Network.enable');
  await call('Page.navigate',{url:ready.base+'/#/decision'});await until(()=>evaluate(`document.readyState==='complete' && Boolean(window.DecisionPage)`),'page load');
  if(actual) {await actualChecks();}
  else {
  await check('empty / invalid admin credential; no implicit writes',async()=>{
    assert(await evaluate(`document.getElementById('decisionStop').disabled`),'stop without auth');
    await credential('invalid-c07-fixture');await until(()=>[...requests.values()].some(r=>r.status===401),'expected 401');
    await delay(1400);assert(writes().length===0,'automatic write');
  });
  await credential(token);await uiReady();
  await check('minimal two cards, no internal panels, pinned models, idle versus unavailable',async()=>{
    assert(await evaluate(`document.querySelectorAll('.decision-provider').length===2`),'provider count');
    assert(await evaluate(`!document.querySelector('#page-decision pre, #page-decision details, #decisionTaskCancel')`),'internal panel/task cancel');
    assert((await content('decisionTaskStatus'))==='暂无任务','idle');
    await fixtureCall('configure',{task:{available:false,phase:'unavailable'}});await refresh();assert((await content('decisionTaskStatus'))==='任务暂不可确认','unavailable conflated');
    await open('jev');assert(await evaluate(`document.getElementById('decisionModel').options.length===1 && document.getElementById('decisionModel').value==='jev-1.13.0'`),'Jev model');
    assert(await evaluate(`document.getElementById('decisionLayaFields').hidden`),'Jev extra fields');await close();
    await open('laya');assert(await evaluate(`document.getElementById('decisionModel').value==='typed-decisions' && document.getElementById('decisionOwnedFields').hidden`),'external form');await close();
  });
  await check('full config save preserves hidden defaults and references; blank key keep',async()=>{
    const before=await readConfig();await open('jev');await set('decisionAddress','https://fixture.invalid/prefix');await save();
    const after=await readConfig(),expected=structuredClone(before.saved);expected.backend='jev';expected.jev.service_connection.base_url='https://fixture.invalid/prefix';
    assert(JSON.stringify(after.saved)===JSON.stringify(expected),'hidden config changed');
    const log=await fixtureCall('requests');const secret=log.filter(r=>r.path==='/secret').at(-1).body;assert(secret.provider==='jev' && secret.action==='keep' && !('value' in secret),'blank key semantics');
    assert(!log.some(r=>['/mode','/stop','/test'].includes(r.path) && r.method==='POST'),'save activated/probed');
  });
  await check('Laya external auth and owned explicit deployment, no invented defaults',async()=>{
    await open('laya');await set('decisionLayaMode','owned');
    assert(await evaluate(`!document.getElementById('decisionOwnedFields').hidden && !document.getElementById('decisionPython').value && !document.getElementById('decisionCache').value && !document.getElementById('decisionDevice').value`),'invented owned deployment');
    await set('decisionLayaMode','external');await set('decisionAuth','bearer');await set('decisionSecret','C07-fixture-supplier-key');await save();
    const c=await readConfig();assert(c.secrets.laya.configured && !c.secrets.jev.configured && c.saved.laya.deployment===null,'provider key isolation');
    await open('laya');assert(await evaluate(`document.getElementById('decisionSecret').value===''`),'key reflected');await close();
  });
  await check('draft test once; no persistence; health 200 + inference 401 not success',async()=>{
    await fixtureCall('configure',{test_result:{ok:false,inference_ok:false,health_ok:true,error_code:'http_401'},delay:0.8});
    await open('laya');await set('decisionAddress','https://draft.fixture.invalid');await set('decisionSecret','C07-ephemeral-key');
    const before=await readConfig(),n=(await fixtureCall('requests')).filter(r=>r.path==='/test' && r.method==='POST').length;
    await click('decisionTest');await click('decisionTest');await until(async()=>(await content('decisionTestResult')).includes('认证失败'),'health only accepted');
    const after=await readConfig();assert(JSON.stringify(before)===JSON.stringify(after),'test saved draft');
    const log=await fixtureCall('requests');assert(log.filter(r=>r.path==='/test' && r.method==='POST').length===n+1,'duplicate probe');
    assert(log.filter(r=>r.path==='/test' && r.method==='POST').at(-1).body.value==='[redacted]','ephemeral omitted');await close();
  });
  await check('draft success is not saved verification; edits fence late test results',async()=>{
    await fixtureCall('configure',{test_result:{ok:true,inference_ok:true,error_code:null},delay:1.1});
    await open('laya');await set('decisionAddress','https://different.fixture.invalid');await click('decisionTest');
    await until(async()=>(await content('decisionTestResult')).includes('不代表已保存'),'draft falsely proves saved');
    await click('decisionTest');await set('decisionAddress','https://newer.fixture.invalid');await delay(1700);
    assert((await content('decisionTestResult'))==='','late test overwrote edited draft');
    await close();await open('jev');await delay(600);assert((await content('decisionTestResult'))==='','provider switch stale probe');await close();
  });
  await check('poll protects draft; CAS review preserves edits without blind retry',async()=>{
    await fixtureCall('configure',{delay:0});await open('jev');await set('decisionAddress','https://cas.fixture.invalid');
    await delay(1300);assert(await evaluate(`document.getElementById('decisionAddress').value==='https://cas.fixture.invalid'`),'poll overwrote draft');
    await fixtureCall('change_config');await click('decisionSave');
    await until(async()=>(await content('decisionModalMessage')).includes('配置已变化'),'CAS message');
    assert(await evaluate(`document.getElementById('decisionAddress').value==='https://cas.fixture.invalid'`),'CAS lost draft');
    await until(()=>evaluate(`!document.getElementById('decisionReviewConfig').hidden`),'review unavailable');await delay(1200);await click('decisionReviewConfig');await save();
    assert((await readConfig()).saved.jev.service_connection.base_url==='https://cas.fixture.invalid','rebase discarded draft');
  });
  await check('business 409 distinct from CAS; stop reachable inside dirty modal',async()=>{
    await fixtureCall('configure',{mode:'running'});await refresh();await open('laya');await set('decisionAddress','https://business.fixture.invalid');await click('decisionSave');
    await until(async()=>(await content('decisionModalMessage')).includes('请先停止 EX'),'business conflict mapped to CAS');
    assert(await evaluate(`document.getElementById('decisionReviewConfig').hidden`),'business conflict forced rebase');
    assert(!await evaluate(`document.getElementById('decisionModalStop').disabled`),'modal stop blocked');await click('decisionModalStop');
    await until(async()=>(await content('decisionEXState'))==='已停用','stop not projected');await close();
  });
  await check('EX start uses execute; no optimistic running; stop during activation',async()=>{
    await fixtureCall('configure',{mode:'disabled',delay:2});await refresh();await click('decisionStart');
    assert((await content('decisionEXState'))==='已停用','optimistic running');
    assert(!await evaluate(`document.getElementById('decisionStop').disabled`),'activation blocks stop');
    await click('decisionStop');await delay(3100);await refresh();assert((await content('decisionEXState'))==='已停用','late start replayed');
    const log=await fixtureCall('requests');assert(log.some(r=>r.path==='/mode' && r.body.mode==='execute'),'wrong start endpoint');
    await fixtureCall('configure',{delay:0});await click('decisionStart');await until(async()=>(await content('decisionEXState'))==='已启用','server running not painted');await click('decisionStop');await until(async()=>(await content('decisionEXState'))==='已停用','stop not painted');
  });
  await check('explicit secret clear only selected provider',async()=>{
    await open('laya');await set('decisionSecretClear',true);assert(await evaluate(`document.getElementById('decisionTest').disabled`),'clear draft can test saved key');await save();
    assert(!(await readConfig()).secrets.laya.configured,'clear failed');
  });
  await check('task real title/goal/progress/status; safe text; no stale task on disconnect',async()=>{
    await fixtureCall('configure',{task:{available:true,phase:'needs_planning',title:'<img src=x onerror="window.c07Injected=true">',current_goal:'当前真实目标（隔离测试）',completed:2,total:3}});await refresh();
    assert((await content('decisionTaskStatus'))==='等待重新规划' && (await content('decisionTaskProgress')).includes('2 / 3'),'task projection');
    assert(await evaluate(`!window.c07Injected && !document.querySelector('#page-decision img')`),'XSS');
    await screenshot('01-desktop-fixture',1440,1000);
    await call('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:-1,uploadThroughput:-1});
    await until(async()=>(await content('decisionTaskStatus'))==='任务暂不可确认','stale task still active');
    assert((await content('decisionTaskTitle'))==='','stale title shown as active');
    await call('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});await uiReady();
    for(const phase of ['canceled','resume_review','completed']){await fixtureCall('configure',{task:{available:true,phase,title:'隔离契约任务',completed:1,total:2}});await refresh();assert((await content('decisionTaskStatus'))!=='任务暂不可确认','phase missing '+phase);}
    const before=writes().length,fixtureWrites=(await fixtureCall('requests')).filter(r=>r.method==='POST').length;
    evidence.task_phase_labels=[];
    for(const [phase,label] of [['lease_lost','执行授权失效，等待复核'],['canceling','正在取消'],['waiting_input','等待输入']]) {
      await fixtureCall('configure',{task:{available:true,phase,title:'隔离契约任务',completed:1,total:2}});await refresh();
      assert((await content('decisionTaskStatus'))===label,'producer phase mislabeled '+phase);
      assert(await evaluate(`!document.getElementById('decisionTaskCancel')`),'task projection exposes cross-user cancel');
      assert(writes().length===before && (await fixtureCall('requests')).filter(r=>r.method==='POST').length===fixtureWrites,'task phase projection wrote/canceled');
      evidence.task_phase_labels.push({phase,label,no_write:true,no_cancel_control:true});
    }
  });
  await check('Escape discard only when dirty; focus restore; native modal contains focus',async()=>{
    await open('laya');await evaluate(`document.getElementById('decisionCancel').focus();true`);await call('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});await call('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
    assert(await evaluate(`document.getElementById('decisionModal').contains(document.activeElement)`),'focus escaped dialog');
    await call('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});await call('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
    await until(()=>evaluate(`!document.getElementById('decisionModal').open`),'escape not close');
    assert(await evaluate(`document.activeElement.id==='decisionProviderlaya'`),'focus not restored');
    await open('jev');await set('decisionAddress','https://discard.fixture.invalid');
    const snapshot=()=>evaluate(`JSON.stringify({draft:Array.from(document.getElementById('decisionConfigForm').elements,n=>[n.id,n.value,n.checked]),dirty:DecisionPage.hasDraft(),modal:document.getElementById('decisionModal').open,focus:document.activeElement.id,page:state.activePage,active:document.querySelector('.page.active').id,nav:document.querySelector('.nav-item.active').dataset.page,hash:location.hash})`);
    const before=writes().length,dialogs=evidence.dialogs.length;
    for(const [name,focus,action] of [
      ['close','decisionModalClose',()=>pointerClick('decisionModalClose')],
      ['cancel','decisionCancel',()=>pointerClick('decisionCancel')],
      ['Escape','decisionAddress',()=>key('Escape','Escape',27)],
      ['navigation','decisionAddress',()=>evaluate(`document.querySelector('[data-page="environments"]').click();true`)],
      ['hashchange','decisionAddress',()=>evaluate(`location.hash='#/logs';true`)],
      ['back','decisionAddress',()=>evaluate(`history.replaceState(null,'','#/core');history.pushState(null,'','#/decision');history.back();true`)]
    ]) {
      await evaluate(`document.getElementById(${JSON.stringify(focus)}).focus();true`);
      const saved=await snapshot();await chooseDialog(false,action);
      await until(()=>evaluate(`location.hash==='#/decision'`),'declined '+name+' left wrong hash');
      assert(await snapshot()===saved,'declined '+name+' changed draft/modal/focus/navigation');
    }
    assert(evidence.dialogs.length===dialogs+6 && evidence.dialogs.slice(dialogs).every(d=>!d.accept),'discard not protected by six native declined confirmations');
    assert(writes().length===before,'decline wrote draft');
    await chooseDialog(true,()=>click('decisionCancel'));await until(()=>evaluate(`!document.getElementById('decisionModal').open`),'accepted discard did not close');
    assert(await evaluate(`document.activeElement.id==='decisionProviderjev' && !DecisionPage.hasDraft()`),'accepted discard did not restore focus/clear draft');
    await open('jev');await set('decisionAddress','https://accepted.fixture.invalid');
    await chooseDialog(true,()=>evaluate(`location.hash='#/environments';true`));
    await until(()=>evaluate(`state.activePage==='environments' && location.hash==='#/environments' && !document.getElementById('decisionModal').open && !DecisionPage.hasDraft()`),'accepted leave failed');
    await evaluate(`document.querySelector('[data-page="decision"]').click();true`);await uiReady();
  });
  await check('credentials + session fence held reads, drafts require review; management 401 vs 403',async()=>{
    await open('jev');await set('decisionAddress','https://credential.fixture.invalid');await set('decisionSecret','C07-unsaved-secret');
    await fixtureCall('configure',{hold_config:1.8});await click('decisionRefresh');await delay(1100);await credential(token);
    await delay(2400);await uiReady();assert(await evaluate(`document.getElementById('decisionSecret').value==='' && document.getElementById('decisionSave').disabled`),'credential reauthorizes draft');
    await click('decisionReviewConfig');await close();
    await fixtureCall('restart');await refresh();await uiReady();
    await fixtureCall('configure',{fault:{path:'/view',status:403,code:'invalid_host_or_origin'}});await refresh();assert(await evaluate(`Boolean(managementCredential)`),'403 clears admin');
    await uiReady();await fixtureCall('configure',{fault:{path:'/view',status:401,code:'unauthorized'}});await refresh();assert(await evaluate(`!managementCredential`),'401 did not clear');await credential(token);await uiReady();
  });
  await check('route-not-found view truthful; configuration error does not remove safety stop',async()=>{
    await fixtureCall('configure',{fault:{path:'/view',status:404,code:'route_not_found'}});await refresh();assert((await content('decisionEXState'))==='状态暂不可确认','stage A invents running');
    assert(!await evaluate(`document.getElementById('decisionStop').disabled`),'no view removes stop');await uiReady();
    await fixtureCall('configure',{fault:{path:'/config',status:500,code:'storage_write_uncertain'}});await refresh();assert(!await evaluate(`document.getElementById('decisionStop').disabled`),'config error removes stop');await uiReady();
  });
  await check('timeout retains draft and stop; no blind replay',async()=>{
    await fixtureCall('configure',{delay:35});await open('laya');await set('decisionAddress','https://timeout.fixture.invalid');await click('decisionTest');
    await call('Network.emulateNetworkConditions',{offline:false,latency:11000,downloadThroughput:-1,uploadThroughput:-1});
    await until(async()=>(await content('decisionTestResult')).includes('超时'),'timeout message',14000);
    assert(await evaluate(`document.getElementById('decisionAddress').value==='https://timeout.fixture.invalid' && !document.getElementById('decisionModalStop').disabled`),'timeout lost draft/stop');
    await call('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});await fixtureCall('configure',{delay:0});await close();await uiReady();
  });
  await check('390px page/modal no horizontal overflow; pointer controls and owned modal stop reachable',async()=>{
    await fixtureCall('configure',{delay:0,test_result:{ok:true,inference_ok:true,error_code:null}});await uiReady();
    await call('Emulation.setDeviceMetricsOverride',{width:390,height:700,deviceScaleFactor:1,mobile:false});
    const noOverflow=()=>evaluate(`(()=>{const d=document.getElementById('decisionModal'),p=document.getElementById('page-decision'),r=d.getBoundingClientRect();return innerWidth===390 && document.documentElement.scrollWidth<=innerWidth && p.scrollWidth<=p.clientWidth && (!d.open || (d.scrollWidth<=d.clientWidth && r.left>=0 && r.right<=innerWidth));})()`);
    assert(await noOverflow(),'390px page overflow');await pointerClick('decisionRefresh');await uiReady();
    await pointerClick('decisionProviderlaya');await until(()=>evaluate(`document.getElementById('decisionModal').open`),'390px card not usable');
    await set('decisionLayaMode','owned');assert(await noOverflow(),'390px owned modal overflow');
    await pointerClick('decisionModalStop');await until(async()=>(await content('decisionEXState'))==='已停用','390px modal stop failed');
    await screenshot('04-mobile-owned-modal-fixture',390,700);assert(await noOverflow(),'390px screenshot modal overflow');
    await set('decisionLayaMode','external');await set('decisionAddress','https://mobile.fixture.invalid');
    const n=(await fixtureCall('requests')).filter(r=>r.path==='/test' && r.method==='POST').length;
    await pointerClick('decisionTest');await until(async()=>(await content('decisionTestResult')).includes('不代表已保存'),'390px test not usable');
    assert((await fixtureCall('requests')).filter(r=>r.path==='/test' && r.method==='POST').length===n+1,'390px test request count');
    await pointerClick('decisionSave');await until(()=>evaluate(`!document.getElementById('decisionModal').open`),'390px save not usable');await uiReady();
    assert((await readConfig()).saved.laya.service_connection.base_url==='https://mobile.fixture.invalid','390px save failed');
    await pointerClick('decisionStart');await until(async()=>(await content('decisionEXState'))==='已启用','390px start not usable');
    await pointerClick('decisionStop');await until(async()=>(await content('decisionEXState'))==='已停用','390px stop not usable');
    assert(await noOverflow(),'390px page overflow after operations');
    await call('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  });
  await check('hidden/navigation pauses reads; other page draft intact; storage/URL never contain keys',async()=>{
    await evaluate(`Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));true`);await delay(400);
    const n=(await fixtureCall('requests')).length;await delay(1200);assert((await fixtureCall('requests')).length===n,'hidden continues reads');
    await evaluate(`delete document.hidden;document.dispatchEvent(new Event('visibilitychange'));true`);await uiReady();
    await evaluate(`document.querySelector('[data-page="environments"]').click();true`);await evaluate('refreshEnvironment()');await set('ros2DiscoveryInterval','2.37');await evaluate('refreshEnvironment()');assert(await evaluate(`document.getElementById('ros2DiscoveryInterval').value==='2.37'`),'other page regression');
    const count=(await fixtureCall('requests')).length;await delay(1200);assert((await fixtureCall('requests')).length===count,'left decision keeps reads');
    await evaluate(`document.querySelector('[data-page="decision"]').click();true`);await uiReady();
    await screenshot('02-narrow-fixture',820,1100);assert(await evaluate(`document.documentElement.scrollWidth<=innerWidth`),'narrow overflow');
    await open('laya');await screenshot('03-modal-fixture',820,1100);await close();
    const stored=await evaluate(`JSON.stringify({local:Object.values(localStorage),session:Object.values(sessionStorage),html:document.documentElement.outerHTML})`);
    assert(!stored.includes(token) && !stored.includes('C07-fixture-supplier-key') && !stored.includes('C07-ephemeral-key') && !stored.includes('C07-unsaved-secret'),'secret stored/reflected');
    assert(![...requests.values()].some(r=>r.credential_in_url),'credential URL');
    const before=writes().length;await call('Page.reload',{ignoreCache:true});await until(()=>evaluate(`document.readyState==='complete' && Boolean(window.DecisionPage) && !managementCredential`),'reload auth persists');await delay(1200);assert(writes().length===before,'reload writes');
  });
  const facts=await fixtureCall('facts');assert(!facts.goal && facts.actions===0 && facts.runtime==='idle','fixture caused runtime/action effects');
  evidence.side_effects={goal: facts.goal,actions: facts.actions,runtime:facts.runtime};
  }
  assert(evidence.errors.length===0,'browser errors');evidence.pass=true;
} catch(error) {evidence.pass=false;evidence.errors.push(String(error.stack||error).split(token).join('[redacted]'));}
finally {
  evidence.requests=[...requests.values()];
  if(socket?.readyState===WebSocket.OPEN)socket.close();
  for(const p of pending.values()){clearTimeout(p.timer);p.reject(new Error('browser closed'));}pending.clear();
  if(chrome) {
    try {
      if(!chromeState.terminated && chrome.exitCode===null && chrome.signalCode===null)chrome.kill();
      if(!await waitForTermination(chromeClosed,3000))cleanupFailure('chrome termination unconfirmed');
    } catch(error) {cleanupFailure('chrome termination failed: '+error.message);}
    evidence.chrome_process={...chromeState};
  }
  await terminateFixture();
  for(const p of waiters.values())p.reject(new Error('fixture closed'));waiters.clear();
  if(profile) {
    if(!chrome || chromeState.terminated)await removeOwnedProfile();
    else {evidence.profile_cleanup={removed:false,preserved:profile,reason:'chrome termination unconfirmed'};cleanupFailure('profile preserved: chrome termination unconfirmed');}
  }
  await chromeLog?.close();await serverLog.close();
  await fs.writeFile(path.join(output,'result.json'),JSON.stringify(evidence,null,2));
  console.log(JSON.stringify({pass:evidence.pass,checks:evidence.checks.length,errors:evidence.errors,output}));process.exitCode=evidence.pass?0:1;
}
