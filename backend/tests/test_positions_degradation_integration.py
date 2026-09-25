"""Integration tests for positions route degradation handling (A2 + A4-R1).

These tests exercise the REAL implementation — `positions.py`'s
`_is_positions_outage`, `_accounts_flagged_stale`, and `_fetch()` — not a
re-implementation of the logic inline. They validate that failed fetches
are correctly surfaced rather than silently converted to empty-but-"live"
responses.

Key scenarios (A2 task repro (a)/(b)/(c)):
  (a) per_acct == [] (all accounts unresolvable) while accounts are
      configured routes through the outage path (raises), NOT a fake-live
      empty response. The genuinely account-less counterpart (no accounts
      configured anywhere) must NOT raise.
  (b) Partial-account failure (R1): one account's fetch fails while a
      sibling succeeds — the failing account's rows must not silently
      vanish from a 'live'-tagged response, and must appear in
      `stale_accounts`, even when there is no last-known-good data to
      substitute (zero rows for that account).
  (c) A genuine, real empty book (e.g. post-rollover, zero real positions)
      still correctly returns `rows=[]`, `stale_accounts=[]`, and does NOT
      raise — the A2 fix must not break the legitimate empty-book case.

Also covers the `snapshot_gate._stash_live_response` contract: an
outage-raise must never reach the stash, while a genuine empty response
does get stashed (A2's "never cache a masked failure as last-good", without
breaking the legitimate empty-book cache).
"""

from __future__ import annotations

import time as _time
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_position_row(
    account: str = "DEMO",
    tradingsymbol: str = "INFY",
    exchange: str = "NSE",
    quantity: int = 50,
    average_price: float = 2400.0,
    last_price: float = 2500.0,
    prev_close: float = 2480.0,
    product: str = "MIS",
    **kwargs,
) -> dict:
    """Build a minimal, fully-enriched position row (as if it had already
    passed through `broker_apis._enrich_positions`) — real equity symbols
    only (non-option) so `_enrich_position_greeks` no-ops without a broker
    round-trip."""
    defaults = dict(
        account=account,
        tradingsymbol=tradingsymbol,
        exchange=exchange,
        quantity=quantity,
        average_price=average_price,
        last_price=last_price,
        prev_close=prev_close,
        product=product,
        multiplier=1,
        unrealised=0.0,
        realised=0.0,
        day_change=last_price - prev_close,
        day_change_val=(last_price - prev_close) * quantity,
        day_change_percentage=0.0,
        pnl=(last_price - average_price) * quantity,
        overnight_quantity=quantity,
        day_buy_quantity=0,
        day_sell_quantity=0,
        day_buy_value=0.0,
        day_sell_value=0.0,
    )
    return dict(defaults, **kwargs)


def _mock_db_session(execute_side_effect: Exception | None = None):
    """Build a mock async context-manager session matching the pattern
    already used in `test_positions_route.py` — patched onto
    `backend.api.database.async_session` so every local `from
    backend.api.database import async_session` call site (GTT fetch,
    `latest_snapshot_ltp_map`) picks it up without a real DB."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    if execute_side_effect is not None:
        mock_session.execute = AsyncMock(side_effect=execute_side_effect)
    else:
        # Empty result set — .all() returns [] for latest_snapshot_ltp_map.
        result = AsyncMock()
        result.all = lambda: []
        mock_session.execute = AsyncMock(return_value=result)
    return mock_session


# ---------------------------------------------------------------------------
# _is_positions_outage — pure unit tests (no I/O)
# ---------------------------------------------------------------------------

class TestIsPositionsOutage:
    def test_empty_list_with_accounts_configured_is_outage(self):
        from backend.api.routes.positions import _is_positions_outage
        with patch("backend.brokers.registry._loaded_accounts", return_value=["ACCT_A"]):
            assert _is_positions_outage([]) is True

    def test_empty_list_with_no_accounts_configured_is_not_outage(self):
        """Genuinely account-less box — [] is a legitimate empty state."""
        from backend.api.routes.positions import _is_positions_outage
        with patch("backend.brokers.registry._loaded_accounts", return_value=[]):
            assert _is_positions_outage([]) is False

    def test_all_frames_fetch_failed_is_outage(self):
        from backend.api.routes.positions import _is_positions_outage
        df1 = pd.DataFrame()
        df1.attrs["fetch_failed"] = True
        df2 = pd.DataFrame()
        df2.attrs["fetch_failed"] = True
        assert _is_positions_outage([df1, df2]) is True

    def test_mixed_success_failure_is_not_outage(self):
        from backend.api.routes.positions import _is_positions_outage
        healthy = pd.DataFrame([{"account": "ACCT_A", "quantity": 1}])
        failed = pd.DataFrame()
        failed.attrs["fetch_failed"] = True
        assert _is_positions_outage([healthy, failed]) is False

    def test_genuine_empty_successful_frame_is_not_outage(self):
        """A successful fetch with zero rows and no fetch_failed attr must
        never be classified as an outage — this is the legitimate
        empty-book case."""
        from backend.api.routes.positions import _is_positions_outage
        healthy_empty = pd.DataFrame()
        assert _is_positions_outage([healthy_empty]) is False


# ---------------------------------------------------------------------------
# _accounts_flagged_stale — pure unit tests (no I/O)
# ---------------------------------------------------------------------------

class TestAccountsFlaggedStale:
    def test_stale_attr_with_rows_picks_up_account_column(self):
        from backend.api.routes.positions import _accounts_flagged_stale
        df = pd.DataFrame([{"account": "ACCT_B", "tradingsymbol": "TCS"}])
        df.attrs["stale"] = True
        assert _accounts_flagged_stale([df]) == {"ACCT_B"}

    def test_fetch_failed_empty_frame_uses_attrs_account_fallback(self):
        """No-LKG failure case: zero rows, but `_stale_substitute_frame`
        tags attrs['account'] so the account is still identifiable."""
        from backend.api.routes.positions import _accounts_flagged_stale
        df = pd.DataFrame()
        df.attrs["fetch_failed"] = True
        df.attrs["account"] = "ACCT_B"
        assert _accounts_flagged_stale([df]) == {"ACCT_B"}

    def test_healthy_frame_not_flagged(self):
        from backend.api.routes.positions import _accounts_flagged_stale
        df = pd.DataFrame([{"account": "ACCT_A", "quantity": 1}])
        assert _accounts_flagged_stale([df]) == set()

    def test_mixed_returns_only_flagged_accounts(self):
        from backend.api.routes.positions import _accounts_flagged_stale
        healthy = pd.DataFrame([{"account": "ACCT_A", "quantity": 1}])
        stale = pd.DataFrame([{"account": "ACCT_B", "quantity": 1}])
        stale.attrs["stale"] = True
        assert _accounts_flagged_stale([healthy, stale]) == {"ACCT_B"}


# ---------------------------------------------------------------------------
# (a) per_acct == [] routes through the outage path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFetchOutageDetection:
    async def test_fetch_raises_when_per_acct_empty_and_accounts_configured(self):
        """The core A2 repro: for_all_accounts / conn_service resolved
        NOTHING for any configured account. Must raise (routes through the
        outage/fallback path upstream), not fabricate a fake-live empty
        response."""
        from backend.api.routes import positions as positions_module

        with patch.object(positions_module.broker_apis, "fetch_positions", return_value=[]), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["ACCT_A"]):
            with pytest.raises(Exception, match="Bad Gateway|outage"):
                await positions_module._fetch()

    async def test_fetch_does_not_raise_when_genuinely_no_accounts_configured(self):
        """Counterpart: zero accounts configured anywhere → per_acct == []
        is legitimate, not an outage."""
        from backend.api.routes import positions as positions_module

        with patch.object(positions_module.broker_apis, "fetch_positions", return_value=[]), \
             patch("backend.brokers.registry._loaded_accounts", return_value=[]):
            resp = await positions_module._fetch()

        assert resp.rows == []
        assert resp.stale_accounts == []


# ---------------------------------------------------------------------------
# (c) genuine empty book still returns rows=[] as a real, non-outage response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_genuine_empty_book_returns_live_empty_not_outage():
    """One (or more) accounts configured, broker fetch SUCCEEDED with zero
    positions on every account (operator closed everything / post-rollover).
    Must return a real PositionsResponse(rows=[]) without raising and
    without any staleness tagging."""
    from backend.api.routes import positions as positions_module

    healthy_empty = pd.DataFrame()  # successful fetch, no fetch_failed attr

    with patch.object(positions_module.broker_apis, "fetch_positions", return_value=[healthy_empty]), \
         patch("backend.brokers.registry._loaded_accounts", return_value=["ACCT_A"]):
        resp = await positions_module._fetch()

    assert resp.rows == []
    assert resp.stale_accounts == []


# ---------------------------------------------------------------------------
# (b) partial-account failure — failing account's rows must not vanish,
# and must appear in stale_accounts, from an otherwise-'live' response.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFetchPartialFailure:
    async def test_substituted_account_rows_survive_and_marked_stale(self):
        """ACCT_A succeeds; ACCT_B's fetch failed but was substituted with
        last-known-good (R1). Both accounts' rows must be present in the
        final response, and ACCT_B must appear in stale_accounts."""
        from backend.api.routes import positions as positions_module

        healthy_df = pd.DataFrame([_make_position_row(account="ACCT_A", tradingsymbol="INFY")])

        substituted_df = pd.DataFrame([_make_position_row(account="ACCT_B", tradingsymbol="TCS")])
        substituted_df["account_stale"] = True
        substituted_df.attrs["stale"] = True
        substituted_df.attrs["stale_since"] = _time.time()
        substituted_df.attrs["account"] = "ACCT_B"
        # fetch_failed intentionally NOT set — substitution is a success
        # with old data, not a fetch failure (mirrors _stale_substitute_frame).

        per_acct = [healthy_df, substituted_df]
        mock_session = _mock_db_session()

        with patch.object(positions_module.broker_apis, "fetch_positions", return_value=per_acct), \
             patch.object(positions_module.broker_apis, "backfill_market_data", return_value=0), \
             patch("backend.api.database.async_session", return_value=mock_session), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["ACCT_A", "ACCT_B"]):
            resp = await positions_module._fetch()

        accounts_in_rows = {r.account for r in resp.rows}
        assert "ACCT_A" in accounts_in_rows
        assert "ACCT_B" in accounts_in_rows, (
            "R1: substituted account's rows must not silently vanish from "
            "an otherwise-'live' response"
        )
        assert "ACCT_B" in resp.stale_accounts
        assert "ACCT_A" not in resp.stale_accounts

    async def test_no_lkg_failure_zero_rows_still_marked_stale(self):
        """ACCT_A succeeds; ACCT_B's fetch failed with NO last-known-good
        available (cold start) — zero rows for ACCT_B, but it must still
        surface in stale_accounts so the frontend knows this account's
        data is incomplete, per the R1 'at minimum' requirement."""
        from backend.api.routes import positions as positions_module

        healthy_df = pd.DataFrame([_make_position_row(account="ACCT_A", tradingsymbol="INFY")])

        failed_no_lkg_df = pd.DataFrame()
        failed_no_lkg_df.attrs["fetch_failed"] = True
        failed_no_lkg_df.attrs["circuit_open"] = True
        failed_no_lkg_df.attrs["account"] = "ACCT_B"

        per_acct = [healthy_df, failed_no_lkg_df]
        mock_session = _mock_db_session()

        with patch.object(positions_module.broker_apis, "fetch_positions", return_value=per_acct), \
             patch.object(positions_module.broker_apis, "backfill_market_data", return_value=0), \
             patch("backend.api.database.async_session", return_value=mock_session), \
             patch("backend.brokers.registry._loaded_accounts", return_value=["ACCT_A", "ACCT_B"]):
            resp = await positions_module._fetch()

        accounts_in_rows = {r.account for r in resp.rows}
        assert accounts_in_rows == {"ACCT_A"}, "ACCT_A's real row is present; ACCT_B has no data to show"
        assert "ACCT_B" in resp.stale_accounts, (
            "Even with zero rows, a failed account with no LKG must still "
            "surface in stale_accounts (R1 'at minimum' requirement)"
        )


# ---------------------------------------------------------------------------
# snapshot_gate stash contract — outage never stashed; genuine empty is.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_gate_never_stashes_a_raised_outage():
    """When broker_fn raises (the A2 outage path), closed_hours_or_broker
    must never call _stash_live_response — the exception itself is the
    gate that keeps a masked failure out of the anti-flicker cache."""
    from backend.api.helpers import snapshot_gate

    snapshot_gate._last_response_by_route.pop("test_outage_stash", None)

    async def _snap():
        return {"rows": ["snapshot_data"]}

    async def _broker_raises():
        raise Exception("Broker (Kite) returned no positions data — upstream Bad Gateway / outage")

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        data, source = await snapshot_gate.closed_hours_or_broker(
            "NSE", _snap, _broker_raises,
            fallback_to_snapshot_on_broker_error=True,
            route_key="test_outage_stash",
        )

    assert source in ("snapshot-fallback", "stale-live")
    stashed = snapshot_gate._get_stale_live("test_outage_stash")
    assert stashed is None, "An outage-raise must never be stashed as last-good live data"

    snapshot_gate._last_response_by_route.pop("test_outage_stash", None)


@pytest.mark.asyncio
async def test_snapshot_gate_stashes_genuine_empty_response():
    """A genuinely empty (but successful, non-raising) broker_fn response
    MUST still be stashed — the A2 fix only gates on the outage-raise, it
    does not add a blanket 'skip empty rows' filter (that would break the
    legitimate empty-book case: closing everything then a transient
    broker blip within the TTL window must still serve the correct empty
    state, not stale pre-close data)."""
    from backend.api.helpers import snapshot_gate

    snapshot_gate._last_response_by_route.pop("test_genuine_empty_stash", None)

    genuinely_empty = {"rows": []}

    async def _snap():
        return {"rows": ["snapshot_data"]}

    async def _broker_ok():
        return genuinely_empty

    with patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True):
        data, source = await snapshot_gate.closed_hours_or_broker(
            "NSE", _snap, _broker_ok,
            fallback_to_snapshot_on_broker_error=True,
            route_key="test_genuine_empty_stash",
        )

    assert source == "live"
    stashed = snapshot_gate._get_stale_live("test_genuine_empty_stash")
    assert stashed is genuinely_empty, "A genuine (non-raised) empty response must still be cached as last-good"

    snapshot_gate._last_response_by_route.pop("test_genuine_empty_stash", None)
