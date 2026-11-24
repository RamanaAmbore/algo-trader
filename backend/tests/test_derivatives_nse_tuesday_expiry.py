"""
NSE moved equity-derivatives (NIFTY/BANKNIFTY/stock F&O on NFO) monthly
expiry from the last Thursday to the last Tuesday of the month, effective
2025-09-01 (contracts expiring on/before 2025-08-31 kept Thursday). BSE/BFO
(SENSEX, BANKEX) was not affected and stays on Thursday. MCX options/futures
are unaffected either way (Friday/approximate-Thursday respectively).

`backend/api/algo/derivatives.py:parse_tradingsymbol()`'s monthly-expiry
approximation was still hardcoded to "last Thursday" for every non-MCX
underlying — stale since the 2025-09-01 shift. See the CLAUDE.md entry this
test accompanies for the live-verification background (confirmed via the
real Kite instruments cache that the currently-held MCX book is unaffected,
but the NFO approximation itself was objectively wrong for any NFO monthly
contract going forward).
"""

from datetime import date

from backend.api.algo.derivatives import (
    _last_tuesday,
    _last_thursday,
    _monthly_expiry,
    _nse_nfo_equity_monthly_expiry,
    is_bse_underlying,
    parse_tradingsymbol,
)


class TestNseNfoEquityMonthlyExpiryTuesdayShift:
    def test_post_shift_month_resolves_last_tuesday(self):
        # Nov 2026: last Tuesday is 2026-11-24 (confirmed via `date` command
        # and WebSearch of the NSE circular during the investigation).
        expiry = _nse_nfo_equity_monthly_expiry(2026, 11)
        assert expiry == date(2026, 11, 24)
        assert expiry.weekday() == 1  # Tuesday

    def test_pre_shift_month_still_resolves_last_thursday(self):
        expiry = _nse_nfo_equity_monthly_expiry(2025, 8)
        assert expiry == date(2025, 8, 28)
        assert expiry.weekday() == 3  # Thursday

    def test_effective_month_itself_is_tuesday(self):
        # 2025-09 is the first Tuesday-expiry month per the circular.
        expiry = _nse_nfo_equity_monthly_expiry(2025, 9)
        assert expiry == date(2025, 9, 30)
        assert expiry.weekday() == 1


class TestMonthlyExpiryDispatch:
    def test_nifty_option_uses_tuesday_post_shift(self):
        parsed = parse_tradingsymbol("NIFTY26NOV22000CE")
        assert parsed["expiry"] == date(2026, 11, 24)
        assert parsed["expiry"].weekday() == 1

    def test_banknifty_option_uses_tuesday_post_shift(self):
        parsed = parse_tradingsymbol("BANKNIFTY26NOV48000PE")
        assert parsed["expiry"] == date(2026, 11, 24)

    def test_stock_option_uses_tuesday_post_shift(self):
        parsed = parse_tradingsymbol("RELIANCE26NOV2800CE")
        assert parsed["expiry"] == date(2026, 11, 24)

    def test_nifty_future_uses_tuesday_post_shift_not_mcx_approximation(self):
        parsed = parse_tradingsymbol("NIFTY26NOVFUT")
        assert parsed["kind"] == "fut"
        assert parsed["expiry"] == date(2026, 11, 24)

    def test_pre_shift_contract_still_resolves_thursday(self):
        parsed = parse_tradingsymbol("NIFTY25AUG22000CE")
        assert parsed["expiry"] == date(2025, 8, 28)
        assert parsed["expiry"].weekday() == 3


class TestBseUnaffectedByTuesdayShift:
    def test_is_bse_underlying(self):
        assert is_bse_underlying("SENSEX") is True
        assert is_bse_underlying("BANKEX") is True
        assert is_bse_underlying("NIFTY") is False
        assert is_bse_underlying("") is False
        assert is_bse_underlying(None) is False

    def test_sensex_option_stays_on_thursday_post_shift(self):
        parsed = parse_tradingsymbol("SENSEX26NOV82000CE")
        assert parsed["expiry"] == date(2026, 11, 26)
        assert parsed["expiry"].weekday() == 3

    def test_bankex_option_stays_on_thursday_post_shift(self):
        parsed = parse_tradingsymbol("BANKEX26NOV58000PE")
        assert parsed["expiry"].weekday() == 3

    def test_monthly_expiry_dispatch_bse_is_thursday(self):
        assert _monthly_expiry("SENSEX", 2026, 11) == _last_thursday(2026, 11)


class TestMcxUnaffectedByTuesdayShift:
    def test_mcx_option_stays_on_friday(self):
        parsed = parse_tradingsymbol("GOLDM26NOV95000CE")
        assert parsed["expiry"].weekday() == 4  # Friday

    def test_mcx_future_keeps_approximate_thursday(self):
        # Deliberately unchanged — MCX futures expiry varies per commodity
        # and has no single real rule; the approximation stays Thursday.
        parsed = parse_tradingsymbol("CRUDEOIL26NOVFUT")
        assert parsed["expiry"].weekday() == 3  # Thursday (approximation)

    def test_monthly_expiry_dispatch_mcx_is_friday(self):
        assert _monthly_expiry("GOLDM", 2026, 11) == date(2026, 11, 27)
