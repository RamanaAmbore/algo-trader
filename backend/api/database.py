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
        from backend.api.models import User, Agent, AgentEvent, AlgoOrderEvent, MarketReport, NewsHeadline, GrammarToken, Setting, DailyBook, Watchlist, WatchlistItem, VisitorLog  # noqa: F401 — ensure model registered
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
        # agent_id back-reference — links every AlgoOrder to its originating
        # agent (manual, or an automated agent that fired the action).
        await conn.execute(text(
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS agent_id INTEGER "
            "REFERENCES agents(id)"
        ))
        for stmt in (
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_agent_id ON algo_orders (agent_id)",
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_mode ON algo_orders (mode)",
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_status ON algo_orders (status)",
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_created_at ON algo_orders (created_at)",
            # broker_order_id — chase.py terminal events + postback handlers
            # all look up an AlgoOrder by broker_order_id. Pre-release audit
            # caught this as the worst missing index on the order-hot-path.
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_broker_order_id ON algo_orders (broker_order_id)",
            # algo_order_events — index on order_id for per-order timeline queries.
            # Base.metadata.create_all will create the table; the index is added
            # idempotently here so existing deployments pick it up too.
            "CREATE INDEX IF NOT EXISTS ix_algo_order_events_order_id ON algo_order_events (order_id)",
            # agent_events — alerts / agents / logs / simulator pages all
            # filter on agent_id + sim_mode + timestamp. Pre-release audit
            # caught these three as missing indexes on a growing append-only
            # table; without them every list query was a seq-scan.
            "CREATE INDEX IF NOT EXISTS ix_agent_events_agent_id ON agent_events (agent_id)",
            "CREATE INDEX IF NOT EXISTS ix_agent_events_sim_mode ON agent_events (sim_mode)",
            "CREATE INDEX IF NOT EXISTS ix_agent_events_timestamp ON agent_events (timestamp)",
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
        # Alert hierarchy (May 2026) — severity tier + topic tag drive
        # topic-scoped suppression in run_cycle; digest_window_sec
        # buffers dispatches so a burst lands as one consolidated
        # message instead of N separate notifications. Defaults are
        # set so existing rows behave like before — operator opts into
        # the new behaviour by re-tagging agents to non-'general'
        # topics with critical/high tiers.
        for stmt in (
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS tier VARCHAR(16) "
            "NOT NULL DEFAULT 'medium'",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS topic VARCHAR(64) "
            "NOT NULL DEFAULT 'general'",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS digest_window_sec "
            "INTEGER NOT NULL DEFAULT 30",
            # Phase 21 — debounce ("for N minutes" gate). 0 = fire
            # immediately (backwards-compatible default).
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS debounce_minutes "
            "INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS condition_first_true_at "
            "TIMESTAMP WITH TIME ZONE",
            # Phase 22 — tagging + quiet hours.
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS tags JSONB "
            "NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS blackout_windows JSONB "
            "NOT NULL DEFAULT '[]'::jsonb",
            # 3-part descriptor "condition - alert - action" so operators
            # can scan the agent list and read what each does without
            # expanding the row. Populated for every built-in by
            # seed_agents; nullable so legacy / custom rows don't break.
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS long_name "
            "VARCHAR(256)",
        ):
            await conn.execute(text(stmt))

        # Multi-broker extension (May 2026) — broker_accounts gains four
        # columns so a Dhan / Groww / future-vendor account fits the same
        # schema as Kite. The api_secret_enc / password_enc / totp_token_enc
        # columns already exist (Kite-shaped); the new fields supplement
        # them for brokers that authenticate differently.
        #   client_id        — plaintext, like api_key. Used by Dhan-style
        #                       brokers that key on client_id + access_token.
        #   access_token_enc — Fernet-encrypted access token for Dhan-style
        #                       brokers (long-lived, pasted from dashboard).
        #   priority         — INT, fallback order for PriceBroker
        #                       (lower = tried first). 100 default — every
        #                       existing account ties; operator can pull
        #                       a preferred broker forward by setting it
        #                       to 10, push laggy ones back to 200, etc.
        #   extra_config     — JSONB, free-form per-broker tuning knobs.
        for stmt in (
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS client_id VARCHAR(64)",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS access_token_enc TEXT",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS priority INTEGER "
            "NOT NULL DEFAULT 100",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS extra_config JSONB "
            "NOT NULL DEFAULT '{}'::jsonb",
            # historical_data_enabled — controls per-account eligibility for the
            # /api/options/historical fallback loop. TRUE for all existing rows
            # preserves previous behaviour (every account participates).
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "historical_data_enabled BOOLEAN NOT NULL DEFAULT TRUE",
        ):
            await conn.execute(text(stmt))
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
        # Feature: basket orders + auto profit-target (June 2026).
        # Four new columns on algo_orders; all nullable / defaulted so
        # existing rows remain valid without any data migration.
        for stmt in (
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS target_pct DOUBLE PRECISION",
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS target_abs DOUBLE PRECISION",
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS parent_order_id INTEGER "
            "REFERENCES algo_orders(id)",
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS basket_tag VARCHAR(64)",
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_parent_order_id "
            "ON algo_orders (parent_order_id)",
        ):
            await conn.execute(text(stmt))

        # Watchlist shared-global + item alias migration.
        # is_global=True rows are shared across every user (managed by
        # admin / designated). user_id becomes nullable to host them.
        # alias is the operator-supplied display name on a watchlist
        # item (e.g. "Crude oil" labelling CRUDEOIL26JUNFUT).
        for stmt in (
            "ALTER TABLE watchlists ADD COLUMN IF NOT EXISTS is_global BOOLEAN "
            "NOT NULL DEFAULT FALSE",
            "ALTER TABLE watchlists ALTER COLUMN user_id DROP NOT NULL",
            "ALTER TABLE watchlist_items ADD COLUMN IF NOT EXISTS alias VARCHAR(64)",
        ):
            await conn.execute(text(stmt))
        # Partial unique index — at most one global Pinned row exists.
        # Standard UNIQUE(user_id, name) treats NULL as distinct so
        # multiple (NULL, 'Pinned') would be allowed without this guard.
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_global_pinned "
            "ON watchlists ((1)) WHERE is_global = true"
        ))
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

    # Warm the alert-recipient cache from the users table so the very
    # first alert after a restart already routes to the right addresses.
    from backend.shared.helpers.alert_utils import refresh_alert_recipients
    await refresh_alert_recipients()


async def get_session() -> AsyncSession:
    """Yield an async session (for use in route handlers)."""
    async with async_session() as session:
        yield session
