"""Unit tests for D1 (product/variety/validity threading) and D5
(orphaned-chase-task cleanup) in `_live_chase_config` /
`_start_live_chase` (backend/api/routes/orders_helpers.py).

D1 — pre-fix, every chased ticket re-placed each attempt with
ChaseConfig's hardcoded dataclass defaults (product="NRML",
variety="regular", validity="DAY") regardless of what the operator
actually selected — a chased MIS intraday close was silently re-placed
as NRML on every re-quote.

D5 — pre-fix, a ticket-side chase failure (first-attempt error) or
15s confirm-timeout could leave the spawned `chase_order` background
task running completely unattended: the ticket route returned an
error to the operator immediately while the task kept retrying/
placing live orders with nobody watching.

These tests patch `backend.api.algo.chase.chase_order` (the lazy
import site inside `_start_live_chase`) with fake coroutines so no
real broker/event-loop machinery is exercised.

Also covers the ACTUAL ticket-route call site,
`orders_place.py:_ticket_place_or_chase_live` — the D1 bug was that
THIS function never forwarded product/variety/validity into
`_start_live_chase` at all (a pure `_start_live_chase`-level test
would pass even if the route-level plumbing were reverted).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.api.routes import orders_helpers as oh


# ── D1 — _live_chase_config forwards product/variety/validity ────────────

class TestLiveChaseConfigProductVarietyValidity:
    def test_defaults_match_chaseconfig_defaults(self):
        """Callers that don't pass the new kwargs (e.g.
        test_close_intent_chase.py) must see unchanged behavior."""
        cfg = oh._live_chase_config(aggressiveness="low")
        assert cfg.product == "NRML"
        assert cfg.variety == "regular"
        assert cfg.validity == "DAY"

    def test_mis_product_forwarded(self):
        cfg = oh._live_chase_config(aggressiveness="low", product="MIS")
        assert cfg.product == "MIS"

    def test_variety_and_validity_forwarded(self):
        cfg = oh._live_chase_config(
            aggressiveness="med", product="MIS",
            variety="amo", validity="IOC",
        )
        assert cfg.product == "MIS"
        assert cfg.variety == "amo"
        assert cfg.validity == "IOC"

    def test_intent_still_propagates_alongside_new_kwargs(self):
        cfg = oh._live_chase_config(
            aggressiveness="low", intent="close", product="MIS",
        )
        assert cfg.intent == "close"
        assert cfg.product == "MIS"


# ── D1 — _start_live_chase end-to-end forwarding ──────────────────────────

@pytest.mark.asyncio
async def test_start_live_chase_forwards_product_variety_validity_to_chase_order():
    """A chased MIS close must send cfg.product == 'MIS' to chase_order,
    not the previously-hardcoded 'NRML'."""
    captured: dict = {}

    async def _fake_chase_order(account, symbol, transaction_type, quantity,
                                cfg, on_event=None, algo_order_id=None):
        captured["cfg"] = cfg
        on_event("order_placed", {"order_id": "B1"})
        return SimpleNamespace(status="filled")

    with patch("backend.api.algo.chase.chase_order", new=_fake_chase_order):
        order_id = await oh._start_live_chase(
            account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
            transaction_type="SELL", quantity=50,
            aggressiveness="low", intent="close",
            product="MIS", variety="regular", validity="DAY",
        )

    assert order_id == "B1"
    cfg = captured["cfg"]
    assert cfg.product == "MIS", (
        f"chase must re-place with the operator's actual product "
        f"(MIS), not the hardcoded ChaseConfig default. Got {cfg.product}."
    )
    assert cfg.variety == "regular"
    assert cfg.validity == "DAY"
    assert cfg.intent == "close"


@pytest.mark.asyncio
async def test_start_live_chase_default_kwargs_preserve_nrml():
    """Callers that don't pass product/variety/validity (none exist
    today besides the ticket path, but future callers might) keep the
    pre-fix NRML/regular/DAY behavior."""
    captured: dict = {}

    async def _fake_chase_order(account, symbol, transaction_type, quantity,
                                cfg, on_event=None, algo_order_id=None):
        captured["cfg"] = cfg
        on_event("order_placed", {"order_id": "B2"})
        return SimpleNamespace(status="filled")

    with patch("backend.api.algo.chase.chase_order", new=_fake_chase_order):
        await oh._start_live_chase(
            account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
            transaction_type="BUY", quantity=50, aggressiveness="low",
        )

    assert captured["cfg"].product == "NRML"
    assert captured["cfg"].variety == "regular"
    assert captured["cfg"].validity == "DAY"


# ── D1 — the actual ticket-route call site ────────────────────────────────

@pytest.mark.asyncio
async def test_ticket_place_or_chase_live_forwards_product_variety_validity():
    """D1 root-cause coverage: `_ticket_place_or_chase_live` (the ONLY
    real call site of `_start_live_chase`, invoked from
    `ticket_order_handler`) must itself forward product/variety/
    validity — pre-fix this function called `_start_live_chase` with
    NEITHER kwarg at all, so a `_start_live_chase`-only test could
    pass even if this route-level plumbing were reverted."""
    from backend.api.routes.orders_place import _ticket_place_or_chase_live

    data = SimpleNamespace(
        chase=True, order_type="LIMIT", price=100.0, exchange="NFO",
        chase_aggressiveness="low", product="MIS", variety="regular",
        intent="close",
        # Deliberately NO `validity` attribute — TicketOrderRequest
        # doesn't carry one (only ModifyOrderRequest does). The
        # `getattr(data, "validity", None) or "DAY"` fallback must
        # produce "DAY" without raising AttributeError.
    )

    mock_start = AsyncMock(return_value="B1")
    with patch("backend.api.routes.orders_helpers._start_live_chase", mock_start):
        order_id, chase_eligible = await _ticket_place_or_chase_live(
            data, account="ACC1", sym="NIFTY24DECFUT", side="SELL",
            qty=50, live_algo_id=None, ls_for_translate=50,
        )

    assert chase_eligible is True
    assert order_id == "B1"
    kwargs = mock_start.call_args.kwargs
    assert kwargs["product"] == "MIS", (
        f"expected the ticket's actual product (MIS) forwarded to "
        f"_start_live_chase, got {kwargs.get('product')!r}"
    )
    assert kwargs["variety"] == "regular"
    assert kwargs["validity"] == "DAY", (
        "TicketOrderRequest has no validity field — getattr fallback "
        "must produce 'DAY', matching the non-chase direct-place branch"
    )
    assert kwargs["intent"] == "close"


@pytest.mark.asyncio
async def test_ticket_place_or_chase_live_defaults_when_product_blank():
    """An empty-string / falsy product on the request must still fall
    back to 'NRML' (matches `data.product or "NRML"`)."""
    from backend.api.routes.orders_place import _ticket_place_or_chase_live

    data = SimpleNamespace(
        chase=True, order_type="LIMIT", price=100.0, exchange="NFO",
        chase_aggressiveness="low", product="", variety="",
        intent=None,
    )

    mock_start = AsyncMock(return_value="B2")
    with patch("backend.api.routes.orders_helpers._start_live_chase", mock_start):
        await _ticket_place_or_chase_live(
            data, account="ACC1", sym="NIFTY24DECFUT", side="BUY",
            qty=50, live_algo_id=None, ls_for_translate=50,
        )

    kwargs = mock_start.call_args.kwargs
    assert kwargs["product"] == "NRML"
    assert kwargs["variety"] == "regular"
    assert kwargs["validity"] == "DAY"


# ── D5 — orphaned chase-task cleanup ──────────────────────────────────────

@pytest.mark.asyncio
async def test_d5_pre_placement_error_cancels_orphaned_task():
    """A first-attempt 'error' event (chase_order tolerates it and
    retries) must not leave the background task running unattended
    after the ticket-side wait gives up — the task must be cancelled."""
    cancelled = {"v": False}

    async def _fake_chase_order(account, symbol, transaction_type, quantity,
                                cfg, on_event=None, algo_order_id=None):
        on_event("error", {"error": "boom"})
        try:
            # Simulates chase_order parked in its inter-attempt sleep
            # after tolerating the first error (real chase_order
            # retries up to _MAX_CHASE_ERRORS before giving up).
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled["v"] = True
            raise
        return SimpleNamespace(status="chasing")

    with patch("backend.api.algo.chase.chase_order", new=_fake_chase_order):
        with pytest.raises(RuntimeError, match="boom"):
            await oh._start_live_chase(
                account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
                transaction_type="BUY", quantity=50, aggressiveness="low",
            )
        # Let the cancellation actually propagate into the task.
        await asyncio.sleep(0.05)

    assert cancelled["v"] is True, (
        "the orphaned chase_order task must be cancelled once the "
        "ticket-side wait gives up on a pre-placement error — leaving "
        "it running is the D5 bug (unattended live chase)."
    )


@pytest.mark.asyncio
async def test_d5_confirm_timeout_marks_abandoned_and_kills_late_placement(monkeypatch):
    """When no event arrives within the confirm window (first
    place_order call still in flight), the task must NOT be
    hard-cancelled (chase_order has no CancelledError handling — the
    broker call could still land). Instead, a LATE order_placed event
    must be fed into chase.py's own `mark_killed()` so the next poll
    cancels it at the broker."""
    monkeypatch.setattr(oh, "_LIVE_CHASE_CONFIRM_TIMEOUT_S", 0.05)

    async def _fake_chase_order(account, symbol, transaction_type, quantity,
                                cfg, on_event=None, algo_order_id=None):
        # Exceeds the shrunk confirm timeout before the first event.
        await asyncio.sleep(0.2)
        on_event("order_placed", {"order_id": "LATE1"})
        return SimpleNamespace(status="filled")

    from backend.api.algo import chase as chase_mod

    with patch("backend.api.algo.chase.chase_order", new=_fake_chase_order):
        with pytest.raises(RuntimeError, match="timed out"):
            await oh._start_live_chase(
                account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
                transaction_type="BUY", quantity=50, aggressiveness="low",
            )
        # Let the fake task's delayed order_placed event fire.
        await asyncio.sleep(0.3)

    assert chase_mod.is_killed("LATE1") is True, (
        "a late order_placed after the ticket-side confirm-timeout "
        "give-up must be killed via chase.py's own mark_killed() so "
        "the next poll cancels it at the broker."
    )


@pytest.mark.asyncio
async def test_d5_task_finishes_without_any_event_fails_fast():
    """chase_order returning WITHOUT ever emitting an event (e.g. the
    market-closed / non-prod-branch early-return paths) must fail the
    ticket fast with the real reason instead of eating the full
    confirm timeout for a blank TimeoutError."""
    async def _fake_chase_order(account, symbol, transaction_type, quantity,
                                cfg, on_event=None, algo_order_id=None):
        return SimpleNamespace(status="failed", detail="NFO closed — chase not started")

    with patch("backend.api.algo.chase.chase_order", new=_fake_chase_order):
        with pytest.raises(RuntimeError, match="NFO closed"):
            await asyncio.wait_for(
                oh._start_live_chase(
                    account="ACC1", symbol="NIFTY24DECFUT", exchange="NFO",
                    transaction_type="BUY", quantity=50, aggressiveness="low",
                ),
                timeout=2.0,
            )
