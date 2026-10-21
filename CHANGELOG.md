# Changelog

## 宏指定浏览器在 Windows 生效（`browser:` 修复）— 2026-10-21（WS-345）

- **根因**：`resolve_macro_browser()` 用 `webbrowser.get(alias)` 找浏览器，Windows 上
  Chrome（尤其 `%LOCALAPPDATA%` 的 per-user 安装）常常不在 `webbrowser` 的注册表列表
  里，抛错后静默 `webbrowser.open()` 回退，于是 `browser:chrome` 打开了 Edge；失败
  信息只 `print()` 到 stdout，desktop 模式（无控制台）下完全不可见。
- **修复（Windows）**：新增 `_windows_browser_exe()`，先查注册表 `App Paths`
  （HKLM → HKCU，读默认值），再查标准安装路径（Chrome / Edge / Firefox 的
  `%PROGRAMFILES%`、`%PROGRAMFILES(X86)%`、`%LOCALAPPDATA%`），命中后用
  `subprocess.Popen([exe, url])` 直接启动，不经过 shell，也不再走 `webbrowser.get()`。
- **修复（Linux / macOS）**：先 `shutil.which()` 找可执行文件并直接 `Popen`，找不到再
  退回原有 `webbrowser.get(alias)`，`run.sh` 用法不变。
- **失败可见**：解析失败写 `level=error` 事件「未找到浏览器 chrome，已回退默认浏览器」；
  打开失败写 `level=error` 事件并带原因；成功写 `level=ok` 事件「已用 Chrome 打开 …」。
  `run_action()` 结果新增 `browser_used`，`/api/action/test` 返回并在前端 toast 里显示
  实际使用的浏览器。
- `browser:safari` 在 Windows 明确报「Windows 不支持 Safari」，不再静默落到 Edge。
- 不改宏语法、不改 `CFG:` / `CMD:` 协议、不动固件；无 `browser:` 步骤的宏行为不变。

## 宏指定浏览器（`browser:` 步骤）— 2026-10-21（WS-340）

- **新增 `browser:<name>` 宏步骤**：可选 `chrome` / `firefox` / `edge` /
  `safari` / `default`（不区分大小写），只影响其后的 `url:` 步骤；不写或写
  `default` 时沿用 `webbrowser.open()`，现有宏行为不变。
- **跨平台别名**：`browser:chrome` 依次尝试 `chrome` / `google-chrome` /
  `chromium` / `chromium-browser`，Firefox 兼容 `mozilla`，Edge 兼容
  `msedge` / `microsoft-edge`，以覆盖 Python `webbrowser` 在 Linux / Windows /
  macOS 上不同的注册名。
- **失败回退**：浏览器未安装、名字不认识或控制器打开失败时记录 `[app]` 日志
  并回退默认浏览器，后续 `text:` / `key:` / `wait:` 步骤继续执行，宏不中断。
- **前端**：`parseMacro()` / `formatMacro()` 支持 `browser:` 序列化与反序列化，
  宏输入框 placeholder、帮助浮层语法表、登录示例与 README 同步更新。无新增
  依赖，不改固件与 `CFG:` / `CMD:` 协议。

## CMD:GET_CFG 回读修复 — 2026-10-21（WS-336）

- **根因**：`BleConfigService::notify()` 用的是 `setValue()` + 无参 `notify()`，走 NimBLE
  的 `ble_gatts_chr_updated()`：每条连接对这个特性只有一个「值已变更」标志，真正发送
  时才去读特性的**当前**值。`CMD:GET_CFG` 在同一个 BLE 写入回调里连发 5 条 `CFG_*`，
  全部在主机任务处理「待发通知」事件之前压在同一个标志上，结果只发出最后一条
  （`CFG_TZ`），上位机把其余 4 个字段判为「设备未回报该字段」。与芯片/分区表无关，
  四个 env 都受影响，只是 16 MB merged 包最先被这样测到。
- **修复**：改走带 payload 的 `NimBLECharacteristic::notify(value, len, conn)`，逐连接
  直接下发 ATT 通知，一条一个包，不再合并；发送失败写一行 CDC 日志。
- **诊断**：`CMD:GET_CFG` 处理中增加一行串口日志，打印实际回读的 5 个字段。
- 版本 1.3.1；四个固件 env 编译通过；16 MB merged / OTA 包重新构建。

## 上位机体验修复（日志复制 / 宏键名 / 帮助）— 2026-10-21（WS-332）

- **日志复制**：日志页每行右侧新增「复制」按钮，点击把「时间 [类型] 内容」
  完整写入剪贴板；优先 `navigator.clipboard.writeText()`，不可用时降级为
  隐藏 textarea + `document.execCommand('copy')`。日志轮询、清空与 300 行上限不变。
- **宏键名映射**：`run_action()` 的 `key:` 步骤先经 `macro_key_name()` 归一化
  （`Tab`→`tab`、`PageUp`→`page up`、`RCtrl`→`right ctrl`、`F1`–`F12`→`f1`–`f12`
  等），组合键逐段处理（`key:LCtrl+Tab` → `ctrl+tab`）；单个字母/数字、
  F13–F24 以及未知键名保持原样，旧宏行为不变，未知键名仍由 `keyboard` 库
  报错并经动作日志 / toast 提示。宏输入框 placeholder 补充支持的键名列表。
- **修复 `key:` 前缀污染（评审必改项）**：`parseMacro()` 原先对 `key:` 行返回
  整行 `"key:Tab"` 而不是键名 `"Tab"`，导致归一化查不到、仍报
  `key:Tab is not mapped to any known key`，且每次保存都会叠加成 `key:key:Tab`。
  现在只存键名并清掉历史前缀；`macro_key_name()` 也兼容旧配置里残留的
  `key:` 前缀，已保存的宏无需手工重录。
- **宏帮助**：右上角新增「帮助」入口，浮层包含 `key:` / `text:` / `wait:` /
  `url:` 语法说明、键名对照表、完整登录示例与常见错误排查；Esc、遮罩点击或
  右上角关闭，打开时锁定页面滚动，不请求后端、不影响现有布局与轮询。
- 涉及 `host/app.py` / `host/static/app.js` / `host/static/style.css` /
  `host/templates/index.html`，无新增依赖；Linux / macOS 的 `run.sh` 用法不变。

## 上位机 OTA — 2026-10-21（WS-328）

- **复审修复**：OTA 期间 CFG RX 特性原先把所有写入都当固件数据，导致
  `CMD:OTA_END` / `CMD:OTA_CANCEL` 到不了命令解析器（`Update.end(true)`
  永不执行、设备不重启）。现在按「短 + `CMD:`/`CFG:` 前缀 + 可打印 ASCII」
  识别文本命令，固件块（`0xE9` 开头）仍走二进制通道；上位机「取消升级」
  按钮改为真正调用 `POST /api/ota/<dev_id>/cancel`，下发 `CMD:OTA_CANCEL`。

- **固件**：新增 `CMD:OTA:<size>:<sha256>` / `CMD:OTA_END` / `CMD:OTA_CANCEL`。
  进入时校验发起者是**已绑定主机且持有 HID 主连接**（`isHostBound()` +
  `primaryConn()`），否则回 `ACK:OTA:ERROR:not_bound`；`Update.begin(size)`
  后 CFG RX 特性切换为二进制接收（`ble_config` 新增 `onOtaData` /
  `onDisconnect`）。每累计 4096 字节回一次 `ACK:OTA:<已确认字节数>`（窗口 ACK，
  不做逐块停等）；`CMD:OTA_END` 先比对总长度与 SHA256，任一不符
  `Update.abort()` 且旧固件保持可启动，回 `ERROR:size` / `ERROR:hash`；
  校验通过才 `Update.end(true)` 并重启。超时改为**块间 30 秒无数据**；
  BLE 断连立即 abort。OTA 期间 `loop()` 暂停按键/编码器处理，CFG 通道保持可用。
- **上位机**：设备页新增「OTA 升级」按钮与进度卡片；`POST /api/ota/<dev_id>`
  后台流水线推送（`min(512, max(200, MTU-3))` 块大小、32 KB 在途窗口），
  `GET /api/ota/<dev_id>/progress` 按**设备已确认字节**报进度。升级后用
  `CMD:VERSION` 比对升级前后的版本号 / 构建时间戳（或文件名里的 `_vX.Y.Z`），
  确认设备真的重启并运行新固件才提示成功。`ble_link` 新增 `send_raw()` / `max_payload()`。
- **校验**：前端 + 后端双重校验 `ota_*.bin` 文件名，后端再校验镜像头与
  SHA256，拒绝 merged 整片包；设备未连接禁止 OTA；OTA 期间上位机锁定
  配置下发与设备指令。
- **兜底**：配网页面 `/update` 保留；旧固件无 `CMD:OTA:<size>:<sha256>` 时
  上位机提示「固件版本过低，请使用配网页面」。
- 四个固件 env（`esp32dev` / `esp32c3` / `esp32c3_2mb` / `esp32c3_16mb`）
  编译通过；`docs/protocol.md` 同步。

## 主机绑定 — 2026-10-21（WS-321）

- **固件**：`src/nimble_hid.cpp` 新增 NVS `hostAddr`（6B）+ `hostBound`（bool），
  首个 HID 订阅时记录 identity address（`peer_id_addr`，无效用 `peer_ota_addr`
  归一化）；已绑定且地址不同的 central 拒绝 HID 订阅，日志打印
  `[ble] reject non-bound host ...`；同一地址的 CFG 连接不受影响。
- **上位机**：设备页新增「清除主机绑定」按钮 + 绑定状态显示（地址脱敏）；
  新增 `CMD:CLEARBOND` / `CMD:GET_BOND` 命令，与现有 `CMD:*` 命名一致。
- **上电长按**：按住编码器 5 秒 clear binding（与 0.2 秒配网、1.2 秒双击配网
  区分），CDC 日志 `[ble] host binding cleared by boot gesture`。
- 四个固件 env（`esp32dev` / `esp32c3` / `esp32c3_2mb` / `esp32c3_16mb`）
  编译通过；`docs/protocol.md` 与 README 同步。

## 上位机桌面版 — 2026-10-20（pywebview 真桌面化）

- **应用模型改为 pywebview 原生窗口**：`host/desktop.py` 新增 `run_webview()`，
  Flask 在后台线程运行，pywebview 在主线程创建本机窗口（1280×800）并
  `webview.start()` 阻塞运行。双击 exe 直接出现应用窗口，**无黑色控制台、
  无浏览器弹窗**；关闭窗口退出，有托盘时改为最小化到托盘。
- `host/app.py` 的 `main()` 桌面分支由 `run_desktop()` 改为 `run_webview()`。
- **兜底**：pywebview 或其 GUI 后端不可用（无头 Linux、Windows 无 pythonnet）
  时自动退回 `_serve_headless()` 托盘/无头服务，`python app.py --desktop`
  不会因缺 GUI 而崩溃；`run.sh` / `run.bat` 浏览器方式不受影响。
- 托盘菜单「显示控制台」改为「显示窗口」（直接唤起 pywebview 窗口）；
  pywebview 的 `Window.show()/hide()/destroy()` 已在 GUI 线程 marshalling，
  托盘回调跨线程调用安全。
- 依赖：`host/requirements-desktop.txt` 新增 `pywebview>=6.0` 与
  `pythonnet>=3.0; sys_platform == "win32"`（pywebview 的 Windows 后端）。
- 打包：`host/app.spec` 增加 `webview` / `webview.platforms.{winforms,
  edgechromium,mshtml}` / `pythonnet` / `clr_loader` hidden-imports，并对
  `webview`、`pythonnet` 做 `collect_submodules`；两个包各自带的
  PyInstaller hook（JS bridge 数据文件、pythonnet .NET 互op 程序集）自动生效。

## 上位机桌面版修复 — 2026-10-20

- 修复托盘不可用时应用无法退出：启动前先探测 pystray / Pillow，探测失败不隐藏控制台；
  托盘创建或运行时失败会恢复控制台，并提供 `POST /api/quit`，UI 新增「退出上位机」按钮。
- 文件对话框子进程超时 600s → 120s；错误信息补充退出码，便于定位。
- `POST /api/desktop` 重写自启动条目失败时写入 warn 日志，不再静默。
- `minimizedToggle` 请求期间禁用，避免快速双击产生并发请求。
- 打开浏览器前改为轮询 `/api/status` 直到服务就绪（最多 5 秒），不再固定 `sleep(0.4)`。
- 打包：`disable_windowed_traceback=True`，崩溃时不再弹出含内部路径的 traceback 窗口。

## 上位机桌面版 — 2026-10-19

- 新增 `host/app.spec` + `host/build.bat`：PyInstaller 打包单文件 `BindDeckLite.exe`
  （windowed、无控制台、带 `host/assets/app.ico`），`templates/`、`static/`、图标一并内嵌。
- 新增 `host/desktop.py`：系统托盘（显示控制台 / 退出）、启动时隐藏控制台、重复双击只打开浏览器、
  `--file-dialog` 原生文件对话框子进程。打包版默认进入托盘模式，源码可用 `python app.py --desktop`。
- 新增 `host/autostart.py`：开机自启动（Windows `HKCU\...\Run`、Linux XDG autostart、macOS LaunchAgent），
  「设备 → 桌面应用」可开关，并可选择开机启动时直接收进托盘。
- 按键配置「启动程序 / 文件」增加「浏览…」按钮（系统文件选择器），最近 8 条路径作为输入候选。
- 配置 / BLE 缓存 / 日志路径适配打包：exe 同目录优先，不可写时退到 `%LOCALAPPDATA%\BindDeckLite`。
- `run.sh` / `run.bat` 与 BLE、配置下发、按键、编码器、休眠、日志逻辑保持不变。

## v1.3.0 — 2026-10-19

- **应用迁移到 Arduino core 3.x**：`platformio.ini` 改用 pioarduino 平台包
  `55.03.312`（Arduino 3.3.12 / ESP-IDF 5.5.5）。`esp32dev` / `esp32c3` / `esp32c3_2mb` /
  `esp32c3_16mb` 四个环境与分区表保持不变。
- **NimBLE-Arduino 升级到 2.x**（`^2.1.0`，本次构建为 2.5.1）：
  - `NimBLEServerCallbacks::onConnect/onDisconnect` 改为 `(NimBLEServer*, NimBLEConnInfo&)` /
    `(NimBLEServer*, NimBLEConnInfo&, int)`，原来的两个断开回调合并为一个；
  - `NimBLECharacteristicCallbacks::onSubscribe/onWrite` 改用 `NimBLEConnInfo&`，
    地址取自 `connInfo.getAddress()`；
  - HID API 改名：`setManufacturer` / `setPnp` / `setHidInfo` / `setReportMap` /
    `getInputReport` / `getDeviceInfoService` / `getHidService`；
  - 服务统一由 `NimBLEServer::start()` 启动（`startServices()` 与 `NimBLEService::start()`
    在 2.x 已弃用）；
  - `setPower(ESP_PWR_LVL_P9)` → `setPower(9)`；
  - 2.x 移除了 `getSubscribedCount()`，CFG TX 特征改为在 `onSubscribe` 里自行跟踪订阅状态。
- `main.cpp`：`esp_efuse_mac_get_default()` → `esp_read_mac(mac, ESP_MAC_WIFI_STA)`（`esp_mac.h`）。
- **bootloader 用同一套 ESP-IDF 5.5.5 重编**（`bl_idf/`：`CONFIG_SPI_FLASH_ROM_IMPL=y`、
  16 MB flash 头、DIO 40 MHz，13.2 KB）。`tools/build_release.py` 打包 16 MB 镜像时自动使用它，
  并校验镜像头确实是 16 MB。
- 协议（`CFG:*` / `CMD:*` / `BTN:*` / `ENC:*`）、引脚映射、CDC 日志、配网门户与 OTA 均未改变。

## v1.2.0 — 2026-10-19

- 新增 **16 MB 布局**镜像：bootloader 以 `CONFIG_ESPTOOLPY_FLASHSIZE_16MB` 构建（头部 flash size = 16 MB），
  分区表用满整片：`nvs 0x9000 / otadata 0xE000 / app0 0x10000(3MB) / app1 0x310000(3MB) /
  storage 0x610000(9.875MB) / coredump 0xFF0000(64KB)`，全部结束于 `0x1000000`。
- 应用版本 1.2.0（与 1.1.0 功能相同，仅便于区分 16 MB 布局）。
- 4 MB 布局的 v1.1.0 继续保留（兼容旧分区/OTA 路径）。

## v1.1.0 — 2026-10-19

- 用 **ESP-IDF 5.1 + `CONFIG_SPI_FLASH_ROM_IMPL=y`** 自建 bootloader（13.3 KB，与 Tasmota 的
  实现方式一致：直接走 ROM 的 Flash 实现建立映射），替换掉临时使用的第三方 bootloader。
- 应用、分区表（4 MB，含 coredump）不变；版本号 1.1.0 便于确认。
- 说明：Arduino core 3.x 仍基于 IDF 5.x；IDF 6 目前没有 Arduino 支持，采用 IDF 6 需要把固件
  改成纯 ESP-IDF 工程（BLE HID / WiFi 门户 / OTA / NVS 全部重写），与本次 Flash 问题无关。

## v1.0.9 — 2026-10-19

- 现场验证 v1.0.8：9 个按键（`BTN:95B4:0..8`）与 EC11（`ENC:95B4:CW/CCW`）全部正常，
  HID primary 成功建立（PC 接管）。
- 本版把 bootloader 换回**自研的 80 MHz 版本**（Arduino core 3.x / ESP-IDF 5 构建，
  与已验证的 Tasmota bootloader 同为 80 MHz DIO），去掉对第三方二进制的依赖。
- 应用侧仅版本号变化。

## v1.0.8 — 2026-10-19

- 修复首次启动的 NVS 噪音：`loadConfig()` 改用读写方式打开命名空间，空白设备首启即创建，
  不再出现 `nvs_open failed: NOT_FOUND` / `nvs_get_blob len fail`。
- 分区表新增 `coredump` 分区（4 MB: `0x3F0000`；16 MB: `0xFF0000`；2 MB: `0x1F0000`），
  消除启动时 `esp_core_dump_flash: No core dump partition found!`，崩溃时可保存 core dump。
- 现场验证：v1.0.7 已在 16 MB C3 板上启动，BLE 广播 `BindDeckLite-95B4` 并能被主机连接。

## v1.0.7 — 2026-10-19

- **C3 日志改走 USB CDC**：GPIO20/21 是本板编码器的 A/B 相（也是 UART0 的默认引脚），
  之前的 `log_e` 全部输出到 UART0，等于发给了编码器，CDC 口什么也看不到。
  现在 C3 环境编译时加 `-DARDUINO_USB_CDC_ON_BOOT=1 -DARDUINO_USB_MODE=1`，
  `Serial` 即 USB Serial/JTAG，`log_e` 重定向到 CDC（`Serial.printf`），
  `setTxTimeoutMs(0)` 保证没有主机时也不会阻塞启动。
- 诊断包也同步改为 CDC 输出。

## 2026-10-19 — 16 MB 板启动问题定位

- 现场 16 MB C3 板在 bootloader 阶段静默复位（`entry 0x403cc710` 后立刻 `rst:0x3`），
  应用从未执行；同板 Tasmota 可正常启动。
- Tasmota 的 bootloader 入口是 `0x403cbf10`（ESP-IDF 5.x），本项目 Arduino 2.0.17 的 bootloader
  入口是 `0x403cc710`（ESP-IDF 4.4.x）。用 core 3.x 的 bootloader 与现有 app/分区表重新合并出
  `test_idf5bl_binddecklite_c3_16mb.bin` 用于验证；若可启动，则整体迁移到 Arduino core 3.x。
- `platformio.ini` 显式固定 `espressif32@7.1.3`，避免机器上同时安装 pioarduino(55.x) 时误用。

## v1.0.6 — 2026-10-19

- 新增 **ESP32-C3 16 MB Flash** 变体（`esp32c3_16mb`）：16 MB 分区表（3 MB OTA 槽 + storage），
  并改用 **40 MHz DIO**。部分 16 MB 模组在 80 MHz 下 bootloader 读完分区表即静默复位
  （`entry 0x403cc710` 后无任何输出），40 MHz 是保守可用的设置。
- 16 MB 表的每个分区都结束在 `0x1000000` 以内，`--flash_size 16MB` 写入镜像头。
- 4 MB / 2 MB 变体保持不变。

## v1.0.5 — 2026-10-19

- 新增 **ESP32-C3 2 MB Flash** 变体（`esp32c3_2mb`）：单 app 槽、无 OTA。
  IDF 的 `esp_partition_table_verify()` 会逐项检查 `offset + size <= chip_size`，任何一项越界都会**整表判为无效**；
  Arduino 2.0.17 的 bootloader 又是 `BOOTLOADER_LOG_LEVEL_NONE`，所以只在 2 MB 模块上表现为
  `rst:0x3 RTC_SW_SYS_RST` 静默循环，且 app 一行日志都不会出现。
  4 MB 的 `app1@0x200000` 正好越界——与现场日志完全吻合。
- 4 MB 版本保持不变（仍带 OTA）。

## v1.0.4 — 2026-10-19

- 修复 C3 引脚校验：v1.0.0–v1.0.3 把经典 ESP32 的「GPIO6–11 不可用」规则也套在了 C3 上，
  导致 C3 上任何包含 GPIO6/7/8 的引脚集合都被判为非法——而本板按键 7/8/9 正好在 GPIO6/7/8，
  于是 `CFG:PINS:` 和配网页的引脚保存总被拒绝（开机默认值仍能生效，所以按键本身是好的）。
  现在经典 ESP32 的限制只在 `esp32dev` 环境生效，C3 只排除 GPIO11–17（Flash）与编码器占用脚。

## v1.0.3 — 2026-10-19

按实际硬件修正 C3 默认引脚（之前的默认值与板子不符）：

| 功能 | 引脚 |
|------|------|
| 按键 1–9 | GPIO0–GPIO8 |
| 编码器按压（第 10 个按键） | GPIO10 |
| 编码器 A / B | GPIO20 / GPIO21 |

- `CFG_SCHEMA` 升到 2：OTA 升级保留 NVS 时也会强制采用新的默认引脚，不需要手动擦除。
- 上位机默认引脚同步更新；README 增加 GPIO8 启动 strapping 提示（上电不要按住 9 号键）。
- 功能与 v1.0.2 相同（双击配网、开机 10 秒内不重启、RTC 启动计数保护）。

## v1.0.2 — 2026-10-19

- 按反馈重新编译发布（分区表 CSV 已带标准表头，构建产物 `partitions.bin` 与 v1.0.1 完全一致）。
- 所有重启路径再加一道保护：**开机 10 秒内绝不重启**，彻底排除因编码器引脚电平导致的复位循环。
- 固件版本号 `1.0.2`，功能与 v1.0.1 相同。

## v1.0.1 — 2026-10-19

修复刷机后无限重启（`rst:0x3 RTC_SW_SYS_RST`）。

- **根因**：v1.0.0 把「长按编码器 5 秒进入配网」做成了固件里唯一的自动 `ESP.restart()`。
  只要编码器按压脚在启动时读到低电平（编码器接成高有效、按键卡住、或该引脚被板上其他电路拉低），
  每次启动都会在 5 秒后重启，形成约 6 秒一次的循环；`RTC_SW_SYS_RST` 正是 `ESP.restart()` 的复位原因。
- 配网手势改为 **1.2 秒内双击**：双击必须有一次「松开（高电平）」才能完成，常低引脚无法触发。
- 手势与按键上报只在编码器按压脚**至少出现过一次空闲高电平**后才启用。
- 新增 RTC 内存启动计数：连续 4 次在 30 秒内重启则进入安全模式，跳过配网检测与手势，保证设备先能正常跑起来。
- 启动日志打印复位原因与启动计数（`[boot] reset=... bootCount=... safe=... sw=...`）。

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
