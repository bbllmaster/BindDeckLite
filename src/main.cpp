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
#include <mbedtls/sha256.h>   // OTA payload integrity (CMD:OTA:<size>:<sha256>)
#include <esp_sleep.h>
#include <esp_timer.h>
#include <esp_log.h>
#include <esp_ota_ops.h>   // esp_ota_get_next_update_partition() (WS-378 diag)
#include <esp_flash.h>    // driver/physical size + write-protect gate (WS-420)
#include <nvs_flash.h>
#include <esp_mac.h>       // esp_read_mac() — Arduino core 3.x / ESP-IDF 5.x
#include <time.h>
#include <vector>         // KeysForm::errors (config portal re-render, WS-408)

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
#define FW_VERSION "1.3.3"   // WS-420: [ota][test] probes + write gate
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

// === OTA (host-driven firmware upgrade, CMD:OTA:<size>:<sha256>) ==========
// While g_otaMode is set the CFG characteristic carries raw firmware bytes and
// the button / encoder / serial paths are skipped. The host sends a continuous
// stream (no per-chunk stop-and-wait): the device acknowledges every
// OTA_ACK_WINDOW bytes so the host can keep a window in flight.
#define OTA_TIMEOUT_MS   30000   // INTER-CHUNK silence → cancel, ERROR:timeout
#define OTA_CHUNK_MAX    512     // hard ceiling = CFG RX characteristic size
#define OTA_ACK_WINDOW   4096    // cumulative bytes between ACKs
#define OTA_SHA_LEN      32

static bool g_otaMode = false;
static unsigned long g_otaStart = 0;      // accepted-at, logging only
static unsigned long g_otaLastData = 0;   // last chunk arrival (inter-chunk timeout)
static uint32_t g_otaTotal = 0;           // expected image size
static uint32_t g_otaReceived = 0;
static uint32_t g_otaAcked = 0;           // last value reported to the host
static uint8_t  g_otaShaExp[OTA_SHA_LEN];
static mbedtls_sha256_context g_otaSha;
static bool g_otaShaActive = false;

// First bytes of the image, collected across one or more BLE writes so the
// app-image header can be validated regardless of the negotiated MTU.
static uint8_t g_otaHdr[0x24];
static size_t g_otaHdrLen = 0;
static bool g_otaHdrOk = false;

// === OTA header capture (WS-378) ===========================================
// The Arduino Update library stashes the first 16 bytes of the image in its
// private _skipBuffer and writes them back to offset 0 of the target slot
// when it verifies the image (Updater.cpp:88). The readback probe below
// repeats that write, so it needs the same 16 bytes.
//
// The library frees _skipBuffer as soon as the verification fails
// (_abort() -> _reset(), Updater.cpp:116-121), so by the time we run the
// probe the bytes are gone. We therefore keep our own copy of the leading
// image bytes while the OTA stream is still arriving.
//
// Note: the bytes are identical to the library's _skipBuffer as long as the
// slot is not flash-encrypted (the library stashes the *decrypted* buffer).
// These boards run without flash encryption, so raw == what was written.
//
// WS-420: the window is 64 bytes, not 16. The first 16 are what the library
// withholds until end() (so the WS-378 diag can replay that write), and
// 16..63 are written with the first 4 KB block — the earliest bytes the
// streaming write path actually puts on the slot, which is what the
// fail-fast read-back in otaVerifyFirstBlock() compares.
static uint8_t g_otaSkip[64];
static size_t g_otaSkipLen = 0;
static bool g_otaSkipOk = false;

static void otaResetSkip() {
  g_otaSkipLen = 0;
  g_otaSkipOk = false;
}

// Collect the leading image bytes. Both OTA paths deliver the image as one
// contiguous stream starting at image offset 0, so the bytes may arrive split
// over several chunks (small BLE MTU) and are accumulated here.
static void otaCaptureSkip(const uint8_t* data, size_t len) {
  if (g_otaSkipOk || !data || !len) return;
  size_t take = sizeof(g_otaSkip) - g_otaSkipLen;
  if (take > len) take = len;
  memcpy(g_otaSkip + g_otaSkipLen, data, take);
  g_otaSkipLen += take;
  if (g_otaSkipLen == sizeof(g_otaSkip)) g_otaSkipOk = true;
}

// === OTA write target (WS-420) =============================================
// Target of the OTA session in flight, kept so the fail-fast read-back
// (otaVerifyFirstBlock) and the end-of-stream diagnostic read back the exact
// partition that was written, on both channels (BLE and config portal).
static const esp_partition_t* g_otaPart = nullptr;
static bool g_otaFirstBlockChecked = false;
// WS-420: why the config-portal upload never got off the ground (gate refusal
// or a lost first block). The POST completion handler reports it instead of
// claiming success.
static const char* g_portalOtaError = nullptr;

// Defined below the OTA helpers, used by the config portal (which is set up
// earlier in the file) and by processCommand().
static bool otaFlashPrepare(const esp_partition_t* part, uint32_t total,
                            const char** reason);
static bool otaVerifyFirstBlock();
static void otaTestRun(const char* trigger, bool withRestart);
static void otaTestOnBoot();

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

// WS-366: deferred-boot request. processCommand() (NimBLE host task) and the
// encoder double-click handler (esp_timer task) must not write NVS or call
// ESP.restart() directly — they only arm this flag. loop() (the only Arduino
// task) performs the NVS write + ACK + restart, serializing with every other
// preferences access and keeping BLE/notify out of unsafe contexts.
enum BootReq : uint8_t { BOOT_NONE = 0, BOOT_CFG = 1, BOOT_REBOOT = 2 };
static BootReq g_bootReq = BOOT_NONE;

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

// Explains why button `idx` cannot use `pin`; an empty string means accepted.
// The accept/reject rule is exactly isValidButtonPin() - this only adds a
// human-readable reason so the portal can name the offending row instead of
// one generic "invalid or duplicate pin" page (WS-408).
static String pinRejectReason(int idx, int pin) {
  String btn = "按键 " + String(idx + 1);
  if (pin < 0 || pin > 39) return btn + " 必须填 0-39 的整数";
  String who = btn + " 的 GPIO " + String(pin);
#ifdef TARGET_ESP32C3
  if (pin > 21) return who + " 在 C3 上不存在（C3 只有 GPIO0-10、20、21）";
  if (pin >= 11 && pin <= 17) return who + " 在 C3 上是内部 Flash 引脚，不能用作按键";
#else
  if (pin >= 6 && pin <= 11) return who + " 是 SPI Flash 引脚（GPIO 6-11），不能用作按键";
  if (pin >= 34) return who + " 是仅输入引脚（GPIO 34-39），没有内部上拉，不能用作按键";
#endif
  if (pin == ENCODER_CLK) return who + " 与编码器 A 相脚（CLK）冲突";
  if (pin == ENCODER_DT)  return who + " 与编码器 B 相脚（DT）冲突";
  if (pin == ENCODER_SW)  return who + " 与编码器按压脚冲突";
  return "";
}

// Strict decimal parse: String::toInt() turns "" / "abc" into 0, which used to
// land silently on GPIO0 and save it.
static bool parsePinText(const String& s, int& out) {
  if (!s.length()) return false;
  unsigned int i = (s[0] == '-' || s[0] == '+') ? 1 : 0;
  if (i >= s.length()) return false;
  long v = 0;
  for (; i < s.length(); i++) {
    char c = s[i];
    if (c < '0' || c > '9') return false;
    v = v * 10 + (c - '0');
    if (v > 9999) return false;
  }
  out = (s[0] == '-') ? (int)-v : (int)v;
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

// Names of the five encoder modes, in encMode order. Index 4 matches the host
// app ("上位机动作", WS-389) so both surfaces show the same wording.
static const char* const ENC_MODE_LABELS[5] = {
  "0 音量", "1 缩放 (Ctrl +/-)", "2 浏览器标签页", "3 撤销/重做", "4 上位机动作"
};

// State of the button-wiring form. Passed to cfgPage() when a /keys POST is
// rejected so the page is re-rendered in place with the submitted values, the
// reasons and the bad rows marked, instead of dropping everything the user
// typed (WS-408).
struct KeysForm {
  String pins[NUM_BUTTONS];            // raw submitted text, echoed verbatim
  bool   bad[NUM_BUTTONS];             // mark the row red / aria-invalid
  std::vector<String> errors;          // reasons for the banner, one per line
  int    enc;                          // encoder mode to keep selected
  bool   sleepEnabled;
  unsigned long sleepMs;

  KeysForm() : enc(0), sleepEnabled(true), sleepMs(300000) {
    for (int i = 0; i < NUM_BUTTONS; i++) bad[i] = false;
  }
};

static String cfgPage(const KeysForm* form = nullptr) {
  String ssid, pwd;
  loadWifiCreds(ssid, pwd);

  String h = F("<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>BindDeckLite</title>"
    "<style>body{font-family:sans-serif;max-width:480px;margin:24px auto;padding:0 16px}"
    "input,select{width:100%;padding:6px;box-sizing:border-box}"
    "table{width:100%;border-collapse:collapse}td{padding:3px}"
    ".err{background:#fdecea;border:1px solid #d93025;color:#8c1d18;padding:8px 10px;"
    "border-radius:6px;margin:0 0 12px;font-size:14px;word-break:break-word}"
    ".err ul{margin:6px 0 0;padding-left:18px}"
    ".bad input{border:2px solid #d93025;background:#fff5f5}"
    "</style></head><body>");
  if (form && !form->errors.empty()) {
    h += F("<div class='err' role='alert'><b>保存失败，未写入设备：</b><ul>");
    for (const String& e : form->errors) {
      h += "<li>";
      h += e;
      h += "</li>";
    }
    h += F("</ul></div>");
  }
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
    bool bad = form && form->bad[i];
    h += bad ? "<tr class='bad'>" : "<tr>";
    h += "<td>按键" + String(i + 1) + " GPIO</td><td><input name='pin" + String(i) +
         "' type='number' min='0' max='39'";
    if (bad) h += " aria-invalid='true'";
    h += " value='";
    h += htmlEsc(form ? form->pins[i] : String(buttonPins[i]));
    h += "'></td></tr>";
  }
  int selEnc = form ? form->enc : encMode;
  if (selEnc < 0 || selEnc > 4) selEnc = 0;
  h += F("</table><p><b>编码器模式</b><br><select name='enc'>");
  for (int m = 0; m < 5; m++) {
    h += "<option value='" + String(m) + "'";
    if (m == selEnc) h += " selected";
    h += ">";
    h += ENC_MODE_LABELS[m];
    h += "</option>";
  }
  h += F("</select></p>");
  bool sleepOn = form ? form->sleepEnabled : sleepEnabled;
  unsigned long sleepMs = form ? form->sleepMs : sleepTimeoutMs;
  h += F("<p><b>自动休眠</b> <label><input type='checkbox' name='sleep' value='1'");
  if (sleepOn) h += " checked";
  h += F("> 启用</label> 延时(分钟) <input name='sleeptime' type='number' min='1' value='");
  h += String(sleepMs / 60000);
  h += F("' style='width:80px'></p><button style='padding:10px 20px'>保存按键配置</button></form>");

  h += F("<hr><p><a href='/update'>→ 固件 OTA 升级（无线刷机）</a></p>"
         "<p style='color:#888;font-size:12px'>设备只在配网模式开热点（按住编码器上电，"
         "或上位机点“进入配网模式”）。离开配网：长按编码器约 1 秒，或等 5 分钟自动退出。</p>"
         "</body></html>");
  return h;
}

static const char* otaUpdateErrorText(int code);  // defined below, used by the
                                                    // config-portal /update handler
static void otaDiagReadback(int code);           // defined below, used by both
                                                    // OTA paths (BLE + config portal)

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
    KeysForm form;
    bool ok = true;

    // Echo back what was submitted (including the encoder/sleep choices) so a
    // rejected save never loses the user's work.
    form.enc = cfgServer.hasArg("enc") ? cfgServer.arg("enc").toInt() : encMode;
    if (form.enc < 0 || form.enc > 4) form.enc = encMode;
    form.sleepEnabled = (cfgServer.arg("sleep") == "1");
    int mins = cfgServer.arg("sleeptime").toInt();
    if (mins < 1) mins = 5;
    form.sleepMs = (unsigned long)mins * 60000UL;

    for (int i = 0; i < NUM_BUTTONS; i++) {
      form.pins[i] = cfgServer.arg("pin" + String(i));
      form.pins[i].trim();
      pins[i] = -1;
      if (!parsePinText(form.pins[i], pins[i])) {
        pins[i] = -1;
        form.bad[i] = true;
        form.errors.push_back("按键 " + String(i + 1) + " 必须填 0-39 的整数");
        ok = false;
        continue;
      }
      String why = pinRejectReason(i, pins[i]);
      if (why.length()) {
        form.bad[i] = true;
        form.errors.push_back(why);
        ok = false;
      }
    }
    // Duplicate pins: one message per clashing pair, both rows marked.
    for (int i = 0; i < NUM_BUTTONS; i++) {
      if (form.bad[i]) continue;
      for (int j = i + 1; j < NUM_BUTTONS; j++) {
        if (form.bad[j] || pins[i] != pins[j]) continue;
        form.bad[i] = form.bad[j] = true;
        form.errors.push_back("按键 " + String(i + 1) + " 与按键 " + String(j + 1) +
                              " 都使用了 GPIO " + String(pins[i]));
        ok = false;
      }
    }

    if (ok && isValidPinSet(pins)) {
      for (int i = 0; i < NUM_BUTTONS; i++) buttonPins[i] = pins[i];
      applyButtonPins();
      // Only touch encMode when the form actually carried it: a field the page
      // did not submit must not silently reset the mode to "0 音量" (WS-408).
      if (cfgServer.hasArg("enc")) {
        encMode = cfgServer.arg("enc").toInt();
        if (encMode < 0 || encMode > 4) encMode = 0;
      }
      sleepEnabled = form.sleepEnabled;
      sleepTimeoutMs = form.sleepMs;
      saveConfig();
      cfgServer.send(200, "text/html; charset=utf-8",
                     "<meta charset='utf-8'><meta http-equiv='refresh' content='1;url=/'>"
                     "<p>已保存。</p>");
    } else {
      cfgServer.send(400, "text/html; charset=utf-8", cfgPage(&form));
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
      if (g_portalOtaError) {
        // WS-420: the gate refused the slot before anything was written. Say
        // so and stay in the portal — reporting "完成" would be a lie.
        msg += F("未开始：");
        msg += g_portalOtaError;
        msg += F("</p><p><a href='/update'>返回</a></p>");
        g_portalOtaError = nullptr;
        cfgServer.send(200, "text/html; charset=utf-8", msg);
        return;
      }
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
        g_portalOtaError = nullptr;
        otaResetSkip();
        log_e("[ota] start: %s", up.filename.c_str());
        // WS-420: same capacity/write-protect gate as the BLE channel. The
        // upload length is not known yet (Update uses UPDATE_SIZE_UNKNOWN), so
        // the gate checks that the whole target slot is addressable.
        const char* gate = "ok";
        if (!otaFlashPrepare(esp_ota_get_next_update_partition(NULL), 0, &gate)) {
          log_e("[ota] portal gate refused: %s", gate);
          g_portalOtaError = gate;
          return;
        }
        if (!Update.begin(UPDATE_SIZE_UNKNOWN)) log_e("[ota] Update.begin failed");
      } else if (up.status == UPLOAD_FILE_WRITE) {
        if (g_portalOtaError) return;   // never write an ungated slot
        if (up.totalSize > 0) g_otaProgress = (int)(up.currentSize * 100 / up.totalSize);
        // up.buf holds the chunk just received; the first one starts the image.
        otaCaptureSkip(up.buf, up.currentSize);
        if (Update.write(up.buf, up.currentSize) != up.currentSize) log_e("[ota] write failed");
        // Same fail-fast read-back as the BLE path (WS-420).
        g_otaReceived = up.currentSize;
        if (!otaVerifyFirstBlock()) {
          log_e("[ota] portal: written bytes are not on the slot, aborting");
          g_portalOtaError = "flash_not_written";
          Update.abort();
        }
      } else if (up.status == UPLOAD_FILE_END) {
        g_otaProgress = 100;
        if (g_portalOtaError) {
          log_e("[ota] portal: not committing (%s)", g_portalOtaError);
        } else if (Update.end(true)) {
          log_e("[ota] done, %u bytes", up.totalSize);
        } else {
          // Same end_failed:<detail>(code) detail as the BLE path so the
          // config-portal channel is diagnosable from CDC logs too.
          int code = Update.getError();
          const char* detail = otaUpdateErrorText(code);
          otaDiagReadback(code);
          log_e("[ota] Update.end failed (end_failed:%s(%d))", detail, code);
        }
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

// === OTA helpers ==========================================================
// Clear every OTA flag without touching the Update partition. Safe to call
// when no session is active.
static void otaResetState() {
  g_otaMode = false;
  g_otaTotal = 0;
  g_otaReceived = 0;
  g_otaAcked = 0;
  g_otaHdrLen = 0;
  g_otaHdrOk = false;
  g_otaPart = nullptr;
  g_otaFirstBlockChecked = false;
  otaResetSkip();
  if (g_otaShaActive) {
    mbedtls_sha256_free(&g_otaSha);
    g_otaShaActive = false;
  }
  bleConfig.setOtaMode(false);
}

// Abort the running session: drop the partial image so the boot partition is
// untouched and the old firmware still boots. Replies ACK:OTA:ERROR:<reason>
// when a reason is given ("" = silent, e.g. the link just disappeared).
static void otaAbort(const char* reason) {
  if (!g_otaMode) return;
  Update.abort();
  log_e("[ota] aborted: %s (%lu/%lu bytes)", reason ? reason : "link lost",
        (unsigned long)g_otaReceived, (unsigned long)g_otaTotal);
  if (reason && *reason) sendToHost(String("ACK:OTA:ERROR:") + reason);
  otaResetState();
}

// Map Update.getError() codes to a short, stable reason token. The host shows
// this verbatim after the `end_failed:` prefix so the user can tell a
// truncated/corrupt image from a flash write failure.
//
// The numeric codes are the UPDATE_ERROR_* macros from <Update.h> (Arduino
// core 3.3.x / ESP-IDF 5.5.5). Switching on the macros instead of bare literals
// keeps the mapping from drifting when the library renumbers its errors:
//   6=STREAM 7=MD5 8=MAGIC_BYTE 9=ACTIVATE 10=NO_PARTITION 11=BAD_ARGUMENT
//   12=ABORT 13=DECRYPT (+14 SIGN / 15 SHA256 / 16 SHA512 on newer cores).
// On this device Update::end() only exercises _enablePartition /
// _partitionIsBootable (READ=3) and esp_ota_set_boot_partition, whose internal
// esp_image_verify fails with ACTIVATE=9, so the only codes a user can actually
// see here are 3 (read-back mismatch) and 9 (image verification / activation).
static const char* otaUpdateErrorText(int code) {
  switch (code) {
    case UPDATE_ERROR_WRITE:        return "write_error";
    case UPDATE_ERROR_ERASE:        return "erase_error";
    case UPDATE_ERROR_READ:         return "read_error";
    case UPDATE_ERROR_SPACE:        return "no_space";
    case UPDATE_ERROR_SIZE:         return "bad_size";
    case UPDATE_ERROR_STREAM:       return "stream";
    case UPDATE_ERROR_MD5:          return "md5";
    case UPDATE_ERROR_MAGIC_BYTE:   return "magic_byte";
    case UPDATE_ERROR_ACTIVATE:     return "activate";
    case UPDATE_ERROR_NO_PARTITION: return "no_partition";
    case UPDATE_ERROR_BAD_ARGUMENT: return "bad_argument";
    case UPDATE_ERROR_ABORT:        return "abort";
    case UPDATE_ERROR_DECRYPT:      return "decrypt";
    default: return "unknown";
  }
}

// === OTA read-back diagnosis (WS-378) =====================================
// Update::end() ends in _verifyEnd() (Updater.cpp:784-789):
//     if (!_enablePartition(_partition) || !_partitionIsBootable(_partition))
//         _abort(UPDATE_ERROR_READ);
// with
//     _enablePartition()    = ESP.partitionWrite(slot, 0, header16, 16)
//     _partitionIsBootable()= ESP.partitionRead(slot, 0, buf, 16) && buf[0]==0xE9
// Both failures collapse into UPDATE_ERROR_READ (3), so the host only ever
// sees "read_error" and cannot tell a dead write channel from a dead read
// channel. This probe repeats the same two calls on the *target* slot only
// and reports which one broke:
//
//   write_failed  - the 16-byte write to offset 0 was rejected
//   read_failed   - the 16-byte read from offset 0 was rejected
//   read_mismatch - the read worked but byte 0 is not the 0xE9 magic
//   readback_ok   - write and read-back agree, the channel itself is fine
//
// The target is esp_ota_get_next_update_partition(NULL) - the same call
// Update::begin() makes (Updater.cpp:264) - so the probe hits the very slot
// the updater was writing to. The running image is never touched: the target
// is compared against the running partition and the probe is skipped when
// they are the same (single-slot boards).
static void otaDiagReadback(int code) {
  if (code != UPDATE_ERROR_READ) return;

  const esp_partition_t* target = esp_ota_get_next_update_partition(NULL);
  if (!target) {
    log_e("[ota][diag] verdict=no_target");
    return;
  }
  const esp_partition_t* running = esp_ota_get_running_partition();
  if (target == running) {
    log_e("[ota][diag] verdict=target_is_running (%s) - skipped", target->label);
    return;
  }
  log_e("[ota][diag] target=%s addr=0x%06X size=0x%05X flash_kb=%u",
        target->label, (unsigned)target->address, (unsigned)target->size,
        (unsigned)(ESP.getFlashChipSize() / 1024));

  // What the updater itself left at offset 0: 0xFF means its header write
  // never landed, 0xE9 means it landed and only the read-back disagreed.
  uint32_t pre[4] = {0, 0, 0, 0};
  bool preOk = ESP.partitionRead(target, 0, pre, sizeof(pre));
  log_e("[ota][diag] updater-left read=%d byte0=0x%02X",
        (int)preOk, preOk ? (unsigned)(pre[0] & 0xFF) : 0u);

  if (!g_otaSkipOk) {
    log_e("[ota][diag] verdict=no_header_bytes");
    return;
  }

  // Repeat the updater's own write, but keep the return value it drops.
  // The bytes are the ones the updater wrote, so this is a bit-identical
  // rewrite and cannot damage the slot beyond what the failed OTA already did.
  uint32_t hdr[4];
  // Only the leading 16 bytes: the library's _skipBuffer and the read-back
  // check are both ENCRYPTED_BLOCK_SIZE wide (WS-420 widened g_otaSkip to 64
  // for the streaming read-back, so bound the copy by the destination).
  memcpy(hdr, g_otaSkip, sizeof(hdr));
  if (!ESP.partitionWrite(target, 0, hdr, sizeof(hdr))) {
    log_e("[ota][diag] verdict=write_failed (16-byte write to offset 0 rejected)");
    return;
  }
  log_e("[ota][diag] write ok=1");

  uint32_t back[4] = {0, 0, 0, 0};
  if (!ESP.partitionRead(target, 0, back, sizeof(back))) {
    log_e("[ota][diag] verdict=read_failed (16-byte read from offset 0 rejected)");
    return;
  }
  unsigned b0 = (unsigned)(back[0] & 0xFF);
  if (b0 != 0xE9) {
    log_e("[ota][diag] verdict=read_mismatch (byte0=0x%02X, expected 0xE9)", b0);
    return;
  }
  log_e("[ota][diag] verdict=readback_ok (byte0=0xE9)");
}

// === OTA flash probes + write gate (WS-420) ===============================
// WS-378 proved the two halves of the failure apart from each other but could
// not tell *why* they disagree, so WS-420 asks the decisive question: does a
// 16-byte pattern written to the OTA slot survive, and does a reboot change
// the answer?
//
// Why the read-back values below are authoritative
// ------------------------------------------------
// ESP.partitionRead() -> esp_partition_read() -> esp_flash_read() ->
// chip_drv->read(), i.e. a direct SPI read issued with the cache disabled
// (esp_flash_api.c: esp_flash_read() -> rom_spiflash_api_funcs->start/end),
// NOT an MMU/cache read. So a 0xFF read back after a write that returned
// ESP_OK means the bytes are not in the array. The one way that argument
// could be wrong is a stale cache line in the *mapped* view, so every
// round-trip here prints the mmap (cache) view next to the SPI view.
//
// The write path, for the same reason, cannot fail silently on a healthy
// part: esp_flash_write() range-checks against chip->size (esp_flash_api.c:
// 1118) and returns the chip driver's error. A write that reports ESP_OK but
// leaves 0xFF therefore points at the chip ignoring the program op (status
// register block protect, WP# pin) or at a part that does not really have the
// capacity the driver believes in.
//
// Every line is one data point with the [ota][test] prefix so the log can be
// pasted back verbatim. Two triggers:
//   * CMD:FLASHTEST            full suite + pattern/restart persistence
//   * CMD:FLASHTEST:NORESTART  same, without the reboot at the end
// and once at boot on large-flash boards (>= 8 MB, i.e. the module class
// this bug is about) so a power cycle alone produces the evidence.
#define OTA_TEST_PATTERN_LEN 16
static const uint8_t OTA_TEST_PATTERN[OTA_TEST_PATTERN_LEN] = {
  0xDE, 0xAD, 0xBE, 0xEF, 0x5A, 0xA5, 0x00, 0xFF,
  0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88,
};
// app1 offsets. 0x0F0000 is the point where the absolute address crosses the
// 4 MB line (0x310000 + 0x0F0000 = 0x400000); 0x200000 is 5.1 MB absolute.
static const uint32_t OTA_TEST_OFFSETS[] = {
  0x000000u, 0x040000u, 0x0E0000u, 0x0F0000u, 0x200000u,
};
#define OTA_TEST_OFFSET_COUNT (sizeof(OTA_TEST_OFFSETS) / sizeof(OTA_TEST_OFFSETS[0]))
#define OTA_TEST_ERASE_LEN   4096u
// Survives ESP.restart() (not a power cycle). Set right before the probe
// reboots; read first thing on the next boot.
#define OTA_TEST_RESTART_MAGIC 0x4207E57u
RTC_NOINIT_ATTR static uint32_t g_otaTestRestart;
// Auto-run only on boards big enough to be the 16 MB C3 case: the round-trip
// below erases 4 KB of app1 five times, which is pointless (and needless wear)
// on the 2 MB / 4 MB builds where OTA already works.
#define OTA_TEST_AUTORUN_MIN_SIZE 0x800000u

// The OTA slot, unless the slot to write would be the running app (single-slot
// boards) — the probes must never touch the firmware that is executing.
static const esp_partition_t* otaTestTarget(const char** why) {
  const esp_partition_t* target = esp_ota_get_next_update_partition(NULL);
  if (!target) { *why = "no_ota_partition"; return nullptr; }
  const esp_partition_t* running = esp_ota_get_running_partition();
  if (target == running) { *why = "target_is_running"; return nullptr; }
  if (target->address + target->size > 0x1000000u) { *why = "target_past_16mb"; return nullptr; }
  *why = "ok";
  return target;
}

// Item 1: capacity / identity. The driver size (chip->size, used for every
// range check) and the physical size (JEDEC/SFDP detect) disagreeing is the
// headline clue: the driver only lets the app address what the configure-time
// flash size said, while the chip may well be larger (or smaller).
static void otaTestDumpIdentity() {
  uint32_t drvSize = 0, physSize = 0, chipId = 0;
  esp_err_t eDrv = esp_flash_get_size(esp_flash_default_chip, &drvSize);
  esp_err_t ePhys = esp_flash_get_physical_size(esp_flash_default_chip, &physSize);
  esp_err_t eId = esp_flash_read_id(esp_flash_default_chip, &chipId);
  bool wp = false;
  esp_err_t eWp = esp_flash_get_chip_write_protect(esp_flash_default_chip, &wp);
  log_e("[ota][test] id drv_size=0x%06X err=%d phys_size=0x%06X err=%d jedec_kb=%u",
        (unsigned)drvSize, (int)eDrv, (unsigned)physSize, (int)ePhys,
        (unsigned)(ESP.getFlashChipSize() / 1024));
  log_e("[ota][test] id chip_id=0x%06X err=%d wp=%d err=%d", (unsigned)chipId,
        (int)eId, (int)wp, (int)eWp);
  if (wp) {
    // esp_flash_get_chip_write_protect() reports SR.WEL, which is 0 (=> "1")
    // whenever no program/erase is in flight, so 1 at rest is normal and is
    // NOT evidence of block protection. The round-trip lines are the evidence.
    log_e("[ota][test] id wp_note=SR.WEL_based_1_at_rest_is_normal");
  }
  if (eDrv == ESP_OK && ePhys == ESP_OK && drvSize != physSize) {
    log_e("[ota][test] id verdict=driver_phys_mismatch drv=0x%06X phys=0x%06X",
          (unsigned)drvSize, (unsigned)physSize);
  }
}

// Item 2 (and item 4): read -> erase 4 KB -> write pattern -> read back at
// each offset, printing both the SPI view (esp_partition_read) and the mapped
// cache view (esp_partition_mmap). "pre" is the first byte before the erase,
// "erased" the first byte right after it: an erase that returns OK without
// changing anything is the write-protect signature.
static void otaTestRoundTrip(const esp_partition_t* part) {
  for (size_t i = 0; i < OTA_TEST_OFFSET_COUNT; i++) {
    uint32_t off = OTA_TEST_OFFSETS[i];
    if (off + OTA_TEST_ERASE_LEN > part->size) {
      log_e("[ota][test] rt off=0x%06X skipped=past_partition_size", (unsigned)off);
      continue;
    }
    uint8_t pre[OTA_TEST_PATTERN_LEN] = {0};
    uint8_t post[OTA_TEST_PATTERN_LEN] = {0};
    uint8_t erased = 0xFF;
    esp_err_t ePre = esp_partition_read(part, off, pre, sizeof(pre));
    esp_err_t eErase = esp_partition_erase_range(part, off, OTA_TEST_ERASE_LEN);
    esp_err_t eErased = esp_partition_read(part, off, &erased, 1);
    esp_err_t eWrite = esp_partition_write(part, off, OTA_TEST_PATTERN, sizeof(OTA_TEST_PATTERN));
    esp_err_t ePost = esp_partition_read(part, off, post, sizeof(post));
    bool match = (ePost == ESP_OK) && memcmp(post, OTA_TEST_PATTERN, sizeof(post)) == 0;
    log_e("[ota][test] rt off=0x%06X abs=0x%06X pre=0x%02X erase_err=%d erased_err=%d "
          "erased=0x%02X write_err=%d read_err=%d post=0x%02X match=%d",
          (unsigned)off, (unsigned)(part->address + off),
          (unsigned)(ePre == ESP_OK ? pre[0] : 0xFF), (int)eErase, (int)eErased,
          (unsigned)erased, (int)eWrite, (int)ePost,
          (unsigned)(ePost == ESP_OK ? post[0] : 0xFF), (int)(match ? 1 : 0));

    // Item 4: same 16 bytes through the MMU/cache. "same=0" with post=0xFF
    // would mean the write landed and only the cache view is stale (W2);
    // "same=1" with post=0xFF means both views agree the bytes are gone (W1).
    const void* mapped = nullptr;
    esp_partition_mmap_handle_t handle = 0;
    esp_err_t eMap = esp_partition_mmap(part, off, sizeof(post), ESP_PARTITION_MMAP_DATA,
                                        &mapped, &handle);
    if (eMap == ESP_OK && mapped) {
      unsigned m0 = (unsigned)((const uint8_t*)mapped)[0];
      log_e("[ota][test] rt off=0x%06X spi=0x%02X mmap=0x%02X same=%d",
            (unsigned)off, (unsigned)(ePost == ESP_OK ? post[0] : 0xFF), m0,
            (int)(m0 == (unsigned)(ePost == ESP_OK ? post[0] : 0xFF)));
      esp_partition_munmap(handle);
    } else {
      log_e("[ota][test] rt off=0x%06X mmap_err=%d", (unsigned)off, (int)eMap);
    }
  }
}

// Item 3: the pattern is on the slot, reboot, and let the next boot say
// whether it is still there. The reboot itself is deferred to loop() (WS-366:
// processCommand runs on the NimBLE host task).
static void otaTestArmPersistence(const esp_partition_t* part) {
  esp_err_t eErase = esp_partition_erase_range(part, 0, OTA_TEST_ERASE_LEN);
  esp_err_t eWrite = esp_partition_write(part, 0, OTA_TEST_PATTERN, sizeof(OTA_TEST_PATTERN));
  uint8_t back[OTA_TEST_PATTERN_LEN] = {0};
  esp_err_t eRead = esp_partition_read(part, 0, back, sizeof(back));
  log_e("[ota][test] persist write off=0x000000 erase_err=%d write_err=%d read_err=%d post=0x%02X",
        (int)eErase, (int)eWrite, (int)eRead, (unsigned)(eRead == ESP_OK ? back[0] : 0xFF));
  g_otaTestRestart = OTA_TEST_RESTART_MAGIC;
}

static void otaTestRun(const char* trigger, bool withRestart) {
  log_e("[ota][test] begin trigger=%s flash_kb=%u", trigger,
        (unsigned)(ESP.getFlashChipSize() / 1024));
  otaTestDumpIdentity();

  const char* why = "ok";
  const esp_partition_t* target = otaTestTarget(&why);
  if (!target) {
    log_e("[ota][test] target=none reason=%s verdict=skipped", why);
    return;
  }
  log_e("[ota][test] target=%s addr=0x%06X size=0x%05X", target->label,
        (unsigned)target->address, (unsigned)target->size);
  otaTestRoundTrip(target);
  if (withRestart) otaTestArmPersistence(target);
  log_e("[ota][test] end");
}

// Boot entry point: report what the previous run left at app1+0 *before*
// anything (including the rest of this probe) touches the slot, then run the
// suite once on large-flash boards.
static void otaTestOnBoot() {
  if (g_otaTestRestart == OTA_TEST_RESTART_MAGIC) {
    g_otaTestRestart = 0;   // consume first: a crash here must not loop
    const char* why = "ok";
    const esp_partition_t* target = otaTestTarget(&why);
    if (!target) {
      log_e("[ota][test] persist_read skipped reason=%s", why);
    } else {
      uint8_t back[OTA_TEST_PATTERN_LEN] = {0};
      esp_err_t err = esp_partition_read(target, 0, back, sizeof(back));
      bool kept = (err == ESP_OK) && memcmp(back, OTA_TEST_PATTERN, sizeof(back)) == 0;
      log_e("[ota][test] persist_read off=0x000000 err=%d byte0=0x%02X match=%d verdict=%s",
            (int)err, (unsigned)(err == ESP_OK ? back[0] : 0xFF), (int)(kept ? 1 : 0),
            kept ? "survived_reboot(W2_data_is_there)" : "lost_across_reboot(W1_data_absent)");
    }
  }
  // Decided on the JEDEC size, not on esp_flash_get_size(): a driver that
  // only believes in 4 MB is exactly one of the failures this probe is for,
  // and it must not be able to switch its own diagnostic off.
  uint32_t jedecSize = (uint32_t)ESP.getFlashChipSize();
  if (jedecSize < OTA_TEST_AUTORUN_MIN_SIZE) {
    log_e("[ota][test] autorun=skipped jedec_kb=%u (below %u KB, use CMD:FLASHTEST)",
          (unsigned)(jedecSize / 1024), (unsigned)(OTA_TEST_AUTORUN_MIN_SIZE / 1024));
    return;
  }
  otaTestRun("boot", false);
}

// Write gate, run before the first OTA byte on both channels (BLE + portal).
// Two things it settles early, while the slot is still untouched:
//   1. capacity: refuse when the target slot is not fully addressable by the
//      flash driver (chip->size), instead of streaming 1.3 MB and failing at
//      the end — this is the `esp_flash_get_size() != 16 MB` case;
//   2. write protection: clear it and log the before/after state.
// It never widens a check and never pretends a write worked. `total` may be 0
// when the channel does not know the length yet (config portal upload).
static bool otaFlashPrepare(const esp_partition_t* part, uint32_t total, const char** reason) {
  *reason = "ok";
  if (!part) { *reason = "no_partition"; return false; }

  const uint32_t partEnd = part->address + part->size;
  const uint32_t imgEnd = total ? (part->address + total) : partEnd;
  uint32_t drvSize = 0, physSize = 0;
  esp_err_t eDrv = esp_flash_get_size(esp_flash_default_chip, &drvSize);
  esp_err_t ePhys = esp_flash_get_physical_size(esp_flash_default_chip, &physSize);
  log_e("[ota][wp] part=%s addr=0x%06X total=%u img_end=0x%06X part_end=0x%06X "
        "drv=0x%06X err=%d phys=0x%06X err=%d",
        part->label, (unsigned)part->address, (unsigned)total, (unsigned)imgEnd,
        (unsigned)partEnd, (unsigned)drvSize, (int)eDrv, (unsigned)physSize, (int)ePhys);
  if (eDrv == ESP_OK && partEnd > drvSize) {
    log_e("[ota][wp] verdict=flash_too_small need=0x%06X drv=0x%06X", (unsigned)partEnd,
          (unsigned)drvSize);
    *reason = "flash_too_small";
    return false;
  }

  bool wp = false;
  esp_err_t eGet = esp_flash_get_chip_write_protect(esp_flash_default_chip, &wp);
  log_e("[ota][wp] protected=%d err=%d", (int)wp, (int)eGet);
  if (eGet == ESP_OK && wp) {
    esp_err_t eSet = esp_flash_set_chip_write_protect(esp_flash_default_chip, false);
    bool after = wp;
    esp_err_t eAfter = esp_flash_get_chip_write_protect(esp_flash_default_chip, &after);
    log_e("[ota][wp] cleared err=%d now=%d err=%d", (int)eSet, (int)after, (int)eAfter);
  }
  g_otaPart = part;
  g_otaFirstBlockChecked = false;
  return true;
}

// Fail-fast check of the *streaming* write path, run once per session after
// the first 4 KB block has been flushed. Returns false when the slot does not
// hold the bytes that were just written; the caller aborts its own channel.
//
// Offset 0 cannot be used here: the Update library deliberately withholds the
// first 16 bytes (_skipBuffer) and only writes them in end(), so the header is
// still 0xFF mid-stream by design. Bytes 16.. are written with the first
// block, so they are the earliest honest sample.
static bool otaVerifyFirstBlock() {
  if (g_otaFirstBlockChecked || !g_otaSkipOk || !g_otaPart) return true;
  // Update buffering: SPI_FLASH_SEC_SIZE (4 KB) per flush, so wait for two
  // blocks to be sure the first one is out.
  if (g_otaReceived < 2 * 4096) return true;
  g_otaFirstBlockChecked = true;

  const size_t off = 16;
  const size_t len = sizeof(g_otaSkip) - off;
  uint8_t back[sizeof(g_otaSkip)] = {0};
  esp_err_t err = esp_partition_read(g_otaPart, off, back, len);
  bool match = (err == ESP_OK) && memcmp(back, g_otaSkip + off, len) == 0;
  log_e("[ota][verify] off=0x%04X addr=0x%06X err=%d sent=0x%02X got=0x%02X match=%d",
        (unsigned)off, (unsigned)(g_otaPart->address + off), (int)err,
        (unsigned)g_otaSkip[off], (unsigned)(err == ESP_OK ? back[0] : 0xFF),
        (int)(match ? 1 : 0));
  if (match) return true;

  // Capacity/identity as seen by the same boot, so the log alone decides
  // whether this is a driver-size problem, a protected part or a dead slot.
  otaTestDumpIdentity();
  log_e("[ota][verify] verdict=first_block_lost - flash accepted the write but the bytes are not there");
  return false;
}

static int otaHexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Parse a 64-char SHA256 hex string into 32 bytes.
static bool otaParseSha256Hex(const String& hex, uint8_t out[OTA_SHA_LEN]) {
  if (hex.length() != OTA_SHA_LEN * 2) return false;
  for (int i = 0; i < OTA_SHA_LEN; i++) {
    int hi = otaHexNibble(hex[i * 2]);
    int lo = otaHexNibble(hex[i * 2 + 1]);
    if (hi < 0 || lo < 0) return false;
    out[i] = (uint8_t)((hi << 4) | lo);
  }
  return true;
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
    // VERSION:<fw>,<model>,<chip>,<build>,<flash_kb>
    // The 5th field (flash size in KB) lets the host size the OTA slot cap
    // precisely (16 MB table → 3 MB slot, 4 MB table → ~1.94 MB slot) without
    // guessing from the chip name. Old hosts ignore the extra field.
    sendToHost(String("VERSION:") + FW_VERSION + "," + FW_MODEL + "," + FW_CHIP
               + "," + FW_BUILD + "," + String(ESP.getFlashChipSize() / 1024));
  } else if (data.startsWith("CMD:GET_CFG")) {
    String keys;
    for (int i = 0; i < NUM_INPUTS; i++) { if (i) keys += ","; keys += String(buttonKeys[i]); }
    String pins;
    for (int i = 0; i < NUM_BUTTONS; i++) { if (i) pins += ","; pins += String(buttonPins[i]); }
    // WS-336: one compact console line with the five values that go out, so a
    // "设备未回报该字段" report can be checked against what the deck actually
    // held without a BLE sniffer. Logging happens before the notifications so
    // the line is on the CDC console even if a notify fails.
    log_e("[cfg] GET_CFG -> keys=%s pins=%s enc=%d sleep=%d,%lu tz=%s",
          keys.c_str(), pins.c_str(), encMode, (int)sleepEnabled,
          (unsigned long)(sleepTimeoutMs / 60000UL), tzString);
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
    // WS-366: defer the NVS write + restart to loop(). This callback runs on the
    // NimBLE host task, where sendToHost()/preferences/ESP.restart() are unsafe
    // and a stale forceCfg write would be lost, leaving the device in normal
    // mode after reboot (no portal). loop() consumes g_bootReq and does the work.
    g_bootReq = BOOT_CFG;
  } else if (data.startsWith("CMD:REBOOT")) {
    // WS-366: same deferral as BOOT_CFG — never restart from the NimBLE task.
    g_bootReq = BOOT_REBOOT;
  } else if (data.startsWith("CMD:CLEARBOND")) {
    // WS-321: drop the persisted host binding so a new central can become the
    // HID primary. The CFG channel stays usable on this same connection.
    bleKeyboard.clearHostBinding();
    sendToHost("ACK:CMD:CLEARBOND");
    log_e("[ble] host binding cleared by CMD:CLEARBOND");
  } else if (data.startsWith("CMD:GET_BOND")) {
    // WS-321: report the current host binding. The address is masked after the
    // first two octets so the full BLE address never leaves the device.
    // WS-423: BOND_STATUS stays exactly `bound,<masked>` / `unbound` (the
    // WS-321 wire format). WS-410 used to comma-append the NimBLE bond state
    // to this line, which broke the old host's `split(",", 1)` — `addr` became
    // `AB12:**:**:**:**,peer_sec=1,nimble_bond=1`. The diagnostics now travel
    // on their own BOND_DIAG line, which old hosts ignore as an unknown line.
    if (bleKeyboard.isHostBound()) {
      uint8_t addr[6];
      if (bleKeyboard.getHostAddr(addr)) {
        char masked[32];
        snprintf(masked, sizeof(masked), "%02X%02X:**:**:**:**",
                 addr[0], addr[1]);
        sendToHost(String("BOND_STATUS:bound,") + masked);
      } else {
        sendToHost("BOND_STATUS:bound,<unavailable>");
      }
      sendToHost(String("BOND_DIAG:peer_sec=") + String(bleKeyboard.bondCount()) +
                 ",nimble_bond=" + (bleKeyboard.isHostBonded() ? "1" : "0"));
    } else {
      sendToHost("BOND_STATUS:unbound");
    }
  } else if (data.startsWith("CMD:OTA_END")) {
    // The host has pushed every byte. Verify the declared size and SHA256
    // before letting Update commit the image: a truncated or tampered stream
    // must never replace the running firmware.
    // NOTE: matched before CMD:OTA — "CMD:OTA_END".startsWith("CMD:OTA").
    if (!g_otaMode) {
      sendToHost("ACK:OTA:ERROR:not_active");
      return;
    }
    if (g_otaReceived != g_otaTotal) {
      log_e("[ota] size mismatch: %lu/%lu", (unsigned long)g_otaReceived,
            (unsigned long)g_otaTotal);
      otaAbort("size");
      return;
    }
    uint8_t digest[OTA_SHA_LEN];
    mbedtls_sha256_finish(&g_otaSha, digest);
    mbedtls_sha256_free(&g_otaSha);
    g_otaShaActive = false;
    if (memcmp(digest, g_otaShaExp, OTA_SHA_LEN) != 0) {
      log_e("[ota] SHA256 mismatch");
      otaAbort("hash");
      return;
    }
    // Report the final confirmed offset before the DONE marker.
    sendToHost("ACK:OTA:" + String(g_otaReceived));
    if (Update.end(true)) {
      log_e("[ota] image verified, rebooting (%lu bytes in %lums)",
            (unsigned long)g_otaReceived, millis() - g_otaStart);
      sendToHost("ACK:OTA:DONE");
      delay(200);
      otaResetState();
      ESP.restart();
    } else {
      // Update.end(true) failed: the image failed ESP-IDF verification
      // (esp_image_verify) or the flash write did. Surface the concrete reason
      // as end_failed:<detail>(code) so the host can tell a corrupt/truncated
      // image apart from a flash error, and old hosts still match the
      // end_failed prefix.
      int code = Update.getError();
      const char* detail = otaUpdateErrorText(code);
      otaDiagReadback(code);
      log_e("[ota] Update.end failed (code %d: %s)", code, detail);
      String reason = String("end_failed:") + detail + "(" + String(code) + ")";
      otaAbort(reason.c_str());
    }
  } else if (data.startsWith("CMD:OTA_CANCEL")) {
    if (!g_otaMode) {
      sendToHost("ACK:OTA:ERROR:not_active");
      return;
    }
    otaAbort(nullptr);
    sendToHost("ACK:OTA:CANCEL");
  } else if (data == "CMD:OTA") {
    // Old (argument-less) form: the host must send size + SHA256 so the image
    // can be verified before it replaces the running firmware.
    sendToHost("ACK:OTA:ERROR:args");
  } else if (data.startsWith("CMD:OTA:")) {
    // Host-driven OTA entry point: CMD:OTA:<total_size>:<sha256_hex>.
    // Only accepted in normal (non-config) mode — the WiFi portal already has
    // its own /update page and OTA here would fight the softAP radio.
    if (g_configMode) {
      sendToHost("ACK:OTA:ERROR:config_mode");
      return;
    }
    if (g_otaMode) {
      sendToHost("ACK:OTA:ERROR:busy");
      return;
    }
    // Authorization: only the bound host that currently owns the HID link may
    // replace the firmware. An unbound deck (no WS-319 binding yet) still
    // requires a live HID primary connection.
    if (!bleKeyboard.isHostBound() || bleKeyboard.primaryConn() < 0) {
      log_e("[ota] rejected: not the bound/primary host (bound=%d primary=%d)",
            (int)bleKeyboard.isHostBound(), bleKeyboard.primaryConn());
      sendToHost("ACK:OTA:ERROR:not_bound");
      return;
    }

    String args = data.substring(8);        // after "CMD:OTA:"
    int colon = args.indexOf(':');
    if (colon <= 0) {
      sendToHost("ACK:OTA:ERROR:args");
      return;
    }
    long total = args.substring(0, colon).toInt();
    String shaHex = args.substring(colon + 1);
    shaHex.trim();
    uint8_t sha[OTA_SHA_LEN];
    if (total <= 0 || total > 4L * 1024 * 1024 || !otaParseSha256Hex(shaHex, sha)) {
      log_e("[ota] bad args (size=%ld sha_len=%u)", total, (unsigned)shaHex.length());
      sendToHost("ACK:OTA:ERROR:args");
      return;
    }

    g_otaMode = true;
    g_otaStart = millis();
    g_otaLastData = g_otaStart;
    g_otaTotal = (uint32_t)total;
    g_otaReceived = 0;
    g_otaAcked = 0;
    g_otaHdrLen = 0;
    g_otaHdrOk = false;
    otaResetSkip();
    memcpy(g_otaShaExp, sha, OTA_SHA_LEN);
    mbedtls_sha256_init(&g_otaSha);
    mbedtls_sha256_starts(&g_otaSha, 0);
    g_otaShaActive = true;
    bleConfig.setOtaMode(true);

    // WS-420: capacity + write-protect gate before a single byte is streamed.
    const char* gate = "ok";
    if (!otaFlashPrepare(esp_ota_get_next_update_partition(NULL), g_otaTotal, &gate)) {
      log_e("[ota] gate refused: %s", gate);
      otaAbort(gate);
      return;
    }

    // Exact size: Update checks it against the target partition up front.
    if (!Update.begin(g_otaTotal)) {
      log_e("[ota] Update.begin(%lu) failed", (unsigned long)g_otaTotal);
      otaAbort("begin_failed");
      return;
    }
    log_e("[ota] armed: %lu bytes, sha256=%s", (unsigned long)g_otaTotal, shaHex.c_str());
    sendToHost("ACK:CMD:OTA");
  } else if (data.startsWith("CMD:FLASHTEST")) {
    // WS-420 flash probes. "CMD:FLASHTEST" runs the full suite and ends with
    // the pattern/reboot persistence experiment; ":NORESTART" keeps the device
    // running. The reboot itself is deferred to loop() (WS-366).
    if (g_otaMode) {
      sendToHost("ACK:OTA:ERROR:busy");
      return;
    }
    bool restart = (data.indexOf("NORESTART") < 0);
    sendToHost("ACK:CMD:FLASHTEST");
    otaTestRun(restart ? "cmd" : "cmd_norestart", restart);
    if (restart) g_bootReq = BOOT_REBOOT;
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
  // ESP-IDF 5.x: esp_efuse_mac_get_default() is no longer the portable entry
  // point for the station MAC; esp_read_mac() covers every target.
  if (esp_read_mac(mac, ESP_MAC_WIFI_STA) != ESP_OK) memset(mac, 0, sizeof(mac));
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

  // WS-420: the flash probe runs before anything else so the first lines of a
  // cold boot are its data points (and so the persistence marker is read
  // before any other code can touch the OTA slot). The short wait is USB-CDC
  // enumeration: without it the first lines fall into the void on the C3.
  delay(250);
  otaTestOnBoot();

  // Config mode: hold the encoder push while powering on (~200 ms stable).
  // WS-321: a 5 s hold is a *separate* gesture that clears the host binding
  // without entering the portal. It takes priority over the 200 ms hold so a
  // user who really wants the portal can release before 5 s.
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
  bool clearBindingAtBoot = false;
  if (!g_safeMode) {
    int low = 0, high = 0;
    int stableLowMs = 0;
    // Up to 6 s: 200 ms debounce + up to 5 s of stable hold.
    for (int i = 0; i < 300; i++) {
      if (digitalRead(ENCODER_SW) == LOW) {
        low++;
        high = 0;
        if (low >= 10) {              // stable low for ~200 ms
          stableLowMs += 20;
          if (stableLowMs >= 5000) {  // 5 s hold -> clear binding only
            clearBindingAtBoot = true;
            break;
          }
        }
      } else {
        if (low >= 10) {              // was stable low, released before 5 s
          heldAtBoot = true;          // -> portal
          break;
        }
        low = 0;
        stableLowMs = 0;
        if (++high >= 30) break;
        if (high >= 3) low = 0;
      }
      delay(20);
    }
  }

  ensureNvs();

  // WS-321: 5 s boot hold clears the host binding. Done after NVS is up (the
  // clear helper only touches NVS) and before BLE begins, so the next HID
  // subscription sees no binding and any central may become the host.
  if (clearBindingAtBoot) {
    log_e("[ble] host binding cleared by boot gesture");
    bleKeyboard.clearHostBinding();
  }

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
  // OTA binary data: the PC writes raw firmware bytes during CMD:OTA.
  bleConfig.onOtaData = [](const uint8_t* data, size_t len) {
    if (!g_otaMode || len == 0) return;

    // Inter-chunk timeout: 30 s without a byte cancels the session.
    if (millis() - g_otaLastData > OTA_TIMEOUT_MS) {
      otaAbort("timeout");
      return;
    }
    g_otaLastData = millis();

    if (len > OTA_CHUNK_MAX) {
      log_e("[ota] chunk too big: %u bytes", (unsigned)len);
      sendToHost("ACK:OTA:ERROR:chunk_too_big");
      return;
    }
    if (g_otaReceived + len > g_otaTotal) {
      log_e("[ota] stream exceeds declared size");
      otaAbort("size");
      return;
    }

    // Keep the first 16 bytes for the WS-378 readback probe: they are the same
    // bytes the Update library stashes in its private _skipBuffer.
    otaCaptureSkip(data, len);

    // App-image header check. A BLE write can be smaller than the 0x24 bytes
    // we need (small MTU), so collect the first bytes across writes and
    // validate as soon as they are complete. A rejection aborts the update,
    // leaving the running firmware in place.
    //   offset 0x00: 0xE9 ESP image magic
    //   offset 0x20: 0xABCD5432 esp_app_desc_t magic (app images only; a
    //                bootloader / merged image has no app descriptor there)
    if (!g_otaHdrOk) {
      size_t need = sizeof(g_otaHdr) - g_otaHdrLen;
      size_t take = (len < need) ? len : need;
      memcpy(g_otaHdr + g_otaHdrLen, data, take);
      g_otaHdrLen += take;
      if (g_otaHdrLen == sizeof(g_otaHdr)) {
        if (g_otaHdr[0] != 0xE9 ||
            g_otaHdr[0x20] != 0x32 || g_otaHdr[0x21] != 0x54 ||
            g_otaHdr[0x22] != 0xCD || g_otaHdr[0x23] != 0xAB) {
          log_e("[ota] not an app image (bad header)");
          otaAbort("not_app_image");
          return;
        }
        g_otaHdrOk = true;
      }
    }

    // UpdateClass::write() takes a non-const buffer but only reads from it,
    // so the const_cast is safe here.
    size_t written = Update.write(const_cast<uint8_t*>(data), len);
    if (written != len) {
      log_e("[ota] Update.write short: %u/%u", (unsigned)written, (unsigned)len);
      otaAbort("write_failed");
      return;
    }
    mbedtls_sha256_update(&g_otaSha, data, len);
    g_otaReceived += len;

    // WS-420: read the first flushed block back instead of trusting the
    // ESP_OK from esp_ota_write(). Fail fast (and say why) rather than
    // streaming the remaining ~1.3 MB into a slot that is not taking writes.
    if (!otaVerifyFirstBlock()) {
      log_e("[ota] aborting: streamed bytes are not on the slot");
      otaAbort("flash_not_written");
      return;
    }

    // Windowed ACK: one notification per OTA_ACK_WINDOW bytes (plus the final
    // byte) instead of one per chunk — the host pipelines and only stalls
    // when this counter stops advancing.
    if (g_otaReceived - g_otaAcked >= OTA_ACK_WINDOW || g_otaReceived == g_otaTotal) {
      g_otaAcked = g_otaReceived;
      sendToHost("ACK:OTA:" + String(g_otaReceived));
    }
  };

  // A drop mid-OTA must abort immediately instead of waiting for the 30 s
  // timeout: the host is gone, so nothing will flush the remaining bytes.
  bleConfig.onDisconnect = []() {
    if (g_otaMode) {
      log_e("[ota] link lost mid-update");
      otaAbort(nullptr);
    }
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

  // ---- deferred boot (WS-366) -------------------------------------------
  // Armed by CMD:BOOT_CFG / CMD:REBOOT (NimBLE task) or the encoder
  // double-click (esp_timer task). Only loop() touches NVS + BLE notify +
  // ESP.restart, so the forceCfg write is serialized with every other
  // preferences access and cannot be lost before reset.
  if (g_bootReq != BOOT_NONE) {
    BootReq r = g_bootReq;
    g_bootReq = BOOT_NONE;
    if (r == BOOT_CFG) {
      preferences.begin("binddecklite", false);
      preferences.putBool("forceCfg", true);
      preferences.end();
      sendToHost("ACK:CMD:BOOT_CFG");
    } else {
      sendToHost("ACK:CMD:REBOOT");
    }
    delay(250);   // let the loop-task ACK/notify flush before reset
    ESP.restart();
  }

  // ---- WS-410: deferred bond/BLE work ----------------------------------
  // Armed by onDisconnect self-heal or CMD:CLEARBOND (both run on the NimBLE
  // task, where an HCI unpair + NVS write is unsafe). Drain it here on the
  // Arduino loop task.
  if (g_bleStarted) {
    processPendingBleWork();
  }

  // ---- OTA mode ----------------------------------------------------------
  // While the host is flashing firmware the CFG characteristic carries binary
  // data (handled by onOtaData on the NimBLE task), so this loop has nothing
  // to do except watch the 30 s timeout and stay out of the way of the
  // buttons / encoder / serial paths.
  if (g_otaMode) {
    // Inter-chunk timeout: the clock restarts on every received write, so a
    // long transfer is fine and only a stalled one is cancelled.
    if (millis() - g_otaLastData > OTA_TIMEOUT_MS) {
      otaAbort("timeout");
    }
    delay(10);
    return;
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
          // WS-366: this runs on the esp_timer task. Only arm the deferred boot;
          // loop() writes forceCfg and restarts from the safe Arduino task.
          g_bootReq = BOOT_CFG;
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
