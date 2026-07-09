"""Self-contained end-to-end demo of the onboarding automation.

Runs the REAL code (`main.py`, `feedback.py`, optionally `dashboard.py`) against
throwaway local stand-ins, so the whole loop can be shown with **no tokens, no
GitHub account, and no Smartsheet sheet**:

    * a local HTTP server that speaks just enough of the Smartsheet API
      (GET sheet / PUT rows), seeded with demo rows in memory;
    * a local *bare* git repo that stands in for the target GitHub repo
      (real clone / branch / commit / push happen against it);
    * a fake `gh` CLI (`demo/fake_gh.py`) that emulates PR create/list/view.

The demo then walks the story:

    1. Onboarding run  -> Ready rows become PR Created (branches pushed, PRs opened),
                          an invalid row is Blocked with a reason.
    2. A human "merges" one PR (we flip its state).
    3. Feedback run    -> that row becomes Merged; the still-open PR is refreshed.
    4. (optional) Launch the live dashboard against the same in-memory data.

This is demo scaffolding, not production code. Run:

    python demo/run_demo.py                 # run the flow, print sheet snapshots
    python demo/run_demo.py --dashboard     # ...then serve the live dashboard
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Make src importable and locate demo assets.
_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
_DEMO = _REPO / "demo"
sys.path.insert(0, str(_SRC))

# Tracker column schema (titles must match what the code expects).
_COLS = [
    "Team Name", "GitHub Org", "GitHub Repo", "App / Cookbook Name",
    "Owner Email", "Environments", "Onboarding Status", "Migration Type",
    "Branch Name", "PR URL", "Last Sync Time", "Validation Status",
    "Notes", "Error Message",
]
_TITLE_TO_ID = {t: i + 1 for i, t in enumerate(_COLS)}
_ID_TO_TITLE = {i + 1: t for i, t in enumerate(_COLS)}

# Seed rows: two valid (will open PRs) + one invalid (missing owner -> Blocked).
_ROWS = {
    1: {
        "Team Name": "Payments Platform", "GitHub Org": "demo-org",
        "GitHub Repo": "payments-service", "App / Cookbook Name": "payments-api",
        "Owner Email": "payments-lead@example.com", "Environments": "dev,test,prod",
        "Onboarding Status": "Ready", "Migration Type": "New onboarding",
    },
    2: {
        "Team Name": "Data Insights", "GitHub Org": "demo-org",
        "GitHub Repo": "insights-etl", "App / Cookbook Name": "insights-cookbook",
        "Owner Email": "data-owner@example.com", "Environments": "dev,prod",
        "Onboarding Status": "Ready", "Migration Type": "Existing repo migration",
    },
    3: {
        "Team Name": "Mobile Web", "GitHub Org": "demo-org",
        "GitHub Repo": "mweb-frontend", "App / Cookbook Name": "mweb-app",
        "Owner Email": "", "Environments": "dev,test",
        "Onboarding Status": "Ready", "Migration Type": "New onboarding",
    },
}


# --------------------------------------------------------------------------- #
# Local Smartsheet API stub
# --------------------------------------------------------------------------- #
def _sheet_payload():
    return {
        "id": 42,
        "name": "Git Onboarding Tracker (demo)",
        "columns": [{"id": _TITLE_TO_ID[t], "title": t} for t in _COLS],
        "rows": [
            {"id": rid, "cells": [{"columnId": _TITLE_TO_ID[t], "value": v}
                                  for t, v in data.items()]}
            for rid, data in _ROWS.items()
        ],
    }


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the demo output clean
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(_sheet_payload())

    def do_PUT(self):
        n = int(self.headers["Content-Length"])
        for row in json.loads(self.rfile.read(n)):
            for cell in row["cells"]:
                _ROWS[row["id"]][_ID_TO_TITLE[cell["columnId"]]] = cell.get("value")
        self._send({"message": "SUCCESS"})


def _start_stub():
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


# --------------------------------------------------------------------------- #
# Local git "GitHub" + fake gh
# --------------------------------------------------------------------------- #
def _init_bare_repo(work: Path) -> Path:
    origin = work / "github" / "payments-service.git"
    seed = work / "seed"
    origin.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@demo",
           "GIT_COMMITTER_NAME": "seed", "GIT_COMMITTER_EMAIL": "seed@demo"}

    def git(*args, cwd):
        subprocess.run(["git", "-C", str(cwd), *args], check=True,
                       capture_output=True, env=env)

    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)],
                   check=True, capture_output=True)
    (seed / "README.md").write_text("# demo target repo\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "initial", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "origin", "main", cwd=seed)
    return origin


def _write_fake_gh(work: Path) -> Path:
    bin_dir = work / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "gh"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_DEMO / "fake_gh.py"}" "$@"\n')
    shim.chmod(0o755)
    return bin_dir


# --------------------------------------------------------------------------- #
# Pretty printing
# --------------------------------------------------------------------------- #
def _banner(text):
    print("\n" + "=" * 74 + f"\n  {text}\n" + "=" * 74)


def _snapshot(title):
    print(f"\n-- {title} " + "-" * (70 - len(title)))
    hdr = f'{"Team":<18}{"Status":<12}{"Valid":<7}{"PR / Error"}'
    print(hdr)
    print("-" * 74)
    for rid, r in _ROWS.items():
        team = (r.get("Team Name") or "")[:17]
        status = r.get("Onboarding Status") or ""
        valid = r.get("Validation Status") or "-"
        detail = r.get("PR URL") or r.get("Error Message") or r.get("Notes") or ""
        print(f"{team:<18}{status:<12}{valid:<7}{detail}")


# --------------------------------------------------------------------------- #
# Demo flow
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="End-to-end onboarding demo.")
    parser.add_argument("--dashboard", action="store_true",
                        help="After the flow, serve the live dashboard (blocks).")
    parser.add_argument("--dashboard-port", type=int, default=8000)
    args = parser.parse_args(argv)

    # Output dir: a fresh temp dir by default, or DEMO_OUTPUT_DIR (used by CI so
    # the rendered files can be uploaded as an artifact).
    work = Path(os.environ.get("DEMO_OUTPUT_DIR") or tempfile.mkdtemp(prefix="onboarding-demo-"))
    work.mkdir(parents=True, exist_ok=True)
    _banner("SETTING UP LOCAL DEMO ENVIRONMENT (no tokens, no real GitHub)")
    print(f"  workspace: {work}")

    # 1. Smartsheet stub.
    stub, port = _start_stub()
    print(f"  smartsheet stub: http://127.0.0.1:{port}")

    # 2. Local bare git repo (stands in for GitHub).
    origin = _init_bare_repo(work)
    print(f"  target 'github' repo (bare): {origin}")

    # 3. Fake gh on PATH + gh state file.
    bin_dir = _write_fake_gh(work)
    gh_state = str(work / "gh_state.json")

    # 4. Wire env + monkeypatch module endpoints to point at the local stand-ins.
    import smartsheet_client
    import repo_modifier
    smartsheet_client._API_BASE = f"http://127.0.0.1:{port}/2.0"
    # Every target repo clones/pushes to our one local bare repo.
    repo_modifier.build_authenticated_url = lambda org, repo, token: str(origin)

    os.environ.update({
        "SMARTSHEET_TOKEN": "demo-token",
        "SMARTSHEET_SHEET_ID": "42",
        "GITHUB_TOKEN": "demo-token",
        "DEMO_GH_STATE": gh_state,
        "WORKSPACE_DIR": str(work / "checkouts"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LOG_LEVEL": os.environ.get("LOG_LEVEL", "INFO"),
    })

    import main as onboarding
    import feedback

    _snapshot("BEFORE: three Ready rows")

    _banner("STEP 1 - ONBOARDING RUN (main.py)")
    onboarding.run([])  # empty argv -> live path against the stubs
    _snapshot("AFTER onboarding: PRs opened; invalid row Blocked")

    # 5. Simulate a human merging the first opened PR.
    _banner("STEP 2 - A HUMAN MERGES ONE PR (simulated)")
    with open(gh_state) as f:
        state = json.load(f)
    merged_num = None
    if state["branches"]:
        head, pr = min(state["branches"].items(), key=lambda kv: kv[1]["number"])
        pr["state"] = "MERGED"
        merged_num = pr["number"]
        with open(gh_state, "w") as f:
            json.dump(state, f)
        print(f"  Marked PR #{merged_num} ({head}) as MERGED.")

    _banner("STEP 3 - FEEDBACK RUN (feedback.py)")
    feedback.run([])  # empty argv -> live path against the stubs
    _snapshot("AFTER feedback: merged PR -> Merged; open PR refreshed")

    # 6. Show the real git side effects.
    _banner("PROOF: real branches pushed to the local 'github' repo")
    branches = subprocess.run(["git", "-C", str(origin), "branch", "--list"],
                              capture_output=True, text=True).stdout.strip()
    print(branches or "  (none)")

    _banner("DEMO COMPLETE")
    print("  The onboarding + feedback loop ran end-to-end with no real")
    print("  credentials. Rendered files live under:", work / "checkouts")

    if args.dashboard:
        _banner(f"LIVE DASHBOARD -> http://127.0.0.1:{args.dashboard_port}")
        print("  Reading the same in-memory demo data. Ctrl+C to stop.\n")
        import dashboard
        config = dashboard.DashboardConfig(live=True, fixture="", refresh_seconds=5)
        dashboard.serve(config, "127.0.0.1", args.dashboard_port)
    else:
        print("\n  Tip: re-run with --dashboard to view the live dashboard.\n")
        stub.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
