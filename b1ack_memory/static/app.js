const base = location.pathname.replace(/\/ui\/?$/, "");
function dashboardBridge() {
  try {
    return window.parent !== window ? window.parent.__B1ACK_MEMORY_DASHBOARD_BRIDGE__ : null;
  } catch (_) {
    return null;
  }
}
const bridge = dashboardBridge();
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
let settings = {};
let statusData = {};
let libraryTab = "memories";
let reviewTab = "decision";
let systemTab = "dream";
let currentProject = null;
let noticeTimer = null;

const workspaceMeta = {
  overview: ["概览", "只看真正需要处理的事项与系统健康"],
  library: ["记忆库", "长期事实、项目工作记忆、实体与历史版本"],
  projects: ["项目", "围绕项目摘要、当前状态、决定与开放问题工作"],
  reviews: ["审核", "高风险决定与日常整理建议分流处理"],
  system: ["系统", "Dream、召回、模型、存储和高级设置"],
};
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const when = (value) => { if (!value) return "—"; const d = typeof value === "number" ? new Date(value) : new Date(String(value).replace(" ", "T")); return Number.isNaN(d.valueOf()) ? esc(value) : d.toLocaleString("zh-CN", {hour12:false}); };
const badge = (text, tone="") => `<span class="badge ${tone}">${esc(text)}</span>`;
const empty = (text) => `<div class="empty">${esc(text)}</div>`;

async function api(path, options={}) {
  const headers = {...(options.headers || {})};
  if (options.body !== undefined && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const request = Object.keys(headers).length ? {...options, headers} : options;
  if (bridge) return bridge.request(path, request);
  const response = await fetch(base + path, request);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return (response.headers.get("content-type") || "").includes("json") ? response.json() : response.text();
}
function notice(message, isError=false) {
  const node = $("#notice"); clearTimeout(noticeTimer); node.textContent = message; node.className = isError ? "error" : ""; node.style.display = "block";
  noticeTimer = setTimeout(() => node.style.display = "none", 4200);
}
function showLoadFailure(error) {
  const message = error?.message || String(error);
  let panel = $("#load-error");
  if (!panel) {
    panel = document.createElement("article");
    panel.id = "load-error";
    $("#overview").prepend(panel);
  }
  Object.assign(panel.style, {border: "1px solid #713c43", background: "#241419", borderRadius: "16px", padding: "15px 18px", marginBottom: "16px", display: "flex", alignItems: "center", justifyContent: "space-between", gap: "16px"});
  panel.innerHTML = `<div><b>数据没有加载完成</b><p style="margin:5px 0 0;color:#e9b9be;line-height:1.45;word-break:break-word">${esc(message)}</p></div><button class="quiet small" type="button">重试</button>`;
  panel.querySelector("button").addEventListener("click", () => loadAll());
}
function clearLoadFailure() { $("#load-error")?.remove(); }
async function mutate(path, body={}, method="POST", reload=true) {
  const result = await api(path, {method, body:JSON.stringify(body)});
  if (reload) await loadAll();
  return result;
}
function nav(workspace, tab) {
  $$("[data-workspace]").forEach(b => b.classList.toggle("active", b.dataset.workspace === workspace));
  $$(".workspace").forEach(p => p.classList.toggle("active", p.id === workspace));
  const [title, subtitle] = workspaceMeta[workspace]; $("#page-title").textContent = title; $("#page-subtitle").textContent = subtitle;
  if (workspace === "system" && tab) showSystem(tab);
  if (workspace === "library" && tab) showLibrary(tab);
}
function showSystem(tab) { systemTab = tab; $$('[data-system-tab]').forEach(b=>b.classList.toggle('active',b.dataset.systemTab===tab)); $$('.system-pane').forEach(p=>p.classList.toggle('active',p.id===`system-${tab}`)); }
function showLibrary(tab) { libraryTab = tab; $$('[data-library-tab]').forEach(b=>b.classList.toggle('active',b.dataset.libraryTab===tab)); $("#library-filter").innerHTML = tab === "memories" ? '<option value="active">当前</option><option value="trashed">回收站</option><option value="superseded">历史</option><option value="all">全部</option>' : tab === "work" ? '<option value="current">进行中</option><option value="suggested">待确认</option><option value="archived">归档</option><option value="all">全部</option>' : '<option value="active">活跃</option><option value="paused">暂停</option><option value="archived">归档</option><option value="all">全部</option>'; loadLibrary().catch(fail); }
function fail(error) { console.error(error); notice(error.message || String(error), true); }

async function loadAll() {
  try {
    const bootstrap = await api("/bootstrap"); statusData = bootstrap.status;
    const loaders = [
      ["概览", loadOverview], ["设置", loadSettings], ["记忆库", loadLibrary], ["项目", loadProjects],
      ["审核", loadReviews], ["Dream", loadDreams], ["召回轨迹", loadTraces], ["模型调用", loadCalls],
      ["存储", loadStorage], ["隔离会话", loadIngestionIssues], ["备份", loadBackups],
    ];
    const results = await Promise.allSettled(loaders.map(([, loader]) => loader()));
    const failures = results.flatMap((result, index) => result.status === "rejected" ? [`${loaders[index][0]}：${result.reason?.message || String(result.reason)}`] : []);
    if (failures.length) showLoadFailure(new Error(`部分数据未加载：${failures.join("；")}`));
    else clearLoadFailure();
  } catch (error) { fail(error); showLoadFailure(error); }
}

async function loadOverview() {
  const s = statusData; const c = s.counts || {};
  const metrics = [["长期记忆",c.active_memories,"durable"],["活跃项目",c.active_projects,"projects"],["工作项",c.active_work_items,"working"],["开放审核",c.open_reviews,"reviews"],["存储健康",s.storage?.ok?1:0,s.storage?.ok?"healthy":"attention"],["最近备份",(await api('/backups')).length,"archives"]];
  $("#metrics").innerHTML = metrics.map(([label,value,note])=>`<div class="metric"><span>${label}</span><b>${value ?? 0}</b><small>${note}</small></div>`).join("");
  const attention=[]; if(c.open_reviews) attention.push(`${c.open_reviews} 项审核需要处理`); if(c.suggested_work_items) attention.push(`${c.suggested_work_items} 条工作记忆待确认归属`); if(s.audit_due) attention.push("全池审计已到期"); if(!s.storage?.ok) attention.push("存储投影需要维护");
  const box=$("#needs-attention"); box.hidden=!attention.length; box.innerHTML=attention.length?`<h2>需要处理</h2><ul>${attention.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:"";
  $("#health").innerHTML = [["数据库",s.database?.integrity],["vault 投影",s.storage?.ok?"正常":`${s.storage?.projection_pending||0} 个任务`],["Dream 模型",s.llm?.configured?`${s.llm.model} · 已配置`:"未配置"],["下一次 Dream",when(s.next_dream)]].map(([a,b])=>`<div class="health-row"><span>${esc(a)}</span><b>${esc(b)}</b></div>`).join("");
  const runs=await api('/dream-runs?limit=5'); $("#recent-activity").innerHTML=runs.map(r=>`<div class="list-row"><div class="list-main"><div class="list-title">Dream · ${esc(r.status)}</div><div class="list-meta">${when(r.started_at)} · admit ${r.admitted_count||0} · work ${r.work_item_count||0} · review ${r.review_count||0}</div></div>${badge(r.error?'blocked':'complete',r.error?'danger':'good')}</div>`).join('')||empty('还没有 Dream 记录');
}

async function loadLibrary() {
  const query=($("#library-search")?.value||"").toLowerCase(); const filter=$("#library-filter")?.value||"active"; let rows=[];
  if(libraryTab==='memories'||libraryTab==='history') {
    const statuses=libraryTab==='history'?['superseded','trashed']:(filter==='all'?['active','superseded','trashed']:[filter]);
    rows=(await Promise.all(statuses.map(s=>api(`/memories?status=${s}`)))).flat();
  } else if(libraryTab==='work') {
    if(filter==='current') rows=[...(await api('/work-items?status=active')),...(await api('/work-items?status=suggested'))]; else if(filter==='all') rows=await api('/work-items'); else rows=await api(`/work-items?status=${filter}`);
  } else { rows=await api(`/subjects${filter==='all'?'':`?status=${filter}`}`); }
  rows=rows.filter(r=>JSON.stringify(r).toLowerCase().includes(query));
  $("#library-list").innerHTML=rows.map(r=>{
    if(libraryTab==='memories'||libraryTab==='history') return `<div class="list-row" data-open="memory" data-id="${r.id}"><div class="list-main"><div class="list-title">${esc(r.content)}</div><div class="list-meta">${badge(r.kind)} ${badge(r.temporal_status,r.temporal_status==='current'?'good':'warn')} ${r.subjects?.map(s=>badge(s.name)).join('')||''} · ${when(r.updated_at)}</div></div>${r.open_review_count?badge(`${r.open_review_count} 审核`,'danger'):''}</div>`;
    if(libraryTab==='work') return `<div class="list-row" data-open="work" data-id="${r.id}"><div class="list-main"><div class="list-title">${esc(r.content)}</div><div class="list-meta">${badge(r.item_type)} ${badge(r.status,r.status==='active'?'good':r.status==='suggested'?'warn':'')} ${r.subject_name?badge(r.subject_name):''} · ${when(r.updated_at)}</div></div>${r.confirmed?badge('用户确认','good'):badge('未确认','warn')}</div>`;
    return `<div class="list-row" data-open="subject" data-id="${r.id}"><div class="list-main"><div class="list-title">${esc(r.name)}</div><div class="list-meta">${badge(r.subject_type)} ${badge(r.status,r.status==='active'?'good':'warn')} · ${r.link_count||0} 个关联 · ${r.work_item_count||0} 个工作项</div></div><span>›</span></div>`;
  }).join('')||empty('当前筛选没有条目');
}

async function loadProjects() {
  const projects=await api('/projects'); const q=($("#project-search")?.value||'').toLowerCase(); const visible=projects.filter(p=>p.name.toLowerCase().includes(q));
  $("#project-list").innerHTML=visible.map(p=>`<button data-project="${p.id}" class="${p.id===currentProject?'active':''}">${esc(p.name)}<small>${esc(p.status)} · ${p.work_item_count||0} 工作项</small></button>`).join('')||empty('还没有项目');
  if(currentProject && projects.some(p=>p.id===currentProject)) await renderProject(currentProject); else if(visible.length && !currentProject){currentProject=visible[0].id;await renderProject(currentProject);}
}
async function renderProject(id) {
  currentProject=id; $$('[data-project]').forEach(b=>b.classList.toggle('active',b.dataset.project===id)); const p=await api(`/projects/${id}`); const summary=p.summary;
  const grouped={decision:[],current_state:[],open_question:[],milestone:[],proposal:[]}; (p.work_items||[]).forEach(w=>(grouped[w.item_type]||grouped.proposal).push(w));
  const chips=(items)=>items.filter(i=>i.status==='active').map(i=>`<div class="work-chip" data-open="work" data-id="${i.id}">${esc(i.content)}</div>`).join('')||'<div class="muted">暂无</div>';
  $("#project-workbench").innerHTML=`<div class="project-hero"><div><span class="eyebrow">${esc(p.status.toUpperCase())}</span><h2>${esc(p.name)}</h2><p class="muted">${esc(p.description||'未填写项目说明')}</p></div><div class="row"><button class="quiet small" data-context="${p.id}">上下文预览</button><button class="quiet small" data-summary="${p.id}">重建摘要</button><a class="button quiet small" href="${base}/projects/${p.id}/export" target="_blank">导出</a><button class="quiet small" data-edit-project="${p.id}">设置</button></div></div><article class="panel"><div class="panel-head"><div><span>SUMMARY</span><h2>项目摘要</h2></div>${summary?badge(summary.mode==='manual_override'?'手工锁定':'自动','good'):badge('未生成','warn')}</div><p class="muted">${summary?esc(summary.content):'尚无可追溯摘要；召回会回退到原子记忆和工作项。'}</p></article><div class="work-columns"><div class="work-column"><h3>当前状态</h3>${chips(grouped.current_state)}</div><div class="work-column"><h3>已确认决定</h3>${chips(grouped.decision)}</div><div class="work-column"><h3>开放问题</h3>${chips(grouped.open_question)}</div></div><details class="panel details"><summary>提案、里程碑与历史工作项</summary>${[...grouped.proposal,...grouped.milestone,...p.work_items.filter(w=>!['active'].includes(w.status))].map(w=>`<div class="work-chip" data-open="work" data-id="${w.id}">${badge(w.item_type)} ${badge(w.status)} ${esc(w.content)}</div>`).join('')||empty('暂无')}</details>`;
}

async function loadReviews() {
  const [decisions,suggestions]=await Promise.all([api('/reviews?status=open&queue=decision'),api('/reviews?status=open&queue=suggestion')]); $("#decision-count").textContent=decisions.length; $("#suggestion-count").textContent=suggestions.length;
  const rows=reviewTab==='resolved'?await api('/reviews?status=resolved'):reviewTab==='suggestion'?suggestions:decisions;
  $$('[data-review-tab]').forEach(b=>b.classList.toggle('active',b.dataset.reviewTab===reviewTab));
  $("#review-list").innerHTML=rows.map(r=>`<div class="list-row" data-open="review" data-id="${r.id}"><div class="list-main"><div class="list-title">${esc(r.proposed_content||r.reason)}</div><div class="list-meta">${badge(r.issue_type,r.queue==='decision'?'danger':'warn')} ${badge(r.proposed_action)} · ${Math.round((r.confidence||0)*100)}% · ${when(r.created_at)}</div></div><span>›</span></div>`).join('')||empty(reviewTab==='decision'?'没有需要决定的高风险事项':'当前队列为空');
}

async function loadDreams(){const rows=await api('/dream-runs?limit=80');$("#dream-list").innerHTML=rows.map(r=>`<div class="list-row"><div class="list-main"><div class="list-title">${when(r.started_at)} · ${esc(r.status)}</div><div class="list-meta">输入 ${r.input_count||0} · admit ${r.admitted_count||0} · observe ${r.observed_count||0} · discard ${r.discarded_count||0} · 工作项 ${r.work_item_count||0} · 归属 ${r.assignment_count||0} · 摘要 ${r.summary_count||0} · 投影 ${r.projection_count||0}</div>${r.error?`<div class="list-meta">${esc(r.error)}</div>`:''}</div>${badge(r.blocked_count?`${r.blocked_count} 阻塞`:'完成',r.blocked_count?'danger':'good')}</div>`).join('')||empty('暂无 Dream 日志');}
async function loadTraces(){const rows=await api('/recall-traces?limit=200');$("#trace-list").innerHTML=`<table><thead><tr><th>时间</th><th>查询</th><th>来源</th><th>项目</th><th>注入</th><th>分数</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${when(r.created_at)}</td><td>${esc(r.query_text)}</td><td>${esc(r.source)}</td><td>${r.project_id?`${esc(r.project_reason||'')} · ${Math.round((r.project_confidence||0)*100)}%`:'未识别'}</td><td>${r.injected?'是':'否'}</td><td>${Number(r.final_score||0).toFixed(3)}</td></tr>`).join('')}</tbody></table>`;}
async function loadCalls(){const rows=await api('/model-calls?limit=40');$("#call-list").innerHTML=rows.map(r=>`<div class="list-row"><div class="list-main"><div class="list-title">${esc(r.phase)} · ${esc(r.model)}</div><div class="list-meta">${when(r.created_at)} · input ${r.input_tokens||0} · output ${r.output_tokens||0}</div></div>${r.error?badge('失败','danger'):badge('成功','good')}</div>`).join('')||empty('暂无模型调用');}
async function loadStorage(){const s=await api('/maintenance/storage');const permissions=(s.permissions||[]).map(p=>[p.path.split(/[\\/]/).pop()||p.path,p.managed_by==='windows_acl'?'Windows ACL':(p.safe?`安全 ${p.mode}`:`需修复 ${p.mode}`)]);$("#storage-health").innerHTML=[["SQLite integrity",s.database_integrity],["待处理投影",s.projection_pending],["已校验 vault 文件",s.vault_files_checked],["状态",s.ok?'正常':(s.errors||[]).join('；')],...permissions].map(([a,b])=>`<div class="health-row"><span>${esc(a)}</span><b>${esc(b)}</b></div>`).join('');}
async function loadIngestionIssues(){const rows=await api('/maintenance/ingestion-issues');$("#ingestion-issues").innerHTML=rows.map(r=>`<div class="list-row"><div class="list-main"><div class="list-title">${esc(r.session_id||'未知会话')} · ${esc(r.ingest_status)}</div><div class="list-meta">${when(r.observed_at)} · 尝试 ${r.ingest_attempts} 次 · 游标 ${r.ingest_cursor}/${r.content_length}<br>${esc(r.last_ingest_error||'')}</div></div><button class="quiet small" data-retry-ingestion="${r.id}">重新处理</button></div>`).join('')||empty('没有隔离或待重试条目');}
async function loadBackups(){const rows=await api('/backups');$("#backup-list").innerHTML=rows.map(r=>`<div class="list-row"><div class="list-main"><div class="list-title">${esc(r.name)}</div><div class="list-meta">${(r.bytes/1024/1024).toFixed(2)} MB · ${when(r.modified*1000)}</div></div><button class="quiet small" data-preview-backup="${esc(r.name)}">恢复预演</button></div>`).join('')||empty('还没有备份');}

async function loadSettings(){settings=await api('/settings');for(const section of ['general','llm','embedding','dream','recall','retention']){const form=$(`#${section}-form`);if(!form)continue;for(const [key,value] of Object.entries(settings[section]||{})){const input=form.elements[key];if(!input)continue;if(input.type==='checkbox')input.checked=Boolean(value);else input.value=value??'';}}$("#llm-key").textContent=`Key：${settings.secrets.llm_api_key.configured?'已配置':'未配置'}`;$("#embedding-key").textContent=`Key：${settings.secrets.embedding_api_key.configured?'已配置':'未配置'}`;$("#detected-timezone").textContent=`浏览器时区：${Intl.DateTimeFormat().resolvedOptions().timeZone||'system'}`;}

function openDrawer(html){$("#drawer-content").innerHTML=html;$("#drawer").classList.add('open');$("#drawer").setAttribute('aria-hidden','false');$("#drawer-scrim").hidden=false;}
function closeDrawer(){$("#drawer").classList.remove('open');$("#drawer").setAttribute('aria-hidden','true');$("#drawer-scrim").hidden=true;}
async function detail(type,id){
  if(type==='memory'){const rows=(await Promise.all(['active','superseded','trashed'].map(s=>api(`/memories?status=${s}`)))).flat();const m=rows.find(x=>x.id===id);if(!m)return;openDrawer(`<span class="eyebrow">LONG-TERM MEMORY</span><h2>${esc(m.content)}</h2><div class="list-meta">${badge(m.kind)} ${badge(m.status)} ${badge(m.temporal_status)}</div><div class="detail-section"><h3>有效时间</h3><p>${esc(m.valid_from||'未指定')} → ${esc(m.valid_to||'当前')}</p><p class="muted">${esc(m.temporal_reason||'无变更原因')}</p></div><div class="detail-section"><h3>项目与实体</h3><p>${m.subjects?.map(s=>badge(s.name)).join(' ')||'全局记忆'}</p></div><div class="detail-section"><h3>来源与版本</h3><p>来源：${esc(m.origin_label||m.origin)} · 修订 ${m.revision_count||0} · ${when(m.updated_at)}</p></div><div class="detail-section row">${m.status==='active'?`<button data-edit-memory="${m.id}">编辑</button><button class="danger" data-trash-memory="${m.id}">移入回收站</button>`:m.status==='trashed'?`<button data-restore-memory="${m.id}">恢复</button><button class="danger" data-purge-memory="${m.id}">永久删除</button>`:`<button data-restore-memory="${m.id}">恢复为当前</button>`}</div>`);}
  if(type==='work'){const w=(await api('/work-items')).find(x=>x.id===id);if(!w)return;openDrawer(`<span class="eyebrow">WORKING MEMORY</span><h2>${esc(w.content)}</h2><div class="list-meta">${badge(w.item_type)} ${badge(w.status)} ${w.subject_name?badge(w.subject_name):badge('未归属','warn')}</div><div class="detail-section"><h3>用户证据</h3><div class="quote">${esc(w.evidence_quote||'没有可验证用户原话')}</div><p class="muted">确认状态：${w.confirmed?'已确认':'未确认'} · 到期：${when(w.expires_at)}</p></div><div class="detail-section"><h3>版本</h3><p>${w.revisions?.length||0} 次可追溯修改</p></div><div class="detail-section row">${w.status==='suggested'?`<button data-work-action="confirm" data-id="${w.id}">确认</button>`:''}${['active','suggested'].includes(w.status)?`<button class="quiet" data-work-action="resolve" data-id="${w.id}">解决</button><button class="quiet" data-work-action="archive" data-id="${w.id}">归档</button>`:''}<button class="quiet" data-work-action="promote" data-id="${w.id}">转长期候选</button></div>`);}
  if(type==='subject'){const s=await api(`/subjects/${id}`);openDrawer(`<span class="eyebrow">${esc(s.subject_type.toUpperCase())}</span><h2>${esc(s.name)}</h2><div class="list-meta">${badge(s.status)} ${(s.aliases||[]).map(a=>badge(a)).join('')}</div><div class="detail-section"><h3>说明</h3><p class="muted">${esc(s.description||'暂无说明')}</p></div><div class="detail-section"><h3>关系与影响</h3><p>${s.links?.length||0} 个对象关联 · ${s.relations?.length||0} 条实体关系</p></div><div class="detail-section row"><button data-edit-subject="${s.id}">编辑</button><button class="quiet" data-merge-subject="${s.id}">合并</button><button class="quiet" data-split-subject="${s.id}">拆分</button><button class="quiet" data-relate-subject="${s.id}">添加关系</button>${s.subject_type==='project'?`<button class="quiet" data-context="${s.id}">上下文预览</button>`:''}</div>`);}
  if(type==='review'){const rows=[...(await api('/reviews?status=open')),...(await api('/reviews?status=resolved')),...(await api('/reviews?status=dismissed'))];const r=rows.find(x=>x.id===id);if(!r)return;const current=r.primary_memory||r.related_memory;openDrawer(`<span class="eyebrow">REVIEW · ${esc(r.queue.toUpperCase())}</span><h2>${esc(r.issue_type)}</h2><p class="muted">${esc(r.reason)}</p><div class="detail-section"><h3>建议与影响</h3><div class="diff"><div class="old"><b>现有</b><p>${esc(current?.content||'无相关长期记忆')}</p></div><div class="new"><b>建议</b><p>${esc(r.proposed_content||r.proposed_action)}</p></div></div></div>${r.candidate?.evidence?.length?`<div class="detail-section"><h3>用户证据</h3>${r.candidate.evidence.map(e=>`<div class="quote">${esc(e.excerpt)}</div>`).join('')}</div>`:''}${r.status==='open'?`<div class="detail-section row"><button data-resolve-review="${r.id}" data-action="${esc(reviewAction(r))}">${esc(reviewLabel(r))}</button><button class="quiet" data-dismiss-review="${r.id}">保留现状</button></div>`:''}`);}
}
function reviewAction(r){if(r.issue_type==='project_assignment')return'confirm_assignment';if(r.issue_type==='stale_work_item')return'archive_work_item';if(r.issue_type==='summary_refresh')return'refresh_summary';return r.proposed_action==='defer'?'keep':r.proposed_action;}
function reviewLabel(r){return({confirm_assignment:'确认归属',archive_work_item:'归档工作项',refresh_summary:'刷新摘要',merge:'合并',supersede:'替代',create:'新建长期记忆',trash:'回收',keep:'保留'})[reviewAction(r)]||'执行建议';}

function formDialog(title,fields,onSave,eyebrow='EDIT'){const d=$("#form-dialog");$("#form-title").textContent=title;$("#form-eyebrow").textContent=eyebrow;$("#form-fields").innerHTML=fields;d.returnValue='';d.showModal();d.onclose=async()=>{if(d.returnValue!=='save')return;try{await onSave(new FormData($("#dynamic-form")));closeDrawer();await loadAll();}catch(e){fail(e);}};}
function confirmDialog(title,message,confirmText=''){return new Promise(resolve=>{const d=$("#confirm-dialog");$("#confirm-title").textContent=title;$("#confirm-message").textContent=message;$("#confirm-text-wrap").hidden=!confirmText;$("#confirm-text").value='';d.showModal();d.onclose=()=>resolve(d.returnValue==='confirm'&&(!confirmText||$("#confirm-text").value===confirmText));});}
async function contextPreview(projectId,query=''){const p=await api(`/projects/${projectId}/context-preview?query=${encodeURIComponent(query)}`);openDrawer(`<span class="eyebrow">CONTEXT PREVIEW</span><h2>${esc(p.project_detection.project?.name||'仅全局记忆')}</h2><p class="muted">置信度 ${Math.round((p.project_detection.confidence||0)*100)}% · ${esc(p.project_detection.reason)} · ${p.budget.used_chars}/${p.budget.max_chars} 字符</p><div class="detail-section"><label>模拟当前查询<input id="preview-query" value="${esc(query)}" placeholder="输入一条查询后刷新预览"></label><button class="quiet small" data-refresh-context="${esc(projectId)}">刷新预览</button></div><div class="detail-section"><h3>将注入</h3>${p.items.map(i=>`<div class="quote">${esc(i.rendered)}</div>`).join('')||empty('没有内容')}</div><div class="detail-section"><h3>已排除</h3>${p.excluded.map(i=>`<p>${esc(i.id)} · ${esc(i.reason)}</p>`).join('')||'<p class="muted">无</p>'}</div>`);}

document.addEventListener('click',async event=>{const b=event.target.closest('button,[data-open]');if(!b)return;try{
  if(b.dataset.workspace) return nav(b.dataset.workspace);
  if(b.dataset.go) return nav(b.dataset.go,b.dataset.tab);
  if(b.dataset.libraryTab) return showLibrary(b.dataset.libraryTab);
  if(b.dataset.systemTab) return showSystem(b.dataset.systemTab);
  if(b.dataset.reviewTab){reviewTab=b.dataset.reviewTab;return loadReviews();}
  if(b.dataset.open) return detail(b.dataset.open,b.dataset.id);
  if(b.dataset.project){currentProject=b.dataset.project;return renderProject(currentProject);}
  if(b.dataset.context) return contextPreview(b.dataset.context);
  if(b.dataset.refreshContext) return contextPreview(b.dataset.refreshContext,$("#preview-query")?.value||'');
  if(b.id==='refresh') return loadAll();
  if(b.id==='global-search'){nav('library');$("#library-search").focus();return;}
  if(b.id==='add-project') return formDialog('新建项目','<label>名称<input name="name" required></label><label>说明<textarea name="description"></textarea></label><label>别名（逗号分隔）<input name="aliases"></label><label>工作目录（逗号分隔）<input name="workspace_aliases"></label>',f=>mutate('/projects',{name:f.get('name'),description:f.get('description'),aliases:String(f.get('aliases')).split(',').filter(Boolean),workspace_aliases:String(f.get('workspace_aliases')).split(',').filter(Boolean)},'POST',false),'PROJECT');
  if(b.id==='library-create'){if(libraryTab==='memories')return formDialog('新增长期记忆','<label>类型<select name="kind"><option>fact</option><option>preference</option><option>decision</option><option>project</option><option>procedure</option></select></label><label>内容<textarea name="content" rows="6" required></textarea></label>',f=>mutate('/memories',{kind:f.get('kind'),content:f.get('content')},'POST',false),'LONG-TERM MEMORY');if(libraryTab==='subjects')return formDialog('新建主题或实体','<label>类型<select name="subject_type"><option>topic</option><option>person</option><option>organization</option><option>tool</option></select></label><label>名称<input name="name" required></label><label>说明<textarea name="description"></textarea></label>',f=>mutate('/subjects',Object.fromEntries(f),'POST',false),'SUBJECT');notice('工作记忆由 Dream observe 或项目工作台产生');return;}
  if(b.dataset.editMemory){const all=(await api('/memories?status=active'));const m=all.find(x=>x.id===b.dataset.editMemory);return formDialog('编辑长期记忆',`<label>类型<input name="kind" value="${esc(m.kind)}"></label><label>内容<textarea name="content" rows="6">${esc(m.content)}</textarea></label><div class="form-pair"><label>有效开始<input name="valid_from" value="${esc(m.valid_from||'')}"></label><label>有效结束<input name="valid_to" value="${esc(m.valid_to||'')}"></label></div><label>时间状态<select name="temporal_status"><option ${m.temporal_status==='current'?'selected':''}>current</option><option ${m.temporal_status==='historical'?'selected':''}>historical</option><option ${m.temporal_status==='disputed'?'selected':''}>disputed</option></select></label><label>变更原因<textarea name="temporal_reason">${esc(m.temporal_reason||'')}</textarea></label>`,f=>mutate(`/memories/${m.id}`,Object.fromEntries(f),'PATCH',false),'VERSIONED MEMORY');}
  if(b.dataset.trashMemory){if(await confirmDialog('移入回收站','该操作可恢复，项目上下文将立即停止使用这条记忆。'))await mutate(`/memories/${b.dataset.trashMemory}/trash`);return;}
  if(b.dataset.restoreMemory){const r=await mutate(`/memories/${b.dataset.restoreMemory}/restore`);if(r.status==='review_required')notice('恢复发现重复或冲突，已进入审核');return;}
  if(b.dataset.purgeMemory){if(await confirmDialog('永久删除','将先创建隐私清理后的整包备份，并清理证据、召回、索引、实体关联及派生摘要。','永久删除'))await mutate(`/memories/${b.dataset.purgeMemory}`,{},'DELETE');return;}
  if(b.dataset.workAction){if(b.dataset.workAction==='confirm'){const projects=await api('/projects?status=active');const item=(await api('/work-items')).find(x=>x.id===b.dataset.id);const options=projects.map(p=>`<option value="${p.id}" ${p.id===item?.subject_id?'selected':''}>${esc(p.name)}</option>`).join('');return formDialog('确认工作记忆',`<label>项目<select name="subject_id" required><option value="">请选择项目</option>${options}</select></label><p class="quote">${esc(item?.evidence_quote||'没有用户原话；请先核对证据')}</p>`,f=>mutate(`/work-items/${b.dataset.id}/confirm`,{subject_id:f.get('subject_id')},'POST',false),'GROUNDING');}const r=await mutate(`/work-items/${b.dataset.id}/${b.dataset.workAction}`,{});if(r.status==='review_required')notice('长期化需要审核');return;}
  if(b.dataset.editSubject){const s=await api(`/subjects/${b.dataset.editSubject}`);return formDialog('编辑主题或项目',`<label>名称<input name="name" value="${esc(s.name)}"></label><label>说明<textarea name="description">${esc(s.description||'')}</textarea></label><label>状态<select name="status"><option ${s.status==='active'?'selected':''}>active</option><option ${s.status==='paused'?'selected':''}>paused</option><option ${s.status==='archived'?'selected':''}>archived</option></select></label><label>别名（逗号分隔）<input name="aliases" value="${esc((s.aliases||[]).join(','))}"></label>`,f=>mutate(`/subjects/${s.id}`,{name:f.get('name'),description:f.get('description'),status:f.get('status'),aliases:String(f.get('aliases')).split(',').filter(Boolean)},'PATCH',false),'SUBJECT');}
  if(b.dataset.mergeSubject){const subjects=(await api('/subjects')).filter(s=>s.id!==b.dataset.mergeSubject);return formDialog('合并到当前实体',`<p class="muted">所选实体的别名、工作项、摘要版本和对象关联将转移到当前 canonical；来源实体会被删除，演化内容保留。</p><label>要吸收的实体<select name="source_id" required><option value="">请选择</option>${subjects.map(s=>`<option value="${s.id}">${esc(s.name)} · ${s.link_count||0} 关联</option>`).join('')}</select></label>`,f=>mutate(`/subjects/${b.dataset.mergeSubject}/merge`,{source_id:f.get('source_id')},'POST',false),'IMPACT PREVIEW');}
  if(b.dataset.splitSubject){return formDialog('拆分实体',`<p class="muted">创建新实体并移动指定对象；未列出的内容留在原实体，操作后可再次纠正归属。</p><label>新实体名称<input name="name" required></label><label>移动的对象 ID（逗号分隔）<textarea name="object_ids"></textarea></label><label>移动的工作项 ID（逗号分隔）<textarea name="work_item_ids"></textarea></label>`,f=>mutate(`/subjects/${b.dataset.splitSubject}/split`,{name:f.get('name'),object_ids:String(f.get('object_ids')).split(',').map(x=>x.trim()).filter(Boolean),work_item_ids:String(f.get('work_item_ids')).split(',').map(x=>x.trim()).filter(Boolean)},'POST',false),'IMPACT PREVIEW');}
  if(b.dataset.relateSubject){const subjects=(await api('/subjects')).filter(s=>s.id!==b.dataset.relateSubject);return formDialog('添加实体关系',`<label>目标实体<select name="target_subject_id" required>${subjects.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('')}</select></label><label>关系类型<input name="relation_type" value="related" required></label>`,f=>mutate(`/subjects/${b.dataset.relateSubject}/relations`,Object.fromEntries(f),'POST',false),'RELATION');}
  if(b.dataset.editProject){return detail('subject',b.dataset.editProject);}
  if(b.dataset.summary){await mutate(`/summaries/project/${b.dataset.summary}/regenerate`);return;}
  if(b.dataset.resolveReview){const action=b.dataset.action;if(['create','edit_execute','supersede'].includes(action)){return formDialog('执行审核建议','<label>执行内容<textarea name="content" rows="6" required></textarea></label><p class="muted">执行后会保留审核决策与记忆版本链。</p>',f=>mutate(`/reviews/${b.dataset.resolveReview}/resolve`,{action,content:f.get('content')},'POST',false),'REVIEW EXECUTION');}await mutate(`/reviews/${b.dataset.resolveReview}/resolve`,{action});return;}
  if(b.dataset.dismissReview){if(await confirmDialog('保留现状','相同内容指纹未变化时，后续审计不会重复提示。'))await mutate(`/reviews/${b.dataset.dismissReview}/dismiss`);return;}
  if(b.id==='run-audit'){await mutate('/reviews/scan',{scope:'full'});return;}
  if(b.id==='run-dream'||b.id==='dry-dream'){$("#dream-overlay").hidden=false;try{await mutate('/dream/run',{dry_run:b.id==='dry-dream'});}finally{$("#dream-overlay").hidden=true;}return;}
  if(b.dataset.test){const r=await mutate('/model/test',{kind:b.dataset.test},'POST',false);notice(r.ok?'连接成功':'连接失败');return;}
  if(b.id==='validate-storage'){const r=await mutate('/maintenance/validate',{},'POST',false);notice(r.ok?'存储校验通过':r.errors.join('；'),!r.ok);await loadStorage();return;}
  if(b.dataset.retryIngestion){await mutate(`/maintenance/ingestion-issues/${b.dataset.retryIngestion}/retry`);await loadIngestionIssues();return;}
  if(b.id==='rebuild-projections'){await mutate('/maintenance/rebuild-projections');return;}
  if(b.id==='rebuild-index'){await mutate('/rebuild',{embeddings:false});return;}
  if(b.id==='create-backup'){await mutate('/backup');return;}
  if(b.dataset.previewBackup){const p=await mutate(`/backups/${encodeURIComponent(b.dataset.previewBackup)}/preview-restore`,{},'POST',false);const d=$("#preview-dialog");d.dataset.backup=b.dataset.previewBackup;$("#preview-title").textContent='恢复预演';$("#preview-content").innerHTML=`<div class="health-list">${Object.entries(p).filter(([,v])=>typeof v!=='object').map(([k,v])=>`<div class="health-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')}</div><pre>${esc(JSON.stringify(p.delta||p.counts||{},null,2))}</pre>`;d.showModal();return;}
}catch(error){fail(error);}});

for(const section of ['general','llm','embedding','dream','recall','retention']){$(`#${section}-form`)?.addEventListener('submit',async e=>{e.preventDefault();try{const form=e.currentTarget;const body={};for(const [key,value] of new FormData(form).entries()){const input=form.elements[key];body[key]=input.type==='number'?Number(value):value;}form.querySelectorAll('input[type=checkbox]').forEach(i=>body[i.name]=i.checked);const key=body.api_key;delete body.api_key;await mutate(`/settings/${section}`,body,'POST',false);if(key)await mutate(`/secrets/${section==='llm'?'llm_api_key':'embedding_api_key'}`,{value:key},'POST',false);notice('设置已保存');await loadSettings();}catch(error){fail(error);}});}
$("#library-search").addEventListener('input',()=>loadLibrary().catch(fail));$("#library-filter").addEventListener('change',()=>loadLibrary().catch(fail));$("#project-search").addEventListener('input',()=>loadProjects().catch(fail));$(".drawer-close").addEventListener('click',closeDrawer);$("#drawer-scrim").addEventListener('click',closeDrawer);$("#export").addEventListener('click',e=>{e.currentTarget.href=base+'/export';});$("#preview-confirm").addEventListener('click',async e=>{e.preventDefault();const d=$("#preview-dialog");if(!await confirmDialog('确认恢复','将先自动备份当前状态，再以备份数据库为准恢复并重建 vault 与索引。'))return;d.close();await mutate(`/backups/${encodeURIComponent(d.dataset.backup)}/restore`);});
loadAll();
