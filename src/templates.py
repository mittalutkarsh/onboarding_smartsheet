"""Render onboarding files from Jinja2 templates into a target repo checkout.

Given a validated onboarding context and the path to a cloned repo, this module
writes:

  * ``.github/workflows/deploy-<env>.yml``  (one per environment, from
    ``workflow.yml.j2``)
  * ``onboarding/app-config.yml``           (from ``app-config.yml.j2``)
  * ``onboarding/policy-groups.yml``        (from ``policy-groups.yml.j2``)
  * ``onboarding/manifest.yml``             (from ``manifest.yml.j2``)
  * ``docs/onboarding.md``                  (from ``onboarding.md.j2``)

Rendering is a pure filesystem write into the checkout; the surrounding git
work (branch, commit, push) is handled by ``repo_modifier.py``. Overwriting the
same files on a re-run is intentional and keeps the operation idempotent.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

from jinja2 import Environment, FileSystemLoader, StrictUndefined

logger = logging.getLogger(__name__)

# Default templates directory: ../templates relative to this file (src/).
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class TemplateRenderer:
    """Renders the onboarding file set for a single repository."""

    def __init__(self, templates_dir: Path | str = DEFAULT_TEMPLATES_DIR) -> None:
        """Create a renderer bound to a templates directory.

        Args:
            templates_dir: Directory holding the ``*.j2`` templates.
        """
        self._templates_dir = Path(templates_dir)
        # StrictUndefined makes a missing context key raise at render time
        # rather than silently emitting an empty string -- we never want a
        # half-populated config file to reach a reviewer.
        self._env = Environment(
            loader=FileSystemLoader(str(self._templates_dir)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
            undefined=StrictUndefined,
        )

    def _render(self, template_name: str, context: Dict) -> str:
        """Render one template to a string."""
        template = self._env.get_template(template_name)
        return template.render(**context)

    @staticmethod
    def _write(repo_root: Path, rel_path: str, content: str) -> Path:
        """Write ``content`` to ``repo_root/rel_path``, creating parents."""
        out = repo_root / rel_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
        return out

    def render_all(self, repo_root: Path | str, context: Dict) -> List[Path]:
        """Render every onboarding file into ``repo_root``.

        Args:
            repo_root: Path to the cloned target repository checkout.
            context: Template context. Must include ``app_name``, ``team_name``,
                ``github_org``, ``github_repo``, ``owner_email`` and
                ``environments`` (a list).

        Returns:
            List of written file paths (for logging / debugging).
        """
        repo_root = Path(repo_root)
        environments: List[str] = list(context.get("environments", []))
        written: List[Path] = []

        # One deploy workflow per environment. The workflow template reads a
        # single ``environment`` key, so we render it repeatedly with a shallow
        # copy of the context per env.
        for env in environments:
            env_context = dict(context)
            env_context["environment"] = env
            content = self._render("workflow.yml.j2", env_context)
            written.append(
                self._write(repo_root, f".github/workflows/deploy-{env}.yml", content)
            )

        # Static (per-repo) onboarding files.
        file_map = {
            "app-config.yml.j2": "onboarding/app-config.yml",
            "policy-groups.yml.j2": "onboarding/policy-groups.yml",
            "manifest.yml.j2": "onboarding/manifest.yml",
            "onboarding.md.j2": "docs/onboarding.md",
        }
        for template_name, rel_path in file_map.items():
            content = self._render(template_name, context)
            written.append(self._write(repo_root, rel_path, content))

        logger.info("Rendered %d onboarding file(s) into %s.", len(written), repo_root)
        return written
