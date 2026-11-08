"""
Tests for market-hours guards in the chase system.

Coverage:
  C3 — chase_order: no segment open → ChaseResult.status=FAILED
  C4 — _place_order: lot_size=0 raises ValueError
  C5 — _emit_chase_terminal exception logged at WARNING level

Five quality dimensions checked per test:
  SSOT   — single canonical return path per guard
  Perf   — guards short-circuit before broker/DB calls
  Stale  — existing non-guard paths continue unchanged
  Reuse  — shared mock builders across test cases
  UX     — error strings are operator-readable (contain exchange name)

C1/C2 (`apply_template_to_order`/`apply_plan_live` market-hours guards)
removed (Bracket/Template-GTT removal, T3.1, 2026-11) — both lived in
`backend.api.algo.template_attach`, deleted.
"""

from __future__ import annotations

import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.algo.chase import (
    ChaseConfig,
    ChaseResult,
    ChaseStatus,
    _emit_chase_terminal,
    _place_order,
    chase_order,
)


# ── Shared helpers ─────────────────────────────────────────────────────────


def _make_cfg(exchange: str = "NFO") -> ChaseConfig:
    return ChaseConfig(exchange=exchange)


# TestTemplateHasWing, TestApplyTemplateToOrderMarketHoursGuard and
# TestApplyPlanLiveMarketHoursGuard (C1/C2) removed (Bracket/
# Template-GTT removal, T3.1, 2026-11) — all covered
# backend.api.algo.template_attach, deleted. C3/C4/C5 below (chase.py)
# are unaffected.


# ── C3: chase_order market-hours pre-flight ───────────────────────────────

class TestChaseOrderMarketHoursPreFlight:
    """C3 — market-hours check at the top of chase_order.

    C3 uses _symbol_exchange_open(cfg.exchange, _build_now_ctx()) from
    agent_engine (not is_any_segment_open) so tests patch those two names.
    """

    @pytest.mark.asyncio
    async def test_all_segments_closed_returns_failed(self):
        """SSOT: exchange closed → ChaseResult.status == FAILED."""
        with patch(
            "backend.shared.helpers.utils.is_prod_branch",
            return_value=True,
        ), patch(
            "backend.api.algo.agent_engine._symbol_exchange_open",
            return_value=False,
        ), patch(
            "backend.api.algo.agent_engine._build_now_ctx",
            return_value={},
        ):
            result = await chase_order(
                account="ACC1",
                symbol="NIFTY25APR22000CE",
                transaction_type="SELL",
                quantity=1,
                cfg=_make_cfg("NFO"),
            )

        assert result.status == ChaseStatus.FAILED
        assert "closed" in result.detail.lower()

    @pytest.mark.asyncio
    async def test_closed_market_detail_names_exchange(self):
        """UX: detail includes the exchange name from cfg.exchange."""
        with patch(
            "backend.shared.helpers.utils.is_prod_branch",
            return_value=True,
        ), patch(
            "backend.api.algo.agent_engine._symbol_exchange_open",
            return_value=False,
        ), patch(
            "backend.api.algo.agent_engine._build_now_ctx",
            return_value={},
        ):
            result = await chase_order(
                account="ACC1",
                symbol="CRUDEOIL25JULFUT",
                transaction_type="BUY",
                quantity=1,
                cfg=_make_cfg("MCX"),
            )

        assert "MCX" in result.detail, f"Expected MCX in detail, got: {result.detail}"

    @pytest.mark.asyncio
    async def test_closed_market_no_broker_call(self):
        """Perf: broker depth/quote never fetched when market closed."""
        mock_depth = MagicMock()

        with patch(
            "backend.shared.helpers.utils.is_prod_branch",
            return_value=True,
        ), patch(
            "backend.api.algo.agent_engine._symbol_exchange_open",
            return_value=False,
        ), patch(
            "backend.api.algo.agent_engine._build_now_ctx",
            return_value={},
        ), patch(
            "backend.api.algo.chase._get_depth",
            mock_depth,
        ):
            await chase_order(
                account="ACC1",
                symbol="NIFTY25APR22000CE",
                transaction_type="SELL",
                quantity=1,
                cfg=_make_cfg("NFO"),
            )

        assert not mock_depth.called, "_get_depth must not be called on closed market"

    @pytest.mark.asyncio
    async def test_non_prod_branch_still_blocks_before_market_check(self):
        """Stale: non-prod branch guard fires before market-hours guard."""
        with patch(
            "backend.shared.helpers.utils.is_prod_branch",
            return_value=False,
        ) as mock_prod, patch(
            "backend.api.algo.agent_engine._symbol_exchange_open",
        ) as mock_open:
            result = await chase_order(
                account="ACC1",
                symbol="SYM",
                transaction_type="BUY",
                quantity=1,
            )

        assert result.status == ChaseStatus.CANCELLED
        # Market-hours check must NOT run (is_prod_branch fires first)
        mock_open.assert_not_called()


# ── C4: _place_order lot_size=0 guard ─────────────────────────────────────

class TestPlaceOrderLotSizeZeroGuard:
    """C4 — ValueError on lot_size=0 before normalise_qty."""

    def test_lot_size_zero_raises_value_error(self):
        """SSOT: lot_size=0 raises ValueError before normalise_qty is called."""
        mock_broker = MagicMock()
        cfg = _make_cfg("MCX")

        with patch(
            "backend.api.algo.chase._get_broker",
            return_value=mock_broker,
        ), patch(
            "backend.api.algo.chase._lot_size_sync",
            return_value=0,
        ):
            with pytest.raises(ValueError, match="lot_size=0"):
                _place_order("ACC1", "CRUDEOIL25JULFUT", "BUY", 1, 5000.0, cfg)

    def test_lot_size_zero_normalise_qty_not_called(self):
        """Perf: normalise_qty must never be called when lot_size=0."""
        mock_broker = MagicMock()
        cfg = _make_cfg("MCX")

        with patch(
            "backend.api.algo.chase._get_broker",
            return_value=mock_broker,
        ), patch(
            "backend.api.algo.chase._lot_size_sync",
            return_value=0,
        ):
            try:
                _place_order("ACC1", "CRUDEOIL25JULFUT", "BUY", 1, 5000.0, cfg)
            except ValueError:
                pass

        assert not mock_broker.normalise_qty.called, (
            "normalise_qty must not be called when lot_size=0"
        )

    def test_lot_size_positive_proceeds_normally(self):
        """Stale: positive lot_size → normalise_qty called, order placed."""
        mock_broker = MagicMock()
        mock_broker.normalise_qty.return_value = 1
        mock_broker.place_order.return_value = "order-001"
        cfg = _make_cfg("MCX")

        with patch(
            "backend.api.algo.chase._get_broker",
            return_value=mock_broker,
        ), patch(
            "backend.api.algo.chase._lot_size_sync",
            return_value=100,
        ):
            order_id = _place_order("ACC1", "CRUDEOIL25JULFUT", "BUY", 1, 5000.0, cfg)

        assert order_id == "order-001"
        assert mock_broker.normalise_qty.called

    def test_lot_size_zero_error_includes_symbol_and_exchange(self):
        """UX: error message includes exchange and symbol for operator diagnosis."""
        mock_broker = MagicMock()
        cfg = _make_cfg("MCX")

        with patch(
            "backend.api.algo.chase._get_broker",
            return_value=mock_broker,
        ), patch(
            "backend.api.algo.chase._lot_size_sync",
            return_value=0,
        ):
            with pytest.raises(ValueError) as exc_info:
                _place_order("ACC1", "CRUDEOIL25JULFUT", "BUY", 1, 5000.0, cfg)

        msg = str(exc_info.value)
        assert "MCX" in msg
        assert "CRUDEOIL25JULFUT" in msg


# ── C5: _emit_chase_terminal warning promotion ────────────────────────────

class _LogCapture(logging.Handler):
    """Lightweight log capture handler for loggers with propagate=False.

    get_logger() in this project sets propagate=False, which makes pytest
    caplog blind. We attach a handler directly to the module logger and
    collect records ourselves.
    """

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def warnings(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.levelno >= logging.WARNING]


class TestEmitChaseTerminalWarningPromotion:
    """C5 — exception in _emit_chase_terminal logged at WARNING not DEBUG."""

    @pytest.mark.asyncio
    async def test_exception_logged_at_warning_level(self):
        """SSOT: exception inside _emit_chase_terminal → WARNING, not DEBUG."""
        import backend.api.algo.chase as _chase_mod

        capture = _LogCapture()
        _chase_mod.logger.addHandler(capture)
        try:
            with patch(
                "backend.api.algo.chase._chase_terminal_update_db",
                side_effect=RuntimeError("simulated DB failure"),
            ):
                await _emit_chase_terminal(
                    broker_order_id="order-999",
                    outcome="chase_fill",
                    symbol="NIFTY25APR22000CE",
                    side="SELL",
                    qty=1,
                    final_price=10050.0,
                )
        finally:
            _chase_mod.logger.removeHandler(capture)

        warnings = capture.warnings()
        assert warnings, "Expected at least one WARNING-level record"
        msgs = [r.getMessage() for r in warnings]
        assert any("_emit_chase_terminal" in m for m in msgs), (
            f"Expected '_emit_chase_terminal' in a WARNING record, got: {msgs}"
        )

    @pytest.mark.asyncio
    async def test_exception_does_not_propagate(self):
        """SSOT: _emit_chase_terminal never raises to caller."""
        with patch(
            "backend.api.algo.chase._chase_terminal_update_db",
            side_effect=Exception("any error"),
        ):
            # Must not raise
            await _emit_chase_terminal(
                broker_order_id="order-999",
                outcome="chase_fill",
                symbol="SYM",
                side="BUY",
                qty=1,
            )

    @pytest.mark.asyncio
    async def test_exception_not_suppressed_at_debug_only(self):
        """Stale: DEBUG alone is insufficient — must appear at WARNING (≥30)."""
        import backend.api.algo.chase as _chase_mod

        capture = _LogCapture()
        _chase_mod.logger.addHandler(capture)
        try:
            with patch(
                "backend.api.algo.chase._chase_terminal_update_db",
                side_effect=RuntimeError("simulated failure"),
            ):
                await _emit_chase_terminal(
                    broker_order_id="order-123",
                    outcome="chase_cancelled",
                    symbol="SYM",
                    side="BUY",
                    qty=1,
                )
        finally:
            _chase_mod.logger.removeHandler(capture)

        warnings = capture.warnings()
        assert warnings, "Exception must be logged at WARNING or above, not only at DEBUG"
        msgs = [r.getMessage() for r in warnings]
        assert any("_emit_chase_terminal" in m for m in msgs)

    @pytest.mark.asyncio
    async def test_warning_includes_exc_info(self):
        """Reuse: exc_info=True is set so the traceback is visible in log output."""
        import backend.api.algo.chase as _chase_mod

        capture = _LogCapture()
        _chase_mod.logger.addHandler(capture)
        try:
            with patch(
                "backend.api.algo.chase._chase_terminal_update_db",
                side_effect=RuntimeError("traceback test"),
            ):
                await _emit_chase_terminal(
                    broker_order_id="order-xyz",
                    outcome="chase_failed",
                    symbol="SYM",
                    side="SELL",
                    qty=1,
                )
        finally:
            _chase_mod.logger.removeHandler(capture)

        warnings = capture.warnings()
        assert warnings
        # exc_info=True → record.exc_info is a non-None tuple
        assert any(r.exc_info is not None for r in warnings), (
            "WARNING record must carry exc_info so the traceback lands in the log"
        )
