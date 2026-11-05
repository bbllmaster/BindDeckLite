#!/usr/bin/env python3
"""Derive the release tag / app version / channel from the git ref.

Both the ``release / host-exe`` job (which injects the version into
``host/_version.py``) and the ``release / publish`` job (which builds the
Release and the ``update.json`` manifest) call this script, so the version
that ends up inside the exe and the version in the manifest can never drift.

Version semantics (WS-523):

* tag ``v1.4.0``        → app version ``1.4.0``, formal release.
* tag ``v1.5.0-rc1``    → app version ``1.5.0-rc1``, prerelease.
* push to ``main``      → app version ``0.0.0+main.<short sha>``, prerelease
  (rolling build; the tag itself is ``main-<utc>`` and is computed by the
  publish job, which is the only place that creates the Release).

Usage::

    python tools/release_version.py --ref "$GITHUB_REF" [--sha "$GITHUB_SHA"]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: main 滚动构建的版本前缀（低于任何正式版）。
MAIN_VERSION_PREFIX = "0.0.0+main."


def short_sha(sha: str = "") -> str:
    """Short commit SHA (7 chars)."""
    if sha:
        return sha[:7]
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=ROOT, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def parse_ref(ref: str, sha: str = "") -> dict:
    """Turn a git ref into the release coordinates.

    Returns ``{tag, version, prerelease, formal}``:

    * ``tag``      — the git tag (formal releases only); empty for main builds.
    * ``version``  — the app version written into ``host/_version.py``.
    * ``prerelease``— whether the GitHub Release must be marked prerelease.
    * ``formal``   — whether this is a formal (non-rolling) release.
    """
    ref = (ref or "").strip()
    if ref.startswith("refs/tags/"):
        tag = ref[len("refs/tags/"):]
        version = tag[1:] if tag.startswith("v") else tag
        # A tag with a dash is a pre-release: it must not be pushed to users.
        prerelease = "-" in version
        return {"tag": tag, "version": version, "prerelease": prerelease,
                "formal": True}
    # main (or any other ref) → rolling build.
    return {"tag": "", "version": MAIN_VERSION_PREFIX + short_sha(sha),
            "prerelease": True, "formal": False}


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Derive release tag / version / channel from a git ref.")
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""),
                        help="git ref (defaults to $GITHUB_REF)")
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""),
                        help="commit SHA (defaults to $GITHUB_SHA)")
    parser.add_argument("--json", action="store_true",
                        help="print JSON instead of KEY=VALUE lines")
    parser.add_argument("--write-version-file", default="",
                        metavar="PATH",
                        help="write host/_version.py with the derived version")
    parser.add_argument("--build", default="",
                        help="build identifier written to APP_BUILD")
    return parser.parse_args(argv)


VERSION_FILE_TEMPLATE = '''"""BindDeckLite 上位机版本源（由发布流水线生成，勿手改）。

版本号来自发布 tag：``v1.4.0`` → ``1.4.0``；main 滚动构建 → ``0.0.0+main.<sha>``。
"""

APP_VERSION = "{version}"
APP_BUILD = "{build}"
'''


def write_version_file(path: str, version: str, build: str = "") -> None:
    """Write ``host/_version.py`` with the release version baked in."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(VERSION_FILE_TEMPLATE.format(version=version, build=build or ""))


def main():
    args = _parse_args(sys.argv[1:])
    info = parse_ref(args.ref, args.sha)
    if args.write_version_file:
        write_version_file(args.write_version_file, info["version"], args.build)
        # 进度信息走 stderr：stdout 只输出 KEY=VALUE（要写进 $GITHUB_OUTPUT）。
        print("==> wrote %s (version %s)" % (args.write_version_file, info["version"]),
              file=sys.stderr)
    if args.json:
        import json
        print(json.dumps(info))
        return 0
    print("TAG=%s" % info["tag"])
    print("VERSION=%s" % info["version"])
    print("PRERELEASE=%s" % ("true" if info["prerelease"] else "false"))
    print("FORMAL=%s" % ("true" if info["formal"] else "false"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
