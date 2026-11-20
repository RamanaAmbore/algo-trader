"""2026-11-20 fix (F9 — live incident: ZG0790 was `is_active=False` for
most of a day, and NAV silently omitted it entirely with no `understated`
flag). `_resolve_conn_keys()` used to return ONLY the live, currently-
active registry (`Connections().conn.keys()` / conn_service's active
accounts). A disabled account is excluded from that registry by
construction, so it was never even "expected" — the existing missing-
account recovery (`_recover_missing_margins_accounts`, exercised in
`test_nav_degraded_fetch_freeze.py`) only flags an account UNDERSTATED
when it's in `expected_accounts` but never seen; a disabled account was
never in that list in the first place.

This file proves `_resolve_conn_keys()` now cross-references the FULL
`broker_accounts` DB table (every row, active or not) and unions it with
the live registry, and that this closes the gap end-to-end: a disabled
account absent from the live fetch now resolves `snap["understated"]`
non-empty, mentioning that account.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from backend.api.algo.nav import _resolve_conn_keys


def _one_shot_session(account_codes: list[str]):
    """Build a single mock async-context-manager session whose
    .execute().scalars().all() returns the given account code strings —
    same AsyncMock __aenter__/__aexit__ pattern already used by
    test_broker_connection_events.py for shared_async_session."""
    session_mock = AsyncMock()
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = account_codes
    session_mock.execute = AsyncMock(return_value=result_mock)
    return session_mock


def _mock_session_with_accounts(account_codes: list[str]):
    """Single-query convenience wrapper over `_one_shot_session` — every
    call to the returned factory answers with the SAME account list.
    Sufficient for tests that only exercise `_resolve_conn_keys()`
    alone."""
    session_mock = _one_shot_session(account_codes)
    factory = MagicMock(return_value=session_mock)
    return factory


def _mock_session_factory_for_two_queries(all_accounts: list[str], inactive_accounts: list[str]):
    """Build a `shared_async_session` factory that answers TWO DISTINCT
    sequential queries in `compute_firm_nav()`'s own call order:
    `_resolve_conn_keys()`'s unfiltered `select(BrokerAccount.account)`
    (called first), then `_resolve_inactive_db_accounts()`'s `WHERE
    is_active IS false` query (called second). Each gets its own fresh
    one-shot session, so the two can answer with different rows."""
    sessions = [
        _one_shot_session(all_accounts),
        _one_shot_session(inactive_accounts),
    ]
    calls = {"n": 0}

    def factory():
        idx = min(calls["n"], len(sessions) - 1)
        calls["n"] += 1
        return sessions[idx]

    return factory


class TestResolveConnKeysCrossReferencesDbTable:
    @pytest.mark.asyncio
    async def test_disabled_account_absent_from_live_registry_is_still_returned(self):
        """THE key fix — a DB row exists for ZG0790 (disabled or not;
        the DB query selects every row with no is_active filter), but
        the live registry (`Connections().conn`) has already excluded
        it. The merged result must still include it."""
        factory = _mock_session_with_accounts(["ZG0790", "DH6847", "GF1234"])
        with patch(
            "backend.brokers.connections.Connections",
        ) as conn_cls, patch(
            "backend.api.database.shared_async_session", factory,
        ):
            conn_cls.return_value.conn = {"DH6847": object(), "GF1234": object()}
            keys = await _resolve_conn_keys()

        assert "ZG0790" in keys, (
            "a DB-known account absent from the live registry must still "
            "be returned — this is the exact live-incident shape"
        )
        assert "DH6847" in keys
        assert "GF1234" in keys

    @pytest.mark.asyncio
    async def test_no_duplicates_when_db_and_live_registry_overlap(self):
        factory = _mock_session_with_accounts(["DH6847", "GF1234"])
        with patch(
            "backend.brokers.connections.Connections",
        ) as conn_cls, patch(
            "backend.api.database.shared_async_session", factory,
        ):
            conn_cls.return_value.conn = {"DH6847": object(), "GF1234": object()}
            keys = await _resolve_conn_keys()

        assert sorted(keys) == ["DH6847", "GF1234"]

    @pytest.mark.asyncio
    async def test_db_query_failure_falls_back_to_registry_only_not_raise(self):
        """Best-effort: a DB outage must degrade to the pre-fix
        registry-only behavior rather than raising — this check must
        never itself become a new way for NAV computation to fail."""
        with patch(
            "backend.brokers.connections.Connections",
        ) as conn_cls, patch(
            "backend.api.database.shared_async_session",
            side_effect=RuntimeError("db down"),
        ):
            conn_cls.return_value.conn = {"DH6847": object()}
            keys = await _resolve_conn_keys()

        assert keys == ["DH6847"]

    @pytest.mark.asyncio
    async def test_empty_live_registry_still_gets_db_accounts(self):
        """Covers the cutover-empty-local-registry branch too — even when
        the live-registry lookup itself returns nothing (e.g. a cold
        boot, or conn_service unreachable), the DB cross-reference still
        surfaces every known account."""
        factory = _mock_session_with_accounts(["ZG0790"])
        with patch(
            "backend.brokers.connections.Connections",
        ) as conn_cls, patch(
            "backend.brokers.client.is_cutover_on", return_value=False,
        ), patch(
            "backend.api.database.shared_async_session", factory,
        ):
            conn_cls.return_value.conn = {}
            keys = await _resolve_conn_keys()

        assert keys == ["ZG0790"]


class TestEndToEndUnderstatedOnDisabledAccount:
    @pytest.mark.asyncio
    async def test_compute_firm_nav_flags_understated_for_a_known_but_unreachable_account(self):
        """End-to-end: `compute_firm_nav()`'s margins phase, fed the now-
        DB-cross-referenced `expected_accounts`, must flag `understated`
        when a known DB account (ZG0790) produced no margins row at all
        (disabled — conn_service never attempted it) and has no
        last-known-good to freeze to."""
        from backend.api.algo.nav import _fetch_funds_phase

        healthy = pd.DataFrame([
            {"account": "DH6847", "avail opening_balance": 5000.0, "util option_premium": 0.0},
        ])
        no_lkg = pd.DataFrame()
        no_lkg.attrs["fetch_failed"] = True

        accounts_in: list[str] = []
        errors: list[str] = []
        # `_resolve_conn_keys()` now returns ["DH6847", "ZG0790"] (DB
        # cross-reference includes the disabled account); the broker
        # fetch itself never produces a ZG0790 frame at all (disabled —
        # conn_service doesn't even know about it).
        with patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.api.algo.nav.asyncio.to_thread",
            new=AsyncMock(return_value=[healthy]),
        ), patch(
            "backend.brokers.broker_apis._stale_substitute_frame", return_value=no_lkg,
        ):
            total = await _fetch_funds_phase(
                accounts_in, errors, expected_accounts=["DH6847", "ZG0790"],
            )

        assert total == pytest.approx(5000.0)
        assert any(
            e.startswith("UNDERSTATED:") and "ZG0790" in e for e in errors
        ), f"expected an UNDERSTATED entry naming ZG0790, got: {errors!r}"

    @pytest.mark.asyncio
    async def test_realistic_post_f4_scenario_margins_lkg_present_still_flags_understated(self):
        """Advisor-driven correction (second round) — an earlier version
        of this test asserted `understated == []` here and documented a
        "known residual" as acceptable. That was wrong: once margins has
        a real LKG for a disabled account (the realistic post-F4 steady
        state — `_preload_db_lkg_cache` now covers `kind='funds'` too),
        margins' own recovery merges the account into `accounts_in`,
        which would otherwise mask the fact that positions/holdings for
        that SAME account are silently 0 with no signal — exactly
        reproducing the live incident's real consequence (an understated
        NAV total) even though the account is technically "present".

        `_resolve_inactive_db_accounts()` closes this directly, keyed on
        the DB's own `is_active` column rather than on `accounts_in`
        membership — see that function's own docstring. This test
        exercises the realistic shape end-to-end through the REAL
        `compute_firm_nav()` + the REAL (not bypassed) `_resolve_conn_
        keys()`/`_resolve_inactive_db_accounts()` DB reads, and now
        asserts the CORRECT, fixed outcome: `understated` names ZG0790,
        even though margins itself has a real LKG value."""
        from backend.api.algo.nav import compute_firm_nav

        # Two distinct queries: `_resolve_conn_keys()` reads every
        # account; `_resolve_inactive_db_accounts()` reads only the
        # disabled ones (ZG0790).
        factory = _mock_session_factory_for_two_queries(
            all_accounts=["ZG0790", "DH6847"], inactive_accounts=["ZG0790"],
        )

        healthy_funds = pd.DataFrame([
            {"account": "DH6847", "avail opening_balance": 5000.0, "util option_premium": 0.0},
        ])
        failed_funds = pd.DataFrame()
        failed_funds.attrs["fetch_failed"] = True
        failed_funds.attrs["account"] = "ZG0790"

        margins_lkg = pd.DataFrame([
            {"account": "ZG0790", "avail opening_balance": 12000.0, "util option_premium": 0.0},
        ])
        margins_lkg.attrs["stale"] = True

        with patch(
            "backend.brokers.connections.Connections",
        ) as conn_cls, patch(
            "backend.api.database.shared_async_session", factory,
        ), patch(
            "backend.api.helpers.snapshot_gate._any_segment_open", return_value=True,
        ), patch(
            "backend.brokers.broker_apis.fetch_margins",
            return_value=[healthy_funds, failed_funds],
        ), patch(
            "backend.brokers.broker_apis.fetch_positions", return_value=[pd.DataFrame()],
        ), patch(
            "backend.api.helpers.snapshot_gate.is_exchange_closed_now", return_value=False,
        ), patch(
            "backend.brokers.broker_apis.fetch_holdings", return_value=[pd.DataFrame()],
        ), patch(
            "backend.brokers.kite_ticker.get_ticker",
            return_value=MagicMock(get_ltp_by_sym=MagicMock(return_value=None)),
        ), patch(
            "backend.brokers.broker_apis._stale_substitute_frame", return_value=margins_lkg,
        ):
            # ZG0790 is disabled: absent from the live registry, but still
            # a real `broker_accounts` DB row — exactly the live incident.
            conn_cls.return_value.conn = {"DH6847": object()}
            snap = await compute_firm_nav()

        assert "ZG0790" in snap["accounts"], (
            "ZG0790 must be present (via the margins LKG merge), not "
            "vanished entirely"
        )
        assert snap["by_account"]["ZG0790"]["cash"] == pytest.approx(12000.0), (
            "margins' own real (if stale) LKG value is still correctly used"
        )
        # Positions/holdings are silently 0 — unknown, not confirmed.
        assert snap["by_account"]["ZG0790"]["pos_m2m"] == 0.0
        assert snap["by_account"]["ZG0790"]["holdings_mtm"] == 0.0
        # THE fix: understated now names ZG0790, flagging that the
        # positions/holdings portion of the total is unknown, even
        # though margins itself is fine.
        assert any("ZG0790" in e for e in snap["understated"]), (
            f"expected an UNDERSTATED entry naming ZG0790, got: {snap['understated']!r}"
        )
        assert any("disabled" in e for e in snap["understated"])
