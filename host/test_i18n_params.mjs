// WS-541 must fix 2：日志事件里的动态文案必须能被前端 t() 真正替换。
//
// 运行：node --test host/test_i18n_params.mjs
//
// 背景：服务端把事件渲染成 text，同时带上 code + params，前端
// `e.code ? t(e.code, e.params) : e.text`（app.js:2477）重新翻译。这些事件
// 原来是 ``t("log.app_updated") % version`` —— Message.__mod__ 只能把参数记成
// ``params = {"args": [...]}``，而 i18n.js 的 t() 只认 ``{name}``，于是日志页
// 中英文都显示 ``%s``。现在模板改成 ``{version}`` / ``{count}``。
//
// 这里加载**生产** static/i18n.js（在无 require 的 vm 里，等价于浏览器），
// 用**生产** locale 文件断言渲染结果 —— 不复制一份翻译逻辑到测试里。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const STATIC_DIR = new URL("./static/", import.meta.url);
const LOCALES = new URL("../locales/", STATIC_DIR);

function loadI18n() {
  const sandbox = { console };
  vm.createContext(sandbox);
  vm.runInContext(
    readFileSync(new URL("i18n.js", STATIC_DIR), "utf8"), sandbox,
    { filename: "i18n.js" });
  assert.ok(sandbox.i18n, "i18n.js 没有挂到 globalThis 上");
  return sandbox.i18n;
}

function locale(tag) {
  return JSON.parse(readFileSync(new URL(`${tag}.json`, LOCALES), "utf8"));
}

// key → [params, 渲染后必须出现的值]
const CASES = [
  ["log.app_updated", { version: "v1.4.0" }, "v1.4.0"],
  ["log.app_update_skipped", { version: "v1.4.0" }, "v1.4.0"],
  ["log.app_update_skipped_short", { version: "v1.4.0" }, "v1.4.0"],
  ["log.app_up_to_date", { version: "v1.4.0" }, "v1.4.0"],
  ["log.app_update_restarting", { version: "v1.4.0" }, "v1.4.0"],
  ["log.app_update_failed_paused", { count: 2 }, "2"],
];

for (const tag of ["zh-CN", "en-US"]) {
  test(`${tag}: 更新日志模板用命名参数渲染，不残留 %s/%d`, () => {
    const i18n = loadI18n();
    i18n.setMessages(locale(tag), tag);
    for (const [key, params, expected] of CASES) {
      const text = i18n.t(key, params);
      assert.notEqual(text, key, `${key} 没有取到文案`);
      assert.ok(text.includes(expected), `${key} 没有替换 ${JSON.stringify(params)}：${text}`);
      assert.doesNotMatch(text, /%[sd]/, `${key} 还留着 printf 占位符：${text}`);
    }
  });
}

test("语言切换后同一批 code+params 立即换成另一种语言", () => {
  const i18n = loadI18n();
  i18n.setMessages(locale("zh-CN"), "zh-CN");
  assert.equal(i18n.t("log.app_updated", { version: "v1.4.0" }), "上位机已更新到 v1.4.0");

  i18n.setMessages(locale("en-US"), "en-US");
  assert.equal(i18n.t("log.app_updated", { version: "v1.4.0" }), "Host updated to v1.4.0");
});
