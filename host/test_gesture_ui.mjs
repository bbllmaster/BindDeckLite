// WS-679（WS-672 的 Stage 1 重做）：「双击进配网」开关的首屏能力探测回归用例。
//
// 运行：cd host && node --test test_gesture_ui.mjs
//
// 背景（WS-677 的 S1）：设备页打开时只探一次 `POST /api/device/<id>/gesture`。
// 1.3.13 旧固件第一次探测必然超时（后端 `{'supported': None, 'reason':
// 'probe_timeout'}`），而单次丢包按 WS-550 不是结论，于是 `state.cfg.
// gesture_supported` 停在 null —— 开关仍是可用态，验收「旧固件打开设备页即
// 置灰」不成立。修复后 `r.sent && r.supported === null` 会补探一次，第二次
// 沉默才判不支持（后端 GESTURE_UNSUPPORTED_AFTER = 2）。
//
// 本文件在假 DOM 上加载**生产** static/app.js，直接调它的
// ensureGestureCapability()，断言的是真实状态写回与真实渲染结果
// （#layoutGesture.disabled / #gestureHint 文案），没有在这里复制一份探测
// 逻辑：把 app.js 的补探删掉，第一个用例就会变红。
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

/** 生产配置形状：编码器布局、手势开关默认开、能力尚未定论（后端 new_device_cfg）。 */
function makeCfg() {
  return {
    name: "t1",
    chip: "C3",
    trusted: true,
    layout: 1,                       // LAYOUT_ENCODER
    layout_supported: true,          // 跳过 12 键布局的旧固件分支
    pins: [0, 1, 2, 3, 4, 5, 6, 7, 8],
    keys: [],
    encoder: { mode: 0, inverted: false, actions: { CW: { type: "none" }, CCW: { type: "none" } } },
    sleep: { enabled: true, minutes: 5 },
    tz: "",
    gesture_enabled: true,
    gesture_supported: null,
  };
}

async function loadApp() {
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
    "#layoutEncoder", "#layout12Key", "#layoutHint", "#layoutGesture", "#gestureHint",
    "#btnApply", "#cfgStatus", "#cfgPopover", "#cfgStatusText", "#logList", "#logEmpty",
  ];
  pre.forEach((sel) => { registry[sel] = [makeEl(sel)]; });
  registry["#cfgPopover"][0].hidden = true;

  const sandbox = {
    document,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    console,
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
  state, ensureGestureCapability, renderLayoutCard,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  assert.ok(host, "app.js 没有导出宿主函数");

  // init() 在加载时就发了请求（harness 里全部失败）；等它的拒绝链跑完，
  // 再装假后端，否则它迟到的 selectDevice 会混进探测调用计数。
  await new Promise((resolve) => setImmediate(resolve));

  host.state.selected = "t1";
  host.state.preview = false;
  host.state.devices = [{ dev_id: "t1", connected: true }];
  host.state.cfg = makeCfg();
  host.state.reconnectDirty = {};
  host.state.readStatus = {};
  host.state.pendingIgnored = {};
  host.state.reconnectSynced = {};
  host.state.connSince = {};

  return { host, registry, sandbox };
}

/** 把 POST /api/device/t1/gesture 换成按脚本作答的假后端，并记录调用次数。 */
function stubProbe(sandbox, replies) {
  const calls = [];
  sandbox.api = async (url) => {
    calls.push(url);
    const next = replies.length > 1 ? replies.shift() : replies[0];
    return next;
  };
  return calls;
}

/* ---------------- 用例 ---------------- */

test("旧固件：首次探测超时后补探一次 → 判不支持、开关置灰、说明写「固件不支持」", async () => {
  const { host, registry, sandbox } = await loadApp();
  // 1.3.13：链路是通的，两次探测都沉默（后端第一次 None，第二次 False）。
  const calls = stubProbe(sandbox, [
    { sent: true, supported: null, reason: "probe_timeout" },
    { sent: true, supported: false, reason: "unsupported" },
  ]);

  const verdict = await host.ensureGestureCapability();

  assert.equal(calls.length, 2, "首屏第一次探测没结论时必须补探一次（WS-550：单次丢包不算结论）");
  assert.equal(verdict, false, "旧固件两次沉默 → 结论是不支持");
  assert.equal(host.state.cfg.gesture_supported, false, "结论必须写回 state.cfg");
  assert.equal(registry["#layoutGesture"][0].disabled, true,
    "旧固件打开设备页时开关必须置灰，不能停在 null 可用态");
  assert.equal(registry["#gestureHint"][0].textContent,
    sandbox.i18n.t("ui.gesture_unsupported"), "置灰时说明应为「固件不支持」");
});

test("新固件：第一次就答 GESTURE: → 只探一次、开关可用", async () => {
  const { host, registry, sandbox } = await loadApp();
  const calls = stubProbe(sandbox, [{ sent: true, supported: true, reason: "supported" }]);

  const verdict = await host.ensureGestureCapability();

  assert.equal(calls.length, 1, "有结论时不该多探");
  assert.equal(verdict, true);
  assert.equal(registry["#layoutGesture"][0].disabled, false);
  assert.equal(registry["#gestureHint"][0].textContent,
    sandbox.i18n.t("ui.layout_gesture_hint"), "可用时说明应为进入方式提示");
});

test("链路不通：一次都不补探，结论保持未知（不误判不支持）", async () => {
  const { host, registry, sandbox } = await loadApp();
  const calls = stubProbe(sandbox, [{ sent: false, supported: null, reason: "link_down" }]);

  const verdict = await host.ensureGestureCapability();

  assert.equal(calls.length, 1, "链路不通时补探没有意义，且会把 null 变成假结论");
  assert.equal(verdict, null);
  assert.equal(host.state.cfg.gesture_supported, null);
  assert.equal(registry["#layoutGesture"][0].disabled, false,
    "未定论不是不支持：链路恢复后应能继续用");
});
