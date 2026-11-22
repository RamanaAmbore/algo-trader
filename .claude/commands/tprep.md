# /tprep — Batch test-case creation, execution, and fix-to-green

Unattended test-prep pass: find coverage/test gaps against the project's OWN current
rules (read live from CLAUDE.md, never hardcoded here), write the missing tests,
run them, and fix whichever side is wrong (test or code) until everything is green
and the documented coverage gates are met. Designed to run once a day (manual
invocation, or scheduled via cron/`ScheduleWakeup` if the operator sets that up
separately — this file does not self-schedule).

No per-step confirmation — this command is explicitly for unattended, cost-efficient
batch execution. Use `haiku`-model agents for the actual write/fix loop wherever the
project's own model-selection rule allows it (CLAUDE.md Model Usage table already
designates `backend-test`/`playwright` as `haiku` — reuse those two agent types, do
not spin up `sonnet` agents for routine test authoring).

## Step 1 — Read current constraints (do this every run, never cache across days)

1. Read `CLAUDE.md`'s **Test Coverage Rules (Hard Gates)** section for:
   - the per-file test location map (which source path maps to which test path/framework)
   - the current coverage thresholds for `backend/brokers/` and `backend/api/`
2. Read `CLAUDE.md`'s **Development Commands** section for the exact pytest/vitest/
   svelte-check invocations already in use (reuse them verbatim — do not invent new
   flags).

## Step 2 — Find the gaps

1. `git diff main...HEAD --name-only` (or `workshop` if `HEAD` already is `workshop`)
   — any source file changed since the last prod sync with no matching test change
   in the same range is a gap per the per-file test location map.
2. Run the full coverage report (background, `run_in_background: true`):
   ```
   cd /Users/ramanambore/projects/ramboq && \
     venv/bin/pytest backend/tests/ -q --tb=line \
       --cov=backend/brokers --cov=backend/api --cov-report=term-missing
   ```
   Parse `--cov-report=term-missing`'s per-file "Missing" line ranges. Rank files
   below their own gate (brokers < threshold, api < threshold) AND any file with
   conspicuously low coverage relative to its own history, highest-opportunity
   (most missing lines in a single file) first.
3. Do the same for the frontend: `cd frontend && npx vitest run --coverage 2>&1`
   (background) if a coverage reporter is configured; otherwise fall back to
   `grep`-ing for `.svelte`/`.js` files under `frontend/src/lib/` with no sibling
   test in `frontend/src/lib/__tests__/` or `frontend/e2e/`, per the per-file test
   location map.

## Step 3 — Batch dispatch (parallel, background, haiku model)

1. Group gaps by file-overlap (CLAUDE.md's own "bundle related files into one
   agent" rule) — never split two gaps that touch the same module across two
   agents.
2. Dispatch `backend-test` (pytest gaps) and `playwright` (frontend e2e gaps) agents
   in a single message, one Agent call per independent group, `model: haiku`
   (or omit `model` and rely on the agent definition's own `haiku` default — check
   `~/.claude/agents/backend-test.md` / `~/.claude/agents/playwright.md` first; only
   pass an explicit `model` override if the definition doesn't already pin it).
3. Each agent's brief: the specific file(s), the specific missing line ranges, and
   an instruction to write a REAL test that exercises that code (never a stub that
   only imports the module to inflate coverage numbers). Follow CLAUDE.md's "Test
   specs assert five quality dimensions" memory guidance where it applies (SSOT,
   performance, stale-code, reusable-component usage, UX consistency) — but the
   primary goal here is closing a genuine coverage/behavior gap, not a full audit.

## Step 4 — Loop to green (max 5 iterations)

1. Run the full gate set in parallel, background:
   - `venv/bin/pytest backend/tests/ -q --tb=line --cov=backend/brokers --cov=backend/api --cov-report=term-missing`
   - `venv/bin/coverage report --include="backend/brokers/*" --fail-under=<current threshold from Step 1>`
   - `venv/bin/coverage report --include="backend/api/*" --fail-under=<current threshold from Step 1>`
   - `cd frontend && npx svelte-check --output machine 2>&1`
   - `cd frontend && npx vitest run 2>&1`
   - `cd frontend && npx playwright test 2>&1` (only the specs touched/added this run,
     to keep the loop fast — full suite only on the final confirmation pass)
2. Any failure: diagnose which side is wrong (the new test, or production code the
   new test correctly caught a real bug in) and fix that side — per the standing
   rule "every test case must pass — fix the test or the code, never leave it red,
   never skip/xfail/delete without replacement."
3. Any coverage gate still short: dispatch one more round of Step 3 for the
   remaining gap, narrower in scope.
4. Stop looping and escalate to the operator (foreground message, do not keep
   retrying) if: the same failure recurs 2 iterations in a row, or 5 iterations are
   reached with the gate still red.

## Step 5 — Commit (workshop only, never push)

1. `git add` the new/changed test files (and any production-code fix from Step 4)
   by explicit path — never `git add -A`/`git add .`.
2. Commit to `workshop` with a message summarizing what was covered:
   ```
   test(coverage): close N coverage gap(s) — <short file list>

   <one line per file: what was untested, what the new test covers>
   ```
3. Do NOT run `/ddev` or `/dprod` — leave the push decision to the operator or a
   separate explicit instruction.

## Foreground report

```
tprep: closed N gap(s) across M file(s)
  backend/brokers coverage: before X% → after Y%
  backend/api coverage:     before X% → after Y%
  frontend: K new test(s), 0 failures
→ committed workshop <hash>
```

On an unresolved gate after 5 iterations:
```
tprep: BLOCKED after 5 iterations — <gate> still failing
  <specific failure, file:line>
Needs operator judgment — stopping rather than looping further.
```
