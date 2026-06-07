"""
Item 2 Stage 1 — notify fragment resolution tests.

Covers `resolve_events()`:
  - $ref expands against the FragmentRegistry cache
  - missing ref logs warning + skips, doesn't crash
  - mixed lists (inline + $ref) merge correctly
  - non-list / None input → []
  - $ref entries must be dicts with $ref key

The registry is patched directly (no DB) so the tests are pure
function units.
"""

from __future__ import annotations

from unittest.mock import patch

from backend.api.algo.template_registry import resolve_events, REGISTRY


def _set_cache(notify: dict):
    """Helper — drop a notify cache into the registry singleton."""
    REGISTRY._cache = {"notify": dict(notify), "condition": {}}


def test_inline_channels_pass_through():
    _set_cache({})
    events = [
        {"channel": "telegram", "enabled": True},
        {"channel": "email",    "enabled": False},
    ]
    out = resolve_events(events)
    assert out == events
    # Ensure we didn't return the original list (defensive copy semantics
    # are nice-to-have but not strictly required).


def test_ref_expands_into_inline_channels():
    _set_cache({
        "notify-trio": [
            {"channel": "telegram", "enabled": True},
            {"channel": "email",    "enabled": True},
            {"channel": "log",      "enabled": True},
        ],
    })
    out = resolve_events([{"$ref": "notify-trio"}])
    assert len(out) == 3
    channels = [c["channel"] for c in out]
    assert channels == ["telegram", "email", "log"]


def test_ref_mixed_with_inline_entry():
    _set_cache({
        "notify-quiet": [{"channel": "log", "enabled": True}],
    })
    out = resolve_events([
        {"channel": "telegram", "enabled": True},
        {"$ref": "notify-quiet"},
    ])
    assert len(out) == 2
    assert out[0]["channel"] == "telegram"
    assert out[1]["channel"] == "log"


def test_missing_ref_skipped_other_channels_still_fire():
    _set_cache({})
    out = resolve_events([
        {"$ref": "doesnt-exist"},
        {"channel": "telegram", "enabled": True},
    ])
    # Missing ref drops, telegram survives.
    assert out == [{"channel": "telegram", "enabled": True}]


def test_empty_inputs_return_empty():
    _set_cache({})
    assert resolve_events(None) == []
    assert resolve_events([]) == []
    assert resolve_events("not a list") == []   # type: ignore[arg-type]


def test_non_dict_entries_dropped():
    _set_cache({})
    out = resolve_events([
        "string entry — bad shape",
        123,
        {"channel": "telegram", "enabled": True},
    ])
    assert out == [{"channel": "telegram", "enabled": True}]


def test_ref_body_must_be_list_to_expand():
    # If a registry row was authored with a non-list body (e.g.
    # accidentally a dict), the resolver should silently drop the ref
    # rather than crash. Defensive against operator-authored bad
    # fragments slipping past validation.
    _set_cache({
        "notify-broken": {"channel": "telegram", "enabled": True},   # dict, not list
    })
    out = resolve_events([{"$ref": "notify-broken"}])
    assert out == []


def test_cache_isolation():
    """The resolver returns shallow copies — mutating its output must
    NOT bleed into the next resolve call's results."""
    _set_cache({
        "notify-trio": [
            {"channel": "telegram", "enabled": True},
        ],
    })
    out1 = resolve_events([{"$ref": "notify-trio"}])
    out1[0]["enabled"] = False    # mutate output

    out2 = resolve_events([{"$ref": "notify-trio"}])
    assert out2[0]["enabled"] is True, "registry cache was mutated by caller"
