"""
Sprint 15 — build-time enforcement for the per-scope exposed-column
registry (`backend.api.algo.grammar.EXPOSED_COLUMNS`).

This is the "generic `scope.attribute` dot-access" registry: a pure
additive alias layer (`exposed_name -> {type, from_column, label}`) that
lets `WHEN` read any real DataFrame/dict column off a scope's row without
a hand-registered metric token per column (e.g. `positions.total.pnl`,
equivalent to `pnl@positions.total`).

Covers:
  - every `exposed_name` is a valid bare identifier (no spaces, no dots) —
    the registry must never leak a raw messy column name (`'avail cash'`)
    as the grammar-facing key.
  - no `exposed_name` collides (case-insensitively) with ANY enum constant
    value anywhere in the real catalog (agent_grammar.yaml +
    order_fields.yaml), walked recursively at test time — never a
    hardcoded partial copy of the enum universe.
  - every entry has a non-empty `label`.
  - EXPOSED_COLUMNS' key set exactly matches the real `token_kind: scope`
    tokens declared in agent_grammar.yaml — a future scope literally
    cannot be added without this test failing until its columns are
    registered too.
  - no exposed_name on a scope shadows one of that scope's own child-scope
    segments (e.g. a column named `nfo` on `positions.expiring_today`
    would be unreachable under the frontend's longest-scope-match-first
    disambiguation rule).
  - the semantic-collision guard actually took effect: `account` is never
    an exposed name anywhere (it is itself an enum constant value — see
    `cancel_all_orders`/`chase_close_positions`'s `scope` param); the
    watchlist family exposes `watchlist_name`, never `account_id`.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from backend.api.algo import grammar as g

_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

_AGENT_GRAMMAR_YAML = Path(__file__).resolve().parents[1] / "config" / "grammars" / "agent_grammar.yaml"
_ORDER_FIELDS_YAML = Path(__file__).resolve().parents[1] / "config" / "grammars" / "order_fields.yaml"


def _collect_enum_values(obj, out: set) -> None:
    """Recursively walk a loaded YAML structure, collecting every value
    under any key literally named `enum` at ANY depth (Sprint 17's
    object-literal typing can nest these inside params_schema fields that
    are themselves nested objects) — never a hand-maintained partial list."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key == "enum" and isinstance(val, list):
                out.update(str(v) for v in val)
            _collect_enum_values(val, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_enum_values(item, out)


def _real_enum_universe() -> set[str]:
    out: set = set()
    for path in (_AGENT_GRAMMAR_YAML, _ORDER_FIELDS_YAML):
        with open(path, "r", encoding="utf-8") as f:
            _collect_enum_values(yaml.safe_load(f), out)
    return {v.lower() for v in out}


def _real_scope_tokens() -> set[str]:
    """Every real `token_kind: scope` token declared in
    agent_grammar.yaml — read live, not a copy-pasted list, so this test
    fails the moment a new scope is added without a matching
    EXPOSED_COLUMNS entry. Top-level YAML shape is `{system_tokens: [...],
    log_tags: [...]}` — scope tokens only ever live in `system_tokens`,
    but both lists are walked for robustness against a future reshuffle."""
    with open(_AGENT_GRAMMAR_YAML, "r", encoding="utf-8") as f:
        catalog = yaml.safe_load(f)
    entries: list = []
    for val in catalog.values():
        if isinstance(val, list):
            entries.extend(val)
    return {
        entry["token"] for entry in entries
        if isinstance(entry, dict)
        and entry.get("grammar_kind") == "condition"
        and entry.get("token_kind") == "scope"
    }


def _real_metric_resolvers() -> dict[str, str]:
    """{metric_token: dotted resolver path} for every real
    `token_kind: metric` row in agent_grammar.yaml — read live, not a
    hand-copied subset (the file has 40+ metric tokens; a prior partial
    `grep | head -60` during investigation truncated the list — this
    reads the whole thing)."""
    with open(_AGENT_GRAMMAR_YAML, "r", encoding="utf-8") as f:
        catalog = yaml.safe_load(f)
    entries: list = []
    for val in catalog.values():
        if isinstance(val, list):
            entries.extend(val)
    return {
        entry["token"]: entry["resolver"] for entry in entries
        if isinstance(entry, dict)
        and entry.get("grammar_kind") == "condition"
        and entry.get("token_kind") == "metric"
        and entry.get("resolver")
    }


def _flat_entries():
    """Yield (scope_token, exposed_name, entry_dict) for every registry row."""
    for scope_token, columns in g.EXPOSED_COLUMNS.items():
        for exposed_name, entry in columns.items():
            yield scope_token, exposed_name, entry


# ── (a) identifier shape ──────────────────────────────────────────────────

def test_every_exposed_name_is_a_valid_identifier():
    bad = [
        (scope, name) for scope, name, _ in _flat_entries()
        if not _IDENTIFIER_RE.match(name)
    ]
    assert not bad, f"non-identifier exposed_name(s): {bad}"


# ── (b) no enum-constant collision ────────────────────────────────────────

def test_no_exposed_name_collides_with_any_enum_constant():
    enum_universe = _real_enum_universe()
    assert enum_universe, "sanity: enum universe must not be empty"
    collisions = [
        (scope, name) for scope, name, _ in _flat_entries()
        if name.lower() in enum_universe
    ]
    assert not collisions, (
        f"exposed_name(s) collide with a real enum constant value: {collisions} "
        f"(enum universe sample: {sorted(enum_universe)[:10]})"
    )


def test_account_is_never_an_exposed_name():
    """Direct regression guard for the specific collision this registry
    was designed around — `account` is a real enum constant
    (cancel_all_orders / chase_close_positions `scope` param:
    `[total, account]`). The real `account` column is always exposed as
    `account_id` (or `watchlist_name` for watchlist rows) instead."""
    offenders = [(scope, name) for scope, name, _ in _flat_entries() if name == 'account']
    assert not offenders, f"'account' used as an exposed_name: {offenders}"


def test_watchlist_rows_never_expose_account_id():
    """watchlist_rows' real `account` column holds a WATCHLIST NAME, not a
    trading account — it must surface as `watchlist_name`, never as
    `account_id` (which every other scope family uses for the real
    trading-account column)."""
    for scope_token in ('watchlist.all', 'watchlist.default', 'watchlist.markets'):
        columns = g.EXPOSED_COLUMNS[scope_token]
        assert 'account_id' not in columns, scope_token
        assert columns.get('watchlist_name', {}).get('from_column') == 'account', scope_token


# ── (c) non-empty label ───────────────────────────────────────────────────

def test_every_entry_has_a_non_empty_label():
    bad = [
        (scope, name) for scope, name, entry in _flat_entries()
        if not (entry.get('label') or '').strip()
    ]
    assert not bad, f"entries missing a label: {bad}"


def test_every_entry_has_a_valid_type_and_from_column():
    valid_types = {'number', 'string', 'boolean'}
    bad = [
        (scope, name, entry) for scope, name, entry in _flat_entries()
        if entry.get('type') not in valid_types or not (entry.get('from_column') or '').strip()
    ]
    assert not bad, f"entries with a bad type/from_column: {bad}"


# ── (d) registry completeness vs. the real scope catalog ──────────────────

def test_registry_covers_every_real_scope_token():
    real_scopes = _real_scope_tokens()
    assert real_scopes, "sanity: must find at least one real scope token in the YAML"
    registered = set(g.EXPOSED_COLUMNS.keys())
    assert registered == real_scopes, (
        f"EXPOSED_COLUMNS keys != real scope tokens — "
        f"missing={real_scopes - registered} extra={registered - real_scopes}"
    )


def test_every_scope_has_provenance_metadata():
    """EXPOSED_COLUMN_SCOPE_PROVENANCE must cover exactly the same scopes
    as EXPOSED_COLUMNS — routes/grammar.py's catalog projection depends on
    both dicts sharing the same key set."""
    assert set(g.EXPOSED_COLUMN_SCOPE_PROVENANCE.keys()) == set(g.EXPOSED_COLUMNS.keys())
    for scope_token, (ctx_attr, fn_name) in g.EXPOSED_COLUMN_SCOPE_PROVENANCE.items():
        assert ctx_attr.startswith('ctx.'), scope_token
        assert hasattr(g, fn_name), f"{scope_token}: resolver fn {fn_name} not found in grammar module"


# ── (e) exposed_name never shadows a child-scope segment ──────────────────
#
# Built dynamically from the real scope-token set (not a hardcoded dict) —
# for every scope S, any OTHER real scope token that starts with "S." has
# a next dotted segment that is a real, longer scope chain; an exposed
# name on S equal to that segment would be unreachable under the
# frontend's longest-scope-match-first disambiguation rule (confirmed
# live example: 'positions.expiring_today' has children 'nfo' and
# 'mcx_unhedged' via 'positions.expiring_today.nfo'/'...mcx_unhedged').
# A future new child scope is automatically covered with no test edit.

def _child_segments_of(scope_token: str, all_scopes: set[str]) -> set[str]:
    prefix = scope_token + "."
    out = set()
    for other in all_scopes:
        if other.startswith(prefix):
            remainder = other[len(prefix):]
            out.add(remainder.split(".", 1)[0])
    return out


def test_no_exposed_name_shadows_a_child_scope_segment():
    all_scopes = _real_scope_tokens()
    offenders = []
    for scope_token, columns in g.EXPOSED_COLUMNS.items():
        child_segments = _child_segments_of(scope_token, all_scopes)
        shadowed = child_segments & set(columns.keys())
        if shadowed:
            offenders.append((scope_token, shadowed))
    assert not offenders, (
        f"exposed name(s) shadow a longer scope chain segment — "
        f"unreachable under longest-match-first disambiguation: {offenders}"
    )


# ── (f) exposed_name uniqueness within a single scope ─────────────────────

def test_no_duplicate_exposed_names_within_a_scope():
    for scope_token, columns in g.EXPOSED_COLUMNS.items():
        # dict keys are inherently unique — this guards against a future
        # refactor that builds a scope's schema from a list instead.
        assert len(columns) == len(set(columns.keys())), scope_token


def test_exposed_columns_for_scope_helper():
    assert g.exposed_columns_for_scope('positions.total') is g.EXPOSED_COLUMNS['positions.total']
    assert g.exposed_columns_for_scope('no-such-scope') == {}


# ── (g) reused names are PROVEN pure passthroughs, not assumed ───────────
#
# For every registry entry whose exposed_name happens to equal a real
# metric token's name (`pnl`, `day_val`, `inv_val`, `cur_val`, `cash`,
# `sod_cash`, `avail_margin`, `used_margin`, `collateral`, `ltp`), call the
# metric's ACTUAL resolver function against a synthetic row carrying a
# sentinel value under `from_column`, and assert the metric returns that
# EXACT sentinel. This is what makes `positions.total.pnl <= -50000`
# provably equivalent to `pnl@positions.total <= -50000` — not merely "we
# read the resolver source and it looked like a pure passthrough" at
# registry-build time. A future edit to any of these metrics that adds
# even one line of extra math (the way `day_pct`/`pnl_pct` already do,
# which is exactly why they are NOT reused here) will fail this test
# immediately instead of silently decoupling `scope.X` from `X@scope`.

_SENTINEL = 12345.5


def _resolver_fn(dotted_path: str):
    module_path, _, fn_name = dotted_path.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, fn_name)


def test_reused_exposed_names_are_pure_passthroughs_of_their_metric():
    metric_resolvers = _real_metric_resolvers()
    checked = []
    for scope_token, name, entry in _flat_entries():
        if name not in metric_resolvers:
            continue
        fn = _resolver_fn(metric_resolvers[name])
        row = {entry["from_column"]: _SENTINEL, "account": "X"}
        ctx = SimpleNamespace()
        got = fn(ctx, row)
        assert got == _SENTINEL, (
            f"{scope_token}.{name} reuses metric '{name}' as its exposed "
            f"name, but the metric resolver did not return the raw "
            f"'{entry['from_column']}' column value unchanged (got {got!r}, "
            f"expected {_SENTINEL!r}) — this name must NOT be reused, give "
            f"it its own distinct exposed_name instead"
        )
        checked.append((scope_token, name))
    # Sanity — at least the known-reused set actually got exercised,
    # so a refactor that silently stops reusing any name doesn't make
    # this test vacuously pass.
    assert len(checked) >= 9, checked
