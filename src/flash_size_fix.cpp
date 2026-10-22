/*
 * BindDeckLite — override for EspClass::getFlashChipSize()
 *
 * cores/esp32/Esp.cpp:360 (pioarduino core, a PlatformIO dependency not
 * vendored in this repo) has a wrong JEDEC capacity-code formula:
 *
 *   uint32_t EspClass::getFlashChipSize(void) {
 *     uint32_t id = (ESP_getFlashChipId() >> 16) & 0xFF;  // JEDEC capacity code
 *     return 2 << (id - 1);        // BUG: 2^id, real capacity is 2^(id+1)
 *   }
 *
 * For a 16 MB flash (W25Q128JV, JEDEC 0x17) this returns 8 MB instead of
 * 16 MB, which makes the host compute the OTA slot cap as 0x1F0000 (1.9 MB)
 * instead of 0x300000 (3 MB). 4 MB / 2 MB C3 and classic ESP32 are
 * unaffected because their wrong values happen to coincide with their real
 * slot sizes (see WS-378).
 *
 * The correct formula is `2 << id` (= 2^(id+1) = the real flash size). This
 * translation unit provides the corrected definition; platformio.ini links
 * it ahead of the core archive with -Wl,--allow-multiple-definition so the
 * linker picks this one.
 */
#include <Esp.h>

// ESP_getFlashChipId() is defined in the core's Esp.cpp but not declared in
// Esp.h, so repeat its prototype here (it is a plain extern "C" ROM wrapper).
extern uint32_t ESP_getFlashChipId(void);

uint32_t EspClass::getFlashChipSize() {
  uint32_t id = (ESP_getFlashChipId() >> 16) & 0xFF;  // JEDEC capacity code
  return 2 << id;                                      // 2^(id+1) = real flash size
}