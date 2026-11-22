# /brefact — Nightly unattended wrapper around /refact

Thin nightly wrapper. All real logic lives in `/refact` — this file exists only to
give the unattended/scheduled invocation its own name and its own persistent
report entry (same convention as `/btprep`).

## Steps

1. Run `/refact` in full (all of that command's own steps — constraint read,
   hotspot find, batch dispatch, loop-to-green, workshop-only commit). Do not
   duplicate its logic here.
2. Append the result to the same persistent report file `/btprep` uses:
   ```bash
   mkdir -p docs/audits
   {
     echo "## $(date '+%Y-%m-%d %H:%M %Z') — /brefact (nightly, unattended)"
     echo
     echo '<refact foreground report verbatim>'
     echo
   } >> docs/audits/NIGHTLY_BATCH_LOG.md
   git add docs/audits/NIGHTLY_BATCH_LOG.md
   git commit -m "chore(brefact): nightly complexity-gate pass $(date +%Y-%m-%d)" --amend --no-edit 2>/dev/null || \
     git commit -m "chore(brefact): nightly complexity-gate pass $(date +%Y-%m-%d)"
   ```
3. If `/refact` escalated (BLOCKED), the log entry must say so explicitly.

## Notes

- No per-step confirmation, no foreground narration — designed to run while the
  operator is offline.
- Never pushes to `dev`/`main` — `/refact`'s own workshop-only rule applies
  unchanged.
- Does not self-schedule.
