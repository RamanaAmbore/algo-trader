"""Tier 1 / A5 — `mask_account_in_text` consolidation tests.

Two inline `_mask_payload` definitions used to live inside
`backend/api/routes/orders.py` (lines 2224 + 2295). Both used a naive
regex `\\b([A-Z]{2})\\d{4,8}\\b → \\1####` which:

  • Bypassed the canonical ordinal-aware `mask_account()` registry
    (DH3747 → D1####, DH6847 → D2####), collapsing both Dhan accounts
    to DH####.
  • Missed Groww codes like GR87DF (has letters after the 2-letter
    prefix; the `\\d{4,8}` digit class refused to match).

The consolidated helper `mask_account_in_text()` lives in
`backend/shared/helpers/utils.py` and routes every match through
`mask_account()` so the registry-aware mask is preserved.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.shared.helpers.utils import (
    mask_account_in_text,
    register_accounts,
)


def test_masks_simple_zerodha_code():
    raw = '{"account": "ZG0790", "price": 100}'
    out = mask_account_in_text(raw)
    assert "ZG0790" not in out
    # No registry → scalar mask → digits replaced with #
    assert "ZG####" in out


def test_masks_groww_alphanumeric_code():
    # Groww codes contain letters after the 2-letter prefix — the OLD
    # `\b([A-Z]{2})\d{4,8}\b` regex would have missed this and leaked
    # the raw code to demo viewers.
    raw = '{"account": "GR87DF", "qty": 5}'
    out = mask_account_in_text(raw)
    assert "GR87DF" not in out


def test_registry_aware_dhan_ordinal_preserved():
    # Register two Dhan accounts so the canonical mask flips to the
    # ordinal-disambiguator (D1####, D2####).
    register_accounts(["DH3747", "DH6847", "ZG0790"])
    try:
        raw = '{"primary": "DH3747", "secondary": "DH6847"}'
        out = mask_account_in_text(raw)
        # The naive inline regex used to render both Dhan codes as
        # `DH####`. The canonical mask renders them as D1####, D2####.
        assert "DH3747" not in out
        assert "DH6847" not in out
        assert "D1####" in out
        assert "D2####" in out
    finally:
        # Reset registry so other tests aren't affected.
        register_accounts([])


def test_passes_through_none_and_empty():
    assert mask_account_in_text(None) is None
    assert mask_account_in_text("") == ""


def test_no_match_passes_through_unchanged():
    raw = '{"price": 100.5, "qty": 5}'
    assert mask_account_in_text(raw) == raw


# ---------------------------------------------------------------------------
# SSOT — orders.py no longer ships the inline `_mask_payload` defects
# ---------------------------------------------------------------------------

ORDERS_FILE = Path(__file__).resolve().parent.parent / "api" / "routes" / "orders.py"


def test_no_inline_mask_payload_in_orders_route():
    """The old `_mask_payload` inline regex no longer lives in orders.py."""
    src = ORDERS_FILE.read_text(encoding="utf-8")
    assert "_mask_payload" not in src, (
        "Inline `_mask_payload` helper resurfaced in orders.py. "
        "Use `mask_account_in_text` from shared.helpers.utils instead."
    )
    # And the naive regex pattern (only matches 2 letters + 4-8 DIGITS) is gone.
    naive = re.compile(r"\\b\(\[A-Z\]\{2\}\)\\d\{4,8\}\\b")
    assert not naive.search(src), (
        "Naive `[A-Z]{2}\\d{4,8}` regex still present in orders.py. "
        "This pattern misses Groww-style alphanumeric codes (GR87DF)."
    )
