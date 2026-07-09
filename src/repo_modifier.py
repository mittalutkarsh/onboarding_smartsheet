"""Git operations for the target repository, via GitPython.

Responsibilities:
  * Clone the target repo (or reuse an existing local checkout).
  * Create **or reuse** the onboarding branch (idempotency check #1).
  * Stage all rendered files, commit, and push.

Safety guarantees (per task boundaries):
  * Never force-pushes.
  * Never deletes branches or history.
  * If the branch already exists remotely, it is reused rather than recreated,
    so a second run does not duplicate work or clobber review history.

Auth: the remote URL embeds a token for push. The token is read from the
environment by the caller and passed in; it is **never logged**. We build the
URL only for the ``origin`` remote and rely on the token's own scope.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import git

logger = logging.getLogger(__name__)


class RepoModifierError(RuntimeError):
    """Raised when a git operation fails."""


def build_authenticated_url(github_org: str, github_repo: str, token: str) -> str:
    """Build an HTTPS clone/push URL with the token embedded.

    Uses the ``x-access-token`` username form, which works for both classic
    PATs and GitHub App / Actions tokens. The token is URL-encoded so special
    characters do not corrupt the URL.

    Args:
        github_org: Owner/org, e.g. ``example-org``.
        github_repo: Repository name, e.g. ``sample-repo``.
        token: GitHub token (from ``GITHUB_TOKEN``).

    Returns:
        A ``https://x-access-token:<token>@github.com/org/repo.git`` URL.
    """
    return (
        f"https://x-access-token:{quote(token, safe='')}"
        f"@github.com/{github_org}/{github_repo}.git"
    )


class RepoModifier:
    """Wraps a single target-repo checkout and its git operations."""

    def __init__(self, repo: git.Repo, default_branch: str) -> None:
        """Prefer :meth:`clone` to construct instances.

        Args:
            repo: An open GitPython ``Repo``.
            default_branch: The repo's base branch (e.g. ``main``) that PRs
                target and onboarding branches fork from.
        """
        self._repo = repo
        self._default_branch = default_branch

    @property
    def default_branch(self) -> str:
        """The base branch onboarding branches fork from / PRs target."""
        return self._default_branch

    @property
    def working_dir(self) -> str:
        """Absolute path to the checkout."""
        return self._repo.working_tree_dir or ""

    # ------------------------------------------------------------- clone

    @classmethod
    def clone(
        cls,
        github_org: str,
        github_repo: str,
        token: str,
        local_path: Path | str,
    ) -> "RepoModifier":
        """Clone the repo, or reuse an existing checkout at ``local_path``.

        Re-using an existing checkout keeps re-runs cheap and idempotent. When
        reusing, we fetch so branch-existence checks see the latest remote refs.

        Args:
            github_org: Repo owner/org.
            github_repo: Repo name.
            token: GitHub token for HTTPS auth (never logged).
            local_path: Where to place / find the checkout.

        Returns:
            A ready :class:`RepoModifier`.

        Raises:
            RepoModifierError: On any git failure.
        """
        local_path = Path(local_path)
        url = build_authenticated_url(github_org, github_repo, token)
        try:
            if local_path.exists() and (local_path / ".git").exists():
                logger.info("Reusing existing checkout at %s", local_path)
                repo = git.Repo(str(local_path))
                # Ensure origin points at the authenticated URL for push.
                repo.remotes.origin.set_url(url)
                repo.remotes.origin.fetch(prune=True)
            else:
                logger.info(
                    "Cloning %s/%s into %s", github_org, github_repo, local_path
                )
                repo = git.Repo.clone_from(url, str(local_path))
        except git.GitCommandError as exc:
            # GitPython includes the command in the message; scrub the token.
            raise RepoModifierError(
                f"git clone/fetch failed for {github_org}/{github_repo}: "
                f"{_scrub(str(exc), token)}"
            ) from exc

        default_branch = cls._detect_default_branch(repo)
        return cls(repo, default_branch)

    @staticmethod
    def _detect_default_branch(repo: git.Repo) -> str:
        """Detect the remote's default branch (HEAD), falling back to main."""
        try:
            # origin/HEAD -> origin/<default>
            ref = repo.remotes.origin.refs["HEAD"].reference.name
            return ref.split("/", 1)[1]  # "origin/main" -> "main"
        except (KeyError, IndexError, AttributeError):
            for candidate in ("main", "master"):
                if candidate in [h.name for h in repo.heads]:
                    return candidate
            return "main"

    # ------------------------------------------------- branch (idempotent)

    def remote_branch_exists(self, branch_name: str) -> bool:
        """Return True if ``branch_name`` exists on ``origin``.

        Uses ``git ls-remote --heads`` so the answer reflects the live remote,
        not just locally-known refs. This is idempotency check #1: it lets the
        caller decide to reuse an existing onboarding branch instead of
        creating a duplicate.
        """
        try:
            output = self._repo.git.ls_remote("--heads", "origin", branch_name)
        except git.GitCommandError as exc:
            raise RepoModifierError(f"git ls-remote failed: {exc}") from exc
        return bool(output.strip())

    def ensure_branch(self, branch_name: str) -> bool:
        """Check out ``branch_name``, creating it from the base branch if new.

        Idempotency: if the branch already exists on the remote, we check it
        out as a tracking branch and reuse it (returning ``existed=True``);
        otherwise we create it fresh from the up-to-date default branch.

        Args:
            branch_name: The onboarding branch to create or reuse.

        Returns:
            ``True`` if the branch already existed remotely (reused),
            ``False`` if it was created fresh.
        """
        existed = self.remote_branch_exists(branch_name)
        try:
            if existed:
                logger.info("Branch %s exists on origin; reusing it.", branch_name)
                # Track the remote branch. Use -B to be safe if a stale local
                # branch of the same name already exists (resets it to remote).
                self._repo.git.fetch("origin", branch_name)
                self._repo.git.checkout("-B", branch_name, f"origin/{branch_name}")
            else:
                logger.info(
                    "Creating branch %s from origin/%s.",
                    branch_name,
                    self._default_branch,
                )
                self._repo.git.fetch("origin", self._default_branch)
                self._repo.git.checkout(
                    "-B", branch_name, f"origin/{self._default_branch}"
                )
        except git.GitCommandError as exc:
            raise RepoModifierError(
                f"Failed to check out branch {branch_name}: {exc}"
            ) from exc
        return existed

    # ----------------------------------------------------- commit & push

    def has_changes(self) -> bool:
        """Return True if the working tree has uncommitted (incl. untracked) changes."""
        return self._repo.is_dirty(untracked_files=True)

    def commit_all(self, message: str, author_name: str, author_email: str) -> Optional[str]:
        """Stage everything and commit.

        Args:
            message: Commit message.
            author_name: Commit author/committer name.
            author_email: Commit author/committer email.

        Returns:
            The new commit SHA, or ``None`` if there was nothing to commit
            (which keeps re-runs idempotent).
        """
        if not self.has_changes():
            logger.info("No changes to commit; skipping commit.")
            return None

        self._repo.git.add(A=True)
        actor = git.Actor(author_name, author_email)
        commit = self._repo.index.commit(message, author=actor, committer=actor)
        logger.info("Committed %s: %s", commit.hexsha[:8], message.splitlines()[0])
        return commit.hexsha

    def push(self, branch_name: str, token: str) -> None:
        """Push ``branch_name`` to origin (no force).

        Args:
            branch_name: Branch to push.
            token: Token, used only to scrub error messages.

        Raises:
            RepoModifierError: On push failure.
        """
        try:
            origin = self._repo.remote(name="origin")
            # Plain refspec push; never --force.
            results = origin.push(refspec=f"{branch_name}:{branch_name}")
            for info in results:
                if info.flags & info.ERROR:
                    raise RepoModifierError(
                        f"Push of {branch_name} rejected: {info.summary}"
                    )
            logger.info("Pushed branch %s to origin.", branch_name)
        except git.GitCommandError as exc:
            raise RepoModifierError(
                f"git push failed for {branch_name}: {_scrub(str(exc), token)}"
            ) from exc


def _scrub(message: str, token: str) -> str:
    """Remove a token substring from an error message before logging/raising."""
    if token and token in message:
        return message.replace(token, "***")
    return message
