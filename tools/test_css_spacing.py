"""WS-581 regression: spacing must come from the ``--sp-*`` scale, not bare px.

The test imports the production guard (``tools/check_css_spacing.py``) and runs
it against the real ``host/static/style.css`` — no parsing is copied here, so a
break in either the guard or the stylesheet fails this file.

Before the fix the real stylesheet held ~128 bare spacing values (6px × 31,
10px × 26, 8px × 15, 4px × 12, 2px × 10 …) next to 132 tokenised ones. The last
assertion pins the remaining budget: only 1px hairlines may stay.

Run::

    python -m pytest tools/test_css_spacing.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_css_spacing

STYLE_CSS = ROOT / "host" / "static" / "style.css"


def test_stylesheet_has_no_bare_spacing_px():
    offenders = check_css_spacing.check_file(STYLE_CSS)
    assert offenders == [], "bare spacing px: " + ", ".join(
        f"style.css:{line} {prop}: {value}" for line, prop, value, _tok in offenders
    )


def test_total_bare_spacing_px_stays_within_budget():
    """WS-581 acceptance: ≤ 10 bare values, all of them 1px hairlines."""

    total = check_css_spacing.bare_spacing_px(STYLE_CSS.read_text(encoding="utf-8"))
    assert len(total) <= 10, "too many bare spacing values: " + ", ".join(
        f"{line}:{prop}={token}" for line, prop, _val, token in total
    )


def test_guard_flags_the_ws581_shape():
    """The exact pre-fix values (6px / 10px / 2px) must be reported."""

    css = ".a { padding: 6px 12px; }\n.b { gap: 10px; }\n.c { margin-top: 2px; }"
    found = [(line, prop, token) for line, prop, _val, token in check_css_spacing.check_file_str(css)]
    assert found == [
        (1, "padding", "6px"),
        (1, "padding", "12px"),   # even on-scale pixels must be spelled as a token
        (2, "gap", "10px"),
        (3, "margin-top", "2px"),
    ]


def test_guard_accepts_tokens_zero_calc_and_hairlines():
    css = (
        ".a { padding: 0 var(--sp-2); }\n"
        ".b { gap: calc(100% - 28px); }\n"
        ".c { margin-bottom: -1px; }\n"
        ".d { inset: 0; }\n"
        ".e { top: 50%; }\n"
        ".f { padding-inline: var(--sp-4) var(--sp-6); }"
    )
    assert check_css_spacing.check_file_str(css) == []


def test_guard_ignores_non_spacing_properties():
    """``width`` / ``min-height`` are sizes, not spacing steps (WS-581 scope)."""

    css = ".a { width: 88px; min-height: 34px; font-size: 11px; }"
    assert check_css_spacing.check_file_str(css) == []


def test_guard_ignores_commented_out_pixels():
    css = "/* .a { gap: 6px; } */\n.b { gap: var(--sp-2); }"
    assert check_css_spacing.check_file_str(css) == []
