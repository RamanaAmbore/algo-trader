"""
SQLAlchemy async database setup — PostgreSQL.

Two databases on the same PostgreSQL server:
  - ramboq       (production — deploy_branch == 'main')
  - ramboq_dev   (development — any other branch)

Credentials from secrets.yaml: db_user, db_password.
The deploy_branch in backend_config.yaml determines which DB to use.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.shared.helpers.utils import config, secrets
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def _build_url() -> str:
    """Build the PostgreSQL URL from secrets.yaml + deploy_branch."""
    user     = secrets.get("db_user", "rambo_admin")
    password = secrets.get("db_password", "")
    host     = secrets.get("db_host", "localhost")
    port     = secrets.get("db_port", 5432)

    branch  = config.get("deploy_branch", "dev")
    db_name = "ramboq" if branch == "main" else "ramboq_dev"

    url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"
    logger.info(f"Database: PostgreSQL → {db_name} on {host}:{port}")
    return url


DATABASE_URL = _build_url()

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,   # validate connections on checkout; prevents stale-conn errors after overnight idle
    pool_recycle=1800,    # recycle proactively every 30 min — before pgbouncer / server idle timeout
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _build_shared_url() -> str:
    """Build the PostgreSQL URL that always points to the shared `ramboq`
    database, regardless of which branch/env is running.

    Used exclusively for the `broker_accounts` table so that both the dev
    API and the prod API read and write the same set of broker credentials.
    All other tables (users, algo_orders, agents, ...) stay on the
    branch-local DB via the regular `async_session`.

    NOTE: broker_accounts schema changes (ALTER TABLE ... IF NOT EXISTS)
    still run against the branch-local DB via `init_db()` — those are
    idempotent no-ops on `ramboq_dev` and the real effective migration
    always lands first on `ramboq` (the shared table). Do NOT move
    broker_accounts DDL here; keep it in init_db so the prod DB gets it.

    NOTE: `_reload_connections` (brokers.py) calls `Connections.rebuild_from_db()`
    locally. When `RAMBOQ_USE_CONN_SERVICE=1` that path short-circuits and
    does NOT ping conn_service's `/rebuild` endpoint — conn_service sees the
    updated shared DB only on its next scheduled poll or manual restart.
    That is a pre-existing gap; surfaced here for operator awareness.
    """
    user     = secrets.get("db_user", "rambo_admin")
    password = secrets.get("db_password", "")
    host     = secrets.get("db_host", "localhost")
    port     = secrets.get("db_port", 5432)
    # Always `ramboq` — the shared broker-credentials database.
    url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/ramboq"
    logger.info("Shared broker DB: PostgreSQL -> ramboq on %s:%s", host, port)
    return url


_SHARED_DATABASE_URL = _build_shared_url()

# Separate engine/session for tables shared between dev and prod branches.
# Currently only `broker_accounts` uses this session. Sized conservatively
# (pool_size=3) because the shared DB sees half the write traffic of the
# main engine even in the worst case.
_shared_engine = create_async_engine(
    _SHARED_DATABASE_URL,
    echo=False,
    pool_size=3,
    max_overflow=5,
    pool_pre_ping=True,   # same rationale as primary engine
    pool_recycle=1800,
)
shared_async_session = async_sessionmaker(
    _shared_engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def _migrate_create_tables(conn) -> None:
    """Create all branch-local tables via metadata.create_all (idempotent)."""
    from backend.api.models import (  # noqa: F401 — ensure model registered
        User, Agent, AgentEvent, AlgoOrderEvent, MarketReport, NewsHeadline,
        GrammarToken, Setting, DailyBook, Watchlist, WatchlistItem, VisitorLog,
        CodeMetricsSnapshot, MarketLifecycleEvent, MarketHoliday,
        MarketSpecialSession, BrokerAccount, PerfSnapshot,
    )
    from sqlalchemy import text
    _branch_local_tables = [
        t for t in Base.metadata.sorted_tables
        if t.name != BrokerAccount.__tablename__
    ]
    await conn.run_sync(
        lambda sync_conn: Base.metadata.create_all(
            sync_conn, tables=_branch_local_tables, checkfirst=True,
        )
    )


async def _migrate_algo_orders_base(conn) -> None:
    """Idempotent column + index additions for algo_orders (base slice)."""
    from sqlalchemy import text
    await conn.execute(text(
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS mode VARCHAR(8) "
        "NOT NULL DEFAULT 'live'"
    ))
    await conn.execute(text(
        "UPDATE algo_orders SET mode = 'sim' WHERE mode = 'test'"
    ))
    await conn.execute(text(
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS agent_id INTEGER "
        "REFERENCES agents(id)"
    ))
    await conn.execute(text(
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS strategy_id INTEGER "
        "REFERENCES strategies(id) ON DELETE SET NULL"
    ))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_agent_id ON algo_orders (agent_id)",
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_mode ON algo_orders (mode)",
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_status ON algo_orders (status)",
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_created_at ON algo_orders (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_broker_order_id ON algo_orders (broker_order_id)",
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_strategy_id ON algo_orders (strategy_id)",
        "CREATE INDEX IF NOT EXISTS ix_algo_order_events_order_id ON algo_order_events (order_id)",
        "CREATE INDEX IF NOT EXISTS ix_agent_events_agent_id ON agent_events (agent_id)",
        "CREATE INDEX IF NOT EXISTS ix_agent_events_timestamp ON agent_events (timestamp)",
        "CREATE INDEX IF NOT EXISTS ix_daily_book_date ON daily_book (date)",
        "CREATE INDEX IF NOT EXISTS ix_daily_book_date_account ON daily_book (date, account)",
        "CREATE INDEX IF NOT EXISTS ix_daily_book_date_segment ON daily_book (date, segment)",
    ):
        await conn.execute(text(stmt))


async def _migrate_agent_events_rename(conn) -> None:
    """One-shot rename of agent_events.test_mode → sim_mode (idempotent)."""
    from sqlalchemy import text
    await conn.execute(text(
        "ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS sim_mode BOOLEAN "
        "NOT NULL DEFAULT FALSE"
    ))
    await conn.execute(text("""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_name='agent_events' AND column_name='test_mode') THEN
            UPDATE agent_events SET sim_mode = test_mode WHERE sim_mode = FALSE AND test_mode = TRUE;
            ALTER TABLE agent_events DROP COLUMN test_mode;
          END IF;
        END$$;
    """))


async def _migrate_agents_columns(conn) -> None:
    """Add lifespan, trade_mode, tier/topic, debounce, tags columns to agents."""
    from sqlalchemy import text
    for stmt in (
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS lifespan_type VARCHAR(16) "
        "NOT NULL DEFAULT 'persistent'",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS lifespan_max_fires INTEGER",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS "
        "lifespan_expires_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS trade_mode VARCHAR(8) "
        "NOT NULL DEFAULT 'paper'",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS tier VARCHAR(16) "
        "NOT NULL DEFAULT 'medium'",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS topic VARCHAR(64) "
        "NOT NULL DEFAULT 'general'",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS digest_window_sec "
        "INTEGER NOT NULL DEFAULT 30",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS debounce_minutes "
        "INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS condition_first_true_at "
        "TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS tags JSONB "
        "NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS blackout_windows JSONB "
        "NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS long_name "
        "VARCHAR(256)",
    ):
        await conn.execute(text(stmt))


async def _migrate_users_columns(conn) -> None:
    """Add user-management v2 columns and collapse is_super → role='designated'."""
    from sqlalchemy import text
    for stmt in (
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN "
        "NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER "
        "NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at "
        "TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS terminated_at "
        "TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS receive_alerts BOOLEAN "
        "NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN "
        "NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS assigned_accounts JSONB "
        "NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS assigned_strategies JSONB "
        "NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS compliance_designated BOOLEAN "
        "NOT NULL DEFAULT FALSE",
    ):
        await conn.execute(text(stmt))
    await conn.execute(text("""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_name='users' AND column_name='is_super') THEN
            UPDATE users SET role = 'designated' WHERE is_super = true;
            ALTER TABLE users DROP COLUMN is_super;
          END IF;
        END$$;
    """))
    await conn.execute(text("""
        UPDATE users SET role = 'partner'
        WHERE role IS NULL
           OR role = '';
    """))


async def _migrate_audit_and_request(conn) -> None:
    """Add audit_log.category and algo_orders.request_id columns + indexes."""
    from sqlalchemy import text
    await conn.execute(text(
        "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS category VARCHAR(32)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_audit_log_category "
        "ON audit_log (category)"
    ))
    await conn.execute(text(
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS request_id VARCHAR(36)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_request_id "
        "ON algo_orders (request_id)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_trail_stop "
        "ON algo_orders (mode, status) "
        "WHERE attached_gtts_json IS NOT NULL"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_news_headlines_published_at "
        "ON news_headlines (published_at DESC)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_strategy_lots_open "
        "ON strategy_lots (strategy_id, account, symbol, side, remaining_qty, opened_at)"
    ))
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_investor_events_user_bootstrap "
        "ON investor_events (user_id) WHERE event_type = 'bootstrap'"
    ))


async def _migrate_slice_g(conn) -> None:
    """Slice G — FK ondelete + missing-index fixes (Jun 2026)."""
    from sqlalchemy import text
    # G1 — agent_events.agent_id → ON DELETE CASCADE
    for stmt in (
        "ALTER TABLE agent_events DROP CONSTRAINT IF EXISTS agent_events_agent_id_fkey",
        "ALTER TABLE agent_events ADD CONSTRAINT agent_events_agent_id_fkey "
        "FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE",
    ):
        await conn.execute(text(stmt))
    # G2 — fix uq_watchlist_global_pinned predicate shape
    await conn.execute(text("DROP INDEX IF EXISTS uq_watchlist_global_pinned"))
    await conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_global_pinned "
        "ON watchlists (name) WHERE is_global = true"
    ))
    # G3 — sim_iterations.parent_run_id self-FK → ON DELETE SET NULL
    for stmt in (
        "ALTER TABLE sim_iterations DROP CONSTRAINT IF EXISTS sim_iterations_parent_run_id_fkey",
        "ALTER TABLE sim_iterations ADD CONSTRAINT sim_iterations_parent_run_id_fkey "
        "FOREIGN KEY (parent_run_id) REFERENCES sim_iterations(id) ON DELETE SET NULL",
    ):
        await conn.execute(text(stmt))
    # G4 — daily_book (kind, date) composite
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_daily_book_kind_date "
        "ON daily_book (kind, date)"
    ))
    # G5 — algo_events.algo_order_id FK + index
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_algo_events_algo_order_id "
        "ON algo_events (algo_order_id)"
    ))
    for stmt in (
        "ALTER TABLE algo_events DROP CONSTRAINT IF EXISTS algo_events_algo_order_id_fkey",
        "ALTER TABLE algo_events ADD CONSTRAINT algo_events_algo_order_id_fkey "
        "FOREIGN KEY (algo_order_id) REFERENCES algo_orders(id) ON DELETE SET NULL",
    ):
        await conn.execute(text(stmt))
    # G6 — InvestorEvent / InvestorToken / ResearchThread created_by FKs
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_investor_events_created_by "
        "ON investor_events (created_by)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_investor_tokens_created_by "
        "ON investor_tokens (created_by)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_research_threads_created_by_user_id "
        "ON research_threads (created_by_user_id)"
    ))
    for stmt in (
        "ALTER TABLE investor_events DROP CONSTRAINT IF EXISTS investor_events_created_by_fkey",
        "ALTER TABLE investor_events ADD CONSTRAINT investor_events_created_by_fkey "
        "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE investor_tokens DROP CONSTRAINT IF EXISTS investor_tokens_created_by_fkey",
        "ALTER TABLE investor_tokens ADD CONSTRAINT investor_tokens_created_by_fkey "
        "FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL",
    ):
        await conn.execute(text(stmt))
    # G7 — AlgoOrder.engine / .exchange server_default
    for stmt in (
        "ALTER TABLE algo_orders ALTER COLUMN engine SET DEFAULT 'manual'",
        "ALTER TABLE algo_orders ALTER COLUMN exchange SET DEFAULT 'NFO'",
    ):
        await conn.execute(text(stmt))


async def _migrate_slice_l(conn) -> None:
    """Slice L — defect cleanup (Jun 2026): agent_id FK + user_id FK ondelete chain."""
    from sqlalchemy import text
    # L2 — algo_orders.agent_id FK → ON DELETE SET NULL
    for stmt in (
        "ALTER TABLE algo_orders DROP CONSTRAINT IF EXISTS algo_orders_agent_id_fkey",
        "ALTER TABLE algo_orders ADD CONSTRAINT algo_orders_agent_id_fkey "
        "FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE SET NULL",
    ):
        await conn.execute(text(stmt))
    # L3 — user_id FK ondelete chain for 4 tables
    for stmt in (
        "ALTER TABLE monthly_statements DROP CONSTRAINT IF EXISTS monthly_statements_user_id_fkey",
        "ALTER TABLE monthly_statements ADD CONSTRAINT monthly_statements_user_id_fkey "
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE",
        "ALTER TABLE investor_events DROP CONSTRAINT IF EXISTS investor_events_user_id_fkey",
        "ALTER TABLE investor_events ADD CONSTRAINT investor_events_user_id_fkey "
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT",
        "ALTER TABLE investor_tokens DROP CONSTRAINT IF EXISTS investor_tokens_user_id_fkey",
        "ALTER TABLE investor_tokens ADD CONSTRAINT investor_tokens_user_id_fkey "
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE",
        "ALTER TABLE auth_tokens DROP CONSTRAINT IF EXISTS auth_tokens_user_id_fkey",
        "ALTER TABLE auth_tokens ADD CONSTRAINT auth_tokens_user_id_fkey "
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE",
    ):
        await conn.execute(text(stmt))


async def _migrate_slice_m(conn) -> None:
    """Slice M — perf indexes + basket-order columns (Jun 2026)."""
    from sqlalchemy import text
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_basket_tag "
        "ON algo_orders (basket_tag) WHERE basket_tag IS NOT NULL"
    ))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_account "
        "ON algo_orders (account)",
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_symbol "
        "ON algo_orders (symbol)",
    ):
        await conn.execute(text(stmt))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_agent_events_simmode_agent_ts "
        "ON agent_events (sim_mode, agent_id, timestamp DESC)"
    ))
    for stmt in (
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS target_pct DOUBLE PRECISION",
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS target_abs DOUBLE PRECISION",
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS parent_order_id INTEGER "
        "REFERENCES algo_orders(id)",
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS basket_tag VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_parent_order_id "
        "ON algo_orders (parent_order_id)",
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS template_id INTEGER "
        "REFERENCES order_templates(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_template_id "
        "ON algo_orders (template_id)",
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS attached_gtts_json TEXT",
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS template_overrides_json TEXT",
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS current_limit DOUBLE PRECISION",
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS product "
        "VARCHAR(8) NOT NULL DEFAULT 'NRML'",
        "ALTER TABLE order_templates ADD COLUMN IF NOT EXISTS tp_order_type "
        "VARCHAR(8) NOT NULL DEFAULT 'LIMIT'",
        "ALTER TABLE order_templates ADD COLUMN IF NOT EXISTS tp_scales_json TEXT",
        "ALTER TABLE order_templates ADD COLUMN IF NOT EXISTS sl_trail_pct "
        "NUMERIC(8, 4)",
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS filled_quantity "
        "INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_mode_status "
        "ON algo_orders (mode, status)",
        "CREATE INDEX IF NOT EXISTS ix_daily_book_kind_acct_sym_captured "
        "ON daily_book (kind, account, symbol, captured_at)",
    ):
        await conn.execute(text(stmt))


async def _migrate_watchlist_global(conn) -> None:
    """Watchlist shared-global + item alias migration."""
    from sqlalchemy import text
    for stmt in (
        "ALTER TABLE watchlists ADD COLUMN IF NOT EXISTS is_global BOOLEAN "
        "NOT NULL DEFAULT FALSE",
        "ALTER TABLE watchlists ALTER COLUMN user_id DROP NOT NULL",
        "ALTER TABLE watchlist_items ADD COLUMN IF NOT EXISTS alias VARCHAR(64)",
    ):
        await conn.execute(text(stmt))


async def _migrate_slice_q(conn) -> None:
    """Slice Q — FK ondelete edges (Jun 2026)."""
    from sqlalchemy import text
    # Q5a — algo_orders.parent_order_id → ON DELETE SET NULL
    for stmt in (
        "ALTER TABLE algo_orders DROP CONSTRAINT IF EXISTS algo_orders_parent_order_id_fkey",
        "ALTER TABLE algo_orders ADD CONSTRAINT algo_orders_parent_order_id_fkey "
        "FOREIGN KEY (parent_order_id) REFERENCES algo_orders(id) ON DELETE SET NULL",
    ):
        await conn.execute(text(stmt))
    # Q5b — algo_order_events.order_id → ON DELETE CASCADE
    for stmt in (
        "ALTER TABLE algo_order_events DROP CONSTRAINT IF EXISTS algo_order_events_order_id_fkey",
        "ALTER TABLE algo_order_events ADD CONSTRAINT algo_order_events_order_id_fkey "
        "FOREIGN KEY (order_id) REFERENCES algo_orders(id) ON DELETE CASCADE",
    ):
        await conn.execute(text(stmt))


async def _migrate_slice_s6_watchlist_fk(conn) -> None:
    """S6 — watchlists.user_id FK → ON DELETE SET NULL (wrapped, ownership may vary)."""
    from sqlalchemy import text
    try:
        for stmt in (
            "ALTER TABLE watchlists DROP CONSTRAINT IF EXISTS watchlists_user_id_fkey",
            "ALTER TABLE watchlists ADD CONSTRAINT watchlists_user_id_fkey "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL",
        ):
            await conn.execute(text(stmt))
    except Exception as _wl_err:
        logger.warning(
            "init_db: watchlists FK migration skipped — %s", _wl_err
        )


async def _migrate_slice_r6_indexes(conn) -> None:
    """R6a/b/c — index migrations wrapped for ownership-drift resilience."""
    from sqlalchemy import text
    _index_migrations = (
        "DROP INDEX IF EXISTS ix_agent_events_sim_mode",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_impersonation_active "
        "ON impersonation_events (actor_username, target_username) "
        "WHERE ended_at IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_research_threads_updated_at "
        "ON research_threads (updated_at DESC)",
    )
    for _stmt in _index_migrations:
        try:
            await conn.execute(text(_stmt))
        except Exception as _idx_err:
            logger.warning(
                "init_db: non-critical index migration skipped — %s "
                "(stmt=%s)", _idx_err, _stmt[:80],
            )


async def _migrate_slice_t(conn) -> None:
    """Slice T — index additions + drops (Jun 2026)."""
    from sqlalchemy import text
    _slice_t_indexes = (
        "CREATE INDEX IF NOT EXISTS ix_algo_order_events_order_id_ts "
        "ON algo_order_events (order_id, ts)",
        "CREATE INDEX IF NOT EXISTS ix_admin_email_events_created_at "
        "ON admin_email_events (created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_monthly_statements_period "
        "ON monthly_statements (period_year, period_month)",
        "DROP INDEX IF EXISTS ix_strategy_snapshots_date",
        "DROP INDEX IF EXISTS ix_audit_log_path",
    )
    for _stmt in _slice_t_indexes:
        try:
            await conn.execute(text(_stmt))
        except Exception as _t_err:
            logger.warning(
                "init_db: slice-T index migration skipped — %s "
                "(stmt=%s)", _t_err, _stmt[:80],
            )


async def _migrate_s7_trgm_index(conn) -> None:
    """S7 — replace btree audit_log.action index with pg_trgm GIN (graceful)."""
    from sqlalchemy import text
    try:
        await conn.execute(text("DROP INDEX IF EXISTS ix_audit_log_action"))
    except Exception as _trgm_err:
        logger.warning("init_db: could not drop ix_audit_log_action — %s", _trgm_err)
    try:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_audit_log_action_trgm "
            "ON audit_log USING gin (action gin_trgm_ops)"
        ))
    except Exception as _trgm_err:
        logger.warning(
            "init_db: pg_trgm GIN index for audit_log.action not created "
            "(extension may require superuser) — %s", _trgm_err,
        )


async def _migrate_persistence_tables(conn) -> None:
    """Stage 1-4 OHLCV / instruments / holidays / intraday persistence tables."""
    try:
        from backend.api.persistence.migrations import create_ohlcv_daily_table
        await create_ohlcv_daily_table(conn)
    except Exception as _ohlcv_err:
        logger.warning("init_db: ohlcv_daily migration skipped — %s", _ohlcv_err)
    try:
        from backend.api.persistence.migrations import create_instruments_snapshot_table
        await create_instruments_snapshot_table(conn)
    except Exception as _instr_err:
        logger.warning("init_db: instruments_snapshot migration skipped — %s", _instr_err)
    try:
        from backend.api.persistence.migrations import create_holidays_snapshot_table
        await create_holidays_snapshot_table(conn)
    except Exception as _hol_err:
        logger.warning("init_db: holidays_snapshot migration skipped — %s", _hol_err)
    try:
        from backend.api.persistence.migrations import create_intraday_bars_table
        await create_intraday_bars_table(conn)
    except Exception as _intraday_err:
        logger.warning("init_db: intraday_bars migration skipped — %s", _intraday_err)


async def _migrate_audit_stability_indexes(conn) -> None:
    """Audit a4f91d + a87bc7 — DB stability indexes (Jul 2026)."""
    from sqlalchemy import text
    _audit_indexes = (
        "CREATE INDEX IF NOT EXISTS ix_algo_orders_account_symbol "
        "ON algo_orders (account, symbol)",
        "CREATE INDEX IF NOT EXISTS ix_algo_events_timestamp "
        "ON algo_events (timestamp)",
    )
    for _stmt in _audit_indexes:
        try:
            await conn.execute(text(_stmt))
        except Exception as _ai_err:
            logger.warning(
                "init_db: audit index migration skipped — %s "
                "(stmt=%s)", _ai_err, _stmt[:80],
            )


async def _migrate_code_metrics_perf_snapshots(conn) -> None:
    """code_metrics_snapshots + perf_snapshots index DDLs (idempotent)."""
    from sqlalchemy import text
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_code_metrics_captured_at "
        "ON code_metrics_snapshots (captured_at DESC)"
    ))
    await conn.execute(text(
        "ALTER TABLE code_metrics_snapshots "
        "ADD COLUMN IF NOT EXISTS test_response_times JSONB"
    ))
    for _stmt in (
        "CREATE INDEX IF NOT EXISTS ix_perf_snapshots_page_captured "
        "ON perf_snapshots (page_or_route, captured_at)",
        "CREATE INDEX IF NOT EXISTS ix_perf_snapshots_captured_at "
        "ON perf_snapshots (captured_at DESC)",
    ):
        try:
            await conn.execute(text(_stmt))
        except Exception as _ps_err:
            logger.warning(
                "init_db: perf_snapshots index migration skipped — %s "
                "(stmt=%s)", _ps_err, _stmt[:80],
            )


async def _migrate_daily_book_previous_close(conn) -> None:
    """Add previous_close column to daily_book (idempotent).

    Frozen first-write per (date, account, kind, symbol): captures
    Kite's close_price at the first snapshot of each trading day —
    the prior-session official settlement. COALESCE in the UPSERT
    ensures subsequent intraday writes never overwrite a non-NULL value.
    Used by _positions_snapshot() to supply a correct close_price during
    closed-hours reads instead of LTP.
    """
    from sqlalchemy import text
    await conn.execute(text(
        "ALTER TABLE daily_book "
        "ADD COLUMN IF NOT EXISTS previous_close DOUBLE PRECISION"
    ))


async def _migrate_algo_orders_chase_timing(conn) -> None:
    """Add chase timing + interval columns to algo_orders (idempotent).

    last_attempt_at / next_attempt_at (Unix epoch seconds, DOUBLE
    PRECISION) — written by the chase loop on every cancel-and-replace
    so the chase panel can show a live countdown to the next re-quote.

    interval_seconds (INTEGER) — persisted chase cadence; eliminates
    the need for the UI to re-derive from /admin/settings per-row.

    All three columns are nullable — existing rows keep NULL, which
    the API serialises as None and the frontend treats as "unknown".
    """
    from sqlalchemy import text
    for stmt in (
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS "
        "last_attempt_at DOUBLE PRECISION",
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS "
        "next_attempt_at DOUBLE PRECISION",
        "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS "
        "interval_seconds INTEGER",
    ):
        await conn.execute(text(stmt))


async def init_db() -> None:
    """Create all tables (idempotent).

    `broker_accounts` is intentionally EXCLUDED from create_all on the
    branch-local engine — the table lives in the shared `ramboq` DB
    (see `_shared_engine`) and reads/writes route there via
    `shared_async_session`. Excluding it from the branch-local schema
    prevents re-provisioning dev from recreating an orphan empty table
    that no code touches. The table itself is created on `_shared_engine`
    in a separate call below.
    """
    async with engine.begin() as conn:
        await _migrate_create_tables(conn)
        await _migrate_algo_orders_base(conn)
        await _migrate_agent_events_rename(conn)
        await _migrate_agents_columns(conn)
        await _migrate_users_columns(conn)
        await _migrate_audit_and_request(conn)
        await _migrate_slice_g(conn)
        await _migrate_slice_l(conn)
        await _migrate_slice_m(conn)
        await _migrate_watchlist_global(conn)
        await _migrate_slice_q(conn)
        await _migrate_slice_s6_watchlist_fk(conn)
        await _migrate_slice_r6_indexes(conn)
        await _migrate_slice_t(conn)
        await _migrate_s7_trgm_index(conn)
        await _migrate_persistence_tables(conn)
        await _migrate_audit_stability_indexes(conn)
        await _migrate_code_metrics_perf_snapshots(conn)
        await _migrate_daily_book_previous_close(conn)
        await _migrate_algo_orders_chase_timing(conn)
    logger.info("Database: tables verified")

    # broker_accounts schema lives on the SHARED engine (ramboq DB) — always
    # points at prod's broker_accounts regardless of deploy_branch. Run
    # create_table + idempotent ALTERs here so both dev and prod agree on
    # the schema; on prod this is a no-op (columns already exist), on dev
    # it never touches the branch-local DB (which no longer has the table).
    await _ensure_shared_broker_schema()

    # Seed grammar tokens (condition / notify / action catalog) BEFORE agents
    # so any agent referencing a token can validate against the catalog.
    from backend.api.algo.grammar import seed_grammar_tokens
    await seed_grammar_tokens()

    # Load the grammar dispatch table — resolves every is_active token's
    # resolver path into an importable callable. Called again whenever the
    # admin edits a token (future UI endpoint).
    from backend.api.algo.grammar_registry import REGISTRY
    await REGISTRY.reload()

    # Seed agent templates (reusable notify/condition sub-trees). Has
    # to land BEFORE seed_agents so any builtin agent referencing a
    # template via $ref can resolve it on first dispatch.
    from backend.api.algo.template_registry import seed_agent_templates
    await seed_agent_templates()

    # Seed built-in agents
    from backend.api.algo.agent_engine import seed_agents
    await seed_agents()

    # Seed DB-backed settings (populates `settings` table from
    # backend/shared/helpers/settings.py seed list; preserves operator
    # overrides on subsequent boots).
    from backend.shared.helpers.settings import seed_settings
    await seed_settings()

    # Seed system OrderTemplate rows (Default Bull / Default Bear /
    # Default Short Vol / None). Refreshes mutable metadata on
    # existing rows; preserves the operator's tuned numeric values.
    from backend.api.algo.templates_seed import seed_templates
    await seed_templates()

    # Seed the single shared 'Pinned' watchlist + migrate any per-user
    # Pinned/Default rows into it. Idempotent.
    from backend.api.routes.watchlist import seed_global_pinned
    await seed_global_pinned()

    # Seed illustrative special-session rows (idempotent — skips if already
    # present). Operator replaces/adds rows via DB when the exchange
    # publishes its actual Muhurat schedule.
    await seed_special_sessions()

    # Warm the alert-recipient cache from the users table so the very
    # first alert after a restart already routes to the right addresses.
    from backend.shared.helpers.alert_utils import refresh_alert_recipients
    await refresh_alert_recipients()


async def seed_special_sessions() -> None:
    """Insert illustrative special-session rows at boot (idempotent).

    These are example rows that demonstrate the override mechanism.  The
    operator should replace / extend them before the relevant dates arrive
    (NSE/MCX publish Muhurat timings ~2 weeks in advance).

    Primary key is ``(exchange, date, start_time)`` so the SELECT-before-
    INSERT guard is exact and concurrent boots cannot duplicate rows.
    """
    from datetime import date as _date, time as _time
    from sqlalchemy import select
    from backend.api.models import MarketSpecialSession

    _SEED_ROWS: list[dict] = [
        # NSE Diwali Muhurat 2026 — operator updates annually.
        {
            "exchange":   "NSE",
            "date":       _date(2026, 11, 1),
            "start_time": _time(18, 0),
            "end_time":   _time(19, 0),
            "reason":     "Diwali Muhurat 2026",
        },
        # MCX Diwali Muhurat 2026 — same window, different exchange.
        {
            "exchange":   "MCX",
            "date":       _date(2026, 11, 1),
            "start_time": _time(18, 0),
            "end_time":   _time(19, 0),
            "reason":     "Diwali Muhurat 2026 (MCX)",
        },
    ]

    async with async_session() as session:
        for row in _SEED_ROWS:
            exists_q = await session.execute(
                select(MarketSpecialSession).where(
                    MarketSpecialSession.exchange   == row["exchange"],
                    MarketSpecialSession.date       == row["date"],
                    MarketSpecialSession.start_time == row["start_time"],
                )
            )
            if exists_q.first() is None:
                session.add(MarketSpecialSession(**row))
        await session.commit()
    logger.info("seed_special_sessions: seed check complete")


def _display_order_for_account(acct: str, broker_id: str, kite_n: int, groww_n: int) -> int:
    """Return the canonical display_order for an account."""
    bid = (broker_id or "").lower()
    if acct == "DH6847":
        return 999
    if acct == "DH3747":
        return 100
    if "kite" in bid or "zerodha" in bid:
        return kite_n * 10
    if "groww" in bid:
        return 200 + (groww_n - 1) * 10
    return 500


async def _seed_circuit_breaker_for_dh6847() -> None:
    """One-shot migration: set circuit_breaker_enabled=TRUE for DH6847."""
    from sqlalchemy import text
    async with _shared_engine.begin() as conn:
        already_enabled = await conn.scalar(
            text(
                "SELECT circuit_breaker_enabled FROM broker_accounts "
                "WHERE account = 'DH6847' LIMIT 1"
            )
        )
        if already_enabled is False:
            await conn.execute(
                text(
                    "UPDATE broker_accounts "
                    "SET circuit_breaker_enabled = TRUE "
                    "WHERE account = 'DH6847'"
                )
            )
            logger.info(
                "_ensure_shared_broker_schema: circuit_breaker_enabled seeded for DH6847"
            )


def _build_display_order_map(rows: list) -> list[tuple[str, int]]:
    """Build (account, display_order) pairs for a list of (account, broker_id) rows."""
    kite_n = groww_n = 0
    order_map: list[tuple[str, int]] = []
    for acct, broker_id in rows:
        bid = (broker_id or "").lower()
        if "kite" in bid or "zerodha" in bid:
            kite_n += 1
        elif "groww" in bid:
            groww_n += 1
        order = _display_order_for_account(acct, broker_id, kite_n, groww_n)
        order_map.append((acct, order))
    return order_map


async def _seed_display_order() -> None:
    """One-shot migration: assign canonical display_order to all broker accounts."""
    from sqlalchemy import text
    async with _shared_engine.begin() as conn:
        do_already = await conn.scalar(
            text("SELECT 1 FROM broker_accounts WHERE display_order != 500 LIMIT 1")
        )
        if do_already:
            return
        rows = (await conn.execute(
            text("SELECT account, broker_id FROM broker_accounts ORDER BY account")
        )).fetchall()
        order_map = _build_display_order_map(rows)
        if order_map:
            cases = " ".join(f"WHEN :a{i} THEN :o{i}" for i in range(len(order_map)))
            params: dict = {}
            for i, (a, o) in enumerate(order_map):
                params[f"a{i}"] = a
                params[f"o{i}"] = o
            params["accounts"] = tuple(a for a, _ in order_map)
            await conn.execute(
                text(
                    f"UPDATE broker_accounts "
                    f"SET display_order = CASE account {cases} END "
                    f"WHERE account IN :accounts"
                ),
                params,
            )
        logger.info(
            "_ensure_shared_broker_schema: display_order seeded for %d accounts",
            len(rows),
        )


async def _ensure_shared_broker_schema() -> None:
    """Create + migrate broker_accounts on the shared engine (ramboq DB).

    Runs after `init_db()`'s branch-local block. On prod (which already
    has broker_accounts + all columns) every statement is a no-op via
    IF NOT EXISTS. On dev the branch-local DB doesn't have the table
    at all (dropped Jul 2026); this block writes only to the shared
    ramboq DB.

    Idempotent — safe to run on every boot.
    """
    from sqlalchemy import text
    from backend.api.models import BrokerAccount  # noqa: F401
    async with _shared_engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[BrokerAccount.__table__],
                checkfirst=True,
            )
        )
        for stmt in (
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS client_id VARCHAR(64)",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS access_token_enc TEXT",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS priority INTEGER "
            "NOT NULL DEFAULT 100",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS extra_config JSONB "
            "NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "historical_data_enabled BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "poll_priority VARCHAR(8) NOT NULL DEFAULT 'hot'",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "auto_downgrade_enabled BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "auto_downgraded_at TIMESTAMPTZ",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "auto_downgrade_reason TEXT",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "circuit_breaker_enabled BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "display_order INTEGER NOT NULL DEFAULT 500",
        ):
            await conn.execute(text(stmt))

    # broker_connection_events lives in the shared DB (ramboq) — same as broker_accounts
    try:
        from backend.api.persistence.migrations import create_broker_connection_events_table
        async with _shared_engine.begin() as _bce_conn:
            await create_broker_connection_events_table(_bce_conn)
    except Exception as _bce_err:
        logger.warning("shared schema: broker_connection_events migration skipped — %s", _bce_err)

    await _seed_circuit_breaker_for_dh6847()
    await _seed_display_order()
    logger.info("Shared broker schema verified on ramboq")


async def get_session() -> AsyncSession:
    """Yield an async session (for use in route handlers)."""
    async with async_session() as session:
        yield session
