// Explicit browser acceptance using installed Chrome and Node's native WebSocket.
// No browser package installation, no credentials in URLs or report payloads.
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {spawn} from 'node:child_process';

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, source) => {
  if (value.startsWith('--')) pairs.push([value.slice(2), source[index + 1]]);
  return pairs;
}, []));
if (!args.base || !args['token-file'] || !args.output) throw new Error('--base --token-file --output required');
const output = path.resolve(args.output);
await fs.mkdir(output, {recursive: true, mode: 0o700});
const token = (await fs.readFile(args['token-file'], 'utf8')).trim();
const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'b08-owned-chrome-'));
const downloads = path.join(output, 'downloads');
await fs.mkdir(downloads, {recursive: true});
const chromeLog = await fs.open(path.join(output, 'chrome.txt'), 'w');
const chrome = spawn(args.chrome || '/usr/bin/google-chrome', ['--headless=new', '--no-sandbox',
  '--disable-gpu', '--disable-dev-shm-usage', '--disable-background-networking', '--no-first-run',
  '--no-default-browser-check', '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'],
  {stdio: ['ignore', chromeLog.fd, chromeLog.fd]});
let socket;
let sequence = 0;
const pending = new Map();
const requests = new Map();
const downloadEvents = [];
const evidence = {browser: 'installed Chrome with native CDP', process_id: chrome.pid,
  base_origin: new URL(args.base).origin, checks: [], requests: [], errors: []};

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
  const result = await call('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (result.exceptionDetails) throw new Error('browser evaluation failed: ' + result.exceptionDetails.text);
  return result.result?.value;
}
function listen(event) {
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
      method: value.request.method, is_api: api, has_authorization: Boolean(authorization),
      uses_expected_authorization: authorization === 'Bearer ' + token,
      credential_in_url: value.request.url.includes(token), status: null});
  }
  if (event.method === 'Network.responseReceived') {
    const row = requests.get(event.params.requestId);
    if (row) row.status = event.params.response.status;
  }
  if (event.method.startsWith('Browser.download')) downloadEvents.push({method: event.method, params: event.params});
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

try {
  const portFile = path.join(profile, 'DevToolsActivePort');
  const info = await until(async () => {
    if (chrome.exitCode !== null) throw new Error('owned browser exited before CDP ready');
    try {return (await fs.readFile(portFile, 'utf8')).trim().split('\n');} catch {return null;}
  }, 'owned browser CDP not ready');
  const targets = await (await fetch('http://127.0.0.1:' + info[0] + '/json/list')).json();
  socket = new WebSocket(targets.find(item=>item.type === 'page').webSocketDebuggerUrl);
  socket.addEventListener('message', event => listen(JSON.parse(event.data)));
  await new Promise((resolve,reject) => {socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
  await call('Page.enable');
  await call('Runtime.enable');
  await call('Network.enable');
  await call('Browser.setDownloadBehavior', {behavior:'allow',downloadPath:downloads,eventsEnabled:true});
  await call('Page.navigate', {url:args.base});
  await until(()=>evaluate(`document.readyState==='complete' && Boolean(document.getElementById('managementCredentialApply'))`), 'credential controls missing');
  assert(writes().length === 0, 'page load caused a write request');
  evidence.checks.push('page load and missing credential caused no execution write');

  const beforeWrong = rows().length;
  await enterCredential('wrong-browser-test-credential');
  await until(()=>rows().slice(beforeWrong).some(row=>row.status === 401), 'wrong credential did not yield 401');
  assert(await evaluate(`document.getElementById('managementCredentialStatus').textContent.length>0`), 'credential error status missing');
  assert(writes().length === 0, 'credential entry caused a write');
  evidence.checks.push('wrong credential shows 401 without execution');

  const beforeValid = rows().length;
  await enterCredential(token);
  await until(()=>rows().some(row=>row.path === '/api/status' && row.status === 200 && row.uses_expected_authorization), 'authenticated dashboard status not read');
  const status = await browserStatus();
  assert(status.status === 200, 'authenticated management status unavailable');
  assert(await evaluate(`document.getElementById('managementCredential').value===''`), 'credential input was not cleared');
  assert(writes().length === 0, 'credential apply triggered execution');
  evidence.checks.push('credential remains in page memory and authenticated reads succeed');

  await until(()=>rows().some(row=>row.path === '/api/events' && row.status === 200 && row.uses_expected_authorization), 'fetch SSE is not authenticated');
  const eventCount = rows().filter(row=>row.path === '/api/events').length;
  await evaluate('connectEvents();true');
  await until(()=>rows().filter(row=>row.path === '/api/events').length > eventCount, 'SSE reconnect did not make a fresh request');
  assert(rows().slice(beforeValid).filter(row=>row.path === '/api/events').every(row=>row.uses_expected_authorization), 'SSE credential was omitted');
  evidence.checks.push('SSE reconnect uses Authorization and no token URL');

  await evaluate(`location.hash='#environments';true`);
  await evaluate(`refreshEnvironment()`);
  await evaluate(`(() => {const input=document.getElementById('ros2DiscoveryInterval');input.value='2.37';
    input.dispatchEvent(new Event('input',{bubbles:true}));return true;})()`);
  assert(await evaluate('envView.dirty === true'), 'environment draft was not marked dirty');
  await evaluate(`refreshEnvironment()`);
  assert(await evaluate(`document.getElementById('ros2DiscoveryInterval').value==='2.37'`), 'authenticated refresh overwrote the draft');
  evidence.checks.push('authenticated refresh preserves existing environment draft');

  await evaluate('createArchive()');
  await until(()=>downloadEvents.some(event=>event.method==='Browser.downloadProgress' && event.params.state==='completed'), 'authenticated backup blob download did not complete');
  const files = await fs.readdir(downloads);
  assert(files.length > 0, 'backup download file missing');
  const downloaded = path.join(downloads, files[0]);
  const archive = await fs.readFile(downloaded);
  // Upload the just-exported empty test instance through the existing page code.
  await evaluate(`(async()=>{const bytes=Uint8Array.from(atob(${JSON.stringify(archive.toString('base64'))}),c=>c.charCodeAt(0));
    await uploadArchive(new File([bytes],'browser-test-snapshot.zip',{type:'application/zip'}));return true;})()`);
  assert(rows().filter(row=>row.path.startsWith('/api/v1/ex/backups')).every(row=>row.uses_expected_authorization), 'backup JSON/upload/download omitted credential');
  evidence.checks.push('existing ZIP download and upload use the shared authentication');

  const forbidden = row => row.is_api && row.method !== 'GET' &&
    (/\/runtime\/(start|stop|control-mode)$/.test(row.path) || /\/decision\/(mode|stop|service\/)/.test(row.path) || /\/environments\/select$/.test(row.path));
  assert(!rows().some(forbidden), 'page flow triggered a runtime, model or mode operation');
  const storage = await evaluate(`({local:Object.values(localStorage),session:Object.values(sessionStorage)})`);
  assert(!JSON.stringify(storage).includes(token), 'credential persisted in browser storage');
  assert(!rows().some(row=>row.credential_in_url), 'credential appeared in request URL');
  const beforeReloadWrites = writes().length;
  await call('Page.reload', {ignoreCache:true});
  await until(()=>evaluate(`document.readyState==='complete' && Boolean(document.getElementById('managementCredential'))`), 'page reload did not complete');
  assert(await evaluate(`document.getElementById('managementCredential').value===''`), 'reload restored credential input');
  const afterReload = await evaluate(`(async()=>{try{const response=await managementFetch('/api/v1/ex/decision/status');
    return response.status;}catch{return 401;}})()`);
  assert(afterReload === 401, 'page reload reused a management credential');
  assert(writes().length === beforeReloadWrites, 'page reload caused a write');
  evidence.checks.push('reload clears credential and causes zero execution');
  evidence.pass = true;
} catch (error) {
  evidence.pass = false;
  evidence.errors.push(String(error.stack || error));
  process.exitCode = 1;
} finally {
  evidence.requests = rows();
  if (socket) socket.close();
  if (chrome.exitCode === null) {
    chrome.kill('SIGTERM');
    await Promise.race([new Promise(resolve=>chrome.once('exit',resolve)),delay(5000)]);
    if (chrome.exitCode === null) chrome.kill('SIGKILL');
    await Promise.race([new Promise(resolve=>chrome.once('exit',resolve)),delay(3000)]);
  }
  evidence.browser_exit_code = chrome.exitCode;
  evidence.browser_exit_signal = chrome.signalCode;
  await chromeLog.close();
  await fs.writeFile(path.join(output,'result.json'),JSON.stringify(scrub(evidence),null,2)+'\n');
  console.log(JSON.stringify({pass:evidence.pass,checks:evidence.checks,errors:scrub(evidence.errors)}));
}
