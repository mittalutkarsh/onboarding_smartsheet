"""Build a STATIC dashboard bundle for GitHub Pages.

The live dashboard (`src/dashboard.py`) needs a running server for `/api/data`.
GitHub Pages only serves static files, so this script bakes the metrics into a
`data.json` next to an `index.html` that fetches it — no server required.

Data source:
    * default            -- a local sheet fixture (safe demo data);
    * ``--live``         -- the real Smartsheet sheet (SMARTSHEET_TOKEN +
                            SMARTSHEET_SHEET_ID; optional SMARTSHEET_API_BASE for
                            EU/Gov regions).

Because GitHub Pages is PUBLIC, ``--anonymize`` scrubs every identifier before
anything is written to disk:
    * Team / repo / app / org names -> stable pseudonyms (Team 01, repo-01, ...)
    * Owner email                   -> dropped entirely
    * PR URLs                       -> "PR (redacted)" (no link, no org/repo)
    * Error / Notes text            -> identifiers replaced, emails/URLs redacted
Only counts, statuses, and environment names (dev/test/stage/prod) remain — none
of which identify a team. Always pair ``--live`` with ``--anonymize`` for a
public deploy.

Output (default ./site):
    site/index.html   -- the dashboard, fetching ./data.json
    site/data.json    -- aggregated (and, with --anonymize, scrubbed) metrics

Run:
    python demo/build_static_dashboard.py                      # fixture (demo)
    python demo/build_static_dashboard.py --live --anonymize   # real, scrubbed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from jinja2 import Environment, FileSystemLoader, select_autoescape

import dashboard  # aggregate(), _records_from_client()
import smartsheet_client
from smartsheet_client import SmartsheetClient

_TEMPLATES_DIR = _REPO / "templates"
_DEFAULT_FIXTURE = _REPO / "samples" / "sheet-fixture.json"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://\S+")


def anonymize_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Replace every identifying field with a non-identifying placeholder.

    Pseudonyms are stable within a single build (same real value -> same alias
    everywhere), so the dashboard's tables stay internally consistent while
    revealing nothing about real teams, repos, or people.

    Args:
        records: Real title-keyed records read from the sheet.

    Returns:
        A new list of records safe to publish on a public page.
    """
    team_map: Dict[str, str] = {}
    repo_map: Dict[str, str] = {}
    app_map: Dict[str, str] = {}
    org_map: Dict[str, str] = {}

    def alias(mapping: Dict[str, str], value: Any, fmt: str) -> Any:
        if value in (None, ""):
            return value
        key = str(value)
        if key not in mapping:
            mapping[key] = fmt.format(n=len(mapping) + 1)
        return mapping[key]

    # Pass 1: assign aliases across all rows.
    for r in records:
        alias(team_map, r.get("Team Name"), "Team {n:02d}")
        alias(repo_map, r.get("GitHub Repo"), "repo-{n:02d}")
        alias(app_map, r.get("App / Cookbook Name"), "app-{n:02d}")
        alias(org_map, r.get("GitHub Org"), "org-{n:02d}")

    # Longest real strings first so "org/repo" is replaced before "repo".
    replacements = sorted(
        {**team_map, **repo_map, **app_map, **org_map}.items(),
        key=lambda kv: len(kv[0]),
        reverse=True,
    )

    def scrub(text: Any) -> Any:
        if text in (None, ""):
            return text
        s = str(text)
        for real, a in replacements:
            if real:
                s = s.replace(real, a)
        s = _EMAIL_RE.sub("[email]", s)
        s = _URL_RE.sub("[link]", s)
        return s

    out: List[Dict[str, Any]] = []
    for r in records:
        r2 = dict(r)
        r2["Team Name"] = team_map.get(str(r.get("Team Name")), r.get("Team Name"))
        r2["GitHub Repo"] = repo_map.get(str(r.get("GitHub Repo")), r.get("GitHub Repo"))
        r2["App / Cookbook Name"] = app_map.get(
            str(r.get("App / Cookbook Name")), r.get("App / Cookbook Name")
        )
        r2["GitHub Org"] = org_map.get(str(r.get("GitHub Org")), r.get("GitHub Org"))
        r2["Owner Email"] = ""  # never publish emails
        r2["PR URL"] = "PR (redacted)" if r.get("PR URL") else r.get("PR URL")
        r2["Error Message"] = scrub(r.get("Error Message"))
        r2["Notes"] = scrub(r.get("Notes"))
        out.append(r2)
    return out


def _make_client(live: bool, fixture: str) -> SmartsheetClient:
    """Build the source client (live real sheet or offline fixture)."""
    if live:
        token = os.environ.get("SMARTSHEET_TOKEN")
        sheet_id = os.environ.get("SMARTSHEET_SHEET_ID")
        if not token or not sheet_id:
            raise SystemExit(
                "ERROR: --live needs SMARTSHEET_TOKEN and SMARTSHEET_SHEET_ID."
            )
        # Optional region override (EU/Gov), e.g. https://api.smartsheet.eu/2.0
        base = os.environ.get("SMARTSHEET_API_BASE")
        if base:
            smartsheet_client._API_BASE = base
        return SmartsheetClient(token, sheet_id)
    return SmartsheetClient.from_fixture(fixture)


def build(
    out_dir: str,
    live: bool = False,
    fixture: str = str(_DEFAULT_FIXTURE),
    anonymize: bool = False,
    refresh_seconds: int = 30,
) -> None:
    """Render index.html + data.json into ``out_dir``."""
    client = _make_client(live, fixture)
    records = dashboard._records_from_client(client)
    if anonymize:
        records = anonymize_records(records)

    payload = dashboard.aggregate(records)
    if live and anonymize:
        payload["source"] = "live Smartsheet (anonymized)"
    elif live:
        payload["source"] = "live Smartsheet"
    else:
        payload["source"] = f"static export ({Path(fixture).name})"

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    jinja = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    html = jinja.get_template("dashboard.html.j2").render(
        refresh_seconds=refresh_seconds, data_url="data.json"
    )
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "data.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {out/'index.html'} and {out/'data.json'}")
    print(f"source={payload['source']}  kpis={payload['kpis']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build a static dashboard for Pages.")
    parser.add_argument("--live", action="store_true",
                        help="Read the real Smartsheet sheet (SMARTSHEET_* env).")
    parser.add_argument("--anonymize", action="store_true",
                        help="Strip all identifiers before writing (use for public deploys).")
    parser.add_argument("--fixture", default=str(_DEFAULT_FIXTURE))
    parser.add_argument("--out", default=str(_REPO / "site"))
    parser.add_argument("--refresh", type=int, default=30)
    args = parser.parse_args(argv)
    build(args.out, live=args.live, fixture=args.fixture,
          anonymize=args.anonymize, refresh_seconds=args.refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
