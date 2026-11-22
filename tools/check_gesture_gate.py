#!/usr/bin/env python3
"""Guard WS-672: the portal gesture must be switchable without swallowing keys.

The 10th input (encoder push / button 10) carries two jobs: it is a normal key
AND the "double press -> reboot into the config portal" gesture. When it is
bound to play/pause or Enter, a fast double press used to restart the deck,
drop BLE and open a 5 minute hotspot with no way to tell why.

WS-672 adds one persisted switch (``CFG:GESTURE:0|1``, NVS ``gestureEnabled``,
default on) that gates *only* the restart. This checker reads the production
source (``src/main.cpp``) and asserts the structure that makes that true:

1. ``gestureEnabled`` defaults to ``true`` and is written / read in NVS, so an
   upgraded deck keeps today's behaviour and the value survives a power cycle;
2. the ``g_bootReq = BOOT_CFG`` assignment is gated by ``gestureEnabled``, while
   ``sendKeyForInput(9)`` and the ``BTN:<id>:9`` report stay OUTSIDE that gate
   (swallowing the key press is the one bug this feature must never have);
3. the parser accepts exactly ``0`` / ``1``, logs ``REJECTED`` with the kept
   value otherwise, and only the accepted path calls ``saveConfig()``;
4. the read-only probe ``CFG:GESTURE`` is matched *before* the ``CFG:GESTURE:``
   prefix branch and answers ``GESTURE:<0|1>``;
5. ``CMD:GET_CFG`` appends ``CFG_GESTURE:<0|1>`` after ``CFG_MEDIA:1`` as the
   very last line (protocol rule: new lines only ever go at the end).

It fails on the pre-WS-672 tree (no such symbols) and again if a later change
drops the gate, moves the key report inside it, or widens the accepted values.

    python tools/check_gesture_gate.py [src/main.cpp]
"""

from __future__ import annotations

import sys

from check_ota_write_verify import DEFAULT_SOURCE, _match_brace, strip_comments


def _require(failures: list[str], cond: bool, where: str, msg: str) -> None:
    if not cond:
        failures.append("%s: %s" % (where, msg))


def _body_after(src: str, needle: str) -> str:
    """Text between the first '{' after *needle* and its matching '}'."""
    at = src.find(needle)
    if at < 0:
        return ""
    brace = src.find("{", at)
    if brace < 0:
        return ""
    end = _match_brace(src, brace)
    return src[brace + 1:end] if end > brace else ""


def _branch_after(src: str, needle: str) -> tuple[str, str]:
    """For ``... if (<needle>) { then } else { otherwise }`` return (then, else).

    Comments must already be stripped. An empty string is returned when the
    needle or a brace cannot be found.
    """
    at = src.find(needle)
    if at < 0:
        return "", ""
    brace = src.find("{", at)
    if brace < 0:
        return "", ""
    end = _match_brace(src, brace)
    if end <= brace:
        return "", ""
    then = src[brace + 1:end]
    rest = src[end + 1:end + 40]
    if not rest.lstrip().startswith("else"):
        return then, ""
    else_brace = src.find("{", end + 1)
    if else_brace < 0:
        return then, ""
    else_end = _match_brace(src, else_brace)
    return then, (src[else_brace + 1:else_end] if else_end > else_brace else "")


def _function_body(src: str, name: str) -> str:
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

    # 1. the flag itself: default on, persisted.
    _require(failures, "bool gestureEnabled = true;" in src, "state",
             "gestureEnabled must be declared and default to true")
    save = _function_body(src, "static void saveConfig")
    load = _function_body(src, "static void loadConfig")
    _require(failures, bool(save), "nvs", "saveConfig() is missing")
    _require(failures, bool(load), "nvs", "loadConfig() is missing")
    _require(failures, 'putBool("gestureEnabled", gestureEnabled)' in save, "nvs",
             "saveConfig() must persist gestureEnabled")
    _require(failures, 'getBool("gestureEnabled", true)' in load, "nvs",
             "loadConfig() must read gestureEnabled with a true default")

    # 2. the gate: only the reboot is conditional; the key press is not.
    gate_needle = "armed && gestureEnabled && lastPressAt != 0"
    _require(failures, gate_needle in src, "gate",
             "the double-click condition must include gestureEnabled")
    then, _otherwise = _branch_after(src, gate_needle)
    _require(failures, bool(then), "gate", "cannot read the double-click branch")
    if then:
        _require(failures, "g_bootReq = BOOT_CFG" in then, "gate",
                 "the gated branch must be the BOOT_CFG assignment")
        _require(failures, "sendKeyForInput" not in then, "gate",
                 "the switch must not swallow the HID key press")
        _require(failures, "BTN:" not in then, "gate",
                 "the switch must not swallow the BTN: report")

    press_at = src.find("stable = raw;")
    _require(failures, press_at >= 0, "press", "the input-9 debounce block is missing")
    if press_at >= 0:
        brace = src.find("{", src.find("if (stable)", press_at))
        press = src[brace:_match_brace(src, brace)] if brace >= 0 else ""
        _require(failures, "sendKeyForInput(9)" in press, "press",
                 "input 9 must still send its HID key on every press")
        _require(failures, 'sendToHost("BTN:" + g_devId + ":9")' in press, "press",
                 "input 9 must still report BTN:<id>:9 on every press")
        _require(failures, "lastPressAt = now;" in press, "press",
                 "lastPressAt must keep tracking every press (handfeel/window)")

    # 3. strict parser: 0|1 only, rejection keeps the value, no NVS write on it.
    probe_at = src.find('data == "CFG:GESTURE"')
    write_at = src.find('data.startsWith("CFG:GESTURE:")')
    _require(failures, probe_at >= 0, "parser",
             'the read-only probe (data == "CFG:GESTURE") is missing')
    _require(failures, write_at >= 0, "parser",
             'the write branch (data.startsWith("CFG:GESTURE:")) is missing')
    _require(failures, 0 <= probe_at < write_at, "parser",
             "the exact probe must be matched before the prefix branch, "
             "otherwise it is swallowed")
    if probe_at >= 0:
        probe_body = _body_after(src, 'data == "CFG:GESTURE"')
        _require(failures, 'sendToHost("GESTURE:"' in probe_body, "parser",
                 "the probe must answer GESTURE:<0|1>")
    if write_at >= 0:
        write_body = _body_after(src, 'data.startsWith("CFG:GESTURE:")')
        accept, reject = _branch_after(write_body, 'if (v == "0" || v == "1")')
        _require(failures, bool(accept), "parser", "cannot read the write branch")
        _require(failures, "substring(12)" in write_body, "parser",
                 '"CFG:GESTURE:" is 12 chars - substring must use 12')
        _require(failures, 'v == "0" || v == "1"' in write_body, "parser",
                 "only the exact values 0 and 1 may be accepted")
        _require(failures, "gestureEnabled = (v == \"1\")" in accept, "parser",
                 "the accepted path must store the parsed value")
        _require(failures, "saveConfig()" in accept, "parser",
                 "the accepted path must persist the value")
        _require(failures, "REJECTED" in reject, "parser",
                 "a bad value must log a REJECTED line")
        _require(failures, "(empty)" in reject, "parser",
                 "the rejection log must name an empty value as (empty)")
        _require(failures, "saveConfig()" not in reject, "parser",
                 "the rejection path must not write NVS")
        _require(failures, "gestureEnabled =" not in reject, "parser",
                 "the rejection path must not change the value")

    # 4. GET_CFG: CFG_GESTURE appended after CFG_MEDIA, as the last line.
    media_at = src.find('sendToHost("CFG_MEDIA:1")')
    gesture_at = src.find('sendToHost("CFG_GESTURE:"')
    _require(failures, media_at >= 0 and gesture_at >= 0, "readback",
             "CMD:GET_CFG must report CFG_MEDIA:1 and CFG_GESTURE:<0|1>")
    _require(failures, 0 <= media_at < gesture_at, "readback",
             "CFG_GESTURE must be appended after CFG_MEDIA (append-only rule)")
    if 0 <= media_at < gesture_at:
        tail = src[gesture_at:src.find('CMD:GET_TZ', gesture_at)]
        _require(failures, tail.count("sendToHost(") == 1, "readback",
                 "CFG_GESTURE must be the very last line CMD:GET_CFG sends")

    # 5. version bump tied to the feature.
    _require(failures, '#define FW_VERSION "1.3.14"' in src, "version",
             "FW_VERSION must be 1.3.14 for this feature")

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
    print("OK: the portal gesture is switchable and never swallows the key press")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
