// WS-602 配网页「倒计时 + 延长 5 分钟」的回归用例。
//
// 运行：cd host && node --test test_portal_clock.mjs
//
// 配网页与路由都内联在 src/main.cpp 里（固件无测试框架，PC 上也没有跑
// WebServer 的桩）。这里从生产源文件里**原样抽取** <script id='portal-clock'>
// 那一段，在假的 DOM / fetch / 定时器 / performance.now 上真跑，再对页面
// 结构与 /extend 路由做源码断言。改动 cfgPage() 里的脚本或路由，会让这里的
// 断言真的失败 —— 不是在这份测试里复制一份倒计时逻辑自证。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC_PATH = new URL("../src/main.cpp", import.meta.url);
const SRC = readFileSync(SRC_PATH, "utf8");
const OPEN_TAG = "\"<script id='portal-clock'>\"";
const CLOSE_TAG = "\"})();</script>\"";

/** 取 [startMarker, endMarker) 之间的源码，标记缺失直接失败。 */
function sliceBetween(source, startMarker, endMarker, what) {
  const start = source.indexOf(startMarker);
  assert.ok(start >= 0, `src/main.cpp 里找不到${what}的起点 (${startMarker})`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `src/main.cpp 里找不到${what}的终点 (${endMarker})`);
  return source.slice(start, end);
}

/** 抽出内联倒计时脚本（拼好 C++ 的多段字符串字面量），返回可执行的 JS。 */
function extractClockScript(source) {
  const start = source.indexOf(OPEN_TAG);
  assert.ok(start >= 0, "src/main.cpp 里找不到 <script id='portal-clock'> 起始标记");
  const end = source.indexOf(CLOSE_TAG, start);
  assert.ok(end > start, "src/main.cpp 里找不到 portal-clock 脚本的结束标记");

  const region = source.slice(start, end + CLOSE_TAG.length);
  // 字面量之间可能夹着 C++ 行注释（里面可能带引号），先丢掉。
  const kept = region.split("\n").filter((line) => !line.trim().startsWith("//")).join("\n");
  const parts = [];
  const re = /"((?:[^"\\]|\\.)*)"/g;
  let m;
  while ((m = re.exec(kept)) !== null) parts.push(JSON.parse(`"${m[1]}"`));
  assert.ok(parts.length > 0, "portal-clock 脚本区域里没有任何字符串字面量");
  const js = parts.join("");
  const tag = "<script id='portal-clock'>";
  assert.ok(js.startsWith(`${tag}(function(){`), "抽出来的不是一段 IIFE");
  return js.slice(tag.length).replace(/<\/script>$/, "");
}

const SCRIPT = extractClockScript(SRC);
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
      remove: (c) => classes.delete(c),
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

/** 虚拟时钟：支持 setTimeout / setInterval，now() 与定时器共用同一条时间轴。 */
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
    now: () => now,
    setTimeout: (fn, ms) => set(fn, ms, 0),
    setInterval: (fn, ms) => set(fn, ms, ms || 1),
    clearTimeout: (id) => timers.delete(id),
    clearInterval: (id) => timers.delete(id),
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
  for (let i = 0; i < 12; i++) await Promise.resolve();
};
const step = async (clock, ms) => {
  clock.advance(ms);
  await flush();
};

/**
 * 起一页。
 *   queue       轮询 /keys/state 的响应序列；用完走 exhausted()。
 *   exhausted   queue 耗尽后的默认响应（默认 {} —— 有响应但缺 left，倒计时继续按
 *               lastLeft 递减、不触发失败计数）。
 *   extendBody  fetch('/extend', POST) 的返回值。
 * 每个响应描述符：{body: {...}} 成功 / {fail:true} 网络层失败 / {status:500} HTTP 失败。
 */
function openPortal({
  queue = [],
  exhausted = () => ({ body: {} }),
  extendBody = { left: 300, capped: false },
} = {}) {
  const els = new Map();
  const calls = [];
  const clock = makeClock();

  const document = {
    getElementById(id) {
      if (!els.has(id)) els.set(id, makeElement(id));
      return els.get(id);
    },
  };
  // 照页面初始状态铺好：两个块默认 hidden，按钮带 HTML 里的初始文案。
  els.set("portalClock", Object.assign(makeElement("portalClock"), { hidden: true }));
  els.set("clockText", makeElement("clockText"));
  els.set("extendBtn", Object.assign(makeElement("extendBtn"), { textContent: "延长 5 分钟" }));
  els.set("portalExpired", Object.assign(makeElement("portalExpired"), { hidden: true }));

  const response = (d) => {
    if (!d || d.fail) return Promise.reject(new TypeError("offline"));
    const status = d.status || 200;
    return Promise.resolve({
      ok: status < 400,
      status,
      headers: { get: () => "application/json" },
      json: () => Promise.resolve(d.body),
    });
  };
  const fetch = (url, opt) => {
    calls.push({ url, opt });
    if (url === "/extend") return response({ body: extendBody });
    const item = queue.length ? queue.shift() : null;
    return response(item === null ? exhausted() : item);
  };
  const performanceStub = { now: () => clock.now() };

  const run = new Function(
    "document", "fetch", "setTimeout", "clearTimeout",
    "setInterval", "clearInterval", "performance", SCRIPT);
  run(document, fetch, clock.setTimeout, clock.clearTimeout,
      clock.setInterval, clock.clearInterval, performanceStub);

  return {
    document, clock, calls, els,
    el: (id) => document.getElementById(id),
    clickExtend: () => els.get("extendBtn").fire("click"),
  };
}

// --- 页面行为：跑的是从 src/main.cpp 抽出来的真实脚本 ------------------------
test("正常态文案 + 逐秒递减（两次轮询之间，不累加）", async () => {
  const p = openPortal({ queue: [{ body: { left: 272 } }] });
  await flush();

  assert.equal(p.el("portalClock").hidden, false, "拿到 left 后倒计时行应显示");
  assert.equal(p.el("portalClock").classList.contains("urgent"), false);
  assert.equal(p.el("clockText").textContent, "配网模式剩余 4:32 · 到时自动关闭热点");

  // 之后的响应都不带 left：倒计时只能靠 1 s 定时器按 lastLeft - 经过时间 递减。
  await step(p.clock, 400);
  await step(p.clock, 400);
  await step(p.clock, 200);                 // t=1000 -> 1 s 定时器
  assert.equal(p.el("clockText").textContent, "配网模式剩余 4:31 · 到时自动关闭热点");
  await step(p.clock, 1000);                // t=2000
  assert.equal(p.el("clockText").textContent, "配网模式剩余 4:30 · 到时自动关闭热点");
});

test("切后台回来：下一次带 left 的轮询覆盖旧值，而不是累加", async () => {
  // 中间两次轮询都缺 left（模拟后台暂停），第 4 次带新值回来；页面必须用
  // 设备的新值覆盖，而不是拿旧值继续往下减。
  const p = openPortal({
    queue: [{ body: { left: 272 } }, {}, {}, { body: { left: 120 } }],
  });
  await flush();
  await step(p.clock, 400);
  await step(p.clock, 400);
  await step(p.clock, 400);                 // t=1200 -> 第 4 次轮询给新值
  assert.equal(p.el("clockText").textContent, "配网模式剩余 2:00 · 到时自动关闭热点");
  await step(p.clock, 1000);                // 之后仍按新值逐秒递减
  assert.equal(p.el("clockText").textContent, "配网模式剩余 1:59 · 到时自动关闭热点");
});

test("剩余 ≤60 s 切琥珀类 + 「即将关闭」；>60 s / =60 s 边界正确", async () => {
  const p = openPortal({ queue: [{ body: { left: 58 } }] });
  await flush();
  assert.equal(p.el("portalClock").classList.contains("urgent"), true);
  assert.match(p.el("clockText").textContent, /即将关闭/);
  assert.equal(p.el("clockText").textContent,
               "即将关闭 · 配网模式剩余 0:58 · 到时自动关闭热点");

  const edge = openPortal({ queue: [{ body: { left: 60 } }] });
  await flush();
  assert.equal(edge.el("portalClock").classList.contains("urgent"), true, "=60 s 也算即将关闭");

  const normal = openPortal({ queue: [{ body: { left: 120 } }] });
  await flush();
  assert.equal(normal.el("portalClock").classList.contains("urgent"), false);
});

test("缺 left（旧页面/旧固件）时 #portalClock 保持隐藏，不显示 undefined", async () => {
  const p = openPortal({ queue: [{ body: {} }] });
  await flush();
  await step(p.clock, 1000);
  await step(p.clock, 1000);
  assert.equal(p.el("portalClock").hidden, true);
  assert.equal(p.el("clockText").textContent, "");
  assert.doesNotMatch(p.el("clockText").textContent, /undefined|NaN/);
  assert.equal(p.el("portalExpired").hidden, true);
});

test("点「延长 5 分钟」→ POST /extend，按返回值更新、不重载、输入保留", async () => {
  const p = openPortal({
    queue: [{ body: { left: 40 } }],
    extendBody: { left: 300, capped: false },
  });
  await flush();
  assert.equal(p.el("clockText").textContent,
               "即将关闭 · 配网模式剩余 0:40 · 到时自动关闭热点");

  p.clickExtend();
  await flush();

  const ext = p.calls.find((c) => c.url === "/extend");
  assert.ok(ext, "没有请求 /extend");
  assert.equal(ext.opt.method, "POST", "/extend 必须是 POST");
  assert.equal(p.el("clockText").textContent, "配网模式剩余 5:00 · 到时自动关闭热点");
  assert.equal(p.el("portalClock").classList.contains("urgent"), false);
  assert.equal(p.el("extendBtn").disabled, false);
  // 无重载：脚本不碰 location，表单元素保持原值。
  assert.equal(p.el("extendBtn").textContent, "延长 5 分钟");
});

test("capped=true → 按钮禁用 + 上限文案，left 不再增大", async () => {
  const p = openPortal({
    queue: [{ body: { left: 30 } }],
    extendBody: { left: 0, capped: true },
  });
  await flush();
  p.clickExtend();
  await flush();

  assert.equal(p.el("extendBtn").disabled, true);
  assert.equal(p.el("extendBtn").textContent, "已达单次配网上限，请退出后重新进入");
  assert.match(p.el("clockText").textContent, /0:00/);
  assert.doesNotMatch(p.el("clockText").textContent, /5:00/, "capped 时倒计时不能回到 5:00");

  const before = p.calls.filter((c) => c.url === "/extend").length;
  p.clickExtend();
  await flush();
  assert.equal(p.calls.filter((c) => c.url === "/extend").length, before,
               "按钮置灰后不应再发 /extend");
});

test("连续 3 次轮询失败且曾收到 left → 页面顶部红色超时块", async () => {
  const p = openPortal({
    queue: [
      { body: { left: 272 } },
      { fail: true }, { fail: true }, { fail: true },
    ],
    exhausted: () => ({ fail: true }),
  });
  await flush();
  assert.equal(p.el("portalExpired").hidden, true);

  await step(p.clock, 400);
  assert.equal(p.el("portalExpired").hidden, true, "1 次失败不该弹红块");
  await step(p.clock, 400);
  assert.equal(p.el("portalExpired").hidden, true, "2 次失败不该弹红块");
  await step(p.clock, 400);
  assert.equal(p.el("portalExpired").hidden, false, "3 次失败应弹红块");
  assert.equal(p.el("portalExpired").textContent,
               "配网已退出（5 分钟超时），热点已关闭。重新按住第 10 个输入上电可再次进入。");
});

test("从未收到 left（旧页面/旧固件）→ 3 次失败也不误报超时", async () => {
  const p = openPortal({ queue: [{ body: {} }], exhausted: () => ({ fail: true }) });
  await flush();
  for (let i = 0; i < 3; i++) await step(p.clock, 400);
  assert.equal(p.el("portalExpired").hidden, true);
  assert.equal(p.el("portalClock").hidden, true);
});

// --- 源码断言：页面结构 / 路由 / 退出日志 -----------------------------------
const CFG_PAGE = sliceBetween(SRC, "static String cfgPage(",
                              "static const char* otaUpdateErrorText", "cfgPage()");
const KEYS_STATE = sliceBetween(SRC, 'cfgServer.on("/keys/state", HTTP_GET',
                                'cfgServer.on("/extend", HTTP_POST', "/keys/state 路由");
const EXTEND_POST = sliceBetween(SRC, 'cfgServer.on("/extend", HTTP_POST',
                                 'cfgServer.on("/extend", HTTP_GET', "/extend POST 路由");
const EXTEND_GET = sliceBetween(SRC, 'cfgServer.on("/extend", HTTP_GET',
                                'cfgServer.on("/update", HTTP_GET', "/extend GET 路由");

test("页面顶部红块 / #portalClock 默认 hidden（无 JS 与今天一致），位置在页头", () => {
  assert.match(CFG_PAGE, /id='portalClock' hidden/, "#portalClock 必须默认 hidden");
  assert.match(CFG_PAGE, /id='portalExpired'[^>]*hidden/, "顶部红块必须默认 hidden");
  assert.match(CFG_PAGE, /id='extendBtn' type='button'/, "延长必须是 type=button，不会提交表单");
  assert.match(CFG_PAGE, /延长 5 分钟/, "缺按钮文案");

  const fwAt = CFG_PAGE.indexOf("固件: ");
  const clockAt = CFG_PAGE.indexOf("id='portalClock'");
  const wifiAt = CFG_PAGE.indexOf("action='/save'");
  assert.ok(fwAt >= 0 && wifiAt >= 0);
  assert.ok(fwAt < clockAt && clockAt < wifiAt,
            "#portalClock 必须在「设备/固件」段之后、WiFi 表单之前");
});

test("/keys/state JSON 增加 left（来自截止时间，钳到 0）", () => {
  assert.match(unescape(KEYS_STATE), /"left"/, "JSON 缺 left 字段");
  assert.match(KEYS_STATE, /g_configDeadline/, "left 必须由截止时间算出，不能是客户端估算");
  assert.match(KEYS_STATE, /\(now >= g_configDeadline\)\s*\?\s*0UL/, "到点后 left 必须钳到 0");
});

test("POST /extend 存在：+5 分钟、封顶 30 分钟、返回 {left,capped} JSON", () => {
  assert.match(EXTEND_POST, /g_configDeadline\s*\+\s*CONFIG_MODE_MS/, "每点一次 +5 分钟");
  assert.match(EXTEND_POST, /30UL\s*\*\s*60UL\s*\*\s*1000UL/, "上限必须是进入 + 30 分钟");
  assert.match(EXTEND_POST, /capped/, "必须返回 capped 标志");
  assert.match(unescape(EXTEND_POST), /"left"/, "必须返回 left");
  assert.match(EXTEND_POST, /send\(200, "application\/json"/, "Content-Type 必须是 application/json");
});

test("GET /extend 返回 405 且不动截止时间（与 /exit 同风格）", () => {
  assert.match(EXTEND_GET, /send\(405/, "GET /extend 必须返回 405");
  assert.doesNotMatch(EXTEND_GET, /g_configDeadline|g_configModeStart/,
                      "GET /extend 不能改动截止时间");
});

test("超时判定读截止时间变量；三条退出路径都打 reason + duration", () => {
  assert.match(SRC, /g_configMode\s*&&\s*millis\(\)\s*>\s*g_configDeadline/,
               "超时必须读 g_configDeadline，而不是 g_configModeStart + 常量");
  assert.match(SRC, /leave reason=timeout duration=/, "缺 timeout 退出日志");
  assert.match(SRC, /leave reason=long-press duration=/, "缺 long-press 退出日志");
  assert.match(SRC, /leave reason=exit duration=/, "缺 exit 退出日志");
  assert.match(SRC, /g_configDeadline = g_configModeStart \+ CONFIG_MODE_MS/,
               "进入配网必须初始化截止时间");
});
