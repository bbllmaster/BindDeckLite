#!/usr/bin/env python3
"""WS-635 regression: an aborted portal OTA must release the scan refusal.

The firmware has no test framework and a PC cannot run an ESP32 WebServer, so
this harness does what ``tools/test_portal_wifi_state_machine.py`` does for the
WiFi state machine: it lifts the **production** ``/update`` upload callback
(``cfgServer.on("/update", HTTP_POST, <final>, <upload>)`` - the second lambda)
verbatim out of ``src/main.cpp``, drops it next to minimal ``WebServer`` /
``Update`` / ``HTTPUpload`` stubs, compiles it with g++ and drives the four
statuses the framework actually delivers.

Why it exists: ``WebServer::_parseFormUploadAborted()`` (Parsing.cpp) hands the
upload callback ``UPLOAD_FILE_ABORTED`` but returns false, so
``_handleRequest()`` - and with it this route's final handler - never runs. Any
cleanup that lives only in the final handler is skipped on a cancelled upload.
Before WS-635 the callback ignored that status, ``g_portalOtaActive`` stayed
true and ``/wifi/scan`` answered ``409 ota_busy`` for the rest of the portal
session.

Usage:
    python tools/test_portal_ota_abort.py [path/to/main.cpp]
Exit: 0 = every scenario passed, 1 = a scenario or the compile failed.

    # the "fails before / passes after" command
    python tools/test_portal_ota_abort.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SOURCE = os.path.join(ROOT, "src", "main.cpp")

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from check_ota_write_verify import _match_brace, strip_comments  # noqa: E402


# --- Minimal Arduino / WebServer / Update stubs -----------------------------
STUBS = r'''
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

#define UPDATE_SIZE_UNKNOWN 0xFFFFFFFFu
#define F(x) (x)
#define log_e(fmt, ...) std::printf("      [log] " fmt "\n", ##__VA_ARGS__)

// Matches WebServer.h's UploadStatus order; only the names matter here.
enum UploadStatus {
  UPLOAD_FILE_START = 0,
  UPLOAD_FILE_WRITE,
  UPLOAD_FILE_END,
  UPLOAD_FILE_ABORTED
};

struct String {
  std::string s;
  String() : s() {}
  String(const char* p) : s(p ? p : "") {}
  const char* c_str() const { return s.c_str(); }
  unsigned int length() const { return static_cast<unsigned int>(s.size()); }
};

struct esp_partition_t {};

inline const esp_partition_t* esp_ota_get_next_update_partition(
    const esp_partition_t* = nullptr) {
  return nullptr;
}

// The fields the upload callback reads off HTTPUpload.
struct HTTPUpload {
  UploadStatus status = UPLOAD_FILE_START;
  String filename;
  size_t totalSize = 0;
  size_t currentSize = 0;
  uint8_t buf[4] = {0, 0, 0, 0};
};

struct CfgServerStub {
  HTTPUpload up;
  HTTPUpload& upload() { return up; }
};
static CfgServerStub cfgServer;

// Instrumented Update: isRunning()/abort() are what the abort branch uses.
struct UpdateStub {
  bool running = false;
  bool error = false;
  int err = 0;
  int begins = 0;
  int rejects = 0;   // begin() while a session is still open
  int aborts = 0;
  int ends = 0;
  bool begin(size_t) {
    ++begins;
    if (running) {               // production: "already running"
      ++rejects;
      return false;
    }
    running = true;
    return true;
  }
  void abort() {
    ++aborts;
    running = false;
  }
  bool end(bool) {
    ++ends;
    running = false;
    return true;
  }
  bool hasError() const { return error; }
  int getError() const { return err; }
  bool isRunning() const { return running; }
};
static UpdateStub Update;
'''

# The firmware globals the callback reads / writes, owned by the harness.
GLOBALS = r'''
static int         g_otaProgress = -1;
static uint32_t    g_otaReceived = 0;
static bool        g_portalOtaActive = false;
static const char* g_portalOtaError = nullptr;
static const char* g_portalOtaCode = nullptr;

// Gate result, driven per scenario (false = capacity/write-protect refusal).
static bool g_gateOk = true;

static void otaResetSkip() {}
static void otaCaptureSkip(const uint8_t*, size_t) {}
static void otaWriteReset() {}
static bool otaVerifyFirstBlock() { return true; }
static const char* otaWrite(const uint8_t*, size_t) { return nullptr; }
static const char* otaWriteFinish() { return nullptr; }
static const char* otaUpdateErrorText(int) { return "end_failed:test"; }
static void otaDiagReadback(int) {}
static bool otaFlashPrepare(const esp_partition_t*, uint32_t, const char** reason) {
  if (!g_gateOk) {
    *reason = "flash_too_small";
    return false;
  }
  return true;
}
'''

DRIVER = r'''
// --- Driver: push HTTPUpload statuses through the real callback -------------
static int g_failures = 0;

static void expect(bool ok, const char* label, const char* got) {
  std::printf("    %s  %-58s %s\n", ok ? "PASS" : "FAIL", label, got ? got : "");
  if (!ok) ++g_failures;
}
static const char* b2s(bool v) { return v ? "true" : "false"; }

static void startUpload() {
  cfgServer.up.status = UPLOAD_FILE_START;
  cfgServer.up.filename = String("ota_binddecklite_c3_v1.3.2.bin");
  uploadCb();
}
static void writeChunk(size_t n) {
  cfgServer.up.status = UPLOAD_FILE_WRITE;
  cfgServer.up.currentSize = n;
  cfgServer.up.totalSize = 4096;
  uploadCb();
}
static void endUpload() {
  cfgServer.up.status = UPLOAD_FILE_END;
  uploadCb();
}
static void abortUpload() {
  cfgServer.up.status = UPLOAD_FILE_ABORTED;
  uploadCb();
}

int main() {
  std::puts("[1] START arms the OTA-in-flight flag");
  startUpload();
  expect(g_portalOtaActive, "START -> g_portalOtaActive true", b2s(g_portalOtaActive));
  expect(Update.begins == 1, "START -> Update.begin() called", "");
  expect(Update.running, "START -> Update session open", b2s(Update.running));

  std::puts("[2] a partial WRITE keeps the session (and the flag)");
  writeChunk(1024);
  expect(g_otaReceived == 1024, "WRITE -> bytes accumulate", "");
  expect(g_portalOtaActive, "WRITE -> still flagged in flight", b2s(g_portalOtaActive));

  std::puts("[3] ABORTED releases the flag WITHOUT the final handler");
  abortUpload();
  expect(!g_portalOtaActive,
         "ABORTED -> g_portalOtaActive false (/wifi/scan not 409)",
         b2s(g_portalOtaActive));
  expect(!Update.running, "ABORTED -> Update session closed", b2s(Update.running));
  expect(Update.aborts == 1, "ABORTED -> Update.abort() called", "");

  std::puts("[4] the same portal session can upload again");
  const int beginsBefore = Update.begins;
  const int rejectsBefore = Update.rejects;
  startUpload();
  expect(Update.begins == beginsBefore + 1, "retry START -> Update.begin() attempted", "");
  expect(Update.rejects == rejectsBefore,
         "retry START -> Update.begin() accepted (not \"already running\")", "");
  expect(Update.running, "retry START -> session open again", b2s(Update.running));
  expect(g_portalOtaActive, "retry START -> flagged in flight again",
         b2s(g_portalOtaActive));

  std::puts("[5] abort before Update.begin() (gate refused) also clears the flag");
  g_gateOk = false;
  Update.running = false;
  Update.aborts = 0;
  startUpload();                       // refused at the gate, never begins
  expect(g_portalOtaError != nullptr, "gate refusal records the reason", "");
  expect(g_portalOtaActive, "gate refusal still flagged (nothing written yet)",
         b2s(g_portalOtaActive));
  abortUpload();
  expect(!g_portalOtaActive, "ABORTED after gate refusal -> flag cleared",
         b2s(g_portalOtaActive));
  expect(Update.aborts == 0, "no Update.abort() when nothing is running", "");

  std::puts("[6] a clean END leaves the release to the request handler");
  g_gateOk = true;
  startUpload();
  endUpload();
  expect(Update.ends == 1, "END -> Update.end() called", "");
  // The final handler (which clears the flag) only runs when the body parsed;
  // the callback must not clear it behind the handler's back on a success path
  // where the handler still has to report the result.
  expect(g_portalOtaActive, "END -> flag untouched inside the callback",
         b2s(g_portalOtaActive));

  std::printf("\n%s (%d failure%s)\n", g_failures ? "FAILED" : "ALL PASS",
              g_failures, g_failures == 1 ? "" : "s");
  return g_failures ? 1 : 0;
}
'''


def _extract_upload_callback(raw: str, clean: str) -> str:
    """Verbatim ``[]() { ... }`` of the /update upload callback."""
    marker = "HTTPUpload& up = cfgServer.upload();"
    at = clean.find(marker)
    if at < 0:
        raise SystemExit("FAIL: /update upload callback not found in source")
    lam = clean.rfind("[]() {", 0, at)
    if lam < 0:
        raise SystemExit("FAIL: upload callback lambda header not found")
    brace = clean.find("{", lam)
    end = _match_brace(clean, brace)
    if end < 0:
        raise SystemExit("FAIL: upload callback has no closing brace")
    return raw[lam:end + 1]


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_SOURCE
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read()
    clean = strip_comments(raw)

    lambda_src = _extract_upload_callback(raw, clean)
    # Sanity: the production statuses really came along.
    for token in ("UPLOAD_FILE_START", "UPLOAD_FILE_WRITE", "UPLOAD_FILE_END",
                  "g_portalOtaActive"):
        if token not in lambda_src:
            raise SystemExit("FAIL: extracted callback is missing %r" % token)

    # The lambda is the handler; name it so the driver can call it.
    handler = "static void uploadCb()" + lambda_src[len("[]()"):]

    program = "\n".join([STUBS, GLOBALS, handler, DRIVER])
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "harness.cpp")
        exe = os.path.join(tmp, "harness")
        with open(src, "w", encoding="utf-8") as handle:
            handle.write(program)
        build = subprocess.run(
            ["g++", "-std=c++17", "-O0", "-Wall", "-o", exe, src],
            capture_output=True, text=True,
        )
        if build.returncode != 0:
            sys.stderr.write(build.stderr)
            print("FAIL: harness did not compile")
            return 1
        run = subprocess.run([exe], capture_output=True, text=True)
        sys.stdout.write(run.stdout)
        if run.stderr:
            sys.stderr.write(run.stderr)
        return run.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
