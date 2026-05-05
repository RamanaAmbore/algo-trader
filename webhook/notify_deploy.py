#!/usr/bin/env python3
"""
Deploy notification script — called directly from deploy scripts after service restart.
Uses only stdlib + requests to avoid opening app log files (permission conflict with
the running service process). Reads config and secrets directly from YAML.
"""
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
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
    try:
        with open("backend/config/backend_config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        with open("backend/config/secrets.yaml", "r", encoding="utf-8") as f:
            sec = yaml.safe_load(f)
    except Exception as e:
        print(f"notify_deploy: config load failed: {e}", file=sys.stderr)
        sys.exit(1)

    branch = cfg.get("deploy_branch", "main")
    is_non_main = branch != "main"
    branch_tag = f" [{branch}]" if is_non_main else ""
    caps = cfg.get("cap_in_dev") or {}
    if not isinstance(caps, dict):
        caps = {}

    # Per-channel enable logic — prod (main) always on, dev gated by cap_in_dev.
    def _cap(name: str) -> bool:
        if not is_non_main:
            return True
        return bool(caps.get(name, False))

    # Skip entirely on dev when notify_on_deploy is off.
    if is_non_main and not _cap("notify_on_deploy"):
        print("notify_deploy: skipped — notify_on_deploy disabled for this environment")
        return

    ts = _timestamp()
    errors = []

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
            status = result.stdout.strip()
            services_status.append(f"{svc}: {status}")
        except Exception:
            services_status.append(f"{svc}: unknown")
    svc_text = " | ".join(services_status)

    # notify_on_deploy is the single gate for the deploy message — by the time
    # we reach here we've already confirmed it's on (or we're on prod). Telegram
    # and email both fire if their credentials are configured; cap_in_dev.telegram
    # / cap_in_dev.mail gate runtime alerts, not this deploy ping.

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
                      "text": f"<b>Deploy OK{branch_tag}</b>{branch_line}\n{ts}\n<code>{svc_text}</code>",
                      "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.ok:
                print("notify_deploy: Telegram sent")
            else:
                errors.append(f"Telegram failed {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            errors.append(f"Telegram error: {e}")

    # --- Email ---
    if True:
        smtp_server   = sec.get("smtp_server", "")
        smtp_port     = int(sec.get("smtp_port", 587))
        smtp_user     = sec.get("smtp_user", "")
        smtp_pass     = sec.get("smtp_pass", "")
        smtp_name     = sec.get("smtp_user_name", "")
        alert_emails  = sec.get("alert_emails", [])

        subject = f"RamboQuant Deploy OK{branch_tag}: {ts}"

        branch_banner = ""
        if is_non_main:
            branch_banner = (
                f"<div style='background-color:#fff3cd;border:1px solid #ffc107;"
                f"border-radius:4px;padding:8px 14px;margin-bottom:12px;"
                f"font-family:sans-serif;font-size:13px;color:#856404'>"
                f"&#9888; <strong>Non-production branch: {branch}</strong>"
                f"</div>"
            )

        html_body = (
            f"<html><body style='font-family:sans-serif'>"
            f"{branch_banner}"
            f"<table style='border-collapse:collapse;width:100%'>"
            f"<thead><tr>"
            f"<th style='background-color:#1a3a5c;color:#fff;padding:8px 12px;text-align:left;font-size:13px'>Event</th>"
            f"<th style='background-color:#1a3a5c;color:#fff;padding:8px 12px;text-align:left;font-size:13px'>Timestamp</th>"
            f"</tr></thead>"
            f"<tbody><tr>"
            f"<td style='padding:6px 12px;font-size:13px;border-bottom:1px solid #dce3ea'><b>Deploy OK{branch_tag}</b></td>"
            f"<td style='padding:6px 12px;font-size:13px;border-bottom:1px solid #dce3ea;font-family:monospace'>{ts}</td>"
            f"</tr><tr>"
            f"<td style='padding:6px 12px;font-size:12px;border-bottom:1px solid #dce3ea'>Services</td>"
            f"<td style='padding:6px 12px;font-size:12px;border-bottom:1px solid #dce3ea;font-family:monospace'>{svc_text}</td>"
            f"</tr></tbody>"
            f"</table>"
            f"</body></html>"
        )

        for email in alert_emails:
            try:
                msg = MIMEMultipart()
                msg["From"]    = formataddr((smtp_name, smtp_user))
                msg["To"]      = email
                msg["Cc"]      = msg["From"]
                msg["Subject"] = subject
                msg.attach(MIMEText(html_body, "html"))

                with smtplib.SMTP(smtp_server, smtp_port) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                    server.sendmail(smtp_user, email, msg.as_string())
                print(f"notify_deploy: email sent to {email}")
            except Exception as e:
                errors.append(f"Email failed for {email}: {e}")

    if errors:
        print("notify_deploy: errors:", "; ".join(errors), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
