# Plan: Add Glob + Grep to pipeline command allowed-tools

## Context

The project uses `defaultMode: "plan"` in `.claude/settings.json`. The four pipeline command files (`depl.md`, `impl.md`, `ddev.md`, `dprod.md`) have `allowed-tools` frontmatter that bypasses permission prompts during execution. However, `Glob` and `Grep` are missing from the list. If the coordinator ever calls those tools directly, Claude Code prompts the user for permission.

Note: the ExitPlanMode plan-review interaction at the start of `/depl` (when plan mode is active) is unavoidable — it is the plan approval step by design.

## Change

**Files:** `.claude/commands/depl.md`, `.claude/commands/impl.md`, `.claude/commands/ddev.md`, `.claude/commands/dprod.md`

In each file, update the `allowed-tools` frontmatter line from:
```
allowed-tools: Bash, Read, Write, Edit, Agent, ExitPlanMode, EnterPlanMode, ToolSearch
```
to:
```
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent, ExitPlanMode, EnterPlanMode, ToolSearch
```

## Agents
- frontend: skip
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: no
- playwright: no

## Commit message
chore(commands): add Glob + Grep to pipeline allowed-tools to reduce permission prompts

## Done when
- All four command files have Glob and Grep in allowed-tools frontmatter
- No new permission prompts appear during /impl, /ddev, /dprod, /depl pipeline runs (except the unavoidable ExitPlanMode plan-review step)
