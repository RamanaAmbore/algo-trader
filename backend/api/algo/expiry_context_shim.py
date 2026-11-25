"""Shared dispatch-context → `Context`-shaped shim for the expiry
pre-close window check.

Extracted (2026-11-24, audit finding #18) from two byte-identical
copies — `actions_live.py:_al_expiry_context_shim` and
`agent_engine.py:_v2_expiry_context_shim` — which were deliberately
duplicated rather than imported, to avoid a circular import between
those two modules. This tiny module has no dependency on either, so
both can import the real thing from here instead, closing the exact
duplication-drift risk that let the original `.now` omission (which
made `_action_live_expiry_auto_close` structurally dead) go unnoticed
in one copy while the other stayed reviewed.
"""


def expiry_context_shim(context: "dict | None"):
    """Minimal attribute-accessible shim over the dispatch `context`
    dict, so the grammar-token metric resolvers (which read `ctx.
    segment_flags`/`ctx.position_rows`/`ctx.now` via `getattr`,
    matching the `Context` class condition-evaluation shape) can be
    reused outside the condition-evaluation path.

    `now` is read from the dispatch context's own `"now"` key (the
    exact value `agent_engine.py:_build_context()` sets), falling back
    to `timestamp_indian()` only when that key is absent (e.g. a
    hand-built test context) — never silently defaulting to a wrong
    clock. See the 2026-11-23 Part C incident this guards against:
    omitting `.now` entirely made `_scope_positions_expiring_today()`
    fail to parse every row (bare `except Exception: continue`), which
    made `_expiry_preclose_exchanges()` always return an empty set —
    both `_action_live_expiry_auto_close` and the alert sibling
    `_v2_expiry_position_breakdown` were structurally dead in
    production until that was fixed.
    """
    from types import SimpleNamespace
    from backend.shared.helpers.date_time_utils import timestamp_indian
    context = context or {}
    return SimpleNamespace(
        segment_flags=context.get("segment_flags") or {},
        position_rows=context.get("position_rows") or [],
        now=context.get("now") or timestamp_indian(),
    )
