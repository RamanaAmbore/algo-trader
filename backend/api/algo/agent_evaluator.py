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
from statistics import median
from typing import Any, Optional

import pandas as pd

from backend.api.algo.grammar_registry import REGISTRY
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Rate-of-change window sizing (fix #1 — audit defect: single-sample spam)
# ═══════════════════════════════════════════════════════════════════════════
#
# The background performance loop polls every ~5m05s. A FIXED rate window
# (e.g. the configured 10-minute default) only ever holds 2 samples at that
# cadence, so the "5-sample quintile smoothing" path in the old
# `_compute_rate` never engaged and every "rate" fired was really just one
# poll's raw Δ — prod fired -28k/min to -33k/min "rates" off a single tick.
#
# Fix: size the effective window from the OBSERVED sample cadence in the
# bucket (median gap over the recent tail), never smaller than the
# configured window, and require >= 3 samples spanning >= 0.8x that
# effective window before a rate leaf is allowed to produce a value.
# Below that, return None (skip — same "missing" convention the rest of
# the grammar uses), not a coerced number.
#
# At the simulator's ~2s tick cadence the median gap is tiny, so
# `max(configured, 2.2 * median_gap)` collapses back to the configured
# window and sim behaviour is unchanged.

def _effective_rate_window_min(hist: list, configured_min: float) -> float:
    """Widen the configured rate window when observed cadence would
    otherwise starve it below the 3-sample minimum. Uses the median gap
    across the last few samples (robust to one-off jitter, e.g. a
    postback-triggered `kick_performance()` firing seconds after a
    normal poll)."""
    if not hist or len(hist) < 3:
        return configured_min
    tail = hist[-6:]
    gaps = [
        (tail[i][0] - tail[i - 1][0]).total_seconds() / 60.0
        for i in range(1, len(tail))
    ]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return configured_min
    return max(configured_min, 2.2 * median(gaps))


def _quintile_rate(window: list, field_idx: int) -> Optional[float]:
    """Quintile-median rate over a >=5-sample window — reduces spike
    sensitivity at window boundaries. Extracted from `windowed_rate` to
    keep it below the radon complexity ceiling."""
    q_size = len(window) // 5
    first_q = window[:q_size]
    last_q  = window[-q_size:]
    old_ts_epoch = median(s[0].timestamp() for s in first_q)
    new_ts_epoch = median(s[0].timestamp() for s in last_q)
    mins = (new_ts_epoch - old_ts_epoch) / 60.0
    if mins <= 0:
        return None
    old_vals = [s[field_idx] for s in first_q if s[field_idx] is not None]
    new_vals = [s[field_idx] for s in last_q  if s[field_idx] is not None]
    if not old_vals or not new_vals:
        return None
    return (median(new_vals) - median(old_vals)) / mins


def _endpoint_rate(window: list, field_idx: int) -> Optional[float]:
    """Oldest-to-newest endpoint rate for a <5-sample window. Extracted
    from `windowed_rate` to keep it below the radon complexity ceiling."""
    oldest = window[0]
    latest = window[-1]
    mins = (latest[0] - oldest[0]).total_seconds() / 60.0
    if mins <= 0:
        return None
    o_val, l_val = oldest[field_idx], latest[field_idx]
    if o_val is None or l_val is None:
        return None
    return (l_val - o_val) / mins


def windowed_rate(hist: list, now, configured_min: float,
                  field_idx: int = 1) -> Optional[float]:
    """
    Pure rate-per-minute computation over an adaptively-sized window.
    Shared by `Context._compute_rate` (live rate metrics) and
    `agent_engine._v2_static_rate_enrichment` (the rate readout surfaced
    on STATIC alert bodies) so both paths apply the identical
    minimum-sample / minimum-span guard — see module docstring above.

    `hist` is a list of `(timestamp, pnl_val, pnl_pct)` tuples (the same
    shape `alert_state['pnl_history']` buckets hold). `field_idx`: 1 =
    pnl ₹, 2 = pnl %.

    Returns None when:
      - fewer than 3 samples fall inside the effective window, or
      - the samples span less than 80% of the effective window (a burst
        of same-second samples must not look like a valid rate), or
      - the field values needed are themselves None/unparseable.
    """
    if not hist or now is None:
        return None
    eff_window = _effective_rate_window_min(hist, configured_min)
    cutoff = now - timedelta(minutes=eff_window)
    window = [s for s in hist if s[0] >= cutoff]
    if len(window) < 3:
        return None
    span_min = (window[-1][0] - window[0][0]).total_seconds() / 60.0
    if span_min < 0.8 * eff_window:
        return None
    if len(window) >= 5:
        return _quintile_rate(window, field_idx)
    return _endpoint_rate(window, field_idx)


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
    # Per-symbol position rows (one dict per Kite row, with tradingsymbol +
    # exchange + quantity + last_price). Used by expiry-aware scopes that
    # need per-contract granularity (sum_positions is per-account
    # aggregated and carries no symbol). Empty when the engine hasn't
    # populated it (e.g. early in startup or in legacy callers).
    position_rows:   list = field(default_factory=list)
    # Underlying spot prices keyed by underlying name (e.g. {"NIFTY":
    # 22150.30, "BANKNIFTY": 48230.10}). Populated by the engine once
    # per tick from broker.ltp on the distinct underlyings of the
    # position book; consumed by is_itm / is_ntm. Empty ⇒ those
    # resolvers return None and their leaves are skipped.
    spot_prices:     dict = field(default_factory=dict)
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
    # Fix #6a — opening-baseline gate, now applied PER RATE LEAF instead of
    # gating the whole agent (agent_engine._cycle_evaluate_agent computes
    # this once per tick from the segment-open-anchored session_start and
    # passes it in). True (default) preserves old behaviour for any caller
    # that doesn't set it explicitly (e.g. legacy/back-compat call sites).
    baseline_live:   bool                   = True
    # Fix #5/#7/#8/#9 support — every leaf that successfully resolves a
    # metric (val is not None) records an observation here, REGARDLESS of
    # whether the op fired. The engine uses this after the tree walk to
    # tell "condition genuinely recovered" (observed, val present, no
    # longer breaching) apart from "data missing this tick" (never
    # observed) — see agent_engine._v2_apply_latch.
    observations:    list                   = field(default_factory=list)

    # ─── Cross-row helpers the resolvers rely on ─────────────────────────

    def used_margin_for(self, account: str) -> Optional[float]:
        """Return utilised margin for an account (or TOTAL) from df_margins.
        Falls back to net column when util debits is zero.
        """
        df = self.df_margins
        if df is None or df.empty or account is None:
            return None
        match = df[df['account'].astype(str) == str(account)]
        if match.empty:
            return None
        try:
            util_debits = float(match.iloc[0].get('util debits', 0) or 0)
            if util_debits > 0:
                return util_debits
            net = float(match.iloc[0].get('net', 0) or 0)
            return net if net > 0 else None
        except Exception:
            return None

    def _compute_rate(self, key: tuple, field_idx: int) -> Optional[float]:
        """Rate-per-minute for a (section, scope) bucket — delegates to the
        shared `windowed_rate` helper (fix #1) so live evaluation and the
        static-alert rate enrichment agree on sample/span requirements."""
        hist = (self.alert_state.get('pnl_history') or {}).get(key, []) or []
        return windowed_rate(hist, self.now, self.rate_window_min, field_idx)

    def rate_abs(self, key: tuple) -> Optional[float]:
        """Rate of change of the absolute metric (₹/min) for this bucket.

        Fix #6a — returns None while the post-open baseline gate hasn't
        cleared yet (per-LEAF, not per-agent: a mixed agent's non-rate
        leaves are unaffected)."""
        if not self.baseline_live:
            return None
        return self._compute_rate(key, field_idx=1)

    def rate_pct(self, key: tuple) -> Optional[float]:
        """Rate of change of the percentage metric (%/min) for this bucket.
        See `rate_abs` for the baseline-gate note."""
        if not self.baseline_live:
            return None
        return self._compute_rate(key, field_idx=2)

    # ─── Phase 24 — rolling-window statistical aggregates ──────────────
    #
    # The rate helpers above only use the oldest and newest sample inside
    # the rate window. Strategy logic increasingly wants the full slice
    # — mean / stdev / drawdown — so an agent can say "exit if P&L has
    # been bleeding for 30 min" instead of "exit if THIS tick crossed a
    # threshold". Same pnl_history dict, different reducer.
    #
    # All helpers return None when the window holds fewer than 2 samples
    # (so a rate metric agent doesn't fire on the first tick of the day).
    # field_idx mirrors _compute_rate: 1 = pnl ₹, 2 = pnl %.

    def _window_slice(self, key: tuple, window_minutes: float) -> list:
        """Samples within `window_minutes` of now for a (section, scope) key."""
        hist = (self.alert_state.get('pnl_history') or {}).get(key, []) or []
        if self.now is None or not hist:
            return hist
        cutoff = self.now - timedelta(minutes=window_minutes)
        return [s for s in hist if s[0] >= cutoff]

    @staticmethod
    def _values(window: list, field_idx: int) -> list[float]:
        out = []
        for s in window:
            v = s[field_idx] if len(s) > field_idx else None
            if v is None:
                continue
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return out

    def window_mean(self, key: tuple, window_minutes: float,
                    field_idx: int = 1) -> Optional[float]:
        vals = self._values(self._window_slice(key, window_minutes), field_idx)
        if len(vals) < 2:
            return None
        return sum(vals) / len(vals)

    def window_stdev(self, key: tuple, window_minutes: float,
                     field_idx: int = 1) -> Optional[float]:
        vals = self._values(self._window_slice(key, window_minutes), field_idx)
        if len(vals) < 2:
            return None
        m = sum(vals) / len(vals)
        var = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
        return var ** 0.5

    def window_range(self, key: tuple, window_minutes: float,
                     field_idx: int = 1) -> Optional[float]:
        vals = self._values(self._window_slice(key, window_minutes), field_idx)
        if len(vals) < 2:
            return None
        return max(vals) - min(vals)

    def window_drawdown(self, key: tuple, window_minutes: float,
                        field_idx: int = 1) -> Optional[float]:
        """
        Peak-to-trough drop within the window. Walks left→right and tracks
        the running max; returns the most-negative (current − running_max),
        so the result is always ≤ 0 and represents "how much below the
        window's peak we've fallen at the worst point". None when fewer
        than 2 samples.
        """
        vals = self._values(self._window_slice(key, window_minutes), field_idx)
        if len(vals) < 2:
            return None
        peak = vals[0]
        worst = 0.0
        for v in vals:
            if v > peak:
                peak = v
            diff = v - peak
            if diff < worst:
                worst = diff
        return worst

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

def evaluate(cond: dict, ctx: Context, _visited: set | None = None) -> list[dict]:
    """
    Walk a condition tree. Returns a list of match dicts (one per triggering
    row); empty list ⇒ the tree did not fire. Malformed nodes log a warning
    and return [] — the engine should never crash because an operator-edited
    agent has a typo.

    `_visited` is the cycle-detection set carried through recursive
    `{"$ref": <fragment-name>}` resolutions. Initialised lazily on the
    first $ref encountered; not part of the public API.
    """
    if cond is None or not isinstance(cond, dict):
        logger.warning(f"Condition evaluator: malformed node (not dict) {cond!r}")
        return []

    # --- $ref: resolve against the fragment registry ----------------------
    # A condition fragment is a saved sub-tree referenced by name. Cycle
    # guard prevents A→B→A from blowing the stack. Missing refs log a
    # warning and return [] — same graceful skip the rest of the
    # evaluator uses.
    if '$ref' in cond:
        ref_name = cond.get('$ref')
        if not isinstance(ref_name, str) or not ref_name:
            logger.warning(f"Condition evaluator: malformed $ref node {cond!r}")
            return []
        visited = _visited or set()
        if ref_name in visited:
            logger.warning(
                f"Condition evaluator: cycle detected — fragment "
                f"'{ref_name}' referenced inside its own resolution chain"
            )
            return []
        from backend.api.algo.template_registry import REGISTRY as _FRAG
        body = _FRAG.get('condition', ref_name)
        if body is None:
            logger.warning(
                f"Condition evaluator: unknown condition fragment "
                f"'{ref_name}' — leaf skipped"
            )
            return []
        if not isinstance(body, dict):
            logger.warning(
                f"Condition evaluator: condition fragment '{ref_name}' "
                f"body is {type(body).__name__}, expected dict"
            )
            return []
        return evaluate(body, ctx, _visited=visited | {ref_name})

    # --- Composite: all / any / not ---------------------------------------
    if 'all' in cond:
        return _eval_all(cond.get('all') or [], ctx, _visited)

    if 'any' in cond:
        children = cond.get('any') or []
        out = []
        for c in children:
            out.extend(evaluate(c, ctx, _visited=_visited))
        return out

    if 'not' in cond:
        inner = cond.get('not')
        fired = bool(evaluate(inner, ctx, _visited=_visited))
        # NOT cannot carry the triggering row forward, so emit a synthetic
        # match carrying the inverted branch for audit.
        return [] if fired else [{'not': inner}]

    # --- Leaf --------------------------------------------------------------
    return _eval_leaf(cond, ctx)


def _all_matches_account_keyed(matches: list[dict]) -> bool:
    """True when EVERY match in the list carries a real per-account row
    (account not None and not 'TOTAL'). Used by `_eval_all` to decide
    whether a child's matches can participate in the cross-account join —
    extracted to keep `_eval_all` below the radon complexity gate."""
    return bool(matches) and all(
        m.get('account') not in (None, 'TOTAL') for m in matches
    )


def _eval_all(children: list, ctx: Context, _visited: set | None) -> list[dict]:
    """
    Evaluate an `all[]` (AND) node.

    Fix #2 — audit defect: `all[]` used to flatten every child's matches
    independently, so `all[acctA_leaf, acctB_leaf]` could fire when NO
    single account satisfied both leaves (prod repro: `loss-margin-low`
    combined one account's `avail_margin=0` with a different, healthy
    account's `avail_margin=373828.52` — each leaf matched on its own
    account, and the flatten made that look like an AND-satisfied fire).

    Fix: when 2+ sibling leaves each produce matches that are ALL keyed
    to a real per-account row (not TOTAL, not None), only accounts
    present in EVERY such child's match set survive — a genuine row-level
    AND. Children whose matches aren't account-keyed (TOTAL-scoped leaves,
    watchlist rows, `not` synthetic matches, …) are not part of the
    intersection — they behave as plain independent AND gates, same as
    before, since there's no "same account" concept to violate for them.
    """
    per_child: list[list[dict]] = []
    for c in children:
        m = evaluate(c, ctx, _visited=_visited)
        if not m:
            return []  # short-circuit — one child false ⇒ AND false
        per_child.append(m)

    keyed   = [mc for mc in per_child if _all_matches_account_keyed(mc)]
    unkeyed = [mc for mc in per_child if not _all_matches_account_keyed(mc)]

    if len(keyed) < 2:
        # No cross-account ambiguity possible — at most one account-keyed
        # child, so nothing to intersect against. Preserve old behaviour.
        out: list[dict] = []
        for mc in per_child:
            out.extend(mc)
        return out

    acct_sets = [{m.get('account') for m in mc} for mc in keyed]
    common_accounts = set.intersection(*acct_sets)
    if not common_accounts:
        return []  # no account satisfied every leaf — AND is false

    out = []
    for mc in keyed:
        out.extend(m for m in mc if m.get('account') in common_accounts)
    for mc in unkeyed:
        out.extend(mc)
    return out


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
        entry = {
            'metric':    metric_tok,
            'scope':     scope_tok,
            'op':        op_tok,
            'threshold': value,
            'value':     val,
            'row':       row,
            'account':   row.get('account'),
        }
        # Fix #5/#7/#8/#9 support — record EVERY successfully-resolved row
        # (matched or not) so the engine can tell "observed and recovered"
        # (real data, condition no longer breaching) apart from "never
        # observed this tick" (fetch timeout/failure/empty frame) when
        # deciding whether to clear a per-key latch. `ctx.observations` is
        # a plain list on the Context the caller supplied — safe even when
        # unused (default empty list, cheap append).
        try:
            ctx.observations.append({**entry, 'fired': fired})
        except Exception:
            pass
        if fired:
            matches.append(entry)
    return matches


# ═══════════════════════════════════════════════════════════════════════════
#  Validation helper (used by admin UI / test harness)
# ═══════════════════════════════════════════════════════════════════════════

def validate(cond: dict) -> list[str]:
    """
    Dry-check a condition tree against the registry. Returns a list of
    human-readable error strings (empty list ⇒ tree looks well-formed).
    Does not evaluate — only verifies the shape and that every referenced
    token exists in the registry. Resolves `{"$ref": <name>}` against the
    fragment registry and recurses into the resolved body so a typo
    deep inside a fragment still surfaces.
    """
    errors: list[str] = []

    def walk(c, path="root", visited: set | None = None):
        if not isinstance(c, dict):
            errors.append(f"{path}: not a dict — {c!r}")
            return
        if '$ref' in c:
            ref = c.get('$ref')
            if not isinstance(ref, str) or not ref:
                errors.append(f"{path}.$ref: must be a non-empty string")
                return
            v = visited or set()
            if ref in v:
                errors.append(
                    f"{path}.$ref: cycle — '{ref}' is already in the "
                    f"resolution chain"
                )
                return
            from backend.api.algo.template_registry import REGISTRY as _FRAG
            body = _FRAG.get('condition', ref)
            if body is None:
                errors.append(
                    f"{path}.$ref: unknown condition fragment '{ref}'"
                )
                return
            walk(body, f"{path}.$ref({ref})", visited=v | {ref})
            return
        if 'all' in c or 'any' in c:
            key = 'all' if 'all' in c else 'any'
            children = c.get(key)
            if not isinstance(children, list) or not children:
                errors.append(f"{path}.{key}: expected non-empty list")
                return
            for i, ch in enumerate(children):
                walk(ch, f"{path}.{key}[{i}]", visited=visited)
            return
        if 'not' in c:
            walk(c.get('not'), f"{path}.not", visited=visited)
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
