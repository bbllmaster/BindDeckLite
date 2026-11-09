// WS-530: 固件在线更新的五段进度阶段名映射的前端回归用例。
//
// 运行：node --test host/test_update_progress.mjs
//
// 背景：后端的固件更新阶段名有两套 —— 任务阶段
// （starting/downloading/verifying/pushing）与推送阶段借用的 OTA 窗口阶段
// （push/restart/confirm）。前端 ``updateFwStep`` 的进度点顺序是
// download/verify/push/restart/confirm，两套名字对不上时 ``indexOf`` 全是
// -1：下载、校验阶段五个点全灭，``done`` 也不点亮（WS-523 Stage 2 评审）。
//
// 这里加载**生产脚本** host/static/app.js，在假 DOM 上真跑 updateFwStep，
// 而不是把映射表抄一份到测试里 —— 抄的那份改了生产代码也不会失败。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const STATIC_DIR = new URL("./static/", import.meta.url);

/* ---------------- 最小 DOM 桩 ---------------- */

function makeEl(sel = "") {
  const classes = new Set();
  const el = {
    _sel: sel,
    tagName: String(sel || "div").toUpperCase(),
    dataset: {},
    style: {},
    classList: {
      _set: classes,
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

/** 五个进度点，data-step 与 index.html 里的一致。 */
const STEPS = ["download", "verify", "push", "restart", "confirm"];

function loadApp() {
  const registry = {};
  const dots = STEPS.map((step) => {
    const dot = makeEl(".ota-step-dot");
    dot.dataset.step = step;
    return dot;
  });
  const steps = makeEl("#updFwSteps");
  steps.querySelectorAll = (sel) => (sel === ".ota-step-dot" ? dots : []);

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
  registry["#updFwSteps"] = [steps];

  const localStorage = {
    _m: new Map(),
    getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
    setItem(k, v) { this._m.set(k, String(v)); },
    removeItem(k) { this._m.delete(k); },
  };

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
  // 生产 i18n + 生产 zh-CN 文案表（默认语言），断言仍是生产函数的输出。
  run("i18n.js");
  sandbox.i18n.setMessages(
    JSON.parse(readFileSync(new URL("../locales/zh-CN.json", STATIC_DIR), "utf8")), "zh-CN");
  run("log_follow.js");
  run("log_device.js");
  run("reconnect_sync.js");
  run("keys.js");

  // app.js 的顶层绑定是词法作用域，追加一段把它暴露出来（不改生产代码）。
  // 只导出函数本身 —— 导出映射表会让「映射表不存在」变成失败原因，
  // 而我们想看到的是「映射错了导致点不亮」。
  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__fw = { updateFwStep };
`, sandbox, { filename: "app.js" });

  const host = sandbox.__fw;
  assert.ok(host, "app.js 没有导出 updateFwStep");

  return { host, dots };
}

/** 当前点亮的阶段（active 优先，其次最后一个 done）。 */
function activeStage(dots) {
  const active = dots.find((d) => d.classList.contains("active"));
  if (active) return active.dataset.step;
  const done = dots.filter((d) => d.classList.contains("done"));
  return done.length ? done[done.length - 1].dataset.step : "";
}

/** 每个进度点的 class 快照（用来比较「有没有被改动」）。 */
function classSnapshot(dots) {
  return dots.map((d) => ({
    step: d.dataset.step,
    active: d.classList.contains("active"),
    done: d.classList.contains("done"),
  }));
}

/* ---------------- 用例 ---------------- */

test("后端的任务阶段映射到正确的进度点", () => {
  const { host, dots } = loadApp();

  host.updateFwStep("downloading");
  assert.equal(activeStage(dots), "download", "下载阶段应点亮 download 点");

  host.updateFwStep("verifying");
  assert.equal(activeStage(dots), "verify", "校验阶段应点亮 verify 点");
  assert.ok(dots[0].classList.contains("done"), "download 点应标记为已完成");

  host.updateFwStep("pushing");
  assert.equal(activeStage(dots), "push", "推送阶段应点亮 push 点");
});

test("推送阶段借用的 OTA 窗口阶段（push/restart/confirm）也能点亮", () => {
  const { host, dots } = loadApp();

  host.updateFwStep("push");
  assert.equal(activeStage(dots), "push");

  host.updateFwStep("restart");
  assert.equal(activeStage(dots), "restart", "设备重启阶段应点亮 restart 点");

  host.updateFwStep("confirm");
  assert.equal(activeStage(dots), "confirm", "确认版本阶段应点亮 confirm 点");
});

test("done 时五个点全部点亮", () => {
  const { host, dots } = loadApp();

  host.updateFwStep("done");

  for (const dot of dots) {
    assert.ok(dot.classList.contains("done"), "完成后每个进度点都应标记完成：" + dot.dataset.step);
  }
});

test("未知阶段（如 error）不改动已有高亮", () => {
  const { host, dots } = loadApp();

  host.updateFwStep("restart");
  const before = classSnapshot(dots);

  host.updateFwStep("error");

  assert.deepEqual(classSnapshot(dots), before, "未知阶段不应把进度清空");
});
