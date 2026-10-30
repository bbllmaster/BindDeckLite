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
from ble_link import BleLinkManager, scan_reason

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
_action_running = {}      # (dev_id, idx) -> {"started", "token", "thread"}; WS-359/360
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
}}

# WS-359: a stuck action thread never reaches its ``finally``, so an entry kept
# past this many seconds is force-released instead of breathing forever.
ACTION_STUCK_SECONDS = 120.0

# --- defaults -------------------------------------------------------------
DEFAULT_PINS = {
    "ESP32": [13, 12, 14, 27, 32, 33, 25, 26, 4],
    "C3": [0, 1, 2, 3, 4, 5, 6, 7, 8],
}
DEFAULT_KEYS = [0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8, 0xF9]
DEFAULT_LABELS = ["F13", "F14", "F15", "F16", "F17", "F18", "F19", "F20", "F21", "F22"]

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
_ENCODER_ROLE_TEXT = {
    "CLK": "编码器 A 相脚（CLK）",
    "DT": "编码器 B 相脚（DT）",
    "SW": "编码器按压脚",
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
    the range limit, ``blocked`` and the encoder pins — plus ``avoid`` for pins
    that are legal but not worth spending (C3 18/19 are USB-CDC).  Deriving
    instead of writing a second list keeps the hint from drifting away from
    the rule again (WS-481).
    """
    occupied = set()
    for lo, hi in rules["blocked"]:
        occupied.update(range(lo, hi + 1))
    occupied.update(rules["encoder"].values())
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
    return " 与 ".join(
        "%s %s" % (_pin_span_text([[lo, hi]]), label) for lo, hi, label in reserved
    )


def pin_rules(chip: str = "") -> dict:
    """The pin constraints for one chip, in the shape the UI renders.

    Descriptive only — ``validate_pins()`` is the single authority on whether
    a value is accepted; this just states the same limits as a hint.  The hint
    text is built from these fields, never from a second hand-written list.
    """
    prof = pin_chip(chip)
    if prof == "C3":
        reserved = [[11, 17, "内部 Flash"]]
        rules = {
            "chip": "C3",
            "count": PIN_COUNT,
            "min": 0,
            "max": 21,
            "blocked": [[lo, hi] for lo, hi, _ in reserved],
            "avoid": [18, 19],   # legal, but USB-CDC — not worth spending
            "encoder": dict(ENCODER_PINS["C3"]),
        }
        rules["hint"] = (
            "C3：按键可用 GPIO%s（避开 %s）；编码器 A/B/按压 = %s；%s 是 USB-CDC，不建议占用。"
            % (
                _pin_span_text(_pin_free_spans(rules)),
                _reserved_pin_text(reserved),
                _pin_span_text([[p, p] for p in rules["encoder"].values()], "/"),
                _pin_span_text([[p, p] for p in rules["avoid"]], "/"),
            )
        )
        return rules

    reserved = [[6, 11, "SPI Flash"], [34, 39, "仅输入脚"]]
    rules = {
        "chip": "ESP32",
        "count": PIN_COUNT,
        "min": 0,
        "max": 39,
        "blocked": [[lo, hi] for lo, hi, _ in reserved],
        "encoder": dict(ENCODER_PINS["ESP32"]),
    }
    rules["hint"] = "经典 ESP32：按键可用 GPIO%s（避开 %s）；编码器 A/B/按压 = %s。" % (
        _pin_span_text(_pin_free_spans(rules)),
        _reserved_pin_text(reserved),
        _pin_span_text([[p, p] for p in rules["encoder"].values()], "/"),
    )
    return rules


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


def _pin_reject_reason(prof: str, btn: str, pin: int):
    """Why ``pin`` cannot be a button pin on ``prof``. ``("", "")`` = accepted.

    Returns ``(code, message)`` so the caller can tag the row without parsing
    the text back apart.
    """
    who = "%s 的 GPIO %d" % (btn, pin)
    if prof == "C3":
        if pin > 21:
            return "out_of_range", who + " 在 C3 上不存在（C3 只有 GPIO0-10、20、21）"
        if 11 <= pin <= 17:
            return "reserved", who + " 在 C3 上是内部 Flash 引脚，不能用作按键"
    else:
        if 6 <= pin <= 11:
            return "reserved", who + " 是 SPI Flash 引脚（GPIO 6-11），不能用作按键"
        if pin >= 34:
            return "reserved", who + " 是仅输入引脚（GPIO 34-39），没有内部上拉，不能用作按键"
    for role, encoder_pin in ENCODER_PINS[prof].items():
        if pin == encoder_pin:
            return "encoder", who + " 与" + _ENCODER_ROLE_TEXT[role] + "冲突"
    return "", ""


def validate_pins(pins, chip: str = "") -> list:
    """Return one ``{index, code, message}`` per offending button row.

    ``[]`` means the pin set is acceptable — the same set the firmware would
    accept, so ``CFG:PINS`` is only sent when the deck can actually take it.
    Every message starts with 「按键 N」so the device page can show it under
    that row instead of framing all nine inputs (the old behaviour).

    Rules (src/main.cpp:228-285): non-empty, integer 0-39, unique, and on top
    of that the per-chip reserved ranges plus the encoder pins.
    """
    prof = pin_chip(chip)
    values = list(pins) if isinstance(pins, (list, tuple)) else []
    errors = []
    accepted = {}   # pin -> [row indices], used for the duplicate report

    for i in range(PIN_COUNT):
        btn = "按键 %d" % (i + 1)
        raw = values[i] if i < len(values) else None
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            errors.append({"index": i, "code": "empty", "message": btn + " 不能为空"})
            continue
        ok, pin = _pin_int(raw)
        if not ok:
            errors.append({"index": i, "code": "not_integer",
                           "message": btn + " 必须是 0-39 的整数"})
            continue
        if pin < 0 or pin > 39:
            errors.append({"index": i, "code": "out_of_range",
                           "message": btn + " 必须填 0-39 的整数"})
            continue
        reason_code, reason = _pin_reject_reason(prof, btn, pin)
        if reason:
            errors.append({"index": i, "code": reason_code, "message": reason})
            continue
        accepted.setdefault(pin, []).append(i)

    # Duplicates: every row that shares a pin is flagged, so fixing either one
    # clears both at once.
    for pin, rows in accepted.items():
        if len(rows) < 2:
            continue
        names = ["按键 %d" % (i + 1) for i in rows]
        text = ("%s 与%s" % (names[0], names[1]) if len(names) == 2
                else "、".join(names))
        for i in rows:
            errors.append({"index": i, "code": "duplicate",
                           "message": "%s 都使用了 GPIO %d" % (text, pin)})

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
    "pins": "按键引脚",
    "keys": "按键码",
    "enc": "编码器模式",
    "sleep": "休眠",
    "tz": "时区",
}

# Ordered (field, CFG prefix, label) tuples for ACK matching and readback
# comparison. The prefix is the part after "CFG:" — e.g. "PINS" for "CFG:PINS:".
_CFG_READBACK_FIELDS = [
    ("pins", "PINS", "按键引脚"),
    ("keys", "KEYS", "按键码"),
    ("enc", "ENC", "编码器模式"),
    ("sleep", "SLEEP", "休眠"),
    ("tz", "TZ", "时区"),
]


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
        self._ack_ok = {f: False for f, _, _ in _CFG_READBACK_FIELDS}
        self._ack_events = {f: threading.Event() for f, _, _ in _CFG_READBACK_FIELDS}
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
        if self._rb_count >= len(_CFG_READBACK_FIELDS):
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
    whether the answer is complete.  ``tz`` is captured as well but stays
    optional — it is merged when it happens to arrive, never waited for.
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
        for i, code in enumerate(codes[:10]):
            if i >= len(cfg.get("keys", [])):
                break
            cfg["keys"][i]["code"] = code
            cfg["keys"][i]["label"] = next((lbl for lbl, c in KEY_CODES if c == code), str(code))
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

    All five device-reported groups are recognized.  Only the four tracked
    ones (pins / keys / enc / sleep) decide whether the answer is complete;
    ``tz`` is captured as well but never waited for.
    """
    for field, prefix, _ in _CFG_READBACK_FIELDS:
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
    add_event("cfg", "设备回报 CFG_" + field.upper() + ":" + raw, dev_id)
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
            return "设备拒绝"
        if len(pins) != len(set(pins)):
            return "引脚重复"
        return "保留脚"
    return "设备拒绝"


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
            }
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print("[app] config load failed:", e)


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
        "pins": list(DEFAULT_PINS.get(chip, DEFAULT_PINS["ESP32"])),
        "keys": [
            {"code": DEFAULT_KEYS[i], "label": DEFAULT_LABELS[i],
             "action": {"type": "none", "value": "", "macro": []}}
            for i in range(10)
        ],
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
        return cfg


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
            "text": text,
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
    0: "未知",
    1: "上电",
    2: "外部复位",
    3: "软件重启",
    4: "软件崩溃（panic）",
    5: "中断看门狗",
    6: "任务看门狗",
    7: "看门狗",
    8: "深度睡眠唤醒",
    9: "掉电（欠压）",
    10: "SDIO 复位",
    11: "USB 复位",
    12: "JTAG 复位",
    13: "EFUSE 错误",
    14: "电源毛刺",
    15: "CPU 锁死",
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
        return "未知"
    label = RESET_REASON_TEXT.get(code, "未知")
    return f"{label}({code})"


def uptime_text(seconds) -> str:
    """「3 小时 12 分」/「4 分 5 秒」/「42 秒」."""
    if seconds is None:
        return "未知"
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def heap_text(free_bytes) -> str:
    """「96 KB」— one decimal below 100 KB so a tight heap stays visible."""
    if free_bytes is None:
        return "未知"
    kb = max(0, int(free_bytes)) / 1024.0
    if kb >= 100 or abs(kb - round(kb)) < 0.05:
        return f"{kb:.0f} KB"
    return f"{kb:.1f} KB"


def nvs_text(nvs) -> str:
    """「可用」/「不可用：配置不会保存」."""
    if nvs == 1:
        return "可用"
    if nvs == 0:
        return "不可用：配置不会保存"
    return "未知"


# --- millis() wrap (WS-449) -----------------------------------------------
# ESP32 millis() is a 32-bit millisecond counter: it wraps at ~49.7 days, so
# SYS:uptime falls from ~4294967 s back to ~0 even though the deck never
# rebooted.  A card showing 「0 秒」 would read as "the device just restarted",
# which is exactly wrong.  We remember the last value per device and, when the
# new one is clearly below it, say "已运行" relative to this connection instead.
UPTIME_WRAP_TEXT = "本次连接内已运行"
UPTIME_WRAP_NOTE = "设备计时已回绕"


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
        notes["safe"] = "安全模式：连续多次启动未稳定，本次跳过配网手势"
    if flags["deepsleep"]:
        notes["deepsleep"] = "设备是睡着了，不是死机"
    if flags["heap_low"]:
        notes["heap"] = "内存偏低，可能影响 OTA"
    if wrapped:
        notes["uptime_wrap"] = UPTIME_WRAP_NOTE
    return {
        "sample": sample,
        "display": {
            "reset": reset_reason_text(sample["reset"]),
            # app.js renders notes.safe/heap/deepsleep only, so the wrap hint
            # is also carried in the value itself; the plain 「5 秒」 is
            # suppressed because it reads as "just rebooted".
            "uptime": (f"{UPTIME_WRAP_TEXT}（{UPTIME_WRAP_NOTE}）" if wrapped
                       else uptime_text(sample["uptime"])),
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
        add_event("key", f"按键 {idx + 1 if 0 <= idx < 9 else '编码器按压'}", dev_id)
        _run_action_for(dev_id, idx)
        return
    if line.startswith("ENC:"):
        parts = line.split(":")
        direction = parts[-1] if parts else ""
        add_event("enc", "编码器 " + ("顺时针" if direction == "CW" else "逆时针"), dev_id)
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
            cfg["fw"] = {"version": fields[0] if fields else "",
                         "model": fields[1] if len(fields) > 1 else "",
                         "chip": chip,
                         "build": fields[3] if len(fields) > 3 else "",
                         # 5th field is flash size in KB (added in v1.3.1); older
                         # firmware omits it and we fall back to 0 (unknown).
                         "flash": int(fields[4]) if len(fields) > 4 and fields[4].strip().isdigit() else 0}
            save_config()
        add_event("device", f"固件 {cfg['fw']['version']} ({chip})", dev_id)
        return

    # Apply window: capture ACK during the sending phase so the caller can
    # confirm the device received each CFG:* line (1.5 s per item).
    # Firmware echoes ACK:CFG:PINS:... — strip "ACK:" then match "CFG:" + prefix + ":".
    if line.startswith("ACK:") and window and window.phase == "sending":
        body = line[4:]  # strip "ACK:" → body = "CFG:PINS:..."
        for field, prefix, _ in _CFG_READBACK_FIELDS:
            if body.startswith("CFG:" + prefix + ":"):
                window.on_ack(field)
                break
        add_event("ack", "设备确认: " + body[:60], dev_id, "ok")
        return

    # Apply window: capture CFG_* during the readback phase WITHOUT touching
    # the local config — the user must explicitly accept device values.
    # Firmware responds to CMD:GET_CFG with CFG_PINS:... (underscore).
    if window and window.phase == "readback":
        for field, prefix, _ in _CFG_READBACK_FIELDS:
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
        add_event("cfg", "已读取设备按键配置", dev_id)
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
        add_event("ack", "设备确认: " + line[len("ACK:"):][:60], dev_id, "ok")
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
                  "主机绑定: " + ("已绑定" if parsed["bound"] else "未绑定"),
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
        add_event("pong", f"设备响应 {rtt} ms", dev_id, "ok")
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


def apply_device(dev_id: str) -> dict:
    cfg = device_cfg(dev_id)

    # WS-476: refuse an invalid pin set before anything touches the link. The
    # old code built the line first, so a cleared box (None) blew up on
    # int(None) and the user only saw "500 INTERNAL SERVER ERROR".
    pin_errors = validate_pins(cfg.get("pins"), cfg.get("chip", ""))
    if pin_errors:
        add_event("cfg", "未下发：" + pin_errors[0]["message"], dev_id, "warn")
        return {"ok": False, "invalid": True, "pin_errors": pin_errors,
                "error": pin_errors[0]["message"]}

    # Build the values we are going to send — this snapshot is what the
    # readback is compared against, so a rejected field shows the user what
    # they tried to set vs what the device actually kept.
    pins = ",".join(str(int(p)) for p in cfg.get("pins", [])[:9])
    keys = ",".join(str(int(k["code"])) for k in cfg.get("keys", [])[:10])
    enc_val = str(int(cfg["encoder"].get("mode", 0)))
    sleep = cfg.get("sleep", {})
    sleep_val = "%d,%d" % (
        1 if sleep.get("enabled", True) else 0,
        max(1, int(sleep.get("minutes", 5))),
    )
    tz_val = cfg.get("tz", "")

    snapshot = {
        "pins": pins,
        "keys": keys,
        "enc": enc_val,
        "sleep": sleep_val,
        "tz": tz_val,
    }

    # Create the per-device apply window before sending anything.
    window = ApplyWindow(dev_id, snapshot)
    with _apply_lock:
        _apply_windows[dev_id] = window

    try:
        results = []
        commands = [
            ("pins", "CFG:PINS:" + pins),
            ("keys", "CFG:KEYS:" + keys),
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
                reason = "设备未连接" if not any_sent else "连接中断"
            elif not any_readback:
                # ACK came back but CMD:GET_CFG produced nothing at all —
                # old firmware that does not support readback.
                status = "unverified"
                reason = "已下发，设备未回报（固件不支持校验）"
            elif field not in window.readback:
                # Some fields came back but this one is missing.
                status = "unverified"
                reason = "设备未回报该字段"
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

        return {
            "ok": all(i["status"] == "ok" for i in items),
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
        add_event("device", "运行状态：设备未响应 CMD:SYSINFO（固件可能不支持）",
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
        reason = "设备未回报配置" if got == 0 else "设备未回报完整配置"
        return {"ok": False, "cfg": None, "fields": fields,
                "at": at, "reason": reason}

    # Work on a copy first: a malformed value must not leave the stored
    # config half-updated either.  The whole read-modify-write happens under
    # the lock so a concurrent writer cannot slip in between.
    with _lock:
        cfg = device_cfg(dev_id)
        merged = copy.deepcopy(cfg)
        for field, _prefix, _label in _CFG_READBACK_FIELDS:
            if field in values and not _apply_cfg_field(merged, field, values[field]):
                return {"ok": False, "cfg": None, "fields": fields, "at": at,
                        "reason": "设备回报的配置无法解析"}
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
            return f"text({delay}ms, {count} 字符)"
        return f"text({count} 字符)"
    if stype == "paste":
        return f"paste({len(str(value or ''))} 字符)"
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
        return "0 步"
    total = len(steps)
    parts = [_macro_step_text(step) for step in steps]
    head = f"{total} 步："
    summary = head + " → ".join(parts)
    if len(summary) > MACRO_SUMMARY_MAX:
        # Keep whole steps only — a half-printed step ("text(13 字") would
        # be misleading.  The tail carries the real step count.
        tail = f"…（共 {total} 步）"
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
            return None, "default", "Windows 不支持 Safari"
        exe = _windows_browser_exe(key)
        if exe:
            return exe, label, ""
        # Never fall through to webbrowser.get() here: that is the call that
        # silently resolves to the default handler (Edge).
        return None, "default", f"未找到浏览器 {key}"

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
    return None, "default", f"未找到浏览器 {key}（{'; '.join(failures)}）"


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
        print(f"[app] 宏浏览器打开失败 ({type(e).__name__}: {e})，回退默认浏览器")
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
        return "未指定可执行文件"
    try:
        parts = _split_app_tokens(value)
    except ValueError:
        return "命令行引号不配对"
    if not parts or not parts[0]:
        return "未指定可执行文件"
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
        print(f"[app] Win32 clipboard API 不可用 ({type(e).__name__}: {e})")
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
                          f"粘贴快捷键 {MACRO_PASTE_HOTKEY} 发送失败"
                          f"（{type(e).__name__}: {e}），paste 步骤回退逐字符输入",
                          dev_id, "error")
            # Restore on both paths: the clipboard already holds this step's
            # text, so bailing out on the failure path would lose what the
            # user had copied.
            _restore_clipboard(cb, previous, text)
            return sent
        finally:
            cb.close()
    add_event("action", "剪贴板不可用，paste 步骤跳过（回退逐字符输入）",
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


def run_action(action: dict, dev_id: str = "") -> dict:
    """Execute one host-side action and return a structured result.

    Returns ``{ok: bool, error: str, action_type: str, target: str}`` where
    ``error`` is a human-readable failure reason (empty when ``ok`` is True).
    ``dev_id`` only tags inner log rows (clipboard/paste warnings) with the
    device they came from.
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
                return {"ok": False, "error": "地址为空",
                        "action_type": kind, "target": target}
            return {"ok": True, "error": "", "action_type": kind, "target": target}
        # Pre-check local launch targets so failures are visible on every
        # platform (xdg-open / open do not raise on Linux for missing files).
        if kind == "launch" and not value.startswith(("http://", "https://")):
            if not os.path.exists(value):
                return {"ok": False, "error": "文件不存在: " + value,
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
            return {"ok": False, "error": "宏需要 keyboard 库: pip install keyboard",
                    "action_type": kind, "target": target}
        # WS-484: remember which step failed so the log row can point at it
        # without echoing the macro body.
        current_step = 0
        current_type = ""
        try:
            # ``browser:`` steps choose the target for later ``url:`` steps;
            # None means "system default browser". Reset on a failed open so a
            # broken target does not break every following url.
            browser_target = None
            browser_used = "default"
            for current_step, step in enumerate(action.get("macro", []), 1):
                st = step.get("type")
                current_type = st or ""
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
                    time.sleep(max(0, int(step.get("value", 0))) / 1000.0)
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
                        add_event("error",
                                  "app: 未指定可执行文件，本步跳过，"
                                  "继续执行后续步骤",
                                  dev_id, "error")
                        continue
                    app_exe_raw, _app_args = _parse_app_cmd(app_val)
                    if not app_exe_raw:
                        # 引号不配对（或只写了一个空的可执行文件名）：整条
                        # 命令行不可信，绝不带着猜测的参数去启动应用。
                        # _app_cmd_error 只在出错这条路径上再解析一次，
                        # 用于区分「引号不配对」和「没写可执行文件」。
                        add_event("error",
                                  f"app: {_app_cmd_error(app_val)}，本步跳过，"
                                  "继续执行后续步骤",
                                  dev_id, "error")
                        continue
                    app_exe = _resolve_exe(app_exe_raw)
                    if not app_exe:
                        add_event("error",
                                  f"app: 未找到可执行文件 "
                                  f"{app_exe_raw}（已尝试路径展开与 PATH "
                                  "解析），本步跳过，继续执行后续步骤",
                                  dev_id, "error")
                        continue
                    add_event("action", f"正在启动 {app_val}", dev_id, "ok")
                    prev_hwnd = _win_foreground_hwnd()
                    proc = None
                    try:
                        proc = _open_app(app_val)
                    except (FileNotFoundError, PermissionError,
                            OSError) as e:
                        add_event("error",
                                  f"app: 启动失败 "
                                  f"{type(e).__name__}: {e}",
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
                                      f"app: 等待窗口到前台超时（"
                                      f"{MACRO_APP_FOCUS_TIMEOUT} "
                                      "秒），后续 text:/key:/paste:"
                                      "可能打到别的窗口",
                                      dev_id, "warn")
                elif st == "browser":
                    browser_target, browser_used, browser_err = (
                        resolve_macro_browser(step.get("value", "")))
                    if browser_err:
                        print("[app] 宏 browser 步骤:", browser_err)
                        add_event("error", browser_err + "，已回退默认浏览器",
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
                                f"未能用 {browser_used} 打开 {url_log}"
                                f"（{open_err}），已回退默认浏览器",
                                level="error")
                            browser_target = None
                            browser_used = "default"
                        elif browser_used == "default":
                            add_event("action", f"已用默认浏览器打开 {url_log}",
                                      level="ok")
                        else:
                            add_event("action",
                                      f"已用 {browser_used} 打开 {url_log}",
                                      level="ok")
                        # Keep the freshly opened window in front, otherwise
                        # the next text:/paste: steps land in the old window.
                        # Windows only; bounded by MACRO_URL_FOCUS_TIMEOUT.
                        _wait_url_foreground()
            return {"ok": True, "error": "", "action_type": kind,
                    "target": target, "browser_used": browser_used}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "action_type": kind, "target": target,
                    "step": current_step, "step_type": current_type}

    if kind == "media":
        kb = _load_keyboard()
        if kb is None:
            return {"ok": False, "error": "媒体键需要 keyboard 库: pip install keyboard",
                    "action_type": kind, "target": target}
        try:
            kb.send(value)
            return {"ok": True, "error": "", "action_type": kind, "target": target}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "action_type": kind, "target": target}

    return {"ok": True, "error": "", "action_type": kind, "target": target}


def _action_label(idx: int) -> str:
    """Human-readable input label for an index (0-8 = 按键 1-9, 9 = 编码器按压)."""
    return f"按键 {idx + 1}" if 0 <= idx < 9 else "编码器按压"


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
        label = _action_label(idx)
    atype = result.get("action_type", (action or {}).get("type", "unknown"))
    target = result.get("target", "")
    if result["ok"]:
        add_event("action", f"动作成功: {label} · {atype} · {target}", dev_id, "ok")
        return
    if atype == "macro" and "step" in result:
        steps = (action or {}).get("macro") or []
        total = len(steps) if isinstance(steps, list) else 0
        step_type = MACRO_STEP_LABELS.get(result.get("step_type", ""),
                                          result.get("step_type", ""))
        where = f"宏第 {result.get('step', 0)}/{total} 步"
        if step_type:
            where += f" {step_type}"
        add_event("action",
                  f"动作失败: {label} · {where} 失败：{result['error']}",
                  dev_id, "error")
        return
    add_event("action",
              f"动作失败: {label} · {atype} · {target} · {result['error']}",
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
                  f"动作超时释放: {_action_label(idx)} · 已运行 {elapsed:.1f} 秒，"
                  f"强制解除执行中状态",
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
        _action_running[(dev_id, idx)] = {"started": now, "token": token, "thread": None}
        return token, None


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
                  f"动作跳过: {_action_label(idx)} · 上一次动作仍在执行"
                  f"（已运行 {busy_for:.1f} 秒）",
                  dev_id, "warn")
        return

    def _do():
        try:
            result = run_action(action, dev_id)
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
    return "编码器顺时针" if direction == "CW" else "编码器逆时针"


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
                  f"动作超时释放: {_encoder_action_label(direction)} · 已运行 "
                  f"{elapsed:.1f} 秒，强制解除执行中状态",
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
                  f"动作跳过: {label} · 上一次动作仍在执行"
                  f"（已运行 {busy_for:.1f} 秒）",
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
    payload = new_device_cfg(chip, "")
    payload["pinRules"] = pin_rules(chip)
    if "pins" in request.args:
        payload["pinErrors"] = validate_pins(
            parse_pins_query(request.args.get("pins", "")), chip)
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
        for field in ("pins", "keys", "encoder", "sleep", "tz", "name"):
            if field in data:
                cfg[field] = data[field]
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
        return jsonify({"ok": False, "error": "该按键的动作仍在执行",
                        "running": True, "elapsed": round(busy_for, 1)}), 200

    # Execute synchronously (5 s cap) so the caller gets the real result.
    result_queue: "queue.Queue" = queue.Queue()

    def _worker():
        try:
            result_queue.put(run_action(action, dev_id))
        except Exception as e:  # noqa: BLE001
            result_queue.put({"ok": False, "error": f"{type(e).__name__}: {e}",
                              "action_type": action.get("type", ""),
                              "target": _action_display_target(action)})
        finally:
            # A macro longer than the 5 s wait keeps running in the background;
            # the worker owns the claim then, so the card stays in "执行中".
            _end_action(dev_id, idx, token)

    t = threading.Thread(target=_worker, daemon=True)
    _bind_action_thread(dev_id, idx, token, t)
    try:
        t.start()
    except Exception:  # noqa: BLE001 — Thread.start() failed, keep the table clean
        _end_action(dev_id, idx, token)
        raise
    try:
        result = result_queue.get(timeout=5.0)
    except queue.Empty:
        # Still running: report it as such instead of a misleading timeout.
        return jsonify({"ok": False, "error": "仍在执行", "running": True}), 200

    _end_action(dev_id, idx, token)
    _record_action_result(dev_id, idx, action, result)
    return jsonify({"ok": result["ok"], "error": result["error"],
                    "browser_used": result.get("browser_used", "")})


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
                        "error": "仅「上位机动作」模式可测试旋转动作"})
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
        return jsonify({"ok": False, "error": "该方向的动作仍在执行",
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

    t = threading.Thread(target=_worker, daemon=True)
    _bind_encoder_action_thread(dev_id, direction, token, t)
    try:
        t.start()
    except Exception:  # noqa: BLE001
        _end_encoder_action(dev_id, direction, token)
        raise
    try:
        result = result_queue.get(timeout=5.0)
    except queue.Empty:
        # Still running: the worker owns the claim, so the card stays
        # "执行中" until the action finishes. Do NOT release here.
        return jsonify({"ok": False, "error": "仍在执行", "running": True}), 200

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
        return "未选择文件"
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # Accept the release naming (ota_binddecklite_c3_v1.3.0.bin) — dots are
    # part of the version, so do not restrict the stem to word characters.
    if not _re.fullmatch(r"ota_[^/\\]+\.bin", base, _re.IGNORECASE):
        return ("文件名必须以 ota_ 开头、以 .bin 结尾（app-only 固件），"
                "不要上传 merged 整片包")
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
        return "文件过小，不是完整的 ESP32 固件镜像"
    if file_data[0] != 0xE9:
        return "不是 ESP32 固件镜像（缺少 0xE9 文件头），请选择 ota_*.bin"
    # app descriptor magic at 0x20 — only an app image has it there.
    if file_data[0x20:0x24] != b"\x32\x54\xcd\xab":
        return ("这是整片 merged 包，OTA 只能刷 app-only 固件（ota_*.bin）。\n"
                "请到发布页下载 ota_binddecklite_*_vX.Y.Z.bin，"
                "或用配网页面 http://192.168.4.1 → 固件 OTA 升级。")
    # Walk the segments to (a) reject a corrupt/truncated segment table and
    # (b) compute the declared image length (header + segments + checksum).
    # seg_count is one byte (0-255), so the loop is bounded; a corrupt count
    # is caught by the pos+8 > n check below rather than silently truncated.
    try:
        seg_count = file_data[1]
        pos = 24  # ESP_IMAGE_HEADER_LEN
        for _ in range(seg_count):
            if pos + 8 > n:
                return "固件镜像段表损坏或被截断"
            seg_len = int.from_bytes(file_data[pos + 4:pos + 8], "little")
            if seg_len > n or pos + 8 + seg_len > n:
                return "固件镜像已损坏或被截断（段长度越界）"
            pos += 8 + seg_len
        core_len = pos + 1  # + trailing checksum byte
    except Exception:
        return "固件镜像结构无法解析"

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
            return ("固件镜像不完整或尾部 SHA256 不匹配（已损坏/被截断）。"
                    "请重新下载并核对发布页的 SHA256。")
        # valid image with appended hash; alignment padding before the hash is
        # covered by the hash itself, so nothing more to check here.
    else:
        # No appended hash: rely on the segment walk. The file must be at
        # least core_len, with at most 16 bytes of alignment padding; anything
        # else is corrupt/truncated.
        if n < core_len:
            return ("固件镜像不完整（已损坏或被截断）：文件 %d 字节，预期至少 %d 字节。"
                    "请重新下载并核对发布页的 SHA256。" % (n, core_len))
        if n > core_len + 16:
            return ("固件镜像可能损坏或不完整（长度异常，预期约 %d 字节）。"
                    "请重新下载并核对发布页的 SHA256。" % core_len)

    if n > slot_cap:
        if slot_cap >= 1024 * 1024:
            cap_txt = "约 %d MB" % (slot_cap // (1024 * 1024))
        else:
            cap_txt = "%d KB" % (slot_cap // 1024)
        return ("固件 %d 字节，超出本设备 OTA 槽容量（%s）。\n"
                "请确认选择了对应设备型号的固件；若确为此设备，"
                "请用配网页面 /update 或 USB 刷 merged 整片包。"
                % (n, cap_txt))
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
    "timeout": "设备接收超时，请重试",
    "not_app_image": "设备拒绝：不是 app-only 固件，请使用 ota_*.bin",
    "not_bound": "设备拒绝：当前主机未绑定或无 HID 连接，请先在设备页确认绑定状态",
    "size": "设备拒绝：固件长度与声明不一致",
    "hash": "设备拒绝：SHA256 校验失败，固件可能已损坏",
    "args": "设备拒绝：OTA 参数无效（固件版本过低？请使用配网页面）",
    "begin_failed": "设备拒绝：固件超出 app 分区容量",
    "write_failed": "设备写入失败，请重试",
    "end_failed": "设备写入/激活失败（end_failed），固件已留在 OTA 槽未生效，旧固件仍可启动",
    "busy": "设备正在执行其他 OTA",
    "config_mode": "设备处于配网模式，请退出后重试",
    "chunk_too_big": "块大小超出设备接受范围",
    "not_active": "设备当前没有 OTA 会话",
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
                guidance = "（OTA 分区不可引导：检查 16MB 分区表 app1 槽与实际 flash，" \
                           "或改用配网页面 /update 交叉验证）"
            elif "activate" in detail:
                guidance = "（镜像激活失败：请用 esptool 从 OTA 槽读回比对，" \
                           "或改用配网页面 /update 交叉验证）"
            return f"设备写入/激活失败（end_failed: {detail}）{guidance}"
        return _OTA_ERROR_TEXT.get("end_failed", "设备写入/激活失败，请重试")
    return _OTA_ERROR_TEXT.get(reason, f"设备拒绝: {reason}")


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
        return True, "版本号未变化（可能是同一构建重刷），设备已重启"
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
        add_event("ota", f"OTA 失败: {filename} · {message}", dev_id, level)

    def _run():
        total = window.total
        add_event("ota", f"OTA 开始: {filename} ({total} bytes)", dev_id)

        # 1. CMD:OTA:<size>:<sha256> — arm the device. OTA is BLE-only: the
        #    CFG characteristic switches to binary mode and the WiFi UDP path
        #    has no OTA channel, so never fall back to it.
        if not ble.send(dev_id, f"CMD:OTA:{total}:{sha_hex}"):
            _fail("设备未连接")
            return
        if not _wait_until(lambda: window.ack_line or window.ack_error, OTA_CMD_TIMEOUT):
            _fail("固件版本过低，不支持 OTA 升级。"
                  "请使用配网页面（浏览器打开 http://192.168.4.1 → 固件 OTA 升级）")
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
        add_event("ota", f"OTA 块大小 {chunk_size} B（MTU 窗口 32 KB）", dev_id)
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
                    _fail("推送超时，请重试")
                    return
                continue
            chunk = file_data[sent:sent + chunk_size]
            if not ble.send_raw(dev_id, chunk):
                _fail("连接中断，请重试")
                return
            sent += len(chunk)
            window.sent = sent
        if window.status == "cancelled":
            return

        # Wait for the device to confirm every byte before finalizing.
        if not _wait_until(lambda: window.acked >= total or bool(window.ack_error)
                                   or window.status == "cancelled",
                           OTA_ACK_TIMEOUT):
            _fail("推送超时，请重试")
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
        if not ble.send(dev_id, "CMD:OTA_END"):
            _fail("连接中断，请重试")
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
                add_event("ota",
                          "排查建议：end_failed 是设备侧写入/激活失败（主机已通过"
                          "长度+SHA256 门禁，文件本身不是主因）。1) 查看日志里的 "
                          "end_failed:<detail>(<code>)：read_error(3) 说明 OTA 分区"
                          "不可引导（检查 16MB 分区表 app1 槽与实际 flash 容量），"
                          "activate(9) 说明 esp_image_verify 失败（用 esptool 从 "
                          "OTA 槽读回比对官方包）；2) 改用配网页面 "
                          "http://192.168.4.1 → 固件 OTA 升级交叉验证（同一套 "
                          "Update.end 路径）；3) 或 USB 刷 merged 整片包从 0x0 烧录。",
                          dev_id, "warn")
            _fail(_ota_error_text(err))
            return

        # 4. Prove the deck actually came back on the image we pushed instead
        #    of trusting a BLE reconnect: compare the version / build stamp
        #    reported by VERSION: against the values captured before the push.
        ok, note = _ota_wait_restart(dev_id, old_version, old_build,
                                     expected_version, OTA_VERIFY_TIMEOUT)
        if not ok:
            _fail(f"设备未在 {int(OTA_VERIFY_TIMEOUT)} 秒内确认运行新固件"
                  f"（期望 {expected_version or '新的构建'}），请检查设备是否正常启动")
            return

        window.status = "done"
        got = _ota_current_fw(dev_id).get("version") or expected_version or "?"
        add_event("ota", f"OTA 完成: {filename} ({total} bytes) → v{got}"
                         + (f"（{note}）" if note else ""), dev_id, "ok")

    threading.Thread(target=_run, daemon=True, name=f"ota-{dev_id}").start()
    return window


@app.route("/api/ota/<dev_id>", methods=["POST"])
def api_ota(dev_id):
    """Start a host-driven OTA push for one deck.

    Accepts a multipart ``file`` field (the app-only ``ota_*.bin``). Returns
    immediately with a ``task`` id; poll ``/api/ota/<dev_id>/progress``.
    """
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "未上传固件文件"}), 400
    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"ok": False, "error": "未选择文件"}), 400

    err = _ota_validate(upload.filename)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    file_data = upload.read()
    if not file_data:
        return jsonify({"ok": False, "error": "文件为空"}), 400

    # Pre-flight the image locally so a corrupt / truncated / wrong-kind file is
    # rejected before we spend 1-3 minutes pushing it over BLE. The firmware's
    # Update.end() still verifies the image itself — this is a fast gate.
    fw = _ota_current_fw(dev_id)
    slot_cap = _ota_slot_cap(fw)
    if slot_cap <= 0:
        return jsonify({"ok": False,
                        "error": "本机型不支持上位机 OTA（2 MB 模块无 app1 分区）。"
                                  "请用 USB 刷 merged 整片包从 0x0 烧录，"
                                  "或使用配网页面 http://192.168.4.1 → 固件 OTA 升级。"}), 400
    img_err = _ota_validate_image(file_data, slot_cap)
    if img_err:
        return jsonify({"ok": False, "error": img_err}), 400

    with _ota_lock:
        existing = _ota_windows.get(dev_id)
        if existing is not None and existing.status in ("starting", "uploading"):
            return jsonify({"ok": False, "error": "该设备正在 OTA 中，请勿重复提交"}), 409

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
            return jsonify({"ok": False, "error": "无 OTA 任务"}), 404
        if window.status in ("done", "error", "cancelled"):
            _ota_windows.pop(dev_id, None)
            return jsonify({"ok": True, "sent": False, "status": window.status})
        window.status = "cancelled"
        window.error = "已取消"

    sent = ble.send(dev_id, "CMD:OTA_CANCEL")
    add_event("ota", "OTA 已取消" + ("" if sent else "（取消指令未送达，设备将在 30 秒后自动中止）"),
              dev_id, "warn")
    return jsonify({"ok": True, "sent": bool(sent), "status": "cancelled"})


@app.route("/api/ota/<dev_id>/progress")
def api_ota_progress(dev_id):
    with _ota_lock:
        window = _ota_windows.get(dev_id)
        if window is None:
            return jsonify({"ok": False, "error": "无 OTA 任务"}), 404
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
    }


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
        if "close_button_behavior" in data:
            value = str(data["close_button_behavior"])
            if value not in ("minimize", "exit"):
                value = "minimize"
            with _lock:
                _config["settings"]["close_button_behavior"] = value
            _close_behavior_ref["behavior"] = value
            save_config()
            add_event("desktop",
                      "关闭按钮行为已设为 " + ("最小化到托盘" if value == "minimize" else "直接退出"),
                      level="ok")

        with _lock:
            minimized = bool(_config["settings"].get("start_minimized", True))
        current = autostart.status()
        enabled = bool(current.get("enabled"))
        # 以真实状态为准：config 可能丢失、被手改或从别的机器拷来，只有
        # 当前条目确实是计划任务时才算「管理员模式」，否则一次只改
        # 「启动时最小化」就会把最高权限的任务静默降级成 Run 键。
        admin_pref = bool(current.get("admin"))
        messages: list[str] = []
        rewrite = False
        result: dict | None = None

        if "autostart" in data:
            want = bool(data["autostart"])
            if want:
                if "admin_autostart" in data:
                    admin_pref = bool(data["admin_autostart"])
                rewrite = True
                messages.append("已开启管理员自启动" if admin_pref else "已开启开机自启动")
            else:
                result = autostart.disable()
                if result.get("ok"):
                    admin_pref = False
                    messages.append("已关闭开机自启动")
        elif "admin_autostart" in data:
            # 提权模式变了：注册表 Run 值与计划任务互斥，重写一次。
            admin_pref = bool(data["admin_autostart"])
            # 自启动本来就是关的、又只是「关掉提权」时不去写 Run 值 ——
            # 否则「取消开机自启动」会被这一下顺手打开。
            if admin_pref or enabled:
                rewrite = True
                messages.append("已开启管理员自启动" if admin_pref
                                else "已改为普通权限自启动")
        elif enabled and "start_minimized" in data:
            # 自启动已开、只改了「启动时最小化」：重写条目让它生效。
            rewrite = True

        if rewrite:
            result = autostart.enable(minimized, admin=admin_pref)
        if result is not None and not result.get("ok"):
            # 原样带上后端原因：UAC 被取消、需要管理员账户、schtasks 报错
            # 都不是「重写条目」失败，改写文案会让用户看不懂该做什么。
            reason = str(result.get("error") or "设置失败")
            add_event("desktop", "自启动设置失败：" + reason, level="warn")
            # desktop_payload() also carries an "error" key (the desktop state
            # error), so it is spread *first* - written last, the real reason
            # below would be overwritten by that (usually empty) value and the
            # UI would fall back to the generic "设置失败".
            return jsonify({"ok": False, **desktop_payload(), "error": reason}), 500
        for message in messages:
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
    message = str(data.get("message") or desktop.PORT_BUSY_HINT)
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
        return jsonify({"ok": False, "error": "仅 Windows 支持以管理员身份运行"}), 409
    try:
        import ctypes
    except Exception as e:  # noqa: BLE001 - ctypes is always available on Windows
        return jsonify({"ok": False, "error": f"无法调用系统提权接口：{e}"}), 500
    args = _elevated_argv()
    params = subprocess.list2cmdline(args)
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"提权启动失败：{type(e).__name__}: {e}"}), 500
    if rc is None or rc <= 32:
        # <= 32: the UAC prompt was declined (or the launch failed).
        add_event("desktop", "已取消提权，未能启动管理员实例", level="warn")
        return jsonify({"ok": False, "error": "已取消提权"}), 409
    add_event("desktop", "已启动管理员实例，正在退出当前实例", level="ok")
    # Leave through the existing exit path (server shutdown + window close),
    # never os._exit, so the elevated instance can bind the port.
    if not _exit_for_restart():
        return jsonify({"ok": False, "error": "当前实例无法自动退出，请手动关闭后重试"}), 500
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
        add_event("desktop", "已选择文件: " + path, level="ok")
    return jsonify(result), (200 if result.get("ok") else 503)


@app.route("/api/quit", methods=["POST"])
def api_quit():
    if desktop.request_quit():
        add_event("desktop", "收到退出请求")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "当前不是桌面/托盘模式，无法从页面退出"}), 409


@app.route("/api/show-window", methods=["POST"])
def api_show_window():
    """Restore the native window (recovery path when tray is unavailable).

    The window may have been hidden by ``_on_closing`` when the user clicked X
    with "minimize to tray" checked but no tray icon was available.  The web UI
    at the served URL is then the only way to interact with the app, and this
    endpoint is how the user brings the window back.
    """
    if not desktop.STATE.get("desktop"):
        return jsonify({"ok": False, "error": "当前不是桌面模式"}), 409
    if desktop.show_window():
        add_event("desktop", "已恢复窗口显示")
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "没有可恢复的原生窗口（源码运行或 pywebview 不可用）"}), 409


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
                    "defaults": {"pins": DEFAULT_PINS, "keys": DEFAULT_KEYS}})


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="BindDeckLite", description="BindDeckLite 上位机")
    parser.add_argument("--desktop", "--tray", dest="desktop", action="store_true",
                        help="桌面模式：pywebview 原生窗口 + 托盘（无控制台、无浏览器弹窗）")
    parser.add_argument("--minimized", action="store_true",
                        help="启动时不显示窗口（最小化到托盘）")
    parser.add_argument("--no-tray", dest="no_tray", action="store_true",
                        help="桌面模式但不创建托盘图标")
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
        # Second double-click: focus the running instance instead of failing.
        print(f"[app] already running at {url}, opening browser")
        desktop.open_url(url)
        return 0

    load_config()
    started = ble.start()
    if not started:
        add_event("error", "BLE 不可用: " + str(ble.error), level="warn")
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
