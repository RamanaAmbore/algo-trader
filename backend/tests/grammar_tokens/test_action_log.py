"""Tests for grammar_tokens/action_log.py — `log` action resolver
(Python function `emit_log`, name unchanged across the `emit_log` ->
`log` DSL-dispatch-key rename).

`test_emit_log_with_level` / `test_emit_log_name_and_tags_threaded_into_
extra_and_result` / `test_emit_log_without_name_or_tags_is_byte_identical_
to_before` moved verbatim from `test_actions_coverage.py` (Wave 1,
DSL-token-unification plan, Part A — actions). The agent_kind-threading
and priority-param test classes in `test_actions_agent_kind_context.py`
and `test_help_lookup_action.py` stay in place — they import
`backend.api.algo.actions.emit_log`, which keeps resolving via that
module's backward-compatible re-export, unchanged by this migration.
"""

import pytest

from backend.api.algo.grammar_tokens.action_log import emit_log


@pytest.mark.asyncio
async def test_emit_log_with_level():
    """emit_log handler respects log level."""
    result = await emit_log({}, {"level": "warning", "message": "test warning"})
    assert result["level"] == "warning"
    assert result["message"] == "test warning"


@pytest.mark.asyncio
async def test_emit_log_name_and_tags_threaded_into_extra_and_result(caplog):
    """2026-10 routing-tree plan — `log(name=..., tags=...)` threads both
    into the log record's `extra` dict under `route_name`/`route_tags`
    (NOT the raw `name`/`tags` keys), same additive convention as the
    pre-existing `priority` param, and surfaces them on the returned
    dict for introspection/testing.

    Deliberately goes through the REAL `logging.Logger.warning` (no
    monkeypatch on the module logger) — a monkeypatched fake logger would
    have hidden the real bug this test guards against: `extra={"name":
    ...}` raises `KeyError: "Attempt to overwrite 'name' in LogRecord"`
    inside `Logger.makeRecord`, because `name` is a reserved
    `LogRecord` attribute. That crash is swallowed by the noop-action-
    handler's own exception guard in production, so a fake logger that
    never actually calls `makeRecord` would never catch it."""
    from backend.api.algo.grammar_tokens.action_log import logger

    # This logger sets `propagate = False` — caplog's handler lives on
    # the ROOT logger by default, so it never sees records from a
    # non-propagating logger. Attach caplog's own handler directly to
    # this logger instead of relying on propagation.
    old_level = logger.level
    logger.addHandler(caplog.handler)
    logger.setLevel("WARNING")
    try:
        result = await emit_log(
            {}, {"level": "warning", "message": "x", "priority": "high",
                 "name": "my-routing-name", "tags": ["gtt", "wing"]})
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(old_level)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.priority == "high"
    assert record.route_name == "my-routing-name"
    assert record.route_tags == ["gtt", "wing"]
    # The real structural `tags` field (condition-matching, read by
    # `log_store.py:tags_for()`) must NOT be clobbered by this param —
    # confirms the key really is `route_tags`, not `tags`.
    assert not hasattr(record, "tags") or record.tags != ["gtt", "wing"]
    assert result["name"] == "my-routing-name"
    assert result["tags"] == ["gtt", "wing"]


@pytest.mark.asyncio
async def test_emit_log_without_name_or_tags_is_byte_identical_to_before(monkeypatch):
    """Zero-regression check: a caller that sets neither `name` nor
    `tags` (every pre-existing agent) gets the exact single-positional-
    arg log call it always did — no `extra=` kwarg at all."""
    from backend.api.algo.grammar_tokens.action_log import logger

    calls = []

    def _fake_info(*a, **kw):
        calls.append((a, kw))

    monkeypatch.setattr(logger, "info", _fake_info)
    result = await emit_log({}, {"message": "plain"})
    assert calls == [(("Agent emit_log: plain",), {})]
    assert result["name"] is None
    assert result["tags"] is None


def test_logger_is_the_same_singleton_as_actions_module():
    """`get_logger("backend.api.algo.actions")` is a process-wide
    singleton keyed by name — confirms this resolver's log lines carry
    the SAME logger identity `actions.py`'s own module logger does, so
    `log_store.py:tags_for()`'s logger-name tag fallback is unchanged by
    this file's relocation out of `actions.py`."""
    from backend.api.algo import actions
    from backend.api.algo.grammar_tokens.action_log import logger

    assert logger is actions.logger
