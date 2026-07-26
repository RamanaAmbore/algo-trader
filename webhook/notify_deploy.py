#!/usr/bin/env python3
"""
Deploy notification script — called directly from deploy scripts after service restart.
Uses only stdlib + requests to avoid opening app log files (permission conflict with
the running service process). Reads config and secrets directly from YAML.

Flags:
  --status ok|fail   (default: ok)
  --branch <name>    git branch (default: read from backend_config.yaml)
  --commit <hash>    short commit hash (default: unknown)
  --reason <text>    failure reason, shown when --status fail (optional)
"""
import argparse
import sys

import requests
import yaml

def main():
    parser = argparse.ArgumentParser(description="RamboQuant deploy notification")
    parser.add_argument("--status", default="ok", choices=["ok", "fail"],
                        help="Deploy outcome (default: ok)")
    parser.add_argument("--branch", default="",
                        help="Git branch name (default: read from backend_config.yaml)")
    parser.add_argument("--commit", default="unknown",
                        help="Short commit hash (default: unknown)")
    parser.add_argument("--reason", default="",
                        help="Failure reason string (used when --status fail)")
    parser.add_argument("--deploy-type", default="full",
                        choices=["full", "fe-only"],
                        help="full = backend service restarted; fe-only = "
                             "frontend rebuild only, broker sessions preserved")
    parser.add_argument("--layers", default="",
                        help="Human-readable layer summary, e.g. 'Backend API · Frontend'")
    args = parser.parse_args()

    try:
        with open("backend/config/backend_config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        with open("backend/config/secrets.yaml", "r", encoding="utf-8") as f:
            sec = yaml.safe_load(f)
    except Exception as e:
        print(f"notify_deploy: config load failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Branch: prefer the CLI arg; fall back to what's written in the config file.
    branch = args.branch or cfg.get("deploy_branch", "main")
    is_non_main = branch != "main"
    branch_tag = f" [{branch}]" if is_non_main else ""
    commit = args.commit
    status = args.status
    reason = args.reason

    # Skip entirely on dev — prod (main) always fires.
    if is_non_main:
        print("notify_deploy: skipped — dev branch deploys suppressed")
        sys.exit(0)

    errors = []

    # Suffix the success label with the deploy-type so the operator can
    # see at a glance whether the API service restarted (full) or stayed
    # up with broker sessions preserved (fe-only). Failure label has no
    # suffix — a failed deploy is always interesting regardless of type.
    deploy_type = args.deploy_type
    type_suffix = " · FE-only" if (status == "ok" and deploy_type == "fe-only") else ""

    layers = args.layers
    layers_line = f"\nLayers: {layers}" if layers else ""

    if status == "ok":
        event_label = f"Deploy OK{branch_tag}{type_suffix}"
        detail_line = f"{branch} → {commit}{layers_line}"
        tg_body = (f"<b>Deploy OK{branch_tag}{type_suffix}</b> · <code>{commit}</code>"
                   + (f"\nLayers: {layers}" if layers else ""))
    else:
        event_label = f"⚠ DEPLOY FAILED{branch_tag}"
        detail_line = f"{branch} → {commit}" + (f" — {reason}" if reason else "") + layers_line
        tg_body = (f"<b>⚠ DEPLOY FAILED{branch_tag}</b> · <code>{commit}</code>"
                   + (f"\n{reason}" if reason else "")
                   + (f"\nLayers: {layers}" if layers else ""))

    # Telegram — info/deploy channel
    tg_token   = sec.get("telegram_bot_token_deploy") or sec.get("telegram_bot_token", "")
    tg_chat_id = sec.get("telegram_chat_id_deploy")   or sec.get("telegram_chat_id", "")
    if tg_token and tg_chat_id:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat_id, "text": tg_body, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.ok:
                print("notify_deploy: telegram sent")
            else:
                errors.append(f"telegram: {resp.status_code} {resp.text[:120]}")
        except Exception as e:
            errors.append(f"telegram: {e}")

    # ntfy
    ntfy_topic = sec.get("ntfy_topic")
    if ntfy_topic:
        ntfy_url = sec.get("ntfy_url", "https://ntfy.sh")
        ntfy_token = sec.get("ntfy_token")
        try:
            import urllib.request as _urlreq
            _ntfy_headers = {"Title": event_label, "Tags": "rocket", "Priority": "default", "Content-Type": "text/plain"}
            if ntfy_token:
                _ntfy_headers["Authorization"] = f"Bearer {ntfy_token}"
            req = _urlreq.Request(
                f"{ntfy_url.rstrip('/')}/{ntfy_topic}",
                data=detail_line.encode(),
                headers=_ntfy_headers,
                method="POST",
            )
            _urlreq.urlopen(req, timeout=5)
            print("notify_deploy: ntfy sent")
        except Exception as e:
            errors.append(f"ntfy: {e}")

    # Email path retired. Deploy noise was cluttering the inbox; the
    # Telegram ping above carries the same information and lands
    # instantly on the ops channel. If a future operator wants email
    # back, restore the prior block from git history (it lived here)
    # plus add a `deploy_emails` list to secrets.yaml.

    # AppMessage DB record — best-effort. This script runs as a subprocess
    # with no asyncio event loop so fire() silently no-ops (its RuntimeError
    # guard swallows the missing loop). The callsite is retained so that
    # if dispatch is ever called from an async context this correctly routes.
    try:
        from backend.shared.helpers.app_message import AppMessage as _AppMsg, fire as _fire_msg
        _fire_msg(_AppMsg(
            level="info" if status == "ok" else "error",
            tags=["deploy"],
            title=event_label,
            body=detail_line,
            data={"branch": branch, "commit": commit, "deploy_type": deploy_type,
                  "status": status},
        ))
    except Exception:
        pass  # subprocess environment may lack the installed package

    if errors:
        print("notify_deploy: errors:", "; ".join(errors), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
