"""
Smoke tests for backend.shared.brokers.capabilities — the per-broker
capability matrix that the OrderTemplate fan-out and GTT tracker consult
to decide what to send to each broker.

Focus is on the contract, not implementation details: matrix lookups
work for the three live brokers, fallback to UNKNOWN_CAPS for any
unknown vendor, and the Kite/Dhan/Groww capability divergence matches
what the rest of the codebase relies on (Groww has no OCO, Kite has no
atomic basket, etc.).
"""
from __future__ import annotations

import pytest

from backend.shared.brokers.capabilities import (
    BrokerCapabilities,
    CAPS_BY_BROKER_ID,
    DHAN_CAPS,
    GROWW_CAPS,
    KITE_CAPS,
    UNKNOWN_CAPS,
    capabilities_for_broker_id,
)


def test_kite_caps_match_industry_reality():
    """Kite: GTT (single + OCO) ✓, atomic basket ✗, BO deprecated."""
    assert KITE_CAPS.broker_id == "zerodha_kite"
    assert KITE_CAPS.gtt_single is True
    assert KITE_CAPS.gtt_oco is True
    assert KITE_CAPS.bracket_order is False, "Kite deprecated BO in 2020"
    assert KITE_CAPS.atomic_basket is False, "Kite has no API basket"
    assert KITE_CAPS.order_tag is True
    assert KITE_CAPS.margin_preview is True
    assert KITE_CAPS.gtt_cap_per_account == 100
    assert KITE_CAPS.gtt_validity_days == 365


def test_dhan_caps_match_industry_reality():
    """Dhan: full feature set — GTT (single + OCO), atomic basket, BO."""
    assert DHAN_CAPS.broker_id == "dhan"
    assert DHAN_CAPS.gtt_single is True
    assert DHAN_CAPS.gtt_oco is True
    assert DHAN_CAPS.bracket_order is True
    assert DHAN_CAPS.atomic_basket is True
    assert DHAN_CAPS.order_tag is True
    assert DHAN_CAPS.margin_preview is True


def test_groww_caps_flag_oco_gap():
    """Groww has only single-trigger GTT. The OCO gap is the single
    most important capability divergence — the orchestrator emulates
    OCO via two singles + a pair-watcher. This test guards that
    contract so a future capability refresh doesn't silently flip the
    flag and break the emulation code path."""
    assert GROWW_CAPS.broker_id == "groww"
    assert GROWW_CAPS.gtt_single is True
    assert GROWW_CAPS.gtt_oco is False, (
        "Groww has no OCO. If this flips to True we must remove the "
        "emulation path in the orchestrator."
    )
    assert GROWW_CAPS.order_tag is False, (
        "Groww has no order-tag field. broker_order_link sidecar is "
        "required for template_id correlation."
    )
    assert GROWW_CAPS.atomic_basket is False
    assert GROWW_CAPS.margin_preview is False


def test_legacy_kite_alias_maps_to_zerodha_kite():
    """YAML-seeded rows carry broker='kite' (legacy); DB rows carry
    'zerodha_kite' (canonical). Both must resolve to the same caps."""
    assert capabilities_for_broker_id("kite") is KITE_CAPS
    assert capabilities_for_broker_id("zerodha_kite") is KITE_CAPS


def test_unknown_broker_returns_safe_default():
    """An unrecognised broker_id falls back to UNKNOWN_CAPS — every
    capability flagged False, so a new vendor without explicit opt-in
    can't accidentally bypass a gate."""
    caps = capabilities_for_broker_id("imaginary_broker_v9")
    assert caps is UNKNOWN_CAPS
    assert caps.gtt_single is False
    assert caps.gtt_oco is False
    assert caps.atomic_basket is False
    assert caps.bracket_order is False
    assert caps.order_tag is False
    assert caps.margin_preview is False


def test_matrix_completeness():
    """Every entry in the matrix is a BrokerCapabilities — guards
    against a future PR accidentally registering a string or None."""
    for broker_id, caps in CAPS_BY_BROKER_ID.items():
        assert isinstance(caps, BrokerCapabilities), (
            f"{broker_id!r} is not a BrokerCapabilities"
        )
        assert caps.broker_id, f"{broker_id!r} is missing broker_id"
        assert caps.display_name, f"{broker_id!r} is missing display_name"
        assert caps.postback_gtt in ("reliable", "partial", "poll_only"), (
            f"{broker_id!r} has invalid postback_gtt={caps.postback_gtt!r}"
        )


def test_capabilities_frozen():
    """The dataclass is frozen — capabilities are static constants,
    never mutated at runtime. A frozen=True dataclass means a stray
    `caps.gtt_oco = False` would TypeError at the assignment site."""
    with pytest.raises(Exception):  # noqa: B017 — frozen dataclass raises FrozenInstanceError
        KITE_CAPS.gtt_oco = False  # type: ignore[misc]


def test_broker_base_exposes_capabilities_via_property():
    """The Broker ABC defines a default `capabilities` property that
    reads the matrix. Adapters inherit it without override — this test
    locks the wiring."""
    from backend.shared.brokers.base import Broker

    # Walk the MRO of an inheriting class — KiteBroker.capabilities
    # should resolve to Broker.capabilities (not overridden).
    from backend.shared.brokers.kite import KiteBroker
    cap_attr = Broker.__dict__.get("capabilities")
    assert cap_attr is not None, "Broker ABC must declare capabilities property"
    # KiteBroker doesn't override — inherited from Broker.
    assert "capabilities" not in KiteBroker.__dict__
