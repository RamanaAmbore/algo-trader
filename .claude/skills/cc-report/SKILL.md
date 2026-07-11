---
name: CC Complexity Report
description: Show cyclomatic complexity for all backend hotspot files. Highlights F/D-grade functions that need refactoring.
allowed-tools: Bash
---

```bash
cd /Users/ramanambore/projects/ramboq && source venv/bin/activate && radon cc backend/api/algo/actions.py backend/api/algo/agent_engine.py backend/api/algo/template_attach.py backend/api/background.py backend/api/algo/nav.py -s --min C 2>/dev/null
```

Format as a markdown table sorted by CC score (highest first) with columns: File | Function | CC | Grade | Status.
- Grade F (CC>25): bold — needs refactoring
- Grade D (CC 16-25): warn
- Grade C or below: ok

Note which F-grade functions already have characterization tests (safe to refactor now) vs which still need tests first. Check `backend/tests/` for test files matching the function name.
