"""WS-630: 固件更新滚动通道（check/apply 传 channel + 通道持久化）回归测试。

测试全部 import 生产函数（``host/app.py`` 与 ``host/online_update.py``），只把
面向网络的 ``resolve_latest_update`` 换成「按通道返回不同固件版本清单」的桩，
验证接线正确——**不在测试里复制任何生产判定逻辑**：

* ``?channel=rolling`` 命中 rolling Release 的固件 entry；
* 不传 / 非法 channel 默认回退 stable（行为与接线前一致）；
* ``apply`` 与 ``check`` 用同一通道，不会悄悄回到 stable；
* ``fw_channel`` 可读写、默认 stable、能跨 config reload 保留。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as host_app  # noqa: E402
import online_update  # noqa: E402


# --- helpers ---------------------------------------------------------------

# 两个通道给出不同的固件版本 / tag，才能证明 check/apply 真的用了所选通道，
# 而不是「参数传了 rolling、解析却仍走 stable」。
FW_VERSION = {"stable": "1.4.2", "rolling": "1.4.5"}
FW_TAG = {"stable": "v1.5.0", "rolling": "main-20261112-0100"}


def _manifest(fw_version):
    return {
        "app": {"version": "1.5.0", "asset": "BindDeckLite.exe",
                "sha256": "a" * 64, "size": 1234, "min_from": "1.0.0"},
        "firmware": {
            "esp32dev": {"version": fw_version,
                         "asset": "ota_binddecklite_esp32_v%s.bin" % fw_version,
                         "sha256": "b" * 64, "size": 100},
            "esp32c3": {"version": fw_version,
                        "asset": "ota_binddecklite_c3_v%s.bin" % fw_version,
                        "sha256": "c" * 64, "size": 100},
            "esp32c3_16mb": {"version": fw_version,
                             "asset": "ota_binddecklite_c3_16mb_v%s.bin" % fw_version,
                             "sha256": "d" * 64, "size": 100},
        },
        "notes": "## 本次更新\n### 新增\n- x",
        "published_at": "2026-11-12T01:00:00Z",
    }


def _resolve_for_channel(channel):
    """按通道构造一份真实结构的更新信息（用生产 resolve_release_update 解析）。

    与 ``resolve_latest_update`` 一样在结果里带上 ``channel``，模拟真实返回值。
    """
    normalized = online_update.normalize_channel(channel)
    fw_version = FW_VERSION[normalized]
    tag = FW_TAG[normalized]
    assets = []
    for target in ("esp32", "c3", "c3_16mb"):
        name = "ota_binddecklite_%s_v%s.bin" % (target, fw_version)
        assets.append({"name": name, "size": 100,
                       "browser_download_url": "https://example.invalid/" + name})
    info = online_update.resolve_release_update(
        {"tag_name": tag, "body": "", "assets": assets},
        online_update.parse_manifest(json.dumps(_manifest(fw_version))))
    info["channel"] = normalized
    return info


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(host_app, "CONFIG_PATH", str(path))
    monkeypatch.setattr(host_app, "_config", {
        "devices": {}, "settings": {"actions_enabled": True},
        "skipped_version": "", "last_update": {}, "update_fail_count": 0,
    })
    host_app._events.clear()
    host_app._ota_windows.clear()
    host_app._fw_update_tasks.clear()
    return path


@pytest.fixture
def client(monkeypatch, cfgfile):
    monkeypatch.setattr(host_app, "save_config", lambda: None)
    host_app.app.config["TESTING"] = True
    return host_app.app.test_client()


@pytest.fixture
def resolve_spy(monkeypatch):
    """按通道返回不同清单，并记录每次 resolve_latest_update 的 channel。"""
    calls = []

    def _fake(*args, **kwargs):
        # 生产代码用 resolve_latest_update(channel=channel) 关键字调用。
        channel = kwargs.get("channel", online_update.DEFAULT_UPDATE_CHANNEL)
        calls.append(online_update.normalize_channel(channel))
        return _resolve_for_channel(channel)

    monkeypatch.setattr(online_update, "resolve_latest_update", _fake)
    return calls


def _allow_apply(monkeypatch):
    """让 apply 走到成功响应，但不真的起下载/OTA 线程。"""
    monkeypatch.setattr(host_app, "_ota_is_connected", lambda dev_id: True)
    # worker 只是后台任务，替换成 no-op：daemon 线程立刻结束，不发网络请求。
    monkeypatch.setattr(host_app, "_fw_update_worker", lambda *a, **k: None)


def _set_device_fw(dev_id, version, chip, flash):
    host_app._config["devices"][dev_id] = {
        "name": dev_id, "chip": chip, "fw": {
            "version": version, "model": "Lite", "chip": chip,
            "build": "2026-11-01", "flash": flash},
    }


# --- check ------------------------------------------------------------------

def test_check_rolling_uses_the_rolling_release(client, resolve_spy):
    """?channel=rolling → resolver 收到 rolling，响应带 channel/tag 与滚动固件。"""
    _set_device_fw("dev1", "1.3.8", "C3", 4096)

    body = client.get(
        "/api/firmware/update/check?dev_id=dev1&channel=rolling").get_json()

    assert resolve_spy == ["rolling"]
    assert body["channel"] == "rolling"
    assert body["tag"] == "main-20261112-0100"
    assert body["latest"] == "1.4.5"
    assert body["has_update"] is True
    assert body["asset"]["name"] == "ota_binddecklite_c3_v1.4.5.bin"


def test_check_defaults_to_stable(client, resolve_spy):
    """不传 channel → 默认 stable，行为与接线前完全一致。"""
    _set_device_fw("dev1", "1.3.8", "C3", 4096)

    body = client.get("/api/firmware/update/check?dev_id=dev1").get_json()

    assert resolve_spy == ["stable"]
    assert body["channel"] == "stable"
    assert body["latest"] == "1.4.2"


def test_check_illegal_channel_falls_back_to_stable(client, resolve_spy):
    """非法 channel → normalize_channel 回退 stable。"""
    _set_device_fw("dev1", "1.3.8", "C3", 4096)

    body = client.get(
        "/api/firmware/update/check?dev_id=dev1&channel=nightly").get_json()

    assert resolve_spy == ["stable"]
    assert body["channel"] == "stable"
    assert body["latest"] == "1.4.2"


# --- apply ------------------------------------------------------------------

def test_apply_uses_rolling_channel(client, monkeypatch, resolve_spy):
    """apply 带 channel=rolling → 成功响应的版本/资产来自 rolling，不回 stable。"""
    _set_device_fw("dev1", "1.3.8", "C3", 4096)
    _allow_apply(monkeypatch)

    resp = client.post("/api/firmware/update/apply",
                       json={"dev_id": "dev1", "channel": "rolling"})
    body = resp.get_json()

    assert resp.status_code == 200
    assert resolve_spy == ["rolling"]
    assert body["ok"] is True
    assert body["channel"] == "rolling"
    assert body["version"] == "1.4.5"
    assert body["asset"] == "ota_binddecklite_c3_v1.4.5.bin"


def test_apply_defaults_to_stable(client, monkeypatch, resolve_spy):
    """apply 不带 channel → 用 stable 清单（默认行为不变）。"""
    _set_device_fw("dev1", "1.3.8", "C3", 4096)
    _allow_apply(monkeypatch)

    resp = client.post("/api/firmware/update/apply", json={"dev_id": "dev1"})
    body = resp.get_json()

    assert resolve_spy == ["stable"]
    assert body["channel"] == "stable"
    assert body["version"] == "1.4.2"


def test_check_and_apply_use_the_same_channel(client, monkeypatch, resolve_spy):
    """同一通道先 check 再 apply：两次 resolver 都收到 rolling（一致性）。"""
    _set_device_fw("dev1", "1.3.8", "C3", 4096)
    _allow_apply(monkeypatch)

    check = client.get(
        "/api/firmware/update/check?dev_id=dev1&channel=rolling").get_json()
    apply = client.post("/api/firmware/update/apply",
                        json={"dev_id": "dev1", "channel": "rolling"}).get_json()

    assert resolve_spy == ["rolling", "rolling"]
    assert check["channel"] == apply["channel"] == "rolling"
    assert check["latest"] == apply["version"] == "1.4.5"


# --- 通道持久化 / 读写 --------------------------------------------------------

def test_fw_channel_defaults_to_stable(cfgfile):
    """config 没有 fw_channel → _update_settings() 给 stable。"""
    assert host_app._update_settings()["fw_channel"] == "stable"


def test_set_fw_channel_persists(client, cfgfile):
    """POST 合法通道 → 写进 config，响应回显。"""
    resp = client.post("/api/firmware/update/channel",
                       json={"channel": "rolling"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "channel": "rolling"}
    assert host_app._config["fw_channel"] == "rolling"
    assert host_app._update_settings()["fw_channel"] == "rolling"


def test_set_fw_channel_rejects_invalid(client, cfgfile):
    """POST 非法通道 → 400，不改动现有设置。"""
    resp = client.post("/api/firmware/update/channel",
                       json={"channel": "nightly"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert host_app._update_settings()["fw_channel"] == "stable"


def test_fw_channel_is_independent_of_app_channel(client, cfgfile):
    """固件通道与上位机通道互不干扰：改固件不动上位机，反之亦然。"""
    client.post("/api/app/update/channel", json={"channel": "rolling"})
    client.post("/api/firmware/update/channel", json={"channel": "rolling"})
    client.post("/api/app/update/channel", json={"channel": "stable"})
    state = host_app._update_settings()
    assert state["update_channel"] == "stable"
    assert state["fw_channel"] == "rolling"


def test_app_version_exposes_fw_channel(client, cfgfile):
    """/api/app/version 与 _app_version_payload() 都带 fw_channel（前端据此恢复）。"""
    client.post("/api/firmware/update/channel", json={"channel": "rolling"})
    body = client.get("/api/app/version").get_json()
    assert body["fw_channel"] == "rolling"
    assert host_app._app_version_payload()["fw_channel"] == "rolling"


def test_load_config_restores_persisted_fw_channel(tmp_path, monkeypatch):
    """config.json 里的 fw_channel 经 load_config() 读回后仍保留（跨重启持久化）。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"devices": {}, "settings": {},
                                "fw_channel": "rolling"}), encoding="utf-8")
    monkeypatch.setattr(host_app, "CONFIG_PATH", str(path))
    monkeypatch.setattr(host_app, "_config", {"devices": {}, "settings": {}})

    host_app.load_config()

    assert host_app._config["fw_channel"] == "rolling"
    assert host_app._update_settings()["fw_channel"] == "rolling"


def test_load_config_defaults_missing_fw_channel_to_stable(tmp_path, monkeypatch):
    """旧 config 没有 fw_channel → load_config() 补 stable，不改变默认行为。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"devices": {}, "settings": {}}), encoding="utf-8")
    monkeypatch.setattr(host_app, "CONFIG_PATH", str(path))
    monkeypatch.setattr(host_app, "_config", {"devices": {}, "settings": {}})

    host_app.load_config()

    assert host_app._config["fw_channel"] == "stable"
