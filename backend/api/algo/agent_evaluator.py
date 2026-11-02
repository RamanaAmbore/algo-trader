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
from typing import Any, Callable, Optional

import pandas as pd

from backend.api.algo import grammar
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
    # watchlist name. Populated by the sim driver from _watchlist_rows
    # on the sim path; populated once per `_task_performance` cycle by
    # `background._perf_fetch_watchlist_rows` on the live path (fixed —
    # see CLAUDE.md "Alert evaluation and latching" / Sprint 21+ notes).
    # Both paths build rows via the same `sim/driver.py:
    # _sd_build_watchlist_row` helper so the row shape is identical.
    watchlist_rows:  list = field(default_factory=list)
    # Per-symbol position rows (one dict per Kite row, with tradingsymbol +
    # exchange + quantity + last_price). Used by expiry-aware scopes that
    # need per-contract granularity (sum_positions is per-account
    # aggregated and carries no symbol). Empty when the engine hasn't
    # populated it (e.g. early in startup or in legacy callers).
    position_rows:   list = field(default_factory=list)
    # Sprint 21 — currently OPEN/active AlgoOrder rows (mode='live', status
    # not in ALGO_ORDER_TERMINAL_STATUSES), one dict per order, carrying
    # account/tradingsymbol/exchange/side/quantity/price/status/order_id.
    # Populated once per `_task_performance` cycle by
    # `background._perf_fetch_orders_rows` — a cheap DB query against the
    # real algo_orders table, NOT a live broker call. Mirrors
    # `position_rows`'s existing pattern exactly. Empty on the sim/replay
    # path (those drivers don't populate it) and before the live task's
    # first tick.
    orders_rows:     list = field(default_factory=list)
    # Sprint 21 — pre-tracked ad-hoc market-data rows (one dict per
    # symbol, carrying tradingsymbol/exchange/last_price/bid/ask/volume)
    # for every symbol referenced by at least one active agent's
    # `quote.*` condition leaf. Populated once per `_task_performance`
    # cycle by `background._perf_build_quote_rows`, which reuses
    # `routes/quote.py`'s existing `_fetch_ltp` fetch infrastructure —
    # never a live per-condition broker round-trip. Mirrors
    # `watchlist_rows`'s existing pattern: cheap, in-memory, same
    # staleness profile as every other scope.
    quote_rows:      list = field(default_factory=list)
    # Sprint G — unified `symbols.*` scope family rows (one dict per row,
    # each carrying tradingsymbol/exchange/last_price/day_change/
    # day_change_percentage plus a `kind` field — 'watchlist' | 'pinned' |
    # 'gainers' | 'losers'). Populated once per `_task_performance` cycle
    # by `background._perf_build_symbols_rows`, which reuses the EXISTING
    # watchlist-row fetch (`_perf_fetch_watchlist_rows`'s own raw-fetch
    # helper) for 'watchlist'/'pinned' and the existing winners/losers
    # movers computation (`routes/watchlist.py`) for 'gainers'/'losers' —
    # never a live per-condition broker round-trip. Empty when the engine
    # hasn't populated it (sim/replay drivers don't; gated out when no
    # active agent references `symbols.*` this cycle).
    symbols_rows:    list = field(default_factory=list)
    # Underlying spot prices keyed by underlying name (e.g. {"NIFTY":
    # 22150.30, "BANKNIFTY": 48230.10}). Populated by the engine once
    # per tick from broker.ltp on the distinct underlyings of the
    # position book; consumed by is_itm / is_ntm. Empty ⇒ those
    # resolvers return None and their leaves are skipped.
    spot_prices:     dict = field(default_factory=dict)
    # Log records not yet offered to this agent (see log_feed). Each record is
    # matched once, by the log leaf, on the cycle that first sees it.
    log_records:     list = field(default_factory=list)
    # The persistent alert_state dict: holds 'pnl_history',
    # 'session_start', 'session_date', 'last_alert' keyed by bucket. Resolvers
    # read it for rate computations and the session-minutes helpers.
    alert_state:     dict                   = field(default_factory=dict)
    now:             Optional[datetime]     = None
    # Market segment definitions (open/close times) for minutes_until_close.
    segments:        list                   = field(default_factory=list)
    # Market-cycle event tokens (2026-10-22) — the per-segment flags
    # computed by `agent_engine._ae_segment_flags()` (nse_open/mcx_open/
    # nse_closed/mcx_closed/nse_holiday/mcx_holiday/minutes_since_{nse,
    # mcx}_open/minutes_since_{nse,mcx}_close/minutes_until_{nse,mcx}_open/
    # minutes_until_{nse,mcx}_close), threaded through as a flat dict
    # rather than individual dataclass fields so new segment-prefixed keys
    # don't require touching this dataclass again. Deliberately a
    # DIFFERENT dict from `alert_state`/sim's `market_state` override key
    # (that one is the simulator's INPUT override; this is the engine's
    # OUTPUT per-tick computation — conflating them would let a stale
    # sim override silently freeze these flags on a reused context).
    # Empty `{}` on any caller that doesn't populate it (log-feed-driven
    # event agents, hand-built test Context) — every grammar.py resolver
    # reading this degrades to `None`, never a false 0/closed reading.
    segment_flags:   dict                   = field(default_factory=dict)
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
    # A label bound by an ordinary assignment statement earlier in this
    # agent's own statement (anything before `WHEN` — no introducer
    # keyword required) resolves, once per cycle, to a single scalar
    # BEFORE this tree walk runs: `agent_engine._cycle_evaluate_given_
    # bindings()` computes it, and `agent_engine._cycle_evaluate_agent()`
    # copies this agent's own slice of that result onto this field right
    # before calling `evaluate()`. Keyed by `name.lower()`, matching the
    # lowercase-everywhere convention every OTHER reference to a bound
    # label already uses (`{"$given": name}` action-param markers, DO-
    # action params, ALERT message parts). Empty `{}` for every caller
    # that never populates it (hand-built Context in a test, a log-feed-
    # driven event agent, any agent whose statement never binds a label)
    # — a leaf referencing a name absent from this dict then simply fails
    # to match (see `_eval_leaf`'s marker-metric branch below), never an
    # exception, never a false 0/None coerced into a comparable value.
    given_bindings:  dict                   = field(default_factory=dict)

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
        # Sprint D2 (Agent/Bracket/Template unification phase) — resolves
        # against TemplateRegistry, whose in-memory cache is sourced
        # ONLY from kind='template' Agent rows (the legacy agent_fragments
        # table + its fallback-read path were retired this sprint — see
        # template_registry.reload()'s own docstring for the condition/
        # notify/collision bucketing rules).
        from backend.api.algo.template_registry import REGISTRY as _TMPL
        body = _TMPL.get('condition', ref_name)
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


def _match_join_key(m: dict) -> "tuple | None":
    """Return a typed join key for cross-row AND-intersection.

    Fix B (underlying-divergence feature) — generalizes the old account-
    only `_all_matches_account_keyed` predicate into a TYPED key so
    `_eval_all` can recognize "underlying" as a second join dimension
    alongside "account", without ever conflating the two (an account-
    keyed child and an underlying-keyed child have no shared identity to
    intersect against — see `_eval_all`'s docstring).

    Returns:
      ('acct', account)      — a real per-account row (account not None,
                                not 'TOTAL').
      ('und', underlying)    — no real account identity, but a real
                                `underlying` (e.g. a `positions.
                                any_underlying` match, which rolls up
                                across every account holding that
                                underlying and so carries no single
                                `account`).
      None                   — unkeyed (TOTAL-scoped leaves, watchlist
                                rows, `not` synthetic matches, …) — not
                                part of any intersection.
    """
    acct = m.get('account')
    if acct not in (None, 'TOTAL'):
        return ('acct', acct)
    underlying = m.get('underlying')
    if underlying:
        return ('und', underlying)
    return None


def _all_matches_keyed_by(matches: list[dict], key_type: str) -> bool:
    """True when EVERY match in the list carries a join key of the given
    TYPE ('acct' or 'und'). Used by `_eval_all` to decide whether a
    child's matches can participate in that dimension's cross-row join —
    extracted to keep `_eval_all` below the radon complexity gate."""
    if not matches:
        return False
    for m in matches:
        jk = _match_join_key(m)
        if jk is None or jk[0] != key_type:
            return False
    return True


_JOIN_KEY_TYPES = ('acct', 'und')


def _eval_all(children: list, ctx: Context, _visited: set | None) -> list[dict]:
    """
    Evaluate an `all[]` (AND) node.

    Fix #2 — audit defect: `all[]` used to flatten every child's matches
    independently, so `all[acctA_leaf, acctB_leaf]` could fire when NO
    single account satisfied both leaves (prod repro: `loss-margin-low`
    combined one account's `avail_margin=0` with a different, healthy
    account's `avail_margin=373828.52` — each leaf matched on its own
    account, and the flatten made that look like an AND-satisfied fire).

    Fix B (underlying-divergence feature) — the SAME bug exists on the
    underlying dimension: `all[underlying_loss_divergence@any_underlying,
    any[pnl_rate_abs@any_underlying, pnl_rate_pct@any_underlying]]` could
    fire when underlying A breaches the divergence leaf and a DIFFERENT
    underlying B independently breaches the rate leaf — no single
    underlying satisfied the whole tree. Generalized: for EACH join-key
    TYPE independently ('acct' then 'und'), when 2+ sibling children each
    produce matches that are ALL keyed to that same type, only the keys
    (accounts, or underlyings) present in EVERY such child's match set
    survive — a genuine row-level AND for that dimension. An account-keyed
    child is never intersected against an underlying-keyed child (no
    shared identity). Children with fewer than 2 same-typed siblings, or
    no join key at all (TOTAL-scoped leaves, watchlist rows, `not`
    synthetic matches, …), behave as plain independent AND gates, same as
    before — there's no "same row" concept to violate for them.
    """
    per_child: list[list[dict]] = []
    for c in children:
        m = evaluate(c, ctx, _visited=_visited)
        if not m:
            return []  # short-circuit — one child false ⇒ AND false
        per_child.append(m)

    consumed = [False] * len(per_child)
    out: list[dict] = []
    for key_type in _JOIN_KEY_TYPES:
        idx = [i for i, mc in enumerate(per_child) if _all_matches_keyed_by(mc, key_type)]
        if len(idx) < 2:
            # No cross-row ambiguity possible for this join dimension —
            # at most one same-typed keyed child, nothing to intersect.
            continue
        key_sets = [{_match_join_key(m)[1] for m in per_child[i]} for i in idx]
        common = set.intersection(*key_sets)
        if not common:
            return []  # no row satisfied every leaf of this dimension — AND is false
        for i in idx:
            out.extend(m for m in per_child[i] if _match_join_key(m)[1] in common)
            consumed[i] = True

    for i, mc in enumerate(per_child):
        if not consumed[i]:
            out.extend(mc)
    return out


_LEVEL_NO = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


# A `where={...}` value is normally a plain scalar, matched by exact
# equality (unchanged default below). When a value is a dict with
# exactly one key from this table (e.g. `where={name: {in: [...]}}`,
# `where={name: {not_in: [...]}}`), that operator is applied instead.
# Already grammar-parseable today with NO parser change — `agents.js`'s
# `_objLiteralParseValue()` already accepts a nested object_literal as
# any object_literal's value (confirmed 2026-10-25) — this is purely a
# runtime extension local to `_eval_log`'s own `where` matching, per
# explicit operator instruction: a new log-condition operator belongs in
# the log token/function itself, not as a general grammar change.
_WHERE_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "in": lambda actual, expected: actual in (expected or []),
    "not_in": lambda actual, expected: actual not in (expected or []),
}


def _where_value_matches(actual: Any, expected: Any) -> bool:
    """True when `actual` satisfies a single `where={key: expected}` entry.

    A dict `expected` with exactly one `_WHERE_OPERATORS` key applies that
    operator. Anything else (a plain scalar, or a dict that doesn't match
    this shape) falls back to the original exact-equality comparison —
    every existing agent's `where` clause uses plain scalars, so this is
    byte-for-byte the same behavior for them; never raises on this hot
    evaluation path, so a malformed operator dict just fails to match
    rather than crashing agent evaluation."""
    if isinstance(expected, dict) and len(expected) == 1:
        ((op_name, op_arg),) = expected.items()
        op_fn = _WHERE_OPERATORS.get(op_name)
        if op_fn is not None:
            return op_fn(actual, op_arg)
    return actual == expected


def _eval_log(leaf: dict, ctx: Context) -> list[dict]:
    """Match log records by tag, minimum level, and extra-field values —
    each `where` value is either an exact-equality scalar or a single
    `_WHERE_OPERATORS` operator dict (`in`/`not_in`), see `_where_value_matches`."""
    spec = leaf.get("log") or {}
    tag = spec.get("tag")
    min_level = _LEVEL_NO.get(str(spec.get("min_level", "INFO")).upper(), 20)
    where = spec.get("where") or {}
    matches = []
    for rec in ctx.log_records or []:
        tags = rec.get("tags") or []
        if tag not in tags or _LEVEL_NO.get(rec.get("level"), 0) < min_level:
            continue
        extra = rec.get("extra") or {}
        if any(not _where_value_matches(extra.get(k), v) for k, v in where.items()):
            continue
        matches.append({
            "metric": "log", "scope": tag, "op": "match", "threshold": spec,
            "value": rec.get("id"), "account": extra.get("account"),
            "row": {"id": rec.get("id"), "level": rec.get("level"), "message": rec.get("message", "")[:200],
                    "tags": tags, "extra": extra},
            "tags": tags, "fired": True,
        })
    return matches


def _resolve_metric_fn(metric_tok: Optional[str], scope_tok: Optional[str]) -> Optional[Callable]:
    """Resolve a leaf's metric token to a callable `(ctx, row) -> value`.

    Primary path: a real registered metric token (`REGISTRY.metric()`).

    Fallback: Sprint 15's per-scope exposed-column registry
    (`grammar.EXPOSED_COLUMNS` / `exposed_columns_for_scope()`) lets a
    leaf dot-read any real row column that has no hand-written metric
    resolver (e.g. `qty`, `symbol`, `realised`, `option_premium`...).
    Only consulted when the real registry has no match for `metric_tok`
    — a genuine metric token is never shadowed or double-resolved
    through this path. Scoped per `scope_tok`: an exposed name that
    exists on a DIFFERENT scope's schema (e.g. `qty` is not on
    `funds.any_acct`) still resolves to None here, same as any other
    unknown token.

    Shared by `_eval_leaf` (runtime) and `validate()` (save-time dry
    check) so the two never disagree about which tokens are legal.
    """
    fn = REGISTRY.metric(metric_tok)
    if fn is not None:
        return fn
    exposed = grammar.exposed_columns_for_scope(scope_tok).get(metric_tok)
    if not exposed:
        return None

    def _exposed_column_resolver(ctx, row, _col=exposed):
        return grammar.read_exposed_value(row, _col)

    return _exposed_column_resolver


# ═══════════════════════════════════════════════════════════════════════════
#  Bound-label references ($given) inside a WHEN leaf's comparator positions
# ═══════════════════════════════════════════════════════════════════════════
#
# A label bound by an ordinary assignment statement earlier in the SAME
# agent's own preamble (anything before `WHEN` — no introducer keyword) can
# be read back inside a leaf's metric OR value position. Wire shape: the
# EXISTING `{"$given": "<name>"}` marker (already used for a DO action
# param referencing an operator-expression label — see
# `actions.py:resolve_given_ref_params`) appears in place of a plain
# string token. No new top-level leaf shape, no discriminator key — a
# leaf's `metric`/`value` field is either its ordinary type (a string
# token / a literal) or this single-key marker dict.
#
# The bound value is a single per-tick scalar the engine already resolved
# BEFORE this tree walk runs (`agent_engine._cycle_evaluate_given_bindings`,
# threaded onto `Context.given_bindings` by `_cycle_evaluate_agent`) — not
# a per-row scope selection, so a marker-metric leaf never consults
# `REGISTRY.scope()`/`_resolve_metric_fn()` at all.

_GIVEN_LEAF_SCOPE = "preamble"


def _is_given_ref_marker(v: Any) -> bool:
    """True iff `v` is EXACTLY the single-key marker dict
    `{"$given": "<name>"}` — mirrors `actions.py:_is_given_ref_marker`
    (duplicated here, not imported, to avoid a new cross-module
    dependency `agent_evaluator.py` otherwise has no reason to carry)."""
    return isinstance(v, dict) and len(v) == 1 and "$given" in v


def _given_marker_name(marker: dict) -> Optional[str]:
    """Extract and lowercase a marker's bound-label name, or None when
    malformed (missing/non-string/empty). Lowercased to match the SAME
    convention every other reference to a bound label already uses —
    `agent_engine._cycle_evaluate_given_bindings` stores bindings keyed
    by `name.lower()`."""
    raw = marker.get("$given")
    if not isinstance(raw, str) or not raw:
        return None
    return raw.lower()


def _resolve_given_operand(raw: Any, ctx: Context) -> "tuple[Any, bool]":
    """Resolve a leaf operand that MAY be a `{"$given": name}` marker.

    Returns `(value, ok)`. `ok=False` is the fail-closed signal: the
    marker was malformed, OR the named binding is absent from
    `ctx.given_bindings` this cycle, OR it resolved to `None` (its own
    upstream evaluation failed/was skipped). The caller MUST treat
    `ok=False` as a clean no-match — never substitute a coerced default,
    never raise. A non-marker `raw` passes through unchanged (`ok=True`)
    — this is the common case for an ordinary literal RHS value."""
    if not _is_given_ref_marker(raw):
        return raw, True
    name = _given_marker_name(raw)
    if name is None:
        return None, False
    bindings = ctx.given_bindings or {}
    if name not in bindings or bindings[name] is None:
        return None, False
    return bindings[name], True


def _eval_given_metric_leaf(leaf: dict, ctx: Context, marker: dict) -> list[dict]:
    """Evaluate a WHEN leaf whose `metric` is a `{"$given": "<name>"}`
    marker — a reference to a label bound earlier in this same agent's
    own statement. See the module section comment above for the wire
    shape and `Context.given_bindings` for where the per-tick value
    comes from.

    Fail-closed, non-negotiable: a missing/unresolved binding (this
    leaf's own metric OR its value operand, when the value is also a
    marker) makes this leaf a clean no-match — mirrors the EXISTING
    `if val is None: continue` guard the ordinary per-row path below
    applies to every registry-resolved metric, just a different source
    for the value.

    The match/observation entry's own `metric` field is the bound
    NAME ITSELF (a plain, hashable string, e.g. `"gap"`) — never the
    marker dict — because `agent_engine._latch_key`/`_V2_LATCH` hash
    this value; a dict there would raise `TypeError: unhashable type`
    and abort evaluation for every agent still queued this cycle
    (`run_cycle`'s per-agent loop has no surrounding try/except). The
    name is already unique per agent (an operator cannot assign the
    same label twice in one statement), so the existing
    `(agent_slug, metric, scope, account)` latch key works unmodified —
    no composite-key redesign needed. `account` is stamped 'TOTAL' (a
    single per-tick scalar is an aggregate value, not a per-row one),
    matching every other aggregate scope's own convention."""
    name = _given_marker_name(marker)
    op_tok = leaf.get('op')
    op_fn = REGISTRY.op(op_tok)
    if name is None or op_fn is None:
        logger.warning(
            f"Condition leaf has a malformed bound-label metric reference "
            f"or unknown operator — metric={marker!r} op={op_tok!r}"
        )
        return []

    bindings = ctx.given_bindings or {}
    if name not in bindings or bindings[name] is None:
        # Either never bound by this statement, or its own evaluation
        # failed/was skipped this tick (see Context.given_bindings'
        # docstring) — never an exception, never a false match.
        return []
    val = bindings[name]

    value, ok = _resolve_given_operand(leaf.get('value'), ctx)
    if not ok:
        return []

    try:
        fired = bool(op_fn(val, value))
    except Exception as e:
        logger.warning(
            f"Operator '{op_tok}' failed comparing bound value "
            f"{val!r} (label={name!r}) vs {value!r}: {e}"
        )
        return []

    entry = {
        'metric':    name,
        'scope':     _GIVEN_LEAF_SCOPE,
        'op':        op_tok,
        'threshold': value,
        'value':     val,
        'row':       {},
        'account':   'TOTAL',
        'underlying': None,
    }
    try:
        ctx.observations.append({**entry, 'fired': fired})
    except Exception:
        pass
    return [entry] if fired else []


# ═══════════════════════════════════════════════════════════════════════════
#  Generic where-filter on a scope segment (DSL dataframe-access gaps, item 2)
# ═══════════════════════════════════════════════════════════════════════════
#
# Wire shape (confirmed against the frontend's actual compiler —
# `agents.js:_compileWhereLeaf`/`_compileWhereComparatorValue`): an
# OPTIONAL `where` key on an ordinary leaf, SIBLING of the existing
# `scope_filter` key —
#   {"metric": "pnl", "scope": "positions.expiring_today", "op": "<=",
#    "value": -30000,
#    "where": {"all": [{"column": "exchange", "op": "==", "value": "MCX"},
#                       {"column": "is_unhedged", "op": "==", "value": 1}]}}
# — reusing the EXISTING top-level condition-tree composite shapes
# (`all`/`any`/`not`) verbatim. A where-LEAF is `{"column": <bare exposed
# column name>, "op": <operator token>, "value": <literal>}` — deliberately
# the KEY NAME `column`, NOT `metric` (the outer leaf's own key), so a
# where-leaf can never be mistaken for a metric/scope leaf missing a
# field (the frontend's own rationale for this choice — see
# `agents.js`'s where-compiler module comment). No `scope` key: there is
# no nested `@`, the bare name resolves directly against THAT row's own
# exposed-column schema (the leaf's own outer `scope`), never a fresh
# scope/metric lookup.
#
# Deliberately a SEPARATE, pure (row, schema) -> bool walker — NOT a
# re-entry into `evaluate()`/`_eval_leaf`. Reusing those would re-run
# `scope_fn` per where-leaf, let `REGISTRY.metric` shadow a bare column
# name, append spurious `ctx.observations` entries (corrupting latch
# recovery), and drag in `_eval_all`'s cross-row join — none of which
# apply to "is this ONE already-selected row's own field value true of
# this sub-expression".
#
# Validated ONCE, structurally, against the WHOLE tree before any row is
# evaluated (`_where_tree_well_formed`) — never per leaf per row. A
# malformed/unknown-column leaf wrapped in `not` would otherwise evaluate
# False on every row, and `not` would flip that to True — matching EVERY
# row (fail OPEN), the opposite of the required fail-CLOSED "skip this
# whole where-filter" contract. `_apply_where_filter` returns `None`
# (never a partial list) on that failure; the caller treats `None`
# exactly like an unknown metric/scope/op token — the whole leaf is
# skipped, logged, never a crash.
#
# No `$given`/`$row` marker support inside a where-expression — explicit
# scope boundary (not requested by the design, and a dict-shaped `value`
# would be ambiguous with those markers) — a dict `value` on a where-leaf
# is rejected as malformed during the structural check.

def _where_operators() -> dict:
    """`grammar.OPERATORS` — the SAME comparator dict `REGISTRY.op()` is
    seeded from at registry-reload time (see `grammar_registry.reload()`).
    Read lazily (not at import time) only so this module never needs to
    care about `grammar` module init ordering; there is no real lazy
    state here, `grammar.OPERATORS` is a plain module-level dict."""
    return grammar.OPERATORS


def _where_leaf_well_formed(node: Any, schema: dict) -> bool:
    """Structural + schema check for ONE where-leaf — no row data
    involved. `node` must be a dict with a `column` that is a real
    exposed column on `schema`, a known operator token, and a `value`
    that is present and not itself a dict (no $given/$row markers; see
    the module section comment above)."""
    if not isinstance(node, dict):
        return False
    name = node.get('column')
    op_tok = node.get('op')
    if not isinstance(name, str) or not name or name not in schema:
        return False
    if not isinstance(op_tok, str) or op_tok not in _where_operators():
        return False
    if 'value' not in node:
        return False
    return not isinstance(node.get('value'), dict)


def _where_tree_well_formed(node: Any, schema: dict) -> bool:
    """Recursively validate an entire where sub-tree against `schema`
    ONCE — see the module section comment above for why this must run
    before any row is evaluated, not per row."""
    if not isinstance(node, dict) or not node:
        return False
    if 'all' in node or 'any' in node:
        children = node.get('all') if 'all' in node else node.get('any')
        if not isinstance(children, list) or not children:
            return False
        return all(_where_tree_well_formed(c, schema) for c in children)
    if 'not' in node:
        return _where_tree_well_formed(node.get('not'), schema)
    return _where_leaf_well_formed(node, schema)


def _eval_where_leaf(node: dict, row: dict, schema: dict) -> bool:
    """Evaluate ONE pre-validated where-leaf against `row`. A missing or
    NaN column value excludes the row via THIS leaf regardless of
    operator (missing-vs-zero convention — never silently matched via,
    say, `!= value` comparing against a bare `None`). Operator failures
    (e.g. a malformed `between` range) are caught and treated as
    non-matching, never raised — this runs inside a per-row list
    comprehension with no surrounding try/except at the call site."""
    col = schema[node['column']]
    actual = grammar.read_exposed_value(row, col)
    if actual is None:
        return False
    op_fn = _where_operators().get(node['op'])
    try:
        return bool(op_fn(actual, node['value']))
    except Exception as e:
        logger.warning(
            f"where-filter: operator '{node['op']}' failed comparing "
            f"{actual!r} vs {node['value']!r}: {e}"
        )
        return False


def _eval_where_tree(node: dict, row: dict, schema: dict) -> bool:
    """Evaluate a pre-validated where sub-tree against one row."""
    if 'all' in node:
        return all(_eval_where_tree(c, row, schema) for c in node['all'])
    if 'any' in node:
        return any(_eval_where_tree(c, row, schema) for c in node['any'])
    if 'not' in node:
        return not _eval_where_tree(node['not'], row, schema)
    return _eval_where_leaf(node, row, schema)


def _apply_where_filter(rows: list, where: Any, scope_tok: str) -> "list | None":
    """Narrow `rows` by an optional leaf-level `where` sub-tree.

    Returns `None` (never a partial/best-effort list) when `where` is
    structurally malformed, or references a column not exposed on
    `scope_tok` — the caller fails the WHOLE leaf closed, same
    treatment as an unknown metric/scope/op token. A well-formed
    where-tree that narrows every row away is a legitimate, meaningful
    `[]` — "nothing passed the filter" — not a failure.

    `where is None` (the common case — no caller uses this feature)
    returns `rows` unchanged, a true no-op."""
    if where is None:
        return rows
    schema = grammar.exposed_columns_for_scope(scope_tok)
    if not schema or not _where_tree_well_formed(where, schema):
        return None
    return [r for r in rows if _eval_where_tree(where, r, schema)]


# ═══════════════════════════════════════════════════════════════════════════
#  Row-set aggregates (DSL dataframe-access gaps, item 3)
# ═══════════════════════════════════════════════════════════════════════════
#
# `count` / `sum(column)` / `max(column)` / `avg(column)` — see
# `grammar_tokens/metric_count.py` and its three siblings. Unlike every
# other metric resolver `(ctx, row) -> value`, a row-set aggregate's
# resolved callable carries a truthy `rowset_aggregate` attribute and has
# the signature `(ctx, rows, scope_tok) -> value`: it reduces ONCE across
# the scope's whole (already scope_filter + where-filtered) row list for
# this tick, rather than being called once per row. `_eval_leaf` detects
# this via `getattr(metric_fn, 'rowset_aggregate', False)` and branches
# to `_eval_leaf_rowset_aggregate` below instead of the ordinary per-row
# loop — collapsing to AT MOST one synthetic match, regardless of the
# underlying scope's own suffix (`sum(qty)@positions.any_acct` is ONE
# number, never a fan-out). `agent_engine._ae_is_aggregate_match` checks
# the SAME `rowset_aggregate` key on the match entry so `$row` fails
# closed on it exactly like a `.total`-scoped leaf.

def _eval_leaf_rowset_aggregate(metric_fn: Callable, op_fn: Callable,
                                 metric_tok: str, scope_tok: str, op_tok: str,
                                 value: Any, raw_rows: list, rows: list,
                                 ctx: Context) -> list[dict]:
    """Row-set aggregate branch of `_eval_leaf`.

    `raw_rows` is the scope resolver's OWN unfiltered output (before
    `scope_filter`/`where`). An EMPTY `raw_rows` means the scope itself
    produced nothing this tick — outage or a genuinely flat book are
    indistinguishable here, same as every ordinary per-row leaf's "no
    rows this tick -> no match" default — so the aggregate is skipped
    entirely rather than computed over an empty set. A non-empty
    `raw_rows` narrowed to an EMPTY `rows` by `scope_filter`/`where` IS
    meaningful (a confirmed zero after filtering) and is passed through
    to the reducer — each aggregate factory decides for itself what an
    empty filtered set means (0 for count/sum, None/undefined for
    max/avg — see `metric_sum.py`'s module docstring)."""
    if not raw_rows:
        return []
    try:
        val = metric_fn(ctx, rows, scope_tok)
    except Exception as e:
        logger.warning(
            f"Row-set aggregate metric '{metric_tok}' failed on scope "
            f"'{scope_tok}': {e}"
        )
        return []
    if val is None:
        return []
    try:
        fired = bool(op_fn(val, value))
    except Exception as e:
        logger.warning(f"Operator '{op_tok}' failed comparing {val!r} vs {value!r}: {e}")
        return []
    entry = {
        'metric':    metric_tok,
        'scope':     scope_tok,
        'op':        op_tok,
        'threshold': value,
        'value':     val,
        'row':       {},
        'account':   'TOTAL',
        'underlying': None,
        # Explicit classifier `agent_engine._ae_is_aggregate_match` checks
        # — see that function's docstring. Set even though `account`
        # already carries the pre-existing 'TOTAL' sentinel belt-and-
        # suspenders check, per the DSL dataframe-access-gaps design:
        # aggregate classification is a function of (scope suffix OR
        # metric choice), checked explicitly, not inferred only from the
        # account field.
        'rowset_aggregate': True,
    }
    try:
        ctx.observations.append({**entry, 'fired': fired})
    except Exception:
        pass
    return [entry] if fired else []


def _eval_leaf_per_row(metric_fn: Callable, op_fn: Callable, metric_tok: str,
                       scope_tok: str, op_tok: str, value: Any, rows: list,
                       ctx: Context) -> list[dict]:
    """Ordinary per-row branch of `_eval_leaf` — extracted verbatim
    (byte-identical logic) so `_eval_leaf` itself stays under the radon
    complexity gate after the where-filter/aggregate additions."""
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
            'underlying': row.get('underlying'),
        }
        try:
            ctx.observations.append({**entry, 'fired': fired})
        except Exception:
            pass
        if fired:
            matches.append(entry)
    return matches


def _eval_leaf(leaf: dict, ctx: Context) -> list[dict]:
    if "log" in leaf:
        return _eval_log(leaf, ctx)

    metric_raw = leaf.get('metric')
    if _is_given_ref_marker(metric_raw):
        return _eval_given_metric_leaf(leaf, ctx, metric_raw)

    try:
        metric_tok = leaf['metric']
        scope_tok  = leaf['scope']
        op_tok     = leaf['op']
        value      = leaf.get('value')
    except KeyError as e:
        logger.warning(f"Condition leaf missing key {e}: {leaf!r}")
        return []

    # An ordinary metric_ref leaf whose VALUE (not metric) operand
    # references a bound label — e.g. `pnl@positions.total < gap` — same
    # fail-closed contract as the marker-metric branch above: a missing/
    # unresolved binding makes this leaf a clean no-match, never a crash
    # from comparing a real number against an unresolved marker dict.
    value, value_ok = _resolve_given_operand(value, ctx)
    if not value_ok:
        return []

    metric_fn = _resolve_metric_fn(metric_tok, scope_tok)
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
        raw_rows = scope_fn(ctx) or []
    except Exception as e:
        logger.warning(f"Scope selector '{scope_tok}' failed: {e}")
        return []
    rows = raw_rows

    # Sprint 19 — optional symbol-filtered scope. Applied AFTER scope_fn's
    # own row-selection logic (expiry / NFO / MCX-unhedged filtering),
    # BEFORE the metric evaluates each row — so it composes uniformly with
    # every per-position scope variant without touching their resolvers.
    # See grammar.scope_filter_row_predicate's docstring for the full
    # design rationale (why this is a leaf key, not a parameterized scope
    # token).
    scope_filter = leaf.get('scope_filter')
    if scope_filter:
        predicate = grammar.scope_filter_row_predicate(scope_tok, scope_filter)
        if predicate is None:
            logger.warning(
                f"Condition leaf scope_filter {scope_filter!r} references a "
                f"qualifier not exposed on scope '{scope_tok}' — leaf skipped"
            )
            return []
        rows = [r for r in rows if predicate(r)]

    # DSL dataframe-access gaps, item 2 — generic where-filter. Applied
    # AFTER scope_filter (both narrow the SAME row list), BEFORE the
    # metric evaluates each row — see the module section comment above
    # `_apply_where_filter` for why this must be its own filter step and
    # not folded into the per-row loop below.
    where = leaf.get('where')
    if where is not None:
        filtered = _apply_where_filter(rows, where, scope_tok)
        if filtered is None:
            logger.warning(
                f"Condition leaf 'where' filter is malformed, or "
                f"references a column not exposed on scope '{scope_tok}' "
                f"— leaf skipped"
            )
            return []
        rows = filtered

    # DSL dataframe-access gaps, item 3 — row-set aggregates (count/sum/
    # max/avg) collapse to ONE result over `rows` as a whole, regardless
    # of the scope's own suffix; every other metric evaluates per row,
    # unchanged. See the module section comment above
    # `_eval_leaf_rowset_aggregate` for the full contract.
    if getattr(metric_fn, 'rowset_aggregate', False):
        return _eval_leaf_rowset_aggregate(
            metric_fn, op_fn, metric_tok, scope_tok, op_tok, value,
            raw_rows, rows, ctx,
        )
    return _eval_leaf_per_row(
        metric_fn, op_fn, metric_tok, scope_tok, op_tok, value, rows, ctx,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Validation helper (used by admin UI / test harness)
# ═══════════════════════════════════════════════════════════════════════════

def _validate_given_marker_shape(marker: dict, given_names, path: str,
                                  errors: list[str], *, where: str) -> None:
    """Dry-check a `{"$given": "<name>"}` marker found at `where`
    ('metric' or 'value') inside leaf `path`. Always checks the marker
    is well-formed. Only checks the name is actually a label this
    statement declares when `given_names` is supplied — `None` means
    the caller (e.g. the standalone `/validate-condition` endpoint,
    which dry-checks a bare condition tree with no agent context at
    all) has no label set to check against, so this degrades to a pure
    shape check, same leniency this function already applies elsewhere
    when a check needs context it doesn't have."""
    name = marker.get("$given")
    if not isinstance(name, str) or not name:
        errors.append(
            f"{path}.{where}: malformed bound-label reference {marker!r} "
            f"— '$given' must be a non-empty string"
        )
        return
    if given_names is not None and name.lower() not in given_names:
        errors.append(
            f"{path}.{where}: references bound label '{name}', which is "
            f"not assigned anywhere earlier in this statement"
        )


def validate(cond: dict, given_names: "set[str] | None" = None) -> list[str]:
    """
    Dry-check a condition tree against the registry. Returns a list of
    human-readable error strings (empty list ⇒ tree looks well-formed).
    Does not evaluate — only verifies the shape and that every referenced
    token exists in the registry. Resolves `{"$ref": <name>}` against the
    fragment registry and recurses into the resolved body so a typo
    deep inside a fragment still surfaces.

    `given_names` — optional set of lowercased label names this agent's
    OWN statement assigns before `WHEN` (an ordinary assignment item in
    the preamble, no keyword). When supplied, a leaf referencing a
    `{"$given": "<name>"}` marker (see `agent_evaluator._eval_leaf`'s
    module-section comment) in its metric or value position is checked
    against this set — a name absent from it is flagged, same as an
    unknown metric/scope token everywhere else in this function. `None`
    (the default) skips that existence check entirely — used by callers
    that validate a bare condition tree with no agent/label context.
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
            # Sprint D2 — same Agent-row-only resolution as evaluate()
            # above (see its comment + template_registry.reload()).
            from backend.api.algo.template_registry import REGISTRY as _TMPL
            body = _TMPL.get('condition', ref)
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
        if 'log' in c:
            spec = c.get('log') or {}
            tag = spec.get('tag') if isinstance(spec, dict) else None
            if not tag:
                errors.append(f"{path}.log: missing 'tag'")
            elif REGISTRY.log_tag(tag) is None:
                errors.append(f"{path}.log: unknown log tag '{tag}'")
            if str(spec.get('min_level', 'INFO')).upper() not in _LEVEL_NO:
                errors.append(f"{path}.log: unknown min_level '{spec.get('min_level')}'")
            return
        if _is_given_ref_marker(c.get('metric')):
            # Marker-metric leaf — references a bound label, not a
            # registered scope/metric token. `scope` is unused by
            # evaluation (see `_eval_given_metric_leaf`) and is NOT
            # required here. `op` still must be a real registered
            # operator. `value` may itself be a marker (two bound
            # labels compared against each other) — checked the same
            # way as the ordinary leaf path below.
            _validate_given_marker_shape(c['metric'], given_names, path, errors, where='metric')
            if 'op' not in c:
                errors.append(f"{path}: leaf missing 'op'")
            elif c.get('op') and REGISTRY.op(c['op']) is None:
                errors.append(f"{path}: unknown operator token '{c['op']}'")
            if _is_given_ref_marker(c.get('value')):
                _validate_given_marker_shape(c['value'], given_names, path, errors, where='value')
            return
        for k in ('metric', 'scope', 'op'):
            if k not in c:
                errors.append(f"{path}: leaf missing '{k}'")
        _resolved_metric_fn = None
        if c.get('metric'):
            _resolved_metric_fn = _resolve_metric_fn(c['metric'], c.get('scope'))
            if _resolved_metric_fn is None:
                errors.append(f"{path}: unknown metric token '{c['metric']}'")
        # DSL dataframe-access gaps, item 3 — a row-set aggregate factory
        # (sum/max/avg) builds its callable with NO scope context (the
        # registry's call-token cache has no scope to check against), so
        # an unknown/wrong-type `column=` argument otherwise sails
        # through save-time validation and only ever surfaces as a
        # runtime warning, every tick, on an MCP/API-authored agent.
        # Checked the SAME way item 2's where-filter columns are: against
        # `exposed_columns_for_scope(scope)`, requiring `type == 'number'`
        # (sum/max/avg only ever accept a numeric column — see
        # `metric_sum.rowset_numeric_values`).
        if (_resolved_metric_fn is not None
                and getattr(_resolved_metric_fn, 'rowset_aggregate', False)
                and hasattr(_resolved_metric_fn, 'column')
                and c.get('scope')):
            _agg_col = _resolved_metric_fn.column
            _agg_entry = grammar.exposed_columns_for_scope(c['scope']).get(_agg_col)
            if not _agg_entry or _agg_entry.get('type') != 'number':
                errors.append(
                    f"{path}: metric '{c['metric']}' references column "
                    f"'{_agg_col}', which is not a number-typed exposed "
                    f"column on scope '{c['scope']}'"
                )
        if c.get('scope') and REGISTRY.scope(c['scope']) is None:
            errors.append(f"{path}: unknown scope token '{c['scope']}'")
        if c.get('op') and REGISTRY.op(c['op']) is None:
            errors.append(f"{path}: unknown operator token '{c['op']}'")
        if _is_given_ref_marker(c.get('value')):
            _validate_given_marker_shape(c['value'], given_names, path, errors, where='value')
        # Sprint 21 — quote.* is instrument-rooted (no account dimension),
        # so a leaf with no (or wildcard) scope_filter.symbol has nothing
        # to disambiguate WHICH tracked symbol it means — ctx.quote_rows
        # can hold several different agents' tracked symbols at once.
        # Reject at validate() time rather than silently OR-evaluating
        # against every tracked row. Mirror of Sprint 19's existing
        # "qualifier not valid for this scope" guard below, just the
        # opposite direction (a REQUIRED qualifier missing, instead of an
        # invalid one supplied).
        if isinstance(c.get('scope'), str) and c['scope'].startswith('quote.'):
            _qsf = c.get('scope_filter')
            _qsym = _qsf.get('symbol') if isinstance(_qsf, dict) else None
            if not _qsym or _qsym == '*':
                errors.append(
                    f"{path}: scope '{c['scope']}' requires a non-wildcard "
                    f"scope_filter.symbol (quote.* is instrument-rooted)"
                )

        # Sprint G — symbols.* unions four materially different row
        # populations (watchlist/pinned/gainers/losers) behind one scope
        # token; OR-evaluating a leaf across all four unfiltered would be
        # meaningless. Mirror of the quote.* guard directly above, just a
        # different required qualifier (`kind` instead of `symbol`).
        if isinstance(c.get('scope'), str) and c['scope'].startswith('symbols.'):
            _ksf = c.get('scope_filter')
            _kind = _ksf.get('kind') if isinstance(_ksf, dict) else None
            if not _kind or _kind == '*':
                errors.append(
                    f"{path}: scope '{c['scope']}' requires a non-wildcard "
                    f"scope_filter.kind (symbols.* unions watchlist/pinned/"
                    f"gainers/losers — must disambiguate which)"
                )

        # Sprint 19 — optional scope_filter (e.g. {"symbol": "..."})
        # narrows a per-position scope to one specific row dimension. Only
        # dry-checked when the base scope itself resolved above — a typo'd
        # scope token already produced its own error; piling a second,
        # derivative error on top of it would be noise.
        scope_filter = c.get('scope_filter')
        if scope_filter is not None:
            if not isinstance(scope_filter, dict) or not scope_filter:
                errors.append(f"{path}.scope_filter: must be a non-empty dict")
            elif c.get('scope') and REGISTRY.scope(c['scope']) is not None:
                if grammar.scope_filter_row_predicate(c['scope'], scope_filter) is None:
                    errors.append(
                        f"{path}.scope_filter: qualifier(s) "
                        f"{sorted(scope_filter.keys())} not valid for scope "
                        f"'{c['scope']}'"
                    )

        # DSL dataframe-access gaps, item 2 — dry-check an optional
        # `where` sub-tree the SAME way as `scope_filter` above: only
        # once the base scope itself resolved (a typo'd scope already
        # produced its own error above), reusing the EXACT same
        # structural+schema walker the runtime `_apply_where_filter`
        # uses (`_where_tree_well_formed`) — the two can never disagree
        # about what counts as well-formed.
        where = c.get('where')
        if where is not None:
            if c.get('scope') and REGISTRY.scope(c['scope']) is not None:
                schema = grammar.exposed_columns_for_scope(c['scope'])
                if not schema or not _where_tree_well_formed(where, schema):
                    errors.append(
                        f"{path}.where: malformed, or references a column "
                        f"not exposed on scope '{c['scope']}'"
                    )

    walk(cond)
    return errors


# `collect_refs` / `fragments_reaching` / `referencing_agents` (the
# Fragment-impact-preview helpers backing the old `/api/admin/fragments/
# {id}/references` endpoint) were deleted in Sprint D2 (Agent/Bracket/
# Template unification phase) along with that route — grep confirmed
# they had no other caller. If a future sprint wants an equivalent
# "who $ref-s this Template" preview for the unified `/api/agents/`
# endpoints, reintroduce the walk fresh against `Agent` rows rather than
# resurrecting these.
