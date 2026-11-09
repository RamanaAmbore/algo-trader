"""
Bracket/Template-GTT removal, T3.2 (2026-11) — second of 6 sub-sprints.

Sub-sprint 1 (T3.1, commit 9b8ded95) deleted the core engine
(`template_attach.py`, `bracket_registry.py`, `templates.py` routes) and
neutralized the dangling imports that deletion left in
`actions.py`/`actions_live.py`/`actions_sim.py`/`lab.py`/`event_agents.py`.

This sub-sprint (T3.2) removes everything the first pass explicitly
deferred:
  1. The two background safety-net task clusters —
     `_task_trail_stop`/`_task_oco_pair_watcher` and their ~1,330-line
     helper cluster in `backend/api/background.py` (both were
     Bracket-exclusive: the first read `attached_gtts_json`/
     `sl_trail_pct`-shaped entries, the second `sibling_id`/
     `sibling_ids` entries — fields only the Bracket engine ever wrote).
  2. `backend/api/algo/sim/gtt_book.py` (`SimGttBook`) and its wiring in
     `SimDriver`/`SimReplayDriver`/`routes/simulator.py`'s `/gtt*` routes.
  3. The `template_id`/`template_slug`/override params on the
     `place_order`/`order` agent-grammar action tokens
     (`backend/config/grammars/agent_grammar.yaml`), plus the
     `mutually_exclusive_groups` declaration that covered two of them.
  4. Residual Bracket passthrough in `routes/simulator.py` (the `/gtt*`
     routes — confirmed live, see item 2) and a re-confirmation that
     `orders_basket.py` and `routes/lab.py` were already fully clean
     after T3.1 (neither needed a change in this round).
  5. `OrderTemplateOut`/`OrderTemplateCreate`/`OrderTemplatePatch`
     schemas, and the override-field kwargs (`tp_pct_override`,
     `sl_pct_override`, `wing_premium_pct_override`,
     `wing_strike_offset_override`, `tp_order_type_override`,
     `wing_max_spread_pct_override`, `sl_trail_pct_override`,
     `tp_scales_json_override`, `hold_template_exit`) from
     `TicketOrderRequest`/`TicketPreviewRequest`/`BasketLeg`.
     `template_id` is DELIBERATELY KEPT on all three — the
     still-unmodified frontend (and MCP's `lab.py`, which constructs a
     `TicketOrderRequest` with `template_id=template_id`) may still send
     it until its own removal sub-sprint lands; it is silently dropped
     before persist on every path, never acted on. The now-orphaned
     serializers (`orders_helpers._ticket_overrides_dict`/
     `_build_overrides_json`) that only ever read those override fields
     were removed in the same round.

Not touched: `GttRow`/`GttListResponse`/`GttCancelResponse`
(`orders_gtt.py`'s confirmed-independent standalone broker-GTT viewer),
the `exit-tp-short-option-90` agent and its hold/release/auto-release/
position-lock infrastructure (Track-1), and any frontend/doc file.
"""
from __future__ import annotations

import ast

import pytest


# ---------------------------------------------------------------------------
# Item 1 — background.py trail-stop / OCO-pair-watcher cluster
# ---------------------------------------------------------------------------

class TestBackgroundTrailStopOcoWatcherGone:
    def test_task_functions_absent(self):
        import backend.api.background as bg

        for name in (
            "_task_trail_stop",
            "_task_oco_pair_watcher",
        ):
            assert not hasattr(bg, name), f"{name} must be fully removed"

    def test_helper_cluster_absent(self):
        """The ~1,330-line helper cluster these two tasks owned — every
        name confirmed to have zero callers outside the cluster itself
        before removal."""
        import backend.api.background as bg

        helper_names = [
            "_bg_trail_entry_key", "_collect_trail_keys_by_account",
            "_batch_ltp_by_account", "_build_trail_modify_kwargs",
            "_send_trail_partial_modify_alert", "_bg_trail_entry_is_valid",
            "_bg_trail_entry_build_fields", "_extract_trail_entry_fields",
            "_compute_trail_watermark", "_resolve_trail_wire_qty",
            "_process_trail_entry", "_process_trail_row",
            "_flush_trail_updates", "_bg_oco_has_any_gtt_entry",
            "_oco_parse_entries", "_oco_build_gtts_map", "_oco_flush_updates",
            "_oco_fetch_account_gtts", "_oco_handle_settled_pair",
            "_oco_cancel_survivor", "_oco_cancel_group_members",
            "_oco_gtt_is_live", "_oco_group_position_is_flat",
            "_oco_row_superseded_by_newer_fill",
            "_oco_cancel_closed_position_gtts",
            "_oco_strip_stale_sibling_pointers", "_oco_orphan_cancel_alert",
            "_oco_group_handle_sl_fired", "_oco_group_handle_ladder_closed",
            "_oco_group_prune_or_settle", "_oco_process_group_entry",
            "_oco_process_one_entry", "_oco_process_account_entries",
        ]
        for name in helper_names:
            assert not hasattr(bg, name), f"{name} must be fully removed"

    def test_not_registered_at_startup(self):
        """Neither task name appears as a real registration call in
        background.py's own source — not a bare substring grep, since
        `_task_agent_close_auto_release`'s own (Track-1, untouched)
        docstring legitimately names both tasks in prose."""
        import inspect
        import backend.api.background as bg

        src = inspect.getsource(bg)
        assert 'asyncio.create_task(_supervised(_task_trail_stop,' not in src
        assert 'asyncio.create_task(_supervised(_task_oco_pair_watcher,' not in src

    def test_track1_docstring_reference_untouched(self):
        """`_task_agent_close_auto_release`'s docstring legitimately names
        both removed tasks in a scheduling-convention comparison — this is
        Track-1 code and must not be edited by this sub-sprint."""
        import inspect
        import backend.api.background as bg

        src = inspect.getsource(bg._task_agent_close_auto_release)
        assert "_task_oco_pair_watcher" in src
        assert "_task_trail_stop" in src

    def test_background_module_imports_cleanly(self):
        import backend.api.background  # noqa: F401


# ---------------------------------------------------------------------------
# Item 2 — sim/gtt_book.py (SimGttBook) removal
# ---------------------------------------------------------------------------

class TestSimGttBookGone:
    def test_module_does_not_exist(self):
        with pytest.raises(ModuleNotFoundError):
            import backend.api.algo.sim.gtt_book  # noqa: F401

    def test_sim_driver_has_no_gtt_book_facade(self):
        from backend.api.algo.sim.driver import SimDriver

        for name in (
            "place_sim_gtt", "cancel_sim_gtt", "list_sim_gtts",
            "_check_sim_gtts", "_on_gtt_trigger",
        ):
            assert not hasattr(SimDriver, name), f"SimDriver.{name} must be removed"

    def test_sim_driver_snapshot_has_no_gtt_book_key(self):
        from backend.api.algo.sim.driver import SimDriver

        sim = SimDriver.instance()
        snap = sim.snapshot()
        assert "gtt_book" not in snap

    def test_sim_driver_init_has_no_gtt_book_attribute(self):
        from backend.api.algo.sim.driver import SimDriver

        sim = SimDriver.instance()
        assert not hasattr(sim, "_gtt_book")

    def test_replay_driver_has_no_gtt_projection_methods(self):
        from backend.api.algo.sim.replay_driver import SimReplayDriver

        for name in (
            "_replay_place_gtt", "_replay_trigger_gtt",
            "_replay_apply_gtt_event",
        ):
            assert not hasattr(SimReplayDriver, name), f"SimReplayDriver.{name} must be removed"

    def test_simulator_routes_module_has_no_gtt_routes(self):
        """`/api/simulator/gtt*` route handlers removed from the
        controller — confirmed via AST (not a bare substring grep, since
        the module's own comments legitimately mention the removal)."""
        import backend.api.routes.simulator as sim_routes

        tree = ast.parse(inspect_source(sim_routes))
        handler_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert "list_sim_gtts" not in handler_names
        assert "place_sim_gtt" not in handler_names
        assert "cancel_sim_gtt" not in handler_names

    def test_sim_and_replay_modules_import_cleanly(self):
        import backend.api.algo.sim.driver  # noqa: F401
        import backend.api.algo.sim.replay_driver  # noqa: F401
        import backend.api.routes.simulator  # noqa: F401


def inspect_source(module) -> str:
    import inspect
    return inspect.getsource(module)


# ---------------------------------------------------------------------------
# Item 3 — agent_grammar.yaml place_order/order Bracket params removed
# ---------------------------------------------------------------------------

class TestGrammarYamlPlaceOrderOrderClean:
    @staticmethod
    def _live_token(name: str) -> dict:
        from backend.api.algo import grammar
        for tok in grammar.SYSTEM_TOKENS:
            if tok.get("grammar_kind") == "action" and tok.get("token") == name:
                return tok
        raise AssertionError(f"{name} token not found in SYSTEM_TOKENS")

    @pytest.mark.parametrize("token_name", ["place_order", "order"])
    def test_no_bracket_params_on_token(self, token_name):
        schema = self._live_token(token_name)["params_schema"]
        for removed in (
            "template_id", "template_slug", "tp_pct_override",
            "sl_pct_override", "wing_premium_pct_override",
            "wing_strike_offset_override",
        ):
            assert removed not in schema, f"{token_name} still declares {removed}"

    @pytest.mark.parametrize("token_name", ["place_order", "order"])
    def test_no_mutex_groups_on_token(self, token_name):
        from backend.api.algo import grammar
        tok = self._live_token(token_name)
        assert tok.get("mutually_exclusive_groups") is None
        assert grammar.get_action_mutex_groups(token_name) is None

    def test_yaml_byte_for_byte_still_matches_golden_baseline(self):
        """Full-suite regression guard — the real
        test_system_tokens_byte_for_byte_equal_to_baseline (in
        test_agent_grammar_yaml_catalog.py) is the authoritative check;
        this re-confirms it specifically for place_order/order so a
        future diff that touches only this sub-sprint's area is caught
        here too."""
        import json
        from pathlib import Path
        from backend.api.algo import grammar

        baseline_path = (
            Path(__file__).parent / "golden" / "agent_grammar_baseline.json"
        )
        baseline = json.loads(baseline_path.read_text())
        live = {
            tok["token"]: tok
            for tok in grammar.SYSTEM_TOKENS
            if tok.get("grammar_kind") == "action"
            and tok.get("token") in ("place_order", "order")
        }
        base = {
            tok["token"]: tok
            for tok in baseline["system_tokens_full"]
            if tok.get("grammar_kind") == "action"
            and tok.get("token") in ("place_order", "order")
        }
        assert live == base


# ---------------------------------------------------------------------------
# Item 4 — residual Bracket passthrough re-check
# ---------------------------------------------------------------------------

class TestResidualPassthroughChecked:
    def test_orders_basket_route_has_no_bracket_references(self):
        """Confirmed already fully clean after T3.1 — no change needed in
        this sub-sprint. Re-asserted here so a future regression (e.g. a
        reverted edit) is caught."""
        import inspect
        import backend.api.routes.orders_basket as ob

        src = inspect.getsource(ob)
        for needle in ("template_id", "template_slug", "apply_template_to_order",
                       "attached_gtts", "wing_premium_pct"):
            assert needle not in src, f"orders_basket.py unexpectedly references {needle}"

    def test_lab_py_has_no_dangling_template_attach_import(self):
        """Confirmed already fixed by T3.1 (stubbed `_res_resolve_
        template_slug` to a no-op) — not re-done here. `template_slug`/
        `template_id` PARAM PLUMBING is deliberately left alone (MCP tool
        schema, out of this sub-sprint's scope); only the dangling import
        is asserted gone."""
        import inspect
        import backend.api.routes.lab as lab

        src = inspect.getsource(lab)
        assert "from backend.api.algo.template_attach import" not in src
        assert "import backend.api.algo.template_attach" not in src

    def test_simulator_py_gtt_routes_removed(self):
        """This WAS a real, live Bracket passthrough — `/api/simulator/
        gtt` accepted a `template_id` field. Removed in this sub-sprint
        (see TestSimGttBookGone above for the structural check)."""
        import inspect
        import backend.api.routes.simulator as sim_routes

        src = inspect.getsource(sim_routes)
        assert '@post("/gtt")' not in src
        assert '@get("/gtt")' not in src
        assert '@delete("/gtt/{gtt_id:str}"' not in src


# ---------------------------------------------------------------------------
# Item 5 — schemas.py struct cleanup
# ---------------------------------------------------------------------------

class TestSchemasCleanedUp:
    def test_order_template_schemas_removed(self):
        import backend.api.schemas as s

        for name in ("OrderTemplateOut", "OrderTemplateCreate", "OrderTemplatePatch"):
            assert not hasattr(s, name), f"schemas.{name} must be removed"

    def test_gtt_viewer_schemas_kept(self):
        """orders_gtt.py's confirmed-independent standalone viewer must
        be unaffected."""
        import backend.api.schemas as s

        for name in ("GttRow", "GttListResponse", "GttCancelResponse"):
            assert hasattr(s, name), f"schemas.{name} must NOT be removed"

    @pytest.mark.parametrize("struct_name,ctor_kwargs", [
        ("TicketOrderRequest", dict(mode="paper", side="BUY", tradingsymbol="X", quantity=1)),
        ("TicketPreviewRequest", dict(mode="paper", side="BUY", tradingsymbol="X", quantity=1)),
        ("BasketLeg", dict(tradingsymbol="X", exchange="NFO", transaction_type="BUY", quantity=1)),
    ])
    def test_override_fields_removed_but_template_id_kept(self, struct_name, ctor_kwargs):
        import backend.api.schemas as s

        cls = getattr(s, struct_name)
        fields = set(cls.__struct_fields__)
        removed = {
            "tp_pct_override", "sl_pct_override", "wing_premium_pct_override",
            "wing_strike_offset_override", "sl_trail_pct_override",
            "tp_scales_json_override", "hold_template_exit",
            "tp_order_type_override", "wing_max_spread_pct_override",
        }
        assert fields.isdisjoint(removed), (
            f"{struct_name} still has override field(s): {fields & removed}"
        )
        assert "template_id" in fields, (
            f"{struct_name}.template_id must be KEPT — the frontend/MCP "
            "may still send it until a later sub-sprint"
        )
        # Must still construct cleanly with no override kwargs.
        cls(**ctor_kwargs)

    def test_ticket_order_request_wire_decode_silently_drops_legacy_override_fields(self):
        """The still-unmodified frontend may keep sending these fields on
        every ticket submission until its own removal sub-sprint lands —
        msgspec must silently drop them on decode, never raise."""
        import json
        import msgspec
        import backend.api.schemas as s

        decoder = msgspec.json.Decoder(s.TicketOrderRequest)
        payload = json.dumps({
            "mode": "paper", "side": "BUY", "tradingsymbol": "X", "quantity": 1,
            "tp_pct_override": 20.0, "sl_pct_override": 10.0,
            "wing_premium_pct_override": 1.0, "wing_strike_offset_override": 2,
            "hold_template_exit": True, "sl_trail_pct_override": 5.0,
            "tp_scales_json_override": "[]", "tp_order_type_override": "MARKET",
            "wing_max_spread_pct_override": 3.0,
        }).encode()
        result = decoder.decode(payload)
        assert result.mode == "paper"
        assert not hasattr(result, "tp_pct_override")

    def test_orders_helpers_serializer_functions_removed(self):
        import backend.api.routes.orders_helpers as oh

        assert not hasattr(oh, "_ticket_overrides_dict")
        assert not hasattr(oh, "_build_overrides_json")

    def test_resolve_max_spread_pct_unaffected_by_serializer_removal(self):
        """`resolve_max_spread_pct` never depended on the two removed
        serializers — confirm it still resolves override > template >
        setting using a plain dict, the shape every real caller
        (`/spread-check`) already uses."""
        from backend.api.algo.spread_check import resolve_max_spread_pct

        value, source = resolve_max_spread_pct(
            {"wing_max_spread_pct": 7.0}, {"wing_max_spread_pct": 3.0},
        )
        assert value == 3.0
        assert source == "override"

    def test_schemas_module_imports_cleanly(self):
        import backend.api.schemas  # noqa: F401
