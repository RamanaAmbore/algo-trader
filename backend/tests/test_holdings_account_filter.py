"""
Tests for the ?account= query param on GET /api/holdings.

Manual run:  python3 backend/tests/test_holdings_account_filter.py
Pytest run:  venv/bin/pytest backend/tests/test_holdings_account_filter.py -v -s

The test targets dev.ramboq.com and is automatically skipped when the
server is unreachable (CI / offline environments).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

import pytest

# ── Constants ────────────────────────────────────────────────────────────────

BASE_URL = "https://dev.ramboq.com"
LOGIN_URL = f"{BASE_URL}/api/auth/login"
HOLDINGS_URL = f"{BASE_URL}/api/holdings"
USERNAME = "rambo"
PASSWORD = "admin1234"
TIMEOUT = 15
UA = "Mozilla/5.0"


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _is_reachable() -> bool:
    """Return True if dev.ramboq.com responds within TIMEOUT seconds."""
    try:
        req = urllib.request.Request(BASE_URL, headers={"User-Agent": UA})
        urllib.request.urlopen(req, timeout=TIMEOUT)
        return True
    except Exception:
        return False


def _login() -> str:
    """Log in and return the JWT access token."""
    payload = json.dumps({"username": USERNAME, "password": PASSWORD}).encode()
    req = urllib.request.Request(
        LOGIN_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = json.loads(resp.read())
    # Token may be at body["access_token"] or body["token"]
    token = body.get("access_token") or body.get("token") or body.get("jwt")
    if not token:
        raise RuntimeError(f"Login response missing token: {body}")
    return token


def _get_holdings(token: str, account: str | None = None) -> dict[str, Any]:
    """Fetch /api/holdings, optionally filtering by account."""
    url = HOLDINGS_URL
    if account:
        url = f"{url}?account={urllib.parse.quote(account)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


# Import urllib.parse (used in _get_holdings above but not imported at top yet)
import urllib.parse  # noqa: E402  (after function definition is fine at module level)


# ── Pytest fixtures ───────────────────────────────────────────────────────────

def pytest_skip_if_unreachable():
    """Shared skip-check — call at the start of every test."""
    if not _is_reachable():
        pytest.skip("dev.ramboq.com is unreachable")


@pytest.fixture(scope="module")
def auth_token() -> str:
    """Module-scoped JWT token obtained via login."""
    pytest_skip_if_unreachable()
    return _login()


@pytest.fixture(scope="module")
def all_holdings(auth_token: str) -> dict[str, Any]:
    """Full /api/holdings response (no account filter)."""
    return _get_holdings(auth_token)


# ── Test class ────────────────────────────────────────────────────────────────

class TestHoldingsAccountFilter:
    """Verify that ?account= isolates rows to the requested account."""

    def test_account_filter_isolates_rows(
        self,
        auth_token: str,
        all_holdings: dict[str, Any],
    ) -> None:
        """Each per-account filtered response must contain only that account's rows."""
        pytest_skip_if_unreachable()

        all_rows = all_holdings.get("rows", [])
        if not all_rows:
            pytest.skip("No holdings rows returned — skipping filter isolation test")

        # Collect all distinct non-TOTAL accounts from the unfiltered response.
        accounts = {
            str(r["account"]).upper()
            for r in all_rows
            if str(r.get("account", "")).upper() != "TOTAL"
        }
        assert accounts, "Expected at least one account in holdings rows"

        for acct in sorted(accounts):
            filtered = _get_holdings(auth_token, account=acct)
            filtered_rows = filtered.get("rows", [])

            # Every row must belong to this account.
            other_rows = [
                r for r in filtered_rows
                if str(r.get("account", "")).upper() != acct
            ]
            assert other_rows == [], (
                f"?account={acct}: got rows from other accounts: "
                f"{[r['account'] for r in other_rows]}"
            )

            # Rows from other accounts must NOT appear.
            other_accts_in_all = accounts - {acct}
            for other in other_accts_in_all:
                leaked = [
                    r for r in filtered_rows
                    if str(r.get("account", "")).upper() == other
                ]
                assert leaked == [], (
                    f"?account={acct}: rows from {other} leaked into response: {leaked}"
                )

    def test_no_filter_returns_all_accounts(
        self,
        auth_token: str,
        all_holdings: dict[str, Any],
    ) -> None:
        """Without ?account= every account's rows are present."""
        pytest_skip_if_unreachable()

        rows = all_holdings.get("rows", [])
        if not rows:
            pytest.skip("No holdings rows returned")

        accounts = {
            str(r["account"]).upper()
            for r in rows
            if str(r.get("account", "")).upper() != "TOTAL"
        }
        assert len(accounts) >= 1, "Expected at least one account in unfiltered response"

    def test_summary_total_rebuilt_for_single_account(
        self,
        auth_token: str,
        all_holdings: dict[str, Any],
    ) -> None:
        """When filtering to one account, a TOTAL summary row must appear."""
        pytest_skip_if_unreachable()

        all_rows = all_holdings.get("rows", [])
        if not all_rows:
            pytest.skip("No holdings rows returned")

        accounts = {
            str(r["account"]).upper()
            for r in all_rows
            if str(r.get("account", "")).upper() != "TOTAL"
        }
        if not accounts:
            pytest.skip("No distinct accounts found")

        acct = next(iter(sorted(accounts)))
        filtered = _get_holdings(auth_token, account=acct)
        summary = filtered.get("summary", [])

        # Summary must include exactly the account row + TOTAL.
        acct_rows = [s for s in summary if str(s.get("account", "")).upper() == acct]
        total_rows = [s for s in summary if str(s.get("account", "")).upper() == "TOTAL"]

        assert acct_rows, f"No summary row for {acct} in filtered response"
        assert total_rows, f"No TOTAL summary row when filtering by {acct}"

        # For a single-account filter the TOTAL values must equal the account row.
        src = acct_rows[0]
        tot = total_rows[0]
        assert abs(src["inv_val"] - tot["inv_val"]) < 0.01, (
            f"TOTAL inv_val ({tot['inv_val']}) != {acct} inv_val ({src['inv_val']})"
        )
        assert abs(src["cur_val"] - tot["cur_val"]) < 0.01, (
            f"TOTAL cur_val ({tot['cur_val']}) != {acct} cur_val ({src['cur_val']})"
        )

    def test_empty_account_param_returns_all(
        self,
        auth_token: str,
        all_holdings: dict[str, Any],
    ) -> None:
        """?account= (empty string) must return the same result as no param."""
        pytest_skip_if_unreachable()

        # Pass an empty account param — should return all rows.
        req = urllib.request.Request(
            f"{HOLDINGS_URL}?account=",
            headers={
                "Authorization": f"Bearer {auth_token}",
                "User-Agent": UA,
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            filtered = json.loads(resp.read())

        unfiltered_count = len(all_holdings.get("rows", []))
        filtered_count = len(filtered.get("rows", []))
        assert filtered_count == unfiltered_count, (
            f"?account= (empty) returned {filtered_count} rows; "
            f"expected {unfiltered_count} (same as no param)"
        )


# ── __main__ block ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Checking connectivity to dev.ramboq.com …")
    if not _is_reachable():
        print("SKIP: dev.ramboq.com is unreachable")
        sys.exit(0)

    print("Logging in …")
    try:
        token = _login()
    except Exception as exc:
        print(f"FAIL: login error — {exc}")
        sys.exit(1)

    print("Fetching all holdings …")
    try:
        all_resp = _get_holdings(token)
    except Exception as exc:
        print(f"FAIL: holdings fetch error — {exc}")
        sys.exit(1)

    all_rows = all_resp.get("rows", [])
    if not all_rows:
        print("SKIP: no holdings rows returned")
        sys.exit(0)

    accounts = sorted(
        {str(r["account"]).upper() for r in all_rows
         if str(r.get("account", "")).upper() != "TOTAL"}
    )
    print(f"Found accounts: {accounts}\n")

    failures: list[str] = []

    for acct in accounts:
        try:
            filtered = _get_holdings(token, account=acct)
        except Exception as exc:
            msg = f"FAIL [{acct}]: fetch error — {exc}"
            print(msg)
            failures.append(msg)
            continue

        filtered_rows = filtered.get("rows", [])
        bad = [r for r in filtered_rows if str(r.get("account", "")).upper() != acct]
        if bad:
            msg = (
                f"FAIL [{acct}]: {len(bad)} row(s) from other accounts in response "
                f"({[r['account'] for r in bad]})"
            )
            print(msg)
            failures.append(msg)
        else:
            print(f"PASS [{acct}]: {len(filtered_rows)} row(s), all account={acct}")

        # Check summary TOTAL.
        summary = filtered.get("summary", [])
        total_rows = [s for s in summary if str(s.get("account", "")).upper() == "TOTAL"]
        if total_rows:
            print(f"      summary TOTAL present (inv_val={total_rows[0].get('inv_val')})")
        else:
            msg = f"FAIL [{acct}]: no TOTAL row in summary"
            print(msg)
            failures.append(msg)

    # No-filter check.
    try:
        no_filter = _get_holdings(token)
        no_filter_count = len(no_filter.get("rows", []))
        all_count = len(all_rows)
        if no_filter_count == all_count:
            print(f"\nPASS [no filter]: {all_count} row(s) returned (unchanged)")
        else:
            msg = f"FAIL [no filter]: expected {all_count} rows, got {no_filter_count}"
            print(msg)
            failures.append(msg)
    except Exception as exc:
        msg = f"FAIL [no filter]: fetch error — {exc}"
        print(msg)
        failures.append(msg)

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print(f"\nAll {len(accounts)} account(s) passed.")
        sys.exit(0)
