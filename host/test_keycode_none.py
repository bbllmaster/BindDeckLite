"""键码 0「不发键」的上位机回归测试（WS-508 / WS-509）。

Why this file exists
--------------------
在 WS-508 之前，HID 键下拉只能从固定键码表里挑一个键，设备按下时必定发一帧
HID —— 想做「只开浏览器 / 只跑宏」的键，就只能选一个碰巧无害的键码，按键仍
会打进当前焦点窗口。

修法是给键码表加哨兵值 ``0``：固件在 ``sendKeyForInput()`` 里遇到 0 直接返回，
上位机把它单独收进「特殊」分组。这一改最容易坏在三处，本文件逐条钉住：

1. ``KEY_CODES`` 里 0 的文案必须固定（前端 / 回读都靠它显示）；
2. 0 必须排在列表**末尾**（``keyOptions(selected)`` 找不到 selected 时浏览器
   默认选第一项；0 排第一会让未知键码显示成「不发键」）；
3. ``0`` 不能被当成「未配置」而在下发 / 回读时被默认值（F13）覆盖。

这些测试 import 生产函数（``app.KEY_CODES`` / ``app.apply_device`` /
``app._apply_cfg_field`` / ``app._on_line``），不复制键码表：把 ``host/app.py``
里的哨兵值改坏，本文件就会失败。

Run::

    cd host && python -m pytest test_keycode_none.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


DEV = "t-none"
C3_PINS = [0, 1, 2, 3, 4, 5, 6, 7, 8]
NONE_LABEL = "不发键（仅上位机动作）"

# 12 键布局下把「按键 11」（index 10，原 EC11 A 相）设为不发键。
KEYS_12_WITH_NONE = "240,241,242,243,244,245,246,247,248,249,0,251"


def _device(layout=app.LAYOUT_12KEY):
    """Register an in-memory device; never touch the real host/config.json."""
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    app.save_config = lambda: None
    app._events.clear()
    cfg = app.new_device_cfg("C3", DEV)
    cfg["pins"] = list(C3_PINS)
    cfg["layout"] = layout
    # 12 键布局的下发要求固件已确认支持 CFG:LAYOUT，否则 apply_device() 会
    # 先探测并拒绝；这里直接给出「已支持」。
    cfg["layout_supported"] = True
    app._config["devices"][DEV] = cfg
    return cfg


def _fake_send(sent, reported):
    """Stand-in for ``app.send``: ACKs every CFG line and reports *reported*.

    ``reported`` is a mutable dict so the readback answer can echo exactly the
    ``CFG:KEYS:`` payload the host sent — that is what a real deck does.
    """

    def _send(dev_id, cmd):
        sent.append(cmd)
        if cmd.startswith("CFG:KEYS:"):
            reported["keys"] = cmd[len("CFG:KEYS:"):]
        if cmd == "CMD:GET_CFG":
            app._on_line(dev_id, "CFG_KEYS:" + reported["keys"])
            app._on_line(dev_id, "CFG_PINS:" + ",".join(str(p) for p in C3_PINS))
            app._on_line(dev_id, "CFG_ENC:0")
            app._on_line(dev_id, "CFG_SLEEP:1,5")
            app._on_line(dev_id, "CFG_LAYOUT:0")
            return {"sent": True, "via": ["ble"]}
        app._on_line(dev_id, "ACK:" + cmd)
        return {"sent": True, "via": ["ble"]}

    return _send


# --- key table ------------------------------------------------------------

def test_zero_is_in_the_keycode_table_with_a_fixed_label():
    codes = {code: label for label, code in app.KEY_CODES}
    assert 0 in codes, "键码表缺少哨兵值 0（不发键）"
    assert codes[0] == NONE_LABEL


def test_zero_is_the_last_entry_and_f13_is_still_first():
    labels = [label for label, _ in app.KEY_CODES]
    codes = [code for _, code in app.KEY_CODES]
    assert codes[0] == 0xF0 and labels[0] == "F13", "第一项必须仍是 F13"
    assert codes[-1] == 0, "不发键必须排在末尾，否则未知键码回退时会显示成它"
    assert codes.count(0) == 1, "0 只能出现一次，前端按键码过滤分组依赖这一点"


# --- apply: 0 原样下发 -----------------------------------------------------

def test_apply_sends_zero_for_the_configured_input():
    cfg = _device()
    cfg["keys"][10]["code"] = 0
    cfg["keys"][10]["label"] = NONE_LABEL

    sent, reported = [], {"keys": ""}
    original_send = app.send
    app.send = _fake_send(sent, reported)
    try:
        result = app.apply_device(DEV)
    finally:
        app.send = original_send

    keys_cmd = next(c for c in sent if c.startswith("CFG:KEYS:"))
    values = keys_cmd[len("CFG:KEYS:"):].split(",")
    assert len(values) == 12
    assert values[10] == "0", "第 11 个输入必须以 0 下发，不能被默认值顶掉"

    item = next(i for i in result["items"] if i["field"] == "keys")
    assert item["status"] == "ok", item
    assert item["expected"] == item["actual"] == KEYS_12_WITH_NONE


# --- readback: 0 原样保留，不被当成缺省 ------------------------------------

def test_readback_keeps_zero_and_its_label():
    cfg = app.new_device_cfg("C3", DEV)
    assert app._apply_cfg_field(cfg, "keys", KEYS_12_WITH_NONE) is True
    assert cfg["keys"][10]["code"] == 0
    assert cfg["keys"][10]["label"] == NONE_LABEL, "回读不能把 0 显示成裸数字 0"
    # 相邻的普通键码不受影响。
    assert cfg["keys"][9]["code"] == 0xF9
    assert cfg["keys"][11]["code"] == 0xFB


def test_device_readback_line_keeps_zero_in_the_stored_config():
    """「读取设备配置」走 _on_line，0 不能被写回成默认键码（F13/F22…）。"""
    cfg = _device()
    app._on_line(DEV, "CFG_KEYS:" + KEYS_12_WITH_NONE)

    stored = app._config["devices"][DEV]
    assert [k["code"] for k in stored["keys"]] == [
        0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9, 0, 0xFB,
    ]
    assert stored["keys"][10]["label"] == NONE_LABEL
    # 普通键码的文案没有被 0 的哨兵值带偏。
    assert stored["keys"][0]["label"] == "F13"
    assert cfg is stored
