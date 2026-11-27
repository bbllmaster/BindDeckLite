"""WS-702: a deck dropping off must stop being silent.

The assertions drive the **production** decision function
(``app.disconnect_notice``), the real watchdog pass (``app._link_watch_sample``)
and the real ``desktop.notify`` wrapper.  Nothing is re-implemented here, so
changing a threshold, dropping a gate or letting a backend error escape makes
these tests fail.

No BLE, no GUI, no network::

    cd host && python -m pytest test_link_watch.py -q
"""

import copy
import json
import time

import pytest

import app as app_module
import desktop
import i18n

BASE = 1_700_000_000.0


@pytest.fixture(autouse=True)
def _clean_watch_state():
    """The watchdog state is process-global; never leak it into another test."""
    saved_settings = copy.deepcopy(app_module._config["settings"])
    saved_tray = dict(desktop.STATE)
    i18n.set_lang("zh-CN")
    app_module._link_state.clear()
    app_module._link_active_until.clear()
    app_module._link_active_global_until = 0.0
    app_module._link_watch_quit = False
    try:
        yield
    finally:
        app_module._link_state.clear()
        app_module._link_active_until.clear()
        app_module._link_active_global_until = 0.0
        app_module._link_watch_quit = False
        app_module._config["settings"] = saved_settings
        desktop.STATE.update(saved_tray)
        i18n.set_lang("zh-CN")


def _notice(**over):
    args = dict(prev_connected=True, now_connected=False,
                conn_since=BASE, now=BASE + 120,
                last_notified=None, suppress_until=0.0,
                notify_enabled=True, tray_active=True, window_hidden=True)
    args.update(over)
    return app_module.disconnect_notice(**args)


def _dev(connected, dev_id="d1", name="Deck", error=None):
    return {"dev_id": dev_id, "name": name, "address": "AA:BB",
            "connected": connected, "last_error": error}


def _record(monkeypatch):
    rows, notes = [], []
    monkeypatch.setattr(app_module, "add_event",
                        lambda kind, text, dev="", level="info":
                        rows.append({"kind": kind, "text": str(text), "dev": dev,
                                     "level": level}))
    monkeypatch.setattr(desktop, "notify",
                        lambda title, body: notes.append((str(title), str(body))) or True)
    desktop.STATE["tray_active"] = True
    desktop.STATE["window_hidden"] = True
    return rows, notes


# --- one sample: edge + notification gates ---------------------------------
def test_a_disconnect_edge_logs_warn_and_notifies():
    decision = _notice()
    assert decision["log"] == "disconnect"
    assert decision["level"] == "warn"
    assert decision["notify"] is True
    assert decision["notify_reason"] == "notified"


def test_a_reconnect_edge_logs_info_and_never_notifies():
    decision = _notice(prev_connected=False, now_connected=True)
    assert decision["log"] == "reconnect"
    assert decision["level"] == "info"
    assert decision["notify"] is False


def test_the_same_state_twice_writes_nothing():
    """2 s sampling must not repeat the line on every tick."""
    for prev, now in ((True, True), (False, False)):
        decision = _notice(prev_connected=prev, now_connected=now)
        assert decision["log"] is None
        assert decision["notify"] is False


def test_a_deck_never_seen_connected_produces_no_line():
    assert _notice(prev_connected=None, now_connected=False)["log"] is None
    assert _notice(prev_connected=None, now_connected=True)["log"] is None


def test_only_20_seconds_of_link_logs_but_does_not_notify():
    decision = _notice(conn_since=BASE, now=BASE + 20)
    assert decision["log"] == "disconnect"
    assert decision["notify"] is False
    assert decision["notify_reason"] == "unstable"


def test_stability_gate_is_exactly_60_seconds():
    """The acceptance bar: 59.9 s quiet, 60 s allowed.

    This is the regression that turns red if ``LINK_STABLE_SECONDS`` is
    changed to 0 (or to anything that lets a 20 s link notify).
    """
    assert _notice(conn_since=BASE, now=BASE + 59.9)["notify"] is False
    assert _notice(conn_since=BASE, now=BASE + 60)["notify"] is True


def test_a_visible_window_only_writes_the_log():
    decision = _notice(window_hidden=False)
    assert decision["log"] == "disconnect"
    assert decision["notify"] is False
    assert decision["notify_reason"] == "window_visible"


def test_no_tray_or_no_switch_never_notifies():
    assert _notice(tray_active=False)["notify_reason"] == "no_tray"
    assert _notice(notify_enabled=False)["notify_reason"] == "disabled"


def test_cooldown_blocks_a_second_notification_within_five_minutes():
    recent = _notice(last_notified=BASE + 60, now=BASE + 120)
    assert recent["notify"] is False
    assert recent["notify_reason"] == "cooldown"
    later = _notice(last_notified=BASE + 60, now=BASE + 360)
    assert later["notify"] is True


def test_the_active_window_marks_the_line_and_stays_quiet():
    decision = _notice(suppress_until=BASE + 140, now=BASE + 120)
    assert decision["log"] == "disconnect"
    assert decision["active"] is True
    assert decision["notify"] is False
    assert decision["notify_reason"] == "active_disconnect"


# --- the watchdog state machine -------------------------------------------
def test_five_burst_drops_notify_once_and_log_every_edge(monkeypatch):
    """5 drops inside the 5-minute cooldown -> 1 notification, 10 log rows.

    Reconnects sit just past the 60 s stability bar so the only gate that can
    silence drops 2..5 is the cooldown (not the stability gate).
    """
    rows, notes = _record(monkeypatch)
    t = BASE
    app_module._link_watch_sample(now=t, devices=[_dev(True)])

    for _ in range(5):
        t += 61
        app_module._link_watch_sample(now=t, devices=[_dev(False)])
        t += 0.5
        app_module._link_watch_sample(now=t, devices=[_dev(True)])

    disconnects = [r for r in rows if r["text"].startswith("设备已断开")]
    reconnects = [r for r in rows if r["text"].startswith("设备已重新连接")]
    assert len(disconnects) == 5
    assert len(reconnects) == 5
    assert all(r["level"] == "warn" for r in disconnects)
    assert all(r["level"] == "info" for r in reconnects)
    assert len(notes) == 1  # 5 min cooldown, same deck


def test_the_drop_line_carries_the_reason_or_the_fallback(monkeypatch):
    rows, _ = _record(monkeypatch)
    app_module._link_watch_sample(now=BASE, devices=[_dev(True)])
    app_module._link_watch_sample(
        now=BASE + 90,
        devices=[_dev(False, error={"at": BASE, "kind": "X", "text": "连接超时"})])
    app_module._link_watch_sample(
        now=BASE + 400, devices=[_dev(True, error=None)])
    app_module._link_watch_sample(now=BASE + 500, devices=[_dev(False, error=None)])

    drop = rows[0]["text"]
    assert "Deck" in drop and "连接超时" in drop
    assert rows[1]["text"] == "设备已重新连接：Deck"
    assert "设备未上报错误" in rows[2]["text"]
    assert rows[2]["text"].count("设备已断开") == 1


def test_a_reconnect_is_silent_when_the_drop_was_never_seen(monkeypatch):
    """Only a watchdog-recorded drop justifies 「已重新连接」.

    Otherwise the existing 「连接已恢复」 line from ``_note_link_connected``
    would be duplicated for a plain connect-failure recovery.
    """
    rows, _ = _record(monkeypatch)
    app_module._link_watch_sample(now=BASE, devices=[_dev(False)])
    app_module._link_watch_sample(now=BASE + 10, devices=[_dev(True)])
    assert rows == []


def test_an_active_reboot_appends_the_suffix_and_stays_quiet(monkeypatch):
    rows, notes = _record(monkeypatch)
    now = time.time()
    app_module._link_watch_sample(now=now, devices=[_dev(True)])
    app_module.note_active_disconnect("d1")
    app_module._link_watch_sample(now=now + 5, devices=[_dev(False)])

    assert len(rows) == 1
    assert "本站操作触发" in rows[0]["text"]
    assert notes == []


def test_the_reboot_action_marks_the_active_window(monkeypatch):
    """The mark is set by the production ``send()`` funnel, not by the UI."""
    monkeypatch.setattr(app_module.ble, "send", lambda dev_id, cmd: True)
    monkeypatch.setattr(app_module, "add_event", lambda *a, **k: None)
    app_module.send("d1", "CMD:REBOOT")
    assert app_module._link_active_until["d1"] > time.time()


# --- desktop notify degrades silently -------------------------------------
def test_notify_without_a_tray_returns_false():
    desktop.STATE["tray_active"] = False
    assert desktop.notify("BindDeckLite", "body") is False


def test_notify_swallows_a_backend_error(monkeypatch):
    class _Broken:
        def notify(self, *args, **kwargs):
            raise RuntimeError("notifications are not supported here")

    monkeypatch.setattr(desktop, "_tray_icon", _Broken())
    desktop.STATE["tray_active"] = True
    assert desktop.notify("BindDeckLite", "body") is False  # no exception


# --- settings --------------------------------------------------------------
def test_payload_reports_the_switch_default_on_for_an_old_config():
    settings = app_module._config["settings"]
    settings.pop("notify_on_disconnect", None)
    try:
        assert app_module.desktop_payload()["notify_on_disconnect"] is True
    finally:
        settings["notify_on_disconnect"] = True


def test_legacy_config_keeps_other_settings_and_the_file_untouched(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    original = json.dumps({"settings": {"close_button_behavior": "exit",
                                        "start_minimized": False}})
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(app_module, "CONFIG_PATH", str(path))
    before = copy.deepcopy(app_module._config)
    try:
        app_module.load_config()
        settings = app_module._config["settings"]
        # 旧文件缺字段 → 界面拿到的是「开」；已有设置原样保留。
        assert app_module.desktop_payload()["notify_on_disconnect"] is True
        assert settings["close_button_behavior"] == "exit"
        assert settings["start_minimized"] is False
        # 只是读，不因为缺一个字段回写整个 config.json。
        assert path.read_text(encoding="utf-8") == original
    finally:
        app_module._config.clear()
        app_module._config.update(before)


def test_desktop_api_round_trips_the_switch(monkeypatch):
    monkeypatch.setattr(app_module, "save_config", lambda: True)
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()

    assert client.get("/api/desktop").get_json()["notify_on_disconnect"] is True
    resp = client.post("/api/desktop", json={"notify_on_disconnect": False})
    assert resp.status_code == 200
    assert app_module._config["settings"]["notify_on_disconnect"] is False
    assert client.get("/api/desktop").get_json()["notify_on_disconnect"] is False
