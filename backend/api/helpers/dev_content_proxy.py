"""
Dev-environment proxy layer for shared market-summary + news content.

2026-09: prod is the sole generator of `/api/market` and `/api/news`
content (see `background._daily_content_generation_enabled`) — that
content is public, non-account-specific, and identical between
environments, so independently generating it on both dev and prod (both
waking on the same 05:30 IST trigger) doubled Gemini quota usage and
duplicate RSS egress for no operator benefit. Dev instead reads prod's
already-generated content over loopback:

    http://127.0.0.1:8000   — prod's uvicorn bind port. See
    webhook/deploy.sh: `PORT=$([ "$ENV" = "prod" ] && echo 8000 || echo 8001)`.

Loopback (not nginx/DNS) sidesteps the IPv6-egress quirk documented in
CLAUDE.md's "Things to Avoid" (that quirk is specific to outbound ntfy.sh
calls, but loopback avoids the whole public-egress path regardless).

Failure mode follows CLAUDE.md's "Staleness indicator freeze rule": any
degraded/failed loopback fetch freezes to the last successfully-decoded
payload for that path (module-level, this-process-lifetime, ignores any
TTL) rather than ever serving blank/empty content or silently falling
back to the static YAML placeholder text. `fetch_from_prod` raises
`ProdProxyUnavailable` ONLY when no last-known-good payload has ever been
captured yet for that path (e.g. a cold dev boot racing a prod outage) —
callers are expected to have their own final fallback for that one case
(e.g. dev's own historical DB row).

This module intentionally does NOT implement its own TTL cache on the
happy path — callers wrap `fetch_from_prod` in
`backend.api.cache.get_or_fetch(key, ..., ttl_seconds=TTL_SECONDS)`, the
same short-TTL-caching mechanism used everywhere else in this codebase,
so there is exactly one place TTL bookkeeping happens.
"""

import time

import httpx
import msgspec

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_PROD_BASE_URL = "http://127.0.0.1:8000"

_TIMEOUT = httpx.Timeout(8.0, connect=3.0)

# Short TTL — NOT prod's own 86400s in-process cache for /api/market (that
# TTL exists because prod's content only changes once a day; a proxied
# copy of it needs to notice regenerations far sooner). Prod's daily
# content-refresh cycle can update market/news at any point during its
# 05:30-08:00 IST retry window, and the news RSS keep-warm can land new
# headlines within minutes of publication. A few minutes keeps dev
# "reasonably current" with prod's regenerations without a loopback GET
# on every single dev page view.
TTL_SECONDS = 180  # 3 minutes


class ProdProxyUnavailable(Exception):
    """Raised by `fetch_from_prod` only when the loopback fetch failed AND
    no last-known-good payload has ever been captured yet for this path
    in this process's lifetime. Callers should catch this and fall back
    to their own last-resort source rather than let it propagate as a
    bare 500."""


# path -> (captured_at monotonic, raw response bytes). Deliberately
# ignores TTL/expiry — this is the "last known good", not the happy-path
# cache (that's `cache.get_or_fetch`'s job at the call site).
_last_good: dict[str, tuple[float, bytes]] = {}


def _reset_for_tests() -> None:
    """Test-only: clear all captured last-known-good state so tests don't
    leak state across cases via this module-level dict."""
    _last_good.clear()


def _always_valid(_payload) -> bool:
    return True


async def fetch_from_prod(path: str, response_type, is_valid=_always_valid):
    """One network attempt: GET `path` from prod over loopback, decode as
    `response_type` (a msgspec.Struct matching the prod route's own
    response schema — the shape is unchanged by proxying, so this is a
    faithful re-decode of exactly what prod's own route would have
    returned to a direct caller).

    `is_valid(payload) -> bool` distinguishes a healthy response from an
    HTTP-200-but-actually-degraded body — prod's own routes can return
    200 with content that is itself a masked failure (e.g.
    `_db_or_gemini`'s cold-boot `_UNAVAILABLE` placeholder string, or
    `_fetch_and_accumulate`'s `items=[]` on a transient DB read failure).
    Per CLAUDE.md's staleness-freeze rule (the A1 pattern), such a
    response must NEVER overwrite a real last-known-good payload — it is
    treated exactly like a network failure. Defaults to "anything that
    decodes is valid" for callers that don't need this check.

    On any failure (network error, non-2xx, decode error, or
    `is_valid` returning False), freezes to the last successfully-
    validated payload for this path if one exists (logged loudly, but
    the return path is otherwise a normal success — never blank, never
    the static YAML placeholder). Raises `ProdProxyUnavailable` only when
    no last-known-good payload exists yet for this path."""
    now = time.monotonic()

    def _degrade(reason: str):
        good = _last_good.get(path)
        if good:
            age_s = int(now - good[0])
            logger.warning(
                f"[DEV-CONTENT-PROXY] {path} degraded ({reason}) — serving "
                f"last-known-good payload captured ~{age_s}s ago"
            )
            return msgspec.json.decode(good[1], type=response_type)
        logger.error(
            f"[DEV-CONTENT-PROXY] {path} degraded ({reason}) — no "
            f"last-known-good payload captured yet this process"
        )
        raise ProdProxyUnavailable(reason)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{_PROD_BASE_URL}{path}")
            resp.raise_for_status()
            raw = resp.content
        payload = msgspec.json.decode(raw, type=response_type)
    except Exception as e:
        return _degrade(str(e))

    if not is_valid(payload):
        return _degrade("prod returned a degraded/empty payload (200 masking a failure)")

    _last_good[path] = (now, raw)
    return payload
