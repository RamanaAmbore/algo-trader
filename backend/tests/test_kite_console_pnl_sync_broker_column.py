"""Tests for the `kite_console_pnl_sync.broker` column (added 2026-11-27
for the Dhan historical trade/ledger backfill) — the migration, the ORM
model's own default, and the shared `scripts/kite_console_pnl_sync.py`
functions threading a `broker` kwarg through to the stored row payload.
"""

from __future__ import annotations

import inspect

import pytest


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

class _FakeTxConn:
    """Mirrors the fake-connection convention used by every other
    `_migrate_*` test in this codebase (see test_agent_close_hold_infra.py)
    — records every executed SQL string, no real DB needed."""

    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, stmt):
        self.executed.append(str(stmt))


class TestKiteConsolePnlSyncBrokerMigration:
    def test_migration_emits_idempotent_add_column_with_default(self):
        from backend.api.database import _migrate_kite_console_pnl_sync_broker
        src = inspect.getsource(_migrate_kite_console_pnl_sync_broker)
        assert "ADD COLUMN IF NOT EXISTS" in src
        assert "broker VARCHAR(16)" in src
        assert "DEFAULT 'kite'" in src
        assert "CREATE INDEX IF NOT EXISTS" in src

    @pytest.mark.asyncio
    async def test_migration_statement_shape_against_fake_conn(self):
        import asyncio
        from backend.api import database as dbmod
        conn = _FakeTxConn()
        await dbmod._migrate_kite_console_pnl_sync_broker(conn)
        joined = "\n".join(conn.executed)
        assert (
            "ALTER TABLE kite_console_pnl_sync ADD COLUMN IF NOT EXISTS "
            "broker VARCHAR(16) DEFAULT 'kite'" in joined
        )
        assert "UPDATE kite_console_pnl_sync SET broker = 'kite' WHERE broker IS NULL" in joined
        assert any("lock_timeout" in s for s in conn.executed)

    def test_migration_is_registered_in_init_db_after_the_columns_migration(self):
        from backend.api import database as db_mod
        src = inspect.getsource(db_mod.init_db)
        lines = src.splitlines()
        begin_idx = next(i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l)
        begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

        def _idx(marker):
            i = next(j for j, l in enumerate(lines) if marker in l)
            return i, len(lines[i]) - len(lines[i].lstrip())

        cols_idx, _ = _idx("_migrate_kite_console_pnl_sync_columns(conn)")
        broker_idx, broker_indent = _idx("_migrate_kite_console_pnl_sync_broker(conn)")

        assert broker_indent > begin_indent, (
            "_migrate_kite_console_pnl_sync_broker(conn) must run INSIDE "
            "the engine.begin() block"
        )
        assert broker_idx > cols_idx, (
            "_migrate_kite_console_pnl_sync_broker(conn) must run after "
            "_migrate_kite_console_pnl_sync_columns(conn)"
        )

    def test_migration_runs_before_the_drop_order_templates_migration(self):
        """The drop migration's own docstring says it MUST run last in
        the transaction — confirm the broker migration doesn't land
        after it."""
        from backend.api import database as db_mod
        src = inspect.getsource(db_mod.init_db)
        lines = src.splitlines()

        def _idx(marker):
            return next(j for j, l in enumerate(lines) if marker in l)

        broker_idx = _idx("_migrate_kite_console_pnl_sync_broker(conn)")
        drop_idx = _idx("_migrate_drop_order_templates_and_bracket_columns(conn)")
        assert broker_idx < drop_idx


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class TestKiteConsolePnlSyncModelBrokerColumn:
    def test_column_exists_with_server_default_kite(self):
        from backend.api.models import KiteConsolePnlSync
        col = KiteConsolePnlSync.__table__.c.broker
        assert col.nullable is False
        assert col.server_default is not None
        assert "kite" in str(col.server_default.arg)

    def test_broker_index_exists(self):
        from backend.api.models import KiteConsolePnlSync
        index_names = {ix.name for ix in KiteConsolePnlSync.__table__.indexes}
        assert "ix_kite_console_pnl_sync_broker" in index_names

    def test_unique_constraint_unchanged_does_not_include_broker(self):
        """Confirmed deliberate — see the model's own column comment:
        the masked `account` form already differs by broker-letter
        prefix, so a Kite and Dhan row can never collide on
        (account, report_type, dedup_key)."""
        from backend.api.models import KiteConsolePnlSync
        uq = next(
            c for c in KiteConsolePnlSync.__table__.constraints
            if getattr(c, "name", "") == "uq_kite_console_pnl_sync_account_type_key"
        )
        cols = {c.name for c in uq.columns}
        assert cols == {"account", "report_type", "dedup_key"}


# ---------------------------------------------------------------------------
# Shared kite_console_pnl_sync.py functions thread `broker` through
# ---------------------------------------------------------------------------

class TestBrokerKwargThreading:
    def test_build_sync_row_defaults_to_kite(self):
        from scripts.kite_console_pnl_sync import build_sync_row
        row = build_sync_row(
            "Z1####", "equity", "tradebook",
            {"symbol": "INFY", "trade_date": "2026-04-05", "quantity": "1",
             "price": "100", "trade_id": "T1", "order_id": "O1", "exchange": "NSE",
             "trade_type": "BUY"},
        )
        assert row["broker"] == "kite"

    def test_build_sync_row_accepts_dhan(self):
        from scripts.kite_console_pnl_sync import build_sync_row
        row = build_sync_row(
            "D1####", "equity", "tradebook",
            {"symbol": "INFY", "trade_date": "2026-04-05", "quantity": "1",
             "price": "100", "trade_id": "T1", "order_id": "O1", "exchange": "NSE",
             "trade_type": "BUY"},
            broker="dhan",
        )
        assert row["broker"] == "dhan"

    def test_build_ledger_sync_row_defaults_to_kite(self):
        from scripts.kite_console_pnl_sync import build_ledger_sync_row
        from datetime import date
        row = build_ledger_sync_row(
            "Z1####", {"posting_date": "2026-04-05", "particulars": "x",
                       "debit": "0", "credit": "0", "net_balance": "0"},
            seq=0, pull_start=date(2026, 4, 1), pull_end=date(2026, 4, 30),
        )
        assert row["broker"] == "kite"

    def test_build_ledger_sync_row_accepts_dhan(self):
        from scripts.kite_console_pnl_sync import build_ledger_sync_row
        from datetime import date
        row = build_ledger_sync_row(
            "D1####", {"posting_date": "2026-04-05", "particulars": "x",
                       "debit": "0", "credit": "0", "net_balance": "0"},
            seq=0, pull_start=date(2026, 4, 1), pull_end=date(2026, 4, 30),
            broker="dhan",
        )
        assert row["broker"] == "dhan"

    @pytest.mark.asyncio
    async def test_ingest_rows_passes_broker_through_to_build_sync_row(self):
        from unittest.mock import AsyncMock, patch
        from scripts.kite_console_pnl_sync import ingest_rows

        session = AsyncMock()
        with patch("scripts.kite_console_pnl_sync.build_sync_row") as mock_build:
            mock_build.return_value = {
                "broker": "dhan", "account": "D1####", "segment": "equity",
                "report_type": "tradebook", "dedup_key": "k",
            }
            await ingest_rows(
                session, "D1####", "equity", "tradebook",
                [{"symbol": "INFY"}], broker="dhan",
            )
        _, kwargs = mock_build.call_args
        assert kwargs["broker"] == "dhan"

    @pytest.mark.asyncio
    async def test_ingest_ledger_rows_filters_delete_by_broker(self):
        from unittest.mock import AsyncMock
        from datetime import date
        from scripts.kite_console_pnl_sync import ingest_ledger_rows

        session = AsyncMock()
        await ingest_ledger_rows(
            session, "D1####", [], pull_start=date(2026, 7, 1),
            pull_end=date(2026, 7, 31), broker="dhan",
        )
        delete_stmt = session.execute.call_args_list[0].args[0]
        compiled = str(delete_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "broker" in compiled
        assert "dhan" in compiled
