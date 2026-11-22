# /prep — Full daily prep: tests then complexity

Runs `/tprep` followed by `/refact`, in that order, as one unattended batch pass.
Tests run first so the complexity refactor in phase 2 has a real safety net —
`/refact` must never change behavior, and phase 1's freshly-closed coverage is
what catches it if it accidentally does.

Both phases are already self-contained (own gates, own fix-to-green loop, own
workshop-only commit, own escalation-after-N-iterations rule) — this command does
not duplicate their steps, it just sequences them and merges the two reports.

## Steps

1. Run `/tprep` in full (Steps 1-5 of that command). Capture its foreground report.
   - If `/tprep` escalates (BLOCKED), stop here and surface that report as-is —
     do not proceed to `/refact` on top of an unresolved test gate.
2. Run `/refact` in full (Steps 1-5 of that command). Capture its foreground report.
   - If `/refact` escalates (BLOCKED), surface both reports — phase 1's commit
     already landed on workshop regardless.
3. Merge the two foreground reports into one combined summary.

## Foreground report

```
prep: tprep ✓ | refact ✓
  tprep: closed N gap(s) across M file(s) | brokers cov X%→Y% | api cov X%→Y%
  refact: cleared N hotspot(s) across M file(s) | CC gate clean
→ workshop now at <latest hash> (2 commits this run)
```

On a phase block:
```
prep: tprep ✓ | refact BLOCKED
  tprep: closed N gap(s) — committed <hash>
  refact: <function> in <file> still grade D after 2 attempts — needs operator judgment
```

## Notes

- Neither phase pushes to `dev` or `main` — this is a workshop-only daily
  maintenance pass. Run `/ddev` (and `/dprod` on explicit request) separately
  once the operator has reviewed what landed.
- Designed for unattended/background execution to save cost — no per-step
  confirmation within either phase. If the operator wants this to run
  automatically every day, use `ScheduleWakeup`/cron separately; this file only
  defines what happens when `/prep` is invoked, it does not self-schedule.
