#!/usr/bin/env python3
"""Resolve and validate the "本次更新" body of a release (WS-523, WS-558).

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
3. ``git log <since>..HEAD`` — the commit range since the previous rolling
   ``main-*`` tag. This is what a rolling build publishes: only what changed
   since the last rolling Release (WS-558), not the whole unreleased backlog.
4. ``CHANGELOG.md`` — the *unreleased* entries, only as a fallback when the
   git history cannot be read at all (the notes then say what is unreleased
   since the last formal version, which is coarser but still truthful).

Commit subjects are grouped by conventional-commit type: ``feat`` → 新增,
``fix`` → 修复, a trailing ``!`` or a ``BREAKING CHANGE`` footer → 变更 /
不兼容, ``chore`` / ``docs`` / ``refactor`` / ``test`` / ``build`` / ``ci`` /
``style`` are left out, and an untyped subject (this repo's usual
``WS-xxx: …`` style) counts as 新增. Identical entries are listed once.

If the resolved text does not carry the full structure, the script exits
non-zero and the publish job aborts **before** creating the Release — a
release without "本次更新" is an unfinished release (WS-523 验收 11).

Usage::

    python tools/release_notes.py --tag v1.5.0 --out NOTES.md
    python tools/release_notes.py --tag main-20261109-1200 --since main-20261108-1703 --out NOTES.md
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 正文里必须有的一级标题。
REQUIRED_HEADING = "## 本次更新"
#: 正文里必须有的小节（按出现顺序）。"变更 / 不兼容" 一个标题算一节。
REQUIRED_SECTIONS = ("新增", "修复", "变更", "升级提示")

#: CHANGELOG 里代表「已发布版本」的标题前缀 —— 只有兜底汇总会用到它。
RELEASED_HEADING = re.compile(r"^##\s+v\d")

#: conventional commit 标题：``type(scope)!: summary``。
CONVENTIONAL_COMMIT = re.compile(
    r"^(?P<type>[A-Za-z]+)(?:\([^)]*\))?(?P<breaking>!)?:\s*(?P<summary>.*)$")

#: 提交类型 → 「### 修复」。
FIX_TYPES = {"fix", "hotfix", "bugfix"}
#: 纯工程改动 —— 不是用户能感知的变化，不进发布说明。
OMIT_TYPES = {"chore", "docs", "doc", "refactor", "test", "tests",
              "build", "ci", "style", "release"}

SECTION_ADDED = "新增"
SECTION_FIXED = "修复"
SECTION_BREAKING = "变更"
SECTION_UPGRADE = "升级提示"


class GitError(RuntimeError):
    """git 不可用、路径不是仓库，或命令执行失败。"""


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


def _section_lines(entries) -> list:
    """Render one section's bullets — an empty section is an explicit 「无」。"""
    return ["- %s" % entry for entry in entries] if entries else ["- 无"]


def compose_notes(added=(), fixed=(), changed=()) -> str:
    """Render the fixed 「本次更新」 structure from the three buckets."""
    body = [REQUIRED_HEADING, "", "### " + SECTION_ADDED]
    body += _section_lines(list(added))
    body += ["", "### " + SECTION_FIXED]
    body += _section_lines(list(fixed))
    body += ["", "### 变更 / 不兼容"]
    body += _section_lines(list(changed))
    body += ["", "### " + SECTION_UPGRADE]
    if changed:
        body.append("- 本次包含不兼容变更，升级前请先阅读「变更 / 不兼容」。")
    else:
        body.append("- 无")
    body.append("")
    return "\n".join(body)


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

    Fallback only: this is the *whole* unreleased backlog since the last
    formal version, so it must not be used for a rolling release while the
    git history is available (that was WS-558's bug).
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

    return compose_notes(added=titles)


def _run_git(args, repo: str) -> str:
    """Run git in *repo* and return stdout decoded as UTF-8.

    Both ends are pinned: git writes the commit message bytes verbatim, so
    decoding must not follow the runner's locale (Windows runners use an ANSI
    code page — the same trap WS-532 fixed for stderr).
    """
    try:
        result = subprocess.run(["git", "--no-pager"] + list(args),
                                cwd=repo, capture_output=True)
    except OSError as exc:
        raise GitError(str(exc))
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(message or "git %s failed" % " ".join(args))
    return result.stdout.decode("utf-8", "replace")


def _try_git(args, repo: str) -> str:
    """Like :func:`_run_git` but an expected failure (e.g. no matching tag) is ""."""
    try:
        return _run_git(args, repo)
    except GitError:
        return ""


def resolve_since(repo: str = ROOT, head: str = "HEAD") -> str:
    """Return the lower bound of a rolling release's commit range.

    The previous rolling ``main-*`` tag wins; the first rolling build falls
    back to the newest formal ``v*`` tag. ``""`` means "no tag at all — start
    from the first commit", and a missing/unreadable repository raises
    :class:`GitError` instead of silently widening the range.
    """
    for pattern in ("main-*", "v*"):
        ref = _try_git(["describe", "--tags", "--abbrev=0",
                        "--match", pattern, "%s^" % head], repo).strip()
        if ref:
            return ref
    # No tag reachable: still assert this is a usable git checkout, so that a
    # broken/missing history fails loudly instead of producing empty notes.
    _run_git(["rev-parse", "--verify", head], repo)
    return ""


def git_commits(since: str, head: str = "HEAD", repo: str = ROOT):
    """Return ``[(subject, body), …]`` for ``since..head`` (all commits if no since)."""
    rev = "%s..%s" % (since, head) if since else head
    # %x1f separates subject from body, %x1e separates records.
    out = _run_git(["log", "--no-color", "--pretty=format:%s%x1f%b%x1e", rev],
                   repo)
    commits = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        subject, _, body = record.partition("\x1f")
        commits.append((subject.strip(), body.strip()))
    return commits


def classify_commit(subject: str, body: str = ""):
    """Map one commit to ``(section, text)``; ``None`` means "leave it out"."""
    subject = (subject or "").strip()
    if not subject:
        return None

    breaking = "BREAKING CHANGE" in (body or "").upper()
    match = CONVENTIONAL_COMMIT.match(subject)
    if match:
        breaking = breaking or bool(match.group("breaking"))
        summary = match.group("summary").strip() or subject
        if breaking:
            return (SECTION_BREAKING, summary)
        if match.group("type").lower() in FIX_TYPES:
            return (SECTION_FIXED, summary)
        if match.group("type").lower() in OMIT_TYPES:
            return None
        return (SECTION_ADDED, summary)

    # Untyped subject keeps its full text: this repo's commits read like
    # "WS-546: 每个输入可配修饰键…", which is user-visible and must not vanish.
    if breaking:
        return (SECTION_BREAKING, subject)
    return (SECTION_ADDED, subject)


def summarize_commits(commits) -> str:
    """Render ``[(subject, body), …]`` into the fixed 「本次更新」 structure."""
    buckets = {SECTION_ADDED: [], SECTION_FIXED: [], SECTION_BREAKING: []}
    seen = set()
    for subject, body in commits:
        entry = classify_commit(subject, body)
        if entry is None:
            continue
        section, text = entry
        if (section, text) in seen:
            continue
        seen.add((section, text))
        buckets[section].append(text)
    return compose_notes(buckets[SECTION_ADDED], buckets[SECTION_FIXED],
                         buckets[SECTION_BREAKING])


def rolling_notes(since: str = "", head: str = "HEAD", repo: str = ROOT) -> str:
    """Notes for a rolling build: the increment since the previous rolling tag."""
    start = since or resolve_since(repo, head)
    return summarize_commits(git_commits(start, head, repo))


def resolve_notes(tag: str, notes_dir: str = "", changelog: str = "",
                  since: str = "", repo: str = ROOT) -> str:
    """Resolve the notes text for *tag*.

    Per-tag file first, then the CHANGELOG for a formal tag. A rolling tag
    (``main-…``) uses the git range ``since..HEAD``; only when git cannot be
    read at all does it fall back to the unreleased CHANGELOG entries. A
    formal tag with no source at all resolves to nothing — the caller then
    fails the release, which is exactly the hard gate WS-523 asks for.
    """
    if notes_dir:
        path = os.path.join(notes_dir, "%s.md" % tag)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()

    formal = tag.startswith("v")
    if formal:
        if changelog and os.path.isfile(changelog):
            return changelog_section(changelog, tag)
        # A formal tag with no notes anywhere → no source (release fails).
        return ""

    try:
        return rolling_notes(since, repo=repo)
    except GitError as exc:
        if changelog and os.path.isfile(changelog):
            print("warning: 读不到 git 历史（%s），退回 CHANGELOG 未发布条目汇总"
                  % exc, file=sys.stderr)
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
    parser.add_argument("--since", default="",
                        help="start of a rolling build's commit range (previous "
                             "main-* tag, else the newest v* tag); auto-detected "
                             "when omitted")
    parser.add_argument("--repo", default=ROOT,
                        help="git repository to read the commit range from")
    return parser.parse_args(argv)


def main():
    args = _parse_args(sys.argv[1:])
    text = resolve_notes(args.tag, args.notes_dir, args.changelog,
                         since=args.since, repo=args.repo)
    missing = validate_notes(text)
    if missing:
        print("release notes for %s are incomplete." % args.tag, file=sys.stderr)
        print("missing: %s" % ", ".join(missing), file=sys.stderr)
        if args.tag.startswith("v"):
            print("write %s with the required structure, or add a '## %s' "
                  "section to CHANGELOG.md"
                  % (os.path.join(args.notes_dir, "%s.md" % args.tag), args.tag),
                  file=sys.stderr)
        else:
            print("rolling notes come from 'git log <since>..HEAD'; make sure "
                  "--repo is a full clone (tags + history, fetch-depth: 0)",
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
