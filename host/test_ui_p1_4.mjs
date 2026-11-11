// WS-587 P1-4 前端回归用例：日志两级过滤 / 空态清除 / 3 列日志行、
// 帮助两栏目录 + 搜索 + 内容完整性 / 检查更新弹窗结构。
//
// 运行：node --test host/test_ui_p1_4.mjs
//
// 行为断言加载**生产** static/app.js 在最小 DOM 上真跑 bindLogFilters /
// setLogMoreFiltersOpen / resetLogFilters / syncLogFilterChrome / filterHelp /
// buildLogRow；结构断言直接读**生产** index.html / help.html / style.css /
// locales，不复制任何生产逻辑。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const HOST_DIR = new URL("./", import.meta.url);
const STATIC_DIR = new URL("./static/", import.meta.url);
// 拆分前没有 help.html：容错读取，让本文件在「修复前」的 worktree 上也能加载，
// 由具体断言报出缺失的项（而不是整个文件在 import 阶段就崩掉）。
const readIfExists = (rel) => {
  try { return readFileSync(new URL(rel, HOST_DIR), "utf8"); } catch (_) { return ""; }
};
const INDEX_HTML = readFileSync(new URL("templates/index.html", HOST_DIR), "utf8");
const HELP_HTML = readIfExists("templates/help.html");
const STYLE_CSS = readFileSync(new URL("static/style.css", HOST_DIR), "utf8")
  .replace(/\/\*[\s\S]*?\*\//g, "");
const LOCALES = JSON.parse(readFileSync(new URL("locales/zh-CN.json", HOST_DIR), "utf8"));

const read = (rel) => readFileSync(new URL(rel, HOST_DIR), "utf8");
const count = (text, re) => (text.match(re) || []).length;

/* ---------------- 最小 DOM 桩 ---------------- */

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
  Object.defineProperty(el, "options", {
    get() { return this.children.filter((c) => c.tagName === "OPTION"); },
  });
  return el;
}

/** 建一个 .help-section：自带若干 h3 标题与 id。 */
function helpSection(id, titles) {
  const sec = makeEl("section#" + id);
  sec.id = id;
  sec._qa.h3 = titles.map((text) => {
    const h = makeEl("h3");
    h.textContent = text;
    return h;
  });
  return sec;
}

/** 载入生产脚本，返回可断言的入口与 DOM registry。 */
function loadApp() {
  const registry = {};
  const reg = (sel) => (registry[sel] = registry[sel] || [makeEl(sel)]);
  [
    "#logList", "#logEmpty", "#logDev", "#logDevLabel", "#logKind",
    "#logSearch", "#logLevelChips", "#btnLogFilterReset", "#btnLogEmptyReset",
    "#btnLogMoreFilters", "#logMoreFilters", "#helpModal", "#btnHelp",
    "#btnHelpClose", "#helpSearch", "#helpToc",
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

  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__host = {
  state, bindLogFilters, bindHelp, setLogMoreFiltersOpen, resetLogFilters,
  syncLogFilterChrome, filterHelp, logFilterActive, buildLogRow,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  assert.ok(host, "app.js 没有导出 P1-4 入口");
  const el = (sel) => registry[sel][0];
  return { host, el, registry, sections, tocLinks, sandbox, document };
}

/* ---------------- 日志：两级过滤 ---------------- */

test("两级过滤：更多筛选展开 / 收起，按钮 aria-expanded 同步", () => {
  const { host, el } = loadApp();
  const panel = el("#logMoreFilters");
  const btn = el("#btnLogMoreFilters");

  // init() 里已跑过 bindLogFilters；这里显式再绑一次，保证断言的是生产绑定。
  host.bindLogFilters();
  assert.equal(host.state.logMoreOpen, false, "默认收起");
  assert.equal(panel.hidden, true);
  assert.equal(btn.getAttribute("aria-expanded"), "false");

  btn.onclick();
  assert.equal(host.state.logMoreOpen, true, "点一次展开");
  assert.equal(panel.hidden, false);
  assert.equal(btn.getAttribute("aria-expanded"), "true");

  btn.onclick();
  assert.equal(host.state.logMoreOpen, false, "再点一次收起");
  assert.equal(panel.hidden, true);
  assert.equal(btn.getAttribute("aria-expanded"), "false");

  host.setLogMoreFiltersOpen(true);
  assert.equal(panel.hidden, false, "生产函数直接调用也要生效");
});

test("清除全部过滤：空态出现，点按钮后过滤条件 / 控件 / 空态一起复位", () => {
  const { host, el, registry } = loadApp();
  host.bindLogFilters();

  const list = el("#logList");
  const empty = el("#logEmpty");
  const reset = el("#btnLogFilterReset");
  const emptyReset = el("#btnLogEmptyReset");
  const chips = el("#logLevelChips");
  const search = el("#logSearch");
  const kind = el("#logKind");

  // 造 3 行全被过滤隐藏，并给出一组「全开」的过滤条件。
  for (let i = 0; i < 3; i++) {
    const row = makeEl("div.log-row");
    row.hidden = true;
    row.dataset = { kind: "key", level: "info", dev: "" };
    list.appendChild(row);
  }
  host.state.logFilter = { kind: "cfg", level: "error", q: "abc", dev: "d1" };
  search.value = "abc";
  kind.value = "cfg";
  const chipEls = ["", "info", "ok", "warn", "error"].map((lvl) => {
    const b = makeEl("button.log-chip");
    b.dataset.level = lvl;
    chips.appendChild(b);
    return b;
  });
  registry[".log-chip"] = chipEls;
  chips._qa[".log-chip"] = chipEls; // $$(".log-chip", chips) 走的是容器内查询
  host.syncLogFilterChrome();

  assert.equal(host.logFilterActive(), true);
  assert.equal(empty.hidden, false, "有内容但全被过滤隐藏时空态必须出现");
  assert.equal(reset.hidden, false, "折叠区的清除按钮也该出现");
  assert.equal(el("#btnLogMoreFilters").classList.contains("active"), true,
    "二级过滤生效时开关要标记 active");

  // 空态「清除全部过滤」与折叠区「清除过滤」共用同一个生产函数。
  assert.equal(emptyReset.onclick, reset.onclick, "两个入口必须绑到同一份重置逻辑");
  emptyReset.onclick();

  // vm 里的对象与测试不在同一 realm，展开成测试侧对象再比结构。
  assert.deepEqual({ ...host.state.logFilter }, { kind: "", level: "", q: "", dev: "" });
  assert.equal(host.logFilterActive(), false);
  assert.equal(search.value, "");
  assert.equal(kind.value, "");
  assert.equal(empty.hidden, true, "过滤清空后空态收起");
  assert.equal(reset.hidden, true);
  assert.equal(el("#btnLogMoreFilters").classList.contains("active"), false);
  assert.equal(chipEls.filter((b) => b.classList.contains("active")).length, 1,
    "只剩「全部」胶囊选中");
  assert.equal(chipEls[0].getAttribute("aria-pressed"), "true");
});

/* ---------------- 日志行：3 列 ---------------- */

test("buildLogRow 产出 3 列：时间 / 内容(类型徽标+查看更多) / 复制", () => {
  const { host } = loadApp();
  const row = host.buildLogRow(
    { time: "12:00:00", kind: "key", text: "按键 3", dev: "", level: "info" }, false);
  const html = row.innerHTML;

  assert.match(html, /^<span class="log-time">12:00:00<\/span>/);
  assert.match(html, /<div class="log-body">/, "内容列由 .log-body 包住");
  assert.match(html, /<span class="log-kind">key<\/span>/, "类型仍是可识别的徽标节点");
  assert.match(html, /<span class="log-text">按键 3<\/span>/);
  assert.match(html, /<details class="log-more" hidden>/, "单设备时查看更多默认收起");
  assert.match(html, /<summary>查看更多<\/summary>/, "summary 用生产 i18n 文案");
  assert.match(html, /<span class="log-dev"><\/span>/, "设备归属在查看更多里，不是常驻列");
  assert.equal(count(html, /<div class="log-body">/g), 1);
  assert.equal(count(html, /<span class="log-time">/g), 1);
  assert.equal(count(html, /class="log-copy"/g), 0, "复制按钮是 appendChild，不在 innerHTML 里");
  assert.equal(row.children.length, 1, "第 3 列复制按钮由 appendChild 补上");
  assert.equal(row.children[0].className, "log-copy");

  const withDev = host.buildLogRow(
    { time: "12:00:01", kind: "cfg", text: "已下发", dev: "A1", level: "ok" }, true);
  assert.match(withDev.innerHTML, /<details class="log-more">/, "多设备时查看更多可展开");
});

/* ---------------- 帮助：目录锚点 + 搜索 ---------------- */

test("帮助搜索：按段落标题过滤章节，目录项跟着显隐；清空后全部恢复", () => {
  const { host, sections, tocLinks } = loadApp();

  host.filterHelp("键名");
  assert.deepEqual(sections.map((s) => s.hidden), [true, false, true, true, true]);
  assert.deepEqual(tocLinks.map((l) => l.hidden), [true, false, true, true, true]);

  // 示例分组有两个 h3，命中其中任意一个都应保留整组。
  host.filterHelp("启动桌面应用");
  assert.equal(sections[2].hidden, false, "第二个示例标题也要能搜到整组");
  assert.equal(sections[0].hidden, true);

  host.filterHelp("");
  assert.ok(sections.every((s) => !s.hidden), "清空搜索恢复全部章节");
  assert.ok(tocLinks.every((l) => !l.hidden), "目录项同步恢复");
});

test("生产 index.html：帮助由 {% include %} 引入，搜索 / 目录 / 空态 ID 齐备", () => {
  assert.match(INDEX_HTML, /\{%\s*include\s+"help\.html"\s*%\}/,
    "帮助正文必须走 Flask include，不再内联");
  for (const id of ["helpModal", "helpTitle", "btnHelpClose", "helpSearch",
                    "logEmpty", "btnLogEmptyReset", "btnLogMoreFilters", "logMoreFilters"]) {
    assert.equal(count(INDEX_HTML, new RegExp(`id="${id}"`, "g")), 1, `缺少或重复 id=${id}`);
  }
  assert.equal(count(INDEX_HTML, /<div class="modal-body">[\s\S]*?help-table/g), 0,
    "帮助表格不应再内联在 index.html");
});

test("生产 help.html：5 个目录锚点都有对应章节，内容一个字都没少", () => {
  const links = HELP_HTML.match(/<a href="#([^"]+)" class="help-toc-link[^"]*"[^>]*>/g) || [];
  assert.equal(links.length, 5, "左侧目录固定 5 项");
  const hrefs = links.map((l) => l.match(/href="#([^"]+)"/)[1]);
  for (const id of hrefs) {
    assert.equal(count(HELP_HTML, new RegExp(`<section class="help-section" id="${id}"`, "g")), 1,
      `目录锚点 #${id} 没有对应章节`);
  }
  assert.equal(count(HELP_HTML, /<section class="help-section"/g), 5);

  // 与拆分前 index.html 的内容基线比对：表格 / 代码块 / <code> 数不减少。
  assert.ok(count(HELP_HTML, /<table class="help-table"/g) >= 3, "键名/宏/密码 3 张表都在");
  assert.ok(count(HELP_HTML, /<pre class="help-code">/g) >= 3, "3 段宏示例都在");
  assert.ok(count(HELP_HTML, /<code>/g) >= 142, "行内 code 标记不减少");
  for (const key of ["help.h_macro", "help.h_keys", "help.h_example_browser",
                     "help.h_example_app", "help.h_password", "help.h_errors",
                     "help.macro_11", "help.keys_13", "help.password_26",
                     "help.errors_14", "help.example_app_2"]) {
    assert.match(HELP_HTML, new RegExp(`data-i18n(?:-html)?="${key}"`), `帮助内容缺 ${key}`);
  }
  // <pre> 里的宏步骤是字面量，缩进一个空格都会改变用户看到的示例。
  assert.ok(HELP_HTML.includes("browser:chrome\nurl:https://example.com/login\nwait:1500\nkey:tab\npaste:my-user\nkey:tab\npaste:my-password\nkey:enter"),
    "宏代码块内容必须与原文逐字一致");
});

test("生产 help.html 的每个 data-i18n key 都在 locale 表里", () => {
  const keys = [...HELP_HTML.matchAll(/data-i18n(?:-html)?="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(keys.length >= 81, "帮助正文的 i18n key 不应在迁移中丢失");
  for (const key of keys) {
    assert.ok(Object.prototype.hasOwnProperty.call(LOCALES, key), `locale 缺 ${key}`);
  }
});

/* ---------------- 检查更新弹窗 ---------------- */

test("生产 index.html：更新弹窗两块结构 / role=alert / 当前→目标对比行", () => {
  assert.equal(count(INDEX_HTML, /<section class="update-block"/g), 2, "仍是上位机 + 固件两块");
  assert.equal(count(INDEX_HTML, /class="upd-version-line"/g), 2, "两块都有当前→目标对比行");
  for (const id of ["updAppCurrent", "updAppLatest", "updFwCurrent", "updFwLatest"]) {
    assert.equal(count(INDEX_HTML, new RegExp(`id="${id}"`, "g")), 1, `${id} 绑定不能丢`);
  }
  // OTA 向导里已有一个 role=alert 的警告（WS-577），这里只认两个更新错误块。
  assert.equal(count(INDEX_HTML, /<p class="ota-error update-error"[^>]*role="alert"[^>]*>/g), 2,
    "两个更新错误块都要带 role=alert");
  assert.match(INDEX_HTML, /id="updAppError" role="alert" hidden/);
  assert.match(INDEX_HTML, /id="updFwError" role="alert" hidden/);
  assert.equal(count(INDEX_HTML, /class="update-foot"/g), 2, "两块都有固定底部的 foot");
  assert.equal(count(INDEX_HTML, /class="upd-sha"/g), 2, "SHA 用独立 class 以便 mono 12px");
  assert.equal(count(INDEX_HTML, /class="[^"]*mono[^"]*upd-sha/g), 0,
    "SHA 不再挂 .mono（.mono 的 --fs-cap 是 11px，会盖掉 12px）");
});

/* ---------------- 样式基线 ---------------- */

/** 取一条 CSS 规则的声明块。 */
function cssBlock(selector) {
  const at = STYLE_CSS.indexOf(selector);
  assert.ok(at >= 0, "找不到 CSS 规则：" + selector);
  const open = STYLE_CSS.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < STYLE_CSS.length; i++) {
    if (STYLE_CSS[i] === "{") depth++;
    else if (STYLE_CSS[i] === "}") {
      depth--;
      if (depth === 0) return STYLE_CSS.slice(open + 1, i);
    }
  }
  throw new Error("CSS 规则没有闭合：" + selector);
}

test("生产 style.css：log-copy 对比度 / 热区、3 列日志行、块间距、SHA mono 12px", () => {
  const copy = cssBlock(".log-copy {");
  assert.match(copy, /opacity:\s*1\b/, ".log-copy 常态不透明度必须是 1.0（6.2:1）");
  assert.match(copy, /min-height:\s*32px\b/, "复制热区 28 → 32px");

  const row = cssBlock(".log-row {");
  assert.match(row, /grid-template-columns:\s*62px minmax\(0, 1fr\) auto/,
    "日志行 3 列，时间列 62px");
  assert.match(STYLE_CSS, /\.log-more\[hidden\]/, "查看更多可整体收起");
  assert.match(cssBlock(".log-kind {"), /border-radius:\s*var\(--r-pill\)/, "类型改徽标");

  const block = cssBlock(".update-block {");
  assert.match(block, /margin-bottom:\s*var\(--sp-6\)/, "两块间距用 --sp-6");
  assert.match(block, /border-bottom:\s*1px solid var\(--line\)/, "两块之间 1px 分隔线");
  assert.match(block, /display:\s*flex/, "块是 flex 列，foot 才能贴底");
  assert.match(cssBlock(".update-foot {"), /margin-top:\s*auto/, "进度/错误/按钮固定在块底");
  assert.match(cssBlock(".upd-progress-slot {"), /min-height:/, "进度槽预留高度，切换不跳动");
  assert.match(cssBlock(".update-error {"), /display:\s*inline-block/, "错误是行内块");

  const sha = cssBlock(".upd-sha {");
  assert.match(sha, /font-family:\s*ui-monospace/, "SHA256 用 mono");
  assert.match(sha, /font-size:\s*var\(--fs-sm\)/, "SHA256 用 12px（--fs-sm）");
  assert.match(cssBlock(".help-layout {"), /grid-template-columns:\s*168px minmax\(0, 1fr\)/,
    "帮助两栏：固定目录 + 内容");
  assert.match(cssBlock(".modal-card {"), /width:\s*min\(900px, 100%\)/, "帮助弹窗 760 → 900");
  assert.match(cssBlock(".help-toc {"), /position:\s*sticky/, "左侧目录固定");
});
