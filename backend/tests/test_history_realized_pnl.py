"""Tests for `GET /api/admin/history/realized-pnl`
(`backend/api/routes/history.py:HistoryController.list_realized_pnl`) —
Part 2 of the Kite Console P&L reconciliation plan.

Wraps `scripts.kite_console_pnl_sync.get_accumulated_pnl()`. These tests
cover the pure flattener/filter helpers directly (no DB needed — the
canned `get_accumulated_pnl()`-shaped dict gives the flattening logic a
deterministic input) plus the controller method itself, invoked via its
underlying `.fn` (the plain, unbound async function Litestar's `@get`
decorator wraps) with `self=None` — the handler body never references
`self`, so no real `Controller`/app instantiation is needed, mirroring
this codebase's documented precedent for directly invoking a route
handler's `.fn` in a test (see CLAUDE.md's "MCX DST-aware non-agri
close time" entry for the `OptionsController` precedent).
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from backend.api.routes.history import (
    HistoryController,
    _RealizedPnlRow,
    _hist_realized_pnl_account_candidates,
    _hist_realized_pnl_rows,
)


# ---------------------------------------------------------------------------
# _hist_realized_pnl_account_candidates — the double-mask guard
# ---------------------------------------------------------------------------

class TestHistRealizedPnlAccountCandidates:
    def test_none_account_means_no_filter(self):
        assert _hist_realized_pnl_account_candidates(None) is None
        assert _hist_realized_pnl_account_candidates("") is None

    def test_raw_account_code_resolves_to_both_forms(self):
        from backend.shared.helpers.utils import register_accounts, mask_account
        register_accounts(["ZG0790"])
        masked = mask_account("ZG0790")
        candidates = _hist_realized_pnl_account_candidates("ZG0790")
        assert "ZG0790" in candidates
        assert masked in candidates

    def test_already_masked_account_is_never_double_masked(self):
        """The literal masked string ('Z1####') must itself be one of
        the candidates — a frontend round-tripping a displayed masked
        value must still match, without this function trying (and
        failing, since mask_account() is not idempotent) to re-mask it
        into a wrong third string."""
        candidates = _hist_realized_pnl_account_candidates("Z1####")
        assert "Z1####" in candidates

    def test_mask_account_failure_degrades_to_raw_only_never_raises(self, monkeypatch):
        import backend.shared.helpers.utils as u
        monkeypatch.setattr(u, "mask_account", lambda a: (_ for _ in ()).throw(RuntimeError("boom")))
        candidates = _hist_realized_pnl_account_candidates("ZG0790")
        assert candidates == {"ZG0790"}


# ---------------------------------------------------------------------------
# _hist_realized_pnl_rows — the pure flattener/filter
# ---------------------------------------------------------------------------

def _canned_result() -> dict:
    return {
        "groups": {
            ("Z1####", "commodities", "future", "GOLDM", date(2026, 12, 31)): 120000.0,
            ("Z1####", "derivatives", "option", "NIFTY", date(2026, 11, 27)): -5000.0,
            ("Z2####", "equity", "equity", "RELIANCE", None): 2500.0,
        },
        "by_account_root_expiry": {},
        "overall_total": 117500.0,
    }


class TestHistRealizedPnlRows:
    def test_flattens_every_group_with_no_filters(self):
        rows = _hist_realized_pnl_rows(_canned_result())
        assert len(rows) == 3
        assert all(isinstance(r, _RealizedPnlRow) for r in rows)

    def test_empty_groups_returns_empty_list(self):
        assert _hist_realized_pnl_rows({"groups": {}}) == []
        assert _hist_realized_pnl_rows({}) == []

    def test_expiry_rendered_as_iso_string_or_null(self):
        rows = _hist_realized_pnl_rows(_canned_result())
        by_root = {r.symbol_root: r for r in rows}
        assert by_root["GOLDM"].expiry == "2026-12-31"
        assert by_root["RELIANCE"].expiry is None

    def test_sorted_by_expiry_descending_nulls_last(self):
        rows = _hist_realized_pnl_rows(_canned_result())
        assert [r.symbol_root for r in rows] == ["GOLDM", "NIFTY", "RELIANCE"]

    def test_account_filter_matches_raw_or_masked_form(self):
        rows_masked = _hist_realized_pnl_rows(_canned_result(), account="Z1####")
        assert {r.symbol_root for r in rows_masked} == {"GOLDM", "NIFTY"}

        # A raw code that happens to mask to a DIFFERENT string than any
        # group here simply matches nothing — never raises.
        rows_none = _hist_realized_pnl_rows(_canned_result(), account="ZQ9999")
        assert rows_none == []

    def test_segment_filter(self):
        rows = _hist_realized_pnl_rows(_canned_result(), segment="equity")
        assert [r.symbol_root for r in rows] == ["RELIANCE"]

    def test_expiry_range_excludes_null_expiry_rows(self):
        rows = _hist_realized_pnl_rows(
            _canned_result(), expiry_from=date(2026, 1, 1), expiry_to=date(2026, 12, 31),
        )
        symbol_roots = {r.symbol_root for r in rows}
        assert symbol_roots == {"GOLDM", "NIFTY"}
        assert "RELIANCE" not in symbol_roots, (
            "a range filter implies a specific expiry window — an "
            "equity row with no expiry can never satisfy one"
        )

    def test_expiry_range_narrows_to_matching_window(self):
        rows = _hist_realized_pnl_rows(
            _canned_result(), expiry_from=date(2026, 12, 1), expiry_to=date(2026, 12, 31),
        )
        assert [r.symbol_root for r in rows] == ["GOLDM"]

    def test_no_range_set_includes_null_expiry_row(self):
        rows = _hist_realized_pnl_rows(_canned_result())
        assert "RELIANCE" in {r.symbol_root for r in rows}

    def test_combined_filters(self):
        rows = _hist_realized_pnl_rows(
            _canned_result(), account="Z1####", segment="derivatives",
        )
        assert [r.symbol_root for r in rows] == ["NIFTY"]


# ---------------------------------------------------------------------------
# Controller — guard identity + end-to-end flattening via the real
# handler function (self=None; the handler body never references self)
# ---------------------------------------------------------------------------

class TestListRealizedPnlEndpoint:
    def test_guarded_by_view_audit_cap_same_as_sibling_endpoints(self):
        """Same `cap_guard("view_audit")` as list_orders/list_trades/
        list_funds in this file — `cap_guard()` stamps a cap-specific
        `__name__` on its closure, which is the reliable way to confirm
        identity without relying on closure-object equality."""
        handler = HistoryController.list_realized_pnl
        assert len(handler.guards) == 1
        assert handler.guards[0].__name__ == "cap_guard__view_audit"
        # Matches every sibling GET in this file.
        for sibling in (
            HistoryController.list_orders, HistoryController.list_trades,
            HistoryController.list_funds,
        ):
            assert sibling.guards[0].__name__ == "cap_guard__view_audit"

    @pytest.mark.asyncio
    async def test_returns_a_bare_list_not_a_wrapper_object(self):
        canned = {
            "groups": {
                ("Z1####", "commodities", "future", "GOLDM", date(2026, 12, 31)): 120000.0,
            },
            "by_account_root_expiry": {}, "overall_total": 120000.0,
        }
        with patch("scripts.kite_console_pnl_sync.get_accumulated_pnl",
                   AsyncMock(return_value=canned)):
            result = await HistoryController.list_realized_pnl.fn(None)
        assert isinstance(result, list)
        assert result == [
            _RealizedPnlRow(
                account="Z1####", segment="commodities", instrument_type="future",
                symbol_root="GOLDM", expiry="2026-12-31", realized_pnl=120000.0,
            ),
        ]

    @pytest.mark.asyncio
    async def test_empty_result_case(self):
        with patch("scripts.kite_console_pnl_sync.get_accumulated_pnl",
                   AsyncMock(return_value={"groups": {}, "by_account_root_expiry": {}, "overall_total": 0.0})):
            result = await HistoryController.list_realized_pnl.fn(None)
        assert result == []

    @pytest.mark.asyncio
    async def test_instrument_type_and_symbol_root_forwarded_to_underlying_function(self):
        """`instrument_type`/`symbol_root` map onto `get_accumulated_pnl()`'s
        own kwargs directly. `account` must NEVER be forwarded — see
        `_hist_realized_pnl_account_candidates()`'s own docstring for the
        double-mask risk that would introduce."""
        mock_fn = AsyncMock(return_value={"groups": {}, "by_account_root_expiry": {}, "overall_total": 0.0})
        with patch("scripts.kite_console_pnl_sync.get_accumulated_pnl", mock_fn):
            await HistoryController.list_realized_pnl.fn(
                None, account="ZG0790", instrument_type="future", symbol_root="GOLDM",
            )
        mock_fn.assert_awaited_once_with(instrument_type="future", symbol_root="GOLDM", broker=None)

    @pytest.mark.asyncio
    async def test_expiry_from_to_strings_parsed_and_applied(self):
        canned = {
            "groups": {
                ("Z1####", "commodities", "future", "GOLDM", date(2026, 12, 31)): 120000.0,
                ("Z1####", "commodities", "future", "SILVER", date(2026, 6, 30)): 5000.0,
            },
            "by_account_root_expiry": {}, "overall_total": 125000.0,
        }
        with patch("scripts.kite_console_pnl_sync.get_accumulated_pnl",
                   AsyncMock(return_value=canned)):
            result = await HistoryController.list_realized_pnl.fn(
                None, expiry_from="2026-12-01", expiry_to="2026-12-31",
            )
        assert [r.symbol_root for r in result] == ["GOLDM"]
