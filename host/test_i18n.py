"""WS-529 i18n regression tests.

The assertions drive the **production** modules: ``i18n.resolve_lang`` /
``i18n.t`` / ``app.api_i18n`` / the persisted ``config.lang``.  Nothing is
re-implemented here, so changing the resolution rules or dropping a locale key
makes these fail.

Run with::

    pytest host/test_i18n.py
"""

import json
import pytest

import app as app_module
import i18n


@pytest.fixture(autouse=True)
def _restore_language():
    """The language is process-global: a test that switches it must not leak.

    Every other module asserts Chinese strings, so leaving the process on
    en-US would fail dozens of unrelated tests in a full-session run.
    """
    try:
        yield
    finally:
        i18n.set_lang("zh-CN")
        app_module._config["lang"] = "system"


# --------------------------------------------------------------------------
# resolve_lang
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "system_locale, expected",
    [
        ("zh_CN.UTF-8", "zh-CN"),
        ("zh-CN", "zh-CN"),
        ("zh_TW.UTF-8", "zh-CN"),
        ("en_US.UTF-8", "en-US"),
        ("en_GB", "en-US"),
        ("de_DE.UTF-8", "en-US"),
        ("fr_FR", "en-US"),
        ("C", "zh-CN"),
        ("POSIX", "zh-CN"),
    ],
)
def test_resolve_system_locale(monkeypatch, system_locale, expected):
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(name, raising=False)
    if system_locale:
        monkeypatch.setenv("LANG", system_locale)
    assert i18n.resolve_lang("system") == expected


def test_resolve_explicit_preference_wins(monkeypatch):
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert i18n.resolve_lang("en-US") == "en-US"
    assert i18n.resolve_lang("zh-CN") == "zh-CN"


@pytest.mark.parametrize(
    "pref, expected",
    [
        ("zh", "zh-CN"),
        ("zh_TW", "zh-CN"),
        ("en", "en-US"),
        ("en_GB", "en-US"),
        ("", "system"),
        ("klingon", "system"),
    ],
)
def test_normalize_pref(pref, expected):
    assert i18n.normalize_pref(pref) == expected


def test_unknown_system_locale_falls_back_to_chinese(monkeypatch):
    monkeypatch.setenv("LANG", "")
    monkeypatch.setenv("LC_ALL", "")
    monkeypatch.setenv("LC_MESSAGES", "")
    monkeypatch.setenv("LANGUAGE", "")
    monkeypatch.setattr(i18n, "_system_tag", lambda: "")
    assert i18n.resolve_lang("system") == "zh-CN"


# --------------------------------------------------------------------------
# message table
# --------------------------------------------------------------------------
def _load(tag):
    with open(f"{i18n.LOCALES_DIR}/{tag}.json", encoding="utf-8") as fh:
        return json.load(fh)


def test_both_locales_have_the_same_keys():
    assert set(_load("zh-CN")) == set(_load("en-US"))


def test_t_falls_back_to_chinese_for_missing_key(monkeypatch, capsys):
    """A missing en-US key must render the zh text and warn, never the raw key."""
    saved = i18n._messages["en-US"]
    monkeypatch.setitem(i18n._messages, "en-US", {})
    monkeypatch.setattr(i18n, "_current_lang", "en-US")
    monkeypatch.setattr(i18n, "_warned", set())
    try:
        text = i18n.t("ui.tab_keys")
    finally:
        i18n._messages["en-US"] = saved
    err = capsys.readouterr().err
    assert text == _load("zh-CN")["ui.tab_keys"]
    assert "WARNING" in err and "ui.tab_keys" in err


def test_t_substitutes_named_placeholders(monkeypatch):
    monkeypatch.setattr(i18n, "_current_lang", "en-US")
    assert i18n.t("ui.uptime_s", seconds=42) == "42s"


# --------------------------------------------------------------------------
# persisted preference + API
# --------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "save_config", lambda: None)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_config_lang_defaults_to_system(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(app_module, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(app_module, "_config", {"devices": {}, "settings": {}})
    app_module.load_config()
    assert app_module._config["lang"] == "system"


def test_config_lang_survives_a_reload(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(app_module, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(app_module, "_config", {"devices": {}, "settings": {},
                                                "skipped_version": "", "last_update": {},
                                                "update_fail_count": 0, "lang": "en-US"})
    app_module.save_config()
    written = json.loads(cfg.read_text(encoding="utf-8"))
    assert written["lang"] == "en-US"
    monkeypatch.setattr(app_module, "_config", {"devices": {}, "settings": {}})
    app_module.load_config()
    assert app_module._config["lang"] == "en-US"


def test_api_i18n_returns_the_requested_locale(client):
    res = client.get("/api/i18n?lang=en-US")
    assert res.status_code == 200
    body = res.get_json()
    assert body["lang"] == "en-US"
    assert body["messages"]["ui.tab_keys"] == "Keys"


def test_api_desktop_persists_lang(client, monkeypatch):
    monkeypatch.setattr(app_module.autostart, "status", lambda: {
        "enabled": False, "admin": False, "supported": True, "admin_supported": True,
        "method": "", "detail": "", "error": ""})
    res = client.post("/api/desktop", json={"lang": "en-US"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["lang"] == "en-US"
    assert body["lang_effective"] == "en-US"
    assert app_module._config["lang"] == "en-US"


# --------------------------------------------------------------------------
# rendered text stays, code + params are added
# --------------------------------------------------------------------------
def test_event_row_carries_code_and_params():
    item = app_module.add_event("cfg", i18n.t("ui.uptime_s", seconds=7), level="ok")
    assert item["text"] == "7 秒"
    assert item["code"] == "ui.uptime_s"
    assert item["params"] == {"seconds": 7}


def test_event_row_without_code_is_still_plain():
    item = app_module.add_event("line", "VERSION:1.3.0", level="info")
    assert item["text"] == "VERSION:1.3.0"
    assert item["code"] == ""


def test_api_error_keeps_text_and_exposes_code(client):
    res = client.post("/api/ota/nosuchdeck/cancel")
    assert res.status_code == 404
    body = res.get_json()
    assert body["ok"] is False
    assert body["code"] == "error.ota_no_task"
    assert body["error"]  # rendered sentence, not the key
    assert body["error"] != body["code"]


# --------------------------------------------------------------------------
# WS-541: regressions the first round missed
# --------------------------------------------------------------------------
def test_validate_pins_speaks_english_without_crashing(monkeypatch):
    """EN pin validation used to 500: ``%d`` got the button label string.

    ``error.pin_gpio_on_chip`` is now a named placeholder template, so the
    real production entry point must render instead of raising TypeError.
    """
    monkeypatch.setattr(i18n, "_current_lang", "en-US")
    errors = app_module.validate_pins([0, 1, 2, 3, 4, 5, 6, 7, 11], "C3")
    assert errors
    assert errors[0]["code"] == "reserved"
    assert errors[0]["message"] == (
        "GPIO 11 on Button 9 is an internal flash pin on the C3 "
        "and cannot be used as a button"
    )


def test_api_defaults_validates_pins_in_english(client, monkeypatch):
    """The page's real request (GET /api/defaults?chip=C3&pins=…) must be 200."""
    monkeypatch.setattr(i18n, "_current_lang", "en-US")
    res = client.get("/api/defaults?chip=C3&pins=0,1,2")
    assert res.status_code == 200
    body = res.get_json()
    assert body["pinErrors"], "the three pins supplied are valid, the rest are empty"
    assert body["pinErrors"][0]["message"].startswith("Button 4")


def test_update_events_carry_named_params():
    """``app.js`` renders ``t(e.code, e.params)``; a printf row shows ``%s``.

    The event must therefore carry ``{"version": ...}``, not ``{"args": [...]}``.
    """
    i18n.set_lang("en-US")
    item = app_module.add_event("update", i18n.t("log.app_updated", version="v1.4.0"),
                                level="ok")
    assert item["text"] == "Host updated to v1.4.0"
    assert item["code"] == "log.app_updated"
    assert item["params"] == {"version": "v1.4.0"}
    assert "%" not in item["text"]


def test_uptime_wrap_copy_follows_a_language_switch():
    """Module-level ``t()`` froze the language at import time (WS-541)."""
    i18n.set_lang("zh-CN")
    assert app_module.uptime_wrap_text() == "本次连接内已运行"
    i18n.set_lang("en-US")
    assert app_module.uptime_wrap_text() == "Running since this connection"
    assert app_module.uptime_wrap_note() == "Device uptime counter wrapped"


def test_port_busy_message_follows_a_language_switch():
    import desktop

    i18n.set_lang("zh-CN")
    assert desktop.port_busy_message().startswith("已有普通权限实例在运行")
    i18n.set_lang("en-US")
    message = desktop.port_busy_message(8788)
    assert message.startswith("An instance is already running")
    assert "(port 8788 is already in use)" in message


def test_api_desktop_normalizes_an_unknown_lang(client, monkeypatch):
    """``{"lang": "xx"}`` used to be persisted verbatim into config.json."""
    monkeypatch.setattr(app_module.autostart, "status", lambda: {
        "enabled": False, "admin": False, "supported": True, "admin_supported": True,
        "method": "", "detail": "", "error": ""})
    res = client.post("/api/desktop", json={"lang": "xx"})
    assert res.status_code == 200
    assert res.get_json()["lang"] == "system"
    assert app_module._config["lang"] == "system"

