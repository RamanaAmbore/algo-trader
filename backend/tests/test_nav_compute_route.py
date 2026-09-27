"""Tests for `POST /api/nav/compute` (backend/api/routes/nav.py).

2026-09-27 council audit follow-up: `write_nav_snapshot()` can now skip
its own DB upsert (an understated leg — no last-known-good anywhere).
Pre-fix, this operator-triggered route always returned a normal 200 with
numbers, silently implying a row was written even when it wasn't — the
operator had no way to tell "compute now" actually landed in `nav_daily`
from "computed but not persisted". `written=False` makes that
distinguishable.

Invoked via `.fn(self=None)` — the raw handler underneath Litestar's
`@post` decorator — matching the established pattern in
`test_nav_by_account_route.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.api.routes.nav import NavController


@pytest.mark.asyncio
async def test_written_true_on_normal_write():
    snap = {
        "nav": 5000.0, "cash_total": 5000.0, "positions_mtm": 0.0,
        "holdings_mtm": 0.0, "accounts": ["ZG0790"], "errors": [],
    }
    with patch(
        "backend.api.algo.nav.write_nav_snapshot", new=AsyncMock(return_value=snap),
    ):
        resp = await NavController.compute_now.fn(self=None)

    assert resp.written is True
    assert resp.nav == pytest.approx(5000.0)


@pytest.mark.asyncio
async def test_written_false_when_write_was_skipped():
    snap = {
        "nav": 5000.0, "cash_total": 5000.0, "positions_mtm": 0.0,
        "holdings_mtm": 0.0, "accounts": ["ZG0790"],
        "errors": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
        "skipped_write": True,
    }
    with patch(
        "backend.api.algo.nav.write_nav_snapshot", new=AsyncMock(return_value=snap),
    ):
        resp = await NavController.compute_now.fn(self=None)

    assert resp.written is False, (
        "the operator must be able to tell 'computed but not persisted' "
        "from 'actually written to nav_daily' — a silent 200 masked this "
        "pre-fix"
    )
    # Figures are still surfaced for operator inspection even though
    # nothing was persisted.
    assert resp.nav == pytest.approx(5000.0)
    assert resp.errors == ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"]


@pytest.mark.asyncio
async def test_written_true_on_forced_write():
    """A forced write (last-ditch-before-midnight path, or any future
    caller passing force=True) never sets skipped_write — written stays
    True."""
    snap = {
        "nav": 5000.0, "cash_total": 5000.0, "positions_mtm": 0.0,
        "holdings_mtm": 0.0, "accounts": ["ZG0790"],
        "errors": ["UNDERSTATED: margins: DH6847 fetch failed, no last-known-good available"],
    }
    with patch(
        "backend.api.algo.nav.write_nav_snapshot", new=AsyncMock(return_value=snap),
    ):
        resp = await NavController.compute_now.fn(self=None)

    assert resp.written is True
