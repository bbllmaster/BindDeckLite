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
| `CMD:OTA:<size>:<sha256>` | `CMD:OTA:1234567:9f86d0…` | 进入上位机推送 OTA 模式（仅绑定主机 / HID 主连接） |
| `CMD:OTA_END` | | 固件接收完毕，校验长度与 SHA256 后重启 |
| `CMD:OTA_CANCEL` | | 取消进行中的 OTA，丢弃已写入数据 |

任何 `CFG:` 命令都会收到 `ACK:<原命令前 96 字节>`。

### 上位机推送 OTA（CMD:OTA）

与配网页面 `/update` 的 OTA 互补：用户无需手动连接设备热点，直接在上位机
里完成固件刷新。只接受 **app-only** 固件（`ota_*.bin`），**拒绝 merged 整片包**。

1. 上位机发送 `CMD:OTA:<size>:<sha256_hex>`（十进制字节数 + 64 位十六进制
   SHA256）。设备依次校验：非配网模式、无进行中的会话、**发起者必须是已绑定
   主机且持有 HID 主连接**（`isHostBound()` + `primaryConn()`），然后
   `Update.begin(size)` 并切换 CFG 特性为二进制接收，回复 `ACK:CMD:OTA`。
2. 上位机通过 RX 特性连续写入固件原始字节（非文本行，可含 NUL / 换行）。
   块大小按协商 MTU 取 `MTU-3`，不小于 200 B、不大于 512 B；上位机保持
   约 32 KB 的流水线窗口，不等单块 ACK。
   设备端以「短、以 `CMD:` / `CFG:` 开头、内容为可打印 ASCII」区分文本命令与
   固件块（固件块以 `0xE9` 开头），因此 `CMD:OTA_END` / `CMD:OTA_CANCEL`
   在 OTA 期间仍能到达命令解析器。
3. 设备每累计接收 **4096 字节**（以及最后一个字节）回复一次
   `ACK:OTA:<已确认字节数>`，上位机以该值计算进度。
4. 全部写入后上位机发送 `CMD:OTA_END`：设备先比对已收字节数与 `size`，
   再比对 SHA256，**任一不符立即 `Update.abort()`，旧固件保持可启动**，
   分别回复 `ACK:OTA:ERROR:size` / `ACK:OTA:ERROR:hash`。校验通过才
   `Update.end(true)` 并重启，回复 `ACK:OTA:DONE`（重启时该 ACK 可能丢失）。
5. 上位机在重启后通过 `CMD:VERSION` 确认设备确实运行新版本，才提示升级成功。
6. 超时定义为**块间 30 秒无数据**（不是全程 30 秒）；超时或 BLE 断连都会
   立即 `Update.abort()`，设备不重启、不切换分区。
7. 旧固件（不支持 `CMD:OTA:<size>:<sha256>`）→ 上位机 5 秒无回应，提示
   「固件版本过低，请使用配网页面 OTA」。

错误回复格式：`ACK:OTA:ERROR:<原因>`，原因包括：

| 原因 | 说明 |
|------|------|
| `not_bound` | 发起者不是已绑定主机，或没有 HID 主连接 |
| `args` | 参数缺失 / 长度或 SHA256 格式非法 / 超出分区容量 |
| `config_mode` | 设备处于配网模式，请退出后重试 |
| `busy` | 已有 OTA 会话在进行 |
| `not_active` | `CMD:OTA_END` / `CMD:OTA_CANCEL` 时没有会话 |
| `not_app_image` | 镜像头不是 ESP app 镜像（merged 整片包会命中） |
| `chunk_too_big` | 单次写入超过 512 字节 |
| `size` | 已收字节数不等于声明长度（或流超出声明长度） |
| `hash` | SHA256 不匹配 |
| `begin_failed` / `write_failed` / `end_failed` | Update 框架失败 |
| `timeout` | 块间 30 秒无数据 |

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
