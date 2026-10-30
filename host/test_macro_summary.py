"""Unit tests for the macro log summary (WS-484).

A macro body may hold a password (``paste:`` / ``text:`` lines).  The event
log is copyable row by row and is served verbatim by ``GET /api/log``, so the
log must carry a *summary* of the macro steps — never the body.

These tests drive the production functions (``app.macro_summary`` /
``app.run_action`` / ``app._record_action_result``) instead of re-implementing
them, so a change to ``host/app.py`` that puts the macro body back into the
log fails this file.

The tests must stay platform-independent and side-effect free: CI runs them
on Windows, so no test may pop a browser, send a media key, spawn a shell
command or touch the clipboard.  The keyboard / clipboard are stubbed through
``app._load_keyboard`` / ``app._paste_text``; everything above that (the
summary, the macro loop, the result row) is the production code.

Run::

    pytest host/test_macro_summary.py -v
"""

import os
import sys
from unittest import mock

# host/ is not a package on its own; import the module by path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as host_app


# --- helpers ---------------------------------------------------------------

def _macro(steps, value="raw macro body"):
    """A macro action dict as the UI stores it."""
    return {"type": "macro", "value": value, "macro": steps}


def _events(kind="action"):
    return [e for e in host_app._events if e["kind"] == kind]


def _texts(kind="action"):
    return [e["text"] for e in host_app._events if e["kind"] == kind]


class _StubKeyboard:
    """A ``keyboard`` stand-in that records nothing and sends nothing."""

    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.sent = []

    def send(self, keys, *args, **kwargs):
        if self.fail_on is not None and self.fail_on in str(keys):
            raise ValueError(f"unknown key: {keys}")
        self.sent.append(keys)

    def write(self, text, *args, **kwargs):
        pass


def _run_action(action, keyboard=None):
    """Run one action through the production ``run_action``.

    Everything that would touch the machine is stubbed: the keyboard, the
    clipboard, the browser opener and the shell.  The macro loop, the step
    bookkeeping and the result object are the production code.
    """
    kb = keyboard if keyboard is not None else _StubKeyboard()
    with mock.patch.object(host_app, "_load_keyboard", return_value=kb):
        with mock.patch.object(host_app, "_paste_text", return_value=True):
            with mock.patch.object(host_app, "_open_target") as open_mock:
                with mock.patch.object(host_app.subprocess, "Popen"):
                    return host_app.run_action(action, dev_id="dev-1")


# --- macro_summary: the step summary ---------------------------------------

def test_summary_paste_and_key():
    """``paste:`` shows only its length, ``key:`` keeps its value."""
    summary = host_app.macro_summary(_macro([
        {"type": "paste", "value": "my-Secret-123"},
        {"type": "key", "value": "enter"},
    ]))

    assert summary == "2 步：paste(13 字符) → key:enter", summary
    assert "my-Secret-123" not in summary, (
        "the macro body must never reach the log — it may be a password")


def test_summary_text_and_wait():
    """``text:`` shows only its length, ``wait:`` keeps its value."""
    summary = host_app.macro_summary(_macro([
        {"type": "text", "value": "alice@example.com"},
        {"type": "delay", "value": 500},
    ]))

    assert summary == "2 步：text(17 字符) → wait:500", summary
    assert "alice@example.com" not in summary, (
        "the macro body must never reach the log")


def test_summary_text_with_explicit_delay():
    """``text!0:`` / ``text!80:`` keep their per-character delay."""
    assert "text(0ms, 3 字符)" in host_app.macro_summary(_macro([
        {"type": "text", "value": "abc", "delay": 0}]))
    assert "text(80ms, 9 字符)" in host_app.macro_summary(_macro([
        {"type": "text", "value": "123456789", "delay": 80}]))


def test_summary_without_delay_has_no_ms():
    """A plain ``text:`` step carries no explicit delay."""
    assert "text(3 字符)" in host_app.macro_summary(_macro([
        {"type": "text", "value": "abc"}]))
    assert "ms" not in host_app.macro_summary(_macro([
        {"type": "text", "value": "abc"}]))


def test_summary_empty_macro():
    """An empty macro has no steps — and never echoes ``action["value"]``."""
    action = _macro([], value="paste:my-Secret-123")
    summary = host_app.macro_summary(action)

    assert summary == "0 步", summary
    assert "my-Secret-123" not in summary


def test_summary_legacy_config_without_macro_array():
    """An old config.json keeps only ``value`` — the summary must not leak it."""
    action = {"type": "macro", "value": "paste:my-Secret-123"}
    summary = host_app.macro_summary(action)

    assert summary == "0 步", summary
    for fragment in ("my-Secret", "Secret", "123", "paste"):
        assert fragment not in summary, (
            f"the legacy macro body must not be echoed ({fragment})")


def test_summary_macro_field_not_a_list():
    """A corrupt ``macro`` field is treated like an empty macro."""
    action = {"type": "macro", "value": "paste:my-Secret-123",
              "macro": "not-a-list"}
    summary = host_app.macro_summary(action)

    assert summary == "0 步", summary
    assert "my-Secret-123" not in summary


def test_summary_long_macro_is_truncated():
    """A 30-step macro is cut short but keeps the real step count."""
    summary = host_app.macro_summary(_macro([
        {"type": "key", "value": f"k{i}"} for i in range(30)]))

    assert len(summary) <= 140, summary
    assert summary.endswith("…（共 30 步）"), summary
    assert "\n" not in summary, summary


def test_summary_is_a_single_line():
    """A step value with a newline must not break the log row."""
    summary = host_app.macro_summary(_macro([
        {"type": "text", "value": "a\nb"},
        {"type": "key", "value": "enter"},
    ]))

    assert "\n" not in summary and "\r" not in summary, summary


def test_summary_counts_unicode_code_points():
    """The character count counts code points, not bytes (emoji count 1)."""
    summary = host_app.macro_summary(_macro([
        {"type": "text", "value": "🙂"},
    ]))

    assert "text(1 字符)" in summary, summary


# --- the log row: the macro body must not survive --------------------------

def test_log_row_has_summary_only():
    """The production log row shows the summary, never the macro body."""
    host_app._events.clear()
    action = _macro([
        {"type": "paste", "value": "my-Secret-123"},
        {"type": "key", "value": "enter"},
    ])

    result = _run_action(action)
    assert result["ok"] is True, result
    assert result["target"] == "2 步：paste(13 字符) → key:enter", result

    host_app._record_action_result("dev-1", 2, action, result)
    rows = _texts()
    assert rows, "the action must be logged"
    for row in rows:
        assert "my-Secret-123" not in row, row
    assert "2 步：paste(13 字符) → key:enter" in rows[-1], rows[-1]


def test_api_log_never_serves_the_macro_body():
    """``GET /api/log`` is copyable — it must not carry the macro body."""
    host_app._events.clear()
    action = _macro([
        {"type": "paste", "value": "my-Secret-123"},
        {"type": "key", "value": "enter"},
    ])

    result = _run_action(action)
    host_app._record_action_result("dev-1", 2, action, result)

    client = host_app.app.test_client()
    rows = client.get("/api/log").get_json()["events"]
    body = "\n".join(e["text"] for e in rows)

    assert "my-Secret-123" not in body, (
        "GET /api/log hands the log out verbatim — the macro body must "
        "not be in it")
    assert "2 步：paste(13 字符) → key:enter" in body, body


def test_failure_row_points_at_the_failing_step():
    """A failing macro names the step — and never the macro body."""
    host_app._events.clear()
    action = _macro([
        {"type": "paste", "value": "my-Secret-123"},
        {"type": "paste", "value": "second-Secret-456"},
        {"type": "key", "value": "bogus"},
        {"type": "key", "value": "enter"},
        {"type": "delay", "value": 10},
        {"type": "key", "value": "tab"},
    ])

    result = _run_action(action, keyboard=_StubKeyboard(fail_on="bogus"))
    assert result["ok"] is False, result
    assert result["step"] == 3, result
    assert result["step_type"] == "key", result

    host_app._record_action_result("dev-1", 2, action, result)
    row = _texts()[-1]
    assert "宏第 3/6 步 key 失败" in row, row
    assert "my-Secret-123" not in row, row
    assert "second-Secret-456" not in row, row


def test_failure_row_names_a_wait_step_as_written():
    """A failed ``wait:`` step is named ``wait`` — the way the user wrote it."""
    host_app._events.clear()
    action = _macro([{"type": "delay", "value": 500}])

    result = {"ok": False, "error": "boom", "action_type": "macro",
              "target": "1 步：wait:500", "step": 1, "step_type": "delay"}
    host_app._record_action_result("dev-1", 2, action, result)
    row = _texts()[-1]

    assert "宏第 1/1 步 wait 失败" in row, row


def test_failure_row_falls_back_without_step_info():
    """Without step information the old row format is used."""
    host_app._events.clear()
    action = {"type": "command", "value": "notepad.exe"}
    result = {"ok": False, "error": "boom", "action_type": "command",
              "target": "notepad.exe"}

    host_app._record_action_result("dev-1", 2, action, result)
    row = _texts()[-1]

    assert row == "动作失败: 按键 3 · command · notepad.exe · boom", row


def test_non_macro_actions_keep_their_original_target():
    """A command / launch action still logs its original text."""
    result = _run_action({"type": "command", "value": "notepad.exe"})

    assert result["ok"] is True, result
    assert result["target"] == "notepad.exe", result


# --- URL userinfo redaction ------------------------------------------------

def test_redact_url_userinfo_replaces_credentials():
    """The userinfo segment becomes ``***``."""
    assert host_app._redact_url_userinfo(
        "https://alice:pw@example.com/x") == "https://***@example.com/x"


def test_redact_url_userinfo_leaves_query_alone():
    """An ``@`` in the query string is not userinfo — it stays untouched."""
    assert host_app._redact_url_userinfo(
        "https://example.com/a?u=b@c") == "https://example.com/a?u=b@c"


def test_redact_url_userinfo_without_userinfo():
    """A URL without credentials is returned unchanged."""
    assert host_app._redact_url_userinfo(
        "https://example.com/x") == "https://example.com/x"


def test_redact_url_userinfo_not_a_url():
    """A non-URL string is returned unchanged."""
    assert host_app._redact_url_userinfo("C:\\Users\\me\\a.txt") == \
        "C:\\Users\\me\\a.txt"
    assert host_app._redact_url_userinfo("") == ""


def test_url_action_target_is_redacted():
    """A URL action with credentials logs the masked URL."""
    result = _run_action({"type": "url",
                          "value": "https://alice:pw@example.com/x"})

    assert result["target"] == "https://***@example.com/x", result


def test_macro_url_step_is_redacted():
    """The ``url:`` step of a macro shows the masked URL."""
    summary = host_app.macro_summary(_macro([
        {"type": "url", "value": "https://alice:pw@example.com/x"},
    ]))

    assert "https://***@example.com/x" in summary, summary
    assert "alice:pw" not in summary, summary
