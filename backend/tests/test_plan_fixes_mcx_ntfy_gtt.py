"""
Tests for three order rejection fixes from the plan:
1. MCX G1 LOT_MULTIPLE — applies uniformly to MCX/NCO too (C7 fix,
   2026-09): qty reaching preflight is always normalized to CONTRACTS
   (see `broker_apis.py:_annotate_lot_size`), so a genuine non-multiple
   quantity now correctly blocks, and a proper whole-lot-in-contracts
   quantity still passes cleanly. FAT_FINGER_5_LOT_CAP still skips
   MCX/NCO (independent rationale — route-level 20-lot guard already
   covers it).
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
    C7 fix (2026-09) — qty reaching preflight is ALWAYS already
    normalized to CONTRACTS for MCX/NCO too (see
    `broker_apis.py:_annotate_lot_size`, ~line 1992-2003). The previous
    "broker returns qty in lots, so skip G1 for MCX/NCO" premise was
    confirmed FALSE for the paths that reach `_preflight_validate_lots`
    — the skip let genuinely non-multiple quantities bypass G1
    entirely. G1 now applies uniformly to every F&O exchange.
    """

    @pytest.mark.asyncio
    async def test_mcx_close_position_qty_non_multiple_now_blocked_by_g1(self):
        """MCX close: qty=50 CONTRACTS with lot_size=100 is a genuine
        non-multiple (0.5 lots) — G1 LOT_MULTIPLE must now fire.
        (Pre-fix this incorrectly passed — the C7 regression case.)"""
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
                "quantity": 50,  # CONTRACTS — not a multiple of lot_size=100
                "order_type": "LIMIT",
                "product": "NRML",
                "variety": "regular",
                "side": "SELL",
                "price": 5500.0,
                "intent": "close",  # Close intent — G1 still applies
            })

        codes = [b["code"] for b in result["blocked"]]
        assert "LOT_MULTIPLE" in codes, (
            f"G1 must now fire for a genuinely non-multiple MCX qty, "
            f"got: {result['blocked']}"
        )
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_mcx_close_position_qty_clean_multiple_passes_g1(self):
        """MCX close: qty=5000 CONTRACTS (=50 lots) with lot_size=100 is
        a clean multiple — G1 LOT_MULTIPLE must NOT fire."""
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
                "quantity": 5000,  # 50 lots in CONTRACTS — clean multiple
                "order_type": "LIMIT",
                "product": "NRML",
                "variety": "regular",
                "side": "SELL",
                "price": 5500.0,
                "intent": "close",
            })

        codes = [b["code"] for b in result["blocked"]]
        assert "LOT_MULTIPLE" not in codes, (
            f"G1 must NOT fire for a clean-multiple MCX qty, "
            f"got: {result['blocked']}"
        )

    @pytest.mark.asyncio
    async def test_mcx_open_within_lot_cap_not_blocked_by_fat_finger(self):
        """MCX open (not close): qty=100 contracts (1 lot at
        lot_size=100) must NOT trigger FAT_FINGER_5_LOT_CAP — that
        guard still skips MCX/NCO for its own independent reason (the
        route-level 20-lot cap is authoritative there), unaffected by
        the G1 fix."""
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
                "quantity": 1000,  # 10 lots — well above the 5-lot FAT_FINGER cap
                "order_type": "LIMIT",
                "product": "NRML",
                "variety": "regular",
                "side": "BUY",
                "price": 5500.0,
                # no intent="close" — this is a new open
            })

        codes = [b["code"] for b in result["blocked"]]
        assert "FAT_FINGER_5_LOT_CAP" not in codes, (
            f"FAT_FINGER_5_LOT_CAP must still skip MCX/NCO (route-level "
            f"20-lot guard is authoritative), got: {result['blocked']}"
        )
        assert "LOT_MULTIPLE" not in codes


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
        """Full close order preflight with a clean-multiple MCX qty (in
        CONTRACTS, per C7) → no G1 block."""
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
            # Simulate a close order with qty=5000 CONTRACTS (= 50 lots,
            # a clean multiple of lot_size=100).
            result = await run_preflight("ZG0790", {
                "exchange": "MCX",
                "tradingsymbol": "CRUDEOILAUG25FUT",
                "quantity": 5000,
                "order_type": "LIMIT",
                "product": "NRML",
                "variety": "regular",
                "side": "SELL",
                "price": 5500.0,
                "intent": "close",
            })

        # Should not have G1 LOT_MULTIPLE block
        assert result["ok"], f"Preflight should pass, but got blocks: {result.get('blocked', [])}"
