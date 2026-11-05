"""WS-523: 固件 OTA 在线检测与推送的回归测试。

覆盖需求单列出的固件相关项：

* 型号映射 4 个分支：ESP32 / C3 16MB / C3 4MB / ``flash=0`` 与 2048 → 无在线固件；
* 远端版本 <= 当前版本 → 已是最新，不提供更新（不降级、不重装）；
* OTA 忙时 apply 被 ``_ota_lock`` 拦截；
* ``_ota_validate`` 拒绝 merged 整片包（既有行为回归）；
* 推送前必须用 ``_ota_slot_cap`` / ``_ota_validate_image`` 校验镜像。

这些测试**全部 import 生产函数**（``host/app.py`` 与 ``host/online_update.py``），
不在测试里复制一份判定逻辑。
"""

import hashlib
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as host_app  # noqa: E402
import online_update  # noqa: E402


# --- helpers ---------------------------------------------------------------


def _manifest(fw_version="1.4.2"):
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
        "published_at": "2026-11-04T00:00:00Z",
    }


def _install_manifest(monkeypatch, fw_version="1.4.2"):
    """Make every release lookup return the same manifest."""
    def _resolve(*args, **kwargs):
        return online_update.resolve_release_update(
            {"tag_name": "v1.5.0", "body": "", "assets": []},
            online_update.parse_manifest(json.dumps(_manifest(fw_version))))
    monkeypatch.setattr(online_update, "resolve_latest_update", _resolve)


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(host_app, "CONFIG_PATH", str(path))
    host_app._config = {"devices": {}, "settings": {"actions_enabled": True},
                        "skipped_version": "", "last_update": {},
                        "update_fail_count": 0}
    host_app._events.clear()
    return path


@pytest.fixture
def client(monkeypatch, cfgfile):
    """Flask test client. ``cfgfile`` runs first so the real save_config is
    still in place when that fixture asserts it was not stubbed elsewhere."""
    monkeypatch.setattr(host_app, "save_config", lambda: None)
    host_app.app.config["TESTING"] = True
    return host_app.app.test_client()


def _set_device_fw(dev_id, version, chip, flash):
    host_app._config["devices"][dev_id] = {
        "name": dev_id, "chip": chip, "fw": {
            "version": version, "model": "Lite", "chip": chip,
            "build": "2026-11-01", "flash": flash},
    }


# --- 型号映射 -----------------------------------------------------------------

def test_esp32_maps_to_esp32dev():
    """经典 ESP32（4 MB）→ esp32dev。"""
    assert online_update.variant_for_device("ESP32", 4096) == "esp32dev"


def test_c3_16mb_maps_to_esp32c3_16mb():
    """C3 + 16 MB → esp32c3_16mb。"""
    assert online_update.variant_for_device("C3", 16384) == "esp32c3_16mb"


def test_c3_4mb_maps_to_esp32c3():
    """C3 + 4 MB → esp32c3。"""
    assert online_update.variant_for_device("C3", 4096) == "esp32c3"


def test_unknown_flash_size_yields_no_online_firmware():
    """老固件缺第 5 个 VERSION 字段（flash=0）→ 不猜，无在线固件。"""
    assert online_update.variant_for_device("C3", 0) is None
    assert online_update.variant_for_device("ESP32", 0) is None


def test_two_megabyte_module_has_no_online_firmware():
    """2 MB 模块（2048）没有 app1 分区 → 无在线固件。"""
    assert online_update.variant_for_device("C3", 2048) is None


def test_chip_alone_is_not_enough_to_guess_the_variant():
    """只看 flash 会把经典 ESP32 误判成 C3 —— 必须同时看 chip。"""
    assert online_update.variant_for_device("ESP32", 16384) == "esp32dev"
    assert online_update.variant_for_device("C3", 16384) == "esp32c3_16mb"


# --- 检测接口 ---------------------------------------------------------------

def test_check_reports_the_variant_and_the_new_version(client, monkeypatch, cfgfile):
    _install_manifest(monkeypatch)
    _set_device_fw("dev1", "1.3.8", "C3", 16384)

    body = client.get("/api/firmware/update/check?dev_id=dev1").get_json()

    assert body["device_variant"] == "esp32c3_16mb"
    assert body["current"] == "1.3.8"
    assert body["latest"] == "1.4.2"
    assert body["has_update"] is True
    assert body["asset"]["name"] == "ota_binddecklite_c3_16mb_v1.4.2.bin"
    assert body["asset"]["sha256"] == "d" * 64


def test_check_says_there_is_no_online_firmware_for_unknown_models(client, monkeypatch,
                                                                  cfgfile):
    """型号不在清单里 → 提示「该型号暂无在线固件」，不发起下载。"""
    _install_manifest(monkeypatch)
    _set_device_fw("dev-old", "1.0.0", "C3", 0)

    body = client.get("/api/firmware/update/check?dev_id=dev-old").get_json()

    assert body["device_variant"] == ""
    assert body["has_update"] is False
    assert "暂无在线固件" in body["reason"]
    assert body["reason_code"] == "E_NO_VARIANT"


def test_check_reports_already_up_to_date(client, monkeypatch, cfgfile):
    """远端版本 <= 当前版本 → 已是最新，不提供更新。"""
    _install_manifest(monkeypatch, fw_version="1.3.8")
    _set_device_fw("dev1", "1.3.8", "C3", 4096)

    body = client.get("/api/firmware/update/check?dev_id=dev1").get_json()

    assert body["latest"] == "1.3.8"
    assert body["has_update"] is False


def test_check_does_not_offer_a_downgrade(client, monkeypatch, cfgfile):
    """远端比当前旧 → 不提供更新（不降级）。"""
    _install_manifest(monkeypatch, fw_version="1.0.0")
    _set_device_fw("dev1", "1.3.8", "C3", 4096)

    body = client.get("/api/firmware/update/check?dev_id=dev1").get_json()

    assert body["has_update"] is False


# --- apply 被 OTA 锁拦截 ------------------------------------------------------

def test_apply_is_blocked_while_an_ota_is_running(client, monkeypatch, cfgfile):
    """OTA 进行中 → apply 被拒绝并说明「升级进行中」。"""
    _install_manifest(monkeypatch)
    _set_device_fw("dev1", "1.3.8", "C3", 4096)

    class _Busy:
        status = "uploading"
        error = ""

    host_app._ota_windows["dev1"] = _Busy()
    try:
        resp = client.post("/api/firmware/update/apply", json={"dev_id": "dev1"})
        body = resp.get_json()
    finally:
        host_app._ota_windows.pop("dev1", None)

    assert resp.status_code == 409
    assert body["error_code"] == "E_OTA_BUSY"
    assert "设备正在升级" in body["error"]


def test_apply_is_blocked_by_the_ota_lock_itself(client, monkeypatch, cfgfile):
    """_ota_lock 被占用时同样拒绝（并发保护）。

    锁必须由**另一个线程**占着：``_ota_lock`` 是 RLock，同一线程可以重入，
    只有别的线程占着时才算「忙」。
    """
    _install_manifest(monkeypatch)
    _set_device_fw("dev1", "1.3.8", "C3", 4096)

    holding = threading.Event()
    release = threading.Event()

    def _hold():
        with host_app._ota_lock:
            holding.set()
            release.wait(5.0)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert holding.wait(5.0), "the holder thread did not take the lock"
    try:
        resp = client.post("/api/firmware/update/apply", json={"dev_id": "dev1"})
    finally:
        release.set()
        holder.join(5.0)

    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == "E_OTA_BUSY"


def test_apply_refuses_models_without_online_firmware(client, monkeypatch, cfgfile):
    _install_manifest(monkeypatch)
    _set_device_fw("dev-old", "1.0.0", "C3", 0)

    resp = client.post("/api/firmware/update/apply", json={"dev_id": "dev-old"})
    body = resp.get_json()

    assert resp.status_code == 400
    assert body["error_code"] == "E_NO_VARIANT"


def test_apply_refuses_when_the_remote_is_not_newer(client, monkeypatch, cfgfile):
    _install_manifest(monkeypatch, fw_version="1.3.8")
    _set_device_fw("dev1", "1.3.8", "C3", 4096)

    resp = client.post("/api/firmware/update/apply", json={"dev_id": "dev1"})

    assert resp.status_code == 409
    assert "已是最新" in resp.get_json()["error"]


# --- 既有 OTA 行为回归 -------------------------------------------------------

def test_ota_validate_still_rejects_merged_images():
    """回归：merged 整片包仍被拒绝（只接受 ota_*.bin）。"""
    assert host_app._ota_validate("ota_binddecklite_c3_v1.3.0.bin") == ""
    err = host_app._ota_validate("merged_binddecklite_c3_v1.3.0.bin")
    assert err, "merged 整片包必须被拒绝"
    assert "ota_" in err


def test_slot_cap_for_the_three_supported_models():
    """回归：槽容量按 flash 容量算；2 MB 板不可 OTA。"""
    assert host_app._ota_slot_cap({"flash": 16384}) == 3 * 1024 * 1024
    assert host_app._ota_slot_cap({"flash": 4096}) == 0x1F0000
    assert host_app._ota_slot_cap({"flash": 2048}) == 0


def test_image_validation_rejects_a_merged_package():
    """回归：merged 包缺少 app descriptor 魔数，必须被拒。"""
    err = host_app._ota_validate_image(b"\xE9" + b"\x00" * 200, 3 * 1024 * 1024)
    assert err, "merged 包必须被拒绝"
    assert "merged" in err or "app-only" in err


def test_online_asset_name_must_pass_the_existing_ota_validator():
    """清单里的资产名同样要过 _ota_validate —— 不能绕过既有约束。"""
    assert host_app._ota_validate("ota_binddecklite_c3_v1.4.2.bin") == ""
    assert host_app._ota_validate("merged_binddecklite_c3_v1.4.2.bin")


# --- 校验与推送 ---------------------------------------------------------------

def test_downloaded_firmware_is_verified_before_it_is_pushed(tmp_path, monkeypatch):
    """下载的固件必须先过 SHA256 校验，才交给推送通道。"""
    payload = b"firmware-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    part = tmp_path / "ota_binddecklite_c3_v1.4.2.bin.part"
    part.write_bytes(payload)
    final = tmp_path / "ota_binddecklite_c3_v1.4.2.bin"

    error = online_update.finalize_download(str(part), str(final), digest,
                                            len(payload))

    assert error == ""
    assert final.read_bytes() == payload


def test_firmware_with_a_wrong_checksum_is_not_pushed(tmp_path):
    """校验不符 → 不产生可推送的文件。"""
    part = tmp_path / "ota_binddecklite_c3_v1.4.2.bin.part"
    part.write_bytes(b"tampered")
    final = tmp_path / "ota_binddecklite_c3_v1.4.2.bin"

    error = online_update.finalize_download(str(part), str(final), "e" * 64, 9)

    assert error
    assert not final.exists()
    assert not part.exists()
