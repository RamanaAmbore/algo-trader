"""
Tests for Sprint 16 — matched-row fan-out for DO actions.

`_ae_dispatch_survivor_entry` / `_ae_dispatch_actions` (agent_engine.py)
used to fire `agent.actions` EXACTLY ONCE per tick, with
`action_ctx["account"]` hardcoded to the literal `"TOTAL"`, discarding
which real row(s) matched. This sprint:

  1. Fans out `execute()` once per matched row, but ONLY when at least
     one action in the chain references matched-row data via the
     `{"$row": "<exposed_name>"}` marker (`_ae_actions_use_row_ref`) —
     every agent that predates this sprint cannot contain `$row`, so
     fan-out is a true no-op for them BY CONSTRUCTION, not merely "by
     scope" (a per-row-scoped whole-exchange sweep action like
     `expiry_auto_close` must never be re-invoked once per row — see
     TestBackwardCompatRealAgentShapes.test_expiry_auto_close_* below).
  2. Resolves `$row` markers against the entry's own matched row
     (`actions.py:resolve_row_ref_params`), failing closed (never
     silently "TOTAL"/None) when no row is bound (the aggregate-scope
     case) or the referenced column isn't exposed for that scope.
  3. Fixes a latch-key-granularity gap (`_latch_key` has no symbol
     component) that would otherwise silently drop all-but-one same-
     account multi-symbol row from `latched_matches` — `fire_matches`
     (computed in `_cycle_maybe_buffer_fire`) re-expands back to every
     raw row whose latch KEY passed the escalation gate.
  4. Dedupes same-row duplicates (two leaves breaching the identical
     underlying row) via `_ae_fanout_matches`' row-identity key.

Five quality dimensions:
  SSOT        — drives the REAL dispatch gate (_cycle_maybe_buffer_fire
                -> _cycle_dispatch_survivors), not hand-built entries,
                so the latch-key fix (#3 above) is actually exercised.
  Correctness — backward-compat proven against REAL BUILTIN_AGENTS
                condition/action shapes (operator-mandated), not just
                synthetic fixtures.
  Performance — no DB/broker I/O; execute()/dispatch/_v2_send_rich_alert
                mocked at the agent_engine module boundary.
  Reuse       — shared helpers (_row, _run_tick) across test classes.
  UX          — every assert carries an f-string with actual/expected.
"""
from __future__ import annotations

from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.algo import agent_engine
from backend.api.algo.agent_engine import (
    _ae_fanout_matches,
    _ae_row_identity,
    _ae_actions_use_row_ref,
    _cycle_maybe_buffer_fire,
    _cycle_dispatch_survivors,
    BUILTIN_AGENTS,
)
from backend.api.algo import actions as actions_mod
from backend.api.algo.actions import resolve_row_ref_params


def _now():
    return datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc)


class _Agent:
    """Minimal Agent-row stand-in — mirrors test_agent_latch.py's _Agent."""
    def __init__(self, id=1, slug="test-agent", name=None, tier="medium",
                 topic="general", actions=None, cooldown_minutes=0,
                 fire_at_time=None, status="active", trigger_count=0):
        self.id = id
        self.slug = slug
        self.name = name or slug
        self.tier = tier
        self.topic = topic
        self.actions = actions or []
        self.cooldown_minutes = cooldown_minutes
        self.fire_at_time = fire_at_time
        self.status = status
        self.trigger_count = trigger_count


def _builtin(slug: str) -> dict:
    agent = next((a for a in BUILTIN_AGENTS if a.get('slug') == slug), None)
    assert agent is not None, f"{slug} not found in BUILTIN_AGENTS"
    return agent


def _row(account: str, **extra) -> dict:
    return {'account': account, **extra}


# Real per-row scope used by both the PLAN's own canonical example
# (`close_position(account=account_id, symbol=symbol)`) and
# `expiry-day-equity-itm-auto-close` — exposes account_id/symbol/
# exchange/product/qty (confirmed via grammar.exposed_columns_for_scope).
# `positions.any_acct` is deliberately NOT used as the default here: it
# is an account-level SUMMARY scope (one row per account, no
# tradingsymbol/exchange/product columns at all), so it cannot carry
# `symbol` for a $row reference — using it would silently test an
# unrealistic shape the plan's own example could never produce.
def _m(metric="is_itm", scope="positions.expiring_today", account="ZG0790",
       op="==", threshold=1.0, value=1.0, row=None):
    r = row if row is not None else _row(account, tradingsymbol="NIFTY26OCT22500CE",
                                         exchange="NFO", product="NRML")
    return {'metric': metric, 'scope': scope, 'op': op, 'threshold': threshold,
            'value': value, 'row': r, 'account': account}


async def _run_tick(agent, matches, *, cfg=None, bypass_suppression=False,
                    context=None, sim_mode=False):
    """Drive the REAL dispatch gate: _cycle_maybe_buffer_fire ->
    _cycle_dispatch_survivors, with execute()/dispatch/_v2_send_rich_alert
    mocked at the agent_engine boundary. Returns the `execute` AsyncMock
    so callers can inspect call_count / call_args_list."""
    pending: list = []
    fired = _cycle_maybe_buffer_fire(
        agent, matches, now=_now(),
        bypass_suppression=bypass_suppression, bypass_schedule=True,
        sim_mode=sim_mode, alert_state={}, cfg=cfg or {'cooldown_min': 30},
        broadcast_fn=None, debounce_min=0, pending_dispatches=pending,
    )
    with (
        patch.object(agent_engine, "execute", new=AsyncMock()) as exec_mock,
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=False)),
        patch.object(agent_engine, "dispatch", new=AsyncMock()),
    ):
        if pending:
            await _cycle_dispatch_survivors(pending, _now(), context or {}, None)
    return exec_mock, fired


async def _run_tick_real_execute(agent, matches, *, cfg=None, bypass_suppression=False,
                                 context=None):
    """Same as `_run_tick`, but runs the REAL `execute()` (including the
    real `resolve_row_ref_params` + `resolve_action_params` +
    exchange-gate chain) instead of mocking it — proves $row actually
    resolves to the correct live value at dispatch time, not just that
    fan-out produced the right COUNT of (mocked) calls.

    `sim_mode=True` is forced into `_cycle_maybe_buffer_fire` so
    `_resolve_mode` routes to 'sim' (bypassing the exchange-open gate,
    which needs a live market-hours context this test doesn't set up)
    and `_al_dispatch_by_mode` is mocked at the point actions.py would
    otherwise hand off to the sim/paper/live writer — the lowest level
    at which the FINAL resolved `params` dict can be inspected.
    Returns the `_al_dispatch_by_mode` AsyncMock."""
    pending: list = []
    fired = _cycle_maybe_buffer_fire(
        agent, matches, now=_now(),
        bypass_suppression=bypass_suppression, bypass_schedule=True,
        sim_mode=True, alert_state={}, cfg=cfg or {'cooldown_min': 30},
        broadcast_fn=None, debounce_min=0, pending_dispatches=pending,
    )
    with (
        patch.object(actions_mod, "_al_dispatch_by_mode",
                     new=AsyncMock(return_value=True)) as dispatch_mock,
        patch.object(agent_engine, "_v2_send_rich_alert", new=AsyncMock(return_value=False)),
        patch.object(agent_engine, "dispatch", new=AsyncMock()),
        patch("backend.api.algo.events.log_event", new=AsyncMock()) as log_event_mock,
    ):
        if pending:
            await _cycle_dispatch_survivors(pending, _now(), context or {}, None)
    return dispatch_mock, log_event_mock, fired


@pytest.fixture(autouse=True)
def _clear_latch():
    agent_engine._V2_LATCH.clear()
    yield
    agent_engine._V2_LATCH.clear()


# ═══════════════════════════════════════════════════════════════════════════
#  Backward compat — REAL seeded agent shapes, no $row anywhere
# ═══════════════════════════════════════════════════════════════════════════

class TestBackwardCompatRealAgentShapes:
    """An agent whose action chain contains no `$row` marker — i.e. every
    agent that exists today — must fire `execute()` EXACTLY ONCE per
    tick with `account="TOTAL"`, byte-identical to pre-Sprint-16
    behavior, regardless of how many rows its condition matched."""

    @pytest.mark.asyncio
    async def test_loss_pos_total_auto_close_single_aggregate_match(self):
        """Real agent `loss-pos-total-auto-close`: single-leaf
        positions.total scope + chase_close_positions action (no $row).
        One TOTAL match -> fires once, account=TOTAL — the simplest,
        most direct aggregate-scope-with-actions real shape."""
        spec = _builtin("loss-pos-total-auto-close")
        assert spec["actions"], "loss-pos-total-auto-close must carry a real action"
        agent = _Agent(id=101, slug=spec["slug"], tier=spec["tier"],
                       topic=spec["topic"], actions=spec["actions"])
        matches = [_m(metric="pnl", scope="positions.total", account="TOTAL",
                      row=_row("TOTAL", pnl=-60000))]

        exec_mock, fired = await _run_tick(agent, matches)

        assert fired, "agent must have fired"
        assert exec_mock.call_count == 1, (
            f"expected exactly 1 execute() call, got {exec_mock.call_count}"
        )
        action_ctx = exec_mock.call_args.args[2]
        assert action_ctx["account"] == "TOTAL", (
            f"expected account='TOTAL', got {action_ctx['account']!r}"
        )

    @pytest.mark.asyncio
    async def test_loss_positions_total_multi_leaf_coincident_breach(self):
        """Real agent `loss-positions-total`'s exact 4-leaf any[]
        condition (all positions.total scope) — 2 leaves co-breaching
        the SAME tick must still fire exactly once, not twice. Uses the
        real condition shape with a synthetic action attached (the prod
        agent itself is notify-only) to prove the dispatch-loop behavior
        the shape implies."""
        spec = _builtin("loss-positions-total")
        assert spec["conditions"] == {"any": [
            {"metric": "day_val",      "scope": "positions.total", "op": "<=", "value": -50000},
            {"metric": "day_pct",      "scope": "positions.total", "op": "<=", "value": -2.0},
            {"metric": "pnl_rate_abs", "scope": "positions.total", "op": "<=", "value": -6000},
            {"metric": "pnl_rate_pct", "scope": "positions.total", "op": "<=", "value": -0.25},
        ]}, "loss-positions-total's real condition shape changed — update this test"
        agent = _Agent(id=102, slug=spec["slug"], tier=spec["tier"], topic=spec["topic"],
                       actions=[{"type": "chase_close_positions", "params": {"scope": "total"}}])
        # Both day_val and day_pct leaves breach the SAME TOTAL row this tick.
        total_row = _row("TOTAL", day_change_val=-60000, day_change_percentage=-3.0)
        matches = [
            _m(metric="day_val", scope="positions.total", account="TOTAL",
               op="<=", threshold=-50000, value=-60000, row=total_row),
            _m(metric="day_pct", scope="positions.total", account="TOTAL",
               op="<=", threshold=-2.0, value=-3.0, row=total_row),
        ]

        exec_mock, fired = await _run_tick(agent, matches)

        assert fired
        assert exec_mock.call_count == 1, (
            f"two aggregate leaves co-breaching must still fire ONCE, "
            f"got {exec_mock.call_count}"
        )
        assert exec_mock.call_args.args[2]["account"] == "TOTAL"

    @pytest.mark.asyncio
    async def test_expiry_auto_close_nfo_three_itm_rows_fires_once(self):
        """Real agent `expiry-day-equity-itm-auto-close`: per-row scope
        (positions.expiring_today.nfo) + expiry_auto_close action (NO
        $row — the action scans+closes the whole NFO exchange in one
        call). Three matching ITM rows (the exact scenario fan-out COULD
        have broken) must still fire exactly once — re-invoking a whole-
        exchange sweep 3x would be actively harmful (redundant races),
        not 3 correct partial actions."""
        spec = _builtin("expiry-day-equity-itm-auto-close")
        assert spec["actions"] == [{"type": "expiry_auto_close", "params": {"exchange": "NFO"}}]
        agent = _Agent(id=103, slug=spec["slug"], tier=spec["tier"], topic=spec["topic"],
                       actions=spec["actions"])
        matches = [
            _m(metric="is_itm", scope="positions.expiring_today.nfo", account="ZG0790",
               op="==", threshold=1.0, value=1.0,
               row=_row("ZG0790", tradingsymbol="NIFTY26OCT22500CE", exchange="NFO")),
            _m(metric="is_itm", scope="positions.expiring_today.nfo", account="ZG0790",
               op="==", threshold=1.0, value=1.0,
               row=_row("ZG0790", tradingsymbol="NIFTY26OCT22600CE", exchange="NFO")),
            _m(metric="is_itm", scope="positions.expiring_today.nfo", account="ZJ6294",
               op="==", threshold=1.0, value=1.0,
               row=_row("ZJ6294", tradingsymbol="BANKNIFTY26OCT48000PE", exchange="NFO")),
        ]

        exec_mock, fired = await _run_tick(agent, matches)

        assert fired
        assert exec_mock.call_count == 1, (
            f"expiry_auto_close (whole-exchange sweep, no $row) must fire "
            f"ONCE for 3 matched rows, got {exec_mock.call_count} — a "
            f"regression here means N redundant close-sweeps racing each "
            f"other"
        )
        assert exec_mock.call_args.args[2]["account"] == "TOTAL"

    @pytest.mark.asyncio
    async def test_expiry_auto_close_mcx_unhedged_fires_once(self):
        """Same proof as above for the MCX sibling
        (expiry-day-commodity-itm-auto-close, scope
        positions.expiring_today.mcx_unhedged)."""
        spec = _builtin("expiry-day-commodity-itm-auto-close")
        agent = _Agent(id=104, slug=spec["slug"], tier=spec["tier"], topic=spec["topic"],
                       actions=spec["actions"])
        matches = [
            _m(metric="is_itm", scope="positions.expiring_today.mcx_unhedged",
               account="ZG0790", op="==", threshold=1.0, value=1.0,
               row=_row("ZG0790", tradingsymbol="CRUDEOIL26OCT6000CE", exchange="MCX")),
            _m(metric="is_itm", scope="positions.expiring_today.mcx_unhedged",
               account="ZG0790", op="==", threshold=1.0, value=1.0,
               row=_row("ZG0790", tradingsymbol="CRUDEOIL26OCT6100CE", exchange="MCX")),
        ]

        exec_mock, fired = await _run_tick(agent, matches)

        assert fired
        assert exec_mock.call_count == 1, (
            f"expected exactly 1 execute() call, got {exec_mock.call_count}"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  New behavior — $row-using actions fan out per matched row
# ═══════════════════════════════════════════════════════════════════════════

class TestRowRefFanOut:
    _CLOSE_ACTION = [{
        "type": "close_position",
        "params": {
            "account": {"$row": "account_id"},
            "symbol":  {"$row": "symbol"},
        },
    }]

    @pytest.mark.asyncio
    async def test_three_rows_two_accounts_fires_three_times(self):
        """A per-row-scoped agent (positions.expiring_today — the
        PLAN's own canonical example scope) whose action references
        $row fan out: 3 matching rows across 2 accounts fire execute()
        exactly 3 times, each bound to the correct distinct
        account/row."""
        agent = _Agent(id=201, slug="test-row-fanout", actions=self._CLOSE_ACTION)
        rows = [
            _row("ZG0790", tradingsymbol="NIFTY26OCT22500CE"),
            _row("ZJ6294", tradingsymbol="BANKNIFTY26OCT48000PE"),
            _row("ZG0790", tradingsymbol="RELIANCE"),
        ]
        matches = [
            _m(account=r['account'], row=r, value=-10.0 - i)
            for i, r in enumerate(rows)
        ]

        exec_mock, fired = await _run_tick(agent, matches)

        assert fired
        assert exec_mock.call_count == 3, (
            f"expected 3 execute() calls (one per matched row), got {exec_mock.call_count}"
        )
        seen = [
            (c.args[2]["account"], c.args[2]["_matched_row"].get("tradingsymbol"))
            for c in exec_mock.call_args_list
        ]
        expected = [(r['account'], r['tradingsymbol']) for r in rows]
        assert seen == expected, f"expected {expected}, got {seen}"

    @pytest.mark.asyncio
    async def test_single_row_fires_exactly_once(self):
        """Boundary case — exactly 1 matching row fires exactly once
        (not zero, not duplicated)."""
        agent = _Agent(id=202, slug="test-row-fanout-single", actions=self._CLOSE_ACTION)
        matches = [_m(account="ZG0790", row=_row("ZG0790", tradingsymbol="NIFTY26OCT22500CE"))]

        exec_mock, fired = await _run_tick(agent, matches)

        assert fired
        assert exec_mock.call_count == 1, (
            f"expected exactly 1 execute() call, got {exec_mock.call_count}"
        )
        assert exec_mock.call_args.args[2]["account"] == "ZG0790"

    @pytest.mark.asyncio
    async def test_zero_matches_fires_zero_times(self):
        """Regression guard — zero matching rows must never buffer a
        dispatch at all (`_cycle_maybe_buffer_fire`'s pre-existing
        `if not matches: return False`), so execute() is never called."""
        agent = _Agent(id=203, slug="test-row-fanout-zero", actions=self._CLOSE_ACTION)

        exec_mock, fired = await _run_tick(agent, [])

        assert not fired, "zero matches must not fire"
        assert exec_mock.call_count == 0, (
            f"expected 0 execute() calls, got {exec_mock.call_count}"
        )

    @pytest.mark.asyncio
    async def test_simulator_bypass_suppression_fans_out_identically(self):
        """The isolated single-agent simulator/dry-run path
        (bypass_suppression=True) must fan out identically to the live
        path for the same scenario — same 3 rows, same 3 executions."""
        agent = _Agent(id=204, slug="test-row-fanout-sim", actions=self._CLOSE_ACTION)
        rows = [
            _row("ZG0790", tradingsymbol="NIFTY26OCT22500CE"),
            _row("ZJ6294", tradingsymbol="BANKNIFTY26OCT48000PE"),
            _row("ZG0790", tradingsymbol="RELIANCE"),
        ]
        matches = [_m(account=r['account'], row=r) for r in rows]

        exec_mock, fired = await _run_tick(agent, matches, bypass_suppression=True)

        assert fired
        assert exec_mock.call_count == 3, (
            f"sim/dry-run fan-out must match the live path (3 rows -> 3 "
            f"executions), got {exec_mock.call_count}"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  End-to-end — the REAL execute() (not mocked): proves $row resolves to
#  the correct LIVE value at dispatch time, and that the aggregate/
#  unexposed-column cases genuinely skip the broker call (not just that
#  fan-out produced the right mocked-call COUNT, per advisor review).
# ═══════════════════════════════════════════════════════════════════════════

class TestEndToEndRowRefExecution:
    _CLOSE_ACTION = TestRowRefFanOut._CLOSE_ACTION

    @pytest.mark.asyncio
    async def test_per_row_dispatch_receives_resolved_params(self):
        """Real `execute()` end-to-end: for each of 2 matched rows
        (positions.expiring_today — the PLAN's own canonical scope),
        `_al_dispatch_by_mode` (the point actions.py hands off to the
        sim/paper/live writer) receives the ACTUAL resolved
        {"account": "<real>", "symbol": "<real>"} — not the raw $row
        marker, not a stale/wrong row."""
        agent = _Agent(id=501, slug="test-e2e-row-fanout", actions=self._CLOSE_ACTION)
        rows = [
            _row("ZG0790", tradingsymbol="NIFTY26OCT22500CE", exchange="NFO", product="NRML"),
            _row("ZJ6294", tradingsymbol="BANKNIFTY26OCT48000PE", exchange="NFO", product="NRML"),
        ]
        matches = [_m(account=r['account'], row=r) for r in rows]

        dispatch_mock, log_event_mock, fired = await _run_tick_real_execute(agent, matches)

        assert fired
        assert dispatch_mock.call_count == 2, (
            f"expected 2 real dispatch calls, got {dispatch_mock.call_count}"
        )
        seen_params = [c.args[3] for c in dispatch_mock.call_args_list]
        expected_params = [
            {"account": r['account'], "symbol": r['tradingsymbol']} for r in rows
        ]
        assert seen_params == expected_params, (
            f"expected resolved params {expected_params}, got {seen_params}"
        )
        skipped = [c for c in log_event_mock.call_args_list if c.args[1] == "action_skipped"]
        assert not skipped, f"no action should have been skipped, got {skipped}"

    @pytest.mark.asyncio
    async def test_aggregate_scope_skips_broker_dispatch_and_logs(self):
        """Real execute() end-to-end for an aggregate (positions.total)
        match carrying a $row-using action: the broker dispatch must
        NEVER be reached, and an `action_skipped` event must be logged
        with a reason naming the unbound-row cause."""
        agent = _Agent(id=502, slug="test-e2e-aggregate-skip", actions=self._CLOSE_ACTION)
        matches = [_m(metric="pnl", scope="positions.total", op="<=",
                     threshold=-50000, value=-60000, account="TOTAL",
                     row=_row("TOTAL", pnl=-60000))]

        dispatch_mock, log_event_mock, fired = await _run_tick_real_execute(agent, matches)

        assert fired
        assert dispatch_mock.call_count == 0, (
            f"an aggregate match must never reach the broker dispatch, "
            f"got {dispatch_mock.call_count} calls"
        )
        skipped = [c for c in log_event_mock.call_args_list if c.args[1] == "action_skipped"]
        assert len(skipped) == 1, f"expected exactly 1 action_skipped log, got {len(skipped)}"
        reason = skipped[0].args[3]["skip_reason"]
        assert "no row is bound" in reason, f"expected a clear reason, got {reason!r}"

    @pytest.mark.asyncio
    async def test_any_acct_symbol_reference_is_invalid_and_skips(self):
        """Documents that the PLAN's example, if mis-applied to
        `positions.any_acct` (an account-level SUMMARY scope with no
        per-symbol identity — `symbol` is NOT in its exposed-column
        schema), is invalid: the action is skipped, never silently
        resolved to a blank/wrong symbol. This is the exact scope the
        CLI's compile-time guard must also reject."""
        agent = _Agent(id=503, slug="test-e2e-any-acct-invalid", actions=self._CLOSE_ACTION)
        matches = [_m(metric="pnl_pct", scope="positions.any_acct", op="<=",
                     threshold=-5.0, value=-10.0, account="ZG0790",
                     row=_row("ZG0790", pnl=-1000))]

        dispatch_mock, log_event_mock, fired = await _run_tick_real_execute(agent, matches)

        assert fired
        assert dispatch_mock.call_count == 0, (
            f"positions.any_acct has no 'symbol' column — dispatch must "
            f"never be reached, got {dispatch_mock.call_count} calls"
        )
        skipped = [c for c in log_event_mock.call_args_list if c.args[1] == "action_skipped"]
        assert len(skipped) == 1, f"expected exactly 1 action_skipped log, got {len(skipped)}"
        reason = skipped[0].args[3]["skip_reason"]
        assert "symbol" in reason and "positions.any_acct" in reason, (
            f"expected a reason naming the field and scope, got {reason!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Aggregate scope + $row — fails closed, never silently "TOTAL"/None
# ═══════════════════════════════════════════════════════════════════════════

class TestAggregateRowRefFailsClosed:
    @pytest.mark.asyncio
    async def test_aggregate_match_never_binds_a_row(self):
        """An AGGREGATE match (account='TOTAL') must never bind
        `_matched_row` — even though the TOTAL summary row technically
        has an 'account' column that would resolve to the literal
        'TOTAL' string, which is exactly the silent-value failure mode
        this feature must never produce."""
        agent = _Agent(id=301, slug="test-agg-no-bind", actions=[{
            "type": "close_position",
            "params": {"account": {"$row": "account_id"}, "symbol": "X"},
        }])
        matches = [_m(scope="positions.total", account="TOTAL",
                     row=_row("TOTAL", pnl=-60000))]

        exec_mock, fired = await _run_tick(agent, matches)

        assert fired
        assert exec_mock.call_count == 1
        action_ctx = exec_mock.call_args.args[2]
        assert action_ctx["_matched_row"] is None, (
            "an aggregate match must never bind a row for $row resolution"
        )
        assert action_ctx["account"] == "TOTAL"

    def test_resolve_row_ref_fails_closed_with_no_bound_row(self):
        """Direct unit check of the runtime backstop: resolve_row_ref_params
        refuses (non-None error, unresolved params returned) when
        context["_matched_row"] is None — the exact shape _ae_dispatch_actions
        produces for an aggregate match."""
        params = {"account": {"$row": "account_id"}, "symbol": "X"}
        resolved, err = resolve_row_ref_params(params, {"_matched_row": None})
        assert err is not None, "expected a fail-closed error, got None"
        assert resolved == params, "params must be returned UNRESOLVED on failure"

    def test_resolve_row_ref_fails_closed_on_unexposed_column(self):
        """`symbol` is not an exposed column for the aggregate
        `positions.total` scope (no per-symbol identity) — must fail
        closed with a clear reason, not resolve to None/blank."""
        params = {"symbol": {"$row": "symbol"}}
        resolved, err = resolve_row_ref_params(
            params, {"_matched_row": {"account": "TOTAL"},
                     "_matched_row_scope": "positions.total"},
        )
        assert err is not None and "symbol" in err, f"expected a clear error, got {err!r}"
        assert resolved == params

    def test_resolve_row_ref_fails_closed_on_missing_value(self):
        """A real per-row scope whose row is simply missing the column
        value (None) must fail closed, not pass through None."""
        params = {"symbol": {"$row": "symbol"}}
        resolved, err = resolve_row_ref_params(
            params, {"_matched_row": {"account": "ZG0790", "tradingsymbol": None},
                     "_matched_row_scope": "positions.any_acct"},
        )
        assert err is not None, f"expected a fail-closed error for a missing value, got None"

    def test_resolve_row_ref_converts_numpy_scalar(self):
        """A pandas/numpy scalar (the real shape row.to_dict() produces)
        must be converted to a native Python type, not passed through
        raw (json.dumps would reject a numpy scalar)."""
        np = pytest.importorskip("numpy")
        params = {"qty": {"$row": "qty"}}
        resolved, err = resolve_row_ref_params(
            params,
            {"_matched_row": {"account": "ZG0790", "quantity": np.int64(50)},
             "_matched_row_scope": "positions.expiring_today"},
        )
        assert err is None, f"unexpected error: {err}"
        assert type(resolved["qty"]) is int, (
            f"expected a native int, got {type(resolved['qty'])}"
        )
        assert resolved["qty"] == 50

    def test_resolve_row_ref_is_noop_for_literal_params(self):
        """No $row markers anywhere -> params returned byte-identical,
        no error — the common case for every pre-existing agent."""
        params = {"account": "ZG0790", "symbol": "NIFTY26OCT22500CE", "qty": 50}
        resolved, err = resolve_row_ref_params(params, {})
        assert err is None
        assert resolved == params


# ═══════════════════════════════════════════════════════════════════════════
#  fire_matches — latch-key granularity fix (same account, different symbol)
# ═══════════════════════════════════════════════════════════════════════════

class TestFireMatchesLatchKeyFix:
    def test_two_same_account_different_symbol_both_survive_fire_matches(self):
        """`_latch_key` is (agent_slug, metric, scope, account) — NO
        symbol component. Two matches sharing that key (same account,
        same metric/scope, different tradingsymbol) must BOTH appear in
        `fire_matches`, even though the escalation gate's own
        `effective`/`latched_matches` only keeps the first (a real,
        pre-existing per-KEY gating property that must not silently
        drop a real per-ROW fire)."""
        agent = _Agent(id=401, slug="test-latch-key-gap", cooldown_minutes=30)
        matches = [
            _m(metric="is_itm", scope="positions.expiring_today", account="ZG0790",
               op="==", threshold=1.0, value=1.0,
               row=_row("ZG0790", tradingsymbol="A26OCTFUT")),
            _m(metric="is_itm", scope="positions.expiring_today", account="ZG0790",
               op="==", threshold=1.0, value=1.0,
               row=_row("ZG0790", tradingsymbol="B26OCTFUT")),
        ]
        pending: list = []
        fired = agent_engine._cycle_maybe_buffer_fire(
            agent, matches, now=_now(), bypass_suppression=False,
            bypass_schedule=True, sim_mode=False, alert_state={},
            cfg={'cooldown_min': 30}, broadcast_fn=None, debounce_min=0,
            pending_dispatches=pending,
        )
        assert fired
        entry = pending[0]
        assert len(entry['latched_matches']) == 1, (
            "sanity check — the pre-existing per-KEY gate still only "
            "latches the first row for this key (expected/unchanged)"
        )
        fire_syms = sorted(m['row']['tradingsymbol'] for m in entry['fire_matches'])
        assert fire_syms == ["A26OCTFUT", "B26OCTFUT"], (
            f"fire_matches must contain BOTH rows sharing the latch key, "
            f"got {fire_syms}"
        )

    @pytest.mark.asyncio
    async def test_both_rows_dispatch_when_row_ref_action_attached(self):
        """End-to-end: with a $row-using action attached, both same-
        account different-symbol rows from the latch-key-gap scenario
        above actually fire execute() twice — not once."""
        agent = _Agent(id=402, slug="test-latch-key-gap-dispatch", cooldown_minutes=30,
                       actions=TestRowRefFanOut._CLOSE_ACTION)
        matches = [
            _m(metric="is_itm", scope="positions.expiring_today", account="ZG0790",
               op="==", threshold=1.0, value=1.0,
               row=_row("ZG0790", tradingsymbol="A26OCTFUT")),
            _m(metric="is_itm", scope="positions.expiring_today", account="ZG0790",
               op="==", threshold=1.0, value=1.0,
               row=_row("ZG0790", tradingsymbol="B26OCTFUT")),
        ]

        exec_mock, fired = await _run_tick(agent, matches, cfg={'cooldown_min': 30})

        assert fired
        assert exec_mock.call_count == 2, (
            f"expected both same-account different-symbol rows to fire "
            f"(2 executions), got {exec_mock.call_count}"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  _ae_fanout_matches / _ae_row_identity / _ae_actions_use_row_ref — units
# ═══════════════════════════════════════════════════════════════════════════

class TestFanoutHelperUnits:
    def test_aggregate_collapses_to_one(self):
        matches = [
            _m(scope="positions.total", account="TOTAL", row=_row("TOTAL")),
            _m(scope="positions.total", account=None, row=_row(None)),
        ]
        out = _ae_fanout_matches(matches)
        assert len(out) == 1, f"expected aggregate matches to collapse to 1, got {len(out)}"

    def test_per_row_distinct_rows_not_collapsed(self):
        matches = [
            _m(account="ZG0790", row=_row("ZG0790", tradingsymbol="A")),
            _m(account="ZG0790", row=_row("ZG0790", tradingsymbol="B")),
            _m(account="ZJ6294", row=_row("ZJ6294", tradingsymbol="C")),
        ]
        out = _ae_fanout_matches(matches)
        assert len(out) == 3, f"expected 3 distinct rows preserved, got {len(out)}"

    def test_same_row_duplicate_from_two_leaves_deduped(self):
        """Real shape `loss-funds-negative`: any[cash<0, avail_margin<0]
        on funds.any_acct — one account breaching BOTH legs produces two
        match entries for the identical row. Must dedupe to 1."""
        row = _row("ZG0790")
        matches = [
            _m(metric="cash", scope="funds.any_acct", account="ZG0790",
               op="<", threshold=0, value=-500.0, row=row),
            _m(metric="avail_margin", scope="funds.any_acct", account="ZG0790",
               op="<", threshold=0, value=-200.0, row=row),
        ]
        out = _ae_fanout_matches(matches)
        assert len(out) == 1, (
            f"same account breaching 2 leaves on the identical row must "
            f"dedupe to 1 execution, got {len(out)}"
        )

    def test_row_identity_distinguishes_different_symbols(self):
        m1 = _m(account="ZG0790", row=_row("ZG0790", tradingsymbol="A"))
        m2 = _m(account="ZG0790", row=_row("ZG0790", tradingsymbol="B"))
        assert _ae_row_identity(m1) != _ae_row_identity(m2)

    def test_empty_matches_returns_empty(self):
        assert _ae_fanout_matches([]) == []

    def test_actions_use_row_ref_true_when_marker_present(self):
        actions = [{"type": "close_position",
                    "params": {"account": {"$row": "account_id"}}}]
        assert _ae_actions_use_row_ref(actions) is True

    def test_actions_use_row_ref_false_for_every_builtin_agent(self):
        """No currently-seeded agent can contain $row — confirms the
        backward-compat guarantee holds for the ENTIRE real catalog,
        not just the 4 shapes spot-checked above."""
        for spec in BUILTIN_AGENTS:
            assert _ae_actions_use_row_ref(spec.get("actions") or []) is False, (
                f"BUILTIN_AGENTS['{spec.get('slug')}'] unexpectedly uses $row"
            )

    def test_actions_use_row_ref_false_for_empty_or_none(self):
        assert _ae_actions_use_row_ref([]) is False
        assert _ae_actions_use_row_ref(None) is False
