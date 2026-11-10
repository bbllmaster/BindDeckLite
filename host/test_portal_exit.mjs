// WS-551 配网页「退出配网并重启」的回归用例。
//
// 运行：node --test host/test_portal_exit.mjs
//
// 配网页与路由都内联在 src/main.cpp 里（固件无测试框架，也没有在 PC 上跑
// WebServer 的桩），所以这里从生产源文件里**原样抽取** cfgPage() 的页面区块、
// /exit 的两条路由和 loop() 的消费分支，再对它们断言。改动固件里的任何一处
// （表单 action、确认文案、POST/GET 的注册方式、回调里直接 ESP.restart()、
// loop() 不再置 BOOT_REBOOT）都会让这些断言真的失败 —— 不是在这份测试里复制
// 一份判定逻辑自证。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC_PATH = new URL("../src/main.cpp", import.meta.url);
const SRC = readFileSync(SRC_PATH, "utf8");

/** 取 [startMarker, endMarker) 之间的源码，标记缺失直接失败。 */
function sliceBetween(source, startMarker, endMarker, what) {
  const start = source.indexOf(startMarker);
  assert.ok(start >= 0, `src/main.cpp 里找不到${what}的起点 (${startMarker})`);
  const end = source.indexOf(endMarker, start + startMarker.length);
  assert.ok(end > start, `src/main.cpp 里找不到${what}的终点 (${endMarker})`);
  return source.slice(start, end);
}

// cfgPage() 的页面拼装段：到「OTA 错误文案表」声明之前为止。
const CFG_PAGE = sliceBetween(SRC, "static String cfgPage(", "static const char* otaUpdateErrorText",
                              " cfgPage()");
// /exit 的 POST 处理：从注册处到 GET 注册处。
const EXIT_POST = sliceBetween(SRC, 'cfgServer.on("/exit", HTTP_POST', 'cfgServer.on("/exit", HTTP_GET', "/exit POST 路由");
const EXIT_GET = sliceBetween(SRC, 'cfgServer.on("/exit", HTTP_GET', 'cfgServer.on("/keys/state"', "/exit GET 路由");
// loop() 在文件末尾：从声明处抽到文件尾。
const LOOP_START = SRC.indexOf("void loop()");
assert.ok(LOOP_START >= 0, "src/main.cpp 里找不到 loop()");
const LOOP = SRC.slice(LOOP_START);

test("页面有「退出配网并重启」表单，位置在按键表单之后、OTA 链接之前", () => {
  const keysFormEnd = CFG_PAGE.indexOf("保存按键配置</button>");
  const exitForm = CFG_PAGE.indexOf("action='/exit'");
  const otaLink = CFG_PAGE.indexOf("href='/update'");
  assert.ok(keysFormEnd >= 0, "找不到按键表单的提交按钮");
  assert.ok(exitForm >= 0, "配网页里没有 action='/exit' 的表单");
  assert.ok(otaLink >= 0, "找不到 OTA 链接");
  assert.ok(keysFormEnd < exitForm, "/exit 表单必须在按键表单之后");
  assert.ok(exitForm < otaLink, "/exit 表单必须在 OTA 链接之前");
  assert.match(CFG_PAGE.slice(exitForm - 80, exitForm), /method='POST'/,
               "/exit 表单必须是 POST");
  assert.match(CFG_PAGE, /退出配网并重启/, "缺少主按钮文案");
  assert.match(CFG_PAGE, /未保存的表单修改会丢失/, "缺少「未保存会丢失」的说明");
});

test("确认框文案写明未保存的修改会丢失", () => {
  // WS-572: the wording is generated from the unsaved state now, so the exit
  // form points at the guard instead of carrying a fixed sentence. The
  // sentence itself is asserted against the dirty script below (and exercised
  // for real by host/test_portal_dirty.mjs).
  const exitForm = CFG_PAGE.indexOf("action='/exit'");
  assert.ok(exitForm >= 0, "配网页里没有 action='/exit' 的表单");
  const handler = CFG_PAGE.slice(exitForm, CFG_PAGE.indexOf(">", CFG_PAGE.indexOf("button", exitForm)));
  assert.match(handler, /portalDirty\.leave\(\)/, "退出必须走动态确认（portalDirty.leave）");
  assert.match(CFG_PAGE, /退出配网并重启？/, "缺少退出确认的主文案");
  assert.match(CFG_PAGE, /以下修改尚未保存，会丢失：/, "缺少「未保存会丢失」的动态点名");
});

test("320 px 下不横向溢出：按钮撑满且计入内边距", () => {
  const block = CFG_PAGE.slice(CFG_PAGE.indexOf("完成配置"), CFG_PAGE.indexOf("href='/update'"));
  assert.match(block, /width:100%/, "按钮应撑满一列");
  assert.match(block, /box-sizing:border-box/, "width:100% + padding 需要 border-box，否则 320 px 横向滚动");
});

test("POST /exit 先响应再延后重启，不在回调里 ESP.restart()", () => {
  assert.match(EXIT_POST, /cfgServer\.send\(200/, "POST /exit 必须返回 200");
  assert.match(EXIT_POST, /正在退出配网/, "结果页必须给出「正在退出配网」文案");
  assert.match(EXIT_POST, /g_portalExitAt\s*=\s*millis\(\)\s*\+\s*PORTAL_EXIT_MS/,
               "重启必须交给 loop() 的 deadline，而不是立刻执行");
  assert.doesNotMatch(EXIT_POST, /ESP\.restart\(\)/, "WebServer 回调里不能直接 ESP.restart()");
  assert.doesNotMatch(EXIT_POST, /delay\(/, "回调里不能靠 delay() 等响应发完");
});

test("GET /exit 返回 405 且不重启", () => {
  assert.match(EXIT_GET, /send\(405/, "GET /exit 必须返回 405");
  assert.doesNotMatch(EXIT_GET, /g_portalExitAt|BOOT_REBOOT|ESP\.restart\(\)/,
                      "GET /exit 不能触发任何重启路径");
});

test("loop() 到点后才置 BOOT_REBOOT，复用既有延迟重启路径", () => {
  assert.match(LOOP, /g_portalExitAt/, "loop() 没有消费 g_portalExitAt");
  const armAt = LOOP.indexOf("g_bootReq = BOOT_REBOOT");
  assert.ok(armAt >= 0, "loop() 到点后必须置 BOOT_REBOOT");
  const guard = LOOP.lastIndexOf("if (g_portalExitAt", armAt);
  assert.ok(guard >= 0, "BOOT_REBOOT 必须由 g_portalExitAt 的到点判断触发");
  assert.match(LOOP.slice(guard, armAt), /millis\(\)\s*-\s*g_portalExitAt/,
               "到点判断必须比较 millis() 与 g_portalExitAt");
  // 退出只重启：不得出现清凭据 / 清绑定的调用。
  const armRegion = LOOP.slice(armAt - 600, armAt + 200);
  assert.doesNotMatch(armRegion, /putBool\(|putString\(|CLEARBOND|WiFi\.disconnect/,
                      "退出配网只重启，不写 NVS、不清 WiFi 凭据、不清绑定");
});
