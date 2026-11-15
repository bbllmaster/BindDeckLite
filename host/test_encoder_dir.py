"""编码器旋转方向反转（WS-626）的上位机回归测试。

Why this file exists
--------------------
WS-626 给编码器加了「旋转方向反转」：设备侧布尔配置 ``encInverted``（NVS），
通过 ``CFG:ENCDIR:0|1`` 写入 / ``CFG:ENCDIR`` 探测 / ``CFG_ENCDIR:`` 回读；
上位机编码器页多一行开关，并复用 WS-546 / WS-550 的三态能力探测，旧固件时
置灰且 ``apply`` 不下发。改动最容易坏在这五处，本文件逐条钉住：

1. ``/api/apply`` 必须拼出 ``CFG:ENCDIR:0`` / ``CFG:ENCDIR:1``，追加在所有
   既有命令（含 ``CFG:MODS:``）之后，并纳入逐项回读；
2. ``CFG_ENCDIR:`` 回读必须写进 ``cfg.encoder.inverted``（并标记支持）；
3. 旧固件（无 ``ENCDIR:`` 应答）→ 连续两次探测判不支持，控件禁用，且
   ``apply`` 一条 ``CFG:ENCDIR:`` 都不发、不产生「未回报字段」噪音；
4. 非法回读值（``2`` / 空 / 非数字 / 越界）整条拒绝，不改本地状态；
5. 12 键布局没有编码器：控件禁用判定，反转值仍保留。

这些测试 import 生产函数（``app.apply_device`` / ``app._apply_cfg_field`` /
``app._on_line`` / ``app.probe_encdir`` / ``app.encdir_applicable`` /
``app.api_device_encdir`` / ``app.new_device_cfg``），不在测试里复制行拼装、
解析或三态判定逻辑：把 ``host/app.py`` 里的实现改坏，本文件就会失败。

修复前：上面这些生产符号都不存在（``encdir_applicable`` / ``probe_encdir`` /
``api_device_encdir`` 不存在，``_apply_cfg_field`` 不认 ``encdir``，
``apply_device`` 不发 ``CFG:ENCDIR:``），本文件全部失败；修复后全部通过。

Run::

    cd host && python -m pytest test_encoder_dir.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


DEV = "t-encdir"
C3_PINS = [0, 1, 2, 3, 4, 5, 6, 7, 8]


def _reset_state():
    """用内存配置替换存档并 stub 掉 save_config，绝不碰真实 config.json。"""
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    app.save_config = lambda: None
    app._events.clear()
    # 模块级「连续超时计数」要按用例隔离；修复前没有该字段，getattr 安全。
    misses = getattr(app, "_encdir_misses", None)
    if isinstance(misses, dict):
        misses.clear()


def _device(layout=app.LAYOUT_ENCODER, supported=None, inverted=False):
    """注册一台内存设备；返回它的配置。"""
    _reset_state()
    cfg = app.new_device_cfg("C3", DEV)
    cfg["layout"] = layout
    cfg["layout_supported"] = True     # 跳过 12 键布局的 probe_layout 分支
    cfg["encoder"]["inverted"] = inverted
    if supported is not None:
        cfg["encdir_supported"] = supported
    app._config["devices"][DEV] = cfg
    return cfg


def _fake_send(sent, reported):
    """替换 ``app.send`` 的假设备：ACK 每条 CFG、应答 GET_CFG 并回显已接受值。

    ``reported['encdir']`` 为 None 表示「旧固件」——GET_CFG 不带
    ``CFG_ENCDIR:`` 尾行（这正是三态探测判不支持的依据）。
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
            return {"sent": True, "via": ["ble"]}
        app._on_line(dev_id, "ACK:" + cmd)
        return {"sent": True, "via": ["ble"]}

    return _send


def _apply(supported=None, inverted=False, mods_supported=None):
    """用假设备跑一次真实 ``apply_device``，返回 (发出的命令, 结果)。"""
    cfg = _device(supported=supported, inverted=inverted)
    if mods_supported is not None:
        cfg["mods_supported"] = mods_supported
    sent = []
    reported = {"keys": "", "tz": "", "mods": None, "encdir": None}
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
    reported = {"keys": "", "tz": "", "mods": None, "encdir": None}
    original_send = app.send
    app.send = _fake_send(sent, reported)
    try:
        result = app.apply_device(DEV)
    finally:
        app.send = original_send
    return sent, result


# --- 1. 下发：CFG:ENCDIR:0|1 行拼装 + 逐项回读 ---------------------------

def test_apply_builds_cfg_encdir_line_for_both_values():
    for value, expected in ((False, "CFG:ENCDIR:0"), (True, "CFG:ENCDIR:1")):
        sent, result = _apply(supported=True, inverted=value)
        lines = [c for c in sent if c.startswith("CFG:ENCDIR:")]
        assert lines == [expected], "反转开关必须拼出恰好一条 CFG:ENCDIR:0|1"

        # 该行参与逐项回读：设备回显同值时状态为 ok。
        item = next((i for i in result["items"] if i["field"] == "encdir"), None)
        assert item is not None, "CFG:ENCDIR 必须出现在逐项校验里"
        assert item["status"] == "ok", item
        assert item["expected"] == item["actual"] == expected[len("CFG:ENCDIR:"):]


def test_apply_appends_cfg_encdir_last_after_mods():
    """新行只加最后：CFG:ENCDIR 排在 CFG:MODS（若适用）之后。"""
    sent, _result = _apply(supported=True, inverted=True, mods_supported=True)
    encdir_at = next(i for i, c in enumerate(sent) if c.startswith("CFG:ENCDIR:"))
    mods_at = next(i for i, c in enumerate(sent) if c.startswith("CFG:MODS:"))
    assert encdir_at > mods_at, "CFG:ENCDIR 必须追加在 CFG:MODS 之后（末尾）"
    assert encdir_at == len(sent) - 2, "CFG:ENCDIR 之后只剩 CMD:GET_CFG"


# --- 2. 回读：CFG_ENCDIR: 写入 cfg.encoder.inverted ------------------------

def test_cfg_encdir_readback_writes_inverted():
    # 直接调用生产解析函数。
    cfg = app.new_device_cfg("C3", DEV)
    assert app._apply_cfg_field(cfg, "encdir", "1") is True
    assert cfg["encoder"]["inverted"] is True
    assert cfg["encdir_supported"] is True, "回了 CFG_ENCDIR 就是支持反转"
    assert app._apply_cfg_field(cfg, "encdir", "0") is True
    assert cfg["encoder"]["inverted"] is False

    # 真实入口：CMD:GET_CFG 的尾行 CFG_ENCDIR 落到已存配置上。
    _device()
    app._on_line(DEV, "CFG_ENCDIR:1")
    stored = app._config["devices"][DEV]
    assert stored["encoder"]["inverted"] is True
    assert stored["encdir_supported"] is True


# --- 3. 旧固件：三态判不支持 + 不下发 --------------------------------------

def _post_encdir(query=""):
    return app.app.test_client().post(
        "/api/device/%s/encdir%s" % (DEV, query)).get_json()


def test_probe_encdir_reports_supported_and_silent():
    """能力探测：答 ENCDIR: = 支持；沉默 = 未应答（都走生产 probe_encdir）。"""
    _device()

    def answering(dev_id, cmd):
        app._on_line(dev_id, "ENCDIR:1")
        return {"sent": True, "via": ["ble"]}

    original = app.send
    app.send = answering
    try:
        probe = app.probe_encdir(DEV, timeout=0.05)
    finally:
        app.send = original
    assert probe["answered"] is True
    assert probe["encdir"] == "1"

    _device()
    app.send = lambda dev_id, cmd: {"sent": True, "via": ["ble"]}
    try:
        probe = app.probe_encdir(DEV, timeout=0.05)
    finally:
        app.send = original
    assert probe["answered"] is False
    assert probe["encdir"] is None


def test_old_firmware_two_silent_probes_conclude_unsupported_and_apply_skips():
    """WS-550/626：一次丢包仍 unknown，连续两次才判不支持；apply 不下发。"""
    _device()
    original_send = app.send
    # 旧固件：链路通、但从不回 ENCDIR:（只会有通用 ACK:）。
    app.send = lambda dev_id, cmd: {"sent": True, "via": ["ble"]}
    try:
        first = _post_encdir()
        assert first["sent"] is True
        assert first["supported"] is None, "单次超时是 unknown，不是 unsupported"
        second = _post_encdir()
    finally:
        app.send = original_send
    assert second["supported"] is False, "连续两次沉默 → 判不支持"

    cfg = app._config["devices"][DEV]
    assert cfg["encdir_supported"] is False
    assert app.encdir_applicable(cfg) is False, "旧固件时控件必须禁用"

    # 已判不支持的同一台设备：apply 一条 CFG:ENCDIR 都不发，且逐项校验里
    # 不出现该字段（不产生「设备未回报」噪音）。
    sent, result = _apply_existing()
    assert not [c for c in sent if c.startswith("CFG:ENCDIR:")], \
        "能力探测判不支持时，一条 CFG:ENCDIR 都不能发"
    assert not [i for i in result["items"] if i["field"] == "encdir"], \
        "没发就不该出现在逐项校验里"


# --- 4. 非法回读：整条拒绝、不改本地状态 ----------------------------------

def test_invalid_encdir_readback_is_rejected_and_keeps_local_state():
    cfg = app.new_device_cfg("C3", DEV)
    assert app._apply_cfg_field(cfg, "encdir", "1") is True
    before_inverted = cfg["encoder"]["inverted"]
    before_supported = cfg.get("encdir_supported")

    for bad in ("2", "", "x", "-1", "10", " 3 ", "0.5"):
        assert app._apply_cfg_field(cfg, "encdir", bad) is False, \
            "非法值 %r 必须整条拒绝" % bad

    assert cfg["encoder"]["inverted"] == before_inverted, \
        "被拒绝的回读不能改动本地反转值"
    assert cfg.get("encdir_supported") == before_supported


# --- 5. 12 键布局：禁用判定 + 值保留 --------------------------------------

def test_encdir_disabled_for_12key_layout_and_value_is_kept():
    # 即使固件已确认支持、且用户设了反转，12 键布局也必须禁用（无编码器）。
    cfg = _device(layout=app.LAYOUT_12KEY, supported=True, inverted=True)
    assert app.encdir_applicable(cfg) is False
    assert cfg["encoder"]["inverted"] is True, "切布局不能把反转值丢掉"
    # 旧固件 + 12 键同样禁用。
    assert app.encdir_applicable(
        _device(layout=app.LAYOUT_12KEY, supported=False)) is False


def test_encdir_applicable_true_for_supported_encoder_deck():
    # 编码器布局：supported / unknown 都可用；只有 unsupported 禁用。
    for verdict in (True, None):
        assert app.encdir_applicable(
            _device(layout=app.LAYOUT_ENCODER, supported=verdict)) is True
    assert app.encdir_applicable(
        _device(layout=app.LAYOUT_ENCODER, supported=False)) is False
