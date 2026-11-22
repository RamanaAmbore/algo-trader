# /push — Quick commit and push dev + main (doc/config-only, no test gates)

Commit staged changes and push to both `dev` and `main`. **No test gates** — this is
deliberately a narrow escape hatch for doc/config-only changes
(`docs/`, `.claude/`, `CLAUDE.md`, YAML config) that `/ddev`/`/dprod` would otherwise
gate for no reason. Never use this for a code change under `backend/` or
`frontend/src/` — those must go through `/ddev` and `/dprod` (or `/depl`), which run
the real test/coverage/CC gates.

## Fixed bug (do not reintroduce)

The previous version of this command committed on whatever branch was currently
checked out, then ran `git push origin dev` / merged a LOCAL `dev` branch that never
received that commit — on this repo's actual workflow (commits land on `workshop`
first, per CLAUDE.md's branch strategy), that silently pushed nothing of the new
commit to either `dev` or `main`. The steps below follow the real
`workshop → dev → main` model, matching `/ddev`'s and `/dprod`'s own merge commands.

## Steps

1. `cd /Users/ramanambore/projects/ramboq`

2. `git status` — if the current branch is not `workshop`, STOP and report:
   ```
   push: BLOCKED — current branch is <branch>, not workshop.
   All commits go to workshop first (CLAUDE.md branch strategy). Checkout workshop,
   commit there, then re-run /push.
   ```
   Never commit directly on `dev` or `main`.

3. `git diff --cached --stat` — confirm every staged file is doc/config-only
   (`docs/`, `.claude/`, `CLAUDE.md`, `backend/config/*.yaml`, similar). If any staged
   file is under `backend/` (excluding config) or `frontend/src/`, STOP and report:
   ```
   push: BLOCKED — staged change touches <file>, which needs test gates.
   Use /ddev (+ /dprod) or /depl instead of /push for this change.
   ```

4. Prompt for a commit message if not provided; use `ARGUMENTS` as the message if given.

5. Commit on `workshop`, via a HEREDOC (never a `-m "..."` string with literal `\n`)
   and ending with whatever attribution trailer the current session's own
   system-reminder specifies:
   ```bash
   git commit -m "$(cat <<'EOF'
   <message>

   <Co-Authored-By trailer from the current session's attribution reminder>
   EOF
   )"
   git push origin workshop
   ```

6. Sync to dev (same pattern as `/ddev`'s own sync rule — pull before merging to
   avoid a divergence conflict):
   ```bash
   git fetch origin
   git checkout dev
   git pull origin dev
   git merge workshop --no-edit
   git push origin dev
   ```

7. Merge to main:
   ```bash
   git checkout main
   git merge dev --no-edit
   git push origin main
   ```

8. Fast-forward workshop back to main, so all three branches end at the same commit
   (CLAUDE.md's "sync rule after every dprod" invariant — applies here too, since this
   command also ends with a prod push):
   ```bash
   git checkout workshop
   git merge main --ff-only
   git push origin workshop
   ```

9. Report:
   - Commit SHA (short hash)
   - Push results for all three branches (workshop / dev / main: OK)
   - Confirmation all three branches are at the same commit

## Output format

```
push: committed abc1234 "chore(audit): daily fix — P1: XYZ"
→ pushed workshop, dev, main — all three at abc1234
```

## Error handling

- Merge conflict at any step (dev, main, or the final workshop fast-forward): abort
  the conflicting merge (`git merge --abort`), report the conflict with the files
  involved, and stop — do not attempt an automatic resolution. Suggest manual
  resolution, then re-running `/push` (or the individual merge) once resolved.
- Push failure (rejected, branch protection, network): show the git error verbatim,
  suggest `git fetch origin` + checking branch protection rules, and stop without
  retrying blindly.
- If Step 2 or Step 3's guard fires, stop immediately — do not proceed to commit.
