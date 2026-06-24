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

import asyncio
import base64
import hashlib
import hmac as _hmac
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
from sqlalchemy import func, or_, select, update

from backend.api.auth_guard import jwt_guard
from backend.api.database import async_session
from backend.api.models import AuthToken, ImpersonationEvent, User
from backend.api.rate_limit import make_rate_limit_guard
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

# Rate-limit thresholds — slow brute-force credential stuffing without
# locking out a typo'd legit user. Per (client_ip, route) sliding
# window. /login: 5 attempts/min covers genuine wrong-password retries
# but stops automated guessing dead. /forgot-password and
# /reset-password: 3/min — these issue tokens + dispatch emails so the
# cost of every hit is non-trivial.
_login_rate_limit         = make_rate_limit_guard(limit=5, window_seconds=60)
_forgot_pw_rate_limit     = make_rate_limit_guard(limit=3, window_seconds=60)
_reset_pw_rate_limit      = make_rate_limit_guard(limit=3, window_seconds=60)
_register_rate_limit      = make_rate_limit_guard(limit=3, window_seconds=300)
_verify_email_rate_limit  = make_rate_limit_guard(limit=10, window_seconds=60)


def _jwt_secret() -> str:
    secret = secrets.get("jwt_secret") or secrets.get("cookie_secret", "")
    if not secret:
        raise RuntimeError("jwt_secret / cookie_secret not set in secrets.yaml")
    return secret


_IMPERSONATE_TTL_SECONDS = 30 * 60  # 30 minutes


def _make_token(user: User, *, imp_by: Optional[str] = None, ttl_seconds: Optional[int] = None) -> str:
    """Encode every claim future guards may need: role, email_verified,
    token_version (tv). The legacy `is_super` claim was retired —
    role='designated' is the new top tier. Bumping User.token_version
    invalidates every JWT that carries the old tv on the next request
    that hits a guard performing the DB check.

    Impersonation: when an admin / designated takes a support session,
    they call POST /api/auth/impersonate/{target_username}, which mints
    a JWT for `user=target` BUT stamped with `imp_by=actor.username`
    and a shortened TTL (30 min default). Routes that block or
    decorate based on impersonation read `imp_by` from the decoded
    payload. POST /api/auth/stop-impersonate returns a fresh normal
    JWT for the actor (no imp_by claim).
    """
    ttl = ttl_seconds if ttl_seconds is not None else _TOKEN_TTL_SECONDS
    payload = {
        "sub":            user.username,
        "role":           user.role,
        "display_name":   user.display_name,
        "contribution":   user.contribution or 0,
        "email_verified": bool(user.email_verified),
        "tv":             int(user.token_version or 1),
        "iat":            int(time.time()),
        "exp":            int(time.time()) + ttl,
    }
    if imp_by:
        payload["imp_by"] = imp_by
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
    """Constant-time PBKDF2 verification. The earlier implementation used
    Python's `==` on the base64 strings, which short-circuits on the
    first differing byte and creates a timing oracle for password
    guessing. `hmac.compare_digest` runs in time linear to the input
    length regardless of where the mismatch is."""
    try:
        _, iterations, salt, stored_b64 = stored.split("$", 3)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        return _hmac.compare_digest(base64.b64encode(dk).decode(), stored_b64)
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


def _dispatch_async(callable_, /, *args, **kwargs) -> None:
    """Fire-and-forget an SMTP send so the request handler returns at
    the same speed regardless of whether an email actually goes out.

    Two reasons for this:
    1. Closes the timing side-channel on /forgot-password — both miss
       and hit return in <50 ms instead of <50 ms vs ~3 s (Gmail TLS).
    2. Avoids blocking the response on a flaky SMTP path; failures land
       in the log and don't 500 the user-facing flow.

    `asyncio.to_thread` runs the sync `send_email` off the event loop;
    `asyncio.create_task` schedules it without awaiting. If the loop
    isn't running (CLI / tests), we fall back to a synchronous call.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop (CLI). Fire synchronously.
        callable_(*args, **kwargs)
        return
    loop.create_task(asyncio.to_thread(callable_, *args, **kwargs))


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
    must_change_password: bool = False
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
    # Capability list — what this role can do, per the rbac matrix.
    # Mirrored to the frontend at /me so UI gates (hide nav items,
    # disable buttons, branch flow) don't need to re-implement the
    # role→cap mapping client-side. Always present even for legacy
    # tokens; empty list is a safe default (treated as observer).
    caps: list[str] = []


class LogoutResponse(msgspec.Struct):
    detail: str


class ForgotPasswordRequest(msgspec.Struct):
    """Either username or email is fine — we look up by both. Single
    field so the UI stays a single text box."""
    identifier: str


class ResetPasswordRequest(msgspec.Struct):
    token: str
    password: str


class ChangePasswordRequest(msgspec.Struct):
    """Used by the post-admin-reset force-change flow. The current JWT
    is the only credential — the user JUST signed in with the
    admin-supplied password and the auth_guard let them through to
    /change-password specifically because must_change_password=True."""
    password: str


# ---------------------------------------------------------------------------
# Firm NAV — shared computation
# ---------------------------------------------------------------------------

# Short module-level cache so the public unauthenticated endpoint can't
# be used to hammer broker.holdings() / broker.margins() at unbounded
# rate. NAV moves slow enough that a 30 s memo is operationally fine.
_NAV_CACHE: dict = {"ts": 0.0, "value": None}
_NAV_TTL_SEC = 30.0


async def _compute_firm_nav() -> tuple[float, float, float, str]:
    """Return (firm_nav, firm_day_pnl, firm_cum_pnl, as_of_iso).

    Canonical formula (operator-verified — produces the same number
    they were used to seeing on the NavCard, ~₹2.28 Cr at time of
    writing):

        firm_nav = holdings.cur_val + cash + collateral

    Where:
        holdings.cur_val — MTM of all stocks held in demat
                           (Zerodha returns the raw stock value
                            regardless of pledge state)
        cash             — free liquid cash in the trading account
        collateral       — haircut-adjusted margin value of PLEDGED
                           stocks. NOT a 1:1 subset of cur_val —
                           Zerodha computes this as
                           pledged_stock_value × haircut_factor and
                           treats it as an asset of the firm. Adding
                           it on top of cur_val matches the
                           operator's NAV semantics (the firm's
                           total claim on broker assets).

    Earlier "fix" (cur_val + cash + positions.pnl) was wrong —
    omitted collateral and produced ~₹0.6 Cr too low.

    day_pnl / cum_pnl come from the live intraday-equity deque when
    populated; off-hours falls back to holdings.day_change + positions.pnl.
    """
    import time
    now_ts = time.time()
    cached = _NAV_CACHE.get("value")
    if cached is not None and (now_ts - _NAV_CACHE["ts"]) < _NAV_TTL_SEC:
        return cached

    from backend.api.background import (
        _intraday_equity,
        _fetch_holdings_direct,
        _fetch_margins_direct,
        _fetch_positions_direct,
    )
    import asyncio as _asyncio
    from concurrent.futures import ThreadPoolExecutor as _TPE

    firm_nav     = 0.0
    firm_day_pnl = 0.0
    firm_cum_pnl = 0.0
    as_of_iso    = datetime.now(timezone.utc).isoformat()

    try:
        loop = _asyncio.get_running_loop()
        with _TPE(max_workers=3) as ex:
            df_h_fut = loop.run_in_executor(ex, _fetch_holdings_direct)
            df_m_fut = loop.run_in_executor(ex, _fetch_margins_direct)
            df_p_fut = loop.run_in_executor(ex, _fetch_positions_direct)
            _, sum_h     = await df_h_fut
            df_m         = await df_m_fut
            _, sum_p     = await df_p_fut

        total_h = sum_h[sum_h['account'] == 'TOTAL'] if not sum_h.empty else sum_h
        total_m = df_m[df_m['account'] == 'TOTAL']
        total_p = sum_p[sum_p['account'] == 'TOTAL'] if not sum_p.empty else sum_p

        cur_val = 0.0
        if not total_h.empty and 'cur_val' in total_h.columns:
            cur_val = float(total_h['cur_val'].iloc[0] or 0)

        # Margin total — _fetch_margins_direct returns the RAW broker
        # dataframe (NOT the canonicalised one funds.py emits via
        # _COL_MAP). Raw columns are 'net' / 'avail cash' /
        # 'avail collateral' / etc. The consolidated 'net' field is
        # Kite's total margin claim (cash + collateral - used_margin)
        # — adding cur_val + net matches the operator's verified NAV
        # (~₹2.28 Cr at time of writing).
        margin_total = 0.0
        if not total_m.empty:
            margin_col = next((c for c in [
                'net',           # raw broker dataframe field
                'avail_margin',  # canonicalised name (funds.py rewrite)
            ] if c in total_m.columns), None)
            if margin_col:
                margin_total = float(total_m[margin_col].iloc[0] or 0)

        firm_nav = cur_val + max(margin_total, 0.0)

        pos_pnl = 0.0
        if not total_p.empty and 'pnl' in total_p.columns:
            pos_pnl = float(total_p['pnl'].iloc[0] or 0)

        # Prefer the deque for P&L (already running totals) when alive.
        # Buffer carries (ts, day, cum, h_pnl, h_day, p_pnl, p_day) per tick
        # — destructure the two aggregates the showcase needs; ignore the
        # 4 breakdowns. Tolerate legacy 3-tuples during rolling deploys.
        if _intraday_equity:
            _last = _intraday_equity[-1]
            ts, day_pnl, cum_pnl = _last[0], _last[1], _last[2]
            as_of_iso    = ts
            firm_day_pnl = float(day_pnl)
            firm_cum_pnl = float(cum_pnl)
        else:
            # Off-hours fallback — synthesize from holdings + positions
            if not total_h.empty and 'day_change_val' in total_h.columns:
                firm_day_pnl = float(total_h['day_change_val'].iloc[0] or 0)
            firm_cum_pnl = pos_pnl
            if not total_h.empty and 'pnl' in total_h.columns:
                firm_cum_pnl += float(total_h['pnl'].iloc[0] or 0)
    except Exception as e:
        logger.warning(f"_compute_firm_nav: broker fetch failed: {e}")

    result = (firm_nav, firm_day_pnl, firm_cum_pnl, as_of_iso)
    _NAV_CACHE.update(ts=now_ts, value=result)
    return result


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class AuthController(Controller):
    path = "/api/auth"

    @post("/login", guards=[_login_rate_limit])
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
            must_change_password=bool(user.must_change_password),
        )

    @post("/register", guards=[_register_rate_limit])
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
                # Self-registered users land as 'partner' — read-only
                # aggregate view. Admin promotes them to a real role
                # (trader/risk/ops/admin) after approval.
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
            new_user_id = user.id

        # Seed Default + Markets watchlists for the new partner. Lazy
        # path in /api/watchlist also handles this — eager seeding here
        # just means the user's first /watchlist call returns populated
        # rows immediately instead of triggering an inline seed write.
        from backend.api.routes.watchlist import seed_default_watchlists_for_user
        try:
            await seed_default_watchlists_for_user(new_user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Auth: watchlist seed failed for {data.username!r}: {exc}")

        logger.info(f"Auth: registered {data.username!r} email={data.email!r}")

        # Fire emails off the event loop so SMTP latency doesn't block
        # the response. Each helper swallows its own errors internally.
        _dispatch_async(_send_verify_email, user.display_name, user.email, verify_token)
        _dispatch_async(_notify_admins_new_registration, user)

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
        from backend.api.rbac import caps_for_role
        payload = request.state.token_payload
        role = payload.get("role", "partner")
        return UserProfile(
            username=payload.get("sub", ""),
            role=role,
            display_name=payload.get("display_name", ""),
            contribution=payload.get("contribution", 0),
            caps=caps_for_role(role),
        )

    @get("/whoami")
    async def whoami(self, request: Request) -> UserProfile:
        """Public role + caps endpoint. Unlike /me, requires no JWT —
        anonymous demo sessions get back `role='demo'` with the demo
        capability set. Lets the frontend bootstrap nav / button gates
        without first calling a guarded endpoint and 401'ing. Identity
        fields (username, display_name) are blanked for demo."""
        from backend.api.rbac import (
            caps_for_role, normalise_role,
            resolve_role_from_connection,
        )
        from backend.api.auth_guard import is_authenticated_request
        # Hydrate token_payload only when a real JWT is present —
        # `auth_or_demo_guard` would do this, but /whoami isn't behind
        # any guard so we inline the small check.
        if is_authenticated_request(request):
            from backend.api.routes.auth import verify_token
            token = request.headers.get("Authorization", "") \
                .removeprefix("Bearer ").strip()
            payload = verify_token(token) or {}
            role = normalise_role(payload.get("role"))
            return UserProfile(
                username=payload.get("sub", ""),
                role=role,
                display_name=payload.get("display_name", ""),
                contribution=payload.get("contribution", 0),
                caps=caps_for_role(role),
            )
        # Anonymous — surface as 'demo' (on prod) or 'partner' (dev/no
        # auth at all). On non-prod we don't have a demo mode; return
        # partner caps so dev UIs that build off /whoami don't crash.
        from backend.shared.helpers.utils import is_prod_branch
        role = "demo" if is_prod_branch() else "partner"
        return UserProfile(
            username="", role=role, display_name="", contribution=0,
            caps=caps_for_role(role),
        )

    @post("/logout")
    async def logout(self) -> LogoutResponse:
        return LogoutResponse(detail="Logged out")

    # ------------------------------------------------------------------
    # Impersonation — support sessions for admin / designated.
    # ------------------------------------------------------------------
    # POST /impersonate/{target_username}  → mint a 30-min JWT for the
    #   target user, stamped with imp_by=actor. Permission ladder:
    #     - designated → anyone
    #     - admin      → partners only
    #     - partner    → 401
    # POST /stop-impersonate               → revert. Requires JWT with
    #   imp_by claim; returns a fresh full-TTL JWT for the original
    #   actor.
    # Every event written to impersonation_events for audit.

    @post("/impersonate/{target_username:str}", guards=[jwt_guard])
    async def impersonate(self, target_username: str, request: Request) -> LoginResponse:
        payload = getattr(request.state, "token_payload", {}) or {}
        actor_username = str(payload.get("sub", "")).strip()
        actor_role     = str(payload.get("role", "")).strip()
        already_impersonating = bool(payload.get("imp_by"))

        if actor_role not in ("admin", "designated"):
            raise HTTPException(status_code=403, detail="Only admin or designated can impersonate")
        if already_impersonating:
            # Disallow nested impersonation — end the current one first.
            raise HTTPException(status_code=409, detail="Already in an impersonation session — stop the current one first")
        if target_username == actor_username:
            raise HTTPException(status_code=422, detail="Cannot impersonate yourself")

        async with async_session() as session:
            target = (await session.execute(
                select(User).where(User.username == target_username)
            )).scalar_one_or_none()
            if not target:
                raise HTTPException(status_code=404, detail="Target user not found")
            if target.terminated_at is not None or not target.is_active:
                raise HTTPException(status_code=400, detail="Target user is terminated / inactive")
            if target.suspended_at is not None:
                raise HTTPException(status_code=400, detail="Target user is suspended")

            # Permission ladder.
            if actor_role == "admin" and target.role != "partner":
                raise HTTPException(status_code=403, detail="Admin can only impersonate partners")

            # Audit row first — if token mint fails, we still have the
            # attempt logged. ended_at left NULL; will be filled by
            # /stop-impersonate or post-hoc expiry sweep.
            ev = ImpersonationEvent(
                actor_username=actor_username, actor_role_at_time=actor_role,
                target_username=target.username, target_role_at_time=target.role,
            )
            session.add(ev)
            await session.commit()
            logger.warning(
                f"IMPERSONATE start: actor={actor_username!r} ({actor_role}) "
                f"→ target={target.username!r} ({target.role}) event_id={ev.id}"
            )

            token = _make_token(target, imp_by=actor_username, ttl_seconds=_IMPERSONATE_TTL_SECONDS)
            return LoginResponse(
                token=token, username=target.username, role=target.role,
                display_name=target.display_name or "",
                must_change_password=False,
            )

    @post("/stop-impersonate", guards=[jwt_guard])
    async def stop_impersonate(self, request: Request) -> LoginResponse:
        payload = getattr(request.state, "token_payload", {}) or {}
        imp_by = str(payload.get("imp_by", "")).strip()
        target_username = str(payload.get("sub", "")).strip()
        if not imp_by:
            raise HTTPException(status_code=400, detail="Not an impersonation session")

        async with async_session() as session:
            # Close out the most recent open audit row.
            from sqlalchemy import update
            await session.execute(
                update(ImpersonationEvent)
                .where(
                    ImpersonationEvent.actor_username == imp_by,
                    ImpersonationEvent.target_username == target_username,
                    ImpersonationEvent.ended_at.is_(None),
                )
                .values(ended_at=datetime.now(timezone.utc), end_reason="stopped")
            )
            await session.commit()

            actor = (await session.execute(
                select(User).where(User.username == imp_by)
            )).scalar_one_or_none()
            if not actor:
                raise HTTPException(status_code=404, detail="Original actor not found")
            logger.warning(
                f"IMPERSONATE end: actor={imp_by!r} ← target={target_username!r}"
            )
            new_token = _make_token(actor)
            return LoginResponse(
                token=new_token, username=actor.username, role=actor.role,
                display_name=actor.display_name or "",
                must_change_password=False,
            )

    # ------------------------------------------------------------------
    # Email verification + password reset
    # ------------------------------------------------------------------

    @get("/verify-email", guards=[_verify_email_rate_limit])
    async def verify_email(self, token: str = "") -> Redirect:
        """Click-target for the email verification link.

        Atomic consume — a single conditional UPDATE marks the token used
        only if it's still un-used and not expired. Two parallel clicks
        on the same link can't both succeed (TOCTOU-safe).
        """
        if not token:
            return Redirect(path="/signin?verified=0")
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            result = await session.execute(
                update(AuthToken)
                .where(
                    AuthToken.token == token,
                    AuthToken.purpose == "verify",
                    AuthToken.used_at.is_(None),
                    AuthToken.expires_at > now,
                )
                .values(used_at=now)
                .returning(AuthToken.user_id)
            )
            row = result.first()
            if not row:
                return Redirect(path="/signin?verified=0")
            user = await session.get(User, row[0])
            if not user:
                return Redirect(path="/signin?verified=0")
            user.email_verified = True
            await session.commit()
        logger.info(f"Auth: verified email for {user.username!r}")
        return Redirect(path="/signin?verified=1")

    @post("/forgot-password", guards=[_forgot_pw_rate_limit])
    async def forgot_password(self, data: ForgotPasswordRequest) -> dict:
        """Always returns 200 with the same message — prevents account
        enumeration. The email send is dispatched off the event loop so
        the response time is identical for hit + miss (closes the
        timing side-channel that the earlier dummy-hash pad only
        crudely approximated)."""
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
                _dispatch_async(_send_reset_email, user.display_name, user.email or "", tok)
                logger.info(f"Auth: reset link issued for {user.username!r}")
            elif user:
                logger.info(
                    f"Auth: reset suppressed for inactive/terminated "
                    f"user {user.username!r}"
                )
            else:
                logger.info(f"Auth: reset for unknown identifier {ident!r}")
        return {"detail": "If the account exists, a reset link has been emailed."}

    @post("/reset-password", guards=[_reset_pw_rate_limit])
    async def reset_password(self, data: ResetPasswordRequest) -> dict:
        """Atomic consume — single conditional UPDATE on auth_tokens marks
        the token used only if it's still un-used + not expired. After
        that the password is set and `token_version` is bumped so the
        next request from any device with an old JWT gets bounced by
        jwt_guard."""
        ok, msg = validate_password_standard(data.password)
        if not ok:
            raise HTTPException(status_code=422, detail=msg)
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            result = await session.execute(
                update(AuthToken)
                .where(
                    AuthToken.token == data.token,
                    AuthToken.purpose == "reset",
                    AuthToken.used_at.is_(None),
                    AuthToken.expires_at > now,
                )
                .values(used_at=now)
                .returning(AuthToken.user_id)
            )
            row = result.first()
            if not row:
                raise HTTPException(status_code=400, detail="Reset link is invalid or expired")
            user = await session.get(User, row[0])
            if not user or not user.is_active or user.terminated_at is not None:
                raise HTTPException(status_code=400, detail="Reset link is invalid or expired")
            user.password_hash = hash_password(data.password)
            user.token_version = (user.token_version or 1) + 1
            await session.commit()
        logger.info(f"Auth: password reset for {user.username!r}")
        return {"detail": "Password reset. Sign in with your new password."}

    @post("/change-password", guards=[jwt_guard])
    async def change_password(self, data: ChangePasswordRequest, request: Request) -> LoginResponse:
        """Force-change-password endpoint used after an admin reset.

        The user is authenticated via the JWT they got from /login —
        and that JWT is THE ONLY thing they can do while the
        must_change_password flag is set (auth_guard rejects every
        other path). This endpoint:
          1. Validates the new password against the standard.
          2. Hashes + writes it.
          3. Clears must_change_password.
          4. Bumps token_version so any other device the user might
             have left signed in gets bounced.
          5. Mints a fresh JWT carrying the new tv and returns it,
             so the client can replace the now-invalid one in
             sessionStorage and continue.
        """
        ok, msg = validate_password_standard(data.password)
        if not ok:
            raise HTTPException(status_code=422, detail=msg)
        sub = (getattr(request.state, "token_payload", {}) or {}).get("sub", "")
        async with async_session() as session:
            result = await session.execute(select(User).where(User.username == sub))
            user = result.scalar_one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            user.password_hash         = hash_password(data.password)
            user.must_change_password  = False
            user.token_version         = (user.token_version or 1) + 1
            await session.commit()
        new_token = _make_token(user)
        logger.info(f"Auth: change_password completed for {user.username!r}")
        return LoginResponse(
            access_token=new_token,
            username=user.username,
            role=user.role,
            display_name=user.display_name,
            must_change_password=False,
        )

    # ------------------------------------------------------------------
    # NAV slice — operator's share of the firm's current portfolio
    # ------------------------------------------------------------------

    @get("/firm-nav")
    async def get_firm_nav_public(self) -> dict:
        """Public, unauthenticated firm-aggregate NAV.

        Returns only firm-level figures (no slices, no partner count,
        no role info). Cached behind _compute_firm_nav's 30 s memo so
        repeated polls don't hammer the broker. Used by NavCard on the
        public /performance page so investors can see the live firm
        NAV without signing in. Designated/admin operators get the
        same numbers plus their share via the authenticated /me/nav."""
        firm_nav, firm_day_pnl, firm_cum_pnl, as_of = await _compute_firm_nav()
        return {
            "firm_nav":     round(firm_nav,     2),
            "firm_day_pnl": round(firm_day_pnl, 2),
            "firm_cum_pnl": round(firm_cum_pnl, 2),
            "as_of":        as_of,
        }

    @get("/me/nav", guards=[jwt_guard])
    async def get_my_nav(self, request: Request) -> dict:
        """Return the operator's share of the firm's current NAV + P&L.

        Pulls live figures from the most-recent _intraday_equity deque
        point (populated every ~5 min by the background task). Falls
        back to a one-shot synchronous broker fetch off-hours or when
        the deque is empty. Swallows broker errors and returns 0s with
        a warning log so the frontend never hard-errors.

        Response keys:
          role / share_pct / contribution / share_nav / share_day_pnl /
          share_cum_pnl / as_of
          (designated + admin only) firm_nav / firm_day_pnl /
          firm_cum_pnl / partner_count
        """
        payload  = request.state.token_payload
        username = payload.get("sub", "")

        async with async_session() as session:
            result = await session.execute(
                select(User.role, User.share_pct, User.contribution)
                .where(User.username == username)
            )
            row = result.first()

        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        role: str         = row.role or "partner"
        share_pct: float  = float(row.share_pct or 0.0)
        contribution: float = float(row.contribution or 0.0)

        firm_nav, firm_day_pnl, firm_cum_pnl, as_of = await _compute_firm_nav()

        share_nav     = firm_nav      * share_pct / 100
        share_day_pnl = firm_day_pnl  * share_pct / 100
        share_cum_pnl = firm_cum_pnl  * share_pct / 100

        resp: dict = {
            "role":           role,
            "share_pct":      share_pct,
            "contribution":   contribution,
            "share_nav":      round(share_nav,     2),
            "share_day_pnl":  round(share_day_pnl, 2),
            "share_cum_pnl":  round(share_cum_pnl, 2),
            "as_of":          as_of,
        }

        # Designated + admin see firm-level figures + partner count
        if role in ("admin", "designated"):
            async with async_session() as session:
                cnt_result = await session.execute(
                    select(func.count()).select_from(User).where(
                        User.role.in_(["partner", "designated"]),
                        User.share_pct > 0,
                        User.is_active.is_(True),
                    )
                )
                partner_count = cnt_result.scalar_one() or 0

            resp["firm_nav"]       = round(firm_nav,     2)
            resp["firm_day_pnl"]   = round(firm_day_pnl, 2)
            resp["firm_cum_pnl"]   = round(firm_cum_pnl, 2)
            resp["partner_count"]  = partner_count

        logger.info(
            f"Auth /me/nav: {username!r} role={role} share_pct={share_pct} "
            f"share_nav={share_nav:.0f}"
        )
        return resp
