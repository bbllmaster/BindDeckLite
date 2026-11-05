#!/usr/bin/env python3
"""Resolve and validate the "本次更新" body of a release (WS-523).

Every GitHub Release must state what changed, in a fixed structure::

    ## 本次更新
    ### 新增
    - …
    ### 修复
    - …
    ### 变更 / 不兼容
    - …（无则写「无」）
    ### 升级提示
    - …（无则写「无」）

Sources, in priority order:

1. ``release-notes/<tag>.md`` — the hand-written notes for a formal tag.
2. ``CHANGELOG.md`` — a ``## <tag>`` section (formal tags).
3. ``CHANGELOG.md`` — the *unreleased* entries (the top-level sections above
   the first ``## v…`` heading), summarised into the same structure. This is
   what the rolling ``main`` builds use.

If the resolved text does not carry the full structure, the script exits
non-zero and the publish job aborts **before** creating the Release — a
release without "本次更新" is an unfinished release (WS-523 验收 11).

Usage::

    python tools/release_notes.py --tag v1.5.0 --out NOTES.md
"""

from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 正文里必须有的一级标题。
REQUIRED_HEADING = "## 本次更新"
#: 正文里必须有的小节（按出现顺序）。"变更 / 不兼容" 一个标题算一节。
REQUIRED_SECTIONS = ("新增", "修复", "变更", "升级提示")

#: CHANGELOG 里代表「已发布版本」的标题前缀 —— 滚动包只汇总它上面的条目。
RELEASED_HEADING = re.compile(r"^##\s+v\d")


def validate_notes(text: str) -> list:
    """Return the list of missing required parts (empty = valid)."""
    missing = []
    body = text or ""
    if REQUIRED_HEADING not in body:
        missing.append(REQUIRED_HEADING)
    for section in REQUIRED_SECTIONS:
        # "### 变更 / 不兼容" 也算 "### 变更"。
        if not re.search(r"^###\s*%s" % re.escape(section), body, re.MULTILINE):
            missing.append("### " + section)
    return missing


def changelog_section(path: str, tag: str) -> str:
    """Return the CHANGELOG section for *tag* (empty when absent).

    The heading may carry a date suffix (``## v1.3.0 — 2026-10-19``); the
    tag itself is the part before the dash.
    """
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    out = []
    collecting = False
    for line in lines:
        if line.startswith("## "):
            if collecting:
                break
            title = line[3:].strip()
            collecting = title == tag or title.split("—")[0].strip() == tag
            continue
        if collecting:
            out.append(line)
    return "\n".join(out).strip()


def changelog_summary(path: str) -> str:
    """Summarise the unreleased CHANGELOG entries into the fixed structure.

    The CHANGELOG keeps the newest entries first; the ``## v…`` headings mark
    the already-released versions. Everything above the first such heading is
    unreleased and belongs to the next release.
    """
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    titles = []
    for line in lines:
        if not line.startswith("## "):
            continue
        if RELEASED_HEADING.match(line):
            break
        title = line[3:].strip()
        if title and title != "Changelog":
            titles.append(title)

    body = ["## 本次更新", "", "### 新增", ""]
    if titles:
        for title in titles:
            body.append("- %s" % title)
    else:
        body.append("- 无")
    body += ["", "### 修复", "", "- 无", "",
             "### 变更 / 不兼容", "", "- 无", "",
             "### 升级提示", "", "- 无", ""]
    return "\n".join(body)


def resolve_notes(tag: str, notes_dir: str = "", changelog: str = "") -> str:
    """Resolve the notes text for *tag*.

    Per-tag file first, then the CHANGELOG. A formal tag (``v…``) with no
    source at all resolves to nothing — the caller then fails the release,
    which is exactly the hard gate WS-523 asks for.
    """
    if notes_dir:
        path = os.path.join(notes_dir, "%s.md" % tag)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
    if changelog and os.path.isfile(changelog):
        if tag.startswith("v"):
            section = changelog_section(changelog, tag)
            if section:
                return section
            # A formal tag with no notes anywhere → no source (release fails).
            return ""
        return changelog_summary(changelog)
    return ""


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Resolve + validate the release '本次更新' body.")
    parser.add_argument("--tag", required=True,
                        help="release tag (e.g. v1.5.0 or main-20261104-1200)")
    parser.add_argument("--out", required=True,
                        help="where to write the resolved notes")
    parser.add_argument("--notes-dir", default=os.path.join(ROOT, "release-notes"),
                        help="directory holding per-tag notes files")
    parser.add_argument("--changelog", default=os.path.join(ROOT, "CHANGELOG.md"),
                        help="CHANGELOG.md used as a notes source")
    return parser.parse_args(argv)


def main():
    args = _parse_args(sys.argv[1:])
    text = resolve_notes(args.tag, args.notes_dir, args.changelog)
    missing = validate_notes(text)
    if missing:
        print("release notes for %s are incomplete." % args.tag, file=sys.stderr)
        print("missing: %s" % ", ".join(missing), file=sys.stderr)
        if not os.path.isfile(os.path.join(args.notes_dir, "%s.md" % args.tag)):
            print("write %s with the required structure, or add a '## %s' "
                  "section to CHANGELOG.md"
                  % (os.path.join(args.notes_dir, "%s.md" % args.tag), args.tag),
                  file=sys.stderr)
        return 1

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")
    print("==> wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
