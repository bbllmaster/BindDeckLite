#!/usr/bin/env python3
"""决定一次 push 需要重建发布内容的哪一半（WS-571）。

`release.yml` 以前无条件编译三个固件环境并打包 Windows exe：只改 `host/`
也会重编固件，只改 `src/` 也会重打 exe。这个脚本把「改了什么 → 要重建什么」
的判定集中到一处（YAML 里只调用它，不再各写一份路径清单），输出三个值：

===================  ==========================================================
``firmware``         需要重建固件（merged + OTA 镜像）
``host``             需要重建 Windows 上位机 exe
``docs``             本次改动涉及文档（纯文档改动时前两个都为 false）
===================  ==========================================================

判定规则：

* **tag（``v*`` 正式发布）**：两侧都重建，保证 tag 可复现，不沿用旧产物。
* **其余改动**：按路径分类；未知路径一律保守地「两侧都重建」，宁可多编
  一次也不少发半个包（少发的后果是 Release 缺 bin，OTA 直接失效）。
* **拿不到可信的 before（新分支 / force push / 首次推送）**：两侧都重建。

用法::

    python tools/release_changes.py --ref "$GITHUB_REF" \
        --before "${{ github.event.before }}" --sha "$GITHUB_SHA"

输出为 ``KEYVALUE`` 行（``firmware=true``），可直接追加进 ``$GITHUB_OUTPUT``。
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 只影响固件的路径（改了必须重编三个固件环境）。
FIRMWARE_PATTERNS = (
    "src/**",
    "include/**",
    "lib/**",
    "platformio.ini",
    "partitions/**",
    "bl_idf/**",
    "tools/build_release.py",
    ".github/workflows/build-firmware.yml",
)

#: 只影响上位机 exe 的路径（含只影响 manifest 版本号的工具）。
HOST_PATTERNS = (
    "host/**",
    "tools/build_manifest.py",
    "tools/release_version.py",
    ".github/workflows/build-host.yml",
)

#: 文档类路径：改了不影响任何产物。
DOC_PATTERNS = (
    "README.md",
    "CHANGELOG.md",
    "docs/**",
    "release-notes/**",
    "*.md",
)

#: 发布工具 / 发布工作流本身：任何改动都按「两侧都重建」处理，
#: 否则一个发布管线 bug 的修复可能落到某个半包里，产物与流程不一致。
SHARED_PATTERNS = (
    ".github/workflows/release.yml",
    "tools/release_changes.py",
    "tools/release_notes.py",
    "tools/test_workflows.py",
)


def _matches(path, patterns):
    """fnmatch 匹配；`dir/**` 同时匹配 `dir` 自身与任意深度子路径。"""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.endswith("/**") and path == pattern[:-3]:
            return True
    return False


def classify(paths):
    """把改动路径集合映射成 ``{firmware, host, docs}``。

    * 任一路径命中固件集合 → 需要重编固件；
    * 任一路径命中上位机集合 → 需要重打 exe；
    * 只有文档路径 → 两侧都跳过；
    * 未知路径 → 两侧都重建（保守）。
    """
    firmware = False
    host = False
    docs = False
    changed = False

    for raw in paths:
        path = (raw or "").strip()
        if not path:
            continue
        changed = True
        if _matches(path, SHARED_PATTERNS):
            firmware = True
            host = True
        elif _matches(path, FIRMWARE_PATTERNS):
            firmware = True
        elif _matches(path, HOST_PATTERNS):
            host = True
        elif _matches(path, DOC_PATTERNS):
            docs = True
        else:
            # 未知路径：重建两侧，绝不因为「没认出来」而少发东西。
            firmware = True
            host = True

    if not changed:
        # 空 diff（重复推送同一 commit 等）：按全量重建处理。
        firmware = True
        host = True

    return {"firmware": firmware, "host": host, "docs": docs}


def is_release_tag(ref):
    """tag 推送（``refs/tags/v1.4.0`` 等）必须全量重建。"""
    return (ref or "").strip().startswith("refs/tags/")


def changed_paths(before, sha, cwd=ROOT):
    """``git diff --name-only <before> <sha>``；拿不到可信范围时返回 None。"""
    before = (before or "").strip()
    sha = (sha or "").strip()
    if not sha or not before or set(before) == {"0"}:
        # 新分支 / force push / 首次推送：before 不可信。
        return None
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", before, sha],
            cwd=cwd, capture_output=True, text=True, check=True)
    except Exception:  # noqa: BLE001 - before 已被回收（force push）等
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def decide(ref, before="", sha="", cwd=ROOT):
    """返回 ``{firmware, host, docs}`` 的最终判定。"""
    if is_release_tag(ref):
        return {"firmware": True, "host": True, "docs": False}
    paths = changed_paths(before, sha, cwd=cwd)
    if paths is None:
        return {"firmware": True, "host": True, "docs": False}
    return classify(paths)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Decide whether the release must rebuild firmware / host.")
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""),
                        help="git ref (defaults to $GITHUB_REF)")
    parser.add_argument("--before", default=os.environ.get("GITHUB_EVENT_BEFORE", ""),
                        help="commit SHA before this push")
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""),
                        help="commit SHA of this push")
    parser.add_argument("--list", action="store_true",
                        help="also print the changed paths (to stderr)")
    return parser.parse_args(argv)


def main():
    args = _parse_args(sys.argv[1:])
    if args.list and not is_release_tag(args.ref):
        paths = changed_paths(args.before, args.sha)
        if paths:
            print("==> changed paths:", file=sys.stderr)
            for path in paths:
                print("    %s" % path, file=sys.stderr)
    result = decide(args.ref, args.before, args.sha)
    for key in ("firmware", "host", "docs"):
        print("%s=%s" % (key, "true" if result[key] else "false"))
    print("==> rebuild firmware=%s host=%s docs=%s"
          % (result["firmware"], result["host"], result["docs"]),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
