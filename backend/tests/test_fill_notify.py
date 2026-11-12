"""Fill records: each FILLED live or paper fill is logged once, tagged orders, with its mode."""
from types import SimpleNamespace

import pytest

from backend.api.algo import fill_notify


def _row(**over):
    base = dict(
        id=101, account="ZG0790", symbol="NIFTY26OCT25000CE", exchange="NFO",
        transaction_type="BUY", quantity=75, fill_price=112.5,
        product="NRML", mode="live", engine="live", agent_id=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def logged(monkeypatch):
    calls = []
    monkeypatch.setattr(fill_notify.logger, "info", lambda msg, **kw: calls.append((msg, kw)))
    return calls


@pytest.mark.asyncio
async def test_live_fill_is_logged_once_with_tags_and_fields(logged):
    await fill_notify.notify_fills([_row()])
    assert len(logged) == 1
    msg, kw = logged[0]
    assert msg == "order filled"
    extra = kw["extra"]
    # "alert" (2026-10) — additive tag surfacing this record in
    # /admin/alerts/history alongside real AgentEvent rows.
    assert extra["tags"] == ["orders", "alert"]
    assert extra["alert_event"] == "filled"
    assert extra["mode"] == "live"
    assert extra["order_id"] == 101
    assert extra["account"] == "ZG0790"
    assert extra["symbol"] == "NIFTY26OCT25000CE"
    assert extra["fill_price"] == 112.5


@pytest.mark.asyncio
async def test_paper_fill_is_logged_with_its_mode_so_agents_can_skip_it(logged):
    await fill_notify.notify_fills([_row(mode="paper")])
    assert logged[0][1]["extra"]["mode"] == "paper"


@pytest.mark.asyncio
async def test_each_row_gets_its_own_record(logged):
    await fill_notify.notify_fills([_row(id=1), _row(id=2)])
    assert [kw["extra"]["order_id"] for _, kw in logged] == [1, 2]


@pytest.mark.asyncio
async def test_a_bad_row_does_not_stop_the_others(monkeypatch, logged):
    await fill_notify.notify_fills([SimpleNamespace(id=5), _row(id=6)])
    assert [kw["extra"]["order_id"] for _, kw in logged] == [6]


def test_format_uses_the_given_time_in_ist():
    # 2026-10: upgraded from a bare IST-only stamp to the dual-tz (IST + ET)
    # format shared with every other alert channel (format_dual_tz) — same
    # SSOT as timestamp_display(), just fed the caller's own `when` instead
    # of the clock. Seconds are dropped by that formatter; assert on the
    # IST wall-clock minute (unambiguous from the UTC input) and that an ET
    # zone abbreviation is present, rather than a brittle exact string.
    #
    # 2026-10 audit fix: the timestamp now lives on the header's own
    # second line (no more standalone "Time:" label — the shared
    # `format_notification_header` owns it), so this asserts on the
    # dual-tz content directly rather than a "Time: " prefix.
    from datetime import datetime, timezone
    title, body = fill_notify.format_fill_message(
        _row(), when=datetime(2026, 10, 6, 4, 45, 30, tzinfo=timezone.utc))
    assert title == "Order filled: BUY 75 NIFTY26OCT25000CE"
    assert "10:15 IST" in body
    assert ("EDT" in body) or ("EST" in body)


def test_body_contains_the_title_text_not_just_the_title_slot():
    """2026-11-05 live operator report: "order filled alert is not
    showing order filled text anywhere in alert text". `title` used to
    reach Telegram only via `_send_telegram_html`'s old
    `tg or f"{title}\\n{body}"` fallback — once `_render_fill`
    (event_agents.py) started supplying `tg=body` explicitly (closing a
    separate "agent label not at top" bug the same day), `title` never
    reached Telegram at all, and the word "filled" disappeared from the
    alert entirely. `body` must now carry a "filled" summary line as an
    explicit line — ntfy (which always reads `body` directly, never
    `title` for its own message) had this exact same gap even before
    the other fix, just never reported.

    Amended same day — operator: "the symbol [is shown] multiple times
    in two different places. remove symbol just after order filled
    text." The body-embedded summary line is now symbol-free (side+qty
    only); the symbol still appears exactly once, on the trade-detail
    line below it. `title` itself (the separate tuple element, used for
    ntfy's own notification-title slot) is unchanged and still carries
    the symbol — so `title` as a whole string no longer appears
    verbatim inside `body`, only its "Order filled: {side} {qty}" prefix
    does."""
    row = _row()
    title, body = fill_notify.format_fill_message(row)
    side = str(row.transaction_type or "").upper()
    qty = int(row.quantity or 0)
    summary = f"Order filled: {side} {qty}"
    assert summary in body, f"summary line must appear inside body — got body: {body!r}"
    assert "filled" in body.lower()
    assert row.symbol in title
    # The symbol must appear in body exactly once (the trade-detail
    # line), never duplicated on the "Order filled:" summary line.
    assert body.count(row.symbol) == 1, f"symbol must appear exactly once in body — got body: {body!r}"
    # Origin line must still be the FIRST line (the other 2026-11-05 fix) —
    # the summary line goes right after it, not ahead of it.
    assert body.splitlines()[0].startswith("Agent: ")
    assert body.splitlines()[1] != body.splitlines()[0]


def test_fill_message_shows_masked_account():
    # P1 fix (2026-10 audit): the account was previously shown RAW/
    # unmasked — this is the HIGHEST-VOLUME live notification (every
    # fill, Telegram+ntfy). Now masked via the same mask_account()
    # helper every other correctly-masked alert path already uses.
    title, body = fill_notify.format_fill_message(_row(account="ZG0790"))
    assert "Account: ZG0790" not in body
    assert "Account: ZG####" in body


def test_fill_message_shows_agent_name_as_origin_matching_agents_log_chip():
    # 2026-10-17 fix — origin now reflects WHO/WHAT triggered the order
    # (the originating agent's name, resolved via AlgoOrder.agent_id),
    # the same signal LogPanel.svelte's `log-agent-chip` already shows —
    # NOT AlgoOrder.engine (execution mode: live/sim/paper/replay/shadow/
    # expiry), which a manually-placed LIVE order sets to "live", never
    # the literal string "manual" (see orders_place.py).
    #
    # 2026-10 audit fix: the ad-hoc "Source: {NAME}" line (uppercased)
    # is replaced by the shared `format_notification_header`'s origin
    # line — now the message's FIRST line, in natural case (not
    # uppercased), matching every other alert renderer in the app.
    _, body = fill_notify.format_fill_message(_row(agent_name="Chase close positions"))
    assert body.splitlines()[0] == "Agent: Chase close positions"
    _, body = fill_notify.format_fill_message(_row(agent_name="Manual operator order"))
    assert body.splitlines()[0] == "Agent: manual"


def test_fill_message_defaults_origin_to_manual_when_agent_name_missing():
    # A row that somehow arrives without agent_name resolved (e.g. the
    # agent row was deleted, or agent_id was NULL) still gets a sensible
    # origin line rather than silently omitting one.
    row = SimpleNamespace(id=101, account="ZG0790", symbol="NIFTY26OCT25000CE",
                           exchange="NFO", transaction_type="BUY", quantity=75,
                           fill_price=112.5, product="NRML")
    _, body = fill_notify.format_fill_message(row)
    assert body.splitlines()[0] == "Agent: manual"


@pytest.mark.asyncio
async def test_engine_is_forwarded_into_the_log_extra(logged):
    await fill_notify.notify_fills([_row(engine="manual")])
    assert logged[0][1]["extra"]["engine"] == "manual"


@pytest.mark.asyncio
async def test_agent_name_is_resolved_and_forwarded_into_the_log_extra(monkeypatch, logged):
    from unittest.mock import AsyncMock, patch, MagicMock

    fake_result = MagicMock()
    fake_result.all = MagicMock(return_value=[(42, "Chase close positions")])
    fake_session = AsyncMock()
    fake_session.execute = AsyncMock(return_value=fake_result)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch.object(fill_notify, "async_session", side_effect=lambda: ctx):
        await fill_notify.notify_fills([_row(agent_id=42)])
    assert logged[0][1]["extra"]["agent_name"] == "Chase close positions"


@pytest.mark.asyncio
async def test_no_agent_ids_skips_the_db_query_entirely(logged):
    # Every row in the batch has agent_id=None (the common manual-order
    # case) — _resolve_agent_names must short-circuit before touching the
    # DB, not issue a WHERE id IN () query.
    await fill_notify.notify_fills([_row(agent_id=None)])
    assert logged[0][1]["extra"]["agent_name"] is None


@pytest.mark.asyncio
async def test_agent_id_rides_along_even_when_name_lookup_fails(monkeypatch, logged):
    """2026-11-08 audit fix (item 8) regression guard. A real agent's
    order (agent_id=42) whose name lookup fails (DB error -> _resolve_
    agent_names returns {}) must log `agent_id` alongside the resulting
    `agent_name=None`, not just the name. Before the fix, `extra` never
    carried `agent_id` at all, so `_render_fill`/`resolve_origin` read a
    row with NEITHER key and rendered the false "manual" for a genuinely
    real agent whose name simply failed to resolve."""
    from unittest.mock import patch

    with patch.object(fill_notify, "_resolve_agent_names", return_value={}):
        await fill_notify.notify_fills([_row(agent_id=42)])
    extra = logged[0][1]["extra"]
    assert extra["agent_id"] == 42
    assert extra["agent_name"] is None

    # End-to-end: the renderer must resolve this to "Agent #42", never "manual".
    from backend.api.algo import event_agents as ea

    rec = {"extra": extra, "ts": None}
    _title, body, _tg = ea._render_fill(rec)
    assert "Agent #42" in body
    assert "manual" not in body.lower()
