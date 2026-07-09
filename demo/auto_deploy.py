"""Reliably trigger the 'Deploy dashboard' workflow on an interval.

Why this exists: GitHub's built-in ``schedule:`` cron is best-effort and often
delays or silently drops high-frequency runs (e.g. ``*/5``), so the hosted
dashboard can go stale. ``workflow_dispatch`` via the API, by contrast, fires
immediately and reliably. This script calls it on a precise interval from a
machine you control, so the Pages dashboard refreshes on time **while this is
running**.

Trade-off: it only fires while this process (and the machine) is up. For a
demo or working session that's fine; for always-on refresh independent of your
laptop, use an external scheduler (see README).

Auth + repo are auto-detected: the GitHub token comes from ``GITHUB_TOKEN`` or
the local git credential helper; the repo from ``origin``'s URL.

Run:
    python demo/auto_deploy.py                 # every 5 min, until Ctrl+C
    python demo/auto_deploy.py --interval 120  # every 2 min
    python demo/auto_deploy.py --once          # trigger one deploy and exit
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime

try:  # use the OS/corporate trust store behind an intercepting proxy
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001
    pass

import requests

_WORKFLOW = "deploy-dashboard.yml"


def _detect_repo() -> str:
    """Return 'owner/repo' from the origin remote URL."""
    url = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        raise SystemExit(f"Could not parse owner/repo from origin URL: {url!r}")
    return m.group(1)


def _detect_token() -> str:
    """Return a GitHub token from env or the git credential helper."""
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True,
    ).stdout
    m = re.search(r"^password=(.+)$", out, re.MULTILINE)
    if not m:
        raise SystemExit("No GitHub token in env or git credential helper.")
    return m.group(1)


def trigger(repo: str, token: str) -> bool:
    """Fire one workflow_dispatch. Returns True on success (HTTP 204)."""
    resp = requests.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/{_WORKFLOW}/dispatches",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"ref": "main"},
        timeout=30,
    )
    ok = resp.status_code == 204
    stamp = datetime.now().strftime("%H:%M:%S")
    if ok:
        print(f"[{stamp}] triggered Deploy dashboard (HTTP 204)")
    else:
        print(f"[{stamp}] FAILED: HTTP {resp.status_code} {resp.text[:200]}")
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reliably trigger the dashboard deploy.")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between triggers.")
    parser.add_argument("--once", action="store_true", help="Trigger once and exit.")
    args = parser.parse_args(argv)

    repo, token = _detect_repo(), _detect_token()
    print(f"repo: {repo}  |  interval: {args.interval}s  |  workflow: {_WORKFLOW}")

    if args.once:
        return 0 if trigger(repo, token) else 1

    print("Auto-deploy loop started. Ctrl+C to stop.")
    try:
        while True:
            trigger(repo, token)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
