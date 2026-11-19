// WS-646 日志页「导出…」的前端回归用例（WS-649 补齐防重与 _text）。
//
// 运行：node --test test_log_export.mjs
//
// 断言对象是 host/static/log_export.js 里的**生产函数**（app.js 在浏览器里
// 用的同一份）、它所复用的 LogDevice.logCopyText，以及通过 app_harness.mjs
// 加载的**生产 app.js** 本身 —— 不在这里复制任何头部拼装 / 行格式化 / 防重
// 逻辑，改坏生产代码会让断言真的失败。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import LogExport from "./static/log_export.js";
import LogDevice from "./static/log_device.js";
import i18n from "./static/i18n.js";
import { loadApp, makeEl } from "./app_harness.mjs";

const {
  BUFFER_MAX,
  VIEW_MAX,
  formatStamp,
  suggestFileName,
  exportButtonState,
  buildHeader,
  buildExportText,
  sanitizeNamePart,
} = LogExport;

const A = "AA:BB:CC:DD:EE:01";
const B = "AA:BB:CC:DD:EE:02";
const DEVICES = [
  { dev_id: A, name: "BindDeckLite-A1B2", connected: true },
  { dev_id: B, name: "BindDeckLite-B2C3", connected: true },
];

// 本地时间构造：formatStamp 用本地字段，测试不能依赖机器时区。
const NOW = new Date(2026, 10, 20, 0, 12, 33);

/** 造一个「屏幕上的一行」：DOM 只用到 hidden / _event / _text。 */
function row(event, extra = {}) {
  return { hidden: false, _event: event, _text: event.text, ...extra };
}

// 1. 头部 5 行：时间、版本与形态、筛选（未生效写「全部」「(无)」）、命中数。
test("头部固定 5 行，未生效的筛选项写「全部」「(无)」", () => {
  const header = buildHeader({
    now: NOW,
    version: "v1.4.3",
    mode: "desktop",
    filter: { kind: "", level: "", q: "", dev: "" },
    devLabel: "",
    count: 12,
  });
  assert.equal(header.length, 5);
  assert.equal(header[0], "BindDeckLite 日志导出");
  assert.equal(header[1], "导出时间: 2026-11-20 00:12:33");
  assert.equal(header[2], "上位机: v1.4.3（桌面版）");
  assert.equal(header[3], "筛选: 级别=全部 · 类型=全部 · 设备=全部 · 搜索=(无)");
  // WS-649：头部必须同时写出「内存缓冲 400」与「屏幕只留 300」，
  // 否则用户会以为导出少了 100 行。
  assert.equal(header[4], `命中: 12 行（日志缓冲上限 ${BUFFER_MAX} 行，屏幕最多保留 ${VIEW_MAX} 行，更早的行已不在列表里）`);
  assert.equal(BUFFER_MAX, 400);
  assert.equal(VIEW_MAX, 300);
  assert.ok(header[4].includes(String(BUFFER_MAX)) && header[4].includes(String(VIEW_MAX)));
});

// 2. 头部按实际生效项写：级别 / 类型用页面上的同一套标签，搜索词带引号，
//    设备写显示名（不是 MAC）。
test("头部按生效筛选写级别/类型/设备/搜索，搜索词原样带引号", () => {
  const header = buildHeader({
    now: NOW,
    version: "v1.4.3",
    mode: "browser",
    filter: { kind: "cfg", level: "error", q: "timeout", dev: A },
    devLabel: "BindDeckLite-A1B2",
    count: 3,
  });
  assert.equal(header[2], "上位机: v1.4.3（浏览器）");
  assert.equal(header[3],
    "筛选: 级别=错误 · 类型=配置 cfg · 设备=BindDeckLite-A1B2 · 搜索=\"timeout\"");
  assert.equal(header[4], `命中: 3 行（日志缓冲上限 ${BUFFER_MAX} 行，屏幕最多保留 ${VIEW_MAX} 行，更早的行已不在列表里）`);
});

// 3. 正文只取可见行、保持 DOM 顺序，且每一行都等于 logCopyText 的输出
//    （与单行「复制」同一份格式化，不是第二套）。
test("正文按 DOM 顺序取可见行，行文本就是 logCopyText 的输出", () => {
  const e1 = { time: "00:00:01", kind: "key", text: "按键 1", dev: A };
  const e2 = { time: "00:00:02", kind: "cfg", text: "配置已下发", dev: B };
  const e3 = { time: "00:00:03", kind: "device", text: "设备上线", dev: A };
  const rows = [row(e1), row(e2, { hidden: true }), row(e3)];

  const out = buildExportText({
    rows, devices: DEVICES, now: NOW, version: "v1.4.3",
    mode: "desktop", filter: { kind: "", level: "", q: "", dev: "" }, devLabel: "",
  });

  assert.equal(out.count, 2);           // 被筛掉的行不导出
  assert.deepEqual(out.rows, [
    LogDevice.logCopyText(e1, DEVICES, "按键 1"),
    LogDevice.logCopyText(e3, DEVICES, "设备上线"),
  ]);
  assert.equal(out.rows[0], "00:00:01 [key] [BindDeckLite-A1B2] 按键 1");
  assert.ok(out.rows[1].indexOf("配置已下发") === -1);

  const lines = out.text.split("\n");
  assert.deepEqual(lines.slice(0, 5), out.header);
  assert.equal(lines[5], "");           // 头部与正文之间空一行
  assert.deepEqual(lines.slice(6), [out.rows[0], out.rows[1], ""]);  // 末尾换行
});

// 4. 脱敏沿用事件里的摘要：`text:` 行导出的是长度摘要，不得出现原文。
test("text: 宏步骤导出的是长度摘要，不含原文", () => {
  const secret = "correct horse battery staple";
  const event = {
    time: "00:00:04", kind: "action", dev: A,
    text: `text(${secret.length} 字符)`,
  };
  // 就算渲染后的正文被换成摘要，导出也不得回头读事件里的原始正文之外的
  // 敏感内容 —— 生产路径给的就是摘要。
  const out = buildExportText({
    rows: [row(event, { _text: "text(28 字符)" })],
    devices: DEVICES, now: NOW, version: "",
    mode: "desktop", filter: {}, devLabel: "",
  });
  assert.equal(out.count, 1);
  assert.ok(!out.text.includes(secret));
  assert.ok(out.text.includes("text(28 字符)"));
  assert.equal(out.header[2], "上位机: —（桌面版）");  // 版本缺失不编造
});

// 5. 0 行时按钮禁用并说明原因；有命中即可点。
test("0 行禁用并给出原因，有命中可点", () => {
  const none = exportButtonState(0);
  assert.equal(none.disabled, true);
  assert.equal(none.title, "没有可导出的日志");
  assert.equal(exportButtonState(undefined).disabled, true);

  const some = exportButtonState(3);
  assert.equal(some.disabled, false);
  assert.equal(some.title, "导出当前筛选命中的日志");
});

// 6. 文件名净化：只留 [A-Za-z0-9._-]，中文 / 空格 / 斜杠全部变 _，空名回退 all。
test("文件名净化：设备名里的中文/空格/路径分隔符都变下划线", () => {
  assert.equal(sanitizeNamePart("MyDeck.v2"), "MyDeck.v2");
  assert.equal(sanitizeNamePart("设备 A/1"), "A_1");
  assert.equal(sanitizeNamePart("../../etc/passwd"), "etc_passwd");
  assert.equal(sanitizeNamePart(""), "");

  assert.equal(suggestFileName("BindDeckLite-A1B2", NOW),
    "BindDeckLite-log-BindDeckLite-A1B2-20261120-0012.txt");
  assert.equal(suggestFileName("设备 A", NOW),
    "BindDeckLite-log-A-20261120-0012.txt");
  // 纯中文 / 空设备名不产生 "--" 这种没有信息量的名字。
  assert.equal(suggestFileName("", NOW), "BindDeckLite-log-all-20261120-0012.txt");
  assert.equal(suggestFileName("编码器", NOW), "BindDeckLite-log-all-20261120-0012.txt");
});

// 7. 时间戳用本地时间，格式与头部一致。
test("时间戳格式", () => {
  assert.equal(formatStamp(NOW), "2026-11-20 00:12:33");
});

// 8. 切到英文后，头部与正文都跟着换（导出的是当前语言）。
test("切语言后头部用英文", () => {
  const en = JSON.parse(readFileSync(new URL("locales/en-US.json", import.meta.url), "utf8"));
  i18n.setMessages(en, "en-US");
  const header = buildHeader({
    now: NOW, version: "v1.4.3", mode: "desktop",
    filter: { kind: "", level: "", q: "", dev: "" }, devLabel: "", count: 5,
  });
  assert.equal(header[0], "BindDeckLite log export");
  assert.equal(header[3], "Filter: level=All · kind=All · device=All · search=(none)");
  assert.equal(header[4], `Matched: 5 rows (the buffer holds ${BUFFER_MAX} rows; the list keeps only the newest ${VIEW_MAX})`);
  // 还原语言：i18n 是模块级单例，后面的用例仍按中文断言。
  const zh = JSON.parse(readFileSync(new URL("locales/zh-CN.json", import.meta.url), "utf8"));
  i18n.setMessages(zh, "zh-CN");
});

// 9. WS-649：正文取**渲染后**的 row._text，不是事件里的原始正文。
//    切语言后屏幕上是英文，导出也必须是英文（两参调用 logCopyText 会退回归
//    事件原文，这条断言就会红）。
test("正文用 row._text 而不是事件原文（切语言后导出仍是屏幕上的字）", () => {
  const event = { time: "00:00:05", kind: "key", dev: A, text: "按键 1" };
  const out = buildExportText({
    rows: [row(event, { _text: "Button 1" })],
    devices: DEVICES, now: NOW, version: "v1.4.3",
    mode: "desktop", filter: {}, devLabel: "",
  });
  assert.equal(out.count, 1);
  assert.equal(out.rows[0], "00:00:05 [key] [BindDeckLite-A1B2] Button 1");
  assert.ok(!out.text.includes("按键 1"), "不得回退到未渲染的事件正文");
});

// 10. WS-649：导出中恒禁用 —— 纯函数层面先钉住判定，再在下面用生产 app.js
//     验证调用方真的把 busy 传了进来（只改一处都不够）。
test("导出中按钮恒禁用，与命中行数无关", () => {
  const busySome = exportButtonState(3, true);
  assert.equal(busySome.disabled, true);
  assert.equal(busySome.title, "导出中…");
  assert.equal(exportButtonState(0, true).disabled, true);
  // 不忙时按行数判定，行为不变。
  assert.equal(exportButtonState(3, false).disabled, false);
  assert.equal(exportButtonState(0, false).disabled, true);
});

/* ---------------- 导出防重（生产 app.js + 真实入口） ----------------
 * 复现路径就是评审报告的那条：导出进行中（/api/log/export 未返回）时轮询
 * 追加一行 → syncLogFilterChrome() 重算按钮态 → 再点一次。旧实现读
 * btn.disabled 当状态，会被重新算成「可点」，于是发第二份请求、弹第二个
 * 保存对话框。
 */

/** 让本轮微任务跑完：vm 里的 await 链在 test realm 之外，需要一次宏任务。 */
const flush = () => new Promise((resolve) => setImmediate(resolve));

/** app.js 的 init() 也会发若干引导请求；只数导出流程自己的那两条。 */
const EXPORT_URLS = ["/api/app/version", "/api/log/export"];
const exportCalls = (calls) => calls.filter((u) => EXPORT_URLS.includes(u));

/** 往 #logList 里放一行「屏幕上的日志」，字段与 buildLogRow 产出的一致。 */
function appendRow(list, text = "按键 1") {
  const row = makeEl("div.log-row");
  row.dataset = { kind: "key", level: "info", dev: "" };
  row._event = { time: "00:00:01", kind: "key", dev: "", text, level: "info" };
  row._text = text;
  row.hidden = false;
  list.appendChild(row);
  return row;
}

test("导出中轮询/改筛选/切语言都不会重新启用按钮，连点只发一次请求", async () => {
  const calls = [];
  // 永不 resolve：模拟保存对话框还开着、/api/log/export 没返回。
  const hang = () => new Promise(() => {});
  const { host, el } = loadApp({
    fetch: (url) => { calls.push(url); return hang(); },
  });
  const btn = el("#btnLogExport");
  const list = el("#logList");

  host.state.logFilter = { kind: "", level: "", q: "", dev: "" };
  host.state.logFollow = false;
  appendRow(list);
  host.syncLogFilterChrome();
  assert.equal(btn.disabled, false, "有可见行时可点");

  // 第一次点击：进入导出，卡在取版本号上。
  void host.exportVisibleLog();
  await flush();
  assert.equal(btn.disabled, true);
  assert.equal(btn.textContent, "导出中…");
  assert.deepEqual(exportCalls(calls), ["/api/app/version"]);

  // 轮询追加一行并重算按钮态（旧实现就是在这里把按钮重新打开）。
  appendRow(list, "按键 2");
  host.syncLogFilterChrome();
  assert.equal(btn.disabled, true, "导出中追加日志不得重新启用按钮");
  assert.equal(btn.textContent, "导出中…");
  assert.equal(host.isLogExportBusy(), true, "忙标记要覆盖整个导出过程");

  // 改筛选、切语言同样会重算，也不许打开。
  host.state.logFilter = { kind: "key", level: "", q: "", dev: "" };
  host.applyLogFilter();
  assert.equal(btn.disabled, true, "导出中改筛选不得重新启用按钮");
  host.retranslateLog();
  assert.equal(btn.disabled, true, "导出中切语言不得重新启用按钮");

  // 再点一次：busy 直接挡回，不发第二份请求、不弹第二个对话框。
  void host.exportVisibleLog();
  await flush();
  assert.deepEqual(exportCalls(calls), ["/api/app/version"], "第二次点击不得发起新请求");
});

test("导出结束后释放 busy：取消保存对话框后按钮按行数恢复", async () => {
  const calls = [];
  const { host, el } = loadApp({
    fetch: async (url) => {
      calls.push(url);
      if (url === "/api/app/version") {
        return { ok: true, json: async () => ({ version: "v1.4.3" }) };
      }
      if (url === "/api/log/export") {
        return { ok: true, json: async () => ({ ok: false, cancelled: true }) };
      }
      // 其余是 init() 的引导请求：挂住即可，不影响导出流程的断言。
      return new Promise(() => {});
    },
  });
  const btn = el("#btnLogExport");
  const list = el("#logList");

  host.state.logFilter = { kind: "", level: "", q: "", dev: "" };
  host.state.logFollow = false;
  appendRow(list);
  host.syncLogFilterChrome();

  await host.exportVisibleLog();
  assert.deepEqual(exportCalls(calls), ["/api/app/version", "/api/log/export"]);
  assert.equal(host.isLogExportBusy(), false, "finally 必须释放 busy");
  assert.equal(btn.textContent, "导出…");
  assert.equal(btn.disabled, false, "取消后还有可见行，按钮恢复可点");

  // 清空后导出结束，按 0 行禁用（不是靠 busy 兜着）。
  host.state.logFilter = { kind: "", level: "", q: "", dev: "" };
  list.innerHTML = "";
  list.children.length = 0;
  host.syncLogFilterChrome();
  assert.equal(btn.disabled, true);
  assert.equal(btn.title, "没有可导出的日志");
});
