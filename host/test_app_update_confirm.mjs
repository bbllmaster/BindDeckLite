// WS-531: 「立即更新」前的确认提示的前端回归用例。
//
// 运行：node --test host/test_app_update_confirm.mjs
//
// 背景：上位机自更新会退出上位机进程（BLE 被断、正在跑的上位机动作全停），
// 但更新弹窗的「立即更新」原来一点就直接 POST /api/app/update/apply ——
// 设备连着时用户毫无预警地被踢下线（WS-524 第三轮评审遗留）。
//
// 这里加载**生产脚本** host/static/app.js，在假 DOM 上真跑 applyAppUpdate，
// 而不是把判定逻辑抄一份到测试里 —— 抄的那份改了生产代码也不会失败。
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

/**
 * 载入生产脚本，返回 { host, calls, confirms }。
 *
 * ``calls`` 记录被调用的接口（用来断言 apply 只在用户确认后才发），
 * ``confirms`` 记录弹过的确认框与用户的回答。
 *
 * ``preloadZh=false`` 复刻**浏览器**的顺序：i18n.js 先执行（此时没有文案表），
 * app.js 解析完，最后才由 initLanguage() 的 i18n.load() 填表。模块级
 * ``const ... = t(...)`` 在这个顺序下会把 key 名冻住（WS-541 must fix 3）。
 */
function loadApp({ preloadZh = true } = {}) {
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
  if (preloadZh) {
    sandbox.i18n.setMessages(
      JSON.parse(readFileSync(new URL("../locales/zh-CN.json", STATIC_DIR), "utf8")), "zh-CN");
  }
  run("log_follow.js");
  run("log_device.js");
  run("keys.js");

  // app.js 的顶层绑定是词法作用域，追加一段把它暴露出来（不改生产代码）。
  // 新增的绑定用 typeof 兜底：改动前的 app.js 里没有它们，测试要失败在**断言**
  // 上（而不是 ReferenceError），才看得出是「行为没实现」而不是「脚本炸了」。
  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__host = {
  state, applyAppUpdate,
  appUpdateNeedsConfirm: typeof appUpdateNeedsConfirm !== "undefined"
    ? appUpdateNeedsConfirm : null,
  appUpdateConfirmText: typeof appUpdateConfirmText !== "undefined"
    ? appUpdateConfirmText : null,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  assert.ok(host, "app.js 没有导出宿主函数");

  // 记录接口调用与确认框。
  const calls = [];
  const confirms = [];
  sandbox.api = async (path, opts) => {
    calls.push({ path: String(path), opts: opts || {} });
    return { ok: true, task: "app-1" };
  };
  sandbox.confirm = (message) => {
    confirms.push(String(message));
    return sandbox.__confirmAnswer !== false;
  };
  sandbox.__confirmAnswer = true;

  return { host, calls, confirms, sandbox };
}

/* ---------------- 用例 ---------------- */

test("设备已连接时点「立即更新」先弹确认，取消就不发请求", async () => {
  const { host, calls, confirms, sandbox } = loadApp();
  host.state.devices = [{ dev_id: "t1", connected: true }];
  sandbox.__confirmAnswer = false;

  await host.applyAppUpdate();

  assert.equal(confirms.length, 1, "有设备连着时必须先弹一次确认");
  assert.match(confirms[0], /更新会退出上位机/);
  assert.equal(
    calls.filter((c) => c.path === "/api/app/update/apply").length, 0,
    "用户取消后不能调用 apply 接口"
  );
});

test("用户确认后才调用 apply 接口", async () => {
  const { host, calls, confirms } = loadApp();
  host.state.devices = [{ dev_id: "t1", connected: true }];

  await host.applyAppUpdate();

  assert.equal(confirms.length, 1, "必须先弹确认");
  const applied = calls.filter((c) => c.path === "/api/app/update/apply");
  assert.equal(applied.length, 1, "确认后必须调用 apply 接口一次");
  assert.equal(applied[0].opts.method, "POST");
});

test("没有设备连着时不打扰用户，直接更新", async () => {
  const { host, calls, confirms } = loadApp();
  host.state.devices = [{ dev_id: "t1", connected: false }];

  await host.applyAppUpdate();

  assert.equal(confirms.length, 0, "没有设备连着时不该弹确认");
  assert.equal(
    calls.filter((c) => c.path === "/api/app/update/apply").length, 1,
    "没设备连着时应该直接更新"
  );
});

test("正在 OTA 时不弹确认（服务端的 E_OTA_BUSY 硬拦保持原样）", async () => {
  const { host, calls, confirms } = loadApp();
  host.state.devices = [{ dev_id: "t1", connected: true }];
  host.state.ota = { status: "uploading" };

  await host.applyAppUpdate();

  assert.equal(confirms.length, 0, "OTA 进行中不该再弹确认（服务端会硬拦）");
  assert.equal(
    calls.filter((c) => c.path === "/api/app/update/apply").length, 1,
    "OTA 中的拦截由服务端负责，前端照常请求"
  );
});

/* WS-541 must fix 3：确认文案在浏览器里曾经渲染成原始 key。
 *
 * 浏览器分支下 i18n.js 没有 require，messages 初始是空表；app.js 里
 * `const APP_UPDATE_CONFIRM_TEXT = t("update.confirm_quit")` 在解析时就执行，
 * 于是常量里冻住的是 "update.confirm_quit"，确认框弹出来就是这串英文。
 * 旧用例因为先 setMessages 再跑 app.js，正好绕过了这个顺序（假绿）。
 */
test("语言表晚于 app.js 加载时，确认文案仍是句子而不是 key", () => {
  const { host, sandbox } = loadApp({ preloadZh: false });
  assert.equal(
    typeof host.appUpdateConfirmText, "function",
    "确认文案必须是函数：模块级 const 会在 i18n.load() 之前把 key 冻住"
  );

  // 复刻 initLanguage()：app.js 已解析完，这时 i18n.load() 才填表。
  sandbox.i18n.setMessages(
    JSON.parse(readFileSync(new URL("../locales/en-US.json", STATIC_DIR), "utf8")), "en-US");
  const text = host.appUpdateConfirmText();
  assert.notEqual(text, "update.confirm_quit");
  assert.match(text, /Updating quits the host/);
});
