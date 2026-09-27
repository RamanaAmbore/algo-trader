"""
Tests for fetch_margins() TTL guard in broker_apis.py

Root cause (2026-09 dev-badge investigation, live-verified on prod):
fetch_margins()'s zero-arg ssot_fetch cache had NO TTL guard, unlike its
holdings/positions siblings. conn_service's Dhan interval gate + "no LKG
yet" branch in _fetch_margins_local can return an EMPTY per-account frame
for a Dhan account on the very first fetch after a conn_service restart
(race with session establishment) — that empty-for-Dhan result then got
cached FOREVER by ssot_fetch(mode="coalesce") since fetch_margins() never
passed force_refresh=True on a schedule. Verified live: /internal/margins
kept returning `account: null, rows: []` for both DH3747/DH6847 for
several minutes straight (well past any interval-gate window), while a
direct per-account broker.margins() dispatch call always returned real
data. Fix mirrors the existing _POSITIONS_SSOT_TTL / _HOLDINGS_SSOT_TTL
pattern exactly (same 30 s window, same force_refresh plumbing).

Coverage (mirrors test_positions_ssot_ttl.py):
  1. Two calls within 30 s → _fetch_margins_cached called with force_refresh=False (cache hit)
  2. Second call after >30 s → force_refresh=True (TTL expired)
  3. force_refresh=True → always propagates force_refresh=True downstream
  4. _margins_ssot_refresh_at is updated only after a non-None fetch
  5. Single-account positional/kwarg args bypass the TTL path entirely
"""

from unittest.mock import patch, MagicMock, call
import backend.brokers.broker_apis as broker_apis


def _reset_ttl_state():
    broker_apis._margins_ssot_refresh_at = 0.0


def test_fetch_margins_cache_hit_within_ttl():
    """Two zero-arg fetch_margins() calls within 30 s of each other (after the
    first has stamped refresh_at) → second call is force_refresh=False."""
    _reset_ttl_state()
    sentinel = [object()]

    with patch.object(broker_apis, '_fetch_margins_cached', return_value=sentinel) as mock_cached, \
         patch('backend.brokers.broker_apis._time') as mock_time:

        mock_time.monotonic.return_value = 100.0
        broker_apis.fetch_margins()          # TTL expired from 0.0 → force_refresh=True; stamps 100.0

        mock_time.monotonic.return_value = 115.0
        broker_apis.fetch_margins()          # 15s elapsed < 30 → force_refresh=False

    calls = mock_cached.call_args_list
    assert len(calls) == 2
    assert calls[0] == call(force_refresh=True)
    assert calls[1] == call(force_refresh=False)


def test_fetch_margins_ttl_expired_triggers_refresh():
    """After 30+ s elapses since last fetch, next call uses force_refresh=True —
    this is the exact mechanism that self-heals a Dhan-empty margins cache."""
    _reset_ttl_state()
    sentinel = [object()]

    with patch.object(broker_apis, '_fetch_margins_cached', return_value=sentinel) as mock_cached, \
         patch('backend.brokers.broker_apis._time') as mock_time:

        mock_time.monotonic.return_value = 1000.0
        broker_apis.fetch_margins()

        mock_time.monotonic.return_value = 1035.0
        broker_apis.fetch_margins()

    calls = mock_cached.call_args_list
    assert calls[0] == call(force_refresh=True)
    assert calls[1] == call(force_refresh=True), (
        "35s elapsed since last fetch must force a re-fetch — this is the "
        "fix for margins staying permanently empty for Dhan accounts."
    )


def test_fetch_margins_ttl_boundary_exactly_30s():
    """At exactly 30 s elapsed, TTL is still not expired (> not >=)."""
    _reset_ttl_state()
    broker_apis._margins_ssot_refresh_at = 1000.0
    sentinel = [object()]

    with patch.object(broker_apis, '_fetch_margins_cached', return_value=sentinel) as mock_cached, \
         patch('backend.brokers.broker_apis._time') as mock_time:

        mock_time.monotonic.return_value = 1030.0
        broker_apis.fetch_margins()

        broker_apis._margins_ssot_refresh_at = 1000.0
        mock_time.monotonic.return_value = 1030.001
        broker_apis.fetch_margins()

    calls = mock_cached.call_args_list
    assert calls[0] == call(force_refresh=False), "At exactly 30s, cache should still be valid"
    assert calls[1] == call(force_refresh=True), "At 30.001s, TTL should be expired"


def test_fetch_margins_force_refresh_true_bypasses_ttl():
    """force_refresh=True always calls _fetch_margins_cached(force_refresh=True)
    regardless of elapsed time."""
    _reset_ttl_state()
    sentinel = [object()]

    with patch.object(broker_apis, '_fetch_margins_cached', return_value=sentinel) as mock_cached, \
         patch('backend.brokers.broker_apis._time') as mock_time:

        mock_time.monotonic.return_value = 1000.0
        broker_apis.fetch_margins()

        mock_time.monotonic.return_value = 1001.0
        result = broker_apis.fetch_margins(force_refresh=True)

    calls = mock_cached.call_args_list
    assert calls[1] == call(force_refresh=True)
    assert result is sentinel


def test_fetch_margins_refresh_at_updated_on_success():
    """_margins_ssot_refresh_at is stamped after a successful (non-None) fetch."""
    _reset_ttl_state()
    assert broker_apis._margins_ssot_refresh_at == 0.0
    sentinel = [object()]

    with patch.object(broker_apis, '_fetch_margins_cached', return_value=sentinel), \
         patch('backend.brokers.broker_apis._time') as mock_time:

        mock_time.monotonic.return_value = 9999.0
        broker_apis.fetch_margins()

    assert broker_apis._margins_ssot_refresh_at == 9999.0


def test_fetch_margins_refresh_at_not_updated_on_none_result():
    """A None result (broker outage) must not update refresh_at — next call
    should retry immediately rather than being TTL-gated by a failed attempt."""
    _reset_ttl_state()

    with patch.object(broker_apis, '_fetch_margins_cached', return_value=None), \
         patch('backend.brokers.broker_apis._time') as mock_time:

        mock_time.monotonic.return_value = 7777.0
        broker_apis.fetch_margins()

    assert broker_apis._margins_ssot_refresh_at == 0.0


def test_fetch_margins_with_positional_args_bypasses_ttl():
    """When args/kwargs are present, the single-account local path is used —
    TTL guard / _fetch_margins_cached are not touched."""
    _reset_ttl_state()
    original_refresh_at = broker_apis._margins_ssot_refresh_at
    fake_broker = MagicMock()

    with patch.object(broker_apis, '_fetch_margins_cached') as mock_cached, \
         patch.object(broker_apis, '_fetch_margins_local', return_value=MagicMock()) as mock_local:

        broker_apis.fetch_margins(broker=fake_broker)

    mock_cached.assert_not_called()
    mock_local.assert_called_once_with(broker=fake_broker)
    assert broker_apis._margins_ssot_refresh_at == original_refresh_at


def test_fetch_margins_with_account_kwarg_bypasses_ttl():
    """account= kwarg routes to _fetch_margins_local, not the TTL path."""
    _reset_ttl_state()

    with patch.object(broker_apis, '_fetch_margins_cached') as mock_cached, \
         patch.object(broker_apis, '_fetch_margins_local', return_value=MagicMock()) as mock_local:

        broker_apis.fetch_margins(account="DH6847")

    mock_cached.assert_not_called()
    mock_local.assert_called_once_with(account="DH6847")
