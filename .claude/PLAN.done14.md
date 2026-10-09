# Plan: Fix `_grammar_snapshot()` AttributeError + match agents-row styling to automation templates

## Context

Two independent, small fixes requested back-to-back this session:

**1. Backend bug** — the Phase-3 grammar-registry research (earlier this
session) flagged a real, live `AttributeError` as an explicitly out-of-scope
side finding: `backend/api/algo/agent_ai.py`'s `_grammar_snapshot()` (line 98)
does `for token in REGISTRY.tokens.values():`, but `GrammarRegistry`
(`backend/api/algo/grammar_registry.py`) has no `tokens` attribute anywhere —
confirmed by reading the class (`__init__` only defines `metrics`/`scopes`/
`operators`/`channels`/`formats`/`templates`/`actions`/`log_tags`, never
`tokens`). `_grammar_snapshot()` feeds the Lab-chat agent-builder's system
prompt (`agent_ai.py:333`), so every real call raises, with no test catching
it — the 2 existing tests (`test_agent_ai_coverage.py:137,176`) mock the
entire `REGISTRY` object away (`patch('backend.api.algo.grammar_registry.REGISTRY')`
+ `mock_registry.tokens = {...}`), so they only prove the function's own
branching logic is correct, never that the real registry actually has the
attribute it reads.

**2. Frontend styling** — operator: "make agents row background look similar
to agent templates in automation." The automation agent list
(`frontend/src/routes/(algo)/automation/+page.svelte:1657`) wraps each agent
in `<div class="algo-status-card ...">`. Read `app.css` in full for this
class: it has **no background/border/radius of its own** — only
`.algo-status-card[data-status="..."]` variants exist, and those only set
`--st-fg`/`--st-bg`/`--st-border` custom properties consumed by the nested
`.algo-status-pill` badge, never by the card container itself (confirmed via
exhaustive grep — every line mentioning `algo-status-card` in `app.css` was
read). So agent rows render with no visible card chrome, blending into the
page background. The sibling page the operator is comparing against,
`automation/agent-templates/+page.svelte`, has a genuinely bordered/
gradient-backed row via its own scoped `.frag-row` class (line 438):
`background: linear-gradient(180deg, #0f1729 0%, #0a1020 100%); border: 1px
solid rgba(126,151,184,0.10); border-radius: 0.3rem; overflow: hidden;` plus
`.frag-row:hover { border-color: rgba(251,191,36,0.25); }`.

`.algo-status-card` is a **shared global class** used on 9+ other pages
(MarketPulse, OrderBook, OrderCard, dashboard, admin/brokers, admin/tokens,
admin/derivatives, SimulatorPanel, automation/templates) — per Scope
Discipline, the fix must NOT touch the global class definition in `app.css`,
only scope the new look to the automation page's own agent rows. Svelte's
built-in CSS scoping does this for free: a `.algo-status-card { ... }` rule
written inside `automation/+page.svelte`'s own `<style>` block only matches
elements rendered by that component (Svelte adds a scoping class), so no
`:global()` and no risk to the other 9 pages.

## Approach

**Backend fix**

1. `backend/api/algo/grammar_registry.py` — add `self.tokens: dict[int, Any] = {}`
   to `GrammarRegistry.__init__` (alongside the existing `self.metrics`/etc.,
   with a one-line comment: raw `GrammarToken` rows keyed by `id`, kept ONLY
   so `agent_ai.py`'s `_grammar_snapshot()` can render full per-token metadata
   — description/value_type/params_schema — which the processed per-kind
   dispatch tables above don't uniformly carry; never used for dispatch).
   In `reload()`, right after `rows = (await s.execute(...)).scalars().all()`,
   build `tokens_by_id = {r.id: r for r in rows}`; inside the existing
   `with self._lock:` block (alongside the other attribute assignments),
   add `self.tokens = tokens_by_id`. No new DB query — reuses `rows`, which
   is already fetched.
2. No change needed to `agent_ai.py`'s `_grammar_snapshot()` or the 2 existing
   mocked tests — both already expect `REGISTRY.tokens` to be a dict
   supporting `.values()`, which this fix satisfies exactly (keying by `id`
   avoids any risk of a `token` string colliding across different
   grammar_kind/token_kind pairs, since `GrammarToken.token` has no global
   uniqueness constraint in `models.py`).
3. Add ONE new regression test that does NOT mock `REGISTRY` away — the gap
   that let this bug ship. Call the real `await REGISTRY.reload()` (follow
   whichever existing test already exercises a real/test-DB `reload()` —
   `backend/tests/test_agent_evaluator.py`'s docstring references this
   pattern; locate and reuse its exact DB-session fixture), then call the
   real `_grammar_snapshot()` against that real, reloaded registry and
   assert it returns without raising and that `snap["actions"]` is
   non-empty (action tokens are always seeded). Put it in
   `backend/tests/test_grammar_registry.py` (the dedicated registry test
   file) or `backend/tests/test_agent_ai_coverage.py` — whichever already
   has the right DB fixture available; check both before picking.

**Frontend fix**

4. In `frontend/src/routes/(algo)/automation/+page.svelte`'s own `<style>`
   block, add a scoped `.algo-status-card` rule matching
   `automation/agent-templates/+page.svelte`'s `.frag-row` look exactly
   (same literal gradient/border/radius values, for genuine visual parity —
   not the more common `var(--card-bg-gradient)` token used by `.algo-card`
   elsewhere, which is a visibly different, lighter navy):
   ```css
   .algo-status-card {
     background: linear-gradient(180deg, #0f1729 0%, #0a1020 100%);
     border: 1px solid rgba(126,151,184,0.10);
     border-radius: 0.3rem;
     overflow: hidden;
     transition: border-color 0.08s;
   }
   .algo-status-card:hover { border-color: rgba(251,191,36,0.25); }
   ```
   Do not add `padding` — the existing inline `style="padding: 0"` on the
   element already overrides it, matching `.frag-row`'s own approach (its
   inner `.frag-head` carries the real padding). Because this rule lives in
   this page's own `<style>` block, Svelte's scoping guarantees it affects
   only this page's `.algo-status-card` instances — `app.css`'s global
   class, and all 9 other pages using it, are untouched.
5. Verify no visual regression to the existing `[data-status="..."]`
   variants — they only set `--st-fg`/`--st-bg`/`--st-border` (consumed by
   the nested `.algo-status-pill`), never the card's own background/border,
   so this new rule and those variants don't conflict.

## Files

- `backend/api/algo/grammar_registry.py` — add `self.tokens` dict, populated
  in `reload()`.
- `backend/tests/test_grammar_registry.py` or `test_agent_ai_coverage.py` —
  one new real-registry regression test (not mocked) for `_grammar_snapshot()`.
- `frontend/src/routes/(algo)/automation/+page.svelte` — scoped
  `.algo-status-card` style rule in its own `<style>` block.

## Verification

- `cd backend && venv/bin/python -c "from backend.api.algo import grammar_registry, agent_ai"` —
  clean import.
- Full `pytest backend/tests/` green, including the new test exercising a
  real (not mocked) `REGISTRY.reload()` → `_grammar_snapshot()` call.
- `npx svelte-check --output machine` — 0 new errors.
- Visually: load `/automation` locally/dev and confirm agent rows now show
  the same dark-navy-gradient bordered card look as `/automation/agent-templates`
  rows; confirm the other 9 pages using `.algo-status-card` are visually
  unchanged (spot-check MarketPulse, dashboard, admin/brokers at minimum).
- A Playwright source-level spec (matching this session's established
  pattern) asserting: (a) the new `.algo-status-card` rule exists scoped
  inside `automation/+page.svelte`'s own `<style>` block with the exact
  gradient/border values matching `.frag-row`'s, and (b) `app.css`'s global
  `.algo-status-card` definition is unchanged (no new background/border
  added there) — guards against a future edit accidentally "fixing" this
  globally instead of per-page.

## Done when

`REGISTRY.tokens` exists and is populated by `reload()`; `_grammar_snapshot()`
runs without `AttributeError` against a real (non-mocked) registry, proven by
a new test; automation's agent rows visually match agent-templates' row
styling; the global `.algo-status-card` class and the other 9 pages using it
are untouched; full pytest + svelte-check green.
