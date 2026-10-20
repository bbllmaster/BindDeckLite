#!/usr/bin/env python3
"""Build BindDeckLite firmware images for release.

Produces, for each target (classic ESP32 + ESP32-C3):
  * merged_<target>_v<ver>.bin  — bootloader + partition table + otadata +
    app, flashed from 0x0 in one shot (esptool write_flash 0x0 file.bin)
  * ota_<target>_v<ver>.bin     — app-only image for the config-mode OTA page

Usage:
  python tools/build_release.py [--skip-build] [--target <name> ...]

  --target may be given several times to build only the chosen targets
  (esp32 | c3 | c3_16mb | c3_2mb); default = all. The flag lets CI build a
  single target without re-running the whole matrix, e.g. after a targeted
  change to one partition table.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
PIO_HOME = os.path.expanduser("~/.platformio")

# Built by bl_idf (ESP-IDF 5.x): the stock Arduino bootloader is compiled for
# 4 MB flash, which the 16 MB C3 module rejects (WS-278/WS-279).
IDF_BOOTLOADER = os.path.join(ROOT, "bl_idf", ".pio", "build", "idf", "bootloader.bin")

TARGETS = {
    "esp32": {
        "env": "esp32dev",
        "chip": "esp32",
        "name": "esp32",
        "flash_freq": "40m",
        "bootloader_offset": "0x1000",
        "boot_app0_offset": "0xd000",   # otadata starts at 0xd000 on this table
    },
    "c3": {
        "env": "esp32c3",
        "chip": "esp32c3",
        "name": "c3",
        "flash_freq": "80m",
        "bootloader_offset": "0x0",
        "boot_app0_offset": "0xe000",
    },
    # 16 MB C3 modules: 3 MB OTA slots, 40 MHz DIO (conservative). Uses the
    # custom IDF 5 bootloader (16 MB flash header + CONFIG_SPI_FLASH_ROM_IMPL).
    "c3_16mb": {
        "env": "esp32c3_16mb",
        "chip": "esp32c3",
        "name": "c3_16mb",
        "flash_freq": "40m",
        "flash_size": "16MB",
        "bootloader_offset": "0x0",
        "boot_app0_offset": "0xe000",
        "bootloader": IDF_BOOTLOADER,
    },
    # 2 MB C3 modules: single app slot, no OTA.
    "c3_2mb": {
        "env": "esp32c3_2mb",
        "chip": "esp32c3",
        "name": "c3_2mb",
        "flash_freq": "80m",
        "flash_size": "2MB",
        "bootloader_offset": "0x0",
        "boot_app0_offset": "0xd000",
    },
}

# ESP image header: byte 3 high nibble = flash size code.
FLASH_SIZE_CODES = {0: "1MB", 1: "2MB", 2: "4MB", 3: "8MB", 4: "16MB",
                    5: "32MB", 6: "64MB", 7: "128MB"}


def fw_version() -> str:
    with open(os.path.join(ROOT, "src", "main.cpp"), encoding="utf-8") as f:
        m = re.search(r'#define FW_VERSION "([^"]+)"', f.read())
    return m.group(1) if m else "0.0.0"


def find_esptool() -> str:
    candidates = [
        os.path.join(PIO_HOME, "packages", "tool-esptoolpy", "esptool.py"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    raise SystemExit("esptool.py not found - run `pio run` once to install it")


def find_boot_app0() -> str:
    base = os.path.join(PIO_HOME, "packages", "framework-arduinoespressif32", "tools", "partitions")
    path = os.path.join(base, "boot_app0.bin")
    if not os.path.isfile(path):
        raise SystemExit("boot_app0.bin not found: " + path)
    return path


def build_idf_bootloader(skip_build: bool) -> str:
    """Return the custom 16 MB bootloader, building bl_idf when needed."""
    if not skip_build:
        print("==> building 16 MB bootloader (bl_idf, ESP-IDF 5.x)")
        subprocess.run(["pio", "run", "-d", "bl_idf", "-e", "idf"], cwd=ROOT, check=True)
    if not os.path.isfile(IDF_BOOTLOADER):
        raise SystemExit(
            "custom bootloader missing: %s\n"
            "run `pio run -d bl_idf -e idf` first (or drop --skip-build)" % IDF_BOOTLOADER
        )
    with open(IDF_BOOTLOADER, "rb") as f:
        hdr = f.read(4)
    size = FLASH_SIZE_CODES.get(hdr[3] >> 4, "unknown")
    if size != "16MB":
        raise SystemExit(
            "%s has a %s flash header - rebuild bl_idf with "
            "CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y" % (IDF_BOOTLOADER, size)
        )
    return IDF_BOOTLOADER


def _parse_targets(argv):
    """Return the subset of TARGETS requested on the command line.

    Every --target <name> adds one; with none given, all targets are built.
    Unknown names abort so a typo never silently builds the wrong image.
    """
    wanted = [a for i, a in enumerate(argv) if i > 0 and argv[i - 1] == "--target"]
    if not wanted:
        return dict(TARGETS)
    unknown = [w for w in wanted if w not in TARGETS]
    if unknown:
        raise SystemExit(
            "unknown --target %s (choose from %s)" % (", ".join(unknown), ", ".join(TARGETS))
        )
    return {w: TARGETS[w] for w in wanted}


def main():
    skip_build = "--skip-build" in sys.argv
    version = fw_version()
    os.makedirs(DIST, exist_ok=True)
    esptool = find_esptool()
    boot_app0 = find_boot_app0()
    # Built once, used by the 16 MB image only.
    build_idf_bootloader(skip_build)

    for target, t in _parse_targets(sys.argv).items():
        build_dir = os.path.join(ROOT, ".pio", "build", t["env"])
        if not skip_build:
            print(f"==> building {t['env']}")
            subprocess.run(["pio", "run", "-e", t["env"]], cwd=ROOT, check=True)

        bootloader = t.get("bootloader") or os.path.join(build_dir, "bootloader.bin")
        partitions = os.path.join(build_dir, "partitions.bin")
        app = os.path.join(build_dir, "firmware.bin")
        for p in (bootloader, partitions, app):
            if not os.path.isfile(p):
                raise SystemExit(f"missing build output: {p}")
        print("    bootloader:", os.path.relpath(bootloader, ROOT))

        merged = os.path.join(DIST, f"merged_binddecklite_{t['name']}_v{version}.bin")
        cmd = [
            sys.executable, esptool,
            "--chip", t["chip"],
            "merge_bin", "-o", merged,
            "--flash_mode", "dio",
            "--flash_freq", t["flash_freq"],
            "--flash_size", t.get("flash_size", "4MB"),
            t["bootloader_offset"], bootloader,
            "0x8000", partitions,
            "0x10000", app,
        ]
        if t["boot_app0_offset"]:
            cmd[-2:-2] = [t["boot_app0_offset"], boot_app0]
        print("==> merging", os.path.basename(merged))
        subprocess.run(cmd, cwd=ROOT, check=True)

        ota = os.path.join(DIST, f"ota_binddecklite_{t['name']}_v{version}.bin")
        shutil.copyfile(app, ota)
        print(f"    {merged}\n    {ota}")

    print(f"\nDone. version={version}, output: {DIST}")


if __name__ == "__main__":
    main()
