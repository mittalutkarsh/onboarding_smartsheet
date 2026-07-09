"""Smartsheet REST client for the Git Onboarding Tracker.

Responsibilities:
  * Read the tracker sheet (``GET /sheets/{id}``).
  * Build the **bidirectional** column-ID <-> title map.
  * Convert rows into onboarding records keyed by column title.
  * Write status / PR URL / timestamp / error back to the exact source row
    (``PUT /sheets/{id}/rows``).

Only real, documented Smartsheet API v2 behavior is used:
  * ``GET  https://api.smartsheet.com/2.0/sheets/{sheetId}`` returns
    ``columns`` (each with ``id`` and ``title``) and ``rows`` (each with
    ``id`` and ``cells`` carrying ``columnId`` + ``value``/``displayValue``).
  * ``PUT  https://api.smartsheet.com/2.0/sheets/{sheetId}/rows`` accepts a
    JSON array of row objects ``[{"id": <rowId>, "cells": [...]}]`` and updates
    those cells in place.

Auth is a Bearer token in the ``Authorization`` header.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_API_BASE = "https://api.smartsheet.com/2.0"


class SmartsheetError(RuntimeError):
    """Raised when a Smartsheet API call fails or returns unexpected data."""


class SmartsheetClient:
    """Thin wrapper around the Smartsheet v2 REST API for one tracker sheet.

    The client caches the column map after the first sheet fetch so that
    write-back can translate titles -> column IDs without a second round trip.
    """

    def __init__(self, token: str, sheet_id: str, timeout: int = 30) -> None:
        """Create a client bound to a single sheet.

        Args:
            token: Smartsheet API access token (from ``SMARTSHEET_TOKEN``).
            sheet_id: Numeric sheet ID as a string (from
                ``SMARTSHEET_SHEET_ID``).
            timeout: Per-request timeout in seconds.
        """
        if not token:
            raise SmartsheetError("Smartsheet token is empty.")
        if not sheet_id:
            raise SmartsheetError("Smartsheet sheet ID is empty.")

        self._sheet_id = str(sheet_id)
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

        # --- The two-directional column map (built lazily on first fetch). ---
        # id_to_title:  Smartsheet columnId (int) -> human title (str)
        # title_to_id:  human title (str)         -> Smartsheet columnId (int)
        # We keep BOTH because reading rows needs id->title (cells only carry
        # columnId) while writing back needs title->id (we address cells by the
        # business-friendly title). Building them together from one source of
        # truth guarantees they never drift.
        self._id_to_title: Dict[int, str] = {}
        self._title_to_id: Dict[str, int] = {}

    # ------------------------------------------------------------------ read

    def get_sheet(self) -> Dict[str, Any]:
        """Fetch the full sheet payload and (re)build the column map.

        Returns:
            The parsed JSON sheet object.

        Raises:
            SmartsheetError: On HTTP error or malformed response.
        """
        url = f"{_API_BASE}/sheets/{self._sheet_id}"
        try:
            resp = self._session.get(url, timeout=self._timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:  # network / HTTP failure
            raise SmartsheetError(f"Failed to fetch sheet {self._sheet_id}: {exc}") from exc

        sheet = resp.json()
        self._build_column_map(sheet)
        return sheet

    def _build_column_map(self, sheet: Dict[str, Any]) -> None:
        """Populate both directions of the column map from a sheet payload."""
        columns = sheet.get("columns")
        if not columns:
            raise SmartsheetError("Sheet payload has no 'columns' array.")
        self._id_to_title = {col["id"]: col["title"] for col in columns}
        self._title_to_id = {col["title"]: col["id"] for col in columns}
        logger.debug("Built column map with %d columns.", len(self._id_to_title))

    def row_to_dict(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Convert one Smartsheet row into a title-keyed record.

        Args:
            row: A row object from the sheet payload.

        Returns:
            Dict keyed by column title, plus ``row_id`` for write-back.
        """
        data: Dict[str, Any] = {"row_id": row["id"]}
        for cell in row.get("cells", []):
            title = self._id_to_title.get(cell.get("columnId"))
            if title is None:
                continue  # unknown/hidden column; skip
            data[title] = cell.get("value")
        return data

    def get_ready_rows(self) -> List[Dict[str, Any]]:
        """Return records whose ``Onboarding Status`` == ``Ready``.

        Returns:
            List of title-keyed records ready for onboarding.
        """
        sheet = self.get_sheet()
        records: List[Dict[str, Any]] = []
        for row in sheet.get("rows", []):
            record = self.row_to_dict(row)
            if record.get("Onboarding Status") == "Ready":
                records.append(record)
        logger.info("Found %d row(s) with Onboarding Status = Ready.", len(records))
        return records

    # ----------------------------------------------------------------- write

    def update_row(self, row_id: int, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Write cell values back to a single row, addressing cells by title.

        Uses the cached title->id map to translate business-friendly column
        titles into the column IDs the API requires. Unknown titles are
        skipped with a warning rather than failing the whole write, so a
        renamed column never blocks an error message from being recorded.

        Args:
            row_id: The Smartsheet row ID to update (from ``record['row_id']``).
            updates: Mapping of column *title* -> new value.

        Returns:
            The parsed JSON response from Smartsheet.

        Raises:
            SmartsheetError: If the column map is unavailable or the call fails.
        """
        if not self._title_to_id:
            # Callers normally fetch rows first; guard the standalone case.
            self.get_sheet()

        cells: List[Dict[str, Any]] = []
        for title, value in updates.items():
            column_id = self._title_to_id.get(title)
            if column_id is None:
                logger.warning("Skipping unknown column title on write-back: %r", title)
                continue
            cells.append({"columnId": column_id, "value": value})

        if not cells:
            raise SmartsheetError(
                f"No known columns to update for row {row_id}; updates={list(updates)}"
            )

        payload = [{"id": row_id, "cells": cells}]
        url = f"{_API_BASE}/sheets/{self._sheet_id}/rows"
        try:
            resp = self._session.put(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SmartsheetError(
                f"Failed to update row {row_id} on sheet {self._sheet_id}: {exc}"
            ) from exc

        logger.info("Updated Smartsheet row %s (%d cell(s)).", row_id, len(cells))
        return resp.json()

    # ------------------------------------------------------------- accessors

    def id_to_title(self) -> Dict[int, str]:
        """Return a copy of the columnId -> title map."""
        return dict(self._id_to_title)

    def title_to_id(self) -> Dict[str, int]:
        """Return a copy of the title -> columnId map."""
        return dict(self._title_to_id)
