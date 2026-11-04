// WS-519 配网页 OTA 进度与结果卡的前端回归用例。
//
// 运行：node --test host/test_portal_ota_progress.mjs
//
// 页面脚本内联在 src/main.cpp 的 /update GET 处理函数里（配网页必须保持单
// 响应、无外部资源），所以这里从生产源文件里**原样抽取**
// <script id='ota-progress'> 那一段，在假的 DOM / XHR / 定时器上真跑，而不是
// 在这份测试里复制一份进度逻辑：改 src/main.cpp 里的脚本会让这些断言失败。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC_PATH = new URL("../src/main.cpp", import.meta.url);
const SOURCE = readFileSync(SRC_PATH, "utf8");
const OPEN_TAG = "\"<script id='ota-progress'>\"";
const CLOSE_TAG = "\"</script>\"";

/** 从 src/main.cpp 抽出内联 OTA 脚本（拼好 C++ 的多段字符串字面量）。 */
function extractOtaScript(source) {
  const start = source.indexOf(OPEN_TAG);
  assert.ok(start >= 0, "src/main.cpp 里找不到 <script id='ota-progress'> 起始标记");
  const end = source.indexOf(CLOSE_TAG, start + OPEN_TAG.length);
  assert.ok(end > start, "src/main.cpp 里找不到 OTA 脚本的结束标记");

  const region = source.slice(start + OPEN_TAG.length, end);
  const parts = [];
  const re = /"((?:[^"\\]|\\.)*)"/g;
  let m;
  while ((m = re.exec(region)) !== null) {
    parts.push(JSON.parse(`"${m[1]}"`));
  }
  assert.ok(parts.length > 0, "OTA 脚本区域里没有任何字符串字面量");
  return parts.join("");
}

const SCRIPT = extractOtaScript(SOURCE);

// --- 假的浏览器环境 --------------------------------------------------------
function makeElement(id) {
  const classes = new Set();
  const listeners = new Map();
  const el = {
    id,
    textContent: "",
    hidden: false,
    disabled: false,
    files: [],
    style: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      _all: () => [...classes],
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    fire(type, event) {
      for (const fn of listeners.get(type) || []) fn(event || {});
    },
    _listenerCount: (type) => (listeners.get(type) || []).length,
  };
  // 与浏览器一致：把 file input 的 value 置空会一并清掉已选文件。
  let value = "";
  Object.defineProperty(el, "value", {
    get: () => value,
    set: (v) => {
      value = String(v);
      if (value === "") el.files = [];
    },
  });
  return el;
}

function makeClock() {
  let now = 0;
  let nextId = 1;
  const pending = new Map();
  const add = (fn, ms, repeat) => {
    const id = nextId++;
    pending.set(id, { at: now + (ms || 0), fn, repeat: repeat ? (ms || 1) : 0 });
    return id;
  };
  return {
    setTimeout: (fn, ms) => add(fn, ms, false),
    setInterval: (fn, ms) => add(fn, ms, true),
    clearTimeout(id) {
      pending.delete(id);
    },
    clearInterval(id) {
      pending.delete(id);
    },
    advance(ms) {
      const target = now + ms;
      for (;;) {
        let due = null;
        for (const [id, item] of pending) {
          if (item.at <= target && (!due || item.at < due.item.at)) due = { id, item };
        }
        if (!due) break;
        if (due.item.repeat) due.item.at += due.item.repeat;
        else pending.delete(due.id);
        now = due.item.at;
        due.item.fn();
      }
      now = target;
    },
    get pendingCount() {
      return pending.size;
    },
  };
}

function makeXhr(instances) {
  const headers = {};
  const xhr = {
    upload: {},
    status: 0,
    readyState: 0,
    responseText: "",
    timeout: 0,
    method: null,
    url: null,
    body: null,
    sentHeaders: {},
    open(method, url) {
      this.method = method;
      this.url = url;
      this.readyState = 1;
    },
    setRequestHeader(k, v) {
      this.sentHeaders[k] = v;
    },
    getResponseHeader(k) {
      const v = headers[String(k).toLowerCase()];
      return v === undefined ? null : v;
    },
    send(body) {
      this.body = body;
      instances.push(this);
    },
    // --- 测试驱动 ---
    progress(loaded, total) {
      assert.ok(this.upload.onprogress, "脚本没有挂 upload.onprogress");
      this.upload.onprogress({ lengthComputable: true, loaded, total });
    },
    finishUpload() {
      if (this.upload.onload) this.upload.onload();
    },
    respond({ status = 200, text = "", headers: h = {} } = {}) {
      this.status = status;
      this.readyState = 4;
      this.responseText = text;
      for (const k of Object.keys(h)) headers[k.toLowerCase()] = h[k];
      if (this.onload) this.onload();
    },
    fail(kind = "error") {
      this.status = 0;
      this.readyState = 4;
      const fn = this["on" + kind];
      if (fn) fn({ type: kind });
    },
  };
  return xhr;
}

const flush = async () => {
  for (let i = 0; i < 8; i++) await Promise.resolve();
};

const IDS = [
  "otaForm", "fw", "otaGo", "fwMeta", "otaProg", "otaBar", "otaStat",
  "otaResult", "otaTitle", "otaLine1", "otaLine2", "otaCode", "otaRetry",
  "st1", "st2", "st3", "st4",
];

function openPage() {
  const els = new Map();
  const xhrs = [];
  const clock = makeClock();
  const document = {
    getElementById(id) {
      if (!els.has(id)) els.set(id, makeElement(id));
      return els.get(id);
    },
  };
  class FakeXhr {
    constructor() {
      return makeXhr(xhrs);
    }
  }
  class FakeFormData {
    constructor(form) {
      this.form = form;
    }
  }
  const run = new Function(
    "document", "XMLHttpRequest", "FormData", "setInterval", "clearInterval",
    SCRIPT,
  );
  // 初始文案与 hidden 由 HTML 提供（脚本只在交互后改写），这里照页面初始状态铺好。
  document.getElementById("otaGo").textContent = "开始升级";
  for (const id of ["fwMeta", "otaProg", "otaResult"]) {
    document.getElementById(id).hidden = true;
  }
  run(document, FakeXhr, FakeFormData, clock.setInterval, clock.clearInterval);
  const el = (id) => document.getElementById(id);
  return { els, xhrs, clock, el, document };
}

/** 结果卡上所有可见文案，用于「页面任何位置都不出现某句话」的断言。 */
function visibleText(page) {
  return IDS.map((id) => page.el(id).textContent || "").join("\n");
}

function pickFile(page, name = "ota_binddecklite_c3_v1.3.2.bin", size = 3984588) {
  const fw = page.el("fw");
  fw.files = [{ name, size }];
  fw.fire("change");
}

function submit(page) {
  const before = page.xhrs.length;
  let prevented = false;
  page.el("otaForm").fire("submit", {
    preventDefault() {
      prevented = true;
    },
  });
  assert.equal(prevented, true, "submit 必须被拦截（不能整页 POST）");
  assert.equal(page.xhrs.length, before + 1, "每次提交应当发出且只发出一个 XHR");
  return page.xhrs[page.xhrs.length - 1];
}

// --- 用例 ------------------------------------------------------------------

test("页面骨架：未选文件时「开始升级」disabled，进度区与结果区隐藏", () => {
  const page = openPage();
  const go = page.el("otaGo");

  assert.equal(go.disabled, true);
  assert.equal(go.textContent, "开始升级");
  assert.equal(page.el("otaProg").hidden, true);
  assert.equal(page.el("otaResult").hidden, true);
  assert.equal(page.el("st1").classList.contains("on"), true);
  assert.equal(page.el("st2").classList.contains("on"), false);
  // 但 HTML 里不能硬写 disabled：脚本没跑起来（关掉 JS）时，
  // 页面必须还能像本轮之前一样直接 POST。
  assert.match(SOURCE, /id='otaGo' type='submit'>开始升级/, "otaGo 的初始文案由 HTML 提供");
  assert.equal(
    /id='otaGo'[^>]*disabled/.test(SOURCE),
    false,
    "HTML 不能硬禁用 otaGo，否则非 JS 兜底无法提交",
  );
  for (const id of ["fwMeta", "otaProg", "otaResult"]) {
    assert.match(
      SOURCE,
      new RegExp(`id='${id}' hidden`),
      id + " 必须在 HTML 里就 hidden（脚本没跑起来也不显示）",
    );
  }
});

test("选中固件后显示「文件名 · X.X MB」并放开按钮", () => {
  const page = openPage();
  pickFile(page, "ota_c3.bin", 3984588);

  const meta = page.el("fwMeta");
  assert.equal(meta.hidden, false);
  assert.equal(meta.textContent, "ota_c3.bin · 3.8 MB");
  assert.equal(page.el("otaGo").disabled, false);
});

test("上传 0% / 50% / 100%：文案与阶段条都跟着走", () => {
  const page = openPage();
  pickFile(page, "ota_c3.bin", 4194304); // 4.0 MB，便于断言
  const xhr = submit(page);

  assert.equal(xhr.method, "POST");
  assert.equal(xhr.url, "/update");
  assert.equal(page.el("otaProg").hidden, false);
  assert.equal(page.el("otaResult").hidden, true);
  assert.equal(page.el("st2").classList.contains("on"), true);
  assert.equal(page.el("fw").disabled, true);
  assert.equal(page.el("otaGo").disabled, true);
  assert.equal(page.el("otaGo").textContent, "升级中…");
  assert.equal(page.el("otaStat").textContent, "已传 0.0 / 4.0 MB · 0%");

  xhr.progress(2097152, 4194304);
  assert.equal(page.el("otaStat").textContent, "已传 2.0 / 4.0 MB · 50%");
  assert.equal(page.el("otaBar").style.width, "50%");
  assert.equal(page.el("st2").classList.contains("on"), true);

  xhr.progress(4194304, 4194304);
  assert.equal(page.el("otaStat").textContent, "已传 4.0 / 4.0 MB · 100%");
  assert.equal(page.el("otaBar").style.width, "100%");
  assert.equal(page.el("st3").classList.contains("on"), false, "100% 之前还没进写入阶段");
});

test("上传 100% 后切「写入与校验」：不确定态 + 每秒「已等待 N 秒」", () => {
  const page = openPage();
  pickFile(page, "ota_c3.bin", 4194304);
  const xhr = submit(page);

  xhr.progress(4194304, 4194304);
  xhr.finishUpload();

  assert.equal(page.el("st3").classList.contains("on"), true);
  assert.equal(page.el("otaBar").classList.contains("indet"), true);
  assert.match(page.el("otaStat").textContent, /设备正在写入与校验/);
  assert.match(page.el("otaStat").textContent, /已等待 0 秒/);
  assert.equal(page.el("otaGo").disabled, true);

  page.clock.advance(2000);
  assert.match(page.el("otaStat").textContent, /已等待 2 秒/);
  // 进度条仍在，结果卡还没出现
  assert.equal(page.el("otaProg").hidden, false);
  assert.equal(page.el("otaResult").hidden, true);
});

test("X-Ota-Result: ok → 绿卡「升级完成」并隐藏进度区", () => {
  const page = openPage();
  pickFile(page, "ota_c3.bin", 4194304);
  const xhr = submit(page);
  xhr.progress(4194304, 4194304);
  xhr.finishUpload();
  page.clock.advance(3000);

  xhr.respond({ status: 200, text: "<p>升级完成，设备重启中…</p>", headers: { "X-Ota-Result": "ok" } });

  const card = page.el("otaResult");
  assert.equal(card.hidden, false);
  assert.equal(card.classList.contains("ok"), true);
  assert.equal(page.el("otaTitle").textContent, "升级完成，设备正在重启");
  assert.match(page.el("otaLine1").textContent, /约 10 秒/);
  assert.match(page.el("otaLine1").textContent, /蓝牙模式/);
  assert.equal(page.el("otaProg").hidden, true);
  assert.equal(page.el("st4").classList.contains("on"), true);
  assert.equal(page.clock.pendingCount, 0, "结果出来后不能再有别在跑的等待计时器");
});

test("X-Ota-Result: failed + read_error → 红卡同时有中文原因与原始 code", () => {
  const page = openPage();
  pickFile(page, "ota_c3.bin", 4194304);
  const xhr = submit(page);
  xhr.progress(4194304, 4194304);
  xhr.finishUpload();
  xhr.respond({
    status: 200,
    text: "<p>升级失败，设备重启中…</p>",
    headers: { "X-Ota-Result": "failed", "X-Ota-Code": "read_error" },
  });

  const card = page.el("otaResult");
  assert.equal(card.classList.contains("bad"), true);
  assert.match(page.el("otaTitle").textContent, /升级失败/);
  assert.match(page.el("otaLine1").textContent, /回读不一致/);
  assert.match(page.el("otaCode").textContent, /read_error/);
  assert.equal(page.el("otaProg").hidden, true);
});

test("失败码映射：flash_too_small 提示 merged 整片包，未知码保留原文", () => {
  const page = openPage();
  pickFile(page);
  const xhr = submit(page);
  xhr.finishUpload();
  xhr.respond({
    headers: { "X-Ota-Result": "failed", "X-Ota-Code": "flash_too_small" },
  });
  assert.match(page.el("otaLine1").textContent, /merged 整片包/);
  assert.match(page.el("otaCode").textContent, /flash_too_small/);

  const page2 = openPage();
  pickFile(page2);
  const xhr2 = submit(page2);
  xhr2.finishUpload();
  xhr2.respond({
    headers: { "X-Ota-Result": "failed", "X-Ota-Code": "weird_code" },
  });
  assert.match(page2.el("otaLine1").textContent, /未知原因（weird_code）/);
  assert.match(page2.el("otaCode").textContent, /weird_code/);
});

test("响应头缺失时退回按正文判定，并在卡上标注判定来源", () => {
  const page = openPage();
  pickFile(page);
  const xhr = submit(page);
  xhr.finishUpload();
  xhr.respond({ text: "<p>升级完成，设备重启中…</p>" });

  assert.equal(page.el("otaResult").classList.contains("ok"), true);
  assert.match(page.el("otaLine2").textContent, /判定来源：响应正文/);
});

test("onerror → 琥珀卡「没收到设备的结果」，页面任何位置不出现「升级失败」", () => {
  const page = openPage();
  pickFile(page);
  const xhr = submit(page);
  xhr.progress(4194304, 4194304);
  xhr.finishUpload();
  xhr.fail("error");

  const card = page.el("otaResult");
  assert.equal(card.classList.contains("warn"), true);
  assert.equal(card.classList.contains("bad"), false);
  assert.match(page.el("otaLine1").textContent, /没收到设备的结果/);
  assert.match(page.el("otaLine1").textContent, /正在重启/);
  assert.equal(visibleText(page).includes("升级失败"), false);
  assert.equal(page.clock.pendingCount, 0, "断连后等待计时器必须停掉");
});

test("ontimeout 同样走琥珀卡，不写成失败", () => {
  const page = openPage();
  pickFile(page);
  const xhr = submit(page);
  xhr.fail("timeout");

  assert.equal(page.el("otaResult").classList.contains("warn"), true);
  assert.equal(visibleText(page).includes("升级失败"), false);
});

test("「重新选择固件」清空 input、隐藏进度与结果并回阶段 1", () => {
  const page = openPage();
  pickFile(page, "ota_c3.bin", 4194304);
  const xhr = submit(page);
  xhr.progress(4194304, 4194304);
  xhr.finishUpload();
  xhr.respond({ headers: { "X-Ota-Result": "failed", "X-Ota-Code": "write_error" } });
  assert.equal(page.el("otaResult").hidden, false);

  page.el("otaRetry").fire("click");

  assert.equal(page.el("otaResult").hidden, true);
  assert.equal(page.el("otaProg").hidden, true);
  assert.equal(page.el("fw").value, "");
  assert.deepEqual(page.el("fw").files, []);
  assert.equal(page.el("fw").disabled, false);
  assert.equal(page.el("otaGo").disabled, true, "没有文件时按钮重新禁用");
  assert.equal(page.el("otaGo").textContent, "开始升级");
  assert.equal(page.el("fwMeta").hidden, true);
  assert.equal(page.el("st1").classList.contains("on"), true);
  for (const id of ["st2", "st3", "st4"]) {
    assert.equal(page.el(id).classList.contains("on"), false, id + " 应回到未激活");
  }
  assert.equal(page.clock.pendingCount, 0);
  // 复位后能立刻再选一次并重新提交
  pickFile(page, "ota_c3_retry.bin", 1024);
  assert.equal(page.el("otaGo").disabled, false);
  submit(page);
  assert.equal(page.xhrs.length, 2);
});

test("非 JS 兜底：form 的 method/action/enctype 与字段名保持不变", () => {
  assert.match(SOURCE, /<form method='POST' action='\/update' enctype='multipart\/form-data' id='otaForm'>/);
  assert.match(SOURCE, /name='firmware'/);
});
