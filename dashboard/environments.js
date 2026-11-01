/* Navigation reads state; only explicit mode clicks change the environment. */
const envView = {flight:null,timer:null,graph:null,switching:false,dirty:false,revision:null,session:null,drafts:new Map(),pluginFlight:null,
 diagnostics:null,diagnosticsSeq:0,diagnosticsFlight:null,diagnosticsRefreshFlight:null,diagnosticsNote:null};
const ENV_LABELS = {normal:"普通环境",ros2:"ROS 2"};
/* ready 只说明资源/匹配就绪，不再暗示已经持续收到数据；持续收数看 data_health。 */
const ROS_STATES = {ready:"资源/匹配就绪",waiting_peer:"已创建 / 等待通信",waiting_environment:"等待 ROS 环境",
 disabled:"已关闭",error:"错误",missing_interface:"缺少接口支持",closed:"已回收",idle:"就绪",starting:"启动中",
 stopping:"停止中",failed:"切换失败",ok:"正常",degraded:"部分功能异常",unknown:"未知",unavailable:"不可用",qos_incompatible:"QoS 或类型不兼容"};
const envSnapshot = () => state.environment?.environment || {};
const rosLabel = value => ROS_STATES[value] || value || "--";
function environmentHasDrafts() {return envView.dirty || [...envView.drafts.values()].some(d=>d.dirty);}
function sameEnvironment(a,b) {return a?.session_id===b?.session_id && a?.generation===b?.generation && a?.revision===b?.revision;}
function renderEnvironmentStatus(payload) {
 const old=envSnapshot(),next=payload.environment;
 if (!next || (old.session_id===next.session_id && next.revision<old.revision)) return;
 state.environment=payload;
 setText("environmentActiveMode",ENV_LABELS[next.active_mode] || next.active_mode);
 setText("environmentPhase",rosLabel(next.phase)); setText("environmentHealth",rosLabel(next.health));
 setText("environmentGeneration",next.generation);
 setText("environmentTopicBus",next.topic_bus_available===true?"正常":"未知");
 setText("environmentRosNode",payload.active_adapter?.node_name || "--");
 setText("environmentOperation",next.operation_id?"操作 "+next.operation_id.slice(0,8):"");
 const reason=next.last_error?.message || payload.active_adapter?.reason || "";
 $("environmentError").hidden=!reason; $("environmentError").textContent=reason;
 const busy=envView.switching || ["starting","stopping"].includes(next.phase);
 $("environmentModeGrid").innerHTML=["normal","ros2"].map(mode=>{
  const active=mode===next.active_mode,available=payload.adapters?.[mode]?.available!==false;
  return `<button class="tab-item ${active?"active":""}" type="button" data-environment-mode="${mode}"
    aria-pressed="${active}" ${busy?"disabled":""} title="${escapeHtml(payload.adapters?.[mode]?.reason || "")}">
    ${ENV_LABELS[mode]} <span class="tab-count">${active?"当前":available?"可启用":"缺少依赖"}</span></button>`;
 }).join("");
 if (!envView.dirty) {
  const cfg={...(payload.config?.ros2 || {})};
  for(const [key,item] of Object.entries(payload.locked_config || {}))cfg[key]=item.value;
  [["ros2DomainId",cfg.domain_id ?? 0],["ros2Namespace",cfg.namespace || "/astrbotex"],
   ["ros2NodeName",cfg.node_name || "environment"],["ros2DiscoveryInterval",cfg.discovery_interval_sec ?? 1]]
   .forEach(([id,value])=>{$(id).value=value;});
  envView.revision=next.revision;envView.session=next.session_id;
  $("ros2IncludeHidden").checked=Boolean(cfg.include_hidden_topics);
 }
 for(const [id,key] of [["ros2DomainId","domain_id"],["ros2Namespace","namespace"],["ros2NodeName","node_name"]]) {
  const lock=payload.locked_config?.[key];$(id).disabled=Boolean(lock);$(id).title=lock?`由 ${lock.source} 固定`:"";
 }
 setText("environmentEffectiveConfig",`当前生效：${JSON.stringify(payload.effective_config || {})}`);
 setText("environmentConfigNote",envView.dirty?"有未保存修改；实时刷新不会覆盖草稿":"");
}
function emptyList(id,message) {$(id).innerHTML=`<div class="environment-empty">${escapeHtml(message)}</div>`;}
function nodeName(node) {return ((node.node_namespace || node.namespace || "/").replace(/\/$/,"")+"/"+(node.node_name || node.name || "")).replace(/\/+/g,"/");}
function renderEnvironmentGraph(data) {
 envView.graph=data;
 const nodes=data.nodes || [],topics=data.topics || [];
 setText("environmentGraphMeta",data.refreshed_at?`${topics.length} Topic · ${formatTime(data.refreshed_at)}`:"未扫描");
 setText("environmentNodeMeta",`${nodes.length} 节点`);
 if (topics.length) $("environmentGraphList").innerHTML=topics.map(topic=> {
  const details=[["发布者",topic.publishers],["订阅者",topic.subscriptions]].map(([label,items])=>
   `<div><b>${label} ${(items || []).length}</b>${(items || []).map(item=>`<p>${escapeHtml(nodeName(item))}
    <small>${escapeHtml(item.topic_type)} · ${escapeHtml(JSON.stringify(item.qos || {}))}</small></p>`).join("")}</div>`).join("");
  return `<details class="ros-graph-topic"><summary><b>${escapeHtml(topic.name)}</b>
   <span>${escapeHtml((topic.types || []).join(", "))}${topic.multiple_types?" · 多类型":""}</span>
   <small>发布 ${(topic.publishers || []).length} / 订阅 ${(topic.subscriptions || []).length}</small></summary>${details}</details>`;
 }).join(""); else emptyList("environmentGraphList",data.reason || "ROS 已启用，尚未发现 Topic");
 if (nodes.length) $("environmentNodeList").innerHTML=nodes.map(node=>`<div class="environment-list-item"><b>${escapeHtml(nodeName(node))}</b></div>`).join("");
 else emptyList("environmentNodeList",data.reason || "尚未发现节点");
 $("rosTopicSuggestions").innerHTML=topics.map(t=>`<option value="${escapeHtml(t.name)}">${escapeHtml((t.types || []).join(", "))}</option>`).join("");
}
function renderEnvironmentEndpoints(data) {
 for (const [key,id,label] of [["received","environmentReceived","接收"],["sent","environmentSent","发送"]]) {
  const rows=data[key] || [];
  setText(id+"Meta",`${rows.filter(r=>r.resource_created).length} 已创建 / ${rows.length} 请求`);
  if (!rows.length) {emptyList(id+"List",`暂无插件请求 ROS ${label}端口`);continue;}
  $(id+"List").innerHTML=rows.map(row=>endpointCardHtml(row,key)).join("");
 }
}
/* 一张端口卡片同时给出“资源状态”和“实际收数健康”，两者不再混为一谈。 */
function endpointCardHtml(row,key) {
 const publish=key==="sent";
 const healthKey=dataHealthKey(row),health=DATA_HEALTH_LABELS[healthKey];
 const diag=portDiagnostics(row);
 const peers=`可见对端 ${row.graph_peer_count ?? "未知"} / 匹配 ${row.matched_peer_count ?? "未知"}`;
 const counters=publish
  ?`入队 ${row.tx_queued ?? 0} / 本地发布 ${row.tx_published ?? 0}（仅证明本地 API 已调用，不证明对端已收到）`
  :`收到 ${row.rx_received ?? 0} / 已取 ${row.rx_consumed ?? 0}`;
 const ages=publish?"发送端口不做周期收数判定":`${agePhrase("最近接收",diag.receiveAge)} · ${agePhrase("最近消费",diag.consumeAge)}`;
 return `<article class="ros-endpoint" data-health="${healthKey}" data-direction="${publish?"sent":"received"}">
  <header class="ros-endpoint-head"><b>${escapeHtml(row.topic)}</b>
   <span class="ros-endpoint-type">${escapeHtml(row.message_type)}</span>
   ${healthChipHtml(healthKey)}</header>
  <div class="ros-endpoint-columns">
   <div><span class="ros-col-label">资源状态</span><b class="ros-col-value">${escapeHtml(rosLabel(row.state))}</b></div>
   <div><span class="ros-col-label">收数健康</span><b class="ros-col-value">${health.text}</b></div>
   <div><span class="ros-col-label">对端</span><b class="ros-col-value">${peers}</b></div>
  </div>
  <small>${escapeHtml(row.plugin_id)} / ${escapeHtml(row.port_id)} · ${publish?"发送":"接收"}</small>
  <small>${ages}${diag.monitoringAge===null||diag.monitoringAge===undefined?"":" · 观察窗口 "+(formatAge(diag.monitoringAge)||"未知")}</small>
  <small>${diagnosticsPolicyText(diag.policy)}${diag.monitoringActive===true?" · 监测中":diag.monitoringActive===false?" · 未监测":""}${diag.reasonCode?" · "+escapeHtml(diag.reasonCode):""}</small>
  <small>${counters} · 积压 ${row.queue_depth ?? 0} · 丢弃 ${row.queue_dropped ?? 0} · 过期 ${row.expired ?? 0} · 拒绝 ${row.rejected ?? 0}</small>
  <small>${row.rate_hz ?? 0} Hz · 队列 ${row.queue_bytes ?? 0} 字节 · 最近消息 ${formatTime(row.last_message_at)} · 超大消息 ${row.oversized ?? 0}</small>
  ${(row.qos_compatibility || []).filter(d=>d.state!=="compatible").map(d=>`<small>${escapeHtml(d.node)}：${escapeHtml(d.reason || d.state)}</small>`).join("")}
  ${row.message?`<p class="state-err">${escapeHtml(row.message)}</p>`:""}</article>`;
}
function refreshEnvironment() {
 if (envView.flight) return envView.flight;
 envView.flight=(async()=>{
  try {
   const payload=await apiJson("/api/v1/ex/environments");
   renderEnvironmentStatus(payload);
   const snapshot=envSnapshot();
   const results=await Promise.allSettled([apiJson("/api/v1/ex/environments/ros2/graph"),apiJson("/api/v1/ex/environments/ros2/endpoints"),fetchEnvironmentDiagnostics()]);
   if (results[0].status==="fulfilled" && sameEnvironment(snapshot,results[0].value.environment)) {
    const graph=results[0].value;if (snapshot.active_mode!=="ros2") graph.reason="普通环境：ROS 通信未启用";
    renderEnvironmentGraph(graph);
   } else emptyList("environmentGraphList","图状态更新中或读取失败");
   if (results[1].status==="fulfilled" && sameEnvironment(snapshot,results[1].value.environment)) renderEnvironmentEndpoints(results[1].value);
   else {emptyList("environmentReceivedList","端点状态暂不可用");emptyList("environmentSentList","端点状态暂不可用");}
   /* 诊断是独立缓存接口：它失败不得清空上面任何成功结果。 */
   if (results[2].status==="rejected") renderEnvironmentDiagnosticsUnavailable(results[2].reason);
   if(snapshot.active_mode==="ros2" && (!envView.interfaceAt || Date.now()-envView.interfaceAt>10000)) {
    envView.interfaceAt=Date.now();
    const interfaces=await apiJson("/api/v1/ex/environments/ros2/interfaces");
    if(sameEnvironment(envSnapshot(),interfaces.environment))renderRosInterfaces(interfaces);
   } else if(snapshot.active_mode!=="ros2")emptyList("rosInterfacePackages","启用 ROS 2 后查看接口包");
   return payload;
  } catch(error) {
   $("environmentError").hidden=false;$("environmentError").textContent="环境状态读取失败："+error.message;
   throw error;
  }
 })().finally(()=>{envView.flight=null;});
 return envView.flight;
}
function scheduleEnvironmentRefresh() {
 if (envView.timer) return;
 envView.timer=setTimeout(()=>{envView.timer=null;refreshEnvironment().catch(()=>{});refreshPluginRos().catch(()=>{});},200);
}
async function selectEnvironment(mode) {
 if (envView.switching) return;
 envView.switching=true;
 try {
  const snapshot=envSnapshot();
  const result=await apiJson("/api/v1/ex/environments/select",{method:"POST",body:JSON.stringify({mode,expected_revision:snapshot.revision,expected_session:snapshot.session_id})});
  showToast(result.accepted?"切换请求已提交，等待后端完成":"已处于当前环境");
 } finally {
  envView.switching=false;
  if (envView.flight) await envView.flight.catch(()=>{});
  await refreshEnvironment();
 }
}
async function saveRos2Config() {
 for (const el of $("ros2ConfigForm").querySelectorAll("input")) if (!el.reportValidity()) return;
 const result=await apiJson("/api/v1/ex/environments/ros2/config",{method:"POST",body:JSON.stringify({
  expected_revision:envView.revision,expected_session:envView.session,
  config:{domain_id:Number($("ros2DomainId").value),namespace:$("ros2Namespace").value,node_name:$("ros2NodeName").value,
  discovery_interval_sec:Number($("ros2DiscoveryInterval").value),include_hidden_topics:$("ros2IncludeHidden").checked}
 })});
 envView.dirty=false;showToast(result.restart_required?"已保存；切回普通环境再启用 ROS 2 后生效":"配置已保存");
 await refreshEnvironment();
}
function renderPluginRos(plugin) {
 const panel=$("pluginRosPanel"),config=plugin.ros2 || {};
 panel.hidden=!(config.ports || []).length;if(panel.hidden)return;
 let draft=envView.drafts.get(plugin.id);
 if(!draft || !draft.dirty) {
  draft={dirty:false,revision:config.revision,session:envSnapshot().session_id,bindings:structuredClone(config.bindings || {})};
  envView.drafts.set(plugin.id,draft);
 }
 const fields=$("pluginRosFields"),same=fields.dataset.pluginId===plugin.id;
 if(!draft.dirty || !same) {
  fields.dataset.pluginId=plugin.id;
  fields.innerHTML=config.ports.map(port=>{
   const binding=draft.bindings[port.id],qos=binding.qos;
   const select=(key,choices,value)=>`<select class="config-input" data-ros-field="${key}">${choices.map(v=>`<option ${v===value?"selected":""} value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("")}</select>`;
   return `<article class="ros-port" data-ros-port="${escapeHtml(port.id)}"><div><b>${escapeHtml(port.label || port.id)}</b>
    <small>${port.direction==="subscribe"?"接收":"发送"} · ${escapeHtml(port.id)}</small></div>
    <div class="environment-config-grid">
    <label class="connection-field"><span>启用端口</span><input type="checkbox" data-ros-field="enabled" ${binding.enabled?"checked":""}></label>
    <label class="connection-field"><span>Topic（可选择或手填）</span><input class="config-input" list="rosTopicSuggestions" data-ros-field="topic" value="${escapeHtml(binding.topic)}"></label>
    <label class="connection-field"><span>消息类型</span>${select("message_type",port.message_types,binding.message_type)}</label>
    <label class="connection-field"><span>Reliability</span>${select("reliability",["reliable","best_effort"],qos.reliability)}</label>
    <label class="connection-field"><span>Durability</span>${select("durability",port.requires_runtime_running?["volatile"]:["volatile","transient_local"],qos.durability)}</label>
    <label class="connection-field"><span>DDS 队列深度</span><input class="config-input" data-ros-field="depth" type="number" min="1" max="1024" value="${qos.depth}"></label>
    </div><p class="pubsub-help" data-ros-live="${escapeHtml(port.id)}"></p></article>`;
  }).join("");
 }
 renderPluginLive(plugin);
 setText("pluginRosNotice",draft.dirty?"有未保存 ROS 草稿；实时状态不会覆盖输入。":"ROS 配置独立保存；消息入队不代表远端已收到。");
}
function collectRosDraft() {
 const draft=envView.drafts.get($("pluginRosFields").dataset.pluginId);if(!draft)return;
 $("pluginRosFields").querySelectorAll("[data-ros-port]").forEach(row=>{
  const binding=draft.bindings[row.dataset.rosPort];
  row.querySelectorAll("[data-ros-field]").forEach(input=>{
   const key=input.dataset.rosField;
   if(["depth","reliability","durability"].includes(key))binding.qos[key]=key==="depth"?Number(input.value):input.value;
   else binding[key]=key==="enabled"?input.checked:input.value;
  });
 });
 draft.dirty=true;setText("pluginRosNotice","有未保存 ROS 草稿；实时状态不会覆盖输入。");
}
async function refreshPluginRos() {
 if(state.activePage!=="plugin" || !state.activePluginId || envView.pluginFlight)return;
 const id=state.activePluginId;
 envView.pluginFlight=apiJson(`/api/v1/ex/plugins/${encodeURIComponent(id)}/ros2`);
 try {
  const response=await envView.pluginFlight,plugin=state.plugins.find(p=>p.id===id);
  if(plugin){plugin.ros2=response.ros2;if(state.activePluginId===id)renderPluginRos(plugin);}
 } finally {envView.pluginFlight=null;}
}
async function savePluginRos() {
 const id=state.activePluginId,draft=envView.drafts.get(id);if(!draft)return;
 for(const input of $("pluginRosFields").querySelectorAll("input"))if(!input.reportValidity())return;
 const result=await apiJson(`/api/v1/ex/plugins/${encodeURIComponent(id)}/ros2`,{method:"POST",body:JSON.stringify({
  bindings:draft.bindings,expected_revision:draft.revision,expected_session:draft.session})});
 draft.dirty=false;const plugin=state.plugins.find(p=>p.id===id);
 if(plugin){plugin.ros2=result.ros2;renderPluginRos(plugin);}
 showToast(result.applied?"ROS 配置已保存并应用":"ROS 配置已保存；部分端口等待环境、插件或接口包");
 await refreshEnvironment();
}
function bindEnvironmentActions() {
 bindEnvironmentDiagnosticsActions();
 document.addEventListener("visibilitychange",()=>{if(!document.hidden)scheduleEnvironmentRefresh();});
 $("pluginDashboardBody").appendChild($("pluginRosPanel"));
 $("environmentModeGrid").addEventListener("click",event=>{
  const button=event.target.closest("[data-environment-mode]");
  if(button && !button.disabled)selectEnvironment(button.dataset.environmentMode).catch(e=>showToast(e.message,"error"));
 });
 $("environmentRefreshButton").addEventListener("click",event=>runAction(event.currentTarget,"刷新中",async()=>{
  await apiJson("/api/v1/ex/environments/ros2/discovery/refresh",{method:"POST",body:"{}"});await refreshEnvironment();
 }));
 $("ros2ConfigForm").addEventListener("input",()=>{envView.dirty=true;setText("environmentConfigNote","有未保存修改");});
 $("ros2ConfigSaveButton").addEventListener("click",event=>runAction(event.currentTarget,"保存中",saveRos2Config));
 $("ros2ConfigResetButton").addEventListener("click",()=>{envView.dirty=false;refreshEnvironment().catch(()=>{});});
 $("pluginRosFields").addEventListener("input",collectRosDraft);
 $("pluginRosSaveButton").addEventListener("click",event=>runAction(event.currentTarget,"保存中",savePluginRos));
 $("pluginRosResetButton").addEventListener("click",()=>{envView.drafts.delete(state.activePluginId);refreshPluginRos().catch(e=>showToast(e.message,"error"));});
 $("rosInterfaceCheckButton").addEventListener("click",event=>runAction(event.currentTarget,"检查中",async()=>{
  const result=await apiJson("/api/v1/ex/environments/ros2/interfaces/check",{method:"POST",body:JSON.stringify({message_type:$("rosInterfaceType").value.trim()})});
  $("rosInterfaceResult").textContent=result.available?"Python 消息类与 typesupport 均可用":`${result.code}：${result.message || "类型不可用"}`;
 }));
 setInterval(()=>{if(["environments","plugin"].includes(state.activePage)){refreshEnvironment().catch(()=>{});refreshPluginRos().catch(()=>{});}},1000);
}

function renderRosInterfaces(data) {
 const packages=data.packages || [];
 $("rosInterfacePackages").innerHTML=packages.map(pkg=>`<details class="ros-graph-topic"><summary>${escapeHtml(pkg.package)} <small>${escapeHtml(pkg.version || "版本未知")}</small></summary><small>${escapeHtml(pkg.prefix)}</small><p>${escapeHtml((pkg.message_types || []).join(", "))}</p></details>`).join("") || "未发现已安装的消息接口包";
 const missing=(data.discovered_types || []).filter(item=>!item.available);
 setText("rosInterfaceMissing",missing.length?"网络已发现、本机不可用："+missing.map(item=>`${item.message_type}（${item.code}）`).join("；"):"已发现类型的本地支持检查完成");
}

/* ============ ROS 诊断展示（B06） ======================================
 * 环境页与插件 ROS 面板只读展示 B04 诊断快照，三条硬约束：
 * 1) 资源状态（旧 state）与收数健康（data_health）分开呈现：ready 不代表正在收数；
 * 2) 只更新统计区域，绝不用 innerHTML 覆盖表单或草稿输入；
 * 3) 不切换环境、不启动 runtime；唯一的 POST 是诊断重新采集。
 */
const DATA_HEALTH_LABELS = {
 not_monitored:{text:"未监测",tone:"muted",mark:"○",hint:"mode off 或 publish 端口，不推断持续数据健康"},
 disabled:{text:"已禁用",tone:"muted",mark:"○",hint:"周期订阅已禁用"},
 paused:{text:"已暂停",tone:"muted",mark:"‖",hint:"运行时未运行或环境操作中，周期检查暂停"},
 unavailable:{text:"资源不可用",tone:"error",mark:"■",hint:"预期运行，但当前原生资源或接口不可用"},
 warming_up:{text:"启动宽限中",tone:"warning",mark:"▲",hint:"观察窗口内还没有回调，仍在启动宽限"},
 no_data:{text:"未收到数据",tone:"warning",mark:"▲",hint:"宽限期已到，本窗口从未收到回调"},
 receiving:{text:"正在收数",tone:"ok",mark:"●",hint:"当前绑定最近收到了回调"},
 stale:{text:"数据陈旧",tone:"warning",mark:"▲",hint:"本窗口收到过回调，但最新 age 已超过陈旧阈值"},
 unknown:{text:"证据不足",tone:"unknown",mark:"?",hint:"证据不完整或采集失败，不能判定健康"},
};
const DIAG_SUMMARY_LABELS = {
 ok:{text:"全部正常",tone:"ok",mark:"●"},
 warning:{text:"存在告警",tone:"warning",mark:"▲"},
 error:{text:"存在错误",tone:"error",mark:"■"},
 unknown:{text:"证据不足",tone:"unknown",mark:"?"},
 paused:{text:"监测已暂停",tone:"muted",mark:"‖"},
};
const DIAG_REFRESH_LABELS = {idle:"空闲",pending:"排队中",running:"采集中",completed:"已完成",failed:"采集失败",superseded:"已被更新请求取代"};
const DIAG_SEVERITY_LABELS = {
 debug:{key:"debug",text:"调试",tone:"muted",mark:"○"},
 info:{key:"info",text:"提示",tone:"ok",mark:"●"},
 warning:{key:"warning",text:"警告",tone:"warning",mark:"▲"},
 error:{key:"error",text:"错误",tone:"error",mark:"■"},
 critical:{key:"critical",text:"严重",tone:"error",mark:"■"},
};
const DIAG_TERMINAL_REFRESH = ["completed","failed","superseded","idle"];

function diagSpan(className,text) {
 const node=document.createElement("span");if(className)node.className=className;
 if(text!==undefined&&text!==null)node.textContent=String(text);return node;
}
function diagChip(tone,mark,text,key) {
 const node=document.createElement("span");node.className="env-health env-health-"+tone;
 if(key)node.dataset.health=key;
 const icon=document.createElement("i");icon.className="env-health-mark";icon.setAttribute("aria-hidden","true");icon.textContent=mark;
 node.appendChild(icon);node.appendChild(diagSpan("env-health-text",text));return node;
}
/* 后端算好的单调 age；前端绝不用墙钟 last_message_at 反推健康。 */
function formatAge(value) {
 if(value===null||value===undefined||value==="")return null;
 const number=Number(value);
 if(!Number.isFinite(number)||number<0)return null;
 if(number<1)return (Math.round(number*100)/100)+" 秒";
 if(number<60)return number.toFixed(1)+" 秒";
 if(number<3600)return Math.floor(number/60)+" 分 "+Math.round(number%60)+" 秒";
 return (number/3600).toFixed(1)+" 小时";
}
function agePhrase(label,value) {
 const text=formatAge(value);
 return label+" "+(text?text+"前":"未知");
}
function portDiagnostics(row) {
 const source=row&&typeof row==="object"?row:{};
 const nested=source.diagnostics&&typeof source.diagnostics==="object"?source.diagnostics:{};
 const pick=key=>nested[key]!==undefined?nested[key]:source[key];
 return {
  policy:nested.policy||source.diagnostics_policy||null,
  monitoringActive:pick("monitoring_active"),
  bindingReceived:pick("binding_received"),
  receiveAge:pick("receive_age_sec"),
  consumeAge:pick("consume_age_sec"),
  monitoringAge:pick("monitoring_age_sec"),
  reasonCode:pick("reason_code"),
 };
}
function dataHealthKey(row) {
 const raw=row&&typeof row==="object"?String(row.data_health||""):"";
 return DATA_HEALTH_LABELS[raw]?raw:"unknown";
}
function healthChipHtml(key) {
 const health=DATA_HEALTH_LABELS[key]||DATA_HEALTH_LABELS.unknown;
 return `<span class="env-health env-health-${health.tone}" data-health="${key}" title="${escapeHtml(health.hint)}">`+
  `<i class="env-health-mark" aria-hidden="true">${health.mark}</i><span class="env-health-text">${health.text}</span></span>`;
}
function diagnosticsPolicyText(policy) {
 if(!policy||typeof policy!=="object")return "监测策略未知";
 const mode=String(policy.mode||"").toLowerCase();
 if(!mode||mode==="off"||mode==="none")return "未启用周期监测";
 const grace=formatAge(policy.grace_sec),stale=formatAge(policy.stale_sec);
 return ["周期监测（"+mode+"）",grace?"宽限 "+grace:null,stale?"陈旧 "+stale:null].filter(Boolean).join(" · ");
}
function severityMeta(value) {
 const key=String(value||"warning").toLowerCase();
 return DIAG_SEVERITY_LABELS[key]||{key:"warning",text:"警告",tone:"warning",mark:"▲"};
}

/* -- 面板容器：B08 在 index.html 提供同名容器时直接复用，缺失时就地补齐 -- */
let diagnosticsParts=null;
function ensureDiagnosticsChild(parent,id,tag,className) {
 let node=document.getElementById(id);
 if(!node){node=document.createElement(tag);node.id=id;if(className)node.className=className;}
 if(node.parentNode!==parent)parent.appendChild(node);
 return node;
}
function ensureEnvironmentDiagnosticsParts() {
 if(diagnosticsParts && diagnosticsParts.root.isConnected)return diagnosticsParts;
 const page=document.getElementById("page-environments");
 if(!page)return null;
 let root=document.getElementById("environmentDiagnostics");
 if(!root){
  root=document.createElement("section");
  root.className="panel environment-diagnostics";
  root.id="environmentDiagnostics";
  const errorBox=document.getElementById("environmentError");
  const anchor=errorBox&&errorBox.closest?errorBox.closest(".panel"):null;
  if(anchor&&anchor.parentNode===page)page.insertBefore(root,anchor.nextSibling);
  else page.appendChild(root);
 }
 root.classList.add("environment-diagnostics");
 let head=root.querySelector(".panel-head");
 if(!head){head=document.createElement("div");head.className="panel-head";root.insertBefore(head,root.firstChild);}
 if(!head.querySelector(".panel-num")){const num=diagSpan("panel-num","06.A.1");head.insertBefore(num,head.firstChild);}
 if(!head.querySelector("h2")){const title=document.createElement("h2");title.textContent="诊断";head.appendChild(title);}
 const meta=ensureDiagnosticsChild(head,"environmentDiagnosticsMeta","span","panel-meta");
 const button=ensureDiagnosticsChild(head,"environmentDiagnosticsRefreshButton","button","btn btn-ghost btn-sm");
 if(!button.type)button.type="button";
 if(!button.textContent)button.textContent="重新检查";
 const status=ensureDiagnosticsChild(root,"environmentDiagnosticsStatus","div","env-diag-status");
 status.setAttribute("role","status");status.setAttribute("aria-live","polite");
 const summary=ensureDiagnosticsChild(root,"environmentDiagnosticsSummary","div","env-diag-summary");
 const issues=ensureDiagnosticsChild(root,"environmentDiagnosticsIssues","div","environment-list");
 const note=ensureDiagnosticsChild(root,"environmentDiagnosticsNote","p","pubsub-help");
 diagnosticsParts={root,head,meta,button,status,summary,issues,note};
 return diagnosticsParts;
}

/* -- 身份校验：旧代/旧会话/旧修订的响应一律不得覆盖当前画面 -- */
function diagnosticsIdentityMatches(identity) {
 const current=envSnapshot();
 if(!identity||typeof identity!=="object"||!current||typeof current!=="object")return false;
 if(identity.session_id!==current.session_id)return false;
 if(String(identity.generation)!==String(current.generation))return false;
 if(Number.isFinite(identity.revision)&&Number.isFinite(current.revision))return identity.revision===current.revision;
 return true;
}
function diagnosticsSummaryKey(snapshot) {
 let key=String(snapshot.summary||"unknown");
 if(!DIAG_SUMMARY_LABELS[key])key="unknown";
 if(snapshot.current!==true)key="unknown";
 else if(key==="ok"&&(snapshot.partial===true||(Array.isArray(snapshot.active_issues)&&snapshot.active_issues.length)))key="warning";
 return key;
}
function diagnosticsReason(snapshot,key,issues) {
 if(key==="paused")return typeof snapshot.reason==="string"&&snapshot.reason?snapshot.reason:"ROS 未启用或运行时未运行，周期检查已暂停；这不代表已通过检测。";
 if(key==="unknown"){
  if(snapshot.current!==true)return "尚未完成首次采集，无法判定健康。";
  if(snapshot.partial===true)return "采集不完整，缺失证据按未知处理。";
  return "证据不完整，无法判定健康。";
 }
 if(key==="ok")return "最近一次采集未发现异常。";
 const first=issues&&issues.length?issues[0]:null;
 if(first&&first.message)return String(first.message);
 return key==="error"?"存在活跃诊断错误。":"存在活跃诊断告警。";
}
function preflightSummary(report) {
 if(!report||typeof report!=="object")return "";
 const checks=Array.isArray(report.checks)?report.checks:[];
 if(!checks.length)return "";
 const counts={pass:0,warn:0,fail:0,unknown:0};
 checks.forEach(check=>{const status=String(check&&check.status||"unknown");if(counts[status]!==undefined)counts[status]+=1;});
 const age=formatAge(report.age_sec);
 const stale=report.current===false?"（缓存已过期"+(age?"，"+age+"前":"")+"）":"";
 return `${counts.pass} 通过 / ${counts.warn} 告警 / ${counts.fail} 失败 / ${counts.unknown} 未知${stale}`;
}
function renderDiagnosticsFacts(container,snapshot,issues,ports,key) {
 container.textContent="";
 const refresh=snapshot.refresh&&typeof snapshot.refresh==="object"?snapshot.refresh:{};
 const age=formatAge(snapshot.collection_age_sec);
 const refreshState=String(refresh.state||"idle");
 let refreshText=DIAG_REFRESH_LABELS[refreshState]||refreshState;
 if(refresh.error_code)refreshText+="（"+String(refresh.error_code)+"）";
 const facts=[
  ["结论",DIAG_SUMMARY_LABELS[key].text],
  ["采集",snapshot.current===true?"当前"+(age?" · "+age+"前":""):"尚未完成首次采集"],
  ["活跃问题",String(issues.length)],
  ["端口样本",String(ports.length)],
  ["刷新",refreshText],
 ];
 const preflight=preflightSummary(snapshot.preflight);
 if(preflight)facts.push(["预检",preflight]);
 facts.forEach(([label,value])=>{
  const cell=document.createElement("div");cell.className="env-diag-fact";
  cell.appendChild(diagSpan("env-diag-fact-label",label));
  const strong=document.createElement("b");strong.textContent=value;cell.appendChild(strong);
  container.appendChild(cell);
 });
}
function evidenceList(evidence) {
 if(!evidence||typeof evidence!=="object")return null;
 const keys=Object.keys(evidence).slice(0,24);
 if(!keys.length)return null;
 const list=document.createElement("dl");list.className="env-issue-evidence";
 keys.forEach(key=>{
  const term=document.createElement("dt");term.textContent=String(key);
  const value=document.createElement("dd");
  const raw=evidence[key];
  if(raw===null||raw===undefined)value.textContent="null";
  else if(typeof raw==="object")value.textContent=JSON.stringify(raw).slice(0,240);
  else value.textContent=String(raw);
  list.appendChild(term);list.appendChild(value);
 });
 return list;
}
function remediationText(remediation) {
 if(!remediation)return "";
 if(typeof remediation==="string")return remediation;
 if(typeof remediation==="object"){
  const parts=[];
  if(remediation.summary)parts.push(String(remediation.summary));
  if(Array.isArray(remediation.steps))remediation.steps.slice(0,6).forEach(step=>parts.push("· "+String(step)));
  return parts.join(" ");
 }
 return "";
}
function diagnosticsScopeText(scope) {
 if(!scope||typeof scope!=="object")return "";
 const bits=[];
 if(scope.topic)bits.push(String(scope.topic));
 if(scope.plugin_id||scope.port_id)bits.push([scope.plugin_id,scope.port_id].filter(Boolean).join(" / "));
 if(!bits.length&&scope.endpoint_id)bits.push(String(scope.endpoint_id));
 return bits.join(" · ");
}
function issueCard(issue) {
 const severity=severityMeta(issue&&issue.severity);
 const code=String(issue&&issue.code||"UNKNOWN");
 const card=document.createElement("article");
 card.className="env-issue";card.dataset.severity=severity.key;card.dataset.code=code;
 const head=document.createElement("header");head.className="env-issue-head";
 head.appendChild(diagChip(severity.tone,severity.mark,severity.text,severity.key));
 const title=document.createElement("b");title.className="env-issue-code";title.textContent=code;head.appendChild(title);
 const scopeText=diagnosticsScopeText(issue&&issue.scope);
 if(scopeText)head.appendChild(diagSpan("env-issue-scope",scopeText));
 if(issue&&issue.source)head.appendChild(diagSpan("env-issue-source",String(issue.source)));
 const action=document.createElement("button");
 action.type="button";action.className="btn btn-ghost btn-sm env-issue-log";
 action.dataset.diagnosticLog=code;action.textContent="查看日志";
 head.appendChild(action);card.appendChild(head);
 const message=document.createElement("p");message.className="env-issue-message";
 message.textContent=String(issue&&issue.message||"（后端未提供描述）");card.appendChild(message);
 const evidence=evidenceList(issue&&issue.evidence);
 if(evidence)card.appendChild(evidence);
 const remediation=remediationText(issue&&issue.remediation);
 if(remediation){const fix=document.createElement("p");fix.className="env-issue-fix";fix.textContent="建议："+remediation;card.appendChild(fix);}
 const meta=[];
 if(issue&&issue.occurrences)meta.push("出现 "+issue.occurrences+" 次");
 if(issue&&issue.first_seen_at)meta.push("首次 "+formatTime(issue.first_seen_at));
 if(issue&&issue.last_seen_at)meta.push("最近 "+formatTime(issue.last_seen_at));
 if(meta.length)card.appendChild(diagSpan("env-issue-meta",meta.join(" · ")));
 return card;
}
function diagnosticsEmptyText(key,snapshot) {
 if(key==="paused")return "监测已暂停："+(typeof snapshot.reason==="string"&&snapshot.reason?snapshot.reason:"ROS 未启用或运行时未运行，周期检查暂停；不是通过检测。");
 if(key==="unknown")return snapshot.current!==true?"尚未完成首次采集：暂无诊断结论。":"证据不完整：无法判定健康。";
 return "无活跃问题：最近一次采集未发现异常。";
}
function renderDiagnosticsIssues(container,issues,key,snapshot) {
 container.textContent="";
 if(!issues.length){
  const empty=document.createElement("div");empty.className="environment-empty";
  empty.textContent=diagnosticsEmptyText(key,snapshot);container.appendChild(empty);return;
 }
 issues.slice(0,64).forEach(issue=>container.appendChild(issueCard(issue)));
}
function renderEnvironmentDiagnostics(snapshot) {
 const parts=ensureEnvironmentDiagnosticsParts();
 if(!parts||!snapshot||typeof snapshot!=="object")return false;
 if(!diagnosticsIdentityMatches(snapshot.environment))return false;
 envView.diagnostics=snapshot;
 const issues=Array.isArray(snapshot.active_issues)?snapshot.active_issues:[];
 const ports=Array.isArray(snapshot.ports)?snapshot.ports:[];
 const key=diagnosticsSummaryKey(snapshot);
 const summary=DIAG_SUMMARY_LABELS[key];
 parts.status.dataset.summary=key;
 parts.status.dataset.current=snapshot.current===true?"1":"0";
 parts.status.dataset.partial=snapshot.partial===true?"1":"0";
 parts.status.dataset.stale="0";
 parts.status.textContent="";
 parts.status.appendChild(diagChip(summary.tone,summary.mark,summary.text,key));
 parts.status.appendChild(diagSpan("env-diag-reason",diagnosticsReason(snapshot,key,issues)));
 const age=formatAge(snapshot.collection_age_sec);
 parts.meta.textContent=[
  snapshot.current===true?"当前":"尚未采集",
  age?"采集 "+age+"前":null,
  snapshot.collected_at?formatTime(snapshot.collected_at):null,
 ].filter(Boolean).join(" · ");
 renderDiagnosticsFacts(parts.summary,snapshot,issues,ports,key);
 renderDiagnosticsIssues(parts.issues,issues,key,snapshot);
 const note=envView.diagnosticsNote;
 parts.note.textContent=note&&Date.now()-note.at<8000
  ?note.text
  :(snapshot.partial===true?"采集不完整：部分端口或预检证据缺失，缺失项按未知处理。":"诊断快照为后端缓存；本页只读，不改变环境与运行时。");
 if(state.activePage==="plugin")renderActivePluginLive();
 return true;
}
function setDiagnosticsNote(text) {
 envView.diagnosticsNote={text:String(text),at:Date.now()};
 const parts=ensureEnvironmentDiagnosticsParts();
 if(parts)parts.note.textContent=envView.diagnosticsNote.text;
 return envView.diagnosticsNote.text;
}
function renderEnvironmentDiagnosticsUnavailable(error) {
 const parts=ensureEnvironmentDiagnosticsParts();
 if(!parts)return;
 const status=error&&error.status;
 const message=error&&error.message?error.message:"诊断接口暂不可用";
 setDiagnosticsNote((status===401||status===403)
  ?"没有权限读取诊断数据（HTTP "+status+"）；请重新输入管理凭据。"
  :"诊断数据读取失败："+message+"。显示的是上一次成功采集的结果，不代表当前健康。");
 if(!envView.diagnostics){
  parts.status.dataset.summary="unknown";parts.status.dataset.current="0";parts.status.dataset.stale="1";
  parts.status.textContent="";
  parts.status.appendChild(diagChip("unknown","?","诊断数据不可用","unknown"));
  parts.status.appendChild(diagSpan("env-diag-reason","尚未成功读取诊断快照，不能判定健康。"));
  parts.meta.textContent="诊断不可用";
  return;
 }
 parts.status.dataset.stale="1";
}

/* -- 请求：GET 只读缓存；POST 只触发诊断重采集 -- */
async function diagnosticsRequest(path,options={}) {
 const headers=new Headers(options.headers||{});
 headers.set("Content-Type","application/json");
 const response=await managementFetch(path,{...options,headers});
 const data=await response.json().catch(()=>({}));
 return {status:response.status,ok:response.ok,data:data&&typeof data==="object"?data:{}};
}
async function fetchEnvironmentDiagnostics() {
 const seq=++envView.diagnosticsSeq;
 const {status,ok,data}=await diagnosticsRequest("/api/v1/ex/environments/diagnostics");
 if(seq!==envView.diagnosticsSeq)return null;
 if(!ok||data.ok===false){
  const error=new Error(data.message||data.error||("诊断接口返回 "+status));
  error.status=status;throw error;
 }
 const snapshot={...data};delete snapshot.ok;
 if(!diagnosticsIdentityMatches(snapshot.environment))return null;
 renderEnvironmentDiagnostics(snapshot);
 return snapshot;
}
async function requestEnvironmentDiagnosticsRefresh() {
 const parts=ensureEnvironmentDiagnosticsParts();
 if(!parts)return;
 const snapshot=envSnapshot();
 if(!snapshot.session_id){
  setDiagnosticsNote("环境状态尚未读取，暂不能重新检查；请等待环境状态加载完成。");return;
 }
 if(envView.diagnosticsRefreshFlight)return envView.diagnosticsRefreshFlight;
 const button=parts.button;
 envView.diagnosticsRefreshFlight=(async()=>{
  button.disabled=true;button.classList.add("busy");
  const setNote=text=>setDiagnosticsNote(text);
  try{
   const {status,data}=await diagnosticsRequest("/api/v1/ex/environments/diagnostics/refresh",{
    method:"POST",body:JSON.stringify({expected_session:snapshot.session_id,expected_revision:snapshot.revision})});
   if(status===202&&data.accepted){
    setNote("已提交重新检查请求"+(data.refresh_id?"（"+String(data.refresh_id).slice(0,8)+"）":"")+"；后台采集完成后自动更新，不会改变环境或运行时。");
    await followDiagnosticsRefresh(data.refresh_id);
    return;
   }
   if(status===409){
    await refreshEnvironment().catch(()=>{});
    setNote("环境在请求期间发生变化（会话/代次/修订不一致），已重新读取权威状态；确认后可以再次点击重新检查。");
    return;
   }
   if(status===400){
    await refreshEnvironment().catch(()=>{});
    setNote("请求缺少会话或修订号，已重新读取环境状态；请再次点击重新检查。");
    return;
   }
   if(status===401||status===403){setNote("没有权限执行重新检查（HTTP "+status+"）。");return;}
   if(status===503){setNote("服务正在关闭或诊断服务不可用（HTTP 503），请稍后重试。");return;}
   setNote("重新检查失败："+(data.message||data.error||("HTTP "+status))+"。");
   showToast(envView.diagnosticsNote.text,"error");
  }catch(error){
   setNote("重新检查失败："+error.message+"。");
   showToast(envView.diagnosticsNote.text,"error");
  }finally{
   button.disabled=false;button.classList.remove("busy");
   envView.diagnosticsRefreshFlight=null;
  }
 })();
 return envView.diagnosticsRefreshFlight;
}
async function followDiagnosticsRefresh(refreshId) {
 for(let attempt=0;attempt<10;attempt+=1){
  await new Promise(resolve=>setTimeout(resolve,400));
  let snapshot=null;
  try{snapshot=await fetchEnvironmentDiagnostics();}catch(error){return;}
  if(!snapshot)return;
  const refresh=snapshot.refresh&&typeof snapshot.refresh==="object"?snapshot.refresh:{};
  if(refreshId&&refresh.id&&refresh.id!==refreshId)return;
  if(DIAG_TERMINAL_REFRESH.includes(String(refresh.state)))return;
 }
}
/* 只导航并设置日志筛选：不切换环境、不启动 runtime。 */
function openDiagnosticLog(code) {
 const text=String(code||"").trim();
 try{
  if(typeof switchPage==="function")switchPage("logs");
  else window.location.hash="#/logs";
 }catch(error){/* 导航失败不影响筛选 */}
 const logs=window.EXLogs;
 if(logs&&typeof logs.setFilter==="function")logs.setFilter({sources:[],text});
 else showToast("日志组件尚未加载，请在日志页手动筛选 "+text,"error");
 return text;
}
function bindEnvironmentDiagnosticsActions() {
 const parts=ensureEnvironmentDiagnosticsParts();
 if(!parts)return;
 if(parts.button.dataset.bound==="1")return;
 parts.button.dataset.bound="1";
 parts.button.addEventListener("click",()=>{requestEnvironmentDiagnosticsRefresh().catch(error=>showToast(error.message,"error"));});
 parts.issues.addEventListener("click",event=>{
  const button=event.target&&event.target.closest?event.target.closest("[data-diagnostic-log]"):null;
  if(button)openDiagnosticLog(button.dataset.diagnosticLog);
 });
}

/* -- 插件 ROS 面板：端口级资源状态 + 收数健康 -- */
function diagnosticsPortSample(pluginId,portId) {
 const snapshot=envView.diagnostics;
 if(!snapshot||!Array.isArray(snapshot.ports))return null;
 const generation=snapshot.environment?snapshot.environment.generation:null;
 return snapshot.ports.find(sample=>{
  if(!sample||typeof sample!=="object")return false;
  if(String(sample.plugin_id||"")!==String(pluginId))return false;
  if(String(sample.port_id||"")!==String(portId))return false;
  if(generation!==null&&generation!==undefined&&sample.environment_generation!==undefined&&String(sample.environment_generation)!==String(generation))return false;
  return true;
 })||null;
}
function pluginLiveNode(plugin,port,portId) {
 const wrapper=document.createElement("span");wrapper.className="env-plugin-live";
 const config=plugin.ros2||{};
 const portMeta=port||(config.ports||[]).find(item=>item.id===portId)||null;
 const endpoint=(config.endpoints||[]).find(item=>item.port_id===portId)||null;
 const sample=diagnosticsPortSample(plugin.id,portId);
 if(!endpoint&&!sample){
  wrapper.appendChild(document.createTextNode("端点未创建（插件未启用或尚未申请句柄）"));
  return wrapper;
 }
 const healthKey=dataHealthKey(sample||endpoint);
 const health=DATA_HEALTH_LABELS[healthKey];
 wrapper.appendChild(diagChip(health.tone,health.mark,health.text,healthKey));
 const publish=(portMeta&&portMeta.direction==="publish")||(endpoint&&endpoint.direction==="publish");
 const diag=portDiagnostics(sample||endpoint||{});
 const bits=[];
 if(endpoint)bits.push("资源："+rosLabel(endpoint.state));
 if(publish)bits.push("本地发布 "+(endpoint?endpoint.tx_published??0:0)+" 条（不证明对端已收到）");
 else{
  bits.push(agePhrase("最近接收",diag.receiveAge));
  bits.push(agePhrase("最近消费",diag.consumeAge));
 }
 bits.push(diagnosticsPolicyText(diag.policy));
 if(diag.monitoringActive===false)bits.push("未监测");
 wrapper.appendChild(document.createTextNode(" "+bits.join(" · ")));
 return wrapper;
}
function renderPluginLive(plugin) {
 const fields=document.getElementById("pluginRosFields");
 if(!fields||!plugin)return;
 fields.querySelectorAll("[data-ros-live]").forEach(el=>{
  el.textContent="";
  el.appendChild(pluginLiveNode(plugin,null,el.dataset.rosLive));
 });
}
function renderActivePluginLive() {
 const plugins=Array.isArray(state.plugins)?state.plugins:[];
 const plugin=plugins.find(item=>item.id===state.activePluginId);
 if(plugin)renderPluginLive(plugin);
}
