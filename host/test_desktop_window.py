"""Unit tests for the desktop window geometry memory (WS-691).

Everything here drives the **production** helpers from ``desktop.py`` —
``normalize_window_geometry``, ``looks_maximized``, ``window_geometry`` and the
real ``make_on_closing`` handler.  Nothing is re-implemented, so a change in
``desktop.py`` really fails these tests::

    cd host && python -m pytest test_desktop_window.py -q
"""

import unittest.mock as mock

import desktop
from desktop import (
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    looks_maximized,
    make_on_closing,
    normalize_window_geometry,
    window_geometry,
)

PRIMARY_1080 = {"x": 0, "y": 0, "w": 1920, "h": 1040}
LEFT_1080 = {"x": -1920, "y": 0, "w": 1920, "h": 1040}
SECOND_1080 = {"x": 1920, "y": 0, "w": 1920, "h": 1040}
LAPTOP_768 = {"x": 0, "y": 0, "w": 1366, "h": 728}


def test_missing_geometry_falls_back_to_default():
    """First launch: no settings.window at all → 1280x800, reason missing."""
    geo = normalize_window_geometry(None, [])
    assert geo["reason"] == "missing"
    assert (geo["w"], geo["h"]) == (DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
    # No screens probed → let the OS place it, never guess a position.
    assert geo["x"] is None and geo["y"] is None


def test_empty_dict_is_missing():
    assert normalize_window_geometry({}, [])["reason"] == "missing"


def test_missing_one_field_is_missing():
    geo = normalize_window_geometry({"w": 1100, "h": 700, "x": 10}, [])
    assert geo["reason"] == "missing"
    assert (geo["w"], geo["h"]) == (DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)


def test_non_numeric_size_is_invalid():
    """"1100" is not a number here: a corrupted config must not be coerced."""
    geo = normalize_window_geometry({"w": "1100", "h": 700, "x": 0, "y": 0}, [])
    assert geo["reason"] == "invalid"
    assert geo["w"] == DEFAULT_WINDOW_WIDTH


def test_negative_size_is_invalid():
    geo = normalize_window_geometry({"w": -1100, "h": 700, "x": 0, "y": 0}, [])
    assert geo["reason"] == "invalid"
    assert (geo["w"], geo["h"]) == (DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)


def test_zero_size_is_invalid():
    geo = normalize_window_geometry({"w": 1100, "h": 0, "x": 0, "y": 0}, [])
    assert geo["reason"] == "invalid"
    assert geo["h"] == DEFAULT_WINDOW_HEIGHT


def test_nan_size_is_invalid():
    geo = normalize_window_geometry({"w": float("nan"), "h": 700, "x": 0, "y": 0}, [])
    assert geo["reason"] == "invalid"


def test_valid_geometry_is_used_as_stored():
    saved = {"w": 1100, "h": 700, "x": 1920 + 40, "y": 60}
    geo = normalize_window_geometry(saved, [PRIMARY_1080, SECOND_1080])
    assert geo == {"w": 1100, "h": 700, "x": 1960, "y": 60, "reason": ""}


def test_negative_position_on_left_screen_is_valid():
    """A screen left of the primary one gives negative x — that is legal."""
    saved = {"w": 1000, "h": 600, "x": -1500, "y": 100}
    geo = normalize_window_geometry(saved, [PRIMARY_1080, LEFT_1080])
    assert geo["reason"] == ""
    assert (geo["x"], geo["y"]) == (-1500, 100)


def test_intersecting_any_single_screen_is_enough():
    """Two screens, the saved rectangle only touches the second one."""
    saved = {"w": 800, "h": 600, "x": 2000, "y": 200}
    geo = normalize_window_geometry(saved, [PRIMARY_1080, SECOND_1080])
    assert geo["reason"] == ""
    assert (geo["w"], geo["h"], geo["x"], geo["y"]) == (800, 600, 2000, 200)


def test_partial_overlap_is_valid():
    """Half off the right edge still intersects the primary screen."""
    saved = {"w": 1000, "h": 600, "x": 1500, "y": 100}
    geo = normalize_window_geometry(saved, [PRIMARY_1080])
    assert geo["reason"] == ""
    assert geo["x"] == 1500


def test_offscreen_geometry_converges_to_primary():
    """Second screen unplugged: the window must land inside the primary one."""
    saved = {"w": 1200, "h": 800, "x": 3000, "y": 300}
    geo = normalize_window_geometry(saved, [PRIMARY_1080])
    assert geo["reason"] == "offscreen"
    assert geo["x"] >= 0 and geo["y"] >= 0
    assert geo["x"] + geo["w"] <= PRIMARY_1080["w"]
    assert geo["y"] + geo["h"] <= PRIMARY_1080["h"]
    # Converged size is capped at 90% of the work area.
    assert geo["w"] <= int(PRIMARY_1080["w"] * 0.9)
    assert geo["h"] <= int(PRIMARY_1080["h"] * 0.9)


def test_offscreen_large_window_is_capped_at_90_percent():
    saved = {"w": 5000, "h": 4000, "x": 9000, "y": 9000}
    geo = normalize_window_geometry(saved, [PRIMARY_1080])
    assert geo["reason"] == "offscreen"
    assert geo["w"] <= int(PRIMARY_1080["w"] * 0.9)
    assert geo["h"] <= int(PRIMARY_1080["h"] * 0.9)


def test_first_launch_on_1366x768_fits_the_work_area():
    """Acceptance: 800px default height would hide the log page's last line."""
    geo = normalize_window_geometry(None, [LAPTOP_768])
    assert geo["reason"] == "missing"
    assert geo["y"] + geo["h"] <= LAPTOP_768["h"]
    assert geo["h"] <= int(LAPTOP_768["h"] * 0.9)
    assert geo["x"] + geo["w"] <= LAPTOP_768["w"]


def test_looks_maximized_detects_a_full_work_area():
    assert looks_maximized({"w": 1920, "h": 1040, "x": 0, "y": 0},
                           [PRIMARY_1080]) is True
    assert looks_maximized({"w": 1915, "h": 1035, "x": 3, "y": 2},
                           [PRIMARY_1080]) is True


def test_looks_maximized_ignores_normal_windows():
    assert looks_maximized({"w": 1100, "h": 700, "x": 40, "y": 60},
                           [PRIMARY_1080]) is False


class _FakeWindow:
    def __init__(self, w, h, x, y):
        self.width, self.height, self.x, self.y = w, h, x, y


class _FakeRect:
    """WinForms ``Rectangle``: upper-case attributes, work area not bounds."""

    def __init__(self, x, y, w, h):
        self.X, self.Y, self.Width, self.Height = x, y, w, h


class _FakeScreen:
    def __init__(self, x, y, w, h, frame=None):
        self.x, self.y, self.width, self.height = x, y, w, h
        self.frame = frame


def test_screen_workareas_prefers_the_work_area_frame(monkeypatch):
    """The Windows frame is the work area (screen minus taskbar)."""
    import sys
    import types

    fake = types.SimpleNamespace(screens=[
        _FakeScreen(0, 0, 1920, 1080, _FakeRect(0, 0, 1920, 1040)),
    ])
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert desktop.screen_workareas() == [{"x": 0, "y": 0, "w": 1920, "h": 1040}]


def test_screen_workareas_puts_the_primary_screen_first(monkeypatch):
    """Enumeration order is not guaranteed; convergence targets the origin."""
    import sys
    import types

    fake = types.SimpleNamespace(screens=[
        _FakeScreen(-1920, 0, 1920, 1080),
        _FakeScreen(0, 0, 1920, 1080),
    ])
    monkeypatch.setitem(sys.modules, "webview", fake)
    areas = desktop.screen_workareas()
    assert areas[0]["x"] == 0, areas
    assert len(areas) == 2


def test_screen_workareas_is_empty_when_headless(monkeypatch):
    """No pywebview → no screen knowledge, placement is left to the OS."""
    import sys

    monkeypatch.setitem(sys.modules, "webview", None)
    assert desktop.screen_workareas() == []


def test_window_geometry_reads_the_live_values():
    assert window_geometry(_FakeWindow(1100, 700, 40, 60)) == {
        "w": 1100, "h": 700, "x": 40, "y": 60}


def test_window_geometry_rejects_minimized_sentinel():
    """Windows parks a minimized window at -32000; never persist that."""
    assert window_geometry(_FakeWindow(1100, 700, -32000, -32000)) is None


def test_window_geometry_rejects_zero_size():
    assert window_geometry(_FakeWindow(0, 0, 0, 0)) is None


def _close_window(window, geometry_save, hidden=False, force_quit=False,
                  workareas=(PRIMARY_1080,)):
    """Run the real ``_on_closing`` handler and return what it did.

    ``STATE`` is patched for the whole call (the handler reads
    ``window_hidden`` / ``tray_active`` at close time), and the screen probe is
    stubbed so no GUI backend is needed.
    """
    state = {"force_quit": force_quit}
    handler = make_on_closing(state, {"behavior": "exit"}, False,
                              "http://127.0.0.1:8788", [window],
                              geometry_save=geometry_save)
    with mock.patch.dict(desktop.STATE,
                         {"tray_active": False, "window_hidden": hidden}):
        with mock.patch.object(desktop, "screen_workareas",
                               return_value=list(workareas)):
            return handler()


def test_visible_close_saves_geometry():
    """Closing a visible window writes the four values through the callback."""
    saved = []
    assert _close_window(_FakeWindow(1100, 700, 40, 60), saved.append) is True
    assert saved == [{"w": 1100, "h": 700, "x": 40, "y": 60}]


def test_tray_hidden_close_does_not_save():
    """Hidden in the tray: the stored geometry must stay the last visible one."""
    saved = []
    assert _close_window(_FakeWindow(1100, 700, 40, 60), saved.append,
                         hidden=True) is True
    assert saved == []


def test_maximized_close_does_not_save():
    saved = []
    assert _close_window(_FakeWindow(1920, 1040, 0, 0), saved.append,
                         force_quit=True) is True
    assert saved == []


def test_failing_save_does_not_block_the_close():
    """A read-only config must not trap the user in the window."""
    def _boom(geo):
        raise OSError("disk full")

    assert _close_window(_FakeWindow(1100, 700, 40, 60), _boom) is True
