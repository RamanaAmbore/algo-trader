"""
Stock-market news feed — headlines persisted in Postgres.

Pulls from curated Indian and global financial RSS feeds (already pre-filtered
by their editors), applies a small keyword exclusion for noise, dedupes by
link, and stores in Postgres. Gated by is_enabled('market_feed').

Daily reset (2026-09 scheduling redesign): the table is truncated + reloaded
from fresh RSS once per calendar day via `_perform_news_reset_once()`
(background.py's `_daily_content_refresh_cycle`, spawned from the SAME
05:30 IST wake-up `_task_holiday_refresh` uses for the holiday calendar —
one shared clock, not an independent one). The reset marker is persisted
in the `settings` table (`news.last_reset_date`), not an in-memory flag,
so it survives a process restart. This request path (`_fetch_and_accumulate`)
no longer triggers the truncation itself — it only accumulates/dedupes.
A separate recurring keep-warm task (`background._task_news_keepwarm`,
every 5 minutes) primes the plain "news" cache key (never "news_scored")
so a visitor rarely hits a cold RSS fetch.
"""

import asyncio
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# Force IPv4 for outbound requests — the server's IPv6 /48 can reach Kite but
# hangs on public internet hosts. Matches the override already applied in
# alert_utils.py so anything using requests/urllib3 goes over IPv4.
import urllib3.util.connection as _urllib3_conn
_urllib3_conn.HAS_IPV6 = False

import requests
import msgspec

from litestar import Controller, get
from litestar.exceptions import HTTPException
from sqlalchemy import select, delete

from backend.api.cache import get_or_fetch
from backend.api.database import async_session
from backend.api.models import NewsHeadline
from backend.api.schemas import NewsItem, NewsResponse
from backend.shared.helpers.date_time_utils import (
    timestamp_display,
    timestamp_indian,
    timestamp_est,
)
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import is_enabled, is_prod_branch

logger = get_logger(__name__)

_CACHE_TTL = 60   # 1-minute route-level coalescing — operator-visible News feeds
                   # poll every 2 min, so a 1-min cache shields the upstream
                   # RSS hosts from N concurrent operators while still surfacing
                   # new headlines within ~3 min of publication.

# Curated Indian financial RSS feeds — market coverage only.
_FEEDS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.moneycontrol.com/rss/business.xml",
    "https://www.business-standard.com/rss/markets-106.rss",
    "https://www.livemint.com/rss/markets",
    "https://www.financialexpress.com/market/feed/",
    "https://www.ndtvprofit.com/feed",
    "https://www.zeebiz.com/rss/markets",
]

# Drop obvious non-market content that sometimes sneaks into general sections.
_NOISE_RE = re.compile(
    r'\b(horoscope|astrology|bollywood|hollywood|cricket|ipl|kohli|dhoni|sports|'
    r'weather|recipe|cooking|travel|tourism|lifestyle|fashion|beauty|health\s+tip|'
    r'viral\s+video|whatsapp\s+status|rashifal|vastu)\b',
    re.IGNORECASE,
)

# Drop low-information headlines — pure stubs, "X in 10 seconds" fillers, etc.
_STUB_RE = re.compile(
    r'^\s*('
    r'market\s+(update|wrap|recap|roundup|close|open)s?'
    r'|stock\s+updates?'
    r'|daily\s+(wrap|recap|roundup)'
    r'|morning\s+(brief|briefing)'
    r'|closing\s+bell'
    r'|news\s+(wrap|recap)'
    r'|top\s+\d+\s+(news|stocks?|gainers?|losers?)'
    r')\s*[:\-—|]*\s*$',
    re.IGNORECASE,
)

_MIN_TITLE_CHARS = 40  # headlines shorter than this are usually just labels
_MIN_TITLE_WORDS = 5

# US-centric markers — indicate the story is about US markets/companies.
_US_RE = re.compile(
    r'\b(wall\s+street|s&p\s*500|nasdaq|dow\s+jones|dow\s+industrial|'
    r'us\s+(stocks?|market|tech|economy|gdp|inflation|jobs)|'
    r'federal\s+reserve|fomc|jerome\s+powell|janet\s+yellen|'
    r'tesla|apple|microsoft|nvidia|alphabet|google|meta|amazon|netflix|'
    r'pentagon|white\s+house|washington\s+(dc|\.\s+c\.)|'
    r'biden|trump|harris|secretary\s+of\s+(state|treasury))\b',
    re.IGNORECASE,
)

# India-relevant markers — if any of these appear, keep the story even when
# it also mentions the US (the US angle is then Indian-market-relevant).
_IN_RE = re.compile(
    r'\b(nifty|sensex|nse|bse|nfo|sebi|rbi|reserve\s+bank|indian|india|'
    r'mumbai|delhi|bengaluru|chennai|hyderabad|kolkata|pune|'
    r'\brupee\b|\binr\b|fii|dii|dalal\s+street|'
    r'tata|reliance|adani|infosys|wipro|hdfc|icici|sbi|bajaj|mahindra|'
    r'ambani|modi|sitharaman|ministry\s+of\s+finance)\b',
    re.IGNORECASE,
)


# Short words that don't distinguish stories — excluded from the dedupe fingerprint.
_STOP_TOKENS = frozenset({
    'the', 'and', 'for', 'from', 'with', 'this', 'that', 'over', 'into', 'amid',
    'after', 'before', 'says', 'said', 'today', 'news', 'market', 'markets',
    'stock', 'stocks', 'shares', 'report', 'live', 'update', 'breaking',
    'week', 'day', 'morning', 'evening', 'session', 'amp',
})


def _title_fingerprint(title: str) -> str:
    """
    Order-independent fingerprint of the informative tokens in a headline.
    Two headlines covering the same story tend to share the same fingerprint
    even when wording differs. Strips " - Source" suffix, lowercases, keeps
    alnum tokens ≥3 chars, drops stop-words, uses the top-10 sorted unique.
    """
    t = (title or "").lower()
    t = re.sub(r'\s+[-—|]\s+[^-—|]{1,40}$', '', t)
    tokens = re.findall(r'\b[a-z0-9]{3,}\b', t)
    keyed = [tok for tok in tokens if tok not in _STOP_TOKENS]
    return ' '.join(sorted(set(keyed))[:10])


def _is_low_info(title: str) -> bool:
    """Drop headlines that carry no substantive information."""
    t = (title or "").strip()
    if not t:
        return True
    # Question-mark headlines are almost always speculative/clickbait ("Will Nifty
    # hit 30,000?") — drop them.
    if '?' in t:
        return True
    # US-only stories with no Indian-market angle — skip.
    if _US_RE.search(t) and not _IN_RE.search(t):
        return True
    # Strip trailing " - Source" suffix Google News appends, for length checks
    stripped = re.sub(r'\s+[-—|]\s+[^-—|]{1,40}$', '', t)
    if len(stripped) < _MIN_TITLE_CHARS:
        return True
    if len(stripped.split()) < _MIN_TITLE_WORDS:
        return True
    if _STUB_RE.match(stripped):
        return True
    return False

def _fmt_stamp(dt: datetime) -> str:
    try:
        ist = dt.astimezone(timestamp_indian().tzinfo)
        est = dt.astimezone(timestamp_est().tzinfo)
        return (
            f"{ist.strftime('%a, %b %d, %Y, %I:%M %p IST')} | "
            f"{est.strftime('%a, %b %d, %Y, %I:%M %p %Z')}"
        )
    except Exception:
        return ""


_NEWS_RESET_MARKER_KEY = "news.last_reset_date"

# Serializes the two DB write paths that touch news_headlines wholesale:
# _perform_news_reset_once's truncate+reload and _fetch_and_accumulate's
# purge+insert. Without this, a startup catch-up reset racing the
# keep-warm task's first cycle (both can fire within moments of a boot
# that lands just before 05:30 IST) could interleave a DELETE from one
# with an INSERT from the other, corrupting the accumulated set or
# raising a duplicate-key error that surfaces as an empty NewsResponse.
_NEWS_WRITE_LOCK = asyncio.Lock()


async def _news_needs_reset_today() -> bool:
    """DB-persisted marker read straight from the settings table — NOT
    via `settings.get_string`/`_CACHE` (that in-process cache is loaded
    once at boot via `reload_cache()` and won't see a same-process write
    made moments earlier by `_news_mark_reset_done_today`).

    Used ONLY by the background/startup-catchup path
    (`_perform_news_reset_once` / `_daily_content_refresh_cycle`) —
    replaces the old in-memory `_last_reset` + wall-clock 07:00 check,
    which reset to None on every process restart and could re-truncate
    the table on a redeploy even after today's reset had already run."""
    from backend.api.models import Setting

    try:
        async with async_session() as s:
            row = (await s.execute(
                select(Setting).where(Setting.key == _NEWS_RESET_MARKER_KEY)
            )).scalar_one_or_none()
        if not row or not row.value:
            return True
        return row.value != timestamp_indian().date().isoformat()
    except Exception as e:
        logger.warning(f"News: reset-marker read failed (assume needs reset): {e}")
        return True


async def _news_mark_reset_done_today() -> None:
    """Persist today's IST date as the reset marker. Called ONLY after a
    successful truncate + reload — never before, so a crash mid-reset
    doesn't falsely mark today as done."""
    from backend.api.models import Setting

    today_iso = timestamp_indian().date().isoformat()
    async with async_session() as s:
        row = (await s.execute(
            select(Setting).where(Setting.key == _NEWS_RESET_MARKER_KEY)
        )).scalar_one_or_none()
        if row:
            row.value = today_iso
        else:
            s.add(Setting(
                category="news", key=_NEWS_RESET_MARKER_KEY, value_type="string",
                value=today_iso, default_value="",
                description=(
                    "Internal marker (not operator-facing) — last IST "
                    "calendar date the news_headlines table was truncated "
                    "and reloaded from fresh RSS."
                ),
            ))
        await s.commit()


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


_FEED_TIMEOUT = 4  # seconds per feed — slow publishers get dropped from this cycle


def _resolve_item_source(src_el, link: str) -> str:
    """Return the item's source string, falling back to the link domain."""
    source = ((src_el.text if src_el is not None else "") or "").strip()
    if not source:
        try:
            from urllib.parse import urlparse
            source = urlparse(link).netloc.removeprefix("www.")
        except Exception:
            source = ""
    return source


def _parse_pub_date(pub: str) -> datetime | None:
    """Parse an RSS pubDate string into a timezone-aware datetime, or None on failure."""
    try:
        dt = parsedate_to_datetime(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_feed_item(item) -> tuple[datetime, dict] | None:
    """Parse one RSS <item> element into a (datetime, row-dict) pair.

    Returns None when the item should be skipped (missing fields, noise, etc.).
    """
    title = (item.findtext("title") or "").strip()
    link = (item.findtext("link") or "").strip()
    pub = (item.findtext("pubDate") or "").strip()
    if not title or not link or not pub:
        return None
    if _NOISE_RE.search(title):
        return None
    if _is_low_info(title):
        return None
    dt = _parse_pub_date(pub)
    if dt is None:
        return None
    source = _resolve_item_source(item.find("source"), link)
    return dt, {
        "link": link, "title": title, "source": source,
        "published_at": dt, "timestamp_display": _fmt_stamp(dt),
    }


def _fetch_one_feed(url: str) -> list[tuple[datetime, dict]]:
    r = requests.get(url, headers={
        "User-Agent": _UA,
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5",
    }, timeout=_FEED_TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out: list[tuple[datetime, dict]] = []
    for item in list(root.iterfind(".//item"))[:40]:
        parsed = _parse_feed_item(item)
        if parsed is not None:
            out.append(parsed)
    return out


def _fetch_rss() -> list[tuple[datetime, dict]]:
    """Fetch every configured feed in parallel; return whatever finished in time."""
    merged: dict[str, tuple[datetime, dict]] = {}
    with ThreadPoolExecutor(max_workers=len(_FEEDS)) as ex:
        future_by_url = {ex.submit(_fetch_one_feed, u): u for u in _FEEDS}
        try:
            for fut in as_completed(future_by_url, timeout=_FEED_TIMEOUT + 2):
                url = future_by_url[fut]
                try:
                    for dt, row in fut.result(timeout=0.1):
                        merged.setdefault(row["link"], (dt, row))
                except Exception as e:
                    logger.warning(f"News feed {url[:60]}… failed: {e}")
        except TimeoutError:
            # Slow feeds don't kill the batch — keep whatever completed.
            slow = [url for f, url in future_by_url.items() if not f.done()]
            logger.warning(f"News: {len(slow)} feed(s) timed out, using partial results")
    return list(merged.values())


async def _purge_stale_db_rows(s) -> tuple[list[str], set[str]]:
    """Delete question-mark titles, re-filter noisy rows, and cross-dedupe.

    Returns (stale_links, seen_fps) so the insert phase can skip already-seen
    fingerprints without a second query.
    """
    # 1. '?' titles (clickbait / speculative).
    await s.execute(delete(NewsHeadline).where(NewsHeadline.title.like('%?%')))
    # 2. Re-apply current filters + cross-publisher dedupe to existing rows so
    #    legacy rows from earlier code versions don't linger on the feed.
    all_rows = await s.execute(
        select(NewsHeadline.link, NewsHeadline.title)
        .order_by(NewsHeadline.published_at.desc())
    )
    seen_fps: set[str] = set()
    stale_links: list[str] = []
    for link, title in all_rows.all():
        if _NOISE_RE.search(title) or _is_low_info(title):
            stale_links.append(link)
            continue
        fp = _title_fingerprint(title)
        if not fp or fp in seen_fps:
            stale_links.append(link)
            continue
        seen_fps.add(fp)
    if stale_links:
        await s.execute(delete(NewsHeadline).where(NewsHeadline.link.in_(stale_links)))
        logger.info(f"News: purged {len(stale_links)} stale/duplicate rows")
    return stale_links, seen_fps


async def _insert_new_headlines(s, fresh: list, stale_links: list[str], seen_fps: set[str]) -> int:
    """Insert fresh RSS rows not already present in DB after the purge.

    Returns the number of headlines added.
    """
    if not fresh:
        return 0
    existing_links = await s.execute(
        select(NewsHeadline.link).where(
            NewsHeadline.link.in_([row["link"] for _, row in fresh])
        )
    )
    have_links = {r[0] for r in existing_links} - set(stale_links)
    added = 0
    for _dt, row in fresh:
        if row["link"] in have_links:
            continue
        fp = _title_fingerprint(row["title"])
        if not fp or fp in seen_fps:
            continue
        seen_fps.add(fp)
        s.add(NewsHeadline(**row))
        added += 1
    return added


def _resolve_refreshed(db_items: list) -> str:
    """Return the dual-tz string for the most recent headline, or 'now' on cold start."""
    from backend.shared.helpers.date_time_utils import format_dual_tz
    if db_items:
        return format_dual_tz(db_items[0].published_at)
    return timestamp_display()


def _news_items_from_db_rows(db_items: list) -> list:
    """Shared row -> NewsItem mapping — used by both the live accumulate
    path and the background reset's cache-priming path so the two never
    drift out of sync."""
    return [
        NewsItem(
            title=h.title, link=h.link,
            source=h.source or "", timestamp=h.timestamp_display or "",
        )
        for h in db_items
    ]


async def _build_news_response_from_db() -> NewsResponse:
    """Query the full current news_headlines table and build a
    NewsResponse — shared tail used by `_fetch_and_accumulate` and by
    `_perform_news_reset_once`'s cache-priming step (avoids a second RSS
    round-trip just to build the response object after a reset)."""
    async with async_session() as s:
        rows = await s.execute(
            select(NewsHeadline).order_by(NewsHeadline.published_at.desc())
        )
        db_items = list(rows.scalars().all())
    items = _news_items_from_db_rows(db_items)
    return NewsResponse(items=items, refreshed_at=_resolve_refreshed(db_items))


async def _fetch_and_accumulate() -> NewsResponse:
    """Fetch RSS feeds, insert new links into DB, return the full accumulated list.

    Daily truncation no longer happens on this hot request path — see
    `_perform_news_reset_once` (background.py's `_daily_content_refresh_cycle`,
    chained off the same 05:30 IST wake-up as the holiday calendar
    refresh). This function only accumulates/dedupes."""
    if not is_enabled('market_feed'):
        return NewsResponse(items=[], refreshed_at=timestamp_display())

    loop = asyncio.get_running_loop()
    try:
        fresh = await loop.run_in_executor(None, _fetch_rss)
    except Exception as e:
        logger.error(f"News fetch failed: {e}")
        fresh = []

    try:
        async with _NEWS_WRITE_LOCK:
            async with async_session() as s:
                stale_links, seen_fps = await _purge_stale_db_rows(s)
                added = await _insert_new_headlines(s, fresh, stale_links, seen_fps)
                if added or stale_links:
                    await s.commit()
                if added:
                    logger.info(f"News: +{added} new headlines")

                rows = await s.execute(
                    select(NewsHeadline).order_by(NewsHeadline.published_at.desc())
                )
                db_items = list(rows.scalars().all())
                items = _news_items_from_db_rows(db_items)
    except Exception as e:
        logger.error(f"News DB query failed: {e}")
        items = []
        db_items = []

    return NewsResponse(items=items, refreshed_at=_resolve_refreshed(db_items))


async def _perform_news_reset_once() -> bool:
    """One daily news-reset attempt: fetch fresh RSS, and ONLY on a
    non-empty successful fetch, truncate news_headlines and reload from
    it — never truncate first (a failed fetch must leave yesterday's
    headlines visible all morning, not an empty page). Marks the reset
    done + primes the plain "news" cache key (never "news_scored" — that
    variant calls Gemini per-headline and the public page never requests
    it) only on success. Returns True on success, False to signal retry.
    """
    if not is_enabled('market_feed'):
        return True   # capability off — nothing to do, don't spin retries

    loop = asyncio.get_running_loop()
    try:
        fresh = await loop.run_in_executor(None, _fetch_rss)
    except Exception as e:
        logger.error(f"[DAILY-CONTENT] news RSS fetch failed: {e}")
        return False
    if not fresh:
        logger.warning(
            "[DAILY-CONTENT] news RSS fetch returned nothing — will retry, no truncate"
        )
        return False

    try:
        async with _NEWS_WRITE_LOCK:
            async with async_session() as s:
                await s.execute(delete(NewsHeadline))
                added = await _insert_new_headlines(s, fresh, [], set())
                await s.commit()
        logger.info(f"News: daily reset — table truncated, {added} fresh headlines loaded")
    except Exception as e:
        logger.error(f"[DAILY-CONTENT] news DB reset failed: {e}")
        return False

    try:
        await _news_mark_reset_done_today()
    except Exception as e:
        # Don't let a marker-write failure crash the whole cycle (via
        # _guarded in background.py) and skip the remaining retries —
        # the reset itself already succeeded; worst case we re-run an
        # already-successful reset on the next retry pass, which is
        # harmless (truncate+reload is idempotent).
        logger.error(f"[DAILY-CONTENT] news reset-marker write failed: {e}")

    try:
        from backend.api import cache as _cache_mod
        fresh_response = await _build_news_response_from_db()
        _cache_mod.put("news", fresh_response, ttl_seconds=360)
    except Exception as e:
        logger.warning(f"[DAILY-CONTENT] news cache prime failed (non-fatal): {e}")

    return True


async def _fetch_and_score() -> NewsResponse:
    """Same as _fetch_and_accumulate, then tag each item with a
    bull / bear / neutral sentiment. Separate cache key + TTL so the
    Gemini Flash free-tier RPM budget isn't hammered — sentiment-tagged
    payload caches for 10 min like the base feed.
    """
    base = await _fetch_and_accumulate()
    if not base.items:
        return base
    try:
        from backend.shared.helpers.genai_helpers import sentiment_scores
        scored = sentiment_scores([it.title for it in base.items])
    except Exception as e:
        logger.warning(f"sentiment scoring failed (no tags): {e}")
        scored = []
    if not scored or len(scored) != len(base.items):
        return base
    tagged = [
        NewsItem(
            title=it.title, link=it.link, source=it.source,
            timestamp=it.timestamp, sentiment=scored[i],
        )
        for i, it in enumerate(base.items)
    ]
    return NewsResponse(items=tagged, refreshed_at=base.refreshed_at)


async def _dev_news_content(sentiment: bool = False) -> NewsResponse:
    """Dev-only: proxy-read prod's already-generated/accumulated news feed
    over loopback (query string forwarded so `?sentiment=true` still gets
    prod's own Gemini-scored result — prod does the real work, dev only
    reads it, so this never violates "dev must never call Gemini").

    Falls back to dev's own accumulated `news_headlines` rows (never a
    fresh RSS fetch, never Gemini) only when the proxy has no
    last-known-good payload yet for this process. That fallback always
    marks `stale=True, source="snapshot-fallback"` — reached only when
    the live proxy has already failed AND no frozen proxy copy exists
    either."""
    from backend.api.helpers.dev_content_proxy import (
        ProdProxyUnavailable,
        fetch_from_prod,
    )

    path = "/api/news?sentiment=true" if sentiment else "/api/news"
    try:
        return await fetch_from_prod(path, NewsResponse, is_valid=lambda n: bool(n.items))
    except ProdProxyUnavailable:
        logger.error(
            f"News: prod proxy unavailable for {path!r}, no last-known-good "
            f"yet — falling back to dev's own accumulated DB rows"
        )
        resp = await _build_news_response_from_db()
        return msgspec.structs.replace(resp, stale=True, source="snapshot-fallback")


class NewsController(Controller):
    path = "/api/news"

    @get("/")
    async def get_news(self, sentiment: bool = False) -> NewsResponse:
        """Default response carries no sentiment field (free-tier
        friendly). Pass ?sentiment=true to add bull / bear / neutral
        tags — used by the MCP get_recent_news tool. Score result is
        cached for 10 min separately so back-to-back operator calls
        share the LLM round-trip.

        2026-09: on dev, proxy-reads prod's already-generated feed over
        loopback (short TTL, freeze-to-last-good) instead of ever
        fetching RSS or calling Gemini locally — see
        `_dev_news_content` / `dev_content_proxy.py`."""
        try:
            if not is_prod_branch():
                from backend.api.helpers.dev_content_proxy import TTL_SECONDS as _DEV_TTL
                cache_key = "news_scored_dev_proxy" if sentiment else "news_dev_proxy"

                async def _dev_fetch() -> NewsResponse:
                    return await _dev_news_content(sentiment)

                return await get_or_fetch(cache_key, _dev_fetch, ttl_seconds=_DEV_TTL)
            if sentiment:
                return await get_or_fetch("news_scored", _fetch_and_score, ttl_seconds=_CACHE_TTL)
            return await get_or_fetch("news", _fetch_and_accumulate, ttl_seconds=_CACHE_TTL)
        except Exception as e:
            logger.error(f"News API error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
