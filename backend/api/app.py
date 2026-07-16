"""
Litestar API application.

Single process serves both the REST API and the SvelteKit SPA.
All /api/* and /ws/* routes are handled by Litestar; everything else falls
through to index.html (SPA fallback) so SvelteKit client-side routing works.

Background refresh (holdings/positions/funds, market warm, alerts, open/close
summaries) runs as asyncio tasks within this same process — no Redis, no ARQ.
"""

import mimetypes
from pathlib import Path

from litestar import Litestar, get

# Litestar's `File` response defaults to application/octet-stream when no
# media_type is supplied, which causes PWA installers / favicon handlers to
# silently reject the icon files. Register the W3C MIME for .webmanifest
# (Python stdlib's mimetypes doesn't ship it) and rely on guess_type() for
# the rest (PNG, SVG, ICO, etc. all come back correctly).
mimetypes.add_type("application/manifest+json", ".webmanifest")
from litestar.config.cors import CORSConfig
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin
from litestar.response import File
from litestar.static_files import create_static_files_router

from backend.api.background import on_startup as bg_startup, on_shutdown as bg_shutdown
from backend.api.database import init_db
from backend.api.routes.admin import AdminController
from backend.api.routes.agents import AgentController
from backend.api.routes.audit import AuditController
from backend.api.routes.strategies import StrategiesController
from backend.api.routes.nav import NavController
from backend.api.routes.investor import (
    InvestorAdminController, InvestorPortalController,
    InvestorStatementsController,
)
from backend.api.routes.history import HistoryController
from backend.api.routes.algo import algo_ws_handler
from backend.api.routes.auth import AuthController
from backend.api.routes.config import ConfigController
from backend.api.routes.contact import ContactController
from backend.api.routes.funds import FundsController
from backend.api.routes.holdings import HoldingsController
from backend.api.routes.market import MarketController
from backend.api.routes.news import NewsController
from backend.api.routes.grammar import GrammarTokenController
from backend.api.routes.agent_templates import AgentTemplateController
from backend.api.routes.templates import OrderTemplateController
from backend.api.routes.instruments import InstrumentsController
from backend.api.routes.orders import AccountsController, OrdersController
from backend.api.routes.quote import QuoteController, SparklineController
from backend.api.routes.positions import PositionsController
from backend.api.routes.settings import SettingsController
from backend.api.routes.brokers import BrokersController
from backend.api.routes.hedge_proxies import HedgeProxiesController, seed_hedge_proxies
from backend.api.routes.research import ResearchController
from backend.api.routes.economic import EconomicController
from backend.api.routes.charts import ChartsController
from backend.api.routes.options import OptionsController
from backend.api.routes.alerts import AlertsController
from backend.api.routes.health import HealthController, PersistenceAdminController, BrokerHealthController, BrokerConnectionEventsController
from backend.api.routes.pnl import PnLController
from backend.api.routes.simulator import SimulatorController
from backend.api.routes.replay import ReplayController
from backend.api.routes.live import LiveController
from backend.api.routes.execution import ExecutionController
from backend.api.routes.logs import LogsController
from backend.api.routes.metrics import MetricsController
from backend.api.routes.perf import PerfController
from backend.api.routes.watchlist import WatchlistController
from backend.api.routes.symbols import SymbolsController
from backend.api.routes.ws import performance_ws_handler
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Path to SvelteKit build output (repo root → frontend/build)
_FRONTEND_BUILD = Path(__file__).parent.parent.parent / "frontend" / "build"

cors_config = CORSConfig(
    allow_origins=[
        "http://localhost:5173",   # SvelteKit dev server
        "https://ramboq.com",
        "https://dev.ramboq.com",
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

openapi_config = OpenAPIConfig(
    title="RamboQuant API",
    version="3.0.0",
    description="Portfolio data API — holdings, positions, funds, orders, market, live WS push",
    render_plugins=[ScalarRenderPlugin()],
)

# ---------------------------------------------------------------------------
# SvelteKit static serving (production only — dev uses Vite)
# ---------------------------------------------------------------------------

_static_router      = None
_assets_router      = None
_spa_fallback       = None
_spa_root           = None

if _FRONTEND_BUILD.exists():
    # SvelteKit's adapter-static writes TWO root-level HTMLs:
    #   - index.html  — the PRERENDERED homepage. Contains the full
    #     <svelte:head> block from (public)/+page.svelte, including
    #     og:image / og:title / twitter:image meta tags that WhatsApp,
    #     Twitter, LinkedIn, Slack unfurlers scrape on link share.
    #   - _spa.html   — the bare SPA shell. No per-page meta tags —
    #     used only for client-side-only routes (algo console, admin
    #     pages, anything that hydrates after JS runs).
    # Serve index.html at "/" so OG unfurlers see the right tags;
    # fall back to _spa.html for hydration-only routes.
    _index_html      = _FRONTEND_BUILD / "_spa.html"
    _home_html       = _FRONTEND_BUILD / "index.html"
    _root_html       = _home_html if _home_html.is_file() else _index_html

    _static_router = create_static_files_router(
        path="/_app",
        directories=[_FRONTEND_BUILD / "_app"],
        name="frontend_assets",
        html_mode=False,
    )
    _assets_router = create_static_files_router(
        path="/assets-root",
        directories=[_FRONTEND_BUILD],
        name="frontend_root_assets",
        html_mode=False,
    )

    def _serve(file_path: Path, *, filename: str | None = None) -> File:
        """Build a Litestar File response with the correct media_type so PWA
        manifest parsers, favicon handlers, and OG-image unfurlers accept the
        bytes. Without media_type Litestar defaults to application/octet-stream
        and clients silently reject the asset."""
        media_type, _ = mimetypes.guess_type(file_path.name)
        return File(
            path=file_path,
            filename=filename,
            content_disposition_type="inline",
            media_type=media_type or "application/octet-stream",
        )

    @get("/{path:path}", include_in_schema=False)
    async def _spa_fallback(path: str) -> File:  # noqa: F811
        # Guard against directory traversal
        if ".." in path:
            return _serve(_root_html, filename="index.html")
        # Litestar's `{path:path}` capture includes the leading slash; pathlib's
        # `/` operator replaces the base when given an absolute path, so always
        # strip leading + trailing slashes before joining.
        rel = path.strip("/")
        if not rel:
            return _serve(_root_html, filename="index.html")
        static_file = _FRONTEND_BUILD / rel
        # 1. Exact match (PNG, SVG, robots.txt, etc.)
        if static_file.is_file():
            return _serve(static_file)
        # 2. Prerendered page — adapter-static writes /about → about.html
        html_file = _FRONTEND_BUILD / (rel + ".html")
        if html_file.is_file():
            return _serve(html_file, filename="index.html")
        # 3. SPA fallback for (algo) routes and anything else
        return _serve(_index_html, filename="index.html")

    @get("/", include_in_schema=False)
    async def _spa_root() -> File:  # noqa: F811
        return _serve(_root_html, filename="index.html")

    logger.info(f"Serving SvelteKit build from {_FRONTEND_BUILD}")
else:
    logger.info("SvelteKit build not found — static serving skipped (dev mode)")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

_route_handlers = [
    AuthController,
    AdminController,
    AgentController,
    AuditController,
    StrategiesController,
    NavController,
    InvestorAdminController,
    InvestorPortalController,
    InvestorStatementsController,
    HistoryController,
    HoldingsController,
    PositionsController,
    FundsController,
    MarketController,
    NewsController,
    GrammarTokenController,
    AgentTemplateController,
    OrderTemplateController,
    OrdersController,
    AccountsController,
    InstrumentsController,
    QuoteController,
    SparklineController,
    ContactController,
    ConfigController,
    SettingsController,
    AlertsController,
    HealthController,
    PersistenceAdminController,
    BrokerHealthController,
    BrokerConnectionEventsController,
    PnLController,
    SimulatorController,
    ReplayController,
    LiveController,
    ExecutionController,
    LogsController,
    MetricsController,
    PerfController,
    ChartsController,
    OptionsController,
    BrokersController,
    HedgeProxiesController,
    ResearchController,
    EconomicController,
    WatchlistController,
    SymbolsController,
    performance_ws_handler,
    algo_ws_handler,
]

if _FRONTEND_BUILD.exists():
    _route_handlers += [_static_router, _assets_router, _spa_fallback, _spa_root]

async def _rebuild_broker_connections() -> None:
    """Move broker accounts off secrets.yaml onto the DB-backed view.
    Runs once on startup after init_db so the broker_accounts table
    exists. First run also seeds DB from YAML for backwards compat."""
    from backend.brokers.connections import Connections
    try:
        await Connections().rebuild_from_db()
    except Exception as e:
        logger.warning(f"broker rebuild_from_db failed (sticking with YAML view): {e}")


def _start_mmap_reader() -> None:
    """Start the MmapTickReader when conn_service owns the KiteTicker (slice 4)."""
    import asyncio as _asyncio
    from backend.brokers.kite_ticker import get_ticker
    reader = get_ticker()
    reader.set_loop(_asyncio.get_event_loop())
    reader.start()
    logger.info(
        "MmapTickReader: poller started — KiteTicker WS lives in "
        "conn_service (slice 4); ticks read from /dev/shm/ramboq_ticks"
    )


def _resolve_creds_conn_service() -> tuple[str | None, str | None, str]:
    """Fetch api_key + access_token from conn_service for the first Kite account."""
    from backend.brokers.client.remote_broker import list_remote_accounts, fetch_access_token
    for r in list_remote_accounts():
        if r.get("broker_id") not in ("zerodha_kite", "kite"):
            continue
        acct = r["account"]
        ak, tok = fetch_access_token(acct)
        if ak and tok:
            logger.info(
                f"KiteTicker (conn_svc): using account {acct} "
                f"(api_key=…{ak[-4:]})"
            )
            return ak, tok, acct
    return None, None, ""


def _kite_broker_creds(broker: "Any") -> "tuple[str | None, str | None, str] | None":
    """Extract (api_key, access_token, account) from one KiteBroker adapter.

    Returns None when the broker has no usable token so the caller can
    continue to the next broker in the list.
    """
    from backend.brokers.connections import Connections
    conn = getattr(broker, "_conn", None)
    if conn is None:
        account = broker.account
        conn = Connections().conn.get(account)
    if conn is None:
        return None
    tok = getattr(conn, "_access_token", None) or (
        conn.get_access_token() if hasattr(conn, "get_access_token") else None
    )
    if not tok:
        return None
    api_key = getattr(conn, "api_key", None)
    acct = getattr(conn, "account", "") or ""
    logger.info(
        f"KiteTicker: using account {acct or '?'} "
        f"(api_key=…{(api_key or '')[-4:]})"
    )
    return api_key, tok, acct


def _resolve_creds_local() -> tuple[str | None, str | None, str]:
    """Walk local sparkline broker adapters to find a live Kite access_token."""
    from backend.brokers.registry import get_sparkline_broker
    from backend.brokers.adapters.kite import KiteBroker
    spark_broker = get_sparkline_broker()
    brokers = getattr(spark_broker, "_brokers", [spark_broker])
    for broker in brokers:
        if not isinstance(broker, KiteBroker):
            continue
        result = _kite_broker_creds(broker)
        if result is not None:
            return result
    return None, None, ""


async def _start_kite_ticker() -> None:
    """
    Start the KiteTicker WebSocket for the sparkline-broker account.

    Must run after _rebuild_broker_connections() so the Connections
    singleton holds a valid access_token. Uses the sparkline-dedicated
    account (get_sparkline_broker()) to avoid contending with the
    chart-historical 3 req/sec budget.

    Resolution of api_key + access_token:
      get_sparkline_broker() returns a PriceBroker wrapper whose
      _brokers[0] is a KiteBroker adapter. KiteBroker wraps a
      KiteConnection which exposes .api_key and .get_access_token().
      We walk the PriceBroker._brokers list to find the first Kite
      account that has a live access_token.

    Deferred gracefully if:
      - No broker accounts are configured.
      - The sparkline broker's access_token is None (not yet
        authenticated — token restore from disk may still be in progress,
        or the account hasn't logged in yet). The sparkline endpoint
        falls back to broker.ltp() via TickerManager.get_ltp() → None
        until the ticker eventually connects (e.g. after the first
        background performance tick logs in).
    """
    try:
        import os as _os
        from backend.brokers.kite_ticker import get_ticker
        from backend.shared.helpers.utils import is_engine_idle

        if is_engine_idle():
            logger.info(
                "KiteTicker: skipped startup — engine idle (dev). "
                "Pick PAPER/SIM/REPLAY from navbar to activate."
            )
            return

        if _os.environ.get("RAMBOQ_USE_CONN_SERVICE", "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            _start_mmap_reader()
            return

        _use_conn_svc = _os.environ.get(
            "RAMBOQ_USE_CONN_SERVICE", "",
        ).strip().lower() in ("1", "true", "yes", "on")

        if _use_conn_svc:
            api_key, access_token, _ticker_account = _resolve_creds_conn_service()
        else:
            api_key, access_token, _ticker_account = _resolve_creds_local()

        if not api_key or not access_token:
            logger.warning(
                "KiteTicker: no live access_token found at startup — "
                "ticker not started; sparkline will fall back to broker.ltp()"
            )
            return

        import asyncio as _asyncio
        get_ticker().set_loop(_asyncio.get_event_loop())
        get_ticker().start(api_key, access_token, account=_ticker_account)

    except KeyError:
        logger.warning(
            "KiteTicker: no broker accounts configured — ticker skipped"
        )
    except Exception:
        logger.exception(
            "KiteTicker: failed to start at app boot — sparkline will "
            "fall back to broker.ltp()"
        )


# ── Visitor IP / location logger ─────────────────────────────────────────
#
# Logs the approximate origin of every page open so the operator can see
# in /api/admin/logs ("System log" tab) when a visitor lands. Site sits
# behind Cloudflare, so the request headers carry:
#
#   CF-Connecting-IP  : real client IP (not the CF edge proxy)
#   CF-IPCountry      : 2-letter ISO country code (per CF GeoIP)
#   CF-Ray            : <id>-<colo> where colo is a 3-letter CF datacenter
#                       code (BOM = Mumbai, SIN = Singapore, LHR = London,
#                       IAD = Ashburn VA, …) — coarse geographic hint
#                       about which CF edge served the request.
#
# City + company resolution now uses the local MaxMind GeoLite2 databases
# (/usr/share/GeoIP/GeoLite2-City.mmdb + GeoLite2-ASN.mmdb) — same files
# the daily visitor_report.py batch reads. Lookups are memory-mapped,
# ~1 ms, no network round-trip, no rate limit. The IP digest is logged
# inline on the [visitor] line (no follow-up [visitor-loc] line) so the
# operator sees the full "who" in one row of the System log tab.
# Honours visitors.ignore_ips and visitors.ignore_companies from
# /admin/settings so the server's own outbound + operator's laptop +
# any noisy hosting providers don't spam the System log.
from time import monotonic
_visitor_log_cache: dict[tuple[str, str], float] = {}
_VISITOR_LOG_TTL_SEC    = 60 * 60       # re-log a known IP after 1 hour
_VISITOR_LOG_EVICT_SEC  = 60 * 60 * 24  # drop entries older than 24 h
# MaxMind reader handles — opened once at process start (memory-mapped).
_mmdb_city = None
_mmdb_asn  = None

def _mmdb_open() -> None:
    """Open the GeoLite2 databases on first use. Re-tried on every
    cache-miss request so a restart with the .mmdb files in place
    starts working without a service bounce."""
    global _mmdb_city, _mmdb_asn
    if _mmdb_city is not None and _mmdb_asn is not None:
        return
    try:
        import maxminddb
        from pathlib import Path as _P
        city = _P("/usr/share/GeoIP/GeoLite2-City.mmdb")
        asn  = _P("/usr/share/GeoIP/GeoLite2-ASN.mmdb")
        if _mmdb_city is None and city.exists():
            _mmdb_city = maxminddb.open_database(str(city))
        if _mmdb_asn is None and asn.exists():
            _mmdb_asn = maxminddb.open_database(str(asn))
    except Exception:
        pass


def _is_private_ip(ip: str) -> bool:
    """Skip GeoIP lookup for RFC1918 / loopback / link-local IPs —
    ip-api would just return a 'private range' error and we'd waste
    a request."""
    if not ip or ip == "?":
        return True
    if ip.startswith(("10.", "127.", "192.168.", "169.254.", "172.")):
        return True
    if ip in ("::1", "0.0.0.0") or ip.startswith("fc") or ip.startswith("fd"):
        return True
    return False


def _mmdb_city_region(rec: dict) -> str:
    """Extract 'City, Region' from a MaxMind city record dict."""
    city_obj = (rec.get("city") or {}).get("names") or {}
    city = city_obj.get("en") or ""
    subdivisions = rec.get("subdivisions") or [{}]
    region = subdivisions[0].get("iso_code") or ""
    if city and region:
        return f"{city}, {region}"
    return city


def _mmdb_city_lookup(ip: str) -> str:
    """Return 'City, Region' string from the city MaxMind DB, or '' on miss."""
    if _mmdb_city is None:
        return ""
    try:
        rec = _mmdb_city.get(ip) or {}
        return _mmdb_city_region(rec)
    except Exception:
        return ""


def _mmdb_asn_lookup(ip: str) -> "tuple[str, str]":
    """Return (asn_string, company_short) from the ASN MaxMind DB, or ('', '') on miss."""
    if _mmdb_asn is None:
        return "", ""
    try:
        asn_rec = _mmdb_asn.get(ip) or {}
        asn_num = asn_rec.get("autonomous_system_number")
        asn_org = asn_rec.get("autonomous_system_organization") or ""
        asn = f"AS{asn_num}" if asn_num else ""
        company = ""
        if asn_org:
            try:
                from backend.scripts.visitor_report import _shorten_company
                company = _shorten_company(asn_org)
            except Exception:
                company = asn_org[:40]
        return asn, company
    except Exception:
        return "", ""


def _mmdb_lookup(ip: str) -> tuple[str, str, str]:
    """Return (city_region, company_short, asn) via local MaxMind. Empty
    strings on miss — the hook still logs country + colo from CF headers
    so a missing .mmdb file degrades gracefully."""
    _mmdb_open()
    city_region = _mmdb_city_lookup(ip)
    asn, company = _mmdb_asn_lookup(ip)
    return city_region, company, asn


def _ip_in_ignore_list(ip: str, ips_raw: str) -> bool:
    """Return True if ip exactly matches or starts with any pattern in the CSV list."""
    for pat in (p.strip() for p in ips_raw.split(",") if p.strip()):
        if ip == pat or ip.startswith(pat):
            return True
    return False


def _company_in_ignore_list(company: str, companies_raw: str) -> bool:
    """Return True if company (lowercased) contains any pattern in the CSV list."""
    if not company:
        return False
    cl = company.lower()
    for pat in (p.strip().lower() for p in companies_raw.split(",") if p.strip()):
        if pat and pat in cl:
            return True
    return False


def _visitor_ignored(ip: str, company: str) -> bool:
    """Honour visitors.ignore_ips + visitors.ignore_companies from
    /admin/settings. Operators add their laptop IP + hosting providers
    so those visitors don't spam the System log."""
    try:
        from backend.shared.helpers.settings import get_string as _get_string
        ips_raw = _get_string("visitors.ignore_ips", "")
        companies_raw = _get_string("visitors.ignore_companies", "")
    except Exception:
        return False
    return _ip_in_ignore_list(ip, ips_raw) or _company_in_ignore_list(company, companies_raw)


_SKIP_PATH_PREFIXES = ("/assets/", "/_app/", "/cdn-cgi/")
_SKIP_PATH_SUFFIXES = (".png", ".jpg", ".svg", ".css", ".js", ".woff", ".woff2", ".map")


def _lv_should_skip_path(path: str) -> bool:
    """Return True for static assets + infra paths the visitor log should ignore."""
    if path == "/favicon.ico":
        return True
    if any(path.startswith(p) for p in _SKIP_PATH_PREFIXES):
        return True
    if any(path.endswith(s) for s in _SKIP_PATH_SUFFIXES):
        return True
    return False


def _lv_extract_client(headers, request) -> tuple[str, str, str, str]:  # type: ignore[no-untyped-def]
    """Extract (ip, country, colo, ua) from Cloudflare + standard headers."""
    ip = (
        headers.get("CF-Connecting-IP")
        or (headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        or (request.client.host if request.client else "")
        or "?"
    )
    country = headers.get("CF-IPCountry") or "??"
    cf_ray = headers.get("CF-Ray") or ""
    colo = cf_ray.rsplit("-", 1)[-1] if "-" in cf_ray else "?"
    ua = (headers.get("User-Agent") or "")[:80]
    return ip, country, colo, ua


def _lv_touch_cache(key: tuple[str, str], now: float) -> bool:
    """Evict stale entries and return True if the key was seen recently.

    Returns True  → caller should skip logging (already logged this hour).
    Returns False → caller should proceed and then mark the key as seen.
    """
    for k in [k for k, t in _visitor_log_cache.items()
              if (now - t) > _VISITOR_LOG_EVICT_SEC]:
        _visitor_log_cache.pop(k, None)
    last = _visitor_log_cache.get(key)
    return last is not None and (now - last) < _VISITOR_LOG_TTL_SEC


def _lv_format_log_line(ip: str, country: str, colo: str,
                         city_region: str, company: str, asn: str,
                         method: str, path: str, ua: str) -> str:
    """Build the [visitor] log line digest."""
    loc_parts = [p for p in (city_region, company, asn) if p]
    loc_part = (" — " + " · ".join(loc_parts)) if loc_parts else ""
    return f"[visitor] {ip} ({country}, CF:{colo}){loc_part} {method} {path} UA=\"{ua}\""


async def _log_visitor(request) -> None:  # type: ignore[no-untyped-def]
    """Litestar before_request hook — resets per-request market-data broker
    cache + logs every first-sight-per-hour visitor to the operator-facing
    System log with country / city / region / company / ASN resolved inline
    via local MaxMind. Skips static asset paths and IPs matched by
    visitors.ignore_ips / visitors.ignore_companies so the System tab reads
    as real third-party visitor traffic."""
    # Reset per-request market-data broker selection so that quote / instruments
    # / historical_data calls within this request always use the same session.
    try:
        from backend.brokers.registry import reset_market_data_broker_ctx
        reset_market_data_broker_ctx()
    except Exception:
        pass
    try:
        path = request.scope.get("path") or ""
        if _lv_should_skip_path(path):
            return
        headers = request.headers
        ip, country, colo, ua = _lv_extract_client(headers, request)

        now = monotonic()
        key = (ip, country)
        if _lv_touch_cache(key, now):
            return

        # MaxMind enrichment — fast (~1 ms, memory-mapped). Skip for
        # private IPs since MaxMind would just return None and we'd
        # waste the dict-walk.
        city_region, company, asn = ("", "", "")
        if not _is_private_ip(ip):
            city_region, company, asn = _mmdb_lookup(ip)

        # Operator-managed ignore filters — applies AFTER MaxMind so the
        # company check has a value to compare against.
        if _visitor_ignored(ip, company):
            # Still mark as seen so a flapping ignore-rule doesn't open
            # the flood gates retroactively for the rest of the hour.
            _visitor_log_cache[key] = now
            return

        _visitor_log_cache[key] = now
        method = request.scope.get("method") or "GET"
        logger.info(_lv_format_log_line(ip, country, colo, city_region, company, asn, method, path, ua))
    except Exception:
        # Hook must NEVER break a real request — geolog is best-effort.
        pass


async def _stop_kite_ticker(app) -> None:  # noqa: ARG001
    """Gracefully close the KiteTicker WebSocket on Litestar shutdown."""
    try:
        from backend.brokers.kite_ticker import get_ticker
        get_ticker().stop()
    except Exception:
        pass


async def _start_write_queue(app) -> None:  # noqa: ARG001
    from backend.api.persistence import write_queue
    await write_queue.start()


async def _stop_write_queue(app) -> None:  # noqa: ARG001
    from backend.api.persistence import write_queue
    await write_queue.stop()


async def _start_event_queues(app) -> None:  # noqa: ARG001
    """Start the four EventQueue flush tasks for high-frequency event writers."""
    from backend.api.routes.algo import algo_event_queue
    from backend.api.algo.events import agent_event_queue
    from backend.api.algo.order_events import order_event_queue
    from backend.api.routes.research import mcp_audit_queue
    await algo_event_queue.start()
    await agent_event_queue.start()
    await order_event_queue.start()
    await mcp_audit_queue.start()
    logger.info("event_queues: algo_event + agent_event + order_event + mcp_audit started")


async def _stop_event_queues(app) -> None:  # noqa: ARG001
    """Gracefully flush + stop all four EventQueues on shutdown."""
    from backend.api.routes.algo import algo_event_queue
    from backend.api.algo.events import agent_event_queue
    from backend.api.algo.order_events import order_event_queue
    from backend.api.routes.research import mcp_audit_queue
    await algo_event_queue.stop()
    await agent_event_queue.stop()
    await order_event_queue.stop()
    await mcp_audit_queue.stop()
    logger.info("event_queues: all stopped and flushed")


from backend.api.audit import AuditMiddleware

# Optional dev-only perf-stats middleware. Gated on RAMBOQ_PERF_STATS=1
# so prod never pays the timing / query-count cost per request. The
# module import is cheap (no side-effects); only the wiring calls fire
# when the flag is set. Writes `.log/perf_stats.json` every 5 min +
# on shutdown.
from backend.api.middleware import perf_stats as _perf_stats

_perf_middleware: list = []
_perf_on_startup: list = []
_perf_on_shutdown: list = []
if _perf_stats.is_enabled():
    _perf_middleware.append(_perf_stats.PerfStatsMiddleware())

    async def _start_perf_stats() -> None:
        _perf_stats.attach_sqlalchemy_listener()
        _perf_stats.start_background_flusher()
        logger.info("[perf_stats] enabled (RAMBOQ_PERF_STATS=1)")

    async def _stop_perf_stats() -> None:
        _perf_stats.shutdown_flush()

    _perf_on_startup.append(_start_perf_stats)
    _perf_on_shutdown.append(_stop_perf_stats)

app = Litestar(
    route_handlers=_route_handlers,
    cors_config=cors_config,
    openapi_config=openapi_config,
    on_startup=[init_db, _rebuild_broker_connections, seed_hedge_proxies, _start_kite_ticker, bg_startup, _start_write_queue, _start_event_queues, *_perf_on_startup],
    on_shutdown=[bg_shutdown, _stop_kite_ticker, _stop_write_queue, _stop_event_queues, *_perf_on_shutdown],
    before_request=_log_visitor,
    # Audit middleware — writes one audit_log row per mutating
    # request after the response leaves the server. Reads + suppressed
    # paths (health, /auth/whoami, etc.) short-circuit at zero cost.
    middleware=[AuditMiddleware(), *_perf_middleware],
)
