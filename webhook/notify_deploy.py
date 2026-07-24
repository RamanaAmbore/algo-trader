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

    if status == "ok":
        event_label = f"Deploy OK{branch_tag}{type_suffix}"
        detail_line = f"{branch} → {commit}"
    else:
        event_label = f"⚠ DEPLOY FAILED{branch_tag}"
        detail_line = f"{branch} → {commit}" + (f" — {reason}" if reason else "")

    # notify_on_deploy is the single gate for the deploy message — by the time
    # we reach here we've already confirmed it's on (or we're on prod). Deploy
    # pings ship ntfy-only; the prior Telegram path was retired and folded into
    # _send_telegram_info() inside alert_utils so all Telegram routing goes
    # through the config-driven _alert_route() table.

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

    if errors:
        print("notify_deploy: errors:", "; ".join(errors), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
