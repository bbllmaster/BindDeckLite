#pragma once
#include <Arduino.h>
#include <functional>

// ---------------------------------------------------------------------------
// Secondary GATT service that carries the same CFG:/CMD: lines the WiFi UDP
// channel uses, but over the existing BLE link - so the PC app can configure
// the device without any WiFi at all. It lives alongside the HID service on
// the same connection (HID is just another GATT service).
//
//   RX characteristic (PC -> device): write "CFG:..." / "CMD:..." lines
//   TX characteristic (device -> PC): notify status / WIFI_INFO / telemetry
//
// The service must be created before the server's services are started, so
// BleKeyboard::begin() creates it on the server it already owns.
// ---------------------------------------------------------------------------

#define BLE_CFG_SVC_UUID "b1d3c0de-0001-4a5b-9c6d-1a2b3c4d5e6f"
#define BLE_CFG_RX_UUID  "b1d3c0de-0002-4a5b-9c6d-1a2b3c4d5e6f"
#define BLE_CFG_TX_UUID  "b1d3c0de-0003-4a5b-9c6d-1a2b3c4d5e6f"

class NimBLEServer;   // forward declaration, keeps this header NimBLE-free

class BleConfigService {
public:
  // Set this before BleKeyboard::begin(). Called for every line the PC writes.
  std::function<void(const String&)> onCommand;
  // OTA binary data: called when the PC writes raw firmware bytes during an
  // active OTA session (see CMD:OTA). The payload is the raw characteristic
  // value — NOT a text line — so it may contain NULs and newlines.
  std::function<void(const uint8_t*, size_t)> onOtaData;
  // Any central disconnected. Used to abort an in-flight OTA immediately
  // instead of waiting for the inter-chunk timeout.
  std::function<void()> onDisconnect;

  bool begin(NimBLEServer* server);   // true if the service was created
  void notify(const String& data);    // device -> PC (no-op if not subscribed)
  bool subscribed();

  // OTA mode flag. While set, onWrite() routes every write to onOtaData()
  // instead of splitting it into CFG:/CMD: text lines.
  void setOtaMode(bool enabled) { _otaMode = enabled; }
  bool isOtaMode() const { return _otaMode; }

private:
  void* _rx = nullptr;   // NimBLECharacteristic*
  void* _tx = nullptr;
  bool _otaMode = false;
};

extern BleConfigService bleConfig;
