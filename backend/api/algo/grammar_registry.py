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
        """
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
