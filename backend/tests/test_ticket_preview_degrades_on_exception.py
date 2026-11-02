"""
Route-level tests for POST /api/orders/ticket/preview.

Live bug (2026-11-02, confirmed via `ramboq_conn` prod logs): `ticket_preview`
had NO exception handling at all. Every Chain-tab field change while a
Bracket template is armed calls this endpoint, so a transient, expected,
self-resolving failure inside `apply_template_to_order` — e.g. the MCX
instruments cache hasn't warmed back up yet right after a `ramboq_conn`
restart, which correctly trips `_validate_exchange_qty_multiple`'s
`[QTY-GUARD] ... lot_size=0 ... Refusing order` safety check in
backend/brokers/base.py — propagated as a raw 500 "Internal Server Error"
instead of a clean degraded preview.

Covers:
  - a ValueError shaped like the real qty-guard message degrades to a clean 2xx,
    wing_feasible=False, and a masked/truncated `gtt_trigger_errors[0]`
    naming the real cause
  - a bare exception with no message (e.g. asyncio.TimeoutError) still
    produces a non-empty, useful error string (class-name fallback)
  - a genuine HTTPException(422) raised deep inside apply_template_to_order
    (e.g. template-override validation) still propagates as a 422, not
    swallowed into a soft success
  - a genuine HTTPException(422) raised by the broker-gates pre-check
    (MCX GTT capability) still propagates as a 422
  - `apply_template_to_order` returning None still returns the pre-existing
    empty-plan success response (happy-path regression guard)
  - a successful plan resolution is returned unchanged, with the real
    wing_feasible / gtt_trigger_errors computed by `_preview_template_meta`
  - a failure inside `_preview_template_meta` (e.g. a live LTP broker call
    failing) also degrades cleanly, not a 500
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _admin_patches():
    """Mark every request as authenticated so auth_or_demo_guard passes —
    same helper shape as test_orders_spread_check.py's _admin_patches."""
    return patch.multiple(
        "backend.api.auth_guard",
        is_authenticated_request=lambda _conn: True,
        is_admin_request=lambda _conn: True,
        jwt_guard=AsyncMock(return_value=None),
    )


def _preview_body(**overrides) -> dict:
    body = {
        "mode": "live",
        "side": "BUY",
        "tradingsymbol": "CRUDEOIL26NOVFUT",
        "quantity": 1,
        "exchange": "MCX",
        "product": "NRML",
        "account": "ZT1234",
        "reference_price": 6000.0,
        "template_id": 7,
    }
    body.update(overrides)
    return body


def _no_qty_resolution():
    """Patch out the real instruments-cache lot lookup so these tests
    never depend on live/seeded instrument data — mirrors how
    `apply_template_to_order` is independently mocked below."""
    return patch(
        "backend.api.routes.orders._rco_preview_resolve_qty",
        AsyncMock(return_value=100),
    )


def _skip_broker_gates():
    """`_preview_check_broker_gates` makes a real `capabilities_for()`
    lookup for MCX/NCO + a template_id — bypass it so these tests isolate
    the resolver failure under test, not an unrelated capability gate."""
    return patch(
        "backend.api.routes.orders._preview_check_broker_gates",
        AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
async def test_qty_guard_value_error_degrades_to_200_not_500(async_client):
    """The exact reported incident: apply_template_to_order raises the
    real [QTY-GUARD] ValueError shape. Must be a clean non-5xx, never a 500 (route default success is 201)."""
    qty_guard_msg = (
        "[QTY-GUARD] MCX lot_size=0 for qty=100 — instruments cache miss "
        "(no real MCX contract has lot_size<=1). Refusing order to "
        "prevent catastrophic oversize. Retry after cache warms."
    )
    with _admin_patches(), _skip_broker_gates(), _no_qty_resolution(), patch(
        "backend.api.algo.template_attach.apply_template_to_order",
        AsyncMock(side_effect=ValueError(qty_guard_msg)),
    ):
        response = await async_client.post(
            "/api/orders/ticket/preview", json=_preview_body()
        )

    assert response.status_code == 201
    body = response.json()
    assert body["wing_feasible"] is False
    assert isinstance(body["gtt_trigger_errors"], list) and body["gtt_trigger_errors"]
    assert "lot_size=0" in body["gtt_trigger_errors"][0]
    assert "Preview unavailable" in body["gtt_trigger_errors"][0]
    # Plan degrades to the existing empty-plan shape — never half-built.
    assert body["plan"]["gtts"] == []
    assert body["plan"]["wing"] is None


@pytest.mark.asyncio
async def test_exception_with_empty_str_falls_back_to_class_name(async_client):
    """asyncio.TimeoutError (and similar) has an empty str() — the
    degraded message must still be useful, not 'Preview unavailable: '."""
    with _admin_patches(), _skip_broker_gates(), _no_qty_resolution(), patch(
        "backend.api.algo.template_attach.apply_template_to_order",
        AsyncMock(side_effect=TimeoutError()),
    ):
        response = await async_client.post(
            "/api/orders/ticket/preview", json=_preview_body()
        )

    assert response.status_code == 201
    body = response.json()
    assert body["wing_feasible"] is False
    assert body["gtt_trigger_errors"] == ["Preview unavailable: TimeoutError"]


@pytest.mark.asyncio
async def test_degraded_message_is_truncated_and_masked(async_client):
    """A long message carrying a raw account code must come back masked
    and bounded to the schema's <=200-char budget."""
    from backend.shared.helpers.utils import mask_account

    long_msg = "account ZT1234 blew up: " + ("x" * 300)
    with _admin_patches(), _skip_broker_gates(), _no_qty_resolution(), patch(
        "backend.api.algo.template_attach.apply_template_to_order",
        AsyncMock(side_effect=ValueError(long_msg)),
    ):
        response = await async_client.post(
            "/api/orders/ticket/preview", json=_preview_body()
        )

    assert response.status_code == 201
    err = response.json()["gtt_trigger_errors"][0]
    assert len(err) <= 200
    assert "ZT1234" not in err
    assert mask_account("ZT1234") in err


@pytest.mark.asyncio
async def test_broker_gates_http_exception_propagates_as_422(async_client):
    """A genuine validation outcome from the broker-gates pre-check
    (e.g. MCX GTT capability refusal) must still be a real 422 — the
    generic exception handler must never swallow it into a 200."""
    from litestar.exceptions import HTTPException

    with _admin_patches(), patch(
        "backend.api.routes.orders._preview_check_broker_gates",
        AsyncMock(side_effect=HTTPException(status_code=422, detail="Selected broker does not support MCX GTT")),
    ):
        response = await async_client.post(
            "/api/orders/ticket/preview", json=_preview_body()
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_apply_template_http_exception_propagates_as_422(async_client):
    """apply_template_to_order itself raises HTTPException(422) deep in
    template-override validation (e.g. wing_premium_pct <= 0 on an
    already-saved bad template) — must still be a real 422, not swallowed
    into a soft degraded success."""
    from litestar.exceptions import HTTPException

    with _admin_patches(), _skip_broker_gates(), _no_qty_resolution(), patch(
        "backend.api.algo.template_attach.apply_template_to_order",
        AsyncMock(side_effect=HTTPException(status_code=422, detail="wing_premium_pct must be > 0")),
    ):
        response = await async_client.post(
            "/api/orders/ticket/preview", json=_preview_body()
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_none_result_returns_existing_empty_plan_200(async_client):
    """Regression guard: the pre-existing `result is None` branch (no
    template, no overrides) is untouched by the new try/except."""
    with _admin_patches(), _skip_broker_gates(), _no_qty_resolution(), patch(
        "backend.api.algo.template_attach.apply_template_to_order",
        AsyncMock(return_value=None),
    ):
        response = await async_client.post(
            "/api/orders/ticket/preview",
            json=_preview_body(template_id=None),
        )

    assert response.status_code == 201
    body = response.json()
    assert body["plan"]["gtts"] == []
    assert body["plan"]["wing"] is None
    assert body["wing_feasible"] is None
    assert body["gtt_trigger_errors"] == []


@pytest.mark.asyncio
async def test_happy_path_returns_real_plan_unchanged(async_client):
    """Successful resolution is unaffected by the new try/except — the
    real plan dict and the real _preview_template_meta outcome flow
    through unchanged."""
    fake_plan = MagicMock()
    fake_plan.to_dict.return_value = {
        "template_id": 7,
        "template_name": "Standard Bracket",
        "template_slug": "standard-bracket",
        "parent_account": "ZT1234",
        "parent_symbol": "CRUDEOIL26NOVFUT",
        "parent_side": "BUY",
        "parent_qty": 100,
        "parent_exchange": "MCX",
        "parent_fill_price": 6000.0,
        "gtts": [{"kind": "tp", "label": "TP", "trigger_price": 6100.0}],
        "wing": None,
        "notes": [],
    }
    fake_plan.gtts = []
    fake_result = MagicMock()
    fake_result.plan = fake_plan
    fake_result.wing_skipped_reason = None

    with _admin_patches(), _skip_broker_gates(), _no_qty_resolution(), patch(
        "backend.api.algo.template_attach.apply_template_to_order",
        AsyncMock(return_value=fake_result),
    ), patch(
        "backend.api.routes.orders._preview_template_meta",
        AsyncMock(return_value=(None, [])),
    ):
        response = await async_client.post(
            "/api/orders/ticket/preview", json=_preview_body()
        )

    assert response.status_code == 201
    body = response.json()
    assert body["plan"]["template_name"] == "Standard Bracket"
    assert body["plan"]["gtts"][0]["label"] == "TP"
    assert body["wing_feasible"] is None
    assert body["gtt_trigger_errors"] == []


@pytest.mark.asyncio
async def test_preview_template_meta_failure_also_degrades_to_200(async_client):
    """`_preview_template_meta` makes its own live broker LTP call
    (`_opl_price_from_broker`) and can fail independently of
    apply_template_to_order — that failure must also degrade, not 500."""
    fake_plan = MagicMock()
    fake_plan.to_dict.return_value = {
        "template_id": 7,
        "template_name": "Standard Bracket",
        "template_slug": "standard-bracket",
        "parent_account": "ZT1234",
        "parent_symbol": "CRUDEOIL26NOVFUT",
        "parent_side": "BUY",
        "parent_qty": 100,
        "parent_exchange": "MCX",
        "parent_fill_price": 6000.0,
        "gtts": [],
        "wing": None,
        "notes": [],
    }
    fake_plan.gtts = []
    fake_result = MagicMock()
    fake_result.plan = fake_plan
    fake_result.wing_skipped_reason = None

    with _admin_patches(), _skip_broker_gates(), _no_qty_resolution(), patch(
        "backend.api.algo.template_attach.apply_template_to_order",
        AsyncMock(return_value=fake_result),
    ), patch(
        "backend.api.routes.orders._preview_template_meta",
        AsyncMock(side_effect=RuntimeError("broker LTP fetch timed out")),
    ):
        response = await async_client.post(
            "/api/orders/ticket/preview", json=_preview_body()
        )

    assert response.status_code == 201
    body = response.json()
    assert body["wing_feasible"] is False
    assert "broker LTP fetch timed out" in body["gtt_trigger_errors"][0]
