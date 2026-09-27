"""
Tests for fetch_margins() TTL guard in broker_apis.py

Root cause (2026-09 dev-badge investigation, live-verified on prod):

_dhan_next_poll (the interval gate in _is_dhan_interval_due /
_update_dhan_next_poll) is keyed by ACCOUNT ONLY and is shared across
ALL THREE of _fetch_holdings_local / _fetch_positions_local /
_fetch_margins_local (see test_broker_priority.py's own "Reuse" note:
"same _dhan_next_poll dict shared by all three _fetch_*_local fns").
Whichever of the three wins the race for a given Dhan account's poll
slot in a given interval window "claims" it; the other two see
"not yet due" and fall back to _stale_substitute_frame (the LKG) — or,
if NO LKG has ever been recorded for that (kind, account) yet, an EMPTY
frame with interval_skipped=True.

fetch_holdings()/fetch_positions() already force a periodic re-check
every 30s (_HOLDINGS_SSOT_TTL / _POSITIONS_SSOT_TTL). fetch_margins()
had NO such periodic trigger — it relied purely on invalidation events
(postback / ?fresh=1) to ever re-run _fetch_margins_local. If margins
never wins the shared per-account interval-gate race even once (e.g.
holdings/positions' more frequent TTL-driven calls keep winning it),
margins never establishes an LKG for that Dhan account and stays
permanently empty. Live-verified on prod: /internal/margins kept
returning `account: null, rows: []` for both DH3747/DH6847 for several
minutes straight, while a direct broker.margins() dispatch call for the
same accounts (bypassing the interval gate entirely) returned a
structurally-complete result immediately — the underlying balance
happened to be 0.0 on both Dhan accounts at the time (a real value, not
missing data), which is what the interval-gated aggregate path should
also have shown once it got a turn.

Fix mirrors the existing _POSITIONS_SSOT_TTL / _HOLDINGS_SSOT_TTL
pattern exactly (same 30 s window, same force_refresh plumbing) so
margins competes for the shared interval slot on the same cadence as
its siblings instead of never re-attempting at all. This does not
guarantee margins wins every window (the gate is still shared — see
TestSharedIntervalGateMargins below for the real mechanism), but it
converts "permanently stuck empty forever" into "self-heals once
margins wins any window," matching the existing accepted behaviour for
holdings/positions polling each other under the same shared gate.

Coverage:
  1. Two calls within 30 s → _fetch_margins_cached called with force_refresh=False (cache hit)
  2. Second call after >30 s → force_refresh=True (TTL expired)
  3. force_refresh=True → always propagates force_refresh=True downstream
  4. _margins_ssot_refresh_at is updated only after a non-None fetch
  5. Single-account positional/kwarg args bypass the TTL path entirely
  6. TestSharedIntervalGateMargins — real _fetch_holdings_local /
     _fetch_margins_local interaction proving the shared-gate starvation
     mechanism and that a later re-attempt (what the TTL fix guarantees)
     self-heals it.
"""

import time as _time
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


# ---------------------------------------------------------------------------
# Test 6: The REAL shared-interval-gate mechanism — not mocked at the
# _fetch_margins_cached boundary. Reproduces the actual live-observed
# starvation: _dhan_next_poll is one slot PER ACCOUNT, shared by holdings,
# positions, AND margins (see test_broker_priority.py's "Reuse" note).
# ---------------------------------------------------------------------------

def _make_dhan_broker() -> MagicMock:
    """Same helper pattern as test_broker_priority.py — a mock whose class
    name contains 'dhan' so _is_dhan_interval_due treats it as Dhan-gated."""
    m = MagicMock()
    m.__class__.__name__ = "DhanBroker"
    return m


class TestSharedIntervalGateMargins:
    """Reproduces the real starvation mechanism (not the TTL wrapper mock)."""

    ACCOUNT = "TEST-DHAN-GATE-SHARE"

    def setup_method(self):
        broker_apis._dhan_next_poll.pop(self.ACCOUNT, None)
        broker_apis._FETCH_HEALTH.pop(self.ACCOUNT, None)
        broker_apis._LKG_FRAME_BY_ACCT.pop(("margins", self.ACCOUNT), None)
        broker_apis._LKG_FRAME_BY_ACCT.pop(("holdings", self.ACCOUNT), None)

    def teardown_method(self):
        broker_apis._dhan_next_poll.pop(self.ACCOUNT, None)
        broker_apis._FETCH_HEALTH.pop(self.ACCOUNT, None)
        broker_apis._LKG_FRAME_BY_ACCT.pop(("margins", self.ACCOUNT), None)
        broker_apis._LKG_FRAME_BY_ACCT.pop(("holdings", self.ACCOUNT), None)

    def test_holdings_wins_slot_then_margins_starves_with_no_lkg(self):
        """_fetch_holdings_local claims this account's ONE shared poll slot.
        The immediately-following _fetch_margins_local call for the SAME
        account sees 'not yet due' and — since margins has no LKG for this
        account yet — returns an EMPTY, interval_skipped frame. This is the
        exact live-observed failure mode (account: null, rows: [] forever)
        when nothing ever forces margins to retry."""
        # Call the undecorated function directly with account=/broker=/kite=
        # kwargs — same pattern as test_positions_r1_substitution.py. This
        # bypasses @for_all_accounts' single-account Case 1 branch, which
        # would otherwise look the account up in the real Connections().conn
        # registry (KeyError for a fake test account).
        _fetch_holdings = broker_apis._fetch_holdings_local.__wrapped__
        _fetch_margins = broker_apis._fetch_margins_local.__wrapped__

        dhan_broker = _make_dhan_broker()
        dhan_broker.holdings.return_value = [
            {"tradingsymbol": "TESTSTOCK", "quantity": 1, "average_price": 100.0}
        ]

        holdings_df = _fetch_holdings(account=self.ACCOUNT, broker=dhan_broker, kite=None)
        assert not holdings_df.empty, "holdings call should have won the slot and succeeded"

        margins_df = _fetch_margins(account=self.ACCOUNT, broker=dhan_broker, kite=None)
        assert margins_df.empty, (
            "margins must be starved (empty) — the shared interval slot was "
            "just claimed by holdings and margins has no LKG of its own yet"
        )
        assert margins_df.attrs.get("interval_skipped") is True

    def test_margins_self_heals_once_it_gets_a_turn(self):
        """Once the interval elapses and margins is actually invoked again
        (exactly what the fetch_margins() TTL fix guarantees happens every
        30s instead of never), the real broker call runs and succeeds —
        proving the fix's mechanism, not just the wrapper's call count."""
        _fetch_holdings = broker_apis._fetch_holdings_local.__wrapped__
        _fetch_margins = broker_apis._fetch_margins_local.__wrapped__

        dhan_broker = _make_dhan_broker()
        dhan_broker.holdings.return_value = [
            {"tradingsymbol": "TESTSTOCK", "quantity": 1, "average_price": 100.0}
        ]
        dhan_broker.margins.return_value = {
            "enabled": True, "net": 0.0,
            "available": {"cash": 0.0, "live_balance": 0.0},
            "utilised": {"debits": 0.0},
        }

        # Holdings wins the slot first (mirrors the live scenario).
        _fetch_holdings(account=self.ACCOUNT, broker=dhan_broker, kite=None)
        starved = _fetch_margins(account=self.ACCOUNT, broker=dhan_broker, kite=None)
        assert starved.empty, "sanity: margins starved on the first attempt"

        # Simulate the interval elapsing (what happens ~30s later in
        # production, and what fetch_margins()'s new TTL forces a caller
        # to retry for instead of never calling _fetch_margins_local again).
        broker_apis._dhan_next_poll[self.ACCOUNT] = _time.time() - 1.0

        healed = _fetch_margins(account=self.ACCOUNT, broker=dhan_broker, kite=None)
        assert not healed.empty, (
            "margins must succeed once it actually gets a turn at the "
            "shared interval slot — this is the recovery path the TTL "
            "fix relies on to self-heal a Dhan account stuck starved"
        )
        assert healed["account"].iloc[0] == self.ACCOUNT
