/*
 * BindDeckLite firmware — 9 buttons + EC11 rotary encoder, no display.
 *
 * Targets: classic ESP32 (esp32dev) and ESP32-C3.
 *
 * Design notes
 * ------------
 * - The original BindDeck carried an OLED, a separate menu button and eight
 *   buttons. The Lite board drops the display and the menu key, adds a ninth
 *   button and keeps the EC11 (rotate + push). With no display there is no
 *   on-device UI: all configuration happens from the host app (BLE CFG service
 *   or the WiFi config portal), and the encoder push doubles as the config-mode
 *   entry/exit gesture.
 * - Button presses are reported as `BTN:<devId>:<index>` (0..8 = buttons,
 *   9 = encoder push) and are ALSO sent as HID keystrokes, so the deck works
 *   without the host app running.
 * - No display libraries, no RoboEyes, no I2C: the firmware is small and the
 *   C3 pin budget (13 usable GPIOs) finally fits 9 buttons + 3 encoder pins.
 */
#include <Arduino.h>
#include "nimble_hid.h"   // NimBLE HID keyboard + CFG/CMD service (shared code)
#include "ble_config.h"   // CFG/CMD over the BLE link (secondary GATT service)
#include <Bounce2.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <WebServer.h>
#include <Update.h>
#include <esp_sleep.h>
#include <esp_timer.h>
#include <esp_log.h>
#include <nvs_flash.h>
#include <esp_efuse.h>
#include <time.h>

// === ESP32-C3 console =====================================================
// On this board GPIO20/21 are the EC11 A/B lines (UART0's default pins), so the
// firmware must NOT use UART0 for logging. The C3 is built with
// -DARDUINO_USB_CDC_ON_BOOT=1, which makes `Serial` the USB Serial/JTAG (CDC)
// port; the app's logs and the diag output therefore appear on the same CDC
// port as the ROM banner, and the encoder pins stay untouched.
#ifdef TARGET_ESP32C3
  #undef log_e
  #define log_e(fmt, ...) \
      do { Serial.printf("[%8lu] " fmt "\r\n", (unsigned long)millis(), ##__VA_ARGS__); } while (0)
#endif

// === Identity =============================================================
#define FW_VERSION "1.1.0"
#define FW_MODEL   "Lite"

#ifdef TARGET_ESP32C3
  #define FW_CHIP "C3"
#else
  #define FW_CHIP "ESP32"
#endif
#define FW_BUILD __DATE__ " " __TIME__

#define FW_NAME_PREFIX "BindDeckLite"

// === Hardware =============================================================
#define NUM_BUTTONS 9
#define NUM_INPUTS  10          // 9 buttons + encoder push (index 9)
// Bumped when the default pin map changes, so a board that keeps its NVS across
// an OTA adopts the corrected wiring instead of the old stored pins.
#define CFG_SCHEMA  2

#ifdef TARGET_ESP32C3
  // ESP32-C3 usable GPIOs: 0-10, 20, 21. Flash uses 11-17, USB-CDC 18/19.
  //
  // BindDeckLite C3 board wiring (WS-278, confirmed by the hardware owner):
  //   9 buttons  -> GPIO0..GPIO8   (button 1..9)
  //   EC11 push  -> GPIO10         (10th independent button, BTN index 9)
  //   EC11 A/B   -> GPIO20/GPIO21
  // Note: GPIO8/GPIO9 are C3 strapping pins; button 9 sits on GPIO8, so do not
  // hold it while powering on (the ROM would enter download mode instead).
  #define ENCODER_CLK 20
  #define ENCODER_DT  21
  #define ENCODER_SW  10
  static const int DEFAULT_BUTTON_PINS[NUM_BUTTONS] = {0, 1, 2, 3, 4, 5, 6, 7, 8};
#else
  // Classic ESP32: encoder on 18/19/5 as before; GPIO4 was the menu key and
  // GPIO21 the OLED SDA - both are now free and become the ninth button and
  // a spare respectively (the menu pin is used here to keep wiring simple).
  #define ENCODER_CLK 18
  #define ENCODER_DT  19
  #define ENCODER_SW   5
  static const int DEFAULT_BUTTON_PINS[NUM_BUTTONS] = {13, 12, 14, 27, 32, 33, 25, 26, 4};
#endif

// Default HID keys for the 9 buttons and the encoder push (F13..F22).
static const uint8_t DEFAULT_KEYS[NUM_INPUTS] = {
  KEY_F13, KEY_F14, KEY_F15, KEY_F16, KEY_F17,
  KEY_F18, KEY_F19, KEY_F20, KEY_F21, KEY_F22
};

// v1.0.1: the old "hold 5 s to open the portal" gesture was the firmware's only
// automatic ESP.restart() path. A pin that reads LOW (active-high encoder
// wiring, a stuck switch, or a pin used by something else on the board) turned
// it into a reboot loop every ~6 s. The portal now needs a deliberate
// DOUBLE-CLICK, which a permanently-LOW pin can never produce.
#define DOUBLE_CLICK_MS     1200   // second press within this window -> portal
#define CONFIG_MODE_MS      300000 // portal auto-closes after 5 min
#define CONFIG_EXIT_MS      1000   // hold the encoder push this long to leave
#define DEBOUNCE_MS         25

// === Globals ==============================================================
BleKeyboard bleKeyboard(FW_NAME_PREFIX, "BindDeckLite", 100);
Preferences preferences;

int buttonPins[NUM_BUTTONS];
int buttonKeys[NUM_INPUTS];
int encMode = 0;
bool sleepEnabled = true;
unsigned long sleepTimeoutMs = 300000;
char tzString[64] = "CET-1CEST,M3.5.0,M10.5.0/3";

static String g_devId;    // "A1B2"
static String g_bleName;  // "BindDeckLite-A1B2"

static bool g_configMode = false;
static bool g_bleStarted = false;
static unsigned long g_configModeStart = 0;
static String g_cfgApName;
static int g_otaProgress = -1;

static bool g_nvsOk = false;
static unsigned long g_lastActivity = 0;

// v1.0.1 boot-loop guard (see setup()).
#define BOOT_MAGIC 0xB17D0001u
RTC_NOINIT_ATTR static uint32_t g_bootMagic;
RTC_NOINIT_ATTR static uint32_t g_bootCount;
static bool g_safeMode = false;
// True once the encoder push has been seen idle (HIGH at least once). Gestures
// stay disarmed until then so a pin that is permanently LOW cannot fire them.
static bool g_encSeenIdle = false;

// === Encoder sampling =====================================================
static const int8_t ENC_STATES[] = {0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0};
static volatile int encoderSteps = 0;
static volatile uint8_t oldAB = 0;

static void readEncoder() {
  oldAB <<= 2;
  uint8_t cur = 0;
  if (digitalRead(ENCODER_CLK)) cur |= 0x02;
  if (digitalRead(ENCODER_DT))  cur |= 0x01;
  oldAB |= (cur & 0x03);
  encoderSteps += ENC_STATES[(oldAB & 0x0f)];
}

static esp_timer_handle_t s_encoderTimer = NULL;
static void encoderTimerCb(void*) { readEncoder(); }

// === Pin validation =======================================================
static bool isValidButtonPin(int pin) {
#ifdef TARGET_ESP32C3
  if (pin < 0 || pin > 21) return false;
  if (pin >= 11 && pin <= 17) return false;   // embedded flash
  // Do NOT apply the classic-ESP32 exclusions here: GPIO6..GPIO8 are ordinary
  // pins on the C3 and buttons 7..9 of this board sit on them. Applying the
  // classic rule made every pin set invalid on the C3, so CFG:PINS: and the
  // portal form were always rejected (v1.0.0..v1.0.3).
#else
  if (pin < 0 || pin > 39) return false;
  if (pin >= 6 && pin <= 11) return false;    // classic ESP32 SPI flash
  if (pin >= 34) return false;                // input-only, no pull-up
#endif
  if (pin == ENCODER_CLK || pin == ENCODER_DT || pin == ENCODER_SW) return false;
  return true;
}

static bool isValidPinSet(const int pins[NUM_BUTTONS]) {
  for (int i = 0; i < NUM_BUTTONS; i++) {
    if (!isValidButtonPin(pins[i])) return false;
    for (int j = i + 1; j < NUM_BUTTONS; j++) {
      if (pins[i] == pins[j]) return false;
    }
  }
  return true;
}

// === Buttons ==============================================================
Bounce2::Button switches[NUM_BUTTONS];

static void applyButtonPins() {
  for (int i = 0; i < NUM_BUTTONS; i++) {
    switches[i].attach(buttonPins[i], INPUT_PULLUP);
    switches[i].interval(DEBOUNCE_MS);
    switches[i].setPressedState(LOW);
  }
}

// === Config persistence ===================================================
static void saveConfig() {
  preferences.begin("binddecklite", false);
  preferences.putInt("encMode", encMode);
  preferences.putInt("cfgVer", CFG_SCHEMA);
  preferences.putBool("sleepEnabled", sleepEnabled);
  preferences.putULong("sleepTimeoutMs", sleepTimeoutMs);
  preferences.putString("tz", tzString);
  for (int i = 0; i < NUM_BUTTONS; i++) {
    char k[10];
    snprintf(k, sizeof(k), "pin%d", i);
    preferences.putInt(k, buttonPins[i]);
  }
  for (int i = 0; i < NUM_INPUTS; i++) {
    char k[10];
    snprintf(k, sizeof(k), "key%d", i);
    preferences.putInt(k, buttonKeys[i]);
  }
  preferences.end();
}

static void loadConfig() {
  for (int i = 0; i < NUM_INPUTS; i++) buttonKeys[i] = DEFAULT_KEYS[i];
  for (int i = 0; i < NUM_BUTTONS; i++) buttonPins[i] = DEFAULT_BUTTON_PINS[i];

  // Open read-write so the namespace is created on a blank device; a read-only
  // open returns NOT_FOUND on first boot and produced the nvs_open errors.
  if (!preferences.begin("binddecklite", false)) {
    log_e("[cfg] NVS unavailable - defaults only");
    return;
  }
  encMode = preferences.getInt("encMode", 0);
  if (encMode < 0 || encMode > 4) encMode = 0;
  sleepEnabled = preferences.getBool("sleepEnabled", true);
  sleepTimeoutMs = preferences.getULong("sleepTimeoutMs", 300000);
  if (sleepTimeoutMs < 60000) sleepTimeoutMs = 300000;

  String tz = preferences.getString("tz", tzString);
  tz.trim();
  if (tz.length() > 0 && tz.length() < (int)sizeof(tzString)) {
    strncpy(tzString, tz.c_str(), sizeof(tzString) - 1);
    tzString[sizeof(tzString) - 1] = '\0';
  }

  int schema = preferences.getInt("cfgVer", 1);
  bool staleSchema = (schema < CFG_SCHEMA);

  int loaded[NUM_BUTTONS];
  bool bad = false;
  for (int i = 0; i < NUM_BUTTONS; i++) {
    char k[10];
    snprintf(k, sizeof(k), "pin%d", i);
    loaded[i] = staleSchema ? DEFAULT_BUTTON_PINS[i]
                            : preferences.getInt(k, DEFAULT_BUTTON_PINS[i]);
    if (!isValidButtonPin(loaded[i])) bad = true;
  }
  if (staleSchema) {
    log_e("[cfg] pin schema v%d -> v%d: using the board defaults", schema, CFG_SCHEMA);
  }
  if (isValidPinSet(loaded)) {
    for (int i = 0; i < NUM_BUTTONS; i++) buttonPins[i] = loaded[i];
  } else {
    log_e("[cfg] stored pin set invalid - keeping defaults");
    (void)bad;
  }

  for (int i = 0; i < NUM_INPUTS; i++) {
    char k[10];
    snprintf(k, sizeof(k), "key%d", i);
    buttonKeys[i] = preferences.getInt(k, DEFAULT_KEYS[i]);
  }
  preferences.end();
  if (staleSchema) {
    saveConfig();   // persist the corrected pin map + new schema version
  }
  log_e("[cfg] enc=%d sleep=%d/%lums", encMode, (int)sleepEnabled, sleepTimeoutMs);
}

// === Transport to the host ================================================
static WiFiUDP udp;
static IPAddress pcIP;
static bool pcIpSet = false;

static void sendToHost(const String& data) {
  Serial.println(data);
  bleConfig.notify(data);                 // BLE CFG TX (works with no WiFi)
  if (WiFi.status() == WL_CONNECTED) {    // config mode only
    if (pcIpSet) udp.beginPacket(pcIP, 4211);
    else         udp.beginPacket(IPAddress(255, 255, 255, 255), 4211);
    udp.print(data);
    udp.endPacket();
  }
}

static void applyTimezone() { configTzTime(tzString, "pool.ntp.org"); }

// === HID actions ==========================================================
static void sendKeyForInput(int idx) {
  if (idx < 0 || idx >= NUM_INPUTS) return;
  if (bleKeyboard.isConnected()) {
    bleKeyboard.press(buttonKeys[idx]);
    delay(8);
    bleKeyboard.releaseAll();
  }
}

static void handleEncoderAction(bool forward) {
  if (bleKeyboard.isConnected()) {
    switch (encMode) {
      case 0:  // volume
        bleKeyboard.write(forward ? KEY_MEDIA_VOLUME_UP : KEY_MEDIA_VOLUME_DOWN);
        break;
      case 1:  // zoom (Ctrl +/-)
        bleKeyboard.press(KEY_LEFT_CTRL);
        bleKeyboard.write(forward ? '+' : '-');
        bleKeyboard.releaseAll();
        break;
      case 2:  // browser tabs
        bleKeyboard.press(KEY_LEFT_CTRL);
        if (!forward) bleKeyboard.press(KEY_LEFT_SHIFT);
        bleKeyboard.write(KEY_TAB);
        bleKeyboard.releaseAll();
        break;
      case 3:  // undo / redo
        bleKeyboard.press(KEY_LEFT_CTRL);
        bleKeyboard.write(forward ? 'y' : 'z');
        bleKeyboard.releaseAll();
        break;
      case 4:  // per-app volume (host macro on F23/F24)
        bleKeyboard.write(forward ? KEY_F24 : KEY_F23);
        break;
    }
  }
  sendToHost("ENC:" + g_devId + ":" + (forward ? "CW" : "CCW"));
}

// === WiFi provisioning + OTA (config mode) ===============================
static void loadWifiCreds(String& ssid, String& pwd) {
  preferences.begin("binddecklite", true);
  ssid = preferences.getString("wifiSSID", "");
  pwd  = preferences.getString("wifiPwd", "");
  preferences.end();
}

static void saveWifiCreds(const String& ssid, const String& pwd) {
  preferences.begin("binddecklite", false);
  preferences.putString("wifiSSID", ssid);
  preferences.putString("wifiPwd", pwd);
  preferences.end();
}

static String htmlEsc(const String& s) {
  String o;
  for (unsigned int i = 0; i < s.length(); i++) {
    char c = s.charAt(i);
    if      (c == '<') o += "&lt;";
    else if (c == '>') o += "&gt;";
    else if (c == '&') o += "&amp;";
    else if (c == '"') o += "&quot;";
    else if (c == '\'') o += "&#39;";
    else o += c;
  }
  return o;
}

static WebServer cfgServer(80);

static String cfgPage() {
  String ssid, pwd;
  loadWifiCreds(ssid, pwd);

  String h = F("<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>BindDeckLite</title>"
    "<style>body{font-family:sans-serif;max-width:480px;margin:24px auto;padding:0 16px}"
    "input,select{width:100%;padding:6px;box-sizing:border-box}"
    "table{width:100%;border-collapse:collapse}td{padding:3px}</style></head><body>");
  h += F("<h2>BindDeckLite 配置</h2><p>设备: <b>");
  h += g_bleName;
  h += F("</b><br>固件: ");
  h += FW_VERSION;
  h += F(" (");
  h += FW_CHIP;
  h += F(")</p><p>WiFi 状态: ");
  if (WiFi.status() == WL_CONNECTED) {
    h += "已连接 <b>" + htmlEsc(WiFi.SSID()) + "</b> · IP " + WiFi.localIP().toString();
  } else if (ssid.length()) {
    h += "未连接，已保存 <b>" + htmlEsc(ssid) + "</b>";
  } else {
    h += "未配置";
  }
  h += F("</p><form method='POST' action='/save'>"
         "<p><b>WiFi 名称</b></p><input name='ssid' autocomplete='off' value='");
  h += htmlEsc(ssid);
  h += F("'><p><b>WiFi 密码</b></p><input name='pwd' type='password' value='");
  h += htmlEsc(pwd);
  h += F("'><p><button style='padding:10px 20px'>保存并连接</button></p></form>");

  h += F("<hr><h3>按键接线</h3><form method='POST' action='/keys'><table>");
  for (int i = 0; i < NUM_BUTTONS; i++) {
    h += "<tr><td>按键" + String(i + 1) + " GPIO</td><td><input name='pin" + String(i) +
         "' type='number' min='0' max='39' value='" + String(buttonPins[i]) + "'></td></tr>";
  }
  h += F("</table><p><b>编码器模式</b><br><select name='enc'>"
         "<option value='0'>0 音量</option>"
         "<option value='1'>1 缩放 (Ctrl +/-)</option>"
         "<option value='2'>2 浏览器标签页</option>"
         "<option value='3'>3 撤销/重做</option>"
         "<option value='4'>4 应用音量</option></select></p>");
  h += F("<p><b>自动休眠</b> <label><input type='checkbox' name='sleep' value='1'");
  if (sleepEnabled) h += " checked";
  h += F("> 启用</label> 延时(分钟) <input name='sleeptime' type='number' min='1' value='");
  h += String(sleepTimeoutMs / 60000);
  h += F("' style='width:80px'></p><button style='padding:10px 20px'>保存按键配置</button></form>");

  h += F("<hr><p><a href='/update'>→ 固件 OTA 升级（无线刷机）</a></p>"
         "<p style='color:#888;font-size:12px'>设备只在配网模式开热点（按住编码器上电，"
         "或上位机点“进入配网模式”）。离开配网：长按编码器约 1 秒，或等 5 分钟自动退出。</p>"
         "</body></html>");
  return h;
}

static void setupProvisioning() {
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAPConfig(IPAddress(192, 168, 4, 1), IPAddress(192, 168, 4, 1),
                    IPAddress(255, 255, 255, 0));
  WiFi.softAP(g_cfgApName.c_str(), NULL, 6, 4);
  String apIp = WiFi.softAPIP().toString();
  log_e("[cfg] AP '%s' up at http://%s", g_cfgApName.c_str(), apIp.c_str());

  String ssid, pwd;
  loadWifiCreds(ssid, pwd);
  if (ssid.length()) {
    WiFi.begin(ssid.c_str(), pwd.c_str());
    applyTimezone();
    log_e("[cfg] joining saved WiFi '%s'", ssid.c_str());
  }

  cfgServer.on("/", HTTP_GET, []() {
    cfgServer.send(200, "text/html; charset=utf-8", cfgPage());
  });
  cfgServer.on("/save", HTTP_POST, []() {
    String ssid = cfgServer.arg("ssid");
    String pwd  = cfgServer.arg("pwd");
    ssid.trim();
    if (!ssid.length()) {
      cfgServer.send(400, "text/html; charset=utf-8",
                     "<meta charset='utf-8'><p>WiFi 名称不能为空。<a href='/'>返回</a></p>");
      return;
    }
    saveWifiCreds(ssid, pwd);
    WiFi.disconnect(true, true);
    delay(100);
    WiFi.mode(WIFI_AP_STA);
    WiFi.begin(ssid.c_str(), pwd.c_str());
    applyTimezone();
    cfgServer.send(200, "text/html; charset=utf-8",
                   "<meta charset='utf-8'><meta http-equiv='refresh' content='3;url=/'>"
                   "<p>已保存，正在连接 <b>" + htmlEsc(ssid) + "</b>…</p>");
  });
  cfgServer.on("/keys", HTTP_POST, []() {
    int pins[NUM_BUTTONS];
    bool ok = true;
    for (int i = 0; i < NUM_BUTTONS; i++) {
      pins[i] = cfgServer.arg("pin" + String(i)).toInt();
      if (!isValidButtonPin(pins[i])) ok = false;
    }
    if (ok && isValidPinSet(pins)) {
      for (int i = 0; i < NUM_BUTTONS; i++) buttonPins[i] = pins[i];
      applyButtonPins();
      encMode = cfgServer.arg("enc").toInt();
      if (encMode < 0 || encMode > 4) encMode = 0;
      sleepEnabled = (cfgServer.arg("sleep") == "1");
      int mins = cfgServer.arg("sleeptime").toInt();
      if (mins < 1) mins = 5;
      sleepTimeoutMs = (unsigned long)mins * 60000UL;
      saveConfig();
      cfgServer.send(200, "text/html; charset=utf-8",
                     "<meta charset='utf-8'><meta http-equiv='refresh' content='1;url=/'>"
                     "<p>已保存。</p>");
    } else {
      cfgServer.send(400, "text/html; charset=utf-8",
                     "<meta charset='utf-8'><p>引脚无效或重复（避开 6-11、34-39 和编码器引脚）。"
                     "<a href='/'>返回</a></p>");
    }
  });

  // --- OTA (config mode only) ---
  cfgServer.on("/update", HTTP_GET, []() {
    cfgServer.send(200, "text/html; charset=utf-8",
      F("<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>BindDeckLite OTA</title></head><body style='font-family:sans-serif;max-width:440px;"
        "margin:24px auto;padding:0 16px'><h2>固件 OTA 升级</h2>"
        "<p>请选择 <b>app-only</b> 固件（<code>update_apponly_*.bin</code>），"
        "不要上传 merged 整片包（OTA 校验会失败）。</p>"
        "<form method='POST' action='/update' enctype='multipart/form-data'>"
        "<input type='file' name='firmware' accept='.bin' required><br><br>"
        "<button style='padding:10px 20px'>开始升级</button></form>"
        "<p style='color:#888'>升级中请勿断电，完成后自动重启并回到蓝牙模式。</p>"
        "<p><a href='/'>返回</a></p></body></html>"));
  });
  cfgServer.on("/update", HTTP_POST,
    []() {
      String msg = F("<meta charset='utf-8'><p>升级");
      msg += Update.hasError() ? F("失败") : F("完成");
      msg += F("，设备重启中…</p>");
      cfgServer.send(200, "text/html; charset=utf-8", msg);
      delay(500);
      ESP.restart();
    },
    []() {
      HTTPUpload& up = cfgServer.upload();
      if (up.status == UPLOAD_FILE_START) {
        g_otaProgress = 0;
        log_e("[ota] start: %s", up.filename.c_str());
        if (!Update.begin(UPDATE_SIZE_UNKNOWN)) log_e("[ota] Update.begin failed");
      } else if (up.status == UPLOAD_FILE_WRITE) {
        if (up.totalSize > 0) g_otaProgress = (int)(up.currentSize * 100 / up.totalSize);
        if (Update.write(up.buf, up.currentSize) != up.currentSize) log_e("[ota] write failed");
      } else if (up.status == UPLOAD_FILE_END) {
        g_otaProgress = 100;
        if (Update.end(true)) log_e("[ota] done, %u bytes", up.totalSize);
        else                  log_e("[ota] Update.end failed");
      }
    });
  cfgServer.onNotFound([]() {
    cfgServer.sendHeader("Location", "/", true);
    cfgServer.send(302, "text/plain", "");
  });
  cfgServer.begin();
}

static void processCommand(String data);

static void loopWiFi() {
  if (WiFi.status() != WL_CONNECTED) return;
  static bool udpStarted = false;
  if (!udpStarted) { udp.begin(4210); udpStarted = true; }

  int size = udp.parsePacket();
  if (!size) return;
  pcIP = udp.remoteIP();
  pcIpSet = true;
  char buf[255];
  int len = udp.read(buf, 255);
  if (len > 0) {
    buf[len] = 0;
    String data(buf);
    data.trim();
    if (data.length()) processCommand(data);
  }
}

// === Command parser =======================================================
static void processCommand(String data) {
  if (data.startsWith("CFG:PINS:")) {
    // CFG:PINS:13,12,14,27,32,33,25,26,4
    String payload = data.substring(9);
    int pins[NUM_BUTTONS];
    int n = 0;
    while (n < NUM_BUTTONS && payload.length() > 0) {
      int comma = payload.indexOf(',');
      String tok = (comma == -1) ? payload : payload.substring(0, comma);
      tok.trim();
      pins[n++] = tok.toInt();
      if (comma == -1) break;
      payload = payload.substring(comma + 1);
    }
    if (n == NUM_BUTTONS && isValidPinSet(pins)) {
      for (int i = 0; i < NUM_BUTTONS; i++) buttonPins[i] = pins[i];
      applyButtonPins();
      saveConfig();
      log_e("[cfg] pins applied");
    } else {
      log_e("[cfg] pins REJECTED");
    }
  } else if (data.startsWith("CFG:KEYS:")) {
    // CFG:KEYS:<9 button codes>,<encoder push code>
    String payload = data.substring(9);
    int keys[NUM_INPUTS];
    int n = 0;
    while (n < NUM_INPUTS && payload.length() > 0) {
      int comma = payload.indexOf(',');
      String tok = (comma == -1) ? payload : payload.substring(0, comma);
      tok.trim();
      keys[n++] = tok.toInt();
      if (comma == -1) break;
      payload = payload.substring(comma + 1);
    }
    if (n == NUM_INPUTS) {
      for (int i = 0; i < NUM_INPUTS; i++) buttonKeys[i] = keys[i];
      saveConfig();
      log_e("[cfg] keys applied");
    } else {
      log_e("[cfg] keys REJECTED (need %d values)", NUM_INPUTS);
    }
  } else if (data.startsWith("CFG:ENC:")) {
    encMode = data.substring(8).toInt();
    if (encMode < 0 || encMode > 4) encMode = 0;
    saveConfig();
  } else if (data.startsWith("CFG:SLEEP:")) {
    String p = data.substring(10);
    int comma = p.indexOf(',');
    sleepEnabled = (comma != -1) ? p.substring(0, comma).toInt() : 1;
    unsigned long mins = (comma != -1) ? p.substring(comma + 1).toInt() : 5;
    if (mins < 1) mins = 5;
    sleepTimeoutMs = mins * 60000UL;
    saveConfig();
  } else if (data.startsWith("CFG:TZ:")) {
    String tz = data.substring(7);
    tz.trim();
    if (tz.length() > 0 && tz.length() < (int)sizeof(tzString)) {
      strncpy(tzString, tz.c_str(), sizeof(tzString) - 1);
      tzString[sizeof(tzString) - 1] = '\0';
      saveConfig();
      applyTimezone();
    }
  } else if (data.startsWith("CFG:WIFI:")) {
    String p = data.substring(9);
    int bar = p.indexOf('|');
    if (bar != -1) {
      String ssid = p.substring(0, bar);
      String pwd  = p.substring(bar + 1);
      ssid.trim(); pwd.trim();
      saveWifiCreds(ssid, pwd);
      WiFi.disconnect(true, true);
      delay(100);
      WiFi.mode(WIFI_AP_STA);
      WiFi.begin(ssid.c_str(), pwd.c_str());
      applyTimezone();
    }
  } else if (data.startsWith("CMD:VERSION")) {
    sendToHost(String("VERSION:") + FW_VERSION + "," + FW_MODEL + "," + FW_CHIP + "," + FW_BUILD);
  } else if (data.startsWith("CMD:GET_CFG")) {
    String keys;
    for (int i = 0; i < NUM_INPUTS; i++) { if (i) keys += ","; keys += String(buttonKeys[i]); }
    String pins;
    for (int i = 0; i < NUM_BUTTONS; i++) { if (i) pins += ","; pins += String(buttonPins[i]); }
    sendToHost("CFG_KEYS:" + keys);
    sendToHost("CFG_PINS:" + pins);
    sendToHost("CFG_ENC:" + String(encMode));
    sendToHost("CFG_SLEEP:" + String(sleepEnabled ? 1 : 0) + "," + String(sleepTimeoutMs / 60000));
    sendToHost("CFG_TZ:" + String(tzString));
  } else if (data.startsWith("CMD:GET_TZ")) {
    sendToHost("TZ_INFO:" + String(tzString));
  } else if (data.startsWith("CMD:GET_WIFI")) {
    if (WiFi.status() == WL_CONNECTED) {
      String ssid, pwd;
      loadWifiCreds(ssid, pwd);
      sendToHost("WIFI_INFO:" + ssid + "," + WiFi.localIP().toString());
    } else {
      sendToHost("WIFI_INFO:DISCONNECTED,0.0.0.0");
    }
  } else if (data.startsWith("CMD:PING")) {
    sendToHost("PONG:" + String(millis()));
  } else if (data.startsWith("CMD:BOOT_CFG")) {
    // Reboot into the WiFi portal (used by the host app: no menu key on Lite).
    preferences.begin("binddecklite", false);
    preferences.putBool("forceCfg", true);
    preferences.end();
    sendToHost("ACK:" + data.substring(0, 96));
    delay(150);
    ESP.restart();
  } else if (data.startsWith("CMD:REBOOT")) {
    sendToHost("ACK:" + data.substring(0, 96));
    delay(150);
    ESP.restart();
  }

  if (data.startsWith("CFG:")) sendToHost("ACK:" + data.substring(0, 96));
}

static void parseSerialData() {
  if (!Serial.available()) return;
  String data = Serial.readStringUntil('\n');
  data.trim();
  if (data.length()) processCommand(data);
}

// === NVS bring-up (self-healing, same rationale as BindDeck) =============
static void ensureNvs() {
  esp_err_t err = nvs_flash_init();
  if (err == ESP_OK) { g_nvsOk = true; return; }
  log_e("[nvs] init failed (%d) - erasing NVS", (int)err);
  if (nvs_flash_erase() == ESP_OK) {
    g_nvsOk = (nvs_flash_init() == ESP_OK);
  }
  log_e("[nvs] usable=%d", (int)g_nvsOk);
}

static void applyDeviceIdentity() {
  uint8_t mac[6];
  if (esp_efuse_mac_get_default(mac) != ESP_OK) memset(mac, 0, sizeof(mac));
  char suffix[5];
  snprintf(suffix, sizeof(suffix), "%02X%02X", mac[4], mac[5]);
  g_devId = suffix;
  g_bleName = String(FW_NAME_PREFIX) + "-" + g_devId;
  g_cfgApName = g_bleName;
  bleKeyboard.setName(g_bleName.c_str());
  log_e("[id] %s", g_bleName.c_str());
}

// === setup / loop =========================================================
void setup() {
  Serial.begin(115200);
  Serial.setTimeout(10);
#ifdef TARGET_ESP32C3
  // USB CDC: never block the boot if the host is not reading the port.
  Serial.setTxTimeoutMs(0);
#endif

  // Config mode: hold the encoder push while powering on (~200 ms stable).
  pinMode(ENCODER_SW, INPUT_PULLUP);
  delay(5);
  bool swLowAtBoot = (digitalRead(ENCODER_SW) == LOW);

  // Boot-loop guard. RTC memory survives a software reset (but not a power
  // cycle); the magic invalidates it after a real power-on. Four boots without
  // ever reaching 30 s of uptime means something restarts the device, so the
  // next boot skips config mode and the portal gesture and just runs normally.
  if (g_bootMagic != BOOT_MAGIC) {
    g_bootMagic = BOOT_MAGIC;
    g_bootCount = 0;
  }
  g_bootCount++;
  if (g_bootCount >= 4) {
    g_safeMode = true;
    g_bootCount = 0;
  }
  esp_reset_reason_t rr = esp_reset_reason();
  log_e("[boot] reset=%d bootCount=%u safe=%d sw=%d", (int)rr, g_bootCount,
        (int)g_safeMode, (int)swLowAtBoot);

  bool heldAtBoot = false;
  if (!g_safeMode) {
    int low = 0, high = 0;
    for (int i = 0; i < 60 && !heldAtBoot; i++) {
      if (digitalRead(ENCODER_SW) == LOW) {
        if (++low >= 10) heldAtBoot = true;
        high = 0;
      } else {
        if (++high >= 30) break;
        if (high >= 3) low = 0;
      }
      delay(20);
    }
  }

  ensureNvs();
  applyDeviceIdentity();
  loadConfig();

  if (g_nvsOk) {
    preferences.begin("binddecklite", true);
    bool force = preferences.getBool("forceCfg", false);
    preferences.end();
    if (force) {
      preferences.begin("binddecklite", false);
      preferences.putBool("forceCfg", false);
      preferences.end();
      heldAtBoot = true;
      log_e("[cfg] portal requested by host");
    }
  }

  g_configMode = heldAtBoot;
  if (g_configMode) {
    g_configModeStart = millis();
    log_e("[cfg] CONFIG MODE - AP + portal, BLE deferred");
    setupProvisioning();
  } else {
    log_e("[cfg] normal mode - BLE only, WiFi off");
  }

  // Encoder + buttons
  pinMode(ENCODER_CLK, INPUT_PULLUP);
  pinMode(ENCODER_DT, INPUT_PULLUP);
  pinMode(ENCODER_SW, INPUT_PULLUP);
  if (s_encoderTimer == NULL) {
    esp_timer_create_args_t cfg = {
      .callback = &encoderTimerCb,
      .arg = NULL,
      .dispatch_method = ESP_TIMER_TASK,
      .name = "enc"
    };
    esp_timer_create(&cfg, &s_encoderTimer);
    esp_timer_start_periodic(s_encoderTimer, 1000);
  }
  applyButtonPins();
  g_lastActivity = millis();

  // BLE is deferred in config mode: the softAP and BLE share one radio.
  bleConfig.onCommand = [](const String& cmd) {
    log_e("[cfg ble] rx: %s", cmd.c_str());
    processCommand(cmd);
  };
  if (g_nvsOk && !g_configMode) {
    bleKeyboard.begin();
    g_bleStarted = true;
  }
}

void loop() {
  // Reaching 30 s of uptime means the boot was healthy; forget the loop count.
  static bool bootCountCleared = false;
  if (!bootCountCleared && millis() > 30000) {
    bootCountCleared = true;
    g_bootCount = 0;
  }

  // ---- config mode -------------------------------------------------------
  if (g_configMode) {
    cfgServer.handleClient();
    loopWiFi();
    static unsigned long exitHold = 0;
    if (digitalRead(ENCODER_SW) == LOW) {
      if (exitHold == 0) exitHold = millis();
      else if (millis() - exitHold >= CONFIG_EXIT_MS) {
        log_e("[cfg] exit -> BLE mode");
        WiFi.mode(WIFI_OFF);
        g_configMode = false;
        g_lastActivity = millis();
        exitHold = 0;
      }
    } else {
      exitHold = 0;
    }
    if (g_configMode && millis() - g_configModeStart > CONFIG_MODE_MS) {
      log_e("[cfg] timeout -> BLE mode");
      WiFi.mode(WIFI_OFF);
      g_configMode = false;
      g_lastActivity = millis();
    }
  }

  if (!g_configMode && g_nvsOk && !g_bleStarted) {
    applyDeviceIdentity();   // re-assert the name before the deferred begin()
    bleKeyboard.begin();
    g_bleStarted = true;
    log_e("[cfg] BLE started after config mode");
  }

  // ---- buttons -----------------------------------------------------------
  for (int i = 0; i < NUM_BUTTONS; i++) {
    switches[i].update();
    if (switches[i].pressed()) {
      g_lastActivity = millis();
      log_e("[btn] %d (ble=%d)", i, (int)bleKeyboard.isConnected());
      sendKeyForInput(i);
      sendToHost("BTN:" + g_devId + ":" + String(i));
    }
  }

  // ---- encoder push: key + portal gesture --------------------------------
  // The portal gesture is a DOUBLE-CLICK. It is armed only after the pin has
  // been seen idle (HIGH) at least once, so a permanently-LOW pin can never
  // reboot the device in a loop.
  if (!g_configMode) {
    static bool lastRaw = false;
    static bool stable = false;
    static unsigned long rawChangeAt = 0;
    static unsigned long lastPressAt = 0;
    bool raw = (digitalRead(ENCODER_SW) == LOW);
    if (!raw) g_encSeenIdle = true;
    if (raw != lastRaw) { lastRaw = raw; rawChangeAt = millis(); }
    if (millis() - rawChangeAt >= DEBOUNCE_MS && stable != raw) {
      stable = raw;
      if (stable) {
        unsigned long now = millis();
        // Never restart during the first 10 s of a boot: even a wiring fault
        // must not be able to turn the device into a reset loop.
        bool armed = g_encSeenIdle && !g_safeMode && millis() > 10000;
        if (armed && lastPressAt != 0 && (now - lastPressAt) <= DOUBLE_CLICK_MS) {
          log_e("[cfg] encoder double-click -> reboot into portal");
          preferences.begin("binddecklite", false);
          preferences.putBool("forceCfg", true);
          preferences.end();
          delay(100);
          ESP.restart();
        }
        lastPressAt = now;
        g_lastActivity = now;
        log_e("[btn] encoder push");
        sendKeyForInput(NUM_INPUTS - 1);
        sendToHost("BTN:" + g_devId + ":" + String(NUM_INPUTS - 1));
      }
    }
  }

  // ---- encoder rotation --------------------------------------------------
  {
    static int lastSteps = 0;
    int cur = encoderSteps;
    if (cur / 4 != lastSteps / 4) {
      int diff = cur / 4 - lastSteps / 4;
      if (diff > 3) diff = 3;
      if (diff < -3) diff = -3;
      for (int i = 0; i < abs(diff); i++) {
        handleEncoderAction(diff > 0);
        delay(15);
      }
      lastSteps = cur;
      g_lastActivity = millis();
    }
  }

  // ---- keep discoverable for the host app --------------------------------
  {
    static unsigned long lastAdv = 0;
    if (!g_configMode && millis() - lastAdv > 1500) {
      lastAdv = millis();
      bleKeyboard.keepAdvertising();
    }
  }

  // ---- serial CFG (classic ESP32 console / USB) --------------------------
  if (!g_configMode) parseSerialData();

  // ---- deep sleep --------------------------------------------------------
  if (bleKeyboard.isConnected()) g_lastActivity = millis();
  if (!g_configMode && sleepEnabled && (millis() - g_lastActivity >= sleepTimeoutMs)) {
    log_e("[sleep] idle %lums -> deep sleep", sleepTimeoutMs);
    delay(100);
#ifdef TARGET_ESP32C3
    uint64_t mask = 0;
    if (ENCODER_SW <= 5) mask |= (1ULL << ENCODER_SW);
    for (int i = 0; i < NUM_BUTTONS; i++) {
      if (buttonPins[i] >= 0 && buttonPins[i] <= 5) mask |= (1ULL << buttonPins[i]);
    }
    if (mask) esp_deep_sleep_enable_gpio_wakeup(mask, ESP_GPIO_WAKEUP_GPIO_LOW);
#else
    esp_sleep_enable_ext0_wakeup((gpio_num_t)ENCODER_SW, 0);
#endif
    esp_deep_sleep_start();
  }
}
