// WS-586 设备页 / 编码器页前端回归用例。
//
// 运行：node --test host/test_device_encoder.mjs
//
// 这一版把两页的结构收敛掉，风险不在样式，而在三件会静默坏掉的事：
//   1. 编码器 5 个模式仍能各选一次，选中态跟着 state.cfg.encoder.mode 走；
//      12 键布局必须整组禁用，且锁定态不能靠 opacity 表达（B11）。
//   2. 设备页「连接 / 运行时长 / 可用堆 / 固件版本」只出现一次（状态条），
//      「详细信息」不再复读这四个字段。
//   3. 「固件更新」只剩一个入口；在线 / 本地文件是同一弹窗里的两种方式，
//      切换时只显示对应面板。
//
// 这里加载**生产脚本** app.js 在假 DOM 上真跑 renderEncoder / renderStatusStrip /
// renderSysinfo / setFwMethod，并读生产 index.html / style.css 做结构断言；
// 没有一份逻辑是从生产代码抄过来的副本。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const STATIC_DIR = new URL("./static/", import.meta.url);
const TEMPLATE = new URL("./templates/index.html", import.meta.url);

/* ---------------- 最小 DOM 桩 ---------------- */

function makeEl(sel = "") {
  const el = {
    _sel: sel,
    _q: null,
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
    querySelector(s) {
      if (!this._q) this._q = new Map();
      if (!this._q.has(s)) this._q.set(s, makeEl(s));
      return this._q.get(s);
    },
    querySelectorAll() { return []; },
    get options() { return []; },
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
    set(v) {
      el._html = String(v);
      if (v === "") el.children.length = 0;
    },
  });
  Object.defineProperty(el, "className", {
    get() { return [...el.classList._set].join(" "); },
    set(v) { el.classList._set = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  return el;
}

const flush = () => new Promise((r) => setImmediate(r));

/** 载入生产脚本，返回可断言的对象。 */
async function loadApp() {
  const registry = {};
  const body = makeEl("body");
  const docListeners = {};
  const grid = makeEl("#keyGrid");

  const document = {
    body,
    documentElement: makeEl("html"),
    querySelector(sel) {
      if (registry[sel]) return registry[sel][0];
      if (sel === "#keyGrid") return grid;
      return makeEl(sel);
    },
    querySelectorAll(sel) {
      if (sel === ".keycard") return grid.children.filter((c) => c.classList.contains("keycard"));
      if (sel === "#fwMethod .fw-method-btn") {
        return [registry["#fwMethodOnline"][0], registry["#fwMethodLocal"][0]];
      }
      return registry[sel] || [];
    },
    getElementById(id) { return this.querySelector("#" + id); },
    createElement(sel) { return makeEl(sel); },
    createTextNode(t) { return { textContent: t }; },
    addEventListener(type, fn) { (docListeners[type] = docListeners[type] || []).push(fn); },
    removeEventListener() {},
  };

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

  // 弹窗默认收起，避免 Esc / 焦点逻辑以为有模态打开。
  ["#helpModal", "#otaModal", "#updateModal"].forEach((sel) => {
    const m = makeEl(sel);
    m.hidden = true;
    registry[sel] = [m];
  });
  [
    "#encoderModes", "#encoderModeDesc", "#encoderActions", "#encoderDisabledNote",
    "#stripConn", "#stripUptime", "#stripHeap", "#stripFw",
    "#sysInfo", "#sysSampleAt", "#fwOnlinePane", "#fwLocalPane", "#btnFwLocal",
  ].forEach((sel) => { registry[sel] = [makeEl(sel)]; });
  registry["#encoderDisabledNote"][0].hidden = true;
  registry["#fwOnlinePane"][0].hidden = false;
  registry["#fwLocalPane"][0].hidden = true;
  registry["#fwMethodOnline"] = [makeEl("#fwMethodOnline")];
  registry["#fwMethodLocal"] = [makeEl("#fwMethodLocal")];
  registry["#fwMethodOnline"][0].dataset.method = "online";
  registry["#fwMethodLocal"][0].dataset.method = "local";

  run("i18n.js");
  sandbox.i18n.setMessages(
    JSON.parse(readFileSync(new URL("../locales/zh-CN.json", STATIC_DIR), "utf8")), "zh-CN");
  run("log_follow.js");
  run("log_device.js");
  run("reconnect_sync.js");
  run("cfg_status.js");
  run("keys.js");

  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__host = {
  state, renderEncoder, renderEncoderActions, renderStatusStrip, renderSysinfo,
  setFwMethod, is12Key,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  host.state.keycodes = [{ code: 0, label: "不发键" }];
  host.state.modDefs = [];
  sandbox.api = async () => ({ ok: true });
  await flush();
  await flush();
  host.state.selected = "t1";
  host.state.preview = false;
  host.state.cfg = null;
  host.state.devices = [];

  return { host, registry, sandbox };
}

function makeCfg(overrides = {}) {
  return Object.assign({
    layout: 1,
    keys: [],
    pins: [],
    chip: "C3",
    name: "BindDeckLite-ABCD",
    fw: { version: "1.3.0" },
    encoder: { mode: 0, actions: {} },
  }, overrides);
}

/* ---------------- 编码器：分段控件 ---------------- */

test("5 个模式渲染成一行 5 段，选中态跟着 mode 走", async () => {
  const { host, registry } = await loadApp();
  host.state.cfg = makeCfg();
  host.renderEncoder();

  const box = registry["#encoderModes"][0];
  assert.equal(box.children.length, 5, "5 种编码器模式必须是 5 段");
  const segs = box.children;
  assert.ok(segs.every((s) => s.classList.contains("enc-seg")), "分段统一用 .enc-seg");
  assert.ok(segs.every((s) => s.getAttribute("role") === "radio"), "分段是单选组");
  assert.ok(segs[0].classList.contains("active"));
  assert.equal(segs[0].getAttribute("aria-checked"), "true");
  assert.equal(segs[1].getAttribute("aria-checked"), "false");
  assert.match(registry["#encoderModeDesc"][0].textContent, /音量/, "描述只展示当前模式");

  // 点第 4 段（撤销 / 重做）→ 配置写入并重渲染，选中态唯一。
  segs[3].onclick();
  assert.equal(host.state.cfg.encoder.mode, 3);
  const after = box.children;
  assert.ok(after[3].classList.contains("active"));
  assert.equal(after.filter((s) => s.classList.contains("active")).length, 1);
  assert.equal(after[0].getAttribute("aria-checked"), "false");
  assert.equal(after[3].getAttribute("aria-checked"), "true");
  assert.match(registry["#encoderModeDesc"][0].textContent, /撤销/);
});

test("12 键布局：整组分段锁定，不靠 opacity 表达", async () => {
  const { host, registry } = await loadApp();
  const cfg = makeCfg({ layout: 0 });
  host.state.cfg = cfg;
  host.renderEncoder();

  const box = registry["#encoderModes"][0];
  assert.ok(box.classList.contains("locked"), "锁定时容器切到下沉表面");
  assert.ok(box.children.every((s) => s.disabled), "所有分段不可点");
  assert.ok(box.children.every((s) => s.classList.contains("locked")));
  assert.equal(registry["#encoderDisabledNote"][0].hidden, false, "必须给出为什么锁的说明");

  const css = readFileSync(new URL("style.css", STATIC_DIR), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "");
  assert.doesNotMatch(css, /\.mode-card/, "旧的模式卡样式应已删除");
  const lockedRule = css.match(/\.enc-seg\.locked,[\s\S]*?\{([^}]*)\}/);
  assert.ok(lockedRule, "锁定态规则必须存在");
  assert.doesNotMatch(lockedRule[1], /opacity/, "锁定态不能用 opacity");
  assert.match(css, /\.enc-segmented\.locked \{[^}]*--surface-sunken/,
    "锁定态容器改用 --surface-sunken");
});

/* ---------------- 设备页：状态条唯一事实源 ---------------- */

test("状态条固定 4 项，固件版本来自配置且不只依赖连接状态", async () => {
  const { host, registry } = await loadApp();
  host.state.cfg = makeCfg();
  host.state.devices = [{ dev_id: "t1", connected: true, address: "AA:BB:CC:DD:EE:FF" }];
  host.state.selected = "t1";
  host.state.preview = false;
  host.state.sysinfo = {
    t1: {
      supported: true,
      display: { reset: "上电(1)", uptime: "3 小时 12 分", nvs: "可用", heap: "96 KB" },
      flags: { nvs_bad: false, heap_low: false, safe: false, deepsleep: false },
      notes: {},
      at: Date.now(),
    },
  };

  host.renderSysinfo();

  assert.equal(registry["#stripConn"][0].textContent, "已连接");
  assert.equal(registry["#stripUptime"][0].textContent, "3 小时 12 分");
  assert.equal(registry["#stripHeap"][0].textContent, "96 KB");
  assert.equal(registry["#stripFw"][0].textContent, "1.3.0");

  const html = readFileSync(TEMPLATE, "utf8");
  const strip = html.match(/<div class="dev-status-strip"[\s\S]*?<\/div>\s*<!-- 详细信息/);
  assert.ok(strip, "状态条后必须紧跟「详细信息」");
  assert.equal((strip[0].match(/<div><span/g) || []).length, 4, "状态条固定 4 项");

  // 「详细信息」只保留状态条以外的字段：不得复读运行时长 / 可用堆 / 固件版本。
  const detail = registry["#sysInfo"][0].innerHTML;
  assert.match(detail, /上次复位/);
  assert.match(detail, /NVS/);
  assert.match(detail, /设备地址/);
  assert.match(detail, /AA:BB:CC:DD:EE:FF/);
  assert.doesNotMatch(detail, /运行时长/);
  assert.doesNotMatch(detail, /可用空间|空闲堆/);
  assert.doesNotMatch(detail, /固件版本/);
});

test("设备页只有 1 个固件入口、0 个页内 primary", () => {
  const html = readFileSync(TEMPLATE, "utf8");
  const deviceAt = html.indexOf('id="tab-device"');
  const logAt = html.indexOf('id="tab-log"');
  const device = html.slice(deviceAt, logAt);

  assert.equal((device.match(/id="btnFwUpdate"/g) || []).length, 1, "固件更新只有一个入口");
  assert.equal((device.match(/id="btnOta"/g) || []).length, 0, "旧的 OTA 按钮已合并");
  assert.equal((device.match(/class="btn primary/g) || []).length, 0,
    "设备页 primary 数量必须为 0（应用配置在顶栏）");
  assert.equal((device.match(/id="sysCard"/g) || []).length, 0, "旧的运行状态卡已删除");
  assert.equal((device.match(/data-i18n="ui.firmware_version"/g) || []).length, 1,
    "固件版本只出现一次");
});

/* ---------------- 固件更新：在线 / 本地分支 ---------------- */

test("在线 / 本地切换只显示对应面板", async () => {
  const { host, registry } = await loadApp();

  host.setFwMethod("local");
  assert.equal(registry["#fwOnlinePane"][0].hidden, true, "本地模式收起在线检查");
  assert.equal(registry["#fwLocalPane"][0].hidden, false);
  assert.ok(registry["#fwMethodLocal"][0].classList.contains("active"));
  assert.equal(registry["#fwMethodLocal"][0].getAttribute("aria-checked"), "true");
  assert.equal(registry["#fwMethodOnline"][0].getAttribute("aria-checked"), "false");

  host.setFwMethod("online");
  assert.equal(registry["#fwOnlinePane"][0].hidden, false);
  assert.equal(registry["#fwLocalPane"][0].hidden, true);
  assert.ok(registry["#fwMethodOnline"][0].classList.contains("active"));
});

test("本地文件入口存在且仍复用原有 OTA 向导", () => {
  const html = readFileSync(TEMPLATE, "utf8");
  assert.equal((html.match(/id="btnFwLocal"/g) || []).length, 1);
  assert.equal((html.match(/id="btnOtaPick"/g) || []).length, 1, "OTA 向导的选文件步骤必须保留");
  const app = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  assert.match(app, /\$\("#btnFwLocal"\)[\s\S]{0,200}openOtaWizard\(\)/,
    "#btnFwLocal 必须打开原有 OTA 向导");
  assert.doesNotMatch(app, /\$\("#btnOta"\)\.onclick/,
    "已删除的 #btnOta 不应再被绑定");
});
