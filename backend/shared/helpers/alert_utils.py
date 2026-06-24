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
  - _dispatch()      — channel router (Telegram + SMTP + log), gated by
                        cap_in_dev.telegram / cap_in_dev.mail.

Secrets (secrets.yaml)
  telegram_bot_token  bot token from @BotFather
  telegram_chat_id    group chat_id (negative integer for groups)
  alert_emails        list of email addresses to notify

Message type prefixes
  Telegram : Open | Agent | Close
  Email    : RamboQuant Open: | RamboQuant Agent: | RamboQuant Close:
"""

import atexit
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import requests

from backend.shared.helpers.mail_utils import send_email
from backend.shared.helpers.ramboq_logger import get_logger
import urllib3.util.connection
urllib3.util.connection.HAS_IPV6 = False  # Server IPv6 outbound hangs

from backend.shared.helpers.utils import secrets, config, is_enabled

logger = get_logger(__name__)

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
            seen: set[str] = set()
            out: list[str] = []
            for e in dev_emails:
                if e and e not in seen:
                    seen.add(e)
                    out.append(e)
            return out
        # No dev override configured — fall through to prod list so
        # alerts still go somewhere visible during dev testing.

    with _alert_recipients_lock:
        db_part = list(_alert_recipients_cache)
    static_part = secrets.get('alert_emails', []) or []
    seen: set[str] = set()
    out: list[str] = []
    for e in db_part + static_part:
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


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


def _send_telegram(message: str):
    import logging
    _log = logging.getLogger('backend.api.background')
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
    token = secrets.get('telegram_bot_token', '')
    chat_id = secrets.get('telegram_chat_id', '')
    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        if resp.ok:
            _log.info("Telegram alert sent")
        else:
            _log.error(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        _log.error(f"Telegram error: {e}")


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


def _html_table(headers, rows):
    """Render a list of string-tuple rows as an HTML table for email,
    styled in the algo dark palette."""
    th_style = (
        f"background-color:{_EMAIL_BG};color:{_EMAIL_AMBER};padding:8px 12px;"
        f"text-align:left;font-family:monospace;font-size:13px;"
        f"font-weight:700;letter-spacing:0.04em;"
        f"border-bottom:2px solid {_EMAIL_AMBER};white-space:nowrap"
    )
    td_style = (
        f"padding:6px 12px;font-family:monospace;font-size:13px;"
        f"background-color:{_EMAIL_PANEL_BG};color:{_EMAIL_TEXT};"
        f"border-bottom:1px solid {_EMAIL_BORDER};white-space:nowrap"
    )
    td_alt_style = td_style.replace(_EMAIL_PANEL_BG, _EMAIL_PANEL_ALT)
    # TOTAL row — stronger amber tint so the rollup stands out from the
    # data rows above it. Matches the on-screen TOTAL row stratum.
    td_total_style = (
        f"padding:8px 12px;font-family:monospace;font-size:13px;"
        f"background-color:#3a2c10;color:{_EMAIL_AMBER};font-weight:700;"
        f"border-top:2px solid {_EMAIL_AMBER};"
        f"border-bottom:1px solid {_EMAIL_AMBER_SOFT};white-space:nowrap"
    )

    def _is_total_row(row):
        # First cell is account / label; flag the rollup row by name.
        return bool(row) and str(row[0]).strip().upper() in ("TOTAL", "TOTALS", "GRAND TOTAL")

    header_cells = "".join(f"<th style='{th_style}'>{h}</th>" for h in headers)
    row_html = ""
    for i, row in enumerate(rows):
        if _is_total_row(row):
            bg = td_total_style
        else:
            bg = td_alt_style if i % 2 else td_style
        cells = "".join(f"<td style='{bg}'>{v}</td>" for v in row)
        row_html += f"<tr>{cells}</tr>"

    return (
        f"<table style='border-collapse:collapse;width:100%;"
        f"background-color:{_EMAIL_PANEL_BG};border:1px solid {_EMAIL_BORDER};"
        f"border-radius:4px;overflow:hidden'>"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{row_html}</tbody>"
        f"</table>"
    )


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


def _dispatch(msg_type: str, ist_display: str, tg_table: str, email_table_html: str,
              subject_detail: str, sim_mode: bool = False, mode_tag: str = ''):
    """
    Send Telegram + email with correct prefixes for the message type.

    When `sim_mode` is True every surface (subjects, Telegram preamble, email
    banner, log lines) is tagged `SIMULATOR` so the operator can distinguish
    a simulated fire from a real one.

    `mode_tag` is the additional execution-mode marker for prod alerts —
    typically `[PAPER]` (when this agent's broker actions all wrote
    paper rows) or `[MIXED]` (some paper, some live). Empty string for
    "all live" (default real-mode alert) or for non-broker agents.
    """
    import logging
    _log = logging.getLogger('backend.api.background')
    sim_prefix = '[SIM] ' if sim_mode else ''
    _log.info(f"_dispatch called: {sim_prefix}{mode_tag}{msg_type} — {subject_detail}")
    tg_prefix, email_prefix = _MSG_TYPES[msg_type]
    tg_prefix_full    = f"SIMULATOR {tg_prefix}"    if sim_mode else tg_prefix
    email_prefix_full = f"SIMULATOR {email_prefix}" if sim_mode else email_prefix

    branch = config.get('deploy_branch', 'main')
    branch_tag = f" [{branch}]" if branch != 'main' else ''
    # mode_tag goes immediately after the message-type prefix so it's
    # readable on Telegram + at the start of the email subject.
    mode_pfx = f"{mode_tag} " if mode_tag else ''

    # Telegram: fixed-width monospace table; branch + simulator warning lines
    warning_lines = []
    if sim_mode:
        warning_lines.append("&#128680; <b>SIMULATOR RUN</b> — fabricated market data")
    if branch != 'main':
        warning_lines.append(f"⚠ <b>Branch: {branch}</b>")
    warning_block = ("\n" + "\n".join(warning_lines)) if warning_lines else ''

    telegram_msg = (
        f"<b>{tg_prefix_full}{branch_tag} {mode_pfx}— {ist_display}</b>{warning_block}\n\n"
        f"<code>{tg_table}</code>"
    )
    _send_telegram(telegram_msg)

    # Operator request (Jun 2026): market open/close summaries ship
    # Telegram-only. Agent alerts ('alert' msg_type) continue to fan
    # out across both Telegram + email per the existing operator
    # alert recipients. Deploy notifications are sent by
    # webhook/notify_deploy.py against a dedicated Telegram channel
    # (telegram_chat_id_deploy), already Telegram-only.
    if msg_type in ('open', 'close'):
        return

    alert_emails = get_alert_recipients()
    if alert_emails:
        subj_pfx = f"{email_prefix_full}{branch_tag}{(' ' + mode_tag) if mode_tag else ''}"
        subject = f"{subj_pfx}{subject_detail}" if (branch_tag or mode_tag) else f"{email_prefix_full}{subject_detail}"
        banners = ''
        if sim_mode:
            banners += _sim_banner_html()
        if branch != 'main':
            banners += _branch_banner_html(branch)
        html_body = (
            f"<html><body style='font-family:sans-serif;background-color:{_EMAIL_BG};"
            f"color:{_EMAIL_TEXT};margin:0;padding:18px'>"
            f"<div style='max-width:760px;margin:0 auto'>"
            f"{banners}"
            f"<p style='font-size:14px;color:{_EMAIL_AMBER};letter-spacing:0.04em;"
            f"margin:0 0 14px 0'>"
            f"<b>{tg_prefix_full}{branch_tag} — {ist_display}</b></p>"
            f"{email_table_html}"
            f"</div>"
            f"</body></html>"
        )
        # Offload SMTP to the bounded _SMTP_EXECUTOR pool so a Hostinger
        # connection-timeout (up to 30s) never stalls the event loop and
        # concurrent fires can't spawn unbounded daemon threads.
        # Fire-and-forget: failures are logged inside the wrapper.
        for email in alert_emails:
            def _send_one(addr=email, subj=subject, body=html_body,
                          pfx=f"{sim_prefix}{tg_prefix}"):
                try:
                    send_email("", addr, subj, body)
                    logger.info(f"{pfx} email sent to {addr}")
                except Exception as _e:
                    logger.error(f"Failed to send {pfx} email to {addr}: {_e}")
            _SMTP_EXECUTOR.submit(_send_one)


# ---------------------------------------------------------------------------
# Funds table helpers
# ---------------------------------------------------------------------------

def _build_funds_rows(df_margins):
    """Build (Account, Cash, Avail Margin, Used Margin, Collateral) rows from df_margins."""
    rows = []
    if df_margins is None or df_margins.empty:
        return rows
    for _, row in df_margins.iterrows():
        account   = str(row.get('account', ''))
        cash      = float(row.get('avail opening_balance', 0) or 0)
        avail_net = float(row.get('net', 0) or 0)
        used      = float(row.get('util debits', 0) or 0)
        collat    = float(row.get('avail collateral', 0) or 0)
        rows.append((account, _fmt_inr(cash), _fmt_inr(avail_net),
                     _fmt_inr(used), _fmt_inr(collat)))
    return rows


# ---------------------------------------------------------------------------
# Open / Close summary
# ---------------------------------------------------------------------------

def send_summary(sum_holdings, sum_positions, ist_display: str, msg_type: str,
                 label: str = "", df_margins=None, df_positions=None):
    """
    Send holdings + positions + funds summary at market open or close.
    msg_type: 'open' or 'close'
    df_margins: full margins dataframe (all accounts + TOTAL); included when provided.
    df_positions: raw broker positions dataframe. When provided AND
        `alerts.summary_show_underlying_breakdown` is true, an extra
        per-underlying section is appended after the Positions table —
        same format as the per-alert breakdown so the operator's eye
        moves naturally between the two surfaces.
    """
    # Holdings table: Account | Cur Val | P&L | P&L% | Day Loss | Day Loss%
    h_headers = ("Account", "Cur Val", "P&L", "P&L%", "Day Loss", "Day Loss%")
    h_rows = []
    for _, row in sum_holdings.iterrows():
        account  = str(row.get('account', ''))
        cur_val  = float(row.get('cur_val', 0) or 0)
        pnl      = float(row.get('pnl', 0) or 0)
        pnl_pct  = float(row.get('pnl_percentage', 0) or 0)
        day_val  = float(row.get('day_change_val', 0) or 0)
        day_pct  = float(row.get('day_change_percentage', 0) or 0)
        h_rows.append((
            account,
            _fmt_inr(cur_val),
            _fmt_inr(pnl),
            f"{pnl_pct:.2f}%",
            _fmt_inr(day_val),
            f"{day_pct:.2f}%",
        ))

    # Positions table: Account | P&L
    p_headers = ("Account", "P&L")
    p_rows = []
    for _, row in sum_positions.iterrows():
        account = str(row.get('account', ''))
        pnl     = float(row.get('pnl', 0) or 0)
        p_rows.append((account, _fmt_inr(pnl)))

    # Funds table: Account | Cash | Avail Margin | Used Margin | Collateral
    f_headers = ("Account", "Cash", "Avail Margin", "Used Margin", "Collateral")
    f_rows = _build_funds_rows(df_margins)

    segment_label = f" — {label}" if label else ""
    subject_detail = f"{label + ' — ' if label else ''}{ist_display}"

    # ── Per-underlying breakdown (optional) ──────────────────────────
    # Settings-gated; defaults to enabled. Reuses the same helper the
    # per-alert formatter uses so the format stays consistent. Returns
    # an empty list on any failure (settings cache miss, missing
    # column, etc.) so the summary always sends.
    und_rows: list[dict] = []
    try:
        from backend.shared.helpers.settings import get_bool, get_int
        from backend.shared.helpers.summarise import (
            breakdown_positions_by_underlying,
        )
        if df_positions is not None and get_bool(
                'alerts.summary_show_underlying_breakdown', True):
            top_n = get_int('alerts.max_underlyings_per_alert', 5)
            und_rows = breakdown_positions_by_underlying(
                df_positions, account=None, top_n=top_n,
            )
    except Exception as e:
        logger.warning(f"summary underlying breakdown failed: {e}")

    # Telegram: fixed-width monospace
    h_tg = _fixed_table(h_headers, h_rows) if h_rows else "No holdings data"
    p_tg = _fixed_table(p_headers, p_rows) if p_rows else "No positions data"
    tg_table = f"Holdings{segment_label}\n{h_tg}\n\nPositions{segment_label}\n{p_tg}"
    if f_rows:
        f_tg = _fixed_table(f_headers, f_rows)
        tg_table += f"\n\nFunds\n{f_tg}"
    if und_rows:
        # Compact one-line-per-underlying so the section stays short
        # on a phone. Sign + ₹ format reads at a glance.
        und_lines = "\n".join(
            f"  {u['underlying']:<10} {_fmt_rupees(u['pnl'])}"
            for u in und_rows
        )
        tg_table += f"\n\nBy underlying\n{und_lines}"

    # Email: HTML tables with section headings
    h_email = _html_table(h_headers, h_rows) if h_rows else "<p>No holdings data</p>"
    p_email = _html_table(p_headers, p_rows) if p_rows else "<p>No positions data</p>"
    email_table_html = (
        f"<p style='margin-top:16px;font-weight:bold'>Holdings{segment_label}</p>"
        f"{h_email}"
        f"<p style='margin-top:16px;font-weight:bold'>Positions{segment_label}</p>"
        f"{p_email}"
    )
    if f_rows:
        f_email = _html_table(f_headers, f_rows)
        email_table_html += f"<p style='margin-top:16px;font-weight:bold'>Funds</p>{f_email}"
    if und_rows:
        und_html = _html_table(
            ("Underlying", "P&L", "Positions"),
            [(u['underlying'], _fmt_rupees(u['pnl']), str(u['count']))
             for u in und_rows],
        )
        email_table_html += (
            f"<p style='margin-top:16px;font-weight:bold'>By underlying</p>"
            f"{und_html}"
        )

    _dispatch(msg_type, ist_display, tg_table, email_table_html, subject_detail)
    logger.info(f"Background: {msg_type} summary sent")


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
) -> None:
    """
    Telegram + email alert when an order placement fails.

    Deduplicated by (masked_account, symbol, side, error_signature)
    within `_ORDER_ALERT_COOLDOWN_SEC`.  Suppressed fires increment a
    counter that flows into the next dispatched alert as "(+N suppressed)".

    All exceptions are swallowed — a broken Telegram connection must never
    interrupt order placement or the chase loop.
    """
    try:
        from backend.shared.helpers.date_time_utils import timestamp_display
        from backend.shared.helpers.utils import mask_account

        masked = mask_account(account)
        sig    = _error_sig(error)
        key    = (masked, symbol.upper(), side.upper(), sig)
        now    = datetime.utcnow()

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
                    return
                # Cooldown elapsed — fire; carry suppressed count
                suppressed_count = entry["suppressed"]
                entry["last_sent"]  = now
                entry["suppressed"] = 0
            else:
                suppressed_count = 0
                _order_alert_state[key] = {
                    "first_seen": now, "last_sent": now, "suppressed": 0,
                }

        branch   = config.get("deploy_branch", "main")
        ist_disp = timestamp_display()
        mode_tag = f"[{mode.upper()}]" if mode else ""
        sup_note = f"  (+{suppressed_count} suppressed)" if suppressed_count else ""
        error_short = error[:160].strip()

        # Telegram — compact monospace block
        tg_body = (
            f"<b>&#10060; Order rejected</b>  {mode_tag}{sup_note}\n"
            f"{masked}  {side}  {qty}  {symbol}  ({exchange})\n"
            f"source: {source}\n"
            f"<code>{error_short}</code>"
        )

        # Email — HTML table
        rows_html = _html_table(
            ("Field", "Value"),
            [
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
              f"{' ' + mode_tag if mode_tag else ''}</p>"
            + rows_html
            + f"</body></html>"
        )

        subject = (
            f"RamboQuant Order Rejected: {symbol} {side}"
            + (f" [{branch}]" if branch != "main" else "")
            + (f" ({mode})" if mode else "")
        )

        # Deliver — best-effort; never raises out of this function.
        # Offload SMTP to the bounded _SMTP_EXECUTOR pool so a timeout never
        # stalls the chase loop or agent run_cycle event-loop iteration.
        _send_telegram(tg_body)
        alert_emails = get_alert_recipients()
        for email in alert_emails:
            def _send_failure_email(addr=email, subj=subject, body=email_body):
                try:
                    send_email("", addr, subj, body)
                except Exception as _mail_e:
                    logger.error(f"order-failure email to {addr} failed: {_mail_e}")
            _SMTP_EXECUTOR.submit(_send_failure_email)

        logger.warning(
            f"order-failure alert sent: {masked} {side} {qty} {symbol} "
            f"mode={mode} source={source}{sup_note}"
        )
    except Exception as _top_e:
        logger.error(f"send_order_failure_alert internal error: {_top_e}")


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


def _tg_alert_body(alerts: list) -> str:
    """
    Build the narrow 2-line-per-row Telegram body. Each alert gets:
      line 1:  ▸ <short> <scope>  <current ₹> (<pct>)
      line 2:    <rule>  <extra / threshold>

    Position alerts can carry two extra lines:
      line 3:    by und: NIFTY -₹22k · BANKNIFTY -₹13k · …
      line 4:    rate:   <rate ₹/min>            (when alert_state had
                                                  enough history for a
                                                  static-alert rate
                                                  reading; rate alerts
                                                  already carry it on
                                                  line 2)

    Keeps rows under ~32 char so they don't wrap on a phone in portrait.
    """
    lines = []
    for a in alerts:
        short = _SECTION_SHORT.get(a['section'], a['section'][:3].upper())
        head_right = _fmt_rupees(a['pnl'])
        if a.get('pct') is not None and a['pct'] != 0:
            head_right += f" ({_fmt_pct(a['pct'])})"
        lines.append(f"▸ {short} {a['scope']}  {head_right}")

        # Second line varies slightly by rule so the "why" is obvious at a glance.
        k = a['kind']
        label = _KIND_LABEL[k]
        if k == 'static_pct':
            lines.append(f"  {label}  floor {a['threshold']}")
        elif k == 'static_abs':
            lines.append(f"  {label}  floor {a['threshold']}")
        elif k == 'rate_abs':
            lines.append(f"  {label}  now {_fmt_rupees(a['rate_val'])}/min  "
                         f"floor {a['threshold']}")
        elif k == 'rate_pct':
            lines.append(f"  {label}  now {_fmt_pct(a['rate_val'])}/min  "
                         f"floor {a['threshold']}")
        else:
            lines.append(f"  {label}  {a['threshold']}")

        # Optional enrichment for position alerts. Compact ₹ formatting
        # keeps the line under the 32-char rule of thumb.
        if a['section'] == 'Positions':
            ub = a.get('underlyings_breakdown') or []
            if ub:
                pieces = [f"{u['underlying']} {_fmt_rupees_compact(u['pnl'])}"
                          for u in ub]
                lines.append("  by und: " + " · ".join(pieces))
            # Static-alert rate enrichment — rate alerts already showed
            # `now <rate>/min` on line 2 so we suppress to avoid the
            # dupe.
            rv = a.get('rate_val')
            if rv is not None and k not in ('rate_abs', 'rate_pct'):
                lines.append(f"  rate:   {_fmt_rupees(rv)}/min")

        lines.append("")  # blank line between alerts for easy scanning
    if lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _email_alert_body(alerts: list) -> str:
    """
    Build a proper HTML table for email. Columns: Type, Account, Rule,
    Current (₹/%), Rate (when applicable), Threshold. Rows are colored by
    severity kind so rate alerts pop visually.
    """
    th = (
        f"background-color:{_EMAIL_BG};color:{_EMAIL_AMBER};padding:8px 12px;"
        f"text-align:left;font-size:13px;font-weight:700;letter-spacing:0.04em;"
        f"border-bottom:2px solid {_EMAIL_AMBER};white-space:nowrap"
    )
    td = (
        f"padding:7px 12px;font-size:13px;color:{_EMAIL_TEXT};"
        f"border-bottom:1px solid {_EMAIL_BORDER};white-space:nowrap"
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
        # that carry the breakdown payload. Renders as a nested table
        # spanning all 6 columns so the operator sees the contributing
        # underlyings without leaving the alert.
        ub = a.get('underlyings_breakdown') or []
        if a['section'] == 'Positions' and ub:
            sub_cells = ''.join(
                f"<td style='padding:3px 8px;font-size:11px;color:{_EMAIL_TEXT};"
                f"border-right:1px solid {_EMAIL_BORDER}'>"
                f"<b style='color:{_EMAIL_AMBER}'>{u['underlying']}</b> "
                f"<span style='color:{_EMAIL_TEXT_MUTED}'>{_fmt_rupees(u['pnl'])}</span>"
                f"</td>"
                for u in ub
            )
            row_html += (
                f"<tr><td colspan='6' style='padding:0 12px 8px;"
                f"background-color:{bg or _EMAIL_PANEL_BG}'>"
                f"<div style='font-size:11px;color:{_EMAIL_TEXT_MUTED};padding:4px 0 2px'>"
                f"By underlying:</div>"
                f"<table style='border-collapse:collapse'>"
                f"<tr>{sub_cells}</tr></table>"
                f"</td></tr>"
            )
    return (
        f"<table style='border-collapse:collapse;width:100%;"
        f"background-color:{_EMAIL_PANEL_BG};border:1px solid {_EMAIL_BORDER};"
        f"border-radius:4px;overflow:hidden'>"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{row_html}</tbody>"
        f"</table>"
    )

