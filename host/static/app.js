/* BindDeckLite host UI — vanilla JS, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const ENCODER_MODES = [
  { id: 0, name: "音量", desc: "媒体音量加 / 减" },
  { id: 1, name: "缩放", desc: "Ctrl + / Ctrl -" },
  { id: 2, name: "标签页", desc: "Ctrl + Tab / Ctrl + Shift + Tab" },
  { id: 3, name: "撤销 / 重做", desc: "Ctrl + Z / Ctrl + Y" },
  { id: 4, name: "应用音量", desc: "F23 / F24，交给上位机宏处理" },
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

const state = {
  devices: [],
  selected: null,
  selectedConnected: false,
  cfg: null,
  keycodes: [],
  logSeq: 0,
  dirty: false,
  saveTimer: null,
  preview: false,
  desktop: null,
  ota: null,           // {status, sent, total, percent, error, filename}
  otaFile: null,       // File object chosen by the user
  running: {},         // dev_id -> {"<idx>": 已运行秒数}，来源 /api/status
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
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
    if (state.selectedConnected && !nowConnected) clearApplyResults();
    state.selectedConnected = nowConnected;
    const connected = state.devices.filter((d) => d.connected).length;
    if (sel) {
      $("#statusDot").className = "dot " + (sel.connected ? "ok" : "warn");
      $("#statusText").textContent = sel.connected ? "已连接" : "未连接";
      const fw = sel.fw && sel.fw.version ? `固件 ${sel.fw.version} · ${sel.fw.chip || ""}` : "等待设备信息";
      $("#statusSub").textContent = `${sel.name} · ${fw}`;
    } else {
      $("#statusDot").className = "dot bad";
      $("#statusText").textContent = "未发现设备";
      $("#statusSub").textContent = state.preview ? "显示默认配置（预览）" : "请给设备上电";
    }
    highlightKeys(s.last_press || {});
    applyRunningState();
  } catch (e) {
    $("#statusDot").className = "dot bad";
    $("#statusText").textContent = "上位机未就绪";
    $("#statusSub").textContent = String(e.message || e);
  }
}

function renderDevices() {
  const ul = $("#deviceList");
  ul.innerHTML = "";
  if (!state.devices.length) {
    const li = document.createElement("li");
    li.className = "device-empty";
    li.textContent = "未发现 BindDeckLite 设备";
    ul.appendChild(li);
    return;
  }
  state.devices.forEach((d) => {
    const li = document.createElement("li");
    li.className = "device" + (d.dev_id === state.selected ? " active" : "");
    li.innerHTML = `
      <span class="dot ${d.connected ? "ok" : ""}"></span>
      <span class="dev-main">
        <b>${escapeHtml(d.name || d.dev_id || "BindDeckLite")}</b>
        <small>${escapeHtml(d.address || "")}</small>
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
      if (card.classList.contains("running")) {
        // 按钮此时本应 disabled，兜底防止键盘触发第二次宏。
        toast("该按键的动作仍在执行", "warn");
        return;
      }
      const devId = state.selected;
      const noAction = !item.action || (item.action.type || "none") === "none";
      // /api/action/test 最多阻塞 5 秒才返回；先本地点亮执行中态，
      // 否则点击后到下一次轮询之间用户看不到任何反馈。
      if (devId && !noAction) {
        markRunning(devId, i);
        applyRunningState();
      }
      let r = null;
      try {
        r = await api(`/api/action/test/${encodeURIComponent(devId)}/${i}`, { method: "POST" });
        if (r.ok) {
          // Show which browser a macro actually used, so a silent fallback to
          // the system default is visible instead of surprising.
          const used = r.browser_used && r.browser_used !== "default" ? ` · ${r.browser_used}` : "";
          toast("动作执行成功：" + (item.action.type === "none" ? "无上位机动作" : item.action.type) + used, "ok");
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
        if (!(r && r.running)) {
          if (devId) clearRunning(devId, i);
          applyRunningState();
        }
      }
    };
  }
  applyRunningState();
}

/* ---------- 执行中态（WS-359） ----------
 * 服务端 /api/status 的 running 字段是唯一权威来源；这里只在点击测试的
 * 5 秒等待窗口内先本地标记，随后由轮询结果覆盖。 */

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

function setCardRunning(card, secs) {
  const live = secs !== undefined && secs !== null;
  card.classList.toggle("running", live);
  const btn = $(".test", card);
  if (!btn) return;
  btn.disabled = live;
  btn.textContent = live ? "执行中…" : "测试动作";
  btn.title = live
    ? `动作仍在执行（已运行 ${Number(secs).toFixed(1)} 秒），重复按下会被忽略`
    : TEST_BTN_IDLE_TITLE;
}

function applyRunningState() {
  // 预览 / 无选中设备时没有任何运行项；status 缺少该项即按未运行渲染。
  const running = state.preview || !state.selected ? {} : runningFor(state.selected);
  $$(".keycard").forEach((card) => {
    const idx = String(parseInt(card.dataset.idx, 10));
    setCardRunning(card, running[idx]);
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
      "每行一步：browser: / url: / wait:（毫秒）/ text: / text!80: / paste: / key:\n" +
      "例：browser:chrome → url:https://example.com/login → paste:密码";
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
}

/* ---------------- device ---------------- */

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
    bondEl.innerHTML = `<span class="dot ok"></span>已绑定 <b>${escapeHtml(bond.addr || "—")}</b>`;
    clearBtn.hidden = false;
    $("#bondHint").textContent = "只有已绑定的主机能发送 HID 按键；清除后任何主机均可接入。";
  } else {
    bondEl.innerHTML = `<span class="dot warn"></span>未绑定`;
    clearBtn.hidden = true;
    $("#bondHint").textContent = "首次 HID 订阅的主机会被绑定；按住编码器 5 秒也可清除。";
  }

  const hint = chip === "C3" || chip === "ESP32C3"
    ? "C3 预设：按键 GPIO0-8、编码器按压 GPIO10、编码器 A/B=GPIO20/21；避开 11-17（Flash）与 18/19（USB）。"
    : "经典 ESP32：避开 6-11（Flash）与 34-39（仅输入）。编码器固定 18/19/5。";
  $("#pinHint").textContent = hint;
}

function renderPins() {
  const box = $("#pinGrid");
  box.innerHTML = "";
  if (!state.cfg) return;
  const pins = state.cfg.pins || [];
  for (let i = 0; i < 9; i++) {
    const wrap = document.createElement("label");
    wrap.className = "pin";
    wrap.innerHTML = `<span>按键 ${i + 1}</span>`;
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0";
    input.max = "39";
    input.value = pins[i] ?? "";
    input.oninput = () => {
      state.cfg.pins[i] = parseInt(input.value, 10);
      markDirty();
    };
    wrap.appendChild(input);
    box.appendChild(wrap);
  }
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
  const toggle = $("#autostartToggle");
  toggle.checked = !!auto.enabled;
  toggle.disabled = !auto.supported;
  $("#minimizedToggle").checked = !!d.start_minimized;
  // close_button_behavior: "minimize" (default) → checked, "exit" → unchecked
  $("#closeMinimizeToggle").checked = (d.close_button_behavior || "minimize") === "minimize";
  $("#btnQuitHost").hidden = !d.desktop_mode;

  const bits = [];
  if (d.tray_active) bits.push("托盘图标在任务栏右下角：单击显示控制台，右键可显示 / 退出");
  else if (d.desktop_mode) bits.push("托盘不可用，程序在后台运行，可用下方「退出上位机」关闭");
  else bits.push("当前由 run.sh / python app.py 启动，没有托盘图标；打包版双击即带托盘");
  if (auto.supported) {
    bits.push(`开机自启动${auto.enabled ? "已开启" : "未开启"}（${auto.method}）`);
  } else {
    bits.push("开机自启动不可用");
  }
  if (auto.error) bits.push(auto.error);
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
  clearTimeout(state.saveTimer);
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
      ? `<button class="btn tiny" onclick="acceptDeviceValue('${item.field}')">以设备为准</button>`
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

async function acceptDeviceValue(field) {
  if (!state.selected) return;
  try {
    // Trigger a fresh read from the device so the local form reflects what
    // the device actually has. The CFG_* lines that come back update the
    // local config through the normal _on_line path.
    await api(`/api/device/${encodeURIComponent(state.selected)}/refresh`, { method: "POST" });
    toast("已请求设备配置，更新中…", "ok");
    // Give the device a moment to respond, then re-render the affected parts.
    setTimeout(() => {
      if (state.cfg) {
        renderKeys();
        renderEncoder();
        renderPins();
        renderDeviceMeta();
      }
      clearApplyResults();
    }, 600);
  } catch (e) {
    toast("刷新失败：" + (e.message || e), "bad");
  }
}

/* ---------------- log ---------------- */

async function pollLog() {
  try {
    const r = await api(`/api/log?since=${state.logSeq}`);
    state.logSeq = r.seq;
    if (!r.events.length) return;
    const list = $("#logList");
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
      copyBtn.title = "复制这一行日志";
      copyBtn.onclick = () => copyLogLine(`${e.time} [${e.kind}] ${e.text}`, copyBtn);
      row.appendChild(copyBtn);

      list.appendChild(row);
      // Physical-button action failures arrive through the log stream;
      // surface them as a toast so the user sees them immediately.
      if (e.kind === "action" && e.level === "error") {
        toast(e.text, "bad");
      }
    });
    while (list.children.length > 300) list.removeChild(list.firstChild);
    list.scrollTop = list.scrollHeight;
  } catch (_) {
    /* ignore */
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
      if (tab.dataset.tab === "device") renderDeviceMeta();
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
      toast(`扫描到 ${r.found ? r.found.length : 0} 台设备`);
    } catch (e) {
      toast("扫描失败：" + e.message, "bad");
    }
  };
  $("#btnApply").onclick = applyConfig;
  $("#btnClearLog").onclick = () => {
    $("#logList").innerHTML = "";
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
  $("#btnRefresh").onclick = () => devAction("refresh", "已请求设备配置");
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
    $("#otaFileLine").textContent = "";
    $("#otaProgressBar").style.width = "0%";
    $("#btnOtaStart").hidden = true;
    $("#btnOtaCancel").hidden = false;
    $("#btnOtaCancel").textContent = "关闭";
    $("#otaHint").textContent = ota.error || "未知错误";
  } else if (ota.status === "uploading") {
    $("#otaStatusText").textContent = "上传中";
    $("#otaFileLine").textContent = `${escapeHtml(ota.filename || "")} · ${ota.total} bytes`;
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
    $("#otaFileLine").textContent = `${escapeHtml(ota.filename || "")} · ${ota.total} bytes`;
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
    $("#otaFileLine").textContent = `${escapeHtml(ota.filename || "")} · ${ota.total} bytes`;
    $("#otaProgressBar").style.width = "0%";
    $("#btnOtaStart").hidden = false;
    $("#btnOtaCancel").hidden = false;
    $("#btnOtaCancel").textContent = "取消";
    $("#otaHint").textContent = connected
      ? "点击「开始升级」推送固件（分块传输，约需 1-3 分钟）"
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
  ["btnApply", "btnOta", "btnPing", "btnRefresh", "btnPortal", "btnReboot", "btnClearBond"]
    .forEach((id) => {
      const el = $("#" + id);
      if (el) el.disabled = busy;
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
    state.ota = {
      status: "idle",
      sent: 0,
      total: file.size,
      percent: 0,
      error: "",
      filename: file.name,
    };
    renderOta();
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
