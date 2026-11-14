"""WS-619: 动作导出 / 导入回归用例。

断言全部驱动 **生产函数**（host/app.py 的 build_actions_export /
validate_actions_import / merge_actions_import，以及 Flask 视图
api_actions_import），不在这里复制任何一份序列化或校验逻辑 —— 改坏生产代码
就会真的变红。

运行::

    cd host && python -m pytest test_actions_io.py -q
"""

import copy
import json
import os
import sys

import pytest

# host/ 不是包，按路径 import 生产模块。
sys.path.insert(0, os.path.dirname(__file__))
import app as host_app


def _cfg_with_actions():
    """一台配了动作的设备存档（new_device_cfg 自带 12 条 keys）。"""
    cfg = host_app.new_device_cfg("ESP32", "MyDeck")
    cfg["keys"][0]["action"] = {
        "type": "command", "value": "calc.exe", "macro": []}
    cfg["keys"][3]["action"] = {
        "type": "macro", "value": "", "macro": [
            {"type": "key", "value": "ctrl+alt+t"},
            {"type": "text", "value": "hello"},
        ]}
    cfg["encoder"]["actions"]["CW"] = {
        "type": "command", "value": "echo cw", "macro": []}
    return cfg


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Flask 测试客户端 + 临时 config.json，并重置进程级 _config。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"devices": {}, "settings": {}},
                               ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(host_app, "CONFIG_PATH", str(path))
    host_app._config = {
        "devices": {}, "settings": {},
        "skipped_version": "", "last_update": {}, "update_fail_count": 0,
        "update_channel": "stable", "lang": "system",
    }
    host_app.app.config["TESTING"] = True
    return host_app.app.test_client(), path


# ---------------------------------------------------------------------------
# 导出结构：不含 dev_id / BLE 地址 / WiFi 凭据
# ---------------------------------------------------------------------------

def test_export_has_no_sensitive_fields():
    cfg = _cfg_with_actions()
    # 故意把敏感字段塞进 cfg：如果有人偷懒直接 json.dump(cfg)，这里就会露馅。
    cfg["dev_id"] = "secret-dev-id"
    cfg["bond"] = {"bound": True, "addr": "AA:BB:CC:DD:EE:FF"}
    cfg["wifi"] = {"ssid": "office", "password": "hunter2"}
    cfg["pins"] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    payload = host_app.build_actions_export(cfg)
    text = json.dumps(payload, ensure_ascii=False)

    assert "secret-dev-id" not in text
    assert "AA:BB:CC:DD:EE:FF" not in text
    assert "hunter2" not in text
    assert "office" not in text
    assert "pins" not in payload
    assert "wifi" not in payload and "bond" not in payload
    assert "dev_id" not in payload
    assert payload["schema"] == host_app.ACTIONS_EXPORT_SCHEMA
    assert set(payload) == {"schema", "exported_at", "source", "inputs", "encoder"}
    assert set(payload["source"]) == {"name", "chip", "layout"}
    assert all(set(i) == {"index", "code", "mods", "action"}
               for i in payload["inputs"])
    assert set(payload["encoder"]) == {"CW", "CCW"}


def test_export_only_known_action_types():
    """导出只带 type/value/macro，不带动作对象上的其它杂字段。"""
    cfg = _cfg_with_actions()
    cfg["keys"][0]["action"]["secret"] = "should-not-export"
    payload = host_app.build_actions_export(cfg)
    assert payload["inputs"][0]["action"] == {
        "type": "command", "value": "calc.exe", "macro": []}


# ---------------------------------------------------------------------------
# 往返一致
# ---------------------------------------------------------------------------

def test_roundtrip_preserves_keys_and_encoder_actions():
    cfg = _cfg_with_actions()
    payload = host_app.build_actions_export(cfg)
    validated = host_app.validate_actions_import(copy.deepcopy(payload))

    fresh = host_app.new_device_cfg("ESP32-C3", "")
    host_app.merge_actions_import(fresh, validated)

    for item in payload["inputs"]:
        idx = item["index"]
        assert fresh["keys"][idx]["code"] == item["code"]
        assert fresh["keys"][idx]["mods"] == item["mods"]
        assert fresh["keys"][idx]["action"] == item["action"]
    # 文件里没有出现的输入保持默认值（仅 HID）。
    assert fresh["keys"][1]["action"]["type"] == "none"
    assert fresh["encoder"]["actions"]["CW"] == cfg["encoder"]["actions"]["CW"]


def test_merge_preserves_inputs_absent_from_file():
    """按 index 覆盖，未出现的 index 保持原值。"""
    cfg = _cfg_with_actions()
    expected_action = copy.deepcopy(cfg["keys"][1]["action"])
    payload = {"schema": 1, "inputs": [
        {"index": 5, "code": 77, "mods": 3,
         "action": {"type": "url", "value": "https://example.com", "macro": []}},
    ], "encoder": {}}
    validated = host_app.validate_actions_import(payload)
    host_app.merge_actions_import(cfg, validated)
    assert cfg["keys"][1]["action"] == expected_action
    assert cfg["keys"][5]["action"]["value"] == "https://example.com"


# ---------------------------------------------------------------------------
# 坏文件整份拒绝（纯函数层）
# ---------------------------------------------------------------------------

def _bad_variants():
    good = host_app.build_actions_export(_cfg_with_actions())
    out = []

    v = copy.deepcopy(good); v["schema"] = 99; out.append(("schema", v))
    v = copy.deepcopy(good); v["inputs"][0]["action"]["type"] = "teleport"
    out.append(("unknown type", v))
    v = copy.deepcopy(good)
    v["inputs"][3]["action"]["macro"].append({"type": "warp"})
    out.append(("unknown macro step", v))
    v = copy.deepcopy(good); v["inputs"] = good["inputs"] + [dict(good["inputs"][0])]
    out.append(("too many inputs", v))
    v = copy.deepcopy(good); v["inputs"][2]["index"] = 99
    out.append(("bad index", v))
    v = copy.deepcopy(good); v["inputs"][2]["index"] = good["inputs"][1]["index"]
    out.append(("dup index", v))
    v = copy.deepcopy(good); del v["inputs"]
    out.append(("no inputs", v))
    out.append(("not an object", ["not", "an", "object"]))
    return out


@pytest.mark.parametrize("label,data", _bad_variants(),
                         ids=[label for label, _ in _bad_variants()])
def test_validate_rejects_bad_files_with_chinese_reason(label, data):
    with pytest.raises(host_app.ActionsImportError) as exc:
        host_app.validate_actions_import(data)
    reason = str(exc.value)
    assert reason, label
    # 翻译键缺失时 t() 会把 key 原样回显；以 "error." 开头说明文案没配好。
    assert not reason.startswith("error.")
    # 是给用户看的中文句子，不是空串或原始 key。
    assert any("\u4e00" <= ch <= "\u9fff" for ch in reason)


def test_validate_reason_names_the_input():
    good = host_app.build_actions_export(_cfg_with_actions())
    good["inputs"][0]["action"]["type"] = "teleport"
    with pytest.raises(host_app.ActionsImportError) as exc:
        host_app.validate_actions_import(good)
    # 点名第几个输入。
    assert "1" in str(exc.value) and "输入" in str(exc.value)


def test_twelve_input_limit():
    """12 项正好接受，第 13 项拒绝。"""
    good = host_app.build_actions_export(_cfg_with_actions())
    assert len(good["inputs"]) == host_app.ACTIONS_INPUT_LIMIT
    host_app.validate_actions_import(copy.deepcopy(good))  # 不抛
    good["inputs"].append(dict(good["inputs"][0]))
    with pytest.raises(host_app.ActionsImportError):
        host_app.validate_actions_import(good)


# ---------------------------------------------------------------------------
# 坏文件整份拒绝（Flask 视图层）+ config.json 零改动
# ---------------------------------------------------------------------------

def test_route_rejects_non_json_and_leaves_config_unchanged(client):
    c, path = client
    for raw in (b"{ truncated", b"", b"\xff\xfe", b"not json at all"):
        before = path.read_bytes()
        resp = c.post("/api/actions/import/dev1", data=raw,
                      content_type="application/json")
        assert resp.status_code == 400, raw
        assert path.read_bytes() == before, raw  # 一个字节都没动


def test_route_rejects_bad_shape_and_leaves_config_unchanged(client):
    c, path = client
    for _label, payload in _bad_variants():
        before = path.read_bytes()
        resp = c.post("/api/actions/import/dev1", json=payload)
        assert resp.status_code == 400, payload
        body = resp.get_json()
        assert body["ok"] is False
        assert body["error"]
        assert path.read_bytes() == before, payload


def test_route_rejects_oversize_file(client):
    c, path = client
    before = path.read_bytes()
    big = b" " * (host_app.ACTIONS_IMPORT_MAX_BYTES + 1)
    resp = c.post("/api/actions/import/dev1", data=big,
                  content_type="application/json")
    assert resp.status_code == 400
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# 导出视图：原生保存对话框 → 写 UTF-8 / 2 空格缩进的 JSON
# ---------------------------------------------------------------------------

def test_export_route_writes_json_file(client, monkeypatch):
    c, path = client
    cfg = _cfg_with_actions()
    cfg["bond"] = {"bound": True, "addr": "AA:BB:CC:DD:EE:FF"}
    host_app._config["devices"]["dev1"] = cfg
    out = path.parent / "backup.json"
    seen = {}

    def _fake_browse(kind, default_name=""):
        seen["kind"] = kind
        seen["default_name"] = default_name
        return {"ok": True, "path": str(out)}

    monkeypatch.setattr(host_app.desktop, "browse", _fake_browse)
    resp = c.post("/api/actions/export/dev1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["cancelled"] is False
    assert body["count"] == host_app.ACTIONS_INPUT_LIMIT
    assert body["actions"] == 2          # command + macro，编码器动作另计
    assert seen["kind"] == "save"
    assert seen["default_name"].startswith("BindDeckLite-")
    assert seen["default_name"].endswith(".json")

    text = out.read_text(encoding="utf-8")
    assert '\n  "schema"' in text          # 2 空格缩进
    payload = json.loads(text)
    assert "AA:BB:CC:DD:EE:FF" not in text
    assert "dev_id" not in payload
    assert payload["encoder"]["CW"]["value"] == "echo cw"


def test_export_route_cancel_has_no_side_effects(client, monkeypatch):
    """用户在保存对话框点取消：不写文件、不加日志、返回 cancelled。"""
    c, path = client
    host_app._config["devices"]["dev1"] = _cfg_with_actions()
    before_events = len(host_app._events)
    monkeypatch.setattr(host_app.desktop, "browse",
                        lambda kind, default_name="": {"ok": True, "path": ""})
    resp = c.post("/api/actions/export/dev1")
    assert resp.status_code == 200
    assert resp.get_json()["cancelled"] is True
    assert len(host_app._events) == before_events
    assert not (path.parent / "backup.json").exists()


# ---------------------------------------------------------------------------
# 成功导入：原子写盘 + 合并语义 + 脏标记
# ---------------------------------------------------------------------------

def test_import_route_writes_config_atomically(client):
    c, path = client
    cfg = _cfg_with_actions()
    host_app._config["devices"]["dev1"] = cfg
    host_app.save_config()  # 先落一份基线
    payload = host_app.build_actions_export(cfg)
    expected = copy.deepcopy(cfg["keys"][0]["action"])

    resp = c.post("/api/actions/import/dev1", json=payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["dirty"] is True
    # 10 输入（编码器布局）：前 10 个生效，第 11–12 个保存但暂不显示。
    assert body["applied"] == host_app.INPUT_COUNT_ENCODER
    assert body["skipped_extra"] == [11, 12]

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["devices"]["dev1"]["keys"][0]["action"] == expected
    assert after["devices"]["dev1"]["encoder"]["actions"]["CW"]["value"] == "echo cw"
    # 没有留下 .tmp 半份文件。
    assert not (path.parent / "config.json.tmp").exists()
