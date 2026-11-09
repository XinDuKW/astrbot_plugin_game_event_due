const bridge = window.AstrBotPluginPage;

const COVER_LABELS = {
  image: "直接发送图片",
  link: "只发图片链接",
  off: "不附带封面",
};

const elements = {
  runtimeStatus: document.getElementById("runtime-status"),
  refreshButton: document.getElementById("refresh-button"),
  cycleButton: document.getElementById("cycle-button"),
  errorNotice: document.getElementById("error-notice"),
  sessionList: document.getElementById("session-list"),
  sessionSearch: document.getElementById("session-search"),
  detailView: document.getElementById("detail-view"),
  sourceBand: document.getElementById("source-band"),
  toast: document.getElementById("toast"),
  metrics: {
    sessions: document.getElementById("metric-sessions"),
    enabled: document.getElementById("metric-enabled"),
    games: document.getElementById("metric-games"),
    reminders: document.getElementById("metric-reminders"),
  },
};

const state = {
  overview: null,
  selectedUmo: null,
  search: "",
  loading: true,
  saving: false,
  toastTimer: null,
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message, isError = false) {
  window.clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("is-error", isError);
  elements.toast.classList.remove("is-hidden");
  state.toastTimer = window.setTimeout(() => {
    elements.toast.classList.add("is-hidden");
  }, 3200);
}

function setError(message = "") {
  elements.errorNotice.textContent = message;
  elements.errorNotice.classList.toggle("is-hidden", !message);
}

function setRuntime(text, kind = "") {
  elements.runtimeStatus.querySelector("span:last-child").textContent = text;
  elements.runtimeStatus.classList.toggle("is-ready", kind === "ready");
  elements.runtimeStatus.classList.toggle("is-error", kind === "error");
}

function setSaving(saving) {
  state.saving = saving;
  document.querySelectorAll("button, input, textarea").forEach((control) => {
    control.disabled = saving;
  });
}

// 插件页跑在 iframe 里，window.confirm 可能被静默拦掉（点了毫无反应），
// 因此危险操作统一改成按钮内二次确认：第一次点变成「再点一次…」，
// 4 秒内不跟就自动复原，避免误触。
function armDangerButton(button, armedLabel, onConfirm) {
  if (button.dataset.armed === "1") {
    button.dataset.armed = "";
    button.textContent = button.dataset.idleLabel || "移除";
    button.classList.remove("is-armed");
    onConfirm();
    return;
  }
  button.dataset.idleLabel = button.textContent;
  button.dataset.armed = "1";
  button.textContent = armedLabel;
  button.classList.add("is-armed");
  window.setTimeout(() => {
    if (!button.isConnected || button.dataset.armed !== "1") return;
    button.dataset.armed = "";
    button.textContent = button.dataset.idleLabel || "移除";
    button.classList.remove("is-armed");
  }, 4000);
}

function currentSession() {
  if (!state.overview) return null;
  const sessions = state.overview.sessions || [];
  return sessions.find((item) => item.umo === state.selectedUmo) || null;
}

const MISSING_BRIDGE_HINT =
  "未能连接到 AstrBot Dashboard：桥接脚本（AstrBotPluginPage）未加载。\n" +
  "请通过 AstrBot 后台 → 插件 → 本插件详情页打开此页面；" +
  "若直接访问该 URL 或插件未重载，就会出现此提示。";

function bridgeMissing() {
  setError(MISSING_BRIDGE_HINT);
  setRuntime("未连接 Dashboard", "error");
}

async function loadOverview({ announce = false } = {}) {
  if (!bridge) {
    bridgeMissing();
    return;
  }
  state.loading = true;
  elements.refreshButton.classList.add("is-loading");
  try {
    state.overview = await bridge.apiGet("overview");
    const sessions = state.overview.sessions || [];
    if (!sessions.some((item) => item.umo === state.selectedUmo)) {
      state.selectedUmo = sessions.length ? sessions[0].umo : null;
    }
    render();
    setRuntime("已连接", "ready");
    setError("");
    if (announce) showToast("数据已刷新");
  } catch (error) {
    setError(error?.message || "读取订阅数据失败");
    setRuntime("读取失败", "error");
    if (announce) showToast("刷新失败", true);
  } finally {
    state.loading = false;
    elements.refreshButton.classList.remove("is-loading");
  }
}

async function post(endpoint, payload, successMessage) {
  setSaving(true);
  try {
    await bridge.apiPost(endpoint, payload);
    await loadOverview();
    if (successMessage) showToast(successMessage);
    return true;
  } catch (error) {
    showToast(error?.message || "保存失败", true);
    return false;
  } finally {
    setSaving(false);
  }
}

function render() {
  renderMetrics();
  renderSources();
  renderSessionList();
  renderDetail();
}

function renderMetrics() {
  const overview = state.overview;
  if (!overview) return;
  const sessions = overview.sessions || [];
  elements.metrics.sessions.textContent = String(sessions.length);
  elements.metrics.enabled.textContent = String(
    sessions.filter((item) => item.enabled).length,
  );
  elements.metrics.games.textContent = String((overview.games || []).length);
  const cycle = overview.cycle || {};
  const reminders = Number(cycle.reminders || 0);
  elements.metrics.reminders.textContent = cycle.finished_at
    ? String(reminders)
    : "待运行";
}

function renderSources() {
  const overview = state.overview;
  if (!overview) return;
  const parts = (overview.sources || []).map((source) => {
    const title = source.enabled ? source.note : "已在配置中关闭";
    return `<span class="source-chip ${source.enabled ? "is-on" : ""}" title="${escapeHtml(title)}">
      <span class="dot"></span>${escapeHtml(source.name)}
    </span>`;
  });
  const cycle = overview.cycle || {};
  const failures = Object.entries(cycle.failures || {});
  if (cycle.finished_at) {
    parts.push(
      `<span class="source-chip" title="上一轮检查时间">
        上轮检查 ${escapeHtml(cycle.finished_at)}
      </span>`,
    );
  }
  if (failures.length) {
    const detail = failures.map(([id, text]) => `${id}: ${text}`).join("\n");
    parts.push(
      `<span class="source-chip" title="${escapeHtml(detail)}" style="color:var(--danger)">
        ⚠️ ${failures.length} 个数据源异常
      </span>`,
    );
  }
  elements.sourceBand.innerHTML = parts.join("");
}

function filteredSessions() {
  const sessions = state.overview?.sessions || [];
  const keyword = state.search.trim().toLowerCase();
  if (!keyword) return sessions;
  return sessions.filter((item) =>
    `${item.session_name} ${item.session_id} ${item.umo} ${item.game_names.join(" ")}`
      .toLowerCase()
      .includes(keyword),
  );
}

function renderSessionList() {
  const sessions = filteredSessions();
  if (!sessions.length) {
    elements.sessionList.innerHTML =
      '<li class="empty-state">没有匹配的会话</li>';
    return;
  }
  elements.sessionList.innerHTML = sessions
    .map((item) => {
      const active = item.umo === state.selectedUmo;
      const statusBadge = item.enabled
        ? '<span class="badge is-on">推送中</span>'
        : '<span class="badge is-off">已暂停</span>';
      const games = item.game_names.join("、") || "未选择游戏";
      return `<li class="session-item ${active ? "is-active" : ""}" data-umo="${escapeHtml(item.umo)}">
        <div class="session-name">${escapeHtml(item.session_name)} ${statusBadge}</div>
        <div class="session-meta">${escapeHtml(games)}</div>
      </li>`;
    })
    .join("");

  elements.sessionList.querySelectorAll(".session-item").forEach((node) => {
    node.addEventListener("click", () => {
      state.selectedUmo = node.dataset.umo;
      render();
    });
  });
}

function renderDetail() {
  const session = currentSession();
  if (!session) {
    elements.detailView.innerHTML =
      '<div class="empty-state">左侧选择一个会话来管理它的活动订阅。<br />在群聊中使用 <code>/活动订阅 &lt;游戏名&gt;</code> 即可创建订阅。</div>';
    return;
  }

  const games = state.overview.games || [];
  const defaults = state.overview.defaults || {};
  const selected = new Set(session.games || []);
  const thresholdsValue = (session.thresholds || []).join(", ");

  const gameOptions = games
    .map(
      (game) => `<label class="game-option">
        <input type="checkbox" name="games" value="${escapeHtml(game.id)}"
          ${selected.has(game.id) ? "checked" : ""} />
        <span>${escapeHtml(game.name)}
          <span class="game-source">${escapeHtml(game.source_name)}</span>
        </span>
      </label>`,
    )
    .join("");

  elements.detailView.innerHTML = `
    <h2>${escapeHtml(session.session_name)}</h2>
    <div class="detail-umo">${escapeHtml(session.umo)}</div>

    <form class="detail-form" id="detail-form">
      <div class="field-group">
        <div class="toggle-row">
          <label class="toggle">
            <input type="checkbox" id="field-enabled" ${session.enabled ? "checked" : ""} />
            <span>启用活动推送</span>
          </label>
          <label class="toggle">
            <input type="checkbox" id="field-notify-new" ${session.notify_new ? "checked" : ""} />
            <span>推送新活动</span>
          </label>
        </div>
      </div>

      <fieldset class="field-group">
        <legend>订阅游戏（${selected.size} / ${games.length}）</legend>
        <div class="game-grid">${gameOptions || "没有可用的游戏，请在插件配置中启用数据源"}</div>
      </fieldset>

      <fieldset class="field-group">
        <legend>封面形式</legend>
        <div class="field-row">
          <label class="toggle">
            <input type="checkbox" id="field-cover-default" ${session.cover_mode_overridden ? "" : "checked"} />
            <span>跟随全局默认（${escapeHtml(COVER_LABELS[defaults.cover_mode] || defaults.cover_mode || "直接发送图片")}）</span>
          </label>
          <label for="field-cover-mode">本会话封面形式</label>
          <select id="field-cover-mode" ${session.cover_mode_overridden ? "" : "disabled"}>
            <option value="image" ${session.effective_cover_mode === "image" ? "selected" : ""}>直接发送图片</option>
            <option value="link" ${session.effective_cover_mode === "link" ? "selected" : ""}>只发图片链接</option>
            <option value="off" ${session.effective_cover_mode === "off" ? "selected" : ""}>不附带封面</option>
          </select>
          <span class="field-hint">也可以在本会话直接用 <code>/活动封面 图片</code> 或 <code>/活动封面 链接</code> 切换。</span>
        </div>
      </fieldset>

      <fieldset class="field-group">
        <legend>倒计时提醒</legend>
        <div class="field-row">
          <label class="toggle">
            <input type="checkbox" id="field-use-default" ${session.thresholds_overridden ? "" : "checked"} />
            <span>使用全局默认档位（${escapeHtml((defaults.thresholds || []).join("、") || "已关闭")}）</span>
          </label>
          <label for="field-thresholds">自定义档位（天，逗号分隔；留空表示关闭）</label>
          <input type="text" id="field-thresholds" value="${escapeHtml(thresholdsValue)}"
            placeholder="例如 7, 3, 1" ${session.thresholds_overridden ? "" : "disabled"} />
          <span class="field-hint">1 表示剩余不足 24 小时。活动剩余时间进入某一档位时提醒一次。</span>
        </div>
      </fieldset>

      <fieldset class="field-group">
        <legend>关键词过滤</legend>
        <div class="field-row">
          <label for="field-blacklist">黑名单（命中任一关键词就不推送）</label>
          <textarea id="field-blacklist" rows="2" placeholder="例如 双倍掉落, 签到">${escapeHtml((session.blacklist || []).join(", "))}</textarea>
        </div>
        <div class="field-row" style="margin-top:10px">
          <label for="field-whitelist">白名单（填写后只推送命中的活动）</label>
          <textarea id="field-whitelist" rows="2" placeholder="例如 寻访, 主线">${escapeHtml((session.whitelist || []).join(", "))}</textarea>
        </div>
        <span class="field-hint">匹配对象为活动名称、描述与标签，逗号分隔，不区分大小写。</span>
      </fieldset>

      <div class="detail-actions">
        <button type="submit" class="primary-button">保存设置</button>
        <button type="button" id="preview-button">预览当前活动</button>
        <button type="button" id="remove-button" class="danger-button">移除订阅</button>
      </div>

      <pre class="preview-output is-hidden" id="preview-output"></pre>
    </form>
  `;

  const useDefault = document.getElementById("field-use-default");
  const thresholdsInput = document.getElementById("field-thresholds");
  useDefault.addEventListener("change", () => {
    thresholdsInput.disabled = useDefault.checked;
  });

  const coverDefault = document.getElementById("field-cover-default");
  const coverSelect = document.getElementById("field-cover-mode");
  coverDefault.addEventListener("change", () => {
    coverSelect.disabled = coverDefault.checked;
  });

  document
    .getElementById("detail-form")
    .addEventListener("submit", (event) => {
      event.preventDefault();
      submitDetail(session);
    });

  document
    .getElementById("preview-button")
    .addEventListener("click", () => previewGame(session));

  document
    .getElementById("remove-button")
    .addEventListener("click", (event) => {
      const button = event.currentTarget;
      button.title = `确定要移除「${session.session_name}」的全部活动订阅吗？`;
      armDangerButton(button, "确认移除？", () =>
        post("session/remove", { umo: session.umo }, "已移除该会话的订阅"),
      );
    });
}

function parseThresholds(text) {
  return String(text || "")
    .split(/[,，\s]+/)
    .map((item) => Number.parseInt(item, 10))
    .filter((value) => Number.isFinite(value) && value > 0);
}

function parseKeywords(text) {
  const seen = [];
  String(text || "")
    .split(/[,，\n]+/)
    .map((item) => item.trim())
    .forEach((item) => {
      if (item && !seen.includes(item)) seen.push(item);
    });
  return seen;
}

async function submitDetail(session) {
  const games = Array.from(
    document.querySelectorAll('input[name="games"]:checked'),
  ).map((node) => node.value);
  if (!games.length) {
    showToast("请至少保留一个订阅游戏", true);
    return;
  }

  const useDefault = document.getElementById("field-use-default").checked;
  const coverDefault = document.getElementById("field-cover-default").checked;
  const payload = {
    umo: session.umo,
    games,
    enabled: document.getElementById("field-enabled").checked,
    notify_new: document.getElementById("field-notify-new").checked,
    cover_mode: coverDefault
      ? null
      : document.getElementById("field-cover-mode").value,
    thresholds: useDefault
      ? null
      : parseThresholds(document.getElementById("field-thresholds").value),
    blacklist: parseKeywords(document.getElementById("field-blacklist").value),
    whitelist: parseKeywords(document.getElementById("field-whitelist").value),
  };
  await post("session/update", payload, "设置已保存");
}

async function previewGame(session) {
  const games = Array.from(
    document.querySelectorAll('input[name="games"]:checked'),
  ).map((node) => node.value);
  const output = document.getElementById("preview-output");
  const gameId = games[0];
  if (!gameId) {
    showToast("请先勾选至少一个游戏", true);
    return;
  }
  output.classList.remove("is-hidden");
  output.textContent = "正在获取活动数据…";
  setSaving(true);
  try {
    const result = await bridge.apiPost("preview", { game_id: gameId });
    output.textContent = result?.text || "没有获取到活动数据";
  } catch (error) {
    output.textContent = `获取失败：${error?.message || error}`;
  } finally {
    setSaving(false);
  }
}

async function runCycle() {
  setSaving(true);
  try {
    const result = await bridge.apiPost("refresh", {});
    const summary = `检查完成：数据源 ${result.fetched_games} 个 / 活动 ${result.activities} 条 / 提醒 ${result.reminders} 条`;
    showToast(summary);
    if (result.failures && Object.keys(result.failures).length) {
      showToast("部分数据源异常，详见状态栏", true);
    }
    await loadOverview();
  } catch (error) {
    showToast(error?.message || "检查失败", true);
  } finally {
    setSaving(false);
  }
}

// ---- 消息模板编辑器 ----
//
// 后端只会给出「结构化预览」：标题、分段标题、分隔线、活动块，以及合并转发里
// 每个节点的名字与首行。这里只负责把它画成 QQ 的样子，不参与文案拼装，
// 所以页面上看到的就是真正会发出去的那条消息。

const TEMPLATE_FIELDS = [
  {
    group: "基础",
    items: [
      {
        key: "use_forward_node",
        label: "使用合并转发",
        hint: "关闭后按游戏合并成一条普通消息；开启后每个活动一个转发节点。",
        type: "switch",
      },
      {
        key: "forward_node_name",
        label: "转发节点名",
        hint: "普通消息模式下这条消息的显示名，建议 4 字以内。",
        type: "text",
        placeholder: "活动提醒",
      },
      {
        key: "cover_mode",
        label: "封面形式",
        hint: "",
        type: "select",
      },
      {
        key: "image_mode_detail_link",
        label: "图片模式也附详情链接",
        hint: "开启后即使已经发了封面图，也会再补一行详情链接。",
        type: "switch",
      },
    ],
  },
  {
    group: "倒计时标记",
    items: [
      {
        key: "urgency_marker",
        label: "显示颜色方块",
        hint: "每个活动标题前的方块，颜色表示剩余时间档位。",
        type: "switch",
      },
      {
        key: "urgency_rules",
        label: "档位与颜色",
        hint: "写成 天数:颜色，用逗号分隔；* 表示兜底档，颜色可写 emoji 或红/橙/绿/蓝。",
        type: "text",
        placeholder: "1:🟥,3:🟧,7:🟩,*:🟦",
      },
      {
        key: "tier_summary",
        label: "分段标题带档位统计",
        hint: "例如 ▎进行中（🟦5 | 🟩1）。",
        type: "switch",
      },
      {
        key: "headline_remaining",
        label: "标题里写剩余时间",
        hint: "把「剩余 3 天 4 小时」贴在活动名后面，避免一眼看不到。",
        type: "switch",
      },
      {
        key: "sort_by_urgency",
        label: "按紧急度排序",
        hint: "剩余时间少的排在前面。",
        type: "switch",
      },
    ],
  },
  {
    group: "正文内容",
    items: [
      {
        key: "include_description",
        label: "包含活动说明",
        hint: "过长的说明会被截断。",
        type: "switch",
      },
      {
        key: "include_tags",
        label: "包含标签",
        hint: "",
        type: "switch",
      },
      {
        key: "include_extra_times",
        label: "包含兑换结束时间",
        hint: "",
        type: "switch",
      },
      {
        key: "activity_separator",
        label: "活动之间的分隔线",
        hint: "留空则不加分隔线。",
        type: "text",
        placeholder: "━━━━━━━━━━━━",
      },
    ],
  },
  {
    group: "游戏显示名",
    items: [
      {
        key: "use_short_names",
        label: "转发节点用简称",
        hint: "关掉后节点名用完整游戏名。",
        type: "switch",
      },
      {
        key: "game_short_names",
        label: "游戏简称",
        hint: "每行一个，写成 代号=简称，例如 ys=原。",
        type: "textarea",
        rows: 4,
      },
      {
        key: "game_aliases",
        label: "游戏别名",
        hint: "每行一个，写成 代号=显示名，命令里可直接用这个叫法。",
        type: "textarea",
        rows: 4,
      },
    ],
  },
];

const templateState = {
  values: null,
  initial: null,
  preview: null,
  games: [],
  coverModes: ["image", "link", "off"],
  gameId: "",
  urgentGames: 3,
  timer: null,
};

templateState.elements = {
  button: document.getElementById("template-button"),
  modal: document.getElementById("template-modal"),
  close: document.getElementById("template-close"),
  controls: document.getElementById("template-controls"),
  preview: document.getElementById("template-preview"),
  note: document.getElementById("template-note"),
  reset: document.getElementById("template-reset"),
  save: document.getElementById("template-save"),
};

function setTemplateNote(message = "", isError = false) {
  templateState.elements.note.textContent = message;
  templateState.elements.note.classList.toggle("is-error", isError);
}

function renderField(item) {
  const value = templateState.values ? templateState.values[item.key] : "";
  if (item.type === "switch") {
    const checked = value ? " checked" : "";
    return [
      '<label class="toggle tmpl-toggle">',
      '  <input type="checkbox" data-tmpl-field="' + item.key + '"' + checked + " />",
      '  <span class="tmpl-toggle-text">',
      "    <strong>" + escapeHtml(item.label) + "</strong>",
      item.hint
        ? '    <span class="field-hint">' + escapeHtml(item.hint) + "</span>"
        : "",
      "  </span>",
      "</label>",
    ].join("");
  }
  if (item.type === "select") {
    const options = (item.options || templateState.coverModes)
      .map((option) => {
        const optionValue = typeof option === "string" ? option : option.value;
        const optionLabel =
          typeof option === "string"
            ? COVER_LABELS[option] || option
            : option.label;
        const selected = String(value) === String(optionValue) ? " selected" : "";
        return (
          '<option value="' + escapeHtml(optionValue) + '"' + selected + ">" +
          escapeHtml(optionLabel) +
          "</option>"
        );
      })
      .join("");
    return [
      '<div class="field-group">',
      '  <label class="field-label">' + escapeHtml(item.label) + "</label>",
      '  <select data-tmpl-field="' + item.key + '">' + options + "</select>",
      item.hint
        ? '  <span class="field-hint">' + escapeHtml(item.hint) + "</span>"
        : "",
      "</div>",
    ].join("");
  }
  if (item.type === "textarea") {
    return [
      '<div class="field-group">',
      '  <label class="field-label">' + escapeHtml(item.label) + "</label>",
      '  <textarea data-tmpl-field="' + item.key + '" rows="' + (item.rows || 3) + '">' +
      escapeHtml(value) +
      "</textarea>",
      item.hint
        ? '  <span class="field-hint">' + escapeHtml(item.hint) + "</span>"
        : "",
      "</div>",
    ].join("");
  }
  return [
    '<div class="field-group">',
    '  <label class="field-label">' + escapeHtml(item.label) + "</label>",
    '  <input type="text" data-tmpl-field="' + item.key + '" value="' +
    escapeHtml(value) +
    '" placeholder="' + escapeHtml(item.placeholder || "") + '" />',
    item.hint
      ? '  <span class="field-hint">' + escapeHtml(item.hint) + "</span>"
      : "",
    "</div>",
  ].join("");
}

function renderTemplateControls() {
  const gameOptions = [
    '<option value="">示例活动（离线）</option>',
  ]
    .concat(
      templateState.games.map(
        (game) =>
          '<option value="' + escapeHtml(game.game_id) + '"' +
          (templateState.gameId === game.game_id ? " selected" : "") +
          ">" +
          escapeHtml(game.name) +
          "</option>",
      ),
    )
    .join("");
  const gameField = [
    '<div class="field-group tmpl-game-field">',
    '  <label class="field-label" for="tmpl-game">预览数据</label>',
    '  <select id="tmpl-game">' + gameOptions + "</select>",
    '  <span class="field-hint">选具体游戏会去拉真实活动；默认用内置示例，四档颜色都能看到。</span>',
    "</div>",
  ].join("");
  const urgentOptions = [2, 3, 4, 5, 6]
    .map(
      (value) =>
        '<option value="' + value + '"' +
        (Number(templateState.urgentGames) === value ? " selected" : "") +
        ">" + value + " 个游戏</option>",
    )
    .join("");
  const urgentField = [
    '<div class="field-group tmpl-game-field">',
    '  <label class="field-label" for="tmpl-urgent-games">紧急提醒预览</label>',
    '  <select id="tmpl-urgent-games">' + urgentOptions + "</select>",
    '  <span class="field-hint">模拟这么多个游戏同时到期时，私聊提醒长什么样（示例数据，不拉真实活动）。</span>',
    '  <button class="ghost-button" type="button" data-tmpl-jump="urgent">看右侧的紧急提醒预览 ↓</button>',
    "</div>",
  ].join("");
  const sections = TEMPLATE_FIELDS.map((section) =>
    [
      '<section class="tmpl-group">',
      '  <h3 class="tmpl-group-title">' + escapeHtml(section.group) + "</h3>",
      section.items.map(renderField).join(""),
      "</section>",
    ].join(""),
  ).join("");
  templateState.elements.controls.innerHTML = gameField + urgentField + sections;
  bindTemplateInputs();
}

function bindTemplateInputs() {
  templateState.elements.controls
    .querySelectorAll("[data-tmpl-field]")
    .forEach((node) => {
      const eventName = node.type === "checkbox" || node.tagName === "SELECT"
        ? "change"
        : "input";
      node.addEventListener(eventName, scheduleTemplatePreview);
    });
  const gameSelect = document.getElementById("tmpl-game");
  if (gameSelect) {
    gameSelect.addEventListener("change", () => {
      templateState.gameId = gameSelect.value;
      refreshTemplatePreview();
    });
  }
  const jumpButton = templateState.elements.controls.querySelector(
    '[data-tmpl-jump="urgent"]',
  );
  if (jumpButton) jumpButton.addEventListener("click", scrollTemplateToUrgent);
  const urgentSelect = document.getElementById("tmpl-urgent-games");
  if (urgentSelect) {
    urgentSelect.addEventListener("change", () => {
      templateState.urgentGames = Number(urgentSelect.value) || 3;
      refreshTemplatePreview();
    });
  }
}

function collectTemplateValues() {
  const values = { ...(templateState.values || {}) };
  templateState.elements.controls
    .querySelectorAll("[data-tmpl-field]")
    .forEach((node) => {
      const key = node.dataset.tmplField;
      if (!key) return;
      values[key] = node.type === "checkbox" ? node.checked : node.value;
    });
  return values;
}

function scheduleTemplatePreview() {
  window.clearTimeout(templateState.timer);
  templateState.timer = window.setTimeout(() => {
    templateState.values = collectTemplateValues();
    refreshTemplatePreview();
  }, 300);
}

async function refreshTemplatePreview() {
  if (!bridge) return;
  if (!templateState.values) templateState.values = collectTemplateValues();
  templateState.elements.preview.classList.add("is-loading");
  try {
    const preview = await bridge.apiPost("template-preview", {
      values: templateState.values,
      game_id: templateState.gameId || "",
      urgent_games: templateState.urgentGames || 3,
    });
    templateState.preview = preview;
    renderTemplatePreview(preview);
    setTemplateNote(preview && preview.note ? preview.note : "");
  } catch (error) {
    setTemplateNote(error && error.message ? error.message : "预览失败", true);
  } finally {
    templateState.elements.preview.classList.remove("is-loading");
  }
}

function renderMessageFrame(preview, bodyHtml) {
  const sender = (preview && preview.sender) || {};
  const avatar = sender.avatar || "logo.png";
  const name = sender.name || "游戏活动日历";
  return [
    '<div class="qq-message">',
    '  <img class="qq-avatar" src="./' + escapeHtml(avatar) + '" alt="" />',
    '  <div class="qq-message-main">',
    '    <div class="qq-sender-name">' + escapeHtml(name) + '</div>',
    bodyHtml,
    '  </div>',
    '</div>',
  ].join("");
}

function renderPlainSection(section) {
  if (section.kind === "heading") {
    return '<div class="qq-heading">' + escapeHtml(section.text) + "</div>";
  }
  if (section.kind === "separator") {
    return '<div class="qq-separator">' + escapeHtml(section.text) + "</div>";
  }
  if (section.kind === "note") {
    return '<div class="qq-note">' + escapeHtml(section.text) + "</div>";
  }
  const lines = (section.lines || [])
    .map((line) => '<div class="qq-line">' + escapeHtml(line) + "</div>")
    .join("");
  const image = section.image
    ? '<img class="qq-cover" src="' + escapeHtml(section.image) + '" alt="" />'
    : "";
  return '<div class="qq-activity">' + lines + image + "</div>";
}

function renderPlainBubble(preview) {
  const plain = (preview && preview.plain) || {};
  const body = (plain.sections || []).map(renderPlainSection).join("");
  const bubble = [
    '<div class="qq-bubble">',
    '  <div class="qq-bubble-title">' + escapeHtml(plain.header || "") + "</div>",
    body,
    "</div>",
  ].join("");
  return renderMessageFrame(preview, bubble);
}

function renderForwardBubble(preview) {
  const forward = (preview && preview.forward) || {};
  const nodes = [];
  if (forward.title_node) nodes.push(forward.title_node);
  (forward.nodes || []).forEach((node) => nodes.push(node));
  const rows = nodes
    .map((node) => {
      const firstLine = (node.lines || [])[0] || "";
      return [
        '<li class="qq-forward-row">',
        '  <span class="qq-forward-name">' + escapeHtml(node.name || "") + "</span>",
        '  <span class="qq-forward-text">' + escapeHtml(firstLine) + "</span>",
        "</li>",
      ].join("");
    })
    .join("");
  const card = [
    '<div class="qq-forward-card">',
    '  <div class="qq-forward-title">' +
      escapeHtml(forward.card_title || "群聊的聊天记录") +
      "</div>",
    '  <ul class="qq-forward-list">' + rows + "</ul>",
    '  <div class="qq-forward-foot">' + escapeHtml(forward.footer || "") + "</div>",
    "</div>",
  ].join("");
  return renderMessageFrame(preview, card);
}

function renderUrgentPreview(preview, urgent) {
  if (!urgent || !urgent.nodes || !urgent.nodes.length) return "";
  const sender = (preview && preview.sender) || {};
  const lines = (urgent.plain_lines || [])
    .map((line) => '<div class="qq-line">' + escapeHtml(line) + "</div>")
    .join("");
  const plain = renderMessageFrame(
    preview,
    '<div class="qq-bubble">' + lines + "</div>",
  );
  const forward = renderForwardBubble({
    sender: sender,
    forward: {
      card_title: "群聊的聊天记录",
      title_node: urgent.nodes[0],
      nodes: urgent.nodes.slice(1),
      footer: "查看" + urgent.nodes.length + "条转发消息",
    },
  });
  return [
    '<section class="tmpl-urgent">',
    '  <div class="tmpl-urgent-head">',
    '    <span class="tmpl-pane-title">紧急提醒 · 模拟 ' +
      escapeHtml(String(urgent.count || 0)) +
      " 个游戏同时到期</span>",
    '    <span class="field-hint">示例数据，不拉真实活动；剩余时间刻意拉开四档，方便对比颜色。</span>',
    "  </div>",
    '  <div class="tmpl-urgent-panes">',
    '    <div class="tmpl-pane">',
    '      <div class="tmpl-pane-title">普通消息</div>',
    plain,
    "    </div>",
    '    <div class="tmpl-pane">',
    '      <div class="tmpl-pane-title">合并转发</div>',
    forward,
    "    </div>",
    "  </div>",
    "</section>",
  ].join("");
}

function templateUrgentNode() {
  return templateState.elements.preview.querySelector(".tmpl-urgent");
}

function scrollTemplateToUrgent() {
  const target = templateUrgentNode();
  if (!target) return;
  const preview = templateState.elements.preview;
  const top =
    preview.scrollTop +
    target.getBoundingClientRect().top -
    preview.getBoundingClientRect().top -
    8;
  preview.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
}

function updateTemplateScrollHint() {
  const preview = templateState.elements.preview;
  const hint = document.getElementById("tmpl-scroll-hint");
  if (!hint) return;
  const target = templateUrgentNode();
  if (!target) {
    hint.classList.add("is-hidden");
    return;
  }
  const box = preview.getBoundingClientRect();
  // 紧急提醒区顶边已经进入可见范围就撤掉引导；滚回上方或切游戏重渲染时自动回来
  hint.classList.toggle("is-hidden", target.getBoundingClientRect().top < box.bottom - 48);
}

templateState.elements.preview.addEventListener("scroll", updateTemplateScrollHint, {
  passive: true,
});

templateState.elements.preview.addEventListener("click", (event) => {
  if (event.target.closest("#tmpl-scroll-hint")) scrollTemplateToUrgent();
});

function renderTemplatePreview(preview) {
  templateState.elements.preview.innerHTML = [
    '<div class="tmpl-pane">',
    '  <div class="tmpl-pane-title">普通消息</div>',
    renderPlainBubble(preview),
    "</div>",
    '<div class="tmpl-pane">',
    '  <div class="tmpl-pane-title">合并转发</div>',
    renderForwardBubble(preview),
    "</div>",
    renderUrgentPreview(preview, preview && preview.urgent),
    '<button class="tmpl-scroll-hint" id="tmpl-scroll-hint" type="button">',
    '  <span>下面还有「紧急提醒」预览，点我跳过去</span>',
    '  <span aria-hidden="true">↓</span>',
    "</button>",
  ].join("");
  updateTemplateScrollHint();
}

function applyTemplateValues(values) {
  templateState.values = { ...values };
  renderTemplateControls();
}

async function openTemplate() {
  if (!bridge) {
    bridgeMissing();
    return;
  }
  templateState.elements.modal.classList.remove("is-hidden");
  if (templateState.values) {
    renderTemplateControls();
    return;
  }
  setTemplateNote("正在读取当前模板…");
  try {
    const data = await bridge.apiGet("template");
    templateState.values = (data && data.values) || {};
    templateState.initial = { ...templateState.values };
    templateState.games = (data && data.games) || [];
    templateState.coverModes =
      (data && data.cover_modes) || ["image", "link", "off"];
    templateState.gameId = "";
    renderTemplateControls();
    await refreshTemplatePreview();
  } catch (error) {
    setTemplateNote(error && error.message ? error.message : "读取模板失败", true);
  }
}

function closeTemplate() {
  templateState.elements.modal.classList.add("is-hidden");
}

function resetTemplate() {
  if (!templateState.initial) return;
  applyTemplateValues(templateState.initial);
  refreshTemplatePreview();
  showToast("已还原为保存前的设置");
}

async function saveTemplate() {
  if (!bridge) return;
  const values = collectTemplateValues();
  templateState.values = values;
  templateState.elements.save.classList.add("is-loading");
  try {
    const result = await bridge.apiPost("template-save", { values });
    templateState.values = (result && result.values) || values;
    templateState.initial = { ...templateState.values };
    renderTemplateControls();
    showToast("模板已保存，下一条推送就按这个样式");
    await refreshTemplatePreview();
  } catch (error) {
    showToast(error && error.message ? error.message : "保存失败", true);
  } finally {
    templateState.elements.save.classList.remove("is-loading");
  }
}

templateState.elements.button.addEventListener("click", openTemplate);
templateState.elements.close.addEventListener("click", closeTemplate);
templateState.elements.reset.addEventListener("click", resetTemplate);
templateState.elements.save.addEventListener("click", saveTemplate);
templateState.elements.modal.addEventListener("click", (event) => {
  if (event.target === templateState.elements.modal) closeTemplate();
});

// ---- 数据源与代理设置 ----

const SOURCE_HINTS = {
  sra: "境外站点，建议走代理",
  akedata: "国内站点，走直连更快",
  prts: "国内站点，走直连更快",
};

const settingsState = {
  sources: [],
  gameSources: [],
  network: { proxy: "", direct_hosts: "", default_direct_hosts: "" },
  initial: null,
  elements: {
    button: document.getElementById("settings-button"),
    modal: document.getElementById("settings-modal"),
    close: document.getElementById("settings-close"),
    body: document.getElementById("settings-body"),
    note: document.getElementById("settings-note"),
    reset: document.getElementById("settings-reset"),
    save: document.getElementById("settings-save"),
  },
};

function setSettingsNote(message = "", isError = false) {
  settingsState.elements.note.textContent = message;
  settingsState.elements.note.classList.toggle("is-error", isError);
}

function renderSettings() {
  const sourceItems = settingsState.sources
    .map((source) =>
      [
        '<label class="toggle tmpl-toggle">',
        '  <input type="checkbox" data-settings-source="' +
          escapeHtml(source.id) +
          '"' +
          (source.enabled ? " checked" : "") +
          " />",
        '  <span class="tmpl-toggle-text">',
        "    <strong>" + escapeHtml(source.name) + "</strong>",
        '    <span class="field-hint">' +
          escapeHtml(SOURCE_HINTS[source.id] || "数据源开关") +
          "</span>",
        "  </span>",
        "</label>",
      ].join(""),
    )
    .join("");

  const gameItems = settingsState.gameSources
    .map((game) => {
      const options = game.options
        .map(
          (option) =>
            '<option value="' +
            escapeHtml(option.id) +
            '"' +
            (option.id === game.source_id ? " selected" : "") +
            ">" +
            escapeHtml(option.name) +
            (option.enabled ? "" : "（已关闭）") +
            "</option>",
        )
        .join("");
      return [
        '<div class="field-group">',
        '  <label class="field-label">' +
          escapeHtml(game.name) +
          " 的数据源</label>",
        '  <select data-settings-game="' + escapeHtml(game.game_id) + '">' +
          options +
          "</select>",
        '  <span class="field-hint">当前生效：' +
          escapeHtml(game.current || "（无可用数据源）") +
          "</span>",
        "</div>",
      ].join("");
    })
    .join("");

  const network = settingsState.network || {};
  const networkItems = [
    '<div class="field-group">',
    '  <label class="field-label">代理地址</label>',
    '  <input type="text" id="settings-proxy" value="' +
      escapeHtml(network.proxy || "") +
      '" placeholder="http://127.0.0.1:7890" />',
    '  <span class="field-hint">留空表示不使用代理；支持 http / socks5。</span>',
    "</div>",
    '<div class="field-group">',
    '  <label class="field-label">直连域名</label>',
    '  <textarea id="settings-direct" rows="3" placeholder="' +
      escapeHtml(network.default_direct_hosts || "") +
      '">' +
      escapeHtml(network.direct_hosts || "") +
      "</textarea>",
    '  <span class="field-hint">这些域名即使配了代理也直连；清空表示全部走代理。</span>',
    "</div>",
  ].join("");

  settingsState.elements.body.innerHTML = [
    '<section class="tmpl-group">',
    '  <h3 class="tmpl-group-title">数据源</h3>',
    sourceItems,
    '  <p class="field-hint">至少要保留一个数据源；关掉某个源会同时移除它提供的游戏。</p>',
    "</section>",
    gameItems ? '<section class="tmpl-group">' : "",
    gameItems ? '  <h3 class="tmpl-group-title">同一游戏用哪个源</h3>' : "",
    gameItems,
    gameItems ? "</section>" : "",
    '<section class="tmpl-group">',
    '  <h3 class="tmpl-group-title">网络</h3>',
    networkItems,
    "</section>",
  ].join("");
}

function collectSettings() {
  const enabled = {};
  settingsState.elements.body
    .querySelectorAll("[data-settings-source]")
    .forEach((node) => {
      enabled[node.dataset.settingsSource] = node.checked;
    });
  const gameSources = {};
  settingsState.elements.body
    .querySelectorAll("[data-settings-game]")
    .forEach((node) => {
      gameSources[node.dataset.settingsGame] = node.value;
    });
  const proxyNode = document.getElementById("settings-proxy");
  const directNode = document.getElementById("settings-direct");
  return {
    enabled,
    game_sources: gameSources,
    proxy: proxyNode ? proxyNode.value : "",
    direct_hosts: directNode ? directNode.value : "",
  };
}

function applySettings(data) {
  settingsState.sources = (data && data.sources) || [];
  settingsState.gameSources = (data && data.game_sources) || [];
  settingsState.network = (data && data.network) || {};
  renderSettings();
}

async function openSettings() {
  if (!bridge) return;
  settingsState.elements.modal.classList.remove("is-hidden");
  setSettingsNote("正在读取当前设置…");
  try {
    const data = await bridge.apiGet("settings");
    applySettings(data);
    settingsState.initial = collectSettings();
    setSettingsNote("");
  } catch (error) {
    setSettingsNote(
      error && error.message ? error.message : "读取设置失败",
      true,
    );
  }
}

function closeSettings() {
  settingsState.elements.modal.classList.add("is-hidden");
}

async function resetSettings() {
  if (!bridge) return;
  try {
    const data = await bridge.apiGet("settings");
    applySettings(data);
    settingsState.initial = collectSettings();
    showToast("已还原为当前生效的设置");
    setSettingsNote("");
  } catch (error) {
    setSettingsNote(
      error && error.message ? error.message : "读取设置失败",
      true,
    );
  }
}

async function saveSettings() {
  if (!bridge) return;
  const payload = collectSettings();
  settingsState.elements.save.classList.add("is-loading");
  try {
    const result = await bridge.apiPost("settings-save", payload);
    applySettings(result);
    setSettingsNote("");
    showToast("数据源设置已保存并生效");
    await loadOverview();
  } catch (error) {
    setSettingsNote(error && error.message ? error.message : "保存失败", true);
  } finally {
    settingsState.elements.save.classList.remove("is-loading");
  }
}

settingsState.elements.button.addEventListener("click", openSettings);
settingsState.elements.close.addEventListener("click", closeSettings);
settingsState.elements.reset.addEventListener("click", resetSettings);
settingsState.elements.save.addEventListener("click", saveSettings);
settingsState.elements.modal.addEventListener("click", (event) => {
  if (event.target === settingsState.elements.modal) closeSettings();
});


// ---- 紧急私聊提醒 ----

const URGENT_FIELD_LABELS = {
  trigger_days: "触发档位（剩余不足几天）",
  repeat_minutes: "提醒间隔（分钟）",
  daily_limit: "每天最多几次",
};

const URGENT_ACTIVITY_LIMIT = 5;

const urgentState = {
  data: null,
  elements: {
    button: document.getElementById("urgent-button"),
    modal: document.getElementById("urgent-modal"),
    close: document.getElementById("urgent-close"),
    list: document.getElementById("urgent-list"),
    note: document.getElementById("urgent-note"),
    reload: document.getElementById("urgent-reload"),
    newUmo: document.getElementById("urgent-new-umo"),
    add: document.getElementById("urgent-add-button"),
  },
};

function setUrgentNote(message = "", isError = false) {
  urgentState.elements.note.textContent = message;
  urgentState.elements.note.classList.toggle("is-error", isError);
}

function urgentOverrideValue(target, key) {
  const current = (target.settings || {})[key];
  // 空字符串表示「没有覆盖，跟随全局」
  return current === undefined || current === null ? "" : String(current);
}

function renderUrgentCard(target) {
  const data = urgentState.data || {};
  const limits = data.limits || {};
  const defaults = data.defaults || {};
  const fields = Object.keys(URGENT_FIELD_LABELS)
    .map((key) => {
      const range = limits[key] || [];
      const placeholder = "跟随全局 " + defaults[key];
      const bound = range.length === 2
        ? ' min="' + range[0] + '" max="' + range[1] + '"'
        : "";
      return [
        '<div class="urgent-field">',
        '  <label class="field-label">' + escapeHtml(URGENT_FIELD_LABELS[key]) + "</label>",
        '  <input type="number" data-urgent-field="' + key + '" value="' +
          escapeHtml(urgentOverrideValue(target, key)) + '" placeholder="' +
          escapeHtml(placeholder) + '"' + bound + " />",
        "</div>",
      ].join("");
    })
    .join("");

  const chosen = new Set(target.games || []);
  const gameChips = (data.games || [])
    .map((game) =>
      [
        '<label class="urgent-game">',
        '  <input type="checkbox" data-urgent-game="' + escapeHtml(game.id) + '"' +
          (chosen.has(game.id) ? " checked" : "") + " />",
        "  <span>" + escapeHtml(game.name) + "</span>",
        "</label>",
      ].join(""),
    )
    .join("");

  const items = target.activities || [];
  const ackedCount = items.filter((item) => item.acked).length;
  const activityItem = (item) =>
    [
      '<li class="urgent-activity">',
      '  <span class="urgent-activity-name">' +
        escapeHtml(item.name || item.uid || "") + "</span>",
      '  <span class="field-hint">' + escapeHtml(item.end || "") + "</span>",
      item.acked
        ? '  <span class="urgent-tag is-acked">已确认</span>' +
          '  <button class="ghost-button urgent-ack-clear" type="button" data-urgent-ack="' +
          escapeHtml(item.uid || "") + '">解除</button>'
        : '  <span class="urgent-tag">已催 ' +
          escapeHtml(String(item.sent_total || 0)) + " 次</span>",
      "</li>",
    ].join("");
  const headActivities = items
    .slice(0, URGENT_ACTIVITY_LIMIT)
    .map(activityItem)
    .join("");
  const restItems = items.slice(URGENT_ACTIVITY_LIMIT);
  const restActivities = restItems.map(activityItem).join("");

  return [
    '<article class="urgent-card" data-urgent-card="' + escapeHtml(target.umo) + '">',
    '  <header class="urgent-card-head">',
    '    <div class="urgent-card-title">',
    "      <strong>" + escapeHtml(target.session_name) + "</strong>",
    '      <span class="field-hint">' + escapeHtml(target.umo) + "</span>",
    "    </div>",
    '    <div class="urgent-card-actions">',
    '      <label class="toggle tmpl-toggle urgent-switch">',
    '        <input type="checkbox" data-urgent-enabled="1"' +
          (target.enabled ? " checked" : "") + " />",
    '        <span class="tmpl-toggle-text"><strong>启用</strong></span>',
    "      </label>",
      ackedCount
        ? '      <button class="ghost-button" type="button" data-urgent-ack-clear="1">解除确认（' +
          escapeHtml(String(ackedCount)) + "）</button>"
        : "",
    '      <button class="ghost-button" type="button" data-urgent-save="1">保存</button>',
    '      <button class="ghost-button is-danger" type="button" data-urgent-remove="1">移除</button>',
    "    </div>",
    "  </header>",
    '  <p class="urgent-effective">当前生效：' +
      escapeHtml(target.describe || "") + "</p>",
    '  <div class="urgent-fields">' + fields + "</div>",
    '  <details class="urgent-games">',
    "    <summary>限定游戏（都不勾 = 跟随全局与订阅）</summary>",
    '    <div class="urgent-game-list">' + gameChips + "</div>",
    "  </details>",
    items.length
      ? '  <ul class="urgent-activities">' + headActivities + "</ul>" +
        (restItems.length
          ? '  <details class="urgent-more"><summary>还有 ' +
            escapeHtml(String(restItems.length)) +
            ' 条记录，展开查看 ↓</summary><ul class="urgent-activities">' +
            restActivities +
            "</ul></details>"
          : "")
      : '  <p class="field-hint">还没有催过活动；一旦有活动进入档位，这里会列出进度。</p>',
    "</article>",
  ].join("");
}

function applyUrgent(data) {
  urgentState.data = data || {};
  const defaults = urgentState.data.defaults || {};
  const targets = urgentState.data.targets || [];
  const summary = [
    '<p class="urgent-summary">',
    "  全局默认 " + escapeHtml(String(defaults.trigger_days)) + " 天内开始，每 " +
      escapeHtml(String(defaults.repeat_minutes)) + " 分钟最多一次，每天 " +
      escapeHtml(String(defaults.daily_limit)) + " 次；目标共 " +
      escapeHtml(String(targets.length)) + " 个。",
    urgentState.data.enabled ? "" : "  <strong>全局开关是关闭的</strong>",
    "</p>",
  ].join("");
  const cards = targets.map(renderUrgentCard).join("");
  urgentState.elements.list.innerHTML =
    summary +
    (cards ||
      '<p class="field-hint">还没有任何提醒目标。可以在群里用 /活动私人提醒 @某人 添加，或在上面的输入框里填 UMO。</p>');
}

function collectUrgentCard(card) {
  const settings = {};
  card.querySelectorAll("[data-urgent-field]").forEach((node) => {
    const key = node.dataset.urgentField;
    const raw = (node.value || "").trim();
    settings[key] = raw === "" ? null : Number(raw);
  });
  const games = [];
  card.querySelectorAll("[data-urgent-game]").forEach((node) => {
    if (node.checked) games.push(node.dataset.urgentGame);
  });
  const enabledNode = card.querySelector("[data-urgent-enabled]");
  return {
    umo: card.dataset.urgentCard,
    enabled: !!(enabledNode && enabledNode.checked),
    settings: settings,
    games: games,
  };
}

async function loadUrgent() {
  if (!bridge) return;
  setUrgentNote("正在读取…");
  try {
    applyUrgent(await bridge.apiGet("urgent-targets"));
    setUrgentNote("");
  } catch (error) {
    setUrgentNote(
      error && error.message ? error.message : "读取紧急提醒配置失败",
      true,
    );
  }
}

async function openUrgent() {
  if (!bridge) {
    bridgeMissing();
    return;
  }
  urgentState.elements.modal.classList.remove("is-hidden");
  await loadUrgent();
}

function closeUrgent() {
  urgentState.elements.modal.classList.add("is-hidden");
}

async function submitUrgent(payload, successMessage) {
  const data = await bridge.apiPost("urgent-save", payload);
  applyUrgent(data);
  setUrgentNote("");
  showToast(successMessage);
}

async function saveUrgentCard(card, button) {
  if (!bridge) return;
  button.classList.add("is-loading");
  try {
    const payload = collectUrgentCard(card);
    await submitUrgent(payload, "已保存 " + payload.umo);
  } catch (error) {
    setUrgentNote(error && error.message ? error.message : "保存失败", true);
    showToast("保存失败", true);
  } finally {
    button.classList.remove("is-loading");
  }
}

function armUrgentRemove(button, card) {
  // 卡片里的「移除」同样不能依赖原生对话框，见 armDangerButton 的说明。
  armDangerButton(button, "再点一次移除", () => removeUrgentCard(card));
}

async function removeUrgentCard(card) {
  if (!bridge) return;
  const umo = card.dataset.urgentCard;
  try {
    await submitUrgent({ umo: umo, remove: true }, "已移除 " + umo);
  } catch (error) {
    const message = error && error.message ? error.message : "移除失败";
    setUrgentNote(message, true);
    showToast("移除失败", true);
  }
}

async function clearUrgentAck(card, uid, button) {
  if (!bridge) return;
  button.classList.add("is-loading");
  try {
    await submitUrgent(
      { umo: card.dataset.urgentCard, reset_ack: true, activity_id: uid },
      uid ? "已解除这个活动的确认" : "已解除全部确认",
    );
  } catch (error) {
    setUrgentNote(error && error.message ? error.message : "解除确认失败", true);
    showToast("解除确认失败", true);
  } finally {
    button.classList.remove("is-loading");
  }
}

async function addUrgentTarget() {
  if (!bridge) return;
  const umo = (urgentState.elements.newUmo.value || "").trim();
  if (!umo) {
    setUrgentNote("先填一个目标，例如 aiocqhttp:FriendMessage:123456", true);
    return;
  }
  urgentState.elements.add.classList.add("is-loading");
  try {
    await submitUrgent({ umo: umo, enabled: true, games: [] }, "已添加 " + umo);
    urgentState.elements.newUmo.value = "";
  } catch (error) {
    setUrgentNote(error && error.message ? error.message : "添加失败", true);
  } finally {
    urgentState.elements.add.classList.remove("is-loading");
  }
}

urgentState.elements.list.addEventListener("click", (event) => {
  const card = event.target.closest("[data-urgent-card]");
  if (!card) return;
  const saveButton = event.target.closest("[data-urgent-save]");
  if (saveButton) {
    saveUrgentCard(card, saveButton);
    return;
  }
  const ackButton = event.target.closest("[data-urgent-ack]");
  if (ackButton) {
    clearUrgentAck(card, ackButton.dataset.urgentAck || "", ackButton);
    return;
  }
  const ackAllButton = event.target.closest("[data-urgent-ack-clear]");
  if (ackAllButton) {
    clearUrgentAck(card, "", ackAllButton);
    return;
  }
  const removeButton = event.target.closest("[data-urgent-remove]");
  if (removeButton) armUrgentRemove(removeButton, card);
});

urgentState.elements.button.addEventListener("click", openUrgent);
urgentState.elements.close.addEventListener("click", closeUrgent);
urgentState.elements.reload.addEventListener("click", loadUrgent);
urgentState.elements.add.addEventListener("click", addUrgentTarget);
urgentState.elements.modal.addEventListener("click", (event) => {
  if (event.target === urgentState.elements.modal) closeUrgent();
});

elements.refreshButton.addEventListener("click", () =>
  loadOverview({ announce: true }),
);
elements.cycleButton.addEventListener("click", runCycle);

elements.sessionSearch.addEventListener("input", (event) => {
  state.search = event.target.value;
  renderSessionList();
});

async function start() {
  if (!bridge) {
    bridgeMissing();
    return;
  }
  try {
    const context = await bridge.ready();
    document.title =
      bridge.t?.("pages.activity.title", "游戏活动订阅管理") ||
      "游戏活动订阅管理";
    document.documentElement.dataset.theme = context?.isDark ? "dark" : "light";
    bridge.onContext?.((nextContext) => {
      const current = nextContext || bridge.getContext?.();
      document.documentElement.dataset.theme = current?.isDark
        ? "dark"
        : "light";
    });
  } catch (error) {
    /* 上下文获取失败不影响主要功能 */
  }
  await loadOverview();
}

start().catch((error) => {
  setError(error?.message || "页面初始化失败");
});
