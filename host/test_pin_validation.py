"""Pin validation for the device page (WS-476).

Why this file exists
--------------------
The device page rendered nine bare number inputs with no validation. Clearing
one box stored ``null``; ``apply_device()`` then built ``CFG:PINS`` with
``str(int(None))`` and the route answered 500 — the user saw only
「500 INTERNAL SERVER ERROR」 next to a box that looked perfectly normal.

``app.validate_pins()`` is now the **single** copy of the rule (the firmware's
``isValidButtonPin`` / ``isValidPinSet`` from ``src/main.cpp``). These tests
import that production function and the production routes; nothing here
re-implements the rule, so a change to ``host/app.py`` that breaks it fails
this file.

What is covered
---------------
  * empty / ``None`` / short lists -> 「按键 N 不能为空」;
  * non-integer, out-of-range, and per-chip reserved ranges
    (C3 11-17; classic 6-11 and 34-39);
  * encoder CLK / DT / SW pins per chip;
  * duplicate values flagged on *every* row that shares the pin, so fixing one
    clears both;
  * ``POST /api/apply`` with a ``null`` pin -> 400 + readable error and **no**
    ``CFG:`` line leaves the host;
  * ``GET /api/defaults`` publishes both the rule and per-row errors.

Run::

    cd host && python -m pytest test_pin_validation.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


C3_GOOD = [0, 1, 2, 3, 4, 5, 6, 7, 8]
ESP32_GOOD = [13, 12, 14, 27, 32, 33, 25, 26, 4]
DEV = "t-pins"


def _by_index(errors):
    return {e["index"]: e for e in errors}


def _messages(errors):
    return [e["message"] for e in errors]


def _device(chip="ESP32", pins=None):
    """Register an in-memory device; never touch the real host/config.json."""
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    app.save_config = lambda: None
    app._events.clear()
    cfg = app.new_device_cfg(chip, DEV)
    cfg["pins"] = list(ESP32_GOOD if chip == "ESP32" else C3_GOOD) if pins is None else pins
    app._config["devices"][DEV] = cfg
    return cfg


# --- the happy path -------------------------------------------------------

def test_default_pin_sets_are_accepted():
    """Both shipped pin maps must pass — otherwise apply would always block."""
    assert app.validate_pins(ESP32_GOOD, "ESP32") == []
    assert app.validate_pins(C3_GOOD, "C3") == []


def test_unknown_chip_falls_back_to_the_classic_map():
    """An old config without ``chip`` validates like a classic ESP32."""
    assert app.validate_pins(ESP32_GOOD, "") == []
    errs = app.validate_pins([13, 12, 14, 27, 32, 33, 25, 26, 6], "")
    assert errs and "SPI Flash" in errs[0]["message"]


# --- the 500: empty / missing values --------------------------------------

def test_cleared_row_is_reported_as_empty():
    pins = list(C3_GOOD)
    pins[2] = None
    errors = app.validate_pins(pins, "C3")

    assert len(errors) == 1
    assert errors[0]["index"] == 2
    assert errors[0]["code"] == "empty"
    assert errors[0]["message"] == "按键 3 不能为空"


def test_empty_string_counts_as_empty():
    pins = list(C3_GOOD)
    pins[0] = "   "
    errors = app.validate_pins(pins, "C3")

    assert _messages(errors) == ["按键 1 不能为空"]


def test_short_list_marks_every_missing_row():
    """A legacy config with fewer than nine pins marks the tail, not a crash."""
    errors = app.validate_pins(C3_GOOD[:3], "C3")

    assert [e["index"] for e in errors] == [3, 4, 5, 6, 7, 8]
    assert all(e["code"] == "empty" for e in errors)
    assert errors[0]["message"] == "按键 4 不能为空"


def test_non_list_value_reports_all_nine_rows():
    """``"pins": null`` in config.json is bad data, not an exception."""
    errors = app.validate_pins(None, "C3")

    assert len(errors) == app.PIN_COUNT
    assert errors[0]["message"] == "按键 1 不能为空"


# --- types and ranges -----------------------------------------------------

def test_non_integer_values_are_rejected():
    pins = list(C3_GOOD)
    pins[0] = "abc"
    pins[1] = 6.5
    pins[2] = True          # bool is an int subclass — must not sneak through
    errors = _by_index(app.validate_pins(pins, "C3"))

    for idx in (0, 1, 2):
        assert errors[idx]["code"] == "not_integer"
        assert errors[idx]["message"].startswith("按键 %d 必须是 0-39 的整数" % (idx + 1))


def test_numeric_string_is_accepted():
    """A legacy "13" still reaches the firmware as GPIO13, so it is not an error.

    Both validation entry points (JSON config and the ``?pins=`` query) carry
    numbers as text, so flagging numeric strings would paint every row red.
    """
    pins = [str(p) for p in C3_GOOD]
    assert app.validate_pins(pins, "C3") == []


def test_out_of_range_uses_the_global_0_39_rule():
    for bad in (-1, 40):
        pins = list(ESP32_GOOD)
        pins[0] = bad
        errors = app.validate_pins(pins, "ESP32")
        assert _messages(errors) == ["按键 1 必须填 0-39 的整数"]


def test_c3_pin_above_21_does_not_exist():
    pins = list(C3_GOOD)
    pins[0] = 25
    errors = app.validate_pins(pins, "C3")

    assert len(errors) == 1
    assert errors[0]["code"] == "out_of_range"
    assert "在 C3 上不存在" in errors[0]["message"]


def test_c3_internal_flash_range_is_rejected():
    pins = list(C3_GOOD)
    pins[0] = 12
    errors = app.validate_pins(pins, "C3")

    assert len(errors) == 1
    assert errors[0]["message"] == "按键 1 的 GPIO 12 在 C3 上是内部 Flash 引脚，不能用作按键"


def test_classic_spi_flash_range_is_rejected():
    pins = list(ESP32_GOOD)
    pins[0] = 6
    errors = app.validate_pins(pins, "ESP32")

    assert len(errors) == 1
    assert errors[0]["message"] == "按键 1 的 GPIO 6 是 SPI Flash 引脚（GPIO 6-11），不能用作按键"


def test_classic_input_only_range_is_rejected():
    pins = list(ESP32_GOOD)
    pins[0] = 35
    errors = app.validate_pins(pins, "ESP32")

    assert len(errors) == 1
    assert "仅输入引脚" in errors[0]["message"]


def test_c3_does_not_apply_the_classic_spi_flash_rule():
    """GPIO 6-8 are ordinary C3 pins and the default C3 buttons sit on them."""
    assert app.validate_pins([6, 7, 8, 0, 1, 2, 3, 4, 5], "C3") == []


# --- encoder pins ---------------------------------------------------------

def test_c3_encoder_pins_are_rejected():
    expectations = {20: "编码器 A 相脚（CLK）", 21: "编码器 B 相脚（DT）", 10: "编码器按压脚"}
    for pin, role in expectations.items():
        pins = list(C3_GOOD)
        pins[0] = pin
        errors = app.validate_pins(pins, "C3")
        assert len(errors) == 1
        assert errors[0]["code"] == "encoder"
        assert errors[0]["message"] == "按键 1 的 GPIO %d 与%s冲突" % (pin, role)


def test_classic_encoder_pins_are_rejected():
    expectations = {18: "编码器 A 相脚（CLK）", 19: "编码器 B 相脚（DT）", 5: "编码器按压脚"}
    for pin, role in expectations.items():
        pins = list(ESP32_GOOD)
        pins[0] = pin
        errors = app.validate_pins(pins, "ESP32")
        assert len(errors) == 1
        assert errors[0]["message"] == "按键 1 的 GPIO %d 与%s冲突" % (pin, role)


# --- duplicates -----------------------------------------------------------

def test_duplicate_flags_both_rows_and_clears_together():
    # 按键 2 and 按键 5 both on GPIO 7; everything else is a legal C3 pin.
    pins = [0, 7, 2, 3, 7, 5, 6, 8, 9]
    errors = app.validate_pins(pins, "C3")

    assert len(errors) == 2
    assert [e["index"] for e in errors] == [1, 4]
    assert all(e["code"] == "duplicate" for e in errors)
    assert errors[0]["message"] == "按键 2 与按键 5 都使用了 GPIO 7"
    assert errors[1]["message"] == "按键 2 与按键 5 都使用了 GPIO 7"

    # Fixing one row clears both at once.
    assert app.validate_pins([0, 7, 2, 3, 1, 5, 6, 8, 9], "C3") == []


def test_three_way_duplicate_names_every_row():
    pins = [0, 0, 0, 3, 4, 5, 6, 7, 8]
    errors = app.validate_pins(pins, "C3")

    assert [e["index"] for e in errors] == [0, 1, 2]
    assert errors[0]["message"] == "按键 1、按键 2、按键 3 都使用了 GPIO 0"


def test_duplicate_uses_the_lowest_index_first():
    pins = [3, 2, 1, 0, 7, 4, 5, 6, 7]
    errors = _by_index(app.validate_pins(pins, "C3"))

    assert errors[8]["message"] == "按键 5 与按键 9 都使用了 GPIO 7"


# --- routes ---------------------------------------------------------------

def test_apply_route_returns_400_and_sends_nothing(monkeypatch):
    """The exact 500 repro: pins含 null，点应用 → 400，且一条 CFG: 都不发。"""
    pins = list(C3_GOOD)
    pins[2] = None
    _device("C3", pins)

    sent = []
    monkeypatch.setattr(app, "send", lambda dev_id, cmd: sent.append(cmd) or {"sent": True, "via": ["ble"]})

    client = app.app.test_client()
    res = client.post("/api/apply/%s" % DEV)

    assert res.status_code == 400
    body = res.get_json()
    assert body["ok"] is False
    assert body["error"] == "按键 3 不能为空"
    assert body["pin_errors"][0]["index"] == 2
    assert sent == []


def test_apply_route_reports_duplicates_before_sending(monkeypatch):
    pins = list(ESP32_GOOD)
    pins[4] = pins[1]
    _device("ESP32", pins)

    sent = []
    monkeypatch.setattr(app, "send", lambda dev_id, cmd: sent.append(cmd) or {"sent": True, "via": ["ble"]})

    res = app.app.test_client().post("/api/apply/%s" % DEV)

    assert res.status_code == 400
    assert "都使用了 GPIO" in res.get_json()["error"]
    assert sent == []


def test_defaults_publishes_rules_and_per_row_errors():
    client = app.app.test_client()

    res = client.get("/api/defaults?chip=C3&pins=0,1,,3,4,5,6,7,8")
    assert res.status_code == 200
    body = res.get_json()

    assert body["pinRules"]["chip"] == "C3"
    assert body["pinRules"]["count"] == app.PIN_COUNT
    assert body["pinRules"]["encoder"] == {"CLK": 20, "DT": 21, "SW": 10}
    assert [e["index"] for e in body["pinErrors"]] == [2]
    assert body["pinErrors"][0]["message"] == "按键 3 不能为空"


def test_defaults_without_pins_keeps_the_old_contract():
    """The startup call (no query) still returns a plain default config."""
    body = app.app.test_client().get("/api/defaults").get_json()

    assert body["pins"] == app.DEFAULT_PINS["ESP32"]
    assert body["chip"] == "ESP32"
    assert "pinErrors" not in body
    assert body["pinRules"]["chip"] == "ESP32"


def test_defaults_rules_match_validate_pins():
    """The published rule and the validator must not drift apart."""
    client = app.app.test_client()

    classic = client.get("/api/defaults?chip=ESP32").get_json()["pinRules"]
    assert classic["min"] == 0 and classic["max"] == 39
    assert classic["blocked"] == [[6, 11], [34, 39]]
    assert classic["encoder"] == {"CLK": 18, "DT": 19, "SW": 5}

    c3 = client.get("/api/defaults?chip=C3").get_json()["pinRules"]
    assert (c3["min"], c3["max"]) == (0, 21)
    assert c3["blocked"] == [[11, 17]]
