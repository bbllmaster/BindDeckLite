// WS-595 / WS-596：UI 重构遗留三项的回归用例。
//
// 运行：cd host && node --test test_ui_leftovers.mjs
//
// 三项都断言**生产代码**，不复制实现：
//   1. 切语言不再整块重建检查器（retranslateDynamic 走 keys.js 的
//      retranslateKeyDrawer，只换文案）；
//   2. buildLogRow 的 time / kind 与 rowText 一样转义，`<img onerror>` 只会
//      变成文本；复制文本（LogDevice.logCopyText）保持原样；
//   3. 「先忽略」只写会话状态，不动 cfg.trusted —— 用 CfgStatus 的纯函数
//      shouldShowPending / derive 与本文件里真跑的点击处理器一起钉住。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import CfgStatus from "./static/cfg_status.js";
import LogDevice from "./static/log_device.js";

const { STATES, shouldShowPending, derive } = CfgStatus;

const HOST_DIR = new URL("./", import.meta.url);
const STATIC_DIR = new URL("./static/", import.meta.url);
const normalizeEol = (text) => text.replace(/\r\n/g, "\n");
const readText = (rel) => normalizeEol(readFileSync(new URL(rel, HOST_DIR), "utf8"));

/* ---------------- 最小 DOM 桩（与 test_ui_p1_4.mjs 同形） ---------------- */

function makeEl(sel = "") {
  const classes = new Set();
  const el = {
    _sel: sel,
    tagName: String(sel || "div").toUpperCase(),
    dataset: {}, style: {}, children: [], attributes: {}, _q: {}, _qa: {},
    textContent: "", _html: "", title: "", value: "", checked: false,
    hidden: false, disabled: false, parentNode: null,
    classList: {
      _set: classes,
      add(...c) { c.forEach((x) => this._set.add(x)); },
      remove(...c) { c.forEach((x) => this._set.delete(x)); },
      toggle(c, on) {
        if (on === undefined) this._set.has(c) ? this._set.delete(c) : this._set.add(c);
        else if (on) this._set.add(c); else this._set.delete(c);
      },
      contains(c) { return this._set.has(c); },
    },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    removeAttribute(k) { delete this.attributes[k]; },
    getAttribute(k) { return this.attributes[k] ?? null; },
    focus() {}, insertAdjacentHTML() {}, closest() { return null; },
    querySelector(s) { return this._q[s] || null; },
    querySelectorAll(s) { return this._qa[s] || []; },
    getBoundingClientRect() { return { top: 0, bottom: 0, height: 0, width: 0 }; },
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
  };
  Object.defineProperty(el, "className", {
    get() { return [...el.classList._set].join(" "); },
    set(v) { el.classList._set = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  Object.defineProperty(el, "innerHTML", {
    get() { return el._html; },
    set(v) { el._html = String(v); },
  });
  return el;
}

/** 载入生产脚本，返回可断言的入口与 DOM registry。 */
function loadApp() {
  const registry = {};
  [
    "#logList", "#logEmpty", "#btnApply", "#cfgStatus", "#cfgStatusText",
    "#cfgStatusMsg", "#cfgStatusSub", "#cfgStatusExtra", "#cfgLastApplyText",
    "#cfgPopover", "#cfgPopoverClose", "#cfgPendingBlock", "#cfgPendingLabel",
    "#cfgPendingList", "#cfgUntrustedActions", "#btnUntrustedRead",
    "#btnUntrustedIgnore",
  ].forEach((sel) => { registry[sel] = [makeEl(sel)]; });

  const document = {
    body: makeEl("body"),
    documentElement: makeEl("html"),
    querySelector(sel) { return registry[sel] ? registry[sel][0] : makeEl(sel); },
    querySelectorAll(sel) { return registry[sel] || []; },
    getElementById(id) {
      return registry["#" + id] ? registry["#" + id][0] : makeEl("#" + id);
    },
    createElement(tag) { return makeEl(tag); },
    createTextNode(text) { return { textContent: text }; },
    addEventListener() {}, removeEventListener() {},
  };
  const sandbox = {
    document,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    console,
    setTimeout: () => 0, clearTimeout() {},
    setInterval: () => 0, clearInterval() {},
    fetch: async () => { throw new Error("no network in the harness"); },
    navigator: { userAgent: "node" },
    location: { href: "http://127.0.0.1/", search: "" },
    alert() {}, confirm() { return false; },
    addEventListener() {}, removeEventListener() {},
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
  run("reconnect_sync.js");
  run("cfg_status.js");
  run("keys.js");

  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8")
    // app.js 末尾会自启 init()；桩里没有后端，请求必然失败。吞掉这个
    // 只属于测试环境的 rejection，避免它挂在测试结束后变成未处理异常。
    .replace(/\ninit\(\);\s*$/, "\ninit().catch(() => {});\n");
  vm.runInContext(appSrc + `
globalThis.__host = {
  state, buildLogRow, retranslateDynamic, retranslateKeyDrawer,
  bindCfgStatus, cfgStatusView, updatePendingUi, resetReconnectCycle, selectDevice,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  assert.ok(host, "app.js 没有导出 UI 遗留项入口");
  const el = (sel) => registry[sel][0];
  return { host, el, sandbox, document };
}

/* ---------------- 1. 切语言不重建检查器 ---------------- */

test("retranslateDynamic 走只换文案的 retranslateKeyDrawer，不整块重绘抽屉", () => {
  const { host, sandbox } = loadApp();

  // keys.js 里的生产入口存在且不是重绘函数。
  assert.equal(typeof sandbox.retranslateKeyDrawer, "function",
    "keys.js 必须提供 retranslateKeyDrawer（只换文案）");
  assert.notEqual(sandbox.retranslateKeyDrawer, sandbox.renderKeyDrawer,
    "切语言不能复用整块重绘");
  assert.equal(typeof sandbox.renderKeyDrawer, "function", "重绘函数本身仍在（打开 / 换动作时用）");

  // 切语言路径的源码：调 retranslateKeyDrawer、不调 renderKeyDrawer。
  // 一旦有人把这里改回整块重绘，断言立刻变红。
  const src = host.retranslateDynamic.toString();
  assert.match(src, /\bretranslateKeyDrawer\s*\(/,
    "retranslateDynamic 必须调用 retranslateKeyDrawer");
  assert.doesNotMatch(src, /\brenderKeyDrawer\s*\(/,
    "retranslateDynamic 不能再直接调 renderKeyDrawer —— 会丢未保存的宏草稿");
});

test("retranslateDynamic 覆盖四个页面的文案刷新（不止抽屉）", () => {
  const { host } = loadApp();
  const src = host.retranslateDynamic.toString();
  // 按键 / 编码器 / 设备（引脚 + 元信息）/ 日志页各自的重绘入口。
  ["renderDevices", "renderKeys", "renderEncoder", "renderPins",
    "renderDeviceMeta", "retranslateLog", "updatePendingUi"].forEach((fn) => {
    assert.ok(src.includes(fn), `切语言漏了 ${fn}（该页会停在旧语言）`);
  });
});

/* ---------------- 2. 日志字段转义 ---------------- */

const XSS = '<img src=x onerror=alert(1)>';

test("buildLogRow 的 time / kind 与正文一样转义：不产生元素", () => {
  const { host } = loadApp();
  const row = host.buildLogRow({
    time: XSS, kind: XSS, level: "info", text: "ok", dev: "",
    code: null, params: {},
  }, false);

  assert.doesNotMatch(row.innerHTML, /<img/i, "time / kind 里拼出了真实标签");
  assert.match(row.innerHTML, /&lt;img/, "应转义成文本而不是被丢掉");
  assert.ok(row._text === "ok", "行文本仍供 toast / 复制使用");
  // 正文本来就走 escapeHtml，这里一并钉住不被顺手改回去。
  assert.match(row.innerHTML, /class="log-text">ok</);
});

test("日志复制文本不经 HTML：原文保持不变", () => {
  const text = LogDevice.logCopyText({
    time: XSS, kind: XSS, text: "ok", dev: "",
  }, []);
  assert.equal(text, `${XSS} [${XSS}] ok`);
  assert.doesNotMatch(text, /&lt;/, "复制路径不该出现 HTML 实体");
});

/* ---------------- 3. 「先忽略」只写会话状态 ---------------- */

test("shouldShowPending：只有「无存档且未忽略」才呈现提示", () => {
  assert.equal(shouldShowPending({ trusted: false }, false), true);
  assert.equal(shouldShowPending({ trusted: false }, undefined), true);
  assert.equal(shouldShowPending({ trusted: false }, true), false, "忽略后提示收起");
  assert.equal(shouldShowPending({ trusted: true }, false), false);
  assert.equal(shouldShowPending({}, false), false, "旧 config 无 trusted 字段按可信");
  assert.equal(shouldShowPending(null, false), false);
});

test("shouldShowPending 只读：调用不会改写 trusted", () => {
  const cfg = { trusted: false, keys: [] };
  shouldShowPending(cfg, true);
  assert.deepEqual({ ...cfg }, { trusted: false, keys: [] });
});

test("忽略后不谎称「已同步」：无改动时徽标收起，有改动仍按 pending 提示", () => {
  const base = { selected: "d1", preview: false, trusted: false, ignored: true };
  assert.equal(derive({ ...base, pendingCount: 0 }).state, STATES.NONE);
  assert.equal(derive({ ...base, pendingCount: 2 }).state, STATES.PENDING);
  // 未忽略时优先级不变：untrusted 压过 pending。
  assert.equal(derive({ ...base, ignored: false, pendingCount: 2 }).state, STATES.UNTRUSTED);
});

test("点「先忽略」只写 state.pendingIgnored，cfg.trusted 原样不动", () => {
  const { host, el } = loadApp();
  host.state.selected = "d1";
  host.state.preview = false;
  host.state.cfg = { trusted: false, keys: [], pins: [], sleep: {}, encoder: { mode: 0 } };

  assert.equal(host.cfgStatusView().state, STATES.UNTRUSTED, "点击前提示成立");
  host.bindCfgStatus();          // init() 里绑的是同一份生产处理器
  el("#btnUntrustedIgnore").onclick();

  assert.equal(host.state.pendingIgnored.d1, true, "忽略状态记在会话状态里");
  assert.equal(host.state.cfg.trusted, false, "忽略不改 trusted —— 下发确认仍在");
  assert.equal(host.cfgStatusView().state, STATES.NONE, "提示收起（徽标隐藏）");
  assert.equal(el("#cfgStatus").hidden, true);
});

test("重新选中 / 断开重连后忽略状态清空，提示重新出现", () => {
  const { host } = loadApp();
  host.state.selected = "d1";
  host.state.preview = false;
  host.state.cfg = { trusted: false, keys: [], pins: [], sleep: {}, encoder: { mode: 0 } };
  host.state.pendingIgnored.d1 = true;

  // 断开：resetReconnectCycle 是状态轮询里每台设备断开时都走的路径。
  host.resetReconnectCycle("d1");
  assert.equal(host.state.pendingIgnored.d1, undefined, "断开后不再忽略");
  assert.equal(host.cfgStatusView().state, STATES.UNTRUSTED, "重连后提示重新出现");

  // 重新选中同一台设备也要清空：早退分支里在选设备前就 delete 了。
  // （renderDevices 需要完整设备列表 DOM，这里只钉住生产源码里的清理动作。）
  assert.match(host.selectDevice.toString(), /delete state\.pendingIgnored\[devId\]/,
    "selectDevice 必须清掉这台设备的忽略标记");
});

test("「先忽略」按钮与文案在两套 locale 都在", () => {
  const zh = JSON.parse(readText("locales/zh-CN.json"));
  const en = JSON.parse(readText("locales/en-US.json"));
  assert.equal(typeof zh["ui.untrusted_ignore"], "string");
  assert.equal(typeof en["ui.untrusted_ignore"], "string");
  assert.match(readText("templates/index.html"), /id="btnUntrustedIgnore"/,
    "popover 里必须有「先忽略」按钮");
});
