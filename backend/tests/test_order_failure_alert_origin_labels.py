"""test_order_failure_alert_origin_labels.py

2026-10 operator request: order-failure alerts must be labeled
"Manual" / "Manual Bracket" / "Agent" / "Agent Bracket" depending on
(a) whether the order was placed by an operator (agent_id is None) or
an agent (agent_id set — reliably populated since the Sprint 1a
_al_place_resolve_params fix), and (b) whether the alert is about the
template/Bracket exit mechanism (source in {"template_wing",
"template_wing_chase"}) or the order itself.

Covers:
  1. The core classifier, `_classify_order_origin_label`.
  2. `order_failure_messages` embeds the label in tg_body/subject/
     email_body/rows_html.
  3. `send_order_failure_alert` → `_send_order_failure_messages` threads
     `agent_id` into the logged `extra` dict (survives the log→event-
     agent replay pipeline event_agents.py:_render_order_failure reads).
  4. A representative chase.py internal alert path (max-attempts
     exhaustion) receives `agent_id` from `chase_order()`'s own new
     parameter, through `_chase_order_impl` and its helper.
  5. The previously-mislabeled manual-ticket alert
     (orders_place.py:_opl_send_failure_alert) now sources "ticket",
     not "agent:manual:ticket", and passes no agent_id.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestClassifyOrderOriginLabel:
    def test_manual_non_bracket(self):
        from backend.shared.helpers.alert_utils import _classify_order_origin_label
        assert _classify_order_origin_label("ticket", None) == "Manual"
        assert _classify_order_origin_label("chase", None) == "Manual"
        assert _classify_order_origin_label("circuit-breaker", None) == "Manual"

    def test_manual_bracket(self):
        from backend.shared.helpers.alert_utils import _classify_order_origin_label
        assert _classify_order_origin_label("template_wing", None) == "Manual Bracket"
        assert _classify_order_origin_label("template_wing_chase", None) == "Manual Bracket"

    def test_agent_non_bracket(self):
        from backend.shared.helpers.alert_utils import _classify_order_origin_label
        assert _classify_order_origin_label("chase", 42) == "Agent"
        assert _classify_order_origin_label("ticket", 42) == "Agent"

    def test_agent_bracket(self):
        from backend.shared.helpers.alert_utils import _classify_order_origin_label
        assert _classify_order_origin_label("template_wing", 42) == "Agent Bracket"
        assert _classify_order_origin_label("template_wing_chase", 42) == "Agent Bracket"


class TestOrderFailureMessagesEmbedLabel:
    def _call(self, **overrides):
        from backend.shared.helpers.alert_utils import order_failure_messages
        kwargs = dict(
            masked="ZG####", symbol="NIFTY25OCTFUT", exchange="NFO", side="SELL",
            qty=50, mode="live", source="chase", error="boom",
            suppressed_count=0, ist_disp="10:00:00 IST",
        )
        kwargs.update(overrides)
        return order_failure_messages(**kwargs)

    def test_manual_label_in_all_three_outputs(self):
        tg_body, subject, email_body = self._call(agent_id=None)
        assert "[Manual]" in tg_body
        assert "[Manual]" in subject
        assert "Manual" in email_body

    def test_agent_bracket_label_in_all_three_outputs(self):
        tg_body, subject, email_body = self._call(
            source="template_wing_chase", agent_id=7,
        )
        assert "[Agent Bracket]" in tg_body
        assert "[Agent Bracket]" in subject
        assert "Agent Bracket" in email_body

    def test_default_agent_id_is_none(self):
        """No agent_id passed at all → correctly defaults to Manual."""
        from backend.shared.helpers.alert_utils import order_failure_messages
        tg_body, _, _ = order_failure_messages(
            masked="ZG####", symbol="X", exchange="NFO", side="SELL",
            qty=1, mode="live", source="chase", error="e",
            suppressed_count=0, ist_disp="t",
        )
        assert "[Manual]" in tg_body


class TestSendOrderFailureAlertThreadsAgentId:
    def test_agent_id_reaches_the_logged_extra_dict(self):
        from backend.shared.helpers import alert_utils

        captured = {}

        def _fake_warning(msg, extra=None, **kw):
            captured["msg"] = msg
            captured["extra"] = extra

        with patch.object(alert_utils, "logger") as mock_logger, \
             patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True), \
             patch.object(alert_utils, "_redis_cooldown_check", return_value=(False, 0, True)):
            mock_logger.warning.side_effect = _fake_warning
            alert_utils.send_order_failure_alert(
                account="ZG0790", symbol="NIFTY25OCTFUT", exchange="NFO",
                side="SELL", qty=50, mode="live", source="chase",
                error="boom", agent_id=99,
            )

        assert captured["extra"]["agent_id"] == 99
        assert "[Agent]" in captured["msg"]

    def test_no_agent_id_logs_manual(self):
        from backend.shared.helpers import alert_utils

        captured = {}

        def _fake_warning(msg, extra=None, **kw):
            captured["msg"] = msg
            captured["extra"] = extra

        with patch.object(alert_utils, "logger") as mock_logger, \
             patch("backend.api.helpers.snapshot_gate._any_segment_open", return_value=True), \
             patch.object(alert_utils, "_redis_cooldown_check", return_value=(False, 0, True)):
            mock_logger.warning.side_effect = _fake_warning
            alert_utils.send_order_failure_alert(
                account="ZG0790", symbol="NIFTY25OCTFUT", exchange="NFO",
                side="SELL", qty=50, mode="live", source="ticket",
                error="boom",
            )

        assert captured["extra"]["agent_id"] is None
        assert "[Manual]" in captured["msg"]


class TestEventAgentsRenderPassesAgentId:
    def test_render_order_failure_forwards_agent_id_from_extra(self):
        from backend.api.algo.event_agents import _render_order_failure

        rec = {
            "extra": {
                "masked": "ZG####", "symbol": "NIFTY25OCTFUT", "exchange": "NFO",
                "side": "SELL", "qty": 50, "mode": "live", "source": "chase",
                "error": "boom", "suppressed_count": 0, "ist_disp": "t",
                "agent_id": 7,
            }
        }
        with patch(
            "backend.shared.helpers.alert_utils.order_failure_messages",
        ) as mock_fn:
            mock_fn.return_value = ("tg", "subj", "email")
            _render_order_failure(rec)
        assert mock_fn.call_args.kwargs["agent_id"] == 7

    def test_render_order_failure_defaults_agent_id_none_when_absent(self):
        """Old, pre-fix persisted log records never had 'agent_id' in
        extra at all — .get() must not KeyError."""
        from backend.api.algo.event_agents import _render_order_failure

        rec = {
            "extra": {
                "masked": "ZG####", "symbol": "X", "exchange": "NFO",
                "side": "SELL", "qty": 1, "mode": "live", "source": "ticket",
                "error": "e", "suppressed_count": 0, "ist_disp": "t",
            }
        }
        with patch(
            "backend.shared.helpers.alert_utils.order_failure_messages",
        ) as mock_fn:
            mock_fn.return_value = ("tg", "subj", "email")
            _render_order_failure(rec)
        assert mock_fn.call_args.kwargs["agent_id"] is None


class TestChaseMaxAttemptsThreadsAgentId:
    @pytest.mark.asyncio
    async def test_exhaust_max_attempts_passes_agent_id_to_alert(self):
        from backend.api.algo.chase import _ch_exhaust_max_attempts, ChaseResult, ChaseConfig

        cfg = ChaseConfig(exchange="NFO", max_attempts=3)
        result = ChaseResult()
        captured = {}

        def _fake_alert(**kwargs):
            captured.update(kwargs)

        with patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
                   side_effect=_fake_alert), \
             patch("backend.api.algo.chase._run", new=AsyncMock()), \
             patch("backend.api.algo.chase._ch_write_order_event", new=AsyncMock()):
            await _ch_exhaust_max_attempts(
                result, None, cfg, "ZG0790", "NIFTY25OCTFUT", "SELL", 50,
                123, lambda *a, **k: None, agent_id=55,
            )

        assert captured.get("agent_id") == 55

    @pytest.mark.asyncio
    async def test_exhaust_max_attempts_defaults_agent_id_none(self):
        from backend.api.algo.chase import _ch_exhaust_max_attempts, ChaseResult, ChaseConfig

        cfg = ChaseConfig(exchange="NFO", max_attempts=3)
        result = ChaseResult()
        captured = {}

        def _fake_alert(**kwargs):
            captured.update(kwargs)

        with patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
                   side_effect=_fake_alert), \
             patch("backend.api.algo.chase._run", new=AsyncMock()), \
             patch("backend.api.algo.chase._ch_write_order_event", new=AsyncMock()):
            await _ch_exhaust_max_attempts(
                result, None, cfg, "ZG0790", "NIFTY25OCTFUT", "SELL", 50,
                123, lambda *a, **k: None,
            )

        assert captured.get("agent_id") is None


class TestManualTicketSourceMislabelFixed:
    def test_ticket_source_no_longer_prefixed_with_agent(self):
        """Pre-fix this literally said source='agent:manual:ticket' for a
        genuinely manual ticket placement — contradicting any source-based
        Manual/Agent split before agent_id even existed."""
        from backend.api.routes.orders_place import _opl_send_failure_alert

        captured = {}

        def _fake_alert(**kwargs):
            captured.update(kwargs)

        data = MagicMock()
        data.exchange = "NFO"
        with patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
                   side_effect=_fake_alert):
            _opl_send_failure_alert("ZG0790", "NIFTY25OCTFUT", data, "SELL", 50, "rejected")

        assert captured["source"] == "ticket"
        assert "agent_id" not in captured or captured["agent_id"] is None


class TestTemplatePlanParentAgentId:
    def test_default_is_none(self):
        from backend.api.algo.template_attach import TemplatePlan
        plan = TemplatePlan(
            template_id=1, template_name="default-bull", template_slug="default-bull",
            parent_account="ZG0790", parent_symbol="NIFTY25OCTFUT", parent_side="BUY",
            parent_qty=50, parent_exchange="NFO", parent_fill_price=100.0,
        )
        assert plan.parent_agent_id is None

    def test_to_dict_includes_parent_agent_id(self):
        from backend.api.algo.template_attach import TemplatePlan
        plan = TemplatePlan(
            template_id=1, template_name="default-bull", template_slug="default-bull",
            parent_account="ZG0790", parent_symbol="NIFTY25OCTFUT", parent_side="BUY",
            parent_qty=50, parent_exchange="NFO", parent_fill_price=100.0,
            parent_agent_id=9,
        )
        assert plan.to_dict()["parent_agent_id"] == 9
