// WS-561 重连自动对账的判定回归用例。
//
// 运行：cd host && node --test test_reconnect_sync.mjs
//
// 断言对象是 host/static/reconnect_sync.js 里的**生产函数**（浏览器里的
// app.js 调的就是 window.ReconnectSync 这一份），不在这里复制判定逻辑 ——
// 放宽触发条件、改动字段顺序或破坏拼接都会让这些用例真的失败。
import test from "node:test";
import assert from "node:assert/strict";

import Sync from "./static/reconnect_sync.js";

const { STABLE_MS, shouldReconcile, changedFields, syncSummary } = Sync;

/** 全部条件满足的一组输入；各用例只覆盖自己关心的那一项。 */
const ready = {
  trusted: true,
  pendingCount: 0,
  otaBusy: false,
  connectedForMs: STABLE_MS,
  reconciledThisCycle: false,
};

test("所有条件满足 → 允许对账", () => {
  const v = shouldReconcile(ready);
  assert.equal(v.ok, true);
  assert.equal(v.reason, "ok");
});

test("本机有未下发修改 → 不请求，原因 pending", () => {
  const v = shouldReconcile({ ...ready, pendingCount: 2 });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "pending");
});

test("OTA 进行中 → 不请求", () => {
  const v = shouldReconcile({ ...ready, otaBusy: true });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "ota");
});

test("连接不足 2 秒 → 不请求，等稳定窗口", () => {
  const v = shouldReconcile({ ...ready, connectedForMs: STABLE_MS - 1 });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "unstable");
});

test("本连接周期已对账 → 不重复请求（掉线重连由调用方重置该标记）", () => {
  const v = shouldReconcile({ ...ready, reconciledThisCycle: true });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "reconciled");
});

test("本机无存档（trusted=false）→ 不自动读，走 WS-512 的显式确认", () => {
  const v = shouldReconcile({ ...ready, trusted: false });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "untrusted");
});

test("未传 trusted（老存档 undefined）也不当作可信", () => {
  const v = shouldReconcile({ ...ready, trusted: undefined });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "untrusted");
});

test("差异比较只认四组配置，字段顺序固定为 pins/keys/enc/sleep", () => {
  const before = {
    pins: [13, 12, 14],
    keys: [{ hid: 240 }],
    enc: { mode: 0 },
    sleep: { enabled: true, minutes: 5 },
    name: "旧名字",
  };
  const after = {
    pins: [15, 12, 14],          // 引脚改了
    keys: [{ hid: 240 }],        // 没变
    enc: { mode: 2 },            // 编码器模式改了
    sleep: { enabled: true, minutes: 5 },
    name: "新名字",              // 不属于对账字段，不算差异
  };
  assert.deepEqual(changedFields(before, after), ["pins", "enc"]);
});

test("完全一致 → 无差异，日志字段串为空（调用方据此不记日志）", () => {
  const cfg = { pins: [13], keys: [{ hid: 240 }], enc: { mode: 0 }, sleep: { enabled: false } };
  assert.deepEqual(changedFields(cfg, JSON.parse(JSON.stringify(cfg))), []);
  assert.equal(syncSummary(cfg, cfg, (f) => f, "、"), "");
});

test("差异日志字段拼接：引脚 + 编码器模式，顺序与顿号来自生产视图", () => {
  const labels = { pins: "按键引脚", keys: "按键码", enc: "编码器模式", sleep: "休眠" };
  const before = { pins: [13], keys: [{ hid: 240 }], enc: { mode: 0 }, sleep: { enabled: false } };
  const after = { pins: [15], keys: [{ hid: 240 }], enc: { mode: 2 }, sleep: { enabled: false } };
  assert.equal(
    syncSummary(before, after, (f) => labels[f], "、"),
    "按键引脚、编码器模式"
  );
});
