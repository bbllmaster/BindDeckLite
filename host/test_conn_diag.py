"""Unit tests for the BLE connection diagnostics (WS-395).

``scan_reason`` and ``conn_reason`` are the pure functions that turn a raw
scan / connect result into the one-line Chinese conclusion the user reads in
the top bar, the sidebar and the log page.  They are imported from the
production module on purpose -- a copy of the classification logic in this
file would keep passing after the production code was broken.

No bleak, no GUI, no network::

    cd host && python -m pytest test_conn_diag.py -q
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ble_link import conn_reason, scan_reason

try:  # the real exception when bleak is installed ...
    from bleak.exc import BleakDeviceNotFoundError
except Exception:  # ... otherwise a stand-in with the same class name, which
    # is what the production code matches on (it must import without bleak).
    class BleakDeviceNotFoundError(Exception):
        """Stand-in for bleak's exception (same class name -> same mapping)."""


# --- scan_reason ----------------------------------------------------------
def test_scan_reason_reports_the_error():
    """A scan that raised -> the user gets the error, not a device count."""
    assert scan_reason(0, 0, "boom") == "扫描出错：boom"


def test_scan_reason_maps_permission_errors():
    """No adapter permission is a distinct, actionable cause."""
    for text in ("not permitted", "unauthorized", "access is denied"):
        assert scan_reason(0, 0, text) == "扫描失败：系统未授予蓝牙权限"


def test_scan_reason_prefers_the_error_over_a_device_count():
    """A failed scan is reported even when an earlier scan saw devices."""
    assert scan_reason(4, 1, "boom") == "扫描出错：boom"


def test_scan_reason_is_silent_when_devices_were_found():
    """Devices are available -> no diagnostic line is needed."""
    assert scan_reason(4, 1, None) is None


def test_scan_reason_when_no_ble_device_was_seen():
    """Nothing on the air at all -> the adapter is off / not authorised."""
    assert scan_reason(0, 0, None) == \
        "扫描未发现任何 BLE 设备（系统蓝牙已关闭 / 未授权 / 设备未广播）"


def test_scan_reason_when_other_ble_devices_were_seen():
    """BLE works, but no BindDeckLite is among the devices."""
    assert scan_reason(3, 0, None) == "看到 3 个 BLE 设备，没有 BindDeckLite"


# --- conn_reason ----------------------------------------------------------
def test_conn_reason_explains_a_pairing_failure():
    """An encryption/authentication error means: drop the old pairing."""
    assert conn_reason(Exception("InsufficientEncryption")) == \
        "配对失败：请在系统蓝牙设置里删除该设备的旧配对后重试"


def test_conn_reason_explains_a_missing_device():
    """The deck is not on the air -> it is off or it changed address."""
    assert conn_reason(BleakDeviceNotFoundError("not found")) == \
        "设备不在广播范围（可能已断电或换了地址）"


def test_conn_reason_falls_back_to_the_exception_text():
    """Any other error keeps the exception name and message."""
    err = RuntimeError("boom")
    assert conn_reason(err) == "RuntimeError：boom"


def test_conn_reason_truncates_long_errors():
    """A long error must stay one line; the full text goes into the title."""
    reason = conn_reason(RuntimeError("x" * 500))
    assert reason.startswith("RuntimeError：")
    assert len(reason) == len("RuntimeError：") + 120


# --- import without bleak --------------------------------------------------
def test_module_imports_without_bleak():
    """ble_link must import (and diagnose) even when bleak is not installed.

    The packaged exe ships bleak, but the diagnostics are also used by the
    unit tests and must not depend on it.
    """
    code = (
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'bleak' or name.startswith('bleak.'):\n"
        "            raise ImportError('bleak is not installed')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "from ble_link import conn_reason, scan_reason\n"
        "assert scan_reason(0, 0, None)\n"
        "assert conn_reason(RuntimeError('boom'))\n"
    )
    res = subprocess.run([sys.executable, "-c", code],
                         cwd=os.path.dirname(os.path.abspath(__file__)),
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
