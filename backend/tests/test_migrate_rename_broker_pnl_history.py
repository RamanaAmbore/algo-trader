"""Tests for `_migrate_rename_kite_console_pnl_sync_to_broker_pnl_history`
(backend/api/database.py) — the in-place live-data table rename
`kite_console_pnl_sync` -> `broker_pnl_history` (2026-11-28).

Why a fake connection, not a real Postgres/SQLite engine: the function's
own existence check uses `information_schema.tables`, and its DDL uses
`ALTER TABLE ... RENAME CONSTRAINT`, both Postgres-specific (SQLite has
no `information_schema` and no `RENAME CONSTRAINT`). This mirrors the
established, already-accepted convention in this codebase for testing a
Postgres-only migration without a live Postgres instance — see
`test_migrate_drop_position_pairs.py`'s `_FakeTxConn` and
`test_kite_console_pnl_sync_broker_column.py`'s own fake-conn tests for
`_migrate_kite_console_pnl_sync_broker` (a sibling raw-DDL migration in
the exact same file). `_FakeExistenceConn` below extends that pattern
with a configurable `scalar()` return per existence-check query, which
the simpler `_FakeTxConn` (always returns `None`/`[]`) cannot support.

NOTE for whoever deploys this: statement-shape coverage here proves the
function issues the RIGHT SQL in the RIGHT ORDER for all four
old/new-table-existence states, and that a cosmetic index/constraint
rename failure never blocks the table rename or propagates. It does NOT
prove real Postgres accepts that exact SQL, or that real row data
survives a real `ALTER TABLE ... RENAME TO` round-trip under a live
Postgres engine — no test in this suite does, for any Postgres-only
migration in this file. Verify once against a real dev/staging Postgres
with the real pre-existing `kite_console_pnl_sync` table before this
reaches prod.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import backend.api.database as dbmod


class _FakeExistenceResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeSavepointCtx:
    """No-op stand-in for the real `AsyncConnection.begin_nested()` —
    a real SAVEPOINT's whole purpose is to isolate ONE statement's
    failure from the surrounding Postgres transaction's abort state,
    which a bare Python try/except cannot do (catching the exception
    does not clear the server-side aborted-transaction state). This
    fake has no transaction to abort, so it only needs to let a raised
    exception propagate normally out of the `async with` block, exactly
    like the real SAVEPOINT's own `ROLLBACK TO SAVEPOINT` does for the
    one statement without taking down the rest of the transaction."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False  # never swallow — the caller's own try/except does that


class _FakeExistenceConn:
    """Fake `AsyncConnection` — records every executed statement, and
    answers the two `information_schema.tables` EXISTS probes
    according to `table_exists` (a `{name: bool}` map), matching on the
    bound `:t` parameter. Any other statement (the actual DDL) is just
    recorded and returns a generic "no rows" result."""

    def __init__(self, table_exists: dict):
        self.table_exists = table_exists
        self.executed: "list[str]" = []

    async def execute(self, stmt, params=None, *a, **k):
        text = str(stmt)
        self.executed.append(text)
        if "information_schema.tables" in text and params and "t" in params:
            return _FakeExistenceResult(bool(self.table_exists.get(params["t"], False)))
        return _FakeExistenceResult(None)

    def begin_nested(self):
        self.begin_nested_calls = getattr(self, "begin_nested_calls", 0) + 1
        return _FakeSavepointCtx()


def _ddl_statements(conn: "_FakeExistenceConn") -> "list[str]":
    """Every executed statement that is NOT one of the two EXISTS probes."""
    return [s for s in conn.executed if "information_schema.tables" not in s]


class TestOldTableOnlyRenames:
    def test_renames_table_and_logs_info(self, monkeypatch):
        """`backend.shared.helpers.ramboq_logger.get_logger()` sets
        `propagate = False`, so pytest's `caplog` (which hooks the root
        logger) never sees these records — mirrors how
        `test_dhan_pnl_reconciliation.py` checks this module's own log
        calls (monkeypatching the logger method directly), not `caplog`."""
        info_mock = MagicMock()
        monkeypatch.setattr(dbmod.logger, "info", info_mock)
        conn = _FakeExistenceConn({"kite_console_pnl_sync": True, "broker_pnl_history": False})
        asyncio.run(dbmod._migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn))

        ddl = _ddl_statements(conn)
        assert any(
            "ALTER TABLE kite_console_pnl_sync RENAME TO broker_pnl_history" in s
            for s in ddl
        ), ddl
        info_mock.assert_called_once()
        assert "renamed" in info_mock.call_args[0][0]

    def test_sets_lock_timeout_before_the_rename(self):
        conn = _FakeExistenceConn({"kite_console_pnl_sync": True, "broker_pnl_history": False})
        asyncio.run(dbmod._migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn))

        ddl = _ddl_statements(conn)
        lock_idx = next(i for i, s in enumerate(ddl) if "lock_timeout" in s)
        rename_idx = next(
            i for i, s in enumerate(ddl)
            if "ALTER TABLE kite_console_pnl_sync RENAME TO broker_pnl_history" in s
        )
        assert lock_idx < rename_idx

    def test_renames_constraint_and_every_index(self):
        conn = _FakeExistenceConn({"kite_console_pnl_sync": True, "broker_pnl_history": False})
        asyncio.run(dbmod._migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn))

        ddl = "\n".join(_ddl_statements(conn))
        assert (
            "ALTER TABLE broker_pnl_history RENAME CONSTRAINT "
            "uq_kite_console_pnl_sync_account_type_key "
            "TO uq_broker_pnl_history_account_type_key" in ddl
        )
        for old, new in (
            ("ix_kite_console_pnl_sync_account_segment", "ix_broker_pnl_history_account_segment"),
            ("ix_kite_console_pnl_sync_symbol", "ix_broker_pnl_history_symbol"),
            ("ix_kite_console_pnl_sync_expiry", "ix_broker_pnl_history_expiry"),
            ("ix_kite_console_pnl_sync_root", "ix_broker_pnl_history_root"),
            ("ix_kite_console_pnl_sync_broker", "ix_broker_pnl_history_broker"),
        ):
            assert f"ALTER INDEX IF EXISTS {old} RENAME TO {new}" in ddl, ddl

    def test_every_cosmetic_rename_runs_inside_its_own_savepoint(self):
        """Pins the real fix, not just the statement text: each of the 6
        cosmetic rename statements (1 constraint + 5 indexes) must run
        inside its OWN `conn.begin_nested()` SAVEPOINT, never a bare
        try/except — a bare try/except catches the Python exception but
        leaves the surrounding Postgres transaction in its server-side
        'aborted' state, which would crash every later migration in the
        same `engine.begin()` block and the table rename's own
        already-committed-so-far work along with it."""
        conn = _FakeExistenceConn({"kite_console_pnl_sync": True, "broker_pnl_history": False})
        asyncio.run(dbmod._migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn))

        assert conn.begin_nested_calls == 6, (
            "expected exactly 6 savepoints — 1 constraint rename + 5 "
            "index renames — got %d" % conn.begin_nested_calls
        )

    def test_constraint_rename_happens_after_table_rename(self):
        """The table RENAME TO must land before any index/constraint
        rename targets `broker_pnl_history` by its NEW name — a stale
        ordering would target a table that (from Postgres's own
        perspective mid-transaction) doesn't yet have that name."""
        conn = _FakeExistenceConn({"kite_console_pnl_sync": True, "broker_pnl_history": False})
        asyncio.run(dbmod._migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn))

        ddl = _ddl_statements(conn)
        rename_idx = next(
            i for i, s in enumerate(ddl)
            if "ALTER TABLE kite_console_pnl_sync RENAME TO broker_pnl_history" in s
        )
        constraint_idx = next(
            i for i, s in enumerate(ddl) if "RENAME CONSTRAINT" in s
        )
        assert rename_idx < constraint_idx


class TestNewTableOnlyNoOps:
    def test_no_ddl_issued_at_all(self):
        """Already renamed on a prior boot, or create_all() will create
        broker_pnl_history fresh next — either way, nothing to do."""
        conn = _FakeExistenceConn({"kite_console_pnl_sync": False, "broker_pnl_history": True})
        asyncio.run(dbmod._migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn))

        assert _ddl_statements(conn) == []


class TestNeitherTableExistsNoOps:
    def test_no_ddl_issued_at_all(self):
        """Genuinely fresh DB — create_all() creates broker_pnl_history
        next, with no prior history to preserve."""
        conn = _FakeExistenceConn({"kite_console_pnl_sync": False, "broker_pnl_history": False})
        asyncio.run(dbmod._migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn))

        assert _ddl_statements(conn) == []


class TestBothTablesExistRefusesToTouchEither:
    def test_logs_critical_and_issues_no_ddl(self, monkeypatch):
        """This should never happen in normal operation. A human must
        resolve it manually — this migration must never guess which
        table to keep, drop, or merge; either choice risks destroying
        real P&L/tradebook/ledger history."""
        critical_mock = MagicMock()
        monkeypatch.setattr(dbmod.logger, "critical", critical_mock)
        conn = _FakeExistenceConn({"kite_console_pnl_sync": True, "broker_pnl_history": True})
        asyncio.run(dbmod._migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn))

        assert _ddl_statements(conn) == []
        critical_mock.assert_called_once()


class TestIdempotentAcrossRepeatedBoots:
    def test_second_call_after_a_successful_rename_is_a_no_op(self):
        """Simulates two consecutive app startups against the SAME
        database: the first call renames; a second call (as if init_db()
        ran again on the next deploy) must see the new name already in
        place and do nothing — never attempt to rename a table that no
        longer exists under the old name."""
        table_exists = {"kite_console_pnl_sync": True, "broker_pnl_history": False}
        conn = _FakeExistenceConn(table_exists)
        asyncio.run(dbmod._migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn))
        assert len(_ddl_statements(conn)) > 0

        # Simulate the real post-rename DB state for the second boot.
        table_exists["kite_console_pnl_sync"] = False
        table_exists["broker_pnl_history"] = True
        conn.executed.clear()
        asyncio.run(dbmod._migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn))
        assert _ddl_statements(conn) == []


class TestCosmeticRenameFailureIsolation:
    """An index/constraint rename is cosmetic-only — the one UPSERT in
    this table (`ingest_rows()`) matches by column list
    (`index_elements=`), never by constraint name, so a stale name can
    never break it. A failure here must be logged and swallowed, never
    propagated, and must never undo or block the table rename, which
    already succeeded by the time these statements run."""

    def test_a_failing_index_rename_does_not_raise_and_does_not_stop_the_others(self):
        class _FlakyConn(_FakeExistenceConn):
            async def execute(self, stmt, params=None, *a, **k):
                text = str(stmt)
                if "ALTER INDEX IF EXISTS ix_kite_console_pnl_sync_symbol" in text:
                    raise RuntimeError("simulated: index already renamed by a concurrent process")
                return await super().execute(stmt, params, *a, **k)

        conn = _FlakyConn({"kite_console_pnl_sync": True, "broker_pnl_history": False})
        # Must not raise.
        asyncio.run(dbmod._migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn))

        ddl = "\n".join(_ddl_statements(conn))
        assert "ALTER TABLE kite_console_pnl_sync RENAME TO broker_pnl_history" in ddl
        # The OTHER index renames still ran, after the one that raised.
        assert "ix_kite_console_pnl_sync_expiry" in ddl
        assert "ix_kite_console_pnl_sync_root" in ddl
        assert "ix_kite_console_pnl_sync_broker" in ddl


class TestWiredBeforeCreateTables:
    def test_rename_migration_runs_before_create_all(self):
        """MUST run before `_migrate_create_tables()`'s `create_all()` —
        otherwise create_all would create a brand-new EMPTY
        broker_pnl_history table while real history sits orphaned under
        the old name. Confirms the call ORDER in init_db()'s own source,
        not just that both calls exist somewhere in it."""
        src = inspect.getsource(dbmod.init_db)
        rename_idx = src.index(
            "_migrate_rename_kite_console_pnl_sync_to_broker_pnl_history(conn)"
        )
        create_idx = src.index("_migrate_create_tables(conn)")
        assert rename_idx < create_idx

    def test_additive_broker_and_columns_migrations_target_the_new_name(self):
        """The two pre-existing additive migrations
        (`_migrate_kite_console_pnl_sync_columns`/`_broker`) run AFTER
        the rename in init_db()'s ordered list — by the time they run,
        the table is already `broker_pnl_history`, so their own SQL
        must target that name, not the old one (kept function names,
        updated SQL bodies — see each function's own docstring)."""
        cols_src = inspect.getsource(dbmod._migrate_kite_console_pnl_sync_columns)
        broker_src = inspect.getsource(dbmod._migrate_kite_console_pnl_sync_broker)
        assert "ALTER TABLE broker_pnl_history" in cols_src
        assert "ALTER TABLE kite_console_pnl_sync ADD COLUMN" not in cols_src
        assert "ALTER TABLE broker_pnl_history" in broker_src
        assert "ALTER TABLE kite_console_pnl_sync ADD COLUMN" not in broker_src
        assert "UPDATE broker_pnl_history SET broker" in broker_src
        assert "UPDATE kite_console_pnl_sync SET broker" not in broker_src
