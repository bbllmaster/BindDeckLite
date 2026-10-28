#pragma once
//
// WS-429: pure link policy shared by the firmware and the host-side test.
//
// Everything in here is a plain function over plain data - no Arduino, no
// NimBLE, no NVS, no globals. The firmware includes it from
// src/nimble_hid.cpp; the host test (host/test_ble_link_policy.py) compiles
// the VERY SAME header with g++ and drives it through a tiny CLI, so the test
// exercises production code instead of a copy of it.
//
// Three policies live here:
//
//  1. Boot-time identity classification (WS-407 defect 1).
//     The 128-bit IRK *is* the BLE identity of a deck. When it is missing from
//     NVS the deck mints a new one, so every pairing the host still holds is
//     stale - the device is NOT "brand new". Reporting "none" there is what
//     sent the user hunting for a problem that was on the PC side all along.
//
//  2. Pre-encryption teardown guard (WS-407 defect 2).
//     A peer that connects and is torn down before encryption, over and over,
//     means the PC still holds a pairing for an older identity of this deck.
//     The device cannot clear that pairing (it lives in the PC), so it has to
//     say so once - and stop hammering the radio every 200 ms.
//
//  3. While-connected advertising gate (WS-407 defect 3).
//     Advertising the same address while an unencrypted link is up feeds the
//     reconnect loop. The gate keeps the radio quiet in that case and stays
//     open for the WS-271 case (a working HID host plus the config app).
//

#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>

namespace blepol {

// ---------------------------------------------------------------------------
// 1. Boot-time identity classification
// ---------------------------------------------------------------------------

enum IdentityVerdict : uint8_t {
  IDENTITY_FRESH = 0,             // "none" - genuinely first boot
  IDENTITY_B_IRK_CHANGED,         // "B"    - persisted IRK differs from last boot
  IDENTITY_B_IRK_ABSENT_MIGRATED, // "B"    - IRK gone + the config schema was upgraded
  IDENTITY_B_IRK_ABSENT_PRIOR,    // "B"    - IRK gone + NVS still holds a config
  IDENTITY_B_IRK_ABSENT_REBOOT,   // "B"    - IRK gone + the deck rebooted
  IDENTITY_A,                     // "A"    - app-bound but no BLE bond
  IDENTITY_C                      // "C"    - binding and bond both present
};

// The facts the firmware can observe at boot, captured BEFORE this boot
// writes anything into NVS.
struct IdentityFacts {
  bool irkPresent = false;      // a valid 16-byte irk existed in NVS before this boot
  bool havePrevIrk = false;     // a prevIrk blob existed in NVS
  bool prevIrkMatches = false;  // prevIrk equals the IRK in use
  bool hostBound = false;       // app-layer host binding present
  int  bondCount = 0;           // BLE peer-security (bond) records
  bool schemaMigrated = false;  // an older config schema was upgraded this boot
  bool priorConfig = false;     // NVS still holds an earlier configuration
  bool rebooted = false;        // the deck already booted since the last power cycle
};

// Classify the boot. The IRK rules first: a deck that minted a new identity
// cannot talk to a host that paired with the old one, whatever else NVS says.
inline IdentityVerdict classifyIdentity(const IdentityFacts& f) {
  if (!f.irkPresent) {
    // "irk absent" alone is not enough to call it an identity reset: a deck
    // that never had a configuration is genuinely new. It is an identity reset
    // when something proves the deck was already running before:
    //  * the config schema was upgraded -> the deck was flashed before,
    //  * NVS still holds a configuration -> the deck was in use before,
    //  * the deck already rebooted -> a first boot writes the IRK, so a second
    //    boot without an IRK means the identity was lost in between.
    if (f.schemaMigrated) return IDENTITY_B_IRK_ABSENT_MIGRATED;
    if (f.priorConfig)    return IDENTITY_B_IRK_ABSENT_PRIOR;
    if (f.rebooted)       return IDENTITY_B_IRK_ABSENT_REBOOT;
    return IDENTITY_FRESH;
  }
  if (f.havePrevIrk && !f.prevIrkMatches) return IDENTITY_B_IRK_CHANGED;
  if (f.hostBound && f.bondCount == 0)     return IDENTITY_A;
  if (f.hostBound)                          return IDENTITY_C;
  return IDENTITY_FRESH;
}

inline const char* identityCaseCode(IdentityVerdict v) {
  switch (v) {
    case IDENTITY_B_IRK_CHANGED:
    case IDENTITY_B_IRK_ABSENT_MIGRATED:
    case IDENTITY_B_IRK_ABSENT_PRIOR:
    case IDENTITY_B_IRK_ABSENT_REBOOT:
      return "B";
    case IDENTITY_A: return "A";
    case IDENTITY_C: return "C";
    default:         return "none";
  }
}

inline const char* identityCaseDetail(IdentityVerdict v) {
  switch (v) {
    case IDENTITY_B_IRK_CHANGED:
      return "irk changed since last boot";
    case IDENTITY_B_IRK_ABSENT_MIGRATED:
      return "identity reset: irk absent, nvs schema migrated";
    case IDENTITY_B_IRK_ABSENT_PRIOR:
      return "identity reset: irk absent, nvs has prior config";
    case IDENTITY_B_IRK_ABSENT_REBOOT:
      return "identity reset: irk absent, deck rebooted without a saved identity";
    case IDENTITY_A:
      return "app bound but no ble bond";
    case IDENTITY_C:
      return "binding and bond present";
    default:
      return "";
  }
}

// "B(identity reset: irk absent, nvs schema migrated)" or plain "none".
inline void formatIdentityCase(IdentityVerdict v, char* out, size_t outLen) {
  if (out == nullptr || outLen == 0) return;
  const char* code = identityCaseCode(v);
  const char* detail = identityCaseDetail(v);
  if (detail[0] == '\0') snprintf(out, outLen, "%s", code);
  else                   snprintf(out, outLen, "%s(%s)", code, detail);
}

// ---------------------------------------------------------------------------
// 2. Pre-encryption teardown guard
// ---------------------------------------------------------------------------

// A connection that lived longer than this was in use; its end is an ordinary
// disconnect and must never be counted against the peer.
static const uint32_t STALE_FAST_TEARDOWN_MS = 2000;
// Consecutive pre-encryption teardowns of the SAME peer before we speak up.
static const uint8_t  STALE_THRESHOLD = 3;
// Re-advertise backoff ladder: 2 s / 5 s / 10 s / 30 s, capped at 30 s.
static const uint32_t STALE_BACKOFF_MAX_MS = 30000;
// How many peers are tracked at the same time (Windows HID + config app).
static const uint8_t  STALE_MAX_PEERS = 2;

enum StaleAction : uint8_t {
  STALE_IGNORED = 0,   // not a pre-encryption teardown - nothing to count
  STALE_COUNTED,       // counted as one strike
  STALE_REPORTED       // counted and the threshold was reached -> report once
};

struct StalePeer {
  struct Entry {
    uint8_t addr[6] = {0, 0, 0, 0, 0, 0};
    uint8_t strikes = 0;
    bool    reported = false;
    bool    used = false;
  } slot[STALE_MAX_PEERS];
};

inline void staleReset(StalePeer* p) {
  if (p == nullptr) return;
  memset(p, 0, sizeof(*p));
}

// The strikes recorded for one peer (0 when it is not tracked).
inline uint8_t staleStrikes(const StalePeer* p, const uint8_t addr[6]) {
  if (p == nullptr || addr == nullptr) return 0;
  for (uint8_t i = 0; i < STALE_MAX_PEERS; i++) {
    if (p->slot[i].used && memcmp(p->slot[i].addr, addr, 6) == 0) {
      return p->slot[i].strikes;
    }
  }
  return 0;
}

// A peer that proved it can talk to us (paired / subscribed) is not a suspect
// any more: drop its strikes so the next failure starts from zero.
// Returns true when the tracked peer was actually cleared.
inline bool stalePeerSucceeded(StalePeer* p, const uint8_t addr[6]) {
  if (p == nullptr || addr == nullptr) return false;
  for (uint8_t i = 0; i < STALE_MAX_PEERS; i++) {
    if (p->slot[i].used && memcmp(p->slot[i].addr, addr, 6) == 0) {
      memset(&p->slot[i], 0, sizeof(p->slot[i]));
      return true;
    }
  }
  return false;
}

// Register one pre-encryption teardown of `addr`.
//
//   encrypted          - the link had reached encryption (a working session)
//   bonded             - the peer holds a bond record for us
//   durationMs         - how long the connection lived
//   threshold          - strikes before the conclusion is reported
//
// An encrypted link is a healthy session, and a link that lived longer than
// STALE_FAST_TEARDOWN_MS was in use: neither is counted. Everything else - a
// peer that connects and is dropped before it ever encrypted - is a strike.
inline StaleAction staleNoteTeardown(StalePeer* p, const uint8_t addr[6],
                                     bool encrypted, bool bonded,
                                     uint32_t durationMs, uint8_t threshold) {
  if (p == nullptr || addr == nullptr) return STALE_IGNORED;
  if (encrypted) return STALE_IGNORED;
  if (durationMs >= STALE_FAST_TEARDOWN_MS) return STALE_IGNORED;
  (void)bonded;   // a bond without encryption is still a failed handshake

  if (threshold == 0) threshold = 1;

  // Find (or open) the slot for this peer.
  int idx = -1;
  for (uint8_t i = 0; i < STALE_MAX_PEERS; i++) {
    if (p->slot[i].used && memcmp(p->slot[i].addr, addr, 6) == 0) { idx = i; break; }
  }
  if (idx < 0) {
    for (uint8_t i = 0; i < STALE_MAX_PEERS; i++) {
      if (!p->slot[i].used) { idx = i; break; }
    }
  }
  if (idx < 0) {
    // No free slot: evict the peer with the fewest strikes.
    idx = 0;
    for (uint8_t i = 1; i < STALE_MAX_PEERS; i++) {
      if (p->slot[i].strikes < p->slot[idx].strikes) idx = i;
    }
  }
  if (!p->slot[idx].used || memcmp(p->slot[idx].addr, addr, 6) != 0) {
    memset(&p->slot[idx], 0, sizeof(p->slot[idx]));
    memcpy(p->slot[idx].addr, addr, 6);
    p->slot[idx].used = true;
  }

  if (p->slot[idx].strikes < 255) p->slot[idx].strikes++;
  if (!p->slot[idx].reported && p->slot[idx].strikes >= threshold) {
    p->slot[idx].reported = true;   // one conclusion per streak, never per round
    return STALE_REPORTED;
  }
  return STALE_COUNTED;
}

// Re-advertise backoff for the given number of strikes: 2 s / 5 s / 10 s /
// 30 s, capped at 30 s.
inline uint32_t staleBackoffMs(uint8_t strikes) {
  switch (strikes) {
    case 0:  return 0;
    case 1:  return 2000;
    case 2:  return 5000;
    case 3:  return 10000;
    default: return STALE_BACKOFF_MAX_MS;
  }
}

// The one-line conclusion the user can act on.
inline void formatStaleMessage(char* out, size_t outLen,
                               const char* peerAddr, const char* deviceName,
                               uint8_t strikes) {
  if (out == nullptr || outLen == 0) return;
  snprintf(out, outLen,
           "HOST_STALE_KEY: peer %s keeps terminating before encryption (n=%u). "
           "The PC still holds a pairing for an older identity of this device -> "
           "delete %s in Windows Bluetooth settings and pair again.",
           (peerAddr != nullptr && peerAddr[0] != '\0') ? peerAddr : "(unknown)",
           (unsigned)strikes,
           (deviceName != nullptr && deviceName[0] != '\0') ? deviceName : "the device");
}

// ---------------------------------------------------------------------------
// 3. While-connected advertising gate (WS-407 defect 3)
// ---------------------------------------------------------------------------

// An unencrypted link gets this long to settle before it is treated as "not
// the rapid-teardown pattern". The failing peer in the field tears the link
// down after 200-500 ms, so it never reaches the grace window.
static const uint32_t ADV_UNENCRYPTED_GRACE_MS = 3000;

// May the device keep advertising while a link is up?
//
//   encrypted     - that link reached encryption (the host is paired)
//   hidSubscribed - a HID host already subscribed to our input reports
//   linkAgeMs     - how long the oldest unencrypted link has been up
//
// WS-271 needs the radio open while a working HID host is connected, so the
// config app can join. An unencrypted link that is still young is the
// reconnect-loop pattern: stay quiet, or the peer comes back every 200 ms.
inline bool mayAdvertiseWhileConnected(bool encrypted, bool hidSubscribed,
                                        uint32_t linkAgeMs) {
  if (encrypted) return true;
  if (hidSubscribed) return true;
  return linkAgeMs >= ADV_UNENCRYPTED_GRACE_MS;
}

// ---------------------------------------------------------------------------
// 4. Peer admission (WS-419b)
// ---------------------------------------------------------------------------
//
// The bind gate used to run only in the HID subscription callback, i.e. AFTER
// SMP had already completed. A phone therefore finished pairing (it keeps a
// bond and the OS shows "paired"), and - worse - a central that never
// subscribes to HID was never rejected at all: it kept the link and full
// access to the CFG/CMD channel until it chose to leave.
//
// Admission is decided from the RESOLVED IDENTITY only. `bonded` is not a
// signal: a stranger that just paired is bonded too, which is exactly the bug.

enum PeerVerdict {
  PEER_ALLOW     = 0,   // the bound host, or the deck is not bound yet
  PEER_REJECT    = 1,   // resolved identity that is NOT the bound host
  PEER_UNDECIDED = 2,   // identity unresolved -> leave it to the self-heal path
};

// Should this link be dropped as soon as the identity is known?
//
//   bound           - an app-level host binding exists
//   identityNull    - the stack could not resolve an identity address
//   identityMatches - that identity equals the stored bound address
//
// PEER_UNDECIDED is deliberate: an unresolved identity is the CASE=C self-heal
// path (bond store / IRK mismatch) and must not be mistaken for a stranger,
// or the legitimate host would be locked out permanently.
inline PeerVerdict admitPeer(bool bound, bool identityNull, bool identityMatches) {
  if (!bound) return PEER_ALLOW;
  if (identityNull) return PEER_UNDECIDED;
  return identityMatches ? PEER_ALLOW : PEER_REJECT;
}

// May this peer read/write CFG:/CMD: lines?
//
// Stricter than admitPeer(): an unresolved identity is *not* good enough here.
// CFG writes change pins, encoder modes and can reboot the deck into config
// mode; only OTA carries its own "bound host that owns the HID link" check, so
// this is the only gate in front of everything else.
inline bool allowCfgAccess(bool bound, bool identityNull, bool identityMatches) {
  if (!bound) return true;          // unbound deck: first central configures it
  if (identityNull) return false;   // cannot prove it is the bound host
  return identityMatches;
}

}  // namespace blepol
