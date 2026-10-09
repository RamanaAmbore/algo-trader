"""
Agent Engine — evaluates all active agents using Conditions → Alerts → Actions pipeline.

Called from background.py every refresh cycle with market data context.
Each agent's condition tree is evaluated. If triggered, alerts are dispatched
through configured channels and optional actions are executed.

The engine handles cooldown, state transitions, and WebSocket broadcasts.
"""

import asyncio
import html
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from backend.api.algo.events import dispatch, log_event, EvalResult
from backend.api.algo.actions import execute
from backend.api.algo.agent_evaluator import (
    Context as V2Context, evaluate as v2_evaluate, windowed_rate,
)
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
    """Per-leaf, per-account latch key. `m` is a match/observation dict
    from `agent_evaluator` — has 'metric', 'scope', 'account'."""
    return (agent_slug, m.get('metric'), m.get('scope'), m.get('account') or 'TOTAL')


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
# NSE / BSE / NFO / BFO / CDS → equity segment (09:15-15:30 IST, NSE
#                               holidays).
# MCX                          → commodity segment (09:00-23:30 IST,
#                                MCX holidays).
# Unknown exchanges default to False (safer than wrongly allowing).
_EXCHANGE_TO_SEGMENT = {
    "NSE":  "equity",
    "BSE":  "equity",
    "NFO":  "equity",
    "BFO":  "equity",
    "CDS":  "equity",
    "BCD":  "equity",
    "MCX":  "commodity",
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
      commodity-segment exchanges (MCX)                  → ctx['mcx_open']
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
    scope_label = str(row.get('account', 'TOTAL'))

    underlyings_breakdown, rate_val = _v2_enrich_position_alert(
        section, kind, scope_label, df_positions, alert_state, rate_window_min, rate_val,
    )

    return dict(
        section=section, scope=scope_label, kind=kind,
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


async def _v2_send_rich_alert(agent, matches, now, sim_mode: bool = False,
                              context: dict | None = None):
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
    if (getattr(agent, 'fire_at_time', None)
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
    from backend.shared.helpers.alert_utils import format_notification_header
    subject    = format_notification_header(agent.name, agent.id).splitlines()[0]
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
    "loss-positions-acct":        "high",
    "loss-rate-acct":             "urgent",
    "loss-positions-total":       "urgent",
    "loss-margin-low":            "high",
    "loss-funds-negative":        "urgent",
    "loss-pos-total-auto-close":  "urgent",
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
    Extracted from seed_agents to reduce CC there."""
    from sqlalchemy import delete as sa_delete
    retired = await session.execute(
        select(Agent).where(Agent.is_system.is_(True))
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


def _ae_segment_flags(seg_name: str, seg_cfg: dict, now) -> dict:
    """Compute open/closed/holiday/minutes_since flags for one market segment.

    Returns a flat dict with the ``{prefix}_*`` keys ready to merge into
    the top-level context. Extracted from _build_context to reduce CC there."""
    from backend.brokers.broker_apis import fetch_holidays
    from backend.shared.helpers.date_time_utils import is_trading_day

    h, m = map(int, seg_cfg.get("hours_start", "09:15").split(":"))
    open_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
    h, m = map(int, seg_cfg.get("hours_end", "15:30").split(":"))
    close_time = now.replace(hour=h, minute=m, second=0, microsecond=0)

    prefix = "nse" if seg_name == "equity" else "mcx"
    holiday_exchange = seg_cfg.get("holiday_exchange", "NSE")

    try:
        holidays = fetch_holidays(holiday_exchange)
    except Exception:
        holidays = set()

    is_holiday = now.date() in holidays
    # Passing `now=` lets is_trading_day suppress the live-quote probe
    # when outside the widest Indian market window (09:00-23:30 IST).
    is_trading = is_trading_day(now.date(), holidays,
                                exchange=holiday_exchange,
                                now=now)
    in_time_range = open_time <= now <= close_time
    is_open = in_time_range and is_trading

    mins_open  = (max(0, int((now - open_time).total_seconds() / 60))
                  if now >= open_time and is_open else 0)
    mins_close = (max(0, int((now - close_time).total_seconds() / 60))
                  if now > close_time and is_trading else 0)
    return {
        f"{prefix}_open":    is_open,
        f"{prefix}_closed":  (now > close_time) and is_trading,
        f"{prefix}_holiday": is_holiday,
        f"minutes_since_{prefix}_open":  mins_open,
        f"minutes_since_{prefix}_close": mins_close,
    }


def _build_context(now, sim_overrides: dict | None = None) -> dict:
    """
    Build the base context dict consumed by the schedule/market-open check
    in run_cycle. The v2 grammar engine reads the market DataFrames directly
    via V2Context, so this function only emits the per-segment open/close
    flags used to short-circuit `market_hours` agents.

    `sim_overrides` (optional) is the simulator's way to pretend the clock
    is somewhere it isn't. When non-None, keys in the override dict win
    over the computed values — so a scenario can declare "NSE is open, 30
    minutes before close, today is an expiry day" regardless of wall-clock
    time. Expected keys:

        nse_open / nse_closed / nse_holiday / mcx_open / mcx_closed / mcx_holiday (bool)
        minutes_since_nse_open / minutes_since_nse_close
        minutes_since_mcx_open / minutes_since_mcx_close   (int)
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
        spot_prices=context.get("spot_prices") or {},
        log_records=(context.get("log_records_by_agent") or {}).get(agent.id, []),
        alert_state=alert_state,
        now=now,
        segments=context.get("segments", []),
        rate_window_min=cfg['rate_window_min'],
        agent=agent,
        baseline_live=baseline_live,
    )
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
    else:
        cooldown_min = getattr(agent, 'cooldown_minutes', None) or cfg['cooldown_min']
        store = alert_state.setdefault('_sim_latch', {}) if (sim_mode or replay_mode) else None
        effective = _v2_apply_escalation_gate(
            agent, matches, now, cooldown_min, store=store,
        )
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

    rich_sent = await _v2_send_rich_alert(
        agent, matches_, now, sim_mode=sim_mode_p, context=context,
    )
    # Rich alert (telegram+email+ntfy table) runs first via alert_utils._dispatch
    # -> _alert_route, which already routes telegram/email/ntfy per
    # alert_routing.agent_alert in backend_config.yaml. If it succeeded, skip
    # all three of those channels in dispatch() below — sending them again
    # there would duplicate every rich-alert fire (confirmed live incident:
    # 4 duplicate ntfy pushes for one MCX pre-close event). log / websocket /
    # inapp always run via dispatch() regardless — they have no rich-path
    # equivalent.
    skip = frozenset({'telegram', 'email', 'ntfy'}) if rich_sent else frozenset()
    await dispatch(agent, result, broadcast_fn, sim_mode=sim_mode_p, skip_channels=skip)
    if rich_sent and broadcast_fn:
        broadcast_fn('agent_alert', {
            'slug': agent.slug,
            'message': result.condition_text,
            'timestamp': now.isoformat(),
            'sim_mode': sim_mode_p,
        })
    if agent.actions:
        action_ctx = dict(context)
        action_ctx["account"] = "TOTAL"
        action_ctx["sim_mode"] = sim_mode_p
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
