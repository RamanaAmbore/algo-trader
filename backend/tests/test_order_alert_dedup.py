"""
Tests for order-failure alert deduplication system (Fix 1+2+3).

Fix 1 — Redis-backed cooldown persists across restarts:
  - When Redis has a live TTL key, a second call is suppressed without
    sending Telegram/email, regardless of the in-process dict state.
  - When Redis is unavailable the in-process dict fallback still works.

Fix 2 — Market-hours gate:
  - When all segments are closed, send_order_failure_alert returns without
    firing Telegram or email.
  - When segments are open, delivery proceeds normally.

Fix 3 — _action_place_order preflight-block path uses send_order_failure_alert
  (not _dispatch directly), so cooldown + market-hours gate apply.

These tests patch at the delivery boundary (_send_telegram / _SMTP_EXECUTOR)
so they verify the dedup logic without touching real Redis or a real broker.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_alert_utils():
    """Fresh import of alert_utils with reset module-level state."""
    import backend.shared.helpers.alert_utils as m
    # Reset in-process state between test runs.
    m._order_alert_state.clear()
    m._redis_client = None
    m._redis_available = True
    return m


# ---------------------------------------------------------------------------
# Fix 2: market-hours gate
# ---------------------------------------------------------------------------

class TestMarketHoursGate:
    def test_suppressed_when_all_closed(self):
        """No Telegram fired when _any_segment_open() returns False."""
        m = _reload_alert_utils()
        with (
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=False),
            patch.object(m, "_send_telegram") as mock_tg,
            patch.object(m, "_SMTP_EXECUTOR") as mock_smtp,
        ):
            m.send_order_failure_alert(
                account="ZG1234",
                symbol="CRUDEOIL24JULFUT",
                exchange="MCX",
                side="SELL",
                qty=100,
                mode="live",
                source="agent:test-agent",
                error="InputException: lot_size mismatch",
            )
            mock_tg.assert_not_called()
            mock_smtp.submit.assert_not_called()

    def test_fires_when_market_open(self):
        """Telegram fires when _any_segment_open() returns True and no cooldown."""
        m = _reload_alert_utils()
        with (
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True),
            patch.object(m, "_send_telegram") as mock_tg,
            patch.object(m, "_SMTP_EXECUTOR"),
            patch.object(m, "get_alert_recipients", return_value=[]),
        ):
            m.send_order_failure_alert(
                account="ZG1234",
                symbol="NIFTY24JULCE",
                exchange="NFO",
                side="BUY",
                qty=50,
                mode="live",
                source="agent:nifty-agent",
                error="margin_insufficient",
            )
            mock_tg.assert_called_once()

    def test_market_hours_check_error_fails_open(self):
        """If _any_segment_open import raises, alert still fires (fail-open)."""
        m = _reload_alert_utils()
        with (
            patch(
                "backend.api.helpers.snapshot_gate._any_segment_open",
                side_effect=ImportError("module not found"),
            ),
            patch.object(m, "_send_telegram") as mock_tg,
            patch.object(m, "_SMTP_EXECUTOR"),
            patch.object(m, "get_alert_recipients", return_value=[]),
        ):
            m.send_order_failure_alert(
                account="ZG1234",
                symbol="CRUDEOIL24JULFUT",
                exchange="MCX",
                side="SELL",
                qty=100,
                mode="live",
                source="agent:test-agent",
                error="market-hours check failed",
            )
            # Fail-open: should still fire
            mock_tg.assert_called_once()


# ---------------------------------------------------------------------------
# Fix 1: Redis-backed cooldown
# ---------------------------------------------------------------------------

class TestRedisCooldown:
    def test_redis_suppresses_on_second_call(self):
        """When Redis GET returns a hit, the second call is suppressed."""
        m = _reload_alert_utils()

        fake_redis = MagicMock()
        # First call: key absent → proceed; arm TTL.
        # Second call: key present → suppress.
        fake_redis.get.side_effect = [None, b"1"]
        fake_redis.ping.return_value = True

        with (
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True),
            patch.object(m, "_get_redis", return_value=fake_redis),
            patch.object(m, "_send_telegram") as mock_tg,
            patch.object(m, "_SMTP_EXECUTOR"),
            patch.object(m, "get_alert_recipients", return_value=[]),
        ):
            kwargs = dict(
                account="ZG1234",
                symbol="CRUDEOIL24JULFUT",
                exchange="MCX",
                side="SELL",
                qty=100,
                mode="live",
                source="agent:test-agent",
                error="InputException: quantity",
            )
            m.send_order_failure_alert(**kwargs)
            m.send_order_failure_alert(**kwargs)

        # Only the first call should have fired Telegram.
        assert mock_tg.call_count == 1

    def test_redis_setex_called_on_first_send(self):
        """SETEX is called with correct key prefix and TTL on the first send."""
        m = _reload_alert_utils()

        fake_redis = MagicMock()
        fake_redis.get.return_value = None
        fake_redis.ping.return_value = True

        with (
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True),
            patch.object(m, "_get_redis", return_value=fake_redis),
            patch.object(m, "_send_telegram"),
            patch.object(m, "_SMTP_EXECUTOR"),
            patch.object(m, "get_alert_recipients", return_value=[]),
        ):
            m.send_order_failure_alert(
                account="ZG5678",
                symbol="GOLDPETAL24JULFUT",
                exchange="MCX",
                side="BUY",
                qty=1,
                mode="live",
                source="agent:gold-agent",
                error="InsufficientFunds",
            )

        # Verify setex was called with the right TTL and a ramboq: prefixed key.
        assert fake_redis.setex.call_count == 1
        setex_args = fake_redis.setex.call_args[0]
        assert setex_args[0].startswith("ramboq:order_alert:")
        assert setex_args[1] == m._ORDER_ALERT_COOLDOWN_SEC
        assert setex_args[2] == b"1"

    def test_fallback_to_dict_when_redis_unavailable(self):
        """In-process dict cooldown still works when Redis returns None."""
        m = _reload_alert_utils()

        with (
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True),
            patch.object(m, "_get_redis", return_value=None),  # Redis unavailable
            patch.object(m, "_send_telegram") as mock_tg,
            patch.object(m, "_SMTP_EXECUTOR"),
            patch.object(m, "get_alert_recipients", return_value=[]),
        ):
            kwargs = dict(
                account="ZG9999",
                symbol="SILVER24JULFUT",
                exchange="MCX",
                side="SELL",
                qty=5,
                mode="live",
                source="agent:silver-agent",
                error="timeout",
            )
            m.send_order_failure_alert(**kwargs)  # fires
            m.send_order_failure_alert(**kwargs)  # should be suppressed by dict

        assert mock_tg.call_count == 1  # only the first fire

    def test_redis_error_during_check_falls_back_to_dict(self):
        """If Redis.get() raises, the call falls through to the in-process dict."""
        m = _reload_alert_utils()

        fake_redis = MagicMock()
        fake_redis.get.side_effect = ConnectionError("Redis connection refused")
        fake_redis.ping.return_value = True

        with (
            patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True),
            patch.object(m, "_get_redis", return_value=fake_redis),
            patch.object(m, "_send_telegram") as mock_tg,
            patch.object(m, "_SMTP_EXECUTOR"),
            patch.object(m, "get_alert_recipients", return_value=[]),
        ):
            kwargs = dict(
                account="ZG0001",
                symbol="CRUDEOIL24JULFUT",
                exchange="MCX",
                side="SELL",
                qty=50,
                mode="live",
                source="agent:crude-agent",
                error="network_error",
            )
            m.send_order_failure_alert(**kwargs)  # falls back to dict, fires
            m.send_order_failure_alert(**kwargs)  # dict suppresses

        assert mock_tg.call_count == 1


# ---------------------------------------------------------------------------
# Fix 3: _action_place_order preflight-block → send_order_failure_alert
# ---------------------------------------------------------------------------

class TestPreflightBlockRouting:
    def test_preflight_block_calls_send_order_failure_alert(self):
        """The preflight-block notification path in _action_place_order
        calls send_order_failure_alert (not _dispatch directly)."""
        import backend.api.algo.actions as actions_mod

        context = {"agent_slug": "test-agent"}
        params = {
            "account": "ZG1234",
            "symbol": "CRUDEOIL24JULFUT",
            "exchange": "MCX",
            "transaction_type": "SELL",
            "quantity": 50,
            "product": "NRML",
        }

        # Make preflight fail.
        fake_pf = {
            "ok": False,
            "blocked": [{"code": "G1", "reason": "lot_size mismatch", "fix": "use 100"}],
            "diagnostics": {},
        }

        import asyncio

        with (
            patch.object(actions_mod, "run_preflight", return_value=fake_pf) as mock_pf,
            patch.object(actions_mod, "_fetch_ltp", return_value=None),
            patch(
                "backend.shared.helpers.alert_utils.send_order_failure_alert"
            ) as mock_alert,
            patch.object(actions_mod, "_write_live_order", return_value=None),
        ):
            asyncio.run(actions_mod._action_place_order(context, params))

        # send_order_failure_alert must have been called with the correct args.
        mock_alert.assert_called_once()
        call_kwargs = mock_alert.call_args.kwargs
        assert call_kwargs["symbol"] == "CRUDEOIL24JULFUT"
        assert call_kwargs["side"] == "SELL"
        assert call_kwargs["mode"] == "live"
        assert "agent:test-agent" in call_kwargs["source"]
        assert "preflight blocked" in call_kwargs["error"].lower()

    def test_preflight_block_does_not_call_dispatch_directly(self):
        """_dispatch is NOT called directly on preflight block (bypass eliminated)."""
        import backend.api.algo.actions as actions_mod

        context = {"agent_slug": "test-agent"}
        params = {
            "account": "ZG1234",
            "symbol": "CRUDEOIL24JULFUT",
            "exchange": "MCX",
            "transaction_type": "SELL",
            "quantity": 50,
            "product": "NRML",
        }
        fake_pf = {
            "ok": False,
            "blocked": [{"code": "G1", "reason": "lot_size", "fix": "fix it"}],
            "diagnostics": {},
        }

        import asyncio

        with (
            patch.object(actions_mod, "run_preflight", return_value=fake_pf),
            patch.object(actions_mod, "_fetch_ltp", return_value=None),
            patch("backend.shared.helpers.alert_utils._dispatch") as mock_dispatch,
            patch("backend.shared.helpers.alert_utils.send_order_failure_alert"),
            patch.object(actions_mod, "_write_live_order", return_value=None),
        ):
            asyncio.run(actions_mod._action_place_order(context, params))

        mock_dispatch.assert_not_called()
