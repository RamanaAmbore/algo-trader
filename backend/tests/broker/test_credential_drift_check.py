"""Tests for `backend/brokers/credential_drift_check.py`.

Background: conn_service builds every broker connection from the
`broker_accounts` DB table, never directly from `secrets.yaml` — an
already-present DB row makes the matching `secrets.yaml` entry inert.
The 2026-11-20 ZJ6294 incident burned hours of live debugging because a
`secrets.yaml` edit silently didn't take effect. `run_credential_drift_check()`
is a one-shot startup check that warns (never auto-fixes) when
secrets.yaml and the DB disagree on a credential for an account that
exists in both.

Five quality dimensions:
  SSOT        — tests call `run_credential_drift_check` /
                `_check_one_account` directly, never a reimplementation
                of the comparison logic.
  Correctness — covers a real match (no warning), a mismatch on each of
                the two compared fields, Dhan and Groww (not just Kite),
                a per-account decrypt failure that must not block other
                accounts, a DB-read failure, and an account not yet in
                the DB at all.
  Performance — in-memory SQLite, no real network/broker calls.
  Reuse       — uses the real `BrokerAccount` ORM model and the real
                `encrypt()`/`decrypt()` Fernet helpers (so decryption
                genuinely round-trips), the same `@compiles(JSONB,
                'sqlite')` shim `test_seed_db_from_yaml.py` already uses.
  UX          — every assert carries an f-string with the actual value;
                the no-raw-secret-in-logs test builds the FULL formatted
                message (not just the format string) before searching it,
                since `logger.warning(fmt, *args)`'s raw secret lives in
                the args, not the fmt string.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from backend.api.models import BrokerAccount
from backend.brokers import credential_drift_check as cdc
from backend.shared.helpers.broker_creds import encrypt


# ---------------------------------------------------------------------------
# JSONB(postgresql) has no native SQLite rendering — same shim as
# test_seed_db_from_yaml.py, needed to create BrokerAccount.__table__ on an
# in-memory SQLite engine.
# ---------------------------------------------------------------------------
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(element, compiler, **kw):
    return "JSON"


@pytest_asyncio.fixture
async def broker_accounts_sqlite():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(BrokerAccount.__table__.create)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory

    async with engine.begin() as conn:
        await conn.run_sync(BrokerAccount.__table__.drop)
    await engine.dispose()


async def _insert_row(factory, **kwargs) -> None:
    kwargs.setdefault("api_secret_enc", encrypt("placeholder-api-secret"))
    kwargs.setdefault("password_enc", encrypt("placeholder-pin"))
    kwargs.setdefault("totp_token_enc", encrypt("placeholder-totp"))
    async with factory() as s:
        s.add(BrokerAccount(**kwargs))
        await s.commit()


async def _run_check(factory, fake_secrets):
    """Run `run_credential_drift_check()` with the DB and secrets.yaml
    patched to the test fixtures, and return the mock logger so callers
    can inspect every `.warning(...)` call."""
    mock_logger = MagicMock()
    with patch("backend.api.database.shared_async_session", factory), \
         patch.object(cdc, "secrets", fake_secrets), \
         patch.object(cdc, "logger", mock_logger):
        await cdc.run_credential_drift_check()
    return mock_logger


def _all_warning_text(mock_logger) -> str:
    """Render every `.warning(fmt, *args)` call into its FULLY FORMATTED
    text (fmt % args), concatenated. Searching the raw fmt string alone
    would miss a secret value passed as a lazy-%s arg."""
    parts = []
    for call in mock_logger.warning.call_args_list:
        args = call.args
        if not args:
            continue
        fmt, fmt_args = args[0], args[1:]
        try:
            parts.append(fmt % fmt_args if fmt_args else fmt)
        except Exception:
            parts.append(fmt + " " + " ".join(str(a) for a in fmt_args))
    return "\n".join(parts)


# ---------------------------------------------------------------------------


class TestMatchNoWarning:
    @pytest.mark.asyncio
    async def test_matching_kite_credentials_produce_no_warning(self, broker_accounts_sqlite):
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="ZG0790",
            broker_id="kite",
            api_key="shared-key-123",
            api_secret_enc=encrypt("shared-secret-abc"),
        )
        fake_secrets = {
            "kite_accounts": {
                "ZG0790": {
                    "api_key": "shared-key-123",
                    "api_secret": "shared-secret-abc",
                }
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        mock_logger.warning.assert_not_called()


class TestMismatch:
    @pytest.mark.asyncio
    async def test_api_key_mismatch_logs_one_warning(self, broker_accounts_sqlite):
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="ZG0790",
            broker_id="kite",
            api_key="db-key-OLD",
            api_secret_enc=encrypt("shared-secret-abc"),
        )
        fake_secrets = {
            "kite_accounts": {
                "ZG0790": {
                    "api_key": "yaml-key-NEW",
                    "api_secret": "shared-secret-abc",
                }
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        assert mock_logger.warning.call_count == 1, (
            f"Expected exactly one warning for the api_key mismatch, "
            f"got {mock_logger.warning.call_count}: "
            f"{mock_logger.warning.call_args_list!r}"
        )
        text = _all_warning_text(mock_logger)
        assert "[CREDENTIAL-DRIFT]" in text, f"Missing tag in: {text!r}"
        assert "api_key" in text, f"Expected field name api_key in: {text!r}"
        assert "/api/admin/brokers/" in text, f"Missing admin-PATCH hint in: {text!r}"

    @pytest.mark.asyncio
    async def test_api_secret_mismatch_logs_one_warning(self, broker_accounts_sqlite):
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="ZJ6294",
            broker_id="kite",
            api_key="shared-key-123",
            api_secret_enc=encrypt("db-secret-STALE"),
        )
        fake_secrets = {
            "kite_accounts": {
                "ZJ6294": {
                    "api_key": "shared-key-123",
                    "api_secret": "yaml-secret-FRESH",
                }
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        assert mock_logger.warning.call_count == 1, (
            f"Expected exactly one warning for the api_secret mismatch, "
            f"got {mock_logger.warning.call_count}"
        )
        text = _all_warning_text(mock_logger)
        assert "api_secret" in text, f"Expected field name api_secret in: {text!r}"
        # Masked account code, never the raw account, must appear.
        assert "ZJ6294" not in text, (
            f"Raw account code must be masked before logging, got: {text!r}"
        )

    @pytest.mark.asyncio
    async def test_both_fields_mismatch_logs_two_warnings(self, broker_accounts_sqlite):
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="ZG0790",
            broker_id="kite",
            api_key="db-key",
            api_secret_enc=encrypt("db-secret"),
        )
        fake_secrets = {
            "kite_accounts": {
                "ZG0790": {"api_key": "yaml-key", "api_secret": "yaml-secret"}
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        assert mock_logger.warning.call_count == 2, (
            f"Expected one warning per differing field (2), got "
            f"{mock_logger.warning.call_count}"
        )


class TestDhanAndGroww:
    @pytest.mark.asyncio
    async def test_dhan_api_secret_mismatch_logs_warning(self, broker_accounts_sqlite):
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="DH3747",
            broker_id="dhan",
            api_key="dhan-app-key",
            api_secret_enc=encrypt("dhan-db-app-secret"),
            client_id="1102345678",
        )
        fake_secrets = {
            "dhan_accounts": {
                "DH3747": {
                    "api_key": "dhan-app-key",
                    "api_secret": "dhan-yaml-app-secret-DIFFERENT",
                }
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        assert mock_logger.warning.call_count == 1, (
            f"Expected exactly one Dhan api_secret warning, got "
            f"{mock_logger.warning.call_count}"
        )
        text = _all_warning_text(mock_logger)
        assert "api_secret" in text, f"Expected api_secret field name in: {text!r}"

    @pytest.mark.asyncio
    async def test_dhan_matching_credentials_no_warning(self, broker_accounts_sqlite):
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="DH3747",
            broker_id="dhan",
            api_key="dhan-app-key",
            api_secret_enc=encrypt("dhan-shared-secret"),
            client_id="1102345678",
        )
        fake_secrets = {
            "dhan_accounts": {
                "DH3747": {
                    "api_key": "dhan-app-key",
                    "api_secret": "dhan-shared-secret",
                    # Dhan rows also carry source_ip in real secrets.yaml —
                    # must not be compared or confused with a credential field.
                    "source_ip": "69.62.78.136",
                }
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        mock_logger.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_groww_totp_token_mismatch_logs_warning_not_api_secret(
        self, broker_accounts_sqlite
    ):
        """Groww's connection builder never reads api_secret_enc at all
        (its approval-secret flow was retired — see `_build_groww_conn`).
        A Groww account must be compared on api_key/totp_token, and must
        never spuriously compare (or warn about) api_secret."""
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="GR1234",
            broker_id="groww",
            api_key="groww-jwt-api-key",
            api_secret_enc=encrypt("irrelevant-unused-by-groww"),
            totp_token_enc=encrypt("db-totp-seed-OLD"),
        )
        fake_secrets = {
            "groww_accounts": {
                "GR1234": {
                    "api_key": "groww-jwt-api-key",
                    "totp_token": "yaml-totp-seed-NEW",
                    # Deliberately different from the DB's api_secret_enc
                    # plaintext — must NOT trigger a warning, since Groww
                    # has no api_secret field in its compare map.
                    "api_secret": "some-other-unrelated-value",
                }
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        assert mock_logger.warning.call_count == 1, (
            f"Expected exactly one warning (totp_token only), got "
            f"{mock_logger.warning.call_count}: "
            f"{mock_logger.warning.call_args_list!r}"
        )
        text = _all_warning_text(mock_logger)
        assert "totp_token" in text, f"Expected totp_token field name in: {text!r}"
        assert "disagree on api_secret" not in text, (
            f"Groww must never be compared on api_secret: {text!r}"
        )

    @pytest.mark.asyncio
    async def test_groww_matching_credentials_no_warning(self, broker_accounts_sqlite):
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="GR1234",
            broker_id="groww",
            api_key="groww-jwt-api-key",
            totp_token_enc=encrypt("shared-totp-seed"),
        )
        fake_secrets = {
            "groww_accounts": {
                "GR1234": {
                    "api_key": "groww-jwt-api-key",
                    "totp_token": "shared-totp-seed",
                }
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        mock_logger.warning.assert_not_called()


class TestFailuresDoNotBlock:
    @pytest.mark.asyncio
    async def test_decrypt_failure_for_one_account_does_not_block_others(
        self, broker_accounts_sqlite
    ):
        """A corrupt/undecryptable api_secret_enc for one account must log
        its own warning and move on — the NEXT account's genuine mismatch
        must still be detected and warned about."""
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="ZG0790",
            broker_id="kite",
            api_key="shared-key",
            api_secret_enc="not-a-valid-fernet-token",  # decrypt() will raise
        )
        await _insert_row(
            factory,
            account="ZJ6294",
            broker_id="kite",
            api_key="shared-key-2",
            api_secret_enc=encrypt("db-secret-stale"),
        )
        fake_secrets = {
            "kite_accounts": {
                "ZG0790": {"api_key": "shared-key", "api_secret": "yaml-secret-irrelevant"},
                "ZJ6294": {"api_key": "shared-key-2", "api_secret": "yaml-secret-fresh"},
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        text = _all_warning_text(mock_logger)
        assert "failed to decrypt" in text, (
            f"Expected a decrypt-failure warning for ZG0790's api_secret, got: {text!r}"
        )
        assert "disagree on api_secret" in text, (
            f"ZJ6294's genuine api_secret mismatch must still be detected "
            f"despite ZG0790's decrypt failure, got: {text!r}"
        )
        # Exactly: 1 decrypt-failure warning (ZG0790.api_secret) +
        # 1 real mismatch warning (ZJ6294.api_secret). api_key matched on
        # both accounts, so no extra warnings from that field.
        assert mock_logger.warning.call_count == 2, (
            f"Expected exactly 2 warnings total, got "
            f"{mock_logger.warning.call_count}: "
            f"{mock_logger.warning.call_args_list!r}"
        )

    @pytest.mark.asyncio
    async def test_db_read_failure_logs_warning_and_returns(self, broker_accounts_sqlite):
        """A broker_accounts read failure must degrade to one WARNING and
        return — never raise, never crash startup."""
        mock_logger = MagicMock()
        broken_session_factory = MagicMock(side_effect=RuntimeError("DB unreachable"))

        with patch("backend.api.database.shared_async_session", broken_session_factory), \
             patch.object(cdc, "secrets", {"kite_accounts": {"ZG0790": {"api_key": "x", "api_secret": "y"}}}), \
             patch.object(cdc, "logger", mock_logger):
            # Must not raise.
            await cdc.run_credential_drift_check()

        assert mock_logger.warning.call_count == 1, (
            f"Expected exactly one DB-read-failure warning, got "
            f"{mock_logger.warning.call_count}"
        )
        text = _all_warning_text(mock_logger)
        assert "broker_accounts read failed" in text, f"Unexpected warning text: {text!r}"

    @pytest.mark.asyncio
    async def test_account_not_yet_in_db_is_skipped_silently(self, broker_accounts_sqlite):
        """An account present only in secrets.yaml (genuinely new, not yet
        seeded) has nothing to compare against — must not warn or crash."""
        factory = broker_accounts_sqlite
        fake_secrets = {
            "kite_accounts": {
                "ZZ9999": {"api_key": "brand-new-key", "api_secret": "brand-new-secret"}
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        mock_logger.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_yaml_field_is_not_compared(self, broker_accounts_sqlite):
        """A dhan_accounts/groww_accounts entry that today only carries
        source_ip (no api_key/api_secret at all) must never produce a
        guaranteed-meaningless mismatch warning."""
        factory = broker_accounts_sqlite
        await _insert_row(
            factory,
            account="DH6847",
            broker_id="dhan",
            api_key="db-only-key",
            api_secret_enc=encrypt("db-only-secret"),
        )
        fake_secrets = {
            "dhan_accounts": {
                "DH6847": {"source_ip": "69.62.78.136"}  # no api_key/api_secret keys at all
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        mock_logger.warning.assert_not_called()


class TestNoRawSecretInLogs:
    @pytest.mark.asyncio
    async def test_no_raw_secret_value_ever_appears_in_any_warning_call(
        self, broker_accounts_sqlite
    ):
        """Across every warning call this module can produce in one run
        (match, 2-field mismatch, decrypt failure), the fully-formatted
        message text must never contain any of the known raw secret
        values used as fixtures — only short hash prefixes or '<empty>'."""
        factory = broker_accounts_sqlite
        raw_yaml_key = "RAW-YAML-API-KEY-DO-NOT-LOG-abcdef123456"
        raw_db_key = "RAW-DB-API-KEY-DO-NOT-LOG-zyxwvu654321"
        raw_yaml_secret = "RAW-YAML-API-SECRET-DO-NOT-LOG-999888"
        raw_db_secret = "RAW-DB-API-SECRET-DO-NOT-LOG-111222"

        await _insert_row(
            factory,
            account="ZG0790",
            broker_id="kite",
            api_key=raw_db_key,
            api_secret_enc=encrypt(raw_db_secret),
        )
        await _insert_row(
            factory,
            account="ZJ6294",
            broker_id="kite",
            api_key="shared-ok-key",
            api_secret_enc="not-a-valid-fernet-token",
        )
        fake_secrets = {
            "kite_accounts": {
                "ZG0790": {"api_key": raw_yaml_key, "api_secret": raw_yaml_secret},
                "ZJ6294": {"api_key": "shared-ok-key", "api_secret": "yaml-secret-irrelevant"},
            }
        }

        mock_logger = await _run_check(factory, fake_secrets)

        # Sanity: this run actually produced warnings (api_key mismatch,
        # api_secret mismatch, and a decrypt failure) — otherwise this
        # test would pass vacuously.
        assert mock_logger.warning.call_count >= 3, (
            f"Expected at least 3 warnings to exercise this check, got "
            f"{mock_logger.warning.call_count}"
        )

        text = _all_warning_text(mock_logger)
        for raw in (raw_yaml_key, raw_db_key, raw_yaml_secret, raw_db_secret):
            assert raw not in text, (
                f"Raw secret value {raw!r} leaked into a log message: {text!r}"
            )
        # Also confirm raw account codes never appear unmasked.
        assert "ZG0790" not in text and "ZJ6294" not in text, (
            f"Raw account code leaked into a log message: {text!r}"
        )


class TestHashHelper:
    def test_hash_for_log_is_not_reversible_and_empty_is_distinct(self):
        h1 = cdc._hash_for_log("some-secret-value")
        h2 = cdc._hash_for_log("some-secret-value")
        h3 = cdc._hash_for_log("a-different-secret-value")
        assert h1 == h2, "Same input must hash identically for log correlation"
        assert h1 != h3, "Different inputs must not collide"
        assert "some-secret-value" not in h1, f"Hash must not contain the raw value: {h1!r}"
        assert cdc._hash_for_log("") == "<empty>"
        assert cdc._hash_for_log(None) == "<empty>"
