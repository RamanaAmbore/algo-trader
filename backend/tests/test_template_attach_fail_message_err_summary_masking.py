"""2026-10 notification-masking audit, Wave 2 cluster 4 (defense-in-depth
bundle) — `template_attach.py:template_attach_fail_message`'s `err_summary`
param is raw broker/guard text that can embed an account code (e.g. a
broker-resolution failure message). It was only HTML-escaped, never
account-masked, unlike `order_failure_messages()`'s existing pattern.
"""
from __future__ import annotations

from backend.api.algo.template_attach import template_attach_fail_message
from backend.shared.helpers.utils import mask_account

RAW_ACCOUNT = "ZJ6294"


def test_err_summary_account_masked():
    msg = template_attach_fail_message(
        order_id=1088, symbol="NIFTY26OCTFUT", account="ZG0790",
        err_summary=f"could not resolve broker for {RAW_ACCOUNT!r}: boom",
        ist_label="Tue, Oct 06 2026, 14:30 IST",
    )
    assert RAW_ACCOUNT not in msg
    assert mask_account(RAW_ACCOUNT) in msg


def test_existing_no_account_case_unaffected():
    """Regression guard — the pre-existing no-account case still renders
    the error text, now plain (2026-10 plain-text reversal — no more
    HTML-escaping, so "qty < lot_size" is no longer "qty &lt; lot_size")."""
    msg = template_attach_fail_message(
        order_id=1088, symbol="NIFTY26OCTFUT", account="ZG0790",
        err_summary="qty < lot_size", ist_label="Tue, Oct 06 2026, 14:30 IST",
    )
    assert "qty < lot_size" in msg
    assert "&lt;" not in msg
