"""
Tests for MCX-only movers path (NSE closed, MCX open — 15:30-23:30 IST).

Five quality dimensions:
  1. SSOT        — _build_mcx_universe is the single-pass universe builder;
                   _get_movers_mcx_live calls it; _session_movers_mcx is
                   separate from _session_movers (no NSE/MCX contamination).
  2. Performance — _build_mcx_universe is a single O(N) pass over all_items;
                   no N async look-ups at request time.
  3. Stale code  — NSE snapshot path is NOT taken when MCX is open;
                   MCX rows are NOT persisted to movers_snapshots.
  4. Reusable    — _build_mcx_universe is module-level and importable;
                   _combine_movers is reused unchanged (directional balance).
  5. Correctness — branch decision matrix: 20:00 IST → MCX rows only;
                   12:00 IST → NSE path; 05:00 IST → snapshot path.
                   MCX exchange field = "MCX" on every row.
                   Broker fail during MCX-only → empty, no NSE snapshot fallback.

Behavior-matrix tests (Dimension 5 runtime):
  - test_behavior_20_00_mcx_only  — NSE closed, MCX open  → _get_movers_mcx_live called
  - test_behavior_12_00_nse_open  — NSE open               → _get_movers_mcx_live NOT called
  - test_behavior_05_00_both_closed — both closed          → _get_movers_now_off_hours called
"""

import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

# ---------------------------------------------------------------------------
# Source paths
# ---------------------------------------------------------------------------
_WATCHLIST_SRC = Path(__file__).parent.parent / "api" / "routes" / "watchlist.py"


def _wl_source() -> str:
    return _WATCHLIST_SRC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers for synthetic instruments
# ---------------------------------------------------------------------------

def _make_inst(e: str, t: str, u: str, s: str, x: str = "2026-07-25") -> SimpleNamespace:
    """Synthesise a minimal instrument-like object."""
    return SimpleNamespace(e=e, t=t, u=u, s=s, x=x)


def _mcx_instruments() -> list:
    """A minimal set of MCX instruments covering 3 commodity roots.

    GOLD   — has CE + PE + FUT
    SILVER — has CE + PE + FUT
    CRUDEOIL — has CE + PE + FUT
    """
    return [
        # GOLD
        _make_inst("MCX", "CE", "GOLD", "GOLD26JUL6000CE"),
        _make_inst("MCX", "PE", "GOLD", "GOLD26JUL5500PE"),
        _make_inst("MCX", "FUT", "GOLD", "GOLD26JUNFUT", x="2026-06-28"),
        _make_inst("MCX", "FUT", "GOLD", "GOLD26JULFUT", x="2026-07-25"),  # later expiry
        # SILVER
        _make_inst("MCX", "CE", "SILVER", "SILVER26JUL90000CE"),
        _make_inst("MCX", "PE", "SILVER", "SILVER26JUL80000PE"),
        _make_inst("MCX", "FUT", "SILVER", "SILVER26JUNFUT", x="2026-06-28"),
        # CRUDEOIL
        _make_inst("MCX", "CE", "CRUDEOIL", "CRUDEOIL26JUL7000CE"),
        _make_inst("MCX", "FUT", "CRUDEOIL", "CRUDEOIL26JUNFUT", x="2026-06-28"),
        # NSE equity — must be ignored
        _make_inst("NSE", "CE", "NIFTY", "NIFTY26JUL23000CE"),
        _make_inst("NSE", "EQ", "RELIANCE", "RELIANCE"),
    ]


# ---------------------------------------------------------------------------
# Dimension 1 (SSOT) — structural source checks
# ---------------------------------------------------------------------------

def test_build_mcx_universe_defined():
    """_build_mcx_universe is a module-level helper in watchlist.py."""
    src = _wl_source()
    assert "def _build_mcx_universe(" in src, (
        "_build_mcx_universe not found in watchlist.py"
    )


def test_session_movers_mcx_state_defined():
    """_session_movers_mcx is defined as a separate module-level dict."""
    src = _wl_source()
    assert "_session_movers_mcx" in src, (
        "_session_movers_mcx state not found in watchlist.py"
    )


def test_mcx_branch_calls_get_movers_mcx_live():
    """get_movers delegates the MCX-only path to _get_movers_mcx_live."""
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "get_movers not found"
    body = match.group(0)
    assert "_get_movers_mcx_live(" in body, (
        "get_movers does not call _get_movers_mcx_live — MCX-only branch not wired"
    )


def test_mcx_live_does_not_save_snapshot():
    """_get_movers_mcx_live must NOT call _save_movers_snapshot.

    Persisting MCX rows would overwrite the NSE 15:29 close snapshot and
    corrupt the off-hours fallback before 09:15 the following morning.
    """
    src = _wl_source()
    match = re.search(
        r"async def _get_movers_mcx_live\(.*?\).*?(?=\nasync def |\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "_get_movers_mcx_live body not found"
    body = match.group(0)
    assert "_save_movers_snapshot" not in body, (
        "_get_movers_mcx_live calls _save_movers_snapshot — "
        "MCX rows must NOT be persisted to movers_snapshots"
    )


def test_mcx_branch_uses_separate_session_dict():
    """_get_movers_mcx_live uses _session_movers_mcx, not _session_movers."""
    src = _wl_source()
    match = re.search(
        r"async def _get_movers_mcx_live\(.*?\).*?(?=\nasync def |\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "_get_movers_mcx_live body not found"
    body = match.group(0)
    assert "_session_movers_mcx" in body, (
        "_get_movers_mcx_live does not use _session_movers_mcx — "
        "NSE/MCX sticky state will contaminate each other"
    )
    # Must NOT reference bare _session_movers (would cross-contaminate).
    # Allow _session_movers_mcx but not the plain _session_movers reference.
    # Strip out _session_movers_mcx occurrences first, then check.
    stripped = body.replace("_session_movers_mcx", "")
    assert "_session_movers" not in stripped, (
        "_get_movers_mcx_live references _session_movers (NSE dict) — "
        "use _session_movers_mcx to avoid evening NSE→MCX contamination"
    )


# ---------------------------------------------------------------------------
# Dimension 2 (Performance) — _build_mcx_universe single-pass
# ---------------------------------------------------------------------------

def test_build_mcx_universe_single_pass():
    """_build_mcx_universe body has no nested for-loops (single O(N) pass)."""
    src = _wl_source()
    match = re.search(
        r"def _build_mcx_universe\(.*?\).*?(?=\nasync def |\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "_build_mcx_universe body not found"
    body = match.group(0)
    # Count indented for-lines; a nested loop would add a third+ level.
    for_lines = [ln for ln in body.splitlines() if re.match(r"\s{4,}for ", ln)]
    # One for loop (the single pass over all_items) + one for the fut-pick.
    assert len(for_lines) <= 3, (
        f"_build_mcx_universe has {len(for_lines)} for-lines — expected ≤3 (not nested)"
    )


# ---------------------------------------------------------------------------
# Dimension 3 (Stale code) — NSE snapshot NOT served in MCX-only hours
# ---------------------------------------------------------------------------

def test_mcx_branch_no_snapshot_fallback_in_source():
    """_get_movers_mcx_live does NOT call _get_movers_now_off_hours.

    If broker fails during MCX-only hours the function must return empty rows,
    not fall through to the NSE snapshot helper.
    """
    src = _wl_source()
    match = re.search(
        r"async def _get_movers_mcx_live\(.*?\).*?(?=\nasync def |\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "_get_movers_mcx_live body not found"
    body = match.group(0)
    assert "_get_movers_now_off_hours" not in body, (
        "_get_movers_mcx_live calls _get_movers_now_off_hours — "
        "MCX-only path must not fall back to NSE snapshot on broker failure"
    )


def test_mcx_quote_keys_use_fut_symbol():
    """_get_movers_mcx_live builds keys as MCX:<FUT_symbol>, not MCX:<root>."""
    src = _wl_source()
    match = re.search(
        r"async def _get_movers_mcx_live\(.*?\).*?(?=\nasync def |\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "_get_movers_mcx_live body not found"
    body = match.group(0)
    # Must resolve FUT symbol per root (mcx_underlying_to_fut lookup).
    assert "mcx_underlying_to_fut" in body, (
        "_get_movers_mcx_live does not use mcx_underlying_to_fut — "
        "quote keys must be MCX:<FUT_symbol>, not MCX:<root>"
    )


# ---------------------------------------------------------------------------
# Dimension 4 (Reusable) — _build_mcx_universe functional tests
# ---------------------------------------------------------------------------

def _load_build_fn():
    """Extract and compile _build_mcx_universe from source for unit tests."""
    src = _wl_source()
    # Extract the helper plus its dependency on is_mcx_underlying.
    fn_match = re.search(
        r"(def _build_mcx_universe\(.*?)(?=\nasync def _resolve_mcx_commodity|\Z)",
        src, re.DOTALL,
    )
    assert fn_match, "_build_mcx_universe source not extractable"
    fn_src = fn_match.group(1)

    # Provide a minimal is_mcx_underlying stub so exec doesn't need the import.
    from backend.api.algo.derivatives import is_mcx_underlying
    ns: dict = {"is_mcx_underlying": is_mcx_underlying}
    # Strip the "from backend..." import line inside the function (exec namespace provides it).
    cleaned = re.sub(r"\s+from backend\.api\.algo\.derivatives import.*?\n", "\n", fn_src)
    exec(cleaned, ns)  # noqa: S102
    return ns["_build_mcx_universe"]


def test_build_mcx_universe_correct_roots():
    """_build_mcx_universe extracts GOLD, SILVER, CRUDEOIL from MCX instruments."""
    build = _load_build_fn()
    opt_roots, fut_map = build(_mcx_instruments())
    assert "GOLD" in opt_roots, f"GOLD missing from mcx_opt_roots: {opt_roots}"
    assert "SILVER" in opt_roots, f"SILVER missing: {opt_roots}"
    assert "CRUDEOIL" in opt_roots, f"CRUDEOIL missing: {opt_roots}"


def test_build_mcx_universe_excludes_nse():
    """_build_mcx_universe ignores NSE and BSE instruments."""
    build = _load_build_fn()
    opt_roots, fut_map = build(_mcx_instruments())
    assert "NIFTY" not in opt_roots, f"NSE underlying leaked into MCX set: {opt_roots}"
    assert "RELIANCE" not in opt_roots


def test_build_mcx_universe_fut_map_earliest_expiry():
    """_build_mcx_universe picks the EARLIEST-expiry FUT for GOLD (near-month)."""
    build = _load_build_fn()
    _, fut_map = build(_mcx_instruments())
    # GOLD has two FUTs: GOLD26JUNFUT (2026-06-28) and GOLD26JULFUT (2026-07-25).
    # Earliest should win.
    assert fut_map.get("GOLD") == "GOLD26JUNFUT", (
        f"Expected GOLD26JUNFUT (earliest expiry), got {fut_map.get('GOLD')}"
    )


def test_build_mcx_universe_no_opt_root_no_fut():
    """_build_mcx_universe omits roots from fut_map if they have no CE/PE chain."""
    build = _load_build_fn()
    # Instruments with FUT only (no CE/PE) — shouldn't appear in fut_map.
    items = [_make_inst("MCX", "FUT", "COPPER", "COPPER26JUNFUT")]
    opt_roots, fut_map = build(items)
    assert "COPPER" not in opt_roots
    assert "COPPER" not in fut_map


def test_build_mcx_universe_empty_list():
    """_build_mcx_universe handles empty instruments list gracefully."""
    build = _load_build_fn()
    opt_roots, fut_map = build([])
    assert opt_roots == set()
    assert fut_map == {}


def test_build_mcx_universe_mcx_universe_size():
    """_build_mcx_universe with full MCX fixture yields 3 roots, 3 FUT symbols."""
    build = _load_build_fn()
    opt_roots, fut_map = build(_mcx_instruments())
    assert len(opt_roots) == 3, f"Expected 3 MCX roots, got {len(opt_roots)}: {opt_roots}"
    assert len(fut_map) == 3, f"Expected 3 FUT mappings, got {len(fut_map)}: {fut_map}"


# ---------------------------------------------------------------------------
# Dimension 5 (Correctness) — branch decision matrix
# ---------------------------------------------------------------------------

def _make_market_open_mock(nse_open: bool, mcx_open: bool):
    """Return a side_effect function for is_market_open that returns
    nse_open for NSE calls and mcx_open for MCX calls."""
    def _side_effect(now, holiday_set, market_start=None, market_end=None,
                     exchange=None):
        if exchange == "NSE":
            return nse_open
        if exchange == "MCX":
            return mcx_open
        return False
    return _side_effect


def test_branch_decision_mcx_only_hour_calls_mcx_live():
    """At 20:00 IST (NSE closed, MCX open) → get_movers calls _get_movers_mcx_live."""
    src = _wl_source()
    # Source-level check: the branch exists for not nse_is_open and mcx_is_open.
    assert "not nse_is_open and mcx_is_open" in src, (
        "MCX-only branch condition 'not nse_is_open and mcx_is_open' not found in get_movers"
    )


def test_branch_decision_nse_open_skips_mcx_path():
    """During NSE hours → _get_movers_mcx_live must NOT be the active branch."""
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "get_movers not found"
    body = match.group(0)
    # The MCX-live call must be guarded by 'not nse_is_open and mcx_is_open'.
    # A call to _get_movers_mcx_live outside that guard would mean it fires always.
    # Verify the guard precedes the call.
    mcx_guard_pos = body.find("not nse_is_open and mcx_is_open")
    mcx_call_pos = body.find("_get_movers_mcx_live(")
    assert mcx_guard_pos != -1, "MCX-only guard not found in get_movers"
    assert mcx_call_pos != -1, "_get_movers_mcx_live call not found in get_movers"
    assert mcx_guard_pos < mcx_call_pos, (
        "_get_movers_mcx_live call appears BEFORE its guard — "
        "MCX path would fire even when NSE is open"
    )


def test_branch_decision_both_closed_snapshot():
    """Both closed → snapshot branch: 'not nse_is_open and not mcx_is_open' in get_movers."""
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "get_movers not found"
    body = match.group(0)
    assert "not nse_is_open and not mcx_is_open" in body, (
        "Both-closed guard 'not nse_is_open and not mcx_is_open' missing from get_movers"
    )


def test_mcx_exchange_field_is_mcx():
    """_get_movers_mcx_live sets exchange='MCX' on every row it builds."""
    src = _wl_source()
    match = re.search(
        r"async def _get_movers_mcx_live\(.*?\).*?(?=\nasync def |\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "_get_movers_mcx_live body not found"
    body = match.group(0)
    # All exchange assignments in the body should reference "MCX", not "NSE".
    # Look for exchange= assignments in live_snapshot + _session_movers_mcx blocks.
    exchange_assignments = re.findall(r'"exchange":\s*"([A-Z]+)"', body)
    for exch in exchange_assignments:
        assert exch == "MCX", (
            f"exchange field set to '{exch}' inside _get_movers_mcx_live — must be 'MCX'"
        )


def test_mcx_holidays_fetched_for_mcx_gate():
    """get_movers fetches MCX holidays (fetch_holidays('MCX')) for the MCX gate."""
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "get_movers not found"
    body = match.group(0)
    assert 'fetch_holidays("MCX")' in body or "fetch_holidays('MCX')" in body, (
        "get_movers does not call fetch_holidays('MCX') — "
        "MCX holiday calendar not consulted for the MCX-open gate"
    )


def test_mcx_session_rollover_clears_mcx_dict():
    """Session rollover at midnight must clear _session_movers_mcx too."""
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "get_movers not found"
    body = match.group(0)
    # Find the midnight-rollover block.
    rollover_match = re.search(
        r"if _session_date != ist_today:.*?_session_date = ist_today",
        body, re.DOTALL,
    )
    assert rollover_match, "Session rollover block not found in get_movers"
    rollover_block = rollover_match.group(0)
    assert "_session_movers_mcx" in rollover_block, (
        "Session rollover does not reset _session_movers_mcx — "
        "stale MCX stickies from the previous day survive midnight rollover"
    )


def test_fut_map_cache_populated_in_first_run_branch():
    """_mcx_fut_map_cache is assigned in the cache-miss branch of _get_movers_mcx_live."""
    src = _wl_source()
    match = re.search(
        r"async def _get_movers_mcx_live\(.*?\).*?(?=\nasync def |\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "_get_movers_mcx_live body not found"
    body = match.group(0)
    assert "_mcx_fut_map_cache" in body, (
        "_get_movers_mcx_live does not write _mcx_fut_map_cache — "
        "the warm-cache else branch will re-scan all_items on every 30s poll"
    )


def test_fut_map_cache_cleared_on_rollover():
    """midnight rollover in get_movers clears _mcx_fut_map_cache alongside _session_movers_mcx."""
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "get_movers not found"
    body = match.group(0)
    rollover_match = re.search(
        r"if _session_date != ist_today:.*?_session_date = ist_today",
        body, re.DOTALL,
    )
    assert rollover_match, "Session rollover block not found in get_movers"
    rollover_block = rollover_match.group(0)
    assert "_mcx_fut_map_cache" in rollover_block, (
        "Session rollover does not clear _mcx_fut_map_cache — "
        "stale yesterday's FUT symbols survive into the new trading day"
    )


# ---------------------------------------------------------------------------
# Dimension 5 (Correctness) — behavior-matrix RUNTIME tests
#
# Strategy: patch the *delegates* (_get_movers_mcx_live, _get_movers_now_off_hours)
# as AsyncMocks so no broker calls are made.  Patch is_market_open at its
# source module (date_time_utils) and timestamp_indian likewise — because
# get_movers imports them with `from … import …` inside the function body,
# which re-reads from the source module at call time.
# fetch_holidays is also patched at its source to avoid DB/broker calls.
# ---------------------------------------------------------------------------

_IST = ZoneInfo("Asia/Kolkata")


def _make_ist(hour: int, minute: int = 0) -> datetime:
    """Return a timezone-aware IST datetime on an arbitrary weekday (Wednesday)."""
    return datetime(2026, 7, 1, hour, minute, 0, tzinfo=_IST)  # 2026-07-01 is a Wednesday


def _market_open_side_effect(nse_open: bool, mcx_open: bool):
    """Return a side_effect function for is_market_open keyed on exchange= kwarg."""
    def _fn(now, holiday_set, market_start=None, market_end=None, exchange=None):
        if exchange == "NSE":
            return nse_open
        if exchange == "MCX":
            return mcx_open
        return False
    return _fn


@pytest.fixture(autouse=True)
def _reset_session_state():
    """Ensure module-level session dicts are clean before each behavior test."""
    import backend.api.routes.watchlist as wl
    wl._session_date = None
    wl._session_movers = {}
    wl._session_movers_mcx = {}
    wl._mcx_underlyings_cache = set()
    wl._mcx_underlyings_cache_date = None
    wl._mcx_fut_map_cache = {}
    yield
    # clean up after too
    wl._session_date = None
    wl._session_movers = {}
    wl._session_movers_mcx = {}
    wl._mcx_underlyings_cache = set()
    wl._mcx_underlyings_cache_date = None
    wl._mcx_fut_map_cache = {}


from backend.api.routes.watchlist import WatchlistController, MoversResponse  # noqa: E402


def _call_get_movers(ctrl):
    """Return the raw coroutine for ctrl.get_movers (bypasses HTTPRouteHandler.__call__)."""
    # Litestar wraps get_movers in an HTTPRouteHandler; .fn is the original coroutine.
    handler = WatchlistController.get_movers
    return handler.fn(ctrl)


@pytest.mark.asyncio
async def test_behavior_20_00_mcx_only():
    """20:00 IST (NSE closed, MCX open) → _get_movers_mcx_live is awaited,
    _get_movers_now_off_hours is NOT called."""
    fake_ist = _make_ist(20, 0)
    fake_response = MoversResponse(movers=[], threshold_pct=5.0, session_date="2026-07-01")

    with (
        patch("backend.shared.helpers.date_time_utils.timestamp_indian",
              return_value=fake_ist),
        patch("backend.shared.helpers.date_time_utils.is_market_open",
              side_effect=_market_open_side_effect(nse_open=False, mcx_open=True)),
        patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()),
        patch("backend.api.routes.watchlist._get_movers_mcx_live",
              new_callable=AsyncMock, return_value=fake_response) as mock_mcx,
        patch("backend.api.routes.watchlist._get_movers_now_off_hours",
              new_callable=AsyncMock) as mock_off_hours,
    ):
        ctrl = WatchlistController(owner=MagicMock())
        result = await _call_get_movers(ctrl)

    mock_mcx.assert_awaited_once(), "20:00 IST: _get_movers_mcx_live was NOT called"
    mock_off_hours.assert_not_awaited(), "20:00 IST: NSE snapshot helper was called — MCX path not taken"
    assert result is fake_response


@pytest.mark.asyncio
async def test_behavior_12_00_nse_open():
    """12:00 IST (both NSE and MCX open) → _get_movers_mcx_live is NOT called;
    the NSE live path runs (symbolised here by _get_movers_now_off_hours also not called)."""
    fake_ist = _make_ist(12, 0)

    with (
        patch("backend.shared.helpers.date_time_utils.timestamp_indian",
              return_value=fake_ist),
        patch("backend.shared.helpers.date_time_utils.is_market_open",
              side_effect=_market_open_side_effect(nse_open=True, mcx_open=True)),
        patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()),
        patch("backend.api.routes.watchlist._get_movers_mcx_live",
              new_callable=AsyncMock) as mock_mcx,
        patch("backend.api.routes.watchlist._get_movers_now_off_hours",
              new_callable=AsyncMock) as mock_off_hours,
        # Patch the NSE live sub-calls so the handler doesn't hit the DB/broker.
        patch("backend.api.cache.get_or_fetch", new_callable=AsyncMock, return_value=None),
        patch("backend.brokers.registry.get_price_broker", return_value=MagicMock()),
    ):
        ctrl = WatchlistController(owner=MagicMock())
        # NSE live path ends with a return of MoversResponse; it may raise on
        # missing data — we only care that the MCX delegate was NOT called.
        try:
            await _call_get_movers(ctrl)
        except Exception:
            pass  # NSE path may fail without real instruments — that's expected

    mock_mcx.assert_not_awaited(), "12:00 IST: _get_movers_mcx_live was called when NSE is open"
    mock_off_hours.assert_not_awaited(), "12:00 IST: off-hours helper was called when NSE is open"


@pytest.mark.asyncio
async def test_behavior_05_00_both_closed():
    """05:00 IST (both NSE and MCX closed) → _get_movers_now_off_hours is awaited,
    _get_movers_mcx_live is NOT called."""
    fake_ist = _make_ist(5, 0)

    with (
        patch("backend.shared.helpers.date_time_utils.timestamp_indian",
              return_value=fake_ist),
        patch("backend.shared.helpers.date_time_utils.is_market_open",
              side_effect=_market_open_side_effect(nse_open=False, mcx_open=False)),
        patch("backend.brokers.broker_apis.fetch_holidays", return_value=set()),
        patch("backend.api.routes.watchlist._get_movers_mcx_live",
              new_callable=AsyncMock) as mock_mcx,
        patch("backend.api.routes.watchlist._get_movers_now_off_hours",
              new_callable=AsyncMock,
              return_value=([], "snapshot_missing_off_hours")) as mock_off_hours,
    ):
        ctrl = WatchlistController(owner=MagicMock())
        result = await _call_get_movers(ctrl)

    mock_off_hours.assert_awaited_once(), "05:00 IST: _get_movers_now_off_hours was NOT called"
    mock_mcx.assert_not_awaited(), "05:00 IST: MCX live path was triggered — snapshot path not taken"
    # Result should be empty MoversResponse (no snapshot present)
    assert hasattr(result, "movers"), "result is not a MoversResponse"
    assert result.movers == [], f"Expected empty movers for snapshot_missing_off_hours, got {result.movers}"
