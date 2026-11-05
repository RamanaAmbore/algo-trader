"""test_agent_event_detail_nan_safe.py

2026-11-05 live operator report: "In agent log, I see many nan values
in stringified json. check correct it if required."

Root cause: `AgentEvent.detail` is a plain `Text` column (not JSONB),
written by `agent_engine.py:record_manual_event()` /
`record_chase_terminal()` via a bare `json.dumps(detail)`. Python's
json encoder does not raise on `float('nan')` — it emits a bare,
unquoted `NaN` token, which round-trips fine through Python's own
lenient decoder but is not valid JSON and is never validated by the
DB (a plain Text column accepts any string). A chase-terminal
`slippage`/`final_price` computed from a broker price that was itself
a pandas/numpy NaN (the same missing-vs-zero trap documented elsewhere
in this codebase — `nan or x` evaluates to `nan`, never `x`, because
`bool(float('nan'))` is `True`) is the likely source. The Agents log
viewer (LogPanel.svelte) then shows that literal `NaN` token embedded
in the stringified `detail` JSON.

Fix: `_ae_detail_json_nan_safe()` replaces any NaN float with `None`
before dumping, so the stored text is always valid JSON and the
Agents log viewer shows "null"/nothing instead of a stray NaN token.

Five quality dimensions:
  SSOT        — both `record_manual_event` and `record_chase_terminal`
                route through the one shared sanitizer; a test of the
                sanitizer alone, plus one end-to-end integration test
                through a real (in-process sqlite) `agent_events`
                table, proves the DB-stored text is actually valid.
  Correctness — NaN -> null; a genuine non-NaN float, int, string, and
                None are all passed through unchanged.
  Perf        — n/a (no broker/network calls).
  Reuse       — mirrors the sqlite_env fixture pattern already used by
                test_agents_recent_events_manual_filter.py for
                AgentEvent-table tests.
  UX          — every assert carries an f-string with actual vs expected.
"""
from __future__ import annotations

import json
import math

import pytest
import pytest_asyncio


class TestDetailJsonNanSafeUnit:
    def test_nan_float_becomes_null(self):
        from backend.api.algo.agent_engine import _ae_detail_json_nan_safe

        out = _ae_detail_json_nan_safe({"slippage": float("nan"), "symbol": "X"})
        parsed = json.loads(out)  # must not raise — valid JSON
        assert parsed["slippage"] is None, f"expected null, got {parsed['slippage']!r}"
        assert parsed["symbol"] == "X"

    def test_bare_json_dumps_would_have_emitted_an_invalid_nan_token(self):
        """Documents the defect this fix guards against — a plain
        `json.dumps` on the same dict produces a bare, invalid `NaN`
        token in the output text (the literal bug the operator saw)."""
        raw = json.dumps({"slippage": float("nan")})
        assert "NaN" in raw and '"NaN"' not in raw, f"expected bare NaN token, got {raw!r}"

    def test_non_nan_values_pass_through_unchanged(self):
        from backend.api.algo.agent_engine import _ae_detail_json_nan_safe

        detail = {"qty": 5, "price": 112.5, "symbol": "X", "note": None}
        out = _ae_detail_json_nan_safe(detail)
        assert json.loads(out) == detail

    def test_infinity_is_not_touched_by_this_fix(self):
        """Scope check — this fix targets NaN specifically (the reported
        symptom and the known missing-vs-zero trap). `math.isinf` values
        are a separate, unreported concern and are deliberately left
        alone here rather than silently expanding scope."""
        from backend.api.algo.agent_engine import _ae_detail_json_nan_safe

        out = _ae_detail_json_nan_safe({"x": math.inf})
        assert json.loads(out)["x"] == math.inf


@pytest_asyncio.fixture
async def agent_event_sqlite_env():
    """Creates the real `agent_events` table (the actual declarative
    model, not a hand-rolled copy) against an in-memory sqlite engine,
    and patches `agent_engine.async_session` to use it.
    `record_chase_terminal` takes `agent_id` directly (no `Agent`-table
    query of its own), and sqlite does not enforce FK constraints by
    default, so a real `agents` row is unnecessary here — the real
    `Agent` model's JSONB columns aren't sqlite-renderable anyway.
    Mirrors test_agents_recent_events_manual_filter.py's sqlite_env."""
    from unittest.mock import patch
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    import backend.api.algo.agent_engine as ae
    from backend.api.models import AgentEvent

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: AgentEvent.metadata.create_all(
                sync_conn, tables=[AgentEvent.__table__],
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    p = patch.object(ae, "async_session", factory)
    p.start()
    try:
        yield factory, 1
    finally:
        p.stop()
        await engine.dispose()


class TestRecordChaseTerminalNanSafeIntegration:
    @pytest.mark.asyncio
    async def test_nan_slippage_stores_valid_json_not_a_bare_nan_token(self, agent_event_sqlite_env):
        from sqlalchemy import select
        from backend.api.algo.agent_engine import record_chase_terminal
        from backend.api.models import AgentEvent

        factory, agent_id = agent_event_sqlite_env

        await record_chase_terminal(
            agent_id=agent_id, outcome="chase_fill", symbol="CRUDEOIL26OCTFUT",
            side="SELL", qty=1, final_price=float("nan"), attempts=2,
            slippage=float("nan"),
        )

        async with factory() as session:
            row = (await session.execute(select(AgentEvent))).scalar_one()

        # The stored text must itself be valid JSON (the defect: a bare
        # `json.dumps` would store an unquoted, invalid `NaN` token).
        parsed = json.loads(row.detail)
        assert parsed["slippage"] is None, f"expected null slippage, got raw text: {row.detail!r}"
        assert parsed["final_price"] is None, f"expected null final_price, got raw text: {row.detail!r}"
        assert "NaN" not in row.detail, f"bare NaN token leaked into stored detail: {row.detail!r}"
        assert parsed["symbol"] == "CRUDEOIL26OCTFUT"

    @pytest.mark.asyncio
    async def test_real_slippage_value_is_unaffected(self, agent_event_sqlite_env):
        from sqlalchemy import select
        from backend.api.algo.agent_engine import record_chase_terminal
        from backend.api.models import AgentEvent

        factory, agent_id = agent_event_sqlite_env

        await record_chase_terminal(
            agent_id=agent_id, outcome="chase_fill", symbol="NIFTY26OCTFUT",
            side="BUY", qty=75, final_price=25010.5, attempts=1, slippage=12.5,
        )

        async with factory() as session:
            row = (await session.execute(select(AgentEvent))).scalar_one()

        parsed = json.loads(row.detail)
        assert parsed["slippage"] == 12.5
        assert parsed["final_price"] == 25010.5
