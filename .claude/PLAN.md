# Plan: Signed per-underlying divergence alert, cross-underlying cooldown fix, alert-text conciseness audit

## Task

Redesign `loss-positions-underlying-divergence` (backend/api/algo/agent_engine.py) to be a signed
(market − theoretical) metric instead of unsigned abs(), per operator's "toward/away from max
profit" framing — confirmed via 6-perspective council review that this must reuse the EXISTING
theoretical-vs-market baseline machinery (no new intrinsic-value formula), and that
`loss-positions-total`'s cash rate-of-change leaves (`pnl_rate_abs`/`pnl_rate_pct`) and static
`day_val`/`day_pct` thresholds must stay untouched (near-unanimous council finding: a direction-
aware intrinsic-value replacement would be blind to theta/vega bleed and would regress the
operator's own standing "never narrow rate-of-change coverage" instruction). Also fixes a confirmed,
separate pre-existing bug: the divergence agent's nonzero `cooldown_minutes` triggers the generic
agent-level cooldown gate (`_cycle_in_cooldown`), silencing a genuinely different underlying's
breach for the whole cooldown window once any one underlying fires — fix is `cooldown_minutes: 0`
(same precedent as `exit-tp-short-option-90`), relying on the per-leaf escalation gate's own
~30-min fallback for per-underlying dedup instead. Full design rationale: `~/.claude/plans/lovely-
stirring-allen.md` (already operator-approved).

Separately, fold in a full alert-text conciseness audit (operator: "audit all alert text for
relevant info make it concise without losing consistency and necessary info") — results from 3
parallel read-only audit forks covering `event_agents.py` renderers, `alert_utils.py` shared
building blocks, and the rich-alert table/trailing-tag machinery in `agent_engine.py`. Apply
concrete, low-risk trims only — never cut the load-bearing origin line, masked accounts, real
breach numbers, or dual-tz timestamp.

Also includes two already-shipped-to-workshop same-session fixes needing a correction: the
in-session edit that bumped the divergence agent's cooldown to 45 (wrong direction per the newly-
found bug above — must be 0) and its matching `_ae_sync_divergence_cooldown` migration (must
force-sync stock `10` → `0`, not `45`).

## Agents

- backend: skip — implementing directly (orchestrator already has full council-review context;
  re-briefing a fresh backend agent would duplicate that context for no benefit on a single-file,
  well-scoped change).
- frontend: skip
- broker: skip
- doc: skip — CLAUDE.md update for the divergence-agent redesign folded into this same pass if the
  existing "Alert evaluation and latching" / loss-agent entries need a correction note.
- backend-test: skip — tests written directly alongside the implementation (same reasoning as backend).
- playwright: skip — backend-only change, no frontend surface.

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

```
fix(agents): sign the per-underlying divergence alert; fix cross-underlying cooldown silencing

loss-positions-underlying-divergence redesigned per operator request: signed
(market - theoretical) instead of abs(), so it only fires when actual results
underperform the Greeks/theta-model-predicted path, never when outperforming
it. Reuses the existing theoretical-vs-market baseline machinery unchanged.

Also fixes a confirmed pre-existing bug (same defect class exit-tp-short-
option-90 already worked around): the agent's nonzero cooldown triggered the
generic agent-level cooldown gate, silencing a genuinely different
underlying's breach for the whole cooldown window once any one underlying
fired. cooldown_minutes set to 0; per-underlying dedup now applies via the
per-leaf escalation gate's own ~30-min fallback instead.

loss-positions-total's rate-of-change leaves are deliberately left untouched
- a 6-perspective council review found replacing them with a direction-aware
intrinsic-value metric would be blind to theta/vega bleed and would regress
the operator's own prior "never narrow rate-of-change coverage" instruction.

Also trims alert body text for conciseness per audit findings, and corrects
the earlier in-session cooldown edit (45 -> 0, matching the bug fix above).
```

## Done when

- `underlying_loss_divergence_signed` metric exists, registered in the grammar + golden baseline.
- `loss-positions-underlying-divergence` fires only on negative (adverse) divergence, cooldown=0,
  restart-sync migration force-upgrades an already-seeded stock row.
- Cross-underlying regression test (two different underlyings breaching within one old cooldown
  window both fire) passes, confirmed to fail before the fix.
- `loss-positions-total` is byte-for-byte unchanged.
- Alert-text audit findings applied where low-risk; full backend suite green; CC gate clean.
- Committed to `workshop`, then pushed through `dev` and `main` per explicit operator "deploy"
  instruction.
