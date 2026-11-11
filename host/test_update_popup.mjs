// WS-577: 「检查更新」弹窗的信息层级与交互回归用例。
//
// 运行：node --test host/test_update_popup.mjs
//
// 覆盖验收清单里能纯前端验证的部分：
//   * 已是最新时收起下载大小 / SHA256 / 更新内容（不能拿空字段误导用户）；
//   * 滚动版统一显示「构建 <sha7> · 时间」，不出现 ``0.0.0+main.``；
//   * 切换通道先确认、立即重查，请求期间保留旧结果并显示 loading；
//   * SHA256 复制按钮写入完整 64 位并 toast；
//   * 固件区块的设备状态 / 按钮禁用原因 / 五步条只在升级中显示。
//
// 测试加载**生产脚本** static/app.js 并在假 DOM 上真跑 render/switch，
// 不复制任何渲染逻辑 —— 改了生产代码这里才会跟着变。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const STATIC_DIR = new URL("./static/", import.meta.url);
const FULL_SHA = "98847f88c1a954379cb94917aa1471c65b0b00376ad135d3030428e97991149b";

/* ---------------- 最小 DOM 桩 ---------------- */

function makeEl(sel = "") {
  const classes = new Set();
  const el = {
    _sel: sel,
    tagName: String(sel || "div").toUpperCase(),
    dataset: {},
    style: {},
    children: [],
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
    parentNode: null,
    addEventListener() {},
    removeEventListener() {},
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    insertBefore(c, ref) {
      const i = ref ? this.children.indexOf(ref) : -1;
      c.parentNode = this;
      if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
      return c;
    },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    get options() { return this.children.filter((c) => c.tagName === "OPTION"); },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    setAttribute() {},
    removeAttribute() {},
    getAttribute() { return null; },
    focus() {},
    insertAdjacentHTML() {},
    closest() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, bottom: 0, height: 0, width: 0 }; },
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
  };
  return el;
}

/** 注册一次弹窗需要的全部节点；返回按 id 取元素的函数。 */
function buildRegistry() {
  const ids = [
    "updAppCurrent", "updAppLatest", "updAppSize", "updAppSha", "updAppState",
    "updAppCheckedAt", "updAppError", "updAppNotesBox", "updAppNotesTitle",
    "updAppNotes", "updAppRollingWarn", "updAppHint",
    "btnAppCheck", "btnUpdateApply", "btnUpdateSkip", "btnAppShaCopy",
    "updateChannel",
    "updFwDevice", "updFwDeviceState", "updFwVariant", "updFwCurrent",
    "updFwLatest", "updFwSize", "updFwSha", "updFwState", "updFwCheckedAt",
    "updFwError", "updFwNotesBox", "updFwNotesTitle", "updFwNotes",
    "updFwHint", "updFwSteps", "btnFwCheck", "btnFwApply", "btnFwShaCopy",
    "noticeCenter",
  ];
  const registry = {};
  for (const id of ids) registry["#" + id] = [makeEl("#" + id)];

  // 成对的行（标签 + 值）和五个进度点：按 class 选择器注册。
  registry[".upd-app-asset"] = [makeEl(".k"), makeEl(".v"), makeEl(".k"), makeEl(".v")];
  registry[".upd-fw-asset"] = [makeEl(".k"), makeEl(".v"), makeEl(".k"), makeEl(".v")];
  const dots = ["download", "verify", "push", "restart", "confirm"].map((step) => {
    const dot = makeEl(".ota-step-dot");
    dot.dataset.step = step;
    return dot;
  });
  registry[".ota-step-dot"] = dots;
  const steps = registry["#updFwSteps"][0];
  steps.querySelectorAll = (sel) => (sel === ".ota-step-dot" ? dots : []);
  const center = registry["#noticeCenter"][0];
  center.children = [];
  return registry;
}

function loadApp(fetchImpl) {
  const registry = buildRegistry();
  const document = {
    body: makeEl("body"),
    documentElement: makeEl("html"),
    querySelector(sel) {
      if (registry[sel]) return registry[sel][0] || null;
      return makeEl(sel);
    },
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
  const notices = [];
  const sandbox = {
    document,
    localStorage,
    console,
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    clearInterval() {},
    fetch: fetchImpl,
    navigator: { userAgent: "node" },
    location: { href: "http://127.0.0.1/", search: "" },
    alert() {},
    confirm() { return false; },
    addEventListener() {},
    removeEventListener() {},
    URLSearchParams,
    __notices: notices,
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
  run("keys.js");

  // app.js 的函数是词法作用域：追加一段把需要断言的入口暴露出来（不改生产代码）。
  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__upd = {
  updateState, state, renderUpdateApp, renderUpdateFw, checkAppUpdate,
  checkFwUpdate, switchUpdateChannel, copySha, fmtVersion,
  updateTargetText, updateCurrentText,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__upd;
  assert.ok(host, "app.js 没有导出更新弹窗入口");
  const el = (id) => registry["#" + id][0];
  const all = (sel) => registry[sel] || [];
  return { host, el, all, registry, sandbox };
}

/** 让 async 函数里的 await 链推进若干轮（不依赖真实定时器）。 */
async function tick(n = 4) {
  for (let i = 0; i < n; i++) await Promise.resolve();
}

function okResponse(data) {
  return { ok: true, status: 200, json: async () => data };
}

/** 默认 fetch：读接口给空对象，避免 init() 的网络调用炸掉测试。 */
function quietFetch() {
  return async () => okResponse({});
}

/* ---------------- 用例 ---------------- */

test("滚动版已是最新：收起包信息，当前/最新统一为「构建 sha · 时间」", () => {
  const { host, el, all } = loadApp(quietFetch());
  host.updateState.channel = "rolling";
  host.updateState.appCheckedAt = new Date("2026-11-11T03:04:05");
  host.updateState.app = {
    ok: true, has_update: false, channel: "rolling",
    current: "0.0.0+main.9b84c0b",
    latest: "main-20261111-0119",
    latest_version: "0.0.0+main.9b84c0b",
    tag: "main-20261111-0119",
    published_at: "2026-11-11T01:19:06Z",
    asset: { size: 27235635, sha256: FULL_SHA },
    notes: "## 本次更新\n- WS-571",
  };

  host.renderUpdateApp();

  assert.equal(el("updAppCurrent").textContent, el("updAppLatest").textContent,
    "同一个构建的当前/最新标识必须一致");
  assert.match(el("updAppCurrent").textContent, /^构建 9b84c0b · \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  assert.doesNotMatch(el("updAppCurrent").textContent, /0\.0\.0\+/);
  assert.doesNotMatch(el("updAppLatest").textContent, /0\.0\.0\+/);

  for (const row of all(".upd-app-asset")) {
    assert.equal(row.hidden, true, "已是最新时下载大小/SHA 行必须隐藏");
  }
  assert.equal(el("btnAppShaCopy").hidden, true, "已是最新时不显示复制按钮");
  assert.equal(el("updAppNotesTitle").textContent, "当前版本更新内容（nightly 9b84c0b）");
  assert.equal(el("updAppNotesBox").hidden, false, "当前版本有 notes 时应展示");
  assert.match(el("updAppState").textContent, /^已是最新（检查于 \d{2}:\d{2}:\d{2}）$/);
  assert.equal(el("updAppRollingWarn").hidden, false, "滚动版警告应显示");
});

test("稳定版已是最新：保留 vX.Y.Z，不显示滚动警告", () => {
  const { host, el, all } = loadApp(quietFetch());
  host.updateState.channel = "stable";
  host.updateState.appCheckedAt = new Date("2026-11-11T03:04:05");
  host.updateState.app = {
    ok: true, has_update: false, channel: "stable",
    current: "1.3.10", latest: "1.3.10", latest_version: "1.3.10", tag: "v1.3.10",
    asset: { size: 100, sha256: "" }, notes: "",
  };

  host.renderUpdateApp();

  assert.equal(el("updAppCurrent").textContent, "v1.3.10");
  assert.equal(el("updAppLatest").textContent, "v1.3.10");
  for (const row of all(".upd-app-asset")) assert.equal(row.hidden, true);
  assert.equal(el("updAppRollingWarn").hidden, true);
  assert.equal(el("updAppNotesBox").hidden, true, "没有 notes 时不显示空折叠块");
});

test("滚动版有更新：当前只显示构建号，最新显示构建号 + 时间，包信息可见", () => {
  const { host, el, all } = loadApp(quietFetch());
  host.updateState.channel = "rolling";
  host.updateState.appCheckedAt = new Date("2026-11-11T03:04:05");
  host.updateState.app = {
    ok: true, has_update: true, channel: "rolling",
    current: "0.0.0+main.aaaaaaa",
    latest: "main-20261111-0119",
    latest_version: "0.0.0+main.bbbbbbb",
    tag: "main-20261111-0119",
    local_tag: "main-20261109-0314",
    published_at: "2026-11-11T01:19:06Z",
    asset: { size: 27235635, sha256: FULL_SHA },
    notes: "## 本次更新\n- 新功能",
  };

  host.renderUpdateApp();

  assert.match(el("updAppCurrent").textContent, /^构建 aaaaaaa · \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  assert.match(el("updAppLatest").textContent, /^构建 bbbbbbb · \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  for (const row of all(".upd-app-asset")) assert.equal(row.hidden, false);
  assert.equal(el("btnAppShaCopy").hidden, false);
  assert.equal(el("updAppNotesTitle").textContent, "上位机更新内容");
});

test("fmtVersion 不再把 0.0.0+main 暴露给用户", () => {
  const { host } = loadApp(quietFetch());
  assert.equal(host.fmtVersion("0.0.0+main.9b84c0b"), "nightly 9b84c0b");
  assert.equal(host.fmtVersion("1.3.10"), "v1.3.10");
  assert.equal(host.fmtVersion("v1.4.0"), "v1.4.0");
});

test("切换通道：确认取消则不改通道；确认后立即重查且保留旧结果 + loading", async () => {
  const calls = [];
  let confirmValue = false;
  let resolveCheck;
  const pending = new Promise((resolve) => { resolveCheck = resolve; });

  const fetchImpl = async (path, opts = {}) => {
    calls.push({ path, method: (opts.method || "GET") });
    if (path.endsWith("/api/app/update/channel")) return okResponse({ channel: "rolling" });
    if (path.endsWith("/api/app/update/check")) return pending;
    return okResponse({});
  };
  const { host, el, sandbox } = loadApp(fetchImpl);
  sandbox.window.confirm = () => confirmValue;

  host.updateState.channel = "stable";
  host.updateState.appCheckedAt = new Date("2026-11-11T03:04:05");
  host.updateState.app = {
    ok: true, has_update: false, channel: "stable",
    current: "1.3.10", latest: "1.3.10", tag: "v1.3.10",
    asset: { size: 0, sha256: "" }, notes: "",
  };
  host.renderUpdateApp();

  // ① 取消确认：不发请求，选择器回到原值。
  await host.switchUpdateChannel("rolling");
  const before = calls.filter((c) => c.path.startsWith("/api/app/update")).length;
  assert.equal(before, 0, "用户取消时不应发任何请求");
  assert.equal(el("updateChannel").value, "stable");

  // ② 确认：POST 通道 → 立即 GET 重查。
  confirmValue = true;
  const running = host.switchUpdateChannel("rolling");
  await tick(10);
  const updCalls = calls.filter((c) => c.path.startsWith("/api/app/update"));
  assert.equal(updCalls[0].path, "/api/app/update/channel");
  assert.equal(updCalls[0].method, "POST");
  assert.equal(updCalls[1].path, "/api/app/update/check");

  // 请求还没回来：旧结果（v1.3.10）保留，状态行变 loading，选择器停在新通道。
  assert.equal(el("updateChannel").value, "rolling", "旧结果的 channel 不能把选择器顶回去");
  assert.equal(el("updAppRollingWarn").hidden, false, "loading 期间也应按新通道显示警告");
  assert.equal(el("updAppCurrent").textContent, "v1.3.10", "请求期间必须保留旧结果");
  assert.equal(el("updAppState").textContent, "正在重新检查…");
  assert.equal(el("btnAppCheck").disabled, true, "重查期间按钮应禁用");

  resolveCheck(okResponse({
    ok: true, has_update: true, channel: "rolling",
    current: "1.3.10", latest: "main-20261111-0119",
    latest_version: "0.0.0+main.bbbbbbb", tag: "main-20261111-0119",
    asset: { size: 10, sha256: FULL_SHA }, notes: "",
  }));
  await running;

  assert.equal(el("updAppCurrent").textContent, "v1.3.10");
  assert.match(el("updAppLatest").textContent, /^构建 bbbbbbb · /);
  assert.equal(el("btnAppCheck").disabled, false);
});

test("SHA256：hover 显示全文，复制按钮写入完整 64 位并 toast", async () => {
  let copied = "";
  const { host, el, registry, sandbox } = loadApp(quietFetch());
  sandbox.navigator.clipboard = { writeText: async (v) => { copied = v; } };
  host.updateState.appCheckedAt = new Date("2026-11-11T03:04:05");
  host.updateState.app = {
    ok: true, has_update: true, channel: "stable",
    current: "1.3.9", latest: "1.3.10", tag: "v1.3.10",
    asset: { size: 100, sha256: FULL_SHA }, notes: "",
  };
  host.renderUpdateApp();

  assert.equal(el("updAppSha").title, FULL_SHA, "hover title 必须是完整 SHA");
  assert.equal(el("updAppSha").textContent, FULL_SHA.slice(0, 16) + "…");
  assert.equal(el("btnAppShaCopy").hidden, false);

  await host.copySha("app");

  assert.equal(copied, FULL_SHA);
  const texts = registry["#noticeCenter"][0].children.map(
    (n) => (n.children[0] ? n.children[0].textContent : ""));
  assert.ok(texts.includes("SHA256 已复制"), "复制成功应有 toast");
});

test("固件已是最新：不显示体积/SHA/五步条，设备状态为已连接", () => {
  const { host, el, all } = loadApp(quietFetch());
  host.state.devices = [{ dev_id: "d1", name: "Deck", connected: true }];
  host.state.selected = "d1";
  host.updateState.fwCheckedAt = new Date("2026-11-11T03:04:05");
  host.updateState.fwRunning = false;
  host.updateState.fw = {
    has_update: false, device_variant: "esp32c3_16mb",
    current: "1.3.10", latest: "1.3.10",
    asset: { size: 1392720, sha256: FULL_SHA },
    notes: "## 本次更新\n- 固件改动",
  };

  host.renderUpdateFw();

  for (const row of all(".upd-fw-asset")) assert.equal(row.hidden, true);
  assert.equal(el("updFwSteps").hidden, true, "不在升级中时五步条必须隐藏");
  assert.equal(el("btnFwShaCopy").hidden, true);
  assert.equal(el("updFwDeviceState").textContent, "已连接");
  assert.equal(el("btnFwCheck").disabled, false);
  assert.match(el("updFwState").textContent, /^固件已是最新（检查于 \d{2}:\d{2}:\d{2}）$/);
  assert.equal(el("updFwNotesTitle").textContent, "当前版本更新内容（v1.3.10）");
});

test("固件未连接 / OTA 中：按钮禁用并说明原因", () => {
  const { host, el } = loadApp(quietFetch());
  host.state.devices = [{ dev_id: "d1", name: "Deck", connected: false }];
  host.state.selected = "d1";
  host.updateState.fw = {
    has_update: true, device_variant: "esp32c3",
    current: "1.3.9", latest: "1.3.10",
    asset: { size: 1, sha256: FULL_SHA }, notes: "",
  };

  host.renderUpdateFw();
  assert.equal(el("updFwDeviceState").textContent, "未连接");
  assert.equal(el("btnFwCheck").disabled, true);
  assert.equal(el("btnFwApply").disabled, true);
  assert.equal(el("updFwHint").hidden, false);
  assert.equal(el("updFwHint").textContent, "设备未连接，无法检查或升级固件");

  host.state.ota = { status: "uploading" };
  host.renderUpdateFw();
  assert.equal(el("updFwDeviceState").textContent, "OTA 进行中");
  assert.equal(el("btnFwCheck").disabled, true);
  assert.equal(el("updFwHint").textContent, "设备正在 OTA，完成后再检查或升级");
});

test("固件升级进行中才显示五步条", () => {
  const { host, el } = loadApp(quietFetch());
  host.state.devices = [{ dev_id: "d1", name: "Deck", connected: true }];
  host.state.selected = "d1";
  host.updateState.fw = { has_update: true, current: "1.3.9", latest: "1.3.10" };

  host.updateState.fwRunning = true;
  host.renderUpdateFw();
  assert.equal(el("updFwSteps").hidden, false);
  assert.equal(el("btnFwApply").disabled, false);

  host.updateState.fwRunning = false;
  host.renderUpdateFw();
  assert.equal(el("updFwSteps").hidden, true);
});
