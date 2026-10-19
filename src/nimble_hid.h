#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// NimBLE-based HID keyboard + CFG/CMD service, used for ALL targets (classic
// ESP32 + C3). Drop-in replacement for the t-vk BleKeyboard: same class name,
// same KEY_* constants and same call semantics, so the rest of the firmware is
// untouched. The t-vk/Bluedroid stack was ~570KB heavier and, on the C3, could
// not hold a stable bonded+encrypted HID link. This version owns the pairing
// security (bonding, legacy pairing, NO MITM) and is verified on both chips.
// ---------------------------------------------------------------------------

#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>

// --- key codes, identical values to the t-vk library -----------------------
const uint8_t KEY_LEFT_CTRL  = 0x80;
const uint8_t KEY_LEFT_SHIFT = 0x81;
const uint8_t KEY_TAB        = 0xB3;
const uint8_t KEY_F13        = 0xF0;
const uint8_t KEY_F14        = 0xF1;
const uint8_t KEY_F15        = 0xF2;
const uint8_t KEY_F16        = 0xF3;
const uint8_t KEY_F17        = 0xF4;
const uint8_t KEY_F18        = 0xF5;
const uint8_t KEY_F19        = 0xF6;
const uint8_t KEY_F20        = 0xF7;
const uint8_t KEY_F21        = 0xF8;
const uint8_t KEY_F22        = 0xF9;
const uint8_t KEY_F23        = 0xFA;
const uint8_t KEY_F24        = 0xFB;

// Consumer-control report: we send the raw 16-bit HID usage.
struct MediaKeyReport { uint16_t usage; };
const MediaKeyReport KEY_MEDIA_VOLUME_UP   = { 0x00E9 };
const MediaKeyReport KEY_MEDIA_VOLUME_DOWN = { 0x00EA };

class BleKeyboard {
public:
  BleKeyboard(const char* deviceName, const char* deviceManufacturer, uint8_t batteryLevel);

  void   begin();
  bool   isConnected();

  // Set the BLE device name (used for advertising + NimBLEDevice::init). Must be
  // called before begin(); the name is copied into an internal buffer so callers
  // may pass temporaries or string literals. This lets each device expose a
  // unique, MAC-derived name (e.g. "BindDeck-A1B2") at runtime.
  void   setName(const char* name);

  // Called from the main loop: if we are connected but no longer advertising,
  // restart advertising so the device stays discoverable for the config app.
  void   keepAdvertising();
  void   setBatteryLevel(uint8_t level);
  void   setDelay(uint32_t /*ms*/) {}

  size_t press(uint8_t k);
  size_t release(uint8_t k);
  size_t releaseAll();
  size_t write(uint8_t c);
  size_t write(const MediaKeyReport& k);

  // WS-275: connection handle of the "primary" central (the one whose HID
  // report subscription receives our input reports), or -1 when none is set.
  int    primaryConn() const;

private:
  void sendKeyboard();

  char        _nameBuf[32];   // storage for the (runtime-settable) device name
  const char* _name = _nameBuf;
  const char* _man;
  uint8_t     _battery;

  NimBLEServer*         _server       = nullptr;
  NimBLEHIDDevice*      _hid          = nullptr;
  NimBLECharacteristic* _inputKbd     = nullptr;
  NimBLECharacteristic* _inputConsumer = nullptr;

  uint8_t _modifiers = 0;
  uint8_t _keys[6]   = { 0, 0, 0, 0, 0, 0 };
};
