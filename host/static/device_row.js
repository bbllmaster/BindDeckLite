/**
 * 侧栏设备行副标题的判定（WS-637）。
 *
 * 侧栏每行只有「状态圆点 + 设备名 + 副标题」。副标题原本永远是 BLE 地址
 * （断开时是失败原因），两台同时连接时就分不清哪行对应桌上哪块板。这里把
 * 「这一行现在该显示什么」收敛成一个纯函数：
 *
 *   - 未连接 → 失败原因 / 地址（与改动前完全一致）；
 *   - 已连接且 `age <= 2.5` → 最近一次输入编号（「按键 N」/「输入 N」）；
 *   - 其余 → 地址。
 *
 * 判定只看 `/api/status` 给的 `age`，不挂定时器：过期后下一次 1.2 s 轮询
 * 自然还原成地址，轮询频率保持不动。编码器旋转没有 BTN 事件，不会产生
 * `last_press`，因此天然不触发。
 *
 * 浏览器挂 `window.DeviceRow`，Node 测试直接 import 同一个文件 —— 测试断言
 * 的就是生产代码本身。
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.DeviceRow = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  // 文案表在 i18n.js；Node 测试直接 require，浏览器取 window.i18n。
  const i18n =
    typeof module === "object" && module && module.exports
      ? require("./i18n.js")
      : root && root.i18n;
  const t = (code, params) => (i18n && i18n.t ? i18n.t(code, params) : code);

  /** 命中窗口（秒）。与轮询周期 1.2 s 对齐：至少能看见一次，最多留 2.5 s。 */
  const HIT_WINDOW_S = 2.5;

  /** 编号 0–8 是 9 个按键；>=9 是 12 键布局的扩展键或编码器按压。 */
  const BUTTON_COUNT = 9;

  /**
   * 取这台设备的「最近一次输入」剩余年龄。
   *
   * `/api/status` 直接给 `{index, age}`；只在只有时间戳 `at`（秒）的旧快照
   * 里用 `now` 现算。两者都没有 → NaN，按「没有记录」处理。
   */
  function pressAge(press, now) {
    if (!press) return NaN;
    const age = Number(press.age);
    if (Number.isFinite(age)) return age;
    const at = Number(press.at);
    if (!Number.isFinite(at)) return NaN;
    const clock = Number.isFinite(Number(now)) ? Number(now) : Date.now() / 1000;
    return Math.max(0, clock - at);
  }

  /**
   * 这一行现在该显示什么。
   *
   * @param {object} input
   *   connected {boolean}  设备当前是否连着
   *   devId     {string}   设备 id（`last_press` 的键）
   *   address   {string}   已连接时显示的 BLE 地址
   *   reason    {string}   断开时的失败原因摘要（优先于地址）
   *   lastPress {object}   `/api/status` 的 `last_press`（devId → {index, age}）
   *   now       {number}   当前秒级时间（仅在记录只有 `at` 时用到）
   * @returns {{text: string, hit: boolean}} hit 为真表示「刚按下」态
   */
  function deviceRowView(input) {
    const o = input || {};
    const fallback = o.reason || o.address || "";

    // 断开不残留提示：`last_press` 可能还留着这台设备的最后一条，但行已经
    // 显示失败原因了，不能再盖成编号。
    if (!o.connected) return { text: fallback, hit: false };

    const press = o.lastPress && o.devId ? o.lastPress[o.devId] : null;
    const age = pressAge(press, o.now);
    const index = press ? Number(press.index) : NaN;
    if (!Number.isFinite(age) || !Number.isInteger(index) || index < 0) {
      return { text: fallback, hit: false };
    }
    if (age > HIT_WINDOW_S) return { text: fallback, hit: false };

    // 不写「第 10 键」：12 键布局下第 10 个是扩展键，编码器布局下第 10 个
    // 是按压 —— 「输入 N」两种布局都成立。
    const key = index < BUTTON_COUNT ? "ui.device_last_key" : "ui.device_last_input";
    return { text: t(key, { n: index + 1 }), hit: true };
  }

  return { HIT_WINDOW_S, deviceRowView };
});
