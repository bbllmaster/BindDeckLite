"""12 键布局（WS-499）的上位机回归测试。

Why this file exists
--------------------
WS-499 让同一套固件支持两种物理形态：编码器布局（9 键 + EC11 = 10 输入）
和 12 键布局（原编码器按压 / A / B 三脚各接一键 = 12 输入）。两端靠
``CFG:LAYOUT:<0|1>`` / ``LAYOUT:<0|1>`` 协商。最容易坏掉的不是新功能本身，
而是**旧设备与旧配置的降级**：旧固件根本不回 ``LAYOUT:``，此时上位机必须
仍然给 10 张输入卡，并且不能假装 12 键配置下发成功。

这些测试 import 生产函数（``app.input_count`` / ``app._on_line`` /
``app.apply_device`` / ``app.validate_pins``），不复制任何映射规则：
改坏 ``host/app.py`` 里的布局逻辑，本文件就会失败。

Run::

    cd host && python -m pytest test_layout.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app

DEV = "t-layout"

C3_ENCODER_PINS = [0, 1, 2, 3, 4, 5, 6, 7, 8]
KEYS_12 = "240,241,242,243,244,245,246,247,248,249,250,251"   # F13..F24


def _device(layout=None, **extra):
    """Register an in-memory device; never touch the real host/config.json."""
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    app.save_config = lambda: None
    app._events.clear()
    cfg = app.new_device_cfg("C3", DEV)
    cfg["pins"] = list(C3_ENCODER_PINS)
    if layout is not None:
        cfg["layout"] = layout
    cfg.update(extra)
    app._config["devices"][DEV] = cfg
    return cfg


def _fake_send(sent, layout_line=True, layout="0", keys_csv=KEYS_12):
    """Stand-in for ``app.send``: answers like a real deck on the BLE link."""

    state = {"tz": "CET-1CEST,M3.5.0,M10.5.0/3"}

    def _send(dev_id, cmd):
        sent.append(cmd)
        if cmd.startswith("CFG:LAYOUT:"):
            if layout_line:
                app._on_line(dev_id, "ACK:" + cmd)
        elif cmd.startswith("CFG:TZ:"):
            state["tz"] = cmd[len("CFG:TZ:"):]
            app._on_line(dev_id, "ACK:" + cmd)
        elif cmd == "CMD:GET_CFG":
            app._on_line(dev_id, "CFG_KEYS:" + keys_csv)
            app._on_line(dev_id, "CFG_PINS:" + ",".join(str(p) for p in C3_ENCODER_PINS))
            app._on_line(dev_id, "CFG_ENC:0")
            app._on_line(dev_id, "CFG_SLEEP:1,5")
            app._on_line(dev_id, "CFG_TZ:" + state["tz"])
            if layout_line:
                app._on_line(dev_id, "CFG_LAYOUT:" + layout)
        else:
            app._on_line(dev_id, "ACK:" + cmd)
        return {"sent": True, "via": ["ble"]}

    return _send


# --- input count: the three cases the acceptance criteria name ------------

def test_encoder_layout_reports_ten_inputs():
    assert app.input_count({"layout": app.LAYOUT_ENCODER}) == 10


def test_twelve_key_layout_reports_twelve_inputs():
    assert app.input_count({"layout": app.LAYOUT_12KEY}) == 12


def test_missing_layout_field_keeps_ten_inputs():
    """Old config.json / old firmware: no ``layout`` -> unchanged behaviour."""
    assert app.input_count({}) == 10
    assert app.input_count({"layout": None}) == 10
    assert app.input_count({"layout": "1"}) == 10


def test_layout_line_from_the_device_selects_the_card_count():
    _device()
    app._on_line(DEV, "LAYOUT:0")
    assert app.input_count(app._config["devices"][DEV]) == 12
    app._on_line(DEV, "LAYOUT:1")
    assert app.input_count(app._config["devices"][DEV]) == 10


def test_old_firmware_that_never_reports_layout_stays_at_ten():
    cfg = _device()
    app._on_line(DEV, "VERSION:1.3.6,Lite,C3,Oct 19 2026 03:20:00,4096")
    cfg = app._config["devices"][DEV]
    assert "layout" not in cfg or cfg["layout"] == app.LAYOUT_ENCODER
    assert app.input_count(cfg) == 10
    assert not cfg.get("layout_supported")


# --- key table ------------------------------------------------------------

def test_default_key_table_has_twelve_entries_ending_in_f23_f24():
    assert len(app.DEFAULT_KEYS) == 12
    assert app.DEFAULT_KEYS[9] == 0xF9       # F22 - the old encoder push code
    assert app.DEFAULT_KEYS[10] == 0xFA      # F23
    assert app.DEFAULT_KEYS[11] == 0xFB      # F24
    assert app.DEFAULT_LABELS[10] == "F23"
    assert app.DEFAULT_LABELS[11] == "F24"

    cfg = app.new_device_cfg("C3", DEV)
    assert len(cfg["keys"]) == 12
    assert [k["code"] for k in cfg["keys"]] == app.DEFAULT_KEYS


def test_twelve_key_readback_grows_the_stored_key_list():
    """A 12-code CFG_KEYS answer must fill inputs 10/11, not be truncated."""
    cfg = _device()
    app._on_line(DEV, "CFG_KEYS:" + KEYS_12)
    codes = [k["code"] for k in app._config["devices"][DEV]["keys"]]
    assert codes == app.DEFAULT_KEYS
    assert app._config["devices"][DEV]["keys"][10]["label"] == "F23"


def test_ten_key_readback_still_fills_only_the_first_ten():
    """Old firmware answers with 10 codes; inputs 10/11 keep their defaults."""
    _device()
    app._on_line(DEV, "CFG_KEYS:240,241,242,243,244,245,246,247,248,3")
    codes = [k["code"] for k in app._config["devices"][DEV]["keys"]]
    assert codes[:9] == app.DEFAULT_KEYS[:9]
    assert codes[9] == 3
    assert codes[10] == app.DEFAULT_KEYS[10]


# --- pin rules ------------------------------------------------------------

def test_encoder_pins_are_legal_buttons_in_the_twelve_key_layout():
    pins = list(C3_ENCODER_PINS)
    pins[8] = 10        # input 9 (editable row) may not reuse the fixed SW pin
    errors = app.validate_pins(pins, "C3", app.LAYOUT_12KEY)
    assert len(errors) == 1
    assert errors[0]["code"] == "extended"
    assert "该脚在 12 键模式下是第 10 键" in errors[0]["message"]


def test_encoder_pins_are_still_reserved_in_the_encoder_layout():
    pins = list(C3_ENCODER_PINS)
    pins[8] = 20
    errors = app.validate_pins(pins, "C3", app.LAYOUT_ENCODER)
    assert len(errors) == 1
    assert errors[0]["code"] == "encoder"


def test_twelve_key_hint_never_offers_the_fixed_pins():
    """WS-503 S1: the hint is derived from the same fields validate_pins() uses.

    It used to advertise GPIO10/20/21 as free while the same sentence called
    them the fixed pins of inputs 9..11, and validate_pins() rejected them.
    """
    for chip in ("C3", "ESP32"):
        rules = app.pin_rules(chip, app.LAYOUT_12KEY)
        free = {pin for lo, hi in app._pin_free_spans(rules)
                for pin in range(lo, hi + 1)}
        assert not free & set(rules["extended"]), (chip, sorted(free))
        # The three fixed pins are still named, so the user knows why.
        assert "第 10/11/12 键" in rules["hint"], (chip, rules["hint"])


# --- apply ---------------------------------------------------------------

def test_apply_sends_layout_first_and_twelve_key_codes(monkeypatch):
    _device(layout=app.LAYOUT_12KEY, layout_supported=True)
    sent = []
    monkeypatch.setattr(app, "send", _fake_send(sent, layout_line=True, layout="0"))

    result = app.apply_device(DEV)

    assert sent[0] == "CFG:LAYOUT:0"
    keys_cmd = next(c for c in sent if c.startswith("CFG:KEYS:"))
    assert keys_cmd == "CFG:KEYS:" + KEYS_12
    assert result["ok"], result
    layout_item = next(i for i in result["items"] if i["field"] == "layout")
    assert layout_item["status"] == "ok"


def test_apply_refuses_twelve_keys_on_firmware_without_layout_support(monkeypatch):
    """Old firmware: no LAYOUT: answer -> say so, send nothing."""
    _device(layout=app.LAYOUT_12KEY)
    sent = []
    monkeypatch.setattr(app, "send", _fake_send(sent, layout_line=False))

    result = app.apply_device(DEV)

    assert result["ok"] is False
    assert result.get("unsupported_layout") is True
    assert "不支持 12 键布局" in result["error"]
    assert not any(c.startswith("CFG:KEYS:") for c in sent)


def test_apply_in_encoder_layout_is_unchanged(monkeypatch):
    """Regression: default hardware must still send exactly ten key codes."""
    _device(layout=app.LAYOUT_ENCODER)
    sent = []
    ten = ",".join(str(c) for c in app.DEFAULT_KEYS[:10])
    monkeypatch.setattr(app, "send", _fake_send(sent, layout_line=True, layout="1",
                                                keys_csv=ten))

    result = app.apply_device(DEV)

    assert result["ok"], result
    keys_cmd = next(c for c in sent if c.startswith("CFG:KEYS:"))
    assert keys_cmd == "CFG:KEYS:" + ",".join(str(c) for c in app.DEFAULT_KEYS[:10])


def test_apply_upgrades_a_ten_entry_config_to_twelve_keys(monkeypatch):
    """WS-503 M1: v1.3.6 config.json holds 10 key entries.

    Switching that deck to the 12-key layout used to send 10 codes while the
    deck answered with 12, so every apply reported ``keys: rejected`` and the
    stored list never grew -- the user could not get out of the loop.
    """
    cfg = _device(layout=app.LAYOUT_12KEY, layout_supported=True)
    del cfg["keys"][10:]                 # exactly what an old config.json holds
    sent = []
    monkeypatch.setattr(app, "send", _fake_send(sent, layout_line=True, layout="0"))

    result = app.apply_device(DEV)

    keys_cmd = next(c for c in sent if c.startswith("CFG:KEYS:"))
    assert keys_cmd == "CFG:KEYS:" + KEYS_12
    keys_item = next(i for i in result["items"] if i["field"] == "keys")
    assert keys_item["status"] == "ok", keys_item
    assert result["ok"], result
    # And the padding is persisted, so the next apply starts from 12 as well.
    assert len(app._config["devices"][DEV]["keys"]) == 12
    assert app.apply_device(DEV)["ok"]


# --- the probe route the UI uses to hide the 12-key option ---------------

def test_layout_route_reports_support_when_the_deck_answers(monkeypatch):
    _device()

    def _send(dev_id, cmd):
        app._on_line(dev_id, "LAYOUT:0")
        return {"sent": True, "via": ["ble"]}

    monkeypatch.setattr(app, "send", _send)
    body = app.app.test_client().post("/api/device/%s/layout" % DEV).get_json()

    assert body["sent"] is True
    assert body["supported"] is True
    assert body["layout"] == app.LAYOUT_12KEY


def test_layout_route_reports_unsupported_for_silent_old_firmware(monkeypatch):
    _device()
    monkeypatch.setattr(app, "send", lambda dev_id, cmd: {"sent": True, "via": ["ble"]})

    body = app.app.test_client().post("/api/device/%s/layout" % DEV).get_json()

    assert body["supported"] is False
    assert app._config["devices"][DEV]["layout_supported"] is False


def test_layout_route_keeps_unknown_when_the_link_is_down(monkeypatch):
    _device()
    monkeypatch.setattr(app, "send", lambda dev_id, cmd: {"sent": False, "via": []})

    body = app.app.test_client().post("/api/device/%s/layout" % DEV).get_json()

    assert body["sent"] is False
    assert body["supported"] is None
