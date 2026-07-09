"""Week-1 MVP orchestrator: Smartsheet -> Git -> PR -> Smartsheet write-back.

Flow (per the spec's 4-part design), executed for every row whose
``Onboarding Status`` == ``Ready``:

    1. Validate required fields.
    2. Clone (or reuse) the target repo.
    3. Create (or reuse) the onboarding branch.
    4. Render onboarding files from templates.
    5. Commit and push.
    6. Open (or reuse/skip) the PR via ``gh``.
    7. Write PR URL / status / timestamp back to the same Smartsheet row.

Error handling: each row runs inside a try/except. On any failure we write a
clear ``Error Message`` + ``Validation Status = Fail`` back to that row and
continue to the next -- one bad row never aborts the batch.

Idempotency: branch/PR reuse (see repo_modifier / github_client) plus the merged
-PR guard mean a second run reuses existing work and never reopens a merged PR.

Config comes only from the environment (no hardcoded secrets, sheet IDs, or
org/repo names):

    SMARTSHEET_TOKEN      - Smartsheet API token
    SMARTSHEET_SHEET_ID   - tracker sheet ID          (placeholder: [SHEET_ID])
    GITHUB_TOKEN          - GitHub token for git push + gh
    GIT_AUTHOR_NAME       - (optional) commit author name
    GIT_AUTHOR_EMAIL      - (optional) commit author email
    WORKSPACE_DIR         - (optional) where repos are checked out

Org/repo/app come from each Smartsheet row (placeholders in docs:
[GITHUB_ORG] / [TARGET_REPO]).
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

from github_client import GitHubClient, GitHubCliError, PullRequest
from repo_modifier import RepoModifier, RepoModifierError
from smartsheet_client import SmartsheetClient, SmartsheetError
from templates import TemplateRenderer
from validators import validate_record

logger = logging.getLogger("onboarding")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    """Return an ISO-8601 UTC timestamp for the ``Last Sync Time`` cell."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slugify(value: str) -> str:
    """Lowercase, hyphenate a string for safe use in a branch name."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower())
    return slug.strip("-") or "app"


def _branch_name(record: Dict[str, Any]) -> str:
    """Derive the onboarding branch name for a row.

    Prefers the explicit ``Branch Name`` cell; otherwise derives
    ``onboarding/<app-slug>``. Deterministic per row, which is what makes the
    branch/PR reuse idempotent across runs.
    """
    explicit = record.get("Branch Name")
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    return f"onboarding/{_slugify(str(record.get('App / Cookbook Name', 'app')))}"


def _build_context(record: Dict[str, Any], environments) -> Dict[str, Any]:
    """Assemble the Jinja render context from a validated record."""
    return {
        "team_name": record["Team Name"],
        "app_name": record["App / Cookbook Name"],
        "github_org": record["GitHub Org"],
        "github_repo": record["GitHub Repo"],
        "owner_email": record["Owner Email"],
        "environments": environments,
    }


# --------------------------------------------------------------------------- #
# Per-row processing
# --------------------------------------------------------------------------- #
def process_row(
    record: Dict[str, Any],
    smartsheet: SmartsheetClient,
    renderer: TemplateRenderer,
    workspace: Path,
    github_token: str,
    author_name: str,
    author_email: str,
) -> None:
    """Run the full onboarding pipeline for one row.

    Raises on failure; the caller (:func:`run`) catches and records it. Keeping
    the happy path here and the error write-back in the caller means every exit
    -- success or failure -- results in exactly one Smartsheet write.
    """
    row_id = record["row_id"]
    team = record.get("Team Name", "<unknown team>")
    logger.info("Processing row %s (%s).", row_id, team)

    # (Flagged) 'Migration Type' is read but not branched on in Week-1; new
    # onboarding and existing-repo migration follow the same path for now.
    _migration_type = record.get("Migration Type")

    # 1. Validate.
    result = validate_record(record)
    if not result.ok:
        # Validation failures are ordinary outcomes -> raise so the caller
        # records Validation Status = Fail with the specific reasons.
        raise ValueError(result.summary)

    org = record["GitHub Org"]
    repo = record["GitHub Repo"]
    branch = _branch_name(record)
    context = _build_context(record, result.environments)

    # 2. Clone (or reuse) + 3. branch (idempotent).
    checkout_path = workspace / f"{_slugify(org)}__{_slugify(repo)}"
    modifier = RepoModifier.clone(org, repo, github_token, checkout_path)
    branch_existed = modifier.ensure_branch(branch)

    # 4. Render onboarding files.
    renderer.render_all(modifier.working_dir, context)

    # 5. Commit + push (no-op commit is fine on a clean re-run).
    commit_msg = (
        f"Onboard {context['app_name']} (Smartsheet row {row_id})\n\n"
        f"Team: {context['team_name']}\n"
        f"Environments: {', '.join(context['environments'])}\n"
        f"Generated by onboarding-automation."
    )
    modifier.commit_all(commit_msg, author_name, author_email)
    modifier.push(branch, github_token)

    # 6. Open / reuse / skip PR.
    github = GitHubClient(org, repo)
    pr_title = f"Onboard {context['app_name']}"
    pr_body = (
        "Automated onboarding PR generated from the Smartsheet Git Onboarding "
        f"Tracker (row {row_id}).\n\n"
        f"- Team: {context['team_name']}\n"
        f"- App / Cookbook: {context['app_name']}\n"
        f"- Owner: {context['owner_email']}\n"
        f"- Environments: {', '.join(context['environments'])}\n\n"
        "A human reviewer must approve and merge. This automation does not "
        "auto-merge."
    )
    pr: PullRequest = github.ensure_pr(
        head_branch=branch,
        base_branch=modifier.default_branch,
        title=pr_title,
        body=pr_body,
    )

    # 7. Write back. Map GitHub PR state -> tracker Onboarding Status.
    if pr.is_merged:
        onboarding_status = "Merged"
    else:
        onboarding_status = "PR Created"

    reused_note = []
    if branch_existed:
        reused_note.append("reused existing branch")
    if not pr.created:
        reused_note.append(f"reused existing PR (state {pr.state})")
    note = "; ".join(reused_note) if reused_note else "Created branch and PR."

    smartsheet.update_row(
        row_id,
        {
            "Onboarding Status": onboarding_status,
            "PR URL": pr.url,
            "Last Sync Time": _now_iso(),
            "Validation Status": "Pass",
            "Notes": note,
            "Error Message": "",  # clear any prior error on success
        },
    )
    logger.info("Row %s done: status=%s pr=%s", row_id, onboarding_status, pr.url)


def _record_failure(
    smartsheet: SmartsheetClient, row_id: Any, message: str
) -> None:
    """Best-effort write-back of a failure to the row.

    Never raises: if even the error write-back fails we log and move on, so the
    batch loop is not derailed by a Smartsheet hiccup.
    """
    try:
        smartsheet.update_row(
            row_id,
            {
                "Onboarding Status": "Blocked",
                "Validation Status": "Fail",
                "Error Message": message[:4000],  # keep the cell reasonable
                "Last Sync Time": _now_iso(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        logger.error("Failed to write error back to row %s: %s", row_id, exc)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run() -> int:
    """Fetch Ready rows and process each. Returns a process exit code."""
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # --- Required config (fail fast, but never print secret values). ---
    try:
        smartsheet_token = os.environ["SMARTSHEET_TOKEN"]
        sheet_id = os.environ["SMARTSHEET_SHEET_ID"]
        github_token = os.environ["GITHUB_TOKEN"]
    except KeyError as exc:
        logger.error("Missing required environment variable: %s", exc)
        return 2

    author_name = os.environ.get("GIT_AUTHOR_NAME", "onboarding-automation")
    author_email = os.environ.get(
        "GIT_AUTHOR_EMAIL", "onboarding-automation@users.noreply.github.com"
    )
    workspace = Path(
        os.environ.get("WORKSPACE_DIR")
        or tempfile.mkdtemp(prefix="onboarding-repos-")
    )
    workspace.mkdir(parents=True, exist_ok=True)
    logger.info("Using workspace: %s", workspace)

    # gh authenticates from GH_TOKEN/GITHUB_TOKEN; make sure it is present.
    os.environ.setdefault("GH_TOKEN", github_token)

    smartsheet = SmartsheetClient(smartsheet_token, sheet_id)
    renderer = TemplateRenderer()

    try:
        rows = smartsheet.get_ready_rows()
    except SmartsheetError as exc:
        logger.error("Could not read Smartsheet: %s", exc)
        return 1

    if not rows:
        logger.info("No rows with Onboarding Status = Ready. Nothing to do.")
        return 0

    failures = 0
    for record in rows:
        row_id = record.get("row_id")
        try:
            process_row(
                record,
                smartsheet,
                renderer,
                workspace,
                github_token,
                author_name,
                author_email,
            )
        except (
            ValueError,
            SmartsheetError,
            RepoModifierError,
            GitHubCliError,
        ) as exc:
            failures += 1
            logger.error("Row %s failed: %s", row_id, exc)
            _record_failure(smartsheet, row_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - keep the batch alive
            failures += 1
            logger.exception("Row %s failed with unexpected error.", row_id)
            _record_failure(smartsheet, row_id, f"Unexpected error: {exc}")

    logger.info(
        "Batch complete: %d processed, %d failed.", len(rows), failures
    )
    # Non-zero exit if any row failed, so CI surfaces it -- but only after every
    # row has been attempted and its status written back.
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
