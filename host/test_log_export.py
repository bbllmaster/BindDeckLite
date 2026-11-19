"""WS-646: 日志页「导出…」后端回归用例。

断言全部驱动 **生产实现**（host/app.py 的 api_log_export /
sanitize_log_export_name / atomic_write_text），不在这里复制一份写盘或
净化逻辑 —— 改坏生产代码就会真的变红。

覆盖：写盘成功 + UTF-8 BOM、取消零副作用、写盘失败无 ``.tmp`` 残留、
0 行 / 空文本被拒、建议文件名净化、导出只读（config.json 不变）。

运行::

    cd host && python -m pytest test_log_export.py -q
"""

import json
import os
import sys

import pytest

# host/ 不是包，按路径 import 生产模块。
sys.path.insert(0, os.path.dirname(__file__))
import app as host_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Flask 测试客户端 + 临时 config.json，重置进程级 _config。"""
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


SAMPLE_TEXT = (
    "BindDeckLite 日志导出\n"
    "导出时间: 2026-11-20 00:12:33\n"
    "上位机: v1.4.3（桌面版）\n"
    "筛选: 级别=错误 · 类型=全部 · 设备=全部 · 搜索=(无)\n"
    "命中: 1 行（日志缓冲上限 400 行，屏幕最多保留 300 行，更早的行已不在列表里）\n"
    "\n"
    "00:00:01 [key] [BindDeckLite-A1B2] 按键 1\n"
)


def _post(c, payload=None):
    body = {"text": SAMPLE_TEXT, "count": 1,
            "suggested_name": "BindDeckLite-log-all-20261120-0012.txt"}
    body.update(payload or {})
    return c.post("/api/log/export", json=body)


# ---------------------------------------------------------------------------
# 写盘成功：UTF-8 with BOM + 内容逐字一致 + 记一行 desktop/ok
# ---------------------------------------------------------------------------

def test_export_writes_utf8_bom_file(client, monkeypatch, tmp_path):
    c, _ = client
    out = tmp_path / "log.txt"
    seen = {}

    def _fake_browse(kind, default_name=""):
        seen["kind"] = kind
        seen["default_name"] = default_name
        return {"ok": True, "path": str(out)}

    monkeypatch.setattr(host_app.desktop, "browse", _fake_browse)
    before = len(host_app._events)
    resp = _post(c)

    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["cancelled"] is False
    assert body["path"] == str(out)
    assert body["count"] == 1

    # 对话框以 save 模式打开，并带上净化后的建议名。
    assert seen["kind"] == "save"
    assert seen["default_name"] == "BindDeckLite-log-all-20261120-0012.txt"

    raw = out.read_bytes()
    # BOM 生效：前 3 字节 EF BB BF（记事本 / 聊天窗口靠它认 UTF-8）。
    assert raw[:3] == b"\xef\xbb\xbf"
    assert raw[3:].decode("utf-8") == SAMPLE_TEXT
    # 没有留下 .tmp 半份。
    assert not (tmp_path / "log.txt.tmp").exists()

    added = list(host_app._events)[before:]
    assert len(added) == 1
    assert added[0]["kind"] == "desktop"
    assert added[0]["level"] == "ok"
    assert added[0]["text"] == "已导出 1 行日志 → " + str(out)


# ---------------------------------------------------------------------------
# 取消：不写文件、不记日志、返回 cancelled
# ---------------------------------------------------------------------------

def test_export_cancel_has_no_side_effects(client, monkeypatch, tmp_path):
    c, _ = client
    out = tmp_path / "log.txt"
    monkeypatch.setattr(host_app.desktop, "browse",
                        lambda kind, default_name="": {"ok": True, "path": ""})
    before = len(host_app._events)
    resp = _post(c)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is False
    assert body["cancelled"] is True
    assert not out.exists()
    assert not any(tmp_path.glob("*.tmp"))
    assert len(host_app._events) == before


# ---------------------------------------------------------------------------
# 写盘失败：500 + 原因，临时文件清理干净，旧文件不被破坏
# ---------------------------------------------------------------------------

def test_export_write_failure_leaves_no_tmp(client, monkeypatch, tmp_path):
    c, _ = client
    # 目标是已存在的目录：写 tmp 能成功，os.replace(tmp, dir) 必然失败，
    # 这样才能验证「失败后把 tmp 删掉」而不是本来就写不进去。
    target = tmp_path / "occupied"
    target.mkdir()
    monkeypatch.setattr(host_app.desktop, "browse",
                        lambda kind, default_name="": {"ok": True,
                                                       "path": str(target)})
    before = len(host_app._events)
    resp = _post(c)

    assert resp.status_code == 500
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"]
    assert not body["error"].startswith("error.")
    assert not (tmp_path / "occupied.tmp").exists()
    assert target.is_dir()                    # 原来的目录没被替换掉
    assert len(host_app._events) == before    # 失败不记「已导出」


# ---------------------------------------------------------------------------
# 编码失败（孤立代理项）：回 JSON 原因，且不留 0 字节 .tmp（WS-649）
# ---------------------------------------------------------------------------

def test_export_unencodable_text_leaves_no_tmp(client, monkeypatch, tmp_path):
    """``UnicodeEncodeError`` 不是 ``OSError``，只捕后者会留下半个文件。

    文本里出现孤立代理项（``\\ud800``）时 UTF-8 编码会失败：要么 500 里
    带原因、目录干净，要么就是用户拿到一个 0 字节的「导出成功」。
    """
    c, _ = client
    out = tmp_path / "log.txt"
    monkeypatch.setattr(host_app.desktop, "browse",
                        lambda kind, default_name="": {"ok": True,
                                                       "path": str(out)})
    before = len(host_app._events)
    # 请求体里直接写 JSON 转义 ``\\ud800``：解析后是一个孤立代理项，正是
    # UTF-8 编码不了的输入（前端照抄事件正文时也可能带上类似的坏字符）。
    resp = c.post("/api/log/export",
                  data='{"text": "bad \\ud800 char", "count": 1}',
                  content_type="application/json")

    assert resp.status_code == 500
    body = resp.get_json()
    assert body["ok"] is False and body["cancelled"] is False
    assert body["error"], "编码失败也要给出原因，不能是 HTML 500"
    assert not out.exists()
    assert not (tmp_path / "log.txt.tmp").exists()
    assert len(host_app._events) == before


# ---------------------------------------------------------------------------
# 0 行 / 空文本：不弹对话框、不写文件、不记日志
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"text": "", "count": 1},
    {"text": "   \n", "count": 1},
    {"count": 0},
    {"text": SAMPLE_TEXT, "count": 0},
], ids=["empty", "blank", "no-count", "zero-count"])
def test_export_rejects_empty(client, monkeypatch, payload):
    c, _ = client
    called = []
    monkeypatch.setattr(host_app.desktop, "browse",
                        lambda kind, default_name="": called.append(default_name) or
                        {"ok": True, "path": ""})
    before = len(host_app._events)
    resp = _post(c, payload)
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert called == []                       # 连对话框都不弹
    assert len(host_app._events) == before


# ---------------------------------------------------------------------------
# 没原生对话框（浏览器模式 / 无 tkinter）：交给前端写剪贴板
# ---------------------------------------------------------------------------

def test_export_without_dialog_falls_back_to_clipboard(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(host_app.desktop, "browse",
                        lambda kind, default_name="": {"ok": False,
                                                       "path": "",
                                                       "error": "no tk"})
    before = len(host_app._events)
    resp = _post(c)
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "fallback": "clipboard"}
    assert len(host_app._events) == before


# ---------------------------------------------------------------------------
# 只读：导出不改 config.json
# ---------------------------------------------------------------------------

def test_export_does_not_touch_config(client, monkeypatch, tmp_path):
    c, path = client
    before = path.read_bytes()
    out = tmp_path / "log.txt"
    monkeypatch.setattr(host_app.desktop, "browse",
                        lambda kind, default_name="": {"ok": True, "path": str(out)})
    assert _post(c).status_code == 200
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# 文件名净化：只留 [A-Za-z0-9._-]，空名有回退，必带 .txt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("BindDeckLite-log-all-20261120-0012.txt",
     "BindDeckLite-log-all-20261120-0012.txt"),
    ("设备 A/1.txt", "A_1.txt"),
    ("../../etc/passwd", "etc_passwd.txt"),
    ("", "BindDeckLite-log.txt"),
    ("!!!", "BindDeckLite-log.txt"),
])
def test_sanitize_name(raw, expected):
    assert host_app.sanitize_log_export_name(raw) == expected


def test_export_passes_sanitized_name_to_dialog(client, monkeypatch, tmp_path):
    c, _ = client
    out = tmp_path / "log.txt"
    seen = {}

    def _fake_browse(kind, default_name=""):
        seen["default_name"] = default_name
        return {"ok": True, "path": str(out)}

    monkeypatch.setattr(host_app.desktop, "browse", _fake_browse)
    resp = _post(c, {"suggested_name": "设备 A/../x.txt"})
    assert resp.status_code == 200
    # 前端给的名字再净化一次：不允许路径分隔符混进来。
    assert seen["default_name"] == "A_.._x.txt"[:host_app.LOG_EXPORT_NAME_MAX]
    assert "/" not in seen["default_name"] and "\\" not in seen["default_name"]
    assert seen["default_name"].endswith(".txt")
