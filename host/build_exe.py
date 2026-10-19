#!/usr/bin/env python3
"""Build the BindDeckLite host executable with PyInstaller.

Packages host/app.py (Flask + bleak BLE link) into a standalone executable,
bundling the Jinja templates, static assets and the BLE link module. Run on a
Windows runner (see .github/workflows/build-host.yml) to get a real Windows
.exe; on other platforms it produces a native binary useful for smoke tests.

The host imports bleak lazily, but PyInstaller only sees static imports, so the
WinRT backend is added as a hidden import - without it the frozen app raises
ModuleNotFoundError on Windows at scan time.

Usage:
    python host/build_exe.py [--onefile | --onedir] [--name NAME]
                             [--dist DIR] [--workpath DIR] [--no-clean]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))   # .../host
ROOT = os.path.dirname(HERE)


def _have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


def ensure_pyinstaller() -> None:
    """Install PyInstaller on demand so the script works in a fresh CI env."""
    if _have("PyInstaller"):
        return
    print("==> installing PyInstaller")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"],
        check=True,
    )


def build(onefile: bool, name: str, dist: str, workpath: str, clean: bool) -> str:
    """Run PyInstaller and return the path to the produced executable."""
    ensure_pyinstaller()
    os.makedirs(dist, exist_ok=True)
    os.makedirs(workpath, exist_ok=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", name,
        "--distpath", dist,
        "--workpath", workpath,
        # app assets: templates + static, relative to host/ (cwd below)
        "--add-data", f"templates{os.pathsep}templates",
        "--add-data", f"static{os.pathsep}static",
        # bleak's Windows backend is imported dynamically at runtime
        "--hidden-import", "bleak.backends.winrt",
        # sibling module of app.py on the import path
        "--hidden-import", "ble_link",
        "app.py",
    ]
    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")
    if clean:
        cmd.append("--clean")

    print("==> " + " ".join(cmd))
    subprocess.run(cmd, cwd=HERE, check=True)

    exe_name = f"{name}.exe" if sys.platform == "win32" else name
    if onefile:
        return os.path.join(dist, exe_name)
    return os.path.join(dist, name, exe_name)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build the BindDeckLite host executable with PyInstaller."
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--onefile", action="store_true",
                      help="single-file executable (default)")
    mode.add_argument("--onedir", action="store_true",
                      help="executable + sibling data directory")
    ap.add_argument("--name", default="BindDeckLite",
                    help="executable / bundle name (default: BindDeckLite)")
    ap.add_argument("--dist", default=os.path.join(HERE, "dist"),
                    help="output directory (default: host/dist)")
    ap.add_argument("--workpath", default=os.path.join(HERE, "build"),
                    help="PyInstaller work directory (default: host/build)")
    ap.add_argument("--no-clean", action="store_true",
                    help="keep previous build cache")
    args = ap.parse_args()

    onefile = not args.onedir  # default to one-file
    exe = build(
        onefile=onefile,
        name=args.name,
        dist=os.path.abspath(args.dist),
        workpath=os.path.abspath(args.workpath),
        clean=not args.no_clean,
    )
    print(f"\nDone. executable: {exe}")


if __name__ == "__main__":
    main()