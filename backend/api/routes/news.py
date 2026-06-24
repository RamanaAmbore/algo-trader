"""
Stock-market news feed — headlines persisted in Postgres.

Pulls from curated Indian and global financial RSS feeds (already pre-filtered
by their editors), applies a small keyword exclusion for noise, dedupes by
link, and stores in Postgres. Wipes the table every morning at 07:00 IST,
aligned with the daily market-report refresh. Gated by is_enabled('market_feed').
"""

import asyncio
import re
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime

# Force IPv4 for outbound requests — the server's IPv6 /48 can reach Kite but
# hangs on public internet hosts. Matches the override already applied in
# alert_utils.py so anything using requests/urllib3 goes over IPv4.
import urllib3.util.connection as _urllib3_conn
_urllib3_conn.HAS_IPV6 = False

import requests

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
from backend.shared.helpers.utils import is_enabled

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

_reset_lock = threading.Lock()
_last_reset: date | None = None


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


async def _maybe_reset() -> None:
    """Truncate news_headlines once per day after 07:00 IST (morning rollover)."""
    global _last_reset
    now = timestamp_indian()
    today = now.date()
    seven_am = now.replace(hour=7, minute=0, second=0, microsecond=0)
    with _reset_lock:
        due = (now >= seven_am and _last_reset != today)
        if due:
            _last_reset = today
    if due:
        try:
            async with async_session() as s:
                await s.execute(delete(NewsHeadline))
                await s.commit()
            logger.info("News: headlines table cleared for new trading day")
        except Exception as e:
            logger.error(f"News: reset failed: {e}")


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


_FEED_TIMEOUT = 4  # seconds per feed — slow publishers get dropped from this cycle


def _fetch_one_feed(url: str) -> list[tuple[datetime, dict]]:
    r = requests.get(url, headers={
        "User-Agent": _UA,
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5",
    }, timeout=_FEED_TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out: list[tuple[datetime, dict]] = []
    for item in list(root.iterfind(".//item"))[:40]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        src_el = item.find("source")
        # Fall back to the domain as source when the feed doesn't include one
        source = ((src_el.text if src_el is not None else "") or "").strip()
        if not source:
            try:
                from urllib.parse import urlparse
                source = urlparse(link).netloc.removeprefix("www.")
            except Exception:
                source = ""
        if not title or not link or not pub:
            continue
        if _NOISE_RE.search(title):
            continue
        if _is_low_info(title):
            continue
        try:
            dt = parsedate_to_datetime(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        out.append((dt, {
            "link": link, "title": title, "source": source,
            "published_at": dt, "timestamp_display": _fmt_stamp(dt),
        }))
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


async def _fetch_and_accumulate() -> NewsResponse:
    """Fetch RSS feeds, insert new links into DB, return the full accumulated list."""
    if not is_enabled('market_feed'):
        return NewsResponse(items=[], refreshed_at=timestamp_display())

    await _maybe_reset()

    loop = asyncio.get_running_loop()
    try:
        fresh = await loop.run_in_executor(None, _fetch_rss)
    except Exception as e:
        logger.error(f"News fetch failed: {e}")
        fresh = []

    try:
        async with async_session() as s:
            # ── Purge stale rows on every fetch ────────────────────────────
            # 1. '?' titles (clickbait / speculative).
            await s.execute(delete(NewsHeadline).where(NewsHeadline.title.like('%?%')))
            # 2. Re-apply current filters + cross-publisher dedupe to whatever
            #    is already stored, so legacy rows from earlier code versions
            #    don't linger on the feed.
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
                await s.execute(
                    delete(NewsHeadline).where(NewsHeadline.link.in_(stale_links))
                )
                logger.info(f"News: purged {len(stale_links)} stale/duplicate rows")

            if fresh:
                # Exact-link dedupe against what remains after the purge.
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
                if added or stale_links:
                    await s.commit()
                if added:
                    logger.info(f"News: +{added} new headlines")

            rows = await s.execute(
                select(NewsHeadline).order_by(NewsHeadline.published_at.desc())
            )
            db_items = list(rows.scalars().all())
            items = [
                NewsItem(
                    title=h.title, link=h.link,
                    source=h.source or "", timestamp=h.timestamp_display or "",
                )
                for h in db_items
            ]
    except Exception as e:
        logger.error(f"News DB query failed: {e}")
        items = []
        db_items = []

    # `refreshed_at` reflects the timestamp of the most recent headline
    # — i.e., when the news content was actually last updated, not when
    # this poll ran. Falls back to "now" only when no headlines have
    # been persisted yet (cold-start before the first feed fetch).
    from backend.shared.helpers.date_time_utils import format_dual_tz
    if db_items:
        refreshed = format_dual_tz(db_items[0].published_at)
    else:
        refreshed = timestamp_display()

    return NewsResponse(items=items, refreshed_at=refreshed)


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


class NewsController(Controller):
    path = "/api/news"

    @get("/")
    async def get_news(self, sentiment: bool = False) -> NewsResponse:
        """Default response carries no sentiment field (free-tier
        friendly). Pass ?sentiment=true to add bull / bear / neutral
        tags — used by the MCP get_recent_news tool. Score result is
        cached for 10 min separately so back-to-back operator calls
        share the LLM round-trip."""
        try:
            if sentiment:
                return await get_or_fetch("news_scored", _fetch_and_score, ttl_seconds=_CACHE_TTL)
            return await get_or_fetch("news", _fetch_and_accumulate, ttl_seconds=_CACHE_TTL)
        except Exception as e:
            logger.error(f"News API error: {e}")
            raise HTTPException(status_code=500, detail=str(e))
