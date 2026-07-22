---
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent, ExitPlanMode, EnterPlanMode, ToolSearch
---

# /ddev — Test and push to dev

## Permissions

**Step 1 — Exit plan mode (if active):** If plan mode is currently active, call `ExitPlanMode` (no `allowedPrompts`) as the very first step.

**Step 2 — Engage bypass mode:** Immediately after ExitPlanMode (or as first step if not in plan mode), run:
```bash
python3 -c "import json; p='.claude/settings.json'; d=json.load(open(p)); d['permissions']['defaultMode']='bypassPermissions'; json.dump(d, open(p,'w'), indent=2)"
```

**Final step — Restore plan mode:** After the foreground report, before calling `EnterPlanMode`, run:
```bash
python3 -c "import json; p='.claude/settings.json'; d=json.load(open(p)); d['permissions']['defaultMode']='plan'; json.dump(d, open(p,'w'), indent=2)"
```
Then call `EnterPlanMode`.

Run backend tests and frontend type check. Push to dev only if both pass. Report results in foreground.

## Steps (run backend and frontend checks in parallel background agents)

1. **Backend** — `venv/bin/pytest backend/tests/ -q --tb=line --timeout=60` from repo root. Capture passed/skipped/failed counts and any FAILED lines.

2. **Frontend** — `cd frontend && npx svelte-check --output machine 2>&1` from repo root. Capture error count.

---

## Step 1.5 — Spec-sync gate

After tests complete (pass or fail), check spec coverage. This step runs regardless of test outcome but does NOT block the push.

1. `git diff origin/dev...HEAD --name-only` — collect changed files in commits ahead of origin/dev.

2. Map changed files against spec ownership:

| Changed path pattern | Spec to check |
|---|---|
| `frontend/src/lib/data/nav.js` | `docs/specs/NAVSTRIP_SPEC.md` |
| `frontend/src/lib/data/expiryPnl.js` | `docs/specs/PULSE_SPEC.md` |
| `frontend/src/routes/(algo)/admin/derivatives/` | `docs/specs/PULSE_SPEC.md` |
| `backend/api/routes/orders_place.py` or `orders_basket.py` | `docs/specs/BROKER_SPEC.md` |
| `backend/brokers/` | `docs/specs/BROKER_SPEC.md` |

3. For each matched pair: check whether the corresponding spec file was also modified in this push (`git diff origin/dev...HEAD --name-only | grep SPEC`). If the code file changed but the spec did NOT appear in the diff, emit a warning:
   ```
   ddev: ⚠ spec-sync warning — nav.js:baseDayPnlForPosition changed but NAVSTRIP_SPEC.md not updated in this push
   ```

4. Proceed to the Decision step regardless. Warnings are informational — the operator decides whether to update the spec before or after pushing.

## Decision

- Any backend failures OR any svelte-check errors → **do NOT push**. Report failures clearly. Stop.
- All green → `git push origin dev`. Report commit hash, test counts, zero errors.

## Foreground output format

```
ddev: backend 2712 passed, 13 skipped, 0 failed | svelte-check 0 errors
→ pushed dev <short-hash>
```

On failure:
```
ddev: BLOCKED — <N> test(s) failing: <test names>
Fix failures before pushing dev.
```

## Step — Enter plan mode

After the foreground report (success or block), call `EnterPlanMode` to return to plan mode.

## Notes

- Do not push to main. Dev only.
- Do not commit anything — only push what is already committed.
- If no commits ahead of origin/dev, report "nothing to push" and stop.
