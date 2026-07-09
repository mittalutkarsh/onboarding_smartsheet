"""GitHub pull-request operations via the ``gh`` CLI.

Per the spec (§10, "Option A — easiest: use GitHub CLI") this module shells out
to ``gh`` rather than calling the REST API directly. ``gh`` is assumed to be
installed and authenticated (or ``GH_TOKEN``/``GITHUB_TOKEN`` is set in the
environment, which ``gh`` honors).

Idempotency check #2 lives here in :meth:`ensure_pr`:

  * We list PRs whose *head* branch matches the onboarding branch, across
    **all** states (open/closed/merged) via
    ``gh pr list --head <branch> --state all --json number,url,state``.
  * If a MERGED (or otherwise closed) PR exists, we DO NOT reopen or recreate
    it -- the row's work is already done/decided. We return that PR's state so
    the caller can record it and move on.
  * If an OPEN PR exists, we reuse its URL.
  * Only when no PR exists do we create one with ``gh pr create``, which prints
    the new PR URL on success.

Only real ``gh`` commands and documented JSON fields are used.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


class GitHubCliError(RuntimeError):
    """Raised when a ``gh`` command fails."""


@dataclass
class PullRequest:
    """A pull request as reported by ``gh``.

    Attributes:
        number: PR number.
        url: PR HTML URL.
        state: One of ``OPEN``, ``CLOSED``, ``MERGED`` (``gh`` uppercases).
        created: True if this run created the PR; False if it was pre-existing.
    """

    number: Optional[int]
    url: str
    state: str
    created: bool = False

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN"

    @property
    def is_merged(self) -> bool:
        return self.state.upper() == "MERGED"


class GitHubClient:
    """Wraps ``gh`` for one target repository (``org/repo``)."""

    def __init__(self, org: str, repo: str, gh_path: str = "gh") -> None:
        """Create a client bound to a single repo.

        Args:
            org: GitHub org/owner.
            repo: Repository name.
            gh_path: Path to the ``gh`` executable (override for tests).
        """
        self._repo_slug = f"{org}/{repo}"
        self._gh = gh_path

    def _run(self, args: List[str]) -> str:
        """Run a ``gh`` subcommand, returning stdout.

        Args:
            args: Arguments after the ``gh`` executable.

        Returns:
            Captured stdout (stripped).

        Raises:
            GitHubCliError: On non-zero exit or missing executable.
        """
        cmd = [self._gh, *args]
        logger.debug("Running: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise GitHubCliError(
                f"'{self._gh}' executable not found. Install GitHub CLI."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise GitHubCliError(
                f"gh {' '.join(args)} failed (exit {exc.returncode}): {stderr}"
            ) from exc
        return proc.stdout.strip()

    # --------------------------------------------------- existence check

    def find_pr_for_branch(self, head_branch: str) -> Optional[PullRequest]:
        """Return the existing PR for ``head_branch`` (any state), or None.

        Lists across ``--state all`` so a merged/closed PR is visible and the
        caller can avoid recreating or reopening it.

        Args:
            head_branch: The onboarding (head) branch name.

        Returns:
            The most relevant :class:`PullRequest`, or ``None`` if none exists.
        """
        out = self._run(
            [
                "pr",
                "list",
                "--repo",
                self._repo_slug,
                "--head",
                head_branch,
                "--state",
                "all",
                "--json",
                "number,url,state",
            ]
        )
        try:
            items = json.loads(out) if out else []
        except json.JSONDecodeError as exc:
            raise GitHubCliError(f"Could not parse gh pr list output: {exc}") from exc

        if not items:
            return None

        # Prefer an OPEN PR; otherwise take the first (most recent) reported.
        for item in items:
            if str(item.get("state", "")).upper() == "OPEN":
                return PullRequest(
                    number=item.get("number"),
                    url=item.get("url", ""),
                    state="OPEN",
                )
        first = items[0]
        return PullRequest(
            number=first.get("number"),
            url=first.get("url", ""),
            state=str(first.get("state", "")).upper(),
        )

    # ---------------------------------------------------------- create

    def ensure_pr(
        self,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequest:
        """Create the PR, or reuse/skip an existing one for this branch.

        This is the row-keyed idempotency guarantee: because ``head_branch`` is
        derived deterministically from the sheet row, a second run finds the
        same PR and never opens a duplicate or reopens a merged one.

        Args:
            head_branch: The pushed onboarding branch.
            base_branch: The base branch to merge into (usually ``main``).
            title: PR title.
            body: PR body.

        Returns:
            A :class:`PullRequest` describing the resulting PR.
        """
        existing = self.find_pr_for_branch(head_branch)
        if existing is not None:
            if existing.is_merged:
                logger.info(
                    "PR #%s for %s is already MERGED; leaving it untouched.",
                    existing.number,
                    head_branch,
                )
            else:
                logger.info(
                    "PR #%s for %s already exists (%s); reusing it.",
                    existing.number,
                    head_branch,
                    existing.state,
                )
            return existing

        logger.info("Creating PR for %s -> %s.", head_branch, base_branch)
        url = self._run(
            [
                "pr",
                "create",
                "--repo",
                self._repo_slug,
                "--base",
                base_branch,
                "--head",
                head_branch,
                "--title",
                title,
                "--body",
                body,
            ]
        )
        # gh prints the PR URL on success. Re-query to capture number/state.
        created = self.find_pr_for_branch(head_branch)
        if created is None:
            # Fall back to the printed URL if the follow-up list is empty.
            created = PullRequest(number=None, url=url, state="OPEN")
        created.created = True
        logger.info("Created PR: %s", created.url)
        return created
