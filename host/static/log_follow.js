/**
 * 日志面板「跟随 / 暂停」的纯逻辑（WS-470）。
 *
 * 只放与 DOM 无关的判定与计算，浏览器与 Node 共用：UMD 形态，浏览器挂
 * window.LogFollow，Node 走 module.exports，测试无需 mock DOM。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.LogFollow = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // 距底 ≤ 24 px 视为「还在底部」，> 24 px 视为用户主动上滚。
  const DEFAULT_THRESHOLD = 24;
  // 计数上限，超过显示 99+。
  const MAX_PENDING = 99;

  /** 距内容底部还有多少像素（负数表示越过底部，例如回弹）。 */
  function distanceToBottom(scrollTop, scrollHeight, clientHeight) {
    return scrollHeight - clientHeight - scrollTop;
  }

  /**
   * 是否应该继续跟随最新日志。
   *
   * 度量读不到有效值（pywebview 偶发 NaN）时不锁死滚动，按跟随处理；
   * 调用方在容器隐藏（clientHeight 为 0）时应先自行跳过判定。
   */
  function shouldFollow(scrollTop, scrollHeight, clientHeight, threshold = DEFAULT_THRESHOLD) {
    if (!Number.isFinite(scrollTop) || !Number.isFinite(scrollHeight) || !Number.isFinite(clientHeight)) {
      return true;
    }
    return distanceToBottom(scrollTop, scrollHeight, clientHeight) <= threshold;
  }

  /**
   * 追加 added 行之后的待读计数。
   *
   * 跟随中恒为 0（用户正看着最新行）；暂停时按行累加，不能按轮询批次 +1。
   */
  function pendingAfterAppend(pending, added, following) {
    if (following) return 0;
    const base = Number.isFinite(pending) ? pending : 0;
    const inc = Number.isFinite(added) ? added : 0;
    if (inc <= 0) return Math.max(0, base);
    return Math.max(0, base + inc);
  }

  /**
   * 暂停态裁剪顶部行后补偿滚动位置。
   *
   * 被裁掉的行都在视口上方，内容整体上移了 removedHeight，scrollTop 同步
   * 减去同样的高度，可视首行才停在原处。
   */
  function compensateTrim(scrollTop, removedHeight) {
    const top = Number.isFinite(scrollTop) ? scrollTop : 0;
    const removed = Number.isFinite(removedHeight) ? removedHeight : 0;
    if (removed <= 0) return Math.max(0, top);
    return Math.max(0, top - removed);
  }

  /** 胶囊文案：N > 99 显示 99+。 */
  function pendingLabel(pending) {
    const n = Number.isFinite(pending) && pending > 0 ? Math.floor(pending) : 0;
    return n > MAX_PENDING ? "↓ " + MAX_PENDING + "+ 条新日志" : "↓ " + n + " 条新日志";
  }

  return {
    DEFAULT_THRESHOLD,
    MAX_PENDING,
    distanceToBottom,
    shouldFollow,
    pendingAfterAppend,
    compensateTrim,
    pendingLabel,
  };
});
