// WS-504 配网页「按键自检」前端逻辑的回归用例。
//
// 运行：node --test host/test_portal_selfcheck.mjs
//
// 页面脚本内联在 src/main.cpp 的 cfgPage() 里（配网页必须保持单文件、无外部
// 资源），所以这里从生产源文件里**原样抽取** <script id='selfcheck'> 那一段，
// 在假的 DOM / fetch / 定时器上真跑，而不是在这份测试里复制一份判定逻辑：
// 改动 cfgPage() 里的脚本会让这些断言真的失败。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC_PATH = new URL("../src/main.cpp", import.meta.url);
const OPEN_TAG = "\"<script id='selfcheck'>\"";
const CLOSE_TAG = "\"</script>\"";

/** 从 src/main.cpp 抽出内联自检脚本（拼好 C++ 的多段字符串字面量）。 */
function extractPortalScript(source) {
  const start = source.indexOf(OPEN_TAG);
  const end = source.indexOf(CLOSE_TAG);
  assert.ok(start >= 0, "src/main.cpp 里找不到自检脚本的起始标记");
  assert.ok(end > start, "src/main.cpp 里找不到自检脚本的结束标记");

  const region = source.slice(start + OPEN_TAG.length, end);
  const parts = [];
  const re = /"((?:[^"\\]|\\.)*)"/g;
  let m;
  while ((m = re.exec(region)) !== null) {
    parts.push(JSON.parse(`"${m[1]}"`));
  }
  assert.ok(parts.length > 0, "自检脚本区域里没有任何字符串字面量");
  return parts.join("");
}

const SCRIPT = extractPortalScript(readFileSync(SRC_PATH, "utf8"));

// --- 假的浏览器环境 --------------------------------------------------------
function makeElement(id) {
  const classes = new Set();
  return {
    id,
    textContent: "",
    hidden: false,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
    },
  };
}

function makeClock() {
  let now = 0;
  let nextId = 1;
  const pending = new Map();
  return {
    setTimeout(fn, ms) {
      const id = nextId++;
      pending.set(id, { at: now + (ms || 0), fn });
      return id;
    },
    clearTimeout(id) {
      pending.delete(id);
    },
    advance(ms) {
      const target = now + ms;
      for (;;) {
        let due = null;
        for (const [id, item] of pending) {
          if (item.at <= target && (!due || item.at < due.item.at)) due = { id, item };
        }
        if (!due) break;
        pending.delete(due.id);
        now = due.item.at;
        due.item.fn();
      }
      now = target;
    },
    get pendingCount() {
      return pending.size;
    },
  };
}

const flush = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};

/**
 * 起一页：inputs 是「行号 -> 输入框当前值」，queue 是待消费的响应。
 * 每个响应可以是 {status, type, body} 或 {error}（网络层失败 / 超时）。
 */
function openPortal({ inputs = {}, queue = [] } = {}) {
  const els = new Map();
  const calls = [];
  const clock = makeClock();
  const inputEls = new Map(
    Object.entries(inputs).map(([i, v]) => [Number(i), { value: v }]),
  );

  const document = {
    getElementById(id) {
      if (!els.has(id)) els.set(id, makeElement(id));
      return els.get(id);
    },
    querySelector(sel) {
      const m = /^input\[name=pin(\d+)\]$/.exec(sel);
      return m ? inputEls.get(Number(m[1])) || null : null;
    },
  };
  // 空状态文案由 HTML 提供（脚本只在按键后改写），这里照页面初始状态铺好。
  els.set("chkLast", Object.assign(makeElement("chkLast"), {
    textContent: "按下任意按键开始自检",
  }));

  const fetch = (url) => {
    calls.push(url);
    const item = queue.length ? queue.shift() : { error: new TypeError("offline") };
    if (item.error) return Promise.reject(item.error);
    return Promise.resolve({
      status: item.status || 200,
      ok: (item.status || 200) < 400,
      headers: { get: () => item.type || "application/json" },
      json: () => Promise.resolve(item.body),
    });
  };

  const run = new Function("document", "fetch", "setTimeout", "clearTimeout", SCRIPT);
  run(document, fetch, clock.setTimeout, clock.clearTimeout);

  return { document, els, calls, clock, inputEls, el: (id) => document.getElementById(id) };
}

test("首次响应只做基线：不闪高亮、不改空状态", async () => {
  const page = openPortal({ inputs: { 0: "13" }, queue: [{ body: { seq: 0, index: -1, gpio: -1 } }] });
  await flush();

  assert.equal(page.calls.length, 1);
  assert.equal(page.calls[0], "/keys/state");
  assert.equal(page.el("chkLast").textContent, "按下任意按键开始自检");
  assert.equal(page.el("row0").classList.contains("hit"), false);
});

test("按下按键 3（GPIO 27）→ 第 3 行高亮 + 行尾标签 + 最近触发", async () => {
  const page = openPortal({
    inputs: { 0: "13", 2: "27" },
    queue: [{ body: { seq: 0, index: -1, gpio: -1 } }, { body: { seq: 1, index: 2, gpio: 27 } }],
  });
  await flush();
  page.clock.advance(400);
  await flush();

  assert.equal(page.el("row2").classList.contains("hit"), true);
  assert.equal(page.el("tag2").textContent, "已触发 · GPIO 27");
  assert.equal(page.el("chkLast").textContent, "最近触发：按键 3（GPIO 27）");
  // 其它行不跟着亮
  assert.equal(page.el("row0").classList.contains("hit"), false);
});

test("1.5 秒后行高亮褪去，「最近触发」保留", async () => {
  const page = openPortal({
    inputs: { 2: "27" },
    queue: [{ body: { seq: 0, index: -1, gpio: -1 } }, { body: { seq: 1, index: 2, gpio: 27 } }],
  });
  await flush();
  page.clock.advance(400);
  await flush();
  assert.equal(page.el("row2").classList.contains("hit"), true);

  page.clock.advance(1500);
  await flush();
  assert.equal(page.el("row2").classList.contains("hit"), false);
  assert.equal(page.el("tag2").textContent, "");
  assert.equal(page.el("chkLast").textContent, "最近触发：按键 3（GPIO 27）");
});

test("行上的 GPIO 取当前输入框的值，不是固件报的旧值", async () => {
  const page = openPortal({
    inputs: { 4: "25" }, // 表单里已改成 25，固件这一轮报的还是按下时的 32
    queue: [{ body: { seq: 0, index: -1, gpio: -1 } }, { body: { seq: 1, index: 4, gpio: 32 } }],
  });
  await flush();
  page.clock.advance(400);
  await flush();

  assert.equal(page.el("row4").classList.contains("hit"), true);
  assert.equal(page.el("tag4").textContent, "已触发 · GPIO 25");
  assert.equal(page.el("chkLast").textContent, "最近触发：按键 5（GPIO 25）");
});

test("12 键布局的 index 10/11 只出现在自检区，没有行高亮", async () => {
  const page = openPortal({
    inputs: { 0: "0" },
    queue: [{ body: { seq: 0, index: -1, gpio: -1 } }, { body: { seq: 1, index: 10, gpio: 20 } }],
  });
  await flush();
  page.clock.advance(400);
  await flush();

  assert.equal(page.el("chkLast").textContent, "最近触发：第 11 键（GPIO 20）");
  for (let i = 0; i < 9; i++) {
    assert.equal(page.el("row" + i).classList.contains("hit"), false);
    assert.equal(page.el("tag" + i).textContent, "");
  }
});

test("404 → 「当前固件不支持按键自检」并停止轮询", async () => {
  const page = openPortal({ queue: [{ status: 404, type: "text/plain", body: {} }] });
  await flush();

  assert.equal(page.el("chkNote").textContent, "当前固件不支持按键自检");
  assert.equal(page.el("chkNote").hidden, false);
  const callsAfterStop = page.calls.length;
  page.clock.advance(5000);
  await flush();
  assert.equal(page.calls.length, callsAfterStop);
});

test("旧固件把 /keys/state 302 回首页（200 text/html）→ 同样提示不支持", async () => {
  const page = openPortal({ queue: [{ status: 200, type: "text/html", body: null }] });
  await flush();

  assert.equal(page.el("chkNote").textContent, "当前固件不支持按键自检");
});

test("连续 3 次拿不到响应 → 「配网已退出，请重新进入」", async () => {
  const page = openPortal({
    queue: [{ body: { seq: 0, index: -1, gpio: -1 } }],
  });
  await flush();
  for (let i = 0; i < 3; i++) {
    page.clock.advance(400);
    await flush();
  }

  assert.equal(page.el("chkNote").textContent, "配网已退出，请重新进入");
  assert.equal(page.el("chkNote").hidden, false);
});

test("连续 3 次有响应但状态码异常 → 「设备忙，稍后自动重试」", async () => {
  const page = openPortal({
    queue: [
      { body: { seq: 0, index: -1, gpio: -1 } },
      { status: 500, type: "text/plain", body: {} },
      { status: 500, type: "text/plain", body: {} },
      { status: 500, type: "text/plain", body: {} },
    ],
  });
  await flush();
  for (let i = 0; i < 3; i++) {
    page.clock.advance(400);
    await flush();
  }

  assert.equal(page.el("chkNote").textContent, "设备忙，稍后自动重试");
});

test("失败恢复后提示消失，且新按键照常高亮", async () => {
  const page = openPortal({
    inputs: { 6: "25" },
    queue: [
      { body: { seq: 0, index: -1, gpio: -1 } },
      { error: new TypeError("offline") },
      { body: { seq: 4, index: 6, gpio: 25 } },
    ],
  });
  await flush();
  page.clock.advance(400);
  await flush();
  page.clock.advance(400);
  await flush();

  assert.equal(page.el("chkNote").hidden, true);
  assert.equal(page.el("row6").classList.contains("hit"), true);
  assert.equal(page.el("tag6").textContent, "已触发 · GPIO 25");
});

test("设备重启后 seq 归零：先重新基线，之后的按键照常触发", async () => {
  const page = openPortal({
    inputs: { 1: "1" },
    queue: [
      { body: { seq: 0, index: -1, gpio: -1 } },
      { body: { seq: 5, index: 0, gpio: 0 } },
      { body: { seq: 0, index: -1, gpio: -1 } }, // 重启
      { body: { seq: 1, index: 1, gpio: 1 } },
    ],
  });
  await flush();
  page.clock.advance(400);
  await flush();
  assert.equal(page.el("row0").classList.contains("hit"), true);

  page.clock.advance(400);
  await flush();
  page.clock.advance(400);
  await flush();

  assert.equal(page.el("row1").classList.contains("hit"), true);
  assert.equal(page.el("chkLast").textContent, "最近触发：按键 2（GPIO 1）");
});
