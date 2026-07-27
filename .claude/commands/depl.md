---
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent, ExitPlanMode, EnterPlanMode, ToolSearch, Monitor, TaskCreate, TaskUpdate, TaskGet, TaskList, TaskOutput, TaskStop
---

# /depl — Full deploy pipeline: impl → ddev → dprod

Run the complete build-and-deploy pipeline in sequence: implement the plan, gate on tests,
and ship to prod. All three phases execute with bypass-permissions (no tool-use prompts).
Long-running steps (pytest, svelte-check, PDF regen, CC check) run as background Bash
processes; use Monitor to collect each result. Returns to plan mode when done.

## Permissions

**Step 1 — Exit plan mode (if active):** If plan mode is currently active, call `ExitPlanMode` (no `allowedPrompts`) as the very first step.

**Step 2 — Engage bypass mode:** Immediately after ExitPlanMode (or as the very first step if not in plan mode), run:
```bash
python3 -c "import json, os; p=os.path.expanduser('~/.claude/settings.json'); d=json.load(open(p)); d['defaultMode']='bypassPermissions'; json.dump(d, open(p,'w'), indent=2)"
```

**Final step — Restore plan mode:** Before calling `EnterPlanMode` (after the dprod report or on any hard block), run:
```bash
python3 -c "import json, os; p=os.path.expanduser('~/.claude/settings.json'); d=json.load(open(p)); d['defaultMode']='plan'; json.dump(d, open(p,'w'), indent=2)"
```
Then call `EnterPlanMode`.

---

## Phase 1 — impl (build)

Follow all steps from `/impl` exactly:

1. Guard: check `.claude/PLAN.md` exists. If missing, auto-copy from `~/.claude/plans/` (most recently modified `.md`) **and delete the source**: `_src=$(ls -t ~/.claude/plans/*.md | head -1) && cp "$_src" .claude/PLAN.md && rm "$_src"`. If no plan anywhere, stop and call `EnterPlanMode`.
2. Read plan (title, agents, tests, commit message, done criteria).
3. Dispatch agents in parallel (backend, frontend, doc, backend-test as specified in plan). All agents run as background Agent calls in a single message.
4. Run test loop (pytest + svelte-check + playwright per plan flags). Max 3 fix iterations.
   - pytest: `run_in_background: true` + Monitor
   - svelte-check: `run_in_background: true` + Monitor
   - Both launched in one message (parallel). Wait for Monitor notifications before evaluating.
   - If still failing after 3 iterations: report blockers, call `EnterPlanMode`, stop.
5. Self-audit (unreachable code, P&L consumer grep, delegation verification).
6. Archive plan + Commit: `mv .claude/PLAN.md .claude/PLAN.done.md`, then `git add -u && git add .claude/`, commit with plan message + Co-Authored-By trailer.
7. Spec/doc sync (NAVSTRIP_SPEC, PULSE_SPEC, BROKER_SPEC, USER_GUIDE, DESIGN_GUIDE as affected). Doc agents dispatched in parallel.
8. Report: `impl: <title> → committed <hash>`.

Do NOT call `EnterPlanMode` here — continue to Phase 2.

---

## Phase 2 — ddev (gate + push dev)

Follow all steps from `/ddev` exactly:

1. Run pytest + coverage gates with `run_in_background: true`:
   ```
   cd /Users/ramanambore/projects/ramboq && \
     venv/bin/pytest backend/tests/ -q --tb=line \
       --cov=backend/brokers --cov=backend/api \
       --cov-report=term-missing && \
     venv/bin/coverage report --include="backend/brokers/*" --fail-under=80 && \
     venv/bin/coverage report --include="backend/api/*" --fail-under=80
   ```
2. Run svelte-check (`cd frontend && npx svelte-check --output machine 2>&1`) with `run_in_background: true`.
3. Run Vite unit tests (`cd frontend && npx vitest run 2>&1`) with `run_in_background: true`.
4. Launch all three in one message (parallel). Use Monitor to collect each result when done.
5. Spec-sync gate: check `git diff origin/dev...HEAD --name-only` for unsynced spec files (warning only, non-blocking).
6. Decision:
   - Any pytest failure, broker coverage < 80%, api coverage < 80%, svelte-check error, or Vite failure → report, call `EnterPlanMode`, **stop**.
   - All green → `git push origin dev`.
7. Report: `ddev: backend <N> passed, 0 failed | broker cov <N>% ✓ | api cov <N>% ✓ | svelte-check 0 errors | vite <N> passed → pushed dev <hash>`.

Do NOT call `EnterPlanMode` here — continue to Phase 3.

---

## Phase 3 — dprod (docs + merge + prod)

Follow all steps from `/dprod` exactly:

1. Prerequisite: `git log main..dev --oneline`. If nothing, report "already up to date", call `EnterPlanMode`, stop.
2. Identify changed surfaces; dispatch doc agents for affected specs/guides (parallel, background).
3. Launch PDF regen, CC gate, and coverage gate simultaneously with `run_in_background: true`:
   - PDF: `python3 docs/generate_pdf.py` (only if DESIGN_GUIDE.md was touched)
   - CC gate: `venv/bin/python -m radon cc backend/ -s -n D 2>/dev/null | head -20`
   - Broker cov: `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -q --tb=no --cov=backend/brokers && venv/bin/coverage report --include="backend/brokers/*" --fail-under=80`
   - API cov: `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -q --tb=no --cov=backend/api && venv/bin/coverage report --include="backend/api/*" --fail-under=80`
   - Vite: `cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1`
   - Use Monitor for all. Wait before proceeding.
   - Any D/E/F CC grade → report hotspots, call `EnterPlanMode`, **stop**.
   - Broker coverage < 80% or API coverage < 80% → report %, call `EnterPlanMode`, **stop**.
   - Any Vite failure → report, call `EnterPlanMode`, **stop**.
4. Merge and push:
   ```
   git checkout main && git merge dev --no-edit && git push origin main
   git checkout dev && git push origin dev
   ```
5. Report:
   ```
   depl: impl ✓ | ddev ✓ | dprod ✓
   → <N> doc(s) updated | CC clean | broker cov <N>% ✓ | api cov <N>% ✓ | vite <N> passed | PDF <size>MB
   → merged dev→main <hash> | pushed main + dev
   ```

---

## Final step — Enter plan mode

After the Phase 3 report (or on any block at any phase), call `EnterPlanMode` to return
to plan mode.

---

## Notes

- Never push to prod without an explicit prior operator request ("deploy" / `/dprod` / `/depl`).
- If impl fails tests after 3 iterations, stop at Phase 1 — do not push anything.
- If ddev tests fail, stop at Phase 2 — do not merge to main.
- If CC gate fails, stop at Phase 3 — do not merge.
- Each phase must fully complete before the next begins.
