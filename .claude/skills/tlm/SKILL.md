---
name: RAMBOQ-TLM Full Suite
description: Run the full RAMBOQ-TLM quality suite — CCWATCH + PYCHECK + DEPSCAN + DOCDRIFT + PERFMON + SNAPCHECK. Use after a batch of changes or before pushing to prod.
allowed-tools: Bash
---

Run the full TLM suite and report findings:

```bash
cd /Users/ramanambore/projects/ramboq && source venv/bin/activate && python tools/tlm/run_all.py --since 1 --output docs/audits/AUDIT_$(date +%Y-%m-%d).md
```

Print the consolidated status table exactly as it appears in stdout. If exit code is 1 (P1/P2 findings), list each finding with file:line reference. Ask the operator whether to auto-dispatch fixes.
