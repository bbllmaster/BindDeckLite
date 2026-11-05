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
