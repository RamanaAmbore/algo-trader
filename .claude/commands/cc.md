# /cc — Cyclomatic complexity grade summary

Show cyclomatic complexity grades for the backend codebase.

## Steps

1. Change to project directory: `cd /Users/ramanambore/projects/ramboq`

2. Run Radon complexity scan: `python3 -m radon cc backend/ -s -n C 2>/dev/null | head -80`

3. Summarize results:
   - Count of C-grade functions (CC 11-20)
   - Count of D-grade functions (CC 21-30)
   - Count of E/F-grade functions (CC >30)
   - Top 10 worst offenders by score (file:function CC_score)
   - Files with the most C+ functions (complexity distribution)

4. Show overall health trend (improvement / degradation vs baseline)

## Output format

```
Backend Cyclomatic Complexity Summary
======================================

Grade Distribution:
  A (CC 1-5):   XXX functions
  B (CC 6-10):  XXX functions
  C (CC 11-20): XXX functions
  D (CC 21-30): XXX functions
  E/F (>30):    XXX functions

Top 10 Hotspots (by complexity score):
1. backend/api/routes/orders_place.py:ticket_order_handler (CC: 42)
2. [...]

Files with Most C+ Functions:
- backend/api/routes/orders_place.py: 5 C/D functions
- [...]
```
