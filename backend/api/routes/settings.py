"""
Settings admin API — `/api/admin/settings/*`.

Reads and writes the `settings` table (DB-backed tunables). Paired with
the `/admin/settings` page. All endpoints admin-guarded.

Endpoints
  GET   /api/admin/settings                    — list all, grouped by category
  GET   /api/admin/settings/{key}              — read a single key
  PATCH /api/admin/settings/{key}              — update `value` (validated against value_type + schema)
  POST  /api/admin/settings/{key}/reset        — reset `value` back to `default_value`
"""

from __future__ import annotations

import msgspec
from litestar import Controller, get, patch, post
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.rbac import cap_guard
from backend.api.database import async_session
from backend.api.models import Setting
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.settings import invalidate_cache

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Live-effect dispatch — settings whose runtime state must be re-applied
# on change land here. Most settings are already re-read from the cache
# on use (background tasks, agent engine), so those need nothing extra;
# the ones below are exceptions because they were captured into
# long-lived objects at import time (e.g. logging handlers).
# ---------------------------------------------------------------------------

def _apply_live_effects(key: str, value: str) -> None:
    """Re-apply the new value to any long-lived object that cached it."""
    try:
        if key.startswith("logging."):
            _apply_log_level(key, value)
    except Exception as e:
        # Never break a PATCH just because live-reapply hiccuped —
        # the cache has the new value, next process restart gets it.
        logger.warning(f"Settings: live-reapply of {key!r} failed: {e}")


def _apply_log_level(key: str, value: str) -> None:
    """
    logging.file_log_level / .console_log_level / .error_log_level take
    effect on the matching handler immediately, so no restart is needed
    to raise/lower verbosity.
    """
    import logging
    from backend.shared.helpers import ramboq_logger as rl
    level = getattr(logging, str(value).strip().upper(), None)
    if level is None:
        # Numeric level as string, e.g. "20"
        try: level = int(value)
        except Exception: return
    target = {
        "logging.file_log_level":    rl.log_file_handler,
        "logging.console_log_level": rl.console_handler,
        "logging.error_log_level":   rl.error_file_handler,
    }.get(key)
    if target is not None:
        target.setLevel(level)
        logger.info(f"Settings: log handler for {key} now {value!r}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SettingInfo(msgspec.Struct):
    id: int
    category: str
    key: str
    value_type: str
    value: str
    default_value: str
    description: str
    schema: dict | None
    units: str | None
    updated_at: str


class SettingPatch(msgspec.Struct):
    value: str   # always sent as string; server casts per value_type


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(value_type: str, raw: str, schema: dict | None) -> str:
    """
    Validate + normalise an incoming string value against the stored
    value_type and optional schema. Returns the canonical string form;
    raises HTTPException(400) with a helpful message on any failure.
    """
    raw = (raw or "").strip()
    if value_type == "bool":
        if raw.lower() in ("1", "true", "yes", "on"):  return "true"
        if raw.lower() in ("0", "false", "no", "off"): return "false"
        raise HTTPException(status_code=400, detail=f"Expected bool; got {raw!r}")
    if value_type == "int":
        try:
            n = int(float(raw))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Expected integer; got {raw!r}")
        if schema:
            if "min" in schema and n < schema["min"]:
                raise HTTPException(status_code=400, detail=f"Minimum is {schema['min']}")
            if "max" in schema and n > schema["max"]:
                raise HTTPException(status_code=400, detail=f"Maximum is {schema['max']}")
        return str(n)
    if value_type == "float":
        try:
            f = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Expected number; got {raw!r}")
        if schema:
            if "min" in schema and f < schema["min"]:
                raise HTTPException(status_code=400, detail=f"Minimum is {schema['min']}")
            if "max" in schema and f > schema["max"]:
                raise HTTPException(status_code=400, detail=f"Maximum is {schema['max']}")
        return str(f)
    if value_type == "enum":
        allowed = (schema or {}).get("enum") or []
        if raw not in allowed:
            raise HTTPException(status_code=400,
                                detail=f"Must be one of {allowed}; got {raw!r}")
        return raw
    # string — free-form
    return raw


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

def _to_info(r: Setting) -> SettingInfo:
    return SettingInfo(
        id=r.id, category=r.category, key=r.key, value_type=r.value_type,
        value=r.value, default_value=r.default_value,
        description=r.description, schema=r.schema, units=r.units,
        updated_at=r.updated_at.isoformat() if r.updated_at else "",
    )


class SettingsController(Controller):
    path = "/api/admin/settings"
    # Per-route caps. Reads gated by `view_settings_readonly` (admin /
    # risk / ops / demo — demo gets read-only browse so the showcase
    # tour shows the operator-tunable knobs). Mutations + reset gated
    # by `manage_settings` (admin only).

    @get("/", guards=[cap_guard("view_settings_readonly")])
    async def list_all(self) -> list[SettingInfo]:
        async with async_session() as s:
            rows = (await s.execute(
                select(Setting).order_by(Setting.category, Setting.key)
            )).scalars().all()
        return [_to_info(r) for r in rows]

    @get("/{key:str}", guards=[cap_guard("view_settings_readonly")])
    async def read_one(self, key: str) -> SettingInfo:
        async with async_session() as s:
            row = (await s.execute(
                select(Setting).where(Setting.key == key)
            )).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail=f"Setting {key!r} not found")
        return _to_info(row)

    @patch("/{key:str}", guards=[cap_guard("manage_settings")])
    async def update(self, key: str, data: SettingPatch) -> SettingInfo:
        async with async_session() as s:
            row = (await s.execute(
                select(Setting).where(Setting.key == key)
            )).scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404, detail=f"Setting {key!r} not found")
            row.value = _validate(row.value_type, data.value, row.schema)
            await s.commit()
            await s.refresh(row)
        invalidate_cache()
        _apply_live_effects(key, row.value)
        logger.info(f"Settings: {key} updated to {row.value!r}")
        return _to_info(row)

    @post("/{key:str}/reset", guards=[cap_guard("manage_settings")])
    async def reset(self, key: str) -> SettingInfo:
        async with async_session() as s:
            row = (await s.execute(
                select(Setting).where(Setting.key == key)
            )).scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404, detail=f"Setting {key!r} not found")
            row.value = row.default_value
            await s.commit()
            await s.refresh(row)
        invalidate_cache()
        _apply_live_effects(key, row.value)
        logger.info(f"Settings: {key} reset to default {row.value!r}")
        return _to_info(row)
