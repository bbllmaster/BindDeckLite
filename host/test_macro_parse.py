"""Unit tests for the ``app:`` macro step helpers — pure-function coverage.

These test the command-line parsing and exe-resolution logic behind the new
``app:<exe> [args]`` macro step.  No GUI, no keyboard library, no Win32 — the
helpers are pure Python and run anywhere::

    pytest host/test_macro_parse.py -v
"""

import os
import shlex
import shutil
import sys
from unittest import mock

import pytest

# host/ is not a package on its own; import the module by path.
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import app as host_app


# ---------------------------------------------------------------------------
# _resolve_exe
# ---------------------------------------------------------------------------

def test_resolve_exe_full_path(tmp_path):
    """A full path to an existing file is returned as-is."""
    exe = tmp_path / "myapp.exe"
    exe.write_text("dummy")
    assert host_app._resolve_exe(str(exe)) == str(exe)


def test_resolve_exe_relative_path(tmp_path):
    """A relative path that exists is returned (exists check passes)."""
    exe = tmp_path / "myapp.exe"
    exe.write_text("dummy")
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = host_app._resolve_exe("myapp.exe")
        assert result  # non-empty, file was found
        assert os.path.exists(result)
    finally:
        os.chdir(cwd)


def test_resolve_exe_path_name():
    """A bare program name is resolved through PATH (shutil.which)."""
    # python3 / python must exist on PATH in CI.
    result = host_app._resolve_exe("python3")
    assert result, "python3 should be resolvable via PATH"
    assert os.path.exists(result)


def test_resolve_exe_not_found():
    """A non-existent path/name returns empty string."""
    assert host_app._resolve_exe("nonexistent_xyz_12345.exe") == ""


def test_resolve_exe_empty():
    """Empty input returns empty string."""
    assert host_app._resolve_exe("") == ""


def test_resolve_exe_env_var(tmp_path):
    """%VAR% / $VAR style environment variables are expanded."""
    exe = tmp_path / "myapp.exe"
    exe.write_text("dummy")
    with mock.patch.dict(os.environ, {"TEST_APP_EXE": str(exe)}):
        if sys.platform.startswith("win"):
            result = host_app._resolve_exe("%TEST_APP_EXE%")
        else:
            result = host_app._resolve_exe("$TEST_APP_EXE")
        assert result == str(exe)


def test_resolve_exe_expanduser(tmp_path):
    """~ is expanded to the user home directory."""
    home = os.path.expanduser("~")
    # We can't guarantee a file at ~/nonexistent, but expanduser should at
    # least not crash and should expand the tilde.
    result = host_app._resolve_exe("~/nonexistent_xyz_12345.exe")
    assert result == ""  # file does not exist, so ""


# ---------------------------------------------------------------------------
# app: command-line parsing (shlex split + quote strip)
# ---------------------------------------------------------------------------

def _parse_app_command(value):
    """Mirror the parsing done in run_action for the app: step.

    Returns (exe, args) after shlex.split(posix=False) + quote stripping,
    exactly as ``_open_app`` and the pre-check in ``run_action`` do it.
    """
    if not value:
        return "", []
    parts = shlex.split(value, posix=False)
    parts = [p.strip('"') for p in parts]
    return parts[0], parts[1:]


def test_parse_app_simple():
    exe, args = _parse_app_command("C:\\path\\app.exe")
    assert exe == "C:\\path\\app.exe"
    assert args == []


def test_parse_app_with_args():
    exe, args = _parse_app_command("C:\\path\\app.exe --flag --port 8080")
    assert exe == "C:\\path\\app.exe"
    assert args == ["--flag", "--port", "8080"]


def test_parse_app_quoted_path_with_spaces():
    exe, args = _parse_app_command('"C:\\Program Files\\App\\app.exe" --new')
    assert exe == "C:\\Program Files\\App\\app.exe"
    assert args == ["--new"]


def test_parse_app_quoted_arg_with_spaces():
    exe, args = _parse_app_command('myapp.exe "hello world"')
    assert exe == "myapp.exe"
    assert args == ["hello world"]


def test_parse_app_path_name():
    exe, args = _parse_app_command("notepad")
    assert exe == "notepad"
    assert args == []


def test_parse_app_empty():
    exe, args = _parse_app_command("")
    assert exe == ""
    assert args == []


# ---------------------------------------------------------------------------
# _open_app command construction (mocked subprocess)
# ---------------------------------------------------------------------------

def test_open_app_windows():
    """On Windows the exe + args are passed directly to Popen."""
    with mock.patch.object(host_app.sys, "platform", "win32"):
        with mock.patch.object(host_app.subprocess, "Popen") as popen:
            host_app._open_app("C:\\app.exe --flag")
            popen.assert_called_once_with(["C:\\app.exe", "--flag"])


def test_open_app_macos_uses_open_args():
    """On macOS ``open --args`` is used so ``-``-prefixed args are not swallowed."""
    with mock.patch.object(host_app.sys, "platform", "darwin"):
        with mock.patch.object(host_app.subprocess, "Popen") as popen:
            host_app._open_app("/Applications/App.app --flag")
            popen.assert_called_once_with(
                ["open", "--args", "/Applications/App.app", "--flag"])


def test_open_app_linux():
    """On Linux the exe + args are passed directly to Popen."""
    with mock.patch.object(host_app.sys, "platform", "linux"):
        with mock.patch.object(host_app.subprocess, "Popen") as popen:
            host_app._open_app("/usr/bin/app --flag")
            popen.assert_called_once_with(["/usr/bin/app", "--flag"])


def test_open_app_empty():
    """Empty input is a no-op."""
    with mock.patch.object(host_app.subprocess, "Popen") as popen:
        host_app._open_app("")
        popen.assert_not_called()


# ---------------------------------------------------------------------------
# _win_foreground_hwnd / _wait_app_foreground (off-Windows no-op)
# ---------------------------------------------------------------------------

def test_wait_app_foreground_off_windows():
    """Off Windows the focus wait is a no-op returning False."""
    with mock.patch.object(host_app.sys, "platform", "linux"):
        assert host_app._wait_app_foreground(123, "myapp") is False


def test_win_foreground_hwnd_off_windows():
    """Off Windows the foreground hwnd is always 0."""
    with mock.patch.object(host_app.sys, "platform", "linux"):
        assert host_app._win_foreground_hwnd() == 0