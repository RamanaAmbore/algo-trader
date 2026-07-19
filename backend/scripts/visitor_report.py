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
from backend.shared.helpers.date_time_utils import INDIAN_TIMEZONE as _IST

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Nginx log format (after Cloudflare real-IP + custom log_format):
#   $remote_addr $http_cf_ipcountry - [$time_local] "$request"
#   $status $body_bytes_sent "$http_referer" "$http_user_agent"
# ---------------------------------------------------------------------------

_LOG_RE = re.compile(
    r'^(\S+)'           # 1: remote_addr (real visitor IP via CF real-IP)
    r'\s+(\S+)'         # 2: cf_ipcountry (ISO-2 or "-")
    r'\s+\[([^\]]+)\]'  # 3: time_local
    r'\s+"([^"]*)"'     # 4: request line  e.g. "GET /pulse HTTP/1.1"
    r'\s+(\d+)'         # 5: status
    r'\s+\d+'           # body_bytes_sent (ignored)
    r'\s+"[^"]*"'       # referer (ignored)
    r'\s+"([^"]*)"'     # 6: user_agent
)

# Time format inside [] in nginx logs
_TIME_FMT = "%d/%b/%Y:%H:%M:%S %z"

# Static-asset path filter — lines whose path matches are skipped entirely
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

# ── Ignore filters (operator-managed via /admin/settings) ───────────────
# visitors.ignore_ips        — exact IP or IP prefix; matched literally
# visitors.ignore_companies  — substring match (case-insensitive) against
#                              the shortened company name (post _shorten_company)
def _parse_ignore_ips_setting() -> list[str]:
    try:
        from backend.shared.helpers.settings import get_string as _get_string
        raw = _get_string("visitors.ignore_ips", "")
    except Exception:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_ignore_companies_setting() -> list[str]:
    try:
        from backend.shared.helpers.settings import get_string as _get_string
        raw = _get_string("visitors.ignore_companies", "")
    except Exception:
        return []
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _ip_should_ignore(ip: str, patterns: list[str]) -> bool:
    """A pattern matches if it's an exact IP match OR the IP starts with
    the pattern. Operators can list a /64 prefix as '2601:18c:8500:bd70:'
    (trailing colon) or '69.62.78.' (trailing dot) for a /24 IPv4 range."""
    if not ip:
        return False
    for p in patterns:
        if ip == p or ip.startswith(p):
            return True
    return False


def _company_should_ignore(company: str, patterns_lower: list[str]) -> bool:
    if not company or not patterns_lower:
        return False
    name_lower = company.lower()
    return any(p in name_lower for p in patterns_lower)


# Bot-probe / vuln-scan paths — these still appear in the per-IP detail
# table (so the operator can see WHO is probing) but are suppressed from
# the Top Paths summary so they don't drown out real visitor traffic.
# Anchored to obvious patterns we've seen in the wild:
#   - recursive %25 encoded payloads (AhrefsBot SQL injection probes)
#   - WordPress / phpMyAdmin / .env / Drupal / sql-backup probes
#   - common .git / .svn / .DS_Store fishing
_BOT_PROBE_RE = re.compile(
    r'(?:%25){3,}'                        # recursive percent-encoding (3+ rounds)
    r'|\.sql(?:\.gz)?(?:\?|$)'            # /something.sql or .sql.gz
    r'|^/wp-(?:admin|login|content|includes|json)'   # WordPress
    r'|^/phpmyadmin|^/pma/|^/PMA/'        # phpMyAdmin
    r'|^/\.env(?:\.|$)'                   # .env exfiltration
    r'|^/\.git/|^/\.svn/'                 # git/svn dir leaks
    r'|^/wlwmanifest\.xml'                # Windows Live Writer probe
    r'|^/xmlrpc\.php'                     # WordPress xmlrpc probe
    r'|^/CHANGELOG\.(?:txt|md)$'          # Drupal / CMS version probes
    r'|^/(?:admin|administrator|user)\.(?:php|asp|aspx)$'  # CMS admin probes
    r'|/eval-stdin\.php'                  # ThinkPHP RCE probe
    r'|^/HNAP1|^/boaform'                 # IoT router probes
    ,
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
# IP masking — GDPR-style anonymisation for visitor reports
# ---------------------------------------------------------------------------

def _mask_ip(ip: str) -> str:
    """Mask an IP for GDPR-style anonymisation in reports.

    IPv4 `49.207.222.16` → `49.207.x.###` (first two octets preserved,
    third masked with `x`, fourth masked with `###`).
    IPv6 keeps the first three :-separated groups, the rest collapse
    into `####` so /64 subnet identity stays opaque.

    Was previously in `backend/api/routes/visitors.py`; moved here when
    the admin route was removed but the visitor pipeline (script +
    DB + nightly task) was kept. Used by tests + by future report
    surfaces that want anonymous IPs.
    """
    if not ip:
        return ""
    if ":" in ip:
        # IPv6 — keep first three groups, mask the rest with ####.
        parts = ip.split(":")
        if len(parts) <= 3:
            return ip
        return ":".join(parts[:3]) + "::####"
    octets = ip.split(".")
    if len(octets) != 4:
        return ip
    return f"{octets[0]}.{octets[1]}.x.###"


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
        # Filter by IST date — the report cadence is tied to MCX closure
        # (23:35 IST), so each report covers a calendar IST day that
        # straddles two UTC dates. `target` is the IST date the report
        # represents.
        try:
            dt = datetime.strptime(time_str, _TIME_FMT).astimezone(timezone.utc)
        except ValueError:
            continue
        if dt.astimezone(_IST).date() != target:
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
                # `asn` is the short "AS9498" handle; `company` carries the
                # full network owner ("Google LLC", "Amazon.com Inc.", etc.)
                # so the report row identifies the corporate visitor when
                # one is hitting from their office network. WFH visitors
                # show the ISP (Comcast / AT&T / Jio) — there's no way to
                # identify the employer from a residential IP.
                result["asn"] = f"AS{asn_num}"
                if asn_org:
                    result["company"] = _shorten_company(asn_org)
        except Exception:
            pass
    return result


# Corporate-suffix endings that don't add identification value once the
# company name is recognisable. Stripped from the tail; case-insensitive.
_CORP_SUFFIXES = (
    " LLC", " L.L.C.", " Inc.", " Inc", " Corporation", " Corp.", " Corp",
    " Limited", " Ltd.", " Ltd", " Pvt. Ltd.", " Pvt Ltd", " Pte. Ltd.",
    " Pte Ltd", " S.A.", " SA", " SAS", " S.A.S.", " GmbH", " mbH",
    " AG", " KG", " B.V.", " BV", " N.V.", " NV", " AB", " Oy",
    " Co.", " Co", " Company", " International", " Holdings",
)


def _shorten_company(name: str) -> str:
    """Strip corporate suffixes + truncate at the first separator so a
    long ASN org like 'Atria Convergence Technologies Pvt. Ltd. Broadband
    Internet Service Provider INDIA' collapses to 'Atria Convergence' —
    readable on a phone screen, still identifies the parent network.
    """
    if not name:
        return ""
    s = str(name).strip()
    # Truncate at the first hard separator — these usually signal
    # postal-address fragments or marketing taglines appended to the
    # legal name in MaxMind's autonomous_system_organization field.
    for sep in (",", ";", " - ", " — ", " · ", " | "):
        idx = s.find(sep)
        if idx > 0:
            s = s[:idx].strip()
    # Strip recognisable corporate-form suffixes from the end (looped so
    # 'Foo Pvt. Ltd. International Limited' collapses cleanly).
    changed = True
    while changed:
        changed = False
        for suf in _CORP_SUFFIXES:
            if s.lower().endswith(suf.lower()):
                s = s[: -len(suf)].rstrip(" .,")
                changed = True
                break
    # Last-resort length cap so a no-suffix Chinese / Russian / multi-word
    # name doesn't blow the column.
    if len(s) > 28:
        s = s[:27].rstrip() + "…"
    return s or str(name).strip()


# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------

async def _upsert_records(records: dict[str, _IPRecord], target: date, geo_map: dict[str, dict]) -> None:
    """Insert or update visitor_log rows for the given date.

    Uses a single PostgreSQL INSERT ... ON CONFLICT (ip, seen_date) DO UPDATE
    batch instead of N SELECT + conditional UPDATE/INSERT round-trips.
    """
    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from backend.api.database import async_session
    from backend.api.models import VisitorLog

    if not records:
        return

    rows = []
    for ip, rec in records.items():
        geo = geo_map.get(ip, {})
        country = geo.get("country") or (rec.cf_country or None)
        rows.append({
            "ip":            ip,
            "seen_date":     target,
            "country":       country,
            "region":        geo.get("region"),
            "city":          geo.get("city"),
            "asn":           geo.get("asn"),
            "request_count": rec.count,
            "first_seen_at": rec.first_dt,
            "last_seen_at":  rec.last_dt,
            "last_path":     rec.last_path[:200] if rec.last_path else None,
            "user_agent":    rec.user_agent[:400] if rec.user_agent else None,
        })

    stmt = pg_insert(VisitorLog).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["ip", "seen_date"],
        set_={
            "request_count": VisitorLog.request_count + stmt.excluded.request_count,
            "last_seen_at":  func.greatest(VisitorLog.last_seen_at, stmt.excluded.last_seen_at),
            "first_seen_at": func.least(VisitorLog.first_seen_at, stmt.excluded.first_seen_at),
            "last_path":     func.coalesce(VisitorLog.last_path,  stmt.excluded.last_path),
            "user_agent":    func.coalesce(VisitorLog.user_agent, stmt.excluded.user_agent),
            "country":       func.coalesce(VisitorLog.country,    stmt.excluded.country),
            "region":        func.coalesce(VisitorLog.region,     stmt.excluded.region),
            "city":          func.coalesce(VisitorLog.city,       stmt.excluded.city),
            "asn":           func.coalesce(VisitorLog.asn,        stmt.excluded.asn),
        },
    )

    async with async_session() as sess:
        await sess.execute(stmt)
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

def _ts_dual(dt: datetime) -> str:
    """Compact dual-TZ timestamp for table cells — `HH:MM IST · HH:MM EDT`.
    Matches the convention used by the algo page-header nowStamp + alert
    timestamps so operators see times in the two zones they actually work
    in. The full weekday/date is omitted because each row already belongs
    to a specific day (the report header carries the date)."""
    from backend.shared.helpers.date_time_utils import INDIAN_TIMEZONE, EST_ZONE
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ist = dt.astimezone(INDIAN_TIMEZONE)
    est = dt.astimezone(EST_ZONE)
    return f"{ist.strftime('%H:%M')} IST · {est.strftime('%H:%M %Z')}"


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


_RESIDENTIAL_HINTS = (
    "JIO", "AIRTEL", "BSNL", "VODAFONE", "BHARTI",
    "COMCAST", "AT&T", "VERIZON", "SPECTRUM", "CHARTER", "COX", "T-MOBILE",
    "BT GROUP", "VIRGIN", "SKY UK",
    "DEUTSCHE TELEKOM", "VODAFONE GMBH", "TELEFONICA", "ORANGE",
)


def _is_residential(name: str) -> bool:
    """Return True if the company name looks like a residential ISP."""
    up = name.upper()
    return any(h in up for h in _RESIDENTIAL_HINTS)


def _aggregate_counts(
    records: dict[str, "_IPRecord"], geo_map: dict[str, dict],
) -> tuple[dict, dict, dict, dict, list[tuple]]:
    """Aggregate country / city / path / company counts and build detail rows."""
    country_counts: dict[str, int] = defaultdict(int)
    city_counts: dict[str, int] = defaultdict(int)
    path_counts: dict[str, int] = defaultdict(int)
    company_counts: dict[str, int] = defaultdict(int)
    rows: list[tuple] = []
    for ip, rec in records.items():
        geo     = geo_map.get(ip, {})
        country = geo.get("country") or rec.cf_country or "??"
        region  = geo.get("region") or ""
        city    = geo.get("city") or ""
        asn     = geo.get("asn") or ""
        company = geo.get("company") or ""
        country_counts[country] += rec.count
        if city:
            city_counts[city] += rec.count
        if company:
            company_counts[company] += rec.count
        if rec.last_path:
            base = rec.last_path.split("?")[0]
            if not _BOT_PROBE_RE.search(base):
                path_counts[base] += rec.count
        rows.append((
            ip, rec.first_dt, rec.last_dt, rec.count,
            country, region, city, asn, company,
            rec.last_path or "-", _ua_short(rec.user_agent),
        ))
    rows.sort(key=lambda x: x[3], reverse=True)
    return country_counts, city_counts, path_counts, company_counts, rows


def _top_line(counter: dict[str, int], k: int, sep: str = " · ") -> str:
    """Return top-k entries from a count dict as a joined string."""
    return sep.join(
        f"{c} {n}" for c, n in sorted(counter.items(), key=lambda kv: -kv[1])[:k]
    )


def _non_residential_top(company_counts: dict[str, int], k: int = 8) -> str:
    """Return top-k non-residential company entries as a joined string."""
    items = [
        (c, n) for c, n in sorted(company_counts.items(), key=lambda kv: -kv[1])
        if not _is_residential(c)
    ][:k]
    return " · ".join(f"{c} {n}" for c, n in items) if items else "—"


def _detail_row_lines(rows: list[tuple], cap: int = 200) -> list[str]:
    """Render up to `cap` detail rows as markdown table lines."""
    lines = []
    for i, (ip, first_dt, last_dt, count, country, region, city, asn, company, path, ua) in enumerate(rows):
        if i >= cap:
            lines.append(f"| … | | | | | | | | | additional {len(rows) - cap} IPs | |")
            break
        first_s = _ts_dual(first_dt)
        last_s  = _ts_dual(last_dt)
        if not path:
            short_path = "-"
        elif len(path) > 60:
            short_path = f"{path[:30]}…{path[-12:]}"
        else:
            short_path = path
        company_s = (company[:40] + "…") if len(company) > 40 else company
        lines.append(
            f"| {ip} | {first_s} | {last_s} | {count} "
            f"| {country} | {region} | {city} | {asn} | {company_s} "
            f"| {short_path} | {ua} |"
        )
    return lines


def _render_report(
    target: date,
    records: dict[str, "_IPRecord"],
    geo_map: dict[str, dict],
) -> str:
    total_requests = sum(r.count for r in records.values())
    date_str = target.isoformat()

    country_counts, city_counts, path_counts, company_counts, rows = _aggregate_counts(
        records, geo_map,
    )

    top_countries = _top_line(country_counts, 8)
    top_cities    = _top_line(city_counts, 6)
    top_paths     = _top_line(path_counts, 8)
    top_companies = _non_residential_top(company_counts)

    lines = [
        f"# Visitors — {date_str} (IST trading day, post-MCX close)",
        "",
        "## Summary",
        f"- **Unique IPs**: {len(records):,}",
        f"- **Total requests**: {total_requests:,}",
        f"- **Top countries**: {top_countries or '—'}",
        f"- **Top cities**: {top_cities or '—'}",
        f"- **Top companies (corp networks)**: {top_companies or '—'}",
        f"- **Top paths**: {top_paths or '—'}",
        "",
        "## Detail (one row per unique IP)",
        "| IP | First | Last | Reqs | Country | Region | City | ASN | Company | Last path | UA |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ] + _detail_row_lines(rows)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def _arun_load_lines(target_date: date) -> list[str]:
    """Read nginx log files in a thread and return lines for target_date."""
    import asyncio as _asyncio
    log_dir = Path("/var/log/nginx")

    def _read_all() -> list[str]:
        lines: list[str] = []
        for fname in ("ramboq-access.log", "ramboq-access.log.1", "ramboq-access.log.1.gz"):
            lines.extend(_read_log_file(log_dir / fname))
        return lines

    all_lines = await _asyncio.to_thread(_read_all)
    logger.info(f"visitor_report: {len(all_lines)} raw lines for {target_date}")
    records = _parse_lines(all_lines, target_date)
    logger.info(f"visitor_report: {len(records)} unique IPs after path/date filtering")
    return records


def _arun_apply_ip_ignore(records: dict) -> dict:
    """Drop records whose IP matches visitors.ignore_ips patterns."""
    ignore_ips = _parse_ignore_ips_setting()
    if not ignore_ips:
        return records
    before = len(records)
    records = {ip: r for ip, r in records.items() if not _ip_should_ignore(ip, ignore_ips)}
    logger.info(
        f"visitor_report: dropped {before - len(records)} records by "
        f"visitors.ignore_ips ({len(ignore_ips)} patterns)"
    )
    return records


def _arun_geo_map(records: dict) -> dict[str, dict]:
    """Build geo_map via MaxMind lookups (graceful when databases absent)."""
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
    geo_map: dict[str, dict] = {ip: _geo_lookup(ip, city_db, asn_db) for ip in records}
    for db in (city_db, asn_db):
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return geo_map


def _arun_apply_company_ignore(
    records: dict, geo_map: dict[str, dict],
) -> tuple[dict, dict[str, dict]]:
    """Drop visitors whose ASN org matches visitors.ignore_companies patterns."""
    ignore_companies = _parse_ignore_companies_setting()
    if not ignore_companies:
        return records, geo_map
    before = len(records)
    dropped = {
        ip for ip in records
        if _company_should_ignore(geo_map.get(ip, {}).get("company", ""), ignore_companies)
    }
    for ip in dropped:
        records.pop(ip, None)
        geo_map.pop(ip, None)
    logger.info(
        f"visitor_report: dropped {before - len(records)} records by "
        f"visitors.ignore_companies ({len(ignore_companies)} patterns)"
    )
    return records, geo_map


async def _arun_persist(
    records: dict, target_date: date, geo_map: dict[str, dict],
) -> None:
    """Upsert visitor records + purge old rows from DB."""
    try:
        from backend.shared.helpers.settings import get_int as _get_int
        retention_days = _get_int("visitors.retention_days", 30)
    except Exception:
        retention_days = 30
    try:
        await _upsert_records(records, target_date, geo_map)
        today_utc = datetime.now(timezone.utc).date()
        if retention_days > 0:
            deleted = await _purge_old_rows(today_utc, retention_days=retention_days)
            if deleted:
                logger.info(f"visitor_report: purged {deleted} rows older than {retention_days} days")
        else:
            logger.info("visitor_report: retention=0, auto-purge disabled")
    except Exception as e:
        logger.error(f"visitor_report: DB operations failed: {e}")


def _arun_write_report(
    report_md: str, target_date: date, report_dir: str,
) -> Path:
    """Write the markdown report file and return its Path."""
    report_path = Path(report_dir) / f"visitors_{target_date.isoformat()}.md"
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_md, encoding="utf-8")
        logger.info(f"visitor_report: report written → {report_path}")
    except Exception as e:
        logger.error(f"visitor_report: could not write report: {e}")
    return report_path


async def arun_daily(
    target_date: Optional[date] = None,
    report_dir: str = "/opt/ramboq/.log",
) -> Path:
    """Async version of run_daily. Use this from inside async code
    (the background task) so asyncpg's connection pool stays bound
    to the caller's running event loop.

    Parse nginx logs for `target_date` (default: today IST — the
    IST trading day that's just closed at MCX, 23:30 IST). Upserts
    visitor_log, writes markdown report, purges rows older than 30 days.
    Returns the report Path."""
    today_ist = datetime.now(_IST).date()
    if target_date is None:
        target_date = today_ist

    try:
        from backend.shared.helpers.settings import reload_cache
        await reload_cache()
    except Exception as e:
        logger.warning(f"visitor_report: settings cache reload failed: {e}")

    records   = await _arun_load_lines(target_date)
    records   = _arun_apply_ip_ignore(records)
    geo_map   = _arun_geo_map(records)
    records, geo_map = _arun_apply_company_ignore(records, geo_map)
    await _arun_persist(records, target_date, geo_map)
    report_md = _render_report(target_date, records, geo_map)
    return _arun_write_report(report_md, target_date, report_dir)


def run_daily(
    target_date: Optional[date] = None,
    report_dir: str = "/opt/ramboq/.log",
) -> Path:
    """Sync shim around `arun_daily` for CLI use (no event loop
    already running). Background-task callers should `await
    arun_daily(...)` directly instead — that avoids `asyncio.run`
    creating a fresh loop in the worker thread, which is what
    caused the prior 'Future attached to a different loop' errors
    (asyncpg's pool stays bound to the loop that first opened it)."""
    import asyncio as _asyncio
    return _asyncio.run(arun_daily(target_date=target_date, report_dir=report_dir))


def _parse_summary(report_path: Path) -> dict:
    """Parse the report's Summary section into a dict so each delivery
    channel can format it idiomatically.

    Returns: {title: str, fields: [(label, value), …]} where `fields`
    preserves the order written by _render_report.
    """
    out = {"title": "Visitors", "fields": []}
    try:
        lines = report_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return out
    for raw in lines:
        line = raw.strip()
        if line.startswith("## Detail"):
            break
        if line.startswith("# "):
            out["title"] = line.lstrip("# ").strip()
            continue
        # Bullet rows look like '- **Label**: value'. Parse defensively
        # so a future change to the renderer doesn't silently blank the
        # summary channels.
        m = re.match(r"^[-*]\s+\*\*([^*]+)\*\*\s*:\s*(.*)$", line)
        if m:
            out["fields"].append((m.group(1).strip(), m.group(2).strip()))
            continue
    return out


def _summary_text(report_path: Path) -> str:
    """Plain-text summary — used for logging + telegram fallback when the
    HTML build fails. One line per field, no markdown."""
    data = _parse_summary(report_path)
    lines = [data["title"]] if data["title"] else []
    for label, value in data["fields"]:
        lines.append(f"{label}: {value}")
    return "\n".join(lines) if lines else f"Visitor report ready: {report_path}"


def summary_for_telegram(report_path: Path) -> str:
    """Telegram HTML — bold labels, plain values, one bullet per line.
    Telegram's HTML parser accepts <b>, <i>, <u>, <s>, <code>, <pre>.
    No markdown markers — those render as literal `#` and `**` text on
    mobile clients, which was the original noise we got rid of."""
    data = _parse_summary(report_path)
    title = data["title"] or "Visitors"
    lines = [f"<b>{_html_escape(title)}</b>"]
    if data["fields"]:
        lines.append("")
        for label, value in data["fields"]:
            lines.append(f"• <b>{_html_escape(label)}</b>: {_html_escape(value)}")
    return "\n".join(lines)


def _html_escape(s: str) -> str:
    if not s:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# Backwards-compat alias — kept so the bg task survives a partial deploy
# where the route has rolled out but the script hasn't.
def _summary_block(report_path: Path) -> str:
    return summary_for_telegram(report_path)
