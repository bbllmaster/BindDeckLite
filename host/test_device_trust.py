"""Unit tests for the per-device ``trusted`` flag (WS-512).

``trusted`` answers one question: *is the configuration this host has stored
the one the deck is actually running?*  Every new entry starts at ``False``
(``new_device_cfg``) because it is a copy of the host defaults, not something
read from the device.  It only becomes ``True`` when the host has evidence:

* an explicit 「读取设备配置」 came back complete (``collect_cfg_snapshot`` via
  ``read_device_config``), or
* an apply whose *every* field read back with the value that was sent
  (``apply_device``).

A partial answer, a rejected field, or the layout probe never flips it.  A
config written before the field existed reads as ``True`` (existing users must
not get a new warning), and reading such an entry must not rewrite the file.

The tests drive the production functions directly (``new_device_cfg``,
``device_cfg``, ``read_device_config``, ``apply_device`` and the Flask routes);
none of the rules are re-implemented here.  ``CONFIG_PATH`` points at a tmp
file so the real ``host/config.json`` is never touched.

Run::

    cd host && python -m pytest test_device_trust.py -q
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


DEV = "t-trust"

# What the deck would report for a complete read-back.  The values differ from
# the ESP32 defaults on purpose — a test has to see the stored config change.
DEVICE_VALUES = {
    "pins": "22,23,21,19,18,17,16,15,14",
    "keys": "240,241,242,243,244,245,246,247,248,249",
    "enc": "2",
    "sleep": "1,5",
}


# --- fixtures -------------------------------------------------------------

@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    """Point ``CONFIG_PATH`` at a tmp file and start from an empty archive."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(app, "CONFIG_PATH", str(path))
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    app._events.clear()
    app._read_status.clear()
    return path


@pytest.fixture
def no_link(monkeypatch):
    """Fail loudly if a test that must not talk to the deck sends anything."""
    sent = []

    def fake_send(dev_id, cmd):
        sent.append(cmd)
        return {"sent": False, "via": []}

    monkeypatch.setattr(app, "send", fake_send)
    return sent


def _stored(dev_id=DEV):
    return app._config["devices"][dev_id]


def _on_disk(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --- 1. a brand-new archive is untrusted ----------------------------------

def test_new_device_entry_starts_untrusted(cfgfile):
    cfg = app.device_cfg(DEV)

    assert cfg["trusted"] is False
    assert _stored()["trusted"] is False
    # …and the flag really was persisted, not just held in memory.
    assert _on_disk(cfgfile)["devices"][DEV]["trusted"] is False


# --- 2. legacy entries are trusted and never rewritten --------------------

def test_legacy_entry_without_the_field_reads_as_trusted(cfgfile):
    legacy = app.new_device_cfg("ESP32", DEV)
    legacy.pop("trusted")                      # config.json written before WS-512
    cfgfile.write_text(json.dumps({"devices": {DEV: legacy}, "settings": {}}),
                       encoding="utf-8")
    before = cfgfile.read_text(encoding="utf-8")

    # Re-load through the production loader, then read the entry the way the
    # Flask routes do.
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    app.load_config()
    cfg = app.device_cfg(DEV)

    assert cfg["trusted"] is True
    # No migration: merely opening the page must not add the field to disk.
    assert cfgfile.read_text(encoding="utf-8") == before


# --- 3. a complete read-back marks the archive trusted --------------------

def test_complete_read_marks_trusted(cfgfile, monkeypatch):
    app.device_cfg(DEV)
    assert _stored()["trusted"] is False

    def fake_send(dev_id, cmd):
        if cmd == "CMD:GET_CFG":
            for field, value in DEVICE_VALUES.items():
                # The production capture path, not a re-implementation of it.
                app._capture_read_window(dev_id, field, value)
        return {"sent": True, "via": ["ble"]}

    monkeypatch.setattr(app, "send", fake_send)
    result = app.read_device_config(DEV, timeout=0.1)

    assert result["ok"] is True
    assert _stored()["trusted"] is True
    assert _on_disk(cfgfile)["devices"][DEV]["trusted"] is True
    # The device's real pins replaced the defaults — that is what makes the
    # archive trustworthy in the first place.
    assert _stored()["pins"] == [22, 23, 21, 19, 18, 17, 16, 15, 14]


def test_partial_read_keeps_untrusted(cfgfile, monkeypatch):
    app.device_cfg(DEV)

    partial = dict(DEVICE_VALUES)
    del partial["sleep"]

    def fake_send(dev_id, cmd):
        if cmd == "CMD:GET_CFG":
            for field, value in partial.items():
                app._capture_read_window(dev_id, field, value)
        return {"sent": True, "via": ["ble"]}

    monkeypatch.setattr(app, "send", fake_send)
    result = app.read_device_config(DEV, timeout=0.1)

    assert result["ok"] is False
    assert _stored()["trusted"] is False
    assert _on_disk(cfgfile)["devices"][DEV]["trusted"] is False


# --- 4. apply: all fields verified -> trusted, anything else unchanged ----

def _apply_send(override=None):
    """Answer an apply the way a healthy deck does.

    Each ``CFG:*`` line is ACKed and ``CMD:GET_CFG`` reports back exactly the
    snapshot the host just sent — except for fields in *override*, which
    simulate a value the deck did not take.
    """
    override = override or {}

    def fake_send(dev_id, cmd):
        with app._apply_lock:
            window = app._apply_windows.get(dev_id)
        if window is not None:
            if cmd.startswith("CFG:"):
                prefix = cmd[len("CFG:"):].split(":", 1)[0]
                field = {name.upper(): name for name, _p, _l in app._ALL_CFG_FIELDS}.get(prefix)
                if field:
                    window.on_ack(field)
            elif cmd == "CMD:GET_CFG":
                for field, value in dict(window.snapshot).items():
                    window.on_readback(field, override.get(field, value))
        return {"sent": True, "via": ["ble"]}

    return fake_send


def test_apply_with_every_field_verified_marks_trusted(cfgfile, monkeypatch):
    app.device_cfg(DEV)
    monkeypatch.setattr(app, "send", _apply_send())

    result = app.apply_device(DEV)

    assert result["ok"] is True
    assert all(i["status"] == "ok" for i in result["items"])
    assert _stored()["trusted"] is True
    assert _on_disk(cfgfile)["devices"][DEV]["trusted"] is True


def test_apply_with_one_field_rejected_keeps_untrusted(cfgfile, monkeypatch):
    app.device_cfg(DEV)
    # The deck kept its own pins: that apply is not proof of anything.
    monkeypatch.setattr(app, "send", _apply_send({"pins": "1,2,3,16,17,21,22,23,24"}))

    result = app.apply_device(DEV)

    assert result["ok"] is False
    assert any(i["status"] == "rejected" for i in result["items"])
    assert _stored()["trusted"] is False


def test_partial_apply_does_not_downgrade_a_trusted_archive(cfgfile, monkeypatch):
    cfg = app.device_cfg(DEV)
    cfg["trusted"] = True
    monkeypatch.setattr(app, "send", _apply_send({"keys": "0,0,0,0,0,0,0,0,0,0"}))

    result = app.apply_device(DEV)

    assert result["ok"] is False
    # "Keep the previous value" — a failed apply must not revoke trust either.
    assert _stored()["trusted"] is True


# --- 5. the draft-save route cannot touch the flag -----------------------

def test_config_post_cannot_set_or_clear_trusted(cfgfile):
    client = app.app.test_client()
    app.device_cfg(DEV)

    # A new (untrusted) archive: the draft autosave must not make it trusted.
    resp = client.post("/api/config/%s" % DEV,
                       json={"trusted": True, "pins": [13, 12, 14, 27, 32, 33, 25, 26, 4]})
    assert resp.status_code == 200
    assert _stored()["trusted"] is False

    # A trusted archive: echoing the whole cfg back must not clear it either.
    _stored()["trusted"] = True
    resp = client.post("/api/config/%s" % DEV, json={"trusted": False})
    assert resp.status_code == 200
    assert _stored()["trusted"] is True

    # The GET the UI uses does expose the field.
    body = client.get("/api/config/%s" % DEV).get_json()
    assert body["trusted"] is True


# --- 6. the layout probe never counts as "read the config" ---------------

def test_layout_probe_does_not_mark_trusted(cfgfile, monkeypatch):
    app.device_cfg(DEV)

    def fake_send(dev_id, cmd):
        if cmd == "CFG:LAYOUT":
            # The deck's real answer, through the production line handler.
            app._on_line(dev_id, "LAYOUT:0")
        return {"sent": True, "via": ["ble"]}

    monkeypatch.setattr(app, "send", fake_send)
    body = app.app.test_client().post("/api/device/%s/layout" % DEV).get_json()

    assert body["sent"] is True
    assert body["layout"] == app.LAYOUT_12KEY
    # Layout is not pins/keys: the archive is still a host-side default.
    assert _stored()["trusted"] is False
    assert _stored()["layout"] == app.LAYOUT_12KEY
