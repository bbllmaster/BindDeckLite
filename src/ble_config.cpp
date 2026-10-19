#include "ble_config.h"

BleConfigService bleConfig;

#include <NimBLEDevice.h>
#include <esp_log.h>

// A single write can carry one or more lines; process by line so the PC app can
// either write one command per write or batch several separated by '\n'.
class CfgRxCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* c) override {
    std::string v = c->getValue();
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

  svc->start();
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
  NimBLECharacteristic* tx = (NimBLECharacteristic*)_tx;
  return tx && tx->getSubscribedCount() > 0;
}
