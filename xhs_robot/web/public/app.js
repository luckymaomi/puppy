"use strict";

const params = new URLSearchParams(window.location.search);
const token = params.get("token") || sessionStorage.getItem("xhs-console-token") || "";
if (token) {
  sessionStorage.setItem("xhs-console-token", token);
  history.replaceState(null, "", "/");
}

const state = {
  bootstrap: null,
  gateCheck: null,
  logs: [],
  configHydrated: false,
  configDirty: false,
  refreshQueued: false,
  lastSequence: 0,
  selectedTaskId: null,
  pendingResumeTaskId: null,
};

const statusNames = {
  created: "已创建",
  running: "运行中",
  waiting_login: "等待登录",
  waiting_approval: "等待批准",
  waiting_human: "等待人工处理",
  paused: "已暂停",
  complete: "已完成",
  failed: "失败",
  stopped: "已停止",
};

const eventNames = {
  connected: "事件流已连接",
  configuration_updated: "AI 配置已更新",
  browser_updated: "浏览器状态已更新",
  browser_profile_created: "浏览器资料已创建",
  task_started: "任务已启动",
  task_updated: "任务状态已更新",
  task_finished: "任务执行线程已停止",
  task_execution_started: "执行器已进入真实页面流程",
  task_execution_stopped: "执行器已停止",
  task_completed: "任务已达到目标",
};

function element(id) {
  return document.getElementById(id);
}

async function api(path, options = {}) {
  if (!token) throw new Error("控制台访问凭证缺失，请重新运行 python app.py");
  const headers = new Headers(options.headers || {});
  headers.set("x-xhs-token", token);
  if (options.body) headers.set("content-type", "application/json");
  const response = await fetch(path, {...options, headers});
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(payload?.error || payload?.detail || `请求失败 (${response.status})`);
  return payload;
}

function taskTone(status) {
  if (["running", "complete"].includes(status)) return "ok";
  if (["waiting_login", "waiting_approval", "waiting_human", "paused"].includes(status)) return "warn";
  if (["failed", "stopped"].includes(status)) return "error";
  return "";
}

function setPill(node, text, tone = "") {
  node.className = `status-pill ${tone}`.trim();
  node.innerHTML = `<i></i>${escapeHtml(text)}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatTime(value) {
  if (!value) return "-";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN", {hour12: false});
}

function setBusy(button, busy, busyText = "处理中…") {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = busyText;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.label || button.textContent;
    button.disabled = false;
  }
}

function toast(message, error = false) {
  const node = element("toast");
  node.textContent = message;
  node.className = `toast ${error ? "error" : ""}`.trim();
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 3800);
}

function eventMessage(payload) {
  const shortText = (value, limit = 90) => {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > limit ? `${text.slice(0, limit)}…` : text;
  };
  switch (payload.type) {
    case "browser_started": return `专用浏览器已启动 · PID ${payload.pid || "-"}`;
    case "browser_stopped": return payload.paused_task_id ? `专用浏览器已停止 · 任务 ${payload.paused_task_id} 已暂停` : "专用浏览器已停止";
    case "browser_profile_created": return `已新建浏览器资料 · ${payload.profile_name}`;
    case "page_gate": return payload.gate === "ready" ? "页面检查通过" : `页面需要人工处理 · ${payload.reason || payload.gate}`;
    case "search_submitted": return `已提交关键词搜索 · ${payload.keyword}`;
    case "search_complete": return `搜索结果已加载 · ${payload.keyword} · ${payload.result_count} 篇`;
    case "notes_observed": return `识别到 ${payload.count} 篇可见笔记`;
    case "results_scrolled": return `滚动搜索结果 · ${payload.before_top} → ${payload.after_top}`;
    case "note_opened": return `已打开笔记 · ${payload.note_id}`;
    case "note_context": return `已读取笔记正文与 ${payload.comment_count} 条评论 · ${payload.note_id}`;
    case "generation_requested": return `正在生成${payload.kind === "reply" ? "回复" : "评论"} · ${payload.note_id}`;
    case "draft_ready": return `${payload.kind === "reply" ? "回复" : "评论"}草稿已生成 · ${shortText(payload.text)}`;
    case "draft_reviewed": return `${payload.kind === "reply" ? "回复" : "评论"}草稿已${payload.action === "skip" ? "跳过" : "批准"}`;
    case "reply_activated": return `已定位回复目标 · ${payload.comment_id}`;
    case "write_dispatched": return `${payload.kind === "reply" ? "回复" : "评论"}已点击发送 · ${shortText(payload.text)}`;
    case "write_resolved": return `未决写入已确认为${payload.result === "sent" ? "已发送" : "未发送"}`;
    case "note_closed": return `已关闭笔记并返回搜索结果 · ${payload.note_id}`;
    case "note_skipped": return `已跳过笔记 · ${payload.note_id}`;
    case "capture_failed": return `页面证据截图失败 · ${payload.error || "未知错误"}`;
    case "task_execution_started": return `开始执行 · ${payload.keyword} · ${payload.max_notes} 篇 · ${payload.send_mode === "auto" ? "自动模式" : "批准模式"}`;
    case "task_execution_stopped": return `执行停止 · ${shortText(payload.message || payload.error_type)}`;
    case "task_completed": return `任务完成 · ${payload.processed_count} 篇 · ${payload.comment_count} 条评论 · ${payload.reply_count} 条回复`;
    case "task_updated": return `任务状态更新 · ${statusNames[payload.status] || payload.status}${payload.reason ? ` · ${payload.reason}` : ""}`;
    case "task_finished": return `任务线程结束 · ${statusNames[payload.status] || payload.status || "未知状态"}${payload.reason ? ` · ${payload.reason}` : ""}`;
    default: return eventNames[payload.type] || payload.type;
  }
}

function logEvent(type, message, taskId = "-", time = null, sequence = null) {
  if (sequence && sequence <= state.lastSequence) return;
  if (sequence) state.lastSequence = sequence;
  state.logs.unshift({time: time ? new Date(time) : new Date(), type, message, taskId, sequence});
  state.logs = state.logs.slice(0, 120);
  renderLogs();
}

function ingestEvent(payload) {
  if (!payload || payload.type === "heartbeat") return;
  logEvent(
    payload.type,
    eventMessage(payload),
    payload.task_id || "-",
    payload.time,
    payload.sequence || null,
  );
}

async function refreshBootstrap() {
  const snapshot = await api("/api/bootstrap");
  if (snapshot.supervisor.running) {
    state.selectedTaskId = snapshot.supervisor.task_id;
  }
  if (state.selectedTaskId) {
    const selected = snapshot.tasks.find((task) => task.id === state.selectedTaskId);
    if (selected) snapshot.current_task = selected;
    else state.selectedTaskId = null;
  }
  state.bootstrap = snapshot;
  (snapshot.events || []).forEach(ingestEvent);
  render();
}

function profileName(profileId) {
  const profile = state.bootstrap?.browser_profiles?.find((item) => item.id === profileId);
  return profile?.name || profileId || "-";
}

function queueRefresh() {
  if (state.refreshQueued) return;
  state.refreshQueued = true;
  setTimeout(async () => {
    state.refreshQueued = false;
    try { await refreshBootstrap(); } catch (error) { toast(error.message, true); }
  }, 120);
}

function render() {
  if (!state.bootstrap) return;
  const {configuration, browser, browser_profiles: profiles, supervisor, current_task: current, tasks} = state.bootstrap;
  const configValues = configuration.values;

  setPill(element("provider-status"), configuration.ready ? `${configValues.XHS_PROVIDER} 已就绪` : "AI 未就绪", configuration.ready ? "ok" : "error");
  setPill(element("browser-status"), browser.running ? `${browser.profile_name || "浏览器"} · 运行中` : "浏览器未启动", browser.running ? "ok" : "warn");
  setPill(element("task-status"), current ? statusNames[current.status] || current.status : "无活动任务", current ? taskTone(current.status) : "");

  element("runtime-browser").textContent = browser.running ? `运行中 · ${browser.cdp_port || "CDP"}` : "未启动";
  element("runtime-profile").textContent = browser.profile_name || "-";
  element("runtime-provider").textContent = configuration.ready ? configValues.XHS_PROVIDER : "未配置";
  element("runtime-model").textContent = configValues.XHS_MODEL || "-";
  element("runtime-worker").textContent = supervisor.running ? `执行中 · ${supervisor.task_id}` : "空闲";
  element("config-current-provider").textContent = configValues.XHS_PROVIDER || "-";
  element("config-current-model").textContent = configValues.XHS_MODEL || "-";
  element("config-current-key").textContent = configuration.api_key_present ? "已配置" : "未配置";
  element("browser-check").disabled = !browser.running;
  element("browser-reuse").disabled = browser.running || !profiles.length;
  element("browser-create").disabled = browser.running;
  element("browser-stop").disabled = !browser.running;
  const hasProfileTask = browser.profile_id && tasks.some((task) => task.config.profile_id === browser.profile_id && !["complete", "failed", "stopped"].includes(task.status));
  element("task-form").querySelector("button[type=submit]").disabled = supervisor.running || !configuration.ready || !browser.running || hasProfileTask;

  renderBrowserProfiles(profiles, browser);
  renderCurrentTask(current, browser, supervisor);
  renderTaskTable(tasks);
  renderHome(configuration, browser, current);
  hydrateConfig(configuration);
  renderAttention(current, browser);
}

function renderBrowserProfiles(profiles, browser) {
  const select = element("browser-profile");
  const previous = select.value;
  select.replaceChildren(...profiles.map((profile) => {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.name;
    return option;
  }));
  const preferred = browser.profile_id || (profiles.some((item) => item.id === previous) ? previous : profiles[0]?.id);
  if (preferred) select.value = preferred;
  select.disabled = browser.running || !profiles.length;
  element("browser-profile-name").disabled = browser.running;
}

function renderHome(configuration, browser, task) {
  const values = configuration.values;
  element("home-config-state").textContent = configuration.ready ? "已就绪" : "需配置";
  element("home-config-summary").textContent = configuration.ready
    ? `${values.XHS_PROVIDER} · ${values.XHS_MODEL}`
    : (configuration.error || "Provider 与模型待配置");
  element("home-config-path").textContent = configuration.file;
  element("home-browser-state").textContent = browser.running ? "运行中" : "未启动";
  element("home-browser-summary").textContent = browser.running
    ? `${browser.profile_name} · PID ${browser.browser_pid || "-"}`
    : "Chromium 未连接";
  element("home-task-state").textContent = task ? (statusNames[task.status] || task.status) : "空闲";
  element("home-task-summary").textContent = task
    ? `${task.config.keyword} · ${task.processed_note_ids.length}/${task.config.max_notes} 篇 · ${task.config.send_mode === "auto" ? "自动模式" : "批准模式"}`
    : "没有进行中的任务";
}

function renderCurrentTask(task, browser, supervisor) {
  element("empty-task").hidden = Boolean(task);
  element("task-detail").hidden = !task;
  const badge = element("current-state");
  badge.textContent = task ? (statusNames[task.status] || task.status) : "空闲";
  badge.className = `state-badge ${task ? taskTone(task.status) : ""}`.trim();
  if (!task) return;

  const total = task.config.max_notes;
  const done = task.processed_note_ids.length;
  const progress = total > 0 ? Math.min(100, Math.round(done / total * 100)) : 0;
  element("current-keyword").textContent = task.config.keyword;
  element("current-id").textContent = task.id;
  element("task-progress").style.width = `${progress}%`;
  element("metric-notes").textContent = `${done} / ${total}`;
  element("metric-comments").textContent = task.comment_count;
  element("metric-replies").textContent = task.reply_count;
  element("metric-mode").textContent = task.config.send_mode === "auto" ? "自动模式" : "批准模式";
  element("metric-profile").textContent = profileName(task.config.profile_id);
  const reason = element("stop-reason");
  const resolutionHint = task.write_in_flight && (!browser.running || browser.profile_id !== task.config.profile_id)
    ? `上次写入待确认；请启动 ${profileName(task.config.profile_id)}`
    : null;
  reason.hidden = !(task.stop_reason || task.last_error || resolutionHint);
  reason.textContent = [task.stop_reason, task.last_error, resolutionHint].filter(Boolean).join(" · ");

  const terminal = ["complete", "failed", "stopped"].includes(task.status);
  element("task-pause").disabled = task.status !== "running";
  const approvalHasBrowser = task.status === "waiting_approval" && browser.running && browser.profile_id === task.config.profile_id;
  const executingThisTask = supervisor.running && supervisor.task_id === task.id;
  element("task-resume").disabled = terminal || supervisor.running || executingThisTask || approvalHasBrowser || Boolean(task.write_in_flight);
  element("task-stop").disabled = terminal;
}

function renderTaskTable(tasks) {
  const body = element("task-table");
  if (!tasks.length) {
    body.innerHTML = '<tr><td class="empty-row" colspan="8">暂无任务</td></tr>';
    return;
  }
  body.innerHTML = tasks.map((task) => `
    <tr>
      <td><code>${escapeHtml(task.id)}</code></td>
      <td>${escapeHtml(profileName(task.config.profile_id))}</td>
      <td>${escapeHtml(task.config.keyword)}</td>
      <td><span class="state-badge ${taskTone(task.status)}">${escapeHtml(statusNames[task.status] || task.status)}</span></td>
      <td>${task.comment_count}</td>
      <td>${task.reply_count}</td>
      <td>${escapeHtml(formatTime(task.updated_at))}</td>
      <td><button class="button secondary task-select" type="button" data-task-id="${escapeHtml(task.id)}">查看</button></td>
    </tr>`).join("");
  body.querySelectorAll(".task-select").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedTaskId = button.dataset.taskId;
      state.pendingResumeTaskId = null;
      state.bootstrap.current_task = tasks.find((task) => task.id === state.selectedTaskId) || null;
      render();
      window.scrollTo({top: 0, behavior: "smooth"});
    });
  });
}

function hydrateConfig(configuration, force = false) {
  if ((state.configHydrated && !force) || state.configDirty) return;
  const form = element("config-form");
  Object.entries(configuration.values).forEach(([key, value]) => {
    if (key === "XHS_API_KEY") return;
    const field = form.elements.namedItem(key);
    if (field) field.value = value;
  });
  form.elements.namedItem("XHS_API_KEY").value = "";
  element("key-state").textContent = configuration.api_key_present ? "已配置；留空保存将保留原密钥" : "未配置";
  element("config-file").textContent = configuration.file;
  state.configHydrated = true;
  state.configDirty = false;
}

function renderAttention(task, browser) {
  const taskBrowserRunning = Boolean(
    task && browser.running && task.config.profile_id === browser.profile_id,
  );
  if (task?.write_in_flight && taskBrowserRunning) return showWriteResolution(task);
  if (!browser.running) {
    state.gateCheck = null;
    return closeModal();
  }
  if (task?.status === "waiting_approval" && task.pending_draft && taskBrowserRunning) return showApproval(task);
  if (taskBrowserRunning && ["waiting_login", "waiting_human"].includes(task.status)) return showIntervention(task);
  if (state.gateCheck && state.gateCheck.gate !== "ready") return showGateCheck(state.gateCheck, task, browser);
  closeModal();
}

function modalBase({tone = "", eyebrow, title, message}) {
  element("modal-symbol").className = `modal-symbol ${tone}`.trim();
  element("modal-eyebrow").textContent = eyebrow;
  element("modal-title").textContent = title;
  element("modal-message").textContent = message;
  element("modal-context").hidden = true;
  element("approval-text").hidden = true;
  element("modal-actions").replaceChildren();
  element("modal").hidden = false;
}

function actionButton(label, className, callback) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${className}`;
  button.textContent = label;
  button.addEventListener("click", callback);
  return button;
}

function showIntervention(task) {
  const isLogin = task.status === "waiting_login";
  modalBase({
    eyebrow: "HUMAN CHECKPOINT",
    title: isLogin ? "登录或验证待处理" : "检测到验证码或安全校验",
    message: task.stop_reason || "请在小红书浏览器中完成人工处理。",
  });
  const actions = element("modal-actions");
  actions.append(actionButton("暂停任务", "secondary", () => taskAction("pause")));
  actions.append(actionButton("已处理，继续", "primary", resumeAfterHuman));
}

function showGateCheck(gate, task, browser) {
  const continuingTask = Boolean(
    task
    && state.pendingResumeTaskId === task.id
    && browser.profile_id === task.config.profile_id,
  );
  modalBase({
    eyebrow: "BROWSER CHECK",
    title: gate.gate === "login" ? "需要登录" : "检测到验证码或安全校验",
    message: gate.reason || "请在小红书浏览器中处理后重新检查。",
  });
  element("modal-actions").append(actionButton(
    continuingTask ? "已处理，继续" : "已处理，重新检查",
    "primary",
    continuingTask ? resumeAfterHuman : checkBrowser,
  ));
}

function showApproval(task) {
  const draft = task.pending_draft;
  const limit = draft.kind === "comment" ? 120 : 80;
  modalBase({
    tone: "approval",
    eyebrow: "DRAFT APPROVAL",
    title: draft.kind === "comment" ? "批准笔记评论" : "批准评论回复",
    message: `发送前批准 · 最多 ${limit} 个字符`,
  });
  const context = element("modal-context");
  context.hidden = false;
  context.textContent = draft.target_text || `笔记 ${draft.note_id}`;
  const textarea = element("approval-text");
  textarea.hidden = false;
  textarea.maxLength = limit;
  textarea.value = draft.text;
  const actions = element("modal-actions");
  actions.append(actionButton("暂停", "secondary", () => approveTask("pause")));
  actions.append(actionButton("跳过", "quiet-danger", () => approveTask("skip")));
  actions.append(actionButton("批准并发送", "primary", () => approveTask("edit", textarea.value)));
}

function showWriteResolution(task) {
  const draft = task.write_in_flight;
  modalBase({
    tone: "uncertain",
    eyebrow: "WRITE RESULT",
    title: "上次写入结果待确认",
    message: "请查看小红书页面，确认内容是否已经发送。系统不会自动重试。",
  });
  const context = element("modal-context");
  context.hidden = false;
  context.textContent = draft.text;
  const actions = element("modal-actions");
  actions.append(actionButton("未发送", "secondary", () => resolveWrite("not-sent")));
  actions.append(actionButton("已发送", "primary", () => resolveWrite("sent")));
}

function closeModal() {
  element("modal").hidden = true;
}

async function withButton(button, work, label) {
  setBusy(button, true, label);
  try { return await work(); }
  catch (error) { toast(error.message, true); throw error; }
  finally { setBusy(button, false); }
}

async function checkBrowser(event) {
  const button = event?.currentTarget instanceof HTMLButtonElement ? event.currentTarget : null;
  try {
    if (button) setBusy(button, true, "检查中…");
    state.gateCheck = await api("/api/browser/check", {method: "POST"});
    if (state.gateCheck.gate === "ready") toast("浏览器登录状态正常");
    renderAttention(state.bootstrap?.current_task, state.bootstrap?.browser || {running: false});
    return state.gateCheck;
  } catch (error) { toast(error.message, true); return null; }
  finally { if (button) setBusy(button, false); }
}

async function startBrowserProfile(profileId, button, {preserveTask = false} = {}) {
  await withButton(
    button,
    () => api("/api/browser/start", {method: "POST", body: JSON.stringify({profile_id: profileId})}),
    "启动中…",
  );
  if (!preserveTask) {
    state.selectedTaskId = null;
    state.pendingResumeTaskId = null;
  }
  state.gateCheck = null;
  await refreshBootstrap();
  await checkBrowser();
}

async function ensureTaskBrowser(task) {
  const browser = state.bootstrap?.browser || {running: false};
  if (browser.running && browser.profile_id !== task.config.profile_id) {
    throw new Error(`请先停止 ${browser.profile_name || "当前浏览器"}，再继续此任务`);
  }
  if (!browser.running) {
    await api("/api/browser/start", {
      method: "POST",
      body: JSON.stringify({profile_id: task.config.profile_id}),
    });
    await refreshBootstrap();
  }
  state.gateCheck = await api("/api/browser/check", {method: "POST"});
  renderAttention(state.bootstrap?.current_task, state.bootstrap?.browser || {running: false});
  return state.gateCheck.gate === "ready";
}

async function resumeAfterHuman(event) {
  const task = state.bootstrap?.current_task;
  if (!task) return checkBrowser(event);
  const button = event.currentTarget;
  try {
    setBusy(button, true, "重新检查中…");
    await api(`/api/tasks/${task.id}/resume`, {method: "POST"});
    state.gateCheck = null;
    state.pendingResumeTaskId = null;
    await refreshBootstrap();
    toast("页面检查通过，任务已继续");
  } catch (error) {
    toast(error.message, true);
    await refreshBootstrap().catch(() => {});
  } finally { setBusy(button, false); }
}

async function taskAction(action, button = null) {
  const task = state.bootstrap?.current_task;
  if (!task) return;
  try {
    if (button) setBusy(button, true, action === "resume" ? "继续中…" : "处理中…");
    if (action === "resume") {
      state.gateCheck = null;
      state.pendingResumeTaskId = task.id;
      if (!await ensureTaskBrowser(task)) return;
    }
    await api(`/api/tasks/${task.id}/${action}`, {method: "POST"});
    if (action === "resume") state.pendingResumeTaskId = null;
    await refreshBootstrap();
  } catch (error) {
    if (action === "resume" && (!state.gateCheck || state.gateCheck.gate === "ready")) {
      state.pendingResumeTaskId = null;
    }
    toast(error.message, true);
  }
  finally { if (button) setBusy(button, false); }
}

async function approveTask(action, text = null) {
  const task = state.bootstrap?.current_task;
  if (!task) return;
  try {
    await api(`/api/tasks/${task.id}/approval`, {method: "POST", body: JSON.stringify({action, text})});
    await refreshBootstrap();
  } catch (error) { toast(error.message, true); }
}

async function resolveWrite(result) {
  const task = state.bootstrap?.current_task;
  if (!task) return;
  try {
    await api(`/api/tasks/${task.id}/resolve`, {method: "POST", body: JSON.stringify({result})});
    await refreshBootstrap();
    toast("写入结果已记录，任务保持暂停");
  } catch (error) { toast(error.message, true); }
}

function openWorkflow(name, scrollTarget = null) {
  element("workflow-home").hidden = true;
  element("workflow-detail").hidden = false;
  document.querySelectorAll("[data-workflow-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.workflowPanel !== name;
  });
  window.scrollTo({top: 0, behavior: "instant"});
  if (scrollTarget) requestAnimationFrame(() => element(scrollTarget)?.scrollIntoView({block: "start"}));
}

function bindWorkflowNavigation() {
  document.querySelectorAll("[data-open-workflow]").forEach((button) => {
    button.addEventListener("click", () => openWorkflow(button.dataset.openWorkflow, button.dataset.scrollTarget));
  });
  element("back-home").addEventListener("click", () => {
    element("workflow-detail").hidden = true;
    element("workflow-home").hidden = false;
    document.querySelectorAll("[data-workflow-panel]").forEach((panel) => { panel.hidden = true; });
    window.scrollTo({top: 0, behavior: "instant"});
  });
}

function bindActions() {
  element("browser-reuse").addEventListener("click", async (event) => {
    try {
      const profileId = element("browser-profile").value;
      if (!profileId) throw new Error("请选择已有浏览器资料");
      await startBrowserProfile(profileId, event.currentTarget);
    } catch (_) {}
  });
  element("browser-create").addEventListener("click", async (event) => {
    const nameInput = element("browser-profile-name");
    const name = nameInput.value.trim();
    if (!name) {
      toast("请输入新浏览器名称", true);
      return;
    }
    try {
      const profile = await withButton(
        event.currentTarget,
        () => api("/api/browser/profiles", {method: "POST", body: JSON.stringify({name})}),
        "创建中…",
      );
      nameInput.value = "";
      await startBrowserProfile(profile.id, event.currentTarget);
    } catch (_) {}
  });
  element("browser-stop").addEventListener("click", async (event) => {
    try {
      const result = await withButton(event.currentTarget, () => api("/api/browser/stop", {method: "POST"}), "停止中…");
      state.gateCheck = null;
      state.pendingResumeTaskId = null;
      await refreshBootstrap();
      toast(result.paused_task ? "浏览器已停止，当前任务已暂停" : "浏览器已停止");
    } catch (_) {}
  });
  element("browser-check").addEventListener("click", checkBrowser);
  element("refresh-tasks").addEventListener("click", () => refreshBootstrap().catch((error) => toast(error.message, true)));
  element("task-pause").addEventListener("click", (event) => taskAction("pause", event.currentTarget));
  element("task-resume").addEventListener("click", (event) => taskAction("resume", event.currentTarget));
  element("task-stop").addEventListener("click", (event) => taskAction("stop", event.currentTarget));

  element("task-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const payload = {
      keyword: data.get("keyword"),
      max_notes: Number(data.get("max_notes")),
      replies_min: Number(data.get("replies_min")),
      replies_max: Number(data.get("replies_max")),
      send_mode: data.get("send_mode"),
      min_delay: Number(data.get("min_delay")),
      max_delay: Number(data.get("max_delay")),
      daily_write_limit: Number(data.get("daily_write_limit")),
    };
    const submit = form.querySelector("button[type=submit]");
    try {
      const task = await withButton(submit, async () => {
        state.gateCheck = await api("/api/browser/check", {method: "POST"});
        if (state.gateCheck.gate !== "ready") {
          renderAttention(state.bootstrap?.current_task, state.bootstrap?.browser || {running: false});
          return null;
        }
        return api("/api/tasks", {method: "POST", body: JSON.stringify(payload)});
      }, "检查中…");
      if (!task) return;
      state.selectedTaskId = task.id;
      form.elements.namedItem("keyword").value = "";
      await refreshBootstrap();
    } catch (_) {}
  });

  const configForm = element("config-form");
  configForm.addEventListener("input", () => { state.configDirty = true; });
  configForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(configForm);
    const values = Object.fromEntries(data.entries());
    const submit = configForm.querySelector("button[type=submit]");
    try {
      const result = await withButton(submit, () => api("/api/config", {method: "PUT", body: JSON.stringify({values, clear_api_key: false})}), "保存中…");
      state.configDirty = false;
      state.configHydrated = false;
      hydrateConfig(result, true);
      await refreshBootstrap();
      toast("配置已保存");
    } catch (_) {}
  });
  element("toggle-key").addEventListener("click", () => {
    const input = configForm.elements.namedItem("XHS_API_KEY");
    input.type = input.type === "password" ? "text" : "password";
  });
  element("clear-key").addEventListener("click", async (event) => {
    if (!window.confirm("确认清除 .env 中的 API Key？")) return;
    try {
      await withButton(event.currentTarget, () => api("/api/config", {method: "PUT", body: JSON.stringify({values: {}, clear_api_key: true})}), "清除中…");
      state.configDirty = false;
      state.configHydrated = false;
      await refreshBootstrap();
      toast("API Key 已清除");
    } catch (_) {}
  });

  element("refresh-models").addEventListener("click", async (event) => {
    try {
      const result = await withButton(event.currentTarget, () => api("/api/models"), "加载中…");
      const options = element("model-options");
      options.replaceChildren(...result.models.map((model) => {
        const option = document.createElement("option"); option.value = model; return option;
      }));
      element("model-count").textContent = `${result.count} 个模型`;
      element("provider-output").textContent = `模型目录已刷新\n当前模型：${result.selected}\n可见模型：${result.count}`;
    } catch (_) {}
  });
  element("provider-health").addEventListener("click", (event) => runProviderProbe(event.currentTarget, "/api/provider/probe", "健康检查"));
  element("provider-generate").addEventListener("click", (event) => runProviderProbe(event.currentTarget, "/api/provider/generate-probe", "真实生成测试"));
  element("clear-logs").addEventListener("click", () => {
    state.logs = [];
    renderLogs();
  });
}

async function runProviderProbe(button, path, label) {
  try {
    const result = await withButton(button, () => api(path, {method: "POST"}), "检查中…");
    element("provider-output").textContent = JSON.stringify(result, null, 2);
    toast(`${label}通过`);
  } catch (error) {
    element("provider-output").textContent = `${label}失败\n${error.message}`;
  }
}

function renderLogs() {
  const log = element("event-log");
  element("home-log-summary").textContent = state.logs.length
    ? `${state.logs.length} 条事件 · ${state.logs[0].message}`
    : "暂无控制台事件";
  if (!state.logs.length) {
    log.innerHTML = '<div class="event-empty">当前会话暂无记录</div>';
    return;
  }
  log.innerHTML = state.logs.map((entry) => `
    <div class="event-row">
      <time>${escapeHtml(entry.time.toLocaleTimeString("zh-CN", {hour12: false}))}</time>
      <code>${escapeHtml(entry.taskId)}</code>
      <span>${escapeHtml(entry.message)}</span>
    </div>`).join("");
}

function connectEvents() {
  if (!token) return;
  const source = new EventSource(`/api/events?token=${encodeURIComponent(token)}&after=${state.lastSequence}`);
  source.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      ingestEvent(payload);
      queueRefresh();
    } catch (_) {}
  };
  source.onerror = () => logEvent("events_disconnected", "事件流连接中断，正在重连");
}

async function boot() {
  bindWorkflowNavigation();
  bindActions();
  renderLogs();
  if (!token) {
    toast("控制台访问凭证缺失，请重新运行 python app.py", true);
    return;
  }
  try {
    await refreshBootstrap();
    connectEvents();
  } catch (error) { toast(error.message, true); }
}

boot();
