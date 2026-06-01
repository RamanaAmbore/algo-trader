"""
backend/scripts/visitor_report.py

Parse nginx access.log (and its .1.gz rotation) for a given UTC date,
upsert into visitor_log (one row per unique ip+date), write a markdown
report, and purge rows older than 30 days.

Entry point: run_daily(target_date, report_dir)

Called nightly by _task_visitor_log_daily in background.py.
"""

from __future__ import annotations

import asyncio
import gzip
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Nginx log format (after Cloudflare real-IP + custom log_format):
#   $remote_addr $http_cf_ipcountry - [$time_local] "$request"
#   $status $body_bytes_sent "$http_referer" "$http_user_agent"
# ---------------------------------------------------------------------------

_LOG_RE = re.compile(
    r'^(\S+)'           # 1: remote_addr
    r'\s+(\S+)'         # 2: cf_ipcountry (ISO-2 or "-")
    r'\s+-'             # literal "-"
    r'\s+\[([^\]]+)\]'  # 3: time_local
    r'\s+"([^"]*)"'     # 4: request line  e.g. "GET /pulse HTTP/1.1"
    r'\s+(\d+)'         # 5: status
    r'\s+\d+'           # body_bytes_sent (ignored)
    r'\s+"[^"]*"'       # referer (ignored)
    r'\s+"([^"]*)"'     # 6: user_agent
)

# Time format inside [] in nginx logs
_TIME_FMT = "%d/%b/%Y:%H:%M:%S %z"

# Static-asset path filter — lines whose path matches are skipped
_STATIC_RE = re.compile(
    r'\.(?:js|mjs|css|woff2?|ttf|eot|svg|png|jpg|jpeg|gif|ico|webp|map)(?:\?.*)?$'
    r'|^/\.well-known/'
    r'|^/cdn-cgi/'
    r'|^/_app/'
    r'|^/favicon'
    r'|^/assets-root/'
    r'|^/sitemap'
    r'|^/robots\.txt',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Per-IP accumulator
# ---------------------------------------------------------------------------

class _IPRecord:
    __slots__ = (
        "ip", "cf_country", "first_dt", "last_dt",
        "count", "last_path", "user_agent",
    )

    def __init__(self, ip: str, cf_country: str, dt: datetime, path: str, ua: str):
        self.ip = ip
        self.cf_country = cf_country
        self.first_dt = dt
        self.last_dt = dt
        self.count = 1
        self.last_path = path
        self.user_agent = ua

    def update(self, dt: datetime, path: str, ua: str) -> None:
        if dt < self.first_dt:
            self.first_dt = dt
        if dt > self.last_dt:
            self.last_dt = dt
            self.last_path = path
            self.user_agent = ua
        self.count += 1


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

def _extract_path(request_line: str) -> str:
    """'GET /pulse?foo=bar HTTP/1.1'  →  '/pulse?foo=bar'"""
    parts = request_line.split(" ", 2)
    return parts[1] if len(parts) >= 2 else request_line


def _parse_lines(lines: list[str], target: date) -> dict[str, _IPRecord]:
    records: dict[str, _IPRecord] = {}
    for raw in lines:
        line = raw.rstrip("\n")
        m = _LOG_RE.match(line)
        if not m:
            continue
        ip, cf_country, time_str, request_line, _status, ua = (
            m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6),
        )
        # Filter by date
        try:
            dt = datetime.strptime(time_str, _TIME_FMT).astimezone(timezone.utc)
        except ValueError:
            continue
        if dt.date() != target:
            continue
        # Filter static assets / infra paths
        path = _extract_path(request_line)
        if _STATIC_RE.search(path):
            continue
        cf_country = cf_country if (cf_country and cf_country != "-") else None
        if ip in records:
            records[ip].update(dt, path, ua)
        else:
            records[ip] = _IPRecord(ip, cf_country or "", dt, path, ua)
    return records


def _read_log_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                return fh.readlines()
        else:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                return fh.readlines()
    except Exception as e:
        logger.warning(f"visitor_report: could not read {path}: {e}")
        return []


# ---------------------------------------------------------------------------
# MaxMind GeoIP lookup
# ---------------------------------------------------------------------------

def _geo_lookup(ip: str, city_db, asn_db) -> dict:
    """Return {country, region, city, asn} from MaxMind DBs.
    Any key is None when unavailable."""
    result = {"country": None, "region": None, "city": None, "asn": None}
    if city_db is not None:
        try:
            record = city_db.get(ip) or {}
            country_obj = record.get("country") or record.get("registered_country") or {}
            result["country"] = country_obj.get("iso_code")
            subdivisions = record.get("subdivisions") or []
            if subdivisions:
                result["region"] = subdivisions[0].get("iso_code")
            city_obj = record.get("city") or {}
            names = city_obj.get("names") or {}
            result["city"] = names.get("en")
        except Exception:
            pass
    if asn_db is not None:
        try:
            asn_record = asn_db.get(ip) or {}
            asn_num = asn_record.get("autonomous_system_number")
            asn_org = asn_record.get("autonomous_system_organization") or ""
            if asn_num:
                # Truncate org to fit the column ("AS9498-AIRTEL" style)
                short_org = asn_org[:18].replace(" ", "-").upper() if asn_org else ""
                result["asn"] = f"AS{asn_num}-{short_org}" if short_org else f"AS{asn_num}"
                # Cap at 32 chars (column width)
                result["asn"] = result["asn"][:32]
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------

async def _upsert_records(records: dict[str, _IPRecord], target: date, geo_map: dict[str, dict]) -> None:
    """Insert or update visitor_log rows for the given date."""
    from sqlalchemy import select as sa_select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from backend.api.database import async_session
    from backend.api.models import VisitorLog

    if not records:
        return

    async with async_session() as sess:
        for ip, rec in records.items():
            geo = geo_map.get(ip, {})
            country = geo.get("country") or (rec.cf_country or None)
            region  = geo.get("region")
            city    = geo.get("city")
            asn     = geo.get("asn")
            # Attempt update first (existing row for this ip+date)
            result = await sess.execute(
                sa_select(VisitorLog).where(
                    VisitorLog.ip == ip,
                    VisitorLog.seen_date == target,
                )
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                existing.request_count += rec.count
                if rec.last_dt > existing.last_seen_at:
                    existing.last_seen_at = rec.last_dt
                    existing.last_path = rec.last_path[:200] if rec.last_path else None
                    existing.user_agent = rec.user_agent[:400] if rec.user_agent else None
                if rec.first_dt < existing.first_seen_at:
                    existing.first_seen_at = rec.first_dt
                # Update geo if we got data and didn't have it
                if country and not existing.country:
                    existing.country = country
                if region and not existing.region:
                    existing.region = region
                if city and not existing.city:
                    existing.city = city
                if asn and not existing.asn:
                    existing.asn = asn
            else:
                sess.add(VisitorLog(
                    ip=ip,
                    seen_date=target,
                    country=country,
                    region=region,
                    city=city,
                    asn=asn,
                    request_count=rec.count,
                    first_seen_at=rec.first_dt,
                    last_seen_at=rec.last_dt,
                    last_path=rec.last_path[:200] if rec.last_path else None,
                    user_agent=rec.user_agent[:400] if rec.user_agent else None,
                ))
        await sess.commit()


async def _purge_old_rows(today_utc: date, retention_days: int = 30) -> int:
    """DELETE visitor_log rows older than retention_days. Returns deleted count."""
    from sqlalchemy import delete as sa_delete
    from backend.api.database import async_session
    from backend.api.models import VisitorLog

    cutoff = today_utc - timedelta(days=retention_days)
    try:
        async with async_session() as sess:
            res = await sess.execute(
                sa_delete(VisitorLog).where(VisitorLog.seen_date < cutoff)
            )
            await sess.commit()
            return res.rowcount or 0
    except Exception as e:
        logger.error(f"visitor_report: purge failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# Markdown report renderer
# ---------------------------------------------------------------------------

def _ua_short(ua: str | None) -> str:
    """Extract browser name+major from UA string. Falls back gracefully."""
    if not ua:
        return "-"
    for pat, label in (
        (r"Edg/(\d+)",        "Edge {}"),
        (r"Chrome/(\d+)",     "Chrome {}"),
        (r"Firefox/(\d+)",    "Firefox {}"),
        (r"Safari/(\d+)",     "Safari"),
        (r"curl/",            "curl"),
        (r"python-requests",  "requests"),
        (r"Wget/",            "wget"),
        (r"Go-http-client",   "Go"),
        (r"okhttp/",          "okhttp"),
        (r"Dart/",            "Dart"),
    ):
        m = re.search(pat, ua)
        if m:
            try:
                return label.format(m.group(1))
            except IndexError:
                return label
    # Fallback: first 20 chars
    return ua[:20]


def _render_report(
    target: date,
    records: dict[str, _IPRecord],
    geo_map: dict[str, dict],
) -> str:
    total_requests = sum(r.count for r in records.values())
    date_str = target.isoformat()

    # Country counts
    country_counts: dict[str, int] = defaultdict(int)
    city_counts: dict[str, int] = defaultdict(int)
    path_counts: dict[str, int] = defaultdict(int)

    rows: list[tuple] = []
    for ip, rec in records.items():
        geo = geo_map.get(ip, {})
        country = geo.get("country") or rec.cf_country or "??"
        region  = geo.get("region") or ""
        city    = geo.get("city") or ""
        asn     = geo.get("asn") or ""
        country_counts[country] += rec.count
        if city:
            city_counts[city] += rec.count
        if rec.last_path:
            base = rec.last_path.split("?")[0]
            path_counts[base] += rec.count
        rows.append((
            ip, rec.first_dt, rec.last_dt, rec.count,
            country, region, city, asn,
            rec.last_path or "-", _ua_short(rec.user_agent),
        ))

    rows.sort(key=lambda x: x[3], reverse=True)

    top_countries = " · ".join(
        f"{c} {n}"
        for c, n in sorted(country_counts.items(), key=lambda kv: -kv[1])[:8]
    )
    top_cities = " · ".join(
        f"{c} {n}"
        for c, n in sorted(city_counts.items(), key=lambda kv: -kv[1])[:6]
    )
    top_paths = " · ".join(
        f"{p} {n}"
        for p, n in sorted(path_counts.items(), key=lambda kv: -kv[1])[:8]
    )

    lines = [
        f"# Visitors — {date_str} UTC",
        "",
        "## Summary",
        f"- **Unique IPs**: {len(records):,}",
        f"- **Total requests**: {total_requests:,}",
        f"- **Top countries**: {top_countries or '—'}",
        f"- **Top cities**: {top_cities or '—'}",
        f"- **Top paths**: {top_paths or '—'}",
        "",
        "## Detail (one row per unique IP)",
        "| IP | First | Last | Reqs | Country | Region | City | ASN | Last path | UA |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    cap = 200
    for i, (ip, first_dt, last_dt, count, country, region, city, asn, path, ua) in enumerate(rows):
        if i >= cap:
            remaining = len(rows) - cap
            lines.append(f"| … | | | | | | | | additional {remaining} IPs | |")
            break
        first_s = first_dt.strftime("%H:%M")
        last_s  = last_dt.strftime("%H:%M")
        # Truncate path for table readability
        short_path = path[:40] if path else "-"
        lines.append(
            f"| {ip} | {first_s} | {last_s} | {count} "
            f"| {country} | {region} | {city} | {asn} "
            f"| {short_path} | {ua} |"
        )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_daily(
    target_date: Optional[date] = None,
    report_dir: str = "/opt/ramboq/.log",
) -> Path:
    """Parse nginx logs for `target_date` (default: yesterday UTC),
    upsert visitor_log, write markdown report, purge old rows.
    Returns the report Path."""
    import asyncio as _asyncio

    today_utc = datetime.now(timezone.utc).date()
    if target_date is None:
        target_date = today_utc - timedelta(days=1)

    # ramboq.com is configured (via /etc/nginx/conf.d/00-cloudflare-real-ip.conf
    # + per-server access_log overrides) to write to a dedicated log file in
    # the ramboq_visitor format. Reading this file directly avoids having to
    # disambiguate the prod box's other sites (marathakalyanam, ramanaambore,
    # webhook etc) from the visitor-traffic stream.
    log_dir = Path("/var/log/nginx")
    all_lines: list[str] = []
    for fname in ("ramboq-access.log", "ramboq-access.log.1", "ramboq-access.log.1.gz"):
        all_lines.extend(_read_log_file(log_dir / fname))

    logger.info(
        f"visitor_report: {len(all_lines)} raw lines for {target_date}"
    )

    records = _parse_lines(all_lines, target_date)
    logger.info(
        f"visitor_report: {len(records)} unique IPs after filtering"
    )

    # MaxMind GeoIP
    city_db = None
    asn_db  = None
    try:
        import maxminddb
        city_path = Path("/usr/share/GeoIP/GeoLite2-City.mmdb")
        asn_path  = Path("/usr/share/GeoIP/GeoLite2-ASN.mmdb")
        if city_path.exists():
            city_db = maxminddb.open_database(str(city_path))
        else:
            logger.warning("visitor_report: GeoLite2-City.mmdb not found — country from CF header only")
        if asn_path.exists():
            asn_db = maxminddb.open_database(str(asn_path))
        else:
            logger.warning("visitor_report: GeoLite2-ASN.mmdb not found — ASN lookup skipped")
    except ImportError:
        logger.warning("visitor_report: maxminddb not installed — geo lookup skipped")
    except Exception as e:
        logger.warning(f"visitor_report: MaxMind open failed: {e}")

    geo_map: dict[str, dict] = {}
    for ip in records:
        geo_map[ip] = _geo_lookup(ip, city_db, asn_db)

    if city_db is not None:
        try:
            city_db.close()
        except Exception:
            pass
    if asn_db is not None:
        try:
            asn_db.close()
        except Exception:
            pass

    # Upsert into DB
    try:
        loop = _asyncio.get_event_loop()
    except RuntimeError:
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(_upsert_records(records, target_date, geo_map))
        deleted = loop.run_until_complete(_purge_old_rows(today_utc, retention_days=30))
        if deleted:
            logger.info(f"visitor_report: purged {deleted} rows older than 30 days")
    except Exception as e:
        logger.error(f"visitor_report: DB operations failed: {e}")

    # Write markdown report
    report_md = _render_report(target_date, records, geo_map)
    report_path = Path(report_dir) / f"visitors_{target_date.isoformat()}.md"
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_md, encoding="utf-8")
        logger.info(f"visitor_report: report written → {report_path}")
    except Exception as e:
        logger.error(f"visitor_report: could not write report: {e}")

    return report_path


def _summary_block(report_path: Path) -> str:
    """Extract the Summary section (lines 1-8) from the report file."""
    try:
        lines = report_path.read_text(encoding="utf-8").splitlines()
        # Return title + summary block (up to "## Detail")
        out = []
        for line in lines:
            if line.startswith("## Detail"):
                break
            out.append(line)
        return "\n".join(out)
    except Exception:
        return f"Visitor report ready: {report_path}"
