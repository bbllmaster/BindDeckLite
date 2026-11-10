// WS-537 日志页「设备标签 / 设备筛选 / 复制带设备」的前端回归用例。
//
// 运行：node --test host/test_log_device.mjs
//
// 断言对象是 host/static/log_device.js 里的**生产函数**（app.js 在浏览器里
// 用的同一份），不在这里复制判定逻辑，所以改生产代码会让断言真的失败。
// 后半段在最小 DOM 上加载**生产 app.js**，真跑 syncLogDeviceUi，钉住
// 「≥2 才出标签 / 掉回 1 清干净 / 行节点不重建」这几条 DOM 行为。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import LogDevice from "./static/log_device.js";

const {
  deviceUnion,
  showsDeviceTag,
  rowDeviceLabel,
  deviceOptions,
  rowMatchesDev,
  logCopyText,
} = LogDevice;

const A = "AA:BB:CC:DD:EE:01";
const B = "AA:BB:CC:DD:EE:02";
const GONE = "AA:BB:CC:DD:EE:09";
const NAME_A = "BindDeckLite-A1B2";
const NAME_B = "BindDeckLite-B2C3";

const DEVICES = [
  { dev_id: A, name: NAME_A, connected: true },
  { dev_id: B, name: NAME_B, connected: true },
];

// 1. 单设备：与今天完全一致 —— 不加标签、不出下拉。
test("设备并集为 1 时不显示标签与下拉", () => {
  const onlyDevice = [A];
  const union = deviceUnion(onlyDevice, []);
  assert.deepEqual(union, [A]);
  assert.equal(showsDeviceTag(union), false);
  assert.equal(rowDeviceLabel(A, DEVICES, union), "");

  // 设备只出现在历史日志里、扫描列表已空：并集仍为 1，同样不显示。
  const historical = deviceUnion([], [A, A]);
  assert.deepEqual(historical, [A]);
  assert.equal(showsDeviceTag(historical), false);

  // 没有设备、没有日志：也不显示（旧后端 / 空日志页）。
  assert.equal(showsDeviceTag(deviceUnion([], [])), false);
});

// 2. 双设备：标签取 name；设备已不在列表时退回原始 dev。
test("设备并集为 2 时行标签取 name，未知 dev 退回原始 dev", () => {
  const union = deviceUnion([A, B], [A, B]);
  assert.deepEqual(union, [A, B]);
  assert.equal(showsDeviceTag(union), true);

  assert.equal(rowDeviceLabel(A, DEVICES, union), NAME_A);
  assert.equal(rowDeviceLabel(B, DEVICES, union), NAME_B);
  // 已离线的历史行：名字从日志里来，设备列表查不到 → 原始 dev。
  assert.equal(rowDeviceLabel(GONE, DEVICES, union), GONE);

  // 设备名缺失时同样退回 dev，而不是显示空标签。
  assert.equal(rowDeviceLabel(A, [{ dev_id: A }], union), A);

  // 并集去重 + 顺序稳定：在线设备在前，仅在日志里的 dev 排序追加。
  assert.deepEqual(deviceUnion([B, A], [A, GONE, B]), [B, A, GONE]);
});

// 3. 设备维度过滤：选了某台，其它设备的行不匹配。
test("logRowMatches 的设备维度：选中一台时其它设备的行不匹配", () => {
  assert.equal(rowMatchesDev(A, A), true);
  assert.equal(rowMatchesDev(B, A), false);
  assert.equal(rowMatchesDev(A, B), false);
  // 未选设备（全部设备）时全通过。
  assert.equal(rowMatchesDev(A, ""), true);
  assert.equal(rowMatchesDev("", ""), true);
});

// 4. dev 为空的行：永不加标签、永不被设备过滤命中。
test("dev 为空的行不加标签、不受设备过滤命中", () => {
  const union = deviceUnion([A, B], [A, B]);
  assert.equal(rowDeviceLabel("", DEVICES, union), "");
  assert.equal(rowDeviceLabel(null, DEVICES, union), "");
  assert.equal(rowDeviceLabel(undefined, DEVICES, union), "");

  assert.equal(rowMatchesDev("", A), false);
  assert.equal(rowMatchesDev("", B), false);

  // 也不出现在设备下拉里（只有「全部设备」+ 两台设备）。
  const values = deviceOptions([A, B], [A, B, ""], DEVICES).map((o) => o.value);
  assert.deepEqual(values, ["", A, B]);
});

// 5. 复制文本格式：有 dev 插一段 [设备名]，无 dev 保持原样。
test("复制文本在有 / 无 dev 时的格式", () => {
  assert.equal(
    logCopyText({ time: "12:00:01", kind: "key", dev: A, text: "按键 3" }, DEVICES),
    "12:00:01 [key] [BindDeckLite-A1B2] 按键 3"
  );
  assert.equal(
    logCopyText({ time: "12:00:02", kind: "cfg", dev: "", text: "已下发" }, DEVICES),
    "12:00:02 [cfg] 已下发"
  );
  // 设备已离线：用原始 dev，不留空方括号。
  assert.equal(
    logCopyText({ time: "12:00:03", kind: "key", dev: GONE, text: "按键 1" }, DEVICES),
    "12:00:03 [key] [AA:BB:CC:DD:EE:09] 按键 1"
  );
});

// 下拉选项：在线设备不标、离线设备标「（已离线）」，历史行仍可筛。
test("设备下拉：全部设备 + 每台设备，离线项带「已离线」", () => {
  const opts = deviceOptions([A], [B, A], DEVICES);
  assert.deepEqual(opts, [
    { value: "", label: "全部设备", offline: false },
    { value: A, label: NAME_A, offline: false },
    { value: B, label: NAME_B + "（已离线）", offline: true },
  ]);
  // 离线设备的行仍能被精确命中。
  assert.equal(rowMatchesDev(B, opts[2].value), true);
});

/* ---------------- DOM 层：真跑生产 app.js 的 syncLogDeviceUi ---------------- */

const STATIC_DIR = new URL("./static/", import.meta.url);

/** 最小 DOM：只实现 syncLogDeviceUi / logRowMatches 用到的那几个方法。 */
function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    id: "",
    children: [],
    parentNode: null,
    dataset: {},
    style: {},
    title: "",
    value: "",
    hidden: false,
    disabled: false,
    type: "",
    checked: false,
    _className: "",
    _text: "",
    get className() { return this._className; },
    set className(v) { this._className = String(v == null ? "" : v); },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v == null ? "" : v); },
    get options() { return this.children.filter((c) => c.tagName === "OPTION"); },
    get nextSibling() {
      if (!this.parentNode) return null;
      const i = this.parentNode.children.indexOf(this);
      return i < 0 ? null : this.parentNode.children[i + 1] || null;
    },
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    getAttribute() { return null; },
    focus() {},
    scrollTo() {},
  };
  el.classList = {
    _list() { return el._className.split(/\s+/).filter(Boolean); },
    _write(l) { el._className = l.join(" "); },
    add(...c) { const l = this._list(); c.forEach((x) => { if (!l.includes(x)) l.push(x); }); this._write(l); },
    remove(...c) { this._write(this._list().filter((x) => !c.includes(x))); },
    contains(c) { return this._list().includes(c); },
    toggle(c, on) {
      const want = on === undefined ? !this.contains(c) : !!on;
      if (want) this.add(c); else this.remove(c);
    },
  };
  el.insertBefore = (child, ref) => {
    if (child.parentNode) child.parentNode.removeChild(child);
    const i = ref ? el.children.indexOf(ref) : -1;
    if (i < 0) el.children.push(child); else el.children.splice(i, 0, child);
    child.parentNode = el;
    return child;
  };
  el.appendChild = (child) => el.insertBefore(child, null);
  el.removeChild = (child) => {
    const i = el.children.indexOf(child);
    if (i >= 0) el.children.splice(i, 1);
    child.parentNode = null;
    return child;
  };
  el.remove = () => { if (el.parentNode) el.parentNode.removeChild(el); };
  const hit = (node, sel) => {
    if (sel.startsWith(".")) return node.classList.contains(sel.slice(1));
    if (sel.startsWith("#")) return node.id === sel.slice(1);
    return node.tagName === sel.toUpperCase();
  };
  const walk = (node, fn) => node.children.forEach((c) => { fn(c); walk(c, fn); });
  el.querySelector = (sel) => {
    let found = null;
    walk(el, (c) => { if (!found && hit(c, sel)) found = c; });
    return found;
  };
  el.querySelectorAll = (sel) => {
    const out = [];
    walk(el, (c) => { if (hit(c, sel)) out.push(c); });
    return out;
  };
  return el;
}

/** 加载生产 app.js，返回宿主函数；registry 里的元素全程同一个实例。 */
function loadApp(registry) {
  const document = {
    body: makeEl("body"),
    documentElement: makeEl("html"),
    querySelector(sel) { return registry.get(sel) || makeEl(sel); },
    querySelectorAll() { return []; },
    getElementById(id) { return registry.get("#" + id) || makeEl("#" + id); },
    createElement: (tag) => makeEl(tag),
    createTextNode: (text) => ({ textContent: text }),
    addEventListener() {},
    removeEventListener() {},
  };
  const sandbox = {
    document,
    console,
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    clearInterval() {},
    fetch: async () => { throw new Error("no network in the harness"); },
    navigator: { userAgent: "node" },
    location: { href: "http://127.0.0.1/" },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
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
  // 生产 i18n + 生产 zh-CN 文案表：断言的仍是生产函数的输出（默认中文）。
  run("i18n.js");
  sandbox.i18n.setMessages(
    JSON.parse(readFileSync(new URL("../locales/zh-CN.json", STATIC_DIR), "utf8")), "zh-CN");
  run("log_follow.js");
  run("log_device.js");
  run("reconnect_sync.js");
  run("keys.js");

  const appSrc = readFileSync(new URL("app.js", STATIC_DIR), "utf8");
  vm.runInContext(appSrc + `
globalThis.__host = { state, syncLogDeviceUi, logRowMatches, logFilterActive };
`, sandbox, { filename: "app.js" });
  return sandbox.__host;
}

/** 一行日志的 DOM（与 pollLog 建出来的结构一致，省略 innerHTML 解析）。 */
function newRow(dev, kind = "key", text = "按键 3") {
  const row = makeEl("div");
  row.className = "log-row info";
  row.dataset.dev = dev;
  row.dataset.kind = kind;
  row.dataset.level = "info";
  const time = makeEl("span");
  time.className = "log-time";
  time.textContent = "12:00:00";
  const kindEl = makeEl("span");
  kindEl.className = "log-kind";
  kindEl.textContent = kind;
  const textEl = makeEl("span");
  textEl.className = "log-text";
  textEl.textContent = text;
  const copy = makeEl("button");
  copy.className = "log-copy";
  row.appendChild(time);
  row.appendChild(kindEl);
  row.appendChild(textEl);
  row.appendChild(copy);
  return row;
}

function setup(devices, devs) {
  const list = makeEl("div");
  list.id = "logList";
  const select = makeEl("select");
  select.id = "logDev";
  const label = makeEl("label");
  label.id = "logDevLabel";
  const registry = new Map([["#logList", list], ["#logDev", select], ["#logDevLabel", label]]);
  const host = loadApp(registry);
  host.state.devices = devices;
  devs.forEach((dev) => list.appendChild(newRow(dev)));
  return { host, list, select, label };
}

test("DOM：单设备时不插标签、不出下拉（与今天一致）", () => {
  const { host, list, select, label } = setup([{ dev_id: A, name: NAME_A }], [A]);
  host.syncLogDeviceUi();
  assert.equal(list.classList.contains("show-dev"), false);
  assert.equal(select.hidden, true);
  assert.equal(label.hidden, true);
  assert.equal(list.children[0].querySelector(".log-dev"), null);
  assert.equal(select.options.length, 0);
});

test("DOM：双设备时每行标签取 name，下拉含全部设备", () => {
  const { host, list, select, label } = setup(DEVICES, [A, B]);
  host.syncLogDeviceUi();
  assert.equal(list.classList.contains("show-dev"), true);
  assert.equal(select.hidden, false);
  assert.equal(label.hidden, false);
  assert.equal(list.children[0].querySelector(".log-dev").textContent, NAME_A);
  assert.equal(list.children[1].querySelector(".log-dev").textContent, NAME_B);
  // 长名被省略时，title 保留全文。
  assert.equal(list.children[0].querySelector(".log-dev").title, NAME_A);
  assert.deepEqual(select.options.map((o) => o.value), ["", A, B]);
  assert.deepEqual(select.options.map((o) => o.textContent), ["全部设备", NAME_A, NAME_B]);
  // 标签插在类型列之后、正文之前：时间 / 类型 / 设备 / 正文 / 复制。
  assert.deepEqual(
    list.children[0].children.map((c) => c.className),
    ["log-time", "log-kind", "log-dev", "log-text", "log-copy"]
  );
});

test("DOM：从 2 台掉回 1 台，标签 / 下拉 / 选项 / 过滤一起清掉", () => {
  const { host, list, select, label } = setup([{ dev_id: A, name: NAME_A }], [A, B]);
  host.syncLogDeviceUi();
  assert.equal(list.classList.contains("show-dev"), true); // B 的离线历史行还在 → 并集 2
  host.state.logFilter.dev = B;
  host.syncLogDeviceUi();
  assert.equal(host.logFilterActive(), true);

  // B 的历史行被裁掉、设备也没上电 → 并集掉回 1。
  list.removeChild(list.children[1]);
  host.syncLogDeviceUi();
  assert.equal(list.classList.contains("show-dev"), false);
  assert.equal(select.hidden, true);
  assert.equal(label.hidden, true);
  assert.equal(select.options.length, 0);
  assert.equal(select.value, "");
  assert.equal(host.state.logFilter.dev, "");
  assert.equal(host.logFilterActive(), false);
  assert.equal(list.children[0].querySelector(".log-dev"), null);
});

test("DOM：并集 1 → 2 时就地补标签，不重建行节点", () => {
  const { host, list } = setup([{ dev_id: A, name: NAME_A }], [A]);
  const rowA = list.children[0];
  host.syncLogDeviceUi();
  assert.equal(rowA.querySelector(".log-dev"), null);

  host.state.devices = DEVICES;
  list.appendChild(newRow(B, "key", "按键 4"));
  host.syncLogDeviceUi();
  assert.equal(list.children[0], rowA, "已有行节点被重建了");
  assert.equal(rowA.querySelector(".log-dev").textContent, NAME_A);
});

test("DOM：设备过滤只影响可见性，空 dev 行不被命中", () => {
  const { host, list } = setup(DEVICES, [A, B, ""]);
  host.syncLogDeviceUi();
  const [rowA, rowB, rowNone] = list.children;

  host.state.logFilter.dev = A;
  assert.equal(host.logFilterActive(), true);
  assert.equal(host.logRowMatches(rowA), true);
  assert.equal(host.logRowMatches(rowB), false);
  assert.equal(host.logRowMatches(rowNone), false);
  assert.equal(rowA.querySelector(".log-dev").textContent, NAME_A);
  assert.equal(rowNone.querySelector(".log-dev").textContent, "");

  host.state.logFilter.dev = "";
  assert.equal(host.logFilterActive(), false);
  assert.equal(host.logRowMatches(rowB), true);
});
