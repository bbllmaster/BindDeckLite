"""Regression guards for the ``CMD:GET_BOND`` wire format (WS-423).

Why this file exists
--------------------
WS-410 comma-appended the NimBLE bond diagnostics to ``BOND_STATUS``::

    BOND_STATUS:bound,AB12:**:**:**:**,peer_sec=1,nimble_bond=1

The old host parses that line with ``payload.split(",", 1)``, so ``addr``
became ``"AB12:**:**:**:**,peer_sec=1,nimble_bond=1"`` — a corrupted field on
the very line WS-321 defined as ``bound,<masked>``.  The fix keeps
``BOND_STATUS`` byte-identical to the WS-321 format and moves the diagnostics
to a separate ``BOND_DIAG:`` line that old hosts ignore.

What is guarded here
--------------------
  * the host parsers (imported from ``app.py``, not copied) split the address
    at the first comma only, so a legacy line with appended fields can never
    leak them into ``addr``;
  * the firmware's ``CMD:GET_BOND`` branch emits exactly the two documented
    ``BOND_STATUS`` shapes plus one ``BOND_DIAG`` line, and never appends the
    diagnostics to ``BOND_STATUS`` again.

The firmware is C++ for ESP32 and there is no hardware in CI, so the emit side
is a *source contract* test (same approach as ``test_ota_diag.py``): it reads
``src/main.cpp`` and fails on the pre-fix tree at commit ``48ab2ab``.

Run::

    pytest host/test_bond_status.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as host_app

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_CPP = os.path.join(REPO_ROOT, "src", "main.cpp")

MASKED = "AB12:**:**:**:**"


# --- host side: the production parsers ------------------------------------

def test_bond_status_keeps_the_address_clean():
    """The documented format still parses to just the masked address."""
    assert host_app.parse_bond_status("bound," + MASKED) == {
        "bound": True, "addr": MASKED}


def test_bond_status_never_folds_extra_fields_into_addr():
    """A legacy line with appended diagnostics must not corrupt ``addr``.

    This is the exact wire line WS-410 produced: the pre-fix host turned it
    into ``addr = "<masked>,peer_sec=1,nimble_bond=1"``.
    """
    parsed = host_app.parse_bond_status(
        "bound," + MASKED + ",peer_sec=1,nimble_bond=1")
    assert parsed["bound"] is True
    assert parsed["addr"] == MASKED
    assert "peer_sec" not in parsed["addr"]


def test_bond_status_unbound_has_no_address():
    assert host_app.parse_bond_status("unbound") == {
        "bound": False, "addr": ""}


def test_bond_status_unavailable_is_still_bound():
    """``<unavailable>`` means bound-but-address-unknown, not unbound."""
    assert host_app.parse_bond_status("bound,<unavailable>") == {
        "bound": True, "addr": "<unavailable>"}


def test_bond_diag_parses_both_fields():
    assert host_app.parse_bond_diag("peer_sec=1,nimble_bond=1") == {
        "peer_sec": 1, "nimble_bond": 1}


def test_bond_diag_ignores_unknown_and_malformed_fields():
    """Forward compatibility: an unknown key or a non-int value is dropped."""
    assert host_app.parse_bond_diag("peer_sec=x,nimble_bond=0,extra=7") == {
        "nimble_bond": 0}
    assert host_app.parse_bond_diag("") == {}


# --- firmware side: the emit contract ------------------------------------

def _get_bond_block():
    """Return the source of the ``CMD:GET_BOND`` branch of ``main.cpp``."""
    with open(MAIN_CPP, "r", encoding="utf-8") as fh:
        source = fh.read()
    start = source.index('data.startsWith("CMD:GET_BOND")')
    # The branch ends at the next `} else if (` of the same dispatch chain.
    end = source.index("} else if (", start)
    return source[start:end]


def _literals(block):
    """Every double-quoted literal in *block* as ``(offset, text)`` pairs."""
    out = []
    i = 0
    while True:
        start = block.find('"', i)
        if start < 0:
            return out
        j = start + 1
        while j < len(block):
            if block[j] == "\\":
                j += 2
                continue
            if block[j] == '"':
                break
            j += 1
        out.append((start, block[start + 1:j]))
        i = j + 1


def test_get_bond_emits_only_the_two_documented_status_shapes():
    """BOND_STATUS must not grow fields again — that is what broke ``addr``."""
    block = _get_bond_block()
    lits = _literals(block)
    status = {lit for _pos, lit in lits if lit.startswith("BOND_STATUS")}
    assert status == {
        "BOND_STATUS:bound,",
        "BOND_STATUS:bound,<unavailable>",
        "BOND_STATUS:unbound",
    }, f"BOND_STATUS literals changed: {sorted(status)}"


def test_bond_diag_fields_never_appear_outside_a_bond_diag_line():
    """Appending peer_sec/nimble_bond anywhere else is the pre-fix bug.

    The check walks each diagnostic literal back to the ``sendToHost(`` call
    that emits it: that call must be the BOND_DIAG one.
    """
    block = _get_bond_block()
    for pos, lit in _literals(block):
        if "peer_sec" not in lit and "nimble_bond" not in lit:
            continue
        stmt_start = block.rfind("sendToHost(", 0, pos)
        assert stmt_start >= 0, f"{lit!r} is not emitted by sendToHost()"
        stmt = block[stmt_start:pos + len(lit)]
        assert "BOND_DIAG:" in stmt, \
            f"diagnostic literal {lit!r} leaked into: {stmt.strip()!r}"


def test_get_bond_emits_a_separate_bond_diag_line():
    """The WS-410 diagnostics must travel on their own BOND_DIAG line."""
    lits = _literals(_get_bond_block())
    diag = [lit for _pos, lit in lits if lit.startswith("BOND_DIAG:")]
    assert diag == ["BOND_DIAG:peer_sec="], diag
    # ... after BOND_STATUS, so the status line is complete first.
    status_pos = min(pos for pos, lit in lits if lit.startswith("BOND_STATUS"))
    diag_pos = min(pos for pos, lit in lits if lit.startswith("BOND_DIAG"))
    assert status_pos < diag_pos


def test_bond_diag_literal_carries_the_nimble_bond_flag():
    block = _get_bond_block()
    assert '",nimble_bond="' in block
