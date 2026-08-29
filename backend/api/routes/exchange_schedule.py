"""
`/api/admin/exchange-schedule/*` — CRUD for the exchange_schedule table.

The exchange_schedule table is the single source of truth for all segment
timing.  Default rows (date IS NULL) define the permanent weekday schedule;
date-specific rows override for special sessions or early-close days.

Operator can:
  GET  /api/admin/exchange-schedule          — list all rows
  GET  /api/admin/exchange-schedule/{id}     — get one row
  PUT  /api/admin/exchange-schedule/{id}     — full-replace a row
  POST /api/admin/exchange-schedule          — create a new row
  DELETE /api/admin/exchange-schedule/{id}   — delete a row

After any mutation the exchange_clock module cache is invalidated (forced
refresh on next read) so timing changes propagate within one poll cycle.
"""

from __future__ import annotations

from datetime import date, time
from typing import Optional

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.database import async_session
from backend.api.helpers import exchange_clock
from backend.api.models import ExchangeSchedule
from backend.api.rbac import cap_guard
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Schemas (msgspec.Struct — project convention, ~10x faster than pydantic)
# ---------------------------------------------------------------------------

class ExchangeScheduleDTO(msgspec.Struct):
    """Wire representation of one exchange_schedule row."""
    id: int
    gate: str
    exchanges: list[str]
    date: Optional[date]
    weekdays: Optional[list[int]]
    session_name: str
    is_open: bool
    open_time: Optional[time]
    close_time: Optional[time]
    snapshot_time: Optional[time]
    snapshot_reset_time: Optional[time]
    reason: Optional[str]
    source: str
    deletable: bool  # True only for future-dated overrides (date >= today)
    editable: bool   # True for default rows and future-dated overrides


class ExchangeScheduleWrite(msgspec.Struct):
    """Payload for PUT (full-replace) and POST (create)."""
    gate: str
    exchanges: list[str]
    date: Optional[date] = None
    weekdays: Optional[list[int]] = None
    session_name: str = "regular"
    is_open: bool = True
    open_time: Optional[time] = None
    close_time: Optional[time] = None
    snapshot_time: Optional[time] = None
    snapshot_reset_time: Optional[time] = None
    reason: Optional[str] = None
    source: str = "operator"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_dto(row: ExchangeSchedule) -> ExchangeScheduleDTO:
    today = date.today()
    deletable = row.date is not None and row.date >= today
    editable = row.date is None or row.date >= today
    return ExchangeScheduleDTO(
        id=row.id,
        gate=row.gate,
        exchanges=list(row.exchanges or []),
        date=row.date,
        weekdays=list(row.weekdays) if row.weekdays else None,
        session_name=row.session_name,
        is_open=row.is_open,
        open_time=row.open_time,
        close_time=row.close_time,
        snapshot_time=row.snapshot_time,
        snapshot_reset_time=row.snapshot_reset_time,
        reason=row.reason,
        source=row.source,
        deletable=deletable,
        editable=editable,
    )


async def _invalidate_clock_cache() -> None:
    """Force exchange_clock to reload from DB on next access."""
    import time as _time
    exchange_clock._cache_loaded_at = 0.0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class ExchangeScheduleController(Controller):
    path = "/api/admin/exchange-schedule"

    @get(
        "/",
        guards=[cap_guard("view_exchange_schedule")],
        summary="List all exchange schedule rows",
    )
    async def list_schedules(self) -> list[ExchangeScheduleDTO]:
        async with async_session() as session:
            result = await session.execute(
                select(ExchangeSchedule).order_by(
                    ExchangeSchedule.gate,
                    ExchangeSchedule.session_name,
                    ExchangeSchedule.date.asc().nullsfirst(),
                )
            )
            rows = result.scalars().all()
        return [_to_dto(r) for r in rows]

    @get(
        "/{schedule_id:int}",
        guards=[cap_guard("view_exchange_schedule")],
        summary="Get one exchange schedule row by ID",
    )
    async def get_schedule(self, schedule_id: int) -> ExchangeScheduleDTO:
        async with async_session() as session:
            row = await session.get(ExchangeSchedule, schedule_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"exchange_schedule id={schedule_id} not found")
        return _to_dto(row)

    @post(
        "/",
        guards=[cap_guard("manage_exchange_schedule")],
        summary="Create a new exchange schedule row",
        status_code=201,
    )
    async def create_schedule(self, data: ExchangeScheduleWrite) -> ExchangeScheduleDTO:
        row = ExchangeSchedule(
            gate=data.gate.upper(),
            exchanges=[e.upper() for e in data.exchanges],
            date=data.date,
            weekdays=data.weekdays,
            session_name=data.session_name,
            is_open=data.is_open,
            open_time=data.open_time,
            close_time=data.close_time,
            snapshot_time=data.snapshot_time,
            snapshot_reset_time=data.snapshot_reset_time,
            reason=data.reason,
            source=data.source,
        )
        async with async_session() as session:
            async with session.begin():
                session.add(row)
            await session.refresh(row)
        await _invalidate_clock_cache()
        logger.info(
            "exchange_schedule: created gate=%s session=%s date=%s",
            row.gate, row.session_name, row.date,
        )
        return _to_dto(row)

    @put(
        "/{schedule_id:int}",
        guards=[cap_guard("manage_exchange_schedule")],
        summary="Full-replace an exchange schedule row",
    )
    async def update_schedule(
        self, schedule_id: int, data: ExchangeScheduleWrite
    ) -> ExchangeScheduleDTO:
        async with async_session() as session:
            row = await session.get(ExchangeSchedule, schedule_id)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"exchange_schedule id={schedule_id} not found",
                )
            if row.date is not None and row.date < date.today():
                raise HTTPException(
                    status_code=409,
                    detail="past-date overrides cannot be updated",
                )
            row.gate = data.gate.upper()
            row.exchanges = [e.upper() for e in data.exchanges]
            row.date = data.date
            row.weekdays = data.weekdays
            row.session_name = data.session_name
            row.is_open = data.is_open
            row.open_time = data.open_time
            row.close_time = data.close_time
            row.snapshot_time = data.snapshot_time
            row.snapshot_reset_time = data.snapshot_reset_time
            row.reason = data.reason
            row.source = data.source
            async with session.begin():
                session.add(row)
            await session.refresh(row)
        await _invalidate_clock_cache()
        logger.info(
            "exchange_schedule: updated id=%d gate=%s session=%s",
            schedule_id, row.gate, row.session_name,
        )
        return _to_dto(row)

    @delete(
        "/{schedule_id:int}",
        guards=[cap_guard("manage_exchange_schedule")],
        summary="Delete an exchange schedule row",
        status_code=204,
    )
    async def delete_schedule(self, schedule_id: int) -> None:
        async with async_session() as session:
            row = await session.get(ExchangeSchedule, schedule_id)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"exchange_schedule id={schedule_id} not found",
                )
            if row.date is None:
                raise HTTPException(
                    status_code=409,
                    detail="default gate rows cannot be deleted",
                )
            if row.date < date.today():
                raise HTTPException(
                    status_code=409,
                    detail="past-date overrides cannot be deleted",
                )
            async with session.begin():
                await session.delete(row)
        await _invalidate_clock_cache()
        logger.info("exchange_schedule: deleted id=%d", schedule_id)
