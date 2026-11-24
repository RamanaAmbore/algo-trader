"""
Expiry-day auto-close engine.

Identifies ITM/NTM option positions on expiry day and closes them using
the chase engine before market close.

Key rules:
  - Equity (NFO): close ALL ITM + NTM options (within buffer %)
  - Commodity (MCX): close only UNBALANCED ITM legs (hedged pairs are safe)
  - Expiry may shift due to holidays — use instrument expiry date, not weekday
  - NSE and MCX have different expiry schedules and market hours
  - Re-scan every 30 min for positions that become ITM during the day

Usage:
    engine = ExpiryEngine(on_event=callback)
    await engine.run()  # blocks until market close
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Callable, Optional

from backend.brokers import broker_apis
from backend.shared.helpers.date_time_utils import timestamp_indian, timestamp_display
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config

from backend.api.algo.chase import chase_order, ChaseConfig, ChaseResult, ChaseStatus
from backend.api.algo.derivatives import _OPT_MONTHLY
from backend.api.algo.grammar import _parsed_or_none
from backend.api.algo.order_hold import HoldCategory, _resolve_hold_auto_release_at
from backend.api.algo.order_hold_gate import expiry_close_held, record_held_order

logger = get_logger(__name__)

# Kite quote-API keys differ from the plain underlying name for NSE indices.
# "NSE:NIFTY" is invalid — the correct key is "NSE:NIFTY 50".
_NSE_INDEX_QUOTE_KEYS: dict[str, str] = {
    "NIFTY":       "NSE:NIFTY 50",
    "BANKNIFTY":   "NSE:NIFTY BANK",
    "FINNIFTY":    "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY":  "NSE:NIFTY MIDCAP SELECT",
    "SENSEX":      "BSE:SENSEX",
}
# Reverse map: "NIFTY 50" → "NIFTY", "NIFTY BANK" → "BANKNIFTY", etc.
_KITE_KEY_TO_UNDERLYING: dict[str, str] = {
    v.split(":", 1)[1]: k for k, v in _NSE_INDEX_QUOTE_KEYS.items()
}


# ── Futures-partner contract identity (2026-11-23 fix, Part C) ────────────
#
# `_classify_mcx_group` used to accept ANY held future on the same
# underlying as a netting partner, "even if the future itself expires on
# a different date" (the prior design, now reversed — an MCX option
# devolves at settlement into a future of its OWN specific contract
# month; a pre-existing position in a DIFFERENT month is a genuinely
# separate contract on the exchange's books and does not automatically
# offset the devolved position). These two helpers resolve that specific
# target purely from symbol TEXT, deliberately NOT via
# `derivatives.lookup_future_for_option()`: that function is async and
# reads the DB-backed instruments cache, and this module's netting path
# (`classify_prepared`) must stay synchronous, no-I/O — it is the SSOT
# shared with the `expiry-mcx-risk-alert` condition resolver, which runs
# on the main event loop and must never block on a cache/DB read (see
# `classify_prepared`'s own docstring). A parity test
# (`test_expiry_logic.py`) confirms this pure mirror agrees with the real
# async resolver for both a plain monthly symbol and an MCX day-suffix
# variant.
#
# Fails CLOSED: an unparseable option symbol (e.g. a weekly NSE-index
# format, which never reaches this MCX-only path in practice) resolves
# no target at all, so the option gets NO futures partner rather than
# silently accepting an unrelated contract.

def _mcx_devolvement_future_symbol(option_symbol: str) -> "str | None":
    """Pure, no-I/O: the exact `{root}{yy}{mon}FUT` tradingsymbol a
    monthly MCX option would devolve into at settlement — e.g.
    CRUDEOIL25JUN5800CE → CRUDEOIL25JUNFUT. Mirrors
    `derivatives._resolve_monthly_option_future`'s own `fut_sym`
    construction, reusing the SAME compiled regex so the two can never
    drift apart on which symbols count as "monthly"."""
    sym = (option_symbol or "").upper().strip()
    m = _OPT_MONTHLY.match(sym)
    if not m:
        return None
    root, yy, mon, _strike, _opt = m.groups()
    return f"{root}{yy}{mon}FUT"


def _future_matches_option_devolvement(option_symbol: str, future_symbol: str) -> bool:
    """True when `future_symbol` is the SPECIFIC contract
    `option_symbol` would devolve into — same root AND same contract
    month, allowing the MCX day-suffix variant some futures carry
    (e.g. CRUDEOIL26JUL19FUT for the CRUDEOIL26JULFUT month token —
    mirrors `_resolve_monthly_option_future`'s own Pass-2 prefix match).
    A future on the SAME underlying but a DIFFERENT month must never be
    accepted as a netting partner."""
    target = _mcx_devolvement_future_symbol(option_symbol)
    if target is None:
        return False
    fut = (future_symbol or "").upper().strip()
    if fut == target:
        return True
    prefix = target[:-3]  # strip the trailing "FUT"
    return fut.startswith(prefix) and fut.endswith("FUT")


@dataclass
class OptionPosition:
    account: str
    tradingsymbol: str
    exchange: str
    instrument_type: str     # CE or PE
    underlying: str          # NIFTY, BANKNIFTY, CRUDE, etc.
    strike: float
    expiry: date
    quantity: int            # positive = long, negative = short
    product: str
    ltp: float = 0.0
    underlying_ltp: float = 0.0
    moneyness: str = ""      # ITM, ATM, NTM, OTM
    needs_close: bool = False
    close_reason: str = ""
    # Per-share daily theta from analytical Greeks. Drives the greedy
    # netting priority — pairs are formed from highest |theta| first.
    theta: float = 0.0
    # Signed residual quantity after the netting pass. When zero, the
    # position is fully offset by partners and needs no close action.
    residual_qty: int = 0


@dataclass
class FuturePosition:
    """Same-underlying FUT position considered as a delta-offset
    candidate for MCX option-position netting at expiry. The MCX
    Close logic pairs option-direction with opposite-delta futures
    to neutralise directional exposure that would otherwise need
    its own close ticket."""
    account: str
    tradingsymbol: str
    underlying: str
    quantity: int            # positive = long, negative = short
    product: str


@dataclass
class ExpiryState:
    status: str = "idle"     # idle, scanning, closing, done
    positions: list = field(default_factory=list)
    pending_chases: dict = field(default_factory=dict)   # symbol → ChaseResult
    closed: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    last_scan: str = ""
    total_slippage: float = 0.0


def _exp_opt_pair_valid(same_type: bool, aq: int, bq: int) -> bool:
    """Return True if (A, B) form a valid netting pair under rules 1-4."""
    opp_sign = (aq > 0) != (bq > 0)
    return (same_type and opp_sign) or ((not same_type) and (not opp_sign))


def _exp_opt_same_contract(A: "OptionPosition", B: "OptionPosition") -> bool:
    """True when A and B are literally the SAME option contract (same
    instrument_type AND same strike) — the mirror pair rules 1/2
    describe (long CE + short CE at the same strike, etc). Their
    settlement fate is IDENTICAL by construction: moneyness is a
    deterministic function of (strike, instrument_type, spot), so two
    legs sharing both can never classify differently. This is the one
    deliberate exception to the NTM-exclusion rule below — netting a
    leg against its own exact mirror is always safe, even when both
    read NTM."""
    return A.instrument_type == B.instrument_type and A.strike == B.strike


def _best_opt_partner(
    A: "OptionPosition",
    opts_sorted: "list[OptionPosition]",
    remaining_opt: "dict[int, int]",
) -> "tuple[Optional[OptionPosition], float]":
    """Return the best option-option netting partner for A (highest |theta|).

    Valid pairs:
      - Same type, opposite sign (rules 1 & 2: long CE + short CE, long PE + short PE).
      - Opposite type, same sign (rules 3 & 4: long CE + long PE, short CE + short PE).
    Returns (None, -1.0) when no valid partner exists.

    NTM exclusion (2026-11-23 fix, Part C): an NTM leg's settlement fate
    (devolve into a future vs. expire worthless) is genuinely uncertain,
    so it must never be used to "cancel out" another leg's risk — from
    EITHER side of the pair. This is checked symmetrically here (not
    only "B is NTM"): `_net_commodity_group` walks options by |theta|
    DESCENDING, and a near-the-money leg typically carries the LARGEST
    |theta|, so the NTM leg is usually A, not B. Excluding only
    NTM-as-B would leave an NTM-as-A still free to pick an ITM partner
    and net it away. The one carve-out is `_exp_opt_same_contract` —
    A and B at the identical (strike, type) always classify to the SAME
    moneyness, so excluding that pair would also break a genuine
    same-strike hedge whenever it happens to sit in the NTM band.
    """
    aq = remaining_opt.get(id(A), 0)
    best: "Optional[OptionPosition]" = None
    best_t = -1.0
    for B in opts_sorted:
        if B is A:
            continue
        bq = remaining_opt.get(id(B), 0)
        if aq == 0 or bq == 0:
            continue
        if not _exp_opt_pair_valid(A.instrument_type == B.instrument_type, aq, bq):
            continue
        if (A.moneyness == "NTM" or B.moneyness == "NTM") and not _exp_opt_same_contract(A, B):
            continue
        t = abs(B.theta or 0.0)
        if t > best_t:
            best = B
            best_t = t
    return best, best_t


def _exp_opt_long_delta(instrument_type: str, oq: int) -> bool:
    """Return True when the option has a positive delta (long CE or short PE)."""
    return (instrument_type == "CE" and oq > 0) or (instrument_type == "PE" and oq < 0)


def _exp_fut_pair_valid(opt_long_delta: bool, fq: int) -> bool:
    """Return True if the future is a valid delta-offset partner for the option."""
    return (opt_long_delta and fq < 0) or ((not opt_long_delta) and fq > 0)


def _best_fut_partner(
    A: "OptionPosition",
    futures: "list[FuturePosition]",
    remaining_opt: "dict[int, int]",
    remaining_fut: "dict[int, int]",
) -> "tuple[Optional[FuturePosition], int]":
    """Return the best futures netting partner for A (largest |qty|).

    Long CE / Short PE (option delta +) pairs with short futures (qty < 0).
    Short CE / Long PE (option delta -) pairs with long futures (qty > 0).
    Returns (None, 0) when no valid partner exists.

    NTM exclusion (2026-11-23 fix, Part C): an NTM option's settlement
    fate is uncertain, so it is never netted against a future either —
    unlike the opt-opt case above, there is no same-contract carve-out
    possible for a future partner (a future is never "the same
    contract" as an option). `futures` is also filtered upstream by
    `_classify_mcx_group` to only the SPECIFIC contract A's underlying
    group would actually devolve into — see that function's own
    docstring.
    """
    oq = remaining_opt.get(id(A), 0)
    if A.moneyness == "NTM":
        return None, 0
    opt_long_delta = _exp_opt_long_delta(A.instrument_type, oq)
    best: "Optional[FuturePosition]" = None
    best_fq = 0
    for f in futures:
        fq = remaining_fut.get(id(f), 0)
        if oq == 0 or fq == 0:
            continue
        if not _exp_fut_pair_valid(opt_long_delta, fq):
            continue
        afq = abs(fq)
        if afq > best_fq:
            best = f
            best_fq = afq
    return best, best_fq


def _exp_net_one_pair(
    A: "OptionPosition",
    aq: int,
    best_opt: "Optional[OptionPosition]",
    best_fut: "Optional[FuturePosition]",
    remaining_opt: "dict[int, int]",
    remaining_fut: "dict[int, int]",
) -> int:
    """Net A against its best available partner (opt preferred over fut).

    Mutates remaining_opt / remaining_fut in place.
    Returns the updated aq (remaining signed qty for A).
    """
    if best_opt is not None:
        bq  = remaining_opt[id(best_opt)]
        net = min(abs(aq), abs(bq))
        remaining_opt[id(A)]        = aq - net * (1 if aq > 0 else -1)
        remaining_opt[id(best_opt)] = bq - net * (1 if bq > 0 else -1)
    else:
        # best_fut is guaranteed non-None when best_opt is None (caller checked)
        fq  = remaining_fut[id(best_fut)]  # type: ignore[index]
        net = min(abs(aq), abs(fq))
        remaining_opt[id(A)]       = aq - net * (1 if aq > 0 else -1)
        remaining_fut[id(best_fut)] = fq - net * (1 if fq > 0 else -1)  # type: ignore[index]
    return remaining_opt[id(A)]


_EXP_IN_FLIGHT_STATUSES = ("HELD", "OPEN", "CANCEL_FAILED")


async def _exp_leg_in_flight_this_session(
    account: str, symbol: str, exchange: str, side: str,
) -> bool:
    """True when this exact (account, symbol, exchange, side) already
    has an unresolved `AlgoOrder` from the CURRENT trading session (see
    `expiry_freeze.session_date_of`) — status `HELD` (staged, awaiting
    operator release), `OPEN` (released and actively chasing, or a
    live chase placed directly when hold is off), or `CANCEL_FAILED`
    (a cancel attempt that may still leave the broker order resting).

    2026-11-22 fix (see `close_positions()`'s own docstring for the
    full incident this closes): `record_held_order()`'s own dedup
    guard only matches `status == 'HELD'`, so it alone cannot see a
    row the operator already released and is still chasing. This check
    runs BEFORE that guard, for BOTH the held-staging branch and the
    live-chase branch, so a re-scan (the agent's own 30-min periodic
    re-fire, or this engine's legacy NFO re-scan loop) can never stage
    or place a second order for a leg that already has one outstanding
    this session — regardless of which branch created the first one.

    Deliberately existence-based across EVERY row from today's session
    for this identity, not just the most recent one — an account could
    in principle have more than one row for the same leg this session
    (e.g. a CANCEL_FAILED row followed by a fresh attempt), and any one
    of them being unresolved is reason enough to skip.

    On any lookup failure, fails TOWARD SKIPPING (returns True) — the
    opposite failure direction from the sibling cancel-suppression
    check in `actions_live.py`, and deliberately so: that check guards
    an alert-only re-stage (failing open just means "the operator sees
    the reminder again", harmless), while this one guards whether a
    SECOND REAL ORDER gets placed or staged (failing open there risks
    exactly the double-close incident this function exists to prevent).
    A transient DB error here costs one skipped re-scan tick, which the
    next tick (or the operator's own manual review) recovers from.

    2026-11-23 fix (Part C): a `CANCEL_FAILED` row alone no longer
    blocks forever. `HELD` and `OPEN` rows are unconditionally
    blocking, as before (they are, respectively, still awaiting
    operator action, or actively being worked by a chase — neither
    needs re-verification). When EVERY unresolved row from today's
    session is `CANCEL_FAILED`, each is checked against real broker
    truth via `_exp_verify_cancel_failed_leg` rather than assumed to
    be forever resting: confirmed genuinely gone (cancelled/rejected/
    expired, or filled and reconciled) → no longer blocking; confirmed
    still resting at the broker, or truth unavailable → stays
    blocking, and an alert fires either way (never a silent guess).
    """
    from datetime import timezone
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import AlgoOrder
    from backend.api.algo.expiry_freeze import session_date_of
    try:
        async with async_session() as s:
            rows = (await s.execute(
                select(AlgoOrder).where(
                    AlgoOrder.account == account,
                    AlgoOrder.symbol == symbol,
                    AlgoOrder.exchange == exchange,
                    AlgoOrder.transaction_type == side,
                    AlgoOrder.status.in_(_EXP_IN_FLIGHT_STATUSES),
                )
            )).scalars().all()
        if not rows:
            return False
        now = datetime.now(timezone.utc)
        today_session = session_date_of(now)
        todays_rows = [r for r in rows if session_date_of(r.created_at) == today_session]
        if not todays_rows:
            return False
        unconditionally_blocking = [r for r in todays_rows if r.status != "CANCEL_FAILED"]
        if unconditionally_blocking:
            return True
        # Every one of today's rows is CANCEL_FAILED — verify each
        # against real broker truth rather than assuming it's still
        # resting forever (see `_exp_verify_cancel_failed_leg`).
        for r in todays_rows:
            if await _exp_verify_cancel_failed_leg(r):
                return True
        return False
    except Exception as e:
        logger.warning(
            f"Expiry: in-flight check failed for {symbol} ({account}), "
            f"skipping this leg rather than risking a duplicate order: {e}"
        )
        return True


async def _exp_verify_cancel_failed_leg(row) -> bool:
    """Re-verify ONE `CANCEL_FAILED` `AlgoOrder` row against real broker
    truth. Returns True when the row must STILL be treated as
    in-flight (blocking); False when it is confirmed resolved and safe
    to allow a fresh close to be staged/placed for this leg.

    Three outcomes, matching `get_broker_order_truth`'s own contract —
    never a silent guess:
      - genuinely terminal (cancelled/rejected/expired, or filled) →
        not blocking. A genuine fill is reconciled onto the row via
        `apply_order_truth` (writes the FIFO ledger on FILLED) so the
        row doesn't sit at CANCEL_FAILED forever once the real outcome
        is known.
      - confirmed still resting at the broker → stays blocking. Alerts
        as stale UNLESS a live chase is already working this exact
        `algo_order_id` (`chase.is_chase_active`) — a chase genuinely
        in progress is not "silently unmanaged", so no alert.
      - truth unavailable (lookup failed, or no usable broker id) →
        stays blocking, and always alerts (never silently guessed).
    """
    from backend.api.routes.orders_helpers import get_broker_order_truth, apply_order_truth
    from backend.api.algo.chase import is_chase_active

    try:
        truth = await get_broker_order_truth(row.broker_order_id)
    except Exception as e:
        logger.error(
            f"Expiry: CANCEL_FAILED order {row.id} ({row.symbol}, {row.account}) — "
            f"broker truth lookup failed: {e}. Staying blocked; needs manual review."
        )
        _exp_alert_cancel_failed_stale(row, f"broker truth lookup failed: {e}")
        return True

    if not truth.available:
        logger.error(
            f"Expiry: CANCEL_FAILED order {row.id} ({row.symbol}, {row.account}) — "
            f"broker truth unavailable. Staying blocked; needs manual review."
        )
        _exp_alert_cancel_failed_stale(row, "broker truth unavailable")
        return True

    if truth.algo_status is None:
        # Confirmed still resting at the broker (not yet terminal).
        if not is_chase_active(row.id):
            logger.error(
                f"Expiry: CANCEL_FAILED order {row.id} ({row.symbol}, {row.account}) — "
                f"confirmed still resting at the broker with no active chase. "
                f"Staying blocked; needs manual review."
            )
            _exp_alert_cancel_failed_stale(
                row, "confirmed still resting at the broker with no active chase")
        return True

    # Genuinely terminal — reconcile the row so it no longer sits at
    # CANCEL_FAILED forever, then it's safe (no longer in-flight).
    try:
        await apply_order_truth(row.id, truth)
    except Exception as e:
        logger.warning(f"Expiry: reconcile of CANCEL_FAILED order {row.id} failed: {e}")
    return False


# In-process dedup for the alert below — keyed by AlgoOrder.id, same
# convention as `background.py:_AUTO_RELEASE_FAILED_ALERTED`. A stale
# CANCEL_FAILED leg is re-checked on every re-scan; without this it
# would page the operator again every ~30 minutes for the same row.
_CANCEL_FAILED_ALERTED: set[int] = set()


def _exp_alert_cancel_failed_stale(row, reason: str) -> None:
    """Action-required alert for a CANCEL_FAILED leg that could not be
    confirmed resolved — never a silent `logger.error` alone, which
    only reaches the generic ERROR_AGENT catch-all (Telegram only, with
    its own repeat-gate). `bypass_gates=True` so a closed-market gate
    never suppresses a genuine "this leg may still need manual
    attention" warning on expiry day. One alert per row for the process
    lifetime (ids are never reused)."""
    if row.id in _CANCEL_FAILED_ALERTED:
        return
    _CANCEL_FAILED_ALERTED.add(row.id)
    try:
        from backend.shared.helpers.alert_utils import send_order_failure_alert
        send_order_failure_alert(
            account=row.account, symbol=row.symbol, exchange=row.exchange,
            side=row.transaction_type, qty=int(row.quantity), mode="live",
            source="expiry-cancel-failed-reverify",
            error=(
                f"CANCEL_FAILED order {row.id} could not be confirmed resolved "
                f"at settlement: {reason}. It may still need a manual close "
                f"before expiry."
            ),
            bypass_gates=True,
        )
    except Exception as e:
        logger.warning(f"Expiry: CANCEL_FAILED alert itself failed for order {row.id}: {e}")


class ExpiryEngine:
    def __init__(self, on_event: Callable | None = None, agent_id: "int | None" = None):
        self.state = ExpiryState()
        self.on_event = on_event
        # The firing agent's row id (expiry-day-*-itm-auto-close), when known
        # — threaded into every chase_order()/record_held_order() call this
        # engine makes so failure/hold alerts correctly label "Agent"/
        # "Agent Bracket" instead of defaulting to "Manual".
        self._agent_id = agent_id
        # Settings live in DB now (algo.*); YAML `algo:` block is the
        # boot-time fallback. Re-read on every engine construction so a
        # tweak via /admin/settings takes effect on the next chase run
        # without a service restart.
        from backend.shared.helpers.settings import get_int, get_float
        self._algo_cfg = config.get("algo", {})
        self._ntm_buffer = get_float(
            "algo.expiry_ntm_buffer_pct",
            float(self._algo_cfg.get("expiry_ntm_buffer_pct", 2.0)))
        self._start_offset_h = get_float(
            "algo.expiry_start_offset_hours",
            float(self._algo_cfg.get("expiry_start_offset_hours", 2)))
        self._rescan_min = get_int(
            "algo.expiry_rescan_minutes",
            int(self._algo_cfg.get("expiry_rescan_minutes", 30)))
        self._chase_cfg = ChaseConfig(
            interval_seconds=get_int(
                "algo.chase_interval_seconds",
                int(self._algo_cfg.get("chase_interval_seconds", 20))),
            aggression_step=get_float(
                "algo.aggression_step",
                float(self._algo_cfg.get("aggression_step", 0.10))),
            max_attempts=get_int(
                "algo.max_attempts",
                int(self._algo_cfg.get("max_attempts", 20))),
        )
        self._instruments_cache: dict = {}  # exchange → list of instruments

    def _emit(self, event_type: str, detail: dict = None):
        if self.on_event:
            try:
                self.on_event(event_type, detail or {})
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Instrument cache (loaded once per day per exchange)
    # ------------------------------------------------------------------

    def _load_instruments(self, exchange: str) -> list:
        """Load instruments for an exchange. Cached for the day.

        Routes through the Broker ABC so the call hops to conn_service
        when RAMBOQ_USE_CONN_SERVICE=1 — the main API holds no Kite
        session in that mode."""
        if exchange in self._instruments_cache:
            return self._instruments_cache[exchange]

        from backend.brokers.registry import all_brokers
        # Pick any loaded account — instruments are the same broker-side.
        brokers = all_brokers()
        if not brokers:
            return []
        instruments = brokers[0].instruments(exchange)
        self._instruments_cache[exchange] = instruments
        logger.info(f"Expiry: loaded {len(instruments)} instruments for {exchange}")
        return instruments

    def _get_instrument_info(self, exchange: str, tradingsymbol: str) -> dict | None:
        """Find instrument details by tradingsymbol."""
        instruments = self._load_instruments(exchange)
        for inst in instruments:
            if inst["tradingsymbol"] == tradingsymbol:
                return inst
        return None

    # ------------------------------------------------------------------
    # Position scanning
    # ------------------------------------------------------------------

    def _fetch_future_positions(self) -> list[FuturePosition]:
        """Same shape as _fetch_option_positions but filters to FUT
        legs. Used by the MCX netting pass as delta-offset partners
        for ITM commodity options."""
        raw_dfs = broker_apis.fetch_positions()
        out: list[FuturePosition] = []
        for df in raw_dfs:
            if df.empty:
                continue
            for _, row in df.iterrows():
                exchange = row.get("exchange", "")
                symbol   = row.get("tradingsymbol", "")
                qty      = int(row.get("quantity", 0))
                if qty == 0:
                    continue
                inst_exchange = "NFO" if exchange in ("NSE", "NFO") else "MCX"
                inst = self._get_instrument_info(inst_exchange, symbol)
                if not inst:
                    continue
                if inst.get("instrument_type", "") != "FUT":
                    continue
                out.append(FuturePosition(
                    account=row.get("account", ""),
                    tradingsymbol=symbol,
                    underlying=inst.get("name", ""),
                    quantity=qty,
                    product=row.get("product", "NRML"),
                ))
        return out

    def _compute_theta(self, pos: OptionPosition, *, ref: "datetime | None" = None) -> float:
        """Per-share daily theta via Black-Scholes. Uses a flat IV of
        0.15 (commodity options near expiry — IV barely matters for
        theta when the option is deep ITM/OTM and T → 0). Returns 0
        on degenerate inputs (no spot, no strike, expired). Safe to
        call for every position; failures fall back to 0.

        `ref` (2026-11 condition-resolver re-base): `days_to_expiry`
        defaults to the real wall clock when `ref` is None — fine for
        every live `scan_positions()` caller (a real broker scan always
        happens "now"), but a condition resolver runs against
        `ctx.now`, which a sim/replay/test context can set to an
        arbitrary historical instant. Passing `ref=ctx.now` through
        keeps theta (and therefore the greedy netting priority it
        drives) anchored to the SAME clock the rest of the condition
        evaluation uses, instead of silently reading the live clock
        mid-test. Every existing `scan_positions()` call site omits
        `ref`, so its behaviour is unchanged."""
        try:
            from backend.api.algo.derivatives import (
                greeks, greeks_76, days_to_expiry, DEFAULT_RISK_FREE,
                mcx_non_agri_close_time,
            )
            if pos.underlying_ltp <= 0 or pos.strike <= 0:
                return 0.0
            is_mcx = pos.exchange == "MCX"
            if is_mcx:
                # DST-aware: 23:30 IST in-DST, 23:55 IST in the US-winter
                # window — see mcx_non_agri_close_time()'s own docstring
                # and CLAUDE.md's "MCX DST-aware non-agri close time".
                # Was previously hardcoded (23, 30) year-round, which
                # silently zeroed theta (T<=0 at 23:31) up to 24 minutes
                # before MCX's real winter close — theta drives
                # `_net_commodity_group`'s greedy netting priority, so a
                # stale T=0 could pick the wrong leg to leave open.
                _close_t = mcx_non_agri_close_time(pos.expiry)
                close_time = (_close_t.hour, _close_t.minute)
            else:
                close_time = (15, 30)
            d = float(days_to_expiry(pos.expiry, ref=ref, close_time=close_time))
            T_years = d / 365.25
            if T_years <= 0:
                return 0.0
            sigma = 0.15
            # MCX commodity options: underlying_ltp is the matching futures
            # contract price, not a cash spot — price via Black-76.
            g_fn = greeks_76 if is_mcx else greeks
            g = g_fn(pos.underlying_ltp, pos.strike, T_years,
                     DEFAULT_RISK_FREE, sigma, pos.instrument_type)
            return float(g.get("theta", 0.0))
        except Exception:
            return 0.0

    def _net_commodity_group(
        self,
        options: list[OptionPosition],
        futures: list[FuturePosition],
    ) -> None:
        """4-rule greedy theta-priority netting + futures offset.

        Mutates each option's `residual_qty` field in place. Rules:
          1. Long CE  + Short CE  (same opt type, opposite sign)
          2. Long PE  + Short PE
          3. Long CE  + Long PE   (both receive at settlement, no
                                   close while spot stays in band)
          4. Short CE + Short PE  (locked-in payment)
        Plus futures-as-delta-offset:
          5. Long CE  + Short FUT (Long CE delta +1 vs Short FUT -1)
          6. Long PE  + Long FUT
          7. Short CE + Long FUT
          8. Short PE + Short FUT

        Greedy: at each step, walk options by |theta| DESC, find the
        valid partner (option or future) with highest |theta| for
        options or largest |qty| for futures, net by min(|qty|).
        Mirrors the frontend Close-tab algorithm so the agent and the
        operator UI agree on what stays in the close list.
        """
        if not options:
            return

        # remaining_opt / remaining_fut tracks unmatched signed qty.
        remaining_opt: dict[int, int] = {id(o): o.quantity for o in options}
        remaining_fut: dict[int, int] = {id(f): f.quantity for f in futures}
        opts_sorted = sorted(options, key=lambda o: abs(o.theta or 0.0), reverse=True)

        for A in opts_sorted:
            aq = remaining_opt.get(id(A), 0)
            while aq != 0:
                best_opt, _ = _best_opt_partner(A, opts_sorted, remaining_opt)
                best_fut, _ = _best_fut_partner(A, futures, remaining_opt, remaining_fut)
                if not best_opt and not best_fut:
                    break
                aq = _exp_net_one_pair(
                    A, aq, best_opt, best_fut,
                    remaining_opt, remaining_fut,
                )

        for o in options:
            o.residual_qty = remaining_opt.get(id(o), 0)

    def _fetch_option_positions(self) -> list[OptionPosition]:
        """Fetch all option positions across all accounts with instrument metadata."""
        import pandas as pd

        raw_dfs = broker_apis.fetch_positions()
        all_positions = []

        for df in raw_dfs:
            if df.empty:
                continue
            for _, row in df.iterrows():
                exchange = row.get("exchange", "")
                symbol = row.get("tradingsymbol", "")
                qty = int(row.get("quantity", 0))

                if qty == 0:
                    continue

                # Look up instrument to get expiry, strike, type
                inst_exchange = "NFO" if exchange in ("NSE", "NFO") else "MCX"
                inst = self._get_instrument_info(inst_exchange, symbol)
                if not inst:
                    continue

                inst_type = inst.get("instrument_type", "")
                if inst_type not in ("CE", "PE"):
                    continue  # not an option

                all_positions.append(OptionPosition(
                    account=row.get("account", ""),
                    tradingsymbol=symbol,
                    exchange=inst_exchange,
                    instrument_type=inst_type,
                    underlying=inst.get("name", ""),
                    strike=float(inst.get("strike", 0)),
                    expiry=inst.get("expiry"),
                    quantity=qty,
                    product=row.get("product", "NRML"),
                ))

        return all_positions

    def _classify_moneyness(self, pos: OptionPosition) -> str:
        """Classify option as ITM, ATM, NTM, or OTM based on underlying LTP."""
        if pos.underlying_ltp <= 0:
            return "UNKNOWN"

        if pos.instrument_type == "CE":
            diff_pct = (pos.underlying_ltp - pos.strike) / pos.strike * 100
        else:  # PE
            diff_pct = (pos.strike - pos.underlying_ltp) / pos.strike * 100

        if diff_pct > self._ntm_buffer:
            return "ITM"
        elif diff_pct > 0:
            return "NTM"  # near the money — within buffer
        elif abs(diff_pct) < 0.5:
            return "ATM"
        else:
            return "OTM"

    def _mcx_front_month_symbol(self, root: str) -> str | None:
        """Resolve a bare MCX commodity root (e.g. 'GOLD') to its current
        front-month FUT tradingsymbol (e.g. 'GOLD26OCTFUT').

        2026-10-29 fix (audit finding): Kite's instrument master 'name'
        field is the bare underlying name, never a quotable symbol on
        its own — f"MCX:{root}" (e.g. "MCX:GOLD") is NOT a valid Kite
        quote key. This silently made every MCX underlying_ltp lookup
        fail, so every MCX expiring leg classified as moneyness=UNKNOWN
        and was never flagged for closing — the exact bug class
        `options_helpers.py:_resolve_commodity_spot()` already guards
        against elsewhere in this codebase (it quotes a RESOLVED
        contract symbol, never the bare name). Reuses this engine's own
        already-loaded, synchronous instrument cache (self._load_
        instruments) rather than the async symbol_resolver.py helpers,
        since this whole call chain (scan_positions -> _fetch_
        underlying_ltps) is sync. "Front month" here is simply the
        nearest FUT contract for this root whose expiry hasn't yet
        passed (today's own expiry counts, since the contract is still
        tradeable through its own expiry day) — this is a spot-price
        lookup for moneyness classification, not a rollover/settlement
        decision, so the stricter is_mcx_future_still_current() freeze
        window doesn't apply here.
        """
        today = timestamp_indian().date()
        candidates = []
        for inst in self._load_instruments("MCX"):
            if inst.get("instrument_type") != "FUT":
                continue
            if (inst.get("name") or "").upper() != (root or "").upper():
                continue
            exp = inst.get("expiry")
            if not exp or exp < today:
                continue
            candidates.append((exp, inst.get("tradingsymbol")))
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        return candidates[0][1]

    def _fetch_underlying_ltps(self, positions: list[OptionPosition]) -> dict:
        """Fetch LTPs for all unique underlyings.

        Routes through the Broker ABC so the call hops to conn_service
        when RAMBOQ_USE_CONN_SERVICE=1."""
        from backend.brokers.registry import all_brokers
        brokers = all_brokers()
        if not brokers:
            return {}
        broker = brokers[0]

        # Map underlying name to its index/futures symbol for LTP.
        # For equity indices: use Kite's canonical key (NSE:NIFTY 50,
        # NSE:NIFTY BANK, etc.) via _NSE_INDEX_QUOTE_KEYS — bare
        # "NSE:NIFTY" is an invalid Kite key and returns nothing. Plain
        # stock underlyings quote directly under their own symbol
        # ("NSE:RELIANCE"), so the f"NSE:{p.underlying}" fallback is
        # correct for those.
        # For commodities: resolve the real front-month FUT symbol first
        # (see _mcx_front_month_symbol's own docstring for why the bare
        # name alone is not a valid Kite key) — tracked via
        # symbol_to_root so the result can be mapped back to the
        # underlying root the rest of this engine keys on.
        symbols = set()
        symbol_to_root: dict[str, str] = {}
        unresolved_mcx_roots: set[str] = set()
        for p in positions:
            if p.exchange == "NFO":
                kite_key = _NSE_INDEX_QUOTE_KEYS.get(p.underlying, f"NSE:{p.underlying}")
                symbols.add(kite_key)
            else:
                front = self._mcx_front_month_symbol(p.underlying)
                if not front:
                    unresolved_mcx_roots.add(p.underlying)
                    continue
                symbols.add(f"MCX:{front}")
                symbol_to_root[front] = p.underlying

        if unresolved_mcx_roots:
            logger.error(
                f"Expiry: no current MCX front-month future found for "
                f"underlying(s) {sorted(unresolved_mcx_roots)} — LTP lookup "
                f"skipped, moneyness will read UNKNOWN for these legs"
            )

        if not symbols:
            return {}

        try:
            data = broker.ltp(list(symbols))
            ltps: dict[str, float] = {}
            for kite_k, val in data.items():
                suffix = kite_k.split(":", 1)[1] if ":" in kite_k else kite_k
                underlying = symbol_to_root.get(suffix) or _KITE_KEY_TO_UNDERLYING.get(suffix, suffix)
                ltps[underlying] = val.get("last_price", 0.0)
            return ltps
        except Exception as e:
            logger.error(f"Expiry: LTP fetch failed: {e}")
            return {}

    def _classify_nfo_positions(self, expiring: "list[OptionPosition]") -> None:
        """Flag NFO ITM/NTM positions for closing (no netting exception)."""
        for p in expiring:
            if p.exchange == "NFO" and p.moneyness in ("ITM", "NTM"):
                p.needs_close = True
                p.close_reason = f"Equity {p.moneyness} — must close before expiry"

    def _classify_mcx_group(
        self,
        acct: str,
        underlying: str,
        group: "list[OptionPosition]",
        all_futs: "list[FuturePosition]",
    ) -> None:
        """Net one MCX (account, underlying, expiry) group and flag residual.

        2026-11-23 fix (Part C): `futs` is now ALSO filtered to the
        SPECIFIC contract each option in `group` would actually devolve
        into at settlement (`_future_matches_option_devolvement`), not
        merely "same account + same underlying". A future at a
        DIFFERENT contract month is a genuinely separate position on
        the exchange's books — it does not automatically offset the
        devolved position an expiring option turns into, and must never
        be accepted as a netting partner. Every member of `group`
        shares the same (account, underlying, expiry) key by
        construction (`_classify_expiring_positions`'s own grouping),
        so in the common case they all resolve the same target; the
        per-option `any(...)` check below is still correct when they
        don't (e.g. an unparseable symbol resolves no target at all and
        contributes nothing — fails closed, never "any FUT").
        """
        futs = [
            f for f in all_futs
            if f.account == acct and f.underlying == underlying
            and any(_future_matches_option_devolvement(p.tradingsymbol, f.tradingsymbol)
                    for p in group)
        ]
        self._net_commodity_group(group, futs)
        for p in group:
            if p.residual_qty != 0:
                p.needs_close = True
                p.close_reason = (
                    f"MCX unhedged {p.moneyness} after 4-rule netting "
                    f"(residual qty {p.residual_qty:+d}; "
                    f"theta={p.theta:.3f})"
                )
            else:
                logger.info(
                    f"Expiry: MCX {p.tradingsymbol} ({acct}) fully netted, "
                    f"no close needed."
                )

    def _classify_expiring_positions(
        self,
        expiring: list[OptionPosition],
        all_futs: list[FuturePosition],
    ) -> None:
        """Mark each expiring position with needs_close and close_reason.

        NFO: all ITM + NTM are flagged unconditionally (no netting exception).
        MCX: apply 4-rule + futures greedy theta-priority netting per
             (account, underlying, expiry) group; only non-zero residual qty
             is flagged for closing.  Mutates positions in place.
        """
        self._classify_nfo_positions(expiring)

        # Commodity (MCX) — 4-rule + futures greedy theta netting per group.
        mcx_in_money = [
            p for p in expiring
            if p.exchange == "MCX" and p.moneyness in ("ITM", "NTM")
        ]
        groups: dict[tuple[str, str, date], list[OptionPosition]] = {}
        for p in mcx_in_money:
            groups.setdefault((p.account, p.underlying, p.expiry), []).append(p)

        for (acct, underlying, _expiry), group in groups.items():
            self._classify_mcx_group(acct, underlying, group, all_futs)

    def classify_prepared(
        self,
        expiring: list[OptionPosition],
        all_futs: list[FuturePosition],
        *,
        ref: "datetime | None" = None,
    ) -> None:
        """Pure, no-I/O classification step — SSOT shared by
        `scan_positions()` (real broker-fetched data) and
        `classify_from_context()` (the `ctx.position_rows`/
        `ctx.spot_prices` adapter used by the `expiry-mcx-risk-alert`/
        `expiry-nfo-risk-alert` condition resolvers, see
        `grammar_tokens/scope_positions_expiring_today_mcx_unhedged.py`).

        Takes `expiring` with `underlying_ltp` ALREADY populated (the
        two callers source that differently — a live `broker.ltp()`
        call here, `ctx.spot_prices` there) and computes moneyness,
        theta, and the real 4-rule MCX netting + NFO ITM/NTM
        classification in place. Mutates `expiring`'s own
        `moneyness`/`theta`/`residual_qty`/`needs_close`/`close_reason`
        fields — never a second, independently-reimplemented copy of
        this math, so the condition path and the alert/auto-close
        action path can never disagree about the same position.
        """
        for p in expiring:
            p.moneyness = self._classify_moneyness(p)
            p.theta = self._compute_theta(p, ref=ref)
            p.residual_qty = p.quantity   # default before netting
        self._classify_expiring_positions(expiring, all_futs)

    def scan_positions(self) -> list[OptionPosition]:
        """
        Scan all option positions and identify those expiring today
        that need closing.

        Rules (mirrors the frontend /admin/options Close tab):
          • Equity (NFO): every ITM + NTM contract closes — Zerodha
            doesn't net-settle pairs; STT on ITM longs and physical
            settlement is the trap.
          • Commodity (MCX): per (account, underlying, expiry) group,
            apply the 4-rule greedy theta-priority netting (long-CE ↔
            short-CE, long-PE ↔ short-PE, long-CE ↔ long-PE, short-CE
            ↔ short-PE) PLUS same-underlying FUT positions as delta-
            offset partners (Long-CE ↔ Short-FUT, Long-PE ↔ Long-FUT,
            etc.). Residual (non-zero) qty after netting is what
            needs closing.
        """
        today = timestamp_indian().date()
        self.state.status = "scanning"
        self._emit("scan_start", {"date": str(today)})

        # Fetch options + futures (futures used as MCX offset partners).
        all_opts = self._fetch_option_positions()
        all_futs = self._fetch_future_positions()
        logger.info(f"Expiry: found {len(all_opts)} option, {len(all_futs)} future positions")

        # Filter to today's expiry. `all_futs` itself is NOT expiry-
        # filtered here — `_classify_mcx_group` narrows it per group to
        # only the specific contract each option would actually devolve
        # into (`_future_matches_option_devolvement`), not merely "same
        # underlying" (2026-11-23 fix, Part C — reverses the prior
        # "even if the future itself expires on a different date"
        # design, which risked netting against an unrelated contract).
        expiring = [p for p in all_opts if p.expiry == today]
        logger.info(f"Expiry: {len(expiring)} positions expiring today ({today})")

        if not expiring:
            self.state.status = "idle"
            self._emit("scan_complete", {"count": 0})
            return []

        # Fetch underlying LTPs.
        ltps = self._fetch_underlying_ltps(expiring)

        # Resolve underlying LTP, then run the shared pure classifier
        # (moneyness + theta + 4-rule netting) — see classify_prepared()'s
        # own docstring for why this is factored out as the SSOT.
        for p in expiring:
            p.underlying_ltp = ltps.get(p.underlying, 0)
        self.classify_prepared(expiring, all_futs)

        to_close = [p for p in expiring if p.needs_close]
        self.state.positions = expiring
        self.state.last_scan = timestamp_display()

        self._emit("scan_complete", {
            "total_expiring": len(expiring),
            "to_close": len(to_close),
            "positions": [
                {"account": p.account, "symbol": p.tradingsymbol,
                 "exchange": p.exchange, "qty": p.quantity,
                 "moneyness": p.moneyness, "strike": p.strike,
                 "underlying_ltp": p.underlying_ltp}
                for p in to_close
            ],
        })

        logger.info(f"Expiry: {len(to_close)} positions to close")
        return to_close

    # ------------------------------------------------------------------
    # Closing orchestration
    # ------------------------------------------------------------------

    def _close_time_utc_for_exchange(self, exchange: str,
                                     equity_close_utc: datetime,
                                     mcx_close_utc: datetime) -> datetime:
        """Pick the already-resolved (DST-aware, UTC) session-close
        instant for `exchange` — MCX/NCO use the commodity close,
        everything else (NFO/NSE/BFO) uses the equity close. Mirrors
        `_fetch_future_positions`'s own two-bucket exchange convention."""
        return mcx_close_utc if (exchange or "").upper() in ("MCX", "NCO") else equity_close_utc

    async def close_positions(self, positions: list[OptionPosition],
                              release_policy: "dict | None" = None):
        """Chase-close all flagged positions concurrently. Uses the
        RESIDUAL qty so an MCX leg that paired against a partner only
        gets its un-netted remainder closed. residual_qty defaults to
        the gross quantity in scan_positions, so NFO equity rows
        (which skip the netting pass) close their full position.

        2026-11-22 fix — a re-fire (expiry-day-itm-auto-close's own
        30-min periodic re-scan, or this same engine's legacy NFO
        re-scan loop) used to be "safe to repeat" ONLY while the
        leg's HELD row was still HELD — `record_held_order()`'s dedup
        guard matches on `status == 'HELD'` alone. Once the operator
        releases a hold, `release_held_order()` sets the row to
        `status='OPEN'` and starts chasing it. If that chase is still
        working (a real risk for an illiquid deep-ITM leg on expiry
        day) when the next re-scan runs, the position is still open,
        `needs_close` is still True, the row is no longer HELD, and
        the dedup guard found nothing — so a SECOND HELD row got
        staged for the exact same leg. Releasing that one too placed a
        second real close and flipped the position. `_exp_leg_in_
        flight_this_session()` closes this for both the held-staging
        branch below AND the live-chase branch, so neither path can
        ever act on a leg that already has an unresolved order this
        session.

        2026-11-23 fix (Part C — "it staged unreleasable holds with no
        agent_id"): a HELD expiry-close row with no `agent_id` can
        never be auto-released (`_task_agent_close_auto_release`'s
        kill-switch check needs a real agent to check). This is a
        defense-in-depth refusal: the one production caller
        (`_action_live_expiry_auto_close`) always passes a real
        `agent_id` at construction time, and the legacy caller that
        used to construct this engine with none
        (`_task_expiry_check`) has been retired — but any FUTURE
        caller that forgets to pass `agent_id` is refused here rather
        than silently reproducing the exact bug.

        `release_policy` (Part C) — an optional DSL-expressible hold
        auto-release policy dict (see `order_hold._resolve_hold_auto_
        release_at`'s own docstring for its two shapes), computed by
        the caller. When given, every HELD row staged by this call
        gets a concrete `auto_release_at` deadline resolved against
        this engine's own DST-aware session-close time for the leg's
        exchange (`_parse_segment_close_times`); a hold whose deadline
        can't be resolved (unknown close time, or a malformed policy)
        is refused — the same severity as the agent_id-None refusal
        above — never silently staged with `auto_release_at=None`.
        `release_policy=None` (the default, and the legacy `run()`
        path's implicit value) preserves the exact prior behaviour:
        `auto_release_at` is omitted and the hold never auto-releases.
        """
        self.state.status = "closing"

        # Only resolve today's session-close times when a release_policy
        # is actually in play — the legacy no-policy call path (run()'s
        # own callers, and any test that doesn't pass one) must not pay
        # for, or depend on, this extra config read. Checked via
        # `is not None`, not truthiness: `release_policy=None` is the
        # true "no policy at all" sentinel (preserves the exact prior
        # behaviour); an explicitly-passed-but-EMPTY dict (`{}`) is a
        # caller BUG, not "no policy" — it must still reach the
        # auto_release_at-is-None refusal below, never silently fall
        # back to the legacy no-deadline behaviour.
        equity_close_utc = mcx_close_utc = None
        if release_policy is not None:
            today = timestamp_indian().date()
            from zoneinfo import ZoneInfo
            equity_close_t, mcx_close_t = self._parse_segment_close_times(today)
            _ist = ZoneInfo("Asia/Kolkata")
            equity_close_utc = datetime.combine(today, equity_close_t, tzinfo=_ist).astimezone(timezone.utc)
            mcx_close_utc = datetime.combine(today, mcx_close_t, tzinfo=_ist).astimezone(timezone.utc)

        tasks = []
        for pos in positions:
            effective = pos.residual_qty
            if effective == 0:
                continue   # fully netted; nothing to close
            # Determine transaction type: close long → SELL, close short → BUY
            txn = "SELL" if effective > 0 else "BUY"
            qty = abs(effective)
            if await _exp_leg_in_flight_this_session(pos.account, pos.tradingsymbol,
                                                      pos.exchange, txn):
                logger.info(
                    f"Expiry: {pos.tradingsymbol} ({pos.account}) already has an "
                    f"unresolved order this session (HELD/OPEN/CANCEL_FAILED) — "
                    f"skipping re-stage/re-chase"
                )
                continue
            if expiry_close_held():
                if self._agent_id is None:
                    logger.error(
                        f"Expiry: refusing to stage HELD close for {pos.tradingsymbol} "
                        f"({pos.account}) — this ExpiryEngine has no agent_id, and a "
                        f"HELD row with no agent_id can never be auto-released. "
                        f"Skipping this leg; it needs manual attention."
                    )
                    continue
                auto_release_at = None
                price_policy = "CHASE_MED"
                if release_policy is not None:
                    held_at = datetime.now(timezone.utc)
                    close_time = self._close_time_utc_for_exchange(
                        pos.exchange, equity_close_utc, mcx_close_utc)
                    auto_release_at = _resolve_hold_auto_release_at(
                        release_policy, held_at, close_time, held_at,
                        default_chase_tier="high",
                    )
                    if auto_release_at is None:
                        logger.error(
                            f"Expiry: refusing to stage HELD close for "
                            f"{pos.tradingsymbol} ({pos.account}) — release_policy "
                            f"{release_policy!r} could not resolve an auto-release "
                            f"deadline (close_time={close_time!r}). Staging an "
                            f"unreleasable hold is exactly the bug this guard "
                            f"exists to prevent. Skipping this leg; it needs "
                            f"manual attention."
                        )
                        continue
                    tier = (release_policy.get("floor_tier") or "high").upper()
                    price_policy = f"CHASE_{tier}"
                await record_held_order(
                    HoldCategory.EXPIRY_CLOSE,
                    account=pos.account, symbol=pos.tradingsymbol,
                    exchange=pos.exchange, side=txn, qty=qty,
                    product=pos.product, reason="expiry close (held by default)",
                    agent_id=self._agent_id,
                    price_policy=price_policy,
                    auto_release_at=auto_release_at,
                )
                continue

            cfg = ChaseConfig(
                interval_seconds=self._chase_cfg.interval_seconds,
                aggression_step=self._chase_cfg.aggression_step,
                max_attempts=self._chase_cfg.max_attempts,
                exchange=pos.exchange,
                product=pos.product,
                intent="close",
            )

            async def _chase_one(p=pos, t=txn, q=qty, c=cfg):
                result = await chase_order(
                    account=p.account,
                    symbol=p.tradingsymbol,
                    transaction_type=t,
                    quantity=q,
                    cfg=c,
                    on_event=self.on_event,
                    agent_id=self._agent_id,
                )
                if result.status == ChaseStatus.FILLED:
                    self.state.closed.append(result)
                    self.state.total_slippage += result.slippage
                else:
                    self.state.failed.append(result)
                return result

            self.state.pending_chases[pos.tradingsymbol] = ChaseResult(
                account=pos.account, symbol=pos.tradingsymbol,
                transaction_type=txn, quantity=qty,
                status=ChaseStatus.PENDING,
            )
            tasks.append(_chase_one())

        # Run all chases concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                logger.error(f"Expiry: chase exception: {r}")
            elif isinstance(r, ChaseResult):
                # Remove from pending
                self.state.pending_chases.pop(r.symbol, None)

        self.state.status = "done"
        self._emit("close_complete", {
            "closed": len(self.state.closed),
            "failed": len(self.state.failed),
            "total_slippage": self.state.total_slippage,
        })

    # ------------------------------------------------------------------
    # Main run loop (called by background task on expiry days)
    # ------------------------------------------------------------------

    def _parse_segment_close_times(
        self, today: "date | None" = None,
    ) -> "tuple[dtime, dtime]":
        """Read equity_close and mcx_close from config. Returns (equity_close, mcx_close).

        Routes each segment's `sessions` list through
        `resolve_dst_aware_sessions()` — the documented SSOT for the YAML
        `market_segments` config (see CLAUDE.md's "MCX DST-aware non-agri
        close time") — before reading its `end` time, so the commodity
        segment's resolved close tracks the real US-DST-dependent MCX
        non-agri close (23:30 in-DST, 23:55 in the US-winter window)
        instead of the static legacy `hours_end` literal, which always
        reads 23:30 regardless of season. Equity/NSE is untouched by the
        resolver (it is keyed on `holiday_exchange == "MCX"`), so its
        close is still read the old way. Falls back to the legacy
        `hours_end` field — same default as before this fix — whenever a
        segment carries no `sessions` list at all (not the current
        production YAML shape, but a config this function must not
        mishandle).

        `today` defaults to the real IST date — tests/callers may pass an
        explicit date to pin the DST-season resolution.
        """
        from backend.shared.helpers.date_time_utils import resolve_dst_aware_sessions
        if today is None:
            today = timestamp_indian().date()
        segments = config.get("market_segments", {})
        equity_close = dtime(15, 30)
        mcx_close    = dtime(23, 30)
        for name, seg in segments.items():
            resolved_sessions = resolve_dst_aware_sessions(seg, today)
            end_str = (
                resolved_sessions[-1].get("end")
                if resolved_sessions
                else seg.get("hours_end", "15:30")
            )
            h, m = map(int, end_str.split(":"))
            if name == "equity":
                equity_close = dtime(h, m)
            elif name == "commodity":
                mcx_close = dtime(h, m)
        return equity_close, mcx_close

    async def _run_nfo_close(
        self, today: "date", equity_close: "dtime", nfo_positions: "list[OptionPosition]",
    ) -> None:
        """Wait until NFO close window, then chase-close all NFO positions."""
        nfo_start = (datetime.combine(today, equity_close) -
                     timedelta(hours=self._start_offset_h)).time()
        now_t = timestamp_indian().time()
        if now_t < nfo_start:
            wait = (datetime.combine(today, nfo_start) -
                    datetime.combine(today, now_t)).total_seconds()
            logger.info(
                f"Expiry: waiting {wait/60:.0f} min until NFO close phase "
                f"starts at {nfo_start}"
            )
            self._emit("waiting", {"exchange": "NFO", "start_time": str(nfo_start)})
            await asyncio.sleep(max(wait, 0))
        logger.info(f"Expiry: starting NFO close for {len(nfo_positions)} positions")
        await self.close_positions(nfo_positions)

    async def _run_mcx_close(
        self, today: "date", mcx_close: "dtime",
    ) -> None:
        """Wait until MCX close window, re-scan, then chase-close MCX positions."""
        mcx_start = (datetime.combine(today, mcx_close) -
                     timedelta(hours=self._start_offset_h)).time()
        now_t = timestamp_indian().time()
        if now_t < mcx_start:
            wait = (datetime.combine(today, mcx_start) -
                    datetime.combine(today, now_t)).total_seconds()
            logger.info(
                f"Expiry: waiting {wait/60:.0f} min until MCX close phase "
                f"starts at {mcx_start}"
            )
            self._emit("waiting", {"exchange": "MCX", "start_time": str(mcx_start)})
            await asyncio.sleep(max(wait, 0))
        logger.info("Expiry: re-scanning MCX positions before closing")
        fresh = self.scan_positions()
        mcx_fresh = [p for p in fresh if p.exchange == "MCX" and p.needs_close]
        if mcx_fresh:
            logger.info(f"Expiry: starting MCX close for {len(mcx_fresh)} positions")
            await self.close_positions(mcx_fresh)

    async def run(self):
        """
        Full expiry-day workflow:
        1. Morning scan at 09:15
        2. Wait until T-2h before close
        3. Start closing
        4. Re-scan every _rescan_min minutes for NFO positions that cross ITM intraday
        5. Continue until all closed or 15:25 IST (NFO market close - 5 min buffer)
        """
        now_ist = timestamp_indian()
        today = now_ist.date()
        equity_close, mcx_close = self._parse_segment_close_times(today)

        # Morning scan
        logger.info("Expiry: starting morning scan")
        to_close = self.scan_positions()

        if not to_close:
            logger.info("Expiry: no positions need closing today")
            self._emit("no_positions", {"date": str(today)})
            return

        self._emit("morning_alert", {
            "count": len(to_close),
            "positions": [
                f"{p.account} {p.tradingsymbol} qty={p.quantity} {p.moneyness}"
                for p in to_close
            ],
        })

        nfo_positions = [p for p in to_close if p.exchange == "NFO"]
        mcx_positions = [p for p in to_close if p.exchange == "MCX"]

        if nfo_positions:
            await self._run_nfo_close(today, equity_close, nfo_positions)

        # Periodic re-scan for NFO positions that cross ITM intraday.
        # Runs until 15:25 IST (5-min buffer before NFO close) so any
        # position that moves ITM after the morning scan is still caught.
        _closed_syms: set[str] = {r.symbol for r in self.state.closed}
        _nfo_deadline = now_ist.replace(hour=15, minute=25, second=0, microsecond=0)
        while timestamp_indian() < _nfo_deadline:
            await asyncio.sleep(self._rescan_min * 60)
            _fresh = self.scan_positions()
            _new_itm = [
                p for p in _fresh
                if p.exchange == "NFO"
                and p.moneyness == "ITM"
                and p.tradingsymbol not in _closed_syms
            ]
            if _new_itm:
                logger.info("[EXPIRY] re-scan: %d newly-ITM NFO position(s)", len(_new_itm))
                await self._run_nfo_close(today, equity_close, _new_itm)
                _closed_syms.update(r.symbol for r in self.state.closed)

        if mcx_positions:
            await self._run_mcx_close(today, mcx_close)

        logger.info(
            f"Expiry: complete — closed {len(self.state.closed)}, "
            f"failed {len(self.state.failed)}, "
            f"slippage ₹{self.state.total_slippage:.2f}"
        )
        self._emit("expiry_complete", {
            "closed": len(self.state.closed),
            "failed": len(self.state.failed),
            "total_slippage": self.state.total_slippage,
        })


# ═══════════════════════════════════════════════════════════════════════
#  Context-driven classification adapter (2026-11 classifier re-base)
# ═══════════════════════════════════════════════════════════════════════
#
# `expiry-mcx-risk-alert`/`expiry-nfo-risk-alert` (agent_engine.py) used to
# run their OWN independent classification logic — a crude CE/PE
# signed-quantity sum for MCX, and the generic `is_itm`/`is_future`
# metrics with no quantity filter for NFO — instead of the real 4-rule
# theta-priority netting `expiry-day-preclose-alert` and
# `expiry-day-itm-auto-close` already use via `ExpiryEngine.
# scan_positions()`. Per explicit operator decision, both risk-alert
# scopes are re-based onto the SAME classification `scan_positions()`
# uses, so the two can never disagree about the same position.
#
# `scan_positions()` itself cannot be called directly from a condition
# resolver: it is synchronous and makes real, blocking broker calls
# (fetch_positions/instruments/ltp), and `_cycle_evaluate_agent` (the
# resolver's caller) is itself a plain sync `def` that runs directly on
# the main event loop — a blocking broker call there would stall chase
# polls/postbacks/SSE for its duration, every cycle, for every agent
# that references these scopes. `classify_from_context()` below is the
# alternative: it builds the SAME `OptionPosition`/`FuturePosition`
# shapes `scan_positions()` builds, but sources them from data the
# engine ALREADY populates once per cycle for every other condition
# resolver — `ctx.position_rows` (background.py's `_perf_*` convention)
# and `ctx.spot_prices` (`background.py:_resolve_spot_prices`, which
# already resolves MCX roots to their matching-month future via
# `option_underlying_quote_key`, not the bare-name bug `ExpiryEngine`
# itself works around in `_mcx_front_month_symbol`) — never a second,
# live I/O call. `classify_prepared()` is the one shared pure
# classification step both this adapter and `scan_positions()` call, so
# there is exactly one copy of the netting/moneyness math.

def _opt_from_row(r: dict) -> "OptionPosition | None":
    """Build an `OptionPosition` from one `ctx.position_rows` dict —
    the ctx-side counterpart to `ExpiryEngine._fetch_option_positions`'s
    per-row construction. Returns `None` for a non-option row, an
    unparseable symbol, or a flat (qty==0) position — matching
    `_fetch_option_positions`'s own `if qty == 0: continue` skip, so a
    squared-off leg never re-enters classification from either path
    (closes the audit finding that the old `is_itm`/`is_future`-based
    NFO scope had no quantity filter at all)."""
    sym = r.get("tradingsymbol") or ""
    parsed = _parsed_or_none(sym)
    if not parsed or parsed.get("kind") != "opt":
        return None
    opt_type = parsed.get("opt_type")
    if opt_type not in ("CE", "PE"):
        return None
    try:
        qty = int(float(r.get("quantity", 0) or 0))
    except (TypeError, ValueError):
        return None
    if qty == 0:
        return None
    strike = parsed.get("strike")
    if strike is None:
        return None
    return OptionPosition(
        account=r.get("account") or "",
        tradingsymbol=sym,
        exchange=(r.get("exchange") or "").upper(),
        instrument_type=opt_type,
        underlying=parsed.get("root") or "",
        strike=float(strike),
        expiry=parsed.get("expiry"),
        quantity=qty,
        product=r.get("product") or "NRML",
    )


def _fut_from_row(r: dict) -> "FuturePosition | None":
    """Build a `FuturePosition` from one `ctx.position_rows` dict —
    the ctx-side counterpart to `ExpiryEngine._fetch_future_positions`.
    Returns `None` for a non-future row, an unparseable symbol, or a
    flat (qty==0) position."""
    sym = r.get("tradingsymbol") or ""
    parsed = _parsed_or_none(sym)
    if not parsed or parsed.get("kind") != "fut":
        return None
    try:
        qty = int(float(r.get("quantity", 0) or 0))
    except (TypeError, ValueError):
        return None
    if qty == 0:
        return None
    return FuturePosition(
        account=r.get("account") or "",
        tradingsymbol=sym,
        underlying=parsed.get("root") or "",
        quantity=qty,
        product=r.get("product") or "NRML",
    )


def classify_from_context(
    rows: "list[dict]",
    all_rows: "list[dict]",
    spot_prices: dict,
    *,
    exchange_filter: "str | None" = None,
    now: "datetime | None" = None,
) -> "list[OptionPosition]":
    """Pure, no-I/O adapter — classifies `rows` (option positions) the
    SAME way `ExpiryEngine.scan_positions()` does, sourced entirely
    from already-fetched in-memory context data rather than a live
    broker call. See the module comment above for why this exists.

    `rows` — the near-expiry option rows to classify (typically
    `_scope_positions_expiring_today(ctx)`'s output, pre-filtered to
    one exchange by the caller — see `exchange_filter` below).
    `all_rows` — the FULL, unfiltered `ctx.position_rows`. Futures
    netting partners can sit at ANY expiry, not only rows expiring
    today (mirrors `_fetch_future_positions`, which is never
    itself expiry-filtered) — passing only the near-expiry subset
    here would silently drop a valid futures offset partner.
    `spot_prices` — `ctx.spot_prices`, keyed by parsed root (the same
    convention `_option_moneyness`/`background.py:_resolve_spot_prices`
    already use).
    `exchange_filter` — restrict the OPTION side to one exchange
    ("MCX" or "NFO"); futures partners are never exchange-filtered
    here (a bare root never collides across exchanges in practice).
    `now` — threaded into `classify_prepared(ref=...)` so theta (and
    the netting priority it drives) anchors to `ctx.now`, never the
    live wall clock — see `_compute_theta`'s own `ref` docstring.

    Returns the classified `OptionPosition` list for `rows` (flat/
    unparseable/non-option rows are silently excluded — see
    `_opt_from_row`). `needs_close`/`moneyness`/`residual_qty` are set
    on each returned position exactly as `scan_positions()` would set
    them, GIVEN the same input data. A row with no resolvable spot
    price classifies `moneyness == "UNKNOWN"` and is never flagged
    `needs_close` (see `_classify_moneyness`'s `underlying_ltp <= 0`
    branch) — callers must surface an UNKNOWN leg explicitly rather
    than silently treating it as safe; see
    `scope_positions_expiring_today_mcx_unhedged.py`'s scope resolver,
    which does exactly this.

    Known residual gap (not fixed here — matches `scan_positions()`'s
    own pre-existing behaviour, flagged rather than fixed per the
    2026-11 classifier re-base plan): a LONE expiring future with no
    option leg on the same underlying is never itself flagged. Futures
    are only ever netting PARTNERS in this engine, never a needs_close
    candidate in their own right, despite the risk-alert agents' own
    descriptions promising "...or is a future".
    """
    options = [o for o in (_opt_from_row(r) for r in rows) if o is not None]
    if exchange_filter:
        options = [o for o in options if o.exchange == exchange_filter.upper()]
    futs = [f for f in (_fut_from_row(r) for r in (all_rows or [])) if f is not None]
    for o in options:
        o.underlying_ltp = float(spot_prices.get(o.underlying) or 0.0)
    engine = ExpiryEngine()
    engine.classify_prepared(options, futs, ref=now)
    return options
