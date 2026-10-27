#pragma once
//
// WS-432: pure boot policy for the NVS partition.
//
// The guarantee this header encodes: the deck must never erase the NVS
// partition unless an erase can actually fix the failure. NVS holds the BLE
// IRK (the deck's identity), the host binding, peer bonds, the key pins, the
// sleep config, the timezone and the WiFi credentials - a wipe there is what
// forces the user to delete the device in Windows and pair again.
//
// Background (verified against the toolchain, not assumed):
//
//  * The Arduino-ESP32 core calls nvs_flash_init() from initArduino() before
//    setup() runs. In ESP-IDF 5.5.5 a second init for an already-mounted
//    partition is idempotent and returns ESP_OK
//    (NVSPartitionManager::init_partition -> lookup_storage_from_name hit ->
//    `return ESP_OK`), so "the framework got there first" is NOT an error and
//    never reaches the erase path.
//  * ESP_ERR_NVS_ALREADY_OPEN does not exist in ESP-IDF at all - it is not in
//    any 5.5.5 header. Any patch written against it would not compile.
//  * The core itself erases the partition for exactly two codes:
//    ESP_ERR_NVS_NO_FREE_PAGES and ESP_ERR_NVS_NEW_VERSION_FOUND
//    (cores/esp32/esp32-hal-misc.c:336).
//
// The old ensureNvs() erased on EVERY non-ESP_OK result, including codes that
// an erase cannot fix (INVALID_STATE, PART_NOT_FOUND, NO_MEM, FAIL, ...). That
// is the destructive part worth fixing: an unclassified failure must be
// reported, not answered with a wipe.
//
// The decision is a plain function over an int, so it lives here: the firmware
// includes this header, and host/test_nvs_boot_policy.py compiles the VERY
// SAME header with g++ (through host/nvs_boot_cli.cpp, which holds no policy
// logic) so the assertions exercise production code, not a copy of it.

namespace nvsboot {

// esp_err_t values from ESP-IDF 5.5.5 (<esp_err.h> / <nvs.h>). They are part of
// the public ABI, and main.cpp static_asserts them against the real macros, so
// a drift in the IDF headers breaks the build instead of silently changing the
// policy.
static const int ERR_OK = 0x0000;
static const int ERR_FAIL = -1;
static const int ERR_INVALID_STATE = 0x0103;
static const int ERR_NO_MEM = 0x0101;
static const int ERR_NVS_BASE = 0x1100;
static const int ERR_NVS_NOT_INITIALIZED = 0x1101;     // BASE + 0x01
static const int ERR_NVS_NOT_FOUND = 0x1102;           // BASE + 0x02
static const int ERR_NVS_INVALID_STATE = 0x110B;       // BASE + 0x0b
static const int ERR_NVS_NO_FREE_PAGES = 0x110D;       // BASE + 0x0d
static const int ERR_NVS_PART_NOT_FOUND = 0x110F;      // BASE + 0x0f
static const int ERR_NVS_NEW_VERSION_FOUND = 0x1110;   // BASE + 0x10
static const int ERR_NVS_CORRUPT_KEY_PART = 0x1117;    // BASE + 0x17

enum Verdict : unsigned char {
  // Mounted and usable. The data is live - nothing to do.
  VERDICT_USABLE = 0,
  // Unusable, and an erase is the documented remedy (the Arduino core erases
  // for these two codes as well).
  VERDICT_ERASE = 1,
  // Unusable, but erasing cannot help (bad partition table, transient driver
  // state, out of memory, unknown code). Keep the data and report it: wiping
  // user state here costs the user a full re-pair without fixing anything.
  VERDICT_UNUSABLE = 2,
};

// What to do with the result of nvs_flash_init().
inline Verdict classifyInit(int err) {
  if (err == ERR_OK) return VERDICT_USABLE;
  if (err == ERR_NVS_NO_FREE_PAGES || err == ERR_NVS_NEW_VERSION_FOUND) {
    return VERDICT_ERASE;
  }
  return VERDICT_UNUSABLE;
}

// True when the partition may be erased. The firmware guards its single
// nvs_flash_erase() call with this, so no other error path can wipe NVS.
inline bool mayErase(int err) { return classifyInit(err) == VERDICT_ERASE; }

}  // namespace nvsboot
