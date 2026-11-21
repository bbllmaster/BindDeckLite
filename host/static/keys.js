/* BindDeckLite 按键编辑：摘要卡辅助 + 抽屉 + 宏双视图。
 * 普通 <script> 在 app.js 之前加载（无构建步骤）；只声明函数，
 * 运行期才读取 app.js 里的 state / marker 常量。
 */

/* ---------------- 摘要卡 ---------------- */

/** WS-661：媒体键段的起点（服务器 /api/keycodes 的 media_base，默认 256）。
 *  键码 = 256 + HID consumer usage，与键盘键（0–255）不冲突。 */
function mediaBase() {
  const base = Number(state.mediaBase);
  return Number.isFinite(base) && base > 0 ? base : 256;
}

/** 一个键码是否属于媒体键段（>= 256）。 */
function isMediaCode(code) {
  return parseInt(code, 10) >= mediaBase();
}

/** 设备的 CFG:MEDIA 结论：只有明确 True 才显示媒体分组（先探测、再显示）。
 *  False = 旧固件，整组隐藏并提示升级；None = 未定论，等探测结果再显示。 */
function mediaSupported() {
  return (state.cfg || {}).media_supported === true;
}

function keyOptions(selected) {
  // 键码 0 = 「不发键」，单独收进「特殊」分组，普通键码的顺序不变。
  // WS-661：媒体键（>= 256）收进「媒体键」分组，排在普通键之后、「特殊」
  // 之前；设备未确认支持时不列出来（旧固件下发会被截成键盘键）。
  // 已经选中媒体键时至少保留那一项，否则下拉会退回第一项、显示成 F13。
  const opt = (k) =>
    `<option value="${k.code}" ${String(k.code) === String(selected) ? "selected" : ""}>${k.label}</option>`;
  const base = mediaBase();
  const normal = state.keycodes.filter((k) => k.code !== 0 && k.code < base);
  const media = state.keycodes.filter((k) => k.code >= base);
  const special = state.keycodes.filter((k) => k.code === 0);
  let html = normal.map(opt).join("");
  const shownMedia = mediaSupported()
    ? media
    : media.filter((k) => String(k.code) === String(selected));
  if (shownMedia.length) {
    html += `<optgroup data-group="media" label="${t("keys.media_group")}">${shownMedia.map(opt).join("")}</optgroup>`;
  }
  if (special.length) {
    html += `<optgroup data-group="special" label="${t("keys.special_group")}">${special.map(opt).join("")}</optgroup>`;
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
 *  设备），此时以及 unknown / supported 时都保持原有修饰键显示。
 *  WS-661：媒体键没有修饰键（consumer 报告没有修饰位），摘要只显示标签，
 *  标签与下拉、检查器同源（/api/keycodes 的键码表）。 */
function keyHidText(item) {
  const code = parseInt(item && item.code, 10);
  if (code === 0) return t("keys.key_none");
  const known = state.keycodes.find((k) => k.code === code);
  const base = (known && known.label) || (item && item.label) || String(code);
  if (isMediaCode(code)) return base;
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
  // WS-661: 媒体键能力同理 —— 探测到 MEDIA:1 才把「媒体键」分组放出来，
  // 旧固件（两次无应答）整组隐藏并提示升级，绝不把 >=256 的键码写下去。
  if (typeof ensureMediaCapability === "function") ensureMediaCapability();
  const first = $("#kdHid");
  if (first) first.focus({ preventScroll: true });
}

function closeKeyDrawer(restoreFocus = true) {
  if (state.editingKey === null) return;
  // 关抽屉先退出录入态：值不写，Esc 只关抽屉（录入态自己已经吃掉 Esc）。
  stopMacroRecord();
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
  // 录入态也必须一起退出：它的 row / 按钮引用在重建后已经是孤儿。
  stopMacroRecord();
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
  // WS-661: a media key has no modifier byte at all, so the whole row is
  // disabled while one is selected — Ctrl+播放/暂停 can never be built in the UI.
  // Old firmware keeps its own "modifiers unsupported" reason (WS-546).
  const itemIsMedia = isMediaCode(item.code);
  const modsBlocked = itemIsMedia || (state.cfg || {}).mods_supported === false;
  const modsHint = itemIsMedia
    ? `<p class="muted small" id="kdModsHint" data-i18n="keys.mods_media_ignored">${t("keys.mods_media_ignored")}</p>`
    : modsBlocked
      ? `<p class="muted small" id="kdModsHint" data-i18n="keys.mods_unsupported">${t("keys.mods_unsupported")}</p>
        <button type="button" class="btn secondary sm" id="kdModsRedetect" data-i18n="keys.mods_redetect">${t("keys.mods_redetect")}</button>`
      : "";
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
      ${modsHint}
    </div>` : "";
  // WS-661: the probe ruled the media segment out — say why the group is gone
  // instead of leaving the user wondering where 「播放/暂停」 went.
  // WS-665: a cached False must be recoverable. Two dropped probes can mark a
  // current firmware as unsupported; without this button the group would stay
  // hidden forever. Same ?reset=1 probe the modifier row uses (WS-550/618).
  const mediaBlocked = (state.cfg || {}).media_supported === false;
  const mediaHint = mediaBlocked
    ? `<p class="muted small" id="kdMediaHint" data-i18n="keys.media_unsupported">${t("keys.media_unsupported")}</p>
        <button type="button" class="btn secondary sm" id="kdMediaRedetect" data-i18n="keys.mods_redetect">${t("keys.mods_redetect")}</button>`
    : "";
  body.innerHTML = `
    <div class="kd-field">
      <label class="field-label" for="kdHid" data-i18n="keys.hid_label">${t("keys.hid_label")}</label>
      <select id="kdHid" class="sel-key">${keyOptions(item.code)}</select>
      ${mediaHint}
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
    const wasMedia = isMediaCode(item.code);
    item.code = code;
    item.label = (state.keycodes.find((k) => k.code === code) || {}).label || String(code);
    // WS-661: a media key drops any modifier mask — the consumer report has
    // no modifier byte, so keeping Ctrl would show a combination the device
    // cannot send. Switching back to a keyboard key keeps the (now 0) mask.
    if (isMediaCode(code)) item.mods = 0;
    refreshKeyCard(idx);
    // Only a keyboard <-> media switch changes the drawer's own state (the
    // modifier row is disabled for media keys), so avoid a pointless rebuild.
    if (wasMedia !== isMediaCode(code)) renderKeyDrawer();
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
  // WS-665：媒体组被缓存成「不支持」时的恢复入口（与修饰键同一套 reset 语义）。
  const mediaRedetect = $("#kdMediaRedetect");
  if (mediaRedetect) mediaRedetect.onclick = () => {
    if (typeof redetectMediaCapability === "function") redetectMediaCapability();
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
  // HID 下拉的分组名是 optgroup 的 label 属性 —— data-i18n 只能改 textContent，
  // 改不到属性，切语言不重写就会停在旧语言上（WS-599）。
  // WS-665：媒体组与特殊组按 data-group 各取各的。以前只取第一个 optgroup，
  // 媒体组排在前面时会把「特殊」写到媒体组上，两组一起错位。
  const mo = $('#kdHid optgroup[data-group="media"]');
  if (mo) mo.label = t("keys.media_group");
  const so = $('#kdHid optgroup[data-group="special"]');
  if (so) so.label = t("keys.special_group");
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

/* ---------------- 宏 Key 步骤：按一次组合键就录入（WS-651） ----------------
 * 值输入框右侧的「录入」按钮把真实按键翻译成 `key:` 能用的规范名，省掉
 * 「记拼法 + 查帮助表 + 手打」这一圈。整段逻辑只碰前端草稿：不写配置、
 * 不发 CFG: / CMD:。
 */

// 与 host/app.py 的 MACRO_KEY_NAMES 键集合**同源**。两边的一致性由
// test_macro_record.mjs 直接解析 app.py 源码断言 —— 后端加名字而这里没跟
// 上、或者这里多写一个后端不认识的名字，测试都会变红。
// 集合包含后端接受的全部别名（return / del / pgup …）：录入只写规范名，
// 别名留在集合里只为让「同源」断言成立。
const MACRO_REC_KEY_NAMES = [
  "tab", "enter", "return", "esc", "escape", "space", "backspace",
  "insert", "ins", "delete", "del", "home", "end",
  "pageup", "pgup", "pagedown", "pgdn", "left", "right", "up", "down",
  "lctrl", "ctrl", "control", "rctrl", "lshift", "shift", "rshift",
  "lalt", "alt", "ralt", "altgr", "lwin", "win", "rwin",
];
for (let i = 1; i <= 12; i++) MACRO_REC_KEY_NAMES.push("f" + i);
const MACRO_REC_KEY_SET = new Set(MACRO_REC_KEY_NAMES);

// KeyboardEvent.key → 写入 key: 的规范名。字母 / 数字走下面的单字符正则。
const MACRO_REC_EVENT_NAMES = {
  Tab: "tab", Enter: "enter", Escape: "esc", " ": "space",
  Backspace: "backspace", Insert: "insert", Delete: "delete",
  Home: "home", End: "end", PageUp: "pageup", PageDown: "pagedown",
  ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down",
  "+": "+",
};
for (let i = 1; i <= 12; i++) MACRO_REC_EVENT_NAMES["F" + i] = "f" + i;

// 修饰键固定顺序：ctrl → shift → alt → win（与后端 `+` 连接顺序一致）。
const MACRO_REC_MODS = [
  ["ctrl", "ctrlKey"],
  ["shift", "shiftKey"],
  ["alt", "altKey"],
  ["win", "metaKey"],
];

// 浏览器 / 宿主占用的组合：拦不到就白按，直接拒绝并让人手输。
// 只写「必需的修饰键 + 主键」，判定见 macroRecReserved：同族组合（如
// Ctrl+Shift+W）浏览器照样吞掉、preventDefault 无效，所以一律拒绝。
const MACRO_REC_RESERVED = new Set(["ctrl+w", "ctrl+t", "ctrl+n", "alt+f4"]);

// 静止态的可访问名比可见文字更具体（「录入按键」），可见文字仍是「录入」：
// 读屏能读出按钮用途，视觉宽度不变（行内空间很紧）。录入态两处一起换成
// 「按组合键…」，退出时再换回来。
const MACRO_REC_ARIA_KEY = "keys.macro_rec_aria";
const MACRO_REC_ACTIVE_KEY = "keys.macro_rec_active";

/**
 * 一组按住的修饰键 + 主键是否命中保留集。
 * 命中条件：保留集里的必需修饰键全部按下，且**多出来的只有 shift**。
 *   - 带 shift 的同族组合照样拒绝：浏览器对 Ctrl+Shift+W / Ctrl+Shift+T
 *     的处理不变，preventDefault 仍然无效（Ctrl+Shift+W 直接关窗）。
 *   - 多出 ctrl / alt 等其他修饰键不算命中：Ctrl+Alt+T 是普通可录入组合，
 *     不能因为主键是 t 就误伤。
 */
function macroRecReserved(mods, main) {
  for (const combo of MACRO_REC_RESERVED) {
    const parts = combo.split("+");
    if (parts[parts.length - 1] !== main) continue;
    const need = parts.slice(0, -1);
    const extra = mods.filter((m) => !need.includes(m));
    if (need.every((m) => mods.includes(m)) && extra.every((m) => m === "shift")) {
      return true;
    }
  }
  return false;
}

/**
 * 录入按钮的文字 / tooltip / 可访问名一起换，data-i18n-* 跟着走 —— 否则
 * 切语言时 i18n.apply() 会按旧 key 把标题写回另一个状态的词。
 * textKey 管可见文字，ariaKey 管 title 与 aria-label（缺省同 textKey）。
 */
function setMacroRecordBtnLabel(btn, textKey, ariaKey) {
  if (!btn) return;
  const aria = ariaKey || textKey;
  btn.textContent = t(textKey);
  btn.title = t(aria);
  btn.setAttribute("aria-label", t(aria));
  btn.setAttribute("data-i18n-title", aria);
  btn.setAttribute("data-i18n-aria-label", aria);
}

const MACRO_REC_TIMEOUT_MS = 3000;

/** macOS 上 Meta 是 Cmd、Alt 是 Option；写入值仍是 ctrl/shift/alt/win。 */
function macroRecModLabel(id) {
  const ua = (typeof navigator !== "undefined" && navigator && navigator.userAgent) || "";
  const mac = /Mac|iPhone|iPad/i.test(ua);
  if (id === "win") return mac ? "Cmd" : "Win";
  if (id === "alt") return mac ? "Option" : "Alt";
  return id.charAt(0).toUpperCase() + id.slice(1);
}

/** 事件里按固定顺序取出按住的修饰键 id。 */
function macroRecModsFromEvent(e) {
  return MACRO_REC_MODS.filter(([, flag]) => e[flag]).map(([id]) => id);
}

/**
 * 一个 keydown 对应的录入结果：
 *   { ok: true, name }                     → 写回值并退出录入
 *   { ok: false, reason: "modifier" }      → 只按了修饰键，回显不落值
 *   { ok: false, reason: "plus" }          → + 是分隔符
 *   { ok: false, reason: "reserved" }      → 浏览器 / 系统占用
 *   { ok: false, reason: "unsupported" }   → 表里没有这个主键
 */
function macroRecordKeyName(e) {
  const key = e && typeof e.key === "string" ? e.key : "";
  if (!key) return { ok: false, reason: "unsupported" };
  const mods = macroRecModsFromEvent(e);
  // 纯修饰键：只回显，不落值（单独按 Ctrl 不是合法的 key: 步骤）。
  if (key === "Control" || key === "Shift" || key === "Alt" || key === "Meta") {
    return { ok: false, reason: "modifier" };
  }
  let main = MACRO_REC_EVENT_NAMES[key];
  if (main === undefined && /^[a-z0-9]$/i.test(key)) main = key.toLowerCase();
  if (main === undefined) return { ok: false, reason: "unsupported" };
  if (main === "+") return { ok: false, reason: "plus" };
  // 单字母 / 单数字后端原样透传（不在 MACRO_KEY_NAMES 里），其余主键必须
  // 是后端认识的规范名，否则写进去的 key: 步骤要到执行时才报错。
  if (main.length > 1 && !MACRO_REC_KEY_SET.has(main)) {
    return { ok: false, reason: "unsupported" };
  }
  if (macroRecReserved(mods, main)) return { ok: false, reason: "reserved" };
  const name = mods.concat([main]).join("+");
  return { ok: true, name };
}

function macroRecordActive() {
  return !!(state.macroRec && state.macroRec.row);
}

/** 提示行内容是「文案 key + 参数」，切语言时按当前语言重算而不是写死。 */
function setMacroRecordHint(key, params) {
  const rec = state.macroRec;
  if (!rec) return;
  rec.hintKey = key;
  rec.hintParams = params;
  if (rec.hint) {
    rec.hint.hidden = false;
    rec.hint.textContent = t(key, params);
  }
}

function armMacroRecordTimer() {
  const rec = state.macroRec;
  if (!rec) return;
  if (rec.timer !== null) clearTimeout(rec.timer);
  rec.timer = setTimeout(() => {
    // 宿主没把按键转给页面（焦点在别的窗口 / 系统吞了）：只提示，不退出。
    if (state.macroRec !== rec) return;
    setMacroRecordHint("keys.macro_rec_silent");
  }, MACRO_REC_TIMEOUT_MS);
}

function showMacroRecordHeld(e) {
  const mods = macroRecModsFromEvent(e).map(macroRecModLabel);
  setMacroRecordHint("keys.macro_rec_held", { mods: mods.join(" + ") });
}

function macroRecordKeydown(e) {
  const rec = state.macroRec;
  if (!rec) return;
  // 输入法组合过程中的 keydown（key 为 "Process" / keyCode 229）一律忽略：
  // 既不拦截也不落值，否则会在选词时把半个组合写进配置。
  if (e.isComposing || e.keyCode === 229 || (e.keyCode === 0 && e.key === "Process")) return;
  // 录入期间这个按键归我们：不触发浏览器快捷键，也不能冒泡到
  // keys.js bindKeyDrawer / app.js 的 Esc 关抽屉监听。
  if (e.preventDefault) e.preventDefault();
  if (e.stopPropagation) e.stopPropagation();
  if (e.stopImmediatePropagation) e.stopImmediatePropagation();
  armMacroRecordTimer();
  if (e.key === "Escape") {
    cancelMacroRecord({ focus: true });
    return;
  }
  const res = macroRecordKeyName(e);
  if (res.ok) {
    rec.apply(res.name);
    stopMacroRecord({ focus: true, keepValue: true });
    return;
  }
  if (res.reason === "modifier") {
    showMacroRecordHeld(e);
    return;
  }
  // 拒绝集：值零变化，留在录入态让用户重按或 Esc 退出。
  if (res.reason === "plus") setMacroRecordHint("keys.macro_rec_plus");
  else if (res.reason === "reserved") setMacroRecordHint("keys.macro_rec_reserved");
  else setMacroRecordHint("keys.macro_rec_unsupported");
}

/** 点到 / 聚焦到录入行以外的东西：先退出，值不写。 */
function macroRecordOutside(e) {
  const rec = state.macroRec;
  if (!rec || !rec.row) return;
  const target = e && e.target;
  if (target && typeof rec.row.contains === "function" && rec.row.contains(target)) return;
  cancelMacroRecord();
}

/**
 * 进入录入态。opts：{index, row, btn, input, hint, value, apply(name)}。
 * apply 由宏编辑器提供：写 steps[i].value + 走既有 commit()。
 */
function startMacroRecord(opts) {
  if (!opts || !opts.row || typeof opts.apply !== "function") return;
  stopMacroRecord();
  const rec = {
    stepIndex: opts.index,
    originalValue: String(opts.value === undefined ? "" : opts.value),
    row: opts.row,
    btn: opts.btn,
    input: opts.input,
    hint: opts.hint,
    apply: opts.apply,
    hintKey: "keys.macro_rec_hint",
    hintParams: undefined,
    timer: null,
  };
  state.macroRec = rec;
  rec.row.classList.add("recording");
  if (rec.btn) {
    rec.btn.classList.add("recording");
    // 文案、tooltip、可访问名一起切：只换 textContent 的话读屏仍读「录入」。
    setMacroRecordBtnLabel(rec.btn, MACRO_REC_ACTIVE_KEY);
  }
  // readonly 保留原值：录入期间不能手打，也不会因为 readonly 被清空。
  if (rec.input) rec.input.readOnly = true;
  setMacroRecordHint("keys.macro_rec_hint");
  document.addEventListener("keydown", macroRecordKeydown, true);
  document.addEventListener("pointerdown", macroRecordOutside, true);
  document.addEventListener("focusin", macroRecordOutside, true);
  armMacroRecordTimer();
}

/** 退出录入态并把按钮 / 行 / 输入框恢复原样；不碰 steps 与值。
 *  keepValue=false 时把输入框写回进入录入前的原文 —— 取消路径的值与
 *  进入前逐字节一致，不依赖「readonly 期间没人改过」这个假设。 */
function stopMacroRecord({ focus = false, keepValue = false } = {}) {
  const rec = state.macroRec;
  if (!rec) return;
  state.macroRec = null;
  if (rec.timer !== null) clearTimeout(rec.timer);
  document.removeEventListener("keydown", macroRecordKeydown, true);
  document.removeEventListener("pointerdown", macroRecordOutside, true);
  document.removeEventListener("focusin", macroRecordOutside, true);
  if (rec.row) rec.row.classList.remove("recording");
  if (rec.btn) {
    rec.btn.classList.remove("recording");
    // Esc / 提交 / 点外面 / 重建表单都走这里：三处文案与 i18n 标记一并复原。
    setMacroRecordBtnLabel(rec.btn, "keys.macro_rec", MACRO_REC_ARIA_KEY);
  }
  if (rec.input) {
    if (!keepValue) rec.input.value = rec.originalValue;
    rec.input.readOnly = false;
  }
  if (rec.hint) {
    rec.hint.hidden = true;
    rec.hint.textContent = "";
  }
  if (focus && rec.btn && typeof rec.btn.focus === "function") rec.btn.focus();
}

/** Esc / 点别处 / 重建表单时的取消：值、action.macro、dirty 逐字节不变。 */
function cancelMacroRecord({ focus = false } = {}) {
  stopMacroRecord({ focus });
}

/** 切语言：只重算录入按钮与提示行的文案，不动值、不退出。 */
function retranslateMacroRecord() {
  const rec = state.macroRec;
  if (!rec) return;
  if (rec.btn) setMacroRecordBtnLabel(rec.btn, MACRO_REC_ACTIVE_KEY);
  if (rec.hint) rec.hint.textContent = t(rec.hintKey, rec.hintParams);
}

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
    // 整行重建（切类型 / 上移下移 / 删除 / 切视图）会让录入行的引用失效：
    // 先退出，值不写。
    stopMacroRecord();
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
        ${step.type === "key"
          ? `<button type="button" class="btn sm ms-rec"
               title="${t(MACRO_REC_ARIA_KEY)}" aria-label="${t(MACRO_REC_ARIA_KEY)}"
               data-i18n-title="${MACRO_REC_ARIA_KEY}"
               data-i18n-aria-label="${MACRO_REC_ARIA_KEY}">${t("keys.macro_rec")}</button>`
          : ""}
        <span class="ms-move">
          <button type="button" class="btn sm ms-up" title="${t("keys.move_up")}" ${i === 0 ? "disabled" : ""}>↑</button>
          <button type="button" class="btn sm ms-down" title="${t("keys.move_down")}" ${i === steps.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" class="btn sm ms-del" title="${t("keys.delete_step")}">✕</button>
        </span>
        <p class="ms-error"></p>
        <p class="ms-rec-hint" hidden></p>`;
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
      if (step.type === "key") {
        // 录入按钮：写回值仍走 commit()，摘要卡与「有未下发修改」在同一次
        // 渲染里更新，宏编辑器以外的状态（textValue / lint）也一起刷新。
        const recBtn = $(".ms-rec", row);
        const recHint = $(".ms-rec-hint", row);
        recBtn.onclick = () => startMacroRecord({
          index: i,
          row,
          btn: recBtn,
          input: valueEl,
          hint: recHint,
          value: steps[i].value,
          apply: (name) => {
            steps[i].value = name;
            valueEl.value = name;
            err.innerHTML = issueFor(steps[i]);
            commit();
          },
        });
      }
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
      const recBtn = $(".ms-rec", row);
      if (recBtn) setMacroRecordBtnLabel(recBtn, "keys.macro_rec", MACRO_REC_ARIA_KEY);
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
    // 录入态是「暂时」的：切语言只换文案，不退出、不动值。
    retranslateMacroRecord();
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
