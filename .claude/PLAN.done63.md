# Plan: Track 3, Sub-sprint T3.6 (FINAL) — Schema migration + remaining dead-code residuals

## Task

Sixth and final sub-sprint removing Bracket/Template/GTT (full detail:
`~/.claude/plans/sparkling-dancing-lobster.md`, "Track 3" section — read it in full, especially
T3.1-T3.5's actual findings, all already shipped and live on prod). This is the irreversible step
— dropping a live table and columns from a production database — so it is sequenced last,
deliberately, and gets the most care of any sub-sprint in this track.

**Fresh investigation done before writing this plan (not assumed from memory) surfaced more
live, active residuals than the original T3.5-era framing expected. Read this section before
touching anything — the scope is real, not speculative:**

1. **`backend/api/algo/templates_seed.py` is STILL ACTIVELY SEEDING rows into `order_templates`
   on every single boot.** Called from `backend/api/database.py:1442`
   (`from backend.api.algo.templates_seed import seed_templates`) inside `init_db()`. It inserts
   the system "Default Bull/Bear/Short Vol/None" `OrderTemplate` rows every startup. **If the
   `order_templates` table is dropped without first removing this boot call, the very next
   deploy crashes on startup** (INSERT into a dropped table). This sequencing MUST be respected:
   stop the seed call in the same commit that drops the table, never as a follow-up.
2. **`OrderTemplate` (the ORM class, `backend/api/models.py:1670`, `__tablename__ =
   "order_templates"`) has live references across 7 files, not just `models.py`/`database.py`**:
   `backend/mcp/kite_server.py`, `backend/api/schemas.py`, `backend/api/algo/templates_seed.py`,
   `backend/api/routes/agents.py`, `backend/api/routes/lab.py` — in addition to `models.py` and
   `database.py`. Every one of these must be checked and cleaned, not just the two files the
   original T3.6 brief named.
3. **The MCP `place_order` tool's `template_slug` parameter is dead, and its docstrings are
   actively misleading — a genuine documentation bug on a real, callable tool, found live.**
   `backend/api/routes/lab.py`'s `_res_resolve_template_slug()` (~line 1061) is ALREADY a correct,
   honest no-op since T3.1 (confirmed: its own docstring says "Slug resolution was removed...
   silently dropped, never refused"). But the OUTER docstrings an MCP caller actually reads —
   `PlaceOrderRequest.template_slug`'s field comment (~line 244-251) and
   `backend/mcp/kite_server.py`'s `place_order` tool docstring (~line 750) — still claim real
   behavior: "Resolved server-side... to attach TP/SL/wing exit protection once this order
   fills." This is false today. Remove the `template_slug` parameter entirely from both the MCP
   tool signature and `PlaceOrderRequest` (matching T3.4's equivalent frontend removal), not just
   fix the wording — a parameter that does nothing should not exist, per this track's own
   standing convention (see judgment call 3 in the original Track 3 plan: "DSL schema param
   removed, not just the call site").
4. **Flagged by T3.5, now confirmed real, fold into this sprint:**
   - `backend/shared/helpers/settings.py:514`'s `hold.template_exit_released` seed row +
     `backend/api/routes/admin.py:1106-1209`'s `template_exit_hold_enabled` admin API field —
     `order_hold_gate.py`'s own comment confirms the one real reader (`template_exit_held()`) was
     removed in T3.1. This setting now does nothing; the admin API still accepts and persists it.
   - `backend/brokers/base.py` + the kite/dhan/groww adapters' `place_gtt`/`modify_gtt` methods —
     zero callers confirmed (only `get_gtts`/`cancel_gtt` are kept, for `orders_gtt.py`'s
     independent raw-GTT viewer — do NOT touch those two).
   - `backend/api/routes/orders.py`'s `ticket_preview` route — a stub since T3.1 (returns an
     empty plan), kept only because the frontend called it every Chain-tab field change. T3.3/
     T3.4 removed that frontend call. Confirm via grep that nothing calls `/orders/ticket/preview`
     anymore, then decide: delete the route, or leave the stub (your call — state which and why).
   - `backend/api/algo/template_registry.py` — NOT the same thing as `templates_seed.py`. This is
     the Sprint D2 `$ref` fragment registry (`SYSTEM_AGENT_TEMPLATES`, already emptied this
     session in the unrelated orphaned-agent cleanup). Confirm it's still needed for
     `Agent(kind='template')` custom rows (operator-authored `$ref` fragments, a LIVE, unrelated
     feature — do NOT delete this module, only confirm it has no remaining Bracket-specific
     content, which it shouldn't after the orphaned-agent cleanup already emptied the seed list).

## The actual schema drop

- `DROP TABLE IF EXISTS order_templates` (idempotent, matching the existing
  `_migrate_drop_position_pairs_table` precedent exactly — read that function in `database.py`
  first, line ~860, and copy its style/docstring convention).
- `ALTER TABLE algo_orders DROP COLUMN IF EXISTS template_id` (this column is a real FK to
  `order_templates.id` — dropping the column also drops its FK constraint, no separate step
  needed, but confirm this is true for this Postgres version before relying on it).
- `ALTER TABLE algo_orders DROP COLUMN IF EXISTS template_overrides_json`
- `ALTER TABLE algo_orders DROP COLUMN IF EXISTS attached_gtts_json`
- `ALTER TABLE algo_orders DROP COLUMN IF EXISTS template_hold_override`
- `ALTER TABLE agents DROP COLUMN IF EXISTS legacy_template_id` (also a real FK, same caveat)
- `ALTER TABLE agents DROP COLUMN IF EXISTS bracket_config`
- **Do NOT touch** `AlgoOrder.superseded_broker_order_ids_json` — confirmed chase-owned,
  unrelated (its own docstring says "2026-10-23 chase robustness"), never part of Bracket.
- **Do NOT touch** `AlgoOrder.hold_json` — generic, shared across all 3 hold categories
  (`EXPIRY_CLOSE`, `AGENT_ORDER`, `AGENT_CLOSE`), confirmed this session during the T3.4 review.
- Remove the corresponding `Mapped[...]` column declarations from `models.py` (both classes) and
  the `OrderTemplate` class itself (the whole class, now that its table is gone).
- Remove `templates_seed.py`'s boot call from `database.py:1442` AND delete `templates_seed.py`
  itself (confirmed its only purpose was seeding the now-dropped table).
- Update the stale comment at `models.py:1289-1290` ("`order_templates` stays the real FK anchor
  / id-stability table (kept in place, not dropped, per Sprint E's brief)") — this reasoning no
  longer applies now that Bracket is fully gone; remove or correct the comment in the same edit
  that removes the column it's attached to.

## Agents

- backend: implement the full "fresh investigation" section (items 1-4) AND the schema drop,
  in this order:
  1. **First**, remove `templates_seed.py`'s boot call and delete the file, remove the
     `template_slug` MCP parameter (lab.py + kite_server.py docstrings/signatures), remove the
     `template_exit_hold_enabled` setting/admin API field, remove `place_gtt`/`modify_gtt` from
     the broker ABC + all 3 adapters, decide `ticket_preview`'s fate. Run the full backend suite
     after this step alone, before touching the schema — confirm green before proceeding to the
     irreversible part.
  2. **Then** the actual `DROP TABLE`/`DROP COLUMN` migration function in `database.py`, plus the
     `models.py` class/column removals. Run the full backend suite again.
  3. Grep the ENTIRE `backend/` tree one more time for `OrderTemplate`, `order_templates`,
     `template_id`, `legacy_template_id`, `bracket_config`, `template_overrides_json`,
     `attached_gtts_json` (excluding `hold_json`/`superseded_broker_order_ids_json`, which stay)
     — confirm genuinely zero remaining references before calling this done.

  Write/update tests for every change, per this project's standing hard-gate rule. The schema
  migration itself needs a test proving it's idempotent (runs clean on a fresh DB with no
  `order_templates` table, and runs clean against a DB that still has the old table/columns) —
  mirror whatever test pattern (if any) exists for `_migrate_drop_position_pairs_table`.

  Do NOT touch: anything already covered by T3.1-T3.5 (all shipped, live, verified), the live
  Track-1 `exit-tp-short-option-90` infrastructure, `orders_gtt.py` and its `get_gtts`/
  `cancel_gtt` dependency, `template_registry.py`'s `$ref` fragment mechanism (a separate, live,
  unrelated feature — already emptied of Bracket content this session).

- backend-test: bundle into the backend agent's own work (same reasoning as every other Track 3
  sub-sprint — the test changes are tightly interleaved with the exact functions/schema being
  removed).
- frontend: skip
- broker: skip
- doc: skip
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

(one commit for items 1-4 and the pre-migration suite-green checkpoint, a SEPARATE commit for the
actual schema migration — keep the irreversible step individually revertable/reviewable, matching
every other Track 3 sub-sprint's convention of small, reviewable commits)

## Done when

- `templates_seed.py` is deleted, its boot call removed, zero remaining references to
  `OrderTemplate`/`order_templates` anywhere in `backend/` (grep-confirmed, not assumed).
- The MCP `place_order` tool no longer accepts `template_slug` — neither the tool signature nor
  `PlaceOrderRequest` carries it.
- `template_exit_hold_enabled` is gone from the admin API; `hold.template_exit_released` is no
  longer seeded as a setting.
- `place_gtt`/`modify_gtt` are gone from the broker ABC and all 3 adapters; `get_gtts`/
  `cancel_gtt` are confirmed still present and still passing their own tests (orders_gtt.py
  unaffected).
- `order_templates` table and the 6 named columns are dropped via an idempotent migration
  function, matching the `_migrate_drop_position_pairs_table` precedent.
- `superseded_broker_order_ids_json` and `hold_json` are confirmed untouched.
- Full backend suite green (run twice — once after the dead-code removal, once after the schema
  drop — not just once at the end).
- `git diff --name-only` touches only `backend/` — this is the one sub-sprint with no frontend
  component at all.
