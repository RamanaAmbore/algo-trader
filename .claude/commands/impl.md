---
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent, ExitPlanMode, EnterPlanMode, ToolSearch, Monitor, TaskCreate, TaskUpdate, TaskGet, TaskList, TaskOutput, TaskStop
---

# /impl — Implement agreed plan, loop to green, ready for /ddev

## Permissions

**Step 1 — Exit plan mode (if active):** If plan mode is currently active, call `ExitPlanMode` (no `allowedPrompts`) as the very first step.

**Step 2 — Engage bypass mode:** Immediately after ExitPlanMode (or as first step if not in plan mode), run:
```bash
python3 -c "import json, os; p=os.path.expanduser('~/.claude/settings.json'); d=json.load(open(p)); d['defaultMode']='bypassPermissions'; json.dump(d, open(p,'w'), indent=2)"
```

**Final step — Restore plan mode:** After Step 8 (commit), before calling `EnterPlanMode`, run:
```bash
python3 -c "import json, os; p=os.path.expanduser('~/.claude/settings.json'); d=json.load(open(p)); d['defaultMode']='plan'; json.dump(d, open(p,'w'), indent=2)"
```
Then call `EnterPlanMode`.

Read `.claude/PLAN.md` (written during plan mode), dispatch implementation agents,
run tests until green, commit, and report ready for `/ddev`.

---

## Step 0 — Guard

Check `.claude/PLAN.md` exists. If missing, auto-discover from the Claude plans folder:
```bash
ls -t ~/.claude/plans/*.md 2>/dev/null | head -1
```
If a plan file is found there, copy it **and delete the source** so it cannot be re-selected on a future run:
```bash
_src=$(ls -t ~/.claude/plans/*.md | head -1) && cp "$_src" .claude/PLAN.md && rm "$_src"
```
Then proceed. If no plan file exists anywhere:
```
impl: BLOCKED — no plan found.
Enter plan mode, agree on an approach, then run /impl.
```
Stop.

---

## Step 1 — Read plan

Read `.claude/PLAN.md` in full. Extract:
- **Title** — from the `# Plan:` heading
- **Agent tasks** — each non-"skip" line under `## Agents`
- **Test strategy** — `pytest`, `svelte-check`, `playwright` flags under `## Tests`
- **Commit message** — line under `## Commit message`
- **Done criteria** — text under `## Done when`

---

## Step 2 — Dispatch agents (parallel where independent)

For each non-"skip" agent listed under `## Agents`, dispatch the matching subagent with its task text from the plan.

| Plan entry | Subagent type |
|---|---|
| `backend: <task>` | `backend` |
| `frontend: <task>` | `frontend` |
| `broker: <task>` | `broker` |
| `doc: <task>` | `doc` |
| `backend-test: <task>` | `backend-test` |
| `playwright: <task>` | `playwright` |

**Parallel rule**: dispatch all independent agents in one message (single tool call with multiple Agent blocks). If a backend agent must finish before a frontend agent can start (e.g. API shape changes), sequence them.

Pass each agent its task text verbatim from PLAN.md, plus the project context it needs (working directory, relevant file paths from the plan).

**Test requirement (standing rule — non-negotiable)**: Every agent brief MUST include the following instruction verbatim:
> "For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.
> - `backend/brokers/` change → add/update a pytest test in `backend/tests/broker/` covering the changed lines
> - `backend/api/` change → add/update a pytest test in `backend/tests/` covering the changed lines
> - `frontend/src/lib/data/` change → add/update a Vitest test in `frontend/src/lib/__tests__/` covering the changed logic
> - `frontend/src/` UI change → add/update a Playwright spec in `frontend/tests/` covering the changed flow
> No change ships without a corresponding test update. If you add a helper function, test it. If you fix a branch, test that branch. The test must exercise the exact lines you changed."

Wait for all agents to complete before proceeding.

---

## Step 3 — Test loop (max 3 iterations)

Run all flagged test surfaces **in background** simultaneously:

**Backend** (`pytest: yes`) — launch with `run_in_background: true`:
```
cd /Users/ramanambore/projects/ramboq && \
  venv/bin/pytest backend/tests/ -q --tb=short \
    --cov=backend/brokers --cov=backend/api \
    --cov-report=term-missing && \
  venv/bin/coverage report --include="backend/brokers/*" --fail-under=80 && \
  venv/bin/coverage report --include="backend/api/*" --fail-under=45
```
Use Monitor to collect output when it completes. Capture: passed/skipped/failed counts + FAILED lines + broker coverage % + api coverage %.

**Frontend type check** (`svelte-check: yes`) — launch with `run_in_background: true`:
```
cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1
```
Use Monitor to collect output when it completes. Capture: error count + ERROR lines.

**Frontend Vite unit tests** (always run, regardless of flags) — launch with `run_in_background: true`:
```
cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1
```
Use Monitor to collect output when it completes. Capture: passed/failed counts.

**Playwright** (`playwright: yes`): dispatch `playwright` subagent with the spec path(s) from the plan.

Launch all surfaces simultaneously (parallel Bash `run_in_background` calls + Agent dispatch in one message). Wait for all to complete via Monitor notifications before evaluating results.

### On failure — fix iteration

If any surface fails and iteration < 3:
- Dispatch a targeted fix agent (same subagent type that owns the failing surface)
- Give it: the exact FAILED lines / ERROR lines + the plan task for context
- Re-run the failing surface only (background)
- Increment iteration counter

If still failing after **3 iterations**: report blockers and stop (do not commit).
```
impl: BLOCKED after 3 iterations
pytest: <N> failing — <test names>
svelte-check: <N> errors — <file:line messages>
Fix manually, then run /ddev.
```

---

## Step 4 — Self-audit (before commit)

Run all self-audit checks from CLAUDE.md:
1. Scan for structurally unreachable code in changed files (`git diff --name-only HEAD`)
2. For any P&L / NavStrip / market-data fix: grep all consumers and verify fix propagates
3. If a function was delegated/refactored: verify the called helper contains the original logic
4. **Per-file test coverage check (hard gate — no exceptions)**

   Run:
   ```bash
   git diff --name-only HEAD
   ```
   For every changed source file, confirm a corresponding test change exists in the same commit:

   | Changed file pattern | Required test location |
   |---|---|
   | `backend/brokers/*.py` | `backend/tests/broker/test_*.py` |
   | `backend/api/*.py` or `backend/api/**/*.py` | `backend/tests/test_*.py` |
   | `frontend/src/lib/data/*.js` | `frontend/src/lib/__tests__/data/*.test.js` |
   | `frontend/src/lib/*.js` or `*.svelte` | `frontend/tests/*.spec.js` (Playwright) |

   How to check: `git diff --name-only HEAD | grep -E 'backend/brokers|backend/api|frontend/src'` — then for each result, verify `git diff --name-only HEAD` includes at least one corresponding test file.

   If **any** changed source file has no corresponding test change:
   - Identify which files are missing test coverage
   - Dispatch a `backend-test`, `broker`, `frontend`, or `playwright` agent with the specific changed lines and instruction to write tests for those exact changes
   - Wait for the agent and re-run affected tests
   - Only commit once every changed source file has a test covering it
   - This gate is **non-negotiable and cannot be waived**

5. **Coverage thresholds** — verify thresholds hold after the change:
   - Broker layer (backend/brokers/) must remain ≥ 80%
   - API layer (backend/api/) must remain ≥ 45%
   - If a change risks dropping below threshold, dispatch a `broker` or `backend-test` agent to add tests before committing.

If audit finds a defect, dispatch one more targeted fix, re-run affected tests, then proceed.

---

## Step 5 — Archive plan + Commit

First, archive the plan (so the rename is included in the same commit):
```
mv .claude/PLAN.md .claude/PLAN.done.md
```

Then stage implementation files AND all `.claude/` changes (archived plan, any new command/skill files):
```
git add -u
git add .claude/
```

Commit using the message from `## Commit message` in PLAN.md:
```
git commit -m "<message from plan>\n\nCo-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Step 5.5 — Spec/doc sync

After committing, identify which documentation surfaces are affected and update them.

1. Run `git diff --name-only HEAD~1..HEAD` to identify changed files.

2. Map changed files to spec/guide ownership:

| Changed path pattern | Spec/Guide to update |
|---|---|
| `frontend/src/lib/data/nav.js` | `docs/specs/NAVSTRIP_SPEC.md` |
| `frontend/src/lib/data/expiryPnl.js` | `docs/specs/PULSE_SPEC.md` |
| `frontend/src/routes/(algo)/admin/derivatives/` | `docs/specs/PULSE_SPEC.md` |
| `backend/api/routes/` or `backend/api/background.py` | `docs/specs/BROKER_SPEC.md` or `docs/specs/PULSE_SPEC.md` |
| `backend/brokers/` | `docs/specs/BROKER_SPEC.md` |
| Any operator-visible behaviour change | `docs/guides/USER_GUIDE.md` |

3. For each affected spec/guide, dispatch a `doc` subagent with:
   - Output of `git show HEAD -- <changed_file>` (the diff)
   - The spec/guide file content
   - Instruction: update ONLY the sections that describe the changed behaviour; do not remove or falsify existing content

4. **DESIGN_GUIDE check** — read `docs/DESIGN_GUIDE.md` and assess whether the commit affects any of its documented surfaces:
   - New or changed routes, API contracts, data models, or background tasks → update architecture section
   - New shared helpers / SSOT functions added → update component/data-flow diagrams or descriptions
   - Broker-layer changes, new config knobs, new capabilities → update relevant DESIGN_GUIDE section

   If DESIGN_GUIDE.md needs updating: dispatch a `doc` subagent to edit only the affected sections (do NOT rewrite unaffected sections or alter diagrams unless the diagram is wrong). Then regenerate the PDF with `run_in_background: true`:
   ```
   python3 docs/generate_pdf.py
   ```
   Use Monitor to collect output. Verify the command exits 0 and report the PDF file size. If it fails, fix DESIGN_GUIDE.md and retry before proceeding.

5. If any doc agent made changes, commit them together in one commit:
   ```
   git add docs/
   git commit -m "docs: sync specs/DESIGN_GUIDE after $(git log -1 --format='%s' HEAD~1)"
   ```
   Skip if no docs were changed.

---

## Step 6 — Foreground report

```
impl: <plan title>
agents: backend ✓ | frontend ✓  (or whichever ran)
tests: pytest 2712 passed, 0 failed | broker cov 91% ✓ | api cov 83% ✓ | svelte-check 0 errors | vite 192 passed
→ committed <short-hash> — ready for /ddev
```

---

## Step 8 — Enter plan mode

Call `EnterPlanMode` to return to plan mode after completion.

---

## Notes

- Never push — `/impl` only commits. `/ddev` pushes.
- Never modify `secrets.yaml` or any file listed in `.gitignore`.
- If PLAN.md has `playwright: no` and `svelte-check: no` and `pytest: no`, still run svelte-check as a baseline sanity check (background).
- The plan's `## Done when` is informational — tests passing is the machine-checkable gate.
- **Standing rule (hard gate)**: Every code change must ship with a new or updated test. This is enforced in Step 2 (agent brief) and Step 4 (self-audit). No commit without tests.
