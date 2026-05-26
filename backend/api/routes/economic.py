"""
`/api/economic/snapshot` — India macro snapshot for the Lab page +
MCP get_economic_snapshot tool.

Static reference data: repo rate, CPI, IIP, GDP growth, INR/USD.
Hand-maintained in backend/config/backend_config.yaml under `macros:`;
the deploy script preserves the operator's edited values across
deploys (same pattern as cap_in_dev). Each metric carries an `as_of`
date so the LLM can apply a freshness discount.

No GenAI / no external scraping in this route. Phase 2c+ may add a
dynamic FII/DII fetcher; for now the snapshot is fully offline +
deterministic. Cost: ₹0.

Why not scrape RBI / MoSPI: repo rate moves once a quarter, CPI / IIP
monthly. Hand-edit cadence matches the natural data cadence, avoids
brittle HTML / PDF scraping, and survives upstream layout changes.
"""

from __future__ import annotations

from datetime import datetime, date as date_t
from typing import Any

import msgspec
from litestar import Controller, get

from backend.api.auth_guard import admin_guard
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config as backend_config

logger = get_logger(__name__)


class MacroEntry(msgspec.Struct):
    """One metric — value + freshness + a derived `stale` flag."""
    value:        float
    as_of:        str                # ISO date "YYYY-MM-DD"
    age_days:     int                # days since as_of
    stale:        bool               # age_days > stale_threshold for this metric
    label:        str                # human-readable name for the UI / LLM


class EconomicSnapshot(msgspec.Struct):
    """All India macro reference data the LLM gets via the MCP tool.
    Every field is optional — missing keys in backend_config.yaml just
    drop out of the response rather than 500'ing the endpoint."""
    repo_rate:    MacroEntry | None
    cpi:          MacroEntry | None
    iip:          MacroEntry | None
    gdp_growth:   MacroEntry | None
    inr_usd:      MacroEntry | None
    refreshed_at: str                # client-presentation timestamp


# How many days old before we flag each metric as `stale=True`.
# Reflects the natural release cadence of each series.
_STALE_THRESHOLDS = {
    "repo_rate":  120,   # MPC meets every ~6 weeks; flag if >4 months
    "cpi":         45,   # Released ~12th of next month; flag if >6 weeks
    "iip":         60,   # Released ~12th of two-months-prior; flag if >2 months
    "gdp_growth": 120,   # Quarterly; flag if >4 months
    "inr_usd":      7,   # Spot drifts daily; flag aggressively
}

_LABELS = {
    "repo_rate":   "RBI repo rate (%)",
    "cpi":         "CPI inflation y-o-y (%)",
    "iip":         "Industrial production y-o-y (%)",
    "gdp_growth":  "GDP growth q-o-q (%)",
    "inr_usd":     "USD/INR spot",
}


def _parse_date(s: Any) -> date_t | None:
    if not s:
        return None
    try:
        if isinstance(s, date_t):
            return s
        return datetime.fromisoformat(str(s)).date()
    except Exception:
        return None


def _macro_entry(metric: str, value_key: str, as_of_key: str) -> MacroEntry | None:
    macros = (backend_config or {}).get("macros") or {}
    raw_val = macros.get(value_key)
    raw_dt  = macros.get(as_of_key)
    if raw_val is None or raw_dt is None:
        return None
    try:
        v = float(raw_val)
    except (TypeError, ValueError):
        return None
    d = _parse_date(raw_dt)
    if not d:
        return None
    age = (date_t.today() - d).days
    return MacroEntry(
        value=v,
        as_of=d.isoformat(),
        age_days=max(0, int(age)),
        stale=(age > _STALE_THRESHOLDS.get(metric, 90)),
        label=_LABELS.get(metric, metric),
    )


class EconomicController(Controller):
    path   = "/api/economic"
    guards = [admin_guard]

    @get("/snapshot")
    async def snapshot(self) -> EconomicSnapshot:
        """One-call macro bundle. Missing or malformed config rows
        come back as null so the LLM sees holes explicitly rather
        than getting silent zeros."""
        from backend.shared.helpers.date_time_utils import timestamp_display
        return EconomicSnapshot(
            repo_rate  = _macro_entry("repo_rate",  "repo_rate_pct",  "repo_rate_as_of"),
            cpi        = _macro_entry("cpi",        "cpi_yoy_pct",    "cpi_as_of"),
            iip        = _macro_entry("iip",        "iip_yoy_pct",    "iip_as_of"),
            gdp_growth = _macro_entry("gdp_growth", "gdp_growth_pct", "gdp_as_of"),
            inr_usd    = _macro_entry("inr_usd",    "inr_usd",        "inr_usd_as_of"),
            refreshed_at = timestamp_display(),
        )
