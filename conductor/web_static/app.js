// Conductor Web UI — vanilla JS SPA
const ROLE_LABEL = { planner: "方案", coder: "执行", debugger: "排错", designer: "前端" };
const ICON = { pending: "⏸️", running: "🔄", done: "✅", failed: "❌", skipped: "⏭️" };

const S = {
  config: null,
  sessions: [],
  activeId: null,
  steps: {},          // title -> step view + runtime (preview, status, text)
  stepOrder: [],
  es: null,           // EventSource
  totalCost: 0,
};

const $ = (id) => document.getElementById(id);

// ---------- helpers ----------
function esc(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtCost(c) {
  if (c == null) return "";
  if (c < 0.01) return (c * 1000).toFixed(3) + "m$";
  return "$" + c.toFixed(4);
}

// ---------- init ----------
async function init() {
  bind();
  await loadConfig();
  await loadSessions();
  await loadMemory();
}

function bind() {
  $("btn-run").onclick = startRun;
  $("btn-mem").onclick = addMemory;
  $("sessions").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]");
    if (li) openSession(li.dataset.id, false);
  });
  $("memory").addEventListener("click", (e) => {
    if (e.target.classList.contains("del")) {
      delMemory(e.target.dataset.id);
    }
  });
}

async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    S.config = await r.json();
    renderBackends();
  } catch (e) { console.error(e); }
}

function renderBackends() {
  const box = $("backends");
  if (!S.config) return;
  let html = "";
  for (const [name, b] of Object.entries(S.config.backends)) {
    html += `<div class="bk"><span><span class="dot ${b.ready ? "ok" : "bad"}"></span>${name}</span>` +
            `<span>${b.model || b.type}</span></div>`;
  }
  box.innerHTML = html;
}

// ---------- sessions ----------
async function loadSessions() {
  try {
    const r = await fetch("/api/sessions");
    const data = await r.json();
    S.sessions = data.sessions || [];
    renderSessions();
  } catch (e) { console.error(e); }
}

function renderSessions() {
  const ul = $("sessions");
  if (!S.sessions.length) {
    ul.innerHTML = `<li class="muted small">还没有会话</li>`;
    return;
  }
  ul.innerHTML = S.sessions.map((s) =>
    `<li data-id="${s.id}" class="${s.id === S.activeId ? "active" : ""}">
       <div class="s-task">${esc(s.task)}</div>
       <div class="s-meta">
         <span>${s.status}</span><span>${s.steps}</span>
         <span style="color:var(--ok)">${fmtCost(s.cost_usd)}</span>
       </div>
     </li>`).join("");
}

async function openSession(id, live) {
  S.activeId = id;
  renderSessions();
  closeSSE();
  resetMain();
  try {
    const r = await fetch("/api/sessions/" + id);
    if (r.ok) {
      const s = await r.json();
      hydrateSession(s);
    }
  } catch (e) { console.error(e); }
  if (live) openSSE(id);
}

function hydrateSession(s) {
  $("active-task").textContent = s.task || "(无标题)";
  $("active-meta").textContent = `${s.status} · ${s.plan_source}`;
  S.totalCost = s.cost_total_usd || 0;
  $("active-cost").textContent = fmtCost(s.cost_total_usd);
  S.steps = {}; S.stepOrder = [];
  for (const r of (s.records || [])) {
    S.steps[r.title] = Object.assign({}, r, { preview: "", text: r.text || "" });
    S.stepOrder.push(r.title);
  }
  renderSteps();
  if (s.final) showFinal(s.final, s.verify_ok);
}

// ---------- run ----------
async function startRun() {
  const task = $("task").value.trim();
  $("run-err").textContent = "";
  if (!task) { $("run-err").textContent = "请输入任务描述"; return; }
  const body = {
    task,
    dry_run: $("opt-dryrun").checked,
    stream: $("opt-stream").checked,
    isolate: $("opt-isolate").checked,
    jobs: parseInt($("opt-jobs").value, 10) || 1,
  };
  $("btn-run").disabled = true;
  try {
    const r = await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "启动失败");
    S.activeId = data.session_id;
    resetMain();
    $("active-task").textContent = task;
    $("active-meta").textContent = "编排中…";
    openSSE(data.session_id);
    await loadSessions();
  } catch (e) {
    $("run-err").textContent = e.message;
  } finally {
    $("btn-run").disabled = false;
  }
}

function resetMain() {
  $("steps").innerHTML = "";
  $("final").classList.add("hidden");
  $("active-cost").textContent = "";
  S.steps = {}; S.stepOrder = []; S.totalCost = 0;
}

function closeSSE() {
  if (S.es) { S.es.close(); S.es = null; }
}

// ---------- SSE ----------
function openSSE(id) {
  closeSSE();
  const es = new EventSource("/api/sessions/" + id + "/events");
  S.es = es;
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    handleEvent(ev);
  };
  es.onerror = () => { /* keepalive/重连由浏览器处理 */ };
}

function handleEvent(ev) {
  switch (ev.type) {
    case "steps":
      S.stepOrder = (ev.steps || []).map((s) => s.title);
      S.steps = {};
      ev.steps.forEach((s) => {
        S.steps[s.title] = { title: s.title, role: s.role, instruction: s.instruction,
                             status: "pending", preview: "", text: "" };
      });
      renderSteps();
      break;
    case "step_start":
      setStep(ev.step.title, { status: "running" });
      break;
    case "step_delta":
      addPreview(ev.step.title, ev.text);
      break;
    case "step_done":
      setStep(ev.step.title, {
        status: ev.ok ? "done" : "failed",
        text: ev.text || "", error: ev.error,
        model: ev.model, usage: ev.usage, cost: ev.cost_usd,
      });
      if (ev.cost_usd) { S.totalCost += ev.cost_usd; $("active-cost").textContent = fmtCost(S.totalCost); }
      break;
    case "step_skip":
      setStep(ev.step.title, { status: "skipped" });
      break;
    case "isolate":
      setStep(ev.step.title, { isolated: true, worktree: ev.worktree });
      break;
    case "verify_done":
      flashMeta(ev.ok ? "✅ 校验通过" : "❌ 校验失败");
      break;
    case "debug_done":
      if (ev.text) addDebugNote(ev.text);
      break;
    case "report":
      showFinal(ev.report.final, ev.report.verify_ok);
      if (ev.report.cost_total_usd != null) {
        S.totalCost = ev.report.cost_total_usd;
        $("active-cost").textContent = fmtCost(S.totalCost);
      }
      loadSessions();
      break;
    case "snapshot":
      hydrateSession(ev.session);
      break;
    case "error":
      $("active-meta").textContent = "错误: " + ev.error;
      break;
    case "_end":
      closeSSE();
      $("active-meta").textContent = "完成";
      loadSessions();
      break;
  }
}

function flashMeta(t) { $("active-meta").textContent = t; }

// ---------- 渲染步骤 ----------
function setStep(title, patch) {
  let st = S.steps[title];
  if (!st) {
    st = S.steps[title] = { title, status: "pending", preview: "", text: "" };
    S.stepOrder.push(title);
  }
  Object.assign(st, patch);
  renderSteps();
}

function addPreview(title, text) {
  const st = S.steps[title];
  if (!st) return;
  st.preview = (st.preview + text).slice(-200);
  renderSteps();
}

function addDebugNote(text) {
  // debug 分析作为独立卡片追加
  const title = "🔎 debugger 分析";
  S.steps[title] = { title, role: "debugger", status: "done", text, preview: "" };
  if (!S.stepOrder.includes(title)) S.stepOrder.push(title);
  renderSteps();
}

function renderSteps() {
  const box = $("steps");
  if (!S.stepOrder.length) { box.innerHTML = `<div class="muted">等待 planner 产出计划…</div>`; return; }
  box.innerHTML = S.stepOrder.map(renderStepCard).join("");
}

function renderStepCard(title) {
  const s = S.steps[title];
  if (!s) return "";
  const role = s.role || "planner";
  const cls = s.status || "pending";
  const tokens = s.usage ? `${s.usage.input_tokens}↑${s.usage.output_tokens}↓` : "";
  const stats = [
    s.model ? `<span>${esc(s.model)}</span>` : "",
    tokens ? `<span>${tokens}</span>` : "",
    s.cost != null ? `<span class="cost">${fmtCost(s.cost)}</span>` : "",
  ].join("");
  const body = s.text
    ? `<div class="step-text">${esc(s.text)}</div>`
    : (s.preview ? `<div class="step-preview">${esc(s.preview)}…</div>` : "");
  const err = s.error ? `<div class="step-text" style="color:var(--bad)">${esc(s.error)}</div>` : "";
  const badge = s.isolated ? `<span class="badge">worktree 隔离</span>` : "";
  return `<div class="step ${cls}">
    <div class="step-head">
      <span class="step-icon">${ICON[cls] || "•"}</span>
      <span class="role-tag ${role}">${ROLE_LABEL[role] || role}</span>
      <span class="step-title">${esc(s.title || title)}${badge}</span>
      <span class="step-stats">${stats}</span>
    </div>
    ${s.instruction ? `<div class="step-instr">${esc(s.instruction)}</div>` : ""}
    ${body}${err}
  </div>`;
}

function showFinal(text, verifyOk) {
  const el = $("final");
  el.textContent = text || "";
  el.classList.remove("hidden", "bad");
  if (verifyOk === false) el.classList.add("bad");
}

// ---------- 记忆 ----------
async function loadMemory() {
  try {
    const r = await fetch("/api/memory");
    const data = await r.json();
    renderMemory(data.items || []);
  } catch (e) { console.error(e); }
}

function renderMemory(items) {
  const ul = $("memory");
  if (!items.length) { ul.innerHTML = `<li class="muted small">暂无记忆</li>`; return; }
  ul.innerHTML = items.map((m) =>
    `<li><span class="del" data-id="${m.id}">×</span>
       <span class="mk">${esc(m.key)}</span><span class="mscope">${m.scope}</span>
       <div class="muted">${esc(m.content)}</div>
     </li>`).join("");
}

async function addMemory() {
  const key = $("mem-key").value.trim();
  const content = $("mem-content").value.trim();
  if (!key || !content) return;
  await fetch("/api/memory", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, content, scope: $("mem-scope").value, tags: [] }),
  });
  $("mem-key").value = ""; $("mem-content").value = "";
  loadMemory();
}

async function delMemory(id) {
  await fetch("/api/memory/" + id, { method: "DELETE" });
  loadMemory();
}

init();
