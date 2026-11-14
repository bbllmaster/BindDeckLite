"""Deterministic Windows packaging dependencies (WS-624).

The frozen onefile exe is built in CI from ``requirements-desktop.txt`` and
``requirements-build.txt``.  Those files used to carry unbounded requirements
(``pillow>=10.0``) and left ``pyinstaller-hooks-contrib`` unpinned, so on
2026-11-14 pip silently resolved Pillow 13.0.0 and hooks-contrib 2026.10 and
the frozen exe stopped starting (``tray_available=false`` with an empty error,
because the desktop startup path died before the probe).  The very same
workflow was green on main@d2373cf one day earlier, so this was dependency
drift, not a code regression on the PR.

Keep the whole packaging dependency set explicitly bounded so a future release
cannot silently change the frozen build again, and fail the build early if
anyone reintroduces an unbounded requirement.  This module is real build
tooling: ``build-host.yml`` calls :func:`assert_packaging_pinned` before the
expensive PyInstaller build, and ``test_build_deps.py`` imports these functions
to test them against the real requirement files.
"""

from __future__ import annotations

import os
import re

#: Files whose *direct* requirements are frozen into the Windows exe.  ``-r``
#: includes are resolved by pip at install time but are intentionally out of
#: scope here: ``requirements.txt`` is the lightweight runtime file used by
#: ``run.sh`` / ``run.bat`` and is allowed to stay looser.
PACKAGING_REQUIREMENT_FILES = ("requirements-desktop.txt", "requirements-build.txt")

# A requirement is deterministic only when it carries an upper bound or an
# exact/compatible pin.  A bare name (``keyboard``) or an open-ended ``>=`` is
# unbounded and must be rejected.  ``>=`` alone must NOT match: only ``==`` /
# ``===`` / ``~=`` / ``<`` / ``<=`` count.
_UPPER_BOUND = re.compile(r"===|==|~=|<=|<")
_INCLUDE = re.compile(r"^(-r\b|--requirement\b)")


def _direct_requirements(path):
    """Yield ``(lineno, text)`` for non-comment, non-``-r`` requirement lines.

    Environment markers are kept in ``text``; :func:`find_unbounded` strips
    them before inspecting the version specifier (a marker may itself contain
    ``==`` and must not masquerade as a pin).
    """
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.split("#", 1)[0].strip()
            if not line or _INCLUDE.match(line):
                continue
            yield lineno, line


def find_unbounded(requirements_dir=None) -> list[str]:
    """Return descriptions of unbounded packaging requirements.

    Reads the *real* requirement files from disk rather than duplicating any
    install logic, so removing a pin from the repo makes this return non-empty.
    """
    root = requirements_dir or os.path.dirname(os.path.abspath(__file__))
    problems: list[str] = []
    for filename in PACKAGING_REQUIREMENT_FILES:
        path = os.path.join(root, filename)
        if not os.path.exists(path):
            problems.append(f"{filename}: missing")
            continue
        for lineno, line in _direct_requirements(path):
            # Only the part before the ';' marker is the version specifier.
            spec_part = line.split(";", 1)[0]
            if not _UPPER_BOUND.search(spec_part):
                problems.append(
                    f"{filename}:{lineno}: {line} (no upper bound / exact pin)")
    return problems


def assert_packaging_pinned(requirements_dir=None) -> None:
    """Fail the build if any packaging requirement is unbounded (WS-624)."""
    problems = find_unbounded(requirements_dir)
    if problems:
        raise RuntimeError(
            "WS-624: packaging dependencies must stay pinned/bounded so the "
            "frozen exe is reproducible. Unbounded requirements:\n  "
            + "\n  ".join(problems))
