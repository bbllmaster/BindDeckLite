#!/usr/bin/env python3
"""Guard WS-377: the 16 MB C3 app must use the same flash mode as its bootloader.

Root cause of the long-standing ``Update.end()`` -> ``read_error(3)`` on 16 MB
C3 boards (WS-377):

    [env:esp32c3_16mb] never pinned ``board_build.flash_mode``, so the app
    inherited the pioarduino default (QIO) while the dedicated 16 MB
    bootloader in ``bl_idf/`` runs DIO (``CONFIG_ESPTOOLPY_FLASHMODE_DIO=y``).

With that mismatch the runtime flash *read* path is wrong: ``esp_flash_read()``
/ ``esp_partition_read()`` return 0xFF (or stale-looking bytes) for **every**
address -- including the bootloader at 0x0 and the running app's own header.
Writes land perfectly; the read-back verification is what lies.  Symptoms
that all traced back to this one line:

  * ``Update.end()`` -> ``read_error(3)`` (the slot header reads back as 0xFF);
  * the ``[ota][wt]`` probe -> ``verdict=flash_write_dead`` for app1 *and*
    storage at 1/256/4080 bytes, raw ``esp_flash_write`` included;
  * NVS silently never persisted (``CFG:ENC:3`` applied, ACKed, gone after
    reboot), which also reset the BLE identity on every boot.

Confirmed on hardware: with ``board_build.flash_mode = dio`` the driver reports
``read_mode=3`` instead of 5, every read-back matches, NVS persists, and a full
1.35 MB ``Update`` run completes with ``end(true) == true``.

This checker fails on the pre-fix tree and on any later change that drops the
pin or lets the app and bootloader drift apart again.  It reads the production
configuration files, not a copy of them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ENV = "esp32c3_16mb"
#: Every 16 MB board runs the bl_idf bootloader, so these are the values the
#: app must match.  Keep in sync with bl_idf/sdkconfig.defaults.
BOOTLOADER = {
    "mode": "dio",
    "freq": "40m",
    "size": "16mb",
}


def env_block(ini: str, name: str) -> str | None:
    m = re.search(rf"^\[env:{re.escape(name)}\]\s*$(.*?)(?=^\[|\Z)", ini, re.S | re.M)
    return m.group(1) if m else None


def kv(block: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}\s*=\s*(\S+)", block, re.M)
    return m.group(1) if m else None


def freq_to_token(value: str) -> str:
    """``40000000L`` -> ``40m`` so platformio.ini and sdkconfig can be compared."""
    m = re.match(r"^(\d+)", value.strip())
    if not m:
        return value.strip().lower()
    hz = int(m.group(1))
    if hz % 1_000_000 == 0:
        return f"{hz // 1_000_000}m"
    return f"{hz}hz"


def check(root: Path = ROOT) -> list[str]:
    problems: list[str] = []

    ini_path = root / "platformio.ini"
    boot_path = root / "bl_idf" / "sdkconfig.defaults"
    if not ini_path.is_file():
        return [f"missing {ini_path}"]
    if not boot_path.is_file():
        return [f"missing {boot_path}"]

    ini = ini_path.read_text(encoding="utf-8")
    boot = boot_path.read_text(encoding="utf-8")

    # 1) the bootloader is the reference, and it must stay DIO/40M/16MB
    if "CONFIG_ESPTOOLPY_FLASHMODE_DIO=y" not in boot:
        problems.append("bl_idf: bootloader is no longer DIO (CONFIG_ESPTOOLPY_FLASHMODE_DIO)")
    if "CONFIG_ESPTOOLPY_FLASHFREQ_40M=y" not in boot:
        problems.append("bl_idf: bootloader is no longer 40 MHz (CONFIG_ESPTOOLPY_FLASHFREQ_40M)")
    if "CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y" not in boot:
        problems.append("bl_idf: bootloader is no longer 16 MB (CONFIG_ESPTOOLPY_FLASHSIZE_16MB)")

    # 2) the app env must match it explicitly
    block = env_block(ini, ENV)
    if block is None:
        problems.append(f"platformio.ini: no [env:{ENV}] section")
        return problems

    mode = kv(block, "board_build.flash_mode")
    if mode is None:
        problems.append(
            f"[env:{ENV}] sets no board_build.flash_mode: the app falls back to "
            "the platform default (QIO), which does not match the DIO bootloader "
            "and breaks the runtime flash read path (WS-377)"
        )
    elif mode.strip().lower() != BOOTLOADER["mode"]:
        problems.append(
            f"[env:{ENV}] board_build.flash_mode = {mode!r}, bootloader is "
            f"{BOOTLOADER['mode']!r}"
        )

    raw_freq = kv(block, "board_build.f_flash")
    if raw_freq is None:
        problems.append(f"[env:{ENV}] sets no board_build.f_flash")
    else:
        got = freq_to_token(raw_freq)
        if got != BOOTLOADER["freq"]:
            problems.append(
                f"[env:{ENV}] board_build.f_flash = {raw_freq!r} ({got}), "
                f"bootloader is {BOOTLOADER['freq']}"
            )

    raw_size = kv(block, "board_upload.flash_size")
    if raw_size is None:
        problems.append(f"[env:{ENV}] sets no board_upload.flash_size")
    elif raw_size.strip().lower() != BOOTLOADER["size"]:
        problems.append(
            f"[env:{ENV}] board_upload.flash_size = {raw_size!r}, bootloader is "
            f"{BOOTLOADER['size']}"
        )

    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else ROOT
    problems = check(root)
    if problems:
        print("FAIL: 16 MB C3 app / bootloader flash configuration mismatch")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK: [env:{ENV}] matches the bl_idf bootloader (dio/40m/16MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
