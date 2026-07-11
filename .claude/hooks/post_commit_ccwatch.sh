#!/usr/bin/env bash
# Post-commit CC regression check — runs CCWATCH after every git commit.
# Invoked by Claude Code PostToolUse hook on Bash tool.

set -euo pipefail
REPO="/Users/ramanambore/projects/ramboq"

# Parse command from stdin JSON
TOOL_JSON=$(cat)
CMD=$(echo "$TOOL_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', d)
    print(ti.get('command', ''))
except:
    print('')
" 2>/dev/null || echo "")

# Only act on git commit commands
echo "$CMD" | grep -q "git commit" || exit 0

cd "$REPO"
source venv/bin/activate 2>/dev/null || true
echo "[post-commit] Running CCWATCH..."
python tools/tlm/ccwatch.py 2>&1 || true
exit 0
