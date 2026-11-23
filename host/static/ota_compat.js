/**
 * OTA 降级前的媒体键兼容性判定（WS-684 / WS-661）。
 *
 * 背景：媒体键码是 `>= media_base`（默认 256，256 + HID consumer usage），
 * 从固件 **1.3.13** 起才支持。刷回 v1.3.12 及更早时，设备仍会从 NVS 读到
 * `461` 这类键码，经 `press((uint8_t))` 截成 `0xCD` 当键盘 usage 发出 ——
 * 焦点窗口可能因此打出字符，而且事后极难归因。上位机在**选完文件、进入
 * 确认页之前**先算一次：目标版本不支持媒体键、且本机存档里确实有媒体键时，
 * 点名受影响的输入并要求显式勾选。
 *
 * 判定收敛成这一个纯函数（不读 DOM、不发请求、不改状态）：
 *   输入 version / cfg / mediaBase，输出 `{level, reason, affected}`。
 *   浏览器挂 `window.OtaCompat`，Node 测试直接 import 同一份文件，测的就是
 *   生产代码本身。
 *
 * 边界：
 *   - 版本只看前三段数字（`1.3.12-rc1` → 1.3.12 → 拦；`1.3.13-dev` → 不拦）；
 *     不足三段（`1.3` / `dev` / 空）→ unknown，只提示不拦；
 *   - `code >= mediaBase` 才算媒体键；0（不发键）与 0–255 不参与；
 *   - 只扫前 `inputCount()` 个输入：编码器布局 10、12 键布局 12，
 *     编码器布局下隐藏的第 11/12 键因此自然不参与；
 *   - 没有 `keys` 存档 → unknown（由调用方决定是否提示「先读取设备配置」）。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.OtaCompat = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  /** 媒体键从固件 1.3.13 起支持（WS-661：CFG:MEDIA 探针 + 256 起的键码段）。 */
  const MEDIA_MIN_FW = [1, 3, 13];

  // 与 app.js 的布局常量同源：0 = 12 键布局，1 = 编码器布局。
  const LAYOUT_12KEY = 0;
  const INPUT_COUNT_12KEY = 12;
  const INPUT_COUNT_ENCODER = 10;
  const DEFAULT_MEDIA_BASE = 256;

  /**
   * 版本号 → 前三段数字。
   *
   * 用「扫描所有数字段」而不是严格 semver 正则：镜像描述符里可能是
   * `1.3.12`，文件名里可能是 `v1.3.12-rc1`，两者都要能落到同一组数字。
   * 不足三段返回 null（unknown）。
   */
  function parseVersionTriplet(version) {
    const nums = String(version == null ? "" : version).match(/\d+/g);
    if (!nums || nums.length < 3) return null;
    return nums.slice(0, 3).map((n) => parseInt(n, 10));
  }

  /** a 是否低于 b（按段比较，先 major 再 minor 再 patch）。 */
  function versionBelow(a, b) {
    for (let i = 0; i < 3; i++) {
      if (a[i] !== b[i]) return a[i] < b[i];
    }
    return false;
  }

  /** 媒体键段起点：缺失 / 非法一律退回 256，不写死到业务判定里。 */
  function mediaBaseOf(mediaBase) {
    const base = Number(mediaBase);
    return Number.isFinite(base) && base > 0 ? base : DEFAULT_MEDIA_BASE;
  }

  /** 当前布局下参与判定的输入数（编码器 10 / 12 键 12）。 */
  function inputCountOf(cfg) {
    return cfg && Number(cfg.layout) === LAYOUT_12KEY
      ? INPUT_COUNT_12KEY : INPUT_COUNT_ENCODER;
  }

  const UNKNOWN = (reason) => ({ level: "unknown", reason, affected: [] });
  const OK = () => ({ level: "ok", reason: "", affected: [] });

  /**
   * @param {string} version   目标固件版本（镜像描述符或文件名）
   * @param {object|null} cfg  本机存档（读 `layout` 与 `keys`）
   * @param {number} mediaBase 媒体键段起点（/api/keycodes 的 media_base）
   * @param {{title?:Function,label?:Function}} [resolvers]
   *        展示用回调：`title(idx)` → 「按键 4」；`label(item, code)` → 「播放/暂停」。
   *        生产调用传的是 app.js / keys.js 里的既有函数，本模块不另建标签表。
   * @returns {{level:"ok"|"block"|"unknown", reason:string,
   *            affected:Array<{idx:number,title:string,keyLabel:string}>}}
   */
  function otaMediaDowngradeRisk(version, cfg, mediaBase, resolvers) {
    const v = parseVersionTriplet(version);
    // 版本读不到：不拦，只提示（设备本身也不认这个版本段，没有比较依据）。
    if (!v) return UNKNOWN("version");
    // 目标固件支持媒体键：不看存档，直接放行。
    if (!versionBelow(v, MEDIA_MIN_FW)) return OK();

    const keys = cfg && Array.isArray(cfg.keys) ? cfg.keys : null;
    if (!keys) return UNKNOWN("cfg");

    const base = mediaBaseOf(mediaBase);
    const total = inputCountOf(cfg);
    const resolveTitle = resolvers && typeof resolvers.title === "function"
      ? resolvers.title : (i) => String(i);
    const resolveLabel = resolvers && typeof resolvers.label === "function"
      ? resolvers.label : (item, code) => String(code);

    const affected = [];
    for (let i = 0; i < total; i++) {
      const item = keys[i];
      const code = parseInt(item && item.code, 10);
      if (!Number.isFinite(code) || code < base) continue;
      affected.push({
        idx: i,
        title: resolveTitle(i),
        keyLabel: resolveLabel(item, code),
      });
    }
    // 存档里没有媒体键：降级不会丢任何东西，与今天一致（整块隐藏）。
    if (!affected.length) return OK();
    return { level: "block", reason: "", affected };
  }

  return {
    MEDIA_MIN_FW,
    LAYOUT_12KEY,
    INPUT_COUNT_12KEY,
    INPUT_COUNT_ENCODER,
    DEFAULT_MEDIA_BASE,
    parseVersionTriplet,
    mediaBaseOf,
    inputCountOf,
    otaMediaDowngradeRisk,
  };
});
