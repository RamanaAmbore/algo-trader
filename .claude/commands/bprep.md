# /bprep — Nightly unattended wrapper around /prep (tests then complexity)

Thin nightly wrapper. All real logic lives in `/prep` (which itself sequences
`/tprep` then `/refact`) — this file exists only to give the unattended/scheduled
invocation its own name and persistent report entry. This is the command the
01:00 IST nightly schedule actually invokes.

## Steps

1. Run `/prep` in full (Steps 1-3 of that command: `/tprep`, then `/refact`,
   then the merged report). Do not duplicate its logic here.
2. Append the merged result to the same persistent report file `/btprep` and
   `/brefact` use:
   ```bash
   mkdir -p docs/audits
   {
     echo "## $(date '+%Y-%m-%d %H:%M %Z') — /bprep (nightly, unattended)"
     echo
     echo '<prep merged foreground report verbatim>'
     echo
   } >> docs/audits/NIGHTLY_BATCH_LOG.md
   git add docs/audits/NIGHTLY_BATCH_LOG.md
   git commit -m "chore(bprep): nightly test+complexity pass $(date +%Y-%m-%d)" --amend --no-edit 2>/dev/null || \
     git commit -m "chore(bprep): nightly test+complexity pass $(date +%Y-%m-%d)"
   ```
3. If either phase escalated (BLOCKED), the log entry must say so explicitly —
   this is the one thing the operator needs to see first when they come back
   online.

## Notes

- No per-step confirmation, no foreground narration — designed to run while the
  operator is offline, start to finish.
- Never pushes to `dev`/`main`. The operator reviews `docs/audits/NIGHTLY_BATCH_LOG.md`
  and runs `/ddev` (and `/dprod` on explicit request) separately once they're back.
- Scheduling: intended to fire nightly at 01:00 IST (after MCX settles ~23:30-23:55
  IST and well clear of any broker-token-rotation window). The actual trigger is
  external to this file — see the operator's cron/`CronCreate`/launchd setup.
