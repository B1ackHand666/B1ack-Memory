const base = location.pathname.replace(/\/ui\/?$/, "");
const dashboardBridge =
  window.parent !== window ? window.parent.__B1ACK_MEMORY_DASHBOARD_BRIDGE__ : null;

let token = "";
let settings = {};
let editing = null;
let memoriesById = new Map();
let candidateStatus = "pending";
let analyticsRange = "30d";
let noticeTimer = null;

const pageDescriptions = {
  overview: "记忆健康、处理队列和近期活动",
  evolution: "用趋势和时间线看见记忆如何形成、晋升与修订",
  memories: "查看、修订、回收或永久删除长期记忆",
  candidates: "审核短期候选、证据、REM 判断和晋升进度",
  dream: "检查 Light、REM、Deep 的运行结果与模型调用",
  traces: "查看哪些记忆曾被检索，以及是否实际注入回答",
  settings: "配置时区、兼容模型、密钥、调度和召回参数",
  maintenance: "创建与恢复备份，执行清理和索引重建",
};

const eventLabels = {
  candidate_created: "创建候选",
  evidence_added: "增加证据",
  rem_reviewed: "REM 审查",
  candidate_merged: "合并同义候选",
  candidate_expired: "候选过期",
  candidate_restored: "恢复候选",
  candidate_rejected: "拒绝候选",
  candidate_promoted: "晋升为长期记忆",
  memory_created: "创建长期记忆",
  memory_updated: "修订长期记忆",
  memory_trashed: "移入回收站",
  memory_restored: "恢复长期记忆",
  memory_superseded: "长期记忆被替代",
};

const laneLabels = {
  different_dates: "不同日期证据",
  demonstrated_utility: "实际作用",
  both: "双通道",
  manual: "人工晋升",
  unknown: "历史未知",
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character],
  );
}

function localTime(value) {
  if (!value) return "—";
  const date = typeof value === "number" ? new Date(value) : new Date(String(value).replace(" ", "T"));
  return Number.isNaN(date.valueOf()) ? escapeHtml(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function emptyState(message) {
  return `<div class="empty">${escapeHtml(message)}</div>`;
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (options.method && options.method !== "GET") headers["X-B1ack-Memory-Token"] = token;
  if (dashboardBridge) return dashboardBridge.request(path, { ...options, headers });
  const response = await fetch(base + path, { ...options, headers });
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { detail = await response.text(); }
    throw new Error(detail || response.statusText);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("json") ? response.json() : response.text();
}

function showNotice(message, error = false) {
  const notice = $("#notice");
  clearTimeout(noticeTimer);
  notice.textContent = message;
  notice.className = error ? "error" : "";
  notice.style.display = "block";
  noticeTimer = setTimeout(() => { notice.style.display = "none"; }, 3600);
}

function countUp(element, target) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    element.textContent = target;
    return;
  }
  const started = performance.now();
  const duration = 420;
  const tick = (now) => {
    const progress = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    element.textContent = Math.round(target * eased);
    if (progress < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function staggerCards(container) {
  container.querySelectorAll(".card").forEach((card, index) => card.style.setProperty("--i", Math.min(index, 12)));
}

async function loadAll() {
  try {
    const bootstrap = await api("/bootstrap");
    token = bootstrap.token;
    renderStatus(bootstrap.status);
    await Promise.all([
      loadSettings(), loadMemories(), loadCandidates(), loadDreams(), loadModelCalls(),
      loadRecallTraces(), loadBackups(), loadEvolution(),
    ]);
  } catch (error) {
    showNotice(error.message, true);
  }
}

function renderStatus(status) {
  const counts = status.counts;
  const metrics = [
    ["长期有效", counts.active_memories, "durable"],
    ["待审核", counts.pending_candidates, "candidate"],
    ["已过期", counts.expired_candidates, "expired"],
    ["已拒绝", counts.rejected_candidates, "rejected"],
    ["待处理会话", counts.pending_turns, "turns"],
    ["回收站", counts.trashed_memories, "trash"],
  ];
  $("#metrics").innerHTML = metrics.map(([label, value, note]) =>
    `<div class="metric"><span>${label}</span><b data-count="${value}">0</b><small>${note}</small></div>`).join("");
  $("#metrics").querySelectorAll("[data-count]").forEach((item) => countUp(item, Number(item.dataset.count)));
  $("#candidate-pending-count").textContent = counts.pending_candidates;
  $("#candidate-expired-count").textContent = counts.expired_candidates;
  $("#candidate-rejected-count").textContent = counts.rejected_candidates;
  const tz = status.general?.timezone || "system";
  $("#health").innerHTML = `
    <p>数据库 <b>${escapeHtml(status.database.integrity)}</b> <span class="badge success">${(status.database.bytes / 1024).toFixed(1)} KB</span></p>
    <p>Dream 模型 <b>${escapeHtml(status.llm.model || "未配置")}</b> <span class="badge">Key ${status.llm.configured ? "已配置" : "未配置"}</span></p>
    <p>记忆时区 <b>${escapeHtml(tz)}</b></p>
    <p>下次 Dream <b>${localTime(status.next_dream)}</b></p>
    <p class="meta">数据目录 ${escapeHtml(status.data_root)}</p>`;
}

async function loadSettings() {
  settings = await api("/settings");
  for (const section of ["general", "llm", "embedding", "dream", "recall", "retention"]) {
    const form = $(`#${section}-form`);
    if (!form) continue;
    for (const [key, value] of Object.entries(settings[section] || {})) {
      const input = form.elements[key];
      if (!input) continue;
      if (input.type === "checkbox") input.checked = Boolean(value);
      else input.value = value ?? "";
    }
  }
  $("#llm-key").textContent = `Key：${settings.secrets.llm_api_key.masked || "未配置"}`;
  $("#embedding-key").textContent = `Key：${settings.secrets.embedding_api_key.masked || "未配置"}`;
  const detected = Intl.DateTimeFormat().resolvedOptions().timeZone || "system";
  $("#detected-timezone").textContent = `浏览器检测：${detected}`;
  $("#adopt-timezone").dataset.timezone = detected;
}

async function loadMemories() {
  const status = $("#memory-status").value;
  const rows = await api(`/memories?status=${encodeURIComponent(status)}`);
  memoriesById = new Map(rows.map((item) => [item.id, item]));
  const query = $("#memory-search").value.toLowerCase();
  const visible = rows.filter((item) => item.content.toLowerCase().includes(query));
  const list = $("#memory-list");
  list.innerHTML = visible.map((item) => {
    const lineage = `<button class="ghost" data-action="lineage-memory" data-id="${item.id}">查看演化</button>`;
    const actions = status === "active"
      ? `${lineage}<button class="ghost" data-action="edit-memory" data-id="${item.id}">编辑</button><button class="danger" data-action="trash-memory" data-id="${item.id}">回收</button>`
      : `${lineage}<button data-action="restore-memory" data-id="${item.id}">恢复</button><button class="danger" data-action="purge-memory" data-id="${item.id}">永久删除</button>`;
    const source = item.origin_label || item.origin;
    const linked = item.lineage ? `<span class="badge success">有候选来源</span>` : "";
    return `<div class="card"><div class="card-content"><p class="memory-copy">${escapeHtml(item.content)}</p><div class="meta"><span class="badge">${escapeHtml(item.kind)}</span> <span class="badge">${escapeHtml(source)}</span> ${linked} · 更新于 ${localTime(item.updated_at)} · ${escapeHtml(item.id)}</div></div><div class="actions">${actions}</div></div>`;
  }).join("") || emptyState(query ? "没有匹配的长期记忆" : "当前分区没有长期记忆");
  staggerCards(list);
}

function candidateStatusName(status) {
  return ({ pending: "待审核", promoted: "已晋升", expired: "已过期", rejected: "已拒绝" })[status] || status;
}

async function loadCandidates() {
  const rows = await api(`/candidates?status=${encodeURIComponent(candidateStatus)}`);
  document.querySelectorAll("[data-candidate-status]").forEach((button) => button.classList.toggle("active", button.dataset.candidateStatus === candidateStatus));
  const bulk = $("#purge-candidate-status");
  bulk.hidden = candidateStatus === "pending" || rows.length === 0;
  const list = $("#candidate-list");
  list.innerHTML = rows.map((item) => {
    const progress = item.promotion_progress;
    const evidenceDates = item.evidence_dates?.length ? item.evidence_dates.join("、") : "暂无日期";
    let lifecycle = "";
    if (candidateStatus === "pending") lifecycle = `无活动过期：${localTime(item.lifecycle.expires_at)}`;
    else lifecycle = `自动清理：${localTime(item.lifecycle.purge_at)}`;
    const progressHtml = candidateStatus === "pending" ? `<div class="promotion-progress">
      <span class="${progress.confidence_met ? "met" : ""}">置信度 ${Number(item.model_confidence).toFixed(2)}</span>
      <span class="${progress.rem_approved ? "met" : ""}">REM ${escapeHtml(item.rem_status)}</span>
      <span class="${progress.repeat_evidence.met ? "met" : ""}">不同日期证据 ${progress.repeat_evidence.current}/2</span>
      <span class="${progress.utility.met ? "met" : ""}">实际作用 ${progress.utility.recalls}/2 次 · ${progress.utility.queries}/2 类查询</span>
    </div>` : "";
    const rem = item.rem_reason ? `<div class="candidate-reason">REM 判断：${escapeHtml(item.rem_reason)}</div>` : "";
    const conflict = item.conflict_reason ? `<div class="conflict">冲突：${escapeHtml(item.conflict_reason)}</div>` : "";
    let actions = `<button class="ghost" data-action="lineage-candidate" data-id="${item.id}">查看演化</button>`;
    if (candidateStatus === "pending") actions += `<button data-action="promote-candidate" data-id="${item.id}">人工晋升</button><button class="danger" data-action="reject-candidate" data-id="${item.id}">拒绝</button><button class="danger" data-action="purge-candidate" data-id="${item.id}">永久删除</button>`;
    else if (["expired", "rejected"].includes(candidateStatus)) actions += `<button data-action="restore-candidate" data-id="${item.id}">恢复</button><button class="danger" data-action="purge-candidate" data-id="${item.id}">永久删除</button>`;
    const linked = item.linked_memory ? `<div class="candidate-reason">长期记忆：${escapeHtml(item.linked_memory.content)}</div>` : "";
    const details = escapeHtml(JSON.stringify({ score: item.score_components, evidence_dates: item.evidence_dates, evidence: item.evidence }, null, 2));
    return `<div class="card"><div class="card-content"><p class="candidate-copy">${escapeHtml(item.content)}</p><div class="meta"><span class="badge ${candidateStatus === "promoted" ? "success" : ""}">${candidateStatusName(candidateStatus)}</span> <span class="badge">${escapeHtml(item.kind)}</span> · 评分 ${Number(item.score).toFixed(2)} · 最后活动 ${localTime(item.last_activity_at)} · ${lifecycle}</div>${progressHtml}${rem}${conflict}${linked}<details><summary>证据日期 ${item.evidence_days}/2 · ${escapeHtml(evidenceDates)}</summary><pre>${details}</pre></details></div><div class="actions">${actions}</div></div>`;
  }).join("") || emptyState(`当前没有${candidateStatusName(candidateStatus)}候选`);
  staggerCards(list);
}

async function loadDreams() {
  const rows = await api("/dream-runs");
  const list = $("#dream-list");
  list.innerHTML = rows.map((item) => `<div class="card"><div class="card-content"><p><b>${escapeHtml(item.status)}</b> · ${localTime(item.started_at)}</p><div class="meta">输入 ${item.input_count} · 新增 ${item.candidate_count} · 合并 ${item.merged_count} · 过滤 ${item.filtered_count} · 过期 ${item.expired_count} · 晋升 ${item.promoted_count} · Token ${item.input_tokens}/${item.output_tokens}</div>${item.error ? `<pre>${escapeHtml(item.error)}</pre>` : ""}</div><span class="badge">${escapeHtml(item.id.slice(0, 8))}</span></div>`).join("") || emptyState("尚无 Dream 运行记录");
  staggerCards(list);
}

async function loadModelCalls() {
  const rows = await api("/model-calls");
  $("#call-list").innerHTML = rows.map((item) => {
    const detail = escapeHtml(JSON.stringify({ request: item.request_json, response: item.response_json, error: item.error }, null, 2));
    return `<div class="card"><div class="card-content"><p><b>${escapeHtml(item.phase)}</b> · ${escapeHtml(item.model)} · ${localTime(item.created_at)}</p><div class="meta">Token ${item.input_tokens}/${item.output_tokens} · Dream ${escapeHtml(item.dream_run_id)}</div><details><summary>查看请求与响应</summary><pre>${detail}</pre></details></div></div>`;
  }).join("") || emptyState("尚无模型调用记录");
}

async function loadRecallTraces() {
  const rows = await api("/recall-traces");
  if (!rows.length) {
    $("#trace-list").innerHTML = emptyState("尚无召回轨迹");
    return;
  }
  const body = rows.map((item) => `<tr><td>${localTime(item.created_at)}</td><td>${escapeHtml(item.query_text)}</td><td>${escapeHtml(item.source)}</td><td>${escapeHtml(item.record_id)}</td><td>${item.keyword_rank ?? "—"} / ${Number(item.final_score).toFixed(3)}</td><td><span class="badge ${item.injected ? "success" : ""}">${item.injected ? "已注入" : "仅检索"}</span></td></tr>`).join("");
  $("#trace-list").innerHTML = `<table><thead><tr><th>时间</th><th>查询</th><th>来源</th><th>记录</th><th>关键词排名 / 分数</th><th>作用</th></tr></thead><tbody>${body}</tbody></table>`;
}

async function loadBackups() {
  const rows = await api("/backups");
  $("#backup-list").innerHTML = rows.map((item) => `<div class="card"><div><p>${escapeHtml(item.name)}</p><div class="meta">${(item.bytes / 1024).toFixed(1)} KB · ${localTime(item.modified * 1000)}</div></div><button class="danger" data-action="restore-backup" data-name="${escapeHtml(item.name)}">恢复此备份</button></div>`).join("") || emptyState("尚无数据库备份");
}

function renderDailyChart(rows) {
  const metrics = [
    ["candidates", "新增候选", "#f0f0ee"], ["merged", "合并", "#b6b6b1"],
    ["promoted", "晋升", "#80aa8d"], ["expired", "过期", "#696966"],
    ["rejected", "拒绝", "#bd7478"], ["edited", "编辑", "#92928d"],
  ];
  $("#daily-legend").innerHTML = metrics.map(([, label, color]) => `<span><i style="background:${color}"></i>${label}</span>`).join("");
  if (!rows.length) return emptyState("所选范围内还没有记忆变化");
  const width = 960, height = 285, top = 14, bottom = 34, left = 32, right = 10;
  const max = Math.max(1, ...rows.map((row) => metrics.reduce((sum, [key]) => sum + Number(row[key] || 0), 0)));
  const innerWidth = width - left - right, innerHeight = height - top - bottom;
  const step = innerWidth / rows.length, barWidth = Math.max(2, Math.min(18, step * .68));
  const guides = [0, .5, 1].map((ratio) => {
    const y = top + innerHeight * (1 - ratio);
    return `<line class="grid-line" x1="${left}" y1="${y}" x2="${width-right}" y2="${y}"/><text x="2" y="${y+3}">${Math.round(max*ratio)}</text>`;
  }).join("");
  const labelEvery = Math.max(1, Math.ceil(rows.length / 8));
  const bars = rows.map((row, index) => {
    const x = left + index * step + (step - barWidth) / 2;
    let y = top + innerHeight;
    const segments = metrics.map(([key,, color], metricIndex) => {
      const value = Number(row[key] || 0);
      const segmentHeight = innerHeight * value / max;
      y -= segmentHeight;
      return value ? `<rect style="--i:${index + metricIndex}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}" height="${Math.max(1, segmentHeight).toFixed(2)}" rx="2" fill="${color}"><title>${row.date} · ${metrics[metricIndex][1]} ${value}</title></rect>` : "";
    }).join("");
    const label = index % labelEvery === 0 || index === rows.length - 1 ? `<text text-anchor="middle" x="${(x+barWidth/2).toFixed(2)}" y="${height-10}">${escapeHtml(row.date.slice(5))}</text>` : "";
    return segments + label;
  }).join("");
  return `<svg class="daily-svg" role="img" aria-label="每日记忆变化堆叠柱状图" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">${guides}${bars}</svg>`;
}

function renderBars(items) {
  const max = Math.max(1, ...items.map((item) => item.value));
  if (!items.some((item) => item.value)) return emptyState("暂无数据");
  return items.map((item) => `<div class="bar-row"><span>${escapeHtml(item.label)}</span><div class="bar-track"><i class="bar-value" style="--width:${(item.value/max*100).toFixed(1)}%;--shade:${item.color || "#efefed"}"></i></div><b>${item.value}</b></div>`).join("");
}

async function loadEvolution() {
  const data = await api(`/analytics/memory-flow?range=${analyticsRange}`);
  $("#analytics-timezone").textContent = `按 ${data.timezone} 统计`;
  const totals = data.daily.reduce((sum, row) => {
    for (const key of ["candidates", "merged", "promoted", "expired", "rejected", "edited", "recalls"]) sum[key] += Number(row[key] || 0);
    return sum;
  }, { candidates: 0, merged: 0, promoted: 0, expired: 0, rejected: 0, edited: 0, recalls: 0 });
  $("#evolution-summary").innerHTML = [
    ["新增候选", totals.candidates], ["合并去重", totals.merged], ["完成晋升", totals.promoted], ["实际注入", totals.recalls],
  ].map(([label, value]) => `<div class="metric"><span>${label}</span><b data-count="${value}">0</b><small>${analyticsRange}</small></div>`).join("");
  $("#evolution-summary").querySelectorAll("[data-count]").forEach((item) => countUp(item, Number(item.dataset.count)));
  $("#daily-chart").innerHTML = renderDailyChart(data.daily);
  $("#status-chart").innerHTML = renderBars([
    { label: "待审核", value: data.status.pending || 0 },
    { label: "长期有效", value: data.status.active_memories || 0, color: "#efefed" }, { label: "已过期", value: data.status.expired || 0, color: "#696966" },
    { label: "已拒绝", value: data.status.rejected || 0, color: "#bd7478" },
  ]);
  $("#lane-chart").innerHTML = renderBars(Object.entries(data.promotion_lanes).map(([key, value], index) => ({ label: laneLabels[key] || key, value, color: ["#f0f0ee", "#b8b8b3", "#80aa8d", "#777773", "#545451"][index] })));
  $("#recent-events").innerHTML = data.recent.map((item) => {
    const id = item.candidate_id || item.memory_id;
    const type = item.candidate_id ? "candidate" : "memory";
    const origin = item.backfilled ? "历史回填" : item.dream_run_id ? "Dream" : "实时记录";
    return `<button class="event-row" ${id ? `data-action="lineage-${type}" data-id="${escapeHtml(id)}"` : "disabled"}><time>${escapeHtml(item.occurred_at.slice(0, 10))}</time><span>${escapeHtml(eventLabels[item.event_type] || item.event_type)}</span><small>${origin} →</small></button>`;
  }).join("") || emptyState("所选范围内还没有变化记录");
}

async function mutate(path, body = {}, method = "POST", { reload = true } = {}) {
  const result = await api(path, { method, body: JSON.stringify(body) });
  showNotice("操作已完成");
  if (reload) await loadAll();
  return result;
}

function openEditor(memory = null) {
  editing = memory;
  $("#edit-content").value = memory?.content || "";
  $("#edit-kind").value = memory?.kind || "fact";
  $("#editor-title").textContent = memory ? "编辑长期记忆" : "新增长期记忆";
  $("#editor").showModal();
}

function confirmAction(message, title = "确认危险操作", buttonText = "确认") {
  return new Promise((resolve) => {
    const dialog = $("#confirm-dialog");
    $("#confirm-title").textContent = title;
    $("#confirm-message").textContent = message;
    $("#confirm-submit").textContent = buttonText;
    const close = () => { dialog.removeEventListener("close", close); resolve(dialog.returnValue === "confirm"); };
    dialog.addEventListener("close", close);
    dialog.showModal();
  });
}

function eventDescription(event) {
  const data = event.data || {};
  if (event.event_type === "candidate_promoted") return `通道：${laneLabels[data.promotion_lane] || data.promotion_lane || "未知"}${data.candidate_content && data.memory_content && data.candidate_content !== data.memory_content ? " · Deep 已整理候选原文" : ""}`;
  if (event.event_type === "rem_reviewed") return data.reason || `结果：${data.decision || "未知"}`;
  if (event.event_type === "evidence_added") return data.excerpt || "候选获得新的真实会话证据";
  if (event.event_type === "candidate_merged") return data.reason || "同义候选的证据已合并";
  if (event.event_type === "memory_updated") return data.previous_content && data.content ? `${data.previous_content} → ${data.content}` : "内容或类型已修订";
  return data.reason || data.content || "状态已记录";
}

async function openLineage(type, id) {
  const data = await api(`/lineage/${type}/${encodeURIComponent(id)}`);
  const candidate = data.candidate;
  const memory = data.memory;
  $("#lineage-title").textContent = memory ? "长期记忆演化" : "候选记忆演化";
  $("#lineage-subtitle").textContent = `时区 ${data.timezone} · 注入 ${data.recall_summary.injected} 次`;
  const comparison = candidate || memory ? `<div class="lineage-compare">
    <div class="lineage-node"><span>候选原文</span><p>${escapeHtml(candidate?.content || "没有候选来源（人工或 Hermes 直接写入）")}</p></div>
    <div class="lineage-arrow">→</div>
    <div class="lineage-node"><span>长期记忆</span><p>${escapeHtml(memory?.content || "尚未晋升")}</p></div>
  </div>` : "";
  const evidence = data.evidence.length ? `<div class="lineage-head"><span class="eyebrow">EVIDENCE</span><p>${data.evidence.map((item) => `${escapeHtml(item.local_date)} · ${escapeHtml(item.excerpt || "已关联会话")}`).join("<br>")}</p></div>` : "";
  const timeline = data.events.map((event) => `<div class="timeline-item ${event.backfilled ? "backfilled" : ""}"><i class="timeline-dot"></i><div><time>${escapeHtml(event.occurred_at)} · ${event.backfilled ? "历史回填" : event.dream_run_id ? `Dream ${escapeHtml(event.dream_run_id.slice(0, 8))}` : "实时记录"}</time><h3>${escapeHtml(eventLabels[event.event_type] || event.event_type)}</h3><p>${escapeHtml(eventDescription(event))}</p></div></div>`).join("");
  const revisions = data.revisions.length ? `<details><summary>查看 ${data.revisions.length} 条旧版本</summary><pre>${escapeHtml(JSON.stringify(data.revisions, null, 2))}</pre></details>` : "";
  $("#lineage-content").innerHTML = `${comparison}${evidence}<div class="timeline ${data.events.some((item) => item.backfilled) ? "backfilled" : ""}">${timeline || emptyState("暂无可还原的历史事件")}</div>${revisions}`;
  $("#lineage-dialog").showModal();
}

function setDreaming(active) {
  const overlay = $("#dream-overlay");
  overlay.classList.toggle("active", active);
  overlay.setAttribute("aria-hidden", active ? "false" : "true");
}

async function handleAction(button) {
  const action = button.dataset.action;
  const id = button.dataset.id;
  if (action === "edit-memory") openEditor(memoriesById.get(id));
  else if (action === "lineage-memory") await openLineage("memory", id);
  else if (action === "lineage-candidate") await openLineage("candidate", id);
  else if (action === "trash-memory") await mutate(`/memories/${id}/trash`);
  else if (action === "restore-memory") await mutate(`/memories/${id}/restore`);
  else if (action === "purge-memory") {
    if (await confirmAction("这会清除该记忆、关联候选、证据、会话、Dream 日志、演化事件和旧备份，无法撤销。", "永久删除长期记忆", "永久删除")) await mutate(`/memories/${id}`, {}, "DELETE");
  } else if (action === "promote-candidate") await mutate(`/candidates/${id}/promote`);
  else if (action === "reject-candidate") await mutate(`/candidates/${id}/reject`);
  else if (action === "restore-candidate") await mutate(`/candidates/${id}/restore`);
  else if (action === "purge-candidate") {
    if (await confirmAction("这会清除候选、关联证据、召回、索引、演化事件和旧备份，无法撤销。", "永久删除候选记忆", "永久删除")) await mutate(`/candidates/${id}`, {}, "DELETE");
  } else if (action === "restore-backup") {
    if (await confirmAction("系统会先保存当前状态，再恢复所选备份。当前数据库内容将被替换。", "恢复数据库备份", "恢复备份")) await mutate(`/backups/${encodeURIComponent(button.dataset.name)}/restore`);
  }
}

function navigate(page) {
  document.querySelectorAll("nav button,.page").forEach((item) => item.classList.remove("active"));
  const nav = document.querySelector(`nav button[data-page="${page}"]`);
  nav?.classList.add("active");
  $(`#${page}`).classList.add("active");
  $("#title").textContent = (nav?.textContent.trim() || "记忆演化").replace(/^\d+\s*/, "");
  $("#subtitle").textContent = pageDescriptions[page];
  window.scrollTo({ top: 0, behavior: "smooth" });
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  try {
    if (button.dataset.page) navigate(button.dataset.page);
    else if (button.dataset.range) {
      analyticsRange = button.dataset.range;
      document.querySelectorAll("[data-range]").forEach((item) => item.classList.toggle("active", item === button));
      await loadEvolution();
    } else if (button.dataset.candidateStatus) {
      candidateStatus = button.dataset.candidateStatus;
      await loadCandidates();
    } else if (button.dataset.action) await handleAction(button);
    else if (button.id === "refresh") await loadAll();
    else if (["run-dream", "dry-dream"].includes(button.id)) {
      setDreaming(true);
      try { await mutate("/dream/run", { dry_run: button.id === "dry-dream" }); } finally { setDreaming(false); }
    } else if (button.id === "create-backup") await mutate("/backup");
    else if (button.id === "rebuild") await mutate("/rebuild");
    else if (button.id === "rebuild-vector") await mutate("/rebuild", { embeddings: true });
    else if (button.id === "vacuum") {
      if (await confirmAction("将按保留策略清理数据并压缩 SQLite 数据库。建议先确认已有近期备份。", "清理并压缩数据库", "开始维护")) await mutate("/maintenance", { vacuum: true, cleanup: true });
    } else if (button.dataset.test) await mutate("/model/test", { kind: button.dataset.test });
    else if (button.id === "add-memory") openEditor();
    else if (button.id === "adopt-timezone") $("#general-form").elements.timezone.value = button.dataset.timezone;
    else if (button.id === "purge-candidate-status") {
      const label = candidateStatusName(candidateStatus);
      if (await confirmAction(`将永久删除全部${label}候选及其关联内容、演化记录和旧备份，无法撤销。`, `清空${label}分区`, "全部删除")) await mutate("/candidates/purge", { status: candidateStatus });
    }
  } catch (error) { showNotice(error.message, true); }
});

$("#editor").addEventListener("close", async () => {
  if ($("#editor").returnValue !== "save") return;
  const body = { content: $("#edit-content").value, kind: $("#edit-kind").value };
  try {
    if (editing) await mutate(`/memories/${editing.id}`, body, "PATCH");
    else await mutate("/memories", body);
  } catch (error) { showNotice(error.message, true); }
});

for (const section of ["general", "llm", "embedding", "dream", "recall", "retention"]) {
  const form = $(`#${section}-form`);
  if (!form) continue;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {};
    for (const input of form.elements) {
      if (!input.name || input.name === "api_key") continue;
      if (input.type === "checkbox") body[input.name] = input.checked;
      else if (input.type === "number") body[input.name] = Number(input.value);
      else body[input.name] = input.value;
    }
    try {
      await api(`/settings/${section}`, { method: "POST", body: JSON.stringify(body) });
      const secretInput = form.elements.api_key;
      if (secretInput?.value) {
        const secretName = section === "llm" ? "llm_api_key" : "embedding_api_key";
        await api(`/secrets/${secretName}`, { method: "POST", body: JSON.stringify({ value: secretInput.value }) });
        secretInput.value = "";
      }
      showNotice("设置已保存");
      await loadAll();
    } catch (error) { showNotice(error.message, true); }
  });
}

$("#memory-search").addEventListener("input", loadMemories);
$("#memory-status").addEventListener("change", loadMemories);
$("#export").addEventListener("click", async (event) => {
  if (!dashboardBridge) { event.currentTarget.href = `${base}/export`; return; }
  event.preventDefault();
  try {
    const content = await dashboardBridge.exportText();
    const url = URL.createObjectURL(new Blob([content], { type: "application/x-ndjson;charset=utf-8" }));
    const download = document.createElement("a");
    download.href = url;
    download.download = "b1ack-memory.jsonl";
    download.click();
    URL.revokeObjectURL(url);
  } catch (error) { showNotice(error.message, true); }
});

loadAll();
