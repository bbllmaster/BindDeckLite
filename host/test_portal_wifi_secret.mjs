// WS-590 配网页「WiFi 密码不回显 + 留空不修改」的回归用例。
//
// 运行：cd host && node --test test_portal_wifi_secret.mjs
//
// ⚠️ 这是**源码级静态断言**，不是运行期测试。配网页是固件里用 C++ 字符串拼出来
// 的，node 这边没有可渲染的产物，所以本文件只从 src/main.cpp 抽取相关源码段，
// 断言其结构：密码框不带 value、/save 有「留空且 SSID 未变 → 沿用旧密码」分支、
// 状态行把 WiFi.status() 映射成可读原因、#dirty 仍跟踪 pwd。
//
// 运行期行为（页面上确实搜不到密码、留空保存后设备仍能用原密码连上）由真机证据
// 覆盖：curl -s http://192.168.4.1 | grep -c '<密码>' 必须为 0。本文件不声称
// 验证了运行期。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC_PATH = new URL("../src/main.cpp", import.meta.url);
const SRC = readFileSync(SRC_PATH, "utf8");

const PAGE_START = "static String cfgPage(";
const PAGE_END = "static const char* otaUpdateErrorText";
const SAVE_START = "cfgServer.on(\"/save\", HTTP_POST";
const SAVE_END = "cfgServer.on(\"/keys\", HTTP_POST";

/** 丢掉 C++ 行注释：注释里可能出现引号 / 伪代码，会干扰断言。 */
function stripComments(source) {
  return source
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .join("\n");
}

function slice(source, start, end) {
  const a = source.indexOf(start);
  assert.ok(a >= 0, `src/main.cpp 里找不到起始标记：${start}`);
  const b = source.indexOf(end, a + start.length);
  assert.ok(b > a, `src/main.cpp 里找不到结束标记：${end}`);
  return source.slice(a, b);
}

/** cfgPage() 的页面拼装段（含 WiFi 表单与状态行、不含 /save 处理）。 */
const PAGE = stripComments(slice(SRC, PAGE_START, PAGE_END));
/** /save 的请求处理段。 */
const SAVE = stripComments(slice(SRC, SAVE_START, SAVE_END));

/** 取出 name=<name> 那个 input 标签（到最近的 '>' 为止，跨相邻字符串字面量）。 */
function inputTag(page, name) {
  const at = page.indexOf(`name='${name}'`);
  assert.ok(at >= 0, `页面里找不到 name='${name}' 的字段`);
  const end = page.indexOf(">", at);
  assert.ok(end > at, `name='${name}' 的 input 没有闭合的 '>'`);
  return page.slice(at, end + 1);
}

/** 收集源码里的所有 if (...) { ... } 块（按括号配平，能处理条件里的函数调用）。 */
function ifBlocks(source) {
  const out = [];
  const re = /\bif\s*\(/g;
  let m;
  while ((m = re.exec(source)) !== null) {
    let i = source.indexOf("(", m.index);
    let depth = 0;
    for (; i < source.length; i++) {
      if (source[i] === "(") depth++;
      else if (source[i] === ")" && --depth === 0) break;
    }
    const cond = source.slice(source.indexOf("(", m.index) + 1, i);
    let j = source.indexOf("{", i);
    if (j < 0) continue;
    let bodyDepth = 0;
    const bodyStart = j;
    for (; j < source.length; j++) {
      if (source[j] === "{") bodyDepth++;
      else if (source[j] === "}" && --bodyDepth === 0) break;
    }
    out.push({ cond, body: source.slice(bodyStart + 1, j), index: m.index });
  }
  return out;
}

/* ---------------- 页面：密码绝不回填 ---------------- */

test("密码框没有 value，页面里也不再出现 htmlEsc(pwd)", () => {
  const tag = inputTag(PAGE, "pwd");
  assert.ok(!/\bvalue\s*=/.test(tag), `密码框不能带 value（会回填 NVS 里的明文）：${tag}`);
  const attrs = tag.replace(/name='pwd'/, "");
  assert.ok(!/\bpwd\b/.test(attrs), `密码框的其他属性不能由 pwd 拼接：${tag}`);
  assert.ok(!/htmlEsc\s*\(\s*pwd\s*\)/.test(PAGE), "页面里不能再 htmlEsc(pwd) 把密码写进 HTML");

  // 页面的 HTML 全部来自字符串字面量：字面量里只剩字段名，才谈得上「不回显」。
  const literals = PAGE.match(/"(?:[^"\\]|\\.)*"/g) || [];
  const leaking = literals.filter((lit) => {
    const body = lit.replace(/name='pwd'/g, "").replace(/'pwd'/g, "");
    return /\bpwd\b/.test(body);
  });
  assert.deepEqual(leaking, [], `这些字面量把密码带进了页面：${leaking.join(" | ")}`);

  // 字面量之外，pwd 只允许出现在「读 NVS / 判断是否已保存」这两处。
  const code = PAGE.replace(/"(?:[^"\\]|\\.)*"/g, '""')
    .replace(/pwd\.length\(\)/g, "")
    .replace(/String ssid, pwd;/g, "")
    .replace(/loadWifiCreds\(ssid, pwd\);/g, "");
  assert.ok(!/\bpwd\b/.test(code), "cfgPage() 里 pwd 不能被拼进页面，只能用于判断是否已保存");
});

test("已存密码时给 placeholder 与「已保存（不显示）」，没存过就是普通空框", () => {
  const cond = /\bif\s*\(\s*pwd\.length\(\)\s*\)\s*\{/.exec(PAGE);
  assert.ok(cond, "placeholder 必须按「是否存过密码」分支，不能无条件写");
  const tag = inputTag(PAGE, "pwd");
  assert.match(tag, /type='password'/, "密码框类型变了");
  assert.match(tag, /autocomplete='new-password'/, "要让浏览器别再自动填回旧密码");

  const saved = PAGE.slice(cond.index, PAGE.indexOf("else", cond.index));
  assert.match(saved, /placeholder='已保存，留空则不修改'/, "已存密码时要有「留空则不修改」的提示");
  assert.match(saved, /已保存（不显示）/, "已存密码时下方要说明密码已保存但不显示");

  const bare = PAGE.slice(PAGE.indexOf("else", cond.index));
  assert.ok(!/placeholder=/.test(bare.slice(0, bare.indexOf("}"))), "没存过密码时不要写 placeholder");
  assert.ok(!/已保存（不显示）/.test(bare.slice(0, bare.indexOf("}"))), "没存过密码时不要说已保存");
});

test("补了一句「密码保存后不再显示」的说明", () => {
  assert.match(PAGE, /密码保存后不再显示；忘记就重新输入一次/, "页面缺少密码不回显的说明文案");
});

/* ---------------- /save：留空 = 不修改 ---------------- */

test("SSID 为空仍是 400", () => {
  assert.match(SAVE, /if\s*\(\s*!ssid\.length\(\)\s*\)/, "SSID 空值检查被改掉了");
  assert.match(SAVE, /send\(400/, "SSID 为空必须仍是 400");
});

test("留空且 SSID 未变时沿用 NVS 旧密码，且不会写成空", () => {
  assert.match(SAVE, /loadWifiCreds\(oldSsid, oldPwd\)/, "/save 必须先读 NVS 里的旧值");
  assert.match(
    SAVE,
    /bool\s+sameSsid\s*=\s*\(\s*ssid\s*==\s*oldSsid\s*\)/,
    "需要显式比较提交的 SSID 与已存 SSID",
  );

  const keep = ifBlocks(SAVE).filter((b) => /!\s*pwd\.length\(\)/.test(b.cond));
  assert.equal(keep.length, 1, "应恰好有一个「密码为空」的显式分支");
  const [guard] = keep;
  assert.match(guard.cond, /sameSsid/, "沿用旧密码必须同时要求 SSID 未变");
  assert.match(guard.body, /pwd\s*=\s*[A-Za-z_]\w*\s*;/, "分支里应把 NVS 旧密码赋回 pwd");
  assert.ok(!/pwd\s*=\s*""/.test(guard.body), "沿用分支不能把密码清空");

  // 解析必须发生在落盘之前，否则存进去的仍是空串。
  assert.ok(
    SAVE.indexOf("sameSsid") < SAVE.indexOf("saveWifiCreds(ssid, pwd)"),
    "密码解析要在 saveWifiCreds() 之前完成",
  );
});

test("密码非空时按提交值写入（不会被旧密码覆盖）", () => {
  const keep = ifBlocks(SAVE).filter((b) => /!\s*pwd\.length\(\)/.test(b.cond));
  assert.equal(keep.length, 1);
  assert.match(keep[0].cond, /^\s*!\s*pwd\.length\(\)/, "沿用旧密码的分支必须先排除「填了新密码」");
  assert.match(SAVE, /saveWifiCreds\(ssid,\s*pwd\)/, "落盘仍走 saveWifiCreds(ssid, pwd)");
});

test("SSID 变了且密码留空：按空密码保存（开放网络），不报错", () => {
  const keep = ifBlocks(SAVE).filter((b) => /!\s*pwd\.length\(\)/.test(b.cond));
  assert.equal(keep.length, 1);
  assert.match(keep[0].cond, /sameSsid/, "只有 SSID 相同时才沿用旧密码");
  assert.ok(!/send\(4\d\d/.test(keep[0].body), "换 SSID 且密码为空不是错误，不能返回 4xx");
  assert.ok(
    SAVE.indexOf("saveWifiCreds(ssid, pwd)") < SAVE.indexOf("WiFi.begin"),
    "先落盘再发起连接",
  );
});

/* ---------------- 状态行：给可读原因 ---------------- */

test("未连接时按 WiFi.status() 给可读原因，已连接文案不变", () => {
  assert.match(PAGE, /WiFi\.status\(\)\s*==\s*WL_CONNECTED/, "已连接判断被改掉了");
  assert.match(PAGE, /"已连接 <b>"\s*\+\s*htmlEsc\(WiFi\.SSID\(\)\)/, "已连接的文案不应改变");

  const expect = [
    ["WL_NO_SSID_AVAIL", /找不到该 WiFi/],
    ["WL_CONNECT_FAILED", /密码可能不对/],
    ["WL_DISCONNECTED", /正在连接/],
    ["WL_IDLE_STATUS", /尚未开始连接/],
  ];
  for (const [state, reason] of expect) {
    const at = PAGE.indexOf(state);
    assert.ok(at >= 0, `状态行缺少 ${state} 的映射`);
    const next = PAGE.indexOf("break;", at);
    assert.ok(next > at, `${state} 的 case 没有 break`);
    assert.match(PAGE.slice(at, next), reason, `${state} 没有对应的可读原因`);
  }
});

/* ---------------- WS-572 的未保存跟踪 ---------------- */

test("#dirty 的 FIELDS.wifi 仍含 pwd（基线变空串，结构不变）", () => {
  assert.match(PAGE, /var FIELDS=\{wifi:\['ssid','pwd'\]/, "FIELDS.wifi 不能去掉 pwd");
  // 基线就是页面渲染的值；密码框没有 value 后基线自然是空串。
  assert.ok(!/\bvalue\s*=/.test(inputTag(PAGE, "pwd")), "密码框带 value 会让基线变成明文密码");
});
