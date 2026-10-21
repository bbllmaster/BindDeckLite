#include "nimble_hid.h"

#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
// WS-275: ble_hs_mbuf_from_flat() / ble_gatts_notify_custom() for directed HID
// reports. Same conditional include NimBLE-Arduino's own headers use.
#if defined(CONFIG_NIMBLE_CPP_IDF)
#include "host/ble_hs.h"
#else
#include "nimble/nimble/host/include/host/ble_hs.h"
#endif
#include <Preferences.h>   // per-deck IRK persistence (WS-274)
#include <esp_log.h>
#include <esp_system.h>    // esp_fill_random()
#include <cstdlib>
#include <cstring>
#include "ble_config.h"   // secondary GATT service for CFG/CMD over BLE

// WS-274: NimBLE ships a hard-coded default IRK (ble_hs_pvcy_default_irk in
// ble_hs_pvcy.c) and ble_hs_startup installs it when the app never sets one.
// Windows resolves a bonded peer by IRK, so every deck looked like the SAME
// identity and a second deck was folded into the first HID node. The setter is
// declared in ble_hs_pvcy_priv.h / ble_hs_pvcy.h, neither of which is on the
// public include path for sketches, so declare it here.
extern "C" int ble_hs_pvcy_set_our_irk(const uint8_t *irk);

// ---------------------------------------------------------------------------
// HID report map: keyboard (report ID 1) + consumer control (report ID 2).
// ---------------------------------------------------------------------------
static const uint8_t HID_REPORT_MAP[] = {
  // ---- Keyboard, Report ID 1 ----
  0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01,
  0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00,
  0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
  0x95, 0x01, 0x75, 0x08, 0x81, 0x01, 0x95, 0x05,
  0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05,
  0x91, 0x02, 0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
  0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65,
  0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
  0xC0,
  // ---- Consumer Control, Report ID 2 ----
  0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x02,
  0x75, 0x10, 0x95, 0x01, 0x15, 0x00, 0x26, 0xFF,
  0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x81, 0x00,
  0xC0
};

// --- ASCII -> HID usage map (copied from the t-vk library, identical) -------
#define SHIFT 0x80
static const uint8_t ASCII_MAP[128] = {
  0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,        // 0x00-0x07
  0x2a,0x2b,0x28,0x00,0x00,0x00,0x00,0x00,        // 0x08-0x0F (BS,TAB,LF)
  0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,        // 0x10-0x17
  0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,        // 0x18-0x1F
  0x2c,0x1e|SHIFT,0x34|SHIFT,0x20|SHIFT,0x21|SHIFT,0x22|SHIFT,0x24|SHIFT,0x34, // 0x20-0x27
  0x26|SHIFT,0x27|SHIFT,0x25|SHIFT,0x2e|SHIFT,0x36,0x2d,0x37,0x38,             // 0x28-0x2F
  0x27,0x1e,0x1f,0x20,0x21,0x22,0x23,0x24,        // 0x30-0x37  (0-7)
  0x25,0x26,0x33|SHIFT,0x33,0x36|SHIFT,0x2e,0x37|SHIFT,0x38|SHIFT, // 0x38-0x3F
  0x1f|SHIFT,0x04|SHIFT,0x05|SHIFT,0x06|SHIFT,0x07|SHIFT,0x08|SHIFT,0x09|SHIFT,0x0a|SHIFT, // @ A-G
  0x0b|SHIFT,0x0c|SHIFT,0x0d|SHIFT,0x0e|SHIFT,0x0f|SHIFT,0x10|SHIFT,0x11|SHIFT,0x12|SHIFT, // H-O
  0x13|SHIFT,0x14|SHIFT,0x15|SHIFT,0x16|SHIFT,0x17|SHIFT,0x18|SHIFT,0x19|SHIFT,0x1a|SHIFT, // P-W
  0x1b|SHIFT,0x1c|SHIFT,0x1d|SHIFT,0x2f,0x31,0x30,0x23|SHIFT,0x2d|SHIFT, // X-Z [ \ ] ^ _
  0x35,0x04,0x05,0x06,0x07,0x08,0x09,0x0a,        // ` a-g
  0x0b,0x0c,0x0d,0x0e,0x0f,0x10,0x11,0x12,        // h-o
  0x13,0x14,0x15,0x16,0x17,0x18,0x19,0x1a,        // p-w
  0x1b,0x1c,0x1d,0x2f|SHIFT,0x31|SHIFT,0x30|SHIFT,0x35|SHIFT,0x00 // x-z { | } ~ DEL
};

// --- connection state -------------------------------------------------------
static volatile bool s_connected = false;

// WS-275: the one central that is allowed to receive HID input reports.
// NimBLECharacteristic::notify() fans a report out to EVERY central subscribed
// to that characteristic, so a phone that connects while the PC holds the HID
// link also received (and acted on) volume/key reports. Remember the first
// central that subscribes to a HID input report and address reports to it only.
// The CFG service (PC app) subscribes to its own TX characteristic and never
// touches these callbacks, so it can neither claim nor lose this slot.
static volatile uint16_t s_hid_conn = BLE_HS_CONN_HANDLE_NONE;

// WS-321: host binding. The identity address of the one central we let
// receive HID input, persisted in NVS so it survives a reboot. A central
// whose address does not match is rejected at HID subscription time; the
// CFG service is unaffected (it never subscribes to HID input reports).
static volatile bool   s_hostBound = false;
static uint8_t         s_hostAddr[6] = { 0, 0, 0, 0, 0, 0 };
// Set when the user clears the binding while a host is still connected. The
// old host keeps working so the user is not left with a dead keyboard, but the
// next *different* central that subscribes takes the HID primary slot over
// immediately - that is what makes "clear, then connect the new PC" work
// without unplugging the old one first.
static volatile bool   s_allowRebind = false;

// Load the persisted host binding into the statics above. Safe to call
// before BLE is up (only touches NVS).
static void loadHostBinding() {
  Preferences p;
  if (!p.begin("binddecklite", true)) return;
  s_hostBound = p.getBool("hostBound", false);
  if (p.getBytesLength("hostAddr") == 6 && p.getBytes("hostAddr", s_hostAddr, 6) == 6) {
    // valid
  } else {
    memset(s_hostAddr, 0, sizeof(s_hostAddr));
    s_hostBound = false;
  }
  p.end();
}

// Persist the host binding. Called once when the first HID subscriber binds.
static void saveHostBinding(const uint8_t addr[6]) {
  Preferences p;
  if (!p.begin("binddecklite", false)) return;
  p.putBytes("hostAddr", addr, 6);
  p.putBool("hostBound", true);
  p.end();
}

// Drop the host binding (CMD:CLEARBOND / 5 s boot gesture). The in-RAM state
// is always cleared, even when NVS is unavailable, so the device still lets a
// new host in for this session.
static void clearHostBinding() {
  Preferences p;
  if (p.begin("binddecklite", false)) {
    p.remove("hostAddr");
    p.putBool("hostBound", false);
    p.end();
  }
  memset(s_hostAddr, 0, sizeof(s_hostAddr));
  s_hostBound = false;
  s_allowRebind = true;
}

// Send one report to exactly one connection. ble_gatts_notify_custom() consumes
// (and frees) the mbuf, on success and on failure alike, so we never free it.
static void notifyOne(uint16_t conn, NimBLECharacteristic* ch, const uint8_t* data, size_t len) {
  if (conn == BLE_HS_CONN_HANDLE_NONE || ch == nullptr || data == nullptr || len == 0) return;
  struct os_mbuf* om = ble_hs_mbuf_from_flat(data, (uint16_t)len);
  if (om == nullptr) return;
  ble_gatts_notify_custom(conn, ch->getHandle(), om);
}

// Return this deck's own 128-bit Identity Resolving Key, generating and
// persisting one on first boot. Without this every deck handed the same
// hard-coded NimBLE default IRK to the OS, which is what made Windows treat
// two BindDecks as one device (WS-274).
static void getOrCreateIrk(uint8_t irk[16]) {
  Preferences p;
  bool prefsOk = p.begin("binddecklite", false);
  bool valid = false;

  if (prefsOk && p.getBytesLength("irk") == 16 && p.getBytes("irk", irk, 16) == 16) {
    valid = true;
    bool allZero = true;
    for (int i = 0; i < 16; i++) {
      if (irk[i]) { allZero = false; break; }
    }
    if (allZero) valid = false;
  }

  if (!valid) {
    esp_fill_random(irk, 16);
    bool allZero = true;
    for (int i = 0; i < 16; i++) {
      if (irk[i]) { allZero = false; break; }
    }
    if (allZero) irk[0] = 0x01;   // astronomically unlikely, but never hand out a zero IRK
    if (prefsOk) {
      p.putBytes("irk", irk, 16);
    }
  }

  if (prefsOk) p.end();
}

// WS-275 + WS-321: HID input-report subscription tracking. Only reports
// (0x0001) or indications (0x0002) in subValue mean "this central wants
// HID input". When a host is bound, only the bound central is allowed;
// every other central is rejected at subscription time.
class HidReportCB : public NimBLECharacteristicCallbacks {
  // NimBLE 2.x passes a NimBLEConnInfo reference instead of the GAP descriptor.
  void onSubscribe(NimBLECharacteristic* ch, NimBLEConnInfo& connInfo, uint16_t subValue) override {
    const uint16_t conn = connInfo.getConnHandle();

    if (!(subValue & 0x0003)) {   // subscription cleared
      if (conn == s_hid_conn) {
        ESP_LOGE("BIND", "[ble] HID primary conn=0x%04X addr=%s unsubscribed -> none",
                 conn, connInfo.getAddress().toString().c_str());
        s_hid_conn = BLE_HS_CONN_HANDLE_NONE;
      }
      return;
    }

    // WS-321: host binding check. When bound, only the central whose identity
    // address matches may receive HID input. The CFG service never subscribes
    // to HID input reports, so the host app keeps working over the same link.
    if (s_hostBound) {
      NimBLEAddress idAddr  = connInfo.getIdAddress();   // peer_id_addr
      NimBLEAddress otaAddr = connInfo.getAddress();     // peer_ota_addr
      const uint8_t* peerAddr = !idAddr.isNull() ? idAddr.getVal() : otaAddr.getVal();

      if (peerAddr && memcmp(peerAddr, s_hostAddr, 6) != 0) {
        ESP_LOGE("BIND", "[ble] reject non-bound host conn=0x%04X addr=%s (bound=%02X%02X%02X%02X%02X%02X)",
                 conn, connInfo.getAddress().toString().c_str(),
                 s_hostAddr[0], s_hostAddr[1], s_hostAddr[2],
                 s_hostAddr[3], s_hostAddr[4], s_hostAddr[5]);
        return;   // reject: never set s_hid_conn for this central
      }

      // Bound host — allow it to become (or stay) the HID primary.
      if (s_hid_conn == BLE_HS_CONN_HANDLE_NONE) {
        s_hid_conn = conn;
        ESP_LOGE("BIND", "[ble] HID primary conn=0x%04X addr=%s (bound host, char=0x%04X sub=0x%04X)",
                 conn, connInfo.getAddress().toString().c_str(),
                 ch ? ch->getHandle() : 0, subValue);
      } else if (conn != s_hid_conn) {
        ESP_LOGE("BIND", "[ble] HID extra subscriber conn=0x%04X addr=%s ignored (primary=0x%04X)",
                 conn, connInfo.getAddress().toString().c_str(), s_hid_conn);
      }
      return;
    }

    // Not bound — the first HID subscriber becomes the host. After an explicit
    // clear (CMD:CLEARBOND / boot gesture) a *different* central may also take
    // the slot over from a host that is still connected, so the user can move
    // the deck to another PC without unplugging the old one first.
    const bool canClaim = (s_hid_conn == BLE_HS_CONN_HANDLE_NONE) ||
                          (s_allowRebind && conn != s_hid_conn);
    if (canClaim) {
      // Persist the identity address (peer_id_addr), falling back to the
      // over-the-air address when identity resolution produced nothing.
      NimBLEAddress idAddr  = connInfo.getIdAddress();
      NimBLEAddress otaAddr = connInfo.getAddress();
      const uint8_t* bindAddr = !idAddr.isNull() ? idAddr.getVal() : otaAddr.getVal();
      if (bindAddr) {
        saveHostBinding(bindAddr);
        memcpy(s_hostAddr, bindAddr, 6);
        s_hostBound = true;
        ESP_LOGE("BIND", "[ble] host bound to %s addr=%02X%02X%02X%02X%02X%02X",
                 !idAddr.isNull() ? "id" : "ota",
                 bindAddr[0], bindAddr[1], bindAddr[2],
                 bindAddr[3], bindAddr[4], bindAddr[5]);
      }
      s_hid_conn = conn;
      s_allowRebind = false;
      ESP_LOGE("BIND", "[ble] HID primary conn=0x%04X addr=%s (char=0x%04X sub=0x%04X)",
               conn, connInfo.getAddress().toString().c_str(),
               ch ? ch->getHandle() : 0, subValue);
    } else if (conn != s_hid_conn) {
      // First subscriber wins for the whole session; a second central (phone)
      // is acknowledged but never receives HID input.
      ESP_LOGE("BIND", "[ble] HID extra subscriber conn=0x%04X addr=%s ignored (primary=0x%04X)",
               conn, connInfo.getAddress().toString().c_str(), s_hid_conn);
    }
  }
};

class HidServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* s, NimBLEConnInfo& /*connInfo*/) override {
    // NimBLE allows several centrals (Windows HID plus the config app). Track
    // the *count*, not a single bool: otherwise closing the config app clears
    // the flag while HID is still up and every button press stops working
    // (WS-271).
    s_connected = s && s->getConnectedCount() > 0;
    // The stack stops advertising when the link comes up. Clear the library's
    // advertising state here, but do NOT restart it from this callback - doing
    // stop+start inside the connection callback races and silently fails.
    // keepAdvertising() (main loop) brings it back a moment later.
    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    if (adv) {
      ESP_LOGE("BIND", "[ble] connected n=%d (wasAdvertising=%d)",
               (int)(s ? s->getConnectedCount() : 0), (int)adv->isAdvertising());
      adv->stop();
    }
  }
  // WS-275: when the primary HID host goes away, stop sending reports until a
  // new central subscribes. Do NOT promote an already-connected second host
  // (the phone that just connected) - that would hand it control exactly when
  // the user put the PC away.
  void onDisconnect(NimBLEServer* s, NimBLEConnInfo& connInfo, int /*reason*/) override {
    // NimBLE removes the peer before invoking this callback, so the count here
    // is "centrals still attached". Only re-advertise once the last one is gone.
    s_connected = s && s->getConnectedCount() > 0;
    if (!s_connected) {
      NimBLEDevice::startAdvertising();
    }
    ESP_LOGE("BIND", "[ble] disconnected n=%d%s",
             (int)(s ? s->getConnectedCount() : 0),
             s_connected ? " (still connected)" : " -> re-advertising");

    if (connInfo.getConnHandle() == s_hid_conn) {
      ESP_LOGE("BIND", "[ble] HID primary conn=0x%04X addr=%s disconnected -> none",
               connInfo.getConnHandle(), connInfo.getAddress().toString().c_str());
      s_hid_conn = BLE_HS_CONN_HANDLE_NONE;
    }
  }
};

BleKeyboard::BleKeyboard(const char* deviceName, const char* deviceManufacturer, uint8_t batteryLevel)
  : _man(deviceManufacturer), _battery(batteryLevel) {
  _nameBuf[0] = '\0';
  setName(deviceName);
}

void BleKeyboard::setName(const char* name) {
  _nameBuf[0] = '\0';
  if (name) {
    strncpy(_nameBuf, name, sizeof(_nameBuf) - 1);
    _nameBuf[sizeof(_nameBuf) - 1] = '\0';
  }
}

void BleKeyboard::begin() {
  NimBLEDevice::init(_name);

  // WS-274: install this deck's unique IRK before the GATT server / pairing
  // exists. init() only returns once the host has synced with the controller,
  // so the HCI commands inside ble_hs_pvcy_set_our_irk() are safe to issue
  // here. Keep sm_our_key_dist = ENC | ID (set in NimBLEDevice::init) so the
  // unique IRK is actually distributed during pairing.
  uint8_t irk[16];
  getOrCreateIrk(irk);
  int irkRc = ble_hs_pvcy_set_our_irk(irk);
  ESP_LOGE("BIND", "[ble] IRK fp=%02X%02X set=%d", irk[0], irk[1], irkRc);

  // WS-321: restore the persisted host binding (if any) before the GATT
  // server starts, so the first HID subscription can be validated against it.
  loadHostBinding();
  ESP_LOGE("BIND", "[ble] host binding: %s addr=%02X%02X%02X%02X%02X%02X",
           s_hostBound ? "bound" : "none",
           s_hostAddr[0], s_hostAddr[1], s_hostAddr[2],
           s_hostAddr[3], s_hostAddr[4], s_hostAddr[5]);

  NimBLEDevice::setMTU(517);                                      // room for long CFG lines
  NimBLEDevice::setPower(9);                                      // +9 dBm, max TX power
  // Bonding + LEGACY pairing, no MITM: the C3 has no display/keypad, and
  // requiring MITM is exactly what made the t-vk build unusable.
  NimBLEDevice::setSecurityAuth(true, false, false);
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);

  _server = NimBLEDevice::createServer();
  _server->setCallbacks(new HidServerCB());

  _hid = new NimBLEHIDDevice(_server);
  _hid->setManufacturer(_man);

  // --- unique HID identity (WS-273) ---------------------------------------
  // Every deck used to expose the exact same VID/PID/version (1234:5678:0100)
  // and a Device Information Service with no serial and no model number.
  // Windows keys its HID device container on that identity, so a second deck
  // was folded into the first one's node and could not be added until the
  // first was removed. Derive the PID from the per-device name suffix
  // ("BindDeck-1BD4" -> 0x1BD4) and publish a unique serial + model string.
  uint16_t uid = 0;
  const char* dash = strrchr(_name, '-');
  if (dash && dash[1]) {
    uid = (uint16_t)strtoul(dash + 1, nullptr, 16);
  }
  const uint16_t pid = (uint16_t)(0x5600 | (uid & 0x00FF));
  _hid->setPnp(0x02, 0x1234, pid, 0x0100);

  NimBLEService* dis = _hid->getDeviceInfoService();   // 0x180A
  NimBLECharacteristic* cModel = dis->createCharacteristic((uint16_t)0x2A24, NIMBLE_PROPERTY::READ);
  cModel->setValue((uint8_t*)_name, strlen(_name));
  NimBLECharacteristic* cSerial = dis->createCharacteristic((uint16_t)0x2A25, NIMBLE_PROPERTY::READ);
  cSerial->setValue((uint8_t*)_name, strlen(_name));

  _hid->setHidInfo(0x00, 0x01);
  _hid->setReportMap((uint8_t*)HID_REPORT_MAP, sizeof(HID_REPORT_MAP));
  _inputKbd      = _hid->getInputReport(1);
  _inputConsumer = _hid->getInputReport(2);
  // WS-275: learn which central is the HID host (the first to subscribe).
  HidReportCB* reportCB = new HidReportCB();
  _inputKbd->setCallbacks(reportCB);
  _inputConsumer->setCallbacks(reportCB);
  _hid->setBatteryLevel(_battery);
  // Create the CFG/CMD service on the same server BEFORE the GATT server starts.
  bool cfgOk = bleConfig.begin(_server);
  ESP_LOGE("BIND", "[ble] cfg service: %s", cfgOk ? "created" : "FAILED");
  // NimBLE 2.x: the server starts every registered service; the per-service
  // start calls and NimBLEHIDDevice::startServices() are deprecated.
  _server->start();
  ESP_LOGE("BIND", "[ble] services started, advertising");

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->setAppearance(HID_KEYBOARD);
  adv->addServiceUUID(_hid->getHidService()->getUUID());
  // NimBLEDevice::init(_name) only sets the GAP Device Name (0x2A00) - it does
  // NOT put the name in the advertising data. Without setName() every deck
  // advertises as an anonymous HID device, Windows cannot tell a second deck
  // from the first one and rejects it with "please try connecting the device
  // again" (WS-272). NimBLE moves the name to the scan response if the
  // advertising packet has no room left.
  adv->setName(_name);
  adv->start();
  ESP_LOGE("BIND", "[ble] advertising name=\"%s\" addr=%s", _name,
           NimBLEDevice::getAddress().toString().c_str());
  ESP_LOGE("BIND", "[ble] HID primary: none (waiting for a HID report subscriber)");
  ESP_LOGE("BIND", "[ble] HID identity: model=\"%s\" serial=\"%s\" pnp=%02X:%04X:%04X:%04X",
           _name, _name, 0x02, 0x1234, pid, 0x0100);
}

bool BleKeyboard::isConnected() { return s_connected; }

void BleKeyboard::keepAdvertising() {
  if (!s_connected) return;
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  if (adv && !adv->isAdvertising()) {
    bool ok = NimBLEDevice::startAdvertising();
    ESP_LOGE("BIND", "[ble] re-advertise while connected -> %d", (int)ok);
  }
}

void BleKeyboard::setBatteryLevel(uint8_t level) {
  _battery = level;
  if (_hid) _hid->setBatteryLevel(level);
}

void BleKeyboard::sendKeyboard() {
  if (!s_connected || !_inputKbd) return;
  uint8_t report[8] = { _modifiers, 0, _keys[0], _keys[1], _keys[2], _keys[3], _keys[4], _keys[5] };
  _inputKbd->setValue(report, sizeof(report));   // keep it readable
  notifyOne(s_hid_conn, _inputKbd, report, sizeof(report));
}

int BleKeyboard::primaryConn() const {
  return (s_hid_conn == BLE_HS_CONN_HANDLE_NONE) ? -1 : (int)s_hid_conn;
}

bool BleKeyboard::isHostBound() const {
  return s_hostBound;
}

bool BleKeyboard::getHostAddr(uint8_t out[6]) const {
  if (!s_hostBound) return false;
  memcpy(out, s_hostAddr, 6);
  return true;
}

void BleKeyboard::clearHostBinding() {
  ::clearHostBinding();   // the file-static helper
}

size_t BleKeyboard::press(uint8_t k) {
  uint8_t i;
  if (k >= 136) {              // non-printing key
    k = k - 136;
  } else if (k >= 128) {       // modifier key
    _modifiers |= (1 << (k - 128));
    k = 0;
  } else {                     // printing key
    k = ASCII_MAP[k];
    if (!k) return 0;
    if (k & 0x80) {            // capital / shifted character
      _modifiers |= 0x02;
      k &= 0x7F;
    }
  }

  if (_keys[0] != k && _keys[1] != k && _keys[2] != k &&
      _keys[3] != k && _keys[4] != k && _keys[5] != k) {
    for (i = 0; i < 6; i++) {
      if (_keys[i] == 0x00) { _keys[i] = k; break; }
    }
    if (i == 6) return 0;
  }
  sendKeyboard();
  return 1;
}

size_t BleKeyboard::release(uint8_t k) {
  if (k >= 136) {
    k = k - 136;
  } else if (k >= 128) {
    _modifiers &= ~(1 << (k - 128));
    k = 0;
  } else {
    k = ASCII_MAP[k];
    if (!k) return 0;
    if (k & 0x80) { _modifiers &= ~0x02; k &= 0x7F; }
  }
  for (uint8_t i = 0; i < 6; i++) {
    if (k && _keys[i] == k) _keys[i] = 0;
  }
  sendKeyboard();
  return 1;
}

size_t BleKeyboard::releaseAll() {
  _keys[0] = _keys[1] = _keys[2] = _keys[3] = _keys[4] = _keys[5] = 0;
  _modifiers = 0;
  sendKeyboard();
  return 1;
}

size_t BleKeyboard::write(uint8_t c) {
  uint8_t p = press(c);
  release(c);
  return p;
}

size_t BleKeyboard::write(const MediaKeyReport& k) {
  if (!s_connected || !_inputConsumer) return 0;
  uint8_t on[2]  = { (uint8_t)(k.usage & 0xFF), (uint8_t)(k.usage >> 8) };
  uint8_t off[2] = { 0, 0 };
  _inputConsumer->setValue(on, sizeof(on));
  notifyOne(s_hid_conn, _inputConsumer, on, sizeof(on));
  delay(10);
  _inputConsumer->setValue(off, sizeof(off));
  notifyOne(s_hid_conn, _inputConsumer, off, sizeof(off));
  return 1;
}
