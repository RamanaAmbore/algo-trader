"""2026-10 notification-masking audit, Wave 2 cluster 4 (defense-in-depth
bundle) — three `event_agents.py` renderers read free-text fields
(`errors`, `reason`, and a raw `extra`-dict fallback dump) without
scrubbing them for an embedded broker account code, unlike
`order_failure_messages()`'s existing pattern. Each is now scrubbed with
`mask_account_in_text()`.
"""
from __future__ import annotations

from backend.api.algo.event_agents import (
    _render_partial_gtt,
    _render_template_attach,
    _render_gtt_not_accepted,
)

RAW_ACCOUNT = "ZJ6294"


class TestRenderPartialGttMasksErrors:
    def test_errors_list_scrubbed(self):
        rec = {"extra": {
            "parent_row_id": 1, "parent_symbol": "NIFTY26OCTFUT",
            "placed": 1, "planned": 2,
            "errors": [f"could not resolve broker for {RAW_ACCOUNT!r}: boom"],
            "agent_id": None,
        }}
        title, body = _render_partial_gtt(rec)
        assert title == "Partial GTT placement"
        assert RAW_ACCOUNT not in body


class TestRenderTemplateAttachMasksReason:
    def test_wing_skip_reason_scrubbed(self):
        rec = {"extra": {
            "alert_event": "wing_skip",
            "reason": f"account {RAW_ACCOUNT} margin insufficient",
            "parent_order_id": 5, "symbol": "NIFTY26OCTFUT", "exchange": "NFO",
        }}
        title, body = _render_template_attach(rec)
        assert title == "Wing attach skipped"
        assert RAW_ACCOUNT not in body

    def test_wing_hard_reject_reason_scrubbed(self):
        rec = {"extra": {
            "alert_event": "wing_hard_reject",
            "reason": f"account {RAW_ACCOUNT} rejected",
            "symbol": "NIFTY26OCTFUT", "exchange": "NFO", "target_premium": 12.5,
        }}
        title, body = _render_template_attach(rec)
        assert title == "Wing scan hard-rejected"
        assert RAW_ACCOUNT not in body

    def test_wing_unprotected_reason_scrubbed(self):
        rec = {"extra": {
            "alert_event": "wing_unprotected",
            "gtt_ids_text": "1,2", "reason": f"wing failed for {RAW_ACCOUNT}",
            "parent_order_id": 5, "symbol": "NIFTY26OCTFUT", "exchange": "NFO",
        }}
        title, body = _render_template_attach(rec)
        assert title == "Unprotected SELL position"
        assert RAW_ACCOUNT not in body

    def test_wing_offset_skip_reason_scrubbed(self):
        rec = {"extra": {
            "alert_event": "wing_offset_skip",
            "reason": f"skipped for {RAW_ACCOUNT}",
            "parent_order_id": 5, "symbol": "NIFTY26OCTFUT", "exchange": "NFO",
        }}
        title, body = _render_template_attach(rec)
        assert title == "Wing offset attach skipped"
        assert RAW_ACCOUNT not in body

    def test_fallback_dump_scrubbed(self):
        rec = {"extra": {"account": RAW_ACCOUNT, "note": "unrecognized event"}}
        title, body = _render_template_attach(rec)
        assert title == "Template attach"
        assert RAW_ACCOUNT not in body


class TestRenderGttNotAcceptedMasksReason:
    def test_reason_scrubbed(self):
        rec = {"extra": {
            "symbol": "NIFTY26OCTFUT", "label": "TP", "gtt_id": "G1",
            "reason": f"status mismatch for {RAW_ACCOUNT}",
            "agent_id": None,
        }}
        title, body = _render_gtt_not_accepted(rec)
        assert title == "GTT not accepted at broker"
        assert RAW_ACCOUNT not in body
