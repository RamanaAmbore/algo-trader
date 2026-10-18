"""
Tests for the retired manual position-pin feature's teardown migration.

The manual-pin feature (`PositionPair` ORM model, `/api/positions/pin`
routes, `PositionRow.pin_group_key`) was removed per operator decision.
`_migrate_position_pairs_table` (which used to CREATE the table) is
replaced by `_migrate_drop_position_pairs_table`, an idempotent one-time
DROP wired into the same `init_db()` call-site so any already-deployed
environment with the table gets it dropped on next startup, while a
fresh environment never creates it (mirrors the `_FakeTxConn` pattern
used by `test_agents_cli_source_migration.py` for other additive/raw-DDL
migration steps in this file).

Covers:
  1. `_migrate_drop_position_pairs_table` issues a single idempotent
     `DROP TABLE IF EXISTS position_pairs` statement.
  2. The old creation function `_migrate_position_pairs_table` no longer
     exists on the module.
  3. `init_db()` calls `_migrate_drop_position_pairs_table(conn)` inside
     the `async with engine.begin() as conn:` transactional block (not
     the old creation function).
"""

from __future__ import annotations

import asyncio
import inspect

import backend.api.database as dbmod


class _FakeResult:
    def fetchone(self): return None
    def fetchall(self): return []
    def scalar(self): return None
    def scalar_one_or_none(self): return None
    def all(self): return []
    def first(self): return None
    def scalars(self): return self
    def __iter__(self): return iter(())


class _FakeTxConn:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(str(stmt))
        return _FakeResult()

    async def run_sync(self, fn, *a, **k):
        return None


def test_migrate_drop_position_pairs_table_statement_shape():
    """Issues exactly one idempotent DROP TABLE IF EXISTS statement."""
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_drop_position_pairs_table(conn))

    assert len(conn.executed) == 1
    joined = "\n".join(conn.executed)
    assert "DROP TABLE IF EXISTS position_pairs" in joined


def test_migrate_drop_position_pairs_table_is_idempotent():
    """Calling it twice (simulating two startups) never raises, and issues
    the same safe DROP statement both times."""
    conn = _FakeTxConn()
    asyncio.run(dbmod._migrate_drop_position_pairs_table(conn))
    asyncio.run(dbmod._migrate_drop_position_pairs_table(conn))

    assert len(conn.executed) == 2
    assert all("DROP TABLE IF EXISTS position_pairs" in s for s in conn.executed)


def test_old_position_pairs_create_migration_removed():
    """The retired creation function no longer exists on the module."""
    assert not hasattr(dbmod, "_migrate_position_pairs_table")


def test_init_db_calls_drop_position_pairs_migration_inside_begin_block():
    """Structural guard: init_db must call
    _migrate_drop_position_pairs_table(conn) INSIDE the
    `async with engine.begin() as conn:` block, replacing the old
    creation call at the same call-site."""
    src = inspect.getsource(dbmod.init_db)
    assert "_migrate_drop_position_pairs_table(conn)" in src
    assert "_migrate_position_pairs_table(conn)" not in src.replace(
        "_migrate_drop_position_pairs_table(conn)", ""
    )
