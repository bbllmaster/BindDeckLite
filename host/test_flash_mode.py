"""Regression guard for WS-377: app/bootloader flash-mode mismatch on 16 MB C3.

The bug had no failing unit test because it lived in a build configuration
line that was simply absent.  What is testable is the invariant that made the
bug possible:

    the [env:esp32c3_16mb] app must be built for the same flash mode, clock
    and size as the bl_idf bootloader it is bundled with.

``tools/check_flash_mode.py`` reads the real ``platformio.ini`` and
``bl_idf/sdkconfig.defaults`` and asserts that.  The tests below run the
checker against the production tree (must pass) and against mutated copies
that reintroduce the original defect or a drift variant (must fail), so the
checker cannot silently become a no-op.

Chained from WS-433's lesson: a test that copies production logic instead of
reading it passes happily while production is broken.  These tests only read
files, they never re-implement the rules.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "check_flash_mode.py"


def run_checker(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), str(root)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )


def copy_config(root: Path) -> Path:
    """Copy just the two files the checker reads into a scratch tree."""
    (root / "bl_idf").mkdir(parents=True)
    shutil.copy(REPO / "platformio.ini", root / "platformio.ini")
    shutil.copy(REPO / "bl_idf" / "sdkconfig.defaults", root / "bl_idf" / "sdkconfig.defaults")
    return root


def test_production_tree_matches_bootloader():
    """The shipped configuration must pass -- this is the fix itself."""
    res = run_checker(REPO)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "OK" in res.stdout


def test_missing_flash_mode_fails(tmp_path):
    """The original defect: no board_build.flash_mode in the 16 MB env."""
    root = copy_config(tmp_path)
    ini = (root / "platformio.ini").read_text(encoding="utf-8")
    mutated = "\n".join(
        line for line in ini.splitlines() if line.strip() != "board_build.flash_mode = dio"
    )
    (root / "platformio.ini").write_text(mutated + "\n", encoding="utf-8")

    res = run_checker(root)
    assert res.returncode == 1, res.stdout
    assert "board_build.flash_mode" in res.stdout


def test_wrong_flash_mode_fails(tmp_path):
    """Drift the other way: the app pinned to QIO while bl_idf stays DIO."""
    root = copy_config(tmp_path)
    ini = (root / "platformio.ini").read_text(encoding="utf-8")
    mutated = ini.replace("board_build.flash_mode = dio", "board_build.flash_mode = qio")
    (root / "platformio.ini").write_text(mutated, encoding="utf-8")

    res = run_checker(root)
    assert res.returncode == 1, res.stdout
    assert "qio" in res.stdout.lower()


def test_bootloader_mode_change_is_caught(tmp_path):
    """If bl_idf moves to QIO the app pin must be revisited, not silently kept."""
    root = copy_config(tmp_path)
    cfg = root / "bl_idf" / "sdkconfig.defaults"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "CONFIG_ESPTOOLPY_FLASHMODE_DIO=y", "CONFIG_ESPTOOLPY_FLASHMODE_QIO=y"
        ),
        encoding="utf-8",
    )

    res = run_checker(root)
    assert res.returncode == 1, res.stdout
    assert "DIO" in res.stdout


def test_frequency_drift_fails(tmp_path):
    """20 MHz was tried on hardware and is not the fix; drift must be rejected."""
    root = copy_config(tmp_path)
    ini = (root / "platformio.ini").read_text(encoding="utf-8")
    mutated = ini.replace(
        "board_build.f_flash = 40000000L", "board_build.f_flash = 20000000L", 1
    )
    (root / "platformio.ini").write_text(mutated, encoding="utf-8")

    res = run_checker(root)
    assert res.returncode == 1, res.stdout
    assert "f_flash" in res.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
