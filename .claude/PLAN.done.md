# Plan: After plan approval — hard stop, wait for explicit command

## Context

After `ExitPlanMode` is approved, Claude sometimes proceeds to implementation immediately,
triggering permission prompts. The operator wants a hard stop: after the plan is approved,
Claude outputs "Plan ready — run `/impl` or `/depl`" and waits silently. No implementation,
no permission prompts. Only an explicit `/impl`, `/ddev`, `/dprod`, or `/depl` command
from the operator triggers execution (which runs in bypass-permissions mode via frontmatter).

## Agents
- backend: skip
- frontend: skip
- broker: skip
- doc: Two file edits only — CLAUDE.md and memory file. No subagent needed.
- backend-test: skip
- playwright: skip

## Changes

### Change A — CLAUDE.md line 113
File: `CLAUDE.md` (project root)

Find:
```
**Plan before implement** — always enter plan mode for non-trivial tasks. During plan mode, write `.claude/PLAN.md` using the format below, then call ExitPlanMode for operator approval. After ExitPlanMode, always append: *"Plan ready — run `/impl` to build only, or `/depl` to build + deploy to prod."*
```

Replace with:
```
**Plan before implement** — always enter plan mode for non-trivial tasks. During plan mode, write `.claude/PLAN.md` using the format below, then call ExitPlanMode for operator approval. After ExitPlanMode, output exactly: *"Plan ready — run `/impl` to build only, or `/depl` to build + deploy to prod."* Then **STOP**. Do not start implementing. Do not ask for permissions. Do not take any further action. Wait silently for the operator to run `/impl`, `/ddev`, `/dprod`, or `/depl`.
```

### Change B — memory file
File: `/Users/ramanambore/.claude/projects/-Users-ramanambore-projects-ramboq/memory/feedback_plan_next_command.md`

Replace body with:

```markdown
After calling `ExitPlanMode`, output exactly:

> Plan ready — run `/impl` to build only, or `/depl` to build + deploy to prod.

Then **STOP completely**. Do not start implementing. Do not ask for permissions. Do not take any further action. Wait for the operator to explicitly run one of: `/impl`, `/ddev`, `/dprod`, `/depl`.

**Why:** Running implementation immediately after plan approval triggers permission prompts before the bypass-permissions frontmatter in those command files can take effect. The operator must invoke the command themselves so the CLI loads the allowed-tools frontmatter first.

**How to apply:** Every call to ExitPlanMode ends with the one-line prompt above, then silence. No further tool calls, no file reads, no implementation steps.
```

## Tests
- pytest: no
- svelte-check: no
- playwright: no

## Commit message
fix(workflow): hard stop after ExitPlanMode — wait for explicit impl/depl command

## Done when
- CLAUDE.md Default Workflow paragraph explicitly says STOP after ExitPlanMode
- Memory rule says STOP and explains why (bypass-permissions frontmatter needs CLI to load command)
