"""Regression guards for the WS-433 OTA write path (read-back + repair + probe).

Why this file exists
--------------------
On the 16 MB C3 the OTA slot takes every byte of a 1.35 MB image with
``ESP_OK`` and reads back 0xFF, so ``Update.end()`` dies with
``read_error(3)``.  WS-377/WS-378/WS-420 proved that the bytes are absent and
narrowed it to "the write is accepted, the chip does not keep it", but every
channel still simply forwarded the return code.  WS-433 makes the write path
prove itself:

  * each 4 KB block handed to Update is read back through
    ``esp_partition_read`` (SPI, not the MMU/cache);
  * a block that does not match is erased and reprogrammed page by page
    (``chip_drv->page_size``), then read back again;
  * only a second mismatch aborts, with ``write_not_verified``;
  * the trailing partial block is erased before it is written, because Update
    only erases at 64 KB boundaries and would otherwise program the last
    sector straight over the previous image;
  * ``[ota][wt]`` writes 1 / 256 / 4080 bytes to app1+0x2000 and to the
    ``storage`` tail and prints the raw ``esp_flash_write()`` code, so one
    flash says whether the loss is page crossing, app1-only or flash-wide.

The firmware is C++ for ESP32/ESP32-C3 and CI has no hardware and no esp32
toolchain, so - like ``host/test_ota_flash_guard.py`` (WS-420) - these
assertions read the production source ``src/main.cpp`` instead of executing
it.  ``tools/check_ota_write_verify.py`` is the checker; the mutation tests
below prove it actually bites, i.e. that a future edit which drops the
read-back, the repair or the tail erase fails here instead of shipping.

Run:  pytest host/test_ota_write_verify.py -v
"""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_CPP = os.path.join(REPO_ROOT, "src", "main.cpp")
TOOLS = os.path.join(REPO_ROOT, "tools")

sys.path.insert(0, TOOLS)

import check_ota_write_verify as checker  # noqa: E402  (path set up above)


def _source():
    with open(MAIN_CPP, "r", encoding="utf-8") as fh:
        return fh.read()


def test_production_source_passes():
    failures = checker.check_source(_source())
    assert failures == [], "WS-433 write path regressed:\n" + "\n".join(failures)


def test_checker_rejects_a_channel_that_bypasses_the_verified_writer():
    src = _source().replace(
        "const char* wErr = otaWrite(data, len);",
        "const char* wErr = nullptr; Update.write(const_cast<uint8_t*>(data), len);",
        1,
    )
    failures = checker.check_source(src)
    assert any("Update.write() directly" in f for f in failures), failures


def test_checker_rejects_a_missing_read_back():
    # Drop the comparison entirely: the block is written and assumed correct.
    src = _source().replace(
        "OtaWbVerdict v = otaWbCompare(part, base + (uint32_t)skip, want + skip,\n"
        "                                OTA_WB_BLOCK - skip);",
        "OtaWbVerdict v = { ESP_OK, 0, 0, 0, 0 };",
        1,
    )
    assert src != _source(), "the first read-back line moved; update this test"
    failures = checker.check_source(src)
    assert any("read-back" in f for f in failures), failures


def test_checker_rejects_a_missing_repair():
    src = _source().replace('  return "write_not_verified";', '  return "flash_not_written";', 1)
    failures = checker.check_source(src)
    assert any("write_not_verified" in f for f in failures), failures


def test_checker_rejects_page_unaligned_repair():
    src = _source().replace(
        "size_t n = page - (abs % page);",
        "size_t n = len - done;",
        1,
    )
    assert src != _source()
    failures = checker.check_source(src)
    assert any("page aligned" in f for f in failures), failures


def test_checker_rejects_a_missing_tail_erase():
    # Update only erases at 64 KB boundaries; without this the last sector is
    # programmed over stale bytes and the image checksum in end() fails.
    src = _source().replace(
        "esp_err_t eTail = esp_partition_erase_range(g_otaPart, base, otaWbSectorSize());",
        "esp_err_t eTail = ESP_OK;",
        1,
    )
    assert src != _source()
    failures = checker.check_source(src)
    assert any("tail block is never erased" in f for f in failures), failures


def test_checker_rejects_a_missing_probe():
    src = _source().replace("#define OTA_WT_OFF  0x2000u", "#define OTA_WT_OFF  0x2000", 1)
    src = src.replace("#define OTA_WT_RAW  4080u", "#define OTA_WT_RAW  4080", 1)
    failures = checker.check_source(src)
    assert any("0x2000" in f or "4080-byte" in f for f in failures), failures


def test_checker_rejects_a_probe_without_the_raw_write():
    src = _source().replace(
        "esp_err_t eRaw = esp_flash_write(esp_flash_default_chip, tx,",
        "esp_err_t eRaw = ESP_OK; (void)tx; //",
        1,
    )
    assert src != _source()
    failures = checker.check_source(src)
    assert any("raw esp_flash_write()" in f for f in failures), failures


@pytest.mark.parametrize("path", [MAIN_CPP])
def test_cli_exits_zero_on_production_source(path):
    assert checker.main([path]) == 0
