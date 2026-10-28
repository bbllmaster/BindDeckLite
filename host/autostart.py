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
mechanisms are mutually exclusive - turning one on removes the other.  The
task store is machine-wide, so creating or removing the task from a
non-elevated host raises a UAC prompt on demand.
"""

from __future__ import annotations

import locale
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


# --- UAC elevation for the scheduled task ---------------------------------
# ``schtasks /sc onlogon /rl highest`` writes into the machine task store and
# therefore needs administrator rights — but a non-elevated process cannot ask
# for them through ``subprocess``: creating the task *is* the privileged
# operation.  The only way through is a shell "runas" child, i.e. a UAC prompt
# on demand when the user turns the mode on or off.
_SEE_MASK_NOCLOSEPROCESS = 0x40
# The caller is a werkzeug worker thread (``threaded=True``) with no message
# loop.  MS documents SEE_MASK_NOASYNC as required for exactly that case:
# without it ShellExecuteEx may return before the elevated child has started,
# leaving ``hProcess`` not ready to wait on.
_SEE_MASK_NOASYNC = 0x100
_SW_HIDE = 0
_ERROR_CANCELLED = 1223
# "The requested operation requires elevation" - a standard account with UAC
# turned off cannot answer the prompt at all.
_ERROR_ELEVATION_REQUIRED = 740
_WAIT_OBJECT_0 = 0
_ELEVATED_TIMEOUT_MS = 120000


def _schtasks_exe() -> str:
    """Full path of ``schtasks.exe`` (a "runas" call does not search PATH)."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(root, "System32", "schtasks.exe")


def _elevated_schtasks(args: list[str]) -> tuple[bool, str]:
    """Run ``schtasks`` with *args* behind a UAC prompt.

    ``ShellExecuteExW`` is used instead of ``subprocess.run`` because only the
    shell can raise the integrity level ("runas").  The child gets no stdout
    pipe, so its exit code is the only result channel; failures are reported
    with a fixed, readable message instead of a captured stderr.
    """
    create = bool(args) and args[0] == "/create"

    def _failed(detail: str) -> tuple[bool, str]:
        if create:
            return False, f"创建计划任务失败（{detail}），可能需要管理员账户"
        return False, f"删除计划任务失败（{detail}）"

    def _after_timeout() -> tuple[bool, str]:
        """Decide a timed-out elevation by looking at the task store.

        The 120 s wait can expire while the UAC prompt is still on screen: the
        user clicks "是" after we gave up, the task does get created, and the
        autostart entry we kept (the Run value) would start a second instance
        at the next logon.  Re-query before calling it a failure.
        """
        task = _task_query()
        if not task["supported"] or task["error"]:
            return _failed("等待超时")
        if create:
            return (True, "") if task["exists"] else _failed("等待超时")
        return (True, "") if not task["exists"] else _failed("等待超时")

    if not sys.platform.startswith("win"):
        return False, "当前平台不支持 UAC 提权"
    try:
        import ctypes
        from ctypes import wintypes

        class _SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("fMask", wintypes.DWORD),
                ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD),
                ("hIcon", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell_execute = shell32.ShellExecuteExW
        shell_execute.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
        shell_execute.restype = wintypes.BOOL
        wait_for = kernel32.WaitForSingleObject
        wait_for.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for.restype = wintypes.DWORD
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        info = _SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(info)
        info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC
        info.lpVerb = "runas"
        info.lpFile = _schtasks_exe()
        info.lpParameters = subprocess.list2cmdline(args)
        info.nShow = _SW_HIDE
        if not shell_execute(ctypes.byref(info)):
            err = ctypes.get_last_error()
            if err == _ERROR_CANCELLED:
                return False, "已取消提权"
            if err == _ERROR_ELEVATION_REQUIRED:
                # Nothing the user can click: the account cannot elevate while
                # UAC is off, so say what to fix instead of a bare Win32 code.
                return _failed("需要管理员权限（740）")
            return _failed(f"ShellExecuteEx 失败: {err}")
        handle = info.hProcess
        if not handle:
            return _failed("没有取得进程句柄")
        try:
            if wait_for(handle, _ELEVATED_TIMEOUT_MS) != _WAIT_OBJECT_0:
                return _after_timeout()
            code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(code)):
                return _failed("无法读取退出码")
            if code.value != 0:
                return _failed(f"退出码 {code.value}")
        finally:
            close_handle(handle)
    except Exception as e:  # noqa: BLE001 - ctypes/WinDLL are Windows-only
        return False, f"{type(e).__name__}: {e}"
    return True, ""


# --- scheduled task (elevated autostart) ----------------------------------
def _decode_output(raw: bytes | str) -> str:
    """Decode ``schtasks`` output.

    ``schtasks /query /xml`` writes UTF-16 (with a BOM when redirected), the
    console messages use the OEM code page.  ``text=True`` would decode both
    as the ANSI code page and garble non-ASCII paths, so the raw bytes are
    decoded here instead.
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.count(b"\x00") > len(raw) // 4:
        # UTF-16LE without a BOM.
        return raw.decode("utf-16-le", errors="replace")
    for encoding in ("utf-8", locale.getpreferredencoding(False), "mbcs"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess:
    """Run ``schtasks.exe`` with *args*.

    The argument list is passed without a shell, so a command containing
    spaces or quotes reaches ``schtasks`` unchanged.
    """
    proc = subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    return subprocess.CompletedProcess(
        args=["schtasks", *args],
        returncode=proc.returncode,
        stdout=_decode_output(proc.stdout),
        stderr=_decode_output(proc.stderr),
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
    task, which is what makes the host start elevated at logon.  The task
    store is machine-wide, so a non-elevated process has to go through UAC.
    """
    args = [
        "/create",
        "/tn", TASK_NAME,
        "/tr", command,
        "/sc", "onlogon",
        "/rl", "highest",
        "/f",
    ]
    if not is_elevated():
        return _elevated_schtasks(args)
    try:
        proc = _run_schtasks(args)
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + " " + (proc.stdout or "")).strip()
        return False, detail or f"schtasks /create 退出码 {proc.returncode}"
    return True, ""


def _task_delete() -> tuple[bool, str]:
    """Delete the logon task.  A task that is not there is not a failure."""
    args = ["/delete", "/tn", TASK_NAME, "/f"]
    if not is_elevated():
        # A task registered with "highest" cannot be removed by a non-elevated
        # caller either, so the delete goes through UAC as well - but only
        # when there is something to delete, otherwise the user would face a
        # pointless prompt every time autostart is turned off.
        existing = _task_query()
        if existing["supported"] and not existing["exists"] and not existing["error"]:
            return True, ""
        return _elevated_schtasks(args)
    try:
        proc = _run_schtasks(args)
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
                # Create the task *first*: the operation can be refused (UAC
                # cancelled, standard account) and the existing Run value must
                # survive that, otherwise turning the mode on would silently
                # drop autostart.  Once the task exists the Run entry is
                # removed - the two must not both be active, otherwise the
                # host would start twice.
                ok, err = _task_create(subprocess.list2cmdline(command))
                if not ok:
                    # status() also carries an "error" key - spread it first so
                    # the real reason is not overwritten.
                    return {"ok": False, **status(), "error": err}
                _win_disable()
            else:
                # The two mechanisms are mutually exclusive: a leftover task
                # and the Run value would both start at logon and fight for
                # the port.  A delete failure must abort *before* the Run
                # value is written, otherwise the duplicate is silent.
                ok, err = _task_delete()
                if not ok:
                    return {"ok": False, **status(), "error": err}
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
            # Drop the task first: it may need a UAC prompt that the user can
            # cancel, and a cancelled prompt must leave autostart untouched
            # instead of clearing the Run value and keeping the task alive.
            ok, err = _task_delete()
            if not ok:
                return {"ok": False, **status(), "error": err}
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
        return {"ok": False, **status(), "error": f"{type(e).__name__}: {e}"}
