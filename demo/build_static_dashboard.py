"""Build a STATIC dashboard bundle for GitHub Pages.

The live dashboard (`src/dashboard.py`) needs a running server for `/api/data`.
GitHub Pages only serves static files, so this script bakes the metrics into a
`data.json` next to an `index.html` that fetches it — no server required. The
page is still interactive (auto-refreshes, light/dark, hover); the data is just
a fixed snapshot computed at build time.

Output (default ./site):
    site/index.html   -- the dashboard, fetching ./data.json
    site/data.json    -- aggregated metrics from the sample fixture

Run:
    python demo/build_static_dashboard.py                 # uses samples/sheet-fixture.json
    python demo/build_static_dashboard.py --fixture X --out site
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from jinja2 import Environment, FileSystemLoader, select_autoescape

import dashboard  # aggregate(), _records_from_client()
from smartsheet_client import SmartsheetClient

_TEMPLATES_DIR = _REPO / "templates"
_DEFAULT_FIXTURE = _REPO / "samples" / "sheet-fixture.json"


def build(fixture: str, out_dir: str, refresh_seconds: int = 30) -> None:
    """Render index.html + data.json into ``out_dir``."""
    client = SmartsheetClient.from_fixture(fixture)
    records = dashboard._records_from_client(client)
    payload = dashboard.aggregate(records)
    payload["source"] = f"static export ({Path(fixture).name})"

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    jinja = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    # Static export points the page at a sibling data.json instead of /api/data.
    html = jinja.get_template("dashboard.html.j2").render(
        refresh_seconds=refresh_seconds, data_url="data.json"
    )
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "data.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {out/'index.html'} and {out/'data.json'}")
    print(f"KPIs: {payload['kpis']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build a static dashboard for Pages.")
    parser.add_argument("--fixture", default=str(_DEFAULT_FIXTURE))
    parser.add_argument("--out", default=str(_REPO / "site"))
    parser.add_argument("--refresh", type=int, default=30)
    args = parser.parse_args(argv)
    build(args.fixture, args.out, args.refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
