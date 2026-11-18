/**
 * 编码器旋转自检的计数口径（WS-641）。
 *
 * 编码器页需要在「改完方向、点了应用」之后当场看到旋转是否生效：`ENC:` 行
 * 已经在 `/api/status` 旁边累加成 `encoder_stats`（服务端按 dev_id 记
 * `{cw, ccw, last_dir, age}`），这里只负责把「原始累计值 → 这一刻该显示什么」
 * 收敛成一个纯函数，UI 与测试共用同一份实现：
 *
 *   - `cw` / `ccw` = 原始累计值减去「清零」时的基线（减去而不是清零请求：
 *     清零不发 HTTP、不写设备，只在前端记一条基线）；
 *   - `recent` = 最近一次方向 + 年龄，超过 5 s 回落，清零之前的旧事件不算；
 *   - `frozen` = 设备断开，计数停在最后的值，文案改成「计数暂停」。
 *
 * 浏览器挂 `window.EncoderStats`，Node 测试直接 import 同一个文件 —— 测试
 * 断言的就是生产代码本身。
 *
 * 清零的基线是 `{cw, ccw, at}`，不是单个时间戳：`/api/status` 只给累计值 +
 * 相对年龄，没有「清零之后来了几条」的服务端游标；只记时间无法把清零前的
 * 累计值减掉。
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.EncoderStats = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  /** 「最近」的保留窗口（秒）。与父任务一致：超过 5 s 回落为提示语。 */
  const RECENT_WINDOW_S = 5;

  const DIRS = ["CW", "CCW"];

  /** 非负整数计数：脏数据（缺字段 / 字符串 / 负数）按 0 处理，不抛错。 */
  function count(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return 0;
    return Math.max(0, Math.floor(n));
  }

  function clock(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : Date.now() / 1000;
  }

  /**
   * 记一条清零基线。
   *
   * @param {object|null} stats   这台设备的原始 `encoder_stats[devId]`
   * @param {number} [now]        客户端秒级时间（测试可注入）
   * @returns {{cw: number, ccw: number, at: number}}
   */
  function makeBaseline(stats, now) {
    const raw = stats && typeof stats === "object" ? stats : {};
    return { cw: count(raw.cw), ccw: count(raw.ccw), at: clock(now) };
  }

  /**
   * 自检条这一刻的视图。
   *
   * @param {object} input
   *   stats     {object|null} `/api/status` 的 `encoder_stats` 整张表；
   *             `null` / 缺失 = 旧后端，`available:false`，UI 保持隐藏
   *   devId     {string}      当前选中设备
   *   baseline  {object}      「清零」基线，见 makeBaseline
   *   connected {boolean}     设备当前是否连着
   *   now       {number}      客户端秒级时间（仅用于把 `age` 还原成事件时刻）
   * @returns {{available: boolean, cw: number, ccw: number,
   *            recent: {dir: string, age: number}|null, frozen: boolean}}
   */
  function view(input) {
    const o = input || {};
    const table = o.stats;
    if (!table || typeof table !== "object") {
      return { available: false, cw: 0, ccw: 0, recent: null, frozen: false };
    }

    const raw = (o.devId && table[o.devId]) || null;
    const base = o.baseline && typeof o.baseline === "object" ? o.baseline : null;
    const connected = o.connected !== false;

    // 清零之后只算新增的那几条：累计值减基线，负数不可能出现但要夹住，
    // 免得服务端重启把计数归零后显示成 -3。
    const cw = Math.max(0, count(raw && raw.cw) - (base ? count(base.cw) : 0));
    const ccw = Math.max(0, count(raw && raw.ccw) - (base ? count(base.ccw) : 0));

    // `age` 是服务端算好的秒数；换回客户端时刻只是为了跟清零基线比较，
    // 不做任何跨时钟相减。
    const dir = raw ? String(raw.last_dir || "") : "";
    const ageRaw = raw ? Number(raw.age) : NaN;
    const hasAge = Number.isFinite(ageRaw) && ageRaw >= 0;
    const eventAt = hasAge ? clock(o.now) - ageRaw : NaN;
    const clearedAt = base ? Number(base.at) : NaN;
    const afterClear = !Number.isFinite(clearedAt) || !hasAge || eventAt > clearedAt;

    let recent = null;
    if (connected && DIRS.includes(dir) && hasAge && ageRaw <= RECENT_WINDOW_S && afterClear) {
      recent = { dir, age: Math.max(0, ageRaw) };
    }

    return { available: true, cw, ccw, recent, frozen: !connected };
  }

  return { RECENT_WINDOW_S, makeBaseline, view };
});
