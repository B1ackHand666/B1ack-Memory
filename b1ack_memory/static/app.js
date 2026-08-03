const base = location.pathname.replace(/\/ui\/?$/, "");
let token = "";
let settings = {};
let editing = null;
let memoriesById = new Map();

const pageDescriptions = {
  overview: "记忆健康、处理队列和近期活动",
  memories: "查看、修订、回收或永久删除长期记忆",
  candidates: "审核尚未固化的短期候选、评分和证据",
  dream: "检查 Light、REM、Deep 运行结果和模型调用",
  traces: "查看每条记忆为何被召回并注入上下文",
  settings: "配置兼容模型、密钥、调度和召回参数",
  maintenance: "创建和恢复备份，执行清理与索引重建",
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character],
  );
}

function localTime(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (options.method && options.method !== "GET") {
    headers["X-B1ack-Memory-Token"] = token;
  }
  const response = await fetch(base + path, { ...options, headers });
  if (!response.ok) {
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {
      detail = await response.text();
    }
    throw new Error(detail || response.statusText);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("json") ? response.json() : response.text();
}

function showNotice(message, error = false) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.className = error ? "error" : "";
  notice.style.display = "block";
  setTimeout(() => {
    notice.style.display = "none";
  }, 3500);
}

async function loadAll() {
  try {
    const bootstrap = await api("/bootstrap");
    token = bootstrap.token;
    await Promise.all([
      renderStatus(bootstrap.status),
      loadSettings(),
      loadMemories(),
      loadCandidates(),
      loadDreams(),
      loadModelCalls(),
      loadRecallTraces(),
      loadBackups(),
    ]);
  } catch (error) {
    showNotice(error.message, true);
  }
}

function renderStatus(status) {
  const counts = status.counts;
  const metrics = [
    ["长期记忆", counts.active_memories],
    ["待审核候选", counts.pending_candidates],
    ["待处理会话", counts.pending_turns],
    ["回收站", counts.trashed_memories],
  ];
  $("#metrics").innerHTML = metrics
    .map(([label, value]) => `<div class="metric"><span>${label}</span><b>${value}</b></div>`)
    .join("");
  $("#health").innerHTML = `
    <p>数据库：<b>${escapeHtml(status.database.integrity)}</b> · ${(status.database.bytes / 1024).toFixed(1)} KB</p>
    <p>模型：${escapeHtml(status.llm.model)} · Key ${status.llm.configured ? "已配置" : "未配置"}</p>
    <p>下次 Dream：${localTime(status.next_dream)}</p>
    <p>数据目录：<code>${escapeHtml(status.data_root)}</code></p>`;
}

async function loadSettings() {
  settings = await api("/settings");
  for (const section of ["llm", "embedding", "dream", "recall", "retention"]) {
    const form = $(`#${section}-form`);
    if (!form) continue;
    for (const [key, value] of Object.entries(settings[section])) {
      const input = form.elements[key];
      if (!input) continue;
      if (input.type === "checkbox") input.checked = Boolean(value);
      else input.value = value ?? "";
    }
  }
  $("#llm-key").textContent = `Key：${settings.secrets.llm_api_key.masked || "未配置"}`;
  $("#embedding-key").textContent =
    `Key：${settings.secrets.embedding_api_key.masked || "未配置"}`;
}

async function loadMemories() {
  const status = $("#memory-status").value;
  const rows = await api(`/memories?status=${encodeURIComponent(status)}`);
  memoriesById = new Map(rows.map((item) => [item.id, item]));
  const query = $("#memory-search").value.toLowerCase();
  const visible = rows.filter((item) => item.content.toLowerCase().includes(query));
  $("#memory-list").innerHTML =
    visible
      .map((item) => {
        const actions =
          status === "active"
            ? `<button class="ghost" data-action="edit-memory" data-id="${item.id}">编辑</button>
               <button class="danger" data-action="trash-memory" data-id="${item.id}">回收</button>`
            : `<button data-action="restore-memory" data-id="${item.id}">恢复</button>
               <button class="danger" data-action="purge-memory" data-id="${item.id}">永久删除</button>`;
        return `<div class="card">
          <div>
            <p>${escapeHtml(item.content)}</p>
            <div class="meta"><span class="badge">${escapeHtml(item.kind)}</span> · ${escapeHtml(item.origin)} · ${localTime(item.updated_at)} · ${item.id}</div>
          </div>
          <div class="actions">${actions}</div>
        </div>`;
      })
      .join("") || "<div class='callout'>没有记录</div>";
}

async function loadCandidates() {
  const rows = await api("/candidates");
  $("#candidate-list").innerHTML =
    rows
      .map((item) => {
        const conflict = item.conflict_reason
          ? `<div class="conflict">冲突：${escapeHtml(item.conflict_reason)}</div>`
          : "";
        const details = escapeHtml(
          JSON.stringify({ score: item.score_components, evidence: item.evidence }, null, 2),
        );
        return `<div class="card">
          <div>
            <p>${escapeHtml(item.content)}</p>
            <div class="meta"><span class="badge">${escapeHtml(item.kind)}</span> · 评分 ${Number(item.score).toFixed(2)} · 证据天数 ${item.evidence_days} · 召回 ${item.recall_count} 次 / ${item.unique_query_count} 种查询</div>
            ${conflict}
            <details><summary>评分与证据（${item.evidence.length}）</summary><pre>${details}</pre></details>
          </div>
          <div class="actions">
            <button data-action="promote-candidate" data-id="${item.id}">晋升</button>
            <button class="danger" data-action="reject-candidate" data-id="${item.id}">拒绝</button>
          </div>
        </div>`;
      })
      .join("") || "<div class='callout'>没有待审核候选</div>";
}

async function loadDreams() {
  const rows = await api("/dream-runs");
  $("#dream-list").innerHTML =
    rows
      .map(
        (item) => `<div class="card">
          <div>
            <p><b>${escapeHtml(item.status)}</b> · ${localTime(item.started_at)}</p>
            <div class="meta">输入 ${item.input_count} · 候选 ${item.candidate_count} · 晋升 ${item.promoted_count} · Token ${item.input_tokens}/${item.output_tokens}</div>
            ${item.error ? `<pre>${escapeHtml(item.error)}</pre>` : ""}
          </div>
          <span class="badge">${escapeHtml(item.id.slice(0, 8))}</span>
        </div>`,
      )
      .join("") || "<div class='callout'>尚无 Dream 运行记录</div>";
}

async function loadModelCalls() {
  const rows = await api("/model-calls");
  $("#call-list").innerHTML =
    rows
      .map((item) => {
        const detail = escapeHtml(
          JSON.stringify(
            { request: item.request_json, response: item.response_json, error: item.error },
            null,
            2,
          ),
        );
        return `<div class="card"><div>
          <p><b>${escapeHtml(item.phase)}</b> · ${escapeHtml(item.model)} · ${localTime(item.created_at)}</p>
          <div class="meta">Token ${item.input_tokens}/${item.output_tokens} · Dream ${escapeHtml(item.dream_run_id)}</div>
          <details><summary>查看请求与响应</summary><pre>${detail}</pre></details>
        </div></div>`;
      })
      .join("") || "<div class='callout'>尚无模型调用</div>";
}

async function loadRecallTraces() {
  const rows = await api("/recall-traces");
  const body = rows
    .map(
      (item) => `<tr>
        <td>${localTime(item.created_at)}</td>
        <td>${escapeHtml(item.query_text)}</td>
        <td>${escapeHtml(item.source)}</td>
        <td>${escapeHtml(item.record_id)}</td>
        <td>${item.keyword_rank ?? "—"} / ${Number(item.final_score).toFixed(3)}</td>
        <td>${item.injected ? "是" : "否"}</td>
      </tr>`,
    )
    .join("");
  $("#trace-list").innerHTML = `<table>
    <thead><tr><th>时间</th><th>查询</th><th>来源</th><th>记录</th><th>关键词排名/最终分数</th><th>注入</th></tr></thead>
    <tbody>${body}</tbody>
  </table>`;
}

async function loadBackups() {
  const rows = await api("/backups");
  $("#backup-list").innerHTML =
    rows
      .map(
        (item) => `<div class="card">
          <div><p>${escapeHtml(item.name)}</p><div class="meta">${(item.bytes / 1024).toFixed(1)} KB · ${localTime(item.modified * 1000)}</div></div>
          <button class="danger" data-action="restore-backup" data-name="${escapeHtml(item.name)}">恢复此备份</button>
        </div>`,
      )
      .join("") || "<div class='callout'>尚无备份</div>";
}

async function mutate(path, body = {}, method = "POST") {
  const result = await api(path, { method, body: JSON.stringify(body) });
  showNotice("操作完成");
  await loadAll();
  return result;
}

function openEditor(memory = null) {
  editing = memory;
  $("#edit-content").value = memory?.content || "";
  $("#edit-kind").value = memory?.kind || "fact";
  $("#editor-title").textContent = memory ? "编辑记忆" : "新增记忆";
  $("#editor").showModal();
}

async function handleAction(button) {
  const action = button.dataset.action;
  const id = button.dataset.id;
  if (action === "edit-memory") openEditor(memoriesById.get(id));
  else if (action === "trash-memory") await mutate(`/memories/${id}/trash`);
  else if (action === "restore-memory") await mutate(`/memories/${id}/restore`);
  else if (action === "purge-memory") {
    if (confirm("永久删除会清除关联会话、Dream 日志和全部旧备份，无法撤销。继续？")) {
      await mutate(`/memories/${id}`, {}, "DELETE");
    }
  } else if (action === "promote-candidate") {
    await mutate(`/candidates/${id}/promote`);
  } else if (action === "reject-candidate") {
    await mutate(`/candidates/${id}/reject`);
  } else if (action === "restore-backup") {
    if (confirm("将先保存当前状态，再恢复所选备份。继续？")) {
      await mutate(`/backups/${encodeURIComponent(button.dataset.name)}/restore`);
    }
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  try {
    if (button.dataset.page) {
      document.querySelectorAll("nav button,.page").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      $(`#${button.dataset.page}`).classList.add("active");
      $("#title").textContent = button.textContent;
      $("#subtitle").textContent = pageDescriptions[button.dataset.page];
    } else if (button.dataset.action) await handleAction(button);
    else if (button.id === "refresh") await loadAll();
    else if (button.id === "run-dream") await mutate("/dream/run");
    else if (button.id === "dry-dream") await mutate("/dream/run", { dry_run: true });
    else if (button.id === "create-backup") await mutate("/backup");
    else if (button.id === "rebuild") await mutate("/rebuild");
    else if (button.id === "rebuild-vector") await mutate("/rebuild", { embeddings: true });
    else if (button.id === "vacuum") {
      if (confirm("将清理过期数据并压缩数据库，继续？")) {
        await mutate("/maintenance", { vacuum: true, cleanup: true });
      }
    } else if (button.dataset.test) {
      await mutate("/model/test", { kind: button.dataset.test });
    } else if (button.id === "add-memory") openEditor();
  } catch (error) {
    showNotice(error.message, true);
  }
});

$("#editor").addEventListener("close", async () => {
  if ($("#editor").returnValue !== "save") return;
  const body = { content: $("#edit-content").value, kind: $("#edit-kind").value };
  try {
    if (editing) await mutate(`/memories/${editing.id}`, body, "PATCH");
    else await mutate("/memories", body);
  } catch (error) {
    showNotice(error.message, true);
  }
});

for (const section of ["llm", "embedding", "dream", "recall", "retention"]) {
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
        await api(`/secrets/${secretName}`, {
          method: "POST",
          body: JSON.stringify({ value: secretInput.value }),
        });
        secretInput.value = "";
      }
      showNotice("设置已保存");
      await loadAll();
    } catch (error) {
      showNotice(error.message, true);
    }
  });
}

$("#memory-search").addEventListener("input", loadMemories);
$("#memory-status").addEventListener("change", loadMemories);
$("#export").addEventListener("click", (event) => {
  event.currentTarget.href = `${base}/export`;
});

loadAll();
