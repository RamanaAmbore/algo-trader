"""
Unit tests for backend/api/helpers/price_resolver.py — the unified
per-symbol animation model resolver.

Five quality dimensions (per house style):
  SSOT       — the only place the {live, snapshot_settled, snapshot_unsettled}
               branch matrix lives. Callers dispatch here, not inline.
  Perf       — pure function; no I/O. O(1) per call.
  Stale      — asserts that no branch returns the legacy "snapshot" value
               (must be one of the three post-refactor labels).
  Reusable   — same resolver serves positions, holdings, movers.
  Correctness (branch coverage) — six scenarios below cover the full
               state-space: open×closed × settled×unsettled × has-snap
               ×no-snap.
"""

from __future__ import annotations

import pytest

from backend.api.helpers.price_resolver import resolve_current_price


# ---------------------------------------------------------------------------
# 1. Exchange OPEN — always returns the live LTP and animating=True.
# ---------------------------------------------------------------------------

def test_open_exchange_returns_live_and_animating():
    price, source, animating = resolve_current_price(
        exchange_open=True,
        live_ltp=100.0,
        snapshot_close=None,
        snapshot_last_ltp=None,
        settled=False,
    )
    assert price == 100.0
    assert source == "live"
    assert animating is True


def test_open_exchange_ignores_snapshot_values():
    """When open, snapshot inputs are never consulted."""
    price, source, animating = resolve_current_price(
        exchange_open=True,
        live_ltp=100.0,
        snapshot_close=999.0,           # ignored
        snapshot_last_ltp=888.0,        # ignored
        settled=True,                    # ignored
    )
    assert price == 100.0
    assert source == "live"
    assert animating is True


def test_open_exchange_live_none_stays_none():
    """Broker outage during open hours → resolver preserves None. Caller
    is responsible for LKG substitution before invoking."""
    price, source, animating = resolve_current_price(
        exchange_open=True,
        live_ltp=None,
        snapshot_close=100.0,
        snapshot_last_ltp=99.0,
        settled=True,
    )
    assert price is None
    assert source == "live"
    assert animating is True


# ---------------------------------------------------------------------------
# 2. Exchange CLOSED + settled + snapshot_close available — settled path.
# ---------------------------------------------------------------------------

def test_closed_settled_returns_close_price():
    price, source, animating = resolve_current_price(
        exchange_open=False,
        live_ltp=None,
        snapshot_close=105.5,
        snapshot_last_ltp=99.0,       # ignored — settled wins
        settled=True,
    )
    assert price == 105.5
    assert source == "snapshot_settled"
    assert animating is False


# ---------------------------------------------------------------------------
# 3. Exchange CLOSED + settled but close_price missing — falls to unsettled.
# ---------------------------------------------------------------------------

def test_closed_settled_without_close_falls_to_last_ltp():
    """settled=True but snapshot_close=None → still unsettled branch."""
    price, source, animating = resolve_current_price(
        exchange_open=False,
        live_ltp=None,
        snapshot_close=None,
        snapshot_last_ltp=101.5,
        settled=True,
    )
    assert price == 101.5
    assert source == "snapshot_unsettled"
    assert animating is False


# ---------------------------------------------------------------------------
# 4. Exchange CLOSED + unsettled + last-live LTP present — pre-settle path.
# ---------------------------------------------------------------------------

def test_closed_unsettled_returns_last_ltp():
    price, source, animating = resolve_current_price(
        exchange_open=False,
        live_ltp=None,
        snapshot_close=None,
        snapshot_last_ltp=98.75,
        settled=False,
    )
    assert price == 98.75
    assert source == "snapshot_unsettled"
    assert animating is False


# ---------------------------------------------------------------------------
# 5. Exchange CLOSED + no snapshot values at all — degenerate/first-deploy.
# ---------------------------------------------------------------------------

def test_closed_no_snapshot_returns_none():
    price, source, animating = resolve_current_price(
        exchange_open=False,
        live_ltp=None,
        snapshot_close=None,
        snapshot_last_ltp=None,
        settled=False,
    )
    assert price is None
    assert source == "snapshot_unsettled"
    assert animating is False


# ---------------------------------------------------------------------------
# 6. Exchange CLOSED + unsettled + close_price ignored under unsettled flag.
# ---------------------------------------------------------------------------

def test_closed_unsettled_ignores_close_price():
    """settled=False → close_price is not consulted even when present."""
    price, source, animating = resolve_current_price(
        exchange_open=False,
        live_ltp=None,
        snapshot_close=200.0,           # present but not settled → skip
        snapshot_last_ltp=195.0,
        settled=False,
    )
    assert price == 195.0
    assert source == "snapshot_unsettled"
    assert animating is False


# ---------------------------------------------------------------------------
# Stale-code guard — no legacy "snapshot" label returned anywhere.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", [
    dict(exchange_open=True, live_ltp=100.0, snapshot_close=None, snapshot_last_ltp=None, settled=False),
    dict(exchange_open=False, live_ltp=None, snapshot_close=105.0, snapshot_last_ltp=None, settled=True),
    dict(exchange_open=False, live_ltp=None, snapshot_close=None, snapshot_last_ltp=99.0, settled=False),
    dict(exchange_open=False, live_ltp=None, snapshot_close=None, snapshot_last_ltp=None, settled=False),
])
def test_price_source_uses_new_labels_only(scenario):
    """Every branch returns one of the three post-refactor labels — no
    legacy `snapshot` bare label leaks through."""
    _, source, _ = resolve_current_price(**scenario)
    assert source in ("live", "snapshot_settled", "snapshot_unsettled")
    assert source != "snapshot"   # legacy pre-refactor value must be gone


# ---------------------------------------------------------------------------
# is_animating invariant — true ↔ exchange_open.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exchange_open", [True, False])
def test_is_animating_iff_exchange_open(exchange_open):
    """The animation gate is the exchange-open flag, full stop."""
    _, _, animating = resolve_current_price(
        exchange_open=exchange_open,
        live_ltp=100.0,
        snapshot_close=100.0,
        snapshot_last_ltp=100.0,
        settled=True,
    )
    assert animating is exchange_open
