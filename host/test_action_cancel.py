"""Unit tests for "stop a running macro" (WS-495 / WS-494).

A macro can type a password into whatever window has focus when the user did
not expect it.  Before this feature the only way out was to wait for the macro
to finish; now the card's button turns into "停止" and the server stops at the
next step boundary.

These tests drive the **production** functions — ``app._run_action_for`` (the
physical-key path), ``app.run_action``, ``app._cancel_action`` and the Flask
test client hitting ``/api/action/test`` + ``/api/action/cancel`` — instead of
re-implementing the cancel logic, so a change to ``host/app.py`` that reopens
the hole fails this file.

They must stay platform-independent and side-effect free: CI runs them on
Windows, so no test may pop a browser, send a media key, spawn a shell command
or touch the clipboard.  The keyboard / clipboard are stubbed; everything
above them (the claim table, the cancel flag, the macro loop, the event log)
is production code.

Run::

    cd host && python -m pytest test_action_cancel.py -q
"""

import os
import sys
import threading
import time
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as host_app


# --- helpers ---------------------------------------------------------------

class _StubKeyboard:
    """Records what a macro would send; never touches the real machine."""

    def __init__(self, write_delay=0.0):
        self.sent = []
        self.written = []
        self.write_delay = write_delay

    def send(self, keys, *args, **kwargs):
        self.sent.append(str(keys))

    def write(self, text, *args, **kwargs):
        self.written.append(str(text))
        if self.write_delay:
            time.sleep(self.write_delay)


@contextmanager
def _platform(kb):
    """Stub everything that would reach the machine; keep the macro loop."""
    with mock.patch.object(host_app, "_load_keyboard", return_value=kb):
        with mock.patch.object(host_app, "_paste_text", return_value=True):
            with mock.patch.object(host_app, "_open_target"):
                with mock.patch.object(host_app.subprocess, "Popen"):
                    yield


def _setup(devices):
    """Register one macro per device and reset the global tables.

    ``devices`` maps ``dev_id`` -> ``(idx, steps)``.  The config is written
    straight into ``_config`` so ``device_cfg`` never touches config.json.
    """
    with host_app._lock:
        host_app._action_running.clear()
    host_app._events.clear()
    host_app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    for dev_id, (idx, steps) in devices.items():
        cfg = host_app.new_device_cfg("ESP32", dev_id)
        cfg["keys"][idx]["action"] = {"type": "macro", "value": "登录宏",
                                      "macro": steps}
        host_app._config["devices"][dev_id] = cfg


def _wait_for(predicate, timeout=5.0):
    """Poll ``predicate`` until it is truthy (actions run in threads)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _stopped_rows():
    return [e for e in host_app._events if "动作已停止" in e["text"]]


def _claim_exists(dev_id, idx):
    return (dev_id, idx) in host_app._action_running


# --- the core promise: a stop really stops --------------------------------

def test_cancel_during_wait_never_sends_the_following_key():
    """wait:2000 -> key:enter, stopped while waiting: enter must not go out."""
    _setup({"t1": (3, [{"type": "delay", "value": 2000},
                       {"type": "key", "value": "enter"}])})
    kb = _StubKeyboard()
    client = host_app.app.test_client()

    with _platform(kb):
        host_app._run_action_for("t1", 3)
        assert _wait_for(lambda: _claim_exists("t1", 3)), "claim never appeared"

        res = client.post("/api/action/cancel/t1/3")
        assert res.status_code == 200
        assert res.get_json() == {"ok": True, "cancelling": True}

        assert _wait_for(lambda: bool(_stopped_rows())), "no 动作已停止 row"

    # The whole point: the step after the wait never ran.
    assert kb.sent == []

    row = _stopped_rows()[0]
    assert row["level"] == "warn"
    assert "按键 4" in row["text"]
    assert "第 1/2 步（wait）" in row["text"]

    # The claim is released, so the input can be used again right away.
    assert _wait_for(lambda: not _claim_exists("t1", 3))


def test_cancel_during_text_keeps_the_body_out_of_the_log():
    """A stop mid-``text:`` finishes that step and logs only the step number."""
    _setup({"t1": (3, [{"type": "text", "value": "hunter2"},
                       {"type": "key", "value": "enter"}])})
    kb = _StubKeyboard(write_delay=0.4)
    client = host_app.app.test_client()

    with _platform(kb):
        host_app._run_action_for("t1", 3)
        assert _wait_for(lambda: bool(kb.written)), "text step never started"

        assert client.post("/api/action/cancel/t1/3").status_code == 200
        assert _wait_for(lambda: bool(_stopped_rows())), "no 动作已停止 row"

    # The text step in flight is not retracted, the next one never runs.
    assert kb.written == ["hunter2"]
    assert kb.sent == []

    text = " ".join(e["text"] for e in host_app._events)
    assert "hunter2" not in text, "macro body leaked into the log"
    assert "第 2/2 步（key）" in _stopped_rows()[0]["text"]


def test_cancel_then_rerun_starts_a_fresh_action():
    """After a stop the claim is free: the same input can run again."""
    _setup({"t1": (3, [{"type": "delay", "value": 1500}])})
    kb = _StubKeyboard()
    client = host_app.app.test_client()

    with _platform(kb):
        host_app._run_action_for("t1", 3)
        assert _wait_for(lambda: _claim_exists("t1", 3))
        client.post("/api/action/cancel/t1/3")
        assert _wait_for(lambda: not _claim_exists("t1", 3)), "claim not released"

        # Second, uninterrupted run: it must be claimed and recorded normally.
        host_app._run_action_for("t1", 3)
        assert _wait_for(lambda: _claim_exists("t1", 3)), "re-run not claimed"
        assert _wait_for(lambda: not _claim_exists("t1", 3)), "re-run never ended"

    ok_rows = [e for e in host_app._events if "动作成功" in e["text"]]
    assert len(ok_rows) == 1
    assert len(_stopped_rows()) == 1


# --- idempotency -----------------------------------------------------------

def test_repeat_cancel_logs_exactly_one_row():
    """Clicking stop twice must not write two "已停止" rows."""
    _setup({"t1": (3, [{"type": "delay", "value": 1500}])})
    kb = _StubKeyboard()
    client = host_app.app.test_client()

    with _platform(kb):
        host_app._run_action_for("t1", 3)
        assert _wait_for(lambda: _claim_exists("t1", 3))

        first = client.post("/api/action/cancel/t1/3").get_json()
        second = client.post("/api/action/cancel/t1/3").get_json()
        third = client.post("/api/action/cancel/t1/3").get_json()

        assert first == {"ok": True, "cancelling": True}
        assert second == {"ok": True, "already_done": True}
        assert third == {"ok": True, "already_done": True}

        assert _wait_for(lambda: bool(_stopped_rows()))
        assert _wait_for(lambda: not _claim_exists("t1", 3))

    assert len(_stopped_rows()) == 1


def test_cancel_after_the_macro_finished_is_already_done():
    """A late stop answers 200 already_done and never logs a failure."""
    _setup({"t1": (3, [{"type": "key", "value": "enter"}])})
    kb = _StubKeyboard()
    client = host_app.app.test_client()

    with _platform(kb):
        host_app._run_action_for("t1", 3)
        assert _wait_for(lambda: not _claim_exists("t1", 3)), "macro never ended"

    res = client.post("/api/action/cancel/t1/3")
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "already_done": True}
    assert kb.sent == ["enter"]
    assert [e for e in host_app._events if e["level"] == "error"] == []
    assert _stopped_rows() == []


def test_cancel_without_any_claim_is_200():
    """No claim at all (never ran / deck disconnected) is not an error."""
    _setup({})
    client = host_app.app.test_client()
    res = client.post("/api/action/cancel/ghost/3")
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "already_done": True}
    assert [e for e in host_app._events if e["level"] == "error"] == []


# --- isolation between devices --------------------------------------------

def test_cancel_stops_only_its_own_device():
    """Two decks run at once; stopping A's key 3 must not touch B."""
    _setup({
        "tA": (3, [{"type": "delay", "value": 800},
                   {"type": "key", "value": "enter"}]),
        "tB": (3, [{"type": "delay", "value": 800},
                   {"type": "key", "value": "tab"}]),
    })
    kb = _StubKeyboard()
    client = host_app.app.test_client()

    with _platform(kb):
        host_app._run_action_for("tA", 3)
        host_app._run_action_for("tB", 3)
        assert _wait_for(lambda: _claim_exists("tA", 3) and _claim_exists("tB", 3))

        assert client.post("/api/action/cancel/tA/3").get_json() == {
            "ok": True, "cancelling": True}

        assert _wait_for(lambda: bool(_stopped_rows()))
        assert _wait_for(lambda: not _claim_exists("tA", 3)
                         and not _claim_exists("tB", 3))

    assert "enter" not in kb.sent      # A stopped
    assert "tab" in kb.sent            # B ran to completion
    assert any("按键 4" in e["text"] for e in _stopped_rows())
    assert any("动作成功" in e["text"] and e["dev"] == "tB"
               for e in host_app._events)


# --- the /api/action/test response ----------------------------------------

def test_test_route_reports_cancelled_instead_of_failure():
    """The HTTP response carries ``cancelled: true`` — not a red failure."""
    _setup({"t1": (3, [{"type": "text", "value": "pw"},
                       {"type": "key", "value": "enter"}])})
    kb = _StubKeyboard(write_delay=0.5)
    client = host_app.app.test_client()
    # A second client: Flask's test client is not meant to be shared between
    # the request thread and the cancelling thread.
    cancel_client = host_app.app.test_client()

    def _cancel_when_claimed():
        if _wait_for(lambda: _claim_exists("t1", 3), timeout=3):
            cancel_client.post("/api/action/cancel/t1/3")

    with _platform(kb):
        threading.Thread(target=_cancel_when_claimed, daemon=True).start()
        res = client.post("/api/action/test/t1/3")

    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is False
    assert body["cancelled"] is True
    assert "已停止" in body["error"]

    assert kb.sent == []                       # key:enter never sent
    assert [e for e in host_app._events if e["level"] == "error"] == []
    assert len(_stopped_rows()) == 1
    text = " ".join(e["text"] for e in host_app._events)
    assert "pw" not in text, "macro body leaked into the log"


# --- the cancel flag itself ------------------------------------------------

def test_cancel_flag_is_per_claim():
    """A new claim gets a fresh event; a stale flag cannot leak into it."""
    _setup({})
    with host_app._lock:
        host_app._action_running.clear()

    token, busy = host_app._begin_action("t9", 3)
    assert busy is None
    first = host_app._action_cancel_event("t9", 3)
    assert first is not None and not first.is_set()

    assert host_app._cancel_action("t9", 3) is True
    assert first.is_set()
    # Same claim, second request: nothing to flip.
    assert host_app._cancel_action("t9", 3) is False

    host_app._end_action("t9", 3, token)
    assert host_app._action_cancel_event("t9", 3) is None

    token2, _ = host_app._begin_action("t9", 3)
    second = host_app._action_cancel_event("t9", 3)
    assert second is not None and not second.is_set()
    host_app._end_action("t9", 3, token2)
