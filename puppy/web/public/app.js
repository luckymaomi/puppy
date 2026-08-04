"use strict";

const params = new URLSearchParams(window.location.search);
const token = params.get("token") || sessionStorage.getItem("puppy-console-token") || "";
if (token) {
  sessionStorage.setItem("puppy-console-token", token);
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
};

const platformNames = {xiaohongshu: "小红书", bilibili: "哔哩哔哩"};
const resourceNames = {note: "笔记", video: "视频", article: "专栏"};
const stopNames = {count: "指定数量", duration: "指定时长", continuous: "持续漫游"};
const statusNames = {
  created: "已创建",
  running: "运行中",
  waiting_human: "等待人工处理",
  paused: "已暂停",
  complete: "已完成",
  failed: "失败",
  stopped: "已停止",
};

function element(id) { return document.getElementById(id); }

async function api(path, options = {}) {
  if (!token) throw new Error("工作台访问凭证缺失，请重新运行 python app.py");
  const headers = new Headers(options.headers || {});
  headers.set("x-puppy-token", token);
  if (options.body) headers.set("content-type", "application/json");
  const response = await fetch(path, {...options, headers});
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(payload?.error || payload?.detail || `请求失败 (${response.status})`);
  return payload;
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

function formatElapsed(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total} 秒`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return `${minutes} 分 ${rest} 秒`;
}

function taskTone(status) {
  if (["running", "complete"].includes(status)) return "ok";
  if (["waiting_human", "paused"].includes(status)) return "warn";
  if (["failed", "stopped"].includes(status)) return "error";
  return "";
}

function setPill(node, text, tone = "") {
  node.className = `status-pill ${tone}`.trim();
  node.innerHTML = `<i></i>${escapeHtml(text)}`;
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

function selectedPlatform() {
  return document.querySelector('input[name="platform"]:checked')?.value || "xiaohongshu";
}

function selectedStopMode() {
  return document.querySelector('input[name="stop_mode"]:checked')?.value || "count";
}

function syncTaskFields() {
  const platform = selectedPlatform();
  const select = element("resource-type");
  const previous = select.value;
  const options = platform === "xiaohongshu"
    ? [["note", "笔记"]]
    : [["video", "视频"], ["article", "专栏"]];
  select.replaceChildren(...options.map(([value, label]) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    return option;
  }));
  if (options.some(([value]) => value === previous)) select.value = previous;
  const keyword = element("task-form").elements.namedItem("keyword");
  keyword.placeholder = platform === "xiaohongshu" ? "可留空浏览推荐流" : "例如：机器人总动员";
  keyword.required = platform === "bilibili";

  const mode = selectedStopMode();
  const maxItems = element("task-form").elements.namedItem("max_items");
  const duration = element("task-form").elements.namedItem("duration_minutes");
  maxItems.disabled = mode !== "count";
  duration.disabled = mode !== "duration";
}

function shortText(value, limit = 110) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function eventMessage(payload) {
  switch (payload.type) {
    case "browser_started": return `${platformNames[payload.platform] || payload.platform}匿名浏览器已启动 · PID ${payload.pid || "-"}`;
    case "browser_stopped": return payload.paused_task_id ? `匿名浏览器已停止 · 任务 ${payload.paused_task_id} 已暂停` : "匿名浏览器已停止并清理会话资料";
    case "browser_updated": return "浏览器状态已更新";
    case "login_prompt_closed": return `${platformNames[payload.platform] || payload.platform}登录提示已关闭`;
    case "source_ready": return `公开来源已就绪 · ${payload.keyword || "推荐流"}`;
    case "search_complete": return `搜索结果已加载 · ${payload.keyword}`;
    case "resources_observed": return `识别到 ${payload.count} 条当前可见${resourceNames[payload.resource_type] || "内容"}`;
    case "source_advanced": return `继续浏览公开结果 · ${payload.before_top} → ${payload.after_top}`;
    case "source_page_changed": return "已进入下一页公开结果";
    case "source_exhausted": return "当前公开来源已经浏览完";
    case "resource_opened": return `已打开${resourceNames[payload.resource_type] || "内容"} · ${payload.resource_id}`;
    case "resource_closed": return `已关闭详情并返回结果 · ${payload.resource_id}`;
    case "observation_ready": return `已读取公开内容与 ${payload.comment_count || 0} 条可见评论 · ${payload.resource_id}`;
    case "observation_saved": return `观察已保存到本地 · ${payload.resource_id}`;
    case "capture_failed": return `页面证据保存失败 · ${shortText(payload.error)}`;
    case "task_execution_started": return `开始漫游 · ${payload.keyword || "推荐流"} · ${stopNames[payload.stop_mode] || payload.stop_mode}`;
    case "task_completed": return `漫游完成 · ${payload.observation_count} 条观察 · ${payload.visible_comment_count} 条可见评论`;
    case "task_updated": return `任务状态更新 · ${statusNames[payload.status] || payload.status}${payload.reason ? ` · ${payload.reason}` : ""}`;
    case "task_finished": return `任务线程结束 · ${statusNames[payload.status] || payload.status}${payload.reason ? ` · ${payload.reason}` : ""}`;
    case "configuration_updated": return "AI 配置已更新";
    case "connected": return "事件流已连接";
    default: return payload.type || "事件";
  }
}

function logEvent(type, message, taskId = "-", time = null, sequence = null) {
  if (sequence && sequence <= state.lastSequence) return;
  if (sequence) state.lastSequence = sequence;
  state.logs.unshift({time: time ? new Date(time) : new Date(), type, message, taskId});
  state.logs = state.logs.slice(0, 160);
  renderLogs();
}

function ingestEvent(payload) {
  if (!payload || payload.type === "heartbeat") return;
  logEvent(payload.type, eventMessage(payload), payload.task_id || "-", payload.time, payload.sequence || null);
}

async function refreshBootstrap() {
  const snapshot = await api("/api/bootstrap");
  if (snapshot.supervisor.running) state.selectedTaskId = snapshot.supervisor.task_id;
  if (state.selectedTaskId) {
    const selected = snapshot.tasks.find((task) => task.id === state.selectedTaskId);
    if (selected) snapshot.current_task = selected;
    else state.selectedTaskId = null;
  }
  state.bootstrap = snapshot;
  (snapshot.events || []).forEach(ingestEvent);
  render();
}

function queueRefresh() {
  if (state.refreshQueued) return;
  state.refreshQueued = true;
  setTimeout(async () => {
    state.refreshQueued = false;
    try { await refreshBootstrap(); } catch (error) { toast(error.message, true); }
  }, 140);
}

function render() {
  if (!state.bootstrap) return;
  const {configuration, browser, supervisor, current_task: current, tasks, observations} = state.bootstrap;
  const browserPlatform = platformNames[browser.platform] || browser.platform;
  setPill(element("browser-status"), browser.running ? `${browserPlatform} · 匿名运行中` : "浏览器未启动", browser.running ? "ok" : "warn");
  setPill(element("task-status"), current ? statusNames[current.status] || current.status : "无活动任务", current ? taskTone(current.status) : "");
  setPill(element("storage-status"), `本地观察 ${observations.length}`, observations.length ? "ok" : "");

  if (browser.running) {
    const radio = document.querySelector(`input[name="platform"][value="${browser.platform}"]`);
    if (radio) radio.checked = true;
  } else if (current) {
    const radio = document.querySelector(`input[name="platform"][value="${current.config.platform}"]`);
    if (radio) radio.checked = true;
  }
  document.querySelectorAll('input[name="platform"]').forEach((radio) => { radio.disabled = browser.running; });
  syncTaskFields();

  element("runtime-browser").textContent = browser.running ? `运行中 · ${browser.cdp_port || "CDP"}` : "未启动";
  element("runtime-platform").textContent = browser.running ? browserPlatform : "-";
  element("runtime-session").textContent = browser.running ? "临时 · 关闭即清理" : "未创建";
  element("runtime-worker").textContent = supervisor.running ? `执行中 · ${supervisor.task_id}` : "空闲";
  element("browser-start").disabled = browser.running;
  element("browser-check").disabled = !browser.running;
  element("browser-stop").disabled = !browser.running;
  const unfinished = tasks.some((task) => !["complete", "failed", "stopped"].includes(task.status));
  element("task-form").querySelector("button[type=submit]").disabled = supervisor.running || !browser.running || unfinished;

  renderCurrentTask(current, browser, supervisor);
  renderTaskTable(tasks);
  renderObservations(observations);
  renderHome(configuration, browser, current, observations);
  hydrateConfig(configuration);
  renderAttention(current, browser);
}

function renderHome(configuration, browser, task, observations) {
  const values = configuration.values;
  element("home-config-state").textContent = configuration.ready ? "已就绪" : "需配置";
  element("home-config-summary").textContent = configuration.ready ? `${values.PUPPY_AI_PROVIDER} · ${values.PUPPY_AI_MODEL}` : (configuration.error || "Provider 与模型待配置");
  element("home-config-path").textContent = configuration.file;
  element("home-task-state").textContent = task ? (statusNames[task.status] || task.status) : "空闲";
  element("home-task-summary").textContent = task
    ? `${platformNames[task.config.platform]} · ${task.config.keyword || "推荐流"} · 已保存 ${task.observation_count}`
    : browser.running ? `${platformNames[browser.platform]}匿名浏览器待命` : "没有进行中的漫游";
  element("home-observation-state").textContent = `${observations.length} 条`;
  const latest = observations[0];
  element("home-observation-summary").textContent = latest
    ? `${platformNames[latest.platform]} · ${latest.metadata?.title || latest.resource_id}`
    : "还没有捡回公开内容";
  element("home-log-summary").textContent = state.logs[0]?.message || "暂无控制台事件";
}

function renderCurrentTask(task, browser, supervisor) {
  element("empty-task").hidden = Boolean(task);
  element("task-detail").hidden = !task;
  const badge = element("current-state");
  badge.textContent = task ? (statusNames[task.status] || task.status) : "空闲";
  badge.className = `state-badge ${task ? taskTone(task.status) : ""}`.trim();
  if (!task) return;
  const config = task.config;
  let progress = 0;
  if (config.stop_mode === "count" && config.max_items) {
    progress = Math.min(100, Math.round(task.processed_resource_ids.length / config.max_items * 100));
  } else if (config.stop_mode === "duration" && config.duration_minutes) {
    progress = Math.min(100, Math.round(task.elapsed_seconds / (config.duration_minutes * 60) * 100));
  }
  element("current-keyword").textContent = `${platformNames[config.platform]} · ${config.keyword || "推荐流"}`;
  element("current-id").textContent = task.id;
  element("task-progress").style.width = `${progress}%`;
  const target = config.stop_mode === "count" ? ` / ${config.max_items}` : "";
  element("metric-processed").textContent = `${task.processed_resource_ids.length}${target}`;
  element("metric-observations").textContent = task.observation_count;
  element("metric-comments").textContent = task.visible_comment_count;
  element("metric-mode").textContent = stopNames[config.stop_mode] || config.stop_mode;
  element("metric-elapsed").textContent = formatElapsed(task.elapsed_seconds);
  const reason = element("stop-reason");
  reason.hidden = !(task.stop_reason || task.last_error);
  reason.textContent = [task.stop_reason, task.last_error].filter(Boolean).join(" · ");
  const terminal = ["complete", "failed", "stopped"].includes(task.status);
  const browserMatches = browser.running && browser.platform === config.platform;
  element("task-pause").disabled = task.status !== "running";
  element("task-resume").disabled = terminal || supervisor.running || !browserMatches;
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
      <td>${escapeHtml(platformNames[task.config.platform] || task.config.platform)}</td>
      <td>${escapeHtml(task.config.keyword || "推荐流")}</td>
      <td><span class="state-badge ${taskTone(task.status)}">${escapeHtml(statusNames[task.status] || task.status)}</span></td>
      <td>${task.observation_count}</td>
      <td>${task.visible_comment_count}</td>
      <td>${escapeHtml(formatTime(task.updated_at))}</td>
      <td><button class="button secondary task-select" type="button" data-task-id="${escapeHtml(task.id)}">查看</button></td>
    </tr>`).join("");
  body.querySelectorAll(".task-select").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedTaskId = button.dataset.taskId;
      state.bootstrap.current_task = tasks.find((task) => task.id === state.selectedTaskId) || null;
      render();
      window.scrollTo({top: 0, behavior: "smooth"});
    });
  });
}

function renderObservations(observations) {
  const list = element("observation-list");
  if (!observations.length) {
    list.innerHTML = '<div class="empty-state">还没有本地观察</div>';
    return;
  }
  list.innerHTML = observations.map((item) => {
    const title = item.metadata?.title || item.resource_id;
    const author = item.metadata?.author || item.metadata?.uploader || "公开页面";
    const limitation = item.limitations?.length ? item.limitations.join(" · ") : "完整到当前公开边界";
    return `<article class="observation-row">
      <div class="observation-platform"><strong>${escapeHtml(platformNames[item.platform] || item.platform)}</strong><code>${escapeHtml(resourceNames[item.resource_type] || item.resource_type)} · ${escapeHtml(item.resource_id)}</code></div>
      <div class="observation-main"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(author)} · ${escapeHtml(shortText(item.content, 90) || limitation)}</p></div>
      <div class="observation-meta">${escapeHtml(formatTime(item.observed_at))}<span>${item.comments?.length || 0} 条评论</span></div>
    </article>`;
  }).join("");
}

function hydrateConfig(configuration, force = false) {
  if ((state.configHydrated && !force) || state.configDirty) return;
  const form = element("config-form");
  Object.entries(configuration.values).forEach(([key, value]) => {
    if (key === "PUPPY_AI_API_KEY") return;
    const field = form.elements.namedItem(key);
    if (field) field.value = value;
  });
  form.elements.namedItem("PUPPY_AI_API_KEY").value = "";
  element("key-state").textContent = configuration.api_key_present ? "已配置；留空保存将保留原密钥" : "未配置";
  element("config-current-provider").textContent = configuration.values.PUPPY_AI_PROVIDER || "-";
  element("config-current-model").textContent = configuration.values.PUPPY_AI_MODEL || "-";
  element("config-current-key").textContent = configuration.api_key_present ? "已配置" : "未配置";
  state.configHydrated = true;
  state.configDirty = false;
}

function renderAttention(task, browser) {
  if (task?.status === "waiting_human" && browser.running && browser.platform === task.config.platform) {
    return showIntervention(task.stop_reason || "页面需要人工复核。", true);
  }
  if (state.gateCheck && state.gateCheck.gate !== "ready") {
    return showIntervention(state.gateCheck.reason || "页面需要人工复核。", false);
  }
  closeModal();
}

function actionButton(label, className, callback) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${className}`;
  button.textContent = label;
  button.addEventListener("click", callback);
  return button;
}

function showIntervention(message, resumeTask) {
  element("modal-message").textContent = message;
  const actions = element("modal-actions");
  actions.replaceChildren();
  actions.append(actionButton("关闭", "secondary", closeModal));
  actions.append(actionButton(resumeTask ? "已处理，继续" : "重新检查", "primary", resumeTask ? () => taskAction("resume") : checkBrowser));
  element("modal").hidden = false;
}

function closeModal() { element("modal").hidden = true; }

async function runButton(button, operation, successMessage) {
  setBusy(button, true);
  try {
    await operation();
    if (successMessage) toast(successMessage);
    await refreshBootstrap();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
  }
}

async function startBrowser() {
  const button = element("browser-start");
  await runButton(
    button,
    () => api("/api/browser/start", {method: "POST", body: JSON.stringify({platform: selectedPlatform()})}),
    "匿名浏览器已启动",
  );
}

async function stopBrowser() {
  await runButton(element("browser-stop"), () => api("/api/browser/stop", {method: "POST"}), "浏览器已停止，会话资料已清理");
}

async function checkBrowser() {
  setBusy(element("browser-check"), true, "检查中…");
  try {
    state.gateCheck = await api("/api/browser/check", {method: "POST"});
    if (state.gateCheck.gate === "ready") toast("当前公开页面可继续浏览");
    renderAttention(state.bootstrap?.current_task, state.bootstrap?.browser || {});
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(element("browser-check"), false);
  }
}

async function taskAction(action) {
  const task = state.bootstrap?.current_task;
  if (!task) return;
  const button = element(`task-${action}`);
  await runButton(button, () => api(`/api/tasks/${task.id}/${action}`, {method: "POST"}), action === "stop" ? "任务已停止" : action === "pause" ? "任务已暂停" : "任务已继续");
}

function openWorkflow(name, scrollTarget = null) {
  element("workflow-home").hidden = true;
  element("workflow-detail").hidden = false;
  document.querySelectorAll("[data-workflow-panel]").forEach((panel) => { panel.hidden = panel.dataset.workflowPanel !== name; });
  window.scrollTo({top: 0});
  if (scrollTarget) requestAnimationFrame(() => element(scrollTarget)?.scrollIntoView({behavior: "smooth"}));
}

function showHome() {
  element("workflow-detail").hidden = true;
  element("workflow-home").hidden = false;
  closeModal();
  window.scrollTo({top: 0});
}

function renderLogs() {
  const log = element("event-log");
  if (!state.logs.length) {
    log.innerHTML = '<div class="event-empty">暂无运行记录</div>';
    return;
  }
  log.innerHTML = state.logs.map((item) => `<div class="event-row"><time>${escapeHtml(item.time.toLocaleTimeString("zh-CN", {hour12: false}))}</time><code>${escapeHtml(item.type)}</code><span>${escapeHtml(item.message)}</span></div>`).join("");
  element("home-log-summary").textContent = state.logs[0].message;
}

function connectEvents() {
  if (!token) return;
  const stream = new EventSource(`/api/events?token=${encodeURIComponent(token)}&after=${state.lastSequence}`);
  stream.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      ingestEvent(payload);
      if (!["heartbeat", "connected"].includes(payload.type)) queueRefresh();
    } catch (_) {}
  };
  stream.onerror = () => { stream.close(); setTimeout(connectEvents, 1800); };
}

document.querySelectorAll("[data-open-workflow]").forEach((button) => {
  button.addEventListener("click", () => openWorkflow(button.dataset.openWorkflow, button.dataset.scrollTarget || null));
});
element("back-home").addEventListener("click", showHome);
document.querySelectorAll('input[name="platform"], input[name="stop_mode"]').forEach((radio) => radio.addEventListener("change", syncTaskFields));
element("browser-start").addEventListener("click", startBrowser);
element("browser-stop").addEventListener("click", stopBrowser);
element("browser-check").addEventListener("click", checkBrowser);
element("task-pause").addEventListener("click", () => taskAction("pause"));
element("task-resume").addEventListener("click", () => taskAction("resume"));
element("task-stop").addEventListener("click", () => taskAction("stop"));
element("refresh-tasks").addEventListener("click", refreshBootstrap);
element("refresh-observations").addEventListener("click", refreshBootstrap);
element("clear-logs").addEventListener("click", () => { state.logs = []; renderLogs(); });

element("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const mode = selectedStopMode();
  const body = {
    platform: selectedPlatform(),
    keyword: form.elements.namedItem("keyword").value.trim(),
    resource_type: form.elements.namedItem("resource_type").value,
    stop_mode: mode,
    max_items: mode === "count" ? Number(form.elements.namedItem("max_items").value) : null,
    duration_minutes: mode === "duration" ? Number(form.elements.namedItem("duration_minutes").value) : null,
    comments_limit: Number(form.elements.namedItem("comments_limit").value),
    min_delay: Number(form.elements.namedItem("min_delay").value),
    max_delay: Number(form.elements.namedItem("max_delay").value),
  };
  const button = form.querySelector("button[type=submit]");
  await runButton(button, async () => {
    const task = await api("/api/tasks", {method: "POST", body: JSON.stringify(body)});
    state.selectedTaskId = task.id;
  }, "匿名漫游已启动");
});

element("config-form").addEventListener("input", () => { state.configDirty = true; });
element("config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = {};
  new FormData(form).forEach((value, key) => { values[key] = value; });
  const button = form.querySelector("button[type=submit]");
  await runButton(button, () => api("/api/config", {method: "PUT", body: JSON.stringify({values, clear_api_key: false})}), "AI 配置已保存");
  state.configDirty = false;
  state.configHydrated = false;
});

element("toggle-key").addEventListener("click", () => {
  const field = element("config-form").elements.namedItem("PUPPY_AI_API_KEY");
  field.type = field.type === "password" ? "text" : "password";
});
element("clear-key").addEventListener("click", async () => {
  await runButton(element("clear-key"), () => api("/api/config", {method: "PUT", body: JSON.stringify({values: {}, clear_api_key: true})}), "API Key 已清除");
  state.configHydrated = false;
});
element("refresh-models").addEventListener("click", async () => {
  const button = element("refresh-models");
  setBusy(button, true, "加载中…");
  try {
    const payload = await api("/api/models");
    const list = element("model-options");
    list.replaceChildren(...payload.models.map((model) => { const option = document.createElement("option"); option.value = model; return option; }));
    element("model-count").textContent = `${payload.count} 个模型`;
  } catch (error) { toast(error.message, true); } finally { setBusy(button, false); }
});

async function providerProbe(path, button) {
  setBusy(button, true, "检查中…");
  try {
    const payload = await api(path, {method: "POST"});
    element("provider-output").textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    element("provider-output").textContent = error.message;
    toast(error.message, true);
  } finally { setBusy(button, false); }
}
element("provider-health").addEventListener("click", () => providerProbe("/api/provider/probe", element("provider-health")));
element("provider-generate").addEventListener("click", () => providerProbe("/api/provider/generate-probe", element("provider-generate")));

syncTaskFields();
renderLogs();
refreshBootstrap().then(connectEvents).catch((error) => toast(error.message, true));
