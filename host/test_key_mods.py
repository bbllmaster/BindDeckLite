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


# --- WS-550：探测超时不得一次定论（后端三态 / 可重探）------------------------
#
# 这些用例 POST 的是**生产视图** /api/device/<id>/mods（app.api_device_mods），
# 用桩替换 app.send 来模拟「链路已发但设备不回 MODS:」的探测超时，
# 断言的是生产代码写进 cfg 的能力结论与返回 payload —— 没有复制判定逻辑。

def _reset_probe_state():
    """隔离模块级的连续超时计数；对修复前的代码（无该字段）也是安全的。"""
    misses = getattr(app, "_mods_misses", None)
    if isinstance(misses, dict):
        misses.clear()


def _post_mods(query=""):
    return app.app.test_client().post(
        "/api/device/%s/mods%s" % (DEV, query)).get_json()


def _send_simulating_probe(sent, answer_csv=None):
    """替换 app.send：answer_csv 为 None = 设备不回 MODS:（一次探测超时）。"""
    def _send(dev_id, cmd):
        if sent and answer_csv is not None:
            app._on_line(dev_id, "MODS:" + answer_csv)
        return {"sent": sent, "via": ["ble"]}
    return _send


def _call_endpoint(sent, answer_csv=None, query=""):
    _device()
    _reset_probe_state()
    original_send = app.send
    app.send = _send_simulating_probe(sent, answer_csv)
    try:
        payload = _post_mods(query)
    finally:
        app.send = original_send
    return payload, app._config["devices"][DEV]


def test_single_probe_timeout_does_not_cache_unsupported():
    """验收 1：一次 1.5s 丢包不能把支持修饰键的新固件永久判成「不支持」。

    修复前：sent=True 且未回 MODS: → cfg.mods_supported=False、supported=false；
    修复后：单次超时只算「未定论」，保持 unknown（None）以便下次重探。
    """
    payload, cfg = _call_endpoint(sent=True, answer_csv=None)
    assert payload["sent"] is True
    assert cfg.get("mods_supported") is not False, \
        "一次探测超时不得写死 mods_supported=False"
    assert payload.get("supported") is None, \
        "单次超时必须是三态里的 unknown（None），不是 False"
    assert payload.get("retryable") is True, \
        "结论未定时要告诉前端可重新检测"


def test_probe_timeout_never_counts_as_supported():
    """超时绝不能当 supported=True，否则旧固件会被写入 CFG:MODS 行。"""
    _payload, cfg = _call_endpoint(sent=True, answer_csv=None)
    assert cfg.get("mods_supported") is not True


def test_reprobe_after_timeout_recovers_supported():
    """验收 1 第二分句：超时后重探，设备回了 MODS: 即恢复 True。"""
    _device()
    _reset_probe_state()
    original_send = app.send
    try:
        # 第一次：一次丢包（未定论）。
        app.send = _send_simulating_probe(True, None)
        first = _post_mods()
        assert first.get("supported") is None
        # 第二次：设备这次回了 MODS:（新固件）。
        app.send = _send_simulating_probe(True, MODS_12)
        second = _post_mods()
    finally:
        app.send = original_send
    assert second["supported"] is True
    assert app._config["devices"][DEV].get("mods_supported") is True


def test_old_firmware_two_timeouts_still_concludes_unsupported():
    """验收 3：从不回 MODS: 的旧固件，连续多次超时后仍判 False。"""
    _device()
    _reset_probe_state()
    original_send = app.send
    app.send = _send_simulating_probe(True, None)
    try:
        _post_mods()
        _post_mods()
        payload = _post_mods()
    finally:
        app.send = original_send
    assert payload["supported"] is False, \
        "旧固件最终仍须判不支持，apply_device 才会继续跳过 CFG:MODS"
    assert app._config["devices"][DEV].get("mods_supported") is False


def test_link_down_is_not_a_capability_verdict():
    """链路不通（sent=False）也是 unknown，不能据此缓存任何 False/True。"""
    payload, cfg = _call_endpoint(sent=False)
    assert payload["sent"] is False
    assert payload.get("supported") is None
    assert cfg.get("mods_supported") is not False
    assert cfg.get("mods_supported") is not True


def test_redetect_reset_clears_cached_unsupported():
    """抽屉「重新检测」：?reset=1 清掉缓存结论与计数，设备应答后恢复 True。"""
    cfg = _device(supported=False)
    _reset_probe_state()
    original_send = app.send
    app.send = _send_simulating_probe(True, MODS_12)
    try:
        payload = _post_mods("?reset=1")
    finally:
        app.send = original_send
    assert payload["supported"] is True
    assert cfg.get("mods_supported") is True
