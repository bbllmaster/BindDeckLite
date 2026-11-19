// WS-646 日志页「导出…」的前端回归用例。
//
// 运行：node --test test_log_export.mjs
//
// 断言对象是 host/static/log_export.js 里的**生产函数**（app.js 在浏览器里
// 用的同一份），以及它所复用的 LogDevice.logCopyText —— 不在这里复制任何
// 头部拼装 / 行格式化逻辑，改坏生产代码会让断言真的失败。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import LogExport from "./static/log_export.js";
import LogDevice from "./static/log_device.js";
import i18n from "./static/i18n.js";

const {
  BUFFER_MAX,
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
  assert.equal(header[4], `命中: 12 行（日志缓冲上限 ${BUFFER_MAX} 行，更早的行已丢弃）`);
  assert.equal(BUFFER_MAX, 400);
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
  assert.equal(header[4], `命中: 3 行（日志缓冲上限 ${BUFFER_MAX} 行，更早的行已丢弃）`);
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
  assert.equal(header[4], `Matched: 5 rows (the buffer holds ${BUFFER_MAX} rows; older rows are gone)`);
});
