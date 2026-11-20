"""Shared fixtures for backend/tests/broker/.

Isolates the on-disk login cool-off files `connections.py` persists for
`KiteConnection` and `DhanConnection` so no test in this directory can ever
read or write the real `/tmp/ramboq_kite_login_cooloff.json` /
`/tmp/ramboq_dhan_login_cooloff.json` — which are shared, host-wide paths
also used by the real prod/dev conn-service process. Without this, a test
that exercises a failed login (e.g. test_kite_login_cooloff.py) would
persist a real cool-off entry for whatever account name it used, and any
later test (in this run or a future one, same host) constructing a
connection for that same account name would silently inherit the block.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_login_cooloff_paths(tmp_path, monkeypatch):
    """Redirect both brokers' login cool-off file paths to a per-test
    tmp_path file. Autouse so it applies to every test in this directory,
    not only ones that know to ask for it."""
    import backend.brokers.connections as connections

    monkeypatch.setattr(
        connections, "_KITE_LOGIN_COOLOFF_PATH",
        str(tmp_path / "kite_login_cooloff.json"),
    )
    monkeypatch.setattr(
        connections, "_DHAN_LOGIN_COOLOFF_PATH",
        str(tmp_path / "dhan_login_cooloff.json"),
    )
