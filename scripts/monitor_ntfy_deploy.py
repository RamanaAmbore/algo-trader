#!/usr/bin/env python3
"""
monitor_ntfy_deploy.py — Verify that a deploy notification was received by ntfy.

Polls the ntfy API (GET /topic/json?poll=1&since=Xs) and checks that at least
one notification matching --title-contains was published within the window.

Exit codes:
  0 — receipt confirmed
  1 — no matching message in window
  2 — config/network error

Usage:
  python scripts/monitor_ntfy_deploy.py [--title-contains "Deploy"] [--since 90] [--timeout 30]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_secrets() -> dict:
    import yaml
    p = ROOT / "backend" / "config" / "secrets.yaml"
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify ntfy deploy receipt")
    parser.add_argument("--title-contains", default="Deploy",
                        help="String that must appear in message title (default: Deploy)")
    parser.add_argument("--since", type=int, default=90,
                        help="Look-back window in seconds (default: 90)")
    parser.add_argument("--timeout", type=int, default=30,
                        help="HTTP request timeout in seconds (default: 30)")
    args = parser.parse_args()

    try:
        sec = _load_secrets()
    except Exception as e:
        print(f"monitor_ntfy: secrets load failed: {e}", file=sys.stderr)
        sys.exit(2)

    ntfy_topic = sec.get("ntfy_topic")
    if not ntfy_topic:
        print("monitor_ntfy: ntfy_topic not configured — skipping receipt check")
        sys.exit(0)

    ntfy_url = sec.get("ntfy_url", "https://ntfy.sh").rstrip("/")
    ntfy_token = sec.get("ntfy_token")

    url = f"{ntfy_url}/{ntfy_topic}/json?poll=1&since={args.since}s"
    headers = {"Accept": "application/x-ndjson"}
    if ntfy_token:
        headers["Authorization"] = f"Bearer {ntfy_token}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            body = resp.read().decode("utf-8")
    except Exception as e:
        print(f"monitor_ntfy: poll failed: {e}", file=sys.stderr)
        sys.exit(2)

    needle = args.title_contains.lower()
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        title = (msg.get("title") or "").lower()
        if needle in title:
            print(f"monitor_ntfy: receipt confirmed — '{msg.get('title')}' at {msg.get('time')}")
            sys.exit(0)

    print(f"monitor_ntfy: no '{args.title_contains}' message found in last {args.since}s",
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
