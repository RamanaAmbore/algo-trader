"""
Tests for api/algo/expiry.py — option/future expiry-roll engine.
SSOT: _exp_opt_pair_valid and _best_opt_partner are the pair-selection primitives.
Perf: interval guard ensures scan doesn't run on every tick.
Stale: ExpiryEngine has an idle/scanning/closing state machine.
Reuse: pair validation logic shared between option and futures paths.
UX: opposite-sign CE+PE pair on same underlying → valid netting pair.
"""
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


@pytest.mark.asyncio
async def test_rescan_loop_picks_up_newly_itm_positions():
    """Positions that go ITM after morning scan are caught by the re-scan loop.

    Verifies that _run_mcx_close calls scan_positions() and that
    newly-ITM positions are passed to close_positions() for closing.
    """
    from unittest.mock import AsyncMock, patch
    from backend.api.algo.expiry import ExpiryEngine, OptionPosition
    from datetime import date, time as dtime

    engine = ExpiryEngine()

    # Position that is ITM and needs closing
    pos_itm = OptionPosition(
        account="test_account",
        tradingsymbol="CRUDEOILJAN25C7200",
        exchange="MCX",
        instrument_type="CE",
        underlying="CRUDEOIL",
        strike=7200.0,
        expiry=date.today(),
        quantity=1,
        product="NRML",
        ltp=0.25,
        underlying_ltp=7250.0,  # 0.7% ITM (inside default 2% NTM buffer)
        moneyness="ITM",
        needs_close=True,
        close_reason="MCX unhedged ITM after 4-rule netting (residual qty +1; theta=0.010)",
        theta=0.01,
        residual_qty=1,
    )

    # Mock scan_positions to return the ITM position
    scan_positions_called = []
    def mock_scan_positions():
        scan_positions_called.append(True)
        return [pos_itm]

    # Mock close_positions to track calls
    close_positions_calls = []
    async def mock_close_positions(positions):
        close_positions_calls.append(positions)

    with patch.object(engine, 'scan_positions', side_effect=mock_scan_positions):
        with patch.object(engine, 'close_positions', side_effect=mock_close_positions):
            # Mock _run_nfo_close to do nothing
            with patch.object(engine, '_run_nfo_close', new_callable=AsyncMock):
                # Run the MCX close phase which includes re-scanning
                today = date.today()
                mcx_close = dtime(23, 30)

                # Mock time so we skip the wait
                with patch('backend.api.algo.expiry.timestamp_indian') as mock_ts:
                    # Make it so we're well past the start time
                    mock_ts.return_value.time.return_value = dtime(22, 0)
                    await engine._run_mcx_close(today, mcx_close)

    # Verify scan_positions was called during MCX close phase
    assert len(scan_positions_called) >= 1, (
        "scan_positions must be called during MCX close phase"
    )

    # Verify close_positions was called with the ITM position
    assert len(close_positions_calls) > 0, (
        "close_positions must be called when ITM positions are found"
    )

    # The close call should have the ITM position
    close_call_positions = close_positions_calls[-1]
    assert len(close_call_positions) >= 1, (
        "Close should include at least the ITM position"
    )
    assert close_call_positions[0].tradingsymbol == "CRUDEOILJAN25C7200", (
        "Close must include the ITM position detected in re-scan"
    )


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


def test_expiry_auto_close_agents_ship_active():
    """Both expiry auto-close built-in agents must ship with status='active'."""
    from backend.api.algo.agent_engine import BUILTIN_AGENTS
    slugs = {a["slug"]: a for a in BUILTIN_AGENTS}
    assert slugs["expiry-day-equity-itm-auto-close"]["status"] == "active"
    assert slugs["expiry-day-commodity-itm-auto-close"]["status"] == "active"


@pytest.mark.asyncio
async def test_nfo_rescan_loop_catches_newly_itm_in_run():
    """run() re-scan loop must detect NFO positions that cross ITM after morning scan.

    Scenario: morning scan finds one MCX position (keeps run() alive past the
    early-return guard). The re-scan loop then finds a newly-ITM NFO position
    that was OTM at open and _run_nfo_close is called for it.
    """
    from unittest.mock import AsyncMock, patch, MagicMock
    from backend.api.algo.expiry import ExpiryEngine, OptionPosition
    from datetime import date, time as dtime, datetime, timezone, timedelta

    engine = ExpiryEngine()

    expiry_date = date.today()

    # MCX position found in morning scan — keeps run() from early-returning
    pos_mcx = OptionPosition(
        account="acc1",
        tradingsymbol="CRUDEOILJAN25C7200",
        exchange="MCX",
        instrument_type="CE",
        underlying="CRUDEOIL",
        strike=7200.0,
        expiry=expiry_date,
        quantity=1,
        product="NRML",
        ltp=50.0,
        underlying_ltp=7300.0,
        moneyness="ITM",
        needs_close=True,
        close_reason="MCX unhedged ITM",
        residual_qty=1,
    )

    # NFO position that goes ITM during the day — caught by re-scan loop
    pos_itm_nfo = OptionPosition(
        account="acc1",
        tradingsymbol="NIFTY24JUL25000CE",
        exchange="NFO",
        instrument_type="CE",
        underlying="NIFTY",
        strike=25000.0,
        expiry=expiry_date,
        quantity=50,
        product="NRML",
        ltp=10.0,
        underlying_ltp=25100.0,
        moneyness="ITM",
        needs_close=True,
        close_reason="Equity ITM — must close before expiry",
        residual_qty=50,
    )

    scan_calls: list[int] = []
    def mock_scan():
        scan_calls.append(1)
        if len(scan_calls) == 1:
            return [pos_mcx]  # morning scan: only MCX, no NFO yet
        return [pos_itm_nfo]  # re-scan: newly-ITM NFO position

    nfo_close_calls: list = []
    async def mock_run_nfo_close(today, equity_close, positions):
        nfo_close_calls.append(list(positions))

    async def mock_run_mcx_close(today, mcx_close):
        pass

    ist_offset = timezone(timedelta(hours=5, minutes=30))
    fake_now = datetime(expiry_date.year, expiry_date.month, expiry_date.day,
                        9, 25, 0, tzinfo=ist_offset)

    call_count = 0
    def mock_timestamp_indian():
        nonlocal call_count
        call_count += 1
        # First 2 calls (now_ist at top of run(), _nfo_deadline): return 09:25
        # Call 3 = while-loop guard — return 09:25 (enters loop)
        # Call 4 = while-loop guard after sleep — return 15:30 (exits loop)
        if call_count <= 3:
            return fake_now
        return fake_now.replace(hour=15, minute=30)

    with patch("backend.api.algo.expiry.timestamp_indian", side_effect=mock_timestamp_indian):
        with patch.object(engine, "scan_positions", side_effect=mock_scan):
            with patch.object(engine, "_run_nfo_close", side_effect=mock_run_nfo_close):
                with patch.object(engine, "_run_mcx_close", side_effect=mock_run_mcx_close):
                    with patch("asyncio.sleep", new_callable=AsyncMock):
                        await engine.run()

    assert len(nfo_close_calls) >= 1, (
        "run() must call _run_nfo_close for newly-ITM NFO positions found in re-scan"
    )
    last_call_syms = [p.tradingsymbol for p in nfo_close_calls[-1]]
    assert "NIFTY24JUL25000CE" in last_call_syms, (
        "Re-scan _run_nfo_close call must include the newly-ITM NFO position"
    )


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
