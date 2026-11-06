"""2026-11 alert-noise-reduction plan, Component E — the operator's reported
unmasked-account bug: `_fire_guard_alert()` (`template_attach.py`) passed the
RAW `parent_account` into its `logger.info(..., extra={...})` dict even
though its sibling `template_guard_message()` already masks the same value
for its own display string. A raw account code in `extra` reaches
`log_events.extra` verbatim — `_AccountMaskFilter` (`log_store.py`) only
masks the rendered message TEXT, never a structured `extra` value (see
CLAUDE.md's "Account-masking campaign" + "Alert routing hygiene" entries) —
so this was a real, not theoretical, leak into the DB and anything that
reads `log_events.extra` downstream.

This test reconstructs the real logging record via the actual code path
(a real `LogStoreHandler` attached to the real `template_attach` logger,
same pattern as `test_log_store.py::test_order_failure_and_summary_records_reach_the_queue`)
and asserts on the record's own `extra` dict — not just the rendered
message string, which was already correctly masked before this fix.
"""
from __future__ import annotations

import logging

from backend.shared.helpers import log_store
from backend.shared.helpers.utils import mask_account

RAW_ACCOUNT = "ZG0790"


def _fire_and_capture(**over):
    from backend.api.algo.template_attach import _fire_guard_alert

    kwargs = dict(
        template_slug="tmpl-1", applies_to="buy_option", parent_side="BUY",
        parent_symbol="NIFTY24SEPFUT", parent_account=RAW_ACCOUNT, parent_qty=1,
        parent_fill_price=100.0, parent_order_id=123, reason="normal reason text",
        agent_id=None,
    )
    kwargs.update(over)

    h = log_store.LogStoreHandler()
    target_logger = logging.getLogger("backend.api.algo.template_attach")
    target_logger.addHandler(h)
    target_logger.setLevel(logging.INFO)
    try:
        _fire_guard_alert(**kwargs)
    finally:
        target_logger.removeHandler(h)

    rows = h.drain(10)
    assert rows, "guard alert record never reached the queue"
    return rows[0]


def test_extra_dict_never_carries_the_raw_account():
    """The real bug: `extra["parent_account"]` was the raw, un-masked value."""
    row = _fire_and_capture()
    extra = row["extra"] or {}
    assert extra.get("parent_account") != RAW_ACCOUNT
    assert extra.get("parent_account") == mask_account(RAW_ACCOUNT)


def test_no_unmasked_account_shaped_string_anywhere_in_the_record():
    """Belt-and-suspenders — walk every value in the record's extra dict
    (not just the known key) and confirm the raw account string is not a
    substring of any of them, matching the plan's "reconstruct a log
    record via the real code path and assert on the record's
    extra/__dict__, not just the rendered message string" requirement."""
    row = _fire_and_capture()
    extra = row["extra"] or {}
    for key, value in extra.items():
        assert RAW_ACCOUNT not in str(value), (
            f"raw account leaked into extra[{key!r}] = {value!r}"
        )
    # The rendered message text was already correctly masked before this
    # fix (via `summary`, built from the same masked value) — confirm it
    # stays that way now that both reuse one masked value.
    assert RAW_ACCOUNT not in row["message"]


def test_message_text_also_uses_the_masked_value():
    """`summary` (the log message body) is built from the same masked
    value as `extra["parent_account"]` — confirms they can never disagree
    (one masked value, reused twice, not two independent computations)."""
    row = _fire_and_capture()
    assert mask_account(RAW_ACCOUNT) in row["message"]
