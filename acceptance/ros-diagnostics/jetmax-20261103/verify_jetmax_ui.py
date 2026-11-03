"""Browser verification against the real diagnostic EX instance through SSH."""
import argparse,json,time
from pathlib import Path
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError,URLError
from playwright.sync_api import sync_playwright,expect

p=argparse.ArgumentParser()
p.add_argument('--url',default='http://127.0.0.1:18765')
p.add_argument('--token-file',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
args=p.parse_args()
args.output.mkdir(parents=True,exist_ok=True)
token=args.token_file.read_text().strip()
origin=args.url.rstrip('/')
opener=build_opener(ProxyHandler({}))
checks=[];errors=[];requests=[]
record={'result':'FAIL','scope':'REAL_JETMAX_EX_OVER_SSH','checks':checks,'page_errors':errors,'requests':requests}
def check(name,value=True):
    if not value:raise AssertionError(name)
    checks.append(name)
def api(path,auth=True):
    headers={'Authorization':'Bearer '+token} if auth else {}
    try:r=opener.open(Request(origin+'/api/v1/ex/'+path,headers=headers),timeout=8)
    except HTTPError as exc:r=exc
    with r:
        body=r.read()
        assert token.encode() not in body
        try:data=json.loads(body)
        except ValueError:data=None
        return r.status,data
try:
    print('Waiting for the existing camera/YOLO entry scenario.',flush=True)
    deadline=time.monotonic()+600
    while time.monotonic()<deadline:
        try:status,data=api('plugins')
        except (URLError,TimeoutError):
            time.sleep(.5);continue
        if status==200 and any(row['id']=='yolo' and row['enabled'] for row in data['plugins']):break
        time.sleep(.5)
    else:raise TimeoutError('real entry scenario did not become active')
    for path in ['status','events','environments/diagnostics']:
        check('unauthenticated '+path+' rejected',api(path,auth=False)[0]==401)
    check('authenticated diagnostic cache accessible',api('environments/diagnostics')[0]==200)
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1080})
        page.on('pageerror',lambda exc:errors.append(str(exc)))
        page.on('request',lambda req:requests.append({'path':req.url.split(origin)[-1],'method':req.method}))
        page.goto(origin+'/#/environments',wait_until='domcontentloaded')
        page.locator('#managementCredential').fill(token)
        page.locator('#managementCredentialApply').click()
        page.wait_for_function('state.eventSource && state.eventSource.readyState===1',timeout=20000)
        expect(page.locator('#managementCredential')).to_have_value('')
        expect(page.locator('#environmentDiagnostics')).to_be_visible()
        check('real Dashboard login and authenticated SSE connected')
        # Only EX callback counters count as proof of transport.
        deadline=time.monotonic()+75
        last={}
        while time.monotonic()<deadline:
            _,endpoints=api('environments/ros2/endpoints')
            rows=endpoints.get('received',[])+endpoints.get('ports',[])
            last=next((r for r in rows if r.get('plugin_id')=='yolo' and r.get('port_id')=='image'),{})
            if last.get('rx_received',0)>=10 and last.get('rx_consumed',0)>=10:break
            page.wait_for_timeout(400)
        check('at least ten real camera callbacks and consumed frames',last.get('rx_received',0)>=10 and last.get('rx_consumed',0)>=10)
        record['image_endpoint']=last
        page.locator('#environmentDiagnosticsRefreshButton').click()
        page.wait_for_timeout(600)
        page.screenshot(path=str(args.output/'real-environment-receiving.png'),full_page=True)
        check('environment diagnostics visible with real camera input')
        page.evaluate("location.hash='#/logs'")
        expect(page.locator('#logConsole')).to_be_visible()
        expect(page.locator('#logConsole').get_by_text('YOLO inference ready',exact=False).last).to_be_visible(timeout=30000)
        check('real YOLO inference-ready event appears through SSE')
        check('black terminal',page.locator('#logConsole').evaluate('(e)=>getComputedStyle(e).backgroundColor')=='rgb(17, 17, 17)')
        check('credential absent from rendered text',token not in page.locator('body').inner_text())
        page.screenshot(path=str(args.output/'real-yolo-logs.png'),full_page=True)
        page.evaluate('connectEvents()')
        page.wait_for_function('state.eventSource && state.eventSource.readyState===1')
        check('SSE reconnects against real EX')
        page.wait_for_timeout(500)
        check('browser issued no environment-select or runtime-start request',not any(r['method']=='POST' and (r['path'].endswith('/environments/select') or '/runtime/' in r['path']) for r in requests))
        check('no JavaScript errors',not errors)
        browser.close()
    record['result']='PASS'
except Exception as exc:
    record['error']=type(exc).__name__+': '+str(exc).replace(token,'[REDACTED]')
finally:
    record['passed']=len(checks)
    raw=json.dumps(record,ensure_ascii=False,indent=2)
    assert token not in raw
    (args.output/'results.json').write_text(raw,encoding='utf-8')
    print(json.dumps({'result':record['result'],'passed':len(checks),'error':record.get('error')}),flush=True)
raise SystemExit(0 if record['result']=='PASS' else 1)
