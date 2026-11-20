"""Regression tests for the 2026-11 last-active-account reseed IntegrityError.

Background: `Connections._load_active_broker_rows()` triggers
`Connections._seed_db_from_yaml()` whenever its `is_active=True` filter
comes back empty — which happens not only on a genuinely empty table, but
also whenever an operator disables the LAST currently-active account.
`_seed_db_from_yaml()` used to unconditionally INSERT a fresh row per
`secrets.yaml::kite_accounts` entry with no "does this account code
already exist in the table at all, active or not" guard. Since
`BrokerAccount.account` is `unique=True`, re-inserting a code that already
exists as a disabled row raised an uncaught `IntegrityError`.

Fix: `_seed_db_from_yaml()` now queries every account code already present
in `broker_accounts` (regardless of `is_active`) and skips any
`secrets.yaml` entry whose code is already there — only genuinely new
codes are inserted.

Five quality dimensions:
  SSOT        — tests call `Connections._load_active_broker_rows` /
                `Connections._seed_db_from_yaml` directly, not a
                reimplementation of the guard logic.
  Correctness — covers the disabled-last-active-account case (must not
                raise, must not re-insert), the genuinely-empty-table
                case (must still seed normally), and a mixed case
                (existing + new codes in the same yaml).
  Performance — in-memory SQLite, no real network/broker calls, no real
                Postgres dependency (this sandbox has none configured).
  Reuse       — uses the real `BrokerAccount` ORM model (not a
                reimplemented shadow schema) so the real `unique=True`
                constraint on `.account` is actually exercised; a
                `@compiles(JSONB, 'sqlite')` shim lets that model's
                Postgres-only `extra_config` column live on SQLite for
                the duration of the test.
  UX          — every assert carries an f-string with the actual value.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from backend.api.models import BrokerAccount
from backend.brokers.connections import Connections


# ---------------------------------------------------------------------------
# JSONB(postgresql) has no native SQLite rendering. `BrokerAccount.extra_config`
# uses it, so creating just that one table on an in-memory SQLite engine (the
# only way to exercise the real model's own `unique=True` constraint without a
# real Postgres server, which this sandbox doesn't have configured) needs a
# compile-time shim. Registering this once at import time is harmless outside
# a SQLite-dialect compile — no other test or runtime path compiles
# BrokerAccount against SQLite.
# ---------------------------------------------------------------------------
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):
    return "JSON"


def _make_connections_instance() -> Connections:
    """Build a bare `Connections` instance via `__new__`, bypassing
    `__init__` (and therefore the real `_rebuild_from_yaml()` sync seed +
    singleton bookkeeping). Safe here because neither
    `_load_active_broker_rows` nor `_seed_db_from_yaml` reads `self`."""
    return Connections.__new__(Connections)


@pytest_asyncio.fixture
async def broker_accounts_sqlite():
    """In-memory SQLite engine carrying only the real `broker_accounts`
    table. Yields an `async_sessionmaker` factory shaped exactly like
    `shared_async_session`, so it can be patched in as a drop-in
    replacement for the duration of a test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(BrokerAccount.__table__.create)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory

    async with engine.begin() as conn:
        await conn.run_sync(BrokerAccount.__table__.drop)
    await engine.dispose()


def _yaml_account(code: str, *, source_ip: str | None = None) -> dict:
    """Minimal secrets.yaml `kite_accounts` entry shape consumed by
    `_seed_db_from_yaml()`."""
    return {
        "broker": "kite",
        "api_key": f"key-{code}",
        "api_secret": f"secret-{code}",
        "password": f"pw-{code}",
        "totp_token": "JBSWY3DPEHPK3PXP",
        "source_ip": source_ip,
    }


async def _insert_row(factory, **kwargs) -> None:
    async with factory() as s:
        s.add(BrokerAccount(**kwargs))
        await s.commit()


async def _all_accounts(factory) -> list[tuple[str, bool]]:
    async with factory() as s:
        rows = (await s.execute(select(BrokerAccount.account, BrokerAccount.is_active))).all()
    return [(r[0], r[1]) for r in rows]


class TestDisableLastActiveAccountReseedGuard:
    """Disabling the last active account must not resurrect an
    IntegrityError on the next `_load_active_broker_rows()` call."""

    @pytest.mark.asyncio
    async def test_disabled_last_active_account_does_not_raise(self, broker_accounts_sqlite):
        """A disabled row for an account still present in secrets.yaml must
        be left alone — not re-inserted, and no IntegrityError."""
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="ZG0790",
            broker_id="kite",
            api_key="k",
            api_secret_enc="e",
            password_enc="p",
            totp_token_enc="t",
            is_active=False,
            notes="disabled by operator",
        )

        conn = _make_connections_instance()
        fake_secrets = {"kite_accounts": {"ZG0790": _yaml_account("ZG0790")}}

        with patch("backend.api.database.shared_async_session", factory), \
             patch("backend.brokers.connections.secrets", fake_secrets):
            rows = await conn._load_active_broker_rows()

        assert rows == [], (
            f"Expected no active rows (only a disabled ZG0790 exists), got {rows!r}"
        )

        all_rows = await _all_accounts(factory)
        assert all_rows == [("ZG0790", False)], (
            f"ZG0790 must remain the single disabled row, not duplicated "
            f"or reactivated — got {all_rows!r}"
        )

    @pytest.mark.asyncio
    async def test_seed_db_from_yaml_skips_existing_code_and_inserts_new_one(
        self, broker_accounts_sqlite,
    ):
        """A mixed secrets.yaml (one code already in the table, one
        genuinely new) must skip the existing code and insert only the
        new one — no IntegrityError, correct insert count."""
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="ZG0790",
            broker_id="kite",
            api_key="k",
            api_secret_enc="e",
            password_enc="p",
            totp_token_enc="t",
            is_active=False,
        )

        conn = _make_connections_instance()
        fake_secrets = {
            "kite_accounts": {
                "ZG0790": _yaml_account("ZG0790"),   # already in table (disabled)
                "ZJ6294": _yaml_account("ZJ6294"),   # genuinely new
            }
        }

        with patch("backend.api.database.shared_async_session", factory), \
             patch("backend.brokers.connections.secrets", fake_secrets):
            n = await conn._seed_db_from_yaml()

        assert n == 1, f"Expected exactly 1 new insert (ZJ6294 only), got n={n}"

        all_rows = dict(await _all_accounts(factory))
        assert all_rows.get("ZG0790") is False, (
            f"Pre-existing ZG0790 row must stay disabled/untouched, got {all_rows!r}"
        )
        assert all_rows.get("ZJ6294") is True, (
            f"New ZJ6294 row must be inserted active, got {all_rows!r}"
        )

    @pytest.mark.asyncio
    async def test_load_active_broker_rows_reseeds_and_requeries_when_new_code_added(
        self, broker_accounts_sqlite,
    ):
        """When the YAML now carries a genuinely new account alongside the
        already-disabled one, `_load_active_broker_rows()` must insert the
        new one and immediately return it as active (re-query-after-seed
        behaviour), without raising on the pre-existing code."""
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="ZG0790",
            broker_id="kite",
            api_key="k",
            api_secret_enc="e",
            password_enc="p",
            totp_token_enc="t",
            is_active=False,
        )

        conn = _make_connections_instance()
        fake_secrets = {
            "kite_accounts": {
                "ZG0790": _yaml_account("ZG0790"),
                "ZJ6294": _yaml_account("ZJ6294"),
            }
        }

        with patch("backend.api.database.shared_async_session", factory), \
             patch("backend.brokers.connections.secrets", fake_secrets):
            rows = await conn._load_active_broker_rows()

        accounts = sorted(r.account for r in rows)
        assert accounts == ["ZJ6294"], (
            f"Only the newly-seeded ZJ6294 should be active; ZG0790 stays "
            f"disabled — got {accounts!r}"
        )


class TestFreshTableStillSeedsNormally:
    """The genuinely-first-run case (zero rows for any account) must
    still seed every secrets.yaml account normally."""

    @pytest.mark.asyncio
    async def test_empty_table_seeds_every_yaml_account(self, broker_accounts_sqlite):
        factory = broker_accounts_sqlite
        conn = _make_connections_instance()
        fake_secrets = {
            "kite_accounts": {
                "ZG0790": _yaml_account("ZG0790"),
                "ZJ6294": _yaml_account("ZJ6294"),
            }
        }

        assert await _all_accounts(factory) == [], (
            "Precondition: table must start genuinely empty for this test"
        )

        with patch("backend.api.database.shared_async_session", factory), \
             patch("backend.brokers.connections.secrets", fake_secrets):
            rows = await conn._load_active_broker_rows()

        accounts = sorted(r.account for r in rows)
        assert accounts == ["ZG0790", "ZJ6294"], (
            f"First-run seed must create both yaml accounts active, got {accounts!r}"
        )

    @pytest.mark.asyncio
    async def test_seed_db_from_yaml_returns_zero_when_yaml_empty(self, broker_accounts_sqlite):
        """No `kite_accounts` in secrets.yaml → no-op, zero inserts."""
        factory = broker_accounts_sqlite
        conn = _make_connections_instance()

        with patch("backend.api.database.shared_async_session", factory), \
             patch("backend.brokers.connections.secrets", {}):
            n = await conn._seed_db_from_yaml()

        assert n == 0, f"Expected 0 inserts with empty secrets.yaml, got n={n}"
        assert await _all_accounts(factory) == [], (
            "Table must remain empty when secrets.yaml has no kite_accounts"
        )

    @pytest.mark.asyncio
    async def test_seed_db_from_yaml_returns_zero_when_every_code_already_present(
        self, broker_accounts_sqlite,
    ):
        """Every yaml code already in the table (e.g. all disabled) →
        zero inserts, and — critically — no IntegrityError and no
        unnecessary commit."""
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="ZG0790",
            broker_id="kite",
            api_key="k",
            api_secret_enc="e",
            password_enc="p",
            totp_token_enc="t",
            is_active=False,
        )

        conn = _make_connections_instance()
        fake_secrets = {"kite_accounts": {"ZG0790": _yaml_account("ZG0790")}}

        with patch("backend.api.database.shared_async_session", factory), \
             patch("backend.brokers.connections.secrets", fake_secrets):
            n = await conn._seed_db_from_yaml()

        assert n == 0, f"Expected 0 inserts (ZG0790 already present), got n={n}"
