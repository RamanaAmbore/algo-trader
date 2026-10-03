"""
Tests for Sprint 1a — additive schema foundation for the order lifecycle
model (docs/proposals/ORDER_LIFECYCLE_DATA_MODEL.md), per .claude/PLAN.md.

Covers the 4 scoped items + the 2 corrections from the risk review:

  1. Schema migration — CREATE INDEX CONCURRENTLY runs via a SEPARATE
     AUTOCOMMIT connection OUTSIDE init_db's engine.begin() transaction,
     and a real (mocked-I/O) call to init_db() does not raise.
  2. compliance.algo_id settings placeholder exists.
  3. Live place_order agent-attribution fix (_AgentShim now carries real
     id/slug) + product/template_id scoped to a place_order-only branch,
     NOT the shared _write_live_order constructor — close_position/
     chase_close_positions regression guard.
  4. _fetch_net_position_qty returns None (not a wrong-account value) when
     the account column can't be identified.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

import backend.api.database as dbmod


_DB_SRC = inspect.getsource(dbmod.init_db)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Schema migration — CONCURRENTLY outside the transaction
# ═══════════════════════════════════════════════════════════════════════════

class _FakeResult:
    """Generic no-op DBAPI-result stand-in — covers every shape the
    existing (unrelated) _migrate_* steps call on an execute() result
    (fetchone/fetchall/scalar/scalars/...) without touching real data."""
    def fetchone(self): return None
    def fetchall(self): return []
    def scalar(self): return None
    def scalar_one_or_none(self): return None
    def all(self): return []
    def first(self): return None
    def scalars(self): return self
    def __iter__(self): return iter(())


class _FakeTxConn:
    """Fake connection for the engine.begin() transactional block.

    Generic: accepts any .execute()/.run_sync() call so every existing
    _migrate_* step (not just the new Sprint 1a one) can run against it
    without touching a real database.
    """
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(str(stmt))
        return _FakeResult()

    async def run_sync(self, fn, *a, **k):
        return None


class _FakeBeginCtx:
    def __init__(self, conn: _FakeTxConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakeAutocommitConn:
    """Fake connection for engine.connect() + AUTOCOMMIT (CONCURRENTLY path)."""
    def __init__(self, recorder: dict):
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execution_options(self, **kw):
        self._recorder.setdefault("execution_options", []).append(kw)
        return self

    async def execute(self, stmt, *a, **k):
        self._recorder.setdefault("executed", []).append(str(stmt))
        return None


def test_sprint1a_columns_step_never_uses_concurrently():
    """_migrate_algo_orders_sprint1a_columns (the transactional step) must
    never issue a CONCURRENTLY statement — that would hard-error inside
    init_db's engine.begin() transaction and crash the whole migration
    sequence, not just this index."""
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_algo_orders_sprint1a_columns(conn))

    assert not any("CONCURRENTLY" in s for s in conn.executed), (
        "CONCURRENTLY statement found inside the transactional columns-only "
        "migration step — this would crash init_db()."
    )
    joined = "\n".join(conn.executed)
    for col_stmt in (
        "ADD COLUMN IF NOT EXISTS source",
        "ADD COLUMN IF NOT EXISTS chase_session_id",
        "ADD COLUMN IF NOT EXISTS oco_pair_id",
        "ADD COLUMN IF NOT EXISTS algo_id",
        "ADD COLUMN IF NOT EXISTS broker_order_id_at_event",
    ):
        assert col_stmt in joined, f"missing migration statement: {col_stmt}"
    assert any("lock_timeout" in s for s in conn.executed), (
        "no explicit lock_timeout set on the ADD COLUMN transactional step"
    )
    assert any("fk_algo_orders_oco_pair_id" in s for s in conn.executed), (
        "oco_pair_id FK constraint (guarded DO $$ block) not issued"
    )


class _FakeEngine:
    """Stand-in for the module-level `engine` — AsyncEngine's own `.begin`/
    `.connect` are read-only slots and can't be patched in place, so tests
    replace the whole `backend.api.database.engine` module attribute
    instead (the migration functions look it up as a module global at
    call time, so this swap is transparent to them)."""
    def __init__(self, begin=None, connect=None):
        self.begin = begin or MagicMock()
        self.connect = connect or MagicMock()


def test_sprint1a_concurrent_indexes_run_outside_begin_with_autocommit():
    """The CONCURRENTLY step must run via engine.connect() + AUTOCOMMIT,
    never via engine.begin() — this is the exact hazard the plan's risk
    review flagged and corrected."""
    recorder: dict = {}
    mock_begin = MagicMock(side_effect=AssertionError(
        "engine.begin() must never be used by the CONCURRENTLY index step"
    ))
    mock_connect = MagicMock(side_effect=lambda: _FakeAutocommitConn(recorder))
    fake_engine = _FakeEngine(begin=mock_begin, connect=mock_connect)

    with patch.object(dbmod, "engine", fake_engine):
        asyncio.run(dbmod._migrate_algo_orders_sprint1a_indexes_concurrent())

    assert mock_begin.call_count == 0
    assert mock_connect.call_count == 3  # one AUTOCOMMIT connection per index
    executed = recorder.get("executed", [])
    assert len(executed) == 3
    assert all("CONCURRENTLY" in s for s in executed)
    assert all("IF NOT EXISTS" in s for s in executed)
    opts = recorder.get("execution_options", [])
    assert opts and all(o == {"isolation_level": "AUTOCOMMIT"} for o in opts)


def test_sprint1a_concurrent_index_failure_does_not_raise():
    """A single index's CONCURRENTLY failure (e.g. stale INVALID index,
    lock-wait timeout) must be swallowed, not propagated — init_db() must
    never fail to boot the API over a non-critical index."""
    call_count = {"n": 0}

    class _RaisingConn(_FakeAutocommitConn):
        async def execute(self, stmt, *a, **k):
            call_count["n"] += 1
            raise RuntimeError("simulated lock-wait timeout")

    fake_engine = _FakeEngine(connect=MagicMock(side_effect=lambda: _RaisingConn({})))

    with patch.object(dbmod, "engine", fake_engine):
        # Must not raise.
        asyncio.run(dbmod._migrate_algo_orders_sprint1a_indexes_concurrent())

    # All three statements were attempted despite each one failing.
    assert call_count["n"] == 3


def test_init_db_calls_concurrent_step_after_begin_block_exits():
    """Structural guard: init_db's source must call
    _migrate_algo_orders_sprint1a_indexes_concurrent() OUTSIDE (at a
    shallower indentation than) the `async with engine.begin() as conn:`
    block, and must call _migrate_algo_orders_sprint1a_columns(conn)
    INSIDE it."""
    lines = _DB_SRC.splitlines()
    begin_idx = next(
        i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l
    )
    begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

    columns_idx = next(
        i for i, l in enumerate(lines)
        if "_migrate_algo_orders_sprint1a_columns(conn)" in l
    )
    concurrent_idx = next(
        i for i, l in enumerate(lines)
        if "_migrate_algo_orders_sprint1a_indexes_concurrent()" in l
        and "async def" not in l
    )

    columns_indent = len(lines[columns_idx]) - len(lines[columns_idx].lstrip())
    concurrent_indent = len(lines[concurrent_idx]) - len(lines[concurrent_idx].lstrip())

    assert columns_idx > begin_idx and columns_indent > begin_indent, (
        "_migrate_algo_orders_sprint1a_columns(conn) must be called INSIDE "
        "the engine.begin() transactional block."
    )
    assert concurrent_idx > columns_idx, (
        "the CONCURRENTLY step must be called after the columns step"
    )
    assert concurrent_indent <= begin_indent, (
        "_migrate_algo_orders_sprint1a_indexes_concurrent() must be called "
        "OUTSIDE (same or shallower indentation than) the engine.begin() "
        "block — calling it inside would crash init_db() (CREATE INDEX "
        "CONCURRENTLY cannot run inside a transaction)."
    )


def test_init_db_real_call_does_not_raise():
    """End-to-end: call the REAL init_db() coroutine (not a stand-in) with
    engine.begin()/engine.connect() faked (no real DB) and every other
    seeding substep patched to a no-op, isolating the question the plan
    asks: does wiring the new Sprint 1a steps into init_db's real control
    flow make init_db() raise? It must not."""
    tx_conn = _FakeTxConn()
    recorder: dict = {}
    fake_engine = _FakeEngine(
        begin=MagicMock(return_value=_FakeBeginCtx(tx_conn)),
        connect=MagicMock(side_effect=lambda: _FakeAutocommitConn(recorder)),
    )

    with patch.object(dbmod, "engine", fake_engine), \
         patch.object(dbmod, "_ensure_shared_broker_schema", new=AsyncMock()), \
         patch.object(dbmod, "seed_special_sessions", new=AsyncMock()), \
         patch("backend.api.algo.grammar.seed_grammar_tokens", new=AsyncMock()), \
         patch("backend.api.algo.grammar_registry.REGISTRY.reload", new=AsyncMock()), \
         patch("backend.api.algo.template_registry.seed_agent_templates", new=AsyncMock()), \
         patch("backend.api.algo.agent_engine.seed_agents", new=AsyncMock()), \
         patch("backend.shared.helpers.settings.seed_settings", new=AsyncMock()), \
         patch("backend.api.algo.templates_seed.seed_templates", new=AsyncMock()), \
         patch("backend.api.routes.watchlist.seed_global_pinned", new=AsyncMock()), \
         patch("backend.shared.helpers.alert_utils.refresh_alert_recipients", new=AsyncMock()):

        asyncio.run(dbmod.init_db())  # must not raise

    # The CONCURRENTLY statements really did run, via the AUTOCOMMIT path.
    assert len(recorder.get("executed", [])) == 3
    assert all("CONCURRENTLY" in s for s in recorder["executed"])
    # And the plain ADD COLUMN steps really did run inside the tx block.
    assert any("ADD COLUMN IF NOT EXISTS source" in s for s in tx_conn.executed)


# ═══════════════════════════════════════════════════════════════════════════
# 2. compliance.algo_id settings placeholder
# ═══════════════════════════════════════════════════════════════════════════

def test_compliance_algo_id_setting_seeded():
    from backend.shared.helpers.settings import SEEDS

    rows = [s for s in SEEDS if s[1] == "compliance.algo_id"]
    assert len(rows) == 1, "compliance.algo_id must appear exactly once in SEEDS"
    category, key, value_type, default, _desc, units, schema = rows[0]
    assert category == "compliance"
    assert value_type == "string"
    assert default == ""


# ═══════════════════════════════════════════════════════════════════════════
# 3. Live place_order attribution fix + product/template_id scoping
# ═══════════════════════════════════════════════════════════════════════════

def test_al_place_resolve_params_carries_real_agent_id_and_slug():
    """Pre-fix: the _AgentShim only ever carried a default "place_order"
    slug string and NO id at all, so _write_live_order's
    agent_id=getattr(agent, "id", None) always wrote NULL for live
    agent-placed orders. Post-fix: the shim must carry the REAL agent's
    id and slug."""
    from backend.api.algo.actions_live import _al_place_resolve_params

    agent = MagicMock()
    agent.id = 42
    agent.slug = "loss-funds-negative"

    shim, account, symbol, exchange, side, qty, price, product, template_id = (
        _al_place_resolve_params(agent, {}, {
            "account": "ZG0790", "symbol": "NIFTY25OCTFUT", "exchange": "NFO",
            "transaction_type": "SELL", "quantity": 50, "product": "MIS",
            "template_id": 9,
        })
    )

    assert shim.id == 42
    assert shim.slug == "loss-funds-negative"
    assert product == "MIS"
    assert template_id == 9


def test_al_place_resolve_params_falls_back_when_agent_has_no_id():
    """A context-only / shim-like agent (no real id) falls back to the
    context's agent_slug default — never crashes."""
    from backend.api.algo.actions_live import _al_place_resolve_params

    class _NoIdAgent:
        pass

    shim, *_rest = _al_place_resolve_params(
        _NoIdAgent(), {"agent_slug": "fallback-slug"}, {"account": "ZG0790"},
    )
    assert shim.id is None
    assert shim.slug == "fallback-slug"


@pytest.mark.asyncio
async def test_place_order_set_product_template_applies_both_fields():
    """_place_order_set_product_template issues an UPDATE carrying both
    product and template_id for the given row id."""
    from backend.api.algo.actions_live import _place_order_set_product_template

    captured: dict = {}

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            captured["stmt"] = stmt

        async def commit(self):
            captured["committed"] = True

    with patch("backend.api.database.async_session", side_effect=lambda: _FakeSession()):
        await _place_order_set_product_template(123, "MIS", "9")

    assert captured.get("committed") is True
    # The compiled UPDATE carries both the product and template_id values.
    compiled_params = captured["stmt"].compile().params
    assert compiled_params.get("product") == "MIS"
    assert compiled_params.get("template_id") == 9  # coerced to int


@pytest.mark.asyncio
async def test_place_order_set_product_template_noop_when_both_absent():
    """No product and no template_id → no DB call at all (fast no-op)."""
    from backend.api.algo.actions_live import _place_order_set_product_template

    with patch("backend.api.database.async_session") as mock_session:
        await _place_order_set_product_template(123, "", None)

    mock_session.assert_not_called()


@pytest.mark.asyncio
async def test_place_order_set_product_template_swallows_db_failure():
    """A DB failure in the follow-up UPDATE must never raise — the caller's
    intent_id (already a real row id by this point) must never be
    clobbered by this helper's own failure."""
    from backend.api.algo.actions_live import _place_order_set_product_template

    class _RaisingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            raise RuntimeError("DB down")

        async def commit(self):
            pass

    with patch("backend.api.database.async_session", side_effect=lambda: _RaisingSession()):
        await _place_order_set_product_template(123, "MIS", 9)  # must not raise


def test_dispatch_live_action_passes_real_agent_to_place_order():
    """_dispatch_live_action's place_order branch — the one call site the
    plan's risk review called out as isolated/broken — must forward the
    real `agent` object, not just (context, params)."""
    import backend.api.algo.actions as actions_mod

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 5
    mock_place = AsyncMock()

    with patch("backend.api.algo.actions_live._action_place_order", new=mock_place):
        asyncio.run(actions_mod._dispatch_live_action(
            agent, "place_order", {"account": "ZG0790"}, {},
        ))

    mock_place.assert_called_once_with(agent, {}, {"account": "ZG0790"})


# ─── Regression guard: close_position / chase_close_positions unaffected ───

@pytest.mark.asyncio
async def test_write_live_order_close_position_never_sets_product_or_template():
    """_write_live_order (the SHARED constructor) must construct the exact
    same AlgoOrder kwargs for close_position today as before this sprint —
    in particular it must NEVER set product/template_id, regardless of
    action_type. This is the regression guard the risk review specifically
    required: a blanket edit to this shared function would silently change
    attribution for close_position/chase_close_positions."""
    from backend.api.algo.actions import _write_live_order

    captured_kwargs: dict = {}

    class _FakeRow:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.id = 777

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, row):
            pass

        async def commit(self):
            pass

    agent = MagicMock()
    agent.slug = "close-agent"
    agent.id = 11

    with patch("backend.api.database.async_session", side_effect=lambda: _FakeSession()), \
         patch("backend.api.models.AlgoOrder", side_effect=_FakeRow), \
         patch("backend.api.algo.order_events.write_event", new=AsyncMock()):
        for action_type in ("close_position", "chase_close_positions", "place_order"):
            captured_kwargs.clear()
            await _write_live_order(
                agent, action_type,
                {"account": "ZG0790", "symbol": "NIFTY25OCTFUT", "side": "SELL",
                 "qty": 50, "price": 100.0, "exchange": "NFO"},
            )
            assert "product" not in captured_kwargs, (
                f"_write_live_order unexpectedly set 'product' for "
                f"action_type={action_type!r} — this shared constructor must "
                f"stay untouched; product is set by a place_order-only "
                f"follow-up UPDATE instead."
            )
            assert "template_id" not in captured_kwargs, (
                f"_write_live_order unexpectedly set 'template_id' for "
                f"action_type={action_type!r}"
            )
            # agent_id attribution still flows correctly for every action type.
            assert captured_kwargs.get("agent_id") == 11


def test_action_live_close_position_source_never_calls_product_template_helper():
    """Source-level guard: _action_live_close_position and
    _action_live_chase_close_positions must never reference
    _place_order_set_product_template — that helper is place_order-only."""
    from backend.api.algo import actions_live

    close_src = inspect.getsource(actions_live._action_live_close_position)
    chase_src = inspect.getsource(actions_live._action_live_chase_close_positions)

    assert "_place_order_set_product_template" not in close_src
    assert "_place_order_set_product_template" not in chase_src


# ═══════════════════════════════════════════════════════════════════════════
# 4. _fetch_net_position_qty — None on unresolvable account column
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_fetch_net_position_qty_returns_none_without_account_column():
    """When no column name contains 'account' or 'user', the function must
    return None instead of silently matching a symbol row from ANY
    account."""
    from backend.api.routes.orders_place import _fetch_net_position_qty

    # DataFrame deliberately has no account/user-like column.
    df = pd.DataFrame([
        {"tradingsymbol": "NIFTY25OCTFUT", "quantity": 999, "exchange": "NFO"},
    ])

    with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]):
        result = await _fetch_net_position_qty("NIFTY25OCTFUT", "NFO", "ZG0790")

    assert result is None


@pytest.mark.asyncio
async def test_fetch_net_position_qty_returns_correct_qty_when_unambiguous():
    """Baseline (unchanged) behaviour: with a real account column present
    and a matching row, the signed quantity for the matching account is
    returned."""
    from backend.api.routes.orders_place import _fetch_net_position_qty

    df = pd.DataFrame([
        {"tradingsymbol": "NIFTY25OCTFUT", "quantity": -50,
         "exchange": "NFO", "account": "ZG0790"},
        {"tradingsymbol": "NIFTY25OCTFUT", "quantity": 999,
         "exchange": "NFO", "account": "OTHERACCT"},
    ])

    with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]):
        result = await _fetch_net_position_qty("NIFTY25OCTFUT", "NFO", "ZG0790")

    assert result == -50.0


@pytest.mark.asyncio
async def test_fetch_net_position_qty_no_matching_symbol_returns_none():
    """Unchanged behaviour: a present account column but no symbol match
    still returns None (not an exception)."""
    from backend.api.routes.orders_place import _fetch_net_position_qty

    df = pd.DataFrame([
        {"tradingsymbol": "BANKNIFTY25OCTFUT", "quantity": 50,
         "exchange": "NFO", "account": "ZG0790"},
    ])

    with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]):
        result = await _fetch_net_position_qty("NIFTY25OCTFUT", "NFO", "ZG0790")

    assert result is None
