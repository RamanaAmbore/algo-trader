# /tlm — Daily TLM audit pipeline

Run the TLM daily pipeline and fix P1 findings.

## Steps

1. Change to project directory: `cd /Users/ramanambore/projects/ramboq`

2. Run TLM pipeline: `python3 tools/tlm/run_all.py 2>&1 | tail -100`

3. Parse output for P1 findings (PYCHECK failures, architectural drift, stale code)

4. For each P1: 
   - Read the failing test / audit point
   - Diagnose root cause in production code
   - Fix the issue (NOT the test)
   - Re-run relevant test to confirm green: `pytest backend/tests/<test_file> -v`

5. If DOCDRIFT shows architectural commits without doc coverage, update DESIGN_GUIDE.md accordingly

6. Commit audit report and any fixes:
   ```bash
   git add docs/audits/
   git commit -m "chore(tlm): daily audit $(date +%Y-%m-%d) — <P1_count> P1, <P2_count> P2, <P3_count> P3 — <summary>"
   ```

7. Show final P1/P2/P3 counts and list of fixed issues

## Output format

- Final counts: "P1: 0 fixed, P2: X remaining, P3: Y remaining"
- List fixed issues with file paths
- Link to updated DESIGN_GUIDE.md if changed
