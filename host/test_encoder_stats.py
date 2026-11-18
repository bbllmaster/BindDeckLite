"""编码器旋转自检（WS-641）的后端回归测试。

Why this file exists
--------------------
编码器页的自检条要靠 ``/api/status`` 里一个新字段 ``encoder_stats`` 才能
工作，而这个字段只有一个来源：``_on_line`` 的 ``ENC:`` 分支。改动最容易
坏在这三处，本文件逐条钉住：

1. 方向按线格式原样累加 —— 固件已经按 ``CFG:ENCDIR`` 翻过面，主机**不能**
   再翻一次（否则「切反转后计数换列」这条验收永远不可能成立）；
2. 字段是 additive 的，没有旋转时也存在（空对象），旧前端忽略它不报错；
3. 非法方向（空 / 杂字）不产生计数，也不会在表里塞一条脏记录。

测试 import 生产函数（``app._on_line`` / ``app.api_status``），不复制行解析
或计数逻辑：把 ``host/app.py`` 里的实现改坏，本文件就会失败。

修复前 ``/api/status`` 没有 ``encoder_stats`` 字段、``_enc_stats`` 不存在，
本文件全部失败；修复后全部通过。

Run::

    cd host && python -m pytest test_encoder_stats.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


DEV = "t-enc-self"


@pytest.fixture(autouse=True)
def _isolated_stats():
    """每个用例一张干净的计数表，且不写真实 config.json。"""
    original_save = app.save_config
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    app.save_config = lambda: None
    app._enc_stats.clear()
    try:
        yield
    finally:
        app._enc_stats.clear()
        app.save_config = original_save


def _status():
    return app.app.test_client().get("/api/status").get_json()


def test_enc_lines_accumulate_per_direction_and_keep_wire_direction():
    # 3 条 CW + 2 条 CCW：与真机慢转 5 格同形。
    for _ in range(3):
        app._on_line(DEV, "ENC:CW")
    for _ in range(2):
        app._on_line(DEV, "ENC:CCW")

    stats = _status()["encoder_stats"][DEV]
    assert stats["cw"] == 3
    assert stats["ccw"] == 2
    # 最后一条是 CCW；方向必须原样保留 —— 主机再翻一次的话这里会变成 CW。
    assert stats["last_dir"] == "CCW"
    # age 由服务端算，前端据此判「5 s 内」。
    assert isinstance(stats["age"], (int, float))
    assert 0 <= stats["age"] < 5


def test_direction_is_not_inverted_by_the_host():
    # 单独的 CW 一台设备：翻转一次会让 cw 变成 0、ccw 变成 1。
    app._on_line("t-enc-cw", "ENC:CW")
    stats = _status()["encoder_stats"]["t-enc-cw"]
    assert stats["cw"] == 1
    assert stats["ccw"] == 0
    assert stats["last_dir"] == "CW"


def test_field_is_additive_and_present_before_any_rotation():
    payload = _status()
    assert "encoder_stats" in payload
    assert payload["encoder_stats"] == {}


def test_unknown_direction_is_ignored():
    app._on_line(DEV, "ENC:")
    app._on_line(DEV, "ENC:SIDEWAYS")
    assert _status()["encoder_stats"] == {}
