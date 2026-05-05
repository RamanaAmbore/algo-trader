"""
Operator health check endpoint.

GET /api/admin/health — single round-trip snapshot of the platform state:
  branch, git HEAD, broker accounts, DB row counts, in-process cache size,
  simulator status, paper-engine status, and per-account source IPs.

Admin-only. No demo access.
"""

import subprocess
from typing import Optional

import msgspec
from litestar import Controller, get
from litestar.exceptions import HTTPException
from sqlalchemy import func, select

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session
from backend.api.models import (
    Agent,
    AgentEvent,
    AlgoOrder,
    BrokerAccount,
    NewsHeadline,
    User,
)
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config, mask_column

import pandas as pd

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class BrokerStatus(msgspec.Struct):
    account: str        # masked — ZG#### even for admin; IPs identify better
    status: str         # "LOADED" / "PENDING" / "DISABLED"
    api_key_last4: str  # last 4 chars of plaintext api_key for identification
    source_ip: str      # empty string when not configured


class DBStats(msgspec.Struct):
    users: int
    agents: int
    algo_orders: int
    agent_events: int
    news_headlines: int


class CacheStats(msgspec.Struct):
    keys: int   # rough count of live entries in the in-process TTL cache


class SimStatus(msgspec.Struct):
    active: bool
    scenario: Optional[str]
    tick_count: int


class PaperStatus(msgspec.Struct):
    enabled: bool       # True only when deploy_branch == 'main'
    open_order_count: int


class HealthResponse(msgspec.Struct):
    branch: str
    git_hash: str       # short commit hash, "unknown" on failure
    git_subject: str    # commit subject, "unknown" on failure
    broker_accounts: list[BrokerStatus]
    db: DBStats
    cache: CacheStats
    sim: SimStatus
    paper: PaperStatus
    ipv6: list[str]     # source_ip values configured across all accounts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_hash() -> str:
    """Short commit hash for HEAD; 'unknown' if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def _git_subject() -> str:
    """Commit subject for HEAD; 'unknown' if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out or "unknown"
    except Exception:
        return "unknown"


async def _fetch_broker_statuses() -> tuple[list[BrokerStatus], list[str]]:
    try:
        from backend.shared.helpers.connections import Connections
        loaded_accounts: set[str] = set(Connections().conn.keys())
    except Exception:
        loaded_accounts = set()

    statuses: list[BrokerStatus] = []
    ipv6_list: list[str] = []

    try:
        async with async_session() as session:
            rows = (await session.execute(
                select(BrokerAccount).order_by(BrokerAccount.account)
            )).scalars().all()

        for row in rows:
            masked = mask_column(pd.Series([row.account]))[0]
            if not row.is_active:
                status = "DISABLED"
            elif row.account in loaded_accounts:
                status = "LOADED"
            else:
                status = "PENDING"

            api_key_last4 = (row.api_key or "")[-4:] or "????"
            source_ip = row.source_ip or ""

            statuses.append(BrokerStatus(
                account=masked,
                status=status,
                api_key_last4=api_key_last4,
                source_ip=source_ip,
            ))

            if source_ip:
                ipv6_list.append(source_ip)

    except Exception as exc:
        logger.warning(f"health: broker_accounts query failed: {exc}")

    return statuses, ipv6_list


async def _fetch_db_stats() -> DBStats:
    try:
        async with async_session() as session:
            users = (await session.execute(
                select(func.count()).select_from(User)
            )).scalar_one()
            agents = (await session.execute(
                select(func.count()).select_from(Agent)
            )).scalar_one()
            algo_orders = (await session.execute(
                select(func.count()).select_from(AlgoOrder)
            )).scalar_one()
            agent_events = (await session.execute(
                select(func.count()).select_from(AgentEvent)
            )).scalar_one()
            news_headlines = (await session.execute(
                select(func.count()).select_from(NewsHeadline)
            )).scalar_one()
        return DBStats(
            users=int(users),
            agents=int(agents),
            algo_orders=int(algo_orders),
            agent_events=int(agent_events),
            news_headlines=int(news_headlines),
        )
    except Exception as exc:
        logger.warning(f"health: DB stats query failed: {exc}")
        return DBStats(users=0, agents=0, algo_orders=0, agent_events=0, news_headlines=0)


def _cache_stats() -> CacheStats:
    try:
        from backend.api import cache as _cache_mod
        return CacheStats(keys=len(_cache_mod._store))
    except Exception:
        return CacheStats(keys=0)


def _sim_status() -> SimStatus:
    try:
        from backend.api.algo.sim.driver import SimDriver
        snap = SimDriver.instance().snapshot()
        return SimStatus(
            active=bool(snap.get("active", False)),
            scenario=snap.get("scenario") or None,
            tick_count=int(snap.get("tick_index", 0)),
        )
    except Exception:
        return SimStatus(active=False, scenario=None, tick_count=0)


def _paper_status() -> PaperStatus:
    branch = config.get("deploy_branch", "")
    enabled = branch == "main"
    try:
        from backend.api.algo.paper import get_prod_paper_engine
        engine = get_prod_paper_engine()
        open_count = sum(
            1 for o in engine._open_orders if o.get("status") == "OPEN"
        )
        return PaperStatus(enabled=enabled, open_order_count=open_count)
    except Exception:
        return PaperStatus(enabled=enabled, open_order_count=0)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class HealthController(Controller):
    path = "/api/admin/health"
    guards = [admin_guard]

    @get("")
    async def get_health(self) -> HealthResponse:
        """
        Single-call platform health snapshot for the operator.

        Returns branch, git HEAD, per-account broker status, DB row counts,
        in-process cache size, simulator state, paper-engine state, and the
        list of IPv6 source addresses configured per broker account.
        """
        try:
            broker_statuses, ipv6_list = await _fetch_broker_statuses()
            db_stats = await _fetch_db_stats()
            cache_stats = _cache_stats()
            sim = _sim_status()
            paper = _paper_status()
            git_hash = _git_hash()
            git_subject = _git_subject()
            branch = str(config.get("deploy_branch") or "?")

            return HealthResponse(
                branch=branch,
                git_hash=git_hash,
                git_subject=git_subject,
                broker_accounts=broker_statuses,
                db=db_stats,
                cache=cache_stats,
                sim=sim,
                paper=paper,
                ipv6=ipv6_list,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(f"HealthController.get_health failed: {exc}")
            raise HTTPException(status_code=500, detail="Health check failed")
