"""Unit tests for ``desktop.close_decision`` — the pure decision helper behind
``_on_closing``.

These cover the four branches that matter for the close-button behavior and
the force-quit deadlock regression (WS-374 review #1, #6).  No pywebview, no
GUI, no tray — the function is pure, so it can run anywhere::

    pytest host/test_close_decision.py
"""

from desktop import close_decision


def test_exit_behavior_allows_close():
    """User unchecked "minimize to tray" → close proceeds."""
    allow, hide = close_decision(False, "exit", use_tray=True)
    assert allow is True
    assert hide is False


def test_exit_behavior_allows_close_without_tray():
    allow, hide = close_decision(False, "exit", use_tray=False)
    assert allow is True
    assert hide is False


def test_minimize_with_tray_hides():
    """Default path: tray available + minimize → hide, cancel close."""
    allow, hide = close_decision(False, "minimize", use_tray=True)
    assert allow is False
    assert hide is True


def test_minimize_without_tray_hides():
    """The bug fix: tray unavailable + minimize → still hide, not exit."""
    allow, hide = close_decision(False, "minimize", use_tray=False)
    assert allow is False
    assert hide is True


def test_force_quit_allows_close():
    """``stop_all()`` set force_quit → close must proceed so ``webview.start()``
    returns and the process can exit (WS-374 review #1)."""
    allow, hide = close_decision(True, "minimize", use_tray=True)
    assert allow is True
    assert hide is False


def test_force_quit_allows_close_without_tray():
    allow, hide = close_decision(True, "minimize", use_tray=False)
    assert allow is True
    assert hide is False


def test_force_quit_overrides_exit():
    """force_quit must win even when behavior is "exit" — stop_all() always
    sets force_quit=True regardless of the user's close-button setting."""
    allow, hide = close_decision(True, "exit", use_tray=False)
    assert allow is True
    assert hide is False


def test_unknown_behavior_with_tray_still_hides():
    """When a tray is available, any non-exit behavior minimises to tray
    (matches the original ``use_tray`` branch which did not inspect behavior)."""
    allow, hide = close_decision(False, "bogus", use_tray=True)
    assert allow is False
    assert hide is True


def test_unknown_behavior_without_tray_falls_through_to_exit():
    """Defensive: without a tray and an unrecognized behavior, exit instead of
    hiding forever."""
    allow, hide = close_decision(False, "bogus", use_tray=False)
    assert allow is True
    assert hide is False


# --- WS-382: pywebview Event.set() must actually cancel the close ---------
# The bug: _on_closing took a ``win`` parameter, but pywebview calls
# ``Event.set()`` with no arguments.  The handler raised TypeError inside
# ``set()``; the exception was caught, the return value discarded, and
# ``set()`` reported "no cancel" — the window closed and the app exited
# despite "minimize to tray" being checked.
#
# The fix: the handler takes no parameters and reads the window from
# ``window_ref[0]``.  It is produced by ``desktop.make_on_closing`` so the
# full path can be exercised with a real ``webview.event.Event`` without
# reconstructing the closure by hand (WS-382 review #2, #3).

def test_make_on_closing_handler_is_parameterless():
    """``make_on_closing`` must return a handler that takes no parameters,
    so pywebview's ``Event.set()`` (called with no args) can invoke it
    without a TypeError."""
    import ast
    import inspect
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    import desktop

    src = inspect.getsource(desktop.make_on_closing)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_on_closing":
            args = [a.arg for a in node.args.args]
            assert args == [], (
                f"_on_closing must take no parameters (pywebview calls "
                f"Event.set() with no args); got {args}"
            )
            return
    raise AssertionError("_on_closing not found in make_on_closing")


def test_buggy_handler_with_param_fails_via_real_event():
    """Demonstrate the WS-382 bug: a handler with a positional parameter
    (the PR #25 ``_on_closing(win)`` signature) raises TypeError inside
    ``Event.set()``, which returns False (no cancel) — the window closes.
    This proves the regression test can fail on the buggy version.
    """
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from webview.event import Event

    class FakeWindow:
        def __init__(self):
            self.hidden = False

        def hide(self):
            self.hidden = True

    # PR #25 signature: _on_closing(win)
    def buggy_handler(win):
        win.hide()
        return False

    event = Event(FakeWindow(), True)
    event += buggy_handler

    # pywebview calls set() with NO arguments — buggy_handler(win) raises
    # TypeError, which Event.set() swallows; return_values stays empty,
    # so set() returns False (don't cancel) → window closes.
    result = event.set()
    assert result is False, (
        "A handler with a positional parameter cannot cancel the close via "
        "Event.set() — this is the WS-382 bug"
    )


def test_on_closing_cancels_close_via_real_event():
    """End-to-end: register the real ``make_on_closing`` handler with
    pywebview's real ``Event`` and confirm ``set()`` returns True (cancel)
    when minimize is requested.

    This is the exact path that was broken in WS-382: the handler raised
    TypeError, ``set()`` returned False, and the window closed.
    """
    import os
    import sys
    import unittest.mock as mock

    sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
    from webview.event import Event
    import desktop
    from desktop import make_on_closing

    class FakeWindow:
        def __init__(self):
            self.hidden = False

        def hide(self):
            self.hidden = True

    window_ref = [FakeWindow()]
    state = {"force_quit": False}
    close_ref = {"behavior": "minimize"}
    use_tray = False
    url = "http://127.0.0.1:8788"

    # Reset STATE so the test is independent of any previous test's side
    # effects (the handler reads STATE["tray_active"] and
    # STATE["window_hidden"]).
    with mock.patch.dict(
        desktop.STATE, {"tray_active": False, "window_hidden": False}
    ):
        with mock.patch.object(desktop, "open_url") as mock_open:
            handler = make_on_closing(
                state, close_ref, use_tray, url, window_ref
            )
            # should_lock=True makes Event.set() run the handler
            # synchronously, so the test is deterministic (no thread race
            # on return_values).
            event = Event(FakeWindow(), True)
            event += handler

            # pywebview calls set() with NO arguments — this is what was
            # failing in the buggy version.
            result = event.set()

    assert result is True, (
        "Event.set() must return True (cancel close) when minimize is "
        "requested; got False — the handler raised TypeError and the close "
        "proceeded (WS-382)"
    )
    assert window_ref[0].hidden is True, "window.hide() must have been called"
    # The minimize-without-tray path really does open the web UI in a
    # browser; stubbing it proves the path was exercised without launching
    # a real browser on CI (WS-382 review #2).
    mock_open.assert_called_once_with(url)