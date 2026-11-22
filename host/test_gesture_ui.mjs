// WS-672「双击第 10 个输入进配网」开关的前端回归用例。
//
// 运行：cd host && node --test test_gesture_ui.mjs
//
// 背景：设备页「按键布局」卡新增一行开关，值来自 `cfg.gesture`（默认开），
// 能力来自 `cfg.gesture_supported`（true / false / null 三态）。这一行最容易
// 坏在：开关不跟随配置、旧固件没有置灰、切换不进 dirty（顶栏「应用配置」
// 不亮、回读会静默覆盖）、说明文案缺失。
//
// 这些用例在假 DOM 上加载**生产** static/app.js（及它依赖的脚本），调用真实
// `renderGestureRow()` 渲染出来的复选框，断言的是真实入口 state.dirty /
// collectPending() / #btnApply —— 没有在这里复制一份渲染或判定逻辑：把生产
// 里 gesture 那一行删掉，这些用例会真的变红。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const STATIC_DIR = new URL("./static/", import.meta.url);

/* ---------------- 最小 DOM 桩 ---------------- */

function makeEl(sel = "") {
  const el = {
    _sel: sel,
    tagName: String(sel || "div").toUpperCase(),
    dataset: {},
    style: {},
    classList: {
      _set: new Set(),
      add(...c) { c.forEach((x) => this._set.add(x)); },
      remove(...c) { c.forEach((x) => this._set.delete(x)); },
      toggle(c, on) {
        if (on === undefined) {
          this._set.has(c) ? this._set.delete(c) : this._set.add(c);
        } else if (on) this._set.add(c);
        else this._set.delete(c);
      },
      contains(c) { return this._set.has(c); },
    },
    textContent: "",
    _html: "",
    title: "",
    type: "",
    value: "",
    checked: false,
    hidden: false,
    disabled: false,
    attributes: {},
    children: [],
    onchange: null,
    onclick: null,
    addEventListener() {},
    removeEventListener() {},
    appendChild(c) { this.children.push(c); return c; },
    insertBefore(c) { this.children.push(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      return c;
    },
    querySelector(s) { return makeEl(s); },
    querySelectorAll() { return []; },
    get options() { return this.children.filter((c) => c.tagName === "OPTION"); },
    getAttribute(k) { return this.attributes[k] ?? null; },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    removeAttribute(k) { delete this.attributes[k]; },
    focus() {},
    insertAdjacentHTML() {},
    closest() { return null; },
    remove() {},
    getBoundingClientRect() { return { top: 0, bottom: 0, height: 0, width: 0 }; },
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return el._html; },
    set(v) { el._html = String(v); if (v === "") el.children.length = 0; },
  });
  Object.defineProperty(el, "className", {
    get() { return [...el.classList._set].join(" "); },
    set(v) { el.classList._set = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  return el;
}

/** 生产配置形状（后端 new_device_cfg）：gesture 默认 true，能力未探到 = undefined。 */
function makeCfg(overrides = {}) {
  return Object.assign({
    name: "t1",
    chip: "ESP32",
    trusted: true,
    layout: 1,
    pins: [13, 12, 14, 27, 32, 33, 25, 26, 4],
    keys: [],
    gesture: true,
    encoder: {
      mode: 0,
      inverted: false,
      actions: { CW: { type: "none" }, CCW: { type: "none" } },
    },
    sleep: { enabled: true, minutes: 5 },
  }, overrides);
}

/**
 * 在假 DOM 上加载生产脚本，返回装配好一台「已连接、已下发过一次」设备的 host。
 * b.applied 是已下发快照（gesture=true），关掉开关后应与 state.cfg 产生差异。
 */
function loadApp(cfg = makeCfg()) {
  const registry = {};
  const document = {
    body: makeEl("body"),
    documentElement: makeEl("html"),
    querySelector(sel) {
      if (!registry[sel]) registry[sel] = [makeEl(sel)];
      return registry[sel][0];
    },
    querySelectorAll(sel) { return registry[sel] || []; },
    getElementById(id) { return this.querySelector("#" + id); },
    createElement(sel) { return makeEl(sel); },
    createTextNode(t) { return { textContent: t }; },
    addEventListener() {},
    removeEventListener() {},
  };
  const pre = [
    "#btnApply", "#gestureToggle", "#gestureHint", "#gestureRow",
    "#cfgStatus", "#cfgPopover", "#cfgStatusText", "#cfgReadStatus",
    "#logList", "#logEmpty", "#encoderModes", "#encoderModeDesc",
    "#encoderActions", "#encoderDisabledNote", "#encoderDirSegments",
    "#encoderDirNote", "#noticeCenter",
  ];
  pre.forEach((sel) => { registry[sel] = [makeEl(sel)]; });
  registry["#cfgPopover"][0].hidden = true;
  registry["#encoderDisabledNote"][0].hidden = true;

  const localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  const toasts = [];
  const sandbox = {
    document, localStorage, console,
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    clearInterval() {},
    fetch: async () => { throw new Error("no network in the harness"); },
    navigator: { userAgent: "node" },
    location: { href: "http://127.0.0.1/" },
    alert() {},
    confirm() { return false; },
    addEventListener() {},
    removeEventListener() {},
    URLSearchParams,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  vm.createContext(sandbox);

  const run = (name) => vm.runInContext(
    readFileSync(new URL(name, STATIC_DIR), "utf8"), sandbox, { filename: name });
  run("i18n.js");
  sandbox.i18n.setMessages(
    JSON.parse(readFileSync(new URL("../locales/zh-CN.json", STATIC_DIR), "utf8")), "zh-CN");
  run("log_follow.js");
  run("log_device.js");
  run("log_export.js");
  run("reconnect_sync.js");
  run("cfg_status.js");
  run("keys.js");

  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__host = {
  state, collectPending, renderGestureRow, setGesture, markDirty, bucketFor,
  updatePendingUi, highlightFormField, t,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  assert.ok(host, "app.js 没有导出宿主函数");
  sandbox.api = async () => ({});

  host.state.selected = "t1";
  host.state.preview = false;
  host.state.devices = [{ dev_id: "t1", connected: true }];
  host.state.cfg = cfg;
  const b = host.bucketFor("t1");
  b.cfg = host.state.cfg;
  b.applied = JSON.parse(JSON.stringify(cfg));  // 基线：gesture=true
  host.state.reconnectDirty = {};
  host.state.readStatus = {};
  host.state.pendingIgnored = {};
  host.state.reconnectSynced = {};
  host.state.connSince = {};
  host.state.dirty = false;
  host.state.saveTimer = null;

  return { host, registry, toasts };
}

/* ---------------- 用例 ---------------- */

test("默认配置渲染为勾选、可用，并显示关闭后的进入方式说明", () => {
  const { host, registry } = loadApp();
  host.renderGestureRow();

  const toggle = registry["#gestureToggle"][0];
  assert.equal(toggle.checked, true, "默认必须勾选（与 1.3.13 行为一致）");
  assert.equal(toggle.disabled, false, "能力未知 / 支持时控件可用");
  const hint = registry["#gestureHint"][0].textContent;
  assert.ok(hint.length > 0, "必须有说明文案");
  const expected = host.t("ui.gesture_hint");
  assert.equal(hint, expected, "说明文案来自 i18n，不是另写一份");
  assert.ok(hint.includes("上电") && hint.includes("进入配网模式"),
    "说明必须写清关闭后仍可上电按住 / 用按钮进入：" + hint);
});

test("旧固件（gesture_supported=false）→ 置灰 + 「固件不支持」", () => {
  const { host, registry } = loadApp(makeCfg({ gesture: false, gesture_supported: false }));
  host.renderGestureRow();

  const toggle = registry["#gestureToggle"][0];
  assert.equal(toggle.disabled, true, "旧固件必须置灰，不能假装能关");
  assert.equal(toggle.checked, false, "仍如实显示设备当前值");
  const hint = registry["#gestureHint"][0].textContent;
  assert.equal(hint, host.t("ui.gesture_unsupported"));
  assert.ok(hint.includes("固件不支持"), "置灰原因必须点名「固件不支持」：" + hint);
});

test("关掉开关 → cfg.gesture=false、state.dirty=true、待下发含 gesture", () => {
  const { host, registry } = loadApp();
  host.renderGestureRow();
  const toggle = registry["#gestureToggle"][0];

  assert.equal(host.state.dirty, false, "开工前应无待下发项");
  assert.equal(host.collectPending().length, 0);

  toggle.checked = false;
  toggle.onchange();  // 点真实渲染出来的 handler

  assert.equal(host.state.cfg.gesture, false, "开关应写进 cfg.gesture");
  assert.equal(host.state.dirty, true, "开关没接入 dirty：顶栏不会亮、回读会覆盖本机值");
  const item = host.collectPending().find((i) => i.field === "gesture");
  assert.ok(item, "collectPending() 必须含 field:\"gesture\"");
  assert.equal(item.tab, "device");
  assert.equal(item.label, host.t("ui.gesture_label"));
  assert.ok(registry["#btnApply"][0].classList.contains("dirty"),
    "#btnApply 应带 dirty 类");
});

test("再打开 → 回到 0 项、dirty=false、#btnApply 无 dirty 类", () => {
  const { host, registry } = loadApp();
  host.renderGestureRow();
  const toggle = registry["#gestureToggle"][0];

  toggle.checked = false;
  toggle.onchange();
  assert.equal(host.state.dirty, true);

  toggle.checked = true;
  toggle.onchange();
  assert.equal(host.state.cfg.gesture, true);
  assert.equal(host.state.dirty, false, "切回默认后 dirty 应回落");
  assert.ok(!host.collectPending().some((i) => i.field === "gesture"));
  assert.ok(!registry["#btnApply"][0].classList.contains("dirty"));
});

test("置灰时点开关不改变本地值，并提示固件不支持", () => {
  const { host, registry } = loadApp(makeCfg({ gesture: false, gesture_supported: false }));
  host.renderGestureRow();
  const toggle = registry["#gestureToggle"][0];

  toggle.checked = true;   // 浏览器在 disabled 上不会触发，但直调 handler 也不能改值
  toggle.onchange();

  assert.equal(host.state.cfg.gesture, false, "被禁用的开关不能改本地配置");
  assert.equal(toggle.checked, false, "重绘后必须回到设备当前值");
  assert.equal(host.state.dirty, false, "不产生待下发项");
});

test("highlightFormField('gesture') 高亮开关本身（开关被设备拒绝时）", () => {
  const { host, registry } = loadApp();
  host.renderGestureRow();
  host.highlightFormField("gesture");
  assert.ok(registry["#gestureToggle"][0].classList.contains("field-highlight"),
    "开关被设备拒绝时，开关应高亮");
});
