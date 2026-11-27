"""Dhan historical trade / ledger backfill into `kite_console_pnl_sync`.

WHY THIS IS A SEPARATE, MUCH SIMPLER SCRIPT THAN
`scripts/kite_console_pnl_sync.py` — Zerodha's sanctioned Kite Connect API
has no historical P&L/trade report endpoint at all, so that script drives
Kite Console's web UI with Playwright. Dhan is different: the installed
`dhanhq` v2 SDK exposes real REST endpoints for both historical trades
(`Statement.get_trade_history(from_date, to_date, page_number)`, paginated)
and the funds ledger (`Statement.ledger_report(from_date, to_date)`) —
confirmed live via direct introspection of the installed SDK
(`dhanhq._statement.Statement`), 2026-11-27. No browser automation, no
Console-login cooloff machinery, no CSV/XLSX parsing is needed here.

TWO-PROCESS ARCHITECTURE — this script does NOT build its own
`DhanConnection`. Unlike Kite Console (a completely separate browser
session with its own cookies, independent of the live REST access token),
Dhan's historical trade/ledger endpoints use the SAME REST access token as
live trading. Building a second, independent `DhanConnection` in this
script's own process would risk invalidating conn_service's live token
(Dhan enforces one active token per source IP — see `DhanConnection`'s own
docstring in `backend/brokers/connections.py`, and the DH-906 rotation-loop
incident CLAUDE.md documents) and would arm the SHARED on-disk login-rate
-limit cooloff for the live trading account on a failed script login — the
exact coupling the Kite Console script's own "Cooloff INDEPENDENCE"
section explicitly rejected for a DIFFERENT (browser) mechanism, and which
would be strictly worse here because there IS only one Dhan login
mechanism, not two independent ones.

Instead, this script calls `Connections().rebuild_from_db()` once at
startup (the same bootstrap every long-lived process does) and then uses
`backend.brokers.registry.get_broker(account)` — the SAME accessor
`backend/api/routes/history.py:backfill_funds` already uses. Under the
prod/dev default (`RAMBOQ_USE_CONN_SERVICE=1`), `get_broker()` returns a
`RemoteBroker` that forwards every call to conn_service over UDS — no
broker session of any kind is ever held in THIS process. Under a
non-cutover (local/test) environment, `rebuild_from_db()` builds the real
`DhanConnection` the normal way (reusing the already-shipped, tested
credential-decrypt + token-cache + cross-process-lock code in
`connections.py` — never a second, independent reimplementation of it).

DEPLOY COUPLING — `DhanBroker.trade_history()` / `.ledger_entries_raw()`
(the two new adapter methods this script calls) live in
`backend/brokers/adapters/dhan.py`, and the conn_service RPC whitelist +
`RemoteBroker` forwarders live in `backend/brokers/service/routes.py` /
`backend/brokers/client/remote_broker.py`. Per this project's own
CLAUDE.md "Things to Avoid" note, `backend/brokers/` changes only take
effect in `ramboq_conn` after a PROD deploy that sets `CONN_TOUCHED` — a
dev-only push does NOT pick these up, because dev and prod share one
conn_service process. Running this script against a stale (pre-deploy)
conn_service will raise a clean `NotFoundException`-derived `BrokerError`
("has no method 'trade_history'") for every account, not a silent no-op.

MASKING — reuses the exact same `register_accounts()` / `mask_account()`
registry the live app uses (`backend/shared/helpers/utils.py`), but
registers the FULL active Dhan roster (never narrowed by `--account`)
before applying any account filter — see `_register_all_active_dhan_masks()`
below for why a filtered registration would silently compute the WRONG
ordinal-disambiguated mask (`D1####` vs `D2####`) for an account whose
first-2-character prefix collides with another active Dhan account's.

Dhan's raw API responses carry the account's own numeric `client_id`
inside each trade/ledger entry (e.g. a `dhanClientId` field — exact key
name UNVERIFIED against a live response, so several plausible spellings
are stripped defensively; see `_dhan_scrub_client_id()`). That raw numeric
id is never allowed into this table's `raw_row` JSONB column unmasked —
same invariant as every other log/alert/storage surface in this codebase
(see CLAUDE.md's account-masking campaign entry).

REPORT TYPES — only `tradebook` and `ledger` are populated here. Dhan has
no endpoint analogous to Kite Console's separate P&L-statement export
(realized P&L per symbol over a date range); it would have to be derived
from the tradebook itself (a FIFO/weighted-average cost calculation), which
is explicitly OUT OF SCOPE for this change — `report_type='pnl'` rows are
never written by this script. A future task could derive them from the
`tradebook` rows this script already writes.

ROW SHAPE — every row this script writes lands in the SAME
`kite_console_pnl_sync` table Kite's own rows use, with `broker='dhan'`
(see that model's own column comment). This script reuses the Kite
script's shared, pure helpers (`chunk_by_calendar_month`,
`classify_instrument`, `build_sync_row`, `ingest_rows`,
`build_ledger_sync_row`, `ingest_ledger_rows`, `validate_ledger_rows`,
`alert_critical`) by IMPORTING them, never copying them — each Dhan raw
response is first mapped into the exact same Kite-CSV-shaped intermediate
dict (`symbol`/`trade_date`/`exchange`/`trade_type`/`quantity`/`price`/
`trade_id`/`order_id` for a trade; `particulars`/`posting_date`/`debit`/
`credit`/`net_balance` for a ledger entry) those shared functions already
expect, so a future schema or dedup-key change made to the Kite script's
own functions applies to Dhan rows for free.

UNVERIFIED AGAINST A LIVE ACCOUNT — no Dhan credentials were available
while building this (same constraint the operator brief itself flagged).
Every place a real-field-name assumption was made is called out in that
function's own docstring, most importantly:
  - `DhanBroker.trade_history()`'s own assumption that the paginated
    historical-trade endpoint returns the SAME per-trade shape as
    `get_trade_book()` (so it reuses `_normalise_trades()` unchanged).
  - `_dhan_ledger_entry_to_kite_shape()`'s guess at the ledger entry's
    free-text description field name (`narration` / `voucherdesc` /
    `vouchernumber`, tried in that order) — the aggregator
    (`_dhan_ledger_aggregate`) never needed this field, so there is no
    existing confirmed-live precedent for it in this codebase.
  - `dhanClientId` as the account-identifier key to scrub before storage
    (several spellings tried defensively — see `_dhan_scrub_client_id()`).
Building and unit-testing this code (with mocks) is this change's
deliverable; a real backfill run against a live Dhan account is a
deliberate follow-up, not attempted here.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.shared.helpers.ramboq_logger import get_logger  # noqa: E402

logger = get_logger(__name__)

# Reused, never copied — see module docstring.
from scripts.kite_console_pnl_sync import (  # noqa: E402
    alert_critical,
    build_ledger_sync_row,
    build_sync_row,
    chunk_by_calendar_month,
    ingest_ledger_rows,
    ingest_rows,
    validate_ledger_rows,
)

# A reasonable, operator-overridable guess (`--start`) at how far back a
# Dhan backfill should reach — NOT verified against the account's actual
# trading history start date, and not verified against any cap Dhan's own
# `get_trade_history` endpoint may impose on the requested range.
_DEFAULT_BACKFILL_START = date(2026, 4, 1)

# Dhan's (already Kite-mapped, via `_EXCHANGE_TO_DHAN`'s reverse) exchange
# string -> this table's own segment vocabulary. Deliberately NOT
# `backend.brokers.adapters.dhan._dhan_exchange_to_segment` /
# `_DHAN_SEGMENT_MAP` — those map to `daily_book`'s vocabulary
# ('equity' / 'commodity', F&O folded into 'equity'), a DIFFERENT
# vocabulary from this table's own ('equity' / 'derivatives' /
# 'commodities', matching `kite_console_pnl_sync.segment`'s existing
# values and `scripts/kite_console_pnl_sync.py:_SEGMENT_FOR_EXCHANGE`).
_SEGMENT_FOR_DHAN_EXCHANGE: dict[str, str] = {
    "NSE": "equity", "BSE": "equity",
    "NFO": "derivatives", "BFO": "derivatives",
    "CDS": "derivatives", "BCD": "derivatives",
    "MCX": "commodities",  # NOT "derivatives" — matches the Kite script's
                            # own _SEGMENT_FOR_EXCHANGE["MCX"] == "commodities"
}


def _segment_for_exchange(exchange: "str | None") -> str:
    """Falls back to 'equity' for an unrecognised exchange string — the
    same conservative default `backend/brokers/adapters/dhan.py:
    _dhan_exchange_to_segment()` uses for its own (different) vocabulary."""
    return _SEGMENT_FOR_DHAN_EXCHANGE.get((exchange or "").strip().upper(), "equity")


# Plausible spellings for the account-identifier field Dhan embeds inside
# every trade/ledger entry. UNVERIFIED — see module docstring. Stripped
# defensively from every level of a raw payload before it reaches
# `raw_row` (JSONB), never stored unmasked.
_DHAN_CLIENT_ID_KEYS = ("dhanClientId", "dhan_client_id", "clientId", "client_id")


def _dhan_scrub_client_id(d: "dict | None") -> dict:
    """Pure — returns a shallow copy of `d` with every known
    account-identifier key removed. Never raises on a non-dict input
    (returns `{}`), since the caller always wants SOMETHING storable."""
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items() if k not in _DHAN_CLIENT_ID_KEYS}


def _dhan_trade_date_from_timestamp(ts: "str | None") -> "str | None":
    """Extracts a bare `YYYY-MM-DD` date string from a Dhan trade's
    `exchange_timestamp` field (as normalised by
    `backend.brokers.adapters.dhan._normalise_trades`), which may carry a
    full timestamp (`"YYYY-MM-DD HH:MM:SS"` or ISO-with-`T`) rather than a
    bare date. Returns `None` on anything unparseable — the caller
    (`_dhan_trade_to_kite_shape`) leaves `trade_date` empty rather than
    guessing, and the shared `validate_tradebook_rows`-style date check
    downstream (via `build_sync_row` -> `_parse_date_cell`) then simply
    can't parse it either, which is the same safe degrade `_parse_date_cell`
    already applies to any other unparseable date string."""
    if not ts:
        return None
    s = str(ts).strip()
    if len(s) < 10:
        return None
    candidate = s[:10]
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
        return candidate
    except ValueError:
        return None


def _dhan_trade_exchange(t: dict) -> "str | None":
    """Resolves the Kite-style exchange string ('NSE'/'BSE'/'NFO'/'BFO'/
    'MCX') for one normalised Dhan trade dict.

    Prefers deriving it from the ORIGINAL raw entry's `exchangeSegment`
    field (e.g. `"NSE_FNO"`), via the REVERSE of `backend.brokers.
    adapters.dhan._EXCHANGE_TO_DHAN` — this is the field Dhan's API
    actually encodes the equity-vs-F&O distinction in; the base
    `exchange` field alone (NSE/BSE/MCX) cannot distinguish an NFO trade
    from a plain NSE cash trade. Falls back to `_normalise_trades()`'s
    own already-set `exchange` key only when `exchangeSegment` is absent
    or doesn't map to anything known (e.g. a currency-derivatives
    segment this reverse map doesn't cover) — never raises either way."""
    try:
        from backend.brokers.adapters.dhan import _EXCHANGE_TO_DHAN
        reverse = {v: k for k, v in _EXCHANGE_TO_DHAN.items()}
        raw = t.get("_raw") or {}
        seg = str(raw.get("exchangeSegment") or "").strip().upper()
        if seg in reverse:
            return reverse[seg]
    except Exception:
        pass
    return t.get("exchange")


def dhan_trade_to_kite_shape(t: dict) -> dict:
    """Maps one ALREADY-NORMALISED Dhan trade dict (the shape
    `backend.brokers.adapters.dhan._normalise_trades()` / `DhanBroker.
    trade_history()` returns: `trade_id`, `order_id`, `tradingsymbol`,
    `exchange`, `transaction_type`, `quantity`, `average_price`,
    `exchange_timestamp`, `_raw`) into the Kite-CSV-shaped dict
    `scripts.kite_console_pnl_sync.build_sync_row()` expects (`symbol`,
    `trade_date`, `exchange`, `trade_type`, `quantity`, `price`,
    `trade_id`, `order_id`).

    Deliberately reuses `_normalise_trades()`'s own output UNCHANGED for
    every field EXCEPT `exchange` (see `_dhan_trade_exchange()`) rather
    than reading Dhan's raw per-trade fields a second time — that
    function already applies the MCX lot->contract conversion (via
    `_DHAN_LOT_BY_SECURITY`) and the Dhan->Kite tradingsymbol translation
    (`_dhan_to_kite_symbol`), and duplicating either here would risk the
    two copies drifting.

    The original Dhan trade object (`t["_raw"]`, scrubbed of any
    account-identifier field) is preserved under `_dhan_raw` so the
    stored `raw_row` carries the same key shape a Kite tradebook row's
    `raw_row` does NOT need to carry (Kite's own CSV row IS the raw row)
    — a future read-side consumer that wants the untranslated Dhan fields
    can still find them, scrubbed, at one predictable key regardless of
    which broker wrote the row."""
    return {
        "symbol": t.get("tradingsymbol"),
        "exchange": _dhan_trade_exchange(t),
        "trade_type": t.get("transaction_type"),
        "quantity": t.get("quantity"),
        "price": t.get("average_price"),
        "trade_date": _dhan_trade_date_from_timestamp(t.get("exchange_timestamp")),
        "trade_id": t.get("trade_id"),
        "order_id": t.get("order_id"),
        "_dhan_raw": _dhan_scrub_client_id(t.get("_raw")),
    }


def _dhan_ledger_particulars(e: dict) -> str:
    """Best-effort free-text description for one raw Dhan ledger entry.
    UNVERIFIED field name — see module docstring. Tries, in order,
    `narration` / `voucherdesc` / `vouchernumber`, then falls back to a
    generated, never-empty string (`validate_ledger_rows()` flags an
    empty `particulars` as malformed, so this must never return "")."""
    for key in ("narration", "voucherdesc", "vouchernumber"):
        val = (e.get(key) or "").strip() if isinstance(e.get(key), str) else e.get(key)
        if val:
            return str(val)
    return f"Dhan ledger entry ({e.get('voucherdate') or 'unknown date'})"


def dhan_ledger_entry_to_kite_shape(e: dict) -> dict:
    """Maps one RAW Dhan ledger entry (as returned by `DhanBroker.
    ledger_entries_raw()` — the unaggregated sibling of `funds_ledger()`,
    fields `voucherdate`/`exchange`/`debit`/`credit`/`runbal` confirmed
    via `_dhan_ledger_aggregate()`'s own existing, tested parsing) into
    the Kite-ledger-CSV-shaped dict `build_ledger_sync_row()` /
    `validate_ledger_rows()` expect (`particulars`, `posting_date`,
    `debit`, `credit`, `net_balance`).

    `posting_date` is reused via `backend.brokers.adapters.dhan.
    _parse_dhan_date()` (the SAME parser `_dhan_ledger_aggregate()` already
    relies on for this exact field — handles DD/MM/YYYY, ISO date, and ISO
    datetime), not a second, independent date parser — then re-rendered
    as a bare ISO string so the Kite-shaped `_parse_date_cell()` (which
    only understands string dates, not `datetime.date` objects) can parse
    it back downstream."""
    from backend.brokers.adapters.dhan import _parse_dhan_date

    posting = _parse_dhan_date(e.get("voucherdate"))
    return {
        "particulars": _dhan_ledger_particulars(e),
        "posting_date": posting.isoformat() if posting else None,
        "debit": e.get("debit"),
        "credit": e.get("credit"),
        "net_balance": e.get("runbal"),
        "_dhan_raw": _dhan_scrub_client_id(e),
    }


# ---------------------------------------------------------------------------
# Active-account loading + mask registration
# ---------------------------------------------------------------------------

async def load_active_dhan_accounts(account_filter: "list[str] | None" = None) -> list:
    """Active Dhan `BrokerAccount` rows. Mirrors `scripts.
    kite_console_pnl_sync.load_active_kite_accounts()` exactly, filtered
    to `broker_id == 'dhan'` instead of Kite's `('zerodha_kite', 'kite')`
    pair (confirmed via `backend/brokers/connections.py`'s own
    `rebuild_from_db()` — every live Dhan-branch check there compares
    against the single literal `'dhan'`)."""
    from backend.api.database import shared_async_session
    from backend.api.models import BrokerAccount
    from sqlalchemy import select

    async with shared_async_session() as s:
        rows = (await s.execute(
            select(BrokerAccount).where(
                BrokerAccount.is_active.is_(True),
                BrokerAccount.broker_id == "dhan",
            )
        )).scalars().all()
    if account_filter:
        wanted = set(account_filter)
        rows = [r for r in rows if r.account in wanted]
    return list(rows)


def _register_all_active_dhan_masks(all_rows: list) -> None:
    """Registers the FULL active Dhan roster's mask — see module
    docstring for why this must never be called with an `--account`
    -filtered subset. `register_accounts()`'s ordinal-disambiguation
    (`D1####` / `D2####`) is decided purely by sorting accounts that
    collide on their own first-2-character prefix; since every active
    Dhan account shares the 'DH' prefix by construction, a filtered call
    (e.g. `--account DH6847` alone) would see NO collision at all and
    mint the WRONG mask (`DH####`, not `D2####`) — silently diverging
    from the mask the live app, and the read side's own Dhan rows, use
    for that exact account."""
    from backend.shared.helpers.utils import register_accounts

    client_ids = {r.account: r.client_id for r in all_rows if r.client_id}
    register_accounts([r.account for r in all_rows], client_ids=client_ids)


# ---------------------------------------------------------------------------
# Per-account orchestration
# ---------------------------------------------------------------------------

@dataclass
class DhanSyncOutcome:
    account: str
    ok: bool
    trades_ingested: int = 0
    ledger_ingested: int = 0
    reason: str = ""
    segment_errors: "list[str]" = field(default_factory=list)


async def _sync_account_trades(
    broker: Any, masked_account: str, start: date, end: date,
) -> "tuple[int, list[str]]":
    """Pulls + ingests tradebook rows for one account across every
    calendar-month chunk of `[start, end]`. Returns
    `(rows_ingested, [error strings])` — a per-chunk failure is recorded
    and the loop continues with the next chunk (same "one bad chunk never
    aborts the whole account" posture as the Kite script's own
    `_scrape_segment_chunks`), rather than aborting the whole backfill on
    one transient API hiccup."""
    from backend.api.database import async_session

    total = 0
    errors: "list[str]" = []
    for c_start, c_end in chunk_by_calendar_month(start, end):
        try:
            trades = await asyncio.to_thread(
                broker.trade_history, c_start.isoformat(), c_end.isoformat(),
            )
        except Exception as e:
            errors.append(f"tradebook {c_start}..{c_end}: {e}")
            continue
        if not trades:
            continue
        by_segment: "dict[str, list[dict]]" = {}
        for t in trades:
            mapped = dhan_trade_to_kite_shape(t)
            by_segment.setdefault(_segment_for_exchange(mapped.get("exchange")), []).append(mapped)
        for segment, rows in by_segment.items():
            try:
                async with async_session() as s:
                    n = await ingest_rows(
                        s, masked_account, segment, "tradebook", rows,
                        pull_start=c_start, pull_end=c_end, broker="dhan",
                    )
                total += n
            except Exception as e:
                errors.append(f"tradebook ingest {segment} {c_start}..{c_end}: {e}")
    return total, errors


async def _sync_account_ledger(
    broker: Any, masked_account: str, start: date, end: date,
) -> "tuple[int, list[str]]":
    """Pulls + ingests ledger rows for one account across every
    calendar-month chunk of `[start, end]`. Each chunk's mapped rows are
    validated via the shared `validate_ledger_rows()` BEFORE ingestion —
    a row dated outside the chunk's own range raises `ReportValidationError`
    there, which this function records as a chunk-scoped error rather than
    letting it reach `ingest_ledger_rows()`'s DELETE-then-insert (a
    mis-dated row landing outside the DELETE's own window would never be
    cleaned up by a later re-pull, silently accumulating)."""
    from backend.api.database import async_session
    from scripts.kite_console_pnl_sync import ReportValidationError

    total = 0
    errors: "list[str]" = []
    for c_start, c_end in chunk_by_calendar_month(start, end):
        try:
            entries = await asyncio.to_thread(
                broker.ledger_entries_raw, c_start.isoformat(), c_end.isoformat(),
            )
        except Exception as e:
            errors.append(f"ledger {c_start}..{c_end}: {e}")
            continue
        rows = [dhan_ledger_entry_to_kite_shape(e) for e in (entries or [])]
        try:
            validate_ledger_rows(rows, start=c_start, end=c_end, allow_empty=True)
        except ReportValidationError as e:
            errors.append(f"ledger validation {c_start}..{c_end}: {e}")
            continue
        try:
            async with async_session() as s:
                n = await ingest_ledger_rows(
                    s, masked_account, rows,
                    pull_start=c_start, pull_end=c_end, broker="dhan",
                )
            total += n
        except Exception as e:
            errors.append(f"ledger ingest {c_start}..{c_end}: {e}")
    return total, errors


async def sync_account(row, *, start: date, end: date, ledger_only: bool = False,
                        trades_only: bool = False) -> DhanSyncOutcome:
    """Orchestrates one account's backfill: resolve its `Broker` via
    `registry.get_broker()` (never a bespoke connection — see module
    docstring), then pull+ingest tradebook and ledger for `[start, end]`.

    Best-effort per account: an exception resolving the broker itself
    (e.g. the account isn't loaded in conn_service) fails that ONE
    account and is reported via the returned `DhanSyncOutcome`, mirroring
    the Kite script's own `main()` per-account try/except — one
    misconfigured account must never abort the whole batch."""
    from backend.brokers.registry import get_broker
    from backend.shared.helpers.utils import mask_account

    masked = mask_account(row.account)
    try:
        broker = get_broker(row.account)
    except Exception as e:
        return DhanSyncOutcome(account=masked, ok=False, reason=f"get_broker failed: {e}")

    # Best-effort MCX lot-size cache warm-up (see module docstring's
    # "UNVERIFIED" section) — `trade_history()`'s own
    # `_normalise_trades()` call falls back to a raw (possibly lots, not
    # contracts) quantity with only a WARNING log when a security's lot
    # size isn't cached. Warming the instruments cache first makes that
    # fallback far less likely to fire for a fresh conn_service process,
    # but does not eliminate the risk entirely (e.g. an expired contract
    # genuinely absent from the current scrip master) — never treated as
    # fatal if it fails.
    try:
        await asyncio.to_thread(broker.instruments, "MCX")
    except Exception as e:
        logger.debug("dhan_pnl_sync: MCX instruments warm-up failed for %s: %s", masked, e)

    trades_n = 0
    ledger_n = 0
    errors: "list[str]" = []
    if not ledger_only:
        trades_n, trade_errors = await _sync_account_trades(broker, masked, start, end)
        errors.extend(trade_errors)
    if not trades_only:
        ledger_n, ledger_errors = await _sync_account_ledger(broker, masked, start, end)
        errors.extend(ledger_errors)

    return DhanSyncOutcome(
        account=masked, ok=not errors,
        trades_ingested=trades_n, ledger_ingested=ledger_n,
        segment_errors=errors,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--mode", choices=("backfill",), default="backfill",
        help="Only 'backfill' exists today — an ongoing incremental-sync "
             "mode (mirroring the Kite script's own --mode sync) is a "
             "deliberate follow-up, not built here. Kept as a flag "
             "(rather than omitted) for CLI parity with "
             "scripts/kite_console_pnl_sync.py and so a future 'sync' "
             "mode slots in without a breaking CLI change.",
    )
    p.add_argument(
        "--account", action="append", default=None,
        help="Restrict to one or more account codes (repeatable). "
             "Default: every active Dhan account. Narrows which "
             "accounts are SYNCED — mask registration still always sees "
             "the full active roster first (see module docstring).",
    )
    p.add_argument(
        "--start", type=lambda s: date.fromisoformat(s), default=None,
        help=f"Backfill start date (YYYY-MM-DD). Default: "
             f"{_DEFAULT_BACKFILL_START.isoformat()} — an unverified "
             f"guess, not the account's confirmed trading-history start.",
    )
    p.add_argument(
        "--end", type=lambda s: date.fromisoformat(s), default=None,
        help="Backfill end date (YYYY-MM-DD). Default: today (IST).",
    )
    p.add_argument(
        "--ledger-only", action="store_true",
        help="Pull only the ledger, skipping the tradebook entirely.",
    )
    p.add_argument(
        "--trades-only", action="store_true",
        help="Pull only the tradebook, skipping the ledger entirely.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Resolve accounts and the date range, print the plan, and "
             "exit without calling any broker or touching the database.",
    )
    return p


async def main(argv: "list[str] | None" = None) -> int:
    import json
    from backend.brokers.connections import Connections
    from backend.shared.helpers.date_time_utils import timestamp_indian

    args = _build_arg_parser().parse_args(argv)
    if args.ledger_only and args.trades_only:
        logger.error("dhan_pnl_sync: --ledger-only and --trades-only are mutually exclusive")
        return 1

    all_rows = await load_active_dhan_accounts()
    _register_all_active_dhan_masks(all_rows)

    wanted = set(args.account) if args.account else None
    target_rows = [r for r in all_rows if wanted is None or r.account in wanted]

    today = timestamp_indian().date()
    start = args.start or _DEFAULT_BACKFILL_START
    end = args.end or today

    if args.dry_run:
        from backend.shared.helpers.utils import mask_account
        print(json.dumps({
            "mode": args.mode,
            "accounts": [mask_account(r.account) for r in target_rows],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "ledger_only": args.ledger_only,
            "trades_only": args.trades_only,
        }, indent=2))
        return 0

    if not target_rows:
        logger.warning("dhan_pnl_sync: no active Dhan accounts to sync")
        return 0

    # Populate `Connections`/the broker registry so `get_broker()` can
    # resolve — see module docstring. Best-effort: a failure here still
    # lets `get_broker()` raise its own clear error per account below,
    # rather than this script crashing outright before any account is
    # even attempted.
    try:
        await Connections().rebuild_from_db()
    except Exception as e:
        logger.warning("dhan_pnl_sync: Connections().rebuild_from_db() failed: %s", e)

    exit_code = 0
    for row in target_rows:
        try:
            outcome = await sync_account(
                row, start=start, end=end,
                ledger_only=args.ledger_only, trades_only=args.trades_only,
            )
        except Exception as e:  # one account's crash never aborts the batch
            from backend.shared.helpers.utils import mask_account
            masked = mask_account(row.account)
            logger.exception("dhan_pnl_sync: unexpected failure for %s", masked)
            await alert_critical(
                "Dhan P&L sync crashed", masked, detail=str(e),
                source_name="Dhan P&L sync",
                alert_event="dhan_pnl_sync_failed",
            )
            exit_code = 1
            continue
        if not outcome.ok:
            exit_code = 1
            logger.warning(
                "dhan_pnl_sync: %s completed with errors: %s",
                outcome.account, outcome.segment_errors,
            )
            await alert_critical(
                "Dhan P&L sync had chunk failures", outcome.account,
                detail="; ".join(outcome.segment_errors[:5]),
                source_name="Dhan P&L sync",
                alert_event="dhan_pnl_sync_failed",
                action_required=False,
            )
        else:
            logger.info(
                "dhan_pnl_sync: %s ok — %d trade row(s), %d ledger row(s)",
                outcome.account, outcome.trades_ingested, outcome.ledger_ingested,
            )

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
