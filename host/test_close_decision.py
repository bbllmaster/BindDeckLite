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