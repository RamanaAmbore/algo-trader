# Plan: Separate rate-of-change loss alerts into critical-tier agents

## Context
Loss agents are hard-coded in `BUILTIN_AGENTS` in `agent_engine.py`. Currently:
- `loss-positions-acct` (HIGH tier) contains BOTH static threshold conditions (`pnl_pct <= -2.0`,
  `pnl <= -30000`) AND rate-of-change conditions (`pnl_rate_abs <= -3000`, `pnl_rate_pct <= -0.25`).
- Rate-of-change conditions measure ₹/min burn rate over a rolling window — a different, more urgent
  signal than static threshold breaches.
- The operator wants ALL rate-of-change conditions to fire as CRITICAL, but mixing them with static
  thresholds in a HIGH-tier agent prevents that.

ntfy is already wired: `_LOSS_AGENT_NTFY` dict + additive event sync populates per-agent ntfy events
with priorities on startup. Dispatch is gated by `is_enabled("ntfy")`, which is True in prod
(only disabled in `cap_in_dev`). The legacy `agent_alert.ntfy: false` in backend_config.yaml is
unused by the v2 engine.

The `sync_builtin_agents()` function preserves operator-tuned `conditions`, `cooldown_minutes`,
`events`, and `actions` on conflict — so operators can edit thresholds, cooldowns, and ntfy priority
from `/automation` without redeployment. This already works; the plan just improves the initial seed.

## Task
In `backend/api/algo/agent_engine.py`:

1. **Add `loss-rate-acct`** to `BUILTIN_AGENTS` — a new CRITICAL agent for per-account rate conditions:
   - slug: `"loss-rate-acct"`
   - tier: `"critical"`
   - topic: `"positions_loss"`
   - name: `"Positions per-account burn-rate guardrail"`
   - conditions: `{"any": [{"metric": "pnl_rate_abs", "scope": "positions.any_acct", "op": "<=", "value": -3000}, {"metric": "pnl_rate_pct", "scope": "positions.any_acct", "op": "<=", "value": -0.25}]}`
   - cooldown_minutes: 10 (shorter — rate conditions are time-sensitive)
   - ntfy priority: `"urgent"` (add to `_LOSS_AGENT_NTFY`)

2. **Remove rate conditions from `loss-positions-acct`** — keep only static thresholds:
   - Retain: `pnl_pct <= -2.0`, `pnl <= -30000`
   - Remove: `pnl_rate_abs <= -3000`, `pnl_rate_pct <= -0.25`

3. **Leave `loss-positions-total` unchanged** — it is already CRITICAL tier and already contains
   rate conditions (`pnl_rate_abs <= -6000`, `pnl_rate_pct <= -0.25`) which correctly fire critical alerts.

4. **Leave other agents unchanged** — `loss-margin-low`, `loss-funds-negative`,
   `loss-pos-total-auto-close` are unaffected.

No DB schema changes needed — `agents` table already has all required columns
(`cooldown_minutes`, `tier`, `topic`, `events` JSONB, `is_system`).

## Agents
- backend: In `backend/api/algo/agent_engine.py`:
  (a) Add `loss-rate-acct` dict to BUILTIN_AGENTS (insert after `loss-positions-acct`):
      slug="loss-rate-acct", tier="critical", topic="positions_loss",
      name="Positions per-account burn-rate guardrail",
      long_name="when:positions.any_acct.pnl_rate critical/tg+ntfy+log do:notify-only",
      conditions={"any": [{"metric":"pnl_rate_abs","scope":"positions.any_acct","op":"<=","value":-3000},
                           {"metric":"pnl_rate_pct","scope":"positions.any_acct","op":"<=","value":-0.25}]},
      cooldown_minutes=10 (override default of 30).
  (b) In `_LOSS_AGENT_NTFY` dict (lines 913-919), add: `"loss-rate-acct": "urgent"`.
  (c) In `loss-positions-acct` conditions (lines 805-810), remove the two rate lines
      (pnl_rate_abs and pnl_rate_pct entries). Keep only pnl_pct and pnl static thresholds.
  For every file changed, write or update at least one test covering the changed behaviour.
  This is mandatory — not optional.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: In `backend/tests/` add tests (new file `test_loss_agents.py` or extend existing):
  - Assert `loss-rate-acct` is in BUILTIN_AGENTS, tier=="critical", cooldown_minutes==10
  - Assert `loss-rate-acct` events include ntfy channel with priority=="urgent"
  - Assert `loss-positions-acct` conditions contain NO pnl_rate_abs/pnl_rate_pct entries
  - Assert `loss-positions-total` conditions still include rate entries (unchanged)
  - Assert `_LOSS_AGENT_NTFY["loss-rate-acct"] == "urgent"`
  For every file changed, write or update at least one test covering the changed behaviour.
  This is mandatory — not optional.
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
feat(agents): split per-account rate-of-change into critical-tier loss-rate-acct agent

## Done when
- `loss-rate-acct` (critical, cooldown 10 min, ntfy urgent) is in BUILTIN_AGENTS
- `loss-positions-acct` conditions contain only static threshold conditions (no rate metrics)
- `loss-positions-total` (critical, already has rate conditions) is unchanged
- ntfy fires for all loss agents in prod (already wired — no config change needed)
- Operator can tune thresholds, cooldowns, tier, ntfy priority from `/automation` without redeploy
- pytest green
