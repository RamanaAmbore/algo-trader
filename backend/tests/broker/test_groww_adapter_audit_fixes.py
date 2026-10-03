"""
Regression tests for the 2026-10 Groww adapter audit fixes (groww.py).

Each test mocks the Groww SDK call with the REAL growwapi 1.5.0 method
signature (verified via `inspect.signature(...).bind()` against the
actually-installed SDK in this repo's venv) so a reintroduced wrong-arity
call fails loudly here exactly as it would against the live API — not a
permissive catch-all MagicMock that would silently re-mask the bug.

Items covered in this module so far:
  1. order_status() — real get_order_detail(segment, groww_order_id)
     binding + single-order (non-list) response shape normalisation,
     with the order_id -> segment cache for repeat polls.
  2. orders() — full pagination across every Groww segment (page_size
     is a no-op on the real SDK; `page` is only transmitted when
     `segment` is also passed).
  5. modify_order() — price-only modify preserves the order's existing
     quantity/order_type instead of defaulting to 0/LIMIT.
  6. cancel_gtt() OCO — a single-leg failure now raises instead of
     silently reporting success.
  7. modify_order() — broker-rejected response status is no longer
     discarded.
"""

from __future__ import annotations

import inspect

import pytest
from unittest.mock import MagicMock

from growwapi import GrowwAPI

from backend.brokers.adapters.groww import GrowwBroker


def _bound_get_order_detail(**kwargs) -> None:
    """Raise TypeError exactly as the real SDK would for a bad call."""
    inspect.signature(GrowwAPI.get_order_detail).bind(MagicMock(), **kwargs)


def _bound_get_order_list(**kwargs) -> None:
    inspect.signature(GrowwAPI.get_order_list).bind(MagicMock(), **kwargs)


def _bound_modify_order(**kwargs) -> None:
    inspect.signature(GrowwAPI.modify_order).bind(MagicMock(), **kwargs)


def _one_page_per_segment(rows_by_segment: dict[str, list[dict]]) -> MagicMock:
    """Build a `get_order_list` mock that returns `rows_by_segment[segment]`
    on page 0 and an empty page on page >= 1 for every segment — avoids
    orders()'s real pagination loop running its full 50-page safety cap
    per segment against a flat always-non-empty MagicMock."""
    def fake(page=0, segment=None, **_kw):
        _bound_get_order_list(page=page, segment=segment)
        if page == 0:
            return {"order_list": rows_by_segment.get(segment, [])}
        return {"order_list": []}
    return MagicMock(side_effect=fake)


@pytest.fixture
def broker():
    conn = MagicMock()
    conn.account = "GRW123"
    conn.get_groww_conn = MagicMock()
    return GrowwBroker(conn)


# ── Item 1: order_status() ──────────────────────────────────────────────


class TestOrderStatusRealBinding:
    def test_cold_path_scans_orders_and_populates_segment_cache(self, broker):
        """First poll of an order_id has no cached segment — resolves via
        a single orders() scan (same shape orders() itself returns) and
        caches the segment for next time."""
        broker.groww.get_order_list = _one_page_per_segment({
            "CASH": [{
                "groww_order_id": "ORD123",
                "trading_symbol": "SBIN",
                "exchange": "NSE",
                "order_status": "OPEN",
                "quantity": "10",
            }],
        })
        result = broker.order_status("ORD123")
        assert result["order_id"] == "ORD123"
        assert result["status"] == "OPEN"
        assert broker._order_segment_cache["ORD123"] == "CASH"

    def test_cold_path_not_found_returns_empty_dict(self, broker):
        broker.groww.get_order_list = MagicMock(return_value={"order_list": []})
        assert broker.order_status("UNKNOWN") == {}

    def test_warm_path_calls_get_order_detail_with_real_signature(self, broker):
        """Pre-fix: `get_order_detail(str(order_id))` — one positional
        arg. Real signature is `get_order_detail(segment, groww_order_id,
        timeout=None)`; binding with only one positional arg raises
        TypeError (missing groww_order_id as a 2nd positional, or missing
        segment if called the old way). This test fails exactly like the
        real SDK would if the adapter regresses to the old call shape."""
        broker._order_segment_cache["ORD123"] = "CASH"

        def fake_get_order_detail(**kwargs):
            _bound_get_order_detail(**kwargs)  # raises TypeError if malformed
            return {
                "groww_order_id": kwargs["groww_order_id"],
                "order_status": "COMPLETE",
                "quantity": "10",
                "trading_symbol": "SBIN",
                "exchange": "NSE",
            }

        broker.groww.get_order_detail = MagicMock(side_effect=fake_get_order_detail)
        result = broker.order_status("ORD123")
        assert result["status"] == "COMPLETE"
        broker.groww.get_order_detail.assert_called_once_with(
            segment="CASH", groww_order_id="ORD123"
        )

    def test_warm_path_single_order_payload_shape_normalises(self, broker):
        """Regression for the second half of bug #1: get_order_detail's
        real response has NO `data`/`order_list`/`orders` wrapper — just
        the order's fields directly (confirmed via
        `GrowwAPI._parse_response` source, which already unwraps
        `payload`). Routing this through the list-only `_normalise_orders`
        used to always return `[]`."""
        broker._order_segment_cache["ORD123"] = "CASH"
        broker.groww.get_order_detail = MagicMock(return_value={
            "groww_order_id": "ORD123",
            "order_status": "COMPLETE",
            "quantity": "10",
            "trading_symbol": "SBIN",
            "exchange": "NSE",
        })
        result = broker.order_status("ORD123")
        assert result != {}
        assert result["order_id"] == "ORD123"
        assert result["status"] == "COMPLETE"

    def test_warm_path_exception_drops_stale_cache_entry(self, broker):
        broker._order_segment_cache["ORD123"] = "CASH"
        broker.groww.get_order_detail = MagicMock(side_effect=Exception("boom"))
        result = broker.order_status("ORD123")
        assert result == {}
        assert "ORD123" not in broker._order_segment_cache


# ── Item 2: orders() pagination ─────────────────────────────────────────


class TestOrdersPagination:
    def test_paginates_across_all_segments_until_empty_page(self, broker):
        """Real SDK bug: `page` is only transmitted when `segment` is also
        passed, and `page_size` is never transmitted at all. The fix must
        loop every segment explicitly and page within each until a
        genuinely empty page comes back."""
        calls: list[tuple[int, str]] = []

        def fake_get_order_list(page=0, segment=None, **_kw):
            _bound_get_order_list(page=page, segment=segment)
            calls.append((page, segment))
            if segment == "FNO" and page in (0, 1):
                # Simulate > 1 page of orders on the FNO segment only —
                # the regression this fix targets (order #26+).
                return {"order_list": [
                    {"groww_order_id": f"FNO-{page}-{i}", "exchange": "NSE",
                     "order_status": "OPEN", "quantity": "1"}
                    for i in range(25)
                ]}
            return {"order_list": []}

        broker.groww.get_order_list = MagicMock(side_effect=fake_get_order_list)
        rows = broker.orders()

        # All 4 segments were queried (each needs `segment` truthy for
        # `page` to even be transmitted by the real SDK).
        queried_segments = {seg for _, seg in calls}
        assert queried_segments == {"CASH", "FNO", "COMMODITY", "CURRENCY"}
        # FNO paged twice (0 and 1) before an empty page stopped it.
        fno_pages = sorted(p for p, s in calls if s == "FNO")
        assert fno_pages == [0, 1, 2]
        # 50 orders recovered from FNO alone — would have been capped at
        # ~25 (or even 0, given the no-segment bug) pre-fix.
        assert len(rows) == 50

    def test_empty_book_returns_empty_list_without_hanging(self, broker):
        broker.groww.get_order_list = MagicMock(return_value={"order_list": []})
        assert broker.orders() == []
        # Exactly one page probed per segment, not the full 50-page cap.
        assert broker.groww.get_order_list.call_count == 4


# ── Item 5: modify_order() price-only qty/order_type preservation ───────


class TestModifyOrderPreservesQuantityAndType:
    def test_price_only_modify_preserves_quantity_and_order_type(self, broker):
        """Pre-fix: omitting quantity/order_type on a price-only modify
        sent `quantity=0` (zeroing the order) and silently forced
        `order_type=LIMIT` regardless of the order's real type."""
        broker.groww.get_order_list = _one_page_per_segment({
            "COMMODITY": [{
                "groww_order_id": "ORD123",
                "exchange": "MCX",
                "order_status": "OPEN",
                "quantity": "50",
                "order_type": "SL",
            }],
        })
        captured = {}

        def fake_modify_order(**kwargs):
            _bound_modify_order(**kwargs)
            captured.update(kwargs)
            return {"status": "SUCCESS"}

        broker.groww.modify_order = MagicMock(side_effect=fake_modify_order)
        broker.modify_order("ORD123", price=105.5)

        assert captured["quantity"] == 50
        assert captured["order_type"] == "SL"
        assert captured["price"] == 105.5

    def test_explicit_quantity_and_order_type_are_honoured(self, broker):
        """When the caller DOES supply quantity/order_type, no lookup is
        needed and the explicit values pass through unchanged."""
        broker.groww.modify_order = MagicMock(return_value={"status": "SUCCESS"})
        broker.modify_order(
            "ORD123", exchange="NSE", quantity=20, order_type="LIMIT", price=100,
        )
        kwargs = broker.groww.modify_order.call_args.kwargs
        assert kwargs["quantity"] == 20
        assert kwargs["order_type"] == "LIMIT"
        # No orders() scan needed when exchange+quantity+order_type all given.
        assert not hasattr(broker.groww, "get_order_list") or \
            not broker.groww.get_order_list.called


# ── Item 6: cancel_gtt() OCO partial-failure reporting ───────────────────


class TestCancelGttOcoPartialFailure:
    def test_single_leg_failure_raises_not_silently_succeeds(self, broker):
        """Pre-fix: leg0 fails, leg1 succeeds -> fell through to `return
        gtt_id` (success) since only 'both failed' raised. One leg was
        left live on the book with no signal to the caller."""
        calls = []

        def fake_cancel_gtt(leg_id, *, exchange=None):
            calls.append(leg_id)
            if leg_id == "leg0":
                raise RuntimeError("leg0 broker rejection")
            return leg_id

        broker.cancel_gtt = MagicMock(side_effect=fake_cancel_gtt)
        # Call the REAL bound method (not the mock) by invoking it via
        # the class, since we replaced the instance attribute above only
        # to intercept the recursive single-leg calls.
        real_cancel_gtt = GrowwBroker.cancel_gtt.__wrapped__ \
            if hasattr(GrowwBroker.cancel_gtt, "__wrapped__") else GrowwBroker.cancel_gtt

        with pytest.raises(RuntimeError, match="leg0.*still live|leg0 broker rejection"):
            real_cancel_gtt(broker, "oco:leg0+leg1", exchange="MCX")
        assert set(calls) == {"leg0", "leg1"}

    def test_both_legs_succeed_returns_normally(self, broker):
        def fake_cancel_gtt(leg_id, *, exchange=None):
            return leg_id

        broker.cancel_gtt = MagicMock(side_effect=fake_cancel_gtt)
        real_cancel_gtt = GrowwBroker.cancel_gtt.__wrapped__ \
            if hasattr(GrowwBroker.cancel_gtt, "__wrapped__") else GrowwBroker.cancel_gtt
        result = real_cancel_gtt(broker, "oco:leg0+leg1", exchange="MCX")
        assert result == "oco:leg0+leg1"

    def test_both_legs_fail_raises_both_failed_message(self, broker):
        def fake_cancel_gtt(leg_id, *, exchange=None):
            raise RuntimeError(f"{leg_id} broker rejection")

        broker.cancel_gtt = MagicMock(side_effect=fake_cancel_gtt)
        real_cancel_gtt = GrowwBroker.cancel_gtt.__wrapped__ \
            if hasattr(GrowwBroker.cancel_gtt, "__wrapped__") else GrowwBroker.cancel_gtt
        with pytest.raises(RuntimeError, match="both legs failed"):
            real_cancel_gtt(broker, "oco:leg0+leg1", exchange="MCX")


# ── Item 7: modify_order()/cancel_order() surfacing broker rejection ─────


class TestModifyCancelOrderSurfaceRejection:
    def test_modify_order_raises_on_broker_error_status(self, broker):
        broker.groww.modify_order = MagicMock(return_value={"status": "ERROR",
                                                              "message": "bad price"})
        with pytest.raises(RuntimeError, match="rejected"):
            broker.modify_order("ORD123", exchange="NSE", quantity=10,
                                 order_type="LIMIT", price=100)

    def test_modify_order_succeeds_on_success_status(self, broker):
        broker.groww.modify_order = MagicMock(return_value={"status": "SUCCESS"})
        result = broker.modify_order("ORD123", exchange="NSE", quantity=10,
                                      order_type="LIMIT", price=100)
        assert result == "ORD123"

    def test_cancel_order_raises_on_broker_error_status(self, broker):
        broker.groww.cancel_order = MagicMock(return_value={"status": "ERROR",
                                                              "message": "already filled"})
        with pytest.raises(RuntimeError, match="rejected"):
            broker.cancel_order("ORD123", exchange="NSE")

    def test_cancel_order_succeeds_on_success_status(self, broker):
        broker.groww.cancel_order = MagicMock(return_value={"status": "SUCCESS"})
        result = broker.cancel_order("ORD123", exchange="NSE")
        assert result == "ORD123"
