"""
Litestar API application.

Single process serves both the REST API and the SvelteKit SPA.
All /api/* and /ws/* routes are handled by Litestar; everything else falls
through to index.html (SPA fallback) so SvelteKit client-side routing works.

Background refresh (holdings/positions/funds, market warm, alerts, open/close
summaries) runs as asyncio tasks within this same process — no Redis, no ARQ.
"""

from pathlib import Path

from litestar import Litestar, get
from litestar.config.cors import CORSConfig
from litestar.openapi import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin
from litestar.response import File
from litestar.static_files import create_static_files_router

from backend.api.background import on_startup as bg_startup, on_shutdown as bg_shutdown
from backend.api.database import init_db
from backend.api.routes.admin import AdminController
from backend.api.routes.agents import AgentController
from backend.api.routes.algo import AlgoController, algo_ws_handler
from backend.api.routes.auth import AuthController
from backend.api.routes.config import ConfigController
from backend.api.routes.contact import ContactController
from backend.api.routes.funds import FundsController
from backend.api.routes.holdings import HoldingsController
from backend.api.routes.market import MarketController
from backend.api.routes.news import NewsController
from backend.api.routes.grammar import GrammarTokenController
from backend.api.routes.instruments import InstrumentsController
from backend.api.routes.orders import AccountsController, OrdersController
from backend.api.routes.quote import QuoteController
from backend.api.routes.positions import PositionsController
from backend.api.routes.settings import SettingsController
from backend.api.routes.brokers import BrokersController
from backend.api.routes.charts import ChartsController
from backend.api.routes.options import OptionsController
from backend.api.routes.alerts import AlertsController
from backend.api.routes.health import HealthController
from backend.api.routes.pnl import PnLController
from backend.api.routes.simulator import SimulatorController
from backend.api.routes.replay import ReplayController
from backend.api.routes.shadow import ShadowController
from backend.api.routes.live import LiveController
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
    _index_html = _FRONTEND_BUILD / "index.html"

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

    @get("/{path:path}", include_in_schema=False)
    async def _spa_fallback(path: str) -> File:  # noqa: F811
        # Serve static files (logo.png, nav_image.png, etc.) if they exist
        static_file = _FRONTEND_BUILD / path
        if static_file.is_file() and ".." not in path:
            return File(path=static_file, content_disposition_type="inline")
        return File(path=_index_html, filename="index.html", content_disposition_type="inline")

    @get("/", include_in_schema=False)
    async def _spa_root() -> File:  # noqa: F811
        return File(path=_index_html, filename="index.html", content_disposition_type="inline")

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
    AlgoController,
    HoldingsController,
    PositionsController,
    FundsController,
    MarketController,
    NewsController,
    GrammarTokenController,
    OrdersController,
    AccountsController,
    InstrumentsController,
    QuoteController,
    ContactController,
    ConfigController,
    SettingsController,
    AlertsController,
    HealthController,
    PnLController,
    SimulatorController,
    ReplayController,
    ShadowController,
    LiveController,
    ChartsController,
    OptionsController,
    BrokersController,
    performance_ws_handler,
    algo_ws_handler,
]

if _FRONTEND_BUILD.exists():
    _route_handlers += [_static_router, _assets_router, _spa_fallback, _spa_root]

async def _rebuild_broker_connections() -> None:
    """Move broker accounts off secrets.yaml onto the DB-backed view.
    Runs once on startup after init_db so the broker_accounts table
    exists. First run also seeds DB from YAML for backwards compat."""
    from backend.shared.helpers.connections import Connections
    try:
        await Connections().rebuild_from_db()
    except Exception as e:
        logger.warning(f"broker rebuild_from_db failed (sticking with YAML view): {e}")


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
# Country + colo gets us "approx location" without an external service.
# For city-level resolution we additionally hit ip-api.com — free, no
# auth, 45 req/min. With our 1-hour-per-IP dedup that's plenty of
# headroom. The HTTP call is fire-and-forget (asyncio.create_task) so
# it never blocks the actual request; the city info appears on a
# follow-up `[visitor-loc]` log line once the lookup returns.
from time import monotonic
import asyncio
_visitor_log_cache: dict[tuple[str, str], float] = {}
_visitor_loc_cache: dict[str, str] = {}        # ip → "City, Region, Country (ISP)"
_visitor_loc_inflight: set[str] = set()        # IPs currently being looked up
_VISITOR_LOG_TTL_SEC    = 60 * 60       # re-log a known IP after 1 hour
_VISITOR_LOG_EVICT_SEC  = 60 * 60 * 24  # drop entries older than 24 h


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


async def _resolve_location(ip: str) -> None:
    """Fire-and-forget GeoIP lookup against ip-api.com. Caches the
    result so each IP is only queried once. Logs the resolved
    location on a `[visitor-loc]` line so the operator sees the
    enriched info alongside the original `[visitor]` line.
    Network failures and odd responses are swallowed silently —
    the visitor logger keeps working with country + colo only."""
    if ip in _visitor_loc_cache or ip in _visitor_loc_inflight:
        return
    _visitor_loc_inflight.add(ip)
    try:
        import httpx
        url = f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp"
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(url)
            data = r.json()
        if data.get("status") != "success":
            return
        loc = ", ".join(filter(None, [
            data.get("city"), data.get("regionName"), data.get("country"),
        ])) or "?"
        isp = data.get("isp") or ""
        line = f"{loc}" + (f" ({isp})" if isp else "")
        _visitor_loc_cache[ip] = line
        logger.info(f"[visitor-loc] {ip} → {line}")
    except Exception:
        pass
    finally:
        _visitor_loc_inflight.discard(ip)


async def _log_visitor(request) -> None:  # type: ignore[no-untyped-def]
    """Litestar before_request hook — logs the visitor's IP +
    country + CF colo on first sight per hour, kicks off a
    background GeoIP lookup for city-level enrichment. Skips
    static asset paths so the log isn't drowned in
    /assets/*.js fetches."""
    try:
        path = request.scope.get("path") or ""
        # Skip static + asset traffic — the operator wants to see
        # "someone opened the site," not every chunk fetch.
        if (path.startswith("/assets/")
                or path.startswith("/_app/")
                or path == "/favicon.ico"
                or path.endswith(".png") or path.endswith(".jpg")
                or path.endswith(".svg") or path.endswith(".css")
                or path.endswith(".js")  or path.endswith(".woff")
                or path.endswith(".woff2") or path.endswith(".map")):
            return
        headers = request.headers
        # Cloudflare-supplied real client IP. Fallback chain for
        # local / dev (no Cloudflare) so the hook still produces
        # something useful: X-Forwarded-For (first hop) → request
        # client tuple → "?".
        ip = (
            headers.get("CF-Connecting-IP")
            or (headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or (request.client.host if request.client else "")
            or "?"
        )
        country = headers.get("CF-IPCountry") or "??"
        # CF-Ray format: <id>-<colo>. Last hyphen-separated chunk is
        # the 3-letter CF datacenter code that served the request.
        cf_ray = headers.get("CF-Ray") or ""
        colo = cf_ray.rsplit("-", 1)[-1] if "-" in cf_ray else "?"
        ua = (headers.get("User-Agent") or "")[:80]

        now = monotonic()
        key = (ip, country)
        # Evict stale entries lazily so the dict doesn't grow forever.
        for k in [k for k, t in _visitor_log_cache.items()
                  if (now - t) > _VISITOR_LOG_EVICT_SEC]:
            _visitor_log_cache.pop(k, None)
        last = _visitor_log_cache.get(key)
        if last is not None and (now - last) < _VISITOR_LOG_TTL_SEC:
            return
        _visitor_log_cache[key] = now
        method = request.scope.get("method") or "GET"
        # Inline the cached city/region if we already resolved this IP
        # — saves the operator from cross-referencing the follow-up
        # `[visitor-loc]` line.
        loc_inline = _visitor_loc_cache.get(ip)
        loc_part = f" — {loc_inline}" if loc_inline else ""
        logger.info(f"[visitor] {ip} ({country}, CF:{colo}){loc_part} {method} {path} UA=\"{ua}\"")
        # Fire off background lookup for city-level info on first
        # sight. Real-IP only — RFC1918 / loopback / dev IPs are
        # skipped (ip-api would error on them and we'd waste a hit).
        if not _is_private_ip(ip):
            asyncio.create_task(_resolve_location(ip))
    except Exception:
        # The hook must NEVER break a real request — geolog is
        # best-effort.
        pass


app = Litestar(
    route_handlers=_route_handlers,
    cors_config=cors_config,
    openapi_config=openapi_config,
    on_startup=[init_db, _rebuild_broker_connections, bg_startup],
    on_shutdown=[bg_shutdown],
    before_request=_log_visitor,
)
