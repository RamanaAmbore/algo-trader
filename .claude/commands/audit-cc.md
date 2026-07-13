# /audit-cc — CC audit before push (blocker)

Run cyclomatic complexity audit and block push if any D/E/F-grade functions exist.

## Steps

1. Change to project directory: `cd /Users/ramanambore/projects/ramboq`

2. Run audit: `python3 -m radon cc backend/ -s -n D 2>/dev/null`

3. Check results:
   - If ANY D/E/F functions found:
     - List them with CC scores
     - Show file paths
     - Output: "BLOCKED: CC audit failed — D/E/F functions detected"
     - Ask operator: "Fix complexity hotspots and re-run audit before pushing"
     - Exit with status 1 (do NOT proceed to push)
   
   - If clean (all A/B/C or better):
     - Output: "PASSED: CC audit clean — all functions <= C (CC 20)"
     - Proceed to push

## Output format (blocked)

```
CC Audit: BLOCKED
=================

Found D/E/F-grade functions:

D-Grade (CC 21-30):
- backend/api/routes/orders_place.py:ticket_order_handler (CC: 42)
- [...]

E/F-Grade (>30):
- backend/api/routes/orders.py:_postback_broadcast_fanout (CC: 35)

Action: Fix complexity hotspots before push
```

## Output format (passed)

```
CC Audit: PASSED
================

All functions <= C (CC 20)
Status: Ready to push
```

## Integration

When audit passes, caller may proceed to `/push` or other deployment steps.
When audit fails, work is blocked — operator must fix before re-running.
