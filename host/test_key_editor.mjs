// WS-585 按键页「键位列表 + 常驻检查器」的前端回归用例。
//
// 运行：node --test host/test_key_editor.mjs
//
// 背景：按键页从「10 张摘要卡 + 右侧抽屉」改成「键位行列表 + 常驻检查器」。
// 这一改动的风险点不是样式，而是三件容易静默坏掉的事：
//   1. 10 / 12 个输入必须都渲染出来（少一行就等于丢了一个键）；
//   2. 行尾「测试」必须仍是 hover / 键盘 focus 可见、且仍叫 .test，
//      否则 setCardRunning 找不到按钮，停止入口消失；
//   3. 列表与检查器必须是同一份 DOM —— 两套内容同时可见就等于两个编辑入口。
//
// 这里加载**生产脚本** host/static/keys.js + app.js 在假 DOM 上真跑
// renderKeys / openKeyDrawer / applyRunningState，并直接读生产 style.css
// 断言可⻅性规则；没有一份逻辑是从生产代码抄过来的副本。
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
    // createElement 出来的节点在同一个选择器上必须拿到同一个桩：
    // setCardRunning 反复取 .test 并改写它的文案，换新对象就丢了状态。
    querySelector(s) {
      if (!this._q) this._q = new Map();
      if (!this._q.has(s)) this._q.set(s, makeEl(s));
      return this._q.get(s);
    },
    querySelectorAll() { return []; },
    get options() { return []; },
    getAttribute() { return null; },
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    insertAdjacentHTML() {},
    closest() { return null; },
    remove() {},
    getBoundingClientRect() { return { top: 0, bottom: 0, height: 0, width: 0 }; },
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
  };
  // 真实 DOM 里 className 与 classList 是同一份状态；生产代码两种写法都用
  // （card.className = "keycard …" 之后又要 classList.contains("press")）。
  // 生产代码用 grid.innerHTML = "" 清空列表：桩必须跟着清掉子节点，
  // 否则第二次 render 会翻倍（10 → 20 行）。
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

/** 等一轮宏任务，让 init() 的串行 await 跑到 bindKeyDrawer。 */
const flush = () => new Promise((r) => setImmediate(r));

/** 载入生产 script，返回可断言的宿主对象。 */
async function loadApp() {
  const registry = {};
  const body = makeEl("body");
  const docListeners = {};
  const grid = makeEl("#keyGrid");
  const gridCards = () => grid.children.filter((c) => c.classList.contains("keycard"));

  const document = {
    body,
    documentElement: makeEl("html"),
    querySelector(sel) {
      if (registry[sel]) return registry[sel][0];
      if (sel === "#keyGrid") return grid;
      // 生产 keys.js 的 refreshKeyCard() 用 `.keycard[data-idx="N"]` 找卡片；
      // 桩若每次都新建节点，改写就会落到一个游离对象上，摘要文本永远断言不到。
      const card = /^\.keycard\[data-idx="(\d+)"\]$/.exec(sel);
      if (card) {
        return gridCards()[parseInt(card[1], 10)] || makeEl(sel);
      }
      return makeEl(sel);
    },
    querySelectorAll(sel) {
      if (sel === ".keycard") return gridCards();
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

  run("i18n.js");
  sandbox.i18n.setMessages(
    JSON.parse(readFileSync(new URL("../locales/zh-CN.json", STATIC_DIR), "utf8")), "zh-CN");
  run("log_follow.js");
  run("log_device.js");
  run("log_export.js");
  run("reconnect_sync.js");
  run("cfg_status.js");
  run("keys.js");

  // 弹窗默认收起，否则 bindKeyDrawer 的 Esc 会以为有模态弹窗打开而提前返回。
  ["#helpModal", "#otaModal", "#updateModal"].forEach((sel) => {
    const m = makeEl(sel);
    m.hidden = true;
    registry[sel] = [m];
  });
  ["#keyHint", "#keyDrawer", "#keyDrawerBody", "#keyDrawerTitle", "#kdTest",
    "#btnKeyDrawerClose", "#keyDrawerBackdrop"].forEach((sel) => {
    registry[sel] = [makeEl(sel)];
  });
  registry["#keyDrawerBackdrop"][0].hidden = true;

  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__host = {
  state, renderKeys, applyRunningState, highlightKeys, openKeyDrawer, closeKeyDrawer,
  setCardRunning, inputCount, keyHidText, syncKeySelection,
  ensureModsCapability, redetectModsCapability,
  // WS-661：旧（修复前）app.js 没有这个函数，导出 null 让用例断言失败，
  // 而不是让整份 harness 在 eval 阶段就抛 ReferenceError。
  ensureMediaCapability: typeof ensureMediaCapability === "function" ? ensureMediaCapability : null,
  // WS-665：切语言时两组 optgroup 的 label 要分别换，不能只改第一个。
  retranslateKeyDrawer: typeof retranslateKeyDrawer === "function" ? retranslateKeyDrawer : null,
  // WS-665：判成不支持后要有「重新检测」入口（?reset=1）。
  redetectMediaCapability: typeof redetectMediaCapability === "function" ? redetectMediaCapability : null,
};
`, sandbox, { filename: "app.js" });

  const host = sandbox.__host;
  // 服务端返回的键码表：renderKeys 只按它取标签。
  host.state.keycodes = [
    { code: 0, label: "不发键" },
    ...[240, 241, 242, 243, 244, 245, 246, 247, 248, 249, 250, 251].map((code, i) => ({
      code, label: "F" + (13 + i),
    })),
  ];
  host.state.modDefs = [
    { id: "modCtrl", bit: 1, label: "Ctrl" },
    { id: "modShift", bit: 2, label: "Shift" },
    { id: "modAlt", bit: 4, label: "Alt" },
    { id: "modWin", bit: 8, label: "Win" },
  ];
  sandbox.api = async (path) => {
    if (String(path) === "/api/keycodes") return { keys: host.state.keycodes, mods: host.state.modDefs };
    return { ok: true };
  };
  host.state.selected = "t1";
  host.state.preview = false;
  // init() 是串行的 async：跑完它才注册好 document 上的 Esc 监听。
  // 首轮 pollStatus 会因为「没有设备」把 selected 清空，这里再钉回来。
  await flush();
  await flush();
  host.state.selected = "t1";
  host.state.preview = false;   // 没有设备时 pollStatus 会置 true，钉回「已在编辑」
  host.state.cfg = null;

  return { host, sandbox, grid, document, docListeners, registry };
}

/** 造一份 n 个输入的设备配置；actions 里给第 3 个键一个宏，覆盖「有动作」分支。 */
function makeCfg(total) {
  const keys = [];
  for (let i = 0; i < total; i++) {
    keys.push({
      code: i === 9 ? 249 : 240 + i,
      label: i === 9 ? "F22" : "F" + (13 + i),
      mods: i === 1 ? 3 : 0,
      action: i === 2
        ? { type: "macro", value: "key:Tab", macro: [{ type: "key", value: "Tab" }] }
        : { type: "none", value: "", macro: [] },
    });
  }
  return { layout: 1, keys, pins: [], encoder: { mode: 0, actions: {} }, chip: "C3" };
}

/* ---------------- 用例：渲染 ---------------- */

test("编码器布局：渲染 10 个输入，每行是「编号|键名|HID|动作|状态点」", async () => {
  const { host, grid } = await loadApp();
  host.state.cfg = makeCfg(10);
  host.renderKeys();

  assert.equal(grid.children.length, 10, "10 键布局必须渲染 10 行");

  const row = grid.children[0].innerHTML;
  assert.match(row, /class="kc-main edit"/, "整行必须是编辑入口");
  assert.match(row, /class="kc-num"/);
  assert.match(row, /class="kc-title"/);
  assert.match(row, /<kbd class="kc-hid">/, "HID 键码用 kbd 胶囊承载");
  assert.match(row, /class="kc-act"/);
  assert.match(row, /class="kc-live"/, "状态点必须还在（press/running 靠它发光）");
  assert.match(row, /class="kc-test test"/, "行尾测试入口仍叫 .test");
  assert.doesNotMatch(row, /class="btn/, "常驻按钮降为 0：行内不再有 .btn");
});

test("12 键布局：渲染 12 个输入，第 10–12 行标成扩展键", async () => {
  const { host, grid } = await loadApp();
  const cfg = makeCfg(12);
  cfg.layout = 0;
  host.state.cfg = cfg;
  host.renderKeys();

  assert.equal(grid.children.length, 12);
  assert.equal(host.inputCount(), 12);
  assert.ok(grid.children[9].classList.contains("extended"));
  assert.ok(!grid.children[8].classList.contains("extended"));
});

/* ---------------- 用例：测试 icon 的 hover / focus 可见性 ---------------- */

/** 读生产样式表；先剥掉注释，否则规则选择器会带上上一段注释的尾巴。 */
function readCss() {
  return readFileSync(new URL("style.css", STATIC_DIR), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "");
}

function cssBlock(css, marker) {
  const at = css.indexOf(marker);
  assert.ok(at >= 0, "找不到 CSS 片段：" + marker);
  const open = css.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === "{") depth++;
    else if (css[i] === "}") {
      depth--;
      if (depth === 0) return css.slice(open + 1, i);
    }
  }
  throw new Error("CSS 片段没有闭合：" + marker);
}

/** 取某条规则（选择器可写多个，逗号分隔）在给定 CSS 文本里的声明。 */
function declsFor(css, selector) {
  const out = [];
  const rule = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = rule.exec(css))) {
    const sels = m[1].split(",").map((s) => s.trim());
    if (sels.includes(selector)) out.push(m[2]);
  }
  return out.join(";");
}

test("行尾测试 icon：默认不可见，hover / 键盘 focus / 执行中显形", () => {
  const css = readCss();

  assert.match(declsFor(css, ".kc-test"), /opacity:\s*0\b/,
    "测试 icon 默认必须收起（10 个常驻按钮的观感就是这一步去掉的）");
  assert.match(declsFor(css, ".kc-test:focus-visible"), /opacity:\s*1\b/,
    "键盘 focus 必须让 icon 显形，否则 Tab 过去看不见也点不到");
  assert.match(css, /\.keycard:hover \.kc-test/, "hover 行时显示测试 icon");
  assert.match(declsFor(css, ".keycard.running .kc-test"), /opacity:\s*1\b/,
    "执行中/可停止时 icon 必须常驻，不能藏起来");
});

/* ---------------- 用例：检查器与抽屉互斥 ---------------- */

test("检查器与窄屏抽屉是同一份 DOM：只有一套编辑内容", () => {
  const html = readFileSync(TEMPLATE, "utf8");

  const count = (re) => (html.match(re) || []).length;
  assert.equal(count(/id="keyDrawer"/g), 1, "只能有一个 #keyDrawer");
  assert.equal(count(/id="keyDrawerBody"/g), 1, "只能有一个 #keyDrawerBody");
  assert.equal(count(/id="keysActionsHint"/g), 1, "状态行只应出现一次（已移入检查器）");

  const drawerAt = html.indexOf('id="keyDrawer"');
  const hintAt = html.indexOf('id="keysActionsHint"');
  assert.ok(hintAt > drawerAt, "「上位机动作」状态行必须在检查器内部，不再占页面顶部");

  const css = readCss();
  const wide = cssBlock(css, "@media (min-width: 1280px)");
  const narrow = cssBlock(css, "@media (max-width: 1279px)");

  assert.match(declsFor(wide, ".keys-layout"), /grid-template-columns:[^;]*380px/,
    "≥1280 是 1fr + 380px 的列表/检查器双栏");
  assert.match(declsFor(wide, "#keyDrawerBackdrop"), /display:\s*none/,
    "宽屏检查器常驻，遮罩必须禁用");
  assert.match(declsFor(narrow, ".key-drawer"), /display:\s*none/,
    "<1280 检查器默认收起，变成抽屉");
  assert.match(declsFor(narrow, ".key-drawer.open"), /display:\s*flex/,
    "只有 .open 的抽屉才出现 —— 与常驻检查器互斥");
  assert.match(declsFor(css, ".key-head"), /display:\s*none/,
    "列头只在行形态出现，卡片形态不能留下无主表头");
});

test("点一行打开检查器：选中态跟着走，关闭后清掉", async () => {
  const { host, grid, registry, docListeners } = await loadApp();
  host.state.cfg = makeCfg(10);
  host.renderKeys();

  // 选一个**有上位机动作**的键（第 3 行是宏）：检查器的「测试动作」按钮
  // 只有这一支会去读按钮标题，空动作的键走不到那里。
  const card = grid.children[2];
  card.querySelector(".edit").onclick();

  assert.equal(host.state.editingKey, 2, "行点击应把检查器切到该下标");
  assert.ok(card.classList.contains("selected"), "选中的行要有左侧色条");
  assert.ok(registry["#keyDrawer"][0].classList.contains("open"));
  assert.equal(registry["#keyDrawerBackdrop"][0].hidden, false, "窄屏抽屉需要遮罩");
  assert.match(registry["#keyDrawerBody"][0].innerHTML, /kdHid/, "检查器渲染出 HID 选择器");
  const kdTest = registry["#kdTest"][0];
  assert.equal(kdTest.textContent, "测试动作");
  assert.ok(kdTest.title, "检查器的测试按钮标题必须落到 DOM 上（曾引用未定义常量而抛错）");

  // Esc 关闭（生产 bindKeyDrawer 注册在 document 上）。
  (docListeners.keydown || []).forEach((fn) => fn({ key: "Escape" }));
  assert.equal(host.state.editingKey, null);
  assert.ok(!card.classList.contains("selected"));
  assert.ok(!registry["#keyDrawer"][0].classList.contains("open"));
  assert.equal(registry["#keyDrawerBackdrop"][0].hidden, true);
});

/* ---------------- 用例：press / running 实时反馈 ---------------- */

test("press：只有最新按下的那一行高亮", async () => {
  const { host, grid } = await loadApp();
  host.state.cfg = makeCfg(10);
  host.renderKeys();

  host.highlightKeys({ t1: { index: 3, age: 0.1 } });
  assert.ok(grid.children[3].classList.contains("press"));
  assert.ok(!grid.children[2].classList.contains("press"));

  // 0.9 秒之后不再算「刚按下」，高亮必须自己消失。
  host.highlightKeys({ t1: { index: 3, age: 1.4 } });
  assert.ok(!grid.children[3].classList.contains("press"));
});

test("running：执行中的行带 running 且行尾变成停止，结束后复原", async () => {
  const { host, grid } = await loadApp();
  host.state.cfg = makeCfg(10);
  host.renderKeys();

  host.state.running = { t1: { "2": 1.5 } };
  host.state.stopping = {};
  host.applyRunningState();

  const card = grid.children[2];
  assert.ok(card.classList.contains("running"), "执行中的行要能看出来");
  assert.equal(card.querySelector(".test").textContent, "停止", "执行中行尾就是止损入口");

  host.state.running = {};
  host.applyRunningState();
  assert.ok(!card.classList.contains("running"));
  assert.equal(card.querySelector(".test").textContent, "测试动作");
});

/* ---------------- 用例：WS-550 门控为 False 时摘要一致性 ---------------- */

test("WS-550：mods_supported=false 时摘要只显示主键（显示 == 实际下发）", async () => {
  // 直接调用生产 keys.js 的 keyHidText（由 loadApp 的 vm 桩加载，非副本）。
  const { host } = await loadApp();
  const item = { code: 240, label: "F13", mods: 1 };   // Ctrl + F13

  // state.cfg 可能为空（未选设备）：不能抛错，保持原有修饰键显示。
  host.state.cfg = null;
  assert.equal(host.keyHidText(item), "Ctrl + F13", "state.cfg 为空时保持原行为");

  // 能力未知 / 明确支持：照常显示修饰键。
  host.state.cfg = { mods_supported: true };
  assert.equal(host.keyHidText(item), "Ctrl + F13", "支持时显示 Ctrl");
  host.state.cfg = {};
  assert.equal(host.keyHidText(item), "Ctrl + F13", "unknown 时照常显示");

  // 关键回归：mods_supported=false 时 apply_device 一条 CFG:MODS 都不发，
  // 摘要绝不能再显示「Ctrl」（修复前这里返回 "Ctrl + F13"）。
  host.state.cfg = { mods_supported: false };
  assert.equal(host.keyHidText(item), "F13",
    "门控为 False 时摘要只显示主键，不出现「显示 Ctrl、实际不发」");
});

/* ---------------- 用例：WS-618 结论写回 / 重新检测 ---------------- */

test("WS-618：结论由 unknown 写回 false 后摘要卡立即只显示主键", async () => {
  // 生产入口：app.js 的 ensureModsCapability()（vm 桩加载，不是副本）。
  // 修复前它只调 renderKeyDrawer()，抽屉关着时摘要卡一直停在「Ctrl + F13」。
  const { host, sandbox, grid } = await loadApp();
  const cfg = makeCfg(10);
  cfg.mods_supported = null;          // VERSION 变化后重探的初始态
  cfg.keys[0].mods = 1;               // Ctrl + F13
  host.state.cfg = cfg;
  host.state.editingKey = null;       // 抽屉关着：只有摘要卡会变
  host.renderKeys();
  assert.equal(grid.children[0].querySelector(".kc-hid").textContent, "Ctrl + F13",
    "结论未知时摘要照常显示修饰键");

  sandbox.api = async () => ({ sent: true, supported: false, reason: "unsupported" });
  await host.ensureModsCapability();

  assert.equal(cfg.mods_supported, false);
  assert.equal(grid.children[0].querySelector(".kc-hid").textContent, "F13",
    "写回 false 后摘要卡必须重绘：显示 == 实际下发");
});

test("WS-618：旧固件点「重新检测」不会暂时打开修饰键门控", async () => {
  // 修复前 redetectModsCapability() 先把 cfg.mods_supported 清成 null，
  // 超时结果又是 null —— 抽屉与摘要都退回「支持」，apply_device 会发 CFG:MODS。
  const { host, sandbox, grid } = await loadApp();
  const cfg = makeCfg(10);
  cfg.mods_supported = false;         // 已判定旧固件
  cfg.keys[0].mods = 1;
  host.state.cfg = cfg;
  host.state.editingKey = null;
  host.renderKeys();
  assert.equal(grid.children[0].querySelector(".kc-hid").textContent, "F13");

  let calledPath = null;
  sandbox.api = async (path) => {
    calledPath = String(path);
    return { sent: true, supported: null, reason: "probe_timeout" };
  };
  await host.redetectModsCapability();

  assert.match(calledPath, /\/mods\?reset=1$/, "重新检测要带 ?reset=1");
  assert.equal(cfg.mods_supported, false, "未定论的重探必须保留原 false 结论");
  assert.equal(host.keyHidText(cfg.keys[0]), "F13",
    "重探期间/之后摘要都不能重新出现修饰键");
  assert.equal(grid.children[0].querySelector(".kc-hid").textContent, "F13");
});

/* ---------------- 用例：WS-661 按键媒体键 ---------------- */

// 服务端 /api/keycodes 里 7 个媒体键的 label（与 locales 同源）。标签文本本身
// 由生产表提供，这里只按 code 取，避免在测试里另存一份文案。
const MEDIA_CODES = [461, 438, 437, 439, 482, 489, 490];
const MEDIA_LABELS = {
  461: "播放/暂停", 438: "上一曲", 437: "下一曲", 439: "停止",
  482: "静音", 489: "音量 +", 490: "音量 −",
};

/** 往桩的键码表里补上媒体键（真实顺序：键盘 → 媒体 → 特殊 0）。 */
function withMedia(host) {
  host.state.keycodes = host.state.keycodes
    .filter((k) => k.code !== 0)
    .concat(MEDIA_CODES.map((code) => ({ code, label: MEDIA_LABELS[code] })),
            [{ code: 0, label: "不发键" }]);
}

test("WS-661：支持时下拉出现「媒体键」分组，且「特殊」仍在最后", async () => {
  const { host, sandbox } = await loadApp();
  withMedia(host);
  host.state.cfg = { media_supported: true, mods_supported: true };

  const html = sandbox.keyOptions(240);
  assert.match(html, /<optgroup[^>]*label="媒体键">/, "支持的固件要看到媒体分组");
  assert.match(html, /value="461"/, "播放/暂停 = 256 + 0xCD = 461");
  assert.match(html, />播放\/暂停</, "媒体项用中文标签，不显示数字");
  const mediaAt = html.indexOf("媒体键");
  const specialAt = html.indexOf("特殊");
  assert.ok(mediaAt > 0 && specialAt > mediaAt,
    "「特殊」必须排在媒体分组之后 —— 未知键码回退第一项时不能落到 0");
});

test("WS-661：旧固件（不支持 / 未定论）不列媒体分组，已选中的那一项仍显示", async () => {
  const { host, sandbox } = await loadApp();
  withMedia(host);

  for (const verdict of [false, null]) {
    host.state.cfg = { media_supported: verdict, mods_supported: true };
    const html = sandbox.keyOptions(240);
    assert.doesNotMatch(html, /<optgroup[^>]*label="媒体键">/,
      "没有明确 MEDIA:1 就不能让用户选媒体键");
    assert.doesNotMatch(html, /value="437"/);
  }

  // 存档里已经是媒体码时至少保留选中项，否则下拉会退回第一项、显示成 F13。
  host.state.cfg = { media_supported: false, mods_supported: true };
  const kept = sandbox.keyOptions(461);
  assert.match(kept, /value="461" selected/);
  assert.match(kept, /播放\/暂停/);
});

test("WS-661：选中媒体键时修饰键整块禁用并说明，摘要显示中文标签", async () => {
  const { host, registry } = await loadApp();
  withMedia(host);
  const cfg = makeCfg(10);
  cfg.media_supported = true;
  cfg.mods_supported = true;
  cfg.keys[0] = {
    code: 461, label: "播放/暂停", mods: 3,   // 以前可能残留 Ctrl+Shift
    action: { type: "none", value: "", macro: [] },
  };
  host.state.cfg = cfg;
  host.renderKeys();
  host.openKeyDrawer(0);

  const body = registry["#keyDrawerBody"][0].innerHTML;
  const boxes = body.match(/<input type="checkbox"[^>]*>/g) || [];
  assert.ok(boxes.length >= 4, "修饰键行仍然渲染出来（只是禁用）");
  assert.ok(boxes.every((b) => /\bdisabled\b/.test(b)),
    "媒体键没有修饰位，4 个复选框必须全部禁用");
  assert.match(body, /媒体键不支持修饰键/);
  assert.doesNotMatch(body, /当前固件不支持修饰键/,
    "这是媒体键的无修饰说明，不是旧固件的「升级固件」提示");

  // 摘要：中文标签，且绝不能出现 Ctrl（consumer 报告没有修饰位）。
  assert.equal(host.keyHidText(cfg.keys[0]), "播放/暂停");
  assert.doesNotMatch(host.keyHidText(cfg.keys[0]), /Ctrl/);
});

test("WS-661：HID 下拉切到媒体键会清掉修饰掩码", async () => {
  const { host, registry } = await loadApp();
  withMedia(host);
  const cfg = makeCfg(10);
  cfg.media_supported = true;
  cfg.mods_supported = true;
  cfg.keys[0].mods = 5;                     // Ctrl + Alt，先有旧掩码
  host.state.cfg = cfg;
  host.renderKeys();

  // 生产代码用 $("#kdHid").onchange 绑定：先把选择器登记到桩 registry，
  // 再开抽屉，才能拿到同一个节点并触发真实处理器。
  registry["#kdHid"] = [makeEl("#kdHid")];
  host.openKeyDrawer(0);
  registry["#kdHid"][0].onchange({ target: { value: "489" } });

  assert.equal(cfg.keys[0].code, 489);
  assert.equal(cfg.keys[0].label, "音量 +");
  assert.equal(cfg.keys[0].mods, 0, "媒体键不允许组合出 Ctrl+音量+");
});

test("WS-661：能力探测 True 后分组出现，False 后隐藏", async () => {
  const { host, sandbox } = await loadApp();
  withMedia(host);
  host.state.cfg = { media_supported: null, mods_supported: true };
  sandbox.api = async (path) => {
    assert.match(String(path), /\/media$/, "探测走 /api/device/<id>/media");
    return { sent: true, supported: true };
  };
  await host.ensureMediaCapability();
  assert.equal(host.state.cfg.media_supported, true);
  assert.match(sandbox.keyOptions(240), /<optgroup[^>]*label="媒体键">/);

  host.state.cfg = { media_supported: null, mods_supported: true };
  sandbox.api = async () => ({ sent: true, supported: false });
  await host.ensureMediaCapability();
  assert.equal(host.state.cfg.media_supported, false);
  assert.doesNotMatch(sandbox.keyOptions(240), /<optgroup[^>]*label="媒体键">/);
});

/* ---------------- 用例：WS-665 评审修复 ---------------- */

test("WS-665：切语言后「媒体键」与「特殊」两组 label 各自换，不会串组", async () => {
  // 修复前 retranslateKeyDrawer() 用 $("#kdHid optgroup")（querySelector 取第一个）
  // 去写「特殊」组；而 keyOptions() 现在先输出媒体组，于是媒体组被写成「特殊」，
  // 真正的特殊组停在旧语言。这里给两组各注册一个桩，按生产选择器观察。
  const { host, sandbox, registry } = await loadApp();
  withMedia(host);
  const cfg = makeCfg(10);
  cfg.media_supported = true;
  cfg.mods_supported = true;
  host.state.cfg = cfg;
  host.renderKeys();
  host.openKeyDrawer(0);

  const body = registry["#keyDrawerBody"][0];
  assert.match(body.innerHTML, /data-group="media"/, "媒体组要带 data-group 标识");
  assert.match(body.innerHTML, /data-group="special"/, "特殊组要带 data-group 标识");

  // 生产代码用属性选择器取节点：桩按同一选择器注册，才能观察到属性改写。
  const mediaOg = makeEl('optgroup[data-group="media"]');
  const specialOg = makeEl('optgroup[data-group="special"]');
  mediaOg.label = "媒体键";
  specialOg.label = "特殊";
  registry['#kdHid optgroup[data-group="media"]'] = [mediaOg];
  registry['#kdHid optgroup[data-group="special"]'] = [specialOg];

  const en = JSON.parse(
    readFileSync(new URL("../locales/en-US.json", STATIC_DIR), "utf8"));
  sandbox.i18n.setMessages(en, "en-US");
  host.retranslateKeyDrawer();

  assert.equal(mediaOg.label, "Media keys",
    "媒体组的 label 必须写成「Media keys」，不能是「Special」");
  assert.equal(specialOg.label, "Special", "特殊组也要跟着换，并且只改自己");
});

test("WS-665：判成不支持后有「重新检测」，超时不提前打开门控", async () => {
  // 修复前只有提示文字，没有调用 /media?reset=1 的入口：两次丢包就永久隐藏媒体组。
  const { host, sandbox, registry } = await loadApp();
  withMedia(host);
  const cfg = makeCfg(10);
  cfg.media_supported = false;
  cfg.mods_supported = true;
  cfg.keys[0] = {
    code: 461, label: "播放/暂停", mods: 0,
    action: { type: "none", value: "", macro: [] },
  };
  host.state.cfg = cfg;
  host.renderKeys();

  registry["#kdMediaRedetect"] = [makeEl("#kdMediaRedetect")];
  host.openKeyDrawer(0);
  const body = registry["#keyDrawerBody"][0];
  assert.match(body.innerHTML, /当前固件不支持媒体键/);
  assert.match(body.innerHTML, /id="kdMediaRedetect"/, "提示旁要有「重新检测」按钮");

  let calledPath = null;
  sandbox.api = async (path) => {
    calledPath = String(path);
    return { sent: true, supported: null, reason: "probe_timeout" };
  };
  registry["#kdMediaRedetect"][0].onclick();
  // 重探是 async：flush 两轮让它跑完。
  await flush();
  await flush();

  assert.match(calledPath, /\/media\?reset=1$/, "重新检测要清后端计数（?reset=1）");
  assert.equal(cfg.media_supported, false,
    "未定论的重探必须保留原 false 结论，不能暂时放开媒体键");
  assert.match(registry["#keyDrawerBody"][0].innerHTML, /当前固件不支持媒体键/);
});
