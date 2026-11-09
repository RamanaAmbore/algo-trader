# Plan: Track 3, Sub-sprint T3.5 — Docs + CLAUDE.md cleanup

## Task

Fifth of 6 sub-sprints removing Bracket/Template/GTT (full detail:
`~/.claude/plans/sparkling-dancing-lobster.md`, "Track 3" section — read it in full, especially
T3.1-T3.4's actual findings, all already shipped and live on prod). Bracket/Template/GTT is now
100% removed from both backend and frontend. This sub-sprint is documentation-only — no code
changes.

1. **Delete `docs/specs/GTT_SPEC.md` outright.** A 369-line spec with no subject left to describe
   serves no one — this is the plan's own judgment call 10, already decided, not re-litigated.
2. **Remove the Bracket/Template sections from `docs/guides/ADMIN_GUIDE.md`, `docs/guides/
   USER_GUIDE.md`, and `docs/guides/ORDER_LIFECYCLE.md`** (confirm exact filename/path for
   ORDER_LIFECYCLE.md first — judgment call 10 notes it lives in `docs/guides/`, per the
   `project_lifecycle_docs_location` memory). One correction while doing USER_GUIDE.md: its
   "Adding your own agent" subsection is misfiled under the Order-templates `##` heading by
   document structure, but is a distinct topic unrelated to Bracket — re-home it under an
   appropriate existing heading rather than deleting it.
3. **`CLAUDE.md` — mark the ~21 primarily-Bracket/Template/GTT paragraph entries as historical
   record, not delete them.** Same convention this file already uses elsewhere (e.g. the manual
   position-pin feature: "REMOVED... Historical record only; do not reintroduce... without a new
   design decision"). Grep for `Bracket`, `GTT`, `template_attach`, `bracket_registry` to find
   every candidate paragraph, then judge each one: an entry whose WHOLE SUBJECT is Bracket/GTT
   (e.g. "GTT layer also enforces translate_qty", "Template attach mode gate", "Bracket default
   scope resolved one way...") gets a short historical-record note prepended or appended (do NOT
   rewrite the technical content itself — it's a historical record of what WAS true, keep it
   verbatim, just flag it as retired). An entry that only MENTIONS Bracket/GTT in passing while
   being primarily about something else unrelated (e.g. an entry about order-lifecycle races that
   happens to reference a Bracket-adjacent function in one bullet) is left alone — do not touch
   it, since the removal doesn't change what it documents about the non-Bracket subject.
4. **CLAUDE.md's Common Tasks table — remove the ~9 rows pointing at now-deleted files/functions.**
   A table of "where to make changes" pointing at deleted code is actively wrong, not historically
   useful, unlike the paragraph entries above (which document past incidents/decisions, which stay
   useful as history). Grep the table specifically (it's the markdown table under "## Common
   Tasks — Where to Make Changes") for any row whose target file/function was deleted in T3.1-T3.4
   — confirm each one against the actual current file tree before removing (don't guess from the
   plan's own memory of what it deleted — verify fresh).
5. **`docs/DESIGN_GUIDE.md`** — check whether it documents Bracket/GTT as a live architectural
   component (search for "Bracket", "GTT", "template_attach"). If so, update the relevant
   section(s) to reflect removal, consistent with the rest of this sprint. Regenerate the PDF via
   `python3 docs/generate_pdf.py` only if DESIGN_GUIDE.md was actually touched.

## Agents

- doc: implement items 1-5 above.

  Before touching CLAUDE.md, read the ENTIRE file first (it's ~4000 lines) so you have full
  context on which entries are primarily-Bracket vs incidentally-mentioning. This file has a
  documented history of other features being marked historical (search for "REMOVED" or
  "Historical record only" for the existing convention/wording to match). Do not delete any
  paragraph entry — only the Common-Tasks table rows get deleted outright, per item 4's own
  reasoning (a current-state table pointing at dead code is wrong; a historical narrative entry
  documenting a past incident/decision stays useful).

  For item 3, do a GREP-FIRST pass (search `CLAUDE.md` for `Bracket`, `GTT`, `bracket_registry`,
  `template_attach`, `TemplateBar`, `OrderTemplate`) to enumerate every candidate paragraph before
  deciding which get the historical-record marker — report your full enumeration and classification
  (primarily-Bracket vs incidental-mention) in your final report so it can be spot-checked, not
  just a final diff with no reasoning trail.

  Do NOT touch: any backend or frontend code file (all done, T3.1-T3.4), the live Track-1
  `exit-tp-short-option-90` entries in CLAUDE.md (these are about a DIFFERENT, still-live feature
  that happens to be documented in the same file — do not confuse with Bracket), the orphaned-agent
  cleanup entry (also unrelated, separate cleanup done this session).

- backend: skip
- frontend: skip
- broker: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: no
- playwright: no

## Commit message

docs(bracket-removal): T3.5 — retire GTT_SPEC.md, remove Bracket guide sections, mark CLAUDE.md historical entries, clean Common Tasks table

## Done when

- `docs/specs/GTT_SPEC.md` is deleted.
- ADMIN_GUIDE.md/USER_GUIDE.md/ORDER_LIFECYCLE.md's Bracket/Template sections are gone;
  USER_GUIDE.md's "Adding your own agent" subsection is re-homed under an appropriate heading, not
  deleted.
- CLAUDE.md's ~21 primarily-Bracket paragraph entries carry a historical-record marker, verbatim
  technical content otherwise preserved; entries that only incidentally mention Bracket/GTT are
  untouched.
- CLAUDE.md's Common Tasks table has zero rows pointing at a deleted file/function — verified
  against the actual current file tree, not assumed.
- DESIGN_GUIDE.md checked; updated + PDF regenerated only if it actually referenced Bracket/GTT as
  live.
- No code file touched — `git diff --name-only` shows only `docs/` and `CLAUDE.md`.
