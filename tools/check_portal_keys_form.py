#!/usr/bin/env python3
"""Guard the config-portal button form fixes from WS-408 review (WS-419).

Four defects were fixed in ``src/main.cpp``; each one is cheap to undo by a
later edit and expensive to notice, because the failure mode is a page that
*renders* but silently loses information.  This checker reads the production
source (not a copy of it) and asserts the four invariants:

1. **400 fallback** -- the ``/keys`` POST handler rejects with ``cfgPage(&form)``
   in its final ``else``.  Reaching there with ``form.errors`` empty means the
   page has no error bar at all, which is exactly what happens when
   ``pinRejectReason()`` and ``isValidPinSet()`` drift apart (they are two
   copies of the same whitelist).  The handler must push a fallback message
   before rendering.

2. **Raw text echo** -- the pin inputs must be ``type='text'`` with
   ``inputmode='numeric'``.  Echoing "abc" into ``type='number'`` makes the
   browser blank the field, so a rejected save shows an empty box and the user
   cannot see what they typed.

3. **aria wiring** -- a row marked ``aria-invalid='true'`` must point at the
   reason list (``aria-describedby='cfgerr'``) and that id must exist on the
   banner, otherwise a screen reader announces "invalid" with no reason.

4. **C3 pin range wording** -- GPIO18/19 physically exist on the C3 (they are
   the USB D-/D+ pair, unused on this board); the rejection text must not claim
   the C3 stops at GPIO10,20,21.

Usage:  python tools/check_portal_keys_form.py [path/to/main.cpp]
Exit:   0 = all checks pass, 1 = at least one invariant is broken.
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


def check_400_fallback(src: str) -> None:
    """The 400 branch must never render a page without a reason."""
    send = src.find('cfgPage(&form));')
    if send < 0:
        fail("找不到 /keys 的 400 分支：cfgPage(&form)); 未出现")
        return
    window = src[max(0, send - 800):send]

    guard = window.find("form.errors.empty()")
    if guard < 0:
        fail("400 分支缺少 form.errors.empty() 兜底：两份引脚白名单一旦不一致，"
             "会渲染一张没有错误条的空白 400 页")
        return

    m = re.search(r'form\.errors\.push_back\(\s*"([^"]*)"\s*\)', window[guard:])
    if not m:
        fail("400 分支的兜底没有 push_back 任何文案")
        return
    msg = m.group(1)
    if len(msg.strip()) < 5:
        fail(f"400 兜底文案过短或为空：{msg!r}")
    if "引脚" not in msg:
        fail(f"400 兜底文案没有说明是引脚问题：{msg!r}")


def check_pin_input_type(src: str) -> None:
    """Non-numeric input must survive the round-trip into the re-rendered page."""
    anchor = src.find("<input name='pin\" + String(i)")
    if anchor < 0:
        fail("找不到按键引脚输入框的渲染代码（name='pin' + String(i)）")
        return
    frag = src[anchor:anchor + 240]

    if "type='number'" in frag:
        fail("按键输入框仍是 type='number'：回填 abc 会被浏览器清空，用户看不到自己填的值")
    if "type='text'" not in frag:
        fail("按键输入框缺少 type='text'（非数字原文需要原样回填）")
    if "inputmode='numeric'" not in frag:
        fail("按键输入框缺少 inputmode='numeric'（移动端应保留数字键盘）")


def check_aria(src: str) -> None:
    """aria-invalid without aria-describedby tells the user nothing."""
    if "aria-invalid='true'" not in src:
        fail("按键输入框不再输出 aria-invalid（坏行标记丢失）")
    if "aria-describedby='cfgerr'" not in src:
        fail("aria-invalid 缺少 aria-describedby 指向错误条，读屏软件听不到原因")
    if "id='cfgerr'" not in src:
        fail("错误条缺少 id='cfgerr'，aria-describedby 指向一个不存在的元素")

    pos = src.find("aria-describedby='cfgerr'")
    if pos > 0 and "!form->errors.empty()" not in src[max(0, pos - 300):pos]:
        fail("aria-describedby 未随错误条一起出现（错误条不渲染时会指向悬空 id）")


def check_c3_range_text(src: str) -> None:
    """GPIO18/19 exist on the C3 even though this board does not use them."""
    if "GPIO0-10、18-21" not in src:
        fail("C3 引脚范围文案未修正为 GPIO0-10、18-21")
    if "GPIO0-10、20、21" in src:
        fail("仍残留旧文案「C3 只有 GPIO0-10、20、21」：18/19 物理存在，只是本机未用")


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SOURCE
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        print(f"无法读取 {path}: {exc}", file=sys.stderr)
        return 1

    check_400_fallback(src)
    check_pin_input_type(src)
    check_aria(src)
    check_c3_range_text(src)

    if FAILURES:
        print(f"{len(FAILURES)} 处不满足 WS-419 修复约定：")
        for msg in FAILURES:
            print(f"  - {msg}")
        return 1
    print("WS-419 配网页表单四点约定全部成立")
    return 0


if __name__ == "__main__":
    sys.exit(main())
