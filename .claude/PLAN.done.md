# Plan: Broker Issue Daily Tracker + TLM CONNCHECK + Audit Fixes

## Context
`broker_connection_events` already captures every auth_fail, fetch_fail, circuit_open, and rotation_detected event but there is no daily rollup, no threshold gate, and no automated check that catches a broker going "noisy" before it escalates. The TLM suite is also carrying four unresolved findings from the last 6d-audit + DEPSCAN run. Deploy notifications were missing Telegram delivery (5f0a6d5c removed the Telegram block without a replacement) and had no receive-side verification. This plan adds a daily aggregation table, a scheduled background task, a new CONNCHECK TLM tool, fixes the outstanding audit findings, and adds ntfy receipt monitoring so deploy alerts are verified end-to-end.

---

## Workstream A — Broker issue daily tracker + TLM CONNCHECK

### A1 · New model: `BrokerIssueDaily`
**File:** `backend/api/models.py`

Add after `BrokerConnectionEvent`:
```python
class BrokerIssueDaily(Base):
    __tablename__ = "broker_issue_daily"
    __table_args__ = (
        UniqueConstraint("broker_id", "account", "issue_date",
                         name="uq_broker_issue_daily_broker_account_date"),
        Index("ix_broker_issue_daily_date", "issue_date"),
        Index("ix_broker_issue_daily_broker_date", "broker_id", "issue_date"),
    )
    id:          Mapped[int]      = mapped_column(primary_key=True, autoincrement=True)
    broker_id:   Mapped[str]      = mapped_column(String(32), nullable=False)
    account:     Mapped[str]      = mapped_column(String(32), nullable=False)
    issue_date:  Mapped[date]     = mapped_column(Date, nullable=False)
    issue_count: Mapped[int]      = mapped_column(Integer, nullable=False, default=0)
    breakdown:   Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # breakdown = {"auth_fail": N, "fetch_fail": N, "circuit_open": N, "rotation_detected": N}
    updated_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                   nullable=False,
                                                   default=lambda: datetime.now(timezone.utc),
                                                   onupdate=lambda: datetime.now(timezone.utc))
```

### A2 · Migration
**File:** `backend/api/persistence/migrations.py`

Add `create_broker_issue_daily_table(conn)` async function with IF NOT EXISTS DDL. Register it in `init_db()`.

### A3 · Background aggregation task: `_task_broker_issue_daily`
**File:** `backend/api/background.py`

- Runs once at startup (aggregates yesterday, catches missed runs) and then daily at **23:45 IST**
- Queries `broker_connection_events` for the target date:
  ```sql
  SELECT broker_id, account, event_type, COUNT(*) AS cnt
  FROM broker_connection_events
  WHERE event_type IN ('auth_fail','fetch_fail','circuit_open','rotation_detected')
    AND event_ts::date = :target_date
  GROUP BY broker_id, account, event_type
  ```
- Pivots into per-(broker_id, account) breakdown dict
- UPSERTs into `broker_issue_daily` on (broker_id, account, issue_date)
- Wire through `_supervised` (existing pattern)

### A4 · New TLM tool: CONNCHECK
**File:** `tools/tlm/conncheck.py`

Subclass `TlmTool`. Delegates to `scripts/check_broker_conn_issues.py`.
Exit-code mapping:
- 0 → ok
- 1 → P1 (threshold exceeded)
- 2 → P2 (warn-level exceeded)
- 3 → skip (DB unreachable)

**File:** `scripts/check_broker_conn_issues.py`

- Reads `backend/config/backend_config.yaml` for `broker_issue_thresholds`
- Reads `backend/config/secrets.yaml` for DB creds (same pattern as `check_stale_snapshots.py`)
- Queries `broker_issue_daily` for the last 7 days
- Thresholds (configurable in backend_config.yaml):
  - `auth_fail_p1: 10`  per account per day → P1
  - `circuit_open_p1: 5` per account per day → P1
  - `total_p1: 50`  per account per day → P1
  - `total_p2: 20`  per account per day → P2
- Output lines: `P1 broker=dhan account=DH6847 date=2026-07-24 total=67 (auth_fail=43, circuit_open=8)`
- Exit 1 if any P1, exit 2 if only P2, exit 0 if clean, exit 3 if DB down

**File:** `backend/config/backend_config.yaml`

Add under a new top-level key:
```yaml
broker_issue_thresholds:
  auth_fail_p1: 10
  circuit_open_p1: 5
  total_p1: 50
  total_p2: 20
  lookback_days: 7
```

**File:** `tools/tlm/run_all.py`

Register CONNCHECK between SNAPCHECK and DEPSCAN.

---

## Workstream B — TLM audit findings

### B1 · Fix `test_paper_engine_startup.py` hang (PYCHECK P1)
**File:** `backend/tests/test_paper_engine_startup.py`

Read the test and identify what's hanging (likely waiting for an async task or real network/DB connection). Add a `pytest.mark.timeout` or mock the blocking dependency. The fix must allow the full test suite to complete within 5 minutes.

### B2 · Dependency bumps (DEPSCAN P2)
**File:** `requirements.txt` (or `pyproject.toml`)

- `litellm` → `>=1.83.14` (fixes sandbox escape, role escalation, SSRF/LFI, unauthenticated session)
- `pyasn1` → `>=0.6.4` (fixes BER/DER DoS)
- `mcp` — check if a patched version is available for the session-confusion + unauth WebSocket CVEs; if so bump, if not document as accepted risk in `docs/audits/AUDIT_2026-07-24.md`

### B3 · Restore email for order_failure + agent_alert (6d-audit risk)
**File:** `backend/config/backend_config.yaml:171,181`

The 5f0a6d5c alert routing refactor silently set `order_failure.email: false` and `agent_alert.email: false`. Restore to `true` (matching pre-refactor behavior) unless there's an explicit operator decision to disable. Add a comment explaining the intent.

### B4 · Fix `alert_utils.py` drift (6d-audit P3)
**File:** `backend/shared/helpers/alert_utils.py`

- `:297` — Add `from collections.abc import Callable` under `TYPE_CHECKING` guard
- `:325` — Simplify triple-redundant ntfy guard to `if ntfy_priority:`
- `:526-527` — Fix comment: `"open/close → info channel, **email: true** per backend_config.yaml"`

---

## Workstream C — ntfy deploy receipt monitoring

### C1 · Receipt-polling script: `scripts/monitor_ntfy_deploy.py`
**File:** `scripts/monitor_ntfy_deploy.py`

- Polls `GET {ntfy_url}/{ntfy_topic}/json?poll=1&since=90s` (Bearer auth if `ntfy_token` set)
- Reads secrets from `backend/config/secrets.yaml` (same pattern as `check_broker_conn_issues.py`)
- Accepts `--title-contains` arg (default: `"Deploy"`) — verifies at least one message matches
- Accepts `--timeout` (default: `30`) seconds to wait for the notification before giving up
- Prints matched message title + timestamp on success
- Exit 0 = receipt confirmed; Exit 1 = no matching message in window; Exit 2 = config/network error
- References `test_ntfy_integration.py:_poll_ntfy()` pattern (`GET /topic/json?poll=1&since=Xs` newline-delimited JSON)

### C2 · Wire into deploy.sh
**File:** `webhook/deploy.sh`

After the successful `notify_deploy.py` call (success path only — not in the `ERR` trap), add:
```bash
python3 webhook/monitor_ntfy_deploy.py --title-contains "Deploy" --timeout 30 \
    || echo "[deploy] ntfy receipt check failed — notification may not have arrived"
```
Non-blocking: a failed receipt check logs a warning but does not fail the deploy.

### C3 · Unit tests (existing file)
**File:** `backend/tests/test_deploy_notify.py`

Already updated to return `(mock_ntfy, mock_tg)` tuple from `_run_main()`. Add tests:
- `test_layers_in_ntfy_body` — layers string in ntfy body (main branch)
- `test_layers_in_telegram_body` — layers string in Telegram message JSON
- `test_deploy_type_flag_in_deploy_sh` — `--layers` in deploy.sh text

### C4 · Integration test (new file)
**File:** `backend/tests/test_ntfy_deploy_receipt.py`

Pattern from `test_ntfy_integration.py`:
```python
@pytest.mark.integration
def test_deploy_receipt_round_trip(ntfy_secrets):
    """POST a simulated deploy notification; poll ntfy to verify receipt."""
    # POST to ntfy using same headers as notify_deploy.py
    # Poll GET /topic/json?poll=1&since=10s
    # Assert at least one message title contains "Deploy OK"
```
- Auto-skips if `ntfy_topic` not in secrets (`ntfy_secrets` fixture mirrors existing `ntfy_secrets` in test_ntfy_integration.py)
- Uses `urllib.request` (not httpx/requests) to match prod network path

---

## Agents

- backend: Workstream A1-A3 (BrokerIssueDaily model + migration + background task) + B3 + B4 (`backend_config.yaml` + `alert_utils.py`) + C1 (`scripts/monitor_ntfy_deploy.py`) + C2 (`webhook/deploy.sh` wire-in)
- broker: skip
- frontend: skip
- doc: skip
- backend-test: Tests for `_task_broker_issue_daily` aggregation (mock DB, verify UPSERT counts), CONNCHECK exit codes (mock DB returning threshold-crossing data), B1 (`test_paper_engine_startup.py` hang fix), C3 (unit tests in `test_deploy_notify.py`), C4 (`test_ntfy_deploy_receipt.py` integration test)
- playwright: skip

**Separate backend agent** (can run in parallel with above):
- Workstream A4 (TLM conncheck.py + check script + run_all.py) + B2 (dep bumps)
  Files: `tools/tlm/conncheck.py`, `scripts/check_broker_conn_issues.py`, `tools/tlm/run_all.py`, `requirements.txt`

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
feat(broker): daily issue counter + CONNCHECK TLM gate; ntfy deploy receipt monitor; fix order-email config + alert_utils drift + dep bumps

## Done when
- `broker_issue_daily` table exists and is populated nightly by background task
- `venv/bin/pytest backend/tests/ -q` completes in < 5 min (paper engine hang fixed)
- `python scripts/check_broker_conn_issues.py` exits 0 on clean data, 1 on threshold breach
- CONNCHECK appears in TLM status table
- `litellm>=1.83.14`, `pyasn1>=0.6.4` in requirements
- `order_failure.email: true`, `agent_alert.email: true` restored in backend_config.yaml
- `alert_utils.py` Callable import, dead branch, wrong comment fixed
- `scripts/monitor_ntfy_deploy.py` exits 0 when deploy notification received, 1 when not
- `test_ntfy_deploy_receipt.py` integration test passes (or skips if no ntfy secrets)
- All pytest tests green
