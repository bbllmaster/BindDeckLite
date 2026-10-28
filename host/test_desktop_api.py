"""API-level tests for the desktop / autostart endpoints.

These drive the **production** Flask view ``api_desktop`` through
``app.test_client()`` and monkeypatch only the executor
(``autostart.enable``) - the request parsing, the branch selection and the
response payload all stay production code.  Nothing is re-implemented here,
so a change in ``app.py`` that drops the backend reason fails these tests.

Run with::

    pytest host/test_desktop_api.py
"""

import pytest

import app as app_module
import autostart
import desktop


@pytest.fixture
def client(monkeypatch):
    """Flask test client with the config writes neutralised."""
    # ``save_config`` would write host/config.json into the checkout.
    monkeypatch.setattr(app_module, "save_config", lambda: None)
    desktop.STATE["error"] = ""
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_admin_autostart_failure_keeps_the_backend_reason(client, monkeypatch):
    """A refused elevation must surface its reason, not an empty string.

    ``desktop_payload()`` carries its own ``"error"`` key (the desktop state
    error, empty on a healthy host).  Spreading it *after* the real reason
    overwrote the reason with ``""``, so the UI showed the generic
    "设置失败" instead of "已取消提权".
    """
    def _fake_enable(minimized=True, admin=False):
        return {"ok": False, "error": "已取消提权"}

    monkeypatch.setattr(autostart, "enable", _fake_enable)

    resp = client.post("/api/desktop", json={"admin_autostart": True})

    assert resp.status_code == 500
    body = resp.get_json()
    assert body["error"] == "已取消提权"
    assert body["ok"] is False


def test_admin_autostart_failure_keeps_the_rest_of_the_payload(client, monkeypatch):
    """The error response must still look like a normal desktop payload."""
    def _fake_enable(minimized=True, admin=False):
        return {"ok": False, "error": "已取消提权"}

    monkeypatch.setattr(autostart, "enable", _fake_enable)

    body = client.post("/api/desktop", json={"admin_autostart": True}).get_json()

    assert body["autostart"]["supported"] in (True, False)
    assert "platform" in body
    assert "start_minimized" in body
    assert "close_button_behavior" in body


def test_admin_autostart_success_has_no_error(client, monkeypatch):
    """A successful switch keeps ``error`` empty (payload and reason agree)."""
    def _fake_enable(minimized=True, admin=False):
        return {"ok": True}

    monkeypatch.setattr(autostart, "enable", _fake_enable)

    resp = client.post("/api/desktop", json={"admin_autostart": True})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["error"] == ""
