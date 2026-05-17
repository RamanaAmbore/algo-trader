"""
Condition-tree → scenario synthesiser for the Market Simulator.

Core insight: an agent's condition tree already contains everything needed
to build a scenario that will make it fire — metric + scope + operator +
threshold. This module turns a stored Agent row into an in-memory scenario
dict shaped identically to entries in `scenarios.yaml`, so the existing
SimDriver.start() path runs it with no code changes.

That removes the "add an agent → add a scenario" maintenance coupling:
the scenario is always in sync with the condition because it's derived
from the condition.

Scope
  - Leaf conditions (metric / scope / op / value) — fully supported.
  - `all` / `any` / `not` composites — pick the "most-likely-to-fire"
    leaf and aim for that. `all` needs every leaf to be true, so we
    target the tightest threshold; `any` needs just one, so we target
    the loosest. `not` inverts the intent and is rarely correct for a
    "make this fire" scenario — we warn and fall back to the inner leaf.

Output shape (identical to scenarios.yaml entries)
  {
    "slug":  "agent-<slug>-auto",
    "name":  "Auto: <agent.name>",
    "mode":  "symbol",
    "holdings_every_n_ticks": 1,   # per-tick for determinism
    "positions_every_n_ticks": 1,
    "initial": { "holdings": [...], "positions": [...], "margins": [...] },
    "ticks":   [ {"at": 0, "moves": [...]}, ... ],
  }

Deliberately minimal: we only seed the rows and symbols the agent's scope
will match, not the whole book. Keeps the tick log readable and the sim
state deterministic.
"""

from __future__ import annotations

from typing import Any, Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════════════
#  Tree navigation
# ═════════════════════════════════════════════════════════════════════════

def _is_leaf(node: Any) -> bool:
    return isinstance(node, dict) and "metric" in node and "scope" in node


def pick_target_leaf(cond: Any) -> Optional[dict]:
    """
    Walk the condition tree and pick the single leaf we'll aim to fire.

      leaf                     → that leaf
      all / any[leaf, leaf…]   → loosest threshold (smallest |value|) for `any`;
                                 tightest (largest |value|) for `all`
      not [inner]              → inner leaf (operator noted but not flipped —
                                 a "make NOT X fire" scenario is not the same
                                 as "make X fire"; we warn and target inner).

    Returns None when the tree has no usable leaves.
    """
    if not isinstance(cond, dict):
        return None
    if _is_leaf(cond):
        return cond
    if "all" in cond:
        leaves = [pick_target_leaf(c) for c in (cond.get("all") or [])]
        leaves = [l for l in leaves if l]
        if not leaves:
            return None
        # Tightest threshold — the one hardest to trip; if we trip it, the
        # others (looser) come along for the ride.
        return max(leaves, key=lambda l: abs(float(l.get("value") or 0)))
    if "any" in cond:
        leaves = [pick_target_leaf(c) for c in (cond.get("any") or [])]
        leaves = [l for l in leaves if l]
        if not leaves:
            return None
        # Loosest threshold — only one leaf needs to fire for ANY.
        return min(leaves, key=lambda l: abs(float(l.get("value") or 0)))
    if "not" in cond:
        logger.warning(
            "synthesize: 'not' composite encountered — targeting inner leaf "
            "without inverting. Scenario may not actually trip the NOT."
        )
        return pick_target_leaf(cond.get("not"))
    return None


# ═════════════════════════════════════════════════════════════════════════
#  Metric → move-sequence mapping
# ═════════════════════════════════════════════════════════════════════════
#
# Given a chosen leaf, build (initial_rows, ticks) that will trip it.
#
# Metrics covered (all seeded loss/fund system metrics):
#   pnl              — positions pnl in ₹               → target_pnl on positions
#   pnl_pct          — positions pnl as % of margin     → target_pnl (computed)
#   day_pct          — holdings day-change %            → pct on holdings
#   day_rate_abs     — ΔP&L/min absolute                → scheduled pct over window
#   day_rate_pct     — Δ%/min                           → scheduled pct over window
#   pnl_rate_abs     — positions ΔP&L/min               → scheduled target_pnl
#   pnl_rate_pct     — positions Δ%/min                 → scheduled target_pnl
#   cash             — avail opening_balance            → set_margin
#   avail_margin     — net                              → set_margin

_SCRIPTED_ACCOUNTS = ["ZG####", "ZJ####"]


def _rate_window_min(ctx: dict) -> float:
    """
    Read alerts.rate_window_min from the DB settings cache, falling back
    to the legacy YAML key and the in-code default. Matches the
    evaluator's source of truth so synthesised scenarios size their
    decay windows to the same number operators tune from
    /admin/settings.
    """
    try:
        from backend.shared.helpers.settings import get_float
        from backend.shared.helpers.utils import config
        return get_float("alerts.rate_window_min",
                         float(config.get("alert_rate_window_min", 10)))
    except Exception:
        return 10.0


def _scope_accounts(scope: str) -> list[str]:
    """
    Extract the target account masks from the scope token.
      holdings.any_acct / holdings.total   → both scripted accounts
      positions.ZG####  / exact             → that account only
    """
    parts = scope.split(".", 2)
    if len(parts) < 2:
        return _SCRIPTED_ACCOUNTS
    sub = parts[1]
    if sub in ("any_acct", "total"):
        return _SCRIPTED_ACCOUNTS
    return [sub]


def _scope_kind(scope: str) -> str:
    """
    Returns 'total' / 'any_acct' / 'exact'. The synthesised per-tick
    target_pnl value scales differently:
      - 'total'    — sum across accounts must cross the threshold, so
                     each account gets `target / N`.
      - 'any_acct' — each account must individually cross, so each
                     account gets the full `target` (NOT divided).
                     This is the fix for the historical synthesiser
                     bug where any_acct loss-pos-acct-* agents seeded
                     scenarios that put each account at half the
                     threshold — the agent never fired.
      - 'exact'    — single named account; same as 'total' with N=1.
    """
    parts = scope.split(".", 2)
    if len(parts) < 2:
        return "total"
    sub = parts[1]
    if sub == "total":
        return "total"
    if sub == "any_acct":
        return "any_acct"
    return "exact"


def _per_acct_share(target: float, kind: str, n_accounts: int) -> float:
    """
    Per-account slice of the synthesised target P&L.
      total / exact → target / N  (sum-across must cross)
      any_acct      → target      (each account must individually cross)
    """
    if kind == "any_acct":
        return target
    return target / max(1, n_accounts)


def _section_of(scope: str) -> str:
    """Return 'holdings' | 'positions' | 'funds' based on the scope prefix."""
    if scope.startswith("holdings"):
        return "holdings"
    if scope.startswith("positions"):
        return "positions"
    if scope.startswith("funds") or scope.startswith("margins"):
        return "funds"
    return "positions"   # fall back — positions is the most common


def _default_position_row(account: str) -> dict:
    # Default long option position — positive quantity so target_pnl negative
    # moves work without mixed-sign refusal.
    return {
        "account": account, "tradingsymbol": "NIFTY25APRPE22000", "exchange": "NFO",
        "quantity": 50, "average_price": 200.0,
        "last_price": 200.0, "close_price": 200.0,
    }


def _default_margin_row(account: str) -> dict:
    return {
        "account": account,
        "avail opening_balance": 100000,
        "net": 75000,
        "util debits": 25000,
        "avail collateral": 10000,
    }


def _margin_rows_with_total(accounts: list[str]) -> list[dict]:
    """
    Build per-account margin rows AND a TOTAL aggregate row.

    The real /api/funds route always emits a TOTAL row alongside the
    per-account ones, and the agent evaluator's `pnl_pct` metric
    resolver looks up `ctx.used_margin_for(row.account)` — for a
    `positions.total` scope row, that's `used_margin_for('TOTAL')`.
    Without a TOTAL margin row in the synthesised frame, the resolver
    returns None and total-pct agents never fire. This helper makes
    the synthesised margins match the real-pipeline shape.
    """
    per_acct = [_default_margin_row(a) for a in accounts]
    total = {"account": "TOTAL"}
    for k in ("avail opening_balance", "net", "util debits", "avail collateral"):
        total[k] = sum(r.get(k, 0) for r in per_acct)
    return [*per_acct, total]


# ─────────────────────────────────────────────────────────────────────────
#  Per-metric synthesisers — each returns (initial_section_rows, ticks).
#
#  Positions-only simulator — there are no holdings synthesisers. Agents
#  that check holdings metrics (day_pct, day_rate_abs, day_rate_pct) are
#  deliberately NOT synthesisable. The caller returns a clear error so
#  the operator knows this specific rule can only be validated in
#  production against live market data.
# ─────────────────────────────────────────────────────────────────────────

def _synth_pnl(leaf: dict) -> tuple[dict, list]:
    """Drive positions.pnl to the threshold in 3 ticks via target_pnl."""
    value    = float(leaf.get("value") or 0)
    scope    = leaf.get("scope", "")
    accounts = _scope_accounts(scope)
    kind     = _scope_kind(scope)
    target   = value * 1.2   # go 20 % past the threshold so it clearly trips

    positions = [_default_position_row(a) for a in accounts]
    margins   = _margin_rows_with_total(accounts)

    ticks = []
    for i, frac in enumerate([0.4, 0.7, 1.0]):
        moves = []
        for a in accounts:
            per_acct = _per_acct_share(target * frac, kind, len(accounts))
            moves.append({"type": "target_pnl",
                           "scope": f"positions.{a}.*", "value": per_acct})
        ticks.append({"at": i, "moves": moves})
    return {"positions": positions, "margins": margins}, ticks


def _synth_pnl_pct(leaf: dict) -> tuple[dict, list]:
    """
    pnl_pct ≤ value% of util_margin. Same setup as _synth_pnl but pick a
    pnl such that pnl / used_margin crosses the pct threshold.

    The denominator depends on scope kind:
      total    — pnl_pct = TOTAL_pnl / TOTAL_util_margin
      any_acct — pnl_pct = per_acct_pnl / per_acct_util_margin
      exact    — per_acct, single account
    `_margin_rows_with_total` seeds both per-account AND TOTAL margins
    so the total-pct path actually fires.
    """
    pct      = float(leaf.get("value") or 0)
    scope    = leaf.get("scope", "")
    accounts = _scope_accounts(scope)
    kind     = _scope_kind(scope)
    util     = 25000.0   # matches _default_margin_row

    positions = [_default_position_row(a) for a in accounts]
    margins   = _margin_rows_with_total(accounts)

    # For total scope the denominator is sum-across; for any_acct it's
    # per-account. Either way `util` is the per-account util_margin.
    if kind == "total":
        denom = util * len(accounts)
    else:
        denom = util
    target_abs = denom * (pct / 100.0) * 1.2   # 20 % past threshold
    ticks = []
    for i, frac in enumerate([0.4, 0.7, 1.0]):
        moves = []
        for a in accounts:
            per_acct = _per_acct_share(target_abs * frac, kind, len(accounts))
            moves.append({"type": "target_pnl",
                           "scope": f"positions.{a}.*", "value": per_acct})
        ticks.append({"at": i, "moves": moves})
    return {"positions": positions, "margins": margins}, ticks


def _synth_pnl_rate_abs(leaf: dict) -> tuple[dict, list]:
    """
    pnl_rate_abs ≤ N ₹/min. Smooth positions pnl decay at N ₹/min for
    rate_window_min minutes.

    The rate evaluator computes ΔP&L / Δt_wallclock_minutes from the
    pnl_history accumulator. The synthesised scenario emits one tick
    per simulated minute; `at: i` is the tick index, but the engine's
    timestamps come from real wall-clock time, so we cumulate the
    target P&L proportionally. The 1.2× overshoot ensures the rate
    crosses the threshold cleanly even with sub-minute tick spacing.
    """
    rate_per_min = float(leaf.get("value") or 0)   # negative
    scope        = leaf.get("scope", "")
    accounts     = _scope_accounts(scope)
    kind         = _scope_kind(scope)
    window_min   = int(_rate_window_min({}))

    positions = [_default_position_row(a) for a in accounts]
    margins   = _margin_rows_with_total(accounts)

    ticks = []
    for i in range(window_min + 1):
        moves = []
        for a in accounts:
            # Cumulative target at tick i = rate × (i+1) × 1.2 (past threshold)
            full = rate_per_min * (i + 1) * 1.2
            per_acct = _per_acct_share(full, kind, len(accounts))
            moves.append({"type": "target_pnl",
                           "scope": f"positions.{a}.*", "value": per_acct})
        ticks.append({"at": i, "moves": moves})
    return {"positions": positions, "margins": margins}, ticks


def _synth_pnl_rate_pct(leaf: dict) -> tuple[dict, list]:
    """pnl_rate_pct ≤ N %/min. Similar — pct of util_margin per minute."""
    rate_pct   = float(leaf.get("value") or 0)
    scope      = leaf.get("scope", "")
    accounts   = _scope_accounts(scope)
    kind       = _scope_kind(scope)
    window_min = int(_rate_window_min({}))
    util       = 25000.0

    positions = [_default_position_row(a) for a in accounts]
    margins   = _margin_rows_with_total(accounts)

    # Δpnl/min as % of util → in ₹, rate = util × (pct/100). Total
    # scope's denominator is sum-across; any_acct is per-account.
    util_basis = util * len(accounts) if kind == "total" else util
    rate_abs_per_min = util_basis * (rate_pct / 100.0)
    ticks = []
    for i in range(window_min + 1):
        moves = []
        for a in accounts:
            full = rate_abs_per_min * (i + 1) * 1.2
            per_acct = _per_acct_share(full, kind, len(accounts))
            moves.append({"type": "target_pnl",
                           "scope": f"positions.{a}.*", "value": per_acct})
        ticks.append({"at": i, "moves": moves})
    return {"positions": positions, "margins": margins}, ticks


def _synth_cash_negative(leaf: dict) -> tuple[dict, list]:
    """cash < 0. One-shot set_margin driving avail opening_balance below 0."""
    accounts = _scope_accounts(leaf.get("scope", ""))
    margins  = [_default_margin_row(a) for a in accounts]
    ticks = [{"at": 0, "moves": [
        {"type": "set_margin", "scope": f"margins.{a}",
         "fields": {"avail opening_balance": -1500, "net": -2500}}
        for a in accounts
    ]}]
    return {"positions": [], "margins": margins}, ticks


def _synth_margin_negative(leaf: dict) -> tuple[dict, list]:
    """avail_margin < 0. set_margin on `net`."""
    accounts = _scope_accounts(leaf.get("scope", ""))
    margins  = [_default_margin_row(a) for a in accounts]
    ticks = [{"at": 0, "moves": [
        {"type": "set_margin", "scope": f"margins.{a}",
         "fields": {"net": -2500}}
        for a in accounts
    ]}]
    return {"positions": [], "margins": margins}, ticks


def _default_holding_row(account: str, *, qty: int = 50,
                          avg: float = 200.0, last: float = 200.0,
                          close: float = 200.0) -> dict:
    """Minimal Kite-shape holding row. close_price drives the day_change /
    day_change_percentage fields the holdings agents read."""
    return {
        "account":         account,
        "tradingsymbol":   "TESTHOLD",
        "exchange":        "NSE",
        "quantity":        qty,
        "average_price":   avg,
        "last_price":      last,
        "close_price":     close,
        "pnl":             (last - avg) * qty,
        "day_change":      (last - close) * qty,
        "day_change_percentage": (last - close) / close if close else 0.0,
    }


def _synth_day_pct(leaf: dict) -> tuple[dict, list]:
    """
    day_pct ≤ value%. Drive the holding's last_price down so
    (last - close)/close crosses the threshold. Implemented via three
    pct ticks on the holdings row (~40% / 70% / 100% of the move).
    """
    pct      = float(leaf.get("value") or 0)
    accounts = _scope_accounts(leaf.get("scope", ""))
    target   = pct * 1.2   # 20% past threshold
    holdings = [_default_holding_row(a) for a in accounts]
    ticks = []
    # Total move needed across the 3 ticks (as a fraction of close).
    # Each tick gets ~33% of the total geometric move.
    per_tick = (1.0 + target / 100.0) ** (1.0 / 3.0) - 1.0
    for i in range(3):
        moves = [{"type": "pct", "scope": "holdings.**", "value": per_tick}]
        ticks.append({"at": i, "moves": moves})
    return {"holdings": holdings, "positions": [], "margins": []}, ticks


def _synth_day_rate_abs(leaf: dict) -> tuple[dict, list]:
    """day_rate_abs ≤ N ₹/min on holdings. Spread the loss over the
    rate window so the velocity gate registers."""
    rate_per_min = float(leaf.get("value") or 0)  # negative
    accounts     = _scope_accounts(leaf.get("scope", ""))
    window_min   = int(_rate_window_min({}))
    holdings = [_default_holding_row(a) for a in accounts]
    target   = rate_per_min * window_min * 1.4
    ticks = []
    for i in range(3):
        frac  = (i + 1) / 3.0
        delta = target * frac / 50.0  # qty 50 — per-share delta
        moves = [{"type": "abs", "scope": "holdings.**", "value": delta - (target * (i / 3.0) / 50.0)}]
        ticks.append({"at": i, "moves": moves})
    return {"holdings": holdings, "positions": [], "margins": []}, ticks


def _synth_day_rate_pct(leaf: dict) -> tuple[dict, list]:
    """day_rate_pct ≤ N %/min. Drive a smooth pct decay across the rate
    window — similar shape to _synth_day_pct but spread further over
    time to hit a per-minute rate."""
    pct_per_min = float(leaf.get("value") or 0)
    window_min  = int(_rate_window_min({}))
    accounts    = _scope_accounts(leaf.get("scope", ""))
    target_pct  = pct_per_min * window_min * 1.3
    holdings = [_default_holding_row(a) for a in accounts]
    per_tick = (1.0 + target_pct / 100.0) ** (1.0 / 3.0) - 1.0
    ticks = []
    for i in range(3):
        moves = [{"type": "pct", "scope": "holdings.**", "value": per_tick}]
        ticks.append({"at": i, "moves": moves})
    return {"holdings": holdings, "positions": [], "margins": []}, ticks


_METRIC_SYNTH = {
    "pnl":            _synth_pnl,
    "pnl_pct":        _synth_pnl_pct,
    "pnl_rate_abs":   _synth_pnl_rate_abs,
    "pnl_rate_pct":   _synth_pnl_rate_pct,
    "cash":           _synth_cash_negative,
    "avail_margin":   _synth_margin_negative,
    # Holdings metrics — now synthesisable thanks to holdings sim.
    "day_pct":        _synth_day_pct,
    "day_rate_abs":   _synth_day_rate_abs,
    "day_rate_pct":   _synth_day_rate_pct,
}


# ═════════════════════════════════════════════════════════════════════════
#  Public entry point
# ═════════════════════════════════════════════════════════════════════════

class SynthesizeError(RuntimeError):
    """Raised when an agent's condition isn't synthesizable (unknown metric,
    empty tree, etc.). The route handler turns this into a 400."""


def synthesize_for_agent(agent) -> dict:
    """
    Turn an Agent row into a scenario dict that will trip that agent.

    Raises SynthesizeError when the condition tree is empty or uses a
    metric we don't know how to synthesise for. The admin UI should catch
    that and suggest either (a) running a generic stress scenario instead
    or (b) writing a scripted scenario by hand.
    """
    leaf = pick_target_leaf(agent.conditions)
    if leaf is None:
        raise SynthesizeError(
            f"Agent '{agent.slug}' has no leaf conditions to synthesise from."
        )
    metric = leaf.get("metric", "")
    synth  = _METRIC_SYNTH.get(metric)
    if synth is None:
        raise SynthesizeError(
            f"No synthesiser for metric '{metric}'. "
            f"Known metrics: {sorted(_METRIC_SYNTH.keys())}. "
            f"Pick a generic stress scenario on /admin/simulator instead."
        )

    initial, ticks = synth(leaf)

    # Pick a market-state preset that matches the agent's intent:
    # rate-metric agents want the session already well underway (so the
    # 15-min baseline gate doesn't silence them and the rate window has
    # history); expiry-related slugs get the expiry_day preset; everything
    # else runs mid_session which keeps segment flags on.
    slug = (agent.slug or "").lower()
    if "expiry" in slug:
        preset = "expiry_day"
    elif "rate" in metric:
        preset = "mid_session"    # rate evaluator still has history
    else:
        preset = "mid_session"

    return {
        "slug":  f"agent-{agent.slug}-auto",
        "name":  f"Auto: {agent.name}",
        "mode":  "symbol",
        "description": (
            f"Synthesised from agent #{agent.id} ({agent.slug}) — targets "
            f"leaf {metric}@{leaf.get('scope')} {leaf.get('op')} "
            f"{leaf.get('value')}."
        ),
        "positions_every_n_ticks": 1,
        "market_state": {"preset": preset},
        "initial": initial,
        "ticks":   ticks,
    }
