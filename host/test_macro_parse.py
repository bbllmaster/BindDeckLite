"""Unit tests for the ``app:`` macro step helpers — pure-function coverage.

These test the command-line parsing, exe resolution, and ``run_action``
``app`` branch behind the new ``app:<exe> [args]`` macro step.  No GUI, no
keyboard library, no Win32 — the helpers are pure Python and run anywhere::

    pytest host/test_macro_parse.py -v
"""

import os
import sys
from unittest import mock

import pytest

# host/ is not a package on its own; import the module by path.
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import app as host_app


# ---------------------------------------------------------------------------
# _parse_app_cmd — the single parsing entry point
# ---------------------------------------------------------------------------

def test_parse_app_cmd_simple():
    exe, args = host_app._parse_app_cmd("C:\\path\\app.exe")
    assert exe == "C:\\path\\app.exe"
    assert args == ""


def test_parse_app_cmd_with_args():
    exe, args = host_app._parse_app_cmd("C:\\path\\app.exe --flag --port 8080")
    assert exe == "C:\\path\\app.exe"
    assert args == "--flag --port 8080"


def test_parse_app_cmd_quoted_path_with_spaces():
    exe, args = host_app._parse_app_cmd(
        '"C:\\Program Files\\App\\app.exe" --new')
    assert exe == "C:\\Program Files\\App\\app.exe"
    assert args == "--new"


def test_parse_app_cmd_quoted_arg_with_spaces():
    exe, args = host_app._parse_app_cmd('myapp.exe "hello world"')
    assert exe == "myapp.exe"
    assert args == "hello world"


def test_parse_app_cmd_path_name():
    exe, args = host_app._parse_app_cmd("notepad")
    assert exe == "notepad"
    assert args == ""


def test_parse_app_cmd_empty():
    exe, args = host_app._parse_app_cmd("")
    assert exe == ""
    assert args == ""


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


def test_resolve_exe_expanduser():
    """~ is expanded to the user home directory."""
    result = host_app._resolve_exe("~/nonexistent_xyz_12345.exe")
    assert result == ""  # file does not exist, so ""


# ---------------------------------------------------------------------------
# _open_app command construction (mocked subprocess)
# ---------------------------------------------------------------------------

def test_open_app_windows():
    """On Windows the resolved exe + args are passed directly to Popen."""
    with mock.patch.object(host_app.sys, "platform", "win32"):
        with mock.patch.object(host_app, "_resolve_exe", return_value="C:\\app.exe"):
            with mock.patch.object(host_app.subprocess, "Popen") as popen:
                host_app._open_app("C:\\app.exe --flag")
                popen.assert_called_once_with(["C:\\app.exe", "--flag"])


def test_open_app_macos_uses_open_args():
    """On macOS ``open <exe> --args <args>`` — --args after the target."""
    with mock.patch.object(host_app.sys, "platform", "darwin"):
        with mock.patch.object(host_app, "_resolve_exe",
                               return_value="/Applications/App.app"):
            with mock.patch.object(host_app.subprocess, "Popen") as popen:
                host_app._open_app("/Applications/App.app --flag")
                popen.assert_called_once_with(
                    ["open", "/Applications/App.app", "--args", "--flag"])


def test_open_app_linux():
    """On Linux the resolved exe + args are passed directly to Popen."""
    with mock.patch.object(host_app.sys, "platform", "linux"):
        with mock.patch.object(host_app, "_resolve_exe",
                               return_value="/usr/bin/app"):
            with mock.patch.object(host_app.subprocess, "Popen") as popen:
                host_app._open_app("/usr/bin/app --flag")
                popen.assert_called_once_with(["/usr/bin/app", "--flag"])


def test_open_app_empty():
    """Empty input returns None (no Popen call)."""
    with mock.patch.object(host_app.subprocess, "Popen") as popen:
        assert host_app._open_app("") is None
        popen.assert_not_called()


def test_open_app_missing_exe():
    """A non-existent exe returns None (pre-check fails, no Popen call)."""
    with mock.patch.object(host_app.sys, "platform", "win32"):
        with mock.patch.object(host_app, "_resolve_exe", return_value=""):
            with mock.patch.object(host_app.subprocess, "Popen") as popen:
                assert host_app._open_app("nonexistent_xyz.exe --flag") is None
                popen.assert_not_called()


def test_open_app_env_var_expansion(tmp_path):
    """Environment variables in the exe path are expanded before Popen."""
    exe = tmp_path / "myapp.exe"
    exe.write_text("dummy")
    with mock.patch.dict(os.environ, {"TEST_APP_EXE": str(exe)}):
        with mock.patch.object(host_app.sys, "platform", "win32"):
            with mock.patch.object(host_app, "_resolve_exe",
                                   return_value=str(exe)):
                with mock.patch.object(host_app.subprocess, "Popen") as popen:
                    host_app._open_app("%TEST_APP_EXE% --flag")
                    popen.assert_called_once()


# ---------------------------------------------------------------------------
# _wait_app_foreground (off-Windows no-op)
# ---------------------------------------------------------------------------

def test_wait_app_foreground_off_windows():
    """Off Windows the focus wait returns None (not a failure)."""
    with mock.patch.object(host_app.sys, "platform", "linux"):
        assert host_app._wait_app_foreground(123, pid=456) is None


def test_win_foreground_hwnd_off_windows():
    """Off Windows the foreground hwnd is always 0."""
    with mock.patch.object(host_app.sys, "platform", "linux"):
        assert host_app._win_foreground_hwnd() == 0


def test_win_find_app_window_off_windows():
    """Off Windows PID-based window search returns 0."""
    with mock.patch.object(host_app.sys, "platform", "linux"):
        assert host_app._win_find_app_window(12345) == 0


# ---------------------------------------------------------------------------
# run_action app: branch — end-to-end with mocked Popen / keyboard
# ---------------------------------------------------------------------------

def _make_macro_action(steps):
    """Build a macro action dict for run_action."""
    return {"type": "macro", "value": "test", "macro": steps}


def test_run_action_app_launches_and_continues():
    """``app:<exe>`` followed by ``text:`` — app launches, macro continues."""
    action = _make_macro_action([
        {"type": "app", "value": "/bin/echo hello"},
        {"type": "text", "value": "world"},
    ])
    with mock.patch.object(host_app, "_load_keyboard") as kb_mock:
        kb = mock.MagicMock()
        kb_mock.return_value = kb
        with mock.patch.object(host_app, "_open_app") as app_mock:
            proc = mock.MagicMock()
            proc.pid = 99999
            app_mock.return_value = proc
            with mock.patch.object(host_app, "_wait_app_foreground",
                                   return_value=True):
                result = host_app.run_action(action, dev_id="test-dev")
    assert result["ok"] is True
    app_mock.assert_called_once_with("/bin/echo hello")
    kb.write.assert_called_once_with("world",
                                     delay=host_app.MACRO_TEXT_DEFAULT_DELAY)


def test_run_action_app_missing_exe_skips_step():
    """``app:<nonexistent>`` — error logged, macro continues to next step."""
    action = _make_macro_action([
        {"type": "app", "value": "nonexistent_xyz_12345.exe --flag"},
        {"type": "text", "value": "hello"},
    ])
    with mock.patch.object(host_app, "_load_keyboard") as kb_mock:
        kb = mock.MagicMock()
        kb_mock.return_value = kb
        with mock.patch.object(host_app, "_resolve_exe", return_value=""):
            with mock.patch.object(host_app, "_open_app") as app_mock:
                result = host_app.run_action(action, dev_id="test-dev")
    assert result["ok"] is True  # macro continues despite the failed step
    app_mock.assert_not_called()  # _open_app not reached when exe not found
    kb.write.assert_called_once_with("hello",
                                     delay=host_app.MACRO_TEXT_DEFAULT_DELAY)


def test_run_action_app_with_delay_step():
    """``app:`` + ``delay:`` + ``text:`` — delay between launch and type."""
    action = _make_macro_action([
        {"type": "app", "value": "/bin/echo test"},
        {"type": "delay", "value": 500},
        {"type": "text", "value": "after-delay"},
    ])
    with mock.patch.object(host_app, "_load_keyboard") as kb_mock:
        kb = mock.MagicMock()
        kb_mock.return_value = kb
        with mock.patch.object(host_app, "_open_app") as app_mock:
            proc = mock.MagicMock()
            proc.pid = 88888
            app_mock.return_value = proc
            with mock.patch.object(host_app, "_wait_app_foreground",
                                   return_value=True):
                with mock.patch.object(host_app.time, "sleep") as sleep_mock:
                    result = host_app.run_action(action, dev_id="test-dev")
    assert result["ok"] is True
    # delay step should have called time.sleep with 0.5 seconds
    sleep_mock.assert_called_once_with(0.5)
    kb.write.assert_called_once_with("after-delay",
                                     delay=host_app.MACRO_TEXT_DEFAULT_DELAY)


def test_run_action_app_off_windows_no_false_warning():
    """Off Windows the focus wait returns None — no spurious warning."""
    action = _make_macro_action([
        {"type": "app", "value": "/bin/echo test"},
        {"type": "text", "value": "hello"},
    ])
    with mock.patch.object(host_app, "_load_keyboard") as kb_mock:
        kb = mock.MagicMock()
        kb_mock.return_value = kb
        with mock.patch.object(host_app, "_open_app") as app_mock:
            proc = mock.MagicMock()
            proc.pid = 77777
            app_mock.return_value = proc
            with mock.patch.object(host_app, "_wait_app_foreground",
                                   return_value=None):
                with mock.patch.object(host_app, "add_event") as event_mock:
                    result = host_app.run_action(action, dev_id="test-dev")
    assert result["ok"] is True
    # No warning event should be added (None ≠ False)
    event_types = [call.args[0] for call in event_mock.call_args_list]
    assert "warn" not in event_types


def test_run_action_app_windows_timeout_warns():
    """On Windows a real timeout (False) produces a warning event."""
    action = _make_macro_action([
        {"type": "app", "value": "C:\\app.exe --flag"},
        {"type": "text", "value": "hello"},
    ])
    with mock.patch.object(host_app, "_load_keyboard") as kb_mock:
        kb = mock.MagicMock()
        kb_mock.return_value = kb
        with mock.patch.object(host_app, "_resolve_exe",
                               return_value="C:\\app.exe"):
            with mock.patch.object(host_app, "_open_app") as app_mock:
                proc = mock.MagicMock()
                proc.pid = 66666
                app_mock.return_value = proc
                with mock.patch.object(host_app, "_wait_app_foreground",
                                       return_value=False):
                    with mock.patch.object(host_app, "add_event") as event_mock:
                        result = host_app.run_action(action, dev_id="test-dev")
    assert result["ok"] is True
    # A warning event should have been added
    event_types = [call.args[0] for call in event_mock.call_args_list]
    assert "warn" in event_types