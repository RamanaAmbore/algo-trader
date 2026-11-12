"""
Alert utilities — market open/close summaries and delivery helpers for the
v2 agent engine.

Historical context
  The full intra-day loss-alert engine (check_and_alert + _eval_holdings /
  _eval_positions / _eval_negative_funds and their supporting session/rate
  helpers) was retired when the v2 grammar agents took ownership of every
  loss rule. The rules themselves moved verbatim into BUILTIN_AGENTS in
  backend/api/algo/agent_engine.py (loss-* slugs) and are evaluated by
  backend/api/algo/agent_evaluator.py against a V2 Context.

What lives here now
  - send_summary()   — portfolio open/close summary (called directly from
                        backend.api.background._task_performance / _task_close;
                        not an agent).
  - _tg_alert_body() / _email_alert_body()
                     — narrow Telegram <code> block and coloured HTML table
                        formatters. Consumed by the v2 agent engine's rich
                        alert path (agent_engine._v2_send_rich_alert).
  - dispatch_payload() — builds Telegram and email payloads for summaries and rich alerts (pure)

Secrets (secrets.yaml)
  telegram_bot_token  bot token from @BotFather
  telegram_chat_id    group chat_id (negative integer for groups)
  alert_emails        list of email addresses to notify

Message type prefixes
  Telegram : Open | Agent | Close
  Email    : RamboQuant Open: | RamboQuant Agent: | RamboQuant Close:
"""

import atexit
import hashlib
import html
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import TYPE_CHECKING

import requests

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import secrets, config, is_enabled, pytest_alert_transport_blocked

if TYPE_CHECKING:
    from collections.abc import Callable
    import redis as _redis_t

# ---------------------------------------------------------------------------
# Redis client — lazy singleton for cross-restart cooldown persistence.
# Falls back gracefully when Redis is unavailable (connection refused, not
# installed, etc.) so alert delivery is never blocked by a Redis outage.
# ---------------------------------------------------------------------------

_redis_client: "None | _redis_t.Redis" = None
_redis_init_lock = Lock()
_redis_available: bool = True   # pessimistic flip on first failure


def _get_redis() -> "None | _redis_t.Redis":
    """Return the module-level sync Redis client, or None on any error.

    Uses a lazy singleton so import-time (test) environments without a
    running Redis don't fail.  Connection URL read from secrets.yaml key
    ``redis_url`` or defaults to ``redis://localhost:6379/0``.
    """
    global _redis_client, _redis_available
    if not _redis_available:
        return None
    if _redis_client is not None:
        return _redis_client
    with _redis_init_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            import redis
            url = secrets.get("redis_url", "redis://localhost:6379/0") or "redis://localhost:6379/0"
            client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1, decode_responses=False)
            # Ping to verify connectivity at init time.
            client.ping()
            _redis_client = client
            logger.info(f"Redis client connected ({url})")
        except Exception as _e:
            logger.warning(f"Redis unavailable — order-alert cooldown falls back to in-process dict: {_e}")
            _redis_available = False
    return _redis_client

logger = get_logger(__name__)

# Dedicated logger for records meant to flow into the log_store ->
# event_agents dispatch pipeline (ORDER_FAILURE_AGENT, the summary agent).
# `logger` above (name `backend.shared.helpers.alert_utils`) is one of
# `log_store.py`'s `_SKIP_PREFIXES` -- LogStoreHandler.emit() drops any
# record from that name unconditionally, before tags/dispatch are ever
# considered. That skip-list entry exists to stop this module's OWN
# internal diagnostics (Redis/Telegram/email send status, all the plain
# logger.* calls elsewhere in this file) from re-entering the alert
# pipeline -- it was never meant to also swallow a record whose entire
# purpose IS to trigger dispatch. `_send_order_failure_messages()` and
# `send_summary()` are the only two call sites in this module that build
# a dispatch-shaped record (`extra={"tags": [...], "alert_event": ...}`);
# both now log through this logger instead. Reuses the exact same
# non-skip-listed logger name event_agents.py's own `_EVENTS_LOGGER`
# already uses for the identical purpose (self-logged records meant to
# flow back into the pipeline) -- one convention, not a new one.
# Confirmed via git history: order-failure and summary alerts have had
# ZERO channel delivery since 2026-10-06 (commit 54860caa added this
# skip-list entry), because `_send_order_failure_messages`/`send_summary`
# were migrated off direct `_send_telegram()`/`send_email()` calls to
# this log-and-dispatch model back on 2026-07-23 (commit 5f0a6d5c) --
# the two changes, three months apart, silently combined into this gap.
_DISPATCH_LOGGER = get_logger("backend.api.algo.events")

# Bounded SMTP thread pool — caps concurrent outbound connections to
# Hostinger's SMTP relay (30s timeout each). Four workers handle normal
# burst (14 agents × 3 recipients) without spawning an unbounded number
# of daemon threads under fire.  Fire-and-forget; the wrapper logs errors.
_SMTP_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ramboq-smtp")
# On SIGTERM / systemctl restart, drain in-flight submits without
# blocking indefinitely.  wait=False so the process can exit promptly
# while in-flight threads may still be running their final smtp.quit().
atexit.register(lambda: _SMTP_EXECUTOR.shutdown(wait=False))


# ---------------------------------------------------------------------------
# Alert recipient resolution — merges DB-derived emails with static config.
#
# The recipient list is the union of:
#   - every designated user's email (always — designated has no opt-out)
#   - every admin user with receive_alerts=True
#   - secrets.alert_emails (legacy static list, kept as a fallback so an
#     operator can still receive alerts when the DB has no rows yet)
#
# We cache the DB part so the sync `_dispatch` path doesn't have to query
# the database on every alert. The cache is refreshed on startup, on
# every user-mutation that could affect routing (admin endpoints), and
# periodically by a background task as a safety net.
# ---------------------------------------------------------------------------

_alert_recipients_cache: list[str] = []
_alert_recipients_lock = Lock()


def _dedup_emails(emails: list[str]) -> list[str]:
    """Return a deduplicated, order-preserving list of non-empty email strings."""
    seen: set[str] = set()
    out: list[str] = []
    for e in emails:
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def get_alert_recipients() -> list[str]:
    """Sync — returns the current merged recipient list (designated +
    opted-in admins + secrets fallback), deduped. Used by every alert
    dispatch path (loss / agent / summary).

    Branch-aware override — on non-main branches the recipient list
    is REPLACED (not merged) with `dev_alert_emails` from secrets so
    test alerts don't reach the operator's prod inbox. Falls back to
    the regular list when the dev override is empty / missing."""
    from backend.shared.helpers.utils import is_prod_branch
    if not is_prod_branch():
        dev_emails = secrets.get('dev_alert_emails', []) or []
        if dev_emails:
            return _dedup_emails(dev_emails)
        # No dev override configured — fall through to prod list so
        # alerts still go somewhere visible during dev testing.

    with _alert_recipients_lock:
        db_part = list(_alert_recipients_cache)
    static_part = secrets.get('alert_emails', []) or []
    return _dedup_emails(db_part + static_part)


def get_market_recipients() -> list[str]:
    """Sync — returns the recipient list for PUBLIC-WEBSITE inbound mail
    (contact form, market inquiries). Operator-facing alerts (loss /
    agent / order summary) use `get_alert_recipients()` instead; the
    two audiences are kept separate so the trading-ops inbox stays
    clean of marketing / inbound-lead noise.

    Reads `market_emails` from secrets.yaml. Falls back to smtp_user
    if the key is missing or empty (defensive — never silently swallow
    a contact submission)."""
    raw = secrets.get('market_emails', []) or []
    seen: set[str] = set()
    out: list[str] = []
    for e in raw:
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    if not out:
        fallback = secrets.get('smtp_user', '')
        if fallback:
            out.append(fallback)
    return out


async def refresh_alert_recipients() -> None:
    """Async — re-query the users table for active, non-terminated rows
    that should receive alerts: every designated user plus every admin
    with receive_alerts=True. Updates the in-process cache."""
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import User
    try:
        async with async_session() as session:
            result = await session.execute(
                select(User.email).where(
                    User.is_active.is_(True),
                    User.terminated_at.is_(None),
                    User.suspended_at.is_(None),
                    User.email.is_not(None),
                    (
                        (User.role == "designated")
                        | ((User.role == "admin") & (User.receive_alerts.is_(True)))
                    ),
                )
            )
            emails = [e for (e,) in result.fetchall() if e]
    except Exception as exc:  # noqa: BLE001 — defensive; never break alert path
        logger.warning(f"Alert recipient refresh failed: {exc}")
        return
    with _alert_recipients_lock:
        _alert_recipients_cache.clear()
        _alert_recipients_cache.extend(emails)
    logger.info(f"Alert recipients refreshed: {len(emails)} from DB")

_MSG_TYPES = {
    'open':  ('Open Summary',  'RamboQuant Open Summary: '),
    'alert': ('Agent',          'RamboQuant Agent: '),
    'close': ('Close Summary',  'RamboQuant Close Summary: '),
}


def _send_telegram(
    message: str, parse_mode: "str | None" = None, *,
    token: "str | None" = None, chat_id: "str | None" = None,
):
    """Plain-text Telegram send (2026-10 reversal — no HTML parse mode
    anywhere in the pipeline; every caller already builds a tag-free
    message). `parse_mode` is kept as a parameter (default `None`, so
    no `parse_mode` key is sent at all) only so a future caller can
    still opt back in explicitly; nothing in this codebase does.

    `token`/`chat_id` let a caller post to a DIFFERENT bot/chat than the
    default ops channel (secrets `telegram_bot_token`/`telegram_chat_id`)
    — e.g. the dedicated deploy-alerts channel
    (`telegram_bot_token_deploy`/`telegram_chat_id_deploy`, see
    `event_agents.py:_send_telegram_deploy_html`). Omitted (the default),
    this is byte-identical to the pre-existing single-channel behavior.
    """
    import logging
    _log = logging.getLogger('backend.api.background')
    if pytest_alert_transport_blocked():
        _log.info("Telegram skipped — PYTEST_RUNNING guard (no alert_transport opt-in)")
        return
    # Dev-idle suppression — when dev's engine is idle (no operator
    # picked a mode), no market alerts should fire. Deploy notifications
    # are sent by webhook/notify_deploy.py which doesn't go through this
    # path, so they keep working. Prod (main branch) never enters idle
    # so prod alerts are unaffected.
    try:
        from backend.shared.helpers.utils import is_engine_idle
        if is_engine_idle():
            _log.info("Telegram skipped — engine idle (dev)")
            return
    except Exception:
        # Helper not importable yet (startup race) — fall through to
        # the existing is_enabled gate.
        pass
    if not is_enabled('telegram'):
        _log.info("Telegram skipped — disabled for this environment")
        return
    token = token or secrets.get('telegram_bot_token', '')
    chat_id = chat_id or secrets.get('telegram_chat_id', '')
    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload: dict = {"chat_id": chat_id, "text": message}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = requests.post(url, json=payload, timeout=10)
        if resp.ok:
            _log.info("Telegram alert sent")
        else:
            _log.error(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        _log.error(f"Telegram error: {e}")


def _send_telegram_info(message: str):
    """Send to the RamboQuant alerts Telegram channel (telegram_chat_id_ramboquant).
    Falls back to the ops channel keys if the dedicated ramboquant keys are absent.
    Shares the same engine-idle suppression and is_enabled gate as _send_telegram.
    """
    import logging
    _log = logging.getLogger('backend.api.background')
    if pytest_alert_transport_blocked():
        _log.info("Telegram info skipped — PYTEST_RUNNING guard (no alert_transport opt-in)")
        return
    try:
        from backend.shared.helpers.utils import is_engine_idle
        if is_engine_idle():
            _log.info("Telegram info skipped — engine idle (dev)")
            return
    except Exception:
        pass
    if not is_enabled('telegram'):
        _log.info("Telegram info skipped — disabled for this environment")
        return
    token   = secrets.get('telegram_bot_token_ramboquant') or secrets.get('telegram_bot_token', '')
    chat_id = secrets.get('telegram_chat_id_ramboquant')   or secrets.get('telegram_chat_id', '')
    if not token or not chat_id:
        logger.warning("Telegram info channel not configured — skipping")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        if resp.ok:
            _log.info("Telegram info alert sent")
        else:
            _log.error(f"Telegram info send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        _log.error(f"Telegram info error: {e}")


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_plain(s: str) -> str:
    """Convert a Telegram-HTML-formatted string (``<b>``, ``<code>`` tags,
    plus any ``html.escape()``-introduced entities) into plain text suitable
    for channels with no HTML rendering (ntfy).

    Order matters: strip tags FIRST, then unescape entities. The strings
    this is meant for (``_dispatch``'s ``telegram_msg`` and friends) run
    their dynamic content through ``html.escape()`` before embedding it —
    so any literal ``<``/``>``/``&`` that belongs to the DATA survives as
    ``&lt;``/``&gt;``/``&amp;`` and is untouched by the tag-stripping regex;
    unescaping afterwards restores it correctly. Unescaping first would turn
    that ``&lt;`` back into ``<`` before the regex runs, risking corruption.

    Only intended for strings built as HTML by this module's own message
    builders (where the docstring/contract says "HTML-safe"). Do NOT run
    this over arbitrary user/operator-authored text (e.g. agent condition
    strings like ``pnl < -5000``) — those were never escaped, so a bare
    comparison operator would be misread as a tag boundary.
    """
    if not s:
        return s
    return html.unescape(_HTML_TAG_RE.sub("", s))


def _fixed_table(headers, rows):
    """Render a list of string-tuple rows as a fixed-width monospace table (for Telegram)."""
    col_widths = [max(len(h), max((len(r[i]) for r in rows), default=0))
                  for i, h in enumerate(headers)]

    def fmt(r):
        return "  ".join(str(v).ljust(col_widths[i]) for i, v in enumerate(r))

    sep = "  ".join("─" * w for w in col_widths)
    return "\n".join([fmt(headers), sep] + [fmt(r) for r in rows])


# ── Algo dark palette — mirrors the on-screen pages so the inbox reads
#    in the same visual rhythm as the dashboard. Operator: "The email
#    theme should align with overall algo pages color scheme.
#    presenlty is blue gray color."
_EMAIL_BG          = "#0c1830"   # outer page (algo body navy)
_EMAIL_PANEL_BG    = "#152033"   # card surface
_EMAIL_PANEL_ALT   = "#1a2640"   # alt-row stripe
_EMAIL_BORDER      = "#3b4868"   # subtle separator
_EMAIL_AMBER       = "#fbbf24"   # primary accent (matches navbar / TOTAL row)
_EMAIL_AMBER_SOFT  = "#f59e0b"   # darker amber for headings
_EMAIL_TEXT        = "#c8d8f0"   # body text (slate-blue light)
_EMAIL_TEXT_MUTED  = "#a3b9d0"   # secondary text
_EMAIL_GREEN       = "#4ade80"
_EMAIL_RED         = "#f87171"


# `_html_table` RELOCATED to backend/api/algo/grammar_tokens/
# format_html_table.py (Wave 1, DSL-token-unification plan, Part B —
# formats). Not re-exported at module top here, since that would make
# this shared-layer module (backend/shared/) depend on backend/api/algo/
# at IMPORT time — the two real callers below (`_build_summary_email`,
# `order_failure_messages`) each do a LAZY (function-body) import
# instead, which only resolves at call time, well after both modules are
# already loaded.


def _branch_banner_html(branch: str) -> str:
    """Return a prominent HTML banner for non-main branches."""
    return (
        f"<div style='background-color:#3a2c10;border:1px solid {_EMAIL_AMBER};"
        f"border-radius:4px;padding:8px 14px;margin-bottom:12px;"
        f"font-family:sans-serif;font-size:13px;color:{_EMAIL_AMBER}'>"
        f"&#9888; <strong>Non-production branch: {branch}</strong>"
        f"</div>"
    )


def _sim_banner_html() -> str:
    """Red banner shown on every simulated-market email."""
    return (
        f"<div style='background-color:#3a1010;border:1px solid {_EMAIL_RED};"
        f"border-radius:4px;padding:8px 14px;margin-bottom:12px;"
        f"font-family:sans-serif;font-size:13px;color:{_EMAIL_RED}'>"
        "&#128680; <strong>SIMULATOR RUN — fabricated market data, not a real alert.</strong>"
        "</div>"
    )


def _replay_banner_html() -> str:
    """Red banner shown on every replay-run email (2026-10-31, operator:
    "the alerts should show paper and simulation mode explicitly in
    alert notification. wire it"). Mirrors _sim_banner_html exactly —
    a replay run evaluates historical data, not the live market, and
    must never read as indistinguishable from a real breach."""
    return (
        f"<div style='background-color:#3a1010;border:1px solid {_EMAIL_RED};"
        f"border-radius:4px;padding:8px 14px;margin-bottom:12px;"
        f"font-family:sans-serif;font-size:13px;color:{_EMAIL_RED}'>"
        "&#128680; <strong>REPLAY RUN — historical backtest data, not a real alert.</strong>"
        "</div>"
    )


def _email_message(
    *, email_prefix_full: str, branch_tag: str, mode_tag: str,
    subject_detail: str, sim_mode: bool, branch: str,
    tg_prefix_full: str, ist_display: str, email_table_html: str,
    agent_line: str = '', replay_mode: bool = False, label_word: str = "Agent",
) -> tuple[str, str]:
    """Subject and HTML body of an agent or summary email. Pure.

    `agent_line` — the real firing agent's "Agent: {name} (#{id})" label.
    2026-10 audit fix: shown ABOVE the dual-tz `ist_display` header
    paragraph in the BODY (was below it) — origin leads, matching the
    Telegram body's same reordering. Empty string (the default) when
    the alert has no single originating agent (summaries).

    2026-11-02 subject reshape (operator), SCOPED to the subject only.
    Only the SUBJECT's overall *shape* (prefix/ordering) changes HERE,
    in this `if agent_line:` branch. When a real agent identity is
    known (`agent_line` truthy — same signal `agent_line_html` already
    gates on), the subject becomes `"Agent: {subject_detail}
    {branch_tag}{mode_tag} | {ist_display}"`, dropping the old
    "RamboQuant Agent: " prefix duplication and adding the dual-tz
    timestamp the subject never carried before. `branch_tag`/
    `mode_tag` are threaded through (same ordering convention as the
    Telegram/ntfy merged line in `dispatch_payload`) so a PAPER/SIM/
    REPLAY/non-main-branch alert's email subject still carries that
    fact — dropping them here would have been the only place in the
    whole email that lost the mode tag entirely, since the body's bold
    paragraph never carried `mode_tag` either (see CLAUDE.md's "Every
    alert must explicitly show paper/sim/replay mode" invariant). This
    is deliberately NOT applied to the agent-less (summary) branch
    below, which keeps its prior shape unchanged: `send_summary()`'s
    own `subject_detail` already has `ist_display` baked into it
    (`f"{label} — {ist_display}"` in `send_summary`), so re-appending
    `ist_display` here would duplicate the timestamp.

    2026-11-04 (operator alert-format audit) — the label word here was
    briefly "Alert:" (2026-11-02); reverted to "Agent:" to match every
    other renderer in the system. See `_relabel_agent_label_to_alert()`
    for the matching reversion on the Telegram/ntfy merged line and
    the HTML body header paragraphs."""
    if agent_line:
        _mode_sfx = f" {mode_tag}" if mode_tag else ""
        # 2026-11-08 audit fix (item 5) — `subject_detail` for a summary
        # (`send_summary()`'s own `f"{label} — {ist_display}"`) already
        # embeds the dual-tz timestamp. Since 2026-11-06, a summary
        # carries a process/agent identity, so it now reaches this
        # branch too — appending `| {ist_display}` unconditionally would
        # duplicate the timestamp in the subject. Only append it when
        # `subject_detail` doesn't already carry it.
        _ts_sfx = "" if ist_display in subject_detail else f" | {ist_display}"
        subject = f"{label_word}: {subject_detail}{branch_tag}{_mode_sfx}{_ts_sfx}"
    else:
        subj_pfx = f"{email_prefix_full}{branch_tag}{(' ' + mode_tag) if mode_tag else ''}"
        subject = (
            f"{subj_pfx}{subject_detail}"
            if (branch_tag or mode_tag)
            else f"{email_prefix_full}{subject_detail}"
        )
    banners = _build_email_banners(sim_mode, branch, replay_mode=replay_mode)
    # 2026-11-02 "Agent" -> "Alert" relabel (operator follow-up), SCOPED
    # to these two header paragraphs ONLY — layout/structure (two
    # separate paragraphs, not merged) is unchanged; only the label word
    # itself changes, via the same `_relabel_agent_label_to_alert()`
    # helper the Telegram/ntfy merge uses (see its own docstring for the
    # "never touches format_notification_header or any other caller"
    # scope boundary). `tg_prefix_full` is relabeled unconditionally
    # here (not gated on `agent_line`) because it is the SAME label
    # word in both the known- and agent-less-identity case for this
    # msg_type — "Agent" / "SIMULATOR Agent" / "REPLAY Agent" — and the
    # open/close summary prefixes ("Open Summary", ...) never contain
    # that word, so the relabel is a no-op for them.
    agent_line_html = (
        f"<p style='font-size:13px;color:{_EMAIL_TEXT_MUTED};margin:0 0 10px 0'>"
        f"{html.escape(_relabel_agent_label_to_alert(agent_line))}</p>"
        if agent_line else ''
    )
    tg_prefix_full_label = _relabel_agent_label_to_alert(tg_prefix_full)
    html_body = (
        f"<html><body style='font-family:sans-serif;background-color:{_EMAIL_BG};"
        f"color:{_EMAIL_TEXT};margin:0;padding:18px'>"
        f"<div style='max-width:760px;margin:0 auto'>"
        f"{banners}"
        f"{agent_line_html}"
        f"<p style='font-size:14px;color:{_EMAIL_AMBER};letter-spacing:0.04em;"
        f"margin:0 0 14px 0'>"
        f"<b>{tg_prefix_full_label}{branch_tag} — {ist_display}</b></p>"
        f"{email_table_html}"
        f"</div>"
        f"</body></html>"
    )
    return subject, html_body


def _build_tg_warning_block(sim_mode: bool, branch: str,
                            replay_mode: bool = False) -> str:
    """Build the Telegram warning block for simulator/replay and
    non-main-branch runs. Plain text (2026-10 reversal) — the literal
    emoji character replaces the old `&#128680;` HTML numeric entity
    (meaningless once nothing requests `parse_mode: HTML`), and the
    `<b>` wraps are dropped.

    `replay_mode` (2026-10-31, operator: "the alerts should show paper
    and simulation mode explicitly in alert notification. wire it") —
    replay evaluates historical data, same "not a real breach" concern
    as sim_mode; mutually exclusive with it in practice (one cycle run
    is either sim or replay, never both), so no precedence issue."""
    lines = []
    if replay_mode:
        lines.append("🚨 REPLAY RUN — historical backtest data, not live")
    elif sim_mode:
        lines.append("🚨 SIMULATOR RUN — fabricated market data")
    if branch != 'main':
        lines.append(f"⚠ Branch: {branch}")
    return ("\n" + "\n".join(lines)) if lines else ''


def _build_email_banners(sim_mode: bool, branch: str,
                         replay_mode: bool = False) -> str:
    """Build HTML banners for the email body (simulator/replay and
    non-main-branch). See `_build_tg_warning_block` for the
    replay_mode rationale."""
    banners = ''
    if replay_mode:
        banners += _replay_banner_html()
    elif sim_mode:
        banners += _sim_banner_html()
    if branch != 'main':
        banners += _branch_banner_html(branch)
    return banners


# ---------------------------------------------------------------------------
# Agent-origin resolution — "who really fired this" for every alert.
#
# `resolve_origin()` is a SYNC, no-I/O function (it is called from deep
# inside the renderer/dispatch hot-path — event_agents.py renderers are
# plain sync functions, never awaited for a DB round-trip). The manual
# sentinel Agent row's id is resolved ONCE at startup (and re-resolvable
# on demand) into `_MANUAL_AGENT_ID_CACHE` so a caller that only has a
# bare `agent_id` int (no name — e.g. order_failure_messages) can still
# recognise "this IS the manual sentinel" without a DB hit per alert.
# A caller that already resolved a real `agent_name` (e.g.
# fill_notify.notify_fills's own DB round-trip) gets the sentinel-by-name
# check for free, with no dependency on the cache being warm.
# ---------------------------------------------------------------------------

_MANUAL_SENTINEL_NAME = "manual operator order"
_MANUAL_AGENT_ID_CACHE: dict = {"id": None}


async def warm_manual_agent_id_cache() -> None:
    """Resolve + cache the manual-sentinel Agent row's id once.

    Called at startup (`backend/api/database.py`'s init sequence) so the
    very first alert after a restart already recognises the sentinel by
    id. Never raises — a warm failure just leaves the id-based check
    unavailable; the name-based check in `resolve_origin()` still works
    for any caller that already has `agent_name` resolved.
    """
    try:
        from backend.api.algo.agent_engine import get_agent_id_by_slug
        aid = await get_agent_id_by_slug("manual")
        _MANUAL_AGENT_ID_CACHE["id"] = aid
    except Exception as e:
        logger.warning(f"warm_manual_agent_id_cache failed: {e}")


def resolve_origin(agent_id: "int | None", agent_name: "str | None" = None) -> str:
    """Return the real originating agent's name, or the literal string
    "manual" when the order/alert genuinely has no real originating
    agent.

    Resolution order:
      1. A real, non-sentinel `agent_name` is trusted as-is and returned
         directly — this covers every caller that already resolved a
         name via the DB (fill_notify) even when `agent_id` itself
         wasn't carried along with it (a real, documented shape —
         `event_agents.py:_render_fill` rebuilds its row from a replayed
         log record that only carries `agent_name`, never `agent_id`).
      2. `agent_name` matching the manual sentinel's own row name
         ("Manual operator order", case-insensitive) -> "manual".
      3. No name at all and `agent_id is None` -> "manual" (the bare
         manual ticket/basket case — no Agent row was ever involved).
      4. No name, but `agent_id` matches the cached manual-sentinel id
         (`warm_manual_agent_id_cache`) -> "manual".
      5. No name, a real non-sentinel `agent_id` -> "Agent #{id}" — an
         unresolved name must NEVER be misread as "no agent at all".
    """
    if agent_name:
        if agent_name.strip().lower() == _MANUAL_SENTINEL_NAME:
            return "manual"
        return agent_name
    if agent_id is None:
        return "manual"
    cached_manual_id = _MANUAL_AGENT_ID_CACHE.get("id")
    if cached_manual_id is not None and agent_id == cached_manual_id:
        return "manual"
    return f"Agent #{agent_id}"


def _source_extra(
    agent_name: "str | None", agent_id: "int | None", source_type: "str | None" = None,
    source_name: "str | None" = None, trigger_known: bool = True,
) -> dict:
    """Build the `extra`-shaped dict `resolve_source()` consumes, from
    the scalar params `format_notification_header()`/`dispatch_payload()`
    already take. The ONLY new concept here is `trigger_known`: a bare
    `agent_id=None` is genuinely ambiguous — it means "manual" for an
    order-scoped alert (chase/template-attach/order-poll/order-cancelled)
    but means "no trigger at all" for a background process with no
    cause (ticker watchdog, deploy-sync, market status/summary). Callers
    that know which case they're in say so explicitly; the default
    (`True`) preserves every pre-existing caller's behavior byte-for-byte,
    since every one of them already treats a bare `None` as "manual"."""
    extra: dict = {}
    if source_type and source_name:
        extra["source_type"] = source_type
        extra["source_name"] = source_name
    if trigger_known:
        extra["agent_id"] = agent_id
        extra["agent_name"] = agent_name
    return extra


def resolve_source(extra: dict) -> "tuple[str, str, str | None, str | None]":
    """SSOT for alert identity composition — the one function that
    decides what label a header shows AND what tag a trailing tag line
    carries for ANY alert in the system (2026-11-06, operator: "this is
    ssot. the alert agent framework should use this").

    Returns `(label_word, label_sentence, process_tag, action_tag)`:
      - `label_word` — "Agent" or "Source", the word before the colon.
      - `label_sentence` — the full label text after the colon (may
        fold in "triggered manually"/"triggered by {agent}").
      - `process_tag` — the process's own name for the trailing tag
        line, `None` when there is no process layer.
      - `action_tag` — the resolved action agent/"manual" string, `None`
        when nothing is known to have triggered this (a genuinely
        agent-less background job — never fabricated).

    Branch 1 — `source_type in ("process", "error")` and `source_name`
    truthy: this is a PROCESS (or the ERROR_AGENT catch-all, which is
    "process"-shaped with its name derived from the logger). The action
    tag is resolved ONLY when the caller actually supplied an
    `agent_id`/`agent_name` KEY (checked via `in extra`, not via
    truthiness — a bare `None` VALUE for either key still means "the
    trigger is known to be manual", exactly like every order-scoped
    renderer already relies on via `resolve_origin(None, None) ==
    "manual"`). A caller that never set either key at all (the 8
    background-process producers with no known cause) gets `action_tag
    = None` and a trigger-free sentence/tag set — never a fabricated
    "triggered manually".

    Branch 2 — everything else: `label_word` is "Agent". When the
    caller's `extra` dict carries NEITHER an `agent_id` NOR an
    `agent_name` KEY at all (checked via `in extra`, not truthiness —
    same convention as branch 1's `has_trigger_key`), nothing is known
    about who triggered this, so `label_sentence` is the literal
    "unknown" and `action_tag` is `None` — never the fabricated
    "manual" fallback (2026-11-08 audit fix: this general-purpose guard
    closes the gap for ANY future record with no identity key at all,
    independent of the two concrete producers — the unrouted self-alert
    and the MCP ping — that are fixed at their own source elsewhere).
    When at least one of the two keys IS present (even with a `None`
    value — the genuine "explicitly manual" case every order-scoped
    alert already relies on via `resolve_origin(None, None) ==
    "manual"`), `label_sentence` is `resolve_origin(agent_id,
    agent_name)` exactly as before.
    """
    source_type = extra.get("source_type")
    source_name = extra.get("source_name")
    agent_id = extra.get("agent_id")
    agent_name = extra.get("agent_name")
    if source_type in ("process", "error") and source_name:
        has_trigger_key = "agent_id" in extra or "agent_name" in extra
        action_tag = resolve_origin(agent_id, agent_name) if has_trigger_key else None
        process_tag = source_name
        if action_tag is None:
            label_sentence = source_name
        elif action_tag == "manual":
            label_sentence = f"{source_name} triggered manually"
        else:
            label_sentence = f"{source_name} triggered by {action_tag}"
        if source_type == "error":
            # Distinguishes a bare logger-derived name (the ERROR_AGENT
            # catch-all) from a genuine named process in the HEADER text
            # only — the trailing tag stays the plain process name
            # (`#chase`, not `#chase_error`), matching the plan's own
            # "Bare, uncategorized error" template.
            label_sentence = f"{label_sentence} (error)"
        return "Source", label_sentence, process_tag, action_tag
    has_trigger_key = "agent_id" in extra or "agent_name" in extra
    if not has_trigger_key:
        return "Agent", "unknown", None, None
    action_tag = resolve_origin(agent_id, agent_name)
    return "Agent", action_tag, None, action_tag


def _relabel_agent_label_to_alert(text: str) -> str:
    """No-op (2026-11-04, operator alert-format audit: "the alerts don't
    have standard format. some have alert label. some agent label.
    some no label") — the 2026-11-02 "Agent" -> "Alert" word relabel
    this function used to perform, scoped to `dispatch_payload()`'s
    merged Telegram/ntfy header line and `_email_message()`'s two
    header paragraphs, is reverted. Every renderer now says "Agent:"
    — the convention 16 of the 18 real alert renderers already used;
    "Alert:" was a late, narrow addition for exactly two of them
    (rich-alert, breach) plus a conditional third (summary), which is
    exactly the inconsistency the operator flagged. This function is
    kept (rather than deleted at its call sites) only so a future
    operator decision to re-introduce a distinct label has one place
    to change; it is never called with new relabeling logic without an
    explicit instruction to do so.

    `format_notification_header()` was never affected by the old
    relabel and needs no change here — it already always says
    "Agent: ..."/"Agent #{id}".
    """
    return text


def format_notification_header(
    agent_name: "str | None", agent_id: "int | None", *, ist_display: "str | None" = None,
    source_type: "str | None" = None, source_name: "str | None" = None,
    trigger_known: bool = True,
) -> str:
    """Single source of truth for the "who fired this + when" header shown
    at the top of every notification body (Telegram/email/ntfy).

    ALWAYS emits a label line as the FIRST line, followed by the dual-tz
    timestamp. With no `source_type`/`source_name` (every pre-existing
    caller), the label is "Agent: {origin}" — `origin` resolved via
    `resolve_origin()`, so a genuinely agent-less alert reads
    "Agent: manual" rather than silently omitting any origin at all
    (2026-10 operator requirement: "make sure it has agent info in the
    beginning... if it is not by agent, add manual").

    When BOTH `agent_name` and `agent_id` are truthy and resolve to a
    real (non-manual) agent, the line keeps its original
    "Agent: {name} (#{id})" shape — unchanged from before this fix, so
    every existing caller that already has a full agent identity pair
    (rich alerts, breach alerts) renders byte-identical output. A bare
    `agent_id` with no resolvable name renders as the self-describing
    "Agent #{id}" (no redundant "Agent: " prefix).

    `source_type`/`source_name` (2026-11-06, operator: "there are two
    layers here. The source that generated the log... the alert agent
    then processes it") — when set (and `source_type` is `"process"` or
    `"error"`), the label becomes "Source: {name}", optionally folding in
    who triggered that process: "Source: {name} triggered manually" or
    "Source: {name} triggered by {agent}". This composition is delegated
    entirely to `resolve_source()` (the SSOT for this decision) — this
    function only renders whatever label that returns.

    `trigger_known` (default `True`, preserving every existing caller's
    behavior) — see `_source_extra()`'s own docstring for the "manual
    vs no-trigger-at-all" ambiguity this resolves. Only a caller that
    knows it has NO trigger concept at all (the background-process
    producers with no known cause) passes `False`.

    `ist_display` lets a caller that already computed this request's
    dual-tz string (so Telegram/email/ntfy all share one timestamp
    instead of each re-reading the clock a moment apart) pass it through
    instead of triggering a fresh `timestamp_display()` call.

    Callers embedding this into something that must stay single-line
    (an email Subject header or an ntfy Title HTTP header — a literal
    newline breaks both) should take only the first line, e.g.
    ``format_notification_header(name, id).splitlines()[0]``.
    """
    from backend.shared.helpers.date_time_utils import timestamp_display
    ts = ist_display if ist_display is not None else timestamp_display()
    label_word, label_sentence, _process_tag, _action_tag = resolve_source(
        _source_extra(agent_name, agent_id, source_type, source_name, trigger_known)
    )
    if label_word == "Agent":
        origin = label_sentence
        if agent_name and agent_id is not None and origin == agent_name:
            header_line = f"Agent: {origin} (#{agent_id})"
        elif origin.startswith("Agent #"):
            header_line = origin
        else:
            header_line = f"Agent: {origin}"
    else:
        header_line = f"{label_word}: {label_sentence}"
    return f"{header_line}\n{ts}"


def _dp_resolve_identity(
    agent_name: "str | None", agent_id: "int | None",
    source_type: "str | None", source_name: "str | None",
    ist_display: str, tg_prefix: str, mode_word: str,
) -> "tuple[bool, str, str, str]":
    """`dispatch_payload()`'s identity resolution, extracted to keep that
    function's own cyclomatic complexity within the project's radon gate
    (2026-11-06) — no behavior change, pure refactor.

    Returns `(has_identity, title_base, agent_line, label_word)`. See
    `dispatch_payload()`'s own docstring for the `source_type`/
    `source_name` semantics this implements."""
    has_agent_identity = bool(agent_name) and agent_id is not None
    has_process_identity = bool(source_type) and bool(source_name)
    has_identity = has_agent_identity or has_process_identity
    # See dispatch_payload's own docstring — no "manual, agent-less"
    # concept exists for a rich alert or a summary, so plain value
    # truthiness is a safe proxy for "is there a real known trigger".
    trigger_known = has_agent_identity
    title_base = agent_name if has_agent_identity else (
        source_name if has_process_identity else tg_prefix)
    agent_line = ''
    label_word = "Agent"
    if has_identity:
        agent_line = format_notification_header(
            agent_name, agent_id, ist_display=ist_display,
            source_type=source_type, source_name=source_name,
            trigger_known=trigger_known,
        ).splitlines()[0]
        label_word, _, _, _ = resolve_source(
            _source_extra(agent_name, agent_id, source_type, source_name, trigger_known)
        )
    return has_identity, title_base, agent_line, label_word


def dispatch_payload(msg_type: str, ist_display: str, tg_table: str, email_table_html: str,
                     subject_detail: str, sim_mode: bool = False, mode_tag: str = '',
                     agent_name: "str | None" = None, agent_id: "int | None" = None,
                     replay_mode: bool = False, source_type: "str | None" = None,
                     source_name: "str | None" = None) -> dict:
    """Telegram message, title, route key and email for one alert. Pure; no sending.

    `agent_name`/`agent_id` — when both are given, the real firing agent's
    identity is shown as an additional line in the Telegram/email BODY
    (never the title/subject, which must stay single-line). Omitted
    entirely for alert types with no single originating agent (open/close
    summaries) — the dual-tz `ist_display` already shown in the header
    line is not duplicated.

    `source_type`/`source_name` (2026-11-06, operator SSOT instruction —
    see `resolve_source()`) — lets a caller with no real agent at all
    (the scheduled close-summary cron, "Market summary") still show a
    real process name instead of rendering header-less. Whether the
    trigger is "known" is inferred from plain value truthiness here
    (`agent_name`/`agent_id` truthy), NOT key presence — unlike
    `format_notification_header()`'s own `trigger_known` escape hatch,
    this function's two real callers (rich alerts, summaries) have no
    "manual, agent-less" concept the way order-scoped alerts do, so a
    `None` agent paired with a real `source_type`/`source_name` always
    means "nothing triggered this", never "triggered manually".

    `replay_mode` / `mode_tag` auto-derivation (2026-10-31, operator:
    "the alerts should show paper and simulation mode explicitly in
    alert notification. wire it ... this functionality should be in
    the base code for all alerts") — this is the ONE shared dispatcher
    every alert type (rich alerts, summaries, and any future caller)
    funnels through, so the mode word/banner now live HERE rather than
    requiring each caller to pre-compute them. A caller that already
    knows a more specific tag (e.g. `_v2_send_rich_alert`'s
    `_agent_execution_mode_tag`, which also covers the per-agent PAPER
    case this function has no agent object to check) still wins — an
    explicit `mode_tag` is never overridden. A caller that only knows
    its own `sim_mode`/`replay_mode` flags (e.g. `send_summary`, which
    has no agent at all) now gets a correct `[SIM]`/`[REPLAY]` tag and
    banner for free, simply by passing those two booleans through.
    """
    tg_prefix, email_prefix = _MSG_TYPES[msg_type]
    _mode_word = 'REPLAY' if replay_mode else ('SIMULATOR' if sim_mode else '')
    tg_prefix_full    = f"{_mode_word} {tg_prefix}"    if _mode_word else tg_prefix
    email_prefix_full = f"{_mode_word} {email_prefix}" if _mode_word else email_prefix

    # 2026-10 audit fix: the ntfy/lock-screen TITLE used to be the bare
    # generic `tg_prefix` ("Agent") for every alert, showing e.g.
    # "Agent — 09:14:22 IST" with zero agent name/metric/account visible
    # before opening the notification. When the REAL triggering agent's
    # name is known, use it as the title's base instead of the generic
    # prefix. A known process name (2026-11-06) is the next preference —
    # still better than the generic prefix. Falls back to the prior
    # generic text for callers with no single originating agent or
    # process (open/close summaries) — unchanged behaviour there.
    # Agent-identity line — the first line of format_notification_
    # header's output ("Agent: {name} (#{id})" or, since 2026-11-06,
    # "Source: {process}..."), never its own timestamp (the dual-tz
    # timestamp used in the merged line below is `ist_display` itself,
    # already pipe-separated dual-tz text). See `_dp_resolve_identity`'s
    # own docstring — extracted purely to keep this function's
    # complexity within the project's radon gate.
    has_identity, title_base, agent_line, label_word = _dp_resolve_identity(
        agent_name, agent_id, source_type, source_name, ist_display, tg_prefix, _mode_word,
    )
    title_base_full = f"{_mode_word} {title_base}" if _mode_word else title_base

    branch = config.get('deploy_branch', 'main')
    branch_tag = f" [{branch}]" if branch != 'main' else ''
    # Bracket tag uses the short [SIM]/[REPLAY] form (matching
    # _agent_execution_mode_tag's own convention, agent_engine.py) —
    # deliberately NOT the longer "SIMULATOR" word used for the
    # prefix above, which is a separate, pre-existing convention.
    _mode_bracket = '[REPLAY]' if replay_mode else ('[SIM]' if sim_mode else '')
    mode_tag = mode_tag or _mode_bracket
    mode_pfx = f"{mode_tag} " if mode_tag else ''

    warning_block = _build_tg_warning_block(sim_mode, branch, replay_mode=replay_mode)
    # 2026-10 audit fix: the agent-identity line leads the message —
    # operator requirement that origin always appears first. Only ever
    # populated when a real agent identity pair is known (see
    # `agent_line` above); summaries and other agent-less dispatches are
    # unaffected (unmerged `else` branch below).
    #
    # 2026-10-31 one-line merge (operator: duplicate "Agent: {name}
    # (#{id})" + "Agent — {ts}" two-line header read as redundant —
    # "Agent" is literally repeated). When the real agent is known, the
    # identity line and the dual-tz timestamp are pipe-separated onto
    # ONE line instead of two: `agent_line` already omits any
    # timestamp of its own (see the comment above), and `ist_display`
    # is itself already pipe-separated dual-tz text, so simple
    # concatenation produces the confirmed target shape with no
    # duplicated "Agent" word. `tg_prefix_full` (the generic "Agent" /
    # "SIMULATOR Agent" / "REPLAY Agent" prefix) is dropped entirely in
    # this branch — it is pure redundancy once a real name is shown,
    # and the SIMULATOR/REPLAY *word* it carried is not lost: the
    # [SIM]/[REPLAY] bracket (`mode_pfx`) and the warning banner
    # (`warning_block`) below still carry that fact.
    #
    # 2026-10 plain-text reversal — no `<b>`/`<code>` tags and no
    # `html.escape()` anywhere: `_send_telegram()` no longer requests
    # `parse_mode: HTML`, so escaping here would show literal `&lt;`/
    # `&amp;` text instead of protecting markup that no longer exists.
    #
    # 2026-11-02 "Agent" -> "Alert" relabel (operator), SCOPED to this
    # merged line only via `_relabel_agent_label_to_alert()` (see that
    # helper's own docstring) — `format_notification_header()` itself,
    # and every OTHER caller of it (order display's `agent_origin`
    # chip, fill/failure/chase-cancel renderers, etc.) keep saying
    # "Agent:" unchanged; those answer a different question ("who
    # placed this order: manual or which agent") and must never say
    # "Alert:".
    # 2026-11-03 (operator) — split back to two lines: the merged
    # pipe-separated single line read too cramped in practice. This is
    # NOT a reversion to the original duplicated "Agent: X" / "Agent —
    # ts" shape — there is still exactly one agent mention (line 1)
    # and exactly one timestamp (line 2), just on separate lines for
    # readability. `branch_tag` stays with the identity line; `mode_pfx`
    # moves to the timestamp line (matches where it sat relative to
    # `ist_display` before the one-line merge).
    alert_line = _relabel_agent_label_to_alert(agent_line)
    if agent_line:
        telegram_msg = (
            f"{alert_line}{branch_tag}\n"
            f"{mode_pfx}{ist_display}{warning_block}\n\n"
            f"{tg_table}"
        )
    else:
        telegram_msg = (
            f"{tg_prefix_full}{branch_tag} {mode_pfx}— {ist_display}{warning_block}\n\n"
            f"{tg_table}"
        )
    event_key = 'market_open' if msg_type == 'open' else (
                'market_close' if msg_type == 'close' else 'agent_alert')
    subject, html_body = _email_message(
        email_prefix_full=email_prefix_full, branch_tag=branch_tag, mode_tag=mode_tag,
        subject_detail=subject_detail, sim_mode=sim_mode, branch=branch,
        tg_prefix_full=tg_prefix_full, ist_display=ist_display,
        email_table_html=email_table_html, agent_line=agent_line,
        replay_mode=replay_mode, label_word=label_word,
    )
    return {
        "event_key": event_key,
        "title": f"{title_base_full} — {ist_display}",
        "telegram_msg": telegram_msg,
        "email_subject": subject,
        "email_html": html_body,
        "log_prefix": f"{(mode_tag + ' ') if mode_tag else ''}{tg_prefix}",
    }


# ---------------------------------------------------------------------------
# Funds table helpers
# ---------------------------------------------------------------------------

def _fmt_inr_or_missing(v) -> str:
    """₹-format `v` via `_fmt_inr`, or a clear "—" placeholder when `v`
    is genuinely missing (None or NaN) — 2026-10 audit fix.

    `float(x or 0)` at the call site used to collapse a missing field
    straight into a confirmed-looking "₹0" (or "nan" when the NaN
    survived the `or 0` coercion un-caught, since `float('nan') or 0`
    evaluates to `float('nan')`, truthy). Matches this codebase's
    missing-vs-zero convention (CLAUDE.md) — a broker-confirmed real
    zero still renders as "₹0", only a genuinely absent value renders
    as "—".
    """
    if v is None:
        return "—"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return "—"
    if fv != fv:  # NaN check without importing pandas/math here
        return "—"
    return _fmt_inr(fv)


def _build_funds_rows(df_margins):
    """Build (Account, Cash, Avail Margin, Used Margin, Collateral) rows from df_margins.

    Account codes are masked here (`mask_account`) — this feeds
    `send_summary`'s Telegram/email tables directly, a real
    notification-facing surface, not just a log line. `mask_account`
    leaves the literal "TOTAL" aggregate row unchanged."""
    from backend.shared.helpers.utils import mask_account
    rows = []
    if df_margins is None or df_margins.empty:
        return rows
    for _, row in df_margins.iterrows():
        account   = mask_account(str(row.get('account', '')))
        cash      = row.get('avail opening_balance')
        avail_net = row.get('net')
        used      = row.get('util debits')
        collat    = row.get('avail collateral')
        rows.append((account, _fmt_inr_or_missing(cash), _fmt_inr_or_missing(avail_net),
                     _fmt_inr_or_missing(used), _fmt_inr_or_missing(collat)))
    return rows


# ---------------------------------------------------------------------------
# Open / Close summary
# ---------------------------------------------------------------------------

def _summary_holdings_rows(sum_holdings) -> list[tuple]:
    """Build (Account, Cur Val, P&L, P&L%, Day Loss, Day Loss%) rows.

    Account codes are masked (`mask_account`) — this feeds the market
    open/close summary's Telegram/email tables, a real notification
    surface."""
    from backend.shared.helpers.utils import mask_account
    rows = []
    for _, row in sum_holdings.iterrows():
        account = mask_account(str(row.get('account', '')))
        cur_val = float(row.get('cur_val', 0) or 0)
        pnl     = float(row.get('pnl', 0) or 0)
        pnl_pct = float(row.get('pnl_percentage', 0) or 0)
        day_val = float(row.get('day_change_val', 0) or 0)
        day_pct = float(row.get('day_change_percentage', 0) or 0)
        rows.append((
            account, _fmt_inr(cur_val), _fmt_inr(pnl),
            f"{pnl_pct:.2f}%", _fmt_inr(day_val), f"{day_pct:.2f}%",
        ))
    return rows


def _summary_positions_rows(sum_positions) -> list[tuple]:
    """Build (Account, P&L) rows from the positions summary dataframe.

    Account codes are masked (`mask_account`) — same notification-facing
    reasoning as `_summary_holdings_rows` above."""
    from backend.shared.helpers.utils import mask_account
    rows = []
    for _, row in sum_positions.iterrows():
        account = mask_account(str(row.get('account', '')))
        pnl     = float(row.get('pnl', 0) or 0)
        rows.append((account, _fmt_inr(pnl)))
    return rows


def _summary_underlying_rows(df_positions) -> list[dict]:
    """Build per-underlying breakdown rows (settings-gated, never raises)."""
    try:
        from backend.shared.helpers.settings import get_bool, get_int
        from backend.shared.helpers.summarise import breakdown_positions_by_underlying
        if df_positions is not None and get_bool(
                'alerts.summary_show_underlying_breakdown', True):
            top_n = get_int('alerts.max_underlyings_per_alert', 5)
            return breakdown_positions_by_underlying(
                df_positions, account=None, top_n=top_n,
            )
    except Exception as e:
        logger.warning(f"summary underlying breakdown failed: {e}")
    return []


def _build_summary_telegram(
    segment_label: str,
    h_rows: list[tuple], p_rows: list[tuple],
    f_rows: list[tuple], und_rows: list[dict],
) -> str:
    """Assemble the Telegram fixed-table body for market summaries."""
    h_headers = ("Account", "Cur Val", "P&L", "P&L%", "Day Loss", "Day Loss%")
    p_headers = ("Account", "P&L")
    f_headers = ("Account", "Cash", "Avail Margin", "Used Margin", "Collateral")
    h_tg = _fixed_table(h_headers, h_rows) if h_rows else "No holdings data"
    p_tg = _fixed_table(p_headers, p_rows) if p_rows else "No positions data"
    body = f"Holdings{segment_label}\n{h_tg}\n\nPositions{segment_label}\n{p_tg}"
    if f_rows:
        body += f"\n\nFunds\n{_fixed_table(f_headers, f_rows)}"
    if und_rows:
        und_lines = "\n".join(
            f"  {u['underlying']:<10} {_fmt_rupees(u['pnl'])}" for u in und_rows
        )
        body += f"\n\nBy underlying\n{und_lines}"
    return body


def _build_summary_email(
    segment_label: str,
    h_rows: list[tuple], p_rows: list[tuple],
    f_rows: list[tuple], und_rows: list[dict],
) -> str:
    """Assemble the HTML email body for market summaries."""
    from backend.api.algo.grammar_tokens.format_html_table import _html_table
    h_headers = ("Account", "Cur Val", "P&L", "P&L%", "Day Loss", "Day Loss%")
    p_headers = ("Account", "P&L")
    f_headers = ("Account", "Cash", "Avail Margin", "Used Margin", "Collateral")
    h_email = _html_table(h_headers, h_rows) if h_rows else "<p>No holdings data</p>"
    p_email = _html_table(p_headers, p_rows) if p_rows else "<p>No positions data</p>"
    body = (
        f"<p style='margin-top:16px;font-weight:bold'>Holdings{segment_label}</p>"
        f"{h_email}"
        f"<p style='margin-top:16px;font-weight:bold'>Positions{segment_label}</p>"
        f"{p_email}"
    )
    if f_rows:
        body += (
            f"<p style='margin-top:16px;font-weight:bold'>Funds</p>"
            f"{_html_table(f_headers, f_rows)}"
        )
    if und_rows:
        und_html = _html_table(
            ("Underlying", "P&L", "Positions"),
            [(u['underlying'], _fmt_rupees(u['pnl']), str(u['count']))
             for u in und_rows],
        )
        body += (
            f"<p style='margin-top:16px;font-weight:bold'>By underlying</p>"
            f"{und_html}"
        )
    return body


def _partial_outage_warning_lines(sum_positions) -> "list[str] | None":
    """Return (tg_line, html_line) when `sum_positions` carries a
    `.attrs['partial_outage']` flag (2026-09 partial-outage guard — see
    background._positions_partial_outage_accounts), else None.

    `sum_positions` here is the SAME DataFrame object
    `background._perf_fetch_all_broker_data` builds — the attr survives
    `.copy()` / `groupby()` / `pd.concat()` unchanged, so no extra
    plumbing is needed at the open/close-summary call sites; this reads
    it directly off the frame already being passed in."""
    from backend.shared.helpers.utils import mask_account
    accts = (getattr(sum_positions, 'attrs', {}) or {}).get('partial_outage')
    if not accts:
        return None
    joined = ", ".join(mask_account(str(a)) for a in accts)
    return [
        f"⚠ PARTIAL OUTAGE — account(s) {joined} unavailable this cycle; "
        f"positions TOTAL may be UNDERSTATED",
    ]


def send_summary(sum_holdings, sum_positions, ist_display: str, msg_type: str,
                 label: str = "", df_margins=None, df_positions=None,
                 sim_mode: bool = False, replay_mode: bool = False,
                 agent_name: "str | None" = None, agent_id: "int | None" = None,
                 source_type: "str | None" = None, source_name: "str | None" = None):
    """
    Send holdings + positions + funds summary at market open or close.
    msg_type: 'open' or 'close'
    df_margins: full margins dataframe (all accounts + TOTAL); included when provided.
    df_positions: raw broker positions dataframe. When provided AND
        `alerts.summary_show_underlying_breakdown` is true, an extra
        per-underlying section is appended after the Positions table —
        same format as the per-alert breakdown so the operator's eye
        moves naturally between the two surfaces.

    `sim_mode`/`replay_mode` (2026-10-31, operator: "the alerts should
    show paper and simulation mode explicitly ... base code for all
    alerts") — the background cron-driven open/close summary
    (background.py) never passes either, so it stays the ordinary
    live tag always. The `send_summary` DSL action (an agent's DO
    clause) threads them from its own `context`, since that action can
    run inside a sim/replay cycle — before this fix, a summary fired
    that way carried no distinguishing tag at all.

    `agent_name`/`agent_id` (2026-11-03, operator: "every alert should
    be generated by an agent... format... driven by tags") —
    `event_agents.py:_render_summary()` reads these back out of the
    record's extras and passes them to `dispatch_payload()`, the same
    mechanism `_render_rich_alert()` already uses, so the rendered
    alert shows a real "Alert: {name} (#{id})" header instead of none
    at all. The `send_summary` DSL action threads the REAL firing
    agent's own identity from its `context` (never hardcodes
    "market-summary" — a custom agent calling this action should be
    credited as itself, not as the built-in report). Omitted (the
    default) keeps the pre-existing header-less rendering.

    `source_type`/`source_name` (2026-11-06, operator SSOT instruction —
    see `resolve_source()`'s own docstring) — `background.py`'s cron-
    driven close summary ("Market summary") is a genuinely un-caused
    scheduled job, not an agent action; it passes
    `source_type="process", source_name="Market summary"` instead of a
    fake agent identity. These two kwargs and `agent_name`/`agent_id`
    are mutually exclusive in practice — a caller passes one pair or the
    other, never both — so only ONE pair's keys reach the record's
    `extra` dict, never a stale leftover from the other path.
    """
    h_rows    = _summary_holdings_rows(sum_holdings)
    p_rows    = _summary_positions_rows(sum_positions)
    f_rows    = _build_funds_rows(df_margins)
    und_rows  = _summary_underlying_rows(df_positions)

    segment_label  = f" — {label}" if label else ""
    subject_detail = f"{label + ' — ' if label else ''}{ist_display}"

    tg_table       = _build_summary_telegram(segment_label, h_rows, p_rows, f_rows, und_rows)
    email_html     = _build_summary_email(segment_label, h_rows, p_rows, f_rows, und_rows)

    warning_lines = _partial_outage_warning_lines(sum_positions)
    if warning_lines:
        tg_table = "\n".join(warning_lines) + "\n\n" + tg_table
        email_html = (
            f"<p style='color:{_EMAIL_RED};font-weight:bold'>"
            f"{warning_lines[0]}</p>" + email_html
        )

    extra = {
        # "alert" added additively (2026-10-25 tag-unification) so
        # SUMMARY_AGENT's condition (now tag="alert") matches.
        "tags": ["summary", "alert"], "alert_event": "summary", "msg_type": msg_type,
        "ist_display": ist_display, "tg_table": tg_table,
        "email_table_html": email_html, "subject_detail": subject_detail,
        "sim_mode": bool(sim_mode), "replay_mode": bool(replay_mode),
    }
    if source_type and source_name:
        extra["source_type"] = source_type
        extra["source_name"] = source_name
    else:
        extra["agent_name"] = agent_name
        extra["agent_id"] = agent_id
    _DISPATCH_LOGGER.info(f"Background: {msg_type} summary recorded", extra=extra)


# ---------------------------------------------------------------------------
# Intra-day loss alerts + negative fund balance alert
# ---------------------------------------------------------------------------
#
# Design goals (driven by user requirement "wake me up only when something is
# really wrong"):
#   * Prefer fewer, louder alerts over frequent noisy ones.
#   * Every threshold is configurable via backend_config.yaml.
#   * Two orthogonal rule families: static floors (you've lost too much) and
#     rate-of-change (you're losing fast right now).
#   * Suppress re-alerts as long as the loss is roughly the same and the rate
#     isn't worsening — one ping, then quiet until the situation changes.
#
# Bucket key shape -- used for history, last-alert lookup, and dedup:
#   (section, scope, kind)
# where
#   section = 'holdings' | 'positions'
#   scope   = masked account id (e.g. "ZG####") | 'TOTAL'
#   kind    = 'static_pct' | 'static_abs' | 'rate_abs' | 'rate_pct'
#
# alert_state keys we touch (the state dict is owned by _task_performance):
#   alert_state['pnl_history']   {(section, scope): [(ts, pnl_val, pnl_pct), ...]}
#   alert_state['last_alert']    {(section, scope, kind): (ts, pnl_val, pnl_pct)}
#   alert_state['session_date']  date — resets pnl_history once per day
#   alert_state['session_start'] datetime — anchor for the baseline-offset gate
# Any other keys (e.g. the old 'funds_cash_…' ones) are untouched so funds
# negative-balance alerts keep their existing simple cooldown behaviour.
# ---------------------------------------------------------------------------

# Mapping from a bucket key to a human-readable rule label for the alert row.
# ---------------------------------------------------------------------------
# Order failure alerts
# ---------------------------------------------------------------------------
#
# Fires Telegram + email whenever an order placement fails — manual ticket,
# agent action, or chase loop.  Deduplicated per (masked_account, symbol,
# side, error_signature) within a 10-minute cooldown so a misfiring agent
# doesn't produce an alert storm.  After the cooldown the next fire carries
# a "(+N suppressed)" suffix.
# ---------------------------------------------------------------------------

_ORDER_ALERT_COOLDOWN_SEC = 600   # 10 minutes

# Keyed by (masked_account, symbol, side, error_sig) ->
#   {"first_seen": datetime, "last_sent": datetime, "suppressed": int}
_order_alert_state: dict[tuple, dict] = {}
_order_alert_lock  = Lock()


def _error_sig(error: str) -> str:
    """Normalise an error string to an 80-char lowercase signature for dedup."""
    return error.lower().strip()[:80]


def _redis_cooldown_check(
    redis_key: str, key: tuple, now: "datetime",
) -> "tuple[bool, int, bool]":
    """Check Redis cooldown for an order failure alert.

    Returns (should_suppress, suppressed_count, used_redis).
    should_suppress=True means caller must return early (don't fire the alert).
    used_redis=True means Redis was successfully consulted (skip in-process path).
    """
    try:
        _rc = _get_redis()
        if _rc is None:
            return False, 0, False
        existing = _rc.get(redis_key)
        if existing is not None:
            with _order_alert_lock:
                _entry = _order_alert_state.get(key)
                if _entry is not None:
                    _entry["suppressed"] += 1
            return True, 0, True
        with _order_alert_lock:
            _entry = _order_alert_state.get(key)
            if _entry is not None:
                suppressed_count = _entry["suppressed"]
                _entry["last_sent"]  = now
                _entry["suppressed"] = 0
            else:
                suppressed_count = 0
                _order_alert_state[key] = {
                    "first_seen": now, "last_sent": now, "suppressed": 0,
                }
        _rc.setex(redis_key, _ORDER_ALERT_COOLDOWN_SEC, b"1")
        return False, suppressed_count, True
    except Exception as _redis_e:
        logger.debug(f"Redis cooldown check failed — using in-process dict: {_redis_e}")
        return False, 0, False


def _inprocess_cooldown_check(
    key: tuple, now: "datetime", masked: str, side: str, symbol: str, sig: str,
) -> "tuple[bool, int]":
    """Check in-process cooldown dict for an order failure alert.

    Returns (should_suppress, suppressed_count).
    should_suppress=True means caller must return early.
    """
    with _order_alert_lock:
        entry = _order_alert_state.get(key)
        if entry is not None:
            elapsed = (now - entry["last_sent"]).total_seconds()
            if elapsed < _ORDER_ALERT_COOLDOWN_SEC:
                entry["suppressed"] += 1
                logger.debug(
                    f"order-failure alert suppressed ({entry['suppressed']} total) "
                    f"for {masked} {side} {symbol}: {sig}"
                )
                return True, 0
            suppressed_count = entry["suppressed"]
            entry["last_sent"]  = now
            entry["suppressed"] = 0
        else:
            suppressed_count = 0
            _order_alert_state[key] = {
                "first_seen": now, "last_sent": now, "suppressed": 0,
            }
    return False, suppressed_count


# Sources whose failure is about the template/Bracket exit mechanism
# (wing order, OCO exit GTT) rather than the parent entry order itself —
# used by _classify_order_origin_label below regardless of who placed
# the parent (operator or agent).
_BRACKET_SOURCES = frozenset({"template_wing", "template_wing_chase"})


def _classify_order_origin_label(source: str, agent_id: "int | None") -> str:
    """One of 'Manual' / 'Manual Bracket' / 'Agent' / 'Agent Bracket'.

    2026-10 operator request: alerts for a manually-placed order say
    "Manual"; alerts about that order's template/Bracket exit say
    "Manual Bracket". An agent-placed order (agent_id set, reliably
    populated since the Sprint 1a _al_place_resolve_params fix) gets
    "Agent" / "Agent Bracket" instead — parallel naming, no slug (the
    existing agent-CONDITION rich-alert path already renders
    "Agent [{slug}]" separately; this is the order-failure path only).

    2026-10 audit fix: routed through `resolve_origin()` instead of the
    bare `agent_id is not None` check — a manually-placed order's
    `AlgoOrder.agent_id` is the REAL id of the synthetic "manual" Agent
    row (not None), so the old check misclassified a manual order's
    failure/rejection alert as "Agent"/"Agent Bracket" even though that
    same order's FILL alert (via fill_notify, which resolves the real
    name) correctly showed "Manual operator order". Both paths now
    agree: `resolve_origin()` recognises the sentinel id via the warmed
    `_MANUAL_AGENT_ID_CACHE`.
    """
    is_bracket = source in _BRACKET_SOURCES
    is_agent = resolve_origin(agent_id, None) != "manual"
    if is_agent:
        return "Agent Bracket" if is_bracket else "Agent"
    return "Manual Bracket" if is_bracket else "Manual"


# 2026-11-08 audit fix (item 1) — SSOT for order_failure's process
# identity. Both the header built below AND
# `_send_order_failure_messages()`'s logged `extra` dict (which is what
# the trailing tag and the Alerts tab independently call
# `resolve_source()` on) must use this exact pair — defining it once
# here, instead of two separately-typed literals, is what makes the two
# structurally unable to drift apart again.
_ORDER_FAILURE_SOURCE_TYPE = "process"
_ORDER_FAILURE_SOURCE_NAME = "Order failure"


def order_failure_messages(
    *, masked: str, symbol: str, exchange: str, side: str, qty: int, mode: str,
    source: str, error: str, suppressed_count: int, ist_disp: str,
    agent_id: "int | None" = None, agent_name: "str | None" = None,
) -> tuple[str, str, str]:
    """Telegram body, email subject and email HTML for an order rejection. Pure."""
    from backend.api.algo.grammar_tokens.format_html_table import _html_table
    origin_label = _classify_order_origin_label(source, agent_id)
    branch      = config.get("deploy_branch", "main")
    # 2026-11-08 audit fix (item 2) — a live-mode alert must show NO
    # pill at all, matching the system-wide "live stays silent" rule
    # (`dispatch_payload()`'s own `_mode_bracket`/`mode_tag` derivation:
    # live mode's sim_mode/replay_mode are both False, which resolves to
    # `''`). This function took its own `mode` string straight from the
    # AlgoOrder row, so it used to show `[LIVE]` unconditionally for
    # every real order failure — the one alert type in the whole
    # pipeline that disagreed with the rule.
    mode_tag    = (
        "" if (not mode or mode.lower() == "live") else f"[{mode.upper()}]"
    )
    sup_note    = f"  (+{suppressed_count} suppressed)" if suppressed_count else ""
    # 2026-10-23 audit fix (O11) — `error` is raw caller-supplied text
    # (e.g. a chase abort message) that can embed an unmasked broker
    # account number (`_ch_abort_ambiguous_place_timeout` was one
    # confirmed source, now fixed at the source too). Every other field
    # here reaches this function already masked (`masked`); `error` did
    # not, inconsistent with `_render_chase_cancel`'s own body, which
    # masks unconditionally. Defense in depth: mask here too, so any
    # future caller that embeds a raw account in `error` can't leak it.
    from backend.shared.helpers.utils import mask_account_in_text
    error = mask_account_in_text(error) or error
    error_short = error[:160].strip()
    # 2026-10 plain-text reversal: `tg_body` below is now plain text, so
    # a raw '<' (e.g. guard comparison text like "qty < lot_size")
    # displays literally and safely — no HTML tag/entity to protect it
    # from, and no `_html_to_plain` runs on it downstream any more
    # (that tag-strip regex is exactly what used to require escaping
    # here in the first place).

    # 2026-11-08 audit fix (item 7) — `agent_name` is OPTIONAL (most
    # callers still only carry the bare `agent_id` int off an AlgoOrder
    # row — resolving the name would need a DB round-trip per alert
    # across every chase/template_attach/orders_place call site, out of
    # scope here); when a caller DOES have it, this now passes it
    # through instead of always falling back to the bare "Agent #{id}"
    # form. `source_type="process"/source_name="Order failure"` makes
    # the renderer pick up the "Source: Order failure triggered
    # manually/by {agent}" sentence, same as every other converted
    # order-scoped renderer (`_render_chase_cancel` etc.) — the existing
    # [Manual]/[Agent]/[Agent Bracket] `origin_label` above still carries
    # the bracket distinction this header text doesn't.
    #
    # 2026-10 audit fix: moved to the FIRST line of the message (was line
    # 2, after the bold "Order rejected" title) — operator requirement
    # that every alert shows "who/what triggered this" before anything
    # else.
    header_line = format_notification_header(
        agent_name, agent_id, ist_display=ist_disp,
        source_type=_ORDER_FAILURE_SOURCE_TYPE, source_name=_ORDER_FAILURE_SOURCE_NAME,
    )

    tg_body = (
        f"{header_line}\n"
        f"❌ Order rejected  [{origin_label}]  {mode_tag}{sup_note}\n"
        f"{masked}  {side}  {qty}  {symbol}  ({exchange})\n"
        f"source: {source}\n"
        f"{error_short}"
    )

    rows_html = _html_table(
        ("Field", "Value"),
        [
            ("Origin",    origin_label),
            ("Account",   masked),
            ("Symbol",    symbol),
            ("Exchange",  exchange),
            ("Side",      side),
            ("Qty",       str(qty)),
            ("Mode",      mode),
            ("Source",    source),
            ("Error",     error_short + sup_note),
            ("Timestamp", ist_disp),
        ],
    )
    email_body = (
        f"<html><body style='font-family:sans-serif'>"
        + (_branch_banner_html(branch) if branch != "main" else "")
        + f"<p style='font-size:14px;color:#c0392b'><b>&#10060; Order rejected</b>"
          f"{' [' + origin_label + ']'}"
          f"{' ' + mode_tag if mode_tag else ''}</p>"
        + rows_html
        + f"</body></html>"
    )
    # 2026-11-08 audit fix (item 2) — matches the `{label_word}:
    # {detail}{branch_tag}{mode_sfx} | {ts}` convention every other
    # alert subject in this file now uses (`_email_message`'s own
    # subject line, below), instead of the older `[Manual] RamboQuant
    # Order Rejected: ...` shape, which carried no timestamp at all and
    # predated the "Source:"/"Agent:" label-word convention entirely.
    # `label_word` is resolved the same way the header above is, off the
    # identical `source_type`/`source_name` pair, so the subject can
    # never say "Agent" while the body says "Source" (or vice versa).
    label_word, _, _, _ = resolve_source(
        _source_extra(
            agent_name, agent_id,
            source_type=_ORDER_FAILURE_SOURCE_TYPE, source_name=_ORDER_FAILURE_SOURCE_NAME,
            trigger_known=True,
        )
    )
    branch_tag = f" [{branch}]" if branch != "main" else ""
    mode_sfx   = f" {mode_tag}" if mode_tag else ""
    subject = (
        f"{label_word}: Order Rejected {symbol} {side} [{origin_label}]"
        f"{branch_tag}{mode_sfx} | {ist_disp}"
    )
    return tg_body, subject, email_body


def _send_order_failure_messages(
    *, masked: str, symbol: str, exchange: str, side: str, qty: int, mode: str,
    source: str, error: str, suppressed_count: int, ist_disp: str,
    agent_id: "int | None" = None, agent_name: "str | None" = None,
) -> None:
    """Record an order rejection for the order-failure event agent (no direct send).

    `agent_id`/`agent_name` ride along in `extra` so they survive the
    log→event-agent pipeline — `event_agents.py:_render_order_failure`
    reads them back out of the replayed record's `extra` dict and
    passes them straight through to `order_failure_messages()` for the
    Manual/Agent/Bracket label AND the new "Source: Order failure
    triggered ..." header.

    2026-11-08 audit fix (item 1) — `source_type`/`source_name` now also
    ride along in `extra`, matching exactly what
    `order_failure_messages()` passes to `format_notification_header()`
    for the header text above. Before this, the header said "Source:
    Order failure triggered ..." but the logged record itself carried
    no `source_type`/`source_name` key at all — `resolve_source()`,
    called independently on this same `extra` dict by the trailing tag
    (`_resolve_tag_labels()`) and by the Alerts tab
    (`routes/alerts.py:_log_event_to_alert`), fell through to branch 2
    ("Agent") instead of branch 1 ("Source"), so the tag and the Alerts
    row disagreed with what the header actually said.

    2026-10-25 fix: this used to log through this module's own `logger`
    (name `backend.shared.helpers.alert_utils`, one of `log_store.py`'s
    `_SKIP_PREFIXES`) — `LogStoreHandler.emit()` drops any record from
    that name unconditionally, before tags/dispatch are ever considered.
    That meant this call (and `send_summary()`'s, below) never reached
    `log_events`, so ORDER_FAILURE_AGENT's condition could never match
    and NOTHING was ever sent to any channel for an order failure —
    confirmed via git history to have been silently broken since
    2026-10-06 (commit 54860caa). Now logs through `_DISPATCH_LOGGER`
    instead (see its module-level comment) so the record actually
    reaches the dispatch pipeline.
    """
    sup_note = f"  (+{suppressed_count} suppressed)" if suppressed_count else ""
    origin_label = _classify_order_origin_label(source, agent_id)
    _DISPATCH_LOGGER.warning(
        f"order-failure recorded: [{origin_label}] {masked} {side} {qty} {symbol} "
        f"mode={mode} source={source}{sup_note}",
        extra={
            "tags": ["orders", "alert"], "alert_event": "order_failure", "masked": masked,
            "symbol": symbol, "exchange": exchange, "side": side, "qty": qty,
            "mode": mode, "source": source, "error": error,
            "suppressed_count": suppressed_count, "ist_disp": ist_disp,
            "agent_id": agent_id, "agent_name": agent_name,
            "source_type": _ORDER_FAILURE_SOURCE_TYPE,
            "source_name": _ORDER_FAILURE_SOURCE_NAME,
            "branch": config.get("deploy_branch", "main"),
            # 2026-10-23 chase robustness (fix #4) — every order-failure
            # alert is, by definition, action-required: the operator
            # needs to look at something (a rejection, an abandoned
            # chase, a stuck chase, an ambiguous cancel). Binary ntfy
            # gate consumed by event_agents.py:_dispatch_agent — True
            # forces urgent ntfy regardless of the dispatching agent's
            # own stored channel config, independent of whatever's
            # already seeded in the `agents` DB table.
            "action_required": True,
        },
    )


def send_order_failure_alert(
    *,
    account: str,
    symbol: str,
    exchange: str,
    side: str,
    qty: int,
    mode: str,
    source: str,
    error: str,
    detail: dict | None = None,
    agent_id: "int | None" = None,
    agent_name: "str | None" = None,
    bypass_gates: bool = False,
) -> None:
    """
    Telegram + email alert when an order placement fails.

    Deduplicated by (masked_account, symbol, side, error_signature)
    within `_ORDER_ALERT_COOLDOWN_SEC`.  Suppressed fires increment a
    counter that flows into the next dispatched alert as "(+N suppressed)".

    Market-hours gate: suppressed when all market segments are closed
    (catches misconfigured agents firing at 1am IST).

    Cooldown is persisted to Redis (key TTL = cooldown window) so process
    restarts don't reset the dedup state.  Falls back to the in-process
    `_order_alert_state` dict when Redis is unavailable.

    All exceptions are swallowed — a broken Telegram connection must never
    interrupt order placement or the chase loop.

    `agent_id` — pass the originating AlgoOrder row's `agent_id` when known
    (chase.py / template_attach.py call sites have the row in scope) so the
    rendered alert is labeled "Manual"/"Manual Bracket" (agent_id is None)
    or "Agent"/"Agent Bracket" (agent_id is set) — see
    `_classify_order_origin_label`. Callers with no row context (a plain
    manual ticket placement) omit it, correctly defaulting to "Manual".

    `agent_name` — optional; pass the resolved agent name alongside
    `agent_id` when a caller already has it in scope, so the "Source:
    Order failure triggered by {agent}" header line shows the real name
    instead of the bare "Agent #{id}" fallback. No current caller in
    this codebase resolves a name (that needs a DB round-trip this
    function's hot path doesn't take); omitted, it degrades honestly.

    `bypass_gates` (2026-10-23 chase robustness, fix #7) — when True,
    skips BOTH the market-hours suppression and the dedup cooldown check
    below, always sending. Reserved for genuine abandonment/exhaustion-
    class alerts (chase consecutive-errors abort, max-attempts
    exhaustion, stuck/wall-clock abort, ambiguous place-timeout abort) —
    reconciliation matters regardless of whether the market happens to
    be open, and a second genuinely-new abandonment must never be
    swallowed by the first one's 10-minute dedup window. Every other
    caller (plain non-recoverable rejections, preflight blocks, etc.)
    leaves this False — dedup/market-hours suppression remain correct
    and wanted there.
    """
    try:
        if not bypass_gates:
            try:
                from backend.api.helpers.snapshot_gate import _any_segment_open
                if not _any_segment_open():
                    logger.debug(
                        f"order-failure alert suppressed (all markets closed) "
                        f"for {account} {side} {symbol}"
                    )
                    return
            except Exception as _mh_e:
                logger.debug(f"order-failure market-hours check failed (fail-open): {_mh_e}")

        from backend.shared.helpers.date_time_utils import timestamp_display
        from backend.shared.helpers.utils import mask_account

        masked = mask_account(account)
        sig    = _error_sig(error)
        key    = (masked, symbol.upper(), side.upper(), sig)
        now    = datetime.utcnow()

        _redis_raw = f"{masked}|{symbol.upper()}|{side.upper()}|{sig}"
        _redis_key = "ramboq:order_alert:" + hashlib.sha256(_redis_raw.encode()).hexdigest()[:32]

        suppressed_count = 0
        if not bypass_gates:
            should_suppress, suppressed_count, _used_redis = _redis_cooldown_check(
                _redis_key, key, now
            )
            if should_suppress:
                logger.debug(
                    f"order-failure alert suppressed (Redis TTL) "
                    f"for {masked} {side} {symbol}: {sig}"
                )
                return

            if not _used_redis:
                should_suppress, suppressed_count = _inprocess_cooldown_check(
                    key, now, masked, side, symbol, sig
                )
                if should_suppress:
                    return

        _send_order_failure_messages(
            masked=masked, symbol=symbol, exchange=exchange,
            side=side, qty=qty, mode=mode, source=source,
            error=error, suppressed_count=suppressed_count,
            ist_disp=timestamp_display(), agent_id=agent_id,
            agent_name=agent_name,
        )
    except Exception as _top_e:
        logger.error(f"send_order_failure_alert internal error: {_top_e}")


# Tier -> ntfy priority. Mirrors Agent.tier's vocabulary exactly
# (models.py, NOT NULL default "medium") so a caller that has a real
# firing Agent row in scope can derive a severity-driven ntfy priority
# instead of relying on send_ntfy_alert's clock-based day/night default.
# An explicit per-channel priority (operator-configured) always wins
# over this; this is only ever a fallback default. `.get()` with no
# second argument deliberately returns None for an unmapped/unknown tier
# (e.g. the scheduling-only "info" pseudo-tier some agents use) so the
# caller can omit the kwarg entirely and let send_ntfy_alert's own
# clock-based logic apply, rather than silently coercing to "default".
_TIER_TO_NTFY_PRIORITY = {
    "critical": "urgent",
    "high":     "high",
    "medium":   "default",
    "low":      "low",
    # 2026-10-16 fix — "info" (the built-in scheduled/notify-only agents,
    # e.g. market-open-nse) was missing here entirely. Without an
    # explicit mapping, priority resolution fell through to
    # send_ntfy_alert's own clock-based day/night default, which is
    # "urgent" for essentially the whole IST trading day (22:00-07:00 ET
    # == daytime IST) — triggering urgent's 3x-repeat-send behavior for
    # a routine "market opened" notification. "low" matches info's own
    # "routine, no action needed" semantics and send_ntfy_alert's
    # single-send (non-urgent) path.
    "info":     "low",
}


def send_ntfy_alert(title: str, message: str, priority: str | None = None) -> None:
    """Deliver via ntfy.sh (or self-hosted ntfy). Priority is ET clock-based:
      22:00–07:00 ET → urgent  (operator night / Indian market hours)
      07:00–22:00 ET → high    (operator day)
    Configurable via ntfy_night_start / ntfy_night_end in secrets (24h hours ET).
    A caller-supplied `priority` value takes precedence over the clock-based default.
    """
    import zoneinfo
    from datetime import datetime

    if pytest_alert_transport_blocked():
        logger.info("ntfy skipped — PYTEST_RUNNING guard (no alert_transport opt-in)")
        return

    topic = secrets.get("ntfy_topic")
    if not topic:
        return

    base_url = secrets.get("ntfy_url", "https://ntfy.sh")
    night_start = int(secrets.get("ntfy_night_start", 22))
    night_end   = int(secrets.get("ntfy_night_end",   7))

    et_hour = datetime.now(zoneinfo.ZoneInfo("America/New_York")).hour
    is_night = (et_hour >= night_start or et_hour < night_end) if night_start > night_end \
               else (night_start <= et_hour < night_end)
    if priority is None:
        priority = "urgent" if is_night else "high"

    try:
        import urllib.request as _urlreq
        url = f"{base_url.rstrip('/')}/{topic}"
        send_count = 3 if priority == "urgent" else 1

        # HTTP headers must be latin-1 safe; replace Unicode punctuation that
        # appears in agent titles (em dash u2014, en dash u2013, etc.) with ASCII.
        safe_title = title.replace('—', '--').replace('–', '-').replace('’', "'").replace('“', '"').replace('”', '"').encode('latin-1', errors='replace').decode('latin-1')
        headers = {
            "Title": safe_title,
            "Priority": priority,
            "Tags": "rotating_light",
            "Content-Type": "text/plain",
        }

        # Add Authorization header if ntfy_token is configured
        token = secrets.get("ntfy_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        for _ in range(send_count):
            req = _urlreq.Request(
                url,
                data=message.encode(),
                headers=headers,
                method="POST",
            )
            _urlreq.urlopen(req, timeout=5)
    except Exception as e:
        logger.warning("ntfy alert failed: %s", e)


_KIND_LABEL = {
    'static_pct':      'Static %',
    'static_abs':      'Static ₹',
    'rate_abs':        'Rate ₹/min',
    'rate_pct':        'Rate %/min',
    'negative_cash':   'Cash < 0',
    'negative_margin': 'Margin < 0',
}

# Short section codes for the narrow Telegram layout (mobile-friendly widths).
_SECTION_SHORT = {'Holdings': 'HLD', 'Positions': 'POS', 'Funds': 'FND'}


def _fmt_inr(n: float) -> str:
    """₹ amount in Indian K/L/C convention — mirrors the frontend
    grid's aggCompact rounding so email + Telegram + on-screen
    grids all read with the same precision. Operator: "in the email
    and telegram follow thousand as K, Lakh as L and crore as C
    convention you followed rounding off the numbers in the grids."

    Examples:
      9_999      → "₹9,999"
      50_000     → "₹50K"
      150_000    → "₹1.50L"
      27_500_000 → "₹2.75C"
    """
    a = abs(float(n))
    sign = '-' if n < 0 else ''
    if a >= 10_000_000:        # 1 Cr
        return f"{sign}₹{a / 10_000_000:.2f}C"
    if a >= 100_000:           # 1 L
        return f"{sign}₹{a / 100_000:.2f}L"
    if a >= 1_000:             # 1 K
        return f"{sign}₹{round(a / 1_000)}K"
    return f"{sign}₹{a:,.0f}"


def _fmt_inr_precise(n: float) -> str:
    """Same K/L/C convention as `_fmt_inr` but keeps one decimal at
    the K-tier — used in per-underlying / per-alert breakdowns where
    the ₹500 difference between '₹22K' and '₹22.3K' matters for the
    operator's risk read. Audit fix: pre-fix, the Telegram per-
    underlying chips were re-aliased to `_fmt_inr` which rounded
    sub-K residue away (₹22,300 → "₹22K" loses ₹300).
    """
    a = abs(float(n))
    sign = '-' if n < 0 else ''
    if a >= 10_000_000:
        return f"{sign}₹{a / 10_000_000:.2f}C"
    if a >= 100_000:
        return f"{sign}₹{a / 100_000:.2f}L"
    if a >= 1_000:
        return f"{sign}₹{a / 1_000:.1f}K"
    return f"{sign}₹{a:,.0f}"


# Legacy aliases. `_fmt_rupees` was the integer-rupee formatter used in
# table cells (now upgraded to K/L/C alongside the grids — operator
# explicitly asked for this). `_fmt_rupees_compact` was the inline
# breakdown formatter that always carried K-tier `.1f` precision; we
# keep that precision via `_fmt_inr_precise`.
_fmt_rupees         = _fmt_inr
_fmt_rupees_compact = _fmt_inr_precise


def _fmt_pct(n: float) -> str:
    return f"{n:.2f}%"


def _tg_rule_line(a: dict, show_own_pnl: bool = False) -> str:
    """Build the second Telegram line for an alert (rule kind + threshold/rate).

    `show_own_pnl` (2026-11 alert-consolidation fix) — set by the caller
    when this leaf is part of a multi-leaf group (see
    `agent_engine._ae_group_alert_rows`) whose members DISAGREE on pnl —
    a real, common case for `loss-positions-total` (day_val reads
    day_change_val, every other metric reads lifetime pnl), not a rare
    edge case. The shared header then carries no value (never an
    arbitrarily-picked one), so this leaf's own pnl is shown here
    instead — the operator never loses a number, it just moves from a
    (non-existent) shared header line onto the per-leaf line. Default
    False keeps every pre-existing call site byte-identical."""
    k = a['kind']
    label = _KIND_LABEL[k]
    own = f"{_fmt_rupees(a['pnl'])}  " if show_own_pnl else ""
    if k in ('static_pct', 'static_abs'):
        return f"  {label}  {own}floor {a['threshold']}"
    if k == 'rate_abs':
        return (f"  {label}  {own}now {_fmt_rupees(a['rate_val'])}/min  "
                f"floor {a['threshold']}")
    if k == 'rate_pct':
        return (f"  {label}  {own}now {_fmt_pct(a['rate_val'])}/min  "
                f"floor {a['threshold']}")
    return f"  {label}  {own}{a['threshold']}"


def _tg_position_enrichment_lines(a: dict) -> list[str]:
    """Return optional enrichment lines for a Position alert (underlying breakdown + rate).

    2026-11-06 fix (operator: "I see bullet points in loss guard with
    symbol info. need to remove the bullet point.") — the per-underlying
    breakdown used to join symbols with a middle-dot ('·') separator,
    which reads as a bulleted list even though this whole alert pipeline
    is otherwise plain text (see the "Alert message format" entry in
    CLAUDE.md). Plain ", " joins the same data with no decorative glyph.

    2026-11 alert-consolidation fix — these are scope-level facts (both
    `underlyings_breakdown` and the static-rate `rate_val` only depend
    on (df_positions, scope_label), never on which metric fired), so a
    multi-leaf group must emit them ONCE, not once per leaf. When `a`
    carries `agent_engine._ae_group_alert_rows`'s `_grp_*` annotations
    (the normal path, grouped once upstream in `_v2_send_rich_alert`),
    this reads the group-level values it already computed. Falls back to
    the old per-row computation when those keys are absent (a caller
    passing a bare, un-annotated row — every pre-existing test; treated
    as a group of one) so behavior there is unchanged.
    """
    result: list[str] = []
    if '_grp_underlyings_breakdown' in a:
        ub = a['_grp_underlyings_breakdown'] or []
    else:
        ub = a.get('underlyings_breakdown') or []
    if ub:
        pieces = [f"{u['underlying']} {_fmt_rupees_compact(u['pnl'])}" for u in ub]
        result.append("  by und: " + ", ".join(pieces))
    if '_grp_rate_for_enrichment' in a:
        rv = a['_grp_rate_for_enrichment']
    else:
        k = a['kind']
        rv = a.get('rate_val')
        if k in ('rate_abs', 'rate_pct'):
            rv = None
    if rv is not None:
        result.append(f"  rate:   {_fmt_rupees(rv)}/min")
    return result


def _tg_alert_body(alerts: list) -> str:
    """
    Build the narrow 2-line-per-row Telegram body. Each alert gets:
      line 1:  <short> <scope>  <current ₹> (<pct>)
      line 2:    <rule>  <extra / threshold>

    Position alerts can carry two extra lines:
      line 3:    by und: NIFTY -₹22k, BANKNIFTY -₹13k, …
      line 4:    rate:   <rate ₹/min>            (when alert_state had
                                                  enough history for a
                                                  static-alert rate
                                                  reading; rate alerts
                                                  already carry it on
                                                  line 2)

    Keeps rows under ~32 char so they don't wrap on a phone in portrait.

    2026-11-06 fix (operator: "I see bullet points in loss guard with
    symbol info. need to remove the bullet point.") — line 1 used to
    lead with a '▸' marker, which read as a bulleted list item. This
    whole pipeline is otherwise plain text with no markup (see the
    "Alert message format" entry in CLAUDE.md), so the marker is
    dropped; the row's own section/scope/value text is unambiguous
    without it. See `_tg_position_enrichment_lines` for the matching
    fix to the per-underlying separator on line 3.

    2026-11 alert-consolidation fix (operator complaint: several leaves
    of the same agent firing together for one scope produced N near-
    identical blocks) — `alerts` is expected to already carry
    `agent_engine._ae_group_alert_rows`'s `_grp_*` annotations (set
    once, upstream, by `_v2_send_rich_alert`). Reads those via
    `.get(key, <default>)` so a caller passing bare, un-annotated rows
    (every pre-existing test) still renders each row as its own group of
    one — byte-identical to the pre-consolidation output. A group's
    shared header carries the pnl/pct value ONLY when every member
    genuinely agrees (see `_ae_group_alert_rows`'s docstring for why
    that can't be assumed) and appends a plain-ASCII "xN" marker when
    the group has more than one member; when members disagree, the
    header drops the number and each subline shows its own instead
    (`_tg_rule_line`'s `show_own_pnl`) — the operator never loses which
    thresholds tripped, or their values, vs the pre-consolidation output.
    """
    lines = []
    for a in alerts:
        grp_size   = a.get('_grp_size', 1)
        is_first   = a.get('_grp_is_first', True)
        is_last    = a.get('_grp_is_last', True)
        agrees     = a.get('_grp_pnl_agrees', True)
        if is_first:
            short = _SECTION_SHORT.get(a['section'], a['section'][:3].upper())
            if agrees:
                head_right = _fmt_rupees(a['pnl'])
                if a.get('pct') is not None and a['pct'] != 0:
                    head_right += f" ({_fmt_pct(a['pct'])})"
                head = f"{short} {a['scope']}  {head_right}"
            else:
                head = f"{short} {a['scope']}"
            if grp_size > 1:
                head += f"  x{grp_size}"
            lines.append(head)
        lines.append(_tg_rule_line(a, show_own_pnl=not agrees))
        if a['section'] == 'Positions' and is_last:
            lines.extend(_tg_position_enrichment_lines(a))
        if is_last:
            lines.append("")  # blank line between groups for easy scanning
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _email_underlying_subrow(ub: list, bg: str) -> str:
    """Render an HTML sub-row for the per-underlying breakdown in an email alert."""
    sub_cells = ''.join(
        f"<td style='padding:3px 8px;font-size:11px;color:{_EMAIL_TEXT};"
        f"border-right:1px solid {_EMAIL_BORDER}'>"
        f"<b style='color:{_EMAIL_AMBER}'>{u['underlying']}</b> "
        f"<span style='color:{_EMAIL_TEXT_MUTED}'>{_fmt_rupees(u['pnl'])}</span>"
        f"</td>"
        for u in ub
    )
    return (
        f"<tr><td colspan='6' style='padding:0 12px 8px;"
        f"background-color:{bg or _EMAIL_PANEL_BG}'>"
        f"<div style='font-size:11px;color:{_EMAIL_TEXT_MUTED};padding:4px 0 2px'>"
        f"By underlying:</div>"
        f"<table style='border-collapse:collapse'>"
        f"<tr>{sub_cells}</tr></table>"
        f"</td></tr>"
    )


def _email_alert_body(alerts: list) -> str:
    """
    Build a proper HTML table for email. Columns: Type, Account, Rule,
    Current (₹/%), Rate (when applicable), Threshold. Rows are colored by
    severity kind so rate alerts pop visually.

    2026-11 alert-consolidation fix — every leaf still gets its own
    `<tr>` here (unlike the Telegram body's merged header), because each
    leaf's own Current/Rate/Threshold cells are genuinely different
    values for the same scope (see `_ae_group_alert_rows`'s docstring —
    `pnl` disagrees across members in the common case) and a table row
    is the natural place to show them side-by-side, not a duplication
    bug worth consolidating. The ONE real duplication this file had was
    the per-underlying breakdown sub-row, recomputed and reprinted after
    EVERY leaf's row for the same scope — that is a scope-level fact, so
    it's now emitted once per (section, scope) group, on its last
    member (`_grp_is_last`), reading the group-level value
    `agent_engine._ae_group_alert_rows` already computed. Falls back to
    the per-row value when the `_grp_*` annotations are absent (a bare,
    un-annotated row — every pre-existing test), unchanged from before.
    """
    th = (
        f"background-color:{_EMAIL_BG};color:{_EMAIL_AMBER};padding:8px 12px;"
        f"text-align:left;font-size:13px;font-weight:700;letter-spacing:0.04em;"
        f"border-bottom:2px solid {_EMAIL_AMBER};white-space:nowrap"
    )
    # 2026-11-02 overflow fix (operator-reported: email table text
    # overflows its grid) — headers are short, fixed labels ("Type",
    # "Account", "Rule", ...) and stay `nowrap` intentionally. Data
    # cells are NOT bounded the same way (the "Rule" cell today renders
    # `_KIND_LABEL[a['kind']]`, but `cell()` is the general-purpose
    # renderer, so any longer string reaching it hits the same
    # overflow). `width:100%` on the table + `nowrap` on `td` forced the
    # table wider than its container whenever a cell's content exceeded
    # its column's available width. `word-break` and `overflow-wrap`
    # are deliberately doubled up (harmless when both apply) because
    # email-client HTML rendering (Outlook/Gmail/Apple Mail) is
    # inconsistent about which one it honours.
    td = (
        f"padding:7px 12px;font-size:13px;color:{_EMAIL_TEXT};"
        f"border-bottom:1px solid {_EMAIL_BORDER};white-space:normal;"
        f"word-break:break-word;overflow-wrap:break-word"
    )
    # Per-kind row tints in algo dark palette. Static = amber-tinted
    # navy, rate = red-tinted, fund-negative = slate. Each is a
    # mid-luminance fill that stays readable against the light text.
    row_bg = {
        'static_pct': '#3a2c10',   # amber-tinted dark
        'static_abs': '#3a2c10',
        'rate_abs':   '#3a1010',   # red-tinted dark
        'rate_pct':   '#3a1010',
        'negative_cash':   '#1f2a3f',  # slate-tinted dark
        'negative_margin': '#1f2a3f',
    }

    def cell(v, bg=""):
        style = td + (f";background-color:{bg}" if bg else "")
        return f"<td style='{style}'>{v}</td>"

    header_cells = "".join(
        f"<th style='{th}'>{h}</th>"
        for h in ("Type", "Account", "Rule", "Current P&L", "Rate", "Threshold")
    )
    row_html = ""
    for a in alerts:
        bg = row_bg.get(a['kind'], "")
        current = _fmt_rupees(a['pnl'])
        if a.get('pct') is not None and a['pct'] != 0:
            current += f"<br><span style='color:#555;font-size:11px'>{_fmt_pct(a['pct'])}</span>"
        # Rate column — always show when rate_val is set, regardless of
        # whether the rule itself is rate-based. Static position alerts
        # now carry rate_val too (computed by agent_engine from the same
        # pnl_history rate metrics use). Format follows the metric
        # family — % for percentage rates, ₹ otherwise.
        if a.get('rate_val') is None:
            rate = "—"
        elif a['kind'] == 'rate_pct':
            rate = f"{_fmt_pct(a['rate_val'])}/min"
        else:
            rate = f"{_fmt_rupees(a['rate_val'])}/min"
        row_html += (
            "<tr>"
            + cell(a['section'], bg)
            + cell(a['scope'], bg)
            + cell(_KIND_LABEL[a['kind']], bg)
            + cell(current, bg)
            + cell(rate, bg)
            + cell(a['threshold'], bg)
            + "</tr>"
        )
        # Per-underlying breakdown sub-row — only for Position alerts
        # that carry the breakdown payload. Emitted ONCE per (section,
        # scope) group (on its last member), not once per leaf — see
        # this function's own docstring.
        if '_grp_underlyings_breakdown' in a:
            ub = a['_grp_underlyings_breakdown'] or []
        else:
            ub = a.get('underlyings_breakdown') or []
        is_last = a.get('_grp_is_last', True)
        if a['section'] == 'Positions' and is_last and ub:
            row_html += _email_underlying_subrow(ub, bg)
    return (
        f"<table style='border-collapse:collapse;width:100%;"
        f"background-color:{_EMAIL_PANEL_BG};border:1px solid {_EMAIL_BORDER};"
        f"border-radius:4px;overflow:hidden'>"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{row_html}</tbody>"
        f"</table>"
    )

