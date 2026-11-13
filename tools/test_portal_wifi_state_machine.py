#!/usr/bin/env python3
"""WS-614 regression: the config-portal WiFi attempt state machine.

The firmware has no test framework and the PC cannot run an ESP32 WebServer,
so this harness does the next best thing: it extracts
``wifiAttemptSnapshot()`` (and its ``WifiAttempt`` struct / timeout define)
**verbatim** from ``src/main.cpp``, drops it next to minimal ``String`` /
``WiFi`` / ``millis()`` stubs, compiles the whole thing with g++ and runs the
four scenarios the Stage 2 review called out. The logic under test is the real
production function, not a copy: edit ``wifiAttemptSnapshot()`` in
``src/main.cpp`` and these assertions change with it.

Scenario 2 is the bug WS-614 fixes: a device that has been connected for a
minute must NOT flip to ``fail`` on a single ``WL_DISCONNECTED`` poll (the
window is measured from the last time we had an IP, not from the one-off
``/save`` begin). Scenario 5 is the should-fix: opening the portal must not
start the 25 s fail clock at all.

Usage:
    python tools/test_portal_wifi_state_machine.py [path/to/main.cpp]
Exit: 0 = every scenario passed, 1 = a scenario or the compile failed.

    # the "fix before / pass after" command
    python tools/test_portal_wifi_state_machine.py
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SOURCE = os.path.join(ROOT, "src", "main.cpp")

# Reuse the repo's comment stripper / brace matcher (the same helper the
# WS-433 OTA guard uses) so extraction never trips over braces in comments.
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from check_ota_write_verify import _match_brace, strip_comments  # noqa: E402


# --- Minimal Arduino / ESP32 stubs -----------------------------------------
STUBS = r'''
#include <cstdio>
#include <cstring>
#include <string>

// wl_status_t values match the Arduino/ESP32 WiFi API.
typedef enum {
  WL_IDLE_STATUS      = 0,
  WL_NO_SSID_AVAIL    = 1,
  WL_SCAN_COMPLETED   = 2,
  WL_CONNECTED        = 3,
  WL_CONNECT_FAILED   = 4,
  WL_CONNECTION_LOST  = 5,
  WL_DISCONNECTED     = 6
} wl_status_t;

// The smallest String that supports every operation wifiAttemptSnapshot()
// performs: length(), != const char*, + (const char*/String in both orders),
// String(unsigned long) and assignment from a literal / another String.
struct String {
  std::string s;
  String() : s() {}
  String(const char* p) : s(p ? p : "") {}
  String(const std::string& v) : s(v) {}
  String(unsigned long v) : s(std::to_string(v)) {}
  unsigned int length() const { return static_cast<unsigned int>(s.size()); }
  bool operator==(const char* p) const { return s == (p ? p : ""); }
  bool operator!=(const char* p) const { return s != (p ? p : ""); }
};
inline String operator+(const char* a, const String& b) {
  return String(std::string(a) + b.s);
}
inline String operator+(const String& a, const char* b) {
  return String(a.s + std::string(b));
}
inline String operator+(const String& a, const String& b) {
  return String(a.s + b.s);
}

// Driver-controlled device state.
static wl_status_t g_status = WL_DISCONNECTED;
static const char* g_ip = "192.168.1.50";
static unsigned long g_now = 0;
unsigned long millis() { return g_now; }

struct IPAddrStub { String toString() const { return String(g_ip); } };
struct WiFiStub {
  wl_status_t status() const { return g_status; }
  IPAddrStub localIP() const { return IPAddrStub(); }
};
static WiFiStub WiFi;
'''


# Globals the production function reads. The harness owns them so each
# scenario can set up a device state; none of the *logic* lives here.
GLOBALS = r'''
static String        g_wifiTargetSsid;
static unsigned long g_wifiAttemptStart = 0;
static unsigned long g_wifiLastOkAt     = 0;
static bool          g_wifiWasConnected = false;
static bool          g_wifiAttemptActive = true;
static int           g_wifiFailReason   = 0;
'''


DRIVER = r'''
// --- Test driver: fixture + scenario assertions ---------------------------
static int g_failures = 0;

static void expect(bool ok, const char* label, const char* got) {
  std::printf("    %s  %-60s %s\n", ok ? "PASS" : "FAIL", label,
              got ? got : "");
  if (!ok) ++g_failures;
}
static bool contains(const String& s, const char* needle) {
  return s.s.find(needle) != std::string::npos;
}
static void show(const char* label, const WifiAttempt& a) {
  std::printf("  %-26s wifi=%-10s elapsed=%-3lu text=%s\n",
              label, a.wifi, a.elapsed, a.wifiText.s.c_str());
}
static void fixture(const char* ssid, unsigned long start, bool active) {
  g_wifiTargetSsid = String(ssid);
  g_wifiAttemptStart = start;
  g_wifiLastOkAt = 0;
  g_wifiWasConnected = false;
  g_wifiFailReason = 0;
  g_wifiAttemptActive = active;
  g_now = start;
}

int main() {
  std::puts("[1-3] connected 60 s, one dropped poll, then 25 s without an IP");
  fixture("Home", 0UL, true);
  g_now = 60000UL; g_status = WL_CONNECTED; g_ip = "192.168.1.50";
  WifiAttempt a1 = wifiAttemptSnapshot();
  show("t=60s CONNECTED", a1);
  expect(std::strcmp(a1.wifi, "ok") == 0,
         "S1 t=60s CONNECTED -> ok", a1.wifi);
  expect(contains(a1.wifiText, "已连接 Home") &&
             contains(a1.wifiText, "192.168.1.50"),
         "S1 text shows SSID + IP", a1.wifiText.s.c_str());

  g_now = 60050UL; g_status = WL_DISCONNECTED;
  WifiAttempt a2 = wifiAttemptSnapshot();
  show("t=60.05s 1-poll drop", a2);
  expect(std::strcmp(a2.wifi, "connecting") == 0,
         "S2 one WL_DISCONNECTED -> still connecting (NOT fail)", a2.wifi);
  expect(!contains(a2.wifiText, "密码"),
         "S2 text must not blame the password", a2.wifiText.s.c_str());
  expect(!contains(a2.wifiText, "连接失败"),
         "S2 must not be a failure text", a2.wifiText.s.c_str());

  g_now = 85000UL; g_status = WL_DISCONNECTED;
  WifiAttempt a3 = wifiAttemptSnapshot();
  show("t=85s still no IP", a3);
  expect(std::strcmp(a3.wifi, "fail") == 0,
         "S3 25 s without IP -> fail", a3.wifi);

  std::puts("[4] explicit failure codes keep their wording after 25 s");
  fixture("Home", 100000UL, true);
  g_now = 125000UL; g_status = WL_NO_SSID_AVAIL;
  WifiAttempt b1 = wifiAttemptSnapshot();
  show("+25s WL_NO_SSID_AVAIL", b1);
  expect(std::strcmp(b1.wifi, "fail") == 0,
         "S4a WL_NO_SSID_AVAIL -> fail", b1.wifi);
  expect(contains(b1.wifiText, "找不到该 WiFi") &&
             contains(b1.wifiText, "隐藏网络"),
         "S4a 5 GHz / hidden-network wording unchanged", b1.wifiText.s.c_str());

  fixture("Home", 200000UL, true);
  g_now = 225000UL; g_status = WL_CONNECT_FAILED;
  WifiAttempt b2 = wifiAttemptSnapshot();
  show("+25s WL_CONNECT_FAILED", b2);
  expect(std::strcmp(b2.wifi, "fail") == 0,
         "S4b WL_CONNECT_FAILED -> fail", b2.wifi);
  expect(contains(b2.wifiText, "密码可能不对"),
         "S4b wrong-password wording unchanged", b2.wifiText.s.c_str());

  std::puts("[5] portal opened without a user save/retry, out of range > 25 s");
  fixture("Home", 300000UL, false);
  g_now = 360000UL; g_status = WL_DISCONNECTED;
  WifiAttempt c1 = wifiAttemptSnapshot();
  show("portal open, 60 s", c1);
  expect(std::strcmp(c1.wifi, "fail") != 0,
         "S5 portal open must NOT judge fail", c1.wifi);
  expect(contains(c1.wifiText, "未连接"),
         "S5 stays 未连接", c1.wifiText.s.c_str());

  g_status = WL_CONNECTED; g_ip = "192.168.1.50";
  WifiAttempt c2 = wifiAttemptSnapshot();
  show("portal open, linked", c2);
  expect(std::strcmp(c2.wifi, "ok") == 0,
         "S5b already linked on portal open -> ok", c2.wifi);

  std::printf("\n%s (%d failure%s)\n",
              g_failures ? "FAILED" : "ALL PASS",
              g_failures, g_failures == 1 ? "" : "s");
  return g_failures ? 1 : 0;
}
'''


def _extract_function(raw: str, clean: str) -> str:
    """Verbatim text of ``static WifiAttempt wifiAttemptSnapshot() { ... }``."""
    sig = "static WifiAttempt wifiAttemptSnapshot()"
    at = clean.find(sig)
    if at < 0:
        raise SystemExit("FAIL: wifiAttemptSnapshot() not found in source")
    brace = clean.find("{", at)
    if brace < 0:
        raise SystemExit("FAIL: wifiAttemptSnapshot() has no opening brace")
    end = _match_brace(clean, brace)
    if end < 0:
        raise SystemExit("FAIL: wifiAttemptSnapshot() has no closing brace")
    return raw[at:end + 1]


def _extract_struct(raw: str, clean: str) -> str:
    """Verbatim text of ``struct WifiAttempt { ... };``."""
    at = clean.find("struct WifiAttempt")
    if at < 0:
        raise SystemExit("FAIL: struct WifiAttempt not found in source")
    brace = clean.find("{", at)
    end = _match_brace(clean, brace)
    semi = clean.find(";", end)
    if brace < 0 or end < 0 or semi < 0:
        raise SystemExit("FAIL: struct WifiAttempt is malformed")
    return raw[at:semi + 1]


def _extract_timeout_define(raw: str) -> str:
    m = re.search(r"^#define\s+WIFI_ATTEMPT_TIMEOUT_MS\s+\S+", raw, re.M)
    if not m:
        raise SystemExit("FAIL: WIFI_ATTEMPT_TIMEOUT_MS define not found")
    return m.group(0)


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_SOURCE
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read()
    clean = strip_comments(raw)

    func_src = _extract_function(raw, clean)
    struct_src = _extract_struct(raw, clean)
    define_line = _extract_timeout_define(raw)

    # Sanity: we really pulled the production function, not an empty stub.
    for token in ("WiFi.status()", "WIFI_ATTEMPT_TIMEOUT_MS", "g_wifiFailReason"):
        if token not in func_src:
            raise SystemExit("FAIL: extracted function is missing %r" % token)

    digest = hashlib.sha256(func_src.encode("utf-8")).hexdigest()[:12]
    print("source: %s" % path)
    print("wifiAttemptSnapshot(): extracted verbatim (%d chars, sha256 %s)"
          % (len(func_src), digest))
    print("timeout: %s" % define_line.strip())
    print("")

    cpp = "\n".join([STUBS, define_line, struct_src, GLOBALS, func_src, DRIVER])

    with tempfile.TemporaryDirectory(prefix="bdl_wifi_state_") as tmp:
        cpp_path = os.path.join(tmp, "harness.cpp")
        bin_path = os.path.join(tmp, "harness")
        with open(cpp_path, "w", encoding="utf-8") as handle:
            handle.write(cpp)
        build = subprocess.run(
            ["g++", "-std=c++17", "-O0", "-o", bin_path, cpp_path],
            capture_output=True, text=True)
        if build.returncode != 0:
            sys.stdout.write(build.stdout)
            sys.stderr.write(build.stderr)
            print("FAILED (harness did not compile)")
            return 1
        run = subprocess.run([bin_path], capture_output=True, text=True)
        sys.stdout.write(run.stdout)
        sys.stderr.write(run.stderr)
        return run.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
