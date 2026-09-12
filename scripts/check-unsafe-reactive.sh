#!/usr/bin/env bash
# Detect bare get() calls inside $derived blocks without untrack wrapping.
# Exits non-zero if violations found.
set -euo pipefail

VIOLATIONS=$(grep -rn \
  --include="*.svelte" --include="*.svelte.js" \
  -E '\$derived[^;{]*get\(' \
  frontend/src/ | grep -v 'untrack\|safeRead\|// safe' | grep -vE ':[[:space:]]*\*[[:space:]]|:[[:space:]]*//' || true)

if [[ -n "$VIOLATIONS" ]]; then
  echo "state_unsafe_mutation risk: bare get() inside \$derived without untrack/safeRead:"
  echo "$VIOLATIONS"
  exit 1
fi
echo "no unsafe reactive reads detected"
