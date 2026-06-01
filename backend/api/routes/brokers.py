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

from datetime import datetime, timezone
from typing import Optional

import msgspec
from litestar import Controller, delete, get, patch, post
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session
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


class TestResult(msgspec.Struct):
    ok:      bool
    account: str
    detail:  str


# ── Helpers ───────────────────────────────────────────────────────────

def _to_info(row: BrokerAccount, *, loaded: bool = False) -> BrokerAccountInfo:
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
    )


async def _reload_connections() -> None:
    """Trigger Connections.rebuild_from_db so subsequent broker calls
    pick up the new state. Failures are logged but don't fail the
    request (the row is already persisted; reload can retry later)."""
    try:
        from backend.shared.helpers.connections import Connections
        await Connections().rebuild_from_db()
    except Exception as e:
        logger.warning(f"Connections reload after broker mutation failed: {e}")


def _loaded_accounts() -> set[str]:
    """Account codes currently in the Connections singleton — used to
    flag rows as 'loaded' vs 'not yet picked up'."""
    try:
        from backend.shared.helpers.connections import Connections
        return set(Connections().conn.keys())
    except Exception:
        return set()


# ── Controller ────────────────────────────────────────────────────────

class BrokersController(Controller):
    path   = "/api/admin/brokers"
    guards = [admin_guard]

    # ── List + read ───────────────────────────────────────────────────

    @get("/")
    async def list_accounts(self) -> list[BrokerAccountInfo]:
        async with async_session() as s:
            rows = (await s.execute(
                select(BrokerAccount).order_by(BrokerAccount.account)
            )).scalars().all()
        loaded = _loaded_accounts()
        return [_to_info(r, loaded=(r.account in loaded)) for r in rows]

    @get("/{account:str}")
    async def get_account(self, account: str) -> BrokerAccountInfo:
        async with async_session() as s:
            row = (await s.execute(
                select(BrokerAccount).where(BrokerAccount.account == account)
            )).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404,
                                detail=f"Broker account {account!r} not found")
        return _to_info(row, loaded=(account in _loaded_accounts()))

    # ── Create ────────────────────────────────────────────────────────

    @post("/")
    async def create_account(self, data: BrokerAccountCreate) -> BrokerAccountInfo:
        if not data.account:
            raise HTTPException(status_code=400, detail="account is required")
        async with async_session() as s:
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

    # ── Update ────────────────────────────────────────────────────────

    @patch("/{account:str}")
    async def update_account(self, account: str,
                             data: BrokerAccountUpdate) -> BrokerAccountInfo:
        async with async_session() as s:
            row = (await s.execute(
                select(BrokerAccount).where(BrokerAccount.account == account)
            )).scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404,
                    detail=f"Broker account {account!r} not found")

            # Non-secret fields — straight-through.
            if data.broker_id is not None:  row.broker_id = data.broker_id
            if data.api_key   is not None:  row.api_key   = data.api_key
            if data.client_id is not None:  row.client_id = data.client_id or None
            if data.source_ip is not None:  row.source_ip = data.source_ip or None
            if data.is_active is not None:  row.is_active = bool(data.is_active)
            if data.notes     is not None:  row.notes     = data.notes
            if data.priority  is not None:  row.priority  = int(data.priority)
            if data.extra_config is not None: row.extra_config = dict(data.extra_config or {})
            if data.historical_data_enabled is not None:
                row.historical_data_enabled = bool(data.historical_data_enabled)

            # Secret fields — only update when the operator passed a
            # NON-EMPTY string. Empty / None means "leave unchanged" so
            # a partial edit doesn't blank a credential.
            if data.api_secret:    row.api_secret_enc   = encrypt(data.api_secret)
            if data.password:      row.password_enc     = encrypt(data.password)
            if data.totp_token:    row.totp_token_enc   = encrypt(data.totp_token)
            if data.access_token:  row.access_token_enc = encrypt(data.access_token)

            row.updated_at = datetime.now(timezone.utc)
            await s.commit()
            await s.refresh(row)

        await _reload_connections()
        logger.warning(f"broker_accounts: updated {account!r} via /admin/brokers")
        return _to_info(row, loaded=(account in _loaded_accounts()))

    # ── Delete ────────────────────────────────────────────────────────

    @delete("/{account:str}", status_code=200)
    async def delete_account(self, account: str) -> dict:
        async with async_session() as s:
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

    # ── Test connection ───────────────────────────────────────────────

    @post("/{account:str}/test")
    async def test_account(self, account: str) -> TestResult:
        """Try a cheap broker call (profile()) to confirm the credentials
        actually authenticate. Doesn't mutate state — just exercises the
        login path so the operator gets immediate feedback."""
        await _reload_connections()
        try:
            from backend.shared.brokers.registry import get_broker
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
