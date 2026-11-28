"""Zerodha Kite Console (web) Tradebook / P&L report scraper + ingester.

WHY THIS EXISTS — Zerodha's sanctioned Kite Connect API has no historical
P&L/trade-report endpoint. Confirmed directly against the installed SDK:
`kiteconnect.KiteConnect.orders()` / `.order_history()` / `.trades()` take
no date range at all, and there is no `get_pnl()` / `get_statement()` / any
report method anywhere on the client. Kite Console's own web UI (the human
dashboard at kite.zerodha.com) is the ONLY place a historical Tradebook or
P&L export exists, so this script drives it with Playwright instead of a
sanctioned API call, strictly for this one offline report pull — it is NOT
a general-purpose broker-automation layer and must never be used for order
placement.

WHERE THIS RUNS — standalone, outside `ramboq_api`/`ramboq_conn`. Browser
automation against a real-money account has no business running inside the
process that places real orders or inside the ticker/connection service.
Invoke via cron (operator-managed; this script does not install its own
schedule) in one of two modes:

    venv/bin/python -m scripts.kite_console_pnl_sync --mode backfill
    venv/bin/python -m scripts.kite_console_pnl_sync --mode sync

MODES
  backfill — one-time, operator-triggered. Pulls equity + derivatives +
             commodities, 2026-03-01 -> today, for every active Kite
             account (or the accounts named via --account).
  sync     — ongoing. Pulls derivatives + commodities ONLY (equity is
             deliberately excluded — equity holdings/positions already
             come from the sanctioned API elsewhere in this app and don't
             need this mechanism going forward). Only actually scrapes on
             the first trading day on/after an MCX or NFO expiry that
             hasn't been synced yet — see `should_sync_today()`. A missed
             run is NOT a missed expiry: the "synced through" marker
             (persisted via the `task_markers.*` settings convention) lets
             the next run catch up instead of silently skipping it.

LOGIN / COOLOFF REUSE — this is the single most important design
constraint. `KiteConsoleConnection` below subclasses
`backend.brokers.connections.KiteConnection` and overrides TWO hooks:
`init_kite_conn()` (the browser login itself — the one thing
`_attempt_login_with_cooloff()`, inherited and otherwise byte-for-byte
unmodified, calls) and `_login_cooloff_path()` (which `KiteConnection`
itself calls, dynamically, from `_try_restore_token()` and
`_attempt_login_with_cooloff()`, instead of referencing its own module
global directly — see that method's own docstring in `connections.py`).
Every other piece of the real, already-shipped cross-call cooloff state
machine is reused as-is: the escalating backoff (120s -> 240s -> 480s ->
960s -> capped 1800s, computed by the shared
`exponential_backoff_seconds()` in `backend/shared/helpers/backoff.py`)
and the in-memory `_login_fail_streak`/`_login_blocked_until`/
`_blocked_alert_sent` fields.

  Cooloff INDEPENDENCE, stated explicitly (2026-11-26 — reverses the
  original "deliberate coupling" design below, per an explicit operator
  decision that the coupling is unacceptable): a failed Console web
  login for an account arms ONLY `_CONSOLE_LOGIN_COOLOFF_PATH`'s cooloff
  file, never `backend.brokers.connections._KITE_LOGIN_COOLOFF_PATH` (the
  live `ramboq_conn` process's sanctioned REST login path for the same
  account), and vice versa. The two connection OBJECTS were already
  independent in memory (this script builds its own, separate
  `KiteConnection`-subclass instance per account, never touching the live
  singleton `Connections` registry) — the only thing that was ever
  actually shared was the on-disk cooloff FILE, keyed by the same real
  account code on both sides. Overriding `_login_cooloff_path()` on this
  one mixin closes that gap with no change to the base class's own
  default behaviour for every other caller.

  Historical note (superseded by the independence above, kept for
  context): the original design deliberately shared one file, reasoning
  that Zerodha's anomaly-detection block is reported as IP-level and this
  script runs from the same server IP as `ramboq_conn`, so a failed
  Console login was corroborating evidence the same account might be
  blocked. The operator decided this tradeoff — losing the live
  trading account's own re-logins for up to 30 minutes because an
  unrelated, still-unverified Console page selector broke — was not
  worth that corroboration.

  Flagged, NOT fixed by this independence (a separate, still-real
  concern): `sync_account()` below still holds
  `_cross_process_login_lock(conn.account)` — the LIVE REST path's own
  cross-process file lock — for the ENTIRE Playwright browser-login
  sequence (goto_login -> submit_credentials -> submit_totp ->
  verify_logged_in), which can plausibly run longer than that lock's own
  `_LOCK_TIMEOUT_S` (30s). While held, a concurrent live REST re-login
  attempt for the SAME account would wait up to 30s and then raise
  `BrokerNetworkError`. This is independent of the cooloff-file fix (a
  different mechanism — a lock, not a persisted block) and was not
  changed here; whether Console and live logins for one account need to
  serialise at all depends on Zerodha behaviour this script's author has
  no way to verify from here.

  Also flagged, NOT fixed: `KiteConsoleConnection` does not override
  `_try_restore_token()`, so a freshly-built console connection object
  still loads the LIVE REST path's own cached access token from the
  shared `_TOKEN_CACHE_PATH` file on construction (via the unmodified
  base-class `__init__`). This is inert today — `sync_account()` below
  calls `_attempt_login_with_cooloff()` directly and never calls
  `get_kite_conn()`/`_validate_or_clear_kite_token()` on this object — but
  if any future caller ever did, a `profile()` failure there would call
  `_save_cached_token(account, '')` and wipe the LIVE account's token
  cache. `_load_cached_token`/`_save_cached_token` are module-level free
  functions hardcoded to `connections._TOKEN_CACHE_PATH` (not
  parameterised per-call the way the cooloff path now is), so fixing this
  properly is a materially bigger change than the cooloff-path hook above
  — left as a flagged risk, not fixed in this pass.

  Login-step failures (goto_login / submit_credentials / submit_totp /
  verify_logged_in) feed this cooloff, because they ARE the login.
  Post-login SCRAPING failures (navigate_to_reports / select_segment /
  set_date_range / trigger_download / report validation) must NEVER touch
  it — a broken report-page selector has nothing to do with whether this
  account's login is being rate-limited. See `ConsoleStructuralError`'s own
  docstring for how that boundary is enforced in code.

  `_login_fail_streak` is in-memory only (same as the base class) — a
  one-shot script therefore always arms the FIRST cooloff tier (120s) on
  its first-ever failure in a fresh process, even if a previous process
  had already escalated further. This is faithful reuse of the existing
  policy, not a bug to "fix" here.

CREDENTIALS COME FROM THE DATABASE, NOT secrets.yaml — per this project's
own CLAUDE.md entry on `broker_accounts` being the authoritative source for
an EXISTING account's credentials (secrets.yaml is only consulted for
first-time seeding), this script reads the `broker_accounts` table
(shared `ramboq` DB) and decrypts `api_secret_enc` / `password_enc` /
`totp_token_enc` the exact same way `Connections._build_kite_conn()`
already does, rather than reading `secrets.yaml` directly. A stale
secrets.yaml value would otherwise be used here, trigger a real login
failure against a real account, and arm the shared cooloff above for no
reason.

MASKING — `register_accounts()` is called once at startup with every
active account code, so `mask_account()` produces the SAME `Z1####`-style
form the live app uses everywhere else, not the degraded all-digits-masked
fallback. A masked account string is used as the `account` key stored in
the new `kite_console_pnl_sync` table AND in every log/alert this script
emits. Operator note, flagged rather than silently assumed safe: the
masked form's ordinal (`Z1`/`Z2`/...) is assigned by alphabetically sorting
the CURRENT active-account list, so it can shift if an account is
added/removed/renamed — it is not a permanently stable key across the
account roster's lifetime, same caveat that already applies to every other
masked-account surface in this codebase. A raw HTML diagnostics dump
cannot be run through `mask_account_in_text()` the way a log line can (it's
a full DOM document, not a known-shape string), so the diagnostics
directory is created with 0700 permissions instead — see
`capture_diagnostics()`.

ALERT DELIVERY FROM A SEPARATE, SHORT-LIVED PROCESS — verified, not
assumed, and genuinely different from the always-running `ramboq_api`
process this codebase's "log a tagged record, let the event-agent pipeline
dispatch it" convention was designed for. `get_logger()` enqueues onto a
`queue.Queue` consumed by a `logging.handlers.QueueListener` BACKGROUND
THREAD that IS started automatically at import time (in
`ramboq_logger.py`, independent of asyncio) — so a `logger.critical(...)`
call here genuinely does land in `backend.shared.helpers.log_store.HANDLER`'s
in-memory queue within a very short time. BUT that queue is only ever
drained and DISPATCHED to the real event-agent alert pipeline
(`event_agents.dispatch_rows()`) by `log_store.run_forever()` — an asyncio
task started via `log_store.start(process)`, itself only ever called from
`backend/api/app.py`'s startup inside the long-lived `ramboq_api` process.
A standalone script never starts that loop, so a bare `logger.critical(...)`
call here would sit in memory and be silently lost the instant this
process exits — the same failure class as the real, already-documented
19-day `_SKIP_PREFIXES` alert-drop incident in this codebase's CLAUDE.md,
just via a different mechanism. `_flush_and_dispatch_once()` below closes
this gap by running exactly ONE real flush+dispatch cycle — the literal
same `log_store.flush_once()` / `event_agents.dispatch_rows()` functions
`run_forever()` calls on its own timer — right before this script exits.
No alert-sending logic is duplicated; this just runs the existing pipeline
once, synchronously, instead of waiting for a timer that will never fire.

REPORT TYPE AND IDEMPOTENCY — see `KiteConsolePnlSync`'s own docstring in
`backend/api/models.py` for the full reasoning. In short: the Tradebook
(trade-level rows keyed by the broker's own `trade_id`) is the row-grain
source of truth ingested here, because it supports a genuinely stable
idempotency key across any number of overlapping pulls. The P&L statement
aggregates each symbol over the REQUESTED date range, so naively keying it
on symbol alone would let a later partial-range pull silently overwrite a
wider aggregate with a narrower one — this script's P&L ingestion path (if
exercised) therefore keys on (symbol, expiry, requested_range) instead, so
two different pull ranges are stored as two different rows rather than
merged into one incorrect number.

SELECTORS ARE PLACEHOLDERS, NOT VERIFIED — Kite Console's real page
structure has NOT been checked against a live session as part of this
change (no live credentials were available in this sandboxed task). Every
Playwright locator lives inside `KiteConsoleReportsPage` below, and EVERY
ONE of them must be verified (and likely corrected) against a real session
before the first real run — see that class's own docstring for exactly
which lines to re-check. Nothing outside that one class should need to
change when the selectors are corrected: the retry-never / verify-every-step
/ screenshot-and-alert-on-failure structure around it is independent of the
exact selector strings.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import re
import sys
import time as _time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Optional

# Allow `python scripts/kite_console_pnl_sync.py` (not just `-m
# scripts.kite_console_pnl_sync`) to resolve `backend.*` imports when run
# from an arbitrary cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.shared.helpers.ramboq_logger import get_logger  # noqa: E402

logger = get_logger(__name__)

_DIAGNOSTICS_DIR = _REPO_ROOT / ".log" / "kite_console_diagnostics"
# `data/` (not `.log/`) -- these are the raw downloaded CSV reports, kept
# for further processing/audit, never deleted after ingestion (see
# trigger_download()'s own docstring). `.log/` reads as disposable
# diagnostic output; these are the source records this script's own
# `kite_console_pnl_sync` table is derived from. `/data/` is already a
# recognised top-level convention in this repo (gitignored at the repo
# root) for exactly this kind of persistent, non-code artifact.
_DOWNLOADS_DIR = _REPO_ROOT / "data" / "kite_console_downloads"

_BACKFILL_START = date(2026, 4, 1)  # was 2026-03-01; operator instruction, 2026-11-26
_EXPIRY_LOOKBACK_DAYS = 10
# Wide enough to span two consecutive monthly F&O expiry cycles (with
# buffer) in daily_book position history, so `resolve_sync_plan()` can
# see both the "current" (just-triggered) and "previous" expiry for an
# exchange — never a second, independent expiry calculation, just a
# wider window over the SAME `collect_recent_fo_expiry_dates()` query
# `should_sync_today()`'s own trigger-date detection already uses.
_SYNC_RANGE_LOOKBACK_DAYS = 70
# Conservative start bound used ONLY when an exchange's PREVIOUS expiry
# can't be resolved from recent daily_book history (nothing was held
# that cycle) — never a silent narrowing to just the trigger day.
_SYNC_RANGE_FALLBACK_DAYS = 35
_SYNC_MARKER_KEY = "task_markers.kite_console_pnl_sync_last_expiry_date"

# PLACEHOLDER — Kite Console's own segment-selector label text. Verify
# against a real session; see KiteConsoleReportsPage's docstring.
_SEGMENT_CONSOLE_VALUE = {
    "equity": "EQ",
    "derivatives": "FO",
    "commodities": "COM",
}

# F&O exchange -> which holiday calendar governs its "next market open"
# (mirrors the convention already used elsewhere in this codebase, e.g.
# derivatives.py's own exchange->holiday_exchange mapping for NFO/BFO/CDS).
_HOLIDAY_EXCHANGE = {"NFO": "NSE", "BFO": "NSE", "CDS": "NSE", "MCX": "MCX"}

# Confirmed live, 2026-11-26, against a real Tradebook CSV export (see
# parse_tradebook_csv's own docstring for the full real header).
_REQUIRED_TRADEBOOK_COLUMNS = ("symbol", "trade_date", "quantity", "price", "trade_id")

# Confirmed live, 2026-11-26, against a real P&L-statement XLSX export
# (see parse_pnl_statement_xlsx's own docstring). The real export also
# carries ISIN/Quantity/Buy Value/Sell Value/Realized P&L Pct./Previous
# Closing Price/Open Quantity/Open Quantity Type/Open Value/Unrealized
# P&L/Unrealized P&L Pct. columns, deliberately NOT ingested here --
# `build_sync_row()`'s `_fill_fields_for_row()` leaves buy/sell qty/value
# null for report_type='pnl' by design (a P&L-statement row is a
# symbol-level settled aggregate, not a per-trade fill; Quantity/Buy
# Value/Sell Value there are the realized LEG only, already folded into
# Realized P&L, and the Open-* / Unrealized-* columns describe a still-
# live position, which is the broker feed's job, not this table's).
_REQUIRED_PNL_COLUMNS = ("symbol", "realized_pnl")

# Confirmed live, 2026-11-26, against a real Funds Statement (ledger)
# CSV export, downloaded with NO category selected on
# console.zerodha.com/funds/statement (see parse_ledger_csv's own
# docstring for the full real header and why the category select is
# never touched).
_REQUIRED_LEDGER_COLUMNS = ("particulars", "posting_date", "debit", "credit", "net_balance")

# Own, independent cooloff file for the Console browser login path — see
# the module docstring's "Cooloff INDEPENDENCE" section and
# `KiteConsoleConnection._login_cooloff_path()`. Deliberately NOT
# `backend.brokers.connections._KITE_LOGIN_COOLOFF_PATH` (the live REST
# login path's own file for the same account) — a failed Console scrape
# login must never block the live trading account's own re-logins, and
# vice versa.
_CONSOLE_LOGIN_COOLOFF_PATH = "/tmp/ramboq_kite_console_login_cooloff.json"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConsoleLoginError(RuntimeError):
    """The Kite Console browser LOGIN sequence itself failed (any of
    goto_login / submit_credentials / submit_totp / verify_logged_in).
    Raised from `KiteConsoleConnection.init_kite_conn()`, which is the
    ONLY thing `_attempt_login_with_cooloff()` (inherited, unmodified)
    calls — so this (or any subclass) feeds the real cross-process login
    cooloff, persisted to this class's OWN `_CONSOLE_LOGIN_COOLOFF_PATH`
    file (via the overridden `_login_cooloff_path()`), never the live
    REST path's `_KITE_LOGIN_COOLOFF_PATH`. See the module docstring's
    "Cooloff INDEPENDENCE" section.
    """


class ConsoleUnexpectedChallengeError(ConsoleLoginError):
    """The login flow presented something other than the expected
    password+TOTP steps — a CAPTCHA, an SMS/email OTP challenge, or
    anything else unrecognised. NEVER bypassed. Treated as a login
    failure like any other `ConsoleLoginError` (so it arms the cooloff,
    which is desirable — don't keep hammering a challenge we can't
    solve), and alerted on with a distinct, searchable message."""


class ConsoleStructuralError(RuntimeError):
    """A POST-login scraping step failed its own postcondition check
    (navigate_to_reports / select_segment / set_date_range /
    trigger_download). Deliberately NOT a `ConsoleLoginError` subclass:
    these failures happen entirely OUTSIDE
    `init_kite_conn`/`_attempt_login_with_cooloff`, so they must never
    touch the shared login cooloff — a broken report-page selector says
    nothing about whether this account's login is being rate-limited."""


class ReportValidationError(RuntimeError):
    """A downloaded report failed content validation — schema drift
    (missing/extra columns), empty when it shouldn't be, corrupt, or a
    row falls outside the requested date range. Never silently ingested."""


class StaleReportContentError(ConsoleStructuralError):
    """A downloaded report's own content is byte-identical (per
    `_pnl_content_signature()`) to a DIFFERENT, already-accepted
    report for a different requested date range — Kite Console's
    date-range-picker race (see `_CHUNK_SCRAPE_MAX_ATTEMPTS`'s own
    comment and `_make_pnl_dup_check()` below): the on-screen "fetched"
    label updates correctly to the newly requested range, but the
    server occasionally still serves the PREVIOUS query's export.
    `validate_pnl_rows()` cannot catch this on its own — a P&L
    statement row carries no per-row date to range-check against,
    unlike Tradebook/Ledger's `#date_fetched_label`-backed defense (see
    `KiteConsoleReportsPage._verify_fetched_date_range()`'s own
    docstring for why that landmark is absent from the P&L page's DOM).
    Deliberately a `ConsoleStructuralError` subclass, not a
    `ReportValidationError` — this is caught by `_scrape_one_chunk()`'s
    existing bounded retry loop (a fresh retry resolves the race in
    practice, same as the date-range-mismatch case), never by the
    ingestion-time validation path."""


# ---------------------------------------------------------------------------
# Diagnostics — screenshot + HTML dump on any verification failure
# ---------------------------------------------------------------------------

def capture_diagnostics(page, masked_account: str, step: str) -> dict:
    """Best-effort screenshot + HTML dump. Never raises — a diagnostics
    failure must not mask or replace the real error being reported.

    The directory is created 0700: the dumped HTML is a raw DOM document,
    not a known-shape log string, so it can't be run through
    `mask_account_in_text()` the way a log line can. Restrict access
    instead of trying to scrub an arbitrary HTML document."""
    try:
        _DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(_DIAGNOSTICS_DIR, 0o700)
    except Exception as e:
        logger.warning("[KITE-CONSOLE-SYNC] diagnostics dir setup failed: %s", e)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = _DIAGNOSTICS_DIR / f"{ts}_{masked_account}_{step}"
    result: dict = {"screenshot": None, "html": None}
    try:
        png_path = base.with_suffix(".png")
        page.screenshot(path=str(png_path), full_page=True)
        result["screenshot"] = str(png_path)
    except Exception as e:
        logger.warning("[KITE-CONSOLE-SYNC] screenshot capture failed: %s", e)
    try:
        html_path = base.with_suffix(".html")
        html_path.write_text(page.content(), encoding="utf-8")
        result["html"] = str(html_path)
    except Exception as e:
        logger.warning("[KITE-CONSOLE-SYNC] html capture failed: %s", e)
    return result


# ---------------------------------------------------------------------------
# Playwright page abstraction — SELECTORS ARE PLACEHOLDERS, SEE DOCSTRING
# ---------------------------------------------------------------------------

class KiteConsoleReportsPage:
    """Thin wrapper around a Playwright SYNC `Page` already pointed at
    Kite's web Console. Every selector below is a best-effort guess at
    Kite Console's real DOM — a left-nav "Reports" link, a tab/dropdown
    for Tradebook vs P&L, a from/to date-range picker, a segment selector,
    and a Download button, which is the general shape of a brokerage
    reports page — but Kite Console's ACTUAL markup has not been verified
    against a live session (no live credentials were available while
    building this). CSS class names are the most likely thing to drift
    across a Console redesign, so every locator here deliberately prefers
    Playwright's resilient `get_by_role` / `get_by_text` style over a raw
    CSS class selector, per the operator brief's own guidance.

    VERIFY BEFORE THE FIRST REAL RUN (open a real session, inspect each
    locator with Playwright Inspector — `PWDEBUG=1`):
      - goto_login / submit_credentials: the login form's actual input
        selectors and submit control.
      - submit_totp: the TOTP input's actual selector, and whether a
        CAPTCHA/challenge (if one appears) is detectable via
        `_looks_like_captcha_or_unexpected_challenge()`'s text heuristic
        or needs a more specific selector.
      - verify_logged_in: a stable element that's ONLY present once truly
        authenticated on the Reports/dashboard surface.
      - navigate_to_reports / select_segment: the real tab/dropdown labels
        (`_SEGMENT_CONSOLE_VALUE` at module level) and whether "Tradebook"
        vs "P&L" are tabs, a dropdown, or separate URLs.
      - set_date_range: the real date-input selectors and their expected
        value format (this assumes plain ISO `YYYY-MM-DD` text inputs;
        Console may use a different picker widget entirely).
      - trigger_download: the real Download control's selector.

    Nothing OUTSIDE this class should need to change once these are
    corrected — the verify-every-step / never-retry-a-broken-selector /
    screenshot-and-alert structure in `sync_account()` is independent of
    the exact selector strings.
    """

    LOGIN_URL = "https://kite.zerodha.com/"

    def __init__(self, page):
        self.page = page

    # --- login -----------------------------------------------------------

    def goto_login(self) -> None:
        self.page.goto(self.LOGIN_URL, wait_until="domcontentloaded")
        try:
            # PLACEHOLDER — Kite's login userid input.
            self.page.wait_for_selector("input#userid", timeout=15000)
        except Exception as e:
            raise ConsoleLoginError(f"Kite login page never loaded: {e}") from e

    def submit_credentials(self, user_id: str, password: str) -> None:
        try:
            # PLACEHOLDER
            self.page.fill("input#userid", user_id)
            self.page.fill("input#password", password)
            self.page.click("button[type='submit']")
        except Exception as e:
            raise ConsoleLoginError(f"could not submit userid/password: {e}") from e

    def submit_totp(self, totp_code: str) -> None:
        """Waits for the TOTP step. If something OTHER than the expected
        TOTP input appears within the timeout — a CAPTCHA, an SMS/email
        OTP challenge, anything unrecognised — raises
        `ConsoleUnexpectedChallengeError` rather than guessing. Never
        bypassed, per the operator brief."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        try:
            # PLACEHOLDER — Kite's TOTP/2FA numeric input.
            self.page.wait_for_selector("input[type='number']", timeout=15000)
        except PlaywrightTimeoutError as e:
            if self._looks_like_captcha_or_unexpected_challenge():
                raise ConsoleUnexpectedChallengeError(
                    "Kite Console presented an unexpected challenge "
                    "(CAPTCHA or non-TOTP 2FA) instead of the expected "
                    "TOTP step"
                ) from e
            raise ConsoleLoginError(
                "TOTP input never appeared after credential submit"
            ) from e
        try:
            # PLACEHOLDER
            self.page.fill("input[type='number']", totp_code)
            self.page.keyboard.press("Enter")
        except Exception as e:
            raise ConsoleLoginError(f"could not submit TOTP: {e}") from e

    def _looks_like_captcha_or_unexpected_challenge(self) -> bool:
        try:
            body_text = (self.page.inner_text("body") or "").lower()
        except Exception:
            return False
        return any(
            tok in body_text
            for tok in ("captcha", "verify you are human", "unusual activity", "suspicious")
        )

    def verify_logged_in(self) -> None:
        """Postcondition check for the WHOLE login flow. Never assume a
        click/navigation succeeded just because no exception was thrown.

        2026-11-26 fix (first real run found this): login itself
        (userid/password/TOTP) genuinely succeeds and lands on
        `kite.zerodha.com/dashboard` -- confirmed live, real portfolio
        data rendered -- but that page has NO "Reports" link at all; its
        own top nav is "Dashboard / Orders / Holdings / Positions / IPO
        & Bids / Funds". Reports/Tradebook/P&L live on the SIBLING
        console.zerodha.com subdomain, which this same login session
        carries over to automatically (confirmed live: navigating there
        directly, with no separate login step, lands on Console's own
        authenticated UI). This check now does that navigation itself,
        then confirms via Console's OWN top-nav "Reports" link -- which
        genuinely exists there.

        2026-11-26 fix (second real run found this): the goto() above can
        itself raise `Error: net::ERR_ABORTED`, not a timeout -- a known
        Playwright/Chromium gotcha where the navigation we started is
        cancelled mid-flight by a SECOND, client-side-redirect navigation
        the page fires immediately (e.g. `/` -> `/dashboard`) before
        `domcontentloaded` can fire for the first one. A bare ERR_ABORTED
        is swallowed, but that alone is not sufficient -- see the third
        fix below.

        2026-11-26 fix (third real run found this): swallowing ERR_ABORTED
        alone let the RACE'S LOSING SIDE be ours -- kite.zerodha.com's own
        still-in-flight post-TOTP redirect to its own `/dashboard` can win
        over our goto("console.zerodha.com/"), landing the page back on
        kite's dashboard (confirmed live: Playwright's own timeout log
        showed `navigated to "https://kite.zerodha.com/dashboard"`, not
        console at all) -- which genuinely has no Reports link, so the
        link-wait below then timed out for a reason that has nothing to
        do with a real login failure. Fixed by checking the ACTUAL landed
        URL after the goto and retrying the navigation (bounded, 3
        attempts) until it genuinely lands on the console.zerodha.com
        host, before ever waiting for the Reports link -- the link-wait
        timeout is now reserved for a genuine authentication failure, not
        a navigation race that just needs one more attempt.

        2026-11-26 fix (fourth real run found this, via the real HTML
        dump in `.log/kite_console_diagnostics/`): landing on
        console.zerodha.com is NOT the same as being authenticated there
        -- the docstring's original "this same login session carries over
        to automatically" claim was wrong. The real page shows Console's
        LOGGED-OUT marketing landing page, with a real
        `<button class="btn-blue">Login with Kite</button>`. Confirmed via
        an earlier real research session's own diagnostics
        (`*_r2_after_submit.*`) that clicking through this button DOES
        reach an authenticated Reports/Tradebook page in the same browser
        context (kite.zerodha.com's session is reused by Kite Connect's
        own OAuth-style authorize flow, which auto-approves for Zerodha's
        own first-party apps with no extra consent screen) -- so this is
        an expected one-time bounce, not a failure. The button is clicked
        when present, with a short existence probe so an already-
        authenticated landing (no button) is a silent no-op, not an
        error."""
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        try:
            landed_on_console = False
            for _attempt in range(3):
                try:
                    self.page.goto(
                        "https://console.zerodha.com/", wait_until="domcontentloaded", timeout=20000,
                    )
                except PlaywrightError as e:
                    if "ERR_ABORTED" not in str(e):
                        raise
                if "console.zerodha.com" in (self.page.url or ""):
                    landed_on_console = True
                    break
                self.page.wait_for_timeout(1000)
            if not landed_on_console:
                raise ConsoleLoginError(
                    "Could not land on console.zerodha.com after 3 attempts "
                    f"(still on {self.page.url!r}) -- kite.zerodha.com's own "
                    "post-login redirect kept winning the navigation race"
                )
            login_with_kite = self.page.get_by_role("button", name="Login with Kite")
            try:
                login_with_kite.wait_for(state="visible", timeout=3000)
                present = True
            except PlaywrightTimeoutError:
                present = False
            if present:
                login_with_kite.click()
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=20000)
                except PlaywrightTimeoutError:
                    pass
            self.page.get_by_role("link", name="Reports", exact=True).wait_for(timeout=20000)
        except PlaywrightTimeoutError as e:
            if self._looks_like_captcha_or_unexpected_challenge():
                raise ConsoleUnexpectedChallengeError(
                    "Kite Console presented an unexpected challenge after TOTP submit"
                ) from e
            raise ConsoleLoginError(
                "Console did not land on an authenticated dashboard/Reports "
                "surface after login"
            ) from e

    # --- navigation / scraping (post-login; NEVER touches the cooloff) --

    # Real Console report URLs, confirmed live 2026-11-26 (session cookie
    # carries over from kite.zerodha.com with no separate Console login --
    # Zerodha's Kite and Console are two distinct web apps on sibling
    # subdomains, sharing one top-level-domain session). Console's own
    # 404 page is a client-side-routed SPA shell, so a wrong URL here
    # would NOT raise on navigation -- `verify_logged_in()`'s own
    # Console-nav check is what actually catches that case.
    _REPORT_URL = {
        "tradebook": "https://console.zerodha.com/reports/tradebook",
        "pnl": "https://console.zerodha.com/reports/pnl",
        # Confirmed live, 2026-11-26 -- "Funds Statement" / ledger, the
        # one Console report carrying margin-block/reversal and cash
        # events (debit/credit/running balance), neither Tradebook nor
        # the P&L statement carry anything like this.
        "ledger": "https://console.zerodha.com/funds/statement",
    }

    def navigate_to_reports(self, report_type: str) -> None:
        """2026-11-26 fix (fifth real run found this): after a long,
        single-browser-session backfill (many chunks across several
        segments/report-types), the Vue SPA sometimes takes far longer
        than 20s to mount its content after a fresh `goto()` -- confirmed
        live via diagnostics showing a genuinely BLANK page (the app
        shell/scripts present, no rendered body content at all) for
        commodities/pnl and the ledger page, late in a run. This is
        consistent with resource pressure building up in one long-lived
        browser instance, not a different page structure for those report
        types. Retried once (bounded) with a longer timeout before
        raising, rather than failing on the first transient slow render."""
        url = self._REPORT_URL.get(report_type)
        if not url:
            raise ConsoleStructuralError(f"no known Console report URL for {report_type!r}")
        last_exc: Exception | None = None
        for attempt, timeout_ms in enumerate((20000, 45000)):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # The segment <select> (and, on /reports/pnl, the P&L-view
                # <select> right next to it) is the one stable landmark that
                # confirms the real report-builder form loaded, rather than
                # Console's client-side 404 shell (same HTTP 200 either way).
                self.page.locator("select").first.wait_for(timeout=timeout_ms)
                last_exc = None
                break
            except Exception as e:
                last_exc = e
        if last_exc is not None:
            raise ConsoleStructuralError(
                f"could not navigate to the {report_type!r} report page ({url}): {last_exc}"
            ) from last_exc
        if report_type == "pnl":
            # Second <select> on /reports/pnl only -- Combined / Realised
            # P&L / Unrealised P&L. "realized" is the one whose per-symbol
            # "Realized P&L" column is this script's actual ingestion
            # target (see parse_pnl_statement_xlsx()'s own docstring for
            # why "combined"/"unrealized" are not what's wanted here).
            try:
                self.page.locator("select").nth(1).select_option(value="realized")
            except Exception as e:
                raise ConsoleStructuralError(
                    f"could not select the Realised P&L view on the pnl report page: {e}"
                ) from e

    def select_segment(self, segment: str) -> None:
        console_value = _SEGMENT_CONSOLE_VALUE.get(segment, segment)
        try:
            self.page.locator("select").first.select_option(value=console_value)
        except Exception as e:
            raise ConsoleStructuralError(
                f"could not select segment {console_value!r}: {e}"
            ) from e

    # Both report pages' date-range field is ONE combined litepicker-style
    # text input (not separate from/to inputs) -- confirmed live, real
    # attributes `name="date" placeholder="Select range" class="mx-input"`
    # (id differs per page: tradebook-date-range / pnl-date-range, so the
    # selector below deliberately matches on the shared placeholder, not
    # the page-specific id). Typing "YYYY-MM-DD ~ YYYY-MM-DD" directly
    # and pressing Enter is confirmed to register without needing to
    # drive the calendar popup.
    _DATE_RANGE_SELECTOR = "input[placeholder='Select range']"

    # The Funds Statement (ledger) page's date-range field is a DIFFERENT
    # element than Tradebook/P&L's -- confirmed live, 2026-11-26:
    # `name="date" placeholder="Date" id="ledger-date-range"`, same
    # litepicker-style combined-range input mechanics otherwise.
    _LEDGER_DATE_RANGE_SELECTOR = "input[name='date']"

    def _set_date_range_on(self, selector: str, start: date, end: date) -> None:
        """Shared fill-then-confirm logic for `set_date_range()` and
        `set_ledger_date_range()` — extracted so the Tradebook/P&L and
        ledger selectors can each fail with their own, clearer error
        without duplicating the readback check."""
        start_s, end_s = start.isoformat(), end.isoformat()
        expected = f"{start_s} ~ {end_s}"
        field = self.page.locator(selector)
        try:
            field.fill(expected)
            self.page.keyboard.press("Enter")
        except Exception as e:
            raise ConsoleStructuralError(f"could not set the date range input: {e}") from e
        try:
            actual = field.input_value()
        except Exception as e:
            raise ConsoleStructuralError(f"could not read back the date range input: {e}") from e
        if actual != expected:
            raise ConsoleStructuralError(
                f"date range did not register: requested {expected!r}, page shows {actual!r}"
            )

    def set_date_range(self, start: date, end: date) -> None:
        """Sets the combined date-range input, then re-reads it back to
        confirm the value actually registered — never assumes a fill()
        call took effect just because it didn't raise."""
        self._set_date_range_on(self._DATE_RANGE_SELECTOR, start, end)

    def set_ledger_date_range(self, start: date, end: date) -> None:
        """Same fill-then-confirm mechanics as `set_date_range()`, on the
        Funds Statement page's own differently-selectored date input."""
        self._set_date_range_on(self._LEDGER_DATE_RANGE_SELECTOR, start, end)

    # The segment/date-range form only BUILDS the report; the submit
    # button (confirmed live) is a `.btn-blue` button next to the
    # date-range field -- `type="submit"` on /reports/tradebook,
    # `type="button"` on /reports/pnl, so the selector deliberately
    # matches on the shared class only, not the type attribute.
    _SUBMIT_SELECTOR = "button.btn-blue"

    def _submit_report_form(self, *, timeout_ms: int) -> None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        try:
            self.page.click(self._SUBMIT_SELECTOR, timeout=timeout_ms)
        except PlaywrightTimeoutError as e:
            raise ConsoleStructuralError(
                f"could not find/click the report-builder submit button: {e}"
            ) from e

    # Confirmed live, 2026-11-26: once a report-builder query actually
    # renders (whether rows or an empty result), the page always shows
    # one of these two phrases -- "Showing page N (rows X - Y out of Z)"
    # for a populated report, "Report's empty / We couldn't find any
    # results for your query" for a genuinely empty one. True on
    # Tradebook AND P&L (confirmed via live screenshots of both). This
    # is the one landmark common to every report type, so it is the
    # PRIMARY "has the new query finished rendering" signal -- replacing
    # a blind fixed sleep, which was too short on a slow response and
    # the root cause of both the "download never fired" timeouts and the
    # "rows outside requested range" validation failures (a stale,
    # previously-rendered table's download link got clicked before the
    # new query's own result replaced it).
    _RESULTS_RENDERED_TEXT = re.compile(r"Showing page|Report's empty")

    # Tradebook/ledger ONLY (confirmed absent from /reports/pnl's DOM) --
    # a second, stronger verification: the exact date range the page
    # believes it just fetched, independent of whatever `_RESULTS_
    # RENDERED_TEXT` matched. Used to catch a genuinely stale render
    # that happens to satisfy the primary wait (e.g. the previous
    # chunk's own already-"Showing page"-rendered table, if this
    # landmark's text has not yet updated to the newly requested range).
    _DATE_FETCHED_LABEL_SELECTOR = "#date_fetched_label"

    def _wait_for_results_rendered(self, timeout_ms: int) -> None:
        self.page.get_by_text(self._RESULTS_RENDERED_TEXT).first.wait_for(timeout=timeout_ms)

    def _verify_fetched_date_range(self, start: date, end: date, timeout_ms: int) -> None:
        """Poll `#date_fetched_label` until its text contains BOTH
        requested ISO dates, or raise a clear, distinct error naming the
        real cause (stale results) rather than letting a mismatched
        download proceed silently. No-op when the label doesn't exist on
        this report type at all (the P&L page) -- `_wait_for_results_
        rendered()` is this method's only signal there."""
        label = self.page.locator(self._DATE_FETCHED_LABEL_SELECTOR)
        try:
            if label.count() == 0:
                return
        except Exception:
            return
        start_s, end_s = start.isoformat(), end.isoformat()
        deadline = _time_mod.monotonic() + (timeout_ms / 1000.0)
        last_text = ""
        while _time_mod.monotonic() < deadline:
            try:
                last_text = label.inner_text(timeout=1000)
            except Exception:
                last_text = ""
            if start_s in last_text and end_s in last_text:
                return
            self.page.wait_for_timeout(500)
        raise ConsoleStructuralError(
            f"results still show a stale date range after submit -- requested "
            f"{start_s}..{end_s}, page's own #date_fetched_label shows {last_text!r}"
        )

    def trigger_download(self, *, report_type: str, start: date | None = None,
                          end: date | None = None, masked_account: str = "",
                          timeout_ms: int = 45000) -> Path:
        """Submits the report-builder form (segment + date range must
        already be set), waits for the results to render, then triggers
        the real file download and waits for Playwright's own `download`
        event (`expect_download()` is built on `page.on('download', ...)`)
        — never polls a filesystem Downloads folder.

        The three report types use DIFFERENT download controls, confirmed
        live, 2026-11-26: Tradebook and the ledger/Funds Statement report
        each offer two separate "XLSX" / "CSV" text links (CSV is
        picked); the P&L report offers a single `div[role='button']`
        labeled "Download" whose tooltip reads "Download complete
        results as Excel" -- there is no CSV option for the P&L report
        at all, hence `parse_pnl_statement_xlsx()`.

        `masked_account` (2026-11-26 fix): Kite's own `download.
        suggested_filename` embeds the RAW, unmasked account code (e.g.
        `ledger-ZG0790.csv`, confirmed live) -- using it verbatim as the
        saved local filename would put an unmasked account code on disk,
        violating this codebase's mask-everywhere invariant (see
        CLAUDE.md's account-masking campaign). The destination filename
        is built from `masked_account` + the real suggested name's own
        extension instead; the raw suggested name is never used for the
        path. Optional (defaults to "") only so existing direct callers/
        tests that don't pass it keep working -- `_console_login_and_
        scrape_sync()` always passes the real masked account.

        `start`/`end` (2026-11-26 fix, sixth real run found this): a
        fixed 2s sleep for "results table render" was not long enough on
        a slow response, so the download control sometimes got clicked
        against the PREVIOUS chunk's still-rendered table -- surfacing
        either as a download timeout (no new control existed yet where
        the old one used to be) or, worse, a silent download of the
        wrong date range (caught only after the fact by this module's
        own post-parse validation). Replaced with `_wait_for_results_
        rendered()` (a real landmark wait) plus, when `start`/`end` are
        given, `_verify_fetched_date_range()` -- optional purely so
        existing direct callers/tests that don't pass them keep working;
        every real caller in `_console_login_and_scrape_sync()` passes
        both.
        """
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        self._submit_report_form(timeout_ms=timeout_ms)
        self._wait_for_results_rendered(timeout_ms=timeout_ms)
        if start is not None and end is not None:
            self._verify_fetched_date_range(start, end, timeout_ms=timeout_ms)

        try:
            if report_type in ("tradebook", "ledger"):
                with self.page.expect_download(timeout=timeout_ms) as download_info:
                    self.page.get_by_role("link", name="CSV").click(timeout=timeout_ms)
            elif report_type == "pnl":
                with self.page.expect_download(timeout=timeout_ms) as download_info:
                    self.page.locator("div.table-download").click(timeout=timeout_ms)
            else:
                raise ConsoleStructuralError(f"unknown report_type for download: {report_type!r}")
            download = download_info.value
        except PlaywrightTimeoutError as e:
            raise ConsoleStructuralError(
                "download never fired within the timeout after clicking the "
                "report's download control"
            ) from e
        except ConsoleStructuralError:
            raise
        except Exception as e:
            raise ConsoleStructuralError(f"download failed: {e}") from e
        _DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        suffix = Path(download.suggested_filename or "").suffix or ".csv"
        safe_account = masked_account.replace("#", "X") or "unknown"
        dest = _DOWNLOADS_DIR / f"{report_type}_{safe_account}_{int(_time_mod.time())}{suffix}"
        download.save_as(str(dest))
        return dest


# ---------------------------------------------------------------------------
# KiteConnection subclass — browser login hooked into the real cooloff
# ---------------------------------------------------------------------------

class KiteConsoleConnection:
    """Mixin providing the browser-login override, applied onto the real
    `KiteConnection` at construction time via `build_console_connection()`
    below (not a plain subclass declared at import time) so this module
    imports cleanly even in an environment with no `backend.brokers`
    runtime dependencies installed. See `build_console_connection()`."""

    def _login_cooloff_path(self) -> str:
        """Override of `KiteConnection._login_cooloff_path()` — points at
        this script's OWN cooloff file, `_CONSOLE_LOGIN_COOLOFF_PATH`,
        never the live REST path's `backend.brokers.connections.
        _KITE_LOGIN_COOLOFF_PATH`. This is the one change that makes a
        failed Console browser login and a failed live REST login for
        the same account cool off independently — see the module
        docstring's "Cooloff INDEPENDENCE" section."""
        return _CONSOLE_LOGIN_COOLOFF_PATH

    def init_kite_conn(self, test_conn: bool = False) -> None:  # noqa: D401
        """Override of `KiteConnection.init_kite_conn` — performs a
        Playwright browser login to Kite Console's WEB UI (password +
        TOTP) instead of the sanctioned KiteConnect REST login the base
        class performs. Called ONLY by `_attempt_login_with_cooloff()`
        (inherited, unmodified) with `test_conn=True`; any exception
        raised here is caught by that method and feeds this class's OWN
        cooloff file (via the `_login_cooloff_path()` override above),
        never the live REST path's — see the module docstring's "Cooloff
        INDEPENDENCE" section.
        """
        if not test_conn:
            return
        from backend.shared.helpers.utils import mask_account

        from playwright.sync_api import sync_playwright

        masked = mask_account(self.account)
        pw = sync_playwright().start()
        browser = None
        try:
            # --disable-ipv6: the prod/dev server's IPv6 route to
            # console.zerodha.com is unreachable (confirmed via `curl -6`
            # timing out while `curl -4` succeeds — same class of egress
            # quirk already documented for httpx/urllib3 callers in
            # CLAUDE.md's "Network egress quirks" entry, but Chromium's own
            # network stack doesn't go through that Python-level IPv4-
            # forcing patch at all). Without this flag Chromium's own
            # happy-eyeballs resolution raced and lost, surfacing as a bare
            # `net::ERR_ABORTED` on the very first `page.goto()` — not a
            # CAPTCHA or login failure, just a dead IPv6 route.
            browser = pw.chromium.launch(headless=True, args=["--disable-ipv6"])
            context = browser.new_context()
            page = context.new_page()
            console = KiteConsoleReportsPage(page)
            try:
                console.goto_login()
                console.submit_credentials(self.account, self._password)
                from backend.shared.helpers.utils import generate_totp
                totp_code = generate_totp(self.totp_token)
                console.submit_totp(totp_code)
                console.verify_logged_in()
            except Exception as e:
                capture_diagnostics(page, masked, "login_failed")
                raise ConsoleLoginError(
                    f"Kite Console login failed for {masked}: {e}"
                ) from e
            self._console_page = page
            self._console_context = context
            self._console_browser = browser
            self._console_playwright = pw
        except Exception:
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass
            raise


def build_console_connection(account: str, secrets_blob: dict):
    """Construct a `KiteConnection`-derived instance with the browser-login
    override mixed in. Deferred import of `backend.brokers.connections`
    (which pulls in the Kite SDK) keeps this module importable for the
    pure parsing/validation/trigger-logic tests with zero broker-layer
    dependencies loaded."""
    from backend.brokers.connections import KiteConnection

    class _Conn(KiteConsoleConnection, KiteConnection):
        pass

    conn = _Conn(account, secrets_blob)
    conn._console_page = None
    conn._console_context = None
    conn._console_browser = None
    conn._console_playwright = None
    return conn


def shutdown_console_browser(conn) -> None:
    """Closes the Playwright browser/context/driver stored on `conn` by
    `KiteConsoleConnection.init_kite_conn`, if any. Never raises."""
    for attr in ("_console_context", "_console_browser"):
        obj = getattr(conn, attr, None)
        if obj is not None:
            try:
                obj.close()
            except Exception:
                pass
    pw = getattr(conn, "_console_playwright", None)
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Credential loading — from broker_accounts (DB), never secrets.yaml
# ---------------------------------------------------------------------------

async def load_active_kite_accounts(account_filter: "list[str] | None" = None) -> list:
    """Active Kite/zerodha_kite `BrokerAccount` rows from the shared DB —
    the authoritative source for an EXISTING account's credentials, per
    this project's own ZJ6294-incident CLAUDE.md entry. `secrets.yaml` is
    deliberately NOT read here except for the two non-account config
    values (`kite_login_url`/`kite_twofa_url`) `build_console_secrets_blob`
    pulls, which are not broker credentials."""
    from backend.api.database import shared_async_session
    from backend.api.models import BrokerAccount
    from sqlalchemy import select

    async with shared_async_session() as s:
        rows = (await s.execute(
            select(BrokerAccount).where(
                BrokerAccount.is_active.is_(True),
                BrokerAccount.broker_id.in_(("zerodha_kite", "kite")),
            )
        )).scalars().all()
    if account_filter:
        wanted = set(account_filter)
        rows = [r for r in rows if r.account in wanted]
    return list(rows)


def build_console_secrets_blob(account_code: str, row) -> dict:
    """Mirrors `Connections._build_kite_conn()`'s synthetic "secrets-shaped"
    dict exactly, so `KiteConnection.__init__` works unmodified. Decrypts
    the SAME encrypted columns `_build_kite_conn` reads."""
    from backend.shared.helpers.broker_creds import decrypt
    from backend.shared.helpers.utils import secrets as app_secrets

    creds_blob = {
        "api_key":    row.api_key,
        "api_secret": decrypt(row.api_secret_enc),
        "password":   decrypt(row.password_enc),
        "totp_token": decrypt(row.totp_token_enc),
        "source_ip":  row.source_ip,
    }
    return {
        "kite_accounts":  {account_code: creds_blob},
        "kite_login_url": app_secrets.get("kite_login_url"),
        "kite_twofa_url": app_secrets.get("kite_twofa_url"),
    }


# ---------------------------------------------------------------------------
# Alert delivery — one real flush+dispatch cycle of the production pipeline
# ---------------------------------------------------------------------------

async def _flush_and_dispatch_once(wait_s: float = 5.0) -> None:
    """Runs exactly one real flush+dispatch cycle of the production
    log -> alert pipeline (`log_store.flush_once()` /
    `event_agents.dispatch_rows()` — the SAME functions
    `log_store.run_forever()` calls on its own timer inside the long-lived
    `ramboq_api` process). See the module docstring's "Alert delivery"
    section for why this is necessary at all for a standalone script.
    No alert-sending logic is duplicated here."""
    from backend.shared.helpers import log_store
    from backend.api.algo.event_agents import dispatch_rows

    deadline = _time_mod.monotonic() + wait_s
    while not log_store.HANDLER.pending() and _time_mod.monotonic() < deadline:
        await asyncio.sleep(0.05)
    try:
        await log_store.flush_once(log_store.HANDLER, dispatch=dispatch_rows)
    except Exception as e:
        sys.stderr.write(f"kite_console_pnl_sync: alert flush failed: {e}\n")


async def alert_critical(title: str, masked_account: str, *, detail: str = "",
                          diagnostics: "dict | None" = None,
                          action_required: bool = True,
                          source_name: str = "Kite Console P&L sync",
                          alert_event: str = "kite_console_sync_failed") -> None:
    """Logs one CRITICAL, tagged record (the SSOT alert-authoring
    convention this codebase uses everywhere — see CLAUDE.md's "Alert
    identity SSOT" / "Alert-agent audit" entries) and then runs the real
    flush+dispatch pipeline once so it's actually delivered before this
    script exits. No dedicated seeded agent is needed — a CRITICAL record
    tagged "error" falls through to the existing `ERROR_AGENT` catch-all.

    `source_name`/`alert_event` (added 2026-11-27, both default to the
    pre-existing literal values so every call in THIS script is
    byte-identical) — `scripts/dhan_pnl_sync.py` reuses this exact
    function (never a copy) by passing its own distinct values, so an
    operator reading the alert can tell at a glance which backfill
    script failed, and the two scripts' failures don't collapse onto
    one indistinguishable `alert_event` for routing/dedup purposes.
    `logger` itself stays bound to this module's own name
    (`scripts.kite_console_pnl_sync`) even when called from the Dhan
    script — the alert's own rendered `source_name` is what reaches the
    operator, not the raw Python logger name, so this is cosmetic only
    (visible in a raw log/ `log_events` row, not in the delivered
    alert text)."""
    extra = {
        "source_type": "process",
        "source_name": source_name,
        "action_required": action_required,
        "tags": ["alert"],
        "alert_event": alert_event,
    }
    if diagnostics:
        extra["diagnostics"] = diagnostics
    logger.critical(
        "%s (%s): %s", title, masked_account, detail or "no further detail",
        extra=extra,
    )
    await _flush_and_dispatch_once()


# ---------------------------------------------------------------------------
# Report parsing / validation (pure, no I/O)
# ---------------------------------------------------------------------------

def _normalize_header(h: str) -> str:
    return (h or "").strip().lower().replace(" ", "_").replace("-", "_")


def _parse_report_csv(raw: bytes, required_columns: "tuple[str, ...]",
                       *, report_label: str = "report") -> "list[dict]":
    """CSV-parsing core, used by `parse_tradebook_csv` only — normalizes
    headers (lowercased, spaces/hyphens -> underscores) and enforces a
    required-columns set. The P&L-statement report is XLSX, not CSV (see
    `parse_pnl_statement_xlsx`'s own docstring for why), so this is no
    longer shared between the two report types; kept as its own function
    purely to keep `parse_tradebook_csv` itself small."""
    try:
        text_ = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as e:
        raise ReportValidationError(f"{report_label} file is not valid UTF-8 text: {e}") from e
    reader = csv.DictReader(io.StringIO(text_))
    if not reader.fieldnames:
        raise ReportValidationError(f"{report_label} CSV has no header row (empty or corrupt file)")
    normalized_fields = {
        _normalize_header(f): f for f in reader.fieldnames if f and f.strip()
    }
    missing = [c for c in required_columns if c not in normalized_fields]
    if missing:
        raise ReportValidationError(
            f"{report_label} CSV missing required column(s) {missing}; "
            f"found columns: {sorted(normalized_fields)}"
        )
    rows: "list[dict]" = []
    for raw_row in reader:
        rows.append({norm: raw_row.get(orig) for norm, orig in normalized_fields.items()})
    return rows


def parse_tradebook_csv(raw: bytes) -> "list[dict]":
    """Parses a Kite Console Tradebook CSV export into a list of
    normalized-header dicts. Column names are normalized (lowercased,
    spaces/hyphens -> underscores), though the real export's own header
    is already lowercase-with-underscores (confirmed live, 2026-11-26:
    `symbol,isin,trade_date,exchange,segment,series,trade_type,auction,
    quantity,price,trade_id,order_id,order_execution_time,expiry_date`),
    so normalization is a no-op in practice — kept for resilience against
    a future cosmetic header-casing change on Kite's side, not because
    the real header needs it today. The normalization is intended to be
    forgiving of minor casing/spacing differences, not of a genuinely
    different column set (that's `ReportValidationError`'s job)."""
    return _parse_report_csv(raw, _REQUIRED_TRADEBOOK_COLUMNS, report_label="tradebook")


def parse_pnl_statement_xlsx(raw: bytes) -> "list[dict]":
    """Parses a Kite Console P&L-statement XLSX export into a list of
    `{"symbol": ..., "realized_pnl": ...}` dicts.

    Confirmed live, 2026-11-26: unlike Tradebook (CSV), the P&L report's
    own "Download" control offers ONLY Excel — its tooltip literally
    reads "Download complete results as Excel", and the downloaded
    file's magic bytes are a ZIP container (XLSX), never CSV. Hence
    this is a genuinely different parser, not `_parse_report_csv`.

    Real workbook shape, confirmed against a live export (segment=F&O,
    view=Realised P&L): sheet 0 is named after the segment (e.g. "F&O"),
    sheet 1 is "Other Debits and Credits" (charges-by-posting-date, not
    ingested here — `_REQUIRED_PNL_COLUMNS` only asks for `symbol`/
    `realized_pnl`). Sheet 0 leads with a Client-ID line and a Summary
    block (Charges / Other Credit & Debit / Realized P&L / Unrealized
    P&L totals) BEFORE the real per-symbol table, so the header row is
    located dynamically (by finding "Symbol" + "Realized P&L" as two
    cell values in the SAME row), never assumed to sit at a fixed row
    number — the summary block's own row count can vary (segments differ
    in which charge line-items they carry).

    The "view" selector on the Console page (Combined / Realised P&L /
    Unrealised P&L, set in `navigate_to_reports()`) is what makes this
    sheet carry a literal "Realized P&L" column distinct from "Realized
    P&L Pct." — selecting "Combined" instead would still have a
    "Realized P&L" column (confirmed: it is the per-symbol settled
    figure regardless of view), but would also mix in Unrealised P&L
    columns for still-open positions, which this script has no use for
    (open positions are the live broker feed's job, not this ground-
    truth table's)."""
    import io as _io

    from openpyxl import load_workbook

    # read_only=False (confirmed necessary, not just the default) --
    # the real Kite Console export's own <dimension ref="A1"> tag is
    # stale/wrong (declares only cell A1, despite having real data
    # through row 43+). openpyxl's read_only mode trusts that hint to
    # decide how much of the sheet to stream, so read_only=True
    # silently truncated every row past A1 against a real file,
    # discovered only by testing against the genuine downloaded export,
    # not against a self-built (and therefore correctly-dimensioned)
    # fixture -- confirmed live, 2026-11-26.
    try:
        wb = load_workbook(_io.BytesIO(raw), data_only=True, read_only=False)
    except Exception as e:
        raise ReportValidationError(f"pnl statement file is not a valid XLSX: {e}") from e
    if not wb.sheetnames:
        raise ReportValidationError("pnl statement XLSX has no sheets")
    ws = wb[wb.sheetnames[0]]
    all_rows = list(ws.iter_rows(values_only=True))

    header_row_idx, symbol_col, pnl_col = _locate_pnl_header_row(all_rows)
    return _extract_pnl_symbol_rows(all_rows, header_row_idx, symbol_col, pnl_col)


def _locate_pnl_header_row(all_rows: "list[tuple]") -> "tuple[int, int, int]":
    """Finds the per-symbol table's header row (the one row carrying
    both "Symbol" and "Realized P&L" as cell values) and the column
    indices of each — extracted out of `parse_pnl_statement_xlsx()` to
    keep that function under the complexity gate."""
    for i, row in enumerate(all_rows):
        values = [str(v).strip() if v is not None else "" for v in row]
        if "Symbol" in values and "Realized P&L" in values:
            header_cols = {j: v for j, v in enumerate(values) if v}
            symbol_col = next(j for j, v in header_cols.items() if v == "Symbol")
            pnl_col = next(j for j, v in header_cols.items() if v == "Realized P&L")
            return i, symbol_col, pnl_col
    raise ReportValidationError(
        "pnl statement XLSX: could not locate the per-symbol table's "
        "header row (no row carries both 'Symbol' and 'Realized P&L')"
    )


def _extract_pnl_symbol_rows(all_rows: "list[tuple]", header_row_idx: int,
                              symbol_col: int, pnl_col: int) -> "list[dict]":
    """Builds `{"symbol", "realized_pnl"}` dicts from every data row
    following the header row, stopping at the first blank/missing
    Symbol cell (the real export's own end-of-table sentinel — see
    `parse_pnl_statement_xlsx()`'s own docstring). Extracted out of
    that function to keep it under the complexity gate."""
    rows: "list[dict]" = []
    for row in all_rows[header_row_idx + 1:]:
        if symbol_col >= len(row):
            break
        symbol = row[symbol_col]
        if symbol is None or str(symbol).strip() == "":
            break  # blank row -- end of the per-symbol table, next section follows
        realized_pnl = row[pnl_col] if pnl_col < len(row) else None
        rows.append({"symbol": str(symbol).strip(), "realized_pnl": realized_pnl})
    return rows


def parse_ledger_csv(raw: bytes) -> "list[dict]":
    """Parses a Kite Console Funds Statement (ledger) CSV export.

    Confirmed live, 2026-11-26, downloaded from console.zerodha.com/
    funds/statement with the Category select left UNTOUCHED (its
    options are EQ/MTF/COM -- no F&O entry at all, unlike Tradebook/P&L's
    EQ/FO/COM) and only the date range set. Real header:
    `particulars,posting_date,cost_center,voucher_type,debit,credit,
    net_balance`. Leaving Category unselected shows the CONSOLIDATED
    ledger across segments -- confirmed from the real downloaded CSV
    itself, which carries both "NSE-EQ - Z" and "MCX-F&O - Z" cost
    centers in the same file with no filter applied. This is the
    correct, complete view: Zerodha's margin pool is shared across
    segments for one account, so a per-segment ledger would be an
    artificial split this script has no need to make.

    Two sentinel rows bookend every real export -- `Opening Balance`
    and `Closing Balance` -- both carrying a `net_balance` but no
    `posting_date`/`cost_center`/`voucher_type`/`debit`/`credit`
    (confirmed live: `Opening Balance,,,,,,1861672.145694`). These are
    NOT real transactions -- they describe the pull's own requested
    range, not a dated event, and their own `net_balance` value shifts
    depending on which range was requested. `validate_ledger_rows()`/
    `build_ledger_sync_row()` skip any row with no parseable
    `posting_date`, which filters both sentinels out without a special
    case keyed on the literal text "Opening Balance"/"Closing Balance".

    Unlike Tradebook, the real export's CSV amounts are already plain
    decimals with no thousands separators (e.g. `27579.231718`) -- the
    UI's comma-grouped display (e.g. "27,579.23") is a rendering-only
    artifact, never present in the downloaded file."""
    return _parse_report_csv(raw, _REQUIRED_LEDGER_COLUMNS, report_label="ledger")


def _parse_date_cell(value) -> "date | None":
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float_cell(value) -> "float | None":
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def validate_tradebook_rows(rows: "list[dict]", *, segment: str, start: date, end: date,
                             allow_empty: bool = True) -> None:
    """Raises `ReportValidationError` on a malformed/implausible report.

    `allow_empty=True` by default: a real no-trade day/segment for this
    account produces a genuinely empty, otherwise well-formed tradebook,
    and that must NOT be treated as a failure — see the module docstring's
    report-type note on why an empty-but-valid result is not an error."""
    if not rows:
        if allow_empty:
            return
        raise ReportValidationError(
            f"{segment}: tradebook has zero rows for {start}..{end}"
        )
    bad: "list[object]" = []
    for r in rows:
        d = _parse_date_cell(r.get("trade_date"))
        if d is None or d < start or d > end:
            bad.append(r.get("trade_date"))
    if bad:
        raise ReportValidationError(
            f"{segment}: {len(bad)} row(s) have a trade_date outside the "
            f"requested range {start}..{end} (or unparsable): {bad[:5]}"
        )


def validate_pnl_rows(rows: "list[dict]", *, segment: str,
                       allow_empty: bool = True) -> None:
    """Raises `ReportValidationError` on a malformed/implausible P&L
    statement. Deliberately simpler than `validate_tradebook_rows` — a
    P&L-statement row is a symbol-level aggregate over the WHOLE
    requested range (not an individually-dated trade), so there is no
    per-row date to range-check against. `allow_empty=True` by default
    for the same reason as the Tradebook validator: a genuine no-trade
    segment/range for this account produces an empty, otherwise
    well-formed P&L statement, and that is not a failure."""
    if not rows:
        if allow_empty:
            return
        raise ReportValidationError(f"{segment}: pnl statement has zero rows")
    bad: "list[object]" = []
    for r in rows:
        symbol = (r.get("symbol") or "").strip()
        if not symbol:
            bad.append(r)
            continue
        if r.get("realized_pnl") not in (None, "") and _parse_float_cell(r.get("realized_pnl")) is None:
            bad.append(r)
    if bad:
        raise ReportValidationError(
            f"{segment}: {len(bad)} row(s) have a missing symbol or an "
            f"unparsable realized_pnl: {bad[:5]}"
        )


def validate_ledger_rows(rows: "list[dict]", *, start: date, end: date,
                          allow_empty: bool = True) -> None:
    """Raises `ReportValidationError` on a malformed/implausible ledger
    export. The two Opening/Closing Balance sentinel rows (no
    `posting_date` at all — see `parse_ledger_csv()`'s own docstring)
    are deliberately exempt from the date-range check, not flagged as
    bad: they describe the pull's own boundary, not a dated event, so
    they have no date to range-check in the first place. A row that
    HAS some non-empty `particulars` text but neither a sentinel label
    nor a parseable `posting_date` is genuinely malformed and IS
    flagged — this only exempts the two known, expected sentinel
    shapes, not every dateless row."""
    if not rows:
        if allow_empty:
            return
        raise ReportValidationError(f"ledger has zero rows for {start}..{end}")
    bad: "list[object]" = []
    for r in rows:
        particulars = (r.get("particulars") or "").strip()
        if not particulars:
            bad.append(r)
            continue
        if particulars in ("Opening Balance", "Closing Balance"):
            continue
        d = _parse_date_cell(r.get("posting_date"))
        if d is None or d < start or d > end:
            bad.append(r.get("posting_date"))
            continue
        if _parse_float_cell(r.get("debit")) is None or _parse_float_cell(r.get("credit")) is None:
            bad.append(r)
    if bad:
        raise ReportValidationError(
            f"ledger: {len(bad)} row(s) have a missing particulars, an "
            f"unparsable debit/credit, or a posting_date outside the "
            f"requested range {start}..{end}: {bad[:5]}"
        )


# ---------------------------------------------------------------------------
# Instrument classification (pure, no I/O) — reuses derivatives.py's
# existing F&O symbol parser, never a bespoke regex.
# ---------------------------------------------------------------------------

def classify_instrument(symbol: "str | None") -> "tuple[str, str]":
    """Returns `(instrument_type, symbol_root)` for a Console-reported
    tradingsymbol. `instrument_type` is 'future' | 'option' | 'equity'.

    Reuses `derivatives.parse_tradingsymbol()` — the SAME parser
    `build_sync_row()`'s own expiry derivation already uses — rather than
    a second, independent classification rule. Deliberately NEVER uses
    `symbol.endswith('CE'/'PE')`: that would misclassify an equity
    ticker like RELIANCE or BAJFINANCE as an option (the exact defect
    class this codebase has already fixed elsewhere — see
    derivativesMath.js's `isFOSymbol()`/`rollupByUnderlying` fix in
    CLAUDE.md). A symbol that fails to parse as F&O (equity, or an
    unparseable F&O-looking string) is classified 'equity', with its
    root defaulting to the symbol itself."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return "equity", ""
    try:
        from backend.api.algo.derivatives import parse_tradingsymbol
        parsed = parse_tradingsymbol(sym)
    except Exception:
        parsed = None
    if parsed:
        kind = parsed.get("kind")
        root = parsed.get("root") or sym
        if kind == "fut":
            return "future", root
        if kind == "opt":
            return "option", root
    return "equity", sym


# ---------------------------------------------------------------------------
# Ingestion (pure dedup-key/row-building + the async upsert)
# ---------------------------------------------------------------------------

def build_dedup_key(report_type: str, *, row: dict,
                     pull_start: "date | None" = None,
                     pull_end: "date | None" = None,
                     seq: "int | None" = None) -> str:
    """Stable idempotency key per report_type — see
    `KiteConsolePnlSync`'s own docstring (backend/api/models.py) for the
    full reasoning behind why tradebook and pnl need DIFFERENT key
    shapes.

    `seq` is ledger-only: a ledger entry carries no natural stable
    identifier (no trade_id/order_id, and `net_balance` is a RUNNING
    total that shifts on re-pull whenever Zerodha posts a back-dated
    voucher — see `ingest_ledger_rows()`'s own docstring for why that
    rules out a content-addressed key). `seq` is the row's 0-based
    position within THIS pull's own parsed list, which only needs to
    be unique WITHIN one ingest call — `ingest_ledger_rows()` always
    deletes the whole (account, chunk) bucket before inserting, so
    this key's stability ACROSS pulls is irrelevant, only its
    uniqueness WITHIN one."""
    if report_type == "tradebook":
        trade_id = (row.get("trade_id") or "").strip()
        order_id = (row.get("order_id") or "").strip()
        exchange = (row.get("exchange") or "").strip().upper()
        if trade_id:
            return f"{exchange}:{order_id}:{trade_id}"
        # No trade_id at all — degrade to a composite of every field that
        # identifies a single fill rather than silently dropping the row.
        return (
            f"{exchange}:{order_id}:{row.get('symbol')}:{row.get('trade_date')}:"
            f"{row.get('quantity')}:{row.get('price')}"
        )
    if report_type == "pnl":
        return f"{row.get('symbol')}:{row.get('expiry')}:{pull_start}:{pull_end}"
    if report_type == "ledger":
        return f"{pull_start}:{pull_end}:{seq}"
    raise ValueError(f"unknown report_type: {report_type!r}")


def _derive_expiry_from_symbol(symbol: "str | None") -> "date | None":
    """Pure — wraps `parse_tradingsymbol()` with the same best-effort
    try/except `build_sync_row()` always used, extracted so that
    function's own branching stays under the complexity gate."""
    try:
        from backend.api.algo.derivatives import parse_tradingsymbol
        parsed = parse_tradingsymbol(symbol or "")
    except Exception:
        parsed = None
    return parsed.get("expiry") if parsed else None


def _fill_fields_for_row(report_type: str, row: dict, qty: "float | None",
                          price: "float | None", trade_type: str) -> dict:
    """Returns the report-type-dependent fields
    (`buy_qty`/`sell_qty`/`buy_value`/`sell_value`/`realized_pnl`) for one
    row. Extracted out of `build_sync_row()` to keep that function's own
    branching under the complexity gate (`radon cc ... -s -n D`).

    buy/sell qty/value are Tradebook-only fields — a P&L-statement row is
    a symbol-level aggregate, not a per-trade BUY/SELL row, so these
    deliberately stay null for report_type='pnl' even if a future,
    verified export happens to carry similarly-named columns. See the
    module's `_REQUIRED_PNL_COLUMNS` comment."""
    if report_type == "pnl":
        return {
            "buy_qty": None, "sell_qty": None,
            "buy_value": None, "sell_value": None,
            "realized_pnl": _parse_float_cell(row.get("realized_pnl")),
        }
    has_qty_price = qty is not None and price is not None
    return {
        "buy_qty": qty if trade_type == "BUY" else None,
        "sell_qty": qty if trade_type == "SELL" else None,
        "buy_value": (qty * price) if (trade_type == "BUY" and has_qty_price) else None,
        "sell_value": (qty * price) if (trade_type == "SELL" and has_qty_price) else None,
        "realized_pnl": None,
    }


def build_sync_row(masked_account: str, segment: str, report_type: str, row: dict, *,
                    pull_start: "date | None" = None,
                    pull_end: "date | None" = None,
                    broker: str = "kite") -> dict:
    """Builds one `kite_console_pnl_sync` row payload from one parsed
    report row. Pure — no DB/session access.

    `broker` (added 2026-11-27, default `"kite"` so every pre-existing
    call site in THIS script is byte-identical) — reused by
    `scripts/dhan_pnl_sync.py` to write `broker='dhan'` rows into the
    same table/row-shape, after mapping its own broker-specific response
    fields into this function's Kite-CSV-shaped `row` dict first (see
    that script's own field-mapping docstring)."""
    symbol = row.get("symbol")
    trade_date = _parse_date_cell(row.get("trade_date"))
    expiry = _derive_expiry_from_symbol(symbol)
    instrument_type, symbol_root = classify_instrument(symbol)
    trade_id = (row.get("trade_id") or "").strip() or None
    order_id = (row.get("order_id") or "").strip() or None
    exchange = (row.get("exchange") or "").strip().upper() or None
    qty = _parse_float_cell(row.get("quantity"))
    price = _parse_float_cell(row.get("price"))
    trade_type = (row.get("trade_type") or "").strip().upper()
    fill_fields = _fill_fields_for_row(report_type, row, qty, price, trade_type)
    return {
        "broker": broker,
        "account": masked_account,
        "segment": segment,
        "report_type": report_type,
        "dedup_key": build_dedup_key(report_type, row=row, pull_start=pull_start, pull_end=pull_end),
        "symbol": symbol,
        "exchange": exchange,
        "instrument_type": instrument_type,
        "symbol_root": symbol_root,
        "expiry": expiry,
        "trade_date": trade_date,
        "trade_id": trade_id,
        "order_id": order_id,
        "pull_range_start": pull_start,
        "pull_range_end": pull_end,
        **fill_fields,
        "raw_row": row,
        "updated_at": datetime.now(timezone.utc),
    }


async def ingest_rows(session, masked_account: str, segment: str, report_type: str,
                       rows: "list[dict]", *, pull_start: "date | None" = None,
                       pull_end: "date | None" = None,
                       broker: str = "kite") -> int:
    """Idempotent upsert of parsed report rows into `kite_console_pnl_sync`
    — `ON CONFLICT (account, report_type, dedup_key) DO UPDATE`, mirroring
    this codebase's existing UPSERT-on-conflict convention (see
    `daily_snapshot.py`). Re-ingesting an overlapping pull never creates a
    duplicate row for the same real trade/aggregate.

    `broker` (added 2026-11-27, default `"kite"`) — see `build_sync_row()`'s
    own docstring. Deliberately NOT in `update_cols` below: `broker` can
    never legitimately change for an already-existing `(account,
    report_type, dedup_key)` row (the masked `account` form alone already
    disambiguates Kite from Dhan — different broker-letter mask prefix,
    see `KiteConsolePnlSync.broker`'s own model-column comment), so there
    is nothing to update it TO on a conflict."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from backend.api.models import KiteConsolePnlSync

    if not rows:
        return 0
    payloads = [
        build_sync_row(masked_account, segment, report_type, r,
                        pull_start=pull_start, pull_end=pull_end, broker=broker)
        for r in rows
    ]
    stmt = pg_insert(KiteConsolePnlSync).values(payloads)
    update_cols = {
        c: getattr(stmt.excluded, c) for c in (
            "symbol", "exchange", "instrument_type", "symbol_root",
            "expiry", "trade_date", "order_id",
            "buy_qty", "sell_qty", "buy_value", "sell_value",
            "realized_pnl", "raw_row", "updated_at",
        )
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["account", "report_type", "dedup_key"],
        set_=update_cols,
    )
    await session.execute(stmt)
    await session.commit()
    return len(payloads)


_LEDGER_SEGMENT = "all"  # ledger has no segment dimension — see build_ledger_sync_row()


def build_ledger_sync_row(masked_account: str, row: dict, *, seq: int,
                           pull_start: date, pull_end: date,
                           broker: str = "kite") -> "dict | None":
    """Builds one `kite_console_pnl_sync` row payload for a ledger entry.

    `broker` (added 2026-11-27, default `"kite"`) — see `build_sync_row()`'s
    own docstring; `scripts/dhan_pnl_sync.py` passes `broker="dhan"` after
    mapping its own ledger-entry fields into this function's Kite-ledger-
    CSV-shaped `row` dict (`particulars`/`posting_date`/`debit`/`credit`/
    `net_balance`).
    A SEPARATE function from `build_sync_row()`, not a third branch
    inside it — a ledger row shares almost none of the other two report
    types' shape (no symbol/exchange/trade_id/order_id/buy-sell-qty-
    value at all).

    Returns `None` for a sentinel row (no parseable `posting_date` —
    see `parse_ledger_csv()`'s own docstring) rather than a row with a
    null date, so the caller can filter it out before counting/
    inserting, matching `validate_ledger_rows()`'s own exemption.

    `particulars`/`cost_center`/`voucher_type`/`debit`/`credit`/
    `net_balance` live ONLY in `raw_row` (JSONB) — deliberately no new
    TYPED columns on `KiteConsolePnlSync` for this report type. Adding
    one would need an idempotent `ALTER TABLE ... ADD COLUMN` wired
    into `init_db()` before the first real ingest could succeed against
    the already-existing prod/dev table; `raw_row` already exists,
    already nullable-safe, and the model's own docstring explicitly
    sanctions this ("the typed columns... are the fields this app
    currently cares about... doesn't lose data even before this model
    is updated to add a typed column for it")."""
    posting_date = _parse_date_cell(row.get("posting_date"))
    if posting_date is None:
        return None
    return {
        "broker": broker,
        "account": masked_account,
        "segment": _LEDGER_SEGMENT,
        "report_type": "ledger",
        "dedup_key": build_dedup_key(
            "ledger", row=row, pull_start=pull_start, pull_end=pull_end, seq=seq,
        ),
        "symbol": None,
        "exchange": None,
        "instrument_type": None,
        "symbol_root": None,
        "expiry": None,
        "trade_date": posting_date,
        "trade_id": None,
        "order_id": None,
        "pull_range_start": pull_start,
        "pull_range_end": pull_end,
        "buy_qty": None, "sell_qty": None,
        "buy_value": None, "sell_value": None,
        "realized_pnl": None,
        "raw_row": row,
        "updated_at": datetime.now(timezone.utc),
    }


async def ingest_ledger_rows(session, masked_account: str, rows: "list[dict]", *,
                              pull_start: date, pull_end: date,
                              broker: str = "kite") -> int:
    """REPLACES (delete-then-insert, one transaction) every existing
    `report_type='ledger'` row for this account whose `trade_date`
    falls in `[pull_start, pull_end]`, then inserts the freshly parsed
    rows — NOT the per-row upsert-by-`dedup_key` convention `ingest_
    rows()` uses for tradebook/pnl.

    `broker` (added 2026-11-27, default `"kite"`) — scopes BOTH the
    DELETE and the fresh INSERT. The masked `account` form alone would
    already be sufficient to keep this call from ever touching the other
    broker's rows (see `KiteConsolePnlSync.broker`'s own model-column
    comment on why a Kite/Dhan masked account can never collide), so
    this is defense-in-depth, not the only thing preventing cross-broker
    data loss — but it costs nothing and makes the DELETE's own
    boundary explicit for any future caller/reader of this function.

    Why replace, not upsert: a ledger entry's own `net_balance` is a
    RUNNING total. Zerodha posts genuinely back-dated vouchers (the
    real sample this was built against has a charge for "October"
    posted on 2026-11-04) — re-pulling the SAME month after one lands
    shifts `net_balance` on every row dated AFTER the back-dated entry,
    even though their particulars/debit/credit are unchanged. A
    content-addressed dedup key (as tradebook uses) would then mint a
    NEW key for every shifted row on each re-pull, accumulating stale
    duplicates forever rather than replacing them. Deleting the whole
    chunk's own date range first and inserting the fresh pull is the
    only correct way to keep this report type idempotent across
    re-pulls. `pull_start`/`pull_end` are always calendar-month-aligned
    (via `chunk_by_calendar_month()`, same as every other report type),
    so this never deletes outside the exact window just pulled.

    Sentinel rows (Opening/Closing Balance — no `posting_date`) are
    silently dropped by `build_ledger_sync_row()` returning `None` for
    them; they were never meant to become rows in this table."""
    from sqlalchemy import delete as sa_delete
    from backend.api.models import KiteConsolePnlSync

    await session.execute(sa_delete(KiteConsolePnlSync).where(
        KiteConsolePnlSync.account == masked_account,
        KiteConsolePnlSync.broker == broker,
        KiteConsolePnlSync.report_type == "ledger",
        KiteConsolePnlSync.trade_date >= pull_start,
        KiteConsolePnlSync.trade_date <= pull_end,
    ))
    payloads = [
        p for p in (
            build_ledger_sync_row(masked_account, r, seq=i,
                                   pull_start=pull_start, pull_end=pull_end,
                                   broker=broker)
            for i, r in enumerate(rows)
        )
        if p is not None
    ]
    if payloads:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        await session.execute(pg_insert(KiteConsolePnlSync).values(payloads))
    await session.commit()
    return len(payloads)


# ---------------------------------------------------------------------------
# Trigger logic — "first market-open day on/after an MCX/NFO expiry"
# ---------------------------------------------------------------------------

async def collect_recent_fo_expiry_dates(session, *, lookback_days: int = _EXPIRY_LOOKBACK_DAYS,
                                          as_of: "date | None" = None) -> "set[tuple[str, date]]":
    """Returns {(exchange, expiry_date), ...} derived from recently held
    F&O positions in `daily_book`.

    Deliberately NOT built on the existing `is_mcx_expiry_today` /
    `is_nse_expiry_today` grammar-token metrics — those read
    `ctx.position_rows` and report "no expiry" the instant the position
    is gone, which is exactly the DAY AFTER expiry this sync needs to
    fire on. They answer "is there a position expiring today", this
    answers "did an expiry recently happen" — a different question."""
    from backend.api.models import DailyBook
    from backend.api.algo.derivatives import parse_tradingsymbol
    from sqlalchemy import select

    as_of = as_of or datetime.now(timezone.utc).date()
    cutoff = as_of - timedelta(days=lookback_days)
    rows = (await session.execute(
        select(DailyBook.exchange, DailyBook.symbol).where(
            DailyBook.kind == "positions",
            DailyBook.date >= cutoff,
            DailyBook.exchange.in_(("NFO", "BFO", "MCX", "CDS")),
        ).distinct()
    )).all()
    out: "set[tuple[str, date]]" = set()
    for exchange, symbol in rows:
        parsed = parse_tradingsymbol(symbol or "")
        if not parsed:
            continue
        expiry = parsed.get("expiry")
        if expiry:
            out.add(((exchange or "").upper(), expiry))
    return out


async def compute_trigger_map(
    expiry_pairs: "set[tuple[str, date]]",
) -> "dict[tuple[str, date], date]":
    """Maps each (exchange, expiry_date) pair to the first genuine
    trading day strictly after that expiry, via the existing,
    holiday-aware `next_market_open_ist()` — never a hand-rolled weekday
    walk. Exposed per-pair (not just as a flat date set) so
    `resolve_sync_plan()` can tell WHICH specific expiry(ies) are
    genuinely due today, not merely "some trigger is due" — the flat
    set `compute_trigger_dates()` returns loses the (exchange, expiry)
    association that distinction needs."""
    from backend.api.algo.expiry_freeze import next_market_open_ist

    out: "dict[tuple[str, date], date]" = {}
    for exchange, expiry in expiry_pairs:
        holiday_exchange = _HOLIDAY_EXCHANGE.get(exchange, "NSE")
        dt = await next_market_open_ist(expiry, exchange=holiday_exchange)
        out[(exchange, expiry)] = dt.date()
    return out


async def compute_trigger_dates(expiry_pairs: "set[tuple[str, date]]") -> "set[date]":
    """Thin wrapper over `compute_trigger_map()` — kept for the overall
    "is anything due today at all" gate in `main()`/`should_sync_today()`,
    which only needs the flat trigger-date set, never the per-pair
    mapping. Calls the SAME `next_market_open_ist()` loop, not a second
    one."""
    return set((await compute_trigger_map(expiry_pairs)).values())


def should_sync_today(today_ist: date, trigger_dates: "set[date]",
                       last_synced_through: "date | None") -> bool:
    """Pure, no I/O. True when at least one trigger date is <= today AND
    strictly after the last-synced marker — so a missed run (the script
    didn't fire exactly on the trigger day) still catches up on the next
    run instead of permanently skipping that expiry's sync."""
    floor = last_synced_through or date.min
    return any(floor < d <= today_ist for d in trigger_dates)


async def load_last_synced_through() -> "date | None":
    from backend.shared.helpers.settings import get_string
    raw = get_string(_SYNC_MARKER_KEY, "")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


async def mark_synced_through(d: date) -> None:
    from backend.shared.helpers.settings import set_string
    await set_string(_SYNC_MARKER_KEY, d.isoformat(), category="task_markers")


# ---------------------------------------------------------------------------
# Calendar-month chunking — Kite Console's Tradebook/P&L-statement CSV
# export does not reliably support a requested date range wider than
# ~1 month (direct operator testimony, 2026-11-26 — discovered before
# any real data existed in `kite_console_pnl_sync`: confirmed 0 rows in
# prod at the time, so this is a design correction, not a migration).
# `chunk_by_calendar_month()` is the ONE place a wide range is split
# into a fixed set of calendar-month-ALIGNED, non-overlapping pieces —
# applied uniformly to BOTH report types (tradebook and pnl-statement)
# in `sync_account()` below, never a per-report-type special case.
# Over-chunking (more, narrower requests) is always safe; under-
# chunking risks a hard failure against the real Console site.
# ---------------------------------------------------------------------------

def _month_end(d: date) -> date:
    """Pure — the last calendar day of `d`'s own month."""
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


def chunk_by_calendar_month(start: date, end: date) -> "list[tuple[date, date]]":
    """Splits [start, end] (inclusive) into consecutive calendar-month-
    aligned sub-ranges. Every chunk is a full calendar month EXCEPT the
    first (clipped to `start` when `start` isn't the 1st) and the last
    (clipped to `end` when `end` isn't a month-end). Pure, no I/O.

    Example: (2026-04-01, 2026-11-26) ->
        [(04-01,04-30), (05-01,05-31), (06-01,06-30), (07-01,07-31),
         (08-01,08-31), (09-01,09-30), (10-01,10-31), (11-01,11-26)]

    Returns `[]` for an inverted range (`start > end`) rather than
    raising — callers (e.g. a `resolve_sync_plan()` edge case) should
    treat an empty chunk list as "nothing to pull", not an error."""
    if start > end:
        return []
    chunks: "list[tuple[date, date]]" = []
    cur_start = start
    while cur_start <= end:
        cur_end = min(_month_end(cur_start), end)
        chunks.append((cur_start, cur_end))
        cur_start = cur_end + timedelta(days=1)
    return chunks


# ---------------------------------------------------------------------------
# Backfill vs sync date-range plans — genuinely distinct code paths
# ---------------------------------------------------------------------------

def backfill_plan(today: date) -> "dict[str, tuple[date, date]]":
    """One-time backfill — equity + derivatives + commodities,
    `_BACKFILL_START` (2026-04-01) -> today. Each segment's own range is
    later split into calendar-month chunks by `chunk_by_calendar_month()`
    inside `sync_account()`, for BOTH report types (tradebook and pnl) —
    this function itself still returns the one wide logical range per
    segment; chunking is applied uniformly at the pull site, not here."""
    return {
        "equity": (_BACKFILL_START, today),
        "derivatives": (_BACKFILL_START, today),
        "commodities": (_BACKFILL_START, today),
    }


# Exchange -> this script's own Console segment bucket. BFO/CDS share
# the 'derivatives' (equity F&O) segment with NFO for the purposes of
# this script's Console pull — there is no dedicated currency-
# derivatives segment modelled here.
_SEGMENT_FOR_EXCHANGE: "dict[str, str]" = {
    "NFO": "derivatives", "BFO": "derivatives", "CDS": "derivatives",
    "MCX": "commodities",
}


async def resolve_sync_plan(
    expiry_pairs: "set[tuple[str, date]]", today: date,
    last_synced_through: "date | None",
) -> "dict[str, tuple[date, date]]":
    """Builds the ongoing-sync plan — per operator instruction
    (2026-11-26): the range is (previous expiry date + 1 day) -> (current
    /most recent expiry date), NOT a generic trailing lookback window,
    for whichever exchange(s) genuinely have a due sync today.

    Per exchange, resolves each of ITS OWN expiry dates' real trigger
    (next market open strictly after it) via `compute_trigger_map()` —
    the SAME `next_market_open_ist()` the overall gate already uses,
    never a second, independently-invented derivation — then takes
    `due = {expiry : floor < trigger <= today}` (`floor` = last_synced_
    through, or `date.min`). This is deliberately NOT "the latest
    expiry date <= today": that naive rule breaks for an expiry landing
    exactly on `today` (common for a weekly NFO expiry) — such an
    expiry's own trigger is tomorrow, so it is correctly excluded from
    `due`, and the PRIOR cycle's trigger (which may genuinely be due
    today) is picked up instead.

    `due` can hold MORE than one expiry when a run was missed for more
    than one cycle — `end = max(due)` (the latest due cycle),
    `start = (the expiry immediately before min(due)) + 1` (falling back
    to a conservative window when nothing older is visible), so a
    multi-cycle catch-up pulls the FULL missed span, never only the
    newest cycle's own narrow window.

    A segment covered by more than one triggering exchange (e.g.
    NFO + BFO, both 'derivatives') gets the UNION of their ranges (min
    start, max end) — a conservative widening, consistent with this
    script's own 'over-chunking is always safe' principle; never a
    narrowing.

    This range is still forced through `chunk_by_calendar_month()` in
    `sync_account()` — in the normal, no-downtime case this yields a
    SINGLE chunk well under a month (F&O expiry cycles are monthly-or-
    shorter by construction), but a multi-cycle catch-up after downtime
    still chunks safely. See `get_accumulated_pnl()`'s own docstring for
    why an overlap between this range and an earlier backfill's own
    calendar-month chunks is safe (latest-per-month-bucket wins), not a
    double-count."""
    plan: "dict[str, tuple[date, date]]" = {}
    floor = last_synced_through or date.min
    for exchange, segment in _SEGMENT_FOR_EXCHANGE.items():
        exch_pairs = {(e, d) for (e, d) in expiry_pairs if e == exchange}
        if not exch_pairs:
            continue
        trigger_map = await compute_trigger_map(exch_pairs)
        due_expiries = sorted(
            expiry for (_e, expiry), trig in trigger_map.items()
            if floor < trig <= today
        )
        if not due_expiries:
            continue
        current = due_expiries[-1]
        earliest_due = due_expiries[0]
        older = sorted(d for (_e, d) in exch_pairs if d < earliest_due)
        previous = older[-1] if older else None
        start = (
            previous + timedelta(days=1) if previous is not None
            else earliest_due - timedelta(days=_SYNC_RANGE_FALLBACK_DAYS)
        )
        end = current
        if segment in plan:
            p_start, p_end = plan[segment]
            plan[segment] = (min(p_start, start), max(p_end, end))
        else:
            plan[segment] = (start, end)
    return plan


# ---------------------------------------------------------------------------
# Per-account orchestration
# ---------------------------------------------------------------------------

@dataclass
class SyncOutcome:
    account: str
    ok: bool
    rows_ingested: int = 0
    reason: str = ""
    segment_errors: "list[str]" = field(default_factory=list)


def _console_login_and_scrape_sync(
    conn, plan: "dict[str, tuple[date, date]]",
    ledger_range: "tuple[date, date] | None" = None,
) -> dict:
    """Runs the ENTIRE Playwright-touching portion of an account sync —
    login AND every segment/report-type/chunk's navigate+download —
    synchronously, on whichever thread calls it.

    `ledger_range` (2026-11-27 fix, ninth real run found this): optional
    override for the ledger pull's date range, independent of `plan`.
    Without it, the ledger range is the union of whatever segments are in
    `plan` (see `_scrape_ledger_chunks()`'s own docstring) -- so an
    operator running one segment at a time via `--segment` (to avoid the
    browser-degradation issue documented on that flag) still had the
    ledger's own FULL multi-month range tag along on EVERY segment
    invocation, redundantly re-scraping months already ingested and
    adding enough extra page loads that the SAME degradation this flag
    exists to avoid still showed up by the end of a single-segment run.
    `--ledger-only` (CLI) passes an empty `plan` (so the segment loop
    scrapes nothing) plus this explicit range, giving the ledger its own
    dedicated, lighter invocation -- the same fresh-browser benefit
    `--segment` already gives each equity/derivatives/commodities pull.

    2026-11-26 fix (first real run against the live Console found this):
    Playwright's SYNC API refuses to start inside a thread that already
    has a running asyncio event loop (`sync_playwright().start()` raises
    "It looks like you are using Playwright Sync API inside the asyncio
    loop" — confirmed live, both accounts, zero HTTP ever reached Kite).
    `sync_account()` (async, called from `main()`'s own running loop) used
    to call `conn._attempt_login_with_cooloff()` — a SYNC method — directly
    on the main event-loop thread. This function is the fix: it is plain
    sync, with no `await` anywhere, and `sync_account()` now runs it via
    `asyncio.to_thread()`, which hands it a worker thread with no event
    loop of its own.

    Every Playwright object this creates (browser/context/page) is bound
    to THAT worker thread per Playwright's own thread-affinity rule, so
    every later Playwright call (the whole scrape loop, the diagnostics
    capture on a scrape failure, and the final `shutdown_console_browser`)
    must also happen in this SAME function call, on this SAME thread —
    never split across a second `asyncio.to_thread()` call, which the
    thread pool is not guaranteed to run on the same worker thread.

    CSV parsing/validation/DB-ingestion need no Playwright object at all,
    so they are deliberately NOT done here — see `_ingest_one_report()`,
    called by `sync_account()` back on the main event loop after this
    returns.

    Returns:
        {"login_ok": bool, "login_failed": bool, "chunks": [...]}

    `login_failed` distinguishes "was already cooling off, never
    attempted" (login_ok=False, login_failed=False) from "attempted and
    failed" (login_ok=False, login_failed=True) — only for the caller's
    logging/alert text; both are treated as not-ok. Each chunk dict is
    `{"segment", "report_type", "start", "end", "raw_bytes" | None,
    "scrape_error" | None, "diagnostics" | None}`."""
    from backend.shared.helpers.utils import mask_account
    from backend.brokers.connections import _cross_process_login_lock

    masked = mask_account(conn.account)
    try:
        if conn.is_login_cooling_off():
            return {"login_ok": False, "login_failed": False, "chunks": []}

        with conn._login_lock:
            with _cross_process_login_lock(conn.account):
                conn._attempt_login_with_cooloff()

        if conn.is_login_cooling_off():
            return {"login_ok": False, "login_failed": True, "chunks": []}

        page = conn._console_page
        reports = KiteConsoleReportsPage(page)
        chunks: "list[dict]" = []
        chunks.extend(_scrape_segment_chunks(reports, page, masked, plan))
        chunks.extend(_scrape_ledger_chunks(reports, page, masked, plan, ledger_range))
        return {"login_ok": True, "login_failed": False, "chunks": chunks}
    finally:
        shutdown_console_browser(conn)


# Bounded retry count for a single chunk's scrape+validate cycle. Confirmed
# live, 2026-11-26 (sixth/seventh real runs): Kite Console itself has an
# internal race between its on-screen "fetched" label (which updates
# promptly and correctly) and the actual CSV/XLSX export it serves (which
# sometimes still reflects the PREVIOUS query for a few seconds after) --
# `_verify_fetched_date_range()` cannot catch this, because the label genuinely
# shows the right range while the export is still stale underneath. A fresh
# retry (full re-navigate, re-select, re-date-range, re-submit, re-download)
# resolves it in practice, since the retry's own later wall-clock timing no
# longer races the same backend state. 3 total attempts, not unbounded --
# this must never turn one transient Kite-side race into an indefinite hang
# during the (also time-bounded) scrape loop.
_CHUNK_SCRAPE_MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Stale pnl-export detection — Kite's date-range picker race (see
# `_CHUNK_SCRAPE_MAX_ATTEMPTS`'s own comment above) is UNDETECTABLE by
# `validate_pnl_rows()` alone: a P&L-statement row carries no per-row date
# to range-check against (it is a symbol-level aggregate over the WHOLE
# requested range), unlike Tradebook/Ledger, which are defended by
# `#date_fetched_label` + `_verify_fetched_date_range()` (confirmed absent
# from the P&L report's own DOM — see `KiteConsoleReportsPage._DATE_
# FETCHED_LABEL_SELECTOR`'s own docstring). Measured impact on prod:
# 176 distinct (account, segment, symbol, realized_pnl) groups recurring
# identically across 2-5 different `pull_range_start` months, net
# overcounting realized P&L by ~₹784,975 across both accounts and all
# three segments. `reconcile_against_daily_book()` never caught it
# because it only runs after a full, cleanly-completed `mode=backfill`
# invocation, and none occurred during the affected window.
#
# Two layers, both keyed on the SAME content signature:
#   Layer 1 (`_make_pnl_dup_check`, consumed by `_scrape_one_chunk()`) —
#       within ONE scrape run, a pnl chunk whose full (symbol,
#       realized_pnl) content is byte-identical to an EARLIER, DIFFERENT-
#       dated chunk already accepted this run is treated as a stale
#       download and fed into the existing bounded retry loop, exactly
#       like a date-range-mismatch failure.
#   Layer 2 (`_assert_pnl_not_cross_run_duplicate`, consumed by
#       `_ingest_one_report()`) — a cross-run backstop for whatever
#       layer 1 cannot see (e.g. a `sync`-mode single-chunk pull on a
#       LATER, independent run that happens to duplicate an already-
#       ingested backfill chunk): before upserting, refuse ingestion
#       outright via the existing `ReportValidationError` path if this
#       chunk's content signature already exists in the table under a
#       DIFFERENT `pull_range_start` for this (account, segment).
# ---------------------------------------------------------------------------

# Sentinel token for a missing/unparseable realized_pnl value in the
# content signature below — distinct from any real numeric token, so a
# genuinely-null pnl value is never confused with a real zero.
_PNL_SIG_NONE_TOKEN = "\x00NONE\x00"


def _pnl_value_token(value: object) -> str:
    """Normalizes one `realized_pnl` value (a native float, fresh out of
    `parse_pnl_statement_xlsx()`, OR a `Decimal` read back from this
    table's own `Numeric(18, 4)` column — the two layers below compare
    across BOTH of these representations) into a stable string token.

    Comparing raw floats/Decimals directly would make the two layers'
    signatures for the exact same real value silently never match — a
    freshly-parsed `45000.0` and a stored `Decimal('45000.0000')` are
    not `==` to a naive string/repr comparison, and float reprs of the
    identical decimal value can differ across the two code paths.
    Routing both through `Decimal(str(value)).quantize(...)` first
    normalizes away both gaps.

    `rounding=ROUND_HALF_UP` is deliberate, not the `quantize()`
    default (`ROUND_HALF_EVEN`) — Postgres's own `numeric(18, 4)`
    column (this table's `realized_pnl` type) rounds a tie AWAY from
    zero on storage, same as `ROUND_HALF_UP`'s "half away from zero"
    semantics, not banker's rounding. A Console amount that happens to
    carry a 5th decimal landing exactly on a tie (the ledger's own
    confirmed real export carries 6 decimals, e.g. `27579.231718` — see
    `parse_ledger_csv()`'s own docstring) would otherwise round
    DIFFERENTLY in this function than it was stored, breaking the
    cross-type match this whole signature exists to make reliable."""
    if value is None:
        return _PNL_SIG_NONE_TOKEN
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)


_ZERO_PNL_TOKEN = _pnl_value_token(0)


def _pnl_content_signature(rows: "list[dict]") -> "frozenset | None":
    """Order-independent content signature over a pnl chunk's full set of
    (symbol, realized_pnl) pairs — the WHOLE chunk's content, not any
    one row — shared by both layer 1 (within-run) and layer 2
    (cross-run) stale-export detectors below.

    Returns `None` — meaning "no signal, never compare" — for a
    genuinely empty chunk, AND for a chunk where every row's own
    realized_pnl is null/zero. Both are legitimate, routinely-recurring
    results (a quiet month for every symbol in a segment is ordinary),
    and a duplicated empty-or-all-zero result can never overcount real
    P&L in the first place — flagging it would just be a steady stream
    of false positives on every genuinely flat month."""
    pairs: "set[tuple[str, str]]" = set()
    for r in rows:
        symbol = (r.get("symbol") or "").strip()
        pairs.add((symbol, _pnl_value_token(r.get("realized_pnl"))))
    if not pairs:
        return None
    if all(tok in (_PNL_SIG_NONE_TOKEN, _ZERO_PNL_TOKEN) for _sym, tok in pairs):
        return None
    return frozenset(pairs)


def _make_pnl_dup_check(seen: "dict[frozenset, tuple[date, date]]"):
    """Layer 1 — returns a closure bound to `seen` (one dict per
    (account, segment), built fresh per segment by
    `_scrape_segment_chunks()`), to be passed as `_scrape_one_chunk()`'s
    `dup_check` hook for `report_type='pnl'` chunks only.

    Raises `StaleReportContentError` when THIS chunk's content
    signature already exists in `seen` under a DIFFERENT (start, end)
    — every real pull within one `_scrape_segment_chunks()` call
    targets a unique, non-overlapping calendar-month range, so a
    second chunk's content can only legitimately match an earlier
    chunk's if it is the SAME chunk being retried (same start/end,
    which this function never flags). Records the signature on a
    non-flagged, non-`None` result so the NEXT chunk can be checked
    against it in turn."""
    def _check(rows: "list[dict]", start: date, end: date) -> None:
        sig = _pnl_content_signature(rows)
        if sig is None:
            return
        prior = seen.get(sig)
        if prior is not None and prior != (start, end):
            raise StaleReportContentError(
                f"pnl export for {start}..{end} is byte-identical to the "
                f"export already accepted for {prior[0]}..{prior[1]} this "
                "run -- Kite Console served a stale report (date-range "
                "picker race)"
            )
        seen[sig] = (start, end)
    return _check


def _validate_raw_report(report_type: str, segment: str, start: date, end: date,
                          raw_bytes: bytes) -> "list[dict]":
    """Re-used by the retry loop (to decide whether to retry) AND, via
    `_ingest_one_report()`, by the real ingestion pass. Deliberately the
    IDENTICAL parse+validate calls in both places -- running the same
    pure, Playwright-independent functions twice on a successful chunk is
    cheap, and keeping one implementation (not two copies that could
    drift) is worth that. Raises `ReportValidationError` on a bad report;
    returns the parsed rows on a good one -- `_scrape_one_chunk()` uses
    this to feed the layer-1 stale-pnl-content check (`dup_check` below)
    without a second, duplicate parse of the same bytes."""
    if report_type == "tradebook":
        rows = parse_tradebook_csv(raw_bytes)
        validate_tradebook_rows(rows, segment=segment, start=start, end=end)
    elif report_type == "pnl":
        rows = parse_pnl_statement_xlsx(raw_bytes)
        validate_pnl_rows(rows, segment=segment)
    else:
        rows = parse_ledger_csv(raw_bytes)
        validate_ledger_rows(rows, start=start, end=end)
    return rows


def _scrape_one_chunk(
    reports: "KiteConsoleReportsPage", page, masked: str, *,
    segment: str, report_type: str, start: date, end: date,
    setup_query: "Callable[[], None]", diag_label: str,
    dup_check: "Callable[[list[dict], date, date], None] | None" = None,
) -> dict:
    """Scrape ONE (segment, report_type, date-range) chunk, retrying the
    whole navigate->setup->submit->download->validate cycle up to
    `_CHUNK_SCRAPE_MAX_ATTEMPTS` times on either a scrape-level exception
    or a validation failure -- see `_CHUNK_SCRAPE_MAX_ATTEMPTS`'s own
    comment for why a retry (not just a longer wait) is the right fix for
    the specific race this guards against. Diagnostics are captured only
    on the FINAL failed attempt, not every one, so a transient first
    failure that the retry recovers from doesn't spam the diagnostics
    directory. `setup_query` performs whatever this report type needs
    between `navigate_to_reports()` and `trigger_download()` (segment
    select + date range for tradebook/pnl; just the ledger date range for
    ledger, which has no segment selector at all).

    `dup_check` (layer 1 of the stale-pnl-export fix -- see the module
    section above `_CHUNK_SCRAPE_MAX_ATTEMPTS`) is called with the freshly
    parsed rows + this chunk's own (start, end) right after a successful
    `_validate_raw_report()`, and may raise to reject an otherwise-valid-
    looking download as stale -- caught by the SAME except/retry below as
    any other scrape failure. `None` for every caller except the pnl
    report-type loop in `_scrape_segment_chunks()`."""
    entry: dict = {
        "segment": segment, "report_type": report_type,
        "start": start, "end": end,
        "raw_bytes": None, "scrape_error": None, "diagnostics": None,
    }
    last_exc: Exception | None = None
    for attempt in range(1, _CHUNK_SCRAPE_MAX_ATTEMPTS + 1):
        try:
            reports.navigate_to_reports(report_type)
            setup_query()
            download_path = reports.trigger_download(
                report_type=report_type, start=start, end=end, masked_account=masked,
            )
            raw_bytes = download_path.read_bytes()
            parsed_rows = _validate_raw_report(report_type, segment, start, end, raw_bytes)
            if dup_check is not None:
                dup_check(parsed_rows, start, end)
            entry["raw_bytes"] = raw_bytes
            return entry
        except Exception as e:
            last_exc = e
            if attempt < _CHUNK_SCRAPE_MAX_ATTEMPTS:
                logger.warning(
                    "[KITE-CONSOLE-SYNC] %s attempt %d/%d failed, retrying: %s",
                    diag_label, attempt, _CHUNK_SCRAPE_MAX_ATTEMPTS, e,
                )
    entry["scrape_error"] = (
        f"failed after {_CHUNK_SCRAPE_MAX_ATTEMPTS} attempts: {last_exc}"
    )
    entry["diagnostics"] = capture_diagnostics(page, masked, diag_label)
    return entry


def _scrape_segment_chunks(reports, page, masked: str, plan) -> "list[dict]":
    chunks: "list[dict]" = []
    for segment, (start, end) in plan.items():
        # Layer 1 of the stale-pnl-export fix (see the module section
        # above `_CHUNK_SCRAPE_MAX_ATTEMPTS`) -- one fresh registry per
        # segment per account-sync, so a genuine content coincidence
        # across two DIFFERENT segments (never expected in practice, but
        # not ruled out) can never cross-contaminate. Never shared with
        # tradebook — only ever read/written for report_type='pnl' below.
        pnl_signatures: "dict[frozenset, tuple[date, date]]" = {}
        for report_type in ("tradebook", "pnl"):
            for c_start, c_end in chunk_by_calendar_month(start, end):
                # pnl ONLY: floor the request's own start to the 1st
                # of c_start's month. Still <= ~1 month wide (c_end
                # is already bounded by chunk_by_calendar_month), so
                # the Console limit still holds — but this makes
                # EVERY pnl request for "this calendar month",
                # whichever caller/trigger produced it (a one-time
                # backfill's own full-month chunk, a later sync's
                # narrower expiry-aligned chunk, or a same-day
                # re-run), anchor its requested range at the SAME
                # start, so they become directly comparable as
                # successive snapshots of one month rather than
                # genuinely different, non-comparable ranges. See
                # `get_accumulated_pnl()`'s own docstring for why
                # that comparability is what makes "pick the latest
                # one per month, then sum across months" correct —
                # without this floor, two pulls for the same month
                # with different `pull_range_start` would get
                # DIFFERENT `dedup_key`s and silently SUM instead of
                # replacing each other, double-counting the overlap.
                # Tradebook needs no such floor — trade_id-based
                # dedup already makes any overlapping window safe.
                req_start = (
                    date(c_start.year, c_start.month, 1)
                    if report_type == "pnl" else c_start
                )
                label = "tradebook" if report_type == "tradebook" else "pnl"
                entry = _scrape_one_chunk(
                    reports, page, masked,
                    segment=segment, report_type=report_type,
                    start=req_start, end=c_end,
                    setup_query=lambda s=segment, rs=req_start, ce=c_end: (
                        reports.select_segment(s), reports.set_date_range(rs, ce),
                    ),
                    diag_label=f"scrape_{segment}_{label}",
                    dup_check=(
                        _make_pnl_dup_check(pnl_signatures)
                        if report_type == "pnl" else None
                    ),
                )
                chunks.append(entry)
    return chunks


def _scrape_ledger_chunks(
    reports, page, masked: str, plan,
    ledger_range: "tuple[date, date] | None" = None,
) -> "list[dict]":
    """Ledger (Funds Statement) — pulled ONCE per account, not once per
    segment: this report has no segment dimension at all (its own
    Category select is never touched — see parse_ledger_csv's docstring),
    so looping it inside the segment loop above would scrape and ingest
    the SAME combined ledger three times over. Its own date range is the
    UNION across every segment in `plan` (min start, max end) — a ledger
    spans every segment's activity regardless of which segments actually
    triggered this sync, so narrowing to one segment's own range would
    miss margin events tied to a DIFFERENT segment's positions.

    `ledger_range`, when given, OVERRIDES the plan-derived union entirely
    — see `_console_login_and_scrape_sync()`'s own docstring for why
    (`--ledger-only`'s dedicated, segment-free invocation)."""
    if ledger_range is not None:
        ledger_start, ledger_end = ledger_range
    elif plan:
        ledger_start = min(s for s, _e in plan.values())
        ledger_end = max(e for _s, e in plan.values())
    else:
        return []
    chunks: "list[dict]" = []
    for c_start, c_end in chunk_by_calendar_month(ledger_start, ledger_end):
        entry = _scrape_one_chunk(
            reports, page, masked,
            segment=_LEDGER_SEGMENT, report_type="ledger",
            start=c_start, end=c_end,
            setup_query=lambda cs=c_start, ce=c_end: reports.set_ledger_date_range(cs, ce),
            diag_label="scrape_ledger",
        )
        chunks.append(entry)
    return chunks


async def _assert_pnl_not_cross_run_duplicate(
    masked: str, segment: str, pull_start: date, parsed_rows: "list[dict]",
) -> None:
    """Layer 2 of the stale-pnl-export fix (see the module section above
    `_CHUNK_SCRAPE_MAX_ATTEMPTS`) — a cross-run backstop for whatever
    layer 1 (`_make_pnl_dup_check`, scoped to ONE scrape run) cannot see
    — e.g. a `sync`-mode single-chunk pull on a LATER, independent run
    that happens to duplicate content an earlier backfill run already
    ingested. Raises `ReportValidationError` — caught by
    `_ingest_one_report()`'s existing except block, which alerts and
    refuses the ingest, exactly as for any other validation failure —
    when this chunk's own (symbol, realized_pnl) content signature
    already exists in `kite_console_pnl_sync` for this (account,
    segment, report_type='pnl') under a DIFFERENT `pull_range_start`.

    A legitimate same-month re-run (same `pull_range_start`, a later or
    wider `pull_range_end`) is excluded at the query level, never
    flagged — that is the existing, already-correct "latest wins"
    design `get_accumulated_pnl()` relies on, not a stale-content
    signal. A genuinely empty (or all-zero) chunk is never checked at
    all — see `_pnl_content_signature()`'s own docstring for why a
    duplicated empty/all-zero result can never overcount P&L and must
    never be flagged.

    Deliberately reads `KiteConsolePnlSync` directly here, NOT inside
    `ingest_rows()` — that function is shared with
    `scripts/dhan_pnl_sync.py`, which is explicitly out of scope for
    this fix (the stale-export race is a Kite Console UI defect, not a
    Dhan one). Also deliberately does not filter on `broker` — the
    masked `account` form alone already disambiguates Kite from Dhan
    rows (different broker-letter mask prefix), per this model's own
    `broker` column docstring."""
    new_sig = _pnl_content_signature(parsed_rows)
    if new_sig is None:
        return

    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import KiteConsolePnlSync

    T = KiteConsolePnlSync
    async with async_session() as session:
        result = await session.execute(
            select(T.pull_range_start, T.pull_range_end, T.symbol, T.realized_pnl)
            .where(
                T.account == masked,
                T.segment == segment,
                T.report_type == "pnl",
                T.pull_range_start.isnot(None),
                T.pull_range_start != pull_start,
            )
        )
        existing_rows = result.all()

    by_batch: "dict[tuple, set]" = {}
    for p_start, p_end, symbol, pnl in existing_rows:
        by_batch.setdefault((p_start, p_end), set()).add(
            ((symbol or "").strip(), _pnl_value_token(pnl))
        )

    for (other_start, other_end), pairs in by_batch.items():
        if frozenset(pairs) == new_sig:
            raise ReportValidationError(
                f"pnl content for {segment}/pnl pull_range_start={pull_start} "
                "is byte-identical to the already-ingested pull for "
                f"{other_start}..{other_end} -- refusing to ingest a stale/"
                "duplicated Kite Console export"
            )


async def _ingest_one_report(
    masked: str, segment: str, report_type: str,
    start: date, end: date, raw_bytes: bytes,
) -> "tuple[int, list[str]]":
    """Parses, validates and ingests ONE already-scraped report's raw
    bytes — touches no Playwright object, so it runs on the main event
    loop, not the worker thread `_console_login_and_scrape_sync()` runs
    on. Split out of that function's predecessor (the old
    `_pull_and_ingest_one_report()`) by the 2026-11-26 threading fix —
    see that function's own docstring."""
    from backend.api.database import async_session

    label = {"tradebook": "tradebook", "pnl": "pnl", "ledger": "ledger"}.get(report_type, report_type)
    try:
        if report_type == "tradebook":
            parsed_rows = parse_tradebook_csv(raw_bytes)
            validate_tradebook_rows(parsed_rows, segment=segment, start=start, end=end)
        elif report_type == "pnl":
            parsed_rows = parse_pnl_statement_xlsx(raw_bytes)
            validate_pnl_rows(parsed_rows, segment=segment)
            # Layer 2 of the stale-pnl-export fix — see this function's
            # own docstring above.
            await _assert_pnl_not_cross_run_duplicate(masked, segment, start, parsed_rows)
        else:
            parsed_rows = parse_ledger_csv(raw_bytes)
            validate_ledger_rows(parsed_rows, start=start, end=end)
    except ReportValidationError as e:
        await alert_critical(
            f"Kite Console report failed validation ({segment}/{label})", masked,
            detail=str(e),
        )
        return 0, [f"{segment}/{label}: validation failed: {e}"]

    if report_type == "ledger":
        async with async_session() as session:
            n = await ingest_ledger_rows(
                session, masked, parsed_rows, pull_start=start, pull_end=end,
            )
        return n, []

    # Pull range is stored on the row (and feeds dedup_key) only for the
    # pnl report type — tradebook rows have never carried it (their
    # dedup_key is trade_id-based, not range-based); preserved as-is to
    # avoid an unrelated behaviour change to already-ingested tradebook
    # rows' stored columns.
    pull_kwargs = {"pull_start": start, "pull_end": end} if report_type == "pnl" else {}
    async with async_session() as session:
        n = await ingest_rows(session, masked, segment, report_type, parsed_rows, **pull_kwargs)
    return n, []


async def sync_account(
    row, *, plan: "dict[str, tuple[date, date]]",
    ledger_range: "tuple[date, date] | None" = None,
) -> SyncOutcome:
    """Runs the full login -> scrape -> validate -> ingest cycle for one
    broker_accounts row, across every segment in `plan` AND both report
    types (tradebook + pnl) for each segment. One (segment, report_type)
    combination's structural scraping failure never aborts the rest of
    this account's segments/report types or any other account — each is
    isolated per-chunk (see `_console_login_and_scrape_sync()`'s per-entry
    `scrape_error` and `_ingest_one_report()`'s own try/except), matching
    this codebase's `@for_all_accounts` per-account-degrades-not-the-batch
    convention.

    2026-11-26 threading fix — the entire Playwright-touching sequence
    (login + every segment/report-type/chunk's scrape) now runs via
    `asyncio.to_thread(_console_login_and_scrape_sync, ...)`, confirmed
    necessary by a real first run: Playwright's sync API cannot start
    inside a thread that already has a running asyncio event loop, which
    this function's caller (`main()`) always does. See that function's
    own docstring for the full reasoning, including why the scrape loop
    and the shutdown must stay inside that ONE call rather than being
    split across a second `asyncio.to_thread()` invocation.

    2026-11-26 redesign (pre-existing, unchanged by the threading fix) —
    BOTH report types now pull `plan`'s own range for a segment, each
    split into non-overlapping calendar-month chunks via
    `chunk_by_calendar_month()` (Kite Console's CSV export does not
    reliably support a range wider than ~1 month). The pnl-statement no
    longer pulls a SEPARATE fixed-anchor-to-today range — that design
    assumed pulls were wide, overlapping, variable-width ranges; every
    real pull is now one of a fixed set of non-overlapping calendar-
    month-aligned chunks, so a (symbol, expiry)'s total realized P&L is
    the SUM of its disjoint chunks (see `get_accumulated_pnl()`), and a
    chunk's own `pull_range_start`/`pull_range_end` are exactly the
    chunk's own boundaries, never the full un-chunked `plan` range."""
    from backend.shared.helpers.utils import mask_account

    account = row.account
    masked = mask_account(account)
    secrets_blob = build_console_secrets_blob(account, row)
    conn = build_console_connection(account, secrets_blob)

    result = await asyncio.to_thread(_console_login_and_scrape_sync, conn, plan, ledger_range)

    if not result["login_ok"]:
        if result["login_failed"]:
            await alert_critical(
                "Kite Console login failed", masked,
                detail="browser login did not succeed; see [KITE-LOGIN-COOLOFF] logs",
            )
            return SyncOutcome(account=masked, ok=False, reason="login_failed")
        logger.warning(
            "[KITE-CONSOLE-SYNC] %s: login cooling off, skipping this run", masked,
        )
        return SyncOutcome(account=masked, ok=False, reason="login_cooling_off")

    total_rows = 0
    segment_errors: "list[str]" = []
    for c in result["chunks"]:
        segment, report_type = c["segment"], c["report_type"]
        label = {"tradebook": "tradebook", "pnl": "pnl", "ledger": "ledger"}.get(
            report_type, report_type,
        )
        if c["scrape_error"] is not None:
            await alert_critical(
                f"Kite Console scraping failed ({segment}/{label})", masked,
                detail=c["scrape_error"], diagnostics=c["diagnostics"],
            )
            segment_errors.append(f"{segment}/{label}: scrape failed: {c['scrape_error']}")
            continue
        n, errs = await _ingest_one_report(
            masked, segment, report_type, c["start"], c["end"], c["raw_bytes"],
        )
        total_rows += n
        segment_errors.extend(errs)
    return SyncOutcome(
        account=masked, ok=not segment_errors, rows_ingested=total_rows,
        reason="" if not segment_errors else "segment_errors",
        segment_errors=segment_errors,
    )


# ---------------------------------------------------------------------------
# Accumulation — query-time aggregation over the deduped base rows, NEVER
# an incrementally-mutated running counter (that would double-count every
# time an overlapping range gets re-ingested). Correct by construction
# regardless of how many times a range is re-pulled, because
# `ingest_rows()` upserts each dedup_key in place, never duplicating it.
# ---------------------------------------------------------------------------

async def get_accumulated_pnl(
    account: "str | None" = None,
    instrument_type: "str | None" = None,
    symbol_root: "str | None" = None,
    expiry: "date | None" = None,
) -> dict:
    """Accumulated realized P&L over `kite_console_pnl_sync`
    (`report_type='pnl'` rows only), grouped by every requested dimension
    left ungrouped by the filters above.

    2026-11-26 redesign (see `KiteConsolePnlSync`'s model docstring for
    the fuller reasoning) — real-world constraint discovered before any
    real data existed in this table (confirmed 0 rows in prod at the
    time): Kite Console's Tradebook/P&L-statement CSV export does not
    reliably support a requested date range wider than ~1 month, so
    every real pull is now forced through `chunk_by_calendar_month()`
    into non-overlapping calendar-month-aligned chunks.

    This is NOT a bare `SUM(realized_pnl)` across every matching row —
    an earlier version of this redesign was, and that double-counts on
    two realistic paths: (a) re-running the SAME month (a retried or
    re-triggered pull) produces a DIFFERENT `pull_range_end` (today's
    date moves), hence a DIFFERENT `dedup_key`, hence a SECOND row for
    the same month rather than an upsert in place; (b) the FIRST ongoing
    sync after a one-time backfill can genuinely overlap the backfill's
    own last month (an expiry essentially never lands exactly on a
    calendar-month boundary), again with a different `pull_range_end`
    for that shared month. A bare SUM would double-count both.

    The actual rule: per (account, segment, symbol, expiry,
    `pull_range_start`) — `sync_account()` floors every PNL chunk's own
    `pull_range_start` to the 1st of its calendar month, so this key is
    stable and shared by every pull that ever touched "this symbol, this
    expiry, this calendar month", regardless of which caller/trigger
    produced it — pick only the row with the LATEST `pull_range_end`
    (via a `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY pull_range_end
    DESC, updated_at DESC)` window, `rn=1`). That is the single most
    complete picture of that one month. Summing is then correct, and
    happens, ACROSS the resulting per-month "latest" rows — both across
    DIFFERENT symbols within one requested grouping (e.g. multiple
    strikes under one underlying+expiry) and across DIFFERENT months for
    the same symbol+expiry (a position spanning more than one calendar
    month), since those are genuinely non-overlapping facts once each
    month's own latest-pull value has been picked.

    Returns:
        {
          "groups": {(account, segment, instrument_type, symbol_root, expiry): pnl, ...},
          "by_account_root_expiry": {(account, symbol_root, expiry): pnl, ...},
          "overall_total": float,
        }

    `by_account_root_expiry` collapses `segment`/`instrument_type` out of
    the detailed `groups` key (summed in Python over the already-fetched
    rows — no second query) — this is the shape
    `reconcile_against_daily_book()` below needs, since `daily_book` has
    no `instrument_type` column to join against.

    `account`, when given, is the RAW account code (matching every other
    function in this script) — masked internally before filtering, since
    `KiteConsolePnlSync.account` stores the masked form by deliberate,
    already-tested design (see that model's own docstring). This is a
    flagged "worth a second look" item, not silently changed: the join
    is only as stable as whatever account roster `register_accounts()`
    was last called with in this process — the same precondition every
    other `mask_account()` consumer in this codebase already has.
    """
    from sqlalchemy import select, func, and_
    from backend.api.models import KiteConsolePnlSync
    from backend.api.database import async_session
    from backend.shared.helpers.utils import mask_account

    T = KiteConsolePnlSync
    filters = [T.report_type == "pnl"]
    if account:
        filters.append(T.account == mask_account(account))
    if instrument_type:
        filters.append(T.instrument_type == instrument_type)
    if symbol_root:
        filters.append(T.symbol_root == symbol_root)
    if expiry:
        filters.append(T.expiry == expiry)

    rn = func.row_number().over(
        partition_by=(T.account, T.segment, T.symbol, T.expiry, T.pull_range_start),
        order_by=(T.pull_range_end.desc(), T.updated_at.desc()),
    ).label("rn")

    ranked = (
        select(
            T.account, T.segment, T.instrument_type, T.symbol_root,
            T.symbol, T.expiry, T.pull_range_start, T.realized_pnl, rn,
        )
        .where(and_(*filters))
        .subquery()
    )
    latest_per_month = select(ranked).where(ranked.c.rn == 1).subquery()

    group_cols = (
        latest_per_month.c.account, latest_per_month.c.segment,
        latest_per_month.c.instrument_type, latest_per_month.c.symbol_root,
        latest_per_month.c.expiry,
    )
    stmt = (
        select(*group_cols, func.sum(latest_per_month.c.realized_pnl).label("total_pnl"))
        .group_by(*group_cols)
    )

    async with async_session() as session:
        result = await session.execute(stmt)
        rows = result.all()

    groups: dict = {}
    by_account_root_expiry: dict = {}
    overall_total = 0.0
    for acct, seg, itype, root, exp, total in rows:
        total_f = float(total) if total is not None else 0.0
        groups[(acct, seg, itype, root, exp)] = total_f
        collapsed_key = (acct, root, exp)
        by_account_root_expiry[collapsed_key] = (
            by_account_root_expiry.get(collapsed_key, 0.0) + total_f
        )
        overall_total += total_f

    return {
        "groups": groups,
        "by_account_root_expiry": by_account_root_expiry,
        "overall_total": overall_total,
    }


# ---------------------------------------------------------------------------
# Reconciliation — compares the CSV-sourced ground truth (above) against
# the existing daily_book-derived P&L, flagging "missing" vs "disagree" as
# genuinely distinct outcomes, never conflated.
# ---------------------------------------------------------------------------

# Tolerance below which a delta is NOT flagged as a discrepancy — a CSV
# snapshot (end-of-range, broker-settled) and a live-position snapshot
# (intraday, LTP-marked) can legitimately differ by a small amount from
# rounding/timing alone. ₹500 flat, or 1% of the larger of the two
# figures' magnitude, whichever is larger — generous enough to absorb
# that noise without masking a genuine disagreement.
_RECONCILE_TOLERANCE_RUPEES = 500.0
_RECONCILE_TOLERANCE_PCT = 0.01


def _reconcile_tolerance(ground_truth_pnl: float, daily_book_pnl: float) -> float:
    return max(
        _RECONCILE_TOLERANCE_RUPEES,
        _RECONCILE_TOLERANCE_PCT * max(abs(ground_truth_pnl), abs(daily_book_pnl)),
    )


def compare_pnl_groups(
    ground_truth: "dict[tuple, float]", daily_book: "dict[tuple, float]",
) -> dict:
    """Pure comparator — no I/O. Both inputs are
    `{(account, symbol_root, expiry): pnl, ...}` dicts (already filtered/
    scoped by the caller). Returns a dict with:
        groups: {key: {"ground_truth_pnl", "daily_book_pnl", "delta", "status"}}
        groups_compared / discrepancy_count / missing_count / disagree_count

    `status` is one of three MUTUALLY EXCLUSIVE outcomes, never inferred
    by the caller from `delta is None`-style guessing:
      - "missing"  — daily_book has ZERO rows for this group (the
        confirmed-missing-snapshot case this whole reconciliation exists
        to surface).
      - "disagree" — both sources have data, but the delta exceeds
        `_reconcile_tolerance()`.
      - "match"    — both sources have data and agree within tolerance.
    A group present only in `daily_book` (ground truth has no pnl row at
    all) is NOT represented here — the ground-truth CSV pull is the
    iteration driver, by design: this reconciliation's purpose is
    confirming the broker-authoritative CSV figure is reflected in
    daily_book, not the other direction.
    """
    groups: dict = {}
    missing = 0
    disagree = 0
    for key, gt_val in ground_truth.items():
        if key not in daily_book:
            groups[key] = {
                "ground_truth_pnl": gt_val, "daily_book_pnl": None,
                "delta": None, "status": "missing",
            }
            missing += 1
            continue
        db_val = daily_book[key]
        delta = gt_val - db_val
        tol = _reconcile_tolerance(gt_val, db_val)
        if abs(delta) > tol:
            status = "disagree"
            disagree += 1
        else:
            status = "match"
        groups[key] = {
            "ground_truth_pnl": gt_val, "daily_book_pnl": db_val,
            "delta": delta, "status": status,
        }
    return {
        "groups": groups,
        "groups_compared": len(groups),
        "discrepancy_count": missing + disagree,
        "missing_count": missing,
        "disagree_count": disagree,
    }


async def _daily_book_latest_pnl_by_symbol(
    account: "str | None" = None, symbol_root: "str | None" = None,
    expiry: "date | None" = None, start: "date | None" = None,
    end: "date | None" = None,
) -> "dict[tuple, float]":
    """Rolls up `daily_book` (`kind='positions'`) into
    `{(account, symbol_root, expiry): pnl, ...}`, per the one-line
    methodology given for this reconciliation: for each (account,
    symbol), take the row with the latest `captured_at` (the last
    snapshot before that position closed/rolled/expired), parse its
    symbol into (root, expiry) via the SAME `parse_tradingsymbol()`
    `classify_instrument()` uses, and sum that row's `total_pnl` across
    every symbol sharing one (account, symbol_root, expiry) group.

    Implemented LITERALLY from the one-line description given for this
    function — NOT independently re-derived, and its equivalence to the
    (unseen, this-session-only) "earlier research" that reportedly found
    daily_book missing 38-49% of relevant positions for some underlyings
    is UNVERIFIED here. `account`, when given, is the RAW code — matches
    `daily_book.account`'s own raw storage directly, no masking needed
    on this side of the join."""
    from sqlalchemy import select
    from backend.api.models import DailyBook
    from backend.api.algo.derivatives import parse_tradingsymbol
    from backend.api.database import async_session

    filters = [DailyBook.kind == "positions"]
    if account:
        filters.append(DailyBook.account == account)
    if start:
        filters.append(DailyBook.date >= start)
    if end:
        filters.append(DailyBook.date <= end)

    async with async_session() as session:
        result = await session.execute(
            select(
                DailyBook.account, DailyBook.symbol,
                DailyBook.total_pnl, DailyBook.captured_at,
            ).where(*filters)
        )
        rows = result.all()

    latest: "dict[tuple, tuple]" = {}
    for acct, symbol, total_pnl, captured_at in rows:
        key = (acct, symbol)
        existing = latest.get(key)
        if existing is None or captured_at > existing[1]:
            latest[key] = (total_pnl, captured_at)

    grouped: "dict[tuple, float]" = {}
    for (acct, symbol), (total_pnl, _captured_at) in latest.items():
        try:
            parsed = parse_tradingsymbol(symbol or "")
        except Exception:
            parsed = None
        root = (parsed.get("root") if parsed else None) or (symbol or "").upper()
        exp = parsed.get("expiry") if parsed else None
        if symbol_root and root != symbol_root:
            continue
        if expiry and exp != expiry:
            continue
        gkey = (acct, root, exp)
        grouped[gkey] = grouped.get(gkey, 0.0) + float(total_pnl or 0.0)
    return grouped


async def reconcile_against_daily_book(
    account: "str | None" = None, symbol_root: "str | None" = None,
    expiry: "date | None" = None, start: "date | None" = None,
    end: "date | None" = None,
) -> dict:
    """Compares the CSV-sourced ground truth (`get_accumulated_pnl()`)
    against the existing `daily_book`-derived P&L
    (`_daily_book_latest_pnl_by_symbol()`), per (account, symbol_root,
    expiry). See `compare_pnl_groups()` for the comparison semantics —
    "missing" and "disagree" are reported as genuinely distinct outcomes.

    `start`/`end` scope only the `daily_book` side (it has no natural
    equivalent on the ground-truth side, which is already the SUM of
    every non-overlapping calendar-month chunk pulled so far — see
    `get_accumulated_pnl()`'s own docstring).

    Account-representation note (the same flagged join-key mismatch as
    `get_accumulated_pnl()`): `daily_book`'s grouping key carries the RAW
    account code, while `get_accumulated_pnl()`'s own key carries the
    MASKED form (`KiteConsolePnlSync.account`'s deliberate, already-tested
    storage convention). Both sides are remapped onto the masked form
    here, via `mask_account()`, before comparing — so this reconciliation
    is only as reliable as whatever account roster `register_accounts()`
    was last called with in this process."""
    from backend.shared.helpers.utils import mask_account

    ground_truth_result = await get_accumulated_pnl(
        account=account, symbol_root=symbol_root, expiry=expiry,
    )
    daily_book_grouped = await _daily_book_latest_pnl_by_symbol(
        account=account, symbol_root=symbol_root, expiry=expiry,
        start=start, end=end,
    )
    daily_book_masked = {
        (mask_account(acct), root, exp): pnl
        for (acct, root, exp), pnl in daily_book_grouped.items()
    }
    return compare_pnl_groups(
        ground_truth_result["by_account_root_expiry"], daily_book_masked,
    )


# ---------------------------------------------------------------------------
# Part 3 — surface a genuine reconciliation "disagree" finding through
# the standard log-then-dispatch alert pipeline. Never a direct send —
# see this module's own "Alert delivery" docstring section and
# CLAUDE.md's "Alert identity SSOT"/"Alert-agent audit" entries. A
# "missing"/"match" group is never alert-worthy on its own; this fires
# AT MOST once per reconciliation run (one summary record covering
# every disagreeing group from that run, not one alert per group).
# ---------------------------------------------------------------------------

_RECONCILE_ALERT_TOP_N = 15


def _build_reconciliation_alert(summary: dict) -> "tuple[str, str, dict] | None":
    """Pure builder — extracted so `alert_pnl_reconciliation_disagreement()`
    stays a thin I/O wrapper and this is directly unit-testable with no
    DB/dispatch pipeline involved. Returns `None` when there is nothing
    to alert on (`disagree_count == 0` / no 'disagree'-status group) —
    this is the ONLY gate; a 'missing' or 'match' group never reaches
    this function's output.

    Returns `(title, body, extra)`:
      - `title`/`body` are plain text (this codebase's 2026-10 plain-
        text alert convention — no markup).
      - `body` lists at most `_RECONCILE_ALERT_TOP_N` groups, sorted by
        `|delta|` descending (the worst disagreements first), then
        "+N more" — Telegram's own 4096-character message limit makes
        listing an unbounded discrepancy set unsafe.
      - `extra` is JSON-safe (every `date`/`None` expiry is rendered as
        its ISO string or `None` — `extra` is persisted to `log_events`
        JSONB) and carries the FULL disagreeing-group list under
        `extra["groups"]`, so the complete detail survives even when
        `body` is truncated. Account codes are NOT re-masked here —
        `reconcile_against_daily_book()`'s own group keys are already
        masked (`mask_account()` is not idempotent; re-masking an
        already-masked code produces a different, wrong string)."""
    groups = summary.get("groups") or {}
    disagreeing = [
        (key, g) for key, g in groups.items() if g.get("status") == "disagree"
    ]
    if not disagreeing:
        return None

    disagreeing.sort(key=lambda kv: abs(kv[1].get("delta") or 0.0), reverse=True)

    def _group_payload(key: tuple, g: dict) -> dict:
        account, symbol_root, expiry = key
        return {
            "account": account,
            "symbol_root": symbol_root,
            "expiry": expiry.isoformat() if expiry else None,
            "ground_truth_pnl": g.get("ground_truth_pnl"),
            "daily_book_pnl": g.get("daily_book_pnl"),
            "delta": g.get("delta"),
        }

    all_groups = [_group_payload(key, g) for key, g in disagreeing]

    lines = []
    for gd in all_groups[:_RECONCILE_ALERT_TOP_N]:
        exp_s = gd["expiry"] or "n/a"
        gt = gd["ground_truth_pnl"] if gd["ground_truth_pnl"] is not None else 0.0
        db = gd["daily_book_pnl"] if gd["daily_book_pnl"] is not None else 0.0
        delta = gd["delta"] if gd["delta"] is not None else 0.0
        lines.append(
            f"{gd['account']} {gd['symbol_root']} ({exp_s}): ground truth "
            f"{gt:,.2f} vs daily_book {db:,.2f} (delta {delta:,.2f})"
        )
    remaining = len(all_groups) - _RECONCILE_ALERT_TOP_N
    if remaining > 0:
        lines.append(f"+{remaining} more")

    title = f"Kite Console P&L reconciliation — {len(all_groups)} group(s) disagree"
    body = "\n".join(lines)
    extra = {
        "groups_compared": summary.get("groups_compared"),
        "missing_count": summary.get("missing_count"),
        "disagree_count": summary.get("disagree_count"),
        "groups": all_groups,
    }
    return title, body, extra


async def alert_pnl_reconciliation_disagreement(summary: dict) -> None:
    """Logs ONE tagged record when `reconcile_against_daily_book()`'s
    summary carries at least one genuine 'disagree' group, then runs the
    real flush+dispatch pipeline once (exactly like `alert_critical()`
    above — this is a standalone, short-lived process, so nothing else
    will ever dispatch this record on a timer).

    Uses this module's OWN `logger` — the SAME logger `alert_critical()`
    already uses, not a different one — at WARNING, not ERROR+:
    `tags_for()` (`log_store.py`) only auto-tags a record "error" at
    CRITICAL, so a WARNING here never collides with `ERROR_AGENT`'s
    `min_level="ERROR"` catch-all. The explicit `tags=["alert"]` set
    below is read directly by `tags_for()` whenever a record's own
    `extra["tags"]` is present, bypassing its logger-name-based
    fallback entirely — so this module's logger name (`"__main__"` when
    run as a script, `"scripts.kite_console_pnl_sync"` when imported)
    has no bearing on tagging, and neither name is in
    `log_store._SKIP_PREFIXES`, so the record genuinely reaches the
    in-process alert queue either way."""
    built = _build_reconciliation_alert(summary)
    if built is None:
        return
    title, body, extra = built
    extra = {
        **extra,
        "source_type": "process",
        "source_name": "Kite Console P&L sync",
        "tags": ["alert"],
        "alert_event": "pnl_reconciliation_disagreement",
        "title": title,
        "body": body,
    }
    logger.warning("%s", title, extra=extra)
    await _flush_and_dispatch_once()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("backfill", "sync"), required=True)
    p.add_argument(
        "--account", action="append", default=None,
        help="Restrict to one or more account codes (repeatable). "
             "Default: every active Kite account.",
    )
    p.add_argument(
        "--segment", action="append", default=None,
        choices=("equity", "derivatives", "commodities"),
        help="Restrict to one or more Console segments (repeatable). "
             "Default: every segment in the resolved plan. Confirmed live,"
             " 2026-11-26: a single browser session scraping the FULL "
             "multi-month range across every segment can degrade late in "
             "the run (a genuinely blank Console render, consistent with "
             "resource pressure in one long-lived browser instance -- see "
             "navigate_to_reports()'s own docstring). Splitting the run "
             "into one invocation per segment (each gets a fresh browser) "
             "avoids this; also the natural unit for staggering invocations "
             "a few minutes apart, per the operator's own instruction.",
    )
    p.add_argument(
        "--ledger-only", action="store_true",
        help="Pull ONLY the Funds Statement (ledger), skipping every "
             "segment's Tradebook/P&L entirely. The ledger range is the "
             "full resolved plan's own union, computed BEFORE --segment "
             "narrows anything -- mutually exclusive with --segment in "
             "effect (a --segment value is ignored when this is set, "
             "since there are no segment chunks to run at all). Confirmed"
             " live, 2026-11-27 (ninth real run): the ledger's own full"
             " multi-month range was tagging along on EVERY --segment"
             " invocation (redundantly re-scraping months already"
             " ingested), adding enough extra page loads that the same"
             " browser-degradation issue --segment exists to avoid still"
             " showed up by the end of a single-segment run. This gives"
             " the ledger its own dedicated, lighter invocation instead.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Resolve accounts/segments/date ranges and print the plan "
             "without logging in or scraping anything.",
    )
    return p


def _filter_plan_to_segments(
    plan: "dict[str, tuple[date, date]]", segments: "list[str] | None",
) -> "dict[str, tuple[date, date]]":
    """Pure helper for `--segment` — returns `plan` unchanged when no
    filter was given (the existing default behaviour), otherwise only the
    named segments' entries, in `plan`'s own original order. A named
    segment absent from `plan` (e.g. `--segment commodities` on an
    account/mode whose resolved plan never included it) is silently
    dropped, not an error -- mirrors `--account`'s own silent-narrowing
    behaviour for a name not present in the active roster."""
    if not segments:
        return plan
    wanted = set(segments)
    return {k: v for k, v in plan.items() if k in wanted}


def _resolve_plan_and_ledger_range(
    plan: "dict[str, tuple[date, date]]", args,
) -> "tuple[dict[str, tuple[date, date]], tuple[date, date] | None]":
    """Applies `--ledger-only` / `--segment` to the resolved `plan`.
    Extracted out of `main()` to keep that function's own complexity
    under the project's CC gate (`--ledger-only`'s branching pushed it
    to a D grade) — pure, no I/O, directly unit-testable.

    `--ledger-only` takes priority: the ledger range is computed from
    the FULL resolved `plan`'s own union BEFORE any `--segment` filter,
    then `plan` itself is emptied (no segment chunks at all) — see
    `_console_login_and_scrape_sync()`'s own docstring for why. A bare
    `--segment` (no `--ledger-only`) only narrows `plan`; `ledger_range`
    stays `None`, so the ledger pull keeps deriving its own range from
    whatever segments remain, unchanged from the pre-`--ledger-only`
    behaviour."""
    ledger_range: "tuple[date, date] | None" = None
    if args.ledger_only:
        if plan:
            ledger_range = (
                min(s for s, _e in plan.values()), max(e for _s, e in plan.values()),
            )
        plan = {}
    else:
        plan = _filter_plan_to_segments(plan, args.segment)
    return plan, ledger_range


async def main(argv: "list[str] | None" = None) -> int:
    from backend.shared.helpers.date_time_utils import timestamp_indian
    from backend.shared.helpers.utils import register_accounts
    from backend.api.database import async_session

    args = _build_arg_parser().parse_args(argv)
    rows = await load_active_kite_accounts(args.account)
    register_accounts([r.account for r in rows])

    today = timestamp_indian().date()

    if args.mode == "backfill":
        plan = backfill_plan(today)
    else:
        # Widened lookback (vs. the default _EXPIRY_LOOKBACK_DAYS) so
        # `resolve_sync_plan()` can see BOTH the current (just-triggered)
        # and the previous expiry cycle for an exchange from the SAME
        # `expiry_pairs` set used for the overall trigger gate below —
        # never a second, independent expiry derivation.
        async with async_session() as s:
            expiry_pairs = await collect_recent_fo_expiry_dates(
                s, lookback_days=_SYNC_RANGE_LOOKBACK_DAYS,
            )
        trigger_dates = await compute_trigger_dates(expiry_pairs)
        last_synced = await load_last_synced_through()
        if not should_sync_today(today, trigger_dates, last_synced):
            logger.info(
                "[KITE-CONSOLE-SYNC] no post-expiry trigger today (%s); nothing to do",
                today,
            )
            return 0
        # 2026-11-26 operator instruction: the ongoing-sync range is
        # (previous expiry date -> current/most recent expiry date) per
        # exchange, never a generic trailing lookback window — see
        # `resolve_sync_plan()`'s own docstring.
        plan = await resolve_sync_plan(expiry_pairs, today, last_synced)
        if not plan:
            logger.info(
                "[KITE-CONSOLE-SYNC] trigger fired today (%s) but no "
                "exchange-specific range could be resolved; nothing to do",
                today,
            )
            return 0

    plan, ledger_range = _resolve_plan_and_ledger_range(plan, args)

    if args.dry_run:
        print(json.dumps({
            "mode": args.mode,
            "accounts": [r.account for r in rows],
            "plan": {k: [str(v[0]), str(v[1])] for k, v in plan.items()},
            "ledger_range": (
                [str(ledger_range[0]), str(ledger_range[1])] if ledger_range else None
            ),
        }, indent=2))
        return 0

    if not rows:
        logger.warning("[KITE-CONSOLE-SYNC] no active Kite accounts to sync")
        return 0

    exit_code = 0
    for row in rows:
        try:
            outcome = await sync_account(row, plan=plan, ledger_range=ledger_range)
        except Exception as e:  # never let one account's unexpected failure abort the batch
            from backend.shared.helpers.utils import mask_account
            logger.exception("[KITE-CONSOLE-SYNC] unexpected failure for %s", mask_account(row.account))
            await alert_critical("Kite Console sync crashed", mask_account(row.account), detail=str(e))
            exit_code = 1
            continue
        if not outcome.ok:
            exit_code = 1

    if args.mode == "sync" and exit_code == 0:
        await mark_synced_through(today)

    if args.mode == "backfill" and exit_code == 0:
        # 2026-11-26 — also mark the sync-through marker on a successful
        # backfill (mirroring the sync branch above). This is an
        # EFFICIENCY fix, not a correctness one: without it, the FIRST
        # ongoing-sync run after a one-time backfill would treat
        # `last_synced_through` as never-set (floor=date.min) and
        # immediately re-fire for whichever cycle already fell inside
        # the backfill's own already-covered range, re-scraping Console
        # for data already ingested. Correctness for the resulting
        # overlap (a sync's own chunk and an earlier backfill's chunk
        # sharing one calendar month) is handled separately — see
        # `get_accumulated_pnl()`'s own docstring for why that overlap
        # resolves to "latest wins per month", never a double-count,
        # regardless of whether this marker is set.
        await mark_synced_through(today)

        # Best-effort reconciliation summary — never raises, never blocks
        # the real sync outcome. A backfill is the point where the
        # CSV-sourced ground truth is most complete, so it's the most
        # meaningful moment to compare it against daily_book.
        try:
            summary = await reconcile_against_daily_book()
            logger.info(
                "[KITE-CONSOLE-SYNC] reconciliation: %d group(s) compared, "
                "%d discrepancy/discrepancies found (%d missing, %d disagree)",
                summary["groups_compared"], summary["discrepancy_count"],
                summary["missing_count"], summary["disagree_count"],
            )
            # Part 3 — surface any genuine "disagree" groups through the
            # standard log-then-dispatch alert pipeline (never a direct
            # send — see `alert_pnl_reconciliation_disagreement()`'s own
            # docstring).
            await alert_pnl_reconciliation_disagreement(summary)
        except Exception:
            logger.exception("[KITE-CONSOLE-SYNC] reconciliation summary failed (non-fatal)")

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
