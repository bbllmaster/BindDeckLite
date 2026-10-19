# Changelog

## v1.0.0 — 2026-10-19

首个 BindDeckLite 版本，基于 BindDeck `feature/sh1106-support`（提交 `2dca405`）。

### 固件
- 去掉 OLED 与全部显示相关代码（Adafruit GFX/SSD1306/SH110X、RoboEyes、I2C）。
- 去掉独立菜单键；配网入口改为「上电按住编码器」或「运行中长按编码器 5 秒 / 上位机指令」。
- 9 个按键 + EC11（旋转 + 按压）：`BTN:<devId>:0..8`，编码器按压为 `9`。
- 编码器 5 种模式（音量 / 缩放 / 标签页 / 撤销重做 / 应用音量）。
- 目标缩减为 `esp32dev` 与 `esp32c3` 两个 PlatformIO 环境。
- 保留 NimBLE HID、唯一设备名 / Serial / Model / PID / IRK（复用 BindDeck 实现）。
- 保留 BLE CFG/CMD 服务、WiFi 配网门户、OTA、NVS 配置、深度睡眠。
- 新增 `CMD:GET_CFG`、`CMD:PING`、`CMD:BOOT_CFG`，去掉所有 OLED 相关命令（动画、亮度、预览、消息）。
- C3 引脚：去掉 OLED 后 9 键 + 编码器正好占用 12 个可用 GPIO。

### 上位机
- 全新控制台（Flask + 原生 JS），替代原 BindDeck 的 OLED 中心界面。
- 设备列表 / 自动连接 / 多设备切换。
- 10 个输入独立配置 HID 键码与上位机动作（启动程序、网址、命令、媒体键、宏）。
- 编码器模式、9 个按键引脚、休眠配置，一键下发并读取设备当前配置。
- 实时按键高亮、事件日志、Ping、重启、进入配网模式。

### 工具
- `tools/build_release.py`：一键构建 merged（整片）+ OTA（app-only）发布包，正确处理经典 ESP32 的 `otadata@0xd000`。
