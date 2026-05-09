"""
DB-backed settings helper.

Settings live in the `settings` table (one row per tunable). Callers read
via `get_int / get_float / get_bool / get_string`, which:

  1. Check the in-process cache (refreshed on startup + on any PATCH).
  2. If missing, fall back to backend_config.yaml via `config.get(key)`.
  3. Cast to the requested type with a sensible default on failure.

This lets us move a value from YAML → DB without touching every call site:
the key stays the same; the reader checks DB first, YAML second.

The seeder (seed_settings) runs on startup and populates the table with
the SEED definitions below. It only inserts when a row is missing — so
operators' tweaks via /admin/settings survive deploys. When the default
for a seeded key changes in the code, the `default_value` column is
updated so the "Reset" button keeps working, but the live `value` is
preserved.
"""

from __future__ import annotations

import asyncio
from typing import Any

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config as yaml_config

logger = get_logger(__name__)

# In-process cache: {key: value_str}. Populated by _reload_cache() which
# reads every row from the settings table. Invalidated on PATCH.
_CACHE: dict[str, str] = {}


# ═════════════════════════════════════════════════════════════════════════
#  Seed definitions — the authoritative list of DB-backed tunables.
# ═════════════════════════════════════════════════════════════════════════
#
# Each entry: (category, key, value_type, default, description, units, schema)
# value_type: 'int' | 'float' | 'bool' | 'string' | 'enum'
# units: display-only; e.g. 'min', '₹', '%', '₹/min', 'ms'
# schema: {'min':N, 'max':N, 'step':N} for numbers; {'enum':[...]} for enums
#
# Key naming: <category>.<name>. The reader strips the category prefix
# when falling back to YAML, so existing top-level YAML keys like
# `alert_cooldown_minutes` are preserved — we just prefix them here for
# grouping in the UI.

SEEDS: list[tuple] = [
    # ── Alerts ──────────────────────────────────────────────────────────
    ("alerts", "alerts.cooldown_minutes",        "int",    30,
     "Minimum minutes between re-fires of the same rate-of-change agent.",
     "min", {"min": 1, "max": 600, "step": 1}),
    ("alerts", "alerts.rate_window_min",         "int",    10,
     "How many minutes of P&L history rate-of-change agents look at.",
     "min", {"min": 1, "max": 60, "step": 1}),
    ("alerts", "alerts.baseline_offset_min",     "int",    15,
     "Rate agents stay silent for this long after session start so the "
     "opening gap doesn't trip them.", "min", {"min": 0, "max": 60, "step": 1}),
    ("alerts", "alerts.suppress_delta_abs",      "int",    15000,
     "Rate-agent re-fire requires |Δpnl| of at least this much since last fire.",
     "₹", {"min": 0, "max": 1000000, "step": 500}),
    ("alerts", "alerts.suppress_delta_pct",      "float",  0.5,
     "Rate-agent re-fire requires |Δpct| of at least this much since last fire.",
     "%", {"min": 0, "max": 10, "step": 0.05}),
    # Underlying-breakdown + rate enrichment for position alerts. Each
    # position alert can carry a one-line "by underlying" summary
    # (NIFTY -₹22k · BANKNIFTY -₹13k …) plus a rate-of-loss readout
    # (₹/min over the rate window) to give the operator richer
    # context than the bare account-total number.
    ("alerts", "alerts.show_underlying_breakdown",       "bool",  True,
     "Append per-underlying loss breakdown to position-alert messages "
     "(both Telegram and email).", None, None),
    ("alerts", "alerts.max_underlyings_per_alert",       "int",   5,
     "Top-N underlyings shown in the per-alert breakdown, sorted by "
     "|loss| descending.", None, {"min": 1, "max": 20, "step": 1}),
    ("alerts", "alerts.show_rate_in_static_alerts",      "bool",  True,
     "Show rate-of-loss (₹/min over the rate window) on static-threshold "
     "position alerts too — by default only rate-based alerts surface it.",
     None, None),
    ("alerts", "alerts.summary_show_underlying_breakdown", "bool", True,
     "Append a per-underlying breakdown to the open/close summary "
     "Telegram + email message.", None, None),

    # ── Performance refresh ─────────────────────────────────────────────
    ("performance", "performance.refresh_interval",        "int", 5,
     "Minutes between live broker refreshes during market hours.",
     "min", {"min": 1, "max": 60, "step": 1}),
    ("performance", "performance.open_summary_offset_min", "int", 15,
     "Minutes after segment open to send the Open Summary Telegram/email.",
     "min", {"min": 0, "max": 60, "step": 1}),
    ("performance", "performance.close_summary_offset_min", "int", 15,
     "Minutes after segment close to send the Close Summary Telegram/email.",
     "min", {"min": 0, "max": 60, "step": 1}),

    # ── Simulator defaults ──────────────────────────────────────────────
    # Positions-only sim — no holdings cadence. Holdings agents are
    # untestable in the simulator by design; they evaluate only against
    # live production data.
    ("simulator", "simulator.positions_every_n_ticks", "int", 1,
     "Positions refresh every N ticks (1 = every tick).",
     "ticks", {"min": 1, "max": 100, "step": 1}),
    ("simulator", "simulator.auto_stop_minutes",       "int", 30,
     "Auto-stop a sim after this many wall-clock minutes so a forgotten "
     "run can't bleed forever.", "min", {"min": 1, "max": 240, "step": 1}),
    ("simulator", "simulator.default_rate_ms",         "int", 2000,
     "Default tick rate (ms) when the UI opens.", "ms",
     {"min": 200, "max": 60000, "step": 100}),
    ("simulator", "simulator.default_spread_pct",      "float", 0.10,
     "Default bid/ask spread (% of LTP) applied to every position. Drives "
     "side-aware limit prices and the paper-trade chase engine.",
     "%", {"min": 0.0, "max": 5.0, "step": 0.01}),
    ("simulator", "simulator.chase_max_attempts",      "int", 5,
     "Maximum price-modify attempts a sim paper-trade will make before the "
     "order is marked as unfilled. Zero disables chasing.",
     None, {"min": 0, "max": 50, "step": 1}),

    # ── Notifications (per-branch capability toggles) ───────────────────
    # NOTE: these MIRROR the cap_in_<branch>.<feature> YAML flags; the
    # is_enabled() helper is deliberately NOT rewired — it still reads
    # YAML. Settings page edits here update a parallel set of DB flags
    # that operators can introspect, and future code can migrate per
    # feature once we're confident.
    ("notifications", "notifications.telegram_enabled",    "bool",  True,
     "Send alerts to Telegram (mirrors cap_in_<branch>.telegram).", None, None),
    ("notifications", "notifications.email_enabled",       "bool",  True,
     "Send alerts via SMTP email (mirrors cap_in_<branch>.mail).", None, None),
    ("notifications", "notifications.notify_on_deploy",    "bool",  True,
     "Send a Telegram/email ping on every deploy.", None, None),

    # ── Logging ─────────────────────────────────────────────────────────
    ("logging", "logging.file_log_level",    "enum",   "INFO",
     "Rotating file log verbosity.", None,
     {"enum": ["DEBUG", "INFO", "WARNING", "ERROR"]}),
    ("logging", "logging.console_log_level", "enum",   "INFO",
     "Console log verbosity.", None,
     {"enum": ["DEBUG", "INFO", "WARNING", "ERROR"]}),
    ("logging", "logging.error_log_level",   "enum",   "ERROR",
     "Rotating error file verbosity.", None,
     {"enum": ["DEBUG", "INFO", "WARNING", "ERROR"]}),

    # ── Connections / broker ─────────────────────────────────────────────
    ("connections", "connections.retry_count",      "int", 3,
     "Retry attempts for broker calls before giving up.", None,
     {"min": 1, "max": 10, "step": 1}),
    ("connections", "connections.price_account",    "string", "",
     "Account code (e.g. ZG0790) used for shared market-data fetches "
     "— underlying spot snapshots in the paper engine, historical "
     "candles + quotes for the options-analytics page. Blank = "
     "auto-pick the first account in secrets.yaml. Doesn't affect "
     "per-account holdings / positions / orders calls; those still "
     "hit each account directly.", None, None),

    # ── GenAI ────────────────────────────────────────────────────────────
    ("genai",       "genai.thinking_budget",        "int", 512,
     "Cap on Gemini's internal-thinking tokens so the visible response "
     "doesn't get truncated mid-sentence.", "tokens",
     {"min": 0, "max": 8192, "step": 64}),

    # ── Auth ─────────────────────────────────────────────────────────────
    ("auth",        "auth.enforce_password_standard", "bool", False,
     "Reject weak passwords on registration / password change.", None, None),

    # ── Performance / market refresh ─────────────────────────────────────
    ("performance", "performance.market_refresh_time", "string", "08:30",
     "IST clock time for the daily Gemini market-update warm "
     "(HH:MM, 24-hour).", None, None),

    # ── Algo (chase + expiry) ────────────────────────────────────────────
    ("algo",        "algo.chase_interval_seconds",  "int", 20,
     "Seconds between price adjustments while chasing an open order.",
     "s", {"min": 1, "max": 300, "step": 1}),
    ("algo",        "algo.aggression_step",         "float", 0.10,
     "Spread-fraction increase per chase attempt.", None,
     {"min": 0.0, "max": 1.0, "step": 0.01}),
    ("algo",        "algo.max_attempts",            "int", 20,
     "Maximum chase attempts before the order is marked unfilled.",
     None, {"min": 1, "max": 100, "step": 1}),
    ("algo",        "algo.expiry_start_offset_hours","float", 2,
     "Hours before market close to begin expiry-day auto-close scan.",
     "h", {"min": 0, "max": 6, "step": 0.25}),
    ("algo",        "algo.expiry_ntm_buffer_pct",   "float", 2.0,
     "% from strike to flag as near-the-money on expiry-day scan.",
     "%", {"min": 0, "max": 10, "step": 0.1}),
    ("algo",        "algo.expiry_rescan_minutes",   "int", 30,
     "Re-scan interval (min) on expiry day for new ITM positions.",
     "min", {"min": 1, "max": 120, "step": 1}),

    # execution.paper_trading_mode + execution.shadow_mode are owned by
    # /api/admin/execution/mode (navbar combobox) — single source of
    # truth. Seeding those here would create a second editable surface
    # and foot-gun the operator into thinking they can toggle paper/live
    # from the Settings page. The get_bool() readers fall back to the
    # in-code default (True for paper_trading_mode, False for shadow_mode)
    # when no DB row exists. The seeder auto-prunes those rows from the DB.

    # execution.default_agent_trade_mode IS seeded — it's a default for
    # newly-created agents, not a runtime mode toggle. Settings page
    # surfaces it; agent CRUD reads it when no per-agent value is given.
    ("execution",   "execution.default_agent_trade_mode", "enum", "paper",
     "Default trade mode for newly-created agents (paper or live). "
     "Existing agents keep their per-row trade_mode.",
     None, {"enum": ["paper", "live"]}),

    # ── Replay / Backtest ────────────────────────────────────────��─────
    ("replay",      "replay.max_days",           "int",  60,
     "Maximum date range (days) for a single replay run.",
     "days", {"min": 1, "max": 365, "step": 1}),
    ("replay",      "replay.auto_stop_minutes",  "int",  30,
     "Auto-stop a replay after this many wall-clock minutes.",
     "min", {"min": 1, "max": 120, "step": 1}),
    ("replay",      "replay.default_interval",   "enum", "5minute",
     "Default candle interval for replay runs.",
     None, {"enum": ["minute", "5minute", "15minute", "day"]}),
]


# ═════════════════════════════════════════════════════════════════════════
#  Seeder + cache refresh
# ═════════════════════════════════════════════════════════════════════════

async def seed_settings() -> None:
    """
    Insert any missing seeded rows. Updates default_value on existing rows
    so the "Reset" button reflects the current code default. Leaves the
    operator's `value` alone to preserve runtime overrides across deploys.
    """
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import Setting

    from sqlalchemy import delete as sql_delete
    seed_keys = {s[1] for s in SEEDS}

    async with async_session() as session:
        existing = (await session.execute(select(Setting))).scalars().all()
        existing_by_key = {s.key: s for s in existing}

        inserted = updated_defaults = 0
        for category, key, value_type, default, desc, units, schema in SEEDS:
            default_str = _serialise(default, value_type)
            row = existing_by_key.get(key)
            if row is None:
                session.add(Setting(
                    category=category, key=key, value_type=value_type,
                    value=default_str, default_value=default_str,
                    description=desc, units=units, schema=schema,
                ))
                inserted += 1
            else:
                # Seeder owns category / description / schema / units and the
                # default; operator owns the live value. Sync the former on
                # every boot so renames and help-text tweaks land.
                changed = False
                for field, new_val in (
                    ("category", category), ("description", desc),
                    ("units", units), ("schema", schema),
                    ("default_value", default_str), ("value_type", value_type),
                ):
                    if getattr(row, field) != new_val:
                        setattr(row, field, new_val)
                        changed = True
                if changed:
                    updated_defaults += 1

        # Prune rows whose keys are no longer in the SEEDS list — the code
        # is the source of truth for what settings exist. Custom tokens on
        # the Tokens page have their own lifecycle; this is specifically
        # for retired system-seeded keys.
        # EXCEPTION: keys owned by route handlers outside SEEDS (e.g.
        # execution.paper_trading_mode + execution.shadow_mode are upserted
        # by /api/admin/execution/mode whenever the navbar chip flips
        # mode). Pruning those would mean every service restart resets
        # the operator's last-picked execution mode. Match by prefix so
        # any future "owned-outside-SEEDS" key follows the same pattern.
        OWNED_OUTSIDE_SEEDS_PREFIXES = ("execution.",)
        retired_keys = [
            k for k in existing_by_key
            if k not in seed_keys
            and not any(k.startswith(p) for p in OWNED_OUTSIDE_SEEDS_PREFIXES)
        ]
        removed = 0
        if retired_keys:
            await session.execute(sql_delete(Setting).where(Setting.key.in_(retired_keys)))
            removed = len(retired_keys)

        await session.commit()

    if inserted or updated_defaults or removed:
        logger.info(
            f"Settings: seeded {inserted} new rows, refreshed "
            f"{updated_defaults} existing, pruned {removed} retired"
            + (f" ({', '.join(retired_keys)})" if retired_keys else "")
        )

    await reload_cache()


async def reload_cache() -> None:
    """Rebuild the in-process value cache from the DB."""
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import Setting

    async with async_session() as session:
        rows = (await session.execute(select(Setting))).scalars().all()
    _CACHE.clear()
    for r in rows:
        _CACHE[r.key] = r.value
    logger.info(f"Settings: cache reloaded ({len(_CACHE)} keys)")


def invalidate_cache() -> None:
    """Schedule a reload — called after PATCH."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(reload_cache())
    except RuntimeError:
        pass   # no loop (e.g. during unit tests) — next startup fixes it


# ═════════════════════════════════════════════════════════════════════════
#  Public read helpers — type-cast with YAML fallback
# ═════════════════════════════════════════════════════════════════════════

def _serialise(val: Any, value_type: str) -> str:
    if value_type == "bool":
        return "true" if bool(val) else "false"
    return str(val)


def _lookup_raw(key: str) -> str | None:
    """
    DB cache first, then YAML. YAML fallback tries three shapes:
      1. Top-level flat key matching `<key>` exactly.
      2. Nested dotted lookup — `algo.chase_interval_seconds` →
         yaml_config["algo"]["chase_interval_seconds"].
      3. Legacy flat aliases — `alerts.cooldown_minutes` → YAML's
         `alert_cooldown_minutes`, etc. — kept so downgrades to a
         pre-DB-seeding state still resolve.
    """
    if key in _CACHE:
        return _CACHE[key]
    yaml_val = yaml_config.get(key)
    if yaml_val is not None:
        return str(yaml_val)
    if "." in key:
        # Nested traversal — walk the YAML tree by dotted segments.
        cursor: Any = yaml_config
        ok = True
        for seg in key.split("."):
            if isinstance(cursor, dict) and seg in cursor:
                cursor = cursor[seg]
            else:
                ok = False
                break
        if ok and cursor is not None and not isinstance(cursor, dict):
            return str(cursor)
        # Legacy flat aliases (alert_*, performance_*).
        _, flat = key.split(".", 1)
        for candidate in (flat, "alert_" + flat, "performance_" + flat):
            v = yaml_config.get(candidate)
            if v is not None:
                return str(v)
    return None


def get_int(key: str, default: int = 0) -> int:
    raw = _lookup_raw(key)
    if raw is None:
        return default
    try:
        return int(float(raw))   # tolerate "5.0"
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    raw = _lookup_raw(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    raw = _lookup_raw(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def get_string(key: str, default: str = "") -> str:
    raw = _lookup_raw(key)
    return raw if raw is not None else default
