"""
Admin-only endpoints.

GET  /api/admin/logs               — tail the app log file (last N lines)
POST /api/admin/exec               — run a shell command and return output
GET  /api/admin/users              — list all users (no password hashes)
DELETE /api/admin/users/{username}  — deactivate a user

All routes require admin JWT via admin_guard.
"""

import os
import subprocess
from pathlib import Path

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session
from backend.api.models import User
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


class UpdateUserRequest(msgspec.Struct):
    display_name: str | None = None
    role: str | None = None
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
                join_date=str(u.join_date) if u.join_date else None, notes=u.notes,
            )
        return UsersResponse(users=[_to_info(u) for u in users])

    @post("/users")
    async def create_user(self, data: CreateUserRequest) -> dict:
        """Admin creates a user (pre-approved). Share password via other channel."""
        from backend.api.routes.auth import hash_password
        if len(data.password) < 8:
            raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
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
            )
            session.add(user)
            await session.commit()
        logger.info(f"Admin: created user {data.username!r} role={data.role}")
        return {"detail": f"User {data.username!r} created"}

    @put("/users/{username:str}/approve", status_code=200)
    async def approve_user(self, username: str) -> dict:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            user.is_approved = True
            await session.commit()
        logger.info(f"Admin: approved user {username!r}")
        return {"detail": f"User {username!r} approved"}

    @put("/users/{username:str}/reject", status_code=200)
    async def reject_user(self, username: str) -> dict:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            user.is_approved = False
            user.is_active = False
            await session.commit()
        logger.info(f"Admin: rejected user {username!r}")
        return {"detail": f"User {username!r} rejected"}

    @put("/users/{username:str}")
    async def update_user(self, username: str, data: UpdateUserRequest) -> dict:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            # Apply all non-None fields from the request
            for field in (
                'display_name', 'role', 'email', 'phone', 'pan',
                'kyc_verified', 'address_line1', 'address_line2',
                'city', 'state', 'pincode', 'contribution', 'contribution_date',
                'share_pct', 'bank_name', 'bank_account', 'bank_ifsc',
                'nominee_name', 'nominee_relation', 'nominee_phone',
                'join_date', 'notes',
            ):
                val = getattr(data, field, None)
                if val is not None:
                    if field == 'pan':
                        val = val.upper()
                    if field == 'date_of_birth' or field == 'join_date':
                        from datetime import date as dt_date
                        val = dt_date.fromisoformat(val) if isinstance(val, str) else val
                    setattr(user, field, val)
            await session.commit()
        logger.info(f"Admin: updated user {username!r}")
        return {"detail": f"User {username!r} updated"}

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
    async def delete_user(self, username: str) -> dict:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail=f"User {username!r} not found")
            user.is_active = False
            await session.commit()
        return {"detail": f"User {username!r} deactivated"}
