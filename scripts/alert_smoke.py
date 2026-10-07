"""Smoke check for the alert agents. Run from the repo root with the API venv.

For every seeded event agent: the spec must validate, and its renderer must produce a
non-empty title and body from a representative record. Nothing is sent and nothing is
written: channels are not called and no database is touched.

    venv/bin/python scripts/alert_smoke.py
"""
import sys
from datetime import datetime, timezone

from backend.api.algo import event_agents as ea
from backend.api.algo.grammar import LOG_TAG_TOKENS

_IST_LABEL = "Tue, Oct 06 2026, 14:30 IST"
_ORDER = {"masked": "ZG####", "symbol": "NIFTY26OCTFUT", "exchange": "NFO", "side": "BUY", "qty": 75,
          "mode": "live", "source": "ticket", "error": "Insufficient funds", "suppressed_count": 0,
          "ist_disp": "10:15:30 IST"}

SAMPLES = {
    "fill": {"order_id": 101, "account": "ZG0790", "symbol": "NIFTY26OCTFUT", "exchange": "NFO",
             "transaction_type": "BUY", "quantity": 75, "fill_price": 112.5, "product": "NRML"},
    "error": {},
    "chase_cancel": {"transaction_type": "BUY", "symbol": "NIFTY", "account": "ZG0790", "order_id": "O1",
                     "attempt": 2, "quantity": 100, "remaining_qty": 60},
    "partial_gtt": {"parent_row_id": 1, "parent_symbol": "NIFTY", "planned": 3, "placed": 1, "errors": ["x"]},
    "template_attach": {"alert_event": "wing_skip", "reason": "no candidate", "parent_order_id": 9,
                        "symbol": "NIFTY", "exchange": "NFO"},
    "order_failure": dict(_ORDER),
    "template_guard": {"template_slug": "t", "applies_to": "buy_option", "reason": "r", "parent_order_id": 1,
                       "parent_side": "BUY", "parent_qty": 1, "parent_symbol": "NIFTY", "parent_fill_price": 1.0,
                       "parent_account": "ZG0790", "ist_label": _IST_LABEL},
    "template_attach_fail": {"order_id": 1, "symbol": "NIFTY", "account": "ZG0790", "err_summary": "e",
                             "ist_label": _IST_LABEL},
    "mcp_ping": {"tg": "<b>MCP</b> ping"},
    "deploy_sync": {"title": "Deploy out of sync — main", "body": "HEAD mismatch"},
    "rich_alert": {"ist_display": "10:15:30 IST", "tg_table": "row", "email_table_html": "<table/>",
                   "subject_detail": "ZG0790", "sim_mode": False, "mode_tag": ""},
    "summary": {"msg_type": "open", "ist_display": "10:15:30 IST", "tg_table": "row",
                "email_table_html": "<table/>", "subject_detail": "Summary"},
    "breach": {"agent_name": "Loss", "ntfy_body": "n", "telegram_body": "t",
               "email_subject": "s", "email_body": "b", "channels": []},
}


def _record(extra: dict) -> dict:
    return {"ts": datetime.now(timezone.utc), "level": "INFO", "logger": "smoke", "message": "smoke",
            "tags": ["smoke"], "extra": extra}


def main() -> int:
    known_tags = {t["token"] for t in LOG_TAG_TOKENS}
    failures = 0
    for spec in ea.SEEDED_AGENTS:
        problems = ea.validate_seed_spec(spec)
        render_key = next((a["render"] for a in spec["actions"] if a.get("type") == "render"), None)
        sample = SAMPLES.get(render_key)
        if sample is None:
            problems.append(f"no smoke sample for renderer '{render_key}'")
        else:
            try:
                out = ea.RENDERS[render_key](_record(dict(sample)))
                if not (out[0] and out[1]):
                    problems.append("renderer returned an empty title or body")
            except Exception as e:  # noqa: BLE001
                problems.append(f"renderer raised {type(e).__name__}: {e}")
        status = "OK" if not problems else "FAIL"
        print(f"{status:4} {spec['slug']}" + ("" if not problems else f"  ({'; '.join(problems)})"))
        failures += bool(problems)
    print(f"{len(ea.SEEDED_AGENTS) - failures}/{len(ea.SEEDED_AGENTS)} seeded agents pass"
          f" (tag catalog has {len(known_tags)} tags)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
