# Plan: Market-cycle event tokens for the agent grammar DSL

## Context

The operator wants the agent DSL to be able to refer to market-cycle timeline events (05:30 IST
holiday/MCX-evening refresh, 08:00 IST broker-connectivity window, MCX open/close, non-MCX
open/close) as registered tokens, so agents and alerts can be built against these events
declaratively instead of hardcoded HH:MM strings scattered per-agent. The immediate trigger:
Item 1 of the prior round corrected MCX's open time (09:00→09:30), which required editing a
shared config value AND multiple duplicated constants (`backend_config.yaml`,
`frontend/src/lib/marketHours.js`'s `MCX_OPEN_MIN`) — a token-based SSOT would mean a future
correction like this touches one place, not several.

A prior scoping investigation (read-only, this session) found this DSL already supports exactly
this kind of plug-and-play token for conditions/metrics (`GrammarRegistry`'s `_TOKEN_LOADERS`,
keyed `(grammar_kind, token_kind)` — e.g. `('condition','metric')`). The scoping conclusion,
carried into this plan: **don't touch `fire_at_time` scheduling** — that's a separate, simpler
mechanism (a literal HH:MM clock match) that the three existing schedule-only agents
(`market-open-nse`, `market-preclose-mcx`, `market-status-report`) correctly use as-is and don't
need migrating. This plan is about **new condition/metric tokens**, which let a *future* agent
trigger on a state change ("alert the instant MCX opens") rather than a fixed clock time — a
capability that doesn't exist today.

## What already exists (reuse, don't reinvent)

`_ae_segment_flags()` (`backend/api/algo/agent_engine.py`) computes `nse_open`/`nse_closed`/
`mcx_open`/`mcx_closed`/`minutes_since_nse_open`/`minutes_since_mcx_open`/etc. into the per-tick
agent context dict EVERY cycle — but **zero grammar tokens currently read any of it** (confirmed
dormant via the existing CLAUDE.md "Segment flags honour overrides" note, which already flags
this as unread). The only token that reads anything schedule-related today is the generic,
segment-agnostic `minutes_since_open`/`minutes_until_close` (`grammar.py:566`), which reads
`alert_state['session_start']`, not this context dict.

The 05:30 and 08:00 boundaries do NOT exist as computed fields anywhere today — these need small
new logic, not just exposure of existing data.

## Scope

1. **Pre-flight verification (do this FIRST, before writing any token)** — confirm
   `_ae_segment_flags()`'s dict actually carries the close-side keys
   (`minutes_until_mcx_close`/`minutes_until_nse_close`) as named; the existing CLAUDE.md comment
   only explicitly documents the open-side ones. Also re-verify this function's output is still
   correct after the prior round's MCX 09:00→09:30 config change (same `market_segments` config
   both read) — this function was NOT touched by that round and could have gone stale if it
   caches or derives timing independently rather than reading the YAML live.

2. **New condition/metric tokens** (backend/config/grammars/agent_grammar.yaml catalog +
   resolver wiring in grammar.py, same shape as the existing `minutes_since_open` token):
   - `is_mcx_open`, `is_nse_open` — boolean, reads `ctx.mcx_open`/`ctx.nse_open`.
   - `minutes_since_mcx_open`, `minutes_since_nse_open` — reads the existing computed fields.
   - `minutes_until_mcx_close`, `minutes_until_nse_close` — reads the existing computed fields
     (pending verification step 1 above confirms these exist under these exact names).
   - `minutes_until_mcx_open`, `minutes_until_nse_open` — new computed values (05:30/08:00-style
     boundaries don't have a "minutes until" today; compute from `market_segments` config +
     current IST time, same pattern `_ae_resolve_matched_window()` already uses elsewhere).

3. **New tokens for the two non-segment boundaries** (05:30 refresh, 08:00 window-open) — small
   new logic, not a reuse of existing data:
   - `is_past_holiday_refresh` (05:30 IST boundary passed today) — reads
     `backend_config.yaml:holiday_refresh_time`.
   - `is_past_window_open` (08:00 IST broker-connectivity boundary passed today) — the boundary
     `is_segment_pending_open()` (new in the prior round, `snapshot_gate.py`) already implicitly
     straddles; expose it directly rather than re-deriving.

4. **Golden-baseline test update** — per the established workflow
   (`test_agent_grammar_yaml_catalog.py`), hand-amend `agent_grammar_baseline.json` with each new
   token's live-derived dict and add a docstring "Amendment" paragraph, same recipe used for
   `send_summary`/`send_market_status` this session.

5. **No migration needed** — confirmed in the scoping investigation: `market-open-nse`,
   `market-preclose-mcx`, and `market-status-report` stay exactly as they are (schedule-only
   sentinels on a fixed clock time). These new tokens are purely additive, for a future agent
   that wants condition-based triggering.

6. **Documentation** — one CLAUDE.md entry (likely folding into or adjacent to the existing
   "Segment flags honour overrides" note, since this directly resolves that note's "dormant, no
   live reader" callout) documenting the new tokens and that they're the first real consumers of
   `_ae_segment_flags()`'s computed dict.

## Files

- `backend/api/algo/grammar.py` — new token resolver functions (metric kind), mirroring
  `_metric_minutes_since_open`'s exact pattern.
- `backend/config/grammars/agent_grammar.yaml` — new token catalog entries.
- `backend/tests/golden/agent_grammar_baseline.json` + `test_agent_grammar_yaml_catalog.py` —
  hand-amended per the established recipe.
- `backend/api/algo/agent_engine.py` — only touched if step 1's verification finds the close-side
  keys are missing/misnamed and need adding to `_ae_segment_flags()`.
- `backend/tests/test_grammar_registry.py` or a new `test_market_cycle_tokens.py` — unit tests
  per new token, covering both the true and false/boundary cases (e.g. `is_mcx_open` at 09:29 vs
  09:31 IST).

## Agents

- backend: all of the above.
- backend-test: pytest for the new tokens.
- doc: CLAUDE.md entry.

## Tests

- pytest: yes
- svelte-check: no (backend-only)
- playwright: no

## Commit message

feat(grammar): register market-cycle event tokens (MCX/NSE open-close, 05:30/08:00 boundaries) as agent DSL condition/metric tokens

## Done when

- `is_mcx_open`/`is_nse_open`/the minutes-since/until tokens and the two new boundary tokens
  resolve correctly in a test agent's condition, verified against both the true and false side of
  each boundary.
- Golden-baseline catalog test green with the new tokens hand-amended in.
- No existing agent (`market-open-nse`/`market-preclose-mcx`/`market-status-report`) is touched —
  confirmed via their own existing tests still passing unchanged.
- Backend pytest green.
