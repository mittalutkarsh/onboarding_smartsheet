"""Scan a GitHub owner's repositories, derive REAL onboarding status from each,
and wire the results through Smartsheet (upsert one row per repo).

This flips the tracker from hand-entered intent to ground truth: instead of a
human typing a row's status, the scanner inspects each repo and records what is
actually there. The dashboard keeps reading Smartsheet, so it now reflects
reality across all repos.

Status derivation (per repo, on its default branch):
    * Onboarded      -- onboarding/manifest.yml present AND a deploy workflow
                        (.github/workflows/deploy-*.yml) present
    * Drifted        -- onboarding/manifest.yml present but no deploy workflow
    * In progress    -- an open PR whose head branch starts with "onboarding/"
    * Not onboarded  -- none of the above

Mapping onto the tracker's Onboarding Status vocabulary (so the existing
dashboard works unchanged):
    Not onboarded -> New   |   In progress -> PR Created
    Onboarded     -> Merged|   Drifted     -> Blocked
The human-readable scan result is also written to the Notes column.

GitHub is read via the REST API (requests), so this works without the `gh` CLI
and behind a corporate TLS-intercepting proxy (via truststore).

Run:
    python src/repo_scanner.py --owner mittalutkarsh --dry-run   # scan + print
    python src/repo_scanner.py --owner mittalutkarsh             # scan + upsert
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:  # OS/corporate trust store for TLS behind an intercepting proxy
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001
    pass

import requests
from dotenv import load_dotenv

from smartsheet_client import SmartsheetClient, SmartsheetError

logger = logging.getLogger("scanner")

_GH_API = "https://api.github.com"

# Repo-scan status -> tracker Onboarding Status.
_STATUS_MAP = {
    "Onboarded": "Merged",
    "Drifted": "Blocked",
    "In progress": "PR Created",
    "Not onboarded": "New",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _detect_token() -> Optional[str]:
    """GitHub token from env or the git credential helper (optional for public)."""
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    try:
        out = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=10,
        ).stdout
        m = re.search(r"^password=(.+)$", out, re.MULTILINE)
        return m.group(1) if m else None
    except Exception:  # noqa: BLE001
        return None


class GitHubScanner:
    """Reads repos + onboarding signals from the GitHub REST API."""

    def __init__(self, token: Optional[str] = None, timeout: int = 30) -> None:
        self._timeout = timeout
        self._session = requests.Session()
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "onboarding-repo-scanner",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._session.headers.update(headers)

    def list_repos(self, owner: str) -> List[Dict[str, Any]]:
        """List an owner's repositories (paginated)."""
        repos: List[Dict[str, Any]] = []
        page = 1
        while True:
            resp = self._session.get(
                f"{_GH_API}/users/{owner}/repos",
                params={"per_page": 100, "page": page, "sort": "full_name"},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                raise SmartsheetError(
                    f"GitHub list repos failed ({resp.status_code}): {resp.text[:200]}"
                )
            batch = resp.json()
            if not batch:
                break
            repos.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return repos

    def _path_exists(self, owner: str, repo: str, path: str, ref: str) -> bool:
        r = self._session.get(
            f"{_GH_API}/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref}, timeout=self._timeout,
        )
        return r.status_code == 200

    def _has_deploy_workflow(self, owner: str, repo: str, ref: str) -> bool:
        r = self._session.get(
            f"{_GH_API}/repos/{owner}/{repo}/contents/.github/workflows",
            params={"ref": ref}, timeout=self._timeout,
        )
        if r.status_code != 200:
            return False
        items = r.json()
        if not isinstance(items, list):
            return False
        return any(
            it.get("type") == "file" and it.get("name", "").startswith("deploy-")
            for it in items
        )

    def _open_onboarding_pr(self, owner: str, repo: str) -> Optional[str]:
        r = self._session.get(
            f"{_GH_API}/repos/{owner}/{repo}/pulls",
            params={"state": "open", "per_page": 100}, timeout=self._timeout,
        )
        if r.status_code != 200:
            return None
        for pr in r.json():
            if str(pr.get("head", {}).get("ref", "")).startswith("onboarding/"):
                return pr.get("html_url")
        return None

    def derive_status(self, repo: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        """Return (scan_status, pr_url) for one repo."""
        owner = repo["owner"]["login"]
        name = repo["name"]
        branch = repo.get("default_branch") or "main"

        if self._path_exists(owner, name, "onboarding/manifest.yml", branch):
            if self._has_deploy_workflow(owner, name, branch):
                return "Onboarded", None
            return "Drifted", None
        pr_url = self._open_onboarding_pr(owner, name)
        if pr_url:
            return "In progress", pr_url
        return "Not onboarded", None


def scan(owner: str, token: Optional[str]) -> List[Dict[str, Any]]:
    """Scan all of ``owner``'s repos; return a record per repo."""
    scanner = GitHubScanner(token)
    repos = scanner.list_repos(owner)
    logger.info("Found %d repo(s) under %s.", len(repos), owner)
    records: List[Dict[str, Any]] = []
    for repo in repos:
        scan_status, pr_url = scanner.derive_status(repo)
        records.append(
            {
                "owner": repo["owner"]["login"],
                "repo": repo["name"],
                "scan_status": scan_status,
                "tracker_status": _STATUS_MAP[scan_status],
                "pr_url": pr_url,
            }
        )
        logger.info("  %-40s %s", repo["name"], scan_status)
    return records


def sync_to_smartsheet(
    records: List[Dict[str, Any]], smartsheet: SmartsheetClient
) -> Tuple[int, int]:
    """Upsert scan records into the tracker. Returns (updated, added) counts."""
    sheet = smartsheet.get_sheet()
    index: Dict[Tuple[str, str], int] = {}
    for row in sheet.get("rows", []):
        rec = smartsheet.row_to_dict(row)
        org = str(rec.get("GitHub Org") or "").lower()
        repo = str(rec.get("GitHub Repo") or "").lower()
        if org and repo:
            index[(org, repo)] = rec["row_id"]

    updated = added = 0
    new_rows: List[Dict[str, Any]] = []
    for r in records:
        cells = {
            "Team Name": r["owner"],
            "GitHub Org": r["owner"],
            "GitHub Repo": r["repo"],
            "App / Cookbook Name": r["repo"],
            "Onboarding Status": r["tracker_status"],
            "PR URL": r["pr_url"] or "",
            "Validation Status": "Pass",
            "Notes": f"repo scan: {r['scan_status']}",
            "Last Sync Time": _now_iso(),
        }
        key = (r["owner"].lower(), r["repo"].lower())
        if key in index:
            smartsheet.update_row(index[key], cells)
            updated += 1
        else:
            new_rows.append(cells)
    if new_rows:
        smartsheet.add_rows(new_rows)
        added = len(new_rows)
    return updated, added


def run(argv=None) -> int:
    args = _parse_args(argv)
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    owner = args.owner or os.environ.get("GITHUB_OWNER")
    if not owner:
        logger.error("Provide --owner or set GITHUB_OWNER.")
        return 2

    token = _detect_token()
    try:
        records = scan(owner, token)
    except SmartsheetError as exc:
        logger.error("Scan failed: %s", exc)
        return 1

    # Summary by status.
    counts: Dict[str, int] = {}
    for r in records:
        counts[r["scan_status"]] = counts.get(r["scan_status"], 0) + 1
    logger.info("Scan summary: %s", counts)

    if args.dry_run:
        logger.info("[dry-run] Not writing to Smartsheet. Would upsert %d repo(s).",
                    len(records))
        return 0

    try:
        token_ss = os.environ["SMARTSHEET_TOKEN"]
        sheet_id = os.environ["SMARTSHEET_SHEET_ID"]
    except KeyError as exc:
        logger.error("Missing %s (needed to write results).", exc)
        return 2
    smartsheet = SmartsheetClient(token_ss, sheet_id)
    updated, added = sync_to_smartsheet(records, smartsheet)
    logger.info("Wired through Smartsheet: %d updated, %d added.", updated, added)
    return 0


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="repo-scanner",
        description="Scan a GitHub owner's repos for onboarding status and "
        "wire the results into the Smartsheet tracker.",
    )
    parser.add_argument("--owner", help="GitHub user/org to scan (or GITHUB_OWNER).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and print only; do not write to Smartsheet.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run())
