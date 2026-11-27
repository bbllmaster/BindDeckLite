// 前端回归用例共享的最小 DOM 桩 + 生产 app.js 加载器。
//
// 只放「怎么把生产脚本跑起来」这件事，不含任何断言：加载的是**生产**
// static/app.js / i18n.js / log_*.js，测试断言的也就是浏览器里跑的同一份
// 代码，改坏生产实现会真的变红。多个用例文件共用它，避免各抄一份桩之后
// 只有一份跟着新 DOM 走。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

export const HOST_DIR = new URL("./", import.meta.url);
export const STATIC_DIR = new URL("./static/", import.meta.url);

// CI 在 windows-latest 上跑（core.autocrlf=true），文本文件会以 CRLF 落盘。
// 结构 / 逐字断言都以仓库里的 LF 原文为基准，读入时先归一化。
export const normalizeEol = (text) => text.replace(/\r\n/g, "\n");
export const readText = (rel) => normalizeEol(readFileSync(new URL(rel, HOST_DIR), "utf8"));

/** 最小 DOM 节点：只实现生产脚本真正用到的属性和方法。 */
export function makeEl(sel = "") {
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
    remove() { if (this.parentNode) this.removeChild(this); },
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
  Object.defineProperty(el, "options", {
    get() { return this.children.filter((c) => c.tagName === "OPTION"); },
  });
  return el;
}

/** 建一个 .help-section：自带若干 h3 标题与 id。 */
export function helpSection(id, titles) {
  const sec = makeEl("section#" + id);
  sec.id = id;
  sec._qa.h3 = titles.map((text) => {
    const h = makeEl("h3");
    h.textContent = text;
    return h;
  });
  return sec;
}

/**
 * 载入生产脚本，返回可断言的入口与 DOM registry。
 *
 * `opts.fetch` 可换成测试自己的实现（例如挂起的 promise 或记录调用次序的
 * fetch）；默认在桩里直接抛错，避免用例悄悄打真网络。
 */
export function loadApp(opts = {}) {
  const registry = {};
  const reg = (sel) => (registry[sel] = registry[sel] || [makeEl(sel)]);
  [
    "#logList", "#logEmpty", "#logDev", "#logDevLabel", "#logKind",
    "#logSearch", "#logLevelChips", "#btnLogFilterReset", "#btnLogEmptyReset",
    "#btnLogMoreFilters", "#logMoreFilters", "#btnLogExport", "#helpModal",
    "#btnHelp", "#btnHelpClose", "#helpSearch", "#helpToc",
    // WS-706: 掉线通知那一块（模板里带 hidden，渲染时按 tray_active 翻转）。
    "#notifyDisconnectBlock", "#notifyDisconnectToggle",
  ].forEach(reg);

  const sections = [
    helpSection("helpSecMacro", ["宏怎么用"]),
    helpSection("helpSecKeys", ["键名对照表"]),
    helpSection("helpSecExamples", ["完整示例：用 Chrome 打开页面并登录", "完整示例：启动桌面应用并自动输入"]),
    helpSection("helpSecPassword", ["密码框输入无效怎么办"]),
    helpSection("helpSecErrors", ["常见错误排查"]),
  ];
  const sectionById = new Map(sections.map((s) => [s.id, s]));
  registry[".help-section"] = sections;

  const tocLinks = sections.map((s) => {
    const link = makeEl("a");
    link.setAttribute("href", "#" + s.id);
    return link;
  });
  registry["#helpToc"][0]._qa[".help-toc-link"] = tocLinks;

  const document = {
    body: makeEl("body"),
    documentElement: makeEl("html"),
    querySelector(sel) { return registry[sel] ? registry[sel][0] : makeEl(sel); },
    querySelectorAll(sel) { return registry[sel] || []; },
    getElementById(id) { return sectionById.get(id) || (registry["#" + id] ? registry["#" + id][0] : makeEl("#" + id)); },
    createElement(tag) { return makeEl(tag); },
    createTextNode(t) { return { textContent: t }; },
    addEventListener() {}, removeEventListener() {},
  };
  const sandbox = {
    document,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    console,
    setTimeout: () => 0, clearTimeout() {},
    setInterval: () => 0, clearInterval() {},
    fetch: opts.fetch || (async () => { throw new Error("no network in the harness"); }),
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
  run("log_export.js");
  run("reconnect_sync.js");
  run("cfg_status.js");
  run("keys.js");

  // 只暴露用例要用的入口：仍然是生产函数本体，不是复制品。
  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__host = {
  state, bindLogFilters, bindHelp, setLogMoreFiltersOpen, resetLogFilters,
  syncLogFilterChrome, filterHelp, logFilterActive, buildLogRow,
  exportVisibleLog, retranslateLog, applyLogFilter, renderDesktop,
  isLogExportBusy: () => logExportBusy,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  assert.ok(host, "app.js 没有导出测试入口");
  const el = (sel) => registry[sel][0];
  return { host, el, registry, sections, tocLinks, sandbox, document };
}
