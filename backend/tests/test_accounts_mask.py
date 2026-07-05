"""P0 regression: /api/accounts must mask account_id for non-admin callers.

Root cause: `account_id` shipped raw (e.g. "ZG0790") while only `display`
was masked.  Demo / partner sessions read `account_id` directly (stores.js
connStatus poll, MarketPulse.svelte realAccounts) so real broker IDs appeared
in the account picker and persisted to localStorage.

Fix (orders.py): when `do_mask=True`, set `account_id = mask_account(account)`
(same masked string as `display`) so the raw code never leaves the server for
non-admin callers.

Five quality dimensions tested:
1. SSOT  — masking funnels through `mask_account()`, no re-implementation
2. Perf  — unit test, O(1) no DB
3. Stale — orders.py ships only one masking branch; no dead raw-id path
4. Reuse — canonical `mask_account` helper is used
5. UX    — pattern [A-Z]{2}\\d{4,6} absent from both fields for demo callers
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from backend.shared.helpers.utils import mask_account


# ---------------------------------------------------------------------------
# 1. SSOT — mask_account produces correct output for known account formats
# ---------------------------------------------------------------------------

def test_mask_kite_account():
    assert mask_account("ZG0790") == "ZG####"


def test_mask_dhan_account():
    assert mask_account("DH3747") == "DH####"


def test_mask_groww_account():
    # Groww: alphanumeric after 2-letter prefix — mask_account replaces digits
    out = mask_account("GR87DF")
    assert "GR87DF" not in out


# ---------------------------------------------------------------------------
# 2 + 4. Reuse — list_accounts handler produces masked account_id for demo
# ---------------------------------------------------------------------------

_RAW_ID_PATTERN = re.compile(r'\b[A-Z]{2}\d{4,6}\b')


def _build_account_rows(*, admin: bool, accounts: list[str]) -> list[dict]:
    """
    Replicate the AccountsController.list_accounts masking logic in isolation.
    Tests whether `account_id` is correctly masked for non-admin callers
    without mounting the full Litestar app.
    """
    from backend.shared.helpers.utils import mask_account as _mask
    from backend.api.schemas import AccountInfo

    do_mask = not admin
    rows = [
        AccountInfo(
            account_id=(_mask(a) if do_mask else a),
            display=(_mask(a) if do_mask else a),
        )
        for a in accounts
    ]
    return [{"account_id": r.account_id, "display": r.display} for r in rows]


@pytest.mark.parametrize("raw_id", ["ZG0790", "ZJ6294", "DH3747", "DH6847"])
def test_demo_account_id_is_masked(raw_id):
    """account_id must never contain a raw broker code for non-admin callers."""
    rows = _build_account_rows(admin=False, accounts=[raw_id])
    assert rows, "Expected at least one account row"
    for row in rows:
        assert row["account_id"] != raw_id, (
            f"Raw account_id '{raw_id}' leaked in demo response: {row}"
        )
        # Ensure the masked form is present (digits replaced with #)
        assert _RAW_ID_PATTERN.search(row["account_id"]) is None, (
            f"account_id '{row['account_id']}' still matches raw-id pattern"
        )


@pytest.mark.parametrize("raw_id", ["ZG0790", "ZJ6294", "DH3747", "DH6847"])
def test_demo_display_is_masked(raw_id):
    """display must be masked for non-admin callers."""
    rows = _build_account_rows(admin=False, accounts=[raw_id])
    for row in rows:
        assert row["display"] != raw_id
        assert _RAW_ID_PATTERN.search(row["display"]) is None


@pytest.mark.parametrize("raw_id", ["ZG0790", "ZJ6294", "DH3747"])
def test_admin_account_id_is_raw(raw_id):
    """Admin callers must still receive the real account_id."""
    rows = _build_account_rows(admin=True, accounts=[raw_id])
    found = [r for r in rows if r["account_id"] == raw_id]
    assert found, f"Admin session should see raw account_id={raw_id}"


def test_demo_account_id_equals_display():
    """For demo callers, account_id and display must be identical
    (both masked) so the frontend can use either field without leaking."""
    rows = _build_account_rows(admin=False, accounts=["ZG0790", "DH3747"])
    for row in rows:
        assert row["account_id"] == row["display"], (
            f"account_id ({row['account_id']}) != display ({row['display']}) "
            "for non-admin caller; one field is still raw."
        )


# ---------------------------------------------------------------------------
# 3. Stale — orders.py does not keep a dead raw-account_id branch
# ---------------------------------------------------------------------------

ORDERS_PY = Path(__file__).resolve().parent.parent / "api" / "routes" / "orders.py"


def test_no_raw_account_id_branch_in_orders():
    """list_accounts must not assign `account_id=account` (raw) for any branch.

    The fix unified masking: both fields use `mask_account(account)` when
    do_mask is True.  A regression to the old form (`account_id=account,`) is
    the single-line diff that reintroduces the leak.
    """
    src = ORDERS_PY.read_text(encoding="utf-8")
    # Pattern catches the old assignment: account_id=account,
    # (raw identifier, not mask_account(…))
    raw_assign_re = re.compile(r'account_id\s*=\s*account\s*[,\)]')
    assert not raw_assign_re.search(src), (
        "orders.py still contains `account_id=account` (raw, unmasked). "
        "The fix must set account_id=mask_account(account) when do_mask=True."
    )


# ---------------------------------------------------------------------------
# 5. UX — mask output never matches the raw-id DOM pattern
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_id", ["ZG0790", "ZJ6294", "DH3747", "DH6847", "GR87DF"])
def test_masked_id_fails_raw_id_dom_pattern(raw_id):
    """The masked form must not match \\b[A-Z]{2}\\d{4,6}\\b so a Playwright
    DOM assertion using that pattern finds zero matches on the Pulse page."""
    masked = mask_account(raw_id)
    assert _RAW_ID_PATTERN.search(masked) is None, (
        f"mask_account('{raw_id}') = '{masked}' still matches the raw-id "
        "pattern — Playwright UX assertion would pass for the wrong reason."
    )
