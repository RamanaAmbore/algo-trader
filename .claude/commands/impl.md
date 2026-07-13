# /impl — Implement agreed plan, loop to green, ready for /ddev

Read `.claude/PLAN.md` (written during plan mode), dispatch implementation agents,
run tests until green, commit, and report ready for `/ddev`.

---

## Step 0 — Guard

Check `.claude/PLAN.md` exists. If missing:
```
impl: BLOCKED — no plan found.
Enter plan mode, agree on an approach, and write .claude/PLAN.md before running /impl.
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

Wait for all agents to complete before proceeding.

---

## Step 3 — Test loop (max 3 iterations)

Run the test surfaces flagged in `## Tests`:

**Backend** (`pytest: yes`):
```
cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -q --tb=short
```
Capture: passed / skipped / failed counts + FAILED lines.

**Frontend** (`svelte-check: yes`):
```
cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1
```
Capture: error count + ERROR lines.

**Playwright** (`playwright: yes`): dispatch `playwright` subagent with the spec path(s) from the plan.

### On failure — fix iteration

If any surface fails and iteration < 3:
- Dispatch a targeted fix agent (same subagent type that owns the failing surface)
- Give it: the exact FAILED lines / ERROR lines + the plan task for context
- Re-run the failing surface only
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

Run the three self-audit checks from CLAUDE.md:
1. Scan for structurally unreachable code in changed files (`git diff --name-only HEAD`)
2. For any P&L / NavStrip / market-data fix: grep all consumers and verify fix propagates
3. If a function was delegated/refactored: verify the called helper contains the original logic

If audit finds a defect, dispatch one more targeted fix, re-run affected tests, then proceed.

---

## Step 5 — Commit

Stage all modified tracked files:
```
git add -u
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

4. If the doc agent makes changes, commit them:
   ```
   git add docs/
   git commit -m "docs: sync specs after $(git log -1 --format='%s' HEAD~1)"
   ```
   Skip if no docs were changed.

---

## Step 6 — Archive plan

Rename `.claude/PLAN.md` → `.claude/PLAN.done.md` (keeps a record; overwrite if one exists).

---

## Step 7 — Foreground report

```
impl: <plan title>
agents: backend ✓ | frontend ✓  (or whichever ran)
tests: pytest 2712 passed, 0 failed | svelte-check 0 errors
→ committed <short-hash> — ready for /ddev
```

---

## Notes

- Never push — `/impl` only commits. `/ddev` pushes.
- Never modify `secrets.yaml` or any file listed in `.gitignore`.
- If PLAN.md has `playwright: no` and `svelte-check: no` and `pytest: no`, still run svelte-check as a baseline sanity check.
- The plan's `## Done when` is informational — tests passing is the machine-checkable gate.
- Agents are responsible for writing tests per the standing rule (spec + test for every fix/feature).
