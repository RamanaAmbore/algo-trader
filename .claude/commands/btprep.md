# /btprep — Nightly unattended wrapper around /tprep

Thin nightly wrapper. All real logic lives in `/tprep` — this file exists only to
give the unattended/scheduled invocation its own name, its own persistent report
file (mirroring `/tlm`'s convention), and a place to note that no operator is
expected to be watching when this runs.

## Steps

1. Run `/tprep` in full (all of that command's own steps — constraint read, gap
   find, batch dispatch, loop-to-green, workshop-only commit). Do not duplicate its
   logic here.
2. Append the result to a persistent report file (same convention as `/tlm`'s
   `docs/audits/` reports), so the operator can review what happened overnight
   without needing to have been online when it ran:
   ```bash
   mkdir -p docs/audits
   {
     echo "## $(date '+%Y-%m-%d %H:%M %Z') — /btprep (nightly, unattended)"
     echo
     echo '<tprep foreground report verbatim>'
     echo
   } >> docs/audits/NIGHTLY_BATCH_LOG.md
   git add docs/audits/NIGHTLY_BATCH_LOG.md
   git commit -m "chore(btprep): nightly test-coverage pass $(date +%Y-%m-%d)" --amend --no-edit 2>/dev/null || \
     git commit -m "chore(btprep): nightly test-coverage pass $(date +%Y-%m-%d)"
   ```
   (If `/tprep` itself already committed to `workshop`, fold the log-file commit
   into that SAME commit via `--amend --no-edit` rather than creating a second
   commit for a run that produced no other change — but never amend a commit
   from a DIFFERENT day's run.)
3. If `/tprep` escalated (BLOCKED after 5 iterations), the log entry must say so
   explicitly — do not silently swallow a blocked run as if it succeeded.

## Notes

- No per-step confirmation, no foreground narration — this command is designed to
  run while the operator is offline. The ONLY output that matters is the
  persistent log file from Step 2.
- Still never pushes to `dev`/`main` — `/tprep`'s own workshop-only rule applies
  unchanged.
- Does not self-schedule. Pairing this with an actual nightly trigger (cron /
  `CronCreate` / launchd) is a separate, explicit setup step — see the operator's
  own scheduling setup, not this file.
