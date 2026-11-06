"""Unit tests for `resolve_source()` — the SSOT that separates an alert's
SOURCE identity (a background process, a bare error, a genuine agent
action, or a manual order) from the ALERT-AGENT dispatch identity
(unchanged, handled elsewhere). See `alert_utils.py`'s own docstring and
CLAUDE.md's "Source / alert-agent / triggered-by model" entry.

Covers the 4 template cases from the approved plan:
  1. Pure background process, no known trigger.
  2. Bare, uncategorized error (no known trigger).
  3. Process triggered by a manual order.
  4. Process triggered by a real agent.
  5. Real agent, direct action, no process layer (unchanged, branch 2).
  6. Genuine manual action, no process layer (unchanged, branch 2).
"""
from backend.shared.helpers.alert_utils import resolve_source, _source_extra


def test_pure_background_process_no_known_trigger():
    """Ticker watchdog, deploy-sync, market status/summary, visitor
    report, GTT reconciler, OCO watcher, user registration — nothing
    triggered these; `agent_id`/`agent_name` keys are absent entirely."""
    extra = {"source_type": "process", "source_name": "Ticker watchdog"}
    label_word, label_sentence, process_tag, action_tag = resolve_source(extra)
    assert label_word == "Source"
    assert label_sentence == "Ticker watchdog"
    assert process_tag == "Ticker watchdog"
    assert action_tag is None


def test_bare_uncategorized_error_no_known_trigger():
    """The ERROR_AGENT catch-all — source_type="error" gets a distinct
    " (error)" suffix in the header text only, never in the tag."""
    extra = {"source_type": "error", "source_name": "chase"}
    label_word, label_sentence, process_tag, action_tag = resolve_source(extra)
    assert label_word == "Source"
    assert label_sentence == "chase (error)"
    assert process_tag == "chase"
    assert action_tag is None


def test_bare_error_with_a_real_agent_id_gets_a_trigger_phrase_for_free():
    """A future caller that DOES attach a real `agent_id` to an
    error-tagged record gets the "triggered by {agent}" phrase, same as
    any other process — the " (error)" suffix still applies."""
    extra = {"source_type": "error", "source_name": "chase",
              "agent_id": 12, "agent_name": "Chase close positions"}
    label_word, label_sentence, process_tag, action_tag = resolve_source(extra)
    assert label_word == "Source"
    assert label_sentence == "chase triggered by Chase close positions (error)"
    assert action_tag == "Chase close positions"


def test_process_triggered_by_a_manual_order():
    """Chase cancel-confirm failure, order-poll correction, etc. — the
    order's own `agent_id` is explicitly `None` (a real KEY, not an
    absent one) -> resolves to "manual", not "no trigger at all"."""
    extra = {"source_type": "process", "source_name": "chase",
              "agent_id": None, "agent_name": None}
    label_word, label_sentence, process_tag, action_tag = resolve_source(extra)
    assert label_word == "Source"
    assert label_sentence == "chase triggered manually"
    assert process_tag == "chase"
    assert action_tag == "manual"


def test_process_triggered_by_a_real_agent():
    extra = {"source_type": "process", "source_name": "chase",
              "agent_id": 12, "agent_name": "Close-on-loss"}
    label_word, label_sentence, process_tag, action_tag = resolve_source(extra)
    assert label_word == "Source"
    assert label_sentence == "chase triggered by Close-on-loss"
    assert process_tag == "chase"
    assert action_tag == "Close-on-loss"


def test_real_agent_direct_action_no_process_layer_unchanged():
    """Rich/breach alerts — branch 2, today's exact behavior."""
    extra = {"agent_id": 12, "agent_name": "Chase close positions"}
    label_word, label_sentence, process_tag, action_tag = resolve_source(extra)
    assert label_word == "Agent"
    assert label_sentence == "Chase close positions"
    assert process_tag is None
    assert action_tag == "Chase close positions"


def test_genuine_manual_action_no_process_layer_unchanged():
    """Fill alerts — branch 2, no agent_id/agent_name at all -> "manual"."""
    extra = {}
    label_word, label_sentence, process_tag, action_tag = resolve_source(extra)
    assert label_word == "Agent"
    assert label_sentence == "manual"
    assert process_tag is None
    assert action_tag == "manual"


def test_process_type_with_falsy_source_name_falls_through_to_branch_2():
    """`source_type` alone, with no real `source_name`, never produces a
    half-built "Source:" label — degrades to the ordinary agent branch."""
    extra = {"source_type": "process", "source_name": "", "agent_id": None}
    label_word, label_sentence, process_tag, action_tag = resolve_source(extra)
    assert label_word == "Agent"
    assert label_sentence == "manual"
    assert process_tag is None


def test_unrecognised_source_type_falls_through_to_branch_2():
    extra = {"source_type": "bogus", "source_name": "x", "agent_id": None}
    label_word, label_sentence, process_tag, action_tag = resolve_source(extra)
    assert label_word == "Agent"
    assert process_tag is None


class TestSourceExtra:
    """`_source_extra()` — the shared builder `format_notification_header()`
    and `dispatch_payload()` both use to feed `resolve_source()`."""

    def test_trigger_known_true_always_adds_agent_keys_even_when_none(self):
        extra = _source_extra(None, None, "process", "chase", trigger_known=True)
        assert "agent_id" in extra and extra["agent_id"] is None
        assert "agent_name" in extra and extra["agent_name"] is None

    def test_trigger_known_false_omits_agent_keys_entirely(self):
        extra = _source_extra(None, None, "process", "Ticker watchdog", trigger_known=False)
        assert "agent_id" not in extra
        assert "agent_name" not in extra

    def test_no_source_type_or_name_omits_source_keys(self):
        extra = _source_extra("Chase close positions", 12)
        assert "source_type" not in extra
        assert "source_name" not in extra
        assert extra["agent_id"] == 12
        assert extra["agent_name"] == "Chase close positions"
