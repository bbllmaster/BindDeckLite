"""Login autostart for the BindDeckLite host (Windows / Linux / macOS).

Windows uses the per-user ``Run`` registry key; Linux writes an XDG
autostart ``.desktop`` file; macOS writes a LaunchAgent plist.  Every
function is best-effort: callers get ``{"ok": bool, "error": str}`` instead
of an exception, because the UI has to keep working when the platform
refuses the change (locked-down registry, read-only home, ...).

Windows can additionally start the host *elevated*.  The ``Run`` key can
never raise the integrity level, so that mode uses a logon scheduled task
(``schtasks /create /sc onlogon /rl highest``) instead: the task starts
with the highest privileges the account has, without a UAC prompt.  The two
mechanisms are mutually exclusive - turning one on removes the other.
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import subprocess
import sys

APP_NAME = "BindDeckLite"
VALUE_NAME = "BindDeckLite"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LAUNCH_AGENT = "com.binddecklite.host.plist"
LINUX_DESKTOP_FILE = "binddecklite.desktop"

# Name of the logon task used for the elevated ("run as administrator")
# autostart mode.  ``schtasks`` stores it verbatim.
TASK_NAME = "BindDeckLite"

# ``subprocess.CREATE_NO_WINDOW`` only exists on Windows; resolving it
# defensively keeps this module importable (and testable) elsewhere while
# still hiding the console window on Windows.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ``schtasks`` reports a missing task in the console language of the
# machine; these markers cover the English and the Chinese wording.
_TASK_MISSING_MARKERS = (
    "cannot find", "not found", "no tasks", "找不到", "不存在", "系统找不到",
)

_COMMAND_RE = re.compile(r"<Command>(.*?)</Command>", re.S)


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


def is_elevated() -> bool:
    """Whether *this* process already runs with administrator rights."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - ctypes/windll are Windows-only
        return False


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


# --- scheduled task (elevated autostart) ----------------------------------
def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess:
    """Run ``schtasks.exe`` with *args*.

    The argument list is passed without a shell, so a command containing
    spaces or quotes reaches ``schtasks`` unchanged.
    """
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
    )


def _xml_text(raw: str) -> str:
    """Normalise ``schtasks`` XML (it is written as UTF-16)."""
    return raw.replace("\x00", "").lstrip("﻿")


def _task_missing(text: str) -> bool:
    """Whether *text* says "there is no such task" (not a real failure)."""
    low = text.lower()
    return any(marker in low or marker in text for marker in _TASK_MISSING_MARKERS)


def _task_create(command: str) -> tuple[bool, str]:
    """Create the logon task that runs *command* with the highest rights.

    ``/rl highest`` stores ``<RunLevel>HighestAvailable</RunLevel>`` in the
    task, which is what makes the host start elevated at logon.
    """
    try:
        proc = _run_schtasks([
            "/create",
            "/tn", TASK_NAME,
            "/tr", command,
            "/sc", "onlogon",
            "/rl", "highest",
            "/f",
        ])
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + " " + (proc.stdout or "")).strip()
        return False, detail or f"schtasks /create 退出码 {proc.returncode}"
    return True, ""


def _task_delete() -> tuple[bool, str]:
    """Delete the logon task.  A task that is not there is not a failure."""
    try:
        proc = _run_schtasks(["/delete", "/tn", TASK_NAME, "/f"])
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + " " + (proc.stdout or "")).strip()
        if not detail or _task_missing(detail):
            return True, ""
        return False, detail
    return True, ""


def _task_query() -> dict:
    """Inspect the logon task.

    Returns a dict with ``supported`` (schtasks could be called at all),
    ``exists`` (the task is registered), ``enabled`` (the task itself is
    enabled), ``highest`` (it runs with the highest privileges),
    ``command`` (what it starts) and ``error`` (a readable reason when the
    query itself failed).

    ``schtasks`` exits non-zero when the task does not exist.  That is the
    normal case for a user who never turned the elevated mode on, so it is
    reported as "not present" rather than as an error.
    """
    info = {"supported": False, "exists": False, "enabled": False,
            "highest": False, "command": "", "error": ""}
    try:
        proc = _run_schtasks(["/query", "/tn", TASK_NAME, "/xml"])
    except OSError as e:
        info["error"] = f"{type(e).__name__}: {e}"
        return info
    info["supported"] = True
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + " " + (proc.stdout or "")).strip()
        # A missing task is the normal case (the user never turned the
        # elevated mode on) and must not be reported as an error.
        if detail and not _task_missing(detail):
            info["error"] = detail
        return info
    xml = _xml_text(proc.stdout or "")
    if not xml.strip():
        return info
    info["exists"] = True
    info["enabled"] = "<Enabled>true</Enabled>" in xml
    info["highest"] = "<RunLevel>HighestAvailable</RunLevel>" in xml
    match = _COMMAND_RE.search(xml)
    if match:
        info["command"] = match.group(1).strip()
    return info


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
        # Elevated ("run as administrator") autostart - see TASK_NAME.
        "admin": False,
        "admin_supported": False,
        "is_admin": False,
        "admin_error": "",
    }
    try:
        if platform == "windows":
            value = _win_read()
            info["enabled"] = bool(value)
            info["detail"] = value
            info["is_admin"] = is_elevated()
            task = _task_query()
            info["admin_supported"] = task["supported"]
            info["admin_error"] = task["error"]
            if task["exists"] and (task["highest"] or task["enabled"]):
                # The scheduled task is the autostart entry in this mode.
                info["admin"] = True
                info["enabled"] = True
                info["method"] = "计划任务（最高权限）" if task["highest"] else "计划任务"
                if not value:
                    info["detail"] = task["command"] or f"计划任务 {TASK_NAME}"
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


def enable(minimized: bool = True, admin: bool = False) -> dict:
    """Turn autostart on.

    ``admin=True`` uses a logon scheduled task with the highest privileges
    instead of the registry ``Run`` key (Windows only).  On other platforms
    the flag is ignored and the normal mechanism is used.
    """
    platform = _platform()
    command = launch_command(minimized)
    try:
        if platform == "windows":
            if admin:
                # The task replaces the Run entry - the two must not both be
                # active, otherwise the host would start twice.
                _win_disable()
                ok, err = _task_create(subprocess.list2cmdline(command))
                if not ok:
                    # Put the Run entry back so a transient schtasks failure
                    # does not silently drop the autostart.
                    try:
                        _win_enable(subprocess.list2cmdline(command))
                    except Exception:  # noqa: BLE001
                        pass
                    # status() also carries an "error" key - spread it first so
                    # the real reason is not overwritten.
                    return {"ok": False, **status(), "error": err}
            else:
                # Best effort: drop a task left over from the admin mode.
                _task_delete()
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
        return {"ok": False, **status(), "error": f"{type(e).__name__}: {e}"}


def disable() -> dict:
    """Turn autostart off (registry entry *and* scheduled task)."""
    platform = _platform()
    try:
        if platform == "windows":
            _win_disable()
            ok, err = _task_delete()
            if not ok:
                return {"ok": False, **status(), "error": err}
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
        return {"ok": False, **status(), "error": f"{type(e).__name__}: {e}"}
