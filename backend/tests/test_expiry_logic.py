"""
Tests for api/algo/expiry.py — option/future expiry-roll engine.
SSOT: _exp_opt_pair_valid and _best_opt_partner are the pair-selection primitives.
Perf: interval guard ensures scan doesn't run on every tick.
Stale: ExpiryEngine has an idle/scanning/closing state machine.
Reuse: pair validation logic shared between option and futures paths.
UX: opposite-sign CE+PE pair on same underlying → valid netting pair.
"""
import asyncio
from pathlib import Path
import pytest

_SRC = Path("backend/api/algo/expiry.py").read_text()


def test_exp_opt_pair_valid_exists():
    from backend.api.algo.expiry import _exp_opt_pair_valid
    assert _exp_opt_pair_valid is not None


def test_valid_pair_same_sign_opposite_type():
    """CE long + PE long (same sign, opposite types) is a valid netting pair — rules 3/4.
    same_type=False (CE vs PE), both long (same sign) → valid (CE+PE pair can net).
    """
    from backend.api.algo.expiry import _exp_opt_pair_valid
    # CE long (aq=50) + PE long (bq=50) — same_type=False (CE vs PE), same sign
    result = _exp_opt_pair_valid(same_type=False, aq=50, bq=50)
    assert result is True, (
        "CE+PE with same-sign quantities (both long or both short) must be a valid netting pair"
    )


def test_invalid_pair_same_sign():
    """Two CE positions with the same sign (both long) are NOT a valid netting pair."""
    from backend.api.algo.expiry import _exp_opt_pair_valid
    # same_type=True (both CE), aq=50 bq=50 (same sign = both long) → invalid
    result = _exp_opt_pair_valid(same_type=True, aq=50, bq=50)
    assert result is False, (
        "Two CE positions with same sign must NOT be a valid netting pair"
    )


def test_invalid_pair_same_type_same_sign():
    """same_type=True (both CE), same sign → invalid (two longs can't net each other)."""
    from backend.api.algo.expiry import _exp_opt_pair_valid
    result = _exp_opt_pair_valid(same_type=True, aq=100, bq=50)
    assert result is False, (
        "Same-type pair (two CE) with same sign must NOT be a valid netting pair"
    )


def test_best_opt_partner_exists():
    from backend.api.algo.expiry import _best_opt_partner
    assert _best_opt_partner is not None, "_best_opt_partner must exist for partner selection"


def test_expiry_engine_class_exists():
    from backend.api.algo.expiry import ExpiryEngine
    assert ExpiryEngine is not None


def test_expiry_engine_state_machine_in_source():
    assert "ExpiryState" in _SRC or "_state" in _SRC, (
        "ExpiryEngine must have a state machine (idle/scanning/closing transitions)"
    )


# `test_rescan_loop_picks_up_newly_itm_positions` removed (2026-11-24,
# audit finding #15) — exercised the now-deleted `_run_mcx_close()`
# directly. The real re-scan coverage lives in
# test_expiry_day_and_preclose_agents.py (`expiry-day-itm-auto-close`'s
# own periodic re-fire) and the end-to-end tests below that drive the
# real `_action_live_expiry_auto_close` → `scan_positions()` →
# `close_positions()` chain.


def test_index_ltp_key_maps_nifty():
    """Verify index LTP mapping constructs correct Kite quote keys.

    _fetch_underlying_ltps must map NIFTY/BANKNIFTY to NSE:NIFTY 50/NSE:NIFTY BANK
    for quote lookups, and the reverse map for result ingestion must work correctly.
    """
    try:
        from backend.api.algo.expiry import _NSE_INDEX_QUOTE_KEYS
        # Verify the forward mapping exists
        assert _NSE_INDEX_QUOTE_KEYS.get("NIFTY") == "NSE:NIFTY 50", (
            "_NSE_INDEX_QUOTE_KEYS must map 'NIFTY' to 'NSE:NIFTY 50'"
        )
        assert _NSE_INDEX_QUOTE_KEYS.get("BANKNIFTY") == "NSE:NIFTY BANK", (
            "_NSE_INDEX_QUOTE_KEYS must map 'BANKNIFTY' to 'NSE:NIFTY BANK'"
        )
    except ImportError:
        # _NSE_INDEX_QUOTE_KEYS may not exist yet if parallel agent hasn't added it
        # Skip test but verify the pattern is in the source
        assert "NSE:NIFTY 50" in _SRC, (
            "expiry.py must contain NSE:NIFTY 50 quote key mapping for index LTP"
        )

    try:
        from backend.api.algo.expiry import _KITE_KEY_TO_UNDERLYING
        # Verify the reverse mapping exists
        assert _KITE_KEY_TO_UNDERLYING.get("NIFTY 50") == "NIFTY", (
            "_KITE_KEY_TO_UNDERLYING must map 'NIFTY 50' to 'NIFTY'"
        )
        assert _KITE_KEY_TO_UNDERLYING.get("NIFTY BANK") == "BANKNIFTY", (
            "_KITE_KEY_TO_UNDERLYING must map 'NIFTY BANK' to 'BANKNIFTY'"
        )
    except ImportError:
        # _KITE_KEY_TO_UNDERLYING may not exist yet if parallel agent hasn't added it
        # Skip test but verify the pattern is in the source
        assert "NIFTY BANK" in _SRC, (
            "expiry.py must contain NIFTY BANK quote key for reverse mapping"
        )


def test_expiry_auto_close_agents_ship_inactive():
    """2026-10-29: the two per-exchange expiry auto-close built-in agents
    (expiry-day-equity-itm-auto-close/expiry-day-commodity-itm-auto-close,
    both status='inactive') were retired and consolidated into the single
    expiry-day-itm-auto-close, which covers both exchanges in one agent.
    It deliberately ships status='active' — its safety net is the
    order-level hold-by-default in ExpiryEngine.close_positions()
    (HoldCategory.EXPIRY_CLOSE), not agent deactivation. See
    test_expiry_day_and_preclose_agents.py::TestExpiryDayItmAutoCloseAgent
    and test_seed_ships_inactive_guard.py for the current coverage of
    both halves of this invariant.
    """
    from backend.api.algo.agent_engine import BUILTIN_AGENTS
    slugs = {a["slug"]: a for a in BUILTIN_AGENTS}
    assert "expiry-day-equity-itm-auto-close" not in slugs, "retired slug must be gone"
    assert "expiry-day-commodity-itm-auto-close" not in slugs, "retired slug must be gone"
    assert slugs["expiry-day-itm-auto-close"]["status"] == "active"


# ---------------------------------------------------------------------------
# 2026-10-29: the fire_at_time/before_cutoff() coordination this section
# used to test no longer applies. expiry-day-itm-auto-close has no
# fire_at_time at all — it fires on a flat `is_expiry_preclose_close_window`
# condition (25 min before whichever exchange's own close), and
# _action_live_expiry_auto_close no longer calls before_cutoff() at all
# (removed 2026-10-29 — the condition already gates timing, so a second,
# independently-configured timing gate inside the action would just be a
# second place the same decision could disagree). See
# test_expiry_day_and_preclose_agents.py for the current condition-shape
# coverage, and the comment above _action_live_expiry_auto_close in
# actions_live.py for the before_cutoff removal rationale.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_auto_close_action_scans_unconditionally_no_cutoff_gate():
    """_action_live_expiry_auto_close must always call scan_positions()
    when invoked — there is no before_cutoff()-style internal timing gate
    to defer it (that responsibility moved entirely to the agent's own
    condition, evaluated before this action is ever dispatched)."""
    from unittest.mock import MagicMock, patch
    from backend.api.algo.actions_live import _action_live_expiry_auto_close

    agent = MagicMock()
    agent.slug = "expiry-day-itm-auto-close"

    mock_engine = MagicMock()
    mock_engine.scan_positions.return_value = []
    mock_engine.state.closed = []
    mock_engine.state.failed = []
    mock_engine.state.total_slippage = 0.0

    # 2026-10-29 audit fix: the action re-derives which exchange(s) are
    # actually within their own pre-close window (an empty context has
    # none), so this must be patched for scan_positions() to be reached
    # at all — see test_expiry_position_breakdown.py's identical pattern.
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=mock_engine), \
         patch(
             "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window."
             "_expiry_preclose_exchanges",
             return_value={"MCX", "NFO"},
         ):
        await _action_live_expiry_auto_close(agent, {}, {})

    mock_engine.scan_positions.assert_called_once()


@pytest.mark.asyncio
async def test_expiry_auto_close_honors_explicit_exchange_param():
    """Audit finding #4 (2026-11-24): an explicit params['exchange']
    must narrow which exchange(s) get closed, even when the OTHER
    exchange is also genuinely within its own pre-close window right
    now -- closing the live/paper/sim divergence (paper/sim already
    honored this param)."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from backend.api.algo.actions_live import _action_live_expiry_auto_close

    agent = MagicMock()
    agent.slug = "expiry-day-itm-auto-close"
    agent.id = 42

    mock_engine = MagicMock()
    mock_engine.scan_positions.return_value = [
        MagicMock(exchange="MCX"), MagicMock(exchange="NFO"),
    ]
    mock_engine.close_positions = AsyncMock()
    mock_engine.state.closed = []
    mock_engine.state.failed = []
    mock_engine.state.total_slippage = 0.0

    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=mock_engine), \
         patch(
             "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window."
             "_expiry_preclose_exchanges",
             return_value={"MCX", "NFO"},
         ):
        await _action_live_expiry_auto_close(agent, {}, {"exchange": "NFO"})

    mock_engine.close_positions.assert_awaited_once()
    closed_positions = mock_engine.close_positions.await_args.args[0]
    assert len(closed_positions) == 1
    assert closed_positions[0].exchange == "NFO"


@pytest.mark.asyncio
async def test_expiry_auto_close_no_exchange_param_keeps_every_in_window_exchange():
    """No `exchange` param (the default agent's own shape) -- behaviour
    unchanged from before this fix: every exchange currently in-window
    is processed together."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from backend.api.algo.actions_live import _action_live_expiry_auto_close

    agent = MagicMock()
    agent.slug = "expiry-day-itm-auto-close"
    agent.id = 42

    mock_engine = MagicMock()
    mock_engine.scan_positions.return_value = [
        MagicMock(exchange="MCX"), MagicMock(exchange="NFO"),
    ]
    mock_engine.close_positions = AsyncMock()
    mock_engine.state.closed = []
    mock_engine.state.failed = []
    mock_engine.state.total_slippage = 0.0

    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=mock_engine), \
         patch(
             "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window."
             "_expiry_preclose_exchanges",
             return_value={"MCX", "NFO"},
         ):
        await _action_live_expiry_auto_close(agent, {}, {})

    closed_positions = mock_engine.close_positions.await_args.args[0]
    assert {p.exchange for p in closed_positions} == {"MCX", "NFO"}


# `test_nfo_rescan_loop_catches_newly_itm_in_run` removed (2026-11-24,
# audit finding #15) — exercised the now-deleted `run()` directly (and
# its `_run_nfo_close()`/`_run_mcx_close()` helpers). Real re-scan
# coverage for a leg that turns ITM intraday now lives in
# test_expiry_day_and_preclose_agents.py, via `expiry-day-itm-auto-
# close`'s own periodic re-fire (see the cooldown-retime entry in
# agent_engine.py for why 30 min covers this gap).


def test_nse_index_quote_keys_mapping():
    """_NSE_INDEX_QUOTE_KEYS must map NIFTY → NSE:NIFTY 50 and BANKNIFTY → NSE:NIFTY BANK."""
    from backend.api.algo.expiry import _NSE_INDEX_QUOTE_KEYS, _KITE_KEY_TO_UNDERLYING
    assert _NSE_INDEX_QUOTE_KEYS["NIFTY"] == "NSE:NIFTY 50"
    assert _NSE_INDEX_QUOTE_KEYS["BANKNIFTY"] == "NSE:NIFTY BANK"
    assert _NSE_INDEX_QUOTE_KEYS["FINNIFTY"] == "NSE:NIFTY FIN SERVICE"
    assert _NSE_INDEX_QUOTE_KEYS["MIDCPNIFTY"] == "NSE:NIFTY MIDCAP SELECT"
    assert _KITE_KEY_TO_UNDERLYING["NIFTY 50"] == "NIFTY"
    assert _KITE_KEY_TO_UNDERLYING["NIFTY BANK"] == "BANKNIFTY"


def test_fetch_underlying_ltps_uses_correct_nifty_key():
    """_fetch_underlying_ltps must request NSE:NIFTY 50 (not NSE:NIFTY) for NIFTY underlying."""
    from unittest.mock import MagicMock, patch
    from backend.api.algo.expiry import ExpiryEngine, OptionPosition
    from datetime import date

    engine = ExpiryEngine()
    pos = OptionPosition(
        account="acc1",
        tradingsymbol="NIFTY24JUL25000CE",
        exchange="NFO",
        instrument_type="CE",
        underlying="NIFTY",
        strike=25000.0,
        expiry=date.today(),
        quantity=50,
        product="NRML",
    )

    mock_broker = MagicMock()
    mock_broker.ltp.return_value = {"NSE:NIFTY 50": {"last_price": 24900.0}}

    # all_brokers is imported inside the function body, so patch at registry level
    with patch("backend.brokers.registry.all_brokers", return_value=[mock_broker]):
        result = engine._fetch_underlying_ltps([pos])

    args = mock_broker.ltp.call_args[0][0]
    assert "NSE:NIFTY 50" in args, (
        f"_fetch_underlying_ltps must request NSE:NIFTY 50 for NIFTY, got {args}"
    )
    assert result.get("NIFTY") == 24900.0, (
        f"Result must map NIFTY 50 back to NIFTY, got {result}"
    )


def test_compute_theta_mcx_dispatches_black_76():
    """`_compute_theta` prices MCX commodity positions' theta via
    `greeks_76()` (Black-76 — `underlying_ltp` is the matching futures
    contract price for MCX, not a cash spot), not plain `greeks()`.
    Spies on the real `derivatives` module functions that `_compute_theta`'s
    local import binds at call time, so a regression reverting the branch
    to always-BS fails this test."""
    from datetime import date, timedelta
    from unittest.mock import patch
    from backend.api.algo.expiry import ExpiryEngine, OptionPosition
    from backend.api.algo import derivatives as deriv_mod

    engine = ExpiryEngine()
    pos = OptionPosition(
        account="test_account",
        tradingsymbol="CRUDEOIL26NOV5500CE",
        exchange="MCX",
        instrument_type="CE",
        underlying="CRUDEOIL",
        strike=5500.0,
        expiry=date.today() + timedelta(days=30),
        quantity=1,
        product="NRML",
        underlying_ltp=5500.0,
    )

    with patch.object(deriv_mod, "greeks_76", wraps=deriv_mod.greeks_76) as spy_76, \
         patch.object(deriv_mod, "greeks", wraps=deriv_mod.greeks) as spy_bs:
        theta = engine._compute_theta(pos)

    assert spy_76.called, "MCX position theta must dispatch through greeks_76"
    assert not spy_bs.called, "MCX position theta must NOT fall through to plain BS greeks()"
    assert theta != 0.0


def test_compute_theta_nfo_stays_on_plain_bs():
    """NFO (equity/index) positions must keep using plain `greeks()` for
    theta — the is_mcx branch keys off `pos.exchange == 'MCX'`, so a
    non-MCX exchange must never dispatch through greeks_76."""
    from datetime import date, timedelta
    from unittest.mock import patch
    from backend.api.algo.expiry import ExpiryEngine, OptionPosition
    from backend.api.algo import derivatives as deriv_mod

    engine = ExpiryEngine()
    pos = OptionPosition(
        account="test_account",
        tradingsymbol="NIFTY26NOV25000CE",
        exchange="NFO",
        instrument_type="CE",
        underlying="NIFTY",
        strike=25000.0,
        expiry=date.today() + timedelta(days=30),
        quantity=50,
        product="NRML",
        underlying_ltp=25000.0,
    )

    with patch.object(deriv_mod, "greeks_76", wraps=deriv_mod.greeks_76) as spy_76, \
         patch.object(deriv_mod, "greeks", wraps=deriv_mod.greeks) as spy_bs:
        theta = engine._compute_theta(pos)

    assert spy_bs.called, "NFO position theta must use plain BS greeks()"
    assert not spy_76.called, "NFO position theta must NOT dispatch through greeks_76"
    assert theta != 0.0


@pytest.mark.asyncio
async def test_close_positions_records_held_order_with_expiry_close_category():
    """close_positions() must route through the generic record_held_order
    (order_hold_gate.py) with HoldCategory.EXPIRY_CLOSE when holds are on —
    regression guard for the record_held_close -> record_held_order
    call-site migration."""
    from datetime import date
    from unittest.mock import AsyncMock, patch
    from backend.api.algo.expiry import ExpiryEngine, OptionPosition
    from backend.api.algo.order_hold import HoldCategory

    # 2026-11-23 fix (Part C): close_positions() now refuses to stage a
    # HELD row when the engine has no agent_id (a hold with no agent_id
    # can never be auto-released). A real agent_id is required here so
    # this test still exercises the record_held_order call it asserts.
    engine = ExpiryEngine(agent_id=42)
    pos = OptionPosition(
        account="ZG0790",
        tradingsymbol="CRUDEOIL26OCT8600CE",
        exchange="MCX",
        instrument_type="CE",
        underlying="CRUDEOIL",
        strike=8600.0,
        expiry=date.today(),
        quantity=1,
        product="NRML",
        residual_qty=1,
    )

    mock_record = AsyncMock(return_value=123)

    async def _not_in_flight(account, symbol, exchange, side):
        return False

    with patch("backend.api.algo.expiry._exp_leg_in_flight_this_session", new=_not_in_flight), \
         patch("backend.api.algo.expiry.expiry_close_held", return_value=True), \
         patch("backend.api.algo.actions_live._al_agent_close_recently_cancelled_this_session",
               new=AsyncMock(return_value=False)), \
         patch("backend.api.algo.expiry.record_held_order", mock_record):
        await engine.close_positions([pos])

    mock_record.assert_called_once()
    args, kwargs = mock_record.call_args
    assert args[0] is HoldCategory.EXPIRY_CLOSE
    assert kwargs["account"] == "ZG0790"
    assert kwargs["symbol"] == "CRUDEOIL26OCT8600CE"
    assert kwargs["exchange"] == "MCX"
    assert kwargs["side"] == "SELL"
    assert kwargs["qty"] == 1
    assert kwargs["product"] == "NRML"


# ── 2026-11 classifier re-base: scan_positions() vs classify_from_context() ─

def test_scan_positions_and_classify_from_context_agree_on_needs_close():
    """SSOT parity — the governing invariant behind the 2026-11 operator
    decision to re-base `expiry-mcx-risk-alert`/`expiry-nfo-risk-alert`
    onto `ExpiryEngine`'s real classifier: `scan_positions()` (the
    broker-fetched path `expiry-day-preclose-alert`/`expiry-day-itm-
    auto-close` use) and `classify_from_context()` (the ctx-based
    adapter the two risk-alert condition RESOLVERS now use, since they
    cannot make a live broker call every poll cycle) must agree exactly
    on `needs_close`, given the SAME underlying book.

    Covers both canonical disagreement cases the re-base closes: a
    synthetic long-CE/short-PE combo (no valid netting pair — flagged)
    and a lone unhedged ITM leg (no partner at all — flagged)."""
    from datetime import date as _date
    from unittest.mock import MagicMock, patch

    from backend.api.algo.expiry import ExpiryEngine, OptionPosition, classify_from_context

    today = _date(2025, 5, 29)
    spot_prices = {"GOLDM": 76000.0, "GOLD": 76000.0}

    # Hand-built OptionPositions — what the REAL `_fetch_option_positions`
    # would construct from Kite's instrument master for this exact book.
    fetched_opts = [
        OptionPosition(account="A1", tradingsymbol="GOLDM25MAY75000CE", exchange="MCX",
                        instrument_type="CE", underlying="GOLDM", strike=75000.0,
                        expiry=today, quantity=1, product="NRML"),
        OptionPosition(account="A1", tradingsymbol="GOLD25MAY75000CE", exchange="MCX",
                        instrument_type="CE", underlying="GOLD", strike=75000.0,
                        expiry=today, quantity=1, product="NRML"),
        OptionPosition(account="A1", tradingsymbol="GOLD25MAY75000PE", exchange="MCX",
                        instrument_type="PE", underlying="GOLD", strike=75000.0,
                        expiry=today, quantity=-1, product="NRML"),
    ]

    engine = ExpiryEngine()
    with patch.object(engine, "_fetch_option_positions", return_value=fetched_opts), \
         patch.object(engine, "_fetch_future_positions", return_value=[]), \
         patch.object(engine, "_fetch_underlying_ltps", return_value=spot_prices), \
         patch("backend.api.algo.expiry.timestamp_indian") as mock_ts:
        mock_ts.return_value.date.return_value = today
        to_close = engine.scan_positions()
    scan_needs_close = {p.tradingsymbol for p in to_close}

    # Same raw rows, as a condition resolver would see them in
    # ctx.position_rows (no broker/instrument-master lookup available).
    ctx_rows = [
        {"account": "A1", "tradingsymbol": "GOLDM25MAY75000CE", "exchange": "MCX", "quantity": 1},
        {"account": "A1", "tradingsymbol": "GOLD25MAY75000CE", "exchange": "MCX", "quantity": 1},
        {"account": "A1", "tradingsymbol": "GOLD25MAY75000PE", "exchange": "MCX", "quantity": -1},
    ]
    classified = classify_from_context(ctx_rows, ctx_rows, spot_prices, exchange_filter="MCX")
    ctx_needs_close = {c.tradingsymbol for c in classified if c.needs_close}

    assert scan_needs_close == {"GOLDM25MAY75000CE", "GOLD25MAY75000CE"}, scan_needs_close
    assert ctx_needs_close == scan_needs_close, (
        f"classify_from_context() disagreed with scan_positions(): "
        f"scan={scan_needs_close} ctx={ctx_needs_close}")


def test_scan_positions_and_classify_from_context_agree_on_genuine_hedge():
    """Mirror case — a genuinely rule-valid hedge (same strike, same
    type, opposite sign) must net to zero residual under BOTH paths."""
    from datetime import date as _date
    from unittest.mock import patch

    from backend.api.algo.expiry import ExpiryEngine, OptionPosition, classify_from_context

    today = _date(2025, 5, 29)
    spot_prices = {"GOLD": 76000.0}

    fetched_opts = [
        OptionPosition(account="A1", tradingsymbol="GOLD25MAY75000CE", exchange="MCX",
                        instrument_type="CE", underlying="GOLD", strike=75000.0,
                        expiry=today, quantity=1, product="NRML"),
        OptionPosition(account="A1", tradingsymbol="GOLD25MAY75000CE", exchange="MCX",
                        instrument_type="CE", underlying="GOLD", strike=75000.0,
                        expiry=today, quantity=-1, product="NRML"),
    ]

    engine = ExpiryEngine()
    with patch.object(engine, "_fetch_option_positions", return_value=fetched_opts), \
         patch.object(engine, "_fetch_future_positions", return_value=[]), \
         patch.object(engine, "_fetch_underlying_ltps", return_value=spot_prices), \
         patch("backend.api.algo.expiry.timestamp_indian") as mock_ts:
        mock_ts.return_value.date.return_value = today
        to_close = engine.scan_positions()
    scan_needs_close = {p.tradingsymbol for p in to_close}

    ctx_rows = [
        {"account": "A1", "tradingsymbol": "GOLD25MAY75000CE", "exchange": "MCX", "quantity": 1},
        {"account": "A1", "tradingsymbol": "GOLD25MAY75000CE", "exchange": "MCX", "quantity": -1},
    ]
    classified = classify_from_context(ctx_rows, ctx_rows, spot_prices, exchange_filter="MCX")
    ctx_needs_close = {c.tradingsymbol for c in classified if c.needs_close}

    assert scan_needs_close == set()
    assert ctx_needs_close == set()


# ---------------------------------------------------------------------------
# 2026-11-22 retime (operator: "unnetted in the money positions closed by
# the agents one hour before market close... order will be held until I
# release them") — end-to-end through the REAL action + REAL
# ExpiryEngine.scan_positions()/close_positions(). Only the broker-touching
# fetch methods are mocked; the real 4-rule classification, the real hold
# gate (expiry_close_held()), and the real per-leg dedup guard inside
# record_held_order() all run unmocked. Confirms the operator's explicit
# requirement end to end: a genuinely unnetted ITM MCX position gets HELD,
# never chased straight to the broker, while hold.expiry_close_released
# stays False (the live prod/dev default).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expiry_auto_close_holds_not_chases_real_path_at_60min_window():
    from unittest.mock import AsyncMock, MagicMock, patch
    from datetime import date, datetime, timezone
    from backend.api.algo.expiry import ExpiryEngine, OptionPosition
    from backend.api.algo.actions_live import _action_live_expiry_auto_close

    today = date(2026, 11, 26)
    frozen_now = datetime(2026, 11, 26, 16, 0, 0, tzinfo=timezone.utc)  # ~21:30 IST
    lone_itm_call = OptionPosition(
        account="ZG0790",
        tradingsymbol="CRUDEOIL26NOV5500CE",
        exchange="MCX",
        instrument_type="CE",
        underlying="CRUDEOIL",
        strike=5500.0,
        expiry=today,
        quantity=1,
        product="NRML",
        ltp=120.0,
    )

    agent = MagicMock()
    agent.slug = "expiry-day-itm-auto-close"
    agent.id = 42

    async def _not_in_flight(account, symbol, exchange, side):
        return False

    with patch("backend.api.algo.expiry.timestamp_indian", return_value=frozen_now), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_option_positions",
               return_value=[lone_itm_call]), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_future_positions",
               return_value=[]), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_underlying_ltps",
               return_value={"CRUDEOIL": 6200.0}), \
         patch("backend.api.algo.expiry._exp_leg_in_flight_this_session", new=_not_in_flight), \
         patch("backend.api.algo.expiry.expiry_close_held", return_value=True) as held_gate, \
         patch("backend.api.algo.expiry.record_held_order",
               new=AsyncMock(return_value=101)) as held_mock, \
         patch("backend.api.algo.expiry.chase_order",
               new=AsyncMock()) as chase_mock, \
         patch(
             "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window."
             "_expiry_preclose_exchanges",
             return_value={"MCX"},
         ):
        await _action_live_expiry_auto_close(agent, {}, {})

    held_gate.assert_called()
    held_mock.assert_awaited_once()
    kwargs = held_mock.await_args.kwargs
    assert kwargs["account"] == "ZG0790"
    assert kwargs["symbol"] == "CRUDEOIL26NOV5500CE"
    assert kwargs["exchange"] == "MCX"
    assert kwargs["side"] == "SELL"  # closing a long CALL means selling it
    assert kwargs["qty"] == 1
    chase_mock.assert_not_awaited(), \
        "hold.expiry_close_released=False (hold.expiry_close_held()->True) " \
        "must stage a HELD row, never place a real chase/broker order"


@pytest.mark.asyncio
async def test_expiry_auto_close_chases_when_operator_releases_the_hold():
    """Sanity check for the OTHER side of the same real path: once the
    operator flips hold.expiry_close_released=True (expiry_close_held()
    returns False), the identical unnetted-ITM leg must route to the real
    chase_order() path instead of record_held_order() — proves the test
    above is actually exercising the branch it claims to, not a tautology."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from datetime import date, datetime, timezone
    from backend.api.algo.expiry import ExpiryEngine, OptionPosition, ChaseResult, ChaseStatus
    from backend.api.algo.actions_live import _action_live_expiry_auto_close

    today = date(2026, 11, 26)
    frozen_now = datetime(2026, 11, 26, 16, 0, 0, tzinfo=timezone.utc)  # ~21:30 IST
    lone_itm_call = OptionPosition(
        account="ZG0790",
        tradingsymbol="CRUDEOIL26NOV5500CE",
        exchange="MCX",
        instrument_type="CE",
        underlying="CRUDEOIL",
        strike=5500.0,
        expiry=today,
        quantity=1,
        product="NRML",
        ltp=120.0,
    )

    agent = MagicMock()
    agent.slug = "expiry-day-itm-auto-close"
    agent.id = 42

    fake_result = ChaseResult(
        account="ZG0790", symbol="CRUDEOIL26NOV5500CE",
        transaction_type="SELL", quantity=1, status=ChaseStatus.FILLED,
    )

    async def _not_in_flight(account, symbol, exchange, side):
        return False

    with patch("backend.api.algo.expiry.timestamp_indian", return_value=frozen_now), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_option_positions",
               return_value=[lone_itm_call]), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_future_positions",
               return_value=[]), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_underlying_ltps",
               return_value={"CRUDEOIL": 6200.0}), \
         patch("backend.api.algo.expiry._exp_leg_in_flight_this_session", new=_not_in_flight), \
         patch("backend.api.algo.expiry.expiry_close_held", return_value=False), \
         patch("backend.api.algo.expiry.record_held_order",
               new=AsyncMock()) as held_mock, \
         patch("backend.api.algo.expiry.chase_order",
               new=AsyncMock(return_value=fake_result)) as chase_mock, \
         patch(
             "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window."
             "_expiry_preclose_exchanges",
             return_value={"MCX"},
         ):
        await _action_live_expiry_auto_close(agent, {}, {})

    chase_mock.assert_awaited_once()
    held_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_auto_close_fails_loud_on_unresolved_spot_instead_of_false_all_clear():
    """Audit finding #11 (2026-11-24): a failed underlying-LTP fetch
    leaves every expiring leg's moneyness UNKNOWN (never flagged
    needs_close), so `to_close` reads empty identically whether the
    book is genuinely clean or the fetch just failed. The action must
    log CRITICAL, not a routine INFO "nothing to close", when that
    empty result is explained by an unresolved spot price."""
    from unittest.mock import MagicMock, patch
    from datetime import date, datetime, timezone
    from backend.api.algo.expiry import OptionPosition
    from backend.api.algo.actions_live import _action_live_expiry_auto_close

    today = date(2026, 11, 26)
    frozen_now = datetime(2026, 11, 26, 16, 0, 0, tzinfo=timezone.utc)
    lone_leg = OptionPosition(
        account="ZG0790", tradingsymbol="CRUDEOIL26NOV5500CE", exchange="MCX",
        instrument_type="CE", underlying="CRUDEOIL", strike=5500.0,
        expiry=today, quantity=1, product="NRML", ltp=120.0,
    )

    agent = MagicMock()
    agent.slug = "expiry-day-itm-auto-close"
    agent.id = 42

    with patch("backend.api.algo.expiry.timestamp_indian", return_value=frozen_now), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_option_positions",
               return_value=[lone_leg]), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_future_positions",
               return_value=[]), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_underlying_ltps",
               return_value={}), \
         patch("backend.api.algo.actions_live.logger") as mock_logger, \
         patch(
             "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window."
             "_expiry_preclose_exchanges",
             return_value={"MCX"},
         ):
        await _action_live_expiry_auto_close(agent, {}, {})

    assert mock_logger.critical.called, \
        "an unresolved-spot-price empty result must log CRITICAL, not a silent all-clear"
    assert not mock_logger.info.called or all(
        "no positions need closing" not in str(c) for c in mock_logger.info.call_args_list
    )


@pytest.mark.asyncio
async def test_expiry_auto_close_genuinely_clean_scan_stays_info_level():
    """Sanity check for the other side: a genuinely empty expiry day
    (nothing expiring at all, real LTP fetch untouched) must NOT
    escalate to CRITICAL — only an UNKNOWN-moneyness leg should."""
    from unittest.mock import MagicMock, patch
    from datetime import datetime, timezone
    from backend.api.algo.actions_live import _action_live_expiry_auto_close

    frozen_now = datetime(2026, 11, 26, 16, 0, 0, tzinfo=timezone.utc)

    agent = MagicMock()
    agent.slug = "expiry-day-itm-auto-close"
    agent.id = 42

    with patch("backend.api.algo.expiry.timestamp_indian", return_value=frozen_now), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_option_positions",
               return_value=[]), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_future_positions",
               return_value=[]), \
         patch("backend.api.algo.actions_live.logger") as mock_logger, \
         patch(
             "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window."
             "_expiry_preclose_exchanges",
             return_value={"MCX"},
         ):
        await _action_live_expiry_auto_close(agent, {}, {})

    mock_logger.critical.assert_not_called()


# ---------------------------------------------------------------------------
# 2026-11-22 double-close fix — a leg released from HELD and still chasing
# could get a SECOND HELD row from the next re-scan (expiry-day-itm-auto-
# close's own 30-min periodic re-fire, or the legacy NFO re-scan loop),
# because record_held_order()'s dedup guard matches status=='HELD' only.
# release_held_order() sets a released row to status='OPEN' while it
# chases, so the dedup guard alone could not see it. Releasing the second
# row too would place a second real close and flip the position.
# _exp_leg_in_flight_this_session() closes this; these tests pin it both
# directly and through the real close_positions() method.
# ---------------------------------------------------------------------------

def _mock_db_session_with_rows(rows):
    from unittest.mock import AsyncMock, MagicMock
    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    scalars = MagicMock()
    scalars.all.return_value = rows
    result = MagicMock()
    result.scalars.return_value = scalars
    mock_session.execute = AsyncMock(return_value=result)
    return mock_session


class TestExpLegInFlightThisSession:
    def test_no_rows_returns_false(self, monkeypatch):
        from backend.api.algo.expiry import _exp_leg_in_flight_this_session

        mock_session = _mock_db_session_with_rows([])
        monkeypatch.setattr("backend.api.database.async_session", lambda: mock_session)

        result = asyncio.run(_exp_leg_in_flight_this_session(
            "ZG0790", "CRUDEOIL26NOV5500CE", "MCX", "SELL",
        ))
        assert result is False

    def test_held_row_from_today_returns_true(self, monkeypatch):
        from types import SimpleNamespace
        from datetime import datetime, timezone
        from backend.api.algo.expiry import _exp_leg_in_flight_this_session

        row = SimpleNamespace(status="HELD", created_at=datetime.now(timezone.utc))
        mock_session = _mock_db_session_with_rows([row])
        monkeypatch.setattr("backend.api.database.async_session", lambda: mock_session)

        result = asyncio.run(_exp_leg_in_flight_this_session(
            "ZG0790", "CRUDEOIL26NOV5500CE", "MCX", "SELL",
        ))
        assert result is True

    def test_open_row_from_today_returns_true(self, monkeypatch):
        """THE release-and-still-chasing scenario — release_held_order()
        sets status='OPEN'. This is the exact gap record_held_order()'s
        own dedup guard (status=='HELD' only) could never see."""
        from types import SimpleNamespace
        from datetime import datetime, timezone
        from backend.api.algo.expiry import _exp_leg_in_flight_this_session

        row = SimpleNamespace(status="OPEN", created_at=datetime.now(timezone.utc))
        mock_session = _mock_db_session_with_rows([row])
        monkeypatch.setattr("backend.api.database.async_session", lambda: mock_session)

        result = asyncio.run(_exp_leg_in_flight_this_session(
            "ZG0790", "CRUDEOIL26NOV5500CE", "MCX", "SELL",
        ))
        assert result is True

    def test_cancel_failed_row_from_today_returns_true(self, monkeypatch):
        from types import SimpleNamespace
        from datetime import datetime, timezone
        from backend.api.algo.expiry import _exp_leg_in_flight_this_session

        row = SimpleNamespace(status="CANCEL_FAILED", created_at=datetime.now(timezone.utc))
        mock_session = _mock_db_session_with_rows([row])
        monkeypatch.setattr("backend.api.database.async_session", lambda: mock_session)

        result = asyncio.run(_exp_leg_in_flight_this_session(
            "ZG0790", "CRUDEOIL26NOV5500CE", "MCX", "SELL",
        ))
        assert result is True

    def test_row_from_a_prior_session_returns_false(self, monkeypatch):
        """A HELD/OPEN/CANCEL_FAILED row from a DIFFERENT trading session
        (e.g. a stale row that was never cleaned up) must not block a
        genuinely new close this session."""
        from types import SimpleNamespace
        from datetime import datetime, timedelta, timezone
        from backend.api.algo.expiry import _exp_leg_in_flight_this_session

        stale = datetime.now(timezone.utc) - timedelta(days=5)
        row = SimpleNamespace(status="OPEN", created_at=stale)
        mock_session = _mock_db_session_with_rows([row])
        monkeypatch.setattr("backend.api.database.async_session", lambda: mock_session)

        result = asyncio.run(_exp_leg_in_flight_this_session(
            "ZG0790", "CRUDEOIL26NOV5500CE", "MCX", "SELL",
        ))
        assert result is False

    def test_db_error_fails_toward_skipping_true(self, monkeypatch):
        """Opposite failure direction from the sibling cancel-suppression
        check in actions_live.py (which fails open/False, since it only
        gates a re-ALERT) — this one guards a second REAL order, so a
        lookup failure must skip (True), not risk a duplicate."""
        from backend.api.algo.expiry import _exp_leg_in_flight_this_session

        def _boom():
            raise RuntimeError("db unreachable")
        monkeypatch.setattr("backend.api.database.async_session", _boom)

        result = asyncio.run(_exp_leg_in_flight_this_session(
            "ZG0790", "CRUDEOIL26NOV5500CE", "MCX", "SELL",
        ))
        assert result is True


class TestClosePositionsSkipsInFlightLegs:
    @pytest.mark.asyncio
    async def test_in_flight_leg_is_never_restaged_or_rechased(self, monkeypatch):
        """End-to-end through the real close_positions(): one leg is
        already in-flight this session (simulating a released-and-
        still-chasing row), a second leg genuinely needs a fresh close.
        Only the second leg may reach record_held_order()."""
        from unittest.mock import AsyncMock, patch
        from datetime import date
        from backend.api.algo.expiry import ExpiryEngine, OptionPosition

        in_flight_leg = OptionPosition(
            account="ZG0790", tradingsymbol="CRUDEOIL26NOV5500CE",
            exchange="MCX", instrument_type="CE", underlying="CRUDEOIL",
            strike=5500.0, expiry=date.today(), quantity=1, product="NRML",
            ltp=120.0, residual_qty=1,
        )
        fresh_leg = OptionPosition(
            account="ZG0790", tradingsymbol="CRUDEOIL26NOV5600CE",
            exchange="MCX", instrument_type="CE", underlying="CRUDEOIL",
            strike=5600.0, expiry=date.today(), quantity=1, product="NRML",
            ltp=80.0, residual_qty=1,
        )

        async def _fake_in_flight(account, symbol, exchange, side):
            return symbol == "CRUDEOIL26NOV5500CE"

        # 2026-11-23 fix (Part C): a real agent_id is required so
        # close_positions() doesn't refuse to stage the fresh leg's
        # HELD row (a hold with no agent_id can never be auto-released).
        engine = ExpiryEngine(agent_id=42)
        with patch("backend.api.algo.expiry._exp_leg_in_flight_this_session",
                   new=_fake_in_flight), \
             patch("backend.api.algo.expiry.expiry_close_held", return_value=True), \
             patch("backend.api.algo.actions_live._al_agent_close_recently_cancelled_this_session",
                   new=AsyncMock(return_value=False)), \
             patch("backend.api.algo.expiry.record_held_order",
                   new=AsyncMock(return_value=101)) as held_mock, \
             patch("backend.api.algo.expiry.chase_order",
                   new=AsyncMock()) as chase_mock:
            await engine.close_positions([in_flight_leg, fresh_leg])

        held_mock.assert_awaited_once()
        kwargs = held_mock.await_args.kwargs
        assert kwargs["symbol"] == "CRUDEOIL26NOV5600CE", (
            "only the leg with no in-flight order this session may be "
            "staged — the in-flight leg must be skipped entirely"
        )
        chase_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_in_flight_leg_is_skipped_even_when_hold_is_released(self, monkeypatch):
        """The in-flight check runs BEFORE the expiry_close_held() branch
        — an in-flight leg must be skipped regardless of whether hold is
        currently on or off, because a live chase for it may already be
        running from an earlier release."""
        from unittest.mock import AsyncMock, patch
        from datetime import date
        from backend.api.algo.expiry import ExpiryEngine, OptionPosition

        leg = OptionPosition(
            account="ZG0790", tradingsymbol="CRUDEOIL26NOV5500CE",
            exchange="MCX", instrument_type="CE", underlying="CRUDEOIL",
            strike=5500.0, expiry=date.today(), quantity=1, product="NRML",
            ltp=120.0, residual_qty=1,
        )

        async def _always_in_flight(account, symbol, exchange, side):
            return True

        engine = ExpiryEngine()
        with patch("backend.api.algo.expiry._exp_leg_in_flight_this_session",
                   new=_always_in_flight), \
             patch("backend.api.algo.expiry.expiry_close_held", return_value=False), \
             patch("backend.api.algo.expiry.record_held_order",
                   new=AsyncMock()) as held_mock, \
             patch("backend.api.algo.expiry.chase_order",
                   new=AsyncMock()) as chase_mock:
            await engine.close_positions([leg])

        held_mock.assert_not_awaited()
        chase_mock.assert_not_awaited()
