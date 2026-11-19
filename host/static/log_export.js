/**
 * 日志页「导出…」的纯逻辑（WS-645 / WS-646）。
 *
 * 只放与 DOM 无关的判定：头部 5 行拼装、可见行文本、按钮可用态、文件名净化。
 * 浏览器挂 window.LogExport，Node 测试直接 import —— 因此测试断言的就是
 * 生产代码本身，改坏生产实现会真的变红。
 *
 * 行文本一律走 LogDevice.logCopyText()（与单行「复制」同一份格式化），
 * 这里不另写一套行格式，也不碰正文脱敏：脱敏在事件入缓冲前就已完成。
 */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.LogExport = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  // 文案 / 行格式化都复用现有实现；Node 测试里 require 到的是同一份文件。
  const i18n =
    typeof module === "object" && module && module.exports
      ? require("./i18n.js")
      : root && root.i18n;
  const LogDevice =
    typeof module === "object" && module && module.exports
      ? require("./log_device.js")
      : root && root.LogDevice;
  const t = (code, params) => (i18n && i18n.t ? i18n.t(code, params) : code);

  /** 与 host/app.py 的 deque(maxlen=400) 一致：头部要如实写明上限。 */
  const BUFFER_MAX = 400;

  /**
   * 日志 DOM 的裁剪上限（`pollLog()` 里的 `while (list.children.length > N)`）。
   *
   * 比内存缓冲的 400 小：屏幕上永远看不到更早的 100 行，导出也只可能拿到
   * 列表里的行。头部必须把这两个数字都写出来，否则「命中最多 300 行」
   * 与「缓冲上限 400 行」对不上，用户会以为导出少给了 100 行。
   */
  const VIEW_MAX = 300;

  /** 文件名里设备名片段的最大长度；标题行仍保留完整设备名。 */
  const NAME_PART_MAX = 40;

  const pad2 = (n) => String(n).padStart(2, "0");

  /** 本地时间 `YYYY-MM-DD HH:MM:SS`（导出头部的「导出时间」）。 */
  function formatStamp(date) {
    const d = date || new Date();
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
      `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  }

  /** 文件名里的时间戳 `YYYYMMDD-HHMM`（与动作备份的默认名同形）。 */
  function formatFileStamp(date) {
    const d = date || new Date();
    return `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}-` +
      `${pad2(d.getHours())}${pad2(d.getMinutes())}`;
  }

  /**
   * 文件名净化：非 `[A-Za-z0-9._-]` 一律替换成 `_`。
   *
   * 设备名可能带空格、中文、冒号（MAC 地址）、路径分隔符 —— 这些都进不了
   * 默认文件名。连续下划线合并成一条，首尾的下划线/点去掉，避免 `__A` 这种
   * 反而更难读的名字；全空时由调用方决定回退值。
   *
   * 与动作备份（`app.py` 的 `api_actions_export()` 用 `c.isalnum()` 保留中文）
   * **故意不同**：这份文件的目标是「拖进聊天窗口 / 记事本再当附件交出去」，文件名
   * 会经过 Windows→Linux→issue 多跳，只留 ASCII 才不会在某一跳变成乱码或被
   * 邮件客户端拒绝；中文设备名在文件头与行内仍是原样，信息不丢。
   */
  function sanitizeNamePart(value) {
    const raw = String(value == null ? "" : value)
      .replace(/[^A-Za-z0-9._-]/g, "_")
      .replace(/_+/g, "_")
      .replace(/^[._]+|[._]+$/g, "");
    return raw.slice(0, NAME_PART_MAX);
  }

  /**
   * 保存对话框的默认文件名：`BindDeckLite-log-<设备或 all>-YYYYMMDD-HHMM.txt`。
   *
   * 设备名净化后为空（纯中文/emoji 设备名）时用 `all`，不产生
   * `BindDeckLite-log---20261120-0012.txt` 这种没有信息量的名字。
   */
  function suggestFileName(devLabel, date) {
    const part = sanitizeNamePart(devLabel) || "all";
    return `BindDeckLite-log-${part}-${formatFileStamp(date)}.txt`;
  }

  /**
   * 按钮状态：导出中恒禁用；否则 0 行禁用并说明原因，有命中即可点。
   *
   * 导出不看暂停 / 跟随状态，只看当前筛选命中多少行。`busy` 必须由调用方
   * 的模块级状态传入，**不能**读 `btn.disabled` 反推：轮询会在导出期间反复
   * 调这里，读 DOM 会把「导出中」重新算成「可点」，于是能弹出第二个保存框。
   */
  function exportButtonState(count, busy) {
    if (busy) return { disabled: true, title: t("log.export_busy") };
    const n = Number(count) || 0;
    return n > 0
      ? { disabled: false, title: t("log.export_btn_title") }
      : { disabled: true, title: t("log.export_none_title") };
  }

  /** 级别的显示文案；空值 = 不按级别过滤，写「全部」。 */
  function levelLabel(level) {
    return level ? t("ui.level_" + level) : t("log.export_all");
  }

  /** 类型的显示文案；空值写「全部」，其余沿用日志页下拉的同一套标签。 */
  function kindLabel(kind) {
    return kind ? t("ui.kind_" + kind) : t("log.export_all");
  }

  /** 搜索词原样带引号；没有搜索词写「(无)」。 */
  function searchLabel(q) {
    return q ? t("log.export_quoted", { q }) : t("log.export_none");
  }

  /** 筛选摘要（头部第 4 行的值部分）。 */
  function filterSummary(filter, devLabel) {
    const f = filter || {};
    return t("log.export_filter", {
      level: levelLabel(f.level),
      kind: kindLabel(f.kind),
      dev: f.dev ? (devLabel || f.dev) : t("log.export_all"),
      q: searchLabel(f.q),
    });
  }

  /**
   * 头部 5 行（整段文案是一条 i18n，切语言后随表更新）。
   *
   * `mode` 由调用方判定：桌面版（exe / pywebview 窗口）或浏览器模式；
   * `version` 缺失时写 `—`，不编造版本号。
   */
  function buildHeader(opts) {
    const o = opts || {};
    const text = t("log.export_header", {
      time: formatStamp(o.now),
      version: o.version || "—",
      mode: o.mode === "browser" ? t("log.export_mode_browser") : t("log.export_mode_desktop"),
      level: levelLabel((o.filter || {}).level),
      kind: kindLabel((o.filter || {}).kind),
      dev: (o.filter || {}).dev ? (o.devLabel || o.filter.dev) : t("log.export_all"),
      q: searchLabel((o.filter || {}).q),
      count: Number(o.count) || 0,
      max: BUFFER_MAX,
      view: VIEW_MAX,
    });
    return String(text).split("\n");
  }

  /**
   * 可见行文本，按 DOM 顺序。
   *
   * `hidden === true` 的行（被当前筛选隐藏）不导出；`row._text` 是渲染后的
   * 正文（切语言后仍跟随），交给 logCopyText 拼成与屏幕/单行复制一致的
   * `HH:MM:SS [kind] [设备] 正文`。
   */
  function visibleRowTexts(rows, devices) {
    return (rows || [])
      .filter((row) => row && !row.hidden)
      .map((row) => LogDevice.logCopyText(row._event, devices, row._text));
  }

  /**
   * 完整导出内容：头部 5 行 + 空行 + 正文，正文之间不插空行、不合并。
   *
   * 返回值带 `count`：toast / 日志行都按真实行数报，不按调用方猜的数字。
   */
  function buildExportText(opts) {
    const o = opts || {};
    const texts = visibleRowTexts(o.rows, o.devices);
    const header = buildHeader({
      now: o.now,
      version: o.version,
      mode: o.mode,
      filter: o.filter,
      devLabel: o.devLabel,
      count: texts.length,
    });
    const body = texts.join("\n");
    return {
      count: texts.length,
      // 0 行时只有头部，调用方（按钮禁用 + 后端 count 校验）不会走到这里。
      text: body ? header.join("\n") + "\n\n" + body + "\n" : header.join("\n") + "\n",
      header,
      rows: texts,
    };
  }

  return {
    BUFFER_MAX,
    VIEW_MAX,
    NAME_PART_MAX,
    formatStamp,
    formatFileStamp,
    sanitizeNamePart,
    suggestFileName,
    exportButtonState,
    filterSummary,
    buildHeader,
    visibleRowTexts,
    buildExportText,
  };
});
