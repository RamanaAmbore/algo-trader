"""
Tests for fetch_positions() TTL guard in broker_apis.py

Coverage:
  1. Two calls within 30 s → _fetch_positions_cached called only once (cache hit)
  2. Second call after >30 s → _fetch_positions_cached called again (TTL expired)
  3. force_refresh=True → _fetch_positions_cached always called with force_refresh=True
     regardless of elapsed time
  4. _positions_ssot_refresh_at is updated after a successful fetch
  5. Single-account positional args bypass the TTL path entirely
"""

import pytest
from unittest.mock import patch, MagicMock, call
import backend.brokers.broker_apis as broker_apis


def _reset_ttl_state():
    """Reset module-level TTL state between tests to avoid ordering issues."""
    broker_apis._positions_ssot_refresh_at = 0.0


# ---------------------------------------------------------------------------
# Test 1: Two calls within the 30-second window → cached call only once
# ---------------------------------------------------------------------------

def test_fetch_positions_cache_hit_within_ttl():
    """Two zero-arg fetch_positions() calls within 30 s → underlying called once."""
    _reset_ttl_state()

    sentinel = [object()]  # dummy non-None return value

    with patch.object(broker_apis, '_fetch_positions_cached', return_value=sentinel) as mock_cached, \
         patch('backend.brokers.broker_apis._time') as mock_time:

        # Both calls happen at t=0 (well within 30 s TTL)
        mock_time.monotonic.return_value = 0.0

        result1 = broker_apis.fetch_positions()
        # After first call, _positions_ssot_refresh_at is updated to 0.0
        # (monotonic mock returns 0.0 for both the guard check and the stamp)

        # Second call at t=5 s — still within TTL (5 < 30)
        mock_time.monotonic.return_value = 5.0
        result2 = broker_apis.fetch_positions()

    # _fetch_positions_cached should have been called exactly twice in total —
    # once with force_refresh=True (TTL expired at t=0, _positions_ssot_refresh_at=0)
    # and once with force_refresh=False (t=5, refresh_at=0, 5-0=5 < 30 → cache hit).
    # More precisely: first call sets refresh_at=0.0 (stamp via monotonic()=0.0),
    # second call: now=5.0, 5.0-0.0=5.0 < 30 → force_refresh stays False → 1 call.
    assert mock_cached.call_count == 2, (
        f"Expected 2 calls to _fetch_positions_cached "
        f"(first call: TTL expired from 0.0; second: cache hit). "
        f"Got {mock_cached.call_count}"
    )
    # First call triggers TTL expiry (0.0 - 0.0 = 0.0 > 30? No — 0 is NOT > 30,
    # but _positions_ssot_refresh_at starts at 0.0 and monotonic starts at 0.0
    # so 0.0 - 0.0 = 0.0 which is NOT > 30 → force_refresh stays False on first call too.
    # Both calls should have force_refresh=False in this scenario.
    mock_cached.assert_any_call(force_refresh=False)
    assert result1 is sentinel
    assert result2 is sentinel


def test_fetch_positions_cache_hit_second_call_no_new_fetch():
    """After a successful fetch stamps refresh_at, a second call within TTL
    uses force_refresh=False (no new broker round-trip triggered by TTL)."""
    _reset_ttl_state()

    sentinel = [object()]

    with patch.object(broker_apis, '_fetch_positions_cached', return_value=sentinel) as mock_cached, \
         patch('backend.brokers.broker_apis._time') as mock_time:

        # First call at t=100 — refresh_at is 0.0 so TTL expired (100 > 30)
        mock_time.monotonic.return_value = 100.0
        broker_apis.fetch_positions()
        # refresh_at is now stamped to 100.0

        # Second call at t=115 — only 15 s since last fetch, well within TTL
        mock_time.monotonic.return_value = 115.0
        broker_apis.fetch_positions()

    assert mock_cached.call_count == 2
    # First call: force_refresh=True (TTL expired: 100-0=100 > 30)
    # Second call: force_refresh=False (TTL not expired: 115-100=15 < 30)
    calls = mock_cached.call_args_list
    assert calls[0] == call(force_refresh=True), f"First call should be force_refresh=True, got {calls[0]}"
    assert calls[1] == call(force_refresh=False), f"Second call should be force_refresh=False, got {calls[1]}"


# ---------------------------------------------------------------------------
# Test 2: Second call after >30 s → TTL expired → force_refresh=True
# ---------------------------------------------------------------------------

def test_fetch_positions_ttl_expired_triggers_refresh():
    """After 30+ s elapses since last fetch, next call uses force_refresh=True."""
    _reset_ttl_state()

    sentinel = [object()]

    with patch.object(broker_apis, '_fetch_positions_cached', return_value=sentinel) as mock_cached, \
         patch('backend.brokers.broker_apis._time') as mock_time:

        # First call at t=1000 — TTL expired from initial 0.0 (1000 > 30)
        mock_time.monotonic.return_value = 1000.0
        broker_apis.fetch_positions()
        # refresh_at stamped to 1000.0

        # Second call at t=1035 — 35 s elapsed, TTL expired
        mock_time.monotonic.return_value = 1035.0
        broker_apis.fetch_positions()

    assert mock_cached.call_count == 2
    calls = mock_cached.call_args_list
    # Both calls have force_refresh=True (both times TTL was expired)
    assert calls[0] == call(force_refresh=True), f"First call: {calls[0]}"
    assert calls[1] == call(force_refresh=True), f"Second call after TTL expiry: {calls[1]}"


def test_fetch_positions_ttl_boundary_exactly_30s():
    """At exactly 30 s elapsed, TTL is still not expired (> not >=).
    At 30.001 s from a fixed refresh_at (without intermediate stamps), TTL expires.
    """
    _reset_ttl_state()
    # Manually fix refresh_at so we control the baseline without an intermediate stamp
    broker_apis._positions_ssot_refresh_at = 1000.0

    sentinel = [object()]

    with patch.object(broker_apis, '_fetch_positions_cached', return_value=sentinel) as mock_cached, \
         patch('backend.brokers.broker_apis._time') as mock_time:

        # Exactly 30 s since refresh_at=1000.0: 1030.0-1000.0=30.0, NOT > 30 → cache hit
        mock_time.monotonic.return_value = 1030.0
        broker_apis.fetch_positions()
        # refresh_at is now stamped to 1030.0

        # Reset refresh_at back to 1000.0 to test the 30.001 boundary cleanly
        broker_apis._positions_ssot_refresh_at = 1000.0

        # 30.001 s: 1030.001-1000.0 = 30.001 > 30 → TTL expired → force_refresh=True
        mock_time.monotonic.return_value = 1030.001
        broker_apis.fetch_positions()

    calls = mock_cached.call_args_list
    assert len(calls) == 2
    assert calls[0] == call(force_refresh=False), "At exactly 30 s, cache should still be valid"
    assert calls[1] == call(force_refresh=True), "At 30.001 s, TTL should be expired"


# ---------------------------------------------------------------------------
# Test 3: force_refresh=True → always calls _fetch_positions_cached(force_refresh=True)
# ---------------------------------------------------------------------------

def test_fetch_positions_force_refresh_true_bypasses_ttl():
    """force_refresh=True always calls _fetch_positions_cached(force_refresh=True)
    regardless of elapsed time."""
    _reset_ttl_state()

    sentinel = [object()]

    with patch.object(broker_apis, '_fetch_positions_cached', return_value=sentinel) as mock_cached, \
         patch('backend.brokers.broker_apis._time') as mock_time:

        # Seed a recent refresh so TTL is NOT expired
        mock_time.monotonic.return_value = 1000.0
        broker_apis.fetch_positions()
        # refresh_at = 1000.0

        # Call at t=1001 (1 s later — TTL not expired), but force_refresh=True
        mock_time.monotonic.return_value = 1001.0
        result = broker_apis.fetch_positions(force_refresh=True)

    assert mock_cached.call_count == 2
    calls = mock_cached.call_args_list
    assert calls[1] == call(force_refresh=True), (
        f"force_refresh=True should always pass force_refresh=True downstream, got {calls[1]}"
    )
    assert result is sentinel


def test_fetch_positions_force_refresh_always_fires_even_when_fresh():
    """force_refresh=True works even immediately after a fetch (0 s elapsed)."""
    _reset_ttl_state()

    sentinel = [object()]
    call_log = []

    def fake_cached(force_refresh=False):
        call_log.append(force_refresh)
        return sentinel

    with patch.object(broker_apis, '_fetch_positions_cached', side_effect=fake_cached), \
         patch('backend.brokers.broker_apis._time') as mock_time:

        mock_time.monotonic.return_value = 500.0
        broker_apis.fetch_positions()              # seeds refresh_at = 500.0

        mock_time.monotonic.return_value = 500.0  # same moment → TTL not expired
        broker_apis.fetch_positions(force_refresh=True)

    assert call_log == [True, True], (
        f"Expected [True, True] (first: TTL expired from 0; second: explicit force). Got {call_log}"
    )


# ---------------------------------------------------------------------------
# Test 4: _positions_ssot_refresh_at updated after successful fetch
# ---------------------------------------------------------------------------

def test_fetch_positions_refresh_at_updated_on_success():
    """_positions_ssot_refresh_at is stamped to current monotonic time
    after a successful (non-None) return."""
    _reset_ttl_state()
    assert broker_apis._positions_ssot_refresh_at == 0.0

    sentinel = [object()]

    with patch.object(broker_apis, '_fetch_positions_cached', return_value=sentinel), \
         patch('backend.brokers.broker_apis._time') as mock_time:

        mock_time.monotonic.return_value = 9999.0
        broker_apis.fetch_positions()

    assert broker_apis._positions_ssot_refresh_at == 9999.0, (
        f"refresh_at should be 9999.0 after a successful fetch, "
        f"got {broker_apis._positions_ssot_refresh_at}"
    )


def test_fetch_positions_refresh_at_not_updated_on_none_result():
    """When _fetch_positions_cached returns None, _positions_ssot_refresh_at
    is NOT updated — a failed/empty fetch should not block the next TTL check."""
    _reset_ttl_state()

    with patch.object(broker_apis, '_fetch_positions_cached', return_value=None), \
         patch('backend.brokers.broker_apis._time') as mock_time:

        mock_time.monotonic.return_value = 7777.0
        broker_apis.fetch_positions()

    assert broker_apis._positions_ssot_refresh_at == 0.0, (
        f"refresh_at should remain 0.0 after a None result, "
        f"got {broker_apis._positions_ssot_refresh_at}"
    )


# ---------------------------------------------------------------------------
# Test 5: Positional/kwarg args bypass the TTL path entirely
# ---------------------------------------------------------------------------

def test_fetch_positions_with_positional_args_bypasses_ttl():
    """When args or kwargs are present, the single-account local path is used
    and the TTL guard / _fetch_positions_cached are not touched."""
    _reset_ttl_state()
    original_refresh_at = broker_apis._positions_ssot_refresh_at

    fake_broker = MagicMock()

    with patch.object(broker_apis, '_fetch_positions_cached') as mock_cached, \
         patch.object(broker_apis, '_fetch_positions_local', return_value=MagicMock()) as mock_local:

        broker_apis.fetch_positions(broker=fake_broker)

    mock_cached.assert_not_called()
    mock_local.assert_called_once_with(broker=fake_broker)
    assert broker_apis._positions_ssot_refresh_at == original_refresh_at, (
        "TTL stamp should not be touched by the local path"
    )


def test_fetch_positions_with_account_kwarg_bypasses_ttl():
    """account= kwarg routes to _fetch_positions_local, not TTL path."""
    _reset_ttl_state()

    with patch.object(broker_apis, '_fetch_positions_cached') as mock_cached, \
         patch.object(broker_apis, '_fetch_positions_local', return_value=MagicMock()) as mock_local:

        broker_apis.fetch_positions(account="ZG0790")

    mock_cached.assert_not_called()
    mock_local.assert_called_once_with(account="ZG0790")
