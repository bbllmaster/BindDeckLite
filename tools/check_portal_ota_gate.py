#!/usr/bin/env python3
"""Guard the config-portal OTA first-block read-back gate (WS-425).

The portal upload path shares ``otaVerifyFirstBlock()`` with the BLE path, but
``otaVerifyFirstBlock()`` only starts comparing bytes once ``g_otaReceived``
has grown past ``2 * 4096`` (two 4 KB Update flushes).  ``up.currentSize`` is
the length of the *current* 1460-byte HTTP chunk, so the portal branch must
accumulate it exactly like the BLE branch does -- assigning it makes the
counter peak at 1460 and the gate returns early forever.

This script reads the production source (``src/main.cpp``) and asserts the
three facts the fix depends on:

1. the ``UPLOAD_FILE_WRITE`` branch accumulates ``g_otaReceived`` (``+=``)
   and never plain-assigns it;
2. the ``UPLOAD_FILE_START`` branch resets ``g_otaReceived`` to 0, so a
   second portal OTA in the same power cycle cannot inherit the byte count of
   the previous one (which would trip the gate early with stale data);
3. the ``2 * 4096`` threshold inside ``otaVerifyFirstBlock()`` is intact.

Usage:  python tools/check_portal_ota_gate.py [path/to/main.cpp]
Exit:   0 = all checks pass, 1 = at least one structural defect found.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SOURCE = os.path.join(ROOT, "src", "main.cpp")

FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def strip_comments(text: str) -> str:
    """Blank out // and /* */ comments while preserving line numbers."""
    out = []
    i = 0
    n = len(text)
    state = "code"
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line"
                out.append("  ")
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block"
                out.append("  ")
                i += 2
                continue
            if ch in "\"'":
                state = ch
            out.append(ch)
            i += 1
        elif state == "line":
            if ch == "\n":
                state = "code"
                out.append(ch)
            else:
                out.append(" ")
            i += 1
        elif state == "block":
            if ch == "*" and nxt == "/":
                state = "code"
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
        else:  # inside a string/char literal
            out.append(ch)
            if ch == "\\":
                if nxt:
                    out.append(nxt)
                    i += 2
                    continue
            elif ch == state:
                state = "code"
            i += 1
    return "".join(out)


def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def find_unique(text: str, needle: str) -> int:
    """Offset of ``needle``; aborts when missing or ambiguous."""
    hits = [m.start() for m in re.finditer(re.escape(needle), text)]
    if len(hits) != 1:
        fail(
            "src/main.cpp: expected exactly one %r, found %d -- the config "
            "portal upload handler moved, update this checker" % (needle, len(hits))
        )
        return -1
    return hits[0]


def branch_region(text: str, marker: str, next_markers: list[str]) -> str:
    """Text of one ``up.status ==`` branch, up to the next branch marker."""
    start = find_unique(text, marker)
    if start < 0:
        return ""
    end = len(text)
    for nxt in next_markers:
        pos = text.find(nxt, start + len(marker))
        if pos != -1:
            end = min(end, pos)
    return text[start:end]


# Matches "g_otaReceived = ..." but not "g_otaReceived == ..." or "!=".
ASSIGN_RE = re.compile(r"g_otaReceived\s*=(?!=)")
ADD_ASSIGN_RE = re.compile(r"g_otaReceived\s*\+=\s*([^;]+);")


def check_write_branch(code: str) -> None:
    region = branch_region(
        code,
        "up.status == UPLOAD_FILE_WRITE",
        ["up.status == UPLOAD_FILE_END", "up.status == UPLOAD_FILE_ERROR"],
    )
    if not region:
        return

    base = code.find("up.status == UPLOAD_FILE_WRITE")

    m = ADD_ASSIGN_RE.search(region)
    if not m:
        fail(
            "src/main.cpp:%d portal UPLOAD_FILE_WRITE branch never accumulates "
            "g_otaReceived (no `g_otaReceived += ...;`) -- the 8192-byte gate "
            "in otaVerifyFirstBlock() can never fire"
            % line_of(code, base)
        )
    else:
        expr = m.group(1).strip()
        if "up.currentSize" not in expr:
            fail(
                "src/main.cpp:%d accumulates g_otaReceived from %r; expected "
                "the received chunk size up.currentSize"
                % (line_of(code, base + m.start()), expr)
            )

    for am in ASSIGN_RE.finditer(region):
        fail(
            "src/main.cpp:%d assigns g_otaReceived -- portal gate can never "
            "fire (up.currentSize is one 1460-byte chunk, threshold is 8192)"
            % line_of(code, base + am.start())
        )


def check_start_branch(code: str) -> None:
    region = branch_region(
        code,
        "up.status == UPLOAD_FILE_START",
        ["up.status == UPLOAD_FILE_WRITE", "up.status == UPLOAD_FILE_END"],
    )
    if not region:
        return

    if not re.search(r"g_otaReceived\s*=\s*0\s*;", region):
        base = code.find("up.status == UPLOAD_FILE_START")
        fail(
            "src/main.cpp:%d portal UPLOAD_FILE_START branch does not reset "
            "g_otaReceived to 0 -- a second portal OTA in the same power "
            "cycle inherits the previous byte count and trips the gate early"
            % line_of(code, base)
        )


def check_threshold(code: str) -> None:
    # The definition, not the forward declaration earlier in the file.
    fn = code.find("static bool otaVerifyFirstBlock() {")
    if fn == -1:
        fail("src/main.cpp: otaVerifyFirstBlock() definition not found")
        return
    body_end = code.find("\nstatic ", fn + 10)
    body = code[fn : body_end if body_end != -1 else len(code)]
    if not re.search(
        r"if\s*\(\s*g_otaReceived\s*<\s*2\s*\*\s*4096\s*\)\s*return\s+true\s*;", body
    ):
        fail(
            "src/main.cpp:%d otaVerifyFirstBlock() no longer waits for "
            "2 * 4096 received bytes -- gate precondition changed"
            % line_of(code, fn)
        )


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_SOURCE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError as exc:
        print("FAIL: cannot read %s: %s" % (path, exc))
        return 1

    code = strip_comments(source)

    check_write_branch(code)
    check_start_branch(code)
    check_threshold(code)

    if FAILURES:
        for msg in FAILURES:
            print("FAIL: " + msg)
        return 1

    print("OK: portal gate reached (accumulates, resets per session, threshold intact)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
