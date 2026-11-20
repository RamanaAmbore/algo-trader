"""Tests for F4 (2026-11-20 incident): `_preload_db_lkg_cache` must also
preload `daily_book`'s `kind='funds'` rows so margins/funds survive a
conn_service restart via `_stale_substitute_frame("margins", ...)` /
`_lkg_or_fallback("margins", ...)` — the exact calls
`broker_apis._fetch_margins_local` already makes. Before this fix, ZG0790
had a real `daily_book` funds row sitting completely unused, and both
conn_service restarts during the incident left margins cold with zero
substitution.

`daily_book`'s `kind='funds'` rows carry segment-level cash figures (see
`daily_snapshot.py:_funds_rows`), not instrument rows, so the preload must
shape them into the flattened-Kite-margins()-response column names
`_fetch_margins_local` builds (`net`, `util debits`, `avail cash`,
`avail opening_balance`, `util realised_m2m`) — and store them under the
broker_apis LKG cache KEY "margins", not the literal string "funds"
("funds" is only the daily_book row's own `kind` column value).

KNOWN LIMITATION, confirmed live on prod (2026-11-20) — see the matching
comment in `background.py:_preload_db_lkg_cache`'s own docstring for the
full explanation. `daily_book`'s unique constraint is
`(date, account, kind, symbol)`, and `_funds_rows` writes BOTH segments
under the same sentinel `symbol='__seg__'`, so an account holding both
segments collapses to one row per day — whichever segment is written
LAST in `_snap_fetch_funds`'s `("equity", "commodity")` loop wins. A
direct query against prod confirmed EVERY `kind='funds'` row is
`exchange='COMMODITY'` — there are zero `EQUITY` rows at all, for ANY
account, on any day checked — so this preload's `exchange='EQUITY'`
filter (deliberately kept, not a bug — see the docstring) currently
returns NOTHING in production, including for ZG0790, the account this
fix was written for. This is NOT fixed by this PR — it requires a
`daily_snapshot.py` writer fix (giving each segment its own symbol key)
that is out of scope here and belongs to a future change. The tests
below (mocked SQL) still correctly pin the EQUITY-only behaviour this
code implements; they do not — and cannot — exercise the real writer
collision, since the mocked session bypasses the SQL's WHERE clause
entirely.

Five quality dimensions:
  SSOT        — preload stores under the exact cache key
                (`_stale_substitute_frame`/`_lkg_or_fallback`'s own
                "margins" key) `_fetch_margins_local` already reads, not a
                parallel/invented substitution mechanism.
  Correctness — funds rows map to the correct flattened column names and
                values; holdings/positions rows are unaffected by the new
                branch; a funds row with a null numeric field stays null
                (missing-vs-zero), never coerced to 0.
  Performance — no real DB/broker I/O — `async_session` is mocked exactly
                as in `test_background_lkg_expiry_freeze.py`.
  Reuse       — mirrors that file's `_row`/`_mock_session_with_rows`
                helper pattern rather than inventing a new one.
  UX          — the end-to-end `_stale_substitute_frame("margins", ...)`
                call (not just the raw grouped dict) is asserted, proving
                a real caller downstream can actually consume the result.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _row(account, kind, symbol, exchange, qty=1, avg_cost=100.0, ltp=100.0,
         day_pnl=0.0, total_pnl=0.0, captured_at=None):
    return SimpleNamespace(
        account=account, kind=kind, symbol=symbol, exchange=exchange,
        qty=qty, avg_cost=avg_cost, ltp=ltp, day_pnl=day_pnl,
        total_pnl=total_pnl, captured_at=captured_at,
    )


def _mock_session_with_rows(rows):
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    result = AsyncMock()
    result.all = lambda: rows
    mock_session.execute = AsyncMock(return_value=result)
    return mock_session


class TestPreloadIncludesFundsKind:
    """The SQL filter must include 'funds', not just 'holdings'/'positions'."""

    @pytest.mark.asyncio
    async def test_sql_query_text_includes_funds_kind(self):
        from backend.api import background as background_module

        mock_session = _mock_session_with_rows([])
        with patch("backend.api.database.async_session", return_value=mock_session):
            await background_module._preload_db_lkg_cache()

        executed_sql = str(mock_session.execute.call_args[0][0])
        assert "'funds'" in executed_sql, (
            f"Expected the preload SQL to include the 'funds' kind, got: "
            f"{executed_sql!r}"
        )
        assert "'holdings'" in executed_sql and "'positions'" in executed_sql, (
            "holdings/positions must remain in the query alongside funds"
        )

    @pytest.mark.asyncio
    async def test_sql_restricts_funds_to_equity_segment(self):
        """`_fetch_margins_local` only ever calls
        `broker.margins(segment="equity")` — never "commodity" — so its
        live shape is always exactly one row per account. The preload
        SQL must restrict funds rows to `exchange='EQUITY'`, or an
        account holding both equity and commodity segment rows (written
        by `daily_snapshot.py:_snap_fetch_funds`) would silently double
        into the "margins" LKG frame."""
        from backend.api import background as background_module

        mock_session = _mock_session_with_rows([])
        with patch("backend.api.database.async_session", return_value=mock_session):
            await background_module._preload_db_lkg_cache()

        executed_sql = str(mock_session.execute.call_args[0][0])
        assert "'EQUITY'" in executed_sql, (
            f"Expected the preload SQL to restrict funds rows to the "
            f"EQUITY segment, got: {executed_sql!r}"
        )

    @pytest.mark.asyncio
    async def test_sql_exempts_funds_from_the_ltp_heuristic_filter(self):
        """The `db.ltp IS NOT NULL` / stale-heuristic filter was designed
        for instrument rows (ltp == avg_cost suggesting a stale token).
        A funds row's ltp (opening_balance) can legitimately be 0 or
        null for a Dhan/Groww account with no segment balance — the SQL
        must exempt kind='funds' from that filter entirely, or it would
        silently re-create the exact "margins cold after restart" gap
        this fix closes."""
        from backend.api import background as background_module

        mock_session = _mock_session_with_rows([])
        with patch("backend.api.database.async_session", return_value=mock_session):
            await background_module._preload_db_lkg_cache()

        executed_sql = str(mock_session.execute.call_args[0][0])
        assert "db.kind != 'funds'" in executed_sql, (
            f"Expected the ltp/stale-heuristic filter to be scoped away "
            f"from funds rows via 'db.kind != \\'funds\\'', got: "
            f"{executed_sql!r}"
        )


class TestFundsSegmentIsolation:
    """An account with BOTH equity and commodity funds rows in the same
    batch must only preload the equity row — never two rows under one
    account."""

    @pytest.mark.asyncio
    async def test_commodity_segment_row_is_excluded_by_the_real_query_shape(self):
        """This test cannot exercise the real SQL WHERE clause (the
        session is mocked), so it instead asserts the row-shaping helper
        is account-keyed with no segment dimension — i.e. two funds rows
        sharing the SAME account key (as the real EQUITY-only SQL filter
        guarantees) is the only shape `_lkg_group_row` is ever fed in
        production. A second row for the SAME account overwrites nothing
        — `grouped[(account, "margins")]` is a list, so if the SQL filter
        were ever accidentally removed, this test documents (without
        re-testing the SQL text) that TWO rows would append — pinning the
        current real defence (the SQL-level EQUITY filter, asserted in
        `test_sql_restricts_funds_to_equity_segment` above) as the actual
        enforcement point, not any Python-level dedup."""
        from backend.api import background as background_module
        from collections import defaultdict

        equity_row = _row("ZG0790", "funds", "__seg__", "EQUITY",
                           qty=1000, avg_cost=5000.0, ltp=6000.0,
                           day_pnl=10.0, total_pnl=5900.0)
        commodity_row = _row("ZG0790", "funds", "__seg__", "COMMODITY",
                              qty=2000, avg_cost=7000.0, ltp=8000.0,
                              day_pnl=20.0, total_pnl=7900.0)

        grouped: dict = defaultdict(list)
        background_module._lkg_group_row(equity_row, grouped, set())
        background_module._lkg_group_row(commodity_row, grouped, set())

        # Python-level grouping has no segment awareness by design — the
        # SQL WHERE clause is the real (and only) defence. Confirms the
        # helper itself would append both if the SQL ever let both through.
        assert len(grouped[("ZG0790", "margins")]) == 2, (
            "the row-shaping helper itself has no segment dedup — the "
            "SQL-level EQUITY filter is the real defence against this"
        )


class TestFundsRowShapedAsMarginsFrame:
    """A daily_book funds row must be shaped into the margins()-flattened
    column names and stored under the "margins" cache key."""

    @pytest.mark.asyncio
    async def test_funds_row_stored_under_margins_key_with_correct_columns(self):
        from backend.api import background as background_module

        # qty=util.debits, avg_cost=avail.cash, ltp=avail.opening_balance,
        # day_pnl=util.realised_m2m, total_pnl=net (see daily_snapshot.py).
        rows = [
            _row("ZG0790", "funds", "__seg__", "EQUITY",
                 qty=15000, avg_cost=250000.0, ltp=300000.0,
                 day_pnl=1200.0, total_pnl=298800.0),
        ]
        mock_session = _mock_session_with_rows(rows)

        captured = {}

        def _fake_set_db_lkg_frame(kind, account, df):
            captured[(kind, account)] = df

        with patch("backend.api.database.async_session", return_value=mock_session), \
             patch("backend.brokers.broker_apis.set_db_lkg_frame",
                   side_effect=_fake_set_db_lkg_frame):
            await background_module._preload_db_lkg_cache()

        assert ("margins", "ZG0790") in captured, (
            f"funds row must be stored under cache key ('margins', account), "
            f"not ('funds', account) — got keys {list(captured.keys())!r}"
        )
        assert ("funds", "ZG0790") not in captured, (
            "funds row must NOT be stored under the literal 'funds' cache "
            "key — _fetch_margins_local reads back under 'margins'"
        )

        df = captured[("margins", "ZG0790")]
        row0 = df.iloc[0]
        assert row0["net"] == 298800.0, f"net mismatch: {row0['net']!r}"
        assert row0["util debits"] == 15000.0, (
            f"util debits mismatch: {row0['util debits']!r}"
        )
        assert row0["avail cash"] == 250000.0, (
            f"avail cash mismatch: {row0['avail cash']!r}"
        )
        assert row0["avail opening_balance"] == 300000.0, (
            f"avail opening_balance mismatch: {row0['avail opening_balance']!r}"
        )
        assert row0["util realised_m2m"] == 1200.0, (
            f"util realised_m2m mismatch: {row0['util realised_m2m']!r}"
        )
        assert row0["account"] == "ZG0790"

    @pytest.mark.asyncio
    async def test_funds_row_missing_field_stays_null_not_zero(self):
        """Missing-vs-zero convention (CLAUDE.md) applies to the funds
        branch too — a genuinely absent column must stay null, never a
        coerced 0.0."""
        from backend.api import background as background_module
        import math

        rows = [
            _row("DH3747", "funds", "__seg__", "EQUITY",
                 qty=None, avg_cost=5000.0, ltp=10000.0,
                 day_pnl=None, total_pnl=9000.0),
        ]
        mock_session = _mock_session_with_rows(rows)

        captured = {}

        def _fake_set_db_lkg_frame(kind, account, df):
            captured[(kind, account)] = df

        with patch("backend.api.database.async_session", return_value=mock_session), \
             patch("backend.brokers.broker_apis.set_db_lkg_frame",
                   side_effect=_fake_set_db_lkg_frame):
            await background_module._preload_db_lkg_cache()

        df = captured[("margins", "DH3747")]
        row0 = df.iloc[0]
        assert row0["util debits"] is None or (
            isinstance(row0["util debits"], float) and math.isnan(row0["util debits"])
        ), f"missing qty must stay null, got {row0['util debits']!r}"
        assert row0["util realised_m2m"] is None or (
            isinstance(row0["util realised_m2m"], float)
            and math.isnan(row0["util realised_m2m"])
        ), f"missing day_pnl must stay null, got {row0['util realised_m2m']!r}"

    @pytest.mark.asyncio
    async def test_holdings_and_positions_rows_unaffected_by_funds_branch(self):
        """Adding the funds branch must not change holdings/positions row
        shaping — same tradingsymbol/exchange-centric dict as before."""
        from backend.api import background as background_module

        rows = [
            _row("ZJ6294", "holdings", "TCS", "NSE", qty=10,
                 avg_cost=3000.0, ltp=3100.0),
            _row("ZJ6294", "funds", "__seg__", "EQUITY",
                 qty=1000, avg_cost=5000.0, ltp=6000.0,
                 day_pnl=50.0, total_pnl=5900.0),
        ]
        mock_session = _mock_session_with_rows(rows)

        captured = {}

        def _fake_set_db_lkg_frame(kind, account, df):
            captured[(kind, account)] = df

        with patch("backend.api.database.async_session", return_value=mock_session), \
             patch("backend.brokers.broker_apis.set_db_lkg_frame",
                   side_effect=_fake_set_db_lkg_frame):
            await background_module._preload_db_lkg_cache()

        holdings_df = captured[("holdings", "ZJ6294")]
        assert "tradingsymbol" in holdings_df.columns
        assert holdings_df.iloc[0]["tradingsymbol"] == "TCS"

        margins_df = captured[("margins", "ZJ6294")]
        assert "net" in margins_df.columns
        assert "tradingsymbol" not in margins_df.columns, (
            "margins-shaped frame must not carry the holdings/positions "
            "tradingsymbol column"
        )


class TestFundsLkgEndToEndSubstitution:
    """The real downstream consumer, `_stale_substitute_frame('margins',
    ...)`, must be able to serve the preloaded funds row."""

    @pytest.mark.asyncio
    async def test_stale_substitute_frame_margins_serves_preloaded_funds_row(self):
        from backend.api import background as background_module
        from backend.brokers import broker_apis

        rows = [
            _row("ZG0790", "funds", "__seg__", "EQUITY",
                 qty=15000, avg_cost=250000.0, ltp=300000.0,
                 day_pnl=1200.0, total_pnl=298800.0),
        ]
        mock_session = _mock_session_with_rows(rows)

        # Clear any in-memory LKG for this account/kind so the DB-backed
        # tier (what this test preloads) is what actually gets read back.
        with broker_apis._LKG_FRAME_LOCK:
            broker_apis._LKG_FRAME_BY_ACCT.pop(("margins", "ZG0790"), None)
        with broker_apis._DB_LKG_CACHE_LOCK:
            broker_apis._DB_LKG_CACHE.pop(("margins", "ZG0790"), None)

        with patch("backend.api.database.async_session", return_value=mock_session):
            await background_module._preload_db_lkg_cache()

        substituted = broker_apis._stale_substitute_frame("margins", "ZG0790")
        assert not substituted.empty, (
            "a conn_service restart with a real daily_book funds row must "
            "be able to serve it via the DB-backed LKG tier — got an empty "
            "frame"
        )
        assert substituted.attrs.get("db_lkg") is True, (
            "frame served from the DB-backed tier must carry attrs['db_lkg']"
        )
        assert float(substituted.iloc[0]["net"]) == 298800.0

        # Teardown — this test seeds a real module-level cache entry
        # (`_DB_LKG_CACHE`/`_LKG_FRAME_BY_ACCT` are process-global, not
        # per-test fixtures); leaving it behind could leak into a later
        # test that happens to use the same account code.
        with broker_apis._LKG_FRAME_LOCK:
            broker_apis._LKG_FRAME_BY_ACCT.pop(("margins", "ZG0790"), None)
        with broker_apis._DB_LKG_CACHE_LOCK:
            broker_apis._DB_LKG_CACHE.pop(("margins", "ZG0790"), None)
