"""WS-666 回归：写进 ``_config`` 的 label 必须是普通 ``str``。

Why this file exists
--------------------
``_config`` 里一旦存进 ``i18n.Message``，后续任何 ``copy.deepcopy(_config)``
都会抛 ``TypeError``（``Message.__new__`` 需要 ``text/code/params`` 三参，
而默认的 ``str`` 重建只传文本），表现为「读取设备配置」第二次、以及
「导入动作备份」必 500。

``KEY_CODES`` 里「不发键」哨兵与 WS-661 的 7 个媒体键，label 都来自
``t()``，所以只要设备上报键码 0 / 媒体码，或者导入的备份里带这些码，
label 就会被写进存档 —— 第一次成功，第二次 500。本文件从真实入口
（``app.collect_cfg_snapshot`` / ``app.merge_actions_import``）复现，
钉住三件事：

1. 连续两次采集/导入都不抛；
2. ``type(cfg["keys"][i]["label"]) is str``（存档里不再是 ``Message``）；
3. ``Message`` 自身可 deepcopy 且 ``code`` / ``params`` 不丢（防御层）。

测试 import 生产函数，不复制 label 规则：把 ``app._key_label`` 改回直接返回
``KEY_CODES`` 的原值，本文件就会失败。

Run::

    cd host && python -m pytest test_cfg_deepcopy.py -q
"""

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app
import i18n


DEV = "t-deepcopy"
C3_PINS = [0, 1, 2, 3, 4, 5, 6, 7, 8]

# 12 键布局：按键 1 与按键 11（index 10）都是键码 0（不发键），其余是 F13..F21。
KEYS_12_WITH_NONE = "0,241,242,243,244,245,246,247,248,249,0,251"

REPORTED = {
    "keys": KEYS_12_WITH_NONE,
    "pins": "0,1,2,3,4,5,6,7,8",
    "enc": "0",
    "sleep": "1,5",
    "tz": "",
    "layout": "0",
}

# WS-661: 媒体键码 = ``MEDIA_CODE_BASE + usage``，这里取「播放/暂停」。
# 不要用 ``MEDIA_CODE_BASE`` 本身（256，usage 0）当「未知码」样本：它是否
# 在 KEY_CODES 里是 WS-661 的实现细节，断言不该依赖它。
MEDIA_CODE = app.MEDIA_CODE_BASE + 0x00CD


@pytest.fixture(autouse=True)
def _isolated_app(monkeypatch):
    """每个用例独立的 ``_config`` / ``save_config`` / ``_events``，退出还原。"""
    monkeypatch.setattr(app, "_config", {"devices": {}, "settings": {}})
    monkeypatch.setattr(app, "save_config", lambda: None)
    monkeypatch.setattr(app, "_events", type(app._events)(maxlen=400))


def _device(layout=app.LAYOUT_12KEY):
    """In-memory device; never touches the real host/config.json."""
    cfg = app.new_device_cfg("C3", DEV)
    cfg["pins"] = list(C3_PINS)
    cfg["layout"] = layout
    cfg["layout_supported"] = True
    app._config["devices"][DEV] = cfg
    return cfg


def _no_key_text():
    """「不发键」文案的原始表项（普通 str，不经过 t()）。"""
    return i18n.messages(i18n.current_lang())["ui.key_none_hint"]


def _input(index, code, mods=0, action=None):
    return {"index": index, "code": code, "mods": mods,
            "action": action or {"type": "none"}}


def _backup(inputs):
    return {"schema": app.ACTIONS_EXPORT_SCHEMA, "inputs": inputs}


def test_two_snapshots_with_no_key_code_zero_do_not_raise():
    """两次「读取设备配置」都必须成功——修复前第二次是 TypeError。"""
    _device()
    first = app.collect_cfg_snapshot(DEV, REPORTED, 1.0)
    assert first["ok"] is True, first
    second = app.collect_cfg_snapshot(DEV, REPORTED, 2.0)
    assert second["ok"] is True, second


def test_snapshot_labels_are_plain_str():
    """存档里的 label 是普通 str，内容仍是当前语言的渲染文本。"""
    cfg = _device()
    app.collect_cfg_snapshot(DEV, REPORTED, 1.0)
    for item in cfg["keys"]:
        assert type(item["label"]) is str, (item["code"], type(item["label"]))
    # 键码 0 的文案就是 KEY_CODES 末尾那一项（t() 渲染的结果）。
    assert cfg["keys"][0]["label"] == _no_key_text()


def test_snapshot_media_code_gets_localized_media_label():
    """WS-661 交互：回读带媒体码时 label 走 t() 文案，不是 "461"，仍是 str。"""
    cfg = _device()
    reported = dict(REPORTED, keys="461,241,242,243,244,245,246,247,248,249,0,251")
    snap = app.collect_cfg_snapshot(DEV, reported, 1.0)
    assert snap["ok"] is True, snap
    assert type(cfg["keys"][0]["label"]) is str
    assert cfg["keys"][0]["label"] == str(app.t("keys.media_play_pause"))
    assert cfg["keys"][0]["label"] != str(MEDIA_CODE)
    copy.deepcopy(app._config)
    assert app.collect_cfg_snapshot(DEV, reported, 2.0)["ok"] is True


def test_deepcopy_after_snapshot():
    """deepcopy 整份 _config 不再抛，且存档 JSON 内容不变。"""
    _device()
    app.collect_cfg_snapshot(DEV, REPORTED, 1.0)
    clone = copy.deepcopy(app._config)
    assert clone["devices"][DEV]["keys"][0]["label"] == _no_key_text()
    dumped = json.loads(json.dumps(app._config))
    assert dumped["devices"][DEV]["keys"][0]["label"] == _no_key_text()


def test_import_no_key_and_media_code_then_deepcopy():
    """导入含键码 0 与媒体码的备份后，_config 仍可 deepcopy。"""
    cfg = _device()
    validated = app.validate_actions_import(_backup([
        _input(0, 0),
        _input(5, MEDIA_CODE),
        _input(6, 0xB3),
    ]))
    applied, skipped = app.merge_actions_import(cfg, validated)
    assert applied == 3 and skipped == []

    copy.deepcopy(app._config)          # 修复前这里 TypeError

    assert type(cfg["keys"][0]["label"]) is str
    assert cfg["keys"][0]["label"] == _no_key_text()
    # WS-661 的媒体键走 t()，label 必须是本地化文案，而不是 "461" 这种数字串。
    assert type(cfg["keys"][5]["label"]) is str
    assert cfg["keys"][5]["label"]
    assert cfg["keys"][5]["label"] == str(app.t("keys.media_play_pause"))
    assert cfg["keys"][5]["label"] != str(MEDIA_CODE)
    assert type(cfg["keys"][6]["label"]) is str
    assert cfg["keys"][6]["label"] == "Tab"


def test_second_import_after_first_does_not_raise():
    """「导入备份」连点两次：第二次不 500（修复前在 before=deepcopy 处挂）。"""
    _device()
    first = app.validate_actions_import(_backup([_input(0, 0), _input(1, MEDIA_CODE)]))
    assert app.merge_actions_import(_device(), first)[0] == 2
    second = app.validate_actions_import(_backup([_input(0, MEDIA_CODE), _input(1, 0)]))
    assert app.merge_actions_import(app.device_cfg(DEV), second)[0] == 2
    copy.deepcopy(app._config)


def test_message_deepcopy_keeps_code_and_params():
    """防御层：Message 自身可复制，code / params 不丢（add_event 依赖它们）。"""
    original = i18n.t("error.actions_import_bad_index", pos=7)
    assert original.code == "error.actions_import_bad_index"
    assert original.params == {"pos": 7}

    clone = copy.deepcopy(original)
    assert type(clone) is i18n.Message
    assert clone == original
    assert clone.code == original.code
    assert clone.params == original.params
