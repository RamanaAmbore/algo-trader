# RAMBOQ-TLM Guide

Technical Lifecycle Management — automated quality gates for the RamboQuant codebase.

---

## 1. What is RAMBOQ-TLM

TLM is a named, structured suite of quality tools that run against the RamboQuant
monorepo. Each tool is independently runnable, produces a structured `TlmResult`
(ok / warn / fail / skip), and emits a severity-ranked punch list.

**Philosophy**: _sync + no new defects_. Every tool measures one dimension of
quality against a prior baseline. A run is "green" when nothing regressed since
yesterday. New defects introduced in the last push surface as P1 findings and
block release.

Tools live in `tools/tlm/`. The master orchestrator is `tools/tlm/run_all.py`.

---

## 2. Tool Catalog

| Name | Script | Purpose | Severity Levels | When to Use |
|------|--------|---------|----------------|-------------|
| CCWATCH | `ccwatch.py` | Cyclomatic complexity snapshot + grade regression | P2 (new/regressed F-grade), P3 (existing F tracked) | Every commit; post-commit hook |
| PYCHECK | `pycheck.py` | Pytest baseline tracker — detects new test failures | P1 (new failure vs yesterday) | Pre-push; daily cron; `--quick` for targeted runs |
| PERFMON | `perfmon.py` | API route performance regression monitor | P2 (>20% regression) | Weekly; after background-task changes |
| SNAPCHECK | `snapcheck.py` | Stale closed-hours snapshot detector | P1 (NAV/positions snapshot stale) | Daily cron; after market-close logic changes |
| DEPSCAN | `depscan.py` | pip-audit + npm audit CVE scanner | P1 (CRITICAL/HIGH), P2 (MEDIUM), P3 (LOW) | Weekly; after `pip install` or `npm install` |
| DOCDRIFT | `docdrift.py` | DESIGN_GUIDE.md drift vs recent architectural commits | P3 (doc not updated in commit window) | Daily cron; after architectural changes |

### Severity / Exit Code Semantics

| Status | Severity | Exit Code | Meaning |
|--------|----------|-----------|---------|
| ok | (none) | 0 | All checks passed |
| warn | P3 | 0 | Minor findings; no immediate action required |
| warn | P2 | 1 | Significant findings; investigate before next release |
| fail | P1 | 1 | Critical finding; block release or alert on-call |
| skip | (none) | 2 | Tool could not run (dependency or directory missing) |

---

## 3. Slash Commands

Five project-level slash commands are registered in `.claude/skills/`. Invoke them
with `/skill-name` in the Claude Code chat.

### `/tlm` — Full Suite

Runs all six TLM tools in sequence and writes a markdown audit report to
`docs/audits/AUDIT_YYYY-MM-DD.md`.

**Accepts**: no extra arguments — date stamp is automatic.

**Expected output**: consolidated status table + per-tool finding list if any P1/P2
are present, followed by a prompt asking whether to auto-dispatch fixes.

**When to use**: after a batch of changes; before pushing to prod; end-of-sprint.

---

### `/tlm-quick` — Fast Gate

Runs CCWATCH + PYCHECK only (~35 seconds). No markdown output file.

**Accepts**: no extra arguments.

**Expected output**: two-line status (one per tool). If CCWATCH regressed, names
the function and its old vs new grade. If PYCHECK found a new failure, names it.

**When to use**: before any `git push`; after a targeted bug fix to confirm no
regression.

---

### `/design-sync` — DESIGN_GUIDE Sync

Reads the 24-hour git log, makes additive edits to `docs/DESIGN_GUIDE.md`, then
regenerates `DESIGN_GUIDE.pdf` via `python3 generate_pdf.py`.

**Accepts**: no extra arguments.

**Safety invariants**:
- Never removes existing content from DESIGN_GUIDE.md.
- Verifies `git diff docs/DESIGN_GUIDE.md | grep "^-"` shows zero unexpected
  deletions before committing.
- Commits both `.md` and `.pdf` in one `docs(design): sync …` commit.

**When to use**: after any route signature change, background task change, broker
guard change, F&O math change, or new module addition.

---

### `/6d-audit` — 6-Dimension Opus Audit

Dispatches an Opus-class audit agent to audit recent commits across six dimensions:

| Dimension | Focus |
|-----------|-------|
| D1 Correctness | Logic bugs, off-by-one, missing guards, wrong math |
| D2 Performance | Sync in async, N+1 queries, unbounded loops, missing caches |
| D3 Dead Code | Unused imports, dead branches, stale vars, unreachable paths |
| D4 UX Consistency | Palette violations, density gaps, missing component reuse |
| D5 Broker API Compliance | Lot-size math, G1/G2 guards, translate_qty, GTT quantities |
| D6 Doc Alignment | DESIGN_GUIDE claims vs actual code paths/formulas/routes |

**Accepts**: no extra arguments.

**Expected output**: severity-ranked punch list (P1/P2/P3) with exact `file:line`
citations, one line per finding.

**When to use**: weekly; after any sprint with >5 commits touching multiple layers.

---

### `/cc-report` — CC Complexity Report

Runs `radon cc` on the five highest-complexity backend files and formats a markdown
table sorted by CC score.

**Accepts**: no extra arguments.

**Expected output**: markdown table with columns `File | Function | CC | Grade | Status`.
F-grade (CC>25) functions are bolded. Notes which have characterisation tests and
are safe to refactor now.

**When to use**: before any refactoring sprint; when adding a new branch to an
existing complex function.

---

## 4. Hooks

Two shell hooks in `.claude/hooks/` fire automatically via `.claude/settings.json`
`PostToolUse` rules.

### `post_edit_gate.sh` — fires after Edit or Write

Reads the edited file path from the tool's JSON input. Then:

- **backend/api/\*.py** — finds matching test files in `backend/tests/test_<MODULE>*.py`
  and runs them with `python -m pytest … -q --tb=line --timeout=30`. If no test
  file is found, prints a reminder.
- **frontend/\*.(svelte|js|ts)** — runs `npx svelte-check --output machine` and
  surfaces only ERROR/WARNING lines.
- All other files — exits silently (exit 0).

The hook runs **async** (`"async": true`) — it does not block Claude's next action.
Output appears in the Claude Code terminal panel after Claude finishes its step.

### `post_commit_ccwatch.sh` — fires after Bash

Reads the Bash command from tool input. Exits immediately if the command does not
contain `git commit`. Otherwise runs `python tools/tlm/ccwatch.py` to detect any
CC regression introduced by the commit.

Also runs **async** — does not block subsequent actions.

### Disabling hooks for a session

Hooks are defined in the project-level `.claude/settings.json`. To disable
temporarily for a session without editing the file, run Claude Code with:

```
CLAUDE_HOOKS_DISABLED=1 claude
```

Or remove/rename `.claude/settings.json` and restore it after the session.

---

## 5. Cron

Daily cron pipeline runs at **4:03 PM IST** (10:33 UTC) on weekdays — after the
equity close settlement window. Add to the server crontab:

```cron
33 10 * * 1-5 cd /opt/ramboq && source venv/bin/activate && \
  python tools/tlm/run_all.py \
  --output docs/audits/AUDIT_TLM_$(date +\%Y-\%m-\%d).md \
  --json \
  >> .log/tlm_cron.log 2>&1
```

**What it produces**:

| Artifact | Path |
|----------|------|
| Markdown audit report | `docs/audits/AUDIT_TLM_YYYY-MM-DD.md` |
| JSON machine-readable summary | `docs/audits/AUDIT_TLM_YYYY-MM-DD.json` |
| Cron run log | `.log/tlm_cron.log` (appended) |
| CCWATCH daily snapshot | `.log/cc_snapshot_YYYY-MM-DD.txt` |
| PYCHECK daily snapshot | `.log/pytest_snapshot_YYYY-MM-DD.txt` |

**Exit code semantics**: orchestrator exits 1 if any P1/P2 finding is present —
suitable for systemd `OnFailure=` or a monitoring wrapper that fires a Telegram
alert.

**Optional: auto-commit report**

```cron
33 10 * * 1-5 cd /opt/ramboq && source venv/bin/activate && \
  python tools/tlm/run_all.py \
  --output docs/audits/AUDIT_TLM_$(date +\%Y-\%m-\%d).md \
  --auto-commit \
  >> .log/tlm_cron.log 2>&1
```

---

## 6. Adding a New TLM Tool

1. Create `tools/tlm/mytool.py` with the standard shebang and path setup:

   ```python
   #!/usr/bin/env python3
   import sys
   from pathlib import Path
   sys.path.insert(0, str(Path(__file__).resolve().parent))
   from _base import TlmTool, TlmResult, TlmFinding, REPO_ROOT
   ```

2. Subclass `TlmTool`:

   ```python
   class MyTool(TlmTool):
       name = "MYTOOL"
       description = "One-line description."

       def add_args(self, parser):
           # Optional: add tool-specific CLI flags here
           pass

       def _print_dry_run_plan(self, args):
           print("  Would check X and Y...")

       def run(self, args) -> TlmResult:
           findings = []
           # ... populate TlmFinding(item, detail, severity) ...
           return self.build_result(self.name, findings, "All checks passed")

   if __name__ == "__main__":
       tool = MyTool()
       sys.exit(tool.main())
   ```

3. Register it in `tools/tlm/run_all.py`:

   ```python
   from mytool import MyTool
   ALL_TOOLS = [..., MyTool()]
   ```

4. Make it executable: `chmod +x tools/tlm/mytool.py`

5. Update `tools/tlm/README.md` tool catalog table.

6. Optionally add a skill: see section 7 below.

**Severity guidance**:
- P1 — blocks release (new test failure, stale NAV snapshot, CRITICAL CVE).
- P2 — investigate before next release (CC regression, route perf regression,
  HIGH CVE, new F-grade function).
- P3 — informational (MEDIUM/LOW CVE, doc drift, existing F-grade functions
  already tracked).

---

## 7. Adding a New Skill

Skills are project-level slash commands stored in `.claude/skills/<name>/SKILL.md`.

**Directory structure**:

```
.claude/skills/
  my-skill/
    SKILL.md      <- required; this is the entire skill definition
```

**SKILL.md frontmatter fields**:

| Field | Required | Values | Notes |
|-------|----------|--------|-------|
| `name` | yes | string | Display name shown in skill picker |
| `description` | yes | string | One-line shown in `/skills` list |
| `allowed-tools` | yes* | `Bash`, `Read`, `Edit`, `Write` (comma-separated) | Use when skill runs tools directly |
| `agent` | yes* | `audit`, `backend`, `frontend`, etc. | Use instead of `allowed-tools` to dispatch a subagent |
| `model` | no | `opus`, `sonnet`, `haiku` | Only meaningful with `agent:`; defaults to agent's default model |

*One of `allowed-tools` or `agent` is required.

**When to use `agent: audit` vs `allowed-tools: Bash`**:

- Use `allowed-tools: Bash` (or `Read`/`Edit`/`Write`) when the skill runs shell
  commands directly and returns structured text output — e.g., running a test suite,
  generating a report, checking file contents.
- Use `agent: audit` (or another agent name) when the skill needs a reasoning step
  across many files — e.g., reviewing a batch of commits for defects, cross-referencing
  code against a spec. The agent sees the full repo and can call all its own tools.
- Add `model: opus` only for heavyweight audit tasks. Sonnet is the default for
  all other agents and is sufficient for most skills.

**`!`-prefix in skill body**: a line starting with `` !` `` runs a shell command at
skill-invocation time and injects its output into the prompt. Use for dynamic context
(e.g., recent commits, current branch).

---

## 8. Troubleshooting

### SNAPCHECK fires a P1

SNAPCHECK calls `scripts/check_stale_snapshots.py`. A P1 means the DB has no
`daily_book` row for today's date for at least one active account, or the row's
`snapshot_extras.settled` flag is not set after the 15-minute close-settled window.

Steps:
1. Check `GET /api/admin/health` — look at `ticker.stale_count`.
2. Check `daily_book` table: `SELECT * FROM daily_book WHERE date = CURRENT_DATE ORDER BY account;`
3. If rows are missing: the `MarketLifecycle` lifecycle handler for `<exch>:close` did
   not fire. Check `background.py` task logs for `_task_market_lifecycle`.
4. If `settled=false`: the `<exch>:close_settled` event did not fire. The 15-minute
   delay is operator-tunable via `backend_config.yaml:market.close_settled_delay_minutes`.
5. To manually trigger: `POST /api/admin/lifecycle/trigger` (admin-guarded).

### Silencing a false-positive DOCDRIFT

DOCDRIFT flags P3 when architectural commits touch backend or frontend files but
DESIGN_GUIDE.md was not updated in the same `--since` window.

If the commit is genuinely non-architectural (e.g., a cosmetic fix, a log-level
change, a test-only change):
- Option A: add `[no-doc]` to the commit message — DOCDRIFT skips commits with
  that tag.
- Option B: run `/design-sync` to confirm the guide is already accurate and commit
  a no-op touch to DESIGN_GUIDE.md to reset the drift clock.

### Running a single TLM tool manually

Each tool is a standalone script:

```bash
cd /Users/ramanambore/projects/ramboq && source venv/bin/activate

# CCWATCH only
python tools/tlm/ccwatch.py

# PYCHECK full run
python tools/tlm/pycheck.py

# PYCHECK quick mode — only tests for 'nav' module
python tools/tlm/pycheck.py --quick nav

# DEPSCAN with JSON output
python tools/tlm/depscan.py --json

# DOCDRIFT with 7-day window
python tools/tlm/docdrift.py --since 7

# Dry run any tool (no side effects, no snapshot writes)
python tools/tlm/snapcheck.py --dry-run
```

### PYCHECK quick mode not matching expected tests

Quick mode matches `backend/tests/test_<MODULE>*.py`. If your test file uses a
different naming convention (e.g., `test_api_nav.py` for the `nav` module):

```bash
# Confirm what files exist
find backend/tests -name "test_nav*.py"

# Run manually with the full path
python -m pytest backend/tests/test_api_nav.py -q --tb=line
```

Quick mode does not update the daily snapshot — so a passing quick run does not
replace the need to run the full `pycheck.py` before the next cron cycle.
