// WS-629 编码器「旋转方向反转」dirty / 待下发判定的前端回归用例。
//
// 运行：cd host && node --test test_encoder_dir_ui.mjs
//
// 背景（WS-627 对 PR #87 的 must fix 1）：只切「旋转方向」时
//   markDirty() → setDirty(true) → updatePendingUi() → collectPending()
// 返回 []，于是 state.dirty 被重新算成 false：顶栏「应用配置」不亮、状态浮层
// 没有这一项；readDeviceConfig() 的「有未下发修改」确认不弹、applyReadback()
// 用设备值整体覆盖 state.cfg，用户刚选的反转被静默丢掉；重连对账的
// pendingCount 闸门同样失效。
//
// 这些用例在假 DOM 上加载**生产** static/app.js（及它依赖的脚本），点真实
// renderEncoderDir() 渲染出来的胶囊，断言的是真实入口 state.dirty /
// collectPending() / #btnApply / highlightFormField —— 没有在这里复制一份
// 判定逻辑：把生产里 encdir 那一项删掉，这些用例会真的变红。
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
  // renderEncoderDir() 先 box.innerHTML = "" 再 appendChild：清空时把 children
  // 一起清掉，和真实 DOM 一致，重渲染后拿到的是当前这一组胶囊。
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

/** 生产配置形状：编码器布局，encoder.inverted 默认 false（后端 new_device_cfg）。 */
function makeCfg() {
  return {
    name: "t1",
    chip: "ESP32",
    trusted: true,
    layout: 1,                       // LAYOUT_ENCODER：方向胶囊可用
    pins: [13, 12, 14, 27, 32, 33, 25, 26, 4],
    keys: [],
    encoder: {
      mode: 0,
      inverted: false,
      actions: { CW: { type: "none" }, CCW: { type: "none" } },
    },
    sleep: { enabled: true, minutes: 5 },
  };
}

/**
 * 在假 DOM 上加载生产脚本，返回装配好一台「已连接、已下发过一次」设备的 host。
 * b.applied 是已下发快照（inverted=false），切「反转」后应与 state.cfg 产生
 * 差异 —— 这正是 collectPending() 该报 encdir 的依据。
 */
function loadApp() {
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
  // 预先放好会被断言的元素；popover 默认收起，renderCfgPopover 直接 return。
  const pre = [
    "#btnApply", "#encoderDirSegments", "#encoderDirNote", "#cfgStatus",
    "#cfgPopover", "#cfgStatusText", "#cfgReadStatus", "#logList", "#logEmpty",
    "#encoderModes", "#encoderModeDesc", "#encoderActions", "#encoderDisabledNote",
  ];
  pre.forEach((sel) => { registry[sel] = [makeEl(sel)]; });
  registry["#cfgPopover"][0].hidden = true;
  registry["#encoderDisabledNote"][0].hidden = true;

  const localStorage = {
    getItem: () => null, setItem() {}, removeItem() {},
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
  run("log_export.js");
  run("reconnect_sync.js");
  run("cfg_status.js");
  run("keys.js");

  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__host = {
  state, collectPending, renderEncoderDir, markDirty, bucketFor,
  updatePendingUi, highlightFormField,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  assert.ok(host, "app.js 没有导出宿主函数");
  sandbox.api = async () => ({});

  host.state.selected = "t1";
  host.state.preview = false;
  host.state.devices = [{ dev_id: "t1", connected: true }];
  host.state.cfg = makeCfg();
  const b = host.bucketFor("t1");
  b.cfg = host.state.cfg;
  b.applied = JSON.parse(JSON.stringify(host.state.cfg));  // 基线：inverted=false
  host.state.reconnectDirty = {};
  host.state.readStatus = {};
  host.state.pendingIgnored = {};
  host.state.reconnectSynced = {};
  host.state.connSince = {};
  host.state.dirty = false;
  host.state.saveTimer = null;

  return { host, registry };
}

/** 真实渲染出方向胶囊，返回这两段按钮（第 1 段默认、第 2 段反转）。 */
function renderDirSegments(host, registry) {
  host.renderEncoderDir();
  const segs = registry["#encoderDirSegments"][0].children.slice();
  assert.equal(segs.length, 2, "旋转方向应渲染成 2 个胶囊");
  return segs;
}

/* ---------------- 用例 ---------------- */

test("切『反转』→ state.dirty=true、collectPending 含 encdir、#btnApply 有 dirty 类", () => {
  const { host, registry } = loadApp();
  const segs = renderDirSegments(host, registry);

  assert.equal(host.state.dirty, false, "开工前应无待下发项");
  assert.equal(host.collectPending().length, 0);

  segs[1].onclick();  // 点真实渲染出来的「反转」胶囊

  assert.equal(host.state.cfg.encoder.inverted, true, "反转应写进 cfg.encoder.inverted");
  assert.equal(host.state.dirty, true, "反转没接入 dirty：顶栏不会亮、回读会覆盖本机值");
  const items = host.collectPending();
  const encdir = items.find((i) => i.field === "encdir");
  assert.ok(encdir, "collectPending() 必须含 field:\"encdir\"");
  assert.equal(encdir.tab, "encoder");
  assert.equal(encdir.label, "旋转方向");
  assert.ok(registry["#btnApply"][0].classList.contains("dirty"),
    "#btnApply 应带 dirty 类");
});

test("切回『默认』→ 回到 0 项、dirty=false、#btnApply 无 dirty 类", () => {
  const { host, registry } = loadApp();
  const segs = renderDirSegments(host, registry);

  segs[1].onclick();  // 反转
  assert.equal(host.state.dirty, true, "反转后应先进入待下发态");
  assert.equal(host.collectPending().length, 1);

  segs[0].onclick();  // 切回默认（用同一组胶囊里 val=false 的那段）

  assert.equal(host.state.cfg.encoder.inverted, false);
  assert.equal(host.state.dirty, false, "切回默认后 dirty 应回落");
  const items = host.collectPending();
  assert.equal(items.length, 0, "切回默认应回到 0 项");
  assert.ok(!items.some((i) => i.field === "encdir"));
  assert.ok(!registry["#btnApply"][0].classList.contains("dirty"),
    "#btnApply 不应再带 dirty 类");
});

test("highlightFormField('encdir') 高亮 #encoderDirSegments（方向被设备拒绝时）", () => {
  const { host, registry } = loadApp();
  host.highlightFormField("encdir");
  assert.ok(registry["#encoderDirSegments"][0].classList.contains("field-highlight"),
    "方向被设备拒绝时，方向胶囊应高亮");
});
