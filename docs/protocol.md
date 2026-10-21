# BindDeckLite 通信协议

设备与上位机之间是**一行一条命令**的纯文本协议，行尾 `\n`。三条通道完全等价：

| 通道 | 方向 | 说明 |
|------|------|------|
| BLE CFG 服务 | 双向 | 与 HID 同一个连接上的第二个 GATT 服务，默认使用 |
| WiFi UDP | 双向 | 仅配网模式；上位机 → 设备 `4210`，设备 → 上位机 `4211` |
| USB 串口 | 双向 | 经典 ESP32，115200 |

## BLE 服务

```
service  b1d3c0de-0001-4a5b-9c6d-1a2b3c4d5e6f
RX       b1d3c0de-0002-4a5b-9c6d-1a2b3c4d5e6f   write      上位机 -> 设备
TX       b1d3c0de-0003-4a5b-9c6d-1a2b3c4d5e6f   notify     设备 -> 上位机
```

RX / TX 需要已配对（加密）连接；上位机首次写入会自动配对。设备广播名 `BindDeckLite-XXXX`，`XXXX` 为 MAC 末两字节，同时也是 `devId`。

## 上位机 → 设备

| 命令 | 示例 | 说明 |
|------|------|------|
| `CFG:PINS:` | `CFG:PINS:13,12,14,27,32,33,25,26,4` | 9 个按键 GPIO；必须互不重复且合法，否则整条丢弃 |
| `CFG:KEYS:` | `CFG:KEYS:240,241,242,243,244,245,246,247,248,249` | 10 个 HID 键码：按键 1–9 + 编码器按压 |
| `CFG:ENC:` | `CFG:ENC:0` | 编码器模式 0–4 |
| `CFG:SLEEP:` | `CFG:SLEEP:1,5` | 启用休眠, 分钟（最小 1） |
| `CFG:TZ:` | `CFG:TZ:CST-8` | POSIX 时区串（NTP） |
| `CFG:WIFI:` | `CFG:WIFI:ssid\|password` | 保存并连接 WiFi（配网模式） |
| `CMD:VERSION` | | 请求固件信息 |
| `CMD:GET_CFG` | | 请求当前配置 |
| `CMD:GET_TZ` | | 请求时区 |
| `CMD:GET_WIFI` | | 请求 WiFi 状态 |
| `CMD:PING` | | 链路探测 |
| `CMD:REBOOT` | | 重启 |
| `CMD:BOOT_CFG` | | 重启并进入配网 / OTA 模式 |
| `CMD:CLEARBOND` | | 清除已绑定的 HID 主机（WS-321） |
| `CMD:GET_BOND` | | 查询当前主机绑定状态（WS-321） |

任何 `CFG:` 命令都会收到 `ACK:<原命令前 96 字节>`。

### 编码器模式

| 值 | 模式 | 设备发送的 HID |
|----|------|----------------|
| 0 | 音量 | Consumer Volume Up / Down |
| 1 | 缩放 | Ctrl + `+` / `-` |
| 2 | 标签页 | Ctrl + Tab / Ctrl + Shift + Tab |
| 3 | 撤销 / 重做 | Ctrl + Z / Ctrl + Y |
| 4 | 应用音量 | F23 / F24（由上位机宏处理） |

## 设备 → 上位机

| 消息 | 示例 | 说明 |
|------|------|------|
| `VERSION:` | `VERSION:1.0.0,Lite,ESP32,Oct 19 2026 03:20:00` | 版本, 型号, 芯片, 编译时间 |
| `BTN:` | `BTN:A1B2:3` | 按键事件；`devId` + 输入编号（0–8 按键，9 编码器按压） |
| `ENC:` | `ENC:A1B2:CW` | 编码器旋转，`CW` / `CCW` |
| `ACK:` | `ACK:CFG:ENC:0` | 配置确认 |
| `PONG` | `PONG` | 对 `CMD:PING` 的应答（上位机计算 RTT） |
| `CFG_KEYS:` | `CFG_KEYS:240,...,249` | `CMD:GET_CFG` 应答 |
| `CFG_PINS:` | `CFG_PINS:13,...,4` | `CMD:GET_CFG` 应答 |
| `CFG_ENC:` | `CFG_ENC:0` | `CMD:GET_CFG` 应答 |
| `CFG_SLEEP:` | `CFG_SLEEP:1,5` | `CMD:GET_CFG` 应答 |
| `CFG_TZ:` | `CFG_TZ:CST-8` | `CMD:GET_CFG` 应答 |
| `TZ_INFO:` | `TZ_INFO:CST-8` | `CMD:GET_TZ` 应答 |
| `WIFI_INFO:` | `WIFI_INFO:home,192.168.1.42` | `CMD:GET_WIFI` 应答；未连接为 `DISCONNECTED,0.0.0.0` |
| `BOND_STATUS:` | `BOND_STATUS:bound,A1:B2:**:**:**:**` | `CMD:GET_BOND` 应答；地址脱敏，只保留前两字节 |
| `BOND_STATUS:` | `BOND_STATUS:unbound` | `CMD:GET_BOND` 应答；未绑定任何主机 |

## HID 键码

固件沿用 BindDeck / t-vk 的键码编码：

- `0x80`–`0x87`：修饰键（LCtrl, LShift, LAlt, LWin, RCtrl, RShift, RAlt, RWin）
- `0xC2`–`0xCD`：F1–F12（HID usage + 0x88）
- `0xD1`–`0xDA`：Insert/Home/PgUp/Delete/End/PgDn/Right/Left/Up/Down
- `0xF0`–`0xFB`：F13–F24
- `0x20`–`0x7E`：可打印 ASCII（字母、数字、符号，大小写按 ASCII 处理）

## 示例：配置一个按键

```
-> CMD:VERSION
<- VERSION:1.0.0,Lite,C3,Oct 19 2026 03:20:00
-> CFG:KEYS:240,241,242,243,244,245,246,247,248,249
<- ACK:CFG:KEYS:240,241,242,243,244,245,246,247,248,249
-> CFG:PINS:0,2,3,4,6,7,8,9,10
<- ACK:CFG:PINS:0,2,3,4,6,7,8,9,10
```
