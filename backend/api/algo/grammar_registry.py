"""
Grammar Registry — in-memory dispatch table loaded from the grammar_tokens DB.

The registry is the bridge between token names the operator types into an
agent's condition / notify / action spec and the Python callables that
actually run them. At app startup:

  1. seed_grammar_tokens() (in grammar.py) ensures the DB has the full
     system catalog.
  2. REGISTRY.reload() pulls every active row and imports each resolver by
     dotted path, caching the callable in a typed dispatch dict.

Runtime code (condition evaluator, notify dispatcher, action runner) asks
the registry for a token by (grammar_kind, token_kind, token) and gets back
either a callable or a template body. If the admin UI adds or flips tokens
while the service is running, calling REGISTRY.reload() picks up the change
with no restart.

The registry holds no business logic — it is a pure name → callable map.
"""

from __future__ import annotations

import ast
import importlib
import threading
from typing import Any, Callable, Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


def _import_dotted(path: str) -> Any:
    """Import 'pkg.mod.name' and return the attribute 'name'."""
    module_path, _, attr = path.rpartition(".")
    if not module_path:
        raise ValueError(f"invalid resolver path: {path}")
    module = importlib.import_module(module_path)
    return getattr(module, attr)



def _load_resolved(table: str, allow_bare: bool):
    def load(r, tables) -> bool:
        if r.resolver:
            fn = _import_dotted(r.resolver)
            # A row carrying params_schema is a FACTORY — it takes the
            # call's positional args (not (ctx, row)) and RETURNS the
            # (ctx, row) -> value callable. Keep it out of the plain
            # exact-match table so an un-called base token (e.g. bare
            # "mean_pnl") never resolves as if it were itself a metric.
            if r.params_schema:
                tables['factories'][table][r.token] = (fn, r.params_schema)
            else:
                tables[table][r.token] = fn
        elif allow_bare:
            tables[table][r.token] = None
        return True
    return load


def _load_required_resolver(table: str):
    def load(r, tables) -> bool:
        if not r.resolver:
            return False
        fn = _import_dotted(r.resolver)
        if r.params_schema:
            tables['factories'][table][r.token] = (fn, r.params_schema)
        else:
            tables[table][r.token] = fn
        return True
    return load


def _load_template(r, tables) -> bool:
    tables['templates'][r.token] = r.template_body or ''
    return True


def _load_log_tag(r, tables) -> bool:
    tables['log_tags'][r.token] = r.source or {}
    return True


def _load_action(r, tables) -> bool:
    tables['actions'][r.token] = {
        'fn': _import_dotted(r.resolver) if r.resolver else None,
        'params_schema': r.params_schema or {},
    }
    return True


class ActionDispatchDriftError(RuntimeError):
    """Raised when the agent_grammar.yaml action-type catalog and
    actions.py's real dispatch tables (`_LIVE_ACTION_HANDLERS` /
    `_NOOP_ACTION_HANDLERS`) disagree on which action tokens exist.

    This is the automated guard for the "7 dead grammar-resolver stub
    functions" incident (see CLAUDE.md's "Agent action-dispatch SSOT"
    note): a YAML catalog entry with no real dispatch handler (declared
    but never invocable), or a dispatch handler with no catalog entry
    (invocable but never describable to the operator / Lab-chat
    agent-builder, and rejected by `routes/agents.py`'s
    `is_known_action_type()` gate at agent create/update time).
    """


# Dispatch-table action tokens that are DELIBERATELY not declared as their
# own catalog row in agent_grammar.yaml. Each entry must be a real,
# intentional exception — never a silent skip. `validate_action_dispatch_
# catalog()` re-checks every key here on every call and raises if an
# allowlisted token has since gained a catalog row (the exception is no
# longer needed — remove it) or disappeared from the dispatch tables (the
# exception is stale — remove it). This keeps the allowlist from quietly
# decaying into the same kind of silent drift the check exists to catch.
ACTION_DISPATCH_CATALOG_ALLOWLIST: dict[str, str] = {
    # Deliberate alias for the catalog's own "chase_close_positions" token
    # — both route to the exact same `_action_live_chase_close_positions`
    # handler (see actions.py:200-202, `_LIVE_ACTION_HANDLERS`). Kept as a
    # bare dispatch-only alias rather than a second catalog row so the
    # operator-facing/Lab-chat catalog shows one canonical name.
    "chase_close": (
        "deliberate dispatch alias for the catalog's chase_close_positions "
        "token — both route to _action_live_chase_close_positions"
    ),
    # `send_summary` resolved (2026-10, operator decision): a real catalog
    # row was added to agent_grammar.yaml — it's no longer a dispatch-only
    # exception, so its allowlist entry is removed, not left here stale.
}


def validate_action_dispatch_catalog(
    catalog_tokens: Optional[set] = None,
    live_handlers: Optional[dict] = None,
    noop_handlers: Optional[dict] = None,
    allowlist: Optional[dict] = None,
) -> None:
    """Cross-reference agent_grammar.yaml's action-type catalog against
    actions.py's real dispatch tables. Raises `ActionDispatchDriftError`
    (never just logs) on any undeclared mismatch in either direction:

      1. A catalog token with no dispatch handler in either
         `_LIVE_ACTION_HANDLERS` or `_NOOP_ACTION_HANDLERS` — declared but
         never actually invocable (the "7 dead stubs" bug class).
      2. A dispatch-table key with no catalog token and no entry in
         `ACTION_DISPATCH_CATALOG_ALLOWLIST` — invocable but never
         describable to the operator / Lab-chat agent-builder.

    All arguments default to the real, code-level sources (lazy-imported
    here, not at module level, matching `reload()`'s own import style and
    avoiding any import-order coupling to `actions.py`). Validates against
    the static YAML-derived catalog (`grammar._ACTION_PARAMS_SCHEMAS`), NOT
    the live `grammar_tokens` DB rows `reload()` reads — the DB can carry
    operator-added custom tokens and `is_active` flips that are expected to
    differ from this code-reviewed baseline; only the YAML catalog is the
    trust boundary this check is meant to pin.
    """
    if catalog_tokens is None:
        from backend.api.algo.grammar import _ACTION_PARAMS_SCHEMAS
        catalog_tokens = set(_ACTION_PARAMS_SCHEMAS.keys())
    if live_handlers is None or noop_handlers is None:
        from backend.api.algo.actions import (
            _LIVE_ACTION_HANDLERS as _live_default,
            _NOOP_ACTION_HANDLERS as _noop_default,
        )
        if live_handlers is None:
            live_handlers = _live_default
        if noop_handlers is None:
            noop_handlers = _noop_default
    if allowlist is None:
        allowlist = ACTION_DISPATCH_CATALOG_ALLOWLIST

    dispatch_tokens = set(live_handlers.keys()) | set(noop_handlers.keys())

    # Direction 1 — catalog token declared but never dispatched.
    undispatched = sorted(catalog_tokens - dispatch_tokens)
    if undispatched:
        raise ActionDispatchDriftError(
            "agent_grammar.yaml declares action token(s) with NO dispatch "
            f"handler in _LIVE_ACTION_HANDLERS or _NOOP_ACTION_HANDLERS: "
            f"{undispatched} — either wire a handler or remove the catalog "
            f"row (see the '7 dead grammar-resolver stub functions' "
            f"incident in CLAUDE.md)."
        )

    # Direction 2 — dispatch handler with no catalog token, and not an
    # explicit documented exception.
    undeclared = sorted(dispatch_tokens - catalog_tokens - set(allowlist.keys()))
    if undeclared:
        raise ActionDispatchDriftError(
            "actions.py dispatches action token(s) with NO agent_grammar."
            f"yaml catalog row and no ACTION_DISPATCH_CATALOG_ALLOWLIST "
            f"entry: {undeclared} — either add a catalog row or add a "
            f"documented allowlist exception in grammar_registry.py."
        )

    # Allowlist self-tightening — every allowlisted token must STILL be a
    # genuine dispatch-only-no-catalog exception. If it gained a catalog
    # row, or vanished from the dispatch tables, the allowlist entry is
    # stale and must be removed (never left to silently rot).
    stale = sorted(
        tok for tok in allowlist
        if tok in catalog_tokens or tok not in dispatch_tokens
    )
    if stale:
        raise ActionDispatchDriftError(
            "ACTION_DISPATCH_CATALOG_ALLOWLIST entry/entries no longer "
            f"describe a real dispatch-only-no-catalog exception: {stale} "
            f"— remove the stale allowlist entry (it either gained a "
            f"catalog row or no longer has a dispatch handler)."
        )


_TOKEN_LOADERS = {
    ('condition', 'metric'): _load_resolved('metrics', allow_bare=True),
    ('condition', 'scope'): _load_resolved('scopes', allow_bare=True),
    ('condition', 'operator'): _load_resolved('operators', allow_bare=False),
    ('notify', 'channel'): _load_required_resolver('channels'),
    ('notify', 'format'): _load_required_resolver('formats'),
    ('notify', 'template'): _load_template,
    ('log', 'tag'): _load_log_tag,
    ('action', 'action_type'): _load_action,
}

class GrammarRegistry:
    """
    Thread-safe dispatch table. Reloadable at runtime.

    Each `kind_<x>` dict maps a token string to its resolver/handler. Tokens
    that live purely in the schema (e.g. operators, channels, formats) carry
    inert callables the engine can use directly; metric/scope/action_type
    tokens carry their Python resolver.
    """

    def __init__(self):
        self._lock = threading.RLock()
        # condition
        self.metrics:    dict[str, Callable] = {}
        self.scopes:     dict[str, Callable] = {}
        self.operators:  dict[str, Callable] = {}
        # notify
        self.channels:   dict[str, Callable] = {}
        self.formats:    dict[str, Callable] = {}
        self.templates:  dict[str, str]      = {}
        # action
        self.actions:    dict[str, dict]     = {}   # {token: {"fn": callable, "params_schema": {...}}}
        # log
        self.log_tags:   dict[str, dict]     = {}   # {tag: source descriptor}
        # Phase 26 (Sprint 2) — parameterized function-call tokens, e.g.
        # mean_pnl(30). {base_token: (factory_callable, params_schema)}.
        # metric/scope/channel/format resolvers used by .metric()/.scope()/
        # .channel()/.fmt() for a cache-miss call-shape fallback (see
        # _resolve_call_token below). Populated by _load_resolved /
        # _load_required_resolver whenever a row carries params_schema.
        self.metric_factories:  dict[str, tuple] = {}
        self.scope_factories:   dict[str, tuple] = {}
        self.channel_factories: dict[str, tuple] = {}
        self.format_factories:  dict[str, tuple] = {}
        # Per-literal-string cache for resolved call tokens — "mean_pnl(30)"
        # is parsed + bound to its factory once, then served from here on
        # every subsequent lookup for the life of the process (cleared on
        # reload() since factories may have changed).
        self._metric_call_cache:  dict[str, Optional[Callable]] = {}
        self._scope_call_cache:   dict[str, Optional[Callable]] = {}
        self._channel_call_cache: dict[str, Optional[Callable]] = {}
        self._format_call_cache:  dict[str, Optional[Callable]] = {}
        # raw GrammarToken rows keyed by id — kept ONLY so agent_ai.py's
        # _grammar_snapshot() can render full per-token metadata (description/
        # value_type/params_schema) that the processed per-kind dispatch
        # tables above don't uniformly carry; never used for dispatch itself.
        self.tokens:     dict[int, Any]      = {}

    # ── Accessors ──────────────────────────────────────────────────────────
    def metric(self, token: str) -> Optional[Callable]:
        fn = self.metrics.get(token)
        if fn is not None:
            return fn
        return self._resolve_call_token(token, self.metric_factories, self._metric_call_cache)

    def log_tag(self, token: str) -> Optional[dict]:
        return self.log_tags.get(token)

    def scope(self, token: str) -> Optional[Callable]:
        fn = self.scopes.get(token)
        if fn is not None:
            return fn
        return self._resolve_call_token(token, self.scope_factories, self._scope_call_cache)

    def op(self, token: str) -> Optional[Callable]:
        return self.operators.get(token)

    def channel(self, token: str) -> Optional[Callable]:
        fn = self.channels.get(token)
        if fn is not None:
            return fn
        return self._resolve_call_token(token, self.channel_factories, self._channel_call_cache)

    def fmt(self, token: str) -> Optional[Callable]:
        fn = self.formats.get(token)
        if fn is not None:
            return fn
        return self._resolve_call_token(token, self.format_factories, self._format_call_cache)

    def template(self, token: str) -> Optional[str]:
        return self.templates.get(token)

    def action(self, token: str) -> Optional[dict]:
        return self.actions.get(token)

    def find_tokens(self, name: str) -> list:
        """Reverse-resolve a token string to its catalog row(s).

        `self.tokens` is keyed by DB id, not token string, so this walks
        every loaded row filtering on `.token == name`. On-demand
        diagnostic lookup only (backs the `help` action) — not a hot
        path, so no reverse index is maintained. A name reused across
        `token_kind`s (e.g. a notify `channel` and an action `action_type`
        sharing the same string) returns every match; callers must never
        guess which single one was meant.
        """
        return [r for r in self.tokens.values() if getattr(r, "token", None) == name]

    # ── Parameterized function-call tokens (Phase 26 / Sprint 2) ───────────
    def _resolve_call_token(self, token: str, factories: dict, cache: dict) -> Optional[Callable]:
        """Cache-miss fallback for a token the exact-match table didn't
        have. Parses `token` as a single call expression (e.g.
        "mean_pnl(30)"); on success, caches the bound callable under the
        literal string forever (including a None result for a token that
        parses but doesn't resolve, so a bad token isn't re-parsed every
        tick). Never raises — any parse/shape/lookup failure is just a
        None (unknown token), same contract as a plain dict miss."""
        if token in cache:
            return cache[token]
        result = self._parse_call_token(token, factories)
        with self._lock:
            cache[token] = result
        return result

    @staticmethod
    def _parse_call_token(token: str, factories: dict) -> Optional[Callable]:
        try:
            tree = ast.parse(token, mode="eval")
        except (SyntaxError, ValueError):
            return None
        node = tree.body
        if not isinstance(node, ast.Call) or node.keywords:
            return None
        if not isinstance(node.func, ast.Name):
            return None
        entry = factories.get(node.func.id)
        if entry is None:
            return None
        factory, params_schema = entry
        param_count = len(params_schema or {})
        if len(node.args) != param_count:
            return None
        args = []
        for arg_node in node.args:
            # Only plain numeric literals — no names, no arithmetic, no
            # strings/bools. Matches this registry's narrow purpose
            # (window sizes etc.); the whitelist AST evaluator in
            # expr_eval.py is the place for general expressions. A
            # non-positive value (0 or negative) is rejected too — every
            # current params_schema is a window-in-minutes, and a
            # zero/negative window always resolves to a real callable
            # that then silently returns None forever at evaluation time
            # (the window cutoff is >= now, so <2 samples ever fall
            # inside it). Negative literals already failed by accident
            # (ast parses "-5" as UnaryOp(USub, Constant(5)), not
            # Constant(-5)) — this makes that rejection intentional and
            # also catches the 0 case the accident didn't cover.
            if not isinstance(arg_node, ast.Constant) or isinstance(arg_node.value, bool) \
               or not isinstance(arg_node.value, (int, float)) or arg_node.value <= 0:
                return None
            args.append(arg_node.value)
        try:
            return factory(*args)
        except Exception as e:
            logger.warning(f"Grammar registry: parameterized token '{token}' factory raised: {e}")
            return None

    # ── Loader ─────────────────────────────────────────────────────────────

    @staticmethod
    def _load_one_token(r, tables: dict) -> bool:
        """Dispatch a single GrammarToken row into the appropriate table dict.

        Returns True when the token was successfully loaded, False when it
        should be skipped (no resolver where one is required).  Raises on
        import errors so the caller can count skipped rows.
        """
        loader = _TOKEN_LOADERS.get((r.grammar_kind, r.token_kind))
        return loader(r, tables) if loader else False

    async def reload(self) -> None:
        """
        Re-read the full catalog from grammar_tokens and rebuild the dispatch
        table. Idempotent; safe to call at startup, after admin edits, or on
        demand from an operator API endpoint.

        Runs `validate_action_dispatch_catalog()` FIRST, before the DB read
        and before any table swap, so a real drift (or a newly-stale
        allowlist entry) raises before this reload can leave the registry
        half-rebuilt — and so the error surfaces at startup
        (`database.py`'s init path) or at the admin `/grammar/reload`
        endpoint, never silently.
        """
        validate_action_dispatch_catalog()

        from sqlalchemy import select
        from backend.api.database import async_session
        from backend.api.models import GrammarToken
        # Operators live in code — kept here so even a fully-empty DB still
        # has a comparator set available while seeding completes.
        from backend.api.algo.grammar import OPERATORS

        tables: dict[str, Any] = {
            'metrics':   {},
            'scopes':    {},
            'operators': dict(OPERATORS),
            'channels':  {},
            'formats':   {},
            'templates': {},
            'actions':   {},
            'log_tags':  {},
            'factories': {'metrics': {}, 'scopes': {}, 'channels': {}, 'formats': {}},
        }

        async with async_session() as s:
            rows = (await s.execute(
                select(GrammarToken).where(GrammarToken.is_active == True)  # noqa: E712
            )).scalars().all()

        tokens_by_id = {r.id: r for r in rows}

        loaded = skipped = 0
        for r in rows:
            try:
                if self._load_one_token(r, tables):
                    loaded += 1
            except Exception as e:
                skipped += 1
                logger.warning(
                    f"Grammar registry: failed to load "
                    f"{r.grammar_kind}/{r.token_kind}/{r.token} "
                    f"(resolver={r.resolver}): {e}"
                )

        with self._lock:
            self.metrics   = tables['metrics']
            self.scopes    = tables['scopes']
            self.operators = tables['operators']
            self.channels  = tables['channels']
            self.formats   = tables['formats']
            self.templates = tables['templates']
            self.actions   = tables['actions']
            self.log_tags = tables['log_tags']
            self.tokens    = tokens_by_id
            self.metric_factories  = tables['factories']['metrics']
            self.scope_factories   = tables['factories']['scopes']
            self.channel_factories = tables['factories']['channels']
            self.format_factories  = tables['factories']['formats']
            # Factories may have changed shape (or disappeared) on this
            # reload — a stale cached call-token result must not survive it.
            self._metric_call_cache  = {}
            self._scope_call_cache   = {}
            self._channel_call_cache = {}
            self._format_call_cache  = {}

        logger.info(
            f"Grammar registry reloaded — "
            f"metrics={len(self.metrics)} scopes={len(self.scopes)} "
            f"ops={len(self.operators)} channels={len(self.channels)} "
            f"formats={len(self.formats)} templates={len(self.templates)} "
            f"actions={len(self.actions)} (skipped={skipped})"
        )


# Module-level singleton; import as `from backend.api.algo.grammar_registry import REGISTRY`.
REGISTRY = GrammarRegistry()
