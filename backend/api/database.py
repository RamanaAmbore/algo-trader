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

engine = create_async_engine(DATABASE_URL, echo=False, pool_size=5, max_overflow=10)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    """Create all tables (idempotent)."""
    async with engine.begin() as conn:
        from backend.api.models import User, Agent, AgentEvent, AlgoOrderEvent, MarketReport, NewsHeadline, GrammarToken, Setting, DailyBook, Watchlist, WatchlistItem  # noqa: F401 — ensure model registered
        await conn.run_sync(Base.metadata.create_all)

        # Idempotent column additions for tables that pre-date the column.
        # PostgreSQL ADD COLUMN IF NOT EXISTS is supported since 9.6 and is a
        # cheap no-op when the column already exists.
        from sqlalchemy import text
        await conn.execute(text(
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS mode VARCHAR(8) "
            "NOT NULL DEFAULT 'live'"
        ))
        # agent_events carries the former test_mode flag. Ensure the column
        # exists under its new name (sim_mode) whether the DB was created
        # before or after the rename.
        await conn.execute(text(
            "ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS sim_mode BOOLEAN "
            "NOT NULL DEFAULT FALSE"
        ))
        # One-shot rename: old deploys carried test_mode. If the legacy column
        # is still present, copy values into sim_mode then drop it.
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
        # AlgoOrder.mode values: 'test' was the paper-trade sentinel; rename
        # every existing row to 'sim' so the simulator UI reads them under
        # the new vocabulary.
        await conn.execute(text(
            "UPDATE algo_orders SET mode = 'sim' WHERE mode = 'test'"
        ))
        # Indexes on AlgoOrder hot-path columns. Base.metadata.create_all only
        # adds indexes when CREATING the table — for tables that pre-date the
        # `index=True` flag on the model, we add them explicitly here.
        # CREATE INDEX IF NOT EXISTS is idempotent; cheap no-op once present.
        for stmt in (
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_mode ON algo_orders (mode)",
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_status ON algo_orders (status)",
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_created_at ON algo_orders (created_at)",
            # algo_order_events — index on order_id for per-order timeline queries.
            # Base.metadata.create_all will create the table; the index is added
            # idempotently here so existing deployments pick it up too.
            "CREATE INDEX IF NOT EXISTS ix_algo_order_events_order_id ON algo_order_events (order_id)",
            # daily_book — composite indexes for date-range P&L queries on
            # existing tables (Base.metadata.create_all covers brand-new tables;
            # these are safe no-ops when the table was just created).
            "CREATE INDEX IF NOT EXISTS ix_daily_book_date ON daily_book (date)",
            "CREATE INDEX IF NOT EXISTS ix_daily_book_date_account ON daily_book (date, account)",
            "CREATE INDEX IF NOT EXISTS ix_daily_book_date_segment ON daily_book (date, segment)",
        ):
            await conn.execute(text(stmt))
        # Agent lifespan columns — added after the agents table existed in
        # production. Default 'persistent' on existing rows preserves
        # current behaviour; max_fires + expires_at remain NULL until the
        # operator (or an algo spawning the agent) sets them.
        await conn.execute(text(
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS lifespan_type VARCHAR(16) "
            "NOT NULL DEFAULT 'persistent'"
        ))
        await conn.execute(text(
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS lifespan_max_fires INTEGER"
        ))
        await conn.execute(text(
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS "
            "lifespan_expires_at TIMESTAMP WITH TIME ZONE"
        ))
        # Per-agent trade routing — defaults to 'paper' so existing rows
        # stay safe after the column add. The /agents UI exposes a
        # PAPER / LIVE toggle that flips this per-row.
        await conn.execute(text(
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS trade_mode VARCHAR(8) "
            "NOT NULL DEFAULT 'paper'"
        ))
        # User-management v2 — super-admin role, email verification,
        # token versioning for force-logout, suspend / terminate stamps.
        # Columns added with explicit defaults so existing rows backfill
        # safely without the migration breaking.
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
        ):
            await conn.execute(text(stmt))
        # Collapse `is_super` into role='designated' and drop the column.
        # Idempotent: the IF EXISTS guards skip on subsequent boots.
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
    logger.info("Database: tables verified")

    # Seed grammar tokens (condition / notify / action catalog) BEFORE agents
    # so any agent referencing a token can validate against the catalog.
    from backend.api.algo.grammar import seed_grammar_tokens
    await seed_grammar_tokens()

    # Load the grammar dispatch table — resolves every is_active token's
    # resolver path into an importable callable. Called again whenever the
    # admin edits a token (future UI endpoint).
    from backend.api.algo.grammar_registry import REGISTRY
    await REGISTRY.reload()

    # Seed built-in agents
    from backend.api.algo.agent_engine import seed_agents
    await seed_agents()

    # Seed DB-backed settings (populates `settings` table from
    # backend/shared/helpers/settings.py seed list; preserves operator
    # overrides on subsequent boots).
    from backend.shared.helpers.settings import seed_settings
    await seed_settings()

    # Warm the alert-recipient cache from the users table so the very
    # first alert after a restart already routes to the right addresses.
    from backend.shared.helpers.alert_utils import refresh_alert_recipients
    await refresh_alert_recipients()


async def get_session() -> AsyncSession:
    """Yield an async session (for use in route handlers)."""
    async with async_session() as session:
        yield session
