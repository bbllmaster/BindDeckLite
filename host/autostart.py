"""Login autostart for the BindDeckLite host (Windows / Linux / macOS).

Windows uses the per-user ``Run`` registry key; Linux writes an XDG
autostart ``.desktop`` file; macOS writes a LaunchAgent plist.  Every
function is best-effort: callers get ``{"ok": bool, "error": str}`` instead
of an exception, because the UI has to keep working when the platform
refuses the change (locked-down registry, read-only home, ...).
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys

APP_NAME = "BindDeckLite"
VALUE_NAME = "BindDeckLite"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LAUNCH_AGENT = "com.binddecklite.host.plist"
LINUX_DESKTOP_FILE = "binddecklite.desktop"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def launch_command(minimized: bool = True) -> list[str]:
    """Command line that starts the host in desktop (tray) mode."""
    cmd: list[str] = []
    if is_frozen():
        cmd.append(sys.executable)
    else:
        python = sys.executable
        if sys.platform.startswith("win"):
            pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
            if os.path.exists(pythonw):
                python = pythonw
        cmd += [python, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")]
    cmd += ["--desktop"]
    if minimized:
        cmd.append("--minimized")
    return cmd


def _platform() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _method_label() -> str:
    if _platform() == "windows":
        return "注册表 HKCU\\...\\Run"
    if _platform() == "macos":
        return "LaunchAgent com.binddecklite.host"
    return "XDG 自启动 ~/.config/autostart"


def _linux_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart", LINUX_DESKTOP_FILE)


def _macos_path() -> str:
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LAUNCH_AGENT)


# --- windows --------------------------------------------------------------
def _win_read() -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value or "")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def _win_enable(command: str) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)


def _win_disable() -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass


# --- status / toggle ------------------------------------------------------
def status() -> dict:
    """Current autostart state, as shown in the UI."""
    platform = _platform()
    info = {
        "supported": True,
        "enabled": False,
        "method": _method_label(),
        "detail": "",
        "error": "",
    }
    try:
        if platform == "windows":
            value = _win_read()
            info["enabled"] = bool(value)
            info["detail"] = value
        elif platform == "macos":
            path = _macos_path()
            info["enabled"] = os.path.exists(path)
            info["detail"] = path
        else:
            path = _linux_path()
            info["enabled"] = os.path.exists(path)
            info["detail"] = path
    except Exception as e:  # noqa: BLE001
        info["supported"] = False
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def enable(minimized: bool = True) -> dict:
    platform = _platform()
    command = launch_command(minimized)
    try:
        if platform == "windows":
            _win_enable(subprocess.list2cmdline(command))
        elif platform == "macos":
            path = _macos_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            payload = {
                "Label": "com.binddecklite.host",
                "ProgramArguments": command,
                "RunAtLoad": True,
                "KeepAlive": False,
                "ProcessType": "Interactive",
            }
            with open(path, "wb") as f:
                plistlib.dump(payload, f)
        else:
            path = _linux_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            entry = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                f"Name={APP_NAME}\n"
                "Comment=BindDeckLite host console\n"
                f"Exec={shlex.join(command)}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n"
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(entry)
        return {"ok": True, **status()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", **status()}


def disable() -> dict:
    platform = _platform()
    try:
        if platform == "windows":
            _win_disable()
        elif platform == "macos":
            try:
                os.remove(_macos_path())
            except FileNotFoundError:
                pass
        else:
            try:
                os.remove(_linux_path())
            except FileNotFoundError:
                pass
        return {"ok": True, **status()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", **status()}
