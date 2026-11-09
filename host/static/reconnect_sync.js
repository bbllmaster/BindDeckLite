/**
 * WS-561 重连后自动对账的纯判定逻辑。
 *
 * 只放与 DOM 无关的判定：是否该对账、重连前后哪几组配置真的变了、日志里
 * 该怎么拼字段名。浏览器挂 window.ReconnectSync，Node 测试直接 import，
 * 因此测试断言的就是生产代码本身 —— 改这里的条件会让 test_reconnect_sync.mjs
 * 真的变红。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.ReconnectSync = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // 连接稳定窗口：刚连上就发 CMD:GET_CFG 容易和 VERSION / BOND 抢链路。
  const STABLE_MS = 2000;

  // 参与对账比较的四组配置，顺序即日志里的字段顺序（与后端一致）。
  const FIELDS = ["pins", "keys", "enc", "sleep"];

  /**
   * 本设备、本连接周期是否该静默对账。
   *
   * 返回 `{ok, reason}`：`ok` 为真才发 refresh；`reason` 说明为什么没发，
   * 调用方只用它区分「有未下发修改 → 只提示」这一条。
   */
  function shouldReconcile(input) {
    const o = input || {};
    if (o.trusted !== true) return { ok: false, reason: "untrusted" };
    if (Number(o.pendingCount) > 0) return { ok: false, reason: "pending" };
    if (o.otaBusy) return { ok: false, reason: "ota" };
    if (o.reconciledThisCycle) return { ok: false, reason: "reconciled" };
    if (!(Number(o.connectedForMs) >= STABLE_MS)) return { ok: false, reason: "unstable" };
    return { ok: true, reason: "ok" };
  }

  function sameJson(a, b) {
    return JSON.stringify(a === undefined ? null : a) ===
      JSON.stringify(b === undefined ? null : b);
  }

  /** 设备回报的配置相对重连前本机值变了的字段，按 FIELDS 顺序返回。 */
  function changedFields(before, after) {
    const a = before || {};
    const b = after || {};
    return FIELDS.filter((f) => !sameJson(a[f], b[f]));
  }

  /**
   * 一行日志里「已按设备同步：<字段>」的字段部分。
   *
   * labelOf(field) 取当前语言的组名，separator 取当前语言的顿号/逗号
   * （都来自 i18n，不在这个模块里写死文案）。没有差异返回空串，调用方据此
   * 决定不记日志。
   */
  function syncSummary(before, after, labelOf, separator) {
    const fields = changedFields(before, after);
    if (!fields.length) return "";
    return fields.map((f) => labelOf(f)).join(separator);
  }

  return { STABLE_MS, FIELDS, shouldReconcile, changedFields, syncSummary };
});
