"""BindDeckLite host application (上位机).

Replaces the OLED-oriented BindDeck PC app with a smaller control centre for
the Lite deck: 9 buttons + EC11 encoder, no display, no menu key.

Responsibilities
----------------
* Discover/connect every ``BindDeckLite-XXXX`` deck over BLE (``ble_link``).
* Edit and push the device configuration (button pins, HID keys, encoder mode,
  auto-sleep) over the BLE CFG service, or WiFi UDP while in config mode.
* Show live key/encoder events and a device log.
* Run optional host-side actions (launch app / URL / command / macro) when a
  button is pressed. The deck also sends its HID key itself, so actions are an
  add-on, not a requirement.

Run:  python app.py            (opens http://127.0.0.1:8788 in a browser)
      python app.py --desktop  (native pywebview window + tray; no console/browser)
      python app.py --file-dialog file   (internal: native picker child)

Set ``BINDDECKLITE_DESKTOP=1`` to force desktop mode (same as --desktop).
The frozen Windows build (``BindDeckLite.exe``) always starts in desktop mode:
Flask serves in the background while pywebview renders the existing Web UI in
a real window (see ``desktop.run_webview``).  ``desktop.py`` and ``autostart.py``
own the tray, native dialogs and login-start bits.
"""

from __future__ import annotations

import argparse
import copy
import ctypes  # only used by the Windows clipboard / window helpers below
import hashlib
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque

from flask import Flask, jsonify, render_template, request

import autostart
import desktop
import online_update
from ble_link import BleLinkManager, scan_reason
from i18n import (SUPPORTED_LANGS, SYSTEM_PREF, current_lang, messages,
                  normalize_pref, resolve_lang, set_lang, t)

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))


def _writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _data_dir() -> str:
    """Persistent data (config/ble cache) — next to the exe, else LOCALAPPDATA."""
    if not getattr(sys, "frozen", False):
        return SOURCE_DIR
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if _writable(exe_dir):
        return exe_dir
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        candidate = os.path.join(base, "BindDeckLite")
        if _writable(candidate):
            return candidate
    fallback = os.path.join(os.path.expanduser("~"), ".binddecklite")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def _resource_root() -> str:
    """Directory holding the bundled templates/static assets.

    When packaged with PyInstaller (one-file) the app runs out of a temp
    directory exposed as ``sys._MEIPASS``; Flask's default ``root_path`` is
    derived from ``__file__`` and points at the exe's folder instead, so a
    relative ``templates/`` / ``static/`` resolves to a non-existent path and
    the frozen app 404s on every page. Return the absolute bundle root so the
    Flask folders resolve correctly both when run from source and when frozen.
    """
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


RESOURCE_ROOT = _resource_root()
DATA_DIR = _data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
PORT = int(os.environ.get("BINDDECKLITE_PORT", "8788"))

# How long the elevated instance waits for the running one to release the
# port before it gives up and reports the conflict (WS-457).
ELEVATED_PORT_WAIT = 25.0

# Same vendor service as the firmware's ble_config.cpp (shared protocol).
CONTROL_SERVICE = "b1d3c0de-0001-4a5b-9c6d-1a2b3c4d5e6f"

# Mutable reference for the running webview's close-button behavior.  The
# ``_on_closing`` closure in ``desktop.run_webview`` reads this dict instead of
# capturing a plain string, so a setting change through the settings page takes
# effect immediately without restarting the app.
_close_behavior_ref = {"behavior": "minimize"}

app = Flask(__name__,
            static_folder=os.path.join(RESOURCE_ROOT, "static"),
            template_folder=os.path.join(RESOURCE_ROOT, "templates"))
app.config["JSON_AS_ASCII"] = False

_lock = threading.RLock()
_events = deque(maxlen=400)
_event_seq = 0
_last_press = {}          # dev_id -> (index, timestamp)
_action_running = {}      # (dev_id, idx) -> {"started", "token", "thread", "cancel"}; WS-359/360, WS-495
_action_token_seq = 0     # bumped under _lock, one token per claim
# WS-389: encoder rotation actions share the same preemption model as keys, but
# are keyed by (dev_id, "CW"|"CCW") because each direction has its own action.
_encoder_action_running = {}   # (dev_id, dir) -> {"started", "token", "thread"}
_encoder_action_token_seq = 0  # bumped under _lock, one token per claim
_encoder_last_trigger = {}     # (dev_id, dir) -> monotonic; one-shot 800 ms cooldown
_ENCODER_COOLDOWN_S = 0.8      # launch / url / command: at most one trigger per dir
_ENCODER_ONE_SHOT = {"launch", "url", "command"}
_ping_sent = {}           # dev_id -> monotonic send time
_bond_queried = set()     # dev_ids whose binding status we already asked for
_udp_sock = None
_config = {"devices": {}, "settings": {
    "actions_enabled": True,
    "udp_fallback": True,
    "start_minimized": True,
    "close_button_behavior": "minimize",
    "admin_autostart": False,
    "recent_paths": [],
},
    # WS-523: 在线更新状态。这三个键写在 config.json **顶层**（不放 settings），
    # load_config() 会显式把它们读回来。
    "skipped_version": "",     # 用户跳过的上位机版本
    "last_update": {},         # {status, from, to, at, result, marker}
    "update_fail_count": 0,    # 连续失败次数，>=2 暂停自动更新
    # WS-557: 上位机更新通道（stable = 正式 v* Release，rolling = main-* nightly）。
    "update_channel": online_update.DEFAULT_UPDATE_CHANNEL,
    # WS-529: 界面语言偏好（system / zh-CN / en-US），与上面三个键同层。
    "lang": SYSTEM_PREF,
}

# WS-359: a stuck action thread never reaches its ``finally``, so an entry kept
# past this many seconds is force-released instead of breathing forever.
ACTION_STUCK_SECONDS = 120.0

# --- defaults -------------------------------------------------------------
DEFAULT_PINS = {
    "ESP32": [13, 12, 14, 27, 32, 33, 25, 26, 4],
    "C3": [0, 1, 2, 3, 4, 5, 6, 7, 8],
}
# WS-499: the tail two codes only exist in the 12-key layout (the EC11 A/B
# lines used as plain buttons). Keeping them in the table costs nothing in the
# encoder layout — input_count() simply never shows them.
DEFAULT_KEYS = [0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9,
                0xFA, 0xFB]
DEFAULT_LABELS = ["F13", "F14", "F15", "F16", "F17", "F18", "F19", "F20", "F21",
                  "F22", "F23", "F24"]

# --- WS-546: per-input modifier mask --------------------------------------
# One bit per modifier, left-hand variants only (same as the firmware's MOD_*):
# bit0 Ctrl / bit1 Shift / bit2 Alt / bit3 GUI. This list is the single source
# of the *display order* (Ctrl -> Shift -> Alt -> Win) — the drawer checkboxes
# and the summary card both read it from /api/keycodes, so no second ordering
# rule can drift away in JavaScript.
MOD_MASK = 0x0F
MOD_DEFS = [
    {"bit": 0x01, "id": "kdModCtrl",  "key": "keys.mod_ctrl"},
    {"bit": 0x02, "id": "kdModShift", "key": "keys.mod_shift"},
    {"bit": 0x04, "id": "kdModAlt",   "key": "keys.mod_alt"},
    {"bit": 0x08, "id": "kdModWin",   "key": "keys.mod_win"},
]


def _mask_of(item) -> int:
    """One key item's modifier mask, clamped to the four bits the deck has.

    ``mods`` comes straight from the browser's draft file, so a missing / null
    / odd value is treated as "no modifier" instead of raising inside an apply.
    """
    try:
        return int((item or {}).get("mods", 0) or 0) & MOD_MASK
    except (TypeError, ValueError):
        return 0

# --- WS-499: input layout ------------------------------------------------
# The firmware reports its layout as ``LAYOUT:<0|1>`` and accepts
# ``CFG:LAYOUT:<0|1>``. 1 = encoder layout (9 buttons + EC11 = 10 inputs),
# 0 = 12-key layout (the EC11 SW/A/B lines carry three more buttons = 12).
LAYOUT_ENCODER = 1
LAYOUT_12KEY = 0
INPUT_COUNT_ENCODER = 10
INPUT_COUNT_12KEY = 12


def layout_of(cfg) -> int:
    """The layout stored for one device (unknown -> encoder, i.e. 10 inputs).

    Old firmware never replies to ``CFG:LAYOUT`` and old configs have no
    ``layout`` field; both must keep behaving exactly like today.
    """
    try:
        value = int((cfg or {}).get("layout", LAYOUT_ENCODER))
    except (TypeError, ValueError):
        return LAYOUT_ENCODER
    return LAYOUT_12KEY if value == LAYOUT_12KEY else LAYOUT_ENCODER


def input_count(cfg) -> int:
    """How many input cards this device has: 10 (encoder) or 12 (12-key)."""
    return INPUT_COUNT_12KEY if layout_of(cfg) == LAYOUT_12KEY else INPUT_COUNT_ENCODER


def _input_label(cfg, idx: int) -> str:
    """Display name of one input for the log / event feed (WS-499)."""
    twelve = layout_of(cfg) == LAYOUT_12KEY
    if idx == 9:
        return t("ui.input_button_10_config") if twelve else t("ui.input_encoder_press")
    if 0 <= idx < 9:
        return t("ui.input_button_n") % (idx + 1)
    if twelve and idx < INPUT_COUNT_12KEY:
        return t("ui.input_button_n_extra") % (idx + 1)
    return t("ui.input_button_n") % (idx + 1)


def default_key_item(i: int) -> dict:
    """One ``keys[i]`` entry with the shipped default code and label."""
    code = DEFAULT_KEYS[i] if i < len(DEFAULT_KEYS) else 0
    label = DEFAULT_LABELS[i] if i < len(DEFAULT_LABELS) else str(code)
    # WS-546: ``mods`` lives next to the code (0 = no modifier), so a config
    # written before this feature reads exactly like an all-zero deck.
    return {"code": code, "label": label, "mods": 0,
            "action": {"type": "none", "value": "", "macro": []}}


def ensure_key_items(cfg: dict) -> list:
    """Grow ``cfg["keys"]`` to the full 12-entry table, in place (WS-499/WS-503).

    v1.3.6 stored only 10 entries (the last was the encoder-push code). A deck
    switched to the 12-key layout reports 12 codes, so ``apply_device()`` would
    send 10 and compare them against the 12 that came back — a permanent
    "keys: rejected" the user could never clear, because the read-back used to
    be discarded when it differed. Padding here is idempotent and keeps the
    sent snapshot, the read-back and the 12 input cards on one list.
    """
    keys = cfg.get("keys")
    if not isinstance(keys, list):
        keys = cfg["keys"] = []
    while len(keys) < INPUT_COUNT_12KEY:
        keys.append(default_key_item(len(keys)))
    # WS-546: an archive written before modifiers existed has no ``mods`` on
    # its entries; fill it in (0 = the old behaviour) so the drawer and the
    # apply step always see the same shape.
    for item in keys:
        if isinstance(item, dict):
            item.setdefault("mods", 0)
    return keys


# --- WS-476: pin rules (the host keeps exactly one copy) -------------------
# Mirrors isValidButtonPin() / isValidPinSet() in src/main.cpp. The device page
# used to accept any number, then a cleared box reached CFG:PINS as "None" and
# apply_device() died on int(None) (HTTP 500). The rule lives here and is
# published through /api/defaults so the JavaScript never re-implements it.
PIN_COUNT = 9

# Encoder CLK / DT / SW per chip (main.cpp ENCODER_CLK/DT/SW).
ENCODER_PINS = {
    "C3": {"CLK": 20, "DT": 21, "SW": 10},
    "ESP32": {"CLK": 18, "DT": 19, "SW": 5},
}
# 存“键”而不是译文：模块导入时才 t() 会把语言冻结在启动那一刻，
# 之后切换语言这些标签不会跟着变。
_ENCODER_ROLE_KEYS = {
    "CLK": "ui.pin_encoder_a",
    "DT": "ui.pin_encoder_b",
    "SW": "ui.pin_encoder_press",
}


def pin_chip(chip: str) -> str:
    """Normalise a firmware chip string to one of the two pin profiles.

    Firmware sends ``C3`` or ``ESP32`` (main.cpp FW_CHIP); older configs may
    carry ``ESP32C3`` or an empty string, which falls back to the classic map
    exactly like ``new_device_cfg()`` does.
    """
    return "C3" if "C3" in (chip or "").upper() else "ESP32"


def _pin_span_text(spans, sep="、") -> str:
    """``[[0, 4], [12, 17], [20, 20]]`` -> ``0-4、12-17、20``."""
    return sep.join(str(lo) if lo == hi else "%d-%d" % (lo, hi) for lo, hi in spans)


def _pin_free_spans(rules: dict) -> list:
    """The GPIO ranges a button may actually use, derived from ``rules`` itself.

    Everything here comes out of the same fields ``validate_pins()`` reads —
    the range limit, ``blocked``, the encoder pins and the fixed pins of the
    12-key layout (``extended``) — plus ``avoid`` for pins that are legal but
    not worth spending (C3 18/19 are USB-CDC).  Deriving instead of writing a
    second list keeps the hint from drifting away from the rule again (WS-481).
    """
    occupied = set()
    for lo, hi in rules["blocked"]:
        occupied.update(range(lo, hi + 1))
    occupied.update(rules["encoder"].values())
    # WS-503: inputs 9..11 own the three EC11 lines in the 12-key layout, so
    # the hint must not offer them as free button pins (validate_pins rejects
    # them as duplicates).
    occupied.update(rules.get("extended", ()))
    occupied.update(rules.get("avoid", ()))
    spans = []
    for pin in range(rules["min"], rules["max"] + 1):
        if pin in occupied:
            continue
        if spans and pin == spans[-1][1] + 1:
            spans[-1][1] = pin
        else:
            spans.append([pin, pin])
    return spans


def _reserved_pin_text(reserved) -> str:
    """``[[6, 11, "SPI Flash"]]`` -> ``6-11 SPI Flash``; several joined by 与."""
    return t("ui.and_join").join(
        "%s %s" % (_pin_span_text([[lo, hi]]), label) for lo, hi, label in reserved
    )


def _extended_pin_text(prof: str) -> str:
    """``GPIO10/20/21 = 第 10/11/12 键`` for the 12-key layout (WS-499)."""
    enc = ENCODER_PINS[prof]
    return t("ui.pin_gpio_map_ext") % _pin_span_text(
        [[enc["SW"], enc["SW"]], [enc["CLK"], enc["CLK"]], [enc["DT"], enc["DT"]]], "/")


def pin_rules(chip: str = "", layout: int = LAYOUT_ENCODER) -> dict:
    """The pin constraints for one chip, in the shape the UI renders.

    Descriptive only — ``validate_pins()`` is the single authority on whether
    a value is accepted; this just states the same limits as a hint.  The hint
    text is built from these fields, never from a second hand-written list.

    ``layout`` (WS-499) decides whether the three EC11 pins are off limits
    (encoder layout) or already owned by inputs 9..11 (12-key layout).
    """
    prof = pin_chip(chip)
    twelve = layout_of({"layout": layout}) == LAYOUT_12KEY
    if prof == "C3":
        reserved = [[11, 17, t("ui.pin_internal_flash")]]
        rules = {
            "chip": "C3",
            "count": PIN_COUNT,
            "min": 0,
            "max": 21,
            "blocked": [[lo, hi] for lo, hi, _ in reserved],
            "avoid": [18, 19],   # legal, but USB-CDC — not worth spending
            "encoder": {} if twelve else dict(ENCODER_PINS["C3"]),
            # Fixed pins of inputs 9..11 (the EC11 lines). Published in both
            # layouts so the UI can label the rows without a second copy of
            # the chip map (WS-499).
            "extended": [ENCODER_PINS["C3"]["SW"],
                         ENCODER_PINS["C3"]["CLK"],
                         ENCODER_PINS["C3"]["DT"]],
            "layout": LAYOUT_12KEY if twelve else LAYOUT_ENCODER,
        }
        if twelve:
            rules["hint"] = (
                t("ui.pin_avail_c3")
                % (
                    _pin_span_text(_pin_free_spans(rules)),
                    _reserved_pin_text(reserved),
                    _extended_pin_text(prof),
                    _pin_span_text([[p, p] for p in rules["avoid"]], "/"),
                )
            )
        else:
            rules["hint"] = (
                t("ui.pin_avail_c3_enc")
                % (
                    _pin_span_text(_pin_free_spans(rules)),
                    _reserved_pin_text(reserved),
                    _pin_span_text([[p, p] for p in rules["encoder"].values()], "/"),
                    _pin_span_text([[p, p] for p in rules["avoid"]], "/"),
                )
            )
        return rules

    reserved = [[6, 11, "SPI Flash"], [34, 39, t("ui.pin_input_only")]]
    rules = {
        "chip": "ESP32",
        "count": PIN_COUNT,
        "min": 0,
        "max": 39,
        "blocked": [[lo, hi] for lo, hi, _ in reserved],
        "encoder": {} if twelve else dict(ENCODER_PINS["ESP32"]),
        "extended": [ENCODER_PINS["ESP32"]["SW"],
                     ENCODER_PINS["ESP32"]["CLK"],
                     ENCODER_PINS["ESP32"]["DT"]],
        "layout": LAYOUT_12KEY if twelve else LAYOUT_ENCODER,
    }
    if twelve:
        rules["hint"] = (
            t("ui.pin_avail_esp32_12")
            % (
                _pin_span_text(_pin_free_spans(rules)),
                _reserved_pin_text(reserved),
                _extended_pin_text(prof),
            )
        )
    else:
        rules["hint"] = t("ui.pin_avail_esp32") % (
            _pin_span_text(_pin_free_spans(rules)),
            _reserved_pin_text(reserved),
            _pin_span_text([[p, p] for p in rules["encoder"].values()], "/"),
        )
    return rules


# --- WS-542: deep-sleep wake inputs ---------------------------------------
# A key can only wake the deck from deep sleep when its GPIO stays powered by
# the RTC domain while the rest of the chip is off:
#   classic ESP32: 0 / 2 / 4 / 12-15 / 25-27 / 32-39
#   ESP32-C3:      0-5
# Mirrors isWakeCapablePin() in src/main.cpp. The firmware derives its wake mask
# from the same rule, so the hint the UI shows and the mask the deck really
# arms cannot drift apart. This is descriptive only: validate_pins() stays the
# single authority on whether a pin may be assigned to a button at all.
WAKE_PINS = {
    "ESP32": frozenset([0, 2, 4] + list(range(12, 16)) + list(range(25, 28))
                       + list(range(32, 40))),
    "C3": frozenset(range(0, 6)),
}
# First firmware that derives the wake mask from the live layout (WS-542).
# Older decks only trusted the encoder push because of the ext0 call, so the
# host must not promise them a wake-line it cannot keep.
WAKE_MIN_VERSION = "1.3.9"


def _sleep_layout(layout) -> int:
    """Layout as an int (1 = encoder, 0 = 12-key) from any caller's shape.

    Accepts the numeric form used by the protocol, the names the UI/tests use
    ("encoder" / "12key") and a device cfg dict (``{"layout": 0}``).
    """
    if isinstance(layout, dict):
        return layout_of(layout)
    if isinstance(layout, str):
        text = layout.strip().lower()
        if text in ("0", "12", "12key", "12-key", "twelve"):
            return LAYOUT_12KEY
        return LAYOUT_ENCODER
    try:
        return LAYOUT_12KEY if int(layout) == LAYOUT_12KEY else LAYOUT_ENCODER
    except (TypeError, ValueError):
        return LAYOUT_ENCODER


def _usable_pin(prof: str, pin: int) -> bool:
    """Same pin exclusions as the firmware's isValidButtonPin().

    A pin rejected here can never be a button, so it is not "non-RTC" - it is
    simply out of range for this chip.
    """
    if prof == "C3":
        return 0 <= pin <= 21 and not (11 <= pin <= 17)
    return 0 <= pin <= 39 and not (6 <= pin <= 11) and pin < 34


def wake_inputs(chip: str, layout, pins) -> dict:
    """Which inputs of this deck can wake it from deep sleep (WS-542).

    ``chip`` is the firmware's FW_CHIP ("ESP32" / "C3"), ``layout`` the layout
    the UI currently shows (see :func:`_sleep_layout`) and ``pins`` the nine
    editable button pins exactly as the form holds them - ints, numeric
    strings, ``None`` or garbage. A bad value is reported, never raised: the
    hint has to keep rendering while the user is still typing. Inputs 9..11
    are the fixed EC11 lines and need no row in ``pins``.

    Returns ``{"wake": [input index...], "no_wake": [{"index", "pin",
    "reason"}]}``. Indices are 0-based, the same numbering as
    ``BTN:<dev>:<index>``.

    ``reason`` is one of:

    * ``"range"`` - a value this chip could not use as a button at all;
    * ``"non-rtc"`` - a usable pin the RTC domain cannot watch, so it cannot
      wake the chip from deep sleep;
    * ``"ext1-all-low"`` - a usable RTC pin that is *not* the designated
      single-key input. Classic ESP32's ext1 has no ANY_LOW mode: it wakes
      only when every selected pin is low at once, so these keys wake the deck
      only when held down together. The firmware arms ext0 on the first
      wake-capable input (input order), which is what ``wake`` reports here.

    The C3 has no such limit: ``esp_deep_sleep_enable_gpio_wakeup`` fires on
    any single GPIO0-5 at low level, so every RTC pin in ``wake`` wakes alone.
    """
    prof = pin_chip(chip)
    twelve = _sleep_layout(layout) == LAYOUT_12KEY
    values = list(pins) if isinstance(pins, (list, tuple)) else []
    enc = ENCODER_PINS[prof]
    count = INPUT_COUNT_12KEY if twelve else INPUT_COUNT_ENCODER

    rows = []   # (index, pin, parsed_ok)
    for idx in range(count):
        if idx < PIN_COUNT:
            ok, pin = _pin_int(values[idx]) if idx < len(values) else (False, 0)
        elif idx == 9:
            ok, pin = True, enc["SW"]
        else:
            ok, pin = True, enc["CLK"] if idx == 10 else enc["DT"]
        rows.append((idx, pin, ok))

    # Classic ESP32: only the first wake-capable input is armed with ext0 and
    # therefore wakes on its own (mirrors firstWakeInputPin() in main.cpp).
    single_key = None
    if prof != "C3":
        for idx, pin, ok in rows:
            if ok and _usable_pin(prof, pin) and pin in WAKE_PINS[prof]:
                single_key = idx
                break

    wake, no_wake = [], []
    for idx, pin, ok in rows:
        if not ok or not _usable_pin(prof, pin):
            no_wake.append({"index": idx, "pin": pin if ok else None,
                            "reason": "range"})
        elif pin not in WAKE_PINS[prof]:
            no_wake.append({"index": idx, "pin": pin, "reason": "non-rtc"})
        elif prof != "C3" and idx != single_key:
            no_wake.append({"index": idx, "pin": pin,
                            "reason": "ext1-all-low"})
        else:
            wake.append(idx)
    return {"wake": wake, "no_wake": no_wake}


def _pin_int(value):
    """Strictly parse one candidate pin. Returns ``(ok, pin)``.

    Unlike ``int()`` this never accepts ``None``/``""``/``"abc"``/``6.5``;
    booleans are rejected too, because ``True`` would otherwise pass as GPIO1.
    """
    if isinstance(value, bool):
        return False, 0
    if isinstance(value, int):
        return True, value
    if isinstance(value, float):
        return (True, int(value)) if value.is_integer() else (False, 0)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False, 0
        try:
            return True, int(text, 10)
        except ValueError:
            return False, 0
    return False, 0


def _pin_reject_reason(prof: str, btn: str, pin: int,
                       layout: int = LAYOUT_ENCODER):
    """Why ``pin`` cannot be a button pin on ``prof``. ``("", "")`` = accepted.

    Returns ``(code, message)`` so the caller can tag the row without parsing
    the text back apart. In the 12-key layout (WS-499) the EC11 SW/A/B pins
    are inputs 9..11 rather than an encoder, so using them again is a
    duplicate — the firmware words it the same way.
    """
    who = t("error.pin_gpio_on_chip", btn=btn, pin=pin)
    if prof == "C3":
        if pin > 21:
            return "out_of_range", who + t("error.pin_c3_missing")
        if 11 <= pin <= 17:
            return "reserved", who + t("error.pin_c3_flash")
    else:
        if 6 <= pin <= 11:
            return "reserved", who + t("error.pin_spi_flash")
        if pin >= 34:
            return "reserved", who + t("error.pin_input_only")
    if layout_of({"layout": layout}) == LAYOUT_12KEY:
        for idx, role in ((9, "SW"), (10, "CLK"), (11, "DT")):
            if pin == ENCODER_PINS[prof][role]:
                return "extended", who + (t("error.pin_twelve_key")
                                          % (idx + 1))
        return "", ""
    for role, encoder_pin in ENCODER_PINS[prof].items():
        if pin == encoder_pin:
            return "encoder", t("error.pin_conflict_encoder") % (who, t(_ENCODER_ROLE_KEYS[role]))
    return "", ""


def validate_pins(pins, chip: str = "", layout: int = LAYOUT_ENCODER) -> list:
    """Return one ``{index, code, message}`` per offending button row.

    ``[]`` means the pin set is acceptable — the same set the firmware would
    accept, so ``CFG:PINS`` is only sent when the deck can actually take it.
    Every message starts with 「按键 N」so the device page can show it under
    that row instead of framing all nine inputs (the old behaviour).

    Rules (src/main.cpp:228-285): non-empty, integer 0-39, unique, and on top
    of that the per-chip reserved ranges plus the encoder pins — or, in the
    12-key layout (WS-499), those three pins as inputs 9..11.
    """
    prof = pin_chip(chip)
    values = list(pins) if isinstance(pins, (list, tuple)) else []
    errors = []
    accepted = {}   # pin -> [row indices], used for the duplicate report

    for i in range(PIN_COUNT):
        btn = t("ui.input_button_n") % (i + 1)
        raw = values[i] if i < len(values) else None
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            errors.append({"index": i, "code": "empty", "message": btn + t("error.pin_empty")})
            continue
        ok, pin = _pin_int(raw)
        if not ok:
            errors.append({"index": i, "code": "not_integer",
                           "message": btn + t("error.pin_not_int")})
            continue
        if pin < 0 or pin > 39:
            errors.append({"index": i, "code": "out_of_range",
                           "message": btn + t("error.pin_fill_int")})
            continue
        reason_code, reason = _pin_reject_reason(prof, btn, pin, layout)
        if reason:
            errors.append({"index": i, "code": reason_code, "message": reason})
            continue
        accepted.setdefault(pin, []).append(i)

    # Duplicates: every row that shares a pin is flagged, so fixing either one
    # clears both at once.
    for pin, rows in accepted.items():
        if len(rows) < 2:
            continue
        names = [t("ui.input_button_n") % (i + 1) for i in rows]
        text = (t("error.pin_text_join") % (names[0], names[1]) if len(names) == 2
                else "、".join(names))
        for i in rows:
            errors.append({"index": i, "code": "duplicate",
                           "message": t("error.pin_both_use_gpio") % (text, pin)})

    errors.sort(key=lambda e: e["index"])
    return errors


def parse_pins_query(raw: str) -> list:
    """Turn ``?pins=0,1,,3`` into values for ``validate_pins``.

    An empty token stays ``None`` (「不能为空」); anything non-numeric is kept
    verbatim so the error can still name the row. Missing trailing tokens are
    handled by ``validate_pins`` itself.
    """
    out = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            out.append(None)
            continue
        try:
            out.append(int(token, 10))
        except ValueError:
            out.append(token)
    return out


KEY_CODES = [
    ("F13", 0xF0), ("F14", 0xF1), ("F15", 0xF2), ("F16", 0xF3),
    ("F17", 0xF4), ("F18", 0xF5), ("F19", 0xF6), ("F20", 0xF7),
    ("F21", 0xF8), ("F22", 0xF9), ("F23", 0xFA), ("F24", 0xFB),
    ("F1", 0xC2), ("F2", 0xC3), ("F3", 0xC4), ("F4", 0xC5),
    ("F5", 0xC6), ("F6", 0xC7), ("F7", 0xC8), ("F8", 0xC9),
    ("F9", 0xCA), ("F10", 0xCB), ("F11", 0xCC), ("F12", 0xCD),
    ("Esc", 0xB1), ("Tab", 0xB3), ("Enter", 0xB0), ("Space", 0xB4),
    ("Backspace", 0xB2), ("Insert", 0xD1), ("Delete", 0xD4),
    ("Home", 0xD2), ("End", 0xD5), ("PageUp", 0xD3), ("PageDown", 0xD6),
    ("Left", 0xD8), ("Right", 0xD7), ("Up", 0xD9), ("Down", 0xDA),
    ("LCtrl", 0x80), ("LShift", 0x81), ("LAlt", 0x82), ("LWin", 0x83),
    ("RCtrl", 0x84), ("RShift", 0x85), ("RAlt", 0x86), ("RWin", 0x87),
]
KEY_CODES += [(chr(c), c) for c in range(ord("A"), ord("Z") + 1)]
KEY_CODES += [(chr(c), c) for c in range(ord("0"), ord("9") + 1)]
# WS-508: sentinel code 0 = 「不发键」—— 这个键只跑上位机动作，设备不发 HID
# （固件 v1.3.8 起直接跳过；更早的固件会发一帧全 0 报告，OS 不产生字符）。
# 必须留在列表末尾：``keyOptions(selected)`` 找不到 selected 时退回第一项，
# 0 排第一会让每个未知键码都显示成「不发键」。
KEY_CODES += [(t("ui.key_none_hint"), 0)]

# --- macro key names ------------------------------------------------------
# ``key:`` macro steps are written with the names the UI shows in the HID key
# list, but the ``keyboard`` library spells several of them differently
# ("page up", "right ctrl", ...). Normalise before sending so ``key:PageUp``
# and ``key:RCtrl`` behave like the lowercase names users already write.
# Keys are lowercase looked-up names; values are ``keyboard`` names.
MACRO_KEY_NAMES = {
    "tab": "tab",
    "enter": "enter", "return": "enter",
    "esc": "esc", "escape": "esc",
    "space": "space",
    "backspace": "backspace",
    "insert": "insert", "ins": "insert",
    "delete": "delete", "del": "delete",
    "home": "home", "end": "end",
    "pageup": "page up", "pgup": "page up",
    "pagedown": "page down", "pgdn": "page down",
    "left": "left", "right": "right", "up": "up", "down": "down",
    "lctrl": "ctrl", "ctrl": "ctrl", "control": "ctrl",
    "rctrl": "right ctrl",
    "lshift": "shift", "shift": "shift",
    "rshift": "right shift",
    "lalt": "alt", "alt": "alt",
    "ralt": "right alt", "altgr": "right alt",
    "lwin": "win", "win": "win",
    "rwin": "right win",
}
# F1–F12 use the same lowercase name in the ``keyboard`` library. F13–F24 are
# deliberately absent: they are outside KEY_CODES and go through unchanged, as
# do single letters, digits and any name this table does not know.
MACRO_KEY_NAMES.update({f"f{i}": f"f{i}" for i in range(1, 13)})


def _macro_key_token(token: str) -> str:
    """Map one key name (no ``+`` separator) to its ``keyboard`` spelling."""
    key = token.strip()
    if not key:
        return key
    return MACRO_KEY_NAMES.get(key.lower(), key)


def macro_key_name(value: str) -> str:
    """Normalise a ``key:`` step before handing it to ``keyboard.send()``.

    Combos (``ctrl+alt+t``) are normalised token by token and joined back with
    ``+``; a bare ``+`` stays a ``+``. Anything unknown keeps its original
    spelling so existing macros are unaffected. A leading ``key:`` prefix is
    stripped first, which keeps macros saved by older host builds working.
    """
    raw = (value or "").strip()
    # 旧配置里可能残留 "key:" 前缀：早期前端把整行写进了 value，已保存的宏
    # 不会因为前端修复自动重写，剥掉前缀才能继续用。
    while raw.lower().startswith("key:"):
        raw = raw[4:].strip()
    if not raw:
        return raw
    parts = raw.split("+")
    if len(parts) == 1:
        return _macro_key_token(raw)
    return "+".join(_macro_key_token(p) for p in parts)


# --- apply readback -------------------------------------------------------
# Per-device apply operation state, isolating one apply from another. The BLE
# line callback runs on a different thread than the Flask route handler that
# drives the apply, so every cross-thread touch is guarded by a lock and ACK
# signals use threading.Event so the sender can block with a timeout instead
# of polling.
_apply_windows: dict = {}
_apply_lock = threading.RLock()

# OTA push state — one OtaWindow per device, mirroring the apply windows.
# The BLE line callback runs on a different thread than the Flask route
# handler that drives the push, so every cross-thread touch is guarded by a
# lock and ACK signals use threading.Event so the sender can block with a
# timeout instead of polling.
_ota_windows: dict = {}
_ota_lock = threading.RLock()

# Read-back capture (WS-409) — one window per device for an explicit
# "读取设备配置" request.  The device answers CMD:GET_CFG with CFG_* lines;
# they are collected here instead of being written into the stored config
# right away, so the request can wait for a *complete* answer and merge it
# in one go (an old or silent firmware must not half-update the config).
_read_windows: dict = {}
_read_lock = threading.RLock()

# WS-499: one pending CFG:LAYOUT probe per device. The probe tells "this deck
# speaks the layout command" apart from "old firmware that stays silent", which
# is what stops the host from pretending a 12-key apply succeeded.
_layout_windows: dict = {}
_layout_lock = threading.RLock()

# WS-546: one pending CFG:MODS probe per device. Same idea as the layout probe:
# a deck that answers ``MODS:`` supports per-input modifiers, one that stays
# silent does not. Capability is decided by this answer, never by a version
# number (1.3.9 is WS-542's, this feature lands later).
_mods_windows: dict = {}
_mods_lock = threading.RLock()

# Per-device record of the last explicit read-back (WS-409): when it happened
# and which config groups the device actually reported.  It lives outside the
# device config so a failed read leaves the stored config untouched.
_read_status: dict = {}

# Runtime health samples (WS-445). One record per device, kept in memory only:
# a sample is worth nothing after a host restart, and writing it into
# config.json would mean one disk write per page open / reconnect.
# Shape: {dev_id: {"ok": bool, "supported": bool|None, "at": float|None,
#                  "sample": {...}, "display": {...}, "flags": {...},
#                  "notes": {flag: 说明文字}}}
_sysinfo: dict = {}
# One pending SYS:* wait per device (same cross-thread pattern as _read_windows).
_sysinfo_windows: dict = {}
# Last uptime (seconds) seen per device, for millis() wrap detection (WS-449).
# Runtime-only, same lifetime as _sysinfo: a host restart just means the next
# sample is a "first sample" again.
_sysinfo_uptime: dict = {}
_sysinfo_lock = threading.RLock()

# How long CMD:SYSINFO may take before we call the firmware "unsupported".
SYSINFO_TIMEOUT = 2.0

# Free-heap warning threshold for the device card (WS-445). Deliberately
# conservative: a C3 running BLE + OTA dips well below this during a push, so
# anything under ~20 KB at rest means an OTA is likely to fail.
SYSINFO_HEAP_WARN = 20 * 1024

# The device-managed fields the UI shows.  A read-back only counts as
# complete when every one of them came back; the timezone (CFG_TZ) is
# captured as well but is not one of the configurable groups.
_CFG_READ_FIELDS = ["pins", "keys", "enc", "sleep"]

# How long a read-back waits for the device's answer (WS-409).
CFG_READ_TIMEOUT = 2.0

# OTA constants (must match src/main.cpp).
OTA_CHUNK_MIN = 200          # never go below this even on a small MTU
OTA_CHUNK_MAX = 512          # CFG RX characteristic capacity (firmware guard)
OTA_WINDOW_BYTES = 32 * 1024  # bytes allowed in flight before waiting for ACKs
OTA_CMD_TIMEOUT = 5.0        # s to wait for ACK:CMD:OTA (old firmware)
OTA_ACK_TIMEOUT = 15.0       # s without ACK progress during the push
OTA_END_TIMEOUT = 15.0       # s to wait for ACK:OTA:DONE
OTA_VERIFY_TIMEOUT = 90.0    # s to wait for the new firmware version after reboot

# Human-readable labels for the five configurable fields (matches the order
# the firmware applies them and the order the UI shows them).
FIELD_LABELS = {
    "pins": t("ui.group_pins"),
    "keys": t("ui.group_keys"),
    "enc": t("ui.group_enc"),
    "sleep": t("ui.group_sleep"),
    "tz": t("ui.group_tz"),
}

# Ordered (field, CFG prefix, label) tuples for ACK matching and readback
# comparison. The prefix is the part after "CFG:" — e.g. "PINS" for "CFG:PINS:".
_CFG_READBACK_FIELDS = [
    ("pins", "PINS", t("ui.group_pins")),
    ("keys", "KEYS", t("ui.group_keys")),
    ("enc", "ENC", t("ui.group_enc")),
    ("sleep", "SLEEP", t("ui.group_sleep")),
    ("tz", "TZ", t("ui.group_tz")),
]

# WS-499: the layout line is *optional*. A deck older than this feature never
# sends CFG_LAYOUT / LAYOUT, so requiring it would make every apply look
# half-failed on hardware that works fine. It is merged when it arrives and
# compared by apply_device() only when that device actually reported it.
#
# WS-546: CFG_MODS is optional for the same reason — firmware older than the
# modifier feature never reports it. Its presence is what marks a deck as
# modifier-capable (see probe_mods / _apply_cfg_field).
_CFG_OPTIONAL_FIELDS = [
    ("layout", "LAYOUT", t("ui.layout_label")),
    ("mods", "MODS", t("keys.mods_label")),
]
_ALL_CFG_FIELDS = _CFG_READBACK_FIELDS + _CFG_OPTIONAL_FIELDS


class ApplyWindow:
    """Per-device apply operation state.

    Owns the ACK / readback capture for one ``apply_device`` call. The BLE
    line callback runs on a different thread than the Flask route handler
    that drives the apply, so every cross-thread touch is guarded by a lock
    and ACK signals use ``threading.Event`` so the sender can block with a
    timeout instead of polling.
    """

    def __init__(self, dev_id: str, snapshot: dict):
        self.dev_id = dev_id
        self.snapshot = snapshot  # {field: value_string we tried to send}
        # Optional fields (layout) get an ACK slot too, but they never gate
        # completeness — an old deck simply never answers them.
        self._ack_ok = {f: False for f, _, _ in _ALL_CFG_FIELDS}
        self._ack_events = {f: threading.Event() for f, _, _ in _ALL_CFG_FIELDS}
        self.readback = {}        # {field: value_string from CMD:GET_CFG}
        self._rb_lock = threading.Lock()
        self._rb_count = 0
        self.phase = "sending"    # sending -> readback -> done
        self._done = threading.Event()

    def on_ack(self, field: str):
        if field in self._ack_events:
            self._ack_ok[field] = True
            self._ack_events[field].set()

    def on_readback(self, field: str, value: str):
        with self._rb_lock:
            self.readback[field] = value
            self._rb_count += 1
            # Completeness is decided by the *required* fields only; the
            # optional layout line must not keep the window waiting.
            complete = all(f in self.readback for f, _, _ in _CFG_READBACK_FIELDS)
        if complete:
            self._done.set()

    def wait_ack(self, field: str, timeout: float) -> bool:
        return self._ack_events[field].wait(timeout)

    def wait_readback(self, timeout: float) -> bool:
        return self._done.wait(timeout)


class ReadWindow:
    """Captures the CFG_* answer of one explicit "读取设备配置" request.

    The BLE line callback runs on a different thread than the Flask route
    handler that waits for the answer, so the collected values are guarded
    by a lock and "everything arrived" is signalled by an event.

    Only the four device-managed groups (pins / keys / enc / sleep) decide
    whether the answer is complete.  ``tz``, ``layout`` and ``mods`` are
    captured as well but stay optional — a deck older than those features
    never sends them, so they are merged when they arrive and never waited
    for (see _CFG_OPTIONAL_FIELDS).
    """

    def __init__(self, dev_id: str):
        self.dev_id = dev_id
        self.values: dict = {}      # field -> raw value string
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._tz = threading.Event()

    def on_value(self, field: str, value: str):
        with self._lock:
            self.values[field] = value
            complete = all(f in self.values for f in _CFG_READ_FIELDS)
        if complete:
            self._done.set()
        if field == "tz":
            self._tz.set()

    def wait(self, timeout: float) -> bool:
        """Block until every tracked field arrived (False on timeout)."""
        return self._done.wait(timeout)

    def wait_tz(self, timeout: float) -> bool:
        """Block briefly for the optional timezone line."""
        return self._tz.wait(timeout)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.values)


class SysinfoWindow:
    """Captures the single ``SYS:`` answer to one ``CMD:SYSINFO`` request.

    The BLE line callback runs on a different thread than the Flask route
    handler that waits for it, so the payload is handed over under a lock and
    completion is signalled with an Event (no polling).
    """

    def __init__(self, dev_id: str):
        self.dev_id = dev_id
        self.payload = None
        self._lock = threading.Lock()
        self._done = threading.Event()

    def on_payload(self, payload: str):
        with self._lock:
            # Keep the first answer: a second line (duplicate command) must
            # not overwrite the sample this request is already returning.
            if self.payload is None:
                self.payload = payload
        self._done.set()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    def result(self):
        with self._lock:
            return self.payload


class OtaWindow:
    """Per-device OTA push state, owning the ACK capture for one push.

    The push is pipelined: the writer thread keeps ``sent`` ahead of the
    device's confirmed ``acked`` offset and only blocks when the amount in
    flight reaches ``OTA_WINDOW_BYTES``. ``acked`` — not ``sent`` — drives the
    progress the user sees.

    The BLE line callback runs on a different thread than the Flask route
    handler that drives the push, so every cross-thread touch is guarded by a
    condition variable.
    """

    def __init__(self, dev_id: str, filename: str, total: int):
        self.dev_id = dev_id
        self.filename = filename
        self.total = total
        self.sent = 0              # enqueued to the BLE link
        self.acked = 0             # confirmed by the device
        self.status = "starting"   # starting -> uploading -> done -> error
        # WS-523: 细分的推送阶段，供「固件在线更新」的五段进度展示使用
        # （下载 → 校验 → 推送 → 设备重启 → 确认版本）。status 语义不变。
        self.phase = "push"        # push -> restart -> confirm
        self.error = ""
        self._ack_line = ""
        self._cond = threading.Condition()

    def on_ack(self, line: str):
        with self._cond:
            self._ack_line = line
            # "ACK:OTA:<n>" advances the confirmed offset. DONE and ERROR both
            # start with this prefix but fail the int() parse, so they only
            # update _ack_line.
            if line.startswith("ACK:OTA:"):
                try:
                    n = int(line[len("ACK:OTA:"):])
                except ValueError:
                    n = None
                if n is not None and n > self.acked:
                    self.acked = n
            self._cond.notify_all()

    def wait_change(self, timeout: float):
        """Block until an ACK arrives or *timeout* elapses."""
        with self._cond:
            self._cond.wait(timeout)

    @property
    def ack_line(self) -> str:
        with self._cond:
            return self._ack_line

    @property
    def ack_error(self) -> str:
        """The ACK:OTA:ERROR:<reason> line, or "" when none arrived."""
        with self._cond:
            line = self._ack_line
        return line if line.startswith("ACK:OTA:ERROR") else ""

    @property
    def progress(self) -> int:
        return self.acked

    def percent(self) -> int:
        return (100 * self.acked // self.total) if self.total else 0


def _apply_cfg_field(cfg: dict, field: str, raw: str) -> bool:
    """Apply one device-reported ``CFG_*`` value to *cfg*.

    *raw* is the part after the ``CFG_<FIELD>:`` prefix.  Only the fields
    the device owns are touched — the host-side extras (per-key actions,
    rotation actions, the device name) stay as they are.  Returns True
    when the value was understood and applied.
    """
    if field == "keys":
        vals = [v for v in raw.split(",") if v.strip()]
        try:
            codes = [int(v.strip()) for v in vals]
        except ValueError:
            return False
        keys = cfg.setdefault("keys", [])
        # WS-499: a 12-key deck reports 12 codes; grow the stored list instead
        # of dropping the last two (a 10-code answer still fills 0..9 only).
        for i in range(len(keys), min(len(codes), INPUT_COUNT_12KEY)):
            keys.append(default_key_item(i))
        for i, code in enumerate(codes[:INPUT_COUNT_12KEY]):
            keys[i]["code"] = code
            keys[i]["label"] = next((lbl for lbl, c in KEY_CODES if c == code), str(code))
        return True
    if field == "mods":
        # WS-546: ``CFG_MODS:`` / ``MODS:`` — one 0..15 mask per input.
        vals = [v for v in raw.split(",") if v.strip()]
        try:
            masks = [int(v.strip()) for v in vals]
        except ValueError:
            return False
        if (len(masks) > INPUT_COUNT_12KEY
                or any(m < 0 or m > MOD_MASK for m in masks)):
            return False
        keys = cfg.setdefault("keys", [])
        for i in range(len(keys), min(len(masks), INPUT_COUNT_12KEY)):
            keys.append(default_key_item(i))
        for i, mask in enumerate(masks[:INPUT_COUNT_12KEY]):
            keys[i]["mods"] = mask
        # The device just told us it speaks modifiers — that is a capability
        # fact (never inferred from a version number).
        cfg["mods_supported"] = True
        return True
    if field == "pins":
        vals = [v for v in raw.split(",") if v.strip()]
        try:
            pins = [int(v.strip()) for v in vals]
        except ValueError:
            return False
        if len(pins) != 9:
            return False
        cfg["pins"] = pins
        return True
    if field == "enc":
        try:
            mode = int(raw.strip())
        except ValueError:
            return False
        cfg.setdefault("encoder", {})["mode"] = mode
        return True
    if field == "layout":
        # WS-499: the device answered CFG:LAYOUT, so it speaks the command.
        try:
            value = int(raw.strip())
        except ValueError:
            return False
        cfg["layout"] = LAYOUT_12KEY if value == LAYOUT_12KEY else LAYOUT_ENCODER
        cfg["layout_supported"] = True
        return True
    if field == "sleep":
        payload = raw.split(",")
        try:
            enabled = payload[0].strip() == "1"
            mins = int(payload[1]) if len(payload) > 1 else 5
        except (ValueError, IndexError):
            return False
        cfg["sleep"] = {"enabled": enabled, "minutes": mins}
        return True
    if field == "tz":
        cfg["tz"] = raw
        return True
    return False


def _split_cfg_line(line: str):
    """Split a ``CFG_<FIELD>:<value>`` line into (field, value).

    Every group in ``_ALL_CFG_FIELDS`` is recognized.  Only the four required
    ones (pins / keys / enc / sleep) decide whether the answer is complete;
    ``tz`` / ``layout`` / ``mods`` are captured when they arrive and never
    waited for (old firmware does not send them).
    """
    for field, prefix, _ in _ALL_CFG_FIELDS:
        if line.startswith("CFG_" + prefix + ":"):
            return field, line[len("CFG_") + len(prefix) + 1:]
    return None, ""


def _capture_read_window(dev_id: str, field: str, raw: str) -> bool:
    """Hand one device-reported field to a pending read-back request.

    Returns True when a read-back window was waiting for *dev_id* — the
    caller then knows the line belongs to that request and must not write
    it into the stored configuration on its own.
    """
    with _read_lock:
        read_window = _read_windows.get(dev_id)
    if read_window is None:
        return False
    read_window.on_value(field, raw)
    # Log the raw line so the user can cross-check the values that land in
    # the form against what the device actually reported.
    add_event("cfg", t("log.device_cfg_prefix") + field.upper() + ":" + raw, dev_id)
    return True


def _rejection_reason(field: str, expected: str, actual: str) -> str:
    """Human-readable reason why the device rejected a config value.

    The firmware only genuinely rejects ``CFG:PINS`` (invalid / duplicate pins
    and encoder pins); the other CFG commands always accept (they clamp or
    ignore malformed input). The reason is derived from what we *tried* to
    send (``expected``), not what the device currently holds (``actual``).
    """
    if field == "pins":
        try:
            pins = [int(x) for x in expected.split(",") if x.strip()]
        except ValueError:
            return t("error.cfg_rejected")
        if len(pins) != len(set(pins)):
            return t("error.cfg_dup_pins")
        return t("error.cfg_reserved_pins")
    return t("error.cfg_rejected")


# --- persistence ----------------------------------------------------------
def load_config():
    global _config
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _config = {
                "devices": data.get("devices", {}) or {},
                "settings": {**_config["settings"], **(data.get("settings", {}) or {})},
                # WS-523: 在线更新状态在 config.json 顶层，读回来时保留。
                "skipped_version": str(data.get("skipped_version", "") or ""),
                "last_update": data.get("last_update", {}) or {},
                "update_fail_count": int(data.get("update_fail_count", 0) or 0),
                # WS-557: 更新通道（stable / rolling）；旧 config 没有这个键
                # → stable，不因为升级而改变用户已有的更新来源。
                "update_channel": online_update.normalize_channel(
                    data.get("update_channel", online_update.DEFAULT_UPDATE_CHANNEL)),
                # WS-529: 语言偏好同样在顶层；旧 config 没有这个键时用
                # system（跟随系统），不因为缺键而改动已有设置。
                "lang": str(data.get("lang", SYSTEM_PREF) or SYSTEM_PREF),
            }
            # WS-503: configs written before the 12-key layout carry 10 key
            # entries; repair them on load so an upgraded deck cannot get
            # stuck on "keys: rejected" (see ensure_key_items).
            for cfg in _config["devices"].values():
                if isinstance(cfg, dict):
                    ensure_key_items(cfg)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print("[app] config load failed:", e)
    # WS-529: 老 config.json 没有 lang，读完后统一补上默认值，
    # 这样调用方拿到的 _config 一定带这个顶层键。
    _config.setdefault("lang", SYSTEM_PREF)
    # WS-557: 同理，通道也一定存在（缺省 stable）。
    _config["update_channel"] = online_update.normalize_channel(
        _config.get("update_channel", online_update.DEFAULT_UPDATE_CHANNEL))


def save_config():
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:  # noqa: BLE001
        print("[app] config save failed:", e)


def new_device_cfg(chip: str = "ESP32", name: str = "") -> dict:
    """A fresh default configuration for one deck (ESP32 or C3 pin map)."""
    return {
        "name": name,
        "chip": chip,
        # WS-512: these are *host defaults*, not what the deck currently holds
        # — a new archive has never read the device, so the UI must say so
        # before anything is applied on top of the real configuration.
        "trusted": False,
        # WS-499: a new deck is an encoder deck (10 inputs) until it says
        # otherwise, so the shipped card count does not change.
        "layout": LAYOUT_ENCODER,
        "pins": list(DEFAULT_PINS.get(chip, DEFAULT_PINS["ESP32"])),
        # 12 entries are kept so a layout switch to 12 keys has codes ready;
        # input_count() decides how many the UI shows.
        "keys": [default_key_item(i) for i in range(INPUT_COUNT_12KEY)],
        # WS-389: rotation actions are host-side (never sent to the device), so they
# live next to the device-side encoder mode. Default = 未配置 (仅 HID).
# Keys are the raw ENC: direction ("CW"|"CCW"), not lowercase.
"encoder": {"mode": 0, "actions": {
    "CW": {"type": "none", "value": "", "macro": []},
    "CCW": {"type": "none", "value": "", "macro": []}}},
        "sleep": {"enabled": True, "minutes": 5},
        "tz": "CET-1CEST,M3.5.0,M10.5.0/3",
    }


def device_cfg(dev_id: str, chip: str = "", name: str = "") -> dict:
    """Return (creating if necessary) the per-device configuration."""
    with _lock:
        cfg = _config["devices"].get(dev_id)
        if cfg is None:
            cfg = new_device_cfg(chip or "ESP32", name or dev_id)
            _config["devices"][dev_id] = cfg
            save_config()
        else:
            if chip and cfg.get("chip") != chip:
                cfg["chip"] = chip
            if name:
                cfg["name"] = name
            # WS-512: an archive written before this field existed was filled
            # by an explicit read / apply, so it is trusted.  ``setdefault``
            # only fills the in-memory copy — a legacy config.json must not be
            # rewritten just because someone opened the page.
            cfg.setdefault("trusted", True)
        # WS-503: historical devices only have 10 key entries (v1.3.6). Pad on
        # every access so apply/read-back/UI all see the same 12-entry list.
        ensure_key_items(cfg)
        return cfg


def _err(message, **extra) -> dict:
    """JSON error body that keeps both rendered text and the message key.

    ``error`` stays the rendered sentence (old consumers and the pytest suite
    read it); ``code`` + ``params`` let the web UI re-translate the same error
    after a language switch (WS-529).
    """
    body = {
        "error": str(message),
        "code": getattr(message, "code", ""),
        "params": getattr(message, "params", {}),
    }
    body.update(extra)
    return body


# --- event log ------------------------------------------------------------
def add_event(kind: str, text: str, dev_id: str = "", level: str = "info"):
    global _event_seq
    with _lock:
        _event_seq += 1
        item = {
            "seq": _event_seq,
            "ts": time.time(),
            "time": time.strftime("%H:%M:%S"),
            "kind": kind,
            # ``text`` stays the rendered string (SSE consumers and the pytest
            # suite read it); ``code``/``params`` let the web UI re-translate
            # the same row after a language switch (WS-529).
            "text": str(text),
            "code": getattr(text, "code", ""),
            "params": getattr(text, "params", {}),
            "dev": dev_id,
            "level": level,
        }
        _events.append(item)
    return item


# --- BLE ------------------------------------------------------------------
def _on_ble_diag(dev_id: str, text: str, level: str):
    """Write a BLE diagnosis to the event log (WS-395).

    The BLE layer reports a connect failure / a changed reason / a recovery;
    the app turns that into one log line so the user can see *why* a deck is
    not reachable, instead of guessing from the console (which the packaged
    exe does not even have).
    """
    add_event("ble", text, dev_id, level)


def parse_bond_status(payload: str) -> dict:
    """Split a ``BOND_STATUS:`` payload into ``{"bound": bool, "addr": str}``.

    WS-423: only the field right after the state is the address. The old
    parser took *everything* after the first comma, so a firmware that
    comma-appended diagnostics turned ``addr`` into
    ``"AB12:**:**:**:**,peer_sec=1,nimble_bond=1"`` and the UI printed that
    as the bound host address. Extra fields are now kept out of ``addr``.
    """
    payload = payload.strip()
    if payload == "unbound":
        return {"bound": False, "addr": ""}
    state, _, rest = payload.partition(",")
    return {"bound": state == "bound", "addr": rest.split(",", 1)[0]}


def parse_bond_diag(payload: str) -> dict:
    """Parse ``BOND_DIAG:peer_sec=N,nimble_bond=0/1`` into an int dict.

    Returns only the keys it understood, so an unknown/new field never
    breaks the parse. ``{}`` means the line carried nothing usable.
    """
    known = ("peer_sec", "nimble_bond")
    out = {}
    for field in payload.split(","):
        key, sep, value = field.partition("=")
        key = key.strip()
        if not sep or key not in known:
            continue
        try:
            out[key] = int(value.strip())
        except ValueError:
            continue
    return out


# --- WS-445: SYS: health line ---------------------------------------------
# Wire format (firmware CMD:SYSINFO branch):
#   SYS:uptime=<s>,reset=<esp_reset_reason()>,nvs=<0|1>,heap=<bytes>,safe=<0|1>,boots=<n>
# Every field is parsed independently: a deck that omits one (or sends a
# garbage value) must still render the rest, with the bad field shown as
# 「未知」 rather than blank.
_SYSINFO_FIELDS = ("uptime", "reset", "nvs", "heap", "safe", "boots")

# esp_reset_reason_t values from ESP-IDF 5.5 (esp_system.h). Codes added by a
# newer IDF fall through to 「未知(N)」 so they still reach the user verbatim.
RESET_REASON_TEXT = {
    0: t("log.reset_unknown"),
    1: t("log.reset_poweron"),
    2: t("log.reset_ext"),
    3: t("log.reset_sw"),
    4: t("log.reset_panic"),
    5: t("log.reset_int_wdt"),
    6: t("log.reset_task_wdt"),
    7: t("log.reset_wdt"),
    8: t("log.reset_deep_sleep"),
    9: t("log.reset_brownout"),
    10: t("log.reset_sdio"),
    11: t("log.reset_usb"),
    12: t("log.reset_jtag"),
    13: t("log.reset_efuse"),
    14: t("log.reset_glitch"),
    15: t("log.reset_cpu_lockup"),
}


def _sysinfo_int(field: str, value: str):
    """Parse one ``SYS:`` value, or None when it is missing / out of range."""
    try:
        n = int(value.strip())
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    # nvs / safe are flags: anything but 0/1 is a broken line, not a state.
    if field in ("nvs", "safe") and n not in (0, 1):
        return None
    return n


def parse_sysinfo(payload: str) -> dict:
    """Turn the part after ``SYS:`` into ``{uptime, reset, nvs, heap, safe, boots}``.

    Missing or non-numeric fields are None instead of raising, so one bad
    value never costs the user the other five.  Unknown keys are ignored.
    """
    out = {k: None for k in _SYSINFO_FIELDS}
    for field in (payload or "").split(","):
        key, sep, value = field.partition("=")
        key = key.strip()
        if not sep or key not in out:
            continue
        out[key] = _sysinfo_int(key, value)
    return out


def reset_reason_text(code) -> str:
    """Chinese label for an ``esp_reset_reason()`` code, with the code in parens.

    Known codes read 「深度睡眠唤醒(8)」; anything this host does not know —
    including the code 0 (ESP_RST_UNKNOWN) — still shows the raw number as
    「未知(12)」 so a user can report it instead of seeing a blank cell.
    """
    if code is None:
        return t("log.reset_unknown")
    label = RESET_REASON_TEXT.get(code, t("log.reset_unknown"))
    return f"{label}({code})"


def uptime_text(seconds) -> str:
    """「3 小时 12 分」/「4 分 5 秒」/「42 秒」."""
    if seconds is None:
        return t("log.reset_unknown")
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return t("ui.uptime_hm", hours=hours, minutes=minutes)
    if minutes:
        return t("ui.uptime_ms", minutes=minutes, seconds=secs)
    return t("ui.uptime_s", seconds=secs)


def heap_text(free_bytes) -> str:
    """「96 KB」— one decimal below 100 KB so a tight heap stays visible."""
    if free_bytes is None:
        return t("log.reset_unknown")
    kb = max(0, int(free_bytes)) / 1024.0
    if kb >= 100 or abs(kb - round(kb)) < 0.05:
        return f"{kb:.0f} KB"
    return f"{kb:.1f} KB"


def nvs_text(nvs) -> str:
    """「可用」/「不可用：配置不会保存」."""
    if nvs == 1:
        return t("log.uptime_ok")
    if nvs == 0:
        return t("log.uptime_no_save")
    return t("log.reset_unknown")


# --- millis() wrap (WS-449) -----------------------------------------------
# ESP32 millis() is a 32-bit millisecond counter: it wraps at ~49.7 days, so
# SYS:uptime falls from ~4294967 s back to ~0 even though the deck never
# rebooted.  A card showing 「0 秒」 would read as "the device just restarted",
# which is exactly wrong.  We remember the last value per device and, when the
# new one is clearly below it, say "已运行" relative to this connection instead.


def uptime_wrap_text() -> str:
    """「本次连接内已运行」— looked up per call, so a language switch applies."""
    return t("log.uptime_this_session")


def uptime_wrap_note() -> str:
    """「设备计时已回绕」— looked up per call (see ``uptime_wrap_text``)."""
    return t("log.uptime_wrapped")


def uptime_wrapped(dev_id: str, seconds) -> bool:
    """Store *seconds* as *dev_id*'s latest uptime and report a millis() wrap.

    The rule is deliberately conservative — previous sample above one minute
    and the new one below half of it — so a normal reboot (previous 3600 s,
    new 5 s) is still a wrap report: after a reboot the counter really did
    restart, and "已运行 5 秒" was the misleading reading either way.

    Returns False for a first sample (no history), for a missing value, and
    whenever the counter moved normally, so existing displays are untouched.
    """
    if not dev_id or seconds is None:
        return False
    with _sysinfo_lock:
        prev = _sysinfo_uptime.get(dev_id)
        _sysinfo_uptime[dev_id] = int(seconds)
    return prev is not None and prev > 60 and int(seconds) < prev / 2.0


def sysinfo_view(payload: str, dev_id: str = "") -> dict:
    """Build the display record the device card renders from one ``SYS:`` line.

    All wording lives here (not in app.js) so the Chinese labels are covered
    by the same tests that cover the parser.  ``flags`` carries the raw
    booleans the UI needs for colouring; ``notes`` carries the one-line
    explanations the parent task asks for.

    *dev_id* enables millis() wrap detection; callers that only render one
    line (tests, one-shot previews) may omit it and get the plain formatting.
    """
    sample = parse_sysinfo(payload)
    wrapped = uptime_wrapped(dev_id, sample["uptime"])
    flags = {
        "nvs_bad": sample["nvs"] == 0,
        "heap_low": sample["heap"] is not None and sample["heap"] < SYSINFO_HEAP_WARN,
        "safe": sample["safe"] == 1,
        "deepsleep": sample["reset"] == 8,
    }
    notes = {}
    if flags["safe"]:
        notes["safe"] = t("log.device_safe_mode")
    if flags["deepsleep"]:
        notes["deepsleep"] = t("log.device_asleep")
    if flags["heap_low"]:
        notes["heap"] = t("log.device_low_memory")
    if wrapped:
        notes["uptime_wrap"] = uptime_wrap_note()
    return {
        "sample": sample,
        "display": {
            "reset": reset_reason_text(sample["reset"]),
            # app.js renders notes.safe/heap/deepsleep only, so the wrap hint
            # is also carried in the value itself; the plain 「5 秒」 is
            # suppressed because it reads as "just rebooted".
            "uptime": (t("log.uptime_wrapped_line",
                         text=uptime_wrap_text(), note=uptime_wrap_note())
                       if wrapped else uptime_text(sample["uptime"])),
            "nvs": nvs_text(sample["nvs"]),
            "heap": heap_text(sample["heap"]),
        },
        "flags": flags,
        "notes": notes,
    }



ble = BleLinkManager(on_line=lambda dev_id, line: _on_line(dev_id, line),
                     cache_path=os.path.join(DATA_DIR, "ble_cache.json"),
                     on_diag=_on_ble_diag)


def _on_line(dev_id: str, line: str):
    dev_id = dev_id or ""

    # Check for an active apply window on this device (cross-thread read).
    with _apply_lock:
        window = _apply_windows.get(dev_id)

    if line.startswith("BTN:"):
        parts = line.split(":")
        idx = 0
        if len(parts) >= 3:
            try:
                idx = int(parts[2])
            except ValueError:
                idx = -1
        with _lock:
            _last_press[dev_id] = (idx, time.time())
        with _lock:
            cfg_now = _config["devices"].get(dev_id)
        add_event("key", _input_label(cfg_now, idx), dev_id)
        _run_action_for(dev_id, idx)
        return
    if line.startswith("LAYOUT:"):
        # WS-499: answer to CFG:LAYOUT (and the layout probe). A deck that
        # sends this line supports the 12-key layout; one that stays silent
        # does not, and apply_device() refuses to pretend otherwise.
        raw = line[len("LAYOUT:"):].strip()
        try:
            value = int(raw)
        except ValueError:
            value = LAYOUT_ENCODER
        cfg = device_cfg(dev_id)
        with _lock:
            cfg["layout"] = LAYOUT_12KEY if value == LAYOUT_12KEY else LAYOUT_ENCODER
            cfg["layout_supported"] = True
            save_config()
        with _layout_lock:
            pending = _layout_windows.get(dev_id)
        if pending is not None:
            event, box = pending
            box["value"] = cfg["layout"]
            event.set()
        return
    if line.startswith("MODS:"):
        # WS-546: answer to the CFG:MODS capability probe. Only the probe
        # records it — the values are capability evidence, not a config value,
        # so an untrusted local draft is never overwritten from here.
        raw = line[len("MODS:"):].strip()
        with _mods_lock:
            pending = _mods_windows.get(dev_id)
        if pending is not None:
            event, box = pending
            box["csv"] = raw
            event.set()
        return
    if line.startswith("ENC:"):
        parts = line.split(":")
        direction = parts[-1] if parts else ""
        add_event("enc", t("ui.input_encoder_cw") if direction == "CW"
                  else t("ui.input_encoder_ccw"), dev_id)
        # WS-389: mode 4 ("上位机动作") may bind a host-side action to each
        # rotation direction. The device still sends F23/F24 regardless; the
        # host action is opt-in and only runs when configured.
        _run_encoder_action(dev_id, direction)
        return
    if line.startswith("VERSION:"):
        payload = line[len("VERSION:"):]
        fields = payload.split(",")
        with _lock:
            cfg = _config["devices"].get(dev_id)
        chip = fields[2] if len(fields) > 2 else ""
        cfg = device_cfg(dev_id, chip=chip)
        with _lock:
            new_version = fields[0] if fields else ""
            old_version = (cfg.get("fw") or {}).get("version", "")
            if new_version != old_version:
                # A different firmware may or may not speak CFG:LAYOUT; drop
                # the cached verdict (unknown) so the next 12-key apply probes
                # again instead of inheriting the old answer.
                cfg["layout_supported"] = None
                # WS-546: same for modifiers — an OTA can add or remove the
                # capability, so re-probe instead of trusting the old verdict.
                cfg["mods_supported"] = None
            cfg["fw"] = {"version": new_version,
                         "model": fields[1] if len(fields) > 1 else "",
                         "chip": chip,
                         "build": fields[3] if len(fields) > 3 else "",
                         # 5th field is flash size in KB (added in v1.3.1); older
                         # firmware omits it and we fall back to 0 (unknown).
                         "flash": int(fields[4]) if len(fields) > 4 and fields[4].strip().isdigit() else 0}
            save_config()
        add_event("device", t("log.firmware_version", version=cfg['fw']['version'], chip=chip), dev_id)
        return

    # Apply window: capture ACK during the sending phase so the caller can
    # confirm the device received each CFG:* line (1.5 s per item).
    # Firmware echoes ACK:CFG:PINS:... — strip "ACK:" then match "CFG:" + prefix + ":".
    if line.startswith("ACK:") and window and window.phase == "sending":
        body = line[4:]  # strip "ACK:" → body = "CFG:PINS:..."
        for field, prefix, _ in _ALL_CFG_FIELDS:
            if body.startswith("CFG:" + prefix + ":"):
                window.on_ack(field)
                break
        add_event("ack", t("log.device_confirm") + body[:60], dev_id, "ok")
        return

    # Apply window: capture CFG_* during the readback phase WITHOUT touching
    # the local config — the user must explicitly accept device values.
    # Firmware responds to CMD:GET_CFG with CFG_PINS:... (underscore).
    if window and window.phase == "readback":
        for field, prefix, _ in _ALL_CFG_FIELDS:
            if line.startswith("CFG_" + prefix + ":"):
                raw = line[len("CFG_") + len(prefix) + 1:]
                window.on_readback(field, raw)
                # WS-409: an explicit "读取设备配置" may be waiting for the very
                # same answer — let it see the line too.
                _capture_read_window(dev_id, field, raw)
                return
        # Any other line during the readback window is ignored on purpose:
        # no silent overwrite of the local config while we compare.
        return

    # WS-409: a pending "读取设备配置" request collects the CFG_* answer
    # itself.  The lines are NOT written into the stored configuration
    # here — the request decides whether the answer was complete enough to
    # accept (see collect_cfg_snapshot).
    field, raw = _split_cfg_line(line)
    if field is not None:
        if _capture_read_window(dev_id, field, raw):
            return

    if line.startswith("CFG_KEYS:"):
        cfg = device_cfg(dev_id)
        with _lock:
            _apply_cfg_field(cfg, "keys", line[len("CFG_KEYS:"):])
            save_config()
        add_event("cfg", t("log.keys_read"), dev_id)
        return
    if line.startswith("CFG_PINS:"):
        cfg = device_cfg(dev_id)
        with _lock:
            _apply_cfg_field(cfg, "pins", line[len("CFG_PINS:"):])
            save_config()
        return
    if line.startswith("CFG_ENC:"):
        cfg = device_cfg(dev_id)
        with _lock:
            _apply_cfg_field(cfg, "enc", line[len("CFG_ENC:"):])
            save_config()
        return
    if line.startswith("CFG_SLEEP:"):
        cfg = device_cfg(dev_id)
        with _lock:
            _apply_cfg_field(cfg, "sleep", line[len("CFG_SLEEP:"):])
            save_config()
        return
    if line.startswith("CFG_TZ:"):
        cfg = device_cfg(dev_id)
        with _lock:
            _apply_cfg_field(cfg, "tz", line[len("CFG_TZ:"):])
            save_config()
        return
    if line.startswith("CFG_MODS:"):
        # WS-546: CMD:GET_CFG tail line. Its mere presence marks the deck as
        # modifier-capable (see _apply_cfg_field).
        cfg = device_cfg(dev_id)
        with _lock:
            _apply_cfg_field(cfg, "mods", line[len("CFG_MODS:"):])
            save_config()
        return
    # OTA window: capture ACK:CMD:OTA / ACK:OTA:* / ACK:OTA:ERROR:* lines.
    # These arrive during a host-driven firmware push (CMD:OTA); the push
    # thread blocks on OtaWindow.wait_ack() so it needs the signal.
    ota = _ota_windows.get(dev_id) if dev_id else None
    if ota is not None and (line.startswith("ACK:CMD:OTA")
                            or line.startswith("ACK:OTA")):
        ota.on_ack(line)
        add_event("ota", line, dev_id, "ok")
        return
    if line.startswith("ACK:"):
        add_event("ack", t("log.device_confirm") + line[len("ACK:"):][:60], dev_id, "ok")
        return
    # WS-321: host binding status reported by CMD:GET_BOND. The address is
    # already masked by the firmware; we just store and display it.
    if line.startswith("BOND_STATUS:"):
        parsed = parse_bond_status(line[len("BOND_STATUS:"):])
        cfg = device_cfg(dev_id)
        with _lock:
            bond = dict(cfg.get("bond") or {})
            bond.update(parsed)
            if not parsed["bound"]:
                # An unbound deck has no live bond state to report.
                bond.pop("peer_sec", None)
                bond.pop("nimble_bond", None)
            cfg["bond"] = bond
            save_config()
        add_event("device",
                  t("log.host_binding") + (t("log.bound") if parsed["bound"] else t("log.unbound")),
                  dev_id)
        return
    # WS-423: the firmware reports the live NimBLE bond state on its own line
    # (BOND_STATUS itself stays `bound,<masked>`). Unknown lines are still
    # ignored, so an old host keeps working against a new deck.
    if line.startswith("BOND_DIAG:"):
        diag = parse_bond_diag(line[len("BOND_DIAG:"):])
        if diag:
            cfg = device_cfg(dev_id)
            with _lock:
                bond = dict(cfg.get("bond") or {})
                bond.update(diag)
                cfg["bond"] = bond
                save_config()
        return
    if line.startswith("PONG"):
        sent = _ping_sent.pop(dev_id, None)
        rtt = int((time.time() - sent) * 1000) if sent else 0
        add_event("pong", t("log.ping_response", ms=rtt), dev_id, "ok")
        return
    # WS-445: the answer to CMD:SYSINFO. Hand it to the request that is
    # waiting; an unsolicited line (a newer deck, a duplicate answer) is
    # ignored quietly instead of landing in the log as an unknown line.
    if line.startswith("SYS:"):
        with _sysinfo_lock:
            win = _sysinfo_windows.get(dev_id)
        if win is not None:
            win.on_payload(line[len("SYS:"):])
        return
    add_event("line", line, dev_id)


def device_list():
    with _lock:
        known = {d.get("dev_id", ""): d for d in ble.devices()}
    out = []
    for dev_id, info in known.items():
        cfg = device_cfg(dev_id, name=info.get("name", ""))
        connected = bool(info.get("connected"))
        # WS-321: ask a freshly connected deck for its host binding once, so the
        # device page shows the real state without the user pressing refresh.
        if connected and dev_id not in _bond_queried:
            _bond_queried.add(dev_id)
            send(dev_id, "CMD:GET_BOND")
        elif not connected:
            _bond_queried.discard(dev_id)
        out.append({
            "dev_id": dev_id,
            "name": cfg.get("name") or info.get("name") or dev_id,
            "address": info.get("address", ""),
            "connected": connected,
            # WS-395: why this deck is not reachable (None when there is no
            # known failure). Lets the UI say more than "not connected".
            "last_error": info.get("last_error"),
            "chip": cfg.get("chip", ""),
            "fw": cfg.get("fw", {}),
            "bond": cfg.get("bond", {"bound": False, "addr": ""}),
            # WS-409: the last explicit "读取设备配置" — when it happened and
            # which config groups the device actually reported (None when
            # nothing was read back yet).
            "read": _read_status.get(dev_id),
        })
    return out


def _scan_state() -> dict:
    """The last scan's conclusion for the UI (WS-395).

    ``ble.last_scan`` holds the raw counters; ``reason`` adds the one-line
    explanation the user actually reads ("the adapter is off" vs "we saw 3
    BLE devices but none was a BindDeckLite"). Before the first scan there
    is no conclusion yet (``at`` is None), so ``reason`` stays None.
    """
    state = dict(ble.last_scan)
    if state.get("at") is None:
        state["reason"] = None
    else:
        state["reason"] = scan_reason(state.get("seen", 0), state.get("found", 0),
                                      state.get("error"))
    return state


# --- command transport ----------------------------------------------------
def _udp_send(cmd: str) -> bool:
    global _udp_sock
    if not _config["settings"].get("udp_fallback", True):
        return False
    try:
        if _udp_sock is None:
            _udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        _udp_sock.sendto((cmd + "\n").encode(), ("255.255.255.255", 4210))
        return True
    except Exception:
        return False


def send(dev_id: str, cmd: str) -> dict:
    """Send one line over BLE first, then WiFi UDP (config mode) as fallback."""
    via = []
    if dev_id and ble.send(dev_id, cmd):
        via.append("ble")
    elif _udp_send(cmd):
        via.append("udp")
    if cmd.startswith(("CFG:", "CMD:")):
        add_event("tx", cmd[:90], dev_id, "ok" if via else "warn")
    return {"sent": bool(via), "via": via}


def probe_layout(dev_id: str, timeout: float = 1.5) -> dict:
    """Ask one deck for its layout.

    Returns ``{"sent", "answered", "layout"}``. ``sent`` False means the link
    was down (unknown, not "unsupported"); ``answered`` False with ``sent``
    True means old firmware that predates ``CFG:LAYOUT``. That difference is
    what lets the UI say 「固件不支持 12 键布局」 without ever claiming it about
    a deck that simply is not connected.
    """
    event = threading.Event()
    box = {"value": None}
    with _layout_lock:
        _layout_windows[dev_id] = (event, box)
    try:
        result = send(dev_id, "CFG:LAYOUT")
        sent = bool((result or {}).get("sent"))
        if sent:
            event.wait(timeout)
    finally:
        with _layout_lock:
            _layout_windows.pop(dev_id, None)
    return {"sent": sent,
            "answered": box["value"] is not None,
            "layout": box["value"]}


def probe_mods(dev_id: str, timeout: float = 1.5) -> dict:
    """Ask one deck whether it speaks ``CFG:MODS`` (WS-546).

    Returns ``{"sent", "answered"}``. ``answered`` means the deck replied with
    ``MODS:<csv>`` — i.e. it has per-input modifiers. ``sent`` False means the
    link was down, which is "unknown", NOT "unsupported". This is the only
    place capability is decided, so the UI can disable the modifier row for
    old firmware without hard-coding any version number.
    """
    event = threading.Event()
    box = {"csv": None}
    with _mods_lock:
        _mods_windows[dev_id] = (event, box)
    try:
        result = send(dev_id, "CFG:MODS")
        sent = bool((result or {}).get("sent"))
        if sent:
            event.wait(timeout)
    finally:
        with _mods_lock:
            _mods_windows.pop(dev_id, None)
    return {"sent": sent,
            "answered": box["csv"] is not None,
            "mods": box["csv"]}


def apply_device(dev_id: str) -> dict:
    cfg = device_cfg(dev_id)
    target_layout = layout_of(cfg)

    # WS-476: refuse an invalid pin set before anything touches the link. The
    # old code built the line first, so a cleared box (None) blew up on
    # int(None) and the user only saw "500 INTERNAL SERVER ERROR".
    pin_errors = validate_pins(cfg.get("pins"), cfg.get("chip", ""), target_layout)
    if pin_errors:
        add_event("cfg", t("log.not_sent_prefix") + pin_errors[0]["message"], dev_id, "warn")
        return {"ok": False, "invalid": True, "pin_errors": pin_errors,
                "error": pin_errors[0]["message"]}

    # WS-499: a 12-key config needs firmware that understands CFG:LAYOUT.
    # Say so instead of sending 12 key codes that the deck silently rejects.
    if target_layout == LAYOUT_12KEY and not cfg.get("layout_supported"):
        probe = probe_layout(dev_id)
        if probe["sent"] and not probe["answered"]:
            # Cache the negative verdict so every later apply does not pay
            # another 1.5 s probe; a firmware version change clears it.
            with _lock:
                cfg["layout_supported"] = False
                save_config()
            message = t("log.layout_unsupported")
            add_event("cfg", t("log.not_sent_prefix") + message, dev_id, "warn")
            return {"ok": False, "unsupported_layout": True, "error": message}

    count = input_count(cfg)
    # Build the values we are going to send — this snapshot is what the
    # readback is compared against, so a rejected field shows the user what
    # they tried to set vs what the device actually kept.
    pins = ",".join(str(int(p)) for p in cfg.get("pins", [])[:9])
    keys = ",".join(str(int(k["code"])) for k in cfg.get("keys", [])[:count])
    enc_val = str(int(cfg["encoder"].get("mode", 0)))
    sleep = cfg.get("sleep", {})
    sleep_val = "%d,%d" % (
        1 if sleep.get("enabled", True) else 0,
        max(1, int(sleep.get("minutes", 5))),
    )
    tz_val = cfg.get("tz", "")

    # WS-546: the modifier line is sent only to firmware that speaks it. A
    # negative verdict (probe timed out on this very deck) means the row is
    # disabled in the UI and nothing is sent — "checked but never applied"
    # must not be possible. An unknown verdict only sends when there is
    # something to send (a non-zero mask): on an old deck an all-zero table is
    # a no-op, and skipping it keeps the apply result free of a bogus
    # "mods: not reported" row.
    mods = ",".join(str(_mask_of(k)) for k in cfg.get("keys", [])[:count])
    mods_nonzero = any(_mask_of(k) for k in cfg.get("keys", [])[:count])
    send_mods = (cfg.get("mods_supported") is not False
                 and (cfg.get("mods_supported") is True or mods_nonzero))

    snapshot = {
        "pins": pins,
        "keys": keys,
        "enc": enc_val,
        "sleep": sleep_val,
        "tz": tz_val,
        "layout": str(target_layout),
    }
    if send_mods:
        snapshot["mods"] = mods

    # Create the per-device apply window before sending anything.
    window = ApplyWindow(dev_id, snapshot)
    with _apply_lock:
        _apply_windows[dev_id] = window

    try:
        results = []
        commands = [
            # Layout first: the key count the deck expects depends on it.
            ("layout", "CFG:LAYOUT:%d" % target_layout),
            ("pins", "CFG:PINS:" + pins),
            ("keys", "CFG:KEYS:" + keys),
        ]
        # WS-546: right after CFG:KEYS, so a rejected mask line cannot be
        # mistaken for a key-code failure. Skipped entirely on old firmware.
        if send_mods:
            commands.append(("mods", "CFG:MODS:" + mods))
        commands += [
            ("enc", "CFG:ENC:" + enc_val),
            ("sleep", "CFG:SLEEP:" + sleep_val),
        ]
        if tz_val:
            commands.append(("tz", "CFG:TZ:" + tz_val))

        any_sent = False
        for field, cmd in commands:
            r = send(dev_id, cmd)
            results.append(r)
            if r["sent"]:
                any_sent = True
                # Wait up to 1.5 s for the device's ACK for this field.
                window.wait_ack(field, 1.5)

        # Readback: ask the device for its current config and compare against
        # the snapshot we took before sending. The 2 s window covers the
        # device's round-trip for all five CFG_* lines.
        window.phase = "readback"
        send(dev_id, "CMD:GET_CFG")
        window.wait_readback(2.0)

        # Build per-item results. Status semantics:
        #   ok          — ACK received and readback matches what we sent
        #   rejected    — ACK received but readback differs (device refused)
        #   unconfirmed — no ACK within the per-item timeout
        #   unverified  — ACK received but the device did not report this
        #                  field (old firmware that ignores CMD:GET_CFG)
        items = []
        any_readback = bool(window.readback)
        for field, prefix, label in _CFG_READBACK_FIELDS:
            expected = snapshot.get(field, "")
            actual = window.readback.get(field, "")
            ack_ok = window._ack_ok.get(field, False)

            if not ack_ok:
                status = "unconfirmed"
                reason = t("log.not_connected") if not any_sent else t("log.connection_lost")
            elif not any_readback:
                # ACK came back but CMD:GET_CFG produced nothing at all —
                # old firmware that does not support readback.
                status = "unverified"
                reason = t("log.sent_no_readback")
            elif field not in window.readback:
                # Some fields came back but this one is missing.
                status = "unverified"
                reason = t("log.field_not_reported")
            elif actual == expected:
                status = "ok"
                reason = ""
            else:
                status = "rejected"
                reason = _rejection_reason(field, expected, actual)

            items.append({
                "field": field,
                "label": label,
                "status": status,
                "expected": expected,
                "actual": actual,
                "reason": reason,
            })

        # WS-499: the layout is compared only when the deck reported it back —
        # old firmware has no layout line and must not look half-failed.
        if "layout" in window.readback:
            actual_layout = window.readback["layout"].strip()
            ok_layout = actual_layout == snapshot["layout"]
            items.append({
                "field": "layout",
                "label": t("ui.layout_label"),
                "status": "ok" if ok_layout else "rejected",
                "expected": snapshot["layout"],
                "actual": actual_layout,
                "reason": "" if ok_layout else t("log.layout_rejected"),
            })
            if not ok_layout:
                add_event("cfg", t("log.layout_not_applied") + actual_layout, dev_id, "warn")

        # WS-546: the modifier masks are compared only when we actually sent
        # them. A deck that reports CFG_MODS is modifier-capable from now on;
        # a negative verdict is only ever set by the CFG:MODS probe, so a
        # single missed tail line can never disable the row by accident.
        if "mods" in snapshot:
            expected = snapshot["mods"]
            actual = window.readback.get("mods", "")
            ack_ok = window._ack_ok.get("mods", False)
            if not ack_ok:
                status = "unconfirmed"
                reason = t("log.not_connected") if not any_sent else t("log.connection_lost")
            elif not actual:
                status = "unverified"
                reason = t("log.field_not_reported")
            elif actual == expected:
                status = "ok"
                reason = ""
            else:
                status = "rejected"
                reason = _rejection_reason("mods", expected, actual)
            items.append({
                "field": "mods",
                "label": t("keys.mods_label"),
                "status": status,
                "expected": expected,
                "actual": actual,
                "reason": reason,
            })
            if actual and cfg.get("mods_supported") is not True:
                with _lock:
                    cfg["mods_supported"] = True
                    save_config()

        ok_all = all(i["status"] == "ok" for i in items)
        # WS-512: only an apply whose every field read back with the value we
        # sent makes the archive trustworthy.  A rejected / unconfirmed /
        # unverified field (old firmware cannot be verified at all) leaves the
        # flag exactly as it was.
        if ok_all:
            with _lock:
                cfg["trusted"] = True
                save_config()

        return {
            "ok": ok_all,
            "results": results,
            "items": items,
        }
    finally:
        with _apply_lock:
            _apply_windows.pop(dev_id, None)


def read_device_config(dev_id: str, timeout: float = CFG_READ_TIMEOUT) -> dict:
    """Ask the device for its config and wait for the answer.

    Sends ``CMD:GET_CFG`` and collects the ``CFG_*`` lines for at most
    *timeout* seconds.  A complete answer is merged into the stored config
    and returned; a partial or missing answer leaves the stored config
    untouched (an old or silent firmware must not half-update it).

    Returns ``{ok, cfg, fields, at, reason}`` — *fields* says which of the
    config groups the device reported, *cfg* is the merged configuration
    (only when *ok*).
    """
    window = ReadWindow(dev_id)
    with _read_lock:
        _read_windows[dev_id] = window
    try:
        deadline = time.monotonic() + timeout
        send(dev_id, "CMD:GET_CFG")
        window.wait(timeout)
        # The timezone is optional — the firmware sends it last. Give it a
        # short grace period, but never past the overall deadline.
        grace = min(0.2, max(0.0, deadline - time.monotonic()))
        if grace:
            window.wait_tz(grace)
        values = window.snapshot()
    finally:
        with _read_lock:
            if _read_windows.get(dev_id) is window:
                del _read_windows[dev_id]

    result = collect_cfg_snapshot(dev_id, values)
    with _lock:
        _read_status[dev_id] = {"at": result["at"], "fields": result["fields"],
                                 "ok": result["ok"]}
    return result


def read_sysinfo(dev_id: str, timeout: float = SYSINFO_TIMEOUT) -> dict:
    """Ask one deck for ``CMD:SYSINFO`` and return the rendered health record.

    Sent from the device page (open / reconnect / refresh).  A deck that does
    not answer within *timeout* is an old firmware: the caller renders
    「当前固件不支持运行状态（需更新固件）」.  That case is logged at info
    level only — a device page refresh must not fill the log with errors.

    The result is cached per device in ``_sysinfo`` (memory only, never
    config.json) so a disconnect can keep showing the last real sample.
    """
    if not dev_id:
        return {"ok": False, "supported": None, "at": None,
                "reason": "no_device"}

    window = SysinfoWindow(dev_id)
    with _sysinfo_lock:
        _sysinfo_windows[dev_id] = window
    try:
        sent = send(dev_id, "CMD:SYSINFO")
        if not sent.get("sent"):
            # Nothing reached the deck: keep the previous sample, the UI adds
            # 「已断开，非实时」. Not a firmware-support statement.
            return {"ok": False, "supported": None, "at": None,
                    "reason": "disconnected"}
        completed = window.wait(timeout)
        payload = window.result()
    finally:
        with _sysinfo_lock:
            if _sysinfo_windows.get(dev_id) is window:
                del _sysinfo_windows[dev_id]

    at = time.time()
    if not completed or payload is None:
        add_event("device", t("log.sysinfo_unanswered"),
                  dev_id)
        result = {"ok": False, "supported": False, "at": at,
                  "reason": "timeout"}
        with _sysinfo_lock:
            _sysinfo[dev_id] = result
        return result

    view = sysinfo_view(payload, dev_id)
    result = {"ok": True, "supported": True, "at": at, "reason": ""}
    result.update(view)
    with _sysinfo_lock:
        _sysinfo[dev_id] = result
    return result


def sysinfo_snapshot() -> dict:
    """Copy of the cached samples, for /api/status (additive field)."""
    with _sysinfo_lock:
        return {k: dict(v) for k, v in _sysinfo.items()}



def collect_cfg_snapshot(dev_id: str, values: dict, at: float = 0.0) -> dict:
    """Merge the values a device reported into its stored configuration.

    *values* maps a config group to the raw string the device sent
    ("240,241,..." for keys, "13,12,..." for pins, ...).  The merge is
    atomic: the stored config is only rewritten when **every** group came
    back *and* every value could be parsed, so a partial or broken answer
    never leaves a half-updated config behind.

    Returns ``{ok, cfg, fields, at, reason}``: *fields* says which groups
    the device reported, *cfg* is the merged configuration (only when *ok*).
    """
    fields = {f: (f in values) for f in _CFG_READ_FIELDS}
    got = sum(1 for v in fields.values() if v)
    total = len(_CFG_READ_FIELDS)
    if not at:
        at = time.time()
    if got < total:
        reason = t("log.cfg_not_reported") if got == 0 else t("log.cfg_incomplete")
        return {"ok": False, "cfg": None, "fields": fields,
                "at": at, "reason": reason}

    # Work on a copy first: a malformed value must not leave the stored
    # config half-updated either.  The whole read-modify-write happens under
    # the lock so a concurrent writer cannot slip in between.
    with _lock:
        cfg = device_cfg(dev_id)
        merged = copy.deepcopy(cfg)
        for field, _prefix, _label in _ALL_CFG_FIELDS:
            if field in values and not _apply_cfg_field(merged, field, values[field]):
                return {"ok": False, "cfg": None, "fields": fields, "at": at,
                        "reason": t("log.cfg_unparseable")}
        # WS-512: every tracked group came back and parsed, so the archive is
        # what the deck actually runs — the banner can go away.  A partial
        # answer returned above and keeps the untrusted flag untouched.
        if fields.get("pins") or fields.get("keys"):
            merged["trusted"] = True
        cfg.clear()
        cfg.update(merged)
        save_config()
    return {"ok": True, "cfg": cfg, "fields": fields, "at": at, "reason": ""}


# --- host-side actions ----------------------------------------------------
_keyboard = None
_keyboard_error = None


def _load_keyboard():
    global _keyboard, _keyboard_error
    if _keyboard is not None or _keyboard_error is not None:
        return _keyboard
    try:
        import keyboard  # type: ignore
        _keyboard = keyboard
    except Exception as e:  # noqa: BLE001
        _keyboard_error = f"{type(e).__name__}: {e}"
    return _keyboard


def _truncate(s, n: int = 80) -> str:
    """Truncate a string for log/toast display, keeping it readable."""
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "..."


# --- macro summary (WS-484) ------------------------------------------------
# A macro body may hold a password (``paste:`` / ``text:`` lines).  The event
# log is copyable row by row and is served verbatim by ``GET /api/log``, so
# the log must never carry the macro body — only a summary of its steps.
MACRO_SUMMARY_MAX = 120


def _redact_url_userinfo(url: str) -> str:
    """Replace the ``user:pass@`` part of a URL with ``***``.

    Only the userinfo section of the authority is touched: the host, the
    path and the query string stay exactly as they were.  A string that is
    not a ``scheme://`` URL, or that carries no userinfo, is returned
    unchanged.
    """
    text = str(url or "")
    marker = text.find("://")
    if marker <= 0:
        return text
    scheme = text[:marker]
    if not scheme[0].isalpha() or not scheme.isascii():
        return text
    rest = text[marker + 3:]
    # The authority ends at the first "/", "?" or "#" — anything after it
    # is path / query / fragment and must not be touched.
    end = len(rest)
    for ch in "/?#":
        pos = rest.find(ch)
        if pos != -1 and pos < end:
            end = pos
    authority = rest[:end]
    at = authority.rfind("@")
    if at < 0:
        return text
    # Only the userinfo part (before the "@") is replaced; the host stays.
    return text[:marker + 3] + "***@" + authority[at + 1:] + rest[end:]


def _macro_text_delay_ms(step):
    """The explicit ``text!<ms>:`` delay of a macro step, if it has one.

    ``None`` means "no explicit delay" (a plain ``text:`` step) — the
    summary then shows ``text(N 字符)`` without a delay.  ``0`` is a real
    value (``text!0:`` means "type as fast as possible").
    """
    raw = (step or {}).get("delay")
    if raw is None or raw == "":
        return None
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        return None
    if ms < 0:
        return None
    return int(ms)


# The UI stores a ``wait:`` step as type "delay" — the log names it the way
# the user wrote it.
MACRO_STEP_LABELS = {"delay": "wait"}


def _macro_step_text(step) -> str:
    """One macro step, rendered for the log without its body."""
    step = step if isinstance(step, dict) else {}
    stype = str(step.get("type") or "")
    value = step.get("value", "")
    if stype == "text":
        count = len(str(value or ""))
        delay = _macro_text_delay_ms(step)
        if delay is not None:
            return t("log.macro_step_text_delay", delay=delay, count=count)
        return t("log.macro_step_text", count=count)
    if stype == "paste":
        return t("log.macro_step_paste", count=len(str(value or "")))
    if stype == "delay":
        return f"wait:{_truncate(str(value or 0))}"
    if stype == "url":
        return f"url:{_truncate(_redact_url_userinfo(value))}"
    if not stype:
        return _truncate(value)
    return f"{stype}:{_truncate(value)}"


def macro_summary(action: dict) -> str:
    """Log-safe summary of a macro action.

    ``text:`` / ``paste:`` steps show their character count only — their
    body may be a credential and the log is copyable.  The result is a
    single line and never echoes ``action["value"]`` (the raw macro text
    the UI keeps for the edit box).
    """
    action = action or {}
    steps = action.get("macro")
    if not isinstance(steps, list) or not steps:
        return t("log.macro_zero_steps")
    total = len(steps)
    parts = [_macro_step_text(step) for step in steps]
    head = t("log.macro_head", total=total)
    summary = head + " → ".join(parts)
    if len(summary) > MACRO_SUMMARY_MAX:
        # Keep whole steps only — a half-printed step ("text(13 字") would
        # be misleading.  The tail carries the real step count.
        tail = t("log.macro_tail", total=total)
        budget = MACRO_SUMMARY_MAX - len(head) - len(tail)
        kept = []
        used = 0
        for part in parts:
            extra = len(part) + (3 if kept else 0)
            if kept and used + extra > budget:
                break
            kept.append(part)
            used += extra
        summary = head + " → ".join(kept)
        if len(summary) > MACRO_SUMMARY_MAX:
            summary = summary[:MACRO_SUMMARY_MAX].rstrip()
        summary += tail
    # A step value may contain newlines — the log row stays one line.
    return summary.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _action_display_target(action: dict) -> str:
    """The ``target`` shown in the log / toast for one action.

    Macros are summarised (never their body) and URL credentials are
    masked, so nothing secret ends up in a copyable log row.
    """
    action = action or {}
    kind = action.get("type", "none")
    value = action.get("value", "")
    if kind == "macro":
        return macro_summary(action)
    if kind == "url" or (kind == "launch"
                         and str(value).startswith(("http://", "https://"))):
        return _truncate(_redact_url_userinfo(value))
    return _truncate(value)


# --- macro browser selection ----------------------------------------------
# A ``browser:<name>`` macro step picks the browser that following ``url:``
# steps open with. Users write the canonical names; the aliases cover the
# different spellings ``webbrowser`` registers per platform (Linux has
# ``google-chrome`` / ``chromium-browser``, macOS ships just Safari).
#
# Windows needs a different route entirely: ``webbrowser.get("chrome")`` only
# works when Chrome is registered in the registry and otherwise either raises
# or resolves to the default handler (Edge), which is why ``browser:chrome``
# used to open Edge.  There we locate the real executable (registry ``App
# Paths`` first, then the standard install directories, including per-user
# installs under ``%LOCALAPPDATA%``) and start it directly with
# ``subprocess.Popen([exe, url])``.
MACRO_BROWSER_ALIASES = {
    "chrome": ("chrome", "google-chrome", "chromium", "chromium-browser"),
    "firefox": ("firefox", "mozilla"),
    "edge": ("edge", "msedge", "microsoft-edge"),
    "safari": ("safari",),
}

MACRO_BROWSER_LABELS = {
    "chrome": "Chrome",
    "firefox": "Firefox",
    "edge": "Edge",
    "safari": "Safari",
}

# Windows executable names looked up under ``App Paths`` in the registry.
WINDOWS_BROWSER_EXES = {
    "chrome": ("chrome.exe",),
    "edge": ("msedge.exe",),
    "firefox": ("firefox.exe",),
}

# Standard install locations, in the order they are probed.  Chrome's per-user
# install under %LOCALAPPDATA% is the common case that ``webbrowser`` misses.
WINDOWS_BROWSER_PATHS = {
    "chrome": (
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ),
    "edge": (
        ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
        ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
        ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe"),
    ),
    "firefox": (
        ("PROGRAMFILES", "Mozilla Firefox/firefox.exe"),
        ("PROGRAMFILES(X86)", "Mozilla Firefox/firefox.exe"),
        ("LOCALAPPDATA", "Mozilla Firefox/firefox.exe"),
    ),
}


def _windows_browser_exe(name: str) -> str:
    """Return the absolute exe path for ``name`` on Windows, or "" if missing.

    Checks the registry ``App Paths`` keys (HKLM first, then HKCU so per-user
    installs are found) and then the standard install directories.
    """
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg
    except ImportError:  # pragma: no cover - winreg exists on every Windows build
        winreg = None

    if winreg is not None:
        for exe in WINDOWS_BROWSER_EXES.get(name, ()):
            subkey = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
                      "\\" + exe)
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        path, _ = winreg.QueryValueEx(key, "")
                except OSError:
                    continue
                path = (path or "").strip().strip('"')
                if path and os.path.isfile(path):
                    return path

    for env, rel in WINDOWS_BROWSER_PATHS.get(name, ()):
        base = os.environ.get(env, "")
        if not base:
            continue
        path = os.path.join(base, rel)
        if os.path.isfile(path):
            return path
    return ""


def resolve_macro_browser(name: str):
    """Resolve a ``browser:`` step into ``(target, used, error)``.

    ``target`` is an executable path, a ``webbrowser`` controller, or ``None``
    for "system default browser"; ``used`` is the display name of the browser
    that will actually be used (``default`` when falling back); ``error`` is a
    human-readable reason when the requested browser is unavailable.  A failed
    resolution keeps ``target`` at ``None`` so the caller falls back to the
    default browser instead of aborting the macro.
    """
    key = (name or "").strip().lower()
    if not key or key == "default":
        return None, "default", ""
    label = MACRO_BROWSER_LABELS.get(key, (name or "").strip() or key)
    aliases = MACRO_BROWSER_ALIASES.get(key, (key,))

    if sys.platform.startswith("win"):
        if key == "safari":
            return None, "default", t("error.safari_windows")
        exe = _windows_browser_exe(key)
        if exe:
            return exe, label, ""
        # Never fall through to webbrowser.get() here: that is the call that
        # silently resolves to the default handler (Edge).
        return None, "default", t("error.browser_not_found", name=key)

    for alias in aliases:
        exe = shutil.which(alias)
        if exe:
            return exe, label, ""

    failures = []
    for alias in aliases:
        try:
            return webbrowser.get(alias), label, ""
        except webbrowser.Error as e:
            failures.append(f"{alias}: {e}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"{alias}: {type(e).__name__}: {e}")
    return None, "default", t("error.browser_not_found_detail", name=key, detail="; ".join(failures))


def _open_url_with(target, url: str) -> str:
    """Open ``url`` with ``target``, falling back to the default browser.

    ``target`` is an executable path, a ``webbrowser`` controller, or ``None``
    for the system default.  Returns an error string when the fallback was
    used, empty on success.  Executables are started directly (no shell), so
    the URL is passed as a plain argument and cannot be shell-interpreted.
    """
    if target is None:
        webbrowser.open(url)
        return ""
    try:
        if isinstance(target, str):
            subprocess.Popen([target, url])
        else:
            target.open(url)
        return ""
    except Exception as e:  # noqa: BLE001
        print(f"[app] macro browser failed ({type(e).__name__}: {e}), falling back to the default browser")
        webbrowser.open(url)
        return f"{type(e).__name__}: {e}"


def _open_target(value: str):
    if not value:
        return
    if value.startswith(("http://", "https://")):
        webbrowser.open(value)
        return
    if sys.platform.startswith("win"):
        os.startfile(value)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", value])
    else:
        subprocess.Popen(["xdg-open", value])


def _is_word_char(ch: str) -> bool:
    """True for characters that can appear inside a word (letters, digits, _)."""
    return ch.isalnum() or ch == "_"


def _split_app_tokens(value: str) -> list[str]:
    """Split a command line into tokens, honouring quotes anywhere.

    A double quote may open and close a quoted section **anywhere** inside
    a token, and only the *matching* quote character closes it — so inside
    ``"…"`` a single quote is literal and vice versa.  Text between the
    quotes is taken verbatim (spaces included) and the quote characters
    themselves are removed::

        '"C:\\\\Program Files\\\\App\\\\app.exe" --new'  ->  2 tokens
        'myapp.exe --title="My App"'                    ->  2 tokens

    A single quote opens a quoted section as well (POSIX-style paths such
    as ``'/usr/bin/foo'``), except when it is an **apostrophe** inside a
    word (``Bob's``) — that stays a literal character.

    Backslashes are **literal** (never escapes): Windows paths such as
    ``C:\\Program Files\\App\\app.exe`` must survive untouched.

    An unbalanced quote raises ``ValueError`` — the caller turns that into
    a skipped step, never into a silently mangled command line.
    """
    tokens: list[str] = []
    cur: list[str] = []
    quote = ""      # "" = outside a quoted section
    started = False  # current token exists (it may be empty, e.g. `""`)
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur.append(ch)
            continue
        if ch == '"':
            quote = ch
            started = True
            continue
        if ch == "'":
            # Apostrophe inside a word (Bob's) is literal, not a quote.
            prev = cur[-1] if cur else ""
            nxt = value[i + 1] if i + 1 < len(value) else ""
            if prev and nxt and _is_word_char(prev) and _is_word_char(nxt):
                cur.append(ch)
                started = True
                continue
            quote = ch
            started = True
            continue
        if ch.isspace():
            if started:
                tokens.append("".join(cur))
                cur = []
                started = False
            continue
        cur.append(ch)
        started = True
    if quote:
        raise ValueError("unbalanced quote")
    if started:
        tokens.append("".join(cur))
    return tokens


def _parse_app_cmd(value: str) -> tuple[str, list[str]]:
    """Split an ``app:<exe> [args]`` value into ``(exe, [args])``.

    This is the **single** parsing entry point for the ``app:`` macro step —
    ``run_action``, ``_open_app`` and the unit tests all call this function,
    never ``shlex.split`` directly.  ``shlex.split`` is not used here because
    it cannot express what users actually type: with ``posix=False`` a quote
    is only recognised at the **start** of a token, so ``--title="My App"``
    would be torn into ``--title="My`` + ``App"``.  ``_split_app_tokens``
    accepts quotes anywhere in a token and keeps backslashes literal.

    The args are returned as a **list**, never a joined string: joining
    with spaces and re-splitting downstream would lose the quote
    boundaries that the tokenizer worked so hard to preserve (a
    ``"hello world"`` argument would come back as two separate tokens).

    An unbalanced quote (``app:myapp.exe "oops``) makes the tokenizer raise
    ``ValueError``.  That must not abort the whole macro — a typo in one
    step would otherwise skip every following ``text:`` / ``key:`` step.
    The error is swallowed here and reported as ``("", [])``, so the step
    is skipped (the exe is never resolved, nothing is launched) while the
    rest of the macro keeps running.
    """
    if not value:
        return "", []
    try:
        parts = _split_app_tokens(value)
    except ValueError:
        # Unbalanced quote: the command line is not trustworthy, so do not
        # launch anything with a guessed argv — skip the step instead.
        return "", []
    if not parts:
        return "", []
    return parts[0], parts[1:]


def _app_cmd_error(value: str) -> str:
    """Explain why an ``app:`` value cannot be launched ("" when it is fine).

    Used by ``run_action`` to tell the user *which* mistake they made —
    "unbalanced quote" and "no executable given" need different wording.
    """
    if not value.strip():
        return t("error.no_executable")
    try:
        parts = _split_app_tokens(value)
    except ValueError:
        return t("error.bad_quotes")
    if not parts or not parts[0]:
        return t("error.no_executable")
    return ""


def _resolve_exe(value: str) -> str:
    """Resolve an executable path from a user-supplied **exe name**.

    The caller must pass the bare exe name (the first token from
    ``_parse_app_cmd``), never the full command line.  Tries in order:
    ``%VAR%`` / ``~`` expansion → absolute path check → relative path check →
    ``shutil.which`` (PATH lookup).  Returns the resolved path, or ``""``
    when nothing matches.  This lets users write ``app:notepad`` (PATH name)
    as well as ``app:"C:\\Program Files\\App\\app.exe"``
    and ``app:%LOCALAPPDATA%\\App\\app.exe``.
    """
    if not value:
        return ""
    # 支持 %LOCALAPPDATA% / ~ 环境变量写法
    v = os.path.expandvars(os.path.expanduser(value))
    if os.path.isabs(v) and os.path.exists(v):
        return v
    if os.path.exists(v):
        return v
    # 只写程序名时走 PATH 解析（shutil.which 返回完整路径或 ""）
    return shutil.which(v) or ""


# Launched desktop apps: keep a reference to every child so it is not
# garbage-collected while still running (which would leave a zombie).
_APP_PROCS: list = []


def _reap_app_procs() -> None:
    """Drop finished children from ``_APP_PROCS``.

    A ``Popen`` object that is garbage-collected while its child is still
    running leaves a zombie behind (and emits a ``ResourceWarning``).  The
    launched apps are long-lived, so keep a reference to every one of them
    and reap the ones that already exited.
    """
    for proc in list(_APP_PROCS):
        if proc.poll() is not None:
            _APP_PROCS.remove(proc)


def _open_app(value: str):
    """Launch a desktop application, returning the ``Popen`` object (or ``None``).

    ``value`` is the full command line the user writes after ``app:``, e.g.
    ``C:\\Program Files\\App\\app.exe --flag --port 8080``.  Uses
    ``_parse_app_cmd`` for the single split, ``_resolve_exe`` for the exe
    resolution, and passes the **resolved** exe + args to ``Popen`` so that
    ``%LOCALAPPDATA%`` / ``~`` / ``$MY_APP`` expansions are not lost.

    On Windows ``subprocess.Popen`` is used directly (``os.startfile``
    cannot pass extra args); on macOS the ``open`` command is invoked as
    ``open <exe> --args <args>`` (``--args`` must come *after* the target,
    otherwise ``open`` has no target and the app never starts); on Linux
    the exe is started directly (the user is expected to give a
    PATH-resolvable name or full path).

    An unparsable command line (unbalanced quote) yields ``None`` — the
    app is **not** launched with a guessed argument list.
    """
    exe_raw, args = _parse_app_cmd(value)
    if not exe_raw:
        return None
    exe = _resolve_exe(exe_raw)
    if not exe:
        return None
    # args is already a list from _parse_app_cmd — never re-split it,
    # otherwise quoted arguments with spaces would break apart again.
    if sys.platform.startswith("win"):
        cmd = [exe] + args
    elif sys.platform == "darwin":
        # open <target> --args <args...>：--args 必须在目标之后，
        # 否则 open 没有目标，应用根本不会启动。没有参数时不要拼
        # --args：open 会把 "--args" 当成应用的参数传下去。
        cmd = ["open", exe]
        if args:
            cmd += ["--args"] + args
    else:
        cmd = [exe] + args
    proc = subprocess.Popen(cmd)
    # Keep a reference to the child and reap finished ones: a Popen that
    # is garbage-collected while the process is still running leaves a
    # zombie behind (and emits a ResourceWarning).
    _APP_PROCS.append(proc)
    _reap_app_procs()
    return proc


def _win_foreground_hwnd() -> int:
    """Return the foreground window handle on Windows (0 when unavailable)."""
    api = _win32_api()
    if api is None:
        return 0
    return int(api[0].GetForegroundWindow() or 0)


def _win_find_app_window(pid: int) -> int:
    """Return the topmost visible window owned by ``pid`` (Windows).

    Used as a fallback when the launched app opens behind the current
    window: enumerate every visible window, resolve its owning process
    via ``GetWindowThreadProcessId``, and pick the first match for ``pid``.
    Returns 0 when no match is found or we are off Windows.

    Matching by PID (rather than by window-title substring) is deliberate:
    real window titles such as "Untitled - Notepad" or "Google Chrome"
    do not contain the exe basename, so title matching would never hit.
    """
    api = _win32_api()
    if api is None or not pid:
        return 0
    user32 = api[0]
    found = []
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        proc_id = ctypes.c_uint32(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value == pid:
            found.append(hwnd)
            return False  # stop at the first (topmost) match
        return True

    callback = enum_proc(_cb)
    user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def _wait_app_foreground(prev_hwnd: int, pid: int = 0,
                         timeout: float | None = None):
    """Wait for the just-launched desktop app to take the foreground (Windows).

    Desktop apps start slower than browsers and may open behind the current
    window, which is why the following ``text:`` / ``key:`` steps would land
    in the wrong window.

    Two phases:
    1. Passive — poll for up to ``timeout`` seconds (default
       ``MACRO_APP_FOCUS_TIMEOUT``) for the foreground window to change to
       something other than ``prev_hwnd`` (the handle captured before the
       launch).  Returns ``True`` as soon as a new window is focused.
    2. Force — if phase 1 times out, enumerate visible windows looking for
       the one owned by ``pid`` (via ``GetWindowThreadProcessId``) and call
       ``_force_foreground`` on it (the same fallback ``_wait_url_foreground``
       uses for browsers).  This catches apps that open in the background
       or were reused.

    Returns:
        ``True``  — a window ended up focused.
        ``False`` — Windows: timed out without focusing anything.
        ``None``  — off Windows; the caller must NOT treat this as a
                    failure (window managers already focus new windows).
    """
    if not sys.platform.startswith("win"):
        return None
    if timeout is None:
        timeout = MACRO_APP_FOCUS_TIMEOUT
    deadline = time.monotonic() + max(0.0, timeout)
    # Phase 1: passive wait for the foreground to change.
    while True:
        hwnd = _win_foreground_hwnd()
        if hwnd and hwnd != prev_hwnd:
            return True
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    # Phase 2: try to find and force the app window forward by PID.
    if pid:
        hwnd = _win_find_app_window(pid)
        if hwnd:
            return _force_foreground(hwnd)
    return False


# --- macro input reliability ---------------------------------------------
# ``text:`` used to call ``kb.write(value)`` with no inter-key delay. Electron
# apps, remote desktops and slow clients drop characters at that speed, and
# every character is rebuilt through the active keyboard layout, so non-ASCII
# text (Chinese, symbols) comes out wrong. A per-character delay fixes the
# first problem; ``paste:`` (clipboard + Ctrl/Cmd+V) fixes both, because the
# target receives one text block instead of synthetic keystrokes.
MACRO_TEXT_DEFAULT_DELAY = 0.02      # seconds between characters (20 ms)
MACRO_TEXT_MAX_DELAY = 5.0           # clamp a typo such as text!999999:
MACRO_PASTE_HOTKEY = "command+v" if sys.platform == "darwin" else "ctrl+v"
MACRO_PASTE_SETTLE = 0.05            # let the OS publish the clipboard first
MACRO_PASTE_RESTORE_DELAY = 0.4      # give the target time to read it back
MACRO_URL_FOCUS_TIMEOUT = 3.0        # wait for the browser window (Windows)
MACRO_APP_FOCUS_TIMEOUT = 3.0        # wait for the launched app window (Windows)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SW_RESTORE = 9
# Chromium's Chrome/Edge/Brave/Opera share one window class; Firefox has its own.
_BROWSER_WINDOW_CLASSES = ("Chrome_WidgetWin_1", "MozillaWindowClass")

_win32 = None  # cached (user32, kernel32); False once known to be unavailable


def _win32_api():
    """Return cached ``(user32, kernel32)`` ctypes bindings, or ``None``.

    Only loaded on Windows. Explicit ``argtypes`` / ``restype`` matter on
    64-bit: the default ``c_int`` return type truncates HANDLEs and makes
    ``GlobalLock`` / ``GetClipboardData`` fail with garbage pointers.
    """
    global _win32
    if _win32 is not None:
        return _win32 or None
    if not sys.platform.startswith("win"):
        _win32 = False
        return None
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE

        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(ctypes.c_uint32)]
        user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.EnumWindows.argtypes = None  # function pointer, passed as-is
        user32.EnumWindows.restype = wintypes.BOOL

        _win32 = (user32, kernel32)
    except Exception as e:  # noqa: BLE001
        print(f"[app] Win32 clipboard API unavailable ({type(e).__name__}: {e})")
        _win32 = False
    return _win32 or None


def _win_clipboard_open(retries: int = 8) -> bool:
    """OpenClipboard with short retries — another process may hold it briefly."""
    api = _win32_api()
    if api is None:
        return False
    user32 = api[0]
    for i in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01 * (i + 1))
    return False


def _win_clipboard_get() -> str | None:
    api = _win32_api()
    if api is None or not _win_clipboard_open():
        return None
    user32, kernel32 = api
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _win_clipboard_set(text: str) -> bool:
    api = _win32_api()
    if api is None or not _win_clipboard_open():
        return False
    user32, kernel32 = api
    handle = None
    try:
        if not user32.EmptyClipboard():
            return False
        # Fixed-size Unicode buffer: GlobalAlloc takes a size in bytes and the
        # clipboard expects a NUL-terminated UTF-16 block. Take the byte count
        # from the buffer itself (len() counts UTF-16 code units, NUL included)
        # so allocation and copy can never disagree on the size.
        buf = ctypes.create_unicode_buffer(text)
        size = len(buf) * ctypes.sizeof(ctypes.c_wchar)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        try:
            ctypes.memmove(ptr, buf, size)
        finally:
            kernel32.GlobalUnlock(handle)
        # Ownership of the handle moves to the clipboard on success; only free
        # it ourselves when SetClipboardData failed.
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        handle = None
        return True
    finally:
        if handle:
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()


def _run_quiet(argv, data: bytes | None = None, timeout: float = 2.0):
    """Run a clipboard helper; return stdout, or ``None`` when unusable."""
    try:
        proc = subprocess.run(argv, input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


# (write argv, read argv) per tool, most preferred first.
_LINUX_CLIPBOARD_TOOLS = (
    (("xclip", "-selection", "clipboard"), ("xclip", "-selection", "clipboard", "-o")),
    (("xsel", "--clipboard", "--input"), ("xsel", "--clipboard", "--output")),
    (("wl-copy",), ("wl-paste", "--no-newline")),
)


class _NativeClipboard:
    """Clipboard through a platform tool (Win32 API / pbcopy / xclip / wl-copy)."""

    def __init__(self, reader, writer):
        self._read = reader
        self._write = writer

    def read(self):
        try:
            return self._read()
        except Exception:  # noqa: BLE001
            return None

    def write(self, text: str) -> bool:
        try:
            return bool(self._write(text))
        except Exception:  # noqa: BLE001
            return False

    def close(self):
        pass


class _TkClipboard:
    """tkinter fallback, used when no native clipboard tool is installed.

    On X11 the clipboard is owned by the process that set it, so the Tk root
    must stay alive until the target has read the text — ``close()`` is only
    called after the paste (and the optional restore) finished. A fresh root
    per paste because macros run on worker threads and Tk is thread-affine.
    """

    def __init__(self, root):
        self._root = root

    @classmethod
    def open(cls):
        try:
            import tkinter

            root = tkinter.Tk()
        except Exception:  # noqa: BLE001 — no DISPLAY / Tcl missing
            return None
        try:
            root.withdraw()
        except Exception:  # noqa: BLE001
            pass
        return cls(root)

    def read(self):
        for kwargs in ({"type": "UTF8_STRING"}, {"type": "STRING"}, {}):
            try:
                return self._root.clipboard_get(**kwargs)
            except Exception:  # noqa: BLE001 — no text on the clipboard
                continue
        return None

    def write(self, text: str) -> bool:
        try:
            self._root.clipboard_clear()
            self._root.clipboard_append(text)
            self._root.update()  # flush the selection to the display server
            return True
        except Exception:  # noqa: BLE001
            return False

    def close(self):
        try:
            self._root.destroy()
        except Exception:  # noqa: BLE001
            pass


def _windows_clipboard():
    return _NativeClipboard(_win_clipboard_get, _win_clipboard_set) \
        if _win32_api() else None


def _macos_clipboard():
    if not (shutil.which("pbcopy") and shutil.which("pbpaste")):
        return None

    def _read():
        out = _run_quiet(["pbpaste"])
        return out.decode("utf-8", "replace") if out is not None else None

    def _write(text):
        return _run_quiet(["pbcopy"], text.encode("utf-8")) is not None

    return _NativeClipboard(_read, _write)


def _linux_clipboard():
    tools = list(_LINUX_CLIPBOARD_TOOLS)
    if os.environ.get("WAYLAND_DISPLAY"):
        # Native Wayland: xclip only sees XWayland's (usually empty) clipboard.
        tools.sort(key=lambda t: t[0][0] != "wl-copy")
    for write_argv, read_argv in tools:
        # Both halves must exist: a missing reader would only surface as a
        # failed runtime call (and a silent lost restore), so skip the backend.
        if not (shutil.which(write_argv[0]) and shutil.which(read_argv[0])):
            continue

        def _read(argv=read_argv):
            out = _run_quiet(list(argv))
            return out.decode("utf-8", "replace") if out is not None else None

        def _write(text, argv=write_argv):
            return _run_quiet(list(argv), text.encode("utf-8")) is not None

        return _NativeClipboard(_read, _write)
    return None


def _clipboard_candidates():
    """Backend factories in platform preference order.

    macOS deliberately skips the tkinter fallback: Tk must run on the main
    thread there, while macros run on worker threads, and ``pbcopy`` always
    ships with the OS anyway. A ``None`` result means "try the next one".
    """
    if sys.platform.startswith("win"):
        return (_windows_clipboard, _TkClipboard.open)
    if sys.platform == "darwin":
        return (_macos_clipboard,)
    return (_linux_clipboard, _TkClipboard.open)


def _restore_clipboard(cb, previous: str | None, text: str):
    """Put the user's previous clipboard text back after a paste.

    Best effort and never fatal: only restores plain text, only when the
    clipboard still holds exactly what we put there (so we never clobber
    something the user copied in the meantime), and only after a short delay
    so a slow target (remote desktop, VM) had time to read it.
    """
    if not previous or previous == text:
        return
    time.sleep(MACRO_PASTE_RESTORE_DELAY)
    try:
        if cb.read() == text:
            cb.write(previous)
    except Exception:  # noqa: BLE001
        pass


def _paste_text(kb, text: str, dev_id: str = "") -> bool:
    """Paste ``text`` through the clipboard + Ctrl/Cmd+V.

    Returns ``True`` when a backend pasted it. ``False`` means no clipboard
    was usable (or the hotkey failed) — the caller then falls back to
    per-character typing so the macro still completes.
    """
    if not text:
        return True
    for factory in _clipboard_candidates():
        cb = factory()
        if cb is None:
            continue
        try:
            # Read the old value first: pastes must not cost the user whatever
            # they had copied (passwords especially — see _restore_clipboard).
            previous = cb.read()
            if not cb.write(text):
                continue
            time.sleep(MACRO_PASTE_SETTLE)
            sent = True
            try:
                kb.send(MACRO_PASTE_HOTKEY)
            except Exception as e:  # noqa: BLE001
                sent = False
                add_event("action",
                          t("error.macro_paste_hotkey", hotkey=MACRO_PASTE_HOTKEY,
                            name=type(e).__name__, message=e),
                          dev_id, "error")
            # Restore on both paths: the clipboard already holds this step's
            # text, so bailing out on the failure path would lose what the
            # user had copied.
            _restore_clipboard(cb, previous, text)
            return sent
        finally:
            cb.close()
    add_event("action", t("log.clipboard_paste_skipped"),
              dev_id, "error")
    return False


def _text_delay(step: dict) -> float:
    """Per-character delay for a ``text:`` step, in seconds.

    ``text!80:pass`` stores ``delay=80`` (ms) via ``parseMacro()``; anything
    missing, unparsable or negative falls back to the 20 ms default.
    """
    raw = step.get("delay")
    if raw is None or raw == "":
        return MACRO_TEXT_DEFAULT_DELAY
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        return MACRO_TEXT_DEFAULT_DELAY
    if ms < 0:
        return MACRO_TEXT_DEFAULT_DELAY
    return min(ms / 1000.0, MACRO_TEXT_MAX_DELAY)


def _win_foreground_class() -> str:
    api = _win32_api()
    if api is None:
        return ""
    hwnd = api[0].GetForegroundWindow()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    if not api[0].GetClassNameW(hwnd, buf, 256):
        return ""
    return buf.value


def _win_topmost_browser_window():
    """First visible browser window in z-order (EnumWindows is top-down)."""
    api = _win32_api()
    if api is None:
        return 0
    user32 = api[0]
    found = []
    # Keep the callback object alive for the duration of the call.
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        if not user32.GetClassNameW(hwnd, buf, 256):
            return True
        if buf.value in _BROWSER_WINDOW_CLASSES:
            found.append(hwnd)
            return False  # stop at the first (topmost) match
        return True

    callback = enum_proc(_cb)
    user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def _force_foreground(hwnd) -> bool:
    """Show + focus ``hwnd``; ``False`` when Windows refuses (foreground lock)."""
    api = _win32_api()
    if api is None or not hwnd:
        return False
    user32 = api[0]
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:  # noqa: BLE001 — foreground lock, UIPI, ...
        return False


def _wait_url_foreground(timeout: float | None = None) -> bool:
    """Bring the window that just handled ``url:`` to the foreground (Windows).

    Synthetic keys go to whatever window has focus: a browser that opened
    behind the current app silently swallows the following ``text:`` /
    ``paste:`` steps. Polls for up to ``timeout`` seconds (default
    ``MACRO_URL_FOCUS_TIMEOUT``) while a cold browser starts, and forces the
    topmost browser window forward as soon as one is on screen. Returns
    ``True`` when a browser window ends up focused. A no-op off Windows, where
    window managers already focus new windows.
    """
    if not sys.platform.startswith("win"):
        return False
    if timeout is None:
        timeout = MACRO_URL_FOCUS_TIMEOUT
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if _win_foreground_class() in _BROWSER_WINDOW_CLASSES:
            return True
        hwnd = _win_topmost_browser_window()
        if hwnd:
            # A browser window is already on screen but not focused (opened
            # behind us, or reused as a background tab): bring it forward now
            # instead of burning the rest of the timeout.
            return _force_foreground(hwnd)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _macro_cancelled(action: dict, step: int, total: int,
                     step_type: str) -> dict:
    """Result object for a macro stopped by the user (WS-495).

    Cancelling is not a failure of the action: the result carries
    ``cancelled: True`` so the UI can show a neutral "已停止" instead of a red
    error toast, and so ``_record_action_result`` writes a ``warn`` row that
    names the step but never its body (WS-484 rule).
    """
    label = MACRO_STEP_LABELS.get(step_type, step_type) or t("log.macro_unknown_step")
    return {"ok": False,
            "cancelled": True,
            **_err(t("log.macro_cancelled", step=step, total=total, label=label)),
            "action_type": "macro",
            "target": _action_display_target(action),
            "step": step,
            "step_type": step_type,
            "total_steps": total}


def run_action(action: dict, dev_id: str = "", cancel=None) -> dict:
    """Execute one host-side action and return a structured result.

    Returns ``{ok: bool, error: str, action_type: str, target: str}`` where
    ``error`` is a human-readable failure reason (empty when ``ok`` is True).
    ``dev_id`` only tags inner log rows (clipboard/paste warnings) with the
    device they came from.

    ``cancel`` is an optional ``threading.Event`` (WS-495).  A ``macro`` stops
    at the next step boundary when it is set; ``wait:`` steps are sliced into
    50 ms pieces so a stop does not have to wait out the whole delay.  The
    step in flight is never retracted — the keystrokes it already sent cannot
    be recalled.

    Never raises: every code path produces a result object.
    """
    action = action or {}
    kind = action.get("type", "none")
    value = action.get("value", "")
    # WS-484: a macro target is a summary of its steps, never its body —
    # the body may be a password and the log is copyable.
    target = _action_display_target(action)

    if kind == "none":
        return {"ok": True, "error": "", "action_type": "none", "target": target}

    if kind in ("launch", "url"):
        if not value:
            if kind == "url":
                return {"ok": False, **_err(t("error.empty_address")),
                        "action_type": kind, "target": target}
            return {"ok": True, "error": "", "action_type": kind, "target": target}
        # Pre-check local launch targets so failures are visible on every
        # platform (xdg-open / open do not raise on Linux for missing files).
        if kind == "launch" and not value.startswith(("http://", "https://")):
            if not os.path.exists(value):
                return {"ok": False, **_err(t("log.file_missing") + value),
                        "action_type": kind, "target": target}
        try:
            _open_target(value)
            return {"ok": True, "error": "", "action_type": kind, "target": target}
        except (FileNotFoundError, PermissionError, OSError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "action_type": kind, "target": target}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "action_type": kind, "target": target}

    if kind == "command":
        try:
            subprocess.Popen(value, shell=True)
            return {"ok": True, "error": "", "action_type": kind, "target": target}
        except (FileNotFoundError, PermissionError, OSError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "action_type": kind, "target": target}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "action_type": kind, "target": target}

    if kind == "macro":
        kb = _load_keyboard()
        if kb is None:
            return {"ok": False, **_err(t("error.macro_need_keyboard")),
                    "action_type": kind, "target": target}
        # WS-484: remember which step failed so the log row can point at it
        # without echoing the macro body.
        current_step = 0
        current_type = ""
        macro_steps = action.get("macro", []) or []
        total_steps = len(macro_steps)
        try:
            # ``browser:`` steps choose the target for later ``url:`` steps;
            # None means "system default browser". Reset on a failed open so a
            # broken target does not break every following url.
            browser_target = None
            browser_used = "default"
            # WS-498: one wording for the step number in the stop log — the
            # step that was running when the user asked to stop, never the
            # step that would have come next.  A cancel is only observable
            # at a step boundary, so ``ran_step`` / ``ran_type`` remember the
            # step that just ran and the boundary reports *that* one.
            ran_step = 0
            ran_type = ""
            for current_step, step in enumerate(macro_steps, 1):
                st = step.get("type")
                current_type = st or ""
                # WS-495: stop *between* steps.  The step that is already
                # typing keeps going (its keys are out), but nothing after
                # it runs.
                if cancel is not None and cancel.is_set():
                    if ran_step:
                        # WS-498: the step in flight, not the next one.
                        return _macro_cancelled(action, ran_step,
                                                total_steps, ran_type)
                    # Nothing ran yet: the macro stopped before step 1.
                    return _macro_cancelled(action, current_step,
                                            total_steps, current_type)
                ran_step, ran_type = current_step, current_type
                if st == "key":
                    # 宏里可以写界面上的键名（PageUp / RCtrl / Tab ...），
                    # 这里统一换成 keyboard 库认识的写法。
                    kb.send(macro_key_name(step.get("value", "")))
                elif st == "text":
                    # Per-character delay: 0 ms drops characters in Electron
                    # apps / remote desktops, and non-ASCII text needs the
                    # layout round-trip to settle. ``text!<ms>:`` overrides it.
                    kb.write(step.get("value", ""), delay=_text_delay(step))
                elif st == "paste":
                    # Clipboard + Ctrl/Cmd+V: the only reliable way to type
                    # Chinese / symbols / passwords into apps that mangle
                    # synthetic keystrokes. Falls back to typing on failure.
                    if not _paste_text(kb, step.get("value", ""), dev_id):
                        kb.write(step.get("value", ""),
                                 delay=MACRO_TEXT_DEFAULT_DELAY)
                elif st == "delay":
                    wait_s = max(0, int(step.get("value", 0))) / 1000.0
                    if cancel is None:
                        time.sleep(wait_s)
                    else:
                        # WS-495: sleep in <=50 ms slices so a stop request
                        # takes effect almost immediately instead of after a
                        # whole wait:5000.  The remainder is the last slice.
                        remaining = wait_s
                        while remaining > 0:
                            if cancel.is_set():
                                return _macro_cancelled(
                                    action, current_step, total_steps,
                                    current_type)
                            slice_s = min(0.05, remaining)
                            time.sleep(slice_s)
                            remaining -= slice_s
                elif st == "app":
                    # Launch a desktop application, then wait for its window to
                    # take the foreground so the following text:/key:/paste:
                    # steps land in the right window.  A missing exe or an
                    # unparsable command line is reported and skipped — the
                    # rest of the macro keeps running.
                    #
                    # 单次解析：_parse_app_cmd 拆出 (exe, args)，_resolve_exe
                    # 只对 exe 做展开/PATH 查找，_open_app 用解析后的结果
                    # Popen，避免整条命令行去 exists/which 导致带参数时
                    # 一律判死，以及环境变量展开在启动时丢失。
                    app_val = step.get("value", "")
                    if not app_val.strip():
                        add_event("error", t("log.macro_app_no_exe"),
                                  dev_id, "error")
                        continue
                    app_exe_raw, _app_args = _parse_app_cmd(app_val)
                    if not app_exe_raw:
                        # 引号不配对（或只写了一个空的可执行文件名）：整条
                        # 命令行不可信，绝不带着猜测的参数去启动应用。
                        # _app_cmd_error 只在出错这条路径上再解析一次，
                        # 用于区分「引号不配对」和「没写可执行文件」。
                        add_event("error",
                                  t("log.macro_app_bad_cmd", reason=_app_cmd_error(app_val)),
                                  dev_id, "error")
                        continue
                    app_exe = _resolve_exe(app_exe_raw)
                    if not app_exe:
                        add_event("error",
                                  t("log.macro_app_exe_missing", exe=app_exe_raw),
                                  dev_id, "error")
                        continue
                    add_event("action", t("log.macro_app_starting", target=app_val), dev_id, "ok")
                    prev_hwnd = _win_foreground_hwnd()
                    proc = None
                    try:
                        proc = _open_app(app_val)
                    except (FileNotFoundError, PermissionError,
                            OSError) as e:
                        add_event("error",
                                  t("log.macro_app_start_failed",
                                    name=type(e).__name__, message=e),
                                  dev_id, "error")
                    if proc is not None:
                        # Desktop apps start slower than browsers;
                        # without this the following text:/key:
                        # steps land in the previous window.
                        # _wait_app_foreground returns None off
                        # Windows (not a failure — window managers
                        # already focus new windows); only warn on
                        # a real Windows timeout (False).
                        focused = _wait_app_foreground(
                            prev_hwnd, proc.pid)
                        if focused is False:
                            add_event("warn",
                                      t("log.macro_app_focus_timeout",
                                        seconds=MACRO_APP_FOCUS_TIMEOUT),
                                      dev_id, "warn")
                elif st == "browser":
                    browser_target, browser_used, browser_err = (
                        resolve_macro_browser(step.get("value", "")))
                    if browser_err:
                        print("[app] macro browser step:", browser_err)
                        add_event("error", browser_err + t("log.macro_browser_fallback_suffix"),
                                  level="error")
                elif st == "url":
                    # Empty url: skip silently (no exception).  Non-empty url:
                    # open the browser; subsequent text:/key: steps can fill in
                    # the page that loads (login flows, etc.).
                    url_val = step.get("value", "")
                    if url_val:
                        open_err = _open_url_with(browser_target, url_val)
                        # WS-484: the log is copyable — never write URL
                        # credentials into it.
                        url_log = _redact_url_userinfo(url_val)
                        if open_err:
                            add_event(
                                "error",
                                t("log.macro_url_open_failed", browser=browser_used,
                                  url=url_log, error=open_err),
                                level="error")
                            browser_target = None
                            browser_used = "default"
                        elif browser_used == "default":
                            add_event("action", t("log.macro_url_opened_default", url=url_log),
                                      level="ok")
                        else:
                            add_event("action",
                                      t("log.macro_url_opened", browser=browser_used, url=url_log),
                                      level="ok")
                        # Keep the freshly opened window in front, otherwise
                        # the next text:/paste: steps land in the old window.
                        # Windows only; bounded by MACRO_URL_FOCUS_TIMEOUT.
                        _wait_url_foreground()
            # WS-498: the stop may have arrived during the *last* step — the
            # loop ends without another boundary check, so close it here and
            # name the step that was in flight.
            if cancel is not None and cancel.is_set() and ran_step:
                return _macro_cancelled(action, ran_step, total_steps,
                                        ran_type)
            return {"ok": True, "error": "", "action_type": kind,
                    "target": target, "browser_used": browser_used}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "action_type": kind, "target": target,
                    "step": current_step, "step_type": current_type}

    if kind == "media":
        kb = _load_keyboard()
        if kb is None:
            return {"ok": False, **_err(t("error.media_need_keyboard")),
                    "action_type": kind, "target": target}
        try:
            kb.send(value)
            return {"ok": True, "error": "", "action_type": kind, "target": target}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "action_type": kind, "target": target}

    return {"ok": True, "error": "", "action_type": kind, "target": target}


def _action_label(idx: int, dev_id: str = "") -> str:
    """Human-readable input label for an index.

    0-8 = 按键 1-9, 9 = 编码器按压 — or, in the 12-key layout, 按键 10/11/12
    (WS-499). Without a device the encoder wording is kept, which is what the
    pre-layout log lines said.
    """
    if dev_id:
        with _lock:
            cfg = _config["devices"].get(dev_id)
        if cfg is not None:
            return _input_label(cfg, idx)
    return t("ui.input_button_n") % (idx + 1) if 0 <= idx < 9 else t("ui.input_encoder_press")


def _record_action_result(dev_id: str, idx: int, action: dict, result: dict,
                          label: str = ""):
    """Append a ``kind=action`` result row to the event log.

    Success rows use ``level=ok``; failure rows use ``level=error`` and also
    trigger a toast so the user sees the failure immediately.

    ``label`` overrides the default input label (used by WS-389 for encoder
    directions, which are not one of the 10 button indices).

    WS-484: a macro failure points at the step that failed ("宏第 3/6 步
    key 失败") instead of echoing the macro body.  The body may hold a
    password and the log is copyable row by row.
    """
    if not label:
        label = _action_label(idx, dev_id)
    atype = result.get("action_type", (action or {}).get("type", "unknown"))
    target = result.get("target", "")
    # WS-495: a user-requested stop is not a failure — one neutral warn row
    # naming the step, and (WS-484) never the step body.
    if result.get("cancelled"):
        steps = (action or {}).get("macro") or []
        total = result.get("total_steps")
        if not isinstance(total, int) or total <= 0:
            total = len(steps) if isinstance(steps, list) else 0
        step_type = MACRO_STEP_LABELS.get(result.get("step_type", ""),
                                          result.get("step_type", ""))
        if step_type:
            where = t("log.step_where_type", step=result.get("step", 0),
                      total=total, type=step_type)
        else:
            where = t("log.step_where", step=result.get("step", 0), total=total)
        add_event("action", t("log.action_stopped", label=label, where=where), dev_id, "warn")
        return
    if result["ok"]:
        add_event("action", t("log.action_ok", label=label, type=atype, target=target), dev_id, "ok")
        return
    if atype == "macro" and "step" in result:
        steps = (action or {}).get("macro") or []
        total = len(steps) if isinstance(steps, list) else 0
        step_type = MACRO_STEP_LABELS.get(result.get("step_type", ""),
                                          result.get("step_type", ""))
        if step_type:
            where = t("log.macro_where_type", step=result.get("step", 0),
                      total=total, type=step_type)
        else:
            where = t("log.macro_where", step=result.get("step", 0), total=total)
        add_event("action",
                  t("log.action_failed_macro", label=label, where=where,
                    error=result["error"]),
                  dev_id, "error")
        return
    add_event("action",
              t("log.action_failed", label=label, type=atype, target=target,
                error=result["error"]),
              dev_id, "error")


def _prune_stuck_actions(now: float | None = None):
    """Force-release claims whose owner thread is gone but never released.

    A dead-but-unreleased entry can only come from a thread that disappeared
    without running its ``finally`` (or never started at all). A *live* thread
    is never pruned however long it runs: a 10-minute macro is still one macro,
    and releasing it would let a second press run next to it (WS-360).

    Called whenever the run table is touched, which keeps the check free of any
    extra timer thread.
    """
    if now is None:
        now = time.monotonic()
    with _lock:
        stuck = []
        for key, entry in list(_action_running.items()):
            if now - entry["started"] < ACTION_STUCK_SECONDS:
                continue
            thread = entry.get("thread")
            if thread is not None and thread.is_alive():
                continue
            _action_running.pop(key, None)
            stuck.append((key, now - entry["started"]))
    for (dev_id, idx), elapsed in stuck:
        add_event("action",
                  t("log.action_timeout_release", label=_action_label(idx, dev_id),
                    seconds=f"{elapsed:.1f}"),
                  dev_id, "warn")


def _begin_action(dev_id: str, idx: int):
    """Claim ``(dev_id, idx)`` for one action.

    Returns ``(token, None)`` when the claim succeeded — the caller must pass
    that token back to ``_end_action`` when the action finishes — or
    ``(None, elapsed)`` when the previous action of this input is still busy.

    Each claim gets its own token, so an owner can only release the claim it
    took: whatever drops an entry early (stale-claim guard, any future cleanup
    path), the old thread's ``finally`` must not unlock whoever claimed the
    input afterwards (WS-360).
    """
    global _action_token_seq
    now = time.monotonic()
    _prune_stuck_actions(now)
    with _lock:
        entry = _action_running.get((dev_id, idx))
        if entry is not None:
            return None, now - entry["started"]
        _action_token_seq += 1
        token = _action_token_seq
        _action_running[(dev_id, idx)] = {
            "started": now, "token": token, "thread": None,
            # WS-495: set by /api/action/cancel, polled by the macro loop.
            "cancel": threading.Event(),
        }
        return token, None


def _action_cancel_event(dev_id: str, idx: int):
    """The cancel flag of the live claim on ``(dev_id, idx)``, or ``None``.

    Read right after ``_begin_action`` succeeded: the claim is exclusive, so
    the event belongs to this run and is safe to hand to ``run_action``.
    """
    with _lock:
        entry = _action_running.get((dev_id, idx))
        return entry.get("cancel") if entry else None


def _cancel_action(dev_id: str, idx: int) -> bool:
    """Request a stop for the action running on ``(dev_id, idx)`` (WS-495).

    Returns True only when this call actually flipped the flag; every other
    case (no claim, already finished, already asked) returns False so the
    caller can tell "I stopped it" from "there was nothing to stop".

    WS-498: the return value alone no longer decides what the HTTP route
    answers — a live claim that was already flagged is still winding down and
    must be reported as ``cancelling``.  The route asks
    ``_action_claim_alive`` for that.
    """
    with _lock:
        entry = _action_running.get((dev_id, idx))
        if entry is None:
            return False
        event = entry.get("cancel")
        if event is None or event.is_set():
            return False
        event.set()
        return True


def _action_claim_alive(dev_id: str, idx: int) -> bool:
    """True while ``(dev_id, idx)`` is still claimed by a running action.

    The claim outlives the stop flag: a macro that was asked to stop keeps
    running until the step in flight finishes (WS-498).  The route needs
    this to tell "the macro is still winding down" apart from "there is
    nothing to stop at all" — answering ``already_done`` for a live claim
    makes the UI snap back to idle while the macro is still typing.
    """
    with _lock:
        return (dev_id, idx) in _action_running


def _bind_action_thread(dev_id: str, idx: int, token: int, thread):
    """Attach the owning thread to a claim so the stuck guard can inspect it."""
    with _lock:
        entry = _action_running.get((dev_id, idx))
        if entry is not None and entry["token"] == token:
            entry["thread"] = thread


def _end_action(dev_id: str, idx: int, token: int):
    """Release the claim taken by ``_begin_action`` (idempotent).

    Only the claim carrying *token* is released; a stale owner whose claim was
    already dropped leaves a newer claim untouched.
    """
    with _lock:
        entry = _action_running.get((dev_id, idx))
        if entry is not None and entry["token"] == token:
            _action_running.pop((dev_id, idx), None)


def _running_snapshot() -> dict:
    """``{dev_id: {"<idx>": 已运行秒数}}`` for actions currently executing.

    Claims held by a deck that is no longer connected are *not reported* — the
    card must stop breathing once the device is gone — but they are kept, so a
    BLE hiccup or a quick reconnect cannot hand the same input to a second
    macro while the first one is still typing (WS-360).
    """
    _prune_stuck_actions()
    connected = {d.get("dev_id", "") for d in ble.devices() if d.get("connected")}
    now = time.monotonic()
    out: dict = {}
    with _lock:
        for (dev_id, idx), entry in _action_running.items():
            if dev_id not in connected:
                continue
            out.setdefault(dev_id, {})[str(idx)] = round(now - entry["started"], 1)
    return out


def _run_action_for(dev_id: str, idx: int):
    if not _config["settings"].get("actions_enabled", True):
        return
    if idx < 0:
        return
    cfg = device_cfg(dev_id)
    keys = cfg.get("keys", [])
    if idx >= len(keys):
        return
    action = keys[idx].get("action") or {}
    if action.get("type", "none") == "none":
        return

    token, busy_for = _begin_action(dev_id, idx)
    if token is None:
        # Re-entry: the previous macro is still typing into the focused window,
        # so a second one would interleave characters. Drop it silently.
        add_event("action",
                  t("log.action_skipped_busy", label=_action_label(idx, dev_id),
                    seconds=f"{busy_for:.1f}"),
                  dev_id, "warn")
        return

    # WS-495: a physical key press is stoppable through the same route.
    cancel = _action_cancel_event(dev_id, idx)

    def _do():
        try:
            result = run_action(action, dev_id, cancel=cancel)
            _record_action_result(dev_id, idx, action, result)
        finally:
            # Release even when run_action raised; otherwise the card would
            # breathe until the stale-claim guard fires.
            _end_action(dev_id, idx, token)

    thread = threading.Thread(target=_do, daemon=True)
    _bind_action_thread(dev_id, idx, token, thread)
    try:
        thread.start()
    except Exception:  # noqa: BLE001 — Thread.start() failed, keep the table clean
        _end_action(dev_id, idx, token)
        raise


# --- encoder rotation actions (WS-389) -------------------------------------
def _encoder_action_label(direction: str) -> str:
    """Human-readable label for one rotation direction."""
    return t("ui.input_encoder_cw") if direction == "CW" else t("ui.input_encoder_ccw")


def _prune_stuck_encoder_actions(now: float | None = None):
    """Force-release encoder claims whose owner thread is gone.

    Mirrors ``_prune_stuck_actions`` for the ``(dev_id, direction)`` table.
    """
    if now is None:
        now = time.monotonic()
    with _lock:
        stuck = []
        for key, entry in list(_encoder_action_running.items()):
            if now - entry["started"] < ACTION_STUCK_SECONDS:
                continue
            thread = entry.get("thread")
            if thread is not None and thread.is_alive():
                continue
            _encoder_action_running.pop(key, None)
            stuck.append((key, now - entry["started"]))
    for (dev_id, direction), elapsed in stuck:
        add_event("action",
                  t("log.action_timeout_release",
                    label=_encoder_action_label(direction), seconds=f"{elapsed:.1f}"),
                  dev_id, "warn")


def _begin_encoder_action(dev_id: str, direction: str, action: dict,
                          ignore_cooldown: bool = False):
    """Claim ``(dev_id, direction)`` for one encoder action.

    Returns ``(token, None)`` on success — the caller must pass that token
    back to ``_end_encoder_action`` — or ``(None, busy_for)`` when a previous
    action of this direction is still running (caller logs "动作跳过"), or
    ``(None, None)`` when the one-shot cooldown has not elapsed yet (caller
    drops silently). The cooldown check and the claim are atomic under
    ``_lock`` so two near-simultaneous detents cannot both trigger.

    ``ignore_cooldown`` skips the one-shot cooldown for an explicit user
    action (the "测试动作" button). A manual test must never be swallowed by
    the rotation debounce, and it must not push the debounce window for the
    following rotations either — the cooldown models *rotation detents*, not
    clicks. The "still running" preemption still applies to both paths.
    """
    global _encoder_action_token_seq
    now = time.monotonic()
    _prune_stuck_encoder_actions(now)
    with _lock:
        entry = _encoder_action_running.get((dev_id, direction))
        if entry is not None:
            return None, now - entry["started"]
        if not ignore_cooldown:
            if (action or {}).get("type", "none") in _ENCODER_ONE_SHOT:
                last = _encoder_last_trigger.get((dev_id, direction))
                if last is not None and now - last < _ENCODER_COOLDOWN_S:
                    return None, None
        _encoder_action_token_seq += 1
        token = _encoder_action_token_seq
        _encoder_action_running[(dev_id, direction)] = {
            "started": now, "token": token, "thread": None}
        if (not ignore_cooldown
                and (action or {}).get("type", "none") in _ENCODER_ONE_SHOT):
            _encoder_last_trigger[(dev_id, direction)] = now
        return token, None


def _bind_encoder_action_thread(dev_id, direction, token, thread):
    """Attach the owning thread to a claim so the stuck guard can inspect it."""
    with _lock:
        entry = _encoder_action_running.get((dev_id, direction))
        if entry is not None and entry["token"] == token:
            entry["thread"] = thread


def _end_encoder_action(dev_id, direction, token):
    """Release the claim taken by ``_begin_encoder_action`` (idempotent)."""
    with _lock:
        entry = _encoder_action_running.get((dev_id, direction))
        if entry is not None and entry["token"] == token:
            _encoder_action_running.pop((dev_id, direction), None)


def _encoder_running_snapshot() -> dict:
    """``{dev_id: {"CW"|"CCW": 已运行秒数}}`` for encoder actions executing.

    Same connected-only rule as ``_running_snapshot``: a deck that went away
    mid-action is not reported, but the claim is kept so a quick reconnect
    cannot hand the same detent to a second action (WS-360).
    """
    _prune_stuck_encoder_actions()
    connected = {d.get("dev_id", "") for d in ble.devices() if d.get("connected")}
    now = time.monotonic()
    out: dict = {}
    with _lock:
        for (dev_id, direction), entry in _encoder_action_running.items():
            if dev_id not in connected:
                continue
            out.setdefault(dev_id, {})[direction] = round(now - entry["started"], 1)
    return out


def _run_encoder_action(dev_id: str, direction: str):
    """Execute the host-side action bound to one rotation direction.

    Gate: only mode 4 ("上位机动作") runs host actions; every other mode
    leaves rotation to the device (which still sends F23/F24). A direction
    whose action is "仅 HID" (type none) does nothing here — the device's
    F23/F24 is the only effect, matching the default (未配置) behaviour.
    """
    if not _config["settings"].get("actions_enabled", True):
        return
    if direction not in ("CW", "CCW"):
        return
    cfg = device_cfg(dev_id)
    if cfg.get("encoder", {}).get("mode") != 4:
        return
    actions = cfg.get("encoder", {}).get("actions", {}) or {}
    action = actions.get(direction) or {}
    if not action or action.get("type", "none") == "none":
        return

    label = _encoder_action_label(direction)
    token, busy_for = _begin_encoder_action(dev_id, direction, action)
    if token is None and busy_for is None:
        # One-shot cooldown elapsed: silently ignore this detent.
        return
    if token is None:
        # A previous action of this direction is still running — dropping a
        # second one would interleave characters with the first (WS-360).
        add_event("action",
                  t("log.action_skipped_busy", label=label, seconds=f"{busy_for:.1f}"),
                  dev_id, "warn")
        return

    def _do():
        try:
            result = run_action(action, dev_id)
            _record_action_result(dev_id, -1, action, result, label=label)
        finally:
            _end_encoder_action(dev_id, direction, token)

    thread = threading.Thread(target=_do, daemon=True)
    _bind_encoder_action_thread(dev_id, direction, token, thread)
    try:
        thread.start()
    except Exception:  # noqa: BLE001 — Thread.start() failed, keep the table clean
        _end_encoder_action(dev_id, direction, token)
        raise


# --- HTTP API -------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with _lock:
        last = dict(_last_press)
    return jsonify({
        "ble_ok": ble.available,
        "ble_error": ble.error,
        "devices": device_list(),
        "actions_enabled": _config["settings"].get("actions_enabled", True),
        "last_press": {k: {"index": v[0], "age": round(time.time() - v[1], 2)}
                       for k, v in last.items()},
        # WS-359: additive field — old front-ends simply ignore it.
        "running": _running_snapshot(),
        # WS-389: additive — encoder rotation actions executing, per direction.
        "encoder_running": _encoder_running_snapshot(),
        "event_seq": _event_seq,
        # WS-395: additive field — the last scan's conclusion ("why is no
        # device found?"). ``at`` is None until the first scan finished.
        "scan": _scan_state(),
        # WS-445: additive — the last runtime-health sample per device, so a
        # page reload (or a disconnect) keeps showing the real last values.
        "sysinfo": sysinfo_snapshot(),
        # WS-523: additive — the host's own version, so the UI can show
        # "current v1.4.2" next to the update check.
        "app_version": _app_version_payload(),
    })


@app.route("/api/defaults")
def api_defaults():
    """Default config, plus the pin rule the device page renders (WS-476).

    Read-only and side-effect free, so the form also sends its current values
    here (``?chip=C3&pins=0,1,,3``) to get per-row errors back. That keeps the
    validation rule in one place — the firmware-equivalent ``validate_pins()``
    — instead of a second copy in JavaScript.
    """
    chip = request.args.get("chip", "ESP32")
    try:
        layout = int(request.args.get("layout", LAYOUT_ENCODER))
    except ValueError:
        layout = LAYOUT_ENCODER
    layout = LAYOUT_12KEY if layout == LAYOUT_12KEY else LAYOUT_ENCODER
    payload = new_device_cfg(chip, "")
    payload["layout"] = layout
    payload["pinRules"] = pin_rules(chip, layout)
    pins = payload["pins"]
    if "pins" in request.args:
        pins = parse_pins_query(request.args.get("pins", ""))
        payload["pinErrors"] = validate_pins(pins, chip, layout)
    # WS-542: the device page renders "可唤醒输入：…" from this, and the page
    # passes the firmware version it is looking at so an older deck (< 1.3.9,
    # which still armed ext0 on the encoder push) hides the line instead of
    # being promised a wake it does not have.
    payload["wakeInputs"] = wake_inputs(chip, layout, pins)
    payload["wakeSupported"] = ("fw" not in request.args
                                or online_update.version_at_least(
                                    request.args.get("fw", ""), WAKE_MIN_VERSION))
    return jsonify(payload)


@app.route("/api/scan")
def api_scan():
    found = ble.discover(timeout=6.0)
    reason = _scan_state()["reason"]
    return jsonify({"ok": True, "found": found, "reason": reason})


@app.route("/api/config/<dev_id>", methods=["GET", "POST"])
def api_config(dev_id):
    if request.method == "GET":
        return jsonify(device_cfg(dev_id))
    data = request.get_json(silent=True) or {}
    cfg = device_cfg(dev_id)
    with _lock:
        # WS-512: ``trusted`` is deliberately absent — it is a fact about what
        # the host has read from the deck, never something the draft-save can
        # set or clear (an old front-end echoing the whole cfg must not do it).
        for field in ("pins", "keys", "encoder", "sleep", "tz", "name", "layout"):
            if field in data:
                cfg[field] = data[field]
        # WS-546: a draft from an older page has keys without ``mods``; pad /
        # fill so the entry shape stays uniform (0 = no modifier).
        ensure_key_items(cfg)
        save_config()
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/apply/<dev_id>", methods=["POST"])
def api_apply(dev_id):
    result = apply_device(dev_id)
    # WS-476: an invalid pin set is a client-side problem, not a server crash.
    return jsonify(result), (400 if result.get("invalid") else 200)


@app.route("/api/action/test/<dev_id>/<int:idx>", methods=["POST"])
def api_action_test(dev_id, idx):
    cfg = device_cfg(dev_id)
    keys = cfg.get("keys", [])
    if idx < 0 or idx >= len(keys):
        return jsonify({"ok": False, "error": "bad index"}), 400
    action = keys[idx].get("action") or {}
    if action.get("type", "none") == "none":
        return jsonify({"ok": True, "error": ""})

    # WS-359: the test button shares the physical key's claim, so a test can
    # never start a second macro next to a running one.
    token, busy_for = _begin_action(dev_id, idx)
    if token is None:
        return jsonify({"ok": False, **_err(t("log.action_still_running_key")),
                        "running": True, "elapsed": round(busy_for, 1)}), 200

    # Execute synchronously (5 s cap) so the caller gets the real result.
    result_queue: "queue.Queue" = queue.Queue()
    # WS-495: so the card's "停止" works during the 5 s window too.
    cancel = _action_cancel_event(dev_id, idx)

    def _worker():
        try:
            try:
                result = run_action(action, dev_id, cancel=cancel)
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error": f"{type(e).__name__}: {e}",
                          "action_type": action.get("type", ""),
                          "target": _action_display_target(action)}
            result_queue.put(result)
            # WS-495: record here, not in the route.  A macro that outlived
            # the 5 s window used to finish with no log row at all, so a
            # stop after that window left no "动作已停止" trace.
            _record_action_result(dev_id, idx, action, result)
        finally:
            # A macro longer than the 5 s wait keeps running in the background;
            # the worker owns the claim then, so the card stays in "执行中".
            _end_action(dev_id, idx, token)

    worker = threading.Thread(target=_worker, daemon=True)
    _bind_action_thread(dev_id, idx, token, worker)
    try:
        worker.start()
    except Exception:  # noqa: BLE001 — Thread.start() failed, keep the table clean
        _end_action(dev_id, idx, token)
        raise
    try:
        result = result_queue.get(timeout=5.0)
    except queue.Empty:
        # Still running: report it as such instead of a misleading timeout.
        return jsonify({"ok": False, **_err(t("log.action_still_running")), "running": True}), 200

    _end_action(dev_id, idx, token)
    return jsonify({"ok": result["ok"], "error": result["error"],
                    # WS-495: additive — a stop is not a failure.
                    "cancelled": bool(result.get("cancelled")),
                    "browser_used": result.get("browser_used", "")})


@app.route("/api/action/cancel/<dev_id>/<int:idx>", methods=["POST"])
def api_action_cancel(dev_id, idx):
    """Stop the action running on one input at the next step boundary (WS-495).

    Always 200: "there was nothing to stop" is a normal answer, not an error
    (the macro may have finished between the click and the request, or the
    deck may have disconnected with a stale claim).  ``already_done`` tells
    the UI to leave its neutral state alone instead of showing a failure.

    WS-498: a claim that is still alive means the macro has not finished —
    it is winding down after a stop that was already requested (a long
    ``text:`` / ``app:`` step cannot be interrupted mid-step).  That is still
    "cancelling", not "already_done": the UI has to keep showing
    「正在停止…」 instead of dropping back to 「测试动作」 and then jumping to
    「停止」 again on the next poll.

    The claim is checked *first*: ``already_done`` may only be answered when
    there is nothing to stop at all.  Asking the claim before flipping the
    flag also avoids answering "cancelling" for a macro that started between
    the two calls — a flag that was never set would not stop it.
    """
    if not _action_claim_alive(dev_id, idx):
        return jsonify({"ok": True, "already_done": True})
    # The claim exists: set the stop flag (a no-op when it is already set)
    # and report the macro as winding down.
    _cancel_action(dev_id, idx)
    return jsonify({"ok": True, "cancelling": True})


@app.route("/api/encoder/action/test/<dev_id>/<direction>", methods=["POST"])
def api_encoder_action_test(dev_id, direction):
    """Synchronously test the action bound to one encoder direction.

    Mirrors ``api_action_test``: the 5 s HTTP cap keeps the response alive
    while a longer macro keeps running in the background, the worker owning
    the claim so the card stays "执行中" until the action really finishes.
    """
    if direction not in ("CW", "CCW"):
        return jsonify({"ok": False, "error": "bad direction"}), 400
    cfg = device_cfg(dev_id)
    if cfg.get("encoder", {}).get("mode") != 4:
        # Not in 上位机动作 mode: the card is greyed out in the UI, so this is
        # only reached if the user somehow clicks. Report the refusal instead
        # of a fake success (WS-390 review #1).
        return jsonify({"ok": False,
                        **_err(t("error.encoder_test_mode"))})
    actions = cfg.get("encoder", {}).get("actions", {}) or {}
    action = actions.get(direction) or {}
    if not action or action.get("type", "none") == "none":
        return jsonify({"ok": True, "error": ""})

    # An explicit test is a user action, not a rotation detent: it must not
    # be swallowed by the 800 ms one-shot cooldown (WS-390 review #2). The
    # "still running" preemption still applies.
    token, busy_for = _begin_encoder_action(dev_id, direction, action,
                                            ignore_cooldown=True)
    if token is None:
        return jsonify({"ok": False, **_err(t("log.action_still_running_dir")),
                        "running": True, "elapsed": round(busy_for, 1)}), 200

    result_queue: "queue.Queue" = queue.Queue()

    def _worker():
        try:
            result_queue.put(run_action(action, dev_id))
        except Exception as e:  # noqa: BLE001
            result_queue.put({"ok": False, "error": f"{type(e).__name__}: {e}",
                              "action_type": action.get("type", ""),
                              "target": _action_display_target(action)})
        finally:
            _end_encoder_action(dev_id, direction, token)

    worker = threading.Thread(target=_worker, daemon=True)
    _bind_encoder_action_thread(dev_id, direction, token, worker)
    try:
        worker.start()
    except Exception:  # noqa: BLE001
        _end_encoder_action(dev_id, direction, token)
        raise
    try:
        result = result_queue.get(timeout=5.0)
    except queue.Empty:
        # Still running: the worker owns the claim, so the card stays
        # "执行中" until the action finishes. Do NOT release here.
        return jsonify({"ok": False, **_err(t("log.action_still_running")), "running": True}), 200

    _end_encoder_action(dev_id, direction, token)
    _record_action_result(dev_id, -1, action, result,
                          label=_encoder_action_label(direction))
    return jsonify({"ok": result["ok"], "error": result["error"],
                    "browser_used": result.get("browser_used", "")})


@app.route("/api/device/<dev_id>/<action>", methods=["POST"])
def api_device_action(dev_id, action):
    if action == "reboot":
        return jsonify(send(dev_id, "CMD:REBOOT"))
    if action == "portal":
        return jsonify(send(dev_id, "CMD:BOOT_CFG"))
    if action == "refresh":
        # WS-409: the firmware/bond info is refreshed as before, but the
        # config read-back is *awaited* — the answer carries the device's
        # real values so the UI can redraw them.  A device that does not
        # answer at all leaves the stored config untouched.
        send(dev_id, "CMD:VERSION")
        send(dev_id, "CMD:GET_BOND")   # WS-321
        return jsonify(read_device_config(dev_id))
    if action == "ping":
        _ping_sent[dev_id] = time.time()
        return jsonify(send(dev_id, "CMD:PING"))
    if action == "clearbond":
        # WS-321: drop the persisted host binding so a new central can become
        # the HID primary. The device ACKs on the same CFG channel.
        return jsonify(send(dev_id, "CMD:CLEARBOND"))
    return jsonify({"ok": False, "error": "unknown action"}), 404


@app.route("/api/device/<dev_id>/sysinfo", methods=["POST"])
def api_device_sysinfo(dev_id):
    """WS-445: sample one deck's uptime / reset reason / NVS / free heap.

    Kept as its own route (not an `action`) because it *waits* for the answer
    and returns the rendered card data; the generic device actions only fire
    and forget.
    """
    return jsonify(read_sysinfo(dev_id))


@app.route("/api/device/<dev_id>/layout", methods=["POST"])
def api_device_layout(dev_id):
    """WS-499: ask the deck which layout it runs.

    The UI uses this to hide/disable the 12-key choice on firmware that
    predates ``CFG:LAYOUT`` (「固件不支持」) instead of letting the user pick
    something the apply step would have to refuse. ``supported: None`` means
    "the link was down, verdict unknown".
    """
    cfg = device_cfg(dev_id)
    probe = probe_layout(dev_id)
    with _lock:
        if probe["sent"] and not probe["answered"]:
            cfg["layout_supported"] = False
            save_config()
        elif probe["answered"]:
            cfg["layout_supported"] = True
            save_config()
        supported = cfg.get("layout_supported")
        payload = {"dev_id": dev_id,
                   "supported": None if supported is None and not probe["sent"]
                                else bool(supported),
                   "layout": layout_of(cfg),
                   "sent": probe["sent"]}
    return jsonify(payload)


@app.route("/api/device/<dev_id>/mods", methods=["POST"])
def api_device_mods(dev_id):
    """WS-546: ask the deck whether it supports per-input modifiers.

    The answer is a capability fact, never a version comparison. ``supported``
    is None only when the link was down (verdict unknown); True / False is
    cached in the device config so the drawer does not re-probe on every open.
    """
    cfg = device_cfg(dev_id)
    probe = probe_mods(dev_id)
    with _lock:
        if probe["sent"] and not probe["answered"]:
            cfg["mods_supported"] = False
            save_config()
        elif probe["answered"]:
            cfg["mods_supported"] = True
            save_config()
        supported = cfg.get("mods_supported")
        payload = {"dev_id": dev_id,
                   "supported": None if supported is None and not probe["sent"]
                                else bool(supported),
                   "sent": probe["sent"]}
    return jsonify(payload)


# --- OTA (host-driven firmware push) --------------------------------------
def _ota_validate(filename: str) -> str:
    """Return an error string when *filename* is not an app-only OTA image.

    The firmware's Update framework only flashes a single app image; a
    merged整片包 (bootloader + partitions + otadata + app) is rejected by
    Update.end() anyway, but the host catches the obvious case up front so
    the user gets a clear message instead of a silent flash failure.
    """
    import re as _re
    if not filename:
        return t("error.ota_no_file")
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # Accept the release naming (ota_binddecklite_c3_v1.3.0.bin) — dots are
    # part of the version, so do not restrict the stem to word characters.
    if not _re.fullmatch(r"ota_[^/\\]+\.bin", base, _re.IGNORECASE):
        return t("error.ota_filename_rule")
    return ""


# OTA slot capacity (bytes) by flash size, matching partitions/*.csv:
#   16 MB → 3 MB app slot, 4 MB → ~1.94 MB slot, 2 MB → no app1 slot at all.
# Unknown flash size (older firmware that does not report the 5th VERSION
# field) defaults to the 3 MB cap so a genuine 16 MB image is never wrongly
# rejected; a 4 MB board that exceeds its own slot is still caught by the
# device's Update.begin(). A known 2 MB board (2048) returns 0: its table has
# no app1, so host OTA is simply not possible — the caller must surface that.
def _ota_slot_cap(fw: dict) -> int:
    flash_kb = int((fw or {}).get("flash") or 0)
    if flash_kb == 0:
        return 3 * 1024 * 1024          # unknown (old firmware): conservative 3 MB
    if flash_kb >= 16000:
        return 3 * 1024 * 1024          # 16 MB table: app1 @ 0x310000, 0x300000
    if flash_kb >= 4000:
        return 0x1F0000                 # 4 MB table: app1 @ 0x200000, 0x1F0000
    return 0                             # 2 MB table: no app1, no host OTA


# ESP-IDF app image layout (esp_image_header_t, 24-byte header):
#   [0]   magic (0xE9)
#   [1]   segment count
#   then per segment: 4-byte load addr + 4-byte length + <length> bytes
#   then 1-byte XOR checksum
#   then (optionally) a 32-byte SHA256 over everything before it
# The SHA256 is always the final 32 bytes of a correctly built app image and
# covers all preceding bytes; we verify it directly rather than trusting the
# image's own hash_appended flag (which is 0 on some toolchain outputs even
# though the hash is present). A merged whole-flash image starts with the
# bootloader, so offset 0x20 is NOT the app descriptor (0xABCD5432); that is
# the reliable app-only test.
def _ota_validate_image(file_data: bytes, slot_cap: int) -> str:
    """Return "" if *file_data* is a valid app-only OTA image that fits the
    target slot, otherwise a user-facing, actionable error string."""
    n = len(file_data)
    if n < 0x30:
        return t("error.ota_too_small")
    if file_data[0] != 0xE9:
        return t("error.ota_not_esp_image")
    # app descriptor magic at 0x20 — only an app image has it there.
    if file_data[0x20:0x24] != b"\x32\x54\xcd\xab":
        return t("error.ota_merged_pack")
    # Walk the segments to (a) reject a corrupt/truncated segment table and
    # (b) compute the declared image length (header + segments + checksum).
    # seg_count is one byte (0-255), so the loop is bounded; a corrupt count
    # is caught by the pos+8 > n check below rather than silently truncated.
    try:
        seg_count = file_data[1]
        pos = 24  # ESP_IMAGE_HEADER_LEN
        for _ in range(seg_count):
            if pos + 8 > n:
                return t("error.ota_segment_table")
            seg_len = int.from_bytes(file_data[pos + 4:pos + 8], "little")
            if seg_len > n or pos + 8 + seg_len > n:
                return t("error.ota_segment_bounds")
            pos += 8 + seg_len
        core_len = pos + 1  # + trailing checksum byte
    except Exception:
        return t("error.ota_bad_structure")

    # The image may carry an appended 32-byte SHA256 (the ESP-IDF toolchain rule,
    # flagged by bit 0 of byte 23). When the flag is set the hash MUST be the
    # final 32 bytes and MUST match everything before it — a truncated image
    # that drops those bytes would otherwise slip through the lenient
    # segment-walk fallback below (d[:-32] / d[:-33] / d[:-43] all passed).
    # When the flag is clear some toolchains omit the hash entirely, and we
    # fall back to the segment walk with its 16-byte alignment tolerance.
    hash_appended = (n > 23) and (file_data[23] & 1)
    if hash_appended:
        if n < core_len + 32 or \
                file_data[n - 32:n] != hashlib.sha256(file_data[:n - 32]).digest():
            return t("error.ota_truncated")
        # valid image with appended hash; alignment padding before the hash is
        # covered by the hash itself, so nothing more to check here.
    else:
        # No appended hash: rely on the segment walk. The file must be at
        # least core_len, with at most 16 bytes of alignment padding; anything
        # else is corrupt/truncated.
        if n < core_len:
            return t("error.ota_size_short") % (n, core_len)
        if n > core_len + 16:
            return t("error.ota_size_suspect") % core_len

    if n > slot_cap:
        if slot_cap >= 1024 * 1024:
            cap_txt = t("ui.megabytes") % (slot_cap // (1024 * 1024))
        else:
            cap_txt = "%d KB" % (slot_cap // 1024)
        return t("error.ota_slot_overflow") % (n, cap_txt)
    return ""


def _ota_version_from_name(filename: str) -> str:
    """Best-effort version from the release filename.

    ``ota_binddecklite_c3_v1.3.0.bin`` → ``1.3.0``. The esp_app_desc_t version
    field is a build hash under pioarduino (the descriptor comes from the
    prebuilt Arduino core), so the filename is the reliable source.
    """
    import re as _re
    base = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    m = _re.search(r"_v([0-9][0-9A-Za-z.\-]*)\.bin$", base, _re.IGNORECASE)
    return m.group(1) if m else ""


# Device-side rejection reasons → what the user should do about them.
_OTA_ERROR_TEXT = {
    "timeout": t("error.ota_rx_timeout"),
    "not_app_image": t("error.ota_reject_not_app"),
    "not_bound": t("error.ota_reject_unbound"),
    "size": t("error.ota_reject_length"),
    "hash": t("error.ota_reject_sha"),
    "args": t("error.ota_reject_params"),
    "begin_failed": t("error.ota_reject_capacity"),
    "write_failed": t("error.ota_write_failed"),
    "end_failed": t("error.ota_end_failed_stale"),
    "busy": t("error.ota_busy_device"),
    "config_mode": t("error.ota_provisioning"),
    "chunk_too_big": t("error.ota_block_too_big"),
    "not_active": t("error.ota_no_session"),
}


def _ota_error_text(ack: str) -> str:
    if not ack.startswith("ACK:OTA:ERROR:"):
        return ack
    reason = ack[len("ACK:OTA:ERROR:"):]
    # end_failed:<detail>(code) — the device now attaches the concrete
    # Update error. On this device Update::end() only fails at _partitionIsBootable
    # (read_error=3) or esp_ota_set_boot_partition (activate=9), i.e. a device-side
    # flash/activation problem, NOT a corrupt image (the host already proved the
    # bytes match by passing the size+SHA256 gate). Keep the bare "end_failed"
    # form working for old firmware.
    if reason == "end_failed" or reason.startswith("end_failed:"):
        detail = reason[len("end_failed:"):] if reason.startswith("end_failed:") else ""
        if detail:
            guidance = ""
            if "read_error" in detail:
                guidance = t("error.ota_guidance_read_error")
            elif "activate" in detail:
                guidance = t("error.ota_guidance_activate")
            return t("error.ota_end_failed_detail", detail=detail, guidance=guidance)
        return _OTA_ERROR_TEXT.get("end_failed", t("error.ota_end_failed"))
    return _OTA_ERROR_TEXT.get(reason, t("error.ota_rejected_detail", reason=reason))


def _wait_until(pred, timeout: float) -> bool:
    """Poll *pred* every 20 ms until true or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _ota_current_fw(dev_id: str) -> dict:
    with _lock:
        cfg = _config["devices"].get(dev_id) or {}
    fw = cfg.get("fw") or {}
    return {"version": fw.get("version", ""), "build": fw.get("build", ""),
            "flash": fw.get("flash", 0)}


def _ota_is_connected(dev_id: str) -> bool:
    try:
        for d in ble.devices():
            if (d.get("dev_id") or "") == dev_id:
                return bool(d.get("connected"))
    except Exception:  # noqa: BLE001
        pass
    return False


def _ota_wait_restart(dev_id: str, old_version: str, old_build: str,
                      expected_version: str, timeout: float):
    """Confirm the deck really came back on the new image.

    Returns ``(ok, note)``. Success requires the firmware version or build
    timestamp reported through ``VERSION:`` to change, or the version to match
    the release filename — a BLE reconnect alone is not proof. If neither
    changes but the link dropped and came back, the device did reboot (same
    build re-flashed) and that is reported as success with a note.
    """
    deadline = time.monotonic() + timeout
    saw_down = False
    while time.monotonic() < deadline:
        fw = _ota_current_fw(dev_id)
        if not _ota_is_connected(dev_id):
            saw_down = True
        if expected_version and fw["version"] == expected_version \
                and (fw["version"] != old_version
                     or (fw["build"] and fw["build"] != old_build)):
            return True, ""
        if fw["version"] and fw["version"] != old_version:
            return True, ""
        if old_build and fw["build"] and fw["build"] != old_build:
            return True, ""
        # Nudge the device once the link is back; silent while it reboots.
        ble.send(dev_id, "CMD:VERSION")
        time.sleep(2.0)

    fw = _ota_current_fw(dev_id)
    if expected_version and fw["version"] == expected_version and fw["version"] != old_version:
        return True, ""
    if saw_down and fw["version"]:
        return True, t("log.ota_same_version")
    return False, ""


def _ota_push(dev_id: str, filename: str, file_data: bytes,
              sha_hex: str, expected_version: str,
              old_version: str = "", old_build: str = ""):
    """Run one OTA push on a background thread.

    Returns the OtaWindow; poll ``/api/ota/<dev_id>/progress`` for status.
    """
    window = OtaWindow(dev_id, filename, len(file_data))
    with _ota_lock:
        _ota_windows[dev_id] = window

    def _fail(message: str, level: str = "error"):
        window.status = "error"
        window.error = message
        add_event("ota", t("log.ota_failed", filename=filename, message=message), dev_id, level)

    def _run():
        total = window.total
        add_event("ota", t("log.ota_started", filename=filename, total=total), dev_id)

        # 1. CMD:OTA:<size>:<sha256> — arm the device. OTA is BLE-only: the
        #    CFG characteristic switches to binary mode and the WiFi UDP path
        #    has no OTA channel, so never fall back to it.
        if not ble.send(dev_id, f"CMD:OTA:{total}:{sha_hex}"):
            _fail(t("log.not_connected"))
            return
        if not _wait_until(lambda: window.ack_line or window.ack_error, OTA_CMD_TIMEOUT):
            _fail(t("error.ota_fw_too_old"))
            return
        ack = window.ack_line
        if ack.startswith("ACK:OTA:ERROR"):
            _fail(_ota_error_text(ack))
            return

        # 2. Pipelined push. ``sent`` may run up to OTA_WINDOW_BYTES ahead of
        #    the device's confirmed ``acked`` offset; only then do we block.
        #    Chunks follow the negotiated MTU (never below OTA_CHUNK_MIN) so a
        #    large MTU is not wasted on 200-byte writes.
        window.status = "uploading"
        chunk_size = min(OTA_CHUNK_MAX,
                         max(OTA_CHUNK_MIN, ble.max_payload(dev_id, OTA_CHUNK_MIN)))
        add_event("ota", t("log.ota_chunk_size", size=chunk_size), dev_id)
        sent = 0
        while sent < total:
            if window.status == "cancelled":
                return
            err = window.ack_error
            if err:
                _fail(_ota_error_text(err))
                return
            if sent - window.acked >= OTA_WINDOW_BYTES:
                if not _wait_until(
                        lambda: (sent - window.acked) < OTA_WINDOW_BYTES
                                or bool(window.ack_error)
                                or window.status == "cancelled", OTA_ACK_TIMEOUT):
                    _fail(t("error.ota_push_timeout"))
                    return
                continue
            chunk = file_data[sent:sent + chunk_size]
            if not ble.send_raw(dev_id, chunk):
                _fail(t("error.ota_link_lost"))
                return
            sent += len(chunk)
            window.sent = sent
        if window.status == "cancelled":
            return

        # Wait for the device to confirm every byte before finalizing.
        if not _wait_until(lambda: window.acked >= total or bool(window.ack_error)
                                   or window.status == "cancelled",
                           OTA_ACK_TIMEOUT):
            _fail(t("error.ota_push_timeout"))
            return
        if window.status == "cancelled":
            return
        err = window.ack_error
        if err:
            _fail(_ota_error_text(err))
            return

        # 3. CMD:OTA_END — device verifies size + SHA256, then Update.end()
        #    and reboots. The ACK may be lost to the reset, so a timeout here
        #    is not fatal; step 4 decides.
        window.phase = "restart"
        if not ble.send(dev_id, "CMD:OTA_END"):
            _fail(t("error.ota_link_lost"))
            return
        _wait_until(lambda: window.ack_line == "ACK:OTA:DONE"
                            or bool(window.ack_error)
                            or window.status == "cancelled",
                    OTA_END_TIMEOUT)
        if window.status == "cancelled":
            return
        err = window.ack_error
        if err:
            # Device-side flash failure (end_failed): append concrete next steps
            # so the user is not left with a bare "verify failed" message.
            if err.startswith("ACK:OTA:ERROR:end_failed"):
                detail = err[len("ACK:OTA:ERROR:end_failed:"):]
                add_event("ota", t("log.ota_end_failed_advice"), dev_id, "warn")
            _fail(_ota_error_text(err))
            return

        # 4. Prove the deck actually came back on the image we pushed instead
        #    of trusting a BLE reconnect: compare the version / build stamp
        #    reported by VERSION: against the values captured before the push.
        window.phase = "confirm"
        ok, note = _ota_wait_restart(dev_id, old_version, old_build,
                                     expected_version, OTA_VERIFY_TIMEOUT)
        if not ok:
            _fail(t("error.ota_verify_timeout", seconds=int(OTA_VERIFY_TIMEOUT),
                    expected=expected_version or t("log.ota_new_build")))
            return

        window.status = "done"
        got = _ota_current_fw(dev_id).get("version") or expected_version or "?"
        done = t("log.ota_done", filename=filename, total=total, version=got)
        if note:
            done += t("log.ota_done_note", note=note)
        add_event("ota", done, dev_id, "ok")

    threading.Thread(target=_run, daemon=True, name=f"ota-{dev_id}").start()
    return window


@app.route("/api/ota/<dev_id>", methods=["POST"])
def api_ota(dev_id):
    """Start a host-driven OTA push for one deck.

    Accepts a multipart ``file`` field (the app-only ``ota_*.bin``). Returns
    immediately with a ``task`` id; poll ``/api/ota/<dev_id>/progress``.
    """
    if "file" not in request.files:
        return jsonify({"ok": False, **_err(t("error.ota_no_upload"))}), 400
    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"ok": False, **_err(t("error.ota_no_file"))}), 400

    err = _ota_validate(upload.filename)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    file_data = upload.read()
    if not file_data:
        return jsonify({"ok": False, **_err(t("error.ota_empty_file"))}), 400

    # Pre-flight the image locally so a corrupt / truncated / wrong-kind file is
    # rejected before we spend 1-3 minutes pushing it over BLE. The firmware's
    # Update.end() still verifies the image itself — this is a fast gate.
    fw = _ota_current_fw(dev_id)
    slot_cap = _ota_slot_cap(fw)
    if slot_cap <= 0:
        return jsonify({"ok": False, **_err(t("error.ota_no_app1"))}), 400
    img_err = _ota_validate_image(file_data, slot_cap)
    if img_err:
        return jsonify({"ok": False, "error": img_err}), 400

    with _ota_lock:
        existing = _ota_windows.get(dev_id)
        if existing is not None and existing.status in ("starting", "uploading"):
            return jsonify({"ok": False, **_err(t("error.ota_busy_task"))}), 409

    # The device verifies both values before Update.end(), so a truncated or
    # tampered file can never replace the running firmware.
    sha_hex = hashlib.sha256(file_data).hexdigest()
    version = _ota_version_from_name(upload.filename)
    old = _ota_current_fw(dev_id)
    window = _ota_push(dev_id, upload.filename, file_data, sha_hex, version,
                       old_version=old["version"], old_build=old["build"])
    return jsonify({"ok": True, "task": dev_id, "filename": window.filename,
                    "total": window.total, "sha256": sha_hex, "version": version})


@app.route("/api/ota/<dev_id>/cancel", methods=["POST"])
def api_ota_cancel(dev_id):
    """Cancel an in-flight OTA and tell the device to drop the partial image.

    The push thread stops on its next loop check; the device aborts its own
    session on CMD:OTA_CANCEL (or on the 30 s inter-chunk timeout if the
    command cannot be delivered).
    """
    with _ota_lock:
        window = _ota_windows.get(dev_id)
        if window is None:
            return jsonify({"ok": False, **_err(t("error.ota_no_task"))}), 404
        if window.status in ("done", "error", "cancelled"):
            _ota_windows.pop(dev_id, None)
            return jsonify({"ok": True, "sent": False, "status": window.status})
        window.status = "cancelled"
        window.error = t("log.ota_cancelled_short")

    sent = ble.send(dev_id, "CMD:OTA_CANCEL")
    add_event("ota", t("log.ota_cancelled") + ("" if sent else t("log.ota_cancel_undelivered")),
              dev_id, "warn")
    return jsonify({"ok": True, "sent": bool(sent), "status": "cancelled"})


@app.route("/api/ota/<dev_id>/progress")
def api_ota_progress(dev_id):
    with _ota_lock:
        window = _ota_windows.get(dev_id)
        if window is None:
            return jsonify({"ok": False, **_err(t("error.ota_no_task"))}), 404
        # Clean up finished / errored windows after one last read so the poller
        # can detect completion by the task disappearing.
        done = window.status in ("done", "error", "cancelled")
        payload = {
            "ok": True,
            "status": window.status,
            "sent": window.sent,
            # Progress is what the DEVICE confirmed, not what we queued.
            "acked": window.acked,
            "received": window.acked,
            "total": window.total,
            "percent": window.percent(),
            "error": window.error,
            "filename": window.filename,
        }
        if done:
            _ota_windows.pop(dev_id, None)
    return jsonify(payload)


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json(silent=True) or {}
    with _lock:
        for k in ("actions_enabled", "udp_fallback"):
            if k in data:
                _config["settings"][k] = bool(data[k])
        save_config()
    return jsonify({"ok": True, "settings": _config["settings"]})


# --- online update (WS-523) -------------------------------------------------
# 上位机自更新（换 exe）+ 固件 OTA 在线检测/推送。逻辑与网络部分在
# online_update.py，这里只做路由、状态与 UI 数据组装。
_update_lock = threading.RLock()
#: 上位机自更新任务：task id -> 进度 dict（phase / downloaded / total / …）
_app_update_tasks: dict = {}
#: 固件在线更新任务：dev_id -> 进度 dict（复用现有 OTA 进度接口的窗口）
_fw_update_tasks: dict = {}

#: 上位机更新任务的终态 —— 到了这些状态就不再算「有任务在跑」。
_APP_UPDATE_DONE_PHASES = ("error", "restarting", "done")


def _update_settings() -> dict:
    """config.json 里的在线更新状态（顶层键）。"""
    with _lock:
        return {
            "skipped_version": str(_config.get("skipped_version") or ""),
            "last_update": dict(_config.get("last_update") or {}),
            "update_fail_count": int(_config.get("update_fail_count") or 0),
            "update_channel": online_update.normalize_channel(
                _config.get("update_channel")),
        }


def _local_rolling_tag(state: dict) -> str:
    """本机上次成功装上的滚动 tag（用于「已是最新」判断）。

    回滚过的更新不算装上了 —— 那个 tag 的 exe 并不在本机。
    """
    last = state.get("last_update") or {}
    if last.get("result") == "rollback":
        return ""
    return str(last.get("tag") or "")


def _set_update_state(**fields):
    """写在线更新状态并落盘。"""
    with _lock:
        for key, value in fields.items():
            _config[key] = value
        save_config()


@app.route("/api/app/version")
def api_app_version():
    """上位机版本与可更新性信息（前端顶栏显示用）。"""
    return jsonify({
        "version": online_update.read_app_version(),
        "build": online_update.read_app_build(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable if getattr(sys, "frozen", False) else "",
        "update_fail_count": _update_settings()["update_fail_count"],
        "update_channel": _update_settings()["update_channel"],
    })


def _app_version_payload() -> dict:
    """上位机版本信息（``/api/status`` 与 ``/api/app/version`` 共用一份）。"""
    frozen = bool(getattr(sys, "frozen", False))
    return {
        "version": online_update.read_app_version(),
        "build": online_update.read_app_build(),
        "frozen": frozen,
        "executable": sys.executable if frozen else "",
        "update_fail_count": _update_settings()["update_fail_count"],
        "update_channel": _update_settings()["update_channel"],
    }


@app.route("/api/app/update/channel", methods=["POST"])
def api_app_update_channel():
    """切换上位机更新通道（stable / rolling），立即持久化。"""
    data = request.get_json(silent=True) or {}
    channel = str(data.get("channel") or "").strip().lower()
    if channel not in online_update.UPDATE_CHANNELS:
        return jsonify({"ok": False, **_err(t("error.update_channel_invalid"))}), 400
    _set_update_state(update_channel=channel)
    add_event("update",
              t("log.app_update_channel",
                channel=t("ui.update_channel_" + channel)),
              level="ok")
    return jsonify({"ok": True, "channel": channel})


@app.route("/api/app/update/check")
def api_app_update_check():
    """检查上位机是否有新版本（按当前通道）。

    返回 ``{current, latest, tag, channel, has_update, notes, …}``。
    ``latest`` 在滚动通道是 tag（``main-YYYYMMDD-HHMM``）；``skip_key`` 是
    跳过时该记录的标识（stable = 版本号，rolling = tag）。
    失败时返回可读的中文原因 + 可复制错误码 + Release 链接，不静默失败。
    """
    current = online_update.read_app_version()
    state = _update_settings()
    channel = state["update_channel"]
    payload = {
        "ok": True,
        "current": current,
        "latest": "",
        "tag": "",
        "channel": channel,
        "skip_key": "",
        "has_update": False,
        "notes": "",
        "published_at": "",
        "asset": {"name": "", "size": 0, "sha256": ""},
        "min_from_ok": True,
        "skipped": False,
        "source": "",
        "release_url": online_update.RELEASE_PAGE_URL,
        "error": "",
        "error_code": "",
    }
    try:
        info = online_update.resolve_latest_update(channel=channel)
    except online_update.UpdateError as exc:
        payload["ok"] = False
        payload["error"] = exc.message
        payload["error_code"] = exc.code
        return jsonify(payload)

    app_asset = info.get("app") or {}
    payload["source"] = info.get("source", "")
    payload["notes"] = info.get("notes", "")
    payload["published_at"] = info.get("published_at", "")
    payload["release_url"] = info.get("release_url", online_update.RELEASE_PAGE_URL)
    payload["tag"] = str(info.get("tag") or "")
    target_key = online_update.update_target_key(channel, info)
    payload["skip_key"] = target_key
    if not app_asset:
        payload["ok"] = False
        payload["error"] = t("error.app_update_no_asset")
        payload["error_code"] = "E_NO_APP_ASSET"
        return jsonify(payload)

    latest = app_asset["version"]
    # 滚动通道对用户有意义的是 tag（所有 nightly 的 semver 都一样）。
    payload["latest"] = payload["tag"] if channel == "rolling" else latest
    payload["asset"] = {
        "name": app_asset["asset"],
        "size": app_asset.get("size", 0),
        "sha256": app_asset.get("sha256", ""),
    }

    has_update = online_update.has_app_update(
        channel, info, current,
        local_build=online_update.read_app_build(),
        local_tag=_local_rolling_tag(state))

    # 有新版本时才检查「能不能升」—— 没有新版本就没有「不能升」这回事，
    # 也不该拿 SHA256/资产缺失去打扰已经是最新的用户。
    if has_update:
        # min_from：低于它的版本不能直接自动升级（需要先手动升到中间版本）。
        min_from = app_asset.get("min_from") or ""
        if not online_update.version_at_least(current, min_from):
            payload["min_from_ok"] = False
            payload["ok"] = False
            payload["error"] = (t("error.app_update_jump")
                                % (current, latest, min_from))
            payload["error_code"] = "E_MIN_FROM"
            return jsonify(payload)

        # 没有 SHA256 就不自动更新 —— 强制校验是硬性要求。
        if not app_asset.get("sha256"):
            payload["ok"] = False
            payload["error"] = t("error.app_update_no_sha")
            payload["error_code"] = "E_NO_SHA256"
            return jsonify(payload)

        # 清单里的资产必须真的在这次 Release 里（否则 apply 才会以 E_NO_URL 失败）。
        if not app_asset.get("url"):
            payload["ok"] = False
            payload["error"] = (t("error.app_update_asset_missing")
                                % app_asset.get("asset", ""))
            payload["error_code"] = "E_NO_APP_ASSET"
            return jsonify(payload)

    if online_update.should_skip_update(channel, target_key,
                                        state["skipped_version"]):
        payload["skipped"] = True
        payload["error"] = t("log.app_update_skipped", version=latest)
        payload["error_code"] = "E_SKIPPED"
        return jsonify(payload)

    payload["has_update"] = has_update
    return jsonify(payload)


@app.route("/api/app/update/apply", methods=["POST"])
def api_app_update_apply():
    """下载新版本并安排原地替换 + 重启。

    立即返回 ``{task}``；进度轮询 ``/api/app/update/progress?task=``。
    """
    with _update_lock:
        # 只有还在跑的任务才算「占用」—— 上一轮失败/结束的任务不该挡住下一次。
        running = [t for t in _app_update_tasks.values()
                   if t.get("phase") not in _APP_UPDATE_DONE_PHASES]
        if running:
            return jsonify({"ok": False, **_err(t("error.update_busy"))}), 409
        # 清掉已结束的任务，dict 不会无限增长。
        for task_id in [k for k, v in _app_update_tasks.items()
                        if v.get("phase") in _APP_UPDATE_DONE_PHASES]:
            _app_update_tasks.pop(task_id, None)

    state = _update_settings()
    channel = state["update_channel"]
    try:
        info = online_update.resolve_latest_update(channel=channel)
    except online_update.UpdateError as exc:
        return jsonify({"ok": False, "error": exc.message, "error_code": exc.code,
                        "release_url": online_update.RELEASE_PAGE_URL}), 503

    release_url = info.get("release_url", online_update.RELEASE_PAGE_URL)
    app_asset = info.get("app") or {}
    if not app_asset:
        return jsonify({"ok": False, **_err(t("error.app_update_no_asset")),
                        "error_code": "E_NO_APP_ASSET",
                        "release_url": release_url}), 409
    if not app_asset.get("sha256"):
        return jsonify({"ok": False, **_err(t("error.app_update_no_sha_short")),
                        "error_code": "E_NO_SHA256", "release_url": release_url}), 409
    if not app_asset.get("url"):
        return jsonify({"ok": False,
                        "error": (t("error.app_update_asset_missing")
                                  % app_asset.get("asset", "")),
                        "error_code": "E_NO_APP_ASSET", "release_url": release_url}), 409

    current = online_update.read_app_version()
    latest = app_asset["version"]
    tag = str(info.get("tag") or "")
    target_key = online_update.update_target_key(channel, info)
    shown = tag if channel == "rolling" else latest
    if online_update.should_skip_update(channel, target_key,
                                        state["skipped_version"]):
        return jsonify({"ok": False,
                        **_err(t("log.app_update_skipped_short", version=shown)),
                        "error_code": "E_SKIPPED", "release_url": release_url}), 409
    if not online_update.has_app_update(
            channel, info, current,
            local_build=online_update.read_app_build(),
            local_tag=_local_rolling_tag(state)):
        if channel == "rolling":
            return jsonify({"ok": False,
                            **_err(t("log.app_up_to_date_rolling", tag=tag or current)),
                            "release_url": release_url}), 409
        return jsonify({"ok": False, **_err(t("log.app_up_to_date", version=current)),
                        "release_url": release_url}), 409
    # min_from 与 check 阶段同一把尺：check 说不能升，apply 也不能升。
    min_from = app_asset.get("min_from") or ""
    if not online_update.version_at_least(current, min_from):
        return jsonify({"ok": False,
                        "error": (t("error.app_update_jump")
                                  % (current, shown, min_from)),
                        "error_code": "E_MIN_FROM", "release_url": release_url}), 409
    # 回滚失败计数只拦「同一个失败版本 / 同一个 nightly」：出现更高版本或新的
    # 滚动构建时放行并重新计数，否则 apply 把自己挡死 → 在线更新永久失效。
    fail_count = state["update_fail_count"]
    last_update = state.get("last_update") or {}
    failed_key = (str(last_update.get("target") or last_update.get("to") or "")
                  if last_update.get("result") == "rollback" else "")
    if fail_count >= online_update.UPDATE_FAIL_LIMIT:
        if failed_key and failed_key != target_key:
            _set_update_state(update_fail_count=0)
        else:
            return jsonify({"ok": False,
                            "error": (t("error.app_update_paused")
                                      % (failed_key or shown, fail_count)),
                            "error_code": "E_UPDATE_PAUSED",
                            "release_url": release_url}), 409

    # 退出上位机会断 BLE —— 设备正在 OTA 时绝不能重启上位机。
    busy = _ota_busy_reason()
    if busy:
        return jsonify({"ok": False, "error": busy, "error_code": "E_OTA_BUSY",
                        "release_url": release_url}), 409

    target_dir = _update_target_dir()
    if not target_dir:
        return jsonify({"ok": False,
                        **_err(t("error.app_update_no_target")),
                        "error_code": "E_NO_TARGET", "release_url": release_url}), 409
    if not online_update.is_writable_dir(target_dir):
        # 不破坏原 exe：明确提示手动下载，并给出 Release 链接。
        return jsonify({"ok": False,
                        **_err(t("error.app_update_dir_readonly")),
                        "error_code": "E_DIR_RO",
                        "release_url": release_url}), 409

    task_id = "app-%d" % int(time.time() * 1000)
    with _update_lock:
        _app_update_tasks[task_id] = {
            "phase": "starting", "downloaded": 0, "total": 0, "percent": 0,
            "error": "", "error_code": "",
        }
    threading.Thread(target=_app_update_worker,
                     args=(task_id, info, app_asset, current),
                     daemon=True, name="app-update").start()
    return jsonify({"ok": True, "task": task_id, "version": latest})


def _app_update_worker(task_id, info, app_asset, current):
    """后台：下载 → 校验 → 落状态 → 替换 → 重启。"""
    def _phase(phase, **fields):
        with _update_lock:
            task = _app_update_tasks.get(task_id)
            if task is None:
                return
            task.update(fields)
            task["phase"] = phase

    try:
        version = app_asset["version"]
        channel = online_update.normalize_channel(info.get("channel"))
        tag = str(info.get("tag") or "")
        # 滚动通道用 tag 做下载目录：所有 nightly 的 version 都是
        # 0.0.0+main.<sha>，tag 才是每次构建唯一的名字。
        dest_dir = online_update.update_dir(tag if channel == "rolling" else version)
        final_path = os.path.join(dest_dir, app_asset["asset"])

        _phase("downloading", total=app_asset.get("size", 0))
        def _progress(done, total):
            with _update_lock:
                task = _app_update_tasks.get(task_id)
                if task is None:
                    return
                task["downloaded"] = done
                task["total"] = total or task.get("total", 0)
                if task["total"]:
                    task["percent"] = min(100, int(done * 100 / task["total"]))

        # 边下边算 SHA256：校验时不用把文件再读一遍。
        hasher = hashlib.sha256()
        part_path = online_update.download_to_file(
            app_asset.get("url", ""), dest_dir, app_asset["asset"],
            progress=_progress, expected_size=app_asset.get("size", 0),
            hasher=hasher)
        _phase("verifying")
        error = online_update.finalize_download(part_path, final_path,
                                                app_asset.get("sha256", ""),
                                                app_asset.get("size", 0),
                                                digest=hasher.hexdigest())
        if error:
            _phase("error", error=error, error_code="E_SHA256")
            return

        # 先落盘状态：新版本启动后据此写 last_update；失败回滚也能记结果。
        # ``tag`` / ``target`` 只给滚动通道用（下次检查据此判「已是最新」和
        # 「同一个 nightly 连续失败」）。
        marker = os.path.join(dest_dir, "updated.marker")
        # WS-570: 替换脚本的日志。回滚后启动的旧版本读它，才能告诉用户
        # 具体卡在哪一步，而不是只有一句「更新失败」。
        log_path = online_update.update_log_path(dest_dir)
        _set_update_state(last_update={
            "status": "pending",
            "from": current,
            "to": version,
            "tag": tag,
            "target": online_update.update_target_key(channel, info),
            "at": time.time(),
            "marker": marker,
            "log": log_path,
        })

        _phase("installing")
        exe_path = _current_exe_path()
        backup_path = exe_path + ".bak"
        script_path = os.path.join(dest_dir, "update.bat")
        script = online_update.build_update_script(exe_path, final_path, backup_path,
                                                   marker, os.getpid(),
                                                   log_path=log_path)
        # 上一次失败留下的 marker 会让脚本 ``if exist "%MARKER%"`` 立刻判成功、
        # 跳过回滚等待 —— 生成脚本前先清掉。
        try:
            online_update.stage_update_script(script_path, script, marker)
        except online_update.UpdateError as exc:
            _phase("error", error=exc.message, error_code=exc.code)
            return
        if not online_update.launch_update_script(script_path):
            _phase("error", error=t("error.app_update_script_failed"),
                   error_code="E_LAUNCH")
            return

        # 退出上位机：脚本等本进程退出后才替换 exe。
        _phase("restarting")
        add_event("update", t("log.app_update_restarting", version=version), level="ok")
        time.sleep(0.6)
        _quit_for_update()
        # 进程通常在 _quit_for_update() 里就退出了；走到这里说明退出被拒，
        # 记终态，别让任务永远占着「已有更新任务在运行」。
        _phase("done")
    except online_update.UpdateError as exc:
        _phase("error", error=exc.message, error_code=exc.code)
    except Exception as exc:  # noqa: BLE001
        _phase("error", error=t("error.app_update_failed") % (exc,), error_code="E_INTERNAL")


def _quit_for_update():
    """退出上位机，让更新脚本接管 exe 替换。

    先走既有的优雅退出路径（``_exit_for_restart``：关窗口、停托盘、收服务器），
    让 BLE 与托盘正常清理；只有在它没能真正结束进程时才强制退出 ——
    更新脚本在等本进程退出，不退出 exe 就替换不了。
    """
    try:
        _exit_for_restart()
    except Exception:  # noqa: BLE001
        pass
    # 给优雅退出留一点时间（0.4 s 的响应缓冲 + 清理）。
    for _ in range(30):
        time.sleep(0.1)
        if not _process_alive():
            return
    # 还没退出：强制结束。更新必须真的把 exe 换掉。
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(0)


def _process_alive() -> bool:
    """宿主是否还在正常跑（更新脚本在等本进程退出）。

    不能用 ``os.kill(getpid(), 0)``：那问的是「信号能否送到本进程」，
    自己给自己发信号永远成功，恒为真 —— 于是这段等待永远白等 3 秒，
    然后直接 ``os._exit(0)``。真正能问的是：主线程结束、且没有别的非
    daemon 线程拖住进程时，解释器就会自行退出。
    """
    if threading.main_thread().is_alive():
        return True
    for thread in threading.enumerate():
        if thread is threading.current_thread():
            continue
        if not thread.daemon:
            return True
    return False


def _current_exe_path() -> str:
    """当前运行的 exe 路径（未打包时返回空串）。"""
    if not getattr(sys, "frozen", False):
        return ""
    return os.path.abspath(sys.executable)


def _update_target_dir() -> str:
    """exe 所在目录（替换目标）；未打包时返回空串。"""
    exe = _current_exe_path()
    return os.path.dirname(exe) if exe else ""


def _ota_busy_reason() -> str:
    """有 OTA 在跑时返回原因，空串表示空闲。

    上位机更新要退出进程 → 会断 BLE；设备正在升级时绝不能重启上位机。

    两种判据都要看：

    * ``_ota_windows`` 里还有 ``starting`` / ``uploading`` 的窗口 → 推送在跑；
    * ``_ota_lock`` 被**别的线程**占着 → 推送线程正在临界区里。

    锁必须用非阻塞方式试取：RLock 对同一线程可重入，阻塞取锁会把「忙」
    变成「卡住」。
    """
    if not _ota_lock.acquire(blocking=False):
        return t("error.ota_busy_updating")
    try:
        for window in _ota_windows.values():
            if window.status in ("starting", "uploading"):
                return t("error.ota_busy_updating")
    finally:
        _ota_lock.release()
    return ""


def _update_failure_detail(last: dict) -> dict:
    """读上次更新脚本写的 ``update.log``（WS-570）。

    脚本在旧版本被拉起来之前就把每一步写进了日志，所以这里是唯一还能拿到
    「为什么失败」的地方。``last_update.log`` 是本次写入的路径；老配置没有
    这个键时按 marker 的上级目录推（``updates/update.log``）。
    """
    log_path = str(last.get("log") or "")
    if not log_path:
        marker_dir = os.path.dirname(str(last.get("marker") or ""))
        if marker_dir:
            log_path = online_update.update_log_path(marker_dir)
    if not log_path:
        return {"result": "", "reason": "", "detail": "", "lines": []}
    return online_update.read_update_log(log_path)


def _update_after_start():
    """启动后处理上次自更新的结果（WS-523）。

    * 首次启动的是**新版本**（当前版本 == last_update.to）→ 写标记文件，
      更新脚本看到它就确认成功；同时把 last_update.result 记为 ok。
    * 首次启动的是**旧版本**（更新脚本回滚了 .bak）→ 结果记为 rollback，
      连续失败计数 +1；连续失败 >= 2 次后暂停自动更新。
    * WS-570：回滚时再读 ``update.log``，把失败步骤与原始输出写进日志面板 ——
      用户以前只看到「更新失败」，完全无从排查。
    """
    state = _update_settings()
    last = state["last_update"]
    if not last or last.get("status") != "pending":
        return
    current = online_update.read_app_version()
    target = str(last.get("to") or "")
    marker = str(last.get("marker") or "")
    if target and online_update.same_app_version(current, target):
        if marker:
            try:
                with open(marker, "w", encoding="utf-8") as handle:
                    handle.write(current)
            except OSError:
                pass
        # 成功一次就把失败计数清零 —— 否则「失败两次 → 暂停」会永远停着。
        _set_update_state(last_update={**last, "status": "done", "result": "ok",
                                        "finished_at": time.time()},
                          update_fail_count=0)
        add_event("update", t("log.app_updated", version=current), level="ok")
        return
    # 回到旧版本 = 更新脚本回滚了。
    fail_count = state["update_fail_count"] + 1
    _set_update_state(last_update={**last, "status": "done", "result": "rollback",
                                    "finished_at": time.time()},
                      update_fail_count=fail_count)
    if fail_count >= online_update.UPDATE_FAIL_LIMIT:
        add_event("update", t("log.app_update_failed_paused", count=fail_count), level="warn")
    else:
        add_event("update", t("log.app_update_rolled_back"), level="warn")
    # 具体失败步骤：脚本日志里最后一条失败行（含 copy 的原始输出）。
    info = _update_failure_detail(last)
    if info.get("reason") or info.get("detail"):
        detail = str(info.get("detail") or "")[:300]
        reason = str(info.get("reason") or info.get("result") or "-")
        message = t("log.app_update_script_failed", reason=reason, detail=detail)
        add_event("update", message, level="error")
        # 冻结的 exe 里 stdout 就是 host.log（desktop.setup_stdio），
        # 用户反馈时能直接从 host.log 拿到同一行。
        try:
            print("[update] %s" % message)
        except Exception:  # noqa: BLE001
            pass


@app.route("/api/app/update/progress")
def api_app_update_progress():
    task_id = request.args.get("task") or ""
    with _update_lock:
        task = _app_update_tasks.get(task_id)
        if task is None:
            return jsonify({"ok": False, **_err(t("error.no_update_task"))}), 404
        payload = dict(task)
    payload["ok"] = True
    return jsonify(payload)


@app.route("/api/app/update/skip", methods=["POST"])
def api_app_update_skip():
    """跳过某个版本（写 config.json 的 skipped_version）。"""
    data = request.get_json(silent=True) or {}
    version = str(data.get("version") or "").strip()
    if not version:
        return jsonify({"ok": False, **_err(t("error.missing_version"))}), 400
    _set_update_state(skipped_version=version)
    return jsonify({"ok": True, "skipped_version": version})


@app.route("/api/app/update/cancel", methods=["POST"])
def api_app_update_cancel():
    """取消进行中的上位机更新（仅在下载阶段有效）。"""
    with _update_lock:
        if not _app_update_tasks:
            return jsonify({"ok": False, **_err(t("error.no_update_task"))}), 404
        for task in _app_update_tasks.values():
            task["cancelled"] = True
        _app_update_tasks.clear()
    return jsonify({"ok": True})


# --- firmware online update (WS-523) ---------------------------------------

@app.route("/api/firmware/update/check")
def api_firmware_update_check():
    """检查某个设备的在线固件版本。

    返回 ``{device_variant, current, latest, has_update, asset, notes, reason}``。
    """
    dev_id = request.args.get("dev_id") or ""
    if not dev_id:
        return jsonify({"ok": False, **_err(t("error.missing_dev_id"))}), 400

    with _lock:
        cfg = _config["devices"].get(dev_id) or {}
    fw = cfg.get("fw") or {}
    current = str(fw.get("version") or "")
    chip = str(fw.get("chip") or cfg.get("chip") or "")
    flash = fw.get("flash", 0)

    payload = {
        "ok": True,
        "dev_id": dev_id,
        "device_variant": "",
        "current": current,
        "latest": "",
        "has_update": False,
        "asset": {"name": "", "size": 0, "sha256": ""},
        "notes": "",
        "published_at": "",
        "release_url": online_update.RELEASE_PAGE_URL,
        "reason": "",
    }

    # 型号判定必须同时看 chip 与 flash —— 只看 flash 会把经典 ESP32 误判成 C3。
    variant = online_update.variant_for_device(chip, flash)
    if not variant:
        payload["reason"] = t("error.fw_no_online")
        payload["reason_code"] = "E_NO_VARIANT"
        return jsonify(payload)
    payload["device_variant"] = variant

    try:
        info = online_update.resolve_latest_update()
    except online_update.UpdateError as exc:
        payload["ok"] = False
        payload["reason"] = exc.message
        payload["reason_code"] = exc.code
        payload["release_url"] = online_update.RELEASE_PAGE_URL
        return jsonify(payload)

    payload["notes"] = info.get("notes", "")
    payload["published_at"] = info.get("published_at", "")
    payload["release_url"] = info.get("release_url", online_update.RELEASE_PAGE_URL)

    entry = (info.get("firmware") or {}).get(variant)
    if not entry:
        payload["reason"] = t("error.fw_no_online")
        payload["reason_code"] = "E_NO_VARIANT"
        return jsonify(payload)

    payload["latest"] = entry["version"]
    payload["asset"] = {
        "name": entry["asset"],
        "size": entry.get("size", 0),
        "sha256": entry.get("sha256", ""),
    }
    if not entry.get("sha256"):
        payload["reason"] = t("error.fw_no_sha")
        payload["reason_code"] = "E_NO_SHA256"
        return jsonify(payload)
    # 清单里的固件必须真的在这次 Release 里 —— 否则 apply 才会以
    # E_NO_URL 失败，用户白等一次下载。
    if not entry.get("url"):
        payload["reason"] = (t("error.fw_asset_missing")
                             % entry.get("asset", ""))
        payload["reason_code"] = "E_NO_FW_ASSET"
        return jsonify(payload)

    # 远端 ≤ 当前 → 已是最新，不提供更新（也不做降级/重装）。
    payload["has_update"] = online_update.version_is_newer(entry["version"], current)
    return jsonify(payload)


@app.route("/api/firmware/update/apply", methods=["POST"])
def api_firmware_update_apply():
    """下载在线固件并推送给设备（复用现有 OTA 通道与进度接口）。"""
    data = request.get_json(silent=True) or {}
    dev_id = str(data.get("dev_id") or "")
    if not dev_id:
        return jsonify({"ok": False, **_err(t("error.missing_dev_id"))}), 400

    busy = _ota_busy_reason()
    if busy:
        return jsonify({"ok": False, "error": busy, "error_code": "E_OTA_BUSY"}), 409

    with _lock:
        cfg = _config["devices"].get(dev_id) or {}
    fw = cfg.get("fw") or {}
    chip = str(fw.get("chip") or cfg.get("chip") or "")
    flash = fw.get("flash", 0)
    variant = online_update.variant_for_device(chip, flash)
    if not variant:
        return jsonify({"ok": False, **_err(t("error.fw_no_online")),
                        "error_code": "E_NO_VARIANT"}), 400

    try:
        info = online_update.resolve_latest_update()
    except online_update.UpdateError as exc:
        return jsonify({"ok": False, "error": exc.message, "error_code": exc.code,
                        "release_url": online_update.RELEASE_PAGE_URL}), 503

    release_url = info.get("release_url", online_update.RELEASE_PAGE_URL)
    entry = (info.get("firmware") or {}).get(variant)
    if not entry or not entry.get("sha256"):
        return jsonify({"ok": False, **_err(t("error.fw_no_online")),
                        "error_code": "E_NO_VARIANT", "release_url": release_url}), 409
    if not entry.get("url"):
        return jsonify({"ok": False,
                        "error": (t("error.fw_asset_missing")
                                  % entry.get("asset", "")),
                        "error_code": "E_NO_FW_ASSET", "release_url": release_url}), 409
    if not online_update.version_is_newer(entry["version"], fw.get("version", "")):
        return jsonify({"ok": False,
                        **_err(t("log.app_up_to_date", version=(fw.get("version") or ""))),
                        "release_url": release_url}), 409

    # 只接受 ota_*.bin（app-only），复用现有校验。
    name_err = _ota_validate(entry["asset"])
    if name_err:
        return jsonify({"ok": False, "error": name_err, "error_code": "E_BAD_ASSET",
                        "release_url": release_url}), 400

    # 设备必须在线：否则整包下载完才发现推不上去，白等几分钟。
    if not _ota_is_connected(dev_id):
        return jsonify({"ok": False, **_err(t("error.fw_device_offline")),
                        "error_code": "E_NOT_CONNECTED", "release_url": release_url}), 409

    with _update_lock:
        _fw_update_tasks[dev_id] = {"phase": "starting", "downloaded": 0,
                                     "total": entry.get("size", 0), "percent": 0,
                                     "error": "", "error_code": ""}
    threading.Thread(target=_fw_update_worker,
                     args=(dev_id, variant, entry, fw),
                     daemon=True, name="fw-update-%s" % dev_id).start()
    return jsonify({"ok": True, "task": dev_id, "version": entry["version"],
                    "asset": entry["asset"]})


def _fw_update_worker(dev_id, variant, entry, fw):
    """后台：下载固件 → 校验 → 走现有 OTA 推送通道。"""

    def _phase(phase, **fields):
        with _update_lock:
            task = _fw_update_tasks.get(dev_id)
            if task is None:
                return
            task.update(fields)
            task["phase"] = phase

    try:
        dest_dir = online_update.update_dir("fw-" + variant)
        final_path = os.path.join(dest_dir, entry["asset"])
        _phase("downloading", total=entry.get("size", 0))

        def _progress(done, total):
            with _update_lock:
                task = _fw_update_tasks.get(dev_id)
                if task is None:
                    return
                task["downloaded"] = done
                task["total"] = total or task.get("total", 0)
                if task["total"]:
                    task["percent"] = min(100, int(done * 100 / task["total"]))

        hasher = hashlib.sha256()
        part_path = online_update.download_to_file(
            entry.get("url", ""), dest_dir, entry["asset"],
            progress=_progress, expected_size=entry.get("size", 0),
            hasher=hasher)
        _phase("verifying")
        error = online_update.finalize_download(part_path, final_path,
                                                entry.get("sha256", ""),
                                                entry.get("size", 0),
                                                digest=hasher.hexdigest())
        if error:
            _phase("error", error=error, error_code="E_SHA256")
            return

        with open(final_path, "rb") as handle:
            file_data = handle.read()
        if not file_data:
            _phase("error", error=t("error.fw_file_empty"), error_code="E_EMPTY")
            return

        # 推送前必须校验镜像能装进本设备的 OTA 槽（复用现有逻辑）。
        slot_cap = _ota_slot_cap(fw)
        if slot_cap <= 0:
            _phase("error", error=t("error.fw_no_app1_short"),
                   error_code="E_NO_SLOT")
            return
        img_err = _ota_validate_image(file_data, slot_cap)
        if img_err:
            _phase("error", error=img_err, error_code="E_BAD_IMAGE")
            return

        _phase("pushing")
        sha_hex = hashlib.sha256(file_data).hexdigest()
        version = online_update.version_from_asset_name(entry["asset"])
        old = _ota_current_fw(dev_id)
        with _ota_lock:
            existing = _ota_windows.get(dev_id)
            if existing is not None and existing.status in ("starting", "uploading"):
                _phase("error", error=t("error.ota_busy_task"),
                       error_code="E_OTA_BUSY")
                return
        window = _ota_push(dev_id, entry["asset"], file_data, sha_hex, version,
                           old_version=old["version"], old_build=old["build"])
        _phase("pushing", ota_window=True)
        # 推送结束后由现有 /api/ota/<dev_id>/progress 报告结果；这里等它收尾。
        deadline = time.monotonic() + OTA_VERIFY_TIMEOUT + 60.0
        while time.monotonic() < deadline:
            with _ota_lock:
                live = _ota_windows.get(dev_id)
                if live is None or live.status in ("done", "error", "cancelled"):
                    break
            time.sleep(0.5)
        with _ota_lock:
            live = _ota_windows.get(dev_id)
            if live is not None and live.status == "error":
                _phase("error", error=live.error, error_code="E_OTA")
            else:
                _phase("done")
    except online_update.UpdateError as exc:
        _phase("error", error=exc.message, error_code=exc.code)
    except Exception as exc:  # noqa: BLE001
        _phase("error", error=t("error.fw_update_failed") % (exc,), error_code="E_INTERNAL")


@app.route("/api/firmware/update/progress")
def api_firmware_update_progress():
    """固件在线更新的五段进度（下载 → 校验 → 推送 → 重启 → 确认版本）。"""
    dev_id = request.args.get("dev_id") or ""
    with _update_lock:
        task = _fw_update_tasks.get(dev_id)
        if task is None:
            return jsonify({"ok": False, **_err(t("error.fw_no_task"))}), 404
        payload = dict(task)
    payload["ok"] = True
    # 推送阶段借用现有 OTA 窗口的进度（ack 百分比）。
    # 只有本任务自己推上去的窗口才作数（ota_window 标记）——_ota_windows
    # 里可能还留着上一次手动 OTA 的窗口，借它的百分比会把下载进度冲成 100%。
    if payload.get("ota_window"):
        with _ota_lock:
            window = _ota_windows.get(dev_id)
        if window is not None:
            payload["percent"] = window.percent()
            payload["ota_status"] = window.status
            # 终态（done/error）由任务自己决定：OTA 窗口的 phase 最后停在
            # "confirm"，无条件覆盖会让 done/error 永远到不了前端。
            if payload.get("phase") not in ("done", "error"):
                if window.status in ("starting", "uploading"):
                    payload["phase"] = window.phase
                elif window.status == "error":
                    payload["phase"] = "error"
                    payload["error"] = window.error
                    payload["error_code"] = "E_OTA"
                elif window.status == "done":
                    payload["phase"] = "done"
            if window.error and not payload.get("error"):
                payload["error"] = window.error
    return jsonify(payload)


# --- desktop / shell integration -----------------------------------------
def desktop_payload() -> dict:
    with _lock:
        recent = list(_config["settings"].get("recent_paths") or [])
        start_minimized = bool(_config["settings"].get("start_minimized", True))
        close_behavior = _config["settings"].get("close_button_behavior", "minimize")
    return {
        "platform": sys.platform,
        "frozen": desktop.is_frozen(),
        "desktop_mode": bool(desktop.STATE.get("desktop")),
        "tray_active": bool(desktop.STATE.get("tray_active")),
        "tray_available": bool(desktop.STATE.get("tray_available")),
        "tray_available_error": desktop.STATE.get("tray_available_error") or "",
        "window_hidden": bool(desktop.STATE.get("window_hidden")),
        "url": desktop.STATE.get("url") or f"http://127.0.0.1:{PORT}",
        "error": desktop.STATE.get("error") or "",
        "autostart": autostart.status(),
        "start_minimized": start_minimized,
        "close_button_behavior": close_behavior,
        "recent_paths": recent,
        # WS-529: 语言入口在「设备 → 桌面应用」。lang 是持久化偏好，
        # lang_effective 是当前实际生效的 locale。
        "lang": _config.get("lang", SYSTEM_PREF),
        "lang_effective": current_lang(),
        "langs": list(SUPPORTED_LANGS),
    }


@app.route("/api/i18n")
def api_i18n():
    """Message table for the web UI (WS-529).

    ``?lang=`` may be a tag (``en-US``) or ``system``; without it the stored
    preference is used.  The server's own language is *not* changed here —
    that only happens on ``POST /api/desktop`` so an unauthenticated page load
    cannot flip the tray/log language behind the user's back.
    """
    tag = (request.args.get("lang") or "").strip()
    if tag:
        effective = resolve_lang(tag)
    else:
        effective = current_lang()
    return jsonify({
        "lang": effective,
        "pref": normalize_pref(tag) if tag else _config.get("lang", SYSTEM_PREF),
        "default": SUPPORTED_LANGS[0],
        "messages": messages(effective),
    })


@app.route("/api/desktop", methods=["GET", "POST"])
def api_desktop():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        # 先把本次请求里的普通设置全部生效，再统一处理自启动条目：同一个
        # POST 可能同时带 start_minimized 和 autostart，先读旧值再用会在
        # 自启动项里写入过期的「启动时最小化」。
        if "start_minimized" in data:
            value = bool(data["start_minimized"])
            with _lock:
                _config["settings"]["start_minimized"] = value
            save_config()
        if "lang" in data:
            # Only the three known preferences may be persisted: a stray
            # {"lang": "xx"} used to be written verbatim into config.json.
            pref = normalize_pref(str(data.get("lang") or SYSTEM_PREF))
            with _lock:
                _config["lang"] = pref
            save_config()
            set_lang(pref)

        if "close_button_behavior" in data:
            value = str(data["close_button_behavior"])
            if value not in ("minimize", "exit"):
                value = "minimize"
            with _lock:
                _config["settings"]["close_button_behavior"] = value
            _close_behavior_ref["behavior"] = value
            save_config()
            add_event("desktop",
                      t("log.close_behavior",
                        mode=t("ui.close_minimize") if value == "minimize" else t("ui.close_exit")),
                      level="ok")

        with _lock:
            minimized = bool(_config["settings"].get("start_minimized", True))
        current = autostart.status()
        enabled = bool(current.get("enabled"))
        # 以真实状态为准：config 可能丢失、被手改或从别的机器拷来，只有
        # 当前条目确实是计划任务时才算「管理员模式」，否则一次只改
        # 「启动时最小化」就会把最高权限的任务静默降级成 Run 键。
        admin_pref = bool(current.get("admin"))
        notes: list[str] = []
        rewrite = False
        result: dict | None = None

        if "autostart" in data:
            want = bool(data["autostart"])
            if want:
                if "admin_autostart" in data:
                    admin_pref = bool(data["admin_autostart"])
                rewrite = True
                notes.append(t("log.autostart_admin_on") if admin_pref else t("log.autostart_on"))
            else:
                result = autostart.disable()
                if result.get("ok"):
                    admin_pref = False
                    notes.append(t("log.autostart_off"))
        elif "admin_autostart" in data:
            # 提权模式变了：注册表 Run 值与计划任务互斥，重写一次。
            admin_pref = bool(data["admin_autostart"])
            # 自启动本来就是关的、又只是「关掉提权」时不去写 Run 值 ——
            # 否则「取消开机自启动」会被这一下顺手打开。
            if admin_pref or enabled:
                rewrite = True
                notes.append(t("log.autostart_admin_on") if admin_pref
                             else t("log.autostart_admin_off"))
        elif enabled and "start_minimized" in data:
            # 自启动已开、只改了「启动时最小化」：重写条目让它生效。
            rewrite = True

        if rewrite:
            result = autostart.enable(minimized, admin=admin_pref)
        if result is not None and not result.get("ok"):
            # 原样带上后端原因：UAC 被取消、需要管理员账户、schtasks 报错
            # 都不是「重写条目」失败，改写文案会让用户看不懂该做什么。
            reason = str(result.get("error") or t("error.setting_failed"))
            add_event("desktop", t("log.autostart_failed", reason=reason), level="warn")
            # desktop_payload() also carries an "error" key (the desktop state
            # error), so it is spread *first* - written last, the real reason
            # below would be overwritten by that (usually empty) value and the
            # UI would fall back to the generic "设置失败".
            return jsonify({"ok": False, **desktop_payload(), "error": reason}), 500
        for message in notes:
            add_event("desktop", message, level="ok")

        if rewrite or "autostart" in data or "admin_autostart" in data:
            with _lock:
                _config["settings"]["admin_autostart"] = admin_pref
            save_config()
    return jsonify({"ok": True, **desktop_payload()})


@app.route("/api/desktop/port-busy", methods=["POST"])
def api_desktop_port_busy():
    """Tell *this* instance that another instance could not take the port.

    The elevated instance cannot serve the web UI when 8788 is still held by
    a normal-permission instance, so it reports the conflict here: the running
    instance shows it in its event log and in ``desktopHint``.
    """
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or desktop.port_busy_hint())
    desktop.STATE["error"] = message
    add_event("desktop", message, level="warn")
    return jsonify({"ok": True})


def _elevated_argv() -> list[str]:
    """Arguments for the elevated twin of this process.

    The executable itself is passed to ``ShellExecuteW`` separately, so only
    the arguments are built here.  Running from source the script path has to
    be part of them (``sys.executable`` is the interpreter then).
    """
    args: list[str] = []
    if not desktop.is_frozen():
        args.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"))
    if desktop.STATE.get("desktop"):
        args.append("--desktop")
    # Marker for the elevated instance: it takes over the port instead of
    # treating the running instance as a second copy.
    args.append("--elevated")
    return args


# The browser-mode (no tray, no native window) server, so the elevated
# restart can hand the port over.
_web_server = {"server": None}


def _exit_for_restart() -> bool:
    """Leave the current instance so the elevated twin can take the port.

    Desktop/tray mode goes through the regular quit path (``/api/quit``);
    browser mode shuts its own server down.  Never ``os._exit`` - the window
    and the tray have to be cleaned up.
    """
    if desktop.request_quit():
        return True
    server = _web_server.get("server")
    if server is None:
        return False

    def _run():
        time.sleep(0.4)  # let the HTTP response reach the browser first
        server.shutdown()

    threading.Thread(target=_run, name="quit", daemon=True).start()
    return True


@app.route("/api/desktop/restart-admin", methods=["POST"])
def api_desktop_restart_admin():
    """Restart the host with administrator rights (Windows only).

    The elevated instance is started through ``ShellExecuteW("runas")``, which
    shows the UAC prompt.  Afterwards this instance leaves through the regular
    quit path so the elevated one can take over the port.
    """
    if not sys.platform.startswith("win"):
        return jsonify({"ok": False, **_err(t("error.elevate_windows_only"))}), 409
    try:
        import ctypes
    except Exception as e:  # noqa: BLE001 - ctypes is always available on Windows
        return jsonify({"ok": False, **_err(t("error.elevate_api_failed", detail=e))}), 500
    args = _elevated_argv()
    params = subprocess.list2cmdline(args)
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, **_err(t("error.elevate_start_failed", name=type(e).__name__, message=e))}), 500
    if rc is None or rc <= 32:
        # <= 32: the UAC prompt was declined (or the launch failed).
        add_event("desktop", t("error.elevate_cancelled"), level="warn")
        return jsonify({"ok": False, **_err(t("error.elevation_cancelled"))}), 409
    add_event("desktop", t("log.elevated_started"), level="ok")
    # Leave through the existing exit path (server shutdown + window close),
    # never os._exit, so the elevated instance can bind the port.
    if not _exit_for_restart():
        return jsonify({"ok": False, **_err(t("error.cannot_exit"))}), 500
    return jsonify({"ok": True})


@app.route("/api/browse", methods=["POST"])
def api_browse():
    data = request.get_json(silent=True) or {}
    result = desktop.browse(data.get("kind") or "file")
    if result.get("ok") and result.get("path"):
        path = result["path"]
        with _lock:
            recent = [p for p in (_config["settings"].get("recent_paths") or []) if p != path]
            recent.insert(0, path)
            _config["settings"]["recent_paths"] = recent[:8]
        save_config()
        add_event("desktop", t("log.file_selected") + path, level="ok")
    return jsonify(result), (200 if result.get("ok") else 503)


@app.route("/api/quit", methods=["POST"])
def api_quit():
    if desktop.request_quit():
        add_event("desktop", t("log.quit_requested"))
        return jsonify({"ok": True})
    return jsonify({"ok": False, **_err(t("error.quit_not_desktop"))}), 409


@app.route("/api/show-window", methods=["POST"])
def api_show_window():
    """Restore the native window (recovery path when tray is unavailable).

    The window may have been hidden by ``_on_closing`` when the user clicked X
    with "minimize to tray" checked but no tray icon was available.  The web UI
    at the served URL is then the only way to interact with the app, and this
    endpoint is how the user brings the window back.
    """
    if not desktop.STATE.get("desktop"):
        return jsonify({"ok": False, **_err(t("error.not_desktop"))}), 409
    if desktop.show_window():
        add_event("desktop", t("log.window_restored"))
        return jsonify({"ok": True})
    return jsonify({"ok": False, **_err(t("error.no_window_to_restore"))}), 409


@app.route("/api/log")
def api_log():
    since = request.args.get("since", "0")
    try:
        since_i = int(since)
    except ValueError:
        since_i = 0
    with _lock:
        items = [e for e in _events if e["seq"] > since_i]
    return jsonify({"events": items, "seq": _event_seq})


@app.route("/api/keycodes")
def api_keycodes():
    return jsonify({"keys": [{"label": l, "code": c} for l, c in KEY_CODES],
                    # WS-546: modifiers in display order (Ctrl -> Shift -> Alt
                    # -> Win). The drawer and the summary both render from this
                    # list, so the order exists exactly once.
                    "mods": [{"bit": m["bit"], "id": m["id"], "label": t(m["key"])}
                             for m in MOD_DEFS],
                    "defaults": {"pins": DEFAULT_PINS, "keys": DEFAULT_KEYS}})


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="BindDeckLite", description=t("cli.description"))
    parser.add_argument("--desktop", "--tray", dest="desktop", action="store_true",
                        help=t("cli.desktop_help"))
    parser.add_argument("--minimized", action="store_true",
                        help=t("cli.minimized_help"))
    parser.add_argument("--no-tray", dest="no_tray", action="store_true",
                        help=t("cli.no_tray_help"))
    parser.add_argument("--file-dialog", choices=("file", "dir"), help=argparse.SUPPRESS)
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.file_dialog:
        return desktop.dialog_main(args.file_dialog)

    if desktop.is_frozen():
        # A windowed exe has no console; keep prints in a log file next to config.
        desktop.setup_stdio(os.path.join(DATA_DIR, "host.log"))

    url = f"http://127.0.0.1:{PORT}"
    desktop_mode = (args.desktop or desktop.is_frozen()
                    or os.environ.get("BINDDECKLITE_DESKTOP") == "1")
    if args.elevated:
        # 提权实例接管端口：等当前实例退出、释放 8788。等不到就明确报错，
        # 而不是只抛一个 WinError 10048。
        if not desktop.wait_port_free(PORT, timeout=ELEVATED_PORT_WAIT):
            message = desktop.port_busy_message(PORT)
            print("[app] " + message)
            add_event("desktop", message, level="warn")
            # 让占着端口的那个实例在它的日志与 desktopHint 里显示同样的话。
            desktop.report_port_busy(url, message)
            return 1
    elif desktop_mode and desktop.port_in_use(PORT):
        if autostart.is_elevated():
            # 手动以管理员身份启动，但端口被普通权限实例占着：明确说清楚，
            # 不要只留一个 WinError 10048（WS-457）。
            message = desktop.port_busy_message(PORT)
            print("[app] " + message)
            desktop.report_port_busy(url, message)
            return 1
        # Second double-click: raise the running instance's native window.
        # A desktop exe must never fall back to a browser tab.
        if desktop.focus_running_instance(url):
            print(f"[app] already running at {url}, focused existing window")
            return 0
        message = t("error.already_running")
        print("[app] " + message)
        desktop.show_error_dialog("BindDeckLite", message)
        return 1

    load_config()
    # WS-529: 语言偏好落盘在 config.json 顶层，启动时先应用，
    # 这样首屏渲染、SSE 事件与托盘文案都是用户上次选的语言。
    set_lang(_config.get("lang", SYSTEM_PREF))
    _update_after_start()
    started = ble.start()
    if not started:
        add_event("error", t("log.ble_unavailable") + str(ble.error), level="warn")
        print("[app] BLE unavailable:", ble.error)
    print(f"[app] BindDeckLite host running at {url}")

    if desktop_mode:
        _close_behavior_ref["behavior"] = _config["settings"].get("close_button_behavior", "minimize")
        return desktop.run_webview(app, PORT, url,
                                   start_minimized=bool(args.minimized),
                                   use_tray=not args.no_tray,
                                   close_ref=_close_behavior_ref,
                                   log_path=os.path.join(DATA_DIR, "host.log"),
                                   elevated=bool(args.elevated))

    if os.environ.get("BINDDECKLITE_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # 用 make_server 而不是 app.run：这样「立即以管理员身份重启」能让当前实例
    # 优雅退出、把端口让给提权实例（桌面/托盘模式走 desktop.request_quit）。
    server, error = desktop.make_flask_server(app, PORT)
    if server is None:
        print("[app] cannot bind port:", error)
        if desktop.is_port_busy_error(error):
            desktop.report_port_busy(url, error)
        return 1

    _web_server["server"] = server
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _web_server["server"] = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
