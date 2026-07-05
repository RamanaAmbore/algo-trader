"""
Unified log feed.

GET /api/logs/unified?limit=50&kinds=…&accounts=…&since=ISO

Merges algo_order_events (order lifecycle) and agent_events (rule fires)
into one flat list sorted newest-first. Frontend surfaces use this single
endpoint instead of fetching the two streams separately and merging
client-side.

Kept separate from orders.py to avoid the controller growing further.
"""

import re
from datetime import datetime, timezone

import msgspec
from litestar import Controller, Request, get
from litestar.exceptions import HTTPException

from backend.api.auth_guard import auth_or_demo_guard, is_admin_request
from backend.api.database import async_session
from backend.api.models import Agent, AgentEvent, AlgoOrder, AlgoOrderEvent
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class UnifiedLogRow(msgspec.Struct):
    """One row in the merged log feed.

    source ∈ 'order' | 'agent'
    kind   ∈ placed / chase_modify / fill / unfill / reject /
              preflight_ok / preflight_block / cancel / postback   (order)
            ∈ agent_fire / agent_match / agent_action_success /
              agent_action_error / agent_skipped / agent_paused     (agent)
    sim_mode — True when the row came from a simulator run; False
               for real (live + paper). Surfaced so the UI can tag
               a row with a [SIM] chip or filter it out entirely.
    """
    id: int
    source: str           # 'order' | 'agent'
    ts: str               # ISO-8601 UTC
    kind: str
    message: str
    order_id: int | None
    agent_slug: str | None
    account: str | None
    payload_json: str | None
    sim_mode: bool


_ACCOUNT_RE = re.compile(r'\b([A-Z]{2})\d{4,8}\b')


def _mask_account(val: str | None) -> str | None:
    if val is None:
        return None
    return _ACCOUNT_RE.sub(r'\1####', val)


def _mask_payload(raw: str | None) -> str | None:
    if raw is None:
        return None
    return _ACCOUNT_RE.sub(r'\1####', raw)


# ---------------------------------------------------------------------------
# Query-param parsing helpers
# ---------------------------------------------------------------------------

def _parse_csv_set(raw: str) -> set[str]:
    """Split a comma-separated query param into a stripped, non-empty set."""
    if not raw:
        return set()
    return {tok.strip() for tok in raw.split(",") if tok.strip()}


def _parse_sim_filter(raw: str) -> bool | None:
    """Return True (sim only), False (real only), or None (no filter).

    '' = no filter (both real + sim), 'true' = sim only, 'false' = real
    only. Used by the dashboard agent-activity panel to suppress
    fabricated fires during a sim run.
    """
    val = raw.lower()
    if val == "true":
        return True
    if val == "false":
        return False
    return None


def _parse_since(raw: str) -> datetime | None:
    """Parse an ISO-8601 `since=` param. Empty → None. Raises HTTP 400 on bad format."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid since= value: {raw!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _identity(x):
    return x


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------

async def _fetch_order_events(
    session, *, limit: int, kind_set: set[str], since_dt: datetime | None,
):
    from sqlalchemy import desc, select
    q = (
        select(AlgoOrderEvent, AlgoOrder.account)
        .join(AlgoOrder, AlgoOrderEvent.order_id == AlgoOrder.id)
        .order_by(desc(AlgoOrderEvent.ts))
        .limit(limit * 2)
    )
    if since_dt:
        q = q.where(AlgoOrderEvent.ts > since_dt)
    if kind_set:
        q = q.where(AlgoOrderEvent.kind.in_(kind_set))
    return (await session.execute(q)).all()


async def _fetch_agent_events(
    session, *, limit: int, kind_set: set[str], since_dt: datetime | None,
    sim_filter: bool | None,
):
    from sqlalchemy import desc, select
    q = (
        select(AgentEvent, Agent.slug)
        .join(Agent, AgentEvent.agent_id == Agent.id)
        .order_by(desc(AgentEvent.timestamp))
        .limit(limit * 2)
    )
    if since_dt:
        q = q.where(AgentEvent.timestamp > since_dt)
    if kind_set:
        # Map agent event_type to unified kind — same names used here
        q = q.where(AgentEvent.event_type.in_(kind_set))
    if sim_filter is not None:
        q = q.where(AgentEvent.sim_mode.is_(sim_filter))
    return (await session.execute(q)).all()


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def _order_matches_account(account: str | None, payload_raw: str | None,
                           acct_set: set[str]) -> bool:
    """Return True when this order row survives the account filter."""
    if not acct_set:
        return True
    raw = account or ""
    payload = payload_raw or ""
    return any(a in raw or a in payload for a in acct_set)


def _build_order_row(oe, account, *, mask, mask_p) -> UnifiedLogRow:
    return UnifiedLogRow(
        id=oe.id,
        source="order",
        ts=oe.ts.isoformat() if oe.ts else "",
        kind=oe.kind,
        message=oe.message or "",
        order_id=oe.order_id,
        agent_slug=None,
        account=mask(account),
        payload_json=mask_p(oe.payload_json),
        sim_mode=False,   # order events are real-broker only
    )


def _extract_agent_account(ae) -> str | None:
    """Pull the account id (if any) out of an agent event's detail /
    trigger_condition text.  Returns None when no account token found."""
    detail_str = ae.detail or ae.trigger_condition or ""
    m = re.search(r'\b([A-Z]{2}\d{4,8})\b', detail_str)
    return m.group(1) if m else None


def _build_agent_row(ae, slug, *, mask) -> UnifiedLogRow:
    kind = ae.event_type or "agent_fire"
    acct_raw = _extract_agent_account(ae)
    return UnifiedLogRow(
        id=ae.id,
        source="agent",
        ts=ae.timestamp.isoformat() if ae.timestamp else "",
        kind=kind,
        message=ae.detail or ae.trigger_condition or "",
        order_id=None,
        agent_slug=slug,
        account=mask(acct_raw) if acct_raw else None,
        payload_json=None,
        sim_mode=bool(ae.sim_mode),
    )


def _agent_row_survives_filters(ae, *, kind_set: set[str], acct_set: set[str]) -> bool:
    """Post-query filters that couldn't be pushed into SQL cleanly.

    Kind check is redundant with the SQL WHERE but kept as a safety net
    when SQL binds don't map perfectly to the unified 'agent_fire' default.
    Account check is Python-only because the account token lives inside
    a free-text `detail` column (extracted via regex).
    """
    kind = ae.event_type or "agent_fire"
    if kind_set and kind not in kind_set:
        return False
    if acct_set:
        acct_raw = _extract_agent_account(ae)
        if acct_raw and acct_raw not in acct_set:
            return False
    return True


class LogsController(Controller):
    path = "/api/logs"
    guards = [auth_or_demo_guard]

    @get("/unified")
    async def unified_log(
        self,
        request: Request,
        limit: int = 50,
        kinds: str = "",
        accounts: str = "",
        since: str = "",
        sim_mode: str = "",
    ) -> list[UnifiedLogRow]:
        """Merged order-event + agent-event stream, newest-first.

        Query params:
          limit    — max rows returned (cap 200, default 50)
          kinds    — comma-separated kind filter; empty = all
          accounts — comma-separated account ids; matches row.account or
                     payload_json content; empty = all
          since    — ISO timestamp; only rows newer than this
          sim_mode — '' (default) returns both; 'true' returns sim-only;
                     'false' returns real-only (excludes sim).
        """
        limit = max(1, min(limit, 200))
        kind_set = _parse_csv_set(kinds)
        acct_set = _parse_csv_set(accounts)
        sim_filter = _parse_sim_filter(sim_mode)
        since_dt = _parse_since(since)

        do_mask = not is_admin_request(request)
        mask = _mask_account if do_mask else _identity
        mask_p = _mask_payload if do_mask else _identity

        async with async_session() as s:
            oe_rows = await _fetch_order_events(
                s, limit=limit, kind_set=kind_set, since_dt=since_dt,
            )
            ae_rows = await _fetch_agent_events(
                s, limit=limit, kind_set=kind_set, since_dt=since_dt,
                sim_filter=sim_filter,
            )

        rows: list[UnifiedLogRow] = [
            _build_order_row(oe, account, mask=mask, mask_p=mask_p)
            for oe, account in oe_rows
            if _order_matches_account(account, oe.payload_json, acct_set)
        ]
        rows.extend(
            _build_agent_row(ae, slug, mask=mask)
            for ae, slug in ae_rows
            if _agent_row_survives_filters(ae, kind_set=kind_set, acct_set=acct_set)
        )

        # Sort merged list newest-first, cap to limit
        rows.sort(key=lambda r: r.ts, reverse=True)
        return rows[:limit]
