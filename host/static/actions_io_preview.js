/**
 * 动作备份导入预览（WS-619）。
 *
 * 预览是纯函数：把一份导出 JSON 摊成「会覆盖哪些输入、按类型各几条、
 * 每条会执行什么」的结构。text:/paste: 宏步骤只给长度、绝不带正文；
 * command:/app:/url: 的值必须完整出现（这是导入风险提示的核心）。
 * 取消 = 零请求且不写日志，确认 = 一个 POST，都以 "decision" 表达，
 * 由 app.js 的 runDecision 真正执行 —— Node 测试断言的就是这份生产逻辑。
 *
 * 浏览器挂 window.ActionsIOPreview，Node 测试直接 import 同一个文件。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  if (root) root.ActionsIOPreview = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const KNOWN_ACTION_TYPES = ["none", "launch", "url", "command", "macro", "media"];
  const KNOWN_MACRO_STEP_TYPES = ["key", "text", "paste", "delay", "wait", "url", "browser", "app"];
  // 只有这两种步骤的正文可能是密码 / 隐私，预览与日志都只显示长度。
  const REDACT_STEP_TYPES = new Set(["text", "paste"]);

  /** 单条宏步骤的展示文本：text:/paste: 不带正文，其余带完整值。 */
  function macroStepDisplay(step) {
    const s = step && typeof step === "object" ? step : {};
    const type = String(s.type === undefined ? "" : s.type);
    const value = String(s.value === undefined ? "" : s.value);
    if (REDACT_STEP_TYPES.has(type)) {
      return type + "(" + value.length + " chars)";
    }
    if (type === "") return value;
    return type + ":" + value;
  }

  /** 一个动作的展示值：宏用逐步摘要，其余（命令 / 路径 / 网址 / 媒体）全文。 */
  function actionDisplay(action) {
    const a = action && typeof action === "object" ? action : {};
    const type = String(a.type || "none");
    if (type === "none") return "";
    if (type === "macro") {
      const steps = Array.isArray(a.macro) ? a.macro : [];
      return steps.map(macroStepDisplay).join(" -> ");
    }
    return String(a.value === undefined ? "" : a.value);
  }

  /**
   * 由解析后的备份 JSON 生成预览模型（纯函数，无 DOM / 时钟）。
   *
   * inputCount 是当前设备布局下可见的输入数（10 或 12）。索引 >= 它的
   * 输入会照常写进存档，但在切换布局前不显示，列进 skippedExtra。
   */
  function buildImportPreview(data, filename, inputCount) {
    const doc = data && typeof data === "object" ? data : {};
    const inputs = Array.isArray(doc.inputs) ? doc.inputs : [];
    const source = doc.source && typeof doc.source === "object" ? doc.source : {};
    const typeCounts = {};
    KNOWN_ACTION_TYPES.forEach((k) => { typeCounts[k] = 0; });
    const items = [];
    inputs.forEach((entry) => {
      const e = entry && typeof entry === "object" ? entry : {};
      const action = e.action && typeof e.action === "object" ? e.action : {};
      const type = String(action.type || "none");
      if (typeCounts[type] === undefined) typeCounts[type] = 0;
      typeCounts[type] += 1;
      const display = actionDisplay(action);
      if (display) {
        items.push({ index: (Number(e.index) || 0) + 1, type, display });
      }
    });
    const skippedExtra = [];
    const n = Number(inputCount) || 10;
    inputs.forEach((entry) => {
      const e = entry && typeof entry === "object" ? entry : {};
      const idx = Number(e.index);
      if (Number.isFinite(idx) && idx >= n) skippedExtra.push(idx + 1);
    });
    skippedExtra.sort((a, b) => a - b);
    // 预览里实际展示的纯文本：命令 / 路径 / 网址全文 + 宏摘要，无 text:/paste: 正文。
    const safeText = items
      .map((it) => "#" + it.index + " [" + it.type + "] " + it.display)
      .join("\n");
    return {
      filename: String(filename || ""),
      source: [source.name, source.chip].filter(Boolean).join(" / "),
      exportedAt: String(doc.exported_at || ""),
      override: inputs.length,
      typeCounts,
      items,
      skippedExtra,
      safeText,
    };
  }

  /** 取消 / 确认会产生哪些请求（取消 = 零请求，且不写日志）。 */
  function decideImportAction(kind, ctx) {
    if (kind === "cancel") return { closed: true, requests: [], log: false };
    if (kind === "confirm") {
      const c = ctx || {};
      const devId = String(c.devId || "");
      return {
        closed: false,
        requests: [{
          method: "POST",
          url: "/api/actions/import/" + encodeURIComponent(devId),
          body: c.data,
        }],
        log: true,
      };
    }
    return { closed: false, requests: [], log: false };
  }

  /** 执行一个 decision：逐个发请求，返回响应数组。cancel 的 requests 为空即零请求。 */
  async function runDecision(decision, send) {
    const out = [];
    for (const req of (decision && decision.requests) || []) {
      out.push(await send(req));
    }
    return out;
  }

  return {
    KNOWN_ACTION_TYPES,
    KNOWN_MACRO_STEP_TYPES,
    macroStepDisplay,
    actionDisplay,
    buildImportPreview,
    decideImportAction,
    runDecision,
  };
});
