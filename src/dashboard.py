"""Dynamic web dashboard for the Git Onboarding Tracker.

A small, dependency-light HTTP server (Python stdlib + jinja2, both already
required) that renders live onboarding metrics in the browser. It reuses the
existing :class:`SmartsheetClient`, so it works two ways:

  * **offline (default)** -- reads a local sheet JSON fixture; edit the fixture
    (or the CSV re-exported to JSON) and the next auto-refresh reflects it.
  * **live** (``--live``) -- reads the real Smartsheet sheet each refresh using
    ``SMARTSHEET_TOKEN`` / ``SMARTSHEET_SHEET_ID``, so the page mirrors the
    tracker in near-real-time.

The page polls ``/api/data`` on an interval (default 10s) and re-renders KPI
tiles, a status breakdown, an environment breakdown, and blocked / open-PR
tables. Data is recomputed from the source on every request, which is what
makes it dynamic.

This is beyond the original Week-1 spec (the spec's dashboard was a native
Smartsheet dashboard); it is provided because a dynamic dashboard was requested.

Run:
    python src/dashboard.py                       # offline, sample fixture
    python src/dashboard.py --fixture path.json   # offline, custom fixture
    python src/dashboard.py --live                # live Smartsheet sheet
    python src/dashboard.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape

from smartsheet_client import SmartsheetClient, SmartsheetError

logger = logging.getLogger("dashboard")

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_DEFAULT_FIXTURE = Path(__file__).resolve().parent.parent / "samples" / "sheet-fixture.json"
_STATUS_ORDER = ["New", "Ready", "PR Created", "Merged", "Blocked"]


def _now_iso() -> str:
    """Human-friendly UTC timestamp for the 'updated' indicator."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _records_from_client(client: SmartsheetClient) -> List[Dict[str, Any]]:
    """Fetch the sheet and return every row as a title-keyed record."""
    sheet = client.get_sheet()
    return [client.row_to_dict(row) for row in sheet.get("rows", [])]


def aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute dashboard metrics from tracker records.

    Args:
        records: All rows as title-keyed dicts.

    Returns:
        JSON-serializable payload consumed by ``/api/data``:
        KPIs, status/environment breakdowns, and blocked / open-PR tables.
    """
    status_breakdown: Dict[str, int] = {s: 0 for s in _STATUS_ORDER}
    env_counts: Dict[str, int] = {}
    blocked: List[Dict[str, str]] = []
    open_prs: List[Dict[str, str]] = []
    teams = set()

    for rec in records:
        team = (rec.get("Team Name") or "").strip()
        if team:
            teams.add(team)

        status = (rec.get("Onboarding Status") or "New").strip() or "New"
        # Unknown statuses are still counted so nothing is silently dropped.
        status_breakdown[status] = status_breakdown.get(status, 0) + 1

        for env in str(rec.get("Environments") or "").split(","):
            env = env.strip()
            if env:
                env_counts[env] = env_counts.get(env, 0) + 1

        validation = (rec.get("Validation Status") or "").strip()
        if status == "Blocked" or validation == "Fail":
            blocked.append(
                {
                    "team": team or "(unnamed)",
                    "repo": rec.get("GitHub Repo") or "—",
                    "error": rec.get("Error Message") or rec.get("Notes") or "—",
                }
            )

        if status == "PR Created":
            open_prs.append(
                {
                    "team": team or "(unnamed)",
                    "app": rec.get("App / Cookbook Name") or "—",
                    "pr_url": rec.get("PR URL") or "—",
                }
            )

    # Stable, high-to-low environment order for the bar chart.
    env_breakdown = [
        {"env": env, "count": count}
        for env, count in sorted(env_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    return {
        "generated_at": _now_iso(),
        "kpis": {
            "total": len(records),
            "teams": len(teams),
            "ready": status_breakdown.get("Ready", 0),
            "pr_created": status_breakdown.get("PR Created", 0),
            "merged": status_breakdown.get("Merged", 0),
            "blocked": status_breakdown.get("Blocked", 0),
        },
        "status_breakdown": status_breakdown,
        "env_breakdown": env_breakdown,
        "blocked": blocked,
        "open_prs": open_prs,
    }


class DashboardConfig:
    """Resolved runtime configuration for the server."""

    def __init__(self, live: bool, fixture: str, refresh_seconds: int) -> None:
        self.live = live
        self.fixture = fixture
        self.refresh_seconds = refresh_seconds

    def make_client(self) -> SmartsheetClient:
        """Build a fresh client per request so each refresh sees current data."""
        if self.live:
            token = os.environ.get("SMARTSHEET_TOKEN")
            sheet_id = os.environ.get("SMARTSHEET_SHEET_ID")
            if not token or not sheet_id:
                raise SmartsheetError(
                    "Live mode needs SMARTSHEET_TOKEN and SMARTSHEET_SHEET_ID."
                )
            return SmartsheetClient(token, sheet_id)
        return SmartsheetClient.from_fixture(self.fixture)

    @property
    def source_label(self) -> str:
        return "live Smartsheet" if self.live else f"fixture ({Path(self.fixture).name})"


def _make_handler(config: DashboardConfig):
    """Build a request handler class bound to ``config``.

    The page shell is rendered once from the Jinja template; live data is served
    separately from ``/api/data`` so the browser can poll without reloading.
    """
    jinja = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    page_html = jinja.get_template("dashboard.html.j2").render(
        refresh_seconds=config.refresh_seconds, data_url="/api/data"
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "OnboardingDashboard/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # route through logging
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if self.path in ("/", "/index.html"):
                self._send(200, page_html.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path.startswith("/api/data"):
                self._serve_data()
            elif self.path == "/healthz":
                self._send(200, b'{"ok":true}', "application/json")
            else:
                self._send(404, b'{"error":"not found"}', "application/json")

        def _serve_data(self) -> None:
            try:
                client = config.make_client()
                records = _records_from_client(client)
                payload = aggregate(records)
                payload["source"] = config.source_label
                body = json.dumps(payload).encode("utf-8")
                self._send(200, body, "application/json")
            except SmartsheetError as exc:
                logger.error("Data fetch failed: %s", exc)
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self._send(503, body, "application/json")

    return Handler


def serve(config: DashboardConfig, host: str, port: int) -> None:
    """Start the blocking HTTP server."""
    handler = _make_handler(config)
    httpd = ThreadingHTTPServer((host, port), handler)
    logger.info(
        "Dashboard on http://%s:%d  (source: %s, refresh %ds)",
        host if host != "0.0.0.0" else "localhost",
        port,
        config.source_label,
        config.refresh_seconds,
    )
    logger.info("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        httpd.server_close()


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="onboarding-dashboard",
        description="Serve a dynamic web dashboard for the onboarding tracker.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Read the real Smartsheet sheet (needs SMARTSHEET_TOKEN + "
        "SMARTSHEET_SHEET_ID). Default is offline from a fixture.",
    )
    parser.add_argument(
        "--fixture",
        default=str(_DEFAULT_FIXTURE),
        help=f"Sheet JSON fixture for offline mode (default: {_DEFAULT_FIXTURE}).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port.")
    parser.add_argument(
        "--refresh", type=int, default=10, help="Client auto-refresh seconds."
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    # Use the OS/corporate trust store for TLS if truststore is installed. This
    # lets --live work behind a corporate HTTPS-intercepting proxy (where the
    # bundled certifi CAs would otherwise fail). Harmless if not installed.
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001 - optional convenience only
        pass

    args = _parse_args(argv)
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = DashboardConfig(
        live=args.live, fixture=args.fixture, refresh_seconds=max(2, args.refresh)
    )
    serve(config, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
