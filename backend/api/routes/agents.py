"""
Agent CRUD API + terminal interpreter.

GET  /api/agents/               — list all agents
GET  /api/agents/{slug}         — single agent detail
POST /api/agents/               — create new agent
PUT  /api/agents/{slug}         — update agent config
PUT  /api/agents/{slug}/activate   — activate
PUT  /api/agents/{slug}/deactivate — deactivate
DELETE /api/agents/{slug}       — delete (non-system only)
GET  /api/agents/{slug}/events  — event history
POST /api/agents/interpret      — terminal command parser
"""

import json
import re
from datetime import datetime, timezone

import msgspec
from litestar import Controller, delete, get, post, put
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.rbac import cap_guard
from backend.api.database import async_session
from backend.api.models import Agent, AgentEvent
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# Valid lifespan_type values — see backend/api/models.py::Agent for
# semantics. Engine treats any other value as 'persistent' but the
# CRUD layer rejects unknowns up-front so config typos surface as
# 400s rather than silently becoming persistent.
_LIFESPAN_TYPES = {"persistent", "one_shot", "n_fires", "until_date"}


def _parse_iso_dt(s):
    """Parse an ISO 8601 datetime string into a tz-aware UTC datetime,
    or return None for empty / null input. Operator-supplied strings
    may omit the timezone (e.g. "2026-05-15T15:30:00") — assume UTC
    in that case so the comparison against now-UTC in the engine
    stays sane."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400,
            detail=f"lifespan_expires_at must be an ISO datetime; got {s!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AgentInfo(msgspec.Struct):
    id: int
    slug: str
    name: str
    # 3-part descriptor: "condition - alert - action". Optional for
    # legacy / custom agents; required-by-convention for built-ins.
    long_name: str | None
    description: str | None
    conditions: dict
    events: list
    actions: list
    scope: str
    schedule: str | None
    cooldown_minutes: int
    fire_at_time: str | None
    status: str
    last_triggered_at: str | None
    trigger_count: int
    last_error: str | None
    is_system: bool
    # Lifespan — see backend/api/models.py::Agent for semantics.
    # 'persistent' (default), 'one_shot', 'n_fires', 'until_date'.
    lifespan_type:        str        = "persistent"
    lifespan_max_fires:   int | None = None
    lifespan_expires_at:  str | None = None
    # Phase 21 — debounce "for N minutes" gate. 0 = fire immediately.
    debounce_minutes:     int        = 0
    # Phase 22 — free-form tags + IST blackout windows.
    tags:                 list       = msgspec.field(default_factory=list)
    blackout_windows:     list       = msgspec.field(default_factory=list)
    # Per-agent trade routing — 'paper' or 'live'. Resolved by
    # actions._resolve_mode AFTER dev/shadow gates and the master
    # execution.paper_trading_mode kill-switch.
    trade_mode:           str        = "paper"


class AgentCreateRequest(msgspec.Struct):
    slug: str
    name: str
    conditions: dict
    events: list
    actions: list = []
    description: str = ""
    long_name: str | None = None   # 3-part "condition - alert - action"
    scope: str = "total"
    schedule: str = "market_hours"
    cooldown_minutes: int = 30
    # HH:MM IST. Optional — when set, agent only fires once per IST
    # date inside a small window around this wall-clock time.
    fire_at_time: str | None = None
    # Lifespan accepted on create so algos spawning agents can declare
    # one-shot or bounded behaviour up front. Defaults preserve the
    # existing persistent shape.
    lifespan_type:        str        = "persistent"
    lifespan_max_fires:   int | None = None
    lifespan_expires_at:  str | None = None  # ISO datetime
    # null = inherit from execution.default_agent_trade_mode setting.
    trade_mode:           str | None = None
    # Phase 21 — debounce ("for N minutes"). 0 = fire immediately.
    debounce_minutes:     int        = 0
    # Phase 22 — tagging + quiet hours.
    tags:                 list       = msgspec.field(default_factory=list)
    blackout_windows:     list       = msgspec.field(default_factory=list)


class AgentUpdateRequest(msgspec.Struct):
    name: str | None = None
    long_name: str | None = None
    description: str | None = None
    conditions: dict | None = None
    events: list | None = None
    actions: list | None = None
    scope: str | None = None
    schedule: str | None = None
    cooldown_minutes: int | None = None
    # Phase 21 — debounce update. None = unchanged; 0 = disable; >0 = set.
    debounce_minutes: int | None = None
    # Phase 22 — tagging + quiet hours; None = unchanged.
    tags: list | None = None
    blackout_windows: list | None = None
    # HH:MM IST or empty string to clear. UNSET = leave column unchanged.
    fire_at_time: str | None = None
    lifespan_type:        str | None = None
    lifespan_max_fires:   int | None = None
    lifespan_expires_at:  str | None = None
    trade_mode:           str | None = None


class AgentEventInfo(msgspec.Struct):
    id: int
    agent_id: int
    event_type: str
    trigger_condition: str | None
    detail: str | None
    timestamp: str
    sim_mode: bool


class InterpretRequest(msgspec.Struct):
    command: str


class InterpretResponse(msgspec.Struct):
    output: str


class AIDraftRequest(msgspec.Struct):
    prompt: str


class AIDraftResponse(msgspec.Struct):
    draft:        dict
    errors:       list[str]
    warnings:     list[str]
    why_summary:  str
    prompt:       str = ""    # original NL prompt — echoed for the UI
    success: bool = True


# Legacy field/operator metadata (CONDITION_FIELDS, CONDITION_OPERATORS,
# ACTION_TYPES, EVENT_CHANNELS) is retired. The frontend now reads the full
# grammar from GET /api/admin/grammar/tokens — metrics, scopes, operators,
# channels, templates, and actions are all catalogued in `grammar_tokens`
# and extensible at runtime. See backend/api/algo/grammar_registry.py.


_VALID_TRADE_MODES = {"paper", "live"}

_AI_PROMPT_PREFIX = "[AI prompt] "
_AI_WHY_PREFIX    = "[AI why] "


def _compose_ai_description(prompt: str, why: str, llm_desc: str | None) -> str:
    """Build a structured description for AI-generated agents.

    Format:
      [AI prompt] <original NL prompt>
      [AI why] <LLM one-line summary>
      <optional LLM-provided description>

    The [AI ...] prefix makes provenance detectable from the frontend
    without a schema change. The expanded /agents row renders the
    prompt as a violet chip and surfaces 'why' as the short description.
    """
    parts = []
    if prompt:
        parts.append(f"{_AI_PROMPT_PREFIX}{prompt}")
    if why:
        parts.append(f"{_AI_WHY_PREFIX}{why}")
    if llm_desc and llm_desc.strip() and llm_desc.strip() != why.strip():
        parts.append(llm_desc.strip())
    return "\n".join(parts) if parts else (llm_desc or "")


def _normalize_fire_at(val: str | None) -> str | None:
    """Validate & canonicalise an HH:MM time string.

    Returns None for null / empty-string (clear gate) so the column
    drops back to NULL. Rejects malformed values with 400 so the UI
    catches typos before they corrupt the engine's gate check.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    # Accept HH:MM (single or double-digit hours/minutes). Reject
    # everything else so the engine's wall-clock gate never has to
    # defend against garbage.
    parts = s.split(":")
    if len(parts) != 2:
        raise HTTPException(status_code=400,
            detail=f"fire_at_time must be 'HH:MM' — got {s!r}")
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        raise HTTPException(status_code=400,
            detail=f"fire_at_time must be 'HH:MM' — got {s!r}")
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise HTTPException(status_code=400,
            detail=f"fire_at_time hours 0-23, minutes 0-59 — got {s!r}")
    return f"{hh:02d}:{mm:02d}"


async def _dry_run_context(now) -> dict:
    """Phase 22 — build the live context for an agent dry-run.

    Calls the same broker-aggregate helpers _task_performance uses,
    in a thread-pool. Each broker fetch wrapped in try/except so a
    single-broker outage (Groww disconnected, Kite rate-limited)
    doesn't 500 the whole dry-run — the agent just sees whatever
    DataFrames came back successfully (possibly empty).

    No new caching layer — a dry-run is a manual operator action,
    not a hot path. If it fires 2 broker calls per click, that's fine.
    """
    import asyncio
    from backend.api.background import _fetch_holdings_direct, _fetch_positions_direct
    from backend.brokers import broker_apis
    from backend.shared.helpers.summarise import (
        summarise_holdings as _summarise_holdings,
        summarise_positions as _summarise_positions,
    )
    from backend.api.algo.agent_engine import _build_context
    import pandas as pd
    loop = asyncio.get_running_loop()

    async def _safe_fetch(fn, label, default):
        try:
            return await loop.run_in_executor(None, fn)
        except Exception as e:
            logger.warning(f"dry-run: {label} fetch failed (using empty): {e}")
            return default

    df_holdings, sum_h = await _safe_fetch(
        _fetch_holdings_direct, "holdings",
        (pd.DataFrame(), pd.DataFrame()),
    )
    df_positions, _ = await _safe_fetch(
        _fetch_positions_direct, "positions",
        (pd.DataFrame(), pd.DataFrame()),
    )
    df_margins = await _safe_fetch(
        lambda: pd.concat(broker_apis.fetch_margins(), ignore_index=True),
        "margins", pd.DataFrame(),
    )

    try:
        sum_holdings = _summarise_holdings(df_holdings, sum_h, None)
    except Exception as e:
        logger.warning(f"dry-run: summarise_holdings failed: {e}")
        sum_holdings = pd.DataFrame()
    try:
        sum_positions = _summarise_positions(df_positions)
    except Exception as e:
        logger.warning(f"dry-run: summarise_positions failed: {e}")
        sum_positions = pd.DataFrame()

    base = _build_context(now)
    base.update(
        sum_holdings=sum_holdings,
        sum_positions=sum_positions,
        df_margins=df_margins,
        watchlist_rows=[],
        any_market_open=any(s.get("open") for s in base.get("segments", [])),
    )
    return base


def _check_dry_run_gates(agent, ctx: dict, now) -> "str | None":
    """Evaluate schedule/cooldown/fire_at_time/blackout gates in engine order.

    Returns the name of the first blocking gate, or None when all pass.
    Mirrors the gate ordering in agent_engine.run_cycle so dry-run results
    are consistent with production behaviour.
    """
    from backend.api.algo.agent_engine import _fire_at_window_active, _in_blackout_window
    if agent.schedule == "market_hours" and not ctx.get("any_market_open"):
        return "schedule"
    if agent.status == "cooldown":
        return "cooldown"
    if getattr(agent, "fire_at_time", None) and not _fire_at_window_active(agent.fire_at_time, now):
        return "fire_at_time"
    bw = getattr(agent, "blackout_windows", None) or []
    if bw and _in_blackout_window(now, bw):
        return "blackout"
    return None


def _check_debounce_gate(agent, blocked_by: "str | None", matches: list, now) -> "tuple[str | None, bool]":
    """Apply the debounce gate after condition evaluation.

    Returns (blocked_by, would_fire) — blocked_by is updated to 'debounce'
    when the latch hasn't been held long enough; would_fire tracks whether
    the agent would actually trigger right now.
    """
    would_fire = bool(matches) and blocked_by is None
    debounce_min = int(getattr(agent, "debounce_minutes", 0) or 0)
    if not (matches and blocked_by is None and debounce_min > 0):
        return blocked_by, would_fire
    first_true = getattr(agent, "condition_first_true_at", None)
    if first_true is None:
        return "debounce", False
    elapsed_min = (now - first_true).total_seconds() / 60.0
    if elapsed_min < debounce_min:
        return "debounce", False
    return blocked_by, would_fire


def _flatten_dry_run_matches(matches: list) -> list[dict]:
    """Convert match objects to JSON-safe dicts (capped at 20)."""
    return [
        {
            "metric":    getattr(m, "metric", None),
            "scope":     getattr(m, "scope", None),
            "op":        getattr(m, "op", None),
            "threshold": getattr(m, "threshold", None),
            "value":     getattr(m, "value", None),
            "account":   getattr(m, "account", None),
            "symbol":    getattr(m, "symbol", None),
        }
        for m in (matches or [])[:20]
    ]


def _build_dry_run_response(agent, slug, ctx, now) -> dict:
    """Phase 22 — pure gate/eval/response builder. Module-level so the
    route handler can wrap it in a single try/except that logs the
    actual traceback rather than relying on Litestar's silent default."""
    from backend.api.algo.agent_evaluator import evaluate as v2_evaluate, Context

    blocked_by = _check_dry_run_gates(agent, ctx, now)

    v2_ctx = Context(
        sum_holdings=ctx.get("sum_holdings"),
        sum_positions=ctx.get("sum_positions"),
        df_margins=ctx.get("df_margins"),
        watchlist_rows=ctx.get("watchlist_rows") or [],
        # Expiry-agent inputs (Phase 25) — dry-run leaves them empty so
        # is_itm / is_ntm leaves skip silently rather than 500. Operator
        # validates expiry agents on real ticks once activated.
        position_rows=ctx.get("position_rows") or [],
        spot_prices=ctx.get("spot_prices") or {},
        alert_state={},   # empty — dry-run doesn't carry state
        now=now,
        segments=ctx.get("segments", []),
        rate_window_min=10,
        agent=agent,
    )

    matches = []
    try:
        if agent.conditions:
            matches = v2_evaluate(agent.conditions, v2_ctx) or []
    except Exception as e:
        return {
            "agent_slug":  slug,
            "matches":     [],
            "match_count": 0,
            "would_fire":  False,
            "blocked_by":  "eval_error",
            "eval_error":  str(e),
            "evaluated_at": now.isoformat(),
        }

    blocked_by, would_fire = _check_debounce_gate(agent, blocked_by, matches, now)

    return {
        "agent_slug":   slug,
        "matches":      _flatten_dry_run_matches(matches),
        "match_count":  len(matches),
        "would_fire":   would_fire,
        "blocked_by":   blocked_by,
        "evaluated_at": now.isoformat(),
    }


def _age_lifespan_expires_at_iso(a: Agent) -> str | None:
    """Return lifespan_expires_at as ISO string or None.

    Extracted from _agent_to_info to remove nested ternary."""
    val = getattr(a, "lifespan_expires_at", None)
    return val.isoformat() if val else None


def _agent_to_info(a: Agent) -> AgentInfo:
    return AgentInfo(
        id=a.id, slug=a.slug, name=a.name,
        long_name=getattr(a, "long_name", None),
        description=a.description,
        conditions=a.conditions or {}, events=a.events or [],
        actions=a.actions or [], scope=a.scope,
        schedule=a.schedule, cooldown_minutes=a.cooldown_minutes,
        fire_at_time=getattr(a, "fire_at_time", None),
        status=a.status,
        last_triggered_at=(a.last_triggered_at.isoformat()
                           if a.last_triggered_at else None),
        trigger_count=a.trigger_count, last_error=a.last_error,
        is_system=a.is_system,
        lifespan_type=getattr(a, "lifespan_type", "persistent") or "persistent",
        lifespan_max_fires=getattr(a, "lifespan_max_fires", None),
        lifespan_expires_at=_age_lifespan_expires_at_iso(a),
        trade_mode=getattr(a, "trade_mode", "paper") or "paper",
        debounce_minutes=int(getattr(a, "debounce_minutes", 0) or 0),
        tags=list(getattr(a, "tags", None) or []),
        blackout_windows=list(getattr(a, "blackout_windows", None) or []),
    )


def _extract_ai_prompt(raw: str, prefix_pattern: str) -> str:
    """Strip the fixed command prefix (case-insensitive) and any
    surrounding matched quotes from a natural-language `agent ai …`
    payload. Returns "" when the prompt is empty after cleanup so the
    caller can render a usage hint.

    Args:
      raw: the full trimmed command line, e.g. `'agent ai create "hedge"'`
      prefix_pattern: raw regex string for the prefix to strip. Must
        include trailing `\\s+` — the caller controls whether the slug
        is part of the prefix (refine) or not (create).
    """
    m = re.match(prefix_pattern, raw, re.IGNORECASE)
    prompt = raw[m.end():].strip() if m else ""
    if len(prompt) >= 2 and (
        (prompt[0] == '"' and prompt[-1] == '"') or
        (prompt[0] == "'" and prompt[-1] == "'")
    ):
        prompt = prompt[1:-1].strip()
    return prompt


def _age_coerce_val(val: str):
    """Coerce a string kv-value to int, float, or leave as str.

    Extracted from _cmd_config to remove duplicate try/except blocks."""
    try:
        return float(val) if "." in val else int(val)
    except ValueError:
        return val


def _age_apply_kv_to_conditions(conditions: dict, key: str, val: str) -> None:
    """Apply a key=value pair to a conditions dict in-place.

    Handles both rule-list (v1) and flat (v0) condition shapes.
    Extracted from _cmd_config to reduce CC there."""
    coerced = _age_coerce_val(val)
    if "rules" in conditions:
        for rule in conditions.get("rules", []):
            if rule.get("field") == key:
                rule["value"] = coerced
    elif conditions.get("field") == key:
        conditions["value"] = coerced


def _age_build_ai_agent_row(
    slug: str, prompt: str, draft: dict, why_summary: str,
) -> "Agent":
    """Build an Agent ORM row from an AI-generated draft dict.

    All `draft.get(key) or default` defaulting lives here so that
    _cmd_ai_create doesn't accumulate one CC point per `or` expression.
    Extracted from _cmd_ai_create to reduce CC there."""
    return Agent(
        slug=slug,
        name=draft.get("name") or "AI agent",
        description=_compose_ai_description(
            prompt, why_summary, draft.get("description")
        ),
        conditions=draft.get("conditions") or {},
        events=draft.get("events") or ["telegram", "email"],
        actions=draft.get("actions") or [],
        scope=draft.get("scope") or "total",
        schedule=draft.get("schedule") or "market_hours",
        cooldown_minutes=int(draft.get("cooldown_minutes") or 30),
        lifespan_type=draft.get("lifespan_type") or "one_shot",
        trade_mode="paper",
        status="inactive",
    )


def _age_resolve_trade_mode(raw: str | None) -> str:
    """Resolve trade_mode for a new agent.

    null/empty → inherit from execution.default_agent_trade_mode setting
    (fallback 'paper'). Explicit value must be in _VALID_TRADE_MODES or
    400 is raised. Extracted from create_agent to reduce CC there."""
    tm = (raw or "").lower() or None
    if tm is not None and tm not in _VALID_TRADE_MODES:
        raise HTTPException(status_code=400,
            detail=f"trade_mode must be one of {sorted(_VALID_TRADE_MODES)}")
    if tm is None:
        from backend.shared.helpers.settings import get_string
        tm = get_string("execution.default_agent_trade_mode", "paper")
        if tm not in _VALID_TRADE_MODES:
            tm = "paper"
    return tm


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class AgentController(Controller):
    # Controller-level guard sets the BASE access: anonymous demo
    # can READ everything on this controller (every route inherits
    # `view_agents_catalog`). Per-route overrides tighten to
    # `manage_own_agents` for mutating endpoints (POST / PUT /
    # DELETE), which demo + observer don't hold. cap_guard() chains
    # through auth_or_demo_guard internally for demo-eligible caps,
    # so the legacy demo-state plumbing still works.
    path = "/api/agents"
    guards = [cap_guard("view_agents_catalog")]

    @get("/")
    async def list_agents(self) -> list[AgentInfo]:
        async with async_session() as session:
            result = await session.execute(select(Agent).order_by(Agent.id))
            agents = result.scalars().all()
        return [_agent_to_info(a) for a in agents]

    @post("/ai-draft", guards=[cap_guard("manage_own_agents")])
    async def ai_draft(self, data: AIDraftRequest) -> AIDraftResponse:
        """
        Convert a natural-language prompt into an agent JSON draft.

        Operator describes what they want; Gemini produces a candidate
        agent definition that's been validated against the live grammar
        registry and clamped to safe defaults (paper, inactive,
        one_shot lifespan). Operator reviews and saves via POST /.

        Failure modes return non-empty `errors`; soft risks land in
        `warnings`. Both render in the UI; operator decides.
        """
        from backend.api.algo.agent_ai import draft_agent_from_prompt
        d = draft_agent_from_prompt(data.prompt or "")
        return AIDraftResponse(
            draft=d.draft,
            errors=d.errors,
            warnings=d.warnings,
            why_summary=d.why_summary,
            prompt=d.prompt,
        )

    @post("/validate-condition", guards=[cap_guard("manage_own_agents")])
    async def validate_condition(self, data: dict) -> dict:
        """
        Dry-check a condition tree against the live Grammar Registry.

        Request body: the condition tree JSON that will be saved into
        Agent.conditions. Response: { ok, errors, grammar }. `grammar`
        is always "v2" since the legacy evaluator has been retired;
        structurally invalid trees report a single top-level error.
        """
        from backend.api.algo.agent_evaluator import validate as v2_validate
        from backend.api.algo.agent_engine import is_grammar_tree

        cond = data or {}
        if not is_grammar_tree(cond):
            return {
                "ok": False,
                "errors": ["condition tree must be a grammar node: "
                           "a metric/scope leaf, an all/any/not composite, "
                           "or a {$ref: <fragment>} reference"],
                "grammar": "v2",
            }
        errors = v2_validate(cond)
        return {"ok": not errors, "errors": errors, "grammar": "v2"}

    @get("/{slug:str}")
    async def get_agent(self, slug: str) -> AgentInfo:
        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
        return _agent_to_info(agent)

    @post("/", guards=[cap_guard("manage_own_agents")])
    async def create_agent(self, data: AgentCreateRequest) -> dict:
        async with async_session() as session:
            existing = await session.execute(select(Agent).where(Agent.slug == data.slug))
            if existing.scalar_one_or_none():
                raise HTTPException(status_code=409, detail=f"Agent '{data.slug}' already exists")
            lifespan_type = (data.lifespan_type or "persistent").lower()
            if lifespan_type not in _LIFESPAN_TYPES:
                raise HTTPException(status_code=400,
                    detail=f"lifespan_type must be one of {sorted(_LIFESPAN_TYPES)}")
            tm = _age_resolve_trade_mode(data.trade_mode)
            agent = Agent(
                slug=data.slug, name=data.name,
                long_name=getattr(data, "long_name", None),
                description=data.description,
                conditions=data.conditions, events=data.events, actions=data.actions,
                scope=data.scope, schedule=data.schedule,
                cooldown_minutes=data.cooldown_minutes, status="inactive",
                fire_at_time=_normalize_fire_at(data.fire_at_time),
                lifespan_type=lifespan_type,
                lifespan_max_fires=data.lifespan_max_fires,
                lifespan_expires_at=_parse_iso_dt(data.lifespan_expires_at),
                trade_mode=tm,
                debounce_minutes=max(0, int(data.debounce_minutes or 0)),
                tags=list(data.tags or []),
                blackout_windows=list(data.blackout_windows or []),
            )
            session.add(agent)
            await session.commit()
        logger.info(f"Agent created: {data.slug} [lifespan={lifespan_type}]")
        return {"detail": f"Agent '{data.slug}' created"}

    def _age_apply_lifespan_fields(self, agent, data: 'AgentUpdateRequest') -> None:
        """Apply lifespan_type, lifespan_max_fires, lifespan_expires_at updates.

        Extracted from update_agent to reduce CC there."""
        new_lt = (data.lifespan_type or '').lower() if data.lifespan_type else None
        if new_lt is not None and new_lt != 'n_fires':
            agent.lifespan_max_fires = data.lifespan_max_fires
        elif data.lifespan_max_fires is not None:
            agent.lifespan_max_fires = data.lifespan_max_fires
        if data.lifespan_type is not None:
            lt = data.lifespan_type.lower()
            if lt not in _LIFESPAN_TYPES:
                raise HTTPException(status_code=400,
                    detail=f"lifespan_type must be one of {sorted(_LIFESPAN_TYPES)}")
            agent.lifespan_type = lt
        if data.lifespan_expires_at is not None:
            agent.lifespan_expires_at = _parse_iso_dt(data.lifespan_expires_at)

    @put("/{slug:str}", guards=[cap_guard("manage_own_agents")])
    async def update_agent(self, slug: str, data: AgentUpdateRequest) -> dict:
        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
            if not agent:
                raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
            for field in ('name', 'long_name', 'description', 'conditions',
                          'events', 'actions', 'scope', 'schedule',
                          'cooldown_minutes', 'debounce_minutes',
                          'tags', 'blackout_windows'):
                val = getattr(data, field, None)
                if val is not None:
                    setattr(agent, field, val)
            if data.fire_at_time is not None:
                agent.fire_at_time = _normalize_fire_at(data.fire_at_time)
            if data.trade_mode is not None:
                tm = data.trade_mode.lower()
                if tm not in _VALID_TRADE_MODES:
                    raise HTTPException(status_code=400,
                        detail=f"trade_mode must be one of {sorted(_VALID_TRADE_MODES)}")
                agent.trade_mode = tm
            self._age_apply_lifespan_fields(agent, data)
            await session.commit()
        logger.info(f"Agent updated: {slug}")
        return {"detail": f"Agent '{slug}' updated"}

    @put("/{slug:str}/activate", status_code=200, guards=[cap_guard("manage_own_agents")])
    async def activate_agent(self, slug: str) -> dict:
        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
            if not agent:
                raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
            agent.status = "active"
            await session.commit()
        return {"detail": f"Agent '{slug}' activated"}

    @put("/{slug:str}/deactivate", status_code=200, guards=[cap_guard("manage_own_agents")])
    async def deactivate_agent(self, slug: str) -> dict:
        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
            if not agent:
                raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
            agent.status = "inactive"
            await session.commit()
        return {"detail": f"Agent '{slug}' deactivated"}

    @delete("/{slug:str}", status_code=200, guards=[cap_guard("manage_own_agents")])
    async def delete_agent(self, slug: str) -> dict:
        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
            if not agent:
                raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
            if agent.is_system:
                raise HTTPException(status_code=403, detail="Cannot delete system agent")
            await session.delete(agent)
            await session.commit()
        return {"detail": f"Agent '{slug}' deleted"}

    @get("/{slug:str}/dry-run", guards=[cap_guard("manage_own_agents")])
    async def dry_run(self, slug: str) -> dict:
        """Phase 22 — evaluate this agent's condition tree against the
        CURRENT live market state. Returns what would fire WITHOUT
        firing — no audit row, no Telegram ping, no action execution.

        Use before activating an agent to answer "if I flip this on
        right now, would it immediately fire?". Closes the gap
        between the simulator (synthetic data) and activate (live
        data with no preview).

        Industry analogue: Datadog 'Test Notifications', Grafana
        'Preview Alerts'. Universal in production alerting platforms.

        Returns:
            {
              "agent_slug": str,
              "matches": list[dict],   # per-leaf match objects
              "would_fire": bool,      # matches non-empty + no schedule/cooldown block
              "blocked_by": str | None,  # 'schedule' | 'cooldown' | 'fire_at_time' | 'blackout' | 'debounce' | None
              "evaluated_at": ISO timestamp,
            }
        """
        from datetime import datetime, timezone
        from backend.api.algo.agent_engine import (
            _build_context, _in_blackout_window, _fire_at_window_active,
        )
        from backend.api.algo.agent_evaluator import (
            evaluate as v2_evaluate, Context,
        )

        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")

        now = datetime.now(timezone.utc)
        try:
            ctx = await _dry_run_context(now)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("dry-run: _dry_run_context raised")
            raise HTTPException(status_code=502, detail=f"Could not build live context: {e}")

        # Belt-and-suspenders: log the actual traceback for any unexpected
        # error in the gates/eval/response-builder below. Litestar's default
        # 500 handler swallows tracebacks silently.
        try:
            return _build_dry_run_response(agent, slug, ctx, now)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"dry-run [{slug}]: response build raised — {e!r}")
            raise HTTPException(status_code=500, detail=f"dry-run failed: {e}")

    # The actual gate / eval / response build logic lives in
    # _build_dry_run_response() at module scope so it's easily
    # try/except-able and re-usable from tests.

    @get("/{slug:str}/events")
    async def get_events(self, slug: str, n: int = 50) -> list[AgentEventInfo]:
        async with async_session() as session:
            agent_result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = agent_result.scalar_one_or_none()
            if not agent:
                raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
            result = await session.execute(
                select(AgentEvent)
                .where(AgentEvent.agent_id == agent.id)
                .order_by(AgentEvent.id.desc())
                .limit(n)
            )
            events = result.scalars().all()
        return [
            AgentEventInfo(
                id=e.id, agent_id=e.agent_id, event_type=e.event_type,
                trigger_condition=e.trigger_condition, detail=e.detail,
                timestamp=e.timestamp.isoformat() if e.timestamp else "",
                sim_mode=bool(e.sim_mode),
            )
            for e in events
        ]

    @get("/events/recent")
    async def get_recent_events(self, n: int = 100, mode: str = "live") -> list[AgentEventInfo]:
        """
        Recent agent events across every agent.

        `mode` filters by sim_mode:
          - "live" (default) → only real fires. This is what the /agents
            page wants so simulated fires from a finished sim don't
            linger in the agent log after Stop.
          - "sim"   → only sim_mode=True fires (same data /api/simulator/events
            returns, exposed here for convenience).
          - "all"   → both.
        """
        async with async_session() as session:
            query = select(AgentEvent).order_by(AgentEvent.id.desc()).limit(n)
            if mode == "live":
                query = query.where(AgentEvent.sim_mode.is_(False))
            elif mode == "sim":
                query = query.where(AgentEvent.sim_mode.is_(True))
            # "all" → no filter
            result = await session.execute(query)
            events = result.scalars().all()
        return [
            AgentEventInfo(
                id=e.id, agent_id=e.agent_id, event_type=e.event_type,
                trigger_condition=e.trigger_condition, detail=e.detail,
                timestamp=e.timestamp.isoformat() if e.timestamp else "",
                sim_mode=bool(e.sim_mode),
            )
            for e in events
        ]

    async def _age_dispatch_slug_cmd(
        self, action: str, parts: list[str],
    ) -> InterpretResponse | None:
        """Handle the five slug-argument sub-commands: status, activate,
        deactivate, events, config, fire.  Caller guarantees len(parts) > 2.
        Returns None when action doesn't match any slug command."""
        slug = parts[2]
        if action == "status":
            return await self._cmd_status(slug)
        if action == "activate":
            await self.activate_agent(slug)
            return InterpretResponse(output=f"Agent '{slug}' activated")
        if action == "deactivate":
            await self.deactivate_agent(slug)
            return InterpretResponse(output=f"Agent '{slug}' deactivated")
        if action == "events":
            return await self._cmd_events(slug)
        if action == "config":
            return await self._cmd_config(parts[2:])
        if action == "fire":
            return await self._cmd_fire(slug)
        return None

    async def _age_interpret_dispatch(
        self, action: str, parts: list[str], raw: str,
    ) -> InterpretResponse | None:
        """Route a parsed agent command to its handler.

        Returns the response when a handler matches, or None when the
        action is unknown. Extracted from interpret to reduce CC there."""
        if action == "list":
            return await self._cmd_list()
        if action == "ai":
            return await self._dispatch_ai_command(parts, raw)
        if action == "help":
            return InterpretResponse(output=self._help_text())
        if len(parts) > 2:
            result = await self._age_dispatch_slug_cmd(action, parts)
            if result is not None:
                return result
        return None

    @post("/interpret")
    async def interpret(self, data: InterpretRequest) -> InterpretResponse:
        """Parse and execute a terminal agent command."""
        parts = data.command.strip().split()
        if not parts or parts[0].lower() != "agent":
            return InterpretResponse(
                output="Usage: agent <command> [args]", success=False,
            )
        action = parts[1].lower() if len(parts) > 1 else "help"
        result = await self._age_interpret_dispatch(
            action, parts, data.command.strip()
        )
        if result is not None:
            return result
        return InterpretResponse(
            output=f"Unknown command: agent {action}\n\n{self._help_text()}",
            success=False,
        )

    async def _dispatch_ai_command(
        self, parts: list[str], raw: str,
    ) -> InterpretResponse:
        """Route `agent ai create ...` / `agent ai refine <slug> ...` to
        the create + refine handlers respectively. Common quote-strip
        + empty-prompt guard live in _extract_ai_prompt.

        Forms:
          agent ai create "<natural-language prompt>"
          agent ai refine <slug> "<natural-language refinement>"
        """
        sub = parts[2].lower() if len(parts) > 2 else ""
        if sub == "create":
            prompt = _extract_ai_prompt(
                raw, r"^agent\s+ai\s+create\s+",
            )
            if not prompt:
                return InterpretResponse(
                    output="Usage: agent ai create \"<prompt>\"",
                    success=False,
                )
            return await self._cmd_ai_create(prompt)
        if sub == "refine" and len(parts) > 3:
            slug = parts[3]
            prompt = _extract_ai_prompt(
                raw, r"^agent\s+ai\s+refine\s+\S+\s+",
            )
            if not prompt:
                return InterpretResponse(
                    output="Usage: agent ai refine <slug> \"<refinement prompt>\"",
                    success=False,
                )
            return await self._cmd_ai_refine(slug, prompt)
        return InterpretResponse(
            output=(
                "Usage:\n"
                "  agent ai create \"<prompt>\"\n"
                "  agent ai refine <slug> \"<prompt>\""
            ),
            success=False,
        )

    async def _cmd_list(self) -> InterpretResponse:
        agents = await self.list_agents()
        if not agents:
            return InterpretResponse(output="No agents configured.")
        lines = [f"{'SLUG':<22} {'NAME':<28} {'STATUS':<12} {'TRIGGERS':<8} {'LAST TRIGGERED'}"]
        lines.append("-" * 90)
        for a in agents:
            last = a.last_triggered_at[:16] if a.last_triggered_at else "—"
            lines.append(f"{a.slug:<22} {a.name:<28} {a.status:<12} {a.trigger_count:<8} {last}")
        return InterpretResponse(output="\n".join(lines))

    async def _cmd_status(self, slug: str) -> InterpretResponse:
        try:
            a = await self.get_agent(slug)
        except HTTPException:
            return InterpretResponse(output=f"Agent '{slug}' not found", success=False)
        lines = [
            f"Agent: {a.name} ({a.slug})",
            f"Status: {a.status}",
            f"Scope: {a.scope} | Schedule: {a.schedule} | Cooldown: {a.cooldown_minutes}m",
            f"Triggers: {a.trigger_count} | Last: {a.last_triggered_at or '—'}",
            f"Conditions: {json.dumps(a.conditions, indent=2)}",
            f"Events: {json.dumps(a.events)}",
            f"Actions: {json.dumps(a.actions) if a.actions else 'Alert only'}",
        ]
        if a.last_error:
            lines.append(f"Last Error: {a.last_error}")
        return InterpretResponse(output="\n".join(lines))

    async def _cmd_events(self, slug: str) -> InterpretResponse:
        try:
            events = await self.get_events(slug, n=20)
        except HTTPException:
            return InterpretResponse(output=f"Agent '{slug}' not found", success=False)
        if not events:
            return InterpretResponse(output=f"No events for agent '{slug}'")
        lines = [f"{'TIME':<20} {'TYPE':<18} {'CONDITION'}"]
        lines.append("-" * 70)
        for e in events:
            t = e.timestamp[:19] if e.timestamp else ""
            cond = e.trigger_condition or "—"
            lines.append(f"{t:<20} {e.event_type:<18} {cond[:50]}")
        return InterpretResponse(output="\n".join(lines))

    async def _cmd_config(self, parts: list) -> InterpretResponse:
        slug = parts[0]
        kv_pairs = parts[1:]
        if not kv_pairs:
            return await self._cmd_status(slug)

        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
            if not agent:
                return InterpretResponse(output=f"Agent '{slug}' not found", success=False)

            conditions = agent.conditions or {}
            for kv in kv_pairs:
                if "=" not in kv:
                    continue
                key, val = kv.split("=", 1)
                _age_apply_kv_to_conditions(conditions, key, val)

            agent.conditions = conditions
            await session.commit()

        return InterpretResponse(output=f"Agent '{slug}' config updated")

    async def _age_uniquify_ai_slug(self, session, base: str) -> str:
        """Find the first available slug for a new AI agent.

        Tries base, then base-2, base-3, … until an unused slug is found.
        Extracted from _cmd_ai_create to reduce CC there."""
        slug = base
        n = 1
        while True:
            existing = await session.execute(
                select(Agent).where(Agent.slug == slug)
            )
            if not existing.scalar_one_or_none():
                return slug
            n += 1
            slug = f"{base}-{n}"

    async def _cmd_ai_create(self, prompt: str) -> InterpretResponse:
        """Build an agent draft from an NL prompt and persist it (inactive,
        paper, one_shot). Operator activates / widens via /agents."""
        from backend.api.algo.agent_ai import draft_agent_from_prompt
        d = draft_agent_from_prompt(prompt)
        if d.errors:
            err_block = "\n  ".join(d.errors)
            return InterpretResponse(
                output=f"AI draft failed:\n  {err_block}", success=False,
            )
        draft = d.draft
        base = re.sub(r"[^a-z0-9]+", "-",
                      (draft.get("name") or "ai-agent").lower()).strip("-") or "ai-agent"
        async with async_session() as session:
            slug = await self._age_uniquify_ai_slug(session, base)
            agent = _age_build_ai_agent_row(slug, prompt, draft, d.why_summary)
            session.add(agent)
            await session.commit()
        warn_block = ("\n  ⚠ ".join(d.warnings)) if d.warnings else "(none)"
        out = (
            f"✓ Agent '{slug}' created (inactive · paper · {agent.lifespan_type})\n"
            f"  Why: {d.why_summary}\n"
            f"  Warnings:\n  ⚠ {warn_block}\n"
            f"  Activate via: agent activate {slug}"
        )
        return InterpretResponse(output=out)

    async def _cmd_fire(self, slug: str) -> InterpretResponse:
        """Manually fire an agent right now — bypasses cooldown / schedule /
        baseline / suppression. Forces paper mode regardless of agent.trade_mode
        so a stray fire can never hit the broker. Returns whether the
        condition matched and any actions executed."""
        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
        if not agent:
            return InterpretResponse(output=f"Agent '{slug}' not found", success=False)
        # Build context from current live data (or stub on broker outage).
        # Re-uses the same fetch + summarise plumbing the background task
        # uses each tick so the manual fire sees identical inputs to a
        # natural fire.
        try:
            from backend.api.background import (
                _fetch_holdings_direct, _fetch_positions_direct,
                _fetch_margins_direct,
            )
            from backend.shared.helpers.summarise import (
                summarise_holdings as _sum_h, summarise_positions as _sum_p,
            )
            df_h, sum_h_raw = _fetch_holdings_direct()
            df_p, _         = _fetch_positions_direct()
            df_m            = _fetch_margins_direct()
            sum_h = _sum_h(df_h, sum_h_raw, None)
            sum_p = _sum_p(df_p)
        except Exception as e:
            return InterpretResponse(
                output=f"Could not fetch live book: {e}", success=False,
            )
        from datetime import datetime, timezone
        ctx = {
            "now": datetime.now(timezone.utc),
            "sum_holdings":  sum_h,
            "sum_positions": sum_p,
            "df_margins":    df_m,
            "alert_state":   {},
            "force_paper":   True,           # never hits the real broker
            "manual_fire":   True,           # surfaces on AgentEvent.detail
        }
        from backend.api.algo.agent_engine import run_cycle
        await run_cycle(
            ctx,
            broadcast_fn=None,
            only_agent_ids=[agent.id],
            bypass_schedule=True,
            bypass_suppression=True,
        )
        # Read the latest event for this agent so the output reports the
        # actual outcome (matched/skipped/action executed).
        async with async_session() as session:
            from backend.api.models import AgentEvent
            r = await session.execute(
                select(AgentEvent)
                .where(AgentEvent.agent_id == agent.id)
                .order_by(AgentEvent.timestamp.desc())
                .limit(1)
            )
            ev = r.scalar_one_or_none()
        verdict = (
            f"event={ev.event_type} · {ev.detail or ''}".strip(' ·')
            if ev else "no event recorded"
        )
        return InterpretResponse(
            output=(f"Fired '{slug}' (paper) — {verdict}\n"
                    f"View events: agent events {slug}")
        )

    async def _cmd_ai_refine(self, slug: str, prompt: str) -> InterpretResponse:
        """Apply a natural-language refinement to an existing agent.

        Loads the current agent JSON, asks the LLM to produce an updated
        version that incorporates the operator's instruction, validates,
        and PATCHes the row in place. Slug + status + trigger_count are
        preserved; only conditions / events / actions / scope / schedule /
        cooldown / lifespan / trade_mode can change."""
        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
        if not agent:
            return InterpretResponse(output=f"Agent '{slug}' not found", success=False)
        # Compose a refinement prompt that hands the LLM the current JSON.
        current = {
            "name":             agent.name,
            "description":      agent.description,
            "conditions":       agent.conditions,
            "events":           agent.events,
            "actions":          agent.actions,
            "scope":            agent.scope,
            "schedule":         agent.schedule,
            "cooldown_minutes": agent.cooldown_minutes,
            "lifespan_type":    agent.lifespan_type,
            "trade_mode":       agent.trade_mode,
        }
        refinement = (
            f"Refine this existing agent JSON. Apply the operator's instruction "
            f"as a minimal diff — only change what's necessary, preserve everything else.\n\n"
            f"Existing agent:\n{json.dumps(current, indent=2)}\n\n"
            f"Operator instruction: {prompt}"
        )
        from backend.api.algo.agent_ai import draft_agent_from_prompt
        d = draft_agent_from_prompt(refinement)
        if d.errors:
            return InterpretResponse(
                output="Refine failed:\n  " + "\n  ".join(d.errors), success=False,
            )
        new = d.draft
        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            row = result.scalar_one_or_none()
            if not row:
                return InterpretResponse(output=f"Agent '{slug}' disappeared", success=False)
            # Stricter PATCH — only setattr fields that genuinely DIFFER
            # from the existing value. Preserves any field the LLM
            # reproduced verbatim and reports the diff so the operator
            # sees exactly what changed.
            changed: list[str] = []
            for fld in ("name", "description", "conditions", "events", "actions",
                        "scope", "schedule", "cooldown_minutes",
                        "lifespan_type", "trade_mode"):
                if fld not in new or new[fld] is None:
                    continue
                if getattr(row, fld) != new[fld]:
                    setattr(row, fld, new[fld])
                    changed.append(fld)
            await session.commit()
        warn_block = ("\n  ⚠ ".join(d.warnings)) if d.warnings else "(none)"
        diff_block = (", ".join(changed)) if changed else "(no changes — refinement was a no-op)"
        return InterpretResponse(
            output=(f"✓ Agent '{slug}' refined\n"
                    f"  Why: {d.why_summary}\n"
                    f"  Changed: {diff_block}\n"
                    f"  Warnings:\n  ⚠ {warn_block}")
        )

    def _help_text(self) -> str:
        return """Agent Commands:
  agent list                        — list all agents
  agent status <slug>               — detailed agent info
  agent activate <slug>             — activate agent
  agent deactivate <slug>           — deactivate agent
  agent fire <slug>                 — fire once now (paper · bypasses gates)
  agent events <slug>               — recent events
  agent config <slug> key=value     — update condition params
  agent ai create "<prompt>"        — draft an agent from natural language
                                      (lands inactive · paper · one_shot)
  agent ai refine <slug> "<prompt>" — apply an NL refinement to an agent
  agent help                        — this help"""
