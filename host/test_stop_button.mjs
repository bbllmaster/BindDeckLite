// WS-498 「停止」按钮状态的前端回归用例。
//
// 运行：node --test host/test_stop_button.mjs
//
// 背景：服务端在 claim 还活着时回 already_done，前端就把卡片切回空闲态
// （「测试动作」），1.2 秒后的轮询又把它翻回「停止」—— 用户点完停止只看到
// 一个 toast，按钮却还在那里来回跳。
//
// WS-498 的**行为修复在服务端**：claim 还活着就回 cancelling，前端据此保持
// 「正在停止…」。这份用例钉住前端依赖的那份契约（cancelling → 保持
// 「正在停止…」，already_done → 回空闲态，轮询确认退出 → 回空闲态），
// 改动生产代码会让断言失败。
//
// 这里加载**生产脚本** host/static/app.js（以及它依赖的 keys.js /
// log_follow.js），在假 DOM 上真跑 stopAction / applyRunningState /
// setCardRunning，而不是把判定逻辑复制到测试里。
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
    innerHTML: "",
    title: "",
    value: "",
    checked: false,
    hidden: false,
    disabled: false,
    children: [],
    addEventListener() {},
    removeEventListener() {},
    appendChild(c) { this.children.push(c); return c; },
    insertBefore(c, ref) {
      const i = ref ? this.children.indexOf(ref) : -1;
      if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
      return c;
    },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      return c;
    },
    get options() { return this.children.filter((c) => c.tagName === "OPTION"); },
    remove() {},
    setAttribute() {},
    removeAttribute() {},
    getAttribute() { return null; },
    focus() {},
    insertAdjacentHTML() {},
    closest() { return null; },
    querySelector(s) { return makeEl(s); },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, bottom: 0, height: 0, width: 0 }; },
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
  };
  return el;
}

/**
 * 载入生产脚本，返回 { host, card, btn, server }。
 *
 * ``server`` 是假的服务端：``cancel`` 回 POST /api/action/cancel 的答案，
 * ``status`` 回 GET /api/status 的答案，测试可以随时改。
 */
function loadApp() {
  const registry = {};
  const body = makeEl("body");
  const document = {
    body,
    documentElement: makeEl("html"),
    querySelector(sel) { return registry[sel] ? registry[sel][0] : makeEl(sel); },
    querySelectorAll(sel) { return registry[sel] || []; },
    getElementById(id) { return registry["#" + id] ? registry["#" + id][0] : makeEl("#" + id); },
    createElement(sel) { return makeEl(sel); },
    createTextNode(t) { return { textContent: t }; },
    addEventListener() {},
    removeEventListener() {},
  };
  const localStorage = {
    _m: new Map(),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
    removeItem(k) { this._m.delete(k); },
  };

  const sandbox = {
    document, localStorage, console,
    // init() 会起轮询定时器：这里让它们不注册，脚本跑完就退出。
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
  // log_follow.js / log_device.js 是 UMD（app.js 顶层要读 window.LogFollow，
  // 轮询追加日志要读 window.LogDevice）；
  // keys.js 提供 bindKeyDrawer / syncKeyDrawerRunning（app.js 的 init() 会调用）。
  // 生产 i18n + 生产 zh-CN 文案表（默认语言），断言仍是生产函数的输出。
  run("i18n.js");
  sandbox.i18n.setMessages(
    JSON.parse(readFileSync(new URL("../locales/zh-CN.json", STATIC_DIR), "utf8")), "zh-CN");
  run("log_follow.js");
  run("log_device.js");
  run("keys.js");

  // app.js 的顶层绑定是词法作用域，追加一段把它暴露出来（不改生产代码）。
  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__host = {
  state, stopAction, applyRunningState, setCardRunning,
  markRunning, clearRunning, markStopping, clearStopping, isStopping,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  assert.ok(host, "app.js 没有导出宿主函数");

  // 一张按键卡片 + 它的「测试」按钮
  const btn = makeEl(".test");
  btn.textContent = "测试动作";
  const card = makeEl(".keycard");
  card.dataset.idx = "3";
  card.querySelector = (s) => (s === ".test" ? btn : makeEl(s));
  card.querySelectorAll = () => [];
  registry[".keycard"] = [card];

  const server = {
    cancel: { ok: true, cancelling: true },
    status: { devices: [], running: { t1: { "3": 1.2 } }, actions_enabled: true },
  };
  sandbox.api = async (path) => {
    const p = String(path);
    if (p.startsWith("/api/action/cancel/")) return server.cancel;
    if (p === "/api/status") return server.status;
    return {};
  };

  host.state.selected = "t1";
  host.state.running = { t1: { "3": 1.2 } };
  host.state.stopping = {};
  host.applyRunningState();
  return { host, card, btn, server };
}

/* ---------------- 用例 ---------------- */

test("服务端回 cancelling：按钮保持「正在停止…」", async () => {
  const { host, btn, server } = loadApp();
  server.cancel = { ok: true, cancelling: true };

  await host.stopAction("t1", 3);

  assert.equal(btn.textContent, "正在停止…");
});

test("刷新后再点停止（claim 还活着）：不回跳「停止」", async () => {
  const { host, btn, server } = loadApp();
  // 刷新页面后：本地 stopping 标记没了，但服务端那边宏还在收尾。
  host.state.running = { t1: { "3": 3.4 } };
  host.state.stopping = {};
  host.applyRunningState();
  assert.equal(btn.textContent, "停止");

  server.cancel = { ok: true, cancelling: true };
  await host.stopAction("t1", 3);

  // 关键断言：不能翻回「停止」，也不能掉回「测试动作」。
  assert.equal(btn.textContent, "正在停止…");
});

test("服务端确认没有 claim：回到「测试动作」", async () => {
  const { host, btn, server } = loadApp();
  host.state.running = { t1: { "3": 5.0 } };
  host.state.stopping = {};

  server.cancel = { ok: true, already_done: true };
  await host.stopAction("t1", 3);

  assert.equal(btn.textContent, "测试动作");
});

test("轮询确认宏已退出：回到「测试动作」", async () => {
  const { host, btn, server } = loadApp();
  server.cancel = { ok: true, cancelling: true };
  await host.stopAction("t1", 3);
  assert.equal(btn.textContent, "正在停止…");

  // 轮询不再报 running → 卡片必须回到空闲态，不能永远停在「正在停止…」。
  host.state.running = {};
  host.applyRunningState();

  assert.equal(btn.textContent, "测试动作");
});
