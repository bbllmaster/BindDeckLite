/**
 * 日志页「设备标注 / 设备筛选」的纯逻辑。
 *
 * 只放与 DOM 无关的判定：设备并集、标签取名、下拉选项、单行是否命中设备
 * 过滤、复制文本格式。浏览器挂 window.LogDevice，Node 测试直接 import，
 * 因此测试断言的就是生产代码本身。
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.LogDevice = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  // 文案表在 i18n.js；Node 测试直接 require，浏览器取 window.i18n。
  const i18n =
    typeof module === "object" && module && module.exports
      ? require("./i18n.js")
      : root && root.i18n;
  const t = (code, params) => (i18n && i18n.t ? i18n.t(code, params) : code);

  /** 「全部设备」选项的 value（空串 = 不按设备过滤）。 */
  const ALL_DEVICES = "";

  /**
   * 设备并集 = 已发现设备的 id ∪ 日志缓冲里出现过的 dev。
   *
   * 先按设备列表顺序列出在线设备，再把只存在于历史日志里的 dev 排序追加，
   * 保证下拉每次重算顺序稳定。空 dev 不进入并集（上位机自身事件 / 旧固件）。
   */
  function deviceUnion(deviceIds, logDevs) {
    const seen = new Set();
    const out = [];
    (deviceIds || []).forEach((id) => {
      if (!id || seen.has(id)) return;
      seen.add(id);
      out.push(id);
    });
    const extras = [];
    (logDevs || []).forEach((id) => {
      if (!id || seen.has(id)) return;
      seen.add(id);
      extras.push(id);
    });
    extras.sort();
    return out.concat(extras);
  }

  /**
   * 是否显示设备标签与「设备」下拉。
   *
   * 只有并集 ≥ 2 才显示：单设备时界面与不标注设备时完全一致（零改动回归）。
   */
  function showsDeviceTag(union) {
    return Array.isArray(union) ? union.length >= 2 : false;
  }

  /** 设备显示名：优先设备列表里的 name，找不到（已离线 / 未发现）用原始 dev。 */
  function deviceDisplayName(dev, devices) {
    if (!dev) return "";
    const hit = (devices || []).find((d) => d && d.dev_id === dev);
    return (hit && hit.name) || dev;
  }

  /**
   * 单行的设备标签文案；空串表示这一行不加标签、不占设备列。
   *
   * dev 为空（上位机自身事件 / 无后缀旧固件）一律不加标签。
   */
  function rowDeviceLabel(dev, devices, union) {
    if (!dev) return "";
    if (!showsDeviceTag(union)) return "";
    return deviceDisplayName(dev, devices);
  }

  /**
   * 「设备」下拉的选项：第一项「全部设备」，其后每个 dev 一项。
   *
   * 只在日志缓冲里出现、已从扫描列表消失的设备标「（已离线）」，其历史行
   * 仍然能筛出来。
   */
  function deviceOptions(deviceIds, logDevs, devices) {
    const online = new Set((deviceIds || []).filter(Boolean));
    const options = [{ value: ALL_DEVICES, label: t("log.all_devices"), offline: false }];
    deviceUnion(deviceIds, logDevs).forEach((dev) => {
      const name = deviceDisplayName(dev, devices);
      const offline = !online.has(dev);
      options.push({
        value: dev,
        label: offline ? name + t("log.device_offline_suffix") : name,
        offline,
      });
    });
    return options;
  }

  /**
   * 单行是否命中设备过滤。
   *
   * 未选设备时全通过；dev 为空的行永不被设备过滤命中（这类行没有归属，
   * 不能因为选了某台设备就把它当成那台设备的日志）。
   */
  function rowMatchesDev(rowDev, selectedDev) {
    if (!selectedDev) return true;
    return !!rowDev && rowDev === selectedDev;
  }

  /**
   * 单行复制文本：`HH:MM:SS [kind] [设备名] 正文`，dev 为空时不加设备段。
   */
  function logCopyText(event, devices) {
    const e = event || {};
    const dev = e.dev ? " [" + deviceDisplayName(e.dev, devices) + "]" : "";
    return `${e.time} [${e.kind}]${dev} ${e.text}`;
  }

  return {
    ALL_DEVICES,
    deviceUnion,
    showsDeviceTag,
    deviceDisplayName,
    rowDeviceLabel,
    deviceOptions,
    rowMatchesDev,
    logCopyText,
  };
});
