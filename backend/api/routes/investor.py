"""
Investor portal — token-gated public URL for LPs to read their NAV slice.

The portal is a token-as-credential surface: anyone holding the URL
`/investor/<token>` sees the LP's NAV share, day delta, contribution,
and NAV-slice history. No login. No registration. The operator mints
a token via the admin endpoint, copies the URL, and forwards it to
the LP through whatever channel they trust (email, WhatsApp, in-
person).

This trade — UX simplicity vs login security — is intentional:
- LPs don't want to manage a password for quarterly NAV checks.
- The boutique fund has 1–5 LPs at most; a magic-link is sufficient.
- Tokens are revocable. If a URL leaks, admin clicks Revoke and
  re-mints; the old URL 401s on the next visit.

Industry analog: Carta investor portal magic-links, SS&C/GP-Link
share-class URLs, Yieldstreet's per-LP URL slugs. Same shape.

Endpoints:
    Admin (cap_guard("manage_investor_tokens"))
        GET    /api/admin/users/{id}/investor-tokens       list (active + revoked)
        POST   /api/admin/users/{id}/investor-tokens       mint new
        DELETE /api/admin/users/{id}/investor-tokens/{tid} revoke

    Public (no auth; token in URL)
        GET /api/investor/{token}/slice    InvestorSlice
        GET /api/investor/{token}/history  scaled NAV curve

The public endpoints reuse the same NAV slice math as `/api/nav/me`,
just with the token-resolved user_id instead of the JWT-resolved one.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import msgspec
import asyncio

from litestar import Controller, Response, get, post, delete
from litestar.exceptions import HTTPException
from sqlalchemy import desc, select, update

from backend.api.database import async_session
from backend.api.models import (
    InvestorEvent, InvestorToken, MonthlyStatement, NavDaily, User,
)
from backend.api.rbac import cap_guard


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class _TokenRow(msgspec.Struct):
    """One investor-token row for the admin list view."""
    id:            int
    token_preview: str        # first 8 chars + ellipsis; full token only at mint
    expires_at:    str
    revoked_at:    Optional[str]
    last_visit_at: Optional[str]
    visit_count:   int
    note:          Optional[str]
    created_at:    str
    is_active:     bool       # not revoked AND not expired


class TokenListResponse(msgspec.Struct):
    user_id:  int
    username: str
    rows:     list[_TokenRow]


class MintTokenRequest(msgspec.Struct):
    expires_in_days: int = 90
    note:            Optional[str] = None


# ── Investor events (subscription / redemption journal) ────────────

class _EventRow(msgspec.Struct):
    id:           int
    event_type:   str
    event_date:   str           # ISO date
    amount:       float
    nav_per_unit: float
    units_delta:  float
    note:         Optional[str]
    created_at:   str


class EventListResponse(msgspec.Struct):
    user_id:        int
    username:       str
    rows:           list[_EventRow]
    total_units:    float       # sum(units_delta) — current LP unit balance
    total_in:       float       # sum of subscription amounts
    total_out:      float       # sum of redemption amounts


class CreateEventRequest(msgspec.Struct):
    event_type:   str           # 'subscription' | 'redemption' | 'bootstrap'
    event_date:   str           # 'YYYY-MM-DD'
    amount:       float         # positive ₹
    nav_per_unit: float         # positive
    note:         Optional[str] = None


class CreateEventResponse(msgspec.Struct):
    id:           int
    event_type:   str
    event_date:   str
    amount:       float
    nav_per_unit: float
    units_delta:  float


class MintTokenResponse(msgspec.Struct):
    """Returned ONCE on mint. The full token is only ever shown here
    so the admin must copy it immediately; subsequent list calls
    surface only a preview. Same pattern as MCP token mint."""
    id:         int
    token:      str       # full 64-char token — copy to clipboard now
    portal_url: str       # convenience: full URL the admin pastes to LP
    expires_at: str


class InvestorSliceResponse(msgspec.Struct):
    """Public — exposed at /api/investor/{token}/slice."""
    display_name:        str
    share_pct:           float
    contribution:        float
    firm_nav:            float
    nav_share:           float
    pnl:                 float
    pnl_pct:             Optional[float]
    day_delta_share:     Optional[float]
    day_delta_share_pct: Optional[float]
    as_of_date:          Optional[str]


class InvestorHistoryPoint(msgspec.Struct):
    as_of_date: str
    firm_nav:   float
    nav_share:  float
    pnl:        float


class InvestorHistoryResponse(msgspec.Struct):
    rows:         list[InvestorHistoryPoint]
    days:         int
    share_pct:    float
    contribution: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _is_active(row: InvestorToken, now: datetime) -> bool:
    return row.revoked_at is None and row.expires_at > now


async def _resolve_token(token: str) -> tuple[InvestorToken, User]:
    """Look up the token, validate it's active, return (row, user).
    Raises 401 for missing / revoked / expired tokens."""
    now = datetime.now(timezone.utc)
    async with async_session() as s:
        tok_row = (await s.execute(
            select(InvestorToken).where(InvestorToken.token == token)
        )).scalar_one_or_none()
        if tok_row is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        if not _is_active(tok_row, now):
            raise HTTPException(status_code=401, detail="Token revoked or expired")
        user = (await s.execute(
            select(User).where(User.id == tok_row.user_id)
        )).scalar_one_or_none()
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Investor account inactive")
        # Best-effort visit tracking. Don't block the request on a
        # bookkeeping write — separate UPDATE, swallow errors. The
        # admin list "this LP last visited X" can lag by a tick;
        # nobody loses money over it.
        try:
            await s.execute(
                update(InvestorToken)
                  .where(InvestorToken.id == tok_row.id)
                  .values(last_visit_at=now, visit_count=tok_row.visit_count + 1)
            )
            await s.commit()
        except Exception:
            await s.rollback()
        return tok_row, user


def _portal_url(token: str) -> str:
    """Best-effort canonical URL. Frontend can override but we ship
    a reasonable default so the admin's clipboard contains a usable
    URL on mint."""
    # No request context here (mint is admin-triggered) — use a
    # relative URL so it resolves against whichever host the admin
    # is using. The frontend mint-success modal turns this into a
    # full URL via window.location.origin.
    return f"/investor/{token}"


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

class InvestorAdminController(Controller):
    path = "/api/admin/users"

    @get("/{user_id:int}/investor-tokens",
         guards=[cap_guard("manage_investor_tokens")])
    async def list_tokens(self, user_id: int) -> TokenListResponse:
        now = datetime.now(timezone.utc)
        async with async_session() as s:
            user = (await s.execute(
                select(User).where(User.id == user_id)
            )).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")
            rows = (await s.execute(
                select(InvestorToken)
                  .where(InvestorToken.user_id == user_id)
                  .order_by(desc(InvestorToken.created_at))
            )).scalars().all()
        return TokenListResponse(
            user_id=user.id,
            username=user.username,
            rows=[
                _TokenRow(
                    id=r.id,
                    token_preview=(r.token[:8] + "…") if r.token else "—",
                    expires_at=_iso(r.expires_at) or "",
                    revoked_at=_iso(r.revoked_at),
                    last_visit_at=_iso(r.last_visit_at),
                    visit_count=int(r.visit_count or 0),
                    note=r.note,
                    created_at=_iso(r.created_at) or "",
                    is_active=_is_active(r, now),
                )
                for r in rows
            ],
        )

    @post("/{user_id:int}/investor-tokens",
          guards=[cap_guard("manage_investor_tokens")])
    async def mint_token(self, user_id: int,
                         data: MintTokenRequest) -> MintTokenResponse:
        """Mint a new long-lived token for the LP. The full token is
        returned ONCE so the admin must copy + forward immediately;
        subsequent list calls only show the first-8 preview.

        Idempotent enough: minting always creates a new row; the
        operator should revoke the old one if rotating."""
        days = max(1, min(int(data.expires_in_days or 90), 3650))  # cap 10y
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=days)
        # 32 bytes hex = 64 chars. ~128-bit entropy. URL-safe.
        token = secrets.token_hex(32)
        async with async_session() as s:
            user = (await s.execute(
                select(User).where(User.id == user_id)
            )).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")
            row = InvestorToken(
                user_id=user_id,
                token=token,
                expires_at=expires,
                note=(data.note or None),
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
        return MintTokenResponse(
            id=row.id,
            token=token,
            portal_url=_portal_url(token),
            expires_at=_iso(expires) or "",
        )

    @get("/{user_id:int}/statement/{year:int}/{month:int}",
         guards=[cap_guard("manage_investor_tokens")])
    async def admin_statement(self, user_id: int, year: int, month: int) -> Response:
        """Admin PDF preview — same renderer as the public portal,
        gated by manage_investor_tokens so the admin can preview /
        spot-check what the LP will receive. Useful for QA before
        forwarding a portal URL."""
        return await _generate_statement_response(user_id, year, month)

    @get("/{user_id:int}/investor-events",
         guards=[cap_guard("manage_investor_tokens")])
    async def list_events(self, user_id: int) -> EventListResponse:
        """List every subscription / redemption / bootstrap event
        for an LP, ascending by event_date. Surfaces totals so the
        operator can sanity-check capital in/out + the LP's current
        unit balance.

        Currently a passive log — NAV math still uses the v1
        static-share model. The next slice flips the NAV
        computation to consume these events."""
        async with async_session() as s:
            user = (await s.execute(
                select(User).where(User.id == user_id)
            )).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")
            rows = (await s.execute(
                select(InvestorEvent)
                  .where(InvestorEvent.user_id == user_id)
                  .order_by(InvestorEvent.event_date.asc(),
                            InvestorEvent.id.asc())
            )).scalars().all()
        total_units = sum(float(r.units_delta or 0.0) for r in rows)
        total_in    = sum(float(r.amount or 0.0) for r in rows
                          if r.event_type in ("subscription", "bootstrap"))
        total_out   = sum(float(r.amount or 0.0) for r in rows
                          if r.event_type == "redemption")
        return EventListResponse(
            user_id=user.id,
            username=user.username,
            rows=[
                _EventRow(
                    id=r.id,
                    event_type=r.event_type,
                    event_date=r.event_date.isoformat() if r.event_date else "",
                    amount=float(r.amount or 0.0),
                    nav_per_unit=float(r.nav_per_unit or 0.0),
                    units_delta=float(r.units_delta or 0.0),
                    note=r.note,
                    created_at=_iso(r.created_at) or "",
                )
                for r in rows
            ],
            total_units=total_units,
            total_in=total_in,
            total_out=total_out,
        )

    @post("/{user_id:int}/investor-events",
          guards=[cap_guard("manage_investor_tokens")])
    async def create_event(self, user_id: int,
                           data: CreateEventRequest) -> CreateEventResponse:
        """Record a new subscription / redemption / bootstrap event.
        Operator supplies amount + nav_per_unit; backend computes
        signed units_delta. Subscriptions / bootstraps are positive,
        redemptions negative.

        No idempotency key — legitimate same-day same-amount events
        are possible (rare but valid). If the operator submits twice
        by mistake, they can DELETE the duplicate."""
        from datetime import date as _date
        et = (data.event_type or "").strip().lower()
        if et not in ("subscription", "redemption", "bootstrap"):
            raise HTTPException(status_code=400,
                                detail="event_type must be subscription | redemption | bootstrap")
        if data.amount <= 0:
            raise HTTPException(status_code=400, detail="amount must be > 0")
        if data.nav_per_unit <= 0:
            raise HTTPException(status_code=400, detail="nav_per_unit must be > 0")
        try:
            edate = _date.fromisoformat(data.event_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="event_date must be YYYY-MM-DD")

        sign = -1.0 if et == "redemption" else 1.0
        units_delta = sign * data.amount / data.nav_per_unit

        async with async_session() as s:
            user = (await s.execute(
                select(User).where(User.id == user_id)
            )).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")
            row = InvestorEvent(
                user_id=user_id,
                event_type=et,
                event_date=edate,
                amount=float(data.amount),
                nav_per_unit=float(data.nav_per_unit),
                units_delta=float(units_delta),
                note=(data.note or None),
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
        return CreateEventResponse(
            id=row.id,
            event_type=row.event_type,
            event_date=row.event_date.isoformat(),
            amount=float(row.amount),
            nav_per_unit=float(row.nav_per_unit),
            units_delta=float(row.units_delta),
        )

    @delete("/{user_id:int}/investor-events/{event_id:int}",
            guards=[cap_guard("manage_investor_tokens")],
            status_code=204)
    async def delete_event(self, user_id: int, event_id: int) -> None:
        """Remove an event. Operator's escape hatch for fat-finger
        entries. There's no undo — once deleted the row is gone."""
        async with async_session() as s:
            row = (await s.execute(
                select(InvestorEvent).where(
                    InvestorEvent.id == event_id,
                    InvestorEvent.user_id == user_id,
                )
            )).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="Event not found")
            await s.delete(row)
            await s.commit()

    @delete("/{user_id:int}/investor-tokens/{token_id:int}",
            guards=[cap_guard("manage_investor_tokens")],
            status_code=204)
    async def revoke_token(self, user_id: int, token_id: int) -> None:
        """Mark the token revoked. Idempotent — revoking an already
        revoked row is a no-op (revoked_at stays at the first
        revocation timestamp)."""
        now = datetime.now(timezone.utc)
        async with async_session() as s:
            row = (await s.execute(
                select(InvestorToken).where(
                    InvestorToken.id == token_id,
                    InvestorToken.user_id == user_id,
                )
            )).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="Token not found")
            if row.revoked_at is None:
                await s.execute(
                    update(InvestorToken)
                      .where(InvestorToken.id == token_id)
                      .values(revoked_at=now)
                )
                await s.commit()


# ---------------------------------------------------------------------------
# Monthly statement audit + manual trigger
# ---------------------------------------------------------------------------

class _StatementRow(msgspec.Struct):
    """Row shape for /api/admin/statements list view."""
    id:              Optional[int]   # null for PENDING rows (no audit row exists yet)
    user_id:         int
    username:        str
    display_name:    str
    email:           Optional[str]
    share_pct:       float
    period_year:     int
    period_month:    int
    status:          str             # 'sent' | 'failed' | 'pending'
    generated_at:    Optional[str]
    sent_at:         Optional[str]
    recipients:      list[str]
    pdf_size_bytes:  Optional[int]
    error:           Optional[str]


class StatementListResponse(msgspec.Struct):
    period_year:   int
    period_month:  int
    rows:          list[_StatementRow]
    counts:        dict[str, int]    # sent / failed / pending counts


class StatementSendRequest(msgspec.Struct):
    user_id: int
    year:    int
    month:   int


class StatementSendResponse(msgspec.Struct):
    user_id:  int
    year:     int
    month:    int
    status:   str       # 'sent' | 'failed'
    error:    Optional[str]


class InvestorStatementsController(Controller):
    """Cross-LP audit + manual send for the auto-email task."""
    path = "/api/admin/statements"

    @get("/", guards=[cap_guard("manage_investor_tokens")])
    async def list_statements(self, year: int = 0,
                              month: int = 0) -> StatementListResponse:
        """List the statement audit rows for a (year, month) plus
        every eligible LP without a row (PENDING). Without year/
        month, defaults to the prior month.

        Eligible = active + share_pct > 0 + email present, matching
        the bg task's filter so the operator sees exactly what the
        next 02:00 IST wake will process."""
        if not year or not month:
            from datetime import date as _date
            today = datetime.now(timezone.utc).date()
            first_of_this = _date(today.year, today.month, 1)
            prior_end = first_of_this - timedelta(days=1)
            year, month = prior_end.year, prior_end.month

        async with async_session() as s:
            eligible_users = (await s.execute(
                select(User).where(
                    User.is_active.is_(True),
                    User.share_pct > 0,
                    User.email.is_not(None),
                    User.email != "",
                )
            )).scalars().all()
            stmt_rows = (await s.execute(
                select(MonthlyStatement).where(
                    MonthlyStatement.period_year  == year,
                    MonthlyStatement.period_month == month,
                )
            )).scalars().all()

        rows_by_user = {r.user_id: r for r in stmt_rows}
        out: list[_StatementRow] = []
        sent = failed = pending = 0
        for u in eligible_users:
            r = rows_by_user.get(u.id)
            if r is None:
                pending += 1
                out.append(_StatementRow(
                    id=None, user_id=u.id, username=u.username,
                    display_name=u.display_name or u.username,
                    email=u.email, share_pct=float(u.share_pct),
                    period_year=year, period_month=month,
                    status="pending",
                    generated_at=None, sent_at=None,
                    recipients=[], pdf_size_bytes=None, error=None,
                ))
            else:
                status = "sent" if r.sent_at is not None else "failed"
                if status == "sent": sent += 1
                else: failed += 1
                out.append(_StatementRow(
                    id=r.id, user_id=u.id, username=u.username,
                    display_name=u.display_name or u.username,
                    email=u.email, share_pct=float(u.share_pct),
                    period_year=year, period_month=month,
                    status=status,
                    generated_at=_iso(r.generated_at),
                    sent_at=_iso(r.sent_at),
                    recipients=list(r.recipients_json or []),
                    pdf_size_bytes=r.pdf_size_bytes,
                    error=r.error,
                ))

        # Audit rows whose user is no longer eligible (changed share_pct
        # to 0, deactivated, dropped email) — still surface them so the
        # operator can see "this LP got a statement before; their row
        # still exists." Tagged with status from the audit row.
        eligible_ids = {u.id for u in eligible_users}
        for r in stmt_rows:
            if r.user_id in eligible_ids:
                continue
            async with async_session() as s:
                u = (await s.execute(
                    select(User).where(User.id == r.user_id)
                )).scalar_one_or_none()
            if u is None:
                continue
            status = "sent" if r.sent_at is not None else "failed"
            if status == "sent": sent += 1
            else: failed += 1
            out.append(_StatementRow(
                id=r.id, user_id=u.id, username=u.username,
                display_name=u.display_name or u.username,
                email=u.email, share_pct=float(u.share_pct),
                period_year=year, period_month=month,
                status=status,
                generated_at=_iso(r.generated_at),
                sent_at=_iso(r.sent_at),
                recipients=list(r.recipients_json or []),
                pdf_size_bytes=r.pdf_size_bytes,
                error=r.error,
            ))

        # Stable sort: pending first (need attention), then failed,
        # then sent; within group alphabetical by username.
        order = {"pending": 0, "failed": 1, "sent": 2}
        out.sort(key=lambda r: (order.get(r.status, 9), r.username))

        return StatementListResponse(
            period_year=year, period_month=month, rows=out,
            counts={"sent": sent, "failed": failed, "pending": pending},
        )

    @post("/send", guards=[cap_guard("manage_investor_tokens")])
    async def manual_send(self, data: StatementSendRequest) -> StatementSendResponse:
        """Manual trigger for a single (user, period). Inserts the
        audit row directly; the bg task ignores users whose rows
        already exist, so this is the operator's way to short-circuit
        the daily 02:00 IST wait or retry a failed send.

        Idempotent failure path: if a row already exists, returns
        the existing status without re-sending."""
        async with async_session() as s:
            user = (await s.execute(
                select(User).where(User.id == data.user_id)
            )).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")
            existing = (await s.execute(
                select(MonthlyStatement).where(
                    MonthlyStatement.user_id == data.user_id,
                    MonthlyStatement.period_year  == data.year,
                    MonthlyStatement.period_month == data.month,
                )
            )).scalar_one_or_none()
        if existing is not None:
            status = "sent" if existing.sent_at is not None else "failed"
            return StatementSendResponse(
                user_id=data.user_id, year=data.year, month=data.month,
                status=status,
                error=("Already exists. Delete the row to retry."
                       if status == "failed" else None),
            )

        # Delegate to the bg helper so the audit-row + email logic
        # stays in one place. Imported lazily to avoid a hard import
        # cycle through background.py.
        from backend.api.background import _send_one_monthly_statement
        await _send_one_monthly_statement(user, data.year, data.month)

        async with async_session() as s:
            row = (await s.execute(
                select(MonthlyStatement).where(
                    MonthlyStatement.user_id == data.user_id,
                    MonthlyStatement.period_year  == data.year,
                    MonthlyStatement.period_month == data.month,
                )
            )).scalar_one_or_none()
        status = "failed"
        error = "Audit row missing after send"
        if row is not None:
            status = "sent" if row.sent_at is not None else "failed"
            error = row.error
        return StatementSendResponse(
            user_id=data.user_id, year=data.year, month=data.month,
            status=status, error=error,
        )

    @delete("/{row_id:int}",
            guards=[cap_guard("manage_investor_tokens")],
            status_code=204)
    async def delete_row(self, row_id: int) -> None:
        """Clear an audit row. The next bg wake (or manual send) will
        re-process the LP. Use this when a send failed and the error
        has been resolved (e.g. operator fixed the LP's email)."""
        async with async_session() as s:
            row = (await s.execute(
                select(MonthlyStatement).where(MonthlyStatement.id == row_id)
            )).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="Row not found")
            await s.delete(row)
            await s.commit()


# ---------------------------------------------------------------------------
# Public endpoints (token in URL — no auth gate)
# ---------------------------------------------------------------------------

class InvestorPortalController(Controller):
    """Public-facing endpoints. The token is the credential.

    Rate limiting / abuse protection is delegated to Cloudflare in
    front of the host. A token in the URL is no different from a
    long-lived API key from a security standpoint; if it leaks,
    admin revokes."""
    path = "/api/investor"

    @get("/{token:str}/slice")
    async def slice(self, token: str) -> InvestorSliceResponse:
        tok_row, user = await _resolve_token(token)
        async with async_session() as s:
            rows = (await s.execute(
                select(NavDaily)
                  .order_by(desc(NavDaily.as_of_date))
                  .limit(2)
            )).scalars().all()
        share_pct = float(user.share_pct or 0.0)
        contribution = float(user.contribution or 0.0)
        firm_nav = float(rows[0].nav) if rows else 0.0
        nav_share = (share_pct / 100.0) * firm_nav
        pnl = nav_share - contribution
        pnl_pct: Optional[float] = (pnl / contribution) if contribution > 0 else None
        day_delta_share: Optional[float] = None
        day_delta_share_pct: Optional[float] = None
        if len(rows) >= 2:
            firm_delta = float(rows[0].nav) - float(rows[1].nav)
            day_delta_share = firm_delta * (share_pct / 100.0)
            prior_share = float(rows[1].nav) * (share_pct / 100.0)
            day_delta_share_pct = (day_delta_share / prior_share) if prior_share else None
        as_of = rows[0].as_of_date.isoformat() if rows else None
        return InvestorSliceResponse(
            display_name=user.display_name or user.username,
            share_pct=share_pct,
            contribution=contribution,
            firm_nav=firm_nav,
            nav_share=round(nav_share, 2),
            pnl=round(pnl, 2),
            pnl_pct=pnl_pct,
            day_delta_share=(round(day_delta_share, 2)
                             if day_delta_share is not None else None),
            day_delta_share_pct=day_delta_share_pct,
            as_of_date=as_of,
        )

    @get("/{token:str}/statement/{year:int}/{month:int}")
    async def statement(self, token: str, year: int, month: int) -> Response:
        """Monthly PDF statement for the LP. Token in URL is the
        credential; same active-check as /slice + /history. Returns
        binary PDF with Content-Disposition: attachment so the
        browser saves with the canonical filename.

        Stateless — re-generated on each request. No `monthly_
        statements` table yet; auto-email + DB persistence land in
        the next slice."""
        _tok, user = await _resolve_token(token)
        return await _generate_statement_response(user.id, year, month)

    @get("/{token:str}/history")
    async def history(self, token: str,
                      days: int = 90) -> InvestorHistoryResponse:
        days = max(1, min(int(days or 90), 1825))
        _tok, user = await _resolve_token(token)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        async with async_session() as s:
            rows = (await s.execute(
                select(NavDaily)
                  .where(NavDaily.as_of_date >= cutoff)
                  .order_by(NavDaily.as_of_date.asc())
            )).scalars().all()
        share_pct = float(user.share_pct or 0.0)
        contribution = float(user.contribution or 0.0)
        ratio = share_pct / 100.0
        out: list[InvestorHistoryPoint] = []
        for r in rows:
            firm_nav = float(r.nav or 0.0)
            nav_share = firm_nav * ratio
            out.append(InvestorHistoryPoint(
                as_of_date=r.as_of_date.isoformat() if r.as_of_date else "",
                firm_nav=firm_nav,
                nav_share=round(nav_share, 2),
                pnl=round(nav_share - contribution, 2),
            ))
        return InvestorHistoryResponse(
            rows=out, days=days,
            share_pct=share_pct, contribution=contribution,
        )


# ---------------------------------------------------------------------------
# Shared PDF helper (admin preview + public LP path use the same renderer)
# ---------------------------------------------------------------------------

async def _generate_statement_response(user_id: int, year: int, month: int) -> Response:
    """Compute + render the statement and wrap in a Litestar Response
    with download headers. Heavy CPU work (fpdf2 layout) runs in a
    thread so we don't block the event loop on a slow render."""
    if year < 2020 or year > 2100 or month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Invalid period")
    from backend.api.algo.investor_statement import (
        compute_statement, render_statement_pdf,
    )
    data = await compute_statement(user_id, year, month)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail="No NAV data for this period yet",
        )
    pdf_bytes = await asyncio.to_thread(render_statement_pdf, data)
    filename = f"ramboquant_{year:04d}_{month:02d}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control":       "no-store",
        },
    )
