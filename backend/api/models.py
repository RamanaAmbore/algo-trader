"""
SQLAlchemy ORM models — user and partner management.
"""

import secrets as _secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, Time, UniqueConstraint, text
from sqlalchemy import func
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
    # Role tiers — single source of truth for privilege. Canonical 5:
    # designated / trader / risk / admin / partner (+ demo synthetic).
    # 'designated' = firm owner; 'admin' = operational support;
    # 'partner' = LP read-only (default for self-registration).
    # See backend/api/rbac.py for the cap matrix.
    role: Mapped[str]           = mapped_column(String(32), nullable=False, default="partner")
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
    # Per-admin opt-in for getting platform alerts (loss thresholds,
    # market summaries, agent fires, deploy notifications).
    #   - designated rows always receive alerts (this flag is ignored
    #     for them; designated email is broadcast on every event).
    #   - admin rows receive alerts only when this is True.
    #   - partner rows never receive alerts.
    receive_alerts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set when an admin/designated resets the user's password via the
    # /admin/users/{u}/reset-password endpoint. The user is forced to
    # set their own password on next login (admin-supplied passwords
    # are throwaway). Cleared by /api/auth/change-password.
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Email verification — required before admin can flip is_approved=True
    # for self-registered users. Populated when /api/auth/verify-email
    # consumes a one-time token.
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # token_version — every JWT carries the value at issue time; admin_guard
    # re-checks DB vs token. Bumping this column invalidates every live JWT
    # for the user. Used by the Suspend / Terminate / Reset Password actions
    # so a force-logout takes effect on the next request.
    token_version: Mapped[int]  = mapped_column(Integer, nullable=False, default=1)
    # Suspension — reversible. Set when admin clicks Suspend; cleared on
    # Reinstate. While suspended, login is blocked AND existing JWTs are
    # invalidated via the token_version bump on the same write.
    suspended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Termination — terminal state, not reversible from the UI. Sets
    # is_active=False and records the wall-clock time. Distinct from a
    # rejected (never-approved) row so the operator can audit the difference.
    terminated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    join_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # admin notes

    # ── RBAC scoping (slice 5) ────────────────────────────────────────────────
    # Per-user horizontal scope. Sits ON TOP of the capability matrix:
    # the cap decides "can role X do action Y?", these decide "on which
    # accounts / strategies?". Empty list means "no explicit scope" —
    # designated / risk / admin / partner / demo treat empty as ALL
    # (those roles are firm-wide by design); trader treats empty as
    # NONE (fail-safe — a freshly-assigned trader sees nothing until
    # designated grants accounts explicitly).
    #
    # Stored as JSONB rather than ARRAY for two reasons:
    #   1) avoids an Alembic-style migration in a non-Alembic codebase
    #      (the rest of this app uses JSONB exclusively for list-shaped
    #      data; matches existing convention).
    #   2) trivially extends to richer per-account metadata later
    #      (limits, can_trade_options, etc.) without another column.
    assigned_accounts: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    assigned_strategies: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    # SEBI Cat-III compliance flag. Orthogonal to `role` — the legal
    # designation of compliance officer is a real-world title that can
    # overlap with any role (often risk or admin, sometimes designated). The
    # flag surfaces in the audit UI + future operator-attestation forms
    # without complicating the role enum. NULL = not designated.
    compliance_designated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )

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
# Impersonation — admin / designated views the platform as a partner
# ---------------------------------------------------------------------------

class ImpersonationEvent(Base):
    """
    Audit row for every impersonation start + end. An actor (admin or
    designated) starts impersonating a target via POST /api/auth/
    impersonate/{target_username}; the row is created at start with
    `ended_at = NULL`. POST /api/auth/stop-impersonate (or token
    expiry, currently 30 min TTL) fills `ended_at` + `end_reason`.

    Write endpoints triggered under an impersonation JWT emit
    WARNING-level logs with `imp_by` so the forensic trail outside this
    table is complete (writes themselves persist via their normal
    audit rows — orders, agent events, settings — but the imp_by
    annotation links them back to the impersonator).
    """
    __tablename__ = "impersonation_events"

    id:                Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    actor_username:    Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_role_at_time: Mapped[str] = mapped_column(String(32), nullable=False)
    target_username:   Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_role_at_time: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at:        Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # NULL while session is active. Set on POST /stop-impersonate, on
    # token expiry detection, or on revoke (admin Suspend / Terminate).
    ended_at:          Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 'stopped' | 'expired' | 'revoked' — populated when ended_at is set.
    end_reason:        Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


# ---------------------------------------------------------------------------
# Auth — short-lived tokens for email verification + password reset
# ---------------------------------------------------------------------------

class MonthlyStatement(Base):
    """
    Audit row for the monthly auto-emailed LP statement. One row per
    (user, period_year, period_month). The unique constraint is the
    idempotency guarantee — the daily background task only generates
    + sends when no row exists, then INSERTs on success. A duplicate
    INSERT (from a race or a redundant trigger) blocks at the DB
    level so an LP never receives two copies of the same statement.

    `sent_at IS NULL AND error IS NOT NULL` indicates a failed send
    (SMTP error, PDF render failure, etc.); admin can inspect from
    /admin and trigger a retry — failed rows can be deleted so the
    daily task picks up the period again on the next wake.

    `recipients_json` is the list of email addresses we actually
    delivered to (snapshot at send time so if the LP later changes
    their email, the audit log still shows where we sent the
    statement). PDF bytes are NOT stored — the renderer is stateless
    and we can recompute any historical statement on demand.
    """
    __tablename__ = "monthly_statements"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "period_year", "period_month",
            name="uq_monthly_statements_user_period",
        ),
    )

    id:              Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id:         Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    period_year:     Mapped[int] = mapped_column(Integer, nullable=False)
    period_month:    Mapped[int] = mapped_column(Integer, nullable=False)
    generated_at:    Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    sent_at:         Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    recipients_json: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    pdf_size_bytes:  Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error:           Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class InvestorEvent(Base):
    """
    Subscription / redemption event for an LP. One row per capital
    movement; the operator logs these as they happen.

    Today this is a passive log — NAV slice math still uses
    `User.share_pct × firm_nav` (the v1 static-share model). The
    next slice switches the NAV computation to consume these
    events via the standard fund-accounting units model:

        units_held(user)   = sum(units_delta for user's events)
        nav_per_unit(today) = current_firm_nav / sum(units_held across LPs)
        slice(user, today) = units_held × nav_per_unit
        pnl(user, today)   = slice - (sum(subscriptions) - sum(redemptions))

    Industry analog: Carta partnership unit register, CAMSonline
    mutual-fund units, SS&C subscription/redemption journal.

    Units math:
    - Subscription: `units_delta = amount / nav_per_unit` (positive)
    - Redemption:   `units_delta = -amount / nav_per_unit` (negative)
    - Operator supplies amount + nav_per_unit; backend computes the
      delta + sign. Idempotency relies on the operator NOT re-
      submitting; no DB-level unique key since legitimate same-day
      same-amount events are possible (rare but valid).

    Exception — bootstrap: `ensure_user_bootstrap` is a check-then-
    insert that two concurrent callers (e.g. two LPs loading their
    portals at once) could race, ending with two bootstrap rows for
    the same user. Doubled total_units deflates nav_per_unit fund-
    wide. The partial unique index below forces the DB to reject
    the second insert; the application code is best-effort with
    try/rollback on IntegrityError.
    """
    __tablename__ = "investor_events"
    __table_args__ = (
        Index(
            "uq_investor_events_user_bootstrap",
            "user_id", unique=True,
            postgresql_where=text("event_type = 'bootstrap'"),
        ),
    )

    id:               Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # LP fund-accounting ledger. ondelete=RESTRICT: deleting a user with
    # capital events must be a deliberate operator action (redeem first,
    # then delete) — never a silent cascade. The audit trail is the
    # authoritative record of every rupee the LP put in and pulled out.
    user_id:          Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    # 'subscription' (capital in) | 'redemption' (capital out) |
    # 'bootstrap' (synthetic seed event when migrating an existing
    # LP from the static-share model). Use 'bootstrap' so the
    # operator can tell which rows are real vs auto-generated.
    event_type:       Mapped[str] = mapped_column(String(16), nullable=False)
    event_date:       Mapped[datetime] = mapped_column(Date, nullable=False, index=True)
    # Cash amount in ₹. Always stored as a positive number; the sign
    # of units_delta encodes direction (subscription positive,
    # redemption negative).
    amount:           Mapped[float] = mapped_column(Float, nullable=False)
    # NAV per unit at the time of the event. Operator-supplied at
    # the event date so historical events can be backdated with the
    # correct per-unit value (read from the NavDaily curve on the
    # event date).
    nav_per_unit:     Mapped[float] = mapped_column(Float, nullable=False)
    # Signed units delta — positive for subscription, negative for
    # redemption. Computed at insert time as ±amount/nav_per_unit;
    # stored explicitly so reads don't re-derive on every call.
    units_delta:      Mapped[float] = mapped_column(Float, nullable=False)
    note:             Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by:       Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    created_at:       Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class InvestorToken(Base):
    """
    Long-lived, revocable, public URL token for LP-facing investor
    portal access. The token IS the credential — anyone holding the
    URL `/investor/<token>` reads the LP's NAV slice + history.
    Operator mints from `/admin/users/{id}/investor-token`, then
    forwards the URL to the LP (email / WhatsApp); no LP login.

    Revocation: set `revoked_at`. Once non-null the token is dead;
    next visit returns 401. Re-issue by minting a new row (the old
    row stays for the audit trail).

    Expiry: 90-day default. Operator picks at mint time; longer is
    fine for trusted LPs. Each visit bumps `visit_count` +
    `last_visit_at` so the operator can see "this LP last looked at
    statements 3 weeks ago" from the admin UI.

    Industry analog: Carta investor portal magic-link, SS&C / GP-
    Link share-class URLs, Yieldstreet's LP-specific URL slugs.
    """
    __tablename__ = "investor_tokens"

    id:            Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Investor URL tokens are credentials. CASCADE on user delete — the
    # token no longer has a holder and a stale URL should die with them.
    user_id:       Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 32-byte secrets.token_hex → 64-char string. Stored raw (same
    # convention as AuthToken) — the token is the URL slug, not a
    # password; hashing would just complicate revoke + lookup
    # without adding security since possession of the URL == access.
    token:         Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_visit_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    visit_count:   Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Admin-supplied label, e.g. "Mailed to LP via WhatsApp 2026-06-23"
    note:          Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Audit: which admin minted this token. Nullable so a self-mint
    # via /admin won't break if the originator has been deleted
    # later.
    created_by:    Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    created_at:    Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class AuthToken(Base):
    """
    One-time token for email verification + password reset. Single table
    with a `purpose` discriminator so we don't carry two near-identical
    schemas. Tokens are 32-byte secrets, hex-encoded, single-use, with a
    short TTL (60 min default for verify, 30 min for reset).
    """
    __tablename__ = "auth_tokens"

    id:         Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # One-time verify / reset tokens. Ephemeral by design — CASCADE on
    # user delete tears the row down with the user (no orphan rows).
    user_id:    Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    purpose:    Mapped[str] = mapped_column(String(16), nullable=False)  # 'verify' | 'reset'
    token:      Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Watchlist — per-user named symbol groups for monitoring
# ---------------------------------------------------------------------------

class Watchlist(Base):
    """
    Watchlist owned by a single user OR shared globally (is_global=True,
    user_id=None). Operator-created lists are always per-user; the
    canonical 'Pinned' list is global — every user sees it, only
    admin / designated roles can mutate it.
    """
    __tablename__ = "watchlists"
    # The unique constraint allows multiple users to have the same name
    # (e.g. each user's own "test"). For global rows user_id is NULL and
    # the partial index below (created in the seeder) enforces a single
    # global Pinned. Postgres treats NULL as distinct in standard UNIQUE
    # so the (NULL, "Pinned") constraint doesn't collide with itself.
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_watchlist_user_name"),)

    id:         Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # user_id is NULL when the row is global (shared across all users).
    user_id:    Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    name:       Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # is_default flags the user's primary watchlist. UI uses this to pick
    # which list a "+ Watch" affordance on /admin/options adds to.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # is_pinned flags lists whose contents land in the "Pinned" major group
    # on Market Pulse (top of the unified grid).
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # is_global=True means this row is shared across every user. Only
    # admin / designated roles can mutate it; everyone else reads it.
    is_global: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class WatchlistItem(Base):
    """
    Single symbol inside a watchlist. Market data only — no qty / pnl
    fields; the quotes endpoint fetches LTP / bid / ask / day-change
    on demand via the broker.
    """
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint(
        "watchlist_id", "exchange", "tradingsymbol",
        name="uq_watchlist_item_unique",
    ),)

    id:            Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    watchlist_id:  Mapped[int] = mapped_column(
        Integer, ForeignKey("watchlists.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange:      Mapped[str] = mapped_column(String(8),  nullable=False)
    # Optional operator-supplied display name. Operator can label a
    # tradeable contract (e.g. CRUDEOIL26JUNFUT) with the underlying
    # nickname they actually think in (e.g. "Crude oil"). The raw
    # tradingsymbol still drives quotes / orders; alias only affects
    # display in the watchlist grid + tooltips.
    alias:         Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sort_order:    Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added_at:      Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Algo — chase orders and events
# ---------------------------------------------------------------------------
# Strategy — the unit of attribution. Every AlgoOrder + every internal
# lot ledger entry ties back here, so per-strategy P&L can be computed
# without re-deriving the bucket every time. v1 model is intentionally
# thin (id, slug, name, owner, capacity_cap, target_vol, active).
# Subsequent slices add `parent_strategy_id` for sub-strategies and a
# `risk_budget_inr` for the risk officer's allocation view.
# ---------------------------------------------------------------------------

class StrategyLot(Base):
    """One entry in the per-strategy FIFO lot ledger. Opens on a
    BUY fill (long lot) or a SELL fill (short lot) attributed to a
    strategy; closes when a counter-direction fill consumes it via
    `close_lot_fifo()`.

    Why this exists — broker reports NET positions per account, but
    the strategy attribution layer needs the LOT-LEVEL trail so a
    partial close of "100 long + 100 long" cleanly debits the older
    100 in P&L terms. Without the ledger, two strategies sharing the
    same symbol on the same account would have their fills aggregated
    by the broker and the realised-pnl-by-strategy view would be
    mathematically wrong.

    The ledger is the source of truth for per-strategy P&L; the
    Strategy.realised_pnl rollup queries SUM(realized_pnl) here, not
    AlgoOrder.pnl.

    Lifecycle:
      OPEN  — created on fill; remaining_qty == qty.
      PARTIAL — close_lot_fifo consumed some qty; remaining_qty > 0.
      CLOSED — remaining_qty == 0; closed_at + realized_pnl populated.

    Industry analogue: Interactive Brokers' Trader Workstation
    "Tax Lot" ledger; Bloomberg PRM's allocation buckets.
    """
    __tablename__ = "strategy_lots"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    strategy_id: Mapped[int]     = mapped_column(
        Integer, ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # The AlgoOrder that opened this lot. ON DELETE SET NULL so the
    # operator can delete an order row (rare; audit reasons) without
    # corrupting the ledger.
    open_order_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("algo_orders.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # Account + symbol denormalised so the FIFO match query doesn't
    # have to join algo_orders. Indexed for the `(account, symbol)`
    # lookup that `close_lot_fifo()` runs on every closing fill.
    account: Mapped[str]         = mapped_column(String(32), nullable=False)
    symbol: Mapped[str]          = mapped_column(String(64), nullable=False)
    exchange: Mapped[str]        = mapped_column(String(8),  nullable=False)
    # Direction of the lot: 'B' = long (opened via BUY), 'S' = short
    # (opened via SELL). The closing-side direction is implicit (an
    # opposite-side fill closes the lot).
    side: Mapped[str]            = mapped_column(String(1), nullable=False)
    qty: Mapped[int]             = mapped_column(Integer, nullable=False)
    remaining_qty: Mapped[int]   = mapped_column(Integer, nullable=False)
    open_price: Mapped[float]    = mapped_column(Numeric(12, 4), nullable=False)
    # Average closing price across every partial that consumed this
    # lot. NULL while OPEN; weighted-average once any qty closes.
    close_price: Mapped[Optional[float]] = mapped_column(Numeric(12, 4), nullable=True)
    # Cumulative realised pnl across every close against this lot.
    # Stays at 0 while OPEN; sums up the per-partial pnl ((close -
    # open) × qty_closed × sign) as closes happen.
    realized_pnl: Mapped[float]  = mapped_column(
        Numeric(14, 2), nullable=False, default=0.0, server_default="0.0",
    )
    opened_at: Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    __table_args__ = (
        # FIFO match query — find the oldest open lot for this
        # (strategy, account, symbol, side). Indexed composite makes
        # this O(log n) instead of O(rows in strategy_lots).
        Index("ix_strategy_lots_open",
              "strategy_id", "account", "symbol", "side", "remaining_qty",
              "opened_at"),
    )


class NavDaily(Base):
    """Daily firm-level NAV snapshot. Written by `_task_nav_compute`
    at 16:00 IST (15 min after the per-strategy snapshot task) so the
    day's broker positions + funds are settled.

    NAV calculation (v1, firm-aggregate):

        cash_total      = Σ available_margin + cash across all accounts
        positions_mtm   = Σ quantity × last_price for every open position
                          (long > 0, short < 0 → naturally signed)
        holdings_mtm    = Σ quantity × last_price for every equity holding
        nav             = cash_total + positions_mtm + holdings_mtm

    Stored in ₹ (no normalisation — Cat-III AIF reporting is INR-
    denominated). Unique on `as_of_date` so manual re-runs (operator
    triggers via /admin/exec or background task restart) upsert cleanly.

    Per-investor slicing is a separate slice — this table holds the
    firm-aggregate number; the slice helper combines it with the
    User table's `share_pct` to compute each LP's portion of NAV.

    SEBI Cat-III audit horizon is 8 years — keep this table forever;
    cleanup is unnecessary (~365 rows per year, ~3000 rows lifetime).
    """
    __tablename__ = "nav_daily"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    as_of_date: Mapped[datetime] = mapped_column(Date, nullable=False, unique=True, index=True)
    nav: Mapped[float]           = mapped_column(Numeric(18, 2), nullable=False)
    cash_total: Mapped[float]    = mapped_column(Numeric(18, 2), nullable=False, default=0.0)
    positions_mtm: Mapped[float] = mapped_column(Numeric(18, 2), nullable=False, default=0.0)
    holdings_mtm: Mapped[float]  = mapped_column(Numeric(18, 2), nullable=False, default=0.0)
    # Snapshot of which accounts contributed — JSON list of broker
    # account codes that the daily aggregate covered. Forensic: if
    # an account was offline when the snapshot ran, it's noted here
    # so the row's NAV is interpretable.
    accounts_snapshot: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    # Free-form note (e.g. "broker outage 14:30-15:45 — DH3747 excluded").
    # Operator-editable via /admin/exec → "Manual NAV adjustment" surface
    # (slice 7k).
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class StrategySnapshot(Base):
    """Daily roll-up per strategy. One row per (strategy_id, as_of_date).
    Written by the 15:45 IST background task — captures the day's
    realised + unrealised P&L + open notional so the per-strategy P&L
    chart on /strategies/{slug} can plot a time series without
    re-aggregating the full lot ledger on every page load.

    The snapshot daemon runs at 15:45 IST (5 min after NSE equity
    close) so the day's closes are settled. MCX positions still have
    unrealised exposure overnight; that's captured in the next day's
    snapshot at 15:45 the following day. Acceptable lag for a chart
    that's labelled "EOD" anyway.

    Slice 7a ships the table; slice 7b wires the background task +
    chart UI.
    """
    __tablename__ = "strategy_snapshots"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    strategy_id: Mapped[int]     = mapped_column(
        Integer, ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    as_of_date: Mapped[datetime] = mapped_column(
        Date, nullable=False, index=True,
    )
    open_lots_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_notional:   Mapped[float] = mapped_column(Numeric(18, 2), nullable=False, default=0.0)
    realised_pnl:    Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0.0)
    unrealised_pnl:  Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0.0)
    margin_allocated: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("strategy_id", "as_of_date", name="uq_strategy_snap_pair"),
        # ix_strategy_snapshots_date (singleton as_of_date index) was
        # dropped in slice T-8d — the unique constraint on (strategy_id,
        # as_of_date) already covers every point-lookup. DROP in init_db.
    )


class Strategy(Base):
    """A named bucket for attribution. One trader can own multiple
    strategies; orders + lots tie back via `strategy_id`.

    Slug is the operator-facing stable identifier (used in URLs +
    backend audit log filters). Name is the human label that shows
    on dropdowns + P&L surfaces. `owner_user_id` controls who can
    edit the strategy via the cap matrix's `manage_own_strategies`
    cap; the trader user must also have this strategy in their
    `assigned_strategies` list for execution / write privileges.

    Slug uniqueness is firm-wide — a fund has at most one strategy
    called `nifty-mean-reversion`, regardless of owner. Indexed
    lookups by slug from the UI's strategy picker + by owner_user_id
    from the per-user strategy list.
    """
    __tablename__ = "strategies"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    slug: Mapped[str]            = mapped_column(String(40), unique=True, nullable=False, index=True)
    name: Mapped[str]            = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Owning user — drives the manage_own_strategies cap check. NULL
    # for firm-managed strategies (admin-only) — every other case
    # carries the trader's user_id.
    owner_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # Notional cap — operator-defined ceiling on open position
    # notional for this strategy. Risk officers use it to enforce
    # per-strategy budgets without touching account-level margin.
    # NULL = no cap.
    capacity_cap_inr: Mapped[Optional[float]] = mapped_column(Numeric(18, 2), nullable=True)
    # Target volatility (annualised, decimal fraction — 0.15 = 15%).
    # Informational for the per-strategy NAV view; used in slice 7's
    # vol-targeting sizing helper. NULL = no target.
    target_volatility: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool]      = mapped_column(Boolean, nullable=False, default=True, server_default="true")
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

class AlgoOrder(Base):
    __tablename__ = "algo_orders"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    account: Mapped[str]         = mapped_column(String(32), nullable=False)
    symbol: Mapped[str]          = mapped_column(String(64), nullable=False)
    exchange: Mapped[str]        = mapped_column(
        String(8), nullable=False, default="NFO", server_default="NFO",
    )
    transaction_type: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY/SELL
    quantity: Mapped[int]        = mapped_column(Integer, nullable=False)
    # Sprint B (audit #4) — cumulative filled qty across partials.
    # 0 → nothing filled yet. On chase terminal-fill this equals
    # `quantity`. On partial fills it accumulates so downstream
    # readers know how much actually traded vs the unfilled
    # residual. Distinct from `quantity` (the ORIGINAL ask) so the
    # template-attach path can size exit GTTs against the actual
    # filled amount, not over-size.
    filled_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    initial_price: Mapped[float] = mapped_column(Float, nullable=True)
    # Audit fix (M-6) — the chase loop's cancel-and-replace updates
    # the broker's limit price every iteration, but pre-fix the only
    # record was the `detail` text ("chase #2 limit=₹181"). The
    # ChaseCard rendered `initial_price` (the FIRST attempt's price)
    # which read as misleadingly stale after 3 iterations. Now the
    # chase loop writes `current_limit` on every `_sync_algo_order_id`
    # so the UI can show the live re-quoted price. NULL when the
    # chase has never re-quoted (still at initial_price).
    current_limit: Mapped[float] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float]    = mapped_column(Float, nullable=True)
    attempts: Mapped[int]        = mapped_column(Integer, nullable=False, default=0)
    slippage: Mapped[float]      = mapped_column(Float, nullable=True)
    status: Mapped[str]          = mapped_column(String(16), nullable=False, default="OPEN", index=True)
    engine: Mapped[str]          = mapped_column(
        String(16), nullable=False, default="manual", server_default="manual",
    )  # sim/paper/live/replay/shadow/expiry/manual
    mode: Mapped[str]            = mapped_column(String(8), nullable=False, default="live", index=True)  # sim/paper/live/replay/shadow
    # Which agent originated this order. NULL for rows written before this
    # column existed (pre-migration), and for orders whose origin can't be
    # determined (e.g. legacy /place path that predates the manual agent).
    agent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # Indexed — chase.py terminal events (fill / unfill / chase_failed)
    # query `WHERE broker_order_id = ?` on every real broker fill, and
    # postback handlers in routes/orders.py do the same. Without the
    # index that's a seq-scan on a growing append-only table.
    broker_order_id: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, index=True,
    )
    # Audit cross-reference — the request UUID stamped by AuditMiddleware
    # for the HTTP request that created this row. Lets /admin/history
    # link each Orders row directly to /admin/audit?request_id=…
    # showing the full forensic context (actor, response status, full
    # path, downstream rows that share the same request). Indexed for
    # the drill-through filter.
    request_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True,
    )
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expiry_date: Mapped[Optional[datetime]] = mapped_column(Date, nullable=True)
    # Auto profit-target — set once at order creation; engine arms a child
    # TP order on fill.  Exactly one of target_pct / target_abs is set
    # (or both may be None to opt out of the TP feature).
    target_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_abs: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # parent_order_id links a take-profit child row back to the originating
    # parent AlgoOrder.  NULL on parent rows; set on every TP child so
    # idempotency checks can skip duplicate TP creation.
    parent_order_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("algo_orders.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    # Template attachment — captured at submit time so the postback
    # handler can fire apply_plan_live(template, actual_fill_price)
    # when the parent flips to FILLED. Without this column the LIVE
    # path computed a TemplatePlan at submit-time but never sent it
    # to the broker, so the operator had no GTTs / wings attached even
    # though the UI showed a template was picked.
    template_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("order_templates.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # Parent product code — needed at template-attach time so exit
    # GTTs inherit the right product. NRML for F&O / commodity carry,
    # MIS for intraday equity / F&O, CNC for cash-equity delivery.
    # Without this column the postback handler defaulted every exit
    # leg to NRML, which Kite rejects on MIS equity day-trades after
    # 3:20 PM (auto-square-off) and silently leaves operator with
    # unwanted overnight positions.
    product: Mapped[str] = mapped_column(
        String(8), nullable=False, default="NRML", server_default="NRML",
    )
    # JSON list of {broker, gtt_id, label} dicts returned by
    # apply_plan_live after the parent fills. Used to cancel the
    # attached GTTs when the parent is manually closed and for
    # /admin/templates audit reporting. NULL means "no attach yet"
    # (parent not yet filled, or no template was picked).
    attached_gtts_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Per-order template parameter overrides — JSON dict with keys
    # tp_pct / sl_pct / wing_premium_pct / wing_strike_offset. Set
    # by the basket / ticket route when the operator tweaks the
    # shell-level "On fill" inputs at submit. The postback handler
    # reads it back and passes it as `overrides` to
    # apply_template_to_order so the actual GTTs reflect the
    # operator's per-submit tweaks even though the template row
    # itself stays untouched. NULL when no overrides were supplied.
    template_overrides_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # basket_tag groups the legs of a single basket submission.  Carried
    # through to kite.place_order(tag=…) so the broker also groups them.
    basket_tag: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Chase timing fields — written by _sync_algo_order_id on every
    # cancel-and-replace so the chase panel can display a live countdown
    # to the next re-quote. Both are Unix epoch seconds (float). NULL
    # on rows created before this migration and on non-chased orders.
    last_attempt_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    next_attempt_at: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Configured interval between re-quotes (seconds). Persisted so the
    # UI countdown doesn't need to re-derive it from /admin/settings.
    interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Slice 6 — attribution v1. Optional foreign key to strategies.id.
    # Nullable so existing rows (created before the slice 6 deploy)
    # keep validating without a backfill, and so the order-place path
    # can omit it during operator-driven manual entry while the picker
    # UI rolls out. The chase + agent-fire paths populate it from the
    # parent agent's strategy_id (when set) so attribution flows
    # automatically through automated orders. Tightening to NOT NULL
    # happens after slice 7 ships the lot ledger.
    strategy_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("strategies.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # Sprint E (audit #7) — composite (mode, status) index. The trail-
    # stop poller, OCO pair-watcher, and paper recovery all hit
    # `WHERE mode = ? AND status = ?` on every cycle. With only the
    # single-column indexes Postgres uses one and filters in memory;
    # the composite lets it drop straight to the matching rows. ~µs
    # impact on the small dev DB but matters as algo_orders grows.
    __table_args__ = (
        Index("ix_algo_orders_mode_status", "mode", "status"),
        Index("ix_algo_orders_account_symbol", "account", "symbol"),
    )


class AlgoEvent(Base):
    __tablename__ = "algo_events"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    algo_order_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("algo_orders.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    event_type: Mapped[str]      = mapped_column(String(32), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class AlgoOrderEvent(Base):
    """Append-only per-order timeline. One row per state transition so
    operators have a full audit trail (placed → chase_modify × N → fill /
    unfill / reject) without overwriting AlgoOrder.detail."""
    __tablename__ = "algo_order_events"

    id: Mapped[int]       = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("algo_orders.id", ondelete="CASCADE"), nullable=False, index=True
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
    # `slug` is the short, unique, machine-friendly identifier (e.g.
    # "loss-positions-total"). Used in URLs + API paths + condition
    # references. Stable across renames.
    slug: Mapped[str]            = mapped_column(String(64), unique=True, nullable=False, index=True)
    # `name` is the human-readable display name shown in alerts +
    # in the agent list (e.g. "Positions total loss guardrail").
    name: Mapped[str]            = mapped_column(String(128), nullable=False)
    # `long_name` is a structured 3-part descriptor encoding the
    # agent's condition / alert / action profile, separated by ` - `.
    # Example: "positions-total-loss-thresholds - critical-multi - alert-only"
    # Format helps operators scan a long agent list and immediately see
    # what each agent does without expanding the row. Optional for
    # custom agents (the editor surfaces it as a field), required for
    # every built-in agent.
    long_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
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

    # Time-of-day gate. When set ("HH:MM" 24-hour IST), the agent only
    # evaluates / fires during a small window around this wall-clock
    # time once per IST date — useful for "fire at 14:30 IST every
    # expiry day" close-position agents, EOD summaries, etc.
    # NULL = no time gate (legacy behaviour — evaluated every tick).
    # Stored as a string so DB doesn't need timezone-aware TIME WITH
    # ZONE handling; engine parses "HH:MM" at evaluation time.
    fire_at_time: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)

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

    # ── Alert hierarchy / noise reduction (Sprint 1, May 2026) ─────────
    #
    # tier   — severity bucket. critical / high / medium / low.
    #          Drives topic-scoped suppression: if a higher-tier agent
    #          fires for the same topic, lower-tier agents are logged
    #          as suppressed and skipped (no push notification).
    # topic  — freeform tag. Agents with the same topic are siblings —
    #          one tier wins, others are suppressed. Operator decides
    #          which agents are "about the same thing"; default
    #          'general' means no topic-suppression.
    # digest_window_sec — buffer outgoing dispatches in N-sec windows
    #          and send ONE consolidated alert message per window per
    #          channel. 0 = fire immediately. 30s default keeps a
    #          market-crash burst to a single push.
    tier: Mapped[str]            = mapped_column(
        String(16), nullable=False, default="medium",
    )
    topic: Mapped[str]           = mapped_column(
        String(64), nullable=False, default="general",
    )
    digest_window_sec: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30,
    )

    # ── Debounce (Phase 21) — "fire only when condition holds for N
    #    consecutive minutes." Eliminates spike-driven false positives:
    #    a single tick where pnl_pct dips to -2.1% from a Kite quote
    #    glitch no longer trips a 30-min cooldown.
    #
    #    Semantics:
    #      debounce_minutes = 0 (default) — fire immediately on first
    #                          true evaluation. Backwards-compatible.
    #      debounce_minutes = N — record the timestamp of the FIRST
    #                          true evaluation (in condition_first_true_at);
    #                          on subsequent ticks, fire only when
    #                          (now - first_true_at) ≥ N minutes AND
    #                          condition is still true. ANY false
    #                          evaluation resets first_true_at to NULL.
    #
    #    Industry analogue: Datadog `For:`, Grafana `For:`, CloudWatch
    #    `EvaluationPeriods` — universal in production rule engines.
    debounce_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    condition_first_true_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # ── Phase 22: tagging + quiet hours ─────────────────────────────
    #
    # tags — free-form labels for filtering on /agents. Industry analogue:
    #   Datadog tags, Grafana labels. Empty list = untagged. Operators
    #   can group agents by strategy ("iron-condor", "nifty"), by lifecycle
    #   ("review-q3", "draft"), or anything else. Tags are read on the
    #   /agents page filter chips and via the MCP `list_agents(tag=...)`
    #   path.
    tags: Mapped[list]           = mapped_column(JSONB, nullable=False, default=list)
    # blackout_windows — list of {"start": "HH:MM", "end": "HH:MM"} entries
    #   in IST. When the current wall-clock IST time is INSIDE any window,
    #   the engine skips this agent in run_cycle. Use for "no alerts during
    #   12:00-13:00 lunch" or "muted during scheduled deploy".
    #   Crossing-midnight windows ({"start":"23:00","end":"01:00"}) supported.
    #   Industry analogue: Datadog `mute_until`, PagerDuty maintenance windows,
    #   Grafana silences.
    blackout_windows: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

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
    # Indexed — alerts/agents/logs pages all filter by agent_id; without
    # the index every page load was a seq-scan on a growing append-only
    # table.
    agent_id: Mapped[int]        = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    event_type: Mapped[str]      = mapped_column(String(32), nullable=False)
    trigger_condition: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The (sim_mode, agent_id, timestamp DESC) composite index in init_db
    # covers every query that previously used a singleton sim_mode index.
    # index=False here so SQLAlchemy's metadata.create_all (new installs)
    # doesn't also create the now-redundant singleton; the composite is
    # created explicitly in init_db regardless.
    sim_mode: Mapped[bool]       = mapped_column(
        Boolean, nullable=False, default=False, index=False,
    )
    # Indexed — every list query orders by timestamp DESC and most filter
    # by a recent time range.
    timestamp: Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


class BrokerConnectionEvent(Base):
    __tablename__ = "broker_connection_events"

    id:         Mapped[int]            = mapped_column(primary_key=True, autoincrement=True)
    account:    Mapped[str]            = mapped_column(String(32), nullable=False, index=True)
    broker_id:  Mapped[str]            = mapped_column(String(32), nullable=False)
    event_type: Mapped[str]            = mapped_column(String(32), nullable=False, index=True)
    event_ts:   Mapped[datetime]       = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    detail:     Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


# ---------------------------------------------------------------------------
# Sim iteration — one row per simulator iteration. A single `/start` call
# with `iterations: N` creates N rows, all sharing the same `parent_run_id`
# (the SimIteration row of iteration 1). Stats land in `summary_json` at
# the end of each iteration so reports survive a `/clear` wipe of the
# detailed event/order rows.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sim recording — deterministic event log for "replay this sim run later".
#
# When the operator runs a sim with record_mode=true, SimDriver buffers
# every state-mutating event (tick, position add/close, GTT lifecycle,
# chase fill, agent fire) with a relative timestamp. On sim stop the
# buffer flushes to one row here. SimReplayDriver consumes the row to
# re-emit the events at configurable speed — the operator's screen
# looks identical to the original run.
#
# Industry analogue: NinjaTrader Market Replay binary, IBKR session log
# replay. Event-stream recording (not input-only) keeps replay
# deterministic regardless of code changes between record + replay.
# ---------------------------------------------------------------------------

class SimRecording(Base):
    __tablename__ = "sim_recordings"

    id: Mapped[int]            = mapped_column(primary_key=True, autoincrement=True)
    # Operator-supplied free-form label ("NIFTY -3% with Default-Bull").
    label: Mapped[str]         = mapped_column(String(160), nullable=False, default="")
    scenario: Mapped[Optional[str]]   = mapped_column(String(64), nullable=True)
    seed_mode: Mapped[Optional[str]]  = mapped_column(String(32), nullable=True)
    # Wall-clock timestamps of the recording window. duration_sec is
    # ended_at - started_at; surfaced as a column so the Replays tab
    # doesn't have to compute it client-side.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    duration_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tick_count: Mapped[int]    = mapped_column(Integer, nullable=False, default=0)
    event_count: Mapped[int]   = mapped_column(Integer, nullable=False, default=0)
    # Event log — list of {t, kind, payload} dicts. Compact at ~150 KB
    # per ~10-min sim; bounded by SimDriver's tick rate * duration.
    payload: Mapped[dict]      = mapped_column(JSONB, nullable=False, default=dict)
    owner_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class SimIteration(Base):
    __tablename__ = "sim_iterations"

    id: Mapped[int]                = mapped_column(primary_key=True, autoincrement=True)
    slug: Mapped[str]              = mapped_column(String(64), nullable=False, unique=True, index=True)
    # First iteration of a multi-run; iteration 1 references itself.
    parent_run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("sim_iterations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    iteration_index: Mapped[int]   = mapped_column(Integer, nullable=False, default=1)
    iterations_total: Mapped[int]  = mapped_column(Integer, nullable=False, default=1)
    regime: Mapped[str]            = mapped_column(String(64), nullable=False)
    seed: Mapped[Optional[int]]    = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime]   = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 'book_empty' | 'time_limit' | 'scenario_complete' | 'stopped' | 'failed'
    end_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Full start() params as JSON — used by /replay to reconstruct the run
    params_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # End-of-iteration stats: P&L per account, # agent fires, # orders, etc.
    summary_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


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
# Agent templates — reusable saved condition/notify/action sub-trees
# ---------------------------------------------------------------------------
#
# An AgentTemplate is a saved JSONB body that an agent can reference inline via
# `{"$ref": "<name>"}`. Two kinds today:
#
#   notify — saves an events list (channels + enabled flags). Used in
#            place of (or inside) `Agent.events`. When N agents share
#            the same telegram+email+log trio, they all reference
#            `notify-critical-trio` instead of each carrying their own
#            three-row copy.
#
#   condition — saves a condition sub-tree (a leaf, an `any/all/not`
#            block, or a full tree). Referenced from inside an agent's
#            `conditions` field via `{"$ref": "loss-positions-default"}`.
#            Stage 2 — Stage 1 lands notify only.
#
# System templates ship with the code and seed on every boot. Operators
# can deactivate but not delete them; custom templates have full CRUD.
#
# The evaluator dereferences refs lazily via the TemplateRegistry
# singleton. Cycles (A refs B refs A) are detected with a visited set
# and surface as a warning + no-fire — same graceful path as missing
# tokens in the existing grammar pipeline.
#
# Renamed from AgentFragment in v2.1 alongside fragment_registry.py
# → template_registry.py. The DB table keeps its historical name
# `agent_fragments` so existing rows survive the rename without
# requiring a migration; the Python class + module paths use the new
# vocabulary.

class AgentTemplate(Base):
    __tablename__ = "agent_fragments"   # historical — table rename pending

    id: Mapped[int]           = mapped_column(primary_key=True, autoincrement=True)
    # 'condition' or 'notify'. Reserved 'action' for a future stage.
    kind: Mapped[str]         = mapped_column(String(16), nullable=False, index=True)
    # Slug-style name — what callers write inside the $ref. Lowercase,
    # hyphenated. Unique per kind so a notify fragment and a condition
    # fragment can share a name without colliding.
    name: Mapped[str]         = mapped_column(String(128), nullable=False)
    # The actual content — for notify: list of {channel, enabled} dicts;
    # for condition: a condition sub-tree (leaf or composite).
    body: Mapped[dict]        = mapped_column(JSONB, nullable=False)
    description: Mapped[str]  = mapped_column(Text, nullable=False, default="")
    # System fragments seed from code on every boot. Operators can
    # toggle is_active but never delete.
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
        UniqueConstraint('kind', 'name', name='uq_agent_fragment'),
    )


# ---------------------------------------------------------------------------
# Order templates — exit-rule presets attached at OrderTicket submit time.
#
# An OrderTemplate carries the TP / SL / Wing config the operator wants
# applied to every order using it. At submit:
#   - the entry order goes to the broker
#   - the template's TP / SL translate to a broker-native GTT
#   - the template's Wing (SELL options only) becomes a paired basket leg
#
# The model is intentionally flat (one row per template) because templates
# are a closed vocabulary — TP%, SL%, Wing%. Future fields (trailing-stop
# rules, time-based exits) join as nullable columns.
#
# Industry analogue: NinjaTrader ATM Strategy template. Operator picks one
# from a dropdown at order entry; saved templates are reusable + bulk-editable.
# ---------------------------------------------------------------------------

class OrderTemplate(Base):
    __tablename__ = "order_templates"

    id: Mapped[int]            = mapped_column(primary_key=True, autoincrement=True)
    # Stable identifier — system templates use a short slug ("default-bull").
    # Operator templates leave slug NULL and use `name` as the visible label.
    slug: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)
    name: Mapped[str]          = mapped_column(String(128), nullable=False)
    description: Mapped[str]   = mapped_column(Text, nullable=False, default="")
    # 'buy_any'      — applies to BUY orders (any instrument)
    # 'sell_option'  — applies to SELL option orders (CE / PE)
    # 'both'         — applies to any order side
    applies_to: Mapped[str]    = mapped_column(String(16), nullable=False, default="both")
    # TP/SL/Wing — nullable so a "no TP, just SL" template is expressible.
    # Values are signed percentages: +30.0 means tp at fill*1.30, -20.0
    # means sl at fill*0.80.
    tp_pct: Mapped[Optional[float]]   = mapped_column(Numeric(8, 4), nullable=True)
    sl_pct: Mapped[Optional[float]]   = mapped_column(Numeric(8, 4), nullable=True)
    # Two alternative ways to size a protective wing: by % of the SELL
    # premium (e.g. 10.0 → buy a wing whose premium is ~10% of the short
    # leg's premium) OR by strike offset (e.g. 500 → buy a wing at strike
    # +500 for CE, -500 for PE). One of the two; never both.
    wing_premium_pct: Mapped[Optional[float]]   = mapped_column(Numeric(8, 4), nullable=True)
    wing_strike_offset: Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    # Order type fired by the TP GTT when its trigger crosses. Default
    # 'LIMIT' matches the historical behaviour. 'MARKET' lets operators
    # express "take what the book gives me at +X% — don't risk a missed
    # fill". NinjaTrader / IBKR Bracket Order both let the operator
    # pick this; ours mirrored that. SL legs stay LIMIT — a MARKET SL
    # is functionally a stop-market which we surface via a separate
    # field if needed in Phase 3.
    tp_order_type: Mapped[str] = mapped_column(
        String(8), nullable=False, default="LIMIT",
        server_default="LIMIT",
    )
    # Scale-out targets (Phase 3A). JSON list of {at_pct, close_pct}
    # entries — e.g. [{"at_pct": 30, "close_pct": 50},
    #                  {"at_pct": 60, "close_pct": 50}] means "close 50 %
    # of the position at +30 % gain, the remaining 50 % at +60 %".
    # When set, supersedes tp_pct (operator builds the full TP ladder
    # here). N separate single GTTs are placed at submit time, one
    # per scale, each for its fraction of parent_qty. Sum of close_pct
    # must be <= 100 (validated at PATCH/POST); the remainder stays
    # open with no TP. Industry analogue: NinjaTrader ATM Strategy
    # multiple targets, IBKR Bracket Order auto-scale.
    tp_scales_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Trailing stop distance (Phase 3B). When set, the background
    # `_task_trail_stop` poller watches every attached SL GTT and
    # ratchets its trigger toward the highest LTP seen (long parent)
    # or lowest LTP seen (short parent). New trigger = high × (1 −
    # sl_trail_pct/100) for longs, low × (1 + sl_trail_pct/100) for
    # shorts. Trigger only moves favorably — never against the
    # operator. Locks in profits as the underlying runs without
    # forcing the operator to manually drag the stop. Industry
    # standard "trailing stop" — NinjaTrader Trail, IBKR Trailing
    # Stop, Kite GTT modify cadence. Independent of sl_pct: if both
    # are set, sl_pct is the floor (trail never pulls trigger lower
    # than the initial sl_pct level on a long).
    sl_trail_pct: Mapped[Optional[float]] = mapped_column(
        Numeric(8, 4), nullable=True,
    )
    # Marks the operator's default pick — surfaced in OrderTicket as the
    # pre-selected option. Only one default per applies_to scope; the
    # seeder enforces a single is_default=True row for each scope.
    is_default: Mapped[bool]  = mapped_column(Boolean, nullable=False, default=False)
    # System templates ship from code on every boot. Operators can edit
    # values (tp_pct / sl_pct / wing_*) but cannot delete them — same
    # contract as AgentFragment system rows.
    is_system: Mapped[bool]   = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool]   = mapped_column(Boolean, nullable=False, default=True)
    # NULL for system templates (no owner). Operator-created templates
    # carry their owner so /api/admin/templates filters per-user when
    # the route layer cares.
    owner_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
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
        UniqueConstraint('owner_user_id', 'name', name='uq_order_template_per_owner'),
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
    exchange: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    qty: Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    avg_cost: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)
    ltp: Mapped[Optional[float]]      = mapped_column(Numeric, nullable=True)
    day_pnl: Mapped[Optional[float]]  = mapped_column(Numeric, nullable=True)
    total_pnl: Mapped[Optional[float]] = mapped_column(Numeric, nullable=True)
    # Frozen first-write per (date, account, kind, symbol). Captures
    # Kite's close_price at the first snapshot of each trading day —
    # which equals yesterday's official settlement before Kite overwrites
    # it at EOD. COALESCE in the UPSERT ensures subsequent writes never
    # overwrite a non-NULL value. Used by _positions_snapshot() to supply
    # a correct close_price during closed-hours reads instead of LTP.
    previous_close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
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
        # Sprint F (post-audit) — `_override_stale_close_from_snapshot`
        # (Sprint D) runs on every `/api/positions/` cache miss and
        # executes a `DISTINCT ON (account, symbol) ... ORDER BY
        # account, symbol, captured_at DESC` over kind='positions'.
        # With ~100 positions × multiple accounts that's a real query.
        # Composite supports the DISTINCT ON sort.
        Index(
            "ix_daily_book_kind_acct_sym_captured",
            "kind", "account", "symbol", "captured_at",
        ),
    )


class AdminEmailEvent(Base):
    """Audit row for admin/designated outbound emails to partners.
    One row per /api/admin/email-partners POST — captures who sent
    what to whom, when, and how many deliveries succeeded."""
    __tablename__ = "admin_email_events"

    id:               Mapped[int]      = mapped_column(primary_key=True, autoincrement=True)
    created_at:       Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,   # backs admin.py ORDER BY created_at DESC (slice T-8b)
    )
    actor_username:   Mapped[str]      = mapped_column(String(64), nullable=False, index=True)
    actor_role:       Mapped[str]      = mapped_column(String(32), nullable=False)
    # JSON array of usernames actually sent to (post-resolution of presets).
    recipients:       Mapped[list]     = mapped_column(JSONB, nullable=False, default=list)
    subject:          Mapped[str]      = mapped_column(String(256), nullable=False)
    # First 500 chars of body — full body NOT persisted to bound PII risk + DB size.
    body_preview:     Mapped[str]      = mapped_column(Text, nullable=False, default="")
    sent_count:       Mapped[int]      = mapped_column(Integer, nullable=False, default=0)
    failed_count:     Mapped[int]      = mapped_column(Integer, nullable=False, default=0)
    # First 200 chars of per-recipient failure notes; empty when all sent.
    failures_summary: Mapped[str]      = mapped_column(Text, nullable=False, default="")


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    # Account code (e.g. "ZG0790") — keep unique so the code paths that
    # already key on this string keep working without a per-row id rewrite.
    account: Mapped[str]         = mapped_column(String(32), unique=True, nullable=False)
    # Canonical vendor identifier. Matches the key in registry._ADAPTERS.
    # Values: "zerodha_kite" (canonical), "kite" (legacy YAML-seeded alias).
    # Future values: "upstox", "angel_one", "dhan", "fyers" etc.
    # Column width 32 to accommodate future multi-word identifiers.
    broker_id: Mapped[str]       = mapped_column(String(32), nullable=False, default="zerodha_kite")
    # api_key is plaintext — Kite API keys aren't a credentialing secret
    # (they pair with api_secret to authenticate, but the key alone leaks
    # nothing). Keeping it in the clear means it shows up unmasked in
    # admin UI lists, which is what operators expect.
    # Type TEXT (not VARCHAR(64)) because some vendors use long JWTs
    # as the api_key value — Groww in particular: their api_key is a
    # ~900-char JWT used as the Bearer header for the access-token mint
    # endpoint, not a short ID like Kite's. Kept TEXT to accommodate
    # any future vendor that does the same.
    api_key: Mapped[str]         = mapped_column(Text, nullable=False)
    # Fernet-encrypted; key derived from cookie_secret via HKDF.
    api_secret_enc: Mapped[str]  = mapped_column(Text, nullable=False)
    password_enc: Mapped[str]    = mapped_column(Text, nullable=False)
    totp_token_enc: Mapped[str]  = mapped_column(Text, nullable=False)
    # IPv6 source binding (Kite enforces one IP per app). Optional —
    # accounts without this fall back to the OS default route.
    source_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool]      = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Multi-broker fields (Sprint 2 prep, May 2026) ─────────────────
    # client_id — for brokers that authenticate by client_id + permanent
    #             access_token (Dhan-style) rather than the api_key /
    #             api_secret / TOTP flow Kite uses. Plaintext, like
    #             api_key (the client_id alone doesn't authenticate).
    # access_token_enc — Fernet-encrypted long-lived access token for
    #             Dhan-style brokers. Operators paste the token from
    #             the broker dashboard; we encrypt at rest and the
    #             adapter reads it during Connections.rebuild_from_db.
    # priority — fallback ordering for PriceBroker market-data calls.
    #            Lower = tried first. Lets the operator tune "if Kite
    #            rate-limits, hit Dhan next" without code changes.
    # extra_config — free-form JSON for per-broker tuning knobs
    #            (rate-limit overrides, custom endpoints, future
    #            adapter settings). Adapters read what they need;
    #            unknown keys are ignored.
    client_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    access_token_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    priority: Mapped[int]            = mapped_column(Integer, nullable=False, default=100)
    extra_config: Mapped[dict]       = mapped_column(JSONB, nullable=False, default=dict)
    # When True (default), this account is eligible for /api/options/historical
    # fallback. The historical endpoint walks the ordered list returned by
    # get_historical_brokers(); accounts with this flag False are skipped so
    # operators can reserve low-rate-limit accounts for order-flow only.
    historical_data_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default="true"
    )
    # ── Per-account poll priority (Dhan-only, Jul 2026) ───────────────────
    # poll_priority — controls how often the background 30s poller
    #   re-fetches positions/holdings/margins for THIS account.
    #   hot (default) = every 30s, warm = every 120s, cold = every 600s.
    #   Kite + Groww accounts ignore this field; it gates ONLY the Dhan
    #   background poll path.  Values: 'hot' | 'warm' | 'cold'.
    # auto_downgrade_enabled — when True the breaker-open history watcher
    #   will automatically drop this account to 'cold' after ≥5 breaker
    #   opens within a 15-min window.
    # auto_downgraded_at — epoch when the last auto-downgrade fired;
    #   NULL means the current poll_priority was set manually.
    # auto_downgrade_reason — human-readable cause string stamped at
    #   auto-downgrade time, e.g. "5 breaker opens in 15 min".
    poll_priority: Mapped[str] = mapped_column(
        String(8), nullable=False, default="hot", server_default="hot"
    )
    auto_downgrade_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    auto_downgraded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    auto_downgrade_reason: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    # ── Per-account circuit breaker opt-in (Jul 2026) ────────────────────
    # circuit_breaker_enabled — when True, the 3-fail / 5-min open-circuit
    #   breaker is fully active for this account.  When False (the default)
    #   the account still gets its last_ok_at / last_fail_at health stamps
    #   for the admin badge but the OPEN / HALF-OPEN state machine is
    #   bypassed so transient blips on one account never freeze the others.
    #   Currently opt-in only for DH6847; all other accounts stay at False
    #   unless the operator explicitly toggles via PATCH /api/admin/brokers/{id}.
    circuit_breaker_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # ── Display ordering (Jul 2026) ───────────────────────────────────────
    # display_order — canonical position for UI surfaces: dropdowns,
    #   health-badge chip popup, PerformancePage rows, dashboard tables,
    #   order-ticket pickers. Lower = shown earlier.
    #   Seeded at startup via _ensure_shared_broker_schema() one-shot
    #   migration (settings marker 'migrations.display_order_seeded_v1').
    #   Operator can adjust live via PATCH /api/admin/brokers/{id}.
    #   Default 500 so new/unknown accounts land in the middle, not last.
    display_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=500, server_default="500"
    )
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
# Research threads — chat transcripts persisted from MCP sessions on
# /admin/research. One row per "Research RELIANCE" session; the
# `transcript` JSONB carries the back-and-forth + tool calls so the
# operator can revisit the reasoning that led to a thesis. `draft_agent_id`
# links to the Agent row generated by the Drafts tab (NULL until promoted).
# ---------------------------------------------------------------------------

class AuditLog(Base):
    """Single forensic trail for every mutating HTTP request handled
    by the RamboQuant API. Lives alongside `mcp_audit` (which is
    MCP-specific) — together they cover both UI and LLM-initiated
    actions with a uniform shape.

    Populated by `AuditMiddleware` (backend/api/audit.py) on every
    successful 2xx POST / PATCH / PUT / DELETE response. Reads
    (GET) are intentionally NOT audited — the volume would dominate
    storage with no forensic value (a SEBI auditor wants to know
    WHAT CHANGED, not who looked at what).

    Schema is denormalised on purpose: actor_username + actor_role
    are SNAPSHOTTED at request time so a later role demotion doesn't
    rewrite history. `target_type` / `target_id` are best-effort
    structured fields (action middleware can fill them when the
    route's path parameter happens to be the target id); when
    absent the path itself is the only forensic identifier and
    that's fine for v1.

    `request_id` is a UUID injected by the middleware and surfaced
    in response headers + error logs so an audit row can be
    cross-referenced with the API log file for the full request
    trace.

    Industry analogue: SEBI Cat-III audit requirement (8-year
    retention); Bloomberg AIM action_log; Splunk UBA event stream.
    """
    __tablename__ = "audit_log"

    id: Mapped[int]               = mapped_column(primary_key=True, autoincrement=True)
    # Actor snapshot — captured at request time, never rewritten.
    actor_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    actor_username: Mapped[str]   = mapped_column(String(64), nullable=False, default="", index=True)
    actor_role:     Mapped[str]   = mapped_column(String(32), nullable=False, default="")
    # Action descriptor — derived from method + path. e.g. "POST /api/orders/ticket".
    # index=True (btree) removed — btree cannot serve leading-wildcard ilike
    # queries (ilike "%X%"). A GIN trigram index is created in init_db instead
    # when the pg_trgm extension is available; without it queries fall back to
    # seq-scan (correct result, no crash). See S7 migration block in database.py.
    action: Mapped[str]           = mapped_column(String(120), nullable=False)
    # Coarse category tag for filtering. Examples:
    #   'http'             — generic mutating HTTP request (middleware default)
    #   'order.place'      — order placement
    #   'order.fill'       — broker postback fill detail
    #   'order.modify'     — order modification
    #   'order.cancel'     — order cancellation
    #   'agent.action'     — agent engine fired an action
    #   'system.nav'       — NAV compute (background task)
    #   'system.statement' — monthly statement send (background task)
    #   'system.bootstrap' — auto-bootstrap synthetic event
    # Nullable for back-compat with rows written before the column
    # existed. Code paths writing new rows MUST set a category.
    category: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    method: Mapped[str]           = mapped_column(String(8),   nullable=False)
    # index=True removed in slice T-8e — no WHERE clause uses path as a
    # filter; the column was only accessed in ORDER BY / display, making
    # the btree index pure write amplification on a high-volume table.
    # DROP INDEX IF EXISTS ix_audit_log_path is in init_db (slice T block).
    path:   Mapped[str]           = mapped_column(String(255), nullable=False)
    # Optional structured target — captured when the path includes a
    # `/{target_id}` parameter and the middleware can identify the
    # target type from the path prefix. Best-effort; absent for
    # routes that don't follow the convention.
    target_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)
    target_id:   Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    # Outcome — HTTP status + optional summary (first 200 chars of
    # response body for JSON responses with a `detail` field).
    status_code: Mapped[int]      = mapped_column(Integer, nullable=False, index=True)
    summary:     Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Request correlation — UUID generated by middleware, mirrored in
    # response headers (`X-Request-ID`) so an operator can trace a
    # specific audit row back to the API log file.
    request_id:  Mapped[str]      = mapped_column(String(36), nullable=False, index=True)
    # Source attribution — IP for forensic reach. Stored unmasked so a
    # SEBI auditor can correlate by client.
    client_ip:   Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent:  Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # When the row was written. Indexed for date-range queries (the
    # primary audit UI is "what happened in the last N days").
    created_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), index=True,
    )

    __table_args__ = (
        # Composite for the common "actor's activity in a date range" query.
        Index("ix_audit_actor_date", "actor_user_id", "created_at"),
        Index("ix_audit_target",     "target_type",   "target_id"),
    )


class McpAudit(Base):
    """One row per MCP-initiated mutating call (Phase 3 place_order etc.).
    Captures who called what, with what args (redacted of any token
    material), and what happened. Read-only forensic trail — the
    Lab page's Audit tab will surface this in Phase 3+.

    Args are stored as JSONB so we can index into them for forensic
    queries (e.g. "every place_order in the last 24h for ZG0790")
    without parsing strings.
    """
    __tablename__ = "mcp_audit"

    id: Mapped[int]               = mapped_column(primary_key=True, autoincrement=True)
    tool: Mapped[str]             = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    args_redacted: Mapped[dict]   = mapped_column(JSONB, nullable=False, default=dict)
    result_status: Mapped[str]    = mapped_column(String(16), nullable=False, default="ok")
    result_summary: Mapped[str]   = mapped_column(Text, nullable=False, default="")
    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), index=True,
    )


# ---------------------------------------------------------------------------
# Visitor log — one row per unique (ip, UTC-date). Updated in-place when
# the same IP is seen again on the same day.  Populated nightly by
# backend/scripts/visitor_report.py after parsing /var/log/nginx/access.log.
# ---------------------------------------------------------------------------

class VisitorLog(Base):
    """One row per unique (ip, UTC-date). Updated in place if the IP is
    seen again on the same day."""
    __tablename__ = "visitor_log"

    id:            Mapped[int]            = mapped_column(primary_key=True, autoincrement=True)
    ip:            Mapped[str]            = mapped_column(String(45), nullable=False)               # IPv4 or IPv6
    seen_date:     Mapped[datetime]       = mapped_column(Date, nullable=False)                      # UTC date
    country:       Mapped[Optional[str]]  = mapped_column(String(2),   nullable=True)                # ISO-2 e.g. "IN"
    region:        Mapped[Optional[str]]  = mapped_column(String(8),   nullable=True)                # ISO subdivision e.g. "KA"
    city:          Mapped[Optional[str]]  = mapped_column(String(80),  nullable=True)
    asn:           Mapped[Optional[str]]  = mapped_column(String(32),  nullable=True)                # "AS9498" style
    request_count: Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime]       = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at:  Mapped[datetime]       = mapped_column(DateTime(timezone=True), nullable=False)
    last_path:     Mapped[Optional[str]]  = mapped_column(String(200), nullable=True)
    user_agent:    Mapped[Optional[str]]  = mapped_column(String(400), nullable=True)
    created_at:    Mapped[datetime]       = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_visitor_log_ip_date",   "ip", "seen_date", unique=True),
        Index("ix_visitor_log_seen_date", "seen_date"),
    )


class ResearchThread(Base):
    __tablename__ = "research_threads"

    id: Mapped[int]               = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str]           = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str]            = mapped_column(String(256), nullable=False, default="")
    thesis_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[str]       = mapped_column(String(16), nullable=False, default="unsure")  # bull / bear / neutral / unsure
    transcript: Mapped[list]      = mapped_column(JSONB, nullable=False, default=list)
    draft_agent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class HedgeProxy(Base):
    """Pair-only cross-reference between a HELD instrument
    (`proxy_symbol`) and an UNDERLYING root (`target_root`) it can hedge
    against. Conversion factor is ALWAYS derived dynamically at runtime
    from current LTPs (`factor = proxy_LTP / target_spot`); lot count
    comes from `effective_qty / target_lot_size` via the instruments
    cache. No tuning knobs to maintain.

    Operator: "to start with table can have goldm and gold, with
    goldbees cross reference, similarly silverm, silver and silverbees.
    the conversion is dynamic, the code should find it based units and
    market value and convert into option lots and qty."

    Schema simplified 2026-06-17 — the earlier Stage 2 shape with
    conversion_kind / static_factor / beta / correlation / kind /
    source columns was over-engineered for ETF tracking hedges (all
    conversions are dynamic, all correlations ~1.0 by construction).
    A one-time migration in `seed_hedge_proxies` DROPs the legacy
    table on next boot so the new schema lands cleanly.
    """
    __tablename__ = "hedge_proxies"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    proxy_symbol: Mapped[str]    = mapped_column(String(32), nullable=False, index=True)
    target_root: Mapped[str]     = mapped_column(String(32), nullable=False, index=True)
    is_active: Mapped[bool]      = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    # Stage 3 placeholder. For ETF tracking hedges (Stage 2, current
    # use case) this is always 1.0 — GOLDBEES → GOLD correlation is
    # ~1.0 by construction (the ETF's NAV mechanics force it). Reserved
    # for future stock-vs-index hedges (RELIANCE → NIFTY etc.) where
    # the value WOULD be auto-generated from a rolling regression of
    # daily returns (R² between proxy and target). Column exists so
    # the schema doesn't need a migration when Stage 3 lands.
    correlation: Mapped[float]   = mapped_column(Float, nullable=False, default=1.0,
                                                  server_default="1.0")
    # Stage 3 — regression slope from a rolling regression of proxy
    # daily returns vs target daily returns (`proxy_return = α + β ×
    # target_return + ε`). NULL → Stage 2 ETF case where β=1.0 by
    # construction. Populated by POST /api/admin/hedge-proxies/{id}/compute
    # on demand (Stage 3) and by a periodic background task (Stage 4).
    beta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    regression_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Sprint D — surfaces a failed regression run so the operator
    # can distinguish "computed 3 days ago OK" from "tried 3 days
    # ago, failed, won't retry until <max_age_days> elapses". Pre-fix
    # the daily background task wrote `regression_at = now()` even on
    # failure, so the freshness gate blocked retries silently for a
    # week. Schema: nullable string holding the last failure reason
    # (`"too few overlapping bars"`, `"symbol resolution failed"`,
    # `"broker timeout"`, ...); NULL means the last run succeeded.
    regression_error: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True,
    )
    # Annualised volatility (σ × √252) of the daily-return series used
    # for the regression. NULL until the regression has run successfully.
    # `target_sigma` is the one the operator typically reads (it tells
    # them how volatile the hedged underlying is); `proxy_sigma` is the
    # ETF / stock proxy's own vol, useful for sanity-checking
    # leveraged-ETF cases where β should be ~2-3 × the target's vol.
    target_sigma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    proxy_sigma:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)
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
        UniqueConstraint("proxy_symbol", "target_root", name="uq_hedge_proxy_pair"),
    )


class CodeMetricsSnapshot(Base):
    """Per-release codebase-health snapshot. Captured by
    `scripts/capture_metrics.py` either manually or from the deploy
    pipeline so the operator can watch eight cross-cutting health
    metrics trend across releases.

    Eight metrics — six numeric backend/frontend pairs plus bug_count
    and per_page_latency_ms — populated from radon, vulture, jscpd,
    pytest-cov, ESLint, the e2e perf spec, and `git log` heuristics.
    See `scripts/capture_metrics.py` for the exact tool→column wiring.

    Phase 1 deliberately omits decoupling (afferent/efferent coupling
    requires an import-graph build via pydeps or a custom AST walker —
    deferred to Phase 2). The `notes` column is the operator-facing
    free-text channel; `raw_payload` keeps the full tool stdout JSON
    for forensics ("why did duplicated_lines spike on v2.4?").

    Idempotency: `release_tag` is UNIQUE — the capture script logs +
    skips when a row for the same tag already exists unless `--force`
    is passed (the force path UPDATEs in place rather than INSERTing
    a second row, so the trend chart stays clean).
    """
    __tablename__ = "code_metrics_snapshots"

    id: Mapped[int]               = mapped_column(primary_key=True, autoincrement=True)
    # Release identifier. Usually `git describe --tags --abbrev=0`
    # (e.g. 'v2.1.0'); operator-triggered captures use
    # 'manual-YYYY-MM-DD'; dev-branch captures use 'dev-<short-sha>'.
    release_tag: Mapped[str]      = mapped_column(String(64), unique=True, nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), index=True,
    )
    # Commit hash captured at run time. Useful for reproducing a
    # snapshot (`git checkout <git_sha>` then re-run capture).
    git_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    # ── Backend metrics (Python — radon + vulture + pytest-cov) ────
    backend_loc:               Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    backend_complexity_avg:    Mapped[Optional[float]] = mapped_column(Float,   nullable=True)
    backend_complexity_max:    Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    backend_duplicated_lines:  Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    backend_stale_count:       Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    backend_coverage_pct:      Mapped[Optional[float]] = mapped_column(Float,   nullable=True)

    # ── Frontend metrics (JS/Svelte — jscpd + ESLint + wc) ─────────
    frontend_loc:              Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    frontend_complexity_avg:   Mapped[Optional[float]] = mapped_column(Float,   nullable=True)
    frontend_complexity_max:   Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    frontend_duplicated_lines: Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    frontend_stale_count:      Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    frontend_coverage_pct:     Mapped[Optional[float]] = mapped_column(Float,   nullable=True)

    # ── Cross-cutting ───────────────────────────────────────────────
    # Count of commits matching the bug-fix heuristic between the
    # previous release tag and this one (or the last 30 days for
    # 'manual-*'). See `_count_bug_commits` in capture script.
    bug_count_since_last_release: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Per-page latency dict: { "/pulse": {dcl: 221, idle: 4223, lcp: 2012}, ... }
    # JSONB so the schema doesn't churn when pages come and go. Empty
    # dict `{}` when the e2e spec hasn't been run yet (capture script
    # logs a warning + writes {}).
    per_page_latency_ms: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Test execution times. Populated when --with-test-times is passed to
    # the capture script (requires pytest-json-report for backend;
    # Playwright JSON reporter for frontend). Structure:
    # {
    #   "backend":  { "total_tests": N, "total_wall_time_s": F,
    #                 "median_s": F, "max_s": F,
    #                 "top_10_slowest": [{"name": "...", "duration_s": F}, ...],
    #                 "slow_count": N, "slow_threshold_s": 1.0 },
    #   "frontend": { same shape or {"_skipped": "..."} }
    # }
    # NULL when not yet captured (first snapshot, or capture run without
    # --with-test-times). {} when the flag was passed but the JSON output
    # was not found.
    test_response_times: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Operator-facing free-text channel for release notes / context.
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Full tool outputs (radon json, vulture text, jscpd report,
    # coverage json fragments). Saved for forensics — never queried
    # by routes. Capped at ~1MB by truncating each tool's payload
    # in the capture script.
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class PerfSnapshot(Base):
    """Per-page / per-route static + runtime perf metrics snapshot.

    Captured nightly at 04:00 IST by ``_task_perf_snapshot`` in
    ``background.py`` (runs ``scripts/perf_baseline.py``). One row per
    ``(side, page_or_route)`` per run — the writer inserts a fresh row
    on every cron tick; retention is 365 days (configurable via
    ``retention.perf_snapshots_days`` in settings).

    Frontend rows carry ``lcp_ms / tbt_ms / heap_mb`` when the optional
    ``--with-runtime`` Playwright pass succeeds; backend rows leave these
    NULL. Backend rows carry ``route_p50_ms / route_p95_ms / route_qps``
    when available (future load-test integration); frontend rows leave
    them NULL.

    ``hotspots_json`` stores the top-5 cyclomatic-complexity hotspots as
    ``[{fn_name, cc, line}]`` — the raw list from radon / Svelte heuristic.
    """
    __tablename__ = "perf_snapshots"
    __table_args__ = (
        Index("ix_perf_snapshots_page_captured", "page_or_route", "captured_at"),
        Index("ix_perf_snapshots_captured_at", "captured_at"),
    )

    id: Mapped[int]             = mapped_column(primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    commit_sha: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # 'FE' = frontend page / lib component; 'BE' = backend route controller
    side: Mapped[str]           = mapped_column(String(2), nullable=False)
    # '/pulse' | 'lib::MarketPulse' | 'GET /api/quote'
    page_or_route: Mapped[str]  = mapped_column(String(160), nullable=False)

    # ── Static complexity metrics ────────────────────────────────────
    loc: Mapped[Optional[int]]          = mapped_column(Integer, nullable=True)
    effect_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    state_count: Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    derived_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cc_max: Mapped[Optional[int]]       = mapped_column(Integer, nullable=True)
    cc_avg: Mapped[Optional[float]]     = mapped_column(Float,   nullable=True)
    hotspots_json: Mapped[Optional[dict]] = mapped_column(JSONB,  nullable=True)

    # ── Frontend runtime (Playwright — only when --with-runtime ran) ─
    lcp_ms: Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    tbt_ms: Mapped[Optional[int]]   = mapped_column(Integer, nullable=True)
    heap_mb: Mapped[Optional[float]] = mapped_column(Float,  nullable=True)

    # ── Backend runtime (load-test integration — future) ────────────
    route_p50_ms: Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    route_p95_ms: Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    route_qps: Mapped[Optional[float]]   = mapped_column(Float,   nullable=True)


class MoversSnapshot(Base):
    """Last-good winners/losers snapshot for off-hours display.

    Written at the end of every successful in-market movers fetch.
    One row per IST calendar date (upserted — the latest intraday
    call always wins for that date). The route reads the most recent
    row when the market is closed and the live broker call would return
    an empty result.

    Retention: 7 days (purged daily by _task_purge_persistence_caches).
    """
    __tablename__ = "movers_snapshots"

    id:           Mapped[int]      = mapped_column(primary_key=True, autoincrement=True)
    date:         Mapped[datetime] = mapped_column(Date, nullable=False, index=True)
    # JSON-serialised list[dict] — each dict is one MoverRow payload.
    payload_json: Mapped[str]      = mapped_column(Text, nullable=False)
    captured_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # One row per date — upsert path replaces on conflict.
        UniqueConstraint("date", name="uq_movers_snapshots_date"),
    )


class MarketLifecycleEvent(Base):
    """Audit row for every per-exchange lifecycle transition fired by
    `backend.api.algo.market_lifecycle.MarketLifecycle.poll()`.

    One row per (exchange, event_type) transition. Retention is short
    (~30 days) — the table is informational only, and a missing row
    does not affect lifecycle dispatch. Used by /admin to surface
    "which handlers fired this morning at NSE open" + by debug tooling
    to spot handlers that consistently error.
    """
    __tablename__ = "market_lifecycle_events"

    id: Mapped[int]              = mapped_column(primary_key=True, autoincrement=True)
    exchange: Mapped[str]        = mapped_column(String(8), nullable=False)
    event_type: Mapped[str]      = mapped_column(String(20), nullable=False)
    fired_at: Mapped[datetime]   = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    is_holiday: Mapped[bool]     = mapped_column(Boolean, nullable=False, default=False)
    handlers_run: Mapped[int]    = mapped_column(Integer, nullable=False, default=0)
    handlers_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_lifecycle_fired_at", "fired_at"),
        Index("ix_lifecycle_exch_type_fired", "exchange", "event_type", "fired_at"),
    )


class MarketHoliday(Base):
    """Trading-holiday calendar per exchange, persisted for durable lookup.

    Populated by the daily `_task_holiday_refresh` cron (04:00 IST) which
    calls `fetch_holidays(exchange)` — that in turn hits the NSE public API
    (`nseindia.com/api/holiday-master?type=trading`) and normalises the
    payload into (exchange, date) rows. Idempotent UPSERT on the composite
    PK; a row disappearing from the NSE payload does NOT auto-delete the
    stored row (holidays only accrete during a year — a removal would be an
    exchange-side error and should be operator-reviewed, not silently
    swallowed).

    Read path (Tier 3 of `fetch_holidays`) queries by `exchange` filtered
    to the current IST calendar year. On a cold boot with an empty table
    the code falls back to Tier 4 (direct NSE HTTP fetch) and the cron
    populates the table on its next run.

    `source` values:
      • `'nse_auto'`     — populated by the automated cron (default)
      • `'operator'`     — hand-edited via /admin/settings (future)
      • `'legacy_seed'`  — imported from `_HOLIDAY_CACHE` on first boot
    """
    __tablename__ = "market_holidays"

    exchange: Mapped[str]  = mapped_column(String(10), nullable=False, primary_key=True)
    date: Mapped[datetime] = mapped_column(Date, nullable=False, primary_key=True)
    reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    source: Mapped[str]    = mapped_column(String(20), nullable=False, default="nse_auto")
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_market_holidays_exchange_date", "exchange", "date"),
    )


class MarketSpecialSession(Base):
    """Operator-defined special trading sessions that trump all calendar rules.

    A row in this table says: on ``date`` the exchange named ``exchange`` is
    open ONLY during ``[start_time, end_time)`` IST.  This is the HIGHEST-
    precedence rule in ``is_market_open`` — it short-circuits holiday checks,
    regular session windows, and the live-quote probe.

    Typical use: Diwali Muhurat sessions where NSE/MCX hold a 1-hour evening
    session on an otherwise-holiday date.  On any date that has a special-
    session row the exchange is treated as:
      • open   if now.time() in [start_time, end_time)
      • closed  at all other times during that day

    If no row exists for a given (exchange, date) the normal precedence chain
    (holiday → regular sessions → probe) applies unchanged.

    Columns
    -------
    exchange   : Exchange identifier, e.g. "NSE", "MCX".
    date       : Calendar date (IST) of the special session.
    start_time : Session open time (IST), inclusive.
    end_time   : Session close time (IST), exclusive.
    reason     : Human-readable label, e.g. "Diwali Muhurat 2026".
    created_at : Row-creation timestamp (UTC).

    Primary key is ``(exchange, date, start_time)`` so multiple non-overlapping
    windows on the same day are representable (rare but valid).
    """
    __tablename__ = "market_special_sessions"

    exchange:   Mapped[str]      = mapped_column(String(10),  nullable=False, primary_key=True)
    date:       Mapped[datetime] = mapped_column(Date,        nullable=False, primary_key=True)
    start_time: Mapped[datetime] = mapped_column(Time,        nullable=False, primary_key=True)
    end_time:   Mapped[datetime] = mapped_column(Time,        nullable=False)
    reason:     Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_special_sessions_exchange_date", "exchange", "date"),
    )
