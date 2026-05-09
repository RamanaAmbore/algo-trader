"""
SQLAlchemy ORM models — user and partner management.
"""

import secrets as _secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


def _gen_account_id() -> str:
    """Generate a unique account key like rambo-a3f8b2."""
    return f"rambo-{_secrets.token_hex(3)}"

from backend.api.database import Base


class User(Base):
    __tablename__ = "users"

    # ── Identity ──────────────────────────────────────────────────────────────
    id: Mapped[int]             = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[str]     = mapped_column(String(16), unique=True, nullable=False, default=_gen_account_id, index=True)
    username: Mapped[str]       = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str]  = mapped_column(Text, nullable=False)
    role: Mapped[str]           = mapped_column(String(16), nullable=False, default="partner")
    display_name: Mapped[str]   = mapped_column(String(128), nullable=False, default="")
    email: Mapped[Optional[str]]       = mapped_column(String(128), nullable=True)
    phone: Mapped[Optional[str]]       = mapped_column(String(20), nullable=True)

    # ── KYC / compliance ──────────────────────────────────────────────────────
    pan: Mapped[Optional[str]]         = mapped_column(String(10), nullable=True)   # Indian PAN
    aadhaar_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)  # last 4 digits only
    date_of_birth: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    kyc_verified: Mapped[bool]  = mapped_column(Boolean, nullable=False, default=False)

    # ── Address ───────────────────────────────────────────────────────────────
    address_line1: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    address_line2: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    city: Mapped[Optional[str]]          = mapped_column(String(64), nullable=True)
    state: Mapped[Optional[str]]         = mapped_column(String(64), nullable=True)
    pincode: Mapped[Optional[str]]       = mapped_column(String(10), nullable=True)

    # ── Investment / partnership ───────────────────────────────────────────────
    contribution: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    contribution_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    share_pct: Mapped[float]    = mapped_column(Float, nullable=False, default=0.0)

    # ── Bank details (for payouts) ────────────────────────────────────────────
    bank_name: Mapped[Optional[str]]     = mapped_column(String(128), nullable=True)
    bank_account: Mapped[Optional[str]]  = mapped_column(String(32), nullable=True)
    bank_ifsc: Mapped[Optional[str]]     = mapped_column(String(16), nullable=True)

    # ── Nominee ───────────────────────────────────────────────────────────────
    nominee_name: Mapped[Optional[str]]     = mapped_column(String(128), nullable=True)
    nominee_relation: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    nominee_phone: Mapped[Optional[str]]    = mapped_column(String(20), nullable=True)

    # ── Status ────────────────────────────────────────────────────────────────
    is_approved: Mapped[bool]   = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool]     = mapped_column(Boolean, nullable=False, default=True)
    join_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # admin notes

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Algo — chase orders and events
# ---------------------------------------------------------------------------

class AlgoOrder(Base):
    __tablename__ = "algo_orders"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    account: Mapped[str]         = mapped_column(String(32), nullable=False)
    symbol: Mapped[str]          = mapped_column(String(64), nullable=False)
    exchange: Mapped[str]        = mapped_column(String(8), nullable=False, default="NFO")
    transaction_type: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY/SELL
    quantity: Mapped[int]        = mapped_column(Integer, nullable=False)
    initial_price: Mapped[float] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float]    = mapped_column(Float, nullable=True)
    attempts: Mapped[int]        = mapped_column(Integer, nullable=False, default=0)
    slippage: Mapped[float]      = mapped_column(Float, nullable=True)
    status: Mapped[str]          = mapped_column(String(16), nullable=False, default="OPEN", index=True)
    engine: Mapped[str]          = mapped_column(String(16), nullable=False, default="manual")  # sim/paper/live/replay/shadow/expiry/manual
    mode: Mapped[str]            = mapped_column(String(8), nullable=False, default="live", index=True)  # sim/paper/live/replay/shadow
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expiry_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AlgoEvent(Base):
    __tablename__ = "algo_events"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    algo_order_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("algo_orders.id"), nullable=True)
    event_type: Mapped[str]      = mapped_column(String(32), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class AlgoOrderEvent(Base):
    """Append-only per-order timeline. One row per state transition so
    operators have a full audit trail (placed → chase_modify × N → fill /
    unfill / reject) without overwriting AlgoOrder.detail."""
    __tablename__ = "algo_order_events"

    id: Mapped[int]       = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("algo_orders.id"), nullable=False, index=True
    )
    ts: Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # kind ∈ placed | chase_modify | fill | unfill | reject | cancel |
    #         postback | margin_check | preflight_ok | preflight_block | error
    kind: Mapped[str]     = mapped_column(String(32), nullable=False)
    # Human-readable one-liner: "chase #2 limit=₹181"
    message: Mapped[str]  = mapped_column(String(500), nullable=False, default="")
    # Structured detail (limit, qty, slippage, broker_response, …). Nullable.
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Agent Framework — Conditions → Alerts → Actions
# ---------------------------------------------------------------------------

class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    slug: Mapped[str]            = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str]            = mapped_column(String(128), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Condition tree (AND/OR/NOT with account selection)
    conditions: Mapped[dict]     = mapped_column(JSONB, nullable=False, default=dict)

    # Alert channels
    events: Mapped[list]         = mapped_column(JSONB, nullable=False, default=list)

    # Actions (empty list = alert-only)
    actions: Mapped[list]        = mapped_column(JSONB, nullable=False, default=list)

    # Evaluation config
    scope: Mapped[str]           = mapped_column(String(16), nullable=False, default="per_account")
    schedule: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default="market_hours")
    cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # Per-agent trade routing — paper / live. Per-agent override
    # consulted by actions._resolve_mode AFTER the dev / shadow gates;
    # default seeded from execution.default_agent_trade_mode at create
    # time. Keeps the master execution.paper_trading_mode kill-switch
    # working as a global "force paper" override.
    trade_mode: Mapped[str]      = mapped_column(String(8), nullable=False, default="paper")

    # Runtime state. `status` ∈ active / inactive / cooldown / completed.
    # `completed` is the terminal state for lifespan-bounded agents
    # (one_shot, n_fires, until_date). Engine skips `completed` rows
    # entirely; operator can re-arm by editing back to active/inactive.
    status: Mapped[str]          = mapped_column(String(16), nullable=False, default="inactive")
    last_triggered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger_count: Mapped[int]   = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Lifespan — controls whether the agent persists or auto-completes.
    # Industry analogue: TradingView's "once" vs "every time" alerts;
    # IBKR's Active vs Triggered. Lets algos spawn one-shot agents
    # (e.g. an expiry-day auto-close that should only fire once) AND
    # operators keep persistent agents (loss alerts, summaries).
    #
    #   "persistent" : default. Active ↔ Cooldown forever until
    #                  operator deactivates.
    #   "one_shot"   : fires ONCE then completes.
    #   "n_fires"    : fires up to lifespan_max_fires times then
    #                  completes (1 = same as one_shot but more
    #                  explicit; >1 = bounded recurring agent).
    #   "until_date" : completes when now >= lifespan_expires_at.
    #                  Useful for "watch this until expiry" agents
    #                  that algos spawn with a known end date.
    lifespan_type: Mapped[str]   = mapped_column(
        String(16), nullable=False, default="persistent"
    )
    lifespan_max_fires: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    lifespan_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Meta
    is_system: Mapped[bool]      = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    agent_id: Mapped[int]        = mapped_column(Integer, ForeignKey("agents.id"), nullable=False)
    event_type: Mapped[str]      = mapped_column(String(32), nullable=False)
    trigger_condition: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sim_mode: Mapped[bool]       = mapped_column(Boolean, nullable=False, default=False)
    timestamp: Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Market report — single-row cache (id=1). Reused across deploys when <24h old.
# ---------------------------------------------------------------------------

class MarketReport(Base):
    __tablename__ = "market_report"

    id: Mapped[int]            = mapped_column(Integer, primary_key=True)
    content: Mapped[str]       = mapped_column(Text, nullable=False)
    cycle_date: Mapped[str]    = mapped_column(String(32), nullable=False)
    refreshed_at: Mapped[str]  = mapped_column(String(128), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Agent grammar — the extensible token catalog that defines every condition,
# notify channel, and action available to the Agent engine. Built-in tokens
# are seeded at startup with is_system=True; operators add/tune runtime
# tokens via the admin UI (planned) without restarting.
# ---------------------------------------------------------------------------

class GrammarToken(Base):
    __tablename__ = "grammar_tokens"

    id: Mapped[int]           = mapped_column(primary_key=True, autoincrement=True)

    # Three grammar domains — each has its own token namespace:
    #   'condition' : metric, scope, operator, function
    #   'notify'    : channel, format, template
    #   'action'    : action_type
    grammar_kind: Mapped[str] = mapped_column(String(16),  nullable=False, index=True)
    token_kind: Mapped[str]   = mapped_column(String(32),  nullable=False, index=True)
    token: Mapped[str]        = mapped_column(String(128), nullable=False)

    # Semantic classification of the value the token produces or accepts.
    # 'number' | 'string' | 'boolean' | 'enum' | 'array' | 'object' | 'void'
    value_type: Mapped[Optional[str]] = mapped_column(String(16),  nullable=True)
    # Human-readable unit for numeric metrics: "₹", "%", "₹/min", "%/min", "min", ...
    units: Mapped[Optional[str]]      = mapped_column(String(16),  nullable=True)
    description: Mapped[str]          = mapped_column(Text,        nullable=False, default="")

    # Dispatch pointer. For metric/scope/operator/action_type/function: dotted
    # path to a Python resolver/handler function. For channel/format: dotted
    # path to a class or callable. The engine imports by name at reload time.
    resolver: Mapped[Optional[str]]   = mapped_column(String(256), nullable=True)

    # Structured schema describing expected params — used by the admin UI to
    # render forms and by the runtime to validate. Shape:
    #   {"param_name": {"type": "number|string|enum|...", "required": true,
    #                    "enum": [...], "default": ..., "token_ref_ok": true}}
    params_schema: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # For enum value_types: list of legal string values.
    enum_values: Mapped[Optional[list]]   = mapped_column(JSONB, nullable=True)
    # For notify template tokens: the template body with ${placeholder} syntax.
    template_body: Mapped[Optional[str]]  = mapped_column(Text, nullable=True)

    # System tokens ship with the code and are regenerated from seeds each boot.
    # Operators cannot delete them; they can only deactivate. Custom tokens have
    # is_system=False and are freely editable/deletable via the admin UI.
    is_system: Mapped[bool]   = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool]   = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint('grammar_kind', 'token_kind', 'token',
                         name='uq_grammar_token'),
    )


# ---------------------------------------------------------------------------
# Settings — DB-backed tunables. Previously lived in backend_config.yaml;
# moved here so operators can tweak thresholds, cadences, and capability
# flags from /admin/settings without a deploy. Seeded on first boot from
# YAML defaults (see backend/api/algo/settings_seed.py).
# ---------------------------------------------------------------------------

class Setting(Base):
    __tablename__ = "settings"

    id: Mapped[int]           = mapped_column(primary_key=True, autoincrement=True)
    # Bucket name shown as a section heading on /admin/settings. Seeded
    # buckets: alerts / performance / simulator / notifications / logging.
    category: Mapped[str]     = mapped_column(String(32), nullable=False, index=True)
    # Dotted path key, unique across all categories.
    key: Mapped[str]          = mapped_column(String(128), unique=True, nullable=False, index=True)
    # Value type discriminator: 'int' | 'float' | 'bool' | 'string' | 'enum'.
    value_type: Mapped[str]   = mapped_column(String(16), nullable=False)
    # Serialised value string — always text; parsers handle coercion.
    value: Mapped[str]        = mapped_column(Text, nullable=False, default="")
    # Default shipped with the code; used when "Reset" is pressed.
    default_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # One-line human description rendered in the UI.
    description: Mapped[str]  = mapped_column(Text, nullable=False, default="")
    # For enum: {"enum": ["..."]}. For numeric: {"min": ..., "max": ..., "step": ...}.
    schema: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Units shown after the value in the UI: '₹', '%', 'min', '₹/min', etc.
    units: Mapped[Optional[str]]   = mapped_column(String(16), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# News headlines — accumulated throughout the day, truncated at 07:00 IST
# ---------------------------------------------------------------------------

class NewsHeadline(Base):
    __tablename__ = "news_headlines"

    link: Mapped[str]          = mapped_column(Text, primary_key=True)
    title: Mapped[str]         = mapped_column(Text, nullable=False)
    source: Mapped[str]        = mapped_column(String(128), nullable=False, default="")
    timestamp_display: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Broker accounts — DB-backed credentials with at-rest encryption.
# secrets.yaml seeds this table on first run; subsequent CRUD edits go
# through the /admin/brokers UI and the Connections singleton reloads
# from here without a service restart.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Daily book — per-account, per-symbol end-of-day snapshot for P&L tracking
# ---------------------------------------------------------------------------

class DailyBook(Base):
    """Per-account, per-symbol daily snapshot — feeds the P&L
    date-range page. One row per (date, account, kind, symbol)."""
    __tablename__ = "daily_book"

    id: Mapped[int]            = mapped_column(primary_key=True, autoincrement=True)
    date: Mapped[datetime]     = mapped_column(Date, nullable=False, index=True)
    account: Mapped[str]       = mapped_column(String(32), nullable=False, index=True)
    segment: Mapped[str]       = mapped_column(String(16), nullable=False)   # 'equity' | 'commodity' | 'currency' | 'derivatives'
    kind: Mapped[str]          = mapped_column(String(16), nullable=False)   # 'holdings' | 'positions' | 'trades'
    symbol: Mapped[str]        = mapped_column(String(64), nullable=False)
    exchange: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    qty: Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    avg_cost: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)
    ltp: Mapped[Optional[float]]      = mapped_column(Numeric, nullable=True)
    day_pnl: Mapped[Optional[float]]  = mapped_column(Numeric, nullable=True)
    total_pnl: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # raw row for forensics
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        # One snapshot per symbol per day per account per kind. Re-running
        # the snapshot updates the row instead of duplicating.
        UniqueConstraint("date", "account", "kind", "symbol", name="uq_daily_book_day_acct_kind_sym"),
    )


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    # Account code (e.g. "ZG0790") — keep unique so the code paths that
    # already key on this string keep working without a per-row id rewrite.
    account: Mapped[str]         = mapped_column(String(32), unique=True, nullable=False)
    broker_id: Mapped[str]       = mapped_column(String(16), nullable=False, default="kite")
    # api_key is plaintext — Kite API keys aren't a credentialing secret
    # (they pair with api_secret to authenticate, but the key alone leaks
    # nothing). Keeping it in the clear means it shows up unmasked in
    # admin UI lists, which is what operators expect.
    api_key: Mapped[str]         = mapped_column(String(64), nullable=False)
    # Fernet-encrypted; key derived from cookie_secret via HKDF.
    api_secret_enc: Mapped[str]  = mapped_column(Text, nullable=False)
    password_enc: Mapped[str]    = mapped_column(Text, nullable=False)
    totp_token_enc: Mapped[str]  = mapped_column(Text, nullable=False)
    # IPv6 source binding (Kite enforces one IP per app). Optional —
    # accounts without this fall back to the OS default route.
    source_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool]      = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
