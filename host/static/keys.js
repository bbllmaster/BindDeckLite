/* BindDeckLite 按键编辑：摘要卡辅助 + 抽屉 + 宏双视图。
 * 普通 <script> 在 app.js 之前加载（无构建步骤）；只声明函数，
 * 运行期才读取 app.js 里的 state / marker 常量。
 */

/* ---------------- 摘要卡 ---------------- */

function keyOptions(selected) {
  // 键码 0 = 「不发键」，单独收进「特殊」分组，普通键码的顺序不变。
  const opt = (k) =>
    `<option value="${k.code}" ${k.code === selected ? "selected" : ""}>${k.label}</option>`;
  const normal = state.keycodes.filter((k) => k.code !== 0);
  const special = state.keycodes.filter((k) => k.code === 0);
  let html = normal.map(opt).join("");
  if (special.length) {
    html += `<optgroup label="${t("keys.special_group")}">${special.map(opt).join("")}</optgroup>`;
  }
  return html;
}

function keyActionSummary(action) {
  const type = (action && action.type) || "none";
  if (type === "none") return t("keys.act_none");
  if (type === "macro") {
    const n = Array.isArray(action.macro) ? action.macro.length : 0;
    return t("keys.macro_summary", { count: n });
  }
  if (type === "launch") return t("keys.act_launch");
  if (type === "url") return t("ui.act_url");
  if (type === "command") return t("ui.act_command");
  if (type === "media") return t("ui.act_media");
  const known = ACTION_TYPES.find((a) => a.id === type);
  return known ? actionName(known) : type;
}

function keyItem(idx) {
  const keys = state.cfg && Array.isArray(state.cfg.keys) ? state.cfg.keys : null;
  if (!keys) return null;
  return keys[idx] || null;
}

/* ---------------- 修饰键（WS-546） ----------------
 * 顺序、位值和文案都由 /api/keycodes 的 mods 列表给出（Ctrl→Shift→Alt→Win），
 * 这里只按位过滤 —— 前端不再写第二份顺序规则。 */
function modDefs() {
  return Array.isArray(state.modDefs) ? state.modDefs : [];
}

/** 掩码 → ["Ctrl", "Shift"]（按服务器给的顺序）。 */
function keyModsText(mask) {
  const bits = parseInt(mask, 10) || 0;
  return modDefs().filter((m) => bits & m.bit).map((m) => m.label);
}

/** 摘要卡上的 HID 文案：Ctrl + Shift + Esc；键码 0 一律「不发键」。
 *  WS-550：门控为 False 时 apply_device() 一条 CFG:MODS 都不发，摘要不能再
 *  显示修饰键（否则「显示 Ctrl、实际只发 F13」）。state.cfg 可能为空（未选
 *  设备），此时以及 unknown / supported 时都保持原有修饰键显示。 */
function keyHidText(item) {
  const code = parseInt(item && item.code, 10);
  if (code === 0) return t("keys.key_none");
  const known = state.keycodes.find((k) => k.code === code);
  const base = (item && item.label) || (known && known.label) || String(code);
  const unsupported = state.cfg && state.cfg.mods_supported === false;
  if (unsupported) return base;          // 显示 == 实际下发：只显示主键
  return keyModsText(item && item.mods).concat([base]).join(" + ");
}

/** 只改摘要卡上的三处文本，不重建卡片（抽屉与焦点不会被冲掉）。 */
function refreshKeyCard(idx) {
  const card = $(`.keycard[data-idx="${idx}"]`);
  const item = keyItem(idx);
  if (!card || !item) return;
  const hid = keyHidText(item);
  const hidEl = $(".kc-hid", card);
  const actEl = $(".kc-act", card);
  if (hidEl) hidEl.textContent = hid;
  if (actEl) actEl.textContent = keyActionSummary(item.action);
  const noAction = !item.action || (item.action.type || "none") === "none";
  card.dataset.noaction = noAction ? "1" : "0";
  if (typeof setCardRunning === "function") setCardRunning(card, undefined, undefined);
}

/* ---------------- 测试动作 ---------------- */

/** 摘要卡与抽屉共用；「仅 HID」直接说明原因，不再发请求拿假成功。 */
async function testKeyAction(item, idx, card) {
  const devId = state.selected;
  const running = devId ? (state.running[devId] || {})[String(idx)] : undefined;
  if (running !== undefined) {
    // 执行中：这颗按钮（摘要卡或抽屉）就是止损入口，不是重入。
    stopAction(devId, idx);
    return;
  }
  const noAction = !item.action || (item.action.type || "none") === "none";
  if (noAction) {
    toast(t("ui.test_none"), "warn");
    return;
  }
  if (!devId) {
    toast(t("ui.no_device_selected"), "bad");
    return;
  }
  let armTimer = null;
  armTimer = setTimeout(() => {
    markRunning(devId, idx);
    applyRunningState();
  }, 300);
  let r = null;
  try {
    r = await api(`/api/action/test/${encodeURIComponent(devId)}/${idx}`, { method: "POST" });
    if (r.ok) {
      const used = r.browser_used && r.browser_used !== "default" ? ` · ${r.browser_used}` : "";
      toast(t("ui.action_ok_prefix") + (item.action.type || "") + used, "ok");
    } else if (r.cancelled) {
      toast(t("ui.stopped"), "");
    } else if (r.running) {
      markRunning(devId, idx, r.elapsed || 0);
      applyRunningState();
      toast(r.error || t("log.action_still_running_key"), "warn");
    } else {
      toast(t("ui.action_failed_prefix") + (r.error || t("ui.unknown_error")), "bad");
    }
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    clearTimeout(armTimer);
    if (!(r && r.running)) {
      clearRunning(devId, idx);
      applyRunningState();
    }
  }
}

/* ---------------- 抽屉 / 检查器 ---------------- */

/** 在键位列表里标出正在编辑的那一行；宽屏检查器与窄屏抽屉共用同一个下标。 */
function syncKeySelection() {
  const idx = state.editingKey;
  $$(".keycard").forEach((card) => {
    card.classList.toggle(
      "selected", idx !== null && parseInt(card.dataset.idx, 10) === idx);
  });
}

function bindKeyDrawer() {
  const drawer = $("#keyDrawer");
  if (!drawer) return;
  $("#btnKeyDrawerClose").onclick = () => closeKeyDrawer();
  $("#keyDrawerBackdrop").onclick = () => closeKeyDrawer();
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || state.editingKey === null) return;
    const help = $("#helpModal");
    const ota = $("#otaModal");
    if ((help && !help.hidden) || (ota && !ota.hidden)) return;
    closeKeyDrawer();
  });
}

function openKeyDrawer(idx) {
  if (!state.cfg || !Array.isArray(state.cfg.keys)) return;
  if (idx < 0 || idx >= inputCount()) return;
  const card = $(`.keycard[data-idx="${idx}"]`);
  state.keyDrawerReturn = card ? $(".edit", card) : null;
  state.editingKey = idx;
  state.macroView = "steps";
  renderKeyDrawer();
  syncKeySelection();
  $("#keyDrawer").classList.add("open");
  $("#keyDrawerBackdrop").hidden = false;
  document.body.classList.add("drawer-open");
  // WS-546: 修饰键能力还未知时问一次设备（旧固件超时后整行禁用并提示）。
  if (typeof ensureModsCapability === "function") ensureModsCapability();
  const first = $("#kdHid");
  if (first) first.focus({ preventScroll: true });
}

function closeKeyDrawer(restoreFocus = true) {
  if (state.editingKey === null) return;
  const idx = state.editingKey;
  const fallback = $(`.keycard[data-idx="${idx}"] .edit`);
  const target = state.keyDrawerReturn && state.keyDrawerReturn.isConnected
    ? state.keyDrawerReturn : fallback;
  state.editingKey = null;
  state.keyDrawerReturn = null;
  syncKeySelection();
  $("#keyDrawer").classList.remove("open");
  $("#keyDrawerBackdrop").hidden = true;
  document.body.classList.remove("drawer-open");
  renderKeyDrawer();
  if (restoreFocus && target) target.focus({ preventScroll: true });
}

/** 当前挂载的宏编辑器的「只换文案」入口；WS-596 切语言时用，见 retranslateKeyDrawer。 */
let macroEditorRetranslate = null;

function renderKeyDrawer() {
  const drawer = $("#keyDrawer");
  const body = $("#keyDrawerBody");
  const title = $("#keyDrawerTitle");
  if (!drawer || !body) return;
  // 表单整块重建，旧的宏编辑器随之作废（动作类型换成非宏时没有新的可挂）。
  macroEditorRetranslate = null;
  const idx = state.editingKey;
  const item = idx === null ? null : keyItem(idx);
  if (!item) {
    title.textContent = t("ui.key_editor");
    body.innerHTML = `<p class="muted small" data-i18n="ui.key_editor_hint">${t("ui.key_editor_hint")}</p>`;
    drawer.classList.remove("open");
    $("#keyDrawerBackdrop").hidden = true;
    document.body.classList.remove("drawer-open");
    return;
  }

  const action = item.action || { type: "none", value: "", macro: [] };
  title.textContent = inputTitle(idx);
  // WS-546: 修饰键与键码同卡。旧固件（不支持 CFG:MODS）时整行禁用并说明原因，
  // 而不是让用户勾完再被静默忽略 —— 也不能只靠版本号判断，能力由
  // /api/device/<id>/mods 探测后写进 cfg.mods_supported。
  const mods = modDefs();
  const mask = parseInt(item.mods, 10) || 0;
  const modsBlocked = (state.cfg || {}).mods_supported === false;
  const modsRow = mods.length ? `
    <div class="kd-field">
      <span class="field-label" data-i18n="keys.mods_label">${t("keys.mods_label")}</span>
      <div class="kd-mods" role="group" aria-label="${t("keys.mods_label")}" data-i18n-aria-label="keys.mods_label">
        ${mods.map((m) => `
          <label class="kd-mod">
            <input type="checkbox" id="${m.id}" data-bit="${m.bit}"
                   ${(mask & m.bit) ? "checked" : ""} ${modsBlocked ? "disabled" : ""}>
            <span>${escapeHtml(m.label)}</span>
          </label>`).join("")}
      </div>
      ${modsBlocked ? `<p class="muted small" id="kdModsHint" data-i18n="keys.mods_unsupported">${t("keys.mods_unsupported")}</p>
        <button type="button" class="btn secondary sm" id="kdModsRedetect" data-i18n="keys.mods_redetect">${t("keys.mods_redetect")}</button>` : ""}
    </div>` : "";
  body.innerHTML = `
    <div class="kd-field">
      <label class="field-label" for="kdHid" data-i18n="keys.hid_label">${t("keys.hid_label")}</label>
      <select id="kdHid" class="sel-key">${keyOptions(item.code)}</select>
    </div>${modsRow}
    <div class="kd-field">
      <label class="field-label" for="kdAct" data-i18n="keys.action_label">${t("keys.action_label")}</label>
      <select id="kdAct" class="sel-act">
        ${ACTION_TYPES.map((a) => `<option value="${a.id}" ${a.id === action.type ? "selected" : ""} data-i18n="${a.nameKey}">${actionName(a)}</option>`).join("")}
      </select>
    </div>
    <div class="kd-field">
      <label class="field-label" id="kdValueLabel" for="kdValue" data-i18n="keys.action_params">${t("keys.action_params")}</label>
      <div class="act-input" id="kdValueBox"></div>
    </div>
    <div class="kd-test-row">
      <button type="button" class="btn secondary sm" id="kdTest" data-i18n="ui.test_action">${t("ui.test_action")}</button>
      <span class="muted small" id="kdTestHint"></span>
    </div>`;

  $("#kdHid").onchange = (e) => {
    const code = parseInt(e.target.value, 10);
    item.code = code;
    item.label = (state.keycodes.find((k) => k.code === code) || {}).label || String(code);
    refreshKeyCard(idx);
    markDirty();
  };
  // 4 个复选框合成一个 0–15 的掩码：勾/取消只改这一位，其余位保留。
  const modBoxes = body.querySelectorAll(".kd-mods input[data-bit]");
  modBoxes.forEach((box) => {
    box.onchange = () => {
      let next = 0;
      modBoxes.forEach((b) => {
        if (b.checked) next |= parseInt(b.dataset.bit, 10) || 0;
      });
      item.mods = next;
      refreshKeyCard(idx);
      markDirty();
    };
  });
  // WS-550：结论为「不支持」时给一个「重新检测」入口，清掉缓存结论并重探，
  // 这样偶尔丢包被误判的新固件不必等 VERSION 变化才能恢复。
  const redetect = $("#kdModsRedetect");
  if (redetect) redetect.onclick = () => {
    if (typeof redetectModsCapability === "function") redetectModsCapability();
  };
  $("#kdAct").onchange = (e) => {
    item.action = { type: e.target.value, value: "", macro: [] };
    renderKeyDrawer();
    markDirty();
  };

  const holder = $("#kdValueBox");
  if (action.type === "macro") {
    holder.dataset.macroEditor = "1";
  }
  renderActionInput(holder, item, { dataset: { idx: String(idx) } });

  const label = $("#kdValueLabel");
  if (action.type === "macro") {
    if (label) label.textContent = t("keys.macro_steps");
    // 标签文案随动作类型变，data-i18n 也要跟着换，i18n.apply 才不会写回旧词。
    if (label) label.setAttribute("data-i18n", "keys.macro_steps");
    if (label) label.removeAttribute("for");
    const editor = $(".macro-editor", holder);
    if (editor && label) editor.setAttribute("aria-labelledby", "kdValueLabel");
  } else {
    if (label) label.setAttribute("data-i18n", "keys.action_params");
    const ctl = holder.querySelector("input, select, textarea");
    if (ctl) {
      ctl.id = "kdValue";
      if (label) label.setAttribute("for", "kdValue");
    } else if (label) {
      label.removeAttribute("for");
    }
  }

  const testBtn = $("#kdTest");
  testBtn.onclick = () => testKeyAction(item, idx);
  syncKeyDrawerRunning();
}

/**
 * WS-596：切语言时只重算检查器 / 抽屉里的文案，不重建表单。
 *
 * 以前 retranslateDynamic() 直接调 renderKeyDrawer()，整个表单 innerHTML 重建：
 * 宏纯文本视图里尚未规范化的原文会被按步骤重新序列化（空行、缩进被吃掉），
 * 焦点与 caret 也一起丢，用户看到的就是「我正在写的东西被改了」。这里只改
 * 文字节点：值、选中项、textarea 内容一概不碰。
 *
 * 抽屉里大部分标签已经由 i18n.apply(document) 按 data-i18n 换好；下面补的是
 * JS 动态拼出来的那几处（标题、下拉选项、测试按钮状态、宏步骤行）。
 */
function retranslateKeyDrawer() {
  const drawer = $("#keyDrawer");
  const body = $("#keyDrawerBody");
  const title = $("#keyDrawerTitle");
  if (!drawer || !body) return;
  const idx = state.editingKey;
  const item = idx === null ? null : keyItem(idx);
  if (!item) {
    // 没有在编辑：抽屉里没有草稿，空态标题照旧换词即可，不必重建。
    if (title) title.textContent = t("ui.key_editor");
    return;
  }
  if (title) title.textContent = inputTitle(idx);
  // 动作类型下拉是按 value 选中的，只改 option 文案，不动 select.value。
  ACTION_TYPES.forEach((a) => {
    const opt = $(`#kdAct option[value="${a.id}"]`);
    if (opt) opt.textContent = actionName(a);
  });
  // HID 下拉的「特殊」分组名是 optgroup 的 label 属性 —— data-i18n 只能改
  // textContent，改不到属性，切语言不重写就会停在旧语言上（WS-599）。
  const og = $("#kdHid optgroup");
  if (og) og.label = t("keys.special_group");
  // 「无上位机动作」的说明是 JS 拼的 innerHTML（locale 内含 <p> 包裹），
  // 同样不在 data-i18n 覆盖范围，按当前键码重写一遍。
  const action = item.action || { type: "none" };
  if ((action.type || "none") === "none") {
    const box = $("#kdValueBox");
    if (box) renderNoActionNote(box, item);
  }
  // 测试按钮 / 提示的文案取决于执行状态，data-i18n 只会写成「测试动作」。
  syncKeyDrawerRunning();
  if (typeof macroEditorRetranslate === "function") macroEditorRetranslate();
}

function syncKeyDrawerRunning() {
  if (state.editingKey === null) return;
  const btn = $("#kdTest");
  if (!btn) return;
  const item = keyItem(state.editingKey);
  const hint = $("#kdTestHint");
  const noAction = !item || !item.action || (item.action.type || "none") === "none";
  const running = state.selected
    ? ((state.running[state.selected] || {})[String(state.editingKey)])
    : undefined;
  const stopping = state.selected
    ? ((state.stopping[state.selected] || {})[String(state.editingKey)])
    : undefined;
  btn.classList.remove("danger");
  if (running !== undefined) {
    if (stopping) {
      btn.disabled = true;
      btn.textContent = t("ui.stopping");
      btn.title = stoppingBtnTitle();   // WS-585：旧常量已随 WS-498 改成函数
    } else {
      btn.disabled = false;
      btn.textContent = t("ui.stop");
      btn.classList.add("danger");
      btn.title = stopBtnTitle();
    }
    if (hint) hint.textContent = t("keys.running_hint");
    return;
  }
  btn.disabled = noAction;
  btn.textContent = t("ui.test_action");
  btn.title = noAction
    ? t("ui.test_none")
    : testBtnIdleTitle();
  if (hint) {
    hint.textContent = noAction
      ? t("keys.hid_only_hint")
      : "";
  }
}

/* ---------------- 宏：DSL 解析（与主机端消费的步骤结构一致） ----------------
 * 主机端没有「解析宏文本」的 HTTP 接口：/api/action/test 会真的执行动作，
 * 不能拿来做预览。因此这里沿用原有的 parseMacro / formatMacro（它们产出的
 * step 对象就是后端 run_action 读取的字段），lint 只在这同一套语法上做行级
 * 检查，不另写解析器。 */

function formatMacro(steps) {
  return steps
    .map((s) => formatMacroStep(s))
    .join("\n");
}

function formatMacroStep(s) {
  if (s.type === "delay") return "wait:" + (s.value || 0);
  if (s.type === "text") {
    const d = typeof s.delay === "number" && isFinite(s.delay) ? s.delay : null;
    return (d === null ? "text:" : "text!" + d + ":") + (s.value || "");
  }
  if (s.type === "paste") return "paste:" + (s.value || "");
  if (s.type === "url") return "url:" + (s.value || "");
  if (s.type === "browser") return "browser:" + (s.value || "");
  if (s.type === "app") return "app:" + (s.value || "");
  return "key:" + (s.value || "");
}

function parseMacro(text) {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((line) => {
      const i = line.indexOf(":");
      if (i < 0) return { type: "key", value: line };
      let kind = line.slice(0, i).trim().toLowerCase();
      const value = line.slice(i + 1).trim();
      let textDelay = null;
      const m = /^text!(\d+)$/.exec(kind);
      if (m) {
        textDelay = parseInt(m[1], 10);
        kind = "text";
      }
      if (kind === "wait" || kind === "delay") return { type: "delay", value: parseInt(value, 10) || 0 };
      if (kind === "text") {
        return textDelay === null
          ? { type: "text", value }
          : { type: "text", value, delay: textDelay };
      }
      if (kind === "paste") return { type: "paste", value };
      if (kind === "url") return { type: "url", value };
      if (kind === "browser") return { type: "browser", value: value.toLowerCase() };
      if (kind === "key") return { type: "key", value: value.replace(/^(?:key:)+/i, "") };
      if (kind === "app") return { type: "app", value };
      return { type: "key", value: line };
    });
}

/** 逐行解析并保留原始文本，供纯文本 ↔ 结构化切换时不改写字面内容。 */
function parseMacroDetailed(text) {
  const out = [];
  text.split("\n").forEach((raw) => {
    if (!raw.trim()) return;
    const steps = parseMacro(raw.trim());
    if (!steps.length) return;
    const step = steps[0];
    step._raw = raw;
    out.push(step);
  });
  return out;
}

function normalizeMacroStep(step) {
  const src = step && typeof step === "object" ? step : {};
  const out = { type: src.type || "key", value: src.value === undefined ? "" : src.value };
  if (src.type === "text" && typeof src.delay === "number" && isFinite(src.delay)) {
    out.delay = src.delay;
  }
  if (src._raw !== undefined) out._raw = src._raw;
  return out;
}

/** 写回配置的步骤：去掉只在编辑器里用的 _raw。 */
function cleanMacroSteps(steps) {
  return steps.map((s) => {
    const out = { type: s.type, value: s.value === undefined ? "" : s.value };
    if (s.type === "text" && typeof s.delay === "number" && isFinite(s.delay)) {
      out.delay = s.delay;
    }
    return out;
  });
}

function serializeMacroSteps(steps) {
  return steps.map((s) => (s._raw !== undefined ? s._raw : formatMacroStep(s))).join("\n");
}

/* ---------------- 宏：逐行 lint ---------------- */

const MACRO_KNOWN_KINDS = new Set([
  "key", "text", "paste", "delay", "wait", "url", "browser", "app",
]);

function lintMacroLine(line) {
  const i = line.indexOf(":");
  if (i < 0) return null;                    // 无前缀 = 键名，交给主机端按键映射
  let kind = line.slice(0, i).trim().toLowerCase();
  const value = line.slice(i + 1).trim();
  if (kind.startsWith("text!")) {
    if (!/^text!\d+$/.test(kind)) {
      return { level: "error", message: t("keys.err_text_delay") };
    }
    kind = "text";
  }
  if (!MACRO_KNOWN_KINDS.has(kind)) {
    return { level: "error", message: t("keys.unknown_step_type", { kind }) };
  }
  if (kind === "wait" || kind === "delay") {
    if (!/^\d+$/.test(value)) return { level: "error", message: t("keys.err_wait_ms") };
    return null;
  }
  if (kind === "key") {
    return value ? null : { level: "error", message: t("keys.err_key_missing") };
  }
  if (kind === "app") {
    return value ? null : { level: "error", message: t("keys.err_app_missing") };
  }
  if (kind === "url") {
    if (!value) return { level: "error", message: t("keys.err_url_missing") };
    return /^[a-z][a-z0-9+.-]*:\/\//i.test(value)
      ? null : { level: "warn", message: t("keys.hint_http") };
  }
  if (kind === "browser" && !value) {
    return { level: "warn", message: t("keys.hint_default_browser") };
  }
  if ((kind === "text" || kind === "paste") && !value) {
    return { level: "warn", message: t("keys.step_empty", { kind }) };
  }
  return null;
}

function lintMacroText(text) {
  const out = [];
  text.split("\n").forEach((raw, i) => {
    const line = raw.trim();
    if (!line) return;
    const issue = lintMacroLine(line);
    if (issue) out.push({ line: i + 1, ...issue });
  });
  return out;
}

function lintMacroStep(step) {
  const type = step.type;
  if (!MACRO_KNOWN_KINDS.has(type)) return { level: "error", message: t("keys.unknown_step_type", { kind: type }) };
  const value = String(step.value === undefined ? "" : step.value);
  if (type === "delay") {
    return /^\d+$/.test(value) ? null : { level: "error", message: t("keys.err_wait_int") };
  }
  if (type === "key") return value ? null : { level: "error", message: t("keys.err_key_absent") };
  if (type === "app") return value ? null : { level: "error", message: t("keys.err_app_absent") };
  if (type === "url") {
    if (!value) return { level: "error", message: t("keys.err_url_absent") };
    return /^[a-z][a-z0-9+.-]*:\/\//i.test(value)
      ? null : { level: "warn", message: t("keys.hint_http") };
  }
  if (type === "browser" && !value) return { level: "warn", message: t("keys.hint_default_browser") };
  if (type === "text" && step.delay !== undefined && !/^\d+$/.test(String(step.delay))) {
    return { level: "error", message: t("keys.err_delay_int") };
  }
  if ((type === "text" || type === "paste") && !value) {
    return { level: "warn", message: t("keys.err_empty_body") };
  }
  return null;
}

function macroLintHtml(issues) {
  if (!issues.length) return "";
  return issues.map((it) => {
    const where = it.line ? t("keys.line_prefix", { line: it.line }) : "";
    const cls = it.level === "error" ? "" : "class=\"warn\"";
    return `<span ${cls}>${where}${escapeHtml(it.message)}</span>`;
  }).join("<br>");
}

/* ---------------- 宏：结构化 + 纯文本双视图 ---------------- */

// 函数而不是常量：模块加载时求值会把语言冻结在启动那一刻。
const macroStepOptions = () => [
  ["key", t("keys.step_key")],
  ["text", t("keys.step_text")],
  ["paste", t("keys.step_paste")],
  ["delay", t("keys.step_wait")],
  ["url", t("keys.step_url")],
  ["browser", t("keys.step_browser")],
  ["app", t("keys.step_app")],
];

const MACRO_STEP_DEFAULTS = {
  key: () => ({ type: "key", value: "" }),
  text: () => ({ type: "text", value: "" }),
  paste: () => ({ type: "paste", value: "" }),
  delay: () => ({ type: "delay", value: 200 }),
  url: () => ({ type: "url", value: "https://" }),
  browser: () => ({ type: "browser", value: "default" }),
  app: () => ({ type: "app", value: "" }),
};

// 函数而不是常量：模块加载时文案表还是空的，const 会把按钮文字冻成
// 「+ ui.tab_keys」这样的 key（与 macroStepOptions 同理）。
const macroQuickInserts = () => [
  ["key", t("ui.tab_keys")],
  ["delay", t("keys.kind_wait")],
  ["text", t("keys.kind_text")],
  ["paste", t("keys.kind_paste")],
  ["url", t("keys.kind_url")],
  ["browser", t("keys.kind_browser")],
  ["app", t("keys.kind_app")],
];

function mountMacroEditor(holder, action) {
  if (!Array.isArray(action.macro)) action.macro = [];
  let steps = action.macro.map(normalizeMacroStep);
  let view = state.macroView === "text" ? "text" : "steps";
  let textValue = view === "text" ? serializeMacroSteps(steps) : null;

  const wrap = document.createElement("div");
  wrap.className = "macro-editor";
  wrap.innerHTML = `
    <div class="macro-tabs" role="tablist" aria-label="${t("keys.macro_view_aria")}"
         data-i18n-aria-label="keys.macro_view_aria">
      <button type="button" class="macro-tab" data-view="steps" role="tab"
              data-i18n="keys.macro_view_steps">${t("keys.macro_view_steps")}</button>
      <button type="button" class="macro-tab" data-view="text" role="tab"
              data-i18n="keys.macro_view_text">${t("keys.macro_view_text")}</button>
      <span class="macro-count"></span>
    </div>
    <div class="macro-pane" data-pane="steps" role="tabpanel">
      <div class="macro-steps"></div>
      <div class="macro-quick"></div>
      <p class="macro-lint" data-lint="steps"></p>
    </div>
    <div class="macro-pane" data-pane="text" role="tabpanel" hidden>
      <textarea class="macro-text" spellcheck="false"
        placeholder="${t("keys.macro_text_placeholder")}"
        data-i18n-placeholder="keys.macro_text_placeholder"></textarea>
      <p class="muted small" data-i18n="keys.macro_text_hint">${t("keys.macro_text_hint")}</p>
      <p class="macro-lint" data-lint="text"></p>
    </div>`;

  const count = $(".macro-count", wrap);
  const list = $(".macro-steps", wrap);
  const quick = $(".macro-quick", wrap);
  const textArea = $(".macro-text", wrap);
  const lintSteps = $('[data-lint="steps"]', wrap);
  const lintText = $('[data-lint="text"]', wrap);

  // silent = 只初始化界面，不回写配置、不触发自动保存（打开抽屉不该算改动）。
  function commit({ fromText = false, silent = false } = {}) {
    if (!silent) {
      action.macro = cleanMacroSteps(steps);
      action.value = serializeMacroSteps(steps);
    }
    if (!fromText) textValue = null;
    if (textArea && (textValue !== null || fromText)) {
      if (fromText) textValue = textArea.value;
      else textArea.value = serializeMacroSteps(steps);
    }
    count.textContent = t("keys.macro_count", { count: steps.length });
    lintSteps.innerHTML = macroLintHtml(
      steps.map((s, i) => {
        const issue = lintMacroStep(s);
        return issue ? { line: i + 1, ...issue } : null;
      }).filter(Boolean));
    if (textArea) lintText.innerHTML = macroLintHtml(lintMacroText(textArea.value));
    if (silent) return;
    markDirty();
    refreshKeyCard(state.editingKey);
  }

  function issueFor(step) {
    const issue = lintMacroStep(step);
    return issue ? macroLintHtml([issue]) : "";
  }

  function renderRows() {
    list.innerHTML = "";
    if (!steps.length) {
      list.innerHTML = `<p class="muted small" data-i18n="keys.macro_empty">${t("keys.macro_empty")}</p>`;
    }
    steps.forEach((step, i) => {
      const row = document.createElement("div");
      row.className = "macro-step";
      row.dataset.i = String(i);
      const opts = macroStepOptions().map(([id, label]) => {
        const unknown = id === step.type ? "" : "";
        return `<option value="${id}" ${id === step.type ? "selected" : ""}>${label}${unknown}</option>`;
      }).join("");
      const extra = MACRO_KNOWN_KINDS.has(step.type) ? "" :
        `<option value="${step.type}" selected>${t("keys.unknown_type_option", { type: escapeHtml(step.type) })}</option>`;
      row.innerHTML = `
        <select class="ms-type">${opts}${extra}</select>
        <input class="ms-value" type="${step.type === "delay" ? "number" : "text"}"
          ${step.type === "delay" ? 'min="0" step="1"' : ""}
          placeholder="${macroValuePlaceholder(step.type)}"
          value="${escapeHtml(String(step.value === undefined ? "" : step.value))}">
        ${step.type === "text"
          ? `<input class="ms-delay" type="number" min="0" step="1" title="${t("keys.text_delay_placeholder")}"
               value="${step.delay === undefined ? "" : step.delay}">`
          : ""}
        ${step.type === "text" || step.type === "paste"
          ? `<span class="macro-sensitive" title="${t("keys.text_body_hint")}">${t("keys.log_not_echoed")}</span>`
          : ""}
        <span class="ms-move">
          <button type="button" class="btn sm ms-up" title="${t("keys.move_up")}" ${i === 0 ? "disabled" : ""}>↑</button>
          <button type="button" class="btn sm ms-down" title="${t("keys.move_down")}" ${i === steps.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" class="btn sm ms-del" title="${t("keys.delete_step")}">✕</button>
        </span>
        <p class="ms-error"></p>`;
      list.appendChild(row);

      const err = $(".ms-error", row);
      err.innerHTML = issueFor(step);

      $(".ms-type", row).onchange = (e) => {
        const type = e.target.value;
        if (!MACRO_KNOWN_KINDS.has(type)) return;
        steps[i] = MACRO_STEP_DEFAULTS[type]();
        renderRows();
        commit();
      };
      const valueEl = $(".ms-value", row);
      valueEl.oninput = () => {
        steps[i].value = valueEl.value;
        err.innerHTML = issueFor(steps[i]);
        commit();
      };
      const delayEl = $(".ms-delay", row);
      if (delayEl) {
        delayEl.oninput = () => {
          if (delayEl.value === "") delete steps[i].delay;
          else steps[i].delay = Math.max(0, parseInt(delayEl.value, 10) || 0);
          err.innerHTML = issueFor(steps[i]);
          commit();
        };
      }
      const up = $(".ms-up", row);
      const down = $(".ms-down", row);
      up.onclick = () => {
        if (i === 0) return;
        [steps[i - 1], steps[i]] = [steps[i], steps[i - 1]];
        renderRows();
        commit();
      };
      down.onclick = () => {
        if (i === steps.length - 1) return;
        [steps[i], steps[i + 1]] = [steps[i + 1], steps[i]];
        renderRows();
        commit();
      };
      $(".ms-del", row).onclick = () => {
        steps.splice(i, 1);
        renderRows();
        commit();
      };
    });
  }

  // 快捷插入按钮没有用户状态，文案变了整组重建即可（不碰 steps / textarea）。
  function buildQuick() {
    quick.innerHTML = "";
    macroQuickInserts().forEach(([type, label]) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn sm";
      btn.textContent = "+ " + label;
      btn.onclick = () => {
        steps.push(MACRO_STEP_DEFAULTS[type]());
        renderRows();
        commit();
        const last = list.querySelector(`.macro-step[data-i="${steps.length - 1}"] .ms-value`);
        if (last) last.focus();
      };
      quick.appendChild(btn);
    });
  }
  buildQuick();

  /**
   * WS-596：切语言时只换文案。steps / textValue / textarea.value 一律不动，
   * 所以宏纯文本视图里正在编辑的原文（含空行、缩进）与 caret 都保留。
   */
  function retranslate() {
    $$(".macro-tab", wrap).forEach((tab) => {
      tab.textContent = t(tab.dataset.view === "text"
        ? "keys.macro_view_text" : "keys.macro_view_steps");
    });
    count.textContent = t("keys.macro_count", { count: steps.length });
    buildQuick();
    if (!steps.length) {
      list.innerHTML = `<p class="muted small" data-i18n="keys.macro_empty">${t("keys.macro_empty")}</p>`;
    }
    $$(".macro-step", list).forEach((row, i) => {
      const step = steps[i];
      if (!step) return;
      const sel = $(".ms-type", row);
      if (sel) {
        const options = Array.from(sel.querySelectorAll("option"));
        macroStepOptions().forEach(([id, label]) => {
          const opt = options.find((o) => o.value === id);
          if (opt) opt.textContent = label;
        });
        const unknown = options.find((o) => !MACRO_KNOWN_KINDS.has(o.value));
        if (unknown) unknown.textContent = t("keys.unknown_type_option", { type: step.type });
      }
      const valueEl = $(".ms-value", row);
      if (valueEl) valueEl.placeholder = macroValuePlaceholder(step.type);
      const delayEl = $(".ms-delay", row);
      if (delayEl) delayEl.title = t("keys.text_delay_placeholder");
      const sensitive = $(".macro-sensitive", row);
      if (sensitive) sensitive.textContent = t("keys.log_not_echoed");
      const up = $(".ms-up", row);
      if (up) up.title = t("keys.move_up");
      const down = $(".ms-down", row);
      if (down) down.title = t("keys.move_down");
      const del = $(".ms-del", row);
      if (del) del.title = t("keys.delete_step");
      const err = $(".ms-error", row);
      if (err) err.innerHTML = issueFor(step);
    });
    lintSteps.innerHTML = macroLintHtml(
      steps.map((s, i) => {
        const issue = lintMacroStep(s);
        return issue ? { line: i + 1, ...issue } : null;
      }).filter(Boolean));
    if (textArea) lintText.innerHTML = macroLintHtml(lintMacroText(textArea.value));
  }
  macroEditorRetranslate = retranslate;

  function showView(next) {
    view = next;
    state.macroView = next;
    $$(".macro-tab", wrap).forEach((t) => {
      const on = t.dataset.view === next;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    $$(".macro-pane", wrap).forEach((p) => {
      p.hidden = p.dataset.pane !== next;
    });
    if (next === "text" && textArea) {
      textArea.value = textValue !== null ? textValue : serializeMacroSteps(steps);
    }
    if (next === "steps") renderRows();
    if (textArea) {
      lintText.innerHTML = macroLintHtml(lintMacroText(textArea.value));
    }
  }

  $$(".macro-tab", wrap).forEach((tab) => {
    tab.onclick = () => showView(tab.dataset.view);
  });

  if (textArea) {
    textArea.oninput = () => {
      textValue = textArea.value;
      steps = parseMacroDetailed(textValue);
      commit({ fromText: true });
    };
  }

  holder.appendChild(wrap);
  showView(view);
  commit({ silent: true });
}

function macroValuePlaceholder(type) {
  if (type === "delay") return t("keys.wait_placeholder");
  if (type === "key") return "Tab / F5 / ctrl+alt+t";
  if (type === "text") return t("keys.text_placeholder");
  if (type === "paste") return t("keys.paste_placeholder");
  if (type === "url") return "https://example.com";
  if (type === "browser") return "chrome / firefox / edge / default";
  if (type === "app") return "C:\\path\\app.exe --flag";
  return "";
}
