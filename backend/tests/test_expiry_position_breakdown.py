"""Tests for `_v2_expiry_preclose_alert_section()` / `_v2_expiry_exchange_
breakdown_or_empty()` (agent_engine.py) — the per-exchange account/symbol
breakdown appended to the expiry-day pre-close alert. Covers the
account-grouped rendering rules:
  - A leg that genuinely needs closing (`needs_close=True`) renders under
    its masked account's "Close: SYM1, SYM2" line — plain tradingsymbol
    only, no side/qty/the engine's internal `close_reason` narrative.
  - A fully-netted (MCX) or OTM (NFO) leg renders NOTHING at all — no
    "netted"/"safe"/"hedged"/"no action" line, it is simply absent.
  - A leg whose moneyness could not be confirmed (UNKNOWN) is NEVER
    silently dropped — it renders under a distinct "Check (price
    unavailable)" line in the same account block, never merged into
    "Close:" and never producing a false "nothing to close".
  - A scan failure renders an explicit degraded note, never a silent
    blank (which would read as a false "all clear").
  - Multiple accounts holding the same root/symbol are never pooled —
    each gets its own "Account: ..." block.

2026-11-26 split: `_v2_expiry_position_breakdown()` (which re-derived,
internally, which exchange(s) were in-window) is DELETED. Its render
core is extracted into `_v2_expiry_exchange_breakdown_or_empty(exchange)`,
and `_v2_expiry_preclose_alert_section(context, exchange)` is its thin
wrapper for the pre-close alert — the caller's own agent condition
(now exchange-pure) already confirmed the window, so this function
takes an EXPLICIT single exchange and never re-derives a set, mirroring
`_v2_expiry_morning_preview(context, exchange)`'s own, already-proven
shape exactly. See `agent_engine.py`'s history comment above
`_EXPIRY_DAY_AWARENESS_AGENTS` for the live incident this fixes.

2026-11-26, same-day follow-up (operator: "audit all the alerts for
market expiry close and make them consistent. Only show the required
info like the positions that only that need to be closed") — the old
demoted "netted: ROOT" / "no action (OTM): ROOT" lines are REMOVED
entirely. A leg/root needing no action is now simply absent from the
rendered text, never mentioned with neutral/reassuring wording.

2026-11-27 (operator: "the exp alerts can show the only the options
that need to be closed. you can show account, the symbol list to be
closed. update exp alert messages to keep it simple and easy to
understand") — the per-ROOT breakdown that showed side, qty-in-lots,
and the engine's own `close_reason` narrative (e.g. "MCX unhedged ITM
after 4-rule netting (residual qty +1; theta=-1.200)") is replaced by a
per-ACCOUNT breakdown showing only the plain symbol list under
"Close:". `_v2_expiry_root_action_lines`/`_v2_expiry_root_unknown_lines`/
`_v2_expiry_qty_display` are deleted along with it — there is no more
qty/lot display in this alert, so the lot-size-resolution-failure
fallback test for it is removed too (the feature it covered no longer
exists). This also changes the fallback-text self-contradiction guard
one more notch: when expiring positions exist but NONE need closing AND
none are UNKNOWN, `_v2_expiry_exchange_section()` still returns `None`
(same as "nothing expiring at all"), so the caller's fallback wording —
"No {exchange} F&O positions need closing today", never "are expiring
today" — still holds true in both cases.

Shares `ExpiryEngine.scan_positions()` (mocked here) with the sibling
order-placing action — this file does not re-implement any netting
logic itself, only asserts on the rendered text given a fabricated
`engine.state.positions` / `scan_positions()` return value.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.api.algo.agent_engine import _v2_expiry_preclose_alert_section
from backend.api.algo.expiry import OptionPosition


def _pos(**kw) -> OptionPosition:
    defaults = dict(
        account="ZG0790", tradingsymbol="X", exchange="MCX",
        instrument_type="CE", underlying="GOLD", strike=0.0,
        expiry=None, quantity=0, product="NRML",
    )
    defaults.update(kw)
    return OptionPosition(**defaults)


def _mock_engine(positions: list[OptionPosition], to_close: list[OptionPosition] | None = None):
    engine = MagicMock()
    engine.scan_positions.return_value = to_close if to_close is not None else [
        p for p in positions if p.needs_close
    ]
    engine.state.positions = positions
    return engine


@pytest.mark.asyncio
async def test_mcx_unnetted_leg_renders_under_masked_account_with_plain_symbol():
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM after 4-rule netting (residual qty +1; theta=-1.200)"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_preclose_alert_section({}, "MCX")
    assert text is not None
    assert "MCX:" in text
    assert "Account:" in text
    assert "Z#####" in text or "ZG####" in text or "#" in text, "account must be masked"
    assert "Close: GOLD26OCT75000CE" in text
    assert "safe" not in text.lower()
    # The old per-leg side/qty/close_reason narrative must be gone.
    assert "SELL" not in text and "BUY" not in text
    assert "4-rule" not in text
    assert "theta" not in text.lower()
    assert "residual qty" not in text.lower()


@pytest.mark.asyncio
async def test_mcx_fully_netted_root_renders_nothing_to_close_fallback():
    """A fully-netted MCX root renders NOTHING — the section itself has
    no content, so the caller's "nothing to close" fallback fires
    instead, and it must never claim CRUDEOIL is not expiring (it
    genuinely is — it just needs no action)."""
    positions = [
        _pos(account="ZG0790", tradingsymbol="CRUDEOIL26OCT6000CE", underlying="CRUDEOIL",
             exchange="MCX", quantity=1, residual_qty=0, needs_close=False, moneyness="ITM"),
        _pos(account="ZG0790", tradingsymbol="CRUDEOIL26OCT6000PE", underlying="CRUDEOIL",
             exchange="MCX", quantity=-1, residual_qty=0, needs_close=False, moneyness="ITM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_preclose_alert_section({}, "MCX")
    assert text is not None
    assert "nothing to close" in text.lower()
    assert "MCX" in text
    for banned in ("netted", "safe", "hedged, no action needed", "no action needed", "are expiring today"):
        assert banned not in text.lower(), f"must never claim an assurance or contradiction: {banned!r} found"


@pytest.mark.asyncio
async def test_mcx_mixed_root_renders_only_the_needs_close_leg():
    """One account holding a needs-close leg (GOLD) and a fully-netted
    leg (CRUDEOIL) must show only GOLD under "Close:" — the netted
    CRUDEOIL leg is simply absent, no demoted mention anywhere."""
    positions = [
        _pos(account="ZG0790", tradingsymbol="CRUDEOIL26OCT6000CE", underlying="CRUDEOIL",
             exchange="MCX", quantity=1, residual_qty=0, needs_close=False, moneyness="ITM"),
        _pos(account="ZG0790", tradingsymbol="CRUDEOIL26OCT6000PE", underlying="CRUDEOIL",
             exchange="MCX", quantity=-1, residual_qty=0, needs_close=False, moneyness="ITM"),
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_preclose_alert_section({}, "MCX")
    assert text is not None
    assert "GOLD26OCT75000CE" in text
    assert "CRUDEOIL" not in text, "the fully-netted CRUDEOIL root must be entirely absent, not demoted"
    assert "netted" not in text.lower()
    # Header line + Account line + exactly one Close line.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 3, f"expected header + Account + Close line only, got: {lines}"
    assert lines[2].strip() == "Close: GOLD26OCT75000CE"


@pytest.mark.asyncio
async def test_nfo_otm_leg_renders_nothing_only_itm_leg_shows():
    """An OTM NFO leg renders NOTHING — only the ITM leg that genuinely
    needs closing appears under "Close:"."""
    positions = [
        _pos(account="ZJ6294", tradingsymbol="NIFTY26OCT22000CE", underlying="NIFTY",
             exchange="NFO", quantity=50, needs_close=True,
             close_reason="Equity ITM — must close before expiry"),
        _pos(account="ZJ6294", tradingsymbol="NIFTY26OCT23000CE", underlying="NIFTY",
             exchange="NFO", quantity=50, needs_close=False, moneyness="OTM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_preclose_alert_section({}, "NFO")
    assert text is not None
    assert "NSE/NFO:" in text
    assert "no action" not in text.lower(), "a no-action NFO leg must be entirely absent, not demoted"
    assert "netted" not in text.lower(), "NFO must never use MCX's netting wording"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 3, f"expected header + Account + Close line only, got: {lines}"
    assert "NIFTY26OCT22000CE" in lines[2]
    assert "NIFTY26OCT23000CE" not in text


@pytest.mark.asyncio
async def test_nfo_mixed_root_itm_and_otm_legs_renders_only_itm():
    """A single NFO root (NIFTY) with one ITM leg and one OTM leg must
    render only the ITM leg's symbol — the OTM sibling in the SAME root
    is just as absent as a different root's OTM leg would be."""
    positions = [
        _pos(account="ZJ6294", tradingsymbol="NIFTY26OCT22000CE", underlying="NIFTY",
             exchange="NFO", quantity=50, needs_close=True,
             close_reason="Equity ITM — must close before expiry"),
        _pos(account="ZJ6294", tradingsymbol="NIFTY26OCT30000CE", underlying="NIFTY",
             exchange="NFO", quantity=50, needs_close=False, moneyness="OTM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_preclose_alert_section({}, "NFO")
    assert text is not None
    assert "no action" not in text.lower()
    assert "netted" not in text.lower()
    assert "NIFTY26OCT30000CE" not in text
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 3, f"expected header + Account + Close line only, got: {lines}"


@pytest.mark.asyncio
async def test_preclose_alert_renders_only_the_requested_exchange():
    """2026-11-26 split regression (replaces the old `_expiry_preclose_
    exchanges`-patch-based "exchange filter" test — that internal
    re-derivation no longer exists in this function; the caller's own
    agent condition is exchange-pure now, so the renderer only ever
    gets ONE exchange name and must never let the OTHER exchange's data
    leak in, even though `ExpiryEngine.scan_positions()` always returns
    both in these fabricated fixtures). Mirrors `_v2_expiry_morning_
    preview`'s own already-proven single-exchange isolation test."""
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
        _pos(account="ZJ6294", tradingsymbol="NIFTY26OCT22000CE", underlying="NIFTY",
             exchange="NFO", quantity=50, needs_close=True,
             close_reason="Equity ITM — must close before expiry"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        nfo_text = await _v2_expiry_preclose_alert_section({}, "NFO")
        mcx_text = await _v2_expiry_preclose_alert_section({}, "MCX")

    assert nfo_text is not None and "NIFTY26OCT22000CE" in nfo_text and "GOLD" not in nfo_text, (
        f"the NFO call must render only the NFO section, got: {nfo_text}"
    )
    assert mcx_text is not None and "GOLD26OCT75000CE" in mcx_text and "NIFTY" not in mcx_text, (
        f"the MCX call must render only the MCX section, got: {mcx_text}"
    )


@pytest.mark.asyncio
async def test_multi_account_same_root_never_pooled():
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
        _pos(account="ZJ6294", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=-1, residual_qty=-1, needs_close=True,
             close_reason="MCX unhedged ITM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_preclose_alert_section({}, "MCX")
    assert text is not None
    assert text.count("Account:") == 2, \
        "two different accounts holding the same symbol must render as two separate Account blocks, never pooled"
    assert text.count("GOLD26OCT75000CE") == 2


@pytest.mark.asyncio
async def test_duplicate_symbol_same_account_deduplicated():
    """The same tradingsymbol can appear more than once for one account
    (e.g. under two different products) — it must render exactly once
    in that account's "Close:" list, never twice."""
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", product="NRML", quantity=1, residual_qty=1, needs_close=True),
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", product="MIS", quantity=1, residual_qty=1, needs_close=True),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_preclose_alert_section({}, "MCX")
    assert text is not None
    assert text.count("GOLD26OCT75000CE") == 1, \
        f"the same symbol for one account must render exactly once, got: {text}"


@pytest.mark.asyncio
async def test_unknown_moneyness_leg_never_produces_false_all_clear():
    """A leg whose moneyness could not be confirmed (UNKNOWN) must
    never be silently dropped — if it's the ONLY expiring leg for this
    exchange, the alert must say so explicitly, never the "nothing to
    close" fallback (which would read as a false all-clear on exactly
    the kind of failure this case exists to surface loudly)."""
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=False,
             moneyness="UNKNOWN"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_preclose_alert_section({}, "MCX")
    assert text is not None
    assert "nothing to close" not in text.lower(), \
        "an UNKNOWN-moneyness leg must never be rendered as a false all-clear"
    assert "Account:" in text
    assert "Check (price unavailable)" in text
    assert "GOLD26OCT75000CE" in text


@pytest.mark.asyncio
async def test_unknown_moneyness_leg_kept_distinct_from_close_list():
    """An UNKNOWN-moneyness leg alongside a genuine needs_close leg
    (same account) must render under a SEPARATE line from "Close:" —
    an unconfirmed leg must never be mistaken for one that genuinely
    needs closing."""
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True),
        _pos(account="ZG0790", tradingsymbol="SILVER26OCT95000PE", underlying="SILVER",
             exchange="MCX", quantity=-1, residual_qty=-1, needs_close=False,
             moneyness="UNKNOWN"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_preclose_alert_section({}, "MCX")
    assert text is not None
    assert "Close: GOLD26OCT75000CE" in text
    assert "Check (price unavailable): SILVER26OCT95000PE" in text
    assert text.count("Account:") == 1, "one account must get one Account block, not two"


@pytest.mark.asyncio
async def test_scan_failure_renders_explicit_degraded_note_never_blank():
    broken_engine = MagicMock()
    broken_engine.scan_positions.side_effect = RuntimeError("broker fetch failed")
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=broken_engine):
        text = await _v2_expiry_preclose_alert_section({}, "MCX")
    assert text is not None
    assert "unavailable" in text.lower()


@pytest.mark.asyncio
async def test_no_expiring_positions_states_nothing_to_close_explicitly():
    """2026-11-22 fix (operator: "if no positions to close, mention that
    clearly") — a scan finding nothing must say so explicitly, never
    return None (which used to make the caller fall back to the bare
    one-line reminder with no real information)."""
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine([])):
        text = await _v2_expiry_preclose_alert_section({}, "MCX")
    assert text is not None
    assert "nothing to close" in text.lower()
    assert "MCX" in text


# ---------------------------------------------------------------------------
# `_v2_expiry_morning_preview()` — morning-side sibling, 2026-11-22. Takes
# an explicit single `exchange` (not a re-derived set), so these tests
# confirm it renders ONLY the requested exchange even when both have
# positions, and carries the "Tentative" framing line the pre-close
# breakdown deliberately does not have. Unaffected by the 2026-11-26
# split or the 2026-11-27 account-grouping simplification beyond sharing
# the same `_v2_expiry_exchange_breakdown_or_empty()` core the pre-close
# alert function above uses.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_morning_preview_renders_only_the_requested_exchange():
    from backend.api.algo.agent_engine import _v2_expiry_morning_preview

    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26NOV75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
        _pos(account="ZJ6294", tradingsymbol="NIFTY26NOV25000CE", underlying="NIFTY",
             exchange="NFO", quantity=50, residual_qty=50, needs_close=True,
             close_reason="Equity ITM — must close before expiry"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        mcx_text = await _v2_expiry_morning_preview({}, "MCX")
        nfo_text = await _v2_expiry_morning_preview({}, "NFO")

    assert mcx_text is not None and "GOLD26NOV75000CE" in mcx_text and "NIFTY" not in mcx_text, (
        f"MCX call must render only the MCX section, got: {mcx_text}"
    )
    assert nfo_text is not None and "NIFTY26NOV25000CE" in nfo_text and "GOLD" not in nfo_text, (
        f"NFO call must render only the NFO section, got: {nfo_text}"
    )


@pytest.mark.asyncio
async def test_morning_preview_carries_tentative_framing():
    from backend.api.algo.agent_engine import _v2_expiry_morning_preview

    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26NOV75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_morning_preview({}, "MCX")

    assert text is not None
    assert text.lower().startswith("tentative"), (
        f"morning preview must lead with an explicit tentative framing so the "
        f"operator never mistakes it for a final determination, got: {text!r}"
    )
    assert "1 hour before" in text


@pytest.mark.asyncio
async def test_morning_preview_states_nothing_to_close_explicitly():
    """2026-11-22 fix (operator: "if no positions to close, mention that
    clearly") -- a scan finding nothing must say so explicitly, never
    return None."""
    from backend.api.algo.agent_engine import _v2_expiry_morning_preview

    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine([])):
        text = await _v2_expiry_morning_preview({}, "MCX")
    assert text is not None
    assert "nothing to close" in text.lower()
    assert "MCX" in text


@pytest.mark.asyncio
async def test_morning_preview_scan_failure_renders_degraded_note():
    from backend.api.algo.agent_engine import _v2_expiry_morning_preview

    engine = MagicMock()
    engine.scan_positions.side_effect = RuntimeError("broker unreachable")
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=engine):
        text = await _v2_expiry_morning_preview({}, "MCX")
    assert text is not None and "unavailable" in text.lower(), (
        "a scan failure must render an explicit degraded note, never a "
        "silent blank (which would read as a false 'all clear')"
    )


@pytest.mark.asyncio
async def test_morning_preview_states_nothing_to_close_when_only_other_exchange_has_positions():
    from backend.api.algo.agent_engine import _v2_expiry_morning_preview

    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26NOV75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_morning_preview({}, "NFO")
    assert text is not None and "nothing to close" in text.lower() and "GOLD" not in text, (
        "the NFO agent's own render call must not surface MCX's section, "
        "and must state explicitly (not return None) that its own "
        "exchange has nothing expiring"
    )
