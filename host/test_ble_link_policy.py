"""WS-429: executable tests for the BLE link policy.

Why this file exists
--------------------
WS-407 shipped a diagnosis for "the deck cannot be reached any more", but on
real hardware two follow-up defects showed up:

  1. ``CASE=`` reported ``none`` ("brand-new device, no problem") on a deck
     whose IRK was gone, i.e. exactly the case where the PC still holds a
     pairing for an older identity of the deck.
  2. A peer that connects and is torn down before encryption was retried every
     200-500 ms, forever, with nothing but ``reason=0x0213`` in the log - no
     conclusion and no way for the user to act on it.

Both decisions are pure functions over plain data, so they live in
``src/ble_link_policy.h``. The firmware includes that header; this test
compiles the **same** header with g++ (through ``host/ble_policy_cli.cpp``,
which contains no policy logic at all) and drives it. The assertions therefore
run against production code, not against a copy of it.

The test fails on the pre-WS-429 tree (the header and the CLI do not exist,
so the compile step fails) and passes on this one.

Run:  pytest host/test_ble_link_policy.py -v
"""

import os
import re
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(REPO_ROOT, "src")
POLICY_HEADER = os.path.join(SRC_DIR, "ble_link_policy.h")
CLI_SOURCE = os.path.join(REPO_ROOT, "host", "ble_policy_cli.cpp")
FIRMWARE = os.path.join(SRC_DIR, "nimble_hid.cpp")
MAIN_CPP = os.path.join(SRC_DIR, "main.cpp")

PEER = "a8:7e:ea:33:0a:49"      # the peer from the field log
PEER2 = "11:22:33:44:55:66"
THRESHOLD = 3


def _compiler():
    for name in ("g++", "c++", "clang++"):
        path = shutil.which(name)
        if path:
            return path
    return None


@pytest.fixture(scope="module")
def policy(tmp_path_factory):
    """Compile the production policy header into a tiny CLI."""
    if not os.path.exists(POLICY_HEADER):
        pytest.fail("missing production header: %s" % POLICY_HEADER)
    if not os.path.exists(CLI_SOURCE):
        pytest.fail("missing policy CLI: %s" % CLI_SOURCE)
    cxx = _compiler()
    if not cxx:
        pytest.skip("no C++ compiler (g++/c++/clang++) available")

    out = str(tmp_path_factory.mktemp("blepol") / "ble_policy_cli")
    cmd = [
        cxx, "-std=c++17", "-Wall", "-Wextra", "-O0",
        "-I", SRC_DIR,
        CLI_SOURCE,
        "-o", out,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, (
        "compiling the production policy failed:\n%s\n%s" % (cmd, proc.stderr)
    )
    return out


def _parse_line(line):
    """Split a `key=value key=value` line; values may contain spaces."""
    fields = {}
    matches = list(re.finditer(r"(?:^|\s)([a-z_]+)=", line))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        fields[match.group(1)] = line[match.end():end]
    return fields


def _run(binary, *commands):
    """Run the policy CLI with the given command lines, return parsed output."""
    stdin = "\n".join(commands) + "\n"
    proc = subprocess.run(
        [binary], input=stdin, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, (
        "policy CLI failed (rc=%d): %s%s" % (proc.returncode, proc.stdout, proc.stderr)
    )
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln]
    assert len(lines) == len(commands), (
        "expected %d output lines, got %d: %r" % (len(commands), len(lines), lines)
    )
    return [_parse_line(line) for line in lines]


def _classify(binary, irk, prev, match, bound, bonds, migrated, prior, rebooted=0):
    return _run(binary, "classify %d %d %d %d %d %d %d %d" % (
        irk, prev, match, bound, bonds, migrated, prior, rebooted))[0]


# ---------------------------------------------------------------------------
# Defect 1 - identity classification
# ---------------------------------------------------------------------------

def test_fresh_device_is_reported_as_none(policy):
    """A deck with no NVS history and no IRK is genuinely new -> "none"."""
    out = _classify(policy, irk=0, prev=0, match=0, bound=0, bonds=0,
                    migrated=0, prior=0, rebooted=0)
    assert out["text"] == "none"
    assert out["code"] == "none"


def test_identity_reset_when_schema_was_migrated(policy):
    """The field case: irk gone, NVS still holds a v1 config -> identity reset."""
    out = _classify(policy, irk=0, prev=0, match=0, bound=0, bonds=0,
                    migrated=1, prior=1)
    assert out["code"] == "B"
    assert out["text"] == "B(identity reset: irk absent, nvs schema migrated)"


def test_identity_reset_when_nvs_has_prior_config(policy):
    """IRK gone but the NVS still carries an older configuration."""
    out = _classify(policy, irk=0, prev=0, match=0, bound=0, bonds=0,
                    migrated=0, prior=1)
    assert out["code"] == "B"
    assert out["text"] == "B(identity reset: irk absent, nvs has prior config)"


def test_identity_reset_when_the_deck_rebooted_without_an_irk(policy):
    """A first boot stores the IRK, so a later boot without one lost it.

    This is the field log: "bootCount=2" together with "irk NOT_FOUND".
    """
    out = _classify(policy, irk=0, prev=0, match=0, bound=0, bonds=0,
                    migrated=0, prior=0, rebooted=1)
    assert out["code"] == "B"
    assert out["text"] == \
        "B(identity reset: irk absent, deck rebooted without a saved identity)"


def test_irk_change_is_still_reported_as_b(policy):
    """The pre-existing case B: the persisted IRK differs from last boot."""
    out = _classify(policy, irk=1, prev=1, match=0, bound=0, bonds=0,
                    migrated=0, prior=1)
    assert out["code"] == "B"
    assert "irk changed" in out["text"]


def test_bound_without_bond_is_case_a(policy):
    out = _classify(policy, irk=1, prev=1, match=1, bound=1, bonds=0,
                    migrated=0, prior=1)
    assert out["code"] == "A"


def test_bound_with_bond_is_case_c(policy):
    out = _classify(policy, irk=1, prev=1, match=1, bound=1, bonds=1,
                    migrated=0, prior=1)
    assert out["code"] == "C"


# ---------------------------------------------------------------------------
# Defect 2 - pre-encryption teardown guard
# ---------------------------------------------------------------------------

def test_encrypted_teardown_is_not_counted(policy):
    """enc=1 means the link worked - it must never count as a failure."""
    out = _run(policy, "teardown %s 1 1 300 %d" % (PEER, THRESHOLD),
               "strikes %s" % PEER)[0]
    assert out["action"] == "ignored"
    assert out["strikes"] == "0"


def test_normal_disconnect_is_not_counted(policy):
    """A connection that lived longer than ~2 s was in use."""
    out = _run(policy, "teardown %s 0 0 5000 %d" % (PEER, THRESHOLD),
               "strikes %s" % PEER)[0]
    assert out["action"] == "ignored"
    assert out["strikes"] == "0"


def test_bonded_but_unencrypted_link_is_counted(policy):
    """A bond without encryption is still a handshake that never completed.

    The issue asks for "enc==0 && bonded==0"; a peer that holds a bond but
    never encrypted is the same broken pairing, and counting it keeps the
    conclusion reachable instead of looping forever.
    """
    out = _run(policy, "teardown %s 0 1 300 %d" % (PEER, THRESHOLD))[0]
    assert out["action"] == "counted"
    assert out["strikes"] == "1"


def test_threshold_reports_exactly_once(policy):
    """Three fast pre-encryption teardowns -> one conclusion, then silence."""
    out = _run(policy,
               "teardown %s 0 0 300 %d" % (PEER, THRESHOLD),
               "teardown %s 0 0 300 %d" % (PEER, THRESHOLD),
               "teardown %s 0 0 300 %d" % (PEER, THRESHOLD),
               "teardown %s 0 0 300 %d" % (PEER, THRESHOLD),
               "teardown %s 0 0 300 %d" % (PEER, THRESHOLD))
    assert [o["action"] for o in out] == [
        "counted", "counted", "reported", "counted", "counted",
    ]
    assert out[-1]["strikes"] == "5"


def test_strikes_are_tracked_per_peer(policy):
    """A second peer must not inherit the first peer's strikes."""
    out = _run(policy,
               "teardown %s 0 0 300 %d" % (PEER, THRESHOLD),
               "teardown %s 0 0 300 %d" % (PEER, THRESHOLD),
               "teardown %s 0 0 300 %d" % (PEER2, THRESHOLD),
               "strikes %s" % PEER,
               "strikes %s" % PEER2)
    assert out[2]["action"] == "counted"
    assert out[2]["strikes"] == "1"
    assert out[3]["strikes"] == "2"
    assert out[4]["strikes"] == "1"


def test_successful_peer_clears_its_strikes(policy):
    """A peer that paired / subscribed is not a suspect any more."""
    out = _run(policy,
               "teardown %s 0 0 300 %d" % (PEER, THRESHOLD),
               "teardown %s 0 0 300 %d" % (PEER, THRESHOLD),
               "success %s" % PEER,
               "strikes %s" % PEER)
    assert out[1]["strikes"] == "2"
    assert out[2]["cleared"] == "1"
    assert out[3]["strikes"] == "0"


def test_backoff_ladder(policy):
    """2 s / 5 s / 10 s / 30 s, capped at 30 s."""
    out = _run(policy, "backoff 0", "backoff 1", "backoff 2",
               "backoff 3", "backoff 4", "backoff 9")
    assert [o["ms"] for o in out] == ["0", "2000", "5000", "10000", "30000", "30000"]


def test_backoff_follows_the_strike_count(policy):
    """The backoff reported with a teardown is the ladder entry for that count."""
    out = _run(policy,
               "teardown %s 0 0 300 %d" % (PEER, THRESHOLD),
               "teardown %s 0 0 300 %d" % (PEER, THRESHOLD))
    assert out[0]["backoff"] == "2000"
    assert out[1]["backoff"] == "5000"


def test_stale_key_message_names_the_peer_and_the_device(policy):
    out = _run(policy, "message %s BindDeckLite-95B4 3" % PEER)[0]
    msg = out["message"]
    assert msg.startswith("HOST_STALE_KEY: peer %s keeps terminating before encryption (n=3)." % PEER)
    assert "delete BindDeckLite-95B4 in Windows Bluetooth settings and pair again" in msg


# ---------------------------------------------------------------------------
# Defect 3 - while-connected advertising gate
# ---------------------------------------------------------------------------

def test_unencrypted_young_link_may_not_re_advertise(policy):
    """The reconnect-loop pattern: an unencrypted link that is still young."""
    out = _run(policy, "adv 0 0 300")[0]
    assert out["allow"] == "0"


def test_encrypted_link_may_re_advertise(policy):
    """WS-271: a paired host keeps the deck discoverable for the config app."""
    out = _run(policy, "adv 1 0 300")[0]
    assert out["allow"] == "1"


def test_hid_subscription_may_re_advertise(policy):
    """WS-271: a HID subscriber means the deck is in use."""
    out = _run(policy, "adv 0 1 300")[0]
    assert out["allow"] == "1"


def test_old_unencrypted_link_may_re_advertise(policy):
    """A link that survived the grace window is not the rapid-teardown case."""
    out = _run(policy, "adv 0 0 4000")[0]
    assert out["allow"] == "1"


# ---------------------------------------------------------------------------
# Wiring guards - the firmware must actually use the production policy
# ---------------------------------------------------------------------------

def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def test_firmware_includes_and_uses_the_policy(policy):
    """The tested header has to be the one the firmware runs on."""
    src = _read(FIRMWARE)
    assert '#include "ble_link_policy.h"' in src, \
        "src/nimble_hid.cpp must include the production policy header"
    for fn in ("classifyIdentity", "staleNoteTeardown", "staleBackoffMs",
               "mayAdvertiseWhileConnected", "formatStaleMessage"):
        assert fn in src, "src/nimble_hid.cpp must call blepol::%s" % fn


def test_firmware_reports_the_stale_key_conclusion(policy):
    """The conclusion the user acts on has to reach the log."""
    src = _read(FIRMWARE)
    assert "HOST_STALE_KEY" in src or "formatStaleMessage" in src
    assert "stale-peer backoff" in src, \
        "the firmware must back off instead of re-advertising every 200 ms"


def test_main_cpp_captures_the_pre_boot_nvs_state(policy):
    """'brand new' and 'identity lost' are only distinguishable if the NVS
    state is captured BEFORE this boot writes anything."""
    src = _read(MAIN_CPP)
    assert 'getInt("cfgVer", -1)' in src, \
        "the cfgVer key must be probed with -1 so 'absent' and 'v1' differ"
    assert "g_cfgSchemaMigrated" in src
    assert "g_cfgHadPriorConfig" in src
    assert "g_deckRebooted" in src
    assert "static BleBootFacts bootFacts()" in src
    assert re.search(r"bleKeyboard\.begin\(bootFacts\(\)\)", src)


# ---------------------------------------------------------------------------
# WS-419b - peer admission and the config-channel gate
# ---------------------------------------------------------------------------
#
# Field case: a phone paired with a deck that was already bound to a PC. The
# log showed `reject non-bound host` at HID-subscribe time only, yet the phone
# said "paired" and kept the link. Two defects behind it:
#
#   1. the gate ran in onSubscribe(), i.e. after SMP - a central that pairs and
#      never subscribes was never rejected at all;
#   2. rejecting the subscription left the link and the bond in place, and the
#      CFG/CMD channel only asked for WRITE_ENC, so the phone could rewrite the
#      deck's configuration.

def _admit(binary, bound, identity_null, identity_matches):
    return _run(binary, "admit %d %d %d" % (bound, identity_null,
                                            identity_matches))[0]


def test_bound_host_is_admitted(policy):
    out = _admit(policy, 1, 0, 1)
    assert out["verdict"] == "allow"
    assert out["disconnect"] == "0"


def test_stranger_that_just_paired_is_rejected(policy):
    """A resolved identity that is not the bound host must lose the link."""
    out = _admit(policy, 1, 0, 0)
    assert out["verdict"] == "reject"
    assert out["disconnect"] == "1", \
        "the stranger's link has to be dropped, not just its HID subscription"


def test_unresolved_identity_is_not_a_stranger(policy):
    """CASE=C is the bound host whose RPA did not resolve - never disconnect it."""
    out = _admit(policy, 1, 1, 0)
    assert out["verdict"] == "undecided"
    assert out["disconnect"] == "0"


def test_unbound_deck_admits_every_central(policy):
    """Before a host is bound the first central takes the slot."""
    assert _admit(policy, 0, 0, 0)["verdict"] == "allow"
    assert _admit(policy, 0, 1, 0)["verdict"] == "allow"


def _cfg(binary, bound, identity_null, identity_matches):
    return _run(binary, "cfg %d %d %d" % (bound, identity_null,
                                          identity_matches))[0]


def test_cfg_allows_the_bound_host(policy):
    assert _cfg(policy, 1, 0, 1)["allow"] == "1"


def test_cfg_denies_the_stranger_that_paired(policy):
    """This is the security hole: the phone could write CFG:/CMD: lines."""
    assert _cfg(policy, 1, 0, 0)["allow"] == "0"


def test_cfg_denies_an_unresolved_identity_on_a_bound_deck(policy):
    assert _cfg(policy, 1, 1, 0)["allow"] == "0"


def test_cfg_allows_an_unbound_deck(policy):
    assert _cfg(policy, 0, 0, 0)["allow"] == "1"


def test_firmware_wires_admission_and_the_config_gate(policy):
    """The policy is only worth testing if the firmware actually calls it."""
    src = _read(FIRMWARE)
    assert "admitPeer" in src, "nimble_hid.cpp must call blepol::admitPeer"
    assert "bleConfig.peerAllowed" in src, \
        "nimble_hid.cpp must install the CFG identity gate"
    assert "disconnect(connInfo.getConnHandle()" in src, \
        "a rejected peer must be disconnected, not just refused"
    cfg = _read(os.path.join(SRC_DIR, "ble_config.cpp"))
    assert "peerAllowed" in cfg, "the CFG write path must consult the gate"


# ---------------------------------------------------------------------------
# WS-419c - boot-time bind window
# ---------------------------------------------------------------------------
#
# Holding key 1 while powering on opens a 2 minute window where a NEW host may
# claim the binding. Outside it the WS-419b gate must stay exactly as strict,
# and an unresolved identity must never count as a takeover (that is CASE=C).

def _claim(binary, bound, window, identity_null, identity_matches):
    return _run(binary, "claim %d %d %d %d" % (bound, window, identity_null,
                                               identity_matches))[0]


def test_no_window_means_no_takeover(policy):
    """Without the boot gesture a stranger is still just a stranger."""
    assert _claim(policy, 1, 0, 0, 0)["claim"] == "none"


def test_window_lets_a_new_host_take_over(policy):
    assert _claim(policy, 1, 1, 0, 0)["claim"] == "takeover"


def test_window_with_the_current_host_only_closes_it(policy):
    assert _claim(policy, 1, 1, 0, 1)["claim"] == "keep"


def test_window_never_hands_the_deck_to_an_unresolved_identity(policy):
    assert _claim(policy, 1, 1, 1, 0)["claim"] == "none"


def test_unbound_deck_uses_the_first_bind_path_not_the_window(policy):
    assert _claim(policy, 0, 1, 0, 0)["claim"] == "none"
    assert _claim(policy, 0, 0, 0, 0)["claim"] == "none"


def test_firmware_opens_and_expires_the_window(policy):
    src = _read(FIRMWARE)
    assert "classifyBindClaim" in src, "firmware must use the tested classifier"
    assert "openBindWindow" in src and "bind window expired" in src, \
        "the window must be opened and expired explicitly"
    main = _read(MAIN_CPP)
    assert "openBindWindow(120000)" in main, \
        "the boot gesture must open a 2 minute window"
