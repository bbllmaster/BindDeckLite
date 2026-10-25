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
_ping_sent = {}           # dev_id -> monotonic send time
_bond_queried = set()     # dev_ids whose binding status we already asked for
_udp_sock = None
_config = {"devices": {}, "settings": {
    "actions_enabled": True,
    "udp_fallback": True,
    "start_minimized": True,
    "close_button_behavior": "minimize",
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
        "encoder": {"mode": 0},
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
                window.on_readback(field, line[len("CFG_") + len(prefix) + 1:])
                return
        # Any other line during the readback window is ignored on purpose:
        # no silent overwrite of the local config while we compare.
        return

    if line.startswith("CFG_KEYS:"):
        vals = [v for v in line[len("CFG_KEYS:"):].split(",") if v.strip()]
        try:
            codes = [int(v.strip()) for v in vals]
        except ValueError:
            return
        cfg = device_cfg(dev_id)
        with _lock:
            for i, code in enumerate(codes[:10]):
                cfg["keys"][i]["code"] = code
                label = next((lbl for lbl, c in KEY_CODES if c == code), str(code))
                cfg["keys"][i]["label"] = label
            save_config()
        add_event("cfg", "已读取设备按键配置", dev_id)
        return
    if line.startswith("CFG_PINS:"):
        vals = [v for v in line[len("CFG_PINS:"):].split(",") if v.strip()]
        try:
            pins = [int(v.strip()) for v in vals]
        except ValueError:
            return
        if len(pins) == 9:
            cfg = device_cfg(dev_id)
            with _lock:
                cfg["pins"] = pins
                save_config()
        return
    if line.startswith("CFG_ENC:"):
        try:
            mode = int(line[len("CFG_ENC:"):])
        except ValueError:
            return
        cfg = device_cfg(dev_id)
        with _lock:
            cfg["encoder"]["mode"] = mode
            save_config()
        return
    if line.startswith("CFG_SLEEP:"):
        payload = line[len("CFG_SLEEP:"):].split(",")
        try:
            enabled = payload[0].strip() == "1"
            mins = int(payload[1]) if len(payload) > 1 else 5
        except (ValueError, IndexError):
            return
        cfg = device_cfg(dev_id)
        with _lock:
            cfg["sleep"] = {"enabled": enabled, "minutes": mins}
            save_config()
        return
    if line.startswith("CFG_TZ:"):
        cfg = device_cfg(dev_id)
        with _lock:
            cfg["tz"] = line[len("CFG_TZ:"):]
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
        payload = line[len("BOND_STATUS:"):]
        cfg = device_cfg(dev_id)
        with _lock:
            if payload == "unbound":
                cfg["bond"] = {"bound": False, "addr": ""}
            else:
                parts = payload.split(",", 1)
                cfg["bond"] = {
                    "bound": True,
                    "addr": parts[1] if len(parts) == 2 else "",
                }
            save_config()
        add_event("device",
                  "主机绑定: " + ("已绑定" if cfg["bond"]["bound"] else "未绑定"),
                  dev_id)
        return
    if line.startswith("PONG"):
        sent = _ping_sent.pop(dev_id, None)
        rtt = int((time.time() - sent) * 1000) if sent else 0
        add_event("pong", f"设备响应 {rtt} ms", dev_id, "ok")
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
    target = _truncate(value)

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
        try:
            # ``browser:`` steps choose the target for later ``url:`` steps;
            # None means "system default browser". Reset on a failed open so a
            # broken target does not break every following url.
            browser_target = None
            browser_used = "default"
            for step in action.get("macro", []):
                st = step.get("type")
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
                        if open_err:
                            add_event(
                                "error",
                                f"未能用 {browser_used} 打开 {url_val}"
                                f"（{open_err}），已回退默认浏览器",
                                level="error")
                            browser_target = None
                            browser_used = "default"
                        elif browser_used == "default":
                            add_event("action", f"已用默认浏览器打开 {url_val}",
                                      level="ok")
                        else:
                            add_event("action",
                                      f"已用 {browser_used} 打开 {url_val}",
                                      level="ok")
                        # Keep the freshly opened window in front, otherwise
                        # the next text:/paste: steps land in the old window.
                        # Windows only; bounded by MACRO_URL_FOCUS_TIMEOUT.
                        _wait_url_foreground()
            return {"ok": True, "error": "", "action_type": kind,
                    "target": target, "browser_used": browser_used}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "action_type": kind, "target": target}

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


def _record_action_result(dev_id: str, idx: int, action: dict, result: dict):
    """Append a ``kind=action`` result row to the event log.

    Success rows use ``level=ok``; failure rows use ``level=error`` and also
    trigger a toast so the user sees the failure immediately.
    """
    label = _action_label(idx)
    atype = result.get("action_type", (action or {}).get("type", "unknown"))
    target = result.get("target", "")
    if result["ok"]:
        add_event("action", f"动作成功: {label} · {atype} · {target}", dev_id, "ok")
    else:
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
        "event_seq": _event_seq,
        # WS-395: additive field — the last scan's conclusion ("why is no
        # device found?"). ``at`` is None until the first scan finished.
        "scan": _scan_state(),
    })


@app.route("/api/defaults")
def api_defaults():
    return jsonify(new_device_cfg("ESP32", ""))


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
    return jsonify(apply_device(dev_id))


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
                              "target": _truncate(action.get("value", ""))})
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


@app.route("/api/device/<dev_id>/<action>", methods=["POST"])
def api_device_action(dev_id, action):
    if action == "reboot":
        return jsonify(send(dev_id, "CMD:REBOOT"))
    if action == "portal":
        return jsonify(send(dev_id, "CMD:BOOT_CFG"))
    if action == "refresh":
        send(dev_id, "CMD:VERSION")
        send(dev_id, "CMD:GET_CFG")
        send(dev_id, "CMD:GET_BOND")   # WS-321
        return jsonify({"ok": True})
    if action == "ping":
        _ping_sent[dev_id] = time.time()
        return jsonify(send(dev_id, "CMD:PING"))
    if action == "clearbond":
        # WS-321: drop the persisted host binding so a new central can become
        # the HID primary. The device ACKs on the same CFG channel.
        return jsonify(send(dev_id, "CMD:CLEARBOND"))
    return jsonify({"ok": False, "error": "unknown action"}), 404


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
        if "start_minimized" in data:
            value = bool(data["start_minimized"])
            with _lock:
                _config["settings"]["start_minimized"] = value
            save_config()
            # Keep the login entry in sync when autostart is already enabled.
            if autostart.status().get("enabled"):
                result = autostart.enable(minimized=value)
                if not result.get("ok"):
                    add_event("desktop", "重写自启动条目失败: " + str(result.get("error", "?")),
                              level="warn")
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
        if "autostart" in data:
            want = bool(data["autostart"])
            with _lock:
                minimized = bool(_config["settings"].get("start_minimized", True))
            result = autostart.enable(minimized) if want else autostart.disable()
            if not result.get("ok"):
                return jsonify({"ok": False, "error": result.get("error", "设置失败"),
                                **desktop_payload()}), 500
            add_event("desktop", "已开启开机自启动" if want else "已关闭开机自启动", level="ok")
    return jsonify({"ok": True, **desktop_payload()})


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
    if desktop_mode and desktop.port_in_use(PORT):
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
                                   log_path=os.path.join(DATA_DIR, "host.log"))

    if os.environ.get("BINDDECKLITE_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
