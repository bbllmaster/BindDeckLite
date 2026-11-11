"""Static guard: spacing declarations must use the ``--sp-*`` scale, not bare px.

Why this file exists
--------------------
WS-581: the stylesheet talked about spacing in two languages at once.  A little
more than half of the 313 spacing values referenced ``var(--sp-N)`` while the
rest were ad-hoc pixels (6px × 31, 10px × 26, 8px × 15, 4px × 12, 2px × 10 …).
The visible result was inconsistent rhythm — the same conceptual gap was 6px in
one card and 8px in the next — and no single place to change it.

The scale is 4px-based: ``--sp-1`` … ``--sp-8``.  Anything else in
``padding / margin / gap / top / right / bottom / left / inset`` should be a
token, so this guard parses the **production** stylesheet and reports every
remaining bare pixel value.

Allowed without a token (and why):

* ``0``                  — no offset at all, not a spacing step.
* ``1px`` / ``-1px``     — hairline separators and 1px overlaps (border-like).
* ``calc(...)``          — arithmetic the scale cannot express (e.g. 100% - 28px).
* ``var(...)``           — the token itself.
* ``auto`` / ``inherit`` / ``unset`` / percentages — not spacing steps.

Usage::

    python tools/check_css_spacing.py [--css PATH]

Exit code 0 = clean, 1 = bare spacing pixels found.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_CSS = Path(__file__).resolve().parent.parent / "host" / "static" / "style.css"

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_DECLARATION = re.compile(r"(?<![\w-])(?P<prop>[A-Za-z-]+)\s*:\s*(?P<value>[^;{}]+)(?P<end>[;{}])")
_PX = re.compile(r"(?<![\w.\-])(?P<sign>-?)(?P<num>\d+(?:\.\d+)?)px\b")

# Every property whose value is a spacing step.  ``padding-*`` / ``margin-*``
# are matched by prefix; the rest are exact names.
_SPACING_EXACT = {"gap", "row-gap", "column-gap", "top", "right", "bottom", "left"}
_SPACING_PREFIX = ("padding", "margin", "inset")

# 1px hairlines are structural (separators / 1px overlaps), not rhythm.
_HAIRLINE = {1.0}


def _strip_comments(css: str) -> str:
    """Blank out comments, keeping newlines so line numbers stay accurate."""

    def _blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _COMMENT.sub(_blank, css)


def _is_spacing_property(prop: str) -> bool:
    name = prop.lower()
    if name.startswith("--"):
        return False          # custom property definition, not a consumer
    if name in _SPACING_EXACT:
        return True
    return any(name == p or name.startswith(p + "-") for p in _SPACING_PREFIX)


def _strip_functions(value: str) -> str:
    """Blank out ``var(...)`` / ``calc(...)`` arguments so their px stay hidden.

    Walks balanced parentheses so nested calls (``calc(var(--sp-2) * 2)``) are
    removed as a unit instead of leaving the inner px behind.
    """

    out = list(value)
    i = 0
    while i < len(out):
        if out[i].isalpha() or out[i] == "-":
            j = i
            while j < len(out) and (out[j].isalnum() or out[j] in "-_"):
                j += 1
            if j < len(out) and out[j] == "(" and "".join(out[i:j]).lower() in ("var", "calc", "min", "max", "clamp"):
                depth = 0
                k = j
                while k < len(out):
                    if out[k] == "(":
                        depth += 1
                    elif out[k] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                for n in range(i, min(k + 1, len(out))):
                    if out[n] != "\n":
                        out[n] = " "
                i = k + 1
                continue
            i = j
            continue
        i += 1
    return "".join(out)


def spacing_values(css: str) -> tuple[list[tuple[int, str, str, str]], list[tuple[int, str, str, str]]]:
    """Split spacing declarations into ``(offenders, hairlines)``.

    Each item is ``(line, property, value, token)``.  ``offenders`` are bare px
    values that must be replaced by a ``--sp-*`` token; ``hairlines`` are the
    whitelisted ``1px`` / ``-1px`` occurrences (kept only for reporting).
    """

    text = _strip_comments(css)
    offenders: list[tuple[int, str, str, str]] = []
    hairlines: list[tuple[int, str, str, str]] = []
    for match in _DECLARATION.finditer(text):
        prop = match.group("prop")
        if not _is_spacing_property(prop):
            continue
        value = match.group("value")
        if "!important" in value:
            value = value.replace("!important", "")
        line = text.count("\n", 0, match.start("value")) + 1
        bare = _strip_functions(value)
        for px in _PX.finditer(bare):
            token = f"{px.group('sign')}{px.group('num')}px"
            item = (line, prop, value.strip(), token)
            if float(px.group("num")) in _HAIRLINE:
                hairlines.append(item)
            else:
                offenders.append(item)
    return offenders, hairlines


def bare_spacing_px(css: str) -> list[tuple[int, str, str, str]]:
    """Every bare px in a spacing declaration, whitelisted hairlines included."""

    offenders, hairlines = spacing_values(css)
    return sorted(offenders + hairlines)


def check_file_str(css: str) -> list[tuple[int, str, str, str]]:
    """Bare spacing px values in an in-memory stylesheet (used by the tests)."""

    return spacing_values(css)[0]


def check_file(path: Path) -> list[tuple[int, str, str, str]]:
    return check_file_str(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--css", type=Path, default=DEFAULT_CSS, help="stylesheet to check")
    parser.add_argument("--max-bare", type=int, default=10,
                        help="maximum bare spacing px (all whitelisted) allowed")
    args = parser.parse_args(argv)

    css = args.css.read_text(encoding="utf-8")
    offenders, hairlines = spacing_values(css)
    total = len(offenders) + len(hairlines)

    if offenders:
        print(f"FAIL: {len(offenders)} bare spacing px value(s) in {args.css}")
        for line, prop, value, token in offenders:
            print(f"  {args.css}:{line}: {prop}: {value}  (use var(--sp-N) instead of {token})")
        return 1

    if total > args.max_bare:
        print(f"FAIL: {total} bare spacing px value(s) left, budget is {args.max_bare}")
        for line, prop, value, token in hairlines:
            print(f"  {args.css}:{line}: {prop}: {value}")
        return 1

    print(f"OK: no bare spacing px in {args.css} besides {total} hairline(s):")
    for line, prop, value, _token in hairlines:
        print(f"  {args.css}:{line}: {prop}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
