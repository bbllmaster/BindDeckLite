#include "ble_config.h"

BleConfigService bleConfig;

#include <NimBLEDevice.h>
#include <esp_log.h>

// Distinguish a CFG:/CMD: text line from raw firmware bytes while OTA is
// active. Firmware chunks start with 0xE9 (ESP image magic) and are binary,
// but every chunk after the first is arbitrary data, so the test must not
// rely on the magic alone: a text command is short, starts with CFG:/CMD: and
// contains printable ASCII only. The odds of a firmware chunk clearing that
// bar are negligible, and the size cap keeps a 512-byte block out.
static bool looksLikeTextCommand(const std::string& v) {
  if (v.size() < 4 || v.size() > 64) return false;
  if (v.compare(0, 4, "CMD:") != 0 && v.compare(0, 4, "CFG:") != 0) return false;
  for (unsigned char ch : v) {
    if (ch == '\n' || ch == '\r') continue;
    if (ch < 0x20 || ch > 0x7E) return false;
  }
  return true;
}

// A single write can carry one or more lines; process by line so the PC app can
// either write one command per write or batch several separated by '\n'.
//
// During an active OTA session (CMD:OTA) the same characteristic carries raw
// firmware bytes instead of text lines — the binary image contains NULs and
// newlines, so the line-splitting path is skipped and the payload is handed
// to onOtaData() verbatim. Text commands (CMD:OTA_END / CMD:OTA_CANCEL) must
// still reach onCommand: they are what finalizes or aborts the session.
class CfgRxCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* c, NimBLEConnInfo& /*connInfo*/) override {
    std::string v = c->getValue();
    if (v.empty()) return;

    if (bleConfig.isOtaMode() && bleConfig.onOtaData) {
      if (looksLikeTextCommand(v)) {
        String data(v.c_str());
        data.trim();
        if (data.length() && bleConfig.onCommand) bleConfig.onCommand(data);
        return;
      }
      bleConfig.onOtaData(reinterpret_cast<const uint8_t*>(v.data()), v.size());
      return;
    }

    String data(v.c_str());
    data.trim();
    if (data.length() == 0) return;
    int start = 0;
    while (start < (int)data.length()) {
      int nl = data.indexOf('\n', start);
      String line = (nl == -1) ? data.substring(start) : data.substring(start, nl);
      line.trim();
      if (line.length() && bleConfig.onCommand) bleConfig.onCommand(line);
      if (nl == -1) break;
      start = nl + 1;
    }
  }
};

// NimBLE 2.x no longer exposes a per-characteristic subscriber count, so the
// TX characteristic tracks its own subscription state.
static volatile bool s_txSubscribed = false;

class CfgTxCallbacks : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic* /*ch*/, NimBLEConnInfo& /*connInfo*/, uint16_t subValue) override {
    s_txSubscribed = (subValue & 0x0003) != 0;
  }
};

bool BleConfigService::begin(NimBLEServer* server) {
  if (!server) return false;
  NimBLEService* svc = server->createService(BLE_CFG_SVC_UUID);
  if (!svc) return false;

  // Deliberately NOT encrypted/authenticated. Requiring security here made the
  // Encrypted-only: the device bonds on pairing (the same bond HID uses), so a
  // client must be bonded/encrypted to read or write the config channel. The
  // earlier "Insufficient Authentication" failure was simply an *unbonded*
  // client - pair it first and this works. NOT _AUTHEN/MITM: the board has no
  // display/keypad, so authenticated pairing can never be satisfied.
  NimBLECharacteristic* rx = svc->createCharacteristic(
      BLE_CFG_RX_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_ENC, 512);
  NimBLECharacteristic* tx = svc->createCharacteristic(
      BLE_CFG_TX_UUID,
      NIMBLE_PROPERTY::NOTIFY | NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::READ_ENC, 512);
  rx->setCallbacks(new CfgRxCallbacks());
  tx->setCallbacks(new CfgTxCallbacks());

  // NimBLE 2.x starts all services from NimBLEServer::start(); the per-service
  // start() is a deprecated no-op and is called by the owner after begin().
  _rx = rx;
  _tx = tx;
  ESP_LOGE("BIND", "[cfg] service %s rx=%d tx=%d",
           BLE_CFG_SVC_UUID, rx->getHandle(), tx->getHandle());
  return true;
}

void BleConfigService::notify(const String& data) {
  NimBLECharacteristic* tx = (NimBLECharacteristic*)_tx;
  if (!tx || data.length() == 0) return;
  tx->setValue((const uint8_t*)data.c_str(), data.length());
  tx->notify();
}

bool BleConfigService::subscribed() {
  return s_txSubscribed;
}
