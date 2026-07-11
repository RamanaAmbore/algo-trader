---
name: TLM Quick Gate
description: Run CCWATCH + PYCHECK only — fast quality gate (~35s). Use before any git push or after a targeted fix to confirm no regression.
allowed-tools: Bash
---

Run the fast TLM gate:

```bash
cd /Users/ramanambore/projects/ramboq && source venv/bin/activate && python tools/tlm/run_all.py --tools ccwatch,pycheck
```

Report results concisely. If any test is newly failing, name it. If CC regressed, name the function and grade change.
