"""
JWT auth endpoints.

POST /api/auth/login     — validate credentials, return access token + user info
POST /api/auth/register  — create a new user account (partner role by default)
POST /api/auth/logout    — client-side token discard (stateless)
GET  /api/auth/me        — decode token, return user profile

Users are stored in SQLAlchemy DB (data/ramboq.db).
On first startup with an empty DB, any non-empty credentials are accepted (stub mode)
so you can sign in immediately and create real users.
"""

import base64
import hashlib
import os
import secrets as _stdlib_secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import msgspec
from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException
from litestar.response import Redirect
from sqlalchemy import or_, select

from backend.api.auth_guard import jwt_guard
from backend.api.database import async_session
from backend.api.models import AuthToken, User
from backend.shared.helpers.mail_utils import send_email
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import (
    is_prod_branch,
    secrets,
    validate_email,
    validate_password_standard,
)

logger = get_logger(__name__)

_JWT_ALGORITHM = "HS256"
_TOKEN_TTL_SECONDS = 24 * 3600  # 24 hours


def _jwt_secret() -> str:
    secret = secrets.get("jwt_secret") or secrets.get("cookie_secret", "")
    if not secret:
        raise RuntimeError("jwt_secret / cookie_secret not set in secrets.yaml")
    return secret


def _make_token(user: User) -> str:
    """Encode every claim future guards may need: role, email_verified,
    token_version (tv). The legacy `is_super` claim was retired —
    role='designated' is the new top tier. Bumping User.token_version
    invalidates every JWT that carries the old tv on the next request
    that hits a guard performing the DB check."""
    payload = {
        "sub":            user.username,
        "role":           user.role,
        "display_name":   user.display_name,
        "contribution":   user.contribution or 0,
        "email_verified": bool(user.email_verified),
        "tv":             int(user.token_version or 1),
        "iat":            int(time.time()),
        "exp":            int(time.time()) + _TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_JWT_ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """Verify a JWT and return the full payload dict. Returns None if
    invalid/expired. Signature + expiry only — token_version DB check
    happens in the async route handlers, not in the sync guard."""
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[_JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


# ---------------------------------------------------------------------------
# Password hashing — PBKDF2-SHA256 with cryptographic salt
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """PBKDF2-SHA256 with a 16-byte os.urandom salt — independent of the
    password input (the prior implementation derived the salt from the
    password itself, weakening the salting guarantee)."""
    salt = base64.b64encode(os.urandom(16)).decode()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"pbkdf2_sha256$260000${salt}${base64.b64encode(dk).decode()}"


def _check_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, stored_b64 = stored.split("$", 3)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        return base64.b64encode(dk).decode() == stored_b64
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Email + verification helpers
# ---------------------------------------------------------------------------

_VERIFY_TTL_MINUTES = 60   # email verification link is good for 1 hour
_RESET_TTL_MINUTES  = 30   # password-reset link is good for 30 minutes


def _public_url() -> str:
    """Branch-aware base URL the verification / reset links land on."""
    return "https://ramboq.com" if is_prod_branch() else "https://dev.ramboq.com"


async def _issue_auth_token(session, user_id: int, purpose: str, ttl_minutes: int) -> str:
    """Mint an AuthToken row and return the raw token string. The DB stores
    the same string (32 bytes hex = 64 chars, fits the unique-indexed
    column). Single-use semantics live on the consumer side via used_at."""
    token = _stdlib_secrets.token_hex(32)
    row = AuthToken(
        user_id=user_id,
        purpose=purpose,
        token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
    )
    session.add(row)
    return token


def _send_verify_email(display_name: str, email: str, token: str) -> None:
    """Fire-and-forget verification email. Uses the existing send_email
    helper which honours the `mail` capability flag — on dev with mail=False
    the body is logged instead of sent."""
    # Link goes straight to the backend endpoint — it validates the
    # token and then 302s to /signin?verified=1|0 so the user never sees
    # the API URL in their browser bar.
    link = f"{_public_url()}/api/auth/verify-email?token={token}"
    body = f"""\
<p>Hi {display_name},</p>
<p>Welcome to <b>RamboQuant</b>. Click the link below to verify your email
address. The link expires in {_VERIFY_TTL_MINUTES} minutes.</p>
<p><a href="{link}">{link}</a></p>
<p>After verification, an admin will approve your account before you can
sign in. You'll get a separate confirmation email once that happens.</p>
<p>If you didn't sign up for RamboQuant, you can ignore this message.</p>
<p>— RamboQuant</p>
"""
    ok, msg = send_email(display_name, email, "RamboQuant: verify your email", body)
    if not ok:
        logger.warning(f"Auth: verify email send failed for {email!r}: {msg}")


def _send_reset_email(display_name: str, email: str, token: str) -> None:
    link = f"{_public_url()}/auth/reset?token={token}"
    body = f"""\
<p>Hi {display_name or 'there'},</p>
<p>You (or someone using your email) requested a password reset on
<b>RamboQuant</b>. Click the link below to set a new password. The link
expires in {_RESET_TTL_MINUTES} minutes.</p>
<p><a href="{link}">{link}</a></p>
<p>If you didn't request this, you can safely ignore the message — your
existing password will continue to work.</p>
<p>— RamboQuant</p>
"""
    ok, msg = send_email(display_name or email, email, "RamboQuant: password reset", body)
    if not ok:
        logger.warning(f"Auth: reset email send failed for {email!r}: {msg}")


def _notify_admins_new_registration(user: User) -> None:
    """Email the alert_emails list when a new partner registers so an
    admin knows to approve. Best-effort — failures get logged, never raised."""
    recipients = secrets.get("alert_emails", []) or []
    if not recipients:
        return
    body = f"""\
<p>A new RamboQuant account is awaiting approval.</p>
<table style="border-collapse:collapse">
  <tr><td><b>Username</b></td><td>{user.username}</td></tr>
  <tr><td><b>Display name</b></td><td>{user.display_name}</td></tr>
  <tr><td><b>Email</b></td><td>{user.email}</td></tr>
  <tr><td><b>Phone</b></td><td>{user.phone or '—'}</td></tr>
</table>
<p>Approve or reject from the <a href="{_public_url()}/admin/users">admin
users page</a>.</p>
"""
    for addr in recipients:
        try:
            ok, msg = send_email("Admin", addr, f"RamboQuant: new registration — {user.username}", body)
            if not ok:
                logger.warning(f"Auth: admin notify failed for {addr!r}: {msg}")
        except Exception as exc:  # noqa: BLE001 — defensive, never block register
            logger.warning(f"Auth: admin notify exception for {addr!r}: {exc}")


# ---------------------------------------------------------------------------
# Schemas (msgspec)
# ---------------------------------------------------------------------------

class LoginRequest(msgspec.Struct):
    username: str
    password: str


class LoginResponse(msgspec.Struct):
    access_token: str
    username: str
    role: str
    display_name: str
    token_type: str = "bearer"
    expires_in: int = _TOKEN_TTL_SECONDS


class RegisterRequest(msgspec.Struct):
    username: str
    password: str
    display_name: str
    email: str = ""
    phone: str = ""
    pan: str = ""


class UserProfile(msgspec.Struct):
    username: str
    role: str
    display_name: str
    contribution: float


class LogoutResponse(msgspec.Struct):
    detail: str


class ForgotPasswordRequest(msgspec.Struct):
    """Either username or email is fine — we look up by both. Single
    field so the UI stays a single text box."""
    identifier: str


class ResetPasswordRequest(msgspec.Struct):
    token: str
    password: str


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class AuthController(Controller):
    path = "/api/auth"

    @post("/login")
    async def login(self, data: LoginRequest) -> LoginResponse:
        async with async_session() as session:
            result = await session.execute(
                select(User).where(User.username == data.username)
            )
            user = result.scalar_one_or_none()

        # Single error string for every credential / status failure so an
        # attacker can't enumerate accounts by error wording. Logged
        # internally so the operator can audit if needed.
        AUTH_FAIL = "Invalid credentials or account not available"

        if not user:
            raise HTTPException(status_code=401, detail=AUTH_FAIL)
        if not _check_password(data.password, user.password_hash):
            logger.info(f"Auth: bad password for {data.username!r}")
            raise HTTPException(status_code=401, detail=AUTH_FAIL)
        if user.terminated_at is not None:
            logger.info(f"Auth: terminated account {data.username!r} blocked")
            raise HTTPException(status_code=401, detail=AUTH_FAIL)
        if user.suspended_at is not None:
            logger.info(f"Auth: suspended account {data.username!r} blocked")
            raise HTTPException(status_code=403, detail="Account suspended — contact admin")
        if not user.is_active:
            raise HTTPException(status_code=401, detail=AUTH_FAIL)
        # Admin / designated bypass approval+verification gates so a
        # freshly-seeded admin can log in without going through the
        # email-verify dance.
        is_privileged = user.role in ("admin", "designated")
        if not is_privileged:
            if not user.email_verified:
                raise HTTPException(
                    status_code=403,
                    detail="Email not verified — check your inbox for the verification link",
                )
            if not user.is_approved:
                raise HTTPException(
                    status_code=403, detail="Account pending admin approval",
                )

        token = _make_token(user)
        logger.info(
            f"Auth: login OK {data.username!r} role={user.role} "
            f"tv={user.token_version}"
        )
        return LoginResponse(
            access_token=token,
            username=user.username,
            role=user.role,
            display_name=user.display_name,
        )

    @post("/register")
    async def register(self, data: RegisterRequest) -> dict:
        # Stronger validation than the original len>=8 check: enforce the
        # password standard from /admin/settings, require a valid email
        # (verification link goes there), and reject weak passwords up
        # front rather than after the DB write succeeds.
        ok, msg = validate_password_standard(data.password)
        if not ok:
            raise HTTPException(status_code=422, detail=msg)
        if not (data.email and validate_email(data.email)):
            raise HTTPException(status_code=422, detail="A valid email is required for registration")

        async with async_session() as session:
            existing = await session.execute(
                select(User).where(User.username == data.username)
            )
            if existing.scalar_one_or_none():
                raise HTTPException(status_code=409, detail="Username already exists")

            user = User(
                username=data.username,
                password_hash=hash_password(data.password),
                role="partner",
                display_name=data.display_name or data.username,
                email=data.email,
                phone=data.phone or None,
                pan=data.pan.upper() if data.pan else None,
                # New self-registered users land:
                #   - is_active=True (so admin can email-verify-then-approve them)
                #   - is_approved=False (admin gate)
                #   - email_verified=False (verify-link gate)
                # Login is blocked on all three failures with a clear message.
                is_active=True,
                is_approved=False,
                email_verified=False,
            )
            session.add(user)
            await session.flush()  # populate user.id for the AuthToken FK
            verify_token = await _issue_auth_token(
                session, user.id, "verify", _VERIFY_TTL_MINUTES,
            )
            await session.commit()

        logger.info(f"Auth: registered {data.username!r} email={data.email!r}")

        # Best-effort notifications — both calls swallow errors internally
        # so a flaky SMTP path can't 500 a successful registration.
        _send_verify_email(user.display_name, user.email, verify_token)
        _notify_admins_new_registration(user)

        return {
            "detail": (
                "Account created. Check your email for the verification link, "
                "then wait for an admin to approve your account."
            ),
            "username": user.username,
            "next_step": "verify_email",
        }

    @get("/me", guards=[jwt_guard])
    async def me(self, request: Request) -> UserProfile:
        payload = request.state.token_payload
        return UserProfile(
            username=payload.get("sub", ""),
            role=payload.get("role", "partner"),
            display_name=payload.get("display_name", ""),
            contribution=payload.get("contribution", 0),
        )

    @post("/logout")
    async def logout(self) -> LogoutResponse:
        return LogoutResponse(detail="Logged out")

    # ------------------------------------------------------------------
    # Email verification + password reset
    # ------------------------------------------------------------------

    @get("/verify-email")
    async def verify_email(self, token: str = "") -> Redirect:
        """Click-target for the email verification link.

        Looks up the token (purpose='verify', not used, not expired),
        flips email_verified=True, marks the token used, and redirects
        the user to /signin?verified=1. Bad/expired tokens redirect to
        /signin?verified=0 — the page renders a clear failure state.
        """
        if not token:
            return Redirect(path="/signin?verified=0")
        async with async_session() as session:
            row = await session.execute(
                select(AuthToken).where(
                    AuthToken.token == token,
                    AuthToken.purpose == "verify",
                )
            )
            tok = row.scalar_one_or_none()
            if (
                not tok
                or tok.used_at is not None
                or tok.expires_at <= datetime.now(timezone.utc)
            ):
                return Redirect(path="/signin?verified=0")
            user = await session.get(User, tok.user_id)
            if not user:
                return Redirect(path="/signin?verified=0")
            user.email_verified = True
            tok.used_at = datetime.now(timezone.utc)
            await session.commit()
        logger.info(f"Auth: verified email for {user.username!r}")
        return Redirect(path="/signin?verified=1")

    @post("/forgot-password")
    async def forgot_password(self, data: ForgotPasswordRequest) -> dict:
        """Always returns 200 with the same message regardless of whether
        the account exists — prevents account enumeration. If the account
        does exist we issue a reset token and dispatch the email."""
        ident = (data.identifier or "").strip()
        if not ident:
            raise HTTPException(status_code=422, detail="Username or email required")
        async with async_session() as session:
            result = await session.execute(
                select(User).where(or_(User.username == ident, User.email == ident))
            )
            user = result.scalar_one_or_none()
            if user and user.is_active and user.terminated_at is None:
                tok = await _issue_auth_token(
                    session, user.id, "reset", _RESET_TTL_MINUTES,
                )
                await session.commit()
                _send_reset_email(user.display_name, user.email or "", tok)
                logger.info(f"Auth: reset link issued for {user.username!r}")
            else:
                # Constant-time-ish: still spend a bit of work so the
                # response timing doesn't leak existence. (Crude — a real
                # constant-time path would hash a dummy password.)
                hashlib.pbkdf2_hmac("sha256", b"x", b"x", 260_000)
                if user:
                    logger.info(
                        f"Auth: reset suppressed for inactive/terminated "
                        f"user {user.username!r}"
                    )
                else:
                    logger.info(f"Auth: reset for unknown identifier {ident!r}")
        return {"detail": "If the account exists, a reset link has been emailed."}

    @post("/reset-password")
    async def reset_password(self, data: ResetPasswordRequest) -> dict:
        """Consume a reset token + set a new password. Bumps token_version
        so any live JWTs the user holds are invalidated, forcing them to
        sign in again with the new password."""
        ok, msg = validate_password_standard(data.password)
        if not ok:
            raise HTTPException(status_code=422, detail=msg)
        async with async_session() as session:
            row = await session.execute(
                select(AuthToken).where(
                    AuthToken.token == data.token,
                    AuthToken.purpose == "reset",
                )
            )
            tok = row.scalar_one_or_none()
            if (
                not tok
                or tok.used_at is not None
                or tok.expires_at <= datetime.now(timezone.utc)
            ):
                raise HTTPException(status_code=400, detail="Reset link is invalid or expired")
            user = await session.get(User, tok.user_id)
            if not user or not user.is_active or user.terminated_at is not None:
                raise HTTPException(status_code=400, detail="Reset link is invalid or expired")
            user.password_hash = hash_password(data.password)
            user.token_version = (user.token_version or 1) + 1
            tok.used_at = datetime.now(timezone.utc)
            await session.commit()
        logger.info(f"Auth: password reset for {user.username!r}")
        return {"detail": "Password reset. Sign in with your new password."}
