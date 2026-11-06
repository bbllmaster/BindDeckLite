"""深睡唤醒能力（WS-542）的上位机回归测试。

Why this file exists
--------------------
经典 ESP32 的深睡入口原先固定 ``esp_sleep_enable_ext0_wakeup(ENCODER_SW, 0)``，
而 ``ENCODER_SW`` 是 GPIO5 —— 不是 RTC GPIO，返回值也没人看，于是设备在没有任何
唤醒源的情况下入睡，按任何键都叫不醒。修复后固件按「芯片 + 当前布局 + 当前引脚」
生成唤醒掩码，上位机必须在界面上如实说明哪些输入能唤醒，否则用户会按着醒不来的
键等下去。

``app.wake_inputs()`` 是这条文案的唯一计算来源（JS 只负责把输入号排成区间），
所以本文件直接 import 它，不复制任何芯片/引脚映射：

    * 复制一份常量，改坏生产代码测试照样绿 —— 那就是假阳性；
    * 直接调用生产函数，改坏 ``host/app.py`` 的 RTC 列表或布局分支，这里必红。

Run::

    cd host && python -m pytest test_sleep_wake.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app

ESP32_DEFAULT = [13, 12, 14, 27, 32, 33, 25, 26, 4]
C3_DEFAULT = [0, 1, 2, 3, 4, 5, 6, 7, 8]


def _wake(info):
    return info["wake"]


def _no_wake(info):
    return {item["index"]: item for item in info["no_wake"]}


def test_esp32_default_all_nine_buttons_wake():
    """默认经典 ESP32：按键 1–9（GPIO13/12/14/27/32/33/25/26/4）全在 RTC 域。"""
    info = app.wake_inputs("ESP32", app.LAYOUT_ENCODER, ESP32_DEFAULT)
    assert _wake(info) == list(range(9))

    # 第 10 输入是编码器按压，接在 GPIO5 —— 非 RTC，唤不醒。
    blocked = _no_wake(info)
    assert list(blocked) == [9]
    assert blocked[9]["pin"] == app.ENCODER_PINS["ESP32"]["SW"] == 5
    assert blocked[9]["reason"] == "non-rtc"


def test_esp32_twelve_key_layout_blocks_fixed_pins():
    """12 键布局：第 10–12 键固定在 GPIO5/18/19，三者都不是 RTC。"""
    info = app.wake_inputs("ESP32", app.LAYOUT_12KEY, ESP32_DEFAULT)
    assert _wake(info) == list(range(9))

    blocked = _no_wake(info)
    assert sorted(blocked) == [9, 10, 11]
    assert [blocked[i]["pin"] for i in (9, 10, 11)] == [5, 18, 19]
    assert {blocked[i]["reason"] for i in (9, 10, 11)} == {"non-rtc"}


def test_c3_default_first_six_buttons_wake():
    """C3 深睡只能被 GPIO0–5 唤醒：按键 1–6 可以，7–9 与编码器按压不行。"""
    info = app.wake_inputs("C3", app.LAYOUT_ENCODER, C3_DEFAULT)
    assert _wake(info) == list(range(6))

    blocked = _no_wake(info)
    assert sorted(blocked) == [6, 7, 8, 9]
    assert [blocked[i]["pin"] for i in (6, 7, 8)] == [6, 7, 8]
    assert {blocked[i]["reason"] for i in blocked} == {"non-rtc"}


def test_c3_twelve_key_layout_blocks_inputs_10_to_12():
    """C3 12 键：第 10–12 键在 GPIO10/20/21，同样不在 0–5。"""
    info = app.wake_inputs("C3", "12key", C3_DEFAULT)
    assert _wake(info) == list(range(6))

    blocked = _no_wake(info)
    assert sorted(blocked) == [6, 7, 8, 9, 10, 11]
    assert [blocked[i]["pin"] for i in (9, 10, 11)] == [10, 20, 21]
    assert {blocked[i]["reason"] for i in (9, 10, 11)} == {"non-rtc"}


def test_layout_accepts_protocol_int_and_names():
    """布局既可能来自协议（0/1），也可能来自 UI 的名字，二者必须一致。"""
    by_int = app.wake_inputs("ESP32", app.LAYOUT_12KEY, ESP32_DEFAULT)
    assert by_int == app.wake_inputs("ESP32", "12key", ESP32_DEFAULT)
    assert by_int == app.wake_inputs("ESP32", {"layout": app.LAYOUT_12KEY}, ESP32_DEFAULT)

    enc = app.wake_inputs("ESP32", app.LAYOUT_ENCODER, ESP32_DEFAULT)
    assert enc == app.wake_inputs("ESP32", "encoder", ESP32_DEFAULT)
    assert enc == app.wake_inputs("ESP32", None, ESP32_DEFAULT)


def test_invalid_and_duplicate_pins_do_not_raise():
    """表单里正在输入的空值 / 乱码 / 越界 / 重复不能把这一行弄崩。"""
    messy = [None, "", "abc", -1, 999, 13, 13, 6, 4]
    info = app.wake_inputs("ESP32", app.LAYOUT_ENCODER, messy)

    # 合法的 RTC 脚仍然被认出来；重复的 13 不会抛异常（固件掩码本来就去重）。
    assert 5 in _wake(info) and 6 in _wake(info)

    blocked = _no_wake(info)
    for idx in (0, 1, 2, 3, 4):
        assert blocked[idx]["reason"] == "range"
    # 6 是 SPI Flash 脚（GPIO6–11）：不是「非 RTC」，而是这块芯片根本不能当按键。
    assert blocked[7]["reason"] == "range"
    # 输入列表短于 9 行时，后面的输入按空值报告，不 IndexError。
    short = app.wake_inputs("C3", app.LAYOUT_ENCODER, [0, 1])
    assert set(_no_wake(short)) == set(range(2, 10))
    assert all(item["reason"] == "range"
               for item in short["no_wake"] if item["index"] < 9)


def test_empty_pin_list_reports_every_row():
    """没有任何引脚（旧配置 / 首次打开）也不崩，且不为任何输入背书。"""
    info = app.wake_inputs("ESP32", app.LAYOUT_ENCODER, [])
    assert _wake(info) == []
    assert sorted(_no_wake(info)) == list(range(10))
