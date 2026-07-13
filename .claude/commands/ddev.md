# /ddev — Test and push to dev

Run backend tests and frontend type check. Push to dev only if both pass. Report results in foreground.

## Steps (run backend and frontend checks in parallel background agents)

1. **Backend** — `venv/bin/pytest backend/tests/ -q --tb=line --timeout=60` from repo root. Capture passed/skipped/failed counts and any FAILED lines.

2. **Frontend** — `cd frontend && npx svelte-check --output machine 2>&1` from repo root. Capture error count.

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

## Notes

- Do not push to main. Dev only.
- Do not commit anything — only push what is already committed.
- If no commits ahead of origin/dev, report "nothing to push" and stop.
