"""
GET /api/admin/visitors?days=7

Returns visitor_log rows for the last N days (cap 30), aggregated per IP
across the date range when the IP appears on multiple days.

Role gating:
  admin / designated  → full fields (real IP, city, ASN)
  partner             → IP masked to "103.34.x.###", region/city/ASN nulled,
                        country preserved
  demo / anonymous    → 403
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import msgspec
from litestar import Controller, Request, get
from litestar.exceptions import HTTPException
from sqlalchemy import select, func

from backend.api.auth_guard import admin_guard, jwt_guard
from backend.api.database import async_session
from backend.api.models import VisitorLog
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class VisitorRow(msgspec.Struct):
    ip:            str
    country:       Optional[str]
    region:        Optional[str]
    city:          Optional[str]
    asn:           Optional[str]
    request_count: int
    first_seen_at: str      # ISO-8601 UTC
    last_seen_at:  str      # ISO-8601 UTC
    last_path:     Optional[str]
    user_agent:    Optional[str]
    days_active:   int      # number of distinct dates in the range


class VisitorResponse(msgspec.Struct):
    days:        int
    from_date:   str        # ISO date
    to_date:     str        # ISO date
    unique_ips:  int
    total_requests: int
    rows:        list[VisitorRow]


# ---------------------------------------------------------------------------
# IP masking helper (partner tier)
# ---------------------------------------------------------------------------

def _mask_ip(ip: str) -> str:
    """Mask last two octets/groups: 49.207.222.16 → 49.207.x.###"""
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.x.###"
    # IPv6 — mask last 4 groups
    groups = ip.split(":")
    masked = groups[:4] + ["####"] * max(0, len(groups) - 4)
    return ":".join(masked[:8])


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class VisitorLogController(Controller):
    path = "/api/admin/visitors"
    # jwt_guard in each handler rather than a single guards= so we can
    # read the role and apply masking for partner tier.

    @get("/", guards=[jwt_guard])
    async def list_visitors(
        self,
        request: Request,
        days: int = 7,
    ) -> VisitorResponse:
        """Return visitor_log rows for the last `days` days (max 30).

        Role behaviour:
          admin / designated → full IP + geo
          partner            → masked IP, country only
          demo / anonymous   → 403
        """
        payload = getattr(request.state, "token_payload", {})
        role = payload.get("role", "")

        # Demo / anonymous → reject
        if role not in ("admin", "designated", "partner"):
            raise HTTPException(status_code=403, detail="Demo: visitor logs hidden.")

        days = max(1, min(days, 30))
        today_utc = datetime.now(timezone.utc).date()
        from_date = today_utc - timedelta(days=days - 1)

        try:
            async with async_session() as sess:
                result = await sess.execute(
                    select(
                        VisitorLog.ip,
                        VisitorLog.country,
                        VisitorLog.region,
                        VisitorLog.city,
                        VisitorLog.asn,
                        func.sum(VisitorLog.request_count).label("request_count"),
                        func.min(VisitorLog.first_seen_at).label("first_seen_at"),
                        func.max(VisitorLog.last_seen_at).label("last_seen_at"),
                        # last_path and user_agent from the most-recent row:
                        # PostgreSQL doesn't have "LAST" aggregate; use a subquery
                        # approach — we'll pick from the aggregated rows after
                        # fetching the raw per-day rows and grouping in Python.
                        func.count(VisitorLog.seen_date.distinct()).label("days_active"),
                    )
                    .where(VisitorLog.seen_date >= from_date)
                    .group_by(
                        VisitorLog.ip,
                        VisitorLog.country,
                        VisitorLog.region,
                        VisitorLog.city,
                        VisitorLog.asn,
                    )
                    .order_by(func.sum(VisitorLog.request_count).desc())
                )
                agg_rows = result.all()

                # Fetch last_path + user_agent per IP from the most recent row
                result2 = await sess.execute(
                    select(
                        VisitorLog.ip,
                        VisitorLog.last_path,
                        VisitorLog.user_agent,
                        VisitorLog.last_seen_at,
                    )
                    .where(VisitorLog.seen_date >= from_date)
                    .order_by(VisitorLog.ip, VisitorLog.last_seen_at.desc())
                )
                # Build ip → (last_path, user_agent) from the most-recent row per IP
                latest: dict[str, tuple[Optional[str], Optional[str]]] = {}
                for r in result2.all():
                    if r.ip not in latest:
                        latest[r.ip] = (r.last_path, r.user_agent)

        except Exception as e:
            logger.error(f"visitors: DB query failed: {e}")
            raise HTTPException(status_code=500, detail="Visitor log unavailable.")

        partner_mode = (role == "partner")

        rows: list[VisitorRow] = []
        for row in agg_rows:
            lp, ua = latest.get(row.ip, (None, None))
            ip_display  = _mask_ip(row.ip) if partner_mode else row.ip
            region_disp = None if partner_mode else row.region
            city_disp   = None if partner_mode else row.city
            asn_disp    = None if partner_mode else row.asn
            lp_disp     = None if partner_mode else lp
            ua_disp     = None if partner_mode else ua

            rows.append(VisitorRow(
                ip=ip_display,
                country=row.country,
                region=region_disp,
                city=city_disp,
                asn=asn_disp,
                request_count=int(row.request_count or 0),
                first_seen_at=row.first_seen_at.isoformat() if row.first_seen_at else "",
                last_seen_at=row.last_seen_at.isoformat() if row.last_seen_at else "",
                last_path=lp_disp,
                user_agent=ua_disp,
                days_active=int(row.days_active or 1),
            ))

        total_reqs = sum(r.request_count for r in rows)
        return VisitorResponse(
            days=days,
            from_date=from_date.isoformat(),
            to_date=today_utc.isoformat(),
            unique_ips=len(rows),
            total_requests=total_reqs,
            rows=rows,
        )
