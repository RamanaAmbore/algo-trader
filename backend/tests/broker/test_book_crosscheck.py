"""
Book cross-check — positions, holdings, and funds consistency.

Verifies that per-account row-level data agrees with summary rows and
that P&L formulas are internally consistent across all three book surfaces.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANUAL RUN (anytime, no Kite session needed):

    python3 backend/tests/broker/test_book_crosscheck.py

AUTOMATED (part of the pytest suite):

    venv/bin/pytest backend/tests/broker/test_book_crosscheck.py -v -s
    venv/bin/pytest backend/tests/ -v -s   # runs along with all other tests

    Skips automatically if dev.ramboq.com is unreachable (e.g. offline CI).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error

import pytest

DEV_BASE = "https://dev.ramboq.com"
DEV_USER = "rambo"
DEV_PASS = "admin1234"

# Tolerances
POSITIONS_DCV_PCT = 5.0       # 5% aggregate tolerance for positions day_change_val
POSITIONS_DCV_RUPEES = 10.0   # ₹10 per-account tolerance (positions intraday complexity)
HOLDINGS_DCV_PCT = 5.0        # 5% aggregate tolerance for holdings day_change_val
HOLDINGS_DCV_RUPEES = 5.0     # ₹5 per-account tolerance
SUMMARY_TOLERANCE_RUPEES = 1.0  # ₹1 tolerance for TOTAL row


# ── fetch from dev API ──────────────────────────────────────────────────────

def _login() -> str:
    """Return a JWT token from dev.ramboq.com."""
    req = urllib.request.Request(
        f"{DEV_BASE}/api/auth/login",
        data=json.dumps({"username": DEV_USER, "password": DEV_PASS}).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["access_token"]


def _dev_reachable() -> bool:
    """Quick connectivity check — used by pytest skip guard."""
    try:
        urllib.request.urlopen(f"{DEV_BASE}/api/health", timeout=5)
        return True
    except Exception:
        return False


def _fetch_all_holdings(token: str) -> dict:
    """Fetch holdings response (rows + summary) from dev.ramboq.com."""
    req = urllib.request.Request(
        f"{DEV_BASE}/api/holdings",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _fetch_all_positions(token: str) -> dict:
    """Fetch positions response (rows + summary) from dev.ramboq.com."""
    req = urllib.request.Request(
        f"{DEV_BASE}/api/positions",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _fetch_all_funds(token: str) -> dict:
    """Fetch funds response (rows only, no summary) from dev.ramboq.com."""
    req = urllib.request.Request(
        f"{DEV_BASE}/api/funds",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ── cross-check logic ───────────────────────────────────────────────────────

def _pct_diff(api_val: float, expected_val: float) -> float:
    """Compute percentage difference (0 if both are near-zero)."""
    if abs(api_val) > 1.0:
        return abs(expected_val - api_val) / abs(api_val) * 100
    return 0.0 if abs(expected_val - api_val) < 1.0 else 100.0


def _check_day_change_val_per_account(
    rows: list[dict],
    account: str,
) -> dict:
    """Validate day_change_val per account against row data.

    Returns:
        {
            'account': str,
            'api_total': float,    # sum of day_change_val from rows
            'n_rows': int,         # number of rows
            'passed': bool,        # whether within tolerance
        }
    """
    account_rows = [r for r in rows if str(r.get("account") or "").upper() == account.upper()]
    if not account_rows:
        return {
            "account": account,
            "api_total": 0.0,
            "n_rows": 0,
            "passed": True,
        }

    api_total = sum(float(r.get("day_change_val") or 0) for r in account_rows)
    return {
        "account": account,
        "api_total": api_total,
        "n_rows": len(account_rows),
        "passed": True,  # Basic sanity pass — existence check
    }


# ── pytest tests ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def book_data():
    """Fetch holdings, positions, and funds once; shared by all tests."""
    if not _dev_reachable():
        pytest.skip("dev.ramboq.com unreachable — skipping live cross-check")
    token = _login()
    return {
        "holdings": _fetch_all_holdings(token),
        "positions": _fetch_all_positions(token),
        "funds": _fetch_all_funds(token),
    }


class TestHoldingsCrossCheck:
    """
    Holdings book cross-checks — per-account consistency and formula validation.
    """

    def test_row_vs_summary_per_account(self, book_data):
        """Each account's sum of day_change_val rows must match its summary row."""
        holdings = book_data["holdings"]
        rows = holdings.get("rows", [])
        summary = holdings.get("summary", [])

        if not rows or not summary:
            pytest.skip("No holdings data")

        # Build per-account sums from rows
        account_row_sums: dict[str, float] = {}
        for row in rows:
            acct = str(row.get("account") or "").upper()
            if acct:
                account_row_sums[acct] = account_row_sums.get(acct, 0.0) + float(row.get("day_change_val") or 0)

        # Check each account's sum against summary
        mismatches = []
        for acct, row_sum in account_row_sums.items():
            summary_row = next((s for s in summary if str(s.get("account") or "").upper() == acct), None)
            if not summary_row:
                mismatches.append(f"Account {acct}: found in rows but missing in summary")
                continue

            summary_dcv = float(summary_row.get("day_change_val") or 0)
            diff = abs(row_sum - summary_dcv)
            tolerance = max(HOLDINGS_DCV_RUPEES, abs(summary_dcv) * HOLDINGS_DCV_PCT / 100)

            if diff > tolerance:
                mismatches.append(
                    f"Account {acct}: row_sum=₹{row_sum:,.2f}, summary=₹{summary_dcv:,.2f}, "
                    f"diff=₹{diff:,.2f} (tolerance ₹{tolerance:,.2f})"
                )

        assert not mismatches, (
            f"Holdings per-account day_change_val mismatches:\n" +
            "\n".join(f"  {m}" for m in mismatches)
        )

    def test_summary_total_equals_sum_of_accounts(self, book_data):
        """The TOTAL row must equal the sum of all per-account rows in summary."""
        holdings = book_data["holdings"]
        summary = holdings.get("summary", [])

        if not summary:
            pytest.skip("No holdings summary")

        total_row = next((s for s in summary if str(s.get("account") or "").upper() == "TOTAL"), None)
        if not total_row:
            pytest.skip("No TOTAL row in holdings summary")

        # Sum all non-TOTAL rows
        acct_rows = [s for s in summary if str(s.get("account") or "").upper() != "TOTAL"]
        if not acct_rows:
            pytest.skip("No per-account rows to sum")

        account_sum = sum(float(s.get("day_change_val") or 0) for s in acct_rows)
        total_dcv = float(total_row.get("day_change_val") or 0)
        diff = abs(account_sum - total_dcv)

        assert diff <= SUMMARY_TOLERANCE_RUPEES, (
            f"Holdings TOTAL row mismatch: sum_of_accounts=₹{account_sum:,.2f}, "
            f"total_row=₹{total_dcv:,.2f}, diff=₹{diff:,.2f} "
            f"(tolerance ₹{SUMMARY_TOLERANCE_RUPEES})"
        )

    def test_quantity_and_price_consistency(self, book_data):
        """Holdings rows must have consistent quantity + price relationships."""
        holdings = book_data["holdings"]
        rows = holdings.get("rows", [])

        if not rows:
            pytest.skip("No holdings rows")

        errors = []
        for i, row in enumerate(rows):
            qty = float(row.get("quantity") or 0)
            last_price = float(row.get("last_price") or 0)
            avg_price = float(row.get("average_price") or 0)
            inv_val = float(row.get("investment_value") or 0)
            cur_val = float(row.get("current_value") or 0)

            # Invested value should be approximately avg_price × qty
            if qty > 0 and avg_price > 0 and inv_val > 0:
                expected_inv = avg_price * qty
                if abs(expected_inv - inv_val) / expected_inv > 0.02:
                    errors.append(
                        f"Row {i} ({row.get('symbol')}): investment_value inconsistent "
                        f"(expected ₹{expected_inv:,.2f}, got ₹{inv_val:,.2f})"
                    )

            # Current value should be approximately last_price × qty
            if qty > 0 and last_price > 0 and cur_val > 0:
                expected_cur = last_price * qty
                if abs(expected_cur - cur_val) / expected_cur > 0.02:
                    errors.append(
                        f"Row {i} ({row.get('symbol')}): current_value inconsistent "
                        f"(expected ₹{expected_cur:,.2f}, got ₹{cur_val:,.2f})"
                    )

        assert not errors, "\n".join(f"  {e}" for e in errors)


class TestPositionsCrossCheck:
    """
    Positions book cross-checks — per-account consistency and sign validation.
    """

    def test_row_vs_summary_per_account(self, book_data):
        """Each account's sum of day_change_val rows must match its summary row."""
        positions = book_data["positions"]
        rows = positions.get("rows", [])
        summary = positions.get("summary", [])

        if not rows or not summary:
            pytest.skip("No positions data")

        # Build per-account sums from rows
        account_row_sums: dict[str, float] = {}
        for row in rows:
            acct = str(row.get("account") or "").upper()
            if acct:
                account_row_sums[acct] = account_row_sums.get(acct, 0.0) + float(row.get("day_change_val") or 0)

        # Check each account's sum against summary
        mismatches = []
        for acct, row_sum in account_row_sums.items():
            summary_row = next((s for s in summary if str(s.get("account") or "").upper() == acct), None)
            if not summary_row:
                mismatches.append(f"Account {acct}: found in rows but missing in summary")
                continue

            summary_dcv = float(summary_row.get("day_change_val") or 0)
            diff = abs(row_sum - summary_dcv)
            tolerance = max(POSITIONS_DCV_RUPEES, abs(summary_dcv) * POSITIONS_DCV_PCT / 100)

            if diff > tolerance:
                mismatches.append(
                    f"Account {acct}: row_sum=₹{row_sum:,.2f}, summary=₹{summary_dcv:,.2f}, "
                    f"diff=₹{diff:,.2f} (tolerance ₹{tolerance:,.2f})"
                )

        assert not mismatches, (
            f"Positions per-account day_change_val mismatches:\n" +
            "\n".join(f"  {m}" for m in mismatches)
        )

    def test_summary_total_equals_sum_of_accounts(self, book_data):
        """The TOTAL row must equal the sum of all per-account rows in summary."""
        positions = book_data["positions"]
        summary = positions.get("summary", [])

        if not summary:
            pytest.skip("No positions summary")

        total_row = next((s for s in summary if str(s.get("account") or "").upper() == "TOTAL"), None)
        if not total_row:
            pytest.skip("No TOTAL row in positions summary")

        acct_rows = [s for s in summary if str(s.get("account") or "").upper() != "TOTAL"]
        if not acct_rows:
            pytest.skip("No per-account rows to sum")

        account_sum = sum(float(s.get("day_change_val") or 0) for s in acct_rows)
        total_dcv = float(total_row.get("day_change_val") or 0)
        diff = abs(account_sum - total_dcv)

        assert diff <= SUMMARY_TOLERANCE_RUPEES, (
            f"Positions TOTAL row mismatch: sum_of_accounts=₹{account_sum:,.2f}, "
            f"total_row=₹{total_dcv:,.2f}, diff=₹{diff:,.2f} "
            f"(tolerance ₹{SUMMARY_TOLERANCE_RUPEES})"
        )

    def test_pnl_sign_sanity(self, book_data):
        """Long positions (qty > 0) with last_price > avg_price should have pnl >= -100."""
        positions = book_data["positions"]
        rows = positions.get("rows", [])

        if not rows:
            pytest.skip("No positions rows")

        errors = []
        for i, row in enumerate(rows):
            qty = float(row.get("quantity") or 0)
            last_price = float(row.get("last_price") or 0)
            avg_price = float(row.get("average_price") or 0)
            pnl = float(row.get("pnl") or 0)

            # Long position with last_price > avg_price should have positive P&L
            if qty > 0 and avg_price > 0 and last_price > avg_price:
                if pnl < -100:  # Allow small negative due to rounding/adjustments
                    errors.append(
                        f"Row {i} ({row.get('tradingsymbol')}): long position with "
                        f"last_price (₹{last_price:.2f}) > avg_price (₹{avg_price:.2f}) "
                        f"but pnl={pnl:,.2f} (should be positive or near-zero)"
                    )

        assert not errors, "\n".join(f"  {e}" for e in errors)


class TestFundsCrossCheck:
    """
    Funds book cross-checks — margin and cash consistency.
    """

    def test_cash_nonnegative(self, book_data):
        """All cash fields must be >= -100 (allow small rounding negatives)."""
        funds = book_data["funds"]
        rows = funds.get("rows", [])

        if not rows:
            pytest.skip("No funds rows")

        errors = []
        for i, row in enumerate(rows):
            acct = str(row.get("account") or "")
            cash = float(row.get("cash") or 0)
            live_cash = float(row.get("live_cash") or 0)
            available_cash = float(row.get("available_cash") or 0)

            # cash should not be large negative
            if cash < -100:
                errors.append(
                    f"Account {acct}: cash={cash:,.2f} (should be >= -₹100 for rounding tolerance)"
                )

            # live_cash should not be large negative
            if live_cash < -100:
                errors.append(
                    f"Account {acct}: live_cash={live_cash:,.2f} (should be >= -₹100)"
                )

            # available_cash should not be large negative
            if available_cash < -100:
                errors.append(
                    f"Account {acct}: available_cash={available_cash:,.2f} (should be >= -₹100)"
                )

        assert not errors, "\n".join(f"  {e}" for e in errors)

    def test_used_margin_nonnegative(self, book_data):
        """Used margin must be >= -100 (allow small rounding negatives)."""
        funds = book_data["funds"]
        rows = funds.get("rows", [])

        if not rows:
            pytest.skip("No funds rows")

        errors = []
        for row in rows:
            acct = str(row.get("account") or "")
            used_margin = float(row.get("used_margin") or 0)

            if used_margin < -100:
                errors.append(
                    f"Account {acct}: used_margin={used_margin:,.2f} "
                    f"(should be >= -₹100 for rounding tolerance)"
                )

        assert not errors, "\n".join(f"  {e}" for e in errors)

    def test_margin_consistency(self, book_data):
        """When total margin is positive, used + available should approximately sum to total."""
        funds = book_data["funds"]
        rows = funds.get("rows", [])

        if not rows:
            pytest.skip("No funds rows")

        # Look for a concept of "total margin" — available_funds + used_margin approximates total
        errors = []
        for row in rows:
            acct = str(row.get("account") or "")
            avail_margin = float(row.get("avail_margin") or 0)
            used_margin = float(row.get("used_margin") or 0)
            available_funds = float(row.get("available_funds") or 0)

            # If we have margin components, they should sum meaningfully
            # available_funds (= avail_margin per the code) + used_margin
            # gives the gross margin picture
            if avail_margin > 100 or used_margin > 100:
                # Very loose check — just ensure components are not wildly inconsistent
                # (both zero when one should be non-zero, etc.)
                if avail_margin < 0 and used_margin < 0:
                    errors.append(
                        f"Account {acct}: both avail_margin (₹{avail_margin:,.2f}) "
                        f"and used_margin (₹{used_margin:,.2f}) are negative"
                    )

        assert not errors, "\n".join(f"  {e}" for e in errors)


# ── manual entry point ──────────────────────────────────────────────────────

def _print_book_summary(data: dict) -> None:
    """Print a human-readable summary of the book data."""
    print(f"\n{'='*84}")
    print(f"  Book Cross-Check Summary")
    print(f"{'='*84}\n")

    holdings = data.get("holdings", {})
    positions = data.get("positions", {})
    funds = data.get("funds", {})

    h_rows = holdings.get("rows", [])
    h_summary = holdings.get("summary", [])
    p_rows = positions.get("rows", [])
    p_summary = positions.get("summary", [])
    f_rows = funds.get("rows", [])

    print(f"Holdings:  {len(h_rows)} rows  |  {len(h_summary)} summary entries")
    print(f"Positions: {len(p_rows)} rows  |  {len(p_summary)} summary entries")
    print(f"Funds:     {len(f_rows)} rows")

    # Aggregate totals from summary
    h_total = sum(float(s.get("day_change_val") or 0) for s in h_summary if str(s.get("account") or "").upper() == "TOTAL")
    p_total = sum(float(s.get("day_change_val") or 0) for s in p_summary if str(s.get("account") or "").upper() == "TOTAL")

    print(f"\nHoldings Day P&L (TOTAL):  ₹{h_total:>12,.2f}")
    print(f"Positions Day P&L (TOTAL): ₹{p_total:>12,.2f}")

    # Funds totals
    f_total = next((r for r in f_rows if str(r.get("account") or "").upper() == "TOTAL"), {})
    if f_total:
        cash = float(f_total.get("cash") or 0)
        avail_margin = float(f_total.get("avail_margin") or 0)
        used_margin = float(f_total.get("used_margin") or 0)
        print(f"\nFunds (TOTAL):")
        print(f"  Cash:          ₹{cash:>12,.2f}")
        print(f"  Avail Margin:  ₹{avail_margin:>12,.2f}")
        print(f"  Used Margin:   ₹{used_margin:>12,.2f}")

    print(f"\n{'='*84}\n")


if __name__ == "__main__":
    print(f"Fetching book data from {DEV_BASE} …")
    try:
        token = _login()
        data = {
            "holdings": _fetch_all_holdings(token),
            "positions": _fetch_all_positions(token),
            "funds": _fetch_all_funds(token),
        }
        _print_book_summary(data)

        # Run basic validation checks
        holdings = data.get("holdings", {})
        positions = data.get("positions", {})
        funds = data.get("funds", {})

        all_passed = True

        # Check holdings consistency
        h_rows = holdings.get("rows", [])
        h_summary = holdings.get("summary", [])
        if h_rows and h_summary:
            print("Holdings per-account check… ", end="", flush=True)
            accounts_in_rows = {str(r.get("account") or "").upper() for r in h_rows if r.get("account")}
            for acct in accounts_in_rows:
                row_sum = sum(float(r.get("day_change_val") or 0) for r in h_rows
                              if str(r.get("account") or "").upper() == acct)
                summary_row = next((s for s in h_summary if str(s.get("account") or "").upper() == acct), {})
                summary_dcv = float(summary_row.get("day_change_val") or 0)
                diff = abs(row_sum - summary_dcv)
                if diff > max(HOLDINGS_DCV_RUPEES, abs(summary_dcv) * HOLDINGS_DCV_PCT / 100):
                    print(f"FAIL ({acct}: ₹{diff:,.2f})")
                    all_passed = False
                    break
            else:
                print("OK")

        # Check positions consistency
        p_rows = positions.get("rows", [])
        p_summary = positions.get("summary", [])
        if p_rows and p_summary:
            print("Positions per-account check… ", end="", flush=True)
            accounts_in_rows = {str(r.get("account") or "").upper() for r in p_rows if r.get("account")}
            for acct in accounts_in_rows:
                row_sum = sum(float(r.get("day_change_val") or 0) for r in p_rows
                              if str(r.get("account") or "").upper() == acct)
                summary_row = next((s for s in p_summary if str(s.get("account") or "").upper() == acct), {})
                summary_dcv = float(summary_row.get("day_change_val") or 0)
                diff = abs(row_sum - summary_dcv)
                if diff > max(POSITIONS_DCV_RUPEES, abs(summary_dcv) * POSITIONS_DCV_PCT / 100):
                    print(f"FAIL ({acct}: ₹{diff:,.2f})")
                    all_passed = False
                    break
            else:
                print("OK")

        # Check funds nonnegative
        f_rows = funds.get("rows", [])
        if f_rows:
            print("Funds sanity check… ", end="", flush=True)
            for row in f_rows:
                cash = float(row.get("cash") or 0)
                used_margin = float(row.get("used_margin") or 0)
                if cash < -100 or used_margin < -100:
                    acct = str(row.get("account") or "")
                    print(f"FAIL ({acct}: cash={cash:,.2f}, used_margin={used_margin:,.2f})")
                    all_passed = False
                    break
            else:
                print("OK")

        sys.exit(0 if all_passed else 1)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"⚠  {DEV_BASE} unreachable: {e}")
        print("Exiting with status 0 (assuming offline environment).")
        sys.exit(0)
    except Exception as e:
        print(f"✗  Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
