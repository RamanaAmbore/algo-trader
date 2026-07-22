---
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent, ExitPlanMode, EnterPlanMode, ToolSearch
---

# /dprod — Update docs and deploy to prod

## Permissions

**Step 1 — Exit plan mode (if active):** If plan mode is currently active, call `ExitPlanMode` (no `allowedPrompts`) as the very first step.

**Step 2 — Engage bypass mode:** Immediately after ExitPlanMode (or as first step if not in plan mode), run:
```bash
python3 -c "import json, os; p=os.path.expanduser('~/.claude/settings.json'); d=json.load(open(p)); d['defaultMode']='bypassPermissions'; json.dump(d, open(p,'w'), indent=2)"
```

**Final step — Restore plan mode:** After Step 7, before calling `EnterPlanMode`, run:
```bash
python3 -c "import json, os; p=os.path.expanduser('~/.claude/settings.json'); d=json.load(open(p)); d['defaultMode']='plan'; json.dump(d, open(p,'w'), indent=2)"
```
Then call `EnterPlanMode`.

Update specs, guides, DESIGN_GUIDE, PDF, and complexity — then merge dev→main and push. All steps run in background; report each result in foreground as it completes.

## Prerequisite check

Run `git log main..dev --oneline`. If nothing ahead, report "already up to date" and stop.

## Steps

### 1. Identify changed surfaces
`git log main..dev --name-only --format=""` — collect unique file paths. Determine which doc surfaces are affected:
- `backend/api/routes/` or `backend/api/algo/` → check `docs/specs/` relevance
- `frontend/src/routes/` → check `docs/guides/USER_GUIDE.md`
- `backend/brokers/` → check `docs/guides/ADMIN_GUIDE.md`
- `backend/api/background.py` or core architecture → `docs/DESIGN_GUIDE.md`

### 2. Update affected docs (background doc agent per surface)
Only update docs where the commit log shows a behaviour change visible to that doc's audience. Skip if changes are internal refactors with no operator-visible effect. Use `doc` subagent.

### 3. Regenerate DESIGN_GUIDE PDF (if DESIGN_GUIDE.md was touched)
`python3 docs/generate_pdf.py` — must complete without error. Report file size.

### 4. Complexity gate
`venv/bin/python -m radon cc backend/ -s -n D 2>/dev/null | head -20`
- Any D/E/F grade found → **block prod deploy**. Report hotspots. Stop.
- Clean → proceed.

### 5. Merge and push
```
git checkout main
git merge dev --no-edit
git push origin main
git checkout dev
git push origin dev
```

### 6. Foreground report
```
dprod: <N> doc(s) updated | CC clean | PDF <size>MB
→ merged dev→main <short-hash> | pushed main + dev
```

On block:
```
dprod: BLOCKED — CC gate: <function> (grade D, CC=<N>) in <file>
Fix complexity before deploying to prod.
```

### 7. Enter plan mode

Call `EnterPlanMode` to return to plan mode after the foreground report.

## Notes

- Never skip the CC gate.
- If PDF regen fails, fix DESIGN_GUIDE.md and retry before merging.
- Commit any doc changes to dev before merging to main.
