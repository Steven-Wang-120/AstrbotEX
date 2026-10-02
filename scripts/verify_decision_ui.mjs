// B09: real temporary B08 HTTP + installed Chrome/CDP. No GPU or robot instance.
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';

const args=Object.fromEntries(process.argv.slice(2).reduce((a,v,i,s)=>{if(v.startsWith('--'))a.push([v.slice(2),s[i+1]]);return a;},[]));
if(!args.output)throw new Error('--output required');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const output=path.resolve(args.output);await fs.mkdir(output,{recursive:true,mode:0o700});
const serverLog=await fs.open(path.join(output,'fixture.txt'),'w');
const fixture=spawn(args.python||path.join(root,'.venv/bin/python'),['-m','tests.decision_ui_fixture'],
  {cwd:root,stdio:['pipe','pipe',serverLog.fd],env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
let fixtureID=0, ready, fixtureFailure;
const fixtureWaiters=new Map();
createInterface({input:fixture.stdout}).on('line',line=>{
  try {const r=JSON.parse(line);if(r.ready)ready=r.ready;else{const p=fixtureWaiters.get(r.id);if(p){fixtureWaiters.delete(r.id);r.error?p.reject(new Error(r.error)):p.resolve(r.result);}}}catch{fixtureFailure='fixture emitted invalid JSON';}
});
fixture.on('exit',code=>{if(code)fixtureFailure='fixture exited '+code;});
function fixtureCall(command, extra={}) {
  const id=++fixtureID;
  return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{fixtureWaiters.delete(id);reject(new Error('fixture timeout: '+command));},45000);
    fixtureWaiters.set(id,{resolve:r=>{clearTimeout(timer);resolve(r);},reject:e=>{clearTimeout(timer);reject(e);}});
    fixture.stdin.write(JSON.stringify({id,command,...extra})+'\n');});
}
let token='',socket,chrome,profile,chromeLog;
let sequence=0;
const pending=new Map(),requests=new Map();
const evidence={scope:'real B08 HTTP, existing fake Laya owned process/health; isolated Mock test Actor; explicitly injected display/race fixtures',checks:[],requests:[],errors:[],screenshots:[]};
function assert(value, message) {if (!value) throw new Error(message);}
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
async function until(predicate, message, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await predicate();
    if (value) return value;
    await delay(25);
  }
  throw new Error(message);
}
function call(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {pending.delete(id); reject(new Error('CDP deadline: ' + method));}, 15000);
    pending.set(id, {resolve, reject, timer});
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  let result;try{result=await call('Runtime.evaluate', {expression, awaitPromise:true, returnByValue:true, timeout:8000});}catch(error){throw new Error(error.message+' at '+scrub(expression).replaceAll('B09-fixture-provider-secret','[redacted]'));}
  if (result.exceptionDetails) throw new Error('browser evaluation failed: ' + result.exceptionDetails.text);
  return result.result?.value;
}
function listen(event) {
  if(event.method==='Debugger.paused')evidence.paused_frames=event.params.callFrames.map(f=>({function:f.functionName,url:f.url,location:f.location}));
  if(event.method==='Page.javascriptDialogOpening'){evidence.dialog=event.params.type;call('Page.handleJavaScriptDialog',{accept:true}).catch(()=>{});}
  if(event.method==='Runtime.exceptionThrown')evidence.errors.push('browser: '+(event.params.exceptionDetails.exception?.description || event.params.exceptionDetails.text));
  if (event.id) {
    const request = pending.get(event.id);
    if (!request) return;
    pending.delete(event.id);
    clearTimeout(request.timer);
    if (event.error) request.reject(new Error(event.error.message)); else request.resolve(event.result);
    return;
  }
  if (event.method === 'Network.requestWillBeSent') {
    const value = event.params;
    const url = new URL(value.request.url);
    const api = url.pathname.startsWith('/api/');
    const authorization = Object.entries(value.request.headers).find(([key]) => key.toLowerCase() === 'authorization')?.[1];
    requests.set(value.requestId, {request_id: value.requestId, path: url.pathname,
      method: value.request.method, at: Date.now(), key: url.pathname+url.search, is_api: api, has_authorization: Boolean(authorization),
      uses_expected_authorization: authorization === 'Bearer ' + token,
      credential_in_url: value.request.url.includes(token), status: null});
  }
  if(['Network.loadingFinished','Network.loadingFailed'].includes(event.method)){const row=requests.get(event.params.requestId);if(row)row.ended_at=Date.now();}
  if (event.method === 'Network.responseReceived') {
    const row = requests.get(event.params.requestId);
    if (row) row.status = event.params.response.status;
  }
  
}
const rows = () => [...requests.values()];
const writes = () => rows().filter(row => row.is_api && !['GET', 'OPTIONS', 'HEAD'].includes(row.method));
async function enterCredential(value) {
  await evaluate(`(() => {const input=document.getElementById('managementCredential');
    input.value=${JSON.stringify(value)};input.dispatchEvent(new Event('input',{bubbles:true}));
    document.getElementById('managementCredentialApply').click();return true;})()`);
}
async function browserStatus() {
  return evaluate(`(async()=>{const response=await managementFetch('/api/v1/ex/decision/status');
    return {status:response.status,value:await response.json()};})()`);
}
function scrub(value) {
  if (typeof value === 'string') return value.split(token).join('[management credential redacted]');
  if (Array.isArray(value)) return value.map(scrub);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key,item])=>[key,scrub(item)]));
  return value;
}
const click=id=>evaluate(`document.getElementById(${JSON.stringify(id)}).click();true`);
const content=id=>evaluate(`document.getElementById(${JSON.stringify(id)}).textContent`);
const set=async(id,value,event='input')=>evaluate(`(()=>{const n=document.getElementById(${JSON.stringify(id)});${typeof value==='boolean'?'n.checked':'n.value'}=${JSON.stringify(value)};n.dispatchEvent(new Event(${JSON.stringify(event)},{bubbles:true}));return true;})()`);
const readJSON=suffix=>evaluate(`(async()=>{const r=await managementFetch('/api/v1/ex/decision'+${JSON.stringify(suffix)});return r.json();})()`);
const uiReady=()=>until(async()=>/saved \d/.test(await content('decisionConfigVersions')) && !await evaluate(`document.getElementById('decisionReloadConfig').disabled`),'decision config not ready');
async function check(name,work) {evidence.current_check=name;await work();evidence.checks.push({name,pass:true});await fs.writeFile(path.join(output,'progress.json'),JSON.stringify(scrub(evidence),null,2));}
async function opClick(id, kind, state='succeeded') {
  const before=await evaluate(`Array.from(document.querySelectorAll('[data-operation-id]'),n=>n.dataset.operationId)`);
  await click(id);
  const op=await until(()=>evaluate(`Array.from(document.querySelectorAll('[data-operation-id]')).find(n=>!${JSON.stringify(before)}.includes(n.dataset.operationId)&&n.textContent.includes(${JSON.stringify(kind)}))?.dataset.operationId`),'operation was not tracked: '+kind);
  await until(()=>evaluate(`document.querySelector('[data-operation-id="${op}"]')?.textContent.includes('(${state})')`),'operation not '+state+': '+kind,15000);
  return op;
}
async function saveBackend(backend) {
  await uiReady();await set('decisionBackendSelect',backend,'change');
  if(backend==='laya'){await set('decision-field-laya.enabled',true);await set('decision-field-laya.allow_live_http',true);}
  await click('decisionSave');
  await until(async()=>!(await content('decisionConfigNote')).includes('未保存'),'config did not save');await uiReady();
}
async function screenshot(name,width=1440,height=1050,target=null) {
  await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
  await evaluate(`(()=>{document.getElementById('decisionSecret').value='';const target=${JSON.stringify(target)};if(target){document.querySelector(target).scrollIntoView({block:'start'});window.scrollBy(0,-100);}else window.scrollTo(0,0);const b=document.createElement('div');b.id='b09FixtureBanner';b.textContent='TEST FIXTURE · 独立 B08 · Laya 服务替身 / 隔离 Actor · 非机器人验收';b.style='position:fixed;bottom:0;left:0;right:0;z-index:99999;padding:8px;background:#133d36;color:white;text-align:center;font:12px sans-serif';document.body.append(b);return true;})()`);
  await delay(100);
  const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
  await fs.writeFile(path.join(output,name+'.png'),Buffer.from(shot.data,'base64'));evidence.screenshots.push(name+'.png');await evaluate(`document.getElementById('b09FixtureBanner').remove();true`);
}
async function installHTTPFixture() {
  await evaluate(`(()=>{
    window.b09Fixture={rules:{},hold:{},waiting:{},releases:{}};
    const real=managementFetch;
    managementFetch=async function(url,options={}){
      const response=await real(url,options);const key=new URL(url,location.href).pathname.replace('/api/v1/ex/decision','');
      if((options.method||'GET')!=='GET')return response;
      const fixture=window.b09Fixture;
      if(fixture.hold[key]){delete fixture.hold[key];fixture.waiting[key]=true;await new Promise(r=>fixture.releases[key]=r);}
      const rule=fixture.rules[key] || (key.startsWith('/operations/')?fixture.rules.operations:null);
      if(!rule)return response;
      const raw=await response.json();const value={...raw,...rule.patch};
      return new Response(JSON.stringify(value),{status:rule.status||response.status,headers:{'Content-Type':'application/json'}});
    };return true;
  })()`);
}

try {
  await until(()=>{if(fixtureFailure)throw new Error(fixtureFailure);return ready;},'fixture startup',20000);
  args.base=ready.base;token=(await fs.readFile(ready.token_file,'utf8')).trim();
  evidence.base_origin=args.base;
  profile=await fs.mkdtemp(path.join(os.tmpdir(),'b09-owned-chrome-'));
  chromeLog=await fs.open(path.join(output,'chrome.txt'),'w');
  chrome=spawn(args.chrome||'/usr/bin/google-chrome',['--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--disable-background-networking','--no-first-run','--no-default-browser-check','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:['ignore',chromeLog.fd,chromeLog.fd]});
  const info=await until(async()=>{try{return (await fs.readFile(path.join(profile,'DevToolsActivePort'),'utf8')).trim().split('\n');}catch{return null;}},'CDP not ready');
  const targets=await (await fetch('http://127.0.0.1:'+info[0]+'/json/list')).json();
  socket=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);
  socket.addEventListener('message',event=>listen(JSON.parse(event.data)));
  await new Promise((r,j)=>{socket.addEventListener('open',r,{once:true});socket.addEventListener('error',j,{once:true});});
  await call('Page.enable');await call('Runtime.enable');await call('Network.enable');await call('Debugger.enable');
  await call('Page.navigate',{url:args.base+'/#/decision'});
  await until(()=>evaluate(`document.readyState==='complete' && Boolean(window.DecisionPage)`),'page not loaded');

  await check('direct URL, empty state, missing/wrong credentials stop reads; zero writes',async()=>{
    assert(await evaluate(`document.getElementById('page-decision').classList.contains('active')`),'route not active');
    assert(await evaluate(`document.getElementById('decisionStop').disabled`),'unauthenticated stop enabled');
    await enterCredential('wrong-B09-fixture-credential');
    await until(()=>rows().some(r=>r.status===401),'wrong credential no 401');
    await delay(1300);const n=rows().filter(r=>r.path.includes('/decision/')).length;await delay(1200);
    assert(rows().filter(r=>r.path.includes('/decision/')).length===n,'reads continue after invalid credential');
    assert(writes().length===0,'automatic writes');
  });
  await enterCredential(token);await uiReady();
  await check('navigation, Back, refresh, SSE reconnect use authenticated reads only',async()=>{
    assert((await content('decisionQuality')).includes('0/8'),'model quality limitation missing');
    await evaluate(`document.querySelector('[data-page="core"]').click();true`);
    await until(()=>evaluate(`state.activePage==='core'`),'core route');
    await evaluate('history.back();true');await until(()=>evaluate(`state.activePage==='decision'`),'Back decision route');await uiReady();
    const count=rows().filter(r=>r.path==='/api/events').length;
    await evaluate('connectEvents();true');await until(()=>rows().filter(r=>r.path==='/api/events').length>count,'SSE reconnect missing');
    await click('decisionRefresh');await delay(1100);assert(writes().length===0,'read/reconnect wrote');
  });
  await check('Mock/Jev/Laya configuration, read-only identity, save/effective, probe scope',async()=>{
    assert(await evaluate(`Array.from(document.getElementById('decisionBackendSelect').options,o=>o.value).join(',')==='mock,jev,laya'`),'backend catalog mismatch');
    await saveBackend('jev');assert(await evaluate(`document.querySelector('#decisionApplyMode [value="execute"]').disabled`),'Jev execute enabled');
    assert((await content('decisionJevIdentity')).includes('https://'),'Jev fixed address missing');
    await saveBackend('laya');assert(await evaluate(`document.querySelector('#decisionApplyMode [value="execute"]').disabled`),'ordinary Laya execute enabled');
    assert((await content('decisionLayaIdentity')).includes('revision'),'Laya identity missing');
    const cfg=await readJSON('/config');assert(cfg.revision!==cfg.effective_revision,'save applied config');
    assert((await readJSON('/status')).service.state==='stopped','save started Laya');
    const op=await opClick('decisionTest','test');
    assert((await evaluate(`document.querySelector('[data-operation-id="${op}"]').textContent`)).includes('health'),'probe scope missing');
    assert((await content('decisionProbeScope')).includes('不证明模型推理'),'probe falsely implies inference');
  });
  await check('draft survives reads; real CAS 409 retains edits; explicit rebase saves',async()=>{
    await set('decision-field-laya.deadline_ms',1200);await delay(1200);
    assert(await evaluate(`document.getElementById('decision-field-laya.deadline_ms').value==='1200'`),'poll overwrote draft');
    // Pause reads to put the actual stale revision into the real B08 CAS check.
    await evaluate('DecisionPage.leave();true');const changed=await fixtureCall('change_config');await click('decisionSave');
    await until(async()=>(await content('decisionMessage')).includes('409'),'CAS conflict not visible');
    assert(await evaluate(`document.getElementById('decision-field-laya.deadline_ms').value==='1200'`),'409 lost draft');
    await evaluate('DecisionPage.enter();true');await until(async()=>(await content('decisionConfigVersions')).includes('saved '+changed.revision+' /'),'new conflict version not read');await uiReady();
    await click('decisionReviewConfig');await click('decisionSave');await until(async()=>!(await content('decisionConfigNote')).includes('未保存'),'rebase save failed');
  });
  await check('secret keep/set/clear, input clearing; no automatic mode/service change',async()=>{
    for(const action of ['set','keep','clear']){
      await uiReady();await set('decisionSecretAction',action,'change');
      if(action==='set')await set('decisionSecret','B09-fixture-provider-secret');
      await click('decisionSecretSave');await until(async()=>(await content('decisionMessage')).includes('密钥操作已完成'),'secret '+action);
      await until(async()=>(await readJSON('/config')).secrets.jev.configured===(action!=='clear'),'secret state');
      assert(await evaluate(`document.getElementById('decisionSecret').value===''`),'secret input remained');
      await delay(1050);
    }
  });
  await check('202 loading, duplicate click, independent stop supersedes loading without waiting',async()=>{
    await uiReady();await fixtureCall('hold');const before=writes().filter(r=>r.path.endsWith('/service/start')).length;
    await click('decisionServiceStart');await click('decisionServiceStart');
    await until(async()=>(await content('decisionOperations')).includes('(running)'),'loading op not running');
    assert(writes().filter(r=>r.path.endsWith('/service/start')).length===before+1,'duplicate service start');
    await set('decision-field-laya.deadline_ms',1100);
    assert(!await evaluate(`document.getElementById('decisionStop').disabled`),'draft/loading blocked stop');
    await click('decisionStop');
    await until(async()=>(await readJSON('/status')).decision.mode==='disabled','stop did not close gate');
    await fixtureCall('release');
    await until(async()=>(await content('decisionOperations')).includes('(superseded)'),'loading not superseded');
    await until(async()=>(await content('decisionStatusFacts')).includes('(proven)'),'stop proof not shown');
    await click('decisionReloadConfig');await uiReady();
  });
  await check('owned service start, restart_required, explicit recover and stop; mode separate',async()=>{
    await opClick('decisionServiceStart','service_start');
    await fixtureCall('quarantine');await until(async()=>(await content('decisionServiceState')).includes('restart_required'),'quarantine not visible');
    await opClick('decisionServiceRecover','service_recover');
    assert((await readJSON('/status')).decision.mode==='disabled','recover restored old mode');
    assert((await readJSON('/snapshot')).current_goal===null,'recover replayed Goal');
    await opClick('decisionServiceStop','service_stop');
    await saveBackend('mock');await set('decisionApplyMode','shadow','change');await opClick('decisionApply','mode');
    await until(async()=>(await content('decisionModeState')).includes('shadow'),'explicit mode missing');
    await opClick('decisionStop','stop');
  });
  await check('real isolated Actor Ledger pagination and request links; success is not robot success',async()=>{
    const seeded=await fixtureCall('seed_actions',{count:21});evidence.actor_commands=seeded.command_ids.length;
    await until(async()=>(await content('decisionActions')).includes('隔离测试 Actor'),'Actor display missing');
    await uiReady();await click('decisionActionsNext');await until(async()=>(await content('decisionActionsMeta')).includes('第 2 页'),'actions paging');
    await click('decisionActionsPrev');await until(async()=>(await content('decisionActionsMeta')).includes('第 1 页'),'actions previous');
    assert((await content('decisionActions')).includes('不代表整个 Goal'),'success conflation');
    assert((await content('decisionActions')).includes('Actor 已接收'),'accepted stage missing');
  });
  await check('actual stop request, blocked, late proof preserves failed action and blocked gate',async()=>{
    await fixtureCall('active_actor',{stop_proof:false});await click('decisionStop');
    await until(async()=>(await content('decisionOperations')).includes('(blocked)'),'stop blocked not shown');
    assert((await content('decisionBlocked'))==='是','blocked not separately visible');
    const failed=await fixtureCall('prove_failed');
    await evaluate(`(()=>{const n=Array.from(document.querySelectorAll('#decisionActions button')).find(n=>false);return true;})()`);
    // Existing request association provides the actual command filter; fixture GET only selects a real Ledger row.
    await installHTTPFixture();
    const row=await readJSON('/actions?command_id='+failed.command_id);
    await evaluate(`b09Fixture.rules['/actions']={patch:${JSON.stringify(row)}};true`);
    await until(async()=>(await content('decisionActions')).includes('失败 + 停止已证明'),'failure/proof distinction missing');
    assert((await content('decisionBlocked'))==='是','proof cleared blocked in UI');
    await screenshot('01-stopping-proof');
    await evaluate(`delete b09Fixture.rules['/actions'];true`);
  });
  await check('history paging/detail, truncated records, unknown socket write, Jev body not collected, safe text',async()=>{
    await fixtureCall('seed_history');
    // Use public cursors, no direct private UI state. Advance to end until Jev fixture is visible.
    for(let i=0;i<8;i++){
      await uiReady();if((await content('decisionHistory')).includes('TEST-FIXTURE-snapshot-21'))break;
      await click('decisionHistoryNext');await delay(1050);
    }
    assert((await content('decisionHistory')).includes('TEST-FIXTURE-snapshot-21'),'fixture history not reached');
    await evaluate(`Array.from(document.querySelectorAll('#decisionHistory article')).find(n=>n.textContent.includes('TEST-FIXTURE-snapshot-20')).querySelector('button').click();true`);
    await until(async()=>(await content('decisionRequestDetail')).includes('记录已截断'),'record truncation missing');
    assert((await content('decisionRequestDetail')).includes('backend_result_display'),'display projection missing');
    await evaluate(`Array.from(document.querySelectorAll('#decisionHistory article')).find(n=>n.textContent.includes('TEST-FIXTURE-snapshot-21')).querySelector('button').click();true`);
    await until(async()=>(await content('decisionRequestDetail')).includes('Jev 正文未采集'),'Jev body reconstructed');
    assert((await content('decisionRequestDetail')).includes('socket 写入：未知'),'null socket not unknown');
    assert((await content('decisionRequestDetail')).includes('discarded'),'EX discard absent');
    assert(await evaluate(`!window.b09Injected && !document.querySelector('#page-decision img')`),'unsafe text rendered as markup');
    await screenshot('04-request-evidence',1440,1050,'#decisionRequestDetail');
  });
  await check('operation 404/failed/unknown and empty extension projections use truthful states',async()=>{
    // Only response display boundaries are injected; all writes still use real B08.
    const states=['failed','blocked','superseded','future_fixture_state'];
    for(const state of states){
      await evaluate(`b09Fixture.rules.operations={patch:{operation:{operation_id:'TEST-FIXTURE',kind:'test',state:${JSON.stringify(state)},error_code:'<img src=x onerror="window.b09Injected=true">'}}};true`);
      await click('decisionTest');await until(async()=>(await content('decisionOperations')).includes('('+state+')'),'state '+state);
      await evaluate(`delete b09Fixture.rules.operations;true`);
      if(state==='future_fixture_state'){
        await evaluate(`b09Fixture.rules.operations={status:404,patch:{ok:false,code:'operation_not_found'}};true`);
        await until(async()=>(await content('decisionOperations')).includes('记录已不可查询，原结果未知'),'operation 404');
        assert((await content('decisionMessage')).includes('原结果未知'),'404 still presented as pending');
        await evaluate(`delete b09Fixture.rules.operations;true`);
      }
    }
    assert((await content('page-decision')).includes('EX 任务协调已接线；A.E.B. 任务与步骤的页面投影尚未接入'),'AEB placeholder absent');
    assert((await content('page-decision')).includes('公开回复'),'delivery placeholder absent');
    assert((await content('page-decision')).includes('物理判定：未接入'),'control placeholder absent');
    assert(await evaluate('!window.b09Injected'),'error text XSS');
  });
  await check('active/pending Goal parameters, unknown phase, malicious text and empty projections',async()=>{
    const goal={schema_version:1,request_id:'TEST-FIXTURE-goal',task_id:'TEST-FIXTURE-task',step_id:'step-1',goal_id:'fixture-active',goal_text_en:'<img src=x onerror="window.b09Injected=true">',parameters:{'arm.move.v1':{meters:1}}};
    await evaluate(`b09Fixture.rules['/snapshot']={patch:{current_goal:${JSON.stringify(goal)},pending_goal:{...${JSON.stringify(goal)},goal_id:'fixture-pending'},goal_phase:'future_fixture_phase'}};true`);
    await until(async()=>(await content('decisionGoals')).includes('fixture-pending'),'pending Goal not rendered');
    const shown=await content('decisionGoals');assert(shown.includes('fixture-active')&&shown.includes('未知状态 (future_fixture_phase)')&&shown.includes('meters'),'Goal mapping wrong');
    assert(await evaluate(`!window.b09Injected && !document.querySelector('#decisionGoals img')`),'Goal XSS');
    await screenshot('06-goal-fixture',1440,1050,'#decisionGoals');
    await evaluate(`b09Fixture.rules['/snapshot']={patch:{current_goal:null,pending_goal:null,last_submitted:null,last_result:null,last_built:null}};true`);
    await until(async()=>!(await content('decisionGoals')).includes('fixture-active'),'empty Goal not rendered');
    assert((await content('decisionLatest')).includes('暂无请求'),'empty decision missing');
    await evaluate(`delete b09Fixture.rules['/snapshot'];true`);
  });
  await check('SSE coalescing, hidden pause, navigation cleanup, stale state and read-only recovery',async()=>{
    await fixtureCall('notify',{count:30});const start=Date.now();await delay(3300);
    const starts=rows().filter(r=>r.path==='/api/v1/ex/decision/status' && r.at>=start).map(r=>r.at);
    assert(starts.length<=4,'SSE caused polling burst');
    const reads=rows().filter(r=>r.method==='GET'&&r.path.includes('/decision/')&&r.at>=start);const previous=new Map();
    for(const row of reads){const prior=previous.get(row.key);if(prior)assert(prior.ended_at!=null&&prior.ended_at<=row.at,'overlapping GET: '+row.key);previous.set(row.key,row);}
    evidence.refresh_window={duration_ms:3300,status_reads:starts.length,non_overlapping_gets:reads.length};
    for(let i=1;i<starts.length;i++)assert(starts[i]-starts[i-1]>=900,'poll interval under 1 second');
    await evaluate(`Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));true`);
    await delay(300);const before=rows().filter(r=>r.path.includes('/decision/')).length;await delay(1300);
    assert(rows().filter(r=>r.path.includes('/decision/')).length===before,'hidden page kept reading');
    await evaluate(`delete document.hidden;document.dispatchEvent(new Event('visibilitychange'));true`);await uiReady();
    const lastGoal=await content('decisionGoals');const offlineWrites=writes().length;
    await call('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:-1,uploadThroughput:-1});
    await until(async()=>(await content('decisionFreshness')).includes('已过期'),'real disconnect not stale');
    assert((await content('decisionGoals'))===lastGoal,'disconnect discarded cached Goal');
    await call('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});
    await until(async()=>!(await content('decisionFreshness')).includes('已过期'),'reconnect did not refresh');
    assert(writes().length===offlineWrites,'network recovery wrote');
    await evaluate(`document.querySelector('[data-page="core"]').click();true`);await delay(300);
    const left=rows().filter(r=>r.path.includes('/decision/')).length;await delay(1200);
    assert(rows().filter(r=>r.path.includes('/decision/')).length===left,'left page kept polling');
    const writesBefore=writes().length;
    await evaluate(`document.querySelector('[data-page="decision"]').click();connectEvents();true`);await uiReady();await delay(1100);
    assert(writes().length===writesBefore,'reentry/reconnect wrote');
  });
  await check('late credential response ignored; draft preserved and secret cleared pending review',async()=>{
    await set('decision-field-mock.kind','cancel');await set('decisionSecretAction','set','change');await set('decisionSecret','B09-fixture-provider-secret');
    await evaluate(`b09Fixture.hold['/config']=true;DecisionPage.refresh();true`);
    await until(()=>evaluate(`Boolean(b09Fixture.waiting['/config'])`),'config not held');
    await enterCredential('invalid-replacement-fixture');await until(()=>evaluate('!managementCredential'),'credential not revoked');
    await evaluate(`b09Fixture.releases['/config']();true`);await delay(100);
    assert((await content('decisionConfigVersions')).includes('saved 未知'),'old config painted');
    assert(await evaluate(`document.getElementById('decisionSecret').value===''`),'credential change retained secret');
    await enterCredential(token);await uiReady();
    assert((await content('decisionConfigNote')).includes('需核对'),'draft automatically reauthorized');
    assert(await evaluate(`document.getElementById('decisionSave').disabled`),'save allowed before review');
    await click('decisionReviewConfig');
  });
  await check('real server session restart clears old operations/version and ignores held history',async()=>{
    const old=(await readJSON('/status')).ex_session;
    await evaluate(`b09Fixture.hold['/decisions']=true;DecisionPage.refresh();true`);
    await until(()=>evaluate(`Boolean(b09Fixture.waiting['/decisions'])`),'history not held');
    await fixtureCall('restart');
    // The next status read must be allowed while old history is held: leave/enter aborts old reads.
    await evaluate(`DecisionPage.leave();b09Fixture.releases['/decisions']();DecisionPage.enter();true`);
    await until(async()=>!(await content('decisionStatusFacts')).includes(old),'old session retained',12000);await uiReady();
    assert(!(await content('decisionOperations')).includes('TEST-FIXTURE'),'old operations retained');
    assert((await content('decisionConfigNote')).includes('需核对'),'session change reauthorized draft');
    assert(!(await content('decisionHistory')).includes('TEST-FIXTURE-snapshot'),'old history painted');
    await click('decisionReviewConfig');await click('decisionReloadConfig');await uiReady();
  });
  await check('existing environment page keeps draft; reload clears credential, no writes or storage leaks',async()=>{
    await evaluate(`document.querySelector('[data-page="environments"]').click();true`);await evaluate('refreshEnvironment()');
    await set('ros2DiscoveryInterval','2.37');await evaluate('refreshEnvironment()');
    assert(await evaluate(`document.getElementById('ros2DiscoveryInterval').value==='2.37'`),'environment draft regression');
    await evaluate(`document.querySelector('[data-page="decision"]').click();true`);await uiReady();
    await until(async()=>!(await content('decisionFreshness')).includes('已过期'),'fresh state before screenshots');
    await screenshot('02-decision-desktop');await screenshot('05-config-goal',1440,1050,'#decisionConfigVersions');await screenshot('03-decision-narrow',820,1180,'#decisionConfigVersions');
    assert(await evaluate(`document.documentElement.scrollWidth<=innerWidth`),'responsive horizontal overflow');
    const stored=await evaluate(`JSON.stringify({local:Object.values(localStorage),session:Object.values(sessionStorage),html:document.documentElement.outerHTML})`);
    assert(!stored.includes(token)&&!stored.includes('B09-fixture-provider-secret'),'credential leak');
    assert(!rows().some(r=>r.credential_in_url),'token URL');
    const n=writes().length;await call('Page.reload',{ignoreCache:true});
    await until(()=>evaluate(`document.readyState==='complete' && Boolean(window.DecisionPage) && !managementCredential`),'reload credential retained');
    await delay(1200);assert(writes().length===n,'reload caused write');
  });
  assert(evidence.errors.length===0,'browser runtime errors');evidence.pass=true;
}catch(error){evidence.pass=false;evidence.errors.push(String(error.stack||error));try{await call('Debugger.pause');await delay(100);}catch{}}
finally{
  evidence.requests=rows();
  if(socket?.readyState===WebSocket.OPEN)socket.close();
  for(const p of pending.values()){clearTimeout(p.timer);p.reject(new Error('browser closed'));}pending.clear();
  if(chrome && chrome.exitCode===null){chrome.kill('SIGTERM');await Promise.race([new Promise(r=>chrome.once('exit',r)),delay(3000)]);if(chrome.exitCode===null)chrome.kill('SIGKILL');}
  fixture.stdin.end(JSON.stringify({id:++fixtureID,command:'quit'})+'\n');
  await Promise.race([new Promise(r=>fixture.once('exit',r)),delay(8000)]);if(fixture.exitCode===null)fixture.kill('SIGTERM');
  for(const p of fixtureWaiters.values())p.reject(new Error('fixture closed'));fixtureWaiters.clear();
  if(profile)await fs.rm(profile,{recursive:true,force:true});
  await chromeLog?.close();await serverLog.close();
  await fs.writeFile(path.join(output,'result.json'),JSON.stringify(scrub(evidence),null,2));
  console.log(JSON.stringify({pass:evidence.pass,checks:evidence.checks.length,errors:evidence.errors,output}));
  process.exitCode=evidence.pass?0:1;
}
