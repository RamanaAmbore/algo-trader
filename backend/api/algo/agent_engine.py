"""
Agent Engine — evaluates all active agents using Conditions → Alerts → Actions pipeline.

Called from background.py every refresh cycle with market data context.
Each agent's condition tree is evaluated. If triggered, alerts are dispatched
through configured channels and optional actions are executed.

The engine handles cooldown, state transitions, and WebSocket broadcasts.
"""

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


# ---------------------------------------------------------------------------
# v2 condition-tree helpers
# ---------------------------------------------------------------------------

def is_grammar_tree(cond) -> bool:
    """True when `cond` is a structurally plausible grammar tree."""
    if not isinstance(cond, dict):
        return False
    if 'all' in cond or 'any' in cond or 'not' in cond:
        return True
    return 'metric' in cond and 'scope' in cond


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
    if section == 'Holdings':
        pnl = float(row.get('day_change_val', 0) or 0)
        pct = float(row.get('day_change_percentage', 0) or 0) if row.get('day_change_percentage') is not None else None
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

    rate_val = value if kind in ('rate_abs', 'rate_pct') else None

    # threshold display — format with units appropriate to the kind
    try:
        thr = float(threshold)
        if kind in ('static_pct', 'rate_pct'):
            thr_str = f"{thr:.2f}%" + ("/min" if kind == 'rate_pct' else "")
        elif kind in ('static_abs', 'rate_abs', 'negative_cash', 'negative_margin'):
            thr_str = f"-₹{abs(thr):,.0f}" + ("/min" if kind == 'rate_abs' else "")
        else:
            thr_str = str(threshold)
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

    tg_body    = _tg_alert_body(rows)
    email_html = _email_alert_body(rows)
    subject    = f"Agent {agent.slug}"
    mode_tag   = '' if sim_mode else _agent_execution_mode_tag(agent)
    try:
        _dispatch('alert', timestamp_display(), tg_body, email_html, subject,
                  sim_mode=sim_mode, mode_tag=mode_tag)
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
    if get_bool("execution.paper_trading_mode", True):
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
    # ── Holdings: static % floors ────────────────────────────────────────
    dict(slug="loss-hold-acct-static-pct",
         name="Holdings per-account day loss ≥ 3%",
         description="Fires when any individual account's holdings day loss crosses the per-account % floor.",
         conditions={"metric": "day_pct", "scope": "holdings.any_acct", "op": "<=", "value": -3.0},
         scope="total",  # scope token inside the condition already handles per-account
         ),
    dict(slug="loss-hold-total-static-pct",
         name="Holdings total day loss ≥ 5%",
         description="Fires when the portfolio total holdings day loss crosses the total % floor.",
         conditions={"metric": "day_pct", "scope": "holdings.total", "op": "<=", "value": -5.0},
         scope="total",
         ),

    # ── Positions: static % floors ───────────────────────────────────────
    dict(slug="loss-pos-acct-static-pct",
         name="Positions per-account loss ≥ 2% of margin",
         description="Fires when any account's positions pnl is ≤ −2% of that account's used margin.",
         conditions={"metric": "pnl_pct", "scope": "positions.any_acct", "op": "<=", "value": -2.0},
         scope="total",
         ),
    dict(slug="loss-pos-total-static-pct",
         name="Positions total loss ≥ 2% of margin",
         description="Fires when the portfolio total positions pnl is ≤ −2% of total used margin.",
         conditions={"metric": "pnl_pct", "scope": "positions.total", "op": "<=", "value": -2.0},
         scope="total",
         ),

    # ── Positions: static ₹ floors ───────────────────────────────────────
    dict(slug="loss-pos-acct-static-abs",
         name="Positions per-account loss ≥ ₹30,000",
         description="Fires when any account's positions pnl is ≤ −₹30,000.",
         conditions={"metric": "pnl", "scope": "positions.any_acct", "op": "<=", "value": -30000},
         scope="total",
         ),
    dict(slug="loss-pos-total-static-abs",
         name="Positions total loss ≥ ₹50,000",
         description="Fires when the portfolio total positions pnl is ≤ −₹50,000.",
         conditions={"metric": "pnl", "scope": "positions.total", "op": "<=", "value": -50000},
         scope="total",
         ),

    # ── Rate-of-change: holdings ────────────────────────────────────────
    dict(slug="loss-hold-acct-rate-abs",
         name="Holdings per-account bleeding ≥ ₹2k/min",
         description="Fires when any account's holdings day-change rate of loss is steeper than ₹2,000/min.",
         conditions={"metric": "day_rate_abs", "scope": "holdings.any_acct", "op": "<=", "value": -2000},
         scope="total",
         ),
    dict(slug="loss-hold-total-rate-abs",
         name="Holdings total bleeding ≥ ₹4k/min",
         description="Fires when the portfolio total holdings day-change rate of loss is steeper than ₹4,000/min.",
         conditions={"metric": "day_rate_abs", "scope": "holdings.total", "op": "<=", "value": -4000},
         scope="total",
         ),
    dict(slug="loss-hold-any-rate-pct",
         name="Holdings bleeding ≥ 0.15 %/min",
         description="Fires on any scope whose holdings %-of-value day rate is worse than 0.15 %/min.",
         conditions={"any": [
             {"metric": "day_rate_pct", "scope": "holdings.any_acct", "op": "<=", "value": -0.15},
             {"metric": "day_rate_pct", "scope": "holdings.total",    "op": "<=", "value": -0.15},
         ]},
         scope="total",
         ),

    # ── Rate-of-change: positions ────────────────────────────────────────
    dict(slug="loss-pos-acct-rate-abs",
         name="Positions per-account bleeding ≥ ₹3k/min",
         description="Fires when any account's positions pnl rate of loss is steeper than ₹3,000/min.",
         conditions={"metric": "pnl_rate_abs", "scope": "positions.any_acct", "op": "<=", "value": -3000},
         scope="total",
         ),
    dict(slug="loss-pos-total-rate-abs",
         name="Positions total bleeding ≥ ₹6k/min",
         description="Fires when the portfolio total positions pnl rate of loss is steeper than ₹6,000/min.",
         conditions={"metric": "pnl_rate_abs", "scope": "positions.total", "op": "<=", "value": -6000},
         scope="total",
         ),
    dict(slug="loss-pos-any-rate-pct",
         name="Positions bleeding ≥ 0.25 %/min",
         description="Fires on any scope whose positions %-of-margin rate is worse than 0.25 %/min.",
         conditions={"any": [
             {"metric": "pnl_rate_pct", "scope": "positions.any_acct", "op": "<=", "value": -0.25},
             {"metric": "pnl_rate_pct", "scope": "positions.total",    "op": "<=", "value": -0.25},
         ]},
         scope="total",
         ),

    # ── Funds: operational negatives ────────────────────────────────────
    dict(slug="loss-funds-cash-negative",
         name="Account cash has gone negative",
         description="Fires when any account's cash balance dips below zero.",
         conditions={"metric": "cash", "scope": "funds.any_acct", "op": "<", "value": 0},
         scope="total",
         ),
    dict(slug="loss-funds-margin-negative",
         name="Account available margin has gone negative",
         description="Fires when any account's available margin dips below zero.",
         conditions={"metric": "avail_margin", "scope": "funds.any_acct", "op": "<", "value": 0},
         scope="total",
         ),

    # ── Auto-close on severe loss (destructive — ships INACTIVE) ────────
    # This agent is the concrete example of a close-positions action that
    # operators can study, copy, or activate directly. It ships inactive
    # because auto-closing is a broker-touching action and should be an
    # explicit opt-in, not a default. Run it in the simulator first, then
    # flip it ON from the /agents page when you're confident.
    dict(slug="loss-pos-total-auto-close",
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
                if existing.schedule != agent_def.get("schedule", "market_hours"):
                    existing.schedule = agent_def.get("schedule", "market_hours")
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
                description=agent_def.get("description", ""),
                conditions=agent_def["conditions"],
                events=agent_def["events"],
                actions=agent_def["actions"],
                scope=agent_def.get("scope", "total"),
                schedule=agent_def.get("schedule", "market_hours"),
                cooldown_minutes=agent_def.get("cooldown_minutes", 30),
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
                # agent_events has a FK into agents.id without ON DELETE
                # CASCADE — clear the child rows first so the parent delete
                # does not raise ForeignKeyViolationError on startup.
                await session.execute(delete(AgentEvent).where(AgentEvent.agent_id == row.id))
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
    from backend.shared.helpers.broker_apis import fetch_holidays

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

        is_holiday = now.date() in holidays
        is_weekend = now.weekday() >= 5  # Saturday=5, Sunday=6
        in_time_range = open_time <= now <= close_time
        is_open = in_time_range and not is_holiday and not is_weekend

        ctx[f"{prefix}_open"] = is_open
        ctx[f"{prefix}_closed"] = (now > close_time) and not is_holiday and not is_weekend
        ctx[f"{prefix}_holiday"] = is_holiday
        ctx[f"minutes_since_{prefix}_open"] = max(0, int((now - open_time).total_seconds() / 60)) if now >= open_time and is_open else 0
        ctx[f"minutes_since_{prefix}_close"] = max(0, int((now - close_time).total_seconds() / 60)) if now > close_time and not is_holiday else 0

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

    # Load agents. For isolated runs (simulator "Run in Simulator") we accept
    # any status so an operator can dry-fire an inactive agent. For the
    # normal cycle we stick to active/cooldown rows.
    async with async_session() as session:
        if only_agent_ids:
            result = await session.execute(
                select(Agent).where(Agent.id.in_(only_agent_ids))
            )
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
    base_ctx = _build_context(now, sim_overrides=context.get("market_state"))

    # Determine whether NSE/MCX are currently open (for schedule filtering)
    nse_open_flag = bool(base_ctx.get("nse_open"))
    mcx_open_flag = bool(base_ctx.get("mcx_open"))
    any_market_open = nse_open_flag or mcx_open_flag

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
        cfg = _v2_cfg()
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

        # Suppression gate: general sim runs and the live path BOTH go
        # through it; only isolated single-agent sim runs bypass it so
        # repeated "Run in Simulator" clicks always fire.
        if matches and (bypass_suppression or not _v2_should_suppress(agent, matches, now, cfg)):
            triggered = True
            result = _v2_build_evalresult(matches, agent.name)
            _v2_record(agent, matches, now)

            if broadcast_fn:
                broadcast_fn("agent_state", {"slug": agent.slug, "status": "triggered"})

            # Send the rich narrow-TG + coloured HTML table message via
            # alert_utils._dispatch. Fall back to the generic dispatch()
            # path on any failure so the log / WebSocket channel still
            # carries a record of the fire.
            rich_sent = await _v2_send_rich_alert(
                agent, matches, now, sim_mode=sim_mode, context=context,
            )
            if not rich_sent:
                await dispatch(agent, result, broadcast_fn, sim_mode=sim_mode)
            else:
                await log_event(agent, 'triggered', result.condition_text, sim_mode=sim_mode)
                if broadcast_fn:
                    broadcast_fn('agent_alert', {
                        'slug': agent.slug,
                        'message': result.condition_text,
                        'timestamp': now.isoformat(),
                        'sim_mode': sim_mode,
                    })

            if agent.actions:
                action_ctx = dict(context)
                action_ctx["account"] = "TOTAL"
                action_ctx["sim_mode"] = sim_mode
                await execute(agent, agent.actions, action_ctx)

        # Update state. For any sim run (schedule-bypassed) we never mutate
        # the agent row — the whole point of the simulator is to exercise the
        # pipeline without leaking cooldown / trigger count into real-market
        # state. The real path runs with bypass_schedule=False and does
        # update the row.
        if not bypass_schedule:
            async with async_session() as session:
                if triggered:
                    # Lifespan check — one_shot or capped n_fires
                    # transition straight to 'completed' instead of
                    # 'cooldown'. Engine won't pick up `completed`
                    # rows on subsequent cycles. Operator can re-arm
                    # by editing status back to active/inactive.
                    new_trigger_count = (agent.trigger_count or 0) + 1
                    lifespan = getattr(agent, "lifespan_type", "persistent") or "persistent"
                    if lifespan == "one_shot":
                        end_status = "completed"
                    elif (lifespan == "n_fires"
                          and agent.lifespan_max_fires is not None
                          and new_trigger_count >= agent.lifespan_max_fires):
                        end_status = "completed"
                    else:
                        end_status = "cooldown"
                    await session.execute(
                        update(Agent).where(Agent.id == agent.id).values(
                            status=end_status,
                            last_triggered_at=datetime.now(timezone.utc),
                            trigger_count=Agent.trigger_count + 1,
                        )
                    )
                elif agent.status == "cooldown":
                    await session.execute(
                        update(Agent).where(Agent.id == agent.id).values(status="active")
                    )
                await session.commit()

            if broadcast_fn:
                if triggered:
                    # Compute the same end_status we just persisted so the
                    # WS payload matches DB state.
                    new_trigger_count = (agent.trigger_count or 0) + 1
                    lifespan = getattr(agent, "lifespan_type", "persistent") or "persistent"
                    if lifespan == "one_shot":
                        new_status = "completed"
                    elif (lifespan == "n_fires"
                          and agent.lifespan_max_fires is not None
                          and new_trigger_count >= agent.lifespan_max_fires):
                        new_status = "completed"
                    else:
                        new_status = "cooldown"
                else:
                    new_status = "active"
                broadcast_fn("agent_state", {"slug": agent.slug, "status": new_status})
