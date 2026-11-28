"""
Regression test for TWO confirmed real-money gate bugs in
`_preflight_check_margin` (backend/api/algo/actions_preflight.py) —
the REAL live pre-order margin gate, not a diagnostic/alert-text
function.

Bug A (fixed by commit e94fb2034, still correct, pinned below):

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
a confirmed zero-margin requirement. That half of the fix is still
correct and is pinned below: a leg error must always void the parsed
total (`basket_margin_used` stays `None`), never silently pass as
confirmed-clean.

Bug B (the regression e94fb2034 ALSO introduced, fixed in this
revision): the same commit routed ANY leg carrying an `error` string
through a `raise RuntimeError(leg_errors)` straight into the
raised-exception keyword classifier (`any(k in msg for k in ("margin",
"fund", "shortfall", "balance"))`). That conflates "we could not
determine margin because of a transient/transport failure" with "we
confirmed a real margin shortfall" — exactly the distinction
`_dlf_inconclusive_message` (same file, `diagnose_live_failure`) was
built to preserve, and whose own docstring names this identical
hazard: a failing SDK method whose own NAME contains "margin" (e.g.
`get_margin_calculator`, `get_order_margin_details`) would be
misclassified as a confident margin-shortfall diagnosis purely because
of the method name, not because margin was ever actually confirmed
insufficient.

With `preflight.enforce_margin_dhan`/`enforce_margin_groww` turned ON,
Bug B meant a real, placeable order could get falsely BLOCKED by a
transient calculator hiccup (a timeout, a signature mismatch, any
generic SDK/transport failure whose text happens to mention "margin"
because that's the method's own name) — the operator would see a
misleading "insufficient margin" message for an order that was never
actually confirmed to lack margin.

Fix: a leg carrying its own `error` string is now treated as
INCONCLUSIVE — the same outcome as the pre-existing `required is
None` (no leg error at all) case: no blocker, no `margin_would_block`
entry (this is not a "would have blocked" outcome, so it must not
leak into log-only mode's diagnostic list either), diagnostics record
`margin_check_inconclusive` with the real leg-error text, and the
broker's own place_order remains the real backstop. The raised-
exception keyword classifier is now reached ONLY by a genuinely RAISED
exception from the basket-margin call itself (Kite's own
`basket_order_margins` can raise; this is unchanged, pre-existing,
out of scope for this fix) — never by a leg-level `error` string.

The ONLY path that still confirms a real MARGIN_SHORTFALL for
Dhan/Groww is the numeric `required > available` comparison in
`_preflight_handle_positive_margin` — never error-text keyword
matching on a leg's `error` field.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# ---------------------------------------------------------------------------
# Direct unit tests of _preflight_check_margin — the real gate function.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_total_with_margin_keyword_error_is_inconclusive_not_blocked():
    """A leg returning {"total": 0.0, "error": "<margin-keyword text>"}
    must be treated as INCONCLUSIVE, not as a confirmed MARGIN_SHORTFALL
    — the error text containing the word "margin" must never, on its
    own, confirm a real shortfall. It also must never silently pass as
    a confirmed-clean zero-margin read."""
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
    assert "MARGIN_SHORTFALL" not in codes, (
        f"a leg error must never be read as a confirmed shortfall, got {blockers}"
    )
    # Diagnostics must never claim a confirmed-clean zero-margin read either.
    assert diag.get("basket_margin_used") is None
    assert diag.get("margin_check_inconclusive")
    # Not a would-block outcome — must not appear in margin_would_block.
    assert not diag.get("margin_would_block")


@pytest.mark.asyncio
async def test_zero_total_with_margin_keyword_error_inconclusive_regardless_of_enforce():
    """Same leg shape with enforce=False (the Dhan/Groww default) — the
    inconclusive classification is independent of the enforce toggle:
    it was never a "would have blocked" outcome in the first place, so
    turning enforcement off must not change anything about it."""
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
    assert "MARGIN_SHORTFALL" not in codes
    assert diag.get("basket_margin_used") is None
    assert diag.get("margin_check_inconclusive")
    assert not diag.get("margin_would_block"), (
        "inconclusive must never masquerade as a log-only would-block entry"
    )


@pytest.mark.asyncio
async def test_transient_timeout_whose_sdk_method_name_contains_margin_is_inconclusive():
    """The exact hazard this fix exists to close: a clearly transient/
    generic SDK or transport failure (a timeout) whose error text
    contains the word "margin" ONLY because that's the literal name of
    the SDK method that failed (`get_margin_calculator`). This must
    NEVER be read as a confirmed MARGIN_SHORTFALL — it must be
    classified as inconclusive/failed-to-determine, exactly like any
    other unparseable margin response."""
    from backend.api.algo.actions_preflight import _preflight_check_margin

    bm_res = [{
        "total": 0.0,
        "error": "get_margin_calculator: connection timed out",
        "raw": None,
    }]
    margins_res = ({"enabled": True, "net": 500000.0}, None)
    margin_ctx = {"is_dhan_groww": True, "enforce": True, "side": "", "product": None,
                  "broker_id": "dhan"}

    blockers, diag = await _preflight_check_margin(
        bm_res, margins_res, "equity", "ZG0790", "NIFTY25APRFUT", 50,
        loop=None, exchange="NFO", lot_size=50, margin_ctx=margin_ctx,
    )

    codes = [b["code"] for b in blockers]
    assert "MARGIN_SHORTFALL" not in codes, (
        f"a transient timeout must never be read as a confirmed "
        f"shortfall merely because the SDK method's own name contains "
        f"'margin', got {blockers}"
    )
    assert diag.get("basket_margin_used") is None
    assert "connection timed out" in diag.get("margin_check_inconclusive", "")


@pytest.mark.asyncio
async def test_real_live_groww_signature_bug_payload_is_inconclusive_not_sufficient_not_blocked():
    """The EXACT confirmed live payload shape from CLAUDE.md's
    'Groww's margin call used the wrong SDK signature' entry:

        [{'total': 0.0, 'error':
          "get_order_margin_details() got an unexpected keyword
          argument 'trading_symbol'", 'raw': None}]

    The error text's method name, `get_order_margin_details`,
    contains the substring "margin" — but this is a transport/
    signature-mismatch failure, never a confirmed margin shortfall.
    This must be classified as inconclusive: neither a false
    "sufficient" (the original e94fb2034-fixed bug) NOR a false
    confirmed MARGIN_SHORTFALL (the regression e94fb2034 introduced)."""
    from backend.api.algo.actions_preflight import _preflight_check_margin

    bm_res = [{
        "total": 0.0,
        "error": "get_order_margin_details() got an unexpected keyword "
                 "argument 'trading_symbol'",
        "raw": None,
    }]
    margins_res = ({"enabled": True, "net": 500000.0}, None)

    for enforce in (True, False):
        margin_ctx = {"is_dhan_groww": True, "enforce": enforce, "side": "",
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
        assert "MARGIN_SHORTFALL" not in codes, (
            f"must not confirm a shortfall from error text alone "
            f"(enforce={enforce}), got {blockers}"
        )
        assert diag.get("margin_check_inconclusive")
        assert not diag.get("margin_would_block")


@pytest.mark.asyncio
async def test_multileg_one_leg_error_still_routes_to_inconclusive_even_with_nonzero_sum():
    """Multi-leg basket where the OTHER leg parses a real nonzero
    total (so the summed `required` is not merely 0.0) but ONE leg
    still carries an `error` — the whole basket must still be treated
    as inconclusive (never a confirmed MARGIN_SHORTFALL from error text,
    never a confirmed partial sum that understates the real
    requirement)."""
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
    assert "MARGIN_SHORTFALL" not in codes
    assert diag.get("basket_margin_used") is None, (
        "a leg error must void the whole basket's parsed sum, never "
        "silently keep the other leg's partial total"
    )
    assert diag.get("margin_check_inconclusive")


@pytest.mark.asyncio
async def test_required_none_with_leg_error_is_inconclusive():
    """The PRE-EXISTING `required is None` + leg error path (no
    parseable total on any leg at all) must also be inconclusive, not
    a confirmed MARGIN_SHORTFALL — same reasoning as every other
    leg-error case above."""
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
    assert "MARGIN_SHORTFALL" not in codes
    assert diag.get("basket_margin_used") is None
    assert diag.get("margin_check_inconclusive")


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
    assert not diag.get("margin_check_inconclusive")


@pytest.mark.asyncio
async def test_genuine_confirmed_shortfall_still_blocks_when_enforced():
    """The real detection path must be UNCHANGED by this fix: when the
    broker explicitly reports a parsed, numeric required-margin figure
    (no leg error — a genuine confirmed number, not error text) that
    exceeds the account's real available margin, this must still
    correctly produce a real MARGIN_SHORTFALL blocker when enforced."""
    from backend.api.algo.actions_preflight import _preflight_check_margin

    bm_res = [{"total": 600000.0}]  # no "error" key — a clean, confirmed figure
    margins_res = ({"enabled": True, "net": 500000.0}, None)
    margin_ctx = {"is_dhan_groww": True, "enforce": True, "side": "", "product": None,
                  "broker_id": "groww"}

    blockers, diag = await _preflight_check_margin(
        bm_res, margins_res, "equity", "ZG0790", "NIFTY25APRFUT", 50,
        loop=None, exchange="NFO", lot_size=50, margin_ctx=margin_ctx,
    )

    codes = [b["code"] for b in blockers]
    assert "MARGIN_SHORTFALL" in codes, (
        f"a genuine confirmed shortfall (required > available, no leg "
        f"error) must still block, got {blockers}"
    )
    assert diag.get("basket_margin_used") == 600000.0
    assert diag.get("margin_shortfall") == 100000.0
    assert not diag.get("margin_check_inconclusive")


@pytest.mark.asyncio
async def test_genuine_confirmed_shortfall_log_only_when_not_enforced():
    """Same genuine confirmed-shortfall figure, enforce=False — must be
    recorded as a would-have-blocked diagnostic, never appended to the
    live `blockers` list — the pre-existing, unchanged enforce-vs-
    log-only policy for a REAL confirmed shortfall."""
    from backend.api.algo.actions_preflight import _preflight_check_margin

    bm_res = [{"total": 600000.0}]
    margins_res = ({"enabled": True, "net": 500000.0}, None)
    margin_ctx = {"is_dhan_groww": True, "enforce": False, "side": "", "product": None,
                  "broker_id": "groww"}

    blockers, diag = await _preflight_check_margin(
        bm_res, margins_res, "equity", "ZG0790", "NIFTY25APRFUT", 50,
        loop=None, exchange="NFO", lot_size=50, margin_ctx=margin_ctx,
    )

    codes = [b["code"] for b in blockers]
    assert "MARGIN_SHORTFALL" not in codes
    would_block = diag.get("margin_would_block") or []
    assert would_block and would_block[0]["code"] == "MARGIN_SHORTFALL"


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
async def test_e2e_groww_margin_keyword_leg_error_is_inconclusive_not_blocked():
    """End-to-end through run_preflight: a Groww basket_order_margins
    response shaped like {"total": 0.0, "error": "<margin keyword>"}
    must NOT block the order — it is inconclusive, not a confirmed
    shortfall — even with preflight.enforce_margin_groww=True."""
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

    assert result["ok"] is True, (
        f"a leg error must never block a placeable order as a "
        f"confirmed shortfall, got {result}"
    )
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_SHORTFALL" not in codes
    assert result["diagnostics"].get("margin_check_inconclusive")


@pytest.mark.asyncio
async def test_e2e_transient_sdk_timeout_never_blocks_a_placeable_order():
    """End-to-end reproduction of the exact scenario this fix exists
    for: a transient SDK timeout from a method literally named
    `get_margin_calculator` must never falsely block a real order."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(broker_id="dhan")
    broker.basket_order_margins.return_value = [
        {"total": 0.0, "error": "get_margin_calculator: connection timed out"},
    ]
    conns = _conns_with("ZD1234")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)), \
         patch("backend.shared.helpers.settings.get_bool", return_value=True):
        result = await run_preflight("ZD1234", {
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


@pytest.mark.asyncio
async def test_e2e_genuine_confirmed_shortfall_still_blocks_when_enforced():
    """End-to-end: a genuine, confirmed numeric shortfall (no leg
    error) must still block for real — the real detection path is
    untouched by this fix."""
    from backend.api.algo.actions import run_preflight

    broker = _make_broker_stub(broker_id="groww", margin_net=500000.0)
    broker.basket_order_margins.return_value = [{"total": 600000.0}]
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
async def test_e2e_real_groww_signature_bug_payload_is_inconclusive_never_sufficient_never_blocked():
    """End-to-end reproduction of the EXACT confirmed live payload
    (CLAUDE.md: 'Groww's margin call used the wrong SDK signature').
    This must be inconclusive: NOT read as a confirmed-clean "0.0,
    sufficient" (the original e94fb2034-fixed bug) AND NOT read as a
    confirmed MARGIN_SHORTFALL purely because the failing SDK method's
    own name contains "margin" (the regression this revision fixes)."""
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

    assert result["ok"] is True
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_SHORTFALL" not in codes
    # The regression this test actually pins: diagnostics must never
    # claim a confirmed-clean zero-margin read for a failed leg, and
    # must never confirm a shortfall from error text alone.
    assert result["diagnostics"].get("basket_margin_used") is None
    assert result["diagnostics"].get("margin_check_inconclusive")
