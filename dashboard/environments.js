/* Navigation reads state; only explicit mode clicks change the environment. */
const envView = {flight:null,timer:null,graph:null,switching:false,dirty:false,revision:null,session:null,drafts:new Map(),pluginFlight:null};
const ENV_LABELS = {normal:"普通环境",ros2:"ROS 2"};
const ROS_STATES = {ready:"收发端点就绪",waiting_peer:"已创建 / 等待通信",waiting_environment:"等待 ROS 环境",
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
  $(id+"List").innerHTML=rows.map(row=>`<article class="ros-endpoint">
   <b>${escapeHtml(row.topic)}</b><span>${escapeHtml(row.message_type)}</span>
   <small>${escapeHtml(row.plugin_id)} / ${escapeHtml(row.port_id)} · ${escapeHtml(rosLabel(row.state))}</small>
   <small>${key==="received"?`收到 ${row.rx_received} / 已取 ${row.rx_consumed}`:`入队 ${row.tx_queued} / 已发布 ${row.tx_published}`}
   · 积压 ${row.queue_depth} · 丢弃 ${row.queue_dropped} · 过期 ${row.expired} · 拒绝 ${row.rejected}</small>
   <small>可见对端 ${row.graph_peer_count ?? "未知"} / 匹配 ${row.matched_peer_count ?? "未知"} · ${row.rate_hz ?? 0} Hz · 队列 ${row.queue_bytes ?? 0} 字节</small>
   <small>最近消息 ${formatTime(row.last_message_at)} · 超大消息 ${row.oversized ?? 0}</small>
   ${(row.qos_compatibility || []).filter(d=>d.state!=="compatible").map(d=>`<small>${escapeHtml(d.node)}：${escapeHtml(d.reason || d.state)}</small>`).join("")}
   ${row.message?`<p class="state-err">${escapeHtml(row.message)}</p>`:""}</article>`).join("");
 }
}
function refreshEnvironment() {
 if (envView.flight) return envView.flight;
 envView.flight=(async()=>{
  try {
   const payload=await apiJson("/api/v1/ex/environments");
   renderEnvironmentStatus(payload);
   const snapshot=envSnapshot();
   const results=await Promise.allSettled([apiJson("/api/v1/ex/environments/ros2/graph"),apiJson("/api/v1/ex/environments/ros2/endpoints")]);
   if (results[0].status==="fulfilled" && sameEnvironment(snapshot,results[0].value.environment)) {
    const graph=results[0].value;if (snapshot.active_mode!=="ros2") graph.reason="普通环境：ROS 通信未启用";
    renderEnvironmentGraph(graph);
   } else emptyList("environmentGraphList","图状态更新中或读取失败");
   if (results[1].status==="fulfilled" && sameEnvironment(snapshot,results[1].value.environment)) renderEnvironmentEndpoints(results[1].value);
   else {emptyList("environmentReceivedList","端点状态暂不可用");emptyList("environmentSentList","端点状态暂不可用");}
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
 fields.querySelectorAll("[data-ros-live]").forEach(el=>{
  const endpoint=(config.endpoints || []).find(e=>e.port_id===el.dataset.rosLive);
  el.textContent=endpoint?rosLabel(endpoint.state)+(endpoint.message?"："+endpoint.message:""):"端点未创建（插件未启用或尚未申请句柄）";
 });
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
