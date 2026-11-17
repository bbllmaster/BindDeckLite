// WS-631 配网页「扫描附近 WiFi + 点行填 SSID」的回归用例。
//
// 运行：cd host && node --test test_portal_scan.mjs
//
// 页面与路由都内联在 src/main.cpp 里（固件无测试框架，PC 上也没有跑
// WebServer 的桩）。这里从生产源文件里**原样抽取** <script id='portal-scan'>
// 那一段，在假的 DOM / fetch 上真跑；扫描结果由真实的 /keys/state 回包喂进来
// （生产里是 <script id='selfcheck'> 调 window.__portalScanSync(s)）。
// 改脚本（排序、去重、截断、点行行为、状态文案）会让断言真的失败 —— 不是把
// 逻辑抄一份到测试里自证。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC = readFileSync(new URL("../src/main.cpp", import.meta.url), "utf8");

/** 取 [start, end) 之间的源码，标记缺失直接失败。 */
function sliceBetween(source, startMarker, endMarker, what) {
  const start = source.indexOf(startMarker);
  assert.ok(start >= 0, `src/main.cpp 里找不到${what}的起点 (${startMarker})`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `src/main.cpp 里找不到${what}的终点 (${endMarker})`);
  return source.slice(start, end);
}

/** 抽出内联 portal-scan 脚本（丢掉 C++ // 注释行），返回可执行的 JS。 */
function extractScanScript(source) {
  const OPEN = "\"<script id='portal-scan'>\"";
  const CLOSE = "\"})();</script>\"";
  const start = source.indexOf(OPEN);
  assert.ok(start >= 0, "src/main.cpp 里找不到 <script id='portal-scan'> 起始标记");
  const end = source.indexOf(CLOSE, start);
  assert.ok(end > start, "src/main.cpp 里找不到 portal-scan 脚本的结束标记");
  const region = source.slice(start, end + CLOSE.length);
  const kept = region
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .join("\n");
  const parts = [];
  const re = /"((?:[^"\\]|\\.)*)"/g;
  let m;
  while ((m = re.exec(kept)) !== null) parts.push(JSON.parse(`"${m[1]}"`));
  const js = parts.join("");
  const tag = "<script id='portal-scan'>";
  assert.ok(js.startsWith(`${tag}(function(){`), "抽出来的不是一段 IIFE");
  return js.slice(tag.length).replace(/<\/script>$/, "");
}

const SCRIPT = extractScanScript(SRC);

// --- 假的浏览器环境 --------------------------------------------------------

/** 最小的元素：能存 textContent / className / 子节点，能挂并触发 click。 */
function makeEl(tag, id) {
  const listeners = {};
  const el = {
    tagName: tag,
    id: id || "",
    className: "",
    hidden: false,
    disabled: false,
    value: "",
    title: "",
    children: [],
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    addEventListener(type, fn) {
      (listeners[type] || (listeners[type] = [])).push(fn);
    },
    fire(type) {
      for (const fn of listeners[type] || []) fn({ type, target: this });
    },
  };
  let text = "";
  Object.defineProperty(el, "textContent", {
    get: () => text,
    set: (v) => {
      text = v == null ? "" : String(v);
      // 真 DOM 里 `el.textContent = ''` 会清空子节点，脚本依赖这一点清列表。
      if (text === "") el.children.length = 0;
    },
  });
  return el;
}

const flush = async () => {
  for (let i = 0; i < 24; i++) await Promise.resolve();
};

/**
 * 起一页：按 cfgPage() 的初始状态铺好 #wifiScanBox / 按钮 / 提示 / 列表
 * （都带 hidden，跟没有 JS 的页面一致），再跑真实脚本。fetch 记录请求，
 * 默认回 {"ok":true}。
 */
function openPortal(reply) {
  const els = new Map();
  const calls = [];
  const ssid = makeEl("input", "ssid");

  for (const [id, tag] of [
    ["wifiScanBox", "div"],
    ["btnWifiScan", "button"],
    ["wifiScanHint", "div"],
    ["wifiScanList", "ul"],
  ]) {
    els.set(id, Object.assign(makeEl(tag, id), { hidden: true }));
  }
  els.get("btnWifiScan").textContent = "扫描附近 WiFi";

  const document = {
    getElementById: (id) => els.get(id) || null,
    querySelector: (sel) => (sel === "input[name=ssid]" ? ssid : null),
    createElement: (tag) => makeEl(tag),
  };

  let answer = reply || { ok: true, body: { ok: true } };
  const fetch = (url, opt) => {
    calls.push({ url, opt });
    const a = answer;
    return Promise.resolve({
      ok: a.ok,
      status: a.ok ? 200 : 409,
      json: () => Promise.resolve(a.body),
    });
  };

  const window = {};
  const run = new Function("document", "window", "fetch", SCRIPT);
  run(document, window, fetch);

  return {
    els,
    calls,
    window,
    ssid,
    el: (id) => els.get(id),
    btn: els.get("btnWifiScan"),
    hint: els.get("wifiScanHint"),
    list: els.get("wifiScanList"),
    box: els.get("wifiScanBox"),
    setReply: (ok, body) => {
      answer = { ok, body };
    },
    // 生产里由 <script id='selfcheck'> 的 400 ms 轮询喂进来，这里直接调。
    sync: (s) => window.__portalScanSync(s),
  };
}

/** 一份 /keys/state 回包：既有字段照生产原样，scan 追加在最后。 */
function state(scan, extra = {}) {
  return Object.assign(
    {
      seq: 3,
      index: -1,
      gpio: -1,
      left: 271,
      wifi: "connecting",
      ssid: "",
      ip: "",
      elapsed: 2,
      wifiText: "未连接",
      scan,
    },
    extra,
  );
}

const rowText = (row) => row.children.map((c) => c.textContent).join(" ");
const ssidOf = (row) => row.children.find((c) => c.className === "scanSsid").textContent;

// --- 源码断言：路由与既有字段不能被改掉 ------------------------------------
test("源码：POST /wifi/scan 存在、GET 405、/keys/state 既有字段仍在", () => {
  const routes = sliceBetween(SRC, 'cfgServer.on("/wifi/scan"', "cfgServer.on(\"/save\"", "/wifi/scan 路由");
  assert.match(routes, /HTTP_POST/, "/wifi/scan 必须是 POST");
  assert.match(routes, /scanNetworks\(true, false\)/, "必须异步扫描且不含隐藏网络");
  assert.match(routes, /HTTP_GET/);
  assert.match(routes, /send\(405, "text\/plain", "Method Not Allowed"\)/, "GET 必须回 405");
  assert.match(routes, /ota_busy/, "OTA 上传中必须回 ota_busy");
  assert.match(routes, /already/, "扫描中重复 POST 必须回 already");

  const keysState = sliceBetween(SRC, 'cfgServer.on("/keys/state"', 'cfgServer.on("/extend"', "/keys/state 处理");
  for (const field of ['\\"seq\\"', '\\"index\\"', '\\"gpio\\"', '\\"left\\"', '\\"wifi\\"', '\\"wifiText\\"']) {
    assert.ok(keysState.includes(field), `/keys/state 丢了既有字段 ${field}`);
  }
  assert.ok(keysState.includes('\\"scan\\"'), "/keys/state 必须追加 scan 对象");
  assert.ok(keysState.indexOf('\\"scan\\"') > keysState.indexOf('\\"wifiText\\"'),
    "scan 必须追加在既有字段之后，不能插队");

  // 页面侧：结果搭现有轮询，不能新开第二条轮询。
  const selfcheck = sliceBetween(SRC, "\"<script id='selfcheck'>\"", "</script>\");", "自检脚本");
  assert.match(selfcheck, /__portalScanSync\(s\)/, "自检轮询必须把回包喂给扫描块");
  assert.equal((SCRIPT.match(/setInterval/g) || []).length, 0, "扫描脚本不能自己开定时器");
  assert.equal((SCRIPT.match(/fetch\(/g) || []).length, 1, "扫描脚本只能有用户点按钮那一次 fetch");
});

// --- 页面行为：跑的是从 src/main.cpp 抽出来的真实脚本 ------------------------
test("scanning → 按钮禁用 + 「扫描中…」，列表收起", async () => {
  const p = openPortal();
  assert.equal(typeof p.window.__portalScanSync, "function",
    "脚本必须挂 __portalScanSync 钩子（由自检轮询喂数据）");

  p.sync(state({ state: "scanning", seq: 0, nets: [] }));
  await flush();

  assert.equal(p.box.hidden, false, "有 scan 字段时扫描块必须显示");
  assert.equal(p.btn.disabled, true, "扫描中按钮必须禁用");
  assert.equal(p.btn.textContent, "扫描中…");
  assert.match(p.hint.textContent, /2–4 秒/);
  assert.equal(p.list.hidden, true, "扫描中不显示上一次的列表");
});

test("done → 列表按信号从强到弱、同名只留最强、最多 8 条", async () => {
  const p = openPortal();
  p.sync(state({
    state: "done",
    seq: 1,
    nets: [
      { s: "Home-2.4G", r: -78, e: 1 },
      { s: "Neighbour", r: -60, e: 1 },
      { s: "Home-2.4G", r: -52, e: 1 },   // 同名更强 -> 保留这条
      { s: "", r: -40, e: 0 },            // 空名（隐藏网络）丢弃
      { s: "OpenCafe", r: -66, e: 0 },
      { s: "Mesh", r: -71, e: 1 },
      { s: "Mesh", r: -81, e: 1 },        // 同名更弱 -> 丢弃
      { s: "N1", r: -45, e: 1 },
      { s: "N2", r: -47, e: 1 },
      { s: "N3", r: -49, e: 1 },
      { s: "N4", r: -51, e: 1 },
      { s: "N5", r: -53, e: 1 },
      { s: "N6", r: -55, e: 1 },
    ],
  }));
  await flush();

  const rows = p.list.children;
  assert.equal(p.list.hidden, false);
  assert.equal(rows.length, 8, "最多 8 条");

  const names = rows.map(ssidOf);
  assert.equal(names[0], "N1", "最强的必须排第一");
  assert.equal(new Set(names).size, names.length, "同名只能出现一行");
  assert.ok(names.includes("Home-2.4G"));
  assert.ok(!names.includes(""), "空 SSID 不能出现在列表里");

  const rssis = rows.map((r) => Number(r.children[2].textContent.replace(/\s*dBm$/, "")));
  for (let i = 1; i < rssis.length; i++) {
    assert.ok(rssis[i - 1] >= rssis[i], `第 ${i} 行不比上一行弱：${rssis.join(",")}`);
  }
  assert.ok(rowText(rows[0]).includes("加密"), "加密网络要有标记");
  assert.ok(rowText(rows[0]).includes("dBm"), "每行要带 dBm");
  assert.match(p.btn.textContent, /扫描附近 WiFi/);
  assert.equal(p.btn.disabled, false, "扫描完成后按钮恢复可点");
});

test("点行 → 写入 #ssid + 触发 dirty refresh，不提交、不动密码框", async () => {
  const p = openPortal();
  let refreshed = 0;
  p.window.portalDirty = { refresh: () => { refreshed++; } };

  p.sync(state({
    state: "done",
    seq: 1,
    nets: [
      { s: "Home-2.4G", r: -52, e: 1 },
      { s: "OpenCafe", r: -66, e: 0 },
    ],
  }));
  await flush();

  const row = p.list.children[1];
  assert.equal(ssidOf(row), "OpenCafe");
  row.fire("click");
  await flush();

  assert.equal(p.ssid.value, "OpenCafe", "输入框必须等于该行名称");
  assert.equal(refreshed, 1, "必须调用 portalDirty.refresh()（顶部出现未保存提示）");
  assert.equal(p.calls.length, 0, "点行不能发任何请求（不提交、不自动保存）");
  assert.equal(row.className, "sel", "选中的行必须高亮");
  assert.equal(p.list.children[0].className, "", "其它行不能同时高亮");
  assert.match(p.hint.textContent, /OpenCafe/);
});

test("done 空列表 → 空态文案（2.4 GHz / 5 GHz-only / 隐藏网络）", async () => {
  const p = openPortal();
  p.sync(state({ state: "done", seq: 4, nets: [] }));
  await flush();

  assert.equal(p.list.hidden, true, "空结果不渲染列表");
  assert.equal(p.list.children.length, 0);
  assert.match(p.hint.textContent, /没有扫描到 2\.4 GHz 网络/);
  assert.match(p.hint.textContent, /5 GHz-only 或隐藏网络请手动输入名称/);
  assert.equal(p.btn.disabled, false, "空结果按钮仍可再扫");
});

test("failed → 失败行 + 按钮恢复；再点仍能发起扫描", async () => {
  const p = openPortal();
  p.sync(state({ state: "failed", seq: 5, nets: [] }));
  await flush();

  assert.match(p.hint.textContent, /扫描失败，请再试一次/);
  assert.equal(p.btn.disabled, false, "失败后按钮必须恢复可点");
  assert.equal(p.btn.textContent, "扫描附近 WiFi");
  assert.equal(p.list.hidden, true);

  p.btn.fire("click");
  await flush();
  assert.equal(p.calls.length, 1);
  assert.equal(p.calls[0].url, "/wifi/scan");
  assert.equal(p.calls[0].opt.method, "POST");
  assert.equal(p.btn.disabled, true, "重新扫描后立刻禁用");
  assert.equal(p.btn.textContent, "扫描中…");
});

test("连点两次只发一次 POST", async () => {
  const p = openPortal();
  p.btn.fire("click");
  p.btn.fire("click");
  await flush();

  assert.equal(p.calls.filter((c) => c.url === "/wifi/scan").length, 1,
    "第二次点击必须不发请求");
});

test("OTA 中 409 ota_busy → 提示「升级中不可扫描」，按钮恢复", async () => {
  const p = openPortal({ ok: false, body: { ok: false, error: "ota_busy" } });
  p.btn.fire("click");
  await flush();

  assert.match(p.hint.textContent, /升级中不可扫描/);
  assert.equal(p.btn.disabled, false);
  assert.equal(p.btn.textContent, "扫描附近 WiFi");
});

test("点了扫描但设备其实已连上 → 409 wifi_connected 的提示是可执行的说法", async () => {
  // 页面以为未连接（上一轮轮询还是 idle），设备侧已经连上：这是按钮没被置灰
  // 的唯一窗口，提示必须说清楚"换网怎么办"，不能让人去找不存在的断开入口。
  const p = openPortal({ ok: false, body: { ok: false, error: "wifi_connected" } });
  p.btn.fire("click");
  await flush();

  assert.match(p.hint.textContent, /已连接 WiFi，暂不可扫描；换网请直接手填名称/);
  assert.doesNotMatch(p.hint.textContent, /断开后/, "配网页没有断开 WiFi 的入口，不能这么写");
  assert.equal(p.btn.disabled, false);
  assert.equal(p.btn.textContent, "扫描附近 WiFi");
});

test("已连接 WiFi → 按钮禁用并说明原因；旧固件缺 scan → 整块隐藏", async () => {
  const p = openPortal();
  p.sync(state({ state: "idle", seq: 0, nets: [] }, { wifi: "ok" }));
  await flush();
  assert.equal(p.btn.disabled, true, "STA 已连接时不能扫描（AP+STA 同一射频）");
  // WS-635: the hint must describe something the page can actually do - the
  // portal has no "disconnect WiFi" control, so "断开后可扫描" was a dead end.
  assert.match(p.hint.textContent, /已连接 WiFi，暂不可扫描；换网请直接手填名称/);
  assert.doesNotMatch(p.hint.textContent, /断开后/);

  p.sync({ seq: 1, index: -1, gpio: -1, left: 200, wifi: "ok", wifiText: "已连接" });
  await flush();
  assert.equal(p.box.hidden, true, "没有 scan 字段时扫描入口必须隐藏");
});

test("扫描后 STA 连上 → 同一 seq 也要把按钮置灰（连接状态参与重绘条件）", async () => {
  const p = openPortal();
  const done = { state: "done", seq: 1, nets: [{ s: "Home", r: -55, e: 1 }] };
  p.sync(state(done));
  await flush();
  assert.equal(p.btn.disabled, false);

  // seq / state 都没变，只有 wifi 变成 ok：按钮必须跟着禁用。
  p.sync(state(done, { wifi: "ok" }));
  await flush();
  assert.equal(p.btn.disabled, true);
  assert.match(p.hint.textContent, /已连接 WiFi，暂不可扫描/);
  assert.match(p.hint.textContent, /换网可点下方列表或直接手填名称/);
  assert.doesNotMatch(p.hint.textContent, /断开后/);
  assert.equal(p.list.children.length, 1, "列表保留，只是不能再扫");
});
