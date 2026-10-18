"""P1 fix (2026-10 audit, Group C items 8 & 9) —

Item 8: `dispatch_payload`'s ntfy/lock-screen TITLE used to be the bare
generic `"Agent"` prefix for EVERY alert ("Agent — 09:14:22 IST"), with
zero agent name/metric/account visible before opening the notification.
When the real triggering agent's name is known, the title now uses it.

Item 9: the email SUBJECT used to duplicate "Agent: Agent: {name}
(#{id})" — `alert_utils.py`'s base email prefix is already
"RamboQuant Agent: ", and `agent_engine.py:_v2_send_rich_alert`'s
`subject_detail` was ALSO `format_notification_header`'s "Agent: {name}
(#{id})" first line, concatenated with no separator.
"""
from __future__ import annotations

from unittest.mock import patch

from backend.shared.helpers.alert_utils import dispatch_payload


class TestDispatchPayloadTitleUsesRealAgentName:
    def test_title_uses_agent_name_when_both_name_and_id_given(self):
        with patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):
            payload = dispatch_payload(
                'alert', '09:14:22 IST', 'tg_table', '<html></html>', 'subject detail',
                agent_name='loss-margin-low', agent_id=42,
            )
        assert payload["title"] == "loss-margin-low — 09:14:22 IST"
        assert "Agent —" not in payload["title"]

    def test_title_falls_back_to_generic_when_no_agent_identity(self):
        """Summaries (open/close) and any caller with no single
        originating agent keep the prior generic-prefix behaviour."""
        with patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):
            payload = dispatch_payload(
                'alert', '09:14:22 IST', 'tg_table', '<html></html>', 'subject detail',
            )
        assert payload["title"] == "Agent — 09:14:22 IST"

    def test_title_falls_back_when_agent_id_missing_even_with_name(self):
        with patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):
            payload = dispatch_payload(
                'alert', '09:14:22 IST', 'tg_table', '<html></html>', 'subject detail',
                agent_name='loss-margin-low', agent_id=None,
            )
        assert payload["title"] == "Agent — 09:14:22 IST"

    def test_title_keeps_simulator_prefix_with_real_agent_name(self):
        with patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):
            payload = dispatch_payload(
                'alert', '09:14:22 IST', 'tg_table', '<html></html>', 'subject detail',
                sim_mode=True, agent_name='loss-margin-low', agent_id=42,
            )
        assert payload["title"] == "SIMULATOR loss-margin-low — 09:14:22 IST"

    def test_summary_title_unaffected_no_agent_concept(self):
        """Open/close summaries never pass agent_name/agent_id — title
        stays exactly as before this fix."""
        with patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):
            payload = dispatch_payload(
                'open', '09:14:22 IST', 'tg_table', '<html></html>', 'Summary — 09:14:22 IST',
            )
        assert payload["title"] == "Open Summary — 09:14:22 IST"


class TestAgentEngineSubjectNoLongerDuplicatesAgentPrefix:
    def test_subject_detail_does_not_duplicate_agent_prefix(self):
        """Reproduces the exact concatenation agent_engine.py's
        `_v2_send_rich_alert` feeds into dispatch_payload's
        subject_detail — must not read 'Agent: Agent:'."""
        from backend.shared.helpers.alert_utils import format_notification_header

        header_line = format_notification_header("loss-margin-low", 42).splitlines()[0]
        subject_detail = (header_line[len("Agent: "):]
                          if header_line.startswith("Agent: ") else header_line)
        with patch('backend.shared.helpers.alert_utils.config', {'deploy_branch': 'main'}):
            payload = dispatch_payload(
                'alert', '09:14:22 IST', 'tg_table', '<html></html>', subject_detail,
                agent_name='loss-margin-low', agent_id=42,
            )
        assert "Agent: Agent:" not in payload["email_subject"]
        assert payload["email_subject"] == "RamboQuant Agent: loss-margin-low (#42)"
