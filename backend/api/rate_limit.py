"""
In-process rate limiter for auth-sensitive endpoints.

CLAUDE.md mandates a single uvicorn worker on prod, so per-process state
is sufficient — no Redis or cross-worker coordination needed. The
limiter is a sliding-window counter keyed on (client_ip, route), with
the IP resolved from Cloudflare's CF-Connecting-IP header (real client)
falling back to X-Forwarded-For and the request peer.

Used as a Litestar guard. Build one via `make_rate_limit_guard(limit,
window_seconds)` and pass it to the route's `guards=[...]` list.

Repeat-offender escalation
--------------------------

A normal sliding-window limiter is fine for a slow human typing the
wrong password a few times in a row. It's NOT fine for the case we hit
in production: a stuck client (browser auto-submit / saved password
loop) that hammers the endpoint every ~2 seconds for hours. Each hit
still costs the limiter a dict lookup, a bucket scan, and a log write
— at 30 hits/min for 13+ hours that's >20k WARN lines plus the CPU
overhead.

So the limiter now ALSO tracks per-IP "strikes" — the count of 429s
issued in a rolling window. As strikes accumulate the retry_after
returned to the client escalates from the normal short value to
several minutes, then to half an hour. The intent isn't to defeat a
determined attacker (that's Cloudflare's job upstream); it's to take a
stuck client off the hot path so it stops generating log + CPU
pressure. Logs are also throttled to once per minute per IP once the
rate-limit state is established.

Industry analogue: this is the in-process equivalent of fail2ban /
Cloudflare's "challenge → block" escalation, scaled down to what's
sensible inside a single-worker Python app.
"""

import time
from collections import defaultdict, deque
from threading import Lock

from litestar.connection import ASGIConnection
from litestar.exceptions import HTTPException
from litestar.handlers.base import BaseRouteHandler

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Escalation tuning ────────────────────────────────────────────────
# A "strike" = one 429 response. Strikes are counted in a 10-minute
# rolling window per IP across all routes. The thresholds map strike
# count to the retry_after value we return:
#
#   < TIER1_THRESHOLD     → normal retry_after (window-based, < 60 s)
#   TIER1_THRESHOLD-TIER2 → 5-minute lockout
#   ≥ TIER2_THRESHOLD     → 30-minute lockout
#
# Numbers chosen by observing the real prod stuck-client storm: it
# hammered ~30 times per minute, so a 5-min lockout (300 s) at 10
# strikes catches it on the third minute. The 30-min tier at 50 catches
# the truly relentless offenders.
_STRIKE_WINDOW       = 600     # 10 minutes
_TIER1_THRESHOLD     = 10
_TIER1_RETRY_AFTER   = 300     # 5 minutes
_TIER2_THRESHOLD     = 50
_TIER2_RETRY_AFTER   = 1800    # 30 minutes
_LOG_COOLDOWN        = 60      # log at most once / minute / IP


class _Limiter:
    """Sliding-window counter. `_buckets[(ip, route)]` holds a deque of
    monotonic timestamps for hits inside the active window; entries
    older than the window are purged on every hit. Memory cost is
    O(active_clients × limit) which stays small at our scale."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        # Per-IP strike timestamps for escalation. Lives in a deque so
        # the 10-min window ages out naturally without bookkeeping.
        self._strikes: dict[str, deque[float]] = defaultdict(deque)
        # Per-IP last-WARN-emitted timestamp for log throttling.
        self._last_log: dict[str, float] = {}
        self._lock = Lock()

    def hit(self, key: tuple[str, str], limit: int, window: float) -> tuple[bool, int, int]:
        """Record an attempt. Returns (allowed, retry_after_seconds, strike_count).

        `strike_count` is the per-IP strike count after applying this
        hit (only incremented when allowed=False) — the guard uses it
        to escalate retry_after and to throttle the WARN log.

        Empty deques are evicted from the dict on each hit so a long
        scanner sweep across many IPs doesn't accumulate dead keys
        forever — `_buckets` stays bounded by the active set, not the
        all-time set."""
        now = time.monotonic()
        cutoff = now - window
        strike_cutoff = now - _STRIKE_WINDOW
        ip = key[0]
        with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            # Age out old strikes regardless of outcome — keeps the deque
            # bounded and lets a quiet IP "redeem" itself by walking
            # its strike count back to zero over 10 min of silence.
            strikes = self._strikes[ip]
            while strikes and strikes[0] < strike_cutoff:
                strikes.popleft()
            if not bucket:
                del self._buckets[key]
                self._buckets[key].append(now)
                return True, 0, len(strikes)
            if len(bucket) >= limit:
                # Record the strike and compute the escalated retry.
                strikes.append(now)
                strike_count = len(strikes)
                if strike_count >= _TIER2_THRESHOLD:
                    retry = _TIER2_RETRY_AFTER
                elif strike_count >= _TIER1_THRESHOLD:
                    retry = _TIER1_RETRY_AFTER
                else:
                    retry = max(1, int(bucket[0] + window - now))
                return False, retry, strike_count
            bucket.append(now)
            return True, 0, len(strikes)

    def should_log(self, ip: str) -> bool:
        """Return True at most once per _LOG_COOLDOWN seconds per IP.

        A stuck client emitting 30 hits/min would otherwise produce a
        WARN line every 2 s — log throttling keeps the file readable
        and the OS-level write traffic sane."""
        now = time.monotonic()
        with self._lock:
            last = self._last_log.get(ip, 0.0)
            if now - last < _LOG_COOLDOWN:
                return False
            self._last_log[ip] = now
            return True


_limiter = _Limiter()


def _client_ip(connection: ASGIConnection) -> str:
    """Resolve the client IP. On prod (behind Cloudflare) the
    CF-Connecting-IP header is set by the proxy and trustworthy. On dev
    (no Cloudflare) any client can spoof the header to share buckets,
    so we ignore it and use the peer IP only — this prevents a
    malicious dev client from bypassing per-IP throttling by rotating
    a fake CF header value."""
    from backend.shared.helpers.utils import is_prod_branch
    headers = connection.headers
    if is_prod_branch():
        return (
            headers.get("CF-Connecting-IP")
            or (headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or (connection.client.host if connection.client else "")
            or "anon"
        )
    # dev: trust only the peer.
    return (connection.client.host if connection.client else "anon")


def make_rate_limit_guard(limit: int, window_seconds: int):
    """Build a Litestar guard that throttles requests to `limit` per
    `window_seconds` per (client_ip, route_path). Raises 429 with a
    Retry-After header when the bucket overflows."""
    async def _guard(connection: ASGIConnection, handler: BaseRouteHandler) -> None:  # noqa: ARG001
        ip = _client_ip(connection)
        path = connection.scope.get("path", "")
        ok, retry, strikes = _limiter.hit((ip, path), limit, window_seconds)
        if not ok:
            # Log every block at DEBUG so the full history is captured
            # for post-mortem, but emit the loud WARN only once per
            # minute per IP. Without this throttle a stuck client
            # writes a WARN line every ~2 s for hours.
            tier = (
                "tier2-30min" if strikes >= _TIER2_THRESHOLD
                else "tier1-5min" if strikes >= _TIER1_THRESHOLD
                else "tier0-window"
            )
            line = (
                f"RateLimit: blocked ip={ip} path={path} "
                f"limit={limit}/{window_seconds}s retry_after={retry}s "
                f"strikes={strikes} {tier}"
            )
            logger.debug(line)
            if _limiter.should_log(ip):
                logger.warning(line)
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down and try again shortly.",
                headers={"Retry-After": str(retry)},
            )
    return _guard
