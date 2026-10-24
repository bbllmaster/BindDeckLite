"""Unit tests for the encoder rotation actions (WS-389 / WS-390).

The WS-390 Stage-2 review found two "fake success" holes in the rotation
action feature:

* the "测试动作" button was clickable in the four non-上位机动作 modes
  (the status poll re-enabled it), and the backend answered those clicks
  with ``{"ok": true}`` although nothing ran;
* in 上位机动作 mode the button was swallowed by the 800 ms one-shot
  cooldown — the rotation had just fired the same direction — and the
  backend again answered "success" although nothing ran.

Both holes are covered here.  The tests drive the production functions
(``app._on_line`` / ``app.run_action`` / the Flask test client) instead of
re-implementing them, so a change to ``host/app.py`` that reopens either
hole fails this file.

The tests must stay platform-independent and side-effect free: CI runs them
on Windows, so no test may pop a browser, send a media key or spawn a shell
command.  Actions that would touch the machine are stubbed through
``app.run_action``; everything above that (the claim table, the cooldown,
the event log) is the production code.

Run::

    pytest host/test_encoder_actions.py -v
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app


# --- helpers ---------------------------------------------------------------

def _reset_state():
    """Clear the global tables so tests do not leak into each other."""
    with app._lock:
        app._encoder_action_running.clear()
        app._encoder_last_trigger.clear()
    app._events.clear()


def _device(dev_id="t9", mode=4, cw=None, ccw=None):
    """Register a device with the given encoder mode / rotation actions."""
    _reset_state()
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    cfg = app.new_device_cfg("ESP32", dev_id)
    cfg["encoder"]["mode"] = mode
    cfg["encoder"]["actions"]["CW"] = cw or {"type": "none", "value": "",
                                              "macro": []}
    cfg["encoder"]["actions"]["CCW"] = ccw or {"type": "none", "value": "",
                                               "macro": []}
    app._config["devices"][dev_id] = cfg
    return cfg


def _action_events():
    return [e for e in app._events if e["kind"] == "action"]


def _wait_for_action(count=1, timeout=5.0):
    """Wait until ``count`` action rows were logged (actions run in threads)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(_action_events()) >= count:
            return True
        time.sleep(0.02)
    return False


class _StubbedAction:
    """Replace ``app.run_action`` with a harmless, counting stub.

    The stub keeps the production contract (``ok`` / ``error`` /
    ``action_type`` / ``target``) so the claim table, the cooldown and the
    event log stay the real ones.
    """

    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay

    def __enter__(self):
        self._original = app.run_action

        def _run(action, dev_id=""):
            self.calls.append((dev_id, dict(action)))
            if self.delay:
                time.sleep(self.delay)
            return {"ok": True, "error": "",
                    "action_type": action.get("type", "none"),
                    "target": action.get("value", "")}

        app.run_action = _run
        return self

    def __exit__(self, *exc):
        app.run_action = self._original
        return False


# --- WS-390 review #1: the button must not fake a success ------------------

def test_test_endpoint_rejects_non_mode4():
    """Mode != 4 must be refused, not answered with a fake success.

    Review #1: the greyed-out card's button was clickable and the backend
    replied ``{"ok": true}`` while no action ran at all.
    """
    _device(mode=0, cw={"type": "command", "value": "true", "macro": []})
    client = app.app.test_client()

    res = client.post("/api/encoder/action/test/t9/CW")
    body = res.get_json()

    assert body["ok"] is False, (
        "mode != 4 must not report success — nothing ran (WS-390 review #1)")
    assert body["error"], "the refusal must explain itself"
    assert not _action_events(), "no action may run outside mode 4"


def test_test_endpoint_rejects_non_mode4_for_ccw():
    """The refusal covers both directions, not just CW."""
    _device(mode=3, ccw={"type": "command", "value": "true", "macro": []})
    client = app.app.test_client()

    body = client.post("/api/encoder/action/test/t9/CCW").get_json()

    assert body["ok"] is False
    assert body["error"]
    assert not _action_events()


def test_test_endpoint_rejects_unknown_direction():
    """An unknown direction is a bad request, not a silent success."""
    _device(mode=4)
    client = app.app.test_client()

    res = client.post("/api/encoder/action/test/t9/SIDEWAYS")

    assert res.status_code == 400
    assert res.get_json()["ok"] is False


# --- WS-390 review #2: the button must not be swallowed by the cooldown -----

def test_test_endpoint_ignores_rotation_cooldown():
    """An explicit test must run even right after a rotation fired.

    Review #2: the rotation armed the 800 ms one-shot cooldown and the
    test click was dropped — yet the backend still answered "success".
    """
    _device(mode=4, cw={"type": "command", "value": "true", "macro": []})
    client = app.app.test_client()

    with _StubbedAction() as stub:
        # A rotation fires the same direction and arms the cooldown.
        app._on_line("t9", "ENC:t9:CW")
        assert _wait_for_action(1), "the rotation must run the action"

        # The explicit test must run too, despite the cooldown.
        body = client.post("/api/encoder/action/test/t9/CW").get_json()

        assert body["ok"] is True, (
            "the test must actually run — the 800 ms rotation cooldown must "
            "not swallow it (WS-390 review #2)")
        assert _wait_for_action(2), (
            "the test must produce a second action row — a silent cooldown "
            "drop reported as success is the bug under test")
        assert len(stub.calls) == 2, "both the rotation and the test ran"


def test_test_endpoint_does_not_arm_the_rotation_cooldown():
    """A test must not push the rotation cooldown window forward.

    The cooldown models rotation detents, not clicks: a manual test may
    run at any time and must not silence the rotation that follows it.
    """
    _device(mode=4, cw={"type": "command", "value": "true", "macro": []})
    client = app.app.test_client()

    with _StubbedAction() as stub:
        body = client.post("/api/encoder/action/test/t9/CW").get_json()
        assert body["ok"] is True
        assert _wait_for_action(1)

        # The rotation right after the test must still fire.
        app._on_line("t9", "ENC:t9:CW")
        assert _wait_for_action(2), (
            "the rotation after a manual test must still run — the test must "
            "not arm the rotation cooldown")
        assert len(stub.calls) == 2


def test_rotation_cooldown_still_applies():
    """The 800 ms one-shot cooldown must survive for real rotations."""
    _device(mode=4, cw={"type": "command", "value": "true", "macro": []})

    with _StubbedAction() as stub:
        app._on_line("t9", "ENC:t9:CW")
        assert _wait_for_action(1), "the first rotation must run the action"

        # A second rotation inside the 800 ms window is throttled.
        app._on_line("t9", "ENC:t9:CW")
        time.sleep(0.4)
        assert len(stub.calls) == 1, (
            "the 800 ms one-shot cooldown must still throttle rotations")


def test_rotation_media_action_runs_per_detent():
    """Non-one-shot actions (media) run on every detent — no cooldown."""
    _device(mode=4, cw={"type": "media", "value": "volume up", "macro": []})

    with _StubbedAction() as stub:
        for _ in range(3):
            app._on_line("t9", "ENC:t9:CW")
            time.sleep(0.05)

        assert _wait_for_action(3), (
            "media actions run per detent — the cooldown is for one-shot "
            "actions only")
        assert len(stub.calls) == 3


def test_test_endpoint_is_refused_while_the_direction_is_running():
    """The "still running" preemption survives for the test endpoint too."""
    _device(mode=4, cw={"type": "command", "value": "true", "macro": []})
    client = app.app.test_client()

    with _StubbedAction(delay=1.0) as stub:
        app._on_line("t9", "ENC:t9:CW")
        deadline = time.monotonic() + 5.0
        while not stub.calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stub.calls, "the rotation must start the action"

        body = client.post("/api/encoder/action/test/t9/CW").get_json()

        assert body["ok"] is False, (
            "a test while the direction is busy must be refused, not "
            "reported as success")
        assert body.get("running") is True
        assert body.get("error")


# --- the rest of the WS-389 acceptance criteria ---------------------------

def test_rotation_outside_mode4_runs_nothing():
    """Modes 0-3 keep the old behaviour: rotation only reaches the device."""
    _device(mode=0, cw={"type": "command", "value": "true", "macro": []})

    with _StubbedAction() as stub:
        app._on_line("t9", "ENC:t9:CW")
        time.sleep(0.4)

        assert not stub.calls, (
            "outside mode 4 the rotation must not run any host action")
        assert not _action_events()


def test_rotation_without_action_runs_nothing():
    """An unconfigured direction only sends F23/F24 — no action rows."""
    _device(mode=4)

    with _StubbedAction() as stub:
        app._on_line("t9", "ENC:t9:CW")
        time.sleep(0.4)

        assert not stub.calls, (
            "an unconfigured direction must not run or log any action")
        assert not _action_events()
        enc_events = [e for e in app._events if e["kind"] == "enc"]
        assert len(enc_events) == 1, "the rotation itself is still logged"


def test_rotation_while_running_is_skipped():
    """A second detent during a running action is dropped with a log row."""
    _device(mode=4, cw={"type": "macro", "value": "", "macro": []})

    with _StubbedAction(delay=1.0) as stub:
        app._on_line("t9", "ENC:t9:CW")
        deadline = time.monotonic() + 5.0
        while not stub.calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stub.calls, "the first rotation must start the action"

        app._on_line("t9", "ENC:t9:CW")
        time.sleep(0.4)

        skips = [e for e in app._events if "动作跳过" in e.get("text", "")]
        assert skips, "a detent during a running action must log 动作跳过"
        assert "编码器顺时针" in skips[0]["text"]


def test_two_devices_keep_separate_actions():
    """Each deck runs its own rotation action — no cross-talk."""
    _reset_state()
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    for dev_id, value in (("devA", "alpha"), ("devB", "beta")):
        cfg = app.new_device_cfg("ESP32", dev_id)
        cfg["encoder"]["mode"] = 4
        cfg["encoder"]["actions"]["CW"] = {"type": "command", "value": value,
                                           "macro": []}
        app._config["devices"][dev_id] = cfg

    with _StubbedAction() as stub:
        app._on_line("devA", "ENC:devA:CW")
        app._on_line("devB", "ENC:devB:CW")

        assert _wait_for_action(2)
        assert len(stub.calls) == 2
        values = {value for _, action in stub.calls
                  for value in [action.get("value")]}
        assert values == {"alpha", "beta"}, (
            "each deck must run its own action")


def test_legacy_config_without_actions_field():
    """An old config.json (no encoder.actions) must load without errors."""
    _reset_state()
    app._config = {"devices": {}, "settings": {"actions_enabled": True}}
    cfg = app.new_device_cfg("ESP32", "legacy")
    del cfg["encoder"]["actions"]          # as written by an older build
    app._config["devices"]["legacy"] = cfg

    client = app.app.test_client()
    body = client.get("/api/config/legacy").get_json()

    assert "actions" not in body["encoder"], (
        "the legacy config must be returned unchanged")
    # The rotation path treats the missing field as "unconfigured".
    with _StubbedAction() as stub:
        app._on_line("legacy", "ENC:legacy:CW")
        time.sleep(0.4)
        assert not stub.calls, (
            "a legacy config without encoder.actions must behave as 未配置")
        assert not _action_events()


def test_encoder_actions_are_saved_in_config():
    """The rotation actions live in the device config next to the mode."""
    _device(mode=4, cw={"type": "macro", "value": "",
                         "macro": [{"type": "key", "value": "ctrl+win+right"}]})
    client = app.app.test_client()

    body = client.get("/api/config/t9").get_json()

    assert body["encoder"]["mode"] == 4
    assert body["encoder"]["actions"]["CW"]["type"] == "macro"
    assert body["encoder"]["actions"]["CW"]["macro"][0]["value"] == \
        "ctrl+win+right"
