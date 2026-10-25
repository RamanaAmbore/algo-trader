"""
Tests for cluster 1D of the 2026-10-23 orders/chase audit fix plan —
ntfy/alert-routing hygiene (audit items O4, O9, O11).

Covers:
  O4. `_opp_send_cancel_alert` (orders.py) must not fire a direct,
      unconditional, clock-default-priority ntfy push for EVERY
      CANCELLED postback — a chase-managed row currently inside an
      active `chase_order()` retry loop is routine cancel-and-replace,
      not action-required, and must be suppressed (ntfy skipped
      entirely). A genuinely unexpected / operator-initiated cancel
      must still reach ntfy, forced urgent.

  O9. ERROR_AGENT (event_agents.py) is a blanket catch-all matching ANY
      `logger.error(...)` call anywhere (log_store.py auto-tags every
      record with its level name). A log line that already fires its
      own specific operator alert must not ALSO double-fire via this
      generic catch-all — `extra={"self_alerted": True}` suppresses it
      for ERROR_AGENT specifically, leaving every other ERROR record
      (and every other agent) dispatching exactly as before. Chase's
      routine recoverable-retry line is downgraded to WARNING instead
      (it is not a failure at all, just noise at ERROR level).

  O11. An unmasked broker account number must never reach operator-
       facing alert text — `_ch_abort_ambiguous_place_timeout`'s abort
       message embedded a raw account; `order_failure_messages`'s
       `error` field was never passed through `mask_account_in_text`
       at all (inconsistent with `_render_chase_cancel`, which masks
       its whole body).
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio


# ─────────────────────────────────────────────────────────────────────────
# O4 — cancel-alert ntfy gating on chase's own active state
# ─────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def _cancel_alert_sqlite_factory():
    """Minimal in-memory AlgoOrder (id + broker_order_id only) so
    `_opp_cancel_is_own_chase_requote` can resolve a real row id from a
    broker_order_id, same pattern as test_order_poll.py's sqlite fixture."""
    from sqlalchemy import Column, Integer, String
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    class _Base(DeclarativeBase):
        pass

    class _AlgoOrder(_Base):
        __tablename__ = "algo_orders"
        id              = Column(Integer, primary_key=True, autoincrement=True)
        broker_order_id = Column(String(32), nullable=True)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import backend.api.models as _models
    orig = _models.AlgoOrder
    _models.AlgoOrder = _AlgoOrder  # type: ignore[assignment]

    yield factory, _AlgoOrder

    _models.AlgoOrder = orig  # type: ignore[assignment]
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


class TestCancelAlertChaseGate:
    async def test_chase_active_row_is_detected_as_own_requote(self, _cancel_alert_sqlite_factory):
        from backend.api.routes.orders import _opp_cancel_is_own_chase_requote
        from backend.api.algo.chase import _ch_mark_chase_active, _ch_mark_chase_inactive

        factory, AlgoOrder = _cancel_alert_sqlite_factory
        async with factory() as s:
            row = AlgoOrder(broker_order_id="BO-ALERT-1")
            s.add(row)
            await s.commit()
            row_id = row.id

        with patch("backend.api.database.async_session", factory):
            _ch_mark_chase_active(row_id)
            try:
                assert await _opp_cancel_is_own_chase_requote("BO-ALERT-1") is True
            finally:
                _ch_mark_chase_inactive(row_id)
            # Once chase is no longer active for this row, the SAME
            # broker_order_id must resolve to False.
            assert await _opp_cancel_is_own_chase_requote("BO-ALERT-1") is False

    async def test_unknown_order_id_is_never_treated_as_own_requote(self, _cancel_alert_sqlite_factory):
        from backend.api.routes.orders import _opp_cancel_is_own_chase_requote
        factory, _AlgoOrder = _cancel_alert_sqlite_factory
        with patch("backend.api.database.async_session", factory):
            assert await _opp_cancel_is_own_chase_requote("NO-SUCH-ORDER") is False

    async def test_none_order_id_short_circuits_without_db_hit(self):
        from backend.api.routes.orders import _opp_cancel_is_own_chase_requote
        # No DB patch at all — must not even attempt a lookup for falsy input.
        assert await _opp_cancel_is_own_chase_requote(None) is False
        assert await _opp_cancel_is_own_chase_requote("") is False

    async def test_cancel_alert_suppresses_ntfy_for_own_chase_requote(
        self, _cancel_alert_sqlite_factory, monkeypatch,
    ):
        """Regression guard for O4 — pre-fix, `_opp_send_cancel_alert`
        called `send_ntfy_alert` unconditionally for every CANCELLED
        event, including chase's own routine re-quote cancels. This
        must now be suppressed entirely."""
        from backend.api.routes.orders import _opp_send_cancel_alert
        from backend.api.algo.chase import _ch_mark_chase_active, _ch_mark_chase_inactive

        factory, AlgoOrder = _cancel_alert_sqlite_factory
        async with factory() as s:
            row = AlgoOrder(broker_order_id="BO-ALERT-2")
            s.add(row)
            await s.commit()
            row_id = row.id

        calls = []
        monkeypatch.setattr(
            "backend.shared.helpers.alert_utils.send_ntfy_alert",
            lambda *a, **kw: calls.append((a, kw)),
        )

        with patch("backend.api.database.async_session", factory):
            _ch_mark_chase_active(row_id)
            try:
                await _opp_send_cancel_alert(
                    "ZG0790", "RELIANCE", "BUY", 10, "cancelled by chase re-quote",
                    "BO-ALERT-2",
                )
            finally:
                _ch_mark_chase_inactive(row_id)

        assert calls == [], (
            "a CANCELLED event for a row currently under an active chase "
            "retry loop must NOT fire ntfy — it is chase's own routine "
            "cancel-and-replace, not action-required"
        )

    async def test_cancel_alert_sends_urgent_ntfy_when_not_chase_managed(
        self, _cancel_alert_sqlite_factory, monkeypatch,
    ):
        """A genuinely unexpected / operator-initiated cancel (no active
        chase for this row) must still reach ntfy, forced urgent."""
        from backend.api.routes.orders import _opp_send_cancel_alert

        factory, _AlgoOrder = _cancel_alert_sqlite_factory
        calls = []
        monkeypatch.setattr(
            "backend.shared.helpers.alert_utils.send_ntfy_alert",
            lambda *a, **kw: calls.append((a, kw)),
        )

        with patch("backend.api.database.async_session", factory):
            await _opp_send_cancel_alert(
                "ZG0790", "RELIANCE", "BUY", 10, "cancelled by operator",
                "BO-ALERT-3",
            )

        assert len(calls) == 1, "a non-chase-managed cancel must still alert"
        args, kwargs = calls[0]
        # send_ntfy_alert(title, message, priority) — priority is the 3rd
        # positional arg.
        assert args[2] == "urgent", (
            "a genuinely action-required cancel must force urgent priority, "
            "not fall through to send_ntfy_alert's clock-based day/night default"
        )


# ─────────────────────────────────────────────────────────────────────────
# O9 — ERROR_AGENT self_alerted gate
# ─────────────────────────────────────────────────────────────────────────

def _error_rec(message: str = "something failed", **extra) -> dict:
    # `alert_now=True` bypasses ERROR_AGENT's own unrelated repeat-gate
    # (3 occurrences within a 900s window before it alerts at all) — not
    # what these tests are about; each test also uses a distinct
    # `message` so they never collide on the SAME gate_key via the
    # process-wide RepeatGate singleton.
    extra.setdefault("alert_now", True)
    return {
        "id": 1, "level": "ERROR", "tags": ["error", "chase"],
        "logger": "backend.api.algo.chase", "message": message,
        "extra": extra,
    }


def _error_agent() -> SimpleNamespace:
    from backend.api.algo import event_agents
    d = event_agents.ERROR_AGENT
    return SimpleNamespace(
        slug=d["slug"], conditions=d["conditions"], events=d.get("events", []), actions=d["actions"],
    )


@pytest.fixture
def error_senders(monkeypatch):
    from backend.api.algo import event_agents
    import backend.shared.helpers.utils as u
    calls = {"ntfy": [], "tg": []}
    monkeypatch.setattr(u, "is_enabled", lambda cap: True)
    monkeypatch.setitem(event_agents.CHANNELS, "ntfy",
                        ("ntfy", lambda t, b, *_: calls["ntfy"].append((t, b))))
    monkeypatch.setitem(event_agents.CHANNELS, "telegram",
                        ("telegram", lambda t, b, *_: calls["tg"].append((t, b))))
    return calls


class TestErrorAgentSelfAlertedGate:
    async def test_self_alerted_record_does_not_double_fire(self, error_senders):
        """Regression guard for O9 — pre-fix, ERROR_AGENT's blanket
        catch-all condition matched EVERY ERROR-tagged record with no
        way to suppress one that already alerted via its own specific
        path."""
        from backend.api.algo import event_agents
        rec = _error_rec("already-alerted failure 1", self_alerted=True)
        sent = await event_agents.dispatch([rec], [_error_agent()])
        assert sent == 0
        assert error_senders == {"ntfy": [], "tg": []}

    async def test_normal_error_record_still_dispatches(self, error_senders):
        """The gate must be scoped to self_alerted records only — a
        genuine, not-already-alerted ERROR record keeps firing exactly
        as before (both telegram + ntfy)."""
        from backend.api.algo import event_agents
        rec = _error_rec("genuine failure 2")
        sent = await event_agents.dispatch([rec], [_error_agent()])
        assert sent == 2
        assert len(error_senders["tg"]) == 1
        assert len(error_senders["ntfy"]) == 1

    async def test_self_alerted_flag_does_not_affect_other_agents(self, error_senders):
        """The gate is scoped to ERROR_AGENT's own slug — any other
        agent matching the same record (hypothetically) must be
        unaffected."""
        from backend.api.algo import alert_routing, event_agents
        # 2026-10 routing-tree plan — `ERROR_AGENT` no longer carries a
        # literal `events` key; resolve its REAL channel set via
        # `alert_routing.resolve_channels()` (same call `_dispatch_agent`
        # itself makes) and assign it explicitly here, so this
        # differently-slugged test double genuinely mirrors
        # ERROR_AGENT's own real channels rather than accidentally
        # falling through to the mandatory catch-all (a coincidentally
        # same-length but differently-shaped list).
        error_channels = alert_routing.resolve_channels(
            name=event_agents.ERROR_AGENT["slug"], priority=event_agents.ERROR_AGENT.get("priority"))
        other = SimpleNamespace(
            slug="some-other-agent",
            conditions=event_agents.ERROR_AGENT["conditions"],
            events=error_channels,
            actions=event_agents.ERROR_AGENT["actions"],
        )
        rec = _error_rec("already-alerted failure 3", self_alerted=True)
        sent = await event_agents.dispatch([rec], [other])
        assert sent == 2, "self_alerted must only suppress ERROR_AGENT itself, not other agents"

    def test_error_agent_self_alerted_helper_direct(self):
        from backend.api.algo.event_agents import _error_agent_self_alerted, ERROR_AGENT
        agent = SimpleNamespace(slug=ERROR_AGENT["slug"])
        assert _error_agent_self_alerted(agent, {"extra": {"self_alerted": True}}) is True
        assert _error_agent_self_alerted(agent, {"extra": {}}) is False
        assert _error_agent_self_alerted(agent, {}) is False
        other = SimpleNamespace(slug="not-error-agent")
        assert _error_agent_self_alerted(other, {"extra": {"self_alerted": True}}) is False


# ─────────────────────────────────────────────────────────────────────────
# 2026-10-24 — CRITICAL-alert tag mismatch fix
#
# `log_store.py:tags_for()` auto-tagged every record with its own level
# name lowercased — a bare `logger.critical(...)` was tagged "critical",
# never "error", so ERROR_AGENT's condition (`tag="error",
# min_level="ERROR"`) never matched a CRITICAL record at all, regardless
# of severity. Fixed by having `tags_for()` also stamp "error" on a
# CRITICAL record (see that function's own docstring for why the fix
# lives there rather than in ERROR_AGENT's stored `conditions` dict —
# no SEED_VERSION bump needed). `_error_agent_self_alerted` was extended
# so a CRITICAL record that already carries its own `alert_event`
# (routed to a dedicated agent — chase's `cancel_unconfirmed`, the GTT
# wing `wing_hard_reject`/`partial_gtt` agents) doesn't ALSO double-fire
# through this generic catch-all.
# ─────────────────────────────────────────────────────────────────────────

def _critical_rec(message: str = "something very bad", **extra) -> dict:
    """Builds the rec dict the way the real pipeline actually would —
    tags computed via the real `tags_for()` against a real
    `logging.LogRecord`, not a hand-typed list — so a regression in
    `tags_for()` breaks this test too, not just a hardcoded fixture."""
    import logging as _logging
    from backend.shared.helpers import log_store as _log_store

    extra.setdefault("alert_now", True)
    raw = _logging.LogRecord(
        "backend.api.algo.chase", _logging.CRITICAL, __file__, 1, message, None, None,
    )
    if "tags" in extra:
        raw.tags = extra["tags"]
    tags = _log_store.tags_for(raw)
    return {"id": 2, "level": "CRITICAL", "tags": tags, "logger": raw.name,
            "message": message, "extra": extra}


class TestCriticalAlertReachesErrorAgent:
    async def test_bare_critical_record_now_reaches_error_agent(self, error_senders):
        """Regression guard — pre-fix, a bare `logger.critical(...)` with
        no other alert metadata never matched ERROR_AGENT's condition at
        all (tagged "critical", not "error") and silently never alerted.
        Confirmed to fail against pre-fix `tags_for()` (which omits the
        "error" tag for a CRITICAL record): the resulting tags list would
        be `["critical", "chase"]`, missing the "error" tag ERROR_AGENT's
        condition requires, so `dispatch` would return 0."""
        from backend.api.algo import event_agents
        rec = _critical_rec("ticker died hard", tags=["chase"])
        assert "error" in rec["tags"], "tags_for() must stamp a CRITICAL record with 'error'"
        sent = await event_agents.dispatch([rec], [_error_agent()])
        assert sent == 2
        assert len(error_senders["tg"]) == 1
        assert len(error_senders["ntfy"]) == 1

    async def test_critical_record_with_alert_event_does_not_double_fire_via_error_agent(
        self, error_senders,
    ):
        """A CRITICAL record already routed to its own dedicated agent
        (carries `alert_event`) must not ALSO fire through ERROR_AGENT's
        generic catch-all — same double-alert class as O9's
        `self_alerted` guard above, extended to cover this newly-widened
        match set."""
        from backend.api.algo import event_agents
        rec = _critical_rec("chase cancel not confirmed", tags=["chase"],
                             alert_event="cancel_unconfirmed")
        sent = await event_agents.dispatch([rec], [_error_agent()])
        assert sent == 0
        assert error_senders == {"ntfy": [], "tg": []}

    def test_error_agent_self_alerted_helper_covers_critical_with_alert_event(self):
        from backend.api.algo.event_agents import _error_agent_self_alerted, ERROR_AGENT
        agent = SimpleNamespace(slug=ERROR_AGENT["slug"])
        assert _error_agent_self_alerted(
            agent, {"level": "CRITICAL", "extra": {"alert_event": "cancel_unconfirmed"}},
        ) is True
        # A bare CRITICAL record with no alert_event is NOT presumed
        # already-routed — it must fall through to ERROR_AGENT.
        assert _error_agent_self_alerted(agent, {"level": "CRITICAL", "extra": {}}) is False
        # An ERROR-level record with alert_event is unaffected — the
        # extended guard is scoped to CRITICAL only, matching the actual
        # dedicated-agent pattern (every CRITICAL-tagged dedicated agent
        # uses an `alert_event` where-filter; ERROR-level ones already
        # had the pre-existing explicit `self_alerted` convention).
        assert _error_agent_self_alerted(
            agent, {"level": "ERROR", "extra": {"alert_event": "order_failure"}},
        ) is False

    def test_tags_for_regression_guard_without_the_fix(self):
        """Explicitly demonstrates the pre-fix tag shape would never
        have matched — proves this test suite would catch a regression
        if the "error" stamp were ever removed from `tags_for()`."""
        import logging as _logging
        from backend.shared.helpers import log_store as _log_store

        rec = _logging.LogRecord(
            "backend.api.algo.chase", _logging.CRITICAL, __file__, 1, "x", None, None,
        )
        pre_fix_tags = ["critical", "chase"]  # what tags_for() used to return
        real_tags = _log_store.tags_for(rec)
        assert "error" not in pre_fix_tags
        assert "error" in real_tags


# ─────────────────────────────────────────────────────────────────────────
# O9 — specific call-site fixes: downgrade / self_alerted tagging
# ─────────────────────────────────────────────────────────────────────────

class TestChaseLogLevelSourceFixes:
    def test_recoverable_retry_is_logged_at_warning_not_error(self):
        """Regression guard — pre-fix this was `logger.error`, matching
        ERROR_AGENT's blanket catch-all on EVERY recoverable retry
        (a single low-tier chase could generate dozens)."""
        import backend.api.algo.chase as chase
        src = inspect.getsource(chase._ch_handle_attempt_error)
        assert 'logger.warning(\n        f"Chase {symbol}: attempt {attempt} recoverable error' in src, (
            "the recoverable-error retry log line must be logger.warning, "
            "not logger.error — it is routine, auto-retried, non-fatal noise"
        )
        assert 'logger.error(\n        f"Chase {symbol}: attempt {attempt} recoverable error' not in src

    def test_exhaustion_path_marks_self_alerted(self):
        """Regression guard — pre-fix this ERROR line had no
        self_alerted marker, so it double-fired alongside its own
        explicit send_order_failure_alert call."""
        import backend.api.algo.chase as chase
        src = inspect.getsource(chase._ch_exhaust_max_attempts)
        assert 'extra={"self_alerted": True}' in src
        assert "FAILED after {cfg.max_attempts} attempts" in src


class TestActionsLiveSelfAlerted:
    def test_close_position_on_failure_marks_self_alerted(self):
        """Regression guard — pre-fix, close_position's failure path
        logged at ERROR with no self_alerted marker, double-firing
        alongside its own explicit send_order_failure_alert call."""
        import backend.api.algo.actions_live as actions_live
        src = inspect.getsource(actions_live._close_position_on_failure)
        assert 'extra={"self_alerted": True}' in src
        assert "close_position failed for" in src


# ─────────────────────────────────────────────────────────────────────────
# O11 — account masking in operator-facing alert text
# ─────────────────────────────────────────────────────────────────────────

class TestAccountMaskingInAlertText:
    async def test_ambiguous_place_timeout_message_does_not_leak_raw_account(self):
        """Regression guard — pre-fix, `_ch_abort_ambiguous_place_timeout`
        embedded the RAW account number directly in its abort message,
        which flows unmasked into the operator-facing order-failure
        alert via `send_order_failure_alert(error=abort_msg, ...)`."""
        from backend.api.algo.chase import (
            _ch_abort_ambiguous_place_timeout, ChaseResult, ChaseConfig,
        )

        result = ChaseResult()
        cfg = ChaseConfig(exchange="NFO")
        raw_account = "ZJ6294"

        with patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as mock_alert:
            await _ch_abort_ambiguous_place_timeout(
                result, raw_account, "RELIANCE", "BUY", 10, cfg,
                None, lambda *a, **kw: None, 3,
            )

        assert raw_account not in result.detail, (
            "the chase result's own detail text must not carry the raw "
            "unmasked account number"
        )
        assert mock_alert.called
        error_arg = mock_alert.call_args.kwargs.get("error", "")
        assert raw_account not in error_arg, (
            "the error text handed to send_order_failure_alert must not "
            "carry the raw unmasked account number"
        )

    def test_order_failure_messages_masks_account_embedded_in_error_text(self):
        """Defense-in-depth regression guard — `order_failure_messages`
        (alert_utils.py) must mask any account-shaped token embedded in
        the free-form `error` string, matching the convention
        `_render_chase_cancel` already follows for its own body."""
        from backend.shared.helpers.alert_utils import order_failure_messages

        raw_account = "ZJ6294"
        tg_body, subject, email_body = order_failure_messages(
            masked="Z2####", symbol="RELIANCE", exchange="NSE", side="BUY",
            qty=10, mode="live", source="chase",
            error=f"Manually check {raw_account}'s order book for RELIANCE.",
            suppressed_count=0, ist_disp="10:00:00 IST",
        )
        assert raw_account not in tg_body
        assert raw_account not in email_body
        assert raw_account not in subject
