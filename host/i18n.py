"""Host-side i18n: locale tables, language resolution and ``t()`` (WS-529).

The host UI ships two locales, ``zh-CN`` (default, the single source of truth)
and ``en-US``.  Keys are flat strings namespaced by area (``ui.*`` / ``log.*``
/ ``error.*`` / ``tray.*`` / ``dialog.*`` / ``help.*``); values may carry
``{name}`` placeholders, which :func:`t` fills by keyword so word order can
change per language.

``en-US`` missing a key is not fatal: the zh-CN text is used and a
``WARNING`` is printed, so a half-translated page still renders and the gap is
visible in the log instead of showing a raw key to the user.

Nothing here imports :mod:`app`, so ``t()`` is usable from ``desktop.py``,
``autostart.py``, ``online_update.py`` and ``ble_link.py`` too.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading

DEFAULT_LANG = "zh-CN"
SUPPORTED_LANGS = ("zh-CN", "en-US")
SYSTEM_PREF = "system"

LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_lock = threading.RLock()
_messages: dict[str, dict[str, str]] = {}
_current_lang = DEFAULT_LANG
_warned: set[str] = set()


def _load(tag: str) -> dict[str, str]:
    path = os.path.join(LOCALES_DIR, f"{tag}.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        print(f"[i18n] {tag}.json is not a flat object", file=sys.stderr)
    except FileNotFoundError:
        print(f"[i18n] missing locale file: {path}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a broken locale must not kill the app
        print(f"[i18n] failed to load {tag}.json: {exc}", file=sys.stderr)
    return {}


def _ensure_loaded() -> None:
    if not _messages:
        for tag in SUPPORTED_LANGS:
            _messages[tag] = _load(tag)


# --------------------------------------------------------------------------
# language resolution
# --------------------------------------------------------------------------
def normalize_pref(pref: str | None) -> str:
    """Map a stored preference onto ``system`` / a supported tag."""
    value = (pref or "").strip()
    if value in SUPPORTED_LANGS or value == SYSTEM_PREF:
        return value
    # Tolerate a hand-edited config.json ("zh", "en", "en_US", "zh_CN.UTF-8").
    lowered = value.lower().replace("_", "-")
    if lowered.startswith("zh"):
        return "zh-CN"
    if lowered.startswith("en"):
        return "en-US"
    return SYSTEM_PREF


def _system_tag() -> str:
    """Best-effort OS locale, as a tag-like string ("" when unknown)."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(name, "").strip()
        if value:
            return value.split(":", 1)[0]
    try:
        import locale

        try:
            tag = locale.getlocale()[0]
        except (ValueError, TypeError):
            tag = None
        if not tag:
            tag = getattr(locale, "getdefaultlocale", lambda: (None, None))()[0]
        return tag or ""
    except Exception:  # noqa: BLE001 - locale probing must never raise
        return ""


def resolve_lang(pref: str | None) -> str:
    """Resolve the effective language for a stored preference.

    ``system`` + a Chinese OS locale → ``zh-CN``; any other known language →
    ``en-US``; an unknown/undetectable locale → ``zh-CN`` (the default).
    """
    value = normalize_pref(pref)
    if value != SYSTEM_PREF:
        return value
    tag = _system_tag().lower().replace("_", "-")
    if not tag:
        return DEFAULT_LANG
    if tag.startswith("zh"):
        return "zh-CN"
    head = tag.split("-", 1)[0].split(".", 1)[0]
    if head == "en":
        return "en-US"
    if head in ("c", "posix"):
        return DEFAULT_LANG
    # Any other real language: English is the only other shipped locale.
    return "en-US" if len(head) in (2, 3) and head.isalpha() else DEFAULT_LANG


def set_lang(pref: str | None) -> str:
    """Apply a stored preference and return the effective language."""
    global _current_lang
    with _lock:
        _ensure_loaded()
        _current_lang = resolve_lang(pref)
        return _current_lang


def current_lang() -> str:
    return _current_lang


def messages(lang: str | None = None) -> dict[str, str]:
    """Full message table for one language (zh-CN text fills the gaps)."""
    _ensure_loaded()
    tag = lang if lang in SUPPORTED_LANGS else resolve_lang(lang or SYSTEM_PREF)
    table = dict(_messages.get(DEFAULT_LANG, {}))
    if tag != DEFAULT_LANG:
        table.update(_messages.get(tag, {}))
    return table


# --------------------------------------------------------------------------
# translate
# --------------------------------------------------------------------------
class Message(str):
    """A rendered string that remembers which key and params produced it.

    ``add_event()`` puts ``code`` / ``params`` on the event alongside the
    rendered ``text`` so the web UI can re-translate an entry when the language
    changes, while older consumers (SSE clients, tests) keep reading ``text``.
    Being a ``str`` subclass means existing ``"..." % x`` / ``+`` / ``in`` uses
    are untouched.
    """

    __slots__ = ("code", "params")

    def __new__(cls, text: str, code: str, params: dict) -> "Message":
        obj = super().__new__(cls, text)
        obj.code = code
        obj.params = dict(params)
        return obj

    def __mod__(self, args):
        """``t("key") % value`` keeps the key, so the error body still has code."""
        rendered = super().__mod__(args)
        values = list(args) if isinstance(args, tuple) else [args]
        return Message(rendered, self.code, {"args": values})


def t(key: str, **params) -> str:
    """Translate ``code`` for the current language, filling ``{name}`` params."""
    _ensure_loaded()
    lang = _current_lang
    table = _messages.get(lang) or {}
    text = table.get(key)
    if text is None:
        text = _messages.get(DEFAULT_LANG, {}).get(key)
        if text is None:
            if key not in _warned:
                _warned.add(key)
                print(f"[i18n] WARNING missing message key: {key}", file=sys.stderr)
            return Message(key, key, params)
        if lang != DEFAULT_LANG and key not in _warned:
            _warned.add(key)
            print(
                f"[i18n] WARNING {lang}.json is missing key {key!r}; "
                f"falling back to {DEFAULT_LANG}",
                file=sys.stderr,
            )
    if not params:
        return Message(text, key, {})

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(params[name]) if name in params else match.group(0)

    return Message(_PLACEHOLDER.sub(repl, text), key, params)


def has(key: str) -> bool:
    _ensure_loaded()
    return key in _messages.get(DEFAULT_LANG, {})


# Restore the language the user picked last time.  ``app.py`` calls this with
# the persisted ``config.lang``; importing this module alone keeps the default.
_ensure_loaded()
