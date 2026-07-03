from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo


from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Define constants for timezones
EST_ZONE = ZoneInfo("US/Eastern")
INDIAN_TIMEZONE = ZoneInfo("Asia/Kolkata")


# Helper functions for direct use
def timestamp_local():
    """Returns today's date in the local timezone."""
    return datetime.today()  # Uses system's local timezone


def timestamp_est():
    return datetime.now(tz=EST_ZONE)


def timestamp_indian():
    return datetime.now(tz=INDIAN_TIMEZONE)


def today_local():
    """Returns today's date in the local timezone."""
    return datetime.now().date()  # Uses system's local timezone


def today_est():
    return datetime.now(tz=EST_ZONE).date()


def today_indian():
    return datetime.now(tz=INDIAN_TIMEZONE).date()


def current_time_local():
    """Returns the current time in the local timezone."""
    return datetime.today().time()  # Uses system's local timezone


def current_time_est():
    return datetime.now(tz=EST_ZONE).time()


def current_time_indian():
    return datetime.now(tz=INDIAN_TIMEZONE).time()


def timestamp_display() -> str:
    """
    Compact dual-timezone timestamp for alerts, emails, and public-site
    refreshed_at strings. Day-first, 3-letter weekday + month, 24-hour
    time, year dropped (implied by the session). Matches
    stores.js::clientTimestamp so client-generated banners and
    server-generated refreshed_at stamps look identical everywhere.

    Example: "Sat 25 Apr 07:03 IST | Fri 24 Apr 21:33 EDT"
    %Z renders EST / EDT automatically by season.
    """
    return format_dual_tz(datetime.now(tz=INDIAN_TIMEZONE))


def format_dual_tz(dt) -> str:
    """
    Same compact dual-timezone format as `timestamp_display()`, but for an
    arbitrary `datetime`. Used for "refreshed_at" stamps tied to a
    persisted content-generation moment (DB columns) rather than the
    current request handler's wall clock.

    Naive datetimes are interpreted as UTC for safety — every persisted
    `generated_at` column on the platform stores timezone-aware UTC.
    """
    from datetime import timezone as _tz
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    ist = dt.astimezone(INDIAN_TIMEZONE)
    est = dt.astimezone(EST_ZONE)
    return f"{ist.strftime('%a %d %b %H:%M IST')} | {est.strftime('%a %d %b %H:%M %Z')}"


def _parse_extra_trading_days() -> set:
    """Parse `market.extra_trading_days` setting (CSV of YYYY-MM-DD) into
    a set of `date` objects. Operator-managed list of weekend dates that
    ARE trading days — Muhurat Diwali, special SEBI expiry Saturdays,
    etc. Kite's holiday endpoint doesn't carry these (it only lists
    weekday closures), so they need an explicit override or the
    weekday-hardcode below silently treats them as closed.

    Resolves to empty set on import error (settings table missing during
    bootstrap) or parse failure (operator typed a malformed date)."""
    try:
        from backend.shared.helpers.settings import get_string
        raw = get_string("market.extra_trading_days", "") or ""
    except Exception:
        return set()
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            from datetime import date as _date
            y, m, d = part.split("-")
            out.add(_date(int(y), int(m), int(d)))
        except Exception:
            # Bad input — log and skip. Don't crash the gate.
            try:
                from backend.shared.helpers.ramboq_logger import get_logger
                get_logger(__name__).warning(
                    f"market.extra_trading_days has invalid date {part!r}"
                )
            except Exception:
                pass
    return out


# Widest plausible Indian-market window across exchanges. NSE is
# 09:15-15:30, MCX is 09:00-23:30. Outside this window NO exchange
# can be trading, so probes are guaranteed to return False — skip
# them entirely. Used as a probe-suppression guard in is_trading_day.
_WIDEST_MARKET_WINDOW = (dtime(9, 0), dtime(23, 30))


def _calendar_says_closed(d, holiday_set: set | None) -> bool:
    """Pure-calendar verdict for `d`: True if the date is in the
    holiday list OR is a weekend. No probe, no override — just the
    cheap test we run first."""
    if holiday_set and d in holiday_set:
        return True
    return d.weekday() >= 5  # Sat/Sun


def is_trading_day(d, holiday_set: set | None = None,
                   extra_trading_days: set | None = None,
                   exchange: str | None = None,
                   now=None) -> bool:
    """Authoritative "is this a trading day?" check. Resolution order
    is layered cheapest-first so the broker probe runs only when it's
    actually needed:

      1. Date is in `extra_trading_days` (operator override) → OPEN.
      2. Calendar says open (weekday + not in holiday list) → OPEN.
         Skip probe — calendar is authoritative for the common path.
      3. Calendar says closed (holiday or weekend), AND `exchange` is
         provided, AND `now` is inside the widest Indian market
         window (09:00-23:30 IST): probe Kite for fresh ticks on
         the exchange's bellwether. If active → OPEN. Catches:
           * MCX evening session on equity holidays
           * Diwali Muhurat sessions on Saturdays
           * Ad-hoc SEBI-announced sessions
         When `now` is outside the window, the probe would return
         stale data anyway — skip it.
      4. Otherwise → CLOSED.

    Probe cost on the hot path: zero. A weekday non-holiday tick goes
    through the calendar branch and returns immediately; an off-hours
    tick is rejected by the window guard before any network IO.

    Probe call rate (rough):
      Mon-Fri non-holiday:  0 probes/day
      Holiday or weekend:   ~1/min during 09:00-23:30 (cache TTL)"""
    if extra_trading_days is None:
        extra_trading_days = _parse_extra_trading_days()
    if d in extra_trading_days:
        return True
    if not _calendar_says_closed(d, holiday_set):
        return True
    # Calendar says closed — probe only if the time-window guard
    # suggests fresh ticks could exist.
    if exchange and now is not None:
        t = now.time() if hasattr(now, "time") else now
        if _WIDEST_MARKET_WINDOW[0] <= t <= _WIDEST_MARKET_WINDOW[1]:
            try:
                from backend.shared.helpers.market_probe import probe_market_active
                if probe_market_active(exchange) is True:
                    return True
            except Exception:
                pass
    return False


def is_market_open(now, holiday_set: set, market_start: dtime = dtime(9, 15),
                   market_end: dtime = dtime(15, 30),
                   exchange: str | None = None,
                   *,
                   sessions: list | None = None,
                   evening_open_on_holidays: bool = False,
                   special_sessions: list | None = None) -> bool:
    """
    Returns True if the market is currently open.

    Precedence (highest to lowest):

    ① Special-session override — if ``special_sessions`` contains a row
      for today's date, it completely supersedes all other rules.  The
      exchange is open iff ``now.time()`` is in ``[start_time, end_time)``
      (half-open interval).  Holiday, regular-session, and probe checks
      are all skipped.  Pass None if the caller hasn't fetched overrides.

    ② Holiday check — if today is in ``holiday_set``:
      • ``evening_open_on_holidays=True`` → open only if
        ``now.time() >= 17:00`` AND in a published session window.
      • Otherwise → closed.

    ③ Regular sessions — open if ``now.time()`` is inside at least one
      configured session window.

    Calling conventions (both supported):

    Legacy (positional): ``is_market_open(now, holidays, start, end, exchange)``
      A single session window is inferred from ``market_start`` and
      ``market_end``. Pre-existing callsites keep working unchanged.

    Multi-session (keyword): ``is_market_open(now, holidays,
                                              sessions=[{start, end}, ...],
                                              evening_open_on_holidays=True)``
      When ``sessions`` is provided it overrides ``market_start`` /
      ``market_end``. The segment is open if ``now`` falls in ANY window.

    Special-session (keyword): ``is_market_open(now, holidays, ...,
                                                special_sessions=[...])``
      ``special_sessions`` is a list of dicts with keys ``date``
      (datetime.date), ``start`` (datetime.time), and ``end``
      (datetime.time).  Pass only rows for the relevant exchange.

    - now: timezone-aware datetime in IST
    - holiday_set: set of date objects from fetch_holidays(exchange)
    - exchange (optional): forwarded to ``is_trading_day`` so the probe
      layer can identify which exchange to poll.
    - special_sessions (optional): list of override rows; pass None if
      the caller doesn't fetch them (normal path remains unchanged).
    """
    from datetime import date as _date, time as _time  # local import avoids circular

    t = now.time().replace(second=0, microsecond=0)
    today = now.date()

    # ① SPECIAL-SESSION OVERRIDE — highest precedence.  If any row matches
    #    today's date, skip ALL other rules and apply the override window.
    #    Half-open interval: start <= t < end  (matches task spec).
    if special_sessions:
        today_overrides = [
            s for s in special_sessions
            if s.get("date") == today
        ]
        if today_overrides:
            return any(
                s["start"] <= t < s["end"]
                for s in today_overrides
            )
        # No override for today — fall through to normal logic.

    # Resolve session windows — sessions list wins when provided.
    if sessions:
        windows: list[tuple[dtime, dtime]] = []
        for s in sessions:
            try:
                sh, sm = (int(x) for x in str(s.get("start", "09:15")).split(":", 1))
                eh, em = (int(x) for x in str(s.get("end",   "15:30")).split(":", 1))
                windows.append((dtime(sh, sm), dtime(eh, em)))
            except Exception:
                continue
        if not windows:
            windows = [(market_start, market_end)]
    else:
        windows = [(market_start, market_end)]

    # ② Clock — must be inside AT LEAST ONE window. If not, definitely closed.
    in_window = any(w_start <= t <= w_end for (w_start, w_end) in windows)

    # ③ Calendar — a holiday date can still be "open" when
    #    evening_open_on_holidays=True AND clock >= 17:00. Handle that first
    #    so we don't spuriously call the probe on MCX evening-open holidays.
    if today in holiday_set:
        if evening_open_on_holidays and t >= dtime(17, 0):
            # Also require the clock to be in a published session window.
            return in_window
        return False

    if not in_window:
        return False

    # ④ Non-holiday, in-window — standard calendar + probe check.
    return is_trading_day(today, holiday_set,
                          exchange=exchange, now=now)


def is_any_segment_open(now) -> bool:
    """Return True if ANY configured market segment is currently open.

    Reads `market_segments` from the app config at call time so live YAML
    edits (e.g. adding a new exchange segment) land on the next call without a
    service restart.

    Handles the common MCX-open / NSE-closed window (09:00–09:15 IST) by
    checking each segment independently with its own hours and holiday
    calendar, then returning the logical OR.  This is the canonical way for
    background pollers to decide "should I fetch broker data right now?"

    Callers:
      - background._task_performance  (controls whether to fetch positions/holdings)
      - background._task_ticker_watchdog (controls whether to check for WS stalls)
      - Any new task that should run during MCX *or* NSE hours

    Args:
        now: timezone-aware IST datetime (from timestamp_indian()).

    Returns:
        True when at least one segment's session is active right now.
    """
    from backend.shared.helpers.utils import config as _cfg
    from backend.brokers.broker_apis import fetch_holidays

    segments = _cfg.get("market_segments", {}) or {}
    for seg_name, seg_cfg in segments.items():
        try:
            sessions = (seg_cfg or {}).get("sessions") or None
            evening  = bool((seg_cfg or {}).get("evening_open_on_holidays", False))
            exch     = (seg_cfg or {}).get("holiday_exchange", "NSE")
            # Legacy shape fallback — sessions is optional; existing
            # `hours_start`/`hours_end` keys still work when absent.
            h_s, m_s = map(int, (seg_cfg or {}).get("hours_start", "09:15").split(":"))
            h_e, m_e = map(int, (seg_cfg or {}).get("hours_end",   "15:30").split(":"))
            seg_start = dtime(h_s, m_s)
            seg_end   = dtime(h_e, m_e)
            try:
                holidays = fetch_holidays(exch)
            except Exception:
                holidays = set()
            try:
                from backend.brokers.broker_apis import fetch_special_sessions
                special = fetch_special_sessions(exch)
            except Exception:
                special = []
            if is_market_open(
                now, holidays, seg_start, seg_end, exchange=exch,
                sessions=sessions, evening_open_on_holidays=evening,
                special_sessions=special,
            ):
                return True
        except Exception:
            continue
    return False


def convert_to_timezone(date_str, format='%Y-%m-%d', return_date=True, tz=INDIAN_TIMEZONE):
    try:
        dt = datetime.strptime(date_str, format).replace(tzinfo=tz)  # Assign the correct timezone
        return dt if return_date is None else (dt.date() if return_date else dt.time())
    except Exception:
        logger.warning(f"Invalid date format: {date_str}")
        return None


# Test Code in __main__
if __name__ == "__main__":
    logger.info(f"EST timestamp: {timestamp_est()}")
    logger.info(f"Indian timestamp: {timestamp_indian()}")
    logger.info(f"Local timestamp: {timestamp_local()}")

    logger.info(f"Today's Date in EST: {today_est()}")
    logger.info(f"Today's Date in IST: {today_indian()}")
    logger.info(f"Current Time in IST: {today_local()}")

    logger.info(f"Current Time in IST: {current_time_indian()}")
    logger.info(f"Current Time in EST: {current_time_local()}")
    logger.info(f"Current Time in EST: {current_time_est()}")
