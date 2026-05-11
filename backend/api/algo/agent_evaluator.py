"""
Agent condition evaluator — walks a condition tree, asks the grammar
registry for the callables, and returns the list of triggering matches.

Condition tree schema (same one persisted on Agent.conditions JSONB):
  condition  ::=  leaf
               |  { "all": [condition, ...] }      AND
               |  { "any": [condition, ...] }      OR
               |  { "not": condition }             NOT

  leaf       ::=  { "metric": <metric-token>,
                    "scope":  <scope-token>,
                    "op":     <op-token>,
                    "value":  <literal> }

Evaluation
  all / any / not — tree-level booleans.
  leaf            — resolve (metric, scope, op) against REGISTRY, select
                    rows from scope(ctx), evaluate metric(ctx, row) for each
                    row, and OR-combine: if any row satisfies op(val, value)
                    the leaf fires. This is how scope "any_acct" naturally
                    means "fire when at least one account is in trouble".

evaluate(cond, ctx) returns a list[dict] of matches (one per triggering row).
Empty list ⇒ the tree did not fire. This richer return (compared to a plain
bool) lets the caller build an alert row per triggering row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

from backend.api.algo.grammar_registry import REGISTRY
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Context — everything the resolvers need to produce a value
# ═══════════════════════════════════════════════════════════════════════════
#
# One Context is built per tick by the caller (typically _task_performance)
# and passed to every agent's evaluator. Resolvers take (ctx, row) — the row
# comes from a scope selector, the ctx supplies the cross-row helpers
# (used-margin lookups, rate-of-change from persistent history,
# minutes-since-open for window metrics).
#
# alert_state is the long-lived dict owned by the caller. The Context reads
# from it but does not mutate it; mutation happens in the caller after the
# evaluator returns matches.

@dataclass
class Context:
    sum_holdings:    Optional[pd.DataFrame] = None
    sum_positions:   Optional[pd.DataFrame] = None
    df_margins:      Optional[pd.DataFrame] = None
    # Watchlist rows — each row has tradingsymbol, exchange, last_price,
    # day_change, day_change_percentage, and `account` carrying the
    # watchlist name. Populated by the sim driver from _watchlist_rows;
    # empty on the live path until a future "watchlist polling" task
    # mirrors the simulator behaviour.
    watchlist_rows:  list = field(default_factory=list)
    # The persistent alert_state dict: holds 'pnl_history',
    # 'session_start', 'session_date', 'last_alert' keyed by bucket. Resolvers
    # read it for rate computations and the session-minutes helpers.
    alert_state:     dict                   = field(default_factory=dict)
    now:             Optional[datetime]     = None
    # Market segment definitions (open/close times) for minutes_until_close.
    segments:        list                   = field(default_factory=list)
    rate_window_min: float                  = 10.0
    # The Agent that triggered this evaluation — set by the engine per agent.
    agent:           Any                    = None

    # ─── Cross-row helpers the resolvers rely on ─────────────────────────

    def used_margin_for(self, account: str) -> Optional[float]:
        """Return utilised margin for an account (or TOTAL) from df_margins."""
        df = self.df_margins
        if df is None or df.empty or account is None:
            return None
        match = df[df['account'].astype(str) == str(account)]
        if match.empty:
            return None
        try:
            return float(match.iloc[0].get('util debits', 0) or 0)
        except Exception:
            return None

    def _rate_window_samples(self, key: tuple) -> list:
        """Samples in the last rate_window_min minutes for a (section, scope) key."""
        hist = (self.alert_state.get('pnl_history') or {}).get(key, []) or []
        if self.now is None:
            return hist
        cutoff = self.now - timedelta(minutes=self.rate_window_min)
        return [s for s in hist if s[0] >= cutoff]

    def _compute_rate(self, key: tuple, field_idx: int) -> Optional[float]:
        """
        Generic rate-per-minute over the window. field_idx: 1=pnl_val, 2=pct.
        Returns None when fewer than 2 samples or zero span.
        """
        window = self._rate_window_samples(key)
        if len(window) < 2:
            return None
        oldest = window[0]
        latest = window[-1]
        mins = (latest[0] - oldest[0]).total_seconds() / 60.0
        if mins <= 0:
            return None
        o_val, l_val = oldest[field_idx], latest[field_idx]
        if o_val is None or l_val is None:
            return None
        return (l_val - o_val) / mins

    def rate_abs(self, key: tuple) -> Optional[float]:
        """Rate of change of the absolute metric (₹/min) for this bucket."""
        return self._compute_rate(key, field_idx=1)

    def rate_pct(self, key: tuple) -> Optional[float]:
        """Rate of change of the percentage metric (%/min) for this bucket."""
        return self._compute_rate(key, field_idx=2)

    def minutes_since_open(self) -> float:
        start = self.alert_state.get('session_start')
        if not start or self.now is None:
            return 0.0
        return max(0.0, (self.now - start).total_seconds() / 60.0)

    def minutes_until_close(self) -> float:
        """
        Minutes until the nearest-in-the-future segment close. Returns a very
        large number when no close is known (caller can compare to a bound).
        """
        if self.now is None or not self.segments:
            return 1e9
        nearest = None
        for seg in self.segments:
            close = seg.get('hours_end')
            if not close:
                continue
            today_close = self.now.replace(
                hour=close.hour, minute=close.minute,
                second=0, microsecond=0,
            )
            if today_close >= self.now and (nearest is None or today_close < nearest):
                nearest = today_close
        if nearest is None:
            return 1e9
        return (nearest - self.now).total_seconds() / 60.0


# ═══════════════════════════════════════════════════════════════════════════
#  Condition tree walker
# ═══════════════════════════════════════════════════════════════════════════

def evaluate(cond: dict, ctx: Context) -> list[dict]:
    """
    Walk a condition tree. Returns a list of match dicts (one per triggering
    row); empty list ⇒ the tree did not fire. Malformed nodes log a warning
    and return [] — the engine should never crash because an operator-edited
    agent has a typo.
    """
    if cond is None or not isinstance(cond, dict):
        logger.warning(f"Condition evaluator: malformed node (not dict) {cond!r}")
        return []

    # --- Composite: all / any / not ---------------------------------------
    if 'all' in cond:
        children = cond.get('all') or []
        all_matches = []
        for c in children:
            m = evaluate(c, ctx)
            if not m:
                return []          # short-circuit — one child false ⇒ AND false
            all_matches.extend(m)
        return all_matches

    if 'any' in cond:
        children = cond.get('any') or []
        out = []
        for c in children:
            out.extend(evaluate(c, ctx))
        return out

    if 'not' in cond:
        inner = cond.get('not')
        fired = bool(evaluate(inner, ctx))
        # NOT cannot carry the triggering row forward, so emit a synthetic
        # match carrying the inverted branch for audit.
        return [] if fired else [{'not': inner}]

    # --- Leaf --------------------------------------------------------------
    return _eval_leaf(cond, ctx)


def _eval_leaf(leaf: dict, ctx: Context) -> list[dict]:
    try:
        metric_tok = leaf['metric']
        scope_tok  = leaf['scope']
        op_tok     = leaf['op']
        value      = leaf.get('value')
    except KeyError as e:
        logger.warning(f"Condition leaf missing key {e}: {leaf!r}")
        return []

    metric_fn = REGISTRY.metric(metric_tok)
    scope_fn  = REGISTRY.scope(scope_tok)
    op_fn     = REGISTRY.op(op_tok)

    if not metric_fn or not scope_fn or not op_fn:
        logger.warning(
            f"Condition leaf refers to unknown token — "
            f"metric={metric_tok} ({bool(metric_fn)}) "
            f"scope={scope_tok} ({bool(scope_fn)}) "
            f"op={op_tok} ({bool(op_fn)})"
        )
        return []

    try:
        rows = scope_fn(ctx) or []
    except Exception as e:
        logger.warning(f"Scope selector '{scope_tok}' failed: {e}")
        return []

    matches = []
    for row in rows:
        try:
            val = metric_fn(ctx, row)
        except Exception as e:
            logger.warning(f"Metric resolver '{metric_tok}' failed on row {row.get('account','?')}: {e}")
            continue
        if val is None:
            continue
        try:
            fired = bool(op_fn(val, value))
        except Exception as e:
            logger.warning(f"Operator '{op_tok}' failed comparing {val!r} vs {value!r}: {e}")
            continue
        if fired:
            matches.append({
                'metric':    metric_tok,
                'scope':     scope_tok,
                'op':        op_tok,
                'threshold': value,
                'value':     val,
                'row':       row,
                'account':   row.get('account'),
            })
    return matches


# ═══════════════════════════════════════════════════════════════════════════
#  Validation helper (used by admin UI / test harness)
# ═══════════════════════════════════════════════════════════════════════════

def validate(cond: dict) -> list[str]:
    """
    Dry-check a condition tree against the registry. Returns a list of
    human-readable error strings (empty list ⇒ tree looks well-formed).
    Does not evaluate — only verifies the shape and that every referenced
    token exists in the registry.
    """
    errors: list[str] = []

    def walk(c, path="root"):
        if not isinstance(c, dict):
            errors.append(f"{path}: not a dict — {c!r}")
            return
        if 'all' in c or 'any' in c:
            key = 'all' if 'all' in c else 'any'
            children = c.get(key)
            if not isinstance(children, list) or not children:
                errors.append(f"{path}.{key}: expected non-empty list")
                return
            for i, ch in enumerate(children):
                walk(ch, f"{path}.{key}[{i}]")
            return
        if 'not' in c:
            walk(c.get('not'), f"{path}.not")
            return
        for k in ('metric', 'scope', 'op'):
            if k not in c:
                errors.append(f"{path}: leaf missing '{k}'")
        if c.get('metric') and REGISTRY.metric(c['metric']) is None:
            errors.append(f"{path}: unknown metric token '{c['metric']}'")
        if c.get('scope') and REGISTRY.scope(c['scope']) is None:
            errors.append(f"{path}: unknown scope token '{c['scope']}'")
        if c.get('op') and REGISTRY.op(c['op']) is None:
            errors.append(f"{path}: unknown operator token '{c['op']}'")

    walk(cond)
    return errors
