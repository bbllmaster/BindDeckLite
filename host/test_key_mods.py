"""每输入修饰键（WS-546）的上位机回归测试。

Why this file exists
--------------------
WS-546 给每个输入加了 4 位修饰键掩码（bit0 Ctrl / bit1 Shift / bit2 Alt /
bit3 GUI），设备端自己发组合键，不再依赖上位机宏。改动最容易坏在四处，本文件
逐条钉住：

1. ``/api/apply`` 必须在 ``CFG:KEYS:`` 之后发 ``CFG:MODS:``，且值个数等于当前
   布局的输入数（12 键布局 12 个），越界值按 0x0F 截断后再下发；
2. ``CFG_MODS:`` 回读必须写进 ``cfg["keys"][i]["mods"]``，并顺带把设备标记为
   「支持修饰键」；
3. 数量 / 取值非法的回读整条拒绝，已有的掩码不被改坏；
4. 旧固件（无 ``CFG_MODS:``、能力探测超时）→ 标记为不支持，且 ``/api/apply``
   一条 ``CFG:MODS:`` 都不发；
5. 摘要文案的修饰键顺序固定 Ctrl→Shift→Alt→Win，且该顺序只有一份来源
   （``app.MOD_DEFS``，由 ``/api/keycodes`` 下发给前端）。

这些测试 import 生产函数（``app.apply_device`` / ``app._apply_cfg_field`` /
``app._on_line`` / ``app.probe_mods`` / ``app.MOD_DEFS`` / ``app.api_keycodes``），
不在测试里复制掩码表或行拼装逻辑：把 ``host/app.py`` 里的实现改坏，本文件就会失败。

Run::

    cd host && python -m pytest test_key_mods.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


DEV = "t-mods"
C3_PINS = [0, 1, 2, 3, 4, 5, 6, 7, 8]
KEYS_12 = "240,241,242,243,244,245,246,247,248,249,250,251"   # F13..F24
MODS_12 = "1,0,2,0,4,0,8,0,0,0,0,15"
MOD_MASK = 0x0F


def _device(supported=None, layout=app.LAYOUT_12KEY):
    """Register an in-memory device; never touch the real host/config.json."""
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    app.save_config = lambda: None
    app._events.clear()
    cfg = app.new_device_cfg("C3", DEV)
    cfg["pins"] = list(C3_PINS)
    cfg["layout"] = layout
    # 12 键布局的下发要求固件已确认支持 CFG:LAYOUT。
    cfg["layout_supported"] = True
    if supported is not None:
        cfg["mods_supported"] = supported
    app._config["devices"][DEV] = cfg
    return cfg


def _fake_send(sent, reported):
    """Stand-in for ``app.send``: ACKs every CFG line and answers GET_CFG.

    ``reported`` is a mutable dict describing what the fake deck knows:
      * ``keys``  — the code csv it reports (echo of CFG:KEYS by default)
      * ``mods``  — the mask csv it reports; ``None`` = old firmware that has
                    no CFG_MODS line at all.
    """

    def _send(dev_id, cmd):
        sent.append(cmd)
        if cmd.startswith("CFG:KEYS:"):
            reported["keys"] = cmd[len("CFG:KEYS:"):]
        if cmd.startswith("CFG:TZ:"):
            reported["tz"] = cmd[len("CFG:TZ:"):]
        if cmd.startswith("CFG:MODS:"):
            # A modifier-capable deck echoes exactly what it accepted.
            if reported.get("mods") is not None:
                reported["mods"] = cmd[len("CFG:MODS:"):]
        if cmd == "CMD:GET_CFG":
            app._on_line(dev_id, "CFG_KEYS:" + reported["keys"])
            app._on_line(dev_id, "CFG_PINS:" + ",".join(str(p) for p in C3_PINS))
            app._on_line(dev_id, "CFG_ENC:0")
            app._on_line(dev_id, "CFG_SLEEP:1,5")
            app._on_line(dev_id, "CFG_TZ:" + reported.get("tz", ""))
            app._on_line(dev_id, "CFG_LAYOUT:0")
            if reported.get("mods") is not None:
                app._on_line(dev_id, "CFG_MODS:" + reported["mods"])
            return {"sent": True, "via": ["ble"]}
        app._on_line(dev_id, "ACK:" + cmd)
        return {"sent": True, "via": ["ble"]}

    return _send


def _apply(cfg_mods, supported, reported_mods):
    """Run ``apply_device`` against the fake deck and return (sent, result)."""
    cfg = _device(supported=supported)
    for i, mask in enumerate(cfg_mods):
        cfg["keys"][i]["mods"] = mask
    sent = []
    reported = {"keys": "", "mods": reported_mods}
    original_send = app.send
    app.send = _fake_send(sent, reported)
    try:
        result = app.apply_device(DEV)
    finally:
        app.send = original_send
    return sent, result


# --- 下发：CFG:MODS 紧跟 CFG:KEYS -----------------------------------------

def test_apply_sends_mods_right_after_keys_with_one_value_per_input():
    masks = [1, 0, 2, 0, 4, 0, 8, 0, 0, 0, 0, 15]
    sent, result = _apply(masks, supported=True, reported_mods="")

    keys_at = next(i for i, c in enumerate(sent) if c.startswith("CFG:KEYS:"))
    mods_at = next(i for i, c in enumerate(sent) if c.startswith("CFG:MODS:"))
    assert mods_at == keys_at + 1, "CFG:MODS 必须紧跟 CFG:KEYS（行顺序是协议的一部分）"

    values = sent[mods_at][len("CFG:MODS:"):].split(",")
    assert len(values) == 12, "12 键布局必须下发 12 个掩码"
    assert values == [str(m) for m in masks]

    item = next(i for i in result["items"] if i["field"] == "mods")
    assert item["status"] == "ok", item
    assert item["expected"] == item["actual"] == MODS_12


def test_apply_clamps_out_of_range_masks_to_0x0f():
    """越界值不能原样下发（固件会整条拒绝），下发前按 0x0F 截断。"""
    sent, _result = _apply([99] + [0] * 11, supported=True, reported_mods="")
    mods_line = next(c for c in sent if c.startswith("CFG:MODS:"))
    values = mods_line[len("CFG:MODS:"):].split(",")
    assert values[0] == str(99 & MOD_MASK)
    assert all(0 <= int(v) <= MOD_MASK for v in values)


def test_apply_uses_the_current_layout_input_count():
    """编码器布局只发 10 个掩码，不是 12 个。"""
    cfg = _device(supported=True, layout=app.LAYOUT_ENCODER)
    for item in cfg["keys"]:
        item["mods"] = 1
    sent = []
    reported = {"keys": "", "mods": ""}
    original_send = app.send
    app.send = _fake_send(sent, reported)
    try:
        app.apply_device(DEV)
    finally:
        app.send = original_send

    mods_line = next(c for c in sent if c.startswith("CFG:MODS:"))
    assert len(mods_line[len("CFG:MODS:"):].split(",")) == 10


# --- 回读：CFG_MODS 写入 keys[i].mods --------------------------------------

def test_cfg_mods_readback_lands_in_key_items():
    cfg = app.new_device_cfg("C3", DEV)
    assert app._apply_cfg_field(cfg, "mods", MODS_12) is True
    assert [k["mods"] for k in cfg["keys"]] == [1, 0, 2, 0, 4, 0, 8, 0, 0, 0, 0, 15]
    assert cfg["mods_supported"] is True, "设备回了 CFG_MODS 就是支持修饰键"

    # 「读取设备配置」走 _on_line，掩码必须落到已存配置上。
    _device()
    app._on_line(DEV, "CFG_MODS:" + MODS_12)
    stored = app._config["devices"][DEV]
    assert [k["mods"] for k in stored["keys"]][:12] == [1, 0, 2, 0, 4, 0, 8, 0, 0, 0, 0, 15]


# --- 非法回读：整条拒绝、不改旧值 ------------------------------------------

def test_invalid_mods_readback_is_rejected_and_keeps_old_values():
    cfg = app.new_device_cfg("C3", DEV)
    assert app._apply_cfg_field(cfg, "mods", MODS_12) is True
    before = [k["mods"] for k in cfg["keys"]]

    # 13 个值（多于 12 键布局的输入数）
    assert app._apply_cfg_field(cfg, "mods", MODS_12 + ",0") is False
    # 越界（>15）与负数
    assert app._apply_cfg_field(cfg, "mods", "16" + ",0" * 11) is False
    assert app._apply_cfg_field(cfg, "mods", "-1" + ",0" * 11) is False
    # 非数字
    assert app._apply_cfg_field(cfg, "mods", "x" + ",0" * 11) is False

    assert [k["mods"] for k in cfg["keys"]] == before, "被拒绝的整条不能改动任何掩码"


# --- 旧固件：不支持则不拼 CFG:MODS ----------------------------------------

def test_old_firmware_never_gets_a_cfg_mods_line():
    sent, result = _apply([1] * 12, supported=False, reported_mods=None)
    assert not [c for c in sent if c.startswith("CFG:MODS:")], \
        "能力探测已判定不支持时，一条 CFG:MODS 都不能发"
    assert not [i for i in result["items"] if i["field"] == "mods"], \
        "没发就不该出现在逐项校验里"
    # 其它字段照常下发并且全部通过。
    assert result["ok"] is True, result["items"]


def test_mods_probe_reports_unsupported_when_the_deck_stays_silent():
    _device()
    original_send = app.send
    app.send = lambda dev_id, cmd: {"sent": True, "via": ["ble"]}
    try:
        probe = app.probe_mods(DEV, timeout=0.05)
    finally:
        app.send = original_send
    assert probe["sent"] is True
    assert probe["answered"] is False
    assert probe["mods"] is None


def test_mods_probe_reports_supported_when_the_deck_answers():
    _device()
    original_send = app.send

    def _send(dev_id, cmd):
        app._on_line(dev_id, "MODS:" + MODS_12)
        return {"sent": True, "via": ["ble"]}

    app.send = _send
    try:
        probe = app.probe_mods(DEV, timeout=0.05)
    finally:
        app.send = original_send
    assert probe["answered"] is True
    assert probe["mods"] == MODS_12


# --- 摘要文案顺序：Ctrl → Shift → Alt → Win --------------------------------

def test_modifier_display_order_is_ctrl_shift_alt_win():
    assert [m["bit"] for m in app.MOD_DEFS] == [0x01, 0x02, 0x04, 0x08]
    labels = [app.t(m["key"]) for m in app.MOD_DEFS]
    assert labels == ["Ctrl", "Shift", "Alt", "Win"], labels
    assert [m["id"] for m in app.MOD_DEFS] == [
        "kdModCtrl", "kdModShift", "kdModAlt", "kdModWin",
    ]


def test_keycodes_endpoint_publishes_the_same_order():
    """/api/keycodes 是前端的唯一来源：抽屉与摘要都按这个顺序渲染。"""
    payload = app.app.test_client().get("/api/keycodes").get_json()
    assert [m["label"] for m in payload["mods"]] == ["Ctrl", "Shift", "Alt", "Win"]
    assert [m["bit"] for m in payload["mods"]] == [0x01, 0x02, 0x04, 0x08]
