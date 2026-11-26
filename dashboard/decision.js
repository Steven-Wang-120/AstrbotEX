/* Business state comes from /view; /task-selection is a separate read-only local trace. */
window.DecisionPage = (() => {
  const BASE = '/api/v1/ex/decision';
  const TERMINAL = new Set(['succeeded', 'failed', 'blocked', 'superseded', 'unavailable']);
  const CONNECTION = {unverified:'尚未验证', testing:'正在验证', verified:'连接已验证', failed:'连接失败', disconnected:'连接已断开'};
  const EX = {disabled:'已停用', starting:'启动中', running:'已启用', stopping:'停止中', failed:'启用失败', uncertain:'状态暂不可确认'};
  const TASK = {idle:'暂无任务', running:'执行中', active:'执行中', executing:'执行中', needs_planning:'等待重新规划',
    planning:'正在规划', waiting:'等待中', wait:'等待中', waiting_input:'等待输入', waiting_replan:'等待重新规划',
    resume_review:'结果不确定，等待复核', lease_lost:'执行授权失效，等待复核', canceling:'正在取消', stopping:'停止中', completed:'已完成', succeeded:'已完成',
    canceled:'已取消', cancelled:'已取消', failed:'任务失败', unavailable:'任务暂不可确认'};
  const ERRORS = {revision_conflict:'配置已变化，请核对后再保存。', session_conflict:'服务已重启，请核对当前配置。',
    backend_execute_not_allowed:'当前配置无法启用 EX，请检查连接配置。', mode_must_be_disabled:'请先停止 EX 再修改配置。',
    decision_must_be_disabled:'请先停止 EX 再修改配置。', requires_disabled:'请先停止 EX 再修改配置。',
    storage_write_uncertain:'保存结果不确定，请重新读取配置。', secret_cleanup_failed:'配置可能已保存，请重新读取后核对密钥。',
    unsupported_model:'该模型尚不支持。', invalid_deployment:'请填写真实的 Python、缓存路径与设备。',
    deployment_required:'请填写自启服务的部署信息。', owned_requires_loopback:'自启服务必须使用后台指定的本机地址。',
    invalid_service_connection:'请检查服务地址和连接配置。', invalid_base_url:'请检查服务地址。',
    invalid_auth_mode:'请检查服务认证方式。', http_401:'供应商认证失败，请检查密钥。', http_403:'供应商拒绝访问，请检查权限。',
    deadline_exceeded:'测试超时，草稿已保留。', service_not_ready:'服务尚未就绪。', owned_service_not_ready:'自启服务尚未就绪。',
    restart_required:'服务需要恢复，请先停止 EX。', route_not_found:'状态暂不可获取。'};
  const state = {initialized:false, active:false, generation:0, readEpoch:0, session:null, config:null, projection:null,
    stale:true, timer:null, flight:null, opTimer:null, opFlight:null, controllers:new Set(), operations:new Map(),
    busy:new Set(), modalProvider:null, draft:null, draftBase:null, draftRevision:null, draftVersion:0,
    conflict:false, review:false, testSequence:0, testMessage:'', focus:null, lastRead:0,
    taskSelection:null, taskTraceStale:true, retiredTaskScopes:new Set()};
  const el = id => document.getElementById(id);
  const text = (id, value) => {el(id).textContent = value;};
  const clone = value => structuredClone(value);
  const equal = (a,b) => JSON.stringify(a) === JSON.stringify(b);
  const authenticated = () => Boolean(managementCredential);
  const visible = () => state.active && !document.hidden && authenticated();
  const ticket = () => ({generation:state.generation, auth:managementCredentialEpoch, session:state.session});
  const current = t => t.generation === state.generation && t.auth === managementCredentialEpoch && t.session === state.session;
  const dirty = () => Boolean(state.draft && (!equal(state.draft,state.draftBase) || el('decisionSecret').value || el('decisionSecretClear').checked));
  const pending = kind => state.busy.has(kind) || [...state.operations.values()].some(o=>o.kind===kind && !TERMINAL.has(o.state));
  function message(value, kind='') {for(const id of ['decisionMessage','decisionModalMessage']){text(id,value);el(id).dataset.kind=kind;}}
  function errorMessage(error) {
    if(error.status===401)return '管理凭据已失效，请重新输入。';
    if(error.status===403)return '管理访问被拒绝，请检查本机访问与权限。';
    if(error.timeout || error.name==='AbortError')return '请求超时，结果未确认；请读取状态，不会自动重试。';
    if(ERRORS[error.code])return ERRORS[error.code];
    if(error.status===409)return '当前状态不允许此操作，请先停止 EX 或检查配置。';
    return '请求未完成，请检查连接后读取当前状态。';
  }
  async function request(path, body, read=false) {
    const captured=ticket(), epoch=state.readEpoch, controller=new AbortController();
    state.controllers.add(controller);
    let timeout=false;
    const timer=setTimeout(()=>{timeout=true;controller.abort();},10000);
    try {
      const response=await managementFetch(BASE+path,{method:body?'POST':'GET', signal:controller.signal,
        headers:{'Content-Type':'application/json'}, ...(body?{body:JSON.stringify(body)}:{})});
      const data=await response.json();
      if(!current(captured) || (read && epoch!==state.readEpoch))throw {obsolete:true};
      if(!response.ok || data.ok===false)throw {status:response.status,code:data.code};
      return {data,status:response.status};
    } catch(error) {
      if(!current(captured) || (read && epoch!==state.readEpoch))throw {obsolete:true};
      if(timeout)throw {timeout:true};throw error;
    }
    finally {clearTimeout(timer);state.controllers.delete(controller);}
  }
  function clearScope() {
    state.generation++;state.controllers.forEach(c=>c.abort());state.controllers.clear();
    clearTimeout(state.timer);clearTimeout(state.opTimer);state.timer=state.opTimer=null;
    state.flight=state.opFlight=null;state.operations.clear();state.busy.clear();
    state.session=null;state.config=state.projection=null;state.stale=true;
    state.taskSelection=null;state.taskTraceStale=true;state.retiredTaskScopes.clear();
    if(state.draft)state.review=true;
    el('decisionSecret').value='';el('decisionSecretClear').checked=false;invalidateTest();
  }
  function credentialChanged() {
    if(!state.initialized)return;
    clearScope();render();
    message(authenticated()?'正在读取当前配置。':'请输入有效管理凭据。');
    if(!authenticated()) {
      if(el('decisionModal').open)el('decisionModal').close();
      el('managementCredential').focus();
    } else if(state.modalProvider && !el('decisionModal').open)el('decisionModal').showModal();
    schedule(0);
  }
  function receiveConfig(data) {
    if(state.session && data.ex_session!==state.session)clearScope();
    state.session=data.ex_session;
    if(state.config && data.revision<state.config.revision)return;
    if(state.config && data.revision!==state.config.revision)invalidateTest();
    state.config=data;
    if(state.draft && data.revision!==state.draftRevision)state.conflict=true;
  }
  function schedule(delay=1000) {
    if(!visible() || state.timer)return;
    state.timer=setTimeout(()=>{state.timer=null;refresh();},Math.max(delay,1000-(performance.now()-state.lastRead)));
  }
  async function refresh() {
    if(!visible() || state.flight)return state.flight;
    const work=(async()=>{
      state.lastRead=performance.now();
      try {
        receiveConfig((await request('/config',null,true)).data);
        try {
          const value=(await request('/view',null,true)).data;
          if(value.schema_version!==1 || value.ex_session!==state.session || value.revision<state.config.revision)throw {code:'view_unavailable'};
          state.projection=value;state.stale=false;
        } catch(error) {
          if(error.obsolete)return;
          state.stale=true;state.projection=null;message(errorMessage(error),'warning');
        }
        try {
          const value=(await request('/task-selection',null,true)).data;
          if(value.ex_session!==state.session)throw {code:'task_trace_unavailable'};
          receiveTaskSelection(value.task_selection);
        } catch(error) {
          if(error.obsolete)return;
          state.taskTraceStale=true;
        }
      } catch(error) {if(!error.obsolete){state.stale=true;state.taskTraceStale=true;message(errorMessage(error),'error');}}
      finally {if(state.flight===work)state.flight=null;render();schedule();scheduleOperations();}
    })();
    state.flight=work;return work;
  }
  function receiveTaskSelection(next) {
    const prior=state.taskSelection;
    const scope=t=>JSON.stringify([t?.source_instance_id,t?.ex_session,t?.task_session]);
    if(!next || typeof next.connected!=='boolean' || next.ex_session!==state.session ||
      !Number.isInteger(next.sequence) || next.sequence<0) {state.taskTraceStale=true;return;}
    if(next.connected && (typeof next.source_instance_id!=='string' || !next.source_instance_id ||
      typeof next.task_session!=='string' || !next.task_session || state.retiredTaskScopes.has(scope(next)))) {
      state.taskTraceStale=true;return;
    }
    if(prior?.connected && next.connected && scope(prior)!==scope(next))state.retiredTaskScopes.add(scope(prior));
    if(prior && scope(prior)===scope(next) && next.sequence<prior.sequence) {state.taskTraceStale=true;return;}
    state.taskSelection=next;state.taskTraceStale=false;
  }
  function renderTaskSelection() {
    const panel=el('decisionTaskSelection'), trace=state.taskSelection;
    const node=(tag,value)=>{const n=document.createElement(tag);n.textContent=value ?? '未提供';return n;};
    if(!trace?.connected){panel.replaceChildren(node('p','未接入'));return;}
    const stamp=Date.parse(trace.updated_at);
    const stale=state.taskTraceStale || !Number.isFinite(stamp) || Date.now()-stamp>10000 || stamp>Date.now()+1000;
    const fields=document.createElement('dl');
    for(const [key,value] of [['连接',stale?'数据陈旧':'已连接'],['实例',trace.source_instance_id],
      ['EX 会话',trace.ex_session],['任务会话',trace.task_session],['序号',trace.sequence],
      ['状态',TASK[trace.state] || trace.state],['错误 / 拒绝',trace.error ?? trace.rejection]]) {
      fields.append(node('dt',key),node('dd',value));
    }
    const details=[];
    for(const [key,value] of [['实际任务请求',trace.current_request],['原始模型选择',trace.raw_selection],
      ['新观测绑定',trace.binding],['关联 Goal',trace.goal],['请求证据',trace.model_trace]]) {
      const detail=document.createElement('details');
      detail.append(node('summary',key),node('pre',JSON.stringify(value ?? null,null,2)));details.push(detail);
    }
    panel.replaceChildren(fields,...details);
  }
  function render() {
    text('decisionFreshness',!authenticated()?'等待管理凭据':state.stale?'状态暂不可确认':'当前状态已读取');
    const projection=state.stale?null:state.projection;
    for(const provider of ['jev','laya','structured','openai_json']) {
      const selected=state.config?.saved.backend===provider;
      const button=el('decisionProvider'+provider);
      button.dataset.selected=String(selected);button.setAttribute('aria-pressed',String(selected));
      text('decisionProviderState'+provider,selected?'当前选择 · '+(projection?CONNECTION[projection.connection.state] || '暂不可确认':'暂不可确认'):'点击配置');
      button.disabled=!state.config || !authenticated();
    }
    text('decisionEXState',projection?EX[projection.ex.state] || '状态暂不可确认':'状态暂不可确认');
    text('decisionConnection',projection?CONNECTION[projection.connection.state] || '暂不可确认':'连接暂不可确认');
    text('decisionError',projection?.error?errorMessage({code:projection.error.code}):'');
    el('decisionError').hidden=!projection?.error;
    const task=projection?.task;
    const available=task?.available===true;
    text('decisionTaskStatus',available?(TASK[task.phase] || '任务状态暂不可确认'):'任务暂不可确认');
    const idle=available && task.phase==='idle';
    text('decisionTaskTitle',available && !idle?task.title:'');
    text('decisionTaskGoal',available && !idle && task.current_goal?task.current_goal:'');
    text('decisionTaskProgress',available && !idle && Number.isInteger(task.completed) && Number.isInteger(task.total)?`已完成 ${task.completed} / ${task.total}`:'');
    renderTaskSelection();renderModal();updateButtons();
  }
  function updateButtons() {
    const known=authenticated() && Boolean(state.session);
    // Safety stop is independent of drafts, failed configuration reads, and other operations.
    for(const id of ['decisionStop','decisionModalStop'])el(id).disabled=!known || pending('stop');
    el('decisionStart').disabled=!known || state.stale || state.projection?.ex.can_start!==true || pending('mode') || pending('stop') || state.busy.has('save');
    const ready=known && state.config && !state.review && !state.conflict;
    el('decisionSave').disabled=!ready || !dirty() || state.busy.has('save');
    el('decisionTest').disabled=!ready || pending('test') || state.busy.has('save') || el('decisionSecretClear').checked;
    el('decisionReviewConfig').hidden=!(state.review || state.conflict);
    el('decisionReviewConfig').disabled=!state.config || state.busy.has('save');
    for(const input of el('decisionConfigForm').querySelectorAll('input,select'))input.disabled=state.busy.has('save');
    el('decisionSecret').disabled=state.busy.has('save') || el('decisionSecretClear').checked;
    el('decisionSiliconFlow').disabled=state.busy.has('save') || !state.config?.presets?.openai_json?.siliconflow_qwen3_vl_8b;
  }
  function invalidateTest() {state.draftVersion++;state.testSequence++;state.testMessage='';}
  function changed() {invalidateTest();syncDraft();renderModal();updateButtons();}
  function syncDraft() {
    if(!state.draft || state.modalProvider==='structured')return;
    const provider=state.modalProvider, connection=state.draft[provider].service_connection;
    connection.base_url=el('decisionAddress').value.trim();connection.model=el('decisionModel').value;
    if(provider==='openai_json') {
      connection.model=el('decisionJsonModel').value.trim();
      connection.auth_mode=el('decisionJsonAuth').value;
      state.draft.openai_json.response_format=el('decisionJsonFormat').value;
      state.draft.openai_json.require_image=el('decisionJsonImage').checked;
    }
    if(provider==='laya') {
      connection.mode=el('decisionLayaMode').value;connection.auth_mode=el('decisionAuth').value;
      state.draft.laya.deployment=connection.mode==='owned'?{launcher:'subprocess',python:el('decisionPython').value.trim(),cache:el('decisionCache').value.trim(),device:el('decisionDevice').value}:null;
    }
  }
  function populateModal() {
    const provider=state.modalProvider, connection=state.draft[provider]?.service_connection;
    const direct=provider==='structured', json=provider==='openai_json';
    const label={jev:'Jev',laya:'Laya',structured:'上层结构化动作',openai_json:'结构化模型'}[provider];
    text('decisionModalTitle',label+' 配置');
    el('decisionAddress').closest('label').hidden=direct;
    el('decisionAddress').required=!direct;
    el('decisionModel').closest('label').hidden=direct || json;
    el('decisionModel').required=!direct && !json;
    el('decisionJsonFields').hidden=!json;
    el('decisionJsonModel').required=json;
    el('decisionSecret').closest('label').hidden=direct;
    el('decisionSecretClear').closest('label').hidden=direct;
    el('decisionSecretState').hidden=direct;
    if(direct) {
      el('decisionSecret').value='';el('decisionSecretClear').checked=false;
      renderModal();updateButtons();return;
    }
    if(json) {
      el('decisionJsonModel').value=connection.model;
      el('decisionJsonAuth').value=connection.auth_mode;
      el('decisionJsonFormat').value=state.draft.openai_json.response_format;
      el('decisionJsonImage').checked=state.draft.openai_json.require_image;
    }
    el('decisionAddress').value=connection.base_url;
    const models=provider==='jev'?[...new Set(['jev-latest',connection.model])]:['typed-decisions'];
    el('decisionModel').replaceChildren(...models.map(model=>{
      const option=document.createElement('option');option.value=model;option.textContent=model;return option;
    }));el('decisionModel').value=connection.model;
    el('decisionLayaMode').value=provider==='laya'?connection.mode:'external';
    el('decisionAuth').value=provider==='laya'?connection.auth_mode:'bearer';
    const deployment=state.draft.laya.deployment;
    el('decisionPython').value=deployment?.python || '';el('decisionCache').value=deployment?.cache || '';
    el('decisionDevice').value=deployment?.device || '';
    el('decisionSecret').value='';el('decisionSecretClear').checked=false;
    renderModal();updateButtons();
  }
  function applySiliconFlow() {
    if(state.modalProvider!=='openai_json' || state.busy.has('save'))return;
    const preset=state.config?.presets?.openai_json?.siliconflow_qwen3_vl_8b;
    if(!preset)return;
    const compatible=state.draft.openai_json;
    compatible.service_connection=clone(preset.service_connection);
    compatible.response_format=preset.response_format;
    invalidateTest();
    populateModal();
    el('decisionSecret').focus();
  }
  function renderModal() {
    if(!state.modalProvider)return;
    const laya=state.modalProvider==='laya', owned=laya && el('decisionLayaMode').value==='owned';
    el('decisionLayaFields').hidden=!laya;el('decisionOwnedFields').hidden=!owned;
    el('decisionPython').required=el('decisionCache').required=el('decisionDevice').required=owned;
    text('decisionProbeHint',state.modalProvider==='structured'?'测试会在本机检查结构化动作入口。':'测试会调用模型一次，可能产生费用或加载开销。');
    text('decisionServiceHint',state.modalProvider==='structured'?'上层提交动作 JSON，EX 在本地校验并执行。':state.modalProvider==='openai_json'?'填写含 /v1 的基础地址及模型名称。更换地址时需重新填写密钥。':!laya?'选择已配置的 JEV 模型。':owned?'EX 只管理自行启动的服务。请填写已安装的真实部署信息。':'连接已启动的服务；停止 EX 不会终止外部服务。');
    const changedEndpoint=state.modalProvider==='openai_json' && state.draft.openai_json.service_connection.base_url!==state.config?.saved.openai_json.service_connection.base_url;
    const hasKey=state.config?.secrets[state.modalProvider]?.configured;
    text('decisionSecretState',changedEndpoint?'请输入新服务的密钥。保存后将移除旧地址的密钥。':
      hasKey?'已设置密钥，留空保留。':'尚未设置密钥。');
    text('decisionConfigNote',state.review?'会话或凭据已变化，请核对草稿。':state.conflict?'服务器配置已变化，草稿已保留。':dirty()?'有未保存的修改。':'配置与已保存内容一致。');
    text('decisionTestResult',state.testMessage);
  }
  function discardAllowed() {return !dirty() || window.confirm('放弃未保存的配置和密钥修改？');}
  function openProvider(provider) {
    if(!state.config || !authenticated() || state.busy.has('save'))return;
    if(state.modalProvider && !discardAllowed())return;
    state.focus=el('decisionProvider'+provider);state.modalProvider=provider;
    state.draft=clone(state.config.saved);state.draftBase=clone(state.config.saved);state.draft.backend=provider;
    state.draftRevision=state.config.revision;state.conflict=state.review=false;invalidateTest();populateModal();
    if(!el('decisionModal').open)el('decisionModal').showModal();
    el(provider==='structured'?'decisionSave':'decisionAddress').focus();
  }
  function closeModal(force=false) {
    if(!force && (state.busy.has('save') || !discardAllowed()))return false;
    el('decisionSecret').value='';el('decisionSecretClear').checked=false;
    state.draft=state.draftBase=null;state.modalProvider=null;invalidateTest();el('decisionModal').close();
    state.focus?.focus();state.focus=null;return true;
  }
  function reviewConfig() {
    if(!state.config || !state.draft)return;
    const provider=state.modalProvider, previous=state.draft;
    state.draft=clone(state.config.saved);state.draft.backend=provider;
    if(provider!=='structured')state.draft[provider].service_connection=clone(previous[provider].service_connection);
    if(provider==='laya')state.draft.laya.deployment=clone(previous.laya.deployment);
    if(provider==='openai_json') {
      state.draft.openai_json.response_format=previous.openai_json.response_format;
      state.draft.openai_json.require_image=previous.openai_json.require_image;
    }
    state.draftBase=clone(state.config.saved);state.draftRevision=state.config.revision;state.conflict=state.review=false;
    invalidateTest();renderModal();updateButtons();
  }
  function validateDraft() {
    syncDraft();
    if(state.modalProvider==='structured')return true;
    if(!el('decisionConfigForm').reportValidity())return false;
    try {
      const url=new URL(state.draft[state.modalProvider].service_connection.base_url);
      if(url.username || url.password || url.search || url.hash || !['http:','https:'].includes(url.protocol))throw new Error();
    } catch {message('请填写不含凭据或查询参数的服务地址。','warning');return false;}
    return true;
  }
  function envelope() {return {ex_session:state.session,expected_revision:state.config.revision};}
  function handleError(error) {
    if(error.obsolete)return;
    if(['revision_conflict','session_conflict','storage_write_uncertain','secret_cleanup_failed'].includes(error.code)) {
      state.conflict=true;state.review=error.code==='session_conflict';
    }
    message(errorMessage(error),'warning');schedule(0);render();
  }
  async function save() {
    if(el('decisionSave').disabled || !validateDraft())return;
    const captured=ticket(), provider=state.modalProvider, config=clone(state.draft);
    const value=el('decisionSecret').value.trim(), action=el('decisionSecretClear').checked?'clear':value?'set':'keep';
    state.busy.add('save');updateButtons();
    let committed=false;
    try {
      const result=await request('/config',{ex_session:state.session,expected_revision:state.draftRevision,config});
      if(!current(captured))return;
      receiveConfig(result.data);committed=true;
      if(provider!=='structured') await request('/secret',{ex_session:captured.session,expected_revision:result.data.revision,provider,action,...(action==='set'?{value}:{})});
      if(!current(captured))return;
      receiveConfig((await request('/config')).data);
      closeModal(true);message('配置已保存；尚未启用 EX。');schedule(0);
    } catch(error) {
      if(current(captured)){if(committed){state.conflict=true;message('配置已保存，密钥操作未确认；请重新读取并核对。','warning');schedule(0);}else handleError(error);}
    } finally {if(current(captured)){state.busy.delete('save');render();}}
  }
  async function startOperation(kind,path,extra={},binding=null) {
    if(pending(kind) || !authenticated() || !state.session)return;
    const captured=ticket();state.busy.add(kind);updateButtons();
    if(kind!=='test')message(kind==='stop'?'正在请求停止，等待实际状态。':'正在请求启用，等待实际状态。');
    try {
      const {data,status}=await request(path,{...envelope(),...extra});
      if(!current(captured))return;
      if(status!==202 || !data.operation_id || data.ex_session!==state.session)throw {code:'invalid_operation'};
      state.operations.set(data.operation_id,{kind,state:'pending',binding,started:performance.now()});scheduleOperations();
    } catch(error) {if(current(captured)){handleError(error);if(kind==='test' && validBinding(binding)){state.testMessage=errorMessage(error);renderModal();}}}
    finally {if(current(captured)){state.busy.delete(kind);updateButtons();}}
  }
  function test() {
    if(el('decisionTest').disabled || !validateDraft())return;
    const binding={...ticket(),provider:state.modalProvider,draft:state.draftVersion,sequence:++state.testSequence,revision:state.config.revision};
    const value=el('decisionSecret').value.trim();
    state.testMessage='正在测试当前草稿…';renderModal();
    startOperation('test','/test',{provider:state.modalProvider,config:clone(state.draft),...(value?{value}:{})},binding);
  }
  function validBinding(b) {return b && current(b) && b.provider===state.modalProvider && b.draft===state.draftVersion && b.sequence===state.testSequence && b.revision===state.config?.revision;}
  function scheduleOperations() {
    if(!visible() || state.opTimer || state.opFlight || ![...state.operations.values()].some(o=>!TERMINAL.has(o.state)))return;
    state.opTimer=setTimeout(()=>{state.opTimer=null;pollOperations();},500);
  }
  async function pollOperations() {
    if(!visible() || state.opFlight)return;
    const captured=ticket();
    const work=(async()=>{
      for(const [id,op] of state.operations) {
        if(!current(captured))break;
        if(TERMINAL.has(op.state))continue;
        try {
          if(performance.now()-op.started>30000)throw {timeout:true};
          const {data}=await request('/operations/'+encodeURIComponent(id),null,true);
          if(data.ex_session!==state.session)throw {obsolete:true};
          op.state=data.operation.state;
          if(!TERMINAL.has(op.state))continue;
          if(op.kind==='test' && validBinding(op.binding)) {
            const result=data.operation.result;
            const local=result?.provider==='structured';
            const success=op.state==='succeeded' && result?.ok===true && (local || result?.inference_ok===true);
            state.testMessage=success?(local?'本地结构化动作入口可用。':
              result.binding?.current_config_verified===true?'已验证当前保存的模型连接。':'草稿模型连接测试通过。'):
              result?.health_ok===true && result?.error_code==='http_401'?'服务可达，但供应商认证失败。':errorMessage({code:result?.error_code || data.operation.error_code});
          } else if(op.kind!=='test')message(op.state==='succeeded'?'操作已完成，正在读取实际状态。':errorMessage({code:data.operation.error_code}),'');
          schedule(0);
        } catch(error) {
          if(error.obsolete)continue;
          if(!current(captured))break;
          op.state='unavailable';
          if(op.kind==='test' && validBinding(op.binding))state.testMessage=errorMessage(error);
          else if(op.kind!=='test')message('操作结果暂不可确认，请读取当前状态。','warning');
          schedule(0);
        }
      }
      if(current(captured)) {while(state.operations.size>32){const entry=[...state.operations].find(([,o])=>TERMINAL.has(o.state));if(!entry)break;state.operations.delete(entry[0]);}render();}
    })();
    state.opFlight=work;
    try {await work;} finally {if(state.opFlight===work)state.opFlight=null;scheduleOperations();}
  }
  function visibilityChanged() {
    if(document.hidden){clearTimeout(state.timer);clearTimeout(state.opTimer);state.timer=state.opTimer=null;}
    else {state.stale=true;render();schedule(0);scheduleOperations();}
  }
  function init() {
    if(state.initialized)return;state.initialized=true;
    for(const provider of ['jev','laya','structured','openai_json'])el('decisionProvider'+provider).addEventListener('click',()=>openProvider(provider));
    for(const input of el('decisionConfigForm').querySelectorAll('input,select'))input.addEventListener('input',changed);
    el('decisionConfigForm').addEventListener('submit',event=>{event.preventDefault();save();});
    el('decisionModal').addEventListener('cancel',event=>{event.preventDefault();closeModal();});
    el('decisionModalClose').addEventListener('click',()=>closeModal());el('decisionCancel').addEventListener('click',()=>closeModal());
    el('decisionReviewConfig').addEventListener('click',reviewConfig);el('decisionTest').addEventListener('click',test);
    el('decisionStart').addEventListener('click',()=>startOperation('mode','/mode',{mode:'execute'}));
    for(const id of ['decisionStop','decisionModalStop'])el(id).addEventListener('click',()=>startOperation('stop','/stop',{reason:'management_stop'}));
    el('decisionSiliconFlow').addEventListener('click',applySiliconFlow);
    el('decisionRefresh').addEventListener('click',()=>schedule(0));render();
  }
  function enter() {init();if(state.active)return;state.active=true;document.addEventListener('visibilitychange',visibilityChanged);schedule(0);scheduleOperations();}
  function leave() {
    if(state.modalProvider && !closeModal())return false;
    state.active=false;state.readEpoch++;clearTimeout(state.timer);clearTimeout(state.opTimer);state.timer=state.opTimer=null;
    document.removeEventListener('visibilitychange',visibilityChanged);return true;
  }
  window.addEventListener('beforeunload',event=>{if(dirty()){event.preventDefault();event.returnValue='';}});
  return {init,enter,leave,credentialChanged,notify:()=>schedule(0),refresh:()=>schedule(0),connectionChanged:()=>schedule(0),hasDraft:dirty};
})();
