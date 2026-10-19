# BindDeckLite

BindDeckLite 是 [BindDeck](https://github.com/bbllmaster/BindDeck) 的精简版本：**去掉 OLED、去掉菜单键，9 个按键 + EC11 旋转编码器，支持 ESP32 与 ESP32-C3**，并重新设计了上位机。

固件与上位机共用同一套 `CFG:` / `CMD:` 行协议，按键既可以只发 HID（设备独立工作），也可以叠加运行上位机动作（宏、启动程序、执行命令）。

## 与 BindDeck 的差异

| 项目 | BindDeck | BindDeckLite |
|------|----------|--------------|
| 显示屏 | SSD1306 / SH1106 / SH1107 | **无**（无 I2C、无显示库） |
| 菜单键 | 独立 GPIO4 | **取消**（编码器长按替代） |
| 按键 | 8 个 + 编码器按压 | **9 个 + 编码器按压** |
| 编码器 | EC11（旋转 + 按压） | EC11（旋转 + 按压） |
| 芯片 | ESP32 / ESP32-C3 | ESP32 / ESP32-C3 |
| 配置方式 | 上位机 / OLED 菜单 / 配网页面 | **上位机 / 配网页面** |
| 固件环境 | 5 个（含 3 种屏幕） | **2 个**（`esp32dev` / `esp32c3`） |

## 硬件

### 默认引脚

| 功能 | ESP32（经典） | ESP32-C3 |
|------|---------------|----------|
| 按键 1–9 | 13, 12, 14, 27, 32, 33, 25, 26, 4 | 0, 2, 3, 4, 6, 7, 8, 9, 10 |
| EC11 CLK (A) | 18 | 20 |
| EC11 DT (B) | 19 | 21 |
| EC11 按压 (SW) | 5 | 1 |

- 按键接 **GPIO 与 GND**，固件启用内部上拉，按下为低电平。
- EC11 的 A/B 两相各接一个 GPIO 与 GND，公共端接 GND；按压开关一端接 SW，一端接 GND。
- 引脚可在上位机「设备 → 按键引脚」里改成实际接线，应用后写入 NVS。
- 经典 ESP32 避开 **GPIO6–11**（SPI Flash）与 **GPIO34–39**（仅输入、无上拉）。
- ESP32-C3 避开 **GPIO11–17**（内置 Flash）与 **GPIO18/19**（USB-CDC）；可用引脚只有 0–10、20、21。去掉 OLED 后正好容下 9 键 + 3 个编码器引脚。
- ESP32-C3 深度睡眠只能被 **GPIO0–5** 上的按键唤醒；其他按键需要重新上电。

### 输入编号

| 编号（`BTN:` 消息） | 输入 |
|---------------------|------|
| 0–8 | 按键 1–9 |
| 9 | 编码器按压 |

默认键码：按键 1–9 = `F13`–`F21`，编码器按压 = `F22`。

## 固件

依赖 [PlatformIO](https://platformio.org/)。

```bash
pio run -e esp32dev          # 经典 ESP32
pio run -e esp32c3           # ESP32-C3
pio run -e esp32dev -t upload
```

### 发布包

```bash
python tools/build_release.py
```

输出到 `dist/`：

| 文件 | 用途 |
|------|------|
| `merged_binddecklite_esp32_vX.Y.Z.bin` | 经典 ESP32 整片烧录，从 `0x0` 写入 |
| `merged_binddecklite_c3_vX.Y.Z.bin` | C3 整片烧录，从 `0x0` 写入 |
| `ota_binddecklite_esp32_vX.Y.Z.bin` | 经典 ESP32 OTA / app-only |
| `ota_binddecklite_c3_vX.Y.Z.bin` | C3 OTA / app-only |

```bash
esptool.py --chip esp32   write_flash 0x0 merged_binddecklite_esp32_v1.0.0.bin
esptool.py --chip esp32c3 write_flash 0x0 merged_binddecklite_c3_v1.0.0.bin
```

### 配网 / OTA 模式

没有菜单键，进入配网模式有两种方式：

1. **上电时按住编码器按压键**约 0.2 秒；
2. 正常使用时**长按编码器按压键 5 秒**（设备会重启进入配网），或在上位机点「进入配网模式」。

进入后设备开启热点 `BindDeckLite-XXXX`（开放网络），浏览器打开 <http://192.168.4.1>：

- 配置 WiFi（保存后可用 UDP 通道）
- 修改 9 个按键引脚、编码器模式、休眠
- **固件 OTA 升级**：只能上传 `ota_*.bin`（app-only），不要上传 merged 整片包

退出：长按编码器约 1 秒，或 5 分钟自动关闭 WiFi。BLE 在配网模式期间关闭（与热点共用 2.4G 射频），退出后自动恢复。

### 串口协议

经典 ESP32 的 USB 串口（115200）与 BLE CFG 服务、WiFi UDP 使用同一套行协议，详见 [docs/protocol.md](docs/protocol.md)。

## 上位机

`host/` 是重新设计的控制台（Flask + 原生 JS，无构建步骤）：

- 自动发现并连接所有 `BindDeckLite-XXXX`
- 10 个输入（9 键 + 编码器按压）的 HID 键码与上位机动作
- 编码器 5 种模式（音量 / 缩放 / 标签页 / 撤销重做 / 应用音量）
- 引脚、休眠配置，一键下发
- 实时按键高亮与日志、Ping、重启、进入配网

### 运行

```bash
cd host
python -m pip install -r requirements.txt
python app.py
```

浏览器自动打开 <http://127.0.0.1:8788>。

- Windows 宏动作需要 `pip install keyboard`；未安装时宏与媒体键不可用，其他功能不受影响。
- 设备同时只能被一个 BLE central 作为 HID 主机，配置通道是同一连接上的第二个 GATT 服务，不需要断开。
- 上位机配置保存在 `host/config.json`（运行时生成，不入库）。

### 上位机动作

每个按键可单独配置：

| 类型 | 说明 |
|------|------|
| 仅 HID 按键 | 设备自己发键，上位机不做任何事 |
| 启动程序 / 文件 | 本地路径，Windows 用 `os.startfile` |
| 打开网址 | 默认浏览器 |
| 执行命令 | `shell=True`，请只填可信命令 |
| 媒体键 | 音量 / 播放 / 上下曲（需 `keyboard`） |
| 宏 | 每行一步：`key:ctrl+alt+t`、`text:hello`、`wait:200` |

> 上位机动作默认开启，可在右上角关闭；关闭后设备仍然正常发 HID 键。

## 目录结构

```
├── platformio.ini
├── partitions/            # OTA 分区表（ESP32 / C3）
├── src/
│   ├── main.cpp           # Lite 固件：9 键 + EC11，无显示
│   ├── nimble_hid.*       # NimBLE HID + 唯一身份 / IRK（复用 BindDeck）
│   └── ble_config.*       # BLE CFG/CMD 服务（复用 BindDeck）
├── host/
│   ├── app.py             # 上位机后端
│   ├── ble_link.py        # BLE 扫描 / 多设备连接（复用 BindDeck）
│   ├── templates/index.html
│   └── static/{app.js,style.css}
├── tools/build_release.py # 构建 merged + OTA 发布包
└── docs/protocol.md
```

## 版本与发布

固件发布沿用 BindDeck 的做法：项目里单独有一个**滚动固件发布任务**，每个版本的 merged / OTA 包以附件形式追加在该任务下，源码提交与版本说明也记录在那里。

- 固件版本：`src/main.cpp` 的 `FW_VERSION`
- 发布流程：改版本号 → `python tools/build_release.py` → 提交代码 → 把 `dist/` 里的 4 个 bin 追加到发布任务

## 许可

与上游 BindDeck 保持一致。
