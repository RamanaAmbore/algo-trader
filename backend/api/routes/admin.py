"""
Admin-only endpoints.

GET  /api/admin/logs               — tail the app log file (last N lines)
POST /api/admin/exec               — run a shell command and return output
GET  /api/admin/users              — list all users (no password hashes)
DELETE /api/admin/users/{username}  — deactivate a user
GET  /api/admin/pnl/range          — date-range P&L breakdown
POST /api/admin/pnl/upload-csv     — backfill daily_book from Kite Console CSV

All routes require admin JWT via admin_guard.
"""

import io
import os
import subprocess
from datetime import date as dt_date
from pathlib import Path
from typing import Any

import msgspec
from litestar import Controller, Request, delete, get, post, put
from litestar.datastructures import UploadFile
from litestar.enums import RequestEncodingType
from litestar.exceptions import HTTPException
from litestar.params import Body
from sqlalchemy import select, text

from backend.api.auth_guard import admin_guard, designated_guard
from backend.api.database import async_session
from backend.api.models import User
from backend.shared.helpers.alert_utils import refresh_alert_recipients
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config

logger = get_logger(__name__)

_LOG_PREFIX = os.environ.get("RAMBOQ_LOG_PREFIX", "")
_raw_path = Path(config.get("file_log_file", ".log/log_file"))
_LOG_FILE = _raw_path.with_name(_LOG_PREFIX + _raw_path.name)


def _resolve_log() -> Path:
    return _LOG_FILE if _LOG_FILE.is_absolute() else Path.cwd() / _LOG_FILE


# ---------------------------------------------------------------------------
# Schemas (msgspec)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# P&L range schemas
# ---------------------------------------------------------------------------

class PnlRangeResponse(msgspec.Struct):
    from_date: str
    to_date: str
    segment: str
    kind: str
    by_segment: list[dict]    # [{segment, total_pnl, day_pnl, n_rows}]
    by_account: list[dict]    # [{account, segment, kind, total_pnl, day_pnl, n_rows}]
    by_symbol:  list[dict]    # [{symbol, segment, total_pnl, day_pnl, n_rows}] top-50
    daily_series: list[dict]  # [{date, total_pnl, day_pnl, pct_change_from_start}]
    summary: dict             # {total_pnl, day_pnl, n_dates, n_accounts}
    start_capital: float | None = None  # sum |qty*avg_cost| on from_date; None if no rows


class PnlCsvUploadResponse(msgspec.Struct):
    inserted: int
    updated: int
    skipped: int
    sample: list[dict]


# ---------------------------------------------------------------------------
# Benchmark series schemas
# ---------------------------------------------------------------------------

class PnlBenchmarkSeries(msgspec.Struct):
    symbol: str
    name: str
    closes: list[dict]   # [{date, close, pct_change_from_start}]


class PnlBenchmarkResponse(msgspec.Struct):
    from_date: str
    to_date: str
    series: list[PnlBenchmarkSeries]


# Map of user-facing name → (kite_symbol_label, instrument_token).
# Tokens verified against Kite's NSE/BSE index instrument dump (2025).
# NSE indices: NIFTY 50 = 256265, BANK NIFTY = 260105,
#              NIFTY MIDCAP 100 = 259849, NIFTY SMALLCAP 100 = 256777
# BSE index:   SENSEX = 265
BENCHMARK_TOKENS: dict[str, tuple[str, int]] = {
    "NIFTY 50":           ("NSE:NIFTY 50",           256265),
    "BANK NIFTY":         ("NSE:NIFTY BANK",          260105),
    "NIFTY MIDCAP 100":   ("NSE:NIFTY MIDCAP 100",    259849),
    "NIFTY SMALLCAP 100": ("NSE:NIFTY SMALLCAP 100",  256777),
    "SENSEX":             ("BSE:SENSEX",               265),
}

# In-process daily cache: (symbol, from_date, to_date) → list[dict]
# Purged at midnight — keyed by today's date so stale entries auto-miss.
_BENCHMARK_CACHE: dict[tuple[str, str, str, str], list[dict]] = {}


def _benchmark_cache_key(symbol: str, from_str: str, to_str: str) -> tuple[str, str, str, str]:
    from backend.shared.helpers.date_time_utils import timestamp_indian
    today = timestamp_indian().date().isoformat()
    return (symbol, from_str, to_str, today)


class SnapshotRequest(msgspec.Struct):
    date: str  # ISO format: YYYY-MM-DD or 'today'


class SnapshotResponse(msgspec.Struct):
    accounts: list[str]
    holdings_rows: int
    positions_rows: int
    trades_rows: int
    errors: list[str]


class ExecRequest(msgspec.Struct):
    command: str


class ExecResponse(msgspec.Struct):
    stdout: str
    stderr: str
    returncode: int


class LogsResponse(msgspec.Struct):
    lines: list[str]
    path: str


class UserInfo(msgspec.Struct):
    id: int
    account_id: str
    username: str
    role: str
    display_name: str
    email: str | None = None
    phone: str | None = None
    pan: str | None = None
    date_of_birth: str | None = None
    kyc_verified: bool = False
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    contribution: float = 0.0
    contribution_date: str | None = None
    share_pct: float = 0.0
    bank_name: str | None = None
    bank_account: str | None = None
    bank_ifsc: str | None = None
    nominee_name: str | None = None
    nominee_relation: str | None = None
    nominee_phone: str | None = None
    is_approved: bool = False
    is_active: bool = True
    receive_alerts: bool = False
    email_verified: bool = False
    suspended_at: str | None = None
    terminated_at: str | None = None
    join_date: str | None = None
    notes: str | None = None


class UsersResponse(msgspec.Struct):
    users: list[UserInfo]


class CreateUserRequest(msgspec.Struct):
    username: str
    password: str
    display_name: str
    email: str = ""
    phone: str = ""
    role: str = "partner"
    contribution: float = 0.0
    share_pct: float = 0.0
    is_approved: bool = True
    receive_alerts: bool = False


class UpdateUserRequest(msgspec.Struct):
    display_name: str | None = None
    role: str | None = None
    receive_alerts: bool | None = None
    email_verified: bool | None = None
    email: str | None = None
    phone: str | None = None
    pan: str | None = None
    date_of_birth: str | None = None
    kyc_verified: bool | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    contribution: float | None = None
    contribution_date: str | None = None
    share_pct: float | None = None
    bank_name: str | None = None
    bank_account: str | None = None
    bank_ifsc: str | None = None
    nominee_name: str | None = None
    nominee_relation: str | None = None
    nominee_phone: str | None = None
    join_date: str | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class AdminController(Controller):
    path = "/api/admin"
    guards = [admin_guard]

    @get("/logs")
    async def get_logs(self, n: int = 200) -> LogsResponse:
        log_path = _resolve_log()
        if not log_path.exists():
            return LogsResponse(lines=["Log file not found"], path=str(log_path))
        try:
            result = subprocess.run(
                ["tail", f"-{min(n, 2000)}", str(log_path)],
                capture_output=True, text=True, timeout=10,
            )
            return LogsResponse(lines=result.stdout.splitlines(), path=str(log_path))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @post("/exec")
    async def exec_command(self, data: ExecRequest) -> ExecResponse:
        cmd = data.command.strip()
        if not cmd:
            raise HTTPException(status_code=422, detail="Empty command")
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30,
                cwd=str(Path(__file__).parent.parent.parent),
            )
            logger.info(f"Admin exec: {cmd!r} → rc={result.returncode}")
            return ExecResponse(
                stdout=result.stdout[-8000:] if len(result.stdout) > 8000 else result.stdout,
                stderr=result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
                returncode=result.returncode,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=408, detail="Command timed out (30s)")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @get("/users")
    async def list_users(self) -> UsersResponse:
        # Both admin and designated see every user row. The action gate
        # (_check_action) restricts what each tier can DO — not what they
        # can SEE. Admins need full visibility to know which partners
        # exist when running password-reset support.
        async with async_session() as session:
            result = await session.execute(select(User).order_by(User.id))
            users = result.scalars().all()
        def _to_info(u):
            return UserInfo(
                id=u.id, account_id=u.account_id, username=u.username, role=u.role,
                display_name=u.display_name, email=u.email, phone=u.phone,
                pan=u.pan, date_of_birth=str(u.date_of_birth) if u.date_of_birth else None,
                kyc_verified=u.kyc_verified,
                address_line1=u.address_line1, address_line2=u.address_line2,
                city=u.city, state=u.state, pincode=u.pincode,
                contribution=u.contribution,
                contribution_date=str(u.contribution_date) if u.contribution_date else None,
                share_pct=u.share_pct,
                bank_name=u.bank_name, bank_account=u.bank_account, bank_ifsc=u.bank_ifsc,
                nominee_name=u.nominee_name, nominee_relation=u.nominee_relation,
                nominee_phone=u.nominee_phone,
                is_approved=u.is_approved, is_active=u.is_active,
                receive_alerts=getattr(u, 'receive_alerts', False),
                email_verified=getattr(u, 'email_verified', False),
                suspended_at=u.suspended_at.isoformat() if getattr(u, 'suspended_at', None) else None,
                terminated_at=u.terminated_at.isoformat() if getattr(u, 'terminated_at', None) else None,
                join_date=str(u.join_date) if u.join_date else None, notes=u.notes,
            )
        return UsersResponse(users=[_to_info(u) for u in users])

    @post("/users")
    async def create_user(self, data: CreateUserRequest) -> dict:
        """Admin creates a user (pre-approved). Share password via other channel."""
        from backend.api.routes.auth import hash_password
        from backend.shared.helpers.utils import validate_password_standard
        ok, msg = validate_password_standard(data.password)
        if not ok:
            raise HTTPException(status_code=422, detail=msg)
        async with async_session() as session:
            existing = await session.execute(
                select(User).where(User.username == data.username)
            )
            if existing.scalar_one_or_none():
                raise HTTPException(status_code=409, detail="Username already exists")
            user = User(
                username=data.username,
                password_hash=hash_password(data.password),
                role=data.role,
                display_name=data.display_name or data.username,
                email=data.email or None,
                phone=data.phone or None,
                contribution=data.contribution,
                share_pct=data.share_pct,
                is_approved=data.is_approved,
                receive_alerts=data.receive_alerts,
            )
            session.add(user)
            await session.commit()
        await refresh_alert_recipients()
        logger.info(f"Admin: created user {data.username!r} role={data.role}")
        return {"detail": f"User {data.username!r} created"}

    @put("/users/{username:str}/approve", status_code=200)
    async def approve_user(self, username: str, request: Request) -> dict:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            self._check_action(request, user, designated_only=True)
            user.is_approved = True
            await session.commit()
        await refresh_alert_recipients()
        logger.info(f"Admin: approved user {username!r}")
        return {"detail": f"User {username!r} approved"}

    @put("/users/{username:str}/reject", status_code=200)
    async def reject_user(self, username: str, request: Request) -> dict:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            self._check_action(request, user, designated_only=True)
            user.is_approved = False
            user.is_active = False
            await session.commit()
        await refresh_alert_recipients()
        logger.info(f"Admin: rejected user {username!r}")
        return {"detail": f"User {username!r} rejected"}

    @put("/users/{username:str}")
    async def update_user(self, username: str, data: UpdateUserRequest, request: Request) -> dict:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            self._check_action(request, user, admin_self_ok=True)
            # `role` only flows through this endpoint for designated
            # actors. An admin self-editing their own row would otherwise
            # be able to PATCH role to 'designated' (self-elevation) or
            # to 'partner' (self-demote). Promote/Demote between admin
            # and designated has its own audited route (toggle-designated).
            payload = getattr(request.state, "token_payload", {}) or {}
            actor_role = payload.get("role", "")
            allowed_role_change = (actor_role == "designated")

            # Apply all non-None fields from the request
            for field in (
                'display_name', 'role', 'receive_alerts', 'email_verified',
                'email', 'phone', 'pan',
                'kyc_verified', 'address_line1', 'address_line2',
                'city', 'state', 'pincode', 'contribution', 'contribution_date',
                'share_pct', 'bank_name', 'bank_account', 'bank_ifsc',
                'nominee_name', 'nominee_relation', 'nominee_phone',
                'join_date', 'notes',
            ):
                val = getattr(data, field, None)
                if val is None:
                    continue
                if field == 'role':
                    # Privilege-changing field — designated only.
                    if not allowed_role_change:
                        continue
                    if val not in ("partner", "admin", "designated"):
                        raise HTTPException(
                            status_code=422,
                            detail="role must be 'partner', 'admin', or 'designated'",
                        )
                if field == 'email_verified':
                    # Manually flipping email_verified bypasses the
                    # email-token flow — designated only so an admin
                    # can't approve a partner's account by toggling
                    # this for them and then approving.
                    if not allowed_role_change:
                        continue
                if field == 'pan':
                    val = val.upper()
                if field in ('date_of_birth', 'join_date', 'contribution_date'):
                    from datetime import date as dt_date
                    val = dt_date.fromisoformat(val) if isinstance(val, str) else val
                setattr(user, field, val)
            # Bump token_version when role or username flow could grant
            # new privileges; safest is to bump on any role mutation so
            # any active JWT for the affected user gets re-issued on the
            # next request via jwt_guard.
            if data.role is not None and allowed_role_change:
                user.token_version = (user.token_version or 1) + 1
            await session.commit()
        await refresh_alert_recipients()
        logger.info(f"Admin: updated user {username!r}")
        return {"detail": f"User {username!r} updated"}

    @staticmethod
    def _check_action(
        request: Request,
        target: User,
        *,
        designated_only: bool = False,
        admin_partner_ok: bool = False,
        admin_self_ok: bool = False,
        block_self: bool = False,
    ) -> None:
        """Centralized permission gate. Flags pick the policy for each
        route — see the action matrix in the controller doc string.

        - designated_only=True  → only role='designated' can perform.
        - admin_partner_ok=True → admin can perform on partner targets.
        - admin_self_ok=True    → admin can perform on their own row.
        - block_self=True       → reject if target == actor (used for
                                  every destructive action so the actor
                                  can't lock themselves out).

        Designated bypasses every check except `block_self`. Admin gets
        through only when one of the admin_* flags lets it. Anyone else
        (shouldn't happen — admin_guard already filters partners) gets
        a blanket 403.
        """
        payload = getattr(request.state, "token_payload", {}) or {}
        role    = payload.get("role", "partner")
        actor   = payload.get("sub", "")
        is_self = (target.username == actor)

        if block_self and is_self:
            raise HTTPException(
                status_code=403,
                detail="Cannot perform this action on your own account",
            )

        if role == "designated":
            return  # designated does everything (subject to block_self above)

        if role == "admin":
            if designated_only:
                raise HTTPException(status_code=403, detail="Designated-admin access required")
            if admin_self_ok and is_self:
                return
            if admin_partner_ok and target.role == "partner":
                return
            raise HTTPException(
                status_code=403,
                detail="Admin cannot perform this action on this target",
            )

        raise HTTPException(status_code=403, detail="Permission denied")

    @put("/users/{username:str}/suspend", status_code=200)
    async def suspend_user(self, username: str, request: Request) -> dict:
        """Suspend a user — reversible. Sets suspended_at + bumps
        token_version so any live JWTs are invalidated. is_active stays
        True so the row remains addressable; login will reject with
        403 'Account suspended' until reinstated."""
        from datetime import datetime as _dt, timezone as _tz
        async with async_session() as session:
            result = await session.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            self._check_action(request, user, designated_only=True, block_self=True)
            user.suspended_at = _dt.now(_tz.utc)
            user.token_version = (user.token_version or 1) + 1
            await session.commit()
        await refresh_alert_recipients()
        logger.info(f"Admin: suspended user {username!r}")
        return {"detail": f"User {username!r} suspended"}

    @put("/users/{username:str}/reinstate", status_code=200)
    async def reinstate_user(self, username: str, request: Request) -> dict:
        """Clear suspended_at — user can log in again. token_version
        already bumped at suspend time; any old JWTs stay invalid until
        the user requests a new one."""
        async with async_session() as session:
            result = await session.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            self._check_action(request, user, designated_only=True)
            user.suspended_at = None
            await session.commit()
        await refresh_alert_recipients()
        logger.info(f"Admin: reinstated user {username!r}")
        return {"detail": f"User {username!r} reinstated"}

    @put("/users/{username:str}/terminate", status_code=200, guards=[designated_guard])
    async def terminate_user(self, username: str, request: Request) -> dict:
        """Terminal — sets terminated_at + is_active=False + bumps
        token_version. Distinct from reject: rejected users were never
        approved; terminated users were active and have been removed.
        Row is preserved for audit; admin must restore via DB to undo."""
        from datetime import datetime as _dt, timezone as _tz
        async with async_session() as session:
            result = await session.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            if user.role == "designated":
                raise HTTPException(status_code=403, detail="Cannot terminate a designated-admin")
            self._check_action(request, user, designated_only=True, block_self=True)
            user.terminated_at = _dt.now(_tz.utc)
            user.is_active = False
            user.token_version = (user.token_version or 1) + 1
            await session.commit()
        await refresh_alert_recipients()
        logger.info(f"Admin: terminated user {username!r}")
        return {"detail": f"User {username!r} terminated"}

    @put("/users/{username:str}/toggle-designated", status_code=200, guards=[designated_guard])
    async def toggle_designated(self, username: str, data: dict, request: Request) -> dict:
        """Designated-admin only — flip a user's role between admin and
        designated. Promoting an admin to designated is a deliberate,
        audited action. Body: {designated: bool}.
        - True  → role = 'designated'
        - False → role = 'admin'  (caller cannot demote a partner this way)
        """
        make_designated = bool((data or {}).get("designated", False))
        async with async_session() as session:
            result = await session.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            self._check_action(request, user, designated_only=True, block_self=True)
            if user.role == "partner":
                raise HTTPException(
                    status_code=400,
                    detail="Promote a partner to admin via the role field on /users/{u} first",
                )
            user.role = "designated" if make_designated else "admin"
            user.token_version = (user.token_version or 1) + 1
            await session.commit()
        await refresh_alert_recipients()
        logger.info(f"Admin: set role={user.role!r} on {username!r}")
        return {"detail": f"User {username!r} role = {user.role!r}"}

    @put("/users/{username:str}/reset-password", status_code=200)
    async def reset_password(self, username: str, data: dict, request: Request) -> dict:
        """Admin-issued password reset. Validates the new password
        against the standard, hashes with a fresh os.urandom salt,
        bumps token_version to invalidate every live JWT for the user.
        Body: {password: <new>}."""
        from backend.api.routes.auth import hash_password
        from backend.shared.helpers.utils import validate_password_standard
        new_pw = (data or {}).get("password") or ""
        ok, msg = validate_password_standard(new_pw)
        if not ok:
            raise HTTPException(status_code=422, detail=msg)
        async with async_session() as session:
            result = await session.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            self._check_action(request, user, admin_partner_ok=True, block_self=True)
            user.password_hash = hash_password(new_pw)
            user.token_version = (user.token_version or 1) + 1
            await session.commit()
        logger.info(f"Admin: reset password for {username!r}")
        return {"detail": f"Password reset for {username!r}"}

    @post("/users/{username:str}/resend-verification", status_code=200)
    async def resend_verification(self, username: str, request: Request) -> dict:
        """Mint a fresh verify-email token for `username` and dispatch
        the email. Useful when a user lost the original link or the
        token expired (60-min TTL). Permission gate matches the rest:

          - designated → can resend for anyone
          - admin      → can resend only for partners
          - never on self (block_self prevents an admin re-issuing
            their own verify, which would be pointless anyway since
            admin/designated bypass the email-verify gate)
        """
        from datetime import datetime, timedelta, timezone
        import secrets as _s
        from backend.api.models import AuthToken
        from backend.api.routes.auth import (
            _send_verify_email, _VERIFY_TTL_MINUTES, _dispatch_async,
        )

        async with async_session() as session:
            result = await session.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            self._check_action(request, user, admin_partner_ok=True, block_self=True)
            if not user.email:
                raise HTTPException(
                    status_code=422,
                    detail=f"User {username!r} has no email on file",
                )
            if user.email_verified:
                raise HTTPException(
                    status_code=409,
                    detail=f"User {username!r} is already verified",
                )
            tok = _s.token_hex(32)
            session.add(AuthToken(
                user_id=user.id, purpose="verify", token=tok,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=_VERIFY_TTL_MINUTES),
            ))
            await session.commit()
            email = user.email
            display_name = user.display_name
        _dispatch_async(_send_verify_email, display_name, email, tok)
        logger.info(f"Admin: verify email re-sent to {email} for {username!r}")
        return {"detail": f"Verification email sent to {email}"}

    # ------------------------------------------------------------------
    # P&L range endpoint
    # ------------------------------------------------------------------

    @get("/pnl/range")
    async def pnl_range(
        self,
        from_date: str | None = None,
        to_date:   str | None = None,
        segment:   str = "all",
        kind:      str = "all",
    ) -> PnlRangeResponse:
        """
        Date-range P&L breakdown from the daily_book table.

        Query params:
          from=YYYY-MM-DD   (default: today IST)
          to=YYYY-MM-DD     (default: today IST)
          segment=all|equity|commodity|currency|derivatives
          kind=all|holdings|positions

        Each daily_series row carries pct_change_from_start: cumulative
        day_pnl as a % of start_capital (sum of |qty*avg_cost| across
        holdings+positions rows on from_date).  Both fields are None when
        from_date has no daily_book rows (e.g. the date is a weekend or
        predates the snapshot history) — the frontend silently omits the
        portfolio line in that case.
        """
        from backend.shared.helpers.date_time_utils import timestamp_indian

        today_str = timestamp_indian().date().isoformat()
        from_str = (from_date or "").strip() or today_str
        to_str   = (to_date   or "").strip() or today_str

        try:
            d_from = dt_date.fromisoformat(from_str)
            d_to   = dt_date.fromisoformat(to_str)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid date format: {exc}")

        if d_from > d_to:
            raise HTTPException(
                status_code=422,
                detail=f"from ({from_str}) must be <= to ({to_str})",
            )

        seg_filter  = segment.strip().lower()
        kind_filter = kind.strip().lower()

        # Build optional WHERE clauses
        seg_clause  = " AND segment = :segment"  if seg_filter  != "all" else ""
        kind_clause = " AND kind = :kind"         if kind_filter != "all" else ""
        base_where  = f"date BETWEEN :d_from AND :d_to{seg_clause}{kind_clause}"
        # Exclude 'trades' kind from P&L aggregations (no pnl columns)
        pnl_where   = f"{base_where} AND kind != 'trades'"

        params: dict[str, Any] = {"d_from": d_from, "d_to": d_to}
        if seg_filter  != "all": params["segment"] = seg_filter
        if kind_filter != "all": params["kind"]    = kind_filter

        # ------------------------------------------------------------------
        # Helper: cast to float safely
        # ------------------------------------------------------------------
        def _f(v: Any) -> float:
            try:
                return float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        async with async_session() as session:

            # 1 — by_segment
            seg_sql = text(f"""
                SELECT segment,
                       SUM(total_pnl) AS total_pnl,
                       SUM(day_pnl)   AS day_pnl,
                       COUNT(*)       AS n_rows
                FROM daily_book
                WHERE {pnl_where}
                GROUP BY segment
                ORDER BY segment
            """)
            seg_rows = (await session.execute(seg_sql, params)).fetchall()
            by_segment = [
                {
                    "segment":   r[0],
                    "total_pnl": _f(r[1]),
                    "day_pnl":   _f(r[2]),
                    "n_rows":    int(r[3]),
                }
                for r in seg_rows
            ]

            # 2 — by_account  (latest-snapshot total_pnl per (account,symbol)
            #                  + sum of day_pnl across the range)
            acct_sql = text(f"""
                SELECT account,
                       segment,
                       kind,
                       SUM(total_pnl) AS total_pnl,
                       SUM(day_pnl)   AS day_pnl,
                       COUNT(*)       AS n_rows
                FROM daily_book
                WHERE {pnl_where}
                GROUP BY account, segment, kind
                ORDER BY account, segment, kind
            """)
            acct_rows = (await session.execute(acct_sql, params)).fetchall()
            by_account = [
                {
                    "account":   r[0],
                    "segment":   r[1],
                    "kind":      r[2],
                    "total_pnl": _f(r[3]),
                    "day_pnl":   _f(r[4]),
                    "n_rows":    int(r[5]),
                }
                for r in acct_rows
            ]

            # 3 — by_symbol  top-50 by abs(total_pnl)
            sym_sql = text(f"""
                SELECT symbol,
                       segment,
                       SUM(total_pnl) AS total_pnl,
                       SUM(day_pnl)   AS day_pnl,
                       COUNT(*)       AS n_rows
                FROM daily_book
                WHERE {pnl_where}
                GROUP BY symbol, segment
                ORDER BY ABS(SUM(total_pnl)) DESC
                LIMIT 50
            """)
            sym_rows = (await session.execute(sym_sql, params)).fetchall()
            by_symbol = [
                {
                    "symbol":    r[0],
                    "segment":   r[1],
                    "total_pnl": _f(r[2]),
                    "day_pnl":   _f(r[3]),
                    "n_rows":    int(r[4]),
                }
                for r in sym_rows
            ]

            # 4 — daily_series  one row per date
            daily_sql = text(f"""
                SELECT date,
                       SUM(total_pnl) AS total_pnl,
                       SUM(day_pnl)   AS day_pnl
                FROM daily_book
                WHERE {pnl_where}
                GROUP BY date
                ORDER BY date
            """)
            daily_rows = (await session.execute(daily_sql, params)).fetchall()

            # 4b — start_capital: sum of |qty * avg_cost| on from_date for
            #       holdings + positions rows (trades excluded).  Applied
            #       after segment/kind filters so it reflects the same slice
            #       the user is viewing.  Falls back to None when no rows
            #       exist on from_date (weekend, pre-history, etc.).
            cap_seg_clause  = " AND segment = :segment" if seg_filter  != "all" else ""
            cap_kind_clause = " AND kind = :kind"        if kind_filter != "all" else ""
            cap_kind_base   = f" AND kind IN ('holdings','positions')"
            cap_sql = text(f"""
                SELECT SUM(ABS(COALESCE(qty, 0) * COALESCE(avg_cost, 0)))
                FROM daily_book
                WHERE date = :d_from
                  {cap_kind_base}
                  {cap_seg_clause}
                  {cap_kind_clause}
            """)
            cap_row = (await session.execute(cap_sql, params)).fetchone()
            raw_cap = cap_row[0] if cap_row else None
            start_capital: float | None = float(raw_cap) if raw_cap is not None else None

            # 4c — derive pct_change_from_start via cumulative day_pnl / start_capital
            daily_series: list[dict] = []
            cum: float = 0.0
            for r in daily_rows:
                day_pnl_val = _f(r[2])
                cum += day_pnl_val
                if start_capital and start_capital > 0:
                    pct: float | None = round((cum / start_capital) * 100, 4)
                else:
                    pct = None
                daily_series.append({
                    "date":                   str(r[0]),
                    "total_pnl":              _f(r[1]),
                    "day_pnl":                day_pnl_val,
                    "pct_change_from_start":  pct,
                })

            # 5 — summary
            summ_sql = text(f"""
                SELECT SUM(total_pnl)           AS total_pnl,
                       SUM(day_pnl)             AS day_pnl,
                       COUNT(DISTINCT date)     AS n_dates,
                       COUNT(DISTINCT account)  AS n_accounts
                FROM daily_book
                WHERE {pnl_where}
            """)
            summ = (await session.execute(summ_sql, params)).fetchone()

        summary: dict[str, Any] = {
            "total_pnl":  _f(summ[0]) if summ else 0.0,
            "day_pnl":    _f(summ[1]) if summ else 0.0,
            "n_dates":    int(summ[2]) if summ and summ[2] else 0,
            "n_accounts": int(summ[3]) if summ and summ[3] else 0,
        }

        return PnlRangeResponse(
            from_date=from_str,
            to_date=to_str,
            segment=seg_filter,
            kind=kind_filter,
            by_segment=by_segment,
            by_account=by_account,
            by_symbol=by_symbol,
            daily_series=daily_series,
            summary=summary,
            start_capital=start_capital,
        )

    # ------------------------------------------------------------------
    # P&L benchmark series
    # ------------------------------------------------------------------

    @get("/pnl/benchmarks")
    async def pnl_benchmarks(
        self,
        from_date: str | None = None,
        to_date:   str | None = None,
        symbols:   str = "NIFTY 50",
    ) -> PnlBenchmarkResponse:
        """
        Daily closing prices for Indian benchmark indices, normalised to
        % change from the first available close in the requested range.

        Query params:
          from=YYYY-MM-DD   (default: 30 days ago)
          to=YYYY-MM-DD     (default: today IST)
          symbols=NIFTY 50,SENSEX,...  (comma-separated; default: NIFTY 50)

        Each series: { symbol, name, closes: [{date, close, pct_change_from_start}] }
        """
        import asyncio
        from datetime import timedelta
        from backend.shared.helpers.date_time_utils import timestamp_indian

        today_str  = timestamp_indian().date().isoformat()
        to_str     = (to_date   or "").strip() or today_str
        from_str   = (from_date or "").strip() or (
            dt_date.fromisoformat(to_str) - timedelta(days=30)
        ).isoformat()

        try:
            d_from = dt_date.fromisoformat(from_str)
            d_to   = dt_date.fromisoformat(to_str)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid date: {exc}")

        if d_from > d_to:
            raise HTTPException(
                status_code=422,
                detail=f"from ({from_str}) must be <= to ({to_str})",
            )

        requested = [s.strip() for s in symbols.split(",") if s.strip()]
        unknown   = [s for s in requested if s not in BENCHMARK_TOKENS]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown benchmark(s): {', '.join(unknown)}. "
                       f"Valid: {', '.join(BENCHMARK_TOKENS)}",
            )

        from backend.shared.brokers.registry import get_price_broker

        def _fetch_one(symbol: str) -> PnlBenchmarkSeries:
            cache_key = _benchmark_cache_key(symbol, from_str, to_str)
            if cache_key in _BENCHMARK_CACHE:
                closes = _BENCHMARK_CACHE[cache_key]
                return PnlBenchmarkSeries(symbol=symbol, name=symbol, closes=closes)

            _, token = BENCHMARK_TOKENS[symbol]
            try:
                broker = get_price_broker()
                kite   = broker.kite  # type: ignore[attr-defined]
                raw    = kite.historical_data(
                    token,
                    d_from,
                    d_to,
                    "day",
                ) or []
            except Exception as exc:
                logger.warning(f"pnl_benchmarks: {symbol} fetch failed: {exc}")
                return PnlBenchmarkSeries(symbol=symbol, name=symbol, closes=[])

            if not raw:
                return PnlBenchmarkSeries(symbol=symbol, name=symbol, closes=[])

            base_close = float(raw[0].get("close") or raw[0].get("close_price") or 0)
            closes: list[dict] = []
            for bar in raw:
                c = float(bar.get("close") or bar.get("close_price") or 0)
                d = bar.get("date")
                date_str = d.date().isoformat() if hasattr(d, "date") else str(d)[:10]
                pct = ((c / base_close) - 1.0) * 100.0 if base_close else 0.0
                closes.append({"date": date_str, "close": c, "pct_change_from_start": round(pct, 4)})

            _BENCHMARK_CACHE[cache_key] = closes
            return PnlBenchmarkSeries(symbol=symbol, name=symbol, closes=closes)

        # Run each symbol fetch in a thread so sync Kite HTTP doesn't block the loop
        loop = asyncio.get_event_loop()
        tasks = [loop.run_in_executor(None, _fetch_one, s) for s in requested]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        series: list[PnlBenchmarkSeries] = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.warning(f"pnl_benchmarks: gather error for {requested[i]}: {res}")
                series.append(PnlBenchmarkSeries(symbol=requested[i], name=requested[i], closes=[]))
            else:
                series.append(res)  # type: ignore[arg-type]

        return PnlBenchmarkResponse(from_date=from_str, to_date=to_str, series=series)

    # ------------------------------------------------------------------
    # P&L CSV upload  (Kite Console P&L Statement)
    # ------------------------------------------------------------------

    @post("/pnl/upload-csv", status_code=200)
    async def pnl_upload_csv(
        self,
        data: Body(media_type=RequestEncodingType.MULTI_PART),  # type: ignore[valid-type]
    ) -> PnlCsvUploadResponse:
        """
        Upload a Kite Console P&L Statement CSV to backfill daily_book.

        Form fields:
          account  — broker account code this CSV belongs to
          date     — as-of date (YYYY-MM-DD or 'today')  [default: today IST]
          file     — the CSV file
        """
        import csv as csv_mod
        from backend.api.algo.daily_snapshot import kite_seg_from_exchange, _UPSERT_SQL
        from backend.shared.helpers.date_time_utils import timestamp_indian
        from datetime import datetime, timezone

        account_val: str = data.get("account", "") if isinstance(data, dict) else ""
        date_val:    str = data.get("date",    "") if isinstance(data, dict) else ""
        file_upload: UploadFile | None = data.get("file") if isinstance(data, dict) else None

        # Litestar delivers multipart as a dict-like object; handle both dict and
        # attribute access depending on Litestar version.
        if not isinstance(data, dict):
            account_val = getattr(data, "account", "") or ""
            date_val    = getattr(data, "date",    "") or ""
            file_upload = getattr(data, "file",    None)

        account_val = (account_val or "").strip()
        date_val    = (date_val    or "").strip()

        if not account_val:
            raise HTTPException(status_code=422, detail="account field is required")
        if file_upload is None:
            raise HTTPException(status_code=422, detail="file field is required")

        today_str = timestamp_indian().date().isoformat()
        if not date_val or date_val.lower() == "today":
            target = dt_date.fromisoformat(today_str)
        else:
            try:
                target = dt_date.fromisoformat(date_val)
            except ValueError:
                raise HTTPException(
                    status_code=422, detail=f"Invalid date: {date_val!r} — use YYYY-MM-DD"
                )

        raw_bytes = await file_upload.read()
        if not raw_bytes:
            raise HTTPException(status_code=422, detail="Uploaded file is empty")

        # Kite CSV may be UTF-8 or Windows-1252
        try:
            text_content = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            text_content = raw_bytes.decode("cp1252", errors="replace")

        reader = csv_mod.DictReader(io.StringIO(text_content))
        if reader.fieldnames is None:
            raise HTTPException(status_code=422, detail="CSV has no header row")

        # Normalise fieldnames to stripped lower-case
        lower_fields = {f.strip().lower() for f in reader.fieldnames}

        # The symbol column has two common aliases; exchange must be present.
        # Use alias resolution to check: at least one of the tradingsymbol
        # aliases AND the exchange column must exist in the header.
        _TS_ALIASES  = {"tradingsymbol", "trading symbol", "symbol"}
        has_symbol   = bool(lower_fields & _TS_ALIASES)
        has_exchange = "exchange" in lower_fields
        missing: list[str] = []
        if not has_symbol:
            missing.append("tradingsymbol (or 'Trading Symbol' / 'Symbol')")
        if not has_exchange:
            missing.append("exchange")
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"CSV missing required columns: {missing}"
            )

        # Column aliases (Kite Console column names vary by report type)
        _COL = {
            "tradingsymbol": ["tradingsymbol", "trading symbol", "symbol"],
            "exchange":      ["exchange"],
            "qty":           ["open quantity", "quantity", "open_quantity"],
            "avg_cost":      ["open average", "open average price", "average_price", "buy average"],
            "ltp":           ["previous closing price", "last price", "ltp"],
            "day_pnl":       ["unrealized p&l", "unrealized pnl", "day_pnl"],
            "total_pnl":     ["realized p&l", "realized pnl", "total_pnl"],
        }

        def _col_val(row: dict, aliases: list[str]) -> str | None:
            for alias in aliases:
                for k, v in row.items():
                    if k.strip().lower() == alias:
                        return v
            return None

        def _float_or_none(s: str | None) -> float | None:
            if not s:
                return None
            try:
                return float(str(s).replace(",", "").strip())
            except ValueError:
                return None

        rows_to_upsert: list[dict] = []
        skipped = 0
        now_utc = datetime.now(timezone.utc)

        for raw_row in reader:
            symbol = (_col_val(raw_row, _COL["tradingsymbol"]) or "").strip()
            exchange = (_col_val(raw_row, _COL["exchange"])   or "").strip().upper()
            if not symbol or not exchange:
                skipped += 1
                continue
            qty_raw = _float_or_none(_col_val(raw_row, _COL["qty"]))
            rows_to_upsert.append({
                "date":         target,
                "account":      account_val,
                "segment":      kite_seg_from_exchange(exchange),
                "kind":         "holdings",
                "symbol":       symbol,
                "exchange":     exchange,
                "qty":          int(qty_raw) if qty_raw is not None else 0,
                "avg_cost":     _float_or_none(_col_val(raw_row, _COL["avg_cost"])),
                "ltp":          _float_or_none(_col_val(raw_row, _COL["ltp"])),
                "day_pnl":      _float_or_none(_col_val(raw_row, _COL["day_pnl"])),
                "total_pnl":    _float_or_none(_col_val(raw_row, _COL["total_pnl"])),
                "payload_json": None,
                "captured_at":  now_utc,
            })

        if not rows_to_upsert:
            return PnlCsvUploadResponse(inserted=0, updated=0, skipped=skipped, sample=[])

        # Count pre-existing rows for this account+date to separate
        # insert vs update counts.
        async with async_session() as session:
            pre_count_res = await session.execute(
                text(
                    "SELECT COUNT(*) FROM daily_book "
                    "WHERE date = :d AND account = :a AND kind = 'holdings'"
                ),
                {"d": target, "a": account_val},
            )
            pre_count = int(pre_count_res.scalar() or 0)

            await session.execute(_UPSERT_SQL, rows_to_upsert)
            await session.commit()

        inserted = max(0, len(rows_to_upsert) - pre_count)
        updated  = len(rows_to_upsert) - inserted

        sample = [
            {k: (str(v) if not isinstance(v, (int, float, type(None))) else v)
             for k, v in r.items()
             if k not in ("payload_json", "captured_at")}
            for r in rows_to_upsert[:3]
        ]

        logger.info(
            f"Admin: pnl csv upload account={account_val} date={target} "
            f"inserted={inserted} updated={updated} skipped={skipped}"
        )
        return PnlCsvUploadResponse(
            inserted=inserted,
            updated=updated,
            skipped=skipped,
            sample=sample,
        )

    @post("/pnl/snapshot", status_code=200)
    async def trigger_pnl_snapshot(self, data: SnapshotRequest) -> SnapshotResponse:
        """
        Manually trigger a daily book snapshot for the given date.

        Body: {"date": "YYYY-MM-DD"} or {"date": "today"}.

        NOTE: trades are only available for today's IST date. Passing a past
        date captures holdings + positions only; trades rows will be zero.
        """
        from datetime import date as dt_date
        from backend.api.algo.daily_snapshot import snapshot_daily_book
        from backend.shared.helpers.date_time_utils import timestamp_indian

        date_str = (data.date or "").strip()
        if not date_str or date_str.lower() == "today":
            target = timestamp_indian().date()
        else:
            try:
                target = dt_date.fromisoformat(date_str)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"Invalid date: {date_str!r} — use YYYY-MM-DD")

        try:
            result = await snapshot_daily_book(target_date=target)
        except Exception as e:
            logger.error(f"Admin: pnl snapshot failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

        logger.info(
            f"Admin: pnl snapshot triggered for {target} — "
            f"h={result['holdings_rows']} p={result['positions_rows']} t={result['trades_rows']}"
        )
        return SnapshotResponse(
            accounts=result["accounts"],
            holdings_rows=result["holdings_rows"],
            positions_rows=result["positions_rows"],
            trades_rows=result["trades_rows"],
            errors=result["errors"],
        )

    @delete("/users/{username:str}", status_code=200)
    async def delete_user(self, username: str, request: Request) -> dict:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            self._check_action(request, user, designated_only=True, block_self=True)
            user.is_active = False
            await session.commit()
        await refresh_alert_recipients()
        return {"detail": f"User {username!r} deactivated"}
