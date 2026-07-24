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


def _v2_format_threshold(kind: str, threshold) -> str:
    """Format a threshold value with units appropriate to the alert kind.

    kind must be one of {static_pct, rate_pct, static_abs, rate_abs,
    negative_cash, negative_margin}. Non-numeric thresholds fall through to
    the str() fallback via the except branch.
    """
    try:
        thr = float(threshold)
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

    Extracted from _v2_extract_pnl_fields to replace the elif chain."""
    if metric == 'cash':
        return float(row.get('avail opening_balance', 0) or 0)
    if metric == 'avail_margin':
        return float(row.get('net', 0) or 0)
    return float(value or 0)


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

    Returns the rate value (float ₹/min) when at least 2 history samples
    span a non-zero time window, otherwise None.  Reads the same history
    bucket that rate-metric evaluators use so the numbers are consistent.
    """
    from backend.shared.helpers.settings import get_bool
    if not get_bool('alerts.show_rate_in_static_alerts', True):
        return None
    hist = (alert_state.get('pnl_history') or {}).get(('positions', scope_label), []) or []
    if len(hist) < 2:
        return None
    cutoff_window = hist[-1][0] - timedelta(minutes=rate_window_min)
    window = [s for s in hist if s[0] >= cutoff_window]
    if len(window) < 2:
        return None
    oldest, latest = window[0], window[-1]
    mins = (latest[0] - oldest[0]).total_seconds() / 60.0
    if mins <= 0:
        return None
    # field_idx=1 → pnl ₹/min, matching rate_abs metric
    return (latest[1] - oldest[1]) / mins


def _v2_derive_section(scope_tok: str) -> str:
    """Map a scope token prefix to its alert section label."""
    if scope_tok.startswith('holdings'):
        return 'Holdings'
    if scope_tok.startswith('positions'):
        return 'Positions'
    return 'Funds'


def _v2_derive_kind(metric: str) -> str:
    """Map a metric token to its alert kind label."""
    if metric in ('cash',):
        return 'negative_cash'
    if metric in ('avail_margin',):
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
    row       = match.get('row')      or {}
    value     = match.get('value')
    threshold = match.get('threshold')

    section     = _v2_derive_section(scope_tok)
    kind        = _v2_derive_kind(metric)
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

    # Sort Holdings → Positions → Funds, per-account before TOTAL.
    _ae_sort_alert_rows(rows)

    # `simulator.notify_during_run` gate — audit trail is always written;
    # only the noisy outbound channels are suppressed in sim mode.
    if _ae_sim_notify_suppressed(agent, sim_mode):
        return True

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

    # ── Funds: margin shortfall warning (early-warning before negative) ──
    dict(slug="loss-margin-low",
         long_name="when:funds.any_acct.avail_margin<25000   alert:high/tg+email+ntfy+log   do:notify-only",
         tier="high",
         topic="funds_warning",
         name="Margin shortfall warning",
         description=(
             "Fires when available margin on any account drops below "
             "₹25,000 — warning before margin goes negative."
         ),
         conditions={"op": "<", "scope": "funds.any_acct", "value": 25000, "metric": "avail_margin"},
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
        "slug": "market-close-mcx",
        "name": "MCX market close",
        "long_name": "when:fire_at=23:30   alert:info/tg+ntfy(default)+log   do:notify-only",
        "description": "Fires once at MCX close (23:30 IST) on trading days.",
        "fire_at_time": "23:30",
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


def _ae_sync_builtin_status(existing, desired: str | None) -> None:
    """Bidirectionally sync status on a built-in Agent row.

    Only flips active↔inactive; ignores other states.
    Extracted from _ae_sync_existing_builtin to reduce CC there."""
    if not desired or existing.status == desired:
        return
    if desired == "active" and existing.status == "inactive":
        existing.status = "active"
    elif desired == "inactive" and existing.status == "active":
        existing.status = "inactive"


def _ae_sync_existing_builtin(existing, agent_def: dict) -> None:
    """Force-sync mutable fields on an existing system Agent row.

    Operator-editable fields (conditions, cooldown, actions) are left
    untouched. Extracted from seed_agents to reduce CC there."""
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
    _ae_sync_builtin_status(existing, agent_def.get("status"))
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
        status=agent_def.get("status", "active"),
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
    - For existing system rows, force-sync `schedule` and `status` so the
      engine state converges on the current code definition. User-tuned
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
                    "Agent engine: ntfy configured (topic=%s url=%s) — "
                    "no auth token (open server assumed).",
                    _ntfy_topic, _ntfy_url,
                )
            else:
                logger.info(
                    "Agent engine: ntfy configured (topic=%s url=%s) — "
                    "Bearer token present.",
                    _ntfy_topic, _ntfy_url,
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
    window_sec = int(get_int('alerts.fire_at_window_sec', 360))
    return not _fire_at_window_active(agent.fire_at_time, now, window_sec=window_sec)


def _cycle_in_blackout(agent, now, *, bypass_schedule: bool) -> bool:
    """True when the current time falls inside a configured blackout window."""
    if bypass_schedule:
        return False
    blackouts = getattr(agent, "blackout_windows", None) or []
    return bool(blackouts and _in_blackout_window(now, blackouts))


def _cycle_baseline_not_ready(agent, alert_state: dict, now, cfg: dict, *,
                              bypass_schedule: bool) -> bool:
    """True when a rate-metric agent should be suppressed during the baseline window."""
    return (
        not bypass_schedule
        and _v2_has_rate_metric(agent.conditions)
        and not _v2_baseline_live(alert_state, now, cfg['baseline_offset_min'])
    )


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
                select(Agent).where(Agent.id.in_(only_agent_ids))
            )
            return list(result.scalars().all())
        result = await session.execute(
            select(Agent).where(Agent.status.in_(["active", "cooldown"]))
        )
        return list(result.scalars().all())


def _cycle_evaluate_agent(agent, context: dict, cfg: dict, now, alert_state: dict) -> list:
    """Build a V2Context for the agent and run the condition tree evaluator.

    alert_state must be the same dict object held by run_cycle so that any
    mutations made by the evaluator (e.g. pnl_history updates) remain visible
    to subsequent per-agent gates on the same tick.

    Returns the list of match dicts (empty on no match or on evaluator error).
    """
    v2_ctx = V2Context(
        sum_holdings=context.get("sum_holdings"),
        sum_positions=context.get("sum_positions"),
        df_margins=context.get("df_margins"),
        watchlist_rows=context.get("watchlist_rows") or [],
        position_rows=context.get("position_rows") or [],
        spot_prices=context.get("spot_prices") or {},
        alert_state=alert_state,
        now=now,
        segments=context.get("segments", []),
        rate_window_min=cfg['rate_window_min'],
        agent=agent,
    )
    try:
        return v2_evaluate(agent.conditions, v2_ctx)
    except Exception as e:
        logger.error(f"Agent [{agent.slug}] v2 evaluate failed: {e}")
        return []


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
) -> bool:
    """Evaluate the suppression gate and, when the agent fires, buffer a dispatch entry.

    Returns True when the agent fired (triggered), False otherwise.
    Mutates pending_dispatches in place on fire.
    """
    if not matches:
        return False
    if not (bypass_suppression or not _v2_should_suppress(agent, matches, now, cfg)):
        return False

    result = _v2_build_evalresult(matches, agent.name)
    if sim_mode:
        _cycle_shadow_lifespan_decrement(agent, alert_state)
    if broadcast_fn:
        broadcast_fn("agent_state", {"slug": agent.slug, "status": "triggered"})
    new_status, _ = _cycle_compute_post_fire_status(agent, bypass_schedule=bypass_schedule)
    pending_dispatches.append({
        'agent':           agent,
        'matches':         matches,
        'result':          result,
        'sim_mode':        sim_mode,
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
    """Return True when the agent clears all pre-evaluation timing gates.

    Checks schedule, cooldown, fire_at_time, and blackout windows.
    Extracted from _cycle_process_agent to reduce CC there."""
    if _cycle_should_skip_schedule(agent, any_market_open=any_market_open,
                                   bypass_schedule=bypass_schedule):
        return False
    if _cycle_in_cooldown(agent, bypass_schedule=bypass_schedule):
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
) -> None:
    """Evaluate condition tree, apply debounce/lifespan gates, buffer fires.

    Also persists non-triggered state changes. Extracted from
    _cycle_process_agent to reduce CC there."""
    matches = _cycle_evaluate_agent(agent, context, cfg, now, alert_state)
    if not matches:
        _v2_unlatch(agent)

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
    _maybe_reset_v2_state(now.date() if hasattr(now, 'date') else None)

    if _cycle_baseline_not_ready(agent, alert_state, now, cfg,
                                 bypass_schedule=bypass_schedule):
        return

    await _ae_cycle_eval_and_buffer(
        agent, context, cfg, now,
        alert_state=alert_state, sim_mode=sim_mode,
        bypass_schedule=bypass_schedule, bypass_suppression=bypass_suppression,
        broadcast_fn=broadcast_fn, pending_dispatches=pending_dispatches,
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

    agents = await _cycle_load_agents(only_agent_ids)
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
    agent      = entry['agent']
    result     = entry['result']
    sim_mode_p = entry['sim_mode']
    supp_by    = suppressed_ids[agent.id]
    topic      = getattr(agent, 'topic', 'general')
    detail_text = (
        f"Suppressed by higher-tier agent '{supp_by}' in topic '{topic}'."
    )
    try:
        await log_event(
            agent, 'triggered_suppressed',
            f"{result.condition_text} — {detail_text}",
            detail={'suppressed_by': supp_by,
                    'topic': topic,
                    'tier':  getattr(agent, 'tier', 'medium')},
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

    _v2_record(agent, matches_, now)
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
    suppressed_ids = _compute_topic_suppression(pending_dispatches)
    for entry in pending_dispatches:
        agent = entry['agent']
        if agent.id in suppressed_ids:
            await _ae_dispatch_suppressed_entry(entry, suppressed_ids, broadcast_fn)
            continue
        await _ae_dispatch_survivor_entry(entry, now, context, broadcast_fn)


# Tier rank for topic-suppression. Lower = higher priority.
_TIER_RANK = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}


def _ae_topic_winner(group: list[dict]) -> tuple[int, str]:
    """Return (min_rank, winner_slug) for a topic group.

    min_rank is the lowest (highest-priority) tier rank present;
    winner_slug is the first agent slug at that rank.
    Extracted from _compute_topic_suppression to reduce CC there."""
    min_rank = min(
        _TIER_RANK.get(getattr(e['agent'], 'tier', 'medium'), 99)
        for e in group
    )
    winner_slug = next(
        e['agent'].slug for e in group
        if _TIER_RANK.get(getattr(e['agent'], 'tier', 'medium'), 99) == min_rank
    )
    return min_rank, winner_slug


def _ae_suppressed_in_group(group: list[dict], suppressed: dict) -> None:
    """Populate suppressed dict with lower-tier agent ids in this topic group.

    Extracted from _compute_topic_suppression to reduce CC there."""
    min_rank, winner_slug = _ae_topic_winner(group)
    for entry in group:
        agent = entry['agent']
        rank = _TIER_RANK.get(getattr(agent, 'tier', 'medium'), 99)
        if rank > min_rank:
            suppressed[agent.id] = winner_slug


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
    by_topic: dict[str, list[dict]] = {}
    for entry in pending:
        agent = entry['agent']
        topic = getattr(agent, 'topic', 'general') or 'general'
        if topic == 'general':
            continue  # opt-out — no suppression on the default topic
        by_topic.setdefault(topic, []).append(entry)

    suppressed: dict[int, str] = {}
    for topic, group in by_topic.items():
        if len(group) > 1:
            _ae_suppressed_in_group(group, suppressed)
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
