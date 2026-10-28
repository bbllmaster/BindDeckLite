// WS-429: CLI wrapper around the PRODUCTION link policy.
//
// This file contains no policy logic at all - it only parses commands and
// calls the functions from src/ble_link_policy.h, the very same header the
// firmware includes. The host test (host/test_ble_link_policy.py) compiles
// this with g++ and drives it, so the assertions run against production code
// instead of a copy of it.
//
// Commands (one per line on stdin):
//
//   classify <irk> <prev> <match> <bound> <bonds> <migrated> <prior>
//   teardown <addr> <enc> <bonded> <durationMs> <threshold>
//   success  <addr>
//   strikes  <addr>
//   backoff  <strikes>
//   message  <addr> <deviceName> <strikes>
//   adv      <enc> <hidSubscribed> <linkAgeMs>
//   admit    <bound> <identityNull> <identityMatches>
//   cfg      <bound> <identityNull> <identityMatches>
//   claim    <bound> <windowOpen> <identityNull> <identityMatches>
//   reset
//
// Booleans are 0/1, addresses are hex with or without ':' separators.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

#include "ble_link_policy.h"

namespace {

blepol::StalePeer g_stale;

bool parseBool(const std::string& s) {
  return s == "1" || s == "true" || s == "yes";
}

bool parseAddr(const std::string& in, uint8_t out[6]) {
  std::string hex;
  for (char c : in) {
    if (c == ':' || c == '-' || c == ' ') continue;
    hex += c;
  }
  if (hex.size() != 12) return false;
  for (int i = 0; i < 6; i++) {
    char pair[3] = { hex[i * 2], hex[i * 2 + 1], 0 };
    char* end = nullptr;
    long v = strtol(pair, &end, 16);
    if (end == pair || v < 0 || v > 255) return false;
    out[i] = (uint8_t)v;
  }
  return true;
}

int doClassify(std::vector<std::string>& args) {
  if (args.size() < 9) { std::printf("err=usage\n"); return 1; }
  blepol::IdentityFacts f;
  f.irkPresent     = parseBool(args[1]);
  f.havePrevIrk    = parseBool(args[2]);
  f.prevIrkMatches = parseBool(args[3]);
  f.hostBound      = parseBool(args[4]);
  f.bondCount      = atoi(args[5].c_str());
  f.schemaMigrated = parseBool(args[6]);
  f.priorConfig    = parseBool(args[7]);
  f.rebooted       = parseBool(args[8]);

  const blepol::IdentityVerdict v = blepol::classifyIdentity(f);
  char text[160];
  blepol::formatIdentityCase(v, text, sizeof(text));
  std::printf("code=%s detail=%s text=%s\n",
              blepol::identityCaseCode(v),
              blepol::identityCaseDetail(v),
              text);
  return 0;
}

int doTeardown(std::vector<std::string>& args) {
  if (args.size() < 6) { std::printf("err=usage\n"); return 1; }
  uint8_t addr[6];
  if (!parseAddr(args[1], addr)) { std::printf("err=addr\n"); return 1; }
  const bool encrypted = parseBool(args[2]);
  const bool bonded = parseBool(args[3]);
  const uint32_t durationMs = (uint32_t)strtoul(args[4].c_str(), nullptr, 10);
  const uint8_t threshold = (uint8_t)atoi(args[5].c_str());

  const blepol::StaleAction act = blepol::staleNoteTeardown(
      &g_stale, addr, encrypted, bonded, durationMs, threshold);
  const uint8_t strikes = blepol::staleStrikes(&g_stale, addr);
  const char* name = (act == blepol::STALE_REPORTED) ? "reported"
                     : (act == blepol::STALE_COUNTED) ? "counted" : "ignored";
  std::printf("action=%s strikes=%u backoff=%u\n",
              name, (unsigned)strikes, (unsigned)blepol::staleBackoffMs(strikes));
  return 0;
}

int doSuccess(std::vector<std::string>& args) {
  if (args.size() < 2) { std::printf("err=usage\n"); return 1; }
  uint8_t addr[6];
  if (!parseAddr(args[1], addr)) { std::printf("err=addr\n"); return 1; }
  const bool cleared = blepol::stalePeerSucceeded(&g_stale, addr);
  std::printf("cleared=%d strikes=%u\n", cleared ? 1 : 0,
              (unsigned)blepol::staleStrikes(&g_stale, addr));
  return 0;
}

int doStrikes(std::vector<std::string>& args) {
  if (args.size() < 2) { std::printf("err=usage\n"); return 1; }
  uint8_t addr[6];
  if (!parseAddr(args[1], addr)) { std::printf("err=addr\n"); return 1; }
  std::printf("strikes=%u\n", (unsigned)blepol::staleStrikes(&g_stale, addr));
  return 0;
}

int doBackoff(std::vector<std::string>& args) {
  if (args.size() < 2) { std::printf("err=usage\n"); return 1; }
  const uint8_t strikes = (uint8_t)atoi(args[1].c_str());
  std::printf("ms=%u\n", (unsigned)blepol::staleBackoffMs(strikes));
  return 0;
}

int doMessage(std::vector<std::string>& args) {
  if (args.size() < 4) { std::printf("err=usage\n"); return 1; }
  const uint8_t strikes = (uint8_t)atoi(args[3].c_str());
  char msg[256];
  blepol::formatStaleMessage(msg, sizeof(msg), args[1].c_str(), args[2].c_str(), strikes);
  std::printf("message=%s\n", msg);
  return 0;
}

int doAdv(std::vector<std::string>& args) {
  if (args.size() < 4) { std::printf("err=usage\n"); return 1; }
  const bool encrypted = parseBool(args[1]);
  const bool hidSubscribed = parseBool(args[2]);
  const uint32_t age = (uint32_t)strtoul(args[3].c_str(), nullptr, 10);
  std::printf("allow=%d\n",
              blepol::mayAdvertiseWhileConnected(encrypted, hidSubscribed, age) ? 1 : 0);
  return 0;
}

int doAdmit(std::vector<std::string>& args) {
  if (args.size() < 4) { std::printf("err=usage\n"); return 1; }
  const bool bound = parseBool(args[1]);
  const bool idNull = parseBool(args[2]);
  const bool idMatch = parseBool(args[3]);
  const blepol::PeerVerdict v = blepol::admitPeer(bound, idNull, idMatch);
  const char* code = (v == blepol::PEER_ALLOW) ? "allow"
                     : (v == blepol::PEER_REJECT) ? "reject" : "undecided";
  std::printf("verdict=%s disconnect=%d\n", code,
              v == blepol::PEER_REJECT ? 1 : 0);
  return 0;
}

int doCfg(std::vector<std::string>& args) {
  if (args.size() < 4) { std::printf("err=usage\n"); return 1; }
  const bool bound = parseBool(args[1]);
  const bool idNull = parseBool(args[2]);
  const bool idMatch = parseBool(args[3]);
  std::printf("allow=%d\n",
              blepol::allowCfgAccess(bound, idNull, idMatch) ? 1 : 0);
  return 0;
}

int doClaim(std::vector<std::string>& args) {
  if (args.size() < 5) { std::printf("err=usage\n"); return 1; }
  const bool bound = parseBool(args[1]);
  const bool window = parseBool(args[2]);
  const bool idNull = parseBool(args[3]);
  const bool idMatch = parseBool(args[4]);
  const blepol::BindClaim c = blepol::classifyBindClaim(bound, window, idNull, idMatch);
  const char* code = (c == blepol::BIND_NONE) ? "none"
                     : (c == blepol::BIND_KEEP_HOST) ? "keep" : "takeover";
  std::printf("claim=%s\n", code);
  return 0;
}

int doReset(std::vector<std::string>&) {  blepol::staleReset(&g_stale);
  std::printf("ok=1\n");
  return 0;
}

}  // namespace

int main() {
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty()) continue;
    std::vector<std::string> args;
    std::string cur;
    for (char c : line) {
      if (c == ' ') {
        if (!cur.empty()) { args.push_back(cur); cur.clear(); }
      } else {
        cur += c;
      }
    }
    if (!cur.empty()) args.push_back(cur);
    if (args.empty()) continue;

    int rc = 1;
    const std::string& cmd = args[0];
    if (cmd == "classify")       rc = doClassify(args);
    else if (cmd == "teardown")  rc = doTeardown(args);
    else if (cmd == "success")   rc = doSuccess(args);
    else if (cmd == "strikes")   rc = doStrikes(args);
    else if (cmd == "backoff")   rc = doBackoff(args);
    else if (cmd == "message")   rc = doMessage(args);
    else if (cmd == "adv")       rc = doAdv(args);
    else if (cmd == "admit")     rc = doAdmit(args);
    else if (cmd == "cfg")       rc = doCfg(args);
    else if (cmd == "claim")     rc = doClaim(args);
    else if (cmd == "reset")     rc = doReset(args);
    else                         std::printf("err=unknown\n");
    std::fflush(stdout);
    if (rc != 0) return rc;
  }
  return 0;
}
