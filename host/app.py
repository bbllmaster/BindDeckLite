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
import hashlib
import json
import os
import queue
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
from ble_link import BleLinkManager

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
ble = BleLinkManager(on_line=lambda dev_id, line: _on_line(dev_id, line),
                     cache_path=os.path.join(DATA_DIR, "ble_cache.json"))


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
                         "build": fields[3] if len(fields) > 3 else ""}
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
            "chip": cfg.get("chip", ""),
            "fw": cfg.get("fw", {}),
            "bond": cfg.get("bond", {"bound": False, "addr": ""}),
        })
    return out


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


def run_action(action: dict) -> dict:
    """Execute one host-side action and return a structured result.

    Returns ``{ok: bool, error: str, action_type: str, target: str}`` where
    ``error`` is a human-readable failure reason (empty when ``ok`` is True).
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
            for step in action.get("macro", []):
                st = step.get("type")
                if st == "key":
                    kb.send(step.get("value", ""))
                elif st == "text":
                    kb.write(step.get("value", ""))
                elif st == "delay":
                    time.sleep(max(0, int(step.get("value", 0))) / 1000.0)
                elif st == "url":
                    # Empty url: skip silently (no exception).  Non-empty url:
                    # open the browser; subsequent text:/key: steps can fill in
                    # the page that loads (login flows, etc.).
                    url_val = step.get("value", "")
                    if url_val:
                        webbrowser.open(url_val)
            return {"ok": True, "error": "", "action_type": kind, "target": target}
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

    def _do():
        result = run_action(action)
        _record_action_result(dev_id, idx, action, result)

    threading.Thread(target=_do, daemon=True).start()


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
        "event_seq": _event_seq,
    })


@app.route("/api/defaults")
def api_defaults():
    return jsonify(new_device_cfg("ESP32", ""))


@app.route("/api/scan")
def api_scan():
    found = ble.discover(timeout=6.0)
    return jsonify({"ok": True, "found": found})


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

    # Execute synchronously (5 s cap) so the caller gets the real result.
    result_queue: "queue.Queue" = queue.Queue()

    def _worker():
        try:
            result_queue.put(run_action(action))
        except Exception as e:  # noqa: BLE001
            result_queue.put({"ok": False, "error": f"{type(e).__name__}: {e}",
                              "action_type": action.get("type", ""),
                              "target": _truncate(action.get("value", ""))})

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    try:
        result = result_queue.get(timeout=5.0)
    except queue.Empty:
        return jsonify({"ok": False, "error": "执行超时"}), 200

    _record_action_result(dev_id, idx, action, result)
    return jsonify({"ok": result["ok"], "error": result["error"]})


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
    "end_failed": "设备校验固件失败，请重试",
    "busy": "设备正在执行其他 OTA",
    "config_mode": "设备处于配网模式，请退出后重试",
    "chunk_too_big": "块大小超出设备接受范围",
    "not_active": "设备当前没有 OTA 会话",
}


def _ota_error_text(ack: str) -> str:
    reason = ack[len("ACK:OTA:ERROR:"):] if ack.startswith("ACK:OTA:ERROR:") else ack
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
    return {"version": fw.get("version", ""), "build": fw.get("build", "")}


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

    # Firmware images built by tools/build_release.py:
    #   ota_*.bin     — ESP app image: 0xE9 magic at 0 and esp_app_desc_t
    #                   (0xABCD5432) at offset 0x20
    #   merged_*.bin  — bootloader + partitions + otadata + app: starts with
    #                   the bootloader, so offset 0x20 is NOT an app descriptor
    # The header check is the reliable discriminator; the firmware's
    # Update.end() CRC check stays the authoritative one.
    if len(file_data) < 0x30 or file_data[0] != 0xE9:
        return jsonify({"ok": False, "error": "不是 ESP32 固件镜像"}), 400
    if file_data[0x20:0x24] != b"\x32\x54\xcd\xab":
        return jsonify({"ok": False,
                        "error": "请上传 app-only 固件（ota_*.bin），不要上传 merged 整片包"}), 400
    if len(file_data) > 4 * 1024 * 1024:
        return jsonify({"ok": False, "error": "固件过大，超出 app 分区容量"}), 400

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
