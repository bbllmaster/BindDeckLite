// WS-706: 「掉线时发送系统通知」在托盘可用时必须真的能点到。
//
// 断言的是生产 renderDesktop()。模板 index.html 给整块写了 hidden；
// 只把 style.display 清空并不会撤掉 hidden，浏览器仍按 UA 的
// [hidden]{display:none} 把整块藏起来（评审用 headless Chromium 复现过）。
// 所以这里断言 properties.hidden 本身被翻成 false，而不是看 style。
import test from "node:test";
import assert from "node:assert/strict";
import { loadApp } from "./app_harness.mjs";

const desktopPayload = (over = {}) => ({
  platform: "linux",
  frozen: false,
  desktop_mode: true,
  tray_available: true,
  tray_active: true,
  window_hidden: true,
  notify_on_disconnect: true,
  autostart: { supported: false, enabled: false, admin: false },
  recent_paths: [],
  ...over,
});

test("托盘可用时掉线通知整行可见", () => {
  const { host, el } = loadApp();
  const block = el("#notifyDisconnectBlock");
  block.hidden = true; // 模板里的初始状态：<div id="notifyDisconnectBlock" hidden>
  host.state.desktop = desktopPayload({ tray_active: true });
  host.renderDesktop();

  assert.equal(block.hidden, false, "hidden 属性必须被清掉，否则整块仍然 display:none");
  assert.equal(el("#notifyDisconnectToggle").checked, true);
});

test("浏览器模式 / 托盘不可用时整行隐藏", () => {
  const { host, el } = loadApp();
  const block = el("#notifyDisconnectBlock");
  block.hidden = false;
  host.state.desktop = desktopPayload({ tray_active: false, desktop_mode: false });
  host.renderDesktop();

  assert.equal(block.hidden, true);
});

test("旧配置缺 notify_on_disconnect 时复选框按默认开", () => {
  const { host, el } = loadApp();
  const payload = desktopPayload({ tray_active: true });
  delete payload.notify_on_disconnect;
  host.state.desktop = payload;
  host.renderDesktop();

  assert.equal(el("#notifyDisconnectToggle").checked, true);
});
