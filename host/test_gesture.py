"""双击第 10 个输入进配网可关（WS-672）的上位机回归测试。

Why this file exists
--------------------
WS-672 给「双击第 10 个输入 → 重启进配网」加了一个设备侧开关：``CFG:GESTURE:0|1``
写入（NVS ``gestureEnabled``，缺省开）、``CFG:GESTURE`` 只读探针回
``GESTURE:<0|1>``、``CMD:GET_CFG`` 末尾追加 ``CFG_GESTURE:<0|1>``。上位机在
「按键布局」卡多一行开关，并复用 WS-546 / WS-550 / WS-626 的三态能力探测：
旧固件不回 ``GESTURE:`` 时置灰、一条 ``CFG:GESTURE`` 都不发。

本文件钉住最容易坏的五处：

1. ``apply_device`` 必须拼出 ``CFG:GESTURE:0`` / ``CFG:GESTURE:1``，追在
   ``CFG:ENCDIR:`` 之后，并纳入逐项回读；
2. ``CFG_GESTURE:`` 回读必须写进 ``cfg["gesture_enabled"]``（并标记支持）；
3. 旧固件（无 ``GESTURE:`` 应答）→ 连续两次探测判不支持，控件禁用，且
   ``apply_device`` 一条 ``CFG:GESTURE:`` 都不发、不产生「未回报字段」噪音；
4. 非法回读值（``2`` / 空 / 非数字 / 越界）整条拒绝，不改本地状态；
5. 12 键布局同样有第 10 个输入 → 开关必须仍可用（不能像 ENCDIR 那样按布局禁用）。

这些测试 import 生产函数（``app.apply_device`` / ``app._apply_cfg_field`` /
``app._on_line`` / ``app.probe_gesture`` / ``app.gesture_applicable`` /
``app.api_device_gesture`` / ``app.new_device_cfg``），不在测试里复制行拼装、
解析或三态判定逻辑：把 ``host/app.py`` 里的实现改坏，本文件就会失败。

修复前：这些生产符号都不存在（``gesture_applicable`` / ``probe_gesture`` /
``api_device_gesture`` 不存在，``_apply_cfg_field`` 不认 ``gesture``，
``apply_device`` 不发 ``CFG:GESTURE:``），本文件全部失败；修复后全部通过。

Run::

    cd host && python -m pytest test_gesture.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


DEV = "t-gesture"
C3_PINS = [0, 1, 2, 3, 4, 5, 6, 7, 8]


def _reset_state():
    """用内存配置替换存档并 stub 掉 save_config，绝不碰真实 config.json。"""
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    app.save_config = lambda: None
    app._events.clear()
    for name in ("_gesture_misses", "_encdir_misses"):
        table = getattr(app, name, None)
        if isinstance(table, dict):
            table.clear()


def _device(layout=app.LAYOUT_ENCODER, supported=None, enabled=True):
    """注册一台内存设备；返回它的配置。"""
    _reset_state()
    cfg = app.new_device_cfg("C3", DEV)
    cfg["layout"] = layout
    cfg["layout_supported"] = True     # 跳过 12 键布局的 probe_layout 分支
    cfg["gesture_enabled"] = enabled
    if supported is not None:
        cfg["gesture_supported"] = supported
    app._config["devices"][DEV] = cfg
    return cfg


def _fake_send(sent, reported):
    """替换 ``app.send`` 的假设备：ACK 每条 CFG、应答 GET_CFG 并回显已接受值。

    ``reported['gesture']`` 为 None 表示「旧固件」——探针 ``CFG:GESTURE`` 沉默，
    GET_CFG 也不带 ``CFG_GESTURE:`` 尾行（这正是三态探测判不支持的依据）。
    """

    def _send(dev_id, cmd):
        sent.append(cmd)
        # 只读探针（没有冒号），必须在 CFG:* 写入分支之前处理。
        if cmd == "CFG:GESTURE":
            if reported.get("gesture") is not None:
                app._on_line(dev_id, "GESTURE:" + reported["gesture"])
            return {"sent": True, "via": ["ble"]}
        if cmd.startswith("CFG:KEYS:"):
            reported["keys"] = cmd[len("CFG:KEYS:"):]
        if cmd.startswith("CFG:TZ:"):
            reported["tz"] = cmd[len("CFG:TZ:"):]
        if cmd.startswith("CFG:MODS:"):
            reported["mods"] = cmd[len("CFG:MODS:"):]
        if cmd.startswith("CFG:ENCDIR:"):
            reported["encdir"] = cmd[len("CFG:ENCDIR:"):]
        if cmd.startswith("CFG:GESTURE:"):
            reported["gesture"] = cmd[len("CFG:GESTURE:"):]
        if cmd.startswith("CFG:LAYOUT:"):
            reported["layout"] = cmd[len("CFG:LAYOUT:"):]
        if cmd == "CMD:GET_CFG":
            app._on_line(dev_id, "CFG_KEYS:" + reported["keys"])
            app._on_line(dev_id, "CFG_PINS:" + ",".join(str(p) for p in C3_PINS))
            app._on_line(dev_id, "CFG_ENC:0")
            app._on_line(dev_id, "CFG_SLEEP:1,5")
            app._on_line(dev_id, "CFG_TZ:" + reported.get("tz", ""))
            app._on_line(dev_id, "CFG_LAYOUT:" + reported.get("layout", "1"))
            if reported.get("mods") is not None:
                app._on_line(dev_id, "CFG_MODS:" + reported["mods"])
            if reported.get("encdir") is not None:
                app._on_line(dev_id, "CFG_ENCDIR:" + reported["encdir"])
            if reported.get("gesture") is not None:
                app._on_line(dev_id, "CFG_GESTURE:" + reported["gesture"])
            return {"sent": True, "via": ["ble"]}
        app._on_line(dev_id, "ACK:" + cmd)
        return {"sent": True, "via": ["ble"]}

    return _send


def _reported(gesture="1"):
    return {"keys": "", "tz": "", "mods": None, "encdir": None,
            "gesture": gesture, "layout": "1"}


def _apply(enabled=True, supported=True, layout=app.LAYOUT_ENCODER,
           reported_gesture="1"):
    """用假设备跑一次真实 ``apply_device``，返回 (发出的命令, 结果)。"""
    _device(layout=layout, supported=supported, enabled=enabled)
    sent = []
    reported = _reported(reported_gesture)
    original_send = app.send
    app.send = _fake_send(sent, reported)
    try:
        result = app.apply_device(DEV)
    finally:
        app.send = original_send
    return sent, result


def _apply_existing():
    """在当前已存配置（不改它）上跑一次 ``apply_device``。"""
    sent = []
    reported = _reported()
    original_send = app.send
    app.send = _fake_send(sent, reported)
    try:
        result = app.apply_device(DEV)
    finally:
        app.send = original_send
    return sent, result


# --- 1. 下发：CFG:GESTURE:0|1 行拼装 + 逐项回读 ---------------------------

def test_apply_builds_cfg_gesture_line_for_both_values():
    for value, expected in ((False, "CFG:GESTURE:0"), (True, "CFG:GESTURE:1")):
        sent, result = _apply(enabled=value)
        lines = [c for c in sent if c.startswith("CFG:GESTURE:")]
        assert lines == [expected], "开关必须拼出恰好一条 CFG:GESTURE:0|1"

        item = next((i for i in result["items"] if i["field"] == "gesture"), None)
        assert item is not None, "CFG:GESTURE 必须出现在逐项校验里"
        assert item["status"] == "ok", item
        assert item["expected"] == item["actual"] == expected[len("CFG:GESTURE:"):]


def test_apply_appends_cfg_gesture_last_after_encdir():
    """新行只加最后：CFG:GESTURE 排在 CFG:ENCDIR 之后，后面只剩 CMD:GET_CFG。"""
    sent, _result = _apply(enabled=False)
    # 让 encdir 也下发，才能比较先后（默认 0 在「未定论」时会被跳过）。
    cfg = app._config["devices"][DEV]
    cfg["encoder"]["inverted"] = True
    cfg["encdir_supported"] = True
    sent, _result = _apply_existing()
    gesture_at = next(i for i, c in enumerate(sent) if c.startswith("CFG:GESTURE:"))
    encdir_at = next(i for i, c in enumerate(sent) if c.startswith("CFG:ENCDIR:"))
    assert gesture_at > encdir_at, "CFG:GESTURE 必须追加在 CFG:ENCDIR 之后（末尾）"
    assert sent[gesture_at + 1] == "CMD:GET_CFG", "CFG:GESTURE 之后只允许 CMD:GET_CFG"


# --- 2. 回读：CFG_GESTURE: 写入 cfg[gesture_enabled] ----------------------

def test_cfg_gesture_readback_writes_enabled():
    cfg = app.new_device_cfg("C3", DEV)
    assert app._apply_cfg_field(cfg, "gesture", "1") is True
    assert cfg["gesture_enabled"] is True
    assert cfg["gesture_supported"] is True, "回了 CFG_GESTURE 就是支持该开关"
    assert app._apply_cfg_field(cfg, "gesture", "0") is True
    assert cfg["gesture_enabled"] is False

    # 真实入口：CMD:GET_CFG 的尾行 CFG_GESTURE 落到已存配置上。
    _device()
    app._on_line(DEV, "CFG_GESTURE:0")
    stored = app._config["devices"][DEV]
    assert stored["gesture_enabled"] is False
    assert stored["gesture_supported"] is True


# --- 3. 旧固件：三态判不支持 + 不下发 --------------------------------------

def _post_gesture(query=""):
    return app.app.test_client().post(
        "/api/device/%s/gesture%s" % (DEV, query)).get_json()


def test_probe_gesture_reports_supported_and_silent():
    """能力探测：答 GESTURE: = 支持；沉默 = 未应答（都走生产 probe_gesture）。"""
    _device()

    def answering(dev_id, cmd):
        app._on_line(dev_id, "GESTURE:1")
        return {"sent": True, "via": ["ble"]}

    original = app.send
    app.send = answering
    try:
        probe = app.probe_gesture(DEV, timeout=0.05)
    finally:
        app.send = original
    assert probe["answered"] is True
    assert probe["gesture"] == "1"

    _device()
    app.send = lambda dev_id, cmd: {"sent": True, "via": ["ble"]}
    try:
        probe = app.probe_gesture(DEV, timeout=0.05)
    finally:
        app.send = original
    assert probe["answered"] is False
    assert probe["gesture"] is None


def test_old_firmware_two_silent_probes_conclude_unsupported_and_apply_skips():
    """WS-550/672：一次丢包仍 unknown，连续两次才判不支持；apply 不下发。"""
    _device()
    original_send = app.send
    # 旧固件：链路通、但从不回 GESTURE:（只会有通用 ACK:）。
    app.send = lambda dev_id, cmd: {"sent": True, "via": ["ble"]}
    try:
        first = _post_gesture()
        assert first["sent"] is True
        assert first["supported"] is None, "单次超时是 unknown，不是 unsupported"
        second = _post_gesture()
    finally:
        app.send = original_send
    assert second["supported"] is False, "连续两次沉默 → 判不支持"

    cfg = app._config["devices"][DEV]
    assert cfg["gesture_supported"] is False
    assert app.gesture_applicable(cfg) is False, "旧固件时控件必须禁用"

    # 已判不支持的同一台设备：apply 一条 CFG:GESTURE 都不发，且逐项校验里
    # 不出现该字段（不产生「设备未回报」噪音）。
    sent, result = _apply_existing()
    assert not [c for c in sent if c.startswith("CFG:GESTURE:")], \
        "能力探测判不支持时，一条 CFG:GESTURE 都不能发"
    assert not [i for i in result["items"] if i["field"] == "gesture"], \
        "没发就不该出现在逐项校验里"


# --- 4. 非法回读：整条拒绝、不改本地状态 ----------------------------------

def test_invalid_gesture_readback_is_rejected_and_keeps_local_state():
    cfg = app.new_device_cfg("C3", DEV)
    assert app._apply_cfg_field(cfg, "gesture", "0") is True
    before_enabled = cfg["gesture_enabled"]
    before_supported = cfg.get("gesture_supported")

    for bad in ("2", "", "x", "-1", "10", " 3 ", "0.5", "01"):
        assert app._apply_cfg_field(cfg, "gesture", bad) is False, \
            "非法值 %r 必须整条拒绝" % bad

    assert cfg["gesture_enabled"] == before_enabled, \
        "被拒绝的回读不能改动本地开关值"
    assert cfg.get("gesture_supported") == before_supported


# --- 5. 12 键布局：同样有第 10 个输入，开关必须仍可用 ----------------------

def test_gesture_applicable_ignores_layout():
    for layout in (app.LAYOUT_ENCODER, app.LAYOUT_12KEY):
        for verdict in (True, None):
            assert app.gesture_applicable(
                _device(layout=layout, supported=verdict)) is True, \
                "12 键布局同样有第 10 个输入，不能被按布局禁用"
    assert app.gesture_applicable(
        _device(layout=app.LAYOUT_12KEY, supported=False)) is False


def test_12key_layout_still_sends_gesture_and_keeps_presses_as_keys():
    """12 键 + 开关关：下发 CFG:GESTURE:0（第 10 键的两次按下仍由固件发键）。"""
    sent, result = _apply(enabled=False, layout=app.LAYOUT_12KEY)
    assert "CFG:GESTURE:0" in sent
    item = next((i for i in result["items"] if i["field"] == "gesture"), None)
    assert item is not None and item["status"] == "ok", item


# --- 6. 默认值：新设备与旧存档都按「开」 ----------------------------------

def test_default_is_enabled_for_new_and_legacy_configs():
    cfg = app.new_device_cfg("C3", DEV)
    assert cfg["gesture_enabled"] is True, "新设备默认开，与固件缺省一致"
    assert cfg["gesture_supported"] is None, "未探测前是未定论"

    # 旧 config.json（没有 gesture_* 键）读进来也必须按开处理。
    _reset_state()
    app._config["devices"][DEV] = {
        "name": DEV, "chip": "C3", "layout": app.LAYOUT_ENCODER,
        "pins": list(C3_PINS), "keys": [], "encoder": {"mode": 0, "inverted": False},
        "sleep": {"enabled": True, "minutes": 5}, "tz": "",
    }
    legacy = app.device_cfg(DEV)
    assert legacy["gesture_enabled"] is True
    assert app.gesture_applicable(legacy) is True
