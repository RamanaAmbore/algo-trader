"""
Tests for three order rejection fixes from the plan:
1. MCX G1 LOT_MULTIPLE skip — should NOT fire for MCX/NCO broker qty
2. ntfy priority=None header — should NOT include "None" string in X-Priority
3. MCX GTT ceiling config — should read from backend_config.yaml, not hard-coded 50

Covers SSOT, correctness, edge cases, and config override behavior.
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch
from zoneinfo import ZoneInfo


# =============================================================================
# Test 1: MCX G1 LOT_MULTIPLE skip for MCX/NCO close positions (broker qty in LOTS)
# =============================================================================

class TestMcxG1SkipForClose:
    """
    MCX broker returns position qty in LOTS (e.g., 50 lots).
    A close order with qty=50 lots should NOT trigger G1 LOT_MULTIPLE,
    since 50 is already a valid whole-lot quantity for MCX (lot_size=100).

    Testing the fix: _preflight_validate_lots should skip LOT_MULTIPLE check
    for MCX/NCO when they're being closed (qty is already in lots from broker).
    """

    @pytest.mark.asyncio
    async def test_mcx_close_position_qty_50_lots_passes_g1(self):
        """MCX close: qty=50 lots (broker qty) with lot_size=100 → G1 skipped."""
        from backend.api.algo.actions import run_preflight

        broker = MagicMock()
        broker.profile.return_value = {
            "exchanges": ["NSE", "NFO", "BSE", "MCX", "NCO", "CDS"]
        }
        broker.instruments.return_value = [{
            "tradingsymbol": "CRUDEOILAUG25FUT",
            "exchange": "MCX",
            "instrument_type": "FUT",
            "freeze_qty": 10_000,
            "lot_size": 100,
            "tick_size": 1.0,
        }]
        broker.basket_order_margins.return_value = [{
            "initial": {"total": 10_000.0},
        }]
        broker.margins.return_value = {
            "equity": {"enabled": True, "net": 500_000.0},
            "commodity": {"enabled": True, "net": 500_000.0},
        }
        broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)

        conns = MagicMock()
        conns.conn = {"ZG0790": object()}

        with patch("backend.brokers.connections.Connections", return_value=conns), \
             patch("backend.brokers.registry.get_broker", return_value=broker), \
             patch("backend.brokers.adapters.kite.get_lot_size",
                   new=AsyncMock(return_value=100)):
            result = await run_preflight("ZG0790", {
                "exchange": "MCX",
                "tradingsymbol": "CRUDEOILAUG25FUT",
                "quantity": 50,  # Broker qty: 50 lots (not 50 contracts)
                "order_type": "LIMIT",
                "product": "NRML",
                "variety": "regular",
                "side": "SELL",
                "price": 5500.0,
                "intent": "close",  # Close intent
            })

        # G1 LOT_MULTIPLE should NOT fire for MCX close
        # (50 lots is valid MCX qty, even though 50 % 100 ≠ 0 in contract terms)
        codes = [b["code"] for b in result["blocked"]]
        assert "LOT_MULTIPLE" not in codes, (
            f"G1 must be skipped for MCX close, but got: {result['blocked']}"
        )


# =============================================================================
# Test 2: ntfy priority=None fallback (no "None" string in header)
# =============================================================================

class TestNtfyPriorityNoneFallback:
    """
    When send_ntfy_alert is called with priority=None (from loss/expiry agents),
    the function should NOT include "None" string in the X-Priority header.
    Instead, it should use a fallback like "default" or clock-based "urgent"/"high".
    """

    def test_ntfy_priority_none_uses_clock_based_default(self):
        """priority=None → falls back to clock-based 'urgent' or 'high', NOT 'None' string."""
        dt = datetime(2026, 7, 14, 14, 0, 0, tzinfo=ZoneInfo("America/New_York"))  # daytime = "high"

        with patch("backend.shared.helpers.alert_utils.secrets", {
            "ntfy_topic": "test_alerts",
            "ntfy_url": "https://ntfy.sh",
            "ntfy_night_start": 22,
            "ntfy_night_end": 7,
        }), patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Test", "Message", priority=None)

            # Verify urlopen was called
            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None

            # Verify Priority header is NOT the string "None"
            assert req is not None, "Expected Request object"
            priority_header = req.headers.get("Priority")
            assert priority_header != "None", (
                f"Priority header must not be the string 'None', got: {priority_header}"
            )
            assert priority_header in ("urgent", "high"), (
                f"Priority should be 'urgent' or 'high' (clock-based), got: {priority_header}"
            )

    def test_ntfy_priority_none_at_night_uses_urgent(self):
        """priority=None at night → falls back to 'urgent' (ET 23:00 is in night window)."""
        dt = datetime(2026, 7, 14, 23, 0, 0, tzinfo=ZoneInfo("America/New_York"))  # nighttime = "urgent"

        with patch("backend.shared.helpers.alert_utils.secrets", {
            "ntfy_topic": "test_alerts",
            "ntfy_url": "https://ntfy.sh",
            "ntfy_night_start": 22,
            "ntfy_night_end": 7,
        }), patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Alert", "Message", priority=None)

            mock_urlopen.assert_called()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None

            priority_header = req.headers.get("Priority")
            assert priority_header == "urgent", (
                f"Expected priority='urgent' at night, got: {priority_header}"
            )

    def test_ntfy_explicit_priority_overrides_clock(self):
        """Explicit priority='default' → uses 'default', not clock-based."""
        dt = datetime(2026, 7, 14, 23, 0, 0, tzinfo=ZoneInfo("America/New_York"))  # night window

        with patch("backend.shared.helpers.alert_utils.secrets", {
            "ntfy_topic": "test_alerts",
            "ntfy_url": "https://ntfy.sh",
            "ntfy_night_start": 22,
            "ntfy_night_end": 7,
        }), patch("datetime.datetime") as mock_dt, \
             patch("urllib.request.urlopen") as mock_urlopen:

            mock_dt.now.return_value = dt
            mock_urlopen.return_value.status = 200

            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert("Alert", "Message", priority="default")

            mock_urlopen.assert_called_once()
            call_args = mock_urlopen.call_args
            req = call_args[0][0] if call_args[0] else None

            # Explicit priority should override clock-based logic
            priority_header = req.headers.get("Priority")
            assert priority_header == "default", (
                f"Expected priority='default' when explicitly set, got: {priority_header}"
            )


# =============================================================================
# Test 3: MCX GTT ceiling config from backend_config.yaml (not hard-coded 50)
# =============================================================================

class TestMcxGttCeilingConfig:
    """
    The Kite adapter's GTT ceiling for MCX/NCO should be configurable via
    backend_config.yaml key 'orders.mcx_gtt_lot_ceiling', not hard-coded to 50.
    Default should be 200, but operator can override via config.
    """

    def test_mcx_gtt_ceiling_reads_from_config_default_200(self):
        """MCX GTT leg qty=150 with default ceiling=200 → passes (no ceiling error)."""
        from backend.brokers.adapters.kite import _check_kite_gtt_qty_ceiling

        exchange = "MCX"
        tradingsymbol = "CRUDEOILAUG25FUT"
        orders = [{"quantity": 150}]  # 150 lots < 200-lot default ceiling

        with patch("backend.shared.helpers.settings.get_int",
                   return_value=200):  # Default ceiling
            # Should not raise
            try:
                _check_kite_gtt_qty_ceiling(exchange, orders, tradingsymbol)
            except ValueError as e:
                pytest.fail(f"MCX qty=150 should not exceed default 200-lot ceiling, got error: {e}")

    def test_mcx_gtt_ceiling_reads_from_config_custom_override(self):
        """MCX GTT leg qty=75 with custom ceiling=50 → raises (ceiling exceeded)."""
        from backend.brokers.adapters.kite import _check_kite_gtt_qty_ceiling

        exchange = "MCX"
        tradingsymbol = "CRUDEOILAUG25FUT"
        orders = [{"quantity": 75}]  # 75 lots > custom 50-lot ceiling

        with patch("backend.shared.helpers.settings.get_int",
                   return_value=50):  # Custom low ceiling
            # Should raise ValueError about ceiling
            with pytest.raises(ValueError) as exc_info:
                _check_kite_gtt_qty_ceiling(exchange, orders, tradingsymbol)

            error_msg = str(exc_info.value)
            assert "ADAPTER-GTT-QTY-CEILING" in error_msg, (
                f"Expected ceiling guard message, got: {error_msg}"
            )
            assert "50" in error_msg, (
                f"Expected '50' in error message, got: {error_msg}"
            )

    def test_mcx_gtt_ceiling_config_missing_uses_default(self):
        """When 'orders.mcx_gtt_lot_ceiling' not in config, use default 200."""
        from backend.brokers.adapters.kite import _check_kite_gtt_qty_ceiling

        exchange = "MCX"
        tradingsymbol = "CRUDEOILAUG25FUT"
        orders = [{"quantity": 180}]  # 180 lots < 200-lot default

        with patch("backend.shared.helpers.settings.get_int") as mock_get_int:
            # Mock get_int to return the default when called
            def side_effect(key, default):
                if key == "orders.mcx_gtt_lot_ceiling":
                    return default  # Return the provided default (200)
                return default
            mock_get_int.side_effect = side_effect

            # Should not raise (uses default 200)
            try:
                _check_kite_gtt_qty_ceiling(exchange, orders, tradingsymbol)
            except ValueError as e:
                pytest.fail(f"MCX qty=180 should not exceed default 200, got error: {e}")

    def test_nco_gtt_ceiling_same_as_mcx(self):
        """NCO GTT should use same configurable ceiling as MCX."""
        from backend.brokers.adapters.kite import _check_kite_gtt_qty_ceiling

        exchange = "NCO"
        tradingsymbol = "GOLDOCTFUT"
        orders = [{"quantity": 210}]  # 210 lots > 200-lot default ceiling

        with patch("backend.shared.helpers.settings.get_int",
                   return_value=200):  # Default ceiling
            # Should raise for NCO just like MCX
            with pytest.raises(ValueError) as exc_info:
                _check_kite_gtt_qty_ceiling(exchange, orders, tradingsymbol)

            error_msg = str(exc_info.value)
            assert "NCO" in error_msg or "ADAPTER-GTT-QTY-CEILING" in error_msg, (
                f"Expected NCO in error or ceiling guard message, got: {error_msg}"
            )

    def test_nfo_gtt_ceiling_unaffected_by_mcx_config(self):
        """NFO GTT has separate 50000-contract ceiling, independent of MCX config."""
        from backend.brokers.adapters.kite import _check_kite_gtt_qty_ceiling

        exchange = "NFO"
        tradingsymbol = "NIFTY25AUGFUT"
        orders = [{"quantity": 45000}]  # 45000 contracts < 50000 ceiling

        with patch("backend.shared.helpers.settings.get_int",
                   return_value=999):  # MCX config doesn't affect NFO
            # Should not raise (NFO has its own 50000-contract ceiling)
            try:
                _check_kite_gtt_qty_ceiling(exchange, orders, tradingsymbol)
            except ValueError as e:
                pytest.fail(f"NFO qty=45000 should not exceed 50000 ceiling, got error: {e}")

    def test_mcx_gtt_ceiling_high_config_allows_100_lots(self):
        """Operator can raise ceiling to 1000 via config to allow 100-lot positions."""
        from backend.brokers.adapters.kite import _check_kite_gtt_qty_ceiling

        exchange = "MCX"
        tradingsymbol = "CRUDEOILAUG25FUT"
        orders = [{"quantity": 100}]  # 100 lots

        with patch("backend.shared.helpers.settings.get_int",
                   return_value=1000):  # High ceiling from config
            # Should not raise (100 lots < 1000 ceiling)
            try:
                _check_kite_gtt_qty_ceiling(exchange, orders, tradingsymbol)
            except ValueError as e:
                pytest.fail(f"MCX qty=100 should not exceed 1000 ceiling, got error: {e}")


# =============================================================================
# Integration tests: all three fixes together
# =============================================================================

class TestPlanFixesIntegration:
    """
    Integration: verify fixes work together in real preflight + config scenarios.
    """

    @pytest.mark.asyncio
    async def test_mcx_close_with_preflight_and_no_false_blocks(self):
        """Full close order preflight with MCX qty in lots → no G1 block."""
        from backend.api.algo.actions import run_preflight

        broker = MagicMock()
        broker.profile.return_value = {
            "exchanges": ["NSE", "NFO", "BSE", "MCX", "NCO", "CDS"]
        }
        broker.instruments.return_value = [{
            "tradingsymbol": "CRUDEOILAUG25FUT",
            "exchange": "MCX",
            "instrument_type": "FUT",
            "freeze_qty": 10_000,
            "lot_size": 100,
            "tick_size": 1.0,
        }]
        broker.basket_order_margins.return_value = [{
            "initial": {"total": 10_000.0},
        }]
        broker.margins.return_value = {
            "equity": {"enabled": True, "net": 500_000.0},
            "commodity": {"enabled": True, "net": 500_000.0},
        }
        broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)

        conns = MagicMock()
        conns.conn = {"ZG0790": object()}

        with patch("backend.brokers.connections.Connections", return_value=conns), \
             patch("backend.brokers.registry.get_broker", return_value=broker), \
             patch("backend.brokers.adapters.kite.get_lot_size",
                   new=AsyncMock(return_value=100)):
            # Simulate a close order with qty=50 lots (from broker position)
            result = await run_preflight("ZG0790", {
                "exchange": "MCX",
                "tradingsymbol": "CRUDEOILAUG25FUT",
                "quantity": 50,
                "order_type": "LIMIT",
                "product": "NRML",
                "variety": "regular",
                "side": "SELL",
                "price": 5500.0,
                "intent": "close",
            })

        # Should not have G1 LOT_MULTIPLE block
        assert result["ok"], f"Preflight should pass, but got blocks: {result.get('blocked', [])}"
