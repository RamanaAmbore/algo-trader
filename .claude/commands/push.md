# /push — Commit and push to dev + main

Commit staged changes and push to both dev and main branches.

## Steps

1. Change to project directory: `cd /Users/ramanambore/projects/ramboq`

2. Check staged status: `git status`

3. Show diff summary: `git diff --cached --stat`

4. Prompt for commit message if not provided; use ARGUMENTS as message if available

5. Commit: `git commit -m "<message>"`

6. Push to dev: `git push origin dev`

7. Switch to main, merge dev, push main, switch back:
   ```bash
   git checkout main
   git merge dev
   git push origin main
   git checkout dev
   ```

8. Report:
   - Commit SHA (short hash)
   - Both push results (dev: OK, main: OK)
   - Branch now at: dev

## Output format

```
Committed: abc1234 "chore(audit): daily fix — P1: XYZ"

Pushed to origin/dev:   OK
Pushed to origin/main:  OK

Branch: dev (merged to main)
```

## Error handling

- If merge conflict on main: abort merge, report conflict, suggest manual resolution
- If push fails: show git error message, suggest checking branch protection rules
