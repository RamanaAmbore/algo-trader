"""
2026-09 — shared market/news generation between dev and prod.

Prod is the sole Gemini/RSS generator (`background._daily_content_generation_enabled`,
driven by the same `deploy_branch` config signal `_task_deploy_sync_check()` reads).
Dev's `/api/market` and `/api/news` routes instead proxy-read prod's content over
loopback with a short TTL and a freeze-to-last-good failure mode
(`backend.api.helpers.dev_content_proxy`). An admin-only dry-run endpoint lets the
operator preview a not-yet-merged Gemini prompt (B7) without persisting anything.

See .claude/PLAN.md's "Market summary + news: share one generation between dev and
prod" section for the full design brief.

No live network calls anywhere in this file — HTTP is mocked via
`httpx.MockTransport`; Gemini is mocked via monkeypatching `fetch_fresh`.
"""

import httpx
import msgspec
import pytest

from backend.api.helpers import dev_content_proxy
from backend.api.schemas import MarketResponse, NewsItem, NewsResponse
from backend.shared.helpers import utils as shared_utils


@pytest.fixture(autouse=True)
def _clear_proxy_state():
    """The proxy's last-known-good store is a module-level dict — clear it
    before and after every test so cases don't leak state into each other."""
    dev_content_proxy._reset_for_tests()
    yield
    dev_content_proxy._reset_for_tests()


# Captured BEFORE any test monkeypatches `httpx.AsyncClient` — since
# `dev_content_proxy.httpx` is the exact same module object as `httpx`
# here (not a copy), patching one attribute-patches the other; using this
# real reference inside the mock factory avoids the factory recursively
# calling itself.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_async_client_factory(handler):
    """Return a drop-in replacement for `httpx.AsyncClient(...)` that
    ignores real network config (timeout, base_url, ...) and always
    routes through `handler` via `httpx.MockTransport` — no live network
    calls, no live socket ever opened."""
    def _factory(*_args, **_kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))
    return _factory


# ---------------------------------------------------------------------------
# (a) env-signal decision logic — pure function, no subprocess/network
# ---------------------------------------------------------------------------

def test_daily_content_generation_enabled_on_main(monkeypatch):
    from backend.api import background
    monkeypatch.setitem(shared_utils.config, "deploy_branch", "main")
    assert background._daily_content_generation_enabled() is True


def test_daily_content_generation_disabled_on_dev_branch(monkeypatch):
    from backend.api import background
    monkeypatch.setitem(shared_utils.config, "deploy_branch", "workshop")
    assert background._daily_content_generation_enabled() is False

    monkeypatch.setitem(shared_utils.config, "deploy_branch", "dev")
    assert background._daily_content_generation_enabled() is False


@pytest.mark.asyncio
async def test_daily_content_refresh_cycle_noop_on_dev(monkeypatch):
    """The whole daily generation cycle (market Gemini call + news RSS
    truncate/reload) must not run at all on dev — it must return
    immediately without ever touching the retry loop."""
    from backend.api import background

    monkeypatch.setitem(shared_utils.config, "deploy_branch", "dev")

    async def _boom():
        raise AssertionError("must not be called on dev")

    monkeypatch.setattr(background, "_market_needs_refresh_today", _boom)
    monkeypatch.setattr(background, "_perform_market_refresh_once", _boom)

    # Would hang forever (or raise) if the gate didn't return early —
    # this call completing at all is part of the assertion.
    await background._daily_content_refresh_cycle()


@pytest.mark.asyncio
async def test_daily_content_refresh_cycle_runs_on_prod(monkeypatch):
    """Sanity check the gate doesn't also swallow prod's real cycle."""
    from backend.api import background

    monkeypatch.setitem(shared_utils.config, "deploy_branch", "main")

    calls = {"market": 0, "news": 0}

    async def _market_done():
        calls["market"] += 1
        return True  # already done today — loop exits immediately

    async def _news_done():
        calls["news"] += 1
        return True

    monkeypatch.setattr(background, "_market_needs_refresh_today", _market_done)
    # _daily_content_refresh_cycle does a local
    # `from backend.api.routes.news import _news_needs_reset_today` — patch
    # the source module attribute, not a (nonexistent) background one.
    import backend.api.routes.news as news_mod
    monkeypatch.setattr(news_mod, "_news_needs_reset_today", _news_done)

    await background._daily_content_refresh_cycle()
    assert calls["market"] == 1
    assert calls["news"] == 1


@pytest.mark.asyncio
async def test_news_keepwarm_once_noop_on_dev(monkeypatch):
    from backend.api import background
    import backend.api.routes.news as news_mod

    monkeypatch.setitem(shared_utils.config, "deploy_branch", "dev")

    async def _boom():
        raise AssertionError("must not fetch RSS on dev")

    monkeypatch.setattr(news_mod, "_fetch_and_accumulate", _boom)

    await background._news_keepwarm_once()  # returns cleanly, never calls RSS


@pytest.mark.asyncio
async def test_news_keepwarm_once_runs_on_prod(monkeypatch):
    from backend.api import background
    import backend.api.routes.news as news_mod

    monkeypatch.setitem(shared_utils.config, "deploy_branch", "main")

    calls = {"n": 0}

    async def _fake_fetch():
        calls["n"] += 1
        return NewsResponse(
            items=[NewsItem(title="t", link="l", source="s", timestamp="ts")],
            refreshed_at="now",
        )

    monkeypatch.setattr(news_mod, "_fetch_and_accumulate", _fake_fetch)

    await background._news_keepwarm_once()
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# (b) proxy-fetch success / failure / freeze-to-last-good paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_from_prod_success_caches_last_good(monkeypatch):
    market = MarketResponse(content="Hello market", cycle_date="2026-09-27", refreshed_at="now")

    def handler(request):
        assert request.url.path == "/api/market"
        return httpx.Response(200, content=msgspec.json.encode(market))

    monkeypatch.setattr(dev_content_proxy.httpx, "AsyncClient", _mock_async_client_factory(handler))

    result = await dev_content_proxy.fetch_from_prod("/api/market", MarketResponse)
    assert result.content == "Hello market"
    assert "/api/market" in dev_content_proxy._last_good


@pytest.mark.asyncio
async def test_fetch_from_prod_network_failure_freezes_to_last_good(monkeypatch):
    good = MarketResponse(content="Good content", cycle_date="d", refreshed_at="now")

    def ok_handler(request):
        return httpx.Response(200, content=msgspec.json.encode(good))

    monkeypatch.setattr(dev_content_proxy.httpx, "AsyncClient", _mock_async_client_factory(ok_handler))
    first = await dev_content_proxy.fetch_from_prod("/api/market", MarketResponse)
    assert first.content == "Good content"

    def fail_handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(dev_content_proxy.httpx, "AsyncClient", _mock_async_client_factory(fail_handler))
    frozen = await dev_content_proxy.fetch_from_prod("/api/market", MarketResponse)
    assert frozen.content == "Good content"  # frozen to last-good, never raised/blank


@pytest.mark.asyncio
async def test_fetch_from_prod_no_last_good_raises(monkeypatch):
    def fail_handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(dev_content_proxy.httpx, "AsyncClient", _mock_async_client_factory(fail_handler))

    with pytest.raises(dev_content_proxy.ProdProxyUnavailable):
        await dev_content_proxy.fetch_from_prod("/api/market", MarketResponse)


@pytest.mark.asyncio
async def test_fetch_from_prod_masked_failure_never_captured_as_good(monkeypatch):
    """A 200 response whose BODY is itself a masked failure (prod's own
    cold-boot `_UNAVAILABLE` placeholder, or an empty news list from a
    transient DB read failure) must never overwrite a real last-known-good
    payload and must never itself be served as "good" — CLAUDE.md's A1
    staleness-freeze pattern applied to the loopback proxy."""
    good = MarketResponse(content="Real content", cycle_date="d", refreshed_at="now")
    masked_failure = MarketResponse(content="__UNAVAILABLE__", cycle_date="d", refreshed_at="later")

    def is_valid(m):
        return m.content != "__UNAVAILABLE__"

    def ok_handler(request):
        return httpx.Response(200, content=msgspec.json.encode(good))

    monkeypatch.setattr(dev_content_proxy.httpx, "AsyncClient", _mock_async_client_factory(ok_handler))
    first = await dev_content_proxy.fetch_from_prod("/api/market", MarketResponse, is_valid=is_valid)
    assert first.content == "Real content"

    def masked_handler(request):
        return httpx.Response(200, content=msgspec.json.encode(masked_failure))

    monkeypatch.setattr(dev_content_proxy.httpx, "AsyncClient", _mock_async_client_factory(masked_handler))
    frozen = await dev_content_proxy.fetch_from_prod("/api/market", MarketResponse, is_valid=is_valid)
    assert frozen.content == "Real content"  # NOT the masked-failure body

    # And the masked body must not have overwritten last-good.
    stored = msgspec.json.decode(dev_content_proxy._last_good["/api/market"][1], type=MarketResponse)
    assert stored.content == "Real content"


@pytest.mark.asyncio
async def test_dev_market_content_falls_back_to_db_when_proxy_unavailable(monkeypatch):
    import backend.api.routes.market as market_mod
    import backend.api.background as background_mod

    async def _unavailable(*_a, **_kw):
        raise dev_content_proxy.ProdProxyUnavailable("no last good")

    monkeypatch.setattr(dev_content_proxy, "fetch_from_prod", _unavailable)

    called = {"db": False}

    async def _fake_load_from_db():
        called["db"] = True
        return MarketResponse(content="dev's own stale row", cycle_date="d", refreshed_at="old")

    monkeypatch.setattr(background_mod, "_load_market_from_db", _fake_load_from_db)

    result = await market_mod._dev_market_content()
    assert called["db"] is True
    assert result.content == "dev's own stale row"


@pytest.mark.asyncio
async def test_dev_market_content_final_fallback_when_nothing_exists(monkeypatch):
    """Absolute cold-boot case: proxy has never succeeded AND dev's own DB
    has no row either — must serve the existing static placeholder, never
    raise a 500 up to the route."""
    import backend.api.routes.market as market_mod
    import backend.api.background as background_mod

    async def _unavailable(*_a, **_kw):
        raise dev_content_proxy.ProdProxyUnavailable("no last good")

    monkeypatch.setattr(dev_content_proxy, "fetch_from_prod", _unavailable)

    async def _no_db_row():
        return None

    monkeypatch.setattr(background_mod, "_load_market_from_db", _no_db_row)

    result = await market_mod._dev_market_content()
    assert result.content == market_mod._UNAVAILABLE


@pytest.mark.asyncio
async def test_dev_news_content_falls_back_to_db_when_proxy_unavailable(monkeypatch):
    import backend.api.routes.news as news_mod

    async def _unavailable(*_a, **_kw):
        raise dev_content_proxy.ProdProxyUnavailable("no last good")

    monkeypatch.setattr(dev_content_proxy, "fetch_from_prod", _unavailable)

    called = {"db": False}

    async def _fake_build():
        called["db"] = True
        return NewsResponse(items=[], refreshed_at="now")

    monkeypatch.setattr(news_mod, "_build_news_response_from_db", _fake_build)

    result = await news_mod._dev_news_content()
    assert called["db"] is True
    assert result.items == []


# ---------------------------------------------------------------------------
# (d) stale/source staleness-marker field — fresh / frozen / prod-direct
# ---------------------------------------------------------------------------

def test_market_response_defaults_to_live():
    """Any response constructed without explicit stale/source (i.e. every
    prod-direct construction site: _db_or_gemini, fetch_fresh, the dry-run
    endpoint) reports the healthy state by default."""
    resp = MarketResponse(content="x", cycle_date="d", refreshed_at="r")
    assert resp.stale is False
    assert resp.source == "live"


def test_news_response_defaults_to_live():
    resp = NewsResponse(items=[], refreshed_at="r")
    assert resp.stale is False
    assert resp.source == "live"


@pytest.mark.asyncio
async def test_fetch_from_prod_success_marks_live(monkeypatch):
    market = MarketResponse(content="Hello market", cycle_date="d", refreshed_at="now")

    def handler(request):
        return httpx.Response(200, content=msgspec.json.encode(market))

    monkeypatch.setattr(dev_content_proxy.httpx, "AsyncClient", _mock_async_client_factory(handler))

    result = await dev_content_proxy.fetch_from_prod("/api/market", MarketResponse)
    assert result.stale is False
    assert result.source == "live"


@pytest.mark.asyncio
async def test_fetch_from_prod_freeze_marks_snapshot_fallback(monkeypatch):
    good = MarketResponse(content="Good content", cycle_date="d", refreshed_at="now")

    def ok_handler(request):
        return httpx.Response(200, content=msgspec.json.encode(good))

    monkeypatch.setattr(dev_content_proxy.httpx, "AsyncClient", _mock_async_client_factory(ok_handler))
    first = await dev_content_proxy.fetch_from_prod("/api/market", MarketResponse)
    assert first.stale is False
    assert first.source == "live"

    def fail_handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(dev_content_proxy.httpx, "AsyncClient", _mock_async_client_factory(fail_handler))
    frozen = await dev_content_proxy.fetch_from_prod("/api/market", MarketResponse)
    assert frozen.content == "Good content"
    assert frozen.stale is True
    assert frozen.source == "snapshot-fallback"


@pytest.mark.asyncio
async def test_fetch_from_prod_freeze_overrides_baked_in_live_flag(monkeypatch):
    """The cached last-good bytes were captured back when the fetch
    succeeded (stale=False, source='live' baked into the JSON at that
    time) — the CURRENT call's outcome (a failure) must override those
    baked-in values, not just pass them through verbatim."""
    good = MarketResponse(content="Good content", cycle_date="d", refreshed_at="now",
                           stale=False, source="live")

    def ok_handler(request):
        return httpx.Response(200, content=msgspec.json.encode(good))

    monkeypatch.setattr(dev_content_proxy.httpx, "AsyncClient", _mock_async_client_factory(ok_handler))
    await dev_content_proxy.fetch_from_prod("/api/market", MarketResponse)

    def fail_handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(dev_content_proxy.httpx, "AsyncClient", _mock_async_client_factory(fail_handler))
    frozen = await dev_content_proxy.fetch_from_prod("/api/market", MarketResponse)
    assert frozen.stale is True
    assert frozen.source == "snapshot-fallback"


@pytest.mark.asyncio
async def test_dev_final_fallback_marks_snapshot_fallback_with_db_row(monkeypatch):
    import backend.api.routes.market as market_mod
    import backend.api.background as background_mod

    async def _fake_load_from_db():
        return MarketResponse(content="dev's own stale row", cycle_date="d", refreshed_at="old")

    monkeypatch.setattr(background_mod, "_load_market_from_db", _fake_load_from_db)

    result = await market_mod._dev_final_fallback()
    assert result.stale is True
    assert result.source == "snapshot-fallback"


@pytest.mark.asyncio
async def test_dev_final_fallback_marks_snapshot_fallback_without_db_row(monkeypatch):
    """Absolute cold-boot case (no proxy copy, no DB row either) must
    still carry the staleness marker on the static placeholder."""
    import backend.api.routes.market as market_mod
    import backend.api.background as background_mod

    async def _no_db_row():
        return None

    monkeypatch.setattr(background_mod, "_load_market_from_db", _no_db_row)

    result = await market_mod._dev_final_fallback()
    assert result.content == market_mod._UNAVAILABLE
    assert result.stale is True
    assert result.source == "snapshot-fallback"


@pytest.mark.asyncio
async def test_dev_news_content_fallback_marks_snapshot_fallback(monkeypatch):
    import backend.api.routes.news as news_mod

    async def _unavailable(*_a, **_kw):
        raise dev_content_proxy.ProdProxyUnavailable("no last good")

    monkeypatch.setattr(dev_content_proxy, "fetch_from_prod", _unavailable)

    async def _fake_build():
        return NewsResponse(items=[], refreshed_at="now")

    monkeypatch.setattr(news_mod, "_build_news_response_from_db", _fake_build)

    result = await news_mod._dev_news_content()
    assert result.stale is True
    assert result.source == "snapshot-fallback"


# ---------------------------------------------------------------------------
# (c) admin-only dry-run endpoint — mocked Gemini, asserts no DB/cache write
# ---------------------------------------------------------------------------

def test_dry_run_market_is_admin_gated():
    from backend.api.auth_guard import admin_guard
    from backend.api.routes.market import MarketController

    assert admin_guard in MarketController.dry_run_market.guards


@pytest.mark.asyncio
async def test_dry_run_market_returns_without_persisting(monkeypatch):
    import backend.api.routes.market as market_mod
    import backend.api.background as background_mod
    import backend.api.cache as cache_mod

    monkeypatch.setattr(market_mod, "is_enabled", lambda _cap: True)

    fake_result = MarketResponse(content="preview text", cycle_date="d", refreshed_at="now")
    fetch_calls = {"n": 0}

    def _fake_fetch_fresh():
        fetch_calls["n"] += 1
        return fake_result

    monkeypatch.setattr(market_mod, "fetch_fresh", _fake_fetch_fresh)

    save_calls = {"n": 0}

    async def _fake_save(*_a, **_kw):
        save_calls["n"] += 1

    monkeypatch.setattr(background_mod, "_save_market_to_db", _fake_save)

    cache_calls = {"put": 0, "invalidate": 0}
    monkeypatch.setattr(
        cache_mod, "put",
        lambda *a, **kw: cache_calls.__setitem__("put", cache_calls["put"] + 1),
    )
    monkeypatch.setattr(
        cache_mod, "invalidate",
        lambda *a, **kw: cache_calls.__setitem__("invalidate", cache_calls["invalidate"] + 1),
    )

    result = await market_mod._run_market_dry_run()

    assert result.content == "preview text"
    assert fetch_calls["n"] == 1
    assert save_calls["n"] == 0
    assert cache_calls["put"] == 0
    assert cache_calls["invalidate"] == 0


@pytest.mark.asyncio
async def test_dry_run_market_requires_genai_capability(monkeypatch):
    import backend.api.routes.market as market_mod
    from litestar.exceptions import HTTPException

    monkeypatch.setattr(market_mod, "is_enabled", lambda _cap: False)

    fetch_calls = {"n": 0}
    monkeypatch.setattr(market_mod, "fetch_fresh", lambda: fetch_calls.__setitem__("n", fetch_calls["n"] + 1))

    with pytest.raises(HTTPException) as exc_info:
        await market_mod._run_market_dry_run()

    assert exc_info.value.status_code == 400
    assert fetch_calls["n"] == 0  # never even attempted the Gemini call


@pytest.mark.asyncio
async def test_dry_run_market_502_when_gemini_returns_none(monkeypatch):
    import backend.api.routes.market as market_mod
    from litestar.exceptions import HTTPException

    monkeypatch.setattr(market_mod, "is_enabled", lambda _cap: True)
    monkeypatch.setattr(market_mod, "fetch_fresh", lambda: None)

    with pytest.raises(HTTPException) as exc_info:
        await market_mod._run_market_dry_run()

    assert exc_info.value.status_code == 502
