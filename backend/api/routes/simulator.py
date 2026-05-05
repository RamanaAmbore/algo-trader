"""
Market-simulator control plane — `/api/simulator/*`.

Every endpoint is admin-guarded AND additionally refuses to run when the
per-branch capability flag is off (see `SimDriver.assert_enabled`). Pairs
with `backend/api/algo/sim/driver.py` and
`frontend/src/routes/(algo)/admin/simulator`.

Endpoints
  GET  /api/simulator/scenarios           — list available scenarios
  GET  /api/simulator/status              — driver snapshot (active / tick / etc)
  POST /api/simulator/start               — begin a scenario at a given cadence
  POST /api/simulator/stop                — halt the sim
  POST /api/simulator/step                — apply a single tick (deterministic debug)
  POST /api/simulator/run-cycle           — immediately run the agent engine against
                                            the current sim state
  POST /api/simulator/seed-live           — snapshot live broker data as sim baseline
  POST /api/simulator/clear               — wipe sim_mode rows from agent_events
                                            and algo_orders (handy between runs)
  GET  /api/simulator/events/recent       — recent sim_mode agent events
  GET  /api/simulator/orders/recent       — recent sim_mode algo orders
  GET  /api/simulator/ticks/recent        — rolling buffer of recent ticks

The former `/api/test/*` routes are retired — the simulator owns the
vocabulary end-to-end now.
"""

from __future__ import annotations

from typing import Optional

import msgspec
from litestar import Controller, get, post
from litestar.exceptions import HTTPException
from sqlalchemy import delete as sql_delete, desc, select

from backend.api.algo.sim.driver import (
    SimGuardError,
    get_driver,
    load_scenarios,
)
from backend.api.algo.sim.synthesize import synthesize_for_agent, SynthesizeError
from backend.api.auth_guard import admin_guard, auth_or_demo_guard
from backend.api.database import async_session
from backend.api.models import Agent, AgentEvent, AlgoOrder
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SimStartRequest(msgspec.Struct):
    scenario: str
    rate_ms: int = 2000
    # 'scripted' (default, use scenario.initial),
    # 'live' (use last seed-live snapshot, no scenario initial),
    # 'live+scenario' (snapshot + scripted initial layered on top).
    seed_mode: str = "scripted"
    # When non-empty, restrict the run to these agent IDs and bypass the
    # schedule / cooldown / baseline gates so the operator can dry-fire a
    # single agent from the /algo page.
    agent_ids: Optional[list[int]] = None
    # Positions cadence override. None = use scenario YAML value, falling
    # back to the DB setting `simulator.positions_every_n_ticks`. Clamped
    # to >= 1 server-side. (Positions-only sim — no holdings cadence.)
    positions_every_n_ticks: Optional[int] = None
    # Market-state preset override. One of: pre_open / at_open /
    # mid_session / pre_close / at_close / post_close / expiry_day.
    # None = use scenario YAML's market_state block (or default mid_session).
    market_state_preset: Optional[str] = None
    # Per-tick % overrides — when the scenario is of "3 pct moves" shape
    # (crash / euphoria / extreme / wild-swings), each entry in this list
    # replaces the corresponding tick's pct value. Units: decimal
    # fraction (0.05 = 5%). Missing / None = keep YAML default.
    pct_overrides: Optional[list[float]] = None
    # Restrict the sim to a subset of tradingsymbols — after seeding,
    # positions whose tradingsymbol isn't in this list are dropped.
    # Empty / None = all positions. Useful for "what if only NIFTY…?"
    symbols: Optional[list[str]] = None
    # Bid/ask spread sent as a percent (0.10 = 10 bps = 0.10% spread).
    # None = fall back to DB setting `simulator.default_spread_pct`.
    spread_pct: Optional[float] = None
    # Custom positions added inline from the UI — each row is the minimal
    # position dict {tradingsymbol, quantity, last_price, account?,
    # average_price?, exchange?}. Layered on top of scripted/live seeding so
    # an operator can stress-test a synthetic NIFTY24500CE without changing
    # their real book or editing scenarios.yaml.
    custom_positions: Optional[list[dict]] = None


class SimScenarioInfo(msgspec.Struct):
    slug: str
    name: str
    description: str
    mode: str
    ticks: int
    has_initial: bool
    # Per-tick pct defaults extracted from the scenario's YAML. UI renders
    # these as editable fields so operators can tweak the magnitude before
    # Start. Entries are None for ticks that aren't pct-shaped
    # (target_pnl / random_walk / set_margin).
    tick_pcts: list[float | None]
    # Distinct tradingsymbols from the scenario's scripted initial
    # positions. Used by the Symbol picker when the operator hasn't
    # loaded a live-book snapshot yet.
    initial_symbols: list[str]


class SimEventInfo(msgspec.Struct):
    id: int
    agent_id: int
    event_type: str
    trigger_condition: str | None
    detail: str | None
    timestamp: str


class SimOrderInfo(msgspec.Struct):
    id: int
    account: str
    symbol: str
    exchange: str
    transaction_type: str
    quantity: int
    initial_price: float | None   # LIMIT price = sim's LTP when the agent fired
    status: str
    engine: str
    created_at: str
    detail: str | None


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class SimulatorController(Controller):
    """Simulator control plane. Prefix keeps every endpoint easy to spot."""

    path = "/api/simulator"
    guards = [admin_guard]

    @get("/scenarios")
    async def list_scenarios(self) -> list[SimScenarioInfo]:
        out = []
        for s in get_driver().scenarios_manifest():
            out.append(SimScenarioInfo(
                slug=s["slug"], name=s["name"],
                description=s["description"], mode=s["mode"], ticks=s["ticks"],
                has_initial=s["has_initial"],
                tick_pcts=s.get("tick_pcts") or [],
                initial_symbols=s.get("initial_symbols") or [],
            ))
        return out

    @get("/status", guards=[auth_or_demo_guard])
    async def status(self) -> dict:
        """
        Snapshot + per-branch capability flag. The `enabled` key lets the
        frontend pre-disable the simulator form on branches where
        `cap_in_<branch>.simulator` is off (default: False on prod), so
        buttons grey out with an explanatory banner instead of the
        operator having to press Start to discover the gate.
        """
        from backend.shared.helpers.utils import config, is_enabled
        snap = get_driver().snapshot()
        snap["enabled"] = is_enabled("simulator")
        snap["branch"]  = config.get("deploy_branch", "dev")
        return snap

    @post("/start-for-agent/{agent_id:int}")
    async def start_for_agent(self, agent_id: int, rate_ms: int = 2000) -> dict:
        """
        Start the sim against a scenario **synthesised from one agent's
        condition tree**. The scenario is built at call-time (nothing lives
        in scenarios.yaml for it) — zero maintenance when the agent catalog
        changes. Bypasses suppression + schedule gates so the operator
        gets immediate feedback.
        """
        from sqlalchemy import select
        async with async_session() as s:
            row = (await s.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail=f"Agent id={agent_id} not found")
        try:
            scen = synthesize_for_agent(row)
        except SynthesizeError as e:
            raise HTTPException(status_code=400, detail=str(e))
        try:
            return get_driver().start(
                scen["slug"], rate_ms,
                seed_mode="scripted",
                only_agent_ids=[agent_id],
                inline_scenario=scen,
            )
        except SimGuardError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @post("/start")
    async def start(self, data: SimStartRequest) -> dict:
        try:
            market_state_override = (
                {"preset": data.market_state_preset}
                if data.market_state_preset else None
            )
            # Convert percent (UI surface) to decimal fraction (internal).
            spread_fraction = (
                max(0.0, float(data.spread_pct)) / 100.0
                if data.spread_pct is not None else None
            )
            return get_driver().start(
                data.scenario, data.rate_ms,
                seed_mode=data.seed_mode,
                only_agent_ids=data.agent_ids,
                positions_every_n_ticks=data.positions_every_n_ticks,
                market_state_override=market_state_override,
                pct_overrides=data.pct_overrides,
                symbol_filter=data.symbols,
                spread_pct=spread_fraction,
                custom_positions=data.custom_positions,
            )
        except SimGuardError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @post("/stop")
    async def stop(self) -> dict:
        return get_driver().stop()

    @post("/step")
    async def step(self) -> dict:
        try:
            return get_driver().step()
        except SimGuardError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @post("/seed-live")
    async def seed_live(self) -> dict:
        """
        Snapshot live holdings + positions + margins into the driver's
        `_live_snapshot` field so the next `start(seed_mode=live|live+scenario)`
        uses the real book as the starting state. Bypasses the in-process
        cache so the snapshot is fresh at the moment of the call.
        """
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, get_driver().seed_live)
        except SimGuardError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @post("/run-cycle")
    async def run_cycle_now(self) -> dict:
        """
        Run the agent engine against the current sim state immediately.
        Useful for step-mode debugging — advance one tick, then ask the
        engine to evaluate without waiting for the next background tick.
        """
        try:
            drv = get_driver()
            if not drv.active and not drv.scenario:
                raise SimGuardError("No scenario active. Start or step first.")
            from backend.api.algo.agent_engine import run_cycle
            from backend.api.routes.algo import _broadcast_event
            from backend.shared.helpers.date_time_utils import timestamp_indian, timestamp_display

            sum_h, sum_p, df_m = drv.dataframes()
            ctx = {
                "sum_holdings":   sum_h,
                "sum_positions":  sum_p,
                "df_margins":     df_m,
                "now":            timestamp_indian(),
                "ist_display":    timestamp_display(),
                "seg_state":      {},
                "segments":       [],
                # Propagated into V2Context → picked up by _dispatch and actions
                "alert_state":    {"sim_mode": True},
                "sim_mode":       True,
                "market_state":   dict(drv.market_state),
            }
            isolated = bool(drv.only_agent_ids)
            await run_cycle(
                ctx, _broadcast_event,
                only_agent_ids=drv.only_agent_ids,
                bypass_schedule=True,
                bypass_suppression=isolated,
            )
            return {"ok": True, "sim": drv.snapshot()}
        except SimGuardError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @post("/clear")
    async def clear_sim_rows(self) -> dict:
        """Delete every sim_mode row from agent_events and algo_orders."""
        async with async_session() as s:
            ev = await s.execute(sql_delete(AgentEvent).where(AgentEvent.sim_mode.is_(True)))
            od = await s.execute(sql_delete(AlgoOrder).where(AlgoOrder.mode == "sim"))
            await s.commit()
        return {"events_deleted": ev.rowcount or 0, "orders_deleted": od.rowcount or 0}

    @get("/events/recent")
    async def recent_events(self, limit: Optional[int] = 50) -> list[SimEventInfo]:
        limit = max(1, min(int(limit or 50), 500))
        async with async_session() as s:
            rows = (await s.execute(
                select(AgentEvent)
                .where(AgentEvent.sim_mode.is_(True))
                .order_by(desc(AgentEvent.timestamp))
                .limit(limit)
            )).scalars().all()
        return [
            SimEventInfo(
                id=r.id, agent_id=r.agent_id, event_type=r.event_type,
                trigger_condition=r.trigger_condition, detail=r.detail,
                timestamp=r.timestamp.isoformat() if r.timestamp else "",
            )
            for r in rows
        ]

    @get("/ticks/recent")
    async def recent_ticks(self, limit: Optional[int] = 100) -> list[dict]:
        """
        Rolling buffer of recent simulator ticks (oldest-first). Returned
        entries include the moves applied and a per-field diff so the UI
        can render a compact timeline. Empty when no sim has run since
        process start.
        """
        return get_driver().recent_ticks(int(limit or 100))

    @get("/orders/recent")
    async def recent_orders(self, limit: Optional[int] = 50) -> list[SimOrderInfo]:
        limit = max(1, min(int(limit or 50), 500))
        async with async_session() as s:
            rows = (await s.execute(
                select(AlgoOrder)
                .where(AlgoOrder.mode == "sim")
                .order_by(desc(AlgoOrder.created_at))
                .limit(limit)
            )).scalars().all()
        return [
            SimOrderInfo(
                id=r.id, account=r.account, symbol=r.symbol, exchange=r.exchange,
                transaction_type=r.transaction_type, quantity=r.quantity,
                initial_price=(float(r.initial_price) if r.initial_price is not None else None),
                status=r.status, engine=r.engine,
                created_at=r.created_at.isoformat() if r.created_at else "",
                detail=r.detail,
            )
            for r in rows
        ]
