---
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent, ExitPlanMode, EnterPlanMode, ToolSearch
---

# /depl — Full deploy pipeline: impl → ddev → dprod

Run the complete build-and-deploy pipeline in sequence: implement the plan, gate on tests,
and ship to prod. All three phases execute with bypass-permissions (no tool-use prompts).
Returns to plan mode when done.

## Permissions

**Step 1 — Exit plan mode (if active):** If plan mode is currently active, call `ExitPlanMode` (no `allowedPrompts`) as the very first step.

**Step 2 — Engage bypass mode:** Immediately after ExitPlanMode (or as the very first step if not in plan mode), run:
```bash
python3 -c "import json; p='.claude/settings.json'; d=json.load(open(p)); d['permissions']['defaultMode']='bypassPermissions'; json.dump(d, open(p,'w'), indent=2)"
```

**Final step — Restore plan mode:** Before calling `EnterPlanMode` (after the dprod report or on any hard block), run:
```bash
python3 -c "import json; p='.claude/settings.json'; d=json.load(open(p)); d['permissions']['defaultMode']='plan'; json.dump(d, open(p,'w'), indent=2)"
```
Then call `EnterPlanMode`.

---

## Phase 1 — impl (build)

Follow all steps from `/impl` exactly:

1. Guard: check `.claude/PLAN.md` exists. If missing, auto-copy from `~/.claude/plans/` (most recently modified `.md`) **and delete the source**: `_src=$(ls -t ~/.claude/plans/*.md | head -1) && cp "$_src" .claude/PLAN.md && rm "$_src"`. If no plan anywhere, stop and call `EnterPlanMode`.
2. Read plan (title, agents, tests, commit message, done criteria).
3. Dispatch agents in parallel (backend, frontend, doc, backend-test as specified in plan).
4. Run test loop (pytest + svelte-check + playwright per plan flags). Max 3 fix iterations.
   - If still failing after 3 iterations: report blockers, call `EnterPlanMode`, stop.
5. Self-audit (unreachable code, P&L consumer grep, delegation verification).
6. Archive plan + Commit: `mv .claude/PLAN.md .claude/PLAN.done.md`, then `git add -u && git add .claude/`, commit with plan message + Co-Authored-By trailer.
7. Spec/doc sync (NAVSTRIP_SPEC, PULSE_SPEC, BROKER_SPEC, USER_GUIDE, DESIGN_GUIDE as affected).
8. Report: `impl: <title> → committed <hash>`.

Do NOT call `EnterPlanMode` here — continue to Phase 2.

---

## Phase 2 — ddev (gate + push dev)

Follow all steps from `/ddev` exactly:

1. Run pytest (`venv/bin/pytest backend/tests/ -q --tb=line`) in background.
2. Run svelte-check (`cd frontend && npx svelte-check --output machine 2>&1`) in background.
3. Spec-sync gate: check `git diff origin/dev...HEAD --name-only` for unsynced spec files (warning only, non-blocking).
4. Decision:
   - Any failures → report, call `EnterPlanMode`, **stop** (do not proceed to dprod).
   - All green → `git push origin dev`.
5. Report: `ddev: backend <N> passed, 0 failed | svelte-check 0 errors → pushed dev <hash>`.

Do NOT call `EnterPlanMode` here — continue to Phase 3.

---

## Phase 3 — dprod (docs + merge + prod)

Follow all steps from `/dprod` exactly:

1. Prerequisite: `git log main..dev --oneline`. If nothing, report "already up to date", call `EnterPlanMode`, stop.
2. Identify changed surfaces; dispatch doc agents for affected specs/guides.
3. Regenerate DESIGN_GUIDE PDF if DESIGN_GUIDE.md was touched (`python3 docs/generate_pdf.py`).
4. Complexity gate: `venv/bin/python -m radon cc backend/ -s -n D 2>/dev/null | head -20`.
   - Any D/E/F → report hotspots, call `EnterPlanMode`, **stop**.
5. Merge and push:
   ```
   git checkout main && git merge dev --no-edit && git push origin main
   git checkout dev && git push origin dev
   ```
6. Report:
   ```
   depl: impl ✓ | ddev ✓ | dprod ✓
   → <N> doc(s) updated | CC clean | PDF <size>MB
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
