"""Regression tests for the `_positions_snapshot` `latest_batch.cutoff_ts`
session-anchor fix (2026-09 Day P&L audit round 3, item #1).

Bug: the round-2 fix computed `cutoff_ts` from the batch's own `date`
COLUMN (`date + 08:00 IST`). The snapshot writer
(`daily_snapshot.py:snapshot_daily_book`) stamps `date` from the wall-clock
calendar day at write time, not the trading session captured. A
close_settled write that fires just after midnight IST (e.g. MCX close
23:30 + a settled-offset that crosses midnight) gets `date` = the NEXT
calendar day — so the date-based `cutoff_ts` (next day's 08:00) sits a
full day later than it should, and an EARLIER SAME-SESSION row (e.g. that
day's own 16:00 IST NSE close_settled write) slips through the
`captured_at < cutoff_ts` baseline test and gets picked as "baseline" —
base_pnl ≈ current total_pnl, collapsing Day P&L to ~0. Combined with the
qty!=0 baseline filter (audit item #5), a flat row falling out of the
anchored batch can push base_pnl all the way to 0, inflating Day P&L by
the full lifetime total.

Fix: derive `cutoff_ts` purely from `captured_at` — the 08:00 IST start
of the `[08:00, next 08:00)` trading-day window `captured_at` itself
falls into — never from the `date` column.

Covers all four real patterns confirmed in prod:
  1. Normal 15:46 non-MCX close-reset write (same-day, no midnight cross)
  2. Normal 23:46 MCX close-reset write (same-day, no midnight cross)
  3. Same-day-date post-midnight write (rare)
  4. Next-day-date post-midnight write (the actual bug trigger)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")


def _session_anchor_cutoff_ts(captured_at_ist: datetime) -> datetime:
    """Pure-Python mirror of the SQL fix in `_positions_snapshot`'s
    `latest_batch` CTE:

        (date_trunc('day', (captured_at AT TIME ZONE 'Asia/Kolkata')
          - INTERVAL '8 hours') + INTERVAL '8 hours') AT TIME ZONE 'Asia/Kolkata'

    Returns the 08:00 IST boundary of the trading-day window that
    `captured_at_ist` falls into.
    """
    shifted = captured_at_ist - timedelta(hours=8)
    day_start = shifted.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start + timedelta(hours=8)


def _date_column_cutoff_ts(captured_at_ist: datetime, date_column_day: datetime) -> datetime:
    """Pure-Python mirror of the OLD (buggy, round-2) formula:
    `date + 08:00 IST`, using the batch's own `date` COLUMN rather than
    `captured_at`."""
    return date_column_day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=8)


# ---------------------------------------------------------------------------
# Pure formula — four real prod patterns
# ---------------------------------------------------------------------------

class TestSessionAnchorFormula:
    def test_normal_non_mcx_close_write_same_day(self):
        """Pattern 1: normal 15:46 IST non-MCX close-reset write, no
        midnight crossing — cutoff_ts anchors to that same day's 08:00."""
        captured_at = datetime(2026, 9, 21, 15, 46, 0, tzinfo=IST)  # Monday
        cutoff = _session_anchor_cutoff_ts(captured_at)
        assert cutoff == datetime(2026, 9, 21, 8, 0, 0, tzinfo=IST)

    def test_normal_mcx_close_write_same_day(self):
        """Pattern 2: normal 23:46 IST MCX close-reset write, no midnight
        crossing — cutoff_ts anchors to that same day's 08:00."""
        captured_at = datetime(2026, 9, 21, 23, 46, 0, tzinfo=IST)  # Monday
        cutoff = _session_anchor_cutoff_ts(captured_at)
        assert cutoff == datetime(2026, 9, 21, 8, 0, 0, tzinfo=IST)

    def test_same_day_date_post_midnight_write(self):
        """Pattern 3: rare same-day-date post-midnight write (captured_at
        just after midnight, e.g. 00:05 IST) — cutoff_ts still anchors
        purely off captured_at, landing on the PRIOR calendar day's 08:00
        (the window [prior_day 08:00, this_day 08:00) that 00:05 falls
        into), regardless of what the `date` column says."""
        captured_at = datetime(2026, 9, 21, 0, 5, 0, tzinfo=IST)  # Monday 00:05
        cutoff = _session_anchor_cutoff_ts(captured_at)
        assert cutoff == datetime(2026, 9, 20, 8, 0, 0, tzinfo=IST)  # Sunday 08:00

    def test_next_day_date_post_midnight_write_the_bug_trigger(self):
        """Pattern 4 (the actual bug trigger): MCX close_settled fires at
        Saturday 00:00:19 IST (Friday 23:30 close + 30 min settled-offset
        crosses midnight), `date` column stamped Saturday. The
        captured_at-derived cutoff_ts must resolve to FRIDAY's 08:00 —
        the start of the session this write's data actually belongs to —
        not Saturday's 08:00 (what the old date-column formula produced)."""
        captured_at = datetime(2026, 9, 19, 0, 0, 19, tzinfo=IST)  # Saturday 00:00:19
        cutoff = _session_anchor_cutoff_ts(captured_at)
        assert cutoff == datetime(2026, 9, 18, 8, 0, 0, tzinfo=IST)  # Friday 08:00

        # Demonstrate the OLD formula's divergence: date column = Saturday.
        old_cutoff = _date_column_cutoff_ts(
            captured_at, date_column_day=datetime(2026, 9, 19, 0, 0, 0, tzinfo=IST)
        )
        assert old_cutoff == datetime(2026, 9, 19, 8, 0, 0, tzinfo=IST)  # Saturday 08:00
        assert old_cutoff != cutoff, (
            "the old date-column cutoff_ts and the new captured_at-derived "
            "cutoff_ts must diverge for a next-day-date post-midnight write "
            "— that divergence is exactly the bug"
        )


# ---------------------------------------------------------------------------
# Behavioral fixture — full prod scenario reproduction, against REAL Postgres
# ---------------------------------------------------------------------------
#
# A pure-Python mirror of `_BASELINE_PNL_CTE_SQL`'s `pnl_ranked` resolution
# CANNOT faithfully reproduce this bug: the real SQL is a TWO-STAGE
# resolution — `latest_pnl_batch` first picks a SINGLE MAX(captured_at)
# batch timestamp per (account, kind) satisfying the cutoff bound, THEN
# `pnl_ranked` filters qty!=0 rows WITHIN that one batch only. If the
# wrongly-selected batch's row for a symbol happens to be flat, there is
# NO fallback to an earlier valid batch — base_pnl defaults to 0 outright.
# That two-stage "commit to one batch, then filter" structure is exactly
# the mechanism behind the audit's reproduced +307,200 inflation. A naive
# "search all history, filter qty!=0, take max" Python mirror would
# incorrectly still find an earlier valid batch and NOT reproduce the bug
# — so these tests execute the REAL SQL fragments (imported verbatim from
# positions.py, not hand-copied) against a real local Postgres instance.

import asyncio as _asyncio
import getpass as _getpass


def _local_postgres_reachable() -> bool:
    try:
        import asyncpg
    except ImportError:
        return False

    async def _try():
        try:
            conn = await asyncpg.connect(
                host="/tmp", port=5432, user=_getpass.getuser(),
                database="postgres", timeout=2,
            )
            await conn.close()
            return True
        except Exception:
            return False

    try:
        return _asyncio.run(_try())
    except Exception:
        return False


requires_local_postgres = pytest.mark.skipif(
    not _local_postgres_reachable(),
    reason="local Postgres unix-socket instance not reachable at /tmp:5432 "
           "— real-SQL baseline-resolution tests require it",
)


async def _run_baseline_pnl_final(conn, cutoff_ts_sql: str, rows: list[tuple]) -> list:
    """Execute the REAL `_BASELINE_PNL_CTE_SQL` fragment (imported from
    positions.py) wrapped in a minimal `latest_batch` CTE parameterised by
    `cutoff_ts_sql`, against a temp `daily_book`-shaped table seeded with
    `rows`. Returns the `pnl_final` rows (account, symbol, total_pnl,
    kind, qty)."""
    from backend.api.routes.positions import _BASELINE_PNL_CTE_SQL

    await conn.execute("""
        CREATE TEMP TABLE IF NOT EXISTS daily_book (
            account text, symbol text, kind text, qty numeric,
            ltp numeric, total_pnl numeric, date date, captured_at timestamptz
        )
    """)
    await conn.execute("TRUNCATE daily_book")
    await conn.executemany(
        "INSERT INTO daily_book (account, symbol, kind, qty, ltp, total_pnl, date, captured_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
        rows,
    )
    query = f"""
        WITH latest_batch AS (
            SELECT DISTINCT ON (account) account, captured_at AS max_at,
                   {cutoff_ts_sql} AS cutoff_ts
            FROM daily_book
            WHERE kind = 'positions' AND ltp IS NOT NULL AND ltp > 0
            ORDER BY account, captured_at DESC
        ),
        {_BASELINE_PNL_CTE_SQL}
        SELECT account, symbol, total_pnl, kind, qty FROM pnl_final
    """
    return await conn.fetch(query)


_OLD_DATE_COLUMN_CUTOFF_TS_SQL = (
    "(date::timestamp + INTERVAL '8 hours') AT TIME ZONE 'Asia/Kolkata'"
)


@requires_local_postgres
class TestBaselineResolutionAgainstRealPostgres:
    """Reproduces the exact prod scenario against a real Postgres engine:
    Thursday's real close-reset write, Friday's own SAME-session
    close-reset write (23:46 Friday — the write `<exch>:close` fires
    before `close_settled`), and Friday's `close_settled` write landing
    on Saturday 00:00:19 IST with `date` stamped Saturday (the actual bug
    trigger — a settled-offset that crosses midnight). Asserts the
    baseline resolves to the STRICTLY PRIOR trading session (Thursday),
    not the same session re-picked (Friday)."""

    @pytest.fixture
    async def conn(self):
        import asyncpg
        c = await asyncpg.connect(
            host="/tmp", port=5432, user=_getpass.getuser(), database="postgres",
        )
        try:
            yield c
        finally:
            await c.close()

    def _fixture_rows(self):
        from datetime import date as _date
        return [
            # Thursday's real close-reset snapshot — the correct baseline.
            ("ACC1", "TCS", "positions", 5, 100.0, 1000.0,
             _date(2026, 9, 17), datetime(2026, 9, 17, 23, 46, 0, tzinfo=IST)),
            # Friday's OWN same-session close-reset write (the `<exch>:close`
            # handler firing before `close_settled`) — must NOT be picked
            # as "baseline" for the Saturday-stamped display batch below.
            ("ACC1", "TCS", "positions", 5, 105.0, 1500.0,
             _date(2026, 9, 18), datetime(2026, 9, 18, 23, 46, 0, tzinfo=IST)),
            # Friday's close_settled write, crossing midnight — `date`
            # column = Saturday, captured_at = Saturday 00:00:19. This is
            # the "current" display batch.
            ("ACC1", "TCS", "positions", 5, 110.0, 1800.0,
             _date(2026, 9, 19), datetime(2026, 9, 19, 0, 0, 19, tzinfo=IST)),
        ]

    async def test_new_anchor_resolves_baseline_to_strictly_prior_session(self, conn):
        from backend.api.routes.positions import _SESSION_ANCHOR_CUTOFF_TS_SQL

        result = await _run_baseline_pnl_final(conn, _SESSION_ANCHOR_CUTOFF_TS_SQL, self._fixture_rows())

        assert len(result) == 1
        assert float(result[0]["total_pnl"]) == 1000.0, (
            "baseline must resolve to Thursday's close-reset row (1000), "
            "not Friday's own earlier same-session write (1500) — got "
            f"{result}"
        )
        day_pnl = 1800.0 - float(result[0]["total_pnl"])
        assert day_pnl == 800.0, (
            "Day P&L must reflect Friday's genuine session change "
            "(1800 - 1000 = 800), not the fake ~0 / ~300 the old "
            "date-column formula produced"
        )

    async def test_old_date_column_anchor_reproduces_the_bug(self, conn):
        """Sanity check: the OLD (date-column) anchor, applied to the same
        fixture via real SQL, self-collides with Friday's own earlier
        write and produces a wrong (near-zero-delta) Day P&L — proving the
        new anchor is a genuine behavioral fix, not just a cosmetic one."""
        result = await _run_baseline_pnl_final(conn, _OLD_DATE_COLUMN_CUTOFF_TS_SQL, self._fixture_rows())

        assert len(result) == 1
        assert float(result[0]["total_pnl"]) == 1500.0, (
            "the OLD date-column anchor incorrectly picks Friday's own "
            "earlier same-session write as baseline (self-collision) — "
            f"got {result}"
        )
        wrong_day_pnl = 1800.0 - float(result[0]["total_pnl"])
        assert wrong_day_pnl == 300.0, "reproduces the fake near-zero Day P&L bug"
        assert wrong_day_pnl != 800.0

    async def test_flat_same_session_row_inflates_under_old_anchor_fixed_under_new(self, conn):
        """Audit item #5 cascading symptom, reproduced via the REAL
        two-stage SQL resolution: under the OLD anchor, the wrongly-
        selected Friday batch is flat (qty=0) for this symbol → excluded
        by `pnl_ranked`'s qty!=0 filter → `latest_pnl_batch` already
        committed to that single batch timestamp, so there is NO fallback
        to Thursday's still-valid row → base_pnl has NO entry at all
        (defaults to 0 downstream) → the full lifetime total_pnl (309200)
        would show as "today's" Day P&L. Under the NEW anchor, Friday's
        row is excluded from batch SELECTION entirely (it's outside the
        [Fri 08:00, Sat 08:00) window the fixed anchor targets), so
        Thursday's valid row is found correctly."""
        from datetime import date as _date
        from backend.api.routes.positions import _SESSION_ANCHOR_CUTOFF_TS_SQL

        rows = [
            ("ACC1", "TCS", "positions", 5, 100.0, 1000.0,
             _date(2026, 9, 17), datetime(2026, 9, 17, 23, 46, 0, tzinfo=IST)),  # Thu, valid
            # Friday's own same-session write — FLAT for this symbol
            # (position fully closed intraday before Friday's close).
            ("ACC1", "TCS", "positions", 0, 105.0, 0.0,
             _date(2026, 9, 18), datetime(2026, 9, 18, 23, 46, 0, tzinfo=IST)),
            ("ACC1", "TCS", "positions", 5, 110.0, 309200.0,
             _date(2026, 9, 19), datetime(2026, 9, 19, 0, 0, 19, tzinfo=IST)),  # current
        ]

        old_result = await _run_baseline_pnl_final(conn, _OLD_DATE_COLUMN_CUTOFF_TS_SQL, rows)
        assert len(old_result) == 0, (
            "OLD anchor: the flat Friday batch is the ONLY candidate "
            "(latest_pnl_batch already committed to it) and gets excluded "
            "by qty!=0 — base_pnl has no entry (defaults to 0 downstream), "
            "reproducing the +307,200 inflation mechanism — got "
            f"{old_result}"
        )

        new_result = await _run_baseline_pnl_final(conn, _SESSION_ANCHOR_CUTOFF_TS_SQL, rows)
        assert len(new_result) == 1
        assert float(new_result[0]["total_pnl"]) == 1000.0, (
            "NEW anchor must still find Thursday's valid batch even when "
            f"the same-session Friday row is flat — got {new_result}"
        )
        day_pnl = 309200.0 - float(new_result[0]["total_pnl"])
        assert day_pnl == 308200.0


# ---------------------------------------------------------------------------
# SQL source guards
# ---------------------------------------------------------------------------

class TestSqlSourceUsesCapturedAtNotDateColumn:
    def test_latest_batch_cutoff_ts_derived_from_captured_at(self):
        import inspect
        from backend.api.routes import positions as positions_mod

        src = inspect.getsource(positions_mod._positions_snapshot)
        assert "_SESSION_ANCHOR_CUTOFF_TS_SQL" in src, (
            "latest_batch.cutoff_ts must be built from the shared "
            "_SESSION_ANCHOR_CUTOFF_TS_SQL constant (captured_at-derived), "
            "not an inline copy that can silently drift from it"
        )
        assert "(captured_at AT TIME ZONE 'Asia/Kolkata') - INTERVAL '8 hours'" in (
            positions_mod._SESSION_ANCHOR_CUTOFF_TS_SQL
        ), (
            "_SESSION_ANCHOR_CUTOFF_TS_SQL must derive cutoff_ts from "
            "captured_at, not the `date` column"
        )
        assert "date::timestamp + INTERVAL '8 hours'" not in src, (
            "the old date-column-based cutoff_ts formula must be fully "
            "removed — it self-collides with earlier same-session rows "
            "on any post-midnight close_settled write"
        )

    def test_fetch_snapshot_close_map_baseline_half_unaffected_by_date_column(self):
        """`_fetch_snapshot_close_map` / `_fetch_baseline_pnl_map` already
        anchor `cutoff_ts` to a fixed `:today_08` / `:baseline_cutoff`
        bind param (never the `date` column), so they were never exposed
        to this specific self-collision — confirm that contract still
        holds after the `_positions_snapshot` fix."""
        import inspect
        from backend.api.routes import positions as positions_mod

        snap_src = inspect.getsource(positions_mod._fetch_snapshot_close_map)
        baseline_src = inspect.getsource(positions_mod._fetch_baseline_pnl_map)
        for src in (snap_src, baseline_src):
            assert "date::timestamp" not in src
            assert "date_column" not in src
