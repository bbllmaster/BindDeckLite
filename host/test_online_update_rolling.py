"""WS-557: 上位机在线更新支持 rolling 通道的回归测试。

覆盖需求单：

1. ``stable`` 只认正式 ``v*`` Release，不碰 ``main-*`` prerelease；
2. ``rolling`` 从 ``/releases`` 里筛 ``main-YYYYMMDD-HHMM`` 取最新一条，
   找不到返回 ``E_NO_ROLLING``（不回退 stable）；
3. 滚动通道用构建标识比较（tag 时间戳 / ``app.build``），同 tag / 同 build
   不再提示；
4. ``skipped_version`` 在滚动通道按 tag 记录，下一个 nightly 仍会提示；
5. 通道切换持久化到 ``config.json``，非法值被拒绝。

所有断言都调用**生产函数**（``online_update`` / ``app`` 的 Flask 路由），
测试里不复制筛选、排序或版本比较逻辑 —— 复制出来的测试改生产代码也不会
失败，等于没有测试。

Run with::

    pytest host/test_online_update_rolling.py
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as host_app  # noqa: E402
import online_update  # noqa: E402


# --- fixtures ---------------------------------------------------------------


def _rolling_release(tag, published, build="", draft=False, base_url=""):
    """一条 ``main-*`` 预发布（默认带一个可下载的 exe 资产）。

    ``base_url`` 给了就再挂一个 ``update.json`` 资产，让接口层测试能走真实
    的「清单优先」路径。
    """
    assets = [{
        "name": "BindDeckLite.exe",
        "size": 4096,
        "browser_download_url": "https://example.invalid/BindDeckLite.exe",
    }]
    if base_url:
        assets.append({
            "name": "update.json",
            "size": 300,
            "browser_download_url": base_url + "/update.json",
        })
    return {
        "tag_name": tag,
        "prerelease": True,
        "draft": draft,
        "published_at": published,
        "created_at": published,
        "body": "## 本次更新\n### 新增\n- rolling",
        "html_url": "https://github.com/bbllmaster/BindDeckLite/releases/tag/" + tag,
        "assets": assets,
    }


def _manifest_json(version="0.0.0+main.news2222"):
    return {
        "app": {
            "version": version,
            "asset": "BindDeckLite.exe",
            "sha256": "a" * 64,
            "size": 4096,
            "min_from": "0.0.0",
        },
        "firmware": {},
        "notes": "## 本次更新\n### 新增\n- nightly",
        "published_at": "2026-11-09T03:14:00Z",
    }


def _formal_release(tag="v1.5.0", published="2026-11-01T00:00:00Z",
                    manifest_url=""):
    assets = [{
        "name": "BindDeckLite.exe",
        "size": 4096,
        "browser_download_url": "https://example.invalid/BindDeckLite.exe",
    }]
    if manifest_url:
        assets.append({"name": "update.json", "size": 300,
                       "browser_download_url": manifest_url})
    return {
        "tag_name": tag,
        "prerelease": False,
        "draft": False,
        "published_at": published,
        "created_at": published,
        "body": "## 本次更新\n### 新增\n- stable",
        "html_url": "https://github.com/bbllmaster/BindDeckLite/releases/tag/" + tag,
        "assets": assets,
    }


#: 混合列表：正式版 / 非 main 的预发布 / 两个 main nightly / draft。
MIXED_RELEASES = [
    _formal_release("v1.5.0", "2026-11-01T00:00:00Z"),
    {
        "tag_name": "v1.6.0-rc1",
        "prerelease": True,
        "draft": False,
        "published_at": "2026-11-09T05:00:00Z",
        "assets": [],
    },
    _rolling_release("main-20261108-0314", "2026-11-08T03:14:00Z"),
    _rolling_release("main-20261109-0314", "2026-11-09T03:14:00Z"),
    _rolling_release("main-20261110-0314", "2026-11-10T03:14:00Z", draft=True),
]


class _ReleaseHandler(BaseHTTPRequestHandler):
    """本地 GitHub API 替身：``/releases/latest``、``/releases?per_page=30``、清单。"""

    latest_payload = b"{}"
    list_payload = b"[]"
    manifest_payload = b"{}"
    stable_manifest_payload = b"{}"
    latest_hits = 0
    list_hits = 0

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.startswith("/releases/latest"):
            type(self).latest_hits += 1
            body = type(self).latest_payload
        elif self.path.startswith("/releases"):
            type(self).list_hits += 1
            body = type(self).list_payload
        elif self.path.startswith("/update-stable.json"):
            body = type(self).stable_manifest_payload
        elif self.path.startswith("/update.json"):
            body = type(self).manifest_payload
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence the server log
        pass


@pytest.fixture
def api_server():
    _ReleaseHandler.latest_hits = 0
    _ReleaseHandler.list_hits = 0
    _ReleaseHandler.manifest_payload = json.dumps(_manifest_json()).encode()
    _ReleaseHandler.stable_manifest_payload = json.dumps(
        _manifest_json("1.5.0")).encode()
    server = HTTPServer(("127.0.0.1", 0), _ReleaseHandler)
    base = "http://127.0.0.1:%d" % server.server_address[1]
    _ReleaseHandler.latest_payload = json.dumps(
        _formal_release(manifest_url=base + "/update-stable.json")).encode()
    # 列表里的 nightly 带上本地清单资产的 URL（走真实的清单解析路径）。
    _ReleaseHandler.list_payload = json.dumps([
        _formal_release("v1.5.0", "2026-11-01T00:00:00Z"),
        {
            "tag_name": "v1.6.0-rc1",
            "prerelease": True,
            "draft": False,
            "published_at": "2026-11-09T05:00:00Z",
            "assets": [],
        },
        _rolling_release("main-20261108-0314", "2026-11-08T03:14:00Z",
                         base_url=base),
        _rolling_release("main-20261109-0314", "2026-11-09T03:14:00Z",
                         base_url=base),
        _rolling_release("main-20261110-0314", "2026-11-10T03:14:00Z",
                         draft=True, base_url=base),
    ]).encode()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    online_update.reset_caches()
    try:
        yield {"latest": base + "/releases/latest",
               "list": base + "/releases?per_page=30"}
    finally:
        online_update.reset_caches()
        server.shutdown()
        server.server_close()


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setattr(host_app, "CONFIG_PATH", str(path))
    host_app._config = {
        "devices": {}, "settings": {"actions_enabled": True},
        "skipped_version": "", "last_update": {}, "update_fail_count": 0,
        "update_channel": "stable", "lang": "system",
    }
    host_app._events.clear()
    return path


@pytest.fixture
def client(monkeypatch, cfgfile):
    monkeypatch.setattr(host_app, "save_config", lambda: None)
    host_app.app.config["TESTING"] = True
    return host_app.app.test_client()


# --- 1. 通道归一 -------------------------------------------------------------


def test_normalize_channel_only_accepts_the_two_channels():
    assert online_update.normalize_channel("rolling") == "rolling"
    assert online_update.normalize_channel("ROLLING") == "rolling"
    assert online_update.normalize_channel("stable") == "stable"
    assert online_update.normalize_channel("") == "stable"
    assert online_update.normalize_channel(None) == "stable"
    assert online_update.normalize_channel("nightly") == "stable"


def test_rolling_tag_time_parses_the_pipeline_tag():
    assert online_update.rolling_tag_time("main-20261109-0314") == 202611090314
    assert online_update.rolling_tag_time("main-20261108-2314") < \
        online_update.rolling_tag_time("main-20261109-0314")
    for bad in ("main-2026-11-09", "v1.5.0", "main-20261109", ""):
        assert online_update.rolling_tag_time(bad) == 0


# --- 2. Release 解析分流 -----------------------------------------------------


def test_rolling_picks_the_newest_main_prerelease():
    chosen = online_update.pick_rolling_release(MIXED_RELEASES)
    assert chosen["tag_name"] == "main-20261109-0314", \
        "必须忽略 v* 正式版、v1.6.0-rc1 与 draft"


def test_rolling_ordering_uses_published_at_not_list_order():
    shuffled = list(reversed(MIXED_RELEASES))
    assert online_update.pick_rolling_release(shuffled)["tag_name"] == \
        "main-20261109-0314"


def test_rolling_without_any_nightly_is_an_error():
    with pytest.raises(online_update.UpdateError) as exc:
        online_update.pick_rolling_release([_formal_release()])
    assert exc.value.code == "E_NO_ROLLING"


def test_stable_channel_only_asks_for_the_formal_release(api_server):
    """stable 走 releases/latest，不去拉列表 —— 正式版里不会有 main-*。"""
    info = online_update.resolve_latest_update(
        api_url=api_server["latest"], releases_api_url=api_server["list"])

    assert info["channel"] == "stable"
    assert info["tag"] == "v1.5.0"
    assert _ReleaseHandler.latest_hits == 1
    assert _ReleaseHandler.list_hits == 0, \
        "stable 通道不得请求 /releases 列表（会看到 nightly）"


def test_rolling_channel_reads_the_list_and_returns_the_tag(api_server):
    info = online_update.resolve_latest_update(
        channel="rolling", api_url=api_server["latest"],
        releases_api_url=api_server["list"])

    assert info["channel"] == "rolling"
    assert info["tag"] == "main-20261109-0314"
    assert _ReleaseHandler.list_hits == 1
    assert _ReleaseHandler.latest_hits == 0, \
        "rolling 通道不得用 releases/latest（它按定义排除 prerelease）"


def test_stable_channel_refuses_a_non_v_release(api_server):
    """即使 GitHub 把 main-* 标成非预发布，稳定通道也不能收下。"""
    _ReleaseHandler.latest_payload = json.dumps(
        _formal_release("main-20261109-0314")).encode()
    with pytest.raises(online_update.UpdateError) as exc:
        online_update.resolve_latest_update(api_url=api_server["latest"])
    assert exc.value.code == "E_NO_STABLE"


# --- 3. 滚动版本比较 ---------------------------------------------------------


def test_rolling_compares_tag_timestamps():
    assert online_update.rolling_is_newer(
        "main-20261109-0314", "", "main-20261108-0314", "")
    assert not online_update.rolling_is_newer(
        "main-20261108-0314", "", "main-20261109-0314", "")
    assert not online_update.rolling_is_newer(
        "main-20261109-0314", "", "main-20261109-0314", "")


def test_the_same_build_identifier_is_not_an_update():
    """本地 exe 就是用这个 build 构建的 → 不重复下载。"""
    assert not online_update.rolling_is_newer(
        "main-20261109-0314", "abc1234", "", "abc1234")
    assert online_update.rolling_is_newer(
        "main-20261109-0314", "abc1234", "", "def5678")


def test_a_local_build_without_a_comparable_stamp_is_offered_the_nightly():
    """老版本 / 手工构建没有构建标识 → 先升到当前最新 nightly。"""
    assert online_update.rolling_is_newer("main-20261109-0314", "", "", "")


def test_semver_never_decides_the_rolling_channel():
    """所有 nightly 的 semver 都是 0.0.0+main.<sha> —— 用它比会永远「已最新」。"""
    info = {"tag": "main-20261109-0314",
            "app": {"version": "0.0.0+main.abc1234", "build": ""}}
    assert online_update.has_app_update(
        "rolling", info, "0.0.0+main.abc1234", local_build="", local_tag="")
    assert not online_update.has_app_update(
        "rolling", info, "0.0.0+main.abc1234",
        local_build="", local_tag="main-20261109-0314")


def test_stable_still_uses_semver():
    info = {"tag": "v1.5.0", "app": {"version": "1.5.0", "build": ""}}
    assert online_update.has_app_update("stable", info, "1.4.2")
    assert not online_update.has_app_update("stable", info, "1.5.0")


def test_update_target_key_is_tag_on_rolling_version_on_stable():
    rolling = {"tag": "main-20261109-0314",
               "app": {"version": "0.0.0+main.abc"}}
    stable = {"tag": "v1.5.0", "app": {"version": "1.5.0"}}
    assert online_update.update_target_key("rolling", rolling) == \
        "main-20261109-0314"
    assert online_update.update_target_key("stable", stable) == "1.5.0"


def test_skipping_a_nightly_is_by_tag_only():
    """跳过 11-09 后，11-10 的 nightly 必须重新提示。"""
    assert online_update.should_skip_update(
        "rolling", "main-20261109-0314", "main-20261109-0314")
    assert not online_update.should_skip_update(
        "rolling", "main-20261110-0314", "main-20261109-0314")


def test_skipping_a_stable_version_keeps_the_semver_rule():
    assert online_update.should_skip_update("stable", "1.5.0", "1.5.0")
    assert not online_update.should_skip_update("stable", "1.6.0", "1.5.0")


def test_nightlies_are_not_mistaken_for_a_successful_restart():
    """0.0.0+main.aaa 与 0.0.0+main.bbb 的 semver 元组相同 —— 不能按元组比。"""
    assert not online_update.same_app_version("0.0.0+main.aaa1111",
                                              "0.0.0+main.bbb2222")
    assert online_update.same_app_version("0.0.0+main.aaa1111",
                                          "0.0.0+main.aaa1111")
    assert online_update.same_app_version("1.5.0", "1.5.0")


# --- 4. 接口层 ---------------------------------------------------------------


def test_check_rolling_end_to_end(client, monkeypatch, api_server):
    """真实入口：Flask 路由 → /releases → update.json → tag / has_update。"""
    monkeypatch.setattr(online_update, "GITHUB_API_LATEST", api_server["latest"])
    monkeypatch.setattr(online_update, "GITHUB_API_RELEASES", api_server["list"])
    monkeypatch.setattr(online_update, "read_app_version",
                        lambda: "0.0.0+main.old1111")
    monkeypatch.setattr(online_update, "read_app_build", lambda: "")
    host_app._config["update_channel"] = "rolling"

    body = client.get("/api/app/update/check").get_json()

    assert body["ok"] is True, body
    assert body["channel"] == "rolling"
    assert body["latest"] == "main-20261109-0314"
    assert body["tag"] == "main-20261109-0314"
    assert body["source"] == "manifest"
    assert body["asset"]["sha256"] == "a" * 64
    assert body["has_update"] is True


def test_check_stable_end_to_end_ignores_nightlies(client, monkeypatch, api_server):
    monkeypatch.setattr(online_update, "GITHUB_API_LATEST", api_server["latest"])
    monkeypatch.setattr(online_update, "GITHUB_API_RELEASES", api_server["list"])
    monkeypatch.setattr(online_update, "read_app_version", lambda: "1.4.2")
    host_app._config["update_channel"] = "stable"

    body = client.get("/api/app/update/check").get_json()

    assert body["ok"] is True, body
    assert body["channel"] == "stable"
    assert body["latest"] == "1.5.0"
    assert body["has_update"] is True
    assert _ReleaseHandler.list_hits == 0, \
        "stable 通道不得拉 /releases 列表"


def _install_rolling_info(monkeypatch, tag="main-20261109-0314", build="",
                          version="0.0.0+main.abc1234"):
    info = online_update.resolve_release_update(
        _rolling_release(tag, "2026-11-09T03:14:00Z"))
    info["app"]["version"] = version
    info["app"]["sha256"] = "a" * 64
    info["app"]["build"] = build
    info["channel"] = "rolling"
    monkeypatch.setattr(online_update, "resolve_latest_update",
                        lambda *a, **k: info)
    return info


def test_check_reports_the_rolling_tag_and_channel(client, monkeypatch):
    monkeypatch.setattr(online_update, "read_app_version",
                        lambda: "0.0.0+main.old1111")
    monkeypatch.setattr(online_update, "read_app_build", lambda: "")
    _install_rolling_info(monkeypatch)
    host_app._config["update_channel"] = "rolling"

    body = client.get("/api/app/update/check").get_json()

    assert body["ok"] is True
    assert body["channel"] == "rolling"
    assert body["tag"] == "main-20261109-0314"
    assert body["latest"] == "main-20261109-0314", \
        "滚动通道对用户显示 tag，而不是所有 nightly 共用的 0.0.0+main.<sha>"
    assert body["skip_key"] == "main-20261109-0314"
    assert body["has_update"] is True


def test_check_says_up_to_date_for_the_same_nightly(client, monkeypatch):
    monkeypatch.setattr(online_update, "read_app_version",
                        lambda: "0.0.0+main.abc1234")
    monkeypatch.setattr(online_update, "read_app_build", lambda: "")
    _install_rolling_info(monkeypatch)
    host_app._config["update_channel"] = "rolling"
    host_app._config["last_update"] = {
        "status": "done", "result": "ok", "tag": "main-20261109-0314",
    }

    body = client.get("/api/app/update/check").get_json()

    assert body["has_update"] is False


def test_check_propagates_e_no_rolling(client, monkeypatch):
    host_app._config["update_channel"] = "rolling"

    def _boom(*a, **k):
        raise online_update.UpdateError("没有滚动构建", "E_NO_ROLLING")

    monkeypatch.setattr(online_update, "resolve_latest_update", _boom)

    body = client.get("/api/app/update/check").get_json()

    assert body["ok"] is False
    assert body["error_code"] == "E_NO_ROLLING"
    assert body["channel"] == "rolling"


def test_check_applies_the_skip_key_for_rolling(client, monkeypatch):
    monkeypatch.setattr(online_update, "read_app_version",
                        lambda: "0.0.0+main.old1111")
    monkeypatch.setattr(online_update, "read_app_build", lambda: "")
    _install_rolling_info(monkeypatch)
    host_app._config["update_channel"] = "rolling"
    host_app._config["skipped_version"] = "main-20261109-0314"

    body = client.get("/api/app/update/check").get_json()

    assert body["skipped"] is True
    assert body["error_code"] == "E_SKIPPED"
    assert body["has_update"] is False


def test_apply_sends_a_new_nightly_through(client, monkeypatch, tmp_path):
    """新 nightly 能通过 apply 的全部前置检查并起下载任务。"""
    monkeypatch.setattr(online_update, "read_app_version",
                        lambda: "0.0.0+main.old1111")
    monkeypatch.setattr(online_update, "read_app_build", lambda: "")
    monkeypatch.setattr(host_app, "_update_target_dir", lambda: str(tmp_path))
    monkeypatch.setattr(host_app, "_app_update_worker",
                        lambda *a, **k: None)
    _install_rolling_info(monkeypatch)
    host_app._config["update_channel"] = "rolling"

    resp = client.post("/api/app/update/apply")
    body = resp.get_json()

    assert resp.status_code == 200, body
    assert body["ok"] is True
    host_app._app_update_tasks.clear()


def test_apply_refuses_the_same_nightly(client, monkeypatch, tmp_path):
    monkeypatch.setattr(online_update, "read_app_version",
                        lambda: "0.0.0+main.abc1234")
    monkeypatch.setattr(online_update, "read_app_build", lambda: "")
    monkeypatch.setattr(host_app, "_update_target_dir", lambda: str(tmp_path))
    _install_rolling_info(monkeypatch)
    host_app._config["update_channel"] = "rolling"
    host_app._config["last_update"] = {
        "status": "done", "result": "ok", "tag": "main-20261109-0314",
    }

    body = client.post("/api/app/update/apply").get_json()

    assert body["ok"] is False
    assert "滚动构建" in body["error"]


# --- 5. 通道持久化 -----------------------------------------------------------


def test_channel_switch_is_persisted_to_config_json(cfgfile):
    """不 stub save_config：真的写一遍 config.json。"""
    host_app.app.config["TESTING"] = True
    client = host_app.app.test_client()

    resp = client.post("/api/app/update/channel", json={"channel": "rolling"})

    assert resp.status_code == 200
    assert resp.get_json()["channel"] == "rolling"
    written = json.loads(cfgfile.read_text(encoding="utf-8"))
    assert written["update_channel"] == "rolling"


def test_channel_switch_survives_a_reload(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"update_channel": "rolling"}), encoding="utf-8")
    monkeypatch.setattr(host_app, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(host_app, "_config",
                        {"devices": {}, "settings": {}, "lang": "system"})

    host_app.load_config()

    assert host_app._config["update_channel"] == "rolling"


def test_unknown_channel_is_rejected(client):
    resp = client.post("/api/app/update/channel", json={"channel": "nightly"})

    assert resp.status_code == 400
    assert host_app._config["update_channel"] == "stable"


def test_old_config_without_the_key_defaults_to_stable(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"skipped_version": "1.5.0"}), encoding="utf-8")
    monkeypatch.setattr(host_app, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(host_app, "_config",
                        {"devices": {}, "settings": {}, "lang": "system"})

    host_app.load_config()

    assert host_app._config["update_channel"] == "stable"
