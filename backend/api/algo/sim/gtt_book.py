"""
Sim GTT book — fabricated GTT lifecycle inside the simulator.

When the OrderTicket template flow fires an order in sim mode, the TP /
SL legs of the chosen template translate to GTT orders. Those don't
hit a real broker — they hit this book instead. Every tick, the
SimDriver advances prices, then asks the book "did any trigger cross?"
On a crossing, the leg's order is dispatched through the existing
PaperTradeEngine (same engine that handles operator-placed paper
orders) and rides the chase loop until it fills.

Three responsibilities:

1.  **Lifecycle** — `active → triggered → (cancelled?)` state machine
    per GTT, with placed_at / triggered_at timestamps. The
    `cancelled` and `expired` terminal states cover operator cancel
    and validity-day expiry.

2.  **Price crossing** — given (last_ltp_seen, current_ltp), detect
    which trigger_values crossed in either direction. Crossing
    matches the live Kite GTT semantics: the trigger fires when LTP
    reaches OR passes the trigger_value from either side. Last-seen
    is updated only AFTER the crossing check so an active GTT placed
    on tick N evaluates against tick N+1's price diff.

3.  **OCO emulation** — Groww has no native OCO. When the operator
    requests trigger_type='two-leg' on a Groww-bound account, the
    orchestrator places TWO single GTTs with paired `pair_with`
    references. On either leg's trigger this book auto-cancels the
    sibling so only one of TP / SL ever executes. Kite + Dhan land
    as a single two-leg row and behave identically.

Industry analogue: NinjaTrader Sim Engine's order book, but
GTT-specific. The lifecycle model + price-crossing logic mirrors how
Kite's server-side GTT actually behaves at fill time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── State machine ────────────────────────────────────────────────────
#
# active     → trigger crossed → triggered
# active     → operator cancel  → cancelled
# active     → validity expiry  → expired
# triggered  → terminal
# cancelled  → terminal
# expired    → terminal
GTT_STATUS_ACTIVE     = "active"
GTT_STATUS_TRIGGERED  = "triggered"
GTT_STATUS_CANCELLED  = "cancelled"
GTT_STATUS_EXPIRED    = "expired"

_TERMINAL_STATES = {GTT_STATUS_TRIGGERED, GTT_STATUS_CANCELLED, GTT_STATUS_EXPIRED}


@dataclass
class SimGtt:
    """One simulated GTT.

    `last_seen_ltp` is updated every tick AFTER the crossing check —
    the trigger fires when the value moves from one side of the
    trigger_value to the other. `triggered_leg_index` records which
    leg of a two-leg GTT actually fired (always 0 for single GTTs)
    so the snapshot can show the operator "TP fired" vs "SL fired"
    without ambiguity.
    """

    gtt_id:           str
    account:          str
    tradingsymbol:    str
    exchange:         str
    trigger_type:     str                  # "single" | "two-leg"
    trigger_values:   list[float]
    orders:           list[dict]           # one leg dict per trigger_value
    last_price:       float                # ref price at place time
    last_seen_ltp:    float                # rolling per-tick
    status:           str = GTT_STATUS_ACTIVE
    pair_with:        Optional[str] = None  # sibling GTT id for OCO emulation
    template_id:      Optional[int] = None
    parent_order_id:  Optional[int] = None  # AlgoOrder row that placed it
    tag:              Optional[str] = None
    triggered_leg_index: Optional[int] = None
    placed_at:        datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    triggered_at:     Optional[datetime] = None
    cancelled_at:     Optional[datetime] = None

    def is_active(self) -> bool:
        return self.status == GTT_STATUS_ACTIVE

    def to_dict(self) -> dict:
        return {
            "gtt_id":              self.gtt_id,
            "account":             self.account,
            "tradingsymbol":       self.tradingsymbol,
            "exchange":            self.exchange,
            "trigger_type":        self.trigger_type,
            "trigger_values":      list(self.trigger_values),
            "orders":              list(self.orders),
            "last_price":          self.last_price,
            "last_seen_ltp":       self.last_seen_ltp,
            "status":              self.status,
            "pair_with":           self.pair_with,
            "template_id":         self.template_id,
            "parent_order_id":     self.parent_order_id,
            "tag":                 self.tag,
            "triggered_leg_index": self.triggered_leg_index,
            "placed_at":           self.placed_at.isoformat() if self.placed_at else None,
            "triggered_at":        self.triggered_at.isoformat() if self.triggered_at else None,
            "cancelled_at":        self.cancelled_at.isoformat() if self.cancelled_at else None,
        }


# ── Crossing detection ──────────────────────────────────────────────

def _crossed(last_seen: float, current: float, trigger: float) -> bool:
    """Return True when `current` reached or passed `trigger` from
    either side of `last_seen`. Equality on either end counts as a
    crossing so a GTT placed exactly AT the trigger fires immediately
    on its first tick evaluation."""
    if last_seen <= trigger <= current:
        return True
    if last_seen >= trigger >= current:
        return True
    return False


class SimGttBook:
    """In-memory GTT book for one SimDriver run. Reset on every sim
    start() so a fresh run never inherits GTTs from a previous run."""

    def __init__(
        self,
        *,
        on_trigger: Callable[[SimGtt, int], None],
        on_record:  Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        """
        on_trigger(gtt, leg_index) — invoked when a trigger crosses.
        Caller is responsible for dispatching the matched leg order
        through PaperTradeEngine and handling sibling cancellation
        for OCO emulation (the book just FLAGS the trigger; it
        doesn't drive the chase engine itself).

        on_record(kind, payload) — optional hook for the recording
        layer (Phase 2b). Called for every state transition so a
        deterministic event log can be assembled at sim end. None when
        the recorder isn't attached.
        """
        self._book: dict[str, SimGtt] = {}
        self._next_id: int = 1
        self._on_trigger = on_trigger
        self._on_record  = on_record

    # ── Lifecycle ───────────────────────────────────────────────────

    def place(
        self,
        *,
        account: str,
        tradingsymbol: str,
        exchange: str,
        trigger_type: str,
        trigger_values: list[float],
        orders: list[dict],
        last_price: float,
        pair_with: Optional[str] = None,
        template_id: Optional[int] = None,
        parent_order_id: Optional[int] = None,
        tag: Optional[str] = None,
    ) -> SimGtt:
        if trigger_type not in ("single", "two-leg"):
            raise ValueError(f"trigger_type must be 'single' or 'two-leg', got {trigger_type!r}")
        if len(trigger_values) != len(orders):
            raise ValueError(
                f"trigger_values + orders must align; got {len(trigger_values)} triggers "
                f"and {len(orders)} orders"
            )
        if trigger_type == "single" and len(trigger_values) != 1:
            raise ValueError(f"single-trigger GTT requires exactly 1 trigger_value")
        if trigger_type == "two-leg" and len(trigger_values) != 2:
            raise ValueError(f"two-leg GTT requires exactly 2 trigger_values")

        gtt_id = f"sim-gtt-{self._next_id:06d}"
        self._next_id += 1
        gtt = SimGtt(
            gtt_id=gtt_id,
            account=account,
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            trigger_type=trigger_type,
            trigger_values=list(trigger_values),
            orders=list(orders),
            last_price=last_price,
            last_seen_ltp=last_price,
            pair_with=pair_with,
            template_id=template_id,
            parent_order_id=parent_order_id,
            tag=tag,
        )
        self._book[gtt_id] = gtt
        self._record("gtt_placed", gtt.to_dict())
        logger.info(
            f"[SIM-GTT] placed {gtt_id} {tradingsymbol} {trigger_type} "
            f"triggers={trigger_values} acct={account}"
            + (f" pair={pair_with}" if pair_with else "")
        )
        return gtt

    def cancel(self, gtt_id: str, reason: str = "operator") -> Optional[SimGtt]:
        gtt = self._book.get(gtt_id)
        if not gtt or not gtt.is_active():
            return None
        gtt.status = GTT_STATUS_CANCELLED
        gtt.cancelled_at = datetime.now(timezone.utc)
        self._record("gtt_cancelled", {"gtt_id": gtt_id, "reason": reason})
        logger.info(f"[SIM-GTT] cancelled {gtt_id} reason={reason}")
        return gtt

    def get(self, gtt_id: str) -> Optional[SimGtt]:
        return self._book.get(gtt_id)

    def all_active(self) -> list[SimGtt]:
        return [g for g in self._book.values() if g.is_active()]

    def all_(self) -> list[SimGtt]:
        return list(self._book.values())

    def reset(self) -> None:
        """Clear the entire book. Called by SimDriver.start() so a
        new run begins with a clean state — fresh ids, no orphan
        GTTs from a prior scenario."""
        self._book.clear()
        self._next_id = 1

    # ── Per-tick check ──────────────────────────────────────────────

    def check_triggers(self, ltp_by_symbol: dict[tuple[str, str], float]) -> list[SimGtt]:
        """Iterate every active GTT; fire any that crossed.

        `ltp_by_symbol` is keyed (account, tradingsymbol) → ltp.
        Returns the list of GTTs that fired this tick (in placement
        order). Sibling cancellation is handled here so the caller's
        on_trigger hook sees a clean book.

        Caller's on_trigger callback is invoked AFTER status is set
        to 'triggered' but BEFORE sibling cancellation — gives the
        PaperTradeEngine dispatch a chance to fail loudly (e.g. price
        gap, no broker connection) without leaving an orphan sibling.
        Failed dispatches don't roll back the status: the GTT did
        cross. Operator can re-fire from the UI if needed.
        """
        fired: list[SimGtt] = []
        for gtt in list(self._book.values()):
            if not gtt.is_active():
                continue
            ltp = ltp_by_symbol.get((gtt.account, gtt.tradingsymbol))
            if ltp is None:
                # Symbol gone (filled / closed elsewhere in the sim)
                # OR not in our scope. Mark the GTT expired so the
                # book doesn't keep checking a symbol that won't
                # reappear. This matches Kite's behaviour where a
                # GTT against a settled symbol auto-expires.
                if gtt.tradingsymbol not in {k[1] for k in ltp_by_symbol}:
                    gtt.status = GTT_STATUS_EXPIRED
                    gtt.cancelled_at = datetime.now(timezone.utc)
                    self._record("gtt_expired", {
                        "gtt_id": gtt.gtt_id, "reason": "symbol_gone",
                    })
                continue

            # Walk every trigger; first to cross fires the matching
            # leg. For OCO (two-leg) only ONE leg fires; in single
            # there's just one trigger anyway.
            triggered_idx: Optional[int] = None
            for idx, trigger in enumerate(gtt.trigger_values):
                if _crossed(gtt.last_seen_ltp, ltp, trigger):
                    triggered_idx = idx
                    break

            # Update last_seen AFTER the crossing check so a GTT
            # placed at last_seen=trigger doesn't pre-arm its own
            # crossing on tick 0 (the place() call sets last_seen=
            # last_price; only the NEXT tick reveals a real diff).
            gtt.last_seen_ltp = ltp

            if triggered_idx is None:
                continue

            # Fire.
            gtt.status = GTT_STATUS_TRIGGERED
            gtt.triggered_at = datetime.now(timezone.utc)
            gtt.triggered_leg_index = triggered_idx
            self._record("gtt_triggered", {
                "gtt_id":              gtt.gtt_id,
                "leg_index":           triggered_idx,
                "trigger_value":       gtt.trigger_values[triggered_idx],
                "crossed_at":          ltp,
                "matched_order":       gtt.orders[triggered_idx],
            })
            logger.info(
                f"[SIM-GTT] triggered {gtt.gtt_id} leg={triggered_idx} "
                f"trigger={gtt.trigger_values[triggered_idx]} crossed_at={ltp}"
            )
            try:
                self._on_trigger(gtt, triggered_idx)
            except Exception as e:
                logger.error(f"[SIM-GTT] on_trigger dispatch failed for {gtt.gtt_id}: {e}")
            fired.append(gtt)

            # OCO emulation: cancel the sibling on the operator-paired
            # leg. Two-leg GTTs are atomic at the broker so they don't
            # need this — only Groww-emulated singles with pair_with set.
            if gtt.pair_with:
                sibling = self._book.get(gtt.pair_with)
                if sibling and sibling.is_active():
                    sibling.status = GTT_STATUS_CANCELLED
                    sibling.cancelled_at = datetime.now(timezone.utc)
                    self._record("gtt_cancelled", {
                        "gtt_id": sibling.gtt_id,
                        "reason": "oco_sibling_triggered",
                        "sibling_triggered": gtt.gtt_id,
                    })
                    logger.info(
                        f"[SIM-GTT] cancelled sibling {sibling.gtt_id} (OCO pair) "
                        f"after {gtt.gtt_id} fired"
                    )

        return fired

    # ── Snapshot ────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Status snapshot for /api/simulator/status."""
        all_rows = [g.to_dict() for g in self._book.values()]
        return {
            "active_count":    sum(1 for g in self._book.values() if g.is_active()),
            "triggered_count": sum(1 for g in self._book.values()
                                   if g.status == GTT_STATUS_TRIGGERED),
            "cancelled_count": sum(1 for g in self._book.values()
                                   if g.status == GTT_STATUS_CANCELLED),
            "expired_count":   sum(1 for g in self._book.values()
                                   if g.status == GTT_STATUS_EXPIRED),
            "gtts":            all_rows,
        }

    # ── Recording bridge ────────────────────────────────────────────

    def _record(self, kind: str, payload: dict) -> None:
        if self._on_record is not None:
            try:
                self._on_record(kind, payload)
            except Exception as e:
                logger.warning(f"[SIM-GTT] _on_record({kind}) failed: {e}")
