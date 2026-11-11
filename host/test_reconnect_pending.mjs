// WS-561 重连对账「未下发修改」判定的前端回归用例。
//
// 运行：cd host && node --test test_reconnect_pending.mjs
//
// 背景（WS-565 must fix 1）：重连对账在 6dd0ca7 里按 `cfg.enc` 比较编码器，
// 但生产配置根本没有 `enc` 键 —— 编码器模式在 `cfg.encoder.mode`。后果是
//   ① 手机改了编码器模式，重连后本机仍是旧值、编码器页不重绘、无日志，
//      再点「应用配置」就把旧模式写回设备；
//   ② 本机改了 encoder.mode 未下发时 collectPending() 返回 []，于是照样发
//      refresh，琥珀点与状态行提示都不出现。
//
// 这些用例在假 DOM 上加载**生产** static/app.js（以及它依赖的脚本），直接
// 调 collectPending / reconcileDeviceConfig，断言的是真实入口 —— 不是在这里
// 复制一份判定逻辑。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const STATIC_DIR = new URL("./static/", import.meta.url);

/* ---------------- 最小 DOM 桩 ---------------- */

function makeEl(sel = "") {
  return {
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
    insertBefore(c) { this.children.push(c); return c; },
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
}

/** 生产配置形状：后端 new_device_cfg()（编码器模式在 encoder.mode，没有 enc 键）。 */
function realCfg() {
  return {
    name: "工作的板子",
    chip: "ESP32",
    trusted: true,
    layout: 0,
    pins: [13, 12, 14, 27, 32, 33, 25, 26, 4],
    keys: Array.from({ length: 12 }, (_, i) => ({
      code: 240 + i,
      label: `F${13 + i}`,
      action: { type: "none", value: "", macro: [] },
    })),
    encoder: {
      mode: 0,
      actions: {
        CW: { type: "none", value: "", macro: [] },
        CCW: { type: "none", value: "", macro: [] },
      },
    },
    sleep: { enabled: true, minutes: 5 },
  };
}

/**
 * 载入生产脚本，返回装配好一台「已连接」设备的 host。
 *
 * `server.refresh` 是 POST /api/device/<id>/refresh 的答案，`server.calls`
 * 记录每次请求（用来断言「有未下发修改时一次都不发」）。
 */
function loadApp() {
  const registry = {
    "#logList": [makeEl("#logList")],
    "#cfgStatus": [makeEl("#cfgStatus")],
    "#cfgReadStatus": [makeEl("#cfgReadStatus")],
    "#btnApply": [makeEl("#btnApply")],
    "#logEmpty": [makeEl("#logEmpty")],
  };
  const document = {
    body: makeEl("body"),
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
    // init() 会起轮询定时器：让它们不注册，脚本跑完就退出。
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
  run("reconnect_sync.js");
  // WS-584：顶栏三态徽标的判定模块，app.js 的 renderCfgStatus 依赖它。
  run("cfg_status.js");
  run("keys.js");

  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__host = {
  state, collectPending, reconcileDeviceConfig, updatePendingUi, bucketFor,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  assert.ok(host, "app.js 没有导出宿主函数");

  const server = { refresh: null, calls: [] };
  sandbox.api = async (path, opts) => {
    if (String(path).endsWith("/refresh")) {
      server.calls.push(String(path));
      if (!server.refresh) throw new Error("refresh not expected");
      return server.refresh;
    }
    return {};
  };

  // 一台已连接、本机有存档且已对过账的设备。
  host.state.selected = "t1";
  host.state.preview = false;
  host.state.devices = [{ dev_id: "t1", name: "工作的板子", connected: true }];
  host.state.cfg = realCfg();
  const b = host.bucketFor("t1");
  b.cfg = host.state.cfg;
  b.applied = JSON.parse(JSON.stringify(host.state.cfg));
  host.state.connSince = { t1: Date.now() - 3000 };  // 已过 2 s 稳定窗口
  host.state.reconnectSynced = {};
  host.state.reconnectDirty = {};
  host.state.readStatus = {};
  host.state.dirty = false;
  host.state.saveTimer = null;

  return { host, server, registry };
}

/* ---------------- 用例 ---------------- */

test("本机把 encoder.mode 改成 3 未下发 → collectPending 报一项 enc", () => {
  const { host } = loadApp();
  host.state.cfg.encoder.mode = 3;
  const items = host.collectPending();
  assert.equal(items.length, 1, "改了编码器模式却没算待下发，refresh 会被照发");
  assert.equal(items[0].field, "enc");
  assert.equal(items[0].tab, "encoder");
});

test("只改本机 encoder.actions（纯上位机字段）→ 不算待下发", () => {
  const { host } = loadApp();
  host.state.cfg.encoder.actions.CW = { type: "media", value: "play_pause", macro: [] };
  // 跨 vm 上下文的数组原型不同，用长度断言而不是 deepEqual。
  assert.equal(host.collectPending().length, 0);
});

test("有未下发的编码器模式 → 不发 refresh，出琥珀点与状态行提示", async () => {
  const { host, server, registry } = loadApp();
  host.state.cfg.encoder.mode = 3;
  host.state.dirty = true;

  await host.reconcileDeviceConfig("t1");

  assert.equal(server.calls.length, 0, "有未下发修改时绝不能发 refresh");
  const chip = registry["#cfgStatus"][0];
  assert.equal(chip.hidden, false);
  assert.equal(chip.classList.contains("reconnect-dirty"), true, "琥珀点没出现");
  // WS-584：同一个徽标现在承载三态，重连提示落在 pending 态上。
  assert.equal(chip.dataset.state, "pending", "顶栏徽标没进入待下发态");
  assert.equal(chip.title, "设备已重新连接，本机有 1 项修改未下发");
  assert.equal(
    registry["#cfgReadStatus"][0].textContent,
    "设备已重新连接 · 本机有 1 项修改未下发 · 可能与设备当前值不同"
  );
  assert.equal(host.state.cfg.encoder.mode, 3, "本机的编码器模式不能被改回");
});

test("无未下发修改、设备回报 encoder.mode=2 → 按设备值重绘并记一行日志", async () => {
  const { host, server, registry } = loadApp();
  const deviceCfg = realCfg();
  deviceCfg.encoder.mode = 2;
  server.refresh = {
    ok: true,
    at: 1700000000,
    fields: { pins: true, keys: true, enc: true, sleep: true },
    cfg: deviceCfg,
  };

  await host.reconcileDeviceConfig("t1");

  assert.equal(server.calls.length, 1);
  assert.equal(host.state.cfg.encoder.mode, 2, "编码器页仍是旧值 = 验收失败");
  const rows = registry["#logList"][0].children;
  const texts = rows.map((r) => r._text);
  assert.deepEqual(texts, ["已按设备同步：编码器模式"]);
  assert.equal(registry["#cfgStatus"][0].classList.contains("reconnect-dirty"), false);
});

test("四组值完全一致 → 不重绘、不新增 sync 行", async () => {
  const { host, server, registry } = loadApp();
  server.refresh = {
    ok: true,
    at: 1700000000,
    fields: { pins: true, keys: true, enc: true, sleep: true },
    cfg: realCfg(),
  };

  await host.reconcileDeviceConfig("t1");

  assert.equal(server.calls.length, 1);
  assert.deepEqual(registry["#logList"][0].children, [], "值一致时不该有 sync 日志");
});
