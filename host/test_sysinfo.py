"""Regression guards for the ``CMD:SYSINFO`` health line (WS-445).

Why this file exists
--------------------
The deck already knew its uptime, reset reason, NVS state and free heap, but
only printed them on the serial console — which a packaged exe does not have
and a C3 without the USB cable cannot show either.  ``CMD:SYSINFO`` puts them
on the same line protocol the host already speaks::

    SYS:uptime=<s>,reset=<esp_reset_reason()>,nvs=<0|1>,heap=<bytes>,safe=<0|1>,boots=<n>

What is guarded here
--------------------
  * the **production** parser / label functions imported from ``app.py`` (a
    copy of the parsing in this file would keep passing after the real one
    broke);
  * the request path: ``read_sysinfo`` sends ``CMD:SYSINFO``, the ``SYS:``
    line arriving on the BLE callback thread completes the wait, and the
    result is cached per device in memory;
  * old firmware: no answer inside the timeout renders "unsupported" and is
    logged at info level (never ``error``);
  * a disconnected deck keeps the previous sample instead of faking one;
  * the firmware source still emits the documented line (no hardware in CI,
    so this is a source contract test, same approach as ``test_bond_status``).

Run::

    cd host && python -m pytest test_sysinfo.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as host_app

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_CPP = os.path.join(REPO_ROOT, "src", "main.cpp")

FULL_LINE = "uptime=11520,reset=1,nvs=1,heap=98304,safe=0,boots=1"


# --- parser ---------------------------------------------------------------

def test_full_line_parses_all_six_fields():
    got = host_app.parse_sysinfo(FULL_LINE)
    assert got == {
        "uptime": 11520,
        "reset": 1,
        "nvs": 1,
        "heap": 98304,
        "safe": 0,
        "boots": 1,
    }


def test_missing_fields_become_none_not_an_exception():
    """A short line (older/newer firmware) must not cost the whole card."""
    got = host_app.parse_sysinfo("uptime=30,heap=2048")
    assert got["uptime"] == 30
    assert got["heap"] == 2048
    assert got["reset"] is None
    assert got["nvs"] is None
    assert got["safe"] is None
    assert got["boots"] is None


def test_garbage_values_are_rejected():
    got = host_app.parse_sysinfo("uptime=abc,reset=-1,nvs=7,heap=,safe=x,boots=1")
    assert got == {"uptime": None, "reset": None, "nvs": None,
                   "heap": None, "safe": None, "boots": 1}


def test_unknown_keys_are_ignored():
    got = host_app.parse_sysinfo("uptime=5,temp=42")
    assert got["uptime"] == 5
    assert "temp" not in got


# --- reset wording --------------------------------------------------------

def test_known_reset_codes_are_translated():
    assert host_app.reset_reason_text(1) == "上电(1)"
    assert host_app.reset_reason_text(3) == "软件重启(3)"
    assert host_app.reset_reason_text(8) == "深度睡眠唤醒(8)"


def test_unknown_reset_code_keeps_the_number():
    """A code from a newer IDF must still reach the user verbatim."""
    assert host_app.reset_reason_text(99) == "未知(99)"


def test_reset_zero_is_unknown():
    text = host_app.reset_reason_text(0)
    assert text.startswith("未知")
    assert "0" in text


def test_reset_none_is_unknown():
    assert host_app.reset_reason_text(None) == "未知"


# --- display / flags ------------------------------------------------------

def test_view_of_a_healthy_line():
    view = host_app.sysinfo_view(FULL_LINE)
    assert view["display"] == {
        "reset": "上电(1)",
        "uptime": "3 小时 12 分",
        "nvs": "可用",
        "heap": "96 KB",
    }
    assert view["flags"] == {"nvs_bad": False, "heap_low": False,
                             "safe": False, "deepsleep": False}
    assert view["notes"] == {}


def test_nvs_zero_is_flagged_and_explained():
    view = host_app.sysinfo_view("uptime=60,reset=1,nvs=0,heap=98304,safe=0,boots=1")
    assert view["flags"]["nvs_bad"] is True
    assert view["display"]["nvs"] == "不可用：配置不会保存"


def test_low_heap_is_flagged():
    """Below the 20 KB warning line the card must say OTA may fail."""
    view = host_app.sysinfo_view("uptime=60,reset=1,nvs=1,heap=10240,safe=0,boots=1")
    assert view["flags"]["heap_low"] is True
    assert view["notes"]["heap"] == "内存偏低，可能影响 OTA"
    assert view["display"]["heap"] == "10 KB"


def test_heap_exactly_at_the_threshold_is_not_flagged():
    """The threshold is '< 20 KB'; a deck sitting exactly on it is not warned."""
    view = host_app.sysinfo_view(
        "uptime=60,reset=1,nvs=1,heap=%d,safe=0,boots=1" % host_app.SYSINFO_HEAP_WARN)
    assert view["flags"]["heap_low"] is False


def test_deepsleep_and_safe_mode_notes():
    view = host_app.sysinfo_view("uptime=5,reset=8,nvs=1,heap=98304,safe=1,boots=2")
    assert view["flags"]["deepsleep"] is True
    assert view["notes"]["deepsleep"] == "设备是睡着了，不是死机"
    assert view["notes"]["safe"].startswith("安全模式")


def test_missing_fields_render_unknown():
    view = host_app.sysinfo_view("")
    assert view["display"] == {"reset": "未知", "uptime": "未知",
                               "nvs": "未知", "heap": "未知"}


# --- request path ---------------------------------------------------------

def test_read_sysinfo_round_trip(monkeypatch):
    """The SYS: line arrives on another thread; the request must complete."""
    def fake_send(dev_id, cmd):
        assert cmd == "CMD:SYSINFO"
        host_app._on_line(dev_id, "SYS:" + FULL_LINE)
        return {"sent": True, "via": ["ble"]}

    monkeypatch.setattr(host_app, "send", fake_send)
    result = host_app.read_sysinfo("dev-roundtrip", timeout=0.5)

    assert result["ok"] is True
    assert result["supported"] is True
    assert result["sample"]["uptime"] == 11520
    assert result["display"]["reset"] == "上电(1)"
    assert host_app.sysinfo_snapshot()["dev-roundtrip"]["ok"] is True


def test_read_sysinfo_timeout_means_unsupported(monkeypatch):
    """Old firmware: silence is "not supported", logged as info, never error."""
    monkeypatch.setattr(host_app, "send",
                        lambda dev_id, cmd: {"sent": True, "via": ["ble"]})
    host_app._events.clear()
    result = host_app.read_sysinfo("dev-old", timeout=0.05)

    assert result["ok"] is False
    assert result["supported"] is False
    assert result["reason"] == "timeout"
    assert all(e["level"] != "error" for e in host_app._events)


def test_read_sysinfo_disconnected_keeps_the_previous_sample(monkeypatch):
    monkeypatch.setattr(host_app, "send", lambda dev_id, cmd: {"sent": False, "via": []})
    host_app._sysinfo["dev-gone"] = {"ok": True, "supported": True, "at": 1.0}
    result = host_app.read_sysinfo("dev-gone", timeout=0.05)

    assert result["ok"] is False
    assert result["supported"] is None
    assert result["reason"] == "disconnected"
    # The cache still holds the real sample, so the UI can show it as stale.
    assert host_app.sysinfo_snapshot()["dev-gone"]["ok"] is True


def test_sysinfo_route_is_reachable(monkeypatch):
    """The static route must win over /api/device/<dev_id>/<action>."""
    monkeypatch.setattr(host_app, "read_sysinfo",
                        lambda dev_id: {"ok": True, "dev": dev_id})
    client = host_app.app.test_client()
    resp = client.post("/api/device/abc/sysinfo")
    assert resp.status_code == 200
    assert resp.get_json()["dev"] == "abc"


# --- firmware contract ----------------------------------------------------

def test_firmware_emits_the_documented_sys_line():
    with open(MAIN_CPP, encoding="utf-8") as f:
        src = f.read()
    assert '"CMD:SYSINFO"' in src
    assert "SYS:uptime=%lu,reset=%d,nvs=%d,heap=%lu,safe=%d,boots=%lu" in src
