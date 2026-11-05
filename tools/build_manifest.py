#!/usr/bin/env python3
"""Build the release manifest ``update.json`` (WS-523).

The manifest is the contract between the release pipeline and the host app:
the app reads it to learn which exe / firmware to download and what their
SHA256 checksums are.

Every ``size`` and ``sha256`` in the manifest is computed from the **actual
published files** — nothing is hand-written. The script is given the
directory holding the assets that will be attached to the Release and reads
them from disk.

Usage::

    python tools/build_manifest.py --assets artifacts \
        --app-version 1.5.0 --out artifacts/update.json \
        [--notes-file RELEASE_NOTES.md] [--published-at 2026-11-04T00:00:00Z]

The firmware keys follow the host's device-variant mapping:

===================  ==================================
manifest key         release asset
===================  ==================================
``esp32dev``         ``ota_binddecklite_esp32_v…bin``
``esp32c3``          ``ota_binddecklite_c3_v…bin``
``esp32c3_16mb``     ``ota_binddecklite_c3_16mb_v…bin``
===================  ==================================
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

#: manifest 的 firmware key → 发布资产名里的 target 片段。
#: 必须与 host/online_update.py 的 FIRMWARE_TARGET_OF_VARIANT 一致。
FIRMWARE_TARGETS = {
    "esp32dev": "esp32",
    "esp32c3": "c3",
    "esp32c3_16mb": "c3_16mb",
}

#: 上位机 exe 的资产名（与 host/app.spec 的 EXE(name=...) 一致）。
APP_ASSET_NAME = "BindDeckLite.exe"

#: 允许自动更新的最低起始版本（低于它的版本必须先手动升级到中间版本）。
#:
#: 默认 "0.0.0"：现有 exe 一直没有版本号（读出来是 0.0.0+dev），设成更高
#: 会让**所有**老用户永远收不到更新。以后某个版本真需要「先升到 X 再升到 Y」
#: 时，把这里改成 X 即可 —— 上位机会按 min_from 拦住版本过低的用户。
MIN_FROM = "0.0.0"


def sha256_of(path: str) -> str:
    """SHA256 of a file (hex, lowercase)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 256)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def find_firmware_asset(assets: list, target: str):
    """Find the ``ota_…_<target>_v<version>.bin`` asset for one target.

    Returns ``(name, path, version)`` or ``None``.
    """
    pattern = re.compile(r"^ota_binddecklite_%s_v([0-9][0-9A-Za-z.\-]*)\.bin$"
                         % re.escape(target))
    for path in assets:
        name = os.path.basename(path)
        match = pattern.match(name)
        if match:
            return name, path, match.group(1)
    return None


def build_manifest(assets_dir: str, app_version: str, notes: str = "",
                   published_at: str = "") -> dict:
    """Build the manifest dict from the files in *assets_dir*."""
    if not os.path.isdir(assets_dir):
        raise SystemExit("assets directory not found: %s" % assets_dir)
    files = sorted(os.path.join(assets_dir, name)
                   for name in os.listdir(assets_dir)
                   if os.path.isfile(os.path.join(assets_dir, name)))

    app_path = None
    for path in files:
        if os.path.basename(path) == APP_ASSET_NAME:
            app_path = path
            break
    if app_path is None:
        raise SystemExit("missing host executable in %s: %s"
                         % (assets_dir, APP_ASSET_NAME))

    manifest = {
        "app": {
            "version": app_version,
            "asset": APP_ASSET_NAME,
            "sha256": sha256_of(app_path),
            "size": os.path.getsize(app_path),
            "min_from": MIN_FROM,
        },
        "firmware": {},
    }

    for variant, target in FIRMWARE_TARGETS.items():
        found = find_firmware_asset(files, target)
        if not found:
            raise SystemExit("missing ota firmware for target %s in %s"
                             % (target, assets_dir))
        name, path, version = found
        manifest["firmware"][variant] = {
            "version": version,
            "asset": name,
            "sha256": sha256_of(path),
            "size": os.path.getsize(path),
        }

    manifest["notes"] = notes
    manifest["published_at"] = published_at
    return manifest


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Build the BindDeckLite release manifest (update.json).")
    parser.add_argument("--assets", required=True,
                        help="directory holding the files to publish")
    parser.add_argument("--app-version", required=True,
                        help="the app version this release ships")
    parser.add_argument("--out", required=True, help="where to write update.json")
    parser.add_argument("--notes-file", default="",
                        help="file whose content becomes update.json.notes")
    parser.add_argument("--published-at", default="",
                        help="ISO-8601 publication timestamp")
    return parser.parse_args(argv)


def main():
    args = _parse_args(sys.argv[1:])
    notes = ""
    if args.notes_file:
        if not os.path.isfile(args.notes_file):
            raise SystemExit("notes file not found: %s" % args.notes_file)
        with open(args.notes_file, "r", encoding="utf-8") as handle:
            notes = handle.read()
    manifest = build_manifest(args.assets, args.app_version, notes,
                               args.published_at)
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print("==> wrote %s" % args.out)
    print("    app: %s (%s bytes)"
          % (manifest["app"]["version"], manifest["app"]["size"]))
    for variant, item in sorted(manifest["firmware"].items()):
        print("    %s: %s (%s bytes)" % (variant, item["version"], item["size"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
