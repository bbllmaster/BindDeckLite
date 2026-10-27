#!/usr/bin/env python3
"""Guard the WS-433 OTA write path: read-back per block, repair, probe.

The 16 MB C3 slot accepts every byte of a 1.35 MB OTA with ESP_OK and reads
back 0xFF, so a write that trusts its return code cannot work.  WS-433 makes
the write path prove itself -- each 4 KB block is read back through
``esp_partition_read`` and, when the slot did not keep it, erased and
programmed again page by page -- and adds the ``[ota][wt]`` probe that says
which layer (page crossing / app1 only / whole chip) is broken.

This checker reads the *production* source (``src/main.cpp``) and asserts the
structure that makes those two claims true.  It is deliberately structural:
it fails on the pre-WS-433 tree (where every channel calls
``Update.write()`` directly and nothing is ever read back) and it fails again
if a later change quietly drops the verification, the repair, or the probe.

What it asserts
---------------
1. both OTA channels stream through ``otaWrite()`` and no channel calls
   ``Update.write()`` directly any more;
2. ``otaWrite()`` hands Update whole 4 KB blocks and asks for a read-back
   after every one of them;
3. ``otaWbCheckBlock()`` compares the read-back, repairs once on a mismatch,
   compares again, and only then returns ``write_not_verified``;
4. ``otaWbRepair()`` erases the covering sector and reprograms it in
   ``chip_drv->page_size`` units, never spanning a page;
5. the trailing partial block is erased before it is written (Update only
   erases at 64 KB boundaries, which is how the *last* sector of every image
   got programmed over stale bytes);
6. the ``[ota][wt]`` probe writes 1 / 256 / 4080 bytes on app1 and on the
   ``storage`` tail, and reports the raw ``esp_flash_write()`` code;
7. both session start points reset the block bookkeeping.

Usage:  python tools/check_ota_write_verify.py [path/to/main.cpp]
Exit:   0 = all checks pass, 1 = at least one structural defect found.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SOURCE = os.path.join(ROOT, "src", "main.cpp")


def strip_comments(text: str) -> str:
    """Blank out // and /* */ comments while preserving line numbers."""
    out = []
    i = 0
    n = len(text)
    state = "code"
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line"
                out.append("  ")
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block"
                out.append("  ")
                i += 2
                continue
            if ch in "\"'":
                state = ch
            out.append(ch)
            i += 1
        elif state == "line":
            if ch == "\n":
                state = "code"
                out.append(ch)
            else:
                out.append(" ")
            i += 1
        elif state == "block":
            if ch == "*" and nxt == "/":
                state = "code"
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
        else:  # inside a string/char literal
            out.append(ch)
            if ch == "\\":
                if nxt:
                    out.append(nxt)
                    i += 2
                    continue
            elif ch == state:
                state = "code"
            i += 1
    return "".join(out)


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _match_brace(src: str, brace: int) -> int:
    """Index of the '}' matching the '{' at *brace* (comments already gone)."""
    depth = 0
    state = "code"
    i = brace
    while i < len(src):
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        if state == "code":
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
            elif ch == '"':
                state = "string"
            elif ch == "'":
                state = "char"
        elif state in ("string", "char"):
            if ch == "\\":
                i += 2
                continue
            if (state == "string" and ch == '"') or (state == "char" and ch == "'"):
                state = "code"
        i += 1
    raise ValueError("unbalanced braces at offset %d" % brace)


def body_after(code: str, marker: str) -> tuple[int, str]:
    """Text of the first ``{...}`` block at or after *marker*, plus its offset."""
    start = code.find(marker)
    if start < 0:
        return -1, ""
    brace = code.find("{", start)
    if brace < 0:
        return -1, ""
    try:
        return brace, code[brace : _match_brace(code, brace) + 1]
    except ValueError:
        return -1, ""


def function_body(code: str, signature: str) -> str:
    """Body of a function definition; forward declarations end in ';'."""
    pos = 0
    while True:
        start = code.find(signature, pos)
        if start < 0:
            return ""
        brace = code.find("{", start + len(signature))
        semi = code.find(";", start + len(signature))
        if brace < 0:
            return ""
        if 0 <= semi < brace:
            pos = start + len(signature)
            continue
        return code[start : _match_brace(code, brace) + 1]


def _require(failures: list[str], cond: bool, where: str, msg: str) -> bool:
    if not cond:
        failures.append("%s: %s" % (where, msg))
    return cond


def check_source(source: str) -> list[str]:
    """Return a list of human-readable defects; empty means healthy."""
    code = strip_comments(source)
    failures: list[str] = []

    # --- 1. both channels go through the verified writer ------------------
    for channel, marker, call in (
        ("BLE onOtaData", "bleConfig.onOtaData", "otaWrite(data, len)"),
        ("portal UPLOAD_FILE_WRITE", "up.status == UPLOAD_FILE_WRITE",
         "otaWrite(up.buf, up.currentSize)"),
    ):
        off, body = body_after(code, marker)
        where = "src/main.cpp:%s" % (line_of(code, off) if off >= 0 else "?")
        if not _require(failures, bool(body), where, "%s branch not found -- update this checker" % channel):
            continue
        _require(failures, call in body,
                 where,
                 "%s no longer streams through %s, so writes are unverified again" % (channel, call))
        _require(failures, "Update.write(" not in body,
                 where,
                 "%s calls Update.write() directly, bypassing the read-back/repair path" % channel)

    # --- 2. otaWrite hands over whole blocks and verifies each one --------
    off, body = body_after(code, "static const char* otaWrite(const uint8_t* data, size_t len) {")
    where = "src/main.cpp:%s otaWrite" % (line_of(code, off) if off >= 0 else "?")
    if _require(failures, bool(body), where, "verified writer not found"):
        _require(failures, re.search(r"Update\.write\([^;]*OTA_WB_BLOCK\)", body) is not None,
                 where, "must hand Update exactly one OTA_WB_BLOCK-sized block")
        _require(failures, "otaWbCheckReady()" in body,
                 where, "never asks for a read-back after a block was flushed")

    # --- 3. per-block read-back, one repair, then a verdict --------------
    off, body = body_after(code, "static const char* otaWbCheckBlock(const esp_partition_t* part, uint32_t index) {")
    where = "src/main.cpp:%s otaWbCheckBlock" % (line_of(code, off) if off >= 0 else "?")
    if _require(failures, bool(body), where, "block read-back function not found"):
        _require(failures, "esp_partition_read(" in body or "otaWbCompare(" in body,
                 where, "block is never read back from the slot")
        _require(failures, "mismatch" in body, where, "read-back is not compared with the streamed bytes")
        _require(failures, "OTA_WB_HEADER" in body,
                 where, "block 0 must skip the 16 bytes Update withholds until end()")
        reps = body.count("otaWbRepair(")
        _require(failures, reps >= 1, where, "a mismatching block is never repaired")
        _require(failures, body.count("otaWbCompare(") >= 2,
                 where, "the repair is not followed by a second read-back")
        repair_pos = body.find("otaWbRepair(")
        second = body.find("otaWbCompare(", body.find("otaWbCompare(") + 1)
        _require(failures, 0 <= repair_pos < second,
                 where, "repair must sit between the first and the second read-back")
        _require(failures, '"write_not_verified"' in body,
                 where, "a block that fails twice must abort with write_not_verified")

    # --- 4. repair erases the sector and reprograms page by page --------
    off, body = body_after(code, "static const char* otaWbRepair(const esp_partition_t* part, uint32_t off,")
    where = "src/main.cpp:%s otaWbRepair" % (line_of(code, off) if off >= 0 else "?")
    if _require(failures, bool(body), where, "repair function not found"):
        _require(failures, "esp_partition_erase_range(" in body, where, "repair must erase the sector first")
        _require(failures, "esp_partition_write(" in body, where, "repair must program the block back")
        _require(failures, "otaWbPageSize()" in body,
                 where, "repair must use the chip driver's page size, not a hard-coded one")
        _require(failures, re.search(r"%\s*page", body) is not None,
                 where, "repair writes must be page aligned")
        _require(failures, body.find("esp_partition_erase_range(") < body.find("esp_partition_write("),
                 where, "erase must happen before the reprogramming writes")

    # --- 5. the trailing partial block is erased before it is written ---
    off, body = body_after(code, "static const char* otaWriteFinish() {")
    where = "src/main.cpp:%s otaWriteFinish" % (line_of(code, off) if off >= 0 else "?")
    if _require(failures, bool(body), where, "finish path not found"):
        erase_pos = body.find("esp_partition_erase_range(")
        write_pos = body.find("Update.write(")
        _require(failures, erase_pos >= 0,
                 where, "the tail block is never erased; Update only erases at 64 KB boundaries")
        _require(failures, 0 <= erase_pos < write_pos,
                 where, "the tail sector must be erased before the tail bytes are handed over")
        _require(failures, "otaWbCheckReady()" in body,
                 where, "the tail does not trigger the read-back of the blocks it flushed")

    # --- 5b. both commit sites flush through it -------------------------
    for marker, label in (("CMD:OTA_END", "BLE"), ("up.status == UPLOAD_FILE_END", "portal")):
        off, body = body_after(code, marker)
        where = "src/main.cpp:%s %s commit" % (line_of(code, off) if off >= 0 else "?", label)
        _require(failures, "otaWriteFinish()" in body,
                 where, "the commit path never flushes/verifies the trailing block")

    # --- 6. session resets ------------------------------------------------
    for marker, label in (("static void otaResetState() {", "otaResetState"),
                          ("up.status == UPLOAD_FILE_START", "portal start"),
                          ("startsWith(\"CMD:OTA:\")", "BLE arm")):
        off, body = body_after(code, marker)
        where = "src/main.cpp:%s %s" % (line_of(code, off) if off >= 0 else "?", label)
        _require(failures, "otaWriteReset()" in body,
                 where, "session start does not reset the block/verify bookkeeping")

    # --- 7. the [ota][wt] probe ------------------------------------------
    off = code.find("#define OTA_WT_OFF")
    where = "src/main.cpp:%s [ota][wt]" % (line_of(code, off) if off >= 0 else "?")
    if _require(failures, off >= 0, where, "the [ota][wt] write probe is gone"):
        probe = code[off : code.find("static void processCommand(String data) {", off)]
        _require(failures, "esp_flash_write(" in probe,
                 where, "probe must report the raw esp_flash_write() return code")
        _require(failures, "esp_partition_write(" in probe,
                 where, "probe must also exercise the esp_partition_write() path")
        _require(failures, "0x2000u" in probe,
                 where, "probe must write at app1+0x2000, never at the image header")
        _require(failures, "4080u" in probe,
                 where, "probe must include the 4080-byte (16-page) OTA block shape")
        _require(failures, "OTA_WB_PAGE" in probe,
                 where, "probe must include the single-page (256-byte) group")
        _require(failures, "ESP_PARTITION_SUBTYPE_DATA_SPIFFS" in probe,
                 where, "probe must repeat the groups on the storage partition")
        for token in ('"page_crossing"', '"app1_only"', '"flash_write_dead"', '"partition_vs_chip"'):
            _require(failures, token in probe, where, "probe verdict %s is missing" % token)
    return failures


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_SOURCE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError as exc:
        print("FAIL: cannot read %s: %s" % (path, exc))
        return 1

    failures = check_source(source)
    if failures:
        for msg in failures:
            print("FAIL: " + msg)
        return 1
    print("OK: OTA writes are read back per 4 KB block, repaired page-wise, and probed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
