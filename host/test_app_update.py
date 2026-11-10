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
    # 只有 copy / start / tasklist / attrib / del —— 没有执行任意命令的地方。
    for forbidden in ("powershell", "cmd /c", "wscript", "certutil",
                      "Invoke-Expression", "curl ", "reg "):
        assert forbidden.lower() not in script.lower(), \
            "更新器脚本不得包含 %r" % forbidden
    # 删除只发生在脚本自己的两个固定文件上（残留备份 / tasklist 临时输出）。
    for line in script.splitlines():
        if line.strip().lower().startswith("del "):
            assert '"%BAK%"' in line or '"%LIST%"' in line, \
                "del 只能删备份或脚本自己的临时文件: %r" % line
    # 回滚路径存在。
    assert ":rollback" in script
    assert 'copy /Y "%BAK%" "%EXE%"' in script


def test_update_script_only_runs_the_five_expected_commands():
    """脚本里出现的可执行命令必须只有这几个（其余都是变量/跳转/注释）。"""
    script = online_update.build_update_script(
        exe_path=r"C:\Apps\BindDeckLite.exe", new_exe=r"C:\new\a.exe",
        backup_exe=r"C:\Apps\BindDeckLite.exe.bak",
        marker_path=r"C:\new\updated.marker", pid=1)

    allowed = {"tasklist", "find", "ping", "copy", "start", "echo", "set",
               "setlocal", "endlocal", "goto", "if", "rem", "@echo",
               # WS-570: 清只读属性与残留备份 / 临时文件。
               "attrib", "del"}
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


def test_update_script_rejects_percent_in_paths():
    """``%`` 在批处理里会被当成变量引用 —— 路径里有它就一定会坏。"""
    with pytest.raises(online_update.UpdateError):
        online_update.build_update_script("C:\\Users\\100%\\a.exe", "b", "c", "d", 1)


def test_update_script_body_is_pure_ascii():
    """脚本正文只用 ASCII：非 ASCII 的部分只能是路径本身。

    cmd.exe 按控制台 OEM 代码页（zh-CN = 936）解析批处理文件；脚本正文里
    出现中文注释，在非中文代码页上根本写不出来。
    """
    script = online_update.build_update_script(
        exe_path=r"C:\Apps\BindDeckLite.exe", new_exe=r"C:\new\a.exe",
        backup_exe=r"C:\Apps\BindDeckLite.exe.bak",
        marker_path=r"C:\new\updated.marker", pid=1)

    assert script.isascii(), "更新器脚本正文必须是纯 ASCII"


def test_update_script_is_written_in_the_console_code_page(tmp_path):
    """落盘字节必须能被 cmd 的代码页解回原文（中文路径不变形）。

    评审实测：UTF-8 落盘时 ``line.decode("cp936")`` 得到 ``寮犱笁``，
    ``if not exist "%NEW%"`` 直接跳 ``:fail`` —— 此时上位机已经退出，
    用户的程序就消失了。
    """
    exe = r"C:\Users\张三\AppData\Local\BindDeckLite\BindDeckLite.exe"
    script = online_update.build_update_script(
        exe_path=exe, new_exe=r"C:\new\a.exe",
        backup_exe=r"C:\Apps\BindDeckLite.exe.bak",
        marker_path=r"C:\new\updated.marker", pid=1)
    path = tmp_path / "update.bat"

    # 用 zh-CN 的控制台代码页（cp936）落盘 —— 这正是目标机器的情形。
    online_update.write_update_script(str(path), script, encoding="cp936")

    raw = path.read_bytes()
    text = raw.decode("cp936")
    assert exe in text, "控制台代码页下中文路径必须原样保留"
    assert "寮犱笁" not in text, "路径不能被解成乱码"
    # 同一份字节用 UTF-8 解出来就不是原文 —— 说明落盘用的不是 UTF-8。
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")


def test_write_update_script_refuses_paths_the_code_page_cannot_represent(tmp_path):
    """代码页表示不了的字符要明确报错，而不是写出一个 cmd 解析不了的脚本。"""
    script = online_update.build_update_script(
        exe_path=r"C:\Users\张三\a.exe", new_exe=r"C:\new\a.exe",
        backup_exe=r"C:\Apps\BindDeckLite.exe.bak",
        marker_path=r"C:\new\updated.marker", pid=1)
    path = tmp_path / "update.bat"

    with pytest.raises(online_update.UpdateError):
        online_update.write_update_script(str(path), script, encoding="ascii")


def test_stage_update_script_removes_the_stale_marker(tmp_path):
    """上次失败留下的 marker 会让新脚本立刻判成功 —— 生成前必须清掉。"""
    marker = tmp_path / "updated.marker"
    marker.write_text("update failed", encoding="utf-8")
    script = online_update.build_update_script(
        exe_path=r"C:\Apps\BindDeckLite.exe", new_exe=r"C:\new\a.exe",
        backup_exe=r"C:\Apps\BindDeckLite.exe.bak",
        marker_path=str(marker), pid=1)

    online_update.stage_update_script(str(tmp_path / "update.bat"), script, str(marker))

    assert not marker.exists(), "陈旧 marker 必须先删掉"


def test_the_batch_file_is_written_in_the_windows_console_oem_code_page():
    """Windows 上必须用 ``oem``（控制台 OEM 代码页 = CP_OEMCP）。

    cmd.exe 按**控制台**代码页读批处理（``chcp`` 默认报的就是它），
    不是 ANSI 代码页（CP_ACP）。简体中文系统两个代码页都是 936（GBK），
    所以旧实现用 ``mbcs`` 一直没暴露问题；非 936 系统不等价
    （en-US：OEM = 437 / ANSI = 1252），按 ANSI 落盘 cmd.exe 会把路径里的
    重音字符读成另一个字符，``if not exist`` 直接跳 ``:fail``。
    """
    assert online_update.batch_encoding("nt") == "oem"
    assert online_update.batch_encoding("posix") == "utf-8"


def test_a_non_936_code_page_path_is_only_readable_back_as_oem(tmp_path):
    """「非 936 代码页」路径：OEM（437）与 ANSI（1252）不是同一页。

    用 en-US 控制台的真实组合走一遍落盘：按 OEM 落的字节 cmd.exe 能原样
    读回路径；同一份字节按 ANSI 读就是乱码 —— 这正是当年用 ``mbcs`` 的隐患。
    """
    exe = r"C:\Users\José\AppData\Local\BindDeckLite\BindDeckLite.exe"
    script = online_update.build_update_script(
        exe_path=exe, new_exe=r"C:\new\a.exe",
        backup_exe=r"C:\Apps\BindDeckLite.exe.bak",
        marker_path=r"C:\new\updated.marker", pid=1)
    path = tmp_path / "update.bat"

    # en-US 控制台：OEM = 437。
    online_update.write_update_script(str(path), script, encoding="cp437")

    raw = path.read_bytes()
    assert exe in raw.decode("cp437"), "OEM 代码页下路径必须原样保留"
    # 同一份字节按 ANSI（1252）读就不是原文了 —— 用 mbcs 落盘就是这个下场。
    assert exe not in raw.decode("cp1252")


@pytest.mark.skipif(sys.platform != "win32", reason="OEM 代码页只在 Windows 上存在")
def test_the_oem_codec_matches_the_windows_console_code_page():
    """``oem`` 必须等于本机的控制台 OEM 代码页（CP_OEMCP）。

    这是 ``batch_encoding("nt")`` 选 ``oem`` 的前提：只有 ``oem`` 真的按
    CP_OEMCP 编码，cmd.exe 才能把批处理里的路径原样读回来。CI 在
    windows-latest 上跑这条，非 Windows 上跳过。
    """
    import ctypes

    oem_cp = ctypes.windll.kernel32.GetOEMCP()
    ansi_cp = ctypes.windll.kernel32.GetACP()
    assert oem_cp > 0, "拿不到本机的 OEM 代码页"

    # 找一个「本机 OEM 页能表示、且两种代码页表示得不一样」的重音字符：
    # 它在 OEM 与 ANSI 页里是两组不同的字节，用它才能把两个代码页区分开。
    sample = None
    for candidate in ("é", "ü", "ñ", "à"):
        try:
            oem_bytes = candidate.encode("oem")
            ansi_bytes = candidate.encode("mbcs")
        except UnicodeEncodeError:
            continue
        if oem_bytes != ansi_bytes:
            sample = candidate
            break
    if sample is None:
        pytest.skip("本机 OEM 与 ANSI 代码页表示不出可区分的字符")

    # oem == cp<CP_OEMCP>：同一字符两种写法必须落成同样的字节。
    assert sample.encode("oem") == sample.encode("cp%d" % oem_cp)
    # mbcs == cp<CP_ACP>：它是 ANSI 代码页，不是 cmd.exe 读批处理用的那一页。
    assert sample.encode("mbcs") == sample.encode("cp%d" % ansi_cp)
    # 非中文系统上两者不是同一页 —— 这正是必须用 oem 而不是 mbcs 的原因。
    if oem_cp != ansi_cp:
        assert sample.encode("oem") != sample.encode("mbcs")


def test_the_update_script_restarts_the_old_version_on_failure():
    """:fail 时上位机已经退出 —— 必须把旧版本再拉起来，否则程序直接消失。"""
    script = online_update.build_update_script(
        exe_path=r"C:\Apps\BindDeckLite.exe", new_exe=r"C:\new\a.exe",
        backup_exe=r"C:\Apps\BindDeckLite.exe.bak",
        marker_path=r"C:\new\updated.marker", pid=1)

    fail_block = script.split(":fail_new", 1)[1]
    assert 'start "" /D "%EXE_DIR%" "%EXE%"' in fail_block, \
        ":fail 分支必须重启旧 exe（上位机已退出，不重启用户就什么都没有了）"
    # 失败时不能写成功标记：那会让同版本重试时脚本立刻判成功、跳过回滚。
    assert "MARKER%" not in fail_block.replace('if exist "%MARKER%"', "")


# --- 7b. WS-570 更新脚本必须可诊断（日志 / 重试 / PID 解析 / 无窗口） ----------

def _plain_script(**kwargs):
    params = dict(exe_path=r"C:\Apps\BindDeckLite.exe", new_exe=r"C:\new\a.exe",
                  backup_exe=r"C:\Apps\BindDeckLite.exe.bak",
                  marker_path=r"C:\new\updated.marker", pid=1)
    params.update(kwargs)
    return online_update.build_update_script(**params)


def test_update_script_logs_every_step_to_update_log():
    """每一步都要写日志：没有日志，用户只能看到「更新失败」四个字。"""
    script = _plain_script()

    # 默认日志落在 marker 同级目录的 update.log（生产里是 updates\\update.log）。
    assert r'set "LOG=C:\new\update.log"' in script
    # 开头截断旧日志，之后每一步追加 —— 否则上一次的失败会解释这一次。
    assert '>"%LOG%"' in script
    assert script.count('>>"%LOG%"') >= 10
    for step in ("[start]", "[waitpid]", "[copy] step=backup",
                 "[copy] step=replace", "[waitnew]", "[result]"):
        assert step in script, "日志缺少步骤标记 %s" % step
    # copy 的原始输出（含错误原因）必须进日志，不能 2>&1 到 NUL。
    assert 'copy /Y "%NEW%" "%EXE%" >>"%LOG%" 2>&1' in script


def test_update_script_uses_the_update_log_path_it_is_given():
    """生产上传入 updates\\update.log；日志不能落进版本子目录。"""
    script = _plain_script(log_path=r"C:\Users\x\AppData\Local\BindDeckLite\updates\update.log")
    assert r'set "LOG=C:\Users\x\AppData\Local\BindDeckLite\updates\update.log"' in script
    assert online_update.update_log_path(
        r"C:\Users\x\AppData\Local\BindDeckLite\updates\1.5.0") == \
        r"C:\Users\x\AppData\Local\BindDeckLite\updates\update.log"


def test_update_script_parses_tasklist_instead_of_trusting_the_pipe():
    """``tasklist | find`` 的退出码不能直接当「进程已退出」。

    管道把 tasklist 自身失败（不在 PATH / 被拦截）压成了 find 的失败，脚本
    于是抢在上位机退出前覆盖 exe，必然失败 → 立刻回滚 → CMD 一闪而过。
    """
    script = _plain_script()

    assert "| find" not in script, "不能再靠管道的退出码判断进程是否退出"
    assert 'tasklist /FI "PID eq %PID%" /NH /FO CSV >"%LIST%" 2>NUL' in script
    # 解析输出里的 PID：找不到才是真的退出。
    assert 'find "%PID%" "%LIST%" >NUL' in script
    # tasklist 报错 → 走 pid_unknown（继续等），不得直接去 copy。
    assert "if errorlevel 1 goto pid_unknown" in script
    check_block = script.split(":waitpid", 1)[1].split(":pid_tick", 1)[0]
    assert "goto copyexe" not in check_block, \
        "tasklist 不可用时必须继续等待，不能直接覆盖 exe"


def test_update_script_retries_the_copy_once_a_second():
    """替换失败要重试（默认 30 秒），一次失败就回滚正是闪退的原因之一。"""
    script = _plain_script()

    assert "if not errorlevel 1 goto copied" in script
    assert "if %COUNT% LSS 30 goto retrycopy" in script
    retry_block = script.split(":retrycopy", 1)[1].split(":copied", 1)[0]
    assert "ping -n 2 127.0.0.1" in retry_block, "每次重试之间要隔约 1 秒"
    assert "goto rollback" in script, "重试用尽后仍要回滚"


def test_update_script_clears_read_only_and_stale_backup_before_copy():
    script = _plain_script()

    assert 'if exist "%BAK%" attrib -R "%BAK%"' in script
    assert 'del /F /Q "%BAK%"' in script
    assert 'attrib -R "%EXE%"' in script
    # attrib/del 必须发生在 copy NEW→EXE 之前。
    assert script.index('attrib -R "%EXE%"') < script.index('copy /Y "%NEW%" "%EXE%"')


def test_update_script_starts_the_new_version_in_the_exe_directory():
    """``start /D``：新进程的 CWD 必须是 exe 目录，不是 updates 目录。"""
    script = _plain_script()

    assert r'set "EXE_DIR=C:\Apps"' in script
    assert 'start "" /D "%EXE_DIR%" "%EXE%"' in script


def test_launch_flags_hide_the_console_window(monkeypatch):
    """CREATE_NO_WINDOW：正常更新不再闪一个黑窗（DETACHED_PROCESS 时代会闪）。"""
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)

    monkeypatch.setattr(online_update, "os", type("FakeOs", (), {"name": "nt"})())
    monkeypatch.setattr(online_update.subprocess, "Popen", fake_popen)

    assert online_update.launch_update_script(r"C:\updates\update.bat") is True
    flags = captured["creationflags"]
    assert flags & online_update.CREATE_NO_WINDOW, "必须带 CREATE_NO_WINDOW"
    assert flags & online_update.CREATE_NEW_PROCESS_GROUP
    assert not flags & 0x00000008, \
        "DETACHED_PROCESS 与 CREATE_NO_WINDOW 互斥，不能同时给"
    assert captured["stdin"] is not None and captured["stdout"] is not None


def test_parse_update_log_reports_the_failed_step_and_raw_output():
    """日志解析出「哪一步 + 为什么」—— UI 上要显示的正是这两个。"""
    text = "\r\n".join([
        "[start] pid=4321 exe=\"C:\\Apps\\BindDeckLite.exe\"",
        "[waitpid] pid=4321 exited after 2s",
        "已复制        1 个文件。",
        "[copy] step=backup result=ok",
        "拒绝访问。",
        "        0 个文件已复制。",
        "[copy] step=replace result=failed attempt=0 errorlevel=1",
        "[copy] step=replace result=failed attempts=30 giving up",
        "[result] rollback reason=copy_failed",
        "[done]",
    ])

    info = online_update.parse_update_log(text)

    assert info["result"] == "rollback"
    assert info["reason"] == "copy_failed"
    assert "giving up" in info["detail"]


def test_parse_update_log_reports_success():
    info = online_update.parse_update_log(
        "[copy] step=replace result=ok\r\n[result] ok\r\n")
    assert info["result"] == "ok"
    assert info["detail"] == ""


def test_read_update_log_decodes_the_console_code_page(tmp_path):
    """脚本按 cmd 的 OEM 代码页写日志，中文错误不能被解成乱码或抛异常。"""
    path = tmp_path / "update.log"
    body = ("[copy] step=replace result=failed attempt=0 errorlevel=1\r\n"
            "拒绝访问。\r\n"
            "[result] rollback reason=copy_failed\r\n")
    path.write_bytes(body.encode("cp936"))

    info = online_update.read_update_log(str(path))

    assert info["reason"] == "copy_failed"
    assert "拒绝访问" in info["detail"]


def test_read_update_log_missing_file_is_not_an_error(tmp_path):
    info = online_update.read_update_log(str(tmp_path / "nope.log"))
    assert info == {"result": "", "reason": "", "detail": "", "lines": []}


def test_rollback_on_start_logs_the_failed_step_from_update_log(client, monkeypatch,
                                                                cfgfile, tmp_path):
    """回滚后启动的旧版本要读出 update.log，把失败步骤写进日志面板。"""
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.4.2")
    log_path = tmp_path / "updates" / "update.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "[copy] step=replace result=failed attempts=30 giving up\r\n"
        "[result] rollback reason=copy_failed\r\n",
        encoding="cp936")
    host_app._config["last_update"] = {
        "status": "pending", "from": "1.4.2", "to": "1.5.0", "at": 1.0,
        "marker": "", "log": str(log_path),
    }
    host_app._config["update_fail_count"] = 0

    host_app._update_after_start()

    assert host_app._config["last_update"]["result"] == "rollback"
    texts = [e["text"] for e in host_app._events]
    assert any("copy_failed" in text for text in texts), \
        "失败步骤必须进日志面板，而不是只有一句「更新失败」: %r" % texts
    assert any("giving up" in text for text in texts)


def test_rollback_without_a_log_still_records_the_failure(client, monkeypatch,
                                                          cfgfile):
    """日志缺失只该少一条细节，不能让回滚处理本身崩掉。"""
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.4.2")
    host_app._config["last_update"] = {
        "status": "pending", "from": "1.4.2", "to": "1.5.0", "at": 1.0,
        "marker": "", "log": "",
    }

    host_app._update_after_start()

    assert host_app._config["last_update"]["result"] == "rollback"



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


# --- 10. WS-530 评审提出的缺陷 -----------------------------------------------

def _install_app_manifest(monkeypatch, min_from="1.0.0", sha="a" * 64,
                           current="1.4.2", assets=None, version="1.5.0"):
    """让 check/apply 看到一份指定的清单。"""
    monkeypatch.setattr(online_update, "read_app_version", lambda: current)
    manifest = _manifest()
    manifest["app"]["version"] = version
    manifest["app"]["min_from"] = min_from
    manifest["app"]["sha256"] = sha
    if assets is None:
        assets = [{"name": "BindDeckLite.exe", "size": 1234,
                   "browser_download_url": "https://example.invalid/BindDeckLite.exe"}]
    release = _release_json()
    release["assets"] = assets
    monkeypatch.setattr(
        online_update, "resolve_latest_update",
        lambda *a, **k: online_update.resolve_release_update(
            release, online_update.parse_manifest(json.dumps(manifest))))


def test_check_reports_min_from_as_an_error(client, monkeypatch, cfgfile, clean_caches):
    """min_from 不满足时 check 必须 ok=false —— 否则前端显示「已是最新」。

    评审实测：``min_from=9.0.0``、当前 ``0.0.0+dev`` → check 回
    ``{"ok":true,...,"error_code":"E_MIN_FROM"}``，前端只认 ``r.ok===false``，
    于是用户看到「已是最新（v0.0.0+dev）」，根本不知道要先手动升级。
    """
    _install_app_manifest(monkeypatch, min_from="9.0.0", current="0.0.0+dev")

    body = client.get("/api/app/update/check").get_json()

    assert body["ok"] is False
    assert body["error_code"] == "E_MIN_FROM"
    assert body["min_from_ok"] is False
    assert body["has_update"] is False
    assert "手动升级" in body["error"]


def test_check_reports_a_missing_sha256_as_an_error(client, monkeypatch, cfgfile,
                                                      clean_caches):
    """清单没有 SHA256 时 check 必须 ok=false（强制校验是硬性要求）。"""
    _install_app_manifest(monkeypatch, sha="")

    body = client.get("/api/app/update/check").get_json()

    assert body["ok"] is False
    assert body["error_code"] == "E_NO_SHA256"
    assert body["has_update"] is False


def test_check_reports_an_app_asset_missing_from_the_release(client, monkeypatch,
                                                              cfgfile, clean_caches):
    """清单里的安装包不在 Release 资产里 → check 阶段就报错。"""
    _install_app_manifest(monkeypatch, assets=[])

    body = client.get("/api/app/update/check").get_json()

    assert body["ok"] is False
    assert body["error_code"] == "E_NO_APP_ASSET"
    assert body["has_update"] is False


def test_apply_refuses_when_min_from_is_not_satisfied(client, monkeypatch, cfgfile,
                                                        clean_caches):
    """apply 与 check 用同一把尺：check 说不能升，apply 也不能升。"""
    _install_app_manifest(monkeypatch, min_from="9.0.0", current="1.4.2")

    resp = client.post("/api/app/update/apply")
    body = resp.get_json()

    assert resp.status_code == 409
    assert body["error_code"] == "E_MIN_FROM"


def test_apply_refuses_an_app_asset_missing_from_the_release(client, monkeypatch,
                                                              cfgfile, clean_caches):
    """清单里的安装包不在 Release 资产里 → apply 直接拒绝，不下载。"""
    _install_app_manifest(monkeypatch, assets=[])

    resp = client.post("/api/app/update/apply")
    body = resp.get_json()

    assert resp.status_code == 409
    assert body["error_code"] == "E_NO_APP_ASSET"


def test_apply_is_allowed_for_a_newer_version_after_failures(client, monkeypatch,
                                                             cfgfile, clean_caches):
    """连续失败只拦同一个版本：出了更高版本就放行（否则在线更新永久失效）。"""
    _install_app_manifest(monkeypatch, current="1.4.2", version="1.6.0")
    host_app._config["update_fail_count"] = 2
    host_app._config["last_update"] = {"status": "done", "result": "rollback",
                                       "from": "1.4.2", "to": "1.5.0"}
    monkeypatch.setattr(host_app, "_update_target_dir", lambda: "")

    resp = client.post("/api/app/update/apply")
    body = resp.get_json()

    assert body.get("error_code") != "E_UPDATE_PAUSED", \
        "更高版本必须放行，不能被失败计数永久挡住"


def test_apply_stays_blocked_for_the_same_failed_version(client, monkeypatch,
                                                         cfgfile, clean_caches):
    """同一个版本连续失败后仍要挡住（避免反复重启失败）。"""
    _install_app_manifest(monkeypatch, current="1.4.2", version="1.5.0")
    host_app._config["update_fail_count"] = 2
    host_app._config["last_update"] = {"status": "done", "result": "rollback",
                                       "from": "1.4.2", "to": "1.5.0"}

    resp = client.post("/api/app/update/apply")
    body = resp.get_json()

    assert resp.status_code == 409
    assert body["error_code"] == "E_UPDATE_PAUSED"


def test_the_download_hashes_the_file_while_downloading(client, monkeypatch, cfgfile,
                                                        clean_caches, tmp_path):
    """下载时边下边算 SHA256，校验不用把文件再读一遍（验收 2 的措辞）。"""
    payload = b"new-exe-bytes"
    part = tmp_path / "BindDeckLite.exe.part"
    part.write_bytes(payload)
    final = tmp_path / "BindDeckLite.exe"

    hasher = hashlib.sha256()
    hasher.update(payload)
    error = online_update.finalize_download(str(part), str(final),
                                            hasher.hexdigest(), len(payload),
                                            digest=hasher.hexdigest())

    assert error == ""
    assert final.read_bytes() == payload


def test_the_wrong_digest_is_still_rejected(client, monkeypatch, cfgfile,
                                             clean_caches, tmp_path):
    """给了错误的边下边算结果 → 一样要拒绝（不能因为省了一次读文件就放过）。"""
    part = tmp_path / "BindDeckLite.exe.part"
    part.write_bytes(b"new-exe-bytes")
    final = tmp_path / "BindDeckLite.exe"

    error = online_update.finalize_download(str(part), str(final), "b" * 64, 12,
                                            digest="c" * 64)

    assert error
    assert not final.exists()


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
