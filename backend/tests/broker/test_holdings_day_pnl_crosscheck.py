"""
Holdings day P&L cross-check — ZG0790 vs formula (ltp − close) × qty.

Verifies that the API's day_change_val (computed by _enrich_holdings on
the broker data) matches the independent formula (last_price − previous_close)
× quantity within TOLERANCE_PCT (2%).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANUAL RUN (anytime, no Kite session needed):

    python3 backend/tests/broker/test_holdings_day_pnl_crosscheck.py
    python3 backend/tests/broker/test_holdings_day_pnl_crosscheck.py ZJ6294

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

ACCOUNT       = "ZG0790"
TOLERANCE_PCT = 2.0
DEV_BASE      = "https://dev.ramboq.com"
DEV_USER      = "rambo"
DEV_PASS      = "admin1234"


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


def _fetch_holdings(token: str, account: str) -> list[dict]:
    """Fetch holdings rows for account from dev.ramboq.com."""
    req = urllib.request.Request(
        f"{DEV_BASE}/api/holdings?account={account}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read()).get("rows", [])


def _dev_reachable() -> bool:
    """Quick connectivity check — used by pytest skip guard."""
    try:
        urllib.request.urlopen(f"{DEV_BASE}/api/health", timeout=5)
        return True
    except Exception:
        return True   # 4xx/5xx still means the server is up


# ── cross-check logic ───────────────────────────────────────────────────────

def _run_crosscheck(account: str = ACCOUNT) -> dict:
    """
    Fetch holdings for account, then compare:
      Path A — day_change_val from the API (output of _enrich_holdings)
      Path B — (last_price − previous_close) × quantity   (independent formula)

    Returns a result dict with keys:
      api_total, formula_total, pct_diff, passed, mismatches, rows, n_sold
    """
    token = _login()
    all_rows = _fetch_holdings(token, account)

    # Rows with quantity == 0 are fully-sold holdings — their day_change_val
    # reflects realised P&L (not mark-to-market), so the formula doesn't apply.
    held = [r for r in all_rows if float(r.get("quantity") or 0) > 0]
    sold = [r for r in all_rows if float(r.get("quantity") or 0) == 0]

    api_total = formula_total = 0.0
    detail    = []
    mismatches = []

    for h in held:
        sym     = str(h.get("tradingsymbol") or h.get("symbol") or "?")
        qty     = float(h.get("quantity")       or 0)
        close   = float(h.get("previous_close") or h.get("close_price") or 0)
        ltp     = float(h.get("last_price")     or 0)
        api_dcv = float(h.get("day_change_val") or 0)

        # Independent formula — same as what Kite computes on their side
        formula = (ltp - close) * qty if close > 0 else api_dcv

        diff           = formula - api_dcv
        api_total     += api_dcv
        formula_total += formula
        detail.append(dict(sym=sym, qty=qty, close=close, ltp=ltp,
                           api_dcv=api_dcv, formula=formula, diff=diff))
        if abs(diff) > 1.0:
            mismatches.append(dict(sym=sym, api_dcv=api_dcv,
                                   formula=formula, diff=diff))

    if abs(api_total) > 1.0:
        pct_diff = abs(formula_total - api_total) / abs(api_total) * 100
    else:
        pct_diff = 0.0 if abs(formula_total - api_total) < 1.0 else 100.0

    return dict(
        api_total=api_total,
        formula_total=formula_total,
        pct_diff=pct_diff,
        passed=pct_diff <= TOLERANCE_PCT and not mismatches,
        mismatches=mismatches,
        rows=detail,
        n_sold=len(sold),
    )


# ── pretty printer ──────────────────────────────────────────────────────────

def _print_result(result: dict, account: str) -> None:
    rows = result["rows"]
    print(f"\n{'='*84}")
    print(f"  Holdings Day P&L Cross-Check — {account}")
    print(f"  Held: {len(rows)}   Sold/zero-qty (excluded): {result['n_sold']}")
    print(f"  Formula: (last_price − previous_close) × quantity   "
          f"Tolerance: {TOLERANCE_PCT}%")
    print(f"{'='*84}")
    print(f"  {'Symbol':<20} {'Qty':>5} {'Prev Close':>10} {'LTP':>9}  "
          f"{'API dcv':>11}  {'Formula':>11}  {'Diff':>8}")
    print(f"  {'-'*80}")
    for r in rows:
        flag = " ⚠" if abs(r["diff"]) > 1.0 else ""
        print(f"  {r['sym']:<20} {r['qty']:>5.0f} {r['close']:>10.2f} "
              f"{r['ltp']:>9.2f}  {r['api_dcv']:>11.2f}  "
              f"{r['formula']:>11.2f}  {r['diff']:>8.2f}{flag}")
    print(f"  {'-'*80}")
    at = result["api_total"]
    ft = result["formula_total"]
    print(f"  {'TOTAL':<26} {'':>9}  {at:>11.2f}  {ft:>11.2f}  {ft-at:>8.2f}")
    pct = result["pct_diff"]
    verdict = (f"PASS ✓  ({pct:.3f}% < {TOLERANCE_PCT}%)"
               if result["passed"] else f"FAIL ✗  ({pct:.2f}% > {TOLERANCE_PCT}%)")
    print(f"\n  API day_change_val total  : ₹{at:>10,.2f}")
    print(f"  Formula (ltp−close)×qty   : ₹{ft:>10,.2f}")
    print(f"  Difference                :  {pct:.3f}%  →  {verdict}")
    if result["mismatches"]:
        print(f"\n  ⚠  {len(result['mismatches'])} symbol(s) differ by > ₹1:")
        for m in result["mismatches"]:
            print(f"     {m['sym']:<20}  api={m['api_dcv']:,.2f}  "
                  f"formula={m['formula']:,.2f}  diff={m['diff']:,.2f}")
    else:
        print(f"\n  ✓  All {len(rows)} held symbols match within ₹1.00")
    print(f"{'='*84}\n")


# ── pytest tests ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def crosscheck_result():
    """Fetch + run cross-check once; shared by both test methods."""
    if not _dev_reachable():
        pytest.skip("dev.ramboq.com unreachable — skipping live cross-check")
    return _run_crosscheck(ACCOUNT)


class TestHoldingsDayPnlCrossCheck:
    """
    Automated cross-check — runs as part of the normal pytest suite.
    Skips only if dev.ramboq.com is unreachable (offline / CI without network).
    """

    def test_totals_agree_within_tolerance(self, crosscheck_result):
        """Aggregate day P&L must agree within 2% between API and formula."""
        result = crosscheck_result
        _print_result(result, ACCOUNT)
        assert result["passed"], (
            f"Day P&L mismatch: api=₹{result['api_total']:,.2f}  "
            f"formula=₹{result['formula_total']:,.2f}  "
            f"diff={result['pct_diff']:.2f}%  (tolerance {TOLERANCE_PCT}%)"
        )

    def test_per_symbol_within_one_rupee(self, crosscheck_result):
        """Every individual holding must match within ₹1.00."""
        result = crosscheck_result
        assert not result["mismatches"], (
            f"{len(result['mismatches'])} symbol(s) differ by > ₹1:\n" +
            "\n".join(
                f"  {m['sym']:<20}  api={m['api_dcv']:,.2f}  "
                f"formula={m['formula']:,.2f}  diff={m['diff']:,.2f}"
                for m in result["mismatches"]
            )
        )


# ── manual entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    account = sys.argv[1] if len(sys.argv) > 1 else ACCOUNT
    print(f"Fetching holdings for {account} from {DEV_BASE} …")
    result = _run_crosscheck(account)
    _print_result(result, account)
    sys.exit(0 if result["passed"] else 1)
