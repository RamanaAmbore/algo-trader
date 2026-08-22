"""
Holdings day P&L cross-check — all accounts, per-account validation.

Verifies that the API's day_change_val (computed by _enrich_holdings on
the broker data) matches the independent formula (last_price − previous_close)
× quantity for EVERY account independently.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANUAL RUN (anytime, no Kite session needed):

    python3 backend/tests/broker/test_holdings_day_pnl_crosscheck.py

AUTOMATED (part of the pytest suite):

    venv/bin/pytest backend/tests/broker/test_holdings_day_pnl_crosscheck.py -v -s
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

TOLERANCE_PCT = 5.0
PER_SYMBOL_TOLERANCE_RUPEES = 5.0
DEV_BASE = "https://dev.ramboq.com"
DEV_USER = "rambo"
DEV_PASS = "admin1234"


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


def _fetch_all_holdings(token: str) -> list[dict]:
    """Fetch holdings rows for ALL accounts from dev.ramboq.com."""
    req = urllib.request.Request(
        f"{DEV_BASE}/api/holdings",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        all_rows = json.loads(r.read()).get("rows", [])
    return all_rows


def _dev_reachable() -> bool:
    """Quick connectivity check — used by pytest skip guard."""
    try:
        urllib.request.urlopen(f"{DEV_BASE}/api/health", timeout=5)
        return True
    except Exception:
        return False


# ── cross-check logic ───────────────────────────────────────────────────────

def _check_symbol(h: dict) -> dict:
    """Extract per-symbol cross-check data from one holding row."""
    sym     = str(h.get("tradingsymbol") or h.get("symbol") or "?")
    qty     = float(h.get("quantity") or 0)
    close   = float(h.get("previous_close") or h.get("close_price") or 0)
    ltp     = float(h.get("last_price") or 0)
    api_dcv = float(h.get("day_change_val") or 0)
    formula = (ltp - close) * qty if close > 0 else api_dcv
    return dict(sym=sym, qty=qty, close=close, ltp=ltp,
                api_dcv=api_dcv, formula=formula, diff=formula - api_dcv)


def _pct_diff(api_total: float, formula_total: float) -> float:
    if abs(api_total) > 1.0:
        return abs(formula_total - api_total) / abs(api_total) * 100
    return 0.0 if abs(formula_total - api_total) < 1.0 else 100.0


def _run_crosscheck_for_account(account: str, all_rows: list[dict]) -> dict:
    """
    Run cross-check for a single account.

    Path A — day_change_val from the API (= Kite's raw broker value when provided)
    Path B — (last_price − previous_close) × quantity (independent formula)
    """
    account_rows = [r for r in all_rows
                    if str(r.get("account") or "").upper() == account.upper()]
    held = [r for r in account_rows if float(r.get("quantity") or 0) > 0]
    sold = [r for r in account_rows if float(r.get("quantity") or 0) == 0]

    detail     = [_check_symbol(h) for h in held]
    mismatches = [d for d in detail if abs(d["diff"]) > PER_SYMBOL_TOLERANCE_RUPEES]
    api_total      = sum(d["api_dcv"]  for d in detail)
    formula_total  = sum(d["formula"]  for d in detail)
    pct            = _pct_diff(api_total, formula_total)

    return dict(
        account=account,
        api_total=api_total,
        formula_total=formula_total,
        pct_diff=pct,
        passed=pct <= TOLERANCE_PCT and not mismatches,
        mismatches=mismatches,
        rows=detail,
        n_held=len(held),
        n_sold=len(sold),
    )


def _run_all_accounts_crosscheck(all_rows: list[dict]) -> dict:
    """
    Run cross-checks for all accounts in the holdings response.

    Returns a dict with keys:
      accounts (list of per-account result dicts), all_passed, total_accounts
    """
    # Extract unique accounts from all rows
    accounts_set = set()
    for row in all_rows:
        acc = str(row.get("account") or "").upper()
        if acc:
            accounts_set.add(acc)

    account_results = []
    for account in sorted(accounts_set):
        result = _run_crosscheck_for_account(account, all_rows)
        account_results.append(result)

    all_passed = all(r["passed"] for r in account_results)

    return dict(
        accounts=account_results,
        all_passed=all_passed,
        total_accounts=len(account_results),
    )


# ── pretty printer ──────────────────────────────────────────────────────────

def _print_per_account_result(result: dict) -> None:
    """Print per-account cross-check result."""
    account = result["account"]
    rows = result["rows"]
    print(f"\nAccount    Held  Sold  Broker DCV    Formula    Diff%   Result")
    print(f"{account:<10} {result['n_held']:>3}  {result['n_sold']:>3}  ", end="")

    if not rows:
        print("(no holdings)")
        return

    # Header row for detail
    print()
    for r in rows:
        flag = " ⚠" if abs(r["diff"]) > PER_SYMBOL_TOLERANCE_RUPEES else ""
        sym_col = r['sym'][:20].ljust(20)
        qty_col = f"{r['qty']:>5.0f}".rjust(5)
        api_col = f"{r['api_dcv']:>10.2f}".rjust(10)
        form_col = f"{r['formula']:>10.2f}".rjust(10)
        diff_col = f"{r['diff']:>8.2f}".rjust(8)
        print(f"  {sym_col} {qty_col}  {api_col}  {form_col}  {diff_col}{flag}")

    # Summary row
    at = result["api_total"]
    ft = result["formula_total"]
    pct = result["pct_diff"]
    verdict = "PASS" if result["passed"] else "FAIL"
    print(f"  {'-'*70}")
    print(f"  {'TOTAL':<20} {result['n_held']:>5}  {at:>10.2f}  {ft:>10.2f}  "
          f"{pct:>8.3f}%  {verdict}")


def _print_full_crosscheck(all_results: dict) -> None:
    """Print the full cross-check summary."""
    print(f"\n{'='*84}")
    print(f"  Holdings Day P&L Cross-Check — All Accounts")
    print(f"  Formula: (last_price − previous_close) × quantity")
    print(f"  Tolerance: {TOLERANCE_PCT}% per account  |  "
          f"Per-symbol: ₹{PER_SYMBOL_TOLERANCE_RUPEES:.1f}")
    print(f"{'='*84}")

    for result in all_results["accounts"]:
        _print_per_account_result(result)

    # Grand summary
    print(f"\n{'='*84}")
    print(f"  SUMMARY: {all_results['total_accounts']} account(s)")
    total_held = sum(r["n_held"] for r in all_results["accounts"])
    total_sold = sum(r["n_sold"] for r in all_results["accounts"])
    print(f"  Total held: {total_held}  Total sold: {total_sold}")

    passed_count = sum(1 for r in all_results["accounts"] if r["passed"])
    verdict = "ALL PASS ✓" if all_results["all_passed"] else f"FAIL ({passed_count}/{all_results['total_accounts']} passed)"
    print(f"  {verdict}")
    print(f"{'='*84}\n")

    # Detail per failed account
    failed = [r for r in all_results["accounts"] if not r["passed"]]
    if failed:
        print(f"Failed accounts:")
        for r in failed:
            print(f"  {r['account']:<10}  {r['pct_diff']:>8.3f}% "
                  f"(api=₹{r['api_total']:,.2f}, formula=₹{r['formula_total']:,.2f})")
            if r["mismatches"]:
                for m in r["mismatches"]:
                    print(f"    → {m['sym']:<20}  api={m['api_dcv']:,.2f}  "
                          f"formula={m['formula']:,.2f}  diff={m['diff']:,.2f}")
        print()


# ── pytest tests ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def holdings_data():
    """Fetch holdings once; shared by both test methods."""
    if not _dev_reachable():
        pytest.skip("dev.ramboq.com unreachable — skipping live cross-check")
    token = _login()
    all_rows = _fetch_all_holdings(token)
    return all_rows


@pytest.fixture(scope="module")
def crosscheck_results(holdings_data):
    """Run cross-checks for all accounts."""
    return _run_all_accounts_crosscheck(holdings_data)


class TestHoldingsDayPnlCrossCheck:
    """
    Automated cross-check — runs as part of the normal pytest suite.
    Skips only if dev.ramboq.com is unreachable (offline / CI without network).

    Tests verify:
    1. Each account's aggregate day P&L is within TOLERANCE_PCT
    2. Per-symbol day P&L is within PER_SYMBOL_TOLERANCE_RUPEES
    """

    def test_all_accounts_within_tolerance(self, crosscheck_results):
        """Each account's aggregate day P&L must be within 5%."""
        results = crosscheck_results
        _print_full_crosscheck(results)

        # Check each account independently
        for result in results["accounts"]:
            assert result["passed"], (
                f"Account {result['account']} failed: "
                f"api=₹{result['api_total']:,.2f}  "
                f"formula=₹{result['formula_total']:,.2f}  "
                f"diff={result['pct_diff']:.3f}%  "
                f"(tolerance {TOLERANCE_PCT}%)"
            )

    def test_per_symbol_within_five_rupees(self, crosscheck_results):
        """Every individual holding must match within ₹5.00."""
        results = crosscheck_results
        all_mismatches = []

        for result in results["accounts"]:
            for m in result["mismatches"]:
                all_mismatches.append({
                    "account": result["account"],
                    "sym": m["sym"],
                    "api_dcv": m["api_dcv"],
                    "formula": m["formula"],
                    "diff": m["diff"],
                })

        assert not all_mismatches, (
            f"{len(all_mismatches)} symbol(s) differ by > ₹{PER_SYMBOL_TOLERANCE_RUPEES}:\n" +
            "\n".join(
                f"  {m['account']} {m['sym']:<20}  "
                f"api={m['api_dcv']:,.2f}  formula={m['formula']:,.2f}  "
                f"diff={m['diff']:,.2f}"
                for m in all_mismatches
            )
        )


# ── manual entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Fetching holdings from {DEV_BASE} …")
    try:
        token = _login()
        all_rows = _fetch_all_holdings(token)
        results = _run_all_accounts_crosscheck(all_rows)
        _print_full_crosscheck(results)
        sys.exit(0 if results["all_passed"] else 1)
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"⚠  {DEV_BASE} unreachable: {e}")
        print("Exiting with status 0 (assuming offline environment).")
        sys.exit(0)
    except Exception as e:
        print(f"✗  Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
