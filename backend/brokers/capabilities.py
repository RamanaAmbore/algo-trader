"""
Broker capability matrix — declares what each vendor can do natively, so
the rest of the codebase can plan template fan-out + GTT placement
without hard-coding broker-specific branches.

Industry analogue: OpenAlgo / AlgoBulls / Tradetron all carry a similar
per-broker capability dataclass. Strategy authors write once; the
adapter handles divergence (e.g. Groww OCO emulation via two singles).

Add a new vendor: append a CapabilitySet constant + register in
`CAPS_BY_BROKER_ID`. Add a new capability: append a field to the
dataclass + bump every CapabilitySet. The matrix is read on every
template-driven action via `capabilities_for(account)`, so accuracy
here directly shapes operator UX (we surface the gap inline, e.g. "wing
will be placed atomically (Dhan) vs ~100ms (Kite)" on OrderTicket).
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class BrokerCapabilities:
    """What a single broker can do natively.

    Fields are intentionally specific and operator-meaningful — we
    avoid abstract "supports advanced orders" buckets because the
    UX needs to surface concrete capabilities ("OCO works here, not
    there"). Booleans + small enums keep the matrix readable.
    """

    # ── Identity ─────────────────────────────────────────────────────
    broker_id: str
    display_name: str

    # ── GTT / trigger orders ─────────────────────────────────────────
    gtt_single: bool            # Place a single-trigger GTT (TP-only or SL-only)
    gtt_oco: bool               # Place an OCO GTT (TP + SL bundled, broker side)
    gtt_modify: bool            # Modify an existing GTT in place (vs cancel+replace)
    gtt_cap_per_account: int    # Max active GTTs per account; 0 = unknown
    gtt_validity_days: int      # Default GTT validity in days
    # Whether GTT/OCO orders are supported on MCX/NCO commodity exchanges.
    # Kite covers MCX natively; Dhan Forever does NOT cover MCX (place_gtt
    # raises NotImplementedError for MCX/NCO); Groww similarly limited.
    # Pre-fix the absence of this field meant a Dhan account with an MCX
    # option + template silently failed at attach time post-fill — too
    # late. Now read by the cap-warning chip at submit time.
    # Hotfix 2026-06-20 — removed the `= False` default. Python 3.13
    # dataclasses enforce no-default-followed-by-required strictly; the
    # default here blocked import of bracket_order / cover_order / etc.
    # Every broker constant (KITE/DHAN/GROWW) already sets this
    # explicitly, so removing the default is behaviorally a no-op.
    gtt_supports_mcx: bool

    # ── Bracket / Cover ──────────────────────────────────────────────
    bracket_order: bool         # Entry + SL + Target as one ticket (Dhan only today)
    cover_order: bool           # Entry + mandatory SL (margin-efficient intraday)

    # ── Atomic basket ────────────────────────────────────────────────
    # True when N legs can be submitted as a single broker-atomic call
    # (no naked window between legs). Kite has no API basket; Dhan does;
    # Groww doesn't.
    atomic_basket: bool

    # ── Order correlation ────────────────────────────────────────────
    # Lets us bind a broker_order_id back to our template_id without a
    # sidecar table. Kite "tag", Dhan "correlation_id". Absent → sidecar.
    order_tag: bool

    # ── Margin preview ───────────────────────────────────────────────
    # True when the broker exposes a pre-submit margin calculator we can
    # show inline on OrderTicket (e.g. "₹X required for this credit
    # spread"). Kite + Dhan ✓; Groww limited.
    margin_preview: bool

    # ── Postback / event delivery ────────────────────────────────────
    # 'reliable' — broker calls our webhook on every state change (Kite)
    # 'partial'  — webhook delivers some events, polling needed for the rest
    # 'poll_only' — no webhook, our sync loop is the only source of truth
    postback_gtt: str

    # ── Rate limits ──────────────────────────────────────────────────
    rate_limit_orders_sec: int  # Orders / second this broker accepts


# ── Per-broker definitions ────────────────────────────────────────────
#
# Numbers below are sourced from each broker's public docs as of
# v2.1 (mid-2026). When a doc rev changes a limit, update the
# constant here — keep this file as the single source of truth.

KITE_CAPS = BrokerCapabilities(
    broker_id="zerodha_kite",
    display_name="Zerodha Kite",
    gtt_single=True,
    gtt_oco=True,
    gtt_modify=True,
    gtt_cap_per_account=100,
    gtt_validity_days=365,
    gtt_supports_mcx=True,      # Kite GTT covers MCX natively
    bracket_order=False,        # Deprecated by Zerodha in 2020
    cover_order=True,
    atomic_basket=False,        # Web-UI basket only; API has no batch place_order
    order_tag=True,             # Kite `tag` field
    margin_preview=True,        # basket_order_margins endpoint
    postback_gtt="reliable",
    rate_limit_orders_sec=10,
)

DHAN_CAPS = BrokerCapabilities(
    broker_id="dhan",
    display_name="Dhan",
    gtt_single=True,            # Forever Orders
    gtt_oco=True,               # Forever OCO
    gtt_modify=True,
    gtt_cap_per_account=50,     # Tier-dependent; conservative default
    gtt_validity_days=365,      # Forever orders are long-lived
    gtt_supports_mcx=False,     # Dhan Forever doesn't cover MCX/NCO
    bracket_order=True,         # Dhan still supports BO
    cover_order=True,
    atomic_basket=True,         # API-supported multi-leg basket
    order_tag=True,             # `correlation_id` field
    margin_preview=True,        # margin_calculator endpoint
    # Audit fix — no Dhan WebSocket listener or GTT-fire postback handler
    # is wired anywhere in the codebase today. Pre-fix this said
    # "reliable" (intended future state) and the OrderTicket capability
    # chip therefore advertised reliable GTT postback to the operator
    # when actually the only Dhan GTT-fire detection is the
    # `_task_oco_pair_watcher` poll loop. Switch to "poll_only" until a
    # real WebSocket order-update listener lands.
    postback_gtt="poll_only",
    rate_limit_orders_sec=20,
)

GROWW_CAPS = BrokerCapabilities(
    broker_id="groww",
    display_name="Groww",
    gtt_single=True,            # Single-trigger GTT only
    gtt_oco=False,              # No OCO — emulated via two single GTTs + pair-watcher
    gtt_modify=True,
    gtt_cap_per_account=25,     # Conservative; Groww doesn't publish a hard cap
    gtt_validity_days=90,       # Shorter validity than Kite/Dhan
    gtt_supports_mcx=False,     # Groww Smart Order GTT not verified on MCX
    bracket_order=False,
    cover_order=False,
    atomic_basket=False,        # No batch API; fan-out in parallel
    order_tag=False,            # Use the broker_order_link sidecar
    margin_preview=False,       # No public pre-submit margin API
    # Audit fix — was "partial" but no Groww inbound webhook handler
    # exists. GTT-fire detection on Groww is entirely poll-based via
    # _task_oco_pair_watcher. Matches the Dhan correction.
    postback_gtt="poll_only",
    rate_limit_orders_sec=5,
)


# Broker-id → CapabilitySet. Both the canonical id ("zerodha_kite") and
# the legacy alias ("kite") resolve to KITE_CAPS so existing YAML-seeded
# rows work without a column rewrite — matches the registry's _ADAPTERS
# pattern.
CAPS_BY_BROKER_ID: dict[str, BrokerCapabilities] = {
    "zerodha_kite": KITE_CAPS,
    "kite":         KITE_CAPS,   # legacy alias
    "dhan":         DHAN_CAPS,
    "groww":        GROWW_CAPS,
}


# Fallback when a broker_id isn't in the matrix — conservative
# everything-off so a new vendor can't accidentally bypass an
# operator-visible capability gate. New vendors must explicitly opt in
# by adding a CapabilitySet constant.
UNKNOWN_CAPS = BrokerCapabilities(
    broker_id="unknown",
    display_name="Unknown broker",
    gtt_single=False,
    gtt_oco=False,
    gtt_modify=False,
    gtt_cap_per_account=0,
    gtt_validity_days=0,
    gtt_supports_mcx=False,     # Conservative; explicit since the field default was removed
    bracket_order=False,
    cover_order=False,
    atomic_basket=False,
    order_tag=False,
    margin_preview=False,
    postback_gtt="poll_only",
    rate_limit_orders_sec=1,
)


def capabilities_for_broker_id(broker_id: str) -> BrokerCapabilities:
    """Return the capability set for a broker_id, or UNKNOWN_CAPS
    when the vendor isn't in the matrix."""
    return CAPS_BY_BROKER_ID.get(broker_id, UNKNOWN_CAPS)


def capabilities_for(account: str) -> BrokerCapabilities:
    """Resolve a RamboQuant account code to its broker capabilities.
    Uses the same `_broker_id_for` lookup the registry's `get_broker`
    relies on, so a per-account broker_id override in the DB takes
    precedence over YAML defaults."""
    # Local import — keeps circular reference at bay (registry imports
    # from this module too via the Broker.capabilities property).
    from backend.brokers.registry import _broker_id_for
    return capabilities_for_broker_id(_broker_id_for(account))
