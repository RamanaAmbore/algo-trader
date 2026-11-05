"""Event agents: each new log record is matched at once and sent without cooldown, latch, or cycle.

An event agent is an agents row with kind='event'. Its conditions are a log leaf, its
events list names the channels, and its actions carry a render key that picks the message
format. Only the API process dispatches, so each record is sent once.
"""
import asyncio
import html
import sys
import time
from datetime import timezone
from types import SimpleNamespace

from sqlalchemy import select

from backend.api.algo import alert_routing, fill_notify
from backend.api.algo.agent_evaluator import Context, evaluate
from backend.shared.helpers.error_alerts import RepeatGate, clean_message
from backend.shared.helpers.ramboq_logger import get_logger

# Logger name deliberately pinned to "backend.api.algo.events" (NOT this
# module's own __name__) — the "log" channel's send function below is a
# direct port of events.py's old _log_channel, and every LogEvent row it
# writes must keep showing up under that same logger name so stored rows
# look identical to what they did before the Sprint 6 move.
_EVENTS_LOGGER = get_logger("backend.api.algo.events")

# This module's OWN logger (`backend.api.algo.event_agents`) — used ONLY
# for the per-record dispatch trace (council item 4), a pure DEBUG
# diagnostic that must never itself reach a real channel. This exact
# module name is one of `log_store.py`'s own `_SKIP_PREFIXES`, so
# anything logged via `_logger` is structurally guaranteed to never be
# captured by `LogStoreHandler` — correct for a diagnostic, but it is
# NOT used for the catch-all self-alert (council item 5) below: that
# alert's whole point is to reach a real operator-visible channel
# (Telegram/ntfy/email), which a skip-listed logger can never do (2026-10
# fix — the first version of this code logged the self-alert via
# `_logger` too, which meant it could never fire a real notification,
# only ever reach stderr — exactly the "silent" failure mode item 5 was
# written to prevent). See `_emit_unrouted_self_alert()`'s docstring for
# where that alert is actually logged, and `_UNROUTED_ALERT_EVENT` for
# this module's real recursion guard.
_logger = get_logger(__name__)

_CACHE_S = 60.0
_cache: dict = {"at": float("-inf"), "agents": []}


def _render_fill(rec: dict) -> tuple[str, str]:
    """Order fill notification — side, qty, symbol, fill price."""
    extra = rec.get("extra") or {}
    row = SimpleNamespace(
        id=extra.get("order_id"), account=extra.get("account"), symbol=extra.get("symbol"),
        exchange=extra.get("exchange"), transaction_type=extra.get("transaction_type"),
        quantity=extra.get("quantity"), fill_price=extra.get("fill_price"), product=extra.get("product"),
        engine=extra.get("engine"), agent_name=extra.get("agent_name"),
    )
    when = rec.get("ts")
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return fill_notify.format_fill_message(row, when=when)


RENDERS: dict = {}
_unknown_renders: set[str] = set()
_gate = None


def _get_gate():
    global _gate
    if _gate is None:
        from backend.shared.helpers.alert_utils import _get_redis
        from backend.shared.helpers.error_alerts import SharedRepeatGate
        client = _get_redis()
        _gate = SharedRepeatGate(client) if client is not None else RepeatGate()
    return _gate


def _render_chase_cancel(rec: dict) -> tuple[str, str]:
    """Chase cancel-and-replace could not confirm the cancel landed."""
    from backend.shared.helpers.utils import mask_account_in_text
    from backend.shared.helpers.date_time_utils import timestamp_display
    from backend.shared.helpers.alert_utils import format_notification_header
    x = rec.get("extra") or {}
    # 2026-10 audit fix: the prior wording
    # "after attempt {attempt}/{quantity-remaining_qty} filled." reads
    # like a fraction (attempt N of M attempts), but the denominator is
    # actually "contracts already filled so far" — an unrelated number.
    # Spelled out unambiguously below.
    filled_so_far = x.get('quantity', 0) - x.get('remaining_qty', 0)
    # 2026-10 audit fix: origin header leads every alert (order-scoped —
    # `agent_id` threaded from the chase loop's own AlgoOrder context,
    # "manual" when the chased order was placed by a human).
    origin_header = format_notification_header(None, x.get('agent_id'), ist_display=timestamp_display())
    body = (
        f"{origin_header}\n"
        f"{x.get('transaction_type')} {x.get('symbol')} — cancel of order {x.get('order_id')} "
        f"on {x.get('account')} could not be confirmed after attempt "
        f"{x.get('attempt')} ({filled_so_far} contracts filled so far). "
        f"The chase has been ABORTED without placing a replacement order. "
        f"Manually verify the broker's order book — the old order may still be live."
    )
    return "Chase cancel unconfirmed — possible resting duplicate order", mask_account_in_text(body)


def _render_partial_gtt(rec: dict) -> tuple[str, str]:
    """Only some of a parent order's planned GTTs were placed."""
    from backend.shared.helpers.date_time_utils import timestamp_display
    from backend.shared.helpers.alert_utils import format_notification_header
    from backend.shared.helpers.utils import mask_account_in_text
    x = rec.get("extra") or {}
    # 2026-10 audit fix: origin header leads every alert.
    origin_header = format_notification_header(None, x.get('agent_id'), ist_display=timestamp_display())
    # 2026-10 notification-masking audit: `errors` can carry a raw account
    # code (e.g. a broker-resolution KeyError text) — scrub as
    # defense-in-depth, matching `order_failure_messages()`'s pattern.
    errors_text = mask_account_in_text('; '.join(x.get('errors') or [])) or ""
    body = (
        f"{origin_header}\n"
        f"parent #{x.get('parent_row_id')} {x.get('parent_symbol')}: "
        f"{x.get('placed')}/{x.get('planned')} GTTs placed. "
        f"Errors: {errors_text}"
    )
    return "Partial GTT placement", body


def _render_template_attach(rec: dict) -> tuple[str, str]:
    """Template exit attach outcome — unprotected position, wing skip/reject.

    2026-10 audit fix: leads with `format_notification_header()` (origin
    + dual-tz timestamp) — this renderer previously had NO origin line at
    all, and appended a bare trailing timestamp instead; both are now
    fixed, matching every other order-scoped renderer in this file.
    `agent_id` is read from `x.get('agent_id')`, now populated for all
    four branches (see the `template_attach.py` call sites: wing_hard_reject
    threads `parent_agent_id` through the wing-scan helper chain;
    wing_unprotected reads `result.plan.parent_agent_id`)."""
    from backend.shared.helpers.date_time_utils import timestamp_display
    from backend.shared.helpers.utils import mask_account_in_text
    from backend.shared.helpers.alert_utils import format_notification_header
    x = rec.get("extra") or {}
    ev = x.get("alert_event")
    ts = timestamp_display()
    header = format_notification_header(None, x.get('agent_id'), ist_display=ts)
    # 2026-10 notification-masking audit: `reason` is free-caller text that
    # can embed a raw broker account code — scrub as defense-in-depth,
    # matching `order_failure_messages()`'s existing pattern.
    reason = mask_account_in_text(str(x.get('reason'))) if x.get('reason') is not None else x.get('reason')
    if ev == "wing_unprotected":
        return ("Unprotected SELL position",
                f"{header}\n"
                f"GTTs placed (ids: {x['gtt_ids_text']}) but wing failed: {reason} | "
                f"order #{x['parent_order_id']} {x['symbol']} {x['exchange']}")
    if ev == "wing_hard_reject":
        return ("Wing scan hard-rejected",
                f"{header}\n{reason} | {x['symbol']} {x['exchange']} target ₹{x['target_premium']:.2f}")
    if ev == "wing_skip":
        return ("Wing attach skipped",
                f"{header}\n{reason} | order #{x['parent_order_id']} {x['symbol']} {x['exchange']}")
    if ev == "wing_offset_skip":
        return ("Wing offset attach skipped",
                f"{header}\n{reason} | order #{x['parent_order_id']} {x['symbol']} {x['exchange']}")
    # Fallback dumps the whole `extra` dict raw — may embed an `account`
    # key or an account-shaped code inside a nested value; scrub the
    # stringified dump too.
    return "Template attach", f"{header}\n{mask_account_in_text(f'{x}')}"


def _render_order_failure(rec: dict) -> tuple:
    """A live order was rejected or failed to place.

    2026-10 plain-text reversal: `order_failure_messages()` now builds a
    tag-free `tg_body`, so it is used directly for BOTH the ntfy and
    telegram slots — `_html_to_plain` is no longer called here. Running
    it on already-plain text would wrongly strip any literal `<...>` the
    raw broker error text happens to contain (e.g. "Insufficient funds
    <x>" from a broker's own rejection message)."""
    from backend.shared.helpers.alert_utils import order_failure_messages
    x = rec.get("extra") or {}
    tg_body, subject, email_body = order_failure_messages(
        masked=x["masked"], symbol=x["symbol"], exchange=x["exchange"], side=x["side"],
        qty=x["qty"], mode=x["mode"], source=x["source"], error=x["error"],
        suppressed_count=x["suppressed_count"], ist_disp=x["ist_disp"],
        agent_id=x.get("agent_id"),
    )
    return f"Order Rejected: {x['symbol']} {x['side']}", tg_body, tg_body, (subject, email_body)


def _render_template_guard(rec: dict) -> tuple:
    """A template exit guard fired (held/released/blocked an exit).

    4th element (email subject, body) added 2026-10 audit fix —
    TEMPLATE_GUARD_AGENT now carries an `email` channel per
    `template_attach.py:_fire_guard_alert`'s own docstring promise
    ("Email lands in the alert inbox — durable record for end-of-day
    review"), which previously had no renderer support for it at all.

    2026-10 plain-text reversal: `template_guard_message()` now returns
    plain text (no `<b>`/`<code>`), used directly for ntfy/telegram with
    no `_html_to_plain` pass. The email body can no longer reuse `tg`
    verbatim inside `<html><body>` — a bare `\\n` doesn't break a line in
    HTML, and a raw `<` in `reason` (e.g. "qty < lot_size") would corrupt
    the markup — so it wraps the ESCAPED plain text in `<pre>`, preserving
    both the line breaks and content.
    """
    from backend.api.algo.template_attach import template_guard_message
    x = rec.get("extra") or {}
    keys = ("template_slug", "applies_to", "reason", "parent_order_id", "parent_side", "parent_qty",
            "parent_symbol", "parent_fill_price", "parent_account", "ist_label", "agent_id")
    tg = template_guard_message(**{k: x.get(k) for k in keys})
    title = "Template guard fired"
    email_subject = f"RamboQuant {title}: {x.get('parent_symbol') or ''}"
    email_body = (
        "<html><body style='font-family:sans-serif'>"
        f"<pre style='white-space:pre-wrap;font-family:monospace'>{html.escape(tg)}</pre>"
        "</body></html>"
    )
    return title, tg, tg, (email_subject, email_body)


def _render_attach_fail(rec: dict) -> tuple:
    """A template's exit GTTs/wing failed to attach to the filled parent.

    4th element (email subject, body) added 2026-10 audit fix — see
    `_render_template_guard`'s docstring for the rationale (same
    "durable record" promise, same prior gap).

    2026-10 plain-text reversal — same shape as `_render_template_guard`:
    `template_attach_fail_message()` returns plain text, used directly
    for ntfy/telegram, and the email body wraps the escaped plain text
    in `<pre>` instead of reusing the old `<code>`-tagged HTML string."""
    from backend.api.algo.template_attach import template_attach_fail_message
    x = rec.get("extra") or {}
    tg = template_attach_fail_message(order_id=x.get("order_id"), symbol=x.get("symbol"),
                                      account=x.get("account"), err_summary=x.get("err_summary") or "",
                                      ist_label=x.get("ist_label"), agent_id=x.get("agent_id"),
                                      placed_untracked_ids=x.get("placed_untracked_ids"))
    title = "Template attach failed"
    email_subject = f"RamboQuant {title}: {x.get('symbol') or ''}"
    email_body = (
        "<html><body style='font-family:sans-serif'>"
        f"<pre style='white-space:pre-wrap;font-family:monospace'>{html.escape(tg)}</pre>"
        "</body></html>"
    )
    return title, tg, tg, (email_subject, email_body)


def _render_order_correction(rec: dict) -> tuple[str, str]:
    """2026-10-23 order-poll consolidation — a background corrector
    (the open-order watchdog, the periodic order-poll sweep, or
    postback's non-blocking follow-up verification) found and fixed a
    stuck AlgoOrder row. This firing is itself the signal that some
    live path already failed to do its job on its own — see
    `backend/api/algo/order_poll.py`'s module docstring for exactly
    which callers fire it (deliberately NOT `list_active_chases` or
    the operator-triggered admin reconcile routes).

    Leads with `format_notification_header()` (2026-10-23 "every alert
    leads with its origin" invariant — same convention every other
    order-scoped renderer in this file uses, e.g. `_render_order_failure`
    via `order_failure_messages`) so this renderer doesn't silently skip
    the origin line every other renderer carries. `agent_id` comes from
    the corrected AlgoOrder row itself (`order_poll.py:_opo_postprocess`'s
    `static_agent_id`) — `None` resolves to "manual" via `resolve_origin`,
    same as any other agent-less order."""
    from backend.shared.helpers.alert_utils import format_notification_header
    x = rec.get("extra") or {}
    header = format_notification_header(None, x.get("agent_id"))
    title = f"Order corrected: {x.get('symbol') or ''} #{x.get('algo_order_id')}"
    body = (
        f"{header}\n"
        f"AlgoOrder #{x.get('algo_order_id')} {x.get('masked') or ''} "
        f"{x.get('symbol') or ''} {x.get('exchange') or ''}: "
        f"{x.get('prior_status')} -> {x.get('new_status')} "
        f"(source={x.get('source') or 'order-poll'})"
    )
    return title, body


def _render_mcp_ping(rec: dict) -> tuple[str, str, str]:
    """An MCP tool call performed an audited action (place/cancel/modify/...).

    2026-10 plain-text reversal: `extra["tg"]` is now plain text, built
    once (origin header + body) at the single `lab.py:_mcp_ping()`
    chokepoint every MCP-ping call site funnels through — no
    `_html_to_plain` pass here, since there is no longer any markup to
    decode and a raw `<`/`>` in the body (unlikely, but not impossible)
    would otherwise be silently stripped."""
    tg = (rec.get("extra") or {}).get("tg") or ""
    return "MCP", tg, tg


def _render_deploy_sync(rec: dict) -> tuple[str, str, str]:
    """dev/main branches or their deploy webhooks have drifted out of sync.

    Also registered as the "generic_notice" renderer (see
    `register_renderer("generic_notice")` below) — this function was
    already a pure (title, body) passthrough with no deploy-sync-
    specific logic, so it's reused as-is for the 2026-10 audit fix that
    migrated 5 background.py alert call sites (gtt_asymmetric,
    oco_double_fire, ticker_recovered, ticker_degraded, visitor_report)
    off the deleted `alert_utils._alert_route()` dispatcher onto the
    log-record + event-agent pipeline every other alert in this codebase
    already uses. Kept under its original "deploy_sync" render key too
    so DEPLOY_SYNC_AGENT's own behaviour is unchanged.

    2026-10-23 fix — raw HTML tags leaking into Telegram/ntfy: the
    `extra["body"]` these 5 call sites supply is sometimes genuinely
    HTML-formatted (e.g. `background.py`'s visitor-report send builds
    `f"<b>Visitors</b>\\n{tg_body}"`), but this renderer used to pass it
    straight through as the plain-text `body` tuple slot with no `tg`
    (HTML) slot at all. `_send_channel` (`out[1]`=body, `out[2]`=tg)
    then fed that raw-HTML string into `_send_telegram_info_html`'s
    `tg or f"<b>{html.escape(title)}</b>\\n{html.escape(body)}"`
    fallback — since `tg` was always `None` here, `html.escape(body)`
    double-escaped the already-literal `<b>` tags into `&lt;b&gt;`,
    which Telegram then displayed as literal angle-bracket text instead
    of bold. ntfy (which reads `body` directly, never `tg`) would show
    the same raw tags verbatim for any future generic_notice caller that
    embeds HTML, even though no current call site triggers it on that
    channel.

    2026-10 plain-text reversal (supersedes the `_html_to_plain` fix
    described above) — every real producer (gtt_asymmetric,
    oco_double_fire, ticker_recovered, ticker_degraded,
    deploy_out_of_sync, visitor_report, user_registered, the unrouted
    catch-all) now builds `extra["body"]` as plain text; visitor_report's
    `<b>Visitors...</b>` wrap and `routes/auth.py`'s `html.escape()`'d
    username/email were the last two HTML-shaped producers and are fixed
    at the source. `body` is used directly for both slots — no
    `_html_to_plain` pass, since running that tag-stripping regex over
    text that is genuinely plain (not just "no tags today") risks
    deleting a literal `<`/`>` in real data.
    """
    x = rec.get("extra") or {}
    title = x.get("title") or "Deploy out of sync"
    body = x.get("body") or ""
    # 2026-11-03, operator: "every alert should be generated by an
    # agent... format... driven by tags" — origin line, same as every
    # other renderer. Only added when the producer actually resolved a
    # real identity (`generic_notice_identity_extra()`, called from each
    # of the 6 `background.py` producers before logging) — NOT built
    # unconditionally via `format_notification_header(None, None)`,
    # which would render "Agent: manual" here. "Manual" means "a human
    # placed this order", a category that doesn't apply to a system/
    # infra alert like ticker-degraded or deploy-out-of-sync; a missing
    # identity stays header-less (the pre-existing behaviour) rather
    # than showing a misleading label.
    agent_name, agent_id = x.get("agent_name"), x.get("agent_id")
    if agent_name and agent_id is not None:
        from backend.shared.helpers.alert_utils import format_notification_header
        origin_header = format_notification_header(agent_name, agent_id)
        body = f"{origin_header}\n{body}"
    return title, body, body


def _render_rich_alert(rec: dict) -> tuple:
    """A threshold ('cycle') agent fired — full rich alert table.

    2026-10 plain-text reversal: `dispatch_payload()`'s `telegram_msg` is
    now plain text, used directly for both slots — no `_html_to_plain`.

    `replay_mode` (2026-10-31, operator: "the alerts should show paper
    and simulation mode explicitly ... wire it") — now also read from
    extras and passed through, so a replay-fired rich alert gets the
    [REPLAY] tag + warning banner dispatch_payload now derives, not
    just the mode_tag this call site already pre-computed.
    """
    from backend.shared.helpers.alert_utils import dispatch_payload
    x = rec.get("extra") or {}
    p = dispatch_payload("alert", x["ist_display"], x["tg_table"], x["email_table_html"],
                         x["subject_detail"], sim_mode=bool(x.get("sim_mode")),
                         mode_tag=x.get("mode_tag") or "",
                         agent_name=x.get("agent_name"), agent_id=x.get("agent_id"),
                         replay_mode=bool(x.get("replay_mode")))
    return p["title"], p["telegram_msg"], p["telegram_msg"], (p["email_subject"], p["email_html"])


def _render_summary(rec: dict) -> tuple:
    """Open/close market performance summary. Plain text (2026-10
    reversal) — see `_render_rich_alert`'s docstring.

    2026-10-31 fix (operator: "the alerts should show paper and
    simulation mode explicitly ... base code for all alerts") — used to
    hardcode `sim_mode=False, mode_tag=""` unconditionally, so a summary
    fired by the `send_summary` DSL action inside a sim/replay cycle
    (background.py's own scheduled summary never sets these — always a
    real market-hours cron task) carried no distinguishing tag at all.
    Now reads the real flags `send_summary()` writes into the record's
    extras and lets `dispatch_payload` derive the tag/banner itself.

    2026-11-03 fix (operator: "every alert should be generated by an
    agent... format... driven by tags") — used to call `dispatch_payload`
    with no `agent_name`/`agent_id` at all (CLAUDE.md's old "Deliberate
    exception — agent-less summaries"), so this was the one alert type
    with no "Alert: {name} (#{id})" header. Now reads the real identity
    `send_summary()` writes into the record's extras — the market-close
    cron task's own resolved `market-summary` agent id, or the real
    firing agent's identity when sent via the `send_summary` DSL action
    — mirroring `_render_rich_alert`'s own `agent_name`/`agent_id`
    threading exactly. `None`/`None` (no identity resolved) degrades to
    the pre-existing header-less rendering, same as before this fix.
    """
    from backend.shared.helpers.alert_utils import dispatch_payload
    x = rec.get("extra") or {}
    p = dispatch_payload(x["msg_type"], x["ist_display"], x["tg_table"], x["email_table_html"],
                         x["subject_detail"], sim_mode=bool(x.get("sim_mode")),
                         replay_mode=bool(x.get("replay_mode")),
                         agent_name=x.get("agent_name"), agent_id=x.get("agent_id"))
    return p["title"], p["telegram_msg"], p["telegram_msg"], (p["email_subject"], p["email_html"])


def _render_breach(rec: dict) -> tuple:
    """An agent-engine cycle took longer than its breach threshold."""
    x = rec.get("extra") or {}
    return (x.get("agent_name") or "", x.get("ntfy_body") or "", x.get("telegram_body") or "",
            (x.get("email_subject") or "", x.get("email_body") or ""), list(x.get("channels") or []))


def _render_market_status(rec: dict) -> tuple:
    """Daily 05:35 IST NSE/MCX market open-or-closed status report
    (2026-11-03 log+agent conversion of `send_market_status`).

    Plain `(title, body, tg)` passthrough, same shape as
    `_render_deploy_sync` — 2026-11-05 simplification: this used to also
    return a per-record `out[4]` channel list, because Telegram-always/
    ntfy-if-open was content-conditional and `alert_routing.py`'s static
    per-agent resolution couldn't express it. The 2026-11-04 channel-
    routing audit removed that conditionality (the same fixed set —
    log, telegram, email — fires every day now), so
    `MARKET_STATUS_ALERT_AGENT`'s own static `events` list is the SSOT
    for channels again, resolved the normal way by `_dispatch_agent()`.
    Returning a plain 3-tuple (not a 5-tuple with an empty channels
    list) matters: `_dispatch_agent` reads `out[4]` whenever
    `len(out) > 4`, so a stray empty list there would have suppressed
    every channel instead of falling back to `agent_channels`.
    """
    x = rec.get("extra") or {}
    title = x.get("title") or "Market Status"
    body = x.get("body") or ""
    agent_name, agent_id = x.get("agent_name"), x.get("agent_id")
    if agent_name and agent_id is not None:
        from backend.shared.helpers.alert_utils import format_notification_header
        origin_header = format_notification_header(agent_name, agent_id)
        body = f"{origin_header}\n{body}"
    return title, body, body


def _render_action_notify(rec: dict) -> tuple:
    """A DO-clause action's own declared `notify=[...]` channels
    (2026-11-03 log+agent conversion of the `notify=` action param —
    `actions.py:_al_maybe_notify_action`).

    Plain `(title, body, tg)` passthrough, no separate origin header —
    `body` (`full_message`, built by `_al_notify_build_message`)
    already leads with the firing agent's own identity via
    `format_notification_header()` (2026-11-04 alert-format audit fix
    — was the bespoke `"Agent [{agent_key}] action: ..."` shape;
    adding a second `format_notification_header()` call here on top
    would just redundantly repeat the identity line this message's
    first line already is). Own `out[4]` channels list — this action's
    per-channel selection (e.g. ntfy's `priority`) is resolved entirely
    at the producer (`_al_notify_channels_for_log`), never by
    `alert_routing.py`'s static per-agent resolution, same reasoning as
    `_render_market_status` above.
    """
    x = rec.get("extra") or {}
    title = x.get("title") or "Agent action"
    body = x.get("body") or ""
    return title, body, body, None, list(x.get("channels") or [])


def _render_order_cancelled(rec: dict) -> tuple:
    """A postback CANCELLED alert (2026-11-03 log+agent conversion of
    `orders.py:_opp_send_cancel_alert` — Wave 3, final wave, of the
    alert-agent audit).

    This alert is ORDER-scoped — "who placed this order: manual or
    which agent" is always a meaningful question here (unlike
    `_render_market_status`/`_render_deploy_sync`/`_render_action_
    notify`'s genuinely agent-less system/infra events) — so, matching
    `_render_chase_cancel`/`_render_gtt_not_accepted`'s own
    unconditional pattern, `format_notification_header()` is called
    EVERY time, never gated on identity being present:
    `resolve_origin()` already degrades a `None` agent_id to the
    literal "Agent: manual" line, the system-wide invariant that an
    order alert's origin is never silently omitted (2026-10 operator
    requirement: "make sure it has agent info in the beginning... if
    it is not by agent, add manual"). Own `out[4]` channels list —
    this alert's base channel (ntfy) is deliberately narrow by itself;
    the real routing (urgent ntfy + forced email, or dropped entirely)
    comes from the SAME `extra['action_required']` binary gate
    `_apply_action_required_ntfy_gate`/`_apply_action_required_email_
    gate` already apply to every other action-required alert in this
    codebase.
    """
    from backend.shared.helpers.alert_utils import format_notification_header
    x = rec.get("extra") or {}
    title = x.get("title") or "Order cancelled"
    origin_header = format_notification_header(x.get("agent_name"), x.get("agent_id"))
    body = f"{origin_header}\n{x.get('body') or ''}"
    return title, body, body, None, list(x.get("channels") or [])


def _render_gtt_not_accepted(rec: dict) -> tuple[str, str]:
    """A placed GTT was not accepted by the broker (status mismatch)."""
    from backend.shared.helpers.date_time_utils import timestamp_display
    from backend.shared.helpers.alert_utils import format_notification_header
    from backend.shared.helpers.utils import mask_account_in_text
    x = rec.get("extra") or {}
    # 2026-10 audit fix: origin header leads every alert.
    origin_header = format_notification_header(None, x.get('agent_id'), ist_display=timestamp_display())
    # 2026-10 notification-masking audit: `reason` is free-caller text that
    # can embed a raw broker account code — scrub as defense-in-depth.
    reason = mask_account_in_text(str(x.get('reason'))) if x.get('reason') is not None else x.get('reason')
    return ("GTT not accepted at broker",
            f"{origin_header}\n"
            f"{x.get('symbol')} {x.get('label')} GTT {x.get('gtt_id')}: {reason}")


def _render_error(rec: dict) -> tuple[str, str, str]:
    """An ERROR+ log line was written somewhere in the app.

    2026-10 audit fix: this renderer is NOT order-scoped (a generic
    ERROR+ log line, from anywhere in the app, rarely carries an
    `agent_id` in its `extra` dict) — so the origin header below almost
    always resolves to "Agent: manual". That's an accepted, known
    imprecision ("no identifiable originating agent" is the honest
    answer for a generic error log), not a claim that the error was
    literally caused by a manual order. Any future caller that DOES
    attach a real `agent_id` to an error-tagged log record gets the
    real name for free.
    """
    from backend.shared.helpers.utils import mask_account_in_text
    from backend.shared.helpers.date_time_utils import timestamp_display
    from backend.shared.helpers.alert_utils import format_notification_header
    x = rec.get("extra") or {}
    name = rec.get("logger") or ""
    msg = clean_message(mask_account_in_text(rec.get("message") or "") or "")
    repeats = rec.get("repeats") or 0
    suffix = f" (+{repeats} repeats)" if repeats else ""
    ts = timestamp_display()
    origin_line = format_notification_header(None, x.get("agent_id"), ist_display=ts).splitlines()[0]
    # ntfy already shows "RamboQuant error" as its separate Title header
    # (passed through below), so its body omits the redundant line — same
    # shape as before this fix. Telegram has no separate title mechanism,
    # so its body keeps that line, now as plain text (2026-10 reversal —
    # no `<b>`/`<code>` tags, no `html.escape()`).
    ntfy_body = f"{origin_line}\n{name}\n{msg}{suffix}\n{ts}"
    tg_body = f"{origin_line}\nRamboQuant error\n{name}\n{msg}{suffix}\n{ts}"
    return "RamboQuant error", ntfy_body, tg_body


def register_renderer(name: str):
    def deco(fn):
        RENDERS[name] = fn
        return fn
    return deco


register_renderer("fill")(_render_fill)
register_renderer("error")(_render_error)
register_renderer("chase_cancel")(_render_chase_cancel)
register_renderer("partial_gtt")(_render_partial_gtt)
register_renderer("template_attach")(_render_template_attach)
register_renderer("order_failure")(_render_order_failure)
register_renderer("template_guard")(_render_template_guard)
register_renderer("template_attach_fail")(_render_attach_fail)
register_renderer("mcp_ping")(_render_mcp_ping)
register_renderer("deploy_sync")(_render_deploy_sync)
register_renderer("generic_notice")(_render_deploy_sync)
register_renderer("rich_alert")(_render_rich_alert)
register_renderer("summary")(_render_summary)
register_renderer("breach")(_render_breach)
register_renderer("market_status")(_render_market_status)
register_renderer("action_notify")(_render_action_notify)
register_renderer("order_cancelled")(_render_order_cancelled)
register_renderer("gtt_not_accepted")(_render_gtt_not_accepted)
register_renderer("order_correction")(_render_order_correction)


# `_send_ntfy` / `_send_telegram_html` RELOCATED to
# backend/api/algo/grammar_tokens/channel_ntfy.py /
# grammar_tokens/channel_telegram.py (Wave 1, DSL-token-unification plan,
# Part B — channels). Imported immediately below so `CHANNELS` captures
# the identical function objects, and so every existing
# `patch.object(event_agents, "_send_ntfy", ...)` /
# `event_agents._send_telegram_html` call site is unchanged.
from backend.api.algo.grammar_tokens.channel_ntfy import _send_ntfy  # noqa: E402
from backend.api.algo.grammar_tokens.channel_telegram import _send_telegram_html  # noqa: E402


def _render_key(agent) -> str | None:
    for action in agent.actions or []:
        if isinstance(action, dict) and action.get("type") == "render":
            return action.get("render")
    return None


def _channel_enabled(capability: str | None) -> bool:
    if capability is None:
        return True
    from backend.shared.helpers.utils import is_enabled
    return is_enabled(capability)


def resolve_email_recipients(recipients) -> list[str]:
    """'alert' (default) is the current alert list. A list is explicit addresses; anything without '@' is dropped."""
    from backend.shared.helpers.alert_utils import get_alert_recipients
    if recipients in (None, "alert"):
        return list(get_alert_recipients())
    if isinstance(recipients, (list, tuple)):
        return [a for a in recipients if isinstance(a, str) and "@" in a]
    return []


# `_send_email_channel` / `_send_telegram_info_html` /
# `_send_telegram_deploy_html` / `_send_log_channel` RELOCATED to
# backend/api/algo/grammar_tokens/channel_email.py / channel_telegram_info.py
# / channel_telegram_deploy.py / channel_log.py (Wave 1, DSL-token-
# unification plan, Part B — channels). `channel_email.py` imports
# `resolve_email_recipients` back from THIS module lazily (inside its own
# function body) to avoid a circular import, since this import statement
# runs at `event_agents.py`'s own module-load time.
from backend.api.algo.grammar_tokens.channel_email import _send_email_channel  # noqa: E402
from backend.api.algo.grammar_tokens.channel_telegram_info import _send_telegram_info_html  # noqa: E402
from backend.api.algo.grammar_tokens.channel_telegram_deploy import _send_telegram_deploy_html  # noqa: E402
from backend.api.algo.grammar_tokens.channel_log import _send_log_channel  # noqa: E402


CHANNELS = {
    "ntfy": ("ntfy", _send_ntfy),
    "telegram": ("telegram", _send_telegram_html),
    "telegram_info": (None, _send_telegram_info_html),
    # Dedicated deploy-alerts Telegram bot/chat — see
    # `_send_telegram_deploy_html`'s own docstring. No separate
    # capability gate (`None`, matching `telegram_info`'s own entry) —
    # `_send_telegram()` itself already gates on the `telegram`
    # capability internally.
    "telegram_deploy": (None, _send_telegram_deploy_html),
    "email": ("mail", _send_email_channel),
    "log": (None, _send_log_channel),
}


def _gate_spec(agent) -> bool:
    return any(isinstance(a, dict) and a.get("type") == "render" and a.get("gate")
               for a in agent.actions or [])


def _gate_passes(agent, rec: dict, gated: bool) -> dict | None:
    """Apply the agent's repeat gate. Returns the record to render, or None to skip it."""
    if not gated:
        return rec
    from backend.shared.helpers.utils import mask_account_in_text
    gate_key = f"{rec.get('logger')}|{clean_message(mask_account_in_text(rec.get('message') or '') or '')}"
    repeats = _get_gate().decide(gate_key, alert_now=bool((rec.get("extra") or {}).get("alert_now")))
    return None if repeats is None else {**rec, "repeats": repeats}


# ── Action-agent / alert-agent tag (2026-10-31) ─────────────────────────
# Operator: "action agent take actions... alert agent... sends the
# notification... so the alert should have action agent name, as well
# alert agent names... add action agent name, and alert agent name in
# the alert... this should be ssot functionality".
#
# `format_notification_header()`'s existing "Agent: {origin}" line
# already identifies the ACTION agent (who did the thing — "manual" or
# a real agent name) and is left completely unchanged here. What was
# missing: which ALERT agent actually dispatched this notification —
# that identity existed only as internal DB bookkeeping
# (`agent.slug`/`agent.name`), never in the rendered text. Both values
# are appended as a trailing tag/pill row, built ONCE here (the one
# place every channel already funnels through, with both `agent` — the
# dispatching alert-agent — and `rec` — the record carrying the real
# action-agent's `agent_id`/`agent_name` extras — in scope) rather than
# re-derived in every individual renderer.


def _alert_agent_source_slug(agent, rec: dict) -> str:
    """The real dispatching alert-agent's short label.

    Prefers `ALERT_EVENT_DISPATCH[alert_event]['source_slug']` — the
    ORIGINAL (now-retired) detector agent's own slug, e.g.
    'chase-cancel-alert' — over `agent.slug`/`agent.name` directly.
    Without this, the 3 generic post-collapse channel agents
    (`alert-channel-telegram`/`-ntfy`/`-email`) would each show their
    own generic name ("Alert channel: Email") when independently
    dispatching the SAME record to their one channel — three different,
    less meaningful labels for what is really one alert. `source_slug`
    is read, never retyped (same convention `ALERT_EVENT_DISPATCH`
    itself is built with), so all 3 channel agents resolve to the
    identical, original detector name. The 7 agents that stayed
    dedicated post-collapse (`agent-alert-rich`, `cycle-breach-alert`,
    etc.) have no `alert_event`/no table entry and fall through to
    their own slug directly — already the real dispatching agent.
    """
    extra = rec.get("extra") or {}
    alert_event = extra.get("alert_event")
    if alert_event:
        entry = ALERT_EVENT_DISPATCH.get(alert_event)
        if entry and entry.get("source_slug"):
            return str(entry["source_slug"])
    return str(getattr(agent, "slug", None) or getattr(agent, "name", None) or "alert")


def _tag_token(value: "str | None") -> str:
    """Hashtag/pill-safe token: word characters + underscore only,
    lowercase. Telegram's hashtag entity only recognises `[A-Za-z0-9_]+`
    after `#` — a raw kebab-case slug ('chase-cancel-alert') would
    render as the real hashtag '#chase' followed by plain, untapped
    text '-cancel-alert'. Converting every other character to an
    underscore keeps the whole value as one real, tappable tag."""
    import re
    token = re.sub(r"[^0-9a-zA-Z]+", "_", str(value or "")).strip("_").lower()
    return token or "unknown"


def _agent_tag_line(agent, rec: dict) -> str:
    """Trailing plain-text tag line for Telegram/ntfy:
    '#<action_agent> #<alert_agent>'. Both values are sanitised forms
    of the exact same already-resolved values
    `format_notification_header()`'s own "Agent: ..." line uses — never
    a second, independent resolution of the action agent."""
    from backend.shared.helpers.alert_utils import resolve_origin
    extra = rec.get("extra") or {}
    action_label = resolve_origin(extra.get("agent_id"), extra.get("agent_name"))
    alert_label = _alert_agent_source_slug(agent, rec)
    return f"#{_tag_token(action_label)} #{_tag_token(alert_label)}"


def _agent_pill_html(agent, rec: dict) -> str:
    """Trailing HTML pill row for email — same two values as
    `_agent_tag_line`, rendered as their ORIGINAL readable form (not
    the underscored tag token — email has no hashtag constraint, so
    the readable form reads better), as two small inline-styled
    badges. Inline `style=` only — email clients strip `<style>`
    blocks and external CSS, so this can't reuse the app's own
    `.algo-status-pill` class."""
    from backend.shared.helpers.alert_utils import resolve_origin
    extra = rec.get("extra") or {}
    action_label = resolve_origin(extra.get("agent_id"), extra.get("agent_name"))
    alert_label = _alert_agent_source_slug(agent, rec)
    pill_style = (
        "display:inline-block;padding:2px 8px;margin:2px 4px 2px 0;"
        "border-radius:10px;background:#eef1f5;color:#44546a;"
        "font-size:12px;font-family:monospace;"
    )
    return (
        f'<div style="margin-top:10px">'
        f'<span style="{pill_style}">action: {html.escape(action_label)}</span>'
        f'<span style="{pill_style}">alert: {html.escape(alert_label)}</span>'
        f"</div>"
    )


def _append_agent_pill_to_html(html_body: str, pill_html: str) -> str:
    """Insert the pill row just before `</body>` when present (keeps it
    inside the real document); otherwise append — tolerant of any
    renderer's exact HTML shape, never raises."""
    if "</body>" in html_body:
        return html_body.replace("</body>", f"{pill_html}</body>", 1)
    return html_body + pill_html


async def _send_channel(ch: dict, out: tuple, agent, rec: "dict | None" = None) -> bool:
    spec = CHANNELS.get(ch.get("channel")) if isinstance(ch, dict) else None
    if spec is None or not ch.get("enabled"):
        return False
    capability, send = spec
    if ch.get("gate", True) and not _channel_enabled(capability):
        return False
    title, body = out[0], out[1]
    tg = out[2] if len(out) > 2 else None
    # 2026-10-31 — trailing action-agent / alert-agent tag (see the
    # three helpers above). `rec` is None for every pre-existing caller
    # that hasn't been updated to pass it (and every existing test) —
    # a complete no-op in that case, zero behavior change.
    #
    # 2026-11-05 fix (operator, live screenshot: "the tags should be on
    # the same line") — the tag line used to start its own paragraph
    # (`\n\n{tag_line}`), which read as a disconnected trailing block
    # and, combined with `_safe_truncate_message`'s 200-char diagnostic
    # cap elsewhere, could cut a tag in half with no visual link back to
    # the message it belonged to. Appended directly onto the body's own
    # last line instead — one space, no newline.
    if rec is not None:
        tag_line = _agent_tag_line(agent, rec)
        body = f"{body} {tag_line}"
        if tg:
            tg = f"{tg} {tag_line}"
    priority = ch.get("priority")
    # Tier-driven default — ntfy ONLY. `_send_telegram_html`/
    # `_send_telegram_info_html` take no `priority` parameter at all (a
    # bare positional-only signature, no **kwargs) — passing one would
    # raise TypeError and silently break every telegram send for any
    # agent with a tier (i.e. every real agent, since Agent.tier is
    # NOT NULL default "medium"). `_send_email_channel` happens to
    # tolerate it via **_kw, but scoping to ntfy explicitly is the
    # correct fix regardless, matching priority's actual meaning (ntfy
    # notification sound/urgency — meaningless for telegram/email).
    # Explicit per-channel priority still wins; `agent` is the real
    # Agent row configured to render this log-tag type, so `.tier` is
    # always present for this path. Neither set -> omit the kwarg
    # entirely so send_ntfy_alert's own clock-based day/night default
    # applies (unchanged from before this change).
    if not priority and ch.get("channel") == "ntfy":
        from backend.shared.helpers.alert_utils import _TIER_TO_NTFY_PRIORITY
        priority = _TIER_TO_NTFY_PRIORITY.get(getattr(agent, "tier", None))
    kwargs = {"priority": priority} if priority else {}
    if ch.get("channel") == "email":
        # A renderer that never built a structured (subject, html_body)
        # pair (`len(out) <= 3` — e.g. `_render_chase_cancel`/
        # `_render_order_correction`, both bare 2-tuples) still needs a
        # real email body when routed to this channel — most commonly
        # via `_apply_action_required_email_gate` synthesizing an entry
        # for an agent whose renderer never anticipated email. Falls
        # back to the plain `body` wrapped in `<pre>` so it's readable
        # and a raw `<`/`>` in the text can't corrupt the markup.
        email_payload = out[3] if len(out) > 3 else None
        if not email_payload:
            email_payload = (
                f"RamboQuant {title}",
                f"<html><body><pre style='white-space:pre-wrap;font-family:monospace'>"
                f"{html.escape(body)}</pre></body></html>",
            )
        if rec is not None:
            subject, html_body = email_payload
            email_payload = (
                subject, _append_agent_pill_to_html(html_body, _agent_pill_html(agent, rec)),
            )
        kwargs["email"] = email_payload
        kwargs["recipients"] = ch.get("recipients")
    if ch.get("channel") == "log":
        kwargs["log_line"] = ch.get("log_line")
        # 2026-11-05 live fix (operator report: a false "unrouted
        # alertable record" self-alert for market_status) — `events.py`'s
        # `_classify_recorded_channel()` pre-bakes `log_agent_slug` onto
        # a channel dict built from an AGENT'S OWN STATIC `events` list,
        # resolved the normal way. A producer that instead supplies its
        # own PER-RECORD dynamic `channels` override (the `out[4]`
        # mechanism BREACH_AGENT/MARKET_STATUS_ALERT_AGENT/etc. use — see
        # `action_send_market_status.py`'s plain
        # `{"channel": "log", "enabled": True}` literal) never goes
        # through that baking step, so `log_agent_slug` was always
        # `None` for those. `_send_log_channel` then stamped
        # `extra["agent_slug"]=None`, and `_looks_alert_shaped()`'s own
        # echo-exclusion (`if extra.get("agent_slug") and not
        # extra.get("alert_event")`) treats `None` as falsy — same as
        # "not present" — so the echo was wrongly NOT excluded and got
        # flagged as a fresh unrouted observation. `agent` here is
        # ALWAYS the real firing Agent row regardless of which path
        # built `ch`, so falling back to its own `.slug` closes the gap
        # for every current and future per-record dynamic-channel
        # producer, not just this one.
        kwargs["agent_slug"] = ch.get("log_agent_slug") or getattr(agent, "slug", None)
        kwargs["sim_mode"] = ch.get("log_sim_mode", False)
    try:
        await asyncio.to_thread(send, title, body, tg, **kwargs)
        return True
    except Exception as e:
        sys.stderr.write(f"event_agents: {agent.slug} {capability} send failed: {e}\n")
        return False


def _dispatch_trace(agent, rec: dict, channel_name: "str | None",
                     resolved: "dict | None", decision: str) -> None:
    """Per-record/per-channel dispatch trace (council item 4) — a single
    DEBUG line stating: matched agent/tag -> resolved channel+priority
    -> whether a code-level gate (the action-required ntfy override, see
    `_apply_action_required_ntfy_gate`) changed or suppressed it -> the
    final decision. This is the one place that makes the hidden second
    gate's effect visible — without it, "what's configured" on an
    agent's own `events` list and "what actually fired" can silently
    diverge with no trace anywhere (the UX review's finding). DEBUG-only
    and routed through this module's own skip-listed logger (see
    `_logger`'s module-level comment) — it is a diagnostic, never itself
    alert-shaped, and costs nothing when DEBUG logging is off."""
    if not _logger.isEnabledFor(10):  # logging.DEBUG, avoided importing `logging` just for this
        return
    extra = rec.get("extra") or {}
    tag = extra.get("alert_event") or (rec.get("tags") or [None])[0]
    _logger.debug(
        "dispatch-trace agent=%s tag=%s channel=%s priority=%s decision=%s",
        getattr(agent, "slug", None), tag, channel_name,
        (resolved or {}).get("priority"), decision,
    )


async def _dispatch_record_channels(
    agent, rec: dict, out: tuple, channels: list, log_only: bool,
    only_channel: "str | None" = None,
) -> int:
    """Send one rendered record to every channel in its (static or
    per-record) channel list. Extracted from `_dispatch_agent` (2026-
    10-23) purely to keep that function's cyclomatic complexity within
    the project's radon gate — no behavior change beyond the binary
    ntfy action-required gate itself.

    `only_channel` (2026-10-25 alert-channel collapse) — set by
    `_dispatch_channel_agent()` for one of the 3 generic channel agents.
    The full per-event channel list (verbatim from the former dedicated
    agent's own per-slug row) is passed in, the forced-email gate runs
    against the FULL list first (so an action-required record still
    gets its email entry appended exactly once, from whichever of the 3
    channel agents evaluates it — see that gate's own docstring), and
    only THEN is the list narrowed to the one channel this agent owns.
    Narrowing before the gate would either lose chase-cancel-alert's
    forced email (filtered out before the gate could append it) or risk
    double-appending if each channel agent's own copy of the list
    already carried a stale email entry. `None` (every other agent) is
    a no-op passthrough — unchanged behavior.

    2026-10 forced-email gate: `_apply_action_required_email_gate` runs
    once against the whole `channels` list (not per-channel like the
    ntfy gate below, since it may need to APPEND a new entry) so an
    action-required record always reaches email even when the
    dispatching agent's own resolved channel list never listed one.
    `log_only` filtering in the loop below still applies to the
    synthesized entry exactly like any other channel.
    """
    rec_priority = _rec_tracked_ntfy_priority(rec)
    action_required = (rec.get("extra") or {}).get("action_required")
    channels = _apply_action_required_email_gate(channels, action_required)
    if only_channel is not None:
        channels = [c for c in channels if isinstance(c, dict) and c.get("channel") == only_channel]
    sent = 0
    for ch in channels:
        channel_name = ch.get("channel") if isinstance(ch, dict) else None
        # A `_log_only` record is one the branch gate or sim-notify
        # gate would normally have suppressed entirely — 'log' is
        # exempt from both (local log write, no external notification
        # cost), every other channel in the SAME record still must not
        # fire. See dispatch_rows docstring for the full rationale.
        if log_only and (not isinstance(ch, dict) or ch.get("channel") != "log"):
            _dispatch_trace(agent, rec, channel_name, None, "suppressed_log_only")
            continue
        eff_ch = _channel_with_tracked_priority(ch, rec_priority)
        # 2026-10-23 chase robustness (fix #4) — binary ntfy gate: skips
        # ntfy entirely for a pure-FYI record, forces urgent for an
        # action-required one, leaves every other channel/record type
        # unchanged. See `_apply_action_required_ntfy_gate`'s own
        # docstring.
        gated_ch = _apply_action_required_ntfy_gate(eff_ch, action_required)
        if gated_ch is None:
            _dispatch_trace(agent, rec, channel_name, eff_ch, "suppressed_action_required_gate")
            continue
        override_applied = gated_ch != eff_ch
        ok = await _send_channel(gated_ch, out, agent, rec)
        decision = "sent" if ok else "send_failed"
        if override_applied:
            decision = f"{decision}_gate_override_applied"
        _dispatch_trace(agent, rec, channel_name, gated_ch, decision)
        if ok:
            sent += 1
    return sent


async def _dispatch_agent(agent, records: list[dict]) -> int:
    # 2026-10-25 alert-channel collapse — one of the 3 generic channel
    # agents (TELEGRAM_CHANNEL_AGENT/NTFY_CHANNEL_AGENT/EMAIL_CHANNEL_AGENT)
    # dispatches through a completely different path: it has no single
    # static renderer, and must never call `alert_routing.resolve_channels()`
    # (its slug has no real per-slug meaning for live dispatch — see
    # `ALERT_EVENT_DISPATCH`'s own docstring). Checked first and returns
    # immediately so neither of those two things below ever runs for it.
    channel_key = _channel_key_for_agent(agent)
    if channel_key is not None:
        return await _dispatch_channel_agent(agent, records, channel_key)
    key = _render_key(agent)
    render = RENDERS.get(key)
    if render is None:
        if key and key not in _unknown_renders:
            _unknown_renders.add(key)
            sys.stderr.write(f"event_agents: {agent.slug} names unknown renderer '{key}'\n")
        return 0
    gated = _gate_spec(agent)
    # 2026-10 routing-tree plan — channels for a static (non-BREACH)
    # agent now come from `alert_routing.resolve_channels()`, not read
    # directly off `agent.events`. Resolved ONCE per agent per dispatch
    # call (not per record): no current rule consults a per-record
    # `level`/`tags` matcher, and BREACH's own dynamic `out[4]` channels
    # (the only real per-record channel source in this codebase) bypass
    # this resolution entirely at the per-record line below, same as
    # before this change.
    agent_channels = alert_routing.resolve_channels(
        name=getattr(agent, "slug", None), priority=getattr(agent, "priority", None),
        agent_events=agent.events,
    )
    # Cheap backstop against the shared repeat-gate consuming its dedup
    # budget for rows that will end up fully suppressed anyway: a GATED
    # agent whose RESOLVED channel list carries no 'log' channel can
    # never rescue a `_log_only` row (see dispatch_rows docstring) — its
    # only possible channels are real notification channels, every one
    # of which will be dropped below regardless. BREACH_AGENT is the one
    # source of a dynamically-added 'log' channel (via out[4]) and is
    # deliberately ungated, so it always reaches render/_gate_passes.
    static_has_log = any(isinstance(e, dict) and e.get("channel") == "log" for e in agent_channels)
    sent = 0
    for rec in records:
        # Structural DEBUG exemption (2026-10-23) — a DEBUG-level record
        # (the `help` action, bare `debug(x)` sugar, or any future
        # DEBUG-tier producer) can NEVER reach a real notification
        # channel, regardless of what any event agent's own `conditions`
        # claim to match. Checked FIRST, before `conditions` is even
        # evaluated, so this applies uniformly to every kind='event'
        # agent — seeded AND any future operator-authored one — never a
        # hardcoded per-agent list. `LogStoreHandler` (log_store.py) is
        # instantiated at `level=logging.INFO`, so a real DEBUG record
        # never reaches this function's `records` list in production
        # today — this is belt-and-suspenders against a future change to
        # that threshold (or a synthetic record built some other way),
        # not the sole protection.
        if rec.get("level") == "DEBUG":
            continue
        if not evaluate(agent.conditions, Context(log_records=[rec])):
            continue
        if _error_agent_self_alerted(agent, rec):
            continue
        log_only = bool(rec.get("_log_only"))
        if log_only and gated and not static_has_log:
            continue
        to_render = _gate_passes(agent, rec, gated)
        if to_render is None:
            continue
        out = render(to_render)
        channels = out[4] if len(out) > 4 else agent_channels
        sent += await _dispatch_record_channels(agent, rec, out, channels, log_only)
    return sent


_CHANNEL_AGENT_RENDER_SENTINEL = "event_table"


def _channel_key_for_agent(agent) -> "str | None":
    """Non-`None` only for one of the 3 generic channel agents built by
    `_channel_agent_spec()` below. Mirrors `_render_key()`'s own
    action-scan shape, but reads the extra `channel_key` field those 3
    specs' render action carries instead of `render` itself — a channel
    agent's `render` is always the sentinel string, never a real
    renderer name, so `_render_key()`/`RENDERS.get(...)` must never be
    reached for it (see `_dispatch_agent`'s branch, and the sentinel's
    own no-op registration below)."""
    for action in agent.actions or []:
        if (isinstance(action, dict) and action.get("type") == "render"
                and action.get("render") == _CHANNEL_AGENT_RENDER_SENTINEL):
            return action.get("channel_key")
    return None


def _render_event_table_sentinel(rec: dict) -> tuple[str, str]:
    """Never actually invoked. `_dispatch_agent` branches to
    `_dispatch_channel_agent()` for a channel agent BEFORE it ever calls
    `RENDERS.get(_render_key(agent))` — registered here purely so
    `validate_seed_spec()`'s "unknown renderer" check doesn't reject the
    3 channel-agent specs, whose real per-record renderer is picked at
    dispatch time from `ALERT_EVENT_DISPATCH`, not from a static
    `actions[0]['render']` value."""
    return ("", "")


register_renderer(_CHANNEL_AGENT_RENDER_SENTINEL)(_render_event_table_sentinel)


async def _dispatch_channel_agent(agent, records: list[dict], channel_key: str) -> int:
    """Dispatch path for one of the 3 generic channel agents
    (`TELEGRAM_CHANNEL_AGENT`/`NTFY_CHANNEL_AGENT`/`EMAIL_CHANNEL_AGENT`,
    2026-10-25 alert-channel collapse). `agent.conditions` is the single
    OR of every collapsed detector spec's own `(alert_event, min_level,
    where)` leaf (`_CHANNEL_AGENT_CONDITION`) — a record that matches it
    is looked up in `ALERT_EVENT_DISPATCH` by its own `alert_event` to
    find which (now-retired) dedicated agent's renderer and verbatim
    channel list applies, and only the ONE channel entry naming
    `channel_key` from that list is actually sent (via
    `_dispatch_record_channels`'s `only_channel` filter). Firing all 3
    channel agents against the same record therefore reproduces exactly
    the set of real sends that record's former single dedicated agent
    used to make — split across 3 agent objects instead of 1, never
    duplicated and never dropped, since each channel-agent only ever
    claims the one channel it owns.

    Mirrors `_dispatch_agent`'s own per-record loop (DEBUG exemption,
    condition match, `_log_only` suppression) but deliberately omits two
    things that don't apply to any of the 16 collapsed specs: the
    repeat-gate (`_gate_spec`/`_gate_passes` — none of them set
    `actions[0]['gate']`) and `_error_agent_self_alerted` (keyed on
    `ERROR_AGENT`'s own slug, never this agent's)."""
    sent = 0
    for rec in records:
        if rec.get("level") == "DEBUG":
            continue
        if not evaluate(agent.conditions, Context(log_records=[rec])):
            continue
        log_only = bool(rec.get("_log_only"))
        if log_only:
            continue
        extra = rec.get("extra") or {}
        entry = ALERT_EVENT_DISPATCH.get(extra.get("alert_event"))
        if entry is None:
            continue
        render = RENDERS.get(entry["render"])
        if render is None:
            continue
        out = render(rec)
        channels = [dict(c) for c in entry["channels"]]
        sent += await _dispatch_record_channels(agent, rec, out, channels, log_only, only_channel=channel_key)
    return sent


def _rec_tracked_ntfy_priority(rec: dict) -> "str | None":
    """2026-10-16 fix — a record carrying a real triggering agent's
    `tier` (currently only rich_alert records, via
    _v2_send_rich_alert's logged extras) resolves an ntfy priority
    override for that record, from the REAL tier rather than the
    dispatching agent's own (RICH_ALERT_AGENT is a generic multiplexer,
    always tier="medium"). Extracted from `_dispatch_agent` to keep it
    below the complexity gate.

    2026-10-23 (item 7, `log(...)` priority param) — an EXPLICIT
    `priority` on the record's `extra` (set by a hand-typed
    `log(level=..., priority=..., message=...)` DSL call, see
    `actions.py:emit_log`) wins outright and is checked FIRST: it is a
    more specific, explicit per-call signal than an inferred tier
    default. Only when no explicit `priority` is present does this fall
    back to the existing tier-derived resolution — zero regression for
    every currently-matching event agent. `debug(x)`'s compiled sugar
    never sets `priority`, so it always falls through to the tier
    branch (moot anyway once the DEBUG exemption above drops the
    record before this is ever reached)."""
    extra = rec.get("extra") or {}
    explicit_priority = extra.get("priority")
    if explicit_priority:
        return explicit_priority
    rec_tier = extra.get("tier")
    if not rec_tier:
        return None
    from backend.shared.helpers.alert_utils import _TIER_TO_NTFY_PRIORITY
    return _TIER_TO_NTFY_PRIORITY.get(rec_tier)


def _channel_with_tracked_priority(ch: dict, rec_priority: "str | None") -> dict:
    """Apply `rec_priority` to an ntfy channel entry that sets no
    explicit priority of its own — an operator-configured per-channel
    priority always wins. Extracted from `_dispatch_agent` to keep it
    below the complexity gate."""
    if (rec_priority and isinstance(ch, dict)
            and ch.get("channel") == "ntfy" and not ch.get("priority")):
        return {**ch, "priority": rec_priority}
    return ch


def _apply_action_required_ntfy_gate(ch: dict, action_required: "bool | None") -> "dict | None":
    """2026-10-23 chase robustness (fix #4) — binary ntfy gate, applied
    at DISPATCH time rather than by editing any agent's own stored
    `events` channel list (which would not retroactively change an
    already-seeded `agents` DB row on prod/dev — `load_agents()` reads
    from the DB, not from this module's seed dicts).

    `action_required` is read from the log record's own `extra` dict
    (set `False` by `fill_notify.notify_fills`, `True` by
    `send_order_failure_alert` and the chase cancel-unconfirmed record):
      - `False` (a pure-FYI outcome, e.g. a fill) -> this function
        returns `None` for an ntfy channel, meaning the caller must skip
        sending it entirely, regardless of how the agent's own channel
        list is configured.
      - `True` (operator must take action) -> forces `priority="urgent"`
        on an ntfy channel, overriding any existing per-channel priority
        — a simple binary, not a tier spread.
      - Absent (`None`, the default for every record type not yet
        tagged — ticker/gtt/deploy/etc.) -> `ch` passed through
        unchanged, preserving every existing agent's current behavior.
    Non-ntfy channels are always passed through unchanged.
    """
    if not isinstance(ch, dict) or ch.get("channel") != "ntfy":
        return ch
    if action_required is False:
        return None
    if action_required is True:
        return {**ch, "priority": "urgent"}
    return ch


def _apply_action_required_email_gate(channels: list, action_required: "bool | None") -> list:
    """2026-10 forced-email gate, mirroring
    `_apply_action_required_ntfy_gate` above — "Action-required alerts
    go to both ntfy (already gated) and email" (operator requirement
    #5). Operates on the whole CHANNEL LIST (not one channel dict at a
    time, like the ntfy gate) because it may need to APPEND a new entry
    rather than mutate an existing one.

      - `action_required is True` and no `email` entry already present
        -> appends one (`enabled: True, gate: False` — same
        always-fires convention `order-failure-alert`'s own static
        email entry already uses, since the whole point of this gate is
        an action-required alert must not be silently skipped by a
        capability toggle).
      - `action_required is True` and an `email` entry already exists
        (e.g. `order-failure-alert`'s static per-slug list) -> the list
        is returned unchanged; this gate never duplicates an entry.
      - `False` or `None` (absent, the default for every record type not
        yet tagged) -> the list is returned unchanged, same conservative
        default as the ntfy gate (a pure-FYI or untagged record must
        never GAIN a channel it didn't already have).

    `_send_channel`'s own email branch supplies a default `<pre>`-wrapped
    plain-text body for a renderer (`_render_chase_cancel`,
    `_render_order_correction`) that never built a structured
    `(subject, html_body)` 4th tuple element — see its own comment."""
    if action_required is not True:
        return channels
    if any(isinstance(c, dict) and c.get("channel") == "email" for c in channels):
        return channels
    return [*channels, {"channel": "email", "enabled": True, "gate": False}]


async def dispatch(records: list[dict], agents: list) -> int:
    sent = 0
    for agent in agents:
        sent += await _dispatch_agent(agent, records)
    return sent


# ─────────────────────────────────────────────────────────────────────
# Catch-all for an unrouted alert-shaped record (council item 5, risk +
# research safety net) — a record that LOOKS like it should alert
# (carries a known alert-tag, at WARNING+) but matches zero active event
# agent's `conditions` used to be silently dropped with no trace at all.
# Now it gets exactly one WARNING self-alert instead — and that
# self-alert must itself reach a real channel (Telegram/ntfy/email), not
# just a log line, or this is the exact same "silent" failure mode the
# risk review wrote this requirement to prevent (2026-10 fix: the first
# version of this code logged the self-alert via `_logger`, this
# module's own `log_store.py`-skip-listed logger — which made it
# impossible for the self-alert to ever reach a real channel at all).

_LEVEL_ORDER = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Reserved `alert_event` value for the self-alert's OWN log record. This,
# not logger-name exclusion, is the real recursion guard: the self-alert
# is logged through `_EVENTS_LOGGER` (captured normally by
# `LogStoreHandler`/`dispatch_rows`, so it CAN match a real agent and
# reach a real channel — see `UNROUTED_ALERT_AGENT` below) and carries
# this exact `alert_event`. `_looks_alert_shaped()` explicitly excludes
# any record carrying it, so the self-alert can never be classified as
# "unrouted" itself, regardless of whether `UNROUTED_ALERT_AGENT` is
# active, deleted, or fails to match for some future reason — the guard
# does not depend on the agent match succeeding.
_UNROUTED_ALERT_EVENT = "unrouted_alert"


def _level_at_least(level: "str | None", floor: str) -> bool:
    try:
        return _LEVEL_ORDER.index((level or "INFO").upper()) >= _LEVEL_ORDER.index(floor)
    except ValueError:
        return False


def _known_alert_tags() -> set[str]:
    """DSL-token reuse-first: the vocabulary is `LOG_TAG_TOKENS`
    (`agent_grammar.yaml`'s `log_tags.tags`), the SAME catalog
    `validate_seed_spec()` already checks a seeded agent's own
    `conditions` against — never a second hardcoded list that could
    drift from it."""
    from backend.api.algo.grammar import LOG_TAG_TOKENS
    return {t["token"] for t in LOG_TAG_TOKENS}


def _looks_alert_shaped(rec: dict) -> bool:
    """A record that looks like it should have alerted: WARNING+ level,
    carrying at least one tag from the known alert-tag vocabulary.

    Recursion guard: a record carrying the reserved `alert_event`
    (`_UNROUTED_ALERT_EVENT`) is NEVER alert-shaped by this function's
    own definition — this is what stops the self-alert from ever being
    flagged as itself unrouted, independent of whether it actually
    matched a real agent downstream.

    Second exclusion (2026-10-29 live fix): the 'log' notify-channel's
    own delivery write (`_send_log_channel`,
    grammar_tokens/channel_log.py) stamps `extra["agent_slug"]` but
    deliberately NEVER `extra["alert_event"]` (see that function's own
    docstring — setting one would re-match BREACH_AGENT and loop). That
    record is an ECHO of an agent that already matched its own
    conditions upstream and chose "log" as one of its delivery
    channels — it is not a fresh observation that itself needs to match
    a condition. Without this exclusion it is WARNING+ and tagged
    "agent" (a known alert tag), so every single agent firing that
    includes "log" in its channel list re-triggered this catch-all on
    the very next flush, since no event-agent condition is shaped to
    match a bare tag="agent" record with no `alert_event` — confirmed
    live: this produced a spurious "unrouted alertable record"
    self-alert following literally every regular alert.  The two other
    producers that stamp `agent_slug` (`RICH_ALERT_AGENT`'s
    `rich_alert` / `BREACH_AGENT`'s `breach` in `agent_engine.py` /
    `events.py`) always carry a real `alert_event` alongside it, so
    this check only ever excludes the log-channel echo."""
    extra = rec.get("extra") or {}
    if extra.get("alert_event") == _UNROUTED_ALERT_EVENT:
        return False
    if extra.get("agent_slug") and not extra.get("alert_event"):
        return False
    if not _level_at_least(rec.get("level"), "WARNING"):
        return False
    tags = rec.get("tags") or []
    return bool(set(tags) & _known_alert_tags())


def _record_matches_any_agent(rec: dict, agents: list) -> bool:
    """True if at least one agent's own `conditions` match this record —
    independent of channel-level suppression/gating downstream (a
    record that matched an agent but had every channel filtered out by
    `_log_only`/the action-required gate is NOT "unrouted"; it was
    routed, delivery was just suppressed by design — out of scope for
    this catch-all, which is only about condition matching, per the
    plan's own wording)."""
    for agent in agents:
        try:
            if evaluate(agent.conditions, Context(log_records=[rec])):
                return True
        except Exception:
            continue
    return False


def _safe_truncate_message(s: str, limit: int) -> str:
    """Truncate *s* to at most *limit* characters without ever cutting a
    trailing '#tag' fragment in half.

    2026-11-05 live fix (operator-reported, screenshot of the real
    market-status self-alert from before the `_send_channel` `agent_
    slug` fix shipped): the embedded message's hard `[:limit]` cut
    landed mid-way through the trailing `_agent_tag_line()` text,
    rendering as a dangling "...#market_status #" — the second tag's
    leading `#` with nothing after it, confusing on its own even though
    the UNDERLYING tag generation was correct (the full line would have
    read "#market_status #market_status_alert"). If the hard cut lands
    inside the LAST whitespace-delimited word and that word starts with
    '#', the whole dangling fragment is dropped instead of leaving half
    of it visible."""
    if len(s) <= limit:
        return s
    cut = s[:limit]
    last_space = cut.rfind(" ")
    tail = cut[last_space + 1:] if last_space != -1 else cut
    if tail.startswith("#"):
        cut = cut[:last_space] if last_space != -1 else ""
    return cut


def _emit_unrouted_self_alert(rec: dict) -> None:
    """Log the one WARNING self-alert for an unrouted alert-shaped
    record — through `_EVENTS_LOGGER`, NOT `_logger`, so it is captured
    by `LogStoreHandler`/`dispatch_rows()` like any other real alert and
    can actually reach a real channel via `UNROUTED_ALERT_AGENT` below.
    Tagged `["agent"]` (a known, real alert tag) with the reserved
    `alert_event=_UNROUTED_ALERT_EVENT` — `UNROUTED_ALERT_AGENT`'s own
    condition matches exactly that shape. `title`/`body` are pre-built
    onto `extra` for the shared `generic_notice` renderer (the same
    renderer `GTT_ASYMMETRIC_AGENT` etc. already use), mirroring how
    `background.py`'s own `generic_notice` callers build their records.

    Recursion guard: see `_UNROUTED_ALERT_EVENT`'s own comment — this
    record can never be classified as "unrouted" itself by
    `_looks_alert_shaped()`, regardless of whether `UNROUTED_ALERT_AGENT`
    is active or matches."""
    extra = rec.get("extra") or {}
    tags = ",".join(rec.get("tags") or [])
    msg = (
        f"unrouted alertable record: tag={tags} level={rec.get('level')} "
        f"logger={rec.get('logger')} alert_event={extra.get('alert_event')} "
        f"message={_safe_truncate_message(rec.get('message') or '', 200)}"
    )
    _EVENTS_LOGGER.warning(
        msg,
        # 2026-10-25 tag-unification — "alert" added additively alongside
        # the pre-existing "agent" tag so `UNROUTED_ALERT_AGENT`'s own
        # condition (now tag="alert") still matches. See that agent's own
        # dict comment above for why this one, unlike RICH_ALERT_AGENT/
        # BREACH_AGENT, is safe to unify.
        extra={"tags": ["agent", "alert"], "alert_event": _UNROUTED_ALERT_EVENT,
              "title": "Unrouted alertable record", "body": msg},
    )


async def load_agents() -> list:
    now = time.monotonic()
    if now - _cache["at"] < _CACHE_S:
        return _cache["agents"]
    from backend.api.database import async_session
    from backend.api.models import Agent
    async with async_session() as s:
        rows = (await s.execute(
            select(Agent).where(Agent.kind == "event", Agent.status == "active")
        )).scalars().all()
    _cache.update(at=now, agents=list(rows))
    return _cache["agents"]


def _alerts_enabled_here() -> bool:
    from backend.shared.helpers.utils import config
    return config.get("deploy_branch", "main") == "main"


def _sim_notify_allowed() -> bool:
    from backend.shared.helpers.settings import get_bool
    return get_bool("simulator.notify_during_run", False)


async def dispatch_rows(rows: list[dict]) -> None:
    """Flush one batch of tagged log rows to any matching event agent.

    Two independent gates normally suppress a row entirely: the deploy-
    branch gate (`_alerts_enabled_here`, real notifications only fire from
    the main-branch deploy) and the simulator notify-during-run gate
    (`_sim_notify_allowed`). Both exist to stop telegram/email/ntfy from
    paging a real phone/inbox from a dev instance or a fabricated
    simulator run.

    The 'log' channel is exempt from BOTH gates — it's a local log write
    with no external notification cost, and its whole documented purpose
    ("silent testing of new agents") requires it to keep working on dev
    and during simulator runs. Earlier (pre-fix) this function dropped
    the WHOLE row when a gate failed, which also silenced any 'log'
    channel sharing that row — including a MIXED record (e.g. one agent
    firing both 'telegram' and 'log' with no custom per-channel message,
    which land in the SAME shared record per events.py's message-grouping
    logic). Fix: never drop a row here for branch/sim reasons. Instead,
    stamp it `_log_only` when either gate would have suppressed it, and
    let `_dispatch_agent` filter at the CHANNEL level — 'log' always
    passes, every other channel in that same record still correctly
    stays dark.
    """
    rows = [{**r, "extra": {**(r.get("extra") or {}), **(r.get("_bulk") or {})}} for r in rows]
    rows = [r for r in rows if (r.get("extra") or {}).get("origin") in (None, "main")]
    if not rows:
        return
    branch_ok = _alerts_enabled_here()
    sim_ok = True
    if any((r.get("extra") or {}).get("sim_mode") for r in rows):
        sim_ok = _sim_notify_allowed()
    for r in rows:
        is_sim = bool((r.get("extra") or {}).get("sim_mode"))
        r["_log_only"] = (not branch_ok) or (is_sim and not sim_ok)
    try:
        agents = await load_agents()
        if agents and rows:
            await dispatch(rows, agents)
        _run_unrouted_catch_all(rows, agents)
    except Exception as e:
        sys.stderr.write(f"event_agents: dispatch failed: {e}\n")


def _run_unrouted_catch_all(rows: list[dict], agents: list) -> None:
    """Council item 5 — catch-all for an unrouted alert-shaped record.
    Extracted from `dispatch_rows` purely to keep that function's
    cyclomatic complexity within the project's radon gate (same
    extraction pattern already used for `_dispatch_record_channels`).

    Runs regardless of whether `agents` was non-empty: an empty/failed
    `load_agents()` result means EVERY alert-shaped row in this batch is
    correctly flagged unrouted, which is the right signal for a
    pipeline-wide break (not just one agent's condition being wrong).
    Condition-matching only — independent of the `_log_only`/action-
    required channel-level gates, which are legitimate delivery
    suppression, not "unrouted"."""
    for r in rows:
        if _looks_alert_shaped(r) and not _record_matches_any_agent(r, agents):
            _emit_unrouted_self_alert(r)


ERROR_AGENT = {
    "slug": "error-alert",
    "name": "Error alert",
    "description": "Catch-all for any ERROR (or unrouted CRITICAL) log line "
                    "that isn't already alerted by its own dedicated path.",
    # tag="error" also matches a CRITICAL record as of 2026-10-24 — see
    # `log_store.py:tags_for()`'s own docstring. This condition dict is
    # deliberately left UNCHANGED (no `{"any": [...]}` tag="critical"
    # leaf added here): the fix lives entirely in how `tags_for()` tags
    # a record, so every already-seeded `Agent` row in prod/dev (whose
    # stored `conditions` JSON predates this fix) gets the corrected
    # behavior for free, with no `SEED_VERSION` bump and no DB
    # migration required.
    "conditions": {"log": {"tag": "error", "min_level": "ERROR"}},
    # 2026-10 routing-tree plan — channel selection moved to
    # `alert_routing.py`'s per-slug override table. See `FILL_AGENT`'s
    # comment above for the full rationale; every migrated agent below
    # follows the same pattern without repeating it in full.
    "priority": "medium",
    "actions": [{"type": "render", "render": "error", "gate": True}],
}


def _error_agent_self_alerted(agent, rec: dict) -> bool:
    """2026-10-23 audit fix (O9) — ERROR_AGENT is a blanket catch-all:
    `log_store.py:tags_for` auto-tags EVERY record with its level name
    lowercased, so ERROR_AGENT's condition (`tag="error", min_level=
    "ERROR"`) matches ANY `logger.error(...)` call anywhere in the
    codebase, regardless of logger name or intent. Several call sites
    (chase's max-attempts exhaustion, `actions_live.py`'s
    `close_position` failure path) already fire their own specific
    operator alert (`send_order_failure_alert`) immediately around the
    same `logger.error` call — without this gate those events
    double-alert: once via their own specific path, once via this
    generic catch-all.

    Callers that already alert mark their `logger.error(...)` call with
    `extra={"self_alerted": True}`. This gate only applies to
    ERROR_AGENT itself (matched by slug) — any other agent that happens
    to condition on `tag="error"`/ERROR level is unaffected, and an
    ERROR record with no `self_alerted` flag dispatches exactly as
    before.

    2026-10-24 — `tags_for()` now also stamps `"error"` on a CRITICAL
    record, so ERROR_AGENT's unchanged catch-all condition also matches
    every bare `logger.critical(...)` call (previously tagged
    `"critical"` only, so it never matched at all and silently never
    alerted). Several CRITICAL call sites already have their OWN
    dedicated, `alert_event`-gated agent (chase's `cancel_unconfirmed`,
    the GTT wing `wing_hard_reject`/`partial_gtt` agents) — every one of
    those tags its record with an `alert_event` extra, which a truly
    bare, unrouted CRITICAL record never carries. So a CRITICAL record
    that carries `alert_event` is treated the same as an explicit
    `self_alerted` flag here: it is presumed already routed to its own
    agent, and is skipped for ERROR_AGENT specifically. A bare CRITICAL
    record with neither flag falls through to ERROR_AGENT exactly like
    a bare ERROR record always has.
    """
    if getattr(agent, "slug", None) != ERROR_AGENT["slug"]:
        return False
    extra = rec.get("extra") or {}
    if bool(extra.get("self_alerted")):
        return True
    if rec.get("level") == "CRITICAL" and extra.get("alert_event"):
        return True
    return False


CHASE_CANCEL_AGENT = {
    "slug": "chase-cancel-alert",
    "name": "Chase cancel unconfirmed",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Producer (`chase.py`) already tags `["chase", "alert"]`.
    "conditions": {"log": {"tag": "alert", "min_level": "CRITICAL",
                           "where": {"alert_event": "cancel_unconfirmed"}}},
    # 2026-10 routing-tree plan — channel selection moved to
    # `alert_routing.py`'s per-slug override table (verbatim-identical
    # to the telegram+ntfy(urgent) list this dict used to carry
    # directly; see that table's own comment for the "one of the most
    # severe alert types" rationale this `priority` token reflects).
    "priority": "urgent",
    "actions": [{"type": "render", "render": "chase_cancel"}],
}

PARTIAL_GTT_AGENT = {
    "slug": "partial-gtt-alert",
    "name": "Partial GTT placement",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Producer (`orders_place.py`) already tags `["orders", "gtt", "alert"]`.
    "conditions": {"log": {"tag": "alert", "min_level": "CRITICAL",
                           "where": {"alert_event": "partial_gtt"}}},
    "priority": "urgent",
    "actions": [{"type": "render", "render": "partial_gtt"}],
}

TEMPLATE_ATTACH_URGENT_AGENT = {
    "slug": "template-attach-urgent",
    "name": "Template attach urgent",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Both producers (`template_attach.py`) already tag
    # `["orders", "gtt", "alert"]`.
    "conditions": {"any": [
        {"log": {"tag": "alert", "min_level": "WARNING", "where": {"alert_event": "wing_unprotected"}}},
        {"log": {"tag": "alert", "min_level": "CRITICAL", "where": {"alert_event": "wing_hard_reject"}}},
    ]},
    "priority": "urgent",
    "actions": [{"type": "render", "render": "template_attach"}],
}

TEMPLATE_ATTACH_HIGH_AGENT = {
    "slug": "template-attach-high",
    "name": "Template attach high",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Both producers (`template_attach.py`) already tag
    # `["orders", "gtt", "alert"]`.
    "conditions": {"any": [
        {"log": {"tag": "alert", "min_level": "WARNING", "where": {"alert_event": "wing_skip"}}},
        {"log": {"tag": "alert", "min_level": "WARNING", "where": {"alert_event": "wing_offset_skip"}}},
    ]},
    "priority": "high",
    "actions": [{"type": "render", "render": "template_attach"}],
}

ORDER_FAILURE_AGENT = {
    "slug": "order-failure-alert",
    "name": "Order failure",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Producer (`alert_utils.py`) already tags `["orders", "alert"]`.
    "conditions": {"log": {"tag": "alert", "min_level": "WARNING",
                           "where": {"alert_event": "order_failure"}}},
    "priority": "urgent",
    "actions": [{"type": "render", "render": "order_failure"}],
}

ORDER_CORRECTION_AGENT = {
    "slug": "order-correction-alert",
    "name": "Order poll correction",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Producer (`order_poll.py`) already tags `["orders", "alert"]`.
    "conditions": {"log": {"tag": "alert", "min_level": "WARNING",
                           "where": {"alert_event": "order_correction"}}},
    "priority": "urgent",
    "actions": [{"type": "render", "render": "order_correction"}],
}

TEMPLATE_GUARD_AGENT = {
    "slug": "template-guard-alert",
    "name": "Template guard",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Producer (`template_attach.py`) already tags `["orders", "alert"]`.
    "conditions": {"log": {"tag": "alert", "min_level": "INFO",
                           "where": {"alert_event": "template_guard"}}},
    "priority": "high",
    "actions": [{"type": "render", "render": "template_guard"}],
}

TEMPLATE_ATTACH_FAIL_AGENT = {
    "slug": "template-attach-fail-alert",
    "name": "Template attach failed",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Producer (`template_attach.py`) already tags `["orders", "alert"]`.
    "conditions": {"log": {"tag": "alert", "min_level": "WARNING",
                           "where": {"alert_event": "template_attach_fail"}}},
    "priority": "urgent",
    "actions": [{"type": "render", "render": "template_attach_fail"}],
}

MCP_PING_AGENT = {
    "slug": "mcp-ping-alert",
    "name": "MCP audit ping",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Producer (`routes/lab.py`) already tags `["mcp", "alert"]`.
    "conditions": {"log": {"tag": "alert", "min_level": "INFO", "where": {"alert_event": "mcp_ping"}}},
    "priority": "info",
    "actions": [{"type": "render", "render": "mcp_ping"}],
}

DEPLOY_SYNC_AGENT = {
    "slug": "deploy-sync-alert",
    "name": "Deploy out of sync",
    "description": "Fires when a push lands on dev or main but the webhook "
                    "deploy on the other branch hasn't caught up.",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Producer (`background.py`) now also tags `"alert"` (additive,
    # alongside its pre-existing "deploy" tag).
    "conditions": {"log": {"tag": "alert", "min_level": "WARNING",
                           "where": {"alert_event": "deploy_out_of_sync"}}},
    # 2026-10 operator instruction: ntfy removed — a deploy-out-of-sync
    # notice is a developer/ops concern, not something that should page
    # a phone. No replacement channel was requested; the per-slug
    # override in `alert_routing.py` for this slug is an empty list (the
    # agent still fires/renders/logs its condition match — see
    # `_dispatch_agent` — it simply has no channel to send to right
    # now). Add a channel there, not a new mechanism, if one is wanted
    # later.
    "priority": "medium",
    "actions": [{"type": "render", "render": "deploy_sync"}],
}

RICH_ALERT_AGENT = {
    "slug": "agent-alert-rich",
    "name": "Agent alert",
    "description": "Generic multiplexer — renders and delivers every other "
                    "cycle agent's own threshold/breach alert.",
    "conditions": {"log": {"tag": "agent", "min_level": "INFO", "where": {"alert_event": "rich_alert"}}},
    # 2026-10-16 fix (unchanged by the routing-tree migration) — no
    # hardcoded ntfy priority. This agent fans out EVERY other agent's
    # alerts (it's a generic multiplexer, always tier="medium" itself),
    # so a hardcoded "urgent" here forced send_ntfy_alert's 3x-repeat-send
    # behavior onto every single alert regardless of the real triggering
    # agent's own tier — live incident: a routine info-tier scheduled
    # notification (market-open-nse) pushed 3 ntfy alerts. _dispatch_agent
    # resolves the real priority from the record's own carried-through
    # `tier` (see _v2_send_rich_alert's logged extras) — this agent's own
    # `priority` field below is unrelated (routing-table matcher key
    # only, never passed to the ntfy send itself).
    "priority": "medium",
    "actions": [{"type": "render", "render": "rich_alert"}],
}

SUMMARY_AGENT = {
    "slug": "market-summary",
    "name": "Market summary",
    "description": "Sends the scheduled market-open/close funds and "
                    "positions summary.",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Producer (`alert_utils.py`) now also tags `"alert"` (additive,
    # alongside its pre-existing "summary" tag).
    "conditions": {"log": {"tag": "alert", "min_level": "INFO", "where": {"alert_event": "summary"}}},
    "priority": "info",
    "actions": [{"type": "render", "render": "summary"}],
}

BREACH_AGENT = {
    "slug": "cycle-breach-alert",
    "name": "Cycle breach",
    "description": "Renders a cycle agent's own threshold-breach detail "
                    "(kind, value, account) when it fires.",
    "conditions": {"log": {"tag": "agent", "min_level": "INFO", "where": {"alert_event": "breach"}}},
    "events": [],
    "actions": [{"type": "render", "render": "breach"}],
}

# ─────────────────────────────────────────────────────────────────────
# Shared table for the `generic_notice` agents (council item 2, 2026-10).
#
# These 5 agents already share the identical renderer (`_render_deploy_sync`,
# registered under the `"generic_notice"` key) and differ only by
# tag/min_level/alert_event and their own channel list — the ONE place a
# "collapse" is actually free (see CLAUDE.md's "Net:" conclusion on the
# 6-agent council review). Per the UX review's explicit instruction, this
# collapses the MECHANISM, not the identity: each row below still builds
# its own separately-named, separately-toggleable `Agent` dict (same
# `slug`/`name` as before, same individual activate/deactivate on the
# Automation page, same per-row last-fired/count) — only the SOURCE of
# each dict's `conditions`/`events` moved from a ~15-line literal to one
# table row + a shared builder. Adding a 6th `generic_notice`-style alert
# in the future is a table row, not a new dict literal.
#
# The resulting `GTT_ASYMMETRIC_AGENT`/`OCO_DOUBLE_FIRE_AGENT`/
# `TICKER_RECOVERED_AGENT`/`TICKER_DEGRADED_AGENT`/`VISITOR_REPORT_AGENT`
# module-level dicts below are byte-identical in `conditions`/`actions`
# content to what this file used to spell out as 5 separate literals —
# confirmed by `test_event_agents.py`'s shared-table-sourcing regression
# test. Channel selection for each (previously this table's own
# `channels` key, fed straight into the built dict's `events`) moved to
# `alert_routing.py`'s per-slug override table in the 2026-10
# routing-tree plan — this table now carries a `priority` token per spec
# instead, matching every other migrated `SEEDED_AGENTS` entry.
_GENERIC_NOTICE_SPECS: tuple[dict, ...] = (
    {"slug": "gtt-asymmetric-alert", "name": "GTT asymmetric",
     "tag": "gtt", "min_level": "WARNING", "alert_event": "gtt_asymmetric",
     "priority": "high"},
    {"slug": "oco-double-fire-alert", "name": "OCO double-fire",
     "tag": "gtt", "min_level": "WARNING", "alert_event": "oco_double_fire",
     "priority": "urgent"},
    {"slug": "ticker-recovered-alert", "name": "Ticker recovered",
     "tag": "broker", "min_level": "INFO", "alert_event": "ticker_recovered",
     "priority": "high"},
    {"slug": "ticker-degraded-alert", "name": "Ticker degraded",
     "tag": "broker", "min_level": "WARNING", "alert_event": "ticker_degraded",
     "priority": "urgent"},
    {"slug": "visitor-report-alert", "name": "Visitor report",
     "tag": "visitor", "min_level": "INFO", "alert_event": "visitor_report",
     "priority": "info", "description": "Sends the periodic site-visitor traffic report."},
)


def _build_generic_notice_agent(spec: dict) -> dict:
    # 2026-10-25 tag-unification — the condition always matches on the
    # additive "alert" tag now, never `spec["tag"]` (the category tag,
    # e.g. "gtt"/"broker"/"visitor") — `where.alert_event` already
    # uniquely discriminates each of these 5 agents, so the category tag
    # was redundant. `spec["tag"]` is kept in the table purely as
    # documentation of which category tag each real producer (in
    # `background.py`) also still carries, additively, alongside "alert".
    # See FILL_AGENT's comment above for the shared rationale.
    return {
        "slug": spec["slug"],
        "name": spec["name"],
        "description": spec.get("description", ""),
        "conditions": {"log": {"tag": "alert", "min_level": spec["min_level"],
                               "where": {"alert_event": spec["alert_event"]}}},
        "priority": spec["priority"],
        "actions": [{"type": "render", "render": "generic_notice"}],
    }


GTT_ASYMMETRIC_AGENT = _build_generic_notice_agent(_GENERIC_NOTICE_SPECS[0])
OCO_DOUBLE_FIRE_AGENT = _build_generic_notice_agent(_GENERIC_NOTICE_SPECS[1])
TICKER_RECOVERED_AGENT = _build_generic_notice_agent(_GENERIC_NOTICE_SPECS[2])
TICKER_DEGRADED_AGENT = _build_generic_notice_agent(_GENERIC_NOTICE_SPECS[3])
VISITOR_REPORT_AGENT = _build_generic_notice_agent(_GENERIC_NOTICE_SPECS[4])

# `alert_event` -> (slug, name) for every `generic_notice` producer in
# `background.py` (2026-11-03, operator: "every alert should be
# generated by an agent... format... driven by tags"). Read from the
# SAME specs this module's own agent definitions are built from — never
# retyped at a `background.py` call site. `DEPLOY_SYNC_AGENT` is added
# explicitly because it (like `VISITOR_REPORT_AGENT`) stayed a dedicated
# agent in the 2026-10-25 alert-channel collapse, so it has no entry in
# `_GENERIC_NOTICE_SPECS` (that tuple only covers the 4 gtt/ticker
# detectors + visitor-report).
_GENERIC_NOTICE_IDENTITY: dict[str, tuple[str, str]] = {
    spec["alert_event"]: (spec["slug"], spec["name"]) for spec in _GENERIC_NOTICE_SPECS
}
_GENERIC_NOTICE_IDENTITY["deploy_out_of_sync"] = (DEPLOY_SYNC_AGENT["slug"], DEPLOY_SYNC_AGENT["name"])


async def generic_notice_identity_extra(alert_event: "str | None") -> dict:
    """`{"agent_id": int, "agent_name": str}` for a `generic_notice`
    producer's log `extra` dict — called from `background.py`'s 6
    call sites (gtt_asymmetric/oco_double_fire/ticker_recovered/
    ticker_degraded/visitor_report/deploy_out_of_sync) right before each
    `logger.X(...)` call, so `_render_deploy_sync` below can show a real
    "Agent: {name} (#{id})" header instead of none at all.

    Resolves through the shared, cached `get_agent_id_by_slug()` — the
    same accessor `agent_engine.py`'s own manual-sentinel resolution
    uses. All 6 of these agents are real Agent DB rows (4 of them
    retired/inactive after the collapse, never deleted — see
    `_RETIRED_SLUGS`), so the id always resolves once seeded.

    Returns `{}` (no identity) for an unknown `alert_event` or on any
    lookup failure (cold cache + a transient DB hiccup) — degrades to
    the pre-existing header-less rendering, never blocks the alert.
    Renderers only receive `rec`, never the live DB, which is why this
    resolution happens at the PRODUCER (an `async def` background task),
    not inside `_render_deploy_sync` itself.

    2026-11-04 (operator alert-format audit: "some [alerts have] no
    label") — a resolution failure for a KNOWN `alert_event` (one with
    a real entry in `_GENERIC_NOTICE_IDENTITY`) now logs a WARNING.
    Before this, a cold-cache or transient-DB miss silently dropped the
    header forever on that call with no signal anywhere it happened —
    this degrade path is a real, if rare, failure worth seeing in logs,
    not a designed "sometimes blank" behaviour. An unknown `alert_event`
    (no entry at all) is NOT logged — that is the designed, expected
    no-op for a caller this function was never meant to cover.
    """
    entry = _GENERIC_NOTICE_IDENTITY.get(alert_event or "")
    if not entry:
        return {}
    slug, name = entry
    try:
        from backend.api.algo.agent_engine import get_agent_id_by_slug
        agent_id = await get_agent_id_by_slug(slug)
    except Exception:
        agent_id = None
    if agent_id is None:
        _logger.warning(
            f"generic_notice_identity_extra: known alert_event "
            f"'{alert_event}' failed to resolve agent slug '{slug}' — "
            f"this notification will render with no origin header"
        )
        return {}
    return {"agent_id": agent_id, "agent_name": name}


GTT_NOT_ACCEPTED_AGENT = {
    "slug": "gtt-not-accepted-alert",
    "name": "GTT not accepted",
    # 2026-10-25 tag-unification — see FILL_AGENT's comment above.
    # Producer (`template_attach.py`) already tags `["orders", "gtt", "alert"]`.
    "conditions": {"log": {"tag": "alert", "min_level": "ERROR",
                           "where": {"alert_event": "gtt_not_accepted"}}},
    "priority": "urgent",
    "actions": [{"type": "render", "render": "gtt_not_accepted"}],
}

FILL_AGENT = {
    "slug": "fill-alert",
    "name": "Fill alert",
    # 2026-10-25 tag-unification: condition tag moved from the category
    # ("orders") to the additive "alert" tag — `where.alert_event` already
    # uniquely discriminates this agent's producer (`fill_notify.py`,
    # tags=["orders", "alert"]), so the category tag was redundant. See
    # `SEED_VERSION`'s own comment for the full rationale shared by every
    # agent touched in this pass.
    "conditions": {"log": {"tag": "alert", "min_level": "INFO",
                           "where": {"alert_event": "filled", "mode": "live"}}},
    # 2026-10-23 chase robustness (fix #4) — a fill is FYI, never
    # action-required; ntfy channel removed from this seed (cosmetic —
    # the REAL enforcement is the binary action_required gate in
    # _dispatch_agent/_apply_action_required_ntfy_gate, which skips ntfy
    # for every fill record regardless of what's already seeded in the
    # `agents` DB table on an existing deployment).
    #
    # 2026-10 routing-tree plan — channel selection moved to
    # `alert_routing.py`'s per-slug override table (verbatim-identical
    # to the `events` list this dict used to carry directly). `priority`
    # is this agent's own severity token, consulted only as a fallback if
    # the per-slug override were ever removed — see `alert_routing.py`'s
    # module docstring for the full precedence order.
    "priority": "medium",
    "actions": [{"type": "render", "render": "fill"}],
}

USER_REGISTERED_AGENT = {
    "slug": "user-registered-alert",
    "name": "User registered",
    # 2026-10 operator-reported gap: `routes/auth.py`'s self-registration
    # endpoint already wrote a real log line for every new user
    # (`logger.info(f"Auth: registered ...")`), but with no
    # `extra={"tags": ..., "alert_event": ...}` at all — unlike every
    # other alert-producing log call in this codebase. It's not that
    # this condition failed to match; there was nothing for ANY event
    # agent to match against. Fixed at the source in `routes/auth.py`
    # (now tags `["auth"]`, `alert_event="user_registered"`, and builds
    # `title`/`body` for the shared `generic_notice` renderer, same
    # contract every other `generic_notice` caller in `background.py`
    # already follows). The pre-existing direct `send_email()` call in
    # `_notify_admins_new_registration()` is unrelated and unchanged —
    # this is an ADDITIVE Telegram+ntfy channel, not a replacement.
    # 2026-10-25 tag-unification (NOT in the original brief's own
    # enumeration — found during fresh verification and included here
    # under the brief's own stated rule "every agent except the 3
    # excluded ones"; flagged explicitly in this pass's own report).
    # `where.alert_event="user_registered"` already uniquely
    # discriminates this agent, so the "auth" category tag is redundant
    # the same way it is for every other migrated agent. Producer
    # (`routes/auth.py:_registration_alert_extra`) now also tags
    # `"alert"` (additive, alongside its pre-existing "auth" tag).
    "conditions": {"log": {"tag": "alert", "min_level": "INFO",
                           "where": {"alert_event": "user_registered"}}},
    # "high", not "urgent" — needs operator attention (approve/reject
    # the new account) but isn't time-critical the way a live-order
    # failure is; matches this codebase's existing priority convention
    # for "needs a look soon, not a 3x-repeat urgent push" alerts (e.g.
    # `TEMPLATE_ATTACH_HIGH_AGENT`/`GTT_ASYMMETRIC_AGENT`).
    "priority": "high",
    "actions": [{"type": "render", "render": "generic_notice"}],
}

UNROUTED_ALERT_AGENT = {
    "slug": "unrouted-alert-catch-all",
    "name": "Unrouted alert catch-all",
    "description": "Last-resort delivery for any alert that matched no "
                    "specific routing rule, so it is never silently dropped.",
    # Matches exactly the shape `_emit_unrouted_self_alert()` builds —
    # tag "alert" (a real, known alert tag; "agent" is still carried
    # additively too), the reserved `alert_event=_UNROUTED_ALERT_EVENT`
    # ("unrouted_alert"). This is the
    # one place the catch-all's self-alert actually reaches a real
    # channel — without an active agent matching this condition, the
    # self-alert would log correctly (captured by LogStoreHandler) but
    # never fire Telegram/ntfy/email, which is the exact silent-failure
    # mode council item 5 exists to prevent.
    # 2026-10-25 tag-unification (NOT in the original brief's own
    # enumeration — found during fresh verification and included here
    # under the brief's own stated rule "every agent except the 3
    # excluded ones"; flagged explicitly in this pass's own report).
    # Unlike RICH_ALERT_AGENT/BREACH_AGENT (also tag="agent", correctly
    # EXCLUDED from this unification), this agent's own producer
    # (`_emit_unrouted_self_alert` below) is a log-tag event-agent
    # producer like every other one in this file — it writes NOTHING to
    # an AgentEvent DB row, so tagging it "alert" cannot double-count a
    # cycle-agent firing the way it would for those two. The reserved
    # `alert_event=_UNROUTED_ALERT_EVENT` guard in `_looks_alert_shaped()`
    # keys on `alert_event`, not on this tag, so the recursion guard is
    # unaffected by this change.
    "conditions": {"log": {"tag": "alert", "min_level": "WARNING",
                           "where": {"alert_event": "unrouted_alert"}}},
    # 2026-10 routing-tree plan (Step 4 / Task B) — was a hand-duplicated
    # literal of `alert_routing.CATCH_ALL_CHANNELS`, kept in sync only by
    # a test asserting byte-for-byte equality. Now a DEEP copy (fresh
    # dicts, not just a fresh outer list) built directly from that
    # constant at import time, so the two can never silently drift
    # apart again. Deliberately still NOT a live reference and NOT
    # routed through `alert_routing.resolve_channels()` — this agent's
    # own `events` must keep dispatching unconditionally regardless of
    # `priority`/tier plumbing (it has none), and `CATCH_ALL_CHANNELS`
    # carries `gate: False` on every entry (bypasses capability-enabled
    # checks) specifically because it is the last-resort backstop; a
    # future per-slug/tier rule change to `alert_routing.py` must not be
    # able to accidentally mutate this agent's stored content via a
    # shared list/dict reference.
    "events": [dict(ch) for ch in alert_routing.CATCH_ALL_CHANNELS],
    "actions": [{"type": "render", "render": "generic_notice"}],
}

MARKET_STATUS_ALERT_AGENT = {
    "slug": "market-status-alert",
    "name": "Market status",
    "description": "Renders the daily 05:35 IST NSE/MCX open-or-closed "
                    "status report sent by the send_market_status action.",
    # 2026-11-03 fix (operator: "audit all the alerts and make sure
    # every one of them is generated by alert agent... driven by tags,
    # info levels, and messages in the logs") — new, dedicated agent;
    # `_action_live_send_market_status` used to send Telegram/ntfy
    # directly, bypassing the log+event-agent pipeline entirely. A new
    # slug, not a reuse of the pre-existing "market-status-report"
    # BUILTIN_AGENTS schedule placeholder (agent_engine.py) — that row
    # is `kind` cycle/threshold-shaped (a schedule-only sentinel, see
    # CLAUDE.md's "Kind classification" entry) and exists purely for
    # the /agents page's fire_at_time display + pause toggle; it is
    # never evaluated by `run_cycle()` for this report (see its own
    # comment). Reusing its slug here would be two different seeding
    # mechanisms writing the same Agent row under two different `kind`
    # values — this agent is a completely separate row.
    "conditions": {"log": {"tag": "alert", "min_level": "INFO",
                           "where": {"alert_event": "market_status"}}},
    "priority": "info",
    # 2026-11-05 simplification (operator: "move market status onto the
    # static events config now that it no longer needs content-
    # conditional routing") — `events` used to be deliberately omitted
    # here, with the real channel list carried per-record via
    # `_render_market_status`'s `out[4]` (the SAME mechanism BREACH_AGENT
    # needs, because a breach's channel set genuinely varies per firing
    # by severity/account). Market status never had that kind of
    # per-firing variability — its only reason for the dynamic path was
    # a telegram-always/ntfy-if-open decision that the 2026-11-04
    # channel-routing audit already removed (`_action_live_send_market_
    # status` now sends the SAME fixed set — log, telegram, email — every
    # day, open or closed). Moving it back to a plain static `events`
    # list closes the exact gap that caused the false "unrouted
    # alertable record" self-alert (see `_send_channel`'s own fix above):
    # the per-record dynamic path never baked `log_agent_slug` the way
    # the static path's `_dispatch_agent()` resolution does.
    "events": [
        {"channel": "log",      "enabled": True},
        {"channel": "telegram", "enabled": True},
        {"channel": "email",    "enabled": True},
    ],
    "actions": [{"type": "render", "render": "market_status"}],
}

ACTION_NOTIFY_AGENT = {
    "slug": "action-notify-alert",
    "name": "Agent action notify",
    "description": "Renders a DO-clause action's own declared "
                    "notify=[...] channels.",
    # 2026-11-03 fix (operator: "audit all the alerts and make sure
    # every one of them is generated by alert agent... driven by tags")
    # — new, dedicated agent; `actions.py:_al_maybe_notify_action` used
    # to call a send primitive per channel directly, bypassing the
    # log+event-agent pipeline entirely.
    "conditions": {"log": {"tag": "alert", "min_level": "INFO",
                           "where": {"alert_event": "action_notify"}}},
    "priority": "info",
    # `events` deliberately omitted — same reasoning as
    # MARKET_STATUS_ALERT_AGENT above: this agent's real channel list
    # is per-record (`_render_action_notify`'s own `out[4]`, carrying
    # the producer's resolved notify=[...] channel/params selection),
    # never `alert_routing.resolve_channels()`'s static per-agent
    # resolution.
    "actions": [{"type": "render", "render": "action_notify"}],
}

ORDER_CANCELLED_AGENT = {
    "slug": "order-cancelled-alert",
    "name": "Order cancelled",
    "description": "Renders a postback CANCELLED alert that isn't "
                    "chase's own routine cancel-and-replace re-quote.",
    # 2026-11-03 fix (Wave 3, final wave, of the alert-agent audit —
    # operator: "audit all the alerts and make sure every one of them
    # is generated by alert agent... driven by tags") — new, dedicated
    # agent; `orders.py:_opp_send_cancel_alert` used to call
    # send_ntfy_alert directly, bypassing the log+event-agent pipeline.
    "conditions": {"log": {"tag": "alert", "min_level": "WARNING",
                           "where": {"alert_event": "order_cancelled"}}},
    "priority": "high",
    # `events` deliberately omitted — same reasoning as
    # MARKET_STATUS_ALERT_AGENT/ACTION_NOTIFY_AGENT above: this agent's
    # real channel/urgency is the per-record `extra['action_required']`
    # binary gate (always True here — the chase-requote case never
    # reaches this agent at all, filtered out at the producer before
    # any log record is written), applied generically by
    # `_dispatch_record_channels`, never `alert_routing.resolve_
    # channels()`'s static per-agent resolution.
    "actions": [{"type": "render", "render": "order_cancelled"}],
}

GTT_ORPHAN_CANCELLED_AGENT = {
    "slug": "gtt-orphan-cancelled-alert",
    "name": "GTT orphan cancelled",
    "description": "Notifies that an outstanding TP/SL GTT was "
                    "auto-cancelled because its position was already "
                    "closed through another order.",
    # 2026-11-04 fix (operator: "when a position is closed [by any
    # means], the gtt order on that position [should be] closed
    # automatically... also, this should generate an alert in telegram,
    # as there is nothing to act on for ntfy") — `background.py`'s
    # `_oco_cancel_closed_position_gtts()` is the producer. Standalone
    # dedicated agent (same shape as VISITOR_REPORT_AGENT/MARKET_STATUS_
    # ALERT_AGENT above), not folded into `_GENERIC_NOTICE_SPECS` /
    # `_COLLAPSED_DETECTOR_SPECS` — that table's 16/18 counts are pinned
    # byte-for-byte across several tests (test_alert_channel_collapse.py,
    # test_event_agents.py); a 17th/19th entry there is a much larger,
    # unrelated-to-this-fix ripple than one more standalone seeded row.
    "conditions": {"log": {"tag": "alert", "min_level": "INFO",
                           "where": {"alert_event": "gtt_orphan_cancelled"}}},
    "priority": "info",
    "actions": [{"type": "render", "render": "generic_notice"}],
}
_GENERIC_NOTICE_IDENTITY["gtt_orphan_cancelled"] = (
    GTT_ORPHAN_CANCELLED_AGENT["slug"], GTT_ORPHAN_CANCELLED_AGENT["name"],
)


SEED_VERSION = 8
# 2026-11-05 (SEED_VERSION 7 -> 8) — MARKET_STATUS_ALERT_AGENT gains a
# static `events` list (log, telegram, email), replacing the per-record
# dynamic `out[4]` channel override its renderer used to return. An
# existing-row shape change (not a new slug), so this bump is required
# to re-sync the already-seeded prod/dev row. Pre-flight: the row was
# seeded at SEED_VERSION 7 with `events: []` and never operator-edited,
# so `seed_action()`'s hash check sees it as unmodified and takes the
# 'update' branch, not 'modified_skip'.
#
# 2026-11-04 (SEED_VERSION 6 -> 7) — inserts the new GTT_ORPHAN_CANCELLED_
# AGENT row. Standalone dedicated agent (own per-slug channel override in
# alert_routing.py, telegram-only — "nothing to act on"), not a collapsed-
# table entry; see the agent dict's own comment for why.
#
# 2026-10-30 (SEED_VERSION 5 -> 6) — every live `SEEDED_AGENTS` entry gets
# a `description` field (operator: "alert agents don't have description").
# The Automation page's collapsed-row detail line (`automation/+page.
# svelte`, `{#if row.description || row.group}`) was silently omitted for
# every alert-kind row — `event_agents.py`'s seed specs never set this
# field at all, unlike `agent_engine.py`'s cycle-agent seeds, which always
# have. Written the same way `priority` is (direct kwarg on insert, direct
# attribute on update) — NOT added to `event_agent_row_fields()`, which is
# shared with operator-authored agents and whose docstring already states
# `name`/`slug`/`status`/`seed_version` are caller-owned; `description`
# belongs in that same caller-owned category. Deliberately left untouched
# on a `modified_skip` row, same protection as `priority`.
#
# 2026-10-25 — tag-unification: every `kind='event'` seeded agent except
# ERROR_AGENT/RICH_ALERT_AGENT/BREACH_AGENT (explicitly excluded — see
# each one's own dict comment, or the module-level rationale in each
# migrated agent's own comment above) now matches on the single additive
# "alert" tag instead of its own category tag ("orders"/"gtt"/"chase"/
# "mcp"/"deploy"/"summary"/"broker"/"visitor"/"auth"/"agent") — the
# `where.alert_event` clause already uniquely discriminated every one of
# these agents, so the category tag in the condition was redundant. This
# bump rewrites every already-seeded, operator-unmodified row's
# `conditions` to the new shape (see `seed_action()`'s own docstring for
# the hash-based `modified_skip` protection an operator-edited row keeps).
#
# 2026-10-25 — alert-channel collapse (SEED_VERSION 4 -> 5): 16 of the
# previous 23 `SEEDED_AGENTS` entries are retired in favor of 3 generic
# channel agents + `ALERT_EVENT_DISPATCH` — see the block comment
# immediately above `_COLLAPSED_DETECTOR_SPECS`. This bump both (a)
# inserts the 3 new channel-agent rows and (b) drives
# `_retire_collapsed_event_agents()`'s deactivation pass for the 16
# retired slugs — see `seed_event_agents()`'s own call site. Pre-flight
# (`scripts/alert_seed_audit.py`, run 2026-10-25 against prod AND dev):
# all 23 pre-collapse `kind='event'` rows were hash-clean on both
# (zero `modified_skip` risk), so no row's `conditions`/`events`/
# `actions` had ever diverged from what seeding last wrote — the
# deactivation pass below cannot silently discard an operator edit.


def validate_seed_spec(spec: dict) -> list[str]:
    """Errors that would make a seeded agent silently never fire or never render."""
    from backend.api.algo.grammar import LOG_TAG_TOKENS
    known_tags = {t["token"] for t in LOG_TAG_TOKENS}
    errors: list[str] = []
    if not spec.get("slug"):
        errors.append("missing slug")
    if spec.get("kind", "event") != "event":
        errors.append("seeded agents must be kind 'event'")
    for leaf in _log_leaves(spec.get("conditions") or {}):
        tag = (leaf.get("tag") or "")
        if tag not in known_tags:
            errors.append(f"unknown log tag '{tag}'")
        if str(leaf.get("min_level", "INFO")).upper() not in _LEVEL_NAMES:
            errors.append(f"unknown min_level '{leaf.get('min_level')}'")
    for action in spec.get("actions") or []:
        if isinstance(action, dict) and action.get("type") == "render" and action.get("render") not in RENDERS:
            errors.append(f"unknown renderer '{action.get('render')}'")
    for ch in spec.get("events") or []:
        if not isinstance(ch, dict) or ch.get("channel") not in CHANNELS:
            errors.append(f"unknown channel {ch!r}")
    # 2026-10 routing-tree plan — a seeded agent's `priority` (the
    # routing-table matcher key, see `alert_routing.py`'s module
    # docstring) must be a recognised tier, or it would silently never
    # match `_TIER_DEFAULTS` and fall straight through to the catch-all.
    # `None`/absent is fine — every one of the 20 migrated agents also
    # has a per-slug override that's consulted first.
    priority = spec.get("priority")
    if priority is not None and priority not in alert_routing.KNOWN_PRIORITIES:
        errors.append(f"unknown priority '{priority}'")
    return errors


_LEVEL_NAMES = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _log_leaves(cond) -> list[dict]:
    if not isinstance(cond, dict):
        return []
    if "log" in cond and isinstance(cond["log"], dict):
        return [cond["log"]]
    out: list[dict] = []
    for key in ("all", "any"):
        for child in cond.get(key) or []:
            out.extend(_log_leaves(child))
    if "not" in cond:
        out.extend(_log_leaves(cond["not"]))
    return out


# ─────────────────────────────────────────────────────────────────────
# 2026-10-25 alert-channel collapse (council revisit of the 2026-10
# "collapse rejected" decision, see docs/specs/ALERT_AGENTS_DESIGN.md
# §3.9/§3.22) — 16 of the 23 detector specs above (CHASE_CANCEL_AGENT,
# PARTIAL_GTT_AGENT, TEMPLATE_ATTACH_URGENT_AGENT,
# TEMPLATE_ATTACH_HIGH_AGENT, ORDER_FAILURE_AGENT, ORDER_CORRECTION_AGENT,
# TEMPLATE_GUARD_AGENT, TEMPLATE_ATTACH_FAIL_AGENT, MCP_PING_AGENT,
# GTT_ASYMMETRIC_AGENT, OCO_DOUBLE_FIRE_AGENT, TICKER_RECOVERED_AGENT,
# TICKER_DEGRADED_AGENT, GTT_NOT_ACCEPTED_AGENT, FILL_AGENT,
# USER_REGISTERED_AGENT) no longer get their own standalone seeded Agent
# row — their dicts are KEPT, byte-identical, as plain detector data
# (still read by `test_event_agent_tag_unification.py`'s existing
# per-agent condition pins, and by `_build_alert_event_dispatch_table()`
# below), but are removed from `SEEDED_AGENTS` and retired on the next
# reseed (`_RETIRED_SLUGS`, below `SEEDED_AGENTS`).
#
# 7 specs are deliberately NOT collapsed, each for a structural reason
# that does not apply to the 16 above:
#   - ERROR_AGENT: a blanket catch-all keyed on `tag="error"` with NO
#     `alert_event` at all (matches ANY ERROR+/CRITICAL log codebase-
#     wide), carries the repeat-gate (`actions[0]['gate']=True`), and
#     `_error_agent_self_alerted` keys on its literal slug. None of
#     this fits a per-`alert_event` dispatch table.
#   - VISITOR_REPORT_AGENT: its real channel is `telegram_info` only
#     (see `alert_routing._PER_SLUG_OVERRIDES["visitor-report-alert"]`)
#     — a dedicated channel none of the 3 generic channel agents below
#     cover, same reason DEPLOY_SYNC_AGENT/SUMMARY_AGENT stay dedicated.
#   - DEPLOY_SYNC_AGENT / SUMMARY_AGENT: dedicated channels
#     (`telegram_deploy` / `telegram_info` + email), not generic
#     telegram/ntfy/email.
#   - RICH_ALERT_AGENT / BREACH_AGENT: fire from the cycle-agent engine
#     pipeline (not a log-tag producer event), already write a real
#     `AgentEvent` row, and (RICH_ALERT_AGENT) resolve ntfy priority
#     from a per-record tracked tier rather than a static table entry.
#   - UNROUTED_ALERT_AGENT: the condition-match catch-all safety net
#     itself — must keep dispatching unconditionally regardless of the
#     routing tree, exactly as before.
#
# The result: 3 new generic channel agents (`TELEGRAM_CHANNEL_AGENT`/
# `NTFY_CHANNEL_AGENT`/`EMAIL_CHANNEL_AGENT`) + the 7 kept-dedicated
# specs = 10 real seeded agents, down from 23. Per-event toggle on the
# Automation page is lost for the 16 collapsed events — they share one
# activate/deactivate switch per channel now, not one each. This is a
# known, accepted tradeoff (there is no live reader of `trigger_count`/
# `last_triggered_at` for ANY `kind='event'` agent today — only
# `agent_engine.py`'s cycle-agent path writes those columns — so no
# per-type fire-count observability is lost on top of the toggle).
# `extra['alert_event']` on the dispatched/logged record is UNCHANGED
# and still carries the specific type, so the Alerts tab / log history
# still supports per-type filtering even though the Automation page's
# per-agent row count collapses to per-channel.
# ─────────────────────────────────────────────────────────────────────

_COLLAPSED_DETECTOR_SPECS: tuple[dict, ...] = (
    CHASE_CANCEL_AGENT, PARTIAL_GTT_AGENT, TEMPLATE_ATTACH_URGENT_AGENT,
    TEMPLATE_ATTACH_HIGH_AGENT, ORDER_FAILURE_AGENT, ORDER_CORRECTION_AGENT,
    TEMPLATE_GUARD_AGENT, TEMPLATE_ATTACH_FAIL_AGENT, MCP_PING_AGENT,
    GTT_ASYMMETRIC_AGENT, OCO_DOUBLE_FIRE_AGENT, TICKER_RECOVERED_AGENT,
    TICKER_DEGRADED_AGENT, GTT_NOT_ACCEPTED_AGENT, FILL_AGENT, USER_REGISTERED_AGENT,
)


def _alert_event_leaves(spec: dict) -> list[dict]:
    """Every `{alert_event, min_level, where}` leaf a collapsed detector
    spec's own `conditions` matches on — one leaf for a bare
    `{"log": {...}}` condition, one PER BRANCH for the two 2-event specs
    (`TEMPLATE_ATTACH_URGENT_AGENT`: wing_unprotected/wing_hard_reject;
    `TEMPLATE_ATTACH_HIGH_AGENT`: wing_skip/wing_offset_skip) — each
    branch keeps its OWN `min_level`, exactly reproducing what matching
    against that spec's `conditions` directly would have required."""
    out: list[dict] = []
    for leaf in _log_leaves(spec["conditions"]):
        where = leaf.get("where") or {}
        alert_event = where.get("alert_event")
        if alert_event is None:
            continue
        out.append({"alert_event": alert_event, "min_level": leaf.get("min_level", "INFO"), "where": where})
    return out


def _build_alert_event_dispatch_table() -> dict[str, dict]:
    """`alert_event` -> `{min_level, where, render, channels}`.

    Every field is READ, never retyped, from the collapsed spec's own
    `conditions`/`actions` and from `alert_routing._PER_SLUG_OVERRIDES
    [spec['slug']]` — the exact verbatim channel list that spec's own
    (now-retired) standalone Agent row used to resolve to. This is the
    one place §3.9's "conditions/channels would be duplicated" objection
    is actually answered: there is nothing here to retype, because
    nothing here is typed at all — it's derived.

    `TEMPLATE_ATTACH_URGENT_AGENT`/`TEMPLATE_ATTACH_HIGH_AGENT` each
    contribute 2 table entries (one per `alert_event`), sharing that
    one spec's single channel list — exactly matching the brief's own
    "two entries per pair, same channel/render data" instruction.
    """
    table: dict[str, dict] = {}
    for spec in _COLLAPSED_DETECTOR_SPECS:
        render = _render_key(SimpleNamespace(actions=spec["actions"]))
        channels = alert_routing._PER_SLUG_OVERRIDES.get(spec["slug"], [])
        for leaf in _alert_event_leaves(spec):
            table[leaf["alert_event"]] = {
                "min_level": leaf["min_level"], "where": leaf["where"],
                "render": render, "channels": channels, "source_slug": spec["slug"],
            }
    return table


# Public — read by `_dispatch_channel_agent()` above and by
# `test_alert_event_channel_collapse.py`. 18 entries (16 specs, 2 of
# which contribute 2 `alert_event` keys each).
ALERT_EVENT_DISPATCH: dict[str, dict] = _build_alert_event_dispatch_table()


def _build_shared_channel_condition() -> dict:
    """The single `{"any": [...]}` condition all 3 generic channel
    agents share — one `{"log": {"tag": "alert", "min_level": ...,
    "where": ...}}` leaf per `ALERT_EVENT_DISPATCH` entry, reproducing
    EXACTLY the per-event min_level/where match semantics each
    collapsed spec's own condition enforced on its own. This is what
    keeps an unknown/future `alert_event`, or a known one logged BELOW
    its required `min_level`, from matching — both fall through to
    `UNROUTED_ALERT_AGENT`'s catch-all exactly as they did before this
    collapse, instead of the bare `{"tag":"alert","min_level":"INFO"}`
    shape (rejected on review — see this plan's own report) which would
    have (a) matched every alert-tagged record regardless of
    `alert_event`, defeating the catch-all's unknown-event test, and
    (b) dropped `FILL_AGENT`'s `mode: "live"` filter, leaking paper/sim
    fills to Telegram."""
    leaves = [
        {"log": {"tag": "alert", "min_level": entry["min_level"], "where": entry["where"]}}
        for entry in ALERT_EVENT_DISPATCH.values()
    ]
    # A FLAT N-ary {"any": [a, b, c, ...]} list is NOT what the frontend's
    # `a | b | c` parser produces. `parseOrExpr` (agents.js) is a plain
    # LEFT-TO-RIGHT FOLD: node = a; then node = {left: node, right: b} per
    # iteration — left-ASSOCIATIVE, so "a | b | c | d" nests as
    # {"any": [{"any": [{"any": [a, b]}, c]}, d]}, the FIRST leaf deepest,
    # not outermost. (AGENTS_GRAMMAR_EBNF.md's own "Event agents" comment
    # describing a right-nested shape is itself imprecise — the real
    # parser code is authoritative; verified directly against it.)
    # Opening one of these 3 agents in Console and saving with zero real
    # changes decompiles-then-recompiles through that exact parser, so
    # matching its fold here means compute_seed_spec_hash() never
    # misclassifies an untouched row as operator-modified on the next
    # SEED_VERSION bump.
    node = leaves[0]
    for leaf in leaves[1:]:
        node = {"any": [node, leaf]}
    return node


_CHANNEL_AGENT_CONDITION: dict = _build_shared_channel_condition()


def _channel_agent_spec(slug: str, name: str, channel_key: str) -> dict:
    """One of the 3 generic channel agents. `priority` is deliberately
    `None` — `alert_routing._PER_SLUG_OVERRIDES` carries a dedicated
    (display-only) row for each of these 3 slugs, consulted before any
    tier default, and the real dispatch path (`_dispatch_channel_agent`)
    never calls `alert_routing.resolve_channels()` for these slugs at
    all (see that function's own docstring)."""
    return {
        "slug": slug, "name": name,
        "description": f"Delivers alerts routed to the {name.split(': ', 1)[-1]} channel.",
        "conditions": _CHANNEL_AGENT_CONDITION,
        "priority": None,
        "actions": [{"type": "render", "render": _CHANNEL_AGENT_RENDER_SENTINEL, "channel_key": channel_key}],
    }


TELEGRAM_CHANNEL_AGENT = _channel_agent_spec("alert-channel-telegram", "Alert channel: Telegram", "telegram")
NTFY_CHANNEL_AGENT = _channel_agent_spec("alert-channel-ntfy", "Alert channel: ntfy", "ntfy")
EMAIL_CHANNEL_AGENT = _channel_agent_spec("alert-channel-email", "Alert channel: Email", "email")


SEEDED_AGENTS = (
    ERROR_AGENT, DEPLOY_SYNC_AGENT, RICH_ALERT_AGENT, SUMMARY_AGENT, BREACH_AGENT,
    VISITOR_REPORT_AGENT, UNROUTED_ALERT_AGENT, MARKET_STATUS_ALERT_AGENT,
    ACTION_NOTIFY_AGENT, ORDER_CANCELLED_AGENT, GTT_ORPHAN_CANCELLED_AGENT,
    TELEGRAM_CHANNEL_AGENT, NTFY_CHANNEL_AGENT, EMAIL_CHANNEL_AGENT,
)

# The 16 collapsed specs' own slugs — deactivated (never deleted) by
# `_retire_collapsed_event_agents()` on the next `seed_event_agents()`
# run. `seed_event_agents()`'s own insert/update loop only ever touches
# a slug present in `SEEDED_AGENTS` — a slug removed from that tuple is
# otherwise just left alone forever, so without this an already-seeded
# 'active' row for one of these 16 slugs would keep loading in
# `load_agents()` (which filters on `status == 'active'`, not on
# `SEEDED_AGENTS` membership) and double-deliver every one of its
# alert_events alongside the new generic channel agents. Deactivated,
# not deleted: the row (and its `AgentEvent`/audit history, if any) stays
# reversible, and `alert_routing._PER_SLUG_OVERRIDES` still resolves its
# historical channel set correctly for display on the Automation page.
_RETIRED_SLUGS: frozenset[str] = frozenset(spec["slug"] for spec in _COLLAPSED_DETECTOR_SPECS)


def event_agent_row_fields(
    conditions: dict, events: list, actions: list, *,
    scope: str = "per_account", cooldown_minutes: int = 0,
    trade_mode: str = "live", lifespan_type: str = "persistent",
    priority: "str | None" = None,
) -> dict:
    """Shared Agent-row field shape for kind='event' agents — conditions/
    events/actions/kind plus the defaults a log-driven agent needs for
    the columns the engine still requires (scope/cooldown_minutes/
    trade_mode/lifespan_type are meaningless for dispatch — event agents
    bypass agent_engine.run_cycle() entirely — but the Agent model has no
    NULL-able escape hatch for them).

    `priority` (2026-10 routing-tree plan) is the new `Agent.priority`
    column — the routing-table matcher key `alert_routing.py`'s resolver
    falls back to when no per-slug override exists for this agent's
    slug. `None` for an agent that doesn't set one (every migrated
    seeded agent sets it; a legacy/operator-authored row reads back
    `None`, which the resolver treats as "no tier default either, fall
    through to the catch-all" — never a crash).

    Reused by `seed_event_agents()` (code-seeded built-in agents, which
    default to active+live+per_account) and the agents API's create/update
    path for operator-authored event agents (which pass their own
    scope/cooldown_minutes/trade_mode through instead of these defaults).
    Callers still own `slug` / `name` / `status` / `seed_version`, which
    differ by caller and don't belong in a shared row-shape helper.
    """
    return {
        "conditions": conditions or {}, "events": events or [], "actions": actions or [],
        "kind": "event", "scope": scope, "cooldown_minutes": cooldown_minutes,
        "trade_mode": trade_mode, "lifespan_type": lifespan_type, "priority": priority,
    }


def compute_seed_spec_hash(conditions, events, actions) -> str:
    """sha256 of the (conditions, events, actions) a seed write is about
    to put on a row — stored in `Agent.seed_spec_hash` at write time, and
    recomputed against a row's CURRENT content on the next seed attempt
    to detect an operator edit in between (see `seed_action()`'s
    docstring). `sort_keys=True` makes this stable across dict key
    ordering; `default=str` tolerates any non-JSON-native value the same
    way `log_store.py:row_for()` already does for `extra`.

    Deliberately does NOT hash `priority` (2026-10 routing-tree plan) —
    every row already seeded before this plan has a stored hash computed
    without it; adding `priority` to this function would make that old
    hash mismatch the moment this ships, misclassifying every existing
    seeded row as 'modified_skip' (operator-edited) on the very next
    seed attempt, even though nothing was ever edited. `priority` is
    written unconditionally alongside `conditions`/`events`/`actions` in
    `seed_event_agents()`'s own 'update' branch instead — same atomic
    write, just not part of what this hash protects.
    """
    import hashlib
    import json
    payload = json.dumps(
        {"conditions": conditions or {}, "events": events or [], "actions": actions or []},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def seed_action(row, current_version: int = SEED_VERSION) -> str:
    """Decide what seeding does to one agent row: 'insert', 'update',
    'modified_skip', or 'keep'.

    2026-10 fix (council item 3/UX finding): `seed_version` alone does
    NOT protect an operator's UI edit — `update_agent()` never bumps it,
    so a row edited via the Automation page stays at whatever version it
    was last seeded at. The next time `SEED_VERSION` increments for ANY
    unrelated reason, the old logic saw `row.seed_version < current_version`
    and unconditionally overwrote the operator's `conditions`/`events`/
    `actions` back to the hardcoded spec — silently discarding the edit.

    'modified_skip' — `row.seed_version` is behind `current_version`
    (seeding would normally rewrite this row), but the row's CURRENT
    `conditions`/`events`/`actions` no longer hash-match
    `row.seed_spec_hash` (the hash of what seeding last ACTUALLY WROTE
    here) — the row diverged since that write, i.e. an operator edited
    it. The caller must skip the content overwrite for this row.
    `row.seed_spec_hash` is `None` for any row that predates this column
    or was never seeded — treated as "no write history to compare
    against", so it falls through to 'update', identical to this
    function's pre-fix behavior (always update). This is purely
    additive for legacy/pre-migration data.
    """
    if row is None:
        return "insert"
    if int(row.seed_version or 0) >= current_version:
        return "keep"
    stored_hash = getattr(row, "seed_spec_hash", None)
    if stored_hash:
        current_hash = compute_seed_spec_hash(
            getattr(row, "conditions", None), getattr(row, "events", None), getattr(row, "actions", None))
        if current_hash != stored_hash:
            return "modified_skip"
    return "update"


async def seed_event_agents() -> None:
    """Insert missing seeded agents. Rewrite an existing row only when the
    code's seed version is newer AND the row hasn't been operator-
    modified since it was last seeded (see `seed_action()`'s docstring
    for the 'modified_skip' branch)."""
    from backend.api.database import async_session
    from backend.api.models import Agent
    async with async_session() as s:
        for spec in SEEDED_AGENTS:
            problems = validate_seed_spec(spec)
            if problems:
                sys.stderr.write(f"event_agents: seed '{spec.get('slug')}' skipped: {'; '.join(problems)}\n")
                continue
            row = (await s.execute(select(Agent).where(Agent.slug == spec["slug"]))).scalar_one_or_none()
            action = seed_action(row)
            # `spec.get("events", [])` — NOT `spec["events"]` — 2026-10
            # routing-tree plan: every migrated seeded agent's dict no
            # longer carries an `events` key at all (channel selection
            # moved to `alert_routing.py`'s per-slug override table), so
            # a literal `spec["events"]` would raise `KeyError` for every
            # one of them on every startup. `compute_seed_spec_hash()`
            # deliberately still hashes an empty `[]` the same way it
            # always hashed an explicit `events: []` (e.g.
            # `DEPLOY_SYNC_AGENT`/`BREACH_AGENT` before this plan) — the
            # hash's own meaning (detect an operator edit to `conditions`/
            # `events`/`actions` since the last seed write) is unchanged;
            # this migration does not add `priority` to it (see
            # `compute_seed_spec_hash()`'s own docstring for why that
            # would retroactively misclassify every already-seeded row as
            # operator-modified on the next version bump).
            spec_events = spec.get("events", [])
            spec_hash = compute_seed_spec_hash(spec["conditions"], spec_events, spec["actions"])
            if action == "insert":
                s.add(Agent(slug=spec["slug"], name=spec["name"],
                            description=spec.get("description", ""), status="active",
                            seed_version=SEED_VERSION, seed_spec_hash=spec_hash,
                            **event_agent_row_fields(spec["conditions"], spec_events,
                                                      spec["actions"], priority=spec.get("priority"))))
            elif action == "update":
                row.conditions = spec["conditions"]
                row.events = spec_events
                row.actions = spec["actions"]
                row.priority = spec.get("priority")
                row.description = spec.get("description", "")
                row.kind = "event"
                row.seed_version = SEED_VERSION
                row.seed_spec_hash = spec_hash
            elif action == "modified_skip":
                # Bump seed_version so this skip is reported once per
                # code version, not once per restart at the SAME
                # version — `seed_spec_hash` is deliberately left
                # untouched (still the hash of the ORIGINAL seed write)
                # so a FUTURE version bump still correctly detects this
                # same pre-existing operator edit rather than adopting
                # the edited content as a new baseline. `row.priority` is
                # likewise NOT touched here (2026-10 routing-tree plan) —
                # same protection umbrella as `conditions`/`events`/
                # `actions`: nothing about this row's content is rewritten
                # on a 'modified_skip' classification, so this new field
                # needs no separate guard, only the discipline of not
                # adding a `row.priority = ...` line to this branch.
                row.seed_version = SEED_VERSION
                sys.stderr.write(
                    f"event_agents: seed '{spec['slug']}' skipped — row was "
                    f"operator-modified since it was last seeded\n"
                )
                # 2026-10 routing-tree activation plan (Step 6) — the
                # stderr line above is kept (an existing test pins it,
                # and it's still useful on an interactive console), but
                # a startup-time stderr write is otherwise invisible
                # once the process is running as a daemon. Also log a
                # real WARNING through `_EVENTS_LOGGER` (NOT this
                # module's own skip-listed `_logger` — see that
                # variable's own comment — `log_store.py`'s
                # `_SKIP_PREFIXES` would swallow it silently) so a
                # future bump's silent skip shows up in the Log panel.
                # Deliberately NOT tagged with a known alert tag (see
                # `_known_alert_tags()`) and NOT given an `alert_event`
                # — an auto-skipped seed row is an operational/startup
                # notice for the operator to review at their own pace,
                # not something that should page Telegram/ntfy, and an
                # alert-shaped tag here would also risk this record
                # itself being classified "unrouted" by
                # `_looks_alert_shaped()` if no agent's conditions
                # happen to cover it.
                _EVENTS_LOGGER.warning(
                    "event_agents: seed '%s' skipped — row was operator-"
                    "modified since it was last seeded (seed_version bumped "
                    "to %s so this is reported once per code version, not "
                    "once per restart)",
                    spec["slug"], SEED_VERSION,
                )
        await _retire_collapsed_event_agents(s)
        await s.commit()


async def _retire_collapsed_event_agents(s) -> None:
    """2026-10-25 alert-channel collapse — deactivates (never deletes)
    any currently-active `kind='event'` row whose slug is one of the 16
    retired `_RETIRED_SLUGS`. `seed_event_agents()`'s own insert/update
    loop above only ever touches a slug present in `SEEDED_AGENTS` — a
    slug simply removed from that tuple is otherwise never touched
    again, so an already-seeded 'active' row for one of these 16 slugs
    would keep loading in `load_agents()` (filters on `status ==
    'active'`, independent of `SEEDED_AGENTS` membership) and
    double-deliver every one of its alert_events alongside the new
    generic channel agents. A fresh database (where these rows were
    never inserted) makes this a no-op — 0 rows matched. Idempotent:
    re-running against an already-inactive row is a no-op write."""
    from backend.api.models import Agent
    rows = (await s.execute(
        select(Agent).where(Agent.slug.in_(_RETIRED_SLUGS), Agent.status == "active")
    )).scalars().all()
    for row in rows:
        row.status = "inactive"
