"""WS-432: executable tests for the NVS boot policy.

Why this file exists
--------------------
``ensureNvs()`` used to erase the whole NVS partition on ANY non-``ESP_OK``
result from ``nvs_flash_init()``. NVS holds the BLE IRK (the deck's identity),
the host binding, peer bonds, the key pins, the sleep config, the timezone and
the WiFi credentials, so a wipe there is what forces the user to delete the
device in Windows and pair again.

Two facts about the real toolchain (ESP-IDF 5.5.5, checked, not assumed) say
the fix cannot be "let ALREADY_OPEN through":

  * A second ``nvs_flash_init()`` on an already-mounted partition is
    idempotent and returns ``ESP_OK``
    (``NVSPartitionManager::init_partition`` -> ``lookup_storage_from_name``
    hit -> ``return ESP_OK``).
  * ``ESP_ERR_NVS_ALREADY_OPEN`` does not exist in ESP-IDF; code written
    against it does not compile.

So the guarantee encoded here is the one that actually protects the user: the
partition may only be erased for the two codes an erase can fix
(``NO_FREE_PAGES`` / ``NEW_VERSION_FOUND``, which the Arduino core erases for
too). Every other failure is reported and the data is kept.

The decision is a pure function over an ``esp_err_t``, so it lives in
``src/nvs_boot_policy.h``. The firmware includes that header; this test
compiles the **same** header with g++ (through ``host/nvs_boot_cli.cpp``, which
contains no policy logic) and drives it. The assertions therefore run against
production code, not against a copy of it.

The test fails on the pre-WS-432 tree (the header and the CLI do not exist, so
the compile step fails, and ``ensureNvs()`` erases on every error).

Run:  pytest host/test_nvs_boot_policy.py -v
"""

import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(REPO_ROOT, "src")
POLICY_HEADER = os.path.join(SRC_DIR, "nvs_boot_policy.h")
CLI_SOURCE = os.path.join(REPO_ROOT, "host", "nvs_boot_cli.cpp")
MAIN_CPP = os.path.join(SRC_DIR, "main.cpp")

ESP_OK = "0x0000"
ESP_ERR_NVS_NO_FREE_PAGES = "0x110D"
ESP_ERR_NVS_NEW_VERSION_FOUND = "0x1110"

# Failures an erase cannot fix. 0x1109 is included on purpose: it is the value
# the WS-432 report assumed for "already open", and it must not wipe NVS either
# even if some future IDF starts returning it.
NON_ERASABLE = [
    "0x1101",  # ESP_ERR_NVS_NOT_INITIALIZED
    "0x1102",  # ESP_ERR_NVS_NOT_FOUND
    "0x1109",  # (not an IDF code) hypothetical "already open"
    "0x110B",  # ESP_ERR_NVS_INVALID_STATE
    "0x110F",  # ESP_ERR_NVS_PART_NOT_FOUND (bad partition table)
    "0x0103",  # ESP_ERR_INVALID_STATE
    "0x0101",  # ESP_ERR_NO_MEM
    "-1",      # ESP_FAIL
    "0x7777",  # unknown / future code
]


def _compiler():
    for name in ("g++", "c++", "clang++"):
        path = shutil.which(name)
        if path:
            return path
    return None


@pytest.fixture(scope="module")
def policy(tmp_path_factory):
    """Compile the production policy header into a tiny CLI."""
    if not os.path.exists(POLICY_HEADER):
        pytest.fail("missing production header: %s" % POLICY_HEADER)
    if not os.path.exists(CLI_SOURCE):
        pytest.fail("missing policy CLI: %s" % CLI_SOURCE)
    cxx = _compiler()
    if not cxx:
        pytest.skip("no C++ compiler (g++/c++/clang++) available")

    out = str(tmp_path_factory.mktemp("nvsboot") / "nvs_boot_cli")
    cmd = [
        cxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O0",
        "-I", SRC_DIR,
        CLI_SOURCE,
        "-o", out,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, (
        "compiling the production policy failed:\n%s\n%s" % (cmd, proc.stderr)
    )
    return out


def _run(binary, lines):
    proc = subprocess.run(
        [binary], input="\n".join(lines) + "\n", capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _verdict(binary, err):
    line = _run(binary, ["init " + err])[0]
    return dict(part.split("=", 1) for part in line.split())


def test_mounted_partition_keeps_data(policy):
    """ESP_OK is what a second nvs_flash_init() returns - data is live."""
    assert _verdict(policy, ESP_OK) == {"verdict": "usable", "may_erase": "0"}


@pytest.mark.parametrize("err", [ESP_ERR_NVS_NO_FREE_PAGES, ESP_ERR_NVS_NEW_VERSION_FOUND])
def test_corrupt_partition_is_erasable(policy, err):
    """The two codes an erase actually fixes stay erasable."""
    assert _verdict(policy, err) == {"verdict": "erase", "may_erase": "1"}


@pytest.mark.parametrize("err", NON_ERASABLE)
def test_unfixable_errors_never_wipe_nvs(policy, err):
    """WS-432 core guarantee: only corruption may wipe the deck's identity."""
    v = _verdict(policy, err)
    assert v["may_erase"] == "0", (
        "%s would erase NVS; an erase cannot fix it and costs the user a full "
        "re-pair (IRK, bonds, pins, WiFi)" % err
    )
    assert v["verdict"] == "unusable"


def test_firmware_calls_the_shared_policy():
    """The policy is worthless if ensureNvs() keeps its own private copy."""
    with open(MAIN_CPP, "r", encoding="utf-8") as fh:
        source = fh.read()
    assert '#include "nvs_boot_policy.h"' in source, (
        "src/main.cpp must include the shared NVS boot policy"
    )

    body = _ensure_nvs_body(source)
    assert "nvs_flash_erase()" in body, "ensureNvs() lost its repair path"
    erase_at = body.index("nvs_flash_erase()")
    guard_at = body.find("nvsboot::classifyInit(")
    assert guard_at != -1, (
        "ensureNvs() must classify nvs_flash_init() through the shared policy"
    )
    assert guard_at < erase_at, (
        "the policy verdict must be consulted before the partition is erased"
    )
    assert "VERDICT_ERASE" in body[:erase_at], (
        "nvs_flash_erase() must sit in the VERDICT_ERASE branch only"
    )
    assert source.count("nvs_flash_erase()") == 1, (
        "nvs_flash_erase() must exist exactly once in the firmware, inside the "
        "guarded ensureNvs() repair path"
    )


def _ensure_nvs_body(source):
    start = source.index("static void ensureNvs()")
    depth = 0
    for i in range(source.index("{", start), len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError("unbalanced braces in ensureNvs()")
