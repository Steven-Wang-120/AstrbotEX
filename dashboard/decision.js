/* B09 maps only the existing B08 HTTP projections. No Goal or inference entry. */
window.DecisionPage = (() => {
  const PREFIX = '/api/v1/ex/decision';
  const TERMINAL = new Set(['succeeded', 'failed', 'blocked', 'superseded', 'unavailable']);
  const LABELS = {disabled:'已禁用', shadow:'影子模式', execute:'执行模式', pending:'排队中',
    requested:'停止已请求', running:'处理中 / 执行中', succeeded:'成功', failed:'失败',
    blocked:'受阻', superseded:'已被新操作取代', unavailable:'记录不可用', stopped:'服务已停止',
    starting:'服务启动中', ready:'服务就绪', stopping:'停止处理中', restart_required:'需要显式恢复',
    proven:'停止已证明', admitted:'EX 已受理', accepted:'Actor 已接收', discarded:'已丢弃',
    rejected:'已拒绝', idle:'空闲', active:'活动中', completed:'已完成', canceled:'已取消',
    stopping_for_replace:'正在停止旧 Goal', wait:'等待', request_replan:'请求重新规划',
    start:'开始', cancel:'取消', keep:'继续', pause:'暂停', resume:'恢复', unknown:'未知状态'};
  const FIELDS = [
    ['mock.kind','Mock 选择','select',['wait','request_replan','start','cancel']],
    ['laya.enabled','启用 Laya 适配器','checkbox'], ['laya.allow_live_http','允许本地推理 HTTP','checkbox'],
    ['laya.deadline_ms','决策期限（ms）','number',1,1500],
    ['laya.min_interval_ms','最小调用间隔（ms）','number',0,60000],
    ['laya.max_request_bytes','请求上限（字节）','number',1,16384],
    ['laya.max_response_bytes','响应上限（字节）','number',1,65536],
    ['laya.max_owners','owner 数量上限','number',1,4],
    ['laya.max_candidates_per_owner','每个 owner 候选上限','number',1,8],
    ['laya.max_len','序列长度上限','number',32,1024],
    ['laya.head_max_len','问题头长度上限','number',32,256],
    ['jev.allow_live_http','允许 Jev 云端请求','checkbox'],
    ['jev.deadline_ms','决策期限（ms）','number',1,60000],
    ['jev.min_interval_ms','最小调用间隔（ms）','number',0,60000],
  ];
  const view = {active:false, generation:0, readEpoch:0, session:null, revision:null, framework:null,
    config:null, status:null, snapshot:null, catalog:null, backends:[], draft:null, edited:new Set(),
    draftRevision:null, reviewRequired:false, conflict:false, authEpoch:null, stale:true, lastOK:null,
    flight:null, timer:null, opTimer:null, opFlight:null, lastStart:0, requests:new Map(), controllers:new Set(),
    operations:new Map(), busy:new Set(), history:null, actions:null, historyCursors:[0], actionCursors:[''],
    selectedRequest:null, selectedRecord:null, detailFlight:null, commandFilter:null, initialized:false, sse:false};
  const el = id => document.getElementById(id);
  const text = (id, value) => { const node=el(id); if(node) node.textContent=value ?? '未提供'; };
  const node = (tag, value, cls) => { const n=document.createElement(tag); if(value!==undefined)n.textContent=value; if(cls)n.className=cls; return n; };
  const copy = value => value == null ? value : structuredClone(value);
  const valueAt = (value, path) => path.split('.').reduce((a,k)=>a?.[k],value);
  const assign = (value, path, item) => {const [group,key]=path.split('.'); if(key)value[group][key]=item; else value[group]=item;};
  const label = value => value==null ? '未知 / 未提供' : (LABELS[value] ? `${LABELS[value]} (${value})` : `未知状态 (${String(value)})`);
  const tri = value => value===true ? '是' : value===false ? '否' : '未知';
  const authenticated = () => Boolean(managementCredential);
  const visible = () => view.active && !document.hidden && authenticated();
  const current = ticket => ticket.generation===view.generation && ticket.auth===managementCredentialEpoch && ticket.session===view.session && (ticket.readEpoch===undefined || ticket.readEpoch===view.readEpoch);
  const ticket = () => ({generation:view.generation,auth:managementCredentialEpoch,session:view.session});
  const readTicket = () => ({...ticket(),readEpoch:view.readEpoch});
  function message(value, kind='') {view.noticeOperation=null;text('decisionMessage',value);el('decisionMessage').dataset.kind=kind;}
  function pairs(entries) {
    const dl=node('dl',undefined,'decision-kv');
    entries.forEach(([key,value])=>{dl.append(node('dt',key),node('dd',value==null?'未提供':typeof value==='object'?JSON.stringify(value):String(value)));});
    return dl;
  }
  function details(title, value, key=title) {
    const d=node('details',undefined,'decision-json');d.dataset.key=key;d.append(node('summary',title));
    d.append(node('pre',value==null?'未提供':typeof value==='string'?value:JSON.stringify(value,null,2)));return d;
  }
  function payload(title, value, missing='未采集') {
    if(value==null)return node('p',`${title}：${missing}`,'decision-muted');
    const d=details(title,value.truncated?value.preview:value.value,title);
    if(value.truncated)d.insertBefore(node('p',`内容已截断 · 原始大小 ${value.original_bytes ?? '未知'} 字节`,'decision-warning'),d.lastChild);
    return d;
  }
  function replace(id, children) {
    const root=el(id), opens=new Set([...root.querySelectorAll('details[open]')].map(d=>d.dataset.key));
    root.replaceChildren(...children);root.querySelectorAll('details').forEach(d=>{d.open=opens.has(d.dataset.key);});
  }
  // Deliberately local to B08. Future HTTP projections require an explicit mapping here.
  function mapData() {
    return {decision:view.status?.decision || {}, service:view.status?.service || {},
      goal:view.snapshot?.current_goal ?? null, pending:view.snapshot?.pending_goal ?? null,
      phase:view.snapshot?.goal_phase ?? view.status?.decision?.goals?.phase,
      lastBuilt:view.snapshot?.last_built, lastSubmitted:view.snapshot?.last_submitted,
      lastResult:view.snapshot?.last_result, aeb:null, control:null, publicDelivery:null};
  }
  function resetScope() {
    view.generation++;view.controllers.forEach(c=>c.abort());view.controllers.clear();view.requests.clear();
    clearTimeout(view.timer);clearTimeout(view.opTimer);view.timer=view.opTimer=null;
    view.flight=view.opFlight=view.detailFlight=null;view.session=view.revision=view.framework=null;
    view.config=view.status=view.snapshot=view.catalog=null;view.backends=[];view.operations.clear();view.busy.clear();
    view.history=view.actions=view.selectedRecord=null;view.selectedRequest=view.commandFilter=null;
    view.historyCursors=[0];view.actionCursors=[''];view.reviewRequired=view.edited.size>0;
    view.conflict=false;view.stale=true;view.lastOK=null;view.lastStart=0;
    el('decisionSecret').value='';el('decisionSecretAction').value='keep';
  }
  function credentialChanged() {
    if(!view.initialized)return;
    view.authEpoch=managementCredentialEpoch;resetScope();render();
    message(authenticated()?'凭据已变化，正在重新读取当前会话。':'请输入有效管理凭据。旧操作关联已清除。');
    if(visible())schedule(0);
  }
  async function request(key, path, options={}) {
    if(view.requests.has(key))return view.requests.get(key);
    const captured=readTicket(), controller=new AbortController();view.controllers.add(controller);
    const deadline=setTimeout(()=>controller.abort(new DOMException("读取超时", "TimeoutError")),10000);
    const work=(async()=>{
      const response=await managementFetch(PREFIX+path,{...options,signal:controller.signal,
        headers:{'Content-Type':'application/json',...(options.headers||{})}});
      const data=await response.json();
      if(!current(captured))throw Object.assign(new Error('旧响应已忽略'),{obsolete:true});
      if(!response.ok || data.ok===false)throw Object.assign(new Error(data.message||data.code||`HTTP ${response.status}`),{status:response.status,code:data.code,data});
      return {data,status:response.status};
    })();
    view.requests.set(key,work);
    try{return await work;}finally{clearTimeout(deadline);view.controllers.delete(controller);if(view.requests.get(key)===work)view.requests.delete(key);}
  }
  function accept(data) {
    if(data.ex_session!==view.session)return false;
    if(Number.isInteger(view.revision) && data.revision<view.revision)return false;
    if(Number.isInteger(view.framework) && data.framework_config_revision<view.framework)return false;
    view.revision=data.revision;view.framework=data.framework_config_revision;return true;
  }
  function overlayDraft(saved) {
    const next=copy(saved);
    for(const path of view.edited){if(path==='backend' || FIELDS.some(f=>f[0]===path))assign(next,path,valueAt(view.draft,path));}
    view.draft=next;
  }
  function receiveConfig(data) {
    view.config=data;
    if(!view.draft || !view.edited.size){view.draft=copy(data.saved);view.draftRevision=data.revision;view.reviewRequired=false;view.conflict=false;}
    else {overlayDraft(data.saved);if(view.draftRevision!==data.revision)view.conflict=true;}
  }
  function schedule(delay=1000) {
    if(!visible() || view.timer)return;
    const wait=Math.max(delay,1000-(performance.now()-view.lastStart));
    view.timer=setTimeout(()=>{view.timer=null;refresh();},Math.max(0,wait));
  }
  async function refresh() {
    if(!visible() || view.flight)return view.flight;
    view.lastStart=performance.now();
    const work=(async()=>{
      try {
        const first=(await request('status','/status')).data;
        if(first.ex_session!==view.session){
          const previous=view.session;resetScope();view.session=first.ex_session;view.flight=work;view.lastStart=performance.now();
          if(previous)message('EX 会话已变化。旧请求与操作关联已清除；草稿需重新核对。','warning');
        }
        if(!accept(first))return;
        view.status=first;const captured=readTicket();
        const hc=view.historyCursors.at(-1), ac=view.actionCursors.at(-1), filter=view.commandFilter;
        const paths=[['backends','/backends'],['config','/config'],['catalog','/catalog'],['snapshot','/snapshot'],
          ['history',`/decisions?cursor=${hc}&limit=20`],['actions',filter?`/actions?command_id=${encodeURIComponent(filter)}`:`/actions?cursor=${encodeURIComponent(ac)}&limit=20`]];
        const results=await Promise.allSettled(paths.map(([key,path])=>request(key,path)));
        if(!current(captured))return;
        let incomplete=false;
        results.forEach((result,i)=>{
          if(result.status!=='fulfilled'){incomplete=true;return;}
          const data=result.value.data,key=paths[i][0];if(!accept(data)){incomplete=true;return;}
          if(key==='config')receiveConfig(data);
          else if(key==='backends')view.backends=data.backends||[];
          else if(key==='history'){if(hc===view.historyCursors.at(-1))view.history=data;}
          else if(key==='actions'){if(ac===view.actionCursors.at(-1) && filter===view.commandFilter)view.actions=data;}
          else view[key]=data;
        });
        view.stale=incomplete;view.lastOK=incomplete?view.lastOK:Date.now();
        render();if(view.selectedRequest)loadRequestDetail();
      } catch(error) {
        if(!error.obsolete && error.name!=='AbortError'){view.stale=true;message(`读取失败：${error.code||error.message}。保留最近数据，数据已过期。`,'error');render();}
      } finally {
        if(view.flight===work)view.flight=null;
        schedule();scheduleOperations();updateButtons();
      }
    })();
    view.flight=work;return work;
  }
  function scheduleOperations() {
    if(!visible() || view.opTimer || view.opFlight || ![...view.operations.values()].some(o=>!TERMINAL.has(o.state)))return;
    view.opTimer=setTimeout(()=>{view.opTimer=null;pollOperations();},500);
  }
  async function pollOperations() {
    if(!visible() || view.opFlight)return;
    const captured=readTicket();
    const work=(async()=>{
      for(const [id,prior] of [...view.operations]) {
        if(!current(captured) || !visible())break;
        if(TERMINAL.has(prior.state))continue;
        try {
          const data=(await request('operation:'+id,'/operations/'+encodeURIComponent(id))).data;
          if(!current(captured) || data.ex_session!==view.session)continue;
          view.operations.set(id,{...data.operation,ui_key:prior.ui_key});
          if(TERMINAL.has(data.operation.state)){
            if(view.noticeOperation===id)message(`${data.operation.kind} 管理操作：${label(data.operation.state)}${data.operation.error_code?' · '+data.operation.error_code:''}。动作结果和停止证明见独立记录。`,data.operation.state==='succeeded'?'':'warning');
            schedule(0);
          }
        } catch(error) {
          if(!current(captured))break;
          if(error.status===404){view.operations.set(id,{...prior,state:'unavailable',error_code:'记录已不可查询，原结果未知'});if(view.noticeOperation===id)message('操作记录已不可查询，原结果未知。正在读取当前状态。','warning');schedule(0);}
          else if(!error.obsolete && error.name!=='AbortError'){view.stale=true;view.operations.set(id,{...prior,query_error:error.code||error.message});}
        }
      }
      if(current(captured)){renderOperations();updateButtons();renderFreshness();}
    })();
    view.opFlight=work;
    try{await work;}finally{if(view.opFlight===work)view.opFlight=null;scheduleOperations();}
  }
  function renderFreshness() {
    text('decisionFreshness',!authenticated()?'等待凭据':view.stale?'数据已过期 / 等待完整读取':`最近读取 ${new Date(view.lastOK).toLocaleTimeString()}`);
    el('decisionFreshness').dataset.stale=String(view.stale);
    text('decisionTransport',view.sse?'SSE 已连接 · 状态读取最多 1 次/秒':'SSE 未连接 · 只读轮询继续，事件更新可能滞后');
  }
  function renderStatus() {
    const {decision:d,service:s}=mapData();
    text('decisionBackend',d.backend?.name ?? '未提供');text('decisionModeState',label(d.mode));
    text('decisionServiceState',label(s.state));text('decisionBlocked',tri(d.blocked));
    const capability=view.backends.find(b=>b.name===d.backend?.name);
    replace('decisionStatusFacts',[pairs([['执行权限',tri(d.execution_allowed)],['执行范围',capability?.execution_scope ?? (d.backend?.name==='mock'?'Mock 后端': '以服务端能力为准')],
      ['执行门禁',tri(d.gate_open)],['EX 会话',view.session],['保存版本',view.config?.revision],['实际生效版本',view.config?.effective_revision],
      ['框架配置版本',view.status?.framework_config_revision],['服务代次',s.generation],['需要显式恢复',tri(s.restart_required)],
      ['进程归属不明',tri(s.ownership_unknown)],['停止状态',label(d.stop?.state)],['停止回执 ID',d.stop?.operation_id],['停止错误',d.stop?.error ?? d.stop_error]])]);
    replace('decisionStatusDetails',[details('后端健康与停止回执',{service:s,stop:d.stop,blocked:d.blocked,unresolved:d.unresolved})]);
    const q=view.status?.model_quality;
    text('decisionQuality',q?`已知 Laya replan 正确 ${q.known_replan_correct ?? '未知'}/${q.known_replan_trials ?? '未知'}；机器人任务质量${q.robot_task_quality_verified===true?'按后端标记为已验证':'未验证'}。`:'模型质量记录尚未读取。');
  }
  function renderOperations() {
    const cards=[node('p',`服务端操作计数：${JSON.stringify(view.status?.operations || {})}。以下只追踪本页发起的操作；刷新后不推测旧 operation ID。`,'decision-muted')];
    for(const op of [...view.operations.values()].reverse()){
      const card=node('article',undefined,'decision-record');card.dataset.operationId=op.operation_id;
      card.append(node('h3',`${op.kind} · ${label(op.state)}`),pairs([['operation_id',op.operation_id],['错误',op.error_code ?? op.query_error],['配置版本',op.revision],['服务代次',op.service_generation]]));
      if(op.result?.scope)card.append(node('p',`探测范围：${op.result.scope}；结果 ${tri(op.result.ok)}；推理调用 ${tri(op.result.inference_called)}`));
      if(op.state==='succeeded')card.append(node('p','此状态只表示该管理操作完成；动作与 Goal 结果见各自记录。','decision-muted'));
      card.append(details('操作结果与关联停止回执',op,op.operation_id));cards.push(card);
    }
    replace('decisionOperations',cards);
  }
  function renderConfig() {
    const select=el('decisionBackendSelect'), wanted=view.draft?.backend || '';
    const options=view.backends.map(b=>b.name);
    if([...select.options].map(o=>o.value).join('|')!==options.join('|')){
      select.replaceChildren(...options.map(name=>{const o=node('option',name);o.value=name;return o;}));
    }
    select.value=wanted;
    FIELDS.forEach(([path,,type])=>{const input=el('decision-field-'+path);if(!input)return;const value=valueAt(view.draft,path);if(type==='checkbox')input.checked=value===true;else if(document.activeElement!==input)input.value=value ?? '';});
    document.querySelectorAll('[data-decision-backend]').forEach(n=>{n.hidden=n.dataset.decisionBackend!==wanted;});
    text('decisionConfigNote',view.edited.size?`未保存（${view.edited.size} 项）${view.conflict?' · 服务器版本已变化':''}${view.reviewRequired?' · 需核对当前会话':''}`:'草稿与已保存配置一致');
    const c=view.config;
    text('decisionConfigVersions',`saved ${c?.revision ?? '未知'} / effective ${c?.effective_revision ?? '未应用'} / 草稿基于 ${view.draftRevision ?? '未知'}`);
    text('decisionSecretState',c?`Jev 密钥：${c.secrets?.jev?.configured===true?'已配置':'未配置'}`:'Jev 密钥状态未知');
    text('decisionLayaIdentity',`模型 ${c?.saved?.laya?.model ?? '未知'}\nrevision ${c?.saved?.laya?.revision ?? '未知'}\n回环端口 ${c?.saved?.laya?.port ?? '未知'}`);
    const jev=view.backends.find(b=>b.name==='jev');text('decisionJevIdentity',`服务地址 ${jev?.address ?? '未提供'}\n模型 ${c?.saved?.jev?.model ?? '未知'}\n固定模式 ${c?.saved?.jev?.mode ?? '未知'}`);
    const effective=view.backends.find(b=>b.name===c?.saved?.backend);
    text('decisionProbeScope',c?.saved?.backend==='laya'?'测试连接仅 GET /health，不证明模型推理或解除隔离。':c?.saved?.backend==='jev'?'Jev 真实探测尚未实现；该按钮不发起付费请求。':'Mock 仅检查本地构造能力，不发送网络请求。');
    text('decisionApplyHint',`服务与模式操作使用已保存配置：${c?.saved?.backend ?? '未知'}。execute ${effective?.execution_allowed===true?'以已授权后端能力为准':'不可用'}。`);
    const execOption=el('decisionApplyMode').querySelector('[value="execute"]');execOption.disabled=effective?.execution_allowed!==true;
    if(execOption.disabled && el('decisionApplyMode').value==='execute')el('decisionApplyMode').value='shadow';
    replace('decisionConfigProjection',[details('已保存配置 saved',c?.saved),details('实际应用配置 effective',c?.effective)]);
  }
  function renderGoal() {
    const {goal,pending,phase}=mapData();
    const sections=[pairs([['Goal 管理阶段',label(phase)],['Goal 管理版本',view.status?.decision?.goals?.revision]])];
    for(const [title,g] of [['当前 Goal',goal],['待替代 Goal',pending]]){
      const card=node('article',undefined,'decision-record');card.append(node('h3',title));
      if(!g)card.append(node('p','无','decision-muted'));
      else {const rows=[['task_id',g.task_id],['step_id',g.step_id],['goal_id',g.goal_id],['Goal',g.goal_text_en],['阶段',label(phase)],['提交 request_id',g.request_id]];
        if(Object.hasOwn(g,'source'))rows.push(['来源',g.source]);
        card.append(pairs(rows),details('参数',g.parameters,title+'params'),details('正式 Goal 原文',g,title));}
      sections.push(card);
    }
    replace('decisionGoals',sections);
  }
  function requestCard(record, compact=false) {
    const card=node('article',undefined,'decision-record');
    if(!record){card.append(node('p','暂无请求','decision-muted'));return card;}
    card.dataset.requestId=record.request_id;
    card.append(node('h3',`${record.backend ?? '未知后端'} · ${record.request_id ?? '无 request_id'}`));
    card.append(pairs([['snapshot_id',record.snapshot_id],['模型 / revision',`${record.model ?? '未提供'} / ${record.model_revision ?? '未提供'}`],
      ['模型选择',record.backend_result?.choices ?? (record.backend_result_display?'结果展示已截断':'尚未返回 / 未采集')],
      ['EX 实际结果',label(record.service_outcome)],['EX 归类',label(record.ex_outcome)],['后端错误',record.backend_error_code],
      ['后端耗时（ms）',record.backend_result?.elapsed_ms ?? (record.completed_monotonic_ns!=null?(record.completed_monotonic_ns-record.started_monotonic_ns)/1e6:null)],
      ['输入 SHA256',record.input_sha256]]));
    const phases=node('div',undefined,'decision-phases');
    for(const [key,title] of [['prepared','请求准备'],['post_attempted','POST 尝试'],['post_written_to_socket','socket 写入'],['response_received','收到响应']])phases.append(node('span',`${title}：${tri(record[key])}`));
    card.append(phases);
    if(record.record_truncated)card.append(node('p',`记录已截断 · 原始 ${record.original_record_bytes ?? '未知'} 字节`,'decision-warning'));
    if(record.backend_record_missing)card.append(node('p','实际后端记录缺失，不能用快照补造。','decision-warning'));
    for(const id of record.command_ids || []){const b=node('button','查看 command '+id,'btn btn-ghost btn-sm');b.type='button';b.addEventListener('click',()=>{view.commandFilter=id;view.actionCursors=[''];schedule(0);el('decision-actions-panel').scrollIntoView({block:'start'});});card.append(b);}
    if(compact){const b=node('button','查看请求详情','btn btn-ghost btn-sm');b.type='button';b.addEventListener('click',()=>selectRequest(record.request_id));card.append(b);}
    else {
      card.append(payload('实际请求',record.actual_request,record.backend==='jev'?'Jev 正文未采集':'未采集 / 尚未发送'),payload('实际响应',record.actual_response),
        payload('原始快照',record.snapshot),details('EX 受理 / 拒绝过程',record.ex_outcomes),details('关联版本',record.versions));
      for(const [key,value] of Object.entries(record))if(key.endsWith('_display'))card.append(payload(key,value));
      card.append(details('完整记录（含原始概率及归一化）',record,'full:'+record.request_id));
    }
    return card;
  }
  function renderDecisions() {
    const data=mapData();
    replace('decisionBuilt',[payload('最近构建快照',data.lastBuilt,'尚未构建')]);
    replace('decisionLatest',[node('h3','最近实际请求'),requestCard(data.lastSubmitted,true),node('h3','最近已完成模型结果'),requestCard(data.lastResult,true)]);
    const items=view.history?.items || [];
    replace('decisionHistory',items.length?items.map(r=>requestCard(r,true)):[node('p','本页暂无请求记录','decision-muted')]);
    text('decisionHistoryMeta',`第 ${view.historyCursors.length} 页 · 保留 ${view.history?.retained ?? '未知'}/${view.history?.capacity ?? '未知'} 条 · 丢失事件 ${view.history?.lost_events ?? '未知'} · 单条展示预算 ${view.history?.display_budget_bytes ?? '未知'} 字节`);
    replace('decisionCatalog',[details('动作目录及最近候选资格',view.catalog)]);
  }
  async function selectRequest(id) {view.selectedRequest=id;view.selectedRecord=null;await loadRequestDetail();el('decisionRequestDetail').scrollIntoView({block:'nearest'});}
  async function loadRequestDetail() {
    if(!view.selectedRequest || !visible() || view.detailFlight)return;
    const id=view.selectedRequest,captured=readTicket();
    const work=(async()=>{
      try {
        const data=(await request('detail','/decisions?request_id='+encodeURIComponent(id)+'&limit=1')).data;
        if(!current(captured) || id!==view.selectedRequest || !accept(data))return;
        view.selectedRecord=data.items?.find(r=>r.request_id===id) || null;
        replace('decisionRequestDetail',view.selectedRecord?[requestCard(view.selectedRecord)]:[node('p','请求记录已不可用，可能已被裁剪；不推测原结果。','decision-warning')]);
      }catch(error){if(current(captured) && !error.obsolete)replace('decisionRequestDetail',[node('p','请求详情读取失败，数据已过期。','decision-warning')]);}
    })();
    view.detailFlight=work;
    try{await work;}finally{if(view.detailFlight===work)view.detailFlight=null;if(current(captured) && id!==view.selectedRequest)loadRequestDetail();}
  }
  function renderActions() {
    const items=view.actions?.items || [], cards=[];
    for(const row of items){
      const command=row.command?.command || {}, card=node('article',undefined,'decision-record');card.dataset.commandId=row.command_id;
      card.append(node('h3',`${label(row.status)} · ${command.action_id ?? '未提供动作'}`),pairs([['command_id',row.command_id],['owner',row.owner],['原因',row.reason_code],
        ['Goal',command.goal_id],['Goal 版本',command.goal_revision],['关联 snapshot / decision_id',command.decision_id],['持有资源',row.held_resources],
        ['停止证明',row.stop_evidence!=null?'已有账本停止证明':'未提供；不能推断已停稳']]));
      card.append(node('p',`已观察事件：${(row.observed_event_states||[]).map(label).join(' · ') || '暂无'}（按状态列出，不推测顺序）`));
      if(row.event_window_partial)card.append(node('p','部分历史不可见；未出现的事件不能判断为从未发生。','decision-warning'));
      if(row.status==='failed' && row.stop_evidence!=null)card.append(node('p','失败 + 停止已证明；失败结果保持不变。','decision-warning'));
      if(row.details?.test_actor_only)card.append(node('p','隔离测试 Actor 数据；不是机械臂物理成功。','decision-warning'));
      if(row.status==='succeeded')card.append(node('p','动作 succeeded 不代表整个 Goal 或任务已完成。','decision-muted'));
      const matched=[...(view.history?.items||[]),view.snapshot?.last_submitted,view.snapshot?.last_result].find(r=>r?.snapshot_id===command.decision_id);
      if(matched){card.append(pairs([['关联 request_id',matched.request_id]]));const b=node('button','查看关联请求','btn btn-ghost btn-sm');b.type='button';b.addEventListener('click',()=>selectRequest(matched.request_id));card.append(b);}
      card.append(details('StopEvidence',row.stop_evidence,'proof:'+row.command_id),details('动作与通用 details',row,'action:'+row.command_id));cards.push(card);
    }
    replace('decisionActions',cards.length?cards:[node('p','本页暂无动作事实','decision-muted')]);
    text('decisionActionsMeta',view.commandFilter?'按 command_id 查询：'+view.commandFilter:`第 ${view.actionCursors.length} 页 · ${items.length} 条 Ledger 事实`);
    el('decisionActionClear').hidden=!view.commandFilter;
  }
  function updateButtons() {
    if(!view.initialized)return;
    const known=authenticated() && Boolean(view.session), ready=known && Boolean(view.config) && !view.reviewRequired;
    for(const b of el('page-decision').querySelectorAll('[data-decision-write]')){
      const key=b.dataset.decisionWrite, isStop=key==='stop';
      const pending=[...view.operations.values()].some(o=>o.ui_key===key && !TERMINAL.has(o.state));
      b.disabled=!(isStop?known:ready) || view.busy.has(key) || pending;
      if(key==='config')b.disabled=b.disabled || view.conflict || !view.edited.size;
      b.title=isStop && !known?'需要有效凭据和当前 EX 会话':!ready && !isStop?'先读取并核对当前会话与配置':'';
    }
    el('decisionReloadConfig').disabled=!known || Boolean(view.flight);
    el('decisionReviewConfig').disabled=!view.config || !(view.reviewRequired || view.conflict);
    el('decisionHistoryPrev').disabled=view.historyCursors.length<2 || Boolean(view.flight);
    el('decisionHistoryNext').disabled=!view.history?.items?.length || view.history.next_cursor===view.historyCursors.at(-1) || Boolean(view.flight);
    el('decisionActionsPrev').disabled=view.actionCursors.length<2 || Boolean(view.flight) || Boolean(view.commandFilter);
    el('decisionActionsNext').disabled=!view.actions?.items?.length || view.actions.next_cursor===view.actionCursors.at(-1) || Boolean(view.flight) || Boolean(view.commandFilter);
    el('decisionSecret').disabled=el('decisionSecretAction').value!=='set';
  }
  function render() {renderFreshness();renderStatus();renderConfig();renderOperations();renderGoal();renderDecisions();renderActions();updateButtons();}
  async function write(key,path,extra={}) {
    if(view.busy.has(key) || [...view.operations.values()].some(o=>o.ui_key===key && !TERMINAL.has(o.state)))return;
    const isStop=path==='/stop';
    if(!authenticated() || !view.session || (!isStop && (!view.config || view.reviewRequired)))return message('先读取并核对当前会话与配置。','warning');
    const captured=ticket(), editSnapshot=new Set(view.edited);
    const body={ex_session:view.session,...(!isStop?{expected_revision:key==='config'?view.draftRevision:view.config.revision}:{}),...extra};
    view.busy.add(key);updateButtons();message(isStop?'正在请求停止，尚未取得停止证明。':'请求已发出，等待管理接口受理。');
    try {
      // A write is never replayed or canceled by a read refresh. A late response is ignored.
      const response=await managementFetch(PREFIX+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const data=await response.json();if(!current(captured))return;
      if(!response.ok || data.ok===false)throw Object.assign(new Error(data.code||data.message||`HTTP ${response.status}`),{status:response.status,code:data.code,data});
      if(data.ex_session!==view.session)return;
      if(response.status===202){
        view.operations.set(data.operation_id,{operation_id:data.operation_id,kind:path.slice(1).replaceAll('/','_'),ui_key:key,state:'pending',revision:data.revision});
        while(view.operations.size>128){const removable=[...view.operations].find(([,op])=>TERMINAL.has(op.state));if(!removable)break;view.operations.delete(removable[0]);}
        message('请求已受理，处理中。管理操作完成、动作成功和停止证明分别显示。');view.noticeOperation=data.operation_id;scheduleOperations();
      } else {
        if(key==='config')for(const field of editSnapshot)if(valueAt(view.draft,field)===valueAt(body.config,field))view.edited.delete(field);
        if(data.saved && accept(data))receiveConfig(data);
        message(key==='config'?'配置已保存；没有启动服务或应用模式。':'密钥操作已完成；值不会由接口返回。');
      }
      render();schedule(0);
    } catch(error) {
      if(!current(captured))return;
      if(error.status===409){view.conflict=true;message(`409 ${error.code}：请求冲突，草稿已保留；没有自动重发。请读取并核对当前状态。`,'warning');schedule(0);}
      else message(`请求未确认完成：${error.code||error.message}。读取当前状态后再决定下一步；不会自动重发。`,'error');
    } finally {if(current(captured)){view.busy.delete(key);updateButtons();}}
  }
  async function reloadConfig(discard=false) {
    if(discard){view.edited.clear();view.draft=null;view.conflict=false;view.reviewRequired=false;}
    schedule(0);await refresh();
  }
  function reviewConfig() {
    if(!view.config)return;
    overlayDraft(view.config.saved);view.draftRevision=view.config.revision;view.reviewRequired=false;view.conflict=false;
    message('已用当前服务器版本核对草稿；仍需显式保存。');renderConfig();updateButtons();
  }
  function init() {
    if(view.initialized)return;view.initialized=true;view.authEpoch=managementCredentialEpoch;
    const root=el('decisionFields');
    for(const backend of ['mock','jev','laya']){
      const section=node('div',undefined,'decision-form-grid');section.dataset.decisionBackend=backend;
      for(const [path,title,type,min,max] of FIELDS.filter(f=>f[0].startsWith(backend+'.'))){
        const wrapper=node('label',undefined,'connection-field');wrapper.append(node('span',title));
        const input=node(type==='select'?'select':'input');input.id='decision-field-'+path;input.className='config-input';input.dataset.field=path;
        if(type==='select')for(const val of min){const o=node('option',val);o.value=val;input.append(o);}
        else {input.type=type;if(type==='number'){input.min=min;input.max=max;input.step='1';input.required=true;}}
        input.addEventListener('input',()=>{if(!view.draft)return;assign(view.draft,path,type==='checkbox'?input.checked:type==='number'?Number(input.value):input.value);view.edited.add(path);renderConfig();updateButtons();});
        wrapper.append(input);section.append(wrapper);
      }
      root.append(section);
    }
    el('decisionBackendSelect').addEventListener('change',event=>{if(view.draft){view.draft.backend=event.target.value;view.edited.add('backend');renderConfig();updateButtons();}});
    el('decisionSave').addEventListener('click',()=>{
      if([...el('decisionFields').querySelectorAll('input')].some(n=>!n.checkValidity()))return message('配置字段超出允许范围。','error');
      if(view.draft?.laya?.head_max_len>=view.draft?.laya?.max_len)return message('Laya 问题头长度必须小于序列长度。','error');
      write('config','/config',{config:copy(view.draft)});
    });
    el('decisionReloadConfig').addEventListener('click',()=>reloadConfig(true));
    el('decisionReviewConfig').addEventListener('click',reviewConfig);
    el('decisionSecretAction').addEventListener('change',()=>{el('decisionSecret').value='';updateButtons();});
    el('decisionSecretSave').addEventListener('click',()=>{
      const action=el('decisionSecretAction').value,value=el('decisionSecret').value;el('decisionSecret').value='';
      if(action==='set' && !value)return message('设置密钥需要输入值；空值不会清除密钥。','warning');
      write('secret','/secret',{action,...(action==='set'?{value}:{})});
    });
    el('decisionTest').addEventListener('click',()=>write('test','/test'));
    el('decisionApply').addEventListener('click',()=>write('mode','/mode',{mode:el('decisionApplyMode').value}));
    el('decisionStop').addEventListener('click',()=>write('stop','/stop',{reason:'B09 explicit stop'}));
    for(const kind of ['start','stop','recover'])el('decisionService'+kind[0].toUpperCase()+kind.slice(1)).addEventListener('click',()=>write('service_'+kind,'/service/'+kind));
    el('decisionRefresh').addEventListener('click',()=>schedule(0));
    for(const [prefix,cursors,data] of [['History','historyCursors','history'],['Actions','actionCursors','actions']]){
      el('decision'+prefix+'Next').addEventListener('click',()=>{if(view[cursors].at(-1)!==view[data].next_cursor)view[cursors].push(view[data].next_cursor);schedule(0);});
      el('decision'+prefix+'Prev').addEventListener('click',()=>{if(view[cursors].length>1)view[cursors].pop();schedule(0);});
    }
    el('decisionActionClear').addEventListener('click',()=>{view.commandFilter=null;view.actionCursors=[''];schedule(0);});
    render();
  }
  function visibilityChanged() {
    if(document.hidden){clearTimeout(view.timer);clearTimeout(view.opTimer);view.timer=view.opTimer=null;}
    else if(view.active){view.stale=true;renderFreshness();schedule(0);scheduleOperations();}
  }
  function enter() {init();if(view.active)return;view.active=true;document.addEventListener('visibilitychange',visibilityChanged);view.stale=true;renderFreshness();schedule(0);scheduleOperations();}
  function leave() {
    view.active=false;view.readEpoch++;view.flight=view.opFlight=view.detailFlight=null;view.requests.clear();clearTimeout(view.timer);clearTimeout(view.opTimer);view.timer=view.opTimer=null;
    view.controllers.forEach(c=>c.abort());view.controllers.clear();document.removeEventListener('visibilitychange',visibilityChanged);
    el('decisionSecret').value='';
  }
  function connectionChanged(connected) {view.sse=connected;if(!view.initialized)return;if(!connected)view.stale=true;renderFreshness();if(connected)schedule(0);}
  return {init,enter,leave,credentialChanged,connectionChanged,notify:()=>schedule(0),refresh:()=>schedule(0),hasDraft:()=>view.edited.size>0};
})();
