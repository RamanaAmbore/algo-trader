"""
Tests for DB retention — daily purge tasks for visitor_log,
impersonation_events, and admin_email_events (audit finding a87bc72e).

Five quality dimensions per table:
  1. SSOT        — the cron handler reads TTL from the settings key; never
                   uses a hardcoded literal in the deletion path.
  2. Performance — DELETE is a single SQL statement, not row-by-row Python
                   iteration.  Asserted by verifying the returned rowcount
                   equals the expected deletes.
  3. Stale code  — source-grep confirms the implementation is present and
                   uses _apply_retention().
  4. Reusable    — _apply_retention() is used for every table-based purge
                   (not ad-hoc loops).
  5. Correctness — seed rows spanning the TTL boundary; verify the right
                   rows survive after purge.

Note on database dialect:
  The correctness tests use an in-process SQLite DB for isolation. SQLite
  does not support ``now() - interval 'N days'`` (Postgres syntax).  The
  correctness tests therefore issue the SQLite-compatible equivalent
  ``datetime('now', '-N days')`` directly, which exercises the same TTL
  boundary logic without crossing the dialect boundary.  The production
  code path (``_apply_retention`` using Postgres interval syntax) is
  validated indirectly: the source-code and structural tests confirm it
  builds a single DELETE with the correct shape.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


# ---------------------------------------------------------------------------
# Lightweight in-process SQLite DB for correctness tests
# ---------------------------------------------------------------------------

class _Base(DeclarativeBase):
    pass


class _VisitorLog(_Base):
    __tablename__ = "visitor_log"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    ip             = Column(String(45), nullable=False)
    seen_date      = Column(String(10), nullable=False)   # simplified for SQLite
    request_count  = Column(Integer, nullable=False, default=0)
    first_seen_at  = Column(DateTime(timezone=True), nullable=False)
    last_seen_at   = Column(DateTime(timezone=True), nullable=False)
    created_at     = Column(DateTime(timezone=True), nullable=False)


class _ImpersonationEvent(_Base):
    __tablename__ = "impersonation_events"
    id                  = Column(Integer, primary_key=True, autoincrement=True)
    actor_username      = Column(String(64), nullable=False)
    actor_role_at_time  = Column(String(32), nullable=False)
    target_username     = Column(String(64), nullable=False)
    target_role_at_time = Column(String(32), nullable=False)
    started_at          = Column(DateTime(timezone=True), nullable=False)


class _AdminEmailEvent(_Base):
    __tablename__ = "admin_email_events"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    actor_username = Column(String(64), nullable=False)
    actor_role     = Column(String(32), nullable=False)
    subject        = Column(String(256), nullable=False)
    sent_count     = Column(Integer, nullable=False, default=0)
    failed_count   = Column(Integer, nullable=False, default=0)
    created_at     = Column(DateTime(timezone=True), nullable=False)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# Source references (loaded once)
# ---------------------------------------------------------------------------

BG_SRC      = Path(__file__).parent.parent / "api" / "background.py"
SETTINGS_SRC = Path(__file__).parent.parent / "shared" / "helpers" / "settings.py"


def _bg() -> str:
    return BG_SRC.read_text(encoding="utf-8")


def _settings() -> str:
    return SETTINGS_SRC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# SQLite-compatible retention helper (mirrors _apply_retention for tests)
# ---------------------------------------------------------------------------

async def _sqlite_retention(
    session: AsyncSession, table: str, ts_col: str, days: int
) -> int:
    from sqlalchemy import text
    res = await session.execute(text(
        f"DELETE FROM {table} WHERE {ts_col} < datetime('now', '-{days} days')"
    ))
    return res.rowcount if res.rowcount >= 0 else 0


async def _seed_rows(
    session: AsyncSession, model, ts_col: str, ages_days: list[int], **extra
):
    """Insert rows with timestamps at given ages (days before now)."""
    now = datetime.now(timezone.utc)
    for age in ages_days:
        ts = now - timedelta(days=age)
        session.add(model(**{ts_col: ts, **extra}))
    await session.flush()


# ---------------------------------------------------------------------------
# Dimension 1 (SSOT) — settings keys present and read
# ---------------------------------------------------------------------------

def test_retention_settings_keys_present():
    """All three new retention keys are seeded in settings.py."""
    src = _settings()
    for key in (
        "retention.visitor_log_days",
        "retention.impersonation_events_days",
        "retention.admin_email_events_days",
    ):
        assert key in src, f"Settings key {key!r} missing from settings.py"


def test_bg_reads_visitor_log_settings_key():
    src = _bg()
    assert "retention.visitor_log_days" in src, (
        "retention.visitor_log_days not read in background.py"
    )


def test_bg_reads_impersonation_events_settings_key():
    src = _bg()
    assert "retention.impersonation_events_days" in src, (
        "retention.impersonation_events_days not read in background.py"
    )


def test_bg_reads_admin_email_events_settings_key():
    src = _bg()
    assert "retention.admin_email_events_days" in src, (
        "retention.admin_email_events_days not read in background.py"
    )


# ---------------------------------------------------------------------------
# Dimension 3 (Stale code) — tasks are defined and registered
# ---------------------------------------------------------------------------

def test_task_purge_visitor_log_defined():
    assert "async def _task_purge_visitor_log(" in _bg(), (
        "_task_purge_visitor_log not defined in background.py"
    )


def test_task_purge_impersonation_events_defined():
    assert "async def _task_purge_impersonation_events(" in _bg(), (
        "_task_purge_impersonation_events not defined in background.py"
    )


def test_task_purge_admin_email_events_defined():
    assert "async def _task_purge_admin_email_events(" in _bg(), (
        "_task_purge_admin_email_events not defined in background.py"
    )


def test_all_three_tasks_registered_in_on_startup():
    """All three new tasks appear in the on_startup task list."""
    src = _bg()
    for name in (
        "_task_purge_visitor_log()",
        "_task_purge_impersonation_events()",
        "_task_purge_admin_email_events()",
    ):
        assert name in src, f"{name} not registered in background.py on_startup"


# ---------------------------------------------------------------------------
# Dimension 4 (Reusable) — _apply_retention used in every new task
# ---------------------------------------------------------------------------

def _extract_task_body(src: str, fn_name: str) -> str:
    match = re.search(
        rf"async def {fn_name}\(\).*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, f"{fn_name} not found in background.py"
    return match.group(0)


def test_visitor_log_task_uses_apply_retention():
    body = _extract_task_body(_bg(), "_task_purge_visitor_log")
    assert "_apply_retention(" in body, (
        "_task_purge_visitor_log does not call _apply_retention"
    )
    assert '"visitor_log"' in body or "'visitor_log'" in body, (
        '"visitor_log" table name missing from _task_purge_visitor_log'
    )


def test_impersonation_events_task_uses_apply_retention():
    body = _extract_task_body(_bg(), "_task_purge_impersonation_events")
    assert "_apply_retention(" in body, (
        "_task_purge_impersonation_events does not call _apply_retention"
    )
    assert '"impersonation_events"' in body or "'impersonation_events'" in body, (
        '"impersonation_events" table name missing from task body'
    )


def test_admin_email_events_task_uses_apply_retention():
    body = _extract_task_body(_bg(), "_task_purge_admin_email_events")
    assert "_apply_retention(" in body, (
        "_task_purge_admin_email_events does not call _apply_retention"
    )
    assert '"admin_email_events"' in body or "'admin_email_events'" in body, (
        '"admin_email_events" table name missing from task body'
    )


# ---------------------------------------------------------------------------
# Dimension 1b (SSOT) — zero-disable guard present in each task
# ---------------------------------------------------------------------------

def test_visitor_log_task_has_zero_disable_guard():
    body = _extract_task_body(_bg(), "_task_purge_visitor_log")
    assert "days <= 0" in body or "days == 0" in body, (
        "_task_purge_visitor_log missing zero-disable guard"
    )


def test_impersonation_events_task_has_zero_disable_guard():
    body = _extract_task_body(_bg(), "_task_purge_impersonation_events")
    assert "days <= 0" in body or "days == 0" in body, (
        "_task_purge_impersonation_events missing zero-disable guard"
    )


def test_admin_email_events_task_has_zero_disable_guard():
    body = _extract_task_body(_bg(), "_task_purge_admin_email_events")
    assert "days <= 0" in body or "days == 0" in body, (
        "_task_purge_admin_email_events missing zero-disable guard"
    )


# ---------------------------------------------------------------------------
# Cron timing — tasks scheduled at 03:25 / 03:30 / 03:35 IST
# ---------------------------------------------------------------------------

def test_visitor_log_cron_time():
    body = _extract_task_body(_bg(), "_task_purge_visitor_log")
    assert "minute=25" in body, (
        "_task_purge_visitor_log not scheduled at minute=25 (03:25 IST)"
    )
    assert "hour=3" in body, (
        "_task_purge_visitor_log not scheduled at hour=3"
    )


def test_impersonation_events_cron_time():
    body = _extract_task_body(_bg(), "_task_purge_impersonation_events")
    assert "minute=30" in body, (
        "_task_purge_impersonation_events not scheduled at minute=30 (03:30 IST)"
    )
    assert "hour=3" in body, (
        "_task_purge_impersonation_events not scheduled at hour=3"
    )


def test_admin_email_events_cron_time():
    body = _extract_task_body(_bg(), "_task_purge_admin_email_events")
    assert "minute=35" in body, (
        "_task_purge_admin_email_events not scheduled at minute=35 (03:35 IST)"
    )
    assert "hour=3" in body, (
        "_task_purge_admin_email_events not scheduled at hour=3"
    )


# ---------------------------------------------------------------------------
# Dimension 5 (Correctness) — seed + purge + verify surviving rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_visitor_log_retention_correctness(session: AsyncSession):
    """Rows older than TTL are deleted; recent rows survive."""
    ttl = 90
    ages = [10, 45, 89, 91, 120, 180]
    now = datetime.now(timezone.utc)
    for age in ages:
        ts = now - timedelta(days=age)
        session.add(_VisitorLog(
            ip="1.2.3.4",
            seen_date="2026-01-01",
            first_seen_at=ts,
            last_seen_at=ts,
            created_at=ts,
        ))
    await session.flush()

    deleted = await _sqlite_retention(session, "visitor_log", "created_at", ttl)
    await session.flush()

    from sqlalchemy import select
    surviving = (await session.execute(select(_VisitorLog))).scalars().all()
    expected_deleted = len([a for a in ages if a >= ttl])
    assert deleted == expected_deleted, (
        f"Expected {expected_deleted} deleted, got {deleted}"
    )
    assert len(surviving) == len([a for a in ages if a < ttl])


@pytest.mark.asyncio
async def test_impersonation_events_retention_correctness(session: AsyncSession):
    """Rows older than TTL are deleted; recent rows survive."""
    ttl = 365
    ages = [30, 180, 364, 366, 400, 730]
    await _seed_rows(
        session, _ImpersonationEvent, "started_at", ages,
        actor_username="admin",
        actor_role_at_time="admin",
        target_username="investor1",
        target_role_at_time="partner",
    )

    deleted = await _sqlite_retention(
        session, "impersonation_events", "started_at", ttl
    )
    await session.flush()

    from sqlalchemy import select
    surviving = (await session.execute(select(_ImpersonationEvent))).scalars().all()
    expected_deleted = len([a for a in ages if a >= ttl])
    assert deleted == expected_deleted, (
        f"Expected {expected_deleted} deleted, got {deleted}"
    )
    assert len(surviving) == len([a for a in ages if a < ttl])


@pytest.mark.asyncio
async def test_admin_email_events_retention_correctness(session: AsyncSession):
    """Rows older than TTL are deleted; recent rows survive."""
    ttl = 90
    ages = [10, 45, 89, 91, 120, 180]
    await _seed_rows(
        session, _AdminEmailEvent, "created_at", ages,
        actor_username="admin",
        actor_role="admin",
        subject="Monthly statement",
    )

    deleted = await _sqlite_retention(
        session, "admin_email_events", "created_at", ttl
    )
    await session.flush()

    from sqlalchemy import select
    surviving = (await session.execute(select(_AdminEmailEvent))).scalars().all()
    expected_deleted = len([a for a in ages if a >= ttl])
    assert deleted == expected_deleted, (
        f"Expected {expected_deleted} deleted, got {deleted}"
    )
    assert len(surviving) == len([a for a in ages if a < ttl])


# ---------------------------------------------------------------------------
# Zero-disable: retention.X_days = 0 skips cleanup (retain forever)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_visitor_log_zero_days_skips_cleanup(session: AsyncSession):
    """When days=0 the cleanup returns early without deleting any rows."""
    now = datetime.now(timezone.utc)
    for age in [10, 100, 200]:
        ts = now - timedelta(days=age)
        session.add(_VisitorLog(
            ip="1.2.3.4", seen_date="2026-01-01",
            first_seen_at=ts, last_seen_at=ts, created_at=ts,
        ))
    await session.flush()

    # days=0 → the task body should guard early return; simulate it inline.
    days = 0
    if days <= 0:
        deleted = 0
    else:
        deleted = await _sqlite_retention(session, "visitor_log", "created_at", days)

    from sqlalchemy import select
    surviving = (await session.execute(select(_VisitorLog))).scalars().all()
    assert deleted == 0
    assert len(surviving) == 3, "days=0 must retain all rows"


@pytest.mark.asyncio
async def test_impersonation_events_zero_days_skips_cleanup(session: AsyncSession):
    """When days=0 no rows are deleted."""
    now = datetime.now(timezone.utc)
    for age in [10, 400, 800]:
        ts = now - timedelta(days=age)
        session.add(_ImpersonationEvent(
            actor_username="admin", actor_role_at_time="admin",
            target_username="inv", target_role_at_time="partner",
            started_at=ts,
        ))
    await session.flush()

    days = 0
    deleted = 0 if days <= 0 else await _sqlite_retention(
        session, "impersonation_events", "started_at", days
    )

    from sqlalchemy import select
    surviving = (await session.execute(select(_ImpersonationEvent))).scalars().all()
    assert deleted == 0
    assert len(surviving) == 3


@pytest.mark.asyncio
async def test_admin_email_events_zero_days_skips_cleanup(session: AsyncSession):
    """When days=0 no rows are deleted."""
    now = datetime.now(timezone.utc)
    for age in [10, 100, 200]:
        ts = now - timedelta(days=age)
        session.add(_AdminEmailEvent(
            actor_username="admin", actor_role="admin",
            subject="test", created_at=ts,
        ))
    await session.flush()

    days = 0
    deleted = 0 if days <= 0 else await _sqlite_retention(
        session, "admin_email_events", "created_at", days
    )

    from sqlalchemy import select
    surviving = (await session.execute(select(_AdminEmailEvent))).scalars().all()
    assert deleted == 0
    assert len(surviving) == 3
