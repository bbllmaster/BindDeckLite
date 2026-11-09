// WS-542 round 2：「可唤醒输入」文案的区间与芯片判定回归用例。
//
// 运行：node --test host/test_portal_wake.mjs
//
// 两处界面各自把生产代码原样抽出来真跑，不在这里复制一份判定逻辑：
//   * 配网页 <script id='wakeinfo'> 内联在 src/main.cpp 的 cfgPage() 里
//     （单文件、无外部资源），按目标芯片解掉 #ifdef 后拼成 JS；
//   * 上位机 formatWakeList()/wakePiece() 在 host/static/app.js 里，按源码
//     抽取后配上生产 i18n 与生产 zh-CN 文案表。
// 所以改坏 src/main.cpp 或 app.js 的分段/芯片逻辑，这些断言会真的失败。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC_PATH = new URL("../src/main.cpp", import.meta.url);
const APP_JS = new URL("./static/app.js", import.meta.url);
const I18N_JS = new URL("./static/i18n.js", import.meta.url);
const ZH_LOCALE = new URL("./locales/zh-CN.json", import.meta.url);

// 固件默认的 EC11 固定脚（与 src/main.cpp 的 #define 一致；12 键布局下这三脚
// 分别是第 10/11/12 键）。测试只把它们填进抽取出来的脚本，不参与任何判定。
const CHIP_ENCODER_PINS = {
  ESP32: { SW: 5, CLK: 18, DT: 19 },
  C3: { SW: 10, CLK: 20, DT: 21 },
};

const ESP32_DEFAULT = [13, 12, 14, 27, 32, 33, 25, 26, 4];
const C3_DEFAULT = [0, 1, 2, 3, 4, 5, 6, 7, 8];

/**
 * 从 src/main.cpp 抽出配网页的 wakeinfo 脚本。
 *
 * 这段是若干个 C++ 字符串字面量拼起来的，中间还夹着 #ifdef TARGET_ESP32C3
 * （唤醒脚表与 multiWake 按芯片二选一）和 String(ENCODER_*)（固定脚）。
 * 这里按目标芯片解条件编译，再把字面量拼回一整段 JS。
 */
function extractWakeInfoScript(source, chip) {
  const startMark = "\"<script id='wakeinfo'>";
  const endMark = "\"})();</script>\"";
  const start = source.indexOf(startMark);
  assert.ok(start >= 0, "src/main.cpp 里找不到 wakeinfo 脚本的起始标记");
  const end = source.indexOf(endMark, start);
  assert.ok(end > start, "src/main.cpp 里找不到 wakeinfo 脚本的结束标记");
  const region = source.slice(start, end + endMark.length);

  const pins = CHIP_ENCODER_PINS[chip];
  const kept = [];
  let branchOn = true;
  for (const raw of region.split("\n")) {
    const line = raw.trim();
    if (line.startsWith("#ifdef")) {
      branchOn = (chip === "C3") === line.includes("TARGET_ESP32C3");
      continue;
    }
    if (line.startsWith("#else")) {
      branchOn = !branchOn;
      continue;
    }
    if (line.startsWith("#endif")) {
      branchOn = true;
      continue;
    }
    if (!branchOn) continue;
    if (line.startsWith("//")) continue; // C++ 注释，里面可能带引号
    kept.push(raw);
  }

  const code = kept.join("\n")
    .replace(/String\(ENCODER_SW\)/g, `"${pins.SW}"`)
    .replace(/String\(ENCODER_CLK\)/g, `"${pins.CLK}"`)
    .replace(/String\(ENCODER_DT\)/g, `"${pins.DT}"`);

  const parts = [];
  const re = /"((?:[^"\\]|\\.)*)"/g;
  let m;
  while ((m = re.exec(code)) !== null) parts.push(JSON.parse(`"${m[1]}"`));
  const js = parts.join("");
  const tag = "<script id='wakeinfo'>";
  assert.ok(js.startsWith(`${tag}(function(){`), "抽出来的配网页代码不是一段 IIFE");
  const body = js.slice(tag.length).replace(/<\/script>$/, "");
  assert.ok(body.includes("wakeInfo"), "抽出来的脚本没有渲染 wakeInfo");
  return body;
}

/** 在最小 DOM 上跑生产配网页脚本，返回写进 #wakeInfo 的那行文案。 */
function renderWakeInfo({ chip, pins, twelve }) {
  const js = extractWakeInfoScript(readFileSync(SRC_PATH, "utf8"), chip);
  const pinEls = pins.map((v) => ({ value: v == null ? "" : String(v), addEventListener() {} }));
  const infoEl = { textContent: "" };
  const layoutEl = { value: twelve ? "0" : "1" };
  const document = {
    getElementById: (id) => (id === "wakeInfo" ? infoEl : null),
    querySelector(sel) {
      const m = /^input\[name=pin(\d+)\]$/.exec(sel);
      if (m) return pinEls[Number(m[1])] || null;
      if (sel === "select[name=layout]") return layoutEl;
      return null;
    },
  };
  vm.runInNewContext(js, { document, console, parseInt, isNaN }, { filename: "wakeinfo.js" });
  return infoEl.textContent;
}

/* ---------------- 配网页：芯片 + 区间 ---------------- */

test("C3 默认引脚：按键 1–6 每颗都能单键唤醒（不再说 2–6 要一起按）", () => {
  assert.equal(
    renderWakeInfo({ chip: "C3", pins: C3_DEFAULT, twelve: false }),
    "可唤醒输入：按键 1–6；不可唤醒：按键 7–9、编码器按压",
  );
});

test("经典 ESP32 默认引脚：按键 1 单键，2–9 需同时按住，编码器按压不可唤醒", () => {
  assert.equal(
    renderWakeInfo({ chip: "ESP32", pins: ESP32_DEFAULT, twelve: false }),
    "可唤醒输入：按键 1；需同时按住才唤醒：按键 2–9；不可唤醒：编码器按压",
  );
});

test("C3 12 键：按键 1–6 可唤醒，7–9 与第 10–12 键不可唤醒", () => {
  assert.equal(
    renderWakeInfo({ chip: "C3", pins: C3_DEFAULT, twelve: true }),
    "可唤醒输入：按键 1–6；不可唤醒：按键 7–9、第 10–12 键",
  );
});

test("经典 ESP32 12 键：第 10–12 键固定在 5/18/19，全部不可唤醒", () => {
  assert.equal(
    renderWakeInfo({ chip: "ESP32", pins: ESP32_DEFAULT, twelve: true }),
    "可唤醒输入：按键 1；需同时按住才唤醒：按键 2–9；不可唤醒：第 10–12 键",
  );
});

test("跨 8→9 的不可唤醒区间拆成「按键 7–9、编码器按压」，不写「按键 10」", () => {
  const text = renderWakeInfo({ chip: "C3", pins: C3_DEFAULT, twelve: false });
  assert.ok(!text.includes("按键 10"), `编码器布局不该出现「按键 10」：${text}`);
  assert.ok(text.includes("按键 7–9"), text);
  assert.ok(text.includes("编码器按压"), text);
});

test("GPIO34–39 不能当按键，配网页也不说它们能唤醒（与上位机 range 一致）", () => {
  const pins = [34, ...ESP32_DEFAULT.slice(1)];
  const text = renderWakeInfo({ chip: "ESP32", pins, twelve: false });
  assert.equal(
    text,
    "可唤醒输入：按键 2；需同时按住才唤醒：按键 3–9；不可唤醒：按键 1、编码器按压",
  );
});

/* ---------------- 上位机：formatWakeList 区间 ---------------- */

/** 按花括号配对从生产 app.js 里抽一个顶层函数。 */
function extractAppFunction(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `app.js 里找不到 ${name}()`);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error(`${name}() 的花括号没有闭合`);
}

/** 生产 i18n + 生产 zh-CN 文案表。 */
function loadI18n() {
  const sandbox = { console };
  vm.createContext(sandbox);
  vm.runInContext(readFileSync(I18N_JS, "utf8"), sandbox, { filename: "i18n.js" });
  sandbox.i18n.setMessages(
    JSON.parse(readFileSync(ZH_LOCALE, "utf8")), "zh-CN");
  return sandbox.i18n;
}

/** 用生产的 formatWakeList()/wakePiece() 渲染，layout 由调用方钉住。 */
function formatWakeList(indices, twelve) {
  const source = readFileSync(APP_JS, "utf8");
  const code = [
    extractAppFunction(source, "formatWakeList"),
    extractAppFunction(source, "wakePiece"),
  ].join("\n");
  const build = new Function("t", "is12Key", `${code}\nreturn formatWakeList;`);
  const i18n = loadI18n();
  return build(i18n.t, () => twelve)(indices);
}

test("[6,7,8,9] 拆成「按键 7–9、编码器按压」——C3 默认布局每天都会撞到", () => {
  assert.equal(formatWakeList([6, 7, 8, 9], false), "按键 7–9、编码器按压");
  assert.ok(!formatWakeList([6, 7, 8, 9], false).includes("按键 10"));
});

test("[6,7,8,9,10,11] 在 12 键布局拆成「按键 7–9、第 10–12 键」", () => {
  assert.equal(formatWakeList([6, 7, 8, 9, 10, 11], true), "按键 7–9、第 10–12 键");
  assert.ok(!formatWakeList([6, 7, 8, 9, 10, 11], true).includes("按键 10"));
});

test("边界两侧仍是正常区间：按键 1–6 / 按键 9 / 第 10–12 键", () => {
  assert.equal(formatWakeList([0, 1, 2, 3, 4, 5], false), "按键 1–6");
  assert.equal(formatWakeList([8, 9], false), "按键 9、编码器按压");
  assert.equal(formatWakeList([9], false), "编码器按压");
  assert.equal(formatWakeList([9], true), "第 10 键");
  assert.equal(formatWakeList([9, 10, 11], true), "第 10–12 键");
});
