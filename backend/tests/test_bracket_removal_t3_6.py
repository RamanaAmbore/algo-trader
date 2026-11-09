"""
Bracket/Template-GTT removal, T3.6 (2026-11) — sixth and FINAL sub-sprint.

T3.1-T3.5 (all shipped, live on prod before this landed) stopped every
read/write of the `order_templates` table and the 6 `AlgoOrder`/`Agent`
columns that pointed at or mirrored it. This sub-sprint is the
irreversible step: it drops that table and those 6 columns, and cleans
up the handful of remaining dead-code residuals T3.5's own review
flagged but deliberately left for this last sub-sprint (the MCP
`template_slug` parameter, the `hold.template_exit_released` setting,
the `PriceBroker.place_gtt`/`modify_gtt` stubs, the `ticket_preview`
stub route, `templates_seed.py`).

Covers, roughly matching the plan's own "fresh investigation" + schema
drop sections:
  1. `templates_seed.py` deleted, its boot call removed from `init_db()`.
  2. MCP `place_order`'s `template_slug` parameter removed end-to-end
     (kite_server.py signature/docstring, lab.py's `PlaceOrderRequest`/
     `MintTokenRequest`/`_purpose_hash_place`/`_res_resolve_template_slug`),
     with the pre-existing no-template purpose-hash bit-for-bit preserved.
  3. `hold.template_exit_released` / `template_exit_hold_enabled` removed
     from settings.py / order_hold_gate.py / admin.py.
  4. `PriceBroker.place_gtt`/`modify_gtt` stubs removed; `cancel_gtt`/
     `get_gtts` confirmed kept (orders_gtt.py's dependency).
  5. `/orders/ticket/preview` route + `TicketPreviewRequest`/
     `TicketPreviewResponse` removed.
  6. `TicketOrderRequest.template_id` / `BasketLeg.template_id` removed.
  7. `AlgoOrderInfo.template_id` / `.attached_gtts_json` removed;
     `_chase_row_to_info` no longer sets either.
  8. `_pb_wants_take_profit_arm` (orders_postback.py) and
     `_ch_maybe_fire_auto_tp` / `_chase_snapshot_algo_row` (chase.py)
     simplified — no more template_id/template_overrides_json guard.
  9. `actions_live._al_place_resolve_params` returns an 8-tuple (no
     template_id/template_slug); `_place_order_set_product_template`'s
     signature is `(row_id, product)` only; `_al_place_build_overrides`
     and `actions._build_template_overrides` both gone.
  10. `models.py`: `OrderTemplate` class gone; `AlgoOrder.template_id`/
      `.attached_gtts_json`/`.template_overrides_json`/
      `.template_hold_override` gone; `Agent.legacy_template_id`/
      `.bracket_config` gone. `AlgoOrder.hold_json`/
      `.superseded_broker_order_ids_json` confirmed UNTOUCHED.
  11. `database.py`: the new idempotent drop migration, wired into
      `init_db()` as the LAST step inside its transaction; the three
      obsolete ADD-COLUMN migrations that used to target
      `order_templates`/the dropped columns are gone (and would have
      crashed or silently re-added a dropped column on the very next
      boot otherwise); a real scratch-Postgres integration test
      (skipped if no local Postgres is reachable) proves the migration
      actually drops the table/columns and is safe to run twice.

Not touched (confirmed, by design): `orders_gtt.py` and its
`get_gtts`/`cancel_gtt` dependency; `template_registry.py`'s `$ref`
fragment mechanism (`Agent(kind='template')`, a separate, live,
unrelated feature); `event_agents.py`'s dead `template_guard`/
`template_slug` alert-rendering residual (a DIFFERENT "template_slug"
concept — a dict key in an alert's `extra` payload, not this table/
column — explicitly left in place by T3.1's own review); the live
Track-1 `exit-tp-short-option-90` agent and its hold/release/
auto-release/position-lock infrastructure; `AlgoOrder.hold_json` /
`.superseded_broker_order_ids_json`; any pre-existing `kind='bracket'`
`Agent` row (not deleted — `agent_engine.py`'s orphan-sweep `kind !=
"bracket"` guard is untouched, confirmed zero such rows exist on
either prod or dev today).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest


# ═══════════════════════════════════════════════════════════════════════
# 1. templates_seed.py deleted
# ═══════════════════════════════════════════════════════════════════════

class TestTemplatesSeedRemoved:
    def test_module_does_not_exist(self):
        with pytest.raises(ModuleNotFoundError):
            import backend.api.algo.templates_seed  # noqa: F401

    def test_init_db_no_longer_calls_seed_templates(self):
        """Checks for an actual import/call, not a bare substring — the
        function's own explanatory comment legitimately names
        `templates_seed`/`seed_templates` to document the removal (same
        convention as every other migration docstring in this file)."""
        import backend.api.database as dbmod
        src = inspect.getsource(dbmod.init_db)
        assert "import templates_seed" not in src
        assert "await seed_templates()" not in src


# ═══════════════════════════════════════════════════════════════════════
# 2. MCP template_slug removal
# ═══════════════════════════════════════════════════════════════════════

class TestMcpTemplateSlugRemoved:
    def test_place_order_request_has_no_template_slug(self):
        from backend.api.routes.lab import PlaceOrderRequest
        assert "template_slug" not in PlaceOrderRequest.__struct_fields__

    def test_mint_token_request_has_no_template_slug(self):
        from backend.api.routes.lab import MintTokenRequest
        assert "template_slug" not in MintTokenRequest.__struct_fields__

    def test_purpose_hash_place_signature_has_no_template_slug_param(self):
        from backend.api.routes.lab import _purpose_hash_place
        sig = inspect.signature(_purpose_hash_place)
        assert "template_slug" not in sig.parameters

    def test_purpose_hash_place_baseline_unchanged(self):
        """The pre-removal no-template fingerprint must survive
        bit-for-bit — any already-minted (still-live, <=60s TTL) token
        for a template-less order must keep validating."""
        from backend.api.routes.lab import _purpose_hash_place

        _BASELINE_NO_TPL_A = "ac94242b9d0f6fffa8a8588521427c14b2b8d1998d93ae182501471b13f15e0d"
        _BASELINE_NO_TPL_B = "21e687c0236e60279454100ebf386c3883e155a06d9e10b0a15d2e8562e430b5"

        assert _purpose_hash_place(
            "ZG0790", "NIFTY25OCTFUT", "BUY", 1, "LIMIT", "paper", None, None,
        ) == _BASELINE_NO_TPL_A
        assert _purpose_hash_place(
            "zg0790", " nifty25octfut ", "sell", 50, "SL", "live", 590.8, 589.5,
        ) == _BASELINE_NO_TPL_B

    def test_resolve_template_slug_function_removed(self):
        import backend.api.routes.lab as lab_mod
        assert not hasattr(lab_mod, "_res_resolve_template_slug")

    def test_res_make_place_ticket_has_no_template_id_param(self):
        from backend.api.routes.lab import _res_make_place_ticket
        sig = inspect.signature(_res_make_place_ticket)
        assert "template_id" not in sig.parameters

    def test_kite_server_place_order_tool_has_no_template_slug_param(self):
        import backend.mcp.kite_server as ks
        sig = inspect.signature(ks.place_order)
        assert "template_slug" not in sig.parameters

    def test_kite_server_place_order_docstring_mentions_no_template(self):
        import backend.mcp.kite_server as ks
        assert "template" not in (ks.place_order.__doc__ or "").lower()

    @pytest.mark.asyncio
    async def test_place_order_route_forwards_with_no_template_concept(self):
        """A real McpController.place_order call (no template_slug on the
        request at all) still forwards mcp_request_id correctly — mirrors
        the pre-removal end-to-end coverage, minus the template bits."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from backend.api.routes.lab import McpController, PlaceOrderRequest

        ctrl = McpController(owner=None)
        data = PlaceOrderRequest(
            confirm_token="tok123", account="ZG0790",
            tradingsymbol="NIFTY25OCTFUT", side="BUY", quantity=1, mode="paper",
        )
        fake_request = MagicMock()
        captured: dict = {}

        class _FakeTicketResponse:
            order_id = "555"
            mode = "paper"
            status = "OPEN"
            detail = "placed"

        async def _fake_ticket_order_handler(data, request):
            captured["ticket"] = data
            return _FakeTicketResponse()

        with patch("backend.api.routes.lab._consume_token", return_value=None), \
             patch("backend.api.routes.lab._user_id", return_value=1), \
             patch("backend.api.routes.lab._res_mcp_audit", new=AsyncMock()), \
             patch("backend.api.routes.lab._res_place_telegram_ping"), \
             patch("backend.api.routes.orders_place.ticket_order_handler",
                   new=_fake_ticket_order_handler):
            resp = await McpController.place_order.fn(ctrl, data=data, request=fake_request)

        assert resp.order_id == "555"
        ticket = captured["ticket"]
        assert not hasattr(ticket, "template_id")
        assert ticket.mcp_request_id is not None
        assert ticket.source == "mcp"


# ═══════════════════════════════════════════════════════════════════════
# 3. hold.template_exit_released / template_exit_hold_enabled removed
# ═══════════════════════════════════════════════════════════════════════

class TestTemplateExitHoldSettingRemoved:
    def test_order_hold_gate_has_no_template_exit_key(self):
        import backend.api.algo.order_hold_gate as ohg
        assert not hasattr(ohg, "TEMPLATE_EXIT_RELEASED_KEY")

    def test_settings_seeds_no_longer_include_template_exit_released(self):
        from backend.shared.helpers.settings import SEEDS
        keys = {s[1] for s in SEEDS}
        assert "hold.template_exit_released" not in keys

    def test_global_switches_request_has_no_template_exit_field(self):
        from backend.api.routes.admin import GlobalSwitchesRequest
        assert "template_exit_hold_enabled" not in GlobalSwitchesRequest.__struct_fields__

    def test_global_switches_response_has_no_template_exit_field(self):
        from backend.api.routes.admin import GlobalSwitchesResponse
        assert "template_exit_hold_enabled" not in GlobalSwitchesResponse.__struct_fields__

    @pytest.mark.asyncio
    async def test_orphan_setting_row_auto_pruned_on_next_seed(self):
        """seed_settings()'s existing _prune_retired_keys() sweep deletes
        any already-seeded row whose key is no longer in SEEDS — proves
        the orphaned `hold.template_exit_released` row left behind on an
        already-deployed DB is cleaned up with no dedicated migration."""
        from backend.shared.helpers.settings import _prune_retired_keys, SEEDS

        class _FakeRow:
            def __init__(self, key):
                self.key = key

        class _FakeSession:
            def __init__(self):
                self.deleted_stmt = None

            async def execute(self, stmt):
                self.deleted_stmt = stmt
                return None

        existing_by_key = {
            "hold.template_exit_released": _FakeRow("hold.template_exit_released"),
            "hold.expiry_close_released": _FakeRow("hold.expiry_close_released"),
        }
        seed_keys = {s[1] for s in SEEDS}
        session = _FakeSession()
        removed = await _prune_retired_keys(session, existing_by_key, seed_keys)

        assert removed == 1
        assert session.deleted_stmt is not None


# ═══════════════════════════════════════════════════════════════════════
# 4. PriceBroker.place_gtt / modify_gtt stubs removed; cancel_gtt/get_gtts kept
# ═══════════════════════════════════════════════════════════════════════

class TestPriceBrokerGttStubsRemoved:
    def test_place_gtt_and_modify_gtt_gone(self):
        from backend.brokers.registry import PriceBroker
        assert not hasattr(PriceBroker, "place_gtt")
        assert not hasattr(PriceBroker, "modify_gtt")

    def test_cancel_gtt_and_get_gtts_kept(self):
        """orders_gtt.py's confirmed-independent standalone broker-GTT
        viewer depends on these — must survive."""
        from backend.brokers.registry import PriceBroker
        assert hasattr(PriceBroker, "cancel_gtt")
        assert hasattr(PriceBroker, "get_gtts")


# ═══════════════════════════════════════════════════════════════════════
# 5. /orders/ticket/preview route + schemas removed
# ═══════════════════════════════════════════════════════════════════════

class TestTicketPreviewRemoved:
    def test_schemas_have_no_ticket_preview_structs(self):
        import backend.api.schemas as s
        assert not hasattr(s, "TicketPreviewRequest")
        assert not hasattr(s, "TicketPreviewResponse")

    def test_orders_route_has_no_ticket_preview_method(self):
        import backend.api.routes.orders as orders_mod
        assert not hasattr(orders_mod.OrdersController, "ticket_preview")

    def test_orders_route_has_no_preview_empty_plan_helper(self):
        import backend.api.routes.orders as orders_mod
        assert not hasattr(orders_mod, "_rco_preview_empty_plan")

    def test_no_live_route_registration_for_ticket_preview(self):
        import backend.api.routes.orders as orders_mod
        src = inspect.getsource(orders_mod)
        assert '@post("/ticket/preview")' not in src


# ═══════════════════════════════════════════════════════════════════════
# 6. TicketOrderRequest.template_id / BasketLeg.template_id removed
# ═══════════════════════════════════════════════════════════════════════

class TestTicketAndBasketTemplateIdRemoved:
    def test_ticket_order_request_has_no_template_id(self):
        from backend.api.schemas import TicketOrderRequest
        assert "template_id" not in TicketOrderRequest.__struct_fields__
        # Still constructs cleanly with the minimal required fields.
        TicketOrderRequest(mode="paper", side="BUY", tradingsymbol="X", quantity=1)

    def test_basket_leg_has_no_template_id(self):
        from backend.api.schemas import BasketLeg
        assert "template_id" not in BasketLeg.__struct_fields__
        BasketLeg(tradingsymbol="X", exchange="NFO", transaction_type="BUY", quantity=1)

    def test_ticket_order_request_wire_decode_silently_drops_stray_template_id(self):
        """A stray `template_id` key in the raw JSON body (e.g. from a
        caller that hasn't redeployed yet) must be silently dropped by
        msgspec on decode, never raise — same policy as every other
        removed override field."""
        import json
        import msgspec
        from backend.api.schemas import TicketOrderRequest

        decoder = msgspec.json.Decoder(TicketOrderRequest)
        payload = json.dumps({
            "mode": "paper", "side": "BUY", "tradingsymbol": "X", "quantity": 1,
            "template_id": 9,
        }).encode()
        result = decoder.decode(payload)
        assert result.mode == "paper"
        assert not hasattr(result, "template_id")


# ═══════════════════════════════════════════════════════════════════════
# 7. AlgoOrderInfo.template_id / .attached_gtts_json removed
# ═══════════════════════════════════════════════════════════════════════

class TestAlgoOrderInfoFieldsRemoved:
    def test_fields_gone(self):
        from backend.api.routes.orders_helpers import AlgoOrderInfo
        fields = set(AlgoOrderInfo.__struct_fields__)
        assert "template_id" not in fields
        assert "attached_gtts_json" not in fields
        # hold_json must survive — generic, shared across hold categories.
        assert "hold_json" in fields

    def test_chase_row_to_info_does_not_crash_on_row_without_those_columns(self):
        """A SimpleNamespace row with neither attribute (matching the
        post-drop AlgoOrder shape) must not raise AttributeError —
        _chase_row_to_info must not read either attribute anymore."""
        from types import SimpleNamespace
        from backend.api.routes.orders import _chase_row_to_info

        row = SimpleNamespace(
            id=1, account="ZG0790", symbol="NIFTY24DECFUT", exchange="NFO",
            transaction_type="BUY", quantity=50, initial_price=100.0,
            current_limit=None, fill_price=None, attempts=0,
            status="OPEN", engine="live", mode="live", detail=None,
            created_at=None, target_pct=None, target_abs=None,
            parent_order_id=None, basket_tag=None, filled_quantity=None,
            interval_seconds=None, last_attempt_at=None, next_attempt_at=None,
            broker_order_id=None, source=None, agent_id=None, hold_json=None,
        )
        info = _chase_row_to_info(row, lambda a: a, {}, {})
        assert not hasattr(info, "template_id")
        assert not hasattr(info, "attached_gtts_json")


# ═══════════════════════════════════════════════════════════════════════
# 8. Legacy single-target-TP shims simplified (no more template guard)
# ═══════════════════════════════════════════════════════════════════════

class TestLegacyTpShimsSimplified:
    """orders_postback._pb_wants_take_profit_arm and
    chase._ch_maybe_fire_auto_tp / _chase_snapshot_algo_row — the
    template_id/template_overrides_json exclusion guard is gone along
    with those two columns; both now fire on target_pct/target_abs
    alone (live-confirmed zero OPEN/CANCEL_FAILED/HELD rows had both
    set together on prod or dev, so this is a documented no-op change
    for every currently-live row)."""

    def test_pb_wants_take_profit_arm_fires_without_template_fields(self):
        from unittest.mock import MagicMock
        from backend.api.routes.orders_postback import _pb_wants_take_profit_arm

        row = MagicMock()
        row.target_pct = 0.30
        row.target_abs = None
        row.parent_order_id = None
        row.fill_price = 100.0
        assert _pb_wants_take_profit_arm(row) is True

    def test_pb_wants_take_profit_arm_does_not_read_template_attrs(self):
        """Checks the function's real behaviour, not its docstring (which
        legitimately names the removed columns to explain the removal):
        a row whose `template_id`/`template_overrides_json` attribute
        access would raise AttributeError (matching the real dropped-
        column shape) must not raise — the function must never touch
        either attribute."""
        from types import SimpleNamespace
        from backend.api.routes.orders_postback import _pb_wants_take_profit_arm

        class _NoTemplateAttrsRow(SimpleNamespace):
            def __getattr__(self, name):
                if name in ("template_id", "template_overrides_json"):
                    raise AttributeError(name)
                return super().__getattribute__(name)

        row = _NoTemplateAttrsRow(
            target_pct=0.30, target_abs=None, parent_order_id=None, fill_price=100.0,
        )
        assert _pb_wants_take_profit_arm(row) is True

    def test_ch_maybe_fire_auto_tp_fires_without_template_fields(self):
        from unittest.mock import patch
        from backend.api.algo.chase import _ch_maybe_fire_auto_tp

        snap = {
            "id": 5, "target_pct": 0.30, "target_abs": None,
            "parent_order_id": None, "account": "ZG0790", "symbol": "NIFTY24APR25000CE",
            "exchange": "NFO", "transaction_type": "SELL", "product": "NRML",
            "mode": "live", "filled_quantity": 50, "quantity": 50,
            "intent": "", "is_close_intent": False,
        }
        with patch("asyncio.create_task") as mock_create_task:
            _ch_maybe_fire_auto_tp(snap, 100.0)
            assert mock_create_task.called

    def test_chase_snapshot_algo_row_has_no_template_keys(self):
        from types import SimpleNamespace
        from backend.api.algo.chase import _chase_snapshot_algo_row

        row = SimpleNamespace(
            id=1, agent_id=None, target_pct=None, target_abs=None,
            parent_order_id=None, account="A", symbol="S", exchange="NFO",
            transaction_type="BUY", product="NRML", mode="live",
            filled_quantity=0, quantity=10, intent="",
        )
        snap = _chase_snapshot_algo_row(row, "bo-1")
        assert "template_id" not in snap
        assert "template_overrides_json" not in snap


# ═══════════════════════════════════════════════════════════════════════
# 9. Generic place_order action — template_id/template_slug params gone
# ═══════════════════════════════════════════════════════════════════════

class TestGenericPlaceOrderActionTemplateParamsRemoved:
    def test_al_place_resolve_params_returns_eight_tuple(self):
        from unittest.mock import MagicMock
        from backend.api.algo.actions_live import _al_place_resolve_params

        agent = MagicMock()
        agent.id = 1
        agent.slug = "test"
        result = _al_place_resolve_params(agent, {}, {"account": "ZG0790"})
        assert len(result) == 8

    def test_place_order_set_product_template_signature(self):
        import inspect as _inspect
        from backend.api.algo.actions_live import _place_order_set_product_template
        sig = _inspect.signature(_place_order_set_product_template)
        assert list(sig.parameters) == ["row_id", "product"]

    def test_al_place_build_overrides_removed(self):
        import backend.api.algo.actions_live as al
        assert not hasattr(al, "_al_place_build_overrides")

    def test_build_template_overrides_removed(self):
        import backend.api.algo.actions as actions_mod
        assert not hasattr(actions_mod, "_build_template_overrides")


# ═══════════════════════════════════════════════════════════════════════
# 10. models.py — OrderTemplate class + 6 columns gone; hold_json /
#     superseded_broker_order_ids_json confirmed untouched
# ═══════════════════════════════════════════════════════════════════════

class TestModelsColumnsRemoved:
    def test_order_template_class_removed(self):
        import backend.api.models as m
        assert not hasattr(m, "OrderTemplate")

    def test_algo_order_dropped_columns_gone(self):
        from backend.api.models import AlgoOrder
        cols = set(AlgoOrder.__table__.columns.keys())
        for name in ("template_id", "attached_gtts_json",
                     "template_overrides_json", "template_hold_override"):
            assert name not in cols, f"AlgoOrder.{name} must be removed"

    def test_agent_dropped_columns_gone(self):
        from backend.api.models import Agent
        cols = set(Agent.__table__.columns.keys())
        for name in ("legacy_template_id", "bracket_config"):
            assert name not in cols, f"Agent.{name} must be removed"

    def test_algo_order_hold_json_and_superseded_ids_untouched(self):
        from backend.api.models import AlgoOrder
        cols = set(AlgoOrder.__table__.columns.keys())
        assert "hold_json" in cols
        assert "superseded_broker_order_ids_json" in cols

    def test_algo_order_constructs_without_removed_kwargs(self):
        from backend.api.models import AlgoOrder
        row = AlgoOrder(
            account="ZG0790", symbol="NIFTY24DECFUT", exchange="NFO",
            transaction_type="BUY", quantity=1,
        )
        assert not hasattr(row, "template_id")
        assert not hasattr(row, "attached_gtts_json")

    def test_agent_constructs_without_removed_kwargs(self):
        from backend.api.models import Agent
        row = Agent(slug="x", name="x")
        assert not hasattr(row, "legacy_template_id")
        assert not hasattr(row, "bracket_config")


# ═══════════════════════════════════════════════════════════════════════
# 11a. database.py — old ADD-COLUMN migrations removed
# ═══════════════════════════════════════════════════════════════════════

class TestObsoleteAddColumnMigrationsRemoved:
    def test_order_templates_wing_max_spread_pct_migration_removed(self):
        import backend.api.database as dbmod
        assert not hasattr(dbmod, "_migrate_order_templates_wing_max_spread_pct")

    def test_agents_bracket_columns_migration_removed(self):
        import backend.api.database as dbmod
        assert not hasattr(dbmod, "_migrate_agents_bracket_columns")

    def test_order_templates_group_and_tags_migration_removed(self):
        import backend.api.database as dbmod
        assert not hasattr(dbmod, "_migrate_order_templates_group_and_tags")

    def test_init_db_source_never_adds_or_references_order_templates_table(self):
        """Structural guard: nothing in init_db()'s own source (nor any
        function it calls that's still wired in) may emit an ALTER/CREATE
        against `order_templates`, or re-add any of the 6 dropped
        columns — a future regression here would crash the very next
        boot after the drop (ALTER ... ADD COLUMN IF NOT EXISTS raises
        against a dropped table) or silently undo the removal."""
        import backend.api.database as dbmod
        src = inspect.getsource(dbmod.init_db)
        assert "_migrate_order_templates_wing_max_spread_pct" not in src
        assert "_migrate_agents_bracket_columns" not in src
        assert "_migrate_order_templates_group_and_tags" not in src

    def test_migrate_slice_m_no_longer_targets_dropped_columns(self):
        """Checks the EXECUTED SQL statements only, not the function's own
        docstring (which legitimately names the removed columns/table to
        explain the removal, same convention used throughout this file's
        other migration docstrings)."""
        import backend.api.database as dbmod
        conn = _FakeTxConn()
        asyncio.run(dbmod._migrate_slice_m(conn))
        joined = "\n".join(conn.executed)
        assert "order_templates" not in joined
        assert "template_id" not in joined
        assert "attached_gtts_json" not in joined
        assert "template_overrides_json" not in joined

    def test_migrate_grammar_source_no_longer_targets_template_hold_override(self):
        import backend.api.database as dbmod
        conn = _FakeTxConn()
        asyncio.run(dbmod._migrate_grammar_source_and_schema_version(conn))
        joined = "\n".join(conn.executed)
        assert "template_hold_override" not in joined

    def test_migrate_audit_and_request_no_longer_creates_trail_stop_index(self):
        import backend.api.database as dbmod
        conn = _FakeTxConn()
        asyncio.run(dbmod._migrate_audit_and_request(conn))
        joined = "\n".join(conn.executed)
        assert "attached_gtts_json" not in joined
        assert "ix_algo_orders_trail_stop" not in joined


# ═══════════════════════════════════════════════════════════════════════
# 11b. database.py — new drop migration, statement shape + idempotency
#      (_FakeTxConn pattern, mirrors _migrate_drop_position_pairs_table's
#      own test file exactly)
# ═══════════════════════════════════════════════════════════════════════

class _FakeResult:
    def fetchone(self): return None
    def fetchall(self): return []
    def scalar(self): return None
    def scalar_one_or_none(self): return None
    def all(self): return []
    def first(self): return None
    def scalars(self): return self
    def __iter__(self): return iter(())


class _FakeTxConn:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, stmt, *a, **k):
        self.executed.append(str(stmt))
        return _FakeResult()

    async def run_sync(self, fn, *a, **k):
        return None


class TestDropMigrationStatementShape:
    def test_drops_both_fk_columns_first(self):
        import backend.api.database as dbmod
        conn = _FakeTxConn()
        asyncio.run(dbmod._migrate_drop_order_templates_and_bracket_columns(conn))

        joined = conn.executed
        tid_idx = next(i for i, s in enumerate(joined) if "DROP COLUMN IF EXISTS template_id" in s)
        ltid_idx = next(i for i, s in enumerate(joined) if "DROP COLUMN IF EXISTS legacy_template_id" in s)
        table_idx = next(i for i, s in enumerate(joined) if "DROP TABLE IF EXISTS order_templates" in s)

        assert "algo_orders" in joined[tid_idx]
        assert "agents" in joined[ltid_idx]
        # Both FK-column drops happen before the table drop.
        assert tid_idx < table_idx
        assert ltid_idx < table_idx

    def test_drops_all_six_columns_and_the_table(self):
        import backend.api.database as dbmod
        conn = _FakeTxConn()
        asyncio.run(dbmod._migrate_drop_order_templates_and_bracket_columns(conn))

        joined = "\n".join(conn.executed)
        for stmt in (
            "ALTER TABLE algo_orders DROP COLUMN IF EXISTS template_id",
            "ALTER TABLE algo_orders DROP COLUMN IF EXISTS attached_gtts_json",
            "ALTER TABLE algo_orders DROP COLUMN IF EXISTS template_overrides_json",
            "ALTER TABLE algo_orders DROP COLUMN IF EXISTS template_hold_override",
            "ALTER TABLE agents DROP COLUMN IF EXISTS legacy_template_id",
            "ALTER TABLE agents DROP COLUMN IF EXISTS bracket_config",
            "DROP TABLE IF EXISTS order_templates",
        ):
            assert stmt in joined, f"missing statement: {stmt}"

    def test_table_drop_has_no_cascade(self):
        import backend.api.database as dbmod
        conn = _FakeTxConn()
        asyncio.run(dbmod._migrate_drop_order_templates_and_bracket_columns(conn))
        drop_stmt = next(s for s in conn.executed if "DROP TABLE IF EXISTS order_templates" in s)
        assert "CASCADE" not in drop_stmt.upper()

    def test_sets_lock_timeout(self):
        import backend.api.database as dbmod
        conn = _FakeTxConn()
        asyncio.run(dbmod._migrate_drop_order_templates_and_bracket_columns(conn))
        assert any("lock_timeout" in s for s in conn.executed)

    def test_no_concurrently_statement(self):
        """Would hard-error inside init_db's transaction block."""
        import backend.api.database as dbmod
        conn = _FakeTxConn()
        asyncio.run(dbmod._migrate_drop_order_templates_and_bracket_columns(conn))
        assert not any("CONCURRENTLY" in s for s in conn.executed)

    def test_idempotent_rerun_never_raises(self):
        """Running the step twice (fresh DB + already-migrated DB) never
        raises — every statement is IF EXISTS/IF NOT EXISTS."""
        import backend.api.database as dbmod
        conn = _FakeTxConn()
        asyncio.run(dbmod._migrate_drop_order_templates_and_bracket_columns(conn))
        first_count = len(conn.executed)
        asyncio.run(dbmod._migrate_drop_order_templates_and_bracket_columns(conn))  # must not raise
        assert len(conn.executed) == first_count * 2

    def test_hold_json_and_superseded_ids_never_touched(self):
        import backend.api.database as dbmod
        conn = _FakeTxConn()
        asyncio.run(dbmod._migrate_drop_order_templates_and_bracket_columns(conn))
        joined = "\n".join(conn.executed)
        assert "hold_json" not in joined
        assert "superseded_broker_order_ids_json" not in joined

    def test_init_db_calls_drop_migration_inside_begin_block_last(self):
        """Structural guard: must run INSIDE engine.begin(), and AFTER
        every other migration call in that block (it drops schema other
        migrations in this same transaction might still reference)."""
        import backend.api.database as dbmod
        src = inspect.getsource(dbmod.init_db)
        lines = src.splitlines()

        begin_idx = next(
            i for i, l in enumerate(lines) if "async with engine.begin() as conn:" in l
        )
        begin_indent = len(lines[begin_idx]) - len(lines[begin_idx].lstrip())

        call_idx = next(
            i for i, l in enumerate(lines)
            if "_migrate_drop_order_templates_and_bracket_columns(conn)" in l
            and "async def" not in l
        )
        call_indent = len(lines[call_idx]) - len(lines[call_idx].lstrip())
        assert call_idx > begin_idx and call_indent > begin_indent

        # It must be the LAST `await _migrate_*(conn)` call before the
        # block ends (next non-blank line drops below begin_indent).
        for l in lines[call_idx + 1:]:
            stripped = l.strip()
            if not stripped:
                continue
            indent = len(l) - len(l.lstrip())
            if indent <= begin_indent:
                break
            assert "await _migrate_" not in stripped, (
                f"a migration call runs AFTER the drop migration: {stripped!r}"
            )


# ═══════════════════════════════════════════════════════════════════════
# 11c. database.py — real scratch-Postgres integration test
#
# Skipped (not failed) when no local Postgres is reachable — this is a
# genuine integration test, not a unit test, and this project's test
# suite otherwise never requires a live Postgres (every other `_migrate_*`
# test in this codebase uses the _FakeTxConn pattern above). Creates and
# drops its OWN throwaway scratch database — never touches dev/prod.
# ═══════════════════════════════════════════════════════════════════════

def _pg_scratch_dsn_sync() -> "str | None":
    """Best-effort local-Postgres probe via the OS user's own peer/trust
    auth (same convention the local psql client already uses on this
    machine) — returns None (never raises) when unreachable, so the
    gated test below can skip cleanly instead of failing in an
    environment with no local Postgres."""
    import getpass
    import socket
    try:
        sock = socket.create_connection(("127.0.0.1", 5432), timeout=1)
        sock.close()
    except OSError:
        return None
    return f"postgresql://{getpass.getuser()}@127.0.0.1:5432/postgres"


@pytest.mark.asyncio
async def test_drop_migration_real_postgres_scratch_db():
    try:
        import asyncpg
    except ImportError:
        pytest.skip("asyncpg not installed")

    base_dsn = _pg_scratch_dsn_sync()
    if base_dsn is None:
        pytest.skip("no local Postgres reachable on 127.0.0.1:5432")

    import uuid
    scratch_name = f"ramboq_t36_scratch_{uuid.uuid4().hex[:12]}"

    try:
        admin_conn = await asyncpg.connect(base_dsn)
    except Exception as e:
        pytest.skip(f"could not connect to local Postgres: {e}")

    try:
        await admin_conn.execute(f'CREATE DATABASE "{scratch_name}"')
    finally:
        await admin_conn.close()

    scratch_dsn = base_dsn.rsplit("/", 1)[0] + f"/{scratch_name}"
    try:
        conn = await asyncpg.connect(scratch_dsn)
        try:
            # Build the OLD schema this migration is meant to tear down:
            # order_templates + the 6 AlgoOrder/Agent columns + both FKs.
            await conn.execute("""
                CREATE TABLE order_templates (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(128) NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE algo_orders (
                    id SERIAL PRIMARY KEY,
                    hold_json TEXT,
                    superseded_broker_order_ids_json TEXT,
                    template_id INTEGER REFERENCES order_templates(id) ON DELETE SET NULL,
                    attached_gtts_json TEXT,
                    template_overrides_json TEXT,
                    template_hold_override BOOLEAN
                )
            """)
            await conn.execute("""
                CREATE TABLE agents (
                    id SERIAL PRIMARY KEY,
                    legacy_template_id INTEGER UNIQUE
                        REFERENCES order_templates(id) ON DELETE SET NULL,
                    bracket_config JSONB
                )
            """)
            # Seed one row in each to prove DROP COLUMN survives real data.
            tid = await conn.fetchval(
                "INSERT INTO order_templates (name) VALUES ('default-bull') RETURNING id"
            )
            await conn.execute(
                "INSERT INTO algo_orders (template_id, attached_gtts_json, hold_json) "
                "VALUES ($1, '[]', NULL)", tid,
            )
            await conn.execute(
                "INSERT INTO agents (legacy_template_id, bracket_config) VALUES ($1, '{}')",
                tid,
            )

            import backend.api.database as dbmod

            class _RealConnAdapter:
                """Thin async-SQLAlchemy-text-shaped adapter over a raw
                asyncpg connection, matching the `conn.execute(text(...))`
                call shape `_migrate_drop_order_templates_and_bracket_
                columns` uses — enough to drive the real function against
                a real database without pulling in the full SQLAlchemy
                async engine for one test."""
                def __init__(self, raw):
                    self._raw = raw

                async def execute(self, stmt, *a, **k):
                    return await self._raw.execute(str(stmt))

            adapter = _RealConnAdapter(conn)

            # Run it TWICE — proves idempotency against a real database,
            # not just the fake-conn statement recorder above.
            await dbmod._migrate_drop_order_templates_and_bracket_columns(adapter)
            await dbmod._migrate_drop_order_templates_and_bracket_columns(adapter)

            table_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'order_templates')"
            )
            assert table_exists is False

            for table, col in (
                ("algo_orders", "template_id"),
                ("algo_orders", "attached_gtts_json"),
                ("algo_orders", "template_overrides_json"),
                ("algo_orders", "template_hold_override"),
                ("agents", "legacy_template_id"),
                ("agents", "bracket_config"),
            ):
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = $1 AND column_name = $2)", table, col,
                )
                assert exists is False, f"{table}.{col} still exists after the drop"

            # hold_json / superseded_broker_order_ids_json must survive,
            # with the row's own data intact (DROP COLUMN on siblings
            # must never touch an unrelated column's data).
            for table, col in (
                ("algo_orders", "hold_json"),
                ("algo_orders", "superseded_broker_order_ids_json"),
            ):
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = $1 AND column_name = $2)", table, col,
                )
                assert exists is True, f"{table}.{col} must survive the drop"

            remaining_row_count = await conn.fetchval("SELECT count(*) FROM algo_orders")
            assert remaining_row_count == 1, (
                "dropping columns must not delete the row itself"
            )
        finally:
            await conn.close()
    finally:
        admin_conn2 = await asyncpg.connect(base_dsn)
        try:
            await admin_conn2.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
        finally:
            await admin_conn2.close()
