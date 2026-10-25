"""
Agent Engine — evaluates all active agents using Conditions → Alerts → Actions pipeline.

Called from background.py every refresh cycle with market data context.
Each agent's condition tree is evaluated. If triggered, alerts are dispatched
through configured channels and optional actions are executed.

The engine handles cooldown, state transitions, and WebSocket broadcasts.
"""

import asyncio
import html
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from backend.api.algo.events import dispatch, log_event, EvalResult
from backend.api.algo.actions import execute, _is_row_ref_marker
from backend.api.algo.agent_evaluator import (
    Context as V2Context, evaluate as v2_evaluate, windowed_rate,
    _resolve_metric_fn,
)
from backend.api.algo.expr_eval import eval_expr, ExprError
from backend.api.algo.grammar_registry import REGISTRY
from backend.api.database import async_session
from backend.api.models import Agent
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config as app_config

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Per-key re-alert latch (fixes #5, #7, #8, #9 + deploy-survival)
# ═══════════════════════════════════════════════════════════════════════════
#
# REPLACES the old per-AGENT `_V2_LAST_ALERT` latch, which mixed units
# across leaves (a ₹ day_val leaf's magnitude always dominated a %/min
# rate leaf's — fix #8), used the wrong (global, not per-agent) cooldown
# for rate agents (fix #8), had no re-arm hysteresis so a value
# oscillating around the threshold spammed repeat fires (fix #9), never
# escalated for a monotonically-worsening breach (fix #9), and unlatched
# on ANY no-match tick even when that tick's "no match" was really a
# fetch timeout/failure/empty-frame masking the true state, not a real
# recovery (fix #5).
#
# Keyed by (agent_slug, metric, scope, account) — one independent latch
# per LEAF per ACCOUNT, so a ₹ leaf and a %/min leaf on the same agent
# never influence each other's re-fire timing, and one account's
# recovery never re-arms a DIFFERENT account's still-breaching leaf.
_V2_LATCH: dict[tuple, dict] = {}   # key -> {'ts': datetime, 'val': float}
_V2_LAST_RESET_DATE = None

# Cold-start hydration guard (deploy-survival — see "Also fold in" in the
# alerts audit plan). `_V2_LATCH` is in-memory and this app redeploys on
# every push to main; without hydration, every deploy would re-fire every
# currently-latched standing breach the moment its DB-level cooldown
# status naturally elapses post-restart. Set True on the FIRST attempt
# (success or failure) so a DB hiccup never retries every tick.
_V2_LATCH_HYDRATED = False


def _maybe_reset_v2_state(today, *, live: bool = True):
    """Wipe v2 latch state once per new trading day.

    `live=False` (sim/replay cycles) makes this a complete no-op — a
    sim/replay run passes its OWN simulated/historical `now.date()`
    (e.g. a historical backtest date, or a scenario date far from
    today), which would otherwise compare unequal to the real
    `_V2_LAST_RESET_DATE` set by `_v2_hydrate_latch()` and wipe the
    just-hydrated LIVE latch on the very next sim/replay tick. Only a
    real live cycle may mutate `_V2_LAST_RESET_DATE` / trigger THIS
    function's wholesale `_V2_LATCH.clear()`.

    This function is only ONE of two places `_V2_LATCH` can be mutated
    — the other is the per-key recovery/escalation-gate store selection
    in `_ae_cycle_eval_and_buffer` / `_cycle_maybe_buffer_fire`, which
    has its OWN separate `sim_mode or replay_mode` guard (store
    selection, not this function's `live` flag) routing sim/replay
    reads+writes to the isolated `alert_state['_sim_latch']` instead.
    Both guards must hold for "a sim/replay tick never touches the live
    latch" to actually be true system-wide."""
    global _V2_LAST_RESET_DATE
    if not live:
        return
    if _V2_LAST_RESET_DATE != today:
        _V2_LAST_RESET_DATE = today
        _V2_LATCH.clear()


def _latch_key(agent_slug: str, m: dict) -> tuple:
    """Per-leaf, per-account (or per-underlying) latch key. `m` is a
    match/observation dict from `agent_evaluator` — has 'metric',
    'scope', 'account', 'underlying'.

    Fix A (underlying-divergence feature) — `positions.any_underlying`
    matches carry no 'account' at all (they roll up across every account
    holding that underlying), so without the `m.get('underlying')`
    fallback, every underlying's breach would collapse onto the SAME
    'TOTAL' latch key — one underlying's re-alert timing would silently
    gate every other underlying's, and a genuine recovery/escalation on
    one underlying would corrupt another's state. Account still wins
    when both are present (shouldn't happen in practice — the two scope
    families are mutually exclusive on any one match — but preserves the
    pre-existing account-keyed behaviour unconditionally)."""
    return (agent_slug, m.get('metric'), m.get('scope'),
            m.get('account') or m.get('underlying') or 'TOTAL')


# Ops for which "worse" has a well-defined direction. -1 ⇒ smaller is
# worse (loss thresholds); +1 ⇒ larger is worse. Ops absent from this map
# (==, !=, in, not_in, between) have no ordered "worse" direction — they
# get a plain cooldown-gated re-latch with no hysteresis/escalation math,
# same as a boolean condition (e.g. is_itm/is_future).
_ORDERED_OPS_WORSE_DIR = {'<': -1, '<=': -1, '>': 1, '>=': 1}

# Re-arm band — how far PAST the threshold a value must recover before
# the latch clears (fix #9 hysteresis). 0.2 = must recover to within 80%
# of the threshold's magnitude, not merely tick back over the line.
_V2_REARM_BAND = 0.2


def _v2_recovered_past_band(obs: dict) -> bool:
    """True when an OBSERVED non-breaching row has recovered past the
    hysteresis re-arm band (fix #9), not merely back over the raw
    threshold line. Only called for rows the evaluator actually saw this
    tick (`fired=False` in `Context.observations`) — a row that's simply
    absent from observations (fetch failure/timeout/empty frame) is
    handled by the caller and never reaches this function (fix #5)."""
    op = obs.get('op')
    worse_dir = _ORDERED_OPS_WORSE_DIR.get(op)
    if worse_dir is None:
        return True  # non-ordered op — plain re-arm, no hysteresis math
    try:
        thr = float(obs.get('threshold'))
        val = float(obs.get('value'))
    except (TypeError, ValueError):
        return True
    if thr == 0:
        return True  # no magnitude to band against
    rearm_at = thr - worse_dir * _V2_REARM_BAND * abs(thr)
    return (val - rearm_at) * worse_dir < 0


def _v2_leaf_should_fire(agent_slug: str, m: dict, now, cooldown_min: float,
                         store: dict) -> bool:
    """Per-(metric, scope, account) re-alert gate (fixes #8 + #9).

    - First breach for this key (no latch) → always fires.
    - Otherwise: never re-fires inside `cooldown_min` of the last fire
      for THIS key — the caller passes the AGENT's own
      `cooldown_minutes` (fix #8; the old code read the global
      `cfg['cooldown_min']` default for rate agents regardless of the
      agent's configured value).
    - Past cooldown, an ordered-op leaf only re-fires once the value has
      moved at least one more |threshold| unit further in the "worse"
      direction since the last alert (escalation — fix #9: a
      monotonically-worsening breach must eventually re-alert, not stay
      silent forever). Zero-threshold leaves (e.g. `cash < 0`) have no
      escalation step — cooldown elapsed alone re-arms them, since
      "worse by another $0" is meaningless.
    - Non-ordered ops (==, in, between, …) simply re-fire once cooldown
      has elapsed.

    `store` is the latch dict to read (see `_v2_reconcile_latch` for why
    this is parametrised rather than reading the module-level `_V2_LATCH`
    directly — sim runs use an isolated per-run store).
    """
    key = _latch_key(agent_slug, m)
    prev = store.get(key)
    if prev is None:
        return True
    if (now - prev['ts']) < timedelta(minutes=cooldown_min):
        return False
    worse_dir = _ORDERED_OPS_WORSE_DIR.get(m.get('op'))
    if worse_dir is None:
        return True
    try:
        thr = float(m.get('threshold'))
        val = float(m.get('value'))
        prev_val = float(prev.get('val'))
    except (TypeError, ValueError):
        return True
    step = abs(thr)
    if step == 0:
        return True
    moved = (val - prev_val) * worse_dir
    return moved >= step


def _v2_apply_recovery(agent, observations: list, *, store: dict | None = None) -> None:
    """Clear per-key latches for rows that were OBSERVED to have
    recovered past the hysteresis re-arm band this tick (fix #9). A key
    that's simply absent from `observations` (fetch timeout/failure/
    masked-empty frame) is left untouched — never treated as recovered
    (fix #5). MUST run on every tick the agent is evaluated, regardless
    of whether it produced any matches this tick — a fully-recovered
    agent (matches == []) is exactly the case that needs its latches
    cleared, so this is called unconditionally, not gated on `matches`
    being non-empty (unlike `_v2_apply_escalation_gate` below).

    `store` defaults to the module-level `_V2_LATCH`; sim runs pass an
    isolated per-run dict instead (see `_v2_apply_escalation_gate`)."""
    if store is None:
        store = _V2_LATCH
    agent_slug = agent.slug
    for obs in observations:
        if obs.get('fired'):
            continue
        key = _latch_key(agent_slug, obs)
        if key in store and _v2_recovered_past_band(obs):
            store.pop(key, None)


def _v2_apply_escalation_gate(agent, matches: list, now,
                              cooldown_min: float, *, store: dict | None = None) -> list:
    """Filter `matches` down to the subset whose per-key latch gate
    (`_v2_leaf_should_fire`) says this is a genuinely new/worse breach
    (fixes #8/#9). Keys that pass have their latch updated to
    (now, value). Call `_v2_apply_recovery` FIRST (same tick) so a key
    that just recovered doesn't wrongly inherit stale escalation state.

    `store` defaults to the module-level `_V2_LATCH` (the live-engine
    latch). Callers running a simulator tick MUST pass an isolated
    per-run dict instead — writing sim fires into the live latch would
    corrupt real re-alert timing for the rest of the trading session.

    Returns the filtered matches — empty ⇒ nothing about this tick's
    breach is new enough to fire, even though `matches` (the raw
    breaching rows) may be non-empty.
    """
    if store is None:
        store = _V2_LATCH
    agent_slug = agent.slug
    effective = []
    for m in matches:
        if _v2_leaf_should_fire(agent_slug, m, now, cooldown_min, store):
            effective.append(m)
            store[_latch_key(agent_slug, m)] = {'ts': now, 'val': m.get('value')}
    return effective


def _hydrate_latch_from_rows(rows, today) -> None:
    """Pure helper — populate `_V2_LATCH` from (slug, detail_json,
    timestamp) tuples. Rows must be pre-sorted ascending by timestamp so
    a later row naturally overwrites an earlier one for the same key.
    Rows whose IST calendar date isn't `today` are skipped — the daily
    reset (`_maybe_reset_v2_state`) wipes the latch at day-start, so a
    stale prior-day entry must never survive into hydration. Split out
    from `_v2_hydrate_latch` (the DB-querying wrapper) so this can be
    unit-tested with plain tuples — no DB session needed.

    Rows carrying `detail['replay_mode'] = True` are also skipped — replay
    ticks are logged with `sim_mode=False` (deliberately distinct from
    real sim runs), so the SQL-level `AgentEvent.sim_mode.is_(False)`
    hydration query filter alone does not exclude them; this is the
    Python-side backstop that does (see `_v2_build_evalresult` /
    `_ae_dispatch_suppressed_entry`, which stamp the marker at write time).

    Hydrates from `detail['latched_matches']`, NOT `detail['matches']`
    (2026-10 fix). `matches` is the full raw breaching list for a tick —
    it can include leaves that were still gated by
    `_v2_apply_escalation_gate` (cooldown not elapsed / not escalated
    enough) on a tick that nonetheless dispatched because some OTHER
    leaf on the same agent passed the gate. Hydrating from the raw list
    would latch a gated leaf's worse, not-yet-actioned value/timestamp
    instead of its true last-fired one, delaying or masking real
    re-alerts after a restart. `latched_matches` carries only the
    subset that actually updated `_V2_LATCH` on the live path that
    tick, so replaying it in ascending timestamp order reconstructs the
    exact same latch state the live engine would have. Falls back to
    `matches` for rows written before this fix (no `latched_matches`
    key present)."""
    import json as _json
    from zoneinfo import ZoneInfo
    _ist = ZoneInfo("Asia/Kolkata")
    for slug, detail_raw, ts in rows:
        if ts is None or not detail_raw:
            continue
        if ts.astimezone(_ist).date() != today:
            continue
        try:
            detail = _json.loads(detail_raw) if isinstance(detail_raw, str) else detail_raw
        except Exception:
            continue
        if detail.get('replay_mode'):
            continue
        latched = detail.get('latched_matches')
        if latched is None:
            latched = detail.get('matches') or []
        for m in latched:
            if m.get('value') is None:
                continue
            _V2_LATCH[_latch_key(slug, m)] = {'ts': ts, 'val': m.get('value')}


async def _v2_hydrate_latch() -> None:
    """Cold-start hydration of `_V2_LATCH` from today's `agent_events`
    rows — the deploy-survival fix ("Also fold in", alerts audit plan).

    Persistence choice: reuses the EXISTING `agent_events` table instead
    of adding a new DB column. Every fire — survivor or suppressed
    (fix #7 makes suppressed fires also record a latch) — already writes
    its full match list (metric/scope/account/value) into
    `agent_events.detail` via `events.dispatch()`/`log_event()`, so no
    schema change or migration is required. `_cycle_in_cooldown`'s DB
    `status`/`last_triggered_at` columns were considered as the reuse
    target instead, but they are agent-level-only (one timestamp per
    agent) and cannot represent this per-(metric, scope, account)
    hysteresis state, so `agent_events` — which already carries
    everything the latch needs, per-row — is the lower-duplication
    choice. Runs once per process; guarded so a DB hiccup never retries
    every tick (worst case: this process starts cold, same as before
    this fix existed)."""
    global _V2_LATCH_HYDRATED
    if _V2_LATCH_HYDRATED:
        return
    _V2_LATCH_HYDRATED = True
    global _V2_LAST_RESET_DATE
    try:
        from backend.api.models import AgentEvent
        from backend.shared.helpers.date_time_utils import timestamp_indian
        _now_ist = timestamp_indian()
        today = _now_ist.date()
        # 2026-09-28 audit fix — this query had no date bound at all, so
        # every process restart did an unbounded scan of the ENTIRE
        # agent_events history (filtered only by event_type/sim_mode),
        # just to have _hydrate_latch_from_rows discard everything but
        # today's rows in Python. Behaviorally correct (that Python-side
        # filter already prevents stale prior-day entries from surviving
        # into the latch) but wasteful and unbounded-growing as the table
        # accumulates months of history. Adding the SQL-level >= bound
        # lets Postgres seek the indexed timestamp column directly
        # instead of fetching and discarding irrelevant rows; the Python
        # filter stays as the authoritative correctness backstop (it also
        # excludes replay_mode rows, which this SQL bound can't).
        _ist_midnight = _now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        async with async_session() as session:
            result = await session.execute(
                select(Agent.slug, AgentEvent.detail, AgentEvent.timestamp)
                .join(AgentEvent, AgentEvent.agent_id == Agent.id)
                .where(AgentEvent.event_type.in_(('triggered', 'triggered_suppressed')))
                # sim/replay fires must never hydrate the LIVE latch — a
                # sim run that happened to fire the same (agent, metric,
                # scope, account) key would otherwise poison the real
                # latch on the next live restart.
                .where(AgentEvent.sim_mode.is_(False))
                .where(AgentEvent.timestamp >= _ist_midnight)
                .order_by(AgentEvent.timestamp.asc())
            )
            rows = result.all()
        _hydrate_latch_from_rows(rows, today=today)
        # Set the reset-date tracker HERE, at the point hydration
        # completes — not left at its None default. _maybe_reset_v2_state
        # runs unconditionally on every live cycle's first agent
        # (_V2_LAST_RESET_DATE starts None), and without this line it
        # would see today != None on the very first post-hydration tick
        # and immediately wipe the _V2_LATCH we just restored, silently
        # undoing the entire deploy-survival fix. Stays correct across a
        # midnight rollover during a long-running process too — the
        # normal _maybe_reset_v2_state day-boundary check still fires
        # tomorrow off this same tracker.
        _V2_LAST_RESET_DATE = today
        logger.info(f"Agent engine: v2 latch hydrated from agent_events — {len(_V2_LATCH)} keys")
    except Exception as e:
        logger.warning(f"Agent engine: v2 latch hydration failed (starting cold): {e}")


# Max samples per (section, scope) bucket in alert_state['pnl_history'].
# At a 5-minute background refresh that's 200 × 5 = 1000 minutes of
# history per bucket — well past any rate window we ship. At the 2-second
# simulator tick it's ~400 seconds (6+ minutes), comfortably above the
# default 10-minute rate window for fabricated sim runs.
_PNL_HISTORY_CAP = 200


def _v2_positions_pct_raw(row: dict):
    """Return the percentage figure to record in pnl_history for a
    POSITIONS row (fix #10 wiring): prefer the margin-based
    `day_change_pct_margin` column (see `background._apply_positions_margin_pct`)
    over the notional-based `day_change_percentage` when the row carries
    it, so the `pnl_rate_pct` rate metric — which reads this same
    history bucket — is fixed by the same margin-denominator change as
    the static `day_pct` metric. NaN (margin unavailable for that
    account) resolves to None (skip), never a silent fallback to the
    broken notional figure. Rows without the new column at all (e.g. an
    older in-memory alert_state predating this fix, or `df_margins`
    unavailable this tick) fall back to `day_change_percentage`."""
    import math
    if 'day_change_pct_margin' in row:
        v = row.get('day_change_pct_margin')
        if v is None:
            return None
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(fv) else fv
    return row.get('day_change_percentage')


def _update_pnl_history(alert_state: dict, now, sum_positions, sum_holdings,
                        market_state: dict | None = None) -> None:
    """
    Append the current per-(section, scope) P&L snapshot to
    `alert_state['pnl_history']` so the rate evaluator has something
    to compute ΔP&L/min against.

    The retired check-and-alert engine used to maintain this dict; when
    it was replaced by the v2 grammar engine, the writer was lost.
    The reader (V2Context._rate_window_samples) stayed in place but
    found an empty list and returned None — every rate-metric agent
    silently never fired.

    Key shape matches V2Context's lookup: `(section, scope)` where
    section is 'holdings' / 'positions' and scope is the per-row
    account id (incl. 'TOTAL' for the aggregate row).

    Trimming: each bucket caps at _PNL_HISTORY_CAP entries (oldest
    dropped). Session reset: when `alert_state['session_date']` no
    longer matches `now.date()`, the whole pnl_history is wiped so
    yesterday's tail doesn't leak into today's rate window.

    Fix #6b — `market_state` (the `nse_open`/`mcx_open` flags from
    `_build_context`, computed by the caller BEFORE calling this
    function) does two things:

      (a) Segment-anchored baseline: `session_start` tracks the moment a
          market segment actually opened today (09:15 NSE / 09:00 MCX),
          not "whenever the background poller first ran" (~08:00 IST —
          the old anchor made the 15-min opening-gap gate expire around
          08:19, a full 40+ minutes before NSE even opens, providing
          ZERO protection against the volatility it exists to suppress).
          Anchored to the LATEST-opening segment that's currently open
          (conservative — keeps the gate live until every relevant
          segment has cleared its own open, not just the first one).
      (b) Don't record P&L history for a segment while it's still
          closed — samples are only appended once at least one market
          segment is actually open, so the very first rate-window
          samples of the day never span the closed→open discontinuity
          (which would itself look like a huge, fake "rate of change").

    `market_state=None` (back-compat / simulator convenience) falls back
    to the old unconditional-append behaviour — sim ticks aren't tied to
    wall-clock market hours, so "is a segment open" isn't a meaningful
    gate there; the simulator's own scenario clock decides what's live.
    """
    today = now.date() if hasattr(now, 'date') else None
    last_date = alert_state.get('session_date')
    if today and last_date != today:
        alert_state['pnl_history'] = {}
        alert_state['session_date'] = today
        alert_state.pop('session_start', None)
        alert_state.pop('_segment_open_seen', None)

    any_open = False
    if market_state is not None:
        any_open = bool(market_state.get('nse_open') or market_state.get('mcx_open'))
        seen = alert_state.setdefault('_segment_open_seen', {})
        for seg_key, flag_key in (('equity', 'nse_open'), ('commodity', 'mcx_open')):
            if market_state.get(flag_key) and seg_key not in seen:
                seen[seg_key] = now
        if seen:
            alert_state['session_start'] = max(seen.values())

    if 'session_start' not in alert_state:
        alert_state['session_start'] = now   # cold start / no segment open yet

    if market_state is not None and not any_open:
        return  # fix #6b — nothing open yet; don't record the closed-market noise

    hist_map = alert_state.setdefault('pnl_history', {})

    def _append(section: str, df):
        if df is None or getattr(df, 'empty', True):
            return
        if 'account' not in getattr(df, 'columns', []):
            return
        # Partial-outage guard (2026-09 alerts audit item 5) — when some
        # (not all) accounts failed to fetch this tick, `df`'s TOTAL row
        # silently omits the failed account(s)' contribution. Recording
        # that distorted TOTAL into pnl_history would poison a FUTURE
        # tick's rate-of-change window (once accounts recover, the ROC
        # calc would diff against this tick's understated total as if it
        # were real). Skip ONLY the TOTAL sample this tick — per-account
        # buckets for accounts that DID fetch successfully are unaffected
        # and still get their sample (their own history isn't corrupted
        # by a sibling account's outage).
        skip_total = bool((getattr(df, 'attrs', {}) or {}).get('partial_outage'))
        for _, row in df.iterrows():
            acct = str(row.get('account', '') or '')
            if not acct:
                continue
            if skip_total and acct == 'TOTAL':
                continue
            # Positions track day P&L velocity; holdings track total unrealized.
            if section == 'positions':
                try:
                    pnl = float(row.get('day_change_val', 0) or 0)
                except (TypeError, ValueError):
                    continue
                pct_raw = _v2_positions_pct_raw(row)
            else:
                try:
                    pnl = float(row.get('pnl', 0) or 0)
                except (TypeError, ValueError):
                    continue
                # pnl_pct is optional — holdings always have it. Fall back to
                # None when not present so the field_idx=2 path returns None.
                pct_raw = row.get('pnl_percentage')
            try:
                pct = float(pct_raw) if pct_raw is not None else None
            except (TypeError, ValueError):
                pct = None
            key = (section, acct)
            bucket = hist_map.setdefault(key, [])
            bucket.append((now, pnl, pct))
            if len(bucket) > _PNL_HISTORY_CAP:
                # Drop oldest in chunks of 1 so we stay at cap.
                del bucket[:len(bucket) - _PNL_HISTORY_CAP]

    _append('positions', sum_positions)
    _append('holdings',  sum_holdings)


# ═══════════════════════════════════════════════════════════════════════════
#  Underlying-scoped theoretical-vs-market loss divergence (per-underlying
#  rate-of-change gate)
# ═══════════════════════════════════════════════════════════════════════════
#
# See `grammar.py`'s module-level comment above `_scope_positions_any_
# underlying` for the full design rationale (baseline-reprice formula, why
# σ is calibrated ONCE and frozen rather than every tick). This section is
# the WRITE side: both sibling functions below are called once per tick
# from the SAME call site that already calls `_update_pnl_history` (see
# `run_cycle`), immediately after it, so they can read the `session_date`
# that function just set/reconciled for this tick rather than re-deriving
# the day-rollover boundary themselves.

_UNDERLYING_THEO_BASELINE_KEY = 'underlying_theo_baseline'


def _theo_baseline_reconciles(reprice: float, ltp_base: float) -> bool:
    """σ-calibration sanity check — after calibrating `sigma_base` from
    `ltp_base`, reprice at that σ and compare back to `ltp_base` within a
    tolerance (1% of ltp_base, or ₹0.50, whichever is larger). A
    reconciled reprice means the bisection genuinely bracketed a
    solution; a failed one (bisection hit `DEFAULT_IV`/an edge fallback —
    typically a near-zero-time-value / expiry-day contract) would
    otherwise bake a fake divergence into every later tick for that leg
    until it's closed."""
    tol = max(0.01 * abs(ltp_base), 0.50)
    return abs(reprice - ltp_base) <= tol


def _capture_opt_leg_baseline(parsed: dict, S_base: float, ltp_base: float,
                              qty: int, is_mcx: bool, now, dcv_base: float = 0.0) -> "dict | None":
    """Calibrate σ for ONE option leg and return its baseline entry, or
    None when the time-to-expiry is degenerate or the calibration
    doesn't reconcile (advisor point 2 — the calibration-failure guard).
    Extracted from `_capture_leg_theo_baseline` to keep it under the
    radon complexity gate.

    `dcv_base` is the leg's own `day_change_val` at the same moment the
    rest of the baseline is captured — see `_capture_leg_theo_baseline`'s
    docstring for why this is needed (shared reference point for
    `grammar_tokens.metric_underlying_market_loss._metric_underlying_market_loss`)."""
    from backend.api.algo.derivatives import (
        implied_vol, implied_vol_76, black_scholes, black_76,
        days_to_expiry, DEFAULT_RISK_FREE,
    )
    strike = parsed.get('strike')
    opt_type = parsed.get('opt_type')
    if strike is None or opt_type not in ('CE', 'PE'):
        return None
    close_time = (23, 30) if is_mcx else (15, 30)
    try:
        T_base = days_to_expiry(parsed['expiry'], ref=now, close_time=close_time) / 365.0
    except Exception:
        return None
    if T_base <= 0:
        return None  # degenerate at/past expiry — IV undefined, retry next tick
    try:
        if is_mcx:
            sigma_base = implied_vol_76(ltp_base, S_base, strike, T_base, DEFAULT_RISK_FREE, opt_type)
            reprice = black_76(S_base, strike, T_base, DEFAULT_RISK_FREE, sigma_base, opt_type)
        else:
            sigma_base = implied_vol(ltp_base, S_base, strike, T_base, DEFAULT_RISK_FREE, opt_type)
            reprice = black_scholes(S_base, strike, T_base, DEFAULT_RISK_FREE, sigma_base, opt_type)
    except Exception:
        return None
    if not _theo_baseline_reconciles(reprice, ltp_base):
        return None  # calibration failure guard (advisor point 2) — retry next tick
    return {'kind': 'opt', 'ltp_base': ltp_base, 'S_base': S_base,
            'sigma_base': sigma_base, 'K': float(strike), 'opt_type': opt_type,
            'expiry': parsed['expiry'], 'is_mcx': is_mcx, 'qty': qty,
            'dcv_base': dcv_base}


def _capture_leg_theo_baseline(row: dict, now, spot_prices: dict):
    """Calibrate ONE leg's baseline-reprice entry for `alert_state[
    'underlying_theo_baseline']`, or return None when it can't be
    captured yet THIS tick (missing spot, non-positive LTP, degenerate
    time-to-expiry, or a σ-calibration that doesn't reconcile) — the
    caller simply retries next tick, never baking in a bad baseline.
    `row` is one `ctx.position_rows` entry for an already-confirmed
    non-flat F&O leg.

    Also captures `dcv_base` — the leg's own `day_change_val` at this
    SAME moment — so `grammar_tokens.metric_underlying_market_loss._metric_underlying_market_loss` can later
    measure "change since baseline capture" instead of raw day_change_val
    (which is measured from prev_close, a DIFFERENT reference point than
    this baseline's own ltp_base/S_base/sigma_base — see the module
    comment above `grammar_tokens.scope_positions_any_underlying.
    _scope_positions_any_underlying` for why the two sides of the
    divergence gate must share one reference point)."""
    from backend.api.algo.derivatives import parse_tradingsymbol, is_mcx_underlying
    sym = row.get('tradingsymbol') or ''
    try:
        parsed = parse_tradingsymbol(sym)
    except Exception:
        parsed = None
    if not parsed or not parsed.get('expiry'):
        return None
    underlying = parsed.get('root')
    if not underlying:
        return None
    try:
        S_base = float(spot_prices.get(underlying))
    except (TypeError, ValueError):
        return None
    if S_base <= 0:
        return None
    try:
        ltp_base = float(row.get('last_price') or 0)
        qty = int(row.get('quantity') or 0)
    except (TypeError, ValueError):
        return None
    if ltp_base <= 0 or qty == 0:
        return None
    try:
        dcv_base = float(row.get('day_change_val', 0) or 0)
    except (TypeError, ValueError):
        dcv_base = 0.0
    is_mcx = is_mcx_underlying(underlying)
    kind = parsed.get('kind')
    if kind == 'fut':
        return {'kind': 'fut', 'ltp_base': ltp_base, 'S_base': S_base,
                'is_mcx': is_mcx, 'qty': qty, 'dcv_base': dcv_base}
    if kind != 'opt':
        return None
    return _capture_opt_leg_baseline(parsed, S_base, ltp_base, qty, is_mcx, now, dcv_base)


def _ae_sum_positions_partial_outage(sum_positions) -> bool:
    """True when `background._fetch_positions_direct` flagged this tick's
    `sum_positions` frame as missing one or more (not all) accounts'
    contribution — same `attrs['partial_outage']` signal
    `grammar_tokens.scope_positions_total._scope_positions_total`
    already guards on. Shared by both
    underlying-divergence writer functions below so neither has to poke
    at `.attrs` independently."""
    if sum_positions is None:
        return False
    return bool((getattr(sum_positions, 'attrs', {}) or {}).get('partial_outage'))


def _update_underlying_theo_baseline(alert_state: dict, now, position_rows, spot_prices: dict,
                                     *, sum_positions=None) -> None:
    """Sibling to `_update_pnl_history` — captures/retires per-leg
    baseline-reprice entries in `alert_state['underlying_theo_baseline']`
    (keyed by tradingsymbol), one entry per currently-held non-flat F&O
    leg. Read-only from `grammar._underlying_theoretical_loss`'s
    perspective; this function is the sole mutator.

    - A leg with no existing baseline entry gets ONE captured the first
      tick it's seen with a usable spot + reconciling σ calibration
      (`_capture_leg_theo_baseline`) — whether that's a position carried
      from market open or one opened mid-day. A leg that can't be
      captured yet is simply retried next tick.
    - A leg that disappears (closed, or quantity goes to 0) has its
      baseline entry dropped on the SAME tick — no stale legs accumulate
      across a session.
    - Reset keys off `alert_state['session_date']` — the SAME day-
      rollover boundary `_update_pnl_history` already derives and sets,
      read here rather than re-derived, since the call site always runs
      this function immediately after that one on the same tick.
    - Outage guard (fix #5 class — "absent ≠ recovered/closed"): when
      `position_rows` is empty (a full fetch failure, or genuinely no
      F&O book at all — Kite keeps an intraday-closed leg's row at
      qty=0, it does NOT simply vanish, so an empty list here means no
      real read happened) OR `sum_positions` carries the partial-outage
      attrs flag (one account's rows silently missing), this function
      does NOTHING — no capture, no drop. Without this guard, a single
      bad poll would wipe every frozen σ baseline, and the next clean
      poll would silently RE-baseline at the CURRENT (already-moved)
      price — collapsing theoretical loss back to ~0 against a fresh
      baseline and defeating the whole feature, exactly the bug class
      fix #5 closed for latch recovery.
    """
    # Day-rollover reset is TIME-based, not data-based — it must still run
    # even on an outage tick (e.g. the very first tick of a new day before
    # any fetch has succeeded yet), so a stale prior-day baseline never
    # survives into today regardless of fetch health.
    session_date = alert_state.get('session_date')
    if session_date and alert_state.get('_underlying_theo_baseline_date') != session_date:
        alert_state[_UNDERLYING_THEO_BASELINE_KEY] = {}
        alert_state['_underlying_theo_baseline_date'] = session_date

    if not position_rows or _ae_sum_positions_partial_outage(sum_positions):
        return  # outage guard — never capture/drop legs off untrustworthy data

    baseline = alert_state.setdefault(_UNDERLYING_THEO_BASELINE_KEY, {})
    spot_prices = spot_prices or {}
    live_symbols = set()
    for row in position_rows:
        sym = row.get('tradingsymbol') or ''
        if not sym:
            continue
        try:
            if int(row.get('quantity') or 0) == 0:
                continue  # flat leg — nothing to baseline
        except (TypeError, ValueError):
            continue
        live_symbols.add(sym)
        if sym in baseline:
            continue  # already baselined this session — σ stays frozen
        entry = _capture_leg_theo_baseline(row, now, spot_prices)
        if entry is not None:
            baseline[sym] = entry

    for sym in list(baseline.keys()):
        if sym not in live_symbols:
            del baseline[sym]


def _update_underlying_pnl_history(alert_state: dict, now, position_rows,
                                   *, sum_positions=None,
                                   market_state: dict | None = None) -> None:
    """Sibling to `_update_pnl_history` — appends a per-underlying P&L
    sample to its OWN namespaced bucket, `('positions_underlying',
    <underlying>)`, so `pnl_rate_abs`/`pnl_rate_pct` can be evaluated
    under the `positions.any_underlying` scope
    (grammar_tokens.metric_pnl_rate_abs/metric_pnl_rate_pct's resolvers
    key off this namespace when a row carries `underlying` and no real
    `account`).

    Deliberately a SEPARATE section name from `'positions'` (the existing
    per-account/TOTAL bucket) — NOT reused — since an account id could in
    principle collide with an underlying string and corrupt both
    buckets' history.

    `day_change_val_sum` is the same per-underlying sum `underlying_
    market_loss` computes. The pct slot (field_idx=2) is recorded as
    `None` — no per-underlying margin denominator exists in this codebase
    today (fix #10's `day_change_pct_margin` is account-level only), so
    `pnl_rate_pct` is deliberately inert for this scope; use
    `pnl_rate_abs` for underlying-scoped rate conditions. This is a
    scoped, documented limitation, not a silent bug.

    Partial-outage guard: skips appending ANY underlying sample on a tick
    where EITHER `position_rows` carries no rows at all (full-outage/
    never-fetched tick) OR `sum_positions` carries the partial-outage
    attrs flag (`_ae_sum_positions_partial_outage` — one account's rows
    silently missing while others' are present) — mirrors `_update_pnl_
    history`'s own TOTAL-bucket skip for the identical reason: a
    per-underlying sum across accounts is exactly as vulnerable to a
    silently-missing account's contribution as the TOTAL row is. Recording
    a sample built from an understated sum would poison a FUTURE tick's
    rate-of-change window once the missing account recovers — the same
    failure mode `_update_pnl_history`'s own partial-outage guard exists
    to prevent for the TOTAL bucket.

    Reuses the SAME `market_state`-gated close-market skip and
    `_PNL_HISTORY_CAP` trimming `_update_pnl_history` already applies —
    not re-derived here independently, since both functions are always
    called back-to-back on the same tick with the same `market_state`.
    """
    if market_state is not None:
        any_open = bool(market_state.get('nse_open') or market_state.get('mcx_open'))
        if not any_open:
            return  # fix #6b parity — don't record the closed-market noise

    if not position_rows or _ae_sum_positions_partial_outage(sum_positions):
        return

    hist_map = alert_state.setdefault('pnl_history', {})
    sums: dict = {}
    for row in position_rows:
        u = row.get('underlying')
        if not u:
            continue
        try:
            v = float(row.get('day_change_val', 0) or 0)
        except (TypeError, ValueError):
            continue
        sums[u] = sums.get(u, 0.0) + v

    for underlying, total in sums.items():
        key = ('positions_underlying', underlying)
        bucket = hist_map.setdefault(key, [])
        bucket.append((now, total, None))
        if len(bucket) > _PNL_HISTORY_CAP:
            del bucket[:len(bucket) - _PNL_HISTORY_CAP]


# ---------------------------------------------------------------------------
# v2 condition-tree helpers
# ---------------------------------------------------------------------------

def is_grammar_tree(cond) -> bool:
    """True when `cond` is a structurally plausible grammar tree."""
    if not isinstance(cond, dict):
        return False
    if '$ref' in cond:
        # Fragment reference — validator will resolve + recurse.
        return True
    if 'all' in cond or 'any' in cond or 'not' in cond:
        return True
    return 'metric' in cond and 'scope' in cond


# ── Phase 23 — per-order exchange-open gate ──────────────────────────
#
# Maps every Kite exchange code → the market_segment it belongs to.
# This is the single source of truth for "is THIS symbol's market
# open right now?" used by both the agent action layer and the
# operator-initiated ticket route.
#
# NSE / BSE / NFO / BFO / CDS / BCD → equity segment (09:15-15:30 IST,
#                                     NSE holidays).
# MCX / NCO                         → commodity segment (09:00-23:30
#                                     IST, MCX holidays).
# Unknown exchanges default to False (safer than wrongly allowing).
#
# `NCO` was missing until 2026-10-20 — every other MCX/NCO call site in
# the app (~25, e.g. `exchange in ("MCX", "NCO")` in orders_place.py,
# chase.py, actions_preflight.py, template_attach.py, both broker
# adapters) already treats NCO identically to MCX. Its absence here
# meant `_segment_for_exchange("NCO")` returned None, so
# `_symbol_exchange_open("NCO", ctx)` always returned False — every
# NCO order/basket leg/GTT-wing attach/chase was unconditionally
# rejected as "market closed", regardless of actual time. Fixed by
# adding the entry; this flips NCO from always-blocked to
# correctly-gated, a real-money-path behaviour change.
_EXCHANGE_TO_SEGMENT = {
    "NSE":  "equity",
    "BSE":  "equity",
    "NFO":  "equity",
    "BFO":  "equity",
    "CDS":  "equity",
    "BCD":  "equity",
    "MCX":  "commodity",
    "NCO":  "commodity",
}


def _segment_for_exchange(exchange: str) -> str | None:
    """Return the market-segment name an exchange belongs to, or None
    when the code is unknown / unset."""
    if not exchange:
        return None
    return _EXCHANGE_TO_SEGMENT.get(str(exchange).upper().strip())


def _symbol_exchange_open(exchange: str, ctx: dict) -> bool:
    """Phase 23 — return True if the exchange's market segment is
    currently open. `ctx` is the dict emitted by `_build_context`
    — it carries flat `nse_open` / `mcx_open` flags, NOT a nested
    `segments` list.

    Mapping:
      equity-segment exchanges (NSE/BSE/NFO/BFO/CDS/BCD) → ctx['nse_open']
      commodity-segment exchanges (MCX/NCO)              → ctx['mcx_open']
      unknown → False (never wrongly allow)

    Pure function — no DB / broker calls. Designed to be called from
    the action layer, the ticket route, and the MCP gated paths
    without any per-call setup cost.
    """
    seg_name = _segment_for_exchange(exchange)
    if not seg_name:
        return False
    if not isinstance(ctx, dict):
        return False
    if seg_name == "equity":
        return bool(ctx.get("nse_open"))
    if seg_name == "commodity":
        return bool(ctx.get("mcx_open"))
    return False


def _build_now_ctx() -> dict:
    """Build a fresh market-state context for the CURRENT wall-clock
    time. Used by the ticket route + MCP gated paths where we don't
    have a pre-built engine context. Reuses `_build_context` so the
    result is identical to what `run_cycle` sees on the same tick.

    `now` MUST be IST (Asia/Kolkata) — _build_context's hours_start /
    hours_end values are IST times of day, and `now.replace(hour=…)`
    keeps the original tz. Passing a UTC `now` would silently shift
    every comparison by 5h30m and erroneously report MCX (09:00–23:30
    IST) as closed for IST hours 09–14 (= UTC 03:30–08:30). The
    background engine path uses `timestamp_indian()` for exactly this
    reason; this helper now mirrors that contract.
    """
    from backend.shared.helpers.date_time_utils import timestamp_indian
    return _build_context(timestamp_indian())


# Back-compat alias used by callers that imported the old name.
_segments_now = _build_now_ctx


async def _prewarm_special_sessions_for_exchange(exchange: str) -> None:
    """Await the DB-backed special-sessions read for `exchange`'s holiday
    calendar BEFORE calling the sync `_build_now_ctx()` (2026-10-20 fix,
    closes the cold-cache gap for the call sites that use it — see
    `fetch_special_sessions_async`'s docstring in broker_apis.py).

    Callers (all `async def`, all able to `await` this): `orders_place.py`'s
    ticket market-hours gate, `orders_basket.py`'s per-leg gate,
    `chase.py`'s chase-start gate, and (added 2026-10-20, item 2)
    `template_attach.py`'s `_apply_template_to_order_impl`, immediately
    before its `_check_offhours_wing_gate(...)` call —
    `_check_offhours_wing_gate` itself stays plain `def` (cannot `await`);
    its caller does the awaiting instead. `apply_plan_live`
    (`template_attach.py`) was never actually exposed to this gap at all —
    it runs via `asyncio.to_thread` (confirmed via `write_queue.py`'s guard
    condition), not inline on the main loop.

    `market_lifecycle.poll()` (item 1, 2026-10-20) does NOT call this
    helper, deliberately — it resolves `holiday_exchange` via
    `_segment_for_exchange()`, which for the synthesized `cds` segment
    entry resolves to `"NSE"`, not the raw `"CDS"` key
    `_exchange_is_open()` later looks up via `fetch_special_sessions`.
    `poll()` instead awaits `fetch_special_sessions_async(segment["exchange"])`
    directly, per segment, guaranteeing the warmed key always matches the
    key that will be read.

    Best-effort: resolves the exchange's `holiday_exchange` (e.g. "NFO"→"NSE",
    "MCX"/"NCO"→"MCX") the same way `_ae_segment_flags` does, then awaits
    `fetch_special_sessions_async` for it. Any failure (unknown exchange, bad
    config, DB error) is swallowed — this is purely a cache pre-warm; the
    caller's subsequent `_build_now_ctx()` call still runs its existing,
    already-correct fallback behaviour either way.
    """
    try:
        from backend.brokers.broker_apis import fetch_special_sessions_async
        from backend.shared.helpers.utils import config as _cfg

        seg_name = _segment_for_exchange(exchange)
        if not seg_name:
            return
        seg_cfg = (_cfg.get("market_segments", {}) or {}).get(seg_name, {}) or {}
        holiday_exchange = seg_cfg.get("holiday_exchange", "NSE")
        await fetch_special_sessions_async(holiday_exchange)
    except Exception as e:
        # warning (not debug, 2026-10-20 fix) — a persistent failure here
        # silently turns off the evening-closed-holiday gate for the
        # caller's subsequent sync _build_now_ctx() call; must be visible
        # at default log level, not opt-in debug.
        logger.warning(f"_prewarm_special_sessions_for_exchange({exchange}) failed: {e}")


def _ae_cur_min_in_window(cur_min: int, start: int, end: int) -> bool:
    """Return True when cur_min falls inside [start, end].

    start <= end  → same-day range.
    start >  end  → crosses midnight; cur_min >= start OR cur_min <= end.
    Extracted from _in_blackout_window to remove the two-branch conditional."""
    if start <= end:
        return start <= cur_min <= end
    return cur_min >= start or cur_min <= end


def _in_blackout_window(now, windows: list) -> bool:
    """Phase 22 — return True if the current IST wall-clock falls inside
    any blackout window. Each window is `{"start": "HH:MM", "end": "HH:MM"}`
    in IST. Crossing-midnight windows ({"start":"23:00","end":"01:00"})
    are supported by treating start>end as "wraps".

    Empty or malformed entries are silently skipped — defense-in-depth
    so a bad row never accidentally mutes ALL agents."""
    if not windows or not now:
        return False
    try:
        from zoneinfo import ZoneInfo
        now_ist = now.astimezone(ZoneInfo("Asia/Kolkata"))
        cur_min = now_ist.hour * 60 + now_ist.minute
        for w in windows:
            if not isinstance(w, dict):
                continue
            try:
                sh, sm = (w.get("start") or "").split(":", 1)
                eh, em = (w.get("end")   or "").split(":", 1)
                start = int(sh) * 60 + int(sm)
                end   = int(eh) * 60 + int(em)
            except (TypeError, ValueError, AttributeError):
                continue
            if _ae_cur_min_in_window(cur_min, start, end):
                return True
        return False
    except Exception:
        return False


def _fire_at_window_active(fire_at: str, now, window_sec: int = 360) -> bool:
    """Return True when wall-clock IST is within `window_sec` of `fire_at`.

    `fire_at` is "HH:MM" IST. `now` is an aware datetime in any zone.
    Window opens at fire_at and lasts window_sec seconds — covers the
    full background poll cadence (default 5 min + 60 s slack so a
    single missed tick still catches the slot).

    Returns False on parse errors so a malformed value never fires
    an agent. The route layer already rejects bad input on save;
    this is defense-in-depth.
    """
    if not fire_at or not now:
        return False
    try:
        from zoneinfo import ZoneInfo
        hh_str, mm_str = fire_at.split(":", 1)
        hh, mm = int(hh_str), int(mm_str)
        now_ist = now.astimezone(ZoneInfo("Asia/Kolkata"))
        target = now_ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (now_ist - target).total_seconds()
        return 0 <= delta < window_sec
    except Exception:
        return False


def _v2_baseline_live(alert_state, now, offset_min: float) -> bool:
    start = alert_state.get('session_start') if alert_state else None
    if not start:
        return False
    from datetime import timedelta
    return (now - start) >= timedelta(minutes=offset_min)


def _v2_build_evalresult(matches, agent_name: str, *, replay_mode: bool = False,
                         latched_matches: list | None = None) -> EvalResult:
    """
    Wrap v2 matches into an EvalResult so the existing dispatch() function
    (which renders the Telegram/email body) can consume them unchanged.

    `replay_mode` is stamped into `detail` (not just threaded through
    `sim_mode`) so `_hydrate_latch_from_rows` can positively identify and
    skip replay-authored agent_events rows on process restart — replay
    ticks pass `sim_mode=False` (deliberately distinct from real sim
    runs, see `_cycle_process_agent`), so the SQL-level
    `AgentEvent.sim_mode.is_(False)` hydration filter alone does NOT
    exclude them. Piggybacking on `detail` (JSON, no migration needed)
    rather than changing what `sim_mode` means for dispatch()/actions —
    that would also flip replay's alert-banner/action-execution
    behavior, a materially larger and separate change than this fix.

    `latched_matches` — fix for the "hydration latches raw matches,
    not just the escalation-gated ones" bug (2026-10). `matches` is the
    FULL raw breaching list (used for `condition_text` / the alert body
    — operators must still see every currently-breaching leaf, gated or
    not). But a single tick can contain a MIX of leaves: some that just
    passed `_v2_apply_escalation_gate` (and thus updated `_V2_LATCH`
    this tick) and others on the SAME agent that are still gated
    (cooldown not elapsed / not escalated enough) — only possible
    because a non-empty `effective` subset lets the whole tick dispatch
    even though other leaves in `matches` didn't individually pass the
    gate. Storing the full raw `matches` list as the hydration source
    (old behavior) meant a gated leaf's WORSE, NOT-YET-ACTIONED value
    got latched on restart instead of its true last-fired value,
    delaying/missing real re-alerts post-deploy. `latched_matches`
    carries ONLY the subset that actually updated `_V2_LATCH` this tick
    (the caller's `effective` list) — `_hydrate_latch_from_rows` reads
    this field (falling back to `matches` for rows written before this
    fix). Defaults to `matches` when the caller has no escalation-gated
    subset to report (e.g. `bypass_suppression` sim runs, where
    effective == matches by construction).
    """
    # Compact one-liner per match: "scope metric=value (threshold)"
    lines = []
    for m in matches[:10]:  # cap — long lists get truncated
        val = m.get('value')
        try:
            val_str = f"{val:,.2f}" if isinstance(val, (int, float)) else str(val)
        except Exception:
            val_str = str(val)
        lines.append(
            f"{m.get('scope','?')} {m.get('metric','?')}={val_str} "
            f"({m.get('op','?')} {m.get('threshold','?')})"
        )
    if len(matches) > 10:
        lines.append(f"... +{len(matches) - 10} more")
    condition_text = " | ".join(lines) or agent_name
    detail: dict = {
        'matches': matches,
        'latched_matches': matches if latched_matches is None else latched_matches,
        'grammar': 'v2',
    }
    if replay_mode:
        detail['replay_mode'] = True
    return EvalResult(
        triggered=bool(matches),
        condition_text=condition_text,
        detail=detail,
    )


# ─── v2 rich-body Telegram + email ────────────────────────────────────────
#
# For v2 agents we bypass the generic dispatch() body and use the same
# narrow-mobile Telegram format + coloured HTML email table that the legacy
# alert_utils engine already produces. Keeping the user-facing shape
# consistent across both engines makes parity testing trivial — the
# operator can spot-diff two messages and only care about the agent slug.


def _v2_format_threshold(kind: str, threshold) -> str:
    """Format a threshold value with units appropriate to the alert kind.

    kind must be one of {static_pct, rate_pct, static_abs, rate_abs,
    negative_cash, negative_margin}. Non-numeric thresholds fall through to
    the str() fallback via the except branch.

    Belt-and-suspenders guard: some built-in agents (e.g. market-open-nse,
    market-preclose-mcx) hand-author an "always-true" sentinel threshold
    (-999999999) purely to drive a fire_at_time gate — never a real,
    displayable figure. Any threshold whose magnitude is absurdly large
    (>= 1e8, i.e. beyond any real ₹/% figure this app would ever alert on)
    is refused here and rendered as a neutral label instead, so a future
    agent definition that reuses this sentinel trick can't leak a
    fabricated number into the operator-facing alert even if its kind
    classification is ever wrong upstream.
    """
    try:
        thr = float(threshold)
        if abs(thr) >= 1e8:
            logger.warning(
                f"_v2_format_threshold: refusing to render sentinel-magnitude "
                f"threshold {thr!r} for kind={kind!r} — falling back to neutral label"
            )
            return "n/a"
        if kind in ('static_pct', 'rate_pct'):
            return f"{thr:.2f}%" + ("/min" if kind == 'rate_pct' else "")
        else:
            return f"-₹{abs(thr):,.0f}" + ("/min" if kind == 'rate_abs' else "")
    except Exception:
        return str(threshold)


def _ae_holdings_pct(row: dict) -> float | None:
    """Return day_change_percentage for a Holdings alert row, or None.

    Extracted from _v2_extract_pnl_fields to remove the ternary."""
    val = row.get('day_change_percentage')
    return float(val or 0) if val is not None else None


def _ae_funds_pnl(metric: str, value, row: dict) -> float:
    """Return the pnl float for a Funds section row.

    Fix #3/#4 — this now trusts the MATCH's already-resolved `value`
    (produced by the grammar's metric resolver — `cash` reads live
    'avail cash', `sod_cash` reads 'avail opening_balance', etc.) instead
    of independently re-reading raw columns with its own hard-coded
    field map. The old per-metric branch here read 'avail opening_balance'
    for `cash` even after the resolver itself was fixed to read live
    cash — so the alert BODY would keep showing SOD cash while the leaf
    actually fired on live cash. `row`/`metric` args are kept for call-
    site compatibility but are no longer consulted; extracted from
    `_v2_extract_pnl_fields` to keep that function's CC down."""
    return float(value) if value is not None else 0.0


def _v2_extract_pnl_fields(row: dict, section: str, metric: str,
                           value) -> tuple[float, float | None]:
    """Return (pnl, pct) appropriate for the given section.

    - Holdings  → day_change_val + day_change_percentage
    - Positions → pnl; pct stays None (computed later when used_margin is known)
    - Funds     → metric-driven field; pct always None
    """
    if section == 'Holdings':
        pnl: float = float(row.get('day_change_val', 0) or 0)
        pct: float | None = _ae_holdings_pct(row)
    elif section == 'Positions':
        if metric == 'day_val':
            pnl = float(row.get('day_change_val', 0) or 0)
        else:
            pnl = float(row.get('pnl', 0) or 0)
        pct = None  # computed later only when we have used_margin
    else:  # Funds
        pnl = _ae_funds_pnl(metric, value, row)
        pct = None
    return pnl, pct


def _v2_underlying_breakdown(df_positions, scope_label: str) -> list[dict]:
    """Compute per-underlying P&L breakdown for a position alert row.

    Returns empty list when the breakdown feature flag is off, when imports
    fail, or when df_positions is None (caller guards that before calling).
    """
    from backend.shared.helpers.settings import get_bool, get_int
    from backend.shared.helpers.summarise import breakdown_positions_by_underlying
    if not get_bool('alerts.show_underlying_breakdown', True):
        return []
    top_n = get_int('alerts.max_underlyings_per_alert', 5)
    return breakdown_positions_by_underlying(df_positions, account=scope_label, top_n=top_n)


def _v2_static_rate_enrichment(alert_state: dict, kind: str, scope_label: str,
                               rate_window_min: int) -> float | None:
    """Compute ΔP&L/min from pnl_history for static position alerts.

    Fix #1 — routed through the SAME `windowed_rate` helper the live
    `pnl_rate_abs`/`pnl_rate_pct` metrics use, so a static alert's
    displayed rate no longer shows a raw one-poll Δ ("-28k/min") when the
    live metric itself would have returned None for insufficient sample
    count/span. Anchored on the last sample's own timestamp (not
    wall-clock `now`) since this runs once, right after dispatch, using
    whatever history existed at that moment.
    """
    from backend.shared.helpers.settings import get_bool
    if not get_bool('alerts.show_rate_in_static_alerts', True):
        return None
    hist = (alert_state.get('pnl_history') or {}).get(('positions', scope_label), []) or []
    if not hist:
        return None
    # field_idx=1 → pnl ₹/min, matching rate_abs metric
    return windowed_rate(hist, hist[-1][0], rate_window_min, field_idx=1)


def _v2_derive_section(scope_tok: str) -> str:
    """Map a scope token prefix to its alert section label."""
    if scope_tok.startswith('holdings'):
        return 'Holdings'
    if scope_tok.startswith('positions'):
        return 'Positions'
    return 'Funds'


def _v2_derive_kind(metric: str, op: str = '') -> str:
    """Map a metric token (+ its leaf operator) to its alert kind label.

    `cash`/`avail_margin` only classify as the dedicated negative_cash /
    negative_margin "floor breach" kinds when the leaf's own operator is a
    below-floor comparison (`<` / `<=`) — e.g. the real `loss-funds-negative`
    agent. Built-in schedule-only agents (market-open-nse, market-preclose-
    mcx) reuse `avail_margin` with an always-true `>=` leaf purely to drive
    their `fire_at_time` gate (see the comment above `_INFO_AGENTS`) — that
    is NOT a margin breach, so it must fall through to the same generic
    kind any other non-special metric gets, not be mislabeled as one.
    """
    if metric in ('cash',) and op in ('<', '<='):
        return 'negative_cash'
    if metric in ('avail_margin',) and op in ('<', '<='):
        return 'negative_margin'
    if '_rate_abs' in metric:
        return 'rate_abs'
    if '_rate_pct' in metric:
        return 'rate_pct'
    if metric.endswith('_pct') or metric == 'pnl_pct':
        return 'static_pct'
    return 'static_abs'


def _v2_enrich_position_alert(
    section: str, kind: str, scope_label: str,
    df_positions, alert_state, rate_window_min: int, rate_val,
) -> tuple[list[dict], object]:
    """Compute optional position-alert enrichment fields.

    Returns (underlyings_breakdown, rate_val) after applying per-underlying
    breakdown and static-rate enrichment where applicable.
    """
    underlyings_breakdown: list[dict] = []
    if section == 'Positions' and df_positions is not None:
        try:
            underlyings_breakdown = _v2_underlying_breakdown(df_positions, scope_label)
        except Exception as e:
            logger.warning(f"underlying breakdown failed: {e}")

    if (section == 'Positions' and rate_val is None and alert_state
            and kind in ('static_pct', 'static_abs')):
        try:
            rate_val = _v2_static_rate_enrichment(alert_state, kind, scope_label, rate_window_min)
        except Exception as e:
            logger.warning(f"static-alert rate enrichment failed: {e}")

    return underlyings_breakdown, rate_val


def _v2_match_to_alertrow(match: dict, *,
                          df_positions=None,
                          alert_state: dict | None = None,
                          rate_window_min: int = 10) -> dict:
    """
    Convert a v2 evaluator match into the alert-row dict shape consumed by
    alert_utils._tg_alert_body / _email_alert_body.

    Optional enrichment when caller supplies the kwargs:
      - df_positions: raw broker positions DataFrame. Drives the per-
        underlying breakdown surfaced under each Position alert.
      - alert_state: persistent state from background.py — carries
        `pnl_history` keyed by (section, scope). Lets us surface a
        rate-of-loss readout on STATIC position alerts (rate alerts
        already carry it via `rate_val`).
      - rate_window_min: how far back to walk pnl_history when computing
        the rate. Defaults to the engine's rate window.
    """
    scope_tok = match.get('scope', '') or ''
    metric    = match.get('metric', '') or ''
    op        = match.get('op', '')    or ''
    row       = match.get('row')      or {}
    value     = match.get('value')
    threshold = match.get('threshold')

    section     = _v2_derive_section(scope_tok)
    kind        = _v2_derive_kind(metric, op)
    pnl, pct    = _v2_extract_pnl_fields(row, section, metric, value)
    rate_val    = value if kind in ('rate_abs', 'rate_pct') else None
    thr_str     = _v2_format_threshold(kind, threshold)
    # Underlying-divergence feature — a `positions.any_underlying` row
    # carries no 'account' key at all (same reason `_latch_key`/
    # `_match_join_key` fall back to `underlying`), so the OLD
    # `row.get('account', 'TOTAL')` silently mislabeled every such alert
    # row as the generic 'TOTAL' scope instead of the real underlying
    # that fired. Every pre-existing scope still carries a real, truthy
    # `account` string, so this fallback changes nothing for them.
    scope_label = str(row.get('account') or row.get('underlying') or 'TOTAL')

    # Enrichment (per-underlying breakdown lookup, static-rate pnl_history
    # lookup) MUST use the REAL, unmasked `scope_label` — it's a join/
    # filter key against df_positions'/alert_state's own real account
    # codes, not display text. Masking happens ONLY on the value that
    # actually reaches the returned `scope` field below.
    underlyings_breakdown, rate_val = _v2_enrich_position_alert(
        section, kind, scope_label, df_positions, alert_state, rate_window_min, rate_val,
    )

    # Confirmed leak fix (2026-10): this was the single upstream point
    # where a per-account (non-TOTAL) breach alert's REAL broker account
    # code flowed into `scope`, unmasked, from here straight through
    # `_tg_alert_body`/`_email_alert_body` to Telegram/email/ntfy with no
    # masking call anywhere downstream — confirmed end-to-end (background.py/
    # broker_apis.py never mask the DataFrame's `account` column upstream;
    # `event_agents._gate_passes`, the only `mask_account_in_text()` call in
    # that module's dispatch path, never even runs for `RICH_ALERT_AGENT`,
    # whose action carries no `gate: True`). Masking ONCE here, after
    # enrichment (which needs the real code), means every downstream
    # consumer of this row's `scope` field gets the masked value for free —
    # no need to patch `_tg_alert_body`/`_email_alert_body`/anything else
    # that reads `scope` separately. `mask_account()` (not
    # `mask_account_in_text()`) is the correct helper here — `scope_label`
    # is a single discrete account value, not free-form text to scan;
    # `mask_account()` also passes 'TOTAL' through unchanged (its own
    # explicit special case), so the aggregate-row case is unaffected.
    from backend.shared.helpers.utils import mask_account
    masked_scope = mask_account(scope_label)

    return dict(
        section=section, scope=masked_scope, kind=kind,
        pnl=pnl, pct=pct, rate_val=rate_val, threshold=thr_str,
        underlyings_breakdown=underlyings_breakdown,
    )


def _ae_sort_alert_rows(rows: list[dict]) -> None:
    """Sort alert rows in-place: Holdings → Positions → Funds, TOTAL last.

    Extracted from _v2_send_rich_alert to remove inline sort logic."""
    order = {'Holdings': 0, 'Positions': 1, 'Funds': 2}
    rows.sort(key=lambda r: (order.get(r['section'], 9),
                              0 if r['scope'] != 'TOTAL' else 1,
                              r['scope']))


def _ae_sim_notify_suppressed(agent, sim_mode: bool) -> bool:
    """Return True when sim_mode is active AND notify_during_run is off.

    When True the caller should return True early — noisy channels are
    suppressed but the audit trail remains intact.
    Extracted from _v2_send_rich_alert to reduce CC there."""
    if not sim_mode:
        return False
    try:
        from backend.shared.helpers.settings import get_bool
        if not get_bool("simulator.notify_during_run", False):
            logger.info(
                f"[SIM] notify_during_run=off — skipped TG/email for "
                f"agent {agent.slug}; event row + log line still written"
            )
            return True
    except Exception:
        pass
    return False


def _v2_rich_alert_custom_message(agent, eval_result, context: dict | None):
    """Return the first resolved ALERT `msg=` override that applies to
    this fire, or `None` if none of the agent's telegram/ntfy/email
    channels carry one.

    Bug fix (2026-10): `_v2_send_rich_alert` previously ALWAYS built its
    own generic kind/threshold table for telegram/email (and ntfy, which
    reuses the telegram body as plain text) — a channel's own `message`
    override (Sprint 6's plain-string `msg=`, or Part 2's `{"$given":
    name}` server-evaluated marker) was silently dropped for these three
    channels on every REAL cycle-agent fire, since `rich_sent=True`
    makes `dispatch()` skip exactly those three channels to avoid
    duplicate delivery (see `_ae_dispatch_survivor_entry`'s `skip`
    comment). Only log/websocket/inapp (which always run through
    `dispatch()` regardless) ever showed the real custom message. This
    made `msg=` effectively non-functional for the most common channels
    on the most common agent kind — fixed by checking for an override
    HERE, before the generic table is ever built, using the EXACT SAME
    `_tags_match()` gate and `{"$given": ...}` resolution
    (`events._resolve_given_message`) the ordinary `dispatch()` path
    already uses for its own custom-message grouping.

    Known limitation, by design of this function's existing single-
    shared-body architecture (ONE `tg_table`/`email_table_html` pair
    feeds telegram+ntfy+email uniformly via `RICH_ALERT_AGENT` — see
    `event_agents.py:_render_rich_alert`): if different channels on the
    SAME agent carry DIFFERENT custom messages, there's no way to show
    each channel its own text at this layer — the first match found
    (checked in telegram, ntfy, email order) wins for all three. This
    mirrors the existing single-body constraint, not a new one; true
    per-channel differentiation would need the same per-message-group
    splitting `events.py:dispatch()` already does for its own (non-rich)
    path, which is out of scope for this fix.

    Masking: this path sends free-form text (an operator-authored
    literal, or a GIVEN-computed value that could legitimately reference
    `account_id`) straight to Telegram/email/ntfy — text that, before
    this fix, NEVER reached those three channels at all (dispatch()
    skipped them whenever the rich path sent). The returned string is
    masked via `mask_account_in_text()` before returning — the SAME
    chokepoint `events.py:_describe_actions` already uses for its own
    free-form action-description text, per this codebase's "account
    masking used in all alerts" invariant.

    FIXED separate leak (2026-10, same commit as this override check,
    confirmed end-to-end from source to send before the fix):
    `background.py` (builds `sum_positions`/`df_margins`) and
    `broker_apis.py` (the broker fetch layer) never call `mask_account`/
    `mask_column` on the `account` column; `RICH_ALERT_AGENT`'s action
    carries no `gate: True`, so `event_agents._gate_passes()` — the only
    `mask_account_in_text()` call anywhere in that module's dispatch
    path — never even runs for it (`if not gated: return rec`
    short-circuit); `_send_channel()` then sent the renderer's tuple
    completely unmodified. So a per-account (non-TOTAL) breach alert
    rendered via the generic kind/threshold table shipped the REAL,
    unmasked broker account code to Telegram/email/ntfy. Fixed at the
    single upstream choke point instead of patching every renderer:
    `_v2_match_to_alertrow` now masks `scope` via `mask_account()`
    (the single-discrete-value helper — `scope` is one account code, not
    free text to scan) AFTER the enrichment lookups that still need the
    real unmasked code (`_v2_enrich_position_alert`'s df_positions/
    alert_state joins) — see that function's own inline comment. Every
    downstream consumer of a row's `scope` field (`_tg_alert_body`,
    `_email_alert_body`, the sort key in `_ae_sort_alert_rows`) gets the
    masked value for free.
    """
    if eval_result is None:
        return None
    from backend.api.algo.template_registry import resolve_events
    from backend.api.algo.events import _tags_match, _resolve_given_message
    from backend.shared.helpers.utils import mask_account_in_text

    channels = resolve_events(agent.events if isinstance(agent.events, list) else [])
    for wanted_channel in ("telegram", "ntfy", "email"):
        for ch in channels:
            if ch.get("channel") != wanted_channel or not ch.get("enabled", False):
                continue
            if not _tags_match(ch, eval_result):
                continue
            raw_message = ch.get("message")
            if not raw_message:
                continue
            resolved = _resolve_given_message(raw_message, agent, context)
            if resolved:
                return mask_account_in_text(resolved) or resolved
    return None


async def _v2_send_rich_alert(agent, matches, now, sim_mode: bool = False,
                              context: dict | None = None, eval_result=None):
    """
    Render the v2 alert as the same narrow-TG + HTML-table format the legacy
    engine uses, and send through Telegram + email via alert_utils's own
    dispatcher (which already branch-tags and honours is_enabled gates).
    Returns True when at least one channel was attempted.

    `context` is the same dict run_cycle passed into the evaluator; we
    surface df_positions + alert_state from it so per-underlying
    breakdown and static-alert rate enrichment can light up. Backward-
    compatible — when context is None each row builds with the bare
    section/scope/kind/pnl/threshold fields and no enrichment.

    `eval_result` (the same `EvalResult` the caller also passes to
    `dispatch()`) lets this function check for an operator-configured
    ALERT `msg=` override BEFORE falling back to the generic kind/
    threshold table — see `_v2_rich_alert_custom_message`'s docstring
    for the bug this closes. `None` (the default, for any caller that
    doesn't have one — e.g. existing tests) disables the override check
    entirely, preserving the exact prior behavior.
    """
    # Late import avoids the agent_engine → alert_utils cycle at import time.
    from backend.shared.helpers.alert_utils import (
        _tg_alert_body, _email_alert_body,
    )
    from backend.shared.helpers.date_time_utils import timestamp_display

    df_positions = (context or {}).get("df_positions")
    alert_state  = (context or {}).get("alert_state")
    cfg          = _v2_cfg()
    rows = [
        _v2_match_to_alertrow(
            m,
            df_positions=df_positions,
            alert_state=alert_state,
            rate_window_min=cfg['rate_window_min'],
        )
        for m in matches
    ]
    if not rows:
        return False

    # Sort Holdings → Positions → Funds, per-account before TOTAL.
    _ae_sort_alert_rows(rows)

    # `simulator.notify_during_run` gate — audit trail is always written;
    # only the noisy outbound channels are suppressed in sim mode.
    if _ae_sim_notify_suppressed(agent, sim_mode):
        return True

    # Operator-configured ALERT msg= override — checked FIRST, before
    # either the "scheduled" special case or the generic kind/threshold
    # table, so an explicit custom message always wins (an operator who
    # set msg= on a scheduled info/low-tier agent still gets THEIR text,
    # not the generic "Scheduled — HH:MM IST" line). See
    # _v2_rich_alert_custom_message's docstring for the bug this fixes.
    custom_message = _v2_rich_alert_custom_message(agent, eval_result, context)
    # Scheduled, notify-only informational agents (market-open-nse,
    # market-preclose-mcx) use an always-true `avail_margin >= -999999999`
    # leaf purely to drive their `fire_at_time` gate — there is no real
    # breach to report, so the per-account kind/threshold table is
    # meaningless here (and, pre-fix, actively misleading). Mirrors the
    # exact predicate _cycle_maybe_buffer_fire already uses to override
    # `condition_text` to "Scheduled — HH:MM IST" for the same agents —
    # that text never reached Telegram/email (only the audit log/websocket
    # did), so it's surfaced here too, with the agent's own name, so the
    # operator can tell what the alert is actually about.
    if custom_message:
        tg_body    = custom_message
        email_html = f"<p>{html.escape(custom_message)}</p>"
    elif (getattr(agent, 'fire_at_time', None)
            and getattr(agent, 'tier', 'medium') in ('info', 'low')):
        scheduled_line = f"{agent.name} — Scheduled — {agent.fire_at_time} IST"
        tg_body    = scheduled_line
        email_html = f"<p>{html.escape(scheduled_line)}</p>"
    else:
        tg_body    = _tg_alert_body(rows)
        email_html = _email_alert_body(rows)
    # Single-line agent tag for the email subject (via subject_detail) —
    # format_notification_header's full output carries an embedded
    # newline (agent line + dual-tz timestamp) which must never reach an
    # email Subject header or ntfy Title HTTP header; take only its
    # first line here. The dispatch_payload layer (alert_utils.py) is
    # where the full header (both lines) lands in the message BODY.
    #
    # 2026-10 audit fix: `format_notification_header`'s first line is
    # "Agent: {name} (#{id})" — but `dispatch_payload`'s own base email
    # prefix (alert_utils.py `_MSG_TYPES['alert']`) is already
    # "RamboQuant Agent: ", and `_email_message` concatenates
    # `email_prefix_full + subject_detail` with no separator, so using
    # the full header line here produced a duplicated
    # "RamboQuant Agent: Agent: {name} (#{id})" subject. Strip the
    # leading "Agent: " from THIS call site's own result only —
    # `format_notification_header` itself is unchanged (other callers,
    # e.g. the Telegram/email BODY line, want the full "Agent: " label
    # and are unaffected).
    from backend.shared.helpers.alert_utils import format_notification_header
    _subject_header_line = format_notification_header(agent.name, agent.id).splitlines()[0]
    subject = (_subject_header_line[len("Agent: "):]
               if _subject_header_line.startswith("Agent: ")
               else _subject_header_line)
    mode_tag   = '' if sim_mode else _agent_execution_mode_tag(agent)
    try:
        logger.info(
            f"Agent [{agent.slug}] alert recorded",
            extra={
                "tags": ["agent"], "alert_event": "rich_alert", "agent_slug": agent.slug,
                "agent_name": agent.name, "agent_id": agent.id,
                "ist_display": timestamp_display(), "tg_table": tg_body,
                "email_table_html": email_html, "subject_detail": subject,
                "sim_mode": bool(sim_mode), "mode_tag": mode_tag,
                # 2026-10-16 fix — the REAL triggering agent's tier, so
                # event_agents.py's RICH_ALERT_AGENT (a generic fan-out
                # dispatcher for every agent's alerts, itself always
                # tier="medium") can resolve ntfy priority from the
                # real agent instead of a hardcoded "urgent" or its own
                # tier. See _dispatch_agent's per-channel loop.
                "tier": getattr(agent, "tier", None),
            },
        )
    except Exception as e:
        logger.error(f"Agent [{agent.slug}] rich alert record failed: {e}")
        return False
    return True


def _agent_execution_mode_tag(agent) -> str:
    """
    Inspect the master paper_trading_mode toggle and report whether this
    agent's broker actions would land as paper or live. Used to tag alert
    subjects so an operator on Telegram can tell at a glance whether a
    fired agent caused a real broker order or a paper one.

      - non-main branch: returns '' (live engine doesn't run on dev)
      - main, no broker actions configured: '' (alert-only agent)
      - main, paper_trading_mode=True:  '[PAPER]'
      - main, paper_trading_mode=False: '' (live execution)
    """
    from backend.shared.helpers.utils import is_prod_branch
    from backend.shared.helpers.settings import get_bool
    from backend.api.algo.actions import BROKER_ACTIONS
    if not is_prod_branch():
        return ''
    types = {(a.get('type') or '') for a in (agent.actions or [])}
    broker_types = types & BROKER_ACTIONS
    if not broker_types:
        return ''
    if get_bool("execution.paper_trading_mode", False):
        return '[PAPER]'
    return ''


def _initial_shadow_remaining(agent) -> int | None:
    """
    Compute the shadow remaining-fires count for an agent at sim
    iteration start. Mirrors the real lifespan budget at the moment
    the iteration begins, then ticks down in-memory only.

      - `persistent`  → None (no cap; never exhausts in sim)
      - `one_shot`    → 1 fire (always)
      - `n_fires`     → max - current_trigger_count, floor 0
      - `until_date`  → 999 fires (treat as effectively unlimited;
                        until_date is time-based, not fire-count-based.
                        Time-based exhaustion is handled separately at
                        the top of run_cycle.)
    """
    lt = getattr(agent, "lifespan_type", "persistent")
    if lt == "persistent" or not lt:
        return None
    if lt == "one_shot":
        return 1
    if lt == "n_fires":
        max_fires = getattr(agent, "lifespan_max_fires", None)
        used      = getattr(agent, "trigger_count", 0) or 0
        if max_fires is None:
            return None  # malformed config → don't cap shadow
        return max(0, int(max_fires) - int(used))
    if lt == "until_date":
        return 999
    return None


def _v2_cfg():
    """
    Read the gate/suppression parameters. Reads from the DB-backed
    Settings table first (operators can tune these from /admin/settings
    without a deploy); falls back to backend_config.yaml for the legacy
    flat keys if the row is absent.
    """
    from backend.shared.helpers.settings import get_int, get_float
    return {
        'rate_window_min':       get_int('alerts.rate_window_min', 10),
        'baseline_offset_min':   get_int('alerts.baseline_offset_min', 15),
        'cooldown_min':          get_int('alerts.cooldown_minutes', 30),
        'suppress_delta_abs':    get_int('alerts.suppress_delta_abs', 15000),
        'suppress_delta_pct':    get_float('alerts.suppress_delta_pct', 0.5),
    }


# Built-in agents seeded on first startup
BUILTIN_AGENTS = []


# ═══════════════════════════════════════════════════════════════════════════
#  Loss-rule agents (v2 grammar)
# ═══════════════════════════════════════════════════════════════════════════
#
# Each risk rule is an Agent row whose `conditions` is a grammar tree of
# metric/scope/op/value leaves combined by all/any/not. These replace the
# former alert_utils.check_and_alert hard-coded engine — the agent engine
# owns every loss/fund alert end-to-end.
#
# Notify channels + cooldown come from `_LOSS_AGENT_DEFAULTS`. The engine-
# wide suppression deltas and baseline-gate offset are read from
# backend_config.yaml (alert_suppress_delta_abs / _pct,
# alert_baseline_offset_min, alert_rate_window_min, alert_cooldown_minutes).

_LOSS_AGENTS = [
    # ── Consolidated loss-guardrails — one agent per (topic, scope) pair ──
    #
    # Each agent's condition tree uses `any:` to OR multiple threshold
    # types (static %, static ₹, rate ₹/min, rate %/min) together. The
    # alert dispatcher already renders one row per matched leaf, so a
    # single fire of e.g. loss-positions-total with three sub-conditions
    # crossed produces three detail rows in the Telegram message —
    # operator loses zero information vs 4 separate agents.
    #
    # Why per-account + total stay as SEPARATE agents per topic:
    #   - tier differs (acct = high, total = critical)
    #   - notify channels may differ (acct = telegram-only,
    #     total = telegram + email + pager)
    #   - actions may differ (acct = ping; total = future kill-switch)
    # Keep the seam so future config can diverge without re-splitting.
    #
    # See LAB_MCP_GUIDE.md section 7 for the consolidation rationale +
    # the retired slug list.

    # ── Positions: per-account guardrail (high tier) ────────────────────
    dict(slug="loss-positions-acct",
         long_name="when:positions.any_acct.pnl<=acct-thresholds   alert:high/tg+email+log   do:notify-only",
         tier="high",
         topic="positions_loss",
         name="Positions per-account loss guardrail",
         description=(
             "Fires when ANY account's positions trip the per-account "
             "static loss thresholds: -2% of margin OR -₹30k. "
             "Rate-of-change conditions have been moved to "
             "loss-rate-acct (critical tier, 10-min cooldown)."
         ),
         conditions={"any": [
             {"metric": "day_val", "scope": "positions.any_acct", "op": "<=", "value": -30000},
             {"metric": "day_pct", "scope": "positions.any_acct", "op": "<=", "value": -2.0},
         ]},
         scope="total",
         status="inactive",
         ),

    # ── Positions: per-account burn-rate guardrail (critical tier) ───────
    # Split from loss-positions-acct so rate conditions can have a shorter
    # cooldown (10 min vs 30 min) and critical-tier ntfy priority. A burn
    # rate that sustains beyond the first alert window needs to re-fire
    # quickly; static threshold alerts do not.
    dict(slug="loss-rate-acct",
         long_name="when:positions.any_acct.pnl_rate critical/tg+ntfy+log do:notify-only",
         tier="critical",
         topic="positions_loss",
         name="Positions per-account burn-rate guardrail",
         conditions={"all": [
             {"metric": "pnl_rate_abs", "scope": "positions.any_acct", "op": "<=", "value": -10000},
             {"metric": "pnl_rate_pct", "scope": "positions.any_acct", "op": "<=", "value": -0.25},
         ]},
         cooldown_minutes=10,
         status="inactive",
         ),

    # ── Positions: per-underlying burn-rate, gated on theoretical-vs-
    # market loss divergence (critical tier) ────────────────────────────
    #
    # A plain rate-of-change alert (loss-rate-acct above) fires on ANY
    # fast burn, including a burn that's just the EXPECTED Greeks-implied
    # move for a big spot swing (e.g. a legit 3% NIFTY dump blowing
    # through a short-gamma book exactly as priced). Gating on
    # underlying_loss_divergence first means this agent only fires when
    # the rate burn is ALSO diverging from what the frozen-vol
    # Greeks/IV-implied reprice would predict — i.e. something beyond a
    # plain, already-priced-in spot move (a vol blowup, a stale/bad
    # quote, a broker pricing anomaly, …). See CLAUDE.md / grammar.py's
    # module comment above `_scope_positions_any_underlying` for the full
    # baseline-reprice design. Per-underlying (not per-account) because a
    # book-wide ₹ threshold means something different for a single-lot
    # collar than a 50-lot straddle.
    #
    # Shipped INACTIVE at first (2026-10-22) pending a fix to the
    # reference-point mismatch between this agent's two gate sides — see
    # grammar_tokens/metric_underlying_market_loss.py's `_metric_underlying_market_loss` docstring. Fixed and
    # activated the same day; status now defaults to "active" via
    # `_LOSS_AGENT_DEFAULTS` like every other non-destructive loss agent.
    dict(slug="loss-positions-underlying-divergence",
         long_name="when:positions.any_underlying.divergence>100k AND pnl_rate   alert:critical/tg+ntfy+log   do:notify-only",
         tier="critical",
         topic="positions_loss",
         name="Per-underlying loss divergence + burn-rate guardrail",
         description=(
             "Fires when an underlying's theoretical-vs-market loss "
             "divergence exceeds ₹100k AND that same underlying is also "
             "burning fast (-₹10k/min). Divergence gates out a plain, "
             "already-priced-in spot move — only a burn that's ALSO "
             "drifting from the frozen-vol Greeks/IV-implied reprice "
             "fires this agent. The condition tree also carries a "
             "pnl_rate_pct (%/min) leaf to match the %/min vs ₹/min "
             "pairing every other burn-rate agent uses, but it is "
             "currently INERT for this scope — no per-underlying margin "
             "denominator exists yet (see pnl_rate_pct's own docstring) — "
             "so only the ₹/min leaf can fire today."
         ),
         conditions={"all": [
             {"metric": "underlying_loss_divergence", "scope": "positions.any_underlying",
              "op": ">", "value": 100000},
             {"any": [
                 {"metric": "pnl_rate_abs", "scope": "positions.any_underlying", "op": "<=", "value": -10000},
                 {"metric": "pnl_rate_pct", "scope": "positions.any_underlying", "op": "<=", "value": -0.25},
             ]},
         ]},
         cooldown_minutes=10,
         ),

    # ── Positions: total guardrail (critical tier) ──────────────────────
    dict(slug="loss-positions-total",
         long_name="when:positions.total.pnl<=total-thresholds   alert:critical/tg+email+log   do:notify-only",
         tier="critical",
         topic="positions_loss",
         name="Positions total loss guardrail",
         description=(
             "Fires when the book-wide positions trip total loss "
             "thresholds: -2% of total margin OR -₹50k OR -₹6k/min OR "
             "-0.25 %/min. Critical-tier — implicit 'the whole book "
             "is bleeding' signal."
         ),
         conditions={"any": [
             {"metric": "day_val",      "scope": "positions.total", "op": "<=", "value": -50000},
             {"metric": "day_pct",      "scope": "positions.total", "op": "<=", "value": -2.0},
             {"metric": "pnl_rate_abs", "scope": "positions.total", "op": "<=", "value": -6000},
             {"metric": "pnl_rate_pct", "scope": "positions.total", "op": "<=", "value": -0.25},
         ]},
         scope="total",
         ),

    # ── Funds: margin shortfall warning (early-warning before negative) ──
    dict(slug="loss-margin-low",
         long_name="when:funds.any_acct.avail_margin>0&&<25000   alert:high/tg+email+ntfy+log   do:notify-only",
         tier="high",
         topic="funds_warning",
         name="Margin shortfall warning",
         description=(
             "Disabled — cross-account false positives: Dhan/Groww accounts "
             "always report avail_margin=0 (their funds API maps differently), "
             "satisfying the <25000 branch while Kite accounts satisfy >0. "
             "The loss-funds-negative agent covers the real case (margin < 0)."
         ),
         conditions={"all": [
             {"op": "<", "scope": "funds.any_acct", "metric": "avail_margin", "value": 25000},
             {"op": ">", "scope": "funds.any_acct", "metric": "avail_margin", "value": 0},
         ]},
         scope="total",
         status="inactive",
         ),

    # ── Funds: operational negatives (one agent — both are critical) ────
    dict(slug="loss-funds-negative",
         long_name="when:funds.any_acct.cash<0 OR margin<0   alert:critical/tg+email+log   do:notify-only",
         tier="critical",
         topic="funds_warning",
         name="Account funds gone negative (cash or margin)",
         description=(
             "Fires when ANY account's cash OR available margin dips "
             "below zero. Both critical, both about operational health — "
             "consolidated into one agent."
         ),
         conditions={"any": [
             {"metric": "cash",         "scope": "funds.any_acct", "op": "<", "value": 0},
             {"metric": "avail_margin", "scope": "funds.any_acct", "op": "<", "value": 0},
         ]},
         scope="total",
         ),

    # ── Auto-close on severe loss (destructive — ships INACTIVE) ────────
    # Kept as its own agent (not consolidated) because:
    #   - it carries a destructive ACTION (chase_close_positions), not
    #     just notify — needs independent on/off control
    #   - operators frequently want this off while keeping the
    #     loss-positions-total alert on
    #   - auto-close has its own audit story (broker-touching) — easier
    #     to read in /admin/research → Audit when isolated
    dict(slug="loss-pos-total-auto-close",
         long_name="when:positions.total.pnl<=-50k   alert:critical/tg+email+log   do:chase-close(total)",
         tier="critical",
         topic="positions_loss",
         name="Auto-close positions on total ≥ ₹50k loss",
         description=(
             "When total positions pnl ≤ -₹50k, calls chase_close_positions "
             "(adaptive limit-order chase engine) to flatten every open "
             "position. Ships INACTIVE — destructive; enable from /agents "
             "after you've run the simulator against it."
         ),
         conditions={"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -50000},
         scope="total",
         actions=[
             {"type": "chase_close_positions",
              "params": {"scope": "total", "timeout_minutes": 10, "adjust_pct": 0.1}},
         ],
         status="inactive",
         ),
]


# Enrich each row with the common notify + cooldown shape so BUILTIN_AGENTS
# keeps its existing keys; the engine's scheduler reads these fields.
# email + ntfy removed from defaults — routing now driven by alert_routing in
# backend_config.yaml. ntfy priority is set per-agent (high or urgent) below.
_LOSS_AGENT_DEFAULTS = dict(
    events=[
        {"channel": "telegram", "enabled": True},
        {"channel": "log",      "enabled": True},
    ],
    actions=[],                 # notify-only. Attach actions via admin UI later.
    schedule="market_hours",
    cooldown_minutes=30,
    status="active",            # v2 grammar is now the sole loss-alert engine
)

# ntfy priority per agent slug. critical-tier agents → urgent; high-tier → high.
_LOSS_AGENT_NTFY: dict[str, str] = {
    "loss-positions-acct":                   "high",
    "loss-rate-acct":                        "urgent",
    "loss-positions-underlying-divergence":  "urgent",
    "loss-positions-total":                  "urgent",
    "loss-margin-low":                       "high",
    "loss-funds-negative":                   "urgent",
    "loss-pos-total-auto-close":             "urgent",
}

for _a in _LOSS_AGENTS:
    for _k, _v in _LOSS_AGENT_DEFAULTS.items():
        _a.setdefault(_k, _v)
    _slug = _a.get("slug", "")
    if _slug in _LOSS_AGENT_NTFY and not any(
        e.get("channel") == "ntfy" for e in _a.get("events", [])
    ):
        _a["events"] = list(_a["events"]) + [
            {"channel": "ntfy", "enabled": True, "priority": _LOSS_AGENT_NTFY[_slug]},
        ]
BUILTIN_AGENTS.extend(_LOSS_AGENTS)


# ── Expiry-day agents (Item 1 / Phase 25) ────────────────────────────
#
# Two seeded agents. Both ship INACTIVE — the existing ExpiryEngine
# background task at 09:20 IST already handles the automatic close.
# These agents add VISIBILITY (alert) + an opt-in auto-close path
# the operator can activate per-account or globally.
#
# Run side-by-side with ExpiryEngine for one expiry week before
# considering retirement of the bg task. The bg task fires at 09:20;
# these agents fire at 15:15 (NFO) / 23:00 (MCX) — different times,
# no collision.
#
# fire_at_time MUST equal order_hold_gate.cutoff_for(exchange) — the
# scan-and-close action (_action_live_expiry_auto_close) gates on
# before_cutoff(exchange), which is True (deferred) until
# `close - lead_minutes`. NFO: 15:30 close - lead_minutes_nfo(15) =
# 15:15. MCX: 23:30 close - lead_minutes_mcx(30) = 23:00. Fixed 2026-10
# (Sprint 1a): the NFO seed previously used 15:00, 15 minutes BEFORE
# its own cutoff, so every cycle inside its 6-minute firing window
# logged "before cut-off; scan deferred" and the action never actually
# ran. The MCX sibling happened to work only because 23:00 already
# equals its own cutoff.
_EXPIRY_AGENTS = [
    dict(slug="expiry-day-positions-alert",
         long_name="when:positions.expiring_today.days<=1.5   alert:high/tg+email+log   do:notify-only",
         tier="high",
         topic="expiry_warning",
         name="Positions expiring today — review alert",
         description=(
             "Notify-only. Fires once per session when ANY open F&O "
             "position is expiring today (days_until_expiry ≤ 1.5). "
             "Lets the operator review + manually close before the "
             "existing ExpiryEngine bg task takes over at scheduled "
             "times. Ships INACTIVE — enable once you've reviewed "
             "what 'positions expiring today' surfaces on a real "
             "expiry day."
         ),
         conditions={"metric": "days_until_expiry",
                     "scope": "positions.expiring_today",
                     "op": "<=", "value": 1.5},
         scope="total",
         schedule="market_hours",
         cooldown_minutes=180,        # one alert per half-day, max
         status="inactive",
         ),

    dict(slug="expiry-day-equity-itm-auto-close",
         long_name="when:positions.expiring_today.nfo.is_itm==1 @15:15   alert:critical/tg+email+log   do:expiry-auto-close(NFO)",
         tier="critical",
         topic="expiry_warning",
         name="Auto-close ITM equity options on expiry day (T-15min)",
         description=(
             "At 15:15 IST (15 min before NSE 15:30 close — matches "
             "order_hold_gate.cutoff_for('NFO')'s default "
             "lead_minutes_nfo=15) on expiry day, chase-close EVERY "
             "ITM equity option (NFO). Equity rules: hedged or not, "
             "every ITM contract must be closed before expiry — "
             "Zerodha does not net-settle NFO option pairs and "
             "physical settlement / STT on ITM longs is the trap. "
             "Wraps the ExpiryEngine scan+close, restricted to NFO. "
             "Ships INACTIVE (destructive)."
         ),
         conditions={"all": [
             {"metric": "is_itm",
              "scope":  "positions.expiring_today.nfo",
              "op":     "==", "value": 1.0},
         ]},
         scope="total",
         schedule="market_hours",
         fire_at_time="15:15",
         cooldown_minutes=60,
         status="inactive",
         actions=[
             {"type": "expiry_auto_close",
              "params": {"exchange": "NFO"}},
         ],
         ),

    dict(slug="expiry-day-commodity-itm-auto-close",
         long_name="when:positions.expiring_today.mcx_unhedged.is_itm==1 @23:00   alert:critical/tg+email+log   do:expiry-auto-close(MCX)",
         tier="critical",
         topic="expiry_warning",
         name="Auto-close ITM commodity options on expiry day (T-30min)",
         description=(
             "At 23:00 IST (30 min before MCX 23:30 close) on expiry "
             "day, chase-close MCX ITM/NTM commodity options whose "
             "residual qty remains non-zero after the ExpiryEngine's "
             "4-rule greedy theta-priority netting pass: \n"
             "  1. Long CE  + Short CE  (qty cancellation)\n"
             "  2. Long PE  + Short PE  (qty cancellation)\n"
             "  3. Long CE  + Long PE   (both receive at settlement)\n"
             "  4. Short CE + Short PE  (locked-in payment)\n"
             "Same-account FUT positions on the same underlying are "
             "also paired as delta-offset partners (Long CE↔Short "
             "FUT, etc.). Netting is scoped per (account, underlying, "
             "expiry) — different accounts settle independently. "
             "Mirrors the /admin/options Close-tab logic so the "
             "agent and the operator UI agree on what stays in the "
             "close list. Ships INACTIVE (destructive)."
         ),
         conditions={"all": [
             {"metric": "is_itm",
              "scope":  "positions.expiring_today.mcx_unhedged",
              "op":     "==", "value": 1.0},
         ]},
         scope="total",
         schedule="market_hours",
         fire_at_time="23:00",
         cooldown_minutes=60,
         status="inactive",
         actions=[
             {"type": "expiry_auto_close",
              "params": {"exchange": "MCX"}},
         ],
         ),
]
_EXPIRY_AGENT_DEFAULTS = dict(
    events=[
        {"channel": "telegram", "enabled": True},
        {"channel": "email",    "enabled": True},
        {"channel": "log",      "enabled": True},
        {"channel": "ntfy",     "enabled": True},
    ],
    actions=[],
)
for _a in _EXPIRY_AGENTS:
    for _k, _v in _EXPIRY_AGENT_DEFAULTS.items():
        _a.setdefault(_k, _v)
BUILTIN_AGENTS.extend(_EXPIRY_AGENTS)


# ── Expiry risk alert agents ──────────────────────────────────────────
#
# Two active (not INACTIVE) notify-only agents that fire during market
# hours on expiry day whenever any position requires active management:
#   - NFO: ITM options (risk of assignment/STT trap) or open futures
#     (must be rolled or closed before expiry)
#   - MCX: any open position (broker auto-settles at a potentially
#     unfavourable price if not manually closed)

_EXPIRY_RISK_AGENT_DEFAULTS = dict(
    schedule="market_hours",
    cooldown_minutes=60,
    actions=[],
    status="active",
    tier="high",
    topic="expiry_warning",
    events=[
        {"channel": "telegram", "enabled": True},
        {"channel": "email",    "enabled": True},
        {"channel": "log",      "enabled": True},
        {"channel": "ntfy",     "enabled": True, "priority": "high"},
    ],
)

_EXPIRY_RISK_AGENTS = [
    {
        "slug": "expiry-nfo-risk-alert",
        "name": "NFO expiry risk — ITM options or open futures",
        "long_name": "when:positions.expiring_today.nfo.(is_itm==1 OR is_future==1)   alert:high/tg+email+ntfy(high)+log   do:notify-only",
        "description": "Fires on expiry day when any NFO position is ITM (option) or is an open future — both require active management.",
        "conditions": {"any": [
            {"op": "==", "scope": "positions.expiring_today.nfo", "metric": "is_itm",    "value": 1.0},
            {"op": "==", "scope": "positions.expiring_today.nfo", "metric": "is_future", "value": 1.0},
        ]},
    },
    {
        "slug": "expiry-mcx-risk-alert",
        "name": "MCX expiry risk — unhedged options or open futures",
        "long_name": "when:positions.expiring_today.mcx_unhedged.pnl>=-inf   alert:high/tg+email+ntfy(high)+log   do:notify-only",
        "description": "Fires on expiry day when any MCX position is unhedged (net exposure) or is a future — broker will auto-settle without manual close.",
        "conditions": {"op": ">=", "scope": "positions.expiring_today.mcx_unhedged", "metric": "pnl", "value": -999999999},
    },
]

for _a in _EXPIRY_RISK_AGENTS:
    for _k, _v in _EXPIRY_RISK_AGENT_DEFAULTS.items():
        _a.setdefault(_k, _v)

BUILTIN_AGENTS.extend(_EXPIRY_RISK_AGENTS)


# ── Market open/close info agents ────────────────────────────────────
#
# Notify-only agents that fire at exact market open/close times.
# schedule="always" because these must fire at off-peak hours too
# (market open is outside market_hours gate at 09:15 IST). The
# cooldown_minutes=1320 (22 hours) ensures at most one fire per day.
# Condition is a perpetually-true avail_margin >= -999999999 so the
# evaluator always returns a match when the fire_at_time window is open.

_INFO_AGENT_DEFAULTS = dict(
    schedule="always",
    cooldown_minutes=1320,
    actions=[],
    status="active",
    tier="info",
    topic="market_status",
    events=[
        {"channel": "telegram", "enabled": True},
        {"channel": "log",      "enabled": True},
        {"channel": "ntfy",     "enabled": True, "priority": "default"},
    ],
)

_INFO_AGENTS = [
    {
        "slug": "market-open-nse",
        "name": "NSE market open",
        "long_name": "when:fire_at=09:15   alert:info/tg+ntfy(default)+log   do:notify-only",
        "description": "Fires once at NSE open (09:15 IST) on trading days.",
        "fire_at_time": "09:15",
        "conditions": {"op": ">=", "scope": "funds.any_acct", "metric": "avail_margin", "value": -999999999},
    },
    {
        "slug": "market-preclose-mcx",
        "name": "MCX pre-close",
        "long_name": "when:fire_at=23:00   alert:info/tg+ntfy(default)+log   do:notify-only",
        "description": "Fires 30 minutes before MCX close (23:00 IST) on trading days.",
        "fire_at_time": "23:00",
        "conditions": {"op": ">=", "scope": "funds.any_acct", "metric": "avail_margin", "value": -999999999},
    },
]

for _a in _INFO_AGENTS:
    for _k, _v in _INFO_AGENT_DEFAULTS.items():
        _a.setdefault(_k, _v)

BUILTIN_AGENTS.extend(_INFO_AGENTS)


# ── Market status report agent (Item 3, 2026-10-22) ─────────────────
#
# Reports NSE/MCX open-or-closed status, hours when open, closure reason
# when closed, at 05:35 IST daily. `_task_performance`'s loop (the only
# caller of `run_cycle()`) does `if not open_segments: continue` — it
# skips agent evaluation entirely whenever no `_get_segments()` segment
# is open per THAT function's own gate, which uses the broad
# exchange_schedule hours (08:00 open), NOT each segment's real
# 09:00/09:15 session start. `market-open-nse` (fire_at_time=09:15) and
# `market-preclose-mcx` (23:00) both fire because 09:15/23:00 are already
# PAST that 08:00 gate — not because either time literally coincides
# with MCX/NSE's own session boundary. 05:35 IST is still BEFORE that
# 08:00 gate on every day, open or closed, so it is unconditionally true
# at 05:35 regardless of market state. No `fire_at_time` choice inside
# the normal agent-engine trigger mechanism can satisfy "fires whether
# the market is open or closed" (a fully closed day never calls
# `run_cycle` at all — the 08:00 gate never opens that day either). So
# this agent's real
# trigger is a dedicated background tick,
# `background.py:_ds_tick_market_status_report` (runs from
# `_task_daily_snapshot`'s own unconditional 30s loop, which is NOT
# gated on any segment being open) — NOT `run_cycle`/BUILTIN_AGENTS
# evaluation. This row exists purely so the report shows up on the
# /agents page and the operator can pause it via the ordinary active/
# inactive status toggle (`background._market_status_agent_is_active`
# reads this row's `status` column before firing). `schedule="never"`
# makes that explicit — `run_cycle` always skips a `schedule="never"`
# agent (see `_cycle_should_skip_schedule`), so `conditions`/
# `fire_at_time` below are informational metadata only, not a real
# trigger. The actual telegram-vs-ntfy routing (ntfy only when at least
# one segment is open today) is content-conditional — something the
# generic `events` channel fan-out can't express — so delivery is
# entirely inside the `send_market_status` action handler
# (`actions_live._action_live_send_market_status`), called directly by
# the background tick. `events` here deliberately lists ONLY the `log`
# channel; it is never actually consulted by the real trigger path, and
# including telegram/ntfy here would be actively misleading (they are
# NOT what fires).
_MARKET_STATUS_AGENTS = [
    {
        "slug": "market-status-report",
        "name": "Daily market status report",
        "long_name": "when:fire_at=05:35(bg-tick, not run_cycle)   alert:info/log   do:send_market_status",
        "description": (
            "Fires at 05:35 IST daily via a dedicated background tick (not "
            "run_cycle — no fire_at_time inside the agent engine can run "
            "before any segment opens) — reports NSE/MCX open-or-closed "
            "status, hours when open, closure reason when closed. Telegram "
            "always (open or closed); ntfy only when at least one segment "
            "is open today. Pause/resume via this row's status toggle."
        ),
        "fire_at_time": "05:35",
        "schedule": "never",
        "conditions": {"op": ">=", "scope": "funds.any_acct", "metric": "avail_margin", "value": -999999999},
        "actions": [{"type": "send_market_status"}],
        "events": [{"channel": "log", "enabled": True}],
    },
]

for _a in _MARKET_STATUS_AGENTS:
    for _k, _v in _INFO_AGENT_DEFAULTS.items():
        _a.setdefault(_k, _v)

BUILTIN_AGENTS.extend(_MARKET_STATUS_AGENTS)


# ── Manual agent: every operator-initiated order submit fires under
#    this slug so the audit trail (/agents Events tab + agent_events
#    table) shows manual + automated fires in one consistent stream.
#    No condition (null = doesn't run in run_cycle), no cooldown
#    (operator clicks are not throttled).
MANUAL_AGENT = dict(
    slug="manual",
    long_name="when:manual(operator-order)   alert:log-only   do:audit-trail",
    name="Manual operator order",
    description="Every order placed manually via ticket / chain / command writes an event here. No automated triggering.",
    conditions=None,
    events=[
        {"channel": "telegram", "enabled": True},
        {"channel": "email",    "enabled": True},
        {"channel": "log",      "enabled": True},
        {"channel": "ntfy",     "enabled": True},
    ],
    actions=[],
    scope="manual",
    schedule="never",          # never picked up by run_cycle
    cooldown_minutes=0,
    status="active",
)
BUILTIN_AGENTS.append(MANUAL_AGENT)


_SEED_SHIPS_INACTIVE_PHRASE = "Ships INACTIVE"


def _ae_seed_ships_inactive(agent_def: dict) -> bool:
    """True when a seed dict's own description explicitly declares it
    'Ships INACTIVE' (the safety-critical category — see
    `_ae_guard_seed_status` / `_ae_sync_existing_builtin`). Single SSOT
    for the literal phrase check so the insert-time guard and the
    existing-row sync decision can never drift apart (2026-10 fix)."""
    description = agent_def.get("description", "") or ""
    return _SEED_SHIPS_INACTIVE_PHRASE in description


def _ae_guard_seed_status(agent_def: dict, default: str | None = "active") -> str | None:
    """Fail-safe guard: a seed dict whose own description states 'Ships
    INACTIVE' must never actually be seeded (or re-synced) with
    status='active'. Catches the class of bug where a seed's description
    text contradicts its status field (e.g. a destructive auto-close agent
    accidentally left status='active' despite its own docs promising
    otherwise). Forces the effective status to 'inactive' and logs ERROR
    naming the slug — loud, but fail-safe rather than fail-loud: a startup
    crash over a seed-description mismatch would be worse than silently
    (from the operator's perspective) correcting it.

    `default` mirrors whatever fallback the caller would otherwise apply
    to a missing `status` key (``"active"`` for the insert path in
    `_ae_build_agent_row`, ``None`` for the sync path in
    `_ae_sync_existing_builtin`, which no-ops on a falsy desired status).
    """
    status = agent_def.get("status", default)
    if status == "active" and _ae_seed_ships_inactive(agent_def):
        logger.error(
            "Agent engine: seed '%s' has status='active' but its own "
            "description says '%s' — forcing status='inactive'. Fix the "
            "seed dict in agent_engine.py.",
            agent_def.get("slug", "<unknown>"), _SEED_SHIPS_INACTIVE_PHRASE,
        )
        return "inactive"
    return status


def _ae_sync_builtin_status(existing, desired: str | None) -> None:
    """Bidirectionally sync status on a built-in Agent row.

    Only flips active↔inactive; ignores other states.
    Extracted from _ae_sync_existing_builtin to reduce CC there.

    Callers MUST gate this on `_ae_seed_ships_inactive(agent_def)` first
    (2026-10 fix) — this function itself still force-syncs unconditionally
    whenever called, which is exactly the safety-critical "Ships INACTIVE"
    behavior; it must never be called for an ordinary builtin on an
    existing row, or an operator's enable/disable choice gets silently
    reverted on every deploy."""
    if not desired or existing.status == desired:
        return
    if desired == "active" and existing.status == "inactive":
        existing.status = "active"
    elif desired == "inactive" and existing.status == "active":
        existing.status = "inactive"


def _ae_has_pnl_leaf(cond: dict | None) -> bool:
    """True when the condition tree contains any leaf with metric in ('pnl', 'pnl_pct')."""
    if not isinstance(cond, dict):
        return False
    for key in ('all', 'any'):
        if key in cond:
            if any(_ae_has_pnl_leaf(c) for c in (cond[key] or [])):
                return True
    if 'not' in cond:
        return _ae_has_pnl_leaf(cond['not'])
    return (cond.get('metric', '') or '') in ('pnl', 'pnl_pct')


def _ae_should_reset_conditions(existing_cond: dict | None, code_cond: dict | None) -> bool:
    """True when DB conditions have a stale 'pnl'/'pnl_pct' leaf AND code no longer does.

    The two-sided check is critical: agents that legitimately keep 'pnl' in their
    Python default (e.g. loss-pos-total-auto-close) are NOT reset — only agents
    where the code intentionally migrated away from unrealized-P&L metrics.
    """
    return _ae_has_pnl_leaf(existing_cond) and not _ae_has_pnl_leaf(code_cond)


def _ae_sync_existing_builtin(existing, agent_def: dict) -> None:
    """Force-sync mutable fields on an existing system Agent row.

    Operator-editable fields (conditions, cooldown, actions) are left
    untouched EXCEPT when stale ``pnl``/``pnl_pct`` leaves are detected
    (one-time day-P&L metric migration via ``_ae_should_reset_conditions``).
    Extracted from seed_agents to reduce CC there.

    `status` sync (2026-10 fix): only force-synced for the safety-critical
    "Ships INACTIVE" category (`_ae_seed_ships_inactive`) — these must
    always revert to the seed's effective status (always 'inactive' once
    `_ae_guard_seed_status` runs) on every restart, no operator override
    possible, by design (destructive/broker-touching actions). Every OTHER
    builtin agent's `status` is left completely untouched here — the seed
    value is a one-time DEFAULT applied only at first insert
    (`_ae_build_agent_row`); re-enforcing it on every process restart was
    silently reverting an operator's enable/disable choice made from
    /agents (both directions — activating a default-off agent, or
    deactivating a default-on one)."""
    code_long = agent_def.get("long_name")
    if code_long and existing.long_name != code_long:
        existing.long_name = code_long
    if existing.schedule != agent_def.get("schedule", "market_hours"):
        existing.schedule = agent_def.get("schedule", "market_hours")
    # Sync tier + topic only when the row is still at schema defaults.
    _def_tier = agent_def.get("tier", "medium")
    if existing.tier == "medium" and _def_tier != "medium":
        existing.tier = _def_tier
    _def_topic = agent_def.get("topic", "general")
    if existing.topic == "general" and _def_topic != "general":
        existing.topic = _def_topic
    if _ae_seed_ships_inactive(agent_def):
        _ae_sync_builtin_status(existing, _ae_guard_seed_status(agent_def, default=None))
    # Additive-sync events: add any default channel missing from the stored events.
    # Never removes channels the operator may have added manually.
    code_events = agent_def.get("events", [])
    stored_channels = {e["channel"] for e in (existing.events or [])}
    missing = [e for e in code_events if e["channel"] not in stored_channels]
    if missing:
        existing.events = list(existing.events or []) + missing
    new_fire_at_time = agent_def.get("fire_at_time")
    if existing.fire_at_time != new_fire_at_time:
        existing.fire_at_time = new_fire_at_time
        # Reset cooldown so the agent fires promptly at the new time
        # instead of being silenced by a stale last_fired timestamp
        # from the old schedule.
        existing.last_fired = None
    # Force-reset conditions when DB still has stale unrealized-P&L metric leaves.
    code_cond = agent_def.get('conditions')
    if code_cond and _ae_should_reset_conditions(existing.conditions, code_cond):
        existing.conditions = code_cond


def _ae_build_agent_row(agent_def: dict) -> 'Agent':
    """Construct a new system Agent ORM instance from a BUILTIN_AGENTS entry.

    Extracted from seed_agents to isolate the construction block."""
    return Agent(
        slug=agent_def["slug"],
        name=agent_def["name"],
        long_name=agent_def.get("long_name"),
        description=agent_def.get("description", ""),
        conditions=agent_def["conditions"],
        events=agent_def["events"],
        actions=agent_def["actions"],
        scope=agent_def.get("scope", "total"),
        schedule=agent_def.get("schedule", "market_hours"),
        cooldown_minutes=agent_def.get("cooldown_minutes", 30),
        tier=agent_def.get("tier", "medium"),
        topic=agent_def.get("topic", "general"),
        digest_window_sec=agent_def.get("digest_window_sec", 30),
        status=_ae_guard_seed_status(agent_def, default="active"),
        fire_at_time=agent_def.get("fire_at_time"),
        is_system=True,
    )


async def _ae_prune_retired_builtins(session, builtin_slugs: set) -> None:
    """Delete system Agent rows whose slug is no longer in BUILTIN_AGENTS.

    Leaves user-authored (is_system=False) rows untouched. Child
    agent_events rows cascade via ON DELETE CASCADE.
    Extracted from seed_agents to reduce CC there.

    kind != "template" guard (Sprint D, amended Sprint D2 — Agent/
    Bracket/Template unification phase): system Templates (`is_system=
    True` `Agent(kind='template')` rows, seeded directly by
    `seed_agent_templates()` since D2, or backfilled from the now-
    retired `agent_fragments` table by `migrate_and_retire_agent_
    fragments()`) are NOT sourced from BUILTIN_AGENTS at all — without
    this exclusion, this sweep would delete every one of them on the
    very next boot, since none of their slugs ever appear in
    builtin_slugs.

    kind != "bracket" guard (Sprint E, same phase — the exact Sprint D
    landmine recurs, caught by the council-risk review BEFORE dispatch
    this time): system Brackets (`default-bull`/`default-bear`/
    `default-long-option`/`default-short-vol`/`none`, mirrored as
    `Agent(kind='bracket', is_system=True)` rows by
    `bracket_registry.migrate_order_templates_to_bracket_agents()`) are
    likewise never in BUILTIN_AGENTS — same exclusion, same reason."""
    from sqlalchemy import delete as sa_delete
    retired = await session.execute(
        select(Agent).where(
            Agent.is_system.is_(True),
            Agent.kind != "template",
            Agent.kind != "bracket",
        )
    )
    for row in retired.scalars().all():
        if row.slug not in builtin_slugs:
            logger.info(f"Agent engine: pruning retired built-in '{row.slug}'")
            await session.execute(sa_delete(Agent).where(Agent.id == row.id))


async def seed_agents():
    """
    Sync BUILTIN_AGENTS into the `agents` table.

    - Insert system agents that don't exist yet.
    - For existing system rows, force-sync `schedule` always. `status` is
      force-synced ONLY for the safety-critical "Ships INACTIVE" category
      (`_ae_seed_ships_inactive`) — every other builtin's operator-set
      status survives restarts untouched; the seed value is a one-time
      default applied only at first insert (2026-10 fix). User-tuned
      conditions/cooldown/events/actions are preserved.
    - Delete orphan system rows whose slug is no longer in BUILTIN_AGENTS
      (retired built-ins after the v1→v2 cutover).
    """
    builtin_slugs = {a["slug"] for a in BUILTIN_AGENTS}

    async with async_session() as session:
        for agent_def in BUILTIN_AGENTS:
            result = await session.execute(
                select(Agent).where(Agent.slug == agent_def["slug"])
            )
            existing = result.scalar_one_or_none()
            if existing:
                _ae_sync_existing_builtin(existing, agent_def)
                continue
            session.add(_ae_build_agent_row(agent_def))

        await _ae_prune_retired_builtins(session, builtin_slugs)
        await session.commit()
    logger.info(f"Agent engine: {len(BUILTIN_AGENTS)} built-in agents verified")

    # B5: Validate ntfy configuration at startup so operators see a clear
    # log message if ntfy_topic is missing or ntfy_token is absent on a
    # protected server, rather than silent delivery failures at alert time.
    try:
        from backend.shared.helpers.utils import secrets as _ntfy_secrets
        _ntfy_topic = _ntfy_secrets.get("ntfy_topic")
        _ntfy_token = _ntfy_secrets.get("ntfy_token")
        if not _ntfy_topic:
            logger.warning(
                "Agent engine: ntfy_topic not configured in secrets — "
                "ntfy push alerts will be silently skipped."
            )
        else:
            _ntfy_url = _ntfy_secrets.get("ntfy_url", "https://ntfy.sh")
            if not _ntfy_token:
                logger.info(
                    "Agent engine: ntfy configured (url=%s) — "
                    "no auth token (open server assumed).",
                    _ntfy_url,
                )
            else:
                logger.info(
                    "Agent engine: ntfy configured (url=%s) — "
                    "Bearer token present.",
                    _ntfy_url,
                )
    except Exception as _ntfy_cfg_err:
        logger.warning("Agent engine: ntfy config check failed: %s", _ntfy_cfg_err)


async def migrate_order_action_type_rename() -> None:
    """One-time, idempotent backfill — order/place_order unification,
    Phase A (2026-10, see CLAUDE.md "order/place_order unification" and
    the plan at `~/.claude/plans/purrfect-marinating-pixel.md`).

    Rewrites every stored `Agent.actions` JSON array entry whose
    `"type" == "place_order"` to `"type": "order"` — the canonical name
    the frontend CLI compiler emits going forward. Adapted from
    `template_registry.py:migrate_and_retire_agent_fragments()`'s
    migrate-then-verify-then-report shape, but NOT reused as-is: this
    rewrites values INSIDE an existing JSON array column on every row,
    not a migrate-between-two-tables backfill.

    Backend dispatch tables (`actions.py:BROKER_ACTIONS`/
    `_LIVE_ACTION_HANDLERS`, `actions_paper.py`, `actions_sim.py`) and
    the YAML grammar catalog already accept BOTH `"order"` and
    `"place_order"` identically (added in the same change) — this
    migration is a canonicalisation pass, not a behavioural fix. Safe to
    call on every boot: a second run finds zero rows left to rewrite and
    logs a zero-count summary.

    Deliberately does NOT touch `agent_events.detail` JSON anywhere —
    that table is a frozen historical audit log, not live dispatch data,
    and must never be rewritten.

    Run AFTER `seed_agents()` so any row freshly inserted/re-synced this
    boot is covered by the same scan.
    """
    async with async_session() as session:
        result = await session.execute(select(Agent))
        rows = list(result.scalars().all())

        rows_changed = 0
        actions_changed = 0
        for row in rows:
            actions = row.actions
            if not isinstance(actions, list) or not actions:
                continue
            changed_this_row = False
            new_actions = []
            for action in actions:
                if isinstance(action, dict) and action.get("type") == "place_order":
                    new_action = dict(action)
                    new_action["type"] = "order"
                    new_actions.append(new_action)
                    changed_this_row = True
                    actions_changed += 1
                else:
                    new_actions.append(action)
            if changed_this_row:
                # Reassignment (not in-place list mutation) — this is
                # what SQLAlchemy's change-tracking actually detects on
                # a plain (non-Mutable-wrapped) JSONB column.
                row.actions = new_actions
                rows_changed += 1

        if rows_changed:
            await session.commit()

        # Verify — re-query and confirm zero rows still carry the literal
        # string "place_order" as a `type` value inside `actions`.
        verify_result = await session.execute(select(Agent))
        residual = [
            r.slug for r in verify_result.scalars().all()
            if isinstance(r.actions, list) and any(
                isinstance(a, dict) and a.get("type") == "place_order"
                for a in r.actions
            )
        ]

    if residual:
        logger.critical(
            f"order/place_order migration: {len(residual)} agent row(s) "
            f"still carry 'place_order' after backfill — manual review "
            f"needed: {residual}"
        )
    else:
        logger.info(
            f"order/place_order migration: rewrote {actions_changed} "
            f"action(s) across {rows_changed} agent row(s); 0 residual "
            f"'place_order' references"
        )


def _ae_pick_window(windows: list, t):
    """Given a list of ``(start, end)`` dtime tuples, return the one
    containing ``t``; else the most recently-ended one (needed for
    post-close minutes-since-close bookkeeping); else the first.

    Pure, no I/O. Shared by ``_ae_resolve_matched_window`` (regular
    sessions, parsed from config via ``_parse_session_windows``) and
    ``_ae_segment_flags``'s special-session branch (2026-10-20 fix,
    raw dtime tuples straight from the already-fetched
    ``fetch_special_sessions()`` row — no round-trip through
    ``"HH:MM"`` strings needed)."""
    for w_start, w_end in windows:
        if w_start <= t <= w_end:
            return w_start, w_end
    past = [w for w in windows if w[1] <= t]
    return past[-1] if past else windows[0]


def _ae_resolve_matched_window(sessions, legacy_start, legacy_end, t):
    """Resolve the session ``(start, end)`` window that governs clock
    time ``t``. Prefers the window actually containing ``t``; falls back
    to the most recently-ended window (needed for post-close
    minutes-since-close bookkeeping), else the first configured window.

    Pure, no I/O — delegates window parsing to the same
    ``_parse_session_windows`` the canonical ``is_market_open`` uses, so
    a segment's ``sessions`` list (or its legacy ``hours_start``/
    ``hours_end`` pair when ``sessions`` is absent) resolves identically
    here and there."""
    from backend.shared.helpers.date_time_utils import _parse_session_windows
    windows = _parse_session_windows(sessions, legacy_start, legacy_end)
    return _ae_pick_window(windows, t)


def _ae_special_sessions_today(special: "list | None", today) -> "list | None":
    """Return the list of ``(start, end)`` dtime tuples from ``special``
    (the already-fetched ``fetch_special_sessions()`` result) whose
    ``date`` field equals ``today``, or ``None`` when no row applies
    today.

    A non-``None`` result (even one containing only zero-width rows)
    signals ``_ae_segment_flags`` that a special-session override is in
    effect today and must supersede the evening-holiday approximation
    entirely — mirroring ``is_market_open()``'s own precedence
    (special sessions > holiday > regular sessions). Pure, no I/O."""
    if not special:
        return None
    rows_today = [s for s in special if s.get("date") == today]
    if not rows_today:
        return None
    return [(s["start"], s["end"]) for s in rows_today]


def _ae_had_session_today(now, is_holiday: bool, evening_open: bool) -> bool:
    """True when a real session ran (or is running) TODAY — calendar-only,
    no live probe. Deliberately does not consult the Kite bellwether
    probe (`is_trading_day`'s job): the probe only ever upgrades "is this
    instant open right now", it has no bearing on "did a session already
    end earlier today." An operator-declared extra trading day (e.g. a
    Muhurat Saturday) always counts, mirroring `is_trading_day`'s own
    precedence (extra_trading_days checked first)."""
    from backend.shared.helpers.date_time_utils import _parse_extra_trading_days
    if now.date() in _parse_extra_trading_days():
        return True
    if is_holiday:
        return bool(evening_open and now.weekday() < 5)
    return now.weekday() < 5


def _ae_minutes_until(is_open: bool, had_session_today: bool, now,
                       open_time, close_time) -> tuple:
    """Return ``(minutes_until_open, minutes_until_close)`` for one
    segment — new computed values, unlike `minutes_since_{prefix}_open`/
    `_close` above (those reuse pre-existing fields). Extracted to its
    own function to keep `_ae_segment_flags` under the radon complexity
    ceiling (2026-10-22, market-cycle event tokens).

    ``None`` (missing, never a coerced 0/1e9 sentinel) when the value
    isn't meaningful right now:
      - minutes_until_open  — 0 once open; counts down to `open_time`
        while before it on a day with a session; None once past today's
        close or on a no-session day (no more "today" open to count to).
      - minutes_until_close — counts down to `close_time` while open;
        None while closed (nothing to count down to).
    """
    if is_open:
        mins_until_open = 0
        mins_until_close = max(0, int((close_time - now).total_seconds() / 60))
    else:
        mins_until_close = None
        if had_session_today and now < open_time:
            mins_until_open = max(0, int((open_time - now).total_seconds() / 60))
        else:
            mins_until_open = None
    return mins_until_open, mins_until_close


def _ae_segment_flags(seg_name: str, seg_cfg: dict, now) -> dict:
    """Compute open/closed/holiday/minutes_since flags for one market segment.

    Routed through the canonical ``is_market_open()`` — the same
    mechanism `/api/market/status`, the quote gate, and the watchlist
    movers gate use via `_segment_is_open()` — instead of a blind
    `hours_start`/`hours_end` window comparison. Fixes a live incident
    (2026-10-20): on a calendar-holiday date, the old comparison's ONLY
    path to ever read "open" was `is_trading_day()`'s live Kite
    bellwether-probe fallback; a segment with `evening_open_on_holidays:
    true` (e.g. MCX) that was genuinely open in its 17:00+ evening
    session was rejected as closed whenever the probe didn't fire a
    confirming tick in time. Known residual gap from this fix, noted for
    the record: on a plain weekday holiday with no `evening_open_on_
    holidays` override, this function no longer falls through to the
    live probe at all (the old code's second, independent
    `is_trading_day()` call — now removed, see `_ae_had_session_today`
    below — was the only thing that ever gave the probe a say here); an
    ad-hoc SEBI-announced session on such a date would not be picked up
    by this function. `is_any_segment_open()` / `_segment_is_open()`
    callers are unaffected (they never consulted the probe from this
    path either).

    ``special_sessions`` IS wired in (2026-10-20, Step 2) via
    `fetch_special_sessions()` — see that function's own docstring in
    `broker_apis.py` for the residual cold-cache-on-main-loop gap this
    still carries for the async call sites that reach this function from
    a coroutine already running on the main event loop.

    Returns a flat dict with the ``{prefix}_*`` keys ready to merge into
    the top-level context. Extracted from _build_context to reduce CC there."""
    from datetime import time as _dtime
    from backend.brokers.broker_apis import fetch_holidays, fetch_special_sessions
    from backend.shared.helpers.date_time_utils import is_market_open

    prefix = "nse" if seg_name == "equity" else "mcx"
    holiday_exchange = seg_cfg.get("holiday_exchange", "NSE")
    sessions = seg_cfg.get("sessions") or None
    evening_open = bool(seg_cfg.get("evening_open_on_holidays", False))

    h, m = map(int, seg_cfg.get("hours_start", "09:15").split(":"))
    legacy_start = _dtime(h, m)
    h, m = map(int, seg_cfg.get("hours_end", "15:30").split(":"))
    legacy_end = _dtime(h, m)

    try:
        holidays = fetch_holidays(holiday_exchange)
    except Exception:
        holidays = set()
    try:
        special = fetch_special_sessions(holiday_exchange)
    except Exception:
        special = []

    is_holiday = now.date() in holidays
    t = now.time().replace(second=0, microsecond=0)

    is_open = is_market_open(
        now, holidays, legacy_start, legacy_end, exchange=holiday_exchange,
        sessions=sessions, evening_open_on_holidays=evening_open,
        special_sessions=special,
    )

    # Special-session override precedence (2026-10-20 fix). When a DB
    # row applies today, it supersedes the evening-holiday approximation
    # below entirely — resolve win_start/win_end FROM THE OVERRIDE'S OWN
    # dtime values directly (`is_open` itself is already correct via the
    # untouched `is_market_open()` call above; only the matched-window /
    # had-session-today bookkeeping needs to follow suit). Three
    # confirmed-wrong cases this fixes:
    #   1. A zero-width (start==end) override means no session ran today
    #      at all — `had_session_today` must be False, not derived from
    #      `evening_open_on_holidays`, else `{prefix}_closed` reports
    #      True alongside `{prefix}_open=False` (neither should be true).
    #   2. A real override window (e.g. an 18:15-19:15 Muhurat session)
    #      must anchor `minutes_since_{prefix}_open` to ITS OWN start,
    #      not the regular session's 09:15 start.
    #   3. A real override window ending AFTER the regular session's own
    #      close (e.g. 16:00-19:00 on an otherwise normal weekday) must
    #      anchor `{prefix}_closed` to ITS OWN end, not the regular
    #      15:30 close — else both `{prefix}_open` and `{prefix}_closed`
    #      report True simultaneously while still inside the override
    #      window.
    special_today = _ae_special_sessions_today(special, now.date())
    if special_today is not None:
        non_zero = [w for w in special_today if w[0] != w[1]]
        had_session_today = bool(non_zero)
        win_start, win_end = _ae_pick_window(non_zero or special_today, t)
        open_time  = now.replace(hour=win_start.hour, minute=win_start.minute, second=0, microsecond=0)
        close_time = now.replace(hour=win_end.hour,   minute=win_end.minute,   second=0, microsecond=0)
    else:
        win_start, win_end = _ae_resolve_matched_window(sessions, legacy_start, legacy_end, t)
        open_time  = now.replace(hour=win_start.hour, minute=win_start.minute, second=0, microsecond=0)
        close_time = now.replace(hour=win_end.hour,   minute=win_end.minute,   second=0, microsecond=0)

        # Evening-holiday open anchor — the matched window may start at
        # 09:00, but trading only actually began at 17:00 on a holiday
        # evening session; anchoring "minutes since open" to the window
        # start would report ~540 min at 18:00 instead of ~60.
        #
        # Deliberately NOT gated on `is_open` (unlike the original
        # 2026-10-20 version of this block) — `mins_open` below still
        # only USES `open_time` inside its own `if is_open` branch, so
        # that field's value is unaffected either way. Gating removed so
        # `had_session_today` and the new minutes_until_{prefix}_open
        # calc (below) see the correct 17:00 anchor even BEFORE the
        # evening session actually starts (e.g. 10:00 IST on an MCX
        # holiday) — without this, minutes_until_open would measure
        # against the regular 09:00 window start instead of 17:00.
        if is_holiday and evening_open:
            evening_anchor = now.replace(hour=17, minute=0, second=0, microsecond=0)
            if evening_anchor > open_time:
                open_time = evening_anchor

        had_session_today = _ae_had_session_today(now, is_holiday, evening_open)

    mins_open  = (max(0, int((now - open_time).total_seconds() / 60))
                  if is_open else 0)
    mins_close = (max(0, int((now - close_time).total_seconds() / 60))
                  if now > close_time and had_session_today else 0)
    mins_until_open, mins_until_close = _ae_minutes_until(
        is_open, had_session_today, now, open_time, close_time
    )
    return {
        f"{prefix}_open":    is_open,
        f"{prefix}_closed":  (now > close_time) and had_session_today,
        f"{prefix}_holiday": is_holiday,
        f"minutes_since_{prefix}_open":  mins_open,
        f"minutes_since_{prefix}_close": mins_close,
        f"minutes_until_{prefix}_open":  mins_until_open,
        f"minutes_until_{prefix}_close": mins_until_close,
    }


def _build_context(now, sim_overrides: dict | None = None) -> dict:
    """
    Build the base context dict consumed by the schedule/market-open check
    in run_cycle. The v2 grammar engine reads the market DataFrames directly
    via V2Context, so this function's main job is emitting the per-segment
    open/close flags used to short-circuit `market_hours` agents — but
    since 2026-10-22 the full dict is ALSO threaded into `run_cycle`'s
    `context["segment_flags"]` and handed to V2Context, making it the live
    source for the `is_mcx_open`/`is_nse_open`/`minutes_since_*_open`/
    `minutes_until_*_open`/`minutes_until_*_close` grammar tokens
    (`grammar.py`'s market-cycle-event resolvers).

    `sim_overrides` (optional) is the simulator's way to pretend the clock
    is somewhere it isn't. When non-None, keys in the override dict win
    over the computed values — so a scenario can declare "NSE is open, 30
    minutes before close, today is an expiry day" regardless of wall-clock
    time. Expected keys:

        nse_open / nse_closed / nse_holiday / mcx_open / mcx_closed / mcx_holiday (bool)
        minutes_since_nse_open / minutes_since_nse_close
        minutes_since_mcx_open / minutes_since_mcx_close   (int)
        minutes_until_nse_open / minutes_until_nse_close
        minutes_until_mcx_open / minutes_until_mcx_close   (int | None)
        is_expiry_day       (bool, reserved — expiry agents read it directly)

    The real path passes None and we fall through to the live computation.
    """
    from backend.shared.helpers.utils import config as app_config

    ctx: dict = {"now": now}

    segments = app_config.get("market_segments", {})
    for seg_name, seg_cfg in segments.items():
        ctx.update(_ae_segment_flags(seg_name, seg_cfg, now))

    # Sim-mode overrides — a scenario's `market_state` block wins over the
    # computed values above. Only keys present in the override dict are
    # replaced, so a partial override (e.g. just `is_expiry_day`) is safe.
    if sim_overrides:
        ctx.update(sim_overrides)

    return ctx


# ─── run_cycle gate helpers ────────────────────────────────────────────────
# Pure-boolean helpers extracted from run_cycle to reduce its cyclomatic
# complexity. Each returns True when the agent SHOULD be skipped on the
# current tick. No mutation of agent state; no DB calls.

def _cycle_should_skip_schedule(agent, *, any_market_open: bool,
                                bypass_schedule: bool) -> bool:
    """True when the agent should be skipped due to its schedule setting.

    schedule='never' → always skip.
    schedule='market_hours' → skip when no market is open (unless bypassed).
    """
    if agent.schedule == "never":
        return True
    if (not bypass_schedule
            and agent.schedule == "market_hours"
            and not any_market_open):
        return True
    return False


def _cycle_in_cooldown(agent, *, bypass_schedule: bool) -> bool:
    """True when the agent is in cooldown and the window has not elapsed."""
    if agent.status != "cooldown" or bypass_schedule:
        return False
    if not agent.last_triggered_at:
        return False
    elapsed = (datetime.now(timezone.utc) - agent.last_triggered_at).total_seconds() / 60
    return elapsed < agent.cooldown_minutes


def _cycle_outside_fire_at(agent, now, *, bypass_schedule: bool) -> bool:
    """True when fire_at_time is set and the current time is outside its window."""
    if bypass_schedule or not getattr(agent, "fire_at_time", None):
        return False
    from backend.shared.helpers.settings import get_int
    window_sec = int(get_int('alerts.fire_at_window_sec', 360))
    return not _fire_at_window_active(agent.fire_at_time, now, window_sec=window_sec)


def _cycle_in_blackout(agent, now, *, bypass_schedule: bool) -> bool:
    """True when the current time falls inside a configured blackout window."""
    if bypass_schedule:
        return False
    blackouts = getattr(agent, "blackout_windows", None) or []
    return bool(blackouts and _in_blackout_window(now, blackouts))


async def _cycle_maybe_expire_lifespan(agent, now, *, bypass_schedule: bool,
                                       broadcast_fn) -> bool:
    """Auto-complete until_date agents whose expiry has passed.

    Returns True when the agent was completed (caller should `continue`).
    Skipped entirely in sim runs (bypass_schedule=True) so the simulator
    never mutates real DB state.
    """
    if bypass_schedule:
        return False
    if (getattr(agent, "lifespan_type", "persistent") != "until_date"
            or not agent.lifespan_expires_at
            or now < agent.lifespan_expires_at):
        return False
    async with async_session() as session:
        await session.execute(
            update(Agent).where(Agent.id == agent.id).values(status="completed")
        )
        await session.commit()
    if broadcast_fn:
        broadcast_fn("agent_state", {"slug": agent.slug, "status": "completed"})
    return True


async def _cycle_persist_untriggered_state(
    agent,
    *,
    debounce_first_true_changed: bool,
    debounce_new_first_true_at,
    broadcast_fn,
) -> None:
    """Persist non-triggered state transitions to the DB.

    Two cases:
      1. Agent was in cooldown but didn't fire → transition back to active.
      2. Debounce latch changed (armed or cleared) without a fire → write the
         new condition_first_true_at so a process restart doesn't reset it.
    """
    if agent.status == "cooldown":
        async with async_session() as session:
            await session.execute(
                update(Agent).where(Agent.id == agent.id).values(status="active")
            )
            await session.commit()
        if broadcast_fn:
            broadcast_fn("agent_state", {"slug": agent.slug, "status": "active"})
    elif debounce_first_true_changed:
        async with async_session() as session:
            await session.execute(
                update(Agent).where(Agent.id == agent.id).values(
                    condition_first_true_at=debounce_new_first_true_at
                )
            )
            await session.commit()


async def _cycle_load_agents(only_agent_ids: list[int] | None) -> list:
    """Load the agent rows to evaluate this tick.

    Three semantics for only_agent_ids:
      None       → every active / cooldown agent (live path)
      [id1, id2] → only those agents, regardless of DB status (simulator)
      []         → no agents (market-scenario explorer)
    """
    async with async_session() as session:
        if only_agent_ids is not None:
            if not only_agent_ids:
                return []
            result = await session.execute(
                select(Agent).where(Agent.id.in_(only_agent_ids), Agent.kind == "cycle")
            )
            return list(result.scalars().all())
        result = await session.execute(
            select(Agent).where(Agent.status.in_(["active", "cooldown"]), Agent.kind == "cycle")
        )
        return list(result.scalars().all())


# ═══════════════════════════════════════════════════════════════════════════
#  GIVEN server-evaluated bindings (RamboQL Part 2, 2026-10)
# ═══════════════════════════════════════════════════════════════════════════
#
# `agent.given` (a list of `{"name": str, "expr": str}` dicts, in
# declaration order) is a NEW, backend-defensive-read-only key the
# frontend CLI compiler produces for an OPERATOR-BASED GIVEN binding
# (anything involving `+`/`-`/`*`/`/`/`%`/`**`, or referencing an earlier
# GIVEN binding by name). A PURE-ALIAS binding (bare `metric@scope`, no
# operator, no reference to another binding) never reaches this path —
# it still compiles to today's client-side AST substitution inside
# WHEN's own condition tree, unchanged.
#
# `agent.given` does not exist as a column on the `Agent` ORM model yet
# (that frontend compiler change is separate, parallel work) — read
# defensively via `getattr(agent, 'given', None) or []` so this whole
# step is a true no-op until that lands and for every agent that never
# uses GIVEN at all.
_GIVEN_BARE_METRIC_SCOPE_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)@([A-Za-z_][A-Za-z0-9_.]*)$"
)


class _GivenUnresolvedSentinel:
    """Marks a GIVEN binding whose value could not be resolved this tick
    (per-row-scope Phase-1 limitation, unknown metric/scope, or a scope-
    selector/metric-resolver runtime failure) — the warning is already
    logged at the point this sentinel is returned; the caller simply
    leaves the binding's name OUT of the bindings dict rather than
    storing `None`, so a later binding referencing it by name fails with
    `expr_eval`'s own "undefined name" `ExprError` (handled the same way
    as any other GIVEN-evaluation failure), never silently treated as a
    real value."""


_GIVEN_UNRESOLVED = _GivenUnresolvedSentinel()


def _given_binding_bare_metric_scope(expr: str) -> "re.Match | None":
    """True iff `expr` (after stripping whitespace) is EXACTLY a bare
    `metric@scope` reference — no operator character anywhere (the
    fullmatch itself enforces this: a string containing `+ - * / % ( ) "
    '` etc. cannot match this identifier-only pattern). `scope` may
    itself contain dots (e.g. `positions.total`)."""
    return _GIVEN_BARE_METRIC_SCOPE_RE.fullmatch(expr.strip())


def _given_resolve_bare_metric_scope(match: "re.Match", v2_ctx, agent_slug: str):
    """Resolve a bare `metric@scope` GIVEN binding using the EXISTING
    scope-selector + metric-resolver machinery `agent_evaluator._eval_leaf`
    already uses (`REGISTRY.scope()` / `_resolve_metric_fn()`) —
    restricted to AGGREGATE scopes only (Phase 1): a scope that selects
    more than one row this tick has no single well-defined value, so
    that case logs a warning and returns `_GIVEN_UNRESOLVED` rather than
    guessing which row's value to use or crashing. A scope that selects
    zero rows (data not loaded yet this tick) resolves to `None` — same
    "missing, not a bug" treatment every other metric resolver in this
    codebase already uses.

    Known limitation, not a new risk introduced by this feature: several
    aggregate metrics are `.fillna(0)` upstream (the background summary
    builder that produces `sum_positions`/`df_margins`), so a degraded/
    partial fetch can render as a real `0.0` here rather than `None` —
    a GIVEN-computed ALERT message built from that value could then
    read e.g. "Loss: 0" instead of genuinely reflecting missing data.
    This is the exact same upstream behavior every other metric/scope
    leaf in this codebase already lives with; GIVEN just makes it
    visible in prose (a computed message) rather than only a number on
    a grid row.
    """
    metric_tok, scope_tok = match.group(1), match.group(2)
    scope_fn = REGISTRY.scope(scope_tok)
    metric_fn = _resolve_metric_fn(metric_tok, scope_tok)
    if scope_fn is None or metric_fn is None:
        logger.warning(
            f"Agent [{agent_slug}]: GIVEN binding references unknown "
            f"metric/scope '{metric_tok}@{scope_tok}'"
        )
        return _GIVEN_UNRESOLVED
    try:
        rows = scope_fn(v2_ctx) or []
    except Exception as e:
        logger.warning(
            f"Agent [{agent_slug}]: GIVEN binding '{metric_tok}@{scope_tok}' "
            f"scope selector failed: {e}"
        )
        return _GIVEN_UNRESOLVED
    if len(rows) > 1:
        logger.warning(
            f"Agent [{agent_slug}]: GIVEN binding '{metric_tok}@{scope_tok}' "
            f"references a per-row scope ({len(rows)} rows selected this "
            f"tick) — unsupported in this phase, skipping this binding"
        )
        return _GIVEN_UNRESOLVED
    if not rows:
        return None
    try:
        return metric_fn(v2_ctx, rows[0])
    except Exception as e:
        logger.warning(
            f"Agent [{agent_slug}]: GIVEN binding metric '{metric_tok}' "
            f"failed on scope '{scope_tok}': {e}"
        )
        return _GIVEN_UNRESOLVED


class _GivenCaseInsensitiveNamespace(dict):
    """`eval_expr` namespace wrapper for GIVEN operator-expression
    bindings — lowercases key lookups so an `expr` string's verbatim
    `Name` reference (rendered exactly as the operator typed it, e.g.
    `P` in `D = P * 2` — the decompiler's "read expr back verbatim"
    rule means this text is never re-cased) resolves against
    `_cycle_evaluate_given_bindings`'s `bindings` dict, which is keyed
    by `name.lower()` (matching the frontend compiler's own lowercase-
    everywhere convention for every REFERENCE to a binding — `$given`
    markers in `parts`/DO-action-params/ALERT-messages are all
    `.toLowerCase()`'d at compile time, while a binding's own `name`
    field in the compiled JSON preserves the operator's original typed
    case). A pure, no-op wrapper for any namespace that's already
    all-lowercase (the common case). Does not modify `expr_eval.py`
    itself — `_eval_name` just does `namespace[node.id]`, which Python
    dispatches to this subclass's own `__getitem__` transparently."""
    def __getitem__(self, key):
        return super().__getitem__(key.lower() if isinstance(key, str) else key)

    def __contains__(self, key):
        return super().__contains__(key.lower() if isinstance(key, str) else key)


def _given_format_part_value(value) -> str:
    """Format an earlier-resolved GIVEN binding's value as a string
    segment for a `parts`-array binding. A `float` with no fractional
    part renders as a plain integer string (no trailing `.0` — e.g.
    `-60000.0` -> `"-60000"`, matching how an operator would expect a
    whole-number P&L to read in a message); everything else uses
    Python's normal `str()`."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _given_resolve_parts(parts: list, bindings: dict, agent_slug: str):
    """Resolve a `parts`-array GIVEN binding WITHOUT ever calling
    `expr_eval.eval_expr`.

    The frontend compiler classifies an operator-expression GIVEN
    binding by a static property: if its arith-AST contains ANY string
    literal anywhere, it compiles to `{"name": ..., "parts": [...]}`
    instead of `{"name": ..., "expr": ...}` — because `eval_expr`'s
    string-concat path is deliberately strict (no implicit str/number
    coercion, since it sits directly upstream of real order placement),
    so a message like `"Loss: " + p` over a NUMERIC metric `p` can never
    evaluate via `eval_expr` at all (confirmed: raises `ExprError`).
    Pure-number-math bindings (no string literal anywhere) keep using
    `expr`/`eval_expr` exactly as before — zero change to that path.

    A "part" is either a plain string (a literal segment, used as-is)
    or `{"$given": "<name>"}` (a reference to an EARLIER-bound name in
    THIS agent's own `bindings` dict — declaration order is enforced by
    the frontend compiler and by this function's own caller, so a
    referenced name is always already resolved by the time it's looked
    up here). Parts are concatenated in order.

    Returns the final concatenated string — RAW, deliberately NOT masked
    here. Masking is applied at the MESSAGE boundary instead (where this
    string is actually about to be used as alert text —
    `events._resolve_given_message`, which both the rich-alert path
    (`_v2_rich_alert_custom_message`) and the ordinary `dispatch()` path
    share), not at storage time: this same `bindings` dict is also what
    `actions.py:resolve_given_ref_params` reads for a `{"$given": name}`
    DO-action param (e.g. a string-typed `tag`/`symbol`-adjacent field)
    — masking the stored value here would corrupt a legitimate, non-
    alert use of an account-shaped string (the compile-time guard
    explicitly allows a `parts`-built value into any string-typed
    param). Never raises: an unrecognized part shape, or a reference to
    a name missing from `bindings` OR resolved to `None` (never
    resolved, or its own resolution failed/returned missing data earlier
    this tick — same missing-vs-zero convention `resolve_given_ref_params`/
    `_resolve_given_message` already use), logs a warning and returns
    `_GIVEN_UNRESOLVED` — same graceful "skip only this binding" contract
    `_cycle_evaluate_given_bindings` already uses for a failed `expr`.
    """
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
            continue
        if isinstance(part, dict) and len(part) == 1 and "$given" in part:
            ref_name_raw = part["$given"]
            # The frontend compiler always lowercases a $given reference
            # (even though a binding's own `name` field preserves the
            # operator's original typed case) — `bindings` is keyed by
            # `name.lower()` for exactly this reason; lowercase here too
            # for defense-in-depth against any other producer of this
            # marker shape.
            ref_name = ref_name_raw.lower() if isinstance(ref_name_raw, str) else ref_name_raw
            if ref_name not in bindings or bindings[ref_name] is None:
                logger.warning(
                    f"Agent [{agent_slug}]: GIVEN parts binding references "
                    f"'{ref_name}', which did not resolve earlier this "
                    f"tick — skipping this binding"
                )
                return _GIVEN_UNRESOLVED
            out.append(_given_format_part_value(bindings[ref_name]))
            continue
        logger.warning(
            f"Agent [{agent_slug}]: GIVEN parts binding has an unrecognized "
            f"part {part!r} — skipping this binding"
        )
        return _GIVEN_UNRESOLVED
    return "".join(out)


def _cycle_evaluate_given_bindings(agent, v2_ctx, context: dict) -> None:
    """Evaluate this agent's `given` bindings (if any) and store the
    result under a PER-AGENT key on the shared `context` dict:
    `context["given"][agent.id]`.

    MUST use a per-agent key, never a bare name directly on `context` —
    `context` is the SAME dict object passed by reference to every
    agent evaluated in this cycle (`run_cycle`'s per-agent loop), so a
    bare name would leak agent A's bindings into agent B's.

    Never raises: `run_cycle`'s own per-agent loop has NO surrounding
    try/except (confirmed — a raised exception here would abort
    evaluation for every subsequent agent this tick), so every failure
    mode (a raised `ExprError`, an unresolved per-row-scope reference,
    any other exception) is caught here, logged, and this agent simply
    ends up with however many bindings it managed to resolve before the
    failure (possibly zero) — never a crash.
    """
    given_list = getattr(agent, "given", None) or []
    if not given_list:
        return
    bindings: dict = {}
    failing_name = "?"
    try:
        for binding in given_list:
            if not isinstance(binding, dict):
                continue
            name = binding.get("name")
            if not name or not isinstance(name, str):
                continue
            failing_name = name
            # Case-normalisation: the frontend compiler emits a binding's
            # OWN `name` field preserving the operator's original typed
            # case, but lowercases EVERY reference to it — `{"$given":
            # name}` markers (parts refs, DO-action params, ALERT message
            # markers) are all `.toLowerCase()`'d at compile time. Storing
            # under `name.lower()` here keeps lookups by any of those
            # lowercased references working regardless of how the
            # operator originally capitalised the binding name.
            name_key = name.lower()
            parts = binding.get("parts")
            expr = binding.get("expr")
            if isinstance(parts, list):
                # String-building binding (contains a string literal
                # anywhere in its source AST, per the frontend's static
                # classification) — resolved WITHOUT eval_expr; see
                # _given_resolve_parts' own docstring for why.
                value = _given_resolve_parts(parts, bindings, agent.slug)
                if value is _GIVEN_UNRESOLVED:
                    continue
            elif isinstance(expr, str):
                bare_match = _given_binding_bare_metric_scope(expr)
                if bare_match is not None:
                    value = _given_resolve_bare_metric_scope(bare_match, v2_ctx, agent.slug)
                    if value is _GIVEN_UNRESOLVED:
                        # Already logged inside the resolver. Leave `name`
                        # OUT of `bindings` — any later binding referencing
                        # it by name fails via eval_expr's own "undefined
                        # name" ExprError, caught below.
                        continue
                else:
                    # Pure-number-math operator expression — namespace is
                    # every earlier-resolved binding in THIS agent's own
                    # dict, real Python values (never text). Wrapped in
                    # `_GivenCaseInsensitiveNamespace` since `expr`'s own
                    # text is rendered verbatim (the decompiler's "read
                    # expr back verbatim" rule) and may reference an
                    # earlier name in ITS original typed case (e.g. `D =
                    # P * 2`), which must still resolve against this
                    # dict's lowercased keys. Never reached for a string-
                    # literal-containing expression — the frontend
                    # compiles those to `parts` instead (see above)
                    # specifically because eval_expr's string-concat path
                    # has no implicit str/number coercion.
                    value = eval_expr(expr, _GivenCaseInsensitiveNamespace(bindings))
            else:
                continue
            bindings[name_key] = value
    except ExprError as e:
        logger.warning(
            f"Agent [{agent.slug}]: GIVEN binding '{failing_name}' failed "
            f"to evaluate — {e}. GIVEN-dependent ALERT/DO fields for this "
            f"agent are treated as unresolved this tick."
        )
    except Exception as e:  # defense-in-depth — never let anything else
        # escape and abort run_cycle() for other agents.
        logger.warning(
            f"Agent [{agent.slug}]: unexpected error evaluating GIVEN "
            f"bindings (last attempted: '{failing_name}') — {e}"
        )
    context.setdefault("given", {})[agent.id] = bindings


# Grammar-unification plan, Design item 0 — saved cycle-agent `preamble`
# execution. See `Agent.preamble` (models.py) for the compiled shape and
# `_cycle_run_preamble`'s own docstring below for the full split.
_PREAMBLE_EVERY_CYCLE_TYPES = frozenset({"log", "help"})


def _cycle_agent_is_preamble_only(agent) -> bool:
    """True when this saved agent is a preamble-only statement — one or
    more calls with NO `WHEN ... DO ...` pair at all (the plan's own
    canonical worked example: `order(...)` alone, nothing else).

    Why this matters: a preamble-only statement's `conditions` field is
    NOT a real breach condition — the frontend compiler emits the same
    always-true sentinel leaf (`avail_margin@funds.any_acct >=
    -999999999`) the existing `market-open-nse`/`market-preclose-mcx`
    scheduled agents use purely to drive a `fire_at_time` gate (see
    `agentsGrammar.test.js`'s "preamble-only statement" fixture). Without
    this guard, that sentinel would match on EVERY tick, and
    `_v2_send_rich_alert()` sends its rich alert log record
    UNCONDITIONALLY on a match — it never checks `agent.events` at all
    (that check only happens afterward, in `dispatch()`, for the
    telegram/email/ntfy channels `_v2_send_rich_alert` itself does NOT
    own) — so a preamble-only agent with no real condition would fire a
    spurious "breach" alert every cooldown window forever, burn
    `lifespan_max_fires`, and flip `status` to `cooldown`/back. Confirmed
    by reading `_v2_send_rich_alert`'s body: it logs
    `extra={"alert_event": "rich_alert", ...}` on every call with no
    channel-membership gate.

    Discriminator (backend-only, no new compiled field needed):
    `preamble` is non-empty AND `actions`/`events` are BOTH empty.
    Every pre-existing agent (predating this plan) has `preamble` NULL/
    absent — the compiled field is only spliced in when non-empty (see
    `compileAgentStmt`) — so this is False for all of them, by
    construction. A statement with a real `WHEN ... DO ...` pair always
    produces a non-empty `actions` or `events` — the compiler itself
    rejects `DO nop` (`DO must have at least one real action or alert`)
    — so this can never misclassify a genuine conditional agent that
    also happens to carry preamble calls (preamble + WHEN/DO coexist;
    only the preamble-ONLY case, no WHEN/DO at all, trips this).

    Type-checked, not just truthy-checked: `agent.preamble` is required
    to be a genuinely non-empty `list` (the real JSONB column's only two
    possible decoded shapes are `None` or a `list` — never anything
    else). A bare truthy check (`bool(getattr(agent, "preamble", None))`)
    mis-fired on a plain `MagicMock()` test double that never explicitly
    sets `.preamble` — Python auto-vivifies an unset Mock attribute into
    a fresh child Mock, which is truthy by default — misclassifying an
    agent with real `actions=[]`/`events=[]` (both empty, as
    `test_agent_suppression.py`'s own low-tier test agent has) as
    preamble-only and silently discarding its real matches. Confirmed
    via a live regression in that file before this fix."""
    preamble = getattr(agent, "preamble", None)
    if not isinstance(preamble, list) or not preamble:
        return False
    return not (getattr(agent, "actions", None) or []) \
        and not (getattr(agent, "events", None) or [])


def _cycle_preamble_has_once_entries(preamble: list) -> bool:
    """True when `preamble` contains at least one entry that belongs to
    the once-per-activation bucket (anything that isn't `log`/`help`/
    `alert` — see `_cycle_run_preamble`). Used to skip the atomic DB
    claim entirely for a preamble made up only of every-cycle entries,
    the common case for a diagnostic-only preamble."""
    for entry in preamble or []:
        if isinstance(entry, dict) and entry.get("type") not in (
            "log", "help", "alert",
        ):
            return True
    return False


def _cycle_preamble_entry_is_cooldown_gated(entry: dict) -> bool:
    """True for a preamble entry that must respect the windowed alert/
    log cooldown (Design item 2): `alert`, and `log` entries whose level
    is NOT `debug`. `help` is never gated (a read-only catalog lookup
    with no notification channel involved — nothing to rate-limit).
    `debug(x)` sugar — which always compiles to `log(level="debug",
    ...)` — is ALSO never gated: Design item 0.B's own rationale for
    running it every cycle ("dynamic state, re-observed each time") and
    the structural DEBUG exemption (a DEBUG-level record can never reach
    a real notification channel regardless) both mean there is no
    notification-spam risk to rate-limit in the first place."""
    if not isinstance(entry, dict):
        return False
    etype = entry.get("type")
    if etype == "alert":
        return True
    if etype == "log":
        return (entry.get("params") or {}).get("level") != "debug"
    return False


def _cycle_preamble_has_cooldown_gated_entries(preamble: list) -> bool:
    """True when `preamble` contains at least one cooldown-gated entry
    (see `_cycle_preamble_entry_is_cooldown_gated`). Used to skip the
    cooldown claim entirely for a preamble with none — e.g. one made up
    only of `help`/`debug(x)` calls."""
    return any(_cycle_preamble_entry_is_cooldown_gated(e) for e in preamble or [])


def _preamble_utcnow() -> "datetime":
    """Patchable wall-clock read, isolated so tests can simulate elapsed
    cooldown windows (t0 -> t0+5m -> t0+31m) without a real sleep."""
    return datetime.now(timezone.utc)


async def _cycle_preamble_alert_cooldown_ready(
    agent, *, alert_state: dict, sim_mode: bool, replay_mode: bool,
    bypass_schedule: bool, bypass_suppression: bool,
) -> bool:
    """Windowed (repeating) cooldown gate for preamble `alert`/non-debug
    `log` entries (Design item 2) — deliberately a SEPARATE mechanism
    from `_cycle_claim_preamble_activation`'s ONE-TIME marker, not a
    reuse of it: that marker means "fired once, never again" (order/
    close_position/etc.); this gate means "fired, then eligible again
    after `cooldown_minutes`" — a genuinely different shape. Sharing one
    column between them would corrupt one or the other (see
    `Agent.preamble_alert_last_fired_at`'s own model-column comment).
    It is also NOT a reuse of `_cycle_in_cooldown` (the ordered-
    condition cooldown gate): that gate returns False outright unless
    `agent.status == "cooldown"`, which a preamble-only agent (see
    `_cycle_agent_is_preamble_only`) never reaches — its own condition-
    fire lifecycle is structurally skipped. Writing to the SHARED
    `last_triggered_at`/`status` fields from here would also wrongly
    silence a MIXED agent's real DO-level breach cooldown with an
    unrelated preamble alert's timing.

    `bypass_suppression` (isolated single-agent "Run in Simulator")
    always returns True — the operator wants every click to fire, same
    exemption `_v2_apply_escalation_gate` already grants.

    sim/replay/bypass_schedule track the window on an ISOLATED
    `alert_state` dict, never the real DB column — same isolation
    pattern `_cycle_claim_preamble_activation` already uses. (Replay
    never actually reaches this function — `_cycle_run_preamble`'s own
    top-of-function early return skips the whole preamble when
    `replay_mode` is set — included here only for defense in depth.)
    """
    if bypass_suppression:
        return True
    window_min = getattr(agent, "cooldown_minutes", None) or 30
    now_ = _preamble_utcnow()
    if sim_mode or replay_mode or bypass_schedule:
        store = alert_state.setdefault('_sim_preamble_alert_cooldown', {})
        last = store.get(agent.id)
        if last is not None and (now_ - last).total_seconds() / 60 < window_min:
            return False
        store[agent.id] = now_
        return True
    # Pre-check against the freshly loaded row first — skips the DB
    # write entirely on a tick that's clearly still inside the window,
    # same perf-minded pattern as a plain cooldown check elsewhere in
    # this module.
    last_fired = getattr(agent, "preamble_alert_last_fired_at", None)
    if last_fired is not None and (now_ - last_fired).total_seconds() / 60 < window_min:
        return False
    cutoff = now_ - timedelta(minutes=window_min)
    async with async_session() as session:
        result = await session.execute(
            update(Agent)
            .where(
                Agent.id == agent.id,
                or_(Agent.preamble_alert_last_fired_at.is_(None),
                    Agent.preamble_alert_last_fired_at <= cutoff),
            )
            .values(preamble_alert_last_fired_at=now_)
        )
        await session.commit()
    claimed = (result.rowcount or 0) > 0
    if claimed:
        agent.preamble_alert_last_fired_at = now_
    return claimed


class _PreambleAlertAgentView:
    """Lightweight read-only proxy so a preamble `alert(...)` entry's own
    `events` list can be dispatched through the EXACT SAME
    `events.dispatch()` function a matched cycle agent's own `events`
    field already uses — without mutating the real ORM `Agent` row's
    `events` column in place (which would mark it dirty for the next
    commit and risk persisting the preamble's transient events list as
    the agent's real stored `events`).

    `events` is the preamble entry's own list. `actions` is forced to
    `[]` so `dispatch()`'s `_describe_actions(agent.actions)` call never
    describes this agent's real DO-gated actions inside a preamble
    alert's body. Every other attribute (`id`, `slug`, `name`, `tier`,
    `topic`, …) delegates to the real agent unchanged — `dispatch()`
    reads `agent.id` to persist the `agent_events` row under this
    agent's own real id, which is correct: it genuinely is this agent's
    alert, just not one tied to a condition match."""

    def __init__(self, agent, events: list):
        self._agent = agent
        self.events = events
        self.actions = []

    def __getattr__(self, name):
        return getattr(self._agent, name)


async def _cycle_claim_preamble_activation(
    agent, *, alert_state: dict, sim_mode: bool, replay_mode: bool,
    bypass_schedule: bool, now,
) -> bool:
    """Atomically claim this agent's once-per-activation preamble slot.

    Returns True iff THIS call is the one that gets to run the
    once-per-activation side-effecting preamble entries (order/
    close_position/cancel_order/bracket/…) for this activation. Returns
    False when they already fired since the last real activation (or,
    in sim/replay/bypass_schedule, already fired this isolated run).

    Claim-then-fire, not fire-then-claim: `execute()` swallows per-action
    exceptions and the exchange-open gate can silently skip a broker
    call, so "did it actually fire" can't be reliably observed after the
    fact from this caller. Claiming FIRST means a DB-write failure (or a
    process crash between claim and fire) fails safe — no fire this
    cycle, retried next cycle — rather than firing an order and then
    failing to record that it fired, which would re-fire indefinitely
    (the exact P1 this item exists to close). The accepted tradeoff: an
    order whose own broker call is itself skipped by a gate still
    consumes the one-shot — the operator re-activates the agent to
    retry, same outcome as any other once-per-activation entry. The
    conditional `WHERE preamble_fired_at IS NULL` UPDATE also makes two
    concurrent run_cycle ticks for the same agent race-safe: at most one
    can see `rowcount > 0`.

    sim/bypass_schedule (the simulator + isolated manual-fire/dry-run
    paths) never touch the real `preamble_fired_at` DB column — a
    simulator run must not consume a live agent's real one-shot.
    Tracked instead on an ISOLATED per-run set on `alert_state`,
    mirroring the existing `_sim_latch` isolation pattern used elsewhere
    in this module. `replay_mode` is checked here too, for defense in
    depth, but `_cycle_run_preamble`'s own top-of-function early return
    already skips the ENTIRE preamble (not just this claim) whenever
    `replay_mode` is set — this function's caller never actually reaches
    here with `replay_mode=True` on the live path; see that function's
    docstring for why (the replay driver rebuilds `alert_state` fresh
    every historical tick, so there is no "once per activation" to
    anchor to, and `actions_sim.py:_replay_paper_trade` has no
    action-type filter at all).
    """
    if sim_mode or replay_mode or bypass_schedule:
        fired = alert_state.setdefault('_sim_preamble_fired', set())
        if agent.id in fired:
            return False
        fired.add(agent.id)
        return True
    # Always the real wall-clock UTC instant, never the caller's `now` —
    # `now` can be a naive IST scenario timestamp (simulator/replay
    # ticks), which would corrupt a TIMESTAMPTZ column. This mirrors
    # `_ae_dispatch_survivor_entry`'s own `last_triggered_at=datetime.
    # now(timezone.utc)` write, not `now` either.
    fired_ts = datetime.now(timezone.utc)
    async with async_session() as session:
        result = await session.execute(
            update(Agent)
            .where(Agent.id == agent.id, Agent.preamble_fired_at.is_(None))
            .values(preamble_fired_at=fired_ts)
        )
        await session.commit()
    claimed = (result.rowcount or 0) > 0
    if claimed:
        # Keep the in-memory row consistent with the DB for the rest of
        # this tick (e.g. a later re-read of `agent.preamble_fired_at`
        # within the same process shouldn't see the stale None).
        agent.preamble_fired_at = fired_ts
    return claimed


async def _cycle_dispatch_preamble_entry(
    agent, entry: dict, etype: str, *, action_ctx: dict, sim_mode: bool,
    context: dict, broadcast_fn, can_fire_once: bool, alert_log_ready: bool,
) -> None:
    """Dispatch ONE preamble entry — extracted from `_cycle_run_preamble`
    to keep that function's own cyclomatic complexity under this
    project's radon gate. The caller already computed `can_fire_once`
    (once-per-activation claim) and `alert_log_ready` (windowed alert/
    log cooldown claim) ONCE for the whole preamble; this function only
    reads those two flags, it never claims anything itself."""
    if etype == "alert":
        # replay_mode is already handled by _cycle_run_preamble's own
        # top-of-function early return (the whole preamble is skipped
        # in replay) — nothing replay-specific needed here.
        if not alert_log_ready:
            return
        events_list = entry.get("events") or []
        if not events_list:
            return
        shim = _PreambleAlertAgentView(agent, events_list)
        eval_result = EvalResult(
            triggered=True,
            condition_text=f"{getattr(agent, 'name', agent.slug)} — preamble alert",
            detail={},
        )
        # Defense-in-depth, mirroring `_v2_send_rich_alert`'s own
        # belt-and-suspenders double-gate (see
        # `_ae_dispatch_survivor_entry`'s identical comment for the full
        # reasoning): telegram/email/ntfy are `_RECORDED_CHANNELS`
        # (events.py) — `dispatch_rows()`'s own independent sim-notify
        # gate (keyed off `_emit_breach_record`'s `sim_mode` field)
        # already correctly suppresses a sim-mode breach record
        # downstream, regardless of `skip_channels` here. This is not a
        # fix for a confirmed leak; it mirrors an existing pattern.
        skip = (
            frozenset({'telegram', 'email', 'ntfy'})
            if _ae_sim_notify_suppressed(agent, sim_mode) else frozenset()
        )
        await dispatch(
            shim, eval_result, broadcast_fn, sim_mode=sim_mode,
            skip_channels=skip, context=context,
        )
        return
    if etype in _PREAMBLE_EVERY_CYCLE_TYPES:
        # log/help — `log` is cooldown-gated unless it's the debug(x)
        # sugar form (level="debug"); `help` is never gated (see
        # `_cycle_preamble_entry_is_cooldown_gated`'s own docstring).
        if etype == "log" and _cycle_preamble_entry_is_cooldown_gated(entry) \
                and not alert_log_ready:
            return
        params = entry.get("params") or {}
        await execute(agent, [{"type": etype, "params": params}], dict(action_ctx))
        return
    # Once-per-activation bucket — order/place_order/close_position/
    # cancel_order, bracket, and any other action type. Fail-safe
    # default: anything not explicitly log/help/alert is treated as
    # side-effecting and gated on `can_fire_once`.
    if not can_fire_once:
        return
    if etype == "bracket":
        logger.warning(
            f"Agent [{agent.slug}]: preamble 'bracket' entry "
            "has no registered action-dispatch handler "
            "(programmatic bracket creation/attach from a "
            "preamble call is not implemented) — skipped."
        )
        return
    params = entry.get("params") or {}
    await execute(agent, [{"type": etype, "params": params}], dict(action_ctx))


async def _cycle_run_preamble(
    agent, context: dict, now, *, sim_mode: bool, replay_mode: bool = False,
    bypass_schedule: bool = False, bypass_suppression: bool = False,
    alert_state: dict, broadcast_fn=None,
) -> None:
    """Execute a saved cycle agent's `preamble` list (grammar-
    unification plan, Design item 0).

    Split by statement type:
      - `help`, and `debug(x)`'s `log(level="debug")` sugar form — run
        on EVERY call (every poll cycle this agent is evaluated),
        dispatched via the EXACT SAME `execute()` path `agent.actions`/
        DO entries already use (mode resolution, the exchange-open
        gate, $row/$given resolution, audit logging — all for free,
        identical to a DO-gated call).
      - `alert`, and a hand-typed `log(...)` whose level is NOT `debug`
        — ALSO a notification-shaped call, not a broker action, so no
        real-money risk from repetition — but respects this agent's own
        `cooldown_minutes` (Design item 2), the SAME value ordered-
        condition agents already use, via a dedicated windowed claim
        (`_cycle_preamble_alert_cooldown_ready`) — fires, then
        suppressed until the window elapses, then eligible again. Not
        uncapped: see that function's own docstring for why this is a
        SEPARATE mechanism from both `_cycle_in_cooldown` and the once-
        per-activation claim below, not a reuse of either. `alert` is
        routed through `events.dispatch()` via `_PreambleAlertAgentView`
        so it is the IDENTICAL channel-dispatch function a matched
        agent's own `events` field uses, not a reimplementation.
      - Everything else (`order`/`place_order`/`close_position`/
        `cancel_order`, and any other action type) fires AT MOST ONCE
        per agent activation — gated by
        `_cycle_claim_preamble_activation()`. This is the direct fix for
        the audit's confirmed P1 finding (a side-effecting preamble call
        must never place a real broker order on every poll cycle).
      - `bracket` specifically: there is no registered action-dispatch
        handler anywhere (`_LIVE_ACTION_HANDLERS`/`_NOOP_ACTION_HANDLERS`
        in actions.py) for programmatic bracket creation/attach from an
        embedded call — confirmed by grep, not assumed. Rather than let
        it fall through to `execute()` (whose sim-mode branch can return
        a silent "success" for an unregistered type — see
        `actions_sim.py:_sim_paper_trade`'s non-order fallthrough — which
        would violate "never silently do nothing without a trace"), it
        is special-cased here: an explicit WARNING log, once per
        activation (not spammed every poll cycle), and otherwise a
        no-op. Building a real bracket-creation action handler is out of
        scope for this item — flagged as a follow-up (no CLAUDE.md entry
        exists for this yet; the doc-sync pass that lands after this
        item ships owns adding one).

    Never raises — mirrors `_cycle_evaluate_given_bindings`'s "no
    exception may abort run_cycle for other agents" contract; every
    per-entry failure is caught, logged, and the loop continues.
    """
    preamble = getattr(agent, "preamble", None)
    # Type-checked, not just falsy-checked — a bare `or []` fallback
    # isn't enough: a non-list truthy value (e.g. a raw/hand-edited DB
    # row where `preamble` was written as a scalar instead of a JSON
    # array) would reach `_cycle_preamble_has_once_entries(preamble)` or
    # `for entry in preamble:` below and raise, outside the per-entry
    # try/except this function otherwise guarantees — unlike every
    # per-entry failure here, that would abort run_cycle's entire
    # per-agent loop for every agent still queued this tick. See
    # `_cycle_agent_is_preamble_only`'s docstring for the same guard's
    # other real-world trigger (an unconfigured MagicMock test double).
    if not isinstance(preamble, list) or not preamble:
        return
    if replay_mode:
        # Replay rebuilds `alert_state` fresh every historical tick (the
        # backtest driver's own architecture, not this function's
        # concern) — there is no "once per activation" (or cooldown
        # window) to anchor to, and `actions_sim.py:_replay_paper_trade`
        # has NO action-type filter at all: it writes a `mode='replay'`
        # AlgoOrder row for ANY action type, including a diagnostic
        # `log`/`help` call (`symbol = f"{agent.slug}-{action_type}"`
        # fallback confirms it was never meant to special-case non-
        # broker types). Without this guard, every every-cycle preamble
        # entry would pollute backtest results with a spurious
        # informational order row on EVERY tick, and the once-per-
        # activation entries would do the same (their claim resets
        # every tick along with alert_state). Skip the entire preamble
        # during replay — informational/backtest-only, must never be a
        # vector for spurious writes or real notification sends.
        return

    action_ctx = dict(context)
    action_ctx["account"] = "TOTAL"
    action_ctx["sim_mode"] = sim_mode

    can_fire_once = False
    if _cycle_preamble_has_once_entries(preamble):
        try:
            can_fire_once = await _cycle_claim_preamble_activation(
                agent, alert_state=alert_state, sim_mode=sim_mode,
                replay_mode=replay_mode, bypass_schedule=bypass_schedule, now=now,
            )
        except Exception as e:
            # Fail safe, not fail loud — run_cycle's per-agent loop has
            # no surrounding try/except (same contract
            # _cycle_evaluate_given_bindings documents), so letting this
            # escape would abort evaluation for every agent still queued
            # this tick. A DB blip here means the once-per-activation
            # entries simply don't fire THIS cycle (retried next cycle,
            # since `preamble_fired_at` was never actually written) —
            # every-cycle entries are unaffected either way.
            logger.warning(
                f"Agent [{agent.slug}]: preamble activation claim failed "
                f"— once-per-activation entries skipped this cycle: {e}"
            )
            can_fire_once = False

    alert_log_ready = False
    if _cycle_preamble_has_cooldown_gated_entries(preamble):
        try:
            alert_log_ready = await _cycle_preamble_alert_cooldown_ready(
                agent, alert_state=alert_state, sim_mode=sim_mode,
                replay_mode=replay_mode, bypass_schedule=bypass_schedule,
                bypass_suppression=bypass_suppression,
            )
        except Exception as e:
            # Same fail-safe contract as the once-per-activation claim
            # above — a DB blip here means alert/log entries just don't
            # fire this cycle, never an escaped exception.
            logger.warning(
                f"Agent [{agent.slug}]: preamble alert cooldown claim "
                f"failed — alert/log entries skipped this cycle: {e}"
            )
            alert_log_ready = False

    for entry in preamble:
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        try:
            await _cycle_dispatch_preamble_entry(
                agent, entry, etype, action_ctx=action_ctx, sim_mode=sim_mode,
                context=context, broadcast_fn=broadcast_fn,
                can_fire_once=can_fire_once, alert_log_ready=alert_log_ready,
            )
        except Exception as e:
            logger.error(f"Agent [{agent.slug}]: preamble entry {etype!r} failed — {e}")


def _cycle_evaluate_agent(agent, context: dict, cfg: dict, now, alert_state: dict,
                          *, bypass_schedule: bool = False) -> tuple[list, list]:
    """Build a V2Context for the agent and run the condition tree evaluator.

    alert_state must be the same dict object held by run_cycle so that any
    mutations made by the evaluator (e.g. pnl_history updates) remain visible
    to subsequent per-agent gates on the same tick.

    Fix #6a — `baseline_live` is computed HERE (once per agent per tick,
    cheap) and passed onto the Context so every rate leaf (`rate_abs`/
    `rate_pct`) self-gates during the post-open baseline window, instead
    of the old whole-agent gate that only worked for agents whose
    conditions were ENTIRELY rate leaves (both real loss-rate agents mix
    a `day_val`/`pnl` leaf in, so the old gate never actually applied to
    them). `bypass_schedule` (sim mode) always treats the baseline as
    live — sim ticks aren't tied to wall-clock market hours.

    Returns (matches, observations) — see `agent_evaluator.Context.observations`
    for what an observation carries. Both empty on evaluator error.
    """
    baseline_live = bypass_schedule or _v2_baseline_live(
        alert_state, now, cfg['baseline_offset_min']
    )
    v2_ctx = V2Context(
        sum_holdings=context.get("sum_holdings"),
        sum_positions=context.get("sum_positions"),
        df_margins=context.get("df_margins"),
        watchlist_rows=context.get("watchlist_rows") or [],
        position_rows=context.get("position_rows") or [],
        orders_rows=context.get("orders_rows") or [],
        quote_rows=context.get("quote_rows") or [],
        symbols_rows=context.get("symbols_rows") or [],
        spot_prices=context.get("spot_prices") or {},
        log_records=(context.get("log_records_by_agent") or {}).get(agent.id, []),
        alert_state=alert_state,
        now=now,
        segments=context.get("segments", []),
        segment_flags=context.get("segment_flags") or {},
        rate_window_min=cfg['rate_window_min'],
        agent=agent,
        baseline_live=baseline_live,
    )
    # Part 2 (RamboQL GIVEN upgrade) — evaluate this agent's `given`
    # bindings (if any) BEFORE the condition tree runs, using the same
    # V2Context just built. Never raises (see the function's own
    # docstring) and is a true no-op for every agent without `given`.
    _cycle_evaluate_given_bindings(agent, v2_ctx, context)
    try:
        matches = v2_evaluate(agent.conditions, v2_ctx)
        return matches, v2_ctx.observations
    except Exception as e:
        logger.error(f"Agent [{agent.slug}] v2 evaluate failed: {e}")
        return [], []


def _cycle_maybe_buffer_fire(
    agent,
    matches: list,
    *,
    now,
    bypass_suppression: bool,
    bypass_schedule: bool,
    sim_mode: bool,
    alert_state: dict,
    cfg: dict,
    broadcast_fn,
    debounce_min: int,
    pending_dispatches: list,
    replay_mode: bool = False,
) -> bool:
    """Evaluate the per-key re-alert escalation gate and, when the agent
    fires, buffer a dispatch entry.

    Returns True when the agent fired (triggered), False otherwise.
    Mutates pending_dispatches in place on fire.

    Caller MUST have already run `_v2_apply_recovery` for this tick
    (unconditionally, even when `matches` is empty) — recovery clearing
    and the escalation gate both read/write the same per-key latch, and
    a key that just recovered must not carry stale escalation state into
    this function.

    `bypass_suppression` means "fire on every match, ignore the latch
    entirely" (isolated single-agent sim runs). Otherwise the escalation
    gate always applies — sim AND replay runs use an ISOLATED
    per-simulation store (`alert_state['_sim_latch']`) rather than the
    live module-level `_V2_LATCH`, so neither a sim run nor a replay/
    backtest tick ever corrupts real re-alert timing. Replay passes
    `sim_mode=False` (deliberately distinct — see `_cycle_process_agent`)
    but must still route through the isolated store, hence the separate
    `replay_mode` flag rather than folding it into `sim_mode` itself.
    """
    if not matches:
        return False
    if bypass_suppression:
        effective = matches
        fire_matches = matches
    else:
        cooldown_min = getattr(agent, 'cooldown_minutes', None) or cfg['cooldown_min']
        store = alert_state.setdefault('_sim_latch', {}) if (sim_mode or replay_mode) else None
        effective = _v2_apply_escalation_gate(
            agent, matches, now, cooldown_min, store=store,
        )
        # Sprint 16 fan-out support — `effective` (below, unchanged,
        # still feeds `latched_matches`/hydration) is gated at per-KEY
        # granularity: `_latch_key` is (agent_slug, metric, scope,
        # account) — NO symbol component. For a per-row scope like
        # `positions.expiring_today` where the SAME account can
        # legitimately produce 2+ matches with DIFFERENT symbols in one
        # tick, `_v2_apply_escalation_gate`'s loop re-arms the latch
        # after the FIRST row of a key passes, so every subsequent row
        # sharing that same key fails its own cooldown check a moment
        # later in the same loop and is silently dropped from
        # `effective` — even though the key as a whole genuinely fired
        # this tick. `fire_matches` re-expands back to every RAW match
        # row whose key is among the keys that passed, so action fan-out
        # (which needs one execution per real row) doesn't silently skip
        # rows the escalation gate only ever intended to gate at the key
        # level. `latched_matches`/hydration are untouched — they keep
        # reading `effective` exactly as before this fix.
        effective_keys = {_latch_key(agent.slug, m) for m in effective}
        fire_matches = [m for m in matches if _latch_key(agent.slug, m) in effective_keys]
    if not effective:
        return False

    result = _v2_build_evalresult(
        matches, agent.name, replay_mode=replay_mode, latched_matches=effective,
    )
    # Only cosmetic-/notify-only tiers get the "Scheduled — HH:MM IST" label.
    # Critical/high/medium fire_at_time agents (e.g. expiry-day auto-close) emit
    # their real condition text so operators know what condition actually fired.
    if (getattr(agent, 'fire_at_time', None)
            and getattr(agent, 'tier', 'medium') in ('info', 'low')):
        result.condition_text = f"Scheduled — {agent.fire_at_time} IST"
    if sim_mode:
        _cycle_shadow_lifespan_decrement(agent, alert_state)
    if broadcast_fn:
        broadcast_fn("agent_state", {"slug": agent.slug, "status": "triggered"})
    new_status, _ = _cycle_compute_post_fire_status(agent, bypass_schedule=bypass_schedule)
    pending_dispatches.append({
        'agent':           agent,
        'matches':         matches,
        'latched_matches': effective,
        # Sprint 16 fan-out — see the comment above `fire_matches`'
        # derivation. Deliberately NOT touched by
        # `_cycle_dispatch_survivors`' topic-suppression merge (same
        # reasoning as `latched_matches`: a merged sibling agent's rows
        # must never be fired through THIS agent's actions).
        'fire_matches':    fire_matches,
        'result':          result,
        'sim_mode':        sim_mode,
        'replay_mode':     replay_mode,
        'alert_state':     alert_state,
        'bypass_schedule': bypass_schedule,
        'new_status':      new_status,
        'debounce_min':    debounce_min,
    })
    return True


def _cycle_apply_debounce(
    agent,
    matches: list,
    now,
    *,
    sim_mode: bool,
) -> tuple[list, object, bool]:
    """Apply the debounce state machine and return updated latch state.

    Returns (matches_after, new_first_true_at, latch_changed).

    State machine (runs only when debounce_minutes > 0 and not sim_mode):
      match=False + latch set  → clear latch (re-arm); matches unchanged
      match=True  + latch None → set latch to now, suppress (matches=[])
      match=True  + latch set, elapsed < window → suppress (matches=[])
      match=True  + latch set, elapsed >= window → fire normally
    """
    debounce_min = int(getattr(agent, "debounce_minutes", 0) or 0)
    new_first_true_at = agent.condition_first_true_at
    changed = False

    if debounce_min <= 0 or sim_mode:
        # Sim runs bypass debounce; zero window = no gate.
        return matches, new_first_true_at, changed

    if not matches:
        if agent.condition_first_true_at is not None:
            new_first_true_at = None
            changed = True
    else:
        if agent.condition_first_true_at is None:
            new_first_true_at = now
            changed = True
            logger.info(
                f"Agent [{agent.slug}] debounce armed "
                f"({debounce_min}m); waiting for sustained condition"
            )
            matches = []
        else:
            elapsed_min = (now - agent.condition_first_true_at).total_seconds() / 60.0
            if elapsed_min < debounce_min:
                matches = []
            # else: window crossed — let matches through; latch cleared
            # naturally after the fire commit.

    return matches, new_first_true_at, changed


def _cycle_shadow_lifespan_exhausted(agent, alert_state: dict) -> bool:
    """Check (and initialise if needed) the sim shadow-lifespan quota.

    Returns True when the agent's shadow quota is exhausted for this sim
    iteration.  Mutates alert_state to initialise the shadow slot and to mark
    exhaustion so the simulator report doesn't double-count.
    """
    ls_state = alert_state.setdefault('shadow_lifespan', {})
    shadow = ls_state.get(agent.id)
    if shadow is None:
        shadow = {
            'remaining': _initial_shadow_remaining(agent),
            'exhausted': False,
        }
        ls_state[agent.id] = shadow
    if shadow.get('exhausted') or (shadow['remaining'] is not None
                                   and shadow['remaining'] <= 0):
        exh = alert_state.setdefault('lifespan_exhausted_agents', [])
        if agent.id not in exh:
            exh.append(agent.id)
        shadow['exhausted'] = True
        return True
    return False


def _cycle_shadow_lifespan_decrement(agent, alert_state: dict) -> None:
    """Decrement the sim shadow-lifespan counter after a fire.

    Marks exhaustion when remaining hits 0 so the next tick's skip check
    fires correctly.
    """
    ls_state = alert_state.setdefault('shadow_lifespan', {})
    shadow = ls_state.get(agent.id)
    if shadow is not None and shadow.get('remaining') is not None:
        shadow['remaining'] -= 1
        if shadow['remaining'] <= 0:
            shadow['exhausted'] = True
            exh = alert_state.setdefault('lifespan_exhausted_agents', [])
            if agent.id not in exh:
                exh.append(agent.id)


def _cycle_compute_post_fire_status(agent, *, bypass_schedule: bool) -> tuple[str, int]:
    """Compute (new_status, new_trigger_count) for an agent that just fired.

    When bypass_schedule is True (sim mode) the agent's DB row must not be
    mutated, so we return the current values unchanged.
    """
    if bypass_schedule:
        return agent.status, (agent.trigger_count or 0)
    new_trigger_count = (agent.trigger_count or 0) + 1
    lifespan = getattr(agent, "lifespan_type", "persistent") or "persistent"
    if lifespan == "one_shot":
        new_status: str = "completed"
    elif (lifespan == "n_fires"
          and agent.lifespan_max_fires is not None
          and new_trigger_count >= agent.lifespan_max_fires):
        new_status = "completed"
    else:
        new_status = "cooldown"
    return new_status, new_trigger_count


def _ae_cycle_pre_gates_pass(agent, now, *, any_market_open: bool,
                             bypass_schedule: bool) -> bool:
    """Return True when the agent clears all pre-EVALUATION timing gates.

    Checks schedule, fire_at_time, and blackout windows — gates that mean
    "don't even evaluate this tick". Deliberately does NOT include agent-
    level cooldown (`_cycle_in_cooldown`): cooldown must block RE-FIRING
    only, not the recovery pass, which needs to run every tick regardless
    (see `_ae_cycle_eval_and_buffer`'s `in_cooldown` handling) — a
    recovered latch must clear immediately, not sit stuck until the
    cooldown window itself elapses. Extracted from _cycle_process_agent
    to reduce CC there."""
    if _cycle_should_skip_schedule(agent, any_market_open=any_market_open,
                                   bypass_schedule=bypass_schedule):
        return False
    if _cycle_outside_fire_at(agent, now, bypass_schedule=bypass_schedule):
        return False
    if _cycle_in_blackout(agent, now, bypass_schedule=bypass_schedule):
        return False
    return True


async def _ae_cycle_eval_and_buffer(
    agent, context: dict, cfg: dict, now,
    *, alert_state: dict, sim_mode: bool,
    bypass_schedule: bool, bypass_suppression: bool,
    broadcast_fn, pending_dispatches: list,
    in_cooldown: bool = False,
    replay_mode: bool = False,
) -> None:
    """Evaluate condition tree, apply debounce/lifespan gates, buffer fires.

    Also persists non-triggered state changes. Extracted from
    _cycle_process_agent to reduce CC there."""
    matches, observations = _cycle_evaluate_agent(
        agent, context, cfg, now, alert_state, bypass_schedule=bypass_schedule,
    )
    # Grammar-unification plan, Design item 0 — preamble execution runs
    # right after condition evaluation, UNCONDITIONALLY (before the
    # in_cooldown check below, and regardless of `matches`) — same
    # "every poll cycle this agent is evaluated" spirit as the recovery
    # pass immediately below, and the natural place in this function's
    # control flow given `_cycle_evaluate_agent` itself is a sync `def`
    # (many existing tests call it directly and expect a sync tuple
    # return, so it was not converted to async — see
    # `_cycle_run_preamble`'s own docstring for the per-entry split).
    await _cycle_run_preamble(
        agent, context, now, sim_mode=sim_mode, replay_mode=replay_mode,
        bypass_schedule=bypass_schedule, bypass_suppression=bypass_suppression,
        alert_state=alert_state, broadcast_fn=broadcast_fn,
    )
    if _cycle_agent_is_preamble_only(agent):
        # A preamble-only statement's `conditions` is the always-true
        # sentinel leaf, not a real breach condition — it must never
        # drive the normal match/recovery/debounce/buffer-fire/cooldown
        # machinery (see `_cycle_agent_is_preamble_only`'s own docstring
        # for the confirmed spurious-rich-alert failure mode this
        # guards against). The preamble itself already ran above; there
        # is nothing else for this agent to do this tick.
        return
    # Recovery pass runs UNCONDITIONALLY, on the RAW evaluator result —
    # before debounce filtering (which can suppress `matches` to [] for
    # reasons unrelated to the underlying condition, e.g. "not sustained
    # long enough yet") and regardless of whether `matches` is empty this
    # tick (fix #5/#9: a fully-recovered tick, matches == [], is exactly
    # the case whose latches need clearing). Skipped entirely in sim mode
    # bypass_suppression runs (isolated single-agent "run in simulator"),
    # which don't use the latch at all.
    #
    # Runs even when the agent is in agent-level cooldown (`in_cooldown`)
    # — cooldown must block RE-FIRING only. Pre-fix, `_cycle_in_cooldown`
    # sat inside `_ae_cycle_pre_gates_pass` and returned early BEFORE this
    # function (and therefore this recovery pass) ever ran, so a agent
    # stuck in cooldown never cleared a recovered latch until the
    # cooldown window itself elapsed.
    if not bypass_suppression:
        # sim OR replay routes through the isolated store — replay
        # passes sim_mode=False (deliberately distinct, see
        # _cycle_process_agent), so sim_mode alone under-selects here;
        # without replay_mode a replay tick would run recovery against
        # the LIVE _V2_LATCH using historical values, clearing a real
        # breach's latch (next live tick re-fires as a duplicate) or
        # wrongly suppressing one.
        store = alert_state.setdefault('_sim_latch', {}) if (sim_mode or replay_mode) else None
        _v2_apply_recovery(agent, observations, store=store)

    if in_cooldown:
        # Re-fire/escalation stays gated — only the recovery pass above
        # is exempt from agent-level cooldown.
        return

    matches, debounce_new_first_true_at, debounce_first_true_changed = (
        _cycle_apply_debounce(agent, matches, now, sim_mode=sim_mode)
    )
    debounce_min = int(getattr(agent, "debounce_minutes", 0) or 0)

    if matches and sim_mode and _cycle_shadow_lifespan_exhausted(agent, alert_state):
        return

    triggered = _cycle_maybe_buffer_fire(
        agent, matches,
        now=now,
        bypass_suppression=bypass_suppression,
        bypass_schedule=bypass_schedule,
        sim_mode=sim_mode,
        alert_state=alert_state,
        cfg=cfg,
        broadcast_fn=broadcast_fn,
        debounce_min=debounce_min,
        pending_dispatches=pending_dispatches,
        replay_mode=replay_mode,
    )

    if not bypass_schedule and not triggered:
        await _cycle_persist_untriggered_state(
            agent,
            debounce_first_true_changed=debounce_first_true_changed,
            debounce_new_first_true_at=debounce_new_first_true_at,
            broadcast_fn=broadcast_fn,
        )


async def _cycle_process_agent(
    agent, *, agents, context: dict, cfg: dict,
    now, any_market_open: bool,
    bypass_schedule: bool, bypass_suppression: bool,
    broadcast_fn, pending_dispatches: list,
) -> None:
    """Evaluate a single agent within a run_cycle tick.

    Encapsulates the gate chain, debounce, shadow-lifespan check, suppression
    buffering and non-triggered persist so run_cycle's top-level body stays
    below the D-grade threshold.
    """
    if await _cycle_maybe_expire_lifespan(
        agent, now, bypass_schedule=bypass_schedule, broadcast_fn=broadcast_fn
    ):
        return

    if not _ae_cycle_pre_gates_pass(agent, now, any_market_open=any_market_open,
                                    bypass_schedule=bypass_schedule):
        return

    alert_state = context.get("alert_state") or {}
    sim_mode = bool(alert_state.get("sim_mode") or context.get("sim_mode"))
    # replay driver sets alert_state={"replay_mode": True} + context
    # sim_mode=False (deliberately distinct from sim_mode) — a replay
    # cycle's `now` is a historical/scenario timestamp, so it must be
    # excluded from the live-only day-rollover reset the same way
    # sim_mode is (see _maybe_reset_v2_state docstring).
    replay_mode = bool(alert_state.get("replay_mode") or context.get("replay_mode"))
    _maybe_reset_v2_state(
        now.date() if hasattr(now, 'date') else None,
        live=not (sim_mode or replay_mode),
    )

    # Agent-level cooldown (Agent.status=="cooldown" + last_triggered_at +
    # cooldown_minutes) is evaluated HERE, separately from
    # _ae_cycle_pre_gates_pass — it must not prevent evaluation, only
    # gate re-firing, so it's threaded into _ae_cycle_eval_and_buffer as
    # a flag rather than blocking the call entirely.
    in_cooldown = _cycle_in_cooldown(agent, bypass_schedule=bypass_schedule)

    # Fix #6a — the whole-agent "pure rate metric" baseline gate is gone;
    # _cycle_evaluate_agent now computes `baseline_live` once per agent
    # and threads it onto the Context so each rate LEAF self-gates
    # (agent_evaluator.Context.rate_abs/rate_pct), letting a mixed
    # agent's non-rate leaves keep evaluating during the opening window.
    await _ae_cycle_eval_and_buffer(
        agent, context, cfg, now,
        alert_state=alert_state, sim_mode=sim_mode,
        bypass_schedule=bypass_schedule, bypass_suppression=bypass_suppression,
        broadcast_fn=broadcast_fn, pending_dispatches=pending_dispatches,
        in_cooldown=in_cooldown,
        replay_mode=replay_mode,
    )


async def run_cycle(context: dict, broadcast_fn=None,
                    only_agent_ids: list[int] | None = None,
                    bypass_schedule: bool = False,
                    bypass_suppression: bool = False):
    """
    Main agent evaluation cycle. Called from background.py every refresh.

    Args:
        context: dict with sum_holdings, sum_positions, df_margins, now, seg_state
        broadcast_fn: WebSocket broadcast function
        only_agent_ids: when non-empty, restrict evaluation to these agent
                        IDs and include them regardless of `status` — lets the
                        simulator dry-run an inactive agent without flipping
                        it on globally.
        bypass_schedule: when True, ignore the market_hours gate, the DB
                        cooldown status, and the rate-metric baseline offset.
                        The simulator uses this because sim ticks aren't
                        tied to wall-clock market hours.
        bypass_suppression: when True, ALSO skip the per-agent suppression
                        latch. Reserved for isolated single-agent "Run in
                        Simulator" runs where the operator wants every click
                        to fire; general sim runs keep suppression on so a
                        prolonged breach fires once, not every tick.
    """
    now = context.get("now")
    if not now:
        return

    # Deploy-survival latch hydration (fixes #7/#8/#9's "Also fold in") —
    # no-op after the first successful/attempted call this process.
    await _v2_hydrate_latch()

    # Tier-suppression buffer — fires accumulate here during the per-agent
    # loop, then a single post-loop pass computes topic-scoped suppression
    # and dispatches the survivors. State mutations (cooldown latch,
    # lifespan shadow) still happen inline so cross-tick semantics are
    # unchanged; only the push notification + action execution defer.
    # Each entry: {agent, matches, result, sim_mode, alert_state}.
    pending_dispatches: list[dict] = []

    agents = await _cycle_load_agents(only_agent_ids)
    if not agents:
        return

    from backend.api.algo import log_feed
    new_log_rows = await log_feed.records_since_last_cycle()
    context["log_records_by_agent"] = {a.id: new_log_rows for a in agents}

    # Build base context BEFORE _update_pnl_history (fix #6b — the
    # segment-open flags computed here drive whether/how this tick's P&L
    # snapshot gets recorded; the old ordering called _update_pnl_history
    # first, so it never had this information). When the simulator passes
    # a `market_state` override dict on the context, forward it so the
    # per-segment open flags reflect the simulated clock (e.g.
    # "pre_close" preset) instead of real wall-clock time.
    # _build_context can do a blocking HTTP GET to nseindia.com when
    # the holidays cache is cold (once per day per exchange). Offload
    # to a thread so the agent tick doesn't stall the event loop.
    base_ctx = await asyncio.to_thread(
        _build_context, now, sim_overrides=context.get("market_state")
    )

    # Determine whether NSE/MCX are currently open (for schedule filtering)
    nse_open_flag = bool(base_ctx.get("nse_open"))
    mcx_open_flag = bool(base_ctx.get("mcx_open"))
    any_market_open = nse_open_flag or mcx_open_flag

    # Market-cycle event tokens (2026-10-22) — thread `base_ctx`'s
    # per-segment flags into `context` under a key distinct from
    # `market_state` (the sim-override INPUT above) so `_cycle_evaluate_
    # agent` can hand it to V2Context as `segment_flags` — the first live
    # reader of this dict (see `Context.segment_flags`'s own docstring,
    # agent_evaluator.py, for why it isn't folded into `market_state`).
    context["segment_flags"] = dict(base_ctx)

    # Append the current P&L snapshot to alert_state.pnl_history so the
    # rate evaluator has samples to compute ΔP&L/min against. The
    # background performance task and the simulator both pass the same
    # long-lived `alert_state` dict, so each run_cycle call grows the
    # history one entry per (section, scope) bucket.
    #
    # Fix #6b — under bypass_schedule (sim), always record unconditionally
    # (market_state=None) — sim ticks aren't tied to wall-clock market
    # hours, so "is a segment open" isn't a meaningful gate for a
    # scenario the operator is explicitly driving. On the live path, pass
    # base_ctx so the segment-anchored baseline + closed-market skip apply.
    _alert_state = context.get("alert_state")
    if _alert_state is not None:
        _update_pnl_history(
            _alert_state, now,
            context.get("sum_positions"),
            context.get("sum_holdings"),
            market_state=None if bypass_schedule else base_ctx,
        )
        # Underlying-divergence feature — sibling writers, same call site,
        # same tick, run immediately after _update_pnl_history so they can
        # read the session_date that call just set/reconciled. See
        # grammar.py's module comment above `_scope_positions_any_
        # underlying` for the full design.
        _position_rows = context.get("position_rows") or []
        _sum_positions = context.get("sum_positions")
        _update_underlying_theo_baseline(
            _alert_state, now, _position_rows, context.get("spot_prices") or {},
            sum_positions=_sum_positions,
        )
        _update_underlying_pnl_history(
            _alert_state, now, _position_rows, sum_positions=_sum_positions,
            market_state=None if bypass_schedule else base_ctx,
        )

    # Hoist _v2_cfg() outside the per-agent loop — it reads global Settings
    # rows and has no per-agent dependency. Avoids 15 redundant dict lookups
    # per run_cycle tick.
    cfg = _v2_cfg()

    for agent in agents:
        await _cycle_process_agent(
            agent, agents=agents, context=context, cfg=cfg,
            now=now, any_market_open=any_market_open,
            bypass_schedule=bypass_schedule, bypass_suppression=bypass_suppression,
            broadcast_fn=broadcast_fn, pending_dispatches=pending_dispatches,
        )

    # ── Post-loop: topic-scoped tier suppression + dispatch survivors ────
    if pending_dispatches:
        await _cycle_dispatch_survivors(pending_dispatches, now, context, broadcast_fn)


async def _ae_dispatch_suppressed_entry(entry: dict, suppressed_ids: dict,
                                       broadcast_fn) -> None:
    """Emit an audit-log event for a suppressed fire and broadcast state.

    No push notification or action execution. Extracted from
    _cycle_dispatch_survivors to reduce CC there."""
    agent        = entry['agent']
    result       = entry['result']
    sim_mode_p   = entry['sim_mode']
    replay_mode_p = entry.get('replay_mode', False)
    matches_     = entry.get('matches') or []
    # Fix (2026-10): hydration must latch only the escalation-gated subset,
    # not every raw breaching match — see _v2_build_evalresult's docstring.
    # Falls back to matches_ when the caller (e.g. a test building this
    # entry directly) didn't thread 'latched_matches' through.
    latched_     = entry.get('latched_matches')
    if latched_ is None:
        latched_ = matches_
    supp_by      = suppressed_ids[agent.id]
    topic        = getattr(agent, 'topic', 'general')
    detail_text = (
        f"Suppressed by higher-tier agent '{supp_by}' in topic '{topic}'."
    )
    detail: dict = {'matches': matches_,
                    'latched_matches': latched_,
                    'suppressed_by': supp_by,
                    'topic': topic,
                    'tier':  getattr(agent, 'tier', 'medium')}
    if replay_mode_p:
        # Same marker _v2_build_evalresult stamps for the survivor path
        # — _hydrate_latch_from_rows checks this to skip replay-authored
        # rows on restart (replay passes sim_mode=False, so the SQL-level
        # AgentEvent.sim_mode.is_(False) filter alone does not exclude them).
        detail['replay_mode'] = True
    try:
        await log_event(
            agent, 'triggered_suppressed',
            f"{result.condition_text} — {detail_text}",
            # 'latched_matches' is required here for the SAME reason the
            # 'triggered' path (_v2_build_evalresult) writes it:
            # _hydrate_latch_from_rows reads detail['latched_matches'] to
            # reconstruct _V2_LATCH on process restart. Without it every
            # suppressed fire hydrates as an empty latch — a standing
            # breach that was suppressed (not silenced by recovery) would
            # incorrectly appear "never fired" after a deploy.
            detail=detail,
            sim_mode=sim_mode_p,
        )
    except Exception as _le:
        logger.debug(f"suppressed-event log failed: {_le}")
    if broadcast_fn:
        broadcast_fn('agent_state', {
            'slug': agent.slug,
            'status': 'suppressed',
            'suppressed_by': supp_by,
        })


def _ae_fanout_matches(latched_matches: list) -> list[dict]:
    """Collapse `latched_matches` (the escalation-gated subset that
    actually fired THIS tick — see `_cycle_maybe_buffer_fire`, which
    stamps this as `effective = _v2_apply_escalation_gate(...)`, i.e.
    "what really fired", as opposed to the raw `matches` list which can
    also be extended with a SIBLING agent's rows by
    `_cycle_dispatch_survivors`'s topic-tier merge — using the raw list
    here would leak another agent's row data into this agent's action
    execution) into the list of per-execution match entries
    `_ae_dispatch_survivor_entry` fires `agent.actions` against, once per
    entry (Sprint 16 fan-out).

    Aggregate/synthetic matches — classified by SCOPE SHAPE via
    `_ae_is_aggregate_match` (every `positions.total` / `holdings.total`
    / `funds.total`-scoped leaf's row, and the `not` composite's
    synthetic `{'not': inner}` entry; NOT by whether `account` happens
    to be `None` — fixed, see that function's docstring: a `quote.
    tracked` match has `account=None` but is a genuine per-row match,
    never aggregate) — collapse to EXACTLY ONE execution with
    `account='TOTAL'`, using the first such match's row. This is the
    backward-compatibility
    guarantee: an agent built entirely from aggregate-scoped leaves fires
    exactly once today (each such leaf's scope selector returns at most
    one TOTAL row), and must keep firing exactly once after fan-out —
    including the edge case of an `any[]`/`all[]` combining MULTIPLE
    aggregate leaves that all happen to breach the same tick (e.g.
    `loss-positions-total`'s 4-leaf `any[]`, every leaf scoped to
    `positions.total` — two or more could legitimately co-breach in one
    tick; today's code only ever executes `agent.actions` once
    regardless, so fan-out must not turn that into 2+ fires).

    Real per-row matches (`positions.any_acct`, `positions.expiring_today`,
    etc. — `account` is a genuine, non-TOTAL account id) each get their
    own execution, EVEN WHEN two matches share the same account with
    different rows (e.g. two different MCX contracts both expiring today
    in the same account) — fan-out is per matched ROW, not per account.

    Same-row duplicate guard: a condition tree can combine two DIFFERENT
    leaves over the SAME underlying row (e.g. `loss-funds-negative`'s
    `any[cash<0, avail_margin<0]` on `funds.any_acct` — one account
    breaching BOTH legs at once produces two match entries for the
    identical row). Deduped by row identity `(scope, account,
    tradingsymbol, exchange, product)` — the first entry for a given
    identity wins, later ones are dropped — so one real-world breach
    never becomes two executions.
    """
    if not latched_matches:
        return []
    aggregate_entry = None
    per_row: list[dict] = []
    seen_identities: set = set()
    for m in latched_matches:
        if _ae_is_aggregate_match(m):
            if aggregate_entry is None:
                aggregate_entry = m
            continue
        identity = _ae_row_identity(m)
        if identity in seen_identities:
            continue
        seen_identities.add(identity)
        per_row.append(m)
    out: list[dict] = []
    if aggregate_entry is not None:
        out.append(aggregate_entry)
    out.extend(per_row)
    return out


def _ae_is_aggregate_match(m: dict) -> bool:
    """Shared aggregate/synthetic classifier for a v2 condition match
    entry — used by both `_ae_fanout_matches` (dedup pass) and
    `_ae_dispatch_actions` (row-binding gate) so the two can never
    drift apart.

    Classifies by SCOPE SHAPE, never by whether `account` happens to be
    None (fixed — see CLAUDE.md "Alert evaluation and latching" /
    Sprint 21 notes). The pre-fix version checked `m.get('account') in
    (None, 'TOTAL')`, which silently misclassified every `quote.tracked`
    match as aggregate — `quote.tracked` rows carry NO account dimension
    at all (market data belongs to no account — see
    `grammar_tokens.scope_quote_tracked._scope_quote_tracked`'s own
    docstring), so `account` is
    always None on a real, specific, symbol-filtered match, not because
    the match is an aggregate/TOTAL row.

    True when:
      - the entry has no `scope` key at all — the `not` composite's
        synthetic `{'not': inner}` entry (see `evaluate()`'s `not`
        branch in agent_evaluator.py) is the only shape this covers;
        it carries no row to bind, by construction.
      - `scope` ends with `.total` — the three account-level TOTAL
        summary scopes (`positions.total` / `holdings.total` /
        `funds.total` — the only scope tokens ending in `.total` in
        `grammar.py`'s SCOPE_REGISTRY).
      - `account == 'TOTAL'` literal — belt-and-suspenders: every real
        `.total`-scope row already carries this sentinel (see
        `grammar_tokens.scope_positions_total._scope_positions_total`
        et al.), so this is
        redundant with the scope-suffix check above today, but guards
        against a future scope that sets the sentinel without itself
        ending in `.total`.

    Every other match (including `quote.tracked`, whose rows have
    `account=None` but a real `scope='quote.tracked'`) is a genuine
    per-row match and must bind its row for `$row`-referencing actions.
    """
    if 'scope' not in m:
        return True
    scope_tok = m.get('scope')
    if isinstance(scope_tok, str) and scope_tok.endswith('.total'):
        return True
    if m.get('account') == 'TOTAL':
        return True
    return False


def _ae_row_identity(m: dict) -> tuple:
    """Row-identity key for `_ae_fanout_matches`' dedup pass — two match
    entries referring to the SAME underlying row (same scope, account,
    and — for per-symbol scopes — the same tradingsymbol/exchange/
    product) collapse to one execution regardless of which leaf(s)
    produced them. Account-level summary rows (funds/positions/holdings
    `*_acct` scopes) have no tradingsymbol, so those three fields are
    `None` for every row at that scope — which is exactly the desired
    behavior: scope+account alone is already a full identity for a
    one-row-per-account summary frame.

    Sprint 21 — also includes `order_id` (None on every pre-existing row
    shape, which never carries that key — purely additive, no behavior
    change for positions/holdings/funds/watchlist). Without it, two
    genuinely distinct open `orders.any_acct` rows for the same account/
    symbol/exchange (e.g. two separate limit orders resting on the same
    contract) would collapse to one identity and silently drop one of
    them from `$row`-ref fan-out."""
    row = m.get('row') or {}
    return (
        m.get('scope'), m.get('account'),
        row.get('tradingsymbol'), row.get('exchange'), row.get('product'),
        row.get('order_id'),
    )


def _ae_actions_use_row_ref(actions: list | None) -> bool:
    """True when at least one action in `actions` references a bare-NAME
    matched-row column (the `{"$row": "<name>"}` marker — see
    `actions.py:resolve_row_ref_params`).

    Fan-out (`_ae_dispatch_survivor_entry`) activates ONLY when this is
    True. No currently-existing agent (seeded or operator-authored) can
    contain `$row` — it is a brand-new marker the Sprint 16 CLI compiler
    introduces — so this makes the backward-compatibility guarantee true
    BY CONSTRUCTION for every agent that predates this feature, not just
    ones whose condition scope happens to be aggregate. It also protects
    whole-scope "sweep" actions (`chase_close_positions`, `cancel_all_
    orders`, `expiry_auto_close` — each already scans/closes everything
    in scope in ONE call) from being re-invoked once per matched row,
    which would be actively harmful (N redundant close sweeps racing each
    other, not N correct partial actions) for an agent whose action
    doesn't reference per-row data at all. Fan-out is reserved for
    actions that explicitly ask for row-bound values via bare-NAME
    syntax (`close_position(account=account_id, symbol=symbol)`)."""
    for action in actions or []:
        params = action.get('params') if isinstance(action, dict) else None
        if not isinstance(params, dict):
            continue
        if any(_is_row_ref_marker(v) for v in params.values()):
            return True
    return False


async def _ae_dispatch_survivor_entry(entry: dict, now, context: dict,
                                      broadcast_fn) -> None:
    """Commit all side-effects for a surviving (non-suppressed) fire.

    Writes DB state, broadcasts WS status, sends rich alert / dispatch,
    and executes actions. Extracted from _cycle_dispatch_survivors."""
    agent       = entry['agent']
    matches_    = entry['matches']
    result      = entry['result']
    sim_mode_p  = entry['sim_mode']

    # Fix #7 — the per-key latch is already updated at buffer-time
    # (`_cycle_maybe_buffer_fire` → `_v2_reconcile_latch`), BEFORE
    # topic-tier suppression runs, so a suppressed fire's latch is
    # recorded exactly like a survivor's — no separate write needed here.
    if not entry.get('bypass_schedule', False):
        new_status_p   = entry['new_status']
        debounce_min_p = entry.get('debounce_min', 0)
        async with async_session() as session:
            db_values: dict = dict(
                status=new_status_p,
                last_triggered_at=datetime.now(timezone.utc),
                trigger_count=Agent.trigger_count + 1,
            )
            # Phase 21 — clear the debounce latch after a fire.
            if debounce_min_p > 0:
                db_values["condition_first_true_at"] = None
            await session.execute(
                update(Agent).where(Agent.id == agent.id).values(**db_values)
            )
            await session.commit()
        if broadcast_fn:
            broadcast_fn("agent_state", {"slug": agent.slug, "status": new_status_p})

    # Design item 0.C (grammar-unification plan) — a cycle agent whose
    # compiled DO clause produced a genuine `alert(...)` call
    # (`has_explicit_alert=True`, set ONLY by the frontend compiler when
    # `doResult.events.length > 0`) owns its own alert config: skip the
    # automatic kind/threshold rich-alert table and let the normal
    # events.dispatch() call below (which already reads `agent.events`)
    # deliver it instead. `is True` (not a bare truthy check) and the
    # `agent.events` non-empty requirement are both deliberate: a bare
    # `getattr(..., False)` truthy check mis-fires on an unconfigured
    # MagicMock test double (auto-vivifies to a truthy child Mock); the
    # `agent.events` check guards against a stale True flag with an
    # emptied-out events list sending nothing at all — fall back to the
    # rich alert in that case instead. Every currently-saved agent has
    # `has_explicit_alert=False` (server_default='false', backfilled on
    # every pre-existing row), so this is a pure backward-compatible
    # opt-in. See `backend/tests/test_agent_preamble_execution.py`
    # section 7 for the `_LOSS_AGENT_DEFAULTS` evidence of why a bare
    # `bool(agent.events)` check was rejected as the discriminator — a
    # CLAUDE.md entry covering this lands with this item's doc-sync pass.
    use_explicit_alert = (
        getattr(agent, "has_explicit_alert", False) is True
        and bool(agent.events)
    )
    if use_explicit_alert:
        rich_sent = False
    else:
        rich_sent = await _v2_send_rich_alert(
            agent, matches_, now, sim_mode=sim_mode_p, context=context,
            eval_result=result,
        )
    # Rich alert (telegram+email+ntfy table) runs first via alert_utils._dispatch
    # -> _alert_route, which already routes telegram/email/ntfy per
    # alert_routing.agent_alert in backend_config.yaml. If it succeeded, skip
    # all three of those channels in dispatch() below — sending them again
    # there would duplicate every rich-alert fire (confirmed live incident:
    # 4 duplicate ntfy pushes for one MCX pre-close event). log / websocket /
    # inapp always run via dispatch() regardless — they have no rich-path
    # equivalent. The explicit-alert path normally skips nothing here —
    # there is no rich alert to duplicate against, so every channel in
    # `agent.events` (including telegram/email/ntfy) fires through the
    # one normal path.
    #
    # Defense-in-depth, not a confirmed leak: telegram/email/ntfy are
    # `_RECORDED_CHANNELS` (events.py) — they never reach dispatch()'s
    # own direct-send branch regardless of `skip_channels`; they're
    # queued via `_emit_breach_record` into event_agents.py's log-tag
    # pipeline, which has ITS OWN independent sim-notify gate
    # (`dispatch_rows()`'s `extra["sim_mode"]`/`_sim_notify_allowed()`
    # check, keyed off `_emit_breach_record`'s own `sim_mode` field —
    # already correctly suppresses a sim-mode breach record there). This
    # mirrors `_v2_send_rich_alert`'s own belt-and-suspenders double-gate
    # (it ALSO checks `_ae_sim_notify_suppressed` even though the same
    # downstream gate exists) rather than inventing a new pattern — same
    # reasoning as the preamble `alert` entry's identical skip_channels
    # computation in `_cycle_dispatch_preamble_entry`.
    sim_notify_suppressed = use_explicit_alert and _ae_sim_notify_suppressed(agent, sim_mode_p)
    skip = (
        frozenset({'telegram', 'email', 'ntfy'})
        if (rich_sent or sim_notify_suppressed) else frozenset()
    )
    await dispatch(agent, result, broadcast_fn, sim_mode=sim_mode_p, skip_channels=skip,
                   context=context)
    # The explicit-alert path still needs the UI "agent fired" websocket
    # broadcast — dispatch() above only broadcasts a websocket/inapp
    # event when `agent.events` itself names that channel; this is a
    # SEPARATE, always-expected live-feed signal the rich-alert path
    # already provided, which must not go dark just because the operator
    # chose explicit alert config over the automatic table.
    if (rich_sent or use_explicit_alert) and broadcast_fn:
        broadcast_fn('agent_alert', {
            'slug': agent.slug,
            'message': result.condition_text,
            'timestamp': now.isoformat(),
            'sim_mode': sim_mode_p,
        })
    if agent.actions:
        await _ae_dispatch_actions(entry, agent, context, sim_mode_p)


async def _ae_dispatch_actions(entry: dict, agent, context: dict, sim_mode_p: bool) -> None:
    """Run `agent.actions` for one survivor entry — single-execution path
    (today's unconditional behavior, byte-identical) or Sprint 16 fan-out,
    chosen by `_ae_actions_use_row_ref`. Extracted from
    `_ae_dispatch_survivor_entry` to keep it below the complexity gate."""
    if not _ae_actions_use_row_ref(agent.actions):
        # No action in this agent's chain references matched-row data
        # ($row) — fire EXACTLY as before this sprint: once, with
        # account="TOTAL". This is the backward-compatibility guarantee,
        # true BY CONSTRUCTION for every agent that predates Sprint 16
        # (none can contain $row) — see _ae_actions_use_row_ref's
        # docstring for why fan-out is gated on $row usage rather than on
        # condition scope alone (whole-scope "sweep" actions like
        # chase_close_positions / expiry_auto_close must never be
        # re-invoked once per row).
        action_ctx = dict(context)
        action_ctx["account"] = "TOTAL"
        action_ctx["sim_mode"] = sim_mode_p
        await execute(agent, agent.actions, action_ctx)
        return

    # Sprint 16 fan-out — fire agent.actions once PER MATCHED ROW in
    # entry['fire_matches'] (the raw per-row matches whose key passed
    # the escalation gate this tick — see _cycle_maybe_buffer_fire's
    # derivation comment; deliberately NOT entry['matches'], which
    # _cycle_dispatch_survivors' topic merge can extend with a SIBLING
    # agent's rows). Falls back to 'latched_matches'/'matches' only for
    # an entry built by a caller that predates 'fire_matches' (none exist
    # on the real dispatch path today — defensive compatibility guard
    # only).
    fire_matches = entry.get('fire_matches')
    if fire_matches is None:
        fire_matches = entry.get('latched_matches') or entry.get('matches') or []
    fanout = _ae_fanout_matches(fire_matches)
    if not fanout:
        # Defensive only — _cycle_maybe_buffer_fire never buffers an
        # entry with empty latched_matches (`if not effective: return
        # False`), so this is unreachable on the real dispatch path.
        # Guarantees the agent still fires once — today's unconditional
        # behavior — rather than silently firing zero times if some
        # future caller ever violates that invariant.
        fanout = [{}]
    for match_entry in fanout:
        action_ctx = dict(context)
        acct = match_entry.get('account')
        # Classified by scope SHAPE (`_ae_is_aggregate_match`), not by
        # whether `account` happens to be None — see that function's
        # docstring. A genuine per-row match whose scope carries no
        # account dimension at all (e.g. `quote.tracked` — market data
        # belongs to no account) still displays "TOTAL" here (no
        # per-account identity to show) but DOES bind its row below.
        is_aggregate = _ae_is_aggregate_match(match_entry)
        action_ctx["account"] = acct if (not is_aggregate and acct) else "TOTAL"
        action_ctx["sim_mode"] = sim_mode_p
        # Matched-row binding for bare-NAME ($row) action params — see
        # actions.py:resolve_row_ref_params for the marker shape and
        # fail-closed resolution contract. An AGGREGATE match (scope
        # ends in `.total`, or the `not` composite's synthetic entry)
        # never binds a row, even though the underlying TOTAL summary
        # row technically has an 'account' column — a $row reference
        # under an aggregate scope must fail closed (skip + log), never
        # silently resolve to the literal "TOTAL" string.
        action_ctx["_matched_row"] = None if is_aggregate else match_entry.get('row')
        action_ctx["_matched_row_scope"] = None if is_aggregate else match_entry.get('scope')
        await execute(agent, agent.actions, action_ctx)


async def _cycle_dispatch_survivors(
    pending_dispatches: list[dict],
    now,
    context: dict,
    broadcast_fn,
) -> None:
    """Post-loop: apply topic-tier suppression, then dispatch survivors.

    Suppressed agents get an audit-log entry only; no push notification and
    no action execution. Survivor agents commit all side-effects (DB state,
    WS broadcast, rich alert / dispatch, actions).
    """
    suppressed_ids, merge_map = _compute_topic_suppression(pending_dispatches)
    for entry in pending_dispatches:
        agent = entry['agent']
        if agent.id in suppressed_ids:
            await _ae_dispatch_suppressed_entry(entry, suppressed_ids, broadcast_fn)
            continue
        extra = merge_map.get(agent.id)
        if extra:
            # Fix #7 — fold suppressed same-topic siblings' rows into the
            # winner's alert body so extending dedup to equal-tier agents
            # never silently drops a row the operator would otherwise see.
            entry = dict(entry)
            entry['matches'] = list(entry['matches']) + extra
            # 2026-10 fix: stamp ONLY the winner's own escalation-gated
            # subset as latched_matches, not the merged (display-only)
            # list. The merged `extra` rows belong to DIFFERENT agents
            # (different latch slugs) — their own latches were already
            # written correctly at their own _cycle_maybe_buffer_fire
            # call. Re-latching them here under the WINNER's slug would
            # corrupt a sibling agent's re-alert timing on hydration.
            entry['result'] = _v2_build_evalresult(
                entry['matches'], agent.name,
                replay_mode=entry.get('replay_mode', False),
                latched_matches=entry.get('latched_matches'),
            )
        await _ae_dispatch_survivor_entry(entry, now, context, broadcast_fn)


# Tier rank for topic-suppression. Lower = higher priority.
_TIER_RANK = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}


def _ae_has_actions(agent) -> bool:
    """True when the agent carries at least one action.

    Fix #7 — action-bearing agents (e.g. loss-pos-total-auto-close's
    `chase_close_positions` kill-switch) must NEVER be suppressed by
    topic-tier dedup, regardless of tier. Before this fix, a suppressed
    entry ran no action at all (`_ae_dispatch_suppressed_entry` is
    audit-log-only) — if a notify-only critical-tier sibling in the same
    topic happened to win, the auto-close kill-switch would silently
    never fire."""
    return bool(getattr(agent, 'actions', None))


def _ae_topic_winner(group: list[dict]) -> dict:
    """Return the winning entry for a topic group.

    Fix #7 — an action-bearing agent always wins over a notify-only one
    (a kill-switch must always execute), regardless of tier. Among
    entries with the same action-bearing status, the highest-priority
    tier wins (lower `_TIER_RANK`). Ties broken by list order.
    Extracted from _compute_topic_suppression to reduce CC there."""
    def sort_key(e: dict) -> tuple:
        agent = e['agent']
        return (
            0 if _ae_has_actions(agent) else 1,
            _TIER_RANK.get(getattr(agent, 'tier', 'medium'), 99),
        )
    return min(group, key=sort_key)


def _ae_suppressed_in_group(group: list[dict], suppressed: dict,
                            merge_map: dict) -> None:
    """Populate `suppressed` with every non-winner entry in this topic
    group EXCEPT action-bearing ones (fix #7 — never suppress a
    kill-switch), and fold each suppressed entry's matches into
    `merge_map[winner_agent_id]` so the winner's alert body still
    surfaces every row (fix #7 — extending dedup to equal-tier siblings
    must not silently drop information the operator would otherwise see).
    Extracted from _compute_topic_suppression to reduce CC there."""
    winner = _ae_topic_winner(group)
    winner_agent = winner['agent']
    for entry in group:
        agent = entry['agent']
        if agent is winner_agent or _ae_has_actions(agent):
            continue
        suppressed[agent.id] = winner_agent.slug
        merge_map.setdefault(winner_agent.id, []).extend(entry.get('matches') or [])


def _compute_topic_suppression(pending: list[dict]) -> tuple[dict[int, str], dict[int, list]]:
    """
    Given the list of buffered fires from a single run_cycle, return
    (suppressed_agent_id → suppressing_agent_slug, winner_agent_id →
    extra matches merged in from suppressed same-topic siblings).

    Rule (fix #7): within each topic, one entry wins per tick — an
    action-bearing agent always wins over a notify-only one; among
    non-action entries, dedup is extended to cover EQUAL-tier siblings
    too, not just strictly-lower tiers (previously two same-tier agents
    in one topic both pushed separately — prod repro: `loss-rate-acct` +
    `loss-positions-total`, both critical, ~3s apart). Every OTHER
    non-action-bearing entry in the topic is suppressed (dispatch +
    actions skipped), with its matches merged into the winner's alert so
    the wider dedup never silently drops a row. Topic 'general' is
    opt-out — agents at the default tag don't participate, so legacy
    untagged agents behave exactly as before.

    Returns empty dicts when no suppression applies (single-fire ticks,
    all-untagged ticks).
    """
    by_topic: dict[str, list[dict]] = {}
    for entry in pending:
        agent = entry['agent']
        topic = getattr(agent, 'topic', 'general') or 'general'
        if topic == 'general':
            continue  # opt-out — no suppression on the default topic
        by_topic.setdefault(topic, []).append(entry)

    suppressed: dict[int, str] = {}
    merge_map: dict[int, list] = {}
    for topic, group in by_topic.items():
        if len(group) > 1:
            _ae_suppressed_in_group(group, suppressed, merge_map)
    return suppressed, merge_map


# ---------------------------------------------------------------------------
# Agent-id lookup cache + chase terminal event writer
# ---------------------------------------------------------------------------

# Module-level cache: slug → DB id. Avoids a round-trip on every request.
_agent_id_cache: dict[str, int] = {}


async def get_agent_id_by_slug(slug: str) -> int | None:
    """Return the DB id for an agent slug, caching the result.

    Returns None when the slug isn't in the DB yet (e.g. on a fresh deploy
    before seed_agents() has run) so callers can skip the write gracefully.
    """
    if slug in _agent_id_cache:
        return _agent_id_cache[slug]
    try:
        async with async_session() as session:
            row = (await session.execute(
                select(Agent).where(Agent.slug == slug)
            )).scalar_one_or_none()
            if row:
                _agent_id_cache[slug] = row.id
                return row.id
    except Exception as e:
        logger.warning(f"get_agent_id_by_slug({slug!r}): DB lookup failed: {e}")
    return None


async def _get_manual_agent_id() -> int | None:
    """Back-compat shim — delegates to get_agent_id_by_slug('manual')."""
    return await get_agent_id_by_slug("manual")


async def record_manual_event(
    *,
    outcome: str,           # 'action_success' | 'action_failure'
    source: str,            # 'ticket' | 'chain' | 'command' | 'place'
    account: str,
    symbol: str,
    exchange: str,
    side: str,
    qty: int,
    mode: str,              # 'live' | 'paper' | 'draft' | 'shadow'
    order_id: str | None = None,
    error: str | None = None,
) -> None:
    """Write an agent_events row attributed to the 'manual' agent.

    Fire-and-forget: any DB error is logged + swallowed so it cannot
    break the order placement flow.
    """
    import json as _json
    from backend.api.models import AgentEvent

    agent_id = await _get_manual_agent_id()
    if agent_id is None:
        logger.warning(
            "record_manual_event: 'manual' agent not in DB yet "
            f"(will seed on next deploy) — skipping event for {source}/{symbol}"
        )
        return

    detail: dict = {
        "source": source,
        "account": account,
        "symbol": symbol,
        "exchange": exchange,
        "side": side,
        "qty": qty,
        "mode": mode,
    }
    if order_id is not None:
        detail["order_id"] = order_id
    if error is not None:
        detail["error"] = error

    try:
        async with async_session() as session:
            session.add(AgentEvent(
                agent_id=agent_id,
                event_type=outcome,
                trigger_condition=f"manual via {source}",
                detail=_json.dumps(detail),
                sim_mode=False,
            ))
            await session.commit()
    except Exception as e:
        logger.warning(f"record_manual_event: DB write failed: {e}")


async def record_chase_terminal(
    *,
    agent_id: int | None,
    outcome: str,           # chase_fill | chase_unfilled | chase_failed | chase_cancelled
    symbol: str,
    side: str,
    qty: int,
    final_price: float | None = None,
    attempts: int = 0,
    slippage: float | None = None,
    error: str | None = None,
) -> None:
    """Write an AgentEvent row for a terminal chase lifecycle outcome.

    Attributed to the agent that originated the order (via agent_id from
    the AlgoOrder row).  When agent_id is None the write is skipped
    silently — the per-order AlgoOrderEvent timeline still captures the
    outcome via order_events.write_event(), so no information is lost.

    Fire-and-forget: any DB error is logged + swallowed.
    """
    if agent_id is None:
        return

    import json as _json
    from backend.api.models import AgentEvent

    detail: dict = {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "attempts": attempts,
        "outcome": outcome,
    }
    if final_price is not None:
        detail["final_price"] = final_price
    if slippage is not None:
        detail["slippage"] = slippage
    if error is not None:
        detail["error"] = error

    try:
        async with async_session() as session:
            session.add(AgentEvent(
                agent_id=agent_id,
                event_type=outcome,
                trigger_condition=f"chase terminal: {symbol} {side} {qty}",
                detail=_json.dumps(detail),
                sim_mode=False,
            ))
            await session.commit()
    except Exception as e:
        logger.warning(f"record_chase_terminal: DB write failed: {e}")
