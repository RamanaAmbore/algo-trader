# Plan: Split expiry pre-close alert/auto-close into exchange-pure MCX/NFO agents

## Task

`expiry-day-preclose-alert` and `expiry-day-itm-auto-close` (backend/api/algo/agent_engine.py,
`_EXPIRY_DAY_AWARENESS_AGENTS`) are each ONE shared agent spanning BOTH MCX and NFO via a single
flat condition token (`is_expiry_preclose_alert_window` / `is_expiry_preclose_close_window`) with
no exchange identity. Confirmed live incident tonight (2026-11-26, ~22:50 IST): fired for MCX with
a self-contradicting body ("a position expires today, within 65 minutes of that exchange's close"
+ "No MCX F&O positions are expiring today — nothing to close"), because the underlying
`is_mcx_expiry_today`/`is_nse_expiry_today` gate uses a ≤1.5-day lookahead buffer (not exact
"today"), so it can go true for MCX purely because a position expires TOMORROW, while the renderer
correctly filters to exact `expiry == today` and finds nothing.

Mirrors the `expiry-nfo-risk-alert`/`expiry-mcx-risk-alert` split shipped earlier today (same
session) for the identical failure mode (merged 2026-11-19, split back 2026-11-26 after an
identical live incident). Full audit of every BUILTIN_AGENTS entry in agent_engine.py confirms this
is the ONLY remaining combined-exchange agent pair. Full design: `~/.claude/plans/lovely-stirring-
allen.md` (operator-approved).

Two fixes, bundled:
1. **Root cause** — new `_is_exchange_expiring_exactly_today(ctx, exchange)` helper in
   `metric_is_expiry_preclose_alert_window.py` (reuses `_parsed_or_none`/`days_to_expiry`/
   `close_time_for_exchange`, requires `days_to_expiry < 1.0` not `<= 1.5`). Used by
   `_expiry_preclose_exchanges()` AND `_metric_factory_is_expiry_morning_preview_window()` (the
   morning-preview agents share the same day-early self-contradiction risk, though not the
   cross-exchange conflation, since they're already exchange-pure). `is_mcx_expiry_today`/
   `is_nse_expiry_today` themselves stay UNCHANGED (their ≤1.5-day semantic is correct and used
   elsewhere).
2. **Split** — RETIRE `expiry-day-preclose-alert`/`expiry-day-itm-auto-close`, RESHIP as 4
   exchange-pure agents: `expiry-day-mcx-preclose-alert`, `expiry-day-nse-preclose-alert`,
   `expiry-day-mcx-itm-auto-close`, `expiry-day-nse-itm-auto-close`. New per-exchange metric tokens
   (`is_{mcx,nse}_expiry_preclose_{alert,close}_window`), each a parameterized factory binding
   mirroring `metric_is_expiry_morning_preview_window.py`'s own style. Each new agent gets its own
   `topic` (must not collide with `expiry_warning_mcx`/`expiry_warning_nfo` already used by the
   risk-alert pair, nor the shared `expiry_warning` the morning-preview agents use). `cooldown_minutes`
   stays 30 each (unchanged periodic-rescan reasoning). New single-exchange render/action functions
   mirroring the EXISTING `_v2_expiry_morning_preview(context, exchange)` shape exactly — delete
   `_v2_expiry_position_breakdown()` and the combined action path once unreferenced.

**Pre-deploy safety check (devil's-advocate finding, non-negotiable):** `expiry-day-itm-auto-close`
is tier `critical`, `trade_mode: "live"`, stages real HELD orders. MCX's own pre-close window is
active tonight until ~23:55 IST (confirmed current time 23:06 IST). Do NOT push to dev or main
(even though this /depl run was explicitly requested) until after 23:55 IST tonight — restarting
mid-window risks a duplicate re-fire pattern via cold cooldown/latch state. Commit to `workshop`
only (Phase 1) now; hold Phase 2 (ddev push) and Phase 3 (dprod merge+push) until after 23:55 IST.

Also verify during implementation (devil's-advocate finding): no order-release/reconcile path does
a hard `Agent` lookup that raises on a missing `agent_id` after the old agent rows are pruned —
confirm this degrades to a cosmetic fallback label everywhere, same as the rest of this codebase's
alert-identity resolution, not an exception. **Confirmed during implementation**: `resolve_origin()`/
`_fetch_agent_name_map()` degrade to a generic `"Agent #{id}"` label, never raise. The one place a
missing agent has a real functional effect is `_agent_close_auto_release_sweep_once()`'s kill-switch
in `background.py` — a missing agent row skips auto-release (stays HELD, needs manual release)
rather than raising. Confirmed via direct query on both prod (agent ids 97/98) and dev (178/179)
before shipping: zero currently-HELD rows reference either retired agent id.

## Agents

- backend: skip — implementing directly (orchestrator already has full investigation + council
  context from this session; re-briefing a fresh agent would duplicate it for no benefit on this
  well-scoped, single-area change).
- frontend: skip
- broker: skip
- doc: skip — CLAUDE.md update folded into this same pass (the "Expiry pre-close alert + auto-close"
  entry needs a correction note for the split).
- backend-test: skip — tests written directly alongside the implementation.
- playwright: skip — backend-only change, no frontend surface.

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

```
fix(agents): split expiry pre-close alert/auto-close into exchange-pure MCX/NFO agents

expiry-day-preclose-alert and expiry-day-itm-auto-close each shared one
condition spanning both MCX and NFO with no exchange identity. Combined
with a <=1.5-day lookahead buffer in the underlying is_{mcx,nse}_expiry_
today gate, this let the window fire for MCX purely because a position
expires TOMORROW, while the renderer correctly checked exact today and
found nothing - producing a self-contradicting "a position expires
today" / "No MCX positions are expiring today" message.

Mirrors the expiry-nfo-risk-alert/expiry-mcx-risk-alert split shipped
earlier today for the identical failure mode. Retired the two combined
agents, re-shipped as four exchange-pure ones (mcx/nse preclose-alert,
mcx/nse itm-auto-close), each with its own topic and a new, narrower
exact-today check shared with the morning-preview agents.

Full audit of every BUILTIN_AGENTS entry confirmed this was the only
remaining combined-exchange agent pair in the system.
```

## Done when

- `_is_exchange_expiring_exactly_today` exists and is used by both the preclose-window and
  morning-preview factories; `is_mcx_expiry_today`/`is_nse_expiry_today` byte-for-byte unchanged.
- 4 new exchange-pure agents replace the 2 combined ones; grammar YAML + golden baseline updated;
  `_ae_prune_retired_builtins` covers the 2 old slugs.
- Regression test: an MCX (or NFO) position expiring TOMORROW does not satisfy its exchange's own
  preclose window today — confirmed to fail before the fix.
- Same-day MCX-then-NFO (and reverse) no-suppression regression preserved per-exchange.
- Full backend suite green (11628 passed, 0 failed); CC gate clean on every changed file.
- Committed to `workshop`. Push to `dev`/`main` held until after 23:55 IST tonight (MCX close) —
  do not push early even under /depl.

## Outcome (filled in after implementation)

- All 4 new agents implemented, 4 new per-exchange tokens registered additively in
  `agent_grammar.yaml`/`agent_grammar_baseline.json` (old combined tokens kept registered, unused,
  not verified dead for every possible custom agent — flagged, not removed).
- `_action_live_expiry_auto_close` needed zero code changes — it already supported `params.exchange`
  narrowing; only the new agents' own `actions` entries needed to set it.
- 12 additional pre-existing test files referenced the old combined slug/metrics directly and needed
  updating beyond the two files anticipated in the original plan: `test_agent_action_fanout.py`,
  `test_builtin_status_sync_preserves_operator_choice.py`, `test_expiry_close_release_finding20.py`,
  `test_expiry_close_release_e2e.py` (fixture bug found: relied on the OLD 1.5-day buffer with a
  "day before expiry" frozen_now — fixed to the real expiry date itself, which is the CORRECT fix
  given the new exact-today semantic, not a workaround), `test_expiry_logic.py`,
  `test_expiry_resolvers.py`, `test_mcx_preclose_agent.py`, `test_seed_ships_inactive_guard.py`.
- Full backend suite confirmed green after all fixes: 11628 passed, 0 failed, 75 skipped, 4 xfailed,
  2 xpassed. CC gate clean across the whole backend tree.
- Devil's-advocate's two real findings were both verified empirically (not just reasoned about):
  the agent-lookup degradation is genuinely cosmetic, and zero live HELD orders were at risk.
- Committed to `workshop` at 23:3x IST, ~20 minutes before MCX's 23:55 close. Push to dev/main
  explicitly held until after that close, per the plan's own gate.
