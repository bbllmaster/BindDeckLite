/* BindDeckLite host UI — vanilla JS, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const ENCODER_MODES = [
  { id: 0, name: "音量", desc: "媒体音量加 / 减" },
  { id: 1, name: "缩放", desc: "Ctrl + / Ctrl -" },
  { id: 2, name: "标签页", desc: "Ctrl + Tab / Ctrl + Shift + Tab" },
  { id: 3, name: "撤销 / 重做", desc: "Ctrl + Z / Ctrl + Y" },
  { id: 4, name: "上位机动作", desc: "旋转方向由下方「旋转动作」决定" },
];

const ACTION_TYPES = [
  { id: "none", name: "仅 HID 按键" },
  { id: "launch", name: "启动程序 / 文件" },
  { id: "url", name: "打开网址" },
  { id: "command", name: "执行命令" },
  { id: "media", name: "媒体键" },
  { id: "macro", name: "宏" },
];

const MEDIA_KEYS = ["volume up", "volume down", "volume mute", "play/pause", "next track", "previous track"];

// 测试按钮空闲态的固定提示。执行中临时换成「仍在执行」，结束后恢复成这一句，
// 而不是把 title 清空（WS-360 评审 nit）。
const TEST_BTN_IDLE_TITLE = "立即执行这条上位机动作（不会发送 HID 键）";

// 宏输入框 placeholder 里展示的键名列表（映射规则见 host/app.py 的
// MACRO_KEY_NAMES）。这里只是提示，真正的归一化在主机端完成。
const MACRO_KEY_HINT =
  "Tab Enter Esc Space Backspace Insert Delete Home End PageUp PageDown " +
  "Left Right Up Down LCtrl RCtrl LShift RShift LAlt RAlt LWin RWin " +
  "F1-F12、单个字母/数字；组合键用 + 连接（ctrl+alt+t）";

// WS-476：按键引脚校验。判定规则只有一份，在 host/app.py 的 validate_pins()
// 里（与固件 isValidButtonPin / isValidPinSet 对齐）。这里只负责把当前 9 个
// 值交给只读的 /api/defaults 取回逐行错误，不在 JS 里再抄一遍规则。
const PIN_COUNT = 9;
// 停止输入 300 ms 后才校验：13 改成 1 会先经过空串，不能每敲一个字符就飘红。
const PIN_VALIDATE_DEBOUNCE_MS = 300;

const state = {
  devices: [],
  selected: null,
  selectedConnected: false,
  cfg: null,
  keycodes: [],
  logSeq: 0,
  logFollow: true,     // 日志是否跟随最新行（WS-470）；上滚即暂停
  logPending: 0,       // 暂停期间新到达、尚未查看的行数
  dirty: false,
  saveTimer: null,
  preview: false,
  desktop: null,
  ota: null,           // {status, sent, total, percent, error, filename}
  otaFile: null,       // File object chosen by the user
  running: {},         // dev_id -> {"<idx>": 已运行秒数}，来源 /api/status
  stopping: {},        // dev_id -> {"<idx>": true}，已发出停止请求、等状态确认（WS-495）
  scan: null,          // 最近一次扫描的结论，来源 /api/status（WS-395）
  encRunning: {},     // dev_id -> {"CW"|"CCW": 已运行秒数}，来源 /api/status（WS-389）
  readStatus: {},     // dev_id -> {at, fields, ok}，来源 /api/status 的 read 字段（WS-409）
  sysinfo: {},        // dev_id -> 运行状态采样，来源 /api/device/<id>/sysinfo 与 /api/status（WS-445）
  sysinfoBusy: {},    // dev_id -> true 表示已有一条采样请求在等回包（防重复请求）
  pinErrors: [],      // 当前引脚表单的逐行错误，来源 /api/defaults 的 pinErrors（WS-476）
  pinRules: null,     // 当前 chip 的引脚规则/提示，来源 /api/defaults 的 pinRules（WS-476）
  pinValidateSeq: 0,  // 每次校验自增，丢弃过期响应（防抖 / 快速切换设备时乱序）
  pinValidateTimer: null,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    // WS-476: 后端 400 会带 {ok:false,error:"按键 3 不能为空"}，把它显示出来，
    // 别再让用户只看到 "400 BAD REQUEST"。
    let detail = "";
    try {
      const body = await res.json();
      if (body && body.error) detail = body.error;
    } catch (_) {
      /* 不是 JSON：退回状态码文案 */
    }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show " + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.className = "toast"), 2400);
}

/* ---------------- devices ---------------- */

async function pollStatus() {
  try {
    const s = await api("/api/status");
    state.devices = s.devices || [];
    state.actionsEnabled = !!s.actions_enabled;
    state.running = s.running || {};
    // WS-395: 最近一次扫描的结论（为什么没找到设备）。
    state.scan = s.scan || null;
    // WS-389: additive field — old front-ends simply ignore it.
    state.encRunning = s.encoder_running || {};
    // WS-409: the server remembers the last read-back per device, so the
    // device page keeps showing "上次读取" across a page reload.
    state.devices.forEach((d) => {
      if (d.read) state.readStatus[d.dev_id] = d.read;
    });
    // WS-445: 运行状态采样只存在服务端内存里，页面刷新后靠这个字段恢复；
    // 断开时卡片显示的就是这份「上次采样」。
    state.sysinfo = Object.assign({}, state.sysinfo, s.sysinfo || {});
    renderReadStatus();
    $("#actionsToggle").checked = state.actionsEnabled;

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
          } catch (_) {
            /* ignore */
          }
        }
        renderDevices();
      }
    } else {
      renderDevices();
    }

    const sel = state.devices.find((d) => d.dev_id === state.selected);
    // 设备断开后，上一次的配置结果已经无法再操作，直接收起浮层。
    const nowConnected = !!(sel && sel.connected);
    const wasConnected = state.selectedConnected;
    if (wasConnected && !nowConnected) clearApplyResults();
    // WS-445: 重新连上时立刻补一次运行状态（断开期间卡片保留旧值）。
    if (state.selected && !wasConnected && nowConnected) {
      loadSysinfo(state.selected);
    }
    state.selectedConnected = nowConnected;
    // 连接状态变化时重绘卡片：断开后必须出现「已断开，非实时」。
    if (state.selected && wasConnected !== nowConnected) renderSysinfo();
    const connected = state.devices.filter((d) => d.connected).length;
    if (sel) {
      $("#statusDot").className = "dot " + (sel.connected ? "ok" : "warn");
      $("#statusText").textContent = sel.connected ? "已连接" : "未连接";
    } else {
      $("#statusDot").className = "dot bad";
      $("#statusText").textContent = "未发现设备";
    }
    // WS-395: 副标题按优先级说明「为什么连不上」（蓝牙不可用 > 单设备失败
    // 原因 > 扫描结论），而不是一律显示「请给设备上电」。
    let sub;
    if (s.ble_ok === false) {
      sub = `蓝牙依赖不可用：${s.ble_error || "未知原因"}`;
    } else if (sel && !sel.connected && sel.last_error) {
      sub = `未连接 · ${sel.last_error.text}`;
    } else if (!sel) {
      sub = scanDiagSub(state.scan)
        || (state.preview ? "显示默认配置（预览）" : "请给设备上电");
    } else {
      const fw = sel.fw && sel.fw.version
        ? `固件 ${sel.fw.version} · ${sel.fw.chip || ""}` : "等待设备信息";
      sub = `${sel.name} · ${fw}`;
    }
    $("#statusSub").textContent = sub;
    highlightKeys(s.last_press || {});
    applyRunningState();
    applyEncoderRunningState();
  } catch (e) {
    $("#statusDot").className = "dot bad";
    $("#statusText").textContent = "上位机未就绪";
    $("#statusSub").textContent = String(e.message || e);
  }
}

/**
 * 扫描诊断文案（WS-395）。
 *
 * - 没有 scan 字段 = 旧后端：返回 null，界面保持原样。
 * - at 为 null = 还没扫完：显示「扫描中…」，不要显示「0 个 BLE 设备」。
 * - 有 reason = 说明这次扫描为什么没找到设备。
 */
function scanDiagSub(scan) {
  if (!scan) return null;
  if (scan.at === null || scan.at === undefined) return "扫描中…";
  if (scan.reason) return `未发现设备：${scan.reason}`;
  return null;
}

/** 扫描时间戳 → HH:MM:SS（本地时区）。 */
function fmtClock(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function renderDevices() {
  const ul = $("#deviceList");
  ul.innerHTML = "";
  if (!state.devices.length) {
    const li = document.createElement("li");
    li.className = "device-empty";
    // WS-395: 没找到设备时给出下一步该做什么，而不是只丢一句「未发现」。
    const scan = state.scan;
    const parts = [
      "<b>未发现 BindDeckLite 设备</b>",
      "<span>① 系统蓝牙已开启</span>",
      "<span>② 设备已上电且在 1 米内</span>",
      "<span>③ 刚进过配网模式的话，长按编码器约 1 秒退出</span>",
    ];
    if (scan && scan.at !== null && scan.at !== undefined) {
      const tail = scan.reason ? ` · ${escapeHtml(scan.reason)}` : "";
      parts.push(`<span class="dev-scan">最后扫描 ${fmtClock(scan.at)}${tail}</span>`);
    }
    li.innerHTML = parts.join("");
    ul.appendChild(li);
    return;
  }
  state.devices.forEach((d) => {
    const li = document.createElement("li");
    li.className = "device" + (d.dev_id === state.selected ? " active" : "");
    // WS-395: 未连接且已知失败原因时，用原因摘要代替地址，点变琥珀色。
    const reason = !d.connected && d.last_error ? d.last_error.text : "";
    const dot = d.connected ? "ok" : (reason ? "warn" : "");
    const sub = reason
      ? `<small class="reason" title="${escapeHtml(d.last_error.text)}">${escapeHtml(reason)}</small>`
      : `<small>${escapeHtml(d.address || "")}</small>`;
    li.innerHTML = `
      <span class="dot ${dot}"></span>
      <span class="dev-main">
        <b>${escapeHtml(d.name || d.dev_id || "BindDeckLite")}</b>
        ${sub}
      </span>`;
    li.onclick = () => selectDevice(d.dev_id);
    ul.appendChild(li);
  });
}

async function selectDevice(devId) {
  // 切换设备：上一台设备的结果浮层不再适用。
  clearApplyResults();
  state.selected = devId;
  state.preview = false;
  localStorage.setItem("binddecklite.device", devId);
  renderDevices();
  try {
    state.cfg = await api(`/api/config/${encodeURIComponent(devId)}`);
    renderKeys();
    renderEncoder();
    renderPins();
    renderDeviceMeta();
    state.dirty = false;
    setDirty(false);
    // WS-445: 切换设备后按设备各自采样，不与上一台的缓存混用。
    loadSysinfo(devId);
  } catch (e) {
    toast("读取配置失败：" + e.message, "bad");
  }
}

/* ---------------- keys ---------------- */

function keyOptions(selected) {
  return state.keycodes
    .map((k) => `<option value="${k.code}" ${k.code === selected ? "selected" : ""}>${k.label}</option>`)
    .join("");
}

function renderKeys() {
  const grid = $("#keyGrid");
  grid.innerHTML = "";
  if (!state.cfg) return;
  $("#keyHint").textContent = state.preview ? "预览 · 未连接设备" : "共 10 个输入";
  const keys = state.cfg.keys || [];
  for (let i = 0; i < 10; i++) {
    const item = keys[i] || { code: 0, label: "?", action: { type: "none", value: "" } };
    const action = item.action || { type: "none", value: "" };
    const card = document.createElement("div");
    card.className = "keycard" + (i === 9 ? " encoder-push" : "");
    card.dataset.idx = i;
    card.innerHTML = `
      <div class="kc-head">
        <span class="kc-num">${i === 9 ? "⏻" : i + 1}</span>
        <span class="kc-title">${i === 9 ? "编码器按压" : "按键 " + (i + 1)}</span>
        <span class="kc-live"></span>
      </div>
      <label class="field-label">HID 键</label>
      <select class="sel-key">${keyOptions(item.code)}</select>
      <label class="field-label">上位机动作</label>
      <select class="sel-act">
        ${ACTION_TYPES.map((a) => `<option value="${a.id}" ${a.id === action.type ? "selected" : ""}>${a.name}</option>`).join("")}
      </select>
      <div class="act-input"></div>
      <button class="btn tiny test">测试动作</button>`;
    grid.appendChild(card);

    const selKey = $(".sel-key", card);
    const selAct = $(".sel-act", card);
    const holder = $(".act-input", card);

    selKey.onchange = () => {
      const code = parseInt(selKey.value, 10);
      item.code = code;
      item.label = (state.keycodes.find((k) => k.code === code) || {}).label || String(code);
      markDirty();
    };
    selAct.onchange = () => {
      item.action = { type: selAct.value, value: "", macro: [] };
      renderActionInput(holder, item, card);
      markDirty();
    };
    renderActionInput(holder, item, card);

    $(".test", card).onclick = async () => {
      const devId = state.selected;
      if (card.classList.contains("running")) {
        // 执行中：这颗按钮就是止损入口（WS-495），不是重入。
        stopAction(devId, i);
        return;
      }
      const noAction = !item.action || (item.action.type || "none") === "none";
      // /api/action/test 最多阻塞 5 秒才返回，先本地点亮执行中态；但 300 ms
      // 内就结束的动作不该让按钮闪一下「停止」，所以延迟点亮，快速返回时
      // 定时器直接清掉（WS-495 验收：5 秒内正常结束不出现「停止」）。
      let armTimer = null;
      if (devId && !noAction) {
        armTimer = setTimeout(() => {
          markRunning(devId, i);
          applyRunningState();
        }, 300);
      }
      let r = null;
      try {
        r = await api(`/api/action/test/${encodeURIComponent(devId)}/${i}`, { method: "POST" });
        if (r.ok) {
          // Show which browser a macro actually used, so a silent fallback to
          // the system default is visible instead of surprising.
          const used = r.browser_used && r.browser_used !== "default" ? ` · ${r.browser_used}` : "";
          toast("动作执行成功：" + (item.action.type === "none" ? "无上位机动作" : item.action.type) + used, "ok");
        } else if (r.cancelled) {
          // WS-495: 用户按下了「停止」，不是失败 —— 中性提示。
          toast("已停止", "");
        } else if (r.running) {
          // 仍在执行（重入被拒绝，或宏总时长超过 5 秒）：保持执行中态。
          if (devId) markRunning(devId, i, r.elapsed || 0);
          applyRunningState();
          toast(r.error || "该按键的动作仍在执行", "warn");
        } else {
          toast("动作执行失败：" + (r.error || "未知错误"), "bad");
        }
      } catch (e) {
        toast(e.message, "bad");
      } finally {
        clearTimeout(armTimer);
        if (!(r && r.running)) {
          if (devId) clearRunning(devId, i);
          applyRunningState();
        }
      }
    };
  }
  applyRunningState();
}

/* ---------- 执行中态（WS-359）+ 停止入口（WS-495） ----------
 * 服务端 /api/status 的 running 字段是唯一权威来源；这里只在点击测试的
 * 5 秒等待窗口内先本地标记，随后由轮询结果覆盖。
 *
 * 执行期间按钮不再是灰掉的「执行中…」，而是「停止」：点它就请求服务端在
 * 下一步边界退出。已经发出的按键收不回来，所以文案必须写明这一点。 */

// 执行中（可点）与已请求停止（不可点）两态的提示。
const STOP_BTN_TITLE = "在当前步骤结束后停止，已经发出的按键不会被撤回";
const STOPPING_BTN_TITLE = "已发出停止请求，当前步骤结束后退出";

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
 * 请求停止一条正在执行的动作（WS-495）。
 *
 * 成功（含 already_done）后不立刻假装已结束：服务端在下一步边界才退出，
 * 这里保持「正在停止…」，由 /api/status 不再报 running 时自动回到
 * 「测试动作」，所以卡片不会先跳回「停止」再跳一次。
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
    toast("停止请求没有送达，请再试一次", "warn");
    return;
  }
  if (r.already_done) {
    // 服务端没有在跑的 claim（宏刚好结束）：直接回空闲态。
    clearRunning(devId, idx);
    clearStopping(devId, idx);
  }
  applyRunningState();
  toast("已停止", "");        // 中性提示：停止不是失败
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
    btn.textContent = "正在停止…";
    btn.classList.add("danger");
    btn.title = STOPPING_BTN_TITLE;
    return;
  }
  if (live) {
    // 执行中：按钮可点，就是止损入口。
    btn.disabled = false;
    btn.textContent = "停止";
    btn.classList.add("danger");
    btn.title = STOP_BTN_TITLE;
    return;
  }
  btn.disabled = false;
  btn.textContent = "测试动作";
  btn.classList.remove("danger");
  btn.title = TEST_BTN_IDLE_TITLE;
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
}

function renderActionInput(holder, item, card) {
  const action = item.action || { type: "none", value: "" };
  holder.innerHTML = "";
  if (action.type === "none") {
    holder.innerHTML = `<p class="muted small">只发送上面的 HID 键。</p>`;
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
    const ta = document.createElement("textarea");
    ta.className = "in-val macro";
    ta.rows = 3;
    // 键名列表放在第一行：卡片里的输入框只有 3 行高，用户一眼能看到支持
    // 哪些键名；完整示例与语法说明见右上角「帮助」。
    ta.placeholder =
      "键名：" + MACRO_KEY_HINT + "\n" +
      "每行一步：app: / browser: / url: / wait:（毫秒）/ text: / text!80: / paste: / key:\n" +
      "例：app:C:\\path\\app.exe → text:hello";
    ta.title = "每行一步；完整语法与键名对照见右上角「帮助」";
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
  btn.textContent = "浏览…";
  btn.title = "调用系统文件选择器";
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = "选择中…";
    try {
      const res = await fetch("/api/browse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: "file" }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        toast(data.error || "无法打开系统文件选择器", "bad");
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
      toast("文件选择失败：" + (e.message || e), "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = "浏览…";
    }
  };
  row.append(input, btn, list);
  holder.appendChild(row);
}

function formatMacro(steps) {
  return steps
    .map((s) => {
      if (s.type === "delay") return "wait:" + (s.value || 0);
      if (s.type === "text") {
        // 只有显式写了延迟才输出 text!<ms>:；默认 20ms 由主机端套用，
        // 这样旧宏序列化回来仍是 text:xxx（往返一致）。
        // 注意 delay: 0 是「关闭延迟」（主机端 _text_delay() 返回 0），
        // 不是「使用默认值」，因此照原样序列化为 text!0:；默认值只在
        // delay 字段缺失 / 非法时生效。
        const d = typeof s.delay === "number" && isFinite(s.delay) ? s.delay : null;
        return (d === null ? "text:" : "text!" + d + ":") + (s.value || "");
      }
      if (s.type === "paste") return "paste:" + (s.value || "");
      if (s.type === "url") return "url:" + (s.value || "");
      if (s.type === "browser") return "browser:" + (s.value || "");
      if (s.type === "app") return "app:" + (s.value || "");
      return "key:" + (s.value || "");
    })
    .join("\n");
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
      // text!<延迟毫秒>:<文本> 覆盖默认的 20ms 逐字符延迟，如 text!80:pass
      let textDelay = null;
      const m = /^text!(\d+)$/.exec(kind);
      if (m) {
        textDelay = parseInt(m[1], 10);
        kind = "text";
      }
      if (kind === "wait" || kind === "delay") return { type: "delay", value: parseInt(value, 10) || 0 };
      if (kind === "text") {
        // 没写延迟就不带 delay 字段，主机端用默认 20ms，旧宏行为不变。
        return textDelay === null
          ? { type: "text", value }
          : { type: "text", value, delay: textDelay };
      }
      if (kind === "paste") return { type: "paste", value };
      if (kind === "url") return { type: "url", value };
      if (kind === "browser") {
        // 名字统一小写；主机端按 chrome/firefox/edge/safari/default 解析，
        // 其余值回退默认浏览器。
        return { type: "browser", value: value.toLowerCase() };
      }
      if (kind === "key") {
        // 只存键名本身；旧版本曾把整行（含 key: 前缀）写进 value，这里把
        // 累积的前缀去掉，否则重启后会变成 key:key:tab。
        return { type: "key", value: value.replace(/^(?:key:)+/i, "") };
      }
      if (kind === "app") {
        // 启动桌面应用程序：可执行文件路径，可带参数（引号包裹含空格的路径）。
        // 主机端 _parse_app_cmd 拆出 (exe, args)：第一个 token 作为 exe 交给
        // _resolve_exe 解析，其余为参数，原样传给 Popen。
        return { type: "app", value };
      }
      return { type: "key", value: line };
    });
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
  const mode = state.cfg.encoder ? state.cfg.encoder.mode : 0;
  ENCODER_MODES.forEach((m) => {
    const el = document.createElement("button");
    el.className = "mode-card" + (m.id === mode ? " active" : "");
    el.innerHTML = `<b>${m.name}</b><span>${m.desc}</span>`;
    el.onclick = () => {
      state.cfg.encoder = state.cfg.encoder || {};
      state.cfg.encoder.mode = m.id;
      renderEncoder();
      markDirty();
    };
    grid.appendChild(el);
  });
  // WS-389: the rotation-action card depends on the active mode, so redraw it
  // whenever the mode grid is (re)rendered.
  renderEncoderActions();
}

/* ---------------- encoder rotation actions (WS-389) ---------------- */

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
  // 于是灰卡上的按钮又能点，出现「假成功」（WS-390 评审 #1）。
  const locked = row.dataset.locked === "1";
  btn.disabled = live || locked;
  btn.textContent = live ? "执行中…" : "测试动作";
  btn.title = live
    ? `动作仍在执行（已运行 ${Number(secs).toFixed(1)} 秒），重复按下会被忽略`
    : (locked ? "切到「上位机动作」模式后可配置旋转动作" : TEST_BTN_IDLE_TITLE);
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
  // 不能一直显示旧文案（WS-390 评审 #3）。
  if (!editable) return "切到「上位机动作」模式后可配置";
  const cw = actions.CW || defaultEncoderAction();
  const ccw = actions.CCW || defaultEncoderAction();
  const cwType = cw.type || "none";
  const ccwType = ccw.type || "none";
  if (cwType === "none" && ccwType === "none") {
    return "未配置：旋转只会发送 F23/F24，不会有任何效果";
  }
  const nameOf = (t) => {
    const hit = ACTION_TYPES.find((a) => a.id === t);
    return hit ? hit.name : t;
  };
  return `顺时针：${nameOf(cwType)} · 逆时针：${nameOf(ccwType)}；` +
    "上位机动作模式下设备仍会额外发送 F23/F24，如与现有软件冲突请改用其他模式";
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
  const editable = mode === 4;

  const card = document.createElement("div");
  card.className = "enc-action-card" + (editable ? "" : " disabled");

  const statusEl = document.createElement("p");
  statusEl.className = "enc-action-status";
  const refreshStatus = () => {
    statusEl.textContent = encoderStatusText(actions, editable);
  };
  refreshStatus();
  card.appendChild(statusEl);

  const dirs = [
    { dir: "CW", label: "顺时针" },
    { dir: "CCW", label: "逆时针" },
  ];
  dirs.forEach(({ dir, label }) => {
    const action = actions[dir];
    const row = document.createElement("div");
    row.className = "enc-action-row";
    row.dataset.dir = dir;
    // 其他模式下整卡置灰：记住这个状态，状态轮询刷新执行中态时不能把
    // 测试按钮重新启用（WS-390 评审 #1）。
    row.dataset.locked = editable ? "0" : "1";
    row.innerHTML = `
      <span class="enc-dir-label">${label}</span>
      <select class="sel-enc-act">
        ${ACTION_TYPES.map((a) =>
          `<option value="${a.id}" ${a.id === action.type ? "selected" : ""}>${a.name}</option>`).join("")}
      </select>
      <div class="enc-act-input"></div>
      <button class="btn tiny test">测试动作</button>`;
    card.appendChild(row);

    const sel = row.querySelector("select");
    const holder = row.querySelector(".enc-act-input");
    const btn = row.querySelector("button");

    sel.onchange = () => {
      actions[dir] = { type: sel.value, value: "", macro: [] };
      renderActionInput(holder, { action: actions[dir] },
                       { dataset: { idx: "enc-" + dir } });
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
        toast("该方向的动作仍在执行", "warn");
        return;
      }
      if (!editable) {
        // 灰卡上的按钮本应 disabled，兜底挡住键盘触发。
        toast("切到「上位机动作」模式后可配置旋转动作", "warn");
        return;
      }
      const devId = state.selected;
      if (!devId || !state.selectedConnected) {
        toast("未连接设备", "bad");
        return;
      }
      const noAction = !actions[dir] || (actions[dir].type || "none") === "none";
      if (!noAction) {
        markEncRunning(devId, dir);
        applyEncoderRunningState();
      }
      let r = null;
      try {
        r = await api(`/api/encoder/action/test/${encodeURIComponent(devId)}/${dir}`,
                      { method: "POST" });
        if (r.ok) {
          const used = r.browser_used && r.browser_used !== "default"
            ? ` · ${r.browser_used}` : "";
          toast("动作执行成功：" + (actions[dir].type === "none"
            ? "无上位机动作" : actions[dir].type) + used, "ok");
        } else if (r.running) {
          if (devId) markEncRunning(devId, dir, r.elapsed || 0);
          applyEncoderRunningState();
          toast(r.error || "该方向的动作仍在执行", "warn");
        } else {
          toast("动作执行失败：" + (r.error || "未知错误"), "bad");
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

/* 运行状态（WS-445）：固件只回报事实，文案与着色都在这里。
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

function renderSysinfo() {
  const box = $("#sysInfo");
  const atEl = $("#sysSampleAt");
  if (!box || !atEl) return;
  const devId = state.selected;
  if (!devId || state.preview) {
    box.innerHTML = `<span class="k">状态</span><span class="v muted">未选择设备</span>`;
    atEl.textContent = "";
    return;
  }
  if (otaBusy()) {
    box.innerHTML = `<span class="k">状态</span><span class="v warn">OTA 中，暂停查询</span>`;
    atEl.textContent = "";
    return;
  }
  const dev = state.devices.find((d) => d.dev_id === devId);
  const connected = !!(dev && dev.connected);
  const info = sysinfoFor(devId);

  if (!info) {
    box.innerHTML = `<span class="k">状态</span><span class="v muted">${
      connected ? "读取中…" : "设备未连接，暂无采样"
    }</span>`;
    atEl.textContent = "";
    return;
  }

  if (info.supported === false) {
    // 旧固件：2 秒内没有 SYS: 行。这是「不支持」，不是错误。
    box.innerHTML = `<span class="k">状态</span><span class="v muted">当前固件不支持运行状态（需更新固件）</span>`;
    atEl.textContent = (info.at ? `采样 ${fmtClock(info.at)}` : "")
      + (connected ? "" : " · 已断开，非实时");
    return;
  }

  const d = info.display || {};
  const flags = info.flags || {};
  const notes = info.notes || {};
  const resetNote = notes.deepsleep
    ? `<span class="muted small"> · ${escapeHtml(notes.deepsleep)}</span>` : "";
  const rows = [
    ["上次复位", escapeHtml(d.reset || "未知") + resetNote, flags.deepsleep ? "warn" : ""],
    ["运行时长", escapeHtml(d.uptime || "未知"), ""],
    ["NVS", escapeHtml(d.nvs || "未知"), flags.nvs_bad ? "bad" : ""],
    ["空闲堆", escapeHtml(d.heap || "未知"), flags.heap_low ? "warn" : ""],
  ];
  const extraNotes = [];
  if (notes.safe) extraNotes.push(`<li>${escapeHtml(notes.safe)}</li>`);
  if (notes.heap) extraNotes.push(`<li>${escapeHtml(notes.heap)}</li>`);
  box.innerHTML = rows
    .map(([k, v, cls]) => `<span class="k">${k}</span><span class="v ${cls}">${v}</span>`)
    .join("") + (extraNotes.length
      ? `<span class="k"></span><ul class="sys-notes">${extraNotes.join("")}</ul>` : "");

  const when = info.at ? `采样 ${fmtClock(info.at)}` : "";
  const stale = connected ? "" : " · 已断开，非实时";
  atEl.textContent = when + stale;
}

function renderDeviceMeta() {
  if (!state.cfg) return;
  const chip = state.cfg.chip || "ESP32";
  const fw = state.cfg.fw || {};
  $("#chipBadge").textContent = chip;
  $("#deviceMeta").innerHTML = `
    <div><span>设备</span><b>${escapeHtml(state.cfg.name || state.selected || "—")}</b></div>
    <div><span>固件</span><b>${escapeHtml(fw.version || "—")}</b></div>
    <div><span>编译</span><b class="mono">${escapeHtml(fw.build || "—")}</b></div>
    <div><span>输入</span><b>9 键 + EC11</b></div>`;
  $("#sleepEnabled").checked = !!(state.cfg.sleep && state.cfg.sleep.enabled);
  $("#sleepMinutes").value = (state.cfg.sleep && state.cfg.sleep.minutes) || 5;

  // WS-321: host binding status
  const bond = state.cfg.bond || { bound: false, addr: "" };
  const bondEl = $("#bondStatus");
  const clearBtn = $("#btnClearBond");
  if (bond.bound) {
    // WS-423: the live NimBLE bond state now arrives on BOND_DIAG (separate
    // from BOND_STATUS), so the address stays clean. Show it next to the addr.
    const diag = bond.peer_sec === undefined ? "" :
      ` <span class="muted small">· 配对 ${bond.peer_sec} 个 · NimBLE 绑定 ${bond.nimble_bond ? "正常" : "缺失"}</span>`;
    bondEl.innerHTML = `<span class="dot ok"></span>已绑定 <b>${escapeHtml(bond.addr || "—")}</b>${diag}`;
    clearBtn.hidden = false;
    $("#bondHint").textContent = "只有已绑定的主机能发送 HID 按键；清除会同时断开它的连接，之后任何主机均可接入。";
  } else {
    bondEl.innerHTML = `<span class="dot warn"></span>未绑定`;
    clearBtn.hidden = true;
    $("#bondHint").textContent = "首次 HID 订阅的主机会被绑定；按住编码器 5 秒也可清除。";
  }

  // 引脚提示文案也来自后端规则（/api/defaults 的 pinRules），避免 JS 里再写一份
  // 与固件可能漂移的引脚限制。
  renderPinHint();
  // WS-409: 设备页的「上次读取」状态行。
  renderReadStatus();
  // WS-445: 运行状态卡片（采样请求由 selectDevice / 重连 / 刷新 / 打开设备页触发）。
  renderSysinfo();
}

function renderPins() {
  const box = $("#pinGrid");
  box.innerHTML = "";
  if (!state.cfg) return;
  // 旧 config.json 可能缺项 / 是 null / 甚至不是数组：照原样渲染，由校验标红，
  // 不在这里替用户补默认值（WS-476 要求不改写用户的值）。
  const pins = Array.isArray(state.cfg.pins) ? state.cfg.pins : [];
  for (let i = 0; i < PIN_COUNT; i++) {
    const wrap = document.createElement("label");
    wrap.className = "pin";
    const name = document.createElement("span");
    name.textContent = `按键 ${i + 1}`;
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
  // 每次重绘都先清掉上一轮的结论，再按当前值重算，避免旧红框挂在新值上。
  applyPinErrors([]);
  validatePins();
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
  const query = `?chip=${encodeURIComponent(state.cfg.chip || "ESP32")}`
    + `&pins=${encodeURIComponent(tokens.join(","))}`;
  let errors;
  try {
    const r = await api("/api/defaults" + query);
    errors = Array.isArray(r.pinErrors) ? r.pinErrors : [];
    if (r.pinRules) {
      state.pinRules = r.pinRules;
      renderPinHint();
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

function renderPinErrorCount() {
  const el = $("#pinErrorCount");
  if (!el) return;
  const n = state.pinErrors.length;
  el.textContent = n ? `${n} 处需要修正` : "";
  el.hidden = !n;
}

/** 滚动并聚焦第一处错误行（非平滑滚动，避免长时间动画）。 */
function focusFirstPinError(errors) {
  const first = (errors || [])[0];
  if (!first) return;
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
  badge.textContent = d.frozen ? "打包版 exe" : d.desktop_mode ? "托盘模式" : "源码运行";

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
  if (d.tray_active) bits.push("托盘图标在任务栏右下角：单击显示控制台，右键可显示 / 退出");
  else if (d.desktop_mode) {
    if (d.window_hidden) bits.push("托盘不可用，窗口已隐藏：可用下方「显示窗口」恢复，或「退出上位机」关闭");
    else bits.push("托盘不可用，程序在后台运行，可用下方「退出上位机」关闭");
  }
  else bits.push("当前由 run.sh / python app.py 启动，没有托盘图标；打包版双击即带托盘");
  if (auto.supported) {
    bits.push(`开机自启动${auto.enabled ? "已开启" : "未开启"}（${auto.method}）`);
  } else {
    bits.push("开机自启动不可用");
  }
  if (auto.error) bits.push(auto.error);
  // auto.admin：自启动条目是不是「计划任务（最高权限）」。
  // auto.is_admin：当前这个进程本身有没有提权。两者互相独立。
  if (auto.admin) bits.push("当前以计划任务最高权限启动");
  if (isWindows && !auto.is_admin) {
    bits.push("当前未提权，宏无法输入到以管理员运行的程序，可点「立即以管理员身份重启」");
  }
  if (auto.admin_error) bits.push(auto.admin_error);
  const desktopError = (d.error || "").replace(/^托盘不可用：/, "");
  if (desktopError && !bits.includes(desktopError)) bits.push(desktopError);
  $("#desktopHint").textContent = bits.join("；") + "。";
  if (auto.detail) $("#desktopHint").title = auto.detail;
}

async function updateDesktop(patch) {
  const res = await fetch("/api/desktop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || "设置失败");
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

/** 重新挂起自动保存。 */
function armPendingSave() {
  cancelPendingSave();
  state.saveTimer = setTimeout(saveConfig, 500);
}

function setDirty(dirty) {
  state.dirty = dirty;
  const btn = $("#btnApply");
  btn.textContent = dirty ? "应用配置 •" : "应用配置";
  btn.classList.toggle("dirty", dirty);
}

async function saveConfig() {
  if (!state.selected || !state.cfg) return;
  try {
    await api(`/api/config/${encodeURIComponent(state.selected)}`, {
      method: "POST",
      body: JSON.stringify(state.cfg),
    });
  } catch (e) {
    toast("保存失败：" + e.message, "bad");
  }
}

async function applyConfig() {
  if (otaBusy()) {
    toast("OTA 进行中，配置下发已锁定", "bad");
    return;
  }
  if (!state.selected) {
    toast("未连接设备，当前只是默认配置预览", "bad");
    return;
  }
  // WS-476：先校验。有错误就一条 CFG: 都不发，直接把用户带到第一处错误行。
  // 顺序放在「未连接设备」之后：预览/离线仍只提示未连接，不叠加两套 toast。
  const pinErrors = await validatePins();
  if (pinErrors.length) {
    toast(`按键引脚有 ${pinErrors.length} 处错误`, "bad");
    focusFirstPinError(pinErrors);
    return;
  }
  await saveConfig();
  try {
    const r = await api(`/api/apply/${encodeURIComponent(state.selected)}`, { method: "POST" });

    // New contract: per-item readback results.
    if (r.items && r.items.length) {
      renderApplyResults(r.items);
      if (r.ok) {
        toast("配置已生效", "ok");
        setDirty(false);
      } else {
        toast("部分配置未生效，请查看右上角结果", "bad");
        // Keep dirty — the button stays "应用配置 •" until everything is ok.
      }
      return;
    }

    // Legacy fallback (old backend that only returns {ok, results}).
    if (r.ok) {
      toast(`已下发到设备（${r.results.filter((x) => x.sent).length} 条）`, "ok");
      setDirty(false);
    } else {
      toast("下发失败：设备未连接？", "bad");
    }
  } catch (e) {
    toast(e.message, "bad");
  }
}

/* ---------------- apply results overlay ---------------- */

// 全部生效时浮层自动消失的时长；鼠标悬停或键盘聚焦时暂停计时。
const APPLY_AUTOCLOSE_MS = 5000;
// 与 style.css 中 .apply-results 的过渡时长保持一致。
const APPLY_FADE_MS = 200;

// 自动消失计时状态：deadline 为绝对时间戳，remaining 是暂停时刻的剩余毫秒。
let applyTimer = null;
let applyDeadline = 0;
let applyRemaining = 0;

function stopApplyTimer() {
  if (applyTimer !== null) {
    clearTimeout(applyTimer);
    applyTimer = null;
  }
}

function startApplyTimer(ms) {
  stopApplyTimer();
  if (ms <= 0) return;
  applyDeadline = Date.now() + ms;
  applyTimer = setTimeout(dismissApplyResults, ms);
}

function pauseApplyTimer() {
  if (applyTimer === null) return;
  applyRemaining = Math.max(0, applyDeadline - Date.now());
  stopApplyTimer();
}

function resumeApplyTimer() {
  if (applyTimer === null && applyRemaining > 0) startApplyTimer(applyRemaining);
}

function clearFieldHighlights() {
  $$(".field-highlight").forEach((el) => el.classList.remove("field-highlight"));
}

function clearApplyResults() {
  stopApplyTimer();
  applyRemaining = 0;
  const existing = $("#applyResults");
  if (existing) existing.remove();
  clearFieldHighlights();
}

// 自动消失：先淡出，再移除节点。若期间被新的结果替换或手动关闭，
// 节点已不在文档中，这里直接放弃，避免清掉新浮层的字段高亮。
function dismissApplyResults() {
  stopApplyTimer();
  applyRemaining = 0;
  const el = $("#applyResults");
  if (!el) {
    clearFieldHighlights();
    return;
  }
  el.classList.add("leaving");
  setTimeout(() => {
    if (!el.isConnected) return;
    el.remove();
    clearFieldHighlights();
  }, APPLY_FADE_MS);
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
  title.textContent = allOk ? "配置结果 · 全部已生效" : "配置结果 · 有项目未生效";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "apply-results-close";
  closeBtn.textContent = "×";
  closeBtn.title = "关闭（Esc）";
  closeBtn.setAttribute("aria-label", "关闭配置结果");
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
        statusText = "已生效";
        break;
      case "rejected":
        statusText = `未生效：设备拒绝（${item.reason}）`;
        break;
      case "unconfirmed":
        statusText = `未确认：${item.reason}`;
        break;
      case "unverified":
        statusText = item.reason;
        break;
      default:
        statusText = item.status;
    }

    const actionBtn = (item.status !== "ok")
      ? `<button class="btn tiny accept-device-value" onclick="acceptDeviceValue('${item.field}')">以设备为准</button>`
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
  // WS-409: 动态生成的「以设备为准」按钮也要服从 OTA 锁。
  applyOtaLock();

  // Highlight the form fields whose value was rejected.
  items.forEach((item) => {
    if (item.status === "rejected") highlightFormField(item.field);
  });

  // 全部生效 → 5 秒后自动淡出，悬停 / 聚焦期间暂停；有未生效项 → 常驻，
  // 由用户点 × 或按 Esc 关闭。
  if (allOk) {
    applyRemaining = APPLY_AUTOCLOSE_MS;
    startApplyTimer(APPLY_AUTOCLOSE_MS);
    container.addEventListener("mouseenter", pauseApplyTimer);
    container.addEventListener("mouseleave", resumeApplyTimer);
    container.addEventListener("focusin", pauseApplyTimer);
    container.addEventListener("focusout", resumeApplyTimer);
  }
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

/* ---------------- 读取设备配置（WS-409） ---------------- */

// 设备回报的配置分组（与服务端 app.py 的 _CFG_READ_FIELDS 一致）。
// 这四组全部回来才算一次完整的回读；缺任何一组都按「设备未回报」处理。
const READ_FIELDS = ["pins", "keys", "enc", "sleep"];
const READ_FIELD_LABELS = {
  pins: "按键引脚",
  keys: "按键码",
  enc: "编码器模式",
  sleep: "休眠",
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
    toast("OTA 进行中，设备指令已锁定", "bad");
    return null;
  }
  if (state.dirty) {
    if (!window.confirm("有修改尚未下发到设备，读取设备配置会丢弃它们")) return null;
  }
  // 挂起的自动保存必须先停掉：它会把前端旧副本写回服务端，把刚读回来的
  // 设备真值覆盖掉（WS-409）。
  const wasDirty = state.dirty;
  cancelPendingSave();
  try {
    const r = await api(`/api/device/${encodeURIComponent(devId)}/refresh`, { method: "POST" });
    // 多设备：请求期间用户切到了另一台 → 这次结果作废。
    if (state.selected !== devId) return null;
    state.readStatus[devId] = { at: r.at, fields: r.fields || {}, ok: !!r.ok };
    renderReadStatus();
    if (!r.ok) {
      // 表单与本地配置都不动，但用户原本的修改仍要能存下去。
      if (wasDirty) armPendingSave();
      toast(`${r.reason || "设备未回报配置"}，可能固件过旧`, "bad");
      return r;
    }
    applyReadback(r);
    return r;
  } catch (e) {
    if (wasDirty) armPendingSave();
    toast("读取设备配置失败：" + (e.message || e), "bad");
    return null;
  }
}

/** 用设备回报的配置覆盖 state.cfg 并重绘整个表单。 */
function applyReadback(r) {
  if (!r || !r.cfg) return;
  state.cfg = r.cfg;
  setDirty(false);
  renderKeys();
  renderEncoder();
  renderPins();
  renderDeviceMeta();
  renderReadStatus();
  const got = readFieldCount(r.fields);
  toast(`已读取设备配置（${fmtClock(r.at)}）· ${got}/${READ_FIELDS.length} 项`, "ok");
}

/** 设备页状态行：上次读取时间 + 设备回报了几组（缺的标「设备未回报」）。 */
function renderReadStatus() {
  const el = $("#cfgReadStatus");
  if (!el) return;
  const st = state.readStatus[state.selected];
  if (!st || !st.at) {
    el.textContent = "尚未读取设备配置";
    return;
  }
  const got = readFieldCount(st.fields);
  const missing = READ_FIELDS.filter((k) => !(st.fields || {})[k]);
  let text = `上次读取：${fmtClock(st.at)}（设备回报 ${got}/${READ_FIELDS.length} 项）`;
  if (!got) {
    // 设备一句都没回：不要笼统报成功。
    text += " · 设备未回报配置";
  } else if (missing.length) {
    text += " · " + missing.map((k) => `${READ_FIELD_LABELS[k]} 设备未回报`).join("、");
  }
  el.textContent = text;
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

// WS-470：日志「跟随 / 暂停」。判据只有一条 —— 用户是否停在底部（≤ 24 px）。
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

    r.events.forEach((e) => {
      const row = document.createElement("div");
      row.className = "log-row " + (e.level || "info");
      row.innerHTML = `<span class="log-time">${e.time}</span><span class="log-kind">${e.kind}</span><span class="log-text">${escapeHtml(e.text)}</span>`;

      // 每行右侧的「复制」按钮：把这一行的完整文本（时间 + 类型 + 内容）
      // 放进剪贴板，方便贴到 issue / 聊天里。
      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "log-copy";
      copyBtn.textContent = "复制";
      copyBtn.title = "复制这一行（宏正文已隐藏）";
      copyBtn.onclick = () => copyLogLine(`${e.time} [${e.kind}] ${e.text}`, copyBtn);
      row.appendChild(copyBtn);

      list.appendChild(row);
      // Physical-button action failures arrive through the log stream;
      // surface them as a toast so the user sees them immediately.
      if (e.kind === "action" && e.level === "error") {
        toast(e.text, "bad");
      }
    });

    // 裁剪前后的内容高度差 = 被移除行占掉的高度，暂停态要补回滚动位置。
    const heightBeforeTrim = list.scrollHeight;
    while (list.children.length > 300) list.removeChild(list.firstChild);
    const removedHeight = Math.max(0, heightBeforeTrim - list.scrollHeight);

    // 计数按实际新增行数累加（裁剪前统计），不按批次 +1。
    state.logPending = window.LogFollow.pendingAfterAppend(state.logPending, r.events.length, state.logFollow);

    if (state.logFollow) {
      stickLogToBottom(list);
    } else if (removedHeight > 0) {
      list.scrollTop = window.LogFollow.compensateTrim(list.scrollTop, removedHeight);
      realignLogAnchor(list, anchor);
    }
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
    toast("复制失败，请手动选中日志文本后按 Ctrl+C", "bad");
    return;
  }
  toast("已复制日志");
  if (!btn) return;
  const label = btn.textContent;
  btn.textContent = "已复制";
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
  $$(".tab").forEach((tab) => {
    tab.onclick = () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      $$(".panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      $("#tab-" + tab.dataset.tab).classList.add("active");
      if (tab.dataset.tab === "device") {
        renderDeviceMeta();
        // WS-445: 打开设备页采样一次运行状态。
        loadSysinfo(state.selected);
      }
      // 日志页隐藏期间容器量不出高度，切回来时补一次贴底 / 胶囊重绘。
      if (tab.dataset.tab === "log") syncLogFollowUi({ stick: true });
    };
  });
}

/* ---------------- help ---------------- */

/* 帮助浮层：说明宏语法、键名对照、登录示例与常见错误。纯前端开关，
   不请求后端，关掉后页面状态完全不变。 */
function bindHelp() {
  const modal = $("#helpModal");
  const openBtn = $("#btnHelp");
  const closeBtn = $("#btnHelpClose");
  if (!modal || !openBtn || !closeBtn) return;

  const open = () => {
    modal.hidden = false;
    document.body.classList.add("modal-open");
    closeBtn.focus();
  };
  const close = () => {
    modal.hidden = true;
    document.body.classList.remove("modal-open");
    openBtn.focus();
  };

  openBtn.onclick = open;
  closeBtn.onclick = close;
  // 点击遮罩关闭，点击卡片内部不关闭。
  modal.onclick = (e) => {
    if (e.target === modal) close();
  };
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || modal.hidden) return;
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
    const modal = $("#helpModal");
    if (modal && !modal.hidden) return;
    clearApplyResults();
  });
}

async function init() {
  bindTabs();
  bindHelp();
  bindApplyResults();
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
    toast("正在扫描…");
    try {
      const r = await api("/api/scan");
      // WS-395: 说清楚为什么没扫到，而不是只报一个「0 台设备」。
      toast(r.reason ? `扫描未发现设备：${r.reason}` : `扫描到 ${r.found ? r.found.length : 0} 台设备`);
    } catch (e) {
      toast("扫描失败：" + e.message, "bad");
    }
  };
  $("#btnApply").onclick = applyConfig;
  $("#btnClearLog").onclick = () => {
    $("#logList").innerHTML = "";
    // 清空后没有可读的旧行，强制回跟随。
    state.logFollow = true;
    state.logPending = 0;
    syncLogFollowUi({ stick: true });
  };
  $("#actionsToggle").onchange = async (e) => {
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify({ actions_enabled: e.target.checked }) });
      toast(e.target.checked ? "上位机动作已启用" : "上位机动作已关闭");
    } catch (err) {
      toast(err.message, "bad");
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
      toast(want ? "已开启开机自启动" : "已关闭开机自启动", "ok");
    } catch (err) {
      target.checked = !want;
      toast("设置开机自启动失败：" + err.message, "bad");
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
        ? "将请求 UAC 授权（仅用于创建计划任务，不会重启上位机）"
        : "将请求 UAC 授权（用于删除计划任务，不会重启上位机）";
      if (!confirm(hint)) {
        target.checked = !want;
        return;
      }
    }
    target.disabled = true;
    try {
      await updateDesktop({ admin_autostart: want });
      toast(want ? "开机自启动将以管理员身份运行" : "已改为普通权限开机自启动", "ok");
    } catch (err) {
      target.checked = !want;
      toast("设置管理员自启动失败：" + err.message, "bad");
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
      toast(want ? "开机启动时收进托盘" : "开机启动时打开控制台");
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
      toast(minimize ? "关闭窗口将最小化到托盘" : "关闭窗口将直接退出应用");
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
      if (!res.ok || data.ok === false) throw new Error(data.error || "提权重启失败");
      toast("已拉起管理员实例，本实例即将退出", "ok");
    } catch (err) {
      toast("提权重启失败：" + (err.message || err), "bad");
      target.disabled = false;
    }
  };
  $("#btnQuitHost").onclick = async () => {
    if (!window.confirm("退出 BindDeckLite 上位机？退出后按键的上位机动作将不再执行。")) return;
    const target = $("#btnQuitHost");
    target.disabled = true;
    try {
      const res = await fetch("/api/quit", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || "退出失败");
      toast("已退出，可以关闭此页面", "ok");
    } catch (err) {
      toast("退出失败：" + (err.message || err), "bad");
      target.disabled = false;
    }
  };
  $("#btnShowWindow").onclick = async () => {
    const target = $("#btnShowWindow");
    target.disabled = true;
    try {
      const res = await fetch("/api/show-window", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || "恢复窗口失败");
      toast("已显示窗口", "ok");
      await refreshDesktop();
    } catch (err) {
      toast("恢复窗口失败：" + (err.message || err), "bad");
      target.disabled = false;
    }
  };
  const devAction = async (act, okText) => {
    if (!state.selected) return;
    if (otaBusy()) {
      toast("OTA 进行中，设备指令已锁定", "bad");
      return;
    }
    try {
      const r = await api(`/api/device/${encodeURIComponent(state.selected)}/${act}`, { method: "POST" });
      toast(r.sent === false ? "指令未送出：设备未连接" : okText, r.sent === false ? "bad" : "ok");
    } catch (e) {
      toast(e.message, "bad");
    }
  };
  $("#btnPing").onclick = () => devAction("ping", "已发送 Ping");
  // WS-445: 手动刷新运行状态（等待设备回 SYS: 行，不弹 toast）。
  $("#btnSysRefresh").onclick = async () => {
    const btn = $("#btnSysRefresh");
    if (!state.selected) {
      toast("没有选中设备", "bad");
      return;
    }
    btn.disabled = true;
    try {
      await loadSysinfo(state.selected);
    } finally {
      btn.disabled = false;
    }
  };
  // WS-409: 「读取设备配置」等设备回报后再回填表单；dirty 时先确认。
  $("#btnRefresh").onclick = () => readDeviceConfig(state.selected);
  $("#btnPortal").onclick = () => devAction("portal", "设备将重启进入配网模式");
  $("#btnReboot").onclick = () => devAction("reboot", "设备重启中");
  // WS-321: clear the persisted host binding so a new central can take over.
  $("#btnClearBond").onclick = async () => {
    if (!state.selected) return;
    if (!window.confirm("清除主机绑定后，任何主机都能控制键盘。确定要清除吗？")) return;
    const btn = $("#btnClearBond");
    btn.disabled = true;
    try {
      const r = await api(`/api/device/${encodeURIComponent(state.selected)}/clearbond`, { method: "POST" });
      if (r.sent === false) {
        toast("指令未送出：设备未连接", "bad");
        return;
      }
      toast("已发送清除绑定指令", "ok");
      // The device ACKs on the same CFG channel; give it a moment then refresh.
      setTimeout(() => {
        if (state.selected) api(`/api/device/${encodeURIComponent(state.selected)}/refresh`, { method: "POST" });
      }, 500);
    } catch (e) {
      toast("清除失败：" + (e.message || e), "bad");
    } finally {
      btn.disabled = false;
    }
  };
  $("#btnOta").onclick = openOtaPicker;
  $("#btnOtaStart").onclick = startOta;
  $("#btnOtaCancel").onclick = cancelOta;

  const initialTab = new URLSearchParams(location.search).get("tab");
  if (initialTab) {
    const tab = document.querySelector(`.tab[data-tab="${initialTab}"]`);
    if (tab) tab.click();
  }

  await refreshDesktop();
  await pollStatus();
  setInterval(pollStatus, 1200);
  setInterval(pollLog, 800);
  setInterval(pollOta, 500);
}

/* ---------------- OTA ---------------- */

function renderOta() {
  const card = $("#otaCard");
  const ota = state.ota;
  if (!ota) {
    card.hidden = true;
    state.otaFile = null;
    $("#btnOtaStart").hidden = true;
    applyOtaLock();
    return;
  }
  card.hidden = false;

  const sel = state.devices.find((d) => d.dev_id === state.selected);
  const connected = !!(sel && sel.connected);

  const shaLine = (ota.sha256 && ota.status !== "error")
    ? ` · SHA256 ${ota.sha256}` : "";
  if (ota.status === "done") {
    $("#otaStatusText").textContent = "升级成功";
    $("#otaFileLine").innerHTML = `<span class="ok">${escapeHtml(ota.filename || "")} · ${ota.total} bytes</span> · 设备即将重启`;
    $("#otaProgressBar").style.width = "100%";
    $("#btnOtaStart").hidden = true;
    $("#btnOtaCancel").hidden = false;
    $("#btnOtaCancel").textContent = "关闭";
    $("#otaHint").textContent = "升级成功，设备即将重启。重启后将自动重新连接。";
  } else if (ota.status === "error") {
    $("#otaStatusText").textContent = "失败";
    $("#otaFileLine").textContent = `${escapeHtml(ota.filename || "")} · ${ota.total} bytes${shaLine}`;
    $("#otaProgressBar").style.width = "0%";
    $("#btnOtaStart").hidden = true;
    $("#btnOtaCancel").hidden = false;
    $("#btnOtaCancel").textContent = "关闭";
    $("#otaHint").textContent = (ota.error || "未知错误")
      + (ota.sha256 ? `（所选文件 SHA256 ${ota.sha256}，请与发布页 WS-279 核对）` : "");
  } else if (ota.status === "uploading") {
    $("#otaStatusText").textContent = "上传中";
    $("#otaFileLine").textContent = `${escapeHtml(ota.filename || "")} · ${ota.total} bytes${shaLine}`;
    $("#otaProgressBar").style.width = ota.percent + "%";
    $("#btnOtaStart").hidden = true;
    // Cancel sends CMD:OTA_CANCEL so the device aborts and keeps the old image.
    $("#btnOtaCancel").hidden = false;
    $("#btnOtaCancel").textContent = "取消升级";
    const acked = ota.acked != null ? ota.acked : ota.sent;
    $("#otaHint").textContent =
      `${acked} / ${ota.total} bytes (${ota.percent}%) · 已确认字节 · 升级中请勿断电`;
  } else if (ota.status === "starting") {
    $("#otaStatusText").textContent = "启动中";
    $("#otaFileLine").textContent = `${escapeHtml(ota.filename || "")} · ${ota.total} bytes${shaLine}`;
    $("#otaProgressBar").style.width = "0%";
    $("#btnOtaStart").hidden = true;
    $("#btnOtaCancel").hidden = false;
    $("#btnOtaCancel").textContent = "取消升级";
    $("#otaHint").textContent = "正在进入 OTA 模式…";
  } else if (ota.status === "cancelled") {
    $("#otaStatusText").textContent = "已取消";
    $("#otaProgressBar").style.width = "0%";
    $("#btnOtaStart").hidden = true;
    $("#btnOtaCancel").hidden = false;
    $("#btnOtaCancel").textContent = "关闭";
    $("#otaHint").textContent = "升级已取消，设备保持原固件。";
  } else {
    // idle: file selected, waiting for user to click "开始升级"
    $("#otaStatusText").textContent = "就绪";
    $("#otaFileLine").textContent = `${escapeHtml(ota.filename || "")} · ${ota.total} bytes${shaLine}`;
    $("#otaProgressBar").style.width = "0%";
    $("#btnOtaStart").hidden = false;
    $("#btnOtaCancel").hidden = false;
    $("#btnOtaCancel").textContent = "取消";
    $("#otaHint").textContent = connected
      ? (ota.sha256
          ? `SHA256 ${ota.sha256}（请与发布页 WS-279 核对），点击「开始升级」推送`
          : "点击「开始升级」推送固件（分块传输，约需 1-3 分钟）")
      : "设备未连接，无法 OTA";
    $("#btnOtaStart").disabled = !connected;
  }
  applyOtaLock();
}

/* OTA lock: while firmware is being written the deck's configuration and
   device commands are frozen — a concurrent CFG:/CMD: write would interleave
   with the binary stream and corrupt the image. */
function otaBusy() {
  return !!(state.ota && (state.ota.status === "starting" || state.ota.status === "uploading"));
}

function applyOtaLock() {
  const busy = otaBusy();
  ["btnApply", "btnOta", "btnPing", "btnRefresh", "btnPortal", "btnReboot", "btnClearBond",
   "btnSysRefresh"]
    .forEach((id) => {
      const el = $("#" + id);
      if (el) el.disabled = busy;
    });
  // WS-409: 「以设备为准」按钮是运行时动态生成的，OTA 期间同样要锁住。
  $$(".accept-device-value").forEach((el) => {
    el.disabled = busy;
  });
}

async function pollOta() {
  if (!state.selected) return;
  // Terminal (done / error) and idle states do not need polling: the server
  // pops a finished task on its first read, so polling again would 404 and
  // wipe the result card the user needs to see.
  if (!state.ota || ["idle", "done", "error", "cancelled"].includes(state.ota.status)) return;
  try {
    const r = await api(`/api/ota/${encodeURIComponent(state.selected)}/progress`);
    if (!r.ok) {
      // The server no longer has the task (app restarted or device gone).
      toast("连接中断，请重试", "bad");
      state.ota.status = "error";
      state.ota.error = "连接中断，请重试";
      renderOta();
      return;
    }
    state.ota = {
      status: r.status,
      sent: r.sent,
      acked: r.acked != null ? r.acked : r.sent,
      total: r.total,
      percent: r.percent,
      error: r.error,
      filename: r.filename,
    };
    if (r.status === "done") {
      toast("升级成功，设备即将重启", "ok");
    } else if (r.status === "error") {
      toast(r.error || "升级失败", "bad");
    }
    renderOta();
  } catch (_) {
    /* ignore network blips */
  }
}

function openOtaPicker() {
  if (otaBusy()) {
    toast("OTA 进行中", "bad");
    return;
  }
  const sel = state.devices.find((d) => d.dev_id === state.selected);
  if (!sel || !sel.connected) {
    toast("设备未连接，无法 OTA", "bad");
    return;
  }
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".bin";
  input.onchange = async () => {
    const file = input.files[0];
    if (!file) return;
    const name = file.name.toLowerCase();
    // Front-end half of the app-only check; the server repeats it before the
    // push and the firmware validates the image header again.
    if (!name.endsWith(".bin") || !name.startsWith("ota_")) {
      toast("请选择 app-only 固件（ota_*.bin），不要用 merged 整片包", "bad");
      return;
    }
    state.otaFile = file;
    const otaObj = {
      status: "idle",
      sent: 0,
      total: file.size,
      percent: 0,
      error: "",
      filename: file.name,
      sha256: "",
    };
    state.ota = otaObj;
    renderOta();
    // Compute the local SHA256 so the user can compare it against the release
    // page (WS-279) before pushing — a mismatch here is exactly the "device
    // verify failed" root cause we now guard against.
    if (file.arrayBuffer && crypto && crypto.subtle) {
      file.arrayBuffer().then((buf) => crypto.subtle.digest("SHA-256", buf))
        .then((h) => {
          const hex = [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("");
          otaObj.sha256 = hex;
          if (state.ota === otaObj) state.ota.sha256 = hex;
          renderOta();
        })
        .catch(() => {});
    }
  };
  input.click();
}

async function startOta() {
  const sel = state.devices.find((d) => d.dev_id === state.selected);
  if (!sel || !sel.connected) {
    toast("设备未连接，无法 OTA", "bad");
    return;
  }
  if (!state.otaFile) {
    toast("请选择固件文件", "bad");
    return;
  }
  const form = new FormData();
  form.append("file", state.otaFile);
  try {
    const res = await fetch(`/api/ota/${encodeURIComponent(state.selected)}`, {
      method: "POST",
      body: form,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      toast(data.error || "OTA 启动失败", "bad");
      state.ota = null;
      state.otaFile = null;
      renderOta();
      return;
    }
    state.ota = {
      status: "starting",
      sent: 0,
      total: data.total,
      percent: 0,
      error: "",
      filename: data.filename,
      sha256: data.sha256 || (state.ota && state.ota.sha256) || "",
    };
    renderOta();
  } catch (e) {
    toast("OTA 启动失败: " + (e.message || e), "bad");
    state.ota = null;
    state.otaFile = null;
    renderOta();
  }
}

async function cancelOta() {
  if (state.ota && ["done", "error", "cancelled"].includes(state.ota.status)) {
    state.ota = null;
    state.otaFile = null;
    renderOta();
    return;
  }
  // In-progress cancel: ask the server to stop the push and tell the device
  // to abort (CMD:OTA_CANCEL), so the device leaves OTA mode immediately
  // instead of waiting for its 30 s inter-chunk timeout.
  if (state.ota && state.selected && otaBusy()) {
    try {
      const r = await api(`/api/ota/${encodeURIComponent(state.selected)}/cancel`, { method: "POST" });
      if (r.sent === false) {
        toast("已取消；取消指令未送达，设备将在 30 秒后自动中止", "bad");
      } else {
        toast("已取消升级，设备保持原固件", "ok");
      }
    } catch (e) {
      toast("取消失败：" + (e.message || e), "bad");
    }
  }
  state.ota = null;
  state.otaFile = null;
  renderOta();
}

init();
