"""
Agent Engine — evaluates all active agents using Conditions → Alerts → Actions pipeline.

Called from background.py every refresh cycle with market data context.
Each agent's condition tree is evaluated. If triggered, alerts are dispatched
through configured channels and optional actions are executed.

The engine handles cooldown, state transitions, and WebSocket broadcasts.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from backend.api.algo.events import dispatch, log_event, EvalResult
from backend.api.algo.actions import execute
from backend.api.algo.agent_evaluator import Context as V2Context, evaluate as v2_evaluate
from backend.api.database import async_session
from backend.api.models import Agent
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config as app_config

logger = get_logger(__name__)


# Module-level per-agent suppression state for v2-grammar agents.
# Keyed by agent slug: {'ts': datetime, 'pnl': float, 'pct': float}.
# Survives across ticks but is wiped daily by _maybe_reset_v2_state below.
_V2_LAST_ALERT: dict[str, dict] = {}
_V2_LAST_RESET_DATE = None


def _maybe_reset_v2_state(today):
    """Wipe v2 suppression state once per new trading day."""
    global _V2_LAST_RESET_DATE
    if _V2_LAST_RESET_DATE != today:
        _V2_LAST_RESET_DATE = today
        _V2_LAST_ALERT.clear()


# Max samples per (section, scope) bucket in alert_state['pnl_history'].
# At a 5-minute background refresh that's 200 × 5 = 1000 minutes of
# history per bucket — well past any rate window we ship. At the 2-second
# simulator tick it's ~400 seconds (6+ minutes), comfortably above the
# default 10-minute rate window for fabricated sim runs.
_PNL_HISTORY_CAP = 200


def _update_pnl_history(alert_state: dict, now, sum_positions, sum_holdings) -> None:
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
    """
    today = now.date() if hasattr(now, 'date') else None
    last_date = alert_state.get('session_date')
    if today and last_date != today:
        alert_state['pnl_history'] = {}
        alert_state['session_date'] = today
    hist_map = alert_state.setdefault('pnl_history', {})

    def _append(section: str, df):
        if df is None or getattr(df, 'empty', True):
            return
        if 'account' not in getattr(df, 'columns', []):
            return
        for _, row in df.iterrows():
            acct = str(row.get('account', '') or '')
            if not acct:
                continue
            try:
                pnl = float(row.get('pnl', 0) or 0)
            except (TypeError, ValueError):
                continue
            # pnl_pct is optional — holdings always have it; positions
            # carry a per-row pnl_percentage too. Fall back to None
            # when not present so the field_idx=2 path returns None.
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
            if start <= end:
                # Same-day window: in if start ≤ cur ≤ end.
                if start <= cur_min <= end:
                    return True
            else:
                # Crossing midnight: in if cur ≥ start OR cur ≤ end.
                if cur_min >= start or cur_min <= end:
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


def _v2_has_rate_metric(cond) -> bool:
    """
    Walk the tree looking for any leaf whose metric is a rate_* metric. When
    present, the engine applies the opening-gap baseline gate to the whole
    agent. This keeps the per-agent config simple — operator does not have
    to set a baseline flag; the engine infers it from the tree.
    """
    if not isinstance(cond, dict):
        return False
    for key in ('all', 'any'):
        if key in cond:
            return any(_v2_has_rate_metric(c) for c in (cond.get(key) or []))
    if 'not' in cond:
        return _v2_has_rate_metric(cond['not'])
    m = cond.get('metric', '') or ''
    return '_rate_' in m


def _v2_baseline_live(alert_state, now, offset_min: float) -> bool:
    start = alert_state.get('session_start') if alert_state else None
    if not start:
        return False
    from datetime import timedelta
    return (now - start) >= timedelta(minutes=offset_min)


def _v2_build_evalresult(matches, agent_name: str) -> EvalResult:
    """
    Wrap v2 matches into an EvalResult so the existing dispatch() function
    (which renders the Telegram/email body) can consume them unchanged.
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
    return EvalResult(
        triggered=bool(matches),
        condition_text=condition_text,
        detail={'matches': matches, 'grammar': 'v2'},
    )


# ─── v2 rich-body Telegram + email ────────────────────────────────────────
#
# For v2 agents we bypass the generic dispatch() body and use the same
# narrow-mobile Telegram format + coloured HTML email table that the legacy
# alert_utils engine already produces. Keeping the user-facing shape
# consistent across both engines makes parity testing trivial — the
# operator can spot-diff two messages and only care about the agent slug.


def _v2_extract_pnl_fields(row: dict, section: str, metric: str,
                           value) -> tuple[float, float | None]:
    """Return (pnl, pct) appropriate for the given section.

    - Holdings  → day_change_val + day_change_percentage
    - Positions → pnl; pct stays None (computed later when used_margin is known)
    - Funds     → metric-driven field; pct always None
    """
    if section == 'Holdings':
        pnl: float = float(row.get('day_change_val', 0) or 0)
        pct: float | None = (
            float(row.get('day_change_percentage', 0) or 0)
            if row.get('day_change_percentage') is not None else None
        )
    elif section == 'Positions':
        pnl = float(row.get('pnl', 0) or 0)
        pct = None  # computed later only when we have used_margin
    else:  # Funds
        if metric == 'cash':
            pnl = float(row.get('avail opening_balance', 0) or 0)
        elif metric == 'avail_margin':
            pnl = float(row.get('net', 0) or 0)
        else:
            pnl = float(value or 0)
        pct = None
    return pnl, pct


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
    row       = match.get('row')      or {}
    value     = match.get('value')
    threshold = match.get('threshold')

    # section — derived from scope token prefix
    if scope_tok.startswith('holdings'):
        section = 'Holdings'
    elif scope_tok.startswith('positions'):
        section = 'Positions'
    else:
        section = 'Funds'

    # kind — derived from metric token. Drives row colour / label.
    if   metric in ('cash',):                kind = 'negative_cash'
    elif metric in ('avail_margin',):        kind = 'negative_margin'
    elif '_rate_abs' in metric:              kind = 'rate_abs'
    elif '_rate_pct' in metric:              kind = 'rate_pct'
    elif metric.endswith('_pct') or metric == 'pnl_pct':  kind = 'static_pct'
    else:                                    kind = 'static_abs'

    # pnl — section-appropriate ₹ value. For rate alerts we still want the
    # current raw pnl/day_val shown, plus the rate value in rate_val.
    pnl, pct = _v2_extract_pnl_fields(row, section, metric, value)

    rate_val = value if kind in ('rate_abs', 'rate_pct') else None

    # threshold display — format with units appropriate to the kind.
    # kind is exhaustively one of {static_pct, rate_pct, static_abs,
    # rate_abs, negative_cash, negative_margin}; no else branch needed.
    try:
        thr = float(threshold)
        if kind in ('static_pct', 'rate_pct'):
            thr_str = f"{thr:.2f}%" + ("/min" if kind == 'rate_pct' else "")
        else:
            thr_str = f"-₹{abs(thr):,.0f}" + ("/min" if kind == 'rate_abs' else "")
    except Exception:
        thr_str = str(threshold)

    scope_label = str(row.get('account', 'TOTAL'))

    # ── Optional enrichment for position alerts ────────────────────────
    # 1) Per-underlying breakdown — operator wants to see `NIFTY -₹22k ·
    #    BANKNIFTY -₹13k` alongside the bare account total. Honours the
    #    `alerts.show_underlying_breakdown` and `alerts.max_underlyings_per_alert`
    #    settings, with fallbacks so a settings-cache miss doesn't block.
    # 2) Rate-of-loss enrichment for STATIC alerts — rate-based metrics
    #    already populate rate_val above. For static_pct / static_abs we
    #    reach into alert_state's pnl_history (same source the rate
    #    metrics use) and compute ΔP&L over the rate window.
    underlyings_breakdown: list[dict] = []
    if section == 'Positions' and df_positions is not None:
        try:
            from backend.shared.helpers.settings import get_bool, get_int
            from backend.shared.helpers.summarise import (
                breakdown_positions_by_underlying,
            )
            if get_bool('alerts.show_underlying_breakdown', True):
                top_n = get_int('alerts.max_underlyings_per_alert', 5)
                underlyings_breakdown = breakdown_positions_by_underlying(
                    df_positions, account=scope_label, top_n=top_n,
                )
        except Exception as e:
            logger.warning(f"underlying breakdown failed: {e}")

    # Compute rate for static position alerts on the same (section, scope)
    # bucket the rate metrics use. alert_state is keyed by ('positions',
    # scope) tuple per agent_evaluator.Context._compute_rate.
    if (section == 'Positions' and rate_val is None and alert_state
            and kind in ('static_pct', 'static_abs')):
        try:
            from backend.shared.helpers.settings import get_bool
            if get_bool('alerts.show_rate_in_static_alerts', True):
                hist = (alert_state.get('pnl_history') or {}).get(
                    ('positions', scope_label), []
                ) or []
                if len(hist) >= 2:
                    cutoff_window = hist[-1][0] - timedelta(minutes=rate_window_min)
                    window = [s for s in hist if s[0] >= cutoff_window]
                    if len(window) >= 2:
                        oldest, latest = window[0], window[-1]
                        mins = (latest[0] - oldest[0]).total_seconds() / 60.0
                        if mins > 0:
                            # field_idx=1 → pnl ₹/min, matching rate_abs metric
                            rate_val = (latest[1] - oldest[1]) / mins
        except Exception as e:
            logger.warning(f"static-alert rate enrichment failed: {e}")

    return dict(
        section=section, scope=scope_label, kind=kind,
        pnl=pnl, pct=pct, rate_val=rate_val, threshold=thr_str,
        underlyings_breakdown=underlyings_breakdown,
    )


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
        _tg_alert_body, _email_alert_body, _dispatch,
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

    # Sort Holdings → Positions → Funds, per-account before TOTAL (same as
    # alert_utils).
    order = {'Holdings': 0, 'Positions': 1, 'Funds': 2}
    rows.sort(key=lambda r: (order.get(r['section'], 9),
                              0 if r['scope'] != 'TOTAL' else 1,
                              r['scope']))

    # `simulator.notify_during_run` gate — when off (default), skip the
    # outbound Telegram + email send for sim_mode fires. The agent_event
    # row + log line are still written by the caller (_v2_record, log_event)
    # so the audit trail is complete; only the noisy channels are
    # suppressed. Operator opts in by toggling the setting per run when
    # they want the full live-style feedback (e.g. validating dispatch).
    if sim_mode:
        try:
            from backend.shared.helpers.settings import get_bool
            if not get_bool("simulator.notify_during_run", False):
                logger.info(
                    f"[SIM] notify_during_run=off — skipped TG/email for "
                    f"agent {agent.slug}; event row + log line still written"
                )
                # Treat as "rich path attempted" so the caller doesn't fall
                # through to the bare dispatch() that ignores the setting.
                return True
        except Exception:
            pass  # if the setting lookup itself fails, fall through to send

    tg_body    = _tg_alert_body(rows)
    email_html = _email_alert_body(rows)
    subject    = f"Agent {agent.slug}"
    mode_tag   = '' if sim_mode else _agent_execution_mode_tag(agent)
    try:
        await asyncio.to_thread(
            _dispatch, 'alert', timestamp_display(), tg_body, email_html, subject,
            sim_mode=sim_mode, mode_tag=mode_tag,
        )
    except Exception as e:
        logger.error(f"Agent [{agent.slug}] rich alert send failed: {e}")
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


def _v2_should_suppress(agent, matches, now, cfg) -> bool:
    """
    Per-agent suppression for v2 grammar.

    Two semantics depending on whether the agent uses a rate metric:

    - **Static agents** (threshold floors like `pnl <= -30000` or `day_pct <= -3`)
      latch on first fire. They stay silent for the rest of the session as
      long as the condition keeps matching. They re-arm ONLY when a cycle
      sees zero matches (caller clears the latch in that case), i.e. the
      value has recovered above the threshold. This prevents the "same
      breach keeps screaming every tick" behaviour operators saw in the
      simulator and in real-market prolonged drawdowns.

    - **Rate agents** (ΔP&L/Δmin): keep the cooldown + material-delta logic.
      Rate rules are *meant* to re-fire when the bleed accelerates — that's
      the whole point — so we gate on cooldown elapsed + |Δvalue| material.
    """
    from datetime import timedelta

    # Use the WORST (smallest / most-negative) value across matches as the
    # representative loss number for delta comparisons.
    worst_val = None
    for m in matches:
        v = m.get('value')
        if v is None:
            continue
        if worst_val is None or v < worst_val:
            worst_val = v

    prev = _V2_LAST_ALERT.get(agent.slug)
    if not prev:
        return False

    # Static agents: latched since the last fire. Re-fire blocked until the
    # latch is cleared by run_cycle on a no-match tick (see below).
    if not _v2_has_rate_metric(agent.conditions):
        return True

    # Rate agents: cooldown + material delta.
    if worst_val is None:
        return False
    if (now - prev['ts']) < timedelta(minutes=cfg['cooldown_min']):
        return True
    abs_moved = abs(worst_val - prev.get('val', 0)) >= cfg['suppress_delta_abs']
    return not abs_moved


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


def _v2_record(agent, matches, now) -> None:
    worst_val = None
    for m in matches:
        v = m.get('value')
        if v is None:
            continue
        if worst_val is None or v < worst_val:
            worst_val = v
    _V2_LAST_ALERT[agent.slug] = {'ts': now, 'val': worst_val if worst_val is not None else 0.0}


def _v2_unlatch(agent) -> None:
    """
    Clear the static-agent latch so the agent is armed for its next fire.
    Called by run_cycle on any tick where the agent produced zero matches —
    i.e. the condition has recovered. Safe to call unconditionally; no-op
    if the agent was never latched.
    """
    _V2_LAST_ALERT.pop(agent.slug, None)


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
             "loss thresholds: -2% of margin OR -₹30k OR -₹3k/min OR "
             "-0.25 %/min. One agent per topic; alert detail names the "
             "matched threshold."
         ),
         conditions={"any": [
             {"metric": "pnl_pct",      "scope": "positions.any_acct", "op": "<=", "value": -2.0},
             {"metric": "pnl",          "scope": "positions.any_acct", "op": "<=", "value": -30000},
             {"metric": "pnl_rate_abs", "scope": "positions.any_acct", "op": "<=", "value": -3000},
             {"metric": "pnl_rate_pct", "scope": "positions.any_acct", "op": "<=", "value": -0.25},
         ]},
         scope="total",
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
             {"metric": "pnl_pct",      "scope": "positions.total", "op": "<=", "value": -2.0},
             {"metric": "pnl",          "scope": "positions.total", "op": "<=", "value": -50000},
             {"metric": "pnl_rate_abs", "scope": "positions.total", "op": "<=", "value": -6000},
             {"metric": "pnl_rate_pct", "scope": "positions.total", "op": "<=", "value": -0.25},
         ]},
         scope="total",
         ),

    # ── Holdings: per-account guardrail (high tier) ─────────────────────
    dict(slug="loss-holdings-acct",
         long_name="when:holdings.any_acct.day<=acct-thresholds   alert:high/tg+email+log   do:notify-only",
         tier="high",
         topic="holdings_loss",
         name="Holdings per-account loss guardrail",
         description=(
             "Fires when ANY account's holdings trip per-account loss "
             "thresholds: -3% day OR -₹2k/min OR -0.15 %/min."
         ),
         conditions={"any": [
             {"metric": "day_pct",      "scope": "holdings.any_acct", "op": "<=", "value": -3.0},
             {"metric": "day_rate_abs", "scope": "holdings.any_acct", "op": "<=", "value": -2000},
             {"metric": "day_rate_pct", "scope": "holdings.any_acct", "op": "<=", "value": -0.15},
         ]},
         scope="total",
         ),

    # ── Holdings: total guardrail (critical tier) ───────────────────────
    dict(slug="loss-holdings-total",
         long_name="when:holdings.total.day<=total-thresholds   alert:critical/tg+email+log   do:notify-only",
         tier="critical",
         topic="holdings_loss",
         name="Holdings total loss guardrail",
         description=(
             "Fires when book-wide holdings trip total loss thresholds: "
             "-5% day OR -₹4k/min OR -0.15 %/min."
         ),
         conditions={"any": [
             {"metric": "day_pct",      "scope": "holdings.total", "op": "<=", "value": -5.0},
             {"metric": "day_rate_abs", "scope": "holdings.total", "op": "<=", "value": -4000},
             {"metric": "day_rate_pct", "scope": "holdings.total", "op": "<=", "value": -0.15},
         ]},
         scope="total",
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
_LOSS_AGENT_DEFAULTS = dict(
    events=[
        {"channel": "telegram", "enabled": True},
        {"channel": "email",    "enabled": True},
        {"channel": "log",      "enabled": True},
    ],
    actions=[],                 # notify-only. Attach actions via admin UI later.
    schedule="market_hours",
    cooldown_minutes=30,
    status="active",            # v2 grammar is now the sole loss-alert engine
)

for _a in _LOSS_AGENTS:
    for _k, _v in _LOSS_AGENT_DEFAULTS.items():
        _a.setdefault(_k, _v)
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
# these agents fire at 14:30 (NFO) / 23:00 (MCX) — different times,
# no collision.
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
         long_name="when:positions.expiring_today.nfo.is_itm==1 @15:00   alert:critical/tg+email+log   do:expiry-auto-close(NFO)",
         tier="critical",
         topic="expiry_warning",
         name="Auto-close ITM equity options on expiry day (T-30min)",
         description=(
             "At 15:00 IST (30 min before NSE 15:30 close) on expiry "
             "day, chase-close EVERY ITM equity option (NFO). Equity "
             "rules: hedged or not, every ITM contract must be "
             "closed before expiry — Zerodha does not net-settle "
             "NFO option pairs and physical settlement / STT on ITM "
             "longs is the trap. Wraps the ExpiryEngine scan+close, "
             "restricted to NFO. Ships INACTIVE (destructive)."
         ),
         conditions={"all": [
             {"metric": "is_itm",
              "scope":  "positions.expiring_today.nfo",
              "op":     "==", "value": 1.0},
         ]},
         scope="total",
         schedule="market_hours",
         fire_at_time="15:00",
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
    ],
    actions=[],
)
for _a in _EXPIRY_AGENTS:
    for _k, _v in _EXPIRY_AGENT_DEFAULTS.items():
        _a.setdefault(_k, _v)
BUILTIN_AGENTS.extend(_EXPIRY_AGENTS)


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
    ],
    actions=[],
    scope="manual",
    schedule="never",          # never picked up by run_cycle
    cooldown_minutes=0,
    status="active",
)
BUILTIN_AGENTS.append(MANUAL_AGENT)


async def seed_agents():
    """
    Sync BUILTIN_AGENTS into the `agents` table.

    - Insert system agents that don't exist yet.
    - For existing system rows, force-sync `schedule` and `status` so the
      engine state converges on the current code definition. User-tuned
      conditions/cooldown/events/actions are preserved.
    - Delete orphan system rows whose slug is no longer in BUILTIN_AGENTS
      (retired built-ins after the v1→v2 cutover).
    """
    from sqlalchemy import delete
    from backend.api.models import AgentEvent

    builtin_slugs = {a["slug"] for a in BUILTIN_AGENTS}

    async with async_session() as session:
        for agent_def in BUILTIN_AGENTS:
            result = await session.execute(
                select(Agent).where(Agent.slug == agent_def["slug"])
            )
            existing = result.scalar_one_or_none()
            if existing:
                # Sync `long_name` from code unconditionally — it's a
                # structured descriptor owned by the built-in definition,
                # not an operator-editable field. New built-ins get it
                # populated on first deploy; renames propagate.
                code_long = agent_def.get("long_name")
                if code_long and existing.long_name != code_long:
                    existing.long_name = code_long
                if existing.schedule != agent_def.get("schedule", "market_hours"):
                    existing.schedule = agent_def.get("schedule", "market_hours")
                # Sync tier + topic from the built-in definition. These
                # drive topic-scoped suppression in run_cycle; keep the
                # DB row in sync with code so a deploy that re-tags an
                # agent takes effect. We only overwrite when the row is
                # still at the schema default — if the operator has
                # chosen a non-default value via the UI, leave it.
                _def_tier = agent_def.get("tier", "medium")
                if existing.tier == "medium" and _def_tier != "medium":
                    existing.tier = _def_tier
                _def_topic = agent_def.get("topic", "general")
                if existing.topic == "general" and _def_topic != "general":
                    existing.topic = _def_topic
                desired_status = agent_def.get("status")
                # System agents track the code definition bidirectionally:
                # the built-in list is the source of truth for default on/off
                # state. Users can still toggle via the UI — the next deploy
                # will re-sync if the code changes.
                if desired_status and existing.status != desired_status:
                    # Only force-sync when the row is still at the opposite
                    # default; preserves a just-toggled user choice between
                    # deploys.
                    if desired_status == "active" and existing.status == "inactive":
                        existing.status = "active"
                    elif desired_status == "inactive" and existing.status == "active":
                        existing.status = "inactive"
                continue
            agent = Agent(
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
                status=agent_def.get("status", "active"),
                is_system=True,
            )
            session.add(agent)

        # Prune retired system agents (v1 rules that no longer have a code
        # definition). Leaves user-authored (is_system=False) rows alone.
        retired = await session.execute(
            select(Agent).where(Agent.is_system.is_(True))
        )
        for row in retired.scalars().all():
            if row.slug not in builtin_slugs:
                logger.info(f"Agent engine: pruning retired built-in '{row.slug}'")
                # agent_events.agent_id is ON DELETE CASCADE (slice G), so
                # the child rows tear down with the parent.
                await session.execute(delete(Agent).where(Agent.id == row.id))

        await session.commit()
    logger.info(f"Agent engine: {len(BUILTIN_AGENTS)} built-in agents verified")


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

    ctx = {"now": now}

    # Market state per segment (with holiday awareness)
    from backend.brokers.broker_apis import fetch_holidays

    segments = app_config.get("market_segments", {})
    for seg_name, seg_cfg in segments.items():
        h, m = map(int, seg_cfg.get("hours_start", "09:15").split(":"))
        open_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        h, m = map(int, seg_cfg.get("hours_end", "15:30").split(":"))
        close_time = now.replace(hour=h, minute=m, second=0, microsecond=0)

        prefix = "nse" if seg_name == "equity" else "mcx"
        holiday_exchange = seg_cfg.get("holiday_exchange", "NSE")

        # Check if today is a holiday or weekend
        try:
            holidays = fetch_holidays(holiday_exchange)
        except Exception:
            holidays = set()

        # Use the shared trading-day helper so Muhurat / special weekend
        # sessions configured via `market.extra_trading_days` register
        # as open. Passing `exchange=` also enables the live-quote probe
        # — if Kite shows fresh ticks for this exchange's bellwether,
        # the day is treated as open regardless of what the NSE/Kite
        # holiday master says. Catches MCX evening sessions on equity
        # holidays that the COM segment doesn't surface.
        from backend.shared.helpers.date_time_utils import is_trading_day
        is_holiday = now.date() in holidays
        # Passing `now=` lets is_trading_day suppress the probe when
        # we're outside the widest Indian market window (09:00-23:30
        # IST), so a 3 AM tick doesn't fire a useless Kite quote call.
        is_trading = is_trading_day(now.date(), holidays,
                                    exchange=holiday_exchange,
                                    now=now)
        # is_weekend is retained for the legacy `*_closed` semantics
        # (which want "session ended today" — only meaningful on a
        # day when the market actually traded). A trading Saturday
        # therefore correctly emits *_closed once we pass close_time.
        is_non_trading_weekend = not is_trading and now.weekday() >= 5
        in_time_range = open_time <= now <= close_time
        is_open = in_time_range and is_trading

        ctx[f"{prefix}_open"] = is_open
        ctx[f"{prefix}_closed"] = (now > close_time) and is_trading
        ctx[f"{prefix}_holiday"] = is_holiday
        ctx[f"minutes_since_{prefix}_open"] = max(0, int((now - open_time).total_seconds() / 60)) if now >= open_time and is_open else 0
        ctx[f"minutes_since_{prefix}_close"] = max(0, int((now - close_time).total_seconds() / 60)) if now > close_time and is_trading else 0

    # Sim-mode overrides — a scenario's `market_state` block wins over the
    # computed values above. Only keys present in the override dict are
    # replaced, so a partial override (e.g. just `is_expiry_day`) is safe.
    if sim_overrides:
        for k, v in sim_overrides.items():
            ctx[k] = v

    return ctx


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

    # Append the current P&L snapshot to alert_state.pnl_history so the
    # rate evaluator has samples to compute ΔP&L/min against. The
    # background performance task and the simulator both pass the same
    # long-lived `alert_state` dict, so each run_cycle call grows the
    # history one entry per (section, scope) bucket.
    _alert_state = context.get("alert_state")
    if _alert_state is not None:
        _update_pnl_history(
            _alert_state, now,
            context.get("sum_positions"),
            context.get("sum_holdings"),
        )

    # Tier-suppression buffer — fires accumulate here during the per-agent
    # loop, then a single post-loop pass computes topic-scoped suppression
    # and dispatches the survivors. State mutations (cooldown latch,
    # lifespan shadow) still happen inline so cross-tick semantics are
    # unchanged; only the push notification + action execution defer.
    # Each entry: {agent, matches, result, sim_mode, alert_state}.
    pending_dispatches: list[dict] = []

    # Load agents. Three distinct semantics for `only_agent_ids`:
    #   - None        → run every active / cooldown agent (default live path)
    #   - [id1, id2]  → run ONLY those agents, regardless of DB status
    #                   (simulator "Run in Simulator" + multi-agent sim)
    #   - []          → run NO agents — pure market-scenario explorer
    #                   (sim with positions only; no triggers, no orders)
    async with async_session() as session:
        if only_agent_ids is not None:
            if not only_agent_ids:
                agents = []   # empty list → explicit "no agents"
            else:
                result = await session.execute(
                    select(Agent).where(Agent.id.in_(only_agent_ids))
                )
                agents = result.scalars().all()
        else:
            result = await session.execute(
                select(Agent).where(Agent.status.in_(["active", "cooldown"]))
            )
            agents = result.scalars().all()

    if not agents:
        return

    # Build base context. When the simulator passes a `market_state`
    # override dict on the context, forward it so the per-segment open
    # flags reflect the simulated clock (e.g. "pre_close" preset) instead
    # of real wall-clock time.
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

    # Hoist _v2_cfg() outside the per-agent loop — it reads global Settings
    # rows and has no per-agent dependency. Avoids 15 redundant dict lookups
    # per run_cycle tick.
    cfg = _v2_cfg()

    for agent in agents:
        # Lifespan deadline — auto-complete `until_date` agents whose
        # expiry has passed. Done before any other gates so a stale
        # agent doesn't fire on its last tick. Sim runs (bypass_schedule)
        # never mutate agent state, so the deadline check is gated.
        if (not bypass_schedule
                and getattr(agent, "lifespan_type", "persistent") == "until_date"
                and agent.lifespan_expires_at
                and now >= agent.lifespan_expires_at):
            async with async_session() as session:
                await session.execute(
                    update(Agent).where(Agent.id == agent.id).values(status="completed")
                )
                await session.commit()
            if broadcast_fn:
                broadcast_fn("agent_state", {"slug": agent.slug, "status": "completed"})
            continue

        # schedule="never" agents (e.g. the "manual" audit agent) must
        # never be evaluated by run_cycle — they only receive events written
        # explicitly via record_manual_event.
        if agent.schedule == "never":
            continue

        # Enforce schedule: "market_hours" agents only run while some market
        # is open — unless the caller asked to bypass (isolated sim test).
        if (not bypass_schedule
                and agent.schedule == "market_hours" and not any_market_open):
            continue
        # Check cooldown (also skippable during isolated sim runs)
        if agent.status == "cooldown" and not bypass_schedule:
            if agent.last_triggered_at:
                elapsed = (datetime.now(timezone.utc) - agent.last_triggered_at).total_seconds() / 60
                if elapsed < agent.cooldown_minutes:
                    continue

        # fire_at_time gate — when set ("HH:MM" IST), the agent only
        # evaluates if the current IST wall-clock falls inside a small
        # window around that time. The window is the run_cycle poll
        # interval + 60 s slack so a 5-minute poll cadence still
        # reliably catches the slot. Bypass on sim runs so the
        # "Run in Simulator" button never has to wait for wall-clock.
        if not bypass_schedule and getattr(agent, "fire_at_time", None):
            if not _fire_at_window_active(agent.fire_at_time, now,
                                          window_sec=int(get_int('alerts.fire_at_window_sec', 360))):
                continue

        # ── Phase 22 — blackout windows ─────────────────────────────────
        # When the current IST wall-clock falls INSIDE any configured
        # blackout window, skip this agent entirely. Operators use this
        # for "no alerts during 12:00-13:00 lunch" or "muted during
        # scheduled deploy window". Sim bypasses so operators can test
        # against any time. Industry analogue: Datadog `mute_until`,
        # PagerDuty maintenance windows.
        if not bypass_schedule:
            blackouts = getattr(agent, "blackout_windows", None) or []
            if blackouts and _in_blackout_window(now, blackouts):
                continue

        # v2 grammar dispatch: metric/scope leaves or all/any/not composites
        # go through backend.api.algo.agent_evaluator. Baseline gate and
        # suppression are applied here rather than inside the evaluator so
        # the evaluator stays a pure tree walker.
        alert_state = context.get("alert_state") or {}
        # `sim_mode` is set by the simulator; it flows through V2Context and
        # tags every downstream artefact (Telegram, email, agent_events,
        # algo_orders) with a SIMULATOR marker so real and simulated fires
        # can't be confused in the logs or the group chat.
        sim_mode = bool(alert_state.get("sim_mode") or context.get("sim_mode"))
        _maybe_reset_v2_state(now.date() if hasattr(now, 'date') else None)
        triggered = False

        # Baseline gate: skip every rate-based agent for the first N min
        # of the session to avoid the opening-gap firing rate alerts. The
        # isolated-sim path bypasses this so operators can test rate rules
        # without waiting 15 minutes of simulated time.
        if (not bypass_schedule
                and _v2_has_rate_metric(agent.conditions)
                and not _v2_baseline_live(alert_state, now, cfg['baseline_offset_min'])):
            continue

        v2_ctx = V2Context(
            sum_holdings=context.get("sum_holdings"),
            sum_positions=context.get("sum_positions"),
            df_margins=context.get("df_margins"),
            watchlist_rows=context.get("watchlist_rows") or [],
            # Phase 25 — expiry agents read per-symbol rows + underlying
            # spot prices via these context fields. Callers (background
            # tasks, simulator, dry-run) populate them when available;
            # absent ⇒ expiry-* metric resolvers return None gracefully.
            position_rows=context.get("position_rows") or [],
            spot_prices=context.get("spot_prices") or {},
            alert_state=alert_state,
            now=now,
            segments=context.get("segments", []),
            rate_window_min=cfg['rate_window_min'],
            agent=agent,
        )

        try:
            matches = v2_evaluate(agent.conditions, v2_ctx)
        except Exception as e:
            logger.error(f"Agent [{agent.slug}] v2 evaluate failed: {e}")
            matches = []

        # No matches this tick — clear any static-agent latch so the agent
        # re-arms for a future re-breach. This is the other half of the
        # latching semantic defined in `_v2_should_suppress`.
        if not matches:
            _v2_unlatch(agent)

        # ── Phase 21 — debounce gate ("for N minutes" clauses) ─────────────
        # If debounce_minutes > 0, the condition must hold for that many
        # consecutive minutes before the agent fires. Eliminates spike-
        # driven false positives (a single quote glitch no longer trips
        # a 30-min cooldown). Industry pattern: Datadog `For:`, Grafana
        # `For:`, CloudWatch `EvaluationPeriods`.
        #
        # State machine on agent.condition_first_true_at:
        #   match=False              → reset to NULL (re-arm)
        #   match=True + ts=NULL     → set to `now`, suppress this fire
        #   match=True + ts<window   → still inside the window, suppress
        #   match=True + ts≥window   → window crossed, fire normally
        debounce_min = int(getattr(agent, "debounce_minutes", 0) or 0)
        debounce_first_true_changed = False
        debounce_new_first_true_at = agent.condition_first_true_at
        if debounce_min > 0 and not sim_mode:
            # Sim runs bypass debounce — operators iterating in the
            # simulator shouldn't wait N minutes of fake time per fire.
            if not matches:
                if agent.condition_first_true_at is not None:
                    debounce_new_first_true_at = None
                    debounce_first_true_changed = True
            else:
                if agent.condition_first_true_at is None:
                    # First true tick — start the clock, don't fire yet.
                    debounce_new_first_true_at = now
                    debounce_first_true_changed = True
                    logger.info(
                        f"Agent [{agent.slug}] debounce armed "
                        f"({debounce_min}m); waiting for sustained condition"
                    )
                    matches = []
                else:
                    elapsed_min = (now - agent.condition_first_true_at).total_seconds() / 60.0
                    if elapsed_min < debounce_min:
                        # Still inside the window — suppress.
                        matches = []
                    # else: window crossed; condition_first_true_at stays
                    # set (the cooldown latch / suppression will clear it
                    # naturally after the fire commit).

        # Shadow-lifespan gate (sim mode only): every iteration the
        # simulator seeds a per-agent shadow `trigger_count` from the
        # agent's REAL DB row. Each sim fire decrements the shadow;
        # when it hits 0 (or until_date passes in sim's simulated clock)
        # the agent stops firing for the rest of this iteration. The
        # real DB row is never mutated — this is purely a "what-if"
        # preview so the operator sees how a one-shot / n-fires agent
        # would behave under the regime.
        if matches and sim_mode:
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
                # Record the agent as exhausted this iteration (only once
                # per iteration so the report doesn't double-count).
                exh = alert_state.setdefault('lifespan_exhausted_agents', [])
                if agent.id not in exh:
                    exh.append(agent.id)
                shadow['exhausted'] = True
                continue   # skip the suppression + fire decision below

        # Suppression gate: general sim runs and the live path BOTH go
        # through it; only isolated single-agent sim runs bypass it so
        # repeated "Run in Simulator" clicks always fire.
        if matches and (bypass_suppression or not _v2_should_suppress(agent, matches, now, cfg)):
            triggered = True
            result = _v2_build_evalresult(matches, agent.name)

            # Shadow-lifespan decrement — sim mode only. Records this
            # iteration's "would have exhausted" state for the report.
            if sim_mode:
                ls_state = alert_state.setdefault('shadow_lifespan', {})
                shadow = ls_state.get(agent.id)
                if shadow is not None and shadow.get('remaining') is not None:
                    shadow['remaining'] -= 1
                    if shadow['remaining'] <= 0:
                        shadow['exhausted'] = True
                        exh = alert_state.setdefault('lifespan_exhausted_agents', [])
                        if agent.id not in exh:
                            exh.append(agent.id)

            if broadcast_fn:
                broadcast_fn("agent_state", {"slug": agent.slug, "status": "triggered"})

            # Compute the post-fire status now so it can be stored on the
            # dispatch entry and used by the survivor loop after topic-tier
            # suppression is resolved. DB mutation and _v2_record are
            # intentionally deferred: a suppressed agent must NOT consume
            # its lifespan quota or start its cooldown timer.
            if not bypass_schedule:
                _new_trigger_count = (agent.trigger_count or 0) + 1
                _lifespan = getattr(agent, "lifespan_type", "persistent") or "persistent"
                if _lifespan == "one_shot":
                    _new_status: str = "completed"
                elif (_lifespan == "n_fires"
                      and agent.lifespan_max_fires is not None
                      and _new_trigger_count >= agent.lifespan_max_fires):
                    _new_status = "completed"
                else:
                    _new_status = "cooldown"
            else:
                _new_status = agent.status
                _new_trigger_count = agent.trigger_count or 0

            # Buffer dispatch + actions for the post-loop suppression pass.
            # _v2_record, DB mutation (trigger_count, status, cooldown_until,
            # condition_first_true_at), and the post-fire WS broadcast are
            # deferred to the survivor loop so that suppressed agents never
            # have their state mutated.
            pending_dispatches.append({
                'agent':            agent,
                'matches':          matches,
                'result':           result,
                'sim_mode':         sim_mode,
                'alert_state':      alert_state,
                'bypass_schedule':  bypass_schedule,
                'new_status':       _new_status,
                'debounce_min':     debounce_min,
            })

        # Update state for the non-triggered paths only. For any sim run
        # (schedule-bypassed) we never mutate the agent row — the whole
        # point of the simulator is to exercise the pipeline without leaking
        # cooldown / trigger count into real-market state. The real path
        # runs with bypass_schedule=False and does update the row.
        #
        # NOTE: the `if triggered` branch that previously lived here has
        # been moved into the post-loop survivor section so that topic-tier
        # suppressed agents do NOT have their DB state mutated.
        if not bypass_schedule:
            if not triggered:
                if agent.status == "cooldown":
                    _untriggered_status = "active"
                    async with async_session() as session:
                        await session.execute(
                            update(Agent).where(Agent.id == agent.id).values(
                                status=_untriggered_status
                            )
                        )
                        await session.commit()
                    if broadcast_fn:
                        broadcast_fn("agent_state", {
                            "slug": agent.slug, "status": _untriggered_status
                        })
                elif debounce_first_true_changed:
                    # Phase 21 — persist the debounce latch transitions
                    # even when we don't fire (else the latch lives only
                    # in process memory and a restart resets it).
                    async with async_session() as session:
                        await session.execute(
                            update(Agent).where(Agent.id == agent.id).values(
                                condition_first_true_at=debounce_new_first_true_at
                            )
                        )
                        await session.commit()

    # ── Post-loop: topic-scoped tier suppression + dispatch survivors ────
    #
    # Suppression rule: within each topic, find the highest-priority tier
    # that fired this tick. Drop every lower-tier fire in that topic — they
    # get an audit-log entry as `triggered_suppressed` but no push
    # notification, no action execution. The operator gets ONE alert per
    # topic per tick instead of N stacked alarms for the same root cause.
    if pending_dispatches:
        suppressed_ids = _compute_topic_suppression(pending_dispatches)
        for entry in pending_dispatches:
            agent       = entry['agent']
            matches_    = entry['matches']
            result      = entry['result']
            sim_mode_p  = entry['sim_mode']

            if agent.id in suppressed_ids:
                # Audit-log only. Channel push + actions skipped.
                supp_by = suppressed_ids[agent.id]
                detail_text = (
                    f"Suppressed by higher-tier agent '{supp_by}' in topic "
                    f"'{getattr(agent, 'topic', 'general')}'."
                )
                try:
                    await log_event(
                        agent, 'triggered_suppressed',
                        f"{result.condition_text} — {detail_text}",
                        detail={'suppressed_by': supp_by,
                                'topic': getattr(agent, 'topic', 'general'),
                                'tier':  getattr(agent, 'tier',  'medium')},
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
                continue

            # Survivor path — agent passed topic-tier suppression.
            # Now safe to commit side-effects that must NOT run for
            # suppressed agents: latch _v2_record (updates _V2_LAST_ALERT
            # used by cooldown gate), write trigger_count / status / cooldown
            # to DB, and emit the final WS status broadcast.
            _v2_record(agent, matches_, now)
            _bypass_schedule_p = entry.get('bypass_schedule', False)
            if not _bypass_schedule_p:
                _new_status_p = entry['new_status']
                _debounce_min_p = entry.get('debounce_min', 0)
                async with async_session() as session:
                    _db_values: dict = dict(
                        status=_new_status_p,
                        last_triggered_at=datetime.now(timezone.utc),
                        trigger_count=Agent.trigger_count + 1,
                    )
                    # Phase 21 — clear the debounce latch after a fire so
                    # the next true tick starts a fresh window.
                    if _debounce_min_p > 0:
                        _db_values["condition_first_true_at"] = None
                    await session.execute(
                        update(Agent).where(Agent.id == agent.id).values(**_db_values)
                    )
                    await session.commit()
                if broadcast_fn:
                    broadcast_fn("agent_state", {"slug": agent.slug, "status": _new_status_p})

            # Full dispatch path — same code that used to live inside the
            # per-agent loop, now driven from the buffer.
            rich_sent = await _v2_send_rich_alert(
                agent, matches_, now, sim_mode=sim_mode_p, context=context,
            )
            if not rich_sent:
                await dispatch(agent, result, broadcast_fn, sim_mode=sim_mode_p)
            else:
                await log_event(agent, 'triggered', result.condition_text, sim_mode=sim_mode_p)
                if broadcast_fn:
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


# Tier rank for topic-suppression. Lower = higher priority.
_TIER_RANK = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}


def _compute_topic_suppression(pending: list[dict]) -> dict[int, str]:
    """
    Given the list of buffered fires from a single run_cycle, return a
    dict mapping `suppressed_agent_id → suppressing_agent_slug`.

    Rule: within each topic, the highest-priority tier wins. Every fire
    at a lower tier within the same topic is suppressed (dispatch +
    actions skipped). Topic 'general' is opt-out — agents at the default
    tag don't participate, so legacy untagged agents behave exactly as
    before.

    Returns an empty dict when no suppression applies (single-fire ticks,
    all-equal-tier ticks, all-untagged ticks).
    """
    # Bucket fires by topic.
    by_topic: dict[str, list[dict]] = {}
    for entry in pending:
        agent = entry['agent']
        topic = getattr(agent, 'topic', 'general') or 'general'
        if topic == 'general':
            continue  # opt-out — no suppression on the default topic
        by_topic.setdefault(topic, []).append(entry)

    suppressed: dict[int, str] = {}
    for topic, group in by_topic.items():
        if len(group) <= 1:
            continue  # nothing to suppress; one fire is the only fire
        # Find the minimum (highest-priority) tier rank in this topic.
        min_rank = min(
            _TIER_RANK.get(getattr(e['agent'], 'tier', 'medium'), 99)
            for e in group
        )
        # Pick a representative winner slug (first entry at that rank).
        winner_slug = next(
            e['agent'].slug for e in group
            if _TIER_RANK.get(getattr(e['agent'], 'tier', 'medium'), 99) == min_rank
        )
        for entry in group:
            agent = entry['agent']
            rank = _TIER_RANK.get(getattr(agent, 'tier', 'medium'), 99)
            if rank > min_rank:
                suppressed[agent.id] = winner_slug
    return suppressed


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
