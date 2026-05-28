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


def is_trading_day(d, holiday_set: set | None = None,
                   extra_trading_days: set | None = None,
                   exchange: str | None = None) -> bool:
    """Authoritative "is this a trading day?" check used by every gate
    (is_market_open, agent_engine._build_context). Resolution order:

      1. Date is in `extra_trading_days` (operator override) → OPEN.
      2. `exchange` was passed AND a live broker quote shows fresh
         activity for that exchange's bellwether → OPEN. Catches:
           * MCX evening session on equity holidays (NSE COM holiday
             list flags the day as closed but MCX actually trades)
           * Diwali Muhurat sessions (not in any holiday list, fall
             on a weekend, but Kite ticks them like any other day)
           * Ad-hoc SEBI-announced sessions
      3. Date is in `holiday_set` → CLOSED.
      4. Date is a weekday (Mon-Fri) → OPEN.
      5. Otherwise (weekend, no override, no fresh ticks) → CLOSED.

    The probe is cached per-exchange for 60s and gracefully no-ops
    when no broker handle is reachable, so the gate stays fast on the
    hot path and degrades to calendar-only behaviour during a broker
    outage. Pass `exchange=None` to skip the probe entirely (tests,
    sim driver, code paths that have their own market-state)."""
    if extra_trading_days is None:
        extra_trading_days = _parse_extra_trading_days()
    if d in extra_trading_days:
        return True
    if exchange:
        try:
            from backend.shared.helpers.market_probe import probe_market_active
            probe = probe_market_active(exchange)
            if probe is True:
                return True
        except Exception:
            pass
    if holiday_set and d in holiday_set:
        return False
    return d.weekday() < 5  # Mon-Fri = trading


def is_market_open(now, holiday_set: set, market_start: dtime = dtime(9, 15),
                   market_end: dtime = dtime(15, 30),
                   exchange: str | None = None) -> bool:
    """
    Returns True if the market is currently open.
    - now: timezone-aware datetime in IST
    - holiday_set: set of date objects from fetch_holidays(exchange)
    - exchange (optional): when passed, the gate also consults
      market_probe.probe_market_active(exchange) — a live Kite-quote
      check that overrides the calendar verdict when the broker shows
      fresh ticks. Catches MCX evening sessions on equity holidays
      and Muhurat days that calendar APIs don't surface.
    - Weekend handling: regular Sat/Sun are closed, BUT operator can
      list Muhurat / special-session dates in the
      `market.extra_trading_days` setting (or just let the probe
      detect them automatically when ticks start landing).
    - Falls back to time-window-only check if holiday_set is empty.
    """
    if not is_trading_day(now.date(), holiday_set, exchange=exchange):
        return False
    t = now.time().replace(second=0, microsecond=0)
    return market_start <= t <= market_end


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
