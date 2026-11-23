// WS-684：OTA 降级前的媒体键拦截回归用例。
//
// 运行：cd host && node --test test_ota_downgrade_ui.mjs
//
// 两段断言，都打在生产代码上，不复制判定：
//   1. `host/static/ota_compat.js` 的 otaMediaDowngradeRisk() 直接 import：
//      版本三段比较、媒体键段起点、10/12 键布局的扫描范围、后缀版本的处理。
//   2. 在假 DOM 上加载**生产** static/app.js，走真实的 handleOtaFile → 风险
//      计算 → renderOta 链路，断言 #otaCompat 的显隐与 #btnOtaNext 的禁用态
//      （勾选前后各一次），以及确认页兜底、重试路径。「只在文档里写的警示」
//      正是这次要修的东西，所以界面状态必须由这份用例钉住。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import OtaCompat from "./static/ota_compat.js";

const { otaMediaDowngradeRisk, parseVersionTriplet } = OtaCompat;

const STATIC_DIR = new URL("./static/", import.meta.url);

/* ---------------- 生产纯函数 ---------------- */

/** 12 个普通键（F13 起），再按需把指定下标改成媒体键。 */
function cfgWithMedia(mediaIdxs, layout = 1) {
  const keys = Array.from({ length: 12 }, (_, i) => ({ code: 104 + i, mods: 0 }));
  mediaIdxs.forEach((i, n) => { keys[i].code = 320 + n; });
  return { layout, keys };
}

test("v1.3.12 + 共 3 个媒体键 → block，affected 为 4/7/9", () => {
  const risk = otaMediaDowngradeRisk("1.3.12", cfgWithMedia([4, 7, 9]), 256);
  assert.equal(risk.level, "block");
  assert.deepEqual(risk.affected.map((a) => a.idx), [4, 7, 9]);
});

test("目标 1.3.13 / 1.3.14 / 1.3.13-dev → ok（从 1.3.13 起支持媒体键）", () => {
  const cfg = cfgWithMedia([4, 7, 9]);
  for (const v of ["1.3.13", "1.3.14", "1.3.13-dev", "v1.3.13"]) {
    assert.equal(otaMediaDowngradeRisk(v, cfg, 256).level, "ok", v + " 不应被拦");
  }
});

test("v1.3.12 但存档里没有媒体键 → ok（降级不丢能力，与今天一致）", () => {
  assert.equal(otaMediaDowngradeRisk("1.3.12", cfgWithMedia([]), 256).level, "ok");
});

test("不足三段版本 → unknown，只提示不拦", () => {
  for (const v of ["1.3", "dev", "", null, undefined]) {
    const risk = otaMediaDowngradeRisk(v, cfgWithMedia([4]), 256);
    assert.equal(risk.level, "unknown", String(v) + " 应判 unknown");
    assert.equal(risk.reason, "version");
    assert.deepEqual(risk.affected, []);
  }
  assert.equal(parseVersionTriplet("1.3"), null);
});

test("1.3.12-rc1 按 1.3.12 处理 → block", () => {
  assert.equal(otaMediaDowngradeRisk("1.3.12-rc1", cfgWithMedia([4]), 256).level, "block");
});

test("媒体键段起点取 media_base：512 时键码 300 不算媒体键 → ok", () => {
  const cfg = { layout: 1, keys: [{ code: 300 }] };
  assert.equal(otaMediaDowngradeRisk("1.3.12", cfg, 512).level, "ok");
  // 同一份存档按默认 256 判就是 block —— 起点不是写死的。
  assert.equal(otaMediaDowngradeRisk("1.3.12", cfg, 256).level, "block");
  // 起点缺失时回退 256。
  assert.equal(otaMediaDowngradeRisk("1.3.12", cfg, undefined).level, "block");
});

test("编码器布局只看 10 个输入，12 键布局第 11 个输入也参与", () => {
  const cfg = cfgWithMedia([10], 0);           // 只有第 11 个输入是媒体键
  assert.equal(otaMediaDowngradeRisk("1.3.12", cfg, 256).level, "block");
  assert.deepEqual(
    otaMediaDowngradeRisk("1.3.12", cfg, 256).affected.map((a) => a.idx), [10]);
  // 同一份存档换成编码器布局：第 11/12 键被隐藏，不参与判定。
  assert.equal(otaMediaDowngradeRisk("1.3.12", cfgWithMedia([10], 1), 256).level, "ok");
});

test("没有存档（cfg 缺失）→ unknown，交给上层提示去读设备", () => {
  const risk = otaMediaDowngradeRisk("1.3.12", null, 256);
  assert.equal(risk.level, "unknown");
  assert.equal(risk.reason, "cfg");
});

/* ---------------- 生产界面：handleOtaFile → renderOta ---------------- */

function makeEl(sel = "") {
  const el = {
    _sel: sel,
    _text: "",
    tagName: String(sel || "div").toUpperCase(),
    dataset: {}, style: {}, children: [], attributes: {},
    title: "", value: "", checked: false, hidden: false, disabled: false, type: "",
    parentNode: null,
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
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      return c;
    },
    remove() {},
    setAttribute(k, v) { this.attributes[k] = String(v); },
    removeAttribute(k) { delete this.attributes[k]; },
    getAttribute(k) { return this.attributes[k] ?? null; },
    focus() {}, click() {}, insertAdjacentHTML() {}, closest() { return null; },
    querySelector(s) { return (this._q && this._q[s]) || makeEl(s); },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, bottom: 0, height: 0, width: 0 }; },
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    get options() { return []; },
  };
  Object.defineProperty(el, "textContent", {
    get() { return this._text; },
    set(v) { this._text = String(v == null ? "" : v); this.children.length = 0; },
  });
  Object.defineProperty(el, "innerHTML", {
    get() { return el._html || ""; },
    set(v) { el._html = String(v); },
  });
  Object.defineProperty(el, "className", {
    get() { return [...el.classList._set].join(" "); },
    set(v) { el.classList._set = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  return el;
}

/** 一份能通过 localOtaImageError 的 app-only 镜像（描述符里带版本号）。 */
function fakeOtaFile(name, version) {
  const bytes = new Uint8Array(0x80);
  bytes[0] = 0xe9;                       // ESP 镜像头
  bytes[0x20] = 0x32; bytes[0x21] = 0x54; // app 描述符 magic 0xABCD5432
  bytes[0x22] = 0xcd; bytes[0x23] = 0xab;
  const enc = new TextEncoder().encode(version || "");
  bytes.set(enc.subarray(0, 31), 0x30);
  return {
    name,
    size: bytes.length,
    arrayBuffer: async () => bytes.buffer,
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
    createElement(tag) { return makeEl(tag); },
    createTextNode(t) { return { textContent: t }; },
    addEventListener() {}, removeEventListener() {},
  };
  [
    "#otaModal", "#otaCompat", "#otaCompatTitle", "#otaCompatBody", "#otaCompatList",
    "#otaCompatHint", "#otaRiskAckRow", "#otaRiskAck", "#btnOtaCompatRead",
    "#otaConfirmCompat", "#btnOtaNext", "#btnOtaStart", "#btnOtaClose", "#btnOtaPick",
    "#btnOtaRepick", "#btnOtaBack", "#btnOtaRetry", "#btnOtaDone", "#btnOtaCancel",
    ".tab[data-tab=\"keys\"]",
  ].forEach((sel) => { registry[sel] = [makeEl(sel)]; });
  registry["#otaModal"][0].hidden = false;

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
    addEventListener() {}, removeEventListener() {},
    URLSearchParams,
    TextEncoder,
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
  run("ota_compat.js");                 // 与 index.html 同一份脚本、同一顺序
  run("keys.js");

  vm.runInContext(readFileSync(new URL("app.js", STATIC_DIR), "utf8") + `
globalThis.__host = {
  state, handleOtaFile, renderOta, retryOta, refreshOtaRisk, otaRiskUnacked,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  assert.ok(host, "app.js 没有导出测试入口");
  // init() 是 async：让它的首个失败分支跑完，绑定（含 bindOtaWizard）才就位。
  await new Promise((resolve) => setImmediate(resolve));

  host.state.selected = "d1";
  host.state.preview = false;
  host.state.devices = [{ dev_id: "d1", connected: true, fw: { version: "1.3.14" } }];
  host.state.keycodes = [
    { code: 104, label: "F13" },
    { code: 320, label: "播放/暂停" },
    { code: 321, label: "下一曲" },
    { code: 322, label: "静音" },
  ];
  host.state.mediaBase = 256;
  host.state.sessionRead = {};
  host.state.cfg = {
    name: "t1", chip: "C3", trusted: true, layout: 1,
    pins: [0, 1, 2, 3, 4, 5, 6, 7, 8],
    keys: [
      { code: 104, mods: 0, action: { type: "none" } },
      { code: 105, mods: 0, action: { type: "none" } },
      { code: 106, mods: 0, action: { type: "none" } },
      { code: 107, mods: 0, action: { type: "none" } },
      { code: 320, mods: 0, action: { type: "none" } },   // 4 → 媒体键
      { code: 108, mods: 0, action: { type: "none" } },
      { code: 109, mods: 0, action: { type: "none" } },
      { code: 321, mods: 0, action: { type: "none" } },   // 7 → 媒体键
      { code: 110, mods: 0, action: { type: "none" } },
      { code: 322, mods: 0, action: { type: "none" } },   // 9 → 编码器按压
    ],
    encoder: { mode: 0, inverted: false, actions: { CW: { type: "none" }, CCW: { type: "none" } } },
    sleep: { enabled: true, minutes: 5 },
  };
  return { host, registry, sandbox };
}

test("选 v1.3.12：兼容性区块为 block 且列出三行，#btnOtaNext 禁用到勾选为止", async () => {
  const { host, registry } = await loadApp();

  await host.handleOtaFile(fakeOtaFile("ota_binddecklite_c3_v1.3.12.bin", "1.3.12"));

  const compat = registry["#otaCompat"][0];
  assert.equal(host.state.ota.status, "ready");
  assert.equal(host.state.ota.risk.level, "block");
  assert.equal(compat.hidden, false, "block 时兼容性区块必须可见");
  assert.equal(compat.dataset.state, "block");
  assert.equal(registry["#btnOtaNext"][0].disabled, true,
    "未勾选「我了解风险」时「下一步」必须禁用");

  const rows = registry["#otaCompatList"][0].children;
  assert.equal(rows.length, 3, "三个媒体键都要点名");
  assert.equal(rows[0].textContent, "按键 5 · 播放/暂停");
  assert.equal(rows[1].textContent, "按键 8 · 下一曲");
  assert.equal(rows[2].textContent, "编码器按压 · 静音");

  // 勾选 → 两处按钮立刻跟着变（onchange 由 bindOtaWizard 绑定）。
  const ack = registry["#otaRiskAck"][0];
  ack.checked = true;
  ack.onchange({ target: ack });
  assert.equal(host.state.ota.ack, true);
  assert.equal(registry["#btnOtaNext"][0].disabled, false, "勾选后应放行");

  // 确认页兜底红字 + 未勾选时「开始升级」保持禁用。
  assert.equal(registry["#otaConfirmCompat"][0].hidden, false);
  assert.match(registry["#otaConfirmCompat"][0].textContent, /3 个输入受影响/);
  assert.equal(registry["#btnOtaStart"][0].disabled, false);

  ack.checked = false;
  ack.onchange({ target: ack });
  assert.equal(registry["#btnOtaStart"][0].disabled, true,
    "确认页取消勾选后「开始升级」必须禁用");
});

test("受影响行是可点按钮：点「按键 5」关闭向导并跳到输入 4", async () => {
  const { host, registry } = await loadApp();
  await host.handleOtaFile(fakeOtaFile("ota_binddecklite_c3_v1.3.12.bin", "1.3.12"));

  registry["#btnOtaNext"][0].disabled = false;   // 模拟已勾选后进入确认
  const row = registry["#otaCompatList"][0].children[0];
  assert.equal(row.dataset.idx, "4");
  row.onclick();
  assert.equal(registry["#otaModal"][0].hidden, true, "点条目后应关闭 OTA 向导");
});

test("「重试升级」回确认页也挡：未勾选时 #btnOtaStart 仍禁用", async () => {
  const { host, registry } = await loadApp();
  await host.handleOtaFile(fakeOtaFile("ota_binddecklite_c3_v1.3.12.bin", "1.3.12"));

  const ack = registry["#otaRiskAck"][0];
  ack.checked = true;
  ack.onchange({ target: ack });
  assert.equal(registry["#btnOtaStart"][0].disabled, false);

  // 上传失败后回到结果页；重试前把勾选撤掉 = 用户尚未确认。
  ack.checked = false;
  ack.onchange({ target: ack });
  host.state.ota = Object.assign({}, host.state.ota, { status: "error", error: "x" });
  host.retryOta();
  assert.equal(host.state.otaStep, "confirm");
  assert.equal(registry["#btnOtaStart"][0].disabled, true,
    "未勾选时重试路径不能绕过拦截");
});

test("目标 v1.3.13：兼容性区块隐藏、下一步可用（与今天一致）", async () => {
  const { host, registry } = await loadApp();
  await host.handleOtaFile(fakeOtaFile("ota_binddecklite_c3_v1.3.13.bin", "1.3.13"));

  assert.equal(host.state.ota.risk.level, "ok");
  assert.equal(registry["#otaCompat"][0].hidden, true);
  assert.equal(registry["#btnOtaNext"][0].disabled, false);
});

test("版本读不到：unknown 灰块可见但不拦", async () => {
  const { host, registry } = await loadApp();
  await host.handleOtaFile(fakeOtaFile("ota_binddecklite_c3.bin", ""));

  assert.equal(host.state.ota.risk.level, "unknown");
  assert.equal(host.state.ota.risk.reason, "version");
  assert.equal(registry["#otaCompat"][0].hidden, false, "unknown 要提示");
  assert.equal(registry["#otaCompat"][0].dataset.state, "unknown");
  assert.equal(registry["#btnOtaNext"][0].disabled, false, "unknown 不拦");
});

test("存档不可信：提示「无法判断」+ 读取按钮，读成功后按设备真值重算", async () => {
  const { host, registry, sandbox } = await loadApp();
  host.state.cfg.trusted = false;                 // 本机没有这台设备的存档
  host.state.sessionRead = {};

  await host.handleOtaFile(fakeOtaFile("ota_binddecklite_c3_v1.3.12.bin", "1.3.12"));

  assert.equal(host.state.ota.risk.level, "unknown");
  assert.equal(host.state.ota.risk.reason, "untrusted");
  assert.equal(registry["#btnOtaNext"][0].disabled, false, "存档不可信只提示，不误拦");
  assert.equal(registry["#btnOtaCompatRead"][0].hidden, false, "必须给「读取设备配置」");
  assert.match(registry["#otaCompatTitle"][0].textContent, /没有这台设备的配置存档/);

  // 读取设备配置：走生产 readDeviceConfig → applyReadback → refreshOtaRisk。
  const fresh = JSON.parse(JSON.stringify(host.state.cfg));
  fresh.trusted = true;
  sandbox.api = async () => ({
    ok: true, at: Date.now(), cfg: fresh,
    fields: { pins: true, keys: true, enc: true, sleep: true },
  });

  await vm.runInContext("readDeviceConfig(state.selected)", sandbox);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(host.state.sessionRead.d1, true, "读取成功要记进本会话");
  assert.equal(host.state.ota.risk.level, "block", "读完后必须按设备真值重算");
  assert.equal(registry["#btnOtaNext"][0].disabled, true);
});
