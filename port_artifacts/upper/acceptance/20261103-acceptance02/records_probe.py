"""Reproducible real-Host records smoke against the disposable Host only.

The seeded row is explicitly an unsent UI fixture: no model, socket delivery,
receipt or execution is invented. Actual injected rows are checked separately
by the production store/API mapper in test_upper_acceptance.
"""
import json
import subprocess
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'real-host-records'
OUT.mkdir(exist_ok=False)
base=(ROOT/'host-url.txt').read_text(encoding='utf-8').strip()
seed='''
import json
from astrbot_plugin_astrbotex_interaction.task_store import TaskStore
from astrbot_plugin_astrbotex_interaction.upper_store import UpperStore
store=TaskStore('/AstrBot/data/acceptance.sqlite3',recover=False)
upper=UpperStore(store,recover=False)
before={t:store.db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in
        ('upper_inputs','upper_batches','upper_public_replies')}
inbound,_=upper.claim_input(input_key='ui-record-fixture-acceptance02',robot_id='ui-fixture-robot',user_id='offline-ui',session_id='ui-fixture')
plan=upper.save_result(inbound['input_key'],{
    'route':'action_only','probabilities':{'speech_only':.05,'action_only':.90,'speech_and_action':.05},
    'public_text':None,'segments':['UI_FIXTURE_UNSENT_0','UI_FIXTURE_UNSENT_1','UI_FIXTURE_UNSENT_2'],'send_count':2},
    generation=inbound['generation'],settings_snapshot={},catalog_snapshot={},prompt_snapshot={},
    settings_hash='ui-settings-hash',catalog_hash='ui-catalog-hash',prompt_hash='ui-prompt-hash')
print(json.dumps({'before_counts':before,'plan':plan,'fixture_kind':'unsent; no external IO'},ensure_ascii=False))
store.close()
'''
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(storage_state=str(ROOT/'host-storage.json'),viewport={'width':1280,'height':900})
    context.add_init_script("if (window===window.top) localStorage.setItem('astrbot:first_notice_seen:v1','1')")
    page=context.new_page()
    errors=[]
    requests=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    def response(r):
        if '/upper/' in r.url:
            requests.append({'url':r.url.split('/upper/',1)[1],'status':r.status,'method':r.request.method,
                             'body':r.request.post_data})
    page.on('response',response)
    page.goto(base,wait_until='domcontentloaded')
    # Run the real plugin manager reload before querying freshly changed main.py.
    reload=page.evaluate('''async (name)=>{
      const r=await fetch('/api/plugin/reload',{method:'POST',headers:{'Content-Type':'application/json',
        Authorization:'Bearer '+localStorage.getItem('token')},body:JSON.stringify({name})});
      return {status:r.status,body:await r.json()};
    }''','astrbot_plugin_astrbotex_interaction')
    assert reload['status']==200 and reload['body']['status']=='ok',reload
    output=subprocess.check_output(['docker','exec','-e','PYTHONPATH=/aeb-src:/ex-src','-w','/tmp',
        'astrbot-upper-acceptance02','python','-B','-c',seed],text=True,encoding='utf-8')
    fixture=json.loads(output.strip().splitlines()[-1])
    (OUT/'fixture.json').write_text(json.dumps(fixture,indent=2,ensure_ascii=False),encoding='utf-8')
    page.goto(base+'/#/plugin-page/astrbot_plugin_astrbotex_interaction/upper-control',wait_until='domcontentloaded')
    frame=page.wait_for_selector('iframe.plugin-page-frame').content_frame()
    frame.wait_for_function("window.AstrBotPluginPage && /rev \\d+/.test(document.querySelector('#status-revision')?.textContent||'') && document.querySelector('#main')?.inert===false")
    frame.click('#tab-records')
    row=frame.locator('.record-row').filter(has_text=fixture['plan']['plan_id'])
    with page.expect_response(lambda r:'/upper/plans/get' in r.url) as pending:
        row.click()
    result=pending.value
    assert result.status==200
    body=result.json()
    plan=body['plan']
    assert plan['delivery_phase']=='unsent',plan
    assert plan['sent_prefix']==0 and plan['pending_suffix']==3,plan
    assert plan['prompt_version']=='ui-prompt-hash' and plan['catalog_version']=='ui-catalog-hash',plan
    assert plan['deliveries']==[],plan
    frame.wait_for_selector('#record-detail:not([hidden])')
    page.screenshot(path=str(OUT/'records.png'),full_page=True)
    (OUT/'result.json').write_text(json.dumps({'status':result.status,'response':body,'errors':errors,
        'requests':requests,'model_calls':0,'control_calls':0,'seed_only':True},indent=2,ensure_ascii=False),encoding='utf-8')
    assert not errors,errors
    context.close()
    browser.close()
print('Real Host records: unsent phase, zero sent prefix, 3 pending, versions and successful plans/get verified')
