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
    )


async def _reload_connections() -> None:
    """Trigger Connections.rebuild_from_db so subsequent broker calls
    pick up the new state. Failures are logged but don't fail the
    request (the row is already persisted; reload can retry later)."""
    try:
        from backend.brokers.connections import Connections
        await Connections().rebuild_from_db()
    except Exception as e:
        logger.warning(f"Connections reload after broker mutation failed: {e}")


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
    navbar count, surfacing the outage at a glance."""
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
                in_conn = {r["account"] for r in list_remote_accounts() if r.get("account")}
        return {a for a in in_conn if is_account_healthy(a)}
    except Exception:
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
                select(BrokerAccount).order_by(BrokerAccount.account)
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
            if data.poll_priority is not None:
                valid_priorities = {"hot", "warm", "cold"}
                if data.poll_priority not in valid_priorities:
                    raise HTTPException(
                        status_code=400,
                        detail=f"poll_priority must be one of: {sorted(valid_priorities)}"
                    )
                row.poll_priority = data.poll_priority
                # Manual priority change clears auto-downgrade stamps so
                # the UI can distinguish manual vs auto priority changes.
                if data.poll_priority != getattr(row, "auto_downgrade_reason", None):
                    row.auto_downgraded_at = None
                    row.auto_downgrade_reason = None
            if data.auto_downgrade_enabled is not None:
                row.auto_downgrade_enabled = bool(data.auto_downgrade_enabled)
            if data.circuit_breaker_enabled is not None:
                row.circuit_breaker_enabled = bool(data.circuit_breaker_enabled)

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

        # Immediately update the in-process caches so the interval gate and
        # circuit-breaker opt-in check see the new value even before
        # rebuild_from_db completes (it will also update the caches).
        if data.poll_priority is not None or data.circuit_breaker_enabled is not None:
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

        await _reload_connections()
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
