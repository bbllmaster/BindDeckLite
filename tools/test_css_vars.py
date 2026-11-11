"""WS-578 regression: the host stylesheet must not reference undefined variables.

The test imports the production guard (``tools/check_css_vars.py``) and runs it
against the real ``host/static/style.css``. Nothing about the parsing is copied
here, so breaking the guard or the stylesheet fails this file.

Before the fix this file failed with exactly two hits — ``var(--sp-5)`` used by
``.update-body`` (padding) and ``.update-block`` (margin) while ``:root`` only
declared 1/2/3/4/6/8 — and the update dialog rendered flush against its borders.

Run::

    python -m pytest tools/test_css_vars.py -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_css_vars

STYLE_CSS = ROOT / "host" / "static" / "style.css"


def test_stylesheet_has_no_undefined_vars():
    problems = check_css_vars.check_file(STYLE_CSS)
    assert problems == [], "undefined CSS variables: " + ", ".join(
        f"{STYLE_CSS.name}:{line} var({name})" for line, name in problems
    )


def test_spacing_tokens_match_their_names():
    """``--sp-N`` must be N * 4px: the name is what call sites reason about."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    tokens = {int(n): int(v) for n, v in re.findall(r"--sp-(\d+)\s*:\s*(\d+)px", css)}
    assert tokens, "no --sp-N tokens found"
    for step, value in tokens.items():
        assert value == step * 4, f"--sp-{step} should be {step * 4}px, got {value}px"


def test_checker_flags_a_bare_undefined_reference():
    """The guard itself must catch the exact WS-578 shape, not just be silent."""
    css = ":root { --sp-1: 4px; }\n.a { padding: 0 var(--sp-5); }"
    assert check_css_vars.undefined_refs(css) == [(2, "--sp-5")]


def test_checker_allows_fallback_reference():
    css = ":root { --sp-1: 4px; }\n.a { padding: 0 var(--sp-5, 20px); }"
    assert check_css_vars.undefined_refs(css) == []


def test_checker_ignores_commented_out_references():
    css = ":root { --sp-1: 4px; }\n/* .a { gap: var(--sp-5); } */\n.b { gap: var(--sp-1); }"
    assert check_css_vars.undefined_refs(css) == []
