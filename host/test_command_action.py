"""WS-696: ``command`` actions start silently and report the real exit code.

These tests drive the production ``run_action`` / ``_record_action_result``
and assert on the real ``subprocess.Popen`` call and the real event log.  The
process is a fake object — no test spawns a shell — so they are safe on every
CI platform.

What would regress without the fix (each case fails on the old code):

* ``Popen`` was called without ``creationflags``, so the windowed Windows
  build flashed a ``cmd.exe`` console per press.
* The result claimed ``ok`` / "动作成功" before the command had run.
* Nothing waited for the process, so a missing command (exit code 9009) was
  never reported.

Run::

    pytest host/test_command_action.py -q
"""

import os
import re
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as host_app


CMD = "ping -n 3 127.0.0.1"


class _FakeProc:
    """Stand-in for ``subprocess.Popen``: only ``wait()`` is used."""

    def __init__(self, rc=0, error=None, gate=None):
        self.rc = rc
        self.error = error
        self.gate = gate
        self.wait_calls = 0

    def wait(self):
        self.wait_calls += 1
        if self.gate is not None:
            self.gate.wait(2.0)
        if self.error is not None:
            raise self.error
        return self.rc


def _rows(predicate=lambda e: True):
    return [e for e in list(host_app._events)
            if e["kind"] == "action" and predicate(e)]


def _wait_row(predicate, timeout=2.0):
    """Wait for the waiter thread to append its row (it is asynchronous)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _rows(predicate)
        if found:
            return found[0]
        time.sleep(0.01)
    return None


def _run_command(value=CMD, idx=2, proc=None, popen_error=None):
    """Run one command action with ``Popen`` faked out."""
    host_app._events.clear()
    action = {"type": "command", "value": value}
    proc = proc if proc is not None else _FakeProc()
    with mock.patch.object(host_app.subprocess, "Popen",
                           return_value=proc,
                           side_effect=popen_error) as popen:
        result = host_app.run_action(action, "dev-1", idx=idx)
    return action, result, proc, popen


# --- startup: no console window, still a shell ----------------------------

def test_windows_hides_the_console_window(monkeypatch):
    """Windows passes ``CREATE_NO_WINDOW``; ``shell=True`` stays."""
    monkeypatch.setattr(host_app.sys, "platform", "win32")
    monkeypatch.setattr(host_app.autostart, "CREATE_NO_WINDOW", 0x08000000)

    _, result, _, popen = _run_command()

    assert result["ok"] is True, result
    args, kwargs = popen.call_args
    assert args[0] == CMD, "the raw command line must reach the shell unchanged"
    assert kwargs["shell"] is True, "shell=True semantics must not change"
    assert kwargs["creationflags"] == 0x08000000, (
        "a windowed build needs CREATE_NO_WINDOW, otherwise cmd.exe flashes")


def test_other_platforms_keep_the_console_visible(monkeypatch):
    """macOS / Linux pass 0 so output stays in the launching terminal."""
    monkeypatch.setattr(host_app.sys, "platform", "linux")

    _, result, _, popen = _run_command()

    assert result["ok"] is True, result
    assert popen.call_args.kwargs["creationflags"] == 0


def test_result_reports_started_not_success():
    """The immediate result is "started"; the exit code is not known yet."""
    action, result, _, _ = _run_command()

    assert result["started"] is True, result
    assert result["ok"] is True
    assert result["error"] == ""
    assert result["target"] == action["value"]


def test_started_row_is_written_and_no_success_row():
    """Log says 「命令已启动」 (info) and never 「动作成功」."""
    action, result, _, _ = _run_command()
    host_app._record_action_result("dev-1", 2, action, result)

    rows = _rows()
    assert rows[0]["level"] == "info", rows
    assert rows[0]["text"] == f"命令已启动: 按键 3 · {CMD}", rows
    assert not any("动作成功" in r["text"] for r in rows), rows


# --- exit code echo -------------------------------------------------------

def test_exit_zero_is_logged_as_ok():
    """rc=0 produces one ``ok`` row carrying code 0 and the elapsed time."""
    _, _, proc, _ = _run_command(proc=_FakeProc(rc=0))

    row = _wait_row(lambda e: "命令结束" in e["text"])

    assert row is not None, [e["text"] for e in _rows()]
    assert row["level"] == "ok", row
    assert re.fullmatch(r"命令结束: 按键 3 · 退出码 0 · \d+\.\d s", row["text"]), row
    assert row["dev"] == "dev-1", "the row must use the triggering device"
    assert proc.wait_calls == 1


def test_nonzero_exit_is_logged_as_warn():
    """rc=1 (and Windows' 9009 for a missing command) is a warn row."""
    _run_command(proc=_FakeProc(rc=9009))

    row = _wait_row(lambda e: "命令结束" in e["text"])

    assert row is not None, [e["text"] for e in _rows()]
    assert row["level"] == "warn", row
    assert "退出码 9009" in row["text"], row


def test_started_row_comes_before_the_exit_row():
    """A fast command must not log its exit code ahead of 「已启动」."""
    action, result, _, _ = _run_command(proc=_FakeProc(rc=0))
    host_app._record_action_result("dev-1", 2, action, result)
    _wait_row(lambda e: "命令结束" in e["text"])

    texts = [e["text"] for e in _rows()]
    started_at = next(i for i, t in enumerate(texts) if "命令已启动" in t)
    ended_at = next(i for i, t in enumerate(texts) if "命令结束" in t)
    assert started_at < ended_at, texts


def test_long_command_does_not_block_the_caller():
    """A still-running command returns immediately; the row arrives later."""
    gate = threading.Event()
    try:
        _, result, _, _ = _run_command(proc=_FakeProc(rc=0, gate=gate))

        assert result["started"] is True, result
        assert _rows(lambda e: "命令结束" in e["text"]) == [], (
            "a command that has not exited must not have an exit row yet")

        gate.set()
        assert _wait_row(lambda e: "命令结束" in e["text"]) is not None
    finally:
        gate.set()


def test_wait_thread_is_a_daemon():
    """The waiter must not keep the host alive after quit."""
    created = []

    class _RecordingThread(threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    host_app._events.clear()
    with mock.patch.object(host_app.threading, "Thread", _RecordingThread):
        with mock.patch.object(host_app.subprocess, "Popen",
                               return_value=_FakeProc(rc=0)):
            result = host_app.run_action({"type": "command", "value": CMD},
                                         "dev-1", idx=2)

    assert result["started"] is True, result
    assert len(created) == 1, created
    assert created[0].daemon is True, "a non-daemon waiter would hang exit"
    created[0].join(2.0)
    assert _wait_row(lambda e: "命令结束" in e["text"]) is not None


def test_wait_failure_is_a_warn_row_not_an_exception():
    """``proc.wait()`` raising is reported as warn and does not escape."""
    _run_command(proc=_FakeProc(error=OSError("gone")))

    row = _wait_row(lambda e: "动作失败" in e["text"])

    assert row is not None, [e["text"] for e in _rows()]
    assert row["level"] == "warn", row
    assert "OSError: gone" in row["text"], row


# --- startup failure keeps the old error path -----------------------------

def test_popen_failure_stays_an_error_and_writes_no_started_row():
    """A command that cannot be spawned is an error, not a fake start."""
    action, result, _, _ = _run_command(
        value="C:\\not-exist\\x.exe", popen_error=FileNotFoundError("nope"))

    assert result["ok"] is False, result
    assert "started" not in result, result
    assert "FileNotFoundError" in result["error"], result

    host_app._record_action_result("dev-1", 2, action, result)
    rows = _rows()
    assert not any("命令已启动" in r["text"] for r in rows), rows
    assert rows[-1]["level"] == "error", rows
    assert rows[-1]["text"].startswith("动作失败: 按键 3 · command"), rows
