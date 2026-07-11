#!/usr/bin/env bash
# Post-edit quality gate — runs targeted tests after Edit/Write to backend or frontend files.
# Invoked by Claude Code PostToolUse hook. Reads tool input JSON from stdin.

set -euo pipefail
REPO="/Users/ramanambore/projects/ramboq"

# Parse file path from stdin JSON (tool_input.file_path or tool_input.path)
TOOL_JSON=$(cat)
FILE=$(echo "$TOOL_JSON" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    ti = d.get('tool_input', d)
    print(ti.get('file_path', ti.get('path', '')))
except:
    print('')
" 2>/dev/null || echo "")

[ -z "$FILE" ] && exit 0

# Normalise to relative path
FILE="${FILE#$REPO/}"

# --- Backend gate ---
if echo "$FILE" | grep -qE "^backend/api/.*\.py$"; then
    MOD=$(basename "$FILE" .py)
    cd "$REPO"
    source venv/bin/activate
    # Find related test files
    TESTS=$(find backend/tests -name "test_${MOD}*.py" -o -name "test_*${MOD#_}*.py" 2>/dev/null | sort -u | head -5 | tr '\n' ' ')
    if [ -n "$TESTS" ]; then
        echo "[post-edit-gate] Running related tests for $MOD..."
        python -m pytest $TESTS -q --tb=line 2>&1 || true
    else
        echo "[post-edit-gate] No test file found for $MOD — consider adding tests."
    fi
    exit 0
fi

# --- Frontend gate ---
if echo "$FILE" | grep -qE "^frontend/.*\.(svelte|js|ts)$"; then
    cd "$REPO/frontend"
    echo "[post-edit-gate] Running svelte-check for $FILE..."
    npx svelte-check --output machine 2>&1 | grep -E "ERROR|WARNING" | head -20 || true
    exit 0
fi

exit 0
