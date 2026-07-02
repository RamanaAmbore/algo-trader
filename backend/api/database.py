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
    _SHARED_DATABASE_URL, echo=False, pool_size=3, max_overflow=5
)
shared_async_session = async_sessionmaker(
    _shared_engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


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
        from backend.api.models import User, Agent, AgentEvent, AlgoOrderEvent, MarketReport, NewsHeadline, GrammarToken, Setting, DailyBook, Watchlist, WatchlistItem, VisitorLog, CodeMetricsSnapshot, MarketLifecycleEvent, BrokerAccount  # noqa: F401 — ensure model registered
        _branch_local_tables = [
            t for t in Base.metadata.sorted_tables
            if t.name != BrokerAccount.__tablename__
        ]
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=_branch_local_tables, checkfirst=True,
            )
        )

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
        # Slice 6 — strategy_id back-reference for attribution. Optional
        # (nullable + ON DELETE SET NULL) so legacy rows pre-dating
        # this column keep validating. The order-place path captures
        # the operator's strategy choice when present; the chase +
        # agent-fire paths inherit from the parent agent's strategy.
        await conn.execute(text(
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS strategy_id INTEGER "
            "REFERENCES strategies(id) ON DELETE SET NULL"
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
            # Slice 6 — strategy_id index for per-strategy P&L
            # queries. Hot path: dashboard's strategy-level rollup
            # groups by strategy_id; without the index a full scan
            # over algo_orders runs on every dashboard refresh.
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_strategy_id ON algo_orders (strategy_id)",
            # algo_order_events — index on order_id for per-order timeline queries.
            # Base.metadata.create_all will create the table; the index is added
            # idempotently here so existing deployments pick it up too.
            "CREATE INDEX IF NOT EXISTS ix_algo_order_events_order_id ON algo_order_events (order_id)",
            # agent_events — alerts / agents / logs / simulator pages all
            # filter on agent_id + sim_mode + timestamp. Pre-release audit
            # caught these three as missing indexes on a growing append-only
            # table; without them every list query was a seq-scan.
            "CREATE INDEX IF NOT EXISTS ix_agent_events_agent_id ON agent_events (agent_id)",
            # ix_agent_events_sim_mode removed — covered by the
            # ix_agent_events_simmode_agent_ts composite (slice M).
            # The CREATE was retained here while the slice-R DROP block
            # at ~line 652 dropped it every boot (wasted WAL). Removed
            # the CREATE; the DROP IF EXISTS below remains for legacy installs.
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

        # broker_accounts DDL moved to `_ensure_shared_broker_schema()` below.
        # Table lives in the shared `ramboq` DB (see `_shared_engine`); running
        # ALTER TABLE against the branch-local engine would fail on dev (table
        # doesn't exist there — dropped Jul 2026).
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
            # Slice 5 — per-user horizontal scoping. JSONB lists keyed
            # by broker-account code (assigned_accounts) and strategy
            # id (assigned_strategies). Empty list = no explicit scope;
            # the role's scope policy decides whether that means ALL or
            # NONE (designated/risk/admin/partner/demo → all, trader → none).
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS assigned_accounts JSONB "
            "NOT NULL DEFAULT '[]'::jsonb",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS assigned_strategies JSONB "
            "NOT NULL DEFAULT '[]'::jsonb",
            # Compliance officer designation — orthogonal to role. SEBI
            # Cat-III requires a designated compliance officer; the
            # in-app flag tracks the legal title without bloating the
            # role enum.
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS compliance_designated BOOLEAN "
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
        # Canonical 5-role surface is designated / trader / risk / admin /
        # partner. NULL / empty roles collapse to 'partner' — the safest
        # default (read-only LP view), same contract as `normalise_role()`.
        # 'designated' is preserved (it's the firm-owner tier).
        # Idempotent: subsequent boots find no rows to update and no-op.
        await conn.execute(text("""
            UPDATE users SET role = 'partner'
            WHERE role IS NULL
               OR role = '';
        """))
        # audit_log.category — coarse tag for filtering (order.fill /
        # agent.action / system.* etc). Added Jun 2026; existing rows
        # land with NULL which the UI surfaces as 'http' (the
        # middleware default).
        await conn.execute(text(
            "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS category VARCHAR(32)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_audit_log_category "
            "ON audit_log (category)"
        ))
        # algo_orders.request_id — drill-through to /admin/audit. New
        # rows from POST /api/orders/ticket get the middleware's
        # request_id stamped at insert time; existing rows stay NULL.
        await conn.execute(text(
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS request_id VARCHAR(36)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_request_id "
            "ON algo_orders (request_id)"
        ))
        # Audit-driven indexes (Jun 2026). All three are CREATE INDEX
        # IF NOT EXISTS guards so re-running is a no-op.
        #
        # 1. algo_orders partial index for the trail-stop + OCO hot
        #    paths. `_task_trail_stop` (every 30s) and
        #    `_task_oco_pair_watcher` (every 15s) both query
        #    `WHERE mode='live' AND status='FILLED'
        #     AND attached_gtts_json IS NOT NULL`. The existing
        #    ix_algo_orders_mode_status covers the first two predicates
        #    but Postgres still filters NULL in-memory over all FILLED
        #    rows as the table grows.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_trail_stop "
            "ON algo_orders (mode, status) "
            "WHERE attached_gtts_json IS NOT NULL"
        ))
        # 2. news_headlines.published_at — GET /api/news fires
        #    `ORDER BY published_at DESC` on every cache miss. PK is
        #    `link` (TEXT) so without this index the sort is a seq-
        #    scan + sort. Descending index matches the query.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_news_headlines_published_at "
            "ON news_headlines (published_at DESC)"
        ))
        # 3. strategy_lots composite for close_lot_fifo. Index is
        #    declared in the model's __table_args__ but create_all
        #    skips index updates on tables that pre-exist a slice
        #    deploy. Idempotent CREATE here brings dev/prod into
        #    sync.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_strategy_lots_open "
            "ON strategy_lots (strategy_id, account, symbol, side, remaining_qty, opened_at)"
        ))
        # 4. investor_events partial unique index — guarantee at
        #    most one bootstrap event per LP. Closes the
        #    check-then-insert race in ensure_user_bootstrap that
        #    would otherwise double-count total_units fund-wide.
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_investor_events_user_bootstrap "
            "ON investor_events (user_id) WHERE event_type = 'bootstrap'"
        ))
        # ── Slice G — data-layer hardening (Jun 2026) ────────────────────
        # FK ondelete + missing-index fixes flagged by #audit round 2.
        # All ALTERs are guarded; reruns are idempotent.
        #
        # G1 — agent_events.agent_id → ON DELETE CASCADE. Pre-fix the
        # default NO ACTION blocked agent deletes at the DB level (or
        # required manual cleanup in agent_engine.py). CASCADE matches
        # the operator intent: deleting an agent retires its history.
        for stmt in (
            "ALTER TABLE agent_events DROP CONSTRAINT IF EXISTS agent_events_agent_id_fkey",
            "ALTER TABLE agent_events ADD CONSTRAINT agent_events_agent_id_fkey "
            "FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE",
        ):
            await conn.execute(text(stmt))
        # G2 — replace the broken uq_watchlist_global_pinned index
        # whose predicate `((1)) WHERE is_global = true` allowed only
        # ONE global row in the whole table regardless of name. The
        # correct shape is `(name) WHERE is_global = true` so each
        # global name can have at most one row but multiple names can
        # coexist (Markets + Default + a future Sector list).
        await conn.execute(text(
            "DROP INDEX IF EXISTS uq_watchlist_global_pinned"
        ))
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_watchlist_global_pinned "
            "ON watchlists (name) WHERE is_global = true"
        ))
        # G3 — sim_iterations.parent_run_id self-FK. Pre-fix it was a
        # plain int; deleting an iteration silently left children
        # pointing at a dangling id. SET NULL: if the parent run is
        # purged, children become standalone rows.
        for stmt in (
            "ALTER TABLE sim_iterations DROP CONSTRAINT IF EXISTS sim_iterations_parent_run_id_fkey",
            "ALTER TABLE sim_iterations ADD CONSTRAINT sim_iterations_parent_run_id_fkey "
            "FOREIGN KEY (parent_run_id) REFERENCES sim_iterations(id) ON DELETE SET NULL",
        ):
            await conn.execute(text(stmt))
        # G4 — daily_book (kind, date) composite index. The Orders +
        # Trades + Funds endpoints in /admin/history all query
        # `WHERE kind=? AND date BETWEEN ? AND ?`. Existing single-
        # column (date) index uses date scan + memory filter; this
        # composite leads with kind so Postgres drops straight to the
        # right partition.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_daily_book_kind_date "
            "ON daily_book (kind, date)"
        ))
        # G5 — algo_events.algo_order_id FK + index. Pre-fix the FK
        # had NO ACTION (would block algo_orders deletes) and the
        # column lacked an index entirely (FK lookups + future
        # readers would seq-scan).
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
        # G6 — InvestorEvent / InvestorToken / ResearchThread
        # created_by FKs. Pre-fix all three had default NO ACTION +
        # no index. Deleting an admin user would block any of these
        # at the DB level; FK lookups + admin-listing queries would
        # seq-scan.
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
        # G7 — AlgoOrder.engine / .exchange server_default. Pre-fix
        # the columns carried only Python `default=`, so a raw INSERT
        # bypassing the ORM would NULL them. Aligns the column's
        # database-side default with the ORM default so any insert
        # path is safe.
        for stmt in (
            "ALTER TABLE algo_orders ALTER COLUMN engine SET DEFAULT 'manual'",
            "ALTER TABLE algo_orders ALTER COLUMN exchange SET DEFAULT 'NFO'",
        ):
            await conn.execute(text(stmt))
        # ── Slice L — defect cleanup (Jun 2026) ──────────────────────────
        # L2 — algo_orders.agent_id FK → ON DELETE SET NULL. Slice G
        # fixed agent_events but missed this one; agent deletes were
        # blocked at the DB level for accounts that ever held an
        # agent-originated order. SET NULL preserves the order history
        # (we don't want to lose the row, just lose the back-reference).
        for stmt in (
            "ALTER TABLE algo_orders DROP CONSTRAINT IF EXISTS algo_orders_agent_id_fkey",
            "ALTER TABLE algo_orders ADD CONSTRAINT algo_orders_agent_id_fkey "
            "FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE SET NULL",
        ):
            await conn.execute(text(stmt))
        # L3 — user_id FK ondelete chain. Four NOT NULL user_id FKs
        # had no ondelete and so blocked every user delete at the DB
        # level. Policies:
        #   monthly_statements → CASCADE (regeneratable from events)
        #   investor_events    → RESTRICT (LP capital ledger — explicit
        #                        operator action required)
        #   investor_tokens    → CASCADE (URL credentials — die with
        #                        the user)
        #   auth_tokens        → CASCADE (ephemeral verify/reset rows)
        for stmt in (
            # monthly_statements
            "ALTER TABLE monthly_statements DROP CONSTRAINT IF EXISTS monthly_statements_user_id_fkey",
            "ALTER TABLE monthly_statements ADD CONSTRAINT monthly_statements_user_id_fkey "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE",
            # investor_events (the actual FK column is `user_id`, not
            # `created_by` — that one was fixed in slice G6).
            "ALTER TABLE investor_events DROP CONSTRAINT IF EXISTS investor_events_user_id_fkey",
            "ALTER TABLE investor_events ADD CONSTRAINT investor_events_user_id_fkey "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT",
            # investor_tokens (same — the `user_id` column, not
            # `created_by`).
            "ALTER TABLE investor_tokens DROP CONSTRAINT IF EXISTS investor_tokens_user_id_fkey",
            "ALTER TABLE investor_tokens ADD CONSTRAINT investor_tokens_user_id_fkey "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE",
            # auth_tokens
            "ALTER TABLE auth_tokens DROP CONSTRAINT IF EXISTS auth_tokens_user_id_fkey",
            "ALTER TABLE auth_tokens ADD CONSTRAINT auth_tokens_user_id_fkey "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE",
        ):
            await conn.execute(text(stmt))
        # ── Slice M — perf indexes (Jun 2026) ────────────────────────────
        # M5 — algo_orders.basket_tag partial index. CLAUDE.md documents
        # this as a queryable column; the basket-order margin endpoint
        # groups by basket_tag. As basket-order volume grows, the
        # column would seq-scan without this. Partial-index on
        # `basket_tag IS NOT NULL` so the index only carries rows
        # that actually have a basket tag (most rows are stand-alone).
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_basket_tag "
            "ON algo_orders (basket_tag) WHERE basket_tag IS NOT NULL"
        ))
        # M5 — algo_orders.account + algo_orders.symbol. /admin/history
        # Orders endpoint filters on these columns plus created_at.
        # The created_at index narrows the date range but account+symbol
        # filter then scans the result set. Two singleton indexes are
        # cheap to maintain (low write amplification on an append-only
        # table).
        for stmt in (
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_account "
            "ON algo_orders (account)",
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_symbol "
            "ON algo_orders (symbol)",
        ):
            await conn.execute(text(stmt))
        # M5 — agent_events composite (sim_mode, agent_id, timestamp DESC).
        # The three existing singleton indexes (`ix_agent_events_*`)
        # match the query columns individually; Postgres picks one and
        # filters the rest in memory. The composite lets the planner
        # satisfy the common /admin/alerts path
        #   WHERE sim_mode = ? [AND agent_id = ?] ORDER BY timestamp DESC
        # via a single index scan. Leaving the singletons in place
        # for now — they cost ~10% write amp on a low-write table and
        # the audit was a MED, not a HIGH. Re-evaluate the drop in a
        # future cleanup slice.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_agent_events_simmode_agent_ts "
            "ON agent_events (sim_mode, agent_id, timestamp DESC)"
        ))
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
            # Phase 0 — template_id + attached_gtts so the postback
            # handler can fire apply_plan_live(template, actual_fill_price)
            # on the COMPLETE event for each templated parent order.
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS template_id INTEGER "
            "REFERENCES order_templates(id) ON DELETE SET NULL",
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_template_id "
            "ON algo_orders (template_id)",
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS attached_gtts_json TEXT",
            # Phase 2 of the template/on-fill rework — stores the
            # operator's per-submit TP%/SL%/Wing override tweaks so the
            # postback handler can re-apply them when the parent fills.
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS template_overrides_json TEXT",
            # Audit fix (M-6) — current re-quoted limit. Chase loop
            # writes this on every cancel-and-replace so the chase
            # panel can show the LIVE limit price instead of the FIRST
            # attempt's price (which read stale after 3+ iterations).
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS current_limit DOUBLE PRECISION",
            # Phase 3C #2 — parent_product on AlgoOrder. Pre-fix the
            # postback handler hardcoded NRML on every exit leg; MIS
            # day-trades got rejected or left as overnight carry.
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS product "
            "VARCHAR(8) NOT NULL DEFAULT 'NRML'",
            # Phase 1A — tp_order_type on OrderTemplate. Defaults to
            # 'LIMIT' so every existing row keeps prior behaviour on
            # this deploy; the seeder only writes 'MARKET' to the new
            # default-long-option template (Phase 1B+).
            "ALTER TABLE order_templates ADD COLUMN IF NOT EXISTS tp_order_type "
            "VARCHAR(8) NOT NULL DEFAULT 'LIMIT'",
            # Phase 3A — scale-out targets. JSON list per OrderTemplate.
            # NULL for every existing row → no scale-out (single TP via
            # tp_pct keeps working). Operator opts in by editing the
            # template on /automation/templates.
            "ALTER TABLE order_templates ADD COLUMN IF NOT EXISTS tp_scales_json TEXT",
            # Phase 3B — trailing stop distance %. NULL = no trailing
            # (SL stays fixed at fill × (1 − sl_pct/100)). When set,
            # the _task_trail_stop background poller bumps the
            # attached SL GTT's trigger to track the favorable side
            # of LTP every templates.trail_poll_interval_seconds.
            "ALTER TABLE order_templates ADD COLUMN IF NOT EXISTS sl_trail_pct "
            "NUMERIC(8, 4)",
            # Sprint B (#4) — cumulative filled qty across chase
            # partials. Defaults to 0 so existing rows are well-formed
            # without a back-fill pass; downstream readers can treat
            # `filled_quantity == 0 AND status == FILLED` as "broker
            # reported a single complete fill" (legacy behaviour) and
            # `filled_quantity > 0 AND status == OPEN` as "actively
            # chasing the residual".
            "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS filled_quantity "
            "INTEGER NOT NULL DEFAULT 0",
            # Sprint E (audit #7) — composite (mode, status) index. Hot
            # path for _task_trail_stop + _task_oco_pair_watcher +
            # paper recover_from_db. CREATE INDEX IF NOT EXISTS works
            # on every supported Postgres; metadata.create_all skips
            # adding the index to an existing table so this migration
            # is the actual on-disk effect.
            "CREATE INDEX IF NOT EXISTS ix_algo_orders_mode_status "
            "ON algo_orders (mode, status)",
            # Sprint F (post-audit perf) — composite to back the
            # `DISTINCT ON (account, symbol)` query in
            # `_override_stale_close_from_snapshot` (Sprint D close-
            # override). Runs on every /api/positions/ cache miss; the
            # plain (date, account) + (date, segment) indexes don't
            # support the captured_at-desc sort.
            "CREATE INDEX IF NOT EXISTS ix_daily_book_kind_acct_sym_captured "
            "ON daily_book (kind, account, symbol, captured_at)",
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
        # `uq_watchlist_global_pinned` is created earlier in this block
        # with the correct `(name) WHERE is_global = true` predicate
        # (slice G2). The legacy `((1)) WHERE is_global = true` re-create
        # that used to live here had the wrong shape (allowed only ONE
        # global row in the whole table) and was deleted in slice L1.
        # IF NOT EXISTS made it a silent no-op as long as the G2 block
        # ran first, but kept a footgun in the migration ordering.
        # ── Slice Q — FK ondelete edges (Jun 2026) ───────────────────────
        # Q5a — algo_orders.parent_order_id → ON DELETE SET NULL.
        # Self-referential FK; deleting a parent (e.g. manual cleanup)
        # was blocked at the DB level. SET NULL preserves the child row
        # (the TP/SL order history) while releasing the back-reference.
        for stmt in (
            "ALTER TABLE algo_orders DROP CONSTRAINT IF EXISTS algo_orders_parent_order_id_fkey",
            "ALTER TABLE algo_orders ADD CONSTRAINT algo_orders_parent_order_id_fkey "
            "FOREIGN KEY (parent_order_id) REFERENCES algo_orders(id) ON DELETE SET NULL",
        ):
            await conn.execute(text(stmt))
        # Q5b — algo_order_events.order_id → ON DELETE CASCADE.
        # Timeline rows are append-only and meaningless without their
        # parent AlgoOrder. CASCADE lets operators delete a chase row
        # (e.g. during sim cleanup) without first manually purging the
        # event timeline, and avoids the silent NO ACTION block.
        for stmt in (
            "ALTER TABLE algo_order_events DROP CONSTRAINT IF EXISTS algo_order_events_order_id_fkey",
            "ALTER TABLE algo_order_events ADD CONSTRAINT algo_order_events_order_id_fkey "
            "FOREIGN KEY (order_id) REFERENCES algo_orders(id) ON DELETE CASCADE",
        ):
            await conn.execute(text(stmt))
        # S6 — watchlists.user_id FK → ON DELETE SET NULL.
        # user_id is nullable (global rows have user_id=NULL); without
        # the ondelete clause, deleting a user with personal watchlists
        # was blocked at the DB level (NO ACTION default). SET NULL
        # releases the back-reference while keeping the watchlist rows.
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
        # R6a/b/c — three index migrations.
        # WHY THE TRY/EXCEPT WRAPPER: index DDL requires table ownership.
        # `impersonation_events` was created historically as user `postgres`
        # on the production DB while every other table was created as the
        # app user — `CREATE INDEX … ON impersonation_events` then errored
        # with InsufficientPrivilege and aborted on_startup, taking the API
        # down with a Cloudflare host error. Operator fix is a one-time
        # `ALTER TABLE … OWNER TO rambo_admin` as postgres; this wrapper
        # ensures that future ownership drift on ANY table can never crash
        # boot again. Index DDL failure is operator-visible (degraded perf,
        # logged WARNING) but is never load-bearing for correctness.
        _index_migrations = (
            # R6a — drop the now-redundant singleton ix_agent_events_sim_mode.
            # The slice-M composite (ix_agent_events_simmode_agent_ts) satisfies
            # every query that the singleton did. DROP IF EXISTS idempotent.
            "DROP INDEX IF EXISTS ix_agent_events_sim_mode",
            # R6b — unique partial index backs the active-session lookup in
            # auth.py stop_impersonate (WHERE ended_at IS NULL). Without it
            # the most-recent open row is a seq-scan + filter on a growing
            # audit table.
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_impersonation_active "
            "ON impersonation_events (actor_username, target_username) "
            "WHERE ended_at IS NULL",
            # R6c — backs research.py's default ORDER BY updated_at DESC on
            # every Lab page load.
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
        # ── Slice T — index additions + drops (Jun 2026) ─────────────────
        # T-8a: composite (order_id, ts) on algo_order_events.
        # Backs orders.py per-order timeline queries which ORDER BY ts;
        # the existing singleton ix_algo_order_events_order_id covers
        # equality-filter but Postgres must sort the result set. The
        # composite covers filter + sort in one index scan.
        # T-8b: admin_email_events.created_at DESC for admin.py ORDER BY.
        # T-8c: monthly_statements (period_year, period_month) for the
        # investor.py admin filter without a user_id predicate.
        # T-8e: DROP ix_audit_log_path — audit_log.path has index=True
        # but no WHERE clause uses it; pure write amplification on a
        # high-volume table.
        _slice_t_indexes = (
            "CREATE INDEX IF NOT EXISTS ix_algo_order_events_order_id_ts "
            "ON algo_order_events (order_id, ts)",
            "CREATE INDEX IF NOT EXISTS ix_admin_email_events_created_at "
            "ON admin_email_events (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_monthly_statements_period "
            "ON monthly_statements (period_year, period_month)",
            # T-8d: DROP ix_strategy_snapshots_date — redundant; the
            # unique constraint (strategy_id, as_of_date) already covers
            # point-lookups. Singleton date-only index adds write
            # amplification with no query benefit.
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
        # S7 — audit_log.action GIN trigram index.
        # btree (the ORM default when index=True) cannot serve leading-wildcard
        # ilike queries like ilike("%X%"). Replaced with a pg_trgm GIN index.
        # Requires the pg_trgm extension (superuser CREATE EXTENSION); graceful
        # degradation when unavailable — queries still return correct results via
        # seq-scan, just slower on large audit tables. Each step in its own
        # try/except so a pg_trgm absence doesn't prevent the DROP of the old index.
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
        # ohlcv_daily table + index (Stage 1 OHLCV persistence).
        try:
            from backend.api.persistence.migrations import create_ohlcv_daily_table
            await create_ohlcv_daily_table(conn)
        except Exception as _ohlcv_err:
            logger.warning("init_db: ohlcv_daily migration skipped — %s", _ohlcv_err)
        # instruments_snapshot table (Stage 2 OHLCV persistence).
        try:
            from backend.api.persistence.migrations import create_instruments_snapshot_table
            await create_instruments_snapshot_table(conn)
        except Exception as _instr_err:
            logger.warning("init_db: instruments_snapshot migration skipped — %s", _instr_err)
        # holidays_snapshot table (Stage 2 OHLCV persistence).
        try:
            from backend.api.persistence.migrations import create_holidays_snapshot_table
            await create_holidays_snapshot_table(conn)
        except Exception as _hol_err:
            logger.warning("init_db: holidays_snapshot migration skipped — %s", _hol_err)
        # intraday_bars table (intraday persistence pipeline).
        try:
            from backend.api.persistence.migrations import create_intraday_bars_table
            await create_intraday_bars_table(conn)
        except Exception as _intraday_err:
            logger.warning("init_db: intraday_bars migration skipped — %s", _intraday_err)

        # code_metrics_snapshots — captured per release by
        # scripts/capture_metrics.py. The table itself is created by
        # Base.metadata.create_all above (the model is registered in
        # the import list); these extra index DDLs are idempotent.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_code_metrics_captured_at "
            "ON code_metrics_snapshots (captured_at DESC)"
        ))
        # Add test_response_times column if it doesn't exist yet
        # (idempotent — ALTER TABLE ADD COLUMN IF NOT EXISTS is safe to
        # run on every startup against a pre-existing table).
        await conn.execute(text(
            "ALTER TABLE code_metrics_snapshots "
            "ADD COLUMN IF NOT EXISTS test_response_times JSONB"
        ))
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

    # Warm the alert-recipient cache from the users table so the very
    # first alert after a restart already routes to the right addresses.
    from backend.shared.helpers.alert_utils import refresh_alert_recipients
    await refresh_alert_recipients()


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
        # Create the table if it doesn't exist (prod: no-op).
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[BrokerAccount.__table__],
                checkfirst=True,
            )
        )
        # Multi-broker extension (May 2026) — broker_accounts gains four
        # columns so a Dhan / Groww / future-vendor account fits the same
        # schema as Kite. See historical commit notes in git log.
        for stmt in (
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS client_id VARCHAR(64)",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS access_token_enc TEXT",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS priority INTEGER "
            "NOT NULL DEFAULT 100",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS extra_config JSONB "
            "NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "historical_data_enabled BOOLEAN NOT NULL DEFAULT TRUE",
            # Per-account poll priority (Jul 2026 — Dhan background poll
            # interval gate). poll_priority controls how often the
            # background poller re-fetches this Dhan account.
            # auto_downgrade_* columns support the breaker-history watcher.
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "poll_priority VARCHAR(8) NOT NULL DEFAULT 'hot'",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "auto_downgrade_enabled BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "auto_downgraded_at TIMESTAMPTZ",
            "ALTER TABLE broker_accounts ADD COLUMN IF NOT EXISTS "
            "auto_downgrade_reason TEXT",
        ):
            await conn.execute(text(stmt))
    logger.info("Shared broker schema verified on ramboq")


async def get_session() -> AsyncSession:
    """Yield an async session (for use in route handlers)."""
    async with async_session() as session:
        yield session
