// WS-641：编码器页「旋转自检」计数口径回归用例。
//
// 运行：cd host && node --test test_encoder_stats.mjs
//
// 断言对象是 host/static/encoder_stats.js 里的**生产函数**（浏览器里 app.js
// 调的就是 window.EncoderStats 这一份），不在这里复制一份判定逻辑。改动
// 5 s 窗口、清零基线语义、断开冻结，或者让旧后端缺字段时抛错，都会让这里
// 真的变红。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import EncoderStats from "./static/encoder_stats.js";

const { RECENT_WINDOW_S, makeBaseline, view } = EncoderStats;

const DEV = "aa:bb";
const NOW = 1000; // 固定的客户端秒级时间，所有用例自己注入，不依赖真实时钟

/** 这台设备的原始累计值；各用例只覆盖自己关心的字段。 */
const statsFor = (over) => ({
  [DEV]: Object.assign({ cw: 0, ccw: 0, last_dir: "CW", age: 0.3 }, over),
});

const at = (stats, over) =>
  view(Object.assign({ stats, devId: DEV, connected: true, now: NOW }, over));

test("累加：CW +3、CCW +2 → 两列分别显示 3 / 2", () => {
  const v = at(statsFor({ cw: 3, ccw: 2, last_dir: "CCW", age: 0.4 }));
  assert.equal(v.available, true);
  assert.equal(v.cw, 3);
  assert.equal(v.ccw, 2);
  assert.equal(v.frozen, false);
  assert.deepEqual(v.recent, { dir: "CCW", age: 0.4 });
});

test("recent：5 s 内保留，超过 5 s 回落 null（边界 5 s 仍算）", () => {
  assert.deepEqual(at(statsFor({ age: 0.8, last_dir: "CW" })).recent, {
    dir: "CW",
    age: 0.8,
  });
  // 边界：恰好 5 s 仍命中（服务端 age 是浮点，别把窗口判反）。
  assert.deepEqual(at(statsFor({ age: RECENT_WINDOW_S })).recent, {
    dir: "CW",
    age: RECENT_WINDOW_S,
  });
  assert.equal(at(statsFor({ age: 5.01 })).recent, null);
  assert.equal(at(statsFor({ age: 60 })).recent, null);
  // 计数不受窗口影响：转完停下，数字留着，只有「最近」回落。
  const stale = at(statsFor({ cw: 7, ccw: 1, age: 12 }));
  assert.equal(stale.cw, 7);
  assert.equal(stale.recent, null);
});

test("清零基线：只计 clearedAt 之后的计数，清零前的旧方向也不再算「最近」", () => {
  // 清零那一刻：累计 5 / 1。
  const baseline = makeBaseline({ cw: 5, ccw: 1 }, NOW);

  // 清零后立刻又来一条 CW：累计 6 → 显示 1，不是 6。
  const next = at(statsFor({ cw: 6, ccw: 1, age: 0.2 }), { baseline, now: NOW + 1 });
  assert.equal(next.cw, 1);
  assert.equal(next.ccw, 0);
  assert.deepEqual(next.recent, { dir: "CW", age: 0.2 });

  // 再来两条 CCW：累计 6 / 3 → ↺ 2。
  const more = at(statsFor({ cw: 6, ccw: 3, last_dir: "CCW", age: 0.1 }), {
    baseline,
    now: NOW + 1,
  });
  assert.equal(more.cw, 1);
  assert.equal(more.ccw, 2);

  // 停在清零前那条事件上（事件时刻 999.5 < 清零时刻 1000）：「最近」必须
  // 一起清空，否则清完零还挂着旧方向。
  const old = at(statsFor({ cw: 6, ccw: 1, age: 1.5 }), { baseline, now: NOW + 1 });
  assert.equal(old.cw, 1);
  assert.equal(old.recent, null);
});

test("断开：frozen=true，计数保持最后值，recent 不残留", () => {
  const v = at(statsFor({ cw: 9, ccw: 4, age: 0.5 }), { connected: false });
  assert.equal(v.frozen, true);
  assert.equal(v.cw, 9);
  assert.equal(v.ccw, 4);
  assert.equal(v.recent, null, "断连时不能还显示「顺时针 · 0.5 s 前」");
});

test("缺 encoder_stats（旧后端）/ 该设备还没转过 → 初始视图，不报错", () => {
  const empty = { available: false, cw: 0, ccw: 0, recent: null, frozen: false };
  assert.deepEqual(view({ stats: null, devId: DEV, connected: true, now: NOW }), empty);
  assert.deepEqual(
    view({ stats: undefined, devId: DEV, connected: true, now: NOW }),
    empty,
  );
  assert.deepEqual(view({ devId: DEV, connected: true, now: NOW }), empty);

  // 后端支持、但这台设备还没转过：可用且全 0（显示「转动编码器开始自检」）。
  const idle = at({});
  assert.equal(idle.available, true);
  assert.equal(idle.cw, 0);
  assert.equal(idle.ccw, 0);
  assert.equal(idle.recent, null);

  // 别的设备的记录不算这台设备的。
  const other = at({ "cc:dd": { cw: 8, ccw: 0, last_dir: "CW", age: 0.2 } });
  assert.equal(other.cw, 0);
  assert.equal(other.recent, null);
});

test("脏数据不抛错：缺字段 / 负数 / 字符串按 0 或忽略处理", () => {
  const v = at(statsFor({ cw: "x", ccw: -4, last_dir: "SIDEWAYS", age: "soon" }));
  assert.equal(v.cw, 0);
  assert.equal(v.ccw, 0);
  assert.equal(v.recent, null, "方向不是 CW/CCW 时不能当成一次旋转");
});

test("生产页面真的用上了这个口径（模块没接进渲染层时也要变红）", () => {
  const app = readFileSync(new URL("./static/app.js", import.meta.url), "utf8");
  assert.match(app, /EncoderStats\.view\(/, "renderEncoderSelfCheck 没有调用生产纯函数");
  assert.match(app, /EncoderStats\.makeBaseline\(/, "清零没有记前端基线");

  // 清零必须是纯前端：不发请求、不标脏、不写设备。
  const clear = app.match(/function clearEncoderSelfCheck\(\)\s*\{[\s\S]*?\n\}/);
  assert.ok(clear, "找不到 clearEncoderSelfCheck");
  assert.doesNotMatch(clear[0], /markDirty|fetch\(|api\(/, "清零不能发请求或标脏");
  assert.doesNotMatch(clear[0], /CFG:/, "清零不能写设备");

  const html = readFileSync(new URL("./templates/index.html", import.meta.url), "utf8");
  const mod = html.indexOf("/static/encoder_stats.js");
  const appTag = html.indexOf("/static/app.js");
  assert.ok(mod > 0 && mod < appTag, "encoder_stats.js 必须在 app.js 之前加载");
  assert.match(html, /id="encoderSelfCheck"/);
  assert.match(html, /id="btnEncSelfClear"/);
});

test("自检条文案在中英两套 locale 里都存在，占位符一致", () => {
  const dir = new URL("./locales/", import.meta.url);
  const zh = JSON.parse(readFileSync(new URL("zh-CN.json", dir), "utf8"));
  const en = JSON.parse(readFileSync(new URL("en-US.json", dir), "utf8"));
  [
    "ui.encoder_self_session",
    "ui.encoder_self_cw",
    "ui.encoder_self_ccw",
    "ui.encoder_self_recent",
    "ui.encoder_self_idle",
    "ui.encoder_self_paused",
    "ui.encoder_self_clear",
    "ui.encoder_self_clear_title",
  ].forEach((key) => {
    assert.equal(typeof zh[key], "string", `zh-CN 缺少 ${key}`);
    assert.equal(typeof en[key], "string", `en-US 缺少 ${key}`);
    const ph = (s) => (s.match(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g) || []).sort();
    assert.deepEqual(ph(zh[key]), ph(en[key]), `${key} 的中英占位符不一致`);
  });
});
