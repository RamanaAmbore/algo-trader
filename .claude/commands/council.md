# /council — Convene multi-perspective planning council

Dispatch all five council agents in parallel on the current task or plan draft.
Synthesize their verdicts and use the result to inform PLAN.md.

Invoked automatically during plan mode for non-trivial tasks.
Also invokable manually: `/council <task description>`.

## Steps

### Step 1 — Gather task context
If `$ARGUMENTS` is non-empty, use it as the task brief.
Otherwise read `.claude/PLAN.md` (draft or partial). If neither exists, ask the user.

### Step 2 — Dispatch council in parallel
Send a single message with **all five** Agent tool calls, each `run_in_background: true`:

| Agent | subagent_type |
|---|---|
| Architect | `council-architect` |
| Operator UX | `council-ux` |
| Risk | `council-risk` |
| Performance | `council-perf` |
| Devil's Advocate | `council-devil` |
| Research | `council-research` |

Each agent receives the same brief:
```
Project: RamboQuant (SvelteKit + Litestar, dark algo terminal UI, live trading)
Working directory: /Users/ramanambore/projects/ramboq

Task / proposed plan:
<paste full task brief or PLAN.md content>

Analyze from your specific perspective only. Read any relevant files you need.
Return your structured verdict exactly as your instructions specify.
```

### Step 3 — Collect results
Use Monitor to wait for all five agents to complete. Each returns a structured verdict
(## Section, **Verdict**: APPROVE|CONCERN|BLOCK, bullets, **Recommendation**).

### Step 4 — Synthesize
Count verdicts. Surface all BLOCKs and CONCERNs. Write synthesis:

```
Council verdict: N APPROVE · N CONCERN · N BLOCK → PROCEED | PROCEED WITH CHANGES | HOLD

Blocks (must resolve before proceeding):
- [AgentName] ...

Concerns (address in plan):
- [AgentName] ...

Plan adjustments:
- ...
```

### Step 5 — Integrate
If HOLD: surface blocks to operator, do not write PLAN.md yet.
If PROCEED WITH CHANGES: incorporate concerns into PLAN.md agent tasks and done-criteria.
If PROCEED: write PLAN.md as planned.

## Notes
- Council runs BEFORE PLAN.md is finalized, not after
- A single BLOCK pauses the plan until resolved
- CONCERNs become explicit tasks or watchpoints in PLAN.md
- Do not ask the operator to re-approve after council — fold findings directly into the plan
