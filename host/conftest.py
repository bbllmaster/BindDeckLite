"""Session-wide isolation for module-level patches on ``app``.

Several test modules replace helpers on the ``app`` module with a plain
attribute assignment instead of ``monkeypatch`` — e.g. ``app.save_config =
lambda: None`` so a test never writes the real ``host/config.json``.  Plain
assignment is not undone when the test ends, so the stub leaked into every
later module in the same pytest session.

That is exactly how WS-512's ``test_device_trust.py`` failed in CI: it is last
in the ``build-host.yml`` list, and by then ``save_config`` had been a no-op
since ``test_pin_validation.py``, so its "the flag really was persisted"
assertions found no ``config.json`` on disk (WS-516).  Restoring the real
function after every test keeps those modules free to stub without dragging
the rest of the session into their stub.
"""

import pytest

import app


@pytest.fixture(autouse=True)
def _restore_save_config():
    original = app.save_config
    try:
        yield
    finally:
        app.save_config = original
