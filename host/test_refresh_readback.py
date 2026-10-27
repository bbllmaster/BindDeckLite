"""Unit tests for the device-config read-back (WS-409 / WS-416).

WS-409's complaint was that the host's 「读取设备配置」 button and the
「以设备为准」 button in the apply-results overlay did nothing visible: the
server answered ``{"ok": true}`` right after sending ``CMD:GET_CFG``, the
device's ``CFG_*`` answer was written into the server-side config by the
normal line handler, and the front-end never re-read it — so the form kept
showing the old values.

The fix makes the refresh action *await* the device's answer and returns
the merged configuration.  The aggregation function
``app.collect_cfg_snapshot`` is the production code that decides

* all four tracked groups arrived  -> ``ok=True`` and the stored config is
  rewritten with the device's values (4/4),
* a group is missing                -> ``ok=False`` and the stored config is
  left untouched (an old or silent firmware must not half-update it),
* nothing arrived at all (timeout)  -> ``ok=False`` with the reason
  「设备未回报配置」.

These tests drive that production function directly — they do not re-implement
the parsing — so a change to ``host/app.py`` that breaks any of the three
cases fails this file.

The tests must stay platform-independent and side-effect free: they never
touch the real ``host/config.json`` (the stored config is replaced by an
in-memory one and ``save_config`` is stubbed out).

Run::

    cd host && python -m pytest test_refresh_readback.py -q
"""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


DEV = "t-read"

# The four tracked config groups with the values a device would report.
# The pins differ from the ESP32 defaults on purpose — the test has to see
# the stored config actually change.
FULL_VALUES = {
    "pins": "22,23,21,19,18,17,16,15,14",
    "keys": "240,241,242,243,244,245,246,247,248,249",
    "enc": "2",
    "sleep": "1,5",
}


def _reset_state():
    """Replace the stored config with an in-memory one and stub save_config.

    ``save_config`` is what writes ``host/config.json``; stubbing it keeps
    the tests from touching the user's real configuration file.
    """
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    app.save_config = lambda: None
    app._events.clear()


def _device():
    """Register a device with a known starting configuration."""
    _reset_state()
    cfg = app.new_device_cfg("ESP32", DEV)
    app._config["devices"][DEV] = cfg
    return cfg


def _stored():
    """The stored configuration of the test device."""
    return app._config["devices"][DEV]


# --- the three cases the issue asks for -----------------------------------

def test_all_groups_reported_merges_and_reports_four_of_four():
    """4 CFG_* groups arrive -> ok=True and the stored config is rewritten."""
    cfg = _device()
    original_pins = list(cfg["pins"])
    original_mode = cfg["encoder"]["mode"]

    result = app.collect_cfg_snapshot(DEV, dict(FULL_VALUES))

    assert result["ok"] is True
    assert result["reason"] == ""
    assert result["fields"] == {"pins": True, "keys": True, "enc": True, "sleep": True}
    assert sum(1 for v in result["fields"].values() if v) == 4

    # The returned cfg is the merged configuration the UI should draw.
    assert result["cfg"] is not None
    assert result["cfg"]["pins"] == [22, 23, 21, 19, 18, 17, 16, 15, 14]
    assert result["cfg"]["encoder"]["mode"] == 2
    assert result["cfg"]["sleep"] == {"enabled": True, "minutes": 5}
    assert result["cfg"]["keys"][0]["code"] == 240

    # …and the stored config really was updated (not just the copy).
    assert _stored()["pins"] == [22, 23, 21, 19, 18, 17, 16, 15, 14]
    assert _stored()["encoder"]["mode"] == 2
    assert _stored()["keys"][0]["code"] == 240
    # The device does not own the per-key actions — they must survive.
    assert _stored()["keys"][0]["action"] == cfg["keys"][0]["action"]
    assert original_pins != _stored()["pins"]
    assert original_mode != _stored()["encoder"]["mode"]


def test_one_group_missing_fails_and_leaves_the_local_config_untouched():
    """3 of 4 CFG_* groups arrive -> ok=False and nothing is written."""
    cfg = _device()
    before = copy.deepcopy(_stored())

    values = dict(FULL_VALUES)
    del values["sleep"]

    result = app.collect_cfg_snapshot(DEV, values)

    assert result["ok"] is False
    assert result["cfg"] is None
    assert result["fields"]["sleep"] is False
    assert result["fields"]["pins"] is True
    # A partial answer must not half-update the stored config.
    assert _stored() == before
    assert result.get("reason")


def test_timeout_without_any_cfg_line_fails_and_keeps_the_local_config():
    """No CFG_* line at all (timeout) -> ok=False with 「设备未回报配置」."""
    _device()
    before = copy.deepcopy(_stored())

    result = app.collect_cfg_snapshot(DEV, {})

    assert result["ok"] is False
    assert result["cfg"] is None
    assert result["reason"] == "设备未回报配置"
    assert all(v is False for v in result["fields"].values())
    assert _stored() == before


# --- the surrounding read-back contract -----------------------------------

def test_read_device_config_returns_the_snapshot_for_the_refresh_action():
    """The refresh action answers with {ok, cfg, fields, at}."""
    _device()
    sent = []

    def fake_send(dev_id, cmd):
        sent.append((dev_id, cmd))
        return {"sent": True, "via": ["ble"]}

    original_send = app.send
    app.send = fake_send
    try:
        # The device does not answer in this test — the read-back must
        # report the failure instead of pretending success.
        result = app.read_device_config(DEV, timeout=0.05)
    finally:
        app.send = original_send

    assert sent == [(DEV, "CMD:GET_CFG")]
    assert result["ok"] is False
    assert result["reason"] == "设备未回报配置"
    assert result["at"] > 0
    assert set(result["fields"]) == {"pins", "keys", "enc", "sleep"}


def test_read_status_is_recorded_per_device():
    """The device page needs the last read-back per device."""
    _device()
    original_send = app.send
    app.send = lambda dev_id, cmd: {"sent": True, "via": ["ble"]}
    try:
        app.read_device_config(DEV, timeout=0.05)
    finally:
        app.send = original_send

    assert DEV in app._read_status
    assert app._read_status[DEV]["ok"] is False
    assert app._read_status[DEV]["at"] > 0


def test_cfg_lines_reach_a_pending_read_back_instead_of_the_stored_config():
    """While a read-back waits, the CFG_* lines are captured, not applied.

    The merge happens once, atomically, in ``collect_cfg_snapshot`` — a
    partial answer must never leave a half-updated config behind.
    """
    _device()
    before = copy.deepcopy(_stored())

    window = app.ReadWindow(DEV)
    with app._read_lock:
        app._read_windows[DEV] = window
    try:
        app._on_line(DEV, "CFG_PINS:22,23,21,19,18,17,16,15,14")
        # The line was captured by the window…
        assert window.snapshot()["pins"] == "22,23,21,19,18,17,16,15,14"
        # …and not written into the stored config behind the request's back.
        assert _stored() == before
    finally:
        with app._read_lock:
            app._read_windows.pop(DEV, None)


def test_malformed_value_fails_without_touching_the_stored_config():
    """A value the host cannot parse must not corrupt the stored config."""
    _device()
    before = copy.deepcopy(_stored())

    values = dict(FULL_VALUES)
    values["pins"] = "22,23,not-a-pin"       # 9 pins expected, garbage given

    result = app.collect_cfg_snapshot(DEV, values)

    assert result["ok"] is False
    assert result["cfg"] is None
    assert _stored() == before
