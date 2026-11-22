# /refact — Batch complexity-gate refactor, fix-to-clean

Unattended complexity pass: find every function that fails the project's own CC
gate (read live from CLAUDE.md / the actual radon invocation already in use —
never a hardcoded threshold here), refactor it down to a clean grade with no
behavior change, and re-run the full test suite to confirm nothing broke. No
per-step confirmation — batch, background, cost-efficient.

## Step 1 — Read current constraints

1. Read `CLAUDE.md`'s **Development Commands** section for the exact complexity-gate
   command already in use: `venv/bin/python -m radon cc backend/ -s -n D`.
   This is the SAME command `/cc`, `/audit-cc`, `/dprod`, and `/depl` all already
   use — reuse it verbatim, do not invent a different threshold or flag set.
2. The gate blocks on any D/E/F grade (CC ≥ 21). A/B/C stay as-is — this command
   does not chase cosmetic complexity reduction below the gate's own bar.

## Step 2 — Find the hotspots

1. Run (background, `run_in_background: true`):
   ```
   cd /Users/ramanambore/projects/ramboq && venv/bin/python -m radon cc backend/ -s -n D 2>/dev/null
   ```
2. If frontend complexity tooling exists in this repo (check for an eslint
   complexity rule or a JS equivalent of radon before assuming none), run that too.
   If none exists, skip — do not invent a new frontend complexity gate unprompted.
3. Rank hotspots worst-first (E/F before D), and group by file-overlap the same
   way `/tprep` does — never split two hotspots in the same file across two agents.

## Step 3 — Batch dispatch (parallel, background)

1. Dispatch `backend` agents (one per independent file-group) with the specific
   function name(s), their current CC score, and the file. Brief: reduce
   cyclomatic complexity below grade D by extracting well-named helper functions
   or flattening conditional nesting — per CLAUDE.md's own Key Patterns precedent
   (e.g. the `_breaker_guard_frame()`/`_interval_gate_frame()`/`_lkg_or_fallback()`
   extraction that brought `_fetch_holdings_local`/`_fetch_positions_local` back to
   C-grade). Explicitly forbid:
   - Changing behavior, return values, or error handling semantics.
   - Adding new abstractions beyond what's needed to split the one function.
   - Touching any file not in its own assigned group (avoid cross-agent collisions).
2. Use `sonnet` (the project default) for this — complexity-safe refactoring needs
   real judgment about control flow, unlike routine test authoring. Do not use
   `haiku` here.

## Step 4 — Loop to green (max 5 iterations)

1. After each wave of refactors, run in parallel (background):
   - `venv/bin/python -m radon cc backend/ -s -n D 2>/dev/null` (must be empty)
   - The full existing test suite + coverage gates (same commands `/ddev` uses):
     ```
     cd /Users/ramanambore/projects/ramboq && \
       venv/bin/pytest backend/tests/ -q --tb=line \
         --cov=backend/brokers --cov=backend/api --cov-report=term-missing && \
       venv/bin/coverage report --include="backend/brokers/*" --fail-under=80 && \
       venv/bin/coverage report --include="backend/api/*" --fail-under=45
     ```
     (Use whatever the CURRENT thresholds in CLAUDE.md say, not the literal 80/45
     above if those have since changed — re-read Step 1 of `/tprep`'s own logic if
     unsure.)
2. Any new test failure caused by the refactor: fix the refactor, not the test —
   a refactor must never change observable behavior, so a newly-failing
   pre-existing test means the extraction introduced a real regression.
3. Any remaining D/E/F grade: dispatch one more narrower round of Step 3.
4. Stop and escalate to the operator if the same hotspot still fails after 2
   refactor attempts, or 5 iterations are reached with the gate still red —
   do not keep retrying a function that resists simple extraction.

## Step 5 — Commit (workshop only, never push)

1. `git add` the refactored files by explicit path.
2. Commit to `workshop`:
   ```
   refactor: reduce complexity below the CC gate — <short file list>

   <one line per function: old CC → new CC, extraction approach>
   ```
3. Do NOT run `/ddev` or `/dprod` — leave the push decision to the operator.

## Foreground report

```
refact: cleared N hotspot(s) across M file(s)
  <function>: CC <old> → <new>  (x N)
  full suite: <N> passed, 0 failed | CC gate clean
→ committed workshop <hash>
```

On an unresolved hotspot after 5 iterations:
```
refact: BLOCKED — <function> in <file> still grade <D/E/F> (CC <n>) after 2 attempts
Needs operator judgment on how to split it — stopping rather than forcing a risky extraction.
```
