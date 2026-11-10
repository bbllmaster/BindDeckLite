// WS-561 重连自动对账的判定回归用例。
//
// 运行：cd host && node --test test_reconnect_sync.mjs
//
// 断言对象是 host/static/reconnect_sync.js 里的**生产函数**（浏览器里的
// app.js 调的就是 window.ReconnectSync 这一份），不在这里复制判定逻辑 ——
// 放宽触发条件、改动字段顺序或破坏拼接都会让这些用例真的失败。
//
// 配置夹具按后端 new_device_cfg() / device_cfg() 的**真实形状**构造：编码器
// 模式在 `encoder.mode`，配置里没有 `enc` 键（`enc` 只是 CFG/回读的组名）。
// 以前这里喂 `{enc: {mode: 0}}` 这种生产不会出现的形状，等于测试自证 ——
// 生产按 `encoder.mode` 取值时它照样全绿。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import Sync from "./static/reconnect_sync.js";

const { STABLE_MS, LABEL_KEYS, encoderMode, shouldReconcile, changedFields, syncSummary } = Sync;

/** 全部条件满足的一组输入；各用例只覆盖自己关心的那一项。 */
const ready = {
  trusted: true,
  pendingCount: 0,
  otaBusy: false,
  connectedForMs: STABLE_MS,
  reconciledThisCycle: false,
};

/** 后端 new_device_cfg() 的形状（ESP32 默认引脚、12 条按键、编码器、休眠）。 */
function realCfg() {
  return {
    name: "工作的板子",
    chip: "ESP32",
    trusted: true,
    layout: 0,
    pins: [13, 12, 14, 27, 32, 33, 25, 26, 4],
    keys: Array.from({ length: 12 }, (_, i) => ({
      code: 240 + i,
      label: `F${13 + i}`,
      action: { type: "none", value: "", macro: [] },
    })),
    encoder: {
      mode: 0,
      // WS-389：旋转动作只在主机执行，从不下发，不参与对账比较。
      actions: {
        CW: { type: "none", value: "", macro: [] },
        CCW: { type: "none", value: "", macro: [] },
      },
    },
    sleep: { enabled: true, minutes: 5 },
    tz: "CET-1CEST,M3.5.0,M10.5.0/3",
  };
}

/** 真实语言文件：日志标签与顿号都从 i18n 取，不在这里写死中文。 */
const zh = JSON.parse(
  readFileSync(new URL("./locales/zh-CN.json", import.meta.url), "utf8")
);

test("所有条件满足 → 允许对账", () => {
  const v = shouldReconcile(ready);
  assert.equal(v.ok, true);
  assert.equal(v.reason, "ok");
});

test("本机有未下发修改 → 不请求，原因 pending", () => {
  const v = shouldReconcile({ ...ready, pendingCount: 2 });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "pending");
});

test("OTA 进行中 → 不请求", () => {
  const v = shouldReconcile({ ...ready, otaBusy: true });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "ota");
});

test("连接不足 2 秒 → 不请求，等稳定窗口", () => {
  const v = shouldReconcile({ ...ready, connectedForMs: STABLE_MS - 1 });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "unstable");
});

test("本连接周期已对账 → 不重复请求（掉线重连由调用方重置该标记）", () => {
  const v = shouldReconcile({ ...ready, reconciledThisCycle: true });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "reconciled");
});

test("本机无存档（trusted=false）→ 不自动读，走 WS-512 的显式确认", () => {
  const v = shouldReconcile({ ...ready, trusted: false });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "untrusted");
});

test("未传 trusted（老存档 undefined）也不当作可信", () => {
  const v = shouldReconcile({ ...ready, trusted: undefined });
  assert.equal(v.ok, false);
  assert.equal(v.reason, "untrusted");
});

test("夹具是生产形状：没有 enc 键，编码器模式在 encoder.mode", () => {
  const cfg = realCfg();
  assert.equal("enc" in cfg, false);
  assert.equal(cfg.encoder.mode, 0);
  assert.equal(encoderMode(cfg), 0);
});

test("差异比较只认四组配置，字段顺序固定为 pins/keys/enc/sleep", () => {
  const before = realCfg();
  const after = realCfg();
  after.pins = [15, 12, 14, 27, 32, 33, 25, 26, 4]; // 引脚改了
  after.encoder.mode = 2;                            // 编码器模式改了
  after.name = "新名字";                             // 不属于对账字段，不算差异
  assert.deepEqual(changedFields(before, after), ["pins", "enc"]);
});

test("仅 encoder.mode 变化 → 返回 ['enc']（手机把音量改成标签页）", () => {
  const before = realCfg();
  const after = realCfg();
  after.encoder.mode = 3;
  assert.deepEqual(changedFields(before, after), ["enc"]);
});

test("只改本机 encoder.actions → 不算差异（纯上位机字段，从不下发）", () => {
  const before = realCfg();
  const after = realCfg();
  after.encoder.actions.CW = { type: "media", value: "play_pause", macro: [] };
  assert.deepEqual(changedFields(before, after), [], "整对象比较会把旋转动作误判成待下发");
});

test("encoderMode：缺字段 / 只有 actions 时按 0（仅 HID）处理", () => {
  assert.equal(encoderMode({}), 0);
  assert.equal(encoderMode({ encoder: { actions: {} } }), 0);
  assert.equal(encoderMode({ encoder: { mode: 2 } }), 2);
  assert.equal(encoderMode(null), 0);
});

test("完全一致 → 无差异，日志字段串为空（调用方据此不记日志）", () => {
  const cfg = realCfg();
  assert.deepEqual(changedFields(cfg, JSON.parse(JSON.stringify(cfg))), []);
  assert.equal(syncSummary(cfg, cfg, (f) => zh[LABEL_KEYS[f]], zh["ui.list_comma"]), "");
});

test("差异日志字段拼接：引脚 + 编码器模式，顺序与顿号来自生产视图", () => {
  const before = realCfg();
  const after = realCfg();
  after.pins = [15, 12, 14, 27, 32, 33, 25, 26, 4];
  after.encoder.mode = 2;
  assert.equal(
    syncSummary(before, after, (f) => zh[LABEL_KEYS[f]], zh["ui.list_comma"]),
    "按键引脚、编码器模式"
  );
});

test("只改编码器模式时日志只列「编码器模式」（验收：已按设备同步：编码器模式）", () => {
  const before = realCfg();
  const after = realCfg();
  after.encoder.mode = 2;
  const fields = syncSummary(before, after, (f) => zh[LABEL_KEYS[f]], zh["ui.list_comma"]);
  assert.equal(fields, "编码器模式");
  assert.equal(
    zh["ui.reconnect_synced"].replace("{fields}", fields),
    "已按设备同步：编码器模式"
  );
});
