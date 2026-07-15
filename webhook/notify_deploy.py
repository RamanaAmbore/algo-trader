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
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yaml

INDIAN_TZ = ZoneInfo("Asia/Kolkata")
EST_TZ    = ZoneInfo("US/Eastern")


def _timestamp():
    now_ist = datetime.now(tz=INDIAN_TZ)
    now_est = datetime.now(tz=EST_TZ)
    return (f"{now_ist.strftime('%a, %B %d, %Y, %I:%M %p IST')} | "
            f"{now_est.strftime('%a, %B %d, %Y, %I:%M %p %Z')}")


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

    caps = cfg.get("cap_in_dev") or {}
    if not isinstance(caps, dict):
        caps = {}

    # Per-channel enable logic — prod (main) always on, dev gated by cap_in_dev.
    def _cap(name: str) -> bool:
        if not is_non_main:
            return True
        return bool(caps.get(name, False))

    # Skip entirely on dev when notify_on_deploy is off — but always fire on
    # failure so operators know the deploy broke even on a gated dev branch.
    if is_non_main:
        print("notify_deploy: skipped — dev branch deploys suppressed")
        sys.exit(0)

    ts = _timestamp()
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
        tg_header   = f"<b>Deploy OK{branch_tag}{type_suffix}</b> · <code>{commit}</code>"
    else:
        event_label = f"⚠ DEPLOY FAILED{branch_tag}"
        detail_line = f"{branch} → {commit}" + (f" — {reason}" if reason else "")
        tg_header   = (f"<b>⚠ DEPLOY FAILED{branch_tag}</b> · <code>{commit}</code>"
                       + (f"\n{reason}" if reason else ""))

    # Services that were restarted by this deploy (per-env)
    import subprocess
    env_service = "ramboq_api.service" if branch == "main" else "ramboq_dev_api.service"
    # Always include the shared webhook listener
    all_services = [env_service, "ramboq_hook.service"]

    services_status = []
    for svc in all_services:
        try:
            result = subprocess.run(["systemctl", "is-active", svc],
                                    capture_output=True, text=True, timeout=5)
            svc_status = result.stdout.strip()
            services_status.append(f"{svc}: {svc_status}")
        except Exception:
            services_status.append(f"{svc}: unknown")
    svc_text = " | ".join(services_status)

    # notify_on_deploy is the single gate for the deploy message — by the time
    # we reach here we've already confirmed it's on (or we're on prod). Deploy
    # pings ship Telegram-only by operator preference (May 2026); the prior
    # email path was retired because deploy noise was cluttering the inbox
    # while the same information already lands instantly on the ops channel.

    # --- Telegram ---
    # Prefer dedicated deploy-bot keys so deploy pings can go to a
    # separate bot/channel; fall back to the shared alert keys so
    # existing deployments require no secrets.yaml change.
    token   = sec.get("telegram_bot_token_deploy") or sec.get("telegram_bot_token", "")
    chat_id = sec.get("telegram_chat_id_deploy")   or sec.get("telegram_chat_id", "")
    if token and chat_id:
        branch_line = f"\n⚠ <b>Branch: {branch}</b>" if is_non_main else ""
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id,
                      "text": f"{tg_header}{branch_line}\n{ts}\n<code>{svc_text}</code>",
                      "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.ok:
                print("notify_deploy: Telegram sent")
                # ntfy — prod only, default priority (informational deploy ping)
                ntfy_topic = sec.get("ntfy_topic")
                if ntfy_topic:
                    ntfy_url = sec.get("ntfy_url", "https://ntfy.sh")
                    try:
                        import urllib.request as _urlreq
                        req = _urlreq.Request(
                            f"{ntfy_url.rstrip('/')}/{ntfy_topic}",
                            data=detail_line.encode(),
                            headers={"Title": event_label, "Tags": "rocket", "Content-Type": "text/plain"},
                            method="POST",
                        )
                        _urlreq.urlopen(req, timeout=5)
                        print("notify_deploy: ntfy sent")
                    except Exception as e:
                        errors.append(f"ntfy: {e}")
            else:
                errors.append(f"Telegram failed {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            errors.append(f"Telegram error: {e}")

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
