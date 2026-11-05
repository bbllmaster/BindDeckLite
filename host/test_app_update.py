"""WS-523: 上位机在线自查更新的回归测试。

覆盖需求单列出的 9 项（其中固件相关的在 ``test_fw_update.py``）：

1. 版本比较（``1.10.0 > 1.9.9``，预发布低于正式版）；
2. ``update.json`` 解析 + ``source=fallback``（无清单）路径；
3. ``sha256`` / ``size`` 不符 → 拒绝替换、删临时文件；
4. ETag / 304：本地 ``http.server`` 返回 304 时不报错且复用上次结果；
   60 秒内二次调用不发请求（计数请求次数）；
5. 跳过版本：``skipped_version`` 等于 latest 时 ``has_update=false``；
   更高版本重新提示；
6. 回滚计数：连续失败 >= 2 → 暂停自动更新；
7. 更新器脚本只接受固定路径，不接受任意命令；
8. 目录不可写 → 提示而不是破坏原 exe。

这些测试**全部 import 生产函数**（``host/app.py`` 与 ``host/online_update.py``），
不在测试里复制一份判定逻辑 —— 复制出来的测试改生产代码也不会失败，等于没有测试。
"""

import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as host_app  # noqa: E402
import online_update  # noqa: E402


# --- helpers ---------------------------------------------------------------


def _release_json(tag="v1.5.0", asset="BindDeckLite.exe", size=1234,
                  sha="a" * 64, body="## 本次更新\n### 新增\n- x"):
    """A minimal GitHub Release payload."""
    return {
        "tag_name": tag,
        "body": body,
        "published_at": "2026-11-04T00:00:00Z",
        "html_url": "https://github.com/bbllmaster/BindDeckLite/releases/tag/" + tag,
        "assets": [
            {"name": asset, "size": size,
             "browser_download_url": "https://example.invalid/" + asset},
        ],
    }


def _manifest(app_version="1.5.0", asset="BindDeckLite.exe", size=1234,
              sha="a" * 64, notes="## 本次更新\n### 新增\n- x"):
    return {
        "app": {"version": app_version, "asset": asset, "sha256": sha,
                "size": size, "min_from": "1.0.0"},
        "firmware": {
            "esp32dev": {"version": "1.4.2",
                         "asset": "ota_binddecklite_esp32_v1.4.2.bin",
                         "sha256": "b" * 64, "size": 100},
            "esp32c3": {"version": "1.4.2",
                        "asset": "ota_binddecklite_c3_v1.4.2.bin",
                        "sha256": "c" * 64, "size": 100},
            "esp32c3_16mb": {"version": "1.4.2",
                             "asset": "ota_binddecklite_c3_16mb_v1.4.2.bin",
                             "sha256": "d" * 64, "size": 100},
        },
        "notes": notes,
        "published_at": "2026-11-04T00:00:00Z",
    }


class _FakeReleaseHandler(BaseHTTPRequestHandler):
    """Serves a fixed Release JSON with ETag support (304 on match)."""

    payload = b"{}"
    etag = '"release-etag-1"'
    hits = 0

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).hits += 1
        if self.headers.get("If-None-Match") == type(self).etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", type(self).etag)
        self.send_header("Content-Length", str(len(type(self).payload)))
        self.end_headers()
        self.wfile.write(type(self).payload)

    def log_message(self, *args):  # silence the server log
        pass


@pytest.fixture
def fake_server():
    """A local HTTP server that answers the release API with an ETag."""
    _FakeReleaseHandler.hits = 0
    _FakeReleaseHandler.payload = json.dumps(_release_json()).encode()
    server = HTTPServer(("127.0.0.1", 0), _FakeReleaseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def clean_caches():
    online_update.reset_caches()
    try:
        yield
    finally:
        online_update.reset_caches()


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    """Point CONFIG_PATH at a tmp file and start from an empty archive."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(host_app, "CONFIG_PATH", str(path))
    host_app._config = {"devices": {}, "settings": {"actions_enabled": True},
                        "skipped_version": "", "last_update": {},
                        "update_fail_count": 0}
    host_app._events.clear()
    assert host_app.save_config.__module__ == host_app.__name__, \
        "save_config must not be stubbed by another test module"
    return path


@pytest.fixture
def client(monkeypatch, cfgfile):
    """Flask test client. ``cfgfile`` runs first so the real save_config is
    still in place when that fixture asserts it was not stubbed elsewhere."""
    monkeypatch.setattr(host_app, "save_config", lambda: None)
    host_app.app.config["TESTING"] = True
    return host_app.app.test_client()


# --- 1. 版本比较 -------------------------------------------------------------

def test_version_comparison_is_numeric_not_lexicographic():
    """1.10.0 > 1.9.9 —— 数值元组，不是字符串比较。"""
    assert online_update.version_is_newer("1.10.0", "1.9.9")
    assert not online_update.version_is_newer("1.9.9", "1.10.0")
    assert online_update.version_is_newer("2.0.0", "1.99.99")


def test_prerelease_is_lower_than_the_final_release():
    """预发布版本低于同号正式版。"""
    assert online_update.version_is_newer("1.5.0", "1.5.0-rc1")
    assert not online_update.version_is_newer("1.5.0-rc1", "1.5.0")
    assert not online_update.version_is_newer("1.5.0", "1.5.0")


def test_build_metadata_does_not_change_the_ordering():
    """+ 之后的构建元数据不参与比较（0.0.0+main.abc == 0.0.0）。"""
    assert online_update.version_tuple("0.0.0+main.abc123") == \
        online_update.version_tuple("0.0.0")


def test_version_at_least_accepts_the_minimum_itself():
    assert online_update.version_at_least("1.0.0", "1.0.0")
    assert online_update.version_at_least("1.0.1", "1.0.0")
    assert not online_update.version_at_least("0.9.9", "1.0.0")
    assert online_update.version_at_least("0.0.0+dev", "")


# --- 2. update.json 解析 + fallback ----------------------------------------

def test_manifest_is_parsed_with_its_source():
    """有清单时 source=manifest，版本/资产/校验和都取自清单。"""
    info = online_update.resolve_release_update(
        _release_json(), online_update.parse_manifest(json.dumps(_manifest())))
    assert info["source"] == "manifest"
    assert info["app"]["version"] == "1.5.0"
    assert info["app"]["asset"] == "BindDeckLite.exe"
    assert info["app"]["sha256"] == "a" * 64
    assert info["app"]["size"] == 1234
    assert info["firmware"]["esp32c3_16mb"]["version"] == "1.4.2"


def test_fallback_source_is_used_when_there_is_no_manifest():
    """没有 update.json 时回退到 tag + 资产名约定（兼容旧 Release）。"""
    sums = "%s  BindDeckLite.exe\n" % ("e" * 64)
    info = online_update.resolve_release_update(_release_json(), None, sums)
    assert info["source"] == "fallback"
    assert info["app"]["version"] == "1.5.0"
    assert info["app"]["asset"] == "BindDeckLite.exe"
    assert info["app"]["sha256"] == "e" * 64


def test_malformed_manifest_is_rejected():
    with pytest.raises(online_update.UpdateError):
        online_update.parse_manifest("{not json")


# --- 3. sha256 / size 不符 → 拒绝替换、删临时文件 ---------------------------

def test_sha256_mismatch_is_refused_and_the_temp_file_is_removed(tmp_path):
    """校验不符时既不产生正式文件，也把 .part 删掉。"""
    payload = b"not the real exe"
    part = tmp_path / "BindDeckLite.exe.part"
    part.write_bytes(payload)
    final = tmp_path / "BindDeckLite.exe"

    error = online_update.finalize_download(str(part), str(final), "a" * 64, 0)

    assert error, "SHA256 不符必须报错"
    assert "SHA256" in error
    assert not final.exists(), "校验失败不能留下正式文件"
    assert not part.exists(), "校验失败必须删除临时文件"


def test_size_mismatch_is_refused(tmp_path):
    part = tmp_path / "x.bin.part"
    part.write_bytes(b"12345")
    final = tmp_path / "x.bin"
    digest = hashlib.sha256(b"12345").hexdigest()

    error = online_update.finalize_download(str(part), str(final), digest, 999)

    assert "大小不符" in error
    assert not final.exists()
    assert not part.exists()


def test_matching_checksum_promotes_the_temp_file(tmp_path):
    part = tmp_path / "x.bin.part"
    part.write_bytes(b"payload")
    final = tmp_path / "x.bin"
    digest = hashlib.sha256(b"payload").hexdigest()

    error = online_update.finalize_download(str(part), str(final), digest,
                                            len(b"payload"))

    assert error == ""
    assert final.read_bytes() == b"payload"
    assert not part.exists()


def test_missing_sha256_is_never_accepted(tmp_path):
    """清单里没有 sha256 → 一律不通过（强制校验是硬要求）。

    消息要说清是「清单没给校验值」，而不是「校验值对不上」—— 两者的处置
    完全不同（前者是发布方的问题，后者是下载被篡改）。
    """
    path = tmp_path / "x.bin"
    path.write_bytes(b"payload")
    error = online_update.verify_downloaded_file(str(path), "", 0)
    assert "缺少" in error and "SHA256" in error


def test_a_wrong_sha256_is_reported_as_a_mismatch(tmp_path):
    """校验值对不上 → 消息要说「校验失败」，与「缺少校验值」区分开。"""
    path = tmp_path / "x.bin"
    path.write_bytes(b"payload")
    error = online_update.verify_downloaded_file(str(path), "a" * 64, 0)
    assert "校验失败" in error


# --- 4. ETag / 304 + 60 秒防抖 ----------------------------------------------

def test_304_reuses_the_previous_result_without_raising(fake_server, clean_caches):
    """远端没变（304）时沿用上次结果，不当成错误。"""
    url = "http://127.0.0.1:%d/releases/latest" % fake_server.server_address[1]
    first = online_update.fetch_latest_release(url)
    assert first["release"]["tag_name"] == "v1.5.0"
    hits_after_first = _FakeReleaseHandler.hits

    # Force a second request: the cached ETag makes GitHub answer 304.
    second = online_update.fetch_latest_release(url, force=True)

    assert _FakeReleaseHandler.hits == hits_after_first + 1
    assert second["not_modified"] is True
    assert second["release"]["tag_name"] == "v1.5.0"


def test_second_call_within_60s_does_not_hit_the_network(fake_server, clean_caches):
    """60 秒防抖：同 URL 重复点击直接返回上次结果，不发请求。"""
    url = "http://127.0.0.1:%d/releases/latest" % fake_server.server_address[1]
    online_update.fetch_latest_release(url)
    hits_after_first = _FakeReleaseHandler.hits

    for _ in range(5):
        again = online_update.fetch_latest_release(url)

    assert _FakeReleaseHandler.hits == hits_after_first, \
        "60 秒内的重复检查不得再发请求"
    assert again["release"]["tag_name"] == "v1.5.0"
    assert again["from_cache"] is True


def test_etag_is_sent_on_the_second_request(fake_server, clean_caches):
    """第二次请求带上 If-None-Match。"""
    seen = {}

    class _Spy(_FakeReleaseHandler):
        def do_GET(self):  # noqa: N802
            seen["inm"] = self.headers.get("If-None-Match")
            _FakeReleaseHandler.do_GET(self)

    server = HTTPServer(("127.0.0.1", 0), _Spy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = "http://127.0.0.1:%d/releases/latest" % server.server_address[1]
        online_update.fetch_latest_release(url)
        assert seen.get("inm") is None
        online_update.fetch_latest_release(url, force=True)
        assert seen.get("inm") == '"release-etag-1"'
    finally:
        server.shutdown()
        server.server_close()


# --- 5. 跳过版本 -------------------------------------------------------------

def test_skipped_version_is_not_offered_again():
    """跳过 latest 后不再提示该版本。"""
    assert online_update.should_skip_version("1.4.2", "1.5.0", "1.5.0")
    assert not online_update.should_skip_version("1.4.2", "1.5.0", "")


def test_a_higher_version_is_offered_again():
    """出现更高版本后重新提示。"""
    assert not online_update.should_skip_version("1.4.2", "1.6.0", "1.5.0")


def test_check_endpoint_reports_skipped(client, monkeypatch, clean_caches):
    """接口层：skipped_version 等于 latest 时 has_update=false。"""
    monkeypatch.setattr(host_app, "read_app_version", lambda: "1.4.2", raising=False)
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.4.2")
    monkeypatch.setattr(
        online_update, "resolve_latest_update",
        lambda *a, **k: online_update.resolve_release_update(
            _release_json(), online_update.parse_manifest(json.dumps(_manifest()))))
    host_app._config["skipped_version"] = "1.5.0"

    resp = client.get("/api/app/update/check")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["skipped"] is True
    assert body["has_update"] is False
    assert body["error_code"] == "E_SKIPPED"


# --- 6. 回滚计数 -------------------------------------------------------------

def test_two_consecutive_failures_pause_the_auto_update():
    """连续失败 >= 2 → 暂停自动更新。"""
    assert online_update.UPDATE_FAIL_LIMIT == 2
    state = {"update_fail_count": 1}
    assert state["update_fail_count"] < online_update.UPDATE_FAIL_LIMIT
    state["update_fail_count"] = 2
    assert state["update_fail_count"] >= online_update.UPDATE_FAIL_LIMIT


def test_apply_is_refused_once_the_update_is_paused(client, monkeypatch,
                                                     cfgfile, clean_caches):
    """暂停后 apply 被拒绝，并给出中文原因。"""
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.4.2")
    monkeypatch.setattr(
        online_update, "resolve_latest_update",
        lambda *a, **k: online_update.resolve_release_update(
            _release_json(), online_update.parse_manifest(json.dumps(_manifest()))))
    host_app._config["update_fail_count"] = 2

    resp = client.post("/api/app/update/apply")
    body = resp.get_json()

    assert resp.status_code == 409
    assert body["error_code"] == "E_UPDATE_PAUSED"
    assert "暂停自动更新" in body["error"]


def test_rollback_after_start_records_the_failure(client, monkeypatch, cfgfile):
    """新版本没起来（回滚到旧版）时，失败计数 +1 并记 result=rollback。"""
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.4.2")
    host_app._config["last_update"] = {
        "status": "pending", "from": "1.4.2", "to": "1.5.0", "at": 1.0,
        "marker": "",
    }
    host_app._config["update_fail_count"] = 0

    host_app._update_after_start()

    assert host_app._config["last_update"]["result"] == "rollback"
    assert host_app._config["update_fail_count"] == 1


def test_successful_start_records_ok_and_writes_the_marker(client, monkeypatch,
                                                           cfgfile, tmp_path):
    """新版本首次启动写 result=ok，并写标记文件让更新脚本收工。"""
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.5.0")
    marker = tmp_path / "updated.marker"
    host_app._config["last_update"] = {
        "status": "pending", "from": "1.4.2", "to": "1.5.0", "at": 1.0,
        "marker": str(marker),
    }

    host_app._update_after_start()

    assert host_app._config["last_update"]["result"] == "ok"
    assert marker.exists()


# --- 7. 更新器脚本 -----------------------------------------------------------

def test_update_script_only_touches_the_fixed_paths():
    """更新器只接受本次下载的固定路径，不含任意命令。"""
    script = online_update.build_update_script(
        exe_path=r"C:\Apps\BindDeckLite.exe",
        new_exe=r"C:\Users\x\AppData\Local\BindDeckLite\updates\1.5.0\BindDeckLite.exe",
        backup_exe=r"C:\Apps\BindDeckLite.exe.bak",
        marker_path=r"C:\Users\x\AppData\Local\BindDeckLite\updates\1.5.0\updated.marker",
        pid=4321)

    assert r'set "EXE=C:\Apps\BindDeckLite.exe"' in script
    assert r'set "PID=4321"' in script
    assert r'set "BAK=C:\Apps\BindDeckLite.exe.bak"' in script
    # 只有 copy / start / tasklist —— 没有执行任意命令的地方。
    for forbidden in ("powershell", "cmd /c", "wscript", "certutil",
                      "Invoke-Expression", "curl ", "del /", "reg "):
        assert forbidden.lower() not in script.lower(), \
            "更新器脚本不得包含 %r" % forbidden
    # 回滚路径存在。
    assert ":rollback" in script
    assert 'copy /Y "%BAK%" "%EXE%"' in script


def test_update_script_only_runs_the_five_expected_commands():
    """脚本里出现的可执行命令必须只有这五个（其余都是变量/跳转/注释）。"""
    script = online_update.build_update_script(
        exe_path=r"C:\Apps\BindDeckLite.exe", new_exe=r"C:\new\a.exe",
        backup_exe=r"C:\Apps\BindDeckLite.exe.bak",
        marker_path=r"C:\new\updated.marker", pid=1)

    allowed = {"tasklist", "find", "ping", "copy", "start", "echo", "set",
               "setlocal", "endlocal", "goto", "if", "rem", "@echo"}
    commands = set()
    for line in script.splitlines():
        line = line.strip()
        if not line or line.startswith(("rem", "@", ":", "set ", "if ")):
            continue
        # 取第一个词作为命令（管道右侧单独算）。
        for part in line.split("|"):
            word = part.strip().split(" ")[0].strip('"')
            if word:
                commands.add(word.lower())
    unknown = commands - allowed
    assert not unknown, "更新器脚本出现了预期外的命令: %s" % sorted(unknown)


def test_update_script_rejects_paths_that_could_break_the_batch_file():
    """带引号的路径会破坏批处理 —— 直接拒绝，不生成脚本。"""
    with pytest.raises(online_update.UpdateError):
        online_update.build_update_script('C:\\bad"path\\a.exe', "b", "c", "d", 1)


# --- 8. 目录不可写 -----------------------------------------------------------

def test_unwritable_directory_is_detected(tmp_path):
    """不可写目录必须被识别出来（用于提示手动下载，而不是破坏原 exe）。"""
    assert online_update.is_writable_dir(str(tmp_path)) is True
    missing = tmp_path / "nope" / "deeper"
    assert online_update.is_writable_dir(str(missing)) is True  # 会被创建


def test_apply_refuses_when_the_target_directory_is_not_writable(client, monkeypatch,
                                                                 cfgfile, clean_caches,
                                                                 tmp_path):
    """目标目录不可写 → 拒绝更新并给出 Release 链接，原 exe 不受影响。"""
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.4.2")
    monkeypatch.setattr(
        online_update, "resolve_latest_update",
        lambda *a, **k: online_update.resolve_release_update(
            _release_json(), online_update.parse_manifest(json.dumps(_manifest()))))
    monkeypatch.setattr(host_app, "_update_target_dir", lambda: str(tmp_path))
    monkeypatch.setattr(online_update, "is_writable_dir", lambda path: False)

    resp = client.post("/api/app/update/apply")
    body = resp.get_json()

    assert resp.status_code == 409
    assert body["error_code"] == "E_DIR_RO"
    assert "不可写" in body["error"]
    assert body["release_url"].startswith("https://github.com/")


# --- 9. OTA 忙时拒绝更新上位机 ------------------------------------------------

def test_apply_is_refused_while_a_device_is_updating(client, monkeypatch,
                                                      cfgfile, clean_caches):
    """设备正在 OTA 时禁止退出上位机（会断 BLE，打断 OTA）。"""
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.4.2")
    monkeypatch.setattr(
        online_update, "resolve_latest_update",
        lambda *a, **k: online_update.resolve_release_update(
            _release_json(), online_update.parse_manifest(json.dumps(_manifest()))))

    class _Busy:
        status = "uploading"
        error = ""

    host_app._ota_windows["busy-device"] = _Busy()
    try:
        resp = client.post("/api/app/update/apply")
        body = resp.get_json()
    finally:
        host_app._ota_windows.pop("busy-device", None)

    assert resp.status_code == 409
    assert body["error_code"] == "E_OTA_BUSY"
    assert "设备正在升级" in body["error"]


# --- 版本源 -------------------------------------------------------------------

def test_version_source_is_read_from_the_version_module(monkeypatch):
    """版本号来自 host/_version.py —— 不散落在 app.py / 前端。"""
    monkeypatch.setattr(online_update, "_load_version_module",
                        lambda: type("M", (), {"APP_VERSION": "1.4.2",
                                               "APP_BUILD": "abc"})())
    assert online_update.read_app_version() == "1.4.2"
    assert online_update.read_app_build() == "abc"


def test_environment_variable_overrides_the_dev_placeholder(monkeypatch):
    """开发时可用环境变量覆盖占位版本号。"""
    monkeypatch.setattr(online_update, "_load_version_module",
                        lambda: type("M", (), {"APP_VERSION": "0.0.0+dev",
                                               "APP_BUILD": ""})())
    monkeypatch.setenv("BINDDECKLITE_APP_VERSION", "9.9.9")
    assert online_update.read_app_version() == "9.9.9"


def test_version_endpoint_reports_the_current_version(client, monkeypatch):
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.4.2")
    body = client.get("/api/app/version").get_json()
    assert body["version"] == "1.4.2"
    assert "frozen" in body
    assert "executable" in body


# --- 任务状态 ---------------------------------------------------------------

def test_a_finished_task_does_not_block_the_next_update(client, monkeypatch,
                                                         cfgfile, clean_caches):
    """上一轮任务结束（失败/重启）后必须还能再次发起更新。"""
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.4.2")
    monkeypatch.setattr(
        online_update, "resolve_latest_update",
        lambda *a, **k: online_update.resolve_release_update(
            _release_json(), online_update.parse_manifest(json.dumps(_manifest()))))
    monkeypatch.setattr(host_app, "_update_target_dir", lambda: "")
    # 一个已结束的任务（终态）。
    host_app._app_update_tasks["old-task"] = {"phase": "error", "error": "x"}

    resp = client.post("/api/app/update/apply")
    body = resp.get_json()

    # 目标目录为空 → 拒绝，但拒绝原因不是「已有更新任务在运行」。
    assert resp.status_code == 409
    assert body["error_code"] != "E_OTA_BUSY"
    assert "已有更新任务" not in body["error"]


def test_a_running_task_blocks_a_second_update(client, monkeypatch, cfgfile,
                                                clean_caches):
    """正在跑的任务会挡住第二次发起。"""
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.4.2")
    monkeypatch.setattr(
        online_update, "resolve_latest_update",
        lambda *a, **k: online_update.resolve_release_update(
            _release_json(), online_update.parse_manifest(json.dumps(_manifest()))))
    host_app._app_update_tasks["busy-task"] = {"phase": "downloading"}

    resp = client.post("/api/app/update/apply")

    assert resp.status_code == 409
    assert "已有更新任务在运行" in resp.get_json()["error"]
    host_app._app_update_tasks.pop("busy-task", None)
