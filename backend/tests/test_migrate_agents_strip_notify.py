"""
Tests for `_migrate_agents_strip_notify_from_actions` — the 2026-11
channel-authoring retirement's one-time data normalization: strips the
retired `notify` sibling key from every entry of every `agents.actions`
JSONB array.

Mirrors the `_FakeTxConn` statement-shape convention
`test_agent_preamble_execution.py`/`test_migrate_drop_position_pairs.py`
already use for every other migration in `database.py` — this codebase's
migration tests assert on the SQL text and call-ordering, never against
a real Postgres connection.

Covers:
  1. The migration issues a single idempotent `UPDATE agents ... WHERE
     EXISTS (...)` statement that rebuilds `actions` via `jsonb_agg`
     over `jsonb_array_elements(...) WITH ORDINALITY`, ordered by that
     ordinality column — action order must be explicit, never left to
     an unspecified iteration order.
  2. `elem - 'notify'` only runs on an object element (`jsonb_typeof`
     guard), never raising on a malformed non-object entry.
  3. `SET LOCAL lock_timeout` is set, matching every other migration.
  4. `init_db()` calls it inside the `engine.begin()` transactional
     block, after `_migrate_agents_require_release(conn)`.
  5. Scope boundary — this migration does NOT touch `events`,
     `has_explicit_alert`, or `preamble`. The 2026-11 plan's `alert(...)`
     DSL clause and `agent.events`/`has_explicit_alert` remain a
     legitimate, untouched per-agent channel-selection mechanism (per
     the operator's explicit correction) — only the `notify=[...]`
     action-call parameter and the two hardcoded producer literals are
     retired.
"""

from __future__ import annotations

import asyncio
import inspect

import backend.api.database as dbmod


class _FakeTxConn:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(str(stmt))
        from unittest.mock import MagicMock
        return MagicMock(scalar_one_or_none=lambda: None)


def test_migrate_agents_strip_notify_statement_shape():
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_agents_strip_notify_from_actions(conn))

    joined = "\n".join(conn.executed)
    assert "UPDATE agents" in joined
    assert "jsonb_agg" in joined
    assert "jsonb_array_elements(actions) WITH ORDINALITY AS t(elem, ord)" in joined
    assert "ORDER BY ord" in joined
    assert "elem - 'notify'" in joined
    assert "jsonb_typeof(elem) = 'object'" in joined
    assert "WHERE  actions IS NOT NULL" in joined or "WHERE actions IS NOT NULL" in joined
    assert "EXISTS" in joined
    assert "e ? 'notify'" in joined
    assert any("lock_timeout" in s for s in conn.executed)
    assert not any("CONCURRENTLY" in s for s in conn.executed)


def test_migrate_agents_strip_notify_is_idempotent_statement_wise():
    """Calling it twice (simulating two startups) never raises, and
    issues the exact same (WHERE-guarded, hence idempotent at the data
    level) statement both times."""
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_agents_strip_notify_from_actions(conn))
    first = list(conn.executed)
    conn2 = _FakeTxConn()
    asyncio.run(dbmod._migrate_agents_strip_notify_from_actions(conn2))
    second = list(conn2.executed)
    assert first == second


def test_migrate_agents_strip_notify_does_not_touch_events_or_has_explicit_alert():
    """Scope guard — the migration's own SQL text must never reference
    `events`, `has_explicit_alert`, or `preamble`. Those remain a live,
    untouched per-agent channel-selection mechanism (the `alert(...)`
    DSL clause) per the operator's explicit correction mid-plan."""
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_agents_strip_notify_from_actions(conn))
    joined = "\n".join(conn.executed)
    assert "events" not in joined
    assert "has_explicit_alert" not in joined
    assert "preamble" not in joined


def test_init_db_calls_strip_notify_migration_after_require_release():
    src = inspect.getsource(dbmod.init_db)
    lines = src.splitlines()
    begin_idx = next(
        i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l
    )
    begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

    def _idx(marker):
        i = next(j for j, l in enumerate(lines) if marker in l and "async def" not in l)
        return i, len(lines[i]) - len(lines[i].lstrip())

    release_idx, _ = _idx("_migrate_agents_require_release(conn)")
    strip_idx, strip_indent = _idx("_migrate_agents_strip_notify_from_actions(conn)")

    assert strip_indent > begin_indent, (
        "_migrate_agents_strip_notify_from_actions(conn) must run INSIDE "
        "the engine.begin() transactional block"
    )
    assert strip_idx > release_idx, (
        "_migrate_agents_strip_notify_from_actions(conn) must run after "
        "_migrate_agents_require_release(conn)"
    )
