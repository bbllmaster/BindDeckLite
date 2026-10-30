"""WS-484: 宏正文不得进入日志 / 复制内容 / 失败 toast。

这些用例直接 import 生产函数（``macro_summary`` / ``redact_url_userinfo`` /
``_record_action_result`` / ``run_action``），不复制任何摘要逻辑：生产实现回退
到原文时，用例必须失败。

运行：
    cd host && python -m pytest test_macro_log_redaction.py -q
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import app as host_app


SECRET = "my-Secret-123"


class _FakeKeyboard:
    """最小 keyboard 替身：记录调用，可按需让某个键抛错。"""

    def __init__(self, failing_key=""):
        self.sent = []
        self.written = []
        self.failing_key = failing_key

    def send(self, key):
        if self.failing_key and key == self.failing_key:
            raise ValueError(f"Unknown key: {key}")
        self.sent.append(key)

    def write(self, text, delay=0.02):
        self.written.append((text, delay))


@pytest.fixture
def host(monkeypatch):
    """隔离事件日志与 keyboard / 剪贴板依赖。"""
    monkeypatch.setattr(host_app, "_events", [], raising=False)
    monkeypatch.setattr(host_app, "_event_seq", 0, raising=False)
    monkeypatch.setattr(host_app, "_keyboard", _FakeKeyboard(), raising=False)
    # paste 走真实剪贴板，在 CI / headless 上会插入失败日志；这里按成功返回。
    monkeypatch.setattr(host_app, "_paste_text",
                        lambda kb, text, dev_id="": True)
    return host_app


def _last_log(host_app):
    assert host_app._events, "没有写入任何日志"
    return host_app._events[-1]["text"]


def _macro(value, steps):
    return {"type": "macro", "value": value, "macro": steps}


# ---------------------------------------------------------------------------
# macro_summary：只出类型与字符数
# ---------------------------------------------------------------------------

def test_paste_body_becomes_char_count(host):
    action = _macro(f"paste:{SECRET}\nkey:enter",
                    [{"type": "paste", "value": SECRET},
                     {"type": "key", "value": "enter"}])
    summary = host.macro_summary(action)
    assert summary == "2 步：paste(13 字符) → key:enter"
    assert SECRET not in summary


def test_text_body_becomes_char_count(host):
    action = _macro("text:alice@example.com\nwait:500",
                    [{"type": "text", "value": "alice@example.com"},
                     {"type": "delay", "value": 500}])
    summary = host.macro_summary(action)
    assert summary == "2 步：text(17 字符) → wait:500"
    assert "alice@example.com" not in summary


def test_text_custom_delay_is_shown(host):
    action = _macro("text!80:密码", [{"type": "text", "value": "密码", "delay": 80}])
    assert host.macro_summary(action) == "1 步：text(80ms, 2 字符)"


def test_text_zero_delay_is_shown_not_treated_as_unset(host):
    action = _macro("text!0:abc", [{"type": "text", "value": "abc", "delay": 0}])
    assert host.macro_summary(action) == "1 步：text(0ms, 3 字符)"


def test_char_count_uses_unicode_code_points(host):
    # 一个 emoji 是 1 个码点，不是 UTF-8 的 4 字节。
    action = _macro("paste:🔑🔑", [{"type": "paste", "value": "🔑🔑"}])
    assert host.macro_summary(action) == "1 步：paste(2 字符)"


def test_key_app_url_browser_wait_keep_their_values(host):
    steps = [
        {"type": "app", "value": "/usr/bin/notepad --new"},
        {"type": "browser", "value": "chrome"},
        {"type": "url", "value": "https://example.com/login"},
        {"type": "delay", "value": 1500},
        {"type": "key", "value": "ctrl+alt+t"},
    ]
    summary = host.macro_summary(_macro("", steps))
    assert summary.startswith("5 步：")
    assert "app:/usr/bin/notepad --new" in summary
    assert "browser:chrome" in summary
    assert "url:https://example.com/login" in summary
    assert "wait:1500" in summary
    assert "key:ctrl+alt+t" in summary


def test_long_summary_is_truncated_with_total_step_count(host):
    steps = [{"type": "paste", "value": f"secret{i:02d}"} for i in range(30)]
    summary = host.macro_summary(_macro("ignored", steps))
    assert len(summary) <= host.MACRO_SUMMARY_MAX
    assert summary.startswith("30 步：")
    assert summary.endswith("…（共 30 步）")
    assert "\n" not in summary
    assert "secret" not in summary


def test_action_without_macro_array_shows_zero_steps(host):
    # 旧 config.json：只有 value，没有 macro 数组 —— 不得回显 value。
    action = {"type": "macro", "value": f"paste:{SECRET}\nkey:enter"}
    summary = host.macro_summary(action)
    assert summary == "0 步"
    assert SECRET not in summary


# ---------------------------------------------------------------------------
# URL userinfo 脱敏
# ---------------------------------------------------------------------------

def test_redact_url_userinfo_masks_only_userinfo(host):
    assert (host.redact_url_userinfo("https://alice:pw@example.com/x")
            == "https://***@example.com/x")
    assert (host.redact_url_userinfo("http://alice@example.com:8080/a?b=c#d")
            == "http://***@example.com:8080/a?b=c#d")
    # 没有 userinfo 的 URL 原样保留（查询串不猜测脱敏）。
    assert (host.redact_url_userinfo("https://example.com/a?token=1")
            == "https://example.com/a?token=1")
    assert host.redact_url_userinfo("") == ""


def test_macro_url_step_is_redacted(host):
    action = _macro("url:https://alice:pw@example.com/x",
                    [{"type": "url", "value": "https://alice:pw@example.com/x"}])
    summary = host.macro_summary(action)
    assert summary == "1 步：url:https://***@example.com/x"


def test_url_action_target_is_redacted(host, monkeypatch):
    # 不真的拉起浏览器：只验证 display target。
    monkeypatch.setattr(host, "_open_target", lambda value: None)
    result = host.run_action({"type": "url", "value": "https://alice:pw@example.com/x"})
    assert result["target"] == "https://***@example.com/x"


# ---------------------------------------------------------------------------
# run_action + _record_action_result：日志 / toast 文本同源
# ---------------------------------------------------------------------------

def test_success_log_has_no_body(host):
    action = _macro(f"paste:{SECRET}\nkey:enter",
                    [{"type": "paste", "value": SECRET},
                     {"type": "key", "value": "enter"}])
    result = host.run_action(action, "dev")
    assert result["ok"] is True
    host._record_action_result("dev", 2, action, result)
    text = _last_log(host)
    assert text == "动作成功: 按键 3 · macro · 2 步：paste(13 字符) → key:enter"
    assert SECRET not in text
    assert "\n" not in text


def test_failure_log_points_at_step_and_hides_body(host, monkeypatch):
    monkeypatch.setattr(host, "_keyboard", _FakeKeyboard("bogus"))
    steps = [
        {"type": "paste", "value": SECRET},
        {"type": "text", "value": "alice@example.com"},
        {"type": "key", "value": "bogus"},
        {"type": "key", "value": "enter"},
        {"type": "delay", "value": 100},
        {"type": "key", "value": "tab"},
    ]
    action = _macro("ignored", steps)
    result = host.run_action(action, "dev")
    assert result["ok"] is False
    assert result["step"] == 3
    assert result["step_type"] == "key"
    host._record_action_result("dev", 2, action, result)
    text = _last_log(host)
    assert text == ("动作失败: 按键 3 · 宏第 3/6 步 key 失败："
                    "ValueError: Unknown key: bogus")
    assert SECRET not in text
    assert "alice@example.com" not in text


def test_encoder_label_failure_uses_same_summary(host, monkeypatch):
    monkeypatch.setattr(host, "_keyboard", _FakeKeyboard("bogus"))
    action = _macro("paste:pw", [{"type": "paste", "value": "pw"},
                                 {"type": "key", "value": "bogus"}])
    result = host.run_action(action, "dev")
    host._record_action_result("dev", -1, action, result, label="编码器顺时针")
    text = _last_log(host)
    assert text.startswith("动作失败: 编码器顺时针 · 宏第 2/2 步 key 失败：")
    assert "pw" not in text


def test_non_macro_action_log_is_unchanged(host, monkeypatch):
    # 非宏动作的日志口径必须保持原样（路径 / 命令 / URL 原文照常显示）。
    monkeypatch.setattr(host, "_open_target", lambda value: None)
    target_path = os.path.join(os.path.dirname(__file__), "app.py")
    action = {"type": "launch", "value": target_path}
    result = host.run_action(action, "dev")
    host._record_action_result("dev", 0, action, result)
    # 超过 80 字符仍按 _truncate 截断，与改动前完全一致。
    assert _last_log(host) == (f"动作成功: 按键 1 · launch · "
                               f"{host._truncate(target_path)}")
