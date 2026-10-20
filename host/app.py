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

Run:  python app.py            (opens http://127.0.0.1:8788)
"""

from __future__ import annotations

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

from ble_link import BleLinkManager

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
PORT = int(os.environ.get("BINDDECKLITE_PORT", "8788"))

# Same vendor service as the firmware's ble_config.cpp (shared protocol).
CONTROL_SERVICE = "b1d3c0de-0001-4a5b-9c6d-1a2b3c4d5e6f"


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

app = Flask(__name__,
            static_folder=os.path.join(RESOURCE_ROOT, "static"),
            template_folder=os.path.join(RESOURCE_ROOT, "templates"))
app.config["JSON_AS_ASCII"] = False

_lock = threading.RLock()
_events = deque(maxlen=400)
_event_seq = 0
_last_press = {}          # dev_id -> (index, timestamp)
_ping_sent = {}           # dev_id -> monotonic send time
_udp_sock = None
_config = {"devices": {}, "settings": {"actions_enabled": True, "udp_fallback": True}}

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
                     cache_path=os.path.join(APP_DIR, "ble_cache.json"))


def _on_line(dev_id: str, line: str):
    dev_id = dev_id or ""
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
    if line.startswith("ACK:"):
        add_event("ack", "设备确认: " + line[len("ACK:"):][:60], dev_id, "ok")
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
        out.append({
            "dev_id": dev_id,
            "name": cfg.get("name") or info.get("name") or dev_id,
            "address": info.get("address", ""),
            "connected": bool(info.get("connected")),
            "chip": cfg.get("chip", ""),
            "fw": cfg.get("fw", {}),
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
    results = []
    pins = ",".join(str(int(p)) for p in cfg.get("pins", [])[:9])
    keys = ",".join(str(int(k["code"])) for k in cfg.get("keys", [])[:10])
    if len(pins.split(",")) == 9:
        results.append(send(dev_id, "CFG:PINS:" + pins))
    if len(keys.split(",")) == 10:
        results.append(send(dev_id, "CFG:KEYS:" + keys))
    results.append(send(dev_id, f"CFG:ENC:{int(cfg['encoder'].get('mode', 0))}"))
    sleep = cfg.get("sleep", {})
    results.append(send(dev_id, "CFG:SLEEP:%d,%d" % (
        1 if sleep.get("enabled", True) else 0, max(1, int(sleep.get("minutes", 5))))))
    if cfg.get("tz"):
        results.append(send(dev_id, "CFG:TZ:" + cfg["tz"]))
    return {"ok": any(r["sent"] for r in results), "results": results}


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
        return jsonify({"ok": True})
    if action == "ping":
        _ping_sent[dev_id] = time.time()
        return jsonify(send(dev_id, "CMD:PING"))
    return jsonify({"ok": False, "error": "unknown action"}), 404


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json(silent=True) or {}
    with _lock:
        for k in ("actions_enabled", "udp_fallback"):
            if k in data:
                _config["settings"][k] = bool(data[k])
        save_config()
    return jsonify({"ok": True, "settings": _config["settings"]})


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


def main():
    load_config()
    started = ble.start()
    if not started:
        add_event("error", "BLE 不可用: " + str(ble.error), level="warn")
        print("[app] BLE unavailable:", ble.error)
    url = f"http://127.0.0.1:{PORT}"
    print(f"[app] BindDeckLite host running at {url}")
    if os.environ.get("BINDDECKLITE_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
