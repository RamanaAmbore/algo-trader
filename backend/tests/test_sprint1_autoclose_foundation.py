"""
Sprint 1 foundation — verification spikes A and B for the planned "auto-
close a short option position at 90% profit" condition-agent (a later
sprint; THIS sprint is foundation-only, see CLAUDE.md's Sprint 1 entry).

Spike A — fan-out dispatch identity:
  For a fan-out scope match (multiple rows matched in one tick), does
  each matched row produce its OWN independent action dispatch carrying
  THAT row's own account/symbol/exchange — not the first matched row's
  values reused for all matches, and not one aggregated dispatch?

  Result: YES for a single leaf (the real, pre-existing "Sprint 16
  fan-out" mechanism in agent_engine.py — `_ae_dispatch_actions` /
  `_ae_fanout_matches` / `resolve_row_ref_params` — already handles this
  correctly, confirmed end-to-end through the REAL `execute()` path).

  BLOCKING FINDING: NO for an `all[]` of two leaves both scoped to
  `positions.any_symbol` (the shape a "short AND gain>=90%" agent would
  naturally compile to). `agent_evaluator._eval_all`'s cross-row join
  (`_match_join_key`/`_all_matches_keyed_by`) only recognises two
  join-key TYPES — 'acct' and 'und' (account and underlying) — with NO
  per-row/per-symbol dimension. When leg A (same account) satisfies BOTH
  leaves and leg B satisfies only leaf 1, the join intersects on ACCOUNT
  alone and wrongly carries leg B through into the fired result, even
  though leg B never satisfied leaf 2. See
  `TestSpikeAAndCrossJoinBlindSpot` below — this test PASSES by pinning
  the actual (confirmed, reproduced) behaviour; it does not claim the
  behaviour is correct. The next sprint's condition-agent must NOT
  combine two independent leaves over `positions.any_symbol` in one
  `all[]` until this is fixed (or must gate on a single, already
  direction-aware metric resolver instead — see
  `TestSpikeASingleLeafShapeThatWorks` for the shape that IS safe
  today).

Spike B — chase_order() call-site topology:
  `actions_live.py:_action_live_close_position` and
  `order_release.py:_chase_released` (via `release_held_order`) are TWO
  FULLY INDEPENDENT call sites. Confirmed:
    - Neither module imports anything from the other (grep — zero
      matches either direction).
    - Each does its own LOCAL (function-scoped) `from backend.api.algo.
      chase import chase_order` — there is no shared wrapper function
      between them.
    - Each builds its OWN `ChaseConfig` via different code
      (`_action_live_close_position` builds one directly;
      `_chase_released` goes through `orders_helpers._live_chase_config`).
    - Each runs its OWN independent pre-chase verification (position
      sign/magnitude check, price check, preflight) before ever reaching
      `chase_order()`.
  The ONLY shared point between them is `chase_order()` itself (the
  single function both resolve from `backend.api.algo.chase` at call
  time). A future per-position lock must therefore live EITHER inside
  `chase_order()` itself, or be duplicated at every independent call
  site (this one, and any future one) — it cannot be added once at a
  shared pre-chase chokepoint, because no such chokepoint exists today.

Five quality dimensions:
  SSOT        — drives the REAL `_cycle_maybe_buffer_fire` /
                `_cycle_dispatch_survivors` / `execute()` / `evaluate()`
                dispatch chain, and the REAL `_action_live_close_position`
                / `_chase_released` functions — no reimplementation.
  Correctness — Spike A's positive case end-to-end through real
                `resolve_row_ref_params`; Spike A's negative case
                reproduced with a real `evaluate()` call, not asserted
                from reasoning alone.
  Performance — pure in-memory / mocked-broker-boundary; no DB/network.
  Reuse       — mirrors the proven mocking recipes in
                `test_agent_action_fanout.py` and
                `test_live_close_broker_resolve.py`.
  UX          — every assert carries an f-string with actual/expected.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo import agent_engine
from backend.api.algo import actions as actions_mod
from backend.api.algo.agent_engine import (
    _cycle_maybe_buffer_fire,
    _cycle_dispatch_survivors,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Shared helpers (mirrors test_agent_action_fanout.py's conventions)
# ═══════════════════════════════════════════════════════════════════════════

class _Agent:
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


def _now():
    return datetime(2026, 11, 8, 10, 0, 0, tzinfo=timezone.utc)


def _row(account: str, **extra) -> dict:
    return {'account': account, **extra}


def _m(metric: str, scope: str, account: str, op: str, threshold, value, row: dict) -> dict:
    return {'metric': metric, 'scope': scope, 'op': op, 'threshold': threshold,
            'value': value, 'row': row, 'account': account}


async def _run_tick_real_execute(agent, matches, *, cfg=None, bypass_suppression=False,
                                 context=None):
    """Drives the REAL `execute()` (real `resolve_row_ref_params` +
    `resolve_action_params` + exchange-gate chain) down to
    `_al_dispatch_by_mode` — the lowest level at which the FINAL resolved
    params dict can be inspected. Identical recipe to
    `test_agent_action_fanout.py`'s `_run_tick_real_execute`."""
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


_CLOSE_ACTION = [{
    "type": "close_position",
    "params": {
        "account": {"$row": "account_id"},
        "symbol": {"$row": "symbol"},
        "exchange": {"$row": "exchange"},
    },
}]


# ═══════════════════════════════════════════════════════════════════════════
#  Spike A, positive case — single leaf over positions.any_symbol fans
#  out correctly, each dispatch carrying its own row's identity.
# ═══════════════════════════════════════════════════════════════════════════

class TestSpikeAFanOutPerRowDispatch:
    @pytest.mark.asyncio
    async def test_three_rows_two_accounts_three_symbols_dispatch_independently(self):
        """Core Spike A claim, positive case: `positions.any_symbol`
        matching 3 rows across 2 accounts / 3 symbols produces 3
        independent dispatch calls, each carrying THAT row's own
        account/symbol/exchange — never the first row's values reused,
        never a single aggregated dispatch."""
        agent = _Agent(id=901, slug="test-spike-a-any-symbol", actions=_CLOSE_ACTION)
        rows = [
            _row("ZG0790", tradingsymbol="NIFTY26NOV24000PE", exchange="NFO"),
            _row("ZG0790", tradingsymbol="BANKNIFTY26NOV50000CE", exchange="NFO"),
            _row("ZJ6294", tradingsymbol="CRUDEOIL26NOV6000CE", exchange="MCX"),
        ]
        matches = [
            _m("qty", "positions.any_symbol", r['account'], "<", 0, -1.0, r)
            for r in rows
        ]

        dispatch_mock, log_event_mock, fired = await _run_tick_real_execute(agent, matches)

        assert fired, "agent must have fired"
        assert dispatch_mock.call_count == 3, (
            f"expected 3 independent dispatches (one per matched row), "
            f"got {dispatch_mock.call_count}"
        )
        seen = [c.args[3] for c in dispatch_mock.call_args_list]
        expected = [
            {"account": r['account'], "symbol": r['tradingsymbol'], "exchange": r['exchange']}
            for r in rows
        ]
        assert seen == expected, (
            f"each dispatch must carry THAT row's own account/symbol/exchange, "
            f"not the first row's values reused for all — expected {expected}, "
            f"got {seen}"
        )
        skipped = [c for c in log_event_mock.call_args_list if c.args[1] == "action_skipped"]
        assert not skipped, f"no action should have been skipped, got {skipped}"

    @pytest.mark.asyncio
    async def test_gain_from_entry_pct_single_leaf_shape_that_works(self):
        """The 'shape that works' for the next sprint: a SINGLE leaf
        using the new `gain_from_entry_pct` metric over
        `positions.any_symbol` (no `all[]` of two leaves — see
        `TestSpikeAAndCrossJoinBlindSpot` for why that shape is unsafe
        today). 3 rows, only ONE crosses the 90% threshold — exactly
        that one row fires and dispatches, with its own identity."""
        agent = _Agent(id=902, slug="test-spike-a-gain-metric", actions=_CLOSE_ACTION)
        rows = [
            _row("ZG0790", tradingsymbol="SHORT_90PCT", exchange="NFO",
                 quantity=-50, average_price=100.0, last_price=10.0),   # short, 90% gain
            _row("ZG0790", tradingsymbol="SHORT_1PCT", exchange="NFO",
                 quantity=-50, average_price=100.0, last_price=99.0),   # short, 1% gain
            _row("ZJ6294", tradingsymbol="LONG_5PCT", exchange="NFO",
                 quantity=50, average_price=100.0, last_price=105.0),   # long, 5% gain
        ]
        matches = [
            _m("gain_from_entry_pct", "positions.any_symbol", r['account'],
               ">=", 90, 90.0, r)
            for r in rows if r['tradingsymbol'] == 'SHORT_90PCT'
        ]
        # Only the real breaching row produces a match — matches is the
        # evaluator's OWN output in production; hand-built here to the
        # exact single-row shape the real resolver would produce (see
        # TestSpikeAAndCrossJoinBlindSpot for the real evaluate() proof
        # that this metric resolves correctly per-row).
        assert len(matches) == 1

        dispatch_mock, log_event_mock, fired = await _run_tick_real_execute(agent, matches)

        assert fired
        assert dispatch_mock.call_count == 1, (
            f"expected exactly 1 dispatch (only SHORT_90PCT breaches), "
            f"got {dispatch_mock.call_count}"
        )
        seen = dispatch_mock.call_args_list[0].args[3]
        assert seen == {"account": "ZG0790", "symbol": "SHORT_90PCT", "exchange": "NFO"}, (
            f"expected the breaching row's own identity, got {seen}"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Spike A, negative case — BLOCKING FINDING: all[] cross-joins by
#  account, not by row identity, for two leaves over positions.any_symbol
# ═══════════════════════════════════════════════════════════════════════════

class TestSpikeAAndCrossJoinBlindSpot:
    """Reproduces the structural gap with a REAL `evaluate()` call (not
    hand-built match dicts) — proving this from the grammar layer down,
    not asserted from reading the code alone."""

    @pytest.fixture(autouse=True)
    def _wire_registry(self, monkeypatch):
        from backend.api.algo.grammar_registry import REGISTRY
        from backend.api.algo.grammar import OPERATORS
        from backend.api.algo.grammar_tokens.scope_positions_any_symbol import (
            _scope_positions_any_symbol,
        )
        from backend.api.algo.grammar_tokens.metric_gain_from_entry_pct import (
            _metric_gain_from_entry_pct,
        )
        monkeypatch.setitem(REGISTRY.scopes, 'positions.any_symbol', _scope_positions_any_symbol)
        monkeypatch.setitem(REGISTRY.metrics, 'gain_from_entry_pct', _metric_gain_from_entry_pct)
        for op_tok in ('<', '>='):
            monkeypatch.setitem(REGISTRY.operators, op_tok, OPERATORS[op_tok])

    def test_and_of_two_any_symbol_leaves_wrongly_carries_the_non_matching_leg_through(self):
        """BLOCKING FINDING. Two legs, SAME account:
          - SHORT_90PCT: qty<0 (leaf 1 TRUE) AND gain>=90% (leaf 2 TRUE)
            -> genuinely satisfies the whole all[] -> SHOULD fire.
          - SHORT_1PCT:  qty<0 (leaf 1 TRUE) but gain=1% (leaf 2 FALSE)
            -> does NOT genuinely satisfy the whole all[] -> should NOT
            fire.

        `_eval_all`'s cross-row join intersects leaf 1's and leaf 2's
        matched ACCOUNTS (both leaves produce a match for account
        'ZG0790'), then reinstates EVERY row from EACH leaf whose account
        is in that intersection — including SHORT_1PCT from leaf 1, even
        though SHORT_1PCT was never in leaf 2's match set at all. This
        test documents the ACTUAL (confirmed, reproduced) behaviour. It
        is not an endorsement of it — see this module's own docstring
        and the handback report for the recommended next steps.
        """
        from backend.api.algo.agent_evaluator import evaluate, Context

        leg_a = {'account': 'ZG0790', 'tradingsymbol': 'SHORT_90PCT',
                 'quantity': -50, 'average_price': 100.0, 'last_price': 10.0}
        leg_b = {'account': 'ZG0790', 'tradingsymbol': 'SHORT_1PCT',
                 'quantity': -50, 'average_price': 100.0, 'last_price': 99.0}

        ctx = Context(position_rows=[leg_a, leg_b])
        cond = {"all": [
            {"op": "<", "scope": "positions.any_symbol", "metric": "qty", "value": 0},
            {"op": ">=", "scope": "positions.any_symbol", "metric": "gain_from_entry_pct", "value": 90},
        ]}
        result = evaluate(cond, ctx)

        matched_symbols = {m['row']['tradingsymbol'] for m in result}
        assert matched_symbols == {'SHORT_90PCT', 'SHORT_1PCT'}, (
            f"expected the CONFIRMED buggy behaviour (both legs carried "
            f"through, since this test pins what the engine ACTUALLY "
            f"does today) — got {matched_symbols}. If this now reads "
            f"{{'SHORT_90PCT'}} only, `_eval_all`'s join has been fixed "
            f"to be row-identity-aware — update this test's assertion "
            f"and this module's docstring to reflect the fix, do not "
            f"just loosen the assertion."
        )
        # The row that should NOT have fired is nonetheless present —
        # this is the exact shape a future auto-close action must not
        # blindly trust.
        leg_b_matches = [m for m in result if m['row']['tradingsymbol'] == 'SHORT_1PCT']
        assert any(m['metric'] == 'qty' for m in leg_b_matches), (
            "SHORT_1PCT's presence in the result comes only from the "
            "qty<0 leaf — it never satisfied gain_from_entry_pct>=90 "
            "itself"
        )
        assert not any(m['metric'] == 'gain_from_entry_pct' for m in leg_b_matches), (
            "confirms SHORT_1PCT's gain leaf never matched — its presence "
            "in `result` is purely an artifact of the account-level join"
        )

    def test_single_leaf_no_and_does_not_exhibit_the_cross_join_issue(self):
        """Sanity companion — a single leaf (no `all[]`) over
        `positions.any_symbol` has no cross-row join to go wrong; each
        row's own gain is evaluated independently and only the genuinely
        breaching row matches. This IS the safe shape for the next
        sprint (see `TestSpikeAFanOutPerRowDispatch.
        test_gain_from_entry_pct_single_leaf_shape_that_works` for the
        dispatch-level proof)."""
        from backend.api.algo.agent_evaluator import evaluate, Context

        leg_a = {'account': 'ZG0790', 'tradingsymbol': 'SHORT_90PCT',
                 'quantity': -50, 'average_price': 100.0, 'last_price': 10.0}
        leg_b = {'account': 'ZG0790', 'tradingsymbol': 'SHORT_1PCT',
                 'quantity': -50, 'average_price': 100.0, 'last_price': 99.0}

        ctx = Context(position_rows=[leg_a, leg_b])
        cond = {"op": ">=", "scope": "positions.any_symbol",
                "metric": "gain_from_entry_pct", "value": 90}
        result = evaluate(cond, ctx)

        matched_symbols = {m['row']['tradingsymbol'] for m in result}
        assert matched_symbols == {'SHORT_90PCT'}, (
            f"a single leaf must only match the genuinely breaching row, "
            f"got {matched_symbols}"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Spike B — chase_order() call-site topology
# ═══════════════════════════════════════════════════════════════════════════

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ACTIONS_LIVE_PATH = _REPO_ROOT / "backend" / "api" / "algo" / "actions_live.py"
_ORDER_RELEASE_PATH = _REPO_ROOT / "backend" / "api" / "algo" / "order_release.py"


def _imported_module_names(py_path: Path) -> set[str]:
    """AST-level import scan — every module name this file imports
    FROM, at any location in the file (module-level or function-local).
    Used to prove zero cross-coupling, not just grep's substring match."""
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


class TestSpikeBChaseOrderCallSiteTopology:
    def test_actions_live_does_not_import_order_release(self):
        imports = _imported_module_names(_ACTIONS_LIVE_PATH)
        offenders = {n for n in imports if 'order_release' in n}
        assert not offenders, (
            f"actions_live.py imports from order_release.py ({offenders}) — "
            f"the two paths are no longer independent; Spike B's finding "
            f"is stale and must be re-investigated"
        )

    def test_order_release_does_not_import_actions_live(self):
        imports = _imported_module_names(_ORDER_RELEASE_PATH)
        offenders = {n for n in imports if 'actions_live' in n}
        assert not offenders, (
            f"order_release.py imports from actions_live.py ({offenders}) — "
            f"the two paths are no longer independent; Spike B's finding "
            f"is stale and must be re-investigated"
        )

    def test_both_paths_import_chase_order_locally_from_the_same_module(self):
        """Both call sites resolve `chase_order` via a FUNCTION-LOCAL
        `from backend.api.algo.chase import chase_order` — never a
        module-level import. This is WHY a mock must target
        `backend.api.algo.chase.chase_order` directly rather than
        `actions_live.chase_order` / `order_release.chase_order` (neither
        attribute exists at module level in either file) — the absence
        of a module-level binding is itself evidence there is no shared
        wrapper object between the two call sites."""
        import inspect

        from backend.api.algo import actions_live, order_release

        assert not hasattr(actions_live, "chase_order"), (
            "actions_live.py must not bind chase_order at module level "
            "(it is imported locally, inside each function)"
        )
        assert not hasattr(order_release, "chase_order"), (
            "order_release.py must not bind chase_order at module level "
            "(it is imported locally, inside each function)"
        )
        close_src = inspect.getsource(actions_live._action_live_close_position)
        assert "from backend.api.algo.chase import chase_order" in close_src
        released_src = inspect.getsource(order_release._chase_released)
        assert "from backend.api.algo.chase import chase_order" in released_src

    @pytest.mark.asyncio
    async def test_both_paths_reach_the_identical_chase_order_binding_independently(self):
        """Behavioral proof: patch `backend.api.algo.chase.chase_order`
        ONCE with a single shared mock. Drive BOTH
        `_action_live_close_position` (actions_live.py) and
        `_chase_released` (order_release.py) through their OWN
        independent preflight/verification chains. Confirm both reach
        the SAME mock object directly — proving `chase_order()` is the
        only shared point, with no intermediate wrapper — and that each
        call carries its OWN independently-resolved account/symbol/side/
        qty (not values borrowed from the other path)."""
        from backend.api.algo import actions as _actions_mod
        from backend.api.algo import actions_live as _actions_live_mod
        from backend.api.algo import order_release as _order_release_mod
        from backend.api.algo.chase import ChaseStatus

        shared_chase_mock = AsyncMock(
            return_value=MagicMock(status=ChaseStatus.FILLED, detail=None),
        )

        agent = MagicMock()
        agent.slug = "test-spike-b"
        agent.id = 77

        # Both invocations run inside ONE patch scope — `chase_order` is
        # resolved via a function-LOCAL import at call time (see
        # test_both_paths_import_chase_order_locally_from_the_same_module
        # above), so the patch must still be active when EACH function
        # runs its own local `from backend.api.algo.chase import
        # chase_order`, not just the first.
        with (
            patch.object(_actions_live_mod, "logger", MagicMock()),
            patch("backend.api.routes.orders_place._fetch_net_position_qty",
                  new=AsyncMock(return_value=50.0)),
            patch.object(_actions_live_mod, "run_preflight",
                         new=AsyncMock(return_value={"ok": True, "blocked": []})),
            patch.object(_actions_mod, "_write_live_order",
                         new=AsyncMock(return_value=4242)),
            patch("backend.api.algo.chase.chase_order", new=shared_chase_mock),
        ):
            params = {
                "account": "ACC_CLOSE_PATH", "symbol": "NIFTY25DECFUT",
                "exchange": "NFO", "quantity": 50, "side": "SELL", "product": "NRML",
            }
            await _actions_mod._action_live_close_position(agent, {}, params)

            assert shared_chase_mock.call_count == 1, (
                f"expected _action_live_close_position to reach chase_order "
                f"exactly once, got {shared_chase_mock.call_count}"
            )
            close_path_call = shared_chase_mock.call_args_list[0].kwargs
            assert close_path_call["account"] == "ACC_CLOSE_PATH"
            assert close_path_call["symbol"] == "NIFTY25DECFUT"
            assert close_path_call["transaction_type"] == "SELL"
            assert close_path_call["quantity"] == 50

            shared_chase_mock.reset_mock()

            # Second, fully independent path: order_release.py's
            # _chase_released — different account/symbol/side/qty, driven
            # with ZERO shared mocking state from the close-position path
            # above (no run_preflight/_write_live_order/
            # _fetch_net_position_qty patches needed here — this path
            # never calls any of them). Still inside the SAME
            # chase_order patch scope, because the patch target is the
            # one place both local imports resolve from.
            await _order_release_mod._chase_released(
                row_id=9999, account="ACC_RELEASE_PATH", symbol="BANKNIFTY25DECFUT",
                exchange="NFO", side="BUY", qty=25, product="MIS", level="med",
                agent_id=None,
            )

        assert shared_chase_mock.call_count == 1, (
            f"expected _chase_released to reach the SAME patched "
            f"chase_order binding exactly once, got {shared_chase_mock.call_count} "
            f"— if this is 0, the two call sites are NOT resolving "
            f"chase_order from the same place"
        )
        release_path_call = shared_chase_mock.call_args_list[0].kwargs
        assert release_path_call["account"] == "ACC_RELEASE_PATH"
        assert release_path_call["symbol"] == "BANKNIFTY25DECFUT"
        assert release_path_call["transaction_type"] == "BUY"
        assert release_path_call["quantity"] == 25
        assert release_path_call["algo_order_id"] == 9999

        # The two calls never shared any parameter — proving each path
        # independently resolved its own identity, with nothing carried
        # over from the other.
        assert close_path_call["account"] != release_path_call["account"]
        assert close_path_call["symbol"] != release_path_call["symbol"]
