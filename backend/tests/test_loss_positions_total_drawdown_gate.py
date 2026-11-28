"""2026-11 alert-noise-reduction plan, Component C — gate `loss-positions-
total`'s rate leaves (pnl_rate_abs / pnl_rate_pct) on drawdown-from-peak
instead of bare day_val sign.

RETIRED 2026-11-28 — the drawdown-from-peak mechanism this file tested
(`any[day_val<0, max_drawdown_pnl(30)<=-threshold]` gating the rate
leaves) was replaced OUTRIGHT by the pure-theoretical-loss redesign:
leaf 2 (the rate pair) is now gated on leaf 1 (`theoretical_total_loss`)
itself having crossed its own threshold, not on day_val sign or
drawdown-from-peak. The three scenario classes this file used to carry
(`TestGivebackCaseFires`, `TestNearZeroArtifactDoesNotFire`,
`TestModestPullbackNowFiresPerOperatorPriority`) and the nested-latch
sanity class (`TestNestedConditionLatchKeying`) tested a mechanism that
no longer exists in this agent — see
`backend/tests/test_loss_positions_total_theoretical_redesign.py` for
their REPLACEMENT coverage (decay/theta fires both leaves, leaf 2 never
fires on a recovering rate, leaf 2 gated on leaf 1 crossing, and the
same nested-latch-keying sanity check re-pointed at the new tree shape).

`TestMigrationHelper` below is kept UNCHANGED — it tests the EARLIER,
still-independent one-time migration from the ANCIENT pre-drawdown-gate
shape (`_LOSS_POSITIONS_TOTAL_OLD_COND`) to whatever shape `agent_def`
currently carries (now the pure-theoretical shape, read dynamically from
`_LOSS_AGENTS` at test time) — this remains correct and valid regardless
of the 2026-11-28 redesign. The SEPARATE migration from the
drawdown-gated shape to the new theoretical shape is tested in
`test_loss_positions_total_theoretical_redesign.py`'s own
`TestDrawdownToTheoreticalMigration`.

See CLAUDE.md's dated entry for the full 2026-11-28 redesign rationale.
"""
from __future__ import annotations

from backend.api.algo.agent_engine import (
    _LOSS_AGENTS,
    _LOSS_POSITIONS_TOTAL_OLD_COND,
    _ae_is_stale_loss_positions_total_cond,
    _ae_sync_existing_builtin,
)


class TestMigrationHelper:
    def test_detects_exact_old_shape(self):
        assert _ae_is_stale_loss_positions_total_cond(_LOSS_POSITIONS_TOTAL_OLD_COND)

    def test_does_not_match_new_shape(self):
        agent = next(a for a in _LOSS_AGENTS if a["slug"] == "loss-positions-total")
        assert not _ae_is_stale_loss_positions_total_cond(agent["conditions"])

    def test_does_not_match_an_operator_customized_tree(self):
        """An operator who customized this agent's conditions in any way
        (even just a different threshold) must not be silently
        overwritten on the next restart."""
        customized = {"any": [
            {"metric": "theoretical_total_loss", "scope": "positions.total",
             "op": "<=", "value": -75000},
            {"metric": "theoretical_daily_loss", "scope": "positions.total",
             "op": "<=", "value": -75000},
            {"metric": "theo_total_rate_abs", "scope": "positions.total", "op": "<=", "value": -6000},
            {"metric": "theo_total_rate_pct", "scope": "positions.total", "op": "<=", "value": -0.25},
        ]}
        assert not _ae_is_stale_loss_positions_total_cond(customized)

    def test_migration_overwrites_only_the_exact_old_shape(self):
        from types import SimpleNamespace

        agent_def = next(a for a in _LOSS_AGENTS if a["slug"] == "loss-positions-total")
        existing = SimpleNamespace(
            slug="loss-positions-total", long_name=agent_def.get("long_name"),
            schedule="market_hours", tier="critical", topic="positions_loss",
            events=agent_def.get("events", []), fire_at_time=None, last_fired=None,
            conditions=dict(_LOSS_POSITIONS_TOTAL_OLD_COND), cooldown_minutes=30,
            status="active",
        )
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.conditions == agent_def["conditions"]
        assert existing.cooldown_minutes == 45

    def test_migration_leaves_a_customized_row_alone(self):
        from types import SimpleNamespace

        agent_def = next(a for a in _LOSS_AGENTS if a["slug"] == "loss-positions-total")
        customized = {"any": [
            {"metric": "theoretical_total_loss", "scope": "positions.total",
             "op": "<=", "value": -75000},
            {"metric": "theoretical_daily_loss", "scope": "positions.total",
             "op": "<=", "value": -75000},
            {"metric": "theo_total_rate_abs", "scope": "positions.total", "op": "<=", "value": -6000},
            {"metric": "theo_total_rate_pct", "scope": "positions.total", "op": "<=", "value": -0.25},
        ]}
        existing = SimpleNamespace(
            slug="loss-positions-total", long_name=agent_def.get("long_name"),
            schedule="market_hours", tier="critical", topic="positions_loss",
            events=agent_def.get("events", []), fire_at_time=None, last_fired=None,
            conditions=dict(customized), cooldown_minutes=30, status="active",
        )
        _ae_sync_existing_builtin(existing, agent_def)
        assert existing.conditions == customized, "operator customization must survive"
