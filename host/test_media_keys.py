"""按键媒体键（WS-661）的上位机 / 固件一致性回归测试。

Why this file exists
--------------------
WS-661 让一个输入可以直接发消费者控制键（播放/暂停、切歌、静音、音量 ±），
设备自己发、不需要上位机常驻。危险点不在功能本身，而在**旧固件**：旧版
``press(uint8_t)`` 会把媒体码 461 截成 ``0xCD`` 当键盘 usage 发出去，可能在
焦点窗口里打出字符。所以「先探测、再显示、再下发」是硬条件，本文件逐条钉住：

1. 键码表：7 项媒体键的 label 与 code（``256 + usage``）一一对应，哨兵 0 仍在
   最后（前端按下拉分组，未知键码不能显示成「不发键」）；
2. 下发：``media_supported is True`` 才把 ``>=256`` 的值写进 ``CFG:KEYS:``；
   结论为 False 时该输入以 0（「不发键」）发出，回读行显示「设备拒绝」，其它
   输入照常生效；
3. 结论未知时 ``apply_device()`` 先探一次 ``CFG:MEDIA``：设备回答就下发，沉默
   就过滤 —— 绝不把 ``>=256`` 的键码发给没确认过的设备；
4. 回读 ``CFG_KEYS:`` 里的媒体码保留 code 与中文标签，不被当成未知数字；
5. 导入动作备份：不支持的设备上媒体码不写入，该输入保留原值（不得静默改键）；
6. 固件侧同一份媒体表（``src/nimble_hid.h`` 的 ``MEDIA_USAGES``）与上位机表
   完全一致，且两处运行时防护（``CFG:KEYS`` 整条拒绝、未定义媒体码不发）
   仍然在 ``src/main.cpp`` 里。

这些测试 import 生产函数（``app.KEY_CODES`` / ``app.MEDIA_KEYS`` /
``app.apply_device`` / ``app.probe_media`` / ``app.filter_import_media`` /
``app._apply_cfg_field`` / ``app._on_line``），不复制键码表或行拼装逻辑：
把 ``host/app.py`` 或 ``src/nimble_hid.h`` 改坏，本文件就会失败。

Run::

    cd host && python -m pytest test_media_keys.py -q
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


DEV = "t-media"
C3_PINS = [0, 1, 2, 3, 4, 5, 6, 7, 8]
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMWARE_HEADER = os.path.join(REPO_ROOT, "src", "nimble_hid.h")
FIRMWARE_MAIN = os.path.join(REPO_ROOT, "src", "main.cpp")

# The seven usages, in the order the UI / docs list them.
MEDIA_USAGES = [0x00CD, 0x00B6, 0x00B5, 0x00B7, 0x00E2, 0x00E9, 0x00EA]
PLAY_PAUSE = 256 + 0x00CD          # 461
NEXT = 256 + 0x00B5                # 437


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
        cfg["media_supported"] = supported
    app._config["devices"][DEV] = cfg
    return cfg


def _fake_send(sent, reported):
    """Stand-in for ``app.send``: ACKs every CFG line and answers GET_CFG.

    *reported* is a mutable dict:
      * ``keys``        — the code csv the deck reports (echo of CFG:KEYS),
      * ``media_answer`` — the fake deck answers CFG:MEDIA with ``MEDIA:1``,
      * ``media_cfg_line`` — it also appends the ``CFG_MEDIA:1`` tail line.
    """

    def _send(dev_id, cmd):
        sent.append(cmd)
        if cmd.startswith("CFG:KEYS:"):
            reported["keys"] = cmd[len("CFG:KEYS:"):]
        if cmd.startswith("CFG:TZ:"):
            reported["tz"] = cmd[len("CFG:TZ:"):]
        if cmd == "CFG:MEDIA" and reported.get("media_answer"):
            app._on_line(dev_id, "MEDIA:1")
        if cmd == "CMD:GET_CFG":
            app._on_line(dev_id, "CFG_KEYS:" + reported["keys"])
            app._on_line(dev_id, "CFG_PINS:" + ",".join(str(p) for p in C3_PINS))
            app._on_line(dev_id, "CFG_ENC:0")
            app._on_line(dev_id, "CFG_SLEEP:1,5")
            app._on_line(dev_id, "CFG_TZ:" + reported.get("tz", ""))
            app._on_line(dev_id, "CFG_LAYOUT:%d" % reported.get("layout", 0))
            if reported.get("media_cfg_line"):
                app._on_line(dev_id, "CFG_MEDIA:1")
            return {"sent": True, "via": ["ble"]}
        app._on_line(dev_id, "ACK:" + cmd)
        return {"sent": True, "via": ["ble"]}

    return _send


def _apply_with(cfg, **reported):
    sent = []
    box = {"keys": ""}
    box.update(reported)
    original_send = app.send
    app.send = _fake_send(sent, box)
    try:
        result = app.apply_device(DEV)
    finally:
        app.send = original_send
    return sent, result, box


# --- 键码表 ---------------------------------------------------------------

def test_media_table_has_the_seven_documented_keys():
    media = [(lbl, code) for lbl, code in app.KEY_CODES if app.is_media_code(code)]
    assert [code for _lbl, code in media] == [
        app.MEDIA_CODE_BASE + usage for usage in MEDIA_USAGES], \
        "媒体键必须是 256 + usage，且七项顺序与文档一致"
    assert app.MEDIA_CODE_BASE == 256
    for label, _code in media:
        assert label, "每个媒体键都要有标签（前端/摘要/回读共用这一份）"
    assert app.MEDIA_KEYS[0][0] == app.t("keys.media_play_pause") == "播放/暂停"


def test_none_sentinel_stays_last_and_media_codes_are_not_keyboard_codes():
    codes = [code for _lbl, code in app.KEY_CODES]
    assert codes[-1] == 0, "「不发键」必须仍在末尾，否则未知键码会显示成它"
    assert codes.count(0) == 1
    # 媒体码不能落进 0..255 的键盘段，否则旧固件会把它当键盘 usage。
    assert all(code >= 256 for code in codes if app.is_media_code(code))
    assert not any(app.is_media_code(code) for code in codes if code <= 255)


# --- 固件同一份表 / 两处运行时防护 ----------------------------------------

def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_firmware_media_table_matches_the_host_table():
    header = _read(FIRMWARE_HEADER)
    assert "MEDIA_CODE_BASE = 256" in header
    block = re.search(r"MEDIA_USAGES\[\]\s*=\s*\{(.*?)\};", header, re.S)
    assert block, "src/nimble_hid.h 缺少 MEDIA_USAGES 表"
    usages = [int(m, 16) for m in re.findall(r"0x([0-9A-Fa-f]+)", block.group(1))]
    assert usages == MEDIA_USAGES, \
        "固件媒体表与上位机 MEDIA_KEYS 必须一致（改一处就要改另一处和文档）"
    host_usages = [usage for _lbl, usage in app.MEDIA_KEYS]
    assert host_usages == usages


def test_firmware_still_rejects_and_ignores_unknown_media_codes():
    main = _read(FIRMWARE_MAIN)
    # CFG:KEYS 校验：媒体码必须落在表内，否则整条拒绝。
    assert "mediaUsageForCode(v, nullptr)" in main, \
        "CFG:KEYS 的取值校验必须调用固件的媒体表"
    assert "[cfg] keys REJECTED (bad code" in main
    # 运行时兜底：NVS 里的未定义码不发，绝不截成 8 位键盘 usage。
    assert "[cfg] media code ignored" in main
    assert "mediaUsageForCode(code, &usage)" in main
    # 能力探测应答。
    assert 'sendToHost("MEDIA:1")' in main


# --- 下发：只有确认支持才写 >=256 ----------------------------------------

def test_apply_sends_media_code_when_the_deck_supports_it():
    cfg = _device(supported=True)
    cfg["keys"][0]["code"] = PLAY_PAUSE
    cfg["keys"][0]["label"] = app.t("keys.media_play_pause")

    sent, result, _box = _apply_with(cfg)

    keys_line = next(c for c in sent if c.startswith("CFG:KEYS:"))
    values = keys_line[len("CFG:KEYS:"):].split(",")
    assert len(values) == 12, "12 键布局仍是 12 个值"
    assert values[0] == str(PLAY_PAUSE), "支持媒体键时必须原样下发 461"
    assert values[1] == str(0xF1), "相邻输入不串位"

    item = next(i for i in result["items"] if i["field"] == "keys")
    assert item["status"] == "ok", item
    assert result["media_blocked"] == []


def test_apply_filters_media_codes_on_unsupported_firmware():
    cfg = _device(supported=False)
    cfg["keys"][0]["code"] = PLAY_PAUSE
    cfg["keys"][0]["label"] = app.t("keys.media_play_pause")

    sent, result, _box = _apply_with(cfg)

    keys_line = next(c for c in sent if c.startswith("CFG:KEYS:"))
    values = keys_line[len("CFG:KEYS:"):].split(",")
    assert values[0] == "0", "不支持时该输入以「不发键」发出，绝不改成别的键"
    assert all(int(v) < 256 for v in values), "一条 >=256 的键码都不能下发"
    assert not [c for c in sent if c.startswith("CFG:MEDIA")], \
        "已判定不支持时无需再探"

    item = next(i for i in result["items"] if i["field"] == "keys")
    assert item["status"] == "rejected", "回读行必须显示未生效"
    assert "设备拒绝" in item["reason"]
    assert result["ok"] is False
    assert result["media_blocked"] == [1], "报告第 1 个输入被过滤"
    # 其它输入照常生效。
    assert values[1] == str(0xF1)


def test_apply_probes_when_the_verdict_is_unknown_and_sends_on_answer():
    cfg = _device()                     # media_supported 未定论
    cfg["keys"][0]["code"] = PLAY_PAUSE
    cfg["keys"][0]["label"] = app.t("keys.media_play_pause")

    sent, result, _box = _apply_with(cfg, media_answer=True)

    assert "CFG:MEDIA" in sent, "结论未知时 apply 必须先探一次"
    keys_line = next(c for c in sent if c.startswith("CFG:KEYS:"))
    assert keys_line.split(":")[-1].split(",")[0] == str(PLAY_PAUSE)
    assert cfg["media_supported"] is True, "设备答复即能力事实，写回 cfg"
    assert result["media_blocked"] == []


def test_apply_never_writes_media_codes_when_the_probe_stays_silent():
    cfg = _device()                     # 未定论
    cfg["keys"][0]["code"] = NEXT

    sent, result, _box = _apply_with(cfg)      # 不回答 CFG:MEDIA

    assert "CFG:MEDIA" in sent
    keys_line = next(c for c in sent if c.startswith("CFG:KEYS:"))
    values = keys_line[len("CFG:KEYS:"):].split(",")
    assert values[0] == "0"
    assert all(int(v) < 256 for v in values)
    assert result["media_blocked"] == [1]
    assert cfg.get("media_supported") is None, \
        "一次沉默不构成「不支持」结论（两次才定论，见路由测试）"


def test_apply_keeps_the_layout_input_count_with_media_keys():
    """编码器布局 10 个值；12 键布局 12 个值，媒体键不改变个数。"""
    cfg = _device(supported=True, layout=app.LAYOUT_ENCODER)
    cfg["keys"][9]["code"] = PLAY_PAUSE
    sent, result, _box = _apply_with(cfg, layout=1)
    keys_line = next(c for c in sent if c.startswith("CFG:KEYS:"))
    values = keys_line[len("CFG:KEYS:"):].split(",")
    assert len(values) == 10
    assert values[9] == str(PLAY_PAUSE)
    assert result["ok"] is True


# --- 回读 ----------------------------------------------------------------

def test_keys_readback_keeps_the_media_code_and_its_chinese_label():
    cfg = app.new_device_cfg("C3", DEV)
    csv = ",".join(str(c) for c in [PLAY_PAUSE] + [0xF1 + i for i in range(11)])
    assert app._apply_cfg_field(cfg, "keys", csv) is True
    assert cfg["keys"][0]["code"] == PLAY_PAUSE
    assert cfg["keys"][0]["label"] == app.t("keys.media_play_pause"), \
        "回读不能把 461 显示成裸数字"
    assert cfg["keys"][1]["code"] == 0xF1


def test_cfg_media_tail_line_marks_the_deck_as_capable():
    cfg = _device()
    app._on_line(DEV, "CFG_MEDIA:1")
    stored = app._config["devices"][DEV]
    assert stored["media_supported"] is True, \
        "CMD:GET_CFG 的 CFG_MEDIA:1 尾行就是能力证据"
    assert cfg is stored


# --- 能力探测（三态） -----------------------------------------------------

def test_media_probe_reports_supported_when_the_deck_answers():
    _device()
    original_send = app.send

    def _send(dev_id, cmd):
        app._on_line(dev_id, "MEDIA:1")
        return {"sent": True, "via": ["ble"]}

    app.send = _send
    try:
        probe = app.probe_media(DEV, timeout=0.05)
    finally:
        app.send = original_send
    assert probe["sent"] is True
    assert probe["answered"] is True
    assert probe["value"] == "1"


def test_media_probe_reports_silence_when_the_deck_does_not_answer():
    _device()
    original_send = app.send
    app.send = lambda dev_id, cmd: {"sent": True, "via": ["ble"]}
    try:
        probe = app.probe_media(DEV, timeout=0.05)
    finally:
        app.send = original_send
    assert probe["sent"] is True
    assert probe["answered"] is False


def test_media_route_needs_two_silent_probes_before_unsupported():
    """三态：单次超时不是结论，连续两次才判「旧固件」。"""
    _device()
    client = app.app.test_client()
    original_send, original_probe = app.send, app.probe_media
    app.send = lambda dev_id, cmd: {"sent": True, "via": ["ble"]}
    # 保留真实 probe_media，只把等待时间调小，避免每次测试白等 1.5 s。
    app.probe_media = lambda dev_id, timeout=0.05: original_probe(dev_id, timeout=timeout)
    try:
        first = client.post("/api/device/%s/media" % DEV).get_json()
        assert first["supported"] is None, "单次丢包不能直接判不支持"
        assert first["reason"] == "probe_timeout"
        second = client.post("/api/device/%s/media" % DEV).get_json()
        assert second["supported"] is False
        assert second["reason"] == "unsupported"
    finally:
        app.send, app.probe_media = original_send, original_probe
    assert app._config["devices"][DEV]["media_supported"] is False


def test_media_route_upgrades_on_answer_and_clears_the_miss_streak():
    _device()
    client = app.app.test_client()
    original_send, original_probe = app.send, app.probe_media

    def _send(dev_id, cmd):
        app._on_line(dev_id, "MEDIA:1")
        return {"sent": True, "via": ["ble"]}

    app.send = _send
    app.probe_media = lambda dev_id, timeout=0.05: original_probe(dev_id, timeout=timeout)
    try:
        r = client.post("/api/device/%s/media" % DEV).get_json()
    finally:
        app.send, app.probe_media = original_send, original_probe
    assert r["supported"] is True
    assert r["reason"] == "supported"
    assert DEV not in app._media_misses


def test_media_route_link_down_is_not_a_verdict():
    _device()
    client = app.app.test_client()
    original_send = app.send
    app.send = lambda dev_id, cmd: {"sent": False, "via": []}
    try:
        r = client.post("/api/device/%s/media" % DEV).get_json()
    finally:
        app.send = original_send
    assert r["supported"] is None
    assert r["reason"] == "link_down"


# --- 导入动作备份 ---------------------------------------------------------

def _validated_import(codes):
    return {
        "inputs": [
            {"index": i, "code": code, "mods": 0,
             "action": {"type": "none", "value": "", "macro": []}}
            for i, code in enumerate(codes)
        ],
        "encoder": {},
    }


def test_import_drops_media_codes_on_unsupported_firmware_and_keeps_the_old_value():
    cfg = _device(supported=False)
    original = cfg["keys"][0]["code"]
    validated = _validated_import([PLAY_PAUSE, 0xF2])

    filtered, rejected = app.filter_import_media(cfg, validated)
    assert rejected == [0]
    assert [e["index"] for e in filtered["inputs"]] == [1]

    app.merge_actions_import(cfg, filtered)
    assert cfg["keys"][0]["code"] == original, \
        "被拒绝的输入必须保留原值，不得静默改成别的键"
    assert cfg["keys"][1]["code"] == 0xF2, "其余输入照常导入"


def test_import_keeps_media_codes_when_supported_or_unknown():
    validated = _validated_import([PLAY_PAUSE])
    for verdict in (True, None):
        cfg = _device(supported=verdict)
        filtered, rejected = app.filter_import_media(cfg, validated)
        assert rejected == []
        assert filtered is validated
