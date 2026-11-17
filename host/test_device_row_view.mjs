// WS-637：侧栏设备行「刚按下的输入」副标题判定回归用例。
//
// 运行：cd host && node --test test_device_row_view.mjs
//
// 断言对象是 host/static/device_row.js 里的**生产函数**（浏览器里 app.js
// 调的就是 window.DeviceRow 这一份），不在这里复制一份判定逻辑。
// 改动命中窗口、把编号口径写成「第 N 键」、或者让断开/旧后端分支也命中，
// 都会让这里真的变红。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import DeviceRow from "./static/device_row.js";

const { HIT_WINDOW_S, deviceRowView } = DeviceRow;

/** 一台已连接、地址固定的设备；各用例只覆盖自己关心的字段。 */
const base = {
  connected: true,
  devId: "aa:bb",
  address: "AA:BB:CC:DD:EE:FF",
  reason: "",
  lastPress: {},
};

const view = (over) => deviceRowView(Object.assign({}, base, over));

test("未连接 → hit=false，文本是失败原因（没有原因则回地址）", () => {
  const withReason = view({
    connected: false,
    reason: "连接超时",
    lastPress: { "aa:bb": { index: 2, age: 0.2 } },
  });
  assert.deepEqual(withReason, { text: "连接超时", hit: false });

  const noReason = view({ connected: false, reason: "", lastPress: {} });
  assert.deepEqual(noReason, { text: base.address, hit: false });
});

test("age=0.5 + index=2 → 「按键 3」且 hit", () => {
  const v = view({ lastPress: { "aa:bb": { index: 2, age: 0.5 } } });
  assert.equal(v.hit, true);
  assert.equal(v.text, "按键 3");
});

test("age 超过 2.5 s → 还原成地址", () => {
  const v = view({ lastPress: { "aa:bb": { index: 2, age: 3 } } });
  assert.deepEqual(v, { text: base.address, hit: false });

  // 边界：恰好 2.5 s 仍算命中（服务端 age 是浮点，别把边界判反）。
  assert.equal(view({ lastPress: { "aa:bb": { index: 0, age: HIT_WINDOW_S } } }).hit, true);
});

test("index=10（编码器按压 / 12 键扩展键）→ 「输入 11」", () => {
  const v = view({ lastPress: { "aa:bb": { index: 10, age: 1 } } });
  assert.equal(v.hit, true);
  assert.equal(v.text, "输入 11");
});

test("旧后端没有 last_press / 这台设备没有记录 → 地址，不报错", () => {
  assert.deepEqual(view({ lastPress: undefined }), { text: base.address, hit: false });
  assert.deepEqual(view({ lastPress: null }), { text: base.address, hit: false });
  assert.deepEqual(view({ lastPress: {} }), { text: base.address, hit: false });
  // 有别的设备的记录不算命中。
  assert.deepEqual(view({ lastPress: { "cc:dd": { index: 4, age: 0.3 } } }), {
    text: base.address,
    hit: false,
  });
});

test("两台设备交叉断言：只有刚按下的那台 hit，另一台保持地址", () => {
  const lastPress = {
    "aa:bb": { index: 2, age: 0.4 },
    "cc:dd": { index: 6, age: 3.2 },
  };
  const a = view({ devId: "aa:bb", lastPress });
  const b = view({ devId: "cc:dd", address: "11:22:33:44:55:66", lastPress });
  assert.deepEqual(a, { text: "按键 3", hit: true });
  assert.deepEqual(b, { text: "11:22:33:44:55:66", hit: false });

  // 再按 B：下一次轮询后换成 B 命中，A 过期还原 —— 互不影响。
  const swapped = {
    "aa:bb": { index: 2, age: 3.0 },
    "cc:dd": { index: 0, age: 0.1 },
  };
  assert.deepEqual(view({ devId: "aa:bb", lastPress: swapped }), {
    text: base.address,
    hit: false,
  });
  assert.deepEqual(view({ devId: "cc:dd", address: "11:22:33:44:55:66", lastPress: swapped }), {
    text: "按键 1",
    hit: true,
  });
});

test("生产页面真的用上了这个判定（模块没接进渲染层时也要变红）", () => {
  const app = readFileSync(new URL("./static/app.js", import.meta.url), "utf8");
  assert.match(app, /DeviceRow\.deviceRowView\(/, "renderDevices 没有调用生产纯函数");
  const html = readFileSync(new URL("./templates/index.html", import.meta.url), "utf8");
  const row = html.indexOf('/static/device_row.js');
  const appTag = html.indexOf('/static/app.js');
  assert.ok(row > 0 && row < appTag, "device_row.js 必须在 app.js 之前加载");
});

test("两个文案 key 在中英两套 locale 里都存在，占位符一致", () => {
  const dir = new URL("./locales/", import.meta.url);
  const zh = JSON.parse(readFileSync(new URL("zh-CN.json", dir), "utf8"));
  const en = JSON.parse(readFileSync(new URL("en-US.json", dir), "utf8"));
  ["ui.device_last_key", "ui.device_last_input"].forEach((key) => {
    assert.equal(typeof zh[key], "string", `zh-CN 缺少 ${key}`);
    assert.equal(typeof en[key], "string", `en-US 缺少 ${key}`);
    const ph = (s) => (s.match(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g) || []).sort();
    assert.deepEqual(ph(zh[key]), ph(en[key]), `${key} 的中英占位符不一致`);
  });
});
