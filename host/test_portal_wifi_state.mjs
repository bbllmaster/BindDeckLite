// WS-610 配网页「WiFi 状态自动更新 + 一键重试」的回归用例。
//
// 运行：cd host && node --test test_portal_wifi_state.mjs
//
// 配网页与路由都内联在 src/main.cpp 里（固件无测试框架，PC 上也没有跑
// WebServer 的桩）。这里从生产源文件里**原样抽取** <script id='wifi-state'>
// 那一段，在假的 DOM / fetch / 定时器 / performance.now 上真跑，再对页面结构与
// 路由做源码断言。状态行复用自检那条唯一的 /keys/state 轮询：自检脚本把整份
// 响应喂给 window.__portalWifiSync(s)，本段脚本只做 1 s 节流，不新开请求。
// 改 cfgPage() 里的脚本或路由，会让这里的断言真的失败 —— 不是在测试里复制一份
// 状态机逻辑自证。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC_PATH = new URL("../src/main.cpp", import.meta.url);
const SRC = readFileSync(SRC_PATH, "utf8");

/** 取 [startMarker, endMarker) 之间的源码，标记缺失直接失败。 */
function sliceBetween(source, startMarker, endMarker, what) {
  const start = source.indexOf(startMarker);
  assert.ok(start >= 0, `src/main.cpp 里找不到${what}的起点 (${startMarker})`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `src/main.cpp 里找不到${what}的终点 (${endMarker})`);
  return source.slice(start, end);
}

/** 拼好 C++ 的多段字符串字面量，返回解出来的字符串数组。 */
function literalsIn(region) {
  const parts = [];
  const re = /"((?:[^"\\]|\\.)*)"/g;
  let m;
  while ((m = re.exec(region)) !== null) parts.push(JSON.parse(`"${m[1]}"`));
  return parts;
}

/** 抽出内联 wifi-state 脚本（丢掉 C++ // 注释行），返回可执行的 JS。 */
function extractWifiScript(source) {
  const OPEN = "\"<script id='wifi-state'>\"";
  const CLOSE = "\"})();</script>\"";
  const start = source.indexOf(OPEN);
  assert.ok(start >= 0, "src/main.cpp 里找不到 <script id='wifi-state'> 起始标记");
  const end = source.indexOf(CLOSE, start);
  assert.ok(end > start, "src/main.cpp 里找不到 wifi-state 脚本的结束标记");
  const region = source.slice(start, end + CLOSE.length);
  const kept = region.split("\n").filter((line) => !line.trim().startsWith("//")).join("\n");
  const parts = literalsIn(kept);
  assert.ok(parts.length > 0, "wifi-state 脚本区域里没有任何字符串字面量");
  const js = parts.join("");
  const tag = "<script id='wifi-state'>";
  assert.ok(js.startsWith(`${tag}(function(){`), "抽出来的不是一段 IIFE");
  return js.slice(tag.length).replace(/<\/script>$/, "");
}

const SCRIPT = extractWifiScript(SRC);
const unescape = (s) => s.replace(/\\"/g, '"');

// --- 假的浏览器环境 --------------------------------------------------------
function makeElement(id) {
  const classes = new Set();
  const listeners = {};
  return {
    id,
    textContent: "",
    hidden: false,
    disabled: false,
    value: "",
    style: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      contains: (c) => classes.has(c),
    },
    addEventListener(type, fn) {
      (listeners[type] || (listeners[type] = [])).push(fn);
    },
    fire(type) {
      (listeners[type] || []).forEach((fn) => fn({ type }));
    },
  };
}

/** 虚拟时钟：setTimeout / setInterval 共一条时间轴，now() 供节流判断。 */
function makeClock() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  function set(fn, ms, every) {
    const id = nextId++;
    timers.set(id, { at: now + (ms || 0), fn, every });
    return id;
  }
  return {
    setTimeout: (fn, ms) => set(fn, ms, 0),
    setInterval: (fn, ms) => set(fn, ms, ms || 1),
    clearTimeout: (id) => timers.delete(id),
    clearInterval: (id) => timers.delete(id),
    now: () => now,
    advance(ms) {
      const target = now + ms;
      for (;;) {
        let due = null;
        for (const [id, t] of timers) {
          if (t.at <= target && (!due || t.at < due.t.at)) due = { id, t };
        }
        if (!due) break;
        const fireAt = due.t.at;
        if (due.t.every) due.t.at = fireAt + due.t.every;
        else timers.delete(due.id);
        now = fireAt;
        due.t.fn();
      }
      now = target;
    },
  };
}

const flush = async () => {
  for (let i = 0; i < 24; i++) await Promise.resolve();
};
const step = async (clock, ms) => {
  clock.advance(ms);
  await flush();
};

/**
 * 起一页：照页面初始状态铺好 #wifiState（服务端渲染文本 + connecting 类）与
 * #wifiRetryBtn（hidden）。window.__portalWifiSync 由真实脚本挂上，测试通过它
 * 喂入 /keys/state 的解析响应；fetch 记录所有请求，点击重试返回 {ok:true}。
 */
function openPortal() {
  const els = new Map();
  const calls = [];
  const clock = makeClock();
  const window = {};

  const elState = Object.assign(makeElement("wifiState"), {
    textContent: "未连接，正在连接…，已保存 Home",
  });
  elState.classList.add("wifi-connecting");
  els.set("wifiState", elState);
  els.set("wifiRetryBtn", Object.assign(makeElement("wifiRetryBtn"), {
    textContent: "重试连接",
    hidden: true,
    disabled: false,
  }));

  const document = {
    getElementById: (id) => els.get(id) || null,
  };
  const fetch = (url, opt) => {
    calls.push({ url, opt });
    return Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: () => Promise.resolve({ ok: true }),
    });
  };
  const performanceStub = { now: () => clock.now() };

  const run = new Function(
    "document", "fetch", "setTimeout", "clearTimeout", "performance", "window",
    SCRIPT);
  run(document, fetch, clock.setTimeout, clock.clearTimeout,
      performanceStub, window);

  return {
    clock, calls, window,
    el: (id) => els.get(id),
    sync: (s) => window.__portalWifiSync(s),
    clickRetry: () => els.get("wifiRetryBtn").fire("click"),
  };
}

// --- 页面行为：跑的是从 src/main.cpp 抽出来的真实脚本 ------------------------
test("connecting → 灰、文案含「正在连接」", async () => {
  const p = openPortal();
  assert.ok(typeof p.window.__portalWifiSync === "function",
            "脚本必须挂 __portalWifiSync 钩子（由自检轮询喂数据）");
  p.sync({ wifi: "connecting", ssid: "Home", ip: "", elapsed: 3,
           wifiText: "正在连接 Home…（已 3 秒）" });
  await flush();

  assert.match(p.el("wifiState").textContent, /正在连接/);
  assert.match(p.el("wifiState").textContent, /3 秒/);
  assert.equal(p.el("wifiState").classList.contains("wifi-connecting"), true);
  assert.equal(p.el("wifiState").classList.contains("wifi-ok"), false);
  assert.equal(p.el("wifiState").classList.contains("wifi-fail"), false);
  assert.equal(p.el("wifiRetryBtn").hidden, true, "连接中不显示重试按钮");
});

test("ok → 绿、显示 IP", async () => {
  const p = openPortal();
  p.sync({ wifi: "connecting", ssid: "Home", elapsed: 1,
           wifiText: "正在连接 Home…（已 1 秒）" });
  await flush();
  await step(p.clock, 1100);                 // 越过 1 s 节流窗口
  p.sync({ wifi: "ok", ssid: "Home", ip: "192.168.1.23", elapsed: 5,
           wifiText: "已连接 Home · IP 192.168.1.23" });
  await flush();

  assert.match(p.el("wifiState").textContent, /已连接/);
  assert.match(p.el("wifiState").textContent, /192\.168\.1\.23/, "必须显示 IP");
  assert.equal(p.el("wifiState").classList.contains("wifi-ok"), true);
  assert.equal(p.el("wifiState").classList.contains("wifi-connecting"), false);
  assert.equal(p.el("wifiRetryBtn").hidden, true);
});

test("fail → 红、显示「重试连接」按钮", async () => {
  const p = openPortal();
  p.sync({ wifi: "connecting", ssid: "Home", elapsed: 1,
           wifiText: "正在连接 Home…（已 1 秒）" });
  await flush();
  await step(p.clock, 1100);
  p.sync({ wifi: "fail", ssid: "Home", ip: "", elapsed: 25,
           wifiText: "连接失败：无法连接，密码可能不对" });
  await flush();

  assert.match(p.el("wifiState").textContent, /密码可能不对/);
  assert.equal(p.el("wifiState").classList.contains("wifi-fail"), true);
  assert.equal(p.el("wifiRetryBtn").hidden, false, "失败态必须显示重试按钮");
});

test("点重试：POST /wifi/retry、按钮禁用 30 s、状态回 connecting、连点只发一次", async () => {
  const p = openPortal();
  p.sync({ wifi: "connecting", ssid: "Home", elapsed: 1,
           wifiText: "正在连接 Home…（已 1 秒）" });
  await flush();
  await step(p.clock, 1100);
  p.sync({ wifi: "fail", ssid: "Home", elapsed: 25,
           wifiText: "连接失败：无法连接，密码可能不对" });
  await flush();
  assert.equal(p.el("wifiRetryBtn").hidden, false);

  p.clickRetry();
  await flush();

  assert.equal(p.el("wifiRetryBtn").disabled, true, "点击后按钮必须禁用");
  assert.match(p.el("wifiState").textContent, /正在连接/, "状态行乐观回到连接中");
  assert.equal(p.el("wifiState").classList.contains("wifi-connecting"), true);
  assert.equal(p.el("wifiState").classList.contains("wifi-fail"), false);

  const post = p.calls.find((c) => c.url === "/wifi/retry");
  assert.ok(post, "没有请求 /wifi/retry");
  assert.equal(post.opt.method, "POST", "必须是 POST");

  // 连点两次只发一次请求（按钮禁用后第二次直接 return）。
  p.clickRetry();
  await flush();
  assert.equal(p.calls.filter((c) => c.url === "/wifi/retry").length, 1,
               "按钮禁用后不应再发 /wifi/retry");

  // 30 s 后按钮恢复可用。
  await step(p.clock, 30000);
  assert.equal(p.el("wifiRetryBtn").disabled, false, "30 s 后按钮恢复");
});

test("响应缺 wifiText → 不改状态行（保持服务端文本），不显示 undefined", async () => {
  const p = openPortal();
  const serverText = "未连接，正在连接…，已保存 Home";
  assert.equal(p.el("wifiState").textContent, serverText);

  // 旧固件 / 旧页面：只有 WS-602 的字段，没有 WiFi 字段。
  p.sync({ seq: 3, index: 0, gpio: 0, left: 290 });
  await flush();
  assert.equal(p.el("wifiState").textContent, serverText,
               "缺 wifiText 必须保持服务端渲染文本");
  assert.equal(p.el("wifiState").classList.contains("wifi-connecting"), true,
               "也不能乱改三态类名");
  assert.equal(p.el("wifiRetryBtn").hidden, true);
  assert.doesNotMatch(p.el("wifiState").textContent, /undefined|NaN/);

  // 紧随其后的正常响应仍能落地（守卫只忽略缺字段的那一次）。
  p.sync({ wifi: "ok", ssid: "Home", ip: "10.0.0.5", elapsed: 4,
           wifiText: "已连接 Home · IP 10.0.0.5" });
  await flush();
  assert.match(p.el("wifiState").textContent, /10\.0\.0\.5/);
});

// --- 源码断言：页面结构 / 样式兜底 / 路由 / 追加顺序 / 转义 -------------------
const CFG_PAGE = sliceBetween(SRC, "static String cfgPage(",
                              "static const char* otaUpdateErrorText", "cfgPage()");
const KEYS_STATE = sliceBetween(SRC, 'cfgServer.on("/keys/state", HTTP_GET',
                                'cfgServer.on("/extend", HTTP_POST', "/keys/state 路由");
const RETRY_POST = sliceBetween(SRC, 'cfgServer.on("/wifi/retry", HTTP_POST',
                                'cfgServer.on("/wifi/retry", HTTP_GET', "POST /wifi/retry 路由");
const RETRY_GET = sliceBetween(SRC, 'cfgServer.on("/wifi/retry", HTTP_GET',
                               'cfgServer.on("/save", HTTP_POST', "GET /wifi/retry 路由");

test("页面：#wifiState 三态 span + 默认 hidden 的重试按钮（无 JS 与今天一致）", () => {
  assert.match(CFG_PAGE, /id='wifiState' class='/, "缺 #wifiState span");
  assert.match(CFG_PAGE, /wifi-connecting/, "缺连接中类名");
  assert.match(CFG_PAGE, /wifi-ok/, "缺已连接类名");
  assert.match(CFG_PAGE, /wifi-fail/, "缺失败类名");
  assert.match(CFG_PAGE, /id='wifiRetryBtn'[^>]*hidden/, "重试按钮必须默认 hidden");
  assert.match(CFG_PAGE, /type='button'/, "重试按钮必须 type=button，不会提交 WiFi 表单");
  assert.match(CFG_PAGE, /重试连接/, "缺重试按钮文案");

  const style = sliceBetween(CFG_PAGE, "<style>", "</style>", "cfgPage 样式表");
  assert.ok(style.includes("#wifiState.wifi-ok"), "缺绿色已连接样式");
  assert.ok(style.includes("#wifiState.wifi-fail"), "缺红色失败样式");
  assert.ok(style.includes("#wifiRetryBtn[hidden]{display:none}"),
            "缺 [hidden] 兜底：ID 规则的 display 会盖过 UA [hidden]（WS-604 教训）");
});

test("POST /wifi/retry 存在：用已保存凭据 begin、重置 25 s 计时、无 delay", () => {
  assert.match(RETRY_POST, /WiFi\.begin\(/, "必须用已保存凭据 WiFi.begin()");
  assert.match(RETRY_POST, /wifiAttemptReset\(/, "必须重置本次尝试起点（重新计时 25 s）");
  assert.match(SRC, /g_wifiAttemptStart\s*=\s*millis\(\)/,
               "wifiAttemptReset 必须把尝试起点重置为当前 millis()");
  assert.match(RETRY_POST, /throttled/, "必须有 30 s 节流");
  assert.match(RETRY_POST, /WIFI_RETRY_THROTTLE_MS/, "节流量必须是常量");
  assert.doesNotMatch(RETRY_POST, /delay\s*\(/,
                      "/wifi/retry 里不能 delay（会阻塞 /keys/state）");
  assert.match(RETRY_POST, /send\(200, "application\/json"/, "Content-Type 必须是 application/json");
  assert.match(RETRY_POST, /sendHeader\("Connection", "close"\)/, "缺 Connection: close 响应头");
});

test("GET /wifi/retry 返回 405 且不动任何尝试状态（与 /extend / /exit 同风格）", () => {
  assert.match(RETRY_GET, /send\(405/, "GET /wifi/retry 必须返回 405");
  assert.doesNotMatch(RETRY_GET, /WiFi\.begin|wifiAttemptReset|g_lastWifiRetry/,
                      "GET 不能发起连接或改动尝试状态");
});

test("/keys/state JSON 仍含 seq 与 left（防回退 WS-602），wifi 字段追加在其后", () => {
  const k = unescape(KEYS_STATE);
  assert.match(k, /"seq"/, "JSON 缺 seq");
  assert.match(k, /"index"/, "JSON 缺 index");
  assert.match(k, /"gpio"/, "JSON 缺 gpio");
  assert.match(k, /"left"/, "JSON 缺 left（WS-602 回退）");
  assert.match(KEYS_STATE, /g_configDeadline/, "left 仍由截止时间算出");
  for (const f of ["wifi", "ssid", "ip", "elapsed", "wifiText"]) {
    assert.match(k, new RegExp(`"${f}"`), `JSON 缺追加字段 ${f}`);
  }
  // 只追加、不重排：seq < ... < left < wifi。
  const iSeq = k.indexOf('"seq"');
  const iLeft = k.indexOf('"left"');
  const iWifi = k.indexOf('"wifi"');
  assert.ok(iSeq < iLeft && iLeft < iWifi,
            "wifi 字段必须追加在 left 之后，seq/index/gpio/left 保持原顺序");
  // SSID 走 JSON 转义（防止引号 / emoji / 中文破坏 JSON）。
  assert.match(KEYS_STATE, /jsonEsc\(w\.ssid\)/, "ssid 必须 JSON 转义");
  assert.match(KEYS_STATE, /jsonEsc\(w\.wifiText\)/, "wifiText 必须 JSON 转义");
});

test("固件文案：5 GHz / 隐藏网络提示 + 25 s 无 IP 判定 + 最后一次失败码", () => {
  assert.match(SRC, /2\.4 GHz 且名称无误/, "缺 2.4 GHz / 5 GHz 提示");
  assert.match(SRC, /若为隐藏网络，确认名称大小写与是否开启广播/, "缺隐藏网络提示");
  assert.match(SRC, /WIFI_ATTEMPT_TIMEOUT_MS/, "缺 25 s 超时常量");
  assert.match(SRC, /g_wifiFailReason\s*=\s*\(int\)st/, "必须记住最后一次明确失败码");
  assert.match(SRC, /正在连接 /, "连接中文案缺「正在连接」");
  assert.match(SRC, /秒）/, "连接中文案缺秒数");
});

test("wifi-state 脚本：单一轮询、textContent 写入、不新开 /keys/state 请求", () => {
  assert.match(SCRIPT, /window\.__portalWifiSync\s*=/, "必须挂 __portalWifiSync 钩子");
  assert.match(SCRIPT, /\.textContent\s*=/, "必须用 textContent 写入");
  assert.doesNotMatch(SCRIPT, /innerHTML/, "不能拼 HTML（SSID 可能含引号/emoji）");
  assert.doesNotMatch(SCRIPT, /fetch\('\/keys\/state'|setInterval[^;]*\/keys\/state/,
                      "不能新开第二条 /keys/state 轮询（WS-606 教训）");
  const selfcheck = sliceBetween(SRC, "\"<script id='selfcheck'>\"",
                                 "\"</script>\"", "selfcheck 脚本");
  assert.match(selfcheck, /__portalWifiSync\(s\)/,
               "自检成功回调必须把整份响应交给 wifi-state 钩子（复用同一条轮询）");
});

// --- WS-614：节流与窗口对齐的行为用例（不再只有源码正则断言） ----------------
test("1 s 节流：1 秒内第二次响应不落 DOM，越窗后生效", async () => {
  const p = openPortal();
  p.sync({ wifi: "connecting", ssid: "Home", elapsed: 1,
           wifiText: "正在连接 Home…（已 1 秒）" });
  await flush();
  p.sync({ wifi: "ok", ssid: "Home", ip: "10.0.0.9", elapsed: 2,
           wifiText: "已连接 Home · IP 10.0.0.9" });
  await flush();
  assert.match(p.el("wifiState").textContent, /正在连接/,
               "1 s 内的第二次同步必须被节流丢弃");
  assert.equal(p.el("wifiState").classList.contains("wifi-ok"), false);

  await step(p.clock, 1100);
  p.sync({ wifi: "ok", ssid: "Home", ip: "10.0.0.9", elapsed: 3,
           wifiText: "已连接 Home · IP 10.0.0.9" });
  await flush();
  assert.match(p.el("wifiState").textContent, /10\.0\.0\.9/, "越过 1 s 后必须落地");
  assert.equal(p.el("wifiState").classList.contains("wifi-ok"), true);
});

test("25–30 s 不出现「可见但禁用」的重试按钮：fail 且冷却 30 s 后才露出", async () => {
  const p = openPortal();
  p.sync({ wifi: "connecting", ssid: "Home", elapsed: 1,
           wifiText: "正在连接 Home…（已 1 秒）" });
  await flush();
  await step(p.clock, 1100);
  p.sync({ wifi: "fail", ssid: "Home", elapsed: 25,
           wifiText: "连接失败：无法连接，密码可能不对" });
  await flush();
  assert.equal(p.el("wifiRetryBtn").hidden, false, "首次 fail 必须露出重试按钮");

  p.clickRetry();                          // lastRetry = 1100
  await flush();
  assert.equal(p.el("wifiRetryBtn").disabled, true);

  // 服务端 25 s 判 fail，但客户端 30 s 冷却未到：按钮必须继续隐藏。
  await step(p.clock, 25000);              // t = 26100
  p.sync({ wifi: "fail", ssid: "Home", elapsed: 25,
           wifiText: "连接失败：无法连接，密码可能不对" });
  await flush();
  assert.equal(p.el("wifiRetryBtn").hidden, true,
               "25 s fail 但冷却未到 -> 不露出禁用按钮");
  assert.equal(p.el("wifiRetryBtn").disabled, true, "此时仍在 30 s 禁用期");

  // 越过 30 s 冷却后，下一次 fail 响应才把可点的按钮露出来。
  await step(p.clock, 6000);               // t = 32100 >= 1100 + 30000
  p.sync({ wifi: "fail", ssid: "Home", elapsed: 25,
           wifiText: "连接失败：无法连接，密码可能不对" });
  await flush();
  assert.equal(p.el("wifiRetryBtn").hidden, false, "冷却结束后露出重试按钮");
  assert.equal(p.el("wifiRetryBtn").disabled, false, "按钮此时可点");
});

test("WS-614 固件状态机：last-OK 窗口 + portal 观察态（改固件即变红）", () => {
  assert.match(SRC, /g_wifiLastOkAt/, "缺 g_wifiLastOkAt（连上后掉线一帧的根因）");
  assert.match(SRC, /g_wifiAttemptActive/, "缺 g_wifiAttemptActive（进配网页即起 25 s 计时）");
  assert.match(SRC, /windowStart = g_wifiAttemptStart/, "失败窗口必须从尝试起点起算");
  assert.match(SRC, /g_wifiLastOkAt > windowStart/, "窗口必须取 max(attemptStart,lastOk)");
  assert.match(SRC, /\(now - windowStart\) >= WIFI_ATTEMPT_TIMEOUT_MS/,
               "失败判定必须用 windowStart，不能再用 g_wifiAttemptStart");
  assert.match(SRC, /wifiAttemptReset\(ssid, false\)/, "portal 打开必须是观察态");
  assert.match(SRC, /wifiAttemptReset\(ssid, true\)/, "save/retry 必须起一次尝试");
  assert.match(SRC, /lastRetry===null/, "客户端按钮必须与 30 s 节流对齐");
});
