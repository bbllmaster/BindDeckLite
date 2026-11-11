"""WS-581 regression: the consistency baseline stays consistent.

These are the invariants P0 introduced. Each one drifted before because nothing
asserted it, so a future edit could quietly bring back a 10px label, a second
button tier, a stray gradient on the primary button or an off-ladder z-index.

The assertions read the production stylesheet directly (no parsed copy of it),
so a change to ``host/static/style.css`` is what makes them fail.

Run::

    python -m pytest tools/test_css_baseline.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLE_CSS = ROOT / "host" / "static" / "style.css"
INDEX_HTML = ROOT / "host" / "templates" / "index.html"

DECLARATION = re.compile(r"(?<![\w-])(?P<prop>[A-Za-z-]+)\s*:\s*(?P<value>[^;{}]+)[;}]")


def _code() -> str:
    """Stylesheet with comments removed — prose about a rule is not a rule."""

    return re.sub(r"/\*.*?\*/", " ", STYLE_CSS.read_text(encoding="utf-8"), flags=re.S)


def _declarations(css: str) -> list[tuple[str, str]]:
    return [(m.group("prop"), m.group("value").strip()) for m in DECLARATION.finditer(css)]


def test_font_sizes_come_from_the_ladder():
    """WS-581: 10px / 22px were deleted; every size is a --fs-* token now."""

    literal = [
        (prop, value)
        for prop, value in _declarations(_code())
        if prop.strip() == "font-size" and not value.startswith("var(--fs-")
    ]
    assert literal == [], f"font-size must use --fs-*: {literal}"


def test_no_unpackaged_inter_family():
    """The old stack claimed "Inter" with no @font-face — a no-op for every user."""

    assert "Inter" not in _code()


def test_button_has_one_compact_tier():
    """30px (.tiny) and 34px (.small-btn) merged into .btn.sm."""

    css = _code()
    assert ".btn.tiny" not in css
    assert ".small-btn" not in css
    assert ".btn.sm" in css
    assert ".tiny" not in INDEX_HTML.read_text(encoding="utf-8")


def test_primary_fill_and_progress_are_solid():
    """WS-581: gradients stay on the logo only — primary/progress use --accent."""

    decls = _declarations(_code())
    filled = {
        prop: value
        for prop, value in decls
        if prop == "background" and ("--accent" in value or "gradient" in value)
    }
    primary = [v for v in filled.values() if "linear-gradient" in v and "--violet" not in v]
    assert primary == [], f"primary/progress must be solid: {primary}"


def test_z_index_uses_the_ladder():
    """Every page-level z-index is a --z-* token; only 1 (local sticky header) may stay."""

    raw = [
        (prop, value)
        for prop, value in _declarations(_code())
        if prop.strip() == "z-index"
        and not value.startswith("var(--z-")
        and value != "1"
    ]
    assert raw == [], f"z-index must use --z-* tokens: {raw}"
