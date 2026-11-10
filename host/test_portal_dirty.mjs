// WS-572 配网页「未保存状态 + 跨表单拦截」的回归用例。
//
// 运行：cd host && node --test test_portal_dirty.mjs
//
// 配网页的脚本内联在 src/main.cpp 的 cfgPage() 里（页面必须保持单文件、无外部
// 资源），所以这里从生产源文件里**原样抽取** <script id='dirty'> 那一段，在假的
// DOM 上真跑，而不是在这份测试里复制一份判定逻辑：
//   * 改字段分组 / 基线比较 / 文案 / confirm 行为，都会让断言真的失败；
//   * 字段名、表单 action 用源串断言守住，防止重构把协议改掉。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC_PATH = new URL("../src/main.cpp", import.meta.url);
const SRC = readFileSync(SRC_PATH, "utf8");

const PAGE_START = "static String cfgPage(";
const PAGE_END = "static const char* otaUpdateErrorText";

/** cfgPage() 的页面拼装段。 */
function pageSource(source) {
  const start = source.indexOf(PAGE_START);
  assert.ok(start >= 0, "src/main.cpp 里找不到 cfgPage()");
  const end = source.indexOf(PAGE_END, start);
  assert.ok(end > start, "src/main.cpp 里找不到 cfgPage() 的终点");
  return source.slice(start, end);
}

const CFG_PAGE = pageSource(SRC);

/** 从 src/main.cpp 抽出配网页的 dirty 脚本（拼好 C++ 的多段字符串字面量）。 */
function extractDirtyScript(source) {
  const openTag = "\"<script id='dirty'>";
  const closeTag = "\"})();</script>\"";
  const start = source.indexOf(openTag);
  assert.ok(start >= 0, "src/main.cpp 里找不到 <script id='dirty'> 的起始标记");
  const end = source.indexOf(closeTag, start);
  assert.ok(end > start, "src/main.cpp 里找不到 dirty 脚本的结束标记");
  const region = source.slice(start, end + closeTag.length);

  // C++ 注释可能夹在字面量之间，先丢掉（里面可能带引号）。
  const kept = region.split("\n").filter((line) => !line.trim().startsWith("//")).join("\n");
  const parts = [];
  const re = /"((?:[^"\\]|\\.)*)"/g;
  let m;
  while ((m = re.exec(kept)) !== null) parts.push(JSON.parse(`"${m[1]}"`));
  const js = parts.join("");

  const tag = "<script id='dirty'>";
  assert.ok(js.startsWith(`${tag}(function(){`), "抽出来的不是一段 IIFE");
  return js.slice(tag.length).replace(/<\/script>$/, "");
}

const SCRIPT = extractDirtyScript(SRC);

// --- 假的浏览器环境 --------------------------------------------------------

const FIELD_SPECS = {
  ssid: { type: "text", value: "home-net" },
  pwd: { type: "password", value: "hunter2" },
  pin0: { type: "number", value: "13" },
  pin1: { type: "number", value: "12" },
  pin2: { type: "number", value: "14" },
  pin3: { type: "number", value: "27" },
  pin4: { type: "number", value: "32" },
  pin5: { type: "number", value: "33" },
  pin6: { type: "number", value: "25" },
  pin7: { type: "number", value: "26" },
  pin8: { type: "number", value: "4" },
  layout: { type: "select-one", value: "1" },
  enc: { type: "select-one", value: "0" },
  sleep: { type: "checkbox", value: "1", checked: true },
  sleeptime: { type: "number", value: "5" },
};

function makeNode(extra = {}) {
  const listeners = new Map();
  return Object.assign(
    {
      textContent: "",
      hidden: true,
      value: "",
      type: "text",
      checked: false,
      addEventListener(type, fn) {
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(fn);
      },
      fire(type) {
        for (const fn of listeners.get(type) || []) fn({ type, target: this });
      },
    },
    extra,
  );
}

/**
 * 造一个配网页：字段值即「设备渲染进页面的值」，也就是脚本加载时的基线。
 * 表单上的 input / change 事件在真实浏览器里会冒泡到 form，这里直接打在
 * form 上（脚本就是监听 form 的）。
 */
function makePage(initial = {}) {
  const fields = new Map();
  for (const [name, spec] of Object.entries(FIELD_SPECS)) {
    const value = Object.prototype.hasOwnProperty.call(initial, name) ? initial[name] : spec.value;
    fields.set(
      name,
      makeNode({
        name,
        type: spec.type,
        value,
        checked: Object.prototype.hasOwnProperty.call(initial, name)
          ? !!initial[name]
          : !!spec.checked,
      }),
    );
  }

  const byId = new Map();
  for (const id of ["dirtyBar", "otherDirtyWifi", "otherDirtyKeys", "fWifi", "fKeys"]) {
    byId.set(id, makeNode({ id }));
  }

  const formOf = (name) => (name === "ssid" || name === "pwd" ? "fWifi" : "fKeys");
  const confirmCalls = [];
  const state = { answer: true };

  const document = {
    querySelector(sel) {
      const m = /^\[name=([^\]]+)\]$/.exec(sel);
      if (!m) return null;
      return fields.get(m[1]) || null;
    },
    getElementById(id) {
      return byId.get(id) || null;
    },
  };

  const sandbox = {
    document,
    console,
    confirm(msg) {
      confirmCalls.push(msg);
      return state.answer;
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SCRIPT, sandbox, { filename: "dirty.js" });

  return {
    bar: byId.get("dirtyBar"),
    noteWifi: byId.get("otherDirtyWifi"),
    noteKeys: byId.get("otherDirtyKeys"),
    confirmCalls,
    /** 改一个字段并触发 input（模拟用户输入）。 */
    type(name, value) {
      const el = fields.get(name);
      assert.ok(el, `页面里没有字段 ${name}`);
      el.value = String(value);
      if (el.type === "checkbox") el.checked = Boolean(value);
      byId.get(formOf(name)).fire("input");
    },
    /** 只抛一个 change（下拉切换走这条路径）。 */
    change(name) {
      byId.get(formOf(name)).fire("change");
    },
    submitWifi: () => sandbox.portalDirty.wifi(),
    submitKeys: () => sandbox.portalDirty.keys(),
    leave: () => sandbox.portalDirty.leave(),
    setAnswer(v) {
      state.answer = v;
    },
  };
}

/* ---------------- 状态与文案 ---------------- */

test("未改动时不显示任何提示，提交任一保存表单都不弹窗", () => {
  const page = makePage();
  assert.equal(page.bar.hidden, true, "刚加载不应显示 #dirtyBar");
  assert.equal(page.bar.textContent, "", "刚加载 #dirtyBar 应是空的");
  assert.equal(page.noteWifi.hidden, true, "WiFi 表单下不应有提示");
  assert.equal(page.noteKeys.hidden, true, "按键表单下不应有提示");
  assert.equal(page.submitWifi(), true, "没有跨表单修改时不能拦 WiFi 保存");
  assert.equal(page.submitKeys(), true, "没有跨表单修改时不能拦按键保存");
  assert.deepEqual(page.confirmCalls, [], "没有改动时不能弹确认");
});

test("只改编码器模式：顶部只说「按键接线」，WiFi 表单下出现点名提示", () => {
  const page = makePage();
  page.type("enc", "3");
  assert.equal(page.bar.hidden, false, "改了编码器模式就要显示 #dirtyBar");
  assert.equal(page.bar.textContent, "有未保存的修改：按键接线", "只列 dirty 的组");
  assert.equal(page.noteWifi.hidden, false, "WiFi 表单下要提示按键侧没保存");
  assert.match(page.noteWifi.textContent, /按键接线/, "提示必须点名按键接线");
  assert.match(page.noteWifi.textContent, /保存 WiFi 会丢弃/, "要说清保存 WiFi 会丢什么");
  assert.equal(page.noteKeys.hidden, true, "本表单自己没有跨表单提示");
});

test("只改 WiFi 名称：顶部只说 WiFi，按键表单下出现点名提示（对称）", () => {
  const page = makePage();
  page.type("ssid", "office-net");
  assert.equal(page.bar.textContent, "有未保存的修改：WiFi", "只改 WiFi 时不该提按键接线");
  assert.equal(page.noteKeys.hidden, false, "按键表单下要提示 WiFi 没保存");
  assert.match(page.noteKeys.textContent, /WiFi 名称 \/ 密码/, "提示必须点名 WiFi 名称/密码");
  assert.equal(page.noteWifi.hidden, true, "本表单自己没有跨表单提示");
});

test("两个表单都改过：顶部两个组都点名", () => {
  const page = makePage();
  page.type("ssid", "office-net");
  page.type("pin0", "15");
  assert.equal(page.bar.textContent, "有未保存的修改：WiFi / 按键接线");
  assert.equal(page.noteWifi.hidden, false);
  assert.equal(page.noteKeys.hidden, false);
});

test("改回原值后提示消失：按值比较，不是「动过就算脏」", () => {
  const page = makePage();
  page.type("pin0", "15");
  assert.equal(page.bar.hidden, false);
  page.type("pin0", "13");
  assert.equal(page.bar.hidden, true, "值恢复成基线后不能再报未保存");
  assert.equal(page.noteWifi.hidden, true);
});

test("保存后刷新页面（新基线）不误报", () => {
  const page = makePage({ pin0: "15" });
  assert.equal(page.bar.hidden, true, "保存返回的页面里引脚就是 15，不该显示未保存");
});

/* ---------------- 跨表单守卫 ---------------- */

test("按键侧有未保存改动时，WiFi 保存被拦下：取消后不提交、输入还在", () => {
  const page = makePage();
  page.type("pin0", "15");
  page.setAnswer(false);

  assert.equal(page.submitWifi(), false, "取消 confirm 必须阻止提交（返回 false）");
  assert.equal(page.confirmCalls.length, 1, "应该弹一次确认");
  assert.match(page.confirmCalls[0], /按键接线/, "确认文案必须点名按键接线");
  assert.match(page.confirmCalls[0], /会丢弃/, "确认文案必须说清会丢弃");
  assert.equal(page.bar.textContent, "有未保存的修改：按键接线", "取消后状态仍在");
});

test("WiFi 侧有未保存改动时，按键保存被拦下，文案点名 WiFi", () => {
  const page = makePage();
  page.type("pwd", "new-secret");
  page.setAnswer(false);

  assert.equal(page.submitKeys(), false);
  assert.equal(page.confirmCalls.length, 1);
  assert.match(page.confirmCalls[0], /WiFi 名称 \/ 密码/, "确认文案必须点名 WiFi 名称/密码");
});

test("确认后放行，且连点只弹一次（一次性标志）", () => {
  const page = makePage();
  page.type("pin0", "15");
  page.setAnswer(true);

  assert.equal(page.submitWifi(), true, "确认后应允许提交");
  assert.equal(page.confirmCalls.length, 1);
  assert.equal(page.submitWifi(), true, "重复提交不应再弹");
  assert.equal(page.confirmCalls.length, 1, "连点不能连弹两次");
});

test("本表单自己的改动不拦（它本来就要一起提交）", () => {
  const solo = makePage();
  solo.type("ssid", "office-net");
  solo.setAnswer(false);
  assert.equal(solo.submitWifi(), true, "只改 WiFi 自己时保存 WiFi 不该弹窗");
  assert.deepEqual(solo.confirmCalls, []);
});

/* ---------------- 退出确认 ---------------- */

test("没有未保存改动时，退出只问一句「退出配网并重启？」", () => {
  const page = makePage();
  page.setAnswer(true);
  assert.equal(page.leave(), true);
  assert.deepEqual(page.confirmCalls, ["退出配网并重启？"], "无 dirty 时不能带「会丢失」的尾巴");
});

test("有未保存改动时，退出文案点名丢失的内容", () => {
  const keys = makePage();
  keys.type("pin0", "15");
  keys.setAnswer(false);
  assert.equal(keys.leave(), false, "取消退出必须阻止提交");
  assert.equal(keys.confirmCalls.length, 1);
  assert.match(keys.confirmCalls[0], /^退出配网并重启？/, "主文案保持稳定");
  assert.match(keys.confirmCalls[0], /以下修改尚未保存，会丢失：按键接线/, "必须点名按键接线");

  const wifi = makePage();
  wifi.type("ssid", "office-net");
  wifi.setAnswer(true);
  assert.equal(wifi.leave(), true);
  assert.match(wifi.confirmCalls[0], /以下修改尚未保存，会丢失：WiFi/, "必须点名 WiFi");
});

/* ---------------- 12 键只读行 ---------------- */

test("12 键布局的第 10–12 键是只读行：没有 input，也不在 dirty 字段里", () => {
  const at = CFG_PAGE.indexOf("第 10–12 键");
  assert.ok(at >= 0, "找不到 12 键固定行的文案");
  const row = CFG_PAGE.slice(at, CFG_PAGE.indexOf("</tr>", at));
  assert.ok(!/<input/.test(row), "固定行不能有可编辑的 input，否则会被当成未保存");
  assert.ok(!/name=/.test(row), "固定行不能带表单字段名");

  // 脚本的 keys 组只认 pin0..pin8，没有 pin9/10/11。
  const m = /keys:\[([^\]]*)\]/.exec(SCRIPT);
  assert.ok(m, "dirty 脚本里找不到 keys 字段组");
  const keys = m[1].split(",").map((s) => s.trim().replace(/^'|'$/g, "")).filter(Boolean);
  assert.deepEqual(
    keys,
    ["pin0", "pin1", "pin2", "pin3", "pin4", "pin5", "pin6", "pin7", "pin8",
      "layout", "enc", "sleep", "sleeptime"],
    "keys 组字段清单变了：多/少一个都会改变 dirty 判定",
  );
  assert.ok(!keys.some((k) => /^pin(9|10|11)$/.test(k)), "第 10–12 键没有可编辑字段");

  // 12 键布局下页面加载（layout=0）不脏；只切布局才算脏。
  const page = makePage({ layout: "0" });
  assert.equal(page.bar.hidden, true, "12 键布局加载后不该报未保存");
  page.type("layout", "1");
  assert.equal(page.bar.textContent, "有未保存的修改：按键接线", "切布局要按值判定为 dirty");
});

/* ---------------- 不动既有协议 ---------------- */

test("字段名、method、action 与退出路由保持原样", () => {
  assert.match(CFG_PAGE, /action='\/save'/, "WiFi 表单 action 变了");
  assert.match(CFG_PAGE, /action='\/keys'/, "按键表单 action 变了");
  assert.match(CFG_PAGE, /action='\/exit'/, "退出表单 action 变了");
  assert.match(CFG_PAGE, /method='POST' action='\/save'/, "WiFi 表单必须是 POST");
  assert.match(CFG_PAGE, /method='POST' action='\/keys'/, "按键表单必须是 POST");
  assert.match(CFG_PAGE, /method='POST' action='\/exit'/, "退出表单必须是 POST");
  assert.match(CFG_PAGE, /name='ssid'/, "WiFi 名称字段名变了");
  assert.match(CFG_PAGE, /name='pwd'/, "WiFi 密码字段名变了");
  // 引脚名由循环拼出（name='pin0'..'pin8'），源码里是 name='pin<N>，所以断言生成式。
  assert.match(CFG_PAGE, /name='pin" \+ String\(i\)/, "引脚字段名变了（应为 pin0..pin8）");
  assert.match(CFG_PAGE, /i < NUM_BUTTONS/, "引脚行必须仍按 NUM_BUTTONS 生成");
  assert.match(CFG_PAGE, /name='layout'/, "布局字段名变了");
  assert.match(CFG_PAGE, /name='enc'/, "编码器模式字段名变了");
  assert.match(CFG_PAGE, /name='sleep'/, "休眠字段名变了");
  assert.match(CFG_PAGE, /name='sleeptime'/, "休眠时长字段名变了");
});

test("两个保存表单与退出表单的 onsubmit 都指向 dirty 守卫，且没有引入 localStorage", () => {
  assert.match(CFG_PAGE, /onsubmit='return portalDirty\.wifi\(\)'/, "WiFi 表单缺少守卫");
  assert.match(CFG_PAGE, /onsubmit='return portalDirty\.keys\(\)'/, "按键表单缺少守卫");
  assert.match(CFG_PAGE, /onsubmit='return portalDirty\.leave\(\)'/, "退出表单缺少动态确认");
  assert.ok(!/localStorage/.test(SCRIPT), "不要做 localStorage 草稿（同一热点地址会串配置）");
  assert.ok(!/setInterval/.test(SCRIPT), "dirty 跟踪不需要轮询");
  assert.ok(!/fetch|XMLHttpRequest/.test(SCRIPT), "dirty 跟踪必须纯前端，不发请求");
});
