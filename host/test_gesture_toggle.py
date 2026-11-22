"""双击第 10 个输入进配网可关（WS-672）的上位机回归测试。

Why this file exists
--------------------
WS-672 给固件加了「双击进配网」开关：设备侧布尔配置 ``gestureEnabled``
（NVS），通过 ``CFG:GESTURE:0|1`` 写入 / ``CFG:GESTURE`` 探测 /
``CFG_GESTURE:`` 回读；上位机设备页「按键布局」卡多一行开关，复用
WS-550 / WS-626 的三态能力探测，旧固件时置灰且 ``apply`` 不下发。
改动最容易坏在这六处，本文件逐条钉住：

1. 新设备默认「开」——与 1.3.13 及更早的行为完全一致；
2. ``/api/apply`` 拼出 ``CFG:GESTURE:0`` / ``CFG:GESTURE:1``，追加在所有
   既有命令（含 ``CFG:ENCDIR:``）之后，并纳入逐项回读；
3. ``CFG_GESTURE:`` 回读写进 ``cfg.gesture``（并标记支持）；
4. 旧固件（无 ``GESTURE:`` 应答）→ 连续两次探测判不支持，控件禁用，且
   ``apply`` 一条 ``CFG:GESTURE:`` 都不发、不产生「未回报字段」噪音；
5. 结论未定时只发「关闭」（0），默认的 1 不发给未知固件（不制造假行）；
6. 非法回读值（``2`` / 空 / 非数字 / 越界）整条拒绝，不改本地状态。

这些测试 import 生产函数（``app.apply_device`` / ``app._apply_cfg_field`` /
``app._on_line`` / ``app.probe_gesture`` / ``app.new_device_cfg``），不在测试
里复制行拼装、解析或三态判定逻辑：把 ``host/app.py`` 里的实现改坏，本文件就
会失败。

修复前：上面这些生产符号都不存在（``probe_gesture`` / ``/gesture`` 路由
不存在，``_apply_cfg_field`` 不认 ``gesture``，``apply_device`` 不发
``CFG:GESTURE:``，``new_device_cfg`` 没有 ``gesture``），本文件全部失败；
修复后全部通过。

Run::

    cd host && python -m pytest test_gesture_toggle.py -q
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
    misses = getattr(app, "_gesture_misses", None)
    if isinstance(misses, dict):
        misses.clear()


def _device(supported=None, gesture=True):
    """注册一台内存设备；返回它的配置。"""
    _reset_state()
    cfg = app.new_device_cfg("C3", DEV)
    cfg["layout"] = app.LAYOUT_ENCODER
    cfg["layout_supported"] = True     # 跳过 12 键布局的 probe_layout 分支
    cfg["encdir_supported"] = True     # 让 CFG:ENCDIR 也发出来，用于顺序断言
    cfg["gesture"] = gesture
    if supported is not None:
        cfg["gesture_supported"] = supported
    app._config["devices"][DEV] = cfg
    return cfg


def _fake_send(sent, reported):
    """替换 ``app.send`` 的假设备：ACK 每条 CFG、应答 GET_CFG 并回显已接受值。

    ``reported['gesture']`` 为 None 表示「旧固件」——GET_CFG 不带
    ``CFG_GESTURE:`` 尾行（这正是三态探测判不支持的依据）。
    """

    def _send(dev_id, cmd):
        sent.append(cmd)
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
        if cmd == "CMD:GET_CFG":
            app._on_line(dev_id, "CFG_KEYS:" + reported["keys"])
            app._on_line(dev_id, "CFG_PINS:" + ",".join(str(p) for p in C3_PINS))
            app._on_line(dev_id, "CFG_ENC:0")
            app._on_line(dev_id, "CFG_SLEEP:1,5")
            app._on_line(dev_id, "CFG_TZ:" + reported.get("tz", ""))
            app._on_line(dev_id, "CFG_LAYOUT:1")
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


def _apply(supported=None, gesture=True):
    """用假设备跑一次真实 ``apply_device``，返回 (发出的命令, 结果)。"""
    _device(supported=supported, gesture=gesture)
    sent = []
    reported = {"keys": "", "tz": "", "mods": None, "encdir": "0",
                "gesture": None}
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
    reported = {"keys": "", "tz": "", "mods": None, "encdir": "0",
                "gesture": None}
    original_send = app.send
    app.send = _fake_send(sent, reported)
    try:
        result = app.apply_device(DEV)
    finally:
        app.send = original_send
    return sent, result


# --- 0. 默认值 -----------------------------------------------------------

def test_new_device_defaults_gesture_on():
    cfg = app.new_device_cfg("C3", DEV)
    assert cfg["gesture"] is True, "新设备默认必须允许双击进配网（与 1.3.13 一致）"


# --- 1. 下发：CFG:GESTURE:0|1 行拼装 + 逐项回读 ---------------------------

def test_apply_builds_cfg_gesture_line_for_both_values():
    for value, expected in ((False, "CFG:GESTURE:0"), (True, "CFG:GESTURE:1")):
        sent, result = _apply(supported=True, gesture=value)
        lines = [c for c in sent if c.startswith("CFG:GESTURE:")]
        assert lines == [expected], "开关必须拼出恰好一条 CFG:GESTURE:0|1"

        item = next((i for i in result["items"] if i["field"] == "gesture"), None)
        assert item is not None, "CFG:GESTURE 必须出现在逐项校验里"
        assert item["status"] == "ok", item
        assert item["expected"] == item["actual"] == expected[len("CFG:GESTURE:"):]


def test_apply_appends_cfg_gesture_last_after_encdir():
    """新行只加最后：CFG:GESTURE 排在 CFG:ENCDIR 之后。"""
    sent, _result = _apply(supported=True, gesture=False)
    gesture_at = next(i for i, c in enumerate(sent) if c.startswith("CFG:GESTURE:"))
    encdir_at = next(i for i, c in enumerate(sent) if c.startswith("CFG:ENCDIR:"))
    assert gesture_at > encdir_at, "CFG:GESTURE 必须追加在 CFG:ENCDIR 之后（末尾）"
    assert gesture_at == len(sent) - 2, "CFG:GESTURE 之后只剩 CMD:GET_CFG"


# --- 2. 回读：CFG_GESTURE: 写入 cfg.gesture --------------------------------

def test_cfg_gesture_readback_writes_state():
    # 直接调用生产解析函数。
    cfg = app.new_device_cfg("C3", DEV)
    assert app._apply_cfg_field(cfg, "gesture", "0") is True
    assert cfg["gesture"] is False
    assert cfg["gesture_supported"] is True, "回了 CFG_GESTURE 就是支持该开关"
    assert app._apply_cfg_field(cfg, "gesture", "1") is True
    assert cfg["gesture"] is True

    # 真实入口：CMD:GET_CFG 的尾行 CFG_GESTURE 落到已存配置上。
    _device()
    app._on_line(DEV, "CFG_GESTURE:0")
    stored = app._config["devices"][DEV]
    assert stored["gesture"] is False
    assert stored["gesture_supported"] is True


# --- 3. 旧固件：三态判不支持 + 不下发 --------------------------------------

def _post_gesture(query=""):
    return app.app.test_client().post(
        "/api/device/%s/gesture%s" % (DEV, query)).get_json()


def test_probe_gesture_reports_supported_and_silent():
    """能力探测：答 GESTURE: = 支持；沉默 = 未应答（都走生产 probe_gesture）。"""
    _device()

    def answering(dev_id, cmd):
        app._on_line(dev_id, "GESTURE:0")
        return {"sent": True, "via": ["ble"]}

    original = app.send
    app.send = answering
    try:
        probe = app.probe_gesture(DEV, timeout=0.05)
    finally:
        app.send = original
    assert probe["answered"] is True
    assert probe["gesture"] == "0"

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
    _device(supported=None, gesture=False)
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

    # 已判不支持的同一台设备：apply 一条 CFG:GESTURE 都不发，且逐项校验里
    # 不出现该字段（不产生「设备未回报」噪音），即使本地值是「关」。
    sent, result = _apply_existing()
    assert not [c for c in sent if c.startswith("CFG:GESTURE:")], \
        "能力探测判不支持时，一条 CFG:GESTURE 都不能发"
    assert not [i for i in result["items"] if i["field"] == "gesture"], \
        "没发就不该出现在逐项校验里"


def test_unknown_verdict_skips_default_on_but_sends_off():
    """结论未定时：默认「开」是空操作（不发），「关」才发——避免假行。"""
    sent, result = _apply(supported=None, gesture=True)
    assert not [c for c in sent if c.startswith("CFG:GESTURE:")]
    assert not [i for i in result["items"] if i["field"] == "gesture"]

    sent, result = _apply(supported=None, gesture=False)
    assert [c for c in sent if c.startswith("CFG:GESTURE:")] == ["CFG:GESTURE:0"]


# --- 4. 非法回读：整条拒绝、不改本地状态 ----------------------------------

def test_invalid_gesture_readback_is_rejected_and_keeps_local_state():
    cfg = app.new_device_cfg("C3", DEV)
    assert app._apply_cfg_field(cfg, "gesture", "0") is True
    before = cfg["gesture"]
    before_supported = cfg.get("gesture_supported")

    for bad in ("2", "", "x", "-1", "10", " 3 ", "0.5", "true"):
        assert app._apply_cfg_field(cfg, "gesture", bad) is False, \
            "非法值 %r 必须整条拒绝" % bad

    assert cfg["gesture"] == before, "被拒绝的回读不能改动本地开关值"
    assert cfg.get("gesture_supported") == before_supported
