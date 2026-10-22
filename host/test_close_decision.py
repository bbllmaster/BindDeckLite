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


# --- WS-382: pywebview Event.set() parameter-name contract -----------------
# pywebview's ``Event.set()`` (`webview/event.py:40-45`) only injects the native
# window object when the handler's first parameter is literally named
# ``window``.  Any other name falls through to ``func(*args, **kwargs)`` with
# empty args → TypeError inside the swallowed try/except → return value never
# collected → should_cancel=False → close proceeds.  PR #25's ``_on_closing``
# used ``win`` and was silently no-op'ing on every backend.
import inspect


def test_on_closing_handler_uses_window_param():
    """``desktop.run_webview``'s ``_on_closing`` must declare its first
    parameter as ``window`` so pywebview injects the native window."""
    import desktop

    # Source-level check: the handler registered on window.events.closing must
    # accept a ``window`` parameter.  We can't import the closure directly, so
    # scan the source for the registration and the handler signature.
    src = inspect.getsource(desktop.run_webview)
    assert "window.events.closing += _on_closing" in src

    # Find the _on_closing definition and confirm the parameter name.
    import re

    match = re.search(r"def _on_closing\((\w+)\):", src)
    assert match is not None, "_on_closing not found in run_webview"
    assert match.group(1) == "window", (
        f"_on_closing parameter is {match.group(1)!r}, must be 'window' "
        "for pywebview's Event.set() to inject the native window"
    )


def test_event_set_injects_window_for_window_param():
    """Replicate pywebview's Event.set() parameter detection: a handler whose
    first param is ``window`` receives the native window; any other name does
    not.  This is the contract the exe relies on at runtime."""
    received = {}

    def handler_with_window_param(window):
        received["win"] = window
        return False

    def handler_with_win_param(win):
        received["win"] = win
        return False

    native_window = object()

    for handler, expected_ok in [
        (handler_with_window_param, True),
        (handler_with_win_param, False),
    ]:
        received.clear()
        params = inspect.signature(handler).parameters
        value = None
        try:
            if len(params.values()) == 0:
                value = handler()
            elif "window" in params:
                value = handler(native_window)
            else:
                # Same as func(*args, **kwargs) with empty args — pywebview
                # calls the handler with no args here, which raises TypeError
                # for a handler that expects a positional argument.
                value = handler()
        except TypeError:
            # pywebview swallows this in its try/except; the return value is
            # never collected, so should_cancel stays False → close proceeds.
            value = None

        if expected_ok:
            assert received.get("win") is native_window, (
                "handler with 'window' param should receive the native window"
            )
            assert value is False
        else:
            # The handler expects an argument but is called with none — this
            # mirrors the TypeError pywebview swallows, leaving the return
            # value uncollected and should_cancel=False.
            assert "win" not in received
            assert value is None


def test_close_decision_minimize_with_tray_hides_and_returns_false():
    """The end-to-end contract: minimize + tray → _on_closing returns False,
    which Event.set() maps to should_cancel=True, cancelling the close."""
    allow, hide = close_decision(False, "minimize", use_tray=True)
    assert allow is False
    assert hide is True
    # _on_closing returns False when (not allow_close and hide_window)
    assert not allow and hide  # → returns False → should_cancel=True