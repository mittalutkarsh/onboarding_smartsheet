"""Validation of Smartsheet onboarding records.

Week-1 scope: check that the fields the rest of the pipeline depends on are
present and well-formed. This module has **no side effects** (no network, no
git) and never fabricates defaults — if a required field is missing or invalid,
the row fails and the reason is recorded. The caller (``main.py``) is
responsible for writing the failure back to Smartsheet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

# Columns the downstream pipeline cannot proceed without.
REQUIRED_FIELDS = (
    "Team Name",
    "GitHub Org",
    "GitHub Repo",
    "App / Cookbook Name",
    "Owner Email",
    "Environments",
)

# Deliberately permissive email check: we only guard against obviously bad
# input, not RFC-5322 edge cases. Reviewer still sees the value in the PR.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Allowed environment tokens. Kept small and explicit for Week-1; extend as the
# spec grows. (Flagged: spec example uses exactly dev/test/stage/prod.)
_ALLOWED_ENVIRONMENTS = {"dev", "test", "stage", "prod"}


@dataclass
class ValidationResult:
    """Outcome of validating a single onboarding record.

    Attributes:
        ok: True when the record passed every check.
        errors: Human-readable reasons the record failed (empty when ok).
        environments: Parsed, normalized environment list (only meaningful
            when ok is True).
    """

    ok: bool
    errors: List[str] = field(default_factory=list)
    environments: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """One-line summary suitable for the Smartsheet ``Error Message`` cell."""
        if self.ok:
            return "All required fields present and valid."
        return "; ".join(self.errors)


def parse_environments(raw: object) -> List[str]:
    """Parse the ``Environments`` cell into a clean list of tokens.

    The tracker stores environments as a comma-separated string
    (e.g. ``"dev,test,stage,prod"``). Whitespace is trimmed, blanks dropped,
    and order preserved.

    Args:
        raw: The raw cell value (usually ``str``; may be ``None``).

    Returns:
        Ordered list of non-empty, stripped environment tokens.
    """
    if raw is None:
        return []
    text = str(raw)
    return [token.strip() for token in text.split(",") if token.strip()]


def validate_record(record: dict) -> ValidationResult:
    """Validate a single onboarding record read from Smartsheet.

    Args:
        record: Row dict keyed by column *title* (see
            :func:`smartsheet_client.row_to_dict`).

    Returns:
        A :class:`ValidationResult`. On success, ``environments`` holds the
        parsed environment list so the caller does not parse it twice.
    """
    errors: List[str] = []

    # 1. Required, non-empty fields.
    for field_name in REQUIRED_FIELDS:
        value = record.get(field_name)
        if value is None or str(value).strip() == "":
            errors.append(f"Missing required field: {field_name}")

    # 2. Owner email shape (only if present, to avoid duplicate noise).
    owner = record.get("Owner Email")
    if owner and not _EMAIL_RE.match(str(owner).strip()):
        errors.append(f"Owner Email is not a valid email address: {owner!r}")

    # 3. Environments must parse to at least one recognized token.
    environments = parse_environments(record.get("Environments"))
    if record.get("Environments") not in (None, "") and not environments:
        errors.append("Environments field is present but empty after parsing.")
    unknown = [e for e in environments if e not in _ALLOWED_ENVIRONMENTS]
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_ENVIRONMENTS))
        errors.append(
            f"Unknown environment(s): {', '.join(unknown)}. Allowed: {allowed}."
        )

    if errors:
        return ValidationResult(ok=False, errors=errors)
    return ValidationResult(ok=True, environments=environments)
