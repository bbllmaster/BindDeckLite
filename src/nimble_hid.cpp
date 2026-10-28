#include "nimble_hid.h"

#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
// WS-275: ble_hs_mbuf_from_flat() / ble_gatts_notify_custom() for directed HID
// reports. Same conditional include NimBLE-Arduino's own headers use.
#if defined(CONFIG_NIMBLE_CPP_IDF)
#include "host/ble_hs.h"
#include "host/ble_store.h"
#else
#include "nimble/nimble/host/include/host/ble_hs.h"
#include "nimble/nimble/host/include/host/ble_store.h"
#endif
#include <Preferences.h>   // per-deck IRK persistence (WS-274)
#include <esp_log.h>
#include <esp_system.h>    // esp_fill_random()
#include <cstdlib>
#include <cstring>
#include "ble_config.h"   // secondary GATT service for CFG/CMD over BLE
// WS-429: pure link policy (identity classification, stale-peer guard,
// while-connected advertising gate). No Arduino/NimBLE dependencies, so the
// host test compiles the very same header with g++.
#include "ble_link_policy.h"

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
// WS-410: full typed identity address of the bound host. s_hostAddr[] above is
// the raw bytes (kept for GET_BOND masking + the memcmp fallback); this object
// also carries the BLE address type so NimBLEDevice::deleteBond() can match the
// bond record exactly (ble_gap_unpair keys on type + value).
static NimBLEAddress   s_hostAddrObj;
// WS-410: deferred bond deletion. ble_gap_unpair() does an HCI unpair + NVS
// write that must not run on the NimBLE host/SM task, so it is armed here and
// drained by processPendingBleWork() from the Arduino loop task.
static NimBLEAddress   s_pendingDeleteAddr;
static bool            s_pendingDeleteBond = false;
// WS-410: CASE=A reset — armed from onDisconnect, drained by loop().
static bool            s_pendingClearBinding = false;
// WS-411 #4: a CLEARBOND issued while a link is up must not tear that link
// down. Remember the old host address and unpair it only after every central
// has gone.
static NimBLEAddress   s_deferredBondAddr;
static bool            s_deleteBondOnDisconnect = false;
// Retry bookkeeping for the deferred bond delete (EBUSY means "advertising
// still active, try again"; a permanent error must not spin the log forever).
static unsigned long   s_lastBondDeleteAttempt = 0;
static int             s_bondDeleteFails = 0;
// Set when the user clears the binding while a host is still connected. The
// old host keeps working so the user is not left with a dead keyboard, but the
// next *different* central that subscribes takes the HID primary slot over
// immediately - that is what makes "clear, then connect the new PC" work
// without unplugging the old one first.
static volatile bool   s_allowRebind = false;

// --- WS-429: connection bookkeeping -----------------------------------------
// The NimBLE callbacks run on the NimBLE host task, the advertising
// supervisor runs on the Arduino loop task. Both need to know how long the
// current links have been up and whether they ever encrypted, so keep one
// record per connection handle.
#define BD_MAX_CONNS 4
struct BdConnRec {
  uint16_t handle;
  uint32_t startMs;
  bool     encrypted;
  bool     used;
};
static BdConnRec s_conns[BD_MAX_CONNS];
// Number of connected peers whose link completed encryption (paired). Only
// the NimBLE callbacks write it; the Arduino loop task only reads it.
static int s_encryptedConns = 0;

// --- WS-429: stale-peer guard + advertising backoff -------------------------
// The strike counter itself is pure policy state (src/ble_link_policy.h), so
// the host test can drive it. Here we only keep the radio-side bookkeeping.
static blepol::StalePeer s_stale;
// Set while a pre-encryption teardown backoff is running: the radio stays
// quiet so the failing peer cannot reconnect every 200 ms.
static volatile bool s_advBackoff = false;
// ms timestamp (millis()) at which advertising may resume.
static uint32_t      s_advResumeAt = 0;
// The device name, for the HOST_STALE_KEY conclusion.
static char          s_deviceName[32] = { 0 };

static BdConnRec* connRec(uint16_t handle) {
  for (int i = 0; i < BD_MAX_CONNS; i++) {
    if (s_conns[i].used && s_conns[i].handle == handle) return &s_conns[i];
  }
  return nullptr;
}

static void connRecAdd(uint16_t handle) {
  BdConnRec* r = connRec(handle);
  if (r == nullptr) {
    for (int i = 0; i < BD_MAX_CONNS; i++) {
      if (!s_conns[i].used) { r = &s_conns[i]; break; }
    }
  }
  if (r == nullptr) r = &s_conns[0];   // table full: reuse the first slot
  r->handle = handle;
  r->startMs = (uint32_t)millis();
  r->encrypted = false;
  r->used = true;
}

// Age of the oldest connected peer that never encrypted. UINT32_MAX when
// every peer is encrypted (nothing to wait for).
static uint32_t oldestUnencryptedAgeMs() {
  uint32_t oldest = 0;
  bool found = false;
  const uint32_t now = (uint32_t)millis();
  for (int i = 0; i < BD_MAX_CONNS; i++) {
    if (!s_conns[i].used || s_conns[i].encrypted) continue;
    const uint32_t age = now - s_conns[i].startMs;
    if (!found || age > oldest) { oldest = age; found = true; }
  }
  return found ? oldest : UINT32_MAX;
}

// WS-429: keep the radio quiet for `ms` so the peer that keeps failing before
// encryption stops reconnecting every 200 ms. Advertising resumes
// automatically when the deadline expires (serviceAdvertising) or right away
// when the user touches the deck (notifyUserActivity).
static void armAdvBackoff(uint32_t ms) {
  s_advBackoff = true;
  s_advResumeAt = (uint32_t)millis() + ms;
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  if (adv && adv->isAdvertising()) adv->stop();
  ESP_LOGE("BIND", "[ble] stale-peer backoff %lums -> radio quiet (advertising resumes on expiry)",
           (unsigned long)ms);
}

static void clearAdvBackoff() {
  s_advBackoff = false;
  s_advResumeAt = 0;
}

// The peer address used to match a peer across connections: the identity
// address when it resolved (stable across reconnects), the over-the-air
// address otherwise. The bytes are COPIED into `out` on purpose:
// NimBLEAddress::getVal() points into the address object itself, and the
// local copies below would be gone when this function returns.
static bool peerKey(NimBLEConnInfo& connInfo, uint8_t out[6]) {
  NimBLEAddress idAddr = connInfo.getIdAddress();
  if (!idAddr.isNull()) {
    memcpy(out, idAddr.getVal(), 6);
    return true;
  }
  NimBLEAddress otaAddr = connInfo.getAddress();
  if (otaAddr.isNull()) return false;
  memcpy(out, otaAddr.getVal(), 6);
  return true;
}

// WS-429: a HID subscription means the deck is really being used, so the
// stale-peer strikes are over. Clear them and lift the radio backoff.
static void noteHidSubscriber() {
  blepol::staleReset(&s_stale);
  clearAdvBackoff();
}

// Load the persisted host binding into the statics above. Safe to call
// before BLE is up (only touches NVS).
static void loadHostBinding() {
  Preferences p;
  if (!p.begin("binddecklite", true)) return;
  s_hostBound = p.getBool("hostBound", false);
  if (p.getBytesLength("hostAddr") == 6 && p.getBytes("hostAddr", s_hostAddr, 6) == 6) {
    // valid
    // WS-410: rebuild the typed address. The stored identity address type is
    // not persisted; a bonded Windows host uses a PUBLIC identity address, so
    // BLE_ADDR_PUBLIC is the correct best-effort default. The live type is
    // re-learned on the next HID subscription (which sees the resolved peer).
    s_hostAddrObj = NimBLEAddress(s_hostAddr, BLE_ADDR_PUBLIC);
  } else {
    memset(s_hostAddr, 0, sizeof(s_hostAddr));
    s_hostBound = false;
    s_hostAddrObj = NimBLEAddress();
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
  s_hostAddrObj = NimBLEAddress();
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
//
// WS-429: `wasPresent` reports whether a usable IRK was found in NVS. When it
// is false we minted a NEW identity, so every pairing the host still holds
// refers to an identity this deck no longer has.
static void getOrCreateIrk(uint8_t irk[16], bool* wasPresent) {
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
  if (wasPresent) *wasPresent = valid;

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

// WS-410 (review must-fix #3): the HID-subscriber gate. A bound host may only
// be accepted when its RESOLVED IDENTITY matches the stored binding. `peerBonded`
// (connInfo.isBonded() == sec_state.bonded) must never be sufficient on its own:
// a freshly-paired second PC is also "bonded" to us, and letting it through
// would regress WS-319. Kept as a constexpr so static_assert can guard it.
static constexpr bool acceptHidSubscriber(bool identityMatches, bool peerBonded) {
  (void)peerBonded;   // deliberately unused — see above
  return identityMatches;
}
static_assert(acceptHidSubscriber(true, false),  "identity match must accept the bound host");
static_assert(acceptHidSubscriber(true, true),   "identity match must accept the bound host");
static_assert(!acceptHidSubscriber(false, true), "bonded-but-foreign host must be rejected (WS-319)");
static_assert(!acceptHidSubscriber(false, false), "unknown host must be rejected");

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

    // WS-321 / WS-410(C): host binding check. When bound, only the bound
    // central may receive HID input. The CFG service never subscribes to HID
    // input reports, so the host app keeps working over the same link.
    //
    // WS-410 (review must-fix #3): gate on the RESOLVED identity address ONLY.
    // `connInfo.isBonded()` is just sec_state.bonded — a bond exists for *some*
    // device we paired with, and a freshly-paired second PC is "bonded" too, so
    // trusting it would let that PC grab HID (WS-319 regression). Identity match
    // is the only signal that proves "this is the one host I bound".
    // Case A (our bond was lost -> identity cannot resolve) is not handled here;
    // onDisconnect resets the app binding on a bond-key failure so the peer can
    // re-pair and bind again, which is why identity-only matching cannot lock
    // the legitimate host out permanently.
    if (s_hostBound) {
      NimBLEAddress idAddr  = connInfo.getIdAddress();   // peer_id_addr
      NimBLEAddress otaAddr = connInfo.getAddress();     // peer_ota_addr
      const bool peerBonded = connInfo.isBonded();
      const bool idMatch = !idAddr.isNull() &&
                           memcmp(idAddr.getVal(), s_hostAddr, 6) == 0;

      if (acceptHidSubscriber(idMatch, peerBonded)) {
        // Bound host — allow it to become (or stay) the HID primary.
        noteHidSubscriber();   // WS-429: a working HID host clears the strikes
        if (s_hid_conn == BLE_HS_CONN_HANDLE_NONE) {
          s_hid_conn = conn;
          ESP_LOGE("BIND", "[ble] HID primary conn=0x%04X addr=%s (bound host,id-match,bonded=%d, char=0x%04X sub=0x%04X)",
                   conn, otaAddr.toString().c_str(), (int)peerBonded,
                   ch ? ch->getHandle() : 0, subValue);
        } else if (conn != s_hid_conn) {
          ESP_LOGE("BIND", "[ble] HID extra subscriber conn=0x%04X addr=%s ignored (primary=0x%04X)",
                   conn, otaAddr.toString().c_str(), s_hid_conn);
        }
        return;
      }

      // Did NOT match the bound host. Distinguish the two failure modes so the
      // log makes the root cause obvious:
      //  - idAddr null  -> bound host's RPA could not be resolved to its
      //                    identity (bond store / IRK mismatch, case C or B).
      //  - idAddr set   -> a genuinely different central tried to grab HID
      //                    (rejected on purpose, WS-319 holds).
      if (idAddr.isNull()) {
        ESP_LOGE("BIND", "[ble] reject CASE=C: bound host identity NOT resolved (RPA unresolved), "
                 "subscriber ota=%s (bound=%02X%02X%02X%02X%02X%02X)",
                 otaAddr.toString().c_str(),
                 s_hostAddr[0], s_hostAddr[1], s_hostAddr[2],
                 s_hostAddr[3], s_hostAddr[4], s_hostAddr[5]);
      } else {
        ESP_LOGE("BIND", "[ble] reject non-bound host conn=0x%04X ota=%s id=%s (bound=%02X%02X%02X%02X%02X%02X)",
                 conn, otaAddr.toString().c_str(), idAddr.toString().c_str(),
                 s_hostAddr[0], s_hostAddr[1], s_hostAddr[2],
                 s_hostAddr[3], s_hostAddr[4], s_hostAddr[5]);
        // WS-419b: rejecting the subscription is not enough - the link was
        // left up, so the stranger stayed paired with full access to the CFG
        // channel. Drop the link. Only when the identity really resolved: the
        // CASE=C branch above is the bound host with an unresolvable RPA and
        // must be left to the self-heal path.
        //
        // The peer's bond is deliberately kept - see onAuthenticationComplete
        // for why unpairing here would wipe the real host's binding.
        // Reason must be a plain "local host closed the link". 0x05
        // (AUTH_FAIL) reads as "wrong PIN / passkey" on some Android stacks and
        // as a normal close on others - the same firmware produced different
        // phone-side messages depending on the vendor's interpretation.
        if (NimBLEServer* srv = NimBLEDevice::getServer()) {
          srv->disconnect(conn, BLE_ERR_CONN_TERM_LOCAL);
        }
      }
      return;   // reject: never set s_hid_conn for this central
    }

    // Not bound — the first HID subscriber becomes the host. After an explicit
    // clear (CMD:CLEARBOND / boot gesture) a *different* central may also take
    // the slot over from a host that is still connected, so the user can move
    // the deck to another PC without unplugging the old one first.
    const bool canClaim = (s_hid_conn == BLE_HS_CONN_HANDLE_NONE) ||
                          (s_allowRebind && conn != s_hid_conn);
    if (canClaim) {
      // WS-429: the deck is in use -> clear the stale-peer strikes.
      noteHidSubscriber();
      // Persist the identity address (peer_id_addr), falling back to the
      // over-the-air address when identity resolution produced nothing.
      NimBLEAddress idAddr  = connInfo.getIdAddress();
      NimBLEAddress otaAddr = connInfo.getAddress();
      const uint8_t* bindAddr = !idAddr.isNull() ? idAddr.getVal() : otaAddr.getVal();
      if (bindAddr) {
        saveHostBinding(bindAddr);
        memcpy(s_hostAddr, bindAddr, 6);
        // WS-410: keep the typed identity address so deleteBond() can match the
        // bond record by type + value. Prefer the resolved identity address.
        s_hostAddrObj = !idAddr.isNull() ? idAddr : otaAddr;
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

// WS-410: map a NimBLE disconnect reason to a short human-readable name for
// the on-site log. `reason` arrives in the ble_hs error space: HCI disconnect
// codes are wrapped as BLE_HS_HCI_ERR(x) = 0x200 + x, SM (pairing) failures as
// 0x500 + x. 0x208 (supervision timeout = walk away / power off) and
// 0x213/0x214/0x216 are NORMAL closes and must never be read as a broken bond.
static const char* reasonName(int reason) {
  switch (reason) {
    case 0x205: return "HCI_AUTH_FAIL";        // 0x05 authentication failure
    case 0x206: return "HCI_PIN_KEY_MISSING";  // 0x06 PIN or key missing (broken bond)
    case 0x208: return "HCI_CONN_TIMEOUT";     // 0x08 connection timeout
    case 0x213: return "HCI_REMOTE_TERMINATED";// 0x13 remote user terminated (normal)
    case 0x214: return "HCI_REMOTE_POWER_OFF"; // 0x14 remote device power off (normal)
    case 0x216: return "HCI_LOCAL_TERMINATED"; // 0x16 local host terminated (normal)
    case 0x23E: return "HCI_CONN_FAIL_ESTAB";  // 0x3E connection failed to be established
    case 25:    return "BLE_HS_EENCRYPT";      // encryption failed
    default:    return "OTHER";
  }
}

// WS-410 (review must-fix #2): the ONLY reasons that mean "the bond key itself
// is wrong". 0x208 supervision timeout (walk away / power off), 0x213/0x214/
// 0x216 (user/power/local close) and 0x23E are explicitly excluded — wiping a
// bond on those would turn the original bug into an always-on one.
static constexpr bool isSecurityFailureReason(int reason) {
  switch (reason) {
    case 0x205:  // HCI authentication failure
    case 0x206:  // HCI PIN or key missing  (classic broken-bond code)
    case 25:     // BLE_HS_EENCRYPT
      return true;
    default:
      // SM (peer) pairing failures: 0x500 + code.
      return (reason >= 0x500 && reason < 0x600);
  }
}

// WS-410 (review should-fix #5): a stricter subset that specifically proves the
// peer tried to use a bond it no longer shares with us — i.e. it is our former
// host, not a stranger failing a fresh pairing. Used for the case-A reset.
static constexpr bool isBondKeyFailureReason(int reason) {
  return reason == 0x205 || reason == 0x206 || reason == 25;
}

// WS-410 regression guard (review must-fix #2): these are compile-time checks
// against the REAL classifiers above, so re-adding 0x208 (walk away) or a
// normal-close code to the "wipe bond" set fails the build instead of silently
// turning a healthy disconnect into a data-loss bug. `pio run -e esp32dev` is
// the regression command: it fails if any assertion below is violated.
static_assert(isSecurityFailureReason(0x205), "0x205 auth fail must self-heal");
static_assert(isSecurityFailureReason(0x206), "0x206 pin/key missing must self-heal");
static_assert(isSecurityFailureReason(25),    "BLE_HS_EENCRYPT must self-heal");
static_assert(!isSecurityFailureReason(0x208), "0x208 supervision timeout (walk away) must NOT wipe bond");
static_assert(!isSecurityFailureReason(0x213), "0x213 remote user terminated must NOT wipe bond");
static_assert(!isSecurityFailureReason(0x214), "0x214 remote power off must NOT wipe bond");
static_assert(!isSecurityFailureReason(0x216), "0x216 local terminated must NOT wipe bond");
static_assert(!isSecurityFailureReason(0x23E), "0x23E conn failed to establish must NOT wipe bond");
static_assert(isBondKeyFailureReason(0x206),  "case-A reset on pin/key missing");
static_assert(!isBondKeyFailureReason(0x208), "case-A reset never on timeout");

class HidServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* s, NimBLEConnInfo& connInfo) override {
    // NimBLE allows several centrals (Windows HID plus the config app). Track
    // the *count*, not a single bool: otherwise closing the config app clears
    // the flag while HID is still up and every button press stops working
    // (WS-271).
    s_connected = s && s->getConnectedCount() > 0;
    // WS-429: remember when this link came up so the teardown can tell a
    // rapid pre-encryption drop from an ordinary disconnect.
    connRecAdd(connInfo.getConnHandle());
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

  // WS-429: the link reached encryption - the peer paired with us and can
  // talk to the deck. Remember it (the advertising gate needs it) and clear
  // the strikes of that peer: it is not a suspect any more.
  void onAuthenticationComplete(NimBLEConnInfo& connInfo) override {
    BdConnRec* r = connRec(connInfo.getConnHandle());
    if (r != nullptr && !r->encrypted) {
      r->encrypted = true;
      s_encryptedConns++;
    }
    uint8_t key[6];
    if (peerKey(connInfo, key) && blepol::stalePeerSucceeded(&s_stale, key)) {
      clearAdvBackoff();
    }
    ESP_LOGE("BIND", "[ble] pairing complete enc=%d (encrypted links=%d)",
             (int)(r != nullptr && r->encrypted), s_encryptedConns);

    // WS-419b: admission at the earliest point the identity is known. The old
    // gate lived in onSubscribe(), which only fires for a central that actually
    // subscribes to HID - a phone that pairs and stops there kept its bond and
    // the whole CFG channel. Now a resolved identity that is not the bound host
    // loses the link immediately, and the bond our stack just created for it is
    // removed so it cannot hold a slot or displace the real host later.
    //
    // An UNRESOLVED identity is left alone on purpose: that is the CASE=C
    // self-heal path (bond lost / IRK mismatch), not a stranger.
    const NimBLEAddress idAddr = connInfo.getIdAddress();
    const bool idNull  = idAddr.isNull();
    const bool idMatch = !idNull && memcmp(idAddr.getVal(), s_hostAddr, 6) == 0;
    const blepol::PeerVerdict verdict = blepol::admitPeer(s_hostBound, idNull, idMatch);
    if (verdict == blepol::PEER_REJECT) {
      ESP_LOGE("BIND", "[ble] reject non-bound peer at pairing conn=0x%04X ota=%s id=%s "
               "(bound=%02X%02X%02X%02X%02X%02X) -> disconnect",
               connInfo.getConnHandle(),
               connInfo.getAddress().toString().c_str(),
               idAddr.toString().c_str(),
               s_hostAddr[0], s_hostAddr[1], s_hostAddr[2],
               s_hostAddr[3], s_hostAddr[4], s_hostAddr[5]);
      // WS-419b: do NOT delete the peer's bond here. ble_gap_unpair() would
      // leave the central holding a key we no longer have, so its next
      // reconnect fails with 0x205/0x25 - and onDisconnect's CASE=A self-heal
      // (s_hostBound && !hostBonded && bond-key failure) would then wipe the
      // REAL host's binding. That self-heal cannot tell a stranger from a host
      // whose RPA did not resolve, so the bond stays and only the link goes.
      if (NimBLEServer* srv = NimBLEDevice::getServer()) {
        srv->disconnect(connInfo.getConnHandle(), BLE_ERR_CONN_TERM_LOCAL);
      }
      return;
    }
  }
  // WS-275: when the primary HID host goes away, stop sending reports until a
  // new central subscribes. Do NOT promote an already-connected second host
  // (the phone that just connected) - that would hand it control exactly when
  // the user put the PC away.
  void onDisconnect(NimBLEServer* s, NimBLEConnInfo& connInfo, int reason) override {
    // WS-410 diagnostics: the reconnect loop lives or dies on WHY the link
    // dropped. Log the numeric reason + name, both peer addresses, and whether
    // the (former) link was encrypted/bonded. This is the evidence line that
    // tells case A (bond lost) from B (IRK/identity changed) from C (address
    // mismatch) on the user's actual hardware.
    NimBLEAddress otaAddr = connInfo.getAddress();   // peer_ota_addr (RPA)
    NimBLEAddress idAddr  = connInfo.getIdAddress(); // peer_id_addr (identity)
    bool encrypted = connInfo.isEncrypted();
    bool bonded    = connInfo.isBonded();
    ESP_LOGE("BIND", "[ble] disconnected n=%d%s reason=0x%04X(%s) ota=%s id=%s enc=%d bonded=%d",
             (int)(s ? s->getConnectedCount() : 0),
             (s && s->getConnectedCount() > 0) ? " (still connected)" : " -> re-advertising",
             reason, reasonName(reason),
             otaAddr.toString().c_str(),
             idAddr.isNull() ? "(unresolved)" : idAddr.toString().c_str(),
             (int)encrypted, (int)bonded);

    // NimBLE removes the peer before invoking this callback, so the count here
    // is "centrals still attached". Only re-advertise once the last one is gone.
    s_connected = s && s->getConnectedCount() > 0;

    // WS-429: how long this link lived, and whether it ever encrypted. A
    // connection that was dropped before it encrypted, right after it came up,
    // is the "the PC holds a pairing for an older identity" pattern.
    const uint16_t handle = connInfo.getConnHandle();
    BdConnRec* rec = connRec(handle);
    const uint32_t durationMs = rec != nullptr ? ((uint32_t)millis() - rec->startMs) : 0;
    const bool wasEncrypted = rec != nullptr ? rec->encrypted : false;
    if (rec != nullptr) {
      if (rec->encrypted && s_encryptedConns > 0) s_encryptedConns--;
      rec->used = false;
    }

    // WS-429 (WS-407 defect 2): count "connected, then torn down before
    // encryption" per peer and back off instead of re-advertising immediately.
    // Without this the field log showed the same peer reconnecting every
    // 200-500 ms forever, with nothing but reason=0x0213 and no conclusion.
    uint8_t key[6];
    if (peerKey(connInfo, key)) {
      const blepol::StaleAction act = blepol::staleNoteTeardown(
          &s_stale, key, encrypted || wasEncrypted, bonded, durationMs,
          blepol::STALE_THRESHOLD);
      if (act == blepol::STALE_REPORTED) {
        char msg[224];
        blepol::formatStaleMessage(msg, sizeof(msg), otaAddr.toString().c_str(),
                                   s_deviceName,
                                   blepol::staleStrikes(&s_stale, key));
        ESP_LOGE("BIND", "%s", msg);
      }
      if (act != blepol::STALE_IGNORED) {
        armAdvBackoff(blepol::staleBackoffMs(blepol::staleStrikes(&s_stale, key)));
      }
    }

    if (!s_connected && !s_advBackoff) {
      NimBLEDevice::startAdvertising();
    }

    if (connInfo.getConnHandle() == s_hid_conn) {
      ESP_LOGE("BIND", "[ble] HID primary conn=0x%04X addr=%s disconnected -> none",
               connInfo.getConnHandle(), otaAddr.toString().c_str());
      s_hid_conn = BLE_HS_CONN_HANDLE_NONE;
    }

    // WS-411 #4: a CLEARBOND issued while a link was up must not tear that
    // link down. Unpair the old host only once every central has gone, so the
    // user is never left with a dropped HID link right after clearing.
    if (s_deleteBondOnDisconnect && !s_connected) {
      s_deleteBondOnDisconnect = false;
      ESP_LOGE("BIND", "[ble] CLEARBOND deferred: unpairing old host bond now that the link is down");
      requestDeleteBond(s_deferredBondAddr);
    }

    // WS-410 self-heal (review must-fix #2 / should-fix #5). Only the bond-key
    // failure family may touch a bond: 0x208 supervision timeout (walk away),
    // 0x213/0x214/0x216 (user/power/local close) never do, so an ordinary
    // disconnect can no longer wipe a healthy bond.
    const bool secFail    = isSecurityFailureReason(reason);
    const bool hostBonded = !idAddr.isNull() && NimBLEDevice::isBonded(idAddr);
    const bool isOurHost  = s_hostBound && !idAddr.isNull() &&
                            memcmp(idAddr.getVal(), s_hostAddr, 6) == 0;

    if (isOurHost && hostBonded && secFail) {
      // B/C: it is our host and the bond is still in the store, but the
      // security handshake failed -> the stored key is stale. Delete it so
      // Windows re-pairs instead of looping "connected / paired".
      ESP_LOGE("BIND", "[ble] SELF-HEAL(B/C): deleting stale bond for host %s "
               "(reason=0x%04X(%s))", idAddr.toString().c_str(), reason, reasonName(reason));
      requestDeleteBond(idAddr);
    } else if (s_hostBound && !hostBonded && isBondKeyFailureReason(reason)) {
      // A: the app still has a binding but our BLE bond is gone/unresolvable,
      // and the peer tried to use a key we no longer have (so it is our former
      // host, not a stranger failing a fresh pairing). Reset the app binding so
      // it can re-pair and bind again — without this the identity-only gate
      // above would reject it forever.
      ESP_LOGE("BIND", "[ble] SELF-HEAL(A): app-bound but no BLE bond "
               "(reason=0x%04X(%s)) -> clearing app binding so the host can re-pair",
               reason, reasonName(reason));
      requestClearHostBinding();
    }

    // WS-328: a link drop during OTA must abort the write immediately. The
    // handler checks whether a session is active, so ordinary disconnects are
    // unaffected.
    if (bleConfig.onDisconnect) bleConfig.onDisconnect();
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

void BleKeyboard::begin(const BleBootFacts& boot) {
  NimBLEDevice::init(_name);

  // WS-429: remember the advertised name for the HOST_STALE_KEY conclusion.
  snprintf(s_deviceName, sizeof(s_deviceName), "%s", _name ? _name : "");

  // WS-274: install this deck's unique IRK before the GATT server / pairing
  // exists. init() only returns once the host has synced with the controller,
  // so the HCI commands inside ble_hs_pvcy_set_our_irk() are safe to issue
  // here. Keep sm_our_key_dist = ENC | ID (set in NimBLEDevice::init) so the
  // unique IRK is actually distributed during pairing.
  uint8_t irk[16];
  bool irkWasPresent = false;
  getOrCreateIrk(irk, &irkWasPresent);
  int irkRc = ble_hs_pvcy_set_our_irk(irk);
  // WS-410: print the FULL 16-byte IRK fingerprint (not just 2 octets) so two
  // reboots can be compared byte-for-byte. If the fp changes across reboots,
  // the deck presents a different BLE identity to Windows every boot (case B),
  // which is what makes Windows fold/reject it and loop.
  char irkFp[40];
  snprintf(irkFp, sizeof(irkFp),
           "%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X%02X",
           irk[0],  irk[1],  irk[2],  irk[3],  irk[4],  irk[5],  irk[6],  irk[7],
           irk[8],  irk[9],  irk[10], irk[11], irk[12], irk[13], irk[14], irk[15]);
  ESP_LOGE("BIND", "[ble] IRK fp=%s set=%d", irkFp, irkRc);

  // WS-321: restore the persisted host binding (if any) before the GATT
  // server starts, so the first HID subscription can be validated against it.
  loadHostBinding();
  ESP_LOGE("BIND", "[ble] host binding: %s addr=%02X%02X%02X%02X%02X%02X",
           s_hostBound ? "bound" : "none",
           s_hostAddr[0], s_hostAddr[1], s_hostAddr[2],
           s_hostAddr[3], s_hostAddr[4], s_hostAddr[5]);

  // WS-410 diagnostics: how many peer-security (bond) records survived the
  // reboot. If this reads 0 right after a reboot that should have kept a bond,
  // the bond store is being wiped (case A). If it reads 1 but reconnects still
  // loop, the bond is present but stale (case B/C) and self-heal will clear it.
  int bondN = 0;
  ble_store_util_count(BLE_STORE_OBJ_TYPE_PEER_SEC, &bondN);
  ESP_LOGE("BIND", "[ble] bonds(PEER_SEC)=%d", bondN);

  // WS-410 (review must-fix #4): auto-classify the reconnect failure so the
  // log names the case instead of leaving it to a human to infer:
  //   B -> the persisted IRK changed since the previous boot (identity drift)
  //        OR the IRK was missing while NVS still held an older config
  //        (identity reset - the deck minted a brand-new identity)
  //   A -> the app still has a binding but there is no BLE bond
  //   C -> binding + bond both present (reconnect failure = address/identity)
  //   none -> no binding yet (fresh device, first pairing)
  //
  // WS-429 (WS-407 defect 1): the old rule only reported B when a prevIrk
  // blob existed AND differed. A deck whose IRK was lost (the field case:
  // "irk NOT_FOUND" plus "pin schema v1 -> v2") had no prevIrk either, so it
  // fell through to "none" - telling the user "brand new device, no problem"
  // while the deck had just minted a new identity the PC knows nothing about.
  // The BleBootFacts are captured by main.cpp BEFORE this boot writes anything
  // into NVS, so "genuinely new" stays distinguishable from "identity lost".
  {
    Preferences p;
    bool ok = p.begin("binddecklite", false);
    uint8_t prev[16];
    bool havePrev = ok && p.getBytesLength("prevIrk") == 16 &&
                    p.getBytes("prevIrk", prev, 16) == 16;

    blepol::IdentityFacts idFacts;
    idFacts.irkPresent     = irkWasPresent;
    idFacts.havePrevIrk    = havePrev;
    idFacts.prevIrkMatches = havePrev && memcmp(prev, irk, 16) == 0;
    idFacts.hostBound      = s_hostBound;
    idFacts.bondCount      = bondN;
    idFacts.schemaMigrated = boot.schemaMigrated;
    idFacts.priorConfig    = boot.priorConfig;
    idFacts.rebooted       = boot.rebooted;

    const blepol::IdentityVerdict verdict = blepol::classifyIdentity(idFacts);
    char caseStr[112];
    blepol::formatIdentityCase(verdict, caseStr, sizeof(caseStr));
    ESP_LOGE("BIND", "[ble] CASE=%s (irk=%s prevIrk=%s bonds=%d hostBound=%d)",
             caseStr, irkWasPresent ? "present" : "absent",
             havePrev ? "present" : "none", bondN, (int)s_hostBound);
    if (ok) {
      p.putBytes("prevIrk", irk, 16);
      p.end();
    }
  }

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
  // WS-419b: the config channel answers only to the bound host. WRITE_ENC alone
  // let any freshly paired phone reconfigure the deck.
  bleConfig.peerAllowed = [](const uint8_t id[6], bool idNull) -> bool {
    const bool idMatch = !idNull && id != nullptr &&
                         memcmp(id, s_hostAddr, 6) == 0;
    return blepol::allowCfgAccess(s_hostBound, idNull, idMatch);
  };
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

  // WS-429: a stale-peer backoff owns the radio until its deadline expires.
  if (s_advBackoff) return;

  // WS-429 (WS-407 defect 3): while a link is up we only keep advertising when
  // that link is trustworthy.
  //  * encrypted     -> the host is paired; the config app must be able to
  //                     join this deck while Windows holds the HID link
  //                     (WS-271).
  //  * hidSubscribed -> a HID host already subscribed, i.e. the deck is in use.
  // An unencrypted link that is still young is the reconnect-loop pattern
  // (the field peer was dropped after 200-500 ms, over and over). Advertising
  // there is what kept feeding that loop, so stay quiet instead.
  const bool hidSubscribed = (s_hid_conn != BLE_HS_CONN_HANDLE_NONE);
  if (!blepol::mayAdvertiseWhileConnected(s_encryptedConns > 0, hidSubscribed,
                                          oldestUnencryptedAgeMs())) {
    return;
  }

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  if (adv && !adv->isAdvertising()) {
    bool ok = NimBLEDevice::startAdvertising();
    ESP_LOGE("BIND", "[ble] re-advertise while connected -> %d", (int)ok);
  }
}

// WS-429: the user pressed a key / turned the encoder. The deck is in front
// of the user, so there is no point in staying quiet for the rest of a
// backoff: advertise again right away.
void BleKeyboard::notifyUserActivity() {
  if (!s_advBackoff) return;
  ESP_LOGE("BIND", "[ble] user activity -> stale-peer backoff cancelled, advertising resumes");
  clearAdvBackoff();
  if (!s_connected) {
    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    if (adv && !adv->isAdvertising()) {
      bool ok = NimBLEDevice::startAdvertising();
      ESP_LOGE("BIND", "[ble] re-advertise after user activity -> %d", (int)ok);
    }
  }
}

// WS-429: the advertising supervisor. Runs on the Arduino loop task (from
// processPendingBleWork) and lifts the stale-peer backoff when its deadline
// expires, so the device is never locked out for more than the capped
// backoff (30 s).
static void serviceAdvertising() {
  if (!s_advBackoff) return;
  if ((int32_t)((uint32_t)millis() - s_advResumeAt) < 0) return;   // still waiting
  clearAdvBackoff();
  ESP_LOGE("BIND", "[ble] stale-peer backoff over -> advertising resumes");
  if (!s_connected) {
    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    if (adv && !adv->isAdvertising()) {
      bool ok = NimBLEDevice::startAdvertising();
      ESP_LOGE("BIND", "[ble] re-advertise after backoff -> %d", (int)ok);
    }
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
  // WS-410 (part 4): "clear binding" must also drop the NimBLE bond for the
  // bound peer, otherwise the protocol-layer key survives and the user still
  // has to delete the device in Windows. Capture the typed address BEFORE the
  // file-static helper zeroes it, and arm a deferred delete (bond writes are
  // not safe on the NimBLE task — loop() drains it).
  //
  // WS-411 #4: if the host is still connected, do NOT unpair on the spot —
  // ble_gap_unpair() would terminate the live HID link and make the user think
  // "clear binding" broke their keyboard. Remember it and unpair on the next
  // disconnect instead.
  if (s_hostBound && !s_hostAddrObj.isNull()) {
    if (s_connected) {
      s_deferredBondAddr = s_hostAddrObj;
      s_deleteBondOnDisconnect = true;
      ESP_LOGE("BIND", "[ble] CLEARBOND: link active -> bond will be removed after disconnect");
    } else {
      requestDeleteBond(s_hostAddrObj);
    }
  }
  ::clearHostBinding();   // the file-static helper (clears NVS + RAM)
}

int BleKeyboard::bondCount() const {
  int n = 0;
  ble_store_util_count(BLE_STORE_OBJ_TYPE_PEER_SEC, &n);
  return n;
}

bool BleKeyboard::isHostBonded() const {
  if (!s_hostBound || s_hostAddrObj.isNull()) return false;
  return NimBLEDevice::isBonded(s_hostAddrObj);
}

void requestDeleteBond(const NimBLEAddress& addr) {
  if (addr.isNull()) return;
  s_pendingDeleteAddr = addr;
  s_pendingDeleteBond = true;
  s_lastBondDeleteAttempt = 0;   // first attempt runs on the next loop tick
}

void requestClearHostBinding() {
  s_pendingClearBinding = true;
}

void processPendingBleWork() {
  // WS-429: lift the stale-peer advertising backoff when its deadline
  // expires. Runs on the Arduino loop task, so the radio is only touched from
  // a safe context.
  serviceAdvertising();

  // WS-410 CASE=A self-heal: drop the app-layer binding so a host whose BLE
  // bond was lost can re-pair and bind again. Preferences writes stay here on
  // the Arduino loop task, never on the NimBLE task.
  if (s_pendingClearBinding) {
    s_pendingClearBinding = false;
    ::clearHostBinding();
    ESP_LOGE("BIND", "[ble] SELF-HEAL(A): app binding cleared -> host will re-pair");
  }

  if (!s_pendingDeleteBond) return;

  // Throttle retries; the first attempt (lastAttempt==0) runs immediately.
  if (s_lastBondDeleteAttempt != 0 &&
      (unsigned long)(millis() - s_lastBondDeleteAttempt) < 250) {
    return;
  }
  s_lastBondDeleteAttempt = millis();

  NimBLEAddress addr = s_pendingDeleteAddr;
  if (addr.isNull()) { s_pendingDeleteBond = false; return; }

  // WS-410 (review must-fix #1): ble_gap_unpair() returns BLE_HS_EBUSY while
  // advertising or discovery is active, for any peer that distributed an IRK
  // (every Windows bond does) — framework ble_gap.c. Advertising runs almost
  // continuously here (onDisconnect re-advertises, keepAdvertising() restarts
  // it), so calling deleteBond() with advertising up fails every time. Stop
  // advertising around the unpair and restore it afterwards.
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  const bool wasAdv = adv && adv->isAdvertising();
  if (wasAdv) adv->stop();

  // Call the core directly (not NimBLEDevice::deleteBond, which collapses the
  // result to bool) so EBUSY / ENOENT can be told apart and retried correctly.
  const int rc = ble_gap_unpair(addr.getBase());

  if (wasAdv) NimBLEDevice::startAdvertising();

  if (rc == 0) {
    s_pendingDeleteBond = false;
    s_lastBondDeleteAttempt = 0;
    s_bondDeleteFails = 0;
    ESP_LOGE("BIND", "[ble] deferred deleteBond %s -> ok (wasAdv=%d)",
             addr.toString().c_str(), (int)wasAdv);
  } else if (rc == BLE_HS_ENOENT) {
    // No matching store record (wrong address/type, or already gone): another
    // retry cannot help, so clear the request and report it once.
    s_pendingDeleteBond = false;
    s_lastBondDeleteAttempt = 0;
    s_bondDeleteFails = 0;
    ESP_LOGE("BIND", "[ble] deferred deleteBond %s -> no record (rc=0x%X), cleared",
             addr.toString().c_str(), rc);
  } else {
    // Keep pending and retry. EBUSY is called out; both are logged sparingly
    // so a persistent failure does not flood the console.
    ++s_bondDeleteFails;
    if (s_bondDeleteFails == 1 || (s_bondDeleteFails % 40) == 0) {
      ESP_LOGE("BIND", "[ble] deferred deleteBond %s -> FAILED rc=0x%X%s (attempt %d, will retry)",
               addr.toString().c_str(), rc,
               rc == BLE_HS_EBUSY ? " EBUSY(adv active)" : "",
               s_bondDeleteFails);
    }
  }
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
