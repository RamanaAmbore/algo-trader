"""
`/api/admin/brokers/*` — broker-account CRUD for the /admin/brokers page.

Operators add / edit / delete Kite (and future-other-vendor) accounts
from the UI without ever opening secrets.yaml. Credentials sit in the
`broker_accounts` table; api_secret / password / TOTP seed are
Fernet-encrypted at rest with a key derived from `cookie_secret`.

Every mutation reloads the `Connections` singleton so subsequent
broker calls (holdings / positions / quotes / orders) pick up the new
state without a service restart.

API responses NEVER include the encrypted columns or the decrypted
secrets — only metadata (account / broker_id / api_key / source_ip /
is_active / notes / created_at / updated_at). The single-account
GET shows the api_key plaintext (it's not credential-grade alone) but
masks the secrets so the operator can confirm what they entered
without re-leaking it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import msgspec
from litestar import Controller, delete, get, patch, post, put
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.rbac import cap_guard
from backend.api.database import shared_async_session
from backend.api.models import BrokerAccount
from backend.shared.helpers.broker_creds import encrypt
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────

class BrokerAccountInfo(msgspec.Struct):
    id:         int
    account:    str
    broker_id:  str
    api_key:    str            # plaintext — not credential-grade alone
    client_id:  str | None     # plaintext — Dhan-style client identifier
    source_ip:  str | None
    is_active:  bool
    notes:      str | None
    priority:   int            # PriceBroker fallback order; lower = tried first
    extra_config: dict         # free-form per-broker tuning knobs
    # When True, this account participates in the /api/options/historical
    # fallback loop. False reserves the account for order-flow only.
    historical_data_enabled: bool
    created_at: str
    updated_at: str
    # Status — populated by enrichment (whether the account is currently
    # loaded into the Connections singleton). Lets the UI render an
    # "active / not loaded" pill without a separate request.
    loaded:     bool = False
    # Presence flags for the four encrypted credential columns. Boolean
    # only — never the values. Operator can see at a glance which slots
    # are filled in DB so a "test" failure with "no working token" maps
    # back to a missing-field instead of a guessing game. Lets the UI
    # render small ✓/✗ chips next to each credential row.
    has_api_secret:   bool = False
    has_password:     bool = False
    has_totp_token:   bool = False
    has_access_token: bool = False
    # Per-account poll priority (Dhan-only, Jul 2026).
    # 'hot' | 'warm' | 'cold' — controls background poll cadence.
    # Kite/Groww accounts carry this field but the gate is never applied.
    poll_priority: str = "hot"
    auto_downgrade_enabled:  bool = False
    auto_downgraded_at:      str | None = None  # ISO-8601 UTC or None
    auto_downgrade_reason:   str | None = None
    # Circuit-breaker opt-in (Jul 2026). When True the 3-fail / 5-min
    # OPEN state machine is active for this account. Default False so
    # the breaker is disabled for all accounts except DH6847 (seeded
    # by the startup migration in _ensure_shared_broker_schema).
    circuit_breaker_enabled: bool = False
    # Display ordering (Jul 2026) — canonical position in UI dropdowns,
    # health-badge chip popup, performance grids. Lower = shown earlier.
    # Default 500 (mid-tier) so new accounts land in the middle.
    display_order: int = 500


class BrokerAccountCreate(msgspec.Struct):
    account:     str
    broker_id:   str = "zerodha_kite"
    api_key:     str = ""
    api_secret:  str = ""
    password:    str = ""
    totp_token:  str = ""
    client_id:   str = ""             # Dhan-style client id (plaintext)
    access_token: str = ""            # Dhan-style long-lived token
    source_ip:   str | None = None
    is_active:   bool = True
    notes:       str | None = None
    priority:    int = 100
    extra_config: dict = msgspec.field(default_factory=dict)
    historical_data_enabled: bool = True


class BrokerAccountUpdate(msgspec.Struct):
    """Every field is optional — operator can update a single secret
    without re-typing the others. Empty strings on the secret fields
    are treated as 'no change' (so a partial form doesn't blank out a
    credential the operator didn't intend to clear)."""
    broker_id:   Optional[str]  = None
    api_key:     Optional[str]  = None
    api_secret:  Optional[str]  = None
    password:    Optional[str]  = None
    totp_token:  Optional[str]  = None
    client_id:   Optional[str]  = None
    access_token: Optional[str] = None
    source_ip:   Optional[str]  = None
    is_active:   Optional[bool] = None
    notes:       Optional[str]  = None
    priority:    Optional[int]  = None
    extra_config: Optional[dict] = None
    historical_data_enabled: Optional[bool] = None
    # Per-account poll priority (Dhan-only, Jul 2026).
    poll_priority:          Optional[str]  = None   # 'hot' | 'warm' | 'cold'
    auto_downgrade_enabled:  Optional[bool] = None
    circuit_breaker_enabled: Optional[bool] = None  # opt-in per-account breaker
    # Display ordering (Jul 2026) — canonical UI position across all surfaces.
    display_order:           Optional[int]  = None


class TestResult(msgspec.Struct):
    ok:      bool
    account: str
    detail:  str


# ── Helpers ───────────────────────────────────────────────────────────

def _to_info(row: BrokerAccount, *, loaded: bool = False) -> BrokerAccountInfo:
    auto_downgraded_at = getattr(row, "auto_downgraded_at", None)
    return BrokerAccountInfo(
        id=row.id, account=row.account, broker_id=row.broker_id,
        api_key=row.api_key,
        client_id=getattr(row, "client_id", None),
        source_ip=row.source_ip,
        has_api_secret=bool(row.api_secret_enc),
        has_password=bool(row.password_enc),
        has_totp_token=bool(row.totp_token_enc),
        has_access_token=bool(getattr(row, "access_token_enc", None)),
        is_active=bool(row.is_active),
        notes=row.notes,
        priority=int(getattr(row, "priority", 100) or 100),
        extra_config=getattr(row, "extra_config", None) or {},
        historical_data_enabled=bool(getattr(row, "historical_data_enabled", True)),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
        loaded=loaded,
        poll_priority=str(getattr(row, "poll_priority", "hot") or "hot"),
        auto_downgrade_enabled=bool(getattr(row, "auto_downgrade_enabled", False)),
        auto_downgraded_at=(
            auto_downgraded_at.isoformat() if auto_downgraded_at else None
        ),
        auto_downgrade_reason=getattr(row, "auto_downgrade_reason", None),
        circuit_breaker_enabled=bool(getattr(row, "circuit_breaker_enabled", False)),
        display_order=int(getattr(row, "display_order", 500) or 500),
    )


_VALID_POLL_PRIORITIES: frozenset[str] = frozenset({"hot", "warm", "cold"})

# Last-known account list from list_remote_accounts() — serves as a fallback
# when the UDS conn-service is briefly unavailable (e.g. 06:00 IST token expiry
# restart window).  Populated on the first successful call; cleared only on
# process restart.  Thread-safe: the variable is only ever replaced atomically
# (single assignment).
_last_known_remote_accounts: set[str] = set()


def _apply_nonsecret_fields(row: BrokerAccount, data: "BrokerAccountUpdate") -> None:
    """Patch plain (non-encrypted) fields onto *row* in-place.

    Each field is only touched when the operator supplied a non-None value,
    so a partial PATCH never blanks fields the caller didn't intend to touch.
    ``poll_priority`` raises HTTPException(400) on an unrecognised value and
    clears the auto-downgrade stamps when the operator explicitly changes it.
    """
    if data.broker_id is not None:  row.broker_id = data.broker_id
    if data.api_key   is not None:  row.api_key   = data.api_key
    if data.client_id is not None:  row.client_id = data.client_id or None
    if data.source_ip is not None:  row.source_ip = data.source_ip or None
    if data.is_active is not None:  row.is_active = bool(data.is_active)
    if data.notes     is not None:  row.notes     = data.notes
    if data.priority  is not None:  row.priority  = int(data.priority)
    if data.extra_config is not None:
        row.extra_config = dict(data.extra_config or {})
    if data.historical_data_enabled is not None:
        row.historical_data_enabled = bool(data.historical_data_enabled)
    if data.poll_priority is not None:
        if data.poll_priority not in _VALID_POLL_PRIORITIES:
            raise HTTPException(
                status_code=400,
                detail=f"poll_priority must be one of: {sorted(_VALID_POLL_PRIORITIES)}",
            )
        row.poll_priority = data.poll_priority
        # Manual change clears auto-downgrade stamps so the UI can
        # distinguish manual vs auto priority changes.
        if data.poll_priority != getattr(row, "auto_downgrade_reason", None):
            row.auto_downgraded_at = None
            row.auto_downgrade_reason = None
    if data.auto_downgrade_enabled is not None:
        row.auto_downgrade_enabled = bool(data.auto_downgrade_enabled)
    if data.circuit_breaker_enabled is not None:
        row.circuit_breaker_enabled = bool(data.circuit_breaker_enabled)
    if data.display_order is not None:
        row.display_order = int(data.display_order)


def _apply_secret_fields(row: BrokerAccount, data: "BrokerAccountUpdate") -> None:
    """Encrypt and store credential fields. Empty / None → unchanged."""
    if data.api_secret:    row.api_secret_enc   = encrypt(data.api_secret)
    if data.password:      row.password_enc     = encrypt(data.password)
    if data.totp_token:    row.totp_token_enc   = encrypt(data.totp_token)
    if data.access_token:  row.access_token_enc = encrypt(data.access_token)


async def _flush_priority_caches(
    account: str, row: BrokerAccount, data: "BrokerAccountUpdate",
) -> None:
    """Push poll-priority + circuit-breaker opt-in into the in-process
    cache immediately — before rebuild_from_db completes — so the
    interval gate sees the new value on the very next poll cycle."""
    if data.poll_priority is None and data.circuit_breaker_enabled is None:
        return
    try:
        from backend.brokers.broker_apis import (
            set_dhan_priority_cache,
            set_breaker_optin_cache,
        )
        if data.poll_priority is not None:
            set_dhan_priority_cache(account, row.poll_priority or "hot")
        if data.circuit_breaker_enabled is not None:
            set_breaker_optin_cache(account, bool(row.circuit_breaker_enabled))
    except Exception:
        pass


async def _flush_display_order_cache(data: "BrokerAccountUpdate") -> None:
    """Invalidate the display_order cache when the operator changed it."""
    if data.display_order is None:
        return
    try:
        from backend.brokers.broker_apis import invalidate_account_order_cache
        invalidate_account_order_cache()
    except Exception:
        pass


async def _reload_connections() -> None:
    """Trigger Connections.rebuild_from_db so subsequent broker calls
    pick up the new state. Failures are logged but don't fail the
    request (the row is already persisted; reload can retry later)."""
    try:
        from backend.brokers.connections import Connections
        await Connections().rebuild_from_db()
    except Exception as e:
        logger.warning(f"Connections reload after broker mutation failed: {e}")


# F12 — strong references for the fire-and-forget books-refresh task
# (2026-11-20, advisor-driven correction). `asyncio.create_task()`'s
# own return value is the ONLY strong reference to that task by
# default; if nothing else holds it, the task can be garbage-collected
# mid-run once this request handler returns and its local variables go
# out of scope. This refresh now runs for several seconds (multiple
# broker round-trips + a conditional daily_book snapshot write), so the
# risk is real, not theoretical. Each task adds itself here on
# creation and removes itself via `add_done_callback` on completion.
_books_refresh_tasks: set = set()


async def _force_refresh_one_account_after_reenable(account: str) -> None:
    """Re-enabling a disabled account left its positions/holdings/
    margins/funds at whatever was last cached until the next routine
    poll, which is itself gated by closed-hours logic. Called directly
    from `update_account`'s False→True transition, bypassing that gate
    on purpose — this is a one-shot operator action, not a routine poll.

    Calls the ZERO-ARG `fetch_positions(force_refresh=True)` /
    `fetch_margins(force_refresh=True)` forms, not the single-account
    branch (which bypasses the shared SSOT cache every route actually
    reads from). `fetch_holdings` has no `force_refresh` kwarg, so
    `_raw_cache_invalidate("holdings")` is called first to force a
    genuine cache miss. This refreshes every account, not only the
    re-enabled one — bounded, one-shot cost, not a hot path.

    A `daily_book` snapshot for just this account is also written when
    the market is closed, via `_snapshot_books_if_closed()` — see its
    own docstring for why (the live cache refresh above is otherwise
    invisible to the closed-hours route gate).

    Fire-and-forget: tracked in the module-level `_books_refresh_tasks`
    set (see its own comment) so it can't be garbage-collected mid-run.
    Each of the three broker calls has its own try/except so one
    surface failing doesn't skip the others; `mask_account()` is
    applied at each log call site, not at the parameter boundary.
    """
    from backend.shared.helpers.utils import mask_account
    masked = mask_account(account)
    logger.info(
        f"[BOOKS-REFRESH] {masked}: connection re-enabled — forcing an "
        f"unconditional refresh of positions/holdings/margins/funds "
        f"(bypassing closed-hours gate)"
    )
    try:
        from backend.api.cache import invalidate
        from backend.brokers.broker_apis import (
            _raw_cache_invalidate, fetch_positions, fetch_holdings, fetch_margins,
        )
        for _key in ("positions", "holdings", "funds"):
            try:
                invalidate(_key)
            except Exception:
                pass
        for _key in ("positions", "holdings", "margins"):
            try:
                _raw_cache_invalidate(_key)
            except Exception:
                pass

        import asyncio as _asyncio
        # Zero-arg forms — the SSOT-cache-populating path every route
        # reads from (not the single-account branch, which bypasses that
        # cache entirely). See this function's own docstring. Each call
        # is independently guarded so one surface's failure doesn't
        # abort the other two.
        failed_surfaces: list[str] = []
        try:
            await _asyncio.to_thread(fetch_positions, force_refresh=True)
        except Exception as e:
            failed_surfaces.append("positions")
            logger.warning(f"[BOOKS-REFRESH] {masked}: positions refresh failed: {e}")
        try:
            await _asyncio.to_thread(fetch_holdings)
        except Exception as e:
            failed_surfaces.append("holdings")
            logger.warning(f"[BOOKS-REFRESH] {masked}: holdings refresh failed: {e}")
        try:
            await _asyncio.to_thread(fetch_margins, force_refresh=True)
        except Exception as e:
            failed_surfaces.append("margins/funds")
            logger.warning(f"[BOOKS-REFRESH] {masked}: margins/funds refresh failed: {e}")

        if failed_surfaces:
            logger.warning(
                f"[BOOKS-REFRESH] {masked}: forced refresh complete WITH FAILURES "
                f"({', '.join(failed_surfaces)}) — see the per-surface warning(s) above"
            )
        else:
            logger.info(f"[BOOKS-REFRESH] {masked}: forced refresh complete")

        # F12 follow-up (2026-11-20, live-confirmed by the coordinator:
        # the force-refreshed live cache is INVISIBLE during closed
        # hours — `closed_hours_or_broker()`'s route gate serves the
        # `daily_book` snapshot ONLY and never consults the live cache
        # this function just refreshed, see CLAUDE.md's "Closed-hours
        # route gate" entry). Writes a fresh `daily_book` row for just
        # this one account so the closed-hours display route has
        # something current to serve, instead of the stale pre-
        # re-enable row.
        await _snapshot_books_if_closed(account, masked)
    except Exception as e:
        # Cache invalidation itself failed (before any fetch ran), or
        # some other error outside the three per-surface try/excepts.
        logger.warning(f"[BOOKS-REFRESH] {masked}: forced refresh after re-enabling failed: {e}")


async def _last_completed_trading_session_date(now_ist) -> date:
    """Return the `date` of the most recent trading session that has
    actually completed, as of *now_ist*.

    `session_date_of(now)` alone is unsafe: it advances to a new
    session date at every 08:00 IST boundary regardless of whether that
    date is a real trading day. A `daily_book` row dated a non-trading
    day (e.g. Saturday) would become `latest_batch` for `_positions_
    snapshot()`'s per-account baseline-diff query with the same LTP as
    the real prior session's row — collapsing Day P&L to zero, the same
    regression class `_task_closed_hours_refresh`'s own docstring
    documents. Floor-walking to the last real trading day avoids that.
    Mirrors `positions.py`'s own floor-walk (`_SESSION_ANCHOR_CUTOFF_
    TS_SQL` neighbourhood). Bounded to 10 iterations, mirroring
    `expiry_freeze.next_market_open_ist`'s own bound. `is_trading_day()`
    is called WITHOUT `exchange=`/`now=` so this never triggers a live
    broker probe.
    """
    from datetime import timedelta
    from backend.brokers.broker_apis import fetch_holidays as _fetch_holidays
    from backend.shared.helpers.date_time_utils import is_trading_day as _is_trading_day
    from backend.api.algo.expiry_freeze import session_date_of
    import asyncio as _asyncio
    try:
        _nse_holidays = await _asyncio.to_thread(_fetch_holidays, "NSE")
    except Exception:
        _nse_holidays = set()
    _floor_date = session_date_of(now_ist)
    for _ in range(10):
        if _is_trading_day(_floor_date, _nse_holidays):
            break
        _floor_date -= timedelta(days=1)
    return _floor_date


async def _daily_book_row_exists(account: str, target_date: date) -> bool:
    """True when a `positions` or `holdings` `daily_book` row already
    exists for *account* dated *target_date*.

    Gates the snapshot write below so a repeated re-enable (or an
    account whose own regular EOD pass already ran before it was
    disabled) never overwrites an already-correct row with an inferior
    one, and so a late re-enable can never masquerade as "today's real
    EOD already happened" for every OTHER account via `background.py:
    _pcf_eod_already_fired_today`'s own bare `date = :today` existence
    check (that check carries no account filter — see this module's own
    `_snapshot_books_if_closed` docstring for the full reasoning)."""
    from sqlalchemy import text as _sql_text
    async with shared_async_session() as session:
        result = await session.execute(_sql_text(
            "SELECT 1 FROM daily_book "
            "WHERE date = :target_date AND account = :account "
            "AND kind IN ('holdings', 'positions') LIMIT 1"
        ), {"target_date": target_date, "account": account})
        return result.first() is not None


async def _snapshot_books_if_closed(account: str, masked: str) -> None:
    """Write a fresh `daily_book` row for *account* ONLY, ONLY when the
    market is currently closed, ONLY when no row already exists for the
    real last-completed trading session.

    When the market is OPEN, this is deliberately a no-op — the normal
    live-fetch path (positions.py/holdings.py reading the just-
    refreshed cache this function's caller repopulated) already serves
    current data in that case; see the `market_open=False` reasoning
    below for why writing a snapshot here at all is specifically a
    CLOSED-hours-only concern.

    Uses `snapshot_gate._any_segment_open()` — the exact function
    `closed_hours_or_broker()` (the canonical route gate this whole fix
    exists to work around) itself calls — not `date_time_utils.
    is_any_segment_open`, so this can never disagree with the route
    gate about whether the market is open.

    `target_date` is the last REAL completed trading session (see
    `_last_completed_trading_session_date`'s own docstring for why
    `session_date_of(now)` alone is unsafe here), and the write is
    skipped entirely (logged, not silent) when a row already exists for
    that exact (account, date) — this is the second half of the same
    protection: it stops THIS function from ever overwriting an
    already-correct EOD row with an inferior one, AND it stops a stray
    extra row from ever satisfying `_pcf_eod_already_fired_today`'s
    account-unscoped `date = :today` existence check for a day whose
    real EOD snapshot (for every OTHER account) hasn't actually run yet.

    `market_open=False` is passed explicitly — matching
    `routes/admin.py:trigger_pnl_snapshot`'s own resolved value for this
    exact "market confirmed closed" case (that endpoint computes the
    SAME boolean via `is_any_segment_open()` and passes it through
    unless the caller overrides it). `market_open=False` ALSO skips the
    orphan-position-prune step inside `snapshot_daily_book` (gated on
    `market_open=True` — see that function's own 2026-11-05 comment),
    which matters here: this is exactly the "fetch firing hours late /
    a restart's startup snapshot" shape that fix was written to protect
    against, not a genuine live-hours confirmation. `settled` is left
    at its default (`False`) — that flag is reserved for the real
    `close_settled` event; this is a gap-filling refresh, not an EOD
    settlement.

    `_fetch_account_data` (the function `snapshot_daily_book` uses to
    fetch) calls `broker.holdings()`/`broker.positions()` directly —
    the RAW per-account adapter methods, not `broker_apis.fetch_
    holdings()`/`fetch_positions()` (the SSOT-cached, LKG-substituting
    wrappers `_force_refresh_one_account_after_reenable`'s own three
    calls use). Confirmed by reading both: a failure here returns `[]`
    (`_safe_fetch`'s own default), never a last-known-good substitute —
    so this write can never "launder" stale 2026-11-19-dated LKG data
    into a freshly-timestamped row.
    """
    try:
        from backend.api.helpers.snapshot_gate import _any_segment_open
        import asyncio as _asyncio
        is_open = await _asyncio.to_thread(_any_segment_open)
        if is_open:
            logger.info(
                f"[BOOKS-REFRESH] {masked}: market is open — the live "
                f"fetch path already serves current data, no daily_book "
                f"snapshot needed"
            )
            return

        from backend.shared.helpers.date_time_utils import timestamp_indian
        now_ist = timestamp_indian()
        target_date = await _last_completed_trading_session_date(now_ist)

        if await _daily_book_row_exists(account, target_date):
            logger.info(
                f"[BOOKS-REFRESH] {masked}: daily_book already has a row "
                f"for {target_date} — skipping snapshot write"
            )
            return

        from backend.api.algo.daily_snapshot import snapshot_daily_book
        result = await snapshot_daily_book(
            target_date=target_date, accounts=[account], market_open=False,
        )
        if account not in (result.get("accounts") or []) or result.get("errors"):
            logger.warning(
                f"[BOOKS-REFRESH] {masked}: daily_book snapshot for "
                f"{target_date} did not complete cleanly — "
                f"accounts={result.get('accounts')!r} errors={result.get('errors')!r}"
            )
            return
        logger.info(
            f"[BOOKS-REFRESH] {masked}: daily_book snapshot written for "
            f"{target_date} — holdings={result.get('holdings_rows')} "
            f"positions={result.get('positions_rows')}"
        )
    except Exception as e:
        logger.warning(f"[BOOKS-REFRESH] {masked}: daily_book snapshot failed: {e}")


def _loaded_accounts() -> set[str]:
    """Account codes considered HEALTHY: connection object exists in
    the Connections singleton AND the most recent broker fetch for
    this account succeeded.

    Operator: 'when connection issue there in groww, I still 5/5 in
    navbar instead 4/5 as one account connection has issue.' The
    previous implementation flagged loaded=True as soon as the
    Connection object was constructed at startup, even if every
    subsequent API call failed (e.g. Groww 403 / Dhan rate-limit).
    Per-account fetch results are now tracked in
    broker_apis._FETCH_HEALTH and an account is only loaded when its
    latest attempt succeeded — failing accounts drop out of the
    navbar count, surfacing the outage at a glance.

    Fallback: when the UDS conn-service is briefly unavailable (e.g.
    06:00 IST token-expiry restart window) list_remote_accounts()
    returns []. Without a fallback, _loaded_accounts() returns set()
    and the navbar chip shows 0/5 even though Dhan/Groww accounts are
    healthy. The module-level _last_known_remote_accounts cache serves
    the last successful list so the chip stays stable during the UDS
    blip (typically <5 s)."""
    global _last_known_remote_accounts
    try:
        from backend.brokers.connections import Connections
        from backend.brokers.broker_apis import is_account_healthy
        in_conn = set(Connections().conn.keys())
        # Cutover branch — when local Connections is empty (flag-on),
        # pull the loaded-account list from conn_service.
        if not in_conn:
            from backend.brokers.client import is_cutover_on
            if is_cutover_on():
                from backend.brokers.client.remote_broker import list_remote_accounts
                remote_accs = {r["account"] for r in list_remote_accounts() if r.get("account")}
                if remote_accs:
                    _last_known_remote_accounts = remote_accs
                    in_conn = remote_accs
                elif _last_known_remote_accounts:
                    logger.warning(
                        "_loaded_accounts: list_remote_accounts() returned [] "
                        "— UDS briefly unavailable; serving cached account list "
                        "(%d accounts)",
                        len(_last_known_remote_accounts),
                    )
                    in_conn = _last_known_remote_accounts
                else:
                    logger.warning(
                        "_loaded_accounts: conn_service returned no accounts "
                        "— UDS unreachable or conn_service still loading"
                    )
        return {a for a in in_conn if is_account_healthy(a)}
    except Exception as e:
        logger.warning("_loaded_accounts: unexpected error: %s", e)
        return set()


# ── Controller ────────────────────────────────────────────────────────

class BrokersController(Controller):
    path = "/api/admin/brokers"
    # No controller-level guard — each route declares its own
    # capability via `cap_guard`. Read routes (list / detail / caps
    # query) are gated by `view_brokers` which includes ops + risk +
    # demo (masked secrets). Mutating routes (create / patch / delete
    # / test) are gated by `manage_brokers` / `test_broker_connection`
    # which restrict to admin + ops. This is the first controller
    # migrated off the binary admin_guard pattern; further controllers
    # follow the same split.

    # ── List + read (view_brokers cap — admin / ops / risk / demo) ────

    @get("/", guards=[cap_guard("view_brokers")])
    async def list_accounts(self) -> list[BrokerAccountInfo]:
        async with shared_async_session() as s:
            rows = (await s.execute(
                select(BrokerAccount).order_by(
                    BrokerAccount.display_order, BrokerAccount.account
                )
            )).scalars().all()
        loaded = _loaded_accounts()
        return [_to_info(r, loaded=(r.account in loaded)) for r in rows]

    @get("/{account:str}", guards=[cap_guard("view_brokers")])
    async def get_account(self, account: str) -> BrokerAccountInfo:
        async with shared_async_session() as s:
            row = (await s.execute(
                select(BrokerAccount).where(BrokerAccount.account == account)
            )).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404,
                                detail=f"Broker account {account!r} not found")
        return _to_info(row, loaded=(account in _loaded_accounts()))

    @get("/{account:str}/capabilities", guards=[cap_guard("view_brokers")])
    async def get_capabilities(self, account: str) -> dict:
        """Sprint C — return the broker capability matrix for one
        account so OrderTicket can surface inline warnings ("Groww
        emulates OCO — ~15s race window", "Dhan doesn't cover MCX") at
        SUBMIT time, not at fill time. Pure read of the dataclass; no
        broker round-trip."""
        from backend.brokers.capabilities import capabilities_for
        from dataclasses import asdict
        caps = capabilities_for(account)
        return asdict(caps)

    @get("/order", guards=[cap_guard("view_brokers")])
    async def get_display_order(self) -> dict[str, int]:
        """GET /api/admin/brokers/order — returns {account_id: display_order}
        map for every active broker account.

        Frontend hydrates this once at boot into an `accountDisplayOrder`
        store and uses `sortAccountsBy(accounts, orderMap)` on every UI
        surface that lists accounts. Cached on the backend for 60 s;
        invalidated immediately by PATCH display_order changes.
        """
        async with shared_async_session() as s:
            rows = (await s.execute(
                select(BrokerAccount.account, BrokerAccount.display_order)
                .order_by(BrokerAccount.display_order, BrokerAccount.account)
            )).all()
        return {str(r.account): int(r.display_order) for r in rows}

    # ── Create (manage_brokers — admin / ops only) ────────────────────

    @post("/", guards=[cap_guard("manage_brokers")])
    async def create_account(self, data: BrokerAccountCreate) -> BrokerAccountInfo:
        if not data.account:
            raise HTTPException(status_code=400, detail="account is required")
        async with shared_async_session() as s:
            existing = (await s.execute(
                select(BrokerAccount).where(BrokerAccount.account == data.account)
            )).scalar_one_or_none()
            if existing:
                raise HTTPException(status_code=409,
                    detail=f"Account {data.account!r} already exists")
            row = BrokerAccount(
                account=data.account,
                broker_id=data.broker_id or "zerodha_kite",
                api_key=data.api_key or "",
                api_secret_enc=encrypt(data.api_secret),
                password_enc=encrypt(data.password),
                totp_token_enc=encrypt(data.totp_token),
                client_id=(data.client_id or None) or None,
                access_token_enc=(encrypt(data.access_token)
                                   if data.access_token else None),
                source_ip=data.source_ip or None,
                is_active=bool(data.is_active),
                notes=data.notes,
                priority=int(data.priority) if data.priority is not None else 100,
                extra_config=dict(data.extra_config or {}),
                historical_data_enabled=bool(data.historical_data_enabled),
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
        await _reload_connections()
        logger.warning(f"broker_accounts: created {data.account!r} via /admin/brokers")
        return _to_info(row, loaded=(data.account in _loaded_accounts()))

    # ── Update (manage_brokers) ───────────────────────────────────────

    @patch("/{account:str}", guards=[cap_guard("manage_brokers")])
    async def update_account(self, account: str,
                             data: BrokerAccountUpdate) -> BrokerAccountInfo:
        async with shared_async_session() as s:
            row = (await s.execute(
                select(BrokerAccount).where(BrokerAccount.account == account)
            )).scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404,
                    detail=f"Broker account {account!r} not found")

            # F12 (2026-11-20) — capture the OLD is_active value BEFORE
            # _apply_nonsecret_fields mutates row.is_active in place, so
            # the False→True transition can be detected below. Same
            # before-vs-after convention _apply_nonsecret_fields itself
            # already uses for poll_priority.
            was_active = bool(row.is_active)

            _apply_nonsecret_fields(row, data)   # may raise HTTPException(400)
            _apply_secret_fields(row, data)
            row.updated_at = datetime.now(timezone.utc)
            await s.commit()
            await s.refresh(row)

        await _flush_priority_caches(account, row, data)
        await _flush_display_order_cache(data)
        await _reload_connections()

        # F12 — re-enabling a disabled connection forces an immediate,
        # unconditional (non-market-hours-gated) books refresh for just
        # this one account. Scoped to the real False→True transition
        # only: a True→False (disable), or any other field change on an
        # already-active/already-inactive account, must not trigger
        # this. See _force_refresh_one_account_after_reenable's own
        # docstring for why this bypasses the normal closed-hours gate.
        if data.is_active is True and not was_active and bool(row.is_active):
            import asyncio as _asyncio
            _task = _asyncio.create_task(_force_refresh_one_account_after_reenable(account))
            _books_refresh_tasks.add(_task)
            _task.add_done_callback(_books_refresh_tasks.discard)

        logger.warning(f"broker_accounts: updated {account!r} via /admin/brokers")
        return _to_info(row, loaded=(account in _loaded_accounts()))

    # ── Delete (manage_brokers) ───────────────────────────────────────

    @delete("/{account:str}", status_code=200, guards=[cap_guard("manage_brokers")])
    async def delete_account(self, account: str) -> dict:
        async with shared_async_session() as s:
            row = (await s.execute(
                select(BrokerAccount).where(BrokerAccount.account == account)
            )).scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404,
                    detail=f"Broker account {account!r} not found")
            await s.delete(row)
            await s.commit()
        await _reload_connections()
        logger.warning(f"broker_accounts: deleted {account!r} via /admin/brokers")
        return {"ok": True, "account": account}

    # ── Restore poll priority (manage_brokers) ───────────────────────

    @post("/{account:str}/restore-priority", guards=[cap_guard("manage_brokers")])
    async def restore_priority(self, account: str) -> BrokerAccountInfo:
        """Reset poll_priority to 'hot', clear auto-downgrade stamps,
        and set next_poll_at to now so the account is polled on the
        next background cycle.

        Operator action after investigating a wave of Dhan errors that
        triggered auto-downgrade. Does not restart the Connections
        singleton (no credentials changed).
        """
        from backend.brokers.broker_apis import _dhan_next_poll, set_dhan_priority_cache
        async with shared_async_session() as s:
            row = (await s.execute(
                select(BrokerAccount).where(BrokerAccount.account == account)
            )).scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404,
                    detail=f"Broker account {account!r} not found")
            row.poll_priority = "hot"
            row.auto_downgraded_at = None
            row.auto_downgrade_reason = None
            row.updated_at = datetime.now(timezone.utc)
            await s.commit()
            await s.refresh(row)

        # Update in-process cache immediately so the interval gate
        # sees 'hot' before the next rebuild_from_db completes.
        set_dhan_priority_cache(account, "hot")
        # Reset next_poll so the account is polled on the very next cycle.
        _dhan_next_poll[account] = 0.0

        logger.warning(
            f"broker_accounts: poll priority restored to hot for {account!r}"
        )
        return _to_info(row, loaded=(account in _loaded_accounts()))

    # ── Test connection (test_broker_connection cap — admin / ops) ───

    @post("/{account:str}/test", guards=[cap_guard("test_broker_connection")])
    async def test_account(self, account: str) -> TestResult:
        """Try a cheap broker call (profile()) to confirm the credentials
        actually authenticate. Doesn't mutate state — just exercises the
        login path so the operator gets immediate feedback."""
        await _reload_connections()
        try:
            from backend.brokers.registry import get_broker
            broker = get_broker(account)
            prof = broker.profile() or {}
            return TestResult(ok=True, account=account,
                              detail=(f"Authenticated as "
                                      f"{prof.get('user_name') or prof.get('user_id') or '?'}"))
        except KeyError:
            return TestResult(ok=False, account=account,
                detail=("Account not in Connections. Edit + Save first, "
                        "then re-test."))
        except Exception as e:
            return TestResult(ok=False, account=account, detail=str(e))
