"""Regression coverage for Wave 1, DSL-token-unification plan, Part B
(channels/formats) — the ACTUAL behavior change this part of the plan
makes, distinct from the byte-for-byte catalog-equivalence check in
`test_agent_grammar_yaml_catalog.py`.

Before this change, every `('notify', 'channel')` / `('notify',
'format')` catalog row carried `resolver: null`, so `REGISTRY.channel()`
/ `.fmt()` were structurally dead for all of them — the only production
caller, `actions.py`'s `help_lookup` (via `_help_resolve_callable`),
always rendered "(signature unavailable)" / "(no docstring)" for a
channel or format token, even though a real function existed
(`event_agents.py`'s `CHANNELS` table / `alert_utils._html_table`) and
was dispatching real alerts all along.

This file proves: (1) `REGISTRY.channel(token)` / `.fmt(token)` now
resolve to the REAL function for every token that has one; (2)
`help(token)` renders a real signature/docstring instead of the
"unavailable" placeholders; (3) a token with genuinely no implementation
(`text_narrow`/`plain_text`/`json`) still renders "(signature
unavailable)" — a token with a real but unwired function is NOT the
same catalog state as one truly dead, and this suite must tell them
apart; (4) `_load_required_resolver`'s params_schema-is-NOT-a-factory-
signature fix (`grammar_registry.py`) — a channel/format resolver never
routes to the `factories` table regardless of whether the row also
carries a `params_schema` (several channel rows do, for schema-only
validation of their `events: [...]` entry's extra keys like `gate`/
`priority` — an unrelated meaning from the metric/scope factory
convention `_load_resolved` uses for e.g. `mean_pnl(30)`).

Reloads REGISTRY from the REAL `agent_grammar.yaml`-derived
`grammar.SYSTEM_TOKENS` rows (not synthetic fixture data), through the
same mocked-DB-session `REGISTRY.reload()` harness
`test_help_lookup_action.py` already established — no live database
connection required, and this is the actual production catalog content,
not a stand-in for it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo import grammar
from backend.api.algo.grammar_registry import REGISTRY


def _row_from_system_token(i: int, t: dict) -> SimpleNamespace:
    """Build a fake `GrammarToken` ORM row from a real `SYSTEM_TOKENS`
    entry — same shape `test_help_lookup_action.py`'s
    `_make_fake_grammar_token` uses, populated from the REAL catalog
    dict instead of synthetic data."""
    return SimpleNamespace(
        id=i,
        grammar_kind=t["grammar_kind"],
        token_kind=t["token_kind"],
        token=t["token"],
        resolver=t.get("resolver"),
        is_active=True,
        description=t.get("description", ""),
        value_type=t.get("value_type"),
        units=t.get("units"),
        enum_values=t.get("enum_values"),
        params_schema=t.get("params_schema"),
        template_body=t.get("template_body", ""),
        source={},
    )


def _make_grammar_db_session_mock(rows):
    async def _execute(stmt, *args, **kwargs):
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_execute)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _reload_with_the_real_catalog():
    rows = [_row_from_system_token(i, t) for i, t in enumerate(grammar.SYSTEM_TOKENS)]
    mock_session = _make_grammar_db_session_mock(rows)
    with patch("backend.api.database.async_session", return_value=mock_session):
        asyncio.run(REGISTRY.reload())


@pytest.fixture(autouse=True)
def _ensure_real_catalog_loaded():
    _reload_with_the_real_catalog()
    yield


class TestChannelResolution:
    @pytest.mark.parametrize("token,expected_fn_name", [
        ("ntfy", "_send_ntfy"),
        ("telegram", "_send_telegram_html"),
        ("telegram_info", "_send_telegram_info_html"),
        ("telegram_deploy", "_send_telegram_deploy_html"),
        ("email", "_send_email_channel"),
        ("log", "_send_log_channel"),
        ("websocket", "_broadcast_websocket"),
        ("inapp", "_broadcast_inapp"),
    ])
    def test_channel_resolves_to_the_real_function(self, token, expected_fn_name):
        fn = REGISTRY.channel(token)
        assert fn is not None, f"REGISTRY.channel({token!r}) must resolve, not be dead"
        assert fn.__name__ == expected_fn_name
        assert fn.__module__.startswith("backend.api.algo.grammar_tokens.")

    def test_ntfy_resolver_is_not_routed_to_the_factories_table_despite_its_params_schema(self):
        """`ntfy`'s catalog row carries a real `params_schema` (priority,
        gate) — confirms `_load_required_resolver`'s fix: a channel
        resolver always lands in the plain `channels` table, never
        `channel_factories`, regardless of `params_schema`."""
        assert "ntfy" in REGISTRY.channels
        assert "ntfy" not in REGISTRY.channel_factories


class TestFormatResolution:
    def test_html_table_resolves_to_the_real_function(self):
        fn = REGISTRY.fmt("html_table")
        assert fn is not None
        assert fn.__name__ == "_html_table"
        assert fn.__module__ == "backend.api.algo.grammar_tokens.format_html_table"

    @pytest.mark.parametrize("token", ["text_narrow", "plain_text", "json"])
    def test_unimplemented_formats_stay_unresolved(self, token):
        """No real function exists anywhere in the codebase for these
        three — confirmed by repo-wide grep during this investigation.
        They must resolve to `None`, not be silently wired to something
        that doesn't actually implement them."""
        assert REGISTRY.fmt(token) is None


class TestHelpReflectsRealChannelsAndFormats:
    """`help(<token>)`'s reflection output — `actions.help_lookup` via
    `_help_resolve_callable`/`_help_reflect` — must distinguish a real,
    resolved channel/format from a genuinely dead one."""

    @pytest.mark.asyncio
    async def test_help_ntfy_renders_a_real_signature_and_docstring(self):
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "ntfy"})
        matches = [m for m in result["matches"] if m["token_kind"] == "channel"]
        assert len(matches) == 1
        m = matches[0]
        assert m["signature"] != "(signature unavailable)"
        assert "title" in m["signature"] and "body" in m["signature"]
        assert m["docstring"] != "(no docstring)"

    @pytest.mark.asyncio
    async def test_help_html_table_renders_a_real_signature_and_docstring(self):
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "html_table"})
        matches = [m for m in result["matches"] if m["token_kind"] == "format"]
        assert len(matches) == 1
        m = matches[0]
        assert m["signature"] == "(headers, rows)"
        assert "html table" in m["docstring"].lower()

    @pytest.mark.asyncio
    async def test_help_text_narrow_still_renders_unavailable(self):
        """The negative case — proves this suite can tell a resolved
        token apart from a genuinely dead one, not just that the catalog
        row exists."""
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "text_narrow"})
        matches = [m for m in result["matches"] if m["token_kind"] == "format"]
        assert len(matches) == 1
        m = matches[0]
        assert m["signature"] == "(signature unavailable)"
        assert m["docstring"] == "(no docstring)"

    @pytest.mark.asyncio
    async def test_help_websocket_renders_a_real_signature(self):
        """`websocket`/`inapp` are the kind='cycle' direct-dispatch
        channels (`events.py`), a separate mechanism from
        `event_agents.py`'s `CHANNELS` table — confirms the reflection
        path works for both mechanisms equally."""
        from backend.api.algo.actions import help_lookup

        result = await help_lookup({}, {"name": "websocket"})
        matches = [m for m in result["matches"] if m["token_kind"] == "channel"]
        assert len(matches) == 1
        assert matches[0]["signature"] != "(signature unavailable)"
        assert "broadcast_fn" in matches[0]["signature"]
