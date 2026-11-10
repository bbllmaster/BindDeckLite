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

  /**
   * 编码器模式：设备侧 `CFG:ENC` 的值，存在 `cfg.encoder.mode`。
   *
   * 生产配置里没有 `enc` 键（`enc` 只是协议/回读字段名），所以不能直接按
   * 字段名取值。也**不能整对象比较 `cfg.encoder`**：`encoder.actions` 是纯
   * 上位机的旋转动作，从不下发，整对象比会把「只改了本机旋转动作」误判成
   * 待下发。旧存档没有该字段时按 0（仅 HID）处理，与后端默认一致。
   */
  function encoderMode(cfg) {
    const enc = (cfg || {}).encoder;
    const mode = enc && enc.mode;
    return mode === undefined || mode === null ? 0 : mode;
  }

  // 参与对账比较的四组配置，顺序即日志里的字段顺序（与后端一致）。
  // `key` 是后端 CFG: 的组名（也是回读字段名）；`value` 是取该组参与比较
  // 的值的函数，缺省时取同名字段。给了取值器的组按取值结果比较，不做整对象比。
  const FIELDS = [
    { key: "pins" },
    { key: "keys" },
    { key: "enc", value: encoderMode },
    { key: "sleep" },
  ];

  /**
   * 字段 → i18n key（组名）。日志行的标签按这张表取，页面不再另写一份映射：
   * 「测试拼一种、生产拼另一种」正是编码器模式那一组漏掉的原因。
   */
  const LABEL_KEYS = {
    pins: "ui.group_pins",
    keys: "ui.group_keys",
    enc: "ui.group_enc",
    sleep: "ui.group_sleep",
  };

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

  /** 某一组在配置里的可比较值。 */
  function valueOf(field, cfg) {
    return field.value ? field.value(cfg) : (cfg || {})[field.key];
  }

  /** 设备回报的配置相对重连前本机值变了的字段，按 FIELDS 顺序返回。 */
  function changedFields(before, after) {
    const a = before || {};
    const b = after || {};
    return FIELDS
      .filter((f) => !sameJson(valueOf(f, a), valueOf(f, b)))
      .map((f) => f.key);
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

  return {
    STABLE_MS,
    FIELDS,
    LABEL_KEYS,
    encoderMode,
    shouldReconcile,
    changedFields,
    syncSummary,
  };
});
