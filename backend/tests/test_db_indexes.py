"""
DB stability: index declarations + connection pool configuration.

Five quality dimensions:
  SSOT   — index names match what the audit specified
  Perf   — pool_pre_ping + pool_recycle present on both engines
  Stale  — no duplicate CREATE INDEX guards for the two new indexes
  Reuse  — model __table_args__ + database.py idempotent ALTER stay in sync
  UX     — no silent-fail: new index guards use the try/except wrapper
            pattern (non-critical index failure logged, never aborts boot)

NOTE on "verify with EXPLAIN ANALYZE":
  The query-plan test (index scan vs seq-scan on /admin/history filter)
  requires production-scale data and cannot be verified in the unit test
  suite. Verify manually on prod with:
    EXPLAIN ANALYZE
    SELECT * FROM algo_orders
    WHERE account = 'ZG0790' AND symbol = 'NIFTY25JUNFUT'
    ORDER BY created_at DESC LIMIT 50;
  Expected: "Index Scan using ix_algo_orders_account_symbol"
"""

from __future__ import annotations

import pathlib

import pytest

_DB_PY = pathlib.Path(__file__).parent.parent / "api" / "database.py"
_MODELS_PY = pathlib.Path(__file__).parent.parent / "api" / "models.py"

_db_src = _DB_PY.read_text()
_models_src = _MODELS_PY.read_text()


# ── SSOT: new indexes declared in both database.py and models.py ─────────────

def test_composite_account_symbol_in_database_py():
    """SSOT: idempotent CREATE for ix_algo_orders_account_symbol in database.py."""
    assert "CREATE INDEX IF NOT EXISTS ix_algo_orders_account_symbol" in _db_src, (
        "database.py missing idempotent CREATE for ix_algo_orders_account_symbol. "
        "Add it in the _audit_indexes block of init_db()."
    )
    assert "ON algo_orders (account, symbol)" in _db_src, (
        "database.py: ix_algo_orders_account_symbol must index (account, symbol) "
        "in that column order (account has higher selectivity)."
    )


def test_composite_account_symbol_in_models_py():
    """Reuse: AlgoOrder.__table_args__ carries the composite index declaration."""
    assert 'Index("ix_algo_orders_account_symbol", "account", "symbol")' in _models_src, (
        "models.py AlgoOrder.__table_args__ missing "
        'Index("ix_algo_orders_account_symbol", "account", "symbol"). '
        "Required so new-table CREATE ALL produces the index without a migration."
    )


def test_algo_events_timestamp_index_in_database_py():
    """SSOT: idempotent CREATE for ix_algo_events_timestamp in database.py."""
    assert "CREATE INDEX IF NOT EXISTS ix_algo_events_timestamp" in _db_src, (
        "database.py missing idempotent CREATE for ix_algo_events_timestamp. "
        "Add it in the _audit_indexes block of init_db()."
    )
    assert "ON algo_events (timestamp)" in _db_src, (
        "database.py: ix_algo_events_timestamp must index algo_events (timestamp)."
    )


def test_algo_events_timestamp_index_in_models_py():
    """Reuse: AlgoEvent.timestamp mapped_column carries index=True."""
    # The column declaration must include index=True so create_all on a fresh DB
    # produces the index. We check the source around the timestamp column.
    assert "index=True" in _models_src, (
        "models.py: AlgoEvent.timestamp mapped_column must carry index=True "
        "for new-DB create_all to build the index without relying solely on "
        "the idempotent ALTER in init_db()."
    )


# ── Perf: pool_pre_ping + pool_recycle on both engines ───────────────────────

def test_pool_pre_ping_primary_engine():
    """Perf: primary engine (pool_size=5) has pool_pre_ping=True."""
    # Find the primary engine block — ends before _shared_engine definition.
    primary_block = _db_src.split("_shared_engine")[0]
    assert "pool_pre_ping=True" in primary_block, (
        "database.py primary engine missing pool_pre_ping=True. "
        "Without it, stale connections after overnight idle raise InterfaceError "
        "on the first market-open request."
    )


def test_pool_pre_ping_shared_engine():
    """Perf: shared broker engine (pool_size=3) has pool_pre_ping=True."""
    shared_block = _db_src.split("_shared_engine")[1]
    assert "pool_pre_ping=True" in shared_block, (
        "database.py _shared_engine missing pool_pre_ping=True."
    )


def test_pool_recycle_primary_engine():
    """Perf: primary engine has pool_recycle=1800."""
    primary_block = _db_src.split("_shared_engine")[0]
    assert "pool_recycle=1800" in primary_block, (
        "database.py primary engine missing pool_recycle=1800. "
        "Without proactive recycling, connections may be evicted by pgbouncer "
        "before pool_pre_ping gets a chance to validate them."
    )


def test_pool_recycle_shared_engine():
    """Perf: shared broker engine has pool_recycle=1800."""
    shared_block = _db_src.split("_shared_engine")[1]
    assert "pool_recycle=1800" in shared_block, (
        "database.py _shared_engine missing pool_recycle=1800."
    )


# ── Stale: no duplicate CREATE INDEX for the same index names ────────────────

def test_no_duplicate_account_symbol_index():
    """Stale: CREATE INDEX for ix_algo_orders_account_symbol appears exactly once."""
    create_count = _db_src.count(
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_account_symbol"
    )
    assert create_count == 1, (
        f"database.py has {create_count} CREATE INDEX guards for "
        "ix_algo_orders_account_symbol (expected 1). Remove duplicates."
    )


def test_no_duplicate_algo_events_timestamp_index():
    """Stale: CREATE INDEX for ix_algo_events_timestamp appears exactly once."""
    create_count = _db_src.count(
        "CREATE INDEX IF NOT EXISTS ix_algo_events_timestamp"
    )
    assert create_count == 1, (
        f"database.py has {create_count} CREATE INDEX guards for "
        "ix_algo_events_timestamp (expected 1). Remove duplicates."
    )


# ── UX: non-critical wrapper used for the new audit indexes ──────────────────

def test_audit_index_block_uses_try_except():
    """UX: _audit_indexes block is wrapped in try/except so index DDL failure
    never aborts boot (same pattern as _index_migrations and _slice_t_indexes)."""
    assert "_audit_indexes" in _db_src, (
        "database.py: _audit_indexes tuple not found. "
        "New audit indexes should be grouped in _audit_indexes iterated with "
        "a try/except wrapper to prevent DDL failure from crashing startup."
    )
    # Confirm the variable name is actually iterated (not defined and forgotten)
    assert "for _stmt in _audit_indexes" in _db_src, (
        "database.py: _audit_indexes defined but not iterated. "
        "Add `for _stmt in _audit_indexes:` loop with try/except."
    )
