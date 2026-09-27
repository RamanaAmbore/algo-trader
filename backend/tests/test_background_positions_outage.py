"""Tests for the masked-broker-outage guard in `_fetch_positions_direct`
(background.py) and its companion isolation fix in `_compute_firm_nav`
(auth.py) — 2026-09, sibling fix to positions.py's `_is_positions_outage`
(A2).

Bug: `_fetch_positions_direct` (the sync worker `_perf_fetch_all_broker_data`
— the 5-min performance-refresh poller — and `_compute_firm_nav` — the
NavCard `/api/auth/firm-nav` / `/api/auth/me/nav` endpoints — both call
directly) concatenated `broker_apis.fetch_positions()`'s raw per-account
list with NO outage guard. Two masked-failure shapes leaked through as if
they were a legitimate "no positions" result:

  1. `per_acct` non-empty but every frame carries `attrs['fetch_failed'] =
     True` (all accounts unresolvable AND no last-known-good to substitute)
     — concatenated to an empty-but-well-formed frame and fell straight
     through the `raw.empty` guard as fake P&L = 0.
  2. `per_acct == []` while accounts are configured — raised an opaque
     `pd.concat([])` `ValueError` instead of a clear outage signal.

Fix: reuse `positions.py`'s `_is_positions_outage()` (same function, not a
parallel re-implementation) inside `_fetch_positions_direct`, raising
before `pd.concat`. Companion fix: `_compute_firm_nav` isolates its
holdings/positions direct-fetch in its own try/except so an outage there
doesn't wipe a perfectly good `_intraday_equity`-derived Day/Cum P&L back
to 0 — only the (already-fallback-only) live-session/closed-hours branches
lose their direct-fetch inputs.

Five quality dimensions:
  SSOT         — reuses `_is_positions_outage`, no duplicate outage logic
  Correctness  — outage raises; genuine empty book still returns cleanly
  Reachability — confirms `_perf_fetch_all_broker_data` propagates (not
                 swallows) the outage, and `_compute_firm_nav` prefers the
                 live deque over a failed direct-fetch
  Reuse        — same `_is_positions_outage` shapes as positions.py's A2
                 test suite (`test_positions_degradation_integration.py`)
  UX           — NavCard shows last-known-good P&L, not a phantom 0, when
                 the deque is populated and the direct-fetch degrades
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# _fetch_positions_direct — outage detection (mirrors _is_positions_outage
# shapes already covered for the /api/positions route)
# ---------------------------------------------------------------------------

class TestFetchPositionsDirectOutageGuard:
    def test_empty_list_with_accounts_configured_raises(self):
        """Shape (2): per_acct == [] while accounts are configured — must
        raise a clear outage error, not an opaque pd.concat ValueError."""
        from backend.api.background import _fetch_positions_direct

        with patch('backend.brokers.broker_apis.fetch_positions', return_value=[]), \
             patch('backend.brokers.registry._loaded_accounts', return_value=['ACCT_A']):
            with pytest.raises(Exception, match="outage|Bad Gateway"):
                _fetch_positions_direct()

    def test_empty_list_with_no_accounts_configured_does_not_raise(self):
        """Genuinely account-less box — per_acct == [] is legitimate, must
        NOT be classified as an outage (matches _is_positions_outage's
        account-less carve-out)."""
        from backend.api.background import _fetch_positions_direct

        with patch('backend.brokers.broker_apis.fetch_positions', return_value=[]), \
             patch('backend.brokers.registry._loaded_accounts', return_value=[]):
            raw, summary = _fetch_positions_direct()

        assert raw.empty
        assert summary.empty
        assert list(summary.columns) == ['account', 'pnl', 'day_change_val', 'day_change_percentage']

    def test_all_frames_fetch_failed_raises(self):
        """Shape (1): per_acct non-empty, every frame has zero rows AND
        attrs['fetch_failed']=True (no LKG to substitute) — must raise
        instead of silently returning a fake-empty (0 P&L) result."""
        from backend.api.background import _fetch_positions_direct

        df1 = pd.DataFrame()
        df1.attrs['fetch_failed'] = True
        df2 = pd.DataFrame()
        df2.attrs['fetch_failed'] = True

        with patch('backend.brokers.broker_apis.fetch_positions', return_value=[df1, df2]):
            with pytest.raises(Exception, match="outage|Bad Gateway"):
                _fetch_positions_direct()

    def test_partial_failure_does_not_raise_and_keeps_ok_rows(self):
        """One account fetch_failed (zero rows, no LKG), the other genuinely
        succeeded with real rows — NOT an outage (mirrors
        _is_positions_outage's "mixed success/failure is not outage" rule).
        The healthy account's rows must survive."""
        from backend.api.background import _fetch_positions_direct

        failed = pd.DataFrame()
        failed.attrs['fetch_failed'] = True
        healthy = pd.DataFrame([{'account': 'ACCT_A', 'pnl': 1234.0}])

        with patch('backend.brokers.broker_apis.fetch_positions', return_value=[failed, healthy]):
            raw, summary = _fetch_positions_direct()

        assert not raw.empty
        assert 'ACCT_A' in set(raw['account'])
        assert not summary.empty

    def test_partial_failure_sets_partial_outage_attrs(self):
        """2026-09 item 5 fix: a partial failure (one account fetch_failed,
        one healthy) must flag BOTH returned frames' `.attrs['partial_outage']`
        with the failed account code — the caller (agent engine total-scope
        leaf + pnl_history sampler) checks this attr to skip/freeze the
        distorted TOTAL for this tick instead of silently summing over the
        missing account's contribution as if it were zero."""
        from backend.api.background import _fetch_positions_direct

        failed = pd.DataFrame()
        failed.attrs['fetch_failed'] = True
        failed.attrs['account'] = 'ACCT_FAILED'
        healthy = pd.DataFrame([{'account': 'ACCT_A', 'pnl': 1234.0}])

        with patch('backend.brokers.broker_apis.fetch_positions', return_value=[failed, healthy]):
            raw, summary = _fetch_positions_direct()

        assert raw.attrs.get('partial_outage') == ['ACCT_FAILED'], (
            f"Expected raw.attrs['partial_outage'] == ['ACCT_FAILED'], got {raw.attrs.get('partial_outage')}"
        )
        assert summary.attrs.get('partial_outage') == ['ACCT_FAILED'], (
            f"Expected summary.attrs['partial_outage'] == ['ACCT_FAILED'], got {summary.attrs.get('partial_outage')}"
        )

    def test_partial_failure_unresolvable_account_code_still_flagged(self):
        """A fetch_failed frame with NO resolvable account code (the common
        real-world shape — zero rows means the `df['account'].iloc[0]`
        fallback can never fire) must still be counted, as '?' — never
        silently dropped from the partial-outage flag."""
        from backend.api.background import _fetch_positions_direct

        failed = pd.DataFrame()
        failed.attrs['fetch_failed'] = True
        # No attrs['account'] set — mirrors _stale_substitute_frame's
        # no-LKG branch when even the account param resolution failed.
        healthy = pd.DataFrame([{'account': 'ACCT_A', 'pnl': 1234.0}])

        with patch('backend.brokers.broker_apis.fetch_positions', return_value=[failed, healthy]):
            raw, summary = _fetch_positions_direct()

        assert raw.attrs.get('partial_outage') == ['?']
        assert summary.attrs.get('partial_outage') == ['?']

    def test_all_healthy_no_partial_outage_attr(self):
        """Clean case — no partial_outage attr should appear at all (not
        even an empty list) when every account fetched successfully."""
        from backend.api.background import _fetch_positions_direct

        healthy_a = pd.DataFrame([{'account': 'ACCT_A', 'pnl': 1234.0}])
        healthy_b = pd.DataFrame([{'account': 'ACCT_B', 'pnl': -500.0}])

        with patch('backend.brokers.broker_apis.fetch_positions', return_value=[healthy_a, healthy_b]):
            raw, summary = _fetch_positions_direct()

        assert not raw.attrs.get('partial_outage')
        assert not summary.attrs.get('partial_outage')

    def test_rebuild_positions_summary_propagates_partial_outage_attr(self):
        """_rebuild_positions_summary (the post-override rebuild
        `_perf_fetch_all_broker_data` actually uses to overwrite
        `sum_positions`) must carry the partial_outage attr from its
        `raw` input onto the summary object it returns — this is the
        object that reaches `context['sum_positions']` and therefore the
        agent engine."""
        from backend.api.background import _rebuild_positions_summary

        raw = pd.DataFrame([
            {'account': 'ACCT_A', 'pnl': 1000.0, 'day_change_val': 500.0},
        ])
        raw.attrs['partial_outage'] = ['ACCT_FAILED']

        summary = _rebuild_positions_summary(raw)

        assert summary.attrs.get('partial_outage') == ['ACCT_FAILED']

    def test_genuine_empty_successful_book_does_not_raise(self):
        """A successful fetch with zero rows and no fetch_failed attr is a
        legitimate empty book — must not be misclassified as an outage."""
        from backend.api.background import _fetch_positions_direct

        healthy_empty = pd.DataFrame(columns=['account', 'pnl'])

        with patch('backend.brokers.broker_apis.fetch_positions', return_value=[healthy_empty]):
            raw, summary = _fetch_positions_direct()

        assert raw.empty
        assert summary.empty


# ---------------------------------------------------------------------------
# _perf_fetch_all_broker_data — the outage must propagate, not be silently
# absorbed into a fake-empty (df, summary) tuple.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPerfFetchAllBrokerDataPropagatesOutage:
    async def test_positions_outage_propagates_out_of_perf_fetch(self):
        from backend.api import background as bg

        async def _fake_holdings():
            return pd.DataFrame(), pd.DataFrame(columns=['account'])

        with patch.object(bg, '_fetch_holdings_direct',
                           return_value=(pd.DataFrame(), pd.DataFrame(columns=['account']))), \
             patch.object(bg, '_fetch_positions_direct',
                           side_effect=RuntimeError("Broker (Kite) returned no positions data — outage")):
            with pytest.raises(RuntimeError, match="outage"):
                await bg._perf_fetch_all_broker_data()

    async def test_intraday_equity_deque_untouched_on_outage(self):
        """The deque is only mutated by `_perf_append_intraday_equity`,
        called from `_task_performance` AFTER `_perf_fetch_all_broker_data`
        returns successfully. When the fetch raises, that append must never
        run — verified here by confirming the propagated exception prevents
        `_perf_fetch_all_broker_data` from returning at all (so no caller
        downstream of it could feed a phantom-zero point into the deque)."""
        from backend.api import background as bg

        sentinel = [("2026-09-25T10:00:00+05:30", 5000.0, 20000.0, 15000.0, 5000.0, 5000.0, 5000.0)]
        with patch.object(bg, '_intraday_equity', sentinel), \
             patch.object(bg, '_fetch_holdings_direct',
                           return_value=(pd.DataFrame(), pd.DataFrame(columns=['account']))), \
             patch.object(bg, '_fetch_positions_direct',
                           side_effect=RuntimeError("outage")):
            with pytest.raises(RuntimeError):
                await bg._perf_fetch_all_broker_data()
            # Deque still holds only the pre-seeded sentinel — nothing appended.
            assert bg._intraday_equity == sentinel


# ---------------------------------------------------------------------------
# _compute_firm_nav — companion isolation fix: an outage in the direct
# holdings/positions fetch must not wipe a populated intraday-equity-derived
# Day/Cum P&L back to 0.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestComputeFirmNavSurvivesPositionsOutage:
    async def test_deque_populated_outage_in_direct_fetch_still_returns_deque_values(self):
        """`_intraday_equity` has a good last point (from a prior successful
        cycle); the redundant direct-fetch inside `_compute_firm_nav`
        degrades (outage). NavCard must still show the deque's real Day/Cum
        P&L, not 0."""
        from backend.api.routes import auth as auth_mod

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)

        deque_point = ("2026-09-25T10:00:00+05:30", 12345.0, 67890.0, 40000.0, 5000.0, 27890.0, 7345.0)

        async def _fake_compute_firm_nav():
            return {"nav": 2_000_000.0}

        with (
            patch("backend.api.background._fetch_holdings_direct",
                  side_effect=RuntimeError("holdings outage")),
            patch("backend.api.background._fetch_positions_direct",
                  side_effect=RuntimeError("Broker (Kite) returned no positions data — outage")),
            patch("backend.api.background._intraday_equity", [deque_point]),
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_fake_compute_firm_nav),
        ):
            firm_nav, firm_day_pnl, firm_cum_pnl, as_of_iso, _stale = await auth_mod._compute_firm_nav()

        assert firm_nav == pytest.approx(2_000_000.0)
        assert firm_day_pnl == pytest.approx(12345.0), (
            "firm_day_pnl must come from the populated _intraday_equity deque "
            f"(12345.0), not collapse to 0 because the redundant direct-fetch "
            f"failed — got {firm_day_pnl}"
        )
        assert firm_cum_pnl == pytest.approx(67890.0)

    async def test_deque_empty_market_closed_outage_does_not_crash(self):
        """No deque point AND the direct-fetch outages AND the market is
        closed — must not raise out of `_compute_firm_nav` (UX: NavCard
        must never hard-error the endpoint); falls through to whatever the
        closed-hours snapshot fallback produces (0s are acceptable here —
        this is the genuinely-no-data case, not a masked-outage-as-0 case)."""
        from backend.api.routes import auth as auth_mod

        auth_mod._NAV_CACHE.update(ts=0.0, value=None)

        async def _fake_compute_firm_nav():
            return {"nav": 500_000.0}

        with (
            patch("backend.api.background._fetch_holdings_direct",
                  side_effect=RuntimeError("holdings outage")),
            patch("backend.api.background._fetch_positions_direct",
                  side_effect=RuntimeError("positions outage")),
            patch("backend.api.background._intraday_equity", []),
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False),
            patch("backend.api.routes.positions._positions_snapshot", return_value=None),
            patch("backend.api.routes.holdings._holdings_snapshot", return_value=None),
            patch("backend.api.algo.nav.compute_firm_nav", side_effect=_fake_compute_firm_nav),
        ):
            firm_nav, firm_day_pnl, firm_cum_pnl, as_of_iso, _stale = await auth_mod._compute_firm_nav()

        # No exception raised — endpoint stays up. NAV itself is unaffected
        # (comes from a separate, already-outage-guarded code path).
        assert firm_nav == pytest.approx(500_000.0)
