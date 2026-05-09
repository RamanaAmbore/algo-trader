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

from backend.api.auth_guard import admin_guard, auth_or_demo_guard
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
    description: str | None
    conditions: dict
    events: list
    actions: list
    scope: str
    schedule: str | None
    cooldown_minutes: int
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
    scope: str = "total"
    schedule: str = "market_hours"
    cooldown_minutes: int = 30
    # Lifespan accepted on create so algos spawning agents can declare
    # one-shot or bounded behaviour up front. Defaults preserve the
    # existing persistent shape.
    lifespan_type:        str        = "persistent"
    lifespan_max_fires:   int | None = None
    lifespan_expires_at:  str | None = None  # ISO datetime
    # null = inherit from execution.default_agent_trade_mode setting.
    trade_mode:           str | None = None


class AgentUpdateRequest(msgspec.Struct):
    name: str | None = None
    description: str | None = None
    conditions: dict | None = None
    events: list | None = None
    actions: list | None = None
    scope: str | None = None
    schedule: str | None = None
    cooldown_minutes: int | None = None
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
    success: bool = True


# Legacy field/operator metadata (CONDITION_FIELDS, CONDITION_OPERATORS,
# ACTION_TYPES, EVENT_CHANNELS) is retired. The frontend now reads the full
# grammar from GET /api/admin/grammar/tokens — metrics, scopes, operators,
# channels, templates, and actions are all catalogued in `grammar_tokens`
# and extensible at runtime. See backend/api/algo/grammar_registry.py.


_VALID_TRADE_MODES = {"paper", "live"}


def _agent_to_info(a: Agent) -> AgentInfo:
    return AgentInfo(
        id=a.id, slug=a.slug, name=a.name, description=a.description,
        conditions=a.conditions or {}, events=a.events or [],
        actions=a.actions or [], scope=a.scope,
        schedule=a.schedule, cooldown_minutes=a.cooldown_minutes,
        status=a.status,
        last_triggered_at=a.last_triggered_at.isoformat() if a.last_triggered_at else None,
        trigger_count=a.trigger_count, last_error=a.last_error,
        is_system=a.is_system,
        lifespan_type=getattr(a, "lifespan_type", "persistent") or "persistent",
        lifespan_max_fires=getattr(a, "lifespan_max_fires", None),
        lifespan_expires_at=(
            a.lifespan_expires_at.isoformat()
            if getattr(a, "lifespan_expires_at", None) else None
        ),
        trade_mode=getattr(a, "trade_mode", "paper") or "paper",
    )


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class AgentController(Controller):
    # Controller-level guard allows anonymous demo on prod; the
    # write endpoints (POST/PUT/DELETE) override with admin_guard
    # so demo visitors get 401 on any mutation. Reads (GET) flow
    # through auth_or_demo_guard so the /agents page populates for
    # an anonymous visitor.
    path = "/api/agents"
    guards = [auth_or_demo_guard]

    @get("/")
    async def list_agents(self) -> list[AgentInfo]:
        async with async_session() as session:
            result = await session.execute(select(Agent).order_by(Agent.id))
            agents = result.scalars().all()
        return [_agent_to_info(a) for a in agents]

    @post("/ai-draft", guards=[admin_guard])
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
        )

    @post("/validate-condition", guards=[admin_guard])
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
                           "either a metric/scope leaf or an all/any/not composite"],
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

    @post("/", guards=[admin_guard])
    async def create_agent(self, data: AgentCreateRequest) -> dict:
        async with async_session() as session:
            existing = await session.execute(select(Agent).where(Agent.slug == data.slug))
            if existing.scalar_one_or_none():
                raise HTTPException(status_code=409, detail=f"Agent '{data.slug}' already exists")
            lifespan_type = (data.lifespan_type or "persistent").lower()
            if lifespan_type not in _LIFESPAN_TYPES:
                raise HTTPException(status_code=400,
                    detail=f"lifespan_type must be one of {sorted(_LIFESPAN_TYPES)}")
            # trade_mode: null in payload → inherit from setting; explicit
            # value must be paper/live or 400.
            tm = (data.trade_mode or "").lower() or None
            if tm is not None and tm not in _VALID_TRADE_MODES:
                raise HTTPException(status_code=400,
                    detail=f"trade_mode must be one of {sorted(_VALID_TRADE_MODES)}")
            if tm is None:
                from backend.shared.helpers.settings import get_string
                tm = get_string("execution.default_agent_trade_mode", "paper")
                if tm not in _VALID_TRADE_MODES:
                    tm = "paper"
            agent = Agent(
                slug=data.slug, name=data.name, description=data.description,
                conditions=data.conditions, events=data.events, actions=data.actions,
                scope=data.scope, schedule=data.schedule,
                cooldown_minutes=data.cooldown_minutes, status="inactive",
                lifespan_type=lifespan_type,
                lifespan_max_fires=data.lifespan_max_fires,
                lifespan_expires_at=_parse_iso_dt(data.lifespan_expires_at),
                trade_mode=tm,
            )
            session.add(agent)
            await session.commit()
        logger.info(f"Agent created: {data.slug} [lifespan={lifespan_type}]")
        return {"detail": f"Agent '{data.slug}' created"}

    @put("/{slug:str}", guards=[admin_guard])
    async def update_agent(self, slug: str, data: AgentUpdateRequest) -> dict:
        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
            if not agent:
                raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
            for field in ('name', 'description', 'conditions', 'events', 'actions',
                          'scope', 'schedule', 'cooldown_minutes'):
                val = getattr(data, field, None)
                if val is not None:
                    setattr(agent, field, val)
            if data.trade_mode is not None:
                tm = data.trade_mode.lower()
                if tm not in _VALID_TRADE_MODES:
                    raise HTTPException(status_code=400,
                        detail=f"trade_mode must be one of {sorted(_VALID_TRADE_MODES)}")
                agent.trade_mode = tm
            # lifespan_max_fires can legitimately be cleared back to NULL
            # (e.g. switching off n_fires). The msgspec.Struct default is
            # None and we can't distinguish "omitted" from "explicit null",
            # so the convention is: when lifespan_type is being changed
            # AWAY from n_fires, clear max_fires unless the payload also
            # supplied a fresh value. When lifespan_type is unchanged, an
            # explicit null in the payload is honoured.
            new_lt = (data.lifespan_type or '').lower() if data.lifespan_type else None
            if new_lt is not None and new_lt != 'n_fires':
                agent.lifespan_max_fires = data.lifespan_max_fires  # may be None
            elif data.lifespan_max_fires is not None:
                agent.lifespan_max_fires = data.lifespan_max_fires
            # Validate + normalise lifespan_type when supplied.
            if data.lifespan_type is not None:
                lt = data.lifespan_type.lower()
                if lt not in _LIFESPAN_TYPES:
                    raise HTTPException(status_code=400,
                        detail=f"lifespan_type must be one of {sorted(_LIFESPAN_TYPES)}")
                agent.lifespan_type = lt
            # ISO datetime parse for lifespan_expires_at.
            if data.lifespan_expires_at is not None:
                agent.lifespan_expires_at = _parse_iso_dt(data.lifespan_expires_at)
            await session.commit()
        logger.info(f"Agent updated: {slug}")
        return {"detail": f"Agent '{slug}' updated"}

    @put("/{slug:str}/activate", status_code=200, guards=[admin_guard])
    async def activate_agent(self, slug: str) -> dict:
        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
            if not agent:
                raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
            agent.status = "active"
            await session.commit()
        return {"detail": f"Agent '{slug}' activated"}

    @put("/{slug:str}/deactivate", status_code=200, guards=[admin_guard])
    async def deactivate_agent(self, slug: str) -> dict:
        async with async_session() as session:
            result = await session.execute(select(Agent).where(Agent.slug == slug))
            agent = result.scalar_one_or_none()
            if not agent:
                raise HTTPException(status_code=404, detail=f"Agent '{slug}' not found")
            agent.status = "inactive"
            await session.commit()
        return {"detail": f"Agent '{slug}' deactivated"}

    @delete("/{slug:str}", status_code=200, guards=[admin_guard])
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

    @post("/interpret")
    async def interpret(self, data: InterpretRequest) -> InterpretResponse:
        """Parse and execute a terminal agent command."""
        parts = data.command.strip().split()
        if not parts or parts[0].lower() != "agent":
            return InterpretResponse(output="Usage: agent <command> [args]", success=False)

        action = parts[1].lower() if len(parts) > 1 else "help"

        if action == "list":
            return await self._cmd_list()
        elif action == "status" and len(parts) > 2:
            return await self._cmd_status(parts[2])
        elif action == "activate" and len(parts) > 2:
            await self.activate_agent(parts[2])
            return InterpretResponse(output=f"Agent '{parts[2]}' activated")
        elif action == "deactivate" and len(parts) > 2:
            await self.deactivate_agent(parts[2])
            return InterpretResponse(output=f"Agent '{parts[2]}' deactivated")
        elif action == "events" and len(parts) > 2:
            return await self._cmd_events(parts[2])
        elif action == "config" and len(parts) > 2:
            return await self._cmd_config(parts[2:])
        elif action == "ai":
            # Form: agent ai create "<prompt>"
            sub = parts[2].lower() if len(parts) > 2 else ""
            if sub != "create":
                return InterpretResponse(
                    output="Usage: agent ai create \"<natural language prompt>\"",
                    success=False,
                )
            # Reconstruct the prompt from the rest of the original input,
            # not the split parts (preserves quoted whitespace).
            raw = data.command.strip()
            # Drop the leading 'agent ai create' prefix; trim quotes if any.
            prefix_match = re.match(r"^agent\s+ai\s+create\s+", raw, re.IGNORECASE)
            prompt = raw[prefix_match.end():].strip() if prefix_match else ""
            if (prompt.startswith('"') and prompt.endswith('"')) or \
               (prompt.startswith("'") and prompt.endswith("'")):
                prompt = prompt[1:-1].strip()
            if not prompt:
                return InterpretResponse(
                    output="Usage: agent ai create \"<natural language prompt>\"",
                    success=False,
                )
            return await self._cmd_ai_create(prompt)
        elif action == "help":
            return InterpretResponse(output=self._help_text())
        else:
            return InterpretResponse(output=f"Unknown command: agent {action}\n\n{self._help_text()}", success=False)

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
                # Update leaf condition value
                if "rules" in conditions:
                    for rule in conditions.get("rules", []):
                        if rule.get("field") == key:
                            try:
                                rule["value"] = float(val) if "." in val else int(val)
                            except ValueError:
                                rule["value"] = val
                elif conditions.get("field") == key:
                    try:
                        conditions["value"] = float(val) if "." in val else int(val)
                    except ValueError:
                        conditions["value"] = val

            agent.conditions = conditions
            await session.commit()

        return InterpretResponse(output=f"Agent '{slug}' config updated")

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
        # Slug — kebab-case the LLM-given name; uniquify with a counter.
        base = re.sub(r"[^a-z0-9]+", "-",
                      (draft.get("name") or "ai-agent").lower()).strip("-") or "ai-agent"
        slug = base
        async with async_session() as session:
            n = 1
            while True:
                existing = await session.execute(select(Agent).where(Agent.slug == slug))
                if not existing.scalar_one_or_none():
                    break
                n += 1
                slug = f"{base}-{n}"
            agent = Agent(
                slug=slug,
                name=draft.get("name") or "AI agent",
                description=draft.get("description") or d.why_summary,
                conditions=draft.get("conditions") or {},
                events=draft.get("events") or ["telegram", "email"],
                actions=draft.get("actions") or [],
                scope=draft.get("scope") or "total",
                schedule=draft.get("schedule") or "market_hours",
                cooldown_minutes=int(draft.get("cooldown_minutes") or 30),
                lifespan_type=draft.get("lifespan_type") or "one_shot",
                trade_mode="paper",            # AI agents always paper
                status="inactive",             # AI agents always inactive
            )
            session.add(agent)
            await session.commit()
        # Format the response with the verdict.
        warn_block = ("\n  ⚠ ".join(d.warnings)) if d.warnings else "(none)"
        out = (
            f"✓ Agent '{slug}' created (inactive · paper · {agent.lifespan_type})\n"
            f"  Why: {d.why_summary}\n"
            f"  Warnings:\n  ⚠ {warn_block}\n"
            f"  Activate via: agent activate {slug}"
        )
        return InterpretResponse(output=out)

    def _help_text(self) -> str:
        return """Agent Commands:
  agent list                        — list all agents
  agent status <slug>               — detailed agent info
  agent activate <slug>             — activate agent
  agent deactivate <slug>           — deactivate agent
  agent events <slug>               — recent events
  agent config <slug> key=value     — update condition params
  agent ai create "<prompt>"        — draft an agent from natural language
                                      (lands inactive · paper · one_shot)
  agent help                        — this help"""
