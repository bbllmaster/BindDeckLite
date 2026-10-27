"""Regression guards for the WS-420 OTA flash probes and write gate.

Why this file exists
--------------------
The 16 MB C3 build streams a 1.3 MB image, `esp_ota_write()` reports success
for every byte, and yet the OTA slot reads back 0xFF — `Update.end()` then
fails with ``read_error(3)``.  WS-378 added a read-back probe that separated
"write rejected" from "read rejected" but could not say which world the board
is in.  WS-420 adds the decisive experiment (pattern -> reboot -> read) plus
the two guards that make the failure cheap and precise.

What is guarded here
--------------------
  * probe wiring: ``CMD:FLASHTEST`` on both consoles and one automatic run at
    boot, before anything else can touch the OTA slot;
  * probe content: driver size, physical size, JEDEC size, chip id and the
    write-protect bit; the five app1 offsets including the one that crosses
    4 MB; the erase/write/read round-trip and the SPI-vs-cache (mmap) pair of
    views that separates W1 from W2; the pattern/reboot persistence marker;
  * probe safety: only the OTA target partition is erased/written, and the
    probe is skipped when that partition is the one currently executing;
  * the write gate: a slot that ends past the flash driver's size is refused
    with ``flash_too_small`` before the first byte, and a protected part is
    cleared and logged — on both the BLE and the config-portal channel;
  * the fail-fast check: the first flushed 4 KB block is read back and
    compared (offset 16, never offset 0 — the Update library withholds the
    first 16 bytes until ``end()``), and a mismatch aborts the session with
    ``flash_not_written`` instead of streaming the rest;
  * nothing is loosened: the SHA-256 gate and ``Update.end(true)`` stay.

Why source contract tests
-------------------------
The firmware is C++ for ESP32/ESP32-C3 and CI has no hardware and no
esp32 toolchain on the host runners, so — exactly like
``host/test_ota_diag.py`` (WS-378) — these assertions read the production
sources instead of executing them.  They fail on the pre-WS-420 tree and pass
on this one; the behavioural proof is the ``[ota][test]`` log from a real
board.

Run:  pytest host/test_ota_flash_guard.py -v
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_CPP = os.path.join(REPO_ROOT, "src", "main.cpp")
PARTITIONS_16MB = os.path.join(REPO_ROOT, "partitions", "binddecklite_16mb_c3.csv")

# The probe section, from its banner to the next top-level helper.
PROBE_BANNER = "=== OTA flash probes + write gate (WS-420)"
PROBE_END = "static int otaHexNibble(char c)"


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _source():
    return _read(MAIN_CPP)


def _probe_section(src=None):
    src = src if src is not None else _source()
    start = src.index(PROBE_BANNER)
    end = src.index(PROBE_END, start)
    return src[start:end]


def _match_brace(src, brace):
    """Index of the '}' matching the '{' at *brace*, ignoring comments/strings."""
    depth = 0
    state = "code"
    i = brace
    while i < len(src):
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block"
                i += 2
                continue
            if ch == '"':
                state = "string"
            elif ch == "'":
                state = "char"
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        elif state == "line":
            if ch == "\n":
                state = "code"
        elif state == "block":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
        elif state in ("string", "char"):
            if ch == "\\":
                i += 2
                continue
            if (state == "string" and ch == '"') or (state == "char" and ch == "'"):
                state = "code"
        i += 1
    raise AssertionError("unbalanced braces at offset %d" % brace)


def _function_body(src, signature):
    """Return the text of the definition that starts at *signature*.

    Forward declarations in this file repeat the same signature and end with
    ';', so occurrences are skipped until one is followed by a body.
    """
    pos = 0
    while True:
        start = src.find(signature, pos)
        if start < 0:
            raise AssertionError("no definition found for %r" % signature)
        i = start + len(signature)
        parens = 0
        while i < len(src):
            ch = src[i]
            if ch == "(":
                parens += 1
            elif ch == ")":
                parens -= 1
            elif parens == 0 and ch == ";":
                break          # forward declaration
            elif parens == 0 and ch == "{":
                return src[start:_match_brace(src, i) + 1]
            i += 1
        pos = start + 1


def _branch(src, needle):
    """Return the ``{...}`` block that follows *needle* (used for if-branches)."""
    start = src.index(needle)
    brace = src.index("{", start)
    return src[brace:_match_brace(src, brace) + 1]


def _partition_offset(name):
    """Offset/size of *name* from the 16 MB partition table (production data)."""
    for line in _read(PARTITIONS_16MB).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split(",")]
        if fields[0] == name:
            return int(fields[3], 16), int(fields[4], 16)
    raise AssertionError("partition %r not found" % name)


# --- probe wiring ---------------------------------------------------------

def test_flashtest_command_is_wired():
    src = _source()
    assert 'data.startsWith("CMD:FLASHTEST")' in src, "CMD:FLASHTEST is not handled"
    branch = _branch(src, 'else if (data.startsWith("CMD:FLASHTEST"))')
    assert "otaTestRun(" in branch, "CMD:FLASHTEST must call the probe suite"
    # The persistence experiment ends with a restart, but the rest must also be
    # runnable on a device the user does not want rebooted.
    assert "NORESTART" in branch, "CMD:FLASHTEST:NORESTART must skip the reboot"
    # No bare ESP.restart() from the command handler: processCommand runs on the
    # NimBLE host task (WS-366), so the reboot goes through loop().
    assert "ESP.restart()" not in branch
    assert "g_bootReq = BOOT_REBOOT" in branch


def test_probe_runs_once_at_boot_before_other_flash_users():
    src = _source()
    setup = _function_body(src, "void setup()")
    assert "otaTestOnBoot();" in setup, "boot probe is not called from setup()"
    assert setup.index("otaTestOnBoot();") < setup.index("ensureNvs();"), \
        "the probe must read the persistence marker before NVS/flash users run"
    on_boot = _function_body(src, "static void otaTestOnBoot()")
    # Persistence marker first, suite afterwards: the suite erases the slot.
    assert on_boot.index("persist_read") < on_boot.index('otaTestRun("boot", false)'), \
        "the suite would erase app1+0 before the marker is read"
    assert "OTA_TEST_RESTART_MAGIC" in on_boot
    # One-shot: the flag is consumed before it is acted on, so a crash in the
    # probe cannot turn into a boot loop.
    assert on_boot.index("g_otaTestRestart = 0") < on_boot.index("esp_partition_read"), \
        "consume the restart marker before reading it"


# --- probe content --------------------------------------------------------

def test_probe_reports_capacity_identity_and_write_protect():
    probe = _probe_section()
    for token in ("esp_flash_get_size(", "esp_flash_get_physical_size(",
                  "ESP.getFlashChipSize()", "esp_flash_read_id(",
                  "esp_flash_get_chip_write_protect("):
        assert token in probe, "probe is missing %s" % token
    for field in ("drv_size=", "phys_size=", "jedec_kb=", "chip_id=", "wp="):
        assert field in probe, "probe log is missing the %r data point" % field
    # A driver/physical mismatch is the headline clue, so it gets a verdict.
    assert "driver_phys_mismatch" in probe


def test_probe_covers_five_offsets_and_crosses_the_4mb_line():
    probe = _probe_section()
    offsets_block = probe[probe.index("OTA_TEST_OFFSETS[]"):]
    offsets_block = offsets_block[:offsets_block.index("};")]
    offsets = [int(v, 16) for v in re.findall(r"0x([0-9A-Fa-f]+)u", offsets_block)]
    assert offsets == [0x000000, 0x040000, 0x0E0000, 0x0F0000, 0x200000], offsets

    app1_addr, app1_size = _partition_offset("app1")
    assert app1_addr == 0x310000 and app1_size == 0x300000
    crossing = [off for off in offsets if app1_addr + off >= 0x400000]
    just_below = [off for off in offsets if app1_addr + off < 0x400000]
    assert crossing, "no probe offset crosses the 4 MB line"
    assert min(crossing) == 0x0F0000, "0x0F0000 must be the first offset past 4 MB"
    assert just_below and max(just_below) == 0x0E0000
    # Everything stays inside app1: the probes may only scribble on the OTA slot.
    assert max(offsets) + 4096 <= app1_size


def test_probe_round_trip_reports_every_step_and_both_views():
    probe = _probe_section()
    for field in ("pre=", "erase_err=", "erased=", "write_err=", "read_err=", "post=", "match="):
        assert field in probe, "round-trip log is missing %r" % field
    # W2 (write landed, cache view stale) is only distinguishable with the
    # mapped view next to the direct SPI read, so both must be printed.
    assert "esp_partition_mmap(" in probe
    assert "mmap=" in probe and "same=" in probe


def test_probe_writes_only_the_ota_target_partition():
    probe = _probe_section()
    guard = _function_body(probe, "static const esp_partition_t* otaTestTarget")
    assert "esp_ota_get_next_update_partition(" in guard
    assert "target_is_running" in guard, "the running app must never be probed"
    # Every erase/write goes through the target partition pointer, never a
    # literal address (that is what keeps app0 / storage / NVS out of reach).
    for call in ("esp_partition_erase_range(", "esp_partition_write("):
        for match in re.finditer(re.escape(call) + r"([^;]*);", probe):
            args = match.group(1)
            assert args.strip().startswith("part,"), \
                "%s must be called on the target partition: %r" % (call, args)
    for label in ("app0", "storage", "nvs"):
        assert '"%s"' % label not in probe, "probe must not name the %s partition" % label


def test_probe_persists_a_pattern_across_a_restart():
    probe = _probe_section()
    assert "OTA_TEST_RESTART_MAGIC" in probe
    assert "RTC_NOINIT_ATTR" in probe, "the marker must survive ESP.restart()"
    persist = _function_body(probe, "static void otaTestArmPersistence")
    assert "esp_partition_write(part, 0," in persist
    assert "esp_partition_read(part, 0," in persist, "must verify before rebooting"
    on_boot = _function_body(probe, "static void otaTestOnBoot")
    assert "survived_reboot(W2_data_is_there)" in on_boot
    assert "lost_across_reboot(W1_data_absent)" in on_boot


def test_probe_does_not_autorun_on_small_flash_boards():
    probe = _probe_section()
    on_boot = _function_body(probe, "static void otaTestOnBoot")
    assert "OTA_TEST_AUTORUN_MIN_SIZE" in on_boot
    assert 'otaTestRun("boot", false)' in on_boot
    # 2 MB / 4 MB boards already OTA fine; the destructive suite stays opt-in.
    assert re.search(r"#define OTA_TEST_AUTORUN_MIN_SIZE 0x800000u", probe)
    # The decision uses the JEDEC size: a driver stuck at 4 MB (the failure
    # being investigated) must not be able to switch its own probe off.
    assert "ESP.getFlashChipSize()" in on_boot
    assert "esp_flash_get_size(esp_flash_default_chip" not in on_boot


# --- write gate -----------------------------------------------------------

def test_gate_refuses_a_slot_past_the_driver_size():
    gate = _function_body(_source(), "static bool otaFlashPrepare")
    assert "esp_flash_get_size(" in gate and "esp_flash_get_physical_size(" in gate
    assert "partEnd > drvSize" in gate, "the capacity check must use the driver size"
    assert '"flash_too_small"' in gate
    # Refusing means reporting a reason, not silently continuing.
    assert gate.index('"flash_too_small"') < gate.index("g_otaPart = part")
    assert "return false" in gate


def test_gate_clears_write_protect_and_logs_before_and_after():
    gate = _function_body(_source(), "static bool otaFlashPrepare")
    assert "esp_flash_get_chip_write_protect(" in gate
    assert "esp_flash_set_chip_write_protect(esp_flash_default_chip, false)" in gate
    assert "cleared err=" in gate, "the result of clearing must be logged"
    assert gate.index("esp_flash_get_chip_write_protect(") < gate.index(
        "esp_flash_set_chip_write_protect(")


def test_both_ota_channels_run_the_gate():
    src = _source()
    assert src.count("otaFlashPrepare(") >= 3, \
        "expected the definition plus one call per OTA channel (BLE + portal)"
    portal = src[src.index('cfgServer.on("/update", HTTP_POST'):]
    portal = portal[:portal.index("cfgServer.onNotFound")]
    assert "otaFlashPrepare(" in portal, "config-portal OTA must be gated too"
    assert "otaVerifyFirstBlock()" in portal
    ble = src[src.index("static void processCommand(String data)"):]
    assert "otaFlashPrepare(" in ble


def test_portal_refusal_is_reported_instead_of_success():
    src = _source()
    portal = src[src.index('cfgServer.on("/update", HTTP_POST'):]
    portal = portal[:portal.index("cfgServer.onNotFound")]
    # A refused slot must not be presented as "升级完成" + restart.
    assert "g_portalOtaError" in portal
    assert "未开始" in portal
    assert portal.index("g_portalOtaError) {") < portal.index("ESP.restart();")
    # The write branch must stay idle while the gate has refused the slot.
    assert "if (g_portalOtaError) return;" in portal


# --- fail-fast read-back --------------------------------------------------

def test_first_block_check_uses_the_streamed_block_not_the_withheld_header():
    verify = _function_body(_source(), "static bool otaVerifyFirstBlock")
    assert "const size_t off = 16;" in verify, \
        "bytes 0..15 are withheld by the Update library until end()"
    assert "g_otaSkip + off" in verify
    assert "g_otaReceived < 2 * 4096" in verify, \
        "wait until the first 4 KB block has actually been flushed"
    # Never judge offset 0 mid-stream: it is 0xFF by design there.
    assert "esp_partition_read(g_otaPart, 0," not in verify


def test_first_block_mismatch_aborts_with_a_specific_reason():
    src = _source()
    verify = _function_body(src, "static bool otaVerifyFirstBlock")
    assert "first_block_lost" in verify
    assert "return false" in verify
    stream = src[src.index("bleConfig.onOtaData"):]
    stream = stream[:stream.index("bleConfig.onDisconnect")]
    assert "otaVerifyFirstBlock()" in stream
    assert 'otaAbort("flash_not_written")' in stream


def test_probe_is_skipped_while_an_ota_is_in_flight():
    src = _source()
    branch = _branch(src, 'else if (data.startsWith("CMD:FLASHTEST"))')
    assert "g_otaMode" in branch, "the probe must not run during an update"


# --- nothing loosened -----------------------------------------------------

def test_image_verification_is_not_relaxed():
    src = _source()
    assert "Update.end(true)" in src, "the committing end(true) must stay"
    assert "Update.end(false)" not in src
    assert "memcmp(digest, g_otaShaExp, OTA_SHA_LEN) != 0" in src, \
        "the SHA-256 gate must stay in front of the commit"
    assert "g_otaReceived != g_otaTotal" in src, "the size gate must stay"


def test_ws378_diag_still_copies_only_sixteen_bytes():
    src = _source()
    diag = _function_body(src, "static void otaDiagReadback")
    assert "uint32_t hdr[4];" in diag
    assert "memcpy(hdr, g_otaSkip, sizeof(hdr));" in diag, \
        "g_otaSkip is 64 bytes now; the 16-byte header copy must stay bounded"
