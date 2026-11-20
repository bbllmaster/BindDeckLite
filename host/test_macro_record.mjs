// WS-651 宏 Key 步骤「录入」的前端回归用例。
//
// 运行：cd host && node --test test_macro_record.mjs
//
// 加载的是**生产** static/keys.js（mountMacroEditor + 录入状态机），在最小
// DOM 桩上真实走一遍「点录入 → 合成 keydown → commit」，没有一份逻辑是从
// 生产代码抄过来的副本：改坏 keys.js 里的修饰键顺序、键名表或 Esc 分支，
// 这里就会变红。
//
// 第二个用例守「键名同源」：直接解析 host/app.py 的 MACRO_KEY_NAMES，与
// keys.js 的 MACRO_REC_KEY_NAMES 逐项比对 —— 后端加名字而前端没跟上（或
// 反过来）都会失败。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const STATIC_DIR = new URL("./static/", import.meta.url);
const APP_PY = new URL("./app.py", import.meta.url);

/* ---------------- 最小 DOM 桩 ---------------- */

function makeEl(sel = "") {
  const el = {
    _sel: sel,
    _q: new Map(),
    _listeners: {},
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
    readOnly: false,
    hidden: false,
    disabled: false,
    children: [],
    focusCount: 0,
    focus() { this.focusCount += 1; },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
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
    remove() {},
    // 同一个节点上反复取同一个选择器必须拿到同一个桩：录入态把 row / 按钮 /
    // 输入框的引用存进 state.macroRec，换新对象就断言不到真实 DOM 了。
    querySelector(s) {
      if (!this._q.has(s)) this._q.set(s, makeEl(s));
      return this._q.get(s);
    },
    querySelectorAll() { return []; },
    contains(node) {
      if (node === this) return true;
      for (const child of this._q.values()) if (child.contains(node)) return true;
      return this.children.some((c) => c.contains(node));
    },
    getAttribute(k) { return (this.attributes && this.attributes[k]) ?? null; },
    setAttribute(k, v) { this.attributes = this.attributes || {}; this.attributes[k] = String(v); },
    removeAttribute(k) {
      if (this.attributes) delete this.attributes[k];
    },
    insertAdjacentHTML() {},
    closest() { return null; },
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

/* ---------------- 载入生产 keys.js ---------------- */

function loadKeys(lang = "zh-CN") {
  const docListeners = [];
  const document = {
    body: makeEl("body"),
    documentElement: makeEl("html"),
    querySelector() { return makeEl("query"); },
    querySelectorAll() { return []; },
    getElementById(id) { return makeEl("#" + id); },
    createElement(tag) { return makeEl(tag); },
    createTextNode(t) { return { textContent: t }; },
    addEventListener(type, fn, capture) {
      docListeners.push({ type, fn, capture: !!capture });
    },
    removeEventListener(type, fn, capture) {
      const i = docListeners.findIndex(
        (l) => l.type === type && l.fn === fn && l.capture === !!capture);
      if (i >= 0) docListeners.splice(i, 1);
    },
  };

  const calls = { markDirty: 0 };
  const sandbox = {
    document,
    console,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    clearInterval() {},
    navigator: { userAgent: "Mozilla/5.0 (X11; Linux x86_64)" },
    location: { href: "http://127.0.0.1/" },
    URLSearchParams,
    // app.js 提供的宿主入口：$ / $$ 是 DOM 查询糖，markDirty 是「有未下发
    // 修改」的钩子。用桩替代 app.js 的 init() 副作用，但录入与 commit 本身
    // 跑的是 keys.js 的生产实现。
    state: {
      cfg: { keys: [] },
      editingKey: null,
      macroView: "steps",
      running: {},
      stopping: {},
    },
    $: (sel, root) => (root || document).querySelector(sel),
    $$: (sel, root) => (root || document).querySelectorAll(sel),
    escapeHtml: (s) => String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
    markDirty: () => { calls.markDirty += 1; },
    toast() {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  vm.createContext(sandbox);

  const run = (name) => vm.runInContext(
    readFileSync(new URL(name, STATIC_DIR), "utf8"), sandbox, { filename: name });
  run("i18n.js");
  sandbox.i18n.setMessages(
    JSON.parse(readFileSync(new URL(`../locales/${lang}.json`, STATIC_DIR), "utf8")), lang);
  // 追加导出：keys.js 的顶层 const 不是全局对象属性，只能在同一段脚本里取。
  const keysSrc = readFileSync(new URL("keys.js", STATIC_DIR), "utf8");
  vm.runInContext(keysSrc + `
globalThis.__rec = {
  state, mountMacroEditor, macroRecordKeyName, macroRecordActive,
  retranslateMacroRecord,
  MACRO_REC_KEY_NAMES, MACRO_REC_KEY_SET, MACRO_REC_EVENT_NAMES,
  MACRO_REC_RESERVED,
};
`, sandbox, { filename: "keys.js" });

  /** 按真实的捕获 / 冒泡顺序派发一个 document 级事件。 */
  function fire(type, init) {
    const ev = {
      type,
      key: "",
      ctrlKey: false,
      shiftKey: false,
      altKey: false,
      metaKey: false,
      isComposing: false,
      keyCode: 0,
      defaultPrevented: false,
      propagationStopped: false,
      ...init,
    };
    ev.preventDefault = () => { ev.defaultPrevented = true; };
    ev.stopPropagation = () => { ev.propagationStopped = true; };
    ev.stopImmediatePropagation = () => { ev.propagationStopped = true; };
    const same = docListeners.filter((l) => l.type === type);
    for (const l of same) if (l.capture && !ev.propagationStopped) l.fn(ev);
    for (const l of same) if (!l.capture && !ev.propagationStopped) l.fn(ev);
    return ev;
  }

  const rec = sandbox.__rec;
  // 模拟 keys.js bindKeyDrawer 与 app.js 里「Esc 关抽屉」的冒泡监听：录入态
  // 必须在捕获阶段把它拦下，否则 Esc 会连带关掉抽屉。
  const seen = { drawerEsc: 0, bubbleKeys: 0 };
  document.addEventListener("keydown", (e) => {
    seen.bubbleKeys += 1;
    if (e.key === "Escape") seen.drawerEsc += 1;
  });

  return { sandbox, document, rec, fire, calls, seen, docListeners };
}

/** 挂一个宏编辑器（steps 由调用方给），返回行 / 按钮 / 输入框 / 提示行。 */
function mountMacro(env, macro) {
  const action = { type: "macro", value: "", macro };
  const holder = makeEl("#kdValueBox");
  env.rec.mountMacroEditor(holder, action);
  const wrap = holder.children[0];           // mountMacroEditor 挂上去的 .macro-editor
  assert.ok(wrap, "宏编辑器没有挂到 holder 上");
  const list = wrap.querySelector(".macro-steps");
  const row = list.children[0];
  assert.ok(row, "宏编辑器没有渲染出步骤行");
  return {
    action,
    row,
    btn: row.querySelector(".ms-rec"),
    input: row.querySelector(".ms-value"),
    hint: row.querySelector(".ms-rec-hint"),
  };
}

/** 只有一个 key 步骤的宏编辑器。 */
function mountOne(env, value = "") {
  return mountMacro(env, [{ type: "key", value }]);
}

const press = (env, ...events) => events.forEach((e) => env.fire("keydown", e));

const localeMessages = (lang) => JSON.parse(
  readFileSync(new URL(`../locales/${lang}.json`, STATIC_DIR), "utf8"));

/* ---------------- 用例 ---------------- */

test("Ctrl+Alt+T → ctrl+alt+t，且只 commit 一次", () => {
  const env = loadKeys();
  const m = mountOne(env);
  m.btn.onclick();

  press(env,
    { key: "Control", ctrlKey: true },
    { key: "Alt", ctrlKey: true, altKey: true },
  );
  // 只按修饰键：提示行先回显，值不动。
  assert.equal(m.action.value, "", "纯修饰键不能落值");
  assert.equal(m.input.value, "");
  assert.match(m.hint.textContent, /Ctrl \+ Alt \+ …/);

  press(env, { key: "t", ctrlKey: true, altKey: true });

  assert.equal(m.action.macro[0].value, "ctrl+alt+t");
  assert.equal(m.action.value, "key:ctrl+alt+t");
  assert.equal(m.input.value, "ctrl+alt+t");
  assert.equal(env.calls.markDirty, 1, "commit() 应该正好调用一次");
  assert.equal(env.rec.state.macroRec, null, "写完必须退出录入态");
  assert.equal(m.btn.textContent, env.sandbox.t("keys.macro_rec"));
  assert.equal(m.row.classList.contains("recording"), false);

  // 监听已摘掉：再按键不再改值、不再 commit。
  press(env, { key: "u", ctrlKey: true });
  assert.equal(env.calls.markDirty, 1);
  assert.equal(m.action.value, "key:ctrl+alt+t");
});

test("只按 Shift：值不变、仍在录入态、提示回显 Shift", () => {
  const env = loadKeys();
  const m = mountOne(env);
  m.btn.onclick();

  press(env, { key: "Shift", shiftKey: true });

  assert.equal(m.action.value, "");
  assert.equal(m.input.value, "");
  assert.equal(m.action.macro[0].value, "");
  assert.ok(env.rec.state.macroRec, "纯修饰键不能退出录入态");
  assert.equal(env.calls.markDirty, 0);
  assert.match(m.hint.textContent, /Shift \+ …/);
  assert.equal(m.hint.hidden, false);
});

test("Esc：只取消录入，值不变、事件不冒泡（抽屉不关）", () => {
  const env = loadKeys();
  const m = mountOne(env);
  m.btn.onclick();

  const ev = env.fire("keydown", { key: "Escape" });

  assert.equal(ev.defaultPrevented, true, "录入态必须 preventDefault");
  assert.equal(ev.propagationStopped, true, "录入态必须 stopPropagation");
  assert.equal(env.seen.drawerEsc, 0, "Esc 不能冒泡到关抽屉的监听");
  assert.equal(env.seen.bubbleKeys, 0, "捕获阶段就该截住");
  assert.equal(m.action.value, "");
  assert.equal(m.input.value, "");
  assert.equal(env.calls.markDirty, 0);
  assert.equal(env.rec.state.macroRec, null);
  assert.equal(m.input.readOnly, false, "取消后输入框要恢复可编辑");
  assert.ok(m.btn.focusCount >= 1, "Esc 之后焦点回到「录入」按钮");
});

test("F5 / ArrowUp / Space / PageDown → f5 / up / space / pagedown", () => {
  const cases = [
    [{ key: "F5" }, "f5"],
    [{ key: "ArrowUp" }, "up"],
    [{ key: " " }, "space"],
    [{ key: "PageDown" }, "pagedown"],
  ];
  for (const [init, want] of cases) {
    const env = loadKeys();
    const m = mountOne(env);
    m.btn.onclick();
    press(env, init);
    assert.equal(m.action.macro[0].value, want, `${init.key} 应录入 ${want}`);
    assert.equal(m.action.value, `key:${want}`);
    assert.equal(env.calls.markDirty, 1);
  }
});

test("Ctrl+W 与 + 被拒绝：不写值、给文案、仍在录入态", () => {
  const env = loadKeys();
  const m = mountOne(env);
  m.btn.onclick();

  press(env, { key: "w", ctrlKey: true });
  assert.equal(m.action.value, "", "Ctrl+W 是浏览器保留组合，不能写值");
  assert.equal(m.input.value, "");
  assert.equal(m.hint.textContent, env.sandbox.t("keys.macro_rec_reserved"));
  assert.ok(env.rec.state.macroRec, "拒绝后留在录入态，可重按或 Esc");

  press(env, { key: "+", shiftKey: true });
  assert.equal(m.action.value, "");
  assert.equal(m.hint.textContent, env.sandbox.t("keys.macro_rec_plus"));
  assert.ok(env.rec.state.macroRec);
  assert.equal(env.calls.markDirty, 0);
});

test("Ctrl+Shift+W / Ctrl+Shift+T 同族组合一样拒绝：不写值、有文案、Esc 可退", () => {
  const env = loadKeys();
  const m = mountOne(env);
  m.btn.onclick();
  const reserved = env.sandbox.t("keys.macro_rec_reserved");

  // 浏览器对这两个组合 preventDefault 无效（Ctrl+Shift+W 直接关窗），
  // 所以和 Ctrl+W / Ctrl+T 一样按「被占用」处理，不能写进配置。
  for (const key of ["w", "t"]) {
    const ev = env.fire("keydown", { key, ctrlKey: true, shiftKey: true });
    assert.equal(ev.defaultPrevented, true, `Ctrl+Shift+${key.toUpperCase()} 仍要吞掉事件`);
    assert.equal(m.action.value, "", `Ctrl+Shift+${key.toUpperCase()} 不能写值`);
    assert.equal(m.action.macro[0].value, "");
    assert.equal(m.input.value, "");
    assert.equal(m.hint.textContent, reserved);
    assert.ok(env.rec.state.macroRec, "拒绝后留在录入态");
  }
  // Ctrl+Shift+N 与 Alt+Shift+F4 也没被漏掉。
  press(env, { key: "n", ctrlKey: true, shiftKey: true });
  assert.equal(m.action.value, "");
  assert.equal(m.hint.textContent, reserved);
  press(env, { key: "F4", altKey: true, shiftKey: true });
  assert.equal(m.action.value, "");
  assert.equal(m.hint.textContent, reserved);
  assert.equal(env.calls.markDirty, 0);

  const ev = env.fire("keydown", { key: "Escape" });
  assert.equal(ev.propagationStopped, true);
  assert.equal(env.rec.state.macroRec, null);
  assert.equal(m.action.value, "");
  // 不同族：Shift+W 不含 ctrl，是普通组合，照常录入（拒绝规则不能放宽成
  // 「主键命中就拒绝」）。
  m.btn.onclick();
  press(env, { key: "w", shiftKey: true });
  assert.equal(m.action.macro[0].value, "shift+w");
});

test("key 步骤真的渲染出 .ms-rec 按钮，非 key 步骤没有", () => {
  const env = loadKeys();
  const m = mountOne(env);
  const aria = env.sandbox.t("keys.macro_rec_aria");
  const label = env.sandbox.t("keys.macro_rec");
  // 断言读的是 mountMacroEditor 写进 row 的**真实 HTML**：桩里的
  // row.querySelector 会凭空造节点，只查 querySelector 的话删掉按钮也不会红。
  assert.match(m.row._html, /<button[^>]*class="btn sm ms-rec"/,
    "key 步骤缺少「录入」按钮的 markup");
  assert.ok(m.row._html.includes(`title="${aria}"`), "按钮 title 应取 keys.macro_rec_aria");
  assert.ok(m.row._html.includes(`aria-label="${aria}"`), "按钮 aria-label 应取 keys.macro_rec_aria");
  assert.ok(m.row._html.includes('data-i18n-title="keys.macro_rec_aria"'));
  assert.ok(m.row._html.includes('data-i18n-aria-label="keys.macro_rec_aria"'));
  assert.ok(m.row._html.includes(`>${label}</button>`), "按钮可见文字仍是「录入」");

  const text = mountMacro(env, [{ type: "text", value: "hello" }]);
  assert.doesNotMatch(text.row._html, /class="btn sm ms-rec"/, "非 key 步骤不该出现录入按钮");
});

test("录入态可访问名切到「按组合键…」，Esc / 提交 / 点外面都恢复", () => {
  const active = "keys.macro_rec_active";
  const aria = "keys.macro_rec_aria";
  const exits = [
    ["Esc", (env) => press(env, { key: "Escape" })],
    ["提交", (env) => press(env, { key: "t", ctrlKey: true, altKey: true })],
    ["点外面", (env) => env.fire("pointerdown", { target: makeEl("body") })],
  ];
  for (const [name, exit] of exits) {
    const env = loadKeys();
    const m = mountOne(env);
    const t = env.sandbox.t;
    m.btn.onclick();
    assert.equal(m.btn.textContent, t(active), `${name}：录入态文字`);
    assert.equal(m.btn.title, t(active), `${name}：录入态 title 要跟着换，读屏才知道在等键`);
    assert.equal(m.btn.getAttribute("aria-label"), t(active), `${name}：录入态 aria-label`);
    assert.equal(m.btn.getAttribute("data-i18n-title"), active, `${name}：data-i18n-title`);
    assert.equal(m.btn.getAttribute("data-i18n-aria-label"), active, `${name}：data-i18n-aria-label`);

    exit(env);
    assert.equal(env.rec.state.macroRec, null, `${name} 应退出录入态`);
    assert.equal(m.btn.textContent, t("keys.macro_rec"), `${name}：退出后文字`);
    assert.equal(m.btn.title, t(aria), `${name}：退出后 title`);
    assert.equal(m.btn.getAttribute("aria-label"), t(aria), `${name}：退出后 aria-label`);
    assert.equal(m.btn.getAttribute("data-i18n-title"), aria, `${name}：退出后 data-i18n-title`);
    assert.equal(m.btn.getAttribute("data-i18n-aria-label"), aria, `${name}：退出后 data-i18n-aria-label`);
  }
});

test("录入中切语言：可访问名跟当前语言，退出后也跟当前语言", () => {
  const env = loadKeys();
  const m = mountOne(env);
  m.btn.onclick();
  env.sandbox.i18n.setMessages(localeMessages("en-US"), "en-US");
  env.rec.retranslateMacroRecord();
  assert.equal(m.btn.textContent, "Press keys…");
  assert.equal(m.btn.title, "Press keys…");
  assert.equal(m.btn.getAttribute("aria-label"), "Press keys…");
  assert.equal(m.btn.getAttribute("data-i18n-title"), "keys.macro_rec_active");

  press(env, { key: "Escape" });
  assert.equal(m.btn.title, "Record keys");
  assert.equal(m.btn.getAttribute("aria-label"), "Record keys");
  assert.doesNotMatch(m.btn.title, /[\u4e00-\u9fff]/, "英文界面不得残留中文");
});

test("已手写的键名不因取消被改写（PageUp 保持原样）", () => {
  const env = loadKeys();
  const m = mountOne(env, "PageUp");
  m.btn.onclick();
  assert.equal(m.input.readOnly, true, "录入期间输入框只读");
  press(env, { key: "Shift", shiftKey: true });
  press(env, { key: "Escape" });
  assert.equal(m.action.macro[0].value, "PageUp");
  assert.equal(m.input.value, "PageUp");
  assert.equal(env.calls.markDirty, 0);
});

test("输入法组合中的按键（isComposing）一律忽略", () => {
  const env = loadKeys();
  const m = mountOne(env);
  m.btn.onclick();

  const ev = env.fire("keydown", { key: "t", ctrlKey: true, isComposing: true });
  assert.equal(ev.defaultPrevented, false, "组合中的按键不该被吞");
  assert.equal(m.action.value, "");
  assert.ok(env.rec.state.macroRec, "仍在录入态");
  assert.equal(env.calls.markDirty, 0);
});

test("点录入行以外的地方先退出录入态，值不写", () => {
  const env = loadKeys();
  const m = mountOne(env);
  m.btn.onclick();
  env.fire("pointerdown", { target: makeEl("body") });
  assert.equal(env.rec.state.macroRec, null);
  assert.equal(m.action.value, "");
  assert.equal(env.calls.markDirty, 0);

  // 点本行按钮（例如再次点「录入」）不算「点外面」。
  m.btn.onclick();
  assert.ok(env.rec.state.macroRec);
  env.fire("pointerdown", { target: m.btn });
  assert.ok(env.rec.state.macroRec, "点本行不应退出");
});

test("键名同源：前端集合 == 后端 MACRO_KEY_NAMES 键集合", () => {
  const env = loadKeys();
  const src = readFileSync(APP_PY, "utf8");

  const start = src.indexOf("MACRO_KEY_NAMES = {");
  assert.ok(start >= 0, "app.py 里找不到 MACRO_KEY_NAMES");
  const block = src.slice(start, src.indexOf("\n}", start));
  // 键名可以带 `_` / `-`（num_plus、ctrl-shift 这类别名），只认 [a-z0-9]
  // 会漏掉它们：后端加了名字、前端没跟，用例照样绿。
  const backend = new Set([...block.matchAll(/"([^"]+)"\s*:/g)].map((m) => m[1]));

  // F1–F12 由紧随其后的 update(...) 生成，按 range 边界展开。
  const upd = /MACRO_KEY_NAMES\.update\(\{f"f\{i\}": f"f\{i\}" for i in range\((\d+),\s*(\d+)\)\}\)/.exec(src);
  assert.ok(upd, "app.py 的 F 键 update 结构变了，请同步本用例");
  for (let i = Number(upd[1]); i < Number(upd[2]); i++) backend.add("f" + i);

  assert.ok(backend.size >= 30, `后端键名集合解析异常：${backend.size} 项`);
  const front = new Set(env.rec.MACRO_REC_KEY_NAMES);
  assert.deepEqual(
    [...front].sort(), [...backend].sort(),
    "前端 MACRO_REC_KEY_NAMES 与后端 MACRO_KEY_NAMES 漂移了");
  assert.equal(front.size, env.rec.MACRO_REC_KEY_NAMES.length, "前端集合有重复项");
});

test("en-US：按钮 / 提示 / 拒绝文案全为英文", () => {
  const env = loadKeys("en-US");
  const m = mountOne(env);
  m.btn.onclick();
  assert.equal(m.btn.textContent, "Press keys…");
  assert.match(m.hint.textContent, /^Press the shortcut/);

  press(env, { key: "Shift", shiftKey: true });
  assert.equal(m.hint.textContent, "Shift + …");

  press(env, { key: "w", ctrlKey: true });
  assert.match(m.hint.textContent, /browser or the system/);
  assert.doesNotMatch(m.hint.textContent, /[\u4e00-\u9fff]/, "英文界面不得残留中文");
});

test("录入生成的名字都在后端可用的键名集合内", () => {
  const env = loadKeys();
  // 单字母 / 单数字后端原样透传（不在 MACRO_KEY_NAMES 里），其余必须是
  // MACRO_KEY_NAMES 认识的规范名。
  const known = (name) => /^[a-z0-9]$/.test(name) || env.rec.MACRO_REC_KEY_SET.has(name);
  for (const name of Object.values(env.rec.MACRO_REC_EVENT_NAMES)) {
    if (name === "+") continue;               // 分隔符不是主键
    assert.ok(
      known(name),
      `前端会把 ${name} 写进配置，但后端 MACRO_KEY_NAMES 不认识它`);
  }
  for (const combo of env.rec.MACRO_REC_RESERVED) {
    const main = combo.split("+").pop();
    assert.ok(known(main), `拒绝集里的 ${main} 不在键名集合内`);
  }
});
