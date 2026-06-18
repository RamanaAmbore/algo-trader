"""
Order template CRUD — /api/admin/templates/*

Templates are the "exit-rule preset" the operator picks at OrderTicket
submit time. The model is in backend.api.models.OrderTemplate, seeded
from backend.api.algo.templates_seed.SYSTEM_TEMPLATES. The matching
SvelteKit UI lives at /automation/templates.

Endpoint shape mirrors backend.api.routes.grammar.GrammarTokenController:
list/read are demo-readable (visitors browsing /automation/templates
see the system defaults); create/update/delete are admin-only.

System templates support partial edits to their TP / SL / wing values +
is_active toggle + is_default — operators tune the defaults from the
UI. The seeder preserves these tuned values across deploys (see
templates_seed._MUTABLE_FIELDS — name/description/applies_to refresh,
numerics are left alone). System templates can never be DELETEd.
"""

from __future__ import annotations

from litestar import Controller, get, post, patch, delete
from litestar.exceptions import HTTPException, NotFoundException
from sqlalchemy import select

from backend.api.auth_guard import admin_guard, auth_or_demo_guard
from backend.api.database import async_session
from backend.api.models import OrderTemplate
from backend.api.schemas import (
    OrderTemplateOut,
    OrderTemplateCreate,
    OrderTemplatePatch,
)
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


_APPLIES_TO_CHOICES = {"buy_any", "sell_option", "sell_any", "both"}


_TP_ORDER_TYPE_CHOICES = {"LIMIT", "MARKET"}


def _to_out(row: OrderTemplate) -> OrderTemplateOut:
    return OrderTemplateOut(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description or "",
        applies_to=row.applies_to,
        tp_pct=float(row.tp_pct) if row.tp_pct is not None else None,
        sl_pct=float(row.sl_pct) if row.sl_pct is not None else None,
        wing_premium_pct=(float(row.wing_premium_pct)
                          if row.wing_premium_pct is not None else None),
        wing_strike_offset=row.wing_strike_offset,
        tp_order_type=(row.tp_order_type or "LIMIT"),
        tp_scales_json=row.tp_scales_json,
        sl_trail_pct=(float(row.sl_trail_pct)
                      if row.sl_trail_pct is not None else None),
        is_default=row.is_default,
        is_system=row.is_system,
        is_active=row.is_active,
    )


def _validate_sl_trail_pct(value: float | None) -> None:
    if value is None:
        return
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="sl_trail_pct must be a number",
        )
    if v <= 0 or v >= 100:
        raise HTTPException(
            status_code=400,
            detail="sl_trail_pct must be in (0, 100) — % distance to "
                   "trail behind the favorable LTP extreme",
        )


def _validate_tp_order_type(value: str | None) -> None:
    if value is not None and value not in _TP_ORDER_TYPE_CHOICES:
        raise HTTPException(
            status_code=400,
            detail=f"tp_order_type must be one of {sorted(_TP_ORDER_TYPE_CHOICES)}",
        )


def _validate_tp_scales_json(value: str | None) -> None:
    """Parse + validate the scale-out JSON. Empty / None means "no
    scale-out, fall back to tp_pct". Otherwise a list of dicts with
    `at_pct > 0` and `0 < close_pct <= 100`, and the cumulative
    close_pct must not exceed 100. Order of entries is preserved as
    operator-supplied — caller may rely on at_pct ascending for
    readability but we don't enforce sort here."""
    if value is None:
        return
    s = value.strip()
    if not s:
        return
    import json as _json
    try:
        parsed = _json.loads(s)
    except _json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"tp_scales_json must be valid JSON: {e}",
        )
    if not isinstance(parsed, list):
        raise HTTPException(
            status_code=400,
            detail="tp_scales_json must be a JSON list",
        )
    cumulative_close = 0.0
    for idx, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise HTTPException(
                status_code=400,
                detail=f"tp_scales_json[{idx}] must be a dict with at_pct + close_pct",
            )
        at_pct    = entry.get("at_pct")
        close_pct = entry.get("close_pct")
        try:
            at_pct    = float(at_pct)
            close_pct = float(close_pct)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"tp_scales_json[{idx}] at_pct + close_pct must be numbers",
            )
        if at_pct <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"tp_scales_json[{idx}].at_pct must be > 0",
            )
        if close_pct <= 0 or close_pct > 100:
            raise HTTPException(
                status_code=400,
                detail=f"tp_scales_json[{idx}].close_pct must be in (0, 100]",
            )
        cumulative_close += close_pct
    if cumulative_close > 100:
        raise HTTPException(
            status_code=400,
            detail=f"tp_scales_json cumulative close_pct ({cumulative_close}) exceeds 100",
        )


def _validate_applies_to(value: str) -> None:
    if value not in _APPLIES_TO_CHOICES:
        raise HTTPException(
            status_code=400,
            detail=f"applies_to must be one of {sorted(_APPLIES_TO_CHOICES)}",
        )


async def _demote_existing_default(s, applies_to: str, exclude_id: int | None) -> None:
    """Find any existing is_default=True row in the given scope and flip
    it off. Used by CREATE + PATCH to enforce one-default-per-scope —
    auto-selector in OrderTicket would otherwise silently pick whichever
    sorted first if two defaults co-existed. Same enforcement the seeder
    runs at boot time."""
    stmt = select(OrderTemplate).where(
        OrderTemplate.applies_to == applies_to,
        OrderTemplate.is_default.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(OrderTemplate.id != exclude_id)
    existing = (await s.execute(stmt)).scalars().all()
    for r in existing:
        r.is_default = False


class OrderTemplateController(Controller):
    path = "/api/admin/templates"

    # ── List ───────────────────────────────────────────────────────────
    @get("/", guards=[auth_or_demo_guard])
    async def list_templates(self) -> list[OrderTemplateOut]:
        """List every template (system + custom). Sorted system-first
        so the canonical defaults appear at the top of the page."""
        async with async_session() as s:
            stmt = (
                select(OrderTemplate)
                .order_by(
                    OrderTemplate.is_system.desc(),
                    OrderTemplate.is_default.desc(),
                    OrderTemplate.name.asc(),
                )
            )
            rows = (await s.execute(stmt)).scalars().all()
        return [_to_out(r) for r in rows]

    # ── Read one ───────────────────────────────────────────────────────
    @get("/{template_id:int}", guards=[auth_or_demo_guard])
    async def get_template(self, template_id: int) -> OrderTemplateOut:
        async with async_session() as s:
            row = await s.get(OrderTemplate, template_id)
        if not row:
            raise NotFoundException(detail=f"order_template id={template_id} not found")
        return _to_out(row)

    # ── Create (custom only) ───────────────────────────────────────────
    @post("/", guards=[admin_guard])
    async def create_template(self, data: OrderTemplateCreate) -> OrderTemplateOut:
        _validate_applies_to(data.applies_to)
        async with async_session() as s:
            dup = await s.execute(
                select(OrderTemplate).where(OrderTemplate.name == data.name)
            )
            if dup.scalar_one_or_none():
                raise HTTPException(
                    status_code=409,
                    detail=f"template '{data.name}' already exists",
                )
            _validate_tp_order_type(data.tp_order_type)
            _validate_tp_scales_json(data.tp_scales_json)
            _validate_sl_trail_pct(data.sl_trail_pct)
            if data.is_default:
                await _demote_existing_default(s, data.applies_to, exclude_id=None)
            row = OrderTemplate(
                name=data.name,
                description=data.description or "",
                applies_to=data.applies_to,
                tp_pct=data.tp_pct,
                sl_pct=data.sl_pct,
                wing_premium_pct=data.wing_premium_pct,
                wing_strike_offset=data.wing_strike_offset,
                tp_order_type=(data.tp_order_type or "LIMIT"),
                tp_scales_json=data.tp_scales_json,
                sl_trail_pct=data.sl_trail_pct,
                is_default=data.is_default,
                is_system=False,
                is_active=data.is_active,
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
        return _to_out(row)

    # ── Update ─────────────────────────────────────────────────────────
    @patch("/{template_id:int}", guards=[admin_guard])
    async def patch_template(
        self,
        template_id: int,
        data: OrderTemplatePatch,
    ) -> OrderTemplateOut:
        async with async_session() as s:
            row = await s.get(OrderTemplate, template_id)
            if not row:
                raise NotFoundException(
                    detail=f"order_template id={template_id} not found"
                )
            if data.applies_to is not None:
                _validate_applies_to(data.applies_to)
            if data.tp_order_type is not None:
                _validate_tp_order_type(data.tp_order_type)
            if data.tp_scales_json is not None:
                _validate_tp_scales_json(data.tp_scales_json)
            if data.sl_trail_pct is not None:
                _validate_sl_trail_pct(data.sl_trail_pct)
            # One-default-per-scope enforcement. If the operator is
            # flipping is_default on (or moving this row into a new
            # scope while is_default stays on), demote any existing
            # is_default=True row in the resolved scope so two rows
            # can't co-exist as defaults — the auto-selector in
            # OrderTicket would silently pick whichever sorts first.
            target_scope = data.applies_to if data.applies_to is not None else row.applies_to
            target_default = data.is_default if data.is_default is not None else row.is_default
            if target_default:
                await _demote_existing_default(s, target_scope, exclude_id=row.id)
            # Apply only set fields. msgspec sentinels are None for
            # unset; we accept None as "leave unchanged" rather than
            # "clear" because the form sends every field every time.
            for field in (
                "name", "description", "applies_to",
                "tp_pct", "sl_pct",
                "wing_premium_pct", "wing_strike_offset",
                "tp_order_type", "tp_scales_json", "sl_trail_pct",
                "is_default", "is_active",
            ):
                v = getattr(data, field, None)
                if v is not None:
                    setattr(row, field, v)
            await s.commit()
            await s.refresh(row)
        return _to_out(row)

    # ── Delete (custom only) ───────────────────────────────────────────
    @delete("/{template_id:int}", guards=[admin_guard], status_code=200)
    async def delete_template(self, template_id: int) -> dict:
        async with async_session() as s:
            row = await s.get(OrderTemplate, template_id)
            if not row:
                raise NotFoundException(
                    detail=f"order_template id={template_id} not found"
                )
            if row.is_system:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "system templates cannot be deleted — toggle is_active "
                        "off to disable them instead"
                    ),
                )
            await s.delete(row)
            await s.commit()
        return {"deleted": True}
