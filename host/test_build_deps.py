"""WS-624 regression: packaging deps must stay pinned, and the guard must detect it.

These tests import the real production functions from ``build_deps`` (the same
ones ``build-host.yml`` calls before the PyInstaller build); they do not copy
any detection logic into the test file.  ``test_repo_packaging_deps_are_pinned``
reads the real repository requirement files, so reverting a pin in the repo
makes it fail -- it is not self-certifying.
"""

import os
import sys

import pytest

HOST_DIR = os.path.dirname(os.path.abspath(__file__))
if HOST_DIR not in sys.path:
    sys.path.insert(0, HOST_DIR)

from build_deps import (  # noqa: E402  (sys.path adjust must happen first)
    PACKAGING_REQUIREMENT_FILES,
    assert_packaging_pinned,
    find_unbounded,
)


def test_repo_packaging_deps_are_pinned():
    """The checked-in requirement files have no unbounded direct line."""
    assert set(PACKAGING_REQUIREMENT_FILES) == {
        "requirements-desktop.txt", "requirements-build.txt"}
    assert find_unbounded(HOST_DIR) == []
    assert_packaging_pinned(HOST_DIR)  # must not raise


def test_detector_rejects_an_unbounded_requirement(tmp_path):
    """Proof the guard is not a no-op: an open-ended '>=' is flagged."""
    (tmp_path / "requirements-desktop.txt").write_text(
        "pillow>=10.0\n", encoding="utf-8")
    (tmp_path / "requirements-build.txt").write_text(
        "pyinstaller==6.22.3\n", encoding="utf-8")
    problems = find_unbounded(str(tmp_path))
    assert any("requirements-desktop.txt:1" in p for p in problems)
    with pytest.raises(RuntimeError):
        assert_packaging_pinned(str(tmp_path))


def test_detector_rejects_bare_name_and_marker_only_equality(tmp_path):
    """A bare name is unbounded; '==' inside a ';' marker is not a version pin."""
    (tmp_path / "requirements-desktop.txt").write_text(
        'pythonnet>=3.0; sys_platform == "win32"\nkeyboard\n', encoding="utf-8")
    (tmp_path / "requirements-build.txt").write_text("", encoding="utf-8")
    problems = find_unbounded(str(tmp_path))
    assert len(problems) == 2, problems


def test_detector_accepts_bounded_requirements(tmp_path):
    """Upper bounds and exact pins are accepted."""
    (tmp_path / "requirements-desktop.txt").write_text(
        "pillow>=10,<13\npywebview>=6.0,<7\n", encoding="utf-8")
    (tmp_path / "requirements-build.txt").write_text(
        "pyinstaller==6.22.3\npyinstaller-hooks-contrib<2026.10\npytest>=8,<9\n",
        encoding="utf-8")
    assert find_unbounded(str(tmp_path)) == []
    assert_packaging_pinned(str(tmp_path))


def test_include_lines_are_not_treated_as_requirements(tmp_path):
    """'-r ...' includes are skipped, not flagged as unbounded."""
    (tmp_path / "requirements-build.txt").write_text(
        "-r requirements.txt\n-r requirements-desktop.txt\npyinstaller==6.22.3\n",
        encoding="utf-8")
    (tmp_path / "requirements-desktop.txt").write_text(
        "pillow>=10,<13\n", encoding="utf-8")
    assert find_unbounded(str(tmp_path)) == []
