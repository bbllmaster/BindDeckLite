// WS-470 日志「跟随 / 暂停」纯逻辑的回归用例。
//
// 运行：node --test host/test_log_follow.mjs
// 依赖 host/static/log_follow.js 的**生产**函数（不在这里复制判定逻辑），
// 因此改动生产代码会让这些断言真的失败。
import test from "node:test";
import assert from "node:assert/strict";

import LogFollow from "./static/log_follow.js";

const { shouldFollow, pendingAfterAppend, compensateTrim, pendingLabel } = LogFollow;

// 统一用「内容 1000、视口 400」这一组度量，距底 = 1000 - 400 - scrollTop。
const SCROLL_HEIGHT = 1000;
const CLIENT_HEIGHT = 400;

test("距底 0 / 24 px 视为跟随，25 px 视为用户上滚", () => {
  // 0 px
  assert.equal(shouldFollow(SCROLL_HEIGHT - CLIENT_HEIGHT, SCROLL_HEIGHT, CLIENT_HEIGHT), true);
  // 24 px
  assert.equal(shouldFollow(SCROLL_HEIGHT - CLIENT_HEIGHT - 24, SCROLL_HEIGHT, CLIENT_HEIGHT), true);
  // 25 px
  assert.equal(shouldFollow(SCROLL_HEIGHT - CLIENT_HEIGHT - 25, SCROLL_HEIGHT, CLIENT_HEIGHT), false);
});

test("阈值是 24 的边界，不是 20 或 30", () => {
  assert.equal(LogFollow.DEFAULT_THRESHOLD, 24);
  assert.equal(shouldFollow(600, 1000, 400, 30), true); // 距底 0
  assert.equal(shouldFollow(580, 1000, 400, 30), true); // 距底 20，自定义阈值 30
  assert.equal(shouldFollow(580, 1000, 400), true); // 距底 20 ≤ 24
  assert.equal(shouldFollow(575, 1000, 400), false); // 距底 25 > 24
});

test("度量读不到有效值时不锁死滚动（按跟随处理）", () => {
  assert.equal(shouldFollow(NaN, SCROLL_HEIGHT, CLIENT_HEIGHT), true);
  assert.equal(shouldFollow(600, undefined, CLIENT_HEIGHT), true);
});

test("暂停态新增 20 行，计数按行累加", () => {
  assert.equal(pendingAfterAppend(0, 20, false), 20);
  assert.equal(pendingAfterAppend(20, 3, false), 23);
  // 单轮远大于 20 行也不能少算
  assert.equal(pendingAfterAppend(7, 137, false), 144);
});

test("跟随态计数恒为 0", () => {
  assert.equal(pendingAfterAppend(0, 20, true), 0);
  assert.equal(pendingAfterAppend(42, 5, true), 0);
});

test("裁剪补偿后可视首行偏移不变", () => {
  // 暂停在 scrollTop=500，距底 100 px；可视首行在内容坐标 820，屏幕偏移 320。
  const scrollTop = 500;
  const rowContentTop = 820;
  const beforeOffset = rowContentTop - scrollTop;
  assert.equal(beforeOffset, 320);

  // 裁剪掉顶部 120 px 的行：可视首行内容坐标上移 120，scrollTop 同步减 120。
  const removedHeight = 120;
  const afterScrollTop = compensateTrim(scrollTop, removedHeight);
  const afterOffset = rowContentTop - removedHeight - afterScrollTop;

  assert.equal(afterScrollTop, 380);
  assert.equal(afterOffset, beforeOffset);
});

test("裁剪补偿在多种行高下都保持偏移不变", () => {
  for (const [scrollTop, removedHeight, rowTop] of [
    [300, 22, 330],
    [1000, 44, 1060],
    [640, 18.4, 700],
  ]) {
    const before = rowTop - scrollTop;
    const after = rowTop - removedHeight - compensateTrim(scrollTop, removedHeight);
    assert.equal(after, before);
  }
});

test("补偿不会把 scrollTop 拉到负数", () => {
  assert.equal(compensateTrim(40, 120), 0);
  assert.equal(compensateTrim(0, 0), 0);
  assert.equal(compensateTrim(NaN, 10), 0);
});

test("胶囊文案：N > 99 显示 99+", () => {
  assert.equal(pendingLabel(0), "↓ 0 条新日志");
  assert.equal(pendingLabel(3), "↓ 3 条新日志");
  assert.equal(pendingLabel(99), "↓ 99 条新日志");
  assert.equal(pendingLabel(100), "↓ 99+ 条新日志");
  assert.equal(pendingLabel(1234), "↓ 99+ 条新日志");
});
