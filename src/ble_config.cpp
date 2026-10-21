#include "ble_config.h"

BleConfigService bleConfig;

#include <NimBLEDevice.h>
#include <esp_log.h>
#include <vector>   // NimBLEServer::getPeerDevices() (WS-336)

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

  // The value is cached first so a GATT read of the TX characteristic still
  // returns the last line that was sent.
  tx->setValue((const uint8_t*)data.c_str(), data.length());

  NimBLEServer* server = NimBLEDevice::getServer();
  if (!server) return;
  std::vector<uint16_t> peers = server->getPeerDevices();
  if (peers.empty()) return;

  // WS-336: do NOT use tx->notify() (the no-argument form). That path calls
  // ble_gatts_chr_updated(), which only sets ONE "value changed" flag per
  // (connection, characteristic) and reads the characteristic's current value
  // when the NimBLE host task finally runs the pending-notification event.
  // Several sendToHost() calls made inside one write callback therefore
  // collapse into a single notification carrying the LAST value — exactly what
  // broke CMD:GET_CFG, whose five CFG_* lines are emitted back to back (the
  // host only ever saw the final CFG_TZ line and reported the other four
  // fields as "设备未回报该字段").
  //
  // Passing the payload explicitly takes the ble_gatts_notify_custom() path:
  // one ATT notification per call, queued in order, no coalescing.
  const uint8_t* payload = (const uint8_t*)data.c_str();
  const size_t   len     = data.length();
  for (uint16_t conn : peers) {
    if (!tx->notify(payload, len, conn)) {
      ESP_LOGE("BIND", "[cfg] notify failed conn=0x%04X len=%u", conn, (unsigned)len);
    }
  }
}

bool BleConfigService::subscribed() {
  return s_txSubscribed;
}
