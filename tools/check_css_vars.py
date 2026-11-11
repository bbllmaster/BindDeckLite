"""Static guard: every ``var(--x)`` in the host stylesheet must be defined.

Why this file exists
--------------------
WS-578: the update dialog had no left/right padding and zero gap between its
two blocks. The cause was not a layout bug but a typo-shaped bug: the rules used
``var(--sp-5)`` while ``:root`` only went 1/2/3/4/6/8. An undefined custom
property with no fallback invalidates the **whole declaration**, so
``padding: 16px 0 24px`` silently became "no padding" and nothing in the CSS
looked wrong on review.

The stylesheet is the production artifact here, so the guard parses the real
``host/static/style.css`` instead of a copy. ``var(--x, fallback)`` is allowed
by CSS and therefore ignored; only bare references count as errors.

Usage::

    python tools/check_css_vars.py [--css PATH]

Exit code 0 = clean, 1 = undefined references found.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_CSS = Path(__file__).resolve().parent.parent / "host" / "static" / "style.css"

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
# ``--name:`` at a declaration position. The lookbehind keeps ``foo--bar:`` and
# the ``--`` inside values (e.g. ``1px--2px``) out of the result.
_DEFINITION = re.compile(r"(?<![\w-])(--[A-Za-z0-9_-]+)\s*:")
# ``var(--name)`` vs ``var(--name, 12px)``: the optional comma group is the
# fallback, which is legal CSS and must not be reported.
_REFERENCE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(,?)")


def _strip_comments(css: str) -> str:
    """Blank out comments, keeping newlines so line numbers stay accurate."""

    def _blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _COMMENT.sub(_blank, css)


def defined_vars(css: str) -> set[str]:
    """Return every custom property this stylesheet declares."""

    return {m.group(1) for m in _DEFINITION.finditer(_strip_comments(css))}


def undefined_refs(css: str) -> list[tuple[int, str]]:
    """Return ``(line, name)`` for each ``var(--x)`` without a definition.

    A reference with a fallback (``var(--x, ...)``) is skipped: CSS resolves it
    even when ``--x`` is absent, so it cannot blank out the declaration.
    """

    text = _strip_comments(css)
    defined = defined_vars(text)
    problems: list[tuple[int, str]] = []
    for match in _REFERENCE.finditer(text):
        name, has_fallback = match.group(1), match.group(2) == ","
        if has_fallback or name in defined:
            continue
        problems.append((text.count("\n", 0, match.start()) + 1, name))
    return problems


def check_file(path: Path) -> list[tuple[int, str]]:
    return undefined_refs(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--css", type=Path, default=DEFAULT_CSS, help="stylesheet to check")
    args = parser.parse_args(argv)

    problems = check_file(args.css)
    if not problems:
        print(f"OK: no undefined CSS variables in {args.css}")
        return 0

    print(f"FAIL: {len(problems)} undefined CSS variable reference(s) in {args.css}")
    for line, name in problems:
        print(f"  {args.css}:{line}: var({name}) is not defined (add it to :root or give a fallback)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
