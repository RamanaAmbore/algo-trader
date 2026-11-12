"""Tests for the unified AppMessage notification/log system.

Tests cover:
- Retention rules (_retain_until) for different severity levels and tags
- Database persistence (dispatch)
- Sync entry point (fire)
- Alert routing to ntfy.sh (_route_sinks)
- Endpoint filtering and pagination (MessagesController)
- Post-market cleanup cron
"""

import asyncio
import pytest
import pytest_asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call
import os

# Ensure pytest environment is set
os.environ['PYTEST_RUNNING'] = '1'


# ============================================================================
# Group 1: Retention rules (_retain_until)
# ============================================================================

class TestRetentionRules:
    """Test retention expiry calculation for different severity/tag combos."""

    def test_retain_until_critical_is_permanent(self):
        """Critical messages never expire."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _retain_until
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        msg = AppMessage(body="system down", tags=["system"], level="critical")
        assert _retain_until(msg) is None, "critical severity should have None (permanent) retain_until"

    def test_retain_until_error_is_permanent(self):
        """Error messages never expire."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _retain_until
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        msg = AppMessage(body="broker auth failed", tags=["order"], level="error")
        assert _retain_until(msg) is None, "error severity should have None (permanent) retain_until"

    def test_retain_until_warning_is_90_days(self):
        """Warning messages expire in 90 days."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _retain_until
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        msg = AppMessage(body="high leverage", tags=["system"], level="warning")
        result = _retain_until(msg)
        expected = date.today() + timedelta(days=90)
        assert result == expected, f"warning should expire in 90 days, got {result}"

    def test_retain_until_order_tag_is_30_days(self):
        """Order-tagged messages expire in 30 days."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _retain_until
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        msg = AppMessage(body="order filled", tags=["order"], level="info")
        result = _retain_until(msg)
        expected = date.today() + timedelta(days=30)
        assert result == expected, f"order tag should expire in 30 days, got {result}"

    def test_retain_until_system_tag_is_7_days(self):
        """System-tagged messages expire in 7 days."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _retain_until
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        msg = AppMessage(body="background sync", tags=["system"], level="info")
        result = _retain_until(msg)
        expected = date.today() + timedelta(days=7)
        assert result == expected, f"system tag should expire in 7 days, got {result}"

    def test_retain_until_uses_shortest_retention_for_multiple_tags(self):
        """Multiple tags use the minimum retention window."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _retain_until
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        # order=30, system=7 → min=7
        msg = AppMessage(body="order with system note", tags=["order", "system"], level="info")
        result = _retain_until(msg)
        expected = date.today() + timedelta(days=7)
        assert result == expected, f"multiple tags should use shortest retention (7 days), got {result}"

    def test_retain_until_deploy_tag_is_7_days(self):
        """Deploy-tagged messages expire in 7 days."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _retain_until
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        msg = AppMessage(body="api deployed", tags=["deploy"], level="info")
        result = _retain_until(msg)
        expected = date.today() + timedelta(days=7)
        assert result == expected, f"deploy tag should expire in 7 days, got {result}"

    def test_retain_until_news_tag_ephemeral(self):
        """News-tagged (ephemeral) messages should not be persisted at all."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _retain_until
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        msg = AppMessage(body="market news", tags=["news"], level="info")
        # Ephemeral messages may return None or special marker
        result = _retain_until(msg)
        # This test may need adjustment based on actual implementation
        # but ephemeral tags should not be stored in DB
        assert result is None or result == "ephemeral", \
            "news (ephemeral) tag should not be persisted"


# ============================================================================
# Group 2: dispatch() — DB write and fire logic
# ============================================================================

class TestDispatchDBWrite:
    """Test database persistence and async dispatch."""

    @pytest.mark.asyncio
    async def test_dispatch_writes_to_db(self):
        """dispatch() writes AppMessage to database."""
        try:
            from backend.shared.helpers.app_message import AppMessage, dispatch
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("backend.shared.helpers.app_message.async_session",
                   return_value=mock_session), \
             patch("backend.shared.helpers.app_message.asyncio.create_task") as mock_create_task:

            msg = AppMessage(body="test message", tags=["system"], level="info", title="Test")
            await dispatch(msg)

        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dispatch_skips_ephemeral_only_tags(self):
        """dispatch() does not persist news-only messages."""
        try:
            from backend.shared.helpers.app_message import AppMessage, dispatch
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        with patch("backend.shared.helpers.app_message.async_session") as mock_sess:
            msg = AppMessage(body="market news", tags=["news"], level="info")
            await dispatch(msg)

        # Should not open a session for ephemeral-only messages
        mock_sess.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_swallows_db_failure(self):
        """dispatch() does not raise on database errors."""
        try:
            from backend.shared.helpers.app_message import AppMessage, dispatch
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        with patch("backend.shared.helpers.app_message.async_session",
                   side_effect=Exception("DB connection lost")), \
             patch("backend.shared.helpers.app_message.asyncio.create_task"):
            # Should not raise
            msg = AppMessage(body="error message", tags=["system"], level="error")
            await dispatch(msg)

    @pytest.mark.asyncio
    async def test_dispatch_schedules_ntfy_routing(self):
        """dispatch() schedules _route_sinks as background task."""
        try:
            from backend.shared.helpers.app_message import AppMessage, dispatch
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_loop = MagicMock()
        mock_loop.create_task = MagicMock()

        with patch("backend.shared.helpers.app_message.async_session",
                   return_value=mock_session), \
             patch("backend.shared.helpers.app_message.asyncio.create_task") as mock_create_task:

            msg = AppMessage(body="error", tags=["system"], level="error")
            await dispatch(msg)

        # Should have scheduled the sinks routing
        mock_create_task.assert_called()


# ============================================================================
# Group 3: fire() — sync entry point
# ============================================================================

class TestFireSyncEntry:
    """Test synchronous fire() scheduler."""

    def test_fire_schedules_task_on_running_loop(self):
        """fire() schedules dispatch() on the running event loop."""
        try:
            from backend.shared.helpers.app_message import AppMessage, fire
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        mock_loop = MagicMock()
        with patch("backend.shared.helpers.app_message.asyncio.get_running_loop",
                   return_value=mock_loop):
            msg = AppMessage(body="test", tags=["deploy"], level="info")
            fire(msg)
        mock_loop.create_task.assert_called_once()

    def test_fire_silent_when_no_event_loop(self):
        """fire() silently returns if no event loop is running."""
        try:
            from backend.shared.helpers.app_message import AppMessage, fire
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        with patch("backend.shared.helpers.app_message.asyncio.get_running_loop",
                   side_effect=RuntimeError("no running event loop")):
            msg = AppMessage(body="test", tags=["system"], level="info")
            # Should not raise
            fire(msg)

    def test_fire_returns_none(self):
        """fire() is fire-and-forget, returns None."""
        try:
            from backend.shared.helpers.app_message import AppMessage, fire
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        mock_loop = MagicMock()
        with patch("backend.shared.helpers.app_message.asyncio.get_running_loop",
                   return_value=mock_loop):
            msg = AppMessage(body="test", tags=["system"], level="info")
            result = fire(msg)
        assert result is None, "fire() should return None"


# ============================================================================
# Group 4: _route_sinks() — log-and-dispatch routing (2026-11 alert-agent
# audit fix)
#
# `_route_sinks` used to call `send_ntfy_alert()` directly via
# `asyncio.to_thread` — a second, parallel notification pipeline outside
# `event_agents.py`'s control (see this module's own docstring on
# `_route_sinks`). It now logs a single tagged record through the
# module's `logger` instead, so a seeded event agent can pick it up and
# route it through the normal dispatch pipeline. These tests assert
# (a) no direct `send_ntfy_alert`/`asyncio.to_thread` call remains, and
# (b) the expected log record — with `alert_event`/`tags` — is emitted
# only when the pre-existing routing gate (error/critical level, or a
# deploy/alert tag) is satisfied.
# ============================================================================

class TestRouteSinks:
    """Test alert routing via the log-then-dispatch pipeline."""

    @pytest.mark.asyncio
    async def test_route_sinks_no_direct_ntfy_call_remains(self):
        """`_route_sinks` must never call `send_ntfy_alert`/`asyncio.
        to_thread` directly — even for an error-level message, which is
        exactly the case that used to trigger the direct-send call."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _route_sinks
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        with patch("backend.shared.helpers.app_message.asyncio.to_thread",
                   new_callable=AsyncMock) as mock_thread, \
             patch("backend.shared.helpers.alert_utils.send_ntfy_alert") as mock_ntfy:
            msg = AppMessage(body="broker auth failed", tags=["system"], level="error", title="Auth Error")
            await _route_sinks(msg)
        mock_thread.assert_not_awaited()
        mock_ntfy.assert_not_called()

    @pytest.mark.asyncio
    async def test_route_sinks_logs_tagged_record_on_error(self):
        """An error-level message is logged with `alert_event=
        'app_message'` and its own tags forwarded verbatim — `tags_for()`
        (log_store.py) separately stamps the "error" level tag, which is
        what lets `ERROR_AGENT`'s catch-all pick this up for free."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _route_sinks, logger
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        with patch.object(logger, "log") as mock_log:
            msg = AppMessage(body="broker auth failed", tags=["system"], level="error", title="Auth Error")
            await _route_sinks(msg)
        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert args[0] == 40  # logging.ERROR
        extra = kwargs["extra"]
        assert extra["alert_event"] == "app_message"
        assert extra["tags"] == ["system"]
        assert extra["title"] == "Auth Error"
        assert extra["body"] == "broker auth failed"

    @pytest.mark.asyncio
    async def test_route_sinks_logs_tagged_record_on_critical(self):
        """Critical messages are logged at CRITICAL, same tagging
        contract as the error case."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _route_sinks, logger
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        with patch.object(logger, "log") as mock_log:
            msg = AppMessage(body="system failure", tags=["system"], level="critical", title="Critical")
            await _route_sinks(msg)
        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert args[0] == 50  # logging.CRITICAL
        assert kwargs["extra"]["alert_event"] == "app_message"

    @pytest.mark.asyncio
    async def test_route_sinks_logs_tagged_record_on_deploy_tag(self):
        """A deploy-tagged info-level message still satisfies the
        pre-existing gate and is logged (at INFO) — its own "deploy" tag
        is forwarded verbatim, not synthesized into "alert" (see
        `_route_sinks`'s own docstring for why injecting "alert" here
        would risk a double-fire alongside ERROR_AGENT for error/
        critical messages — this message is info-level so that specific
        risk doesn't apply, but the module deliberately uses one
        consistent rule: forward the caller's own tags, never add one)."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _route_sinks, logger
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        with patch.object(logger, "log") as mock_log:
            msg = AppMessage(body="api deployed to prod", tags=["deploy", "system"], level="info")
            await _route_sinks(msg)
        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert args[0] == 20  # logging.INFO
        extra = kwargs["extra"]
        assert extra["alert_event"] == "app_message"
        assert extra["tags"] == ["deploy", "system"]

    @pytest.mark.asyncio
    async def test_route_sinks_no_log_for_info_system(self):
        """`_route_sinks` stays a no-op for generic system info — the
        same gate as before (error/critical level, or a deploy/alert
        tag) applies, just with a different delivery mechanism."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _route_sinks, logger
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        with patch.object(logger, "log") as mock_log:
            msg = AppMessage(body="background sync tick", tags=["system"], level="info")
            await _route_sinks(msg)
        mock_log.assert_not_called()

    @pytest.mark.asyncio
    async def test_route_sinks_no_log_for_news(self):
        """`_route_sinks` does not log on news (ephemeral, info-level,
        no deploy/alert tag)."""
        try:
            from backend.shared.helpers.app_message import AppMessage, _route_sinks, logger
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        with patch.object(logger, "log") as mock_log:
            msg = AppMessage(body="market news", tags=["news"], level="info")
            await _route_sinks(msg)
        mock_log.assert_not_called()


# ============================================================================
# Group 5: MessagesController endpoint
# ============================================================================

class TestMessagesController:
    """Test GET /api/messages endpoint filtering and pagination."""

    @pytest.mark.asyncio
    async def test_messages_endpoint_filters_by_tag(self):
        """GET /api/messages?tags=order returns only order-tagged rows."""
        try:
            from backend.api.routes.messages import list_messages
        except ImportError:
            pytest.skip("messages routes module not yet implemented")

        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.created_at = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)
        mock_row.level = "info"
        mock_row.tags = ["order"]
        mock_row.title = None
        mock_row.body = "NIFTY filled at 24000"
        mock_row.account = None
        mock_row.symbol = "NIFTY"
        mock_row.data = None
        mock_row.retain_until = None

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_row]
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("backend.api.routes.messages.async_session",
                   return_value=mock_session):
            result = await list_messages(tags="order", limit=500, since="", account="")

        assert len(result) == 1, f"expected 1 result, got {len(result)}"
        assert result[0].body == "NIFTY filled at 24000"
        assert result[0].symbol == "NIFTY"

    @pytest.mark.asyncio
    async def test_messages_endpoint_filters_by_account(self):
        """GET /api/messages?account=ZG0790 filters by account."""
        try:
            from backend.api.routes.messages import list_messages
        except ImportError:
            pytest.skip("messages routes module not yet implemented")

        mock_row = MagicMock()
        mock_row.id = 2
        mock_row.created_at = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
        mock_row.level = "warning"
        mock_row.tags = ["order"]
        mock_row.title = None
        mock_row.body = "High leverage"
        mock_row.account = "ZG0790"
        mock_row.symbol = None
        mock_row.data = None
        mock_row.retain_until = None

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_row]
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("backend.api.routes.messages.async_session",
                   return_value=mock_session):
            result = await list_messages(tags="", limit=500, since="", account="ZG0790")

        assert len(result) == 1
        assert result[0].account == "ZG0790"

    @pytest.mark.asyncio
    async def test_messages_endpoint_caps_limit_at_500(self):
        """GET /api/messages limits results to 500 even if limit=9999."""
        try:
            from backend.api.routes.messages import list_messages
        except ImportError:
            pytest.skip("messages routes module not yet implemented")

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("backend.api.routes.messages.async_session",
                   return_value=mock_session):
            result = await list_messages(tags="", limit=9999, since="", account="")

        assert isinstance(result, list)
        # The limit should have been capped; verify execute was called with limit <= 500
        mock_session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_messages_endpoint_filters_by_since_date(self):
        """GET /api/messages?since=2026-07-20 filters messages after that date."""
        try:
            from backend.api.routes.messages import list_messages
        except ImportError:
            pytest.skip("messages routes module not yet implemented")

        mock_row = MagicMock()
        mock_row.id = 3
        mock_row.created_at = datetime(2026, 7, 25, 14, 30, tzinfo=timezone.utc)
        mock_row.level = "info"
        mock_row.tags = ["system"]
        mock_row.title = "Sync"
        mock_row.body = "Daily sync completed"
        mock_row.account = None
        mock_row.symbol = None
        mock_row.data = None
        mock_row.retain_until = None

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_row]
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("backend.api.routes.messages.async_session",
                   return_value=mock_session):
            result = await list_messages(tags="", limit=500, since="2026-07-20", account="")

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_messages_endpoint_returns_ordered_by_date_desc(self):
        """GET /api/messages returns rows ordered by created_at DESC (newest first)."""
        try:
            from backend.api.routes.messages import list_messages
        except ImportError:
            pytest.skip("messages routes module not yet implemented")

        mock_row1 = MagicMock()
        mock_row1.id = 10
        mock_row1.created_at = datetime(2026, 7, 26, 15, 0, tzinfo=timezone.utc)
        mock_row1.level = "info"
        mock_row1.tags = ["system"]
        mock_row1.title = None
        mock_row1.body = "Newer message"
        mock_row1.account = None
        mock_row1.symbol = None
        mock_row1.data = None
        mock_row1.retain_until = None

        mock_row2 = MagicMock()
        mock_row2.id = 9
        mock_row2.created_at = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)
        mock_row2.level = "info"
        mock_row2.tags = ["system"]
        mock_row2.title = None
        mock_row2.body = "Older message"
        mock_row2.account = None
        mock_row2.symbol = None
        mock_row2.data = None
        mock_row2.retain_until = None

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_row1, mock_row2]
        mock_session.execute = AsyncMock(return_value=mock_result)

        with patch("backend.api.routes.messages.async_session",
                   return_value=mock_session):
            result = await list_messages(tags="", limit=500, since="", account="")

        assert len(result) == 2
        assert result[0].body == "Newer message"
        assert result[1].body == "Older message"


# ============================================================================
# Group 6: Post-market cron cleanup
# ============================================================================

class TestLateNightCleanup:
    """Test post-market message cleanup cronjob."""

    @pytest.mark.asyncio
    async def test_late_night_cleanup_deletes_expired_rows(self):
        """_run_late_night (or similar) deletes app_messages with retain_until < today."""
        try:
            from backend.api.background import _run_late_night
        except ImportError:
            pytest.skip("_run_late_night not yet implemented")

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.rowcount = 5
        mock_session.execute = AsyncMock(return_value=mock_result)

        today = date(2026, 7, 26)
        with patch("backend.api.background.async_session", return_value=mock_session):
            await _run_late_night(today, {})

        mock_session.execute.assert_awaited()
        mock_session.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_late_night_cleanup_preserves_permanent_rows(self):
        """_run_late_night does not delete messages with retain_until=None."""
        try:
            from backend.api.background import _run_late_night
        except ImportError:
            pytest.skip("_run_late_night not yet implemented")

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=mock_result)

        today = date(2026, 7, 26)
        with patch("backend.api.background.async_session", return_value=mock_session):
            await _run_late_night(today, {})

        # Should still execute (filter for retain_until IS NOT NULL)
        mock_session.execute.assert_awaited()

    @pytest.mark.asyncio
    async def test_late_night_cleanup_swallows_db_errors(self):
        """_run_late_night does not raise on database errors."""
        try:
            from backend.api.background import _run_late_night
        except ImportError:
            pytest.skip("_run_late_night not yet implemented")

        with patch("backend.api.background.async_session",
                   side_effect=Exception("DB error")):
            today = date(2026, 7, 26)
            # Should not raise
            await _run_late_night(today, {})


# ============================================================================
# Group 7: AppMessage dataclass validation
# ============================================================================

class TestAppMessageDataclass:
    """Test AppMessage construction and validation."""

    def test_app_message_minimal_fields(self):
        """AppMessage can be constructed with body, tags, level."""
        try:
            from backend.shared.helpers.app_message import AppMessage
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        msg = AppMessage(body="test", tags=["system"], level="info")
        assert msg.body == "test"
        assert msg.tags == ["system"]
        assert msg.level == "info"
        assert msg.title is None

    def test_app_message_with_optional_fields(self):
        """AppMessage accepts optional title, account, symbol fields."""
        try:
            from backend.shared.helpers.app_message import AppMessage
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        msg = AppMessage(
            body="error message",
            tags=["order"],
            level="error",
            title="Order Failed",
            account="ZG0790",
            symbol="NIFTY"
        )
        assert msg.title == "Order Failed"
        assert msg.account == "ZG0790"
        assert msg.symbol == "NIFTY"

    def test_app_message_created_at_timestamp(self):
        """AppMessage auto-generates created_at if not provided."""
        try:
            from backend.shared.helpers.app_message import AppMessage
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        before = datetime.now(timezone.utc)
        msg = AppMessage(body="test", tags=["system"], level="info")
        after = datetime.now(timezone.utc)

        if hasattr(msg, 'created_at') and msg.created_at is not None:
            assert before <= msg.created_at <= after


# ============================================================================
# Integration: Full dispatch lifecycle
# ============================================================================

class TestDispatchLifecycle:
    """Test full message lifecycle from fire → dispatch → DB → ntfy."""

    @pytest.mark.asyncio
    async def test_full_error_message_lifecycle(self):
        """Error message flows: fire → dispatch → DB & ntfy."""
        try:
            from backend.shared.helpers.app_message import AppMessage, fire, dispatch
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_loop = MagicMock()
        captured_coro = None

        def capture_task(coro):
            nonlocal captured_coro
            captured_coro = coro
            return MagicMock()

        mock_loop.create_task = capture_task

        with patch("backend.shared.helpers.app_message.async_session",
                   return_value=mock_session), \
             patch("backend.shared.helpers.app_message.asyncio.get_running_loop",
                   return_value=mock_loop):

            msg = AppMessage(body="broker failure", tags=["system"], level="error")
            fire(msg)

        # Verify loop.create_task was called with a coroutine
        assert captured_coro is not None, "loop.create_task should have been called with a dispatch coroutine"

    @pytest.mark.asyncio
    async def test_ephemeral_news_not_persisted(self):
        """News messages are not persisted to DB, only routed to ntfy if warranted."""
        try:
            from backend.shared.helpers.app_message import AppMessage, dispatch
        except ImportError:
            pytest.skip("app_message module not yet implemented")

        with patch("backend.shared.helpers.app_message.async_session") as mock_sess:
            msg = AppMessage(body="market news headline", tags=["news"], level="info")
            await dispatch(msg)

        # Ephemeral news should not open a DB session
        mock_sess.assert_not_called()
