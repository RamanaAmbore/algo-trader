---
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent, ExitPlanMode, EnterPlanMode, ToolSearch, Monitor, TaskCreate, TaskUpdate, TaskGet, TaskList, TaskOutput, TaskStop
---

# /depl — Full deploy pipeline: impl → ddev → dprod

Run the complete build-and-deploy pipeline in sequence: implement the plan, gate on tests,
and ship to prod. All three phases execute with bypass-permissions (no tool-use prompts).
Long-running steps (pytest, svelte-check, PDF regen, CC check) run as background Bash
processes; use Monitor to collect each result. Returns to plan mode when done.

## Permissions

**Step 1 — Exit plan mode (if active):** If plan mode is currently active, call `ExitPlanMode` (no `allowedPrompts`) as the very first step.

**Step 2 — Engage bypass mode:** Immediately after ExitPlanMode (or as the very first step if not in plan mode), run:
```bash
.claude/set-bypass.sh
```
This may be denied by the permission system (it mutates global CLI settings). That
denial is not a pipeline blocker — continue with individually-approved tool calls
through all three phases.

**Final step — Restore plan mode:** Before calling `EnterPlanMode` (after the dprod report or on any hard block), run:
```bash
.claude/set-plan.sh
```
Then call `EnterPlanMode`. Also best-effort — if denied, call `EnterPlanMode` anyway.

---

## Phase 1 — impl (build)

Follow all steps from `/impl` exactly — the full command (Steps 0-7), not a restated
subset, so this phase can never drift out of sync with the standalone command (e.g.
its `/tprep`-reuse coverage gate, its HEREDOC commit convention, its step numbering).
Do NOT call `EnterPlanMode` on its "Final step" — continue to Phase 2 instead.

---

## Phase 2 — ddev (gate + push dev)

Follow all steps from `/ddev` exactly — the full command, not a restated subset
(its spec-sync gate and exact decision/report wording are part of the contract and
must not drift out of sync with the standalone command). Do NOT call `EnterPlanMode`
on its "Final step" — continue to Phase 3 instead.

---

## Phase 3 — dprod (docs + merge + prod)

Follow all steps from `/dprod` exactly — the full command, not a restated subset, so
a CC-gate or coverage block here always carries `/dprod`'s current fix-path pointers
(`/refact`, `/tprep`) with no risk of this file's own copy drifting stale.

After Phase 3's own report, prefix/merge it into the combined `depl` summary:
```
depl: impl ✓ | ddev ✓ | dprod ✓
→ <dprod's own foreground report, verbatim>
```

---

## Final step — Enter plan mode

After the Phase 3 report (or on any block at any phase), call `EnterPlanMode` to return
to plan mode.

---

## Notes

- Never push to prod without an explicit prior operator request ("deploy" / `/dprod` / `/depl`).
- If impl fails tests after 3 iterations, stop at Phase 1 — do not push anything.
- If ddev tests fail, stop at Phase 2 — do not merge to main.
- If CC gate fails, stop at Phase 3 — do not merge.
- Each phase must fully complete before the next begins.
