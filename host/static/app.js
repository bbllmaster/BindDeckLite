/* BindDeckLite host UI — vanilla JS, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// 文案存 key 而不是译文：模块加载时 t() 一次会把语言冻结在那一刻，
// 之后切换到英文，这些卡片仍是中文。
const ENCODER_MODES = [
  { id: 0, nameKey: "ui.enc_mode_volume", descKey: "ui.enc_mode_volume_desc" },
  { id: 1, nameKey: "ui.enc_mode_zoom", desc: "Ctrl + / Ctrl -" },
  { id: 2, nameKey: "ui.enc_mode_tabs", desc: "Ctrl + Tab / Ctrl + Shift + Tab" },
  { id: 3, nameKey: "ui.enc_mode_undo", desc: "Ctrl + Z / Ctrl + Y" },
  { id: 4, nameKey: "ui.enc_mode_actions", descKey: "ui.enc_mode_actions_desc" },
];

const ACTION_TYPES = [
  { id: "none", nameKey: "ui.act_none" },
  { id: "launch", nameKey: "ui.act_launch" },
  { id: "url", nameKey: "ui.act_url" },
  { id: "command", nameKey: "ui.act_command" },
  { id: "media", nameKey: "ui.act_media" },
  { id: "macro", nameKey: "ui.act_macro" },
];

const modeName = (m) => (m.nameKey ? t(m.nameKey) : m.name || "");
const modeDesc = (m) => (m.descKey ? t(m.descKey) : m.desc || "");
const actionName = (a) => (a && a.nameKey ? t(a.nameKey) : (a && a.name) || "");

const MEDIA_KEYS = ["volume up", "volume down", "volume mute", "play/pause", "next track", "previous track"];

// 测试按钮空闲态的固定提示。执行中临时换成「仍在执行」，结束后恢复成这一句，
// 而不是把 title 清空。
const testBtnIdleTitle = () => t("ui.test_btn_title");

// 宏输入框 placeholder 里展示的键名列表（映射规则见 host/app.py 的
// MACRO_KEY_NAMES）。这里只是提示，真正的归一化在主机端完成。
const macroKeyHint = () =>
  "Tab Enter Esc Space Backspace Insert Delete Home End PageUp PageDown " +
  "Left Right Up Down LCtrl RCtrl LShift RShift LAlt RAlt LWin RWin " +
  t("ui.macro_key_hint");

// 按键引脚校验。判定规则只有一份，在 host/app.py 的 validate_pins()
// 里（与固件 isValidButtonPin / isValidPinSet 对齐）。这里只负责把当前 9 个
// 值交给只读的 /api/defaults 取回逐行错误，不在 JS 里再抄一遍规则。
const PIN_COUNT = 9;

/* ---------------- 按键布局 ----------------
 * 固件回报 LAYOUT:1（编码器，10 输入）或 LAYOUT:0（12 键，12 输入）。
 * 旧固件不回这一行 → 配置里没有 layout 字段 → 仍按 10 输入渲染，
 * 行为与今天完全一致。 */
const INPUT_COUNT_ENCODER = 10;
const INPUT_COUNT_12KEY = 12;
const LAYOUT_ENCODER = 1;
const LAYOUT_12KEY = 0;

function layoutOf() {
  return Number((state.cfg || {}).layout) === LAYOUT_12KEY ? LAYOUT_12KEY : LAYOUT_ENCODER;
}

function is12Key() {
  return layoutOf() === LAYOUT_12KEY;
}

function inputCount() {
  return is12Key() ? INPUT_COUNT_12KEY : INPUT_COUNT_ENCODER;
}

/** 第 i 个输入的显示名（0 起）；第 10 个在两种布局下含义不同。 */
function inputTitle(i) {
  if (i < 9) return t("ui.input_button_n_js", { n: i + 1 });
  if (i === 9) return is12Key() ? t("ui.input_button_10_config") : t("ui.input_encoder_press");
  return is12Key()
    ? t("ui.input_button_extra_js", { n: i + 1 })
    : t("ui.input_button_n_js", { n: i + 1 });
}
// 停止输入 300 ms 后才校验：13 改成 1 会先经过空串，不能每敲一个字符就飘红。
const PIN_VALIDATE_DEBOUNCE_MS = 300;

const state = {
  devices: [],
  selected: null,
  deviceRows: new Map(), // dev_id -> <li>，增量渲染用，避免轮询时清空重建
  // dev_id -> { cfg, applied, lastResult, lastApply, ota, otaFile, otaStep }
  // 每台设备各自保存草稿、待下发基线与上次下发结果，切换设备不丢。
  buckets: new Map(),
  selectedConnected: false,
  cfg: null,
  keycodes: [],
  logSeq: 0,
  logFollow: true,     // 日志是否跟随最新行；上滚即暂停
  logPending: 0,       // 暂停期间新到达、尚未查看的行数
  logFilter: { kind: "", level: "", q: "", dev: "" }, // 日志页的客户端过滤（不影响轮询与 seq）
  dirty: false,
  saveTimer: null,
  preview: false,
  desktop: null,
  ota: null,           // {status, sent, acked, total, percent, error, filename, sha256, version}
  otaFile: null,       // File object chosen by the user
  otaStep: null,       // pick / check / confirm / upload / result
  editingKey: null,    // 抽屉里正在编辑的按键下标；null = 未打开
  keyDrawerReturn: null, // 关闭抽屉后要还焦点的卡片按钮
  macroView: null,     // 宏编辑器上次用的视图：steps / text
  running: {},         // dev_id -> {"<idx>": 已运行秒数}，来源 /api/status
  stopping: {},        // dev_id -> {"<idx>": true}，已发出停止请求、等状态确认
  scan: null,          // 最近一次扫描的结论，来源 /api/status
  encRunning: {},     // dev_id -> {"CW"|"CCW": 已运行秒数}，来源 /api/status
  readStatus: {},     // dev_id -> {at, fields, ok}，来源 /api/status 的 read 字段
  // WS-512：本机没有存档（cfg.trusted === false）时，用户点了「先忽略」的设备。
  // 只作用于本次会话，不写 config、不清 trusted、不影响下发确认。
  untrustedIgnored: {},
  // WS-512：本次会话里成功从设备读取过的设备。下发确认用它避免重复骚扰；
  // 它不同于 trusted（trusted 会落盘，会话读取只活在内存）。
  sessionRead: {},
  sysinfo: {},        // dev_id -> 运行状态采样，来源 /api/device/<id>/sysinfo 与 /api/status
  sysinfoBusy: {},    // dev_id -> true 表示已有一条采样请求在等回包（防重复请求）
  pinErrors: [],      // 当前引脚表单的逐行错误，来源 /api/defaults 的 pinErrors
  pinRules: null,     // 当前 chip 的引脚规则/提示，来源 /api/defaults 的 pinRules
  wakeInputs: null,   // WS-542：可唤醒输入，来源 /api/defaults 的 wakeInputs
  wakeSupported: true, // WS-542：固件 >= 1.3.9 才显示「可唤醒输入」行
  pinValidateSeq: 0,  // 每次校验自增，丢弃过期响应（防抖 / 快速切换设备时乱序）
  pinValidateTimer: null,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    // 后端 400 会带 {ok:false,error:"按键 3 不能为空"}，把它显示出来，
    // 别再让用户只看到 "400 BAD REQUEST"。
    let body = null;
    try {
      body = await res.json();
    } catch (_) {
      /* 不是 JSON：退回状态码文案 */
    }
    const detail = (body && (body.error || body.reason)) || "";
    const code = (body && (body.error_code || body.reason_code)) || "";
    const url = (body && body.release_url) || "";
    // 错误码要一起给用户：只报一句「更新失败」的话，用户没法反馈。
    // 完整报文挂在 err.payload 上，需要 Release 链接的界面自己取。
    let text = detail || `${res.status} ${res.statusText}`;
    if (code) text += t("ui.error_code_suffix", { code });
    const err = new Error(text);
    err.payload = body || null;
    err.error_code = code;
    err.release_url = url;
    throw err;
  }
  return res.json();
}

/* ---------------- 按设备分桶 ---------------- */
function bucketFor(devId) {
  let b = state.buckets.get(devId);
  if (!b) {
    b = {
      cfg: null,
      applied: null,      // 已下发到设备的快照，用来算「待下发 N 项」
      lastResult: null,   // 上一次下发的逐项结果（失败时用于常驻浮层）
      lastApply: null,    // { at, failed }
      ota: null,
      otaFile: null,
      otaStep: null,
    };
    state.buckets.set(devId, b);
  }
  return b;
}

function snapshotCfg(cfg) {
  return JSON.parse(JSON.stringify(cfg || {}));
}

/** 离开当前设备前，把活动状态写回它的桶。 */
function saveCurrentBucket() {
  if (!state.selected) return;
  const b = bucketFor(state.selected);
  b.cfg = state.cfg;
  b.ota = state.ota;
  b.otaFile = state.otaFile;
  b.otaStep = state.otaStep;
}

function sameJson(a, b) {
  return JSON.stringify(a === undefined ? null : a) === JSON.stringify(b === undefined ? null : b);
}

function cfgLayout(cfg) {
  return Number((cfg || {}).layout) === LAYOUT_12KEY ? LAYOUT_12KEY : LAYOUT_ENCODER;
}

function firstChangedIndex(next, prev) {
  const a = Array.isArray(next) ? next : [];
  const b = Array.isArray(prev) ? prev : [];
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) {
    if (!sameJson(a[i], b[i])) return i;
  }
  return 0;
}

/** 当前设备相对「已下发快照」的改动；返回空数组表示没有待下发项。 */
function collectPending() {
  const b = state.selected ? bucketFor(state.selected) : null;
  if (!b || !b.applied || !state.cfg) return [];
  const cfg = state.cfg;
  const base = b.applied;
  const out = [];
  if (!sameJson(cfg.keys, base.keys)) {
    out.push({ field: "keys", label: t("ui.keys_title"), tab: "keys", index: firstChangedIndex(cfg.keys, base.keys) });
  }
  if (cfgLayout(cfg) !== cfgLayout(base)) {
    out.push({ field: "layout", label: t("ui.layout_label"), tab: "device" });
  }
  if (!sameJson(cfg.pins, base.pins)) {
    out.push({ field: "pins", label: t("ui.group_pins"), tab: "device", index: firstChangedIndex(cfg.pins, base.pins) });
  }
  if (!sameJson(cfg.sleep, base.sleep)) {
    out.push({ field: "sleep", label: t("ui.sleep_card"), tab: "device" });
  }
  if (!sameJson(cfg.enc, base.enc)) {
    out.push({ field: "enc", label: t("ui.panel_encoder"), tab: "encoder" });
  }
  return out;
}

function updatePendingUi() {
  const items = collectPending();
  const badge = $("#pendingBadge");
  if (badge) {
    badge.hidden = !items.length;
    badge.textContent = items.length ? t("ui.pending_items", { count: items.length }) : "";
    badge.title = items.length ? t("ui.pending_jump", { label: items[0].label }) : "";
  }
  const apply = $("#btnApply");
  if (apply) {
    apply.textContent = t("ui.apply_config");
    apply.classList.toggle("dirty", !!items.length);
  }
  state.dirty = !!items.length;
  renderLastApply();
}

function renderLastApply() {
  const el = $("#lastApplyInfo");
  if (!el) return;
  const b = state.selected ? state.buckets.get(state.selected) : null;
  const la = b && b.lastApply;
  if (!la) {
    el.hidden = true;
    el.textContent = "";
    el.title = "";
    el.onclick = null;
    el.classList.remove("bad");
    return;
  }
  el.hidden = false;
  el.textContent = t("ui.last_apply_line", {
    time: fmtClock(la.at),
    result: la.failed ? t("ui.items_failed", { count: la.failed }) : t("ui.all_applied_short"),
  });
  el.classList.toggle("bad", !!la.failed);
  el.title = la.failed ? t("ui.view_pending_items") : t("ui.all_applied");
  el.onclick = la.failed && b.lastResult ? () => renderApplyResults(b.lastResult) : null;
}

function renderActionsState() {
  const on = !!state.actionsEnabled;
  const toggle = $("#actionsToggle");
  if (toggle) toggle.checked = on;
  const keysHint = $("#keysActionsHint");
  if (keysHint) {
    keysHint.textContent = on
      ? t("ui.actions_on_long")
      : t("ui.actions_off_long");
    keysHint.classList.toggle("off", !on);
  }
  const devHint = $("#actionsStateHint");
  if (devHint) {
    devHint.textContent = on
      ? t("ui.actions_on_short")
      : t("ui.actions_off_short");
  }
}

/** 点击「待下发 N 项」：切到对应页签并聚焦第一处改动。 */
function focusPendingItem() {
  const item = collectPending()[0];
  if (!item) return;
  const tab = document.querySelector(`.tab[data-tab="${item.tab}"]`);
  if (tab && !tab.classList.contains("active")) tab.click();
  setTimeout(() => {
    if (item.field === "keys") {
      // 按键改动落到摘要卡上，真正的编辑入口是抽屉：直接打开并聚焦 HID。
      openKeyDrawer(item.index || 0);
      return;
    }
    let target = null;
    if (item.field === "pins") {
      target = $$("#pinGrid .pin input")[item.index || 0] || null;
    } else if (item.field === "enc") {
      target = document.querySelector(".mode-card");
    } else if (item.field === "sleep") {
      target = $("#sleepEnabled");
    } else if (item.field === "layout") {
      target = $("#layoutEncoder");
    }
    if (!target) return;
    target.scrollIntoView({ block: "center" });
    const focusable = target.matches("input, select, textarea, button")
      ? target : target.querySelector("input, select, textarea, button");
    if (focusable) focusable.focus({ preventScroll: true });
  }, 0);
}

/* ---------------- notices ---------------- */
const NOTICE_AUTO_DISMISS_MS = 3000;
const NOTICE_MAX = 5;

function removeNotice(item) {
  if (!item || !item.parentNode) return;
  clearTimeout(item._timer);
  item.classList.add("leaving");
  setTimeout(() => item.remove(), 160);
}

function showNotice(msg, kind) {
  const center = $("#noticeCenter");
  if (!center) return;

  const item = document.createElement("div");
  item.className = "notice" + (kind ? " " + kind : "");

  const text = document.createElement("span");
  text.className = "notice-text";
  text.textContent = msg;
  item.appendChild(text);

  const close = document.createElement("button");
  close.type = "button";
  close.className = "notice-close";
  close.setAttribute("aria-label", t("ui.close_hint"));
  close.textContent = "×";
  close.onclick = () => removeNotice(item);
  item.appendChild(close);

  center.appendChild(item);

  // 超出上限时优先丢弃最早的非错误通知；错误留给用户手动关闭。
  while (center.children.length > NOTICE_MAX) {
    const oldest = center.querySelector(".notice:not(.bad)") || center.firstElementChild;
    clearTimeout(oldest._timer);
    oldest.remove();
  }

  // 成功/提示 3 秒消退；错误常驻，避免长文案没读完就消失。
  if (kind !== "bad") {
    item._timer = setTimeout(() => removeNotice(item), NOTICE_AUTO_DISMISS_MS);
  }
}

/** 兼容旧调用点：toast(msg) / toast(msg, "ok") / toast(msg, "bad")。 */
function toast(msg, kind = "") {
  showNotice(msg, kind === "bad" ? "bad" : kind === "ok" ? "ok" : "");
}

/* ---------------- devices ---------------- */

async function pollStatus() {
  try {
    const s = await api("/api/status");
    state.devices = s.devices || [];
    // 设备列表（id / 名字）变化时重算日志设备下拉与行标签；签名没变就跳过，
    // 免得每 1.2 秒把 300 行都扫一遍。
    const devSig = state.devices.map((d) => `${d.dev_id}:${d.name || ""}`).join("|");
    if (devSig !== logDevSig) {
      logDevSig = devSig;
      syncLogDeviceUi();
    }
    state.actionsEnabled = !!s.actions_enabled;
    state.running = s.running || {};
    // 最近一次扫描的结论（为什么没找到设备）。
    state.scan = s.scan || null;
    // additive field — old front-ends simply ignore it.
    state.encRunning = s.encoder_running || {};
    // the server remembers the last read-back per device, so the
    // device page keeps showing "上次读取" across a page reload.
    state.devices.forEach((d) => {
      if (d.read) state.readStatus[d.dev_id] = d.read;
    });
    // 运行状态采样只存在服务端内存里，页面刷新后靠这个字段恢复；
    // 断开时卡片显示的就是这份「上次采样」。
    state.sysinfo = Object.assign({}, state.sysinfo, s.sysinfo || {});
    renderReadStatus();
    renderActionsState();

    if (!state.selected || !state.devices.some((d) => d.dev_id === state.selected)) {
      const preferred =
        state.devices.find((d) => d.connected) || state.devices[0] || null;
      if (preferred) {
        await selectDevice(preferred.dev_id);
      } else {
        state.selected = null;
        if (!state.preview) {
          state.preview = true;
          try {
            state.cfg = await api("/api/defaults");
            renderKeys();
            renderEncoder();
            renderPins();
            renderDeviceMeta();
            // WS-512：预览模式没有「这台设备」，不显示无存档提示条。
            renderUntrustedBanner();
          } catch (_) {
            /* ignore */
          }
        }
        renderDevices();
        updatePendingUi();
      }
    } else {
      renderDevices();
    }

    const sel = state.devices.find((d) => d.dev_id === state.selected);
    // 设备断开后，上一次的配置结果已经无法再操作，直接收起浮层。
    const nowConnected = !!(sel && sel.connected);
    const wasConnected = state.selectedConnected;
    if (wasConnected && !nowConnected) clearApplyResults();
    // 重新连上时立刻补一次运行状态（断开期间卡片保留旧值）。
    if (state.selected && !wasConnected && nowConnected) {
      loadSysinfo(state.selected);
    }
    state.selectedConnected = nowConnected;
    // WS-512：连接状态变了，「从设备读取」的可用性跟着变。
    renderUntrustedBanner();
    // 连接状态变化时重绘卡片：断开后必须出现「已断开，非实时」。
    if (state.selected && wasConnected !== nowConnected) renderSysinfo();
    const connected = state.devices.filter((d) => d.connected).length;
    if (sel) {
      $("#statusDot").className = "dot " + (sel.connected ? "ok" : "warn");
      $("#statusText").textContent = sel.connected ? t("ui.connected") : t("ui.disconnected");
    } else {
      $("#statusDot").className = "dot bad";
      $("#statusText").textContent = t("ui.no_devices");
    }
    // 副标题按优先级说明「为什么连不上」（蓝牙不可用 > 单设备失败
    // 原因 > 扫描结论），而不是一律显示「请给设备上电」。
    let sub;
    if (s.ble_ok === false) {
      sub = t("ui.ble_unavailable_short", { reason: s.ble_error || t("ui.unknown_reason") });
    } else if (sel && !sel.connected && sel.last_error) {
      sub = t("ui.not_connected_reason", { reason: sel.last_error.text });
    } else if (!sel) {
      sub = scanDiagSub(state.scan)
        || (state.preview ? t("ui.show_defaults_preview") : t("ui.power_on_device"));
    } else {
      const fw = sel.fw && sel.fw.version
        ? t("ui.fw_chip_line", { version: sel.fw.version, chip: sel.fw.chip || "" })
        : t("ui.waiting_device_info");
      sub = `${sel.name} · ${fw}`;
    }
    $("#statusSub").textContent = sub;
    $("#statusSub").title = sub;
    highlightKeys(s.last_press || {});
    applyRunningState();
    applyEncoderRunningState();
  } catch (e) {
    $("#statusDot").className = "dot bad";
    $("#statusText").textContent = t("ui.host_not_ready");
    $("#statusSub").textContent = String(e.message || e);
    $("#statusSub").title = String(e.message || e);
  }
}

/**
 * 扫描诊断文案。
 *
 * - 没有 scan 字段 = 旧后端：返回 null，界面保持原样。
 * - at 为 null = 还没扫完：显示「扫描中…」，不要显示「0 个 BLE 设备」。
 * - 有 reason = 说明这次扫描为什么没找到设备。
 */
function scanDiagSub(scan) {
  if (!scan) return null;
  if (scan.at === null || scan.at === undefined) return t("ui.scanning");
  if (scan.reason) return t("ui.no_devices_reason", { reason: scan.reason });
  return null;
}

/** 扫描时间戳 → HH:MM:SS（本地时区）。 */
function fmtClock(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/**
 * 设备列表按 dev_id 复用 <li>：轮询只更新文本与状态类，
 * 不清空重建，避免 1.2 秒一次的 hover / focus 丢失。
 */
function renderDevices() {
  const ul = $("#deviceList");

  if (!state.devices.length) {
    state.deviceRows.forEach((li) => li.remove());
    state.deviceRows.clear();
    renderDeviceEmpty(ul);
    return;
  }

  const empty = ul.querySelector(".device-empty");
  if (empty) empty.remove();

  const seen = new Set();
  state.devices.forEach((d) => {
    seen.add(d.dev_id);
    let li = state.deviceRows.get(d.dev_id);
    if (!li) {
      li = document.createElement("li");
      li.className = "device";
      // 设备列表是单选列表：键盘 Tab 进入，Enter / 空格选中。
      li.setAttribute("role", "option");
      li.tabIndex = 0;
      li.innerHTML = '<span class="dot"></span><span class="dev-main"><b></b><small></small></span>';
      li.onclick = () => selectDevice(d.dev_id);
      li.onkeydown = (e) => {
        if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
        e.preventDefault();
        selectDevice(d.dev_id);
      };
      li._dot = li.querySelector(".dot");
      li._name = li.querySelector("b");
      li._sub = li.querySelector("small");
      state.deviceRows.set(d.dev_id, li);
    }

    const active = d.dev_id === state.selected;
    if (li.classList.contains("active") !== active) li.classList.toggle("active", active);
    li.setAttribute("aria-selected", active ? "true" : "false");

    // 未连接且已知失败原因时，用原因摘要代替地址，圆点变琥珀色。
    const reason = !d.connected && d.last_error ? d.last_error.text : "";
    const dotClass = "dot" + (d.connected ? " ok" : (reason ? " warn" : ""));
    if (li._dot.className !== dotClass) li._dot.className = dotClass;

    const name = d.name || d.dev_id || "BindDeckLite";
    if (li._name.textContent !== name) li._name.textContent = name;
    // 名字被省略号截断时，悬停看全文。
    if (li._name.title !== name) li._name.title = name;
    const label = t("ui.device_with_state", {
      name,
      state: d.connected ? t("ui.connected") : t("ui.disconnected"),
    });
    if (li.getAttribute("aria-label") !== label) li.setAttribute("aria-label", label);

    const sub = reason || d.address || "";
    if (li._sub.textContent !== sub) li._sub.textContent = sub;
    const subClass = reason ? "reason" : "";
    if (li._sub.className !== subClass) li._sub.className = subClass;
    // 地址同样可能被截断，悬停提示用全文（失败原因本来就是全文）。
    if (li._sub.title !== sub) li._sub.title = sub;
  });

  // 扫描后设备可能消失：移除对应的行。
  state.deviceRows.forEach((li, id) => {
    if (seen.has(id)) return;
    li.remove();
    state.deviceRows.delete(id);
  });

  // 设备顺序可能变化：按顺序移动节点，不重建。
  state.devices.forEach((d, i) => {
    const li = state.deviceRows.get(d.dev_id);
    if (li && ul.children[i] !== li) ul.insertBefore(li, ul.children[i] || null);
  });
}

/** 空列表引导只在首次创建，后续只更新「最后扫描」一行。 */
function renderDeviceEmpty(ul) {
  let li = ul.querySelector(".device-empty");
  if (!li) {
    li = document.createElement("li");
    li.className = "device-empty";
    li.innerHTML = [
      t("ui.empty_devices_title"),
      t("ui.empty_devices_step1"),
      t("ui.empty_devices_step2"),
      t("ui.empty_devices_step3"),
      '<span class="dev-scan" hidden></span>',
    ].join("");
    li._scan = li.querySelector(".dev-scan");
    ul.appendChild(li);
  }

  const scan = state.scan;
  if (!scan || scan.at === null || scan.at === undefined) {
    li._scan.hidden = true;
    return;
  }
  const tail = scan.reason ? ` · ${scan.reason}` : "";
  const text = t("ui.last_scan", { time: fmtClock(scan.at), tail });
  if (li._scan.textContent !== text) li._scan.textContent = text;
  li._scan.hidden = false;
}

async function selectDevice(devId) {
  if (state.selected === devId) {
    renderDevices();
    return;
  }

  // 切换设备：先保存上一台的草稿 / 待下发基线 / 下发结果 / OTA 状态。
  saveCurrentBucket();
  clearApplyResults();
  closeOtaWizard();
  // 抽屉绑定的是上一台设备的 key 下标；切设备时直接收起，不还焦点
  // （焦点要落在新设备的 DOM 上，由后续渲染决定）。
  if (typeof closeKeyDrawer === "function") closeKeyDrawer(false);
  state.selected = devId;
  state.preview = false;
  // WS-542：可唤醒输入属于上一台设备的芯片/布局/引脚，先清掉，避免切换瞬间
  // 把旧设备的结论画到新设备上；本次 validatePins 返回后会重新填。
  state.wakeInputs = null;
  localStorage.setItem("binddecklite.device", devId);
  renderDevices();

  const b = bucketFor(devId);
  state.ota = b.ota;
  state.otaFile = b.otaFile;
  state.otaStep = b.otaStep;

  // 有草稿就直接恢复，不重新请求，避免覆盖用户尚未下发的修改。
  if (b.cfg) {
    state.cfg = b.cfg;
    renderKeys();
    renderEncoder();
    renderPins();
    renderDeviceMeta();
    if (!b.applied) b.applied = snapshotCfg(state.cfg);
    updatePendingUi();
    renderOta();
    renderUntrustedBanner();
    if (b.lastResult) renderApplyResults(b.lastResult);
    loadSysinfo(devId);
    probeDeviceLayout(devId);
    return;
  }

  try {
    const cfg = await api(`/api/config/${encodeURIComponent(devId)}`);
    const bucket = bucketFor(devId);
    if (state.selected !== devId) {
      // 请求期间用户又切走了：结果只写回它自己的桶，不渲染到别的设备。
      bucket.cfg = cfg;
      if (!bucket.applied) bucket.applied = snapshotCfg(cfg);
      return;
    }
    state.cfg = cfg;
    renderKeys();
    renderEncoder();
    renderPins();
    renderDeviceMeta();
    bucket.cfg = state.cfg;
    bucket.applied = snapshotCfg(state.cfg);
    bucket.lastResult = null;
    setDirty(false);
    renderOta();
    renderUntrustedBanner();
    // 切换设备后按设备各自采样，不与上一台的缓存混用。
    loadSysinfo(devId);
    // 问一次设备的布局。旧固件不回 LAYOUT: → 「固件不支持」，
    // 12 键选项直接禁用，而不是让用户选完再被后端拒绝。
    probeDeviceLayout(devId);
  } catch (e) {
    toast(t("ui.read_config_failed") + e.message, "bad");
  }
}

/* ---------------- keys ---------------- */

function renderKeys() {
  const grid = $("#keyGrid");
  grid.innerHTML = "";
  if (!state.cfg) return;
  const total = inputCount();
  const layoutText = is12Key() ? t("ui.layout_12_suffix") : "";
  $("#keyHint").textContent = state.preview
    ? t("ui.preview_no_device")
    : t("ui.inputs_summary", { total, layout: layoutText });
  // 后端 device_cfg() 已把历史配置的 keys 幂等补齐到 12 条；这里只
  // 兜住异常数据。补出来的条目必须写进 state.cfg.keys —— 以前每次渲染都新建
  // 临时对象，第 11/12 张卡上改的键码落在临时对象里，重绘即丢（改完看不见）。
  if (!Array.isArray(state.cfg.keys)) state.cfg.keys = [];
  const keys = state.cfg.keys;
  while (keys.length < total) {
    keys.push({ code: 0, label: t("ui.not_configured"),
                action: { type: "none", value: "", macro: [] } });
  }
  for (let i = 0; i < total; i++) {
    const item = keys[i];
    const card = document.createElement("div");
    // 12 键布局下第 10–12 张是「扩展」输入（原来编码器的按压 / A / B 三脚），
    // 单独标色，和第 1–9 张普通按键区分开。
    const extended = is12Key() && i >= 9;
    card.className = "keycard" + (i === 9 ? " encoder-push" : "") + (extended ? " extended" : "");
    card.dataset.idx = i;
    card.innerHTML = `
      <div class="kc-head">
        <span class="kc-num">${i === 9 && !is12Key() ? "⏻" : i + 1}</span>
        <span class="kc-title">${inputTitle(i)}</span>
        <span class="kc-live"></span>
      </div>
      <div class="kc-sum">
        <div class="kc-line"><span class="kc-tag">HID</span><b class="kc-hid"></b></div>
        <div class="kc-line"><span class="kc-tag">${t("ui.card_action_tag")}</span><b class="kc-act"></b></div>
      </div>
      <div class="kc-actions">
        <button type="button" class="btn tiny edit">${t("ui.card_edit")}</button>
        <button type="button" class="btn tiny test">${t("ui.card_test")}</button>
      </div>`;
    grid.appendChild(card);

    // 摘要文本、测试按钮的可用态都由 keys.js 统一刷新。
    refreshKeyCard(i);
    $(".edit", card).onclick = () => openKeyDrawer(i);
    $(".test", card).onclick = () => testKeyAction(item, i, card);
  }
  // 布局从 12 键切回 10 键时，抽屉可能停在不存在的下标上。
  if (state.editingKey !== null && state.editingKey >= total && typeof closeKeyDrawer === "function") {
    closeKeyDrawer(false);
  }
  applyRunningState();
}

/* ---------- 执行中态 + 停止入口 ----------
 * 服务端 /api/status 的 running 字段是唯一权威来源；这里只在点击测试的
 * 5 秒等待窗口内先本地标记，随后由轮询结果覆盖。
 *
 * 执行期间按钮不再是灰掉的「执行中…」，而是「停止」：点它就请求服务端在
 * 下一步边界退出。已经发出的按键收不回来，所以文案必须写明这一点。 */

// 执行中（可点）与已请求停止（不可点）两态的提示。
const stopBtnTitle = () => t("ui.stop_hint");
const stoppingBtnTitle = () => t("ui.stop_requested");

function runningFor(devId) {
  return (state.running && state.running[devId]) || {};
}

function markRunning(devId, idx, elapsed = 0) {
  state.running = state.running || {};
  const map = state.running[devId] || (state.running[devId] = {});
  if (map[String(idx)] === undefined) map[String(idx)] = elapsed;
}

function clearRunning(devId, idx) {
  const map = runningFor(devId);
  delete map[String(idx)];
}

function stoppingFor(devId) {
  return (state.stopping && state.stopping[devId]) || {};
}

function markStopping(devId, idx) {
  state.stopping = state.stopping || {};
  const map = state.stopping[devId] || (state.stopping[devId] = {});
  map[String(idx)] = true;
}

function clearStopping(devId, idx) {
  delete stoppingFor(devId)[String(idx)];
}

function isStopping(devId, idx) {
  return !!stoppingFor(devId)[String(idx)];
}

/** 停止后补两次状态快照：轮询间隔 1.2 秒，撑不到「1 秒内恢复」的验收。 */
function nudgeStatus() {
  setTimeout(pollStatus, 250);
  setTimeout(pollStatus, 750);
}

/**
 * 请求停止一条正在执行的动作。
 *
 * 服务端在下一步边界才真正退出，所以「请求已受理」不等于「已经停了」：
 * 这里保持「正在停止…」，由 /api/status 不再报 running 时自动回到
 * 「测试动作」，所以卡片不会先跳回「停止」再跳一次。
 *
 * WS-498：服务端只在**没有 claim** 时才回 already_done（宏确实结束了）；
 * claim 还在但停止标记已置位时回的是 cancelling —— 那说明宏还在收尾
 * （长的 text: / app: 步骤无法中途打断），这里必须继续显示「正在停止…」，
 * 不能把卡片切回空闲，否则 1.2 秒后的轮询又会把它变成「停止」，按钮来回跳。
 */
async function stopAction(devId, idx) {
  if (!devId || isStopping(devId, idx)) return;   // 连点兜底：按钮已 disabled
  markStopping(devId, idx);
  applyRunningState();
  let r = null;
  try {
    r = await api(`/api/action/cancel/${encodeURIComponent(devId)}/${idx}`, { method: "POST" });
  } catch (e) {
    r = null;
  }
  if (!r || r.ok === false) {
    // 网络异常：退回「停止」，让用户还能再点一次；不写「停止失败」日志。
    clearStopping(devId, idx);
    applyRunningState();
    toast(t("ui.stop_not_delivered"), "warn");
    return;
  }
  if (r.cancelling) {
    // 宏还在收尾：保持「正在停止…」，等轮询确认它真的退出。
    markStopping(devId, idx);
  } else if (r.already_done) {
    // 服务端确认没有在跑的 claim（宏刚好结束）：直接回空闲态。
    clearRunning(devId, idx);
    clearStopping(devId, idx);
  }
  applyRunningState();
  toast(t("ui.stopped"), "");        // 中性提示：停止不是失败
  nudgeStatus();
}

function setCardRunning(card, secs, stopping) {
  const live = secs !== undefined && secs !== null;
  card.classList.toggle("running", live);
  const btn = $(".test", card);
  if (!btn) return;
  if (live && stopping) {
    // 已请求停止：防连点，等状态轮询确认退出。
    btn.disabled = true;
    btn.textContent = t("ui.stopping");
    btn.classList.add("danger");
    btn.title = stoppingBtnTitle();
    return;
  }
  if (live) {
    // 执行中：按钮可点，就是止损入口。
    btn.disabled = false;
    btn.textContent = t("ui.stop");
    btn.classList.add("danger");
    btn.title = stopBtnTitle();
    return;
  }
  // 「仅 HID」的按键没有上位机动作可测；按钮置灰并给出原因，
  // 不再允许点出一句「动作执行成功：无上位机动作」的假成功。
  const noAction = card.dataset.noaction === "1";
  btn.disabled = noAction;
  btn.textContent = t("ui.test_action");
  btn.classList.remove("danger");
  btn.title = noAction ? t("ui.test_none") : testBtnIdleTitle();
  // 动作结束后顺手清掉残留的停止标记，避免下次执行还显示「正在停止…」。
  const idx = card.dataset.idx;
  if (idx !== undefined && state.selected) clearStopping(state.selected, idx);
}

function applyRunningState() {
  // 预览 / 无选中设备时没有任何运行项；status 缺少该项即按未运行渲染。
  const running = state.preview || !state.selected ? {} : runningFor(state.selected);
  const stopping = state.preview || !state.selected ? {} : stoppingFor(state.selected);
  $$(".keycard").forEach((card) => {
    const idx = String(parseInt(card.dataset.idx, 10));
    setCardRunning(card, running[idx], stopping[idx]);
  });
  // 抽屉里也有一个测试按钮，状态必须跟卡片同步。
  if (state.editingKey !== null && typeof syncKeyDrawerRunning === "function") {
    syncKeyDrawerRunning();
  }
}

function renderActionInput(holder, item, card) {
  const action = item.action || { type: "none", value: "" };
  holder.innerHTML = "";
  if (action.type === "none") {
    // 键码 0 = 不发键。没有上位机动作时这个键按下去什么都不会发生，
    // 必须说清楚，否则用户只会看到一个「没反应」的死键。
    holder.innerHTML = item.code === 0
      ? t("ui.key_sends_nothing")
      : t("ui.key_sends_hid_only");
    return;
  }
  if (action.type === "media") {
    const sel = document.createElement("select");
    sel.className = "in-val";
    MEDIA_KEYS.forEach((m) => {
      const o = document.createElement("option");
      o.value = m;
      o.textContent = m;
      if (action.value === m) o.selected = true;
      sel.appendChild(o);
    });
    sel.onchange = () => {
      action.value = sel.value;
      markDirty();
    };
    holder.appendChild(sel);
    return;
  }
  if (action.type === "macro") {
    if (holder.dataset && holder.dataset.macroEditor === "1") {
      // 按键抽屉：结构化步骤 + 纯文本双视图（keys.js）。
      // 编码器那一行太窄，仍使用下面的 DSL 文本框。
      mountMacroEditor(holder, action);
      return;
    }
    const ta = document.createElement("textarea");
    ta.className = "in-val macro";
    ta.rows = 3;
    // 键名列表放在第一行：卡片里的输入框只有 3 行高，用户一眼能看到支持
    // 哪些键名；完整示例与语法说明见右上角「帮助」。
    ta.placeholder =
      t("ui.macro_key_label") + macroKeyHint() + "\n" +
      t("ui.macro_placeholder") +
      t("ui.macro_example");
    ta.title = t("ui.macro_hint");
    ta.value = formatMacro(action.macro || []);
    ta.oninput = () => {
      action.macro = parseMacro(ta.value);
      action.value = ta.value;
      markDirty();
    };
    holder.appendChild(ta);
    return;
  }
  const input = document.createElement("input");
  input.className = "in-val";
  input.value = action.value || "";
  input.placeholder =
    action.type === "command" ? "notepad.exe" : action.type === "url" ? "https://example.com" : "C:\\path\\to\\app.exe";
  input.oninput = () => {
    action.value = input.value;
    markDirty();
  };
  if (action.type !== "launch") {
    holder.appendChild(input);
    return;
  }

  // 启动程序 / 文件：文本框 + 原生文件选择器 + 最近使用过的路径
  const row = document.createElement("div");
  row.className = "browse-row";
  const listId = "launch-recents-" + (card ? card.dataset.idx : "x");
  const list = document.createElement("datalist");
  list.id = listId;
  fillRecentOptions(list);
  input.setAttribute("list", listId);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn tiny browse";
  btn.textContent = t("ui.browse");
  btn.title = t("ui.browse_title");
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = t("ui.browsing");
    try {
      const res = await fetch("/api/browse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "file" }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        toast(data.error || t("ui.browse_unavailable"), "bad");
        return;
      }
      if (data.path) {
        input.value = data.path;
        action.value = data.path;
        markDirty();
        await refreshDesktop();
        syncLaunchRecents();
      }
    } catch (e) {
      toast(t("ui.file_pick_failed") + (e.message || e), "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = t("ui.browse");
    }
  };
  row.append(input, btn, list);
  holder.appendChild(row);
}

function highlightKeys(lastPress) {
  const p = lastPress[state.selected];
  const idx = p ? p.index : -1;
  const fresh = p && p.age < 0.9;
  $$(".keycard").forEach((card) => {
    card.classList.toggle("press", !!(fresh && parseInt(card.dataset.idx, 10) === idx));
  });
}

/* ---------------- encoder ---------------- */

function renderEncoder() {
  const grid = $("#encoderModes");
  grid.innerHTML = "";
  if (!state.cfg) return;
  const disabled = is12Key();
  const note = $("#encoderDisabledNote");
  if (note) note.hidden = !disabled;
  const mode = state.cfg.encoder ? state.cfg.encoder.mode : 0;
  ENCODER_MODES.forEach((m) => {
    const el = document.createElement("button");
    el.className = "mode-card" + (m.id === mode ? " active" : "") + (disabled ? " locked" : "");
    el.disabled = disabled;
    el.innerHTML = `<b>${modeName(m)}</b><span>${modeDesc(m)}</span>`;
    el.onclick = () => {
      if (disabled) {
        toast(t("ui.encoder_12key_unavailable"), "warn");
        return;
      }
      state.cfg.encoder = state.cfg.encoder || {};
      state.cfg.encoder.mode = m.id;
      renderEncoder();
      markDirty();
    };
    grid.appendChild(el);
  });
  // the rotation-action card depends on the active mode, so redraw it
  // whenever the mode grid is (re)rendered.
  renderEncoderActions();
}

/* ---------------- encoder rotation actions ---------------- */

function defaultEncoderAction() {
  return { type: "none", value: "", macro: [] };
}

function encRunningFor(devId) {
  return (state.encRunning && state.encRunning[devId]) || {};
}

function markEncRunning(devId, dir, elapsed = 0) {
  state.encRunning = state.encRunning || {};
  const map = state.encRunning[devId] || (state.encRunning[devId] = {});
  if (map[dir] === undefined) map[dir] = elapsed;
}

function clearEncRunning(devId, dir) {
  const map = encRunningFor(devId);
  delete map[dir];
}

function setEncRowRunning(row, secs) {
  const live = secs !== undefined && secs !== null;
  row.classList.toggle("running", live);
  const btn = $(".test", row);
  if (!btn) return;
  // 非「上位机动作」模式整卡置灰：测试按钮必须一直保持禁用。
  // 这里不能只写 btn.disabled = live —— 状态轮询每轮都会调用本函数，
  // 会把上面 renderEncoderActions 设的 disabled=true 覆盖掉，
  // 于是灰卡上的按钮又能点，出现「假成功」。
  const locked = row.dataset.locked === "1";
  const noAction = row.dataset.noaction === "1";
  btn.disabled = live || locked || noAction;
  btn.textContent = live ? t("ui.running") : t("ui.test_action");
  btn.title = live
    ? t("ui.action_running_for", { seconds: Number(secs).toFixed(1) })
    : locked ? t("ui.encoder_need_actions")
      : noAction ? t("ui.test_none")
        : testBtnIdleTitle();
}

function applyEncoderRunningState() {
  // 预览 / 无选中设备时没有任何运行项；status 缺少该项即按未运行渲染。
  const running = state.preview || !state.selected ? {} : encRunningFor(state.selected);
  $$(".enc-action-row").forEach((row) => {
    setEncRowRunning(row, running[row.dataset.dir]);
  });
}

function encoderStatusText(actions, editable) {
  // 顶部状态说明。必须跟着当前配置走：改完动作类型后要立刻反映出来，
  // 不能一直显示旧文案。
  if (!editable) return t("ui.encoder_need_actions_short");
  const cw = actions.CW || defaultEncoderAction();
  const ccw = actions.CCW || defaultEncoderAction();
  const cwType = cw.type || "none";
  const ccwType = ccw.type || "none";
  if (cwType === "none" && ccwType === "none") {
    return t("ui.encoder_none_hint");
  }
  const nameOf = (id) => {
    const hit = ACTION_TYPES.find((a) => a.id === id);
    return hit ? actionName(hit) : id;
  };
  return t("ui.encoder_summary", { cw: nameOf(cwType), ccw: nameOf(ccwType) }) +
    t("ui.encoder_f23_warning");
}

function renderEncoderActions() {
  const box = $("#encoderActions");
  box.innerHTML = "";
  if (!state.cfg) return;

  const enc = state.cfg.encoder || {};
  // 旧 config.json 没有 actions 字段：按「未配置」渲染，不报错。
  // 键名必须与 ENC: 方向值一致（"CW"|"CCW"），不能用小写。
  if (!enc.actions) {
    enc.actions = { CW: defaultEncoderAction(), CCW: defaultEncoderAction() };
  }
  const actions = enc.actions;                 // live reference into state.cfg
  if (!actions.CW) actions.CW = defaultEncoderAction();
  if (!actions.CCW) actions.CCW = defaultEncoderAction();
  const mode = enc.mode || 0;
  // 12 键布局没有旋转编码器，旋转动作同样不可用（配置保留，切回后恢复）。
  const editable = mode === 4 && !is12Key();

  const card = document.createElement("div");
  card.className = "enc-action-card" + (editable ? "" : " disabled");

  const statusEl = document.createElement("p");
  statusEl.className = "enc-action-status";
  const refreshStatus = () => {
    statusEl.textContent = is12Key()
      ? t("ui.encoder_12key_kept")
      : encoderStatusText(actions, editable);
  };
  refreshStatus();
  card.appendChild(statusEl);

  const dirs = [
    { dir: "CW", label: t("ui.clockwise") },
    { dir: "CCW", label: t("ui.counter_clockwise") },
  ];
  dirs.forEach(({ dir, label }) => {
    const action = actions[dir];
    const row = document.createElement("div");
    row.className = "enc-action-row";
    row.dataset.dir = dir;
    // 其他模式下整卡置灰：记住这个状态，状态轮询刷新执行中态时不能把
    // 测试按钮重新启用。
    row.dataset.locked = editable ? "0" : "1";
    // 「仅 HID」同样没有上位机动作；轮询刷新执行中态时不能把它重新点亮。
    row.dataset.noaction = (!action || (action.type || "none") === "none") ? "1" : "0";
    row.innerHTML = `
      <span class="enc-dir-label">${label}</span>
      <select class="sel-enc-act">
        ${ACTION_TYPES.map((a) =>
          `<option value="${a.id}" ${a.id === action.type ? "selected" : ""}>${a.name}</option>`).join("")}
      </select>
      <div class="enc-act-input"></div>
      <button class="btn tiny test">${t("ui.test_action")}</button>`;
    card.appendChild(row);

    const sel = row.querySelector("select");
    const holder = row.querySelector(".enc-act-input");
    const btn = row.querySelector("button");

    sel.onchange = () => {
      actions[dir] = { type: sel.value, value: "", macro: [] };
      row.dataset.noaction = (!actions[dir] || (actions[dir].type || "none") === "none") ? "1" : "0";
      renderActionInput(holder, { action: actions[dir] },
                       { dataset: { idx: "enc-" + dir } });
      applyEncoderRunningState();
      refreshStatus();
      markDirty();
    };
    renderActionInput(holder, { action: action },
                     { dataset: { idx: "enc-" + dir } });

    // 其他 4 个模式下整卡置灰且不可编辑：动作类型、输入区、测试按钮全禁用。
    if (!editable) {
      sel.disabled = true;
      btn.disabled = true;
      holder.querySelectorAll("input, select, textarea").forEach((el) => {
        el.disabled = true;
      });
    }

    btn.onclick = async () => {
      if (row.classList.contains("running")) {
        // 按钮此时本应 disabled，兜底防止键盘触发第二次动作。
        toast(t("log.action_still_running_dir"), "warn");
        return;
      }
      if (!editable) {
        // 灰卡上的按钮本应 disabled，兜底挡住键盘触发。
        toast(t("ui.encoder_need_actions"), "warn");
        return;
      }
      const devId = state.selected;
      if (!devId || !state.selectedConnected) {
        toast(t("ui.no_device_selected"), "bad");
        return;
      }
      const noAction = !actions[dir] || (actions[dir].type || "none") === "none";
      if (noAction) {
        // 按钮本应 disabled；这里兜住键盘触发，不给假成功。
        toast(t("ui.test_none"), "warn");
        return;
      }
      markEncRunning(devId, dir);
      applyEncoderRunningState();
      let r = null;
      try {
        r = await api(`/api/encoder/action/test/${encodeURIComponent(devId)}/${dir}`,
                      { method: "POST" });
        if (r.ok) {
          const used = r.browser_used && r.browser_used !== "default"
            ? ` · ${r.browser_used}` : "";
          toast(t("ui.action_ok_prefix") + actions[dir].type + used, "ok");
        } else if (r.running) {
          if (devId) markEncRunning(devId, dir, r.elapsed || 0);
          applyEncoderRunningState();
          toast(r.error || t("log.action_still_running_dir"), "warn");
        } else {
          toast(t("ui.action_failed_prefix") + (r.error || t("ui.unknown_error")), "bad");
        }
      } catch (e) {
        toast(e.message, "bad");
      } finally {
        if (!(r && r.running)) {
          if (devId) clearEncRunning(devId, dir);
          applyEncoderRunningState();
        }
      }
    };
  });

  box.appendChild(card);
  applyEncoderRunningState();
}

/* ---------------- device ---------------- */

/* 运行状态：固件只回报事实，文案与着色都在这里。
   采样值保存在服务端内存（/api/status 的 sysinfo 字段），断开时继续显示
   上一次的值并标注「已断开，非实时」，绝不把旧值当实时值渲染。 */
function sysinfoFor(devId) {
  return (state.sysinfo && state.sysinfo[devId]) || null;
}

async function loadSysinfo(devId) {
  if (!devId || state.sysinfoBusy[devId]) return null;
  // OTA 期间设备忙于固件通道：不发命令，卡片显示「OTA 中，暂停查询」。
  if (otaBusy()) {
    if (state.selected === devId) renderSysinfo();
    return null;
  }
  const dev = state.devices.find((d) => d.dev_id === devId);
  if (dev && !dev.connected) {
    // 断开时不发请求，保留上次采样值。
    if (state.selected === devId) renderSysinfo();
    return null;
  }
  state.sysinfoBusy[devId] = true;
  try {
    const r = await api(`/api/device/${encodeURIComponent(devId)}/sysinfo`, { method: "POST" });
    // reason=disconnected：命令根本没送到，保留旧值；其余（含超时=固件不支持）
    // 都是新的一次采样结论。
    if (r && r.reason !== "disconnected" && r.reason !== "no_device") {
      state.sysinfo[devId] = r;
    }
  } catch (_) {
    // 上位机自身异常不弹红 toast、不写 error 日志：卡片标注为暂时读不到。
    const prev = sysinfoFor(devId) || {};
    state.sysinfo[devId] = Object.assign({}, prev, { stale: true });
  } finally {
    state.sysinfoBusy[devId] = false;
    if (state.selected === devId) renderSysinfo();
  }
  return sysinfoFor(devId);
}

/** 首屏状态条：连接 / 运行时长 / 可用空间；完整字段仍看下方「运行状态」卡片。 */
function renderStatusStrip() {
  const conn = $("#stripConn");
  if (!conn) return;
  const uptime = $("#stripUptime");
  const heap = $("#stripHeap");
  const devId = state.selected;
  const dev = devId ? state.devices.find((d) => d.dev_id === devId) : null;
  const connected = !!(devId && dev && dev.connected);
  conn.textContent = !devId || state.preview ? t("ui.not_selected") : connected ? t("ui.connected") : t("ui.disconnected");
  conn.className = connected ? "" : "bad";
  if (otaBusy()) {
    uptime.textContent = t("ui.ota_running_badge");
    heap.textContent = "—";
    return;
  }
  const info = devId && !state.preview ? sysinfoFor(devId) : null;
  if (info && info.supported === false) {
    uptime.textContent = t("ui.fw_not_supported");
    heap.textContent = "—";
    return;
  }
  const d = (info && info.display) || {};
  uptime.textContent = d.uptime || (connected ? t("ui.reading") : "—");
  heap.textContent = d.heap || "—";
}

function renderSysinfo() {
  renderStatusStrip();
  const box = $("#sysInfo");
  const atEl = $("#sysSampleAt");
  if (!box || !atEl) return;
  const devId = state.selected;
  if (!devId || state.preview) {
    box.innerHTML = `<span class="k">${t("ui.status_label")}</span><span class="v muted">${t("ui.no_device_selected")}</span>`;
    atEl.textContent = "";
    return;
  }
  if (otaBusy()) {
    box.innerHTML = `<span class="k">${t("ui.status_label")}</span><span class="v warn">${t("ui.ota_pause_query")}</span>`;
    atEl.textContent = "";
    return;
  }
  const dev = state.devices.find((d) => d.dev_id === devId);
  const connected = !!(dev && dev.connected);
  const info = sysinfoFor(devId);

  if (!info) {
    box.innerHTML = `<span class="k">${t("ui.status_label")}</span><span class="v muted">${
      connected ? t("ui.reading") : t("ui.no_sample_offline")
    }</span>`;
    atEl.textContent = "";
    return;
  }

  if (info.supported === false) {
    // 旧固件：2 秒内没有 SYS: 行。这是「不支持」，不是错误。
    box.innerHTML = `<span class="k">${t("ui.status_label")}</span><span class="v muted">${t("ui.sysinfo_unsupported")}</span>`;
    atEl.textContent = (info.at ? t("ui.sampled_at", { time: fmtClock(info.at) }) : "")
      + (connected ? "" : t("ui.disconnected_not_live"));
    return;
  }

  const d = info.display || {};
  const flags = info.flags || {};
  const notes = info.notes || {};
  const resetNote = notes.deepsleep
    ? `<span class="muted small"> · ${escapeHtml(notes.deepsleep)}</span>` : "";
  const rows = [
    [t("ui.last_reset"), escapeHtml(d.reset || t("log.reset_unknown")) + resetNote, flags.deepsleep ? "warn" : ""],
    [t("ui.strip_uptime"), escapeHtml(d.uptime || t("log.reset_unknown")), ""],
    ["NVS", escapeHtml(d.nvs || t("log.reset_unknown")), flags.nvs_bad ? "bad" : ""],
    [t("ui.free_heap"), escapeHtml(d.heap || t("log.reset_unknown")), flags.heap_low ? "warn" : ""],
  ];
  const extraNotes = [];
  if (notes.safe) extraNotes.push(`<li>${escapeHtml(notes.safe)}</li>`);
  if (notes.heap) extraNotes.push(`<li>${escapeHtml(notes.heap)}</li>`);
  box.innerHTML = rows
    .map(([k, v, cls]) => `<span class="k">${k}</span><span class="v ${cls}">${v}</span>`)
    .join("") + (extraNotes.length
      ? `<span class="k"></span><ul class="sys-notes">${extraNotes.join("")}</ul>` : "");

  const when = info.at ? t("ui.sampled_at", { time: fmtClock(info.at) }) : "";
  const stale = connected ? "" : t("ui.disconnected_not_live");
  atEl.textContent = when + stale;
}

/** 向设备确认布局与「是否支持 12 键」，不阻塞页面。 */
async function probeDeviceLayout(devId) {
  if (!devId) return;
  try {
    const r = await api(`/api/device/${encodeURIComponent(devId)}/layout`, { method: "POST" });
    if (state.selected !== devId || !state.cfg) return;
    if (!r.sent) return;            // 设备未连接：结论未知，保持现状
    state.cfg.layout_supported = r.supported;
    if (r.layout === LAYOUT_12KEY || r.layout === LAYOUT_ENCODER) {
      state.cfg.layout = r.layout;  // 以设备为准（配网页可能刚改过）
    }
    renderKeys();
    renderEncoder();
    renderPins();
    renderDeviceMeta();
    // 布局是设备自报的，不应被算成「待下发」；只有用户已经改过其他字段时才保留差异。
    if (!state.dirty) {
      const b = bucketFor(devId);
      if (b.applied) {
        b.applied.layout = state.cfg.layout;
        b.applied.layout_supported = state.cfg.layout_supported;
      }
    }
    updatePendingUi();
    // 布局探测只更新布局（绝不置 trusted）；提示条里的「按键布局已从设备读取」
    // 依赖 layout_supported，所以重绘一次。
    renderUntrustedBanner();
  } catch (_) {
    /* 旧后端或链路异常：布局维持本地配置，不影响其他功能 */
  }
}

/** 设备页「按键布局」卡片的状态与说明。 */
function renderLayoutCard() {
  const enc = $("#layoutEncoder");
  const twelve = $("#layout12Key");
  const hint = $("#layoutHint");
  if (!enc || !twelve) return;
  const supported = (state.cfg || {}).layout_supported;
  const blocked = supported === false;
  // 旧固件：12 键选项直接禁用，并说明原因（而不是选了才在应用时失败）。
  twelve.disabled = blocked;
  if (blocked && is12Key()) {
    // 本地配置是 12 键但固件不支持（例如刚降级）：先按设备真实布局显示。
    state.cfg.layout = LAYOUT_ENCODER;
    enc.checked = true;
    twelve.checked = false;
    renderKeys();
    renderEncoder();
  } else {
    enc.checked = !is12Key();
    twelve.checked = is12Key();
  }
  if (blocked) {
    hint.textContent = t("ui.layout_unsupported_old");
    return;
  }
  hint.textContent = is12Key()
    ? t("ui.layout_12key_hint")
    : t("ui.layout_encoder_hint");
}

function setLayout(value) {
  if (!state.cfg) return;
  state.cfg.layout = value;
  renderKeys();
  renderEncoder();
  renderPins();
  renderDeviceMeta();
  markDirty();
}

function renderDeviceMeta() {
  if (!state.cfg) return;
  const chip = state.cfg.chip || "ESP32";
  const fw = state.cfg.fw || {};
  // 概览 hero：设备名是主信息，芯片 / 固件 / 编译号合并成次行（不再重复展示）。
  const name = state.cfg.name || state.selected || "—";
  const nameEl = $("#devName");
  if (nameEl) { nameEl.textContent = name; nameEl.title = name; }
  const subEl = $("#devSub");
  if (subEl) {
    const parts = [chip];
    if (fw.version) parts.push(t("ui.fw_version_short", { version: fw.version }));
    if (fw.build) parts.push(t("ui.build_label", { build: fw.build }));
    subEl.textContent = parts.join(" · ");
    subEl.title = subEl.textContent;
  }
  const layoutEl = $("#devLayout");
  if (layoutEl) {
    layoutEl.textContent = is12Key() ? t("ui.layout_12key_name") : t("ui.layout_encoder_name");
  }
  renderLayoutCard();
  $("#sleepEnabled").checked = !!(state.cfg.sleep && state.cfg.sleep.enabled);
  $("#sleepMinutes").value = (state.cfg.sleep && state.cfg.sleep.minutes) || 5;

  // host binding status
  const bond = state.cfg.bond || { bound: false, addr: "" };
  const bondEl = $("#bondStatus");
  const clearBtn = $("#btnClearBond");
  if (bond.bound) {
    // the live NimBLE bond state now arrives on BOND_DIAG (separate
    // from BOND_STATUS), so the address stays clean. Show it next to the addr.
    const diag = bond.peer_sec === undefined ? "" :
      `<span class="muted small">${t("ui.bond_diag", {
        count: bond.peer_sec,
        state: bond.nimble_bond ? t("ui.bond_ok") : t("ui.bond_missing"),
      })}</span>`;
    bondEl.innerHTML = `<span class="dot ok"></span>${t("ui.bound_as", { addr: `<b>${escapeHtml(bond.addr || "—")}</b>` })}${diag}`;
    clearBtn.hidden = false;
    $("#bondHint").textContent = t("ui.bond_clear_hint");
  } else {
    bondEl.innerHTML = `<span class="dot warn"></span>${t("ui.unbound")}`;
    clearBtn.hidden = true;
    $("#bondHint").textContent = t("ui.bond_pair_hint");
  }

  // 引脚提示文案也来自后端规则（/api/defaults 的 pinRules），避免 JS 里再写一份
  // 与固件可能漂移的引脚限制。
  renderPinHint();
  renderSleepWake();
  // 设备页的「上次读取」状态行。
  renderReadStatus();
  // 运行状态卡片（采样请求由 selectDevice / 重连 / 刷新 / 打开设备页触发）。
  renderSysinfo();
}

function renderPins() {
  const box = $("#pinGrid");
  box.innerHTML = "";
  if (!state.cfg) return;
  // 旧 config.json 可能缺项 / 是 null / 甚至不是数组：照原样渲染，由校验标红，
  // 不在这里替用户补默认值。
  const pins = Array.isArray(state.cfg.pins) ? state.cfg.pins : [];
  for (let i = 0; i < PIN_COUNT; i++) {
    const wrap = document.createElement("label");
    wrap.className = "pin";
    const name = document.createElement("span");
    name.textContent = t("ui.input_button_n_js", { n: i + 1 });
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0";
    input.max = "39";
    input.value = pins[i] === null || pins[i] === undefined ? "" : pins[i];
    input.dataset.pinIndex = String(i);
    input.oninput = () => {
      const raw = input.value.trim();
      state.cfg.pins = Array.isArray(state.cfg.pins) ? state.cfg.pins : [];
      // 空串就是「空」，不能像以前那样写 parseInt("") = NaN —— JSON.stringify
      // 会把 NaN 变成 null，旧后端在 int(None) 上直接 500。
      state.cfg.pins[i] = raw === "" ? null
        : (Number.isFinite(Number(raw)) ? Number(raw) : raw);
      markDirty();
      schedulePinValidation();
    };
    // 失焦立刻校验，不必等防抖计时器。
    input.onblur = () => validatePins();
    const err = document.createElement("small");
    err.className = "pin-error";
    err.setAttribute("aria-live", "polite");
    err.hidden = true;
    wrap.append(name, input, err);
    box.appendChild(wrap);
  }
  // 12 键布局下第 10–12 键固定在原编码器三脚上，只读展示。
  renderFixedPins();
  // 每次重绘都先清掉上一轮的结论，再按当前值重算，避免旧红框挂在新值上。
  applyPinErrors([]);
  validatePins();
}

/** 12 键布局的三行只读引脚（第 10–12 键 = 原编码器 SW / CLK / DT）。 */
function renderFixedPins() {
  const box = $("#pinGrid");
  if (!box) return;
  $$(".pin-fixed", box).forEach((el) => el.remove());
  if (!is12Key()) return;
  const ext = (state.pinRules && state.pinRules.extended) || null;
  const values = ext || ["?", "?", "?"];
  values.forEach((pin, i) => {
    const idx = 9 + i;
    const wrap = document.createElement("div");
    wrap.className = "pin pin-fixed";
    const name = document.createElement("span");
    name.textContent = t("ui.fixed_key", { n: idx + 1 });
    const val = document.createElement("span");
    val.className = "mono";
    val.textContent = `GPIO ${pin}`;
    wrap.append(name, val);
    box.appendChild(wrap);
  });
}

/** 防抖：停止输入 300 ms 后校验整张引脚表。 */
function schedulePinValidation() {
  clearTimeout(state.pinValidateTimer);
  state.pinValidateTimer = setTimeout(() => {
    state.pinValidateTimer = null;
    validatePins();
  }, PIN_VALIDATE_DEBOUNCE_MS);
}

/**
 * 向只读接口要逐行错误并渲染。
 *
 * 规则来自后端：把 chip 和 9 个原始值原样传过去，服务端用 validate_pins()
 * 判定。返回 [{index, code, message}]，空数组 = 全部合法。
 */
async function validatePins() {
  clearTimeout(state.pinValidateTimer);
  state.pinValidateTimer = null;
  if (!state.cfg) {
    applyPinErrors([]);
    return [];
  }
  const pins = Array.isArray(state.cfg.pins) ? state.cfg.pins : [];
  const tokens = [];
  for (let i = 0; i < PIN_COUNT; i++) {
    const v = pins[i];
    tokens.push(v === null || v === undefined ? "" : String(v));
  }
  const seq = ++state.pinValidateSeq;
  const fwVersion = (state.cfg.fw && state.cfg.fw.version) || "";
  const query = `?chip=${encodeURIComponent(state.cfg.chip || "ESP32")}`
    + `&layout=${layoutOf()}`
    + `&pins=${encodeURIComponent(tokens.join(","))}`
    + `&fw=${encodeURIComponent(fwVersion)}`;
  let errors;
  try {
    const r = await api("/api/defaults" + query);
    errors = Array.isArray(r.pinErrors) ? r.pinErrors : [];
    if (r.pinRules) {
      state.pinRules = r.pinRules;
      renderPinHint();
      renderFixedPins();   // keep the fixed rows in sync with the rule
    }
    // WS-542: the wake-capability line comes from the same backend call; the
    // rule itself lives in app.wake_inputs(), the JS only formats it. A stale
    // response (seq moved on) must not repaint it with older pins.
    if (seq === state.pinValidateSeq) {
      state.wakeInputs = r.wakeInputs || null;
      state.wakeSupported = r.wakeSupported !== false;
      renderSleepWake();
    }
  } catch (_) {
    // 上位机后端暂时取不到：保留上一次结论，绝不假装全部合法。
    return state.pinErrors || [];
  }
  if (seq !== state.pinValidateSeq) return state.pinErrors || [];  // 有更新的请求
  applyPinErrors(errors);
  return errors;
}

/** 把错误列表画到对应行：红边 + 输入框下方红字 + 标题旁计数。 */
function applyPinErrors(errors) {
  state.pinErrors = Array.isArray(errors) ? errors : [];
  const byIndex = {};
  state.pinErrors.forEach((e) => {
    if (e && typeof e.index === "number") byIndex[e.index] = e.message || "";
  });
  $$("#pinGrid .pin").forEach((row, i) => {
    const msg = byIndex[i] || "";
    const input = row.querySelector("input");
    const slot = row.querySelector(".pin-error");
    row.classList.toggle("bad", !!msg);
    if (slot) {
      slot.textContent = msg;
      slot.hidden = !msg;
    }
    if (input) {
      if (msg) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
  });
  renderPinErrorCount();
}

/** 引脚卡片下方的说明文字：来自后端 pinRules，和 validate_pins 同一份规则。 */
function renderPinHint() {
  const el = $("#pinHint");
  if (el && state.pinRules) el.textContent = state.pinRules.hint || "";
}

/**
 * 休眠卡里的只读「可唤醒输入：…」行（WS-542）。
 *
 * 谁能唤醒由后端 wake_inputs() 决定（芯片 + 当前布局 + 当前引脚），JS 只负责
 * 把输入号排成「按键 1–9」这种区间文案。固件 < 1.3.9 不显示：那时的固件只有
 * 编码器按压一条 ext0 唤醒源，承诺不了这一行。
 */
function renderSleepWake() {
  const el = $("#sleepWake");
  if (!el) return;
  const info = state.wakeInputs;
  if (!state.cfg || !info || !state.wakeSupported) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  const blocked = (info.no_wake || [])
    .filter((x) => x && x.reason !== "ext1-all-low")
    .map((x) => x.index);
  const allLow = (info.no_wake || [])
    .filter((x) => x && x.reason === "ext1-all-low")
    .map((x) => x.index);
  let text = t("ui.sleep_wake_line", { list: formatWakeList(info.wake) });
  if (allLow.length) {
    text += t("ui.list_separator")
      + t("ui.sleep_wake_all_low", { list: formatWakeList(allLow) });
  }
  if (blocked.length) {
    text += t("ui.list_separator")
      + t("ui.sleep_wake_blocked", { list: formatWakeList(blocked) });
  }
  el.textContent = text;
  el.hidden = false;
}

/** [0,1,2,3,5,9] -> 「按键 1–4、6；第 10 键」的区间文案（输入号为 0 基）。 */
function formatWakeList(indices) {
  const list = (indices || [])
    .filter((i) => Number.isInteger(i) && i >= 0)
    .sort((a, b) => a - b);
  if (!list.length) return t("ui.sleep_wake_none");
  const twelve = is12Key();
  const parts = [];
  let start = null;
  let prev = null;
  const flush = () => {
    if (start !== null) parts.push(wakePiece(start, prev, twelve));
    start = null;
  };
  list.forEach((i) => {
    if (start === null) {
      start = i;
      prev = i;
      return;
    }
    if (i === prev + 1) {
      prev = i;
      return;
    }
    flush();
    start = i;
    prev = i;
  });
  flush();
  return parts.join(t("ui.list_comma"));
}

/** 一个输入区间/单点：0–8 叫「按键 N」，9 起按布局叫「编码器按压」或「第 N 键」。 */
function wakePiece(a, b, twelve) {
  if (a === b) {
    if (a < 9) return t("ui.input_button_n_js", { n: a + 1 });
    if (a === 9 && !twelve) return t("ui.input_encoder_press");
    return t("ui.sleep_wake_key_n", { n: a + 1 });
  }
  if (a < 9) return t("ui.sleep_wake_range", { from: a + 1, to: b + 1 });
  return t("ui.sleep_wake_key_range", { from: a + 1, to: b + 1 });
}

function renderPinErrorCount() {
  const el = $("#pinErrorCount");
  if (!el) return;
  const n = state.pinErrors.length;
  el.textContent = n ? t("ui.pin_errors", { count: n }) : "";
  el.hidden = !n;
}

/** 滚动并聚焦第一处错误行（非平滑滚动，避免长时间动画）。 */
function focusFirstPinError(errors) {
  const first = (errors || [])[0];
  if (!first) return;
  // 引脚块默认收起：先展开，否则 scrollIntoView 看不到目标。
  const block = $("#pinsBlock");
  if (block && !block.open) block.open = true;
  const row = $$("#pinGrid .pin")[first.index];
  if (!row) return;
  row.scrollIntoView({ block: "center" });
  const input = row.querySelector("input");
  if (input) input.focus();
}

/* ---------------- desktop shell ---------------- */

async function refreshDesktop() {
  try {
    state.desktop = await api("/api/desktop");
    renderDesktop();
  } catch (_) {
    /* 旧版后端或未就绪：静默忽略 */
  }
}

function renderDesktop() {
  const d = state.desktop;
  if (!d) return;
  const badge = $("#desktopMode");
  badge.textContent = d.frozen ? t("ui.mode_frozen") : d.desktop_mode ? t("ui.mode_tray") : t("ui.mode_source");

  const auto = d.autostart || {};
  const isWindows = String(d.platform || "").startsWith("win");
  const toggle = $("#autostartToggle");
  toggle.checked = !!auto.enabled;
  toggle.disabled = !auto.supported;
  // 以管理员身份运行：只有 Windows 能用（计划任务 + 最高权限）。
  const adminToggle = $("#adminAutostartToggle");
  adminToggle.checked = !!auto.admin;
  // 未开自启动时禁用它：勾上会顺手把开机自启动打开，用户未必想要。
  adminToggle.disabled = !auto.admin_supported || !auto.enabled;
  // .row 是 display:flex（作者样式），[hidden] 的 display:none 会被覆盖，
  // 必须直接改 style.display。
  $("#adminAutostartRow").style.display = auto.admin_supported ? "" : "none";
  // 提权重启靠 ShellExecuteW("runas")，同样只有 Windows 有。
  $("#btnRestartAdmin").hidden = !isWindows;
  $("#minimizedToggle").checked = !!d.start_minimized;
  // close_button_behavior: "minimize" (default) → checked, "exit" → unchecked
  $("#closeMinimizeToggle").checked = (d.close_button_behavior || "minimize") === "minimize";
  $("#btnQuitHost").hidden = !d.desktop_mode;
  // Show "显示窗口" only when the native window exists but is hidden
  // (minimize-to-tray with no tray available — recovery path).
  $("#btnShowWindow").hidden = !(d.desktop_mode && d.window_hidden);

  const bits = [];
  if (d.tray_active) bits.push(t("ui.tray_hint_ok"));
  else if (d.desktop_mode) {
    if (d.window_hidden) bits.push(t("ui.tray_hint_hidden"));
    else bits.push(t("ui.tray_hint_background"));
  }
  else bits.push(t("ui.tray_hint_source"));
  if (auto.supported) {
    bits.push(t("ui.autostart_state", {
      state: auto.enabled ? t("ui.autostart_on") : t("ui.autostart_off"),
      method: auto.method,
    }));
  } else {
    bits.push(t("ui.autostart_unavailable"));
  }
  if (auto.error) bits.push(auto.error);
  // auto.admin：自启动条目是不是「计划任务（最高权限）」。
  // auto.is_admin：当前这个进程本身有没有提权。两者互相独立。
  if (auto.admin) bits.push(t("ui.autostart_admin_task"));
  if (isWindows && !auto.is_admin) {
    bits.push(t("ui.autostart_not_elevated"));
  }
  if (auto.admin_error) bits.push(auto.admin_error);
  const desktopError = (d.error || "").replace(/^(托盘不可用：|Tray unavailable: )/, "");
  if (desktopError && !bits.includes(desktopError)) bits.push(desktopError);
  $("#desktopHint").textContent = bits.join(t("ui.list_separator")) + t("ui.sentence_end");
  if (auto.detail) $("#desktopHint").title = auto.detail;
}

async function updateDesktop(patch) {
  const res = await fetch("/api/desktop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || t("error.setting_failed"));
  state.desktop = data;
  renderDesktop();
  return data;
}

/* ---------------- dirty / save ---------------- */

function markDirty() {
  setDirty(true);
  armPendingSave();
}

/** 取消挂起的自动保存（读取设备配置时必须先停掉，否则旧副本会写回服务端）。 */
function cancelPendingSave() {
  if (state.saveTimer) {
    clearTimeout(state.saveTimer);
    state.saveTimer = null;
  }
}

/** 重新挂起自动保存。把编辑时的设备与配置快照带进闭包，切换设备不会串档。 */
function armPendingSave() {
  cancelPendingSave();
  const devId = state.selected;
  const cfg = state.cfg;
  if (!devId || !cfg) return;
  state.saveTimer = setTimeout(() => saveConfigFor(devId, cfg), 500);
}

function setDirty(dirty) {
  state.dirty = !!dirty;
  updatePendingUi();
}

async function saveConfigFor(devId, cfg) {
  if (!devId || !cfg) return;
  try {
    await api(`/api/config/${encodeURIComponent(devId)}`, {
      method: "POST",
      body: JSON.stringify(cfg),
    });
  } catch (e) {
    toast(t("ui.save_failed") + e.message, "bad");
  }
}

async function applyConfig() {
  if (otaBusy()) {
    toast(t("ui.locked_ota"), "bad");
    return;
  }
  if (!state.selected) {
    toast(t("ui.no_device_preview"), "bad");
    return;
  }
  // 固定本次下发的设备与配置快照：等待期间用户切换设备时不要把结果记到别的桶。
  const devId = state.selected;
  const cfg = state.cfg;
  // 先校验。有错误就一条 CFG: 都不发，直接把用户带到第一处错误行。
  // 顺序放在「未连接设备」之后：预览/离线仍只提示未连接，不叠加两套 toast。
  const pinErrors = await validatePins();
  if (state.selected !== devId) return;
  if (pinErrors.length) {
    toast(t("ui.pin_error_count", { count: pinErrors.length }), "bad");
    focusFirstPinError(pinErrors);
    return;
  }
  // WS-512：本机没有这台设备的存档，界面里就是默认值。下发会覆盖设备当前的
  // 引脚 / 键码 / 编码器模式 / 休眠，所以先确认一次；取消时一条 CFG: 都不发
  // （在 saveConfigFor / /api/apply 之前 return）。
  if (cfg.trusted === false && !state.sessionRead[devId]) {
    // 一句话模板（含换行），不要用两句拼接：英文两句之间要有空格，中文不需要。
    const go = window.confirm(t("ui.untrusted_confirm"));
    if (!go) return;
  }
  await saveConfigFor(devId, cfg);
  try {
    const r = await api(`/api/apply/${encodeURIComponent(devId)}`, { method: "POST" });
    if (state.selected !== devId) return;

    // 旧固件收到 12 键配置时后端直接拒绝 —— 明确告诉用户原因，
    // 不要落到下面的「设备未连接？」兜底文案里假装失败得莫名其妙。
    if (!r.ok && r.error && !r.items) {
      toast(r.error, "bad");
      return;
    }

    // New contract: per-item readback results.
    if (r.items && r.items.length) {
      const allOk = r.ok && r.items.every((i) => i.status === "ok");
      if (allOk) {
        // WS-512：全部字段回读生效 = 本机存档就是设备现状，提示条可以收起了。
        cfg.trusted = true;
        state.sessionRead[devId] = true;
        renderUntrustedBanner();
      }
      recordApply(r.items, allOk);
      if (allOk) {
        toast(t("ui.config_applied"), "ok");
      } else {
        toast(t("ui.config_partly_applied"), "bad");
      }
      return;
    }

    // Legacy fallback (old backend that only returns {ok, results}).
    if (r.ok) {
      recordApply([], true);
      toast(t("ui.config_applied_count", { count: r.results.filter((x) => x.sent).length }), "ok");
    } else {
      toast(t("ui.apply_failed_offline"), "bad");
    }
  } catch (e) {
    toast(e.message, "bad");
  }
}

/* ---------------- apply results overlay ---------------- */

/** 记录一次下发结果：全部生效写「上次下发」常驻行，失败项保留浮层。 */
function recordApply(items, allOk) {
  if (!state.selected) return;
  const b = bucketFor(state.selected);
  const failed = (items || []).filter((i) => i.status !== "ok").length;
  b.lastApply = { at: Math.floor(Date.now() / 1000), failed: allOk ? 0 : Math.max(1, failed) };
  if (allOk) {
    b.applied = snapshotCfg(state.cfg);
    b.lastResult = null;
    clearApplyResults();
  } else {
    b.lastResult = items;
    renderApplyResults(items);
  }
  updatePendingUi();
}

function clearFieldHighlights() {
  $$(".field-highlight").forEach((el) => el.classList.remove("field-highlight"));
}

function clearApplyResults() {
  const existing = $("#applyResults");
  if (existing) existing.remove();
  clearFieldHighlights();
}

// 浮层固定定位，top 跟随顶部栏的实际底边：窄窗口下顶部栏会换行变高，
// 所以每次尺寸变化都重新测量，而不是写死一个数值。
function positionApplyResults() {
  const el = $("#applyResults");
  if (!el) return;
  const bar = $(".topbar");
  el.style.top = bar ? Math.round(bar.getBoundingClientRect().bottom + 8) + "px" : "16px";
}

function renderApplyResults(items) {
  // 重复应用配置时先清掉上一份，避免浮层叠加。
  clearApplyResults();

  const allOk = items.every((item) => item.status === "ok");

  const container = document.createElement("div");
  container.id = "applyResults";
  container.className = "apply-results";
  container.setAttribute("role", "status");
  container.setAttribute("aria-live", "polite");

  const head = document.createElement("div");
  head.className = "apply-results-head";
  const title = document.createElement("span");
  title.textContent = allOk ? t("ui.result_all_ok") : t("ui.result_partial");
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "apply-results-close";
  closeBtn.textContent = "×";
  closeBtn.title = t("ui.close_esc");
  closeBtn.setAttribute("aria-label", t("ui.close_result"));
  closeBtn.onclick = clearApplyResults;
  head.appendChild(title);
  head.appendChild(closeBtn);
  container.appendChild(head);

  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "apply-result-row " + item.status;

    let statusText;
    switch (item.status) {
      case "ok":
        statusText = t("ui.effective");
        break;
      case "rejected":
        statusText = t("ui.result_rejected", { reason: item.reason });
        break;
      case "unconfirmed":
        statusText = t("ui.result_unconfirmed", { reason: item.reason });
        break;
      case "unverified":
        statusText = item.reason;
        break;
      default:
        statusText = item.status;
    }

    const actionBtn = (item.status !== "ok")
      ? `<button class="btn tiny secondary accept-device-value" onclick="acceptDeviceValue('${item.field}')">${t("ui.accept_device_value")}</button>`
      : "";

    row.innerHTML = `
      <span class="result-label">${escapeHtml(item.label)}</span>
      <span class="result-status">${escapeHtml(statusText)}</span>
      <span class="result-action">${actionBtn}</span>`;
    container.appendChild(row);
  });

  // 浮层挂在 body 上并脱离文档流，不占用顶部栏的布局空间。
  document.body.appendChild(container);
  positionApplyResults();
  // 动态生成的「以设备为准」按钮也要服从 OTA 锁。
  applyOtaLock();

  // Highlight the form fields whose value was rejected.
  items.forEach((item) => {
    if (item.status === "rejected") highlightFormField(item.field);
  });
}

function highlightFormField(field) {
  switch (field) {
    case "pins":
      $$(".pin").forEach((el) => el.classList.add("field-highlight"));
      break;
    case "keys":
      $$(".keycard").forEach((el) => el.classList.add("field-highlight"));
      break;
    case "enc":
      $("#encoderModes").classList.add("field-highlight");
      break;
    case "sleep":
      $("#sleepEnabled").classList.add("field-highlight");
      break;
    case "tz":
      // No direct tz control in the UI; the hint text stays as-is.
      break;
  }
}

/* ---------------- 读取设备配置 ---------------- */

// 设备回报的配置分组（与服务端 app.py 的 _CFG_READ_FIELDS 一致）。
// 这四组全部回来才算一次完整的回读；缺任何一组都按「设备未回报」处理。
const READ_FIELDS = ["pins", "keys", "enc", "sleep"];
// 存函数而不是译文：切换语言后这些标签要跟着变（模块加载时求值会冻结）。
const READ_FIELD_LABELS = {
  pins: () => t("ui.group_pins"),
  keys: () => t("ui.group_keys"),
  enc: () => t("ui.group_enc"),
  sleep: () => t("ui.group_sleep"),
};

/** 回读结果里设备回报了几组配置。 */
function readFieldCount(fields) {
  const f = fields || {};
  return READ_FIELDS.filter((k) => f[k]).length;
}

/**
 * 读取设备配置：发 CMD:GET_CFG，等设备把配置回报回来。
 *
 * 两个入口（「读取设备配置」与结果浮层里的「以设备为准」）都走这里，
 * 都会用设备回报的值整体覆盖 state.cfg，所以：
 *
 * - OTA 进行中直接拒绝（applyOtaLock 之外的第二道保险）。
 * - state.dirty 时先确认：取消则不发送任何命令，表单保持原样。
 * - 请求期间用户切到别的设备 → 丢弃这次结果（多设备）。
 * - 设备没回报完整配置 → 不改表单，也不改服务端配置，只提示失败。
 */
async function readDeviceConfig(devId) {
  if (!devId) return null;
  if (otaBusy()) {
    toast(t("ui.cmd_locked_ota"), "bad");
    return null;
  }
  if (state.dirty) {
    if (!window.confirm(t("ui.unsent_confirm"))) return null;
  }
  // 挂起的自动保存必须先停掉：它会把前端旧副本写回服务端，把刚读回来的
  // 设备真值覆盖掉。
  const wasDirty = state.dirty;
  cancelPendingSave();
  try {
    const r = await api(`/api/device/${encodeURIComponent(devId)}/refresh`, { method: "POST" });
    // 多设备：请求期间用户切到了另一台 → 这次结果作废。
    if (state.selected !== devId) return null;
    state.readStatus[devId] = { at: r.at, fields: r.fields || {}, ok: !!r.ok };
    renderReadStatus();
    // WS-512：读失败 / 只回部分字段时提示条不动，但可能要把「缺少 xx」写出来。
    renderUntrustedBanner();
    if (!r.ok) {
      // 表单与本地配置都不动，但用户原本的修改仍要能存下去。
      if (wasDirty) armPendingSave();
      toast(t("ui.read_incomplete_old_fw", { reason: r.reason || t("ui.cfg_not_reported_short") }), "bad");
      return r;
    }
    applyReadback(r);
    // WS-512：本次会话确实从设备读到过配置 → 后面的下发不再重复确认。
    state.sessionRead[devId] = true;
    return r;
  } catch (e) {
    if (wasDirty) armPendingSave();
    toast(t("ui.read_config_failed_short") + (e.message || e), "bad");
    return null;
  }
}

/** 用设备回报的配置覆盖 state.cfg 并重绘整个表单。 */
function applyReadback(r) {
  if (!r || !r.cfg) return;
  state.cfg = r.cfg;
  if (state.selected) {
    const b = bucketFor(state.selected);
    b.cfg = state.cfg;
    b.applied = snapshotCfg(state.cfg);
    b.lastResult = null;
  }
  setDirty(false);
  renderKeys();
  renderEncoder();
  renderPins();
  renderDeviceMeta();
  renderReadStatus();
  renderUntrustedBanner();
  const got = readFieldCount(r.fields);
  toast(t("ui.read_done", { time: fmtClock(r.at), got, total: READ_FIELDS.length }), "ok");
}

/** 设备页状态行：上次读取时间 + 设备回报了几组（缺的标「设备未回报」）。 */
function renderReadStatus() {
  const el = $("#cfgReadStatus");
  if (!el) return;
  const st = state.readStatus[state.selected];
  if (!st || !st.at) {
    el.textContent = t("ui.cfg_not_read");
    return;
  }
  const got = readFieldCount(st.fields);
  const missing = READ_FIELDS.filter((k) => !(st.fields || {})[k]);
  let text = t("ui.last_read", { time: fmtClock(st.at), got, total: READ_FIELDS.length });
  if (!got) {
    // 设备一句都没回：不要笼统报成功。
    text += t("ui.cfg_not_reported_suffix");
  } else if (missing.length) {
    text += t("ui.list_separator") + missing.map((k) => t("ui.field_not_reported", { field: READ_FIELD_LABELS[k]() })).join(t("ui.list_comma"));
  }
  el.textContent = text;
}

/**
 * WS-512：本机没有这台设备的配置存档时，在页签下方常驻一条琥珀色提示。
 *
 * 判据只看设备自己的 `trusted` 字段（服务端决定）：新条目为 false，读取成功
 * 或下发全部生效后才是 true；旧 config.json 没有该字段 → undefined，按可信
 * 处理，不打扰存量用户。预览模式（未选中设备）不显示。
 */
function renderUntrustedBanner() {
  const el = $("#untrustedBanner");
  if (!el) return;
  const devId = state.selected;
  const cfg = state.cfg || {};
  const show = !!(devId && !state.preview && cfg.trusted === false &&
    !state.untrustedIgnored[devId]);
  el.hidden = !show;
  if (!show) return;

  // 布局是唯一一项从设备读回来的字段（WS-499 探测）；探测确实回了才这么说，
  // 不回就老实承认布局也是本机默认值。
  const note = $("#untrustedLayoutNote");
  if (note) note.textContent = cfg.layout_supported === true ? t("ui.layout_read_note") : "";

  // 旧固件只回部分字段：读取状态里缺哪几组就点名，并说明表单仍是默认值。
  const st = state.readStatus[devId];
  const extra = $("#untrustedExtra");
  const missing = st && st.at ? READ_FIELDS.filter((k) => !(st.fields || {})[k]) : [];
  if (extra) {
    const partial = missing.length > 0 && missing.length < READ_FIELDS.length;
    extra.hidden = !partial;
    extra.textContent = partial
      ? t("ui.read_incomplete") + missing.map((k) => READ_FIELD_LABELS[k]()).join(t("ui.list_comma"))
      : "";
  }

  // 未连接 / OTA 中都不能读；title 说明原因，而不是给一个点了没反应的按钮。
  const read = $("#btnUntrustedRead");
  if (read) {
    const connected = !!state.selectedConnected;
    read.disabled = !connected || otaBusy();
    read.title = !connected ? t("ui.read_offline")
      : otaBusy() ? t("ui.read_locked_ota")
        : t("ui.read_btn_title");
  }
}

/**
 * 「以设备为准」：把设备回报的值填回表单。
 *
 * 它和「读取设备配置」走同一条回读路径，所以同样受 OTA 锁与 dirty 确认
 * 约束；只有真的读到设备配置时才收起结果浮层，读失败时保留提示让用户
 * 看到原因。
 */
async function acceptDeviceValue(field) {
  if (!state.selected) return;
  const r = await readDeviceConfig(state.selected);
  if (r && r.ok) clearApplyResults();
}

/* ---------------- log ---------------- */

// 日志「跟随 / 暂停」。判据只有一条 —— 用户是否停在底部（≤ 24 px）。
// 纯逻辑在 log_follow.js（同样的代码 Node 测试直接 import）。
const LOG_FOLLOW_THRESHOLD = window.LogFollow.DEFAULT_THRESHOLD;

/**
 * 当前是否贴底。
 *
 * 返回 null 表示容器量不出高度（日志页被切到别的 tab），此时不做判定，
 * 暂停状态与计数跨 tab 保留。
 */
function logIsFollowing(list) {
  if (!list || list.clientHeight === 0) return null;
  const { scrollTop, scrollHeight, clientHeight } = list;
  if (Number.isFinite(scrollTop) && Number.isFinite(scrollHeight)) {
    return window.LogFollow.shouldFollow(scrollTop, scrollHeight, clientHeight, LOG_FOLLOW_THRESHOLD);
  }
  // pywebview 打包版偶发读到异常度量：退化为「最后一行底边 vs 容器底边」。
  const box = list.getBoundingClientRect();
  const last = list.lastElementChild;
  if (!last) return true;
  return last.getBoundingClientRect().bottom - box.bottom <= LOG_FOLLOW_THRESHOLD;
}

// 贴到底。scrollTop 写不进去时（pywebview / 异常度量）退化为 scrollTo，
// 一律用即时滚动，不用 smooth。
function stickLogToBottom(list) {
  list.scrollTop = list.scrollHeight;
  const maxTop = list.scrollHeight - list.clientHeight;
  if (!Number.isFinite(list.scrollTop) || maxTop - list.scrollTop > 1) {
    if (typeof list.scrollTo === "function") list.scrollTo({ top: list.scrollHeight, behavior: "auto" });
  }
}

// 暂停态下记住可视首行及其相对容器顶边的偏移；裁剪后据此校验补偿是否到位。
function captureLogAnchor(list) {
  const box = list.getBoundingClientRect();
  for (const row of list.children) {
    const r = row.getBoundingClientRect();
    if (r.bottom > box.top) return { row, offset: r.top - box.top };
  }
  return null;
}

// compensateTrim 之后的残余偏移（子像素、或 pywebview 写不进 scrollTop）再校一次。
function realignLogAnchor(list, anchor) {
  if (!anchor || !anchor.row.isConnected) return;
  const box = list.getBoundingClientRect();
  const delta = anchor.row.getBoundingClientRect().top - box.top - anchor.offset;
  if (Math.abs(delta) < 1) return;
  const target = list.scrollTop + delta;
  list.scrollTop = target;
  if (Math.abs(list.scrollTop - target) > 1 && typeof list.scrollTo === "function") {
    list.scrollTo({ top: target, behavior: "auto" });
  }
}

// 渲染胶囊与「已暂停」标签。stick=true 时顺带贴底（点胶囊、清空、切回日志页）。
function syncLogFollowUi(opts = {}) {
  const paused = !state.logFollow;
  const tag = $("#logPausedTag");
  const capsule = $("#logNew");
  const count = $("#logNewCount");
  if (tag) tag.hidden = !paused;
  if (capsule) capsule.hidden = !paused;
  if (count) count.textContent = window.LogFollow.pendingLabel(state.logPending);
  if (opts.stick && state.logFollow) {
    const list = $("#logList");
    if (list) stickLogToBottom(list);
  }
}

/* ---------------- 日志页过滤（纯客户端，不影响轮询与 seq） ---------------- */

function logFilterActive() {
  const f = state.logFilter;
  return !!(f.dev || f.kind || f.level || f.q);
}

function logRowMatches(row) {
  const f = state.logFilter;
  if (!window.LogDevice.rowMatchesDev(row.dataset.dev || "", f.dev || "")) return false;
  if (f.kind && (row.dataset.kind || "") !== f.kind) return false;
  if (f.level && (row.dataset.level || "info") !== f.level) return false;
  if (f.q && !(row.textContent || "").toLowerCase().includes(f.q)) return false;
  return true;
}

function applyLogRowVisibility(row) {
  // 用 hidden + CSS（.log-row[hidden]{display:none}）过滤，不动行节点，
  // 轮询追加与 log_follow 的裁剪补偿逻辑保持原样。
  row.hidden = logFilterActive() && !logRowMatches(row);
}

/* ---------------- 日志页设备标注 / 设备筛选 ---------------- */

// 上次同步时的设备签名（id:name|…），pollStatus 用它判断是否要重算下拉与标签。
let logDevSig = "";

// 设备并集 = 已发现设备 ∪ 日志缓冲里出现过的 dev。≥2 才标注设备，
// 单设备（以及旧固件 dev 为空）时界面与不标注设备时完全一致。
function logDeviceUnion() {
  const ids = state.devices.map((d) => d.dev_id).filter(Boolean);
  const list = $("#logList");
  const buffered = list ? Array.from(list.children).map((row) => row.dataset.dev || "") : [];
  return window.LogDevice.deviceUnion(ids, buffered);
}

/**
 * 增量对齐「设备」下拉的选项。
 *
 * 已有的 option 节点原地复用（只改文案 / 位置），只有设备真的消失才移除，
 * 避免每次轮询重建下拉、抢走键盘焦点。
 */
function syncLogDevOptions(select, union) {
  const ids = state.devices.map((d) => d.dev_id).filter(Boolean);
  const list = $("#logList");
  const buffered = list ? Array.from(list.children).map((row) => row.dataset.dev || "") : [];
  const want = window.LogDevice.deviceOptions(ids, buffered, state.devices);

  const existing = new Map();
  Array.from(select.options).forEach((opt) => existing.set(opt.value, opt));
  want.forEach((item, i) => {
    let opt = existing.get(item.value);
    if (opt) existing.delete(item.value);
    else {
      opt = document.createElement("option");
      opt.value = item.value;
    }
    if (opt.textContent !== item.label) opt.textContent = item.label;
    if (select.options[i] !== opt) select.insertBefore(opt, select.options[i] || null);
  });
  existing.forEach((opt) => opt.remove());
}

/**
 * 按设备并集同步整页设备标注：容器 5 列模式、下拉显隐与选项、已有行的标签。
 *
 * 行节点不重建，只在首行/正文之间补删一个 <span class="log-dev">；并集从
 * ≥2 掉回 1 时把标签、下拉和残留的设备过滤一起清掉，不留孤儿控件。
 */
function syncLogDeviceUi() {
  const list = $("#logList");
  if (!list) return;
  const union = logDeviceUnion();
  const show = window.LogDevice.showsDeviceTag(union);
  list.classList.toggle("show-dev", show);

  const select = $("#logDev");
  const label = $("#logDevLabel");
  if (label) label.hidden = !show;
  if (select) select.hidden = !show;

  let changed = false;
  if (!show) {
    // 并集掉回 1：下拉连同选项一起清掉，不留孤儿控件，也不留一个永远
    // 匹配不到任何行的设备过滤。
    if (select) {
      while (select.options.length) select.removeChild(select.options[0]);
      select.value = "";
    }
    if (state.logFilter.dev) {
      state.logFilter.dev = "";
      changed = true;
    }
  } else if (select) {
    syncLogDevOptions(select, union);
    // 选项以 state 为准回填下拉；选中的设备若已从选项里消失（关机且历史
    // 行也被裁掉），连同过滤条件一起复位。
    const want = state.logFilter.dev || "";
    if (want && !Array.from(select.options).some((o) => o.value === want)) {
      state.logFilter.dev = "";
      changed = true;
    }
    if (select.value !== (state.logFilter.dev || "")) select.value = state.logFilter.dev || "";
  }

  Array.from(list.children).forEach((row) => {
    const dev = row.dataset.dev || "";
    let span = row.querySelector(".log-dev");
    if (!show) {
      if (span) span.remove();
      return;
    }
    const text = window.LogDevice.rowDeviceLabel(dev, state.devices, union);
    if (!span) {
      span = document.createElement("span");
      span.className = "log-dev";
      const kind = row.querySelector(".log-kind");
      row.insertBefore(span, kind ? kind.nextSibling : row.children[2] || null);
    }
    if (span.textContent !== text) span.textContent = text;
    if (span.title !== text) span.title = text;
  });

  // 设备项被清掉后，之前生效的设备过滤要跟着失效并重算可见性。
  if (changed) applyLogFilter();
}

/** 只有过滤生效且有内容但全被隐藏时，才显示「没有匹配的日志」。 */
function syncLogFilterChrome() {
  const list = $("#logList");
  const empty = $("#logEmpty");
  if (empty && list) {
    const anyVisible = Array.from(list.children).some((row) => !row.hidden);
    empty.hidden = !(logFilterActive() && list.children.length > 0 && !anyVisible);
  }
  const reset = $("#btnLogFilterReset");
  if (reset) reset.hidden = !logFilterActive();
}

function applyLogFilter() {
  const list = $("#logList");
  if (!list) return;
  Array.from(list.children).forEach(applyLogRowVisibility);
  syncLogFilterChrome();
  if (state.logFollow) stickLogToBottom(list);
}

function bindLogFilters() {
  const dev = $("#logDev");
  const kind = $("#logKind");
  const search = $("#logSearch");
  const chips = $("#logLevelChips");
  const reset = $("#btnLogFilterReset");

  if (dev) {
    dev.onchange = () => {
      state.logFilter.dev = dev.value || "";
      applyLogFilter();
    };
  }
  if (kind) {
    kind.onchange = () => {
      state.logFilter.kind = kind.value;
      applyLogFilter();
    };
  }
  if (search) {
    search.oninput = () => {
      state.logFilter.q = search.value.trim().toLowerCase();
      applyLogFilter();
    };
  }
  if (chips) {
    chips.onclick = (e) => {
      const btn = e.target.closest(".log-chip");
      if (!btn) return;
      state.logFilter.level = btn.dataset.level || "";
      $$(".log-chip", chips).forEach((b) => {
        const on = b === btn;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      applyLogFilter();
    };
  }
  if (reset) {
    reset.onclick = () => {
      state.logFilter = { kind: "", level: "", q: "", dev: "" };
      if (dev) dev.value = "";
      if (kind) kind.value = "";
      if (search) search.value = "";
      if (chips) {
        $$(".log-chip", chips).forEach((b) => {
          const on = !b.dataset.level;
          b.classList.toggle("active", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
      }
      applyLogFilter();
    };
  }
  // 初始化时按当前设备并集决定「设备」下拉是否出现（日志页刚打开时通常
  // 还没有行，等 pollLog 追加完会再同步一次）。
  syncLogDeviceUi();
}

// 上滚 / 回底只改这两个状态值，然后重绘按钮与标签。
function bindLogFollow() {
  const list = $("#logList");
  if (!list) return;
  // passive：滚动是高频事件，不需要 preventDefault。
  list.addEventListener("scroll", () => {
    const following = logIsFollowing(list);
    if (following === null || following === state.logFollow) return;
    state.logFollow = following;
    if (following) state.logPending = 0;
    syncLogFollowUi();
  }, { passive: true });

  const jump = $("#btnLogJump");
  if (jump) {
    jump.onclick = () => {
      state.logFollow = true;
      state.logPending = 0;
      syncLogFollowUi({ stick: true });
    };
  }
}

async function pollLog() {
  try {
    const r = await api(`/api/log?since=${state.logSeq}`);
    state.logSeq = r.seq;
    if (!r.events.length) return;
    const list = $("#logList");

    // 追加前判定：贴底就继续跟随，被上滚过就保持暂停。
    const following = logIsFollowing(list);
    if (following !== null) {
      state.logFollow = following;
      if (following) state.logPending = 0;
    }
    // 暂停态先记住可视首行，裁剪后用它校验补偿。
    const anchor = state.logFollow ? null : captureLogAnchor(list);

    let visibleNew = 0;
    // 设备标注只在并集 ≥2 时插列；空 dev 的行也插一个空 span，行与行才对齐。
    // 这一批里新出现的 dev 可能把并集推到 2，追加完由 syncLogDeviceUi 补标签。
    const showDev = window.LogDevice.showsDeviceTag(logDeviceUnion());
    const devSpan = showDev ? '<span class="log-dev"></span>' : "";
    r.events.forEach((e) => {
      const row = document.createElement("div");
      row.className = "log-row " + (e.level || "info");
      // 过滤用的原始字段，不占视觉空间：kind / level 直接来自后端事件。
      row.dataset.kind = e.kind || "";
      row.dataset.level = e.level || "info";
      // 设备归属：空串表示上位机自身事件或无后缀旧固件，这类行不加标签、
      // 也不参与任何设备过滤。
      row.dataset.dev = e.dev || "";
      // 事件带 code/params（WS-529）：优先本地翻译，语言切换后旧行也跟随；
      // 没有 code 的（设备原始行、旧后端）继续用服务端渲染好的 text。
      const rowText = e.code ? t(e.code, e.params) : e.text;
      row._event = e;
      row.innerHTML = `<span class="log-time">${e.time}</span><span class="log-kind">${e.kind}</span>${devSpan}<span class="log-text">${escapeHtml(rowText)}</span>`;

      // 每行右侧的「复制」按钮：把这一行的完整文本（时间 + 类型 + 内容）
      // 放进剪贴板，方便贴到 issue / 聊天里。有设备归属时带上 [设备名]。
      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "log-copy";
      copyBtn.textContent = t("ui.copy");
      copyBtn.title = t("ui.copy_row_title");
      copyBtn.onclick = () => copyLogLine(window.LogDevice.logCopyText(e, state.devices), copyBtn);
      row.appendChild(copyBtn);

      applyLogRowVisibility(row);
      if (!row.hidden) visibleNew++;
      list.appendChild(row);
      // Physical-button action failures arrive through the log stream;
      // surface them as a toast so the user sees them immediately.
      if (e.kind === "action" && e.level === "error") {
        toast(rowText, "bad");
      }
    });

    // 用最新并集补齐 / 摘掉设备标签与下拉；只动标签节点，不重建行。
    // 必须放在测量 scrollHeight 之前，行高才是最终值。
    syncLogDeviceUi();

    // 裁剪前后的内容高度差 = 被移除行占掉的高度，暂停态要补回滚动位置。
    const heightBeforeTrim = list.scrollHeight;
    while (list.children.length > 300) list.removeChild(list.firstChild);
    const removedHeight = Math.max(0, heightBeforeTrim - list.scrollHeight);

    // 计数按实际新增、且过滤后可见的行数累加（裁剪前统计），不按批次 +1。
    state.logPending = window.LogFollow.pendingAfterAppend(state.logPending, visibleNew, state.logFollow);

    if (state.logFollow) {
      stickLogToBottom(list);
    } else if (removedHeight > 0) {
      list.scrollTop = window.LogFollow.compensateTrim(list.scrollTop, removedHeight);
      realignLogAnchor(list, anchor);
    }
    syncLogFilterChrome();
    syncLogFollowUi();
  } catch (_) {
    /* 轮询失败：不追加、不滚底、不重置暂停状态与计数 */
  }
}

/* ---------------- clipboard ---------------- */

/* 复制文本：优先 Clipboard API（需要安全上下文，127.0.0.1 满足），
   失败时降级到隐藏 textarea + document.execCommand('copy')，兼容
   pywebview / 老浏览器 / 无权限场景。返回是否成功。 */
async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      /* 权限被拒或窗口失焦：走下面的降级路径 */
    }
  }
  let ta = null;
  try {
    ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.left = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    return document.execCommand("copy");
  } catch (_) {
    return false;
  } finally {
    // copy 抛错时也要把临时节点收掉，避免残留在 DOM 里。
    if (ta && ta.parentNode) ta.parentNode.removeChild(ta);
  }
}

async function copyLogLine(text, btn) {
  const ok = await copyText(text);
  if (!ok) {
    toast(t("ui.copy_failed"), "bad");
    return;
  }
  toast(t("ui.copied_log"));
  if (!btn) return;
  const label = btn.textContent;
  btn.textContent = t("ui.copied");
  btn.classList.add("copied");
  setTimeout(() => {
    btn.textContent = label;
    btn.classList.remove("copied");
  }, 1200);
}

/* ---------------- misc ---------------- */

function fillRecentOptions(list) {
  const recents = (state.desktop && state.desktop.recent_paths) || [];
  list.innerHTML = "";
  recents.forEach((p) => {
    const o = document.createElement("option");
    o.value = p;
    list.appendChild(o);
  });
}

function syncLaunchRecents() {
  $$("datalist[id^='launch-recents-']").forEach(fillRecentOptions);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function bindTabs() {
  const tabs = $$(".tab");

  const activate = (tab) => {
    tabs.forEach((t) => {
      const on = t === tab;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
    });
    $$(".panel").forEach((p) => p.classList.remove("active"));
    $("#tab-" + tab.dataset.tab).classList.add("active");
    if (tab.dataset.tab === "device") {
      renderDeviceMeta();
      // 打开设备页采样一次运行状态。
      loadSysinfo(state.selected);
    }
    // 日志页隐藏期间容器量不出高度，切回来时补一次贴底 / 胶囊重绘。
    if (tab.dataset.tab === "log") syncLogFollowUi({ stick: true });
  };

  tabs.forEach((tab, i) => {
    tab.onclick = () => activate(tab);
    tab.onkeydown = (e) => {
      let next = null;
      if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
      else if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
      else if (e.key === "Home") next = tabs[0];
      else if (e.key === "End") next = tabs[tabs.length - 1];
      if (!next) return;
      e.preventDefault();
      activate(next);
      next.focus();
    };
  });
}

/* ---------------- help ---------------- */

/** 浮层焦点陷阱：Tab 到末尾回到第一个，Shift+Tab 到开头回到最后一个。 */
function trapFocus(container, e) {
  const focusables = $$(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    container
  ).filter((el) => el.getClientRects().length > 0);
  if (!focusables.length) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement;
  if (e.shiftKey) {
    if (active === first || !container.contains(active)) {
      e.preventDefault();
      last.focus();
    }
  } else if (active === last || !container.contains(active)) {
    e.preventDefault();
    first.focus();
  }
}

/* 帮助浮层：说明宏语法、键名对照、登录示例与常见错误。纯前端开关，
   不请求后端，关掉后页面状态完全不变。 */
function bindHelp() {
  const modal = $("#helpModal");
  const openBtn = $("#btnHelp");
  const closeBtn = $("#btnHelpClose");
  if (!modal || !openBtn || !closeBtn) return;

  const open = () => {
    modal.hidden = false;
    syncModalOpenClass();
    closeBtn.focus();
  };
  const close = () => {
    modal.hidden = true;
    syncModalOpenClass();
    openBtn.focus();
  };

  openBtn.onclick = open;
  closeBtn.onclick = close;
  // 点击遮罩关闭，点击卡片内部不关闭。
  modal.onclick = (e) => {
    if (e.target === modal) close();
  };
  document.addEventListener("keydown", (e) => {
    if (modal.hidden) return;
    // 焦点陷阱：Tab / Shift+Tab 在浮层内循环，不跑到背景页面。
    if (e.key === "Tab") {
      trapFocus(modal, e);
      return;
    }
    if (e.key !== "Escape") return;
    close();
    // 帮助浮层打开时 Esc 只关它自己：阻止 document 上后续的 Esc 监听
    //（配置结果浮层）在同一事件里继续触发。
    e.stopImmediatePropagation();
  });
}

/* 配置结果浮层：窗口尺寸变化时重新定位（顶部栏在窄窗口会换行变高），
   并按 Esc 关闭（帮助弹层打开时先让弹层处理）。 */
function bindApplyResults() {
  window.addEventListener("resize", positionApplyResults);
  // 页面卸载 / 跳转前显式回收浮层与自动消失定时器。
  window.addEventListener("beforeunload", clearApplyResults);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !$("#applyResults")) return;
    // 兜底：即使帮助浮层的监听没有阻止冒泡，也不连带关掉结果浮层。
    const help = $("#helpModal");
    const ota = $("#otaModal");
    const update = $("#updateModal");
    if ((help && !help.hidden) || (ota && !ota.hidden) || (update && !update.hidden)) return;
    clearApplyResults();
  });
}

/* 三个轮询集中管理，便于窗口不可见时整体暂停。 */
const pollTimers = { status: null, log: null, ota: null };

function startPolling() {
  if (pollTimers.status) return;
  pollTimers.status = setInterval(pollStatus, 1200);
  pollTimers.log = setInterval(pollLog, 800);
  pollTimers.ota = setInterval(pollOta, 500);
}

function stopPolling() {
  Object.keys(pollTimers).forEach((key) => {
    clearInterval(pollTimers[key]);
    pollTimers[key] = null;
  });
}

/** 语言：取服务端持久化的偏好并加载对应文案（WS-529）。 */
async function initLanguage() {
  try {
    const d = await api("/api/desktop");
    state.langPref = d.lang || "system";
    await window.i18n.load(d.lang_effective || d.lang);
    window.i18n.apply(document);
    const sel = $("#langSelect");
    if (sel) sel.value = state.langPref;
  } catch (_) {
    /* 旧后端没有 lang：保持 DOM 里的中文兜底 */
  }
}

/** 日志行记着原始事件，切语言后按 code/params 重新渲染（不重新拉日志）。 */
function retranslateLog() {
  $$("#logList .log-row").forEach((row) => {
    const e = row._event;
    const textEl = row.querySelector(".log-text");
    if (!e || !textEl) return;
    textEl.textContent = e.code ? t(e.code, e.params) : e.text;
  });
}

/** 切语言后重绘动态内容；表单与选中的设备都来自 state，不会丢。 */
function retranslateDynamic() {
  renderDevices();
  renderActionsState();
  renderLastApply();
  renderKeys();
  renderEncoder();
  renderDeviceMeta();
  renderPins();
  renderSysinfo();
  renderDesktop();
  renderReadStatus();
  renderUntrustedBanner();
  retranslateLog();
  syncLogFollowUi();
  renderOta();
  renderUpdateApp();
  renderUpdateFw();
}

async function init() {
  await initLanguage();
  bindTabs();
  bindHelp();
  bindApplyResults();
  bindOtaWizard();
  bindUpdate();
  bindKeyDrawer();
  bindLogFilters();
  bindLogFollow();
  syncLogFollowUi();
  try {
    const k = await api("/api/keycodes");
    state.keycodes = k.keys;
  } catch (_) {
    state.keycodes = [];
  }
  const remembered = localStorage.getItem("binddecklite.device");
  if (remembered) state.selected = remembered;

  $("#btnScan").onclick = async () => {
    const btn = $("#btnScan");
    // 扫描是同步 6 秒的请求，期间禁止重复触发。
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.add("loading");
    btn.setAttribute("aria-busy", "true");
    toast(t("ui.scanning_dots"));
    try {
      const r = await api("/api/scan");
      // 说清楚为什么没扫到，而不是只报一个「0 台设备」。
      toast(r.reason
        ? t("ui.scan_no_devices_reason", { reason: r.reason })
        : t("ui.scan_found", { count: r.found ? r.found.length : 0 }));
    } catch (e) {
      toast(t("ui.scan_failed") + e.message, "bad");
    } finally {
      btn.disabled = false;
      btn.classList.remove("loading");
      btn.removeAttribute("aria-busy");
    }
  };
  $("#langSelect").onchange = async (e) => {
    const pref = e.target.value;
    e.target.disabled = true;
    try {
      const d = await updateDesktop({ lang: pref });
      state.langPref = d.lang || pref;
      await window.i18n.load(d.lang_effective || d.lang);
      window.i18n.apply(document);
      retranslateDynamic();
      toast(t("ui.language_changed"), "ok");
    } catch (err) {
      e.target.value = state.langPref || "system";
      toast(err.message, "bad");
    } finally {
      e.target.disabled = false;
    }
  };
  $("#btnApply").onclick = applyConfig;
  $("#pendingBadge").onclick = focusPendingItem;
  $("#btnClearLog").onclick = () => {
    $("#logList").innerHTML = "";
    // 清空后没有可读的旧行，强制回跟随。
    state.logFollow = true;
    state.logPending = 0;
    // 缓冲清空后设备并集只剩在线设备，掉回 1 台时标签与下拉要一起收起。
    syncLogDeviceUi();
    syncLogFilterChrome();
    syncLogFollowUi({ stick: true });
  };
  $("#actionsToggle").onchange = async (e) => {
    const target = e.target;
    const want = target.checked;
    target.disabled = true;
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify({ actions_enabled: want }) });
      state.actionsEnabled = want;
      renderActionsState();
      toast(want ? t("ui.actions_enabled_toast") : t("ui.actions_disabled_toast"), want ? "ok" : "");
    } catch (err) {
      target.checked = !want;
      toast(err.message, "bad");
    } finally {
      target.disabled = false;
    }
  };
  $("#sleepEnabled").onchange = (e) => {
    state.cfg.sleep = state.cfg.sleep || {};
    state.cfg.sleep.enabled = e.target.checked;
    markDirty();
  };
  $("#sleepMinutes").onchange = (e) => {
    state.cfg.sleep = state.cfg.sleep || {};
    state.cfg.sleep.minutes = Math.max(1, parseInt(e.target.value, 10) || 5);
    markDirty();
  };
  $("#autostartToggle").onchange = async (e) => {
    const target = e.target;
    const want = target.checked;
    target.disabled = true;
    try {
      await updateDesktop({ autostart: want });
      toast(want ? t("ui.autostart_on_toast") : t("ui.autostart_off_toast"), "ok");
    } catch (err) {
      target.checked = !want;
      toast(t("ui.autostart_failed_toast") + err.message, "bad");
    } finally {
      const auto = (state.desktop && state.desktop.autostart) || {};
      target.disabled = !auto.supported;
    }
  };
  $("#adminAutostartToggle").onchange = async (e) => {
    const target = e.target;
    const want = target.checked;
    // 未提权时后端要靠 UAC 弹窗创建/删除计划任务，先说清楚会发生什么。
    // 关闭提权同样要删任务（HIGHEST 任务普通权限删不掉），也会弹 UAC。
    const auto = (state.desktop && state.desktop.autostart) || {};
    if (!auto.is_admin && (want || auto.admin)) {
      const hint = want
        ? t("ui.autostart_uac_create")
        : t("ui.autostart_uac_delete");
      if (!confirm(hint)) {
        target.checked = !want;
        return;
      }
    }
    target.disabled = true;
    try {
      await updateDesktop({ admin_autostart: want });
      toast(want ? t("ui.autostart_admin_on_toast") : t("ui.autostart_admin_off_toast"), "ok");
    } catch (err) {
      target.checked = !want;
      toast(t("ui.autostart_admin_failed") + err.message, "bad");
    } finally {
      const auto = (state.desktop && state.desktop.autostart) || {};
      target.disabled = !auto.admin_supported || !auto.enabled;
    }
  };
  $("#minimizedToggle").onchange = async (e) => {
    const target = e.target;
    const want = target.checked;
    target.disabled = true;
    try {
      await updateDesktop({ start_minimized: want });
      toast(want ? t("ui.minimized_on_toast") : t("ui.minimized_off_toast"));
    } catch (err) {
      target.checked = !want;
      toast(err.message, "bad");
    } finally {
      target.disabled = false;
    }
  };
  $("#closeMinimizeToggle").onchange = async (e) => {
    const target = e.target;
    const minimize = target.checked;
    target.disabled = true;
    try {
      await updateDesktop({ close_button_behavior: minimize ? "minimize" : "exit" });
      toast(minimize ? t("ui.close_minimize_toast") : t("ui.close_exit_toast"));
    } catch (err) {
      target.checked = !minimize;
      toast(err.message, "bad");
    } finally {
      target.disabled = false;
    }
  };
  $("#btnRestartAdmin").onclick = async () => {
    const target = $("#btnRestartAdmin");
    target.disabled = true;
    try {
      const res = await fetch("/api/desktop/restart-admin", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) throw new Error(data.error || t("ui.elevate_failed"));
      toast(t("ui.elevate_started"), "ok");
    } catch (err) {
      toast(t("ui.elevate_failed_detail") + (err.message || err), "bad");
      target.disabled = false;
    }
  };
  $("#btnQuitHost").onclick = async () => {
    if (!window.confirm(t("ui.quit_confirm"))) return;
    const target = $("#btnQuitHost");
    target.disabled = true;
    try {
      const res = await fetch("/api/quit", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || t("ui.quit_failed"));
      toast(t("ui.quit_done"), "ok");
    } catch (err) {
      toast(t("ui.quit_failed_detail") + (err.message || err), "bad");
      target.disabled = false;
    }
  };
  $("#btnShowWindow").onclick = async () => {
    const target = $("#btnShowWindow");
    target.disabled = true;
    try {
      const res = await fetch("/api/show-window", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || t("ui.restore_failed"));
      toast(t("ui.show_window"), "ok");
      await refreshDesktop();
    } catch (err) {
      toast(t("ui.restore_failed_detail") + (err.message || err), "bad");
      target.disabled = false;
    }
  };
  const devAction = async (act, okText, confirmText) => {
    if (!state.selected) return;
    if (otaBusy()) {
      toast(t("ui.cmd_locked_ota"), "bad");
      return;
    }
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      const r = await api(`/api/device/${encodeURIComponent(state.selected)}/${act}`, { method: "POST" });
      toast(r.sent === false ? t("ui.cmd_not_sent_offline") : okText, r.sent === false ? "bad" : "ok");
    } catch (e) {
      toast(e.message, "bad");
    }
  };
  // 布局切换。只改本地配置，点「应用配置」才下发；
  // 下发后重新渲染按键卡片与编码器页，切换立刻可见。
  $$("input[name='layout']").forEach((el) => {
    el.onchange = () => {
      if (!el.checked) return;
      setLayout(Number(el.value) === LAYOUT_12KEY ? LAYOUT_12KEY : LAYOUT_ENCODER);
    };
  });
  $("#btnPing").onclick = () => devAction("ping", t("ui.ping_sent"));
  // 手动刷新运行状态（等待设备回 SYS: 行，不弹 toast）。
  $("#btnSysRefresh").onclick = async () => {
    const btn = $("#btnSysRefresh");
    if (!state.selected) {
      toast(t("ui.no_device_selected_short"), "bad");
      return;
    }
    btn.disabled = true;
    try {
      await loadSysinfo(state.selected);
    } finally {
      btn.disabled = false;
    }
  };
  // 「读取设备配置」等设备回报后再回填表单；dirty 时先确认。
  $("#btnRefresh").onclick = () => readDeviceConfig(state.selected);
  // WS-512：提示条上的「从设备读取」复用同一条回读路径 —— OTA 锁与「有未下发
  // 修改」的确认都在 readDeviceConfig 里，不另起一套。
  $("#btnUntrustedRead").onclick = () => readDeviceConfig(state.selected);
  // 「先忽略」只关掉本次会话的提示条：不写 config、不清 trusted、也不解除
  // 「应用配置」前的覆盖确认（那条判断看的是 trusted 与 sessionRead）。
  $("#btnUntrustedIgnore").onclick = () => {
    if (!state.selected) return;
    state.untrustedIgnored[state.selected] = true;
    renderUntrustedBanner();
  };
  $("#btnPortal").onclick = () => devAction(
    "portal",
    t("ui.portal_toast"),
    t("ui.portal_confirm"),
  );
  $("#btnReboot").onclick = () => devAction(
    "reboot",
    t("ui.reboot_toast"),
    t("ui.reboot_confirm"),
  );
  // clear the persisted host binding so a new central can take over.
  $("#btnClearBond").onclick = async () => {
    if (!state.selected) return;
    if (!window.confirm(t("ui.clear_bond_confirm"))) return;
    const btn = $("#btnClearBond");
    btn.disabled = true;
    try {
      const r = await api(`/api/device/${encodeURIComponent(state.selected)}/clearbond`, { method: "POST" });
      if (r.sent === false) {
        toast(t("ui.cmd_not_sent_offline"), "bad");
        return;
      }
      toast(t("ui.clear_bond_sent"), "ok");
      // The device ACKs on the same CFG channel; give it a moment then refresh.
      setTimeout(() => {
        if (state.selected) api(`/api/device/${encodeURIComponent(state.selected)}/refresh`, { method: "POST" });
      }, 500);
    } catch (e) {
      toast(t("ui.clear_bond_failed") + (e.message || e), "bad");
    } finally {
      btn.disabled = false;
    }
  };
  // OTA 的入口与向导按钮由 bindOtaWizard() 统一绑定。

  const initialTab = new URLSearchParams(location.search).get("tab");
  if (initialTab) {
    const tab = document.querySelector(`.tab[data-tab="${initialTab}"]`);
    if (tab) tab.click();
  }

  await refreshDesktop();
  await pollStatus();
  startPolling();
  // 窗口隐藏（最小化到托盘）时暂停轮询，恢复可见时立刻刷新一次。
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopPolling();
      return;
    }
    pollStatus();
    pollLog();
    pollOta();
    startPolling();
  });
}

/* ---------------- OTA ---------------- */

function selectedDevice() {
  return state.devices.find((d) => d.dev_id === state.selected) || null;
}

function fmtVersion(v) {
  if (!v) return t("ui.unknown");
  const s = String(v);
  return /^v/i.test(s) ? s : "v" + s;
}

function formatBytes(n) {
  const v = Number(n) || 0;
  if (v >= 1024 * 1024) return (v / (1024 * 1024)).toFixed(1) + " MB";
  if (v >= 1024) return Math.round(v / 1024) + " KB";
  return v + " B";
}

/** 从 ota_*.bin 文件名里取版本号，作为镜像描述读取失败时的兜底。 */
function otaVersionFromName(name) {
  const base = String(name || "").split(/[\\/]/).pop();
  const m = /_v([0-9][0-9A-Za-z.\-]*)\.bin$/i.exec(base);
  return m ? m[1] : "";
}

/** 读 ESP-IDF app descriptor 里的 version 字段（0x20 + 16，32 字节）。 */
function parseOtaVersion(buffer) {
  if (!buffer || buffer.byteLength < 0x50) return "";
  const raw = new Uint8Array(buffer, 0x30, 32);
  let out = "";
  for (let i = 0; i < raw.length; i++) {
    if (raw[i] === 0) break;
    if (raw[i] < 0x20 || raw[i] > 0x7e) return "";
    out += String.fromCharCode(raw[i]);
  }
  return out.trim();
}

/** 前端预检：与后端 /api/ota 的 app-only 校验保持一致，后端仍是最终裁决。 */
function localOtaImageError(buffer, size) {
  if (!buffer) return t("ota.unreadable_file");
  if (size < 0x30) return t("ota.too_small");
  const b = new Uint8Array(buffer);
  if (b[0] !== 0xe9) return t("ota.not_esp_image");
  if (!(b[32] === 0x32 && b[33] === 0x54 && b[34] === 0xcd && b[35] === 0xab)) {
    return t("ota.merged_pack");
  }
  return "";
}

async function sha256Hex(buffer) {
  if (!(window.crypto && crypto.subtle)) return "";
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function otaPaneStep(ota) {
  if (!ota) return "pick";
  if (["starting", "uploading"].includes(ota.status)) return "upload";
  if (["done", "error", "cancelled"].includes(ota.status)) return "result";
  return state.otaStep === "confirm" ? "confirm" : "check";
}

function showOtaPane(step) {
  $$("#otaModal .ota-pane").forEach((p) => { p.hidden = p.dataset.pane !== step; });
  const order = ["pick", "check", "confirm", "upload"];
  const activeIdx = step === "result" ? 3 : Math.max(0, order.indexOf(step));
  $$("#otaModal .ota-step-dot").forEach((d) => {
    const i = order.indexOf(d.dataset.step);
    d.classList.toggle("active", i === activeIdx);
    d.classList.toggle("done", i < activeIdx);
  });
}

function renderOta() {
  const modal = $("#otaModal");
  const ota = state.ota;
  if (!ota) {
    // The wizard's first step ("pick") has no OTA session yet.  Closing here
    // would make openOtaWizard() hide the modal it just opened, so the OTA
    // button looked dead on a fresh app.
    if (state.otaStep === "pick" && modal && !modal.hidden) {
      showOtaPane("pick");
      applyOtaLock();
      return;
    }
    state.otaStep = null;
    if (modal && !modal.hidden) closeOtaWizard();
    applyOtaLock();
    return;
  }

  const step = otaPaneStep(ota);
  state.otaStep = step;
  showOtaPane(step);

  const sel = selectedDevice();
  const connected = !!(sel && sel.connected);

  /* 1 / 2. 选择文件与校验 */
  $("#otaCheckName").textContent = ota.filename || "—";
  $("#otaCheckSize").textContent = formatBytes(ota.total);
  const checking = ota.status === "checking";
  $("#otaCheckSha").textContent = ota.sha256
    || (checking ? t("ota.calculating") : t("ota.no_local_hash"));
  const bad = ota.status === "invalid";
  $("#otaCheckState").textContent = bad
    ? t("ota.check_failed") : checking ? t("ota.checking") : t("ota.check_ok");
  $("#otaCheckState").className = "ota-check-state " + (bad ? "bad" : ota.status === "ready" ? "ok" : "");
  $("#otaCheckError").hidden = !bad;
  $("#otaCheckError").textContent = ota.imageError || "";
  $("#btnOtaNext").disabled = ota.status !== "ready";

  /* 3. 确认页：设备名 + 当前版本 → 新版本 + 不可断电提示 */
  const devName = (state.cfg && state.cfg.name) || (sel && sel.name) || state.selected || "—";
  $("#otaConfirmDevice").textContent = `${devName}${t(connected ? "ui.connected_suffix" : "ui.disconnected_suffix")}`;
  const currentVersion = (sel && sel.fw && sel.fw.version)
    || (state.cfg && state.cfg.fw && state.cfg.fw.version) || "";
  $("#otaConfirmVersions").textContent =
    `${fmtVersion(currentVersion)} → ${ota.version ? fmtVersion(ota.version) : t("ota.version_not_in_name")}`;
  $("#otaConfirmFile").textContent = `${ota.filename || "—"} · ${formatBytes(ota.total)}`;
  $("#otaConfirmSha").textContent = ota.sha256 || "—";
  $("#btnOtaStart").disabled = !connected;

  /* 4. 上传 / 结果 */
  const shaLine = ota.sha256 ? ` · SHA256 ${ota.sha256}` : "";
  if (ota.status === "done") {
    $("#otaResultTitle").textContent = t("ota.success");
    $("#otaResultTitle").className = "ota-result-title ok";
    $("#otaResultDetail").textContent = t("ota.result_done_line", {
      file: ota.filename || "",
      size: formatBytes(ota.total),
    });
    $("#otaResultHint").textContent = ota.sha256
      ? t("ota.success_hint_sha", { sha: ota.sha256 })
      : t("ota.success_hint");
    $("#btnOtaRetry").hidden = true;
    $("#btnOtaDone").textContent = t("ota.done");
  } else if (ota.status === "error") {
    $("#otaResultTitle").textContent = t("ota.failed");
    $("#otaResultTitle").className = "ota-result-title bad";
    $("#otaResultDetail").textContent = ota.error || t("ui.unknown_error");
    $("#otaResultHint").textContent = ota.sha256
      ? t("ota.failed_hint_sha", { sha: ota.sha256 })
      : t("ota.failed_hint");
    $("#btnOtaRetry").hidden = !state.otaFile;
    $("#btnOtaDone").textContent = t("ota.close");
  } else if (ota.status === "cancelled") {
    $("#otaResultTitle").textContent = t("ota.cancelled");
    $("#otaResultTitle").className = "ota-result-title";
    $("#otaResultDetail").textContent = t("ota.result_cancelled_line", { file: ota.filename || "" });
    $("#otaResultHint").textContent = t("ota.cancel_sent");
    $("#btnOtaRetry").hidden = !state.otaFile;
    $("#btnOtaDone").textContent = t("ota.close");
  }

  const acked = ota.acked != null ? ota.acked : ota.sent != null ? ota.sent : 0;
  $("#otaStatusText").textContent =
    ota.status === "starting" ? t("ota.phase_starting")
      : ota.status === "uploading" ? t("ota.phase_uploading") : t("ota.phase_preparing");
  $("#otaFileLine").textContent = `${ota.filename || ""} · ${formatBytes(ota.total)}${shaLine}`;
  $("#otaProgressBar").style.width = (ota.percent || 0) + "%";
  $("#otaHint").textContent = ota.status === "starting"
    ? t("ota.entering_mode")
    : t("ota.progress_line", {
      acked, total: ota.total || 0, percent: ota.percent || 0,
    });

  applyOtaLock();
}

/* OTA lock: while firmware is being written the deck's configuration and
   device commands are frozen — a concurrent CFG:/CMD: write would interleave
   with the binary stream and corrupt the image.
   入口按钮不锁：关闭向导后仍可用它重新打开查看进度。 */
function otaBusy() {
  return !!(state.ota && (state.ota.status === "starting" || state.ota.status === "uploading"));
}

function applyOtaLock() {
  const busy = otaBusy();
  ["btnApply", "btnPing", "btnRefresh", "btnPortal", "btnReboot", "btnClearBond",
   "btnSysRefresh"]
    .forEach((id) => {
      const el = $("#" + id);
      if (el) el.disabled = busy;
    });
  // 「以设备为准」按钮是运行时动态生成的，OTA 期间同样要锁住。
  $$(".accept-device-value").forEach((el) => {
    el.disabled = busy;
  });
  // WS-512：提示条的「从设备读取」自己判断连接/OTA，这里只让它重算一次状态。
  renderUntrustedBanner();
}

async function pollOta() {
  if (!state.selected || !otaBusy()) return;
  try {
    const r = await api(`/api/ota/${encodeURIComponent(state.selected)}/progress`);
    if (!r.ok) {
      // The server no longer has the task (app restarted or device gone).
      state.ota = Object.assign({}, state.ota, {
        status: "error",
        error: t("ota.link_lost"),
      });
      state.otaStep = "result";
      toast(t("ota.link_lost"), "bad");
      renderOta();
      return;
    }
    state.ota = Object.assign({}, state.ota, {
      status: r.status,
      sent: r.sent,
      acked: r.acked != null ? r.acked : r.sent,
      total: r.total,
      percent: r.percent,
      error: r.error,
      filename: r.filename || state.ota.filename,
    });
    state.otaStep = ["done", "error", "cancelled"].includes(r.status) ? "result" : "upload";
    if (r.status === "done") {
      toast(t("ota.success_reboot"), "ok");
    } else if (r.status === "error") {
      toast(r.error || t("ota.failed"), "bad");
    }
    renderOta();
  } catch (_) {
    /* ignore network blips */
  }
}

/* ---------- OTA wizard: 选择 → 校验 → 确认 → 上传 / 结果 ---------- */

async function handleOtaFile(file) {
  if (!file) return;
  const otaObj = {
    status: "checking",
    sent: 0,
    acked: 0,
    total: file.size,
    percent: 0,
    error: "",
    imageError: "",
    filename: file.name,
    sha256: "",
    version: otaVersionFromName(file.name),
  };
  state.ota = otaObj;
  state.otaFile = file;
  state.otaStep = "check";
  renderOta();

  let buffer = null;
  try {
    buffer = await file.arrayBuffer();
  } catch (_) {
    /* 读取失败按校验失败处理 */
  }
  // 用户在这期间换了文件：丢弃这次结果。
  if (state.ota !== otaObj) return;

  let error = "";
  if (!/^ota_[^/\\]+\.bin$/i.test(file.name)) {
    error = t("ota.filename_rule");
  } else {
    error = localOtaImageError(buffer, file.size);
  }
  let sha = "";
  if (buffer) {
    try {
      sha = await sha256Hex(buffer);
    } catch (_) {
      /* 环境不支持时由后端与设备校验 */
    }
  }
  if (state.ota !== otaObj) return;

  otaObj.sha256 = sha;
  otaObj.imageError = error;
  otaObj.version = parseOtaVersion(buffer) || otaObj.version;
  otaObj.status = error ? "invalid" : "ready";
  renderOta();
}

function syncModalOpenClass() {
  const help = $("#helpModal");
  const ota = $("#otaModal");
  const update = $("#updateModal");
  document.body.classList.toggle("modal-open",
    !!((help && !help.hidden) || (ota && !ota.hidden) || (update && !update.hidden)));
}

function closeOtaWizard() {
  const modal = $("#otaModal");
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  syncModalOpenClass();
}

function openOtaWizard() {
  const sel = selectedDevice();
  if (!sel || !sel.connected) {
    toast(t("ota.offline"), "bad");
    return;
  }
  if (otaBusy()) {
    state.otaStep = "upload";
  } else if (!state.ota) {
    state.otaStep = "pick";
  } else if (["done", "error", "cancelled"].includes(state.ota.status)) {
    state.otaStep = "result";
  } else if (state.ota.status === "ready" && state.otaStep === "confirm") {
    state.otaStep = "confirm";
  } else {
    state.otaStep = "check";
  }
  const modal = $("#otaModal");
  modal.hidden = false;
  syncModalOpenClass();
  renderOta();
  $("#btnOtaClose").focus();
}

function resetOta() {
  state.ota = null;
  state.otaFile = null;
  state.otaStep = null;
  renderOta();
  closeOtaWizard();
}

function retryOta() {
  if (!state.otaFile) {
    state.ota = null;
    state.otaStep = "pick";
    renderOta();
    return;
  }
  state.ota = Object.assign({}, state.ota, {
    status: "ready",
    error: "",
    sent: 0,
    acked: 0,
    percent: 0,
  });
  state.otaStep = "confirm";
  renderOta();
}

function bindOtaWizard() {
  const modal = $("#otaModal");
  const closeBtn = $("#btnOtaClose");
  if (!modal || !closeBtn) return;

  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".bin";
  input.hidden = true;
  input.onchange = () => {
    const file = input.files && input.files[0];
    input.value = "";
    if (file) handleOtaFile(file);
  };
  document.body.appendChild(input);

  const pick = () => {
    input.value = "";
    input.click();
  };

  $("#btnOta").onclick = openOtaWizard;
  closeBtn.onclick = () => closeOtaWizard();
  $("#btnOtaPick").onclick = pick;
  $("#btnOtaRepick").onclick = pick;
  $("#btnOtaNext").onclick = () => {
    if (!state.ota || state.ota.status !== "ready") return;
    state.otaStep = "confirm";
    renderOta();
  };
  $("#btnOtaBack").onclick = () => {
    state.otaStep = "check";
    renderOta();
  };
  $("#btnOtaStart").onclick = startOta;
  $("#btnOtaCancel").onclick = cancelOta;
  $("#btnOtaRetry").onclick = retryOta;
  $("#btnOtaDone").onclick = resetOta;

  modal.onclick = (e) => {
    if (e.target === modal) closeOtaWizard();
  };
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || modal.hidden) return;
    closeOtaWizard();
    e.stopImmediatePropagation();
  });
}

async function startOta() {
  const sel = selectedDevice();
  if (!sel || !sel.connected) {
    toast(t("ota.offline"), "bad");
    return;
  }
  if (!state.ota || state.ota.status !== "ready" || !state.otaFile) {
    toast(t("ota.verify_first"), "bad");
    return;
  }
  const btn = $("#btnOtaStart");
  btn.disabled = true;
  btn.classList.add("loading");
  btn.setAttribute("aria-busy", "true");
  const form = new FormData();
  form.append("file", state.otaFile);
  try {
    const res = await fetch(`/api/ota/${encodeURIComponent(state.selected)}`, {
      method: "POST",
      body: form,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      const message = data.error || t("ota.start_failed");
      toast(message, "bad");
      // 失败留在结果页：保留已选文件，方便直接重试。
      state.ota = Object.assign({}, state.ota, { status: "error", error: message });
      state.otaStep = "result";
      renderOta();
      return;
    }
    state.ota = Object.assign({}, state.ota, {
      status: "starting",
      sent: 0,
      acked: 0,
      total: data.total,
      percent: 0,
      error: "",
      filename: data.filename || state.ota.filename,
      sha256: data.sha256 || state.ota.sha256 || "",
    });
    state.otaStep = "upload";
    renderOta();
  } catch (e) {
    const message = t("ota.start_failed_detail") + (e.message || e);
    toast(message, "bad");
    state.ota = Object.assign({}, state.ota, { status: "error", error: message });
    state.otaStep = "result";
    renderOta();
  } finally {
    btn.disabled = false;
    btn.classList.remove("loading");
    btn.removeAttribute("aria-busy");
  }
}

async function cancelOta() {
  if (!state.ota || ["done", "error", "cancelled"].includes(state.ota.status)) {
    resetOta();
    return;
  }
  // In-progress cancel: ask the server to stop the push and tell the device
  // to abort (CMD:OTA_CANCEL), so the device leaves OTA mode immediately
  // instead of waiting for its 30 s inter-chunk timeout.
  if (state.selected && otaBusy()) {
    try {
      const r = await api(`/api/ota/${encodeURIComponent(state.selected)}/cancel`, { method: "POST" });
      if (r.sent === false) {
        toast(t("ota.cancel_undelivered"), "bad");
      } else {
        toast(t("ota.cancel_kept_fw"), "ok");
      }
    } catch (e) {
      toast(t("ota.cancel_failed") + (e.message || e), "bad");
    }
    state.ota = Object.assign({}, state.ota, { status: "cancelled" });
    state.otaStep = "result";
    renderOta();
    return;
  }
  resetOta();
}

/* ================= 在线更新（WS-523） =================
 * 上位机自更新（换 exe）+ 固件 OTA 在线检测/推送。
 * 失败提示一律带中文原因；错误码与 Release 链接在弹窗里可复制。
 */

const updateState = {
  app: null,        // /api/app/update/check 的结果
  appTask: null,    // 上位机更新任务 id
  fw: null,         // /api/firmware/update/check 的结果
  fwTask: null,     // 固件更新设备 id
  polling: null,    // 进度轮询定时器
};

/** 失败提示：中文原因 + 可复制错误码 + Release 链接。 */
function updateErrorText(payload, fallback) {
  const reason = (payload && (payload.error || payload.reason)) || fallback || t("ui.unknown_error");
  const code = (payload && (payload.error_code || payload.reason_code)) || "";
  const url = (payload && payload.release_url) || "";
  const parts = [reason];
  if (code) parts.push(t("ui.update_error_code", { code }));
  if (url) parts.push(t("ui.update_release_page", { url }));
  return parts.join(t("ui.update_error_join"));
}

function setUpdateError(id, payload, fallback) {
  const box = $("#" + id);
  if (!box) return;
  if (!payload) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.textContent = updateErrorText(payload, fallback);
}

function setUpdateNotes(boxId, bodyId, notes) {
  const box = $("#" + boxId);
  const body = $("#" + bodyId);
  if (!box || !body) return;
  const text = String(notes || "").trim();
  if (!text) {
    box.hidden = true;
    body.textContent = "";
    return;
  }
  body.textContent = text;
  box.hidden = false;
}

function renderUpdateApp() {
  const r = updateState.app;
  const current = $("#updAppCurrent");
  const latest = $("#updAppLatest");
  const size = $("#updAppSize");
  const sha = $("#updAppSha");
  const st = $("#updAppState");
  const applyBtn = $("#btnUpdateApply");
  const skipBtn = $("#btnUpdateSkip");
  if (!current || !st) return;

  if (!r) {
    current.textContent = "—";
    latest.textContent = "—";
    size.textContent = "—";
    sha.textContent = "—";
    st.textContent = t("update.checking");
    st.classList.remove("ok");
    if (applyBtn) applyBtn.hidden = true;
    if (skipBtn) skipBtn.hidden = true;
    return;
  }

  current.textContent = fmtVersion(r.current);
  latest.textContent = r.latest ? fmtVersion(r.latest) : "—";
  size.textContent = r.asset && r.asset.size ? formatBytes(r.asset.size) : "—";
  sha.textContent = r.asset && r.asset.sha256 ? r.asset.sha256.slice(0, 16) + "…" : "—";
  setUpdateNotes("updAppNotesBox", "updAppNotes", r.notes);
  setUpdateError("updAppError", r.ok === false ? r : null);

  if (r.has_update) {
    st.textContent = t("update.new_version", { latest: fmtVersion(r.latest), current: fmtVersion(r.current) });
    st.classList.remove("ok");
  } else if (r.skipped) {
    st.textContent = t("update.skipped_version", { latest: fmtVersion(r.latest) });
    st.classList.remove("ok");
  } else if (r.ok === false) {
    // 说清楚为什么不能升 —— 只写「检查失败」用户会以为是网络问题。
    st.textContent = r.error || t("update.check_failed");
    st.classList.remove("ok");
  } else {
    st.textContent = t("update.up_to_date", { current: fmtVersion(r.current) });
    st.classList.add("ok");
  }
  if (applyBtn) applyBtn.hidden = !r.has_update;
  if (skipBtn) skipBtn.hidden = !r.has_update;
}

function renderUpdateFw() {
  const r = updateState.fw;
  const dev = $("#updFwDevice");
  const variant = $("#updFwVariant");
  const current = $("#updFwCurrent");
  const latest = $("#updFwLatest");
  const size = $("#updFwSize");
  const sha = $("#updFwSha");
  const st = $("#updFwState");
  const checkBtn = $("#btnFwCheck");
  const applyBtn = $("#btnFwApply");
  if (!st) return;

  const sel = selectedDevice();
  if (dev) dev.textContent = sel ? sel.name || sel.dev_id : t("update.no_device");
  if (!r) {
    if (variant) variant.textContent = "—";
    if (current) current.textContent = "—";
    if (latest) latest.textContent = "—";
    if (size) size.textContent = "—";
    if (sha) sha.textContent = "—";
    st.textContent = sel ? t("update.check_fw_hint") : t("update.pick_device_hint");
    st.classList.remove("ok");
    setUpdateError("updFwError", null);
    setUpdateNotes("updFwNotesBox", "updFwNotes", "");
    if (checkBtn) checkBtn.hidden = !sel;
    if (applyBtn) applyBtn.hidden = true;
    return;
  }

  if (variant) variant.textContent = r.device_variant || t("ui.unknown");
  if (current) current.textContent = fmtVersion(r.current);
  if (latest) latest.textContent = r.latest ? fmtVersion(r.latest) : "—";
  if (size) size.textContent = r.asset && r.asset.size ? formatBytes(r.asset.size) : "—";
  if (sha) sha.textContent = r.asset && r.asset.sha256 ? r.asset.sha256.slice(0, 16) + "…" : "—";
  setUpdateNotes("updFwNotesBox", "updFwNotes", r.notes);
  setUpdateError("updFwError", r.reason ? r : null);

  if (r.has_update) {
    st.textContent = t("update.new_firmware", { latest: fmtVersion(r.latest), current: fmtVersion(r.current) });
    st.classList.remove("ok");
  } else if (r.reason) {
    st.textContent = r.reason;
    st.classList.remove("ok");
  } else {
    st.textContent = t("update.fw_up_to_date", { current: fmtVersion(r.current) });
    st.classList.add("ok");
  }
  if (checkBtn) checkBtn.hidden = false;
  if (applyBtn) applyBtn.hidden = !r.has_update;
}

/** 顶栏红点：有新版本时提示。 */
function refreshUpdateDot() {
  const dot = $("#updateDot");
  if (!dot) return;
  const hasApp = !!(updateState.app && updateState.app.has_update);
  const hasFw = !!(updateState.fw && updateState.fw.has_update);
  dot.hidden = !(hasApp || hasFw);
}

async function checkAppUpdate(quiet) {
  try {
    updateState.app = await api("/api/app/update/check");
  } catch (e) {
    updateState.app = {
      ok: false, has_update: false, current: "", latest: "",
      error: t("update.check_failed_detail") + (e.message || e), error_code: "E_REQUEST",
      release_url: "https://github.com/bbllmaster/BindDeckLite/releases/latest",
    };
  }
  renderUpdateApp();
  refreshUpdateDot();
  if (!quiet && updateState.app.has_update) {
    toast(t("update.found_new_version", { latest: fmtVersion(updateState.app.latest) }), "ok");
  }
}

async function checkFwUpdate(quiet) {
  const sel = selectedDevice();
  if (!sel) {
    updateState.fw = null;
    renderUpdateFw();
    refreshUpdateDot();
    if (!quiet) toast(t("update.pick_device_first"), "bad");
    return;
  }
  try {
    updateState.fw = await api(
      `/api/firmware/update/check?dev_id=${encodeURIComponent(sel.dev_id)}`);
  } catch (e) {
    updateState.fw = {
      has_update: false, device_variant: "", current: "", latest: "",
      reason: t("update.check_failed_detail") + (e.message || e), reason_code: "E_REQUEST",
      release_url: "https://github.com/bbllmaster/BindDeckLite/releases/latest",
    };
  }
  renderUpdateFw();
  refreshUpdateDot();
  if (!quiet && updateState.fw.has_update) {
    toast(t("update.found_new_firmware", { latest: fmtVersion(updateState.fw.latest) }), "ok");
  }
}

function openUpdateModal() {
  const modal = $("#updateModal");
  if (!modal) return;
  modal.hidden = false;
  syncModalOpenClass();
  renderUpdateApp();
  renderUpdateFw();
  $("#btnUpdateClose").focus();
  checkAppUpdate(true);
  checkFwUpdate(true);
}

function closeUpdateModal() {
  const modal = $("#updateModal");
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  syncModalOpenClass();
}

/* 上位机更新的确认文案：更新会退出上位机进程，连着的设备会被断开。

   必须是函数，不能是 `const ... = t(...)`：脚本解析时 i18n.load() 还没跑完，
   浏览器分支下 messages 还是空表，常量会把 key 名本身冻在里面（WS-541）。
   测试（test_app_update_confirm.mjs）在「无 require、后加载语言表」的上下文
   里断言它不是 key。 */
function appUpdateConfirmText() {
  return t("update.confirm_quit");
}

/** 「立即更新」前要不要先问一句。

   有设备连着时才问：更新要退出上位机进程，BLE 会被断掉（上位机动作全停）。
   正在 OTA 时不问 —— 那时本来就被服务端的 E_OTA_BUSY 硬拦（见 app.py
   _ota_busy_reason），先弹确认再报「设备正在升级」是多余的打扰。 */
function appUpdateNeedsConfirm() {
  if (otaBusy()) return false;
  return state.devices.some((d) => d.connected);
}

async function applyAppUpdate() {
  // 确认放在最前面：用户取消时不该看到「更新期间请勿关闭程序」，按钮也不该
  // 被置灰 —— 什么都没发生。
  if (appUpdateNeedsConfirm() && !window.confirm(appUpdateConfirmText())) return;
  const btn = $("#btnUpdateApply");
  if (btn) {
    btn.disabled = true;
    btn.classList.add("loading");
  }
  const box = $("#updAppProgressBox");
  const bar = $("#updAppProgressBar");
  const hint = $("#updAppHint");
  if (box) box.hidden = false;
  if (bar) bar.style.width = "0%";
  if (hint) {
    hint.hidden = false;
    hint.textContent = t("update.do_not_close");
  }
  try {
    const r = await api("/api/app/update/apply", { method: "POST" });
    updateState.appTask = r.task;
    pollAppUpdate(r.task);
  } catch (e) {
    // 失败原因 + 错误码 + Release 链接都要给用户（验收 10）。
    setUpdateError("updAppError", e.payload || null);
    if (hint) {
      hint.hidden = false;
      hint.textContent = t("update.failed_detail") + (e.message || e);
    }
    toast(t("update.failed_detail") + (e.message || e), "bad");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("loading");
    }
  }
}

function pollAppUpdate(task) {
  if (updateState.polling) clearInterval(updateState.polling);
  const bar = $("#updAppProgressBar");
  const hint = $("#updAppHint");
  const phaseText = {
    starting: t("update.phase_preparing"),
    downloading: t("update.phase_downloading"),
    verifying: t("update.phase_verifying"),
    installing: t("update.phase_replacing"),
    restarting: t("update.phase_restarting"),
  };
  updateState.polling = setInterval(async () => {
    try {
      const p = await api(`/api/app/update/progress?task=${encodeURIComponent(task)}`);
      if (bar) bar.style.width = (p.percent || 0) + "%";
      if (hint) {
        hint.textContent = (phaseText[p.phase] || t("update.in_progress")) + t("update.in_progress_note");
      }
      if (p.phase === "error") {
        clearInterval(updateState.polling);
        updateState.polling = null;
        if (hint) hint.textContent = t("update.failed_detail") + (p.error || t("ui.unknown_error"));
        toast(t("update.failed_detail") + (p.error || t("ui.unknown_error")), "bad");
      } else if (p.phase === "restarting") {
        // 程序即将退出重启：轮询会随进程退出而中断，这里停掉。
        clearInterval(updateState.polling);
        updateState.polling = null;
      }
    } catch (_) {
      // 进程正在退出重启，请求失败是预期的。
      clearInterval(updateState.polling);
      updateState.polling = null;
    }
  }, 500);
}

async function skipAppUpdate() {
  const r = updateState.app;
  if (!r || !r.latest) return;
  try {
    await api("/api/app/update/skip", {
      method: "POST",
      body: JSON.stringify({ version: r.latest }),
    });
    toast(t("update.skipped_done", { latest: fmtVersion(r.latest) }), "ok");
    await checkAppUpdate(true);
  } catch (e) {
    toast(t("update.skip_failed") + (e.message || e), "bad");
  }
}

async function applyFwUpdate() {
  const sel = selectedDevice();
  if (!sel) {
    toast(t("update.pick_device_first"), "bad");
    return;
  }
  const btn = $("#btnFwApply");
  if (btn) {
    btn.disabled = true;
    btn.classList.add("loading");
  }
  try {
    const r = await api("/api/firmware/update/apply", {
      method: "POST",
      body: JSON.stringify({ dev_id: sel.dev_id }),
    });
    updateState.fwTask = sel.dev_id;
    pollFwUpdate(sel.dev_id);
    toast(t("update.fw_pushing") + fmtVersion(r.version), "ok");
  } catch (e) {
    // 失败原因 + 错误码 + Release 链接都要给用户（验收 10）。
    setUpdateError("updFwError", e.payload || null);
    toast(t("update.fw_failed") + (e.message || e), "bad");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("loading");
    }
  }
}

/* 固件在线更新的五段进度：后端 phase → 展示阶段名。
   后端在下载/校验阶段发 starting/downloading/verifying/pushing，推送阶段
   借用 OTA 窗口的 push/restart/confirm —— 两套名字对不上时 order.indexOf()
   会返回 -1，五个点全灭（WS-530 评审）。 */
const FW_STAGE_BY_PHASE = {
  starting: "download",
  downloading: "download",
  download: "download",
  verifying: "verify",
  verify: "verify",
  pushing: "push",
  push: "push",
  restart: "restart",
  confirm: "confirm",
};

function updateFwStep(phase) {
  const steps = $("#updFwSteps");
  if (!steps) return;
  const order = ["download", "verify", "push", "restart", "confirm"];
  const finished = phase === "done";
  const stage = FW_STAGE_BY_PHASE[phase];
  // 未知阶段（如 error）：保持上一次的高亮，不要把进度清空。
  if (!finished && !stage) return;
  const active = finished ? order.length : order.indexOf(stage);
  steps.querySelectorAll(".ota-step-dot").forEach((dot) => {
    const idx = order.indexOf(dot.dataset.step);
    dot.classList.toggle("active", !finished && idx === active);
    dot.classList.toggle("done", finished || (active >= 0 && idx < active));
  });
}

function pollFwUpdate(devId) {
  if (updateState.polling) clearInterval(updateState.polling);
  updateState.polling = setInterval(async () => {
    try {
      const p = await api(
        `/api/firmware/update/progress?dev_id=${encodeURIComponent(devId)}`);
      updateFwStep(p.phase || "download");
      if (p.phase === "error") {
        clearInterval(updateState.polling);
        updateState.polling = null;
        setUpdateError("updFwError", {
          error: p.error || t("ui.unknown_error"), error_code: p.error_code || "",
        });
        toast(t("update.fw_failed") + (p.error || t("ui.unknown_error")), "bad");
      } else if (p.phase === "done") {
        clearInterval(updateState.polling);
        updateState.polling = null;
        toast(t("update.fw_done"), "ok");
        await checkFwUpdate(true);
      }
    } catch (e) {
      clearInterval(updateState.polling);
      updateState.polling = null;
      setUpdateError("updFwError", { error: e.message || String(e) });
    }
  }, 500);
}

function bindUpdate() {
  const modal = $("#updateModal");
  const openBtn = $("#btnUpdate");
  const closeBtn = $("#btnUpdateClose");
  if (!modal || !openBtn || !closeBtn) return;

  openBtn.onclick = openUpdateModal;
  closeBtn.onclick = closeUpdateModal;
  modal.onclick = (e) => {
    if (e.target === modal) closeUpdateModal();
  };
  document.addEventListener("keydown", (e) => {
    if (modal.hidden) return;
    if (e.key === "Tab") {
      trapFocus(modal, e);
      return;
    }
    if (e.key !== "Escape") return;
    closeUpdateModal();
    e.stopImmediatePropagation();
  });

  const applyBtn = $("#btnUpdateApply");
  if (applyBtn) applyBtn.onclick = applyAppUpdate;
  const skipBtn = $("#btnUpdateSkip");
  if (skipBtn) skipBtn.onclick = skipAppUpdate;
  const fwCheck = $("#btnFwCheck");
  if (fwCheck) fwCheck.onclick = () => checkFwUpdate(false);
  const fwApply = $("#btnFwApply");
  if (fwApply) fwApply.onclick = applyFwUpdate;

  // 设备页的「固件更新」入口：打开同一个弹窗并直接查固件。
  const fwBtn = $("#btnFwUpdate");
  if (fwBtn) {
    fwBtn.onclick = () => {
      openUpdateModal();
      checkFwUpdate(false);
    };
  }
}

init();
