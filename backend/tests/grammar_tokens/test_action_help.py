"""Tests for grammar_tokens/action_help.py — `help` action resolver
(Python function `help_lookup`; `_help_resolve_callable` /
`_help_reflect` are its private, exclusive helpers, moved alongside it).

`backend/tests/test_help_lookup_action.py` already carries extensive,
deeply cross-cutting coverage of `help_lookup` (per-token_kind
resolution, the structural DEBUG exemption in `event_agents.py`, and the
`log`/`priority` routing-tree behavior) — that file is NOT moved or
duplicated here: it imports `from backend.api.algo.actions import
help_lookup`, which keeps resolving via `actions.py`'s backward-
compatible re-export, unchanged by this migration (Wave 1, DSL-token-
unification plan, Part A — actions). This file provides fresh,
location-anchored baseline coverage of the resolver at its new import
path, per the per-token-file-plus-test convention every other migrated
token already follows.
"""

from types import SimpleNamespace

import pytest

from backend.api.algo.grammar_tokens.action_help import (
    help_lookup,
    _help_resolve_callable,
    _help_reflect,
)


def test_help_reflect_none_renders_explicit_unavailable_text():
    sig, doc = _help_reflect(None)
    assert sig == "(signature unavailable)"
    assert doc == "(no docstring)"


def test_help_reflect_non_callable_renders_the_real_value():
    sig, doc = _help_reflect("a template body")
    assert "a template body" in sig
    assert doc == "(no docstring — not a callable)"


def test_help_reflect_real_function_renders_signature_and_docstring():
    def _fn(a, b=1):
        """Docstring."""
        return a + b

    sig, doc = _help_reflect(_fn)
    assert sig == "(a, b=1)"
    assert doc == "Docstring."


def test_help_resolve_callable_unknown_token_kind_returns_none():
    assert _help_resolve_callable("totally_unknown_kind", "x") is None


@pytest.mark.asyncio
async def test_help_lookup_no_name_degrades_gracefully():
    result = await help_lookup({}, {})
    assert result == {"action": "help", "status": "logged", "name": "", "matches": []}


@pytest.mark.asyncio
async def test_help_lookup_unknown_name_returns_no_matches():
    result = await help_lookup({}, {"name": "totally_unknown_xyz_token"})
    assert result["matches"] == []


@pytest.mark.asyncio
async def test_help_lookup_dispatched_via_noop_handler_is_wired():
    """Confirms `help_lookup` is reachable through the real
    `_al_run_noop_handler` dispatch path (`actions.py`'s
    `_NOOP_ACTION_HANDLERS["help"]`) after relocation — the dispatch
    table's `_HandlerRef(__name__, "help_lookup", True)` entry still
    resolves `help_lookup` off `actions.py`'s re-export."""
    from backend.api.algo.actions import _al_run_noop_handler

    agent = SimpleNamespace(slug="test-agent")
    ok = await _al_run_noop_handler(agent, "help", {"name": "totally_unknown_xyz_token"}, {})
    assert ok is True


def test_logger_is_the_same_singleton_as_actions_module():
    """Same singleton-by-name invariant as the other relocated action
    resolvers — `help_lookup`'s DEBUG log line keeps the exact logger
    identity `actions.py`'s own module logger has."""
    from backend.api.algo import actions
    from backend.api.algo.grammar_tokens.action_help import logger

    assert logger is actions.logger
