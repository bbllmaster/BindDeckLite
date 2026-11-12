/**
 * 顶栏「配置状态」三态判定（WS-584）。
 *
 * 重构前，「配置是否与设备同步」在页面上有四个出口：`.last-apply` 胶囊、
 * `#pendingBadge`（现为 `#cfgStatus`）、`.untrusted-banner` 横幅、`#applyResults` 浮层。同一个
 * 事实被四处各自判断，改一处忘一处就会自相矛盾。这里把判定收敛成**唯一的
 * 纯函数**：输入是页面状态里的几个事实，输出只有一个 state。
 *
 * 优先级（先命中先返回）：
 *   1. `none`      —— 没有选中设备（预览模式），顶栏不显示控件；
 *   2. `untrusted` —— 本机没有这台设备的存档，界面里是默认值。它压过 pending：
 *                     此时连「设备现在是什么」都不知道，不能宣称「只差 N 项」；
 *   3. `pending`   —— 有修改没下发（含 WS-561 重连后的提示）；
 *   4. `synced`    —— 本机与设备一致。
 *
 * 浏览器挂 `window.CfgStatus`，Node 测试直接 import 同一个文件 —— 测试断言
 * 的就是生产代码本身，改这里的优先级会让 test_cfg_status.mjs 真的变红。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.CfgStatus = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const STATES = {
    NONE: "none",
    UNTRUSTED: "untrusted",
    PENDING: "pending",
    SYNCED: "synced",
  };

  /**
   * 状态 → 徽标文案的 i18n key。
   *
   * `synced` 与 `syncedAt` 是同一个状态的两副面孔：有上次下发时间就把时间
   * 带上（「已同步 · 12:04」），没有就只说「已同步」。文案本身在 locales 里，
   * 这里只认 key，切语言时页面按 key 重新取词。
   */
  const LABEL_KEYS = {
    synced: "ui.cfg_state_synced",
    syncedAt: "ui.cfg_state_synced_at",
    pending: "ui.cfg_state_pending",
    untrusted: "ui.cfg_state_untrusted",
  };

  /**
   * WS-512 / WS-596：这台设备的「配置未确认」提示是否还要呈现。
   *
   * `cfg` 就是页面上的 `state.cfg`（判定只看 `trusted`），`ignored` 是本次
   * 会话对这台设备的「先忽略」标记（`state.pendingIgnored[devId]`）。
   *
   * 纯函数、只读：它不碰 cfg，也就改不动 `trusted` —— 「先忽略」只关掉提示，
   * 绝不能顺带解除「应用配置」前的覆盖确认（那条判断在 app.js 里直接读
   * `cfg.trusted` 与 `sessionRead`）。
   */
  function shouldShowPending(cfg, ignored) {
    return (cfg || {}).trusted === false && !ignored;
  }

  /**
   * 由页面事实推导当前状态。
   *
   * @param {object} input
   *   selected     {string|null} 当前设备 id
   *   preview      {boolean}     预览模式（没有设备，只有默认值）
   *   trusted      {boolean|undefined} 本机存档是否来自这台设备（WS-512）
   *   ignored      {boolean}     WS-596 本会话对这台设备点了「先忽略」
   *   pendingCount {number}      相对「已下发快照」的改动项数
   *   reconnect    {boolean}     WS-561 重连后「有修改没下发」的提示是否成立
   *   lastApply    {{at:number, failed:number}|null} 上次下发结论
   */
  function derive(input) {
    const o = input || {};
    const pendingCount = Math.max(0, Number(o.pendingCount) || 0);
    const base = {
      pendingCount,
      reconnect: !!o.reconnect,
      lastApply: o.lastApply || null,
    };

    if (!o.selected || o.preview) return Object.assign({ state: STATES.NONE }, base, { pendingCount: 0 });

    // trusted === false 才表示「确认不是这台设备的存档」；undefined 是旧
    // config.json 没有该字段，按可信处理（WS-512 的兼容约定）。
    if (shouldShowPending({ trusted: o.trusted }, o.ignored)) {
      return Object.assign({ state: STATES.UNTRUSTED }, base);
    }
    // 「先忽略」把未确认提示收掉，但结论本身没变：本机依然不知道设备上
    // 是什么，所以不能改口说「已同步」。还有未下发的修改时那是另一件事，
    // 照常按 pending 提示；两者都没有就干脆不占顶栏。
    if (o.trusted === false) {
      return pendingCount > 0
        ? Object.assign({ state: STATES.PENDING }, base)
        : Object.assign({ state: STATES.NONE }, base, { pendingCount: 0 });
    }

    if (pendingCount > 0) {
      return Object.assign({ state: STATES.PENDING }, base, {
        // 重连提示只在「设备重新连上、本机又有未下发修改」时成立；没有未下发
        // 项时它本来就不该存在（updatePendingUi 会顺手清掉）。
        reconnect: !!o.reconnect,
      });
    }

    return Object.assign({ state: STATES.SYNCED }, base, { reconnect: false });
  }

  /**
   * 徽标文案：`{key, params}`。调用方用 t(key, params) 取当前语言。
   *
   * fmtTime 由页面传入（把秒级时间戳格式化成 HH:MM），纯模块不依赖 DOM/时钟，
   * Node 测试传 `(at) => "T" + at` 就能断言参数是否真的带上了。
   */
  function describe(view, fmtTime) {
    const v = view || {};
    const fmt = typeof fmtTime === "function" ? fmtTime : (at) => String(at);
    switch (v.state) {
      case STATES.PENDING:
        return { key: LABEL_KEYS.pending, params: { count: v.pendingCount } };
      case STATES.UNTRUSTED:
        return { key: LABEL_KEYS.untrusted, params: {} };
      case STATES.SYNCED:
        return v.lastApply && v.lastApply.at
          ? { key: LABEL_KEYS.syncedAt, params: { time: fmt(v.lastApply.at) } }
          : { key: LABEL_KEYS.synced, params: {} };
      default:
        return { key: "", params: {} };
    }
  }

  return { STATES, LABEL_KEYS, shouldShowPending, derive, describe };
});
