#!/usr/bin/env python3
"""Guard WS-504: the config portal must be able to identify a pressed key.

The portal is the only surface where wiring is typed in, and BLE is off while
it runs, so the host app cannot show which key was just pressed: a typo'd or
mis-seated wire could only be found by leaving the portal, opening the host
app, looking at the highlight and coming back.  WS-504 closes that loop with
a read-only ``GET /keys/state`` that reports the last debounced press.

This checker reads the *production* source (``src/main.cpp``) and asserts the
structure that makes the feature work and keeps it harmless:

1. ``recordInputPress()`` exists, is fed by the existing debounced scan (one
   call per physical press, not a second sampler), ignores inputs outside the
   active layout, and bumps the monotonic ``seq`` the page polls for;
2. ``GET /keys/state`` is registered **inside** ``setupProvisioning()`` only,
   answers ``application/json`` with ``seq`` / ``index`` / ``gpio``, and does
   not write NVS, send HID, or leak into the ``CFG:`` / ``CMD:`` line protocol;
3. ``cfgPage()`` carries the self-check panel, the row ids / tag cells the
   script highlights, and the 400 ms poller with the 1.5 s fade-out.

It fails on the pre-WS-504 tree and again if a later change drops the feature
or lets it touch the protocol / storage / HID paths.

    python tools/check_key_selfcheck.py [src/main.cpp]
"""

from __future__ import annotations

import sys

from check_ota_write_verify import DEFAULT_SOURCE, _match_brace, strip_comments


def _require(failures: list[str], cond: bool, where: str, msg: str) -> None:
    if not cond:
        failures.append("%s: %s" % (where, msg))


def _block(src: str, open_paren: int) -> str:
    """Text between the '(' at *open_paren* and its matching ')'."""
    depth = 0
    for i in range(open_paren, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[open_paren + 1:i]
    return ""


def _function_body(src: str, name: str) -> str:
    """Body of ``name(...) { ... }`` or "" when it is not defined."""
    at = src.find(name + "(")
    if at < 0:
        return ""
    brace = src.find("{", at)
    if brace < 0:
        return ""
    end = _match_brace(src, brace)
    return src[brace + 1:end] if end > brace else ""


def check_source(raw: str) -> list[str]:
    src = strip_comments(raw)
    failures: list[str] = []
    where = "recordInputPress"

    struct_at = src.find("struct RecentInput")
    _require(failures, struct_at >= 0, "state", "struct RecentInput is missing")
    _require(failures, "g_recentInput = {0, -1, -1}" in src, "state",
             "g_recentInput must start at seq=0 / index=-1 / gpio=-1")

    body = _function_body(src, "static void recordInputPress")
    _require(failures, bool(body), where, "recordInputPress() is missing")
    if body:
        _require(failures, "inputCount()" in body, where,
                 "must ignore inputs outside the active layout")
        _require(failures, "g_recentInput.seq++" in body, where,
                 "must bump the monotonic seq the page polls for")
        _require(failures, "g_recentInput.index = idx" in body, where,
                 "must record the input index")
        _require(failures, "inputPin(idx)" in body, where,
                 "must record the GPIO the input sits on")
        _require(failures, "digitalRead" not in body, where,
                 "must reuse the debounced scan instead of sampling on its own")

    # 2. fed by the existing scan, one call per debounced press.
    scan_at = src.find("switches[i].pressed()")
    _require(failures, scan_at >= 0, "scan", "the button scan loop is missing")
    if scan_at >= 0:
        brace = src.find("{", scan_at)
        pressed_block = src[brace:_match_brace(src, brace)] if brace >= 0 else ""
        _require(failures, "recordInputPress(i)" in pressed_block, "scan",
                 "the debounced button scan must call recordInputPress(i)")

    # 3. the endpoint: config mode only, JSON, read-only.
    route_at = src.find('cfgServer.on("/keys/state"')
    _require(failures, route_at >= 0, "endpoint", "GET /keys/state is not registered")
    if route_at >= 0:
        _require(failures, "HTTP_GET" in src[route_at:route_at + 120], "endpoint",
                 "the route must be GET only")
        handler = src[route_at:_match_brace(src, src.find("{", route_at))]
        _require(failures, "application/json" in handler, "endpoint",
                 "must answer application/json")
        # C++ escapes the JSON quotes: "{\"seq\":1,...}"
        flat = handler.replace('\\"', '"')
        for field in ('"seq"', '"index"', '"gpio"'):
            _require(failures, field in flat, "endpoint",
                     "the JSON body must carry %s" % field)
        _require(failures, "no-store" in handler, "endpoint",
                 "must not be cached (the phone polls it)")
        for banned, why in (
            ("saveConfig", "must not write NVS"),
            ("preferences.", "must not touch NVS"),
            ("sendKeyForInput", "must not send HID"),
            ("sendToHost", "must not emit CFG:/CMD:/BTN: lines"),
            ('"CFG:', "must stay out of the CFG: line protocol"),
            ('"CMD:', "must stay out of the CMD: line protocol"),
            ('"BTN:', "must stay out of the BTN: line protocol"),
        ):
            _require(failures, banned not in handler, "endpoint", why)

        # Only the portal registers it: normal (BLE) mode must not route it.
        provisioning = _function_body(src, "static void setupProvisioning")
        _require(failures, bool(provisioning) and '"/keys/state"' in provisioning,
                 "endpoint", "the route belongs to the config portal, not the boot path")

    # 4. the portal page: panel, row hooks, poller.
    page = _function_body(src, "static String cfgPage")
    _require(failures, bool(page), "page", "cfgPage() is missing")
    if page:
        _require(failures, "id='chk'" in page, "page", "the self-check panel is missing")
        _require(failures, "按下任意按键开始自检" in page, "page",
                 "the empty state is missing")
        _require(failures, "id='row" in page and "id='tag" in page, "page",
                 "pin rows need an id and a tag cell for the highlight")
        _require(failures, "当前固件不支持按键自检" in page, "page",
                 "a firmware without the endpoint must be reported as unsupported")
        _require(failures, "配网已退出，请重新进入" in page, "page",
                 "leaving the portal must be reported, not shown as 'busy'")
        _require(failures, "设备忙，稍后自动重试" in page, "page",
                 "transient failures must say so")
        _require(failures, "'/keys/state'" in page, "page",
                 "the page must poll /keys/state")
        _require(failures, "later(fails>=3?2000:400)" in page, "page",
                 "the poll interval must stay at 400 ms (backing off when failing)")
        _require(failures, "setTimeout(clearHit,1500)" in page, "page",
                 "the row highlight must fade after 1.5 s")
        _require(failures, "'最近触发：'" in page and "'已触发 · GPIO '" in page, "page",
                 "the panel must keep the last trigger and tag the fired row")

    return failures


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_SOURCE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
    except OSError as exc:
        print("FAIL: cannot read %s: %s" % (path, exc))
        return 1

    failures = check_source(source)
    if failures:
        for msg in failures:
            print("FAIL: " + msg)
        return 1
    print("OK: portal key self-check is wired end to end and stays read-only")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
