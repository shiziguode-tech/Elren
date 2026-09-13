const $ = (selector) => document.querySelector(selector);

let taskId = null;
let continuationTaskId = null;
let lastEventId = null;
let taskViewGeneration = 0;
let taskViewLoading = false;
let startRequestPending = false;
let pollTimer = null;
// Request-scoped pending/ack state survives polling and chat navigation. Never
// infer user consent from a server request that is waiting for confirmation.
const taskActionStates = new Map();
let pendingHumanAction = null;
let surfacedHumanActionId = null;
let runtimePollTimer = null;
let mobilePairingPollTimer = null;
let desktopControlPollTimer = null;
let desktopControlPollPending = false;
let desktopControlActive = false;
let desktopControlStopPending = false;
let desktopControlFailureCount = 0;
let desktopControlSnapshot = null;
let statusRetryDelay = 1000;
let settingsRetryTimer = null;
let settingsRetryDelay = 1000;
let settingsRequestGeneration = 0;
let settingsSavePending = false;
let settingsSnapshotPromise = null;
let settingsSnapshotStartedAt = 0;
let settingModelDirty = false;
let settingReasoningDirty = false;
let settingsFormDirty = false;
// Keep hidden settings immune to browser/password-manager autofill until the
// first server snapshot establishes a trustworthy comparison baseline.
let settingsFormHydrating = true;
let settingsFormBaseline = null;
let settingsPreferenceBaseline = null;
let defaultModelSelector = "auto";
let automaticDefaultModelSelector = "";
// Historical tasks temporarily project their original model/reasoning into the
// composer. Keep the user's new-task choices separate so simply inspecting
// history cannot silently change the model or cost profile of the next task.
let newTaskModelPreference = "auto";
let newTaskReasoningPreference = "auto";
let newTaskProjectPath = "";
let availableModelOptions = [];
let discussionTeamConfigured = false;
let discussionTeamLeaderModel = "auto";
let discussionTeamDirty = false;
let discussionTeamDraftTimer = null;
let recoveredDiscussionTeamDraftPending = false;
let settingsApiVersion = 0;
// Start with no advertised provider capability. The live status response fills
// this list before the picker is shown, so a cold page load never exposes GPT
// reasoning levels while the configured default is DeepSeek.
let activeReasoningLevels = ["auto"];
let reasoningDefaultLoaded = false;
let latestStatus = null;
let latestArtifacts = [];
let artifactRefreshTimer = null;
let artifactRequestGeneration = 0;
let scheduleRequestGeneration = 0;
let terminalStatusRendered = null;
let stopRequestPending = false;
let pollFailureCount = 0;
let runtimeConnectionLost = false;
let timelineAutoFollow = true;
let timelineUnseenProgressCount = 0;
let historyOffset = 0;
let historyHasMore = false;
let historyLoading = false;
let historyAppendPending = false;
let historyReloadPending = false;
let historyReloadWaiters = [];
let navigationIntent = 0;
let historySearchTimer = null;
let historySyncTimer = null;
let historyFirstPageSignature = "";
let historyLoadedFilter = null;
let knownHistoryTaskIds = new Set();
let contextTaskId = null;
let renameTaskId = null;
let renameTaskDialogGeneration = 0;
const pendingTaskRenames = new Set();
let confirmationResolver = null;
let currentTaskSnapshot = null;
let pendingAttachments = [];
// Drafts belong to a conversation, never to whichever task finishes loading
// next. Normally memory-only; an explicit language reload uses a bounded,
// one-shot same-tab transfer (never settings, credentials, or URL message text).
const composerDrafts = new Map();
let composerDraftScope = null;
let uploadRequestPending = false;
let scheduleCreatePending = false;
let dismissedArtifactInspectorTaskId = null;
const dismissedProNotices = new Set();

const HISTORY_PAGE_SIZE = 50;
const TIMELINE_BOTTOM_THRESHOLD = 72;

const LANGUAGE_STORAGE_KEY = "elren.ui-language.v1";
const RUNTIME_STATUS_STORAGE_KEY = "elren.runtime-status.v1";
const SETTINGS_DISPLAY_STORAGE_KEY = "elren.settings-display.v1";
const SIDEBAR_WIDTH_STORAGE_KEY = "elren.sidebar-width.v1";
const DISCUSSION_TEAM_DRAFT_STORAGE_KEY = "elren.discussion-team-draft.v1";
const PREVIOUS_LANGUAGE_STORAGE_KEY = "milo.ui-language.v1";
const PREVIOUS_RUNTIME_STATUS_STORAGE_KEY = "milo.runtime-status.v1";
const PREVIOUS_SIDEBAR_WIDTH_STORAGE_KEY = "milo.sidebar-width.v1";
const SIDEBAR_MIN_WIDTH = 236;
const RUNTIME_STATUS_CACHE_TTL_MS = 2 * 60 * 1000;
const SETTINGS_DISPLAY_CACHE_TTL_MS = 30 * 24 * 60 * 60 * 1000;
const SETTINGS_SNAPSHOT_REUSE_MS = 15000;
let uiLanguage = "zh";
let pendingLanguageSwitchModel = "";
try {
  const initialQuery = new URLSearchParams(window.location.search);
  const urlLanguage = initialQuery.get("lang");
  pendingLanguageSwitchModel = String(initialQuery.get("model") || "").slice(0, 200);
  const savedLanguage = urlLanguage
    ?? window.localStorage.getItem(LANGUAGE_STORAGE_KEY)
    ?? window.localStorage.getItem(PREVIOUS_LANGUAGE_STORAGE_KEY)
    ?? window.localStorage.getItem("deepdesk.ui-language.v1");
  uiLanguage = savedLanguage === "en" ? "en" : "zh";
} catch {
  // Some embedded/private browser contexts disable Web Storage. The URL keeps
  // the language switch functional there and is also safe across a reload.
  try {
    uiLanguage = new URLSearchParams(window.location.search).get("lang") === "en" ? "en" : "zh";
  } catch {}
}
const isEnglish = () => uiLanguage === "en";
const uiText = (zh, en) => isEnglish() ? en : zh;

// Chromium's built-in validation bubbles follow the browser/OS language, not
// document.lang. Own only our constraint messages; leave domain-specific
// custom errors intact and never disable the native constraint checks.
const localizedConstraintMessages = new WeakMap();

function localizedConstraintMessage(input) {
  const validity = input.validity;
  if (input.hasAttribute?.("data-local-datetime") && input.value && !parseScheduleDateTime(input.value)) {
    return uiText("请输入有效日期与时间，格式：YYYY-MM-DD HH:mm。", "Enter a valid date and time: YYYY-MM-DD HH:mm.");
  }
  if (validity.badInput) return uiText("请输入有效的值。", "Please enter a valid value.");
  if (validity.valueMissing) {
    if (input.type === "checkbox") return uiText("请勾选此项。", "Please check this box.");
    if (input.type === "radio" || input.tagName === "SELECT") return uiText("请选择一项。", "Please select an option.");
    return uiText("请填写此字段。", "Please fill out this field.");
  }
  if (validity.typeMismatch) return input.type === "email"
    ? uiText("请输入有效的电子邮箱地址。", "Please enter a valid email address.")
    : uiText("请输入有效的网址。", "Please enter a valid URL.");
  if (validity.rangeUnderflow) return uiText(`请输入不小于 ${input.min} 的值。`, `Please enter a value greater than or equal to ${input.min}.`);
  if (validity.rangeOverflow) return uiText(`请输入不大于 ${input.max} 的值。`, `Please enter a value less than or equal to ${input.max}.`);
  if (validity.tooShort) return uiText(`请至少输入 ${input.minLength} 个字符。`, `Please enter at least ${input.minLength} characters.`);
  if (validity.tooLong) return uiText(`请最多输入 ${input.maxLength} 个字符。`, `Please enter no more than ${input.maxLength} characters.`);
  if (validity.stepMismatch) return uiText("请输入符合指定间隔的有效值。", "Please enter a value matching the allowed step.");
  if (validity.patternMismatch) return uiText("请使用要求的格式。", "Please match the requested format.");
  return "";
}

function syncLocalizedConstraint(input) {
  if (!input || typeof input.setCustomValidity !== "function" || !input.validity) return;
  const previous = localizedConstraintMessages.get(input);
  if (input.validity.customError && input.validationMessage !== previous) return;
  input.setCustomValidity("");
  localizedConstraintMessages.delete(input);
  if (!input.willValidate) return;
  const message = localizedConstraintMessage(input);
  if (message) {
    input.setCustomValidity(message);
    localizedConstraintMessages.set(input, message);
  }
}

function refreshLocalizedConstraints(root = document) {
  root.querySelectorAll("input, textarea, select").forEach(syncLocalizedConstraint);
}

function initializeLocalizedConstraints() {
  // Capture invalid (which doesn't bubble) and pointerover before native hover
  // help opens. Delegation also covers fields added after initial rendering.
  for (const name of ["input", "change", "focusin", "pointerover", "invalid"]) {
    document.addEventListener(name, (event) => syncLocalizedConstraint(event.target), true);
  }
  // Programmatic values do not fire input/change: refresh before default
  // click/Enter form validation so a previously empty field cannot stay stuck.
  document.addEventListener("click", (event) => {
    const control = event.target.closest?.("button, input[type='submit'], input[type='image']");
    if (control?.form) refreshLocalizedConstraints(control.form);
  }, true);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.form) refreshLocalizedConstraints(event.target.form);
  }, true);
  document.addEventListener("reset", (event) => {
    queueMicrotask(() => refreshLocalizedConstraints(event.target));
  }, true);
  refreshLocalizedConstraints();
}

function syncDesktopLanguage(language) {
  if (language !== "zh" && language !== "en") return;
  try {
    if (typeof window.webkit?.messageHandlers?.elrenLanguage?.postMessage === "function") {
      window.webkit.messageHandlers.elrenLanguage.postMessage(language);
      return;
    }
  } catch {}
  try {
    // Only a preference token crosses this bridge, never settings or secrets.
    // Ordinary browsers and older desktop shells can ignore it safely.
    if (typeof window.chrome?.webview?.postMessage === "function") {
      window.chrome.webview.postMessage(`elren:ui-language:${language}`);
      return;
    }
  } catch {}
  try {
    // Edge compatibility mode has no WebView2 bridge. Store just the locale
    // through the same-origin endpoint; the tray reads it when opened.
    window.fetch?.("/api/desktop/language", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language }),
      keepalive: true,
    }).catch(() => {});
  } catch {}
}
syncDesktopLanguage(uiLanguage);

function sidebarWidthBounds() {
  const shellWidth = $("#sidebarResizer")?.parentElement?.getBoundingClientRect().width
    || window.innerWidth;
  return {
    min: SIDEBAR_MIN_WIDTH,
    max: Math.max(SIDEBAR_MIN_WIDTH, Math.floor(shellWidth / 3)),
  };
}

function applySidebarWidth(value, persist = false) {
  const shell = $(".shell");
  const resizer = $("#sidebarResizer");
  if (!shell || !resizer || window.matchMedia("(max-width: 960px)").matches) return;
  const bounds = sidebarWidthBounds();
  const numeric = Number(value);
  const width = Math.min(bounds.max, Math.max(bounds.min, Number.isFinite(numeric) ? numeric : 296));
  shell.style.setProperty("--sidebar-width", `${Math.round(width)}px`);
  resizer.setAttribute("aria-valuemin", String(bounds.min));
  resizer.setAttribute("aria-valuemax", String(bounds.max));
  resizer.setAttribute("aria-valuenow", String(Math.round(width)));
  if (persist) {
    try { window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(Math.round(width))); } catch {}
  }
}

function initializeSidebarResize() {
  const resizer = $("#sidebarResizer");
  const sidebar = $("aside");
  const shell = $(".shell");
  if (!resizer || !sidebar || !shell) return;
  const modalLayerOpen = () => Boolean(document.querySelector("dialog[open]"));
  let dragging = false;
  let startX = 0;
  let startWidth = 0;

  let savedWidth = 296;
  try {
    savedWidth = Number(window.localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY)
      ?? window.localStorage.getItem(PREVIOUS_SIDEBAR_WIDTH_STORAGE_KEY)) || 296;
  } catch {}
  applySidebarWidth(savedWidth);

  const finishDrag = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove("sidebar-resizing");
    applySidebarWidth(sidebar.getBoundingClientRect().width, true);
  };

  const beginDrag = (event) => {
    if (event.button !== undefined && event.button !== 0) return;
    if (window.matchMedia("(max-width: 960px)").matches) return;
    // Native dialogs live in the browser's top layer, but document-level
    // capture handlers still see their pointer events. Never let a Settings
    // control near the underlying sidebar edge begin a sidebar resize.
    if (modalLayerOpen()) return;
    if (dragging) return;
    dragging = true;
    startX = event.clientX;
    startWidth = sidebar.getBoundingClientRect().width;
    document.body.classList.add("sidebar-resizing");
    if (event.pointerId !== undefined) resizer.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  };
  resizer.addEventListener("pointerdown", beginDrag);
  // Some Windows WebView2/native automation paths emit a mouse drag without
  // synthesizing Pointer Events. Keep a mouse fallback so the full-height
  // separator remains draggable with physical mice, touchpads, and Computer Use.
  resizer.addEventListener("mousedown", beginDrag);
  const beginDragFromSidebarEdge = (event) => {
    if (dragging || modalLayerOpen() || window.matchMedia("(max-width: 960px)").matches) return;
    const edge = sidebar.getBoundingClientRect().right;
    // WebView2 can occasionally route the transparent grid separator to the
    // adjacent pane. Capture the same full-height edge at document level so
    // the visible separator remains a dependable target from top to bottom.
    if (event.clientX >= edge - 2 && event.clientX <= edge + 14) beginDrag(event);
  };
  document.addEventListener("pointerdown", beginDragFromSidebarEdge, true);
  document.addEventListener("mousedown", beginDragFromSidebarEdge, true);
  window.addEventListener("pointermove", (event) => {
    if (dragging) applySidebarWidth(startWidth + event.clientX - startX);
  });
  window.addEventListener("mousemove", (event) => {
    if (dragging) applySidebarWidth(startWidth + event.clientX - startX);
  });
  window.addEventListener("pointerup", finishDrag);
  window.addEventListener("pointercancel", finishDrag);
  window.addEventListener("mouseup", finishDrag);
  window.addEventListener("blur", finishDrag);
  resizer.addEventListener("lostpointercapture", finishDrag);
  let resizeFrame = null;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = null;
      applySidebarWidth(sidebar.getBoundingClientRect().width);
    });
  });
  resizer.addEventListener("keydown", (event) => {
    const bounds = sidebarWidthBounds();
    const current = sidebar.getBoundingClientRect().width;
    let next = null;
    if (event.key === "ArrowLeft") next = current - 16;
    if (event.key === "ArrowRight") next = current + 16;
    if (event.key === "Home") next = bounds.min;
    if (event.key === "End") next = bounds.max;
    if (next === null) return;
    event.preventDefault();
    applySidebarWidth(next, true);
  });
  resizer.addEventListener("dblclick", () => applySidebarWidth(296, true));
}

function readCachedRuntimeStatus() {
  try {
    const cached = JSON.parse(window.sessionStorage.getItem(RUNTIME_STATUS_STORAGE_KEY)
      ?? window.sessionStorage.getItem(PREVIOUS_RUNTIME_STATUS_STORAGE_KEY)
      ?? "null");
    if (!cached?.status || Date.now() - Number(cached.savedAt || 0) > RUNTIME_STATUS_CACHE_TTL_MS) {
      return null;
    }
    return cached.status;
  } catch {
    return null;
  }
}

function cacheRuntimeStatus(status) {
  if (!status) return;
  try {
    window.sessionStorage.setItem(
      RUNTIME_STATUS_STORAGE_KEY,
      JSON.stringify({ savedAt: Date.now(), status }),
    );
  } catch {
    // The live in-memory status remains usable when session storage is disabled.
  }
}

function cacheSettingsDisplay(settings) {
  const selector = String(settings?.model || "auto").slice(0, 200);
  const effort = String(settings?.reasoning_effort || "auto");
  const capabilitySelector = selector === "auto"
    ? String(settings?.active_model || "").slice(0, 200)
    : selector;
  const option = Array.isArray(settings?.available_models)
    ? settings.available_models.find((item) => item?.selector === capabilitySelector)
    : null;
  const reasoning = option?.reasoning || {};
  const levels = Array.isArray(reasoning.levels)
    ? reasoning.levels.filter((value) => typeof value === "string").slice(0, 8)
    : [];
  try {
    window.localStorage.setItem(SETTINGS_DISPLAY_STORAGE_KEY, JSON.stringify({
      savedAt: Date.now(),
      model: selector,
      modelLabel: selector === "auto"
        ? "auto"
        : String(option?.model || selector).slice(0, 200),
      reasoningEffort: effort,
      reasoningSupported: reasoning.supported === true,
      reasoningControl: String(reasoning.control || "none").slice(0, 100),
      reasoningLevels: levels,
    }));
  } catch {
    // A fresh settings response still hydrates the form when storage is disabled.
  }
}

function isRetiredBuiltinModel(selector) {
  return /^(?:deepseek:)?deepseek-v4\.1-flash-expires-on-0910$/i.test(String(selector || "").trim());
}

function hydrateSettingsDisplayFromCache() {
  let cached = null;
  try {
    cached = JSON.parse(window.localStorage.getItem(SETTINGS_DISPLAY_STORAGE_KEY) || "null");
  } catch {
    return;
  }
  if (!cached || Date.now() - Number(cached.savedAt || 0) > SETTINGS_DISPLAY_CACHE_TTL_MS) return;
  if (isRetiredBuiltinModel(cached.model)) {
    cached = { ...cached, model: "auto", modelLabel: uiText("自动", "Automatic"),
      reasoningSupported: false, reasoningEffort: "auto", reasoningLevels: [] };
  }
  const model = String(cached.model || "auto").slice(0, 200);
  const modelSelect = $("#settingModel");
  const reasoningSelect = $("#settingReasoningEffort");
  if (!modelSelect || !reasoningSelect) return;
  if (![...modelSelect.options].some((option) => option.value === model)) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = String(cached.modelLabel || model).slice(0, 200);
    modelSelect.append(option);
  }
  modelSelect.value = model;
  const cachedLevels = Array.isArray(cached.reasoningLevels)
    ? cached.reasoningLevels.filter((value) => ALL_REASONING_LEVELS.includes(value))
    : [];
  const levels = cached.reasoningSupported === true
    ? ["auto", ...cachedLevels.filter((value) => value !== "auto")]
    : ["auto"];
  reasoningSelect.innerHTML = levels.map((value) => (
    `<option value="${escapeHtml(value)}">${escapeHtml(reasoningLevelLabel(value))}</option>`
  )).join("");
  const effort = String(cached.reasoningEffort || "auto");
  reasoningSelect.value = levels.includes(effort) ? effort : "auto";
  reasoningSelect.disabled = cached.reasoningSupported !== true;
  const help = reasoningSelect.closest(".reasoning-setting")?.querySelector("small");
  if (help) {
    help.textContent = cached.reasoningSupported === true
      ? uiText(
          `已立即显示上次保存的档位；正在与后台核对（${cached.reasoningControl || "API"}）。`,
          `Showing the last saved level immediately while the service verifies it (${cached.reasoningControl || "API"}).`,
        )
      : uiText(
          "已立即显示上次保存的设置；正在与后台核对模型能力。",
          "Showing the last saved setting immediately while the service verifies model capabilities.",
        );
  }
}

function requestSettingsSnapshot() {
  if (
    settingsSnapshotPromise
    && Date.now() - settingsSnapshotStartedAt <= SETTINGS_SNAPSHOT_REUSE_MS
  ) return settingsSnapshotPromise;
  settingsSnapshotStartedAt = Date.now();
  const request = api("/api/settings");
  settingsSnapshotPromise = request;
  void request.catch(() => {
    // A transient startup failure must not poison Settings for the full cache
    // window. An older rejected request must never clear a newer snapshot.
    if (settingsSnapshotPromise !== request) return;
    settingsSnapshotPromise = null;
    settingsSnapshotStartedAt = 0;
  });
  return request;
}

function preloadSettingsDisplay() {
  const request = requestSettingsSnapshot();
  void request.then((settings) => {
    if (settingsSnapshotPromise !== request) return;
    cacheSettingsDisplay(settings);
    if (!settingsFormDirty && !settingModelDirty && !settingReasoningDirty) {
      syncModelSelectors(settings);
    }
  }).catch(() => {
    // Opening Settings retains the normal retry UI if startup prefetch fails.
  });
}

latestStatus = readCachedRuntimeStatus();

function localizeKnownSystemMessage(value) {
  const text = String(value || "").trim();
  if (!text) return text;
  const needsOpenClawCredentials = /GatewayExplicitAuthRequiredError|gateway or override requires explicit credentials/i.test(text);
  if (!isEnglish()) {
    if (needsOpenClawCredentials) {
      return "OpenClaw Gateway 需要显式身份凭据（token 或 password），请先在 OpenClaw 配置中完成设置。";
    }
    return {
      "OpenClaw CLI is not installed": "未安装 OpenClaw CLI",
      "Task cancelled": "任务已停止",
      "Task cancelled.": "任务已停止",
      "Task canceled": "任务已停止",
      "Task canceled.": "任务已停止",
      "Task was cancelled": "任务已停止",
      "Task was canceled": "任务已停止",
      "The task was cancelled by the user": "用户已停止任务",
      "The task was canceled by the user": "用户已停止任务",
      "Task error": "任务出错",
      "Task error.": "任务出错",
      "Select a local project directory, not a network or device path": "请选择本地项目目录，不能使用网络路径或设备路径",
      "Project path must be an existing absolute directory without parent traversal": "项目路径必须是已存在的绝对目录，且不能包含上级目录跳转",
      "Select a project folder, not a drive root or home directory": "请选择具体项目文件夹，不能使用磁盘根目录或用户主目录",
      "Project paths cannot contain symbolic links or junctions": "项目路径不能包含符号链接或目录联接",
      "Project path must be an existing directory": "项目路径必须是已存在的目录",
      "Project directory does not exist or cannot be accessed": "项目目录不存在或无法访问",
    }[text] || text;
  }
  const exact = {
    "模型未提供目标窗口，请手动切换到需要操作的窗口": "The model did not identify a target window. Switch to the window that needs your input.",
    "目标窗口已聚焦": "The target window is now focused.",
    "请先点击接管操作": "Select Take over before continuing.",
    "Human action is no longer pending": "This takeover request is no longer pending.",
    "Human action could not be resumed": "The task could not be resumed after the takeover.",
    "停止请求已发送": "Stop request sent.",
    "任务已经结束": "The task has already ended.",
    "用户已停止任务": "The task was stopped by the user.",
    "只能继续已结束的任务": "Only a finished task can be continued.",
    "Task cancelled": "Task stopped.",
    "Task cancelled.": "Task stopped.",
    "Task canceled": "Task stopped.",
    "Task canceled.": "Task stopped.",
    "Task was cancelled": "Task stopped.",
    "Task was canceled": "Task stopped.",
  };
  if (exact[text]) return exact[text];
  if (needsOpenClawCredentials) {
    return "OpenClaw Gateway requires an explicit token or password. Add it to the OpenClaw configuration, then probe again.";
  }
  const missingWindow = text.match(/^未找到标题包含[“\"](.+?)[”\"]的窗口，请手动切换$/);
  if (missingWindow) return `No window whose title contains “${missingWindow[1]}” was found. Switch to it manually.`;
  const focusFailure = text.match(/^窗口聚焦失败，请手动切换：(.+)$/s);
  if (focusFailure) return `The target window could not be focused. Switch to it manually: ${focusFailure[1]}`;
  return text;
}

function takeoverFocusMessage(focus) {
  if (!focus) return uiText("请完成人工操作后返回此页面", "Complete the manual action, then return to this page");
  if (!isEnglish()) return focus.message || "请完成人工操作后返回此页面";
  if (focus.code === "target_window_missing") {
    return "The model did not identify a target window. Switch to the window that needs your input.";
  }
  if (focus.code === "target_window_not_found") {
    return `No window whose title contains “${focus.title_hint || "the requested title"}” was found. Switch to it manually.`;
  }
  if (focus.code === "target_window_focused") return "The target window is now focused.";
  if (focus.code === "target_window_focus_failed") {
    const detail = focus.detail ? `: ${focus.detail}` : "";
    return `The target window could not be focused. Switch to it manually${detail}`;
  }
  return localizeKnownSystemMessage(focus.message)
    || "Complete the manual action, then return to this page";
}
const promptPlaceholder = () => taskId
  ? uiText(
      "任务运行中也可以继续发送要求；Agent 会在当前步骤后接收…",
      "You can send follow-ups while this task runs; the Agent will receive them after the current step…",
    )
  : uiText(
      "描述你想完成的任务…",
      "Describe what you want to get done…",
    );

function taskComposerPlaceholder(task = null) {
  if (["completed", "failed", "cancelled"].includes(task?.status)) {
    return uiText("继续这个任务：输入下一步指令…", "Continue this task: enter the next instruction…");
  }
  if (task?.status === "waiting_user" && task?.pending_human_actions?.length) {
    return uiText("请先完成当前人工接管操作", "Complete the current manual action first");
  }
  return promptPlaceholder();
}

function syncComposerAction() {
  syncProjectComposer();
  const button = $("#send");
  const prompt = $("#prompt");
  if (!button || !prompt) return;
  const status = currentTaskSnapshot?.status;
  const taskRunning = Boolean(taskId) && ["queued", "running", "waiting_user", "waiting_approval"].includes(status);
  const waitingStop = $("#stopWaitingTask");
  if (waitingStop) {
    waitingStop.hidden = !taskId || !["waiting_user", "waiting_approval"].includes(status) || taskViewLoading;
    waitingStop.disabled = stopRequestPending;
    waitingStop.textContent = stopRequestPending ? uiText("正在停止…", "Stopping…") : uiText("停止任务", "Stop task");
  }
  const shouldStop = taskRunning && !prompt.value.trim() && !startRequestPending && !taskViewLoading;
  button.dataset.action = shouldStop ? "stop" : "send";
  const label = shouldStop ? uiText("停止任务", "Stop task") : uiText("发送", "Send");
  button.setAttribute("aria-label", label);
  button.title = label;
  if (stopRequestPending) {
    button.disabled = true;
    button.title = uiText("正在停止…", "Stopping…");
    button.setAttribute("aria-label", button.title);
  } else if (taskViewLoading) {
    button.disabled = true;
    button.title = uiText("正在加载任务，请稍候", "Loading task; please wait");
  } else if (shouldStop) {
    button.disabled = stopRequestPending;
  } else if (!startRequestPending) {
    const acceptsFollowUps = !taskId
      || ["queued", "running", "waiting_approval"].includes(status);
    button.disabled = !acceptsFollowUps || !prompt.value.trim();
    if (uploadRequestPending) {
      button.disabled = true;
      button.title = uiText("附件正在上传，请稍候", "Attachments are uploading; please wait");
    }
  } else {
    button.disabled = true;
  }
}
const HUMAN_PROBLEM_SKIP_WARNING_KEY = "deepdesk.human-problem-skip-warning-disabled.v1";

function syncProjectComposer() {
  const input = $("#projectPath");
  if (!input) return;
  const bound = Boolean(taskId || continuationTaskId || currentTaskSnapshot?.id);
  input.readOnly = bound || startRequestPending || taskViewLoading;
  const value = bound ? String(currentTaskSnapshot?.project_path || "") : newTaskProjectPath;
  if (input.value !== value) input.value = value;
  input.title = value || String(latestStatus?.workspace || "");
  input.placeholder = bound
    ? uiText("应用工作区", "Application workspace")
    : uiText("粘贴现有项目的绝对路径；留空使用应用工作区", "Paste an existing project’s absolute path; leave blank for the app workspace");
  const label = $("#projectPathLabel");
  if (label?.lastChild) label.lastChild.textContent = uiText("项目目录", "Project folder");
  $("#projectPathHelp").textContent = bound
    ? uiText("此对话固定使用该目录；切换项目请新建任务。", "This conversation stays in this folder. Start a new task to switch projects.")
    : uiText("直接编辑所选目录中的文件，不会复制项目。任务开始后固定此目录。", "Edits files in this folder directly; no project copy is made. The folder is fixed once the task starts.");
}

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character],
  );
}

// JSON.stringify must escape real line breaks inside string values, which made
// multi-line shell commands and tool output look like a single line containing
// `\n`. Mark real CR/LF characters before serializing and restore only those
// markers afterwards. A literal backslash followed by `n`/`r` never receives a
// marker, so regexes, Windows paths, source-code escapes, and user-authored
// `\n` text remain literal.
function stringifyStructuredValueForDisplay(value) {
  try {
    const raw = JSON.stringify(value, null, 2);
    if (raw === undefined) return String(value ?? "");
    let lineBreakMarker = "\uE000ELREN_ACTUAL_LINE_BREAK\uE001";
    while (raw.includes(lineBreakMarker)) lineBreakMarker += "\uE002";
    // Work from JSON's own lossless representation of this display payload.
    // Besides avoiding mutation, this lets us mark real line breaks in object
    // property names as well as values; JSON.stringify's replacer never sees a
    // property name as a replaceable value.
    const markRealLineBreaks = (item) => {
      if (typeof item === "string") return item.replace(/\r\n?|\n/g, lineBreakMarker);
      if (Array.isArray(item)) return item.map(markRealLineBreaks);
      if (item && typeof item === "object") {
        const mapped = Object.create(null);
        Object.entries(item).forEach(([key, child]) => {
          Object.defineProperty(mapped, key.replace(/\r\n?|\n/g, lineBreakMarker), {
            value: markRealLineBreaks(child),
            enumerable: true,
            configurable: true,
            writable: true,
          });
        });
        return mapped;
      }
      return item;
    };
    const marked = JSON.stringify(markRealLineBreaks(JSON.parse(raw)), null, 2);
    return String(marked ?? "").replaceAll(lineBreakMarker, "\n");
  } catch {
    return String(value ?? "");
  }
}

const TECHNICAL_PREVIEW_MAX_CHARS = 16000;
const TECHNICAL_PREVIEW_MAX_DEPTH = 7;
const TECHNICAL_PREVIEW_MAX_ITEMS = 60;

function boundedTechnicalPreview(value, depth = 0, state = { remaining: 12000, remainingItems: 240, seen: new WeakSet() }) {
  if (value === null || value === undefined || typeof value === "boolean" || typeof value === "number") return value;
  if (typeof value === "bigint") return `${value}n`;
  if (typeof value === "string") {
    const allowance = Math.max(0, Math.min(4000, state.remaining));
    const clipped = value.length > allowance ? `${value.slice(0, allowance)}\n… [preview truncated]` : value;
    state.remaining = Math.max(0, state.remaining - Math.min(value.length, allowance));
    return clipped;
  }
  if (typeof value !== "object") return String(value);
  if (state.seen.has(value)) return "[circular reference]";
  if (depth >= TECHNICAL_PREVIEW_MAX_DEPTH || state.remaining <= 0 || state.remainingItems <= 0) return "… [preview truncated]";
  state.seen.add(value);
  try {
    if (Array.isArray(value)) {
      const itemLimit = Math.min(value.length, TECHNICAL_PREVIEW_MAX_ITEMS, state.remainingItems);
      const preview = [];
      for (let index = 0; index < itemLimit; index += 1) {
        state.remainingItems -= 1;
        preview.push(boundedTechnicalPreview(value[index], depth + 1, state));
      }
      if (value.length > itemLimit) preview.push(`… ${value.length - itemLimit} more items`);
      return preview;
    }
    const preview = Object.create(null);
    let itemCount = 0;
    let truncated = false;
    for (const key in value) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
      if (itemCount >= TECHNICAL_PREVIEW_MAX_ITEMS || state.remainingItems <= 0) {
        truncated = true;
        break;
      }
      state.remainingItems -= 1;
      itemCount += 1;
      const safeKey = key.length > 240 ? `${key.slice(0, 240)}…` : key;
      preview[safeKey] = boundedTechnicalPreview(value[key], depth + 1, state);
    }
    if (truncated) preview["…"] = "more fields omitted";
    return preview;
  } catch {
    return "[unavailable preview]";
  } finally {
    state.seen.delete(value);
  }
}

function technicalPreviewText(value) {
  const text = stringifyStructuredValueForDisplay(boundedTechnicalPreview(value));
  if (text.length <= TECHNICAL_PREVIEW_MAX_CHARS) return text;
  const suffix = "\n… [preview truncated]";
  return `${text.slice(0, TECHNICAL_PREVIEW_MAX_CHARS - suffix.length)}${suffix}`;
}

function renderMathToHtml(value, displayMode = false) {
  const formula = String(value ?? "").trim();
  if (!formula) return "";
  try {
    if (typeof katex !== "undefined" && typeof katex.renderToString === "function") {
      const rendered = katex.renderToString(formula, {
        displayMode,
        throwOnError: false,
        strict: "warn",
        trust: false,
        output: "htmlAndMathml",
      });
      return displayMode
        ? `<div class="math-display">${rendered}</div>`
        : `<span class="math-inline">${rendered}</span>`;
    }
  } catch {}
  const delimiter = displayMode ? "$$" : "$";
  const fallback = `${delimiter}${formula}${delimiter}`;
  return displayMode
    ? `<pre class="math-fallback">${escapeHtml(fallback)}</pre>`
    : `<code class="inline-code math-fallback">${escapeHtml(fallback)}</code>`;
}

function localOutputLink(value, label = "", block = false) {
  const path = String(value || "").trim();
  const normalized = path.replaceAll("\\", "/");
  if (!/^(?:[A-Za-z]:\/|\/|outputs\/)/.test(normalized)
      || normalized.startsWith("//") || normalized.split("/").includes("..")
      || !normalized.split("/").includes("outputs")
      || /[\r\n\x00<>"|]/.test(path)
      || !/\.(?:html?|pdf|txt|md|svg|png|jpe?g|webp|docx|pptx|xlsx|csv|json|midi?|musicxml)$/i.test(path)) return "";
  const owner = taskId || currentTaskSnapshot?.id;
  if (!owner) return "";
  const name = label || normalized.split("/").pop();
  const url = `/api/tasks/${encodeURIComponent(owner)}/file?path=${encodeURIComponent(path)}`;
  const link = `<a class="task-file-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(path)}">${escapeHtml(name)}</a>`;
  return block ? `<div class="task-file-reference">${link}<span class="task-file-path">${escapeHtml(path)}</span></div>` : link;
}

function renderInlineMarkdown(value) {
  const standalone = localOutputLink(value, "", true);
  if (standalone) return standalone;
  let raw = String(value ?? "");
  // CommonMark uses an odd trailing backslash as a hard line break. Remove
  // only that final control slash; doubled slashes and all fenced code remain
  // byte-for-byte literal.
  const trailingSlashes = raw.match(/(\\+)$/)?.[1] || "";
  if (trailingSlashes.length % 2 === 1) raw = raw.slice(0, -1);

  // Protect code before looking for math, so `$x$` in source or a command is
  // never interpreted. Math is tokenized before HTML escaping because KaTeX
  // must receive the original TeX rather than character entities.
  const inlineCode = [];
  raw = raw.replace(/`([^`\n]+)`/g, (_, code) => {
    const marker = `\u0000ELREN_INLINE_CODE_${inlineCode.length}\u0000`;
    inlineCode.push(localOutputLink(code) || `<code class="inline-code">${escapeHtml(code)}</code>`);
    return marker;
  });
  raw = raw.replace(/\[([^\]]+)\]\(([^\n]+?)\)/g, (all, label, path) => {
    const link = localOutputLink(path.replace(/^<|>$/g, ""), label);
    if (!link) return all;
    const marker = `\u0000ELREN_INLINE_CODE_${inlineCode.length}\u0000`;
    inlineCode.push(link);
    return marker;
  });
  const inlineMath = [];
  raw = raw.replace(/(^|[^\\$])\$(?!\$|\s)([^$\n]*?\S)\$/g, (_, prefix, formula) => {
    const marker = `\u0000ELREN_INLINE_MATH_${inlineMath.length}\u0000`;
    inlineMath.push(renderMathToHtml(formula, false));
    return `${prefix}${marker}`;
  });
  let text = escapeHtml(raw);
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  text = text.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  text = text.replace(
    /\[([^\]]+)]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
  );
  inlineCode.forEach((code, index) => {
    text = text.replace(`\u0000ELREN_INLINE_CODE_${index}\u0000`, code);
  });
  inlineMath.forEach((math, index) => {
    text = text.replace(`\u0000ELREN_INLINE_MATH_${index}\u0000`, math);
  });
  return text;
}

function tableCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function isTableDivider(line) {
  const cells = tableCells(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

function isBlockStart(lines, index) {
  const line = lines[index] || "";
  const next = lines[index + 1] || "";
  return (
    !line.trim()
    || /^```/.test(line)
    || /^\s*\$\$/.test(line)
    || /^#{1,4}\s+/.test(line)
    || /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)
    || /^\s*[-*+]\s+/.test(line)
    || /^\s*\d+[.)]\s+/.test(line)
    || /^\s*>\s?/.test(line)
    || (line.includes("|") && isTableDivider(next))
  );
}

function renderMarkdown(source) {
  const lines = String(source ?? "").replace(/\r\n?/g, "\n").split("\n");
  const output = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^```([\w+-]*)\s*$/);
    if (fence) {
      const code = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const language = fence[1] ? ` data-language="${escapeHtml(fence[1])}"` : "";
      // Fenced source is deliberately rendered byte-for-byte. A literal `\n`
      // may be a regex, path, string escape, or normal prose; without transport
      // proof it is unsafe to guess that the model meant a physical line break.
      const fileLink = /^(?:text|path)?$/.test(fence[1]) ? localOutputLink(code.join("\n"), "", true) : "";
      output.push(fileLink || `<pre><code${language}>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }

    const singleLineMath = line.match(/^\s*\$\$(.*?)\$\$\s*\\?\s*$/);
    if (singleLineMath) {
      output.push(renderMathToHtml(singleLineMath[1], true));
      index += 1;
      continue;
    }

    if (/^\s*\$\$\s*$/.test(line)) {
      const formula = [];
      index += 1;
      while (index < lines.length && !/^\s*\$\$\s*\\?\s*$/.test(lines[index])) {
        formula.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      output.push(renderMathToHtml(formula.join("\n"), true));
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      output.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      output.push("<hr>");
      index += 1;
      continue;
    }

    if (line.includes("|") && isTableDivider(lines[index + 1] || "")) {
      const headers = tableCells(line);
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(tableCells(lines[index]));
        index += 1;
      }
      const head = headers.map((cell) => `<th>${renderInlineMarkdown(cell)}</th>`).join("");
      const body = rows.map(
        (row) => `<tr>${headers.map((_, cellIndex) => `<td>${renderInlineMarkdown(row[cellIndex] || "")}</td>`).join("")}</tr>`,
      ).join("");
      output.push(`<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*[-*+]\s+/, ""));
        index += 1;
      }
      output.push(`<ul>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*\d+[.)]\s+/, ""));
        index += 1;
      }
      output.push(`<ol>${items.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</ol>`);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      output.push(`<blockquote>${quote.map(renderInlineMarkdown).join("<br>")}</blockquote>`);
      continue;
    }

    const paragraph = [line];
    index += 1;
    while (index < lines.length && !isBlockStart(lines, index)) {
      paragraph.push(lines[index]);
      index += 1;
    }
    output.push(`<p>${paragraph.map(renderInlineMarkdown).join("<br>")}</p>`);
  }

  return output.join("");
}

// Read-only diagnostic surface for local UI tests and plugin integrations.
function eventDelta(events = [], cursor = null) {
  if (!events.length) return { events: [], cursor, requiresRebuild: false };
  const nextCursor = events.at(-1)?.id || cursor;
  if (!cursor) return { events, cursor: nextCursor, requiresRebuild: false };
  const cursorIndex = events.findIndex((event) => event.id === cursor);
  if (cursorIndex < 0) {
    return { events: [], cursor: nextCursor, requiresRebuild: true };
  }
  return {
    events: events.slice(cursorIndex + 1),
    cursor: nextCursor,
    requiresRebuild: false,
  };
}

function mergeTaskEventUpdate(previousTask, update) {
  if (!update?.event_delta) return update;
  if (!previousTask || previousTask.id !== update.id) return null;
  const previousEvents = Array.isArray(previousTask.events) ? previousTask.events : [];
  const suffix = Array.isArray(update.events) ? update.events : [];
  const cursorId = suffix[0]?.id;
  if (!cursorId) return null;
  const cursorIndex = previousEvents.findIndex((event) => event?.id === cursorId);
  if (cursorIndex < 0) return null;
  return {
    ...update,
    conversation_turns: update.conversation_turns ?? previousTask.conversation_turns ?? [],
    events: [...previousEvents.slice(0, cursorIndex), ...suffix],
  };
}

function isCurrentTaskRequest(expectedTaskId, expectedGeneration) {
  return taskId === expectedTaskId && taskViewGeneration === expectedGeneration;
}

function openClawStatusText(openclaw = {}) {
  if (typeof isEnglish === "function" && isEnglish()) {
    if (openclaw.gateway_ready) return `OpenClaw: Gateway connected${openclaw.tool_count == null ? "" : ` · ${openclaw.tool_count} tools`}`;
    if (!openclaw.cli_installed) return "OpenClaw: not installed (native tools remain available)";
    return openclaw.auto_start
      ? "OpenClaw: Gateway offline (will start on demand)"
      : "OpenClaw: Gateway offline (automatic start is disabled)";
  }
  if (openclaw.gateway_ready) {
    return `OpenClaw：Gateway 已连接${openclaw.tool_count == null ? "" : ` · ${openclaw.tool_count} tools`}`;
  }
  if (!openclaw.cli_installed) return "OpenClaw：未安装（原生工具可用）";
  return openclaw.auto_start
    ? "OpenClaw：Gateway 未启动（需要时自动启动）"
    : "OpenClaw：Gateway 未启动（自动启动已关闭）";
}

function groupLocalCapabilities(tools = []) {
  const definitions = [
    ["files-development", uiText("文件与开发", "Files & development"), ["filesystem", "shell", "sandbox", "process_manager", "skills", "mcp", "memory"]],
    ["web-research", uiText("网页与研究", "Web & research"), ["web", "provider_web_search", "background_browser", "openclaw"]],
    ["computer-devices", uiText("电脑与设备", "Computer & devices"), ["computer", "computer_use", "live_computer_use", "windows_ui", "macos_ui", "vision", "mobile_device", "clipboard", "request_human_action"]],
    ["automation-collaboration", uiText("自动化与协作", "Automation & collaboration"), ["cron", "update_settings", "feishu", "telegram"]],
    ["media-documents", uiText("媒体与文档", "Media & documents"), ["document", "generate_media"]],
  ];
  const prepared = definitions.map(([id, label, names]) => ({ id, label, names: new Set(names), tools: [] }));
  const other = { id: "other", label: uiText("其他能力", "Other capabilities"), tools: [] };
  (Array.isArray(tools) ? tools : []).forEach((tool) => {
    const identifier = String(tool?.name || tool?.id || "");
    const target = prepared.find((group) => group.names.has(identifier)) || other;
    target.tools.push(tool);
  });
  return [...prepared, other]
    .filter((group) => group.tools.length)
    .map(({ names, ...group }) => group);
}

function humanizeTaskFailure(value) {
  const detail = String(value || "").trim();
  const lower = detail.toLowerCase();
  if (/429|rate.?limit|too many requests|quota|余额|额度|限流/.test(lower)) {
    return {
      summary: uiText("模型服务当前繁忙或额度不足。", "The model service is busy or its quota is unavailable."),
      action: uiText("稍后可用当前模型继续；也可从“继续”旁的菜单选择其他模型。", "Continue with the current model shortly, or choose another model from the menu beside Continue."),
    };
  }
  if (/401|403|unauthori[sz]ed|forbidden|api.?key|credential|鉴权|密钥|权限/.test(lower)) {
    return {
      summary: uiText("模型或工具未通过访问验证。", "The model or tool could not verify access."),
      action: uiText("在设置中检查对应密钥或权限，然后继续此任务。", "Check the relevant key or permission in Settings, then continue this task."),
    };
  }
  if (/timeout|timed out|deadline|超时/.test(lower)) {
    return {
      summary: uiText("模型或工具在规定时间内没有完成响应。", "The model or tool did not respond in time."),
      action: uiText("可直接用当前模型继续；若再次发生，也可从“继续”旁的菜单选择其他模型。", "Continue with the current model; if it happens again, choose another model from the menu beside Continue."),
    };
  }
  if (/network|connection|connect|dns|socket|网络|连接/.test(lower)) {
    return {
      summary: uiText("本地服务与模型或工具的连接中断。", "The connection between Elren and the model or tool was interrupted."),
      action: uiText("确认网络或本地服务已恢复，然后用当前模型继续。", "Confirm the network or local service has recovered, then continue with the current model."),
    };
  }
  if (/pair this phone|not paired|配对|绑定/.test(lower)) {
    return {
      summary: uiText("手机尚未与这台电脑建立有效连接。", "The phone is not currently paired with this computer."),
      action: uiText("重新完成手机绑定，再继续此任务。", "Pair the phone again, then continue this task."),
    };
  }
  if (/verif|validat|invalid response|验证|校验/.test(lower)) {
    return {
      summary: uiText("任务结果没有通过完成性检查。", "The result did not pass completion checks."),
      action: uiText("继续会保留已有进度，并从未完成处恢复。", "Continue keeps the existing progress and resumes from the unfinished step."),
    };
  }
  return {
    summary: uiText("任务未能完成。已有进度仍然保留。", "The task could not be completed. Existing progress is still available."),
    action: uiText("可直接用当前模型继续；也可从“继续”旁的菜单选择其他模型。", "Continue with the current model, or choose another model from the menu beside Continue."),
  };
}

// Read-only diagnostic surface for local UI tests and plugin integrations.
window.Elren = Object.freeze({
  artifactInspectorItems,
  eventDelta,
  groupLocalCapabilities,
  historyItemAriaLabel,
  humanizeTaskFailure,
  mergeTaskEventUpdate,
  openClawStatusText,
  presentationOverflowCheck,
  renderMarkdown,
  subagentRuns,
  subagentStatusLabel,
  subagentSummaryText,
  timelineEventIsRenderable,
  normalizeDesktopControlStatus,
});

function normalizeDesktopControlStatus(payload = {}) {
  const status = payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {};
  return {
    ok: status.ok !== false,
    active: status.active === true,
    sessionId: typeof status.session_id === "string" ? status.session_id : "",
    taskId: typeof status.task_id === "string" ? status.task_id : "",
    startedAt: typeof status.started_at === "string" ? status.started_at : "",
    expiresAt: typeof status.expires_at === "string" ? status.expires_at : "",
    stopReason: typeof status.stop_reason === "string" ? status.stop_reason : "",
  };
}

function desktopControlPollDelay() {
  if (document.hidden) return 15000;
  if (desktopControlActive) return 600;
  if (desktopControlFailureCount) return Math.min(15000, 2500 * (2 ** Math.min(desktopControlFailureCount - 1, 3)));
  const taskRunning = ["queued", "running", "waiting_approval", "waiting_user"].includes(currentTaskSnapshot?.status);
  return taskRunning ? 1100 : 5000;
}

function scheduleDesktopControlPoll(delay = desktopControlPollDelay()) {
  clearTimeout(desktopControlPollTimer);
  desktopControlPollTimer = setTimeout(pollDesktopControlStatus, Math.max(0, delay));
}

function updateDesktopControlCopy() {
  const text = $("#desktopControlStatusText");
  const stop = $("#desktopControlStop");
  if (text) text.textContent = desktopControlStopPending
    ? uiText("正在停止…", "Stopping…")
    : uiText(
      "正在控制电脑，按 Esc 强制退出",
      "Controlling your computer — press Esc to force stop",
    );
  if (stop) {
    const label = desktopControlStopPending
      ? uiText("正在停止电脑控制", "Stopping computer control")
      : uiText("立即停止电脑控制", "Stop computer control now");
    stop.setAttribute("aria-label", label);
    stop.title = label;
  }
}

function renderDesktopControlStatus(payload = {}) {
  const status = normalizeDesktopControlStatus(payload);
  const hasExplicitActive = payload
    && typeof payload === "object"
    && !Array.isArray(payload)
    && typeof payload.active === "boolean";
  const banner = $("#desktopControlStatus");
  const stop = $("#desktopControlStop");
  // Missing or malformed state is not proof that native control stopped.  Keep
  // the last known state until the backend explicitly reports active=false.
  if (!hasExplicitActive) status.active = desktopControlActive;
  desktopControlSnapshot = status;
  desktopControlActive = status.active;
  if (!banner) return status;
  updateDesktopControlCopy();
  stop?.toggleAttribute("disabled", desktopControlStopPending);
  banner.toggleAttribute("hidden", !status.active);
  banner.setAttribute("aria-hidden", String(!status.active));
  if (status.active) {
    if (typeof banner.showPopover === "function" && !banner.matches(":popover-open")) {
      try { banner.showPopover(); } catch {}
    }
  } else if (typeof banner.hidePopover === "function" && banner.matches(":popover-open")) {
    try { banner.hidePopover(); } catch {}
  }
  return status;
}

async function requestDesktopControl(path, { method = "GET", timeoutMs = 3000 } = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort("desktop-control-timeout"), timeoutMs);
  try {
    const response = await fetch(path, {
      method,
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

async function pollDesktopControlStatus() {
  if (desktopControlPollPending) return;
  desktopControlPollPending = true;
  try {
    const payload = await requestDesktopControl("/api/desktop-control/status");
    desktopControlFailureCount = 0;
    renderDesktopControlStatus(payload);
  } catch {
    // A loopback restart must not produce repeating toasts or remove the safety
    // banner while a real controller may still be active. Reconcile silently
    // as soon as the service is reachable again.
    desktopControlFailureCount += 1;
  } finally {
    desktopControlPollPending = false;
    scheduleDesktopControlPoll();
  }
}

async function stopDesktopControl() {
  if (!desktopControlActive || desktopControlStopPending) return false;
  const activeSnapshot = desktopControlSnapshot && desktopControlSnapshot.active
    ? desktopControlSnapshot
    : { ok: true, active: true };
  desktopControlStopPending = true;
  // Keep the safety surface visible until the backend explicitly confirms that
  // native control is inactive.  A lost POST must never look like a successful
  // emergency stop.
  renderDesktopControlStatus({ ...activeSnapshot, active: true });
  try {
    const payload = await requestDesktopControl("/api/desktop-control/stop", {
      method: "POST",
      timeoutMs: 4500,
    });
    const confirmedStatus = payload?.status && typeof payload.status === "object"
      ? payload.status
      : payload;
    if (payload?.ok === false || payload?.stopped !== true || confirmedStatus?.active !== false) {
      throw new Error("Computer control did not confirm an inactive state");
    }
    renderDesktopControlStatus(confirmedStatus);
    return true;
  } catch {
    desktopControlStopPending = false;
    renderDesktopControlStatus({ ...activeSnapshot, active: true });
    showToast(
      uiText(
        "停止电脑控制失败，控制状态已恢复；请重试或按 Esc。",
        "Could not stop computer control. Active status was restored; retry or press Esc.",
      ),
      "error",
    );
    scheduleDesktopControlPoll(0);
    return false;
  } finally {
    desktopControlStopPending = false;
    if (desktopControlActive) renderDesktopControlStatus(desktopControlSnapshot || activeSnapshot);
    else {
      updateDesktopControlCopy();
      $("#desktopControlStop")?.removeAttribute("disabled");
    }
  }
}

function handleDesktopControlEscape(event) {
  if (event.key !== "Escape" || event.repeat || !desktopControlActive || desktopControlStopPending) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  void stopDesktopControl();
}

function initializeDesktopControlStatus() {
  updateDesktopControlCopy();
  $("#desktopControlStop")?.addEventListener("click", () => { void stopDesktopControl(); });
  document.addEventListener("keydown", handleDesktopControlEscape, true);
  window.addEventListener("focus", () => scheduleDesktopControlPoll(0));
  scheduleDesktopControlPoll(0);
}

async function api(path, options = {}) {
  const providedHeaders = options.headers || {};
  const defaultContentType = options.body instanceof ArrayBuffer
    ? {}
    : { "Content-Type": "application/json" };
  const requestOptions = {
    ...options,
    // Bypass responses cached by WebView builds from before the server marked
    // every dynamic/private response no-store.
    cache: "no-store",
    headers: {
      ...defaultContentType,
      "Accept-Language": isEnglish() ? "en" : "zh",
      ...providedHeaders,
    },
  };
  const method = String(requestOptions.method || "GET").toUpperCase();
  const attempts = method === "GET" ? 3 : 1;
  const retryableStatuses = new Set([408, 425, 429, 502, 503, 504]);
  let response;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const controller = method === "GET" ? new AbortController() : null;
    const externalSignal = options.signal;
    const forwardAbort = () => controller?.abort(externalSignal?.reason);
    if (controller && externalSignal) {
      if (externalSignal.aborted) forwardAbort();
      else externalSignal.addEventListener("abort", forwardAbort, { once: true });
    }
    const timeout = controller ? setTimeout(() => controller.abort("request-timeout"), 12000) : null;
    try {
      response = await fetch(path, {
        ...requestOptions,
        ...(controller ? { signal: controller.signal } : {}),
      });
      if (retryableStatuses.has(response.status) && attempt < attempts - 1) {
        await response.body?.cancel?.().catch?.(() => {});
        await new Promise((resolve) => setTimeout(resolve, 250 * (2 ** attempt)));
        continue;
      }
      break;
    } catch (error) {
      const timedOut = controller?.signal.aborted && !externalSignal?.aborted;
      const normalizedError = timedOut
        ? new Error(uiText("请求超时，请稍后重试", "Request timed out; try again shortly"))
        : error;
      const retryable = error instanceof TypeError
        || timedOut
        || /failed to fetch|networkerror|load failed/i.test(String(error?.message || ""));
      if (!retryable || attempt === attempts - 1) throw normalizedError;
      await new Promise((resolve) => setTimeout(resolve, 250 * (2 ** attempt)));
    } finally {
      if (timeout) clearTimeout(timeout);
      externalSignal?.removeEventListener?.("abort", forwardAbort);
    }
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = body.detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : (detail?.message || body.message || response.statusText),
    );
  }
  return response.json();
}

function updateModelBadge(task = currentTaskSnapshot) {
  const badge = $("#activeModelBadge");
  if (!badge) return;
  const taskKey = task?.id;
  const isPro = task?.active_model === "deepseek-v4-pro";
  const visible = Boolean(isPro && taskKey && !dismissedProNotices.has(taskKey));
  badge.classList.toggle("hidden", !visible);
  if (!visible) {
    badge.replaceChildren();
    return;
  }
  badge.innerHTML = `<span>${uiText("正在使用 V4 Pro", "Using V4 Pro")}</span><button type="button" aria-label="${uiText("不再显示 V4 Pro 状态提示；模型仍继续使用 V4 Pro", "Hide this V4 Pro notice; the model will continue using V4 Pro")}" title="${uiText("仅隐藏本任务的提示，不会停止或切换 V4 Pro", "Only hide this task notice; V4 Pro will not stop or switch")}">${uiText("不再提示 ×", "Don't show again ×")}</button>`;
  badge.querySelector("button").onclick = () => {
    dismissedProNotices.add(taskKey);
    badge.classList.add("hidden");
    badge.replaceChildren();
  };
}

function updateContextUsage(task = currentTaskSnapshot) {
  const badge = $("#contextUsageBadge");
  if (!badge) return;
  const events = Array.isArray(task?.events) ? task.events : [];
  let compactIndex = -1;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (compactIndex < 0 && events[index].type === "context_compacted") compactIndex = index;
    if (compactIndex >= 0) break;
  }
  const compactEvent = compactIndex >= 0 ? events[compactIndex] : null;
  const relevantUsage = events.filter((event, index) => (
    event.type === "usage" && index > compactIndex
  ));
  const usageEvent = relevantUsage.at(-1)
    || [...events].reverse().find((event) => event.type === "usage")
    || null;
  const windowTokens = Number(usageEvent?.data?.context_window_tokens || 0);
  let promptTokens = relevantUsage.reduce((largest, event) => Math.max(
    largest,
    Number(
      event?.data?.context_tokens_used
      || event?.data?.estimated_context_tokens
      || event?.data?.usage?.prompt_tokens
      || event?.data?.usage?.input_tokens
      || 0
    ),
  ), 0);
  const checkpoint = Number(compactEvent?.data?.checkpoint || 0);
  if (!usageEvent && compactEvent) {
    promptTokens = Number(compactEvent.data?.after_estimated_tokens || 0);
  }
  if (!windowTokens || !promptTokens) {
    badge.classList.add("hidden");
    badge.classList.remove("warning", "critical");
    badge.textContent = "";
    return;
  }
  const percentage = Math.max(1, Math.min(100, Math.round((promptTokens / windowTokens) * 100)));
  badge.classList.remove("hidden");
  badge.classList.toggle("warning", percentage >= 70 && percentage < 85);
  badge.classList.toggle("critical", percentage >= 85);
  badge.textContent = checkpoint
    ? uiText(`上下文 ${percentage}% · 检查点 ${checkpoint}`, `Context ${percentage}% · checkpoint ${checkpoint}`)
    : uiText(`上下文 ${percentage}%`, `Context ${percentage}%`);
  badge.title = uiText(
    `约 ${promptTokens.toLocaleString()} / ${windowTokens.toLocaleString()} token；逼近上限时自动续接`,
    `About ${promptTokens.toLocaleString()} / ${windowTokens.toLocaleString()} tokens; continuation is automatic near the limit`,
  );
}

function renderAttachments() {
  const list = $("#attachmentList");
  if (!list) return;
  list.classList.toggle("hidden", pendingAttachments.length === 0);
  if (!pendingAttachments.length) {
    list.replaceChildren();
    return;
  }
  list.innerHTML = pendingAttachments.map((attachment, index) => `
    <span class="attachment-chip" title="${escapeHtml(attachment.name)}">
      <span>${escapeHtml(attachment.name)}</span>
      <button type="button" data-remove-attachment="${index}" aria-label="${uiText("移除", "Remove")} ${escapeHtml(attachment.name)}"><span class="app-symbol app-symbol-close" aria-hidden="true"></span></button>
    </span>
  `).join("");
  list.querySelectorAll("[data-remove-attachment]").forEach((button) => {
    button.onclick = () => {
      pendingAttachments.splice(Number(button.dataset.removeAttachment), 1);
      renderAttachments();
    };
  });
}

function sentAttachmentMarkup(attachments = []) {
  if (!attachments.length) return "";
  const entries = attachments.map((attachment) => {
    const path = typeof attachment === "string" ? attachment : attachment.path || "";
    const name = typeof attachment === "string" ? path.split(/[\\/]/).at(-1) : attachment.name;
    let url = typeof attachment === "object" ? attachment.url || "" : "";
    if (!url && path) {
      const normalized = path.replaceAll("\\", "/");
      const marker = normalized.toLowerCase().lastIndexOf("/uploads/");
      if (marker >= 0) {
        url = "/api/uploads/" + normalized.slice(marker + 9).split("/").map(encodeURIComponent).join("/");
      }
    }
    return { name, url, preview: mediaElementMarkup(name, url) };
  });
  const names = entries.map(({ name, url }) => {
    const safeName = escapeHtml(name || uiText("未命名附件", "Unnamed attachment"));
    return url
      ? `<a class="sent-attachment-name" href="${escapeHtml(url)}" download title="${safeName}">${safeName}</a>`
      : `<span class="sent-attachment-name" title="${safeName}">${safeName}</span>`;
  });
  const previews = entries.map((entry) => entry.preview).filter(Boolean).join("");
  return `<div class="sent-attachments"><span class="sent-attachments-label">${uiText("附件：", "Attachments:")}</span><div class="sent-attachment-names">${names.join("")}</div>${previews ? `<div class="media-preview-grid">${previews}</div>` : ""}</div>`;
}

function mediaElementMarkup(name, url) {
  if (!url) return "";
  const extension = String(name || "").split(".").at(-1).toLowerCase();
  const safeUrl = escapeHtml(url);
  const safeName = escapeHtml(name || uiText("媒体文件", "Media file"));
  if (["jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "heic"].includes(extension)) {
    return `<a class="media-preview" href="${safeUrl}" download><img src="${safeUrl}" alt="${safeName}" loading="lazy"></a>`;
  }
  if (["mp4", "webm", "mov", "mkv"].includes(extension)) {
    return `<div class="media-preview"><video src="${safeUrl}" controls preload="metadata"></video><a href="${safeUrl}" download>${safeName}</a></div>`;
  }
  if (["mp3", "wav", "flac", "m4a", "aac", "ogg", "opus"].includes(extension)) {
    return `<div class="media-preview audio"><audio src="${safeUrl}" controls preload="metadata"></audio><a href="${safeUrl}" download>${safeName}</a></div>`;
  }
  return "";
}

function generatedMediaMarkup(content) {
  const matches = String(content || "").match(/generated-(?:image|video|music)-[A-Za-z0-9._-]+\.(?:jpe?g|png|webp|gif|svg|mp4|webm|mov|mkv|mp3|wav|flac|m4a|aac|ogg|opus)/gi) || [];
  const names = [...new Set(matches.map((name) => name.split(/[\\/]/).at(-1)))];
  if (!names.length) return "";
  return `<div class="media-preview-grid generated-media">${names.map((name) => mediaElementMarkup(name, `/api/artifacts/${encodeURIComponent(name)}`)).join("")}</div>`;
}

async function uploadFiles(files) {
  if (uploadRequestPending) return;
  const candidates = Array.from(files || []);
  if (!candidates.length) return;
  const uploadScope = composerDraftScope;
  const availableSlots = Math.max(0, 20 - pendingAttachments.length);
  const selected = candidates.slice(0, availableSlots);
  if (!selected.length) {
    showToast(uiText("每条指令最多添加 20 个附件", "Up to 20 attachments can be added to each instruction"), "error");
    return;
  }
  uploadRequestPending = true;
  $("#uploadFile").disabled = true;
  syncComposerAction();
  try {
    for (const file of selected) {
      const attachment = await api("/api/uploads", {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream",
          "X-Filename": encodeURIComponent(file.name),
          "X-Content-Type": file.type || "application/octet-stream",
        },
        body: await file.arrayBuffer(),
      });
      // Navigation changes the view, not ownership of an upload. Preserve all
      // selected files in the originating draft even across A → B → A.
      if (uploadScope === composerDraftScope) {
        pendingAttachments.push(attachment);
        renderAttachments();
      } else {
        const draft = composerDrafts.get(uploadScope) || { text: "", systemDraft: "", attachments: [] };
        draft.attachments.push(attachment);
        composerDrafts.set(uploadScope, draft);
      }
    }
    showToast(isEnglish()
      ? `${selected.length} ${selected.length === 1 ? "file" : "files"} uploaded and attached to the originating chat draft (stored locally)`
      : `已上传 ${selected.length} 个文件并保留在原聊天草稿中，仅存储在本机`);
  } catch (error) {
    showToast(`${uiText("原聊天附件上传未全部完成；已完成的文件已保留：", "The originating chat upload did not fully complete; completed files were retained: ")}${error.message}`, "error");
  } finally {
    uploadRequestPending = false;
    $("#uploadFile").disabled = false;
    $("#fileInput").value = "";
    syncComposerAction();
  }
}

function showToast(message, kind = "success", regionSelector = "#appToastRegion") {
  const region = $(regionSelector);
  if (!region) return;
  const toast = document.createElement("div");
  toast.className = `toast ${kind}`;
  toast.setAttribute("role", kind === "error" ? "alert" : "status");
  toast.textContent = message;
  region.replaceChildren(toast);
  setTimeout(() => toast.remove(), 5200);
}

function showWorkspaceToast(message, kind = "success") {
  const dialog = $("#workspaceDialog");
  showToast(message, kind, dialog?.open ? "#toastRegion" : "#appToastRegion");
}

function nativeOcrLabel(vision = {}) {
  const source = String(vision.local_ocr?.source || "");
  if (/Apple Vision/i.test(source)) return "Apple Vision OCR";
  if (/Windows[. ]Media[. ]Ocr|Windows OCR/i.test(source)) return "Windows OCR";
  return isEnglish() ? "Local OCR" : "本机 OCR";
}

function renderVisionSettings(vision = {}) {
  const card = $("#visionSetupCard");
  if (!card) return;
  const title = $("#visionSetupTitle");
  const detail = $("#visionSetupDetail");
  const local = vision.local_ocr || {};
  const paddle = vision.paddle_ocr || {};
  const semantic = vision.semantic_fallback || vision.google_fallback || {};
  const semanticState = visionFallbackStatus(semantic);
  const semanticIsLocal = semantic.local === true;
  const ready = Boolean(local.ready || paddle.ready || semantic.ready);
  if (isEnglish()) {
    card.classList.toggle("ready", ready);
    card.classList.toggle("attention", !ready);
    title.textContent = ready ? "Layered image recognition is ready" : "Image recognition needs attention";
    const localLabel = local.ready ? `${nativeOcrLabel(vision)} ready (${(local.available_languages || []).join(" / ") || "system language"})` : `${nativeOcrLabel(vision)} unavailable`;
    const paddleLabel = paddle.ready ? "Baidu PaddleOCR AI ready (local CPU inference)" : "Baidu PaddleOCR AI unavailable";
    const semanticLabel = semantic.ready
      ? `${semanticIsLocal ? "Local visual model" : "Google semantic fallback"} ready (${semantic.model})`
      : semantic.configured && !semantic.probed
        ? "Google semantic fallback configured · verifying in the background"
        : semantic.configured
          ? `Google semantic fallback ${semanticState.label}${semanticState.hint ? ` (${semanticState.hint})` : ""}`
          : "Google semantic fallback not configured";
    detail.textContent = `${localLabel}; ${paddleLabel}; ${semanticLabel}. Text extraction stays local. Semantic visual checks prefer a runtime-declared local visual model, then use the configured cloud fallback only when enabled. Each cloud task re-detects its current public egress.`;
    return;
  }
  card.classList.toggle("ready", ready);
  card.classList.toggle("attention", !ready);
  title.textContent = ready ? "分层图片识别链路已启用" : "图片识别链路需要检查";
  const semanticLabel = semantic.ready
    ? `${semanticIsLocal ? "本地视觉模型" : "Google 语义视觉备用"}已就绪（${semantic.model}）`
    : semantic.configured && !semantic.probed
      ? "Google 语义视觉备用已配置 · 正在后台验证"
      : semantic.configured
        ? `Google 语义视觉备用${semanticState.label}${semanticState.hint ? `（${semanticState.hint}）` : ""}`
        : "Google 语义视觉备用未配置";
  detail.textContent = `${nativeOcrLabel(vision)} ${local.ready ? "就绪" : "不可用"}；${paddle.ready ? "百度飞桨 PaddleOCR AI 就绪（CPU，本机推理）" : "百度飞桨 PaddleOCR AI 不可用"}；${semanticLabel}。文字提取始终留在本机；语义理解优先使用运行时明确声明支持视觉的本地模型，再按设置使用云端备用。云端任务会重新识别当前公网出口。`;
}

function visionFallbackStatus(google = {}) {
  if (google.ready) return { label: isEnglish() ? "Ready" : "已就绪", className: "ready" };
  if (google.configured && !google.probed) {
    return { label: isEnglish() ? "Verifying" : "验证中", className: "pending" };
  }
  const error = String(google.last_error || "");
  if (google.configured && /location is not supported|unsupported (?:location|region|country)/i.test(error)) {
    return {
      label: isEnglish() ? "Egress region blocked" : "出口地区受限",
      className: "unavailable",
      hint: isEnglish()
        ? "Google does not accept this network egress. Switch to a Gemini-supported VPN node, then probe again."
        : "当前 Google API 不接受这条网络出口；切换到支持 Gemini API 的节点后重新探测",
    };
  }
  if (google.configured) {
    return {
      label: isEnglish() ? "Unavailable" : "不可用",
      className: "unavailable",
      hint: error
        ? String(error).replace(/^(?:Google semantic|Semantic vision) fallback unavailable:\s*/i, "")
        : "",
    };
  }
  return { label: isEnglish() ? "Not configured" : "未配置", className: "inactive" };
}

function visionCapabilityMarkup(vision = {}) {
  const local = vision.local_ocr || {};
  const paddle = vision.paddle_ocr || {};
  const semantic = vision.semantic_fallback || vision.google_fallback || {};
  const semanticStatus = visionFallbackStatus(semantic);
  const statusLabel = (ready) => ready ? (isEnglish() ? "Ready" : "已就绪") : (isEnglish() ? "Offline" : "未就绪");
  const labels = isEnglish()
    ? ["Image recognition", "Layered vision", nativeOcrLabel(vision), "PaddleOCR AI", semantic.local ? "Local visual model" : "Google semantic fallback"]
    : ["图片识别", "分层视觉链路", nativeOcrLabel(vision), "飞桨 OCR AI", semantic.local ? "本地视觉模型" : "Google 语义备用"];
  const details = isEnglish()
    ? ["System text extraction", "Local CPU inference", "Cloud scene understanding"]
    : ["系统文字提取", "CPU 本机推理", "云端画面理解"];
  const statusItem = (name, state, detail, className) => `
    <div class="capability-vision-item is-${className}" role="listitem">
      <i aria-hidden="true"></i>
      <span><strong>${escapeHtml(name)} · ${escapeHtml(state)}</strong><small>${escapeHtml(detail)}</small></span>
    </div>`;
  return `
    <section class="capability-vision">
      <header><b>${labels[0]}</b><span>${labels[1]}</span></header>
      <div class="capability-vision-grid" role="list">
        ${statusItem(labels[2], statusLabel(local.ready), details[0], local.ready ? "ready" : "unavailable")}
        ${statusItem(labels[3], statusLabel(paddle.ready), details[1], paddle.ready ? "ready" : "unavailable")}
        ${statusItem(labels[4], semanticStatus.label, semanticStatus.hint || details[2], semanticStatus.className)}
      </div>
    </section>`;
}

function renderRuntimeStatus(status, { trustModelCapabilities = true } = {}) {
  if (!status) return;
  latestStatus = status;
  const localModelReady = Boolean(status.local_models?.ready);
  const modelApiConfigured = Boolean(status.primary_key_configured || Number(status.model_provider_count || 0) > 0 || localModelReady);
  $("#apiDot").className = `dot ${modelApiConfigured ? "ok" : "bad"}`;
  $("#apiText").textContent = isEnglish()
    ? (localModelReady ? "Local model ready" : (modelApiConfigured ? "Model API configured" : "Model API key missing"))
    : (localModelReady ? "本地模型已就绪" : (modelApiConfigured ? "模型 API 已配置" : "缺少模型 API 密钥"));
  const modelLabel = modelDisplayName(status.model, status.available_models || []);
  $("#modelText").textContent = modelLabel;
  $("#modelText").title = modelLabel;
  const workspace = String(status.workspace || "");
  const workspaceName = workspace.split(/[\\/]/).filter(Boolean).at(-1) || workspace || "—";
  $("#workspaceText").textContent = `${uiText("工作区", "Workspace")} · ${workspaceName}`;
  $("#workspaceText").title = workspace;
  renderVisionSettings(status.vision || {});
  // Keep the settings device list in sync with phone-side unpair/reconnect
  // events. This consumes the status payload already polled by the page, so it
  // adds no extra request and prevents stale cards from offering a second
  // revoke for a device that has already removed itself.
  if (status.mobile) renderMobileDevices(status.mobile);
  $("#openclawText").textContent = openClawStatusText(status.openclaw || {});
  const readyToolCount = Array.isArray(status.tools) ? status.tools.length : 0;
  $("#tools").innerHTML = `<span class="tools-ready-dot" aria-hidden="true"></span><span class="tools-ready-copy"><b>${escapeHtml(uiText(`${readyToolCount} 项能力已就绪`, `${readyToolCount} ${readyToolCount === 1 ? "capability" : "capabilities"} ready`))}</b><small>${uiText("按任务自动调用", "Used automatically when needed")}</small></span>`;
  // The session cache exists only to avoid an empty status panel while the
  // loopback service reconnects.  It can describe the model that was selected
  // before the user changed Settings, so never let a cached response advertise
  // reasoning levels.  Only a fresh /api/status or /api/settings response may
  // enable API-level controls in the composer.
  if (trustModelCapabilities && Array.isArray(status.available_models)) {
    const settingsProjection = Object.prototype.hasOwnProperty.call(status, "default_model")
      ? { ...status, model: status.default_model, active_model: status.model }
      : status;
    syncModelSelectors(settingsProjection);
  }
  if (isEnglish()) translateSettingsDynamic();
}

async function loadStatus() {
  try {
    const status = await api("/api/status");
    const recovered = runtimeConnectionLost;
    runtimeConnectionLost = false;
    renderRuntimeStatus(status);
    cacheRuntimeStatus(status);
    statusRetryDelay = 1000;
    if (recovered) {
      showWorkspaceToast(uiText("本地服务连接已恢复", "Local service connection restored"), "info");
    }
    clearTimeout(runtimePollTimer);
    runtimePollTimer = setTimeout(loadStatus, 15000);
  } catch (error) {
    const firstFailure = !runtimeConnectionLost;
    runtimeConnectionLost = true;
    $("#apiDot").className = "dot bad";
    $("#apiText").textContent = uiText("服务连接失败", "Service connection failed");
    if (firstFailure) {
      showWorkspaceToast(uiText("本地服务暂时断开，正在自动重连", "Local service disconnected; reconnecting automatically"), "error");
    }
    // A launcher-initiated restart briefly takes the loopback service offline.
    // Recover automatically instead of leaving the page permanently stuck on
    // the first failed request until the user manually refreshes it.
    clearTimeout(runtimePollTimer);
    runtimePollTimer = setTimeout(loadStatus, statusRetryDelay);
    statusRetryDelay = Math.min(statusRetryDelay * 2, 10000);
  }
}

function taskStatusLabel(status) {
  if (isEnglish()) {
    return {
      queued: "Queued",
      running: "Running",
      waiting_approval: "Confirming",
      waiting_user: "Waiting for your action",
      completed: "Completed",
      failed: "Failed",
      cancelled: "Stopped",
    }[status] || status;
  }
  return {
    queued: "排队中",
    running: "执行中",
    waiting_approval: "确认中",
    waiting_user: "等待用户操作",
    completed: "已完成",
    failed: "失败",
    cancelled: "已停止",
  }[status] || status;
}

function modelStreamSummary(data = {}) {
  const seconds = Math.max(0, Number(data.elapsed_seconds || 0));
  const elapsed = seconds >= 60
    ? `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`
    : `${Math.floor(seconds)}s`;
  const chars = Math.max(0, Number(data.reasoning_chars || 0) + Number(data.content_chars || 0));
  const received = new Intl.NumberFormat(isEnglish() ? "en-US" : "zh-CN", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(chars);
  if (data.final) {
    return uiText(`模型响应已返回 · ${elapsed}`, `Model response received · ${elapsed}`);
  }
  return uiText(
    `模型推理 ${elapsed} · 已接收 ${received} 字符`,
    `Model reasoning ${elapsed} · ${received} characters received`,
  );
}

function updateTaskProgress(task = null, launchText = "") {
  const progress = $("#agentProgress");
  if (!progress) return;
  // Once a task exists, progress belongs in the conversation timeline.  The
  // header is only a very short-lived launch fallback; keeping both surfaces
  // visible made the interface feel like a separate diagnostics console.
  if (task) {
    progress.classList.add("hidden");
    progress.classList.remove("terminal");
    progress.replaceChildren();
    return;
  }
  if (!task && !launchText) {
    progress.classList.add("hidden");
    progress.replaceChildren();
    return;
  }
  if (launchText) {
    progress.innerHTML = `<span class="progress-pulse"></span><b>${escapeHtml(launchText)}</b>`;
    progress.classList.remove("hidden", "terminal");
    return;
  }
}

function taskDisplayTitle(title, prompt = "") {
  const normalized = String(title || prompt || "").replace(/\s+/g, " ").trim();
  if (!normalized) return uiText("未命名任务", "Untitled task");
  return normalized.length > 52 ? `${normalized.slice(0, 52)}…` : normalized;
}

function historyItemAriaLabel(task = {}) {
  const status = taskStatusLabel(task.status);
  const suffix = ` · ${status}`;
  const fallback = uiText("未命名任务", "Untitled task");
  const title = String(task.title || task.prompt || fallback).replace(/\s+/g, " ").trim() || fallback;
  const titleLimit = Math.max(1, 120 - suffix.length);
  const shortenedTitle = title.length > titleLimit
    ? `${title.slice(0, Math.max(1, titleLimit - 1)).trimEnd()}…`
    : title;
  return `${shortenedTitle}${suffix}`.slice(0, 120);
}

function updateConversationTitle(task = currentTaskSnapshot, fallbackPrompt = "") {
  const heading = $("#conversationTitle");
  if (!heading) return;
  heading.textContent = task
    ? taskDisplayTitle(task.title, task.prompt)
    : (fallbackPrompt ? taskDisplayTitle("", fallbackPrompt) : uiText("新任务", "New task"));
  heading.title = heading.textContent;
}

function historyTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(uiDateTimeLocale(), {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function uiDateTimeLocale() {
  return isEnglish() ? "en-US" : "zh-CN";
}

function formatUiDateTime(value, options = null) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(
    uiDateTimeLocale(),
    options || { dateStyle: "medium", timeStyle: "medium" },
  ).format(date);
}

function historyMarkup(tasks) {
  return tasks.map((task) => `
    <button class="history-item ${task.id === (taskId || continuationTaskId) ? "active" : ""}" data-task-id="${escapeHtml(task.id)}" data-pinned="${Boolean(task.pinned)}" aria-label="${escapeHtml(historyItemAriaLabel(task))}">
      <span class="history-prompt" title="${escapeHtml(task.prompt)}">${escapeHtml(taskDisplayTitle(task.title, task.prompt))}</span>
      <span class="history-meta"><i class="history-status ${escapeHtml(task.status)}"></i>${escapeHtml(taskStatusLabel(task.status))} · ${task.source === "feishu" ? uiText("飞书 · ", "Feishu · ") : task.source === "telegram" ? "Telegram · " : task.source === "schedule" ? uiText("定时 · ", "Scheduled · ") : ""}${escapeHtml(historyTime(task.updated_at))}</span>
    </button>
  `).join("");
}

function closeTaskContextMenu() {
  const menu = $("#taskContextMenu");
  contextTaskId = null;
  if (!menu) return;
  menu.classList.add("hidden");
  menu.setAttribute("aria-hidden", "true");
}

function openTaskContextMenu(event, id) {
  const menu = $("#taskContextMenu");
  if (!menu || !id) return;
  event.preventDefault();
  contextTaskId = id;
  const item = [...document.querySelectorAll(".history-item")].find(button => button.dataset.taskId === id);
  const pinButton = $("#pinTaskContextAction");
  if (pinButton) {
    pinButton.dataset.nextPinned = String(item?.dataset.pinned !== "true");
    pinButton.textContent = item?.dataset.pinned === "true"
      ? uiText("取消置顶", "Unpin chat") : uiText("置顶聊天", "Pin chat");
  }
  for (const [selector, format] of [["#exportTaskMarkdown", "markdown"], ["#exportTaskJson", "json"]]) {
    const link = $(selector);
    if (link) link.href = `/api/tasks/${encodeURIComponent(id)}/export?format=${format}`;
  }
  menu.classList.remove("hidden");
  menu.setAttribute("aria-hidden", "false");
  const margin = 8;
  const left = Math.min(event.clientX, window.innerWidth - menu.offsetWidth - margin);
  const top = Math.min(event.clientY, window.innerHeight - menu.offsetHeight - margin);
  menu.style.left = `${Math.max(margin, left)}px`;
  menu.style.top = `${Math.max(margin, top)}px`;
  menu.querySelector("button")?.focus();
}

async function pinTaskFromContextMenu() {
  const id = contextTaskId;
  const pinned = $("#pinTaskContextAction").dataset.nextPinned === "true";
  closeTaskContextMenu();
  if (!id) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(id)}/pin`, { method: "PATCH", body: JSON.stringify({ pinned }) });
    await loadTaskHistory();
    showWorkspaceToast(pinned ? uiText("聊天已置顶", "Chat pinned") : uiText("已取消置顶", "Chat unpinned"), "info");
  } catch (error) {
    showWorkspaceToast(`${uiText("置顶设置保存失败：", "Could not save pin: ")}${error.message}`, "error");
  }
}

function settleConfirmation(value) {
  const resolver = confirmationResolver;
  confirmationResolver = null;
  const dialog = $("#confirmDialog");
  if (dialog?.open) dialog.close();
  resolver?.(Boolean(value));
}

function askForConfirmation(message, { title = uiText("确认操作", "Confirm action"), confirmLabel = uiText("确认", "Confirm"), danger = false } = {}) {
  const dialog = $("#confirmDialog");
  if (!dialog) return Promise.resolve(false);
  if (confirmationResolver) settleConfirmation(false);
  $("#confirmDialogTitle").textContent = title;
  $("#confirmDialogMessage").textContent = message;
  $("#acceptConfirmDialog").textContent = confirmLabel;
  $("#acceptConfirmDialog").classList.toggle("is-danger", danger);
  dialog.showModal();
  $("#acceptConfirmDialog").focus();
  return new Promise((resolve) => { confirmationResolver = resolve; });
}

async function deleteTaskFromContextMenu() {
  const id = contextTaskId;
  closeTaskContextMenu();
  if (!id) return;
  const item = [...document.querySelectorAll(".history-item")].find(
    (button) => button.dataset.taskId === id,
  );
  const title = item?.querySelector(".history-prompt")?.textContent?.trim() || uiText("此聊天", "this chat");
  if (!await askForConfirmation(
    isEnglish()
      ? `Delete “${title}”? It cannot be restored from the task list.`
      : `确定删除“${title}”吗？删除后将无法从任务列表恢复。`, {
    title: uiText("删除聊天", "Delete chat"),
    confirmLabel: uiText("删除", "Delete"),
    danger: true,
  })) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
    const deletingOpenTask = taskId === id
      || continuationTaskId === id
      || currentTaskSnapshot?.id === id;
    if (deletingOpenTask) reset();
    await loadTaskHistory();
    showWorkspaceToast(uiText("聊天已删除", "Chat deleted"), "info");
  } catch (error) {
    showWorkspaceToast(`${uiText("删除聊天失败：", "Failed to delete chat: ")}${error.message}`, "error");
  }
}

function syncRenameTaskDialogState() {
  const pending = Boolean(renameTaskId && pendingTaskRenames.has(renameTaskId));
  $("#saveRenameTask").disabled = pending;
  $("#renameTaskForm").setAttribute("aria-busy", String(pending));
}

function closeRenameTaskDialog() {
  renameTaskDialogGeneration += 1;
  renameTaskId = null;
  syncRenameTaskDialogState();
  const dialog = $("#renameTaskDialog");
  if (dialog?.open) dialog.close();
}

function renameTaskFromContextMenu() {
  const id = contextTaskId;
  const item = [...document.querySelectorAll(".history-item")].find(
    (button) => button.dataset.taskId === id,
  );
  const currentTitle = item?.querySelector(".history-prompt")?.textContent?.trim() || "";
  closeTaskContextMenu();
  if (!id) return;
  renameTaskDialogGeneration += 1;
  renameTaskId = id;
  $("#renameTaskInput").value = currentTitle;
  syncRenameTaskDialogState();
  $("#renameTaskDialog").showModal();
  $("#renameTaskInput").focus();
  $("#renameTaskInput").select();
}

function historyResultSignature(result) {
  return JSON.stringify({
    total: Number(result.total ?? result.tasks.length),
    tasks: (result.tasks || []).map((task) => [
      task.id, task.updated_at, task.status, task.title, task.source, Boolean(task.pinned),
    ]),
  });
}

async function loadTaskHistory({ append = false, background = false } = {}) {
  const history = $("#taskHistory");
  const more = $("#historyMore");
  const query = $("#historySearch").value.trim();
  const status = $("#historyStatus").value;
  const requestFilter = JSON.stringify([query, status]);
  // A cursor belongs to a successfully displayed filter, not the current
  // controls. A direct/queued More after a filter failure must fetch page one.
  if (append && requestFilter !== historyLoadedFilter) append = false;
  const foregroundFilter = !append && !background;
  if (foregroundFilter) {
    history.setAttribute("aria-busy", "true");
  }
  if (historyLoading) {
    // A quiet refresh leaves the button enabled. Queue one explicit click
    // behind it instead of dropping the action or overlapping page requests.
    if (append && !background && !historyAppendPending) {
      historyAppendPending = true;
      more.disabled = true;
      const queuedQuery = $("#historySearch").value.trim();
      const queuedStatus = $("#historyStatus").value;
      await new Promise((resolve) => historyReloadWaiters.push(resolve));
      historyAppendPending = false;
      if (!historyHasMore || queuedQuery !== $("#historySearch").value.trim()
          || queuedStatus !== $("#historyStatus").value) {
        if (!historyLoading) more.disabled = false;
        return;
      }
      return loadTaskHistory({ append: true });
    }
    // A foreground refresh represents an explicit filter/search action. Keep
    // it queued instead of silently dropping it behind an in-flight request.
    if (!append && !background) {
      historyReloadPending = true;
      return new Promise((resolve) => historyReloadWaiters.push(resolve));
    }
    return;
  }
  historyLoading = true;
  if (!background) more.disabled = true;
  if (!background) history.setAttribute("aria-busy", "true");
  // Background refreshes always inspect the first page, but an unchanged
  // response must not rewind the cursor for pages already appended below it.
  const requestOffset = append ? historyOffset : 0;
  try {
    const params = new URLSearchParams({
      limit: String(HISTORY_PAGE_SIZE),
      offset: String(requestOffset),
    });
    if (query) params.set("query", query);
    if (status) params.set("status", status);
    const result = await api(`/api/tasks?${params.toString()}`);
    const filtersChanged = query !== $("#historySearch").value.trim()
      || status !== $("#historyStatus").value;
    if (filtersChanged) {
      historyReloadPending = true;
      return;
    }
    const firstPageSignature = !append ? historyResultSignature(result) : "";
    if (background && requestFilter === historyLoadedFilter && firstPageSignature === historyFirstPageSignature) return;
    // Keep older loaded pages and their scroll position while the user reads
    // them. The unchanged signature makes a later first-page refresh retry.
    if (background && requestFilter === historyLoadedFilter && historyOffset > HISTORY_PAGE_SIZE && history.scrollTop > 0) return;
    const previousIds = new Set(knownHistoryTaskIds);
    const newlyVisibleRemoteTasks = previousIds.size
      ? result.tasks.filter((task) => !previousIds.has(task.id) && ["feishu", "telegram"].includes(task.source))
      : [];
    // Offset pagination can overlap when a task is updated between page
    // requests. Advance by the server page size, but never append an ID that
    // is already visible.
    const tasksToRender = append
      ? result.tasks.filter((task) => !knownHistoryTaskIds.has(task.id))
      : result.tasks;
    if (!append) {
      historyLoadedFilter = requestFilter;
      historyFirstPageSignature = firstPageSignature;
      knownHistoryTaskIds = new Set(result.tasks.map((task) => task.id));
    } else {
      result.tasks.forEach((task) => knownHistoryTaskIds.add(task.id));
    }
    const total = Number(result.total ?? result.tasks.length);
    $("#historyCount").textContent = String(total);
    if (!append && !result.tasks.length) {
      history.innerHTML = `<div class="history-empty">${query || status
        ? uiText("没有符合条件的任务", "No tasks match the current filters")
        : uiText("还没有任务", "No tasks yet")}</div>`;
      historyOffset = 0;
      historyHasMore = false;
      more.classList.add("hidden");
      return;
    }
    if (append) history.insertAdjacentHTML("beforeend", historyMarkup(tasksToRender));
    else history.innerHTML = historyMarkup(tasksToRender);
    if (!append) historyOffset = 0;
    historyOffset += result.tasks.length;
    historyHasMore = Boolean(result.has_more);
    more.classList.toggle("hidden", !historyHasMore);
    history.querySelectorAll(".history-item").forEach((button) => {
      button.onclick = () => openTask(button.dataset.taskId);
      button.oncontextmenu = (event) => openTaskContextMenu(event, button.dataset.taskId);
    });
    if (background && newlyVisibleRemoteTasks.length) {
      const channel = newlyVisibleRemoteTasks[0].source === "feishu" ? uiText("飞书", "Feishu") : "Telegram";
      showWorkspaceToast(
        newlyVisibleRemoteTasks.length === 1
          ? uiText(`${channel} 新任务已同步`, `New ${channel} task synced`)
          : uiText(`${newlyVisibleRemoteTasks.length} 个远程任务已同步`, `${newlyVisibleRemoteTasks.length} remote tasks synced`),
        "info",
      );
    }
  } catch (error) {
    if (query !== $("#historySearch").value.trim() || status !== $("#historyStatus").value) {
      historyReloadPending = true;
      return;
    }
    if (requestFilter === historyLoadedFilter) {
      // Keep the list AND its cursor on failure, including foreground refresh.
      if (!background) showWorkspaceToast(`${uiText("读取任务失败，已保留现有列表：", "Could not refresh tasks; the existing list was kept: ")}${error.message}`, "error");
    } else {
      // No displayed first page belongs to this filter. Never mix an error
      // row with page two or make stale task IDs look like matching results.
      historyLoadedFilter = null;
      historyOffset = 0;
      historyHasMore = false;
      historyFirstPageSignature = "";
      knownHistoryTaskIds = new Set();
      $("#historyCount").textContent = "—";
      more.classList.add("hidden");
      history.innerHTML = `<div class="history-empty">${uiText("读取失败：", "Failed to load: ")}${escapeHtml(error.message)}</div>`;
    }
  } finally {
    historyLoading = false;
    if (!background) more.disabled = false;
    if (!background) history.removeAttribute("aria-busy");
    if (historyReloadPending) {
      historyReloadPending = false;
      queueMicrotask(() => loadTaskHistory());
    } else {
      const waiters = historyReloadWaiters;
      historyReloadWaiters = [];
      waiters.forEach((resolve) => resolve());
    }
  }
}

function scheduleTaskHistorySync(delay = 2000) {
  clearTimeout(historySyncTimer);
  historySyncTimer = setTimeout(async () => {
    try {
      if (!document.hidden && !historyLoading) {
        await loadTaskHistory({ background: true });
      }
    } finally {
      scheduleTaskHistorySync(document.hidden ? 15000 : 2500);
    }
  }, delay);
}

function syncTaskViewUrl(id) {
  try {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("task", id);
    else url.searchParams.delete("task");
    if (url.toString() !== window.location.href) window.history.replaceState(null, "", url.toString());
  } catch {
    // History availability must not determine whether the task can be read.
  }
}

async function openTask(id, { activeNavigation = "navChats" } = {}) {
  navigationIntent += 1;
  // Re-selecting the displayed task is navigation, not a new loading cycle.
  if ((taskViewLoading && taskId === id)
      || (!taskViewLoading && currentTaskSnapshot?.id === id
        && (taskId === id || continuationTaskId === id))) {
    closeNavigation({ restoreMainFocus: true });
    setPrimaryNavigation(activeNavigation);
    if (!taskViewLoading) syncTaskViewUrl(id);
    return;
  }
  const viewGeneration = ++taskViewGeneration;
  clearTimeout(pollTimer);
  if (isBlankNewTaskComposer() && !$("#modelPreference")?.dataset.languageResumeModel) rememberNewTaskComposerPreferences();
  taskViewLoading = true;
  switchComposerDraft(id);
  syncTaskHumanAction(null);
  if (currentTaskSnapshot?.id !== id) dismissedArtifactInspectorTaskId = null;
  hideArtifactInspector();
  hideSubagentPanel({ keepToggle: false });
  clearTimelineNewProgress();
  // A history choice owns the currently open mobile drawer. Close it before
  // awaiting network work so a later response cannot dismiss a drawer the
  // user deliberately reopened while the task was loading.
  closeNavigation({ restoreMainFocus: true });
  setPrimaryNavigation(activeNavigation);
  taskId = id;
  window.dispatchEvent(new CustomEvent("elren:task-view-changed", { detail: { taskId: id } }));
  continuationTaskId = null;
  $("#send").disabled = true;
  try {
    const task = reconcileTaskActionState(await api(`/api/tasks/${id}`));
    if (!isCurrentTaskRequest(id, viewGeneration)) return;
    if (task.discussion_team_enabled && !$("#modelPreference option[value='discussion-team']")) {
      $("#modelPreference").insertAdjacentHTML(
        "afterbegin",
        `<option value="discussion-team">${uiText("讨论团", "Agent team")}</option>`,
      );
    }
    $("#modelPreference").value = $("#modelPreference").dataset.languageResumeModel || (task.discussion_team_enabled
      ? "discussion-team"
      : (task.model_preference || "auto"));
    refreshModelPreferenceUI();
    const activeSelector = task.active_model || task.model_preference || "auto";
    syncReasoningAvailability(activeSelector, task.reasoning_effort || "auto");
    lastEventId = null;
    pendingHumanAction = null;
    currentTaskSnapshot = task;
    syncTaskViewUrl(task.id);
    renderArtifactInspector(task);
    renderSubagentPanel(task);
    updateConversationTitle(task);
    window.dispatchEvent(new CustomEvent("elren:task-updated", { detail: task }));
    updateModelBadge(task);
    updateContextUsage(task);
    terminalStatusRendered = null;
    stopRequestPending = false;
    pollFailureCount = 0;
    timelineAutoFollow = true;
    $("#welcome").classList.add("hidden");
    $("#timeline").classList.remove("hidden");
    rebuildTaskTimeline(task);
    restoreTimelineScrollState({ follow: true }, { forceFollow: true });
    updateTaskProgress(task);
    updateTaskOverview(task);
    syncTaskHumanAction(task);
    $("#prompt").placeholder = taskComposerPlaceholder(task);
    const terminal = ["completed", "failed", "cancelled"].includes(task.status);
    if (terminal) {
      continuationTaskId = task.id;
      taskId = null;
    }
    taskViewLoading = false;
    syncComposerAction();
    // The first task snapshot already establishes the correct composer state.
    // History refresh must not delay continuation or add a redundant terminal GET.
    loadTaskHistory();
    if (!terminal) poll(id, viewGeneration);
  } catch (error) {
    if (!isCurrentTaskRequest(id, viewGeneration)) return;
    reset();
    showToast(`${uiText("无法打开任务：", "Unable to open task: ")}${error.message}`, "error");
  } finally {
    if (viewGeneration === taskViewGeneration) {
      taskViewLoading = false;
      syncComposerAction();
    }
  }
}

function syncNavigationModalState() {
  const open = document.body.classList.contains("nav-open")
    && window.matchMedia("(max-width: 960px)").matches;
  const sidebar = $("#taskSidebar");
  const main = $("main");
  if (main) main.inert = open;
  $("#mobileNav")?.setAttribute("aria-expanded", String(open));
  if (!sidebar) return;
  if (open) {
    sidebar.setAttribute("role", "dialog");
    sidebar.setAttribute("aria-modal", "true");
    sidebar.setAttribute("aria-label", uiText("任务侧栏", "Task sidebar"));
  } else {
    sidebar.removeAttribute("role");
    sidebar.removeAttribute("aria-modal");
    sidebar.removeAttribute("aria-label");
  }
}

function openNavigation() {
  hideArtifactInspector();
  hideSubagentPanel();
  document.body.classList.add("nav-open");
  syncNavigationModalState();
  // Move focus into the drawer without opening a phone/tablet keyboard. Search
  // remains one Tab away for people who opened the drawer to filter history.
  requestAnimationFrame(() => $("#newTask")?.focus({ preventScroll: true }));
}

function focusMainContentTarget() {
  requestAnimationFrame(() => {
    const humanAction = $("#humanAction");
    const takeover = humanAction && !humanAction.classList.contains("hidden")
      ? $("#takeOverHumanAction:not([disabled])")
      : null;
    (takeover || $("#prompt") || $("#mobileNav"))?.focus({ preventScroll: true });
  });
}

function closeNavigation({ restoreFocus = false, restoreMainFocus = false } = {}) {
  const wasOpen = document.body.classList.contains("nav-open");
  document.body.classList.remove("nav-open");
  syncNavigationModalState();
  if (restoreMainFocus && wasOpen) {
    focusMainContentTarget();
  } else if (restoreFocus && wasOpen) {
    requestAnimationFrame(() => $("#mobileNav")?.focus({ preventScroll: true }));
  }
}

function setPrimaryNavigation(activeId = "") {
  document.querySelectorAll(".primary-nav button").forEach((button) => {
    const active = Boolean(activeId) && button.id === activeId;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

async function showHistoryNavigation(status = "", activeId = "navChats") {
  const intent = ++navigationIntent;
  const search = $("#historySearch");
  const filter = $("#historyStatus");
  if (search) search.value = "";
  if (filter) {
    filter.value = status;
    syncSettingsSelectWidget(filter);
  }
  setPrimaryNavigation(activeId);
  closeNavigation({ restoreMainFocus: true });
  const history = $("#taskHistory");
  history?.setAttribute("aria-busy", "true");
  try {
    await loadTaskHistory();
  } finally {
    history?.setAttribute("aria-busy", "false");
  }
  if (intent !== navigationIntent) return;
  if (status === "running") {
    if (currentTaskSnapshot?.status === "running") return;
    const firstRunningTask = history?.querySelector(".history-item");
    if (firstRunningTask?.dataset.taskId) {
      await openTask(firstRunningTask.dataset.taskId, { activeNavigation: activeId });
      return;
    }
    reset();
    setPrimaryNavigation(activeId);
    setBlankTaskWelcome("running");
    return;
  }
  if (isBlankNewTaskComposer()) setBlankTaskWelcome();
}

function initializePrimaryNavigation() {
  $("#navChats")?.addEventListener("click", () => showHistoryNavigation("", "navChats"));
  $("#navPlugins")?.addEventListener("click", () => {
    setPrimaryNavigation("navPlugins");
    closeNavigation();
    $("#openCapabilities")?.click();
  });
  setPrimaryNavigation("navChats");
}

function resizePromptInput() {
  const prompt = $("#prompt");
  if (!prompt) return;
  const maxHeight = Math.max(180, Math.floor(window.innerHeight * 0.5));
  prompt.style.height = "auto";
  const nextHeight = Math.max(44, Math.min(prompt.scrollHeight, maxHeight));
  prompt.style.height = `${nextHeight}px`;
  prompt.style.overflowY = prompt.scrollHeight > maxHeight ? "auto" : "hidden";
  syncComposerAction();
}

function setSystemComposerDraft(kind, value) {
  const prompt = $("#prompt");
  if (!prompt) return;
  prompt.value = value;
  prompt.dataset.systemDraft = kind;
}

function clearSystemComposerDraft() {
  const prompt = $("#prompt");
  if (!prompt?.dataset.systemDraft) return false;
  prompt.value = "";
  delete prompt.dataset.systemDraft;
  return true;
}

function switchComposerDraft(nextScope) {
  if (composerDraftScope === nextScope) return;
  if ($("#modelPreference")?.dataset?.languageResumeModel) pendingLanguageSwitchModel = "";
  if ($("#modelPreference")?.dataset) delete $("#modelPreference").dataset.languageResumeModel;
  if ($("#reasoningPreference")?.dataset) delete $("#reasoningPreference").dataset.languageResumeReasoning;
  const prompt = $("#prompt");
  const previous = {
    text: prompt.value,
    systemDraft: prompt.dataset.systemDraft || "",
    attachments: [...pendingAttachments],
  };
  if (previous.text || previous.attachments.length) composerDrafts.set(composerDraftScope, previous);
  else composerDrafts.delete(composerDraftScope);
  composerDraftScope = nextScope;
  const draft = composerDrafts.get(nextScope);
  prompt.value = draft?.text || "";
  if (draft?.systemDraft) prompt.dataset.systemDraft = draft.systemDraft;
  else delete prompt.dataset.systemDraft;
  pendingAttachments = [...(draft?.attachments || [])];
  renderAttachments();
  resizePromptInput();
}

function clearSubmittedDraft(scope, prompt, attachments) {
  if (scope === composerDraftScope) {
    clearSubmittedComposer(prompt, attachments);
    composerDrafts.delete(scope);
    return;
  }
  // A successful send may return after navigation. Remove only what was sent
  // from its originating draft, never from the newly selected conversation.
  const draft = composerDrafts.get(scope);
  if (!draft) return;
  if (draft.text.trim() === prompt) {
    draft.text = "";
    draft.systemDraft = "";
  }
  const submitted = new Set(attachments.map((attachment) => attachment.path));
  draft.attachments = draft.attachments.filter((attachment) => !submitted.has(attachment.path));
  if (!draft.text && !draft.attachments.length) composerDrafts.delete(scope);
}

function isBlankNewTaskComposer() {
  // Terminal tasks deliberately clear taskId so the next send can continue the
  // conversation.  They are still historical-task views, not blank composers;
  // never let their projected model/cost settings overwrite new-task choices.
  return !taskId && !continuationTaskId && !currentTaskSnapshot && !startRequestPending;
}

function rememberNewTaskComposerPreferences() {
  newTaskModelPreference = $("#modelPreference")?.value || "auto";
  newTaskReasoningPreference = reasoningPreferenceValue();
}

function restoreNewTaskComposerPreferences() {
  const preference = $("#modelPreference");
  if (!preference) return;
  const desiredModel = newTaskModelPreference || "auto";
  const modelAvailable = Array.from(preference.options).some((option) => option.value === desiredModel);
  preference.value = modelAvailable ? desiredModel : "auto";
  refreshModelPreferenceUI();
  syncReasoningAvailability("", newTaskReasoningPreference || "auto");
  rememberNewTaskComposerPreferences();
}

function timelineEventIsRenderable(event) {
  if (!event || typeof event.type !== "string") return false;
  return event.type !== "step_warning"
    && !["status", "thinking", "model_stream", "usage", "approval", "human_action", "human_takeover"].includes(event.type);
}

function countTimelineNewProgress(events = [], previousEvents = [], delta = {}, becameTerminal = false) {
  let candidates = Array.isArray(delta.events) ? delta.events : [];
  if (delta.requiresRebuild) {
    const previous = Array.isArray(previousEvents) ? previousEvents : [];
    const previousIds = new Set(previous.map((event) => event?.id).filter(Boolean));
    candidates = previousIds.size
      ? (Array.isArray(events) ? events : []).filter((event) => event?.id && !previousIds.has(event.id))
      : (Array.isArray(events) ? events.slice(previous.length) : []);
  }
  return candidates.filter(timelineEventIsRenderable).length + (becameTerminal ? 1 : 0);
}

function ensureTimelineNewProgressButton() {
  let button = $("#timelineNewProgress");
  if (button) return button;
  const timeline = $("#timeline");
  const footer = timeline?.parentElement?.querySelector(":scope > footer");
  if (!timeline || !footer) return null;
  button = document.createElement("button");
  button.id = "timelineNewProgress";
  button.type = "button";
  button.className = "timeline-new-progress runtime-probe hidden";
  button.hidden = true;
  button.setAttribute("aria-live", "polite");
  button.setAttribute("aria-atomic", "true");
  button.addEventListener("click", () => clearTimelineNewProgress({ scroll: true }));
  footer.prepend(button);
  return button;
}

function updateTimelineNewProgress() {
  const button = ensureTimelineNewProgressButton();
  if (!button) return;
  const visible = timelineUnseenProgressCount > 0 && !timelineAutoFollow && Boolean(currentTaskSnapshot);
  button.hidden = !visible;
  button.classList.toggle("hidden", !visible);
  if (!visible) {
    button.textContent = "";
    button.removeAttribute("aria-label");
    return;
  }
  const count = timelineUnseenProgressCount;
  button.textContent = uiText(`${count} 条新进展 ↓`, `${count} new ${count === 1 ? "update" : "updates"} ↓`);
  button.setAttribute(
    "aria-label",
    uiText(`${count} 条新进展，点击回到底部`, `${count} new ${count === 1 ? "update" : "updates"}; return to the latest update`),
  );
}

function clearTimelineNewProgress({ scroll = false } = {}) {
  timelineUnseenProgressCount = 0;
  if (scroll) {
    const timeline = $("#timeline");
    if (timeline) timeline.scrollTop = timeline.scrollHeight;
    timelineAutoFollow = true;
  }
  updateTimelineNewProgress();
}

const SUBAGENT_STATUS_ORDER = Object.freeze({
  running: 0,
  failed: 1,
  timed_out: 1,
  interrupted: 1,
  queued: 2,
  completed: 3,
  cancelled: 4,
});

function subagentRuns(task = {}) {
  if (!Array.isArray(task?.subagent_runs)) return [];
  return task.subagent_runs
    .filter((run) => run && typeof run === "object" && String(run.id || "").trim())
    .slice(0, 100);
}

function subagentStatus(value) {
  const normalized = String(value || "").toLowerCase();
  return Object.hasOwn(SUBAGENT_STATUS_ORDER, normalized) ? normalized : "queued";
}

function subagentStatusLabel(value) {
  return {
    queued: uiText("排队中", "Queued"),
    running: uiText("运行中", "Running"),
    completed: uiText("已完成", "Completed"),
    failed: uiText("未完成", "Failed"),
    timed_out: uiText("已超时", "Timed out"),
    cancelled: uiText("已取消", "Cancelled"),
    interrupted: uiText("已中断", "Interrupted"),
  }[subagentStatus(value)];
}

function subagentDuration(milliseconds) {
  const totalSeconds = Math.max(0, Math.round(Number(milliseconds || 0) / 1000));
  if (!totalSeconds) return "";
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m${seconds ? ` ${seconds}s` : ""}`;
}

function subagentSummaryText(runs = []) {
  const counts = runs.reduce((result, run) => {
    const status = subagentStatus(run.status);
    result[status] = (result[status] || 0) + 1;
    return result;
  }, {});
  const running = Number(counts.running || 0);
  const queued = Number(counts.queued || 0);
  const completed = Number(counts.completed || 0);
  const incomplete = Number(counts.failed || 0) + Number(counts.timed_out || 0) + Number(counts.interrupted || 0);
  const cancelled = Number(counts.cancelled || 0);
  const parts = [];
  if (running) parts.push(uiText(`${running} 个运行中`, `${running} running`));
  if (queued) parts.push(uiText(`${queued} 个排队中`, `${queued} queued`));
  if (incomplete) parts.push(uiText(`${incomplete} 个未完成`, `${incomplete} incomplete`));
  if (completed) parts.push(uiText(`${completed} 个已完成`, `${completed} completed`));
  if (cancelled) parts.push(uiText(`${cancelled} 个已取消`, `${cancelled} cancelled`));
  return parts.join(" · ") || uiText("尚无子智能体", "No specialists yet");
}

function subagentPanelIsOpen() {
  const panel = $("#subagentPanel");
  if (!panel) return false;
  try {
    if (panel.matches(":popover-open")) return true;
  } catch {}
  return panel.classList.contains("is-open");
}

function syncSubagentToggle(runs = subagentRuns(currentTaskSnapshot), open = subagentPanelIsOpen()) {
  const toggle = $("#toggleSubagents");
  if (!toggle) return;
  const available = runs.length > 0;
  toggle.hidden = !available;
  toggle.setAttribute("aria-expanded", open && available ? "true" : "false");
  const summary = subagentSummaryText(runs);
  const hasActive = runs.some((run) => ["queued", "running"].includes(subagentStatus(run.status)));
  const hasFailed = runs.some((run) => ["failed", "timed_out", "interrupted"].includes(subagentStatus(run.status)));
  toggle.dataset.state = hasFailed ? "failed" : hasActive ? "active" : "complete";
  const action = open
    ? uiText("关闭子智能体", "Close specialists")
    : uiText("打开子智能体", "Open specialists");
  const label = available ? `${action} · ${summary}` : action;
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
}

function positionSubagentPanel() {
  const panel = $("#subagentPanel");
  const toggle = $("#toggleSubagents");
  if (!panel || !toggle || toggle.hidden) return;
  const rect = toggle.getBoundingClientRect();
  const right = Math.max(8, window.innerWidth - rect.right);
  const top = Math.max(8, rect.bottom + 8);
  panel.style.setProperty("--subagent-panel-right", `${right}px`);
  panel.style.setProperty("--subagent-panel-top", `${top}px`);
}

function showSubagentPanel() {
  const panel = $("#subagentPanel");
  const runs = subagentRuns(currentTaskSnapshot);
  if (!panel || !runs.length) return;
  hideArtifactInspector({ dismiss: true });
  renderSubagentPanelContents(runs);
  positionSubagentPanel();
  try {
    if (typeof panel.showPopover === "function") panel.showPopover();
    else panel.classList.add("is-open");
  } catch {
    panel.classList.add("is-open");
  }
  syncSubagentToggle(runs, true);
}

function hideSubagentPanel({ keepToggle = true, returnFocus = false } = {}) {
  const panel = $("#subagentPanel");
  if (!panel) return;
  try {
    if (panel.matches(":popover-open") && typeof panel.hidePopover === "function") panel.hidePopover();
  } catch {}
  panel.classList.remove("is-open");
  const runs = keepToggle ? subagentRuns(currentTaskSnapshot) : [];
  syncSubagentToggle(runs, false);
  if (returnFocus && runs.length) $("#toggleSubagents")?.focus({ preventScroll: true });
}

function subagentListDetails(run = {}) {
  const status = subagentStatus(run.status);
  const duration = subagentDuration(run.elapsed_ms);
  const name = isEnglish()
    ? String(run.name_en || run.name_zh || run.preset_id || uiText("子智能体", "Specialist"))
    : String(run.name_zh || run.name_en || run.preset_id || uiText("子智能体", "Specialist"));
  const assignment = String(run.assignment || uiText("未记录具体分工", "No assignment recorded"));
  const summary = String(run.summary || (
    status === "running" || status === "queued"
      ? uiText("正在独立分析，完成后会在此显示安全摘要。", "Independent analysis is in progress; a safe summary will appear here.")
      : uiText("本次运行没有可展示的摘要。", "No displayable summary was recorded for this run.")
  ));
  const evidence = Array.isArray(run.evidence) ? run.evidence.filter(Boolean).slice(0, 12) : [];
  const risks = Array.isArray(run.risks) ? run.risks.filter(Boolean).slice(0, 12) : [];
  const actualModel = String(run.actual_model || run.configured_model || "—");
  const statusText = `${subagentStatusLabel(status)}${duration ? ` · ${duration}` : ""}`;
  const listMarkup = (titleZh, titleEn, values) => values.length
    ? `<p><strong>${uiText(titleZh, titleEn)}</strong></p><ul>${values.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul>`
    : "";
  return {
    id: String(run.id || ""),
    status,
    signature: JSON.stringify({ name, assignment, summary, evidence, risks, actualModel, statusText }),
    markup: `
      <summary>
        <span class="subagent-item-icon" aria-hidden="true">${appSymbol("user")}</span>
        <span class="subagent-item-copy"><span class="subagent-item-name">${escapeHtml(name)}</span><span class="subagent-item-assignment" title="${escapeHtml(assignment)}">${escapeHtml(assignment)}</span></span>
        <span class="subagent-item-status">${escapeHtml(statusText)}</span>
      </summary>
      <div class="subagent-item-detail">
        <p>${escapeHtml(summary)}</p>
        <dl><dt>${uiText("模型", "Model")}</dt><dd>${escapeHtml(actualModel)}</dd><dt>${uiText("分工", "Assignment")}</dt><dd>${escapeHtml(assignment)}</dd></dl>
        ${listMarkup("证据", "Evidence", evidence)}
        ${listMarkup("风险", "Risks", risks)}
      </div>`,
  };
}

function renderSubagentList(runs = []) {
  const list = $("#subagentList");
  if (!list) return;
  if (!runs.length) {
    list.innerHTML = `<p class="subagent-empty">${uiText("当前任务没有调用子智能体。", "This task has not used specialists.")}</p>`;
    return;
  }
  const ordered = [...runs].sort((left, right) => {
    const statusDifference = SUBAGENT_STATUS_ORDER[subagentStatus(left.status)] - SUBAGENT_STATUS_ORDER[subagentStatus(right.status)];
    if (statusDifference) return statusDifference;
    return String(left.started_at || left.id || "").localeCompare(String(right.started_at || right.id || ""));
  });
  const staleNodes = new Set([...list.children]);
  const nodesById = new Map();
  staleNodes.forEach((node) => {
    const runId = String(node.dataset?.runId || "");
    if (runId) nodesById.set(runId, node);
  });
  ordered.forEach((run) => {
    const item = subagentListDetails(run);
    let node = nodesById.get(item.id);
    if (!node) {
      node = document.createElement("details");
      node.className = "subagent-item";
      node.dataset.runId = item.id;
      node.setAttribute("role", "listitem");
    }
    const wasOpen = Boolean(node.open);
    if (node.dataset.signature !== item.signature) {
      node.innerHTML = item.markup;
      node.dataset.signature = item.signature;
      node.open = wasOpen;
    }
    node.dataset.status = item.status;
    staleNodes.delete(node);
    list.appendChild(node);
  });
  staleNodes.forEach((node) => node.remove());
}

function renderSubagentPanelContents(runs = []) {
  const summary = subagentSummaryText(runs);
  const summaryNode = $("#subagentSummary");
  if (summaryNode && summaryNode.textContent !== summary) summaryNode.textContent = summary;
  const avatarStrip = $("#subagentAvatarStrip");
  if (avatarStrip) {
    avatarStrip.innerHTML = runs.slice(0, 4).map(() => `<span class="subagent-avatar">${appSymbol("user")}</span>`).join("")
      + (runs.length > 4 ? `<span class="subagent-avatar subagent-avatar-more">+${runs.length - 4}</span>` : "");
  }
  renderSubagentList(runs);
}

function renderSubagentPanel(task = currentTaskSnapshot) {
  const panel = $("#subagentPanel");
  if (!panel) return;
  const runs = subagentRuns(task);
  const sameTask = panel.dataset.taskId === String(task?.id || "");
  const wasOpen = sameTask && subagentPanelIsOpen();
  if (!sameTask && subagentPanelIsOpen()) hideSubagentPanel({ keepToggle: false });
  panel.dataset.taskId = String(task?.id || "");
  syncSubagentToggle(runs, wasOpen);
  if (wasOpen) {
    renderSubagentPanelContents(runs);
    positionSubagentPanel();
  }
  else if (!runs.length) hideSubagentPanel({ keepToggle: false });
}

function initializeSubagentPanel() {
  const panel = $("#subagentPanel");
  const toggle = $("#toggleSubagents");
  if (!panel || !toggle) return;
  toggle.addEventListener("click", () => {
    if (subagentPanelIsOpen()) hideSubagentPanel({ returnFocus: true });
    else showSubagentPanel();
  });
  $("#closeSubagentPanel")?.addEventListener("click", () => {
    hideSubagentPanel({ returnFocus: true });
  });
  panel.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    hideSubagentPanel({ returnFocus: true });
  });
  panel.addEventListener("toggle", (event) => {
    const open = event.newState === "open";
    if (!open) panel.classList.remove("is-open");
    syncSubagentToggle(subagentRuns(currentTaskSnapshot), open);
  });
  document.addEventListener("pointerdown", (event) => {
    // Some embedded WebViews expose the Popover API but do not consistently
    // perform native light-dismiss. Keep the explicit outside-pointer path for
    // every engine; hidePopover() is harmless when native dismissal also runs.
    if (!subagentPanelIsOpen()) return;
    if (panel.contains(event.target) || toggle.contains(event.target)) return;
    hideSubagentPanel();
  });
  window.addEventListener("resize", () => {
    if (subagentPanelIsOpen()) positionSubagentPanel();
  });
  syncSubagentToggle([], false);
}

function artifactFileExtension(value) {
  const cleaned = String(value || "")
    .trim()
    .replace(/^["'`(<\[]+/, "")
    .replace(/["'`)>\],.;:]+$/, "")
    .split(/[?#]/, 1)[0];
  const match = cleaned.match(/\.([a-z0-9]{1,8})$/i);
  if (!match) return "";
  const extension = match[1].toLowerCase();
  return [
    "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "csv", "txt", "md", "rtf",
    "png", "jpg", "jpeg", "webp", "gif", "svg", "bmp", "tif", "tiff", "heic",
    "mp4", "webm", "mov", "mkv", "mp3", "wav", "flac", "m4a", "aac", "ogg", "opus",
    "html", "css", "js", "mjs", "ts", "tsx", "jsx", "py", "ps1", "json", "yaml", "yml",
    "toml", "xml", "sql", "sh", "zip", "7z", "tar", "gz",
  ].includes(extension) ? extension : "";
}

function artifactInspectorItems(task = {}) {
  const items = [];
  const byPath = new Map();
  const cleanReference = (value) => String(value || "")
    .trim()
    .replace(/^["'`(<\[]+/, "")
    .replace(/["'`)>\],.;:]+$/, "");
  const add = (value, source, url = "") => {
    const path = cleanReference(value);
    const extension = artifactFileExtension(path);
    if (!path || !extension || path.length > 2_048) return;
    // Search results often end in .html/.pdf but are references, not task
    // artifacts. The inspector is intentionally limited to files the task
    // produced or received locally.
    if (/^(?:https?|data|blob):/i.test(path)) return;
    const normalized = path.replaceAll("\\", "/");
    const name = normalized.split("/").filter(Boolean).at(-1) || path;
    const lowerPath = normalized.toLowerCase();
    const generatedOutput = lowerPath.startsWith("outputs/") || lowerPath.includes("/outputs/");
    if (source === "tool" && !generatedOutput) return;
    if (name.includes("�")) return;
    const key = normalized.toLocaleLowerCase();
    const existing = byPath.get(key);
    if (existing) {
      if (source === "quality") existing.source = source;
      if (url && !existing.url) existing.url = url;
      return;
    }
    const item = { path, name, extension, source, url: String(url || "") };
    byPath.set(key, item);
    items.push(item);
  };
  const fromText = (value, source) => {
    const rawText = String(value || "");
    const text = rawText.length > 100_000
      ? `${rawText.slice(0, 50_000)}\n${rawText.slice(-50_000)}`
      : rawText;
    if (!text) return;
    const candidates = [];
    for (const match of text.matchAll(/\]\(([^)\r\n]+)\)/g)) candidates.push(match[1]);
    for (const match of text.matchAll(/`([^`\r\n]+)`/g)) candidates.push(match[1]);
    const artifactText = text.replace(/\bhttps?:\/\/[^\s<>"'|()[\]]+/gi, " ");
    const extensions = "(?:pdf|docx?|pptx?|xlsx?|csv|txt|md|rtf|png|jpe?g|webp|gif|svg|bmp|tiff?|heic|mp4|webm|mov|mkv|mp3|wav|flac|m4a|aac|ogg|opus|html|css|m?js|tsx?|jsx|py|ps1|json|ya?ml|toml|xml|sql|sh|zip|7z|tar|gz)";
    const rooted = new RegExp(`(?:[A-Za-z]:[\\\\/]|(?:outputs|work)[\\\\/])[^<>"'\\r\\n|]+?\\.${extensions}`, "gi");
    const compact = new RegExp(`[^\\s<>"'|()[\\]]+\\.${extensions}`, "gi");
    candidates.push(...(artifactText.match(rooted) || []), ...(artifactText.match(compact) || []));
    candidates.forEach((candidate) => add(candidate, source));
  };
  const walk = (value, source, depth = 0) => {
    if (depth > 6 || value == null) return;
    if (typeof value === "string") {
      const candidate = value.trim();
      const looksLikeDirectPath = !/[\r\n]/.test(candidate)
        && (/[\\/]/.test(candidate) || !/\s/.test(candidate));
      if (looksLikeDirectPath && artifactFileExtension(candidate)) add(candidate, source);
      else fromText(value, source);
      return;
    }
    if (Array.isArray(value)) {
      value.slice(0, 100).forEach((entry) => walk(entry, source, depth + 1));
      return;
    }
    if (typeof value !== "object") return;
    Object.entries(value).slice(0, 100).forEach(([key, child]) => {
      if (typeof child === "string" && ["url", "download_url"].includes(key) && artifactFileExtension(child)) {
        add(child, source, child.startsWith("/") ? child : "");
      } else {
        walk(child, source, depth + 1);
      }
    });
  };

  (Array.isArray(task.attachments) ? task.attachments : []).forEach((attachment) => {
    if (typeof attachment === "string") add(attachment, "attachment");
    else if (attachment && typeof attachment === "object") add(attachment.path || attachment.name, "attachment", attachment.url);
  });
  (Array.isArray(task.events) ? task.events : []).forEach((event) => {
    if (event?.type === "artifact_quality_verified" || event?.type === "artifact_quality_failed") {
      walk(event.data?.report?.artifacts || [], "quality");
    } else if (event?.type === "tool_result") {
      walk(event.data?.result, "tool");
    } else if (event?.type === "assistant") {
      fromText(event.data?.display_content ?? event.data?.content, "assistant");
    }
  });
  fromText(task.result, "result");
  const extensionRank = {
    pptx: 320, ppt: 310, pdf: 300, docx: 295, doc: 290,
    xlsx: 285, xls: 280, html: 270, zip: 260, csv: 250,
    png: 240, jpg: 240, jpeg: 240, webp: 240, gif: 235, svg: 230,
    mp4: 225, webm: 225, mov: 225, mp3: 220, wav: 220,
    rtf: 210, md: 150, txt: 145,
  };
  const sourceRank = { quality: 90, result: 75, assistant: 65, tool: 45, attachment: 10 };
  const score = (item) => {
    const normalized = String(item.path || "").replaceAll("\\", "/").toLowerCase();
    const fileName = String(item.name || "").toLowerCase();
    let value = Number(extensionRank[item.extension] || 100) + Number(sourceRank[item.source] || 0);
    if (normalized.includes("/outputs/") || normalized.startsWith("outputs/")) value += 35;
    if (/\.(?:template|preview)\.html$/i.test(fileName)) value -= 20;
    if (/^(?:build|embed|verify|check|render)[_-]/i.test(fileName)) value -= 55;
    return value;
  };
  return items.sort((left, right) => score(right) - score(left)
    || left.name.localeCompare(right.name));
}

function presentationOverflowCheck(task = {}, artifact = {}) {
  const extension = String(artifact.extension || artifactFileExtension(artifact.path || artifact.name)).toLowerCase();
  if (!["ppt", "pptx"].includes(extension)) {
    return { applicable: false, state: "not_applicable", slidesChecked: 0, issueCount: 0 };
  }
  const targetName = String(artifact.name || artifact.path || "").replaceAll("\\", "/").split("/").at(-1).toLocaleLowerCase();
  const events = Array.isArray(task.events) ? [...task.events].reverse() : [];
  for (const event of events) {
    if (!["artifact_quality_verified", "artifact_quality_failed"].includes(event?.type)) continue;
    if (String(event.data?.format || "pptx").toLowerCase() !== "pptx") continue;
    const report = event.data?.report && typeof event.data.report === "object" ? event.data.report : {};
    const reports = Array.isArray(report.artifacts) ? report.artifacts : [];
    const matching = reports.filter((item) => {
      const name = String(item?.path || "").replaceAll("\\", "/").split("/").at(-1).toLocaleLowerCase();
      return targetName && name === targetName;
    });
    if (!matching.length) continue;
    const slidesChecked = matching.reduce((total, item) => total + Number(item?.quality?.slides_checked || 0), 0);
    const issueCount = matching.reduce((total, item) => total + Number(item?.quality?.remaining_issue_count || 0), 0);
    const passed = event.type === "artifact_quality_verified"
      && matching.every((item) => item?.quality?.qa_passed === true)
      && report.ok !== false;
    return {
      applicable: true,
      state: passed ? "passed" : "failed",
      slidesChecked,
      issueCount,
    };
  }
  return { applicable: true, state: "not_recorded", slidesChecked: 0, issueCount: 0 };
}

function artifactInspectorUrl(artifact = {}) {
  if (String(artifact.url || "").startsWith("/")) return artifact.url;
  const normalized = String(artifact.path || "").replaceAll("\\", "/");
  const relative = normalized.replace(/^\.\//, "");
  if (relative.toLowerCase().startsWith("uploads/")) {
    return "/api/uploads/" + relative.slice(8).split("/").map(encodeURIComponent).join("/");
  }
  if (relative.toLowerCase().startsWith("outputs/")) {
    return "/api/artifacts/" + relative.slice(8).split("/").map(encodeURIComponent).join("/");
  }
  const uploadsMarker = normalized.toLowerCase().lastIndexOf("/uploads/");
  if (uploadsMarker >= 0) {
    return "/api/uploads/" + normalized.slice(uploadsMarker + 9).split("/").map(encodeURIComponent).join("/");
  }
  const outputsMarker = normalized.toLowerCase().lastIndexOf("/outputs/");
  if (outputsMarker >= 0) {
    return "/api/artifacts/" + normalized.slice(outputsMarker + 9).split("/").map(encodeURIComponent).join("/");
  }
  if (artifact.source !== "attachment" && normalized && !/^[A-Za-z]:\//.test(normalized) && !normalized.startsWith("/")) {
    return "/api/artifacts/" + normalized.replace(/^\.\//, "").split("/").map(encodeURIComponent).join("/");
  }
  return "";
}

function syncArtifactInspectorToggle({ available, open = false }) {
  const toggle = $("#toggleArtifactInspector");
  if (!toggle) return;
  toggle.hidden = !available;
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
  const label = open
    ? uiText("关闭产物检查器", "Close artifact inspector")
    : uiText("打开产物检查器", "Open artifact inspector");
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
}

function setArtifactInspectorModalState(open) {
  const inspector = $("#artifactInspector");
  if (!inspector) return;
  const mobileModal = Boolean(open && window.matchMedia("(max-width: 720px)").matches);
  const main = document.querySelector("main");
  [...(main?.children || [])].forEach((child) => {
    if (child !== inspector) child.inert = mobileModal;
  });
  [document.querySelector(".shell > aside"), $("#sidebarResizer")].forEach((node) => {
    if (node) node.inert = mobileModal;
  });
  if (mobileModal) {
    inspector.setAttribute("role", "dialog");
    inspector.setAttribute("aria-modal", "true");
  } else {
    inspector.removeAttribute("role");
    inspector.removeAttribute("aria-modal");
  }
}

function showArtifactInspector() {
  const inspector = $("#artifactInspector");
  if (!inspector || !currentTaskSnapshot || !artifactInspectorItems(currentTaskSnapshot).length) return;
  hideSubagentPanel();
  dismissedArtifactInspectorTaskId = null;
  inspector.hidden = false;
  inspector.classList.remove("hidden");
  inspector.setAttribute("aria-hidden", "false");
  syncArtifactInspectorToggle({ available: true, open: true });
  setArtifactInspectorModalState(true);
  if (window.matchMedia("(max-width: 720px)").matches) {
    requestAnimationFrame(() => $("#closeArtifactInspector")?.focus({ preventScroll: true }));
  }
}

function hideArtifactInspector({ dismiss = false, keepToggle = true } = {}) {
  const inspector = $("#artifactInspector");
  if (dismiss && currentTaskSnapshot?.id) dismissedArtifactInspectorTaskId = currentTaskSnapshot.id;
  if (!inspector) return;
  inspector.hidden = true;
  inspector.classList.add("hidden");
  inspector.setAttribute("aria-hidden", "true");
  setArtifactInspectorModalState(false);
  const available = Boolean(keepToggle && currentTaskSnapshot && artifactInspectorItems(currentTaskSnapshot).length);
  syncArtifactInspectorToggle({ available, open: false });
}

function updateArtifactInspectorMount(mount, markup) {
  if (!mount || mount.__elrenArtifactInspectorMarkup === markup) return false;
  mount.innerHTML = markup;
  mount.__elrenArtifactInspectorMarkup = markup;
  return true;
}

function activateArtifactInspectorTab(tab) {
  const inspector = $("#artifactInspector");
  if (!inspector || !tab) return;
  const tabs = [...inspector.querySelectorAll('[role="tab"]')];
  tabs.forEach((candidate) => {
    const active = candidate === tab;
    candidate.setAttribute("aria-selected", active ? "true" : "false");
    candidate.tabIndex = active ? 0 : -1;
    const panel = document.getElementById(candidate.getAttribute("aria-controls"));
    if (panel) {
      panel.hidden = !active;
      panel.setAttribute("aria-hidden", active ? "false" : "true");
    }
  });
}

function renderArtifactInspector(task = currentTaskSnapshot, { reveal = false } = {}) {
  const inspector = $("#artifactInspector");
  if (!inspector) return;
  const preview = $("#artifactInspectorPreviewMount");
  const source = $("#artifactInspectorSourceMount");
  const checks = $("#artifactInspectorChecksMount");
  const items = task ? artifactInspectorItems(task) : [];
  if (!task || !items.length) {
    const empty = `<div class="artifact-inspector-empty">${uiText("当前任务还没有附件或输出。", "This task has no attachments or outputs yet.")}</div>`;
    updateArtifactInspectorMount(preview, empty);
    updateArtifactInspectorMount(source, empty);
    updateArtifactInspectorMount(checks, empty);
    hideArtifactInspector({ keepToggle: false });
    return;
  }
  const sameTask = inspector.dataset.taskId === String(task.id || "");
  const wasOpen = sameTask && !inspector.hidden && !inspector.classList.contains("hidden");
  const primary = items[0];
  const url = artifactInspectorUrl(primary);
  const sourceLabels = {
    attachment: uiText("任务随附的输入文件", "Input attached to this task"),
    quality: uiText("Agent 生成，并由主机质量门记录", "Generated by the Agent and recorded by the host quality gate"),
    tool: uiText("任务工具返回的文件", "File returned by a task tool"),
    assistant: uiText("Agent 回复中引用的产物", "Artifact referenced by the Agent response"),
    result: uiText("最终结果中引用的产物", "Artifact referenced by the final result"),
  };
  const fileAction = url
    ? `<a class="artifact-inspector-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">${uiText("打开或下载文件", "Open or download file")}</a>`
    : `<span class="artifact-inspector-unavailable">${uiText("可在产物库中查看完整文件", "View the complete file in Artifacts")}</span>`;
  if (preview) {
    updateArtifactInspectorMount(preview, `<div class="artifact-inspector-file"><b title="${escapeHtml(primary.name)}">${escapeHtml(primary.name)}</b><span>${escapeHtml(primary.extension.toUpperCase())}</span></div><div class="artifact-preview-placeholder">${appSymbol("folder")}<p>${uiText("这里显示任务产物摘要；完整内容不会在检查器中被误当作已验证预览。", "This inspector shows an artifact summary; full content is not presented as a verified preview.")}</p>${fileAction}</div>`);
  }
  if (source) {
    updateArtifactInspectorMount(source, `<p><strong>${uiText("来源：", "Source: ")}</strong>${escapeHtml(sourceLabels[primary.source] || uiText("任务记录", "Task record"))}</p><p>${escapeHtml(taskDisplayTitle(task.title, task.prompt))}</p>${items.length > 1 ? `<ul>${items.slice(0, 12).map((item) => `<li>${escapeHtml(item.name)} · ${escapeHtml(sourceLabels[item.source] || uiText("任务记录", "Task record"))}</li>`).join("")}</ul>` : ""}`);
  }
  if (checks) {
    const overflow = presentationOverflowCheck(task, primary);
    let checkCopy = uiText("当前格式没有专用的幻灯片溢出检查。", "This format does not use the presentation overflow check.");
    let checkClass = "not-applicable";
    if (overflow.applicable && overflow.state === "passed") {
      checkClass = "passed";
      checkCopy = overflow.slidesChecked > 0
        ? uiText(`已完成逐页溢出检查，共检查 ${overflow.slidesChecked} 页。`, `Post-generation overflow check passed for ${overflow.slidesChecked} slides.`)
        : uiText("已通过生成后溢出检查。", "The post-generation overflow check passed.");
    } else if (overflow.applicable && overflow.state === "failed") {
      checkClass = "failed";
      checkCopy = overflow.issueCount > 0
        ? uiText(`溢出检查仍有 ${overflow.issueCount} 个问题，不能标记为通过。`, `The overflow check still has ${overflow.issueCount} unresolved ${overflow.issueCount === 1 ? "issue" : "issues"}; it is not marked as passed.`)
        : uiText("产物尚未通过生成后溢出检查。", "The artifact has not passed its post-generation overflow check.");
    } else if (overflow.applicable) {
      checkClass = "not-recorded";
      checkCopy = primary.source === "attachment"
        ? uiText("这是输入附件，没有生成后溢出检查记录。", "This is an input attachment; no post-generation overflow check is recorded.")
        : uiText("尚未记录生成后溢出检查，不能标记为通过。", "No post-generation overflow check is recorded, so this artifact is not marked as passed.");
    }
    updateArtifactInspectorMount(checks, `<div class="artifact-check ${checkClass}" role="status"><strong>${uiText("检查状态", "Check status")}</strong><p>${escapeHtml(checkCopy)}</p></div>`);
  }
  const showingNewTask = !sameTask;
  inspector.dataset.taskId = String(task.id || "");
  if (showingNewTask) activateArtifactInspectorTab($("#artifactInspectorPreviewTab"));
  if (reveal || (wasOpen && dismissedArtifactInspectorTaskId !== task.id)) showArtifactInspector();
  else hideArtifactInspector({ keepToggle: true });
}

function initializeArtifactInspector() {
  const inspector = $("#artifactInspector");
  if (!inspector) return;
  const tabs = [...inspector.querySelectorAll('[role="tab"]')];
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activateArtifactInspectorTab(tab));
    tab.addEventListener("keydown", (event) => {
      const last = tabs.length - 1;
      const next = {
        ArrowLeft: index === 0 ? last : index - 1,
        ArrowRight: index === last ? 0 : index + 1,
        Home: 0,
        End: last,
      }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      activateArtifactInspectorTab(tabs[next]);
      tabs[next].focus({ preventScroll: true });
    });
  });
  $("#toggleArtifactInspector")?.addEventListener("click", () => {
    if (inspector.hidden || inspector.classList.contains("hidden")) {
      renderArtifactInspector(currentTaskSnapshot, { reveal: true });
    } else {
      hideArtifactInspector({ dismiss: true });
      $("#toggleArtifactInspector")?.focus({ preventScroll: true });
    }
  });
  $("#closeArtifactInspector")?.addEventListener("click", () => {
    hideArtifactInspector({ dismiss: true });
    $("#toggleArtifactInspector")?.focus({ preventScroll: true });
  });
  inspector.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      hideArtifactInspector({ dismiss: true });
      $("#toggleArtifactInspector")?.focus({ preventScroll: true });
      return;
    }
    if (event.key !== "Tab" || inspector.getAttribute("aria-modal") !== "true") return;
    const focusable = [...inspector.querySelectorAll('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')]
      .filter((node) => !node.hidden && node.getClientRects().length);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus({ preventScroll: true });
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus({ preventScroll: true });
    }
  });
  window.addEventListener("resize", () => {
    if (!inspector.hidden && !inspector.classList.contains("hidden")) setArtifactInspectorModalState(true);
  });
  activateArtifactInspectorTab(tabs.find((tab) => tab.getAttribute("aria-selected") === "true") || tabs[0]);
}

function timelineIsNearBottom(timeline = $("#timeline")) {
  if (!timeline) return true;
  return timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight <= TIMELINE_BOTTOM_THRESHOLD;
}

function captureTimelineScrollState() {
  const timeline = $("#timeline");
  return {
    follow: timelineAutoFollow,
    scrollTop: timeline?.scrollTop || 0,
  };
}

function restoreTimelineScrollState(state, { forceFollow = false } = {}) {
  const timeline = $("#timeline");
  if (!timeline) return;
  if (forceFollow || state?.follow) {
    timeline.scrollTop = timeline.scrollHeight;
    timelineAutoFollow = true;
    return;
  }
  const maximum = Math.max(0, timeline.scrollHeight - timeline.clientHeight);
  timeline.scrollTop = Math.min(Math.max(0, state?.scrollTop || 0), maximum);
  timelineAutoFollow = false;
}

function setBlankTaskWelcome(mode = "default") {
  const title = $("#conversationTitle");
  const heading = $("#welcome h2");
  const description = $("#welcome p");
  if (!title || !heading || !description) return;
  if (mode === "running") {
    title.textContent = uiText("运行中", "Running");
    heading.textContent = uiText("当前没有正在运行的任务", "No tasks are running");
    description.textContent = uiText("新任务会在这里出现；已经完成的任务仍可在“聊天”中查看。", "New active tasks will appear here; completed work remains available in Chats.");
    return;
  }
  title.textContent = uiText("新任务", "New task");
  heading.textContent = uiText("今天想在项目里完成什么？", "What would you like to build today?");
  description.textContent = uiText("选择代码项目，让 Elren 理解代码、修复问题、运行测试并解释改动。也可以直接开始通用任务。", "Choose a code project to understand code, fix issues, run tests, and review changes. Or start a general task.");
}

function reset() {
  clearSystemComposerDraft();
  switchComposerDraft(null);
  navigationIntent += 1;
  taskViewGeneration += 1;
  taskViewLoading = false;
  closeNavigation();
  setPrimaryNavigation("navChats");
  // A task deep link is view state, not a permanent routing preference. Once
  // the user returns to the blank composer, remove only that parameter so a
  // refresh cannot unexpectedly reopen the historical task.
  try {
    const cleanUrl = new URL(window.location.href);
    if (cleanUrl.searchParams.has("task")) {
      cleanUrl.searchParams.delete("task");
      window.history.replaceState(null, "", cleanUrl.toString());
    }
  } catch {
    // URL cleanup is defensive; resetting the local task view must still work.
  }
  taskId = null;
  continuationTaskId = null;
  lastEventId = null;
  syncTaskHumanAction(null);
  terminalStatusRendered = null;
  stopRequestPending = false;
  pollFailureCount = 0;
  timelineAutoFollow = true;
  currentTaskSnapshot = null;
  dismissedArtifactInspectorTaskId = null;
  hideArtifactInspector();
  hideSubagentPanel({ keepToggle: false });
  clearTimelineNewProgress();
  restoreNewTaskComposerPreferences();
  updateConversationTitle(null);
  setBlankTaskWelcome();
  window.dispatchEvent(new CustomEvent("elren:task-view-changed", { detail: { taskId: null } }));
  updateModelBadge(null);
  updateContextUsage(null);
  clearTimeout(pollTimer);
  $("#timeline").innerHTML = "";
  $("#timeline").classList.add("hidden");
  $("#welcome").classList.remove("hidden");
  $("#humanAction").classList.add("hidden");
  $("#prompt").placeholder = promptPlaceholder();
  resizePromptInput();
  syncComposerAction();
  updateTaskProgress();
  $("#prompt").focus();
  loadTaskHistory();
}

function startBlankTask() {
  clearTimeout(historySearchTimer);
  const search = $("#historySearch");
  const filter = $("#historyStatus");
  if (search) search.value = "";
  if (filter) {
    filter.value = "";
    syncSettingsSelectWidget(filter);
  }
  reset();
}

function taskOverviewMarkup(task) {
  if (!task || ["completed", "failed", "cancelled"].includes(task.status)) return "";
  const events = Array.isArray(task.events) ? task.events : [];
  const toolCalls = events.filter((event) => event.type === "tool_call");
  const latestThinking = [...events].reverse().find((event) => event.type === "thinking");
  const latestStream = [...events].reverse().find((event) => (
    event.type === "model_stream"
    && (!latestThinking || Number(event.data?.step) === Number(latestThinking.data?.step))
  ));
  const latestStep = latestThinking?.data?.step;
  const latestToolCallIndex = events.findLastIndex((event) => event.type === "tool_call");
  const latestToolResultIndex = events.findLastIndex((event) => event.type === "tool_result");
  // A pending tool is rendered by renderCommandActivity as the requested
  // Codex-style "正在运行 xx" disclosure.  Do not duplicate it with a second
  // generic activity row.
  if (latestToolCallIndex > latestToolResultIndex
    && !["waiting_approval", "waiting_user"].includes(task.status)) return "";
  const latestPhase = [...events].reverse().find((event) => [
    "assistant", "model_stream", "thinking", "tool_result", "status",
  ].includes(event.type));
  const facts = [];
  if (task.active_model) facts.push(modelDisplayName(task.active_model));
  if (latestStep) {
    const numericStep = Number(latestStep);
    facts.push(Number.isFinite(numericStep)
      ? uiText(`第 ${numericStep} 步`, `Step ${numericStep}`)
      : String(latestStep));
  }
  if (latestStream) facts.push(modelStreamSummary(latestStream.data));
  if (toolCalls.length) facts.push(uiText(`${toolCalls.length} 次工具调用`, `${toolCalls.length} tool ${toolCalls.length === 1 ? "call" : "calls"}`));
  facts.unshift(taskStatusLabel(task.status));

  let label = uiText("正在准备任务", "Preparing task");
  if (task.status === "waiting_user") {
    label = uiText("正在等待你的操作", "Waiting for your action");
  } else if (task.status === "waiting_approval") {
    label = uiText("正在确认下一步操作", "Confirming the next action");
  } else if (latestPhase?.type === "thinking") {
    label = latestStep
      ? uiText(`正在等待模型响应 · 第 ${latestStep} 步`, `Waiting for the model · Step ${latestStep}`)
      : uiText("正在等待模型响应", "Waiting for the model");
  } else if (latestPhase?.type === "model_stream") {
    label = latestPhase.data?.final
      ? uiText("正在整理模型响应", "Preparing the model response")
      : uiText("正在接收模型响应", "Receiving the model response");
  } else if (latestPhase?.type === "tool_result") {
    label = uiText("正在准备下一步", "Preparing the next step");
  } else if (latestPhase?.type === "assistant") {
    label = uiText("正在完成任务", "Finishing the task");
  }

  const signature = [task.status, label, ...facts].join("|");
  return `<div class="event task-overview" data-signature="${escapeHtml(signature)}"><div class="event-icon">${appSymbol("bolt")}</div><details class="event-card live-activity"><summary><span class="progress-pulse" aria-hidden="true"></span><span class="task-overview-summary" role="status" aria-live="polite" aria-atomic="true">${escapeHtml(label)}</span><span class="task-overview-chevron" aria-hidden="true"></span></summary><div class="task-overview-detail">${escapeHtml(facts.join(" · "))}</div></details></div>`;
}

function updateTaskOverview(task) {
  const timeline = $("#timeline");
  const current = $("#timeline .task-overview");
  if (!timeline || !task) return;
  const markup = taskOverviewMarkup(task);
  if (!markup) {
    current?.remove();
    return;
  }
  const shell = document.createElement("div");
  shell.innerHTML = markup;
  const replacement = shell.firstElementChild;
  if (!replacement) return;
  if (current?.dataset.signature === replacement.dataset.signature) {
    // New events may have been appended after the live row. Keep it at the
    // bottom without rebuilding it, preserving focus and disclosure state.
    timeline.append(current);
    return;
  }
  const wasOpen = Boolean(current?.querySelector("details")?.open);
  if (wasOpen) replacement.querySelector("details").open = true;
  if (current) current.replaceWith(replacement);
  else timeline.append(replacement);
}

function originalContinuationModel(task) {
  const preference = $("#modelPreference");
  if (!preference) return "auto";
  const available = new Set([...preference.options].map((option) => option.value));
  const candidates = task?.discussion_team_enabled
    ? ["discussion-team", task?.active_model, task?.model_preference, "auto"]
    : [task?.active_model, task?.model_preference, "auto"];
  return candidates.find((selector) => selector && available.has(selector)) || "auto";
}

function continuationModelOptions(task) {
  const preference = $("#modelPreference");
  if (!preference) return [];
  const original = originalContinuationModel(task);
  const options = [...preference.options].map((option) => ({
    selector: option.value,
    label: option.textContent?.trim() || modelDisplayName(option.value),
    original: option.value === original,
  }));
  return [
    ...options.filter((option) => option.original),
    ...options.filter((option) => !option.original),
  ];
}

function continuationControlMarkup(task) {
  const options = continuationModelOptions(task);
  const menuItems = options.map((option) => `
    <button class="terminal-continue-model-option" type="button" role="menuitemradio" aria-checked="${option.original ? "true" : "false"}" data-model="${escapeHtml(option.selector)}">
      <span>${escapeHtml(option.label)}</span>${option.original ? `<small>${uiText("当前模型", "Current model")}</small>` : ""}
    </button>`).join("");
  return `<div class="terminal-continue-split">
    <button class="terminal-continue-task" type="button">${uiText("继续", "Continue")}</button>
    <button class="terminal-continue-menu-toggle" type="button" aria-label="${uiText("选择继续任务的模型", "Choose a model to continue with")}" title="${uiText("选择其他模型", "Choose another model")}" aria-haspopup="menu" aria-expanded="false"><span class="model-select-chevron" aria-hidden="true"></span></button>
    <div class="terminal-continue-model-menu" role="menu" hidden>${menuItems}</div>
  </div>`;
}

function terminalStatePresentation(task) {
  if (!task || typeof task !== "object") return null;
  if (task.status === "failed") {
    const failure = humanizeTaskFailure(task.error);
    return [
      appSymbol("warning"),
      uiText("任务需要处理", "Task needs attention"),
      uiText(`${failure.summary}${failure.action}`, `${failure.summary} ${failure.action}`),
    ];
  }
  if (task.status === "cancelled") {
    const stoppedDetail = localizeKnownSystemMessage(task.error);
    return [
      appSymbol("close"),
      uiText("任务已停止", "Task stopped"),
      stoppedDetail || uiText(
        "已停止后续模型调用和工具操作。",
        "Further model calls and tool actions were stopped.",
      ),
    ];
  }
  return null;
}

function renderTerminalState(task) {
  if (!task || terminalStatusRendered === task.status) return;
  if (task.status === "completed") {
    finalizeCommandActivityGroups();
    terminalStatusRendered = task.status;
    return;
  }
  const terminal = terminalStatePresentation(task);
  if (!terminal) return;
  finalizeCommandActivityGroups();
  terminalStatusRendered = task.status;
  const continueControl = continuationControlMarkup(task);
  const technicalDetails = task.status === "failed" && task.error
    ? `<details class="terminal-technical"><summary>${uiText("技术详情", "Technical details")}</summary><code>${escapeHtml(localizeKnownSystemMessage(task.error))}</code></details>`
    : "";
  $("#timeline").insertAdjacentHTML(
    "beforeend",
    `<div class="event terminal-event ${escapeHtml(task.status)}"><div class="event-icon">${terminal[0]}</div><div class="event-card" role="${task.status === "failed" ? "alert" : "status"}"><strong>${terminal[1]}</strong><p>${escapeHtml(terminal[2])}</p>${technicalDetails}<div class="terminal-actions">${continueControl}<button class="terminal-new-task" type="button">${uiText("开始新任务", "Start a new task")}</button></div></div></div>`,
  );
  const terminalEvent = $("#timeline .terminal-event:last-child");
  const continueAction = terminalEvent?.querySelector(".terminal-continue-task");
  const continueSplit = terminalEvent?.querySelector(".terminal-continue-split");
  const modelMenuToggle = terminalEvent?.querySelector(".terminal-continue-menu-toggle");
  const modelMenu = terminalEvent?.querySelector(".terminal-continue-model-menu");
  const newTaskAction = terminalEvent?.querySelector(".terminal-new-task");
  if (continueAction) continueAction.onclick = () => continueTaskImmediately(task);
  if (modelMenuToggle && modelMenu) {
    const closeMenu = ({ restoreFocus = false } = {}) => {
      modelMenu.hidden = true;
      modelMenuToggle.setAttribute("aria-expanded", "false");
      continueSplit?.classList.remove("menu-open");
      if (restoreFocus) modelMenuToggle.focus({ preventScroll: true });
    };
    const openMenu = () => {
      modelMenu.hidden = false;
      modelMenuToggle.setAttribute("aria-expanded", "true");
      continueSplit?.classList.add("menu-open");
      modelMenu.querySelector('[aria-checked="true"]')?.focus({ preventScroll: true });
      setTimeout(() => document.addEventListener("pointerdown", (event) => {
        if (!continueSplit?.contains(event.target)) closeMenu();
      }, { capture: true, once: true }), 0);
    };
    modelMenuToggle.onclick = () => modelMenu.hidden ? openMenu() : closeMenu();
    modelMenuToggle.onkeydown = (event) => {
      if (event.key === "ArrowDown") { event.preventDefault(); openMenu(); }
      if (event.key === "Escape") { event.preventDefault(); closeMenu({ restoreFocus: true }); }
    };
    modelMenu.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); closeMenu({ restoreFocus: true }); }
    });
    modelMenu.querySelectorAll(".terminal-continue-model-option").forEach((option) => {
      option.onclick = () => {
        const selector = option.dataset.model || originalContinuationModel(task);
        closeMenu();
        continueTaskImmediately(task, selector);
      };
    });
  }
  if (newTaskAction) newTaskAction.onclick = startBlankTask;
  if (task.status === "cancelled") showToast(uiText("任务已停止，后续操作已取消", "Task stopped; subsequent actions were cancelled"), "info");
}

async function continueTaskImmediately(task, modelSelector = "") {
  if (!task || startRequestPending || taskViewLoading || uploadRequestPending || stopRequestPending) return;
  if (currentTaskSnapshot?.id !== task.id || !["completed", "failed", "cancelled"].includes(task.status)) return;
  const preference = $("#modelPreference");
  const requestedModel = modelSelector || originalContinuationModel(task);
  if (preference && [...preference.options].some((option) => option.value === requestedModel)) {
    preference.value = requestedModel;
    refreshModelPreferenceUI();
    syncReasoningAvailability();
  }
  continuationTaskId = task.id;
  taskId = null;
  // A recovery suggestion is a request fallback, not an edit to the user's
  // composer. An existing correction is the instruction to submit and remains
  // untouched until the backend acknowledges that exact text/attachment set.
  const prompt = $("#prompt").value.trim() || uiText(
    "继续完成上一任务。请先核对已有进度，再从尚未完成的下一步安全继续。",
    "Continue the previous task. Verify existing progress, then safely resume from the next unfinished step.",
  );
  showToast(uiText("正在从已有进度继续任务…", "Continuing from the existing task progress…"), "info");
  return start({ promptOverride: prompt });
}

function isFailedToolResult(event) {
  if (event.type !== "tool_result") return false;
  const value = event.data?.result;
  return value?.ok === false || Boolean(value?.error);
}

function isFinalAssistantEvent(event, task) {
  if (event.type !== "assistant" || !["completed", "failed", "cancelled"].includes(task?.status)) return false;
  const assistants = (task.events || []).filter((item) => item.type === "assistant");
  return assistants.at(-1)?.id === event.id && Boolean(task.result);
}

function eventPresentation(event, isFinal = false) {
  const critical = isFinal
    || ["error", "loop_detected", "model_recovery_exhausted", "tool_rejected", "verification_unresolved"].includes(event.type)
    || isFailedToolResult(event)
    || (event.type === "tool_call" && event.data?.risk === "high");
  const optional = !critical && [
    "assistant", "tool_call", "tool_result", "tool_redirected", "loop_review", "model_retry", "model_failover", "context_compacted", "project_rules_loaded",
    "team_member_advice", "team_execution_turn", "team_member_report",
  ].includes(event.type);
  return { critical, optional };
}

function captureTimelineDisclosureState(timeline = $("#timeline")) {
  if (!timeline) return { commandGroups: [], liveActivity: false };
  return {
    commandGroups: [...timeline.querySelectorAll(".command-activity")].map((details) => ({
      open: details.open,
      technical: [...details.querySelectorAll(".event-technical-details")].map((item) => item.open),
    })),
    liveActivity: Boolean(timeline.querySelector(".live-activity")?.open),
  };
}

function restoreTimelineDisclosureState(timeline, state = {}) {
  if (!timeline) return;
  [...timeline.querySelectorAll(".command-activity")].forEach((details, groupIndex) => {
    const saved = state.commandGroups?.[groupIndex];
    if (!saved) return;
    details.open = Boolean(saved.open);
    [...details.querySelectorAll(".event-technical-details")].forEach((item, itemIndex) => {
      if (saved.technical?.[itemIndex] !== undefined) item.open = Boolean(saved.technical[itemIndex]);
    });
  });
  const liveActivity = timeline.querySelector(".live-activity");
  if (liveActivity && state.liveActivity) liveActivity.open = true;
}

function renderArchivedConversationTurn(turn) {
  const timeline = $("#timeline");
  timeline.insertAdjacentHTML("beforeend", `<div class="event task-prompt user-authored"><div class="event-icon">${taskSourceIcon("web")}</div><div class="event-card"><strong>${uiText("你的任务", "Your task")}</strong><p>${escapeHtml(turn.prompt)}</p>${sentAttachmentMarkup(turn.attachments || [])}</div></div>`);
  // renderEvent ignores approval/status controls. Terminal status makes old
  // command groups inert; the next prompt separates them from the live run.
  const events = Array.isArray(turn.events) ? turn.events : [];
  for (const event of events) {
    renderEvent(event, turn.status, isFinalAssistantEvent(event, turn));
  }
  if (turn.result && !events.some(event => event.type === "assistant")) {
    renderEvent({ type: "assistant", data: { content: turn.result } }, turn.status, true);
  }
}

function rebuildTaskTimeline(task) {
  const timeline = $("#timeline");
  const disclosureState = captureTimelineDisclosureState(timeline);
  const recalled = Array.isArray(task.cross_context_task_ids) ? task.cross_context_task_ids.length : 0;
  const recallMarkup = recalled
    ? `<div class="cross-context-note" title="${uiText("只参考已完成的相关对话，不会改变权限和审批规则", "Only completed relevant chats are referenced; permissions and approval rules are unchanged")}">${uiText(`已参考 ${recalled} 个历史对话`, `Using context from ${recalled} earlier ${recalled === 1 ? "chat" : "chats"}`)}</div>`
    : "";
  const sourceLabel = task.source === "feishu"
    ? uiText("飞书任务", "Feishu task")
    : task.source === "telegram"
      ? uiText("Telegram 任务", "Telegram task")
      : task.source === "schedule"
        ? uiText("定时任务", "Scheduled task")
        : "";
  timeline.innerHTML = "";
  (task.conversation_turns || []).forEach(renderArchivedConversationTurn);
  timeline.insertAdjacentHTML("beforeend", `<div class="event task-prompt user-authored"><div class="event-icon">${taskSourceIcon(task.source)}</div><div class="event-card"><strong>${isEnglish() ? "Your task" : "你的任务"}${sourceLabel ? ` · ${sourceLabel}` : ""}</strong>${recallMarkup}<p>${escapeHtml(task.prompt)}</p>${sentAttachmentMarkup(task.attachments)}</div></div>`);
  terminalStatusRendered = null;
  (task.events || []).forEach((event) => {
    if (task.status === "failed" && ["error", "loop_detected"].includes(event.type)) return;
    renderEvent(event, task.status, isFinalAssistantEvent(event, task));
  });
  updateTaskOverview(task);
  restoreTimelineDisclosureState(timeline, disclosureState);
  lastEventId = (task.events || []).at(-1)?.id || null;
  if (["completed", "failed", "cancelled"].includes(task.status)) renderTerminalState(task);
}

function localizedToolName(toolName) {
  const tool = String(toolName || "tool");
  const names = isEnglish() ? {
    background_browser: "Background browser",
    clipboard: "Clipboard",
    computer: "Desktop action",
    computer_use: "Computer Use",
    live_computer_use: "Live Computer Use",
    cron: "Scheduled task",
    document: "Document processing",
    feishu: "Feishu",
    filesystem: "Workspace file",
    generate_media: "Media generation",
    jianpu_omr: "Staff to numbered notation",
    jianpu_to_staff: "Numbered notation to staff",
    mcp: "MCP",
    memory: "Memory",
    mobile_device: "Mobile device",
    macos_ui: "macOS interface",
    openclaw: "OpenClaw",
    process_manager: "Process manager",
    provider_web_search: "Model web search",
    request_human_action: "Required user action",
    sandbox: "Sandbox",
    shell: "Command",
    skills: "Skill library",
    telegram: "Telegram",
    update_settings: "Settings update",
    vision: "Image recognition",
    web: "Web",
    windows_ui: "Windows interface",
  } : {
    background_browser: "后台浏览器",
    clipboard: "剪贴板",
    computer: "桌面操作",
    computer_use: "电脑操作",
    live_computer_use: "实时电脑控制",
    cron: "定时任务",
    document: "文档处理",
    feishu: "飞书",
    filesystem: "工作区文件",
    generate_media: "媒体生成",
    jianpu_omr: "五线谱转简谱",
    jianpu_to_staff: "简谱转五线谱",
    mcp: "MCP 服务",
    memory: "记忆",
    mobile_device: "手机设备",
    macos_ui: "macOS 界面操作",
    openclaw: "OpenClaw 扩展",
    process_manager: "进程管理",
    provider_web_search: "模型联网搜索",
    request_human_action: "需要用户操作",
    sandbox: "安全沙箱",
    shell: "命令执行",
    skills: "技能库",
    telegram: "Telegram",
    update_settings: "设置修改",
    vision: "图片识别",
    web: "网页操作",
    windows_ui: "Windows 界面操作",
  };
  if (names[tool]) return names[tool];
  const readable = tool.replace(/[._-]+/g, " ").replace(/\s+/g, " ").trim();
  return isEnglish() ? readable.replace(/\b\w/g, (letter) => letter.toUpperCase()) : readable;
}

function localizedToolSummary(data = {}) {
  const tool = String(data.tool || "tool");
  const args = data.arguments && typeof data.arguments === "object" ? data.arguments : {};
  const action = String(args.action || "run");
  const short = (value, fallback = "") => {
    const normalized = String(value ?? fallback).replace(/\s+/g, " ").trim();
    return normalized.length > 100 ? `${normalized.slice(0, 100)}…` : normalized;
  };
  const details = short(args.url || args.path || args.query || args.selector || args.name || args.command || args.text);
  const actionLabels = isEnglish() ? {
    run: "run", launch: "open", close: "close", list: "list", read: "read",
    search: "search", write: "write", append: "append", delete: "delete",
    status: "check status", focus: "focus", click: "click", type: "type",
  } : {
    run: "运行", launch: "打开", close: "关闭", list: "列出", read: "读取",
    search: "搜索", write: "写入", append: "追加", delete: "删除",
    status: "检查状态", focus: "切换到窗口", click: "点击", type: "输入",
  };
  const actionLabel = actionLabels[action] || (isEnglish() ? action : "执行");
  const label = `${localizedToolName(tool)} · ${actionLabel}`;
  return details ? `${label} · ${details}` : label;
}

function localizedEventTypeLabel(eventType) {
  const type = String(eventType || "");
  const known = {
    telegram_delivery: uiText("Telegram 发送状态", "Telegram delivery"),
    feishu_delivery: uiText("飞书发送状态", "Feishu delivery"),
    artifact_quality_verified: uiText("产物质量检查通过", "Artifact quality check passed"),
    artifact_quality_failed: uiText("产物质量检查未通过", "Artifact quality check needs attention"),
    cancelled: uiText("任务已停止", "Task stopped"),
  };
  if (known[type]) return known[type];
  if (!isEnglish()) return "系统进展";
  const words = type.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  return words ? `${words[0].toUpperCase()}${words.slice(1)}` : "System update";
}

function remoteDeliveryEventMessage(event = {}) {
  const channel = event.type === "feishu_delivery" ? uiText("飞书", "Feishu") : "Telegram";
  const status = String(event.data?.status || "").toLowerCase();
  if (status === "delivered") {
    const files = Array.isArray(event.data?.files) ? event.data.files.length : 0;
    return files
      ? uiText(`${channel} 消息和 ${files} 个文件已发送。`, `${channel} message and ${files} ${files === 1 ? "file were delivered" : "files were delivered"}.`)
      : uiText(`${channel} 消息已发送。`, `${channel} message delivered.`);
  }
  if (status === "attachment_delivery_failed") {
    return uiText(
      `${channel} 消息已发送，但部分附件发送失败；任务结果和附件仍保留在本机。`,
      `${channel} message was delivered, but some attachments failed; the result and attachments remain available locally.`,
    );
  }
  if (status === "failed") {
    return uiText(
      `${channel} 消息发送失败；任务结果仍保留在本机。`,
      `${channel} delivery failed; the task result remains available locally.`,
    );
  }
  return uiText(`${channel} 发送状态已更新。`, `${channel} delivery status updated.`);
}

function artifactQualityEventMessage(event = {}) {
  const report = event.data?.report && typeof event.data.report === "object" ? event.data.report : {};
  const artifacts = Array.isArray(report.artifacts) ? report.artifacts : [];
  const slides = artifacts.reduce((total, item) => total + Number(item?.quality?.slides_checked || 0), 0);
  const issues = artifacts.reduce((total, item) => total + Number(item?.quality?.remaining_issue_count || 0), 0);
  if (event.type === "artifact_quality_verified") {
    return slides > 0
      ? uiText(`已完成 ${slides} 页幻灯片的生成后溢出检查。`, `Post-generation overflow checks passed for ${slides} slides.`)
      : uiText("产物已通过生成后质量检查。", "The artifact passed its post-generation quality check.");
  }
  return issues > 0
    ? uiText(`检查发现 ${issues} 个待修复问题，Agent 正在继续处理。`, `The check found ${issues} unresolved ${issues === 1 ? "issue" : "issues"}; the Agent is continuing.`)
    : uiText("产物尚未通过质量检查，Agent 正在继续处理。", "The artifact has not passed its quality check; the Agent is continuing.");
}

function commandActivityPrefix(running, waitingApproval = false) {
  if (waitingApproval) return uiText("正在确认", "Confirming");
  return running ? uiText("正在运行", "Running") : uiText("运行了", "Ran");
}

function finalizeCommandActivityGroups() {
  document.querySelectorAll("#timeline .command-activity-group").forEach((group) => {
    const label = group.querySelector(".command-activity-summary");
    if (!label || !group.dataset.latestCommand) return;
    label.textContent = `${commandActivityPrefix(false)} ${group.dataset.latestCommand}`;
    group.dataset.running = "false";
  });
}

function renderCommandActivity(event, taskStatus) {
  const timeline = $("#timeline");
  if (!timeline) return;
  const active = ["queued", "running"].includes(taskStatus);
  const waitingApproval = taskStatus === "waiting_approval";
  const failed = event.type === "tool_result" && isFailedToolResult(event);
  let group = timeline.lastElementChild?.classList?.contains("command-activity-group")
    ? timeline.lastElementChild
    : null;
  if (!group) {
    timeline.insertAdjacentHTML(
      "beforeend",
      `<div class="event command-activity-group"><div class="event-icon">${appSymbol("terminal")}</div><details class="event-card command-activity"><summary><span class="command-activity-summary"></span></summary><div class="command-activity-list"></div></details></div>`,
    );
    group = timeline.lastElementChild;
  }

  const value = event.type === "tool_call" ? event.data.arguments : event.data.result;
  const toolName = localizedToolName(event.data.tool);
  if (event.type === "tool_call") group.dataset.latestCommand = localizedToolSummary(event.data);
  const latestCommand = group.dataset.latestCommand || toolName;
  const summary = group.querySelector(".command-activity-summary");
  if (summary) summary.textContent = `${commandActivityPrefix(
    active && event.type === "tool_call",
    waitingApproval && event.type === "tool_call",
  )} ${latestCommand}`;
  group.dataset.running = String(active && event.type === "tool_call");

  if (event.type === "tool_result") {
    const pendingItem = [...group.querySelectorAll('.command-activity-item[data-pending="true"]')].at(-1);
    const pendingLabel = pendingItem?.querySelector(".command-activity-text");
    if (pendingItem && pendingLabel) {
      pendingLabel.textContent = `${commandActivityPrefix(false)} ${pendingItem.dataset.command || toolName}`;
      pendingItem.dataset.pending = "false";
    }
  }

  const lineLabel = event.type === "tool_call"
    ? `${commandActivityPrefix(active, waitingApproval)} ${localizedToolSummary(event.data)}`
    : failed
      ? uiText(`${toolName} 未完成`, `${toolName} did not complete`)
      : uiText(`${toolName} 已完成`, `${toolName} completed`);
  const technicalLabel = event.type === "tool_call" ? uiText("查看参数", "View parameters") : uiText("查看结果", "View result");
  const browserPayload = event.type === "tool_result" && event.data.tool === "background_browser"
    ? (value?.result && typeof value.result === "object" ? value.result : value)
    : null;
  let displayValue = value;
  if (browserPayload && value?.ok !== false) {
    const compactPayload = { ...browserPayload };
    if (typeof compactPayload.text === "string" && compactPayload.text.length > 4000) compactPayload.text = `${compactPayload.text.slice(0, 4000)}\n…`;
    if (Array.isArray(compactPayload.links)) compactPayload.links = compactPayload.links.slice(0, 20);
    if (Array.isArray(compactPayload.interactive_elements)) compactPayload.interactive_elements = compactPayload.interactive_elements.slice(0, 30);
    displayValue = value?.result && typeof value.result === "object" ? { ...value, result: compactPayload } : compactPayload;
  }
  const browserScreenshot = event.type === "tool_result"
    && event.data.tool === "background_browser"
    && typeof browserPayload?.screenshot_url === "string"
    ? `<a class="browser-capture" href="${escapeHtml(browserPayload.screenshot_url)}" target="_blank" rel="noopener"><img src="${escapeHtml(browserPayload.screenshot_url)}" alt="${uiText("Elren 内置浏览器截图", "Elren built-in browser screenshot")}" loading="lazy"></a>`
    : "";
  const activityList = group.querySelector(".command-activity-list");
  activityList?.insertAdjacentHTML(
    "beforeend",
    `<div class="command-activity-item${failed ? " failed" : ""}"><div class="command-activity-line">${appSymbol(failed ? "warning" : "terminal")}<span class="command-activity-text">${escapeHtml(lineLabel)}</span></div><details class="event-technical-details"${failed ? " open" : ""}><summary>${technicalLabel}</summary><code>${escapeHtml(technicalPreviewText(displayValue))}</code></details>${browserScreenshot}</div>`,
  );
  if (event.type === "tool_call") {
    const item = activityList?.lastElementChild;
    if (item) {
      item.dataset.pending = "true";
      item.dataset.command = localizedToolSummary(event.data);
    }
  }
  if (failed) {
    group.classList.add("critical-event");
    group.querySelector(".command-activity").open = true;
  }
}

function renderEvent(event, taskStatus = null, isFinal = false) {
  if (!event || typeof event.type !== "string") return;
  if (!event.data || typeof event.data !== "object" || Array.isArray(event.data)) {
    event = { ...event, data: {} };
  }
  if (event.type === "step_warning") {
    return;
  }
  if (["status", "thinking", "model_stream", "usage", "approval", "human_action", "human_takeover"].includes(event.type)) return;
  if (["tool_call", "tool_result"].includes(event.type)
    && !(event.type === "tool_result" && event.data.tool === "request_human_action")) {
    renderCommandActivity(event, taskStatus);
    return;
  }
  const config = {
    assistant: [appSymbol("bolt"), "Elren"],
    tool_result: [appSymbol("send"), `${localizedToolName(event.data.tool)} · ${uiText("返回结果", "Result")}`],
    loop_review: [appSymbol("warning"), uiText("AI 正在评估是否陷入循环", "AI is evaluating whether the task is looping")],
    loop_detected: [appSymbol("warning"), uiText("检测到可能死循环，已自动停止", "A possible loop was detected and stopped automatically")],
    model_retry: [appSymbol("bolt"), uiText("模型响应异常，正在安全重试", "The model response was invalid; retrying safely")],
    model_recovery_exhausted: [appSymbol("warning"), uiText("模型恢复已安全停止", "Model recovery stopped safely")],
    verification_unresolved: [appSymbol("warning"), uiText("仍有验证未通过", "Unresolved verification failures")],
    context_compacted: [appSymbol("bolt"), uiText("上下文接近上限，已自动压缩并继续", "Context was near its limit; compressed and continuing")],
    project_rules_loaded: [appSymbol("folder"), uiText("已加载项目规则", "Project rules loaded")],
    model_selected: [appSymbol("bolt"), `${uiText("正在使用", "Using")} ${modelDisplayName(event.data.model)}`],
    tool_catalog_scoped: [appSymbol("bolt"), uiText("已准备本任务所需工具", "Task tools prepared")],
    user_message_queued: [appSymbol("user"), uiText("已发送运行中指令", "Follow-up sent while running")],
    tool_arguments_repaired: [appSymbol("send"), uiText("已自动修正工具参数", "Tool arguments corrected automatically")],
    tool_rejected: [appSymbol("warning"), uiText("已阻止违反用户约束的工具调用", "A tool call that violated user constraints was blocked")],
    tool_redirected: [appSymbol("send"), uiText("已改用内置浏览器路径", "Redirected to the built-in browser path")],
    error: [appSymbol("warning"), uiText("任务出错", "Task error")],
    model_failover: [appSymbol("bolt"), uiText("当前模型不可用，已自动切换", "The current model was unavailable; switched automatically")],
    team_discussion_started: [appSymbol("user"), uiText("讨论团开始协商", "Agent team discussion started")],
    team_leader_proposal: [appSymbol("user"), uiText("组长提出方案", "Leader proposal")],
    team_member_advice: [appSymbol("user"), uiText("组员建议", "Member advice")],
    team_consensus: [appSymbol("user"), uiText("组长已敲定执行方案", "Leader finalized the plan")],
    team_execution_turn: [appSymbol("send"), uiText("讨论团已交接控制权", "Team control handed off")],
    team_member_report: [appSymbol("send"), uiText("组员阶段已完成", "Member phase completed")],
    team_leader_delegated: [appSymbol("user"), uiText("临时组长已接管", "Acting leader took over")],
    team_leader_retry_fallback: [appSymbol("warning"), uiText("原组长仍不可用，临时组长继续", "Configured leader still unavailable; acting leader continues")],
    team_leader_restored: [appSymbol("user"), uiText("原组长已恢复接管", "Configured leader resumed")],
    telegram_delivery: [appSymbol("send"), localizedEventTypeLabel(event.type)],
    feishu_delivery: [appSymbol("send"), localizedEventTypeLabel(event.type)],
    artifact_quality_verified: [appSymbol("send"), localizedEventTypeLabel(event.type)],
    artifact_quality_failed: [appSymbol("warning"), localizedEventTypeLabel(event.type)],
    cancelled: [appSymbol("close"), localizedEventTypeLabel(event.type)],
  }[event.type] || [appSymbol("bolt"), localizedEventTypeLabel(event.type)];

  let content;
  if (event.type === "assistant") {
    const assistantContent = event.data.display_content ?? event.data.content ?? "";
    content = `<div class="markdown">${renderMarkdown(assistantContent)}</div>${generatedMediaMarkup(assistantContent)}`;
  } else if (event.type === "verification_unresolved") {
    content = verificationWarningMarkup(event.data);
  } else if (event.type === "user_message_queued") {
    content = `<p>${escapeHtml(event.data.content || "")}</p>${sentAttachmentMarkup(event.data.attachments || [])}`;
  } else if (event.type === "tool_result" && event.data.tool === "request_human_action") {
    const result = event.data.result && typeof event.data.result === "object" ? event.data.result : {};
    const message = result.completed
      ? uiText("我已完成人工操作，可以继续。", "I completed the requested manual action. You can continue.")
      : (result.issue_description || uiText("我未能完成人工操作。", "I could not complete the requested manual action."));
    content = `<p>${escapeHtml(message)}</p>`;
  } else if (["model_retry", "model_recovery_exhausted", "model_selected", "tool_catalog_scoped"].includes(event.type)) {
    // These are Elren-authored lifecycle events. Render their stable type/code
    // rather than replaying a persisted, language-specific backend sentence.
    content = `<p>${escapeHtml(localizedSystemEventMessage(event))}</p>`;
  } else if (event.type === "context_compacted") {
    const before = Number(event.data.before_estimated_tokens || 0).toLocaleString();
    const after = Number(event.data.after_estimated_tokens || 0).toLocaleString();
    content = `<p>${escapeHtml(uiText(`第 ${event.data.checkpoint || 1} 次续接检查点 · 约 ${before} → ${after} token · 详细历史仍可检索`, `Checkpoint ${event.data.checkpoint || 1} · about ${before} → ${after} tokens · detailed history remains searchable`))}</p>`;
  } else if (event.type === "model_failover") {
    content = `<p>${escapeHtml(`${modelDisplayName(event.data.from_model)} → ${modelDisplayName(event.data.to_model)}`)}</p>`;
  } else if (event.type === "project_rules_loaded") {
    const files = Array.isArray(event.data.files) ? event.data.files.join(" · ") : "";
    const source = event.data.cached
      ? uiText("已从内容缓存即时载入", "loaded instantly from the content cache")
      : uiText("已读取并建立内容指纹缓存", "read and fingerprinted for reuse");
    content = `<p>${escapeHtml(uiText(`已应用 ${event.data.count || 0} 份工作区规则`, `Applied ${event.data.count || 0} workspace rule files`))} · ${escapeHtml(source)}${files ? ` · ${escapeHtml(files)}` : ""}</p>`;
  } else if (["telegram_delivery", "feishu_delivery"].includes(event.type)) {
    content = `<p>${escapeHtml(remoteDeliveryEventMessage(event))}</p>`;
  } else if (["artifact_quality_verified", "artifact_quality_failed"].includes(event.type)) {
    content = `<p>${escapeHtml(artifactQualityEventMessage(event))}</p>`;
  } else if (event.type === "error" || event.type === "cancelled") {
    const message = localizeKnownSystemMessage(event.data.message || event.data.content || event.data.reason);
    content = `<p>${escapeHtml(message || uiText("任务状态已更新。", "Task status updated."))}</p>`;
  } else if (event.type.startsWith("team_")) {
    const participant = event.data.participant || event.data.leader || "";
    const model = event.data.model ? modelDisplayName(event.data.model) : "";
    const meta = [participant, model, event.data.role].filter(Boolean).join(" · ");
    const body = event.data.skipped
      ? uiText("本轮选择不追加建议。", "No additional advice for this round.")
      : (event.data.display_content ?? event.data.content ?? event.data.assignment ?? event.data.message ?? "");
    content = `${meta ? `<small>${escapeHtml(meta)}</small>` : ""}<div class="markdown">${renderMarkdown(body)}</div>`;
  } else {
    const message = localizeKnownSystemMessage(event.data.message || event.data.content || event.data.reason);
    content = `<p>${escapeHtml(message || uiText("系统已记录一项任务进展。", "A task update was recorded."))}</p>`;
  }

  const presentation = eventPresentation(event, isFinal);
  const classes = [
    "event",
    event.type.replaceAll("_", "-"),
    (event.type === "user_message_queued"
      || (event.type === "tool_result" && event.data.tool === "request_human_action")) ? "user-authored" : "",
    presentation.optional ? "optional-event" : "",
    presentation.critical ? "critical-event" : "",
    isFinal ? "final-response" : "",
  ].filter(Boolean).join(" ");

  $("#timeline").insertAdjacentHTML(
    "beforeend",
    `<div class="${classes}"><div class="event-icon">${config[0]}</div><div class="event-card"><strong>${escapeHtml(config[1])}</strong>${content}</div></div>`,
  );
}

function verificationWarningMarkup(data = {}) {
  const failures = Array.isArray(data.failures) ? data.failures : (data.failure ? [data.failure] : []);
  const items = failures.slice(0, 8).map((failure) => {
    const command = String(failure?.command || failure?.tool || uiText("验证步骤", "Verification step"));
    return `<li><code>${escapeHtml(command.slice(0, 1200))}</code></li>`;
  }).join("");
  return `<p>${escapeHtml(uiText("以下验证尚未通过；本轮已停止，不能据此认定改动已验收。可以在此对话继续修复或复测。", "These checks have not passed. This turn has stopped; the changes are not verified. Continue here to fix or rerun them."))}</p>${items ? `<ul>${items}</ul>` : ""}`;
}

function localizedSystemEventMessage(event) {
  const data = event?.data && typeof event.data === "object" && !Array.isArray(event.data)
    ? event.data
    : {};
  if (event?.type === "model_retry") {
    const reasonMessages = {
      unresolved_verification_failure: uiText(
        "最近一次验证仍未通过；Elren 正在让模型根据证据继续修复。",
        "The latest verification still failed; Elren asked the model to continue from the evidence.",
      ),
      unfinished_progress_narration: uiText(
        "模型只返回了进度说明；正在请求完整结果。",
        "The model returned only a progress update; a complete result was requested.",
      ),
      empty_assistant_response: uiText(
        "模型未返回可见结果；正在安全重试。",
        "The model returned no visible result; retrying safely.",
      ),
      invalid_json_output_contract: uiText(
        "模型返回的 JSON 不符合任务约定；正在请求完整的有效 JSON。",
        "The model returned JSON that did not match the task contract; complete valid JSON was requested.",
      ),
      missing_required_python_functions: uiText(
        "模型遗漏了任务要求的 Python 函数；正在请求补全。",
        "The model omitted required Python functions; a complete implementation was requested.",
      ),
      required_artifact_not_created: uiText(
        "任务要求的文件尚未实际创建；正在请求模型完成产物。",
        "The required file was not created; the model was asked to complete the artifact.",
      ),
    };
    const base = reasonMessages[data.reason] || uiText(
      "模型结果未通过结构化检查；正在安全重试。",
      "The model result did not pass a structured check; retrying safely.",
    );
    const attempt = Number(data.attempt);
    if (!Number.isInteger(attempt) || attempt < 1) return base;
    return uiText(`${base} 第 ${attempt} 次恢复尝试。`, `${base} Recovery attempt ${attempt}.`);
  }
  if (event?.type === "model_recovery_exhausted") {
    const categoryMessages = {
      empty_or_progress: uiText(
        "模型连续未返回可交付内容；Elren 已停止自动重试，避免继续消耗 API。你可以切换模型后继续。",
        "The model repeatedly returned no deliverable result. Elren stopped automatic retries to prevent further API usage; you can switch models and continue.",
      ),
      json_contract: uiText(
        "模型连续未满足 JSON-only 输出约定；Elren 已停止自动重试并保留校验错误。",
        "The model repeatedly missed the JSON-only contract. Elren stopped automatic retries and preserved the validation error.",
      ),
      missing_required_functions: uiText(
        "模型连续遗漏任务要求的函数；Elren 已停止自动重试。",
        "The model repeatedly omitted required functions. Elren stopped automatic retries.",
      ),
    };
    return categoryMessages[data.category] || uiText(
      "模型反复返回不可交付结果；Elren 已停止自动重试，避免继续消耗 API。",
      "The model repeatedly returned an undeliverable result. Elren stopped automatic retries to prevent further API usage.",
    );
  }
  if (event?.type === "model_selected") {
    const summaries = {
      manual: uiText("已使用发送栏中选择的模型。", "Using the model selected in the composer."),
      user_opt_out: uiText("已按照本次任务的模型偏好完成选择。", "The model was selected to match this task's preference."),
      default: uiText("已使用设置中的默认模型。", "Using the default model from Settings."),
      default_failover: uiText("默认模型暂不可用，已选择健康的备用模型。", "The default model was unavailable, so a healthy fallback was selected."),
      automatic: uiText("已根据当前任务自动选择模型。", "The model was selected automatically for this task."),
      automatic_fallback: uiText("云端评判暂不可用，已通过本地规则完成模型选择。", "Cloud classification was unavailable, so the model was selected with local rules."),
    };
    return summaries[data.mode] || uiText("已完成本次任务的模型选择。", "The model for this task has been selected.");
  }
  if (event?.type === "tool_catalog_scoped") {
    const visible = Number(data.visible);
    const total = Number(data.total);
    if (Number.isFinite(visible) && Number.isFinite(total) && visible >= 0 && total >= visible) {
      return uiText(
        `已按当前任务加载 ${visible}/${total} 个工具定义；其余工具仍可按需启用。`,
        `Loaded ${visible} of ${total} tool definitions for this task; the others remain available on demand.`,
      );
    }
    return uiText(
      "已优先加载当前任务所需工具；其余工具仍可按需启用。",
      "Loaded the tools needed for this task; the others remain available on demand.",
    );
  }
  return "";
}

function appSymbol(name) {
  const safeName = ["archive", "audio", "bolt", "close", "code", "file", "file-text", "folder", "image", "send", "terminal", "user", "video", "warning"].includes(name) ? name : "bolt";
  return `<span class="app-symbol app-symbol-${safeName}" aria-hidden="true"></span>`;
}

function taskSourceIcon(source = "web") {
  const remote = source === "feishu" || source === "telegram";
  return `<span class="app-symbol ${remote ? "app-symbol-remote" : "app-symbol-user"}" aria-hidden="true"></span>`;
}

function clearSubmittedComposer(prompt, attachments) {
  const input = $("#prompt");
  // Typing the next instruction while a request is in flight must not lose it.
  if (input.value.trim() === prompt) {
    input.value = "";
    delete input.dataset.systemDraft;
  }
  const submitted = new Set(attachments.map((attachment) => attachment.path));
  pendingAttachments = pendingAttachments.filter((attachment) => !submitted.has(attachment.path));
  resizePromptInput();
  renderAttachments();
}

async function sendRunningMessage(prompt) {
  if (!taskId || startRequestPending || uploadRequestPending || taskViewLoading || stopRequestPending) return false;
  if (currentTaskSnapshot?.id !== taskId
      || !["queued", "running", "waiting_approval"].includes(currentTaskSnapshot?.status)) return false;
  const expectedTaskId = taskId;
  const expectedGeneration = taskViewGeneration;
  const submittedScope = composerDraftScope;
  const submittedAttachments = [...pendingAttachments];
  startRequestPending = true;
  $("#send").disabled = true;
  try {
    const task = await api(`/api/tasks/${expectedTaskId}/messages`, {
      method: "POST",
      body: JSON.stringify({
        prompt,
        attachments: submittedAttachments.map((attachment) => attachment.path),
      }),
    });
    clearSubmittedDraft(submittedScope, prompt, submittedAttachments);
    if (!isCurrentTaskRequest(expectedTaskId, expectedGeneration)) return true;
    currentTaskSnapshot = task;
    renderArtifactInspector(task);
    renderSubagentPanel(task);
    showToast(
      uiText("已发送；Agent 会在当前模型调用或工具步骤结束后接收", "Sent; the Agent will receive it after the current model or tool step"),
      "info",
    );
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => poll(expectedTaskId, expectedGeneration), 50);
    return true;
  } catch (error) {
    if (!isCurrentTaskRequest(expectedTaskId, expectedGeneration)) return false;
    showToast(`${uiText("发送失败：", "Send failed: ")}${localizeKnownSystemMessage(error.message)}`, "error");
    return false;
  } finally {
    startRequestPending = false;
    $("#send").disabled = false;
    syncComposerAction();
  }
}

async function start({ voiceRequest = false, promptOverride = null } = {}) {
  const prompt = String(promptOverride ?? $("#prompt").value).trim();
  if (!prompt || startRequestPending || uploadRequestPending || taskViewLoading || stopRequestPending) return false;
  if (taskId) {
    return sendRunningMessage(prompt);
  }
  const modelApiConfigured = Boolean(
    latestStatus?.primary_key_configured
      || Number(latestStatus?.model_provider_count || 0) > 0
      || latestStatus?.local_models?.ready,
  );
  if (latestStatus && !modelApiConfigured) {
    showToast(
      uiText(
        "请先在“设置 → 模型与密钥”中配置至少一个模型 API 密钥。",
        "Configure at least one model API key in Settings → Models & keys first.",
      ),
      "error",
    );
    openWorkspacePanel("settings");
    activateSettingsSection("providers");
    return false;
  }
  startRequestPending = true;
  navigationIntent += 1;
  const viewGeneration = ++taskViewGeneration;
  const continuingFrom = continuationTaskId;
  syncProjectComposer();
  const snapshotBeforeStart = currentTaskSnapshot;
  const submittedScope = composerDraftScope;
  const submittedAttachments = [...pendingAttachments];
  $("#send").disabled = true;
  let taskCreationStarted = false;
  try {
    const projectPath = continuingFrom ? "" : newTaskProjectPath.trim();
    if (projectPath) {
      await api("/api/projects/validate", { method: "POST", body: JSON.stringify({ project_path: projectPath }) });
      if (viewGeneration !== taskViewGeneration) return false;
    }
    // Preflight may fail or outlive navigation. Keep the current view and draft
    // untouched until it succeeds; validation is not a queued/running task.
    taskCreationStarted = true;
    lastEventId = null;
    terminalStatusRendered = null;
    stopRequestPending = false;
    pollFailureCount = 0;
    timelineAutoFollow = true;
    dismissedArtifactInspectorTaskId = null;
    hideArtifactInspector();
    hideSubagentPanel({ keepToggle: false });
    clearTimelineNewProgress();
    updateTaskProgress(null, continuingFrom
      ? uiText("正在继续任务…", "Continuing task…")
      : uiText("正在创建任务…", "Creating task…"));
    $("#welcome").classList.add("hidden");
    $("#timeline").classList.remove("hidden");
    if (!continuingFrom) updateConversationTitle(null, prompt);
    const promptEvent = `<div class="event task-prompt user-authored"><div class="event-icon">${taskSourceIcon("web")}</div><div class="event-card"><strong>${continuingFrom ? (isEnglish() ? "Continue task" : "继续任务") : (isEnglish() ? "Your task" : "你的任务")}</strong><p>${escapeHtml(prompt)}</p>${sentAttachmentMarkup(pendingAttachments)}</div></div>`;
    if (continuingFrom) {
      $("#timeline").insertAdjacentHTML("beforeend", promptEvent);
    } else {
      $("#timeline").innerHTML = promptEvent;
    }
    updateTaskOverview({ status: "queued", events: [] });
    updateTaskProgress(null);
    restoreTimelineScrollState({ follow: true }, { forceFollow: true });
    const endpoint = continuingFrom
      ? `/api/tasks/${continuingFrom}/continue`
      : "/api/tasks";
    const task = await api(endpoint, {
      method: "POST",
      body: JSON.stringify({
        prompt,
        policy: "autonomous",
        agent_profile: "general",
        model_preference: $("#modelPreference").value,
        reasoning_effort: reasoningPreferenceValue(),
        attachments: submittedAttachments.map((attachment) => attachment.path),
        interface_language: isEnglish() ? "en" : "zh",
        voice_request: Boolean(voiceRequest),
        ...(!continuingFrom ? { project_path: projectPath } : {}),
      }),
    });
    clearSubmittedDraft(submittedScope, prompt, submittedAttachments);
    if (viewGeneration !== taskViewGeneration) {
      loadTaskHistory();
      return false;
    }
    currentTaskSnapshot = task;
    renderArtifactInspector(task);
    renderSubagentPanel(task);
    updateConversationTitle(task);
    window.dispatchEvent(new CustomEvent("elren:task-updated", { detail: task }));
    updateModelBadge(task);
    updateContextUsage(task);
    updateTaskOverview(task);
    taskId = task.id;
    continuationTaskId = null;
    composerDraftScope = task.id;
    syncTaskViewUrl(task.id);
    if (continuingFrom) rebuildTaskTimeline(task);
    $("#send").disabled = false;
    $("#prompt").placeholder = promptPlaceholder();
    loadTaskHistory();
    poll(task.id, viewGeneration);
    return true;
  } catch (error) {
    if (viewGeneration !== taskViewGeneration) return false;
    if (!taskCreationStarted) {
      showToast(`${uiText("项目目录不可用：", "Project folder unavailable: ")}${localizeKnownSystemMessage(error.message)}`, "error");
      return false;
    }
    if (continuingFrom && snapshotBeforeStart?.id === continuingFrom) {
      currentTaskSnapshot = snapshotBeforeStart;
      terminalStatusRendered = null;
      rebuildTaskTimeline(snapshotBeforeStart);
      renderArtifactInspector(snapshotBeforeStart);
      renderSubagentPanel(snapshotBeforeStart);
      updateConversationTitle(snapshotBeforeStart);
      updateTaskProgress(snapshotBeforeStart);
      updateTaskOverview(snapshotBeforeStart);
      $("#prompt").placeholder = taskComposerPlaceholder(snapshotBeforeStart);
    }
    renderEvent({ type: "error", data: { message: error.message } });
    $("#send").disabled = false;
    return false;
  } finally {
    startRequestPending = false;
    syncComposerAction();
  }
}

async function poll(expectedTaskId = taskId, expectedGeneration = taskViewGeneration) {
  if (!expectedTaskId || taskViewLoading || !isCurrentTaskRequest(expectedTaskId, expectedGeneration)) return;
  try {
    const snapshotAtRequest = currentTaskSnapshot?.id === expectedTaskId
      ? currentTaskSnapshot
      : null;
    const knownEvents = Array.isArray(snapshotAtRequest?.events) ? snapshotAtRequest.events : [];
    // Keep one stable event behind the live tail. Coalesced progress events can
    // replace the last id; an inclusive penultimate cursor still returns only a
    // tiny suffix while preserving that update.
    const eventCursor = knownEvents.length > 1 ? String(knownEvents.at(-2)?.id || "") : "";
    const pollPath = eventCursor
      ? `/api/tasks/${expectedTaskId}?after_event_id=${encodeURIComponent(eventCursor)}`
      : `/api/tasks/${expectedTaskId}`;
    const update = await api(pollPath);
    if (!isCurrentTaskRequest(expectedTaskId, expectedGeneration)) return;
    // A same-task POST may have installed a newer snapshot while this GET was
    // in flight. Ignore the stale response and immediately ask again instead
    // of truncating those new events during the suffix merge.
    if (snapshotAtRequest && currentTaskSnapshot !== snapshotAtRequest) {
      pollTimer = setTimeout(() => poll(expectedTaskId, expectedGeneration), 0);
      return;
    }
    const previousTaskSnapshot = currentTaskSnapshot;
    let task = mergeTaskEventUpdate(previousTaskSnapshot, update);
    if (!task) {
      task = await api(`/api/tasks/${expectedTaskId}`);
      if (!isCurrentTaskRequest(expectedTaskId, expectedGeneration)) return;
      if (currentTaskSnapshot !== previousTaskSnapshot) {
        pollTimer = setTimeout(() => poll(expectedTaskId, expectedGeneration), 0);
        return;
      }
    }
    task = reconcileTaskActionState(task);
    const previousStatus = previousTaskSnapshot?.status;
    const recoveredFromPollFailure = pollFailureCount > 0;
    currentTaskSnapshot = task;
    renderArtifactInspector(task);
    renderSubagentPanel(task);
    updateConversationTitle(task);
    window.dispatchEvent(new CustomEvent("elren:task-updated", { detail: task }));
    updateModelBadge(task);
    updateContextUsage(task);
    pollFailureCount = 0;
    if (recoveredFromPollFailure) {
      showToast(uiText("任务连接已恢复", "Task connection restored"), "info");
    }
    updateTaskProgress(task);
    $("#prompt").placeholder = taskComposerPlaceholder(task);
    syncComposerAction();
    const becameTerminal = ["completed", "failed", "cancelled"].includes(task.status)
      && !["completed", "failed", "cancelled"].includes(previousStatus);
    const timelineScrollState = captureTimelineScrollState();
    const delta = eventDelta(task.events || [], lastEventId);
    const newProgressCount = countTimelineNewProgress(
      task.events || [],
      previousTaskSnapshot?.events || [],
      delta,
      becameTerminal,
    );
    if (delta.requiresRebuild || becameTerminal) {
      rebuildTaskTimeline(task);
    } else {
      delta.events.forEach((event) => renderEvent(event, task.status, isFinalAssistantEvent(event, task)));
      lastEventId = delta.cursor;
    }
    // Update from the complete snapshot, independently of eventDelta.  This
    // keeps buffered/non-streaming providers visibly alive and allows a
    // coalesced model_stream to refresh without rebuilding the conversation.
    updateTaskOverview(task);
    restoreTimelineScrollState(timelineScrollState);
    if (timelineScrollState.follow) {
      clearTimelineNewProgress();
    } else if (newProgressCount > 0) {
      timelineUnseenProgressCount += newProgressCount;
      updateTimelineNewProgress();
    }
    syncTaskHumanAction(task);
    if (["completed", "failed", "cancelled"].includes(task.status)) {
      $("#send").disabled = false;
      stopRequestPending = false;
      pendingHumanAction = null;
      $("#humanAction").classList.add("hidden");
      renderTerminalState(task);
      continuationTaskId = task.id;
      taskId = null;
      syncComposerAction();
      loadTaskHistory();
      return;
    }
  } catch (error) {
    if (!isCurrentTaskRequest(expectedTaskId, expectedGeneration)) return;
    pollFailureCount += 1;
    if (pollFailureCount === 1) {
      showToast(uiText("连接暂时中断，正在自动重试", "Connection interrupted; retrying automatically"), "error");
    }
  }
  const retryDelay = pollFailureCount
    ? Math.min(5000, 650 * (2 ** Math.min(pollFailureCount, 3)))
    : (document.hidden ? 2500 : ($("#voiceDialog")?.open ? 200 : 650));
  if (isCurrentTaskRequest(expectedTaskId, expectedGeneration)) {
    pollTimer = setTimeout(() => poll(expectedTaskId, expectedGeneration), retryDelay);
  }
}

function isCurrentHumanActionRequest(expectedTaskId, expectedGeneration, requestId) {
  return isCurrentTaskRequest(expectedTaskId, expectedGeneration)
    && pendingHumanAction?.id === requestId;
}

function taskActionKey(id, kind, requestId) {
  return JSON.stringify([id, kind, requestId]);
}

function updateTaskActionState(id, kind, requestId, changes) {
  const key = taskActionKey(id, kind, requestId);
  taskActionStates.set(key, { ...taskActionStates.get(key), ...changes });
  // Bound settled acknowledgements; never evict an in-flight request.
  if (taskActionStates.size > 256) {
    for (const [oldKey, state] of taskActionStates) {
      if (oldKey !== key && !state.pending) taskActionStates.delete(oldKey);
      if (taskActionStates.size <= 256) break;
    }
  }
  if (currentTaskSnapshot?.id === id) {
    currentTaskSnapshot = reconcileTaskActionState({ ...currentTaskSnapshot });
  }
}

function reconcileTaskActionState(task) {
  if (!task?.id) return task;
  const stateFor = (kind, request) => taskActionStates.get(taskActionKey(task.id, kind, request.id));
  return {
    ...task,
    pending_approvals: (task.pending_approvals || []).filter((request) => !stateFor("approval", request)?.resolved),
    pending_human_actions: (task.pending_human_actions || [])
      .filter((request) => !stateFor("human", request)?.resolved)
      .map((request) => stateFor("human", request)?.takenOver ? { ...request, taken_over: true } : request),
  };
}

function syncTaskApproval(task = null) {
  const panel = $("#taskApproval");
  if (!panel) return;
  const approval = !["completed", "failed", "cancelled"].includes(task?.status) && task?.pending_approvals?.[0];
  panel.classList.toggle("hidden", !approval);
  if (!approval) return;
  const busy = stopRequestPending || Boolean(taskActionStates.get(taskActionKey(task.id, "approval", approval.id))?.pending);
  $("#taskApprovalTitle").textContent = uiText("此操作需要你确认", "Your confirmation is required");
  $("#taskApprovalSummary").textContent = approval.summary || approval.tool;
  $("#taskApprovalArguments").textContent = JSON.stringify({ tool: approval.tool, arguments: approval.arguments || {} }, null, 2);
  $("#taskApprovalDetailsLabel").textContent = uiText("查看操作参数", "Review action parameters");
  const confirm = $("#confirmTaskApproval"), reject = $("#rejectTaskApproval");
  confirm.textContent = busy ? uiText("正在提交…", "Submitting…") : uiText("仅允许这一次", "Allow this action once");
  reject.textContent = uiText("拒绝此操作", "Reject this action");
  confirm.disabled = busy;
  reject.disabled = busy;
  confirm.onclick = () => resolveTaskApproval(task.id, approval.id, true);
  reject.onclick = () => resolveTaskApproval(task.id, approval.id, false);
}

async function resolveTaskApproval(expectedTaskId, requestId, approved) {
  const expectedGeneration = taskViewGeneration;
  if (!isCurrentTaskRequest(expectedTaskId, expectedGeneration) || taskViewLoading || stopRequestPending
      || !currentTaskSnapshot?.pending_approvals?.some((request) => request.id === requestId)
      || ["completed", "failed", "cancelled"].includes(currentTaskSnapshot?.status)) return false;
  const key = taskActionKey(expectedTaskId, "approval", requestId);
  if (taskActionStates.get(key)?.pending || taskActionStates.get(key)?.resolved) return false;
  updateTaskActionState(expectedTaskId, "approval", requestId, { pending: true });
  syncTaskApproval(currentTaskSnapshot);
  try {
    await api(`/api/approvals/${encodeURIComponent(requestId)}`, { method: "POST", body: JSON.stringify({ approved: Boolean(approved) }) });
    updateTaskActionState(expectedTaskId, "approval", requestId, { resolved: true });
    if (!isCurrentTaskRequest(expectedTaskId, expectedGeneration)) return true;
    showToast(approved ? uiText("已允许此操作", "This action was allowed") : uiText("已拒绝此操作", "This action was rejected"), "info");
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => poll(expectedTaskId, expectedGeneration), 50);
    return true;
  } catch (error) {
    if (isCurrentTaskRequest(expectedTaskId, expectedGeneration)) {
      showToast(`${uiText("提交确认失败：", "Confirmation failed: ")}${localizeKnownSystemMessage(error.message)}`, "error");
    }
    return false;
  } finally {
    updateTaskActionState(expectedTaskId, "approval", requestId, { pending: false });
    if (isCurrentTaskRequest(expectedTaskId, expectedGeneration)) syncTaskApproval(currentTaskSnapshot);
  }
}

function syncTaskHumanAction(task = null) {
  task = reconcileTaskActionState(task);
  syncTaskApproval(task);
  const terminal = ["completed", "failed", "cancelled"].includes(task?.status);
  const humanAction = !terminal && task?.pending_human_actions?.[0];
  if (!humanAction) {
    pendingHumanAction = null;
    surfacedHumanActionId = null;
    $("#humanAction").classList.add("hidden");
    if ($("#humanProblemDialog")?.open) $("#humanProblemDialog").close();
    document.title = "Elren";
    return;
  }
  pendingHumanAction = humanAction;
  $("#humanActionSummary").textContent = humanAction.summary;
  $("#humanActionInstructions").textContent = humanAction.instructions;
  $("#humanActionTargetApp").textContent = humanAction.target_app || humanAction.target_window || uiText("未指定应用", "App not specified");
  $("#humanActionTargetPage").textContent = humanAction.target_page || humanAction.target_window || uiText("请根据窗口标题确认", "Confirm using the window title");
  $("#humanActionTargetTask").textContent = humanAction.instructions;
  const busy = stopRequestPending || Boolean(taskActionStates.get(taskActionKey(task.id, "human", humanAction.id))?.pending);
  $("#takeOverHumanAction").disabled = busy || humanAction.taken_over;
  $("#takeOverHumanAction").textContent = busy ? uiText("正在提交…", "Submitting…") : humanAction.taken_over ? uiText("已接管", "Taken over") : uiText("接管操作", "Take over");
  $("#completeHumanAction").disabled = busy || !humanAction.taken_over;
  $("#cancelHumanAction").disabled = busy || !humanAction.taken_over;
  $("#humanAction").classList.remove("hidden");
  if (surfacedHumanActionId !== humanAction.id) {
    surfacedHumanActionId = humanAction.id;
    document.title = uiText("需要接管 · Elren", "Action required · Elren");
    window.focus();
  }
}

async function takeOverHumanAction() {
  if (!pendingHumanAction || pendingHumanAction.taken_over || !taskId || taskViewLoading || stopRequestPending) return;
  const expectedTaskId = taskId;
  const expectedGeneration = taskViewGeneration;
  const requestId = pendingHumanAction.id;
  if (taskActionStates.get(taskActionKey(expectedTaskId, "human", requestId))?.pending) return;
  updateTaskActionState(expectedTaskId, "human", requestId, { pending: true });
  const button = $("#takeOverHumanAction");
  button.disabled = true;
  button.textContent = uiText("正在交接…", "Handing over…");
  try {
    const result = await api(`/api/human-actions/${requestId}/takeover`, {
      method: "POST",
    });
    updateTaskActionState(expectedTaskId, "human", requestId, { takenOver: true });
    if (!isCurrentHumanActionRequest(expectedTaskId, expectedGeneration, requestId)) return;
    pendingHumanAction = result.request;
    button.textContent = uiText("已接管", "Taken over");
    $("#completeHumanAction").disabled = false;
    $("#cancelHumanAction").disabled = false;
    showToast(takeoverFocusMessage(result.focus), "info");
  } catch (error) {
    if (!isCurrentHumanActionRequest(expectedTaskId, expectedGeneration, requestId)) return;
    button.disabled = false;
    button.textContent = uiText("接管操作", "Take over");
    showToast(`${uiText("接管失败：", "Takeover failed: ")}${localizeKnownSystemMessage(error.message)}`, "error");
  } finally {
    updateTaskActionState(expectedTaskId, "human", requestId, { pending: false });
    if (isCurrentTaskRequest(expectedTaskId, expectedGeneration)) syncTaskHumanAction(currentTaskSnapshot);
  }
}

async function resolveHumanAction(completed, issueDescription = "", skippedDescription = false) {
  if (!pendingHumanAction?.taken_over || !taskId || taskViewLoading || stopRequestPending) return;
  const expectedTaskId = taskId;
  const expectedGeneration = taskViewGeneration;
  const requestId = pendingHumanAction.id;
  if (taskActionStates.get(taskActionKey(expectedTaskId, "human", requestId))?.pending) return;
  updateTaskActionState(expectedTaskId, "human", requestId, { pending: true });
  const completeButton = $("#completeHumanAction");
  const cancelButton = $("#cancelHumanAction");
  completeButton.disabled = true;
  cancelButton.disabled = true;
  try {
    await api(`/api/human-actions/${requestId}/complete`, {
      method: "POST",
      body: JSON.stringify({
        completed,
        issue_description: issueDescription,
        skipped_description: skippedDescription,
      }),
    });
    const stillCurrent = isCurrentHumanActionRequest(expectedTaskId, expectedGeneration, requestId);
    updateTaskActionState(expectedTaskId, "human", requestId, { resolved: true });
    if (!stillCurrent) return;
    pendingHumanAction = null;
    $("#humanAction").classList.add("hidden");
    if ($("#humanProblemDialog").open) $("#humanProblemDialog").close();
    showToast(completed
      ? uiText("人工操作已完成，Agent 正在继续", "Manual action completed; the Agent is continuing")
      : uiText("问题已交给 Agent，正在调整方案继续", "The issue was sent to the Agent; it is adjusting the plan and continuing"), "info");
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => poll(expectedTaskId, expectedGeneration), 50);
  } catch (error) {
    if (!isCurrentHumanActionRequest(expectedTaskId, expectedGeneration, requestId)) return;
    completeButton.disabled = false;
    cancelButton.disabled = false;
    showToast(`${uiText("无法继续：", "Unable to continue: ")}${localizeKnownSystemMessage(error.message)}`, "error");
  } finally {
    updateTaskActionState(expectedTaskId, "human", requestId, { pending: false });
    if (isCurrentTaskRequest(expectedTaskId, expectedGeneration)) {
      syncTaskHumanAction(currentTaskSnapshot);
      $("#prompt").placeholder = taskComposerPlaceholder(currentTaskSnapshot);
      syncComposerAction();
    }
  }
}

function openHumanProblemDialog() {
  if (!pendingHumanAction?.taken_over || stopRequestPending) return;
  if (taskActionStates.get(taskActionKey(taskId, "human", pendingHumanAction.id))?.pending) return;
  $("#humanProblemDescription").value = "";
  $("#humanProblemFormStep").classList.remove("hidden");
  $("#humanProblemConfirmStep").classList.add("hidden");
  const dialog = $("#humanProblemDialog");
  if (!dialog.open) dialog.showModal();
  $("#humanProblemDescription").focus();
}

function skipWarningDisabled() {
  try { return window.localStorage.getItem(HUMAN_PROBLEM_SKIP_WARNING_KEY) === "true"; }
  catch { return false; }
}

function submitProblemDescription() {
  const description = $("#humanProblemDescription").value.trim();
  if (!description) {
    showToast(uiText("请描述问题，或选择“不填写”", "Describe the problem, or choose “Skip”"), "info");
    return;
  }
  resolveHumanAction(false, description, false);
}

function requestSkipProblemDescription() {
  if (skipWarningDisabled()) {
    resolveHumanAction(false, "", true);
    return;
  }
  $("#humanProblemFormStep").classList.add("hidden");
  $("#humanProblemConfirmStep").classList.remove("hidden");
}

$("#send").onclick = () => {
  if ($("#send").dataset.action === "stop" && !$("#prompt").value.trim()) {
    return requestStop();
  }
  return start();
};
$("#uploadFile").onclick = () => $("#fileInput").click();
$("#fileInput").addEventListener("change", (event) => uploadFiles(event.target.files));
$("#projectPath")?.addEventListener("input", (event) => {
  if (event.currentTarget.readOnly) return;
  newTaskProjectPath = event.currentTarget.value;
  event.currentTarget.title = newTaskProjectPath;
});
$("#modelPreference").addEventListener("change", () => {
  pendingLanguageSwitchModel = "";
  delete $("#modelPreference").dataset.languageResumeModel;
  delete $("#reasoningPreference").dataset.languageResumeReasoning;
  refreshModelPreferenceUI();
  syncReasoningAvailability();
  if (isBlankNewTaskComposer()) rememberNewTaskComposerPreferences();
  if (currentTaskSnapshot) updateModelBadge(currentTaskSnapshot);
});
$("#reasoningPreference").addEventListener("input", () => {
  delete $("#reasoningPreference").dataset.languageResumeReasoning;
  // An explicit user choice is initialization too. A delayed first status or
  // Settings response may refresh capabilities, but must not apply defaults.
  reasoningDefaultLoaded = true;
  setReasoningPreference(reasoningPreferenceValue());
  if (isBlankNewTaskComposer()) rememberNewTaskComposerPreferences();
});
if (typeof ResizeObserver !== "undefined") {
  const reasoningSliderWrap = document.querySelector(".reasoning-slider-wrap");
  if (reasoningSliderWrap) {
    const reasoningSliderResizeObserver = new ResizeObserver(() => updateReasoningVisualState());
    reasoningSliderResizeObserver.observe(reasoningSliderWrap);
  }
}
$("#prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    start();
  }
});
$("#prompt").addEventListener("input", (event) => {
  // Once the user edits a generated recovery draft it becomes their draft;
  // New task must preserve it just like any other user-authored text.
  delete event.currentTarget.dataset.systemDraft;
  resizePromptInput();
  syncComposerAction();
});
window.addEventListener("resize", resizePromptInput);
$("#newTask").onclick = () => {
  // Switching to a blank composer only changes the current view. The previous
  // task continues in the background and stays reachable from All tasks.
  startBlankTask();
};
$("#mobileNav").onclick = openNavigation;
$("#navOverlay").onclick = () => closeNavigation({ restoreFocus: true });
async function requestStop() {
  if (!taskId || stopRequestPending || taskViewLoading) return false;
  const expectedTaskId = taskId;
  const expectedGeneration = taskViewGeneration;
  const button = $("#send");
  stopRequestPending = true;
  button.disabled = true;
  button.setAttribute("aria-label", uiText("正在停止…", "Stopping…"));
  button.title = uiText("正在停止…", "Stopping…");
  syncComposerAction();
  syncTaskHumanAction(currentTaskSnapshot);
  showToast(uiText("正在停止任务…", "Stopping task…"), "info");
  try {
    const result = await api(`/api/tasks/${expectedTaskId}/cancel`, { method: "POST" });
    if (!isCurrentTaskRequest(expectedTaskId, expectedGeneration)) return result.ok;
    if (!result.ok) {
      showToast(result.message || uiText("任务已经结束", "The task has already ended"), "info");
      stopRequestPending = false;
      syncComposerAction();
      syncTaskHumanAction(currentTaskSnapshot);
    }
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => poll(expectedTaskId, expectedGeneration), 50);
    return result.ok;
  } catch (error) {
    if (!isCurrentTaskRequest(expectedTaskId, expectedGeneration)) return false;
    stopRequestPending = false;
    button.disabled = false;
    syncComposerAction();
    syncTaskHumanAction(currentTaskSnapshot);
    showToast(`${uiText("停止失败：", "Failed to stop: ")}${error.message}`, "error");
    return false;
  }
}

$("#takeOverHumanAction").onclick = takeOverHumanAction;
$("#stopWaitingTask").onclick = requestStop;
$("#completeHumanAction").onclick = () => resolveHumanAction(true);
$("#cancelHumanAction").onclick = openHumanProblemDialog;
$("#submitHumanProblem").onclick = submitProblemDescription;
$("#skipHumanProblem").onclick = requestSkipProblemDescription;
$("#returnHumanProblem").onclick = () => {
  $("#humanProblemConfirmStep").classList.add("hidden");
  $("#humanProblemFormStep").classList.remove("hidden");
  $("#humanProblemDescription").focus();
};
$("#confirmSkipHumanProblem").onclick = () => resolveHumanAction(false, "", true);
$("#neverAskHumanProblem").onclick = () => {
  try { window.localStorage.setItem(HUMAN_PROBLEM_SKIP_WARNING_KEY, "true"); } catch {}
  resolveHumanAction(false, "", true);
};
function capabilityGroupTitle(group = {}, index = 0) {
  const explicit = String(group?.label || group?.id || "").trim();
  if (explicit) return explicit;
  return isEnglish() ? `OpenClaw tool group ${index + 1}` : `OpenClaw 工具组 ${index + 1}`;
}

let capabilityRequestGeneration = 0;
$("#openCapabilities").onclick = async () => {
  const generation = ++capabilityRequestGeneration;
  setPrimaryNavigation("navPlugins");
  const dialog = $("#capabilityDialog");
  const content = $("#capabilityContent");
  dialog.showModal();
  const isCurrentRequest = () => dialog.open && generation === capabilityRequestGeneration;
  content.innerHTML = `<div class="capability-loading">${uiText("正在读取 Elren 本机能力…", "Loading Elren local capabilities…")}</div>`;
  const renderCatalog = (groups, summary, notice = "", vision = latestStatus?.vision || {}) => {
    if (!isCurrentRequest()) return;
    const count = groups.reduce((total, group) => total + (group.tools || []).length, 0);
    const displaySummary = String(summary || uiText("个可用工具", "available tools"));
    const displayNotice = isEnglish() ? "Elren, MCP, desktop, and browser tools remain available even when OpenClaw is offline." : notice;
    content.innerHTML = `
      <div class="capability-summary"><strong>${count}</strong><span>${escapeHtml(displaySummary)}</span></div>
      ${displayNotice ? `<div class="capability-runtime-notice">${escapeHtml(displayNotice)}</div>` : ""}
      ${visionCapabilityMarkup(vision)}
      <div class="capability-groups">${groups.map((group, index) => `
        <section class="capability-group">
          <header><h3>${escapeHtml(isEnglish() && group.id === "deepdesk" ? "Elren local tools" : capabilityGroupTitle(group, index))}</h3><span aria-label="${uiText(`${(group.tools || []).length} 项能力`, `${(group.tools || []).length} capabilities`)}">${(group.tools || []).length}</span></header>
          <div>${(group.tools || []).map((tool) => {
            const identifier = tool.name || tool.id || tool.label || "tool";
            const label = isEnglish() ? localizedToolName(identifier) : (tool.label || identifier);
            const description = isEnglish() ? label : (tool.description || label);
            return `<span title="${escapeHtml(description)}" aria-label="${escapeHtml(`${label}${uiText("：", ": ")}${description}`)}">${escapeHtml(label)}</span>`;
          }).join("")}</div>
        </section>
      `).join("")}</div>
      <p class="capability-note">${isEnglish() ? "Availability depends on runtime state, plugin configuration, external credentials, and Elren approval policy." : "工具是否可执行仍由运行时状态、插件配置、外部账号凭据和 Elren 审批策略共同决定。"}</p>`;
  };
  try {
    const status = await api("/api/status");
    if (!isCurrentRequest()) return;
    latestStatus = status;
    const localGroups = groupLocalCapabilities((status.tools || []).map((tool) => ({
        id: tool.name,
        name: tool.name,
        label: localizedToolName(tool.name),
        description: tool.description,
      })));
    renderCatalog(
      localGroups,
      status.openclaw?.gateway_ready
        ? uiText("个可用本机工具 · OpenClaw 已连接，正在载入扩展工具", "available local tools · OpenClaw connected; loading extensions")
        : uiText("个可用本机工具 · 正在连接 OpenClaw Gateway", "available local tools · connecting to OpenClaw Gateway"),
      "即使 OpenClaw 未启动，下列 Elren、MCP、桌面与浏览器工具仍可正常使用。",
    );
    try {
      const [catalog, plugins] = await Promise.all([
        api("/api/openclaw/catalog"),
        api("/api/openclaw/plugins"),
      ]);
      if (!isCurrentRequest()) return;
      if (!Array.isArray(catalog?.groups) || !Number.isInteger(plugins?.loaded)
          || !Number.isInteger(plugins?.total) || plugins.loaded < 0
          || plugins.total < plugins.loaded) {
        throw new Error(uiText("扩展目录数据不完整", "Incomplete extension catalog data"));
      }
      const openClawGroups = catalog.groups;
      renderCatalog(
        [...localGroups, ...openClawGroups],
        uiText(
          `个实时工具 · ${localGroups.length + openClawGroups.length} 个类别 · OpenClaw ${plugins.loaded}/${plugins.total} 个插件已加载`,
          `live tools · ${localGroups.length + openClawGroups.length} categories · OpenClaw ${plugins.loaded}/${plugins.total} plugins loaded`,
        ),
      );
      await loadStatus();
    } catch (error) {
      renderCatalog(
        localGroups,
        uiText("个可用本机工具 · OpenClaw 当前未连接", "available local tools · OpenClaw is offline"),
        uiText(
          `OpenClaw 扩展目录暂不可用：${localizeKnownSystemMessage(error.message)}。需要 OpenClaw 工具时可重新探测，其他能力不受影响。`,
          `The OpenClaw extension catalog is unavailable: ${localizeKnownSystemMessage(error.message)}. Probe the runtime again when OpenClaw tools are needed; other capabilities are unaffected.`,
        ),
      );
    }
  } catch (error) {
    if (!isCurrentRequest()) return;
    content.innerHTML = `<div class="capability-error">${uiText("无法读取 Elren 本机能力：", "Unable to load Elren local capabilities: ")}${escapeHtml(localizeKnownSystemMessage(error.message))}</div>`;
  }
};
function closeCapabilityDialog() {
  capabilityRequestGeneration += 1;
  const dialog = $("#capabilityDialog");
  if (dialog?.open) dialog.close();
  setPrimaryNavigation("navChats");
  focusMainContentTarget();
}

$("#closeCapabilities").onclick = closeCapabilityDialog;
$("#capabilityDialog")?.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeCapabilityDialog();
});
$("#capabilityDialog")?.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  event.preventDefault();
  closeCapabilityDialog();
});

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function setSettingsFormDirty(dirty, { announce = true } = {}) {
  settingsFormDirty = Boolean(dirty);
  const state = $("#settingsSaveState");
  const button = $("#saveSettings");
  state?.classList.toggle("dirty", settingsFormDirty);
  if (!settingsFormDirty || !announce) return;
  if (state) state.textContent = uiText("有未保存的更改", "Unsaved changes");
  if (button && !button.disabled) button.textContent = uiText("保存更改", "Save changes");
}

function showSettingsCleanHint() {
  const state = $("#settingsSaveState");
  const button = $("#saveSettings");
  if (state) {
    state.className = "settings-save-state";
    state.textContent = uiText(
      "密钥只显示配置状态，不返回明文",
      "Keys show configuration status only; plaintext is never returned",
    );
  }
  if (button && !button.disabled) button.textContent = uiText("保存设置", "Save settings");
}

function showRecoveredDiscussionTeamDraftHint() {
  const state = $("#settingsSaveState");
  const button = $("#saveSettings");
  if (state) {
    state.className = "settings-save-state";
    state.textContent = uiText(
      "已恢复上次未保存的讨论团草稿；关闭设置仍会保留，点击保存后才会应用",
      "Recovered the last unsaved Agent-team draft; closing Settings keeps it, and Save applies it",
    );
  }
  if (button && !button.disabled) button.textContent = uiText("保存设置", "Save settings");
}

function settingsFormSignature({ discussionTeam = null } = {}) {
  const form = $("#settingsForm");
  if (!form) return "";
  const fields = [...form.querySelectorAll("input, select, textarea")]
    .filter((control) => !control.closest("#discussionTeamRows"))
    .map((control, index) => ({
      key: control.id || `${control.tagName}:${control.type || ""}:${control.className || ""}:${index}`,
      value: control instanceof HTMLInputElement && ["checkbox", "radio"].includes(control.type)
        ? control.checked
        : control.value,
    }));
  const team = Array.isArray(discussionTeam)
    ? discussionTeam
    : collectDiscussionTeamRows();
  return JSON.stringify({
    fields,
    discussion_team: canonicalDiscussionTeam(team),
  });
}

function rememberSettingsFormBaseline(options = {}) {
  settingsFormBaseline = settingsFormSignature(options);
}

function settingsPreferenceFields() {
  return [
    ["model", "#settingModel", "string"], ["reasoning_effort", "#settingReasoningEffort", "string"],
    ["request_timeout", "#settingTimeout", "number"],
    ["cross_conversation_context", "#settingCrossConversationContext", "boolean"],
    ["custom_system_prompt_suffix", "#settingCustomSystemPromptSuffix", "string"],
    ["voice_language", "#settingVoiceLanguage", "string"], ["voice_name", "#settingVoiceName", "string"],
    ["voice_rate", "#settingVoiceRate", "number"], ["voice_auto_speak", "#settingVoiceAutoSpeak", "boolean"],
    ["voice_hands_free", "#settingVoiceHandsFree", "boolean"], ["voice_auto_continue", "#settingVoiceAutoContinue", "boolean"],
  ];
}

function settingsPreferenceValues() {
  const values = {};
  settingsPreferenceFields().forEach(([field, selector, kind]) => {
    const control = $(selector);
    values[field] = kind === "boolean" ? control.checked : kind === "number" ? Number(control.value)
      : kind === "watchlist" ? control.value.trim() || "ALL" : control.value;
  });
  values.max_output_tokens = $("#settingAutoOutputTokens").checked ? null : Number($("#settingMaxOutputTokens").value);
  values.discussion_team = collectDiscussionTeamRows();
  values.model_providers = collectModelProviderRows();
  return values;
}

function changedSettingsPreferences(values, baseline) {
  return Object.fromEntries(Object.entries(values).filter(([field, value]) =>
    JSON.stringify(value) !== JSON.stringify(baseline?.[field])));
}

function applySettingsPreferenceDraft(values) {
  if (Object.hasOwn(values, "voice_language")) $("#settingVoiceLanguage").value = values.voice_language;
  if (Object.hasOwn(values, "voice_language") || Object.hasOwn(values, "voice_name")) {
    populateSettingVoiceNames(values.voice_name ?? $("#settingVoiceName").value);
  }
  settingsPreferenceFields().forEach(([field, selector, kind]) => {
    if (!Object.hasOwn(values, field)) return;
    if (kind === "boolean") $(selector).checked = values[field];
    else if (field !== "reasoning_effort") $(selector).value = String(values[field]);
  });
  settingModelDirty = Object.hasOwn(values, "model");
  settingReasoningDirty = Object.hasOwn(values, "reasoning_effort") || settingModelDirty;
  syncDefaultReasoningOptions($("#settingModel").value, values.reasoning_effort || $("#settingReasoningEffort").value);
  if (Object.hasOwn(values, "max_output_tokens")) {
    $("#settingAutoOutputTokens").checked = values.max_output_tokens == null;
    $("#settingMaxOutputTokens").disabled = values.max_output_tokens == null;
    if (values.max_output_tokens != null) $("#settingMaxOutputTokens").value = String(values.max_output_tokens);
  }
  if (Object.hasOwn(values, "discussion_team")) renderDiscussionTeamRows(values.discussion_team, { preserveEmpty: true });
  if (Object.hasOwn(values, "model_providers")) renderModelProviderRows(values.model_providers);
  enhanceAllSettingsSelects();
  updateOutputTokenHelp();
}

function hydrateSettingsPreferences(settings) {
  settingModelDirty = false;
  settingReasoningDirty = false;
  syncModelSelectors(settings);
  renderModelProviderRows(settings.model_providers || []);
  clearProviderCredentialDrafts();
  $("#settingVoiceLanguage").value = settings.voice_language || "auto";
  populateSettingVoiceNames(settings.voice_name || "");
  $("#settingVoiceRate").value = String(settings.voice_rate ?? 1);
  $("#settingVoiceAutoSpeak").checked = settings.voice_auto_speak !== false;
  $("#settingVoiceHandsFree").checked = settings.voice_hands_free !== false;
  $("#settingVoiceAutoContinue").checked = settings.voice_auto_continue !== false;
  setProviderKeyPlaceholders(settings);
  const automaticOutputTokens = settings.max_output_tokens == null;
  $("#settingAutoOutputTokens").checked = automaticOutputTokens;
  $("#settingMaxOutputTokens").disabled = automaticOutputTokens;
  $("#settingMaxOutputTokens").value = settings.max_output_tokens ?? 384000;
  updateOutputTokenHelp();
  $("#settingTimeout").value = settings.request_timeout;
  $("#settingCrossConversationContext").checked = settings.cross_conversation_context !== false;
  $("#settingCustomSystemPromptSuffix").value = settings.custom_system_prompt_suffix || "";
  renderDiscussionTeamRows(settings.discussion_team || [], { preserveEmpty: true });
  enhanceAllSettingsSelects();
  settingsPreferenceBaseline = settingsPreferenceValues();
  rememberSettingsFormBaseline();
}

function reconcileSettingsSave(updated, submittedValues, providerKeys, requestGeneration) {
  const sameVisit = requestGeneration === settingsRequestGeneration;
  const visible = $("#workspaceDialog")?.open && $("#panel-settings")?.classList.contains("active");
  if (!sameVisit && (!visible || settingsFormHydrating || !settingsPreferenceBaseline)) return false;
  const current = settingsPreferenceValues();
  const keep = changedSettingsPreferences(current, sameVisit ? submittedValues : settingsPreferenceBaseline);
  const credentialDrafts = providerCredentialFields().map(([selector, field]) => [selector,
    sameVisit && $(selector).value.trim() === (providerKeys[field] || "") ? "" : $(selector).value]);
  if (sameVisit && keep.model_providers) {
    keep.model_providers = keep.model_providers.map((entry) => {
      const submitted = submittedValues.model_providers.find((item) => item.id && item.id === entry.id);
      return submitted && submitted.provider === entry.provider && submitted.model === entry.model
        && submitted.base_url === entry.base_url && submitted.source === entry.source
        && submitted.api_key === entry.api_key ? { ...entry, api_key: "" } : entry;
    });
  }
  hydrateSettingsPreferences(updated);
  applySettingsPreferenceDraft(keep);
  credentialDrafts.forEach(([selector, value]) => { $(selector).value = value; });
  setSettingsFormDirty(settingsFormSignature() !== settingsFormBaseline, { announce: false });
  discussionTeamDirty = Object.hasOwn(keep, "discussion_team");
  recoveredDiscussionTeamDraftPending = false;
  if (discussionTeamDirty) persistDiscussionTeamDraft();
  else clearDiscussionTeamDraft();
  return true;
}

function settingsActivationWarningText(updated, dirty = false) {
  if (!Array.isArray(updated.activation_warnings) || !updated.activation_warnings.length) return "";
  const names = {
    telegram: "Telegram", feishu: uiText("飞书", "Feishu"), openclaw: "OpenClaw",
    vision: uiText("图像识别", "Vision"),
  };
  const components = [...new Set(updated.activation_warnings.map((warning) =>
    Object.hasOwn(names, warning?.component) ? names[warning.component] : uiText("部分服务", "Some services")))].join(", ");
  return uiText(
    `设置已保存，但以下服务未能完成连接或激活：${components}。请检查服务连接状态。`,
    `Settings saved, but connection or activation did not complete for: ${components}. Check the service connection status.`,
  ) + (dirty ? uiText(" 新修改仍未保存。", " Newer changes are still unsaved.") : "");
}

function setSettingsFormHydrating(hydrating) {
  settingsFormHydrating = Boolean(hydrating);
  const form = $("#settingsForm");
  if (!form) return;
  form.toggleAttribute("inert", settingsFormHydrating);
  form.setAttribute("aria-busy", settingsFormHydrating ? "true" : "false");
}

setSettingsFormHydrating(true);

function markSettingsFormDirty() {
  if (settingsFormHydrating) return;
  const dialog = $("#workspaceDialog");
  const settingsActive = $("#panel-settings")?.classList.contains("active");
  if (!dialog?.open || !settingsActive) return;
  const dirty = settingsFormBaseline == null
    ? true
    : settingsFormSignature() !== settingsFormBaseline;
  setSettingsFormDirty(dirty, { announce: dirty });
  if (!dirty) {
    if (recoveredDiscussionTeamDraftPending) {
      showRecoveredDiscussionTeamDraftHint();
    } else {
      clearDiscussionTeamDraft();
      showSettingsCleanHint();
    }
  }
}

function providerCredentialFields() {
  // One write-only field inventory for loading, explicit discard and saving.
  return [
    ["#settingDeepSeekKey", "deepseek_api_key"],
    ["#settingDeepSeekBackupKey", "deepseek_backup_api_key"],
    ["#settingGeminiKey", "gemini_api_key"],
    ["#settingPollinationsKey", "pollinations_api_key"],
    ["#settingHuggingFaceToken", "huggingface_token"],
    ["#settingAicodemirrorKey", "aicodemirror_api_key"],
    ["#settingAicodemirrorFableKey", "aicodemirror_fable_api_key"],
    ["#settingFeishuAppId", "feishu_app_id"],
    ["#settingFeishuAppSecret", "feishu_app_secret"],
    ["#settingFeishuOpenId", "feishu_open_id"],
    ["#settingTelegramBotToken", "telegram_bot_token"],
    ["#settingTelegramChatId", "telegram_chat_id"],
    ["#settingGithubToken", "github_token"],
    ["#settingGooglePlacesKey", "google_places_api_key"],
    ["#settingTrelloApiKey", "trello_api_key"],
    ["#settingTrelloToken", "trello_token"],
    ["#settingElevenLabsKey", "elevenlabs_api_key"],
    ["#settingNotionToken", "notion_token"],
    ["#settingSpotifyClientId", "spotify_client_id"],
    ["#settingSpotifyClientSecret", "spotify_client_secret"],
    ["#settingOpServiceToken", "op_service_account_token"],
    ["#settingGiphyKey", "giphy_api_key"],
    ["#settingTenorKey", "tenor_api_key"],
    ["#settingApifyToken", "apify_api_token"],
    ["#settingFirecrawlKey", "firecrawl_api_key"],
    ["#settingEightctlEmail", "eightctl_email"],
    ["#settingEightctlPassword", "eightctl_password"],
    ["#settingDeliverooToken", "deliveroo_bearer_token"],
    ["#settingDeliverooCookie", "deliveroo_cookie"],
    ["#settingThingsToken", "things_auth_token"],
    ["#settingSagKey", "sag_api_key"],
  ];
}

function clearProviderCredentialDrafts() {
  providerCredentialFields().forEach(([selector]) => { $(selector).value = ""; });
  document.querySelectorAll(".model-provider-key").forEach((control) => { control.value = ""; });
}

function discardSettingsChanges() {
  settingsRequestGeneration += 1;
  clearProviderCredentialDrafts();
  settingsFormBaseline = null;
  settingsPreferenceBaseline = null;
  setSettingsFormHydrating(true);
  setSettingsFormDirty(false, { announce: false });
  settingModelDirty = false;
  settingReasoningDirty = false;
  clearDiscussionTeamDraft();
}

async function requestCloseWorkspaceDialog() {
  const dialog = $("#workspaceDialog");
  if (settingsFormDirty || settingsSavePending) {
    const confirmed = await askForConfirmation(
      settingsSavePending ? uiText(
        "保存已经提交，关闭不会取消保存。尚未提交的新修改会被放弃；重新打开设置会同步保存结果。",
        "Saving has already been submitted and closing will not cancel it. Newer unsaved edits will be discarded; reopening Settings will synchronize the result.",
      ) : uiText(
        "设置中还有未保存的更改。关闭后，这些更改会被放弃。",
        "Settings still contain unsaved changes. Closing will discard them.",
      ),
      {
        title: settingsSavePending ? uiText("保存期间关闭设置？", "Close Settings while saving?") : uiText("放弃未保存的设置？", "Discard unsaved settings?"),
        confirmLabel: settingsSavePending ? uiText("继续关闭", "Close Settings") : uiText("放弃更改", "Discard changes"),
        danger: true,
      },
    );
    if (!confirmed) return false;
    discardSettingsChanges();
  }
  clearTimeout(settingsRetryTimer);
  settingsRetryTimer = null;
  dialog?.close();
  setPrimaryNavigation("navChats");
  focusMainContentTarget();
  return true;
}

function openWorkspacePanel(name) {
  navigationIntent += 1;
  closeNavigation();
  hideSubagentPanel();
  setPrimaryNavigation("");
  const titles = isEnglish()
    ? { artifacts: "Artifacts", schedules: "Scheduled tasks", settings: "Settings" }
    : { artifacts: "产物", schedules: "定时任务", settings: "设置" };
  $("#workspaceDialogTitle").textContent = titles[name];
  document.querySelectorAll(".workspace-tabs button").forEach((button) => {
    const active = button.dataset.panel === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll(".workspace-panel").forEach((panel) => {
    const active = panel.id === `panel-${name}`;
    panel.classList.toggle("active", active);
    panel.setAttribute("aria-hidden", active ? "false" : "true");
  });
  $("#settingsSectionNav").hidden = name !== "settings";
  $("#settingsSaveBar").hidden = name !== "settings";
  const dialog = $("#workspaceDialog");
  if (!dialog.open) dialog.showModal();
  if (name === "settings") scheduleActiveSettingsSectionAlignment();
  if (isEnglish()) applyWorkspaceEnglish(name);
  enhanceWorkspaceSelects();
  if (settingsFormDirty) setSettingsFormDirty(true);
  if (name === "artifacts") loadArtifacts();
  if (name === "schedules") loadSchedules();
  if (name === "settings" && !settingsFormDirty) loadSettings();
}

function applyWorkspaceEnglish(name) {
  const text = (selector, value) => { const node = $(selector); if (node) node.textContent = value; };
  const attr = (selector, name, value) => { const node = $(selector); if (node) node.setAttribute(name, value); };
  const replaceLabelTexts = (selector, values) => {
    document.querySelectorAll(selector).forEach((label, index) => {
      const textNode = [...label.childNodes].find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
      if (textNode && values[index]) textNode.textContent = values[index];
    });
  };
  const directText = (selector, value) => {
    const node = $(selector);
    if (!node) return;
    const textNode = [...node.childNodes].find((item) => item.nodeType === Node.TEXT_NODE);
    if (textNode) textNode.textContent = value;
  };
  if (name === "artifacts") {
    text("#panel-artifacts .panel-toolbar b", "Task artifacts");
    text("#panel-artifacts .panel-toolbar span", "Saved in outputs/ and available to download");
    text("#refreshArtifacts", "Refresh");
    attr("#artifactSearch", "placeholder", "Search files or folders…");
    attr("#artifactSearch", "aria-label", "Search task artifacts");
    attr("#artifactType", "aria-label", "Filter artifacts by type");
    const artifactTypeLabels = ["All types", "Folders", "Documents", "Images", "Videos", "Audio", "Code", "Other"];
    document.querySelectorAll("#artifactType option").forEach((option, index) => {
      if (artifactTypeLabels[index]) option.textContent = artifactTypeLabels[index];
    });
  }
  if (name === "schedules") {
    attr("#scheduleStartAt", "lang", "en-US");
    attr("#scheduleEndAt", "lang", "en-US");
    attr("#scheduleName", "placeholder", "For example: daily work summary");
    attr("#scheduleKind", "aria-label", "Schedule type");
    const kindDaily = $("#scheduleKind option[value='daily']");
    const kindInterval = $("#scheduleKind option[value='interval']");
    const kindAt = $("#scheduleKind option[value='at']");
    if (kindDaily) kindDaily.textContent = "Every day";
    if (kindInterval) kindInterval.textContent = "Repeat at an interval";
    if (kindAt) kindAt.textContent = "Run once";
    replaceLabelTexts("#panel-schedules .form-grid > label", ["Task name", "Run", "Start time", "End time (optional)", "What should it do"]);
    attr("#schedulePrompt", "placeholder", "Describe what the Agent should complete when the time arrives");
    const intervalSpans = document.querySelectorAll("#scheduleIntervalField > span");
    if (intervalSpans[0]) intervalSpans[0].textContent = "Every";
    if (intervalSpans[1]) intervalSpans[1].textContent = "run once";
    const units = { 60: "minutes", 3600: "hours", 86400: "days" };
    Object.entries(units).forEach(([value, label]) => { const option = $(`#scheduleIntervalUnit option[value="${value}"]`); if (option) option.textContent = label; });
    text("#panel-schedules .form-actions span", "Daily and repeating tasks may have an end time. Leave it blank to keep running; one-time tasks stop automatically.");
    text("#scheduleForm button[type='submit']", "Create scheduled task");
  }
  if (name === "settings") {
    ["General", "Agent team", "Models & keys", "Remote channels", "Voice", "Runtime"].forEach((value, index) => {
      const button = document.querySelectorAll("#settingsSectionNav button")[index];
      if (button) button.textContent = value;
    });
    $("#settingsSectionNav")?.setAttribute("aria-label", "Settings categories");
    text("#panel-settings .discussion-team-heading > div > span", "Agent discussion team");
    text("#panel-settings .discussion-team-heading > div > small", "Discuss and reach consensus first, execute with full tools through sequential handoffs, then let the leader verify.");
    text("#addDiscussionTeamMember", document.querySelectorAll(".discussion-team-row").length ? "+ Add member" : "+ Create Agent team");
    text("#clearDiscussionTeam .button-label", "Clear Agent team configuration");
    const teamHelp = $("#panel-settings .discussion-team-setting > small");
    if (teamHelp) teamHelp.textContent = "Edit each participant's name, role, model, assignment, and system prompt, then choose Save settings. A valid saved team automatically appears in the main response-model picker. Minimum 2 participants with exactly one leader.";
    if ($("#discussionTeamRows")?.children.length) renderDiscussionTeamRows(collectDiscussionTeamRows());
    text("#panel-settings .cross-context-setting > span", "Cross-chat context");
    directText("#panel-settings .cross-context-setting .settings-checkbox", "Let new tasks reference all relevant earlier chats");
    text("#panel-settings .cross-context-setting small", "Searches all completed relevant chats with decreasing weight for older turns; tool logs, failed traces, and keys are excluded.");
    text("#settingsSaveState", "Keys show configuration status only; plaintext is never returned");
    text("#saveSettings", "Save settings");
    text("#visionSetupCard .vision-kicker", "Layered image recognition");
    text("#runtimeCards .panel-empty", "Diagnosing…");
    translateRuntimeCards();
    window.setTimeout(translateSettingsDynamic, 350);
    window.setTimeout(translateSettingsDynamic, 3000);
    attr("#settingFeishuAppId", "placeholder", "Optional app ID");
    attr("#settingFeishuAppSecret", "placeholder", "Optional app secret");
    attr("#settingFeishuOpenId", "placeholder", "For example, ou_xxx");
    text(".voice-setting > span", "Voice conversation");
    const voiceLabels = document.querySelectorAll(".voice-setting > label");
    ["Live dictation language", "Reading voice", "Reading speed", "Read final answers aloud", "Send when I finish speaking", "Keep listening after replies"].forEach((value, index) => {
      const label = voiceLabels[index];
      if (!label) return;
      const node = [...label.childNodes].find((item) => item.nodeType === Node.TEXT_NODE && item.textContent.trim());
      if (node) node.textContent = value;
    });
    text("#settingVoiceLanguage option[value='auto']", "Automatic (follow Windows system language)");
    text("#settingVoiceLanguage option[value='zh-CN']", "Chinese (Mandarin)");
    text("#settingVoiceLanguage option[value='en-US']", "English (US)");
    text(".voice-setting small", "Automatic dictation follows the Windows system language, independently of the interface language; Chinese, English, and other common languages can also be selected explicitly. Playback prefers a system voice, uses online neural speech when needed, and falls back to Windows speech offline. Only confirmed text is sent to the Agent.");
  }
}

function dateTimeLocalValue(date) {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function parseScheduleDateTime(value) {
  // No Date.parse locale ambiguity, rollover (Feb 31), or DST-gap coercion.
  const parts = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})$/.exec(String(value));
  if (!parts) return null;
  const [year, month, day, hour, minute] = parts.slice(1).map(Number);
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59) return null;
  const date = new Date(0);
  date.setFullYear(year, month - 1, day);
  date.setHours(hour, minute, 0, 0);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day
      || date.getHours() !== hour || date.getMinutes() !== minute) return null;
  return date;
}

function initializeScheduleDatePickers() {
  // Text remains editable and validated even if this optional local library
  // cannot load. Never fall back to OS-localized native date segments.
  document.querySelectorAll("[data-local-datetime]").forEach((input) => {
    input.lang = isEnglish() ? "en-US" : "zh-CN";
    input.title = uiText("日期与时间：YYYY-MM-DD HH:mm", "Date and time: YYYY-MM-DD HH:mm");
    if (typeof window.AirDatepicker !== "function" || input.elrenDatepicker) return;
    const picker = new window.AirDatepicker(input, {
      locale: window.ElrenDateLocales[isEnglish() ? "en" : "zh"],
      dateFormat: "yyyy-MM-dd", timeFormat: "HH:mm", dateTimeSeparator: " ",
      timepicker: true, minutesStep: 1, keyboardNav: true,
      buttons: ["today", "clear"], toggleSelected: false,
      container: $("#workspaceDialog"), classes: "elren-datepicker",
      position({ $datepicker, $target }) {
        // Components are rebuilt after hide; label the actual range inputs on
        // each show/position, not only the constructor's empty popup shell.
        $datepicker.querySelector('input[name="hours"]')?.setAttribute("aria-label", uiText("小时", "Hours"));
        $datepicker.querySelector('input[name="minutes"]')?.setAttribute("aria-label", uiText("分钟", "Minutes"));
        const rect = $target.getBoundingClientRect();
        const height = $datepicker.offsetHeight;
        const width = $datepicker.offsetWidth;
        $datepicker.style.position = "fixed";
        $datepicker.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
        $datepicker.style.top = `${Math.max(8, rect.bottom + height + 8 < window.innerHeight ? rect.bottom + 4 : rect.top - height - 4)}px`;
      },
      onSelect() {
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
      },
    });
    input.elrenDatepicker = picker;
    input.addEventListener("focus", () => {
      const date = parseScheduleDateTime(input.value);
      if (date) { picker.selectDate(date, { silent: true, updateTime: true }); picker.setViewDate(date); }
      // Do not select/clear an invalid partial input: it belongs to the user.
    });
    input.addEventListener("input", () => {
      if (!input.value) picker.clear({ silent: true });
    });
    const hideVisiblePicker = () => {
      // Air Datepicker 3.6.0 hide() destroys components and is not idempotent.
      // visible is already false while hiding; show() can cancel that hide
      // while hideAnimation remains true, so do not use that stale flag here.
      if (picker.visible) picker.hide();
    };
    $("#workspaceDialog").addEventListener("close", hideVisiblePicker);
    $("#workspaceDialog").addEventListener("scroll", hideVisiblePicker, true);
  });
}

function resetScheduleForm() {
  $("#scheduleForm").reset();
  $("#scheduleKind").value = "daily";
  const start = new Date(Date.now() + 60 * 60 * 1000);
  start.setSeconds(0, 0);
  $("#scheduleStartAt").value = dateTimeLocalValue(start).replace("T", " ");
  $("#scheduleEndAt").value = "";
  $("#scheduleIntervalValue").value = "1";
  $("#scheduleIntervalUnit").value = "3600";
  $("#scheduleEndAt").elrenDatepicker?.clear({ silent: true });
  updateScheduleFields();
  syncSettingsSelectWidget($("#scheduleKind"));
  syncSettingsSelectWidget($("#scheduleIntervalUnit"));
}

function updateScheduleFields() {
  const kind = $("#scheduleKind").value;
  $("#scheduleIntervalField").classList.toggle("hidden", kind !== "interval");
  $("#scheduleEndField").classList.toggle("hidden", kind === "at");
  // Hidden inputs still participate in native constraint validation unless
  // disabled. A stale invalid interval must not block daily/one-time schedules.
  for (const selector of ["#scheduleIntervalValue", "#scheduleIntervalUnit"]) {
    const input = $(selector);
    input.disabled = kind !== "interval";
    syncLocalizedConstraint(input);
  }
  $("#scheduleEndAt").disabled = kind === "at";
  if (kind === "at") {
    $("#scheduleEndAt").value = "";
    $("#scheduleEndAt").elrenDatepicker?.clear({ silent: true });
  }
  syncLocalizedConstraint($("#scheduleEndAt"));
  syncSettingsSelectWidget($("#scheduleIntervalUnit"));
}

function scheduleIntervalText(seconds) {
  const value = Number(seconds || 0);
  if (value % 86400 === 0) return uiText(`每 ${value / 86400} 天`, `Every ${value / 86400} day(s)`);
  if (value % 3600 === 0) return uiText(`每 ${value / 3600} 小时`, `Every ${value / 3600} hour(s)`);
  return uiText(`每 ${Math.max(1, Math.round(value / 60))} 分钟`, `Every ${Math.max(1, Math.round(value / 60))} minute(s)`);
}

function scheduleDescription(schedule) {
  const start = schedule.start_at ? formatUiDateTime(schedule.start_at) : "";
  const end = schedule.end_at ? formatUiDateTime(schedule.end_at) : uiText("持续执行", "No end date");
  const timezone = schedule.timezone || "UTC";
  if (schedule.kind === "daily") return uiText(`每天执行 · 从 ${start} 开始 · ${timezone} · ${end}`, `Every day · starts ${start} · ${timezone} · ${end}`);
  if (schedule.kind === "interval") return `${scheduleIntervalText(schedule.expression)} · ${uiText("开始", "starts")} ${start || uiText("创建后", "after creation")} · ${end}`;
  if (schedule.kind === "at") return uiText(`仅执行一次 · ${start || formatUiDateTime(schedule.expression)}`, `Run once · ${start || formatUiDateTime(schedule.expression)}`);
  return `Cron · ${schedule.expression}`;
}

async function scheduleAction(action, id, enabled) {
  if (action === "delete" && !await askForConfirmation(
    uiText("确定删除这个定时任务吗？删除后将无法恢复。", "Delete this scheduled task? It cannot be restored."),
    { title: uiText("删除定时任务", "Delete scheduled task"), confirmLabel: uiText("删除", "Delete"), danger: true },
  )) return;
  try {
    if (action === "run") await api(`/api/schedules/${id}/run`, { method: "POST" });
    if (action === "toggle") await api(`/api/schedules/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: enabled !== "true" }) });
    if (action === "delete") await api(`/api/schedules/${id}`, { method: "DELETE" });
    await loadSchedules();
    await loadTaskHistory();
  } catch (error) {
    showWorkspaceToast(`${uiText("定时任务操作失败：", "Scheduled task action failed: ")}${error.message}`, "error");
  }
}

const configurableModelProviderLabels = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  google: "Google",
  xai: "xAI",
};

function modelDisplayName(selector, options = null) {
  const available = options || latestStatus?.available_models || [];
  const option = available.find((item) => item.selector === selector);
  if (option?.source === "custom") return `${option.model} · ${option.base_url}`;
  if (option) return option.model || option.selector;
  if (selector === "discussion-team") return uiText("讨论团", "Agent team");
  if (selector === "auto") return uiText("自动", "Automatic");
  return selector || uiText("未选择模型", "No model selected");
}

function closeModelPreferenceMenu({ restoreFocus = false } = {}) {
  const shell = document.querySelector(".model-select-shell");
  const button = $("#modelPreferenceButton");
  const menu = $("#modelPreferenceMenu");
  if (!shell || !button || !menu) return;
  menu.hidden = true;
  shell.dataset.open = "false";
  button.setAttribute("aria-expanded", "false");
  if (restoreFocus) button.focus({ preventScroll: true });
}

function focusAdjacentTabStop(reference, { backwards = false } = {}) {
  if (!reference) return;
  const root = reference.closest("dialog[open]") || document;
  const candidates = [...root.querySelectorAll(
    'button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])',
  )].filter((element) => (
    element.getAttribute("aria-hidden") !== "true"
    && !element.closest("[hidden]")
    && !element.closest("[inert]")
    && (element.offsetParent !== null || getComputedStyle(element).position === "fixed")
  ));
  const index = candidates.indexOf(reference);
  if (index < 0 || !candidates.length) {
    reference.focus({ preventScroll: true });
    return;
  }
  const nextIndex = backwards
    ? (index - 1 + candidates.length) % candidates.length
    : (index + 1) % candidates.length;
  candidates[nextIndex]?.focus({ preventScroll: true });
}

function focusModelPreferenceOption(direction) {
  const menu = $("#modelPreferenceMenu");
  if (!menu || menu.hidden) return;
  const options = [...menu.querySelectorAll(".model-select-option")];
  if (!options.length) return;
  const current = Math.max(0, options.indexOf(document.activeElement));
  let next = current;
  if (direction === "first") next = 0;
  else if (direction === "last") next = options.length - 1;
  else next = (current + Number(direction) + options.length) % options.length;
  options[next].focus({ preventScroll: true });
  options[next].scrollIntoView({ block: "nearest" });
}

function openModelPreferenceMenu({ focusSelected = true } = {}) {
  const shell = document.querySelector(".model-select-shell");
  const button = $("#modelPreferenceButton");
  const menu = $("#modelPreferenceMenu");
  if (!shell || !button || !menu) return;
  menu.hidden = false;
  shell.dataset.open = "true";
  button.setAttribute("aria-expanded", "true");
  if (focusSelected) {
    requestAnimationFrame(() => {
      const selected = menu.querySelector('[aria-selected="true"]') || menu.querySelector(".model-select-option");
      selected?.focus({ preventScroll: true });
      selected?.scrollIntoView({ block: "nearest" });
    });
  }
}

function refreshModelPreferenceUI() {
  const select = $("#modelPreference");
  const value = $("#modelPreferenceValue");
  const button = $("#modelPreferenceButton");
  const menu = $("#modelPreferenceMenu");
  if (!select || !value || !button || !menu) return;
  const selected = select.selectedOptions[0] || select.options[0];
  const selectedLabel = selected?.textContent?.trim() || uiText("自动", "Automatic");
  value.textContent = selectedLabel;
  button.title = selectedLabel;
  menu.replaceChildren();
  [...select.options].forEach((option, index) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "model-select-option";
    item.id = `modelPreferenceOption-${index}`;
    item.dataset.value = option.value;
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", option.value === select.value ? "true" : "false");
    item.textContent = option.textContent;
    item.title = option.textContent;
    if ((option.textContent || "").trim().length >= 24) {
      item.classList.add("model-select-option-long");
    }
    item.addEventListener("click", () => {
      if (select.value !== option.value) {
        select.value = option.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      } else {
        refreshModelPreferenceUI();
      }
      closeModelPreferenceMenu({ restoreFocus: true });
    });
    menu.append(item);
  });
}

function initializeModelPreferencePicker() {
  const shell = document.querySelector(".model-select-shell");
  const select = $("#modelPreference");
  const button = $("#modelPreferenceButton");
  const menu = $("#modelPreferenceMenu");
  if (!shell || !select || !button || !menu || shell.dataset.initialized === "true") return;
  shell.dataset.initialized = "true";
  shell.dataset.open = "false";
  button.addEventListener("click", () => {
    if (menu.hidden) openModelPreferenceMenu();
    else closeModelPreferenceMenu({ restoreFocus: true });
  });
  button.addEventListener("keydown", (event) => {
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      openModelPreferenceMenu();
      requestAnimationFrame(() => {
        if (event.key === "Home") focusModelPreferenceOption("first");
        else if (event.key === "End") focusModelPreferenceOption("last");
        else if (event.key === "ArrowDown") focusModelPreferenceOption(1);
        else focusModelPreferenceOption(-1);
      });
    }
  });
  menu.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeModelPreferenceMenu({ restoreFocus: true });
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      focusModelPreferenceOption(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      focusModelPreferenceOption(-1);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusModelPreferenceOption("first");
    } else if (event.key === "End") {
      event.preventDefault();
      focusModelPreferenceOption("last");
    } else if (event.key === "Tab") {
      event.preventDefault();
      closeModelPreferenceMenu();
      focusAdjacentTabStop(button, { backwards: event.shiftKey });
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (!shell.contains(event.target)) closeModelPreferenceMenu();
  });
  window.addEventListener("blur", () => closeModelPreferenceMenu());
  refreshModelPreferenceUI();
}

const ALL_REASONING_LEVELS = ["auto", "minimal", "low", "medium", "high", "xhigh", "max"];

function reasoningLevelLabel(value) {
  const labels = isEnglish()
    ? { auto: "Automatic", minimal: "Minimal", low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Maximum" }
    : { auto: "自动", minimal: "最低", low: "低", medium: "中", high: "高", xhigh: "极高", max: "最高" };
  return labels[value] || labels.high;
}

function reasoningPreferenceValue() {
  const index = Math.max(0, Math.min(activeReasoningLevels.length - 1, Number($("#reasoningPreference")?.value || 0)));
  return activeReasoningLevels[index] || "auto";
}

function syncReasoningDots() {
  const container = document.querySelector(".reasoning-dots");
  if (!container) return;
  // Tick marks are a capability map, not decoration.  A fixed six-dot track
  // made DeepSeek's three real positions look like six selectable levels.
  // Rebuild it from the same list that defines the range input's min/max.
  container.replaceChildren(...activeReasoningLevels.map(() => document.createElement("i")));
}

function effectiveModelSelector(activeSelector = "") {
  const selected = $("#modelPreference")?.value || "auto";
  if (activeSelector) return activeSelector;
  if (selected === "discussion-team") {
    return discussionTeamLeaderModel !== "auto"
      ? discussionTeamLeaderModel
      : (defaultModelSelector !== "auto" ? defaultModelSelector : (latestStatus?.model || ""));
  }
  if (selected !== "auto") return selected;
  if (defaultModelSelector !== "auto") return defaultModelSelector;
  // Automatic routing still has a concrete current candidate supplied by the
  // backend. On first page load use that capability instead of treating the
  // literal selector "auto" as an unsupported model and hiding the slider.
  return latestStatus?.model || availableModelOptions[0]?.selector || "";
}

function reasoningCapability(activeSelector = "") {
  const selector = effectiveModelSelector(activeSelector);
  return availableModelOptions.find((item) => item.selector === selector)?.reasoning || {
    supported: false, levels: [], control: "none",
  };
}

function syncReasoningAvailability(activeSelector = "", preferredValue = "") {
  const picker = $("#reasoningPreference")?.closest(".reasoning-picker");
  const input = $("#reasoningPreference");
  if (!picker || !input) return;
  const capability = reasoningCapability($("#modelPreference")?.dataset?.languageResumeModel ? "" : activeSelector);
  const previous = input.dataset?.languageResumeReasoning || preferredValue || reasoningPreferenceValue();
  activeReasoningLevels = capability.supported
    ? ["auto", ...(capability.levels || []).filter((value) => value !== "auto")]
    : ["auto"];
  syncReasoningDots();
  input.min = "0";
  input.max = String(Math.max(0, activeReasoningLevels.length - 1));
  input.disabled = !capability.supported;
  picker.hidden = !capability.supported;
  picker.dataset.reasoningControl = capability.control || "none";
  const normalized = activeReasoningLevels.includes(previous)
    ? previous
    : (activeReasoningLevels.includes("high") ? "high" : "auto");
  setReasoningPreference(normalized);
}

function updateReasoningVisualState(value = reasoningPreferenceValue()) {
  const picker = $("#reasoningPreference")?.closest(".reasoning-picker");
  const wrap = picker?.querySelector(".reasoning-slider-wrap");
  if (!picker || !wrap) return;
  const normalized = activeReasoningLevels.includes(value) ? value : "auto";
  const index = activeReasoningLevels.indexOf(normalized);
  const progress = activeReasoningLevels.length > 1
    ? index / (activeReasoningLevels.length - 1)
    : 0;
  const trackWidth = wrap.getBoundingClientRect().width;
  const thumbDiameter = 27;
  const thumbCenter = thumbDiameter / 2 + progress * Math.max(0, trackWidth - thumbDiameter);
  // Keep the animated fill endpoint under the native thumb at every level.
  // Collapsing it to zero at the first level briefly exposed a square edge
  // outside the circular thumb while moving left.
  const fillWidth = index === activeReasoningLevels.length - 1 ? trackWidth : thumbCenter;
  const intensive = normalized === "xhigh" || normalized === "max";
  picker.dataset.reasoningTone = intensive ? "intensive" : "standard";
  picker.dataset.reasoningEdge = index === 0 ? "min" : (index === activeReasoningLevels.length - 1 ? "max" : "middle");
  wrap.style.setProperty("--reasoning-progress", `${progress * 100}%`);
  wrap.style.setProperty("--reasoning-fill-width", trackWidth > 0 ? `${fillWidth}px` : `${progress * 100}%`);
  wrap.style.setProperty("--reasoning-thumb-center", trackWidth > 0 ? `${thumbCenter}px` : `${progress * 100}%`);
}

function setReasoningPreference(value) {
  const normalized = activeReasoningLevels.includes(value) ? value : "auto";
  const index = activeReasoningLevels.indexOf(normalized);
  const input = $("#reasoningPreference");
  if (!input) return;
  input.value = String(index);
  const progress = activeReasoningLevels.length > 1
    ? index / (activeReasoningLevels.length - 1)
    : 0;
  input.style.setProperty("--reasoning-progress", `${progress * 100}%`);
  updateReasoningVisualState(normalized);
  input.setAttribute("aria-label", `${uiText("思考深度", "Reasoning depth")}: ${reasoningLevelLabel(normalized)}`);
  input.closest(".reasoning-picker")?.setAttribute(
    "aria-label",
    uiText("思考深度", "Reasoning depth"),
  );
  const output = $("#reasoningPreferenceLabel");
  if (output) {
    output.textContent = reasoningLevelLabel(normalized);
  }
}

function syncDefaultReasoningOptions(selector, preferredValue = "") {
  const select = $("#settingReasoningEffort");
  if (!select) return;
  const capabilitySelector = selector === "auto"
    ? (automaticDefaultModelSelector || latestStatus?.model || availableModelOptions[0]?.selector || "")
    : selector;
  const capability = availableModelOptions.find((item) => item.selector === capabilitySelector)?.reasoning;
  const levels = capability?.supported
    ? ["auto", ...(capability.levels || []).filter((value) => value !== "auto")]
    : ["auto"];
  const previous = preferredValue || select.value || "auto";
  select.innerHTML = levels
    .filter((value) => ALL_REASONING_LEVELS.includes(value))
    .map((value) => `<option value="${value}">${escapeHtml(reasoningLevelLabel(value))}</option>`)
    .join("");
  select.value = levels.includes(previous)
    ? previous
    : (levels.includes("high") ? "high" : "auto");
  select.disabled = !capability?.supported;
  const help = select.closest(".reasoning-setting")?.querySelector("small");
  if (help) {
    help.textContent = capability?.verification === "user_declared"
      ? uiText("档位由你按第三方接口文档勾选，未经实测；自动档不发送强度参数。", "Levels are declared by you, not verified. Automatic omits the effort parameter.")
      : capability?.supported
      ? uiText(
          `仅显示该模型由官方 API 支持的档位（${capability.control}）；不会用提示词模拟。`,
          `Only levels supported by this model's official API are shown (${capability.control}); no prompt simulation.`,
        )
      : uiText(
          "该模型没有经过验证的 API 推理强度参数，因此不提供档位选择。",
          "This model has no verified API reasoning-depth parameter, so no level selector is offered.",
        );
  }
}

function modelOptionsForDisplay(options) {
  // Display order must not change routing/capability fallbacks that use the
  // original catalog. Move Astra ahead of Sol only within the same provider.
  const ordered = [...options];
  for (const astra of options.filter((item) => item.model === "gpt-6-astra")) {
    const astraIndex = ordered.indexOf(astra);
    const solIndex = ordered.findIndex((item) =>
      item.provider === astra.provider && item.model === "gpt-5.6-sol"
    );
    if (solIndex >= 0 && astraIndex > solIndex) {
      ordered.splice(astraIndex, 1);
      ordered.splice(solIndex, 0, astra);
    }
  }
  return ordered;
}

function syncModelSelectors(settings) {
  // Old display caches or an older running backend cannot restore a removed option.
  settings = { ...settings, model: isRetiredBuiltinModel(settings.model) ? "auto" : settings.model };
  const suppliedOptions = settings.available_models || [];
  const options = suppliedOptions.some((item) => isRetiredBuiltinModel(item.selector))
    ? suppliedOptions.filter((item) => !isRetiredBuiltinModel(item.selector)) : suppliedOptions;
  availableModelOptions = options;
  if (typeof settings.active_model === "string" && settings.active_model !== "auto") {
    automaticDefaultModelSelector = settings.active_model;
  }
  if (Object.prototype.hasOwnProperty.call(settings, "discussion_team_enabled")) {
    discussionTeamConfigured = settings.discussion_team_enabled === true;
  }
  if (Array.isArray(settings.discussion_team)) {
    discussionTeamLeaderModel = settings.discussion_team.find((item) => item.role === "leader")?.model || "auto";
  } else if (typeof settings.discussion_team_leader_model === "string") {
    discussionTeamLeaderModel = settings.discussion_team_leader_model || "auto";
  }
  const preference = $("#modelPreference");
  const defaultModel = $("#settingModel");
  const requestedPreference = preference.dataset?.languageResumeModel || pendingLanguageSwitchModel;
  const previousPreference = requestedPreference || preference.value || "auto";
  // A second settings request can finish after the user has already changed
  // the select. Preserve that unsaved local choice until save or dialog close
  // instead of snapping back to the server's previous value.
  const pendingDefault = settingModelDirty
    ? (defaultModel.value || settings.model)
    : (settings.model || defaultModel.value);
  const currentDefault = isRetiredBuiltinModel(pendingDefault) ? "auto" : pendingDefault;
  const currentReasoning = settingReasoningDirty
    ? ($("#settingReasoningEffort")?.value || settings.reasoning_effort || "auto")
    : (settings.reasoning_effort || "auto");
  const optionMarkup = modelOptionsForDisplay(options).map((item) =>
    `<option value="${escapeHtml(item.selector)}">${escapeHtml(modelDisplayName(item.selector, options))}</option>`
  ).join("");
  const teamOptionMarkup = discussionTeamConfigured || currentTaskSnapshot?.discussion_team_enabled
    ? `<option value="discussion-team">${uiText("讨论团", "Agent team")}</option>`
    : "";
  preference.innerHTML = `<option value="auto">${uiText("自动", "Automatic")}</option>${teamOptionMarkup}${optionMarkup}`;
  defaultModel.innerHTML = `<option value="auto">${uiText("自动", "Automatic")}</option>${optionMarkup}`;
  if (
    previousPreference === "auto"
    || (previousPreference === "discussion-team" && Boolean(teamOptionMarkup))
    || options.some((item) => item.selector === previousPreference)
  ) {
    preference.value = previousPreference;
  } else {
    preference.value = "auto";
  }
  if (requestedPreference) {
    pendingLanguageSwitchModel = "";
    try {
      const cleanUrl = new URL(window.location.href);
      cleanUrl.searchParams.delete("model");
      window.history.replaceState(null, "", cleanUrl.toString());
    } catch {
      // The selection has already been restored; URL cleanup is optional.
    }
  }
  if (currentDefault !== "auto" && !options.some((item) => item.selector === currentDefault)) {
    defaultModel.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(currentDefault)}">${escapeHtml(modelDisplayName(currentDefault, options))}</option>`);
  }
  defaultModel.value = currentDefault;
  // The editor's unsaved default must never become Auto's live capability.
  defaultModelSelector = settings.model || "auto";
  refreshDiscussionTeamModelOptions();
  refreshModelPreferenceUI();
  syncDefaultReasoningOptions(currentDefault, currentReasoning);
  if (settingsPreferenceBaseline && settingsFormBaseline) {
    // Status can legitimately refresh untouched editor defaults. That is not
    // a user edit and must not appear in the next field-difference PATCH.
    const baseline = JSON.parse(settingsFormBaseline);
    const refreshed = [
      ["model", "settingModel", settingModelDirty],
      ["reasoning_effort", "settingReasoningEffort", settingReasoningDirty],
    ];
    refreshed.forEach(([field, id, dirty]) => {
      if (dirty) return;
      settingsPreferenceBaseline[field] = $(`#${id}`).value;
      const saved = baseline.fields.find((entry) => entry.key === id);
      if (saved) saved.value = $(`#${id}`).value;
    });
    settingsFormBaseline = JSON.stringify(baseline);
  }
  // Keep the composer capability in lockstep with every model-catalog update.
  // In particular, the visible selector may remain "auto" while its effective
  // model changes through Settings.  Relying on the caller to refresh this was
  // the reason opening Settings appeared to fix an over-broad reasoning slider.
  const initialReasoning = isBlankNewTaskComposer() && !reasoningDefaultLoaded
    && typeof settings.reasoning_effort === "string" ? settings.reasoning_effort : "";
  syncReasoningAvailability("", initialReasoning);
  if (initialReasoning) reasoningDefaultLoaded = true;
  if (isBlankNewTaskComposer()) rememberNewTaskComposerPreferences();
}

function discussionTeamModelOptions(selected = "auto") {
  const options = [
    `<option value="auto">${uiText("自动", "Automatic")}</option>`,
    ...modelOptionsForDisplay(availableModelOptions).map((item) =>
      `<option value="${escapeHtml(item.selector)}">${escapeHtml(modelDisplayName(item.selector, availableModelOptions))}</option>`
    ),
  ];
  if (selected !== "auto" && !availableModelOptions.some((item) => item.selector === selected)) {
    options.push(`<option value="${escapeHtml(selected)}">${escapeHtml(selected)} · ${uiText("当前不可用", "currently unavailable")}</option>`);
  }
  return options.join("");
}

function discussionTeamReasoningOptions(selector = "auto", selected = "default") {
  const effectiveSelector = selector === "auto"
    ? (defaultModelSelector !== "auto" ? defaultModelSelector : (latestStatus?.model || ""))
    : selector;
  const capability = availableModelOptions.find((item) => item.selector === effectiveSelector)?.reasoning;
  const levels = capability?.supported
    ? (capability.levels || []).filter((value) => ALL_REASONING_LEVELS.includes(value))
    : [];
  const values = ["default", ...levels.filter((value) => value !== "default")];
  if (selected !== "default" && !values.includes(selected)) values.push(selected);
  return values.map((value) => {
    const unavailable = value === selected && value !== "default" && !levels.includes(value);
    const label = value === "default"
      ? uiText("跟随本轮", "Follow task")
      : `${reasoningLevelLabel(value)}${unavailable ? ` · ${uiText("当前不可用", "currently unavailable")}` : ""}`;
    return `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`;
  }).join("");
}

function closeDiscussionTeamSelectMenus(except = null) {
  document.querySelectorAll(".team-select-shell[data-open='true']").forEach((shell) => {
    if (shell === except) return;
    shell.dataset.open = "false";
    shell.querySelector(".team-select-menu")?.setAttribute("hidden", "");
    shell.querySelector(".team-select-button")?.setAttribute("aria-expanded", "false");
  });
}

let discussionTeamSelectId = 0;

function syncDiscussionTeamSelectWidget(select) {
  const shell = select?.closest(".team-select-shell");
  const button = shell?.querySelector(".team-select-button");
  const value = shell?.querySelector(".team-select-value");
  const menu = shell?.querySelector(".team-select-menu");
  if (!shell || !button || !value || !menu) return;
  const selected = select.selectedOptions[0] || select.options[0];
  const label = selected?.textContent?.trim() || "";
  const fieldLabel = select.closest("label")?.querySelector(":scope > span")?.textContent?.trim()
    || uiText("选择选项", "Choose an option");
  value.textContent = label;
  button.title = label;
  button.setAttribute("aria-label", `${fieldLabel}: ${label}`);
  menu.replaceChildren();
  [...select.options].forEach((option) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "team-select-option";
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", option.value === select.value ? "true" : "false");
    item.textContent = option.textContent;
    item.title = option.textContent;
    item.onclick = () => {
      if (select.value !== option.value) {
        select.value = option.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      closeDiscussionTeamSelectMenus();
      button.focus({ preventScroll: true });
    };
    menu.append(item);
  });
}

function enhanceDiscussionTeamSelect(select) {
  if (!select || select.dataset.teamEnhanced === "true") return;
  select.dataset.teamEnhanced = "true";
  const shell = document.createElement("span");
  shell.className = "team-select-shell";
  shell.dataset.open = "false";
  select.parentNode.insertBefore(shell, select);
  shell.append(select);
  select.classList.add("team-native-select");
  // The visible button/listbox is the accessible control. Keeping the
  // visually clipped native select tabbable/exposed creates two controls with
  // the same label and sends keyboard users to a 1px invisible target.
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "team-select-button";
  button.setAttribute("aria-haspopup", "listbox");
  button.setAttribute("aria-expanded", "false");
  button.innerHTML = '<span class="team-select-value"></span><span class="team-select-chevron" aria-hidden="true"></span>';
  const menu = document.createElement("span");
  menu.id = `discussion-team-select-menu-${++discussionTeamSelectId}`;
  button.id = `${menu.id}-button`;
  menu.className = "team-select-menu";
  menu.setAttribute("role", "listbox");
  button.setAttribute("aria-controls", menu.id);
  const containingLabel = select.closest("label");
  if (containingLabel) containingLabel.htmlFor = button.id;
  menu.hidden = true;
  shell.append(button, menu);
  const focusOption = (direction) => {
    const items = [...menu.querySelectorAll(".team-select-option")];
    if (!items.length) return;
    const active = items.indexOf(document.activeElement);
    const next = direction === "first" ? 0 : direction === "last" ? items.length - 1 : (Math.max(0, active) + Number(direction) + items.length) % items.length;
    items[next].focus({ preventScroll: true });
    items[next].scrollIntoView({ block: "nearest" });
  };
  const open = () => {
    closeDiscussionTeamSelectMenus(shell);
    shell.dataset.open = "true";
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    requestAnimationFrame(() => {
      const target = menu.querySelector('[aria-selected="true"]') || menu.querySelector(".team-select-option");
      target?.focus({ preventScroll: true });
      target?.scrollIntoView({ block: "nearest" });
    });
  };
  button.onclick = () => shell.dataset.open === "true" ? closeDiscussionTeamSelectMenus() : open();
  button.onkeydown = (event) => {
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      open();
      requestAnimationFrame(() => focusOption(event.key === "Home" ? "first" : event.key === "End" ? "last" : event.key === "ArrowDown" ? 1 : -1));
    }
  };
  menu.onkeydown = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeDiscussionTeamSelectMenus();
      button.focus({ preventScroll: true });
    } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      focusOption(event.key === "Home" ? "first" : event.key === "End" ? "last" : event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Tab") {
      event.preventDefault();
      closeDiscussionTeamSelectMenus();
      focusAdjacentTabStop(button, { backwards: event.shiftKey });
    }
  };
  select.addEventListener("change", () => syncDiscussionTeamSelectWidget(select));
  syncDiscussionTeamSelectWidget(select);
}

let settingsSelectId = 0;

function closeSettingsSelectMenus(except = null) {
  document.querySelectorAll(".settings-select-shell[data-open='true']").forEach((shell) => {
    if (shell === except) return;
    shell.dataset.open = "false";
    const menu = shell.querySelector(".settings-select-menu");
    const button = shell.querySelector(".settings-select-button");
    if (menu) {
      menu.hidden = true;
      menu.style.removeProperty("left");
      menu.style.removeProperty("top");
      menu.style.removeProperty("bottom");
      menu.style.removeProperty("width");
      menu.style.removeProperty("max-height");
    }
    button?.setAttribute("aria-expanded", "false");
  });
}

function positionSettingsSelectMenu(shell) {
  const button = shell?.querySelector(".settings-select-button");
  const menu = shell?.querySelector(".settings-select-menu");
  if (!button || !menu || menu.hidden) return;
  const rect = button.getBoundingClientRect();
  const viewportPadding = 12;
  const gap = 7;
  const availableBelow = Math.max(0, window.innerHeight - rect.bottom - viewportPadding - gap);
  const availableAbove = Math.max(0, rect.top - viewportPadding - gap);
  const placeAbove = availableBelow < 190 && availableAbove > availableBelow;
  const maxHeight = Math.max(120, Math.min(330, placeAbove ? availableAbove : availableBelow));
  const compact = shell.classList.contains("history-status-select-shell")
    || shell.classList.contains("artifact-type-select-shell")
    || shell.classList.contains("schedule-unit-select-shell");
  const minimumWidth = shell.classList.contains("history-status-select-shell")
    ? (isEnglish() ? 220 : 156)
    : shell.classList.contains("artifact-type-select-shell")
      ? Math.max(180, rect.width)
      : shell.classList.contains("schedule-unit-select-shell")
        ? Math.max(132, rect.width)
        : 268;
  const maximumWidth = compact
    ? Math.max(minimumWidth, Math.min(240, window.innerWidth - viewportPadding * 2))
    : window.innerWidth - viewportPadding * 2;
  const width = Math.min(
    Math.max(rect.width, minimumWidth),
    Math.max(minimumWidth, maximumWidth),
  );
  const preferredLeft = compact ? rect.right - width : rect.left;
  const left = Math.min(
    Math.max(viewportPadding, preferredLeft),
    Math.max(viewportPadding, window.innerWidth - width - viewportPadding),
  );
  menu.style.left = `${Math.round(left)}px`;
  menu.style.width = `${Math.round(width)}px`;
  menu.style.maxHeight = `${Math.round(maxHeight)}px`;
  menu.style.removeProperty(placeAbove ? "top" : "bottom");
  if (placeAbove) {
    menu.style.bottom = `${Math.round(window.innerHeight - rect.top + gap)}px`;
  } else {
    menu.style.top = `${Math.round(rect.bottom + gap)}px`;
  }
  shell.dataset.placement = placeAbove ? "above" : "below";
}

function syncSettingsSelectWidget(select) {
  const shell = select?.closest(".settings-select-shell");
  const button = shell?.querySelector(".settings-select-button");
  const value = shell?.querySelector(".settings-select-value");
  const menu = shell?.querySelector(".settings-select-menu");
  if (!shell || !button || !value || !menu) return;
  const selected = select.selectedOptions[0] || select.options[0];
  const label = selected?.textContent?.trim() || uiText("请选择", "Select an option");
  value.textContent = label;
  button.title = label;
  button.disabled = select.disabled;
  const controlLabel = select.getAttribute("aria-label")
    || select.closest("label")?.childNodes[0]?.textContent?.trim()
    || "";
  button.setAttribute(
    "aria-label",
    controlLabel ? `${controlLabel}${uiText("：", ": ")}${label}` : label,
  );
  menu.replaceChildren();
  [...select.options].forEach((option) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "settings-select-option";
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", option.value === select.value ? "true" : "false");
    item.disabled = option.disabled;
    item.textContent = option.textContent;
    item.title = option.textContent;
    item.onclick = () => {
      if (option.disabled) return;
      if (select.value !== option.value) {
        select.value = option.value;
        select.dispatchEvent(new Event("input", { bubbles: true }));
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      closeSettingsSelectMenus();
      button.focus({ preventScroll: true });
    };
    menu.append(item);
  });
}

function enhanceSettingsSelect(select) {
  if (!select || select.dataset.settingsEnhanced === "true" || select.dataset.teamEnhanced === "true") return;
  select.dataset.settingsEnhanced = "true";
  const shell = document.createElement("span");
  shell.className = "settings-select-shell";
  if (select.id === "historyStatus") shell.classList.add("history-status-select-shell");
  if (select.id === "artifactType") shell.classList.add("artifact-type-select-shell");
  if (select.id === "scheduleKind") shell.classList.add("schedule-kind-select-shell");
  if (select.id === "scheduleIntervalUnit") shell.classList.add("schedule-unit-select-shell");
  shell.dataset.open = "false";
  select.parentNode.insertBefore(shell, select);
  shell.append(select);
  select.classList.add("settings-native-select");
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");

  const button = document.createElement("button");
  button.type = "button";
  button.className = "settings-select-button";
  button.setAttribute("aria-haspopup", "listbox");
  button.setAttribute("aria-expanded", "false");
  button.innerHTML = '<span class="settings-select-value"></span><span class="settings-select-chevron" aria-hidden="true"></span>';
  const menu = document.createElement("span");
  menu.id = `settings-select-menu-${++settingsSelectId}`;
  button.id = `${menu.id}-button`;
  menu.className = "settings-select-menu";
  if (select.id === "historyStatus") menu.classList.add("history-status-select-menu");
  menu.setAttribute("role", "listbox");
  menu.hidden = true;
  button.setAttribute("aria-controls", menu.id);
  const containingLabel = select.closest("label");
  if (containingLabel) containingLabel.htmlFor = button.id;
  shell.append(button, menu);

  const focusOption = (direction) => {
    const items = [...menu.querySelectorAll(".settings-select-option:not(:disabled)")];
    if (!items.length) return;
    const active = items.indexOf(document.activeElement);
    const next = direction === "first"
      ? 0
      : direction === "last"
        ? items.length - 1
        : (Math.max(0, active) + Number(direction) + items.length) % items.length;
    items[next].focus({ preventScroll: true });
    items[next].scrollIntoView({ block: "nearest" });
  };
  const open = () => {
    if (button.disabled) return;
    closeDiscussionTeamSelectMenus();
    closeSettingsSelectMenus(shell);
    syncSettingsSelectWidget(select);
    shell.dataset.open = "true";
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    positionSettingsSelectMenu(shell);
    requestAnimationFrame(() => {
      const target = menu.querySelector('[aria-selected="true"]:not(:disabled)')
        || menu.querySelector(".settings-select-option:not(:disabled)");
      target?.focus({ preventScroll: true });
      if (target) {
        const targetRect = target.getBoundingClientRect();
        const menuRect = menu.getBoundingClientRect();
        if (targetRect.top < menuRect.top || targetRect.bottom > menuRect.bottom) {
          target.scrollIntoView({ block: "nearest" });
        }
      }
    });
  };
  button.onclick = () => shell.dataset.open === "true" ? closeSettingsSelectMenus() : open();
  button.onkeydown = (event) => {
    if (["ArrowDown", "ArrowUp", "Home", "End", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      open();
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        requestAnimationFrame(() => focusOption(
          event.key === "Home" ? "first" : event.key === "End" ? "last" : event.key === "ArrowDown" ? 1 : -1,
        ));
      }
    }
  };
  menu.onkeydown = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      closeSettingsSelectMenus();
      button.focus({ preventScroll: true });
    } else if (["Enter", " "].includes(event.key)
      && event.target instanceof HTMLButtonElement
      && event.target.classList.contains("settings-select-option")) {
      event.preventDefault();
      event.target.click();
    } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      focusOption(event.key === "Home" ? "first" : event.key === "End" ? "last" : event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Tab") {
      event.preventDefault();
      closeSettingsSelectMenus();
      focusAdjacentTabStop(button, { backwards: event.shiftKey });
    }
  };
  select.addEventListener("change", () => syncSettingsSelectWidget(select));
  syncSettingsSelectWidget(select);
}

function enhanceAllSettingsSelects() {
  document.querySelectorAll("#panel-settings select").forEach((select) => {
    if (select.dataset.teamEnhanced === "true") return;
    enhanceSettingsSelect(select);
    syncSettingsSelectWidget(select);
  });
}

function enhanceHistoryStatusSelect() {
  const select = $("#historyStatus");
  if (!select) return;
  enhanceSettingsSelect(select);
  syncSettingsSelectWidget(select);
}

function enhanceWorkspaceSelects() {
  ["artifactType", "scheduleKind", "scheduleIntervalUnit"].forEach((id) => {
    const select = document.getElementById(id);
    if (!select) return;
    enhanceSettingsSelect(select);
    syncSettingsSelectWidget(select);
  });
}

function observeSettingsSelects() {
  const panel = $("#panel-settings");
  if (!panel || panel.dataset.selectObserver === "true") return;
  panel.dataset.selectObserver = "true";
  const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
      if (mutation.target instanceof HTMLSelectElement) {
        if (mutation.target.dataset.teamEnhanced !== "true") {
          enhanceSettingsSelect(mutation.target);
          syncSettingsSelectWidget(mutation.target);
        }
      }
      mutation.addedNodes.forEach((node) => {
        if (!(node instanceof Element)) return;
        const selects = node.matches("select") ? [node] : [...node.querySelectorAll("select")];
        selects.forEach((select) => {
          if (select.dataset.teamEnhanced !== "true") enhanceSettingsSelect(select);
        });
      });
    });
  });
  observer.observe(panel, { childList: true, subtree: true });
  enhanceAllSettingsSelects();
}

document.addEventListener("pointerdown", (event) => {
  if (!event.target.closest(".team-select-shell")) closeDiscussionTeamSelectMenus();
  if (!event.target.closest(".settings-select-shell")) closeSettingsSelectMenus();
});
document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented) return;
  if (event.key === "Tab" && document.body.classList.contains("nav-open")
      && window.matchMedia("(max-width: 960px)").matches
      && !document.querySelector("dialog[open]")) {
    const sidebar = $("#taskSidebar");
    const controls = [$("#navOverlay"), ...sidebar.querySelectorAll(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex="0"]',
    )].filter((node) => node && node.getClientRects().length && !node.closest("[hidden], .hidden, [inert]"));
    const first = controls[0];
    const last = controls.at(-1);
    if (event.shiftKey && (document.activeElement === first || !controls.includes(document.activeElement))) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !controls.includes(document.activeElement))) {
      event.preventDefault();
      first?.focus();
    }
    return;
  }
  if (event.key !== "Escape") return;
  if (document.body.classList.contains("nav-open")) {
    event.preventDefault();
    closeNavigation({ restoreFocus: true });
    return;
  }
  if (document.querySelector(".settings-select-shell[data-open='true']")) {
    event.preventDefault();
    const button = document.querySelector(".settings-select-shell[data-open='true'] .settings-select-button");
    closeSettingsSelectMenus();
    button?.focus({ preventScroll: true });
  }
  closeDiscussionTeamSelectMenus();
});
window.matchMedia("(max-width: 960px)").addEventListener("change", (event) => {
  if (!event.matches) closeNavigation();
});
document.addEventListener("scroll", (event) => {
  if (event.target instanceof Element && event.target.closest(".settings-select-menu")) return;
  const shell = document.querySelector(".settings-select-shell[data-open='true']");
  const button = shell?.querySelector(".settings-select-button");
  if (!shell || !button) return;
  const rect = button.getBoundingClientRect();
  if (rect.bottom < 0 || rect.top > window.innerHeight) closeSettingsSelectMenus();
  else positionSettingsSelectMenu(shell);
}, true);
window.addEventListener("resize", () => {
  const shell = document.querySelector(".settings-select-shell[data-open='true']");
  if (shell) positionSettingsSelectMenu(shell);
});

function refreshDiscussionTeamReasoningOptions(row) {
  const model = row?.querySelector(".team-member-model")?.value || "auto";
  const select = row?.querySelector(".team-member-reasoning");
  if (!select) return;
  const selected = select.value || "default";
  select.innerHTML = discussionTeamReasoningOptions(model, selected);
  select.value = [...select.options].some((option) => option.value === selected) ? selected : "default";
  syncDiscussionTeamSelectWidget(select);
}

function refreshDiscussionTeamModelOptions() {
  document.querySelectorAll(".team-member-model").forEach((select) => {
    const selected = select.value || "auto";
    select.innerHTML = discussionTeamModelOptions(selected);
    select.value = [...select.options].some((option) => option.value === selected)
      ? selected
      : "auto";
    syncDiscussionTeamSelectWidget(select);
    refreshDiscussionTeamReasoningOptions(select.closest(".discussion-team-row"));
  });
}

function collectDiscussionTeamRows() {
  return [...document.querySelectorAll(".discussion-team-row")].map((row) => ({
    id: row.dataset.id || globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
    name: row.querySelector(".team-member-name")?.value.trim() || "",
    role: row.querySelector(".team-member-role")?.value === "leader" ? "leader" : "member",
    model: row.querySelector(".team-member-model")?.value || "auto",
    reasoning_effort: row.querySelector(".team-member-reasoning")?.value || "default",
    assignment: row.querySelector(".team-member-assignment")?.value.trim() || "",
    system_prompt: row.querySelector(".team-member-prompt")?.value.trim() || "",
  }));
}

function persistDiscussionTeamDraft() {
  clearTimeout(discussionTeamDraftTimer);
  discussionTeamDraftTimer = null;
  try {
    window.localStorage.setItem(
      DISCUSSION_TEAM_DRAFT_STORAGE_KEY,
      JSON.stringify(collectDiscussionTeamRows()),
    );
  } catch {}
}

function rememberDiscussionTeamDraft() {
  discussionTeamDirty = true;
  // Large teams can contain many long prompts. Avoid walking and serializing
  // the entire roster for every keystroke; mark the form immediately, then
  // persist and perform the value-based clean check after the user pauses.
  setSettingsFormDirty(true);
  clearTimeout(discussionTeamDraftTimer);
  discussionTeamDraftTimer = setTimeout(() => {
    persistDiscussionTeamDraft();
    markSettingsFormDirty();
  }, 180);
}

function readDiscussionTeamDraft() {
  try {
    const value = JSON.parse(window.localStorage.getItem(DISCUSSION_TEAM_DRAFT_STORAGE_KEY) || "null");
    return Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

function clearDiscussionTeamDraft() {
  clearTimeout(discussionTeamDraftTimer);
  discussionTeamDraftTimer = null;
  discussionTeamDirty = false;
  recoveredDiscussionTeamDraftPending = false;
  try { window.localStorage.removeItem(DISCUSSION_TEAM_DRAFT_STORAGE_KEY); } catch {}
}

function canonicalDiscussionTeam(entries = []) {
  return entries.map((entry) => {
    const originalName = String(entry?.name || "").trim();
    const memberName = originalName.match(/^(?:组员|Member)\s+(\d+)$/);
    const name = originalName === "组长" || originalName === "Leader"
      ? "__default_leader__"
      : memberName
        ? `__default_member_${memberName[1]}__`
        : originalName;
    return {
      name,
      role: entry?.role === "leader" ? "leader" : "member",
      model: String(entry?.model || "auto"),
      reasoning_effort: String(entry?.reasoning_effort || "default"),
      assignment: String(entry?.assignment || "").trim(),
      system_prompt: String(entry?.system_prompt || "").trim(),
    };
  });
}

function discussionTeamDraftDiffers(draft, saved) {
  if (!Array.isArray(draft)) return false;
  return JSON.stringify(canonicalDiscussionTeam(draft)) !== JSON.stringify(canonicalDiscussionTeam(saved));
}

function updateDiscussionTeamMemberCount() {
  const count = document.querySelectorAll(".discussion-team-row").length;
  const node = $("#discussionTeamMemberCount");
  if (!node) return;
  node.textContent = count
    ? uiText(`${count} 名成员`, `${count} participants`)
    : uiText("尚未保存团队", "No saved team");
}

function renderDiscussionTeamRows(entries = [], { preserveEmpty = false } = {}) {
  const container = $("#discussionTeamRows");
  if (!container) return;
  const normalized = entries.length || preserveEmpty ? entries : [
    { id: globalThis.crypto?.randomUUID?.() || "leader", name: uiText("组长", "Leader"), role: "leader", model: "auto", reasoning_effort: "default", assignment: "", system_prompt: "" },
    { id: globalThis.crypto?.randomUUID?.() || "member-1", name: uiText("组员 1", "Member 1"), role: "member", model: "auto", reasoning_effort: "default", assignment: "", system_prompt: "" },
  ];
  const localizedDefaultName = (name) => {
    if (name === "组长" || name === "Leader") return uiText("组长", "Leader");
    const match = String(name || "").match(/^(?:组员|Member)\s+(\d+)$/);
    return match ? uiText(`组员 ${match[1]}`, `Member ${match[1]}`) : name;
  };
  container.innerHTML = normalized.length ? normalized.map((entry, index) => `
    <article class="discussion-team-row" data-id="${escapeHtml(entry.id || `${Date.now()}-${index}`)}" data-role="${entry.role === "leader" ? "leader" : "member"}">
      <header class="discussion-team-row-header">
        <div class="discussion-team-member-heading">
          <span class="discussion-team-member-index">${uiText(`成员 ${index + 1}`, `Participant ${index + 1}`)}</span>
          <span class="discussion-team-role-badge">${entry.role === "leader" ? uiText("组长", "Leader") : uiText("组员", "Member")}</span>
        </div>
        <button class="remove-team-member" type="button" aria-label="${uiText("移除该成员", "Remove this participant")}" title="${uiText("移除该成员", "Remove this participant")}">
          <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <span>${uiText("移除", "Remove")}</span>
        </button>
      </header>
      <div class="discussion-team-fields">
        <label><span>${uiText("名称", "Name")}</span><input class="team-member-name" maxlength="80" value="${escapeHtml(localizedDefaultName(entry.name || ""))}" placeholder="${uiText("例如：架构师", "For example: Architect")}"></label>
        <label><span>${uiText("身份", "Role")}</span><select class="team-member-role"><option value="leader" ${entry.role === "leader" ? "selected" : ""}>${uiText("组长", "Leader")}</option><option value="member" ${entry.role !== "leader" ? "selected" : ""}>${uiText("组员", "Member")}</option></select></label>
        <label><span>${uiText("模型", "Model")}</span><select class="team-member-model">${discussionTeamModelOptions(entry.model || "auto")}</select></label>
        <label><span>${uiText("思考深度", "Reasoning depth")}</span><select class="team-member-reasoning">${discussionTeamReasoningOptions(entry.model || "auto", entry.reasoning_effort || "default")}</select></label>
        <label class="team-member-assignment-field"><span>${uiText("分工", "Assignment")}</span><textarea class="team-member-assignment" maxlength="4000" placeholder="${uiText("写清楚这个成员负责什么", "Describe exactly what this participant owns")}">${escapeHtml(entry.assignment || "")}</textarea></label>
        <label class="team-member-prompt-field"><span>${uiText("该成员的系统提示词", "Participant system prompt")}</span><textarea class="team-member-prompt" maxlength="12000" placeholder="${uiText("只约束这个成员的工作方式、专长和判断标准", "Define this participant's workflow, expertise, and quality bar")}">${escapeHtml(entry.system_prompt || "")}</textarea></label>
      </div>
    </article>`).join("") : `<div class="discussion-team-empty">${uiText("当前没有讨论团配置。点击“创建讨论团”开始添加组长和组员。", "No Agent team is configured. Choose Create Agent team to add a leader and members.")}</div>`;
  container.querySelectorAll(".discussion-team-row").forEach((row) => {
    const role = row.querySelector(".team-member-role");
    const model = row.querySelector(".team-member-model");
    const reasoning = row.querySelector(".team-member-reasoning");
    model.value = normalized.find((item) => item.id === row.dataset.id)?.model || "auto";
    reasoning.value = normalized.find((item) => item.id === row.dataset.id)?.reasoning_effort || "default";
    row.querySelectorAll(".team-member-role, .team-member-model, .team-member-reasoning").forEach(enhanceDiscussionTeamSelect);
    role.onchange = () => {
      if (role.value === "leader") {
        container.querySelectorAll(".team-member-role").forEach((other) => {
          if (other !== role) {
            other.value = "member";
            syncDiscussionTeamSelectWidget(other);
          }
        });
      } else if (![...container.querySelectorAll(".team-member-role")].some((item) => item.value === "leader")) {
        role.value = "leader";
        syncDiscussionTeamSelectWidget(role);
      }
      container.querySelectorAll(".discussion-team-row").forEach((item) => {
        item.dataset.role = item.querySelector(".team-member-role").value;
      });
      rememberDiscussionTeamDraft();
    };
    model.addEventListener("change", () => refreshDiscussionTeamReasoningOptions(row));
    row.querySelectorAll("input, select, textarea").forEach((control) => {
      control.addEventListener("input", rememberDiscussionTeamDraft);
      if (control !== role) control.addEventListener("change", rememberDiscussionTeamDraft);
    });
    row.querySelector(".remove-team-member").onclick = async () => {
      if (container.children.length <= 2) {
        showWorkspaceToast(uiText("讨论团至少需要 2 人", "A discussion team needs at least two participants"), "error");
        return;
      }
      const removingLeader = role.value === "leader";
      const memberName = row.querySelector(".team-member-name")?.value.trim()
        || uiText("该成员", "this participant");
      const confirmed = await askForConfirmation(
        removingLeader
          ? uiText(`确定移除组长“${memberName}”吗？移除后，第一名组员会自动成为组长。`, `Remove leader “${memberName}”? The first remaining participant will become leader.`)
          : uiText(`确定从讨论团中移除“${memberName}”吗？`, `Remove “${memberName}” from the Agent team?`),
        {
          title: uiText("移除讨论团成员", "Remove team participant"),
          confirmLabel: uiText("确认移除", "Remove participant"),
          danger: true,
        },
      );
      if (!confirmed) return;
      row.remove();
      if (removingLeader) {
        const firstRole = container.querySelector(".team-member-role");
        if (firstRole) {
          firstRole.value = "leader";
          firstRole.dispatchEvent(new Event("change"));
        }
      }
      updateDiscussionTeamMemberCount();
      rememberDiscussionTeamDraft();
    };
  });
  const addButton = $("#addDiscussionTeamMember");
  if (addButton) {
    addButton.textContent = normalized.length
      ? uiText("＋ 添加组员", "+ Add member")
      : uiText("＋ 创建讨论团", "+ Create Agent team");
  }
  updateDiscussionTeamMemberCount();
}

$("#addDiscussionTeamMember").onclick = () => {
  const entries = collectDiscussionTeamRows();
  if (!entries.length) {
    entries.push(
      { id: globalThis.crypto?.randomUUID?.() || "leader", name: uiText("组长", "Leader"), role: "leader", model: "auto", reasoning_effort: "default", assignment: "", system_prompt: "" },
      { id: globalThis.crypto?.randomUUID?.() || "member-1", name: uiText("组员 1", "Member 1"), role: "member", model: "auto", reasoning_effort: "default", assignment: "", system_prompt: "" },
    );
  } else {
    entries.push({
      id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
      name: uiText(`组员 ${entries.length}`, `Member ${entries.length}`),
      role: "member",
      model: "auto",
      reasoning_effort: "default",
      assignment: "",
      system_prompt: "",
    });
  }
  renderDiscussionTeamRows(entries);
  rememberDiscussionTeamDraft();
  $("#discussionTeamRows .discussion-team-row:last-child .team-member-name")?.focus();
};

$("#clearDiscussionTeam").onclick = () => {
  renderDiscussionTeamRows([], { preserveEmpty: true });
  rememberDiscussionTeamDraft();
  $("#addDiscussionTeamMember").textContent = uiText("＋ 创建讨论团", "+ Create Agent team");
  showWorkspaceToast(
    uiText("讨论团已在当前草稿中清空；点击“保存设置”后生效", "The team draft is empty; choose Save settings to apply it"),
    "info",
  );
};

function renderModelProviderRows(entries = []) {
  const container = $("#modelProviderRows");
  container.innerHTML = entries.filter((entry) => entry.provider !== "deepseek").map((entry) => {
    const custom = entry.source === "custom" || Object.hasOwn(entry, "base_url");
    const protocols = custom ? { openai: "OpenAI (Chat Completions)", anthropic: "Anthropic (Messages)" } : configurableModelProviderLabels;
    return `
    <div class="model-provider-row" data-source="${custom ? "custom" : "direct"}" data-id="${escapeHtml(entry.id || "")}" data-configured="${entry.configured ? "true" : "false"}">
      <label><span>${custom ? uiText("接口协议", "API protocol") : uiText("旧版直连配置（保留）", "Legacy direct route (preserved)")}</span><select class="model-provider-kind" ${custom ? "" : "disabled"}>${Object.entries(protocols).map(([value, label]) => `<option value="${value}" ${entry.provider === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>
      ${custom ? `<label class="model-provider-url-field"><span>${uiText("第三方 API 接口网址", "Third-party API base URL")}</span><input class="model-provider-url" type="url" required maxlength="2048" value="${escapeHtml(entry.base_url || "")}" placeholder="https://your-provider.example/v1"></label>` : ""}
      <label><span>${uiText("API 模型调用名", "API model name")}</span><input class="model-provider-model" type="text" maxlength="200" value="${escapeHtml(entry.model || "")}" placeholder="${uiText("例如 gpt-5.2", "For example, gpt-5.2")}"></label>
      <label><span>${uiText("API 密钥", "API key")}</span><input class="model-provider-key" type="password" autocomplete="new-password" value="${escapeHtml(entry.api_key || "")}" placeholder="${entry.configured ? uiText("已配置 · 留空保持不变", "Configured · leave blank to keep") : uiText("输入密钥", "Enter API key")}"></label>
      <button class="remove-model-provider" type="button">${uiText("删除", "Remove")}</button>
      ${custom ? `<fieldset class="model-provider-reasoning"><legend>${uiText("勾选此接口支持的模型推理强度", "Select reasoning levels supported by this API")}</legend>${["minimal", "low", "medium", "high", "xhigh", "max"].map((level) => `<label><input type="checkbox" value="${level}" ${entry.reasoning_levels?.includes(level) ? "checked" : ""}><span>${escapeHtml(reasoningLevelLabel(level))} (${level})</span></label>`).join("")}<small>${uiText("未勾选则不发送推理强度。档位由用户声明，非实测结果。修改接口网址或协议后请重新填写密钥。", "Unchecked means no effort parameter. Levels are user-declared, not verified. Re-enter the key after changing the URL or protocol.")}</small></fieldset>` : ""}
    </div>`;
  }).join("");
  container.querySelectorAll('.model-provider-row[data-source="custom"]').forEach((row) => {
    const protocol = row.querySelector(".model-provider-kind");
    const syncLevels = () => {
      const minimal = row.querySelector('.model-provider-reasoning input[value="minimal"]');
      minimal.disabled = protocol.value === "anthropic";
      if (minimal.disabled) minimal.checked = false;
      minimal.closest("label").hidden = minimal.disabled;
    };
    syncLevels();
    protocol.addEventListener("change", syncLevels);
  });
  container.querySelectorAll(".remove-model-provider").forEach((button) => {
    button.onclick = async () => {
      const row = button.closest(".model-provider-row");
      const modelName = row?.querySelector(".model-provider-model")?.value.trim()
        || uiText("这个模型供应商", "this model provider");
      const confirmed = await askForConfirmation(
        uiText(`确定移除“${modelName}”吗？保存设置后生效。`, `Remove “${modelName}”? The change takes effect after you save settings.`),
        {
          title: uiText("移除模型供应商", "Remove model provider"),
          confirmLabel: uiText("确认移除", "Remove provider"),
          danger: true,
        },
      );
      if (!confirmed) return;
      row?.remove();
      markSettingsFormDirty();
    };
  });
}

function collectModelProviderRows(includeIncomplete = false) {
  return [...document.querySelectorAll(".model-provider-row")].map((row) => ({
    id: row.dataset.id || "",
    provider: row.querySelector(".model-provider-kind").value,
    model: row.querySelector(".model-provider-model").value.trim(),
    api_key: row.querySelector(".model-provider-key").value.trim(),
    ...(includeIncomplete ? { configured: row.dataset.configured === "true" } : {}),
    ...(row.dataset.source === "custom" ? {
      source: "custom",
      base_url: row.querySelector(".model-provider-url").value.trim(),
      reasoning_levels: [...row.querySelectorAll(".model-provider-reasoning input:checked:not(:disabled)")].map((input) => input.value),
    } : {}),
  })).filter((entry) => includeIncomplete || entry.source === "custom" || entry.model);
}

const SETTINGS_SECTION_GROUPS = {
  general: [
    ".model-setting", ".reasoning-setting", "#settingAutoOutputTokens",
    "#settingTimeout", ".cross-context-setting", ".custom-system-prompt-setting",
  ],
  team: [".discussion-team-setting"],
  providers: [".provider-keys-setting", ".aicodemirror-setting", ".model-providers-setting", ".tool-credentials-setting"],
  channels: [".feishu-setting", ".telegram-setting", ".mobile-setting"],
  voice: [".voice-setting"],
};

function initializeSettingsSections() {
  Object.entries(SETTINGS_SECTION_GROUPS).forEach(([section, selectors]) => {
    selectors.forEach((selector) => {
      const node = document.querySelector(selector);
      const item = node?.closest("#settingsForm .form-grid > *");
      if (item) item.dataset.settingsSection = section;
    });
  });
  activateSettingsSection("general");
}

function updateSettingsSectionScrollState() {
  const nav = $("#settingsSectionNav");
  const tabs = nav?.querySelector(".settings-section-tabs");
  const more = $("#settingsTabsMore");
  if (!nav || !tabs) return;
  if (nav.hidden || tabs.clientWidth <= 2) {
    if (more) more.hidden = true;
    return;
  }
  // Measure the real tab track, not the search input. A CSS grid track can be
  // wider than its child (e.g. a 240px track around a 180px search input).
  // Reclaim only the visible More track to avoid toggle/resize oscillation.
  // Both explicit grid layouts retain their gaps when that track is empty.
  const moreWidth = more && !more.hidden ? more.getBoundingClientRect().width : 0;
  const availableWithoutMore = tabs.clientWidth + moreWidth;
  const overflow = tabs.scrollWidth > availableWithoutMore + 2;
  nav.dataset.moreNext = uiText("更多 ›", "More ›");
  nav.dataset.morePrevious = uiText("‹ 更多", "‹ More");
  nav.dataset.tabsOverflow = overflow ? "true" : "false";
  nav.dataset.tabsAtStart = !overflow || tabs.scrollLeft <= 2 ? "true" : "false";
  nav.dataset.tabsAtEnd = !overflow || tabs.scrollLeft + tabs.clientWidth >= tabs.scrollWidth - 2 ? "true" : "false";
  if (more) {
    const back = nav.dataset.tabsAtEnd === "true";
    more.hidden = !overflow;
    more.textContent = back ? uiText("‹ 前面", "‹ Back") : uiText("更多 ›", "More ›");
    more.setAttribute("aria-label", back
      ? uiText("显示前面的设置分类", "Show previous settings categories")
      : uiText("显示更多设置分类", "Show more settings categories"));
  }
}

function alignActiveSettingsSectionTab() {
  const nav = $("#settingsSectionNav");
  const tabs = nav?.querySelector(".settings-section-tabs");
  const active = tabs?.querySelector("button.active[data-settings-target]");
  if (!nav || nav.hidden || !tabs || !active) {
    updateSettingsSectionScrollState();
    return;
  }
  // A desktop-to-phone resize changes the number of whole category labels that
  // fit. Keep the selected category visible instead of silently returning the
  // tab strip to its first page while a different section remains on screen.
  const tabsRect = tabs.getBoundingClientRect();
  const activeRect = active.getBoundingClientRect();
  const hasHiddenRight = tabs.scrollLeft + tabs.clientWidth < tabs.scrollWidth - 1;
  const rightFadeInset = hasHiddenRight ? 28 : 0;
  const visibleRight = tabsRect.right - rightFadeInset;
  if (activeRect.left < tabsRect.left) {
    tabs.scrollLeft -= tabsRect.left - activeRect.left;
  } else if (activeRect.right > visibleRight) {
    tabs.scrollLeft += activeRect.right - visibleRight;
  }
  requestAnimationFrame(updateSettingsSectionScrollState);
}

let settingsSectionAlignmentFrame = 0;
let settingsSectionSettleFrame = 0;

function scheduleActiveSettingsSectionAlignment() {
  // Browser chrome, media queries and dialog sizing can settle on adjacent
  // frames during a desktop-to-phone transition. Align once after each stage
  // so the selected category remains visible in native and emulated viewports.
  // Window and visualViewport can emit together, so retain only the latest
  // two-frame pass instead of multiplying layout work during continuous resize.
  if (settingsSectionAlignmentFrame) cancelAnimationFrame(settingsSectionAlignmentFrame);
  if (settingsSectionSettleFrame) cancelAnimationFrame(settingsSectionSettleFrame);
  settingsSectionAlignmentFrame = requestAnimationFrame(() => {
    settingsSectionAlignmentFrame = 0;
    alignActiveSettingsSectionTab();
    settingsSectionSettleFrame = requestAnimationFrame(() => {
      settingsSectionSettleFrame = 0;
      alignActiveSettingsSectionTab();
    });
  });
}

function activateSettingsSection(section) {
  const target = ["general", "team", "providers", "channels", "voice", "runtime"].includes(section)
    ? section : "general";
  const panel = $("#panel-settings");
  if (!panel) return;
  const form = $("#settingsForm");
  if (form) clearSettingsSearchSubfilters(form);
  panel.classList.remove("settings-searching");
  panel.dataset.lastSettingsSection = target;
  panel.dataset.settingsSection = target;
  const search = $("#settingsSearch");
  if (search) search.value = "";
  $("#settingsSearchEmpty")?.setAttribute("hidden", "");
  $("#runtimeCards")?.classList.remove("settings-search-match");
  document.querySelectorAll("#settingsSectionNav button").forEach((button) => {
    const active = button.dataset.settingsTarget === target;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
    if (active) button.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
  requestAnimationFrame(updateSettingsSectionScrollState);
  panel.querySelectorAll("#settingsForm .form-grid > [data-settings-section]").forEach((item) => {
    item.classList.toggle("settings-section-hidden", item.dataset.settingsSection !== target);
  });
  $("#settingsForm").classList.toggle("settings-runtime-active", target === "runtime");
}

function clearSettingsSearchSubfilters(form) {
  form.querySelectorAll(".settings-search-subhidden").forEach((node) => node.classList.remove("settings-search-subhidden"));
  form.querySelectorAll(".settings-search-hit").forEach((node) => node.classList.remove("settings-search-hit"));
  form.querySelectorAll("details[data-search-opened='true']").forEach((details) => {
    details.open = false;
    delete details.dataset.searchOpened;
  });
}

function settingsSearchText(node) {
  if (!node) return "";
  const controls = [...node.querySelectorAll("input,textarea,select")]
    .map((control) => `${control.getAttribute("placeholder") || ""} ${control.getAttribute("aria-label") || ""}`)
    .join(" ");
  return `${node.textContent || ""} ${controls}`.toLocaleLowerCase(isEnglish() ? "en" : "zh-CN");
}

function filterToolCredentialSearch(item, query) {
  const directMetadata = [
    item.querySelector(":scope > span")?.textContent || "",
    ...[...item.querySelectorAll(":scope > small")].map((node) => node.textContent || ""),
  ].join(" ").toLocaleLowerCase(isEnglish() ? "en" : "zh-CN");
  const broadMatch = directMetadata.includes(query);
  const directLabels = [...item.querySelectorAll(":scope > label")];
  const details = item.querySelector(":scope > details");
  const nestedLabels = details ? [...details.querySelectorAll("label")] : [];
  let labelMatches = 0;
  [...directLabels, ...nestedLabels].forEach((label) => {
    const matched = broadMatch || settingsSearchText(label).includes(query);
    label.classList.toggle("settings-search-subhidden", !matched);
    label.classList.toggle("settings-search-hit", matched && !broadMatch);
    if (matched) labelMatches += 1;
  });
  if (details) {
    const nestedMatch = broadMatch || nestedLabels.some((label) => !label.classList.contains("settings-search-subhidden"));
    details.classList.toggle("settings-search-subhidden", !nestedMatch);
    if (nestedMatch && !broadMatch && !details.open) {
      details.open = true;
      details.dataset.searchOpened = "true";
    }
  }
  return broadMatch || labelMatches > 0;
}

function applySettingsSearch() {
  const search = $("#settingsSearch");
  const panel = $("#panel-settings");
  const form = $("#settingsForm");
  if (!search || !panel || !form) return;
  const query = search.value.trim().toLocaleLowerCase(isEnglish() ? "en" : "zh-CN");
  clearSettingsSearchSubfilters(form);
  if (!query) {
    activateSettingsSection(panel.dataset.lastSettingsSection || "general");
    search.focus({ preventScroll: true });
    return;
  }

  panel.classList.add("settings-searching");
  panel.dataset.settingsSection = "search";
  form.classList.remove("settings-runtime-active");
  let matches = 0;
  form.querySelectorAll(".form-grid > [data-settings-section]").forEach((item) => {
    const matched = item.classList.contains("tool-credentials-setting")
      ? filterToolCredentialSearch(item, query)
      : settingsSearchText(item).includes(query);
    item.classList.toggle("settings-section-hidden", !matched);
    if (matched) matches += 1;
  });
  const runtime = $("#runtimeCards");
  const runtimeMatched = Boolean(runtime?.textContent?.toLocaleLowerCase(isEnglish() ? "en" : "zh-CN").includes(query));
  runtime?.classList.toggle("settings-search-match", runtimeMatched);
  if (runtimeMatched) matches += 1;
  document.querySelectorAll("#settingsSectionNav button[data-settings-target]").forEach((button) => {
    button.classList.remove("active");
    button.setAttribute("aria-current", "false");
  });
  const empty = $("#settingsSearchEmpty");
  if (empty) empty.hidden = matches > 0;
}

function populateSettingVoiceNames(selected = "") {
  const select = $("#settingVoiceName");
  if (!select) return;
  const voices = window.speechSynthesis?.getVoices?.() || [];
  const neuralVoices = [
    { name: "zh-CN-XiaoxiaoNeural", label: uiText("晓晓 · 中文女声", "Xiaoxiao · Chinese female") },
    { name: "zh-CN-YunxiNeural", label: uiText("云希 · 中文男声", "Yunxi · Chinese male") },
    { name: "en-US-AvaNeural", label: "Ava · English female" },
    { name: "en-US-AndrewNeural", label: "Andrew · English male" },
    { name: "ja-JP-NanamiNeural", label: "Nanami · Japanese" },
    { name: "ko-KR-SunHiNeural", label: "SunHi · Korean" },
    { name: "fr-FR-DeniseNeural", label: "Denise · French" },
    { name: "de-DE-KatjaNeural", label: "Katja · German" },
    { name: "es-ES-ElviraNeural", label: "Elvira · Spanish" },
    { name: "it-IT-ElsaNeural", label: "Elsa · Italian" },
    { name: "pt-BR-FranciscaNeural", label: "Francisca · Portuguese" },
    { name: "ru-RU-SvetlanaNeural", label: "Svetlana · Russian" },
    { name: "ar-SA-ZariyahNeural", label: "Zariyah · Arabic" },
    { name: "hi-IN-SwaraNeural", label: "Swara · Hindi" },
    { name: "th-TH-PremwadeeNeural", label: "Premwadee · Thai" },
    { name: "vi-VN-HoaiMyNeural", label: "HoaiMy · Vietnamese" },
    { name: "id-ID-GadisNeural", label: "Gadis · Indonesian" },
    { name: "ms-MY-YasminNeural", label: "Yasmin · Malay" },
    { name: "tr-TR-EmelNeural", label: "Emel · Turkish" },
    { name: "pl-PL-ZofiaNeural", label: "Zofia · Polish" },
    { name: "nl-NL-ColetteNeural", label: "Colette · Dutch" },
    { name: "uk-UA-PolinaNeural", label: "Polina · Ukrainian" },
  ];
  select.replaceChildren();
  const automatic = document.createElement("option");
  automatic.value = "";
  automatic.textContent = uiText("自动选择最佳可用声音", "Automatically choose the best available voice");
  select.append(automatic);
  voices.forEach((voice) => {
    const option = document.createElement("option");
    option.value = voice.name;
    option.textContent = `${voice.name} · ${voice.lang}${voice.localService ? ` · ${uiText("本机", "local")}` : ""}`;
    select.append(option);
  });
  neuralVoices.forEach((voice) => {
    const option = document.createElement("option");
    option.value = voice.name;
    option.textContent = `${voice.label} · ${uiText("联网自然语音", "online neural")}`;
    select.append(option);
  });
  if (selected && !voices.some((voice) => voice.name === selected) && !neuralVoices.some((voice) => voice.name === selected)) {
    const unavailable = document.createElement("option");
    unavailable.value = selected;
    unavailable.textContent = `${selected} · ${uiText("当前不可用", "currently unavailable")}`;
    select.append(unavailable);
  }
  select.value = selected || "";
}

$("#addModelProvider").onclick = () => {
  const existing = collectModelProviderRows(true);
  existing.push({ id: globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`, source: "custom", provider: "openai", base_url: "", reasoning_levels: [], model: "", configured: false });
  renderModelProviderRows(existing);
  markSettingsFormDirty();
  $("#modelProviderRows .model-provider-row:last-child .model-provider-model")?.focus();
};

function renderMobileDevices(status = {}) {
  const devices = Array.isArray(status.devices) ? status.devices : [];
  const connected = devices.filter((device) => device.connected).length;
  const summary = $("#mobileSettingSummary");
  const list = $("#mobileDeviceList");
  if (summary) {
    summary.textContent = devices.length
      ? uiText(`${devices.length} 台已绑定 · ${connected} 台在线`, `${devices.length} paired · ${connected} online`)
      : uiText("尚未绑定手机", "No phone is paired");
  }
  if (!list) return;
  if (!devices.length) {
    list.innerHTML = `<small>${uiText("安装 Elren 后，点击上方按钮并扫描二维码。", "Install Elren, then use the button above to scan a pairing QR code.")}</small>`;
    return;
  }
  list.innerHTML = devices.map((device) => `
    <article class="mobile-device-card">
      <span class="mobile-device-state ${device.connected ? "online" : ""}" aria-hidden="true"></span>
      <div><b>${escapeHtml(device.name || uiText("Android 手机", "Android phone"))}</b><small>${device.connected ? uiText("在线", "Online") : uiText("离线", "Offline")}${device.android_version ? ` · Android ${escapeHtml(device.android_version)}` : ""}</small></div>
      <button type="button" data-mobile-revoke="${escapeHtml(device.device_id)}">${uiText("解除绑定", "Unpair")}</button>
    </article>`).join("");
  list.querySelectorAll("[data-mobile-revoke]").forEach((button, index) => {
    const device = devices[index] || {};
    const card = button.closest("article");
    const state = card?.querySelector("small");
    if (state) {
      state.textContent = `${device.connected ? uiText("在线", "Online") : uiText("离线", "Offline")}${device.android_version ? ` · Android ${device.android_version}` : ""}`;
    }
    button.textContent = uiText("解除绑定", "Unpair");
    button.onclick = async () => {
      const deviceName = device.name || uiText("这台 Android 手机", "this Android phone");
      const confirmed = await askForConfirmation(
        uiText(
          `确定解除“${deviceName}”的绑定吗？解除后，该手机将无法继续接收任务，重新使用时需要再次扫码绑定。`,
          `Unpair “${deviceName}”? The phone will no longer receive tasks and must scan a new QR code before it can be used again.`,
        ),
        {
          title: uiText("解除手机绑定", "Unpair phone"),
          confirmLabel: uiText("解除绑定", "Unpair"),
          danger: true,
        },
      );
      if (!confirmed) return;
      button.disabled = true;
      button.textContent = uiText("正在解除…", "Unpairing…");
      try {
        await api(`/api/mobile/devices/${encodeURIComponent(button.dataset.mobileRevoke)}`, { method: "DELETE" });
        showWorkspaceToast(uiText("手机已解除绑定", "Phone unpaired"), "info");
        await loadMobileDevices();
      } catch (error) {
        button.disabled = false;
        button.textContent = uiText("解除绑定", "Unpair");
        showWorkspaceToast(`${uiText("解除绑定失败：", "Could not unpair: ")}${localizeKnownSystemMessage(error.message)}`, "error");
      }
    };
  });
}

async function loadMobileDevices() {
  try {
    const status = await api("/api/mobile/status");
    renderMobileDevices(status);
    return status;
  } catch (error) {
    const list = $("#mobileDeviceList");
    if (list) list.innerHTML = `<small class="panel-error">${escapeHtml(localizeKnownSystemMessage(error.message))}</small>`;
    return null;
  }
}

function closeMobilePairingDialog() {
  clearTimeout(mobilePairingPollTimer);
  mobilePairingPollTimer = null;
  const dialog = $("#mobilePairingDialog");
  if (dialog?.open) dialog.close();
}

async function createMobilePairing() {
  const dialog = $("#mobilePairingDialog");
  const content = $("#mobilePairingContent");
  if (!dialog || !content) return;
  clearTimeout(mobilePairingPollTimer);
  mobilePairingPollTimer = null;
  content.innerHTML = `<div class="panel-empty">${uiText("正在创建一次性绑定码…", "Creating a one-time pairing code…")}</div>`;
  dialog.showModal();
  try {
    const pairing = await api("/api/mobile/pairing", { method: "POST" });
    const expiresAt = new Date(Number(pairing.expires_at) * 1000);
    content.innerHTML = `
      <img class="mobile-pairing-qr" src="${escapeHtml(pairing.qr_url)}?t=${Date.now()}" alt="${uiText("Android 手机绑定二维码", "Android phone pairing QR code")}">
      <div class="mobile-pairing-copy">
        <strong>${uiText("用手机端 Elren 扫描", "Scan with Elren on your phone")}</strong>
        <p>${uiText("确保手机与电脑连接同一局域网，然后在手机端点击“扫描电脑”。", "Connect the phone and PC to the same local network, then tap “Scan computer” on the phone.")}</p>
        <small>${uiText("局域网地址：", "Local endpoint: ")}${escapeHtml(pairing.endpoint)}<br>${uiText("有效期至：", "Expires: ")}${escapeHtml(formatUiDateTime(expiresAt, { hour: "2-digit", minute: "2-digit", second: "2-digit" }))}</small>
      </div>`;
    const initiallyPaired = Number((await loadMobileDevices())?.paired || 0);
    const pollPairing = async () => {
      const status = await loadMobileDevices();
      if (!dialog.open) return;
      if (status && Number(status.paired || 0) > initiallyPaired) {
        mobilePairingPollTimer = null;
        content.innerHTML = `<div class="mobile-pairing-success"><b>${uiText("手机绑定成功", "Phone paired")}</b><p>${uiText("加密连接已建立。你可以关闭此窗口。", "The encrypted connection is ready. You can close this window.")}</p></div>`;
        return;
      }
      mobilePairingPollTimer = setTimeout(pollPairing, 1500);
    };
    mobilePairingPollTimer = setTimeout(pollPairing, 1500);
  } catch (error) {
    content.innerHTML = `<div class="panel-error">${escapeHtml(localizeKnownSystemMessage(error.message))}</div>`;
  }
}

function updateOutputTokenHelp() {
  const input = $("#settingMaxOutputTokens");
  const help = input?.closest(".settings-field")?.querySelector("small");
  if (!help) return;
  help.textContent = uiText(
    "包含隐藏推理、正文和工具调用。自动模式使用当前所选模型及供应商公布的输出上限；回复仍可能提前结束。",
    "Includes hidden reasoning, answer text, and tool calls. Automatic mode uses the selected model and provider's published output limit; a response may still finish earlier.",
  );
}

async function loadSettings() {
  if (settingsFormDirty) return;
  const cards = $("#runtimeCards");
  const requestGeneration = ++settingsRequestGeneration;
  setSettingsFormHydrating(true);
  settingsFormBaseline = null;
  settingsPreferenceBaseline = null;
  settingReasoningDirty = false;
  clearTimeout(settingsRetryTimer);
  settingsRetryTimer = null;
  try {
    let request = requestSettingsSnapshot();
    let settings = await request;
    // A newer prefetch may supersede this read without opening a new Settings
    // visit. Follow its authoritative snapshot so the whole form still loads;
    // merely dropping this callback would leave non-model controls unhydrated.
    while (requestGeneration === settingsRequestGeneration
        && settingsSnapshotPromise && settingsSnapshotPromise !== request) {
      request = settingsSnapshotPromise;
      settings = await request;
    }
    if (requestGeneration !== settingsRequestGeneration) return;
    if (settingsSnapshotPromise !== request) return;
    settingsRetryDelay = 1000;
    settingsApiVersion = Number(settings.settings_api_version || 0);
    cacheSettingsDisplay(settings);
    hydrateSettingsPreferences(settings);
    const savedTeam = settings.discussion_team || [];
    const recoverableDraft = readDiscussionTeamDraft();
    const hasRecoverableDraft = discussionTeamDraftDiffers(recoverableDraft, savedTeam);
    const teamToRender = hasRecoverableDraft ? recoverableDraft : savedTeam;
    if (recoverableDraft && !hasRecoverableDraft) clearDiscussionTeamDraft();
    renderDiscussionTeamRows(teamToRender, {
      preserveEmpty: !teamToRender.length,
    });
    enhanceAllSettingsSelects();
    setSettingsFormHydrating(false);
    discussionTeamDirty = false;
    recoveredDiscussionTeamDraftPending = hasRecoverableDraft;
    if (hasRecoverableDraft) {
      // A recovered draft predates this Settings visit. Showing it must not
      // pretend that the user changed something in the current session. Keep
      // the draft in localStorage, baseline the visible values, and only show
      // the discard warning after a real edit. Saving still applies the draft.
      rememberSettingsFormBaseline();
      setSettingsFormDirty(false, { announce: false });
      showRecoveredDiscussionTeamDraftHint();
    } else {
      rememberSettingsFormBaseline();
      setSettingsFormDirty(false, { announce: false });
      showSettingsCleanHint();
    }
    const activationWarning = settingsActivationWarningText(settings, settingsFormDirty);
    if (activationWarning) {
      $("#settingsSaveState").className = "settings-save-state warning";
      $("#settingsSaveState").textContent = activationWarning;
    }
    if (!latestStatus) await loadStatus();
    const status = latestStatus || {};
    const mcpToolCount = status.mcp?.tool_count;
    const mcpReadyDetail = mcpToolCount == null
      ? `stdio · ${status.mcp?.protocol || "ready"}`
      : `${mcpToolCount} real stdio tools · ${status.mcp?.protocol || "ready"}`;
    const mcpReadyDetailZh = mcpToolCount == null
      ? `stdio · ${status.mcp?.protocol || "已就绪"}`
      : `${mcpToolCount} 个真实 stdio 工具 · ${status.mcp?.protocol || "已就绪"}`;
    const contextTokens = Number(settings.context_window_tokens || 0);
    const contextSource = String(settings.context_window_source || "");
    const contextLimit = contextTokens
      ? contextTokens.toLocaleString(isEnglish() ? "en-US" : "zh-CN")
      : "—";
    const contextDetail = contextSource === "official_model_spec"
      ? (isEnglish()
        ? `${contextLimit} tokens · official specification for this model`
        : `${contextLimit} token · 该模型官方规格`)
      : contextSource === "built_in_model_limit"
      ? (isEnglish()
        ? `${contextLimit} tokens · built-in model limit`
        : `${contextLimit} token · 内置模型上限`)
      : (isEnglish()
        ? `${contextLimit} tokens · conservative fallback; provider does not publish a model limit`
        : `${contextLimit} token · 供应商未公布该模型上限，采用保守回退值`);
    const runtimes = isEnglish() ? [
      ["Model", true, modelDisplayName(settings.model, settings.available_models || [])],
      ["Context window", true, contextDetail],
      ["Model providers", Number(settings.model_provider_count || 0) > 0 || settings.primary_key_configured, `${settings.available_models?.length || 0} configured model choices`],
      ["Tools & plugins", status.plugin_health?.ready !== false, `${status.plugin_health?.loaded ?? status.tools?.length ?? 0} active · ${status.plugin_health?.skipped ?? 0} skipped`],
      ["Workspace", true, settings.workspace],
      ["Portable runtime", status.runtime_bundle?.ready, status.runtime_bundle?.ready
        ? `Python ${status.runtime_bundle.components?.python?.version || "?"} · Node.js ${status.runtime_bundle.components?.node?.version || "?"} · OpenClaw ${status.runtime_bundle.components?.openclaw?.version || "?"}`
        : "Package runtime unavailable · compatible system fallback will be used"],
      ["Portable tools", status.runtime_bundle?.components?.tool_runtime?.ready,
        status.runtime_bundle?.components?.tool_runtime?.ready
          ? `${status.runtime_bundle.components.tool_runtime.available_tool_count || 0}/${status.runtime_bundle.components.tool_runtime.tool_count || 0} ready · Docker is not required`
          : "Tool layer unavailable · compatible host commands will be used when present"],
      ["MCP", status.mcp?.ready, status.mcp?.ready ? mcpReadyDetail : localizeKnownSystemMessage(status.mcp?.last_error) || "Not ready"],
      ["Windows sandbox", status.sandbox?.available, status.sandbox?.available ? "Job Object · process tree / memory / CPU / timeout" : "Unavailable"],
      ["OpenClaw", status.openclaw?.gateway_ready, openClawStatusText(status.openclaw).replace(/^OpenClaw:\s*/, "")],
      ["OpenClaw provider key", settings.openclaw_deepseek_key_configured, settings.openclaw_deepseek_key_configured ? "Configured (hidden)" : "Not configured"],
      ["Feishu", status.feishu?.ready, status.feishu?.configured ? (localizeKnownSystemMessage(status.feishu.last_error) || "Configured; waiting for events or outgoing messages") : "app_id/app_secret not configured"],
      ["Telegram", status.telegram?.ready, status.telegram?.configured ? (localizeKnownSystemMessage(status.telegram.last_error) || "Configured; polling for messages") : "bot token not configured"],
      ["Android companion", status.mobile?.connected > 0, `${status.mobile?.paired || 0} paired · ${status.mobile?.connected || 0} online`],
      ["Public web access", true, "All public domains · private-network protection enabled"],
      ["Scheduled tasks", true, `${status.scheduler?.total ?? 0} tasks · ${status.scheduler?.runs ?? 0} runs`],
      ["Vision", status.vision?.ready, status.vision?.ready
        ? `${status.vision.model} · ${status.vision.source}`
        : "No semantic vision endpoint; local OCR and UIA control recognition remain available"],
      ["Primary key", settings.primary_key_configured, settings.primary_key_configured ? "Configured (hidden)" : "Not configured"],
      ["Backup key", settings.backup_key_configured, settings.backup_key_configured ? "Configured (hidden)" : "Not configured"],
    ] : [
      ["模型", true, modelDisplayName(settings.model, settings.available_models || [])],
      ["上下文窗口", true, contextDetail],
      ["模型供应商", Number(settings.model_provider_count || 0) > 0 || settings.primary_key_configured, `${settings.available_models?.length || 0} 个已配置模型选项`],
      ["工具与插件", status.plugin_health?.ready !== false, `${status.plugin_health?.loaded ?? status.tools?.length ?? 0} 个可用 · ${status.plugin_health?.skipped ?? 0} 个已隔离`],
      ["工作区", true, settings.workspace],
      ["便携运行时", status.runtime_bundle?.ready, status.runtime_bundle?.ready
        ? `Python ${status.runtime_bundle.components?.python?.version || "?"} · Node.js ${status.runtime_bundle.components?.node?.version || "?"} · OpenClaw ${status.runtime_bundle.components?.openclaw?.version || "?"}`
        : "包内运行时不可用，将回退到电脑兼容版本"],
      ["便携工具层", status.runtime_bundle?.components?.tool_runtime?.ready,
        status.runtime_bundle?.components?.tool_runtime?.ready
          ? `${status.runtime_bundle.components.tool_runtime.available_tool_count || 0}/${status.runtime_bundle.components.tool_runtime.tool_count || 0} 个工具就绪 · 无需安装 Docker`
          : "工具层不可用；存在兼容的电脑命令时自动回退"],
      ["MCP", status.mcp?.ready, status.mcp?.ready ? mcpReadyDetailZh : status.mcp?.last_error || "未就绪"],
      ["Windows 沙箱", status.sandbox?.available, status.sandbox?.available ? "Job Object · 进程树/内存/CPU/超时" : "不可用"],
      ["OpenClaw", status.openclaw?.gateway_ready, openClawStatusText(status.openclaw).replace(/^OpenClaw：/, "")],
      ["OpenClaw 模型密钥", settings.openclaw_deepseek_key_configured, settings.openclaw_deepseek_key_configured ? "已配置（已隐藏）" : "未配置"],
      ["飞书", status.feishu?.ready, status.feishu?.configured ? (status.feishu.last_error || "已配置，等待事件或发送消息") : "未配置 app_id/app_secret"],
      ["Telegram", status.telegram?.ready, status.telegram?.configured ? (status.telegram.last_error || "已配置，正在轮询消息") : "未配置 Bot Token"],
      ["Android 手机", status.mobile?.connected > 0, `${status.mobile?.paired || 0} 台已绑定 · ${status.mobile?.connected || 0} 台在线`],
      ["公网访问", true, "全部公网域名 · 私网防护已启用"],
      ["定时任务", true, `${status.scheduler?.total ?? 0} 个任务 · ${status.scheduler?.runs ?? 0} 次执行`],
      ["视觉", status.vision?.ready, status.vision?.ready
        ? `${status.vision.model} · ${status.vision.source}`
        : "未发现语义视觉端点，本机 OCR 与 UIA 控件识别仍可用"],
      ["主密钥", settings.primary_key_configured, settings.primary_key_configured ? "已配置（已隐藏）" : "未配置"],
      ["备用密钥", settings.backup_key_configured, settings.backup_key_configured ? "已配置（已隐藏）" : "未配置"],
    ];
    cards.innerHTML = runtimes.map(([name, ready, detail]) => `
      <div class="runtime-card"><span class="runtime-light ${ready ? "ready" : ""}"></span><div><b>${escapeHtml(name)}</b><small>${escapeHtml(detail)}</small></div></div>`).join("");
    renderVisionSettings(status.vision);
    await loadMobileDevices();
    if (isEnglish()) translateSettingsDynamic();
  } catch (error) {
    if (requestGeneration !== settingsRequestGeneration) return;
    const networkFailure = error instanceof TypeError
      || /failed to fetch|networkerror|load failed/i.test(String(error.message || ""));
    const detail = networkFailure
      ? uiText(
        "本地服务正在重新连接，连接恢复后会自动载入设置。",
        "The local service is reconnecting. Settings will load automatically when it is available.",
      )
      : `${uiText("设置读取失败：", "Failed to load settings: ")}${localizeKnownSystemMessage(error.message)}`;
    cards.innerHTML = `
      <div class="panel-error settings-reconnect" role="status">
        <span>${escapeHtml(detail)}</span>
        <button id="retrySettings" type="button">${uiText("立即重试", "Retry now")}</button>
      </div>`;
    $("#retrySettings")?.addEventListener("click", loadSettings, { once: true });
    settingsRetryTimer = setTimeout(() => {
      const dialog = $("#workspaceDialog");
      const panel = $("#panel-settings");
      if (dialog?.open && panel?.classList.contains("active")) loadSettings();
    }, settingsRetryDelay);
    settingsRetryDelay = Math.min(settingsRetryDelay * 2, 10000);
  } finally {
    if (requestGeneration === settingsRequestGeneration) setSettingsFormHydrating(false);
  }
}

$("#openSettings").onclick = () => openWorkspacePanel("settings");
$("#pairMobileDevice").onclick = createMobilePairing;
$("#closeMobilePairing").onclick = closeMobilePairingDialog;
$("#mobilePairingDialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeMobilePairingDialog();
});
$("#mobilePairingDialog").addEventListener("click", (event) => {
  if (event.target === $("#mobilePairingDialog")) closeMobilePairingDialog();
});
$("#closeWorkspaceDialog").onclick = requestCloseWorkspaceDialog;
$("#workspaceDialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  requestCloseWorkspaceDialog();
});
$("#refreshArtifacts").onclick = loadArtifacts;
$("#artifactSearch").addEventListener("input", renderArtifacts);
$("#artifactType").addEventListener("change", renderArtifacts);
const workspaceTabButtons = [...document.querySelectorAll(".workspace-tabs button")];
workspaceTabButtons.forEach((button, index) => {
  button.onclick = () => {
    openWorkspacePanel(button.dataset.panel);
    button.focus({ preventScroll: true });
  };
  button.addEventListener("keydown", (event) => {
    const lastIndex = workspaceTabButtons.length - 1;
    const nextIndex = {
      ArrowLeft: index === 0 ? lastIndex : index - 1,
      ArrowRight: index === lastIndex ? 0 : index + 1,
      Home: 0,
      End: lastIndex,
    }[event.key];
    if (nextIndex === undefined) return;
    event.preventDefault();
    const nextButton = workspaceTabButtons[nextIndex];
    openWorkspacePanel(nextButton.dataset.panel);
    nextButton.focus({ preventScroll: true });
  });
});
const settingsSectionButtons = [...document.querySelectorAll("#settingsSectionNav button[data-settings-target]")];
settingsSectionButtons.forEach((button, index) => {
  button.onclick = () => activateSettingsSection(button.dataset.settingsTarget);
  button.addEventListener("keydown", (event) => {
    const lastIndex = settingsSectionButtons.length - 1;
    const nextIndex = {
      ArrowLeft: index === 0 ? lastIndex : index - 1,
      ArrowRight: index === lastIndex ? 0 : index + 1,
      Home: 0,
      End: lastIndex,
    }[event.key];
    if (nextIndex === undefined) return;
    event.preventDefault();
    const nextButton = settingsSectionButtons[nextIndex];
    activateSettingsSection(nextButton.dataset.settingsTarget);
    nextButton.focus({ preventScroll: true });
  });
});
$("#settingsSectionNav .settings-section-tabs")?.addEventListener("scroll", updateSettingsSectionScrollState, { passive: true });
$("#settingsTabsMore")?.addEventListener("click", () => {
  const nav = $("#settingsSectionNav");
  const tabs = nav?.querySelector(".settings-section-tabs");
  if (!tabs) return;
  const direction = nav.dataset.tabsAtEnd === "true" ? -1 : 1;
  tabs.scrollBy({ left: direction * Math.max(1, tabs.clientWidth), behavior: "smooth" });
});
window.addEventListener("resize", scheduleActiveSettingsSectionAlignment);
window.visualViewport?.addEventListener?.("resize", scheduleActiveSettingsSectionAlignment);
$("#settingsSearch")?.addEventListener("input", applySettingsSearch);
$("#settingsSearch")?.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  event.preventDefault();
  event.currentTarget.value = "";
  applySettingsSearch();
});
window.speechSynthesis?.addEventListener?.("voiceschanged", () => {
  populateSettingVoiceNames($("#settingVoiceName")?.value || "");
});
$("#settingAutoOutputTokens").onchange = () => {
  $("#settingMaxOutputTokens").disabled = $("#settingAutoOutputTokens").checked;
};
$("#settingModel").addEventListener("change", () => {
  settingModelDirty = true;
  settingReasoningDirty = true;
  syncDefaultReasoningOptions($("#settingModel").value);
  updateOutputTokenHelp();
});
$("#settingReasoningEffort").addEventListener("change", () => {
  settingReasoningDirty = true;
});
$("#settingsForm").addEventListener("input", (event) => {
  if (event.target.closest?.("#discussionTeamRows")) return;
  markSettingsFormDirty();
});
$("#settingsForm").addEventListener("change", markSettingsFormDirty);
$("#workspaceDialog").addEventListener("close", () => {
  if (settingsFormDirty) return;
  settingModelDirty = false;
  settingReasoningDirty = false;
});
$("#scheduleKind").onchange = updateScheduleFields;
function scheduleFormSignature() {
  return JSON.stringify([
    "#scheduleName", "#schedulePrompt", "#scheduleKind", "#scheduleStartAt",
    "#scheduleEndAt", "#scheduleIntervalValue", "#scheduleIntervalUnit",
  ].map((selector) => $(selector)?.value || ""));
}

async function createSchedule(event) {
  event.preventDefault();
  if (scheduleCreatePending) return;
  const button = $("#scheduleForm button[type='submit']");
  scheduleCreatePending = true;
  button.disabled = true;
  const submittedForm = scheduleFormSignature();
  try {
    const kind = $("#scheduleKind").value;
    const startAt = parseScheduleDateTime($("#scheduleStartAt").value);
    const endValue = kind === "at" ? "" : $("#scheduleEndAt").value;
    const endAt = endValue ? parseScheduleDateTime(endValue) : null;
    const intervalSeconds = Number($("#scheduleIntervalValue").value) * Number($("#scheduleIntervalUnit").value);
    if (!startAt) throw new Error(uiText("请选择有效的开始时间", "Choose a valid start time"));
    if (endValue && (!endAt || endAt <= startAt)) {
      throw new Error(uiText("结束时间必须晚于开始时间", "End time must be after the start time"));
    }
    await api("/api/schedules", {
      method: "POST",
      body: JSON.stringify({
        name: $("#scheduleName").value,
        prompt: $("#schedulePrompt").value,
        kind,
        expression: kind === "interval" ? String(intervalSeconds) : kind === "daily" ? "86400" : startAt.toISOString(),
        start_at: startAt.toISOString(),
        end_at: endAt ? endAt.toISOString() : null,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        policy: "autonomous",
        agent_profile: "general",
      }),
    });
    // A user may already be writing the next schedule while this POST runs.
    if (scheduleFormSignature() === submittedForm) resetScheduleForm();
    await loadSchedules();
  } catch (error) {
    showWorkspaceToast(`${uiText("创建失败：", "Creation failed: ")}${localizeKnownSystemMessage(error.message)}`, "error");
  } finally {
    scheduleCreatePending = false;
    button.disabled = false;
  }
}
$("#scheduleForm").onsubmit = createSchedule;
async function saveSettings(event) {
  event.preventDefault();
  if (settingsSavePending) return;
  settingsSavePending = true;
  const requestGeneration = settingsRequestGeneration;
  const state = $("#settingsSaveState");
  const button = $("#saveSettings");
  state.className = "settings-save-state saving";
  state.textContent = uiText("正在保存设置…", "Saving settings…");
  button.disabled = true;
  button.textContent = uiText("正在保存…", "Saving…");
  try {
    if (settingsApiVersion < 2) {
      throw new Error(uiText(
        "当前页面与后台服务版本不一致。你的讨论团草稿已保留，请重新启动 Elren 后再保存。",
        "The page and background service versions do not match. Your Agent team draft is preserved; restart Elren, then save again.",
      ));
    }
    if (settingsFormHydrating || !settingsPreferenceBaseline) {
      throw new Error(uiText("设置尚未完整读取，请读取完成后再保存。", "Settings have not finished loading. Wait for loading to complete before saving."));
    }
    const providerKeys = {};
    const keyFields = providerCredentialFields();
    keyFields.forEach(([selector, field]) => {
      const value = $(selector).value.trim();
      if (value) providerKeys[field] = value;
    });
    const submittedValues = settingsPreferenceValues();
    const patch = { ...changedSettingsPreferences(submittedValues, settingsPreferenceBaseline), ...providerKeys };
    const submittedDiscussionTeam = patch.discussion_team;
    // Pending reads started before this write cannot describe its outcome.
    // Invalidate before awaiting the PATCH, not merely after its acknowledgement.
    settingsSnapshotPromise = null;
    settingsSnapshotStartedAt = 0;
    const updated = await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    if (Number(updated.settings_api_version || 0) < 2
      || !Array.isArray(updated.discussion_team)
      || (submittedDiscussionTeam && (updated.discussion_team.length !== submittedDiscussionTeam.length
        || submittedDiscussionTeam.some((member, index) => updated.discussion_team[index]?.id !== member.id)))) {
      throw new Error(uiText(
        "后台没有完整保存讨论团配置。草稿已安全保留，请重新启动 Elren 后重试。",
        "The background service did not persist the complete Agent team. The draft is preserved; restart Elren and try again.",
      ));
    }
    settingsApiVersion = Number(updated.settings_api_version || 0);
    let confirmed = updated;
    let refreshWarning = "";
    if (settingsSnapshotPromise) {
      // A read made while PATCH was pending may be either pre-commit or newer
      // than its delayed reply. Without a server revision, do not guess which:
      // request a new authoritative snapshot after acknowledgement.
      settingsSnapshotPromise = null;
      settingsSnapshotStartedAt = 0;
      try {
        const refreshed = await requestSettingsSnapshot();
        if (Number(refreshed.settings_api_version || 0) < 2 || !Array.isArray(refreshed.discussion_team)) throw new Error("Invalid settings snapshot");
        confirmed = { ...refreshed, activation_warnings: updated.activation_warnings };
      } catch {
        refreshWarning = uiText("设置已保存，但最新状态暂时无法刷新；请稍后重新打开设置核对。", "Settings were saved, but the latest state could not be refreshed; reopen Settings later to verify it.");
      }
    }
    cacheSettingsDisplay(confirmed);
    // The acknowledged response is the current snapshot. In-flight reads from
    // a reopened visit follow it instead of painting pre-commit values later.
    settingsSnapshotPromise = Promise.resolve(confirmed);
    settingsSnapshotStartedAt = Date.now();
    const reconciled = reconcileSettingsSave(confirmed, submittedValues, providerKeys, requestGeneration);
    window.dispatchEvent(new CustomEvent("elren:voice-settings-updated", { detail: confirmed }));
    if (!reconciled) {
      showWorkspaceToast(settingsActivationWarningText(confirmed) || refreshWarning || uiText("先前提交的设置已保存；重新打开设置即可查看。", "Your earlier settings submission was saved; reopen Settings to view it."), "info");
      return;
    }
    const savedAt = new Intl.DateTimeFormat(isEnglish() ? "en-US" : "zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date());
    state.className = `settings-save-state ${settingsFormDirty ? "dirty" : "success"}`;
    state.textContent = settingsFormDirty
      ? uiText("已保存提交时的设置；新修改仍未保存", "Submitted settings saved; newer changes are still unsaved")
      : `${uiText("设置已保存并立即生效", "Settings saved and applied")} · ${savedAt}`;
    button.textContent = settingsFormDirty ? uiText("保存更改", "Save changes") : uiText("保存设置", "Save settings");
    const activationWarning = settingsActivationWarningText(confirmed, settingsFormDirty) || refreshWarning;
    if (activationWarning) {
      // Durable save and external activation are separate outcomes. Never
      // invite duplicate credential submission by presenting this as a failed
      // save, and never hide a failed reconnect behind a success toast.
      state.className = `settings-save-state warning${settingsFormDirty ? " dirty" : ""}`;
      state.textContent = activationWarning;
      showWorkspaceToast(activationWarning, "info");
    } else {
      showWorkspaceToast(settingsFormDirty
        ? uiText("已保存提交时的设置，新修改已保留", "Submitted settings saved; newer changes were preserved")
        : uiText("设置已成功保存并立即生效", "Settings saved and applied"));
    }
  } catch (error) {
    if (requestGeneration !== settingsRequestGeneration) {
      showWorkspaceToast(`${uiText("先前提交的设置未能确认保存；当前新输入已保留：", "The earlier settings save could not be confirmed; current edits were preserved: ")}${error.message}`, "error");
      return;
    }
    settingsFormDirty = true;
    state.className = "settings-save-state error dirty";
    state.textContent = `${uiText("保存失败：", "Save failed: ")}${error.message}`;
    button.disabled = false;
    button.textContent = uiText("重新保存", "Try saving again");
    showWorkspaceToast(`${uiText("设置保存失败：", "Failed to save settings: ")}${error.message}`, "error");
  } finally {
    settingsSavePending = false;
    button.disabled = false;
  }
}
$("#settingsForm").onsubmit = saveSettings;

$("#historySearch").addEventListener("input", () => {
  clearTimeout(historySearchTimer);
  historySearchTimer = setTimeout(() => loadTaskHistory(), 250);
});
$("#historyStatus").addEventListener("change", () => {
  loadTaskHistory();
});
$("#historyMore").addEventListener("click", () => {
  if (historyHasMore) loadTaskHistory({ append: true });
});
$("#deleteTaskContextAction").onclick = deleteTaskFromContextMenu;
$("#renameTaskContextAction").onclick = renameTaskFromContextMenu;
$("#pinTaskContextAction")?.addEventListener("click", pinTaskFromContextMenu);
$("#cancelRenameTask").onclick = closeRenameTaskDialog;
$("#closeRenameTaskDialog").onclick = closeRenameTaskDialog;
$("#renameTaskDialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeRenameTaskDialog();
});
$("#renameTaskDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closeRenameTaskDialog();
});
async function submitTaskRename(event) {
  event.preventDefault();
  const id = renameTaskId;
  const title = $("#renameTaskInput").value.trim();
  if (!id || !title || pendingTaskRenames.has(id)) return;
  const dialogGeneration = renameTaskDialogGeneration;
  pendingTaskRenames.add(id);
  syncRenameTaskDialogState();
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(id)}/title`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
    if (currentTaskSnapshot?.id === id) {
      // Replace the snapshot so an older in-flight poll cannot roll the title
      // back by passing its same-object freshness check.
      currentTaskSnapshot = { ...currentTaskSnapshot, title: result.title || title };
      updateConversationTitle(currentTaskSnapshot);
    }
    if (renameTaskId === id && renameTaskDialogGeneration === dialogGeneration) {
      const hasNewerInput = $("#renameTaskInput").value.trim() !== title;
      if (!hasNewerInput) closeRenameTaskDialog();
      showWorkspaceToast(hasNewerInput
        ? uiText("提交的名称已保存；新修改仍未保存", "Submitted name saved; newer changes are still unsaved")
        : uiText("聊天名称已更新", "Chat name updated"), "info");
    }
    await loadTaskHistory();
  } catch (error) {
    if (renameTaskId === id && renameTaskDialogGeneration === dialogGeneration) {
      showWorkspaceToast(`${uiText("重命名失败：", "Rename failed: ")}${error.message}`, "error");
    }
  } finally {
    pendingTaskRenames.delete(id);
    syncRenameTaskDialogState();
  }
}
$("#renameTaskForm").onsubmit = submitTaskRename;
document.addEventListener("pointerdown", (event) => {
  if (!event.target.closest("#taskContextMenu")) closeTaskContextMenu();
});
window.addEventListener("blur", closeTaskContextMenu);
window.addEventListener("resize", closeTaskContextMenu);
initializeSettingsSections();
$("#acceptConfirmDialog").onclick = () => settleConfirmation(true);
$("#cancelConfirmDialog").onclick = () => settleConfirmation(false);
$("#closeConfirmDialog").onclick = () => settleConfirmation(false);
$("#confirmDialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  settleConfirmation(false);
});
$("#confirmDialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) settleConfirmation(false);
});
$("#confirmDialog").addEventListener("close", () => {
  if (confirmationResolver) settleConfirmation(false);
});

function applyEnglishInterface() {
  if (!isEnglish()) return;
  document.documentElement.lang = "en";
  document.title = "Elren";
  const setText = (selector, value) => { const element = $(selector); if (element) element.textContent = value; };
  const setAttr = (selector, name, value) => { const element = $(selector); if (element) element.setAttribute(name, value); };
  const replaceLabelTexts = (selector, values) => {
    document.querySelectorAll(selector).forEach((label, index) => {
      const textNode = [...label.childNodes].find((node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
      if (textNode && values[index]) textNode.textContent = values[index];
    });
  };

  setText("#brandName", "Elren");
  setText("#brandTagline", "Local computer agent");
  setText("#navChats span:last-child", "Chats");
  setText("#navPlugins span:last-child", "Plugins");
  setAttr(".primary-nav", "aria-label", "Primary features");
  setText("#artifactInspector .artifact-inspector-kicker", "Task artifact");
  setText("#artifactInspectorTitle", "Artifact inspector");
  setText("#artifactInspectorPreviewTab", "Preview");
  setText("#artifactInspectorSourceTab", "Source");
  setText("#artifactInspectorChecksTab", "Checks");
  setAttr("#artifactInspector .artifact-inspector-tabs", "aria-label", "Artifact inspector views");
  setAttr("#closeArtifactInspector", "aria-label", "Close artifact inspector");
  setText("#subagentPanel .subagent-panel-kicker", "Task delegation");
  setText("#subagentPanelTitle", "Specialists");
  setAttr("#closeSubagentPanel", "aria-label", "Close specialists");
  setText("#apiText", "Connecting…");
  setText("#modelText", "Loading model…");
  setText("#workspaceText", "");
  setText("#openclawText", "OpenClaw: checking…");
  setText("#capabilityDialog .capability-head span", "Runtime diagnostics");
  setText("#capabilityDialog .capability-head h2", "Capability map");
  setText("#capabilityContent", "Loading the real tool catalog…");
  setText("#humanActionTitle", "Take over this action");
  setText("#takeOverHumanAction", "Take over");
  setText("#completeHumanAction", "I’ve completed it — continue");
  setText("#cancelHumanAction", "Something went wrong");
  document.querySelectorAll(".human-target-grid small").forEach((node, index) => { node.textContent = ["App / website", "Specific screen", "What to do"][index] || node.textContent; });
  setText("#humanProblemDialog .human-problem-kicker", "Takeover feedback");
  setText("#humanProblemTitle", "What went wrong?");
  setText("#humanProblemFormStep > p", "Describe the error, where you got stuck, or what happened. The Agent will continue with this context.");
  setAttr("#humanProblemDescription", "placeholder", "For example: after the verification succeeded, the page said the session had expired…");
  setText("#submitHumanProblem", "Send details");
  setText("#skipHumanProblem", "Skip");
  setText("#humanProblemConfirmStep .human-problem-kicker", "Prevent an accidental click");
  setText("#humanProblemConfirmStep h2", "Continue without describing the issue?");
  setText("#humanProblemConfirmStep p", "The Agent will continue, but it will have to infer the failure from the current screen.");
  setText("#returnHumanProblem", "Go back");
  setText("#confirmSkipHumanProblem", "Continue without details");
  setText("#neverAskHumanProblem", "Do not ask again");
  setText("#deleteTaskContextAction", "Delete this chat");
  setText("#renameTaskContextAction", "Rename this chat");
  setText("#pinTaskContextAction", "Pin chat");
  setText("#exportTaskMarkdown", "Export conversation (Markdown)");
  setText("#exportTaskJson", "Export conversation (JSON)");
  setText("#renameTaskDialogKicker", "Chat name");
  setText("#renameTaskDialogTitle", "Rename chat");
  setText("#renameTaskDialog label", "New name");
  setText("#renameTaskHint", "The name is used only in the task list and does not change the chat.");
  setText("#cancelRenameTask", "Cancel");
  setText("#saveRenameTask", "Save");
  setAttr("#closeRenameTaskDialog", "aria-label", "Close rename dialog");
  setAttr("#sidebarResizer", "aria-label", "Resize chat sidebar");
  setAttr("#sidebarResizer", "title", "Drag to resize the chat sidebar; double-click to reset");
  setText("#newTaskLabel", "New task");
  setAttr("#openSettings", "aria-label", "Settings");
  setAttr("#openSettings", "title", "Settings");
  setText("#workspaceDialog .capability-head span", "Elren Control Center");
  setAttr(".workspace-tabs", "aria-label", "Control center pages");
  setAttr("#settingsSectionNav", "aria-label", "Settings categories");
  setText("#modelPickerLabel", "Response model");
  setAttr("#uploadFile", "title", "Upload files");
  setAttr("#uploadFile", "aria-label", "Upload files");
  setAttr(".composer-icon-actions", "aria-label", "Attachments and voice");
  setText("#openCapabilities", "Capability map");
  setText("#settingsSaveState", "Keys show configuration status only; plaintext is never returned");
  setText("#saveSettings", "Save settings");
  setText("#historyMore", "Load more");
  setAttr("#historySearch", "placeholder", "Search tasks…");
  setAttr("#timeline", "aria-label", "Task progress");
  setAttr("#prompt", "aria-label", "Task instructions");
  setAttr("#humanProblemDescription", "aria-label", "Problem description");
  setAttr(".history-controls", "aria-label", "Filter all tasks");
  setAttr("#taskHistory", "aria-label", "All tasks");
  setAttr("#mobileNav", "aria-label", "Open sidebar");
  setAttr("#languageToggle", "title", "Switch to Chinese");
  setAttr("#languageToggle", "aria-label", "Switch to Chinese");
  setText("#languageToggle .language-icon-label", "Switch language");
  updateConversationTitle(currentTaskSnapshot);
  setAttr("#settingsSearch", "placeholder", "Search settings");
  setAttr("#settingsSearch", "aria-label", "Search settings");
  setText("#settingsSearchEmpty", "No matching settings found");
  setText(".welcome h2", "What would you like to build today?");
  setText(".welcome > p", "Choose a code project to understand code, fix issues, run tests, and review changes. Or start a general task.");
  const sectionLabels = document.querySelectorAll("aside > .section-label");
  ["All tasks"].forEach((value, index) => {
    const label = sectionLabels[index];
    if (!label) return;
    if (index === 0) { const first = label.querySelector("span:first-child"); if (first) first.textContent = value; }
    else label.textContent = value;
  });
  const statusOptions = { "": "All statuses", running: "Running", waiting_approval: "Confirming", waiting_user: "Waiting for your action", completed: "Completed", failed: "Failed", cancelled: "Stopped" };
  Object.entries(statusOptions).forEach(([value, label]) => { const option = $(`#historyStatus option[value="${value}"]`); if (option) option.textContent = label; });
  syncSettingsSelectWidget($("#historyStatus"));
  const policyOptions = { cautious: "Cautious · approve changes", balanced: "Balanced · approve high risk", autonomous: "Autonomous · do not ask" };
  Object.entries(policyOptions).forEach(([value, label]) => { const option = $(`#policy option[value="${value}"]`); if (option) option.textContent = label; });
  const modelOptions = { auto: "Automatic", "deepseek-v4-flash": "deepseek-v4-flash", "deepseek-v4-pro": "deepseek-v4-pro", "deepseek-v4-flash-vision-exp": "deepseek-v4-flash-vision-exp" };
  Object.entries(modelOptions).forEach(([value, label]) => { const option = $(`#modelPreference option[value="${value}"]`); if (option) option.textContent = label; });
  refreshModelPreferenceUI();
  setText("#reasoningPickerLabel", "Reasoning depth");
  setReasoningPreference(reasoningPreferenceValue());
  const settingsReasoningLabels = { auto: "Let the model decide", low: "Low (faster)", medium: "Medium (balanced)", high: "High (recommended)", xhigh: "Extra high", max: "Maximum (slowest)" };
  Object.entries(settingsReasoningLabels).forEach(([value, label]) => { const option = $(`#settingReasoningEffort option[value="${value}"]`); if (option) option.textContent = label; });
  const reasoningSetting = document.querySelector(".reasoning-setting");
  if (reasoningSetting) {
    reasoningSetting.childNodes[0].textContent = "Default reasoning depth";
    const help = reasoningSetting.querySelector("small");
    if (help) help.textContent = "Native levels follow verified capabilities; custom API levels are declared by you. No prompt-simulated reasoning control.";
  }
  setAttr("#prompt", "placeholder", "Describe what you want to get done…");
  setAttr("#send", "title", "Start task");
  setAttr("#navOverlay", "aria-label", "Close sidebar");
  setAttr("#historySearch", "aria-label", "Search all tasks");
  setAttr("#historyStatus", "aria-label", "Filter all tasks by status");
  document.querySelectorAll(".workspace-tabs button").forEach((button) => {
    button.textContent = { artifacts: "Artifacts", schedules: "Scheduled tasks", settings: "Settings" }[button.dataset.panel] || button.textContent;
  });
  replaceLabelTexts("#panel-schedules .form-grid > label", ["Name", "Schedule", "First run", "End (optional)", "What should the Agent do?"]);
  setText("#scheduleKind option[value='daily']", "Every day");
  setText("#scheduleKind option[value='interval']", "At a fixed interval");
  setText("#scheduleKind option[value='at']", "Run once");
  setText("#createSchedule", "Create scheduled task");
  setText("#panel-schedules .panel-copy", "Choose when to start, what to do, how often to repeat, and when to stop.");
  setAttr("#schedulePrompt", "placeholder", "Describe what the Agent should complete when the time arrives");
  setText("#panel-schedules .form-actions span", "Daily and repeating tasks may have an end time. Leave it blank to keep running; one-time tasks stop automatically.");
  setText("#scheduleForm button[type='submit']", "Create scheduled task");
  setText("#scheduleList .panel-empty", "Loading scheduled tasks…");
  replaceLabelTexts("#panel-settings .form-grid > label", ["Default model", "Default reasoning depth", "Request timeout (seconds)"]);
  setText(".provider-keys-setting > span", "Provider keys");
  replaceLabelTexts("#panel-settings .provider-keys-setting > label", ["DeepSeek primary key", "DeepSeek backup key (optional)", "Gemini free key (optional)", "Pollinations free media key (optional)", "Hugging Face free read token (optional)"]);
  setText(".provider-keys-setting small", "Keys stay in an OS-protected local encrypted vault, never in plaintext JSON and never echoed; leave a field blank to keep its current value.");
  setAttr("#settingDeepSeekKey", "placeholder", "Enter the DeepSeek primary key");
  setAttr("#settingDeepSeekBackupKey", "placeholder", "Optional; used after primary quota is exhausted");
  setAttr("#settingGeminiKey", "placeholder", "Optional visual fallback key");
  setAttr("#settingPollinationsKey", "placeholder", "Optional free-tier image, video, and music key");
  setAttr("#settingHuggingFaceToken", "placeholder", "hf_…; increases the included daily ZeroGPU quota");
  setText(".aicodemirror-setting > span", "AI Code Mirror relay");
  setText("#settingAicodemirrorKeyLabel", "Universal API key");
  setText("#settingAicodemirrorFableKeyLabel", "Fable dedicated key");
  setText("#settingAicodemirrorFableKeyHelp", "To access Fable models, disable Smart Routing, create a key for the Official Channel or Official Channel Stable route, and enter that key separately in Elren's Fable dedicated-key field.");
  setText(".aicodemirror-setting > p", "Registration: ");
  const mirrorLink = document.createElement("a");
  mirrorLink.href = "https://www.aicodemirror.ai/";
  mirrorLink.target = "_blank";
  mirrorLink.rel = "noopener noreferrer";
  mirrorLink.textContent = "www.aicodemirror.ai";
  document.querySelector(".aicodemirror-setting > p")?.append(mirrorLink);
  setText("#settingAicodemirrorGeneralHelp", "The universal key enables other Claude, OpenAI, and Gemini models. The Fable dedicated key is used only for claude-fable-5 and claude-fable-5-1; neither model falls back to the universal key. Leaving a field blank preserves its saved value.");
  setText(".model-providers-setting > span", "Third-party compatible APIs");
  setText("#addModelProvider", "+ Add third-party API");
  setText(".model-providers-setting > small", "Connect any third-party site compatible with the selected protocol, not just OpenAI or Anthropic. Enter its HTTPS API base URL (including /v1 or its custom path), exact model name and key. A bare domain uses /v1. Existing direct routes are preserved.");
  setText(".tool-credentials-setting > span", "Tool and skill credentials (optional)");
  replaceLabelTexts("#panel-settings .tool-credentials-setting > label", ["GitHub Token", "Google Places API Key", "Trello API Key", "Trello Token", "ElevenLabs API Key", "Notion Token", "Spotify Client ID", "Spotify Client Secret"]);
  setText(".tool-credentials-setting > small", "Credentials stay on this device and the API returns configured status only. Each skill receives only allowlisted variables. Third-party API keys are never passed to other tools as official OpenAI, Claude or Gemini keys.");
  setText(".tool-credentials-extra > summary", "More optional integration credentials");
  replaceLabelTexts(".tool-credentials-extra label", ["1Password Service Account Token", "GIPHY API Key", "Tenor API Key", "Apify API Token", "Firecrawl API Key", "Eight Sleep Email", "Eight Sleep Password", "Deliveroo Bearer Token", "Deliveroo Cookie (optional)", "Things Auth Token", "SAG API Key (optional)"]);
  setText(".tool-credentials-extra > small", "Bear Notes Grizzly accepts only a token-file path, while Sherpa TTS accepts only host runtime/model directories; those host-bound values are not stored here as plaintext secrets. summarize reuses the xAI model-provider key above.");
  setText(".feishu-setting > span", "Optional Feishu channel");
  replaceLabelTexts("#panel-settings .feishu-setting > label", ["App ID", "App Secret", "Open ID (default recipient)"]);
  setText(".feishu-setting small", "App ID and App Secret power the official long connection; Open ID is the default recipient for proactive agent messages.");
  setAttr("#settingFeishuAppId", "placeholder", "Optional app ID");
  setAttr("#settingFeishuAppSecret", "placeholder", "Optional app secret");
  setAttr("#settingFeishuOpenId", "placeholder", "For example, ou_xxx");
  setText(".telegram-setting > span", "Optional Telegram channel");
  replaceLabelTexts("#panel-settings .telegram-setting > label", ["Bot Token", "Default Chat ID (optional)"]);
  setText(".telegram-setting small", "The bot receives text, images, audio, video, and documents through official long polling. The first incoming message can automatically save its Chat ID.");
  setAttr("#settingTelegramBotToken", "placeholder", "Get it from BotFather");
  setAttr("#settingTelegramChatId", "placeholder", "Learned automatically after the first message");
  setText(".mobile-setting > span", "Android phone control");
  setText("#pairMobileDevice", "Pair Android phone");
  setText(".mobile-setting > small", "Connect the phone and PC to the same local network, then scan the QR code with Elren on your phone. Ordinary phone actions do not require Elren approval when the task uses Autonomous mode. Android permissions, biometrics, verification codes, payments, and security settings remain user-controlled.");
  setText("#mobilePairingTitle", "Pair Android phone");
  setAttr("#closeMobilePairing", "aria-label", "Close pairing window");
  setText("#mobilePairingSecurity", "The pairing code expires after five minutes. Device traffic uses end-to-end AES-256-GCM encryption, and keys are never shown in settings or logs.");
  const outputLimitHeading = $("#settingAutoOutputTokens")?.closest(".settings-field")?.querySelector(":scope > span");
  if (outputLimitHeading) outputLimitHeading.textContent = "Per-request output limit";
  setText("#panel-settings .cross-context-setting > span", "Cross-chat context");
  setText("#panel-settings .custom-system-prompt-setting > span", "System prompt addition");
  replaceLabelTexts("#panel-settings .custom-system-prompt-setting > label", ["Append to the end of the built-in system prompt"]);
  setAttr("#settingCustomSystemPromptSuffix", "placeholder", "For example: when answering code questions, provide a runnable example before explaining the key design trade-offs.");
  setText("#panel-settings .custom-system-prompt-setting small", "Optional. Saved text is appended verbatim to the end of every new task's system prompt without replacing Elren's built-in prompt. Clear and save to remove it.");
  setText("#panel-settings .voice-setting > span", "Voice conversation");
  replaceLabelTexts("#panel-settings label:has(> #settingAutoOutputTokens)", ["Use automatic provider limit (default)"]);
  replaceLabelTexts("#panel-settings label:has(> #settingCrossConversationContext)", ["Let new tasks reference all relevant earlier chats"]);
  setText("#panel-settings .cross-context-setting small", "General tasks can reference completed relevant chats, weighted by recency. Project tasks use only their own conversation, never other projects; tool logs and keys are excluded.");
  setText("#closeCapabilities", "×");
  setAttr("#closeCapabilities", "aria-label", "Close capability map");
  setAttr("#closeWorkspaceDialog", "aria-label", "Close control center");
  setText("#confirmDialogKicker", "Confirmation required");
  setText("#confirmDialogTitle", "Confirm action");
  setText("#cancelConfirmDialog", "Cancel");
  setText("#acceptConfirmDialog", "Confirm");
  setAttr("#closeConfirmDialog", "aria-label", "Close confirmation dialog");
}

// English panel renderers keep API-provided status text readable without translating
// user-authored task names or artifact filenames.
function artifactCategory(artifact = {}) {
  if (artifact.kind === "folder") return "folder";
  const extension = String(artifact.name || "").split(".").pop().toLowerCase();
  if (["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "md", "rtf", "csv"].includes(extension)) return "document";
  if (["png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "svg"].includes(extension)) return "image";
  if (["mp4", "webm", "mov", "mkv", "avi", "m4v"].includes(extension)) return "video";
  if (["mp3", "wav", "flac", "m4a", "aac", "ogg", "opus"].includes(extension)) return "audio";
  if (["html", "css", "js", "mjs", "ts", "tsx", "jsx", "py", "ps1", "json", "yaml", "yml", "toml", "xml", "sql", "sh"].includes(extension)) return "code";
  return "other";
}

function artifactIconName(artifact = {}) {
  if (artifact.kind === "folder") return "folder";
  const extension = String(artifact.name || "").split(".").pop().toLowerCase();
  if (["zip", "7z", "rar", "tar", "gz", "bz2", "xz"].includes(extension)) return "archive";
  const category = artifactCategory(artifact);
  return {
    document: "file-text",
    image: "image",
    video: "video",
    audio: "audio",
    code: "code",
  }[category] || "file";
}

function renderArtifacts() {
  const list = $("#artifactList");
  const query = $("#artifactSearch")?.value.trim().toLocaleLowerCase() || "";
  const type = $("#artifactType")?.value || "";
  const artifacts = latestArtifacts
    .filter((artifact) => {
      const searchable = `${artifact.name || ""} ${artifact.path || ""}`.toLocaleLowerCase();
      return (!query || searchable.includes(query)) && (!type || artifactCategory(artifact) === type);
    })
    .sort((left, right) => Number(right.modified || 0) - Number(left.modified || 0)
      || String(left.name || "").localeCompare(String(right.name || ""), undefined, { sensitivity: "base" }));
  if (!artifacts.length) {
    list.innerHTML = `<div class="panel-empty">${latestArtifacts.length
      ? (isEnglish() ? "No artifacts match the current filters." : "没有符合当前筛选条件的产物。")
      : (isEnglish() ? "No artifacts yet. Agent and MCP outputs will appear here." : "暂无产物。Agent 和 MCP 写入 outputs/ 后会显示在这里。")}</div>`;
    return;
  }
  list.innerHTML = artifacts.map((artifact) => {
    const tag = artifact.url ? "a" : "div";
    const link = artifact.url ? ` href="${escapeHtml(artifact.url)}" download` : "";
    const detail = artifact.kind === "folder"
      ? (artifact.summary_pending
        ? (isEnglish() ? "Calculating folder summary…" : "正在统计文件夹…")
        : (isEnglish() ? `${artifact.file_count} files · ${formatBytes(artifact.size)}` : `${artifact.file_count} 个文件 · ${formatBytes(artifact.size)}`))
      : formatBytes(artifact.size);
    return `<${tag} class="artifact-card"${link}>
      <span class="artifact-icon">${appSymbol(artifactIconName(artifact))}</span>
      <span><b>${escapeHtml(artifact.name)}</b><small>${escapeHtml(artifact.path)} · ${escapeHtml(detail)}</small></span>
      <time datetime="${escapeHtml(new Date(artifact.modified * 1000).toISOString())}">${escapeHtml(formatUiDateTime(artifact.modified * 1000))}</time>
    </${tag}>`;
  }).join("");
}

async function loadArtifacts({ background = false } = {}) {
  const requestGeneration = ++artifactRequestGeneration;
  const list = $("#artifactList");
  clearTimeout(artifactRefreshTimer);
  artifactRefreshTimer = null;
  if (!background) list.innerHTML = `<div class="panel-empty">${isEnglish() ? "Loading artifacts…" : "正在读取产物…"}</div>`;
  try {
    const result = await api("/api/artifacts");
    if (requestGeneration !== artifactRequestGeneration) return;
    latestArtifacts = Array.isArray(result.artifacts) ? result.artifacts : [];
    renderArtifacts();
    if (result.summary_pending) {
      artifactRefreshTimer = setTimeout(() => loadArtifacts({ background: true }), 700);
    }
  } catch (error) {
    if (requestGeneration !== artifactRequestGeneration) return;
    if (!background) {
      latestArtifacts = [];
      list.innerHTML = `<div class="panel-error">${isEnglish() ? "Failed to read artifacts: " : "读取失败："}${escapeHtml(error.message)}</div>`;
    }
  }
}

async function loadSchedules() {
  const requestGeneration = ++scheduleRequestGeneration;
  const list = $("#scheduleList");
  list.innerHTML = `<div class="panel-empty">${isEnglish() ? "Loading scheduled tasks…" : "正在读取定时任务…"}</div>`;
  try {
    const result = await api("/api/schedules");
    if (requestGeneration !== scheduleRequestGeneration) return;
    if (!result.schedules.length) {
      list.innerHTML = `<div class="panel-empty">${isEnglish() ? "No scheduled tasks yet." : "尚未创建定时任务。"}</div>`;
      return;
    }
    list.innerHTML = result.schedules.map((schedule) => `
      <article class="schedule-card ${schedule.enabled ? "" : "disabled"}">
        <div><b>${escapeHtml(schedule.name)}</b><span>${escapeHtml(scheduleDescription(schedule))}</span><small>${isEnglish() ? "Next: " : "下次："}${schedule.next_run ? formatUiDateTime(schedule.next_run) : (isEnglish() ? "finished or disabled" : "已结束或停用")} · ${isEnglish() ? "runs" : "已运行"} ${schedule.run_count}</small></div>
        <div class="schedule-actions">
          <button data-action="run" data-id="${schedule.id}">${isEnglish() ? "Run now" : "立即运行"}</button>
          <button data-action="toggle" data-id="${schedule.id}" data-enabled="${schedule.enabled}">${schedule.enabled ? (isEnglish() ? "Disable" : "停用") : (isEnglish() ? "Enable" : "启用")}</button>
          <button class="danger-text" data-action="delete" data-id="${schedule.id}">${isEnglish() ? "Delete" : "删除"}</button>
        </div>
      </article>`).join("");
    list.querySelectorAll("button").forEach((button) => {
      button.onclick = () => scheduleAction(button.dataset.action, button.dataset.id, button.dataset.enabled);
    });
  } catch (error) {
    if (requestGeneration !== scheduleRequestGeneration) return;
    list.innerHTML = `<div class="panel-error">${isEnglish() ? "Failed to read scheduled tasks: " : "读取定时任务失败："}${escapeHtml(error.message)}</div>`;
  }
}

function translateRuntimeCards() {
  if (!isEnglish()) return;
  const names = ["Model", "Context window", "Model providers", "Tools & plugins", "Workspace", "Portable runtime", "Portable tools", "MCP", "Windows sandbox", "OpenClaw", "OpenClaw provider key", "Feishu", "Telegram", "Android companion", "Public web access", "Scheduled tasks", "Vision", "Primary key", "Backup key"];
  document.querySelectorAll("#runtimeCards .runtime-card b").forEach((node, index) => { if (names[index]) node.textContent = names[index]; });
  const replace = {
    "已配置（已隐藏）": "Configured (hidden)",
    "未配置": "Not configured",
    "未配置 app_id/app_secret": "app_id/app_secret not configured",
    "严格模式，无域名白名单": "Strict mode; no domain allowlist",
    "未就绪": "Not ready",
  };
  document.querySelectorAll("#runtimeCards .runtime-card small").forEach((node) => {
    let value = node.textContent || "";
    Object.entries(replace).forEach(([from, to]) => { value = value.replaceAll(from, to); });
    value = value.replaceAll("个真实 stdio 工具", "real stdio tools").replaceAll("个任务", "tasks").replaceAll("次执行", "runs").replaceAll("官方固定，不可修改", "fixed by the provider").replaceAll("Job Object · 进程树/内存/CPU/超时", "Job Object · process tree / memory / CPU / timeout").replaceAll("已配置，等待事件或发送消息", "Configured; waiting for events or outgoing messages").replaceAll("已配置（已隐藏）", "Configured (hidden)");
    node.textContent = value;
  });
}

function setProviderKeyPlaceholders(settings) {
  const configured = uiText(
    "•••••••••••• · 已安全保存；输入新密钥可替换",
    "•••••••••••• · Saved securely; enter a new key to replace it",
  );
  const fields = [
    ["#settingDeepSeekKey", settings.primary_key_configured, "输入 DeepSeek 主密钥", "Enter the DeepSeek primary key"],
    ["#settingDeepSeekBackupKey", settings.backup_key_configured, "可留空，额度用尽时自动切换", "Optional; used after primary quota is exhausted"],
    ["#settingGeminiKey", settings.gemini_key_configured, "可留空，作为视觉备用端点", "Optional visual fallback key"],
    ["#settingPollinationsKey", settings.pollinations_key_configured, "可留空；用于免费档图片、视频和音乐生成", "Optional; used for free-tier image, video, and music generation"],
    ["#settingHuggingFaceToken", settings.huggingface_token_configured, "hf_…；免费账号每天约 5 分钟 ZeroGPU", "hf_…; a free account includes about 5 ZeroGPU minutes per day"],
    ["#settingAicodemirrorKey", settings.aicodemirror_key_configured, "输入通用中转密钥", "Enter the universal relay key"],
    ["#settingAicodemirrorFableKey", settings.aicodemirror_fable_key_configured, "仅用于 Fable 模型", "Used only for Fable models"],
    ["#settingFeishuAppId", settings.feishu_configured, "可留空", "Optional app ID"],
    ["#settingFeishuAppSecret", settings.feishu_configured, "可留空", "Optional app secret"],
    ["#settingFeishuOpenId", settings.feishu_open_id_configured, "例如 ou_xxx", "For example, ou_xxx"],
    ["#settingTelegramBotToken", settings.telegram_configured, "从 BotFather 获取", "Get it from BotFather"],
    ["#settingTelegramChatId", settings.telegram_chat_id_configured, "收到首条消息后自动记录", "Learned automatically after the first message"],
    ["#settingGithubToken", settings.github_token_configured, "用于 github、gh-issues", "Used by github and gh-issues"],
    ["#settingGooglePlacesKey", settings.google_places_key_configured, "用于 goplaces", "Used by goplaces"],
    ["#settingTrelloApiKey", settings.trello_configured, "用于 trello", "Used by trello"],
    ["#settingTrelloToken", settings.trello_configured, "与 Trello API Key 配套", "Pair with the Trello API key"],
    ["#settingElevenLabsKey", settings.elevenlabs_key_configured, "用于 sag 语音", "Used by sag voice"],
    ["#settingNotionToken", settings.notion_token_configured, "用于 notion", "Used by notion"],
    ["#settingSpotifyClientId", settings.spotify_configured, "用于 sonoscli 的可选 Spotify 搜索", "Optional Spotify search for sonoscli"],
    ["#settingSpotifyClientSecret", settings.spotify_configured, "与 Spotify Client ID 配套", "Pair with the Spotify client ID"],
    ["#settingOpServiceToken", settings.onepassword_configured, "用于 1password", "Used by 1password"],
    ["#settingGiphyKey", settings.giphy_key_configured, "用于 gifgrep", "Used by gifgrep"],
    ["#settingTenorKey", settings.tenor_key_configured, "gifgrep 可选", "Optional for gifgrep"],
    ["#settingApifyToken", settings.apify_token_configured, "summarize 的 YouTube 备用", "YouTube fallback for summarize"],
    ["#settingFirecrawlKey", settings.firecrawl_key_configured, "summarize 的受限网页备用", "Blocked-site fallback for summarize"],
    ["#settingEightctlEmail", settings.eightctl_email_configured, "用于 eightctl", "Used by eightctl"],
    ["#settingEightctlPassword", settings.eightctl_password_configured, "用于 eightctl", "Used by eightctl"],
    ["#settingDeliverooToken", settings.deliveroo_token_configured, "用于 ordercli", "Used by ordercli"],
    ["#settingDeliverooCookie", settings.deliveroo_cookie_configured, "ordercli 可选", "Optional for ordercli"],
    ["#settingThingsToken", settings.things_token_configured, "用于 things-mac", "Used by things-mac"],
    ["#settingSagKey", settings.sag_alt_key_configured, "sag 的 ElevenLabs 备用", "Alternative credential for sag"],
  ];
  fields.forEach(([selector, isConfigured, zhEmpty, enEmpty]) => {
    const input = $(selector);
    if (input) {
      input.placeholder = isConfigured ? configured : uiText(zhEmpty, enEmpty);
      input.dataset.configured = isConfigured ? "true" : "false";
    }
  });
  const mirrorStatus = $("#settingAicodemirrorKeyStatus");
  if (mirrorStatus) {
    const isConfigured = settings.aicodemirror_key_configured === true;
    mirrorStatus.textContent = isConfigured
      ? uiText("已配置", "Configured")
      : uiText("未配置", "Not configured");
    mirrorStatus.classList.toggle("configured", isConfigured);
  }
  const fableStatus = $("#settingAicodemirrorFableKeyStatus");
  if (fableStatus) {
    const isConfigured = settings.aicodemirror_fable_key_configured === true;
    fableStatus.textContent = isConfigured
      ? uiText("已配置", "Configured")
      : uiText("未配置", "Not configured");
    fableStatus.classList.toggle("configured", isConfigured);
  }
}

function translateSettingsDynamic() {
  if (!isEnglish()) return;
  const setText = (selector, value) => { const node = $(selector); if (node) node.textContent = value; };
  const setAttr = (selector, name, value) => { const node = $(selector); if (node) node.setAttribute(name, value); };
  const modelDefault = $("#settingModel option[value='deepseek-v4-flash']");
  const modelPro = $("#settingModel option[value='deepseek-v4-pro']");
  if (modelDefault) modelDefault.textContent = "V4 Flash (default)";
  if (modelPro) modelPro.textContent = "V4 Pro (capability first)";
  setAttr("#settingMaxOutputTokens", "aria-label", "Per-request output token limit");
  const fieldSmall = (inputSelector, value) => { const input = $(inputSelector); const small = input?.closest(".settings-field")?.querySelector("small"); if (small) small.textContent = value; };
  updateOutputTokenHelp();
  setText("#runtimeCards .panel-empty", "Diagnosing…");
  populateSettingVoiceNames($("#settingVoiceName")?.value || "");
  translateRuntimeCards();
  enhanceAllSettingsSelects();
}

function saveLanguageComposerTransfer(nextUrl) {
  const draftEntries = new Map(composerDrafts);
  draftEntries.set(composerDraftScope, {
    text: $("#prompt").value,
    systemDraft: $("#prompt").dataset.systemDraft || "",
    attachments: pendingAttachments,
  });
  const token = window.crypto.randomUUID();
  const snapshot = {
    version: 1, token, createdAt: Date.now(), language: nextUrl.searchParams.get("lang"),
    task: nextUrl.searchParams.get("task"), scope: composerDraftScope,
    model: $("#modelPreference").value, reasoning: reasoningPreferenceValue(),
    newTaskModel: newTaskModelPreference, newTaskReasoning: newTaskReasoningPreference,
    newTaskProject: newTaskProjectPath,
    drafts: [...draftEntries].map(([scope, draft]) => [scope, {
      text: draft.text, systemDraft: draft.systemDraft || "",
      // Only already uploaded file associations, never file bytes or settings.
      attachments: (draft.attachments || []).map(({ name, path }) => ({ name, path })),
    }]),
  };
  const serialized = JSON.stringify(snapshot);
  if (serialized.length > 2 * 1024 * 1024 || snapshot.drafts.length > 256) throw new Error("transfer-too-large");
  window.sessionStorage.setItem("elren.language-composer-transfer.v1", serialized);
  if (window.sessionStorage.getItem("elren.language-composer-transfer.v1") !== serialized) throw new Error("transfer-unavailable");
  nextUrl.searchParams.set("resume", token);
}

function restoreLanguageComposerTransfer() {
  const key = "elren.language-composer-transfer.v1";
  try {
    const url = new URL(window.location.href);
    const token = url.searchParams.get("resume");
    const raw = window.sessionStorage.getItem(key);
    // Consume before parsing; malformed, stale and unrelated transfers cannot
    // resurface on a later navigation. No ordinary autosave is introduced.
    window.sessionStorage.removeItem(key);
    url.searchParams.delete("resume");
    if (token) window.history.replaceState(null, "", url.toString());
    if (!token || !raw || raw.length > 2 * 1024 * 1024) return false;
    const snapshot = JSON.parse(raw);
    const age = Date.now() - snapshot.createdAt;
    if (snapshot.version !== 1 || snapshot.token !== token || !Number.isFinite(age) || age < 0 || age > 5 * 60 * 1000
        || snapshot.language !== uiLanguage || snapshot.task !== url.searchParams.get("task")
        || snapshot.scope !== snapshot.task || !Array.isArray(snapshot.drafts) || snapshot.drafts.length > 256) return false;
    const safeScope = (scope) => scope === null || (typeof scope === "string" && scope.length <= 200);
    const safePreference = (value) => typeof value === "string" && value.length <= 200;
    if (![snapshot.model, snapshot.reasoning, snapshot.newTaskModel, snapshot.newTaskReasoning].every(safePreference)) return false;
    if (snapshot.newTaskProject !== undefined && (typeof snapshot.newTaskProject !== "string"
      || snapshot.newTaskProject.length > 4096 || snapshot.newTaskProject.includes("\0"))) return false;
    const restored = new Map();
    for (const entry of snapshot.drafts) {
      if (!Array.isArray(entry) || entry.length !== 2) return false;
      const [scope, draft] = entry;
      if (!safeScope(scope) || restored.has(scope) || !draft || typeof draft.text !== "string"
          || typeof draft.systemDraft !== "string" || !Array.isArray(draft.attachments) || draft.attachments.length > 20) return false;
      if (!draft.attachments.every((item) => item && typeof item.name === "string" && typeof item.path === "string")) return false;
      restored.set(scope, { text: draft.text, systemDraft: draft.systemDraft,
        attachments: draft.attachments.map(({ name, path }) => ({ name, path })) });
    }
    if (!restored.has(snapshot.scope)) return false;
    composerDrafts.clear();
    restored.forEach((draft, scope) => composerDrafts.set(scope, draft));
    composerDraftScope = snapshot.scope;
    const draft = restored.get(snapshot.scope);
    $("#prompt").value = draft.text;
    if (draft.systemDraft) $("#prompt").dataset.systemDraft = draft.systemDraft;
    else delete $("#prompt").dataset.systemDraft;
    pendingAttachments = [...draft.attachments];
    pendingLanguageSwitchModel = snapshot.model;
    $("#modelPreference").dataset.languageResumeModel = snapshot.model;
    $("#reasoningPreference").dataset.languageResumeReasoning = snapshot.reasoning;
    newTaskModelPreference = snapshot.newTaskModel;
    newTaskReasoningPreference = snapshot.newTaskReasoning;
    newTaskProjectPath = $("#projectPath") ? (snapshot.newTaskProject || "") : "";
    reasoningDefaultLoaded = true;
    renderAttachments();
    return true;
  } catch {
    // Storage-disabled sessions are handled before navigation by the sender.
    return false;
  }
}

let languageSwitchPending = false;
$("#languageToggle").onclick = async () => {
  if (languageSwitchPending) return;
  if (uploadRequestPending || startRequestPending) {
    showWorkspaceToast(uiText("请等待上传或发送完成后再切换语言，草稿会保留。", "Wait for the upload or send to finish before switching language. Your draft is kept."), "info");
    return;
  }
  languageSwitchPending = true;
  try {
  if (settingsFormDirty) {
    const confirmed = await askForConfirmation(
      uiText(
        "切换语言会重新载入界面，当前未保存的设置将被放弃。",
        "Switching language reloads the interface and discards unsaved settings.",
      ),
      {
        title: uiText("放弃未保存的设置？", "Discard unsaved settings?"),
        confirmLabel: uiText("放弃并切换", "Discard and switch"),
        danger: true,
      },
    );
    if (!confirmed) return;
  }
  if (uploadRequestPending || startRequestPending) {
    showWorkspaceToast(uiText("请等待上传或发送完成后再切换语言，草稿会保留。", "Wait for the upload or send to finish before switching language. Your draft is kept."), "info");
    return;
  }
  cacheRuntimeStatus(latestStatus);
  const nextLanguage = isEnglish() ? "zh" : "en";
  const activeTaskId = taskId || continuationTaskId || currentTaskSnapshot?.id || null;
  const nextUrl = new URL(window.location.href);
  nextUrl.searchParams.set("lang", nextLanguage);
  nextUrl.searchParams.set("model", $("#modelPreference")?.value || "auto");
  if (activeTaskId) nextUrl.searchParams.set("task", activeTaskId);
  else nextUrl.searchParams.delete("task");
  try {
    saveLanguageComposerTransfer(nextUrl);
  } catch {
    try { window.sessionStorage.removeItem("elren.language-composer-transfer.v1"); } catch {}
    showWorkspaceToast(uiText("无法安全保留当前草稿，已取消语言切换。请先发送或另行保存草稿。", "Language switch cancelled because the draft could not be preserved safely. Send or save your draft first."), "error");
    return;
  }
  if (settingsFormDirty) discardSettingsChanges();
  try { window.localStorage.setItem(LANGUAGE_STORAGE_KEY, nextLanguage); } catch {}
  syncDesktopLanguage(nextLanguage);
  try {
    window.location.assign(nextUrl.toString());
  } catch {
    try { window.sessionStorage.removeItem("elren.language-composer-transfer.v1"); } catch {}
    showWorkspaceToast(uiText("无法重新载入界面，当前草稿仍保留在此页面。", "The interface could not reload. Your draft is still on this page."), "error");
  }
  } finally {
    languageSwitchPending = false;
  }
};
window.addEventListener("beforeunload", (event) => {
  if (!settingsFormDirty) return;
  if (discussionTeamDraftTimer) persistDiscussionTeamDraft();
  event.preventDefault();
  event.returnValue = "";
});
applyEnglishInterface();
restoreLanguageComposerTransfer();
hydrateSettingsDisplayFromCache();
preloadSettingsDisplay();
if (latestStatus) renderRuntimeStatus(latestStatus, { trustModelCapabilities: false });

initializeModelPreferencePicker();
initializeSidebarResize();
initializeDesktopControlStatus();
initializePrimaryNavigation();
initializeSubagentPanel();
initializeArtifactInspector();
observeSettingsSelects();
enhanceHistoryStatusSelect();
enhanceWorkspaceSelects();
$("#timeline")?.addEventListener("scroll", () => {
  timelineAutoFollow = timelineIsNearBottom();
  if (timelineAutoFollow) clearTimelineNewProgress();
}, { passive: true });
setReasoningPreference("high");
resetScheduleForm();
initializeScheduleDatePickers();
initializeLocalizedConstraints();
updateOutputTokenHelp();
loadStatus();
resizePromptInput();
syncComposerAction();
const initialTaskId = new URLSearchParams(window.location.search).get("task");
if (initialTaskId) openTask(initialTaskId);
else loadTaskHistory();
scheduleTaskHistorySync();
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    scheduleDesktopControlPoll(15000);
    scheduleTaskHistorySync(15000);
    return;
  }
  scheduleDesktopControlPoll(0);
  scheduleTaskHistorySync(50);
  if (taskId) {
    clearTimeout(pollTimer);
    poll(taskId, taskViewGeneration);
  }
  if (runtimeConnectionLost) {
    clearTimeout(runtimePollTimer);
    loadStatus();
  }
});
