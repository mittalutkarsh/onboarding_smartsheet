"""Feedback loop (spec §14 / Week-3): sync GitHub PR state back to Smartsheet.

For every tracker row with ``Onboarding Status == "PR Created"`` this reads the
current PR state from GitHub (via the ``gh`` CLI) and reconciles the row:

    * PR **merged**              -> Onboarding Status = ``Merged``
    * PR **closed** (not merged) -> Onboarding Status = ``Blocked`` + reason
    * PR open but **conflicting**-> Validation Status = ``Fail``,
                                    Error Message = ``Merge conflict``
                                    (status stays ``PR Created``)
    * PR open and mergeable      -> refresh Last Sync Time, clear any error

This closes the loop the onboarding run opens: onboarding writes ``PR Created``;
this job advances the row as humans review and merge the PR.

Per-row error handling mirrors ``main.py``: a failure on one row is logged and
recorded, then the loop continues to the next row.

Scope / safety:
    * Read-only against GitHub (``gh pr view``) -- never merges, closes, or edits
      a PR. Humans still own merge decisions.
    * Not scheduled by default (no cron). Run manually, or wire to a schedule
      later. ``--dry-run`` reads a local fixture and makes no network calls and
      no write-backs.

Beyond the original Week-1 scope; added on request.

Run:
    python src/feedback.py                 # live: SMARTSHEET_* + GITHUB_TOKEN
    python src/feedback.py --dry-run       # offline: list candidate rows only
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from github_client import GitHubClient, GitHubCliError, PRStatus
from smartsheet_client import SmartsheetClient, SmartsheetError

logger = logging.getLogger("feedback")

_DEFAULT_FIXTURE = "samples/sheet-fixture.json"
_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")


def _now_iso() -> str:
    """ISO-8601 UTC timestamp for the ``Last Sync Time`` cell."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_pr_number(pr_url: str) -> Optional[int]:
    """Extract the PR number from a GitHub PR URL.

    Args:
        pr_url: e.g. ``https://github.com/org/repo/pull/123``.

    Returns:
        The PR number, or ``None`` if the URL has no ``/pull/<n>`` segment.
    """
    if not pr_url:
        return None
    match = _PR_NUMBER_RE.search(str(pr_url))
    return int(match.group(1)) if match else None


def plan_update(status: PRStatus) -> Tuple[Dict[str, Any], str]:
    """Map a PR state to the Smartsheet cell updates and a log summary.

    Returns:
        (updates, summary) where ``updates`` is a title->value mapping and
        ``summary`` is a one-line description for logging.
    """
    now = _now_iso()
    if status.is_merged:
        return (
            {
                "Onboarding Status": "Merged",
                "Validation Status": "Pass",
                "Notes": "PR merged",
                "Error Message": "",
                "Last Sync Time": now,
            },
            "merged -> Merged",
        )
    if status.is_closed_unmerged:
        return (
            {
                "Onboarding Status": "Blocked",
                "Validation Status": "Fail",
                "Error Message": "PR closed without merging",
                "Last Sync Time": now,
            },
            "closed unmerged -> Blocked",
        )
    if status.has_conflict:
        # Still open; flag the conflict but leave Onboarding Status as PR Created.
        return (
            {
                "Validation Status": "Fail",
                "Error Message": "Merge conflict",
                "Notes": "PR has merge conflicts",
                "Last Sync Time": now,
            },
            "open, conflicting -> Fail (Merge conflict)",
        )
    # Open and mergeable (or mergeability unknown): healthy, just refresh.
    note = "PR open, mergeable" if status.mergeable == "MERGEABLE" else "PR open"
    return (
        {
            "Validation Status": "Pass",
            "Notes": note,
            "Error Message": "",
            "Last Sync Time": now,
        },
        f"open ({status.mergeable or 'UNKNOWN'}) -> refresh",
    )


def _pr_created_rows(client: SmartsheetClient) -> List[Dict[str, Any]]:
    """Return records whose Onboarding Status == ``PR Created``."""
    sheet = client.get_sheet()
    rows = [client.row_to_dict(r) for r in sheet.get("rows", [])]
    return [r for r in rows if r.get("Onboarding Status") == "PR Created"]


def process_row(record: Dict[str, Any], smartsheet: SmartsheetClient) -> None:
    """Reconcile one ``PR Created`` row against its GitHub PR.

    Raises on failure; the caller records it and continues.
    """
    row_id = record["row_id"]
    team = record.get("Team Name", "<unknown team>")
    org = record.get("GitHub Org")
    repo = record.get("GitHub Repo")
    pr_url = record.get("PR URL")
    pr_number = parse_pr_number(pr_url)

    if not (org and repo):
        raise ValueError("Row is PR Created but missing GitHub Org/Repo.")
    if pr_number is None:
        raise ValueError(f"Could not parse a PR number from PR URL: {pr_url!r}")

    logger.info("Row %s (%s): checking PR #%s in %s/%s.", row_id, team, pr_number, org, repo)
    github = GitHubClient(org, repo)
    status = github.get_pr_status(pr_number)

    updates, summary = plan_update(status)
    smartsheet.update_row(row_id, updates)
    logger.info("Row %s: %s", row_id, summary)


def _record_failure(smartsheet: SmartsheetClient, row_id: Any, message: str) -> None:
    """Best-effort error write-back that never raises.

    Records the error but leaves ``Onboarding Status`` untouched -- a transient
    GitHub/API hiccup should not push a still-open PR row to Blocked.
    """
    try:
        smartsheet.update_row(
            row_id,
            {
                "Validation Status": "Fail",
                "Error Message": message[:4000],
                "Last Sync Time": _now_iso(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        logger.error("Failed to write feedback error back to row %s: %s", row_id, exc)


def run(argv=None) -> int:
    """Reconcile all ``PR Created`` rows. Returns a process exit code."""
    args = _parse_args(argv)
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.dry_run:
        logger.info("DRY RUN: reading fixture, no GitHub calls, no write-back.")
        try:
            smartsheet = SmartsheetClient.from_fixture(args.fixture)
            rows = _pr_created_rows(smartsheet)
        except SmartsheetError as exc:
            logger.error("Could not load fixture: %s", exc)
            return 2
        if not rows:
            logger.info("No rows with Onboarding Status = PR Created.")
            return 0
        for record in rows:
            pr_number = parse_pr_number(record.get("PR URL"))
            logger.info(
                "[dry-run] Would check row %s (%s): PR #%s in %s/%s, then write "
                "back Merged / Blocked / Merge-conflict per its GitHub state.",
                record["row_id"],
                record.get("Team Name"),
                pr_number,
                record.get("GitHub Org"),
                record.get("GitHub Repo"),
            )
        return 0

    # --- Live mode. ---
    try:
        smartsheet_token = os.environ["SMARTSHEET_TOKEN"]
        sheet_id = os.environ["SMARTSHEET_SHEET_ID"]
        github_token = os.environ["GITHUB_TOKEN"]
    except KeyError as exc:
        logger.error("Missing required environment variable: %s", exc)
        return 2
    os.environ.setdefault("GH_TOKEN", github_token)

    smartsheet = SmartsheetClient(smartsheet_token, sheet_id)
    try:
        rows = _pr_created_rows(smartsheet)
    except SmartsheetError as exc:
        logger.error("Could not read Smartsheet: %s", exc)
        return 1

    if not rows:
        logger.info("No rows with Onboarding Status = PR Created. Nothing to do.")
        return 0

    failures = 0
    for record in rows:
        row_id = record.get("row_id")
        try:
            process_row(record, smartsheet)
        except (ValueError, SmartsheetError, GitHubCliError) as exc:
            failures += 1
            logger.error("Row %s feedback failed: %s", row_id, exc)
            _record_failure(smartsheet, row_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - keep the batch alive
            failures += 1
            logger.exception("Row %s feedback failed with unexpected error.", row_id)
            _record_failure(smartsheet, row_id, f"Unexpected error: {exc}")

    logger.info("Feedback complete: %d checked, %d failed.", len(rows), failures)
    return 1 if failures else 0


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="onboarding-feedback",
        description="Sync GitHub PR state back into the Smartsheet tracker "
        "(PR Created -> Merged / Blocked / Merge conflict).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read a local fixture and list candidate rows. No GitHub calls, "
        "no write-back.",
    )
    parser.add_argument(
        "--fixture",
        default=_DEFAULT_FIXTURE,
        help=f"Sheet JSON fixture for --dry-run (default: {_DEFAULT_FIXTURE}).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run())
