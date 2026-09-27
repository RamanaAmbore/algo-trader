"""
Tests for the NAV degraded-broker-fetch freeze fix (2026-09-27 council audit,
Bug 1 — CLAUDE.md "Staleness indicator freeze rule").

Root cause: `compute_firm_nav()`'s `_fetch_funds_phase` / `_fetch_positions_phase`
/ `_fetch_holdings_phase` (backend/api/algo/nav.py) called
`fetch_margins()`/`fetch_positions()`/`fetch_holdings()` and summed whatever
DataFrame came back, but never checked `df.attrs.get('fetch_failed')` — the
exact same masked-broker-outage shape `positions.py:_is_positions_outage` /
`_accounts_flagged_stale` already detect on the SAME per-account DataFrame
contract. A single transient per-account exception returns an EMPTY frame
with `attrs['fetch_failed']=True` (not a raised exception), so nav.py's own
try/except never saw it — that account's leg silently became 0 and
`write_nav_snapshot()` unconditionally persisted the understated NAV with
`note=None`, permanently corrupting investor-facing NAV history.

Fix: `_substitute_degraded_frames()` detects the `fetch_failed` shape and
substitutes `broker_apis._stale_substitute_frame` (the SAME last-known-good
mechanism the circuit-breaker-open path already uses) in place of a silent
zero, and populates `errors` (LKG-recovered accounts informationally,
genuinely-understated ones via the `_UNDERSTATED_TAG` prefix — see that
constant's docstring). `write_nav_snapshot()` gates purely on
`snap["understated"]`: it skips the write entirely (never persists a wrong
number, even for the day's first-ever snapshot) whenever ANY leg is
genuinely understated, and proceeds normally otherwise — including when
`errors` is non-empty but `understated` is empty (an LKG-substituted,
still-trustworthy value).

Five quality dimensions:
  SSOT        — reuses broker_apis._stale_substitute_frame (not a new cache)
  Correctness — LKG-available case freezes to real values; no-LKG case still
                excludes the leg but is now VISIBLE via errors (never silent)
  Reachability — exercised through the real _fetch_*_phase functions and
                compute_firm_nav(), not just the helper in isolation
  Reuse       — mirrors positions.py's attrs contract exactly
  UX          — write_nav_snapshot never persists a genuinely understated
                figure; a skipped write is logged (+ once-daily audited),
                never silent
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# _substitute_degraded_frames — pure helper
# ---------------------------------------------------------------------------

class TestSubstituteDegradedFrames:
    def test_healthy_frame_passes_through_unchanged(self):
        from backend.api.algo.nav import _substitute_degraded_frames

        df = pd.DataFrame([{"account": "ZG0790", "cash": 1000.0}])
        errors: list[str] = []
        out = _substitute_degraded_frames([df], "margins", errors)

        assert out == [df]
        assert errors == []

    def test_fetch_failed_frame_with_lkg_freezes_to_last_known_good(self):
        """The core bug scenario: a single transient exception returns an
        empty fetch_failed=True frame for one account. When broker_apis has
        an LKG copy, the account's real (if slightly old) values must be
        used instead of silently contributing zero."""
        from backend.api.algo.nav import _substitute_degraded_frames

        failed = pd.DataFrame()
        failed.attrs["fetch_failed"] = True
        failed.attrs["account"] = "DH6847"

        lkg = pd.DataFrame([{"account": "DH6847", "cash": 20000.0}])
        lkg.attrs["stale"] = True
        lkg.attrs["stale_since"] = 1700000000.0

        errors: list[str] = []
        with patch(
            "backend.brokers.broker_apis._stale_substitute_frame",
            return_value=lkg,
        ) as mock_sub:
            out = _substitute_degraded_frames([failed], "margins", errors)

        mock_sub.assert_called_once_with("margins", "DH6847")
        assert len(out) == 1
        assert out[0] is lkg
        assert out[0].empty is False
        assert out[0]["cash"].iloc[0] == 20000.0
        assert len(errors) == 1
        assert "DH6847" in errors[0]
        assert "degraded" in errors[0]

    def test_fetch_failed_frame_with_no_lkg_contributes_zero_but_is_flagged(self):
        """No LKG anywhere (fresh restart / >24h offline) — genuinely no
        data to freeze to. The account must still be visible in `errors`
        (never a silent drop), even though its leg contributes 0."""
        from backend.api.algo.nav import _substitute_degraded_frames

        failed = pd.DataFrame()
        failed.attrs["fetch_failed"] = True
        failed.attrs["account"] = "DH6847"

        no_lkg = pd.DataFrame()
        no_lkg.attrs["fetch_failed"] = True
        no_lkg.attrs["account"] = "DH6847"

        errors: list[str] = []
        with patch(
            "backend.brokers.broker_apis._stale_substitute_frame",
            return_value=no_lkg,
        ):
            out = _substitute_degraded_frames([failed], "holdings", errors)

        assert out[0].empty
        assert len(errors) == 1
        assert "DH6847" in errors[0]
        assert "no last-known-good" in errors[0]

    def test_unidentifiable_account_still_recorded_in_errors(self):
        from backend.api.algo.nav import _substitute_degraded_frames

        failed = pd.DataFrame()
        failed.attrs["fetch_failed"] = True
        # No attrs['account'] and no 'account' column — can't resolve.

        errors: list[str] = []
        out = _substitute_degraded_frames([failed], "positions", errors)

        assert out == [failed]
        assert len(errors) == 1
        assert "unidentified account" in errors[0]


# ---------------------------------------------------------------------------
# _fetch_funds_phase — end-to-end through the real phase function
# ---------------------------------------------------------------------------

class TestFundsPhaseFreezesDegradedAccount:
    @pytest.mark.asyncio
    async def test_degraded_account_with_lkg_still_contributes_real_cash(self):
        from backend.api.algo.nav import _fetch_funds_phase

        healthy = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0, "util option_premium": 0.0},
        ])
        failed = pd.DataFrame()
        failed.attrs["fetch_failed"] = True
        failed.attrs["account"] = "DH6847"

        lkg = pd.DataFrame([
            {"account": "DH6847", "avail opening_balance": 20000.0, "util option_premium": 0.0},
        ])
        lkg.attrs["stale"] = True
        lkg.attrs["stale_since"] = 1700000000.0

        accounts_in: list[str] = []
        errors: list[str] = []
        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.api.algo.nav.asyncio.to_thread",
            new=AsyncMock(return_value=[healthy, failed]),
        ), patch(
            "backend.brokers.broker_apis._stale_substitute_frame", return_value=lkg,
        ):
            total = await _fetch_funds_phase(accounts_in, errors)

        # Pre-fix this asserted 10000.0 (DH6847's leg silently zeroed).
        assert total == pytest.approx(30000.0), (
            "DH6847's LKG cash must still be summed into the NAV cash "
            "term — silently dropping it understates firm NAV"
        )
        assert "DH6847" in set(accounts_in)
        assert any("DH6847" in e for e in errors)

    @pytest.mark.asyncio
    async def test_degraded_account_with_no_lkg_excludes_leg_but_flags_error(self):
        from backend.api.algo.nav import _fetch_funds_phase

        healthy = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0, "util option_premium": 0.0},
        ])
        failed = pd.DataFrame()
        failed.attrs["fetch_failed"] = True
        failed.attrs["account"] = "DH6847"

        no_lkg = pd.DataFrame()
        no_lkg.attrs["fetch_failed"] = True
        no_lkg.attrs["account"] = "DH6847"

        accounts_in: list[str] = []
        errors: list[str] = []
        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.api.algo.nav.asyncio.to_thread",
            new=AsyncMock(return_value=[healthy, failed]),
        ), patch(
            "backend.brokers.broker_apis._stale_substitute_frame", return_value=no_lkg,
        ):
            total = await _fetch_funds_phase(accounts_in, errors)

        assert total == pytest.approx(10000.0)
        assert "DH6847" not in set(accounts_in)
        # Never silent — errors is populated even though we can't recover data.
        assert any("DH6847" in e for e in errors)


# ---------------------------------------------------------------------------
# _recover_missing_margins_accounts — boundary-agnostic (attrs-independent)
# margins recovery. Verified 2026-09-27: under RAMBOQ_USE_CONN_SERVICE=1
# (prod), an empty fetch_failed=True per-account margins frame crosses the
# conn_service UDS boundary and DataFrame.attrs is not guaranteed to survive
# serialization — this is the ONE case _substitute_degraded_frames (attrs-
# based) cannot see at all. This helper works from expected-vs-actual
# ACCOUNT PRESENCE instead, closing the gap regardless of the attrs outcome.
# ---------------------------------------------------------------------------

class TestFundsPhaseRecoversAccountsMissingAttrsEntirely:
    @pytest.mark.asyncio
    async def test_account_absent_with_zero_attrs_trace_is_recovered_via_lkg(self):
        """Post-UDS shape: fetch_margins() returns a frame for the healthy
        account and an EMPTY frame with NO attrs at all for the failed one
        (simulating attrs lost crossing the conn_service RPC boundary) —
        _substitute_degraded_frames has nothing to detect here since
        there's no fetch_failed attr to find. _recover_missing_margins_
        accounts must still catch it via the expected-accounts diff."""
        from backend.api.algo.nav import _fetch_funds_phase

        healthy = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0, "util option_premium": 0.0},
        ])
        attrs_lost = pd.DataFrame()  # empty, but attrs == {} — NOT tagged fetch_failed

        lkg = pd.DataFrame([
            {"account": "DH6847", "avail opening_balance": 20000.0, "util option_premium": 0.0},
        ])
        lkg.attrs["stale"] = True

        accounts_in: list[str] = []
        errors: list[str] = []
        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.api.algo.nav.asyncio.to_thread",
            new=AsyncMock(return_value=[healthy, attrs_lost]),
        ), patch(
            "backend.brokers.broker_apis._stale_substitute_frame", return_value=lkg,
        ) as mock_sub:
            total = await _fetch_funds_phase(
                accounts_in, errors, expected_accounts=["ZG0790", "DH6847"],
            )

        mock_sub.assert_called_once_with("margins", "DH6847")
        assert total == pytest.approx(30000.0), (
            "DH6847's LKG cash must be recovered even with zero attrs trace"
        )
        assert "DH6847" in set(accounts_in)
        assert any("DH6847" in e for e in errors)

    @pytest.mark.asyncio
    async def test_account_absent_with_zero_attrs_and_no_lkg_is_understated(self):
        from backend.api.algo.nav import _fetch_funds_phase

        healthy = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0, "util option_premium": 0.0},
        ])
        attrs_lost = pd.DataFrame()

        no_lkg = pd.DataFrame()
        no_lkg.attrs["fetch_failed"] = True

        accounts_in: list[str] = []
        errors: list[str] = []
        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.api.algo.nav.asyncio.to_thread",
            new=AsyncMock(return_value=[healthy, attrs_lost]),
        ), patch(
            "backend.brokers.broker_apis._stale_substitute_frame", return_value=no_lkg,
        ):
            total = await _fetch_funds_phase(
                accounts_in, errors, expected_accounts=["ZG0790", "DH6847"],
            )

        assert total == pytest.approx(10000.0)
        assert any(e.startswith("UNDERSTATED:") and "DH6847" in e for e in errors)

    @pytest.mark.asyncio
    async def test_no_expected_accounts_is_a_no_op(self):
        """Backward compatible: existing/other callers that don't resolve
        an expected-accounts list (expected_accounts=None, the default)
        must see zero behaviour change from this recovery pass."""
        from backend.api.algo.nav import _fetch_funds_phase

        healthy = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0, "util option_premium": 0.0},
        ])
        accounts_in: list[str] = []
        errors: list[str] = []
        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.api.algo.nav.asyncio.to_thread",
            new=AsyncMock(return_value=[healthy]),
        ):
            total = await _fetch_funds_phase(accounts_in, errors)

        assert total == pytest.approx(10000.0)
        assert errors == []

    @pytest.mark.asyncio
    async def test_cached_branch_also_recovers_missing_account(self):
        """The _peek('funds') closed-hours branch has the identical hole
        — a no-LKG account is simply absent from cached_funds.rows, with
        no stale_accounts entry either."""
        from backend.api.algo.nav import _fetch_funds_phase

        row1 = MagicMock(); row1.account = "ZG0790"; row1.cash = 10000.0; row1.option_premium = 0.0
        cached = MagicMock(rows=[row1], stale_accounts=[])

        lkg = pd.DataFrame([
            {"account": "DH6847", "avail opening_balance": 5000.0, "util option_premium": 0.0},
        ])
        lkg.attrs["stale"] = True

        accounts_in: list[str] = []
        errors: list[str] = []
        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=False,
        ), patch(
            "backend.api.cache.peek", return_value=cached,
        ), patch(
            "backend.brokers.broker_apis._stale_substitute_frame", return_value=lkg,
        ) as mock_sub:
            total = await _fetch_funds_phase(
                accounts_in, errors, expected_accounts=["ZG0790", "DH6847"],
            )

        mock_sub.assert_called_once_with("margins", "DH6847")
        assert total == pytest.approx(15000.0)
        assert "DH6847" in set(accounts_in)


# ---------------------------------------------------------------------------
# compute_firm_nav — the concrete scenario from the bug report
# ---------------------------------------------------------------------------

class TestComputeFirmNavFreezesOnDegradedAccount:
    @pytest.mark.asyncio
    async def test_one_account_margins_timeout_freezes_not_zeroes(self):
        """Concrete failure scenario from the bug report: one account's
        margins call fails once (transient timeout). Pre-fix, that
        account's cash leg silently became 0 with errors == []. Post-fix,
        it freezes to LKG and errors is populated."""
        from backend.api.algo.nav import compute_firm_nav

        healthy_funds = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0, "util option_premium": 0.0},
        ])
        failed_funds = pd.DataFrame()
        failed_funds.attrs["fetch_failed"] = True
        failed_funds.attrs["account"] = "DH6847"

        lkg_funds = pd.DataFrame([
            {"account": "DH6847", "avail opening_balance": 20000.0, "util option_premium": 0.0},
        ])
        lkg_funds.attrs["stale"] = True
        lkg_funds.attrs["stale_since"] = 1700000000.0

        positions_df = pd.DataFrame([
            {"account": "ZG0790", "symbol": "NIFTY50", "quantity": 0.0, "unrealised": 0.0, "realised": 0.0},
        ])
        holdings_df = pd.DataFrame([])

        with patch(
            "backend.api.algo.nav._resolve_conn_keys",
            new=AsyncMock(return_value=["ZG0790", "DH6847"]),
        ), patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins",
            return_value=[healthy_funds, failed_funds],
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", return_value=[positions_df],
        ), patch(
            "backend.api.helpers.snapshot_gate.is_exchange_closed_now", return_value=False,
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings", return_value=[holdings_df],
        ), patch(
            "backend.brokers.kite_ticker.get_ticker",
            return_value=MagicMock(get_ltp_by_sym=MagicMock(return_value=None)),
        ), patch(
            "backend.brokers.broker_apis._stale_substitute_frame", return_value=lkg_funds,
        ):
            snap = await compute_firm_nav()

        assert snap["cash_total"] == pytest.approx(30000.0), (
            "DH6847's LKG cash leg must not silently collapse to 0"
        )
        assert snap["nav"] == pytest.approx(30000.0)
        assert snap["errors"], "a degraded fetch must populate errors, never stay silent"
        assert any("DH6847" in e for e in snap["errors"])


# ---------------------------------------------------------------------------
# write_nav_snapshot — never poison an already-clean persisted row
# ---------------------------------------------------------------------------

def _mock_session_for_select(select_return, execute_side_effect=None):
    mock_session = AsyncMock()
    if execute_side_effect is not None:
        mock_session.execute = AsyncMock(side_effect=execute_side_effect)
    else:
        mock_session.execute = AsyncMock(return_value=select_return)
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


class TestWriteNavSnapshotNeverPoisonsCleanRow:
    """2026-09-27 council audit refinement: the gate is purely on
    `snap["understated"]`, not on whether the write would overwrite an
    existing clean row. An LKG-substituted-but-correct degradation
    (`errors` non-empty, `understated` empty) must still write normally —
    only a genuinely understated leg (no LKG anywhere) skips the write,
    even for the day's first-ever snapshot (the common case for the
    scheduled 23:45 IST compute, which has no earlier row to compare
    against)."""

    @pytest.mark.asyncio
    async def test_understated_snapshot_skips_write_even_with_no_existing_row(self):
        from backend.api.algo.nav import write_nav_snapshot
        from datetime import date as _date

        understated_snap = {
            "nav": 5000.0, "cash_total": 5000.0, "positions_mtm": 0.0,
            "holdings_mtm": 0.0, "accounts": ["ZG0790"],
            "errors": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
            "understated": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
            "by_account": {},
        }
        mock_session = _mock_session_for_select(MagicMock())

        with patch(
            "backend.api.algo.nav.compute_firm_nav",
            new=AsyncMock(return_value=understated_snap),
        ), patch(
            "backend.api.database.async_session", return_value=mock_session,
        ):
            result = await write_nav_snapshot(target_date=_date(2026, 9, 27))

        assert result["skipped_write"] is True
        # No DB round-trip of any kind — not a SELECT, not an upsert.
        mock_session.execute.assert_not_called()
        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_lkg_substituted_degradation_still_writes_normally(self):
        """errors non-empty (LKG-substituted account) but understated
        empty — the number is trustworthy, so the write must proceed."""
        from backend.api.algo.nav import write_nav_snapshot
        from datetime import date as _date

        lkg_snap = {
            "nav": 5000.0, "cash_total": 5000.0, "positions_mtm": 0.0,
            "holdings_mtm": 0.0, "accounts": ["ZG0790", "DH6847"],
            "errors": ["margins: DH6847 degraded — served last-known-good"],
            "understated": [],
            "by_account": {},
        }
        mock_session = _mock_session_for_select(MagicMock())

        with patch(
            "backend.api.algo.nav.compute_firm_nav",
            new=AsyncMock(return_value=lkg_snap),
        ), patch(
            "backend.api.database.async_session", return_value=mock_session,
        ):
            result = await write_nav_snapshot(target_date=_date(2026, 9, 27))

        assert "skipped_write" not in result
        mock_session.execute.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_clean_snapshot_writes_normally(self):
        from backend.api.algo.nav import write_nav_snapshot
        from datetime import date as _date

        clean_snap = {
            "nav": 5000.0, "cash_total": 5000.0, "positions_mtm": 0.0,
            "holdings_mtm": 0.0, "accounts": ["ZG0790"],
            "errors": [], "understated": [], "by_account": {},
        }
        mock_session = _mock_session_for_select(MagicMock())

        with patch(
            "backend.api.algo.nav.compute_firm_nav",
            new=AsyncMock(return_value=clean_snap),
        ), patch(
            "backend.api.database.async_session", return_value=mock_session,
        ):
            await write_nav_snapshot(target_date=_date(2026, 9, 27))

        mock_session.execute.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_force_true_writes_understated_snapshot_instead_of_skipping(self):
        """2026-09-27 council audit, second-pass refinement: a permanent
        write-skip has no backfill path and a short (23:45-midnight)
        retry window — force=True (the last-ditch call before IST
        midnight) must write the degraded row rather than lose the day
        entirely, with the note marked FORCED for operator visibility."""
        from backend.api.algo.nav import write_nav_snapshot
        from datetime import date as _date

        understated_snap = {
            "nav": 5000.0, "cash_total": 5000.0, "positions_mtm": 0.0,
            "holdings_mtm": 0.0, "accounts": ["ZG0790"],
            "errors": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
            "understated": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
            "by_account": {},
        }
        mock_session = _mock_session_for_select(MagicMock())

        with patch(
            "backend.api.algo.nav.compute_firm_nav",
            new=AsyncMock(return_value=understated_snap),
        ), patch(
            "backend.api.database.async_session", return_value=mock_session,
        ):
            result = await write_nav_snapshot(target_date=_date(2026, 9, 27), force=True)

        assert "skipped_write" not in result
        mock_session.execute.assert_called_once()
        mock_session.commit.assert_awaited_once()
        written_note = mock_session.execute.call_args[0][0].compile().params["note"]
        assert "FORCED" in written_note
        assert "DH6847" in written_note


class TestComputeFirmNavUnderstatedField:
    @pytest.mark.asyncio
    async def test_lkg_substitution_not_understated(self):
        """The core LKG-available scenario must NOT appear in
        `understated` — the number is correct, just slightly old."""
        from backend.api.algo.nav import compute_firm_nav

        healthy_funds = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0, "util option_premium": 0.0},
        ])
        failed_funds = pd.DataFrame()
        failed_funds.attrs["fetch_failed"] = True
        failed_funds.attrs["account"] = "DH6847"

        lkg_funds = pd.DataFrame([
            {"account": "DH6847", "avail opening_balance": 20000.0, "util option_premium": 0.0},
        ])
        lkg_funds.attrs["stale"] = True

        with patch(
            "backend.api.algo.nav._resolve_conn_keys",
            new=AsyncMock(return_value=["ZG0790", "DH6847"]),
        ), patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins",
            return_value=[healthy_funds, failed_funds],
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", return_value=[pd.DataFrame()],
        ), patch(
            "backend.api.helpers.snapshot_gate.is_exchange_closed_now", return_value=False,
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings", return_value=[pd.DataFrame()],
        ), patch(
            "backend.brokers.kite_ticker.get_ticker",
            return_value=MagicMock(get_ltp_by_sym=MagicMock(return_value=None)),
        ), patch(
            "backend.brokers.broker_apis._stale_substitute_frame", return_value=lkg_funds,
        ):
            snap = await compute_firm_nav()

        assert snap["errors"], "must still surface the degradation"
        assert snap["understated"] == [], (
            "an LKG-substituted account is NOT understated — its "
            "contributed value is correct"
        )

    @pytest.mark.asyncio
    async def test_no_lkg_available_is_understated(self):
        from backend.api.algo.nav import compute_firm_nav

        healthy_funds = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 10000.0, "util option_premium": 0.0},
        ])
        failed_funds = pd.DataFrame()
        failed_funds.attrs["fetch_failed"] = True
        failed_funds.attrs["account"] = "DH6847"

        no_lkg = pd.DataFrame()
        no_lkg.attrs["fetch_failed"] = True
        no_lkg.attrs["account"] = "DH6847"

        with patch(
            "backend.api.algo.nav._resolve_conn_keys",
            new=AsyncMock(return_value=["ZG0790", "DH6847"]),
        ), patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins",
            return_value=[healthy_funds, failed_funds],
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", return_value=[pd.DataFrame()],
        ), patch(
            "backend.api.helpers.snapshot_gate.is_exchange_closed_now", return_value=False,
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings", return_value=[pd.DataFrame()],
        ), patch(
            "backend.brokers.kite_ticker.get_ticker",
            return_value=MagicMock(get_ltp_by_sym=MagicMock(return_value=None)),
        ), patch(
            "backend.brokers.broker_apis._stale_substitute_frame", return_value=no_lkg,
        ):
            snap = await compute_firm_nav()

        assert len(snap["understated"]) == 1
        assert "DH6847" in snap["understated"][0]

    @pytest.mark.asyncio
    async def test_holdings_snapshot_none_is_visible_but_not_understated(self):
        """The scheduled 23:45 IST compute path (NSE closed) —
        `_holdings_snapshot()` returning None (DB failure OR genuinely
        zero holdings — conflated at the source) must be VISIBLE via
        `errors` but must NOT block the write (`understated` stays
        empty): tagging it would also block every day for a genuinely
        zero-holdings firm, and a REAL DB outage at write time already
        fails the upsert itself, which retries via the existing
        exception path in `_run_nav_compute_once`."""
        from backend.api.algo.nav import compute_firm_nav

        with patch(
            "backend.api.algo.nav._resolve_conn_keys",
            new=AsyncMock(return_value=["ZG0790"]),
        ), patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=False,
        ), patch(
            "backend.api.cache.peek", return_value=None,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins",
            return_value=[pd.DataFrame([{"account": "ZG0790", "avail opening_balance": 0.0}])],
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", return_value=[pd.DataFrame()],
        ), patch(
            "backend.api.helpers.snapshot_gate.is_exchange_closed_now", return_value=True,
        ), patch(
            "backend.api.routes.holdings._holdings_snapshot",
            new=AsyncMock(return_value=None),
        ), patch(
            "backend.brokers.kite_ticker.get_ticker",
            return_value=MagicMock(get_ltp_by_sym=MagicMock(return_value=None)),
        ):
            snap = await compute_firm_nav()

        assert any("holdings_snapshot" in e for e in snap["errors"])
        assert snap["understated"] == []

    @pytest.mark.asyncio
    async def test_holdings_snapshot_processing_exception_is_understated(self):
        """A genuine exception escaping the row-processing loop (not the
        conflated None case) DOES mean the total is wrong — must be
        tagged understated."""
        from backend.api.algo.nav import compute_firm_nav

        bad_snap = MagicMock()

        # Plain MagicMock attr access never raises — force the failure
        # inside the loop via a rows object that raises when iterated.
        class _BoomRows:
            def __iter__(self):
                raise RuntimeError("boom")
        bad_snap.rows = _BoomRows()

        with patch(
            "backend.api.algo.nav._resolve_conn_keys",
            new=AsyncMock(return_value=["ZG0790"]),
        ), patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=False,
        ), patch(
            "backend.api.cache.peek", return_value=None,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins",
            return_value=[pd.DataFrame([{"account": "ZG0790", "avail opening_balance": 0.0}])],
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", return_value=[pd.DataFrame()],
        ), patch(
            "backend.api.helpers.snapshot_gate.is_exchange_closed_now", return_value=True,
        ), patch(
            "backend.api.routes.holdings._holdings_snapshot",
            new=AsyncMock(return_value=bad_snap),
        ), patch(
            "backend.brokers.kite_ticker.get_ticker",
            return_value=MagicMock(get_ltp_by_sym=MagicMock(return_value=None)),
        ):
            snap = await compute_firm_nav()

        assert any("holdings_snapshot" in e for e in snap["understated"])
