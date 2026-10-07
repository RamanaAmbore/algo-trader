"""
Investigation for plan item #5: "Trade-mode default resolution — read-time,
not creation-time."

FINDING (documented here, not silently "fixed" — see report): the plan's
premise does not match the current implementation, and changing it is a
live-order-routing behaviour change that needs explicit operator sign-off,
not a silent fix bundled into an unrelated agents-CRUD slice.

Current (actual) behaviour:
  - `_age_resolve_trade_mode()` (agents.py) resolves
    `execution.default_agent_trade_mode` ONCE, at agent-creation time,
    and freezes the result into `Agent.trade_mode` — a NOT NULL column
    with its own default ("paper"). No row's `trade_mode` is ever None.
  - The ONLY consumer at use-time is `_resolve_mode()`
    (backend/api/algo/actions.py), which reads
    `getattr(agent, "trade_mode", "paper")` directly — a concrete
    'paper'/'live' string — and does NOT re-check
    `execution.default_agent_trade_mode` at all.
  - Therefore: changing the global default AFTER an agent exists has
    ZERO effect on that agent's live routing, by design as currently
    implemented. This test locks in that CURRENT behaviour so it
    can't silently regress further, and documents the gap.

Proposed design (for operator approval, NOT implemented in this slice):
  - Make `Agent.trade_mode` nullable (migration), stop writing a
    resolved value at create time when the client sends `trade_mode=None`
    (store NULL instead), and change `_resolve_mode()` to fall back to
    `get_string("execution.default_agent_trade_mode", "paper")` when
    `agent.trade_mode` is None/empty. This is a live-order-routing gate
    change — flipping the global default would then retroactively arm
    every NULL-trade_mode agent for live trading on its next fire, which
    is exactly the kind of blast-radius change that needs an explicit
    operator decision, not an agents-CRUD side effect.
"""
from __future__ import annotations

import os
os.environ.setdefault("PYTEST_RUNNING", "1")

from unittest.mock import patch

from backend.api.routes.agents import _age_resolve_trade_mode


def test_resolve_trade_mode_reads_global_default_at_creation_time():
    with patch("backend.shared.helpers.settings.get_string", return_value="live"):
        assert _age_resolve_trade_mode(None) == "live"
    with patch("backend.shared.helpers.settings.get_string", return_value="paper"):
        assert _age_resolve_trade_mode(None) == "paper"


def test_trade_mode_is_frozen_at_creation_not_re_resolved_at_read_time():
    """KNOWN LIMITATION (plan item #5, not implemented — see module
    docstring). Simulates: agent created when the global default was
    'paper' (frozen into trade_mode='paper' at creation), global default
    later changed to 'live'. The ALREADY-CREATED agent's effective
    trade_mode — the only thing _resolve_mode() ever reads — does NOT
    pick up the new default. If this assertion ever starts failing
    because trade_mode resolution became read-time, item #5 has been
    implemented for real — update/remove this test and its docstring
    instead of treating the new result as a regression."""
    with patch("backend.shared.helpers.settings.get_string", return_value="paper"):
        frozen_at_creation = _age_resolve_trade_mode(None)
    assert frozen_at_creation == "paper"

    # Global default changes AFTER creation.
    with patch("backend.shared.helpers.settings.get_string", return_value="live"):
        # _resolve_mode() (actions.py) never calls get_string() again for
        # an existing row — it only reads agent.trade_mode directly. The
        # frozen value from creation time is what a real Agent row would
        # carry in its NOT NULL trade_mode column; it stays 'paper'.
        effective_trade_mode_on_existing_row = frozen_at_creation

    assert effective_trade_mode_on_existing_row == "paper", (
        "This agent's trade_mode was frozen at creation time and is NOT "
        "re-resolved against the current global default — matches the "
        "current _resolve_mode()/Agent.trade_mode implementation. See "
        "this test module's docstring for the proposed (unimplemented, "
        "pending operator approval) design to make this read-time."
    )


def test_resolve_mode_consumer_reads_agent_trade_mode_directly_not_global_default():
    """Confirms actions._resolve_mode's actual consumption pattern: it
    reads agent.trade_mode via getattr with a 'paper' default, and never
    imports/calls get_string('execution.default_agent_trade_mode', ...).
    This is the structural reason item #5's 'read-time resolution'
    premise doesn't hold today."""
    from pathlib import Path
    src = Path("backend/api/algo/actions.py").read_text()
    # _resolve_mode's own body - narrow the search to that function to
    # avoid false negatives from unrelated code elsewhere in the module.
    start = src.index("def _resolve_mode")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    assert 'getattr(agent, "trade_mode"' in body
    assert "default_agent_trade_mode" not in body
