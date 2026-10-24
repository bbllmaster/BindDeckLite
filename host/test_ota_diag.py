"""Regression guards for the WS-378 OTA read-back diagnostic.

Why this file exists
--------------------
WS-378 round 4 shipped two things the WS-379 review rejected:

  1. a *core symbol override* — ``src/flash_size_fix.cpp`` redefined
     ``EspClass::getFlashChipSize()`` and ``platformio.ini`` linked with
     ``-Wl,--allow-multiple-definition`` so the duplicate definition won.
     The upstream formula ``2 << (id - 1)`` (= ``2**id``) is already
     **correct** (JEDEC capacity code 0x15=2MB / 0x16=4MB / 0x17=8MB /
     0x18=16MB), so the override only doubled the reported capacity on every
     board — a regression, not a fix.
  2. a diagnostic that iterated *every* OTA slot and judged the result with
     ``(byte & 0xFF) == 0xE9 ? "write ok, read mismatch?" : "write failed?"``.
     That heuristic cannot tell the three real outcomes apart and always
     printed a misleading line for the running slot.

What is guarded here
--------------------
  * the rollback stays rolled back (no override file, no link flag, no
    ``getFlashChipSize`` redefinition anywhere in ``src/``);
  * the device-side diagnostic probes **only** the OTA target partition
    (``esp_ota_get_next_update_partition(NULL)`` — the very call
    ``Update.begin()`` makes) and never iterates the OTA slot list;
  * it can distinguish ``write_failed`` / ``read_failed`` / ``read_mismatch``
    by using the return value of ``ESP.partitionWrite()`` (the Arduino
    Update library drops ``_enablePartition()``'s return value) and by
    printing the first byte read back;
  * the ``ACK:OTA:ERROR:end_failed:<detail>(<code>)`` wire format and the
    error-code mapping are untouched.

These are *source contract* tests: the firmware is C++ for ESP32/ESP32-C3 and
there is no hardware in CI, so the assertions read the production sources
instead of executing them.  They fail on the pre-fix tree (commit ``b44d9b76``)
and pass on the fixed tree.

Run:  pytest host/test_ota_diag.py -v
"""

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_CPP = os.path.join(REPO_ROOT, "src", "main.cpp")
PLATFORMIO_INI = os.path.join(REPO_ROOT, "platformio.ini")
FLASH_SIZE_FIX = os.path.join(REPO_ROOT, "src", "flash_size_fix.cpp")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _skip_literal_and_comments(source, i):
    """Advance *i* past a line comment, a block comment or a string/char
    literal. Returns the new index (or the same index when nothing matched).
    """
    n = len(source)
    if source.startswith("//", i):
        j = source.find("\n", i)
        return n if j < 0 else j
    if source.startswith("/*", i):
        j = source.find("*/", i + 2)
        return n if j < 0 else j + 2
    if source[i] in ('"', "'"):
        quote = source[i]
        i += 1
        while i < n:
            if source[i] == "\\":
                i += 2
                continue
            if source[i] == quote:
                return i + 1
            i += 1
        return n
    return i


def _strip_comments(source):
    """Return *source* with comments blanked out (string literals kept).

    Used so a test can assert on *code* without tripping over the
    explanatory comments.
    """
    out = []
    i = 0
    n = len(source)
    while i < n:
        if source.startswith("//", i):
            j = source.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if source.startswith("/*", i):
            j = source.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " * (j - i))
            i = j
            continue
        if source[i] in ('"', "'"):
            quote = source[i]
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == quote:
                    j += 1
                    break
                j += 1
            out.append(source[i:j])
            i = j
            continue
        out.append(source[i])
        i += 1
    return "".join(out)


def _function_body(source, signature_fragment):
    """Return the body of the C++ function *defined* by *signature_fragment*.

    The fragment may also appear as a forward declaration; only the
    occurrence followed by ``{`` is the definition. Braces inside comments
    and string literals are ignored so a log format string containing '{'
    cannot break the match.
    """
    start = 0
    while True:
        idx = source.find(signature_fragment, start)
        if idx < 0:
            raise AssertionError("signature not found: " + signature_fragment)
        # Find the next significant character after the signature.
        i = idx + len(signature_fragment)
        while i < len(source) and source[i].isspace():
            i += 1
        if i < len(source) and source[i] == "{":
            open_brace = i
            break
        start = idx + len(signature_fragment)

    depth = 0
    i = open_brace
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "/" or ch in ('"', "'"):
            nxt = _skip_literal_and_comments(source, i)
            if nxt != i:
                i = nxt
                continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace:i + 1]
        i += 1
    raise AssertionError("unbalanced braces while scanning " + signature_fragment)


# --- 1. the rollback must stay rolled back -------------------------------

def test_flash_size_override_file_is_gone():
    """``src/flash_size_fix.cpp`` must not exist.

    The file overrode a core symbol to 'fix' a formula that was never
    broken; keeping it would double the reported flash size on every board.
    """
    assert not os.path.exists(FLASH_SIZE_FIX), (
        "src/flash_size_fix.cpp must be deleted (WS-378): the upstream "
        "getFlashChipSize() formula 2 << (id - 1) is already correct "
        "(2**id); overriding the core symbol is a regression, not a fix"
    )


def test_platformio_has_no_allow_multiple_definition():
    """No env may link with ``-Wl,--allow-multiple-definition``.

    The flag applies to the whole link and hides genuine duplicate symbols
    across the entire project.
    """
    ini = _read(PLATFORMIO_INI)
    assert "--allow-multiple-definition" not in ini, (
        "platformio.ini must not contain -Wl,--allow-multiple-definition "
        "(WS-378): it shadows the whole link, not just the one symbol"
    )


def test_src_does_not_redefine_get_flash_chip_size():
    """No file under ``src/`` may redefine ``getFlashChipSize()``.

    The Arduino core computes the chip size from the JEDEC id as
    ``2 << (id - 1)``; the firmware only needs to *read* it.
    """
    src_dir = os.path.join(REPO_ROOT, "src")
    offenders = []
    for name in sorted(os.listdir(src_dir)):
        if not name.endswith((".cpp", ".cc", ".c", ".h", ".hpp")):
            continue
        text = _read(os.path.join(src_dir, name))
        if re.search(r"EspClass::getFlashChipSize", text):
            offenders.append(name)
    assert not offenders, (
        "src/ must not redefine EspClass::getFlashChipSize() (WS-378); "
        "offending file(s): " + ", ".join(offenders)
    )


# --- 2. the diagnostic must probe only the OTA target partition -----------

def _diag_body():
    return _function_body(_read(MAIN_CPP), "static void otaDiagReadback(int code)")


def test_diag_uses_only_the_next_update_partition():
    """The probe must target ``esp_ota_get_next_update_partition(NULL)``.

    That is the same partition ``Update.begin()`` selects (Updater.cpp), so
    the probe hits the slot the updater was writing to — and nothing else.
    """
    body = _diag_body()
    assert "esp_ota_get_next_update_partition(NULL)" in body, (
        "the diagnostic must resolve the target with "
        "esp_ota_get_next_update_partition(NULL) (WS-378)"
    )
    assert "esp_partition_find_first" not in body, (
        "the diagnostic must not iterate the OTA slot list (WS-378): the "
        "running slot always reads back 0xE9 and produced a misleading "
        "extra log line"
    )


def test_diag_guards_against_touching_the_running_partition():
    """The probe must refuse to write when the target is the running slot.

    ``esp_ota_get_next_update_partition()`` returns the running partition on
    boards without a second OTA slot; writing there would damage the
    firmware that is currently running.
    """
    body = _diag_body()
    assert "esp_ota_get_running_partition" in body, (
        "the diagnostic must compare the target against "
        "esp_ota_get_running_partition() before writing (WS-378)"
    )
    assert "target == running" in body, (
        "the diagnostic must skip the probe when the target is the running "
        "partition (WS-378)"
    )


# --- 3. the three failure modes must be separable ------------------------

def test_diag_distinguishes_write_read_and_mismatch():
    """``write_failed`` / ``read_failed`` / ``read_mismatch`` must each have
    their own log line, and the byte read back must be printed."""
    body = _diag_body()
    for verdict in ("write_failed", "read_failed", "read_mismatch"):
        assert verdict in body, (
            "the diagnostic must report %r (WS-378): the Arduino Update "
            "library folds a failed write and a failed read-back into the "
            "same UPDATE_ERROR_READ (3)" % verdict
        )
    assert "byte0=0x%02X" in body, (
        "the diagnostic must print the first byte read back (WS-378)"
    )
    assert "0xE9" in body, (
        "the diagnostic must compare the read-back byte against the ESP "
        "image magic 0xE9 (WS-378)"
    )


def test_diag_uses_the_partition_write_return_value():
    """The write verdict must come from ``ESP.partitionWrite()``'s return
    value — the library swallows ``_enablePartition()``'s result."""
    body = _diag_body()
    assert "ESP.partitionWrite(" in body, (
        "the diagnostic must call ESP.partitionWrite() itself (WS-378)"
    )
    assert re.search(r"if\s*\(\s*!\s*ESP\.partitionWrite\(", body), (
        "the write verdict must use the return value of ESP.partitionWrite() "
        "(WS-378): the library drops _enablePartition()'s return value, "
        "which is exactly what hides a dead write channel"
    )
    assert "ESP.partitionRead(" in body, (
        "the diagnostic must read the bytes back with ESP.partitionRead() "
        "(WS-378)"
    )


def test_diag_keeps_the_old_ambiguous_heuristic_out():
    """The rejected heuristic must not come back."""
    body = _diag_body()
    for junk in ("write ok, read mismatch?", "write failed?"):
        assert junk not in body, (
            "the ambiguous heuristic %r must not be used again (WS-378): it "
            "cannot tell case A from case B" % junk
        )


def test_diag_reports_the_target_partition_details():
    """The log must name the target slot and its address/size.

    A read error on a 16 MB board is only meaningful next to the slot
    address (the image crosses the 4 MB boundary at 0x310000 + 0x147A00).
    """
    body = _diag_body()
    assert "target=%s" in body, (
        "the diagnostic must log the target partition label (WS-378)"
    )
    assert "addr=0x%06X" in body and "size=0x%05X" in body, (
        "the diagnostic must log the target address and size (WS-378)"
    )


# --- 4. log-only: the wire protocol and error mapping stay untouched -----

def test_end_failed_protocol_format_is_unchanged():
    """``ACK:OTA:ERROR:end_failed:<detail>(<code>)`` must stay as it is."""
    main = _read(MAIN_CPP)
    assert 'String("end_failed:")' in main, (
        "the end_failed:<detail>(<code>) format must not change (WS-378)"
    )
    assert '"ACK:OTA:ERROR:"' in main, (
        "the OTA error reply prefix must not change (WS-378)"
    )


def test_error_code_mapping_is_unchanged():
    """The numeric Update error codes must keep their detail strings."""
    main = _read(MAIN_CPP)
    mapping = _function_body(main, "otaUpdateErrorText(int code)")
    assert "case UPDATE_ERROR_READ:" in mapping, (
        "UPDATE_ERROR_READ must keep its own detail string (WS-378)"
    )
    assert 'return "read_error"' in mapping, (
        "UPDATE_ERROR_READ must still map to 'read_error' (WS-378)"
    )


def test_diag_is_log_only():
    """The diagnostic must not change the reported error code.

    It returns void, never assigns to the error code and never touches the
    Update object or the host protocol.
    """
    main = _read(MAIN_CPP)
    assert "static void otaDiagReadback(int code)" in main
    code_only = _strip_comments(_diag_body())
    assert "Update." not in code_only, (
        "the diagnostic must not call into Update (WS-378): it is log-only"
    )
    assert "sendToHost" not in code_only, (
        "the diagnostic must not send anything to the host (WS-378): it is "
        "log-only and the ACK:OTA:* protocol must stay unchanged"
    )
    assert not re.search(r"\bcode\s*=[^=]", code_only), (
        "the diagnostic must not modify the error code (WS-378): it is "
        "log-only and the reported code/protocol must stay unchanged"
    )


if __name__ == "__main__":
    sys.exit(0)
