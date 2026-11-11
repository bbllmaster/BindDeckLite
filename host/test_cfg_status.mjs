// WS-584：顶栏「配置状态」三态徽标的判定回归用例。
//
// 运行：cd host && node --test test_cfg_status.mjs
//
// 断言对象是 host/static/cfg_status.js 里的**生产函数**（浏览器里的
// app.js 调的就是 window.CfgStatus 这一份），不在这里复制一份判定逻辑。
// 改动三态的优先级、遗漏 `trusted === false`、或者让「已同步」在没有下发
// 记录时也带时间，都会让这里真的变红。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import CfgStatus from "./static/cfg_status.js";

const { STATES, LABEL_KEYS, derive, describe } = CfgStatus;

/** 一台已选中、已连接的设备；各用例只覆盖自己关心的字段。 */
const ready = {
  selected: "t1",
  preview: false,
  trusted: true,
  pendingCount: 0,
  reconnect: false,
  lastApply: null,
};

const fmt = (at) => `T${at}`;
const label = (view) => describe(view, fmt);

test("已同步且没有下发记录 → synced，文案只说「已同步」", () => {
  const v = derive(ready);
  assert.equal(v.state, STATES.SYNCED);
  assert.deepEqual(label(v), { key: "ui.cfg_state_synced", params: {} });
});

test("已同步且有上次下发时间 → 文案带上时间（已同步 · 12:04）", () => {
  const v = derive(Object.assign({}, ready, { lastApply: { at: 1700000000, failed: 0 } }));
  assert.equal(v.state, STATES.SYNCED);
  const d = label(v);
  assert.equal(d.key, "ui.cfg_state_synced_at");
  assert.equal(d.params.time, "T1700000000");
});

test("改了两项没下发 → pending，并带上项数", () => {
  const v = derive(Object.assign({}, ready, { pendingCount: 2 }));
  assert.equal(v.state, STATES.PENDING);
  assert.equal(v.pendingCount, 2);
  assert.deepEqual(label(v), { key: "ui.cfg_state_pending", params: { count: 2 } });
});

test("synced → pending → synced：下发成功后回到已同步", () => {
  const edited = derive(Object.assign({}, ready, { pendingCount: 3 }));
  assert.equal(edited.state, STATES.PENDING);

  // 应用配置全部 ok：applied 快照更新，待下发项归零（lastApply 同时更新）。
  const applied = derive(Object.assign({}, ready, {
    pendingCount: 0,
    lastApply: { at: 1700000100, failed: 0 },
  }));
  assert.equal(applied.state, STATES.SYNCED);
  assert.equal(label(applied).key, "ui.cfg_state_synced_at");
});

test("untrusted → 从设备读取成功（trusted 翻 true）→ synced", () => {
  const before = derive(Object.assign({}, ready, { trusted: false }));
  assert.equal(before.state, STATES.UNTRUSTED);
  assert.equal(label(before).key, "ui.cfg_state_untrusted");

  // readDeviceConfig 成功：服务端把 trusted 置 true、applied 覆盖为设备值。
  const after = derive(Object.assign({}, ready, { trusted: true, pendingCount: 0 }));
  assert.equal(after.state, STATES.SYNCED);
});

test("untrusted 压过 pending：来源不明时不能宣称「只差 N 项」", () => {
  const v = derive(Object.assign({}, ready, { trusted: false, pendingCount: 4 }));
  assert.equal(v.state, STATES.UNTRUSTED);
  // 数量仍带着，popover 里照样能列出待下发的字段。
  assert.equal(v.pendingCount, 4);
});

test("重连后 pending 保留：state 仍是 pending，reconnect 标记为真", () => {
  const v = derive(Object.assign({}, ready, { pendingCount: 1, reconnect: true }));
  assert.equal(v.state, STATES.PENDING, "重连语义不该把「有未下发修改」吞掉");
  assert.equal(v.reconnect, true);
  // 撤回修改（待下发归零）后重连提示也随之不成立。
  const reverted = derive(Object.assign({}, ready, { pendingCount: 0, reconnect: true }));
  assert.equal(reverted.state, STATES.SYNCED);
  assert.equal(reverted.reconnect, false);
});

test("没有选中设备 / 预览模式 → none，不渲染控件", () => {
  assert.equal(derive(Object.assign({}, ready, { selected: null })).state, STATES.NONE);
  assert.equal(derive(Object.assign({}, ready, { preview: true })).state, STATES.NONE);
  assert.deepEqual(label(derive(Object.assign({}, ready, { selected: null }))), {
    key: "",
    params: {},
  });
});

test("旧 config.json 没有 trusted 字段 → 按可信处理（不打扰存量用户）", () => {
  const v = derive(Object.assign({}, ready, { trusted: undefined }));
  assert.equal(v.state, STATES.SYNCED);
});

test("负的待下发项数按 0 处理，不会出现「-1 项未下发」", () => {
  assert.equal(derive(Object.assign({}, ready, { pendingCount: -2 })).state, STATES.SYNCED);
});

test("三态文案在中英两套 locale 里都存在，占位符一致", () => {
  const dir = new URL("./locales/", import.meta.url);
  const zh = JSON.parse(readFileSync(new URL("zh-CN.json", dir), "utf8"));
  const en = JSON.parse(readFileSync(new URL("en-US.json", dir), "utf8"));
  const keys = Object.values(LABEL_KEYS);
  assert.equal(keys.length, 4);
  keys.forEach((key) => {
    assert.equal(typeof zh[key], "string", `zh-CN 缺少 ${key}`);
    assert.equal(typeof en[key], "string", `en-US 缺少 ${key}`);
    const ph = (s) => (s.match(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g) || []).sort();
    assert.deepEqual(ph(zh[key]), ph(en[key]), `${key} 的中英占位符不一致`);
  });
});
