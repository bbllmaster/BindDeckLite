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

const state = {
  devices: [],
  selected: null,
  cfg: null,
  keycodes: [],
  logSeq: 0,
  dirty: false,
  saveTimer: null,
  preview: false,
  desktop: null,
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
      try {
        const r = await api(`/api/action/test/${encodeURIComponent(state.selected)}/${i}`, { method: "POST" });
        if (r.ok) {
          toast("动作执行成功：" + (item.action.type === "none" ? "无上位机动作" : item.action.type), "ok");
        } else {
          toast("动作执行失败：" + (r.error || "未知错误"), "bad");
        }
      } catch (e) {
        toast(e.message, "bad");
      }
    };
  }
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
    ta.placeholder = "key:ctrl+alt+t\nwait:200\ntext:Hello";
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
      if (s.type === "text") return "text:" + (s.value || "");
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
      const kind = line.slice(0, i).trim().toLowerCase();
      const value = line.slice(i + 1).trim();
      if (kind === "wait" || kind === "delay") return { type: "delay", value: parseInt(value, 10) || 0 };
      if (kind === "text") return { type: "text", value };
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
        toast("部分配置未生效，请查看下方结果", "bad");
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

function clearApplyResults() {
  const existing = $("#applyResults");
  if (existing) existing.remove();
  $$(".field-highlight").forEach((el) => el.classList.remove("field-highlight"));
}

function renderApplyResults(items) {
  clearApplyResults();

  const container = document.createElement("div");
  container.id = "applyResults";
  container.className = "apply-results";

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

  // Insert right below the apply button.
  const btn = $("#btnApply");
  btn.parentNode.insertBefore(container, btn.nextSibling);

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

async function init() {
  bindTabs();
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

  const initialTab = new URLSearchParams(location.search).get("tab");
  if (initialTab) {
    const tab = document.querySelector(`.tab[data-tab="${initialTab}"]`);
    if (tab) tab.click();
  }

  await refreshDesktop();
  await pollStatus();
  setInterval(pollStatus, 1200);
  setInterval(pollLog, 800);
}

init();
