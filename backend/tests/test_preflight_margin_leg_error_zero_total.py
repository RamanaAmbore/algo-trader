"""
Regression test for a confirmed real-money gate bug in
`_preflight_check_margin` (backend/api/algo/actions_preflight.py) —
the REAL live pre-order margin gate, not a diagnostic/alert-text
function.

Root cause: the gate's "unknown margin" branch only treated the
margin check as a genuine failure when `required is None` (no leg's
total could be parsed at all). A leg can ALSO return a response that
parses to a real numeric total of 0.0 while ALSO carrying its own
`error` string — the confirmed live Groww signature-mismatch payload
documented in CLAUDE.md's "Groww's margin call used the wrong SDK
signature" entry:

    [{"total": 0.0, "error":
      "...unexpected keyword argument 'trading_symbol'", "raw": None}]

Because `required` parsed to `0.0` (not `None`), the gate's
`required is None` check was skipped entirely, and the function fell
through to `_preflight_handle_positive_margin(required=0.0, ...)`,
which reads `required <= 0` as "no margin needed" and lets the order
pass with no blocker — treating a broker-side margin-check FAILURE as
a confirmed zero-margin requirement.

This is the identical bug class `diagnose_live_failure()` (same file)
was just fixed for in the same session (see
`test_diagnose_live_failure_message.py` ->
`test_total_zero_with_a_leg_error_is_inconclusive_not_sufficient`),
but in the REAL gate rather than in after-the-fact diagnostic text —
so a genuine Dhan/Groww margin-check failure must now be treated
EXACTLY the way the gate already treats any other margin-check
failure: routed through the same keyword-matching classification the
raised-exception branch already applies (which itself goes through
`_preflight_emit_margin_blocker`'s existing enforce-vs-log-only
`preflight.enforce_margin_dhan` / `preflight.enforce_margin_groww`
toggle), never a new, invented behaviour.

Fix: `_preflight_check_margin` now computes
`_preflight_collect_leg_errors(bm_result)` unconditionally (not only
inside the `required is None` branch) and treats `required is None OR
any leg carrying an error` as the same "margin check failed" case —
raising the same `RuntimeError` with the joined leg-error text that
the pre-existing `required is None` + leg-error path already raised,
so it is caught by the identical except-block keyword classifier below
it.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# ---------------------------------------------------------------------------
# Direct unit tests of _preflight_check_margin — the real gate function.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_total_with_margin_keyword_error_blocks_when_enforced():
    """A leg returning {"total": 0.0, "error": "<margin-keyword text>"}
    must surface as a real MARGIN_SHORTFALL blocker when the broker's
    enforce flag is True — not silently pass as a confirmed-zero
    margin requirement."""
    from backend.api.algo.actions_preflight import _preflight_check_margin

    bm_res = [{"total": 0.0, "error": "margin calculation failed upstream"}]
    margins_res = ({"enabled": True, "net": 500000.0}, None)
    margin_ctx = {"is_dhan_groww": True, "enforce": True, "side": "", "product": None,
                  "broker_id": "groww"}

    blockers, diag = await _preflight_check_margin(
        bm_res, margins_res, "equity", "ZG0790", "NIFTY25APRFUT", 50,
        loop=None, exchange="NFO", lot_size=50, margin_ctx=margin_ctx,
    )

    codes = [b["code"] for b in blockers]
    assert "MARGIN_SHORTFALL" in codes, f"expected a real blocker, got {blockers}"
    # Diagnostics must never claim a confirmed-clean zero-margin read.
    assert diag.get("basket_margin_used") is None


@pytest.mark.asyncio
async def test_zero_total_with_margin_keyword_error_log_only_when_not_enforced():
    """Same leg shape, but enforce=False (the Dhan/Groww default) —
    must be recorded as a would-have-blocked diagnostic, never appended
    to the live `blockers` list — matching the gate's existing
    enforce-vs-log-only policy for every other margin-check failure."""
    from backend.api.algo.actions_preflight import _preflight_check_margin

    bm_res = [{"total": 0.0, "error": "margin calculation failed upstream"}]
    margins_res = ({"enabled": True, "net": 500000.0}, None)
    margin_ctx = {"is_dhan_groww": True, "enforce": False, "side": "", "product": None,
                  "broker_id": "groww"}

    blockers, diag = await _preflight_check_margin(
        bm_res, margins_res, "equity", "ZG0790", "NIFTY25APRFUT", 50,
        loop=None, exchange="NFO", lot_size=50, margin_ctx=margin_ctx,
    )

    codes = [b["code"] for b in blockers]
    assert "MARGIN_SHORTFALL" not in codes, (
        "log-only mode must never append a real blocker"
    )
    would_block = diag.get("margin_would_block") or []
    assert would_block and would_block[0]["code"] == "MARGIN_SHORTFALL"


@pytest.mark.asyncio
async def test_real_live_groww_signature_bug_payload_never_reads_as_sufficient():
    """The EXACT confirmed live payload shape from CLAUDE.md's
    'Groww's margin call used the wrong SDK signature' entry:

        [{'total': 0.0, 'error':
          "get_order_margin_details() got an unexpected keyword
          argument 'trading_symbol'", 'raw': None}]

    The error text's method name, `get_order_margin_details`,
    contains the substring "margin" — so the gate's pre-existing
    keyword classifier (shared, unchanged, with the `required is
    None` + leg-error path) reads this as a genuine margin-check
    failure and routes it through `_preflight_emit_margin_blocker`
    exactly as it already does for the `required is None` case
    (confirmed directly against the pre-existing `required is None`
    path with the identical error text, below) — NOT a new behaviour
    introduced by this fix, and NOT the over-cautious/-permissive
    choice either, since it correctly surfaces as a real blocker
    rather than a silent pass-through.

    The actual regression this test pins (the narrower, more
    dangerous bug): pre-fix, this exact payload — `total: 0.0` plus
    an `error` string — was read as "required == 0.0, confirmed
    sufficient", with diagnostics claiming a clean basket-margin
    read of 0.0 and NO blocker at all. Post-fix, diagnostics must
    never make that "confirmed clean" claim for a leg carrying its
    own error, and the gate must route it through the SAME
    failure-handling path as every other margin-check failure
    (enforce-gated MARGIN_SHORTFALL), never silently pass it."""
    from backend.api.algo.actions_preflight import _preflight_check_margin

    bm_res = [{
        "total": 0.0,
        "error": "get_order_margin_details() got an unexpected keyword "
                 "argument 'trading_symbol'",
        "raw": None,
    }]
    margins_res = ({"enabled": True, "net": 500000.0}, None)

    # enforce=True: blocks for real, same as any other classified
    # margin-check failure on an enforced broker.
    margin_ctx = {"is_dhan_groww": True, "enforce": True, "side": "",
                   "product": None, "broker_id": "groww"}
    blockers, diag = await _preflight_check_margin(
        bm_res, margins_res, "equity", "ZG0790", "NIFTY25APRFUT", 50,
        loop=None, exchange="NFO", lot_size=50, margin_ctx=margin_ctx,
    )
    assert diag.get("basket_margin_used") is None, (
        f"must not report a confirmed-clean basket_margin_used for a "
        f"failed leg, got {diag}"
    )
    codes = [b["code"] for b in blockers]
    assert "MARGIN_SHORTFALL" in codes

    # enforce=False: computed but log-only, never appended live —
    # same enforce-vs-log-only policy every other margin blocker on
    # this gate already respects.
    margin_ctx = {"is_dhan_groww": True, "enforce": False, "side": "",
                   "product": None, "broker_id": "groww"}
    blockers, diag = await _preflight_check_margin(
        bm_res, margins_res, "equity", "ZG0790", "NIFTY25APRFUT", 50,
        loop=None, exchange="NFO", lot_size=50, margin_ctx=margin_ctx,
    )
    assert diag.get("basket_margin_used") is None
    codes = [b["code"] for b in blockers]
    assert "MARGIN_SHORTFALL" not in codes
    would_block = diag.get("margin_would_block") or []
    assert would_block and would_block[0]["code"] == "MARGIN_SHORTFALL"


@pytest.mark.asyncio
async def test_multileg_one_leg_error_still_routes_to_failure_path_even_with_nonzero_sum():
    """Multi-leg basket where the OTHER leg parses a real nonzero
    total (so the summed `required` is not merely 0.0) but ONE leg
    still carries an `error` — the whole basket must still be treated
    as a failed margin check, not a confirmed partial sum that
    understates the real requirement."""
    from backend.api.algo.actions_preflight import _preflight_check_margin

    bm_res = [
        {"total": 5000.0},
        {"total": 0.0, "error": "margin shortfall on leg 2"},
    ]
    margins_res = ({"enabled": True, "net": 500000.0}, None)
    margin_ctx = {"is_dhan_groww": True, "enforce": True, "side": "", "product": None,
                  "broker_id": "groww"}

    blockers, diag = await _preflight_check_margin(
        bm_res, margins_res, "equity", "ZG0790", "NIFTY25APRFUT", 50,
        loop=None, exchange="NFO", lot_size=50, margin_ctx=margin_ctx,
    )

    codes = [b["code"] for b in blockers]
    assert "MARGIN_SHORTFALL" in codes
    assert diag.get("basket_margin_used") is None, (
        "a leg error must void the whole basket's parsed sum, never "
        "silently keep the other leg's partial total"
    )


@pytest.mark.asyncio
async def test_required_none_with_leg_error_behaviour_unchanged():
    """Regression guard: the PRE-EXISTING `required is None` + leg
    error path (no parseable total on any leg) must still behave
    exactly as before this fix — routed to the same RuntimeError /
    keyword-classification path."""
    from backend.api.algo.actions_preflight import _preflight_check_margin

    bm_res = [{"error": "margin shortfall, no total field at all"}]
    margins_res = ({"enabled": True, "net": 500000.0}, None)
    margin_ctx = {"is_dhan_groww": True, "enforce": True, "side": "", "product": None,
                  "broker_id": "groww"}

    blockers, diag = await _preflight_check_margin(
        bm_res, margins_res, "equity", "ZG0790", "NIFTY25APRFUT", 50,
        loop=None, exchange="NFO", lot_size=50, margin_ctx=margin_ctx,
    )

    codes = [b["code"] for b in blockers]
    assert "MARGIN_SHORTFALL" in codes
    assert diag.get("basket_margin_used") is None


@pytest.mark.asyncio
async def test_required_zero_with_no_leg_error_is_still_a_confirmed_clean_pass():
    """Regression guard: this fix must NOT over-tighten the gate — a
    genuine `required == 0.0` with NO leg error anywhere (a real
    credit/zero-margin outcome) must still pass cleanly with no
    blocker, exactly as before."""
    from backend.api.algo.actions_preflight import _preflight_check_margin

    bm_res = [{"total": 0.0}]
    margins_res = ({"enabled": True, "net": 500000.0}, None)
    margin_ctx = {"is_dhan_groww": True, "enforce": True, "side": "", "product": None,
                  "broker_id": "groww"}

    blockers, diag = await _preflight_check_margin(
        bm_res, margins_res, "equity", "ZG0790", "NIFTY25APRFUT", 50,
        loop=None, exchange="NFO", lot_size=50, margin_ctx=margin_ctx,
    )

    assert blockers == []
    assert diag.get("basket_margin_used") == 0.0


# ---------------------------------------------------------------------------
# End-to-end through run_preflight(), mirroring the existing enforce /
# log-only test pattern in test_preflight_characterization.py.
# ---------------------------------------------------------------------------

def _make_broker_stub(*,
                       broker_id: str = "groww",
                       margin_net: float = 500000.0) -> MagicMock:
    broker = MagicMock()
    broker.broker_id = broker_id
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "BSE", "MCX", "CDS"]}
    broker.instruments.return_value = [{
        "tradingsymbol": "NIFTY25APRFUT",
        "exchange":      "NFO",
        "instrument_type": "FUT",
        "freeze_qty":    6000,
        "lot_size":      50,
        "tick_size":     0.05,
    }]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": margin_net},
        "commodity": {"enabled": True, "net": margin_net},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


def _conns_with(account: str):
    c = MagicMock()
    c.conn = {account: object()}
    return c


@pytest.mark.asyncio
async def test_e2e_groww_margin_keyword_leg_error_blocks_when_enforced():
    """End-to-end through run_preflight: a Groww basket_order_margins
    response shaped like {"total": 0.0, "error": "<margin keyword>"}
    must block for real once preflight.enforce_margin_groww=True."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(broker_id="groww")
    broker.basket_order_margins.return_value = [
        {"total": 0.0, "error": "insufficient margin to price this order"},
    ]
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)), \
         patch("backend.shared.helpers.settings.get_bool", return_value=True):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      100,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_SHORTFALL" in codes


@pytest.mark.asyncio
async def test_e2e_groww_margin_keyword_leg_error_log_only_by_default():
    """Same payload, default (unenforced) settings — must log-only,
    never block, matching the existing Dhan/Groww default."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(broker_id="groww")
    broker.basket_order_margins.return_value = [
        {"total": 0.0, "error": "insufficient margin to price this order"},
    ]
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)), \
         patch("backend.shared.helpers.settings.get_bool", return_value=False):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      100,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is True
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_SHORTFALL" not in codes
    wb = result["diagnostics"].get("margin_would_block") or []
    assert wb and wb[0]["code"] == "MARGIN_SHORTFALL"


@pytest.mark.asyncio
async def test_e2e_real_groww_signature_bug_payload_never_passes_as_sufficient():
    """End-to-end reproduction of the EXACT confirmed live payload
    (CLAUDE.md: 'Groww's margin call used the wrong SDK signature').
    Before this fix, run_preflight's diagnostics would have reported
    `basket_margin_used: 0.0` with no blocker — a confidently-wrong
    "margin confirmed sufficient" read for an order whose real margin
    requirement was never actually determined.

    The error text's method name, `get_order_margin_details`, happens
    to contain the substring "margin", so the gate's pre-existing
    keyword classifier (shared with, and unchanged by this fix, the
    `required is None` + leg-error path) correctly routes this to a
    real MARGIN_SHORTFALL blocker rather than failing open — the gate
    errs toward blocking an order with a genuinely-unknown margin
    requirement, not toward silently letting it through."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(broker_id="groww")
    broker.basket_order_margins.return_value = [{
        "total": 0.0,
        "error": "get_order_margin_details() got an unexpected keyword "
                 "argument 'trading_symbol'",
        "raw": None,
    }]
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)), \
         patch("backend.shared.helpers.settings.get_bool", return_value=True):
        result = await run_preflight("ZG0790", {
            "exchange":      "NFO",
            "tradingsymbol": "NIFTY25APRFUT",
            "quantity":      100,
            "order_type":    "LIMIT",
            "product":       "NRML",
            "variety":       "regular",
            "side":          "BUY",
            "price":         22000.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_SHORTFALL" in codes
    # The regression this test actually pins: diagnostics must never
    # claim a confirmed-clean zero-margin read for a failed leg.
    assert result["diagnostics"].get("basket_margin_used") is None
