"""
Regression test — live incident, 2026-11-27 (MCX GOLD26NOV expiry night).

`expiry-day-mcx-itm-auto-close` fired 3 times during its own pre-close
window (22:54, 23:25, 23:55 IST) and was SKIPPED all 3 times with
`skip_reason: "exchange closed: MCX"`, even though MCX was genuinely
open the whole time (its real winter-DST close that night was 23:55
IST, per `exchange_schedule`).

Root cause, confirmed empirically against the real prod/dev code
(see `.claude/` session notes / handback report for the live SSH +
DB investigation): `backend.api.algo.actions._exchange_gate_passes`
read `context.get('nse_open')` / `context.get('mcx_open')` directly off
`run_cycle`'s TOP-LEVEL `context` dict. `run_cycle` computes the real
open/closed flags into a local `base_ctx` and only ever merges them
into `context["segment_flags"]` (a NESTED dict) — it never flattens
them onto `context` itself. So every action fired by a saved cycle
agent (the only real caller of `actions.execute()` outside a bare-unit
test) read both flags as `None` → `bool(None)` == `False` → every
exchange read as closed, unconditionally, forever — this has been true
since the gate's own "fix" commit (`dbe6f1100`, 2026-05-26), but stayed
INVISIBLE until 2026-11-26 wired a real `exchange` param onto
`expiry_auto_close`'s action for the first time (before that,
`_action_target_exchanges("expiry_auto_close", {}, ...)` returned `[]`
and the gate was a no-op).

Every PRE-EXISTING test for this gate (`test_actions_coverage.py`'s
`test_exchange_gate_passes_open_exchange_allowed`/`_closed_exchange_
blocked`, and `test_actions.py`'s dispatch tests) either patched
`_symbol_exchange_open` itself or patched `_exchange_gate_passes`
wholesale — none of them exercised the REAL context shape `run_cycle`
actually builds, which is exactly how this bug hid for 6 months. These
tests build that real shape (via the real `_build_context()` /
`_ae_segment_flags()`) and drive the real `_exchange_gate_passes` →
`_symbol_exchange_open` → `execute()` → `_ae_dispatch_actions` chain
with NO mocking of any of those functions themselves.
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")

# Thursday, not a holiday on NSE or MCX, inside the US winter (non-DST)
# window — MCX's real non-agri close that day is 23:55 IST (DST-aware),
# matching the live incident's own season. Mirrors the fixture already
# used by test_market_cycle_tokens.py / test_order_gate_mcx_dst_aware.py.
_PLAIN_WEEKDAY = date(2026, 11, 5)


def _at(hh: int, mm: int) -> datetime:
    return datetime(
        _PLAIN_WEEKDAY.year, _PLAIN_WEEKDAY.month, _PLAIN_WEEKDAY.day,
        hh, mm, tzinfo=IST,
    )


def _fake_fetch_holidays(exchange: str) -> set:
    return set()


def _fake_fetch_special_sessions(exchange: str) -> list:
    return []


@pytest.fixture(autouse=True)
def _patch_holiday_sources():
    with patch("backend.brokers.broker_apis.fetch_holidays",
               side_effect=_fake_fetch_holidays), \
         patch("backend.brokers.broker_apis.fetch_special_sessions",
               side_effect=_fake_fetch_special_sessions):
        yield


def _real_run_cycle_shaped_context(now: datetime) -> dict:
    """Build a `context` dict exactly the way `run_cycle` really does,
    right before it enters the per-agent loop — i.e. a background-task-
    shaped dict (only `now` + unrelated bookkeeping keys, NO flat
    `nse_open`/`mcx_open`) plus the real `base_ctx` nested under
    `segment_flags`, via the actual `_build_context()`. This is the
    exact shape `_ae_dispatch_actions`'s `action_ctx = dict(context)`
    then hands to `execute()` on the real live path."""
    from backend.api.algo.agent_engine import _build_context

    base_ctx = _build_context(now)
    # Mirrors background.py's `_perf_run_agent_engine`'s own
    # `agent_context` dict shape — deliberately NO flat nse_open/mcx_open
    # keys anywhere on this top-level dict, matching the real bug.
    context = {
        "now": now,
        "sum_holdings": None,
        "sum_positions": None,
        "df_margins": None,
    }
    context["segment_flags"] = dict(base_ctx)
    return context


def test_mcx_is_genuinely_open_at_the_incident_timestamp():
    """Pre-flight sanity: 23:50 IST on this fixture date is inside MCX's
    real winter-DST session (23:55 close) — if this fails, the rest of
    this file is testing the wrong clock, not the real bug."""
    from backend.api.algo.agent_engine import _build_context

    base_ctx = _build_context(_at(23, 50))
    assert base_ctx["mcx_open"] is True, base_ctx


class TestExchangeGatePassesReadsRealSegmentFlags:
    """Drives the REAL `_exchange_gate_passes` (no mocking of it, or of
    `_symbol_exchange_open`) against the context shape `run_cycle`
    actually produces."""

    def test_regression_mcx_action_no_longer_falsely_blocked(self):
        from backend.api.algo.actions import _exchange_gate_passes

        now = _at(23, 50)  # MCX genuinely open (real close 23:55)
        context = _real_run_cycle_shaped_context(now)

        allowed, reason = _exchange_gate_passes(
            "expiry_auto_close", {"exchange": "MCX"}, context,
        )
        assert allowed is True, (
            f"expected MCX action to be allowed (MCX is open) but gate "
            f"blocked it: {reason!r} — this is the exact live incident "
            f"(2026-11-27, expiry-day-mcx-itm-auto-close skipped 3x with "
            f"'exchange closed: MCX' while MCX was genuinely open)"
        )
        assert reason == ""

    def test_genuinely_closed_exchange_is_still_blocked(self):
        """Not a tautological fix — a GENUINELY closed exchange (NSE at
        23:50 IST) must still be blocked through the same real code
        path, proving the fix reads real data rather than defaulting to
        always-allow."""
        from backend.api.algo.actions import _exchange_gate_passes

        now = _at(23, 50)  # NSE closed (regular session ends 15:30)
        context = _real_run_cycle_shaped_context(now)

        allowed, reason = _exchange_gate_passes(
            "place_order", {"exchange": "NSE"}, context,
        )
        assert allowed is False
        assert "NSE" in reason

    def test_flat_context_without_segment_flags_still_works(self):
        """Back-compat: a caller that hands `_exchange_gate_passes` an
        already-flat dict (no `segment_flags` key at all — e.g.
        `_build_now_ctx()`'s own direct output) must still resolve
        correctly via the fallback-to-`context`-itself branch."""
        from backend.api.algo.actions import _exchange_gate_passes

        flat_ctx = {"mcx_open": True, "nse_open": False}
        allowed, _reason = _exchange_gate_passes(
            "expiry_auto_close", {"exchange": "MCX"}, flat_ctx,
        )
        assert allowed is True


class TestActionsExecuteEndToEnd:
    """One level up from `_exchange_gate_passes` — drives the real
    `actions.execute()` (the function every saved agent's action chain
    actually calls) with the real gate wired in, proving the handler is
    actually REACHED, not just that the gate function in isolation
    returns the right tuple."""

    @pytest.mark.asyncio
    async def test_live_incident_scenario_action_is_dispatched_not_skipped(self):
        from backend.api.algo.actions import execute

        now = _at(23, 50)
        context = _real_run_cycle_shaped_context(now)
        context["sim_mode"] = False
        context["account"] = "TOTAL"

        agent = MagicMock()
        agent.id = 119
        agent.slug = "expiry-day-mcx-itm-auto-close"
        agent.name = "Expiry auto-close (MCX, ITM)"
        agent.kind = "cycle"
        agent.trade_mode = "live"

        actions = [{"type": "expiry_auto_close", "params": {"exchange": "MCX"}}]

        with patch("backend.api.algo.actions._al_dispatch_by_mode",
                   new=AsyncMock(return_value=True)) as mock_dispatch, \
             patch("backend.api.algo.actions._log_action_success",
                   new=AsyncMock()), \
             patch("backend.api.algo.events.log_event", new=AsyncMock()) as mock_log:
            await execute(agent, actions, context)

        mock_dispatch.assert_called_once()
        # No `action_skipped` row should have been written for this fire.
        for call in mock_log.call_args_list:
            assert call.args[1] != "action_skipped", (
                f"action was skipped instead of dispatched: {call.args}"
            )

    @pytest.mark.asyncio
    async def test_pre_fix_shape_would_have_skipped(self):
        """Documents the bug directly: feeding `execute()` the OLD
        (wrong) assumption — a context with the flags flattened only at
        a `segment_flags`-less top level that `_symbol_exchange_open`
        can't see — reproduces the live skip. This is what the
        background cycle's top-level `context` looked like to the gate
        before the fix (i.e. with no usable flag data at all)."""
        from backend.api.algo.actions import execute
        from backend.api.algo.agent_engine import _build_context

        now = _at(23, 50)
        base_ctx = _build_context(now)
        assert base_ctx["mcx_open"] is True

        # The exact pre-fix failure mode: a context dict carrying
        # NEITHER a flat `mcx_open` key NOR a `segment_flags` mirror —
        # this is what `_build_now_ctx()`-less, `segment_flags`-less
        # background code effectively handed the gate for the entire
        # life of this feature.
        context = {"now": now, "sim_mode": False, "account": "TOTAL"}

        agent = MagicMock()
        agent.id = 119
        agent.slug = "expiry-day-mcx-itm-auto-close"
        agent.trade_mode = "live"
        actions = [{"type": "expiry_auto_close", "params": {"exchange": "MCX"}}]

        with patch("backend.api.algo.actions._al_dispatch_by_mode",
                   new=AsyncMock(return_value=True)) as mock_dispatch, \
             patch("backend.api.algo.events.log_event", new=AsyncMock()) as mock_log:
            await execute(agent, actions, context)

        mock_dispatch.assert_not_called()
        skipped = [c for c in mock_log.call_args_list if c.args[1] == "action_skipped"]
        assert skipped, "expected an action_skipped event for the segment-flags-less context"


class TestAeDispatchActionsEndToEnd:
    """One more level up — `_ae_dispatch_actions` is the exact function
    `_ae_dispatch_survivor_entry` calls on the real post-loop dispatch
    path inside `run_cycle`. Proves the fix holds through that call
    site's own `action_ctx = dict(context)` copy, not just inside a
    hand-built context passed straight to `execute()`."""

    @pytest.mark.asyncio
    async def test_dispatch_actions_reaches_handler_when_mcx_open(self):
        from backend.api.algo.agent_engine import _ae_dispatch_actions

        now = _at(23, 50)
        context = _real_run_cycle_shaped_context(now)

        agent = MagicMock()
        agent.id = 119
        agent.slug = "expiry-day-mcx-itm-auto-close"
        agent.name = "Expiry auto-close (MCX, ITM)"
        agent.kind = "cycle"
        agent.trade_mode = "live"
        agent.actions = [{"type": "expiry_auto_close", "params": {"exchange": "MCX"}}]

        with patch("backend.api.algo.actions._al_dispatch_by_mode",
                   new=AsyncMock(return_value=True)) as mock_dispatch, \
             patch("backend.api.algo.actions._log_action_success",
                   new=AsyncMock()), \
             patch("backend.api.algo.events.log_event", new=AsyncMock()):
            await _ae_dispatch_actions({}, agent, context, False)

        mock_dispatch.assert_called_once()


class TestSymbolExchangeOpenMissingKeyIsLoud:
    """Defense-in-depth: a genuinely absent flag key must never be
    silently treated the same as a confirmed-False/closed value (the
    missing-vs-zero convention) — it must log CRITICAL so ERROR_AGENT's
    catch-all can surface a future recurrence of this exact bug class
    immediately, instead of only a routine INFO `action_skipped` line
    that reads as normal/expected."""

    def test_missing_key_logs_critical_and_fails_closed(self):
        # ramboq_logger.get_logger() sets `propagate = False`, so
        # pytest's `caplog` (which hooks the root logger) never sees
        # these records — patch the module's own `logger.critical`
        # directly instead.
        from backend.api.algo import agent_engine as ae

        with patch.object(ae.logger, "critical") as mock_critical:
            result = ae._symbol_exchange_open("MCX", {"nse_open": True})  # no mcx_open key

        assert result is False
        mock_critical.assert_called_once()
        assert "mcx_open" in mock_critical.call_args.args[0]

    def test_present_but_false_key_does_not_log_critical(self):
        """A genuinely-closed exchange (key present, value False) is the
        normal case and must NOT trip the missing-key alarm."""
        from backend.api.algo import agent_engine as ae

        with patch.object(ae.logger, "critical") as mock_critical:
            result = ae._symbol_exchange_open("MCX", {"mcx_open": False, "nse_open": True})

        assert result is False
        mock_critical.assert_not_called()
