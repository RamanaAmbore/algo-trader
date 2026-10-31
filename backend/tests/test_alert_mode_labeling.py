"""
Regression tests for the 2026-10-31 alert mode-labeling fix (operator:
"the alerts should show paper and simulation mode explicitly in alert
notification. wire it ... this functionality should be in the base
code for all alerts").

Root cause: mode labeling was scattered and incomplete.
  - `_v2_send_rich_alert` blanked its own mode_tag entirely during sim
    mode (`'' if sim_mode else _agent_execution_mode_tag(agent)`) and
    never considered replay_mode at all, so a sim/replay alert that
    reached a channel read identically to a real live breach.
  - `send_summary()`/`_render_summary` hardcoded `sim_mode=False,
    mode_tag=""` unconditionally, so a summary fired via the
    `send_summary` DSL action inside a sim/replay cycle carried no tag.
  - `dispatch_payload()` (the one shared base dispatcher every alert
    type funnels through) required each caller to pre-compute the
    full bracket tag itself, instead of deriving it from the
    fundamental sim_mode/replay_mode flags every caller already has.

Fix: `dispatch_payload()` now derives `[SIM]`/`[REPLAY]` (word prefix,
bracket tag, and warning banner) itself from `sim_mode`/`replay_mode`
whenever the caller doesn't pass a more specific `mode_tag` (e.g. the
per-agent PAPER check, which needs the agent object dispatch_payload
doesn't have). Live mode (both flags False, no explicit mode_tag)
resolves to '' everywhere -- no pill, no prefix, no banner, matching
the operator's explicit "for live, pill is not required, as it is
assumed to be default."
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.shared.helpers.alert_utils import dispatch_payload
from backend.api.algo.agent_engine import _agent_execution_mode_tag


def _base_kwargs(**overrides):
    kwargs = dict(
        msg_type="alert", ist_display="2026-10-31 12:00 IST / 02:30 EDT",
        tg_table="body", email_table_html="<p>body</p>",
        subject_detail="Test Agent",
    )
    kwargs.update(overrides)
    return kwargs


class TestDispatchPayloadLiveModeIsSilent:
    def test_live_mode_has_no_prefix_no_tag_no_banner(self):
        p = dispatch_payload(**_base_kwargs())
        assert "SIMULATOR" not in p["telegram_msg"]
        assert "REPLAY" not in p["telegram_msg"]
        assert "[SIM]" not in p["telegram_msg"]
        assert "[REPLAY]" not in p["telegram_msg"]
        assert "RUN —" not in p["telegram_msg"]


class TestDispatchPayloadSimMode:
    def test_sim_mode_tags_telegram_and_title(self):
        p = dispatch_payload(**_base_kwargs(sim_mode=True))
        assert "[SIM]" in p["telegram_msg"]
        assert "SIMULATOR RUN" in p["telegram_msg"]
        assert "SIMULATOR" in p["title"]

    def test_sim_mode_tags_email_subject_and_banner(self):
        p = dispatch_payload(**_base_kwargs(sim_mode=True))
        assert "SIMULATOR" in p["email_subject"]
        assert "SIMULATOR RUN" in p["email_html"]


class TestDispatchPayloadReplayMode:
    def test_replay_mode_tags_telegram_and_title(self):
        p = dispatch_payload(**_base_kwargs(replay_mode=True))
        assert "[REPLAY]" in p["telegram_msg"]
        assert "REPLAY RUN" in p["telegram_msg"]
        assert "REPLAY" in p["title"]
        # Must never also claim SIMULATOR -- the two modes are mutually
        # exclusive within one cycle run.
        assert "SIMULATOR" not in p["telegram_msg"]
        assert "SIMULATOR" not in p["title"]

    def test_replay_mode_tags_email_subject_and_banner(self):
        p = dispatch_payload(**_base_kwargs(replay_mode=True))
        assert "REPLAY" in p["email_subject"]
        assert "REPLAY RUN" in p["email_html"]

    def test_replay_wins_over_sim_if_both_somehow_set(self):
        """Not a real reachable state (one cycle is either sim or
        replay, never both), but replay should win deterministically
        rather than producing a mixed/undefined tag."""
        p = dispatch_payload(**_base_kwargs(sim_mode=True, replay_mode=True))
        assert "[REPLAY]" in p["telegram_msg"]
        assert "[SIM]" not in p["telegram_msg"]


class TestDispatchPayloadExplicitModeTagWins:
    def test_caller_supplied_mode_tag_is_never_overridden(self):
        """The per-agent PAPER tag (computed by _agent_execution_mode_tag,
        which dispatch_payload has no way to derive itself -- it needs
        the agent object) must survive even though sim_mode/replay_mode
        are both False."""
        p = dispatch_payload(**_base_kwargs(mode_tag="[PAPER]"))
        assert "[PAPER]" in p["telegram_msg"]
        # No SIMULATOR/REPLAY word-prefix machinery kicks in just
        # because an explicit tag was given -- that's sim/replay-only.
        assert "SIMULATOR" not in p["telegram_msg"]
        assert "REPLAY RUN" not in p["telegram_msg"]


class TestAgentExecutionModeTagPrecedence:
    """_agent_execution_mode_tag: replay > sim > paper > live."""

    def _agent(self, actions=None):
        a = MagicMock()
        a.actions = actions or [{"type": "place_order", "params": {}}]
        return a

    def test_replay_mode_wins_even_with_no_broker_actions(self):
        agent = self._agent(actions=[])
        assert _agent_execution_mode_tag(agent, replay_mode=True) == "[REPLAY]"

    def test_sim_mode_wins_even_with_no_broker_actions(self):
        agent = self._agent(actions=[])
        assert _agent_execution_mode_tag(agent, sim_mode=True) == "[SIM]"

    def test_replay_wins_over_sim_when_both_set(self):
        agent = self._agent()
        assert _agent_execution_mode_tag(agent, sim_mode=True, replay_mode=True) == "[REPLAY]"

    def test_live_mode_on_dev_branch_is_silent(self, monkeypatch):
        monkeypatch.setattr(
            "backend.shared.helpers.utils.is_prod_branch", lambda: False,
        )
        agent = self._agent()
        assert _agent_execution_mode_tag(agent) == ""

    def test_live_mode_prod_paper_toggle_on_tags_paper(self, monkeypatch):
        monkeypatch.setattr(
            "backend.shared.helpers.utils.is_prod_branch", lambda: True,
        )
        monkeypatch.setattr(
            "backend.shared.helpers.settings.get_bool",
            lambda key, default=False: True if key == "execution.paper_trading_mode" else default,
        )
        agent = self._agent()
        assert _agent_execution_mode_tag(agent) == "[PAPER]"

    def test_live_mode_prod_paper_toggle_off_is_silent(self, monkeypatch):
        monkeypatch.setattr(
            "backend.shared.helpers.utils.is_prod_branch", lambda: True,
        )
        monkeypatch.setattr(
            "backend.shared.helpers.settings.get_bool",
            lambda key, default=False: False,
        )
        agent = self._agent()
        assert _agent_execution_mode_tag(agent) == ""

    def test_alert_only_agent_never_gets_paper_tag(self, monkeypatch):
        """An agent with no broker action would never place a fake
        order even in paper mode, so it must not claim [PAPER]."""
        monkeypatch.setattr(
            "backend.shared.helpers.utils.is_prod_branch", lambda: True,
        )
        monkeypatch.setattr(
            "backend.shared.helpers.settings.get_bool",
            lambda key, default=False: True if key == "execution.paper_trading_mode" else default,
        )
        agent = self._agent(actions=[{"type": "log", "params": {}}])
        assert _agent_execution_mode_tag(agent) == ""
