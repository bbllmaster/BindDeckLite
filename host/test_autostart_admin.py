"""Unit tests for the elevated ("run as administrator") autostart mode.

These import the **production** functions from ``autostart`` — the schtasks
argument building, the ``/query /xml`` parsing and the Run-key handling all
live in ``autostart.py`` and are exercised here through monkeypatched
``subprocess.run`` / ``winreg`` / platform.  Nothing about the task
construction is re-implemented in this file, so a change in the production
code that breaks the task creation fails these tests.

Run with::

    pytest host/test_autostart_admin.py
"""

import subprocess
import sys
import types

import pytest

import autostart
from autostart import disable, enable, launch_command, status


# --- fake winreg ----------------------------------------------------------
class _FakeKey:
    """Minimal stand-in for a ``winreg`` key object."""

    def __init__(self, store, log):
        self._store = store
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def QueryValueEx(self, name):
        if name not in self._store:
            raise FileNotFoundError(name)
        return self._store[name], 1


@pytest.fixture
def windows(monkeypatch):
    """Pretend to run on Windows; record every winreg/schtasks interaction."""
    store = {}
    log = []
    calls = []
    kwargs_seen = []
    query = {"xml": "", "returncode": 0}
    delete = {"returncode": 0, "stdout": "", "stderr": ""}
    create = {"returncode": 0, "stdout": "", "stderr": ""}

    def _open_key(root, path, reserved=0, access=0):
        if path not in store:
            raise FileNotFoundError(path)
        return _FakeKey(store[path], log)

    def _create_key(root, path, reserved=0, access=0):
        store.setdefault(path, {})
        return _FakeKey(store[path], log)

    def _set_value(key, name, reserved, value_type, value, *extra):
        log.append(("SetValueEx", name, value))
        key._store[name] = value

    def _delete_value(key, name):
        log.append(("DeleteValue", name))
        if name not in key._store:
            raise FileNotFoundError(name)
        del key._store[name]

    module = types.ModuleType("winreg")
    module.HKEY_CURRENT_USER = object()
    module.KEY_SET_VALUE = 0x0002
    module.REG_SZ = 1
    module.OpenKey = _open_key
    module.CreateKeyEx = _create_key
    module.SetValueEx = _set_value
    module.DeleteValue = _delete_value
    monkeypatch.setitem(sys.modules, "winreg", module)

    monkeypatch.setattr(autostart, "_platform", lambda: "windows")

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        kwargs_seen.append(dict(kwargs))
        if cmd[0] == "schtasks":
            verb = cmd[1]
            if verb == "/query":
                return types.SimpleNamespace(returncode=query["returncode"],
                                             stdout=query["xml"], stderr="")
            if verb == "/delete":
                return types.SimpleNamespace(returncode=delete["returncode"],
                                             stdout=delete["stdout"],
                                             stderr=delete["stderr"])
            if verb == "/create":
                return types.SimpleNamespace(returncode=create["returncode"],
                                             stdout=create["stdout"],
                                             stderr=create["stderr"])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return {"store": store, "log": log, "calls": calls, "query": query,
            "delete": delete, "create": create, "kwargs": kwargs_seen}


def _schtasks_calls(windows, verb):
    return [c for c in windows["calls"] if c[0] == "schtasks" and c[1] == verb]


# --- 1. enable(admin=True) builds the logon task -------------------------
def test_enable_admin_creates_highest_privilege_task(windows):
    """``enable(admin=True)`` asks schtasks for an onlogon/highest task."""
    result = enable(admin=True)

    assert result["ok"] is True
    creates = _schtasks_calls(windows, "/create")
    assert creates, "schtasks /create was not called"

    cmd = creates[0]
    assert cmd[cmd.index("/tn") + 1] == "BindDeckLite"
    assert cmd[cmd.index("/sc") + 1] == "onlogon"
    assert cmd[cmd.index("/rl") + 1] == "highest"
    assert "/f" in cmd
    # The task runs the same command launch_command() produces.
    assert "app.py" in cmd[cmd.index("/tr") + 1]


# --- 2. enable(admin=True) removes the Run value first --------------------
def test_enable_admin_removes_run_value(windows):
    """The Run key and the scheduled task are mutually exclusive."""
    run_key = autostart.RUN_KEY
    windows["store"][run_key] = {autostart.VALUE_NAME: "old-command"}

    result = enable(admin=True)

    assert result["ok"] is True
    # The stale Run value is deleted (mock winreg.DeleteValue was called) ...
    deletes = [e for e in windows["log"] if e[0] == "DeleteValue"]
    assert deletes, "the Run value was not deleted"
    assert autostart.VALUE_NAME not in windows["store"].get(run_key, {})
    # ... and no Run value was written back.
    assert not [e for e in windows["log"] if e[0] == "SetValueEx"]


def test_enable_without_admin_uses_run_key_and_drops_task(windows):
    """``admin=False`` keeps the plain Run key and removes a leftover task."""
    run_key = autostart.RUN_KEY
    windows["store"][run_key] = {autostart.VALUE_NAME: "old-command"}

    result = enable(admin=False)

    assert result["ok"] is True
    assert autostart.VALUE_NAME in windows["store"][run_key]
    assert _schtasks_calls(windows, "/delete"), "the leftover task was not removed"
    assert not _schtasks_calls(windows, "/create")


# --- 3. status() parses the task XML --------------------------------------
TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>BindDeckLite</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>DOMAIN\\user</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\\host\\app.py --desktop --minimized</Command>
      <Arguments></Arguments>
    </Exec>
  </Actions>
</Task>
"""


def test_status_reads_highest_available_from_task_xml(windows):
    """``status()`` detects the elevated mode from the task XML."""
    windows["query"]["xml"] = TASK_XML
    windows["query"]["returncode"] = 0

    info = status()

    assert info["admin"] is True
    assert info["admin_supported"] is True
    assert info["admin_error"] == ""
    assert info["enabled"] is True
    assert info["method"] == "计划任务（最高权限）"


def test_status_without_task_is_not_admin_and_has_no_error(windows):
    """No task registered -> not the elevated mode, and no error either."""
    windows["query"]["xml"] = "ERROR: The system cannot find the file specified.\r\n"
    windows["query"]["returncode"] = 1

    info = status()

    assert info["admin"] is False
    assert info["admin_error"] == ""
    assert info["enabled"] is False


# --- 4. disable() clears both mechanisms ----------------------------------
def test_disable_removes_run_value_and_task(windows):
    """Turning autostart off clears the Run value *and* the scheduled task."""
    run_key = autostart.RUN_KEY
    windows["store"][run_key] = {autostart.VALUE_NAME: "old-command"}

    result = disable()

    assert result["ok"] is True
    assert autostart.VALUE_NAME not in windows["store"].get(run_key, {})
    deletes = _schtasks_calls(windows, "/delete")
    assert deletes, "schtasks /delete was not called"
    assert deletes[0][deletes[0].index("/tn") + 1] == "BindDeckLite"


# --- 5. non-Windows ignores the admin flag -------------------------------
def test_non_windows_ignores_admin_flag(monkeypatch, tmp_path):
    """On Linux/macOS the admin flag is ignored and nothing raises."""
    monkeypatch.setattr(autostart, "_platform", lambda: "linux")
    monkeypatch.setattr(autostart, "_linux_path",
                        lambda: str(tmp_path / "binddecklite.desktop"))

    result = enable(admin=True)

    assert result["ok"] is True
    info = status()
    assert info["admin_supported"] is False
    assert info["admin"] is False
    assert info["is_admin"] is False


def test_launch_command_is_unchanged_by_the_admin_mode():
    """``launch_command`` stays the single source for the task's /tr value."""
    cmd = launch_command(minimized=True)
    assert "--desktop" in cmd
    assert "--minimized" in cmd


# --- 6. a failed task delete must not silently leave both entries --------
def test_enable_without_admin_aborts_when_task_delete_fails(windows):
    """`schtasks /delete` failing must not write the Run value.

    A leftover task plus a fresh Run value would start two instances at
    logon and fight for the port - and previously that happened silently
    because the delete result was dropped.
    """
    run_key = autostart.RUN_KEY
    windows["store"][run_key] = {autostart.VALUE_NAME: "old-command"}
    windows["delete"].update(returncode=1, stderr="ERROR: Access is denied.")

    result = enable(admin=False)

    assert result["ok"] is False
    assert "Access is denied." in result["error"]
    # The Run value must not be (re)written while the task is still there.
    assert not [e for e in windows["log"] if e[0] == "SetValueEx"]
    assert windows["store"][run_key][autostart.VALUE_NAME] == "old-command"


# --- 7. schtasks is never run through a shell ---------------------------
def test_schtasks_is_run_without_a_shell(windows):
    """Arguments go as a list; ``shell=True`` would re-parse the command."""
    enable(admin=True)
    disable()

    assert windows["kwargs"], "subprocess.run was never called"
    for kwargs in windows["kwargs"]:
        assert "shell" not in kwargs, "schtasks must not be run through a shell"


# --- 8. UTF-16 task XML is decoded ------------------------------------
def test_status_decodes_utf16_task_xml(windows):
    """``schtasks /query /xml`` is UTF-16; non-ASCII paths must survive."""
    xml = TASK_XML.replace("C:\\host\\app.py", "C:\\程序 目录\\app.py")
    windows["query"]["xml"] = b"\xff\xfe" + xml.encode("utf-16-le")

    info = status()

    assert info["admin"] is True
    assert "程序 目录" in info["detail"]

